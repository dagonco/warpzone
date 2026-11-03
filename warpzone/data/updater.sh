#!/bin/sh
# warpzone updater: keeps a device's git checkout on a release that works.
#
# POSIX sh, git and the usual tools only. It lives outside the warpzone package on purpose
# (a broken release must not be able to stop the tool that puts the good one back) and its
# rules are written in docs/design.md, "Autonomous updates (contract)".
#
#   updater.sh tick      one run of the timer: update, verify, roll back, reconcile
#   updater.sh menu      exit 0 when the frontend is running, 1 when not, 2 when unknown
#   updater.sh selftest  check the tools this script needs and its own helpers
#
# Settings are read, line by line and never sourced, from a key=value file; the state is a
# key=value file only this script writes. Both can be moved for tests:
#   WZ_STATE_DIR  folder of `state`, `conf`, `bad/` and the lock (default
#                 $XDG_STATE_HOME/warpzone/updater)
#   WZ_CONF       the settings file (default $WZ_STATE_DIR/conf)
#   WZ_LOG        the log (default self-update.log next to WZ_STATE_DIR)
#   WZ_PROC       where /proc is; WZ_BOOT_ID_FILE the boot id
#   WZ_NOW        the time as seconds since the epoch
#   WZ_FETCH_INTERVAL  seconds between fetches (default one day)
#   WZ_STEP_TIMEOUT    seconds for each step of a verification
#   WZ_TICK_BUDGET     seconds a whole tick may take (default 1500, under the service's 30 min)
#   WZ_FETCH_TIMEOUT   seconds for the fetch (default 120)
#   WZ_MIN_FREE_KB     free space in KiB needed before switching (default 51200)
#   WZ_PYTHON_FALLBACKS  interpreters to try after the one in the settings

UPDATER_ABI=1

set -u

DAY=86400
DEFER_MAX=$((72 * 3600))
MAX_ATTEMPTS=2
IMPORT_CODE='import os, sys, warpzone.cli, warpzone.provision, warpzone.health; root = os.path.realpath(sys.argv[1]); here = os.path.realpath(warpzone.__file__); sys.exit(0 if here.startswith(root + os.sep) else 7)'
STATE_KEYS="schema good_tag good_sha good_branch trying_tag trying_sha from_tag from_sha from_branch attempts probation pending deferred_since last_fetch last_boot_id last_result last_ok retry_after env_failures"

STATE_DIR=${WZ_STATE_DIR:-${XDG_STATE_HOME:-${HOME:-/nonexistent}/.local/state}/warpzone/updater}
CONF=${WZ_CONF:-$STATE_DIR/conf}
LOG=${WZ_LOG:-$(dirname "$STATE_DIR")/self-update.log}
PROC=${WZ_PROC:-/proc}
BOOT_ID_FILE=${WZ_BOOT_ID_FILE:-/proc/sys/kernel/random/boot_id}
FETCH_INTERVAL=${WZ_FETCH_INTERVAL:-$DAY}
PY_FALLBACKS=${WZ_PYTHON_FALLBACKS-/usr/bin/python3 python3}
STEP_TIMEOUT=${WZ_STEP_TIMEOUT:-}
FETCH_TIMEOUT=${WZ_FETCH_TIMEOUT:-120}
TICK_BUDGET=${WZ_TICK_BUDGET:-1500}
MIN_FREE_KB=${WZ_MIN_FREE_KB:-51200}
DEADLINE=
SETSID=$(command -v setsid 2>/dev/null || true)

export GIT_TERMINAL_PROMPT=0

# ---- small helpers --------------------------------------------------------------------

# real_now: the clock, whatever WZ_NOW says (the time budget is real time).
real_now() {
	date +%s
}

now() {
	case ${WZ_NOW:-} in
	'' | *[!0-9]*) date +%s ;;
	*) printf '%s\n' "$WZ_NOW" ;;
	esac
}

log() {
	LOG_LINE="$*"
	printf 'updater: %s\n' "$LOG_LINE"
	[ -n "$LOG" ] || return 0
	mkdir -p "$(dirname "$LOG")" 2>/dev/null || return 0
	if [ -f "$LOG" ]; then
		set -- $(wc -c <"$LOG" 2>/dev/null || echo 0)
		if [ "${1:-0}" -gt 65536 ] 2>/dev/null; then
			tail -n 200 "$LOG" >"$LOG.tmp" 2>/dev/null && mv -f "$LOG.tmp" "$LOG"
		fi
	fi
	printf '%s updater: %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$LOG_LINE" >>"$LOG" 2>/dev/null
	return 0
}

# ver_gt A B: succeeds when the plain version A (X.Y.Z) is greater than B.
ver_gt() {
	_a=$1
	_b=$2
	for _i in 1 2 3; do
		_x=${_a%%.*}
		_y=${_b%%.*}
		[ "$_x" -gt "$_y" ] && return 0
		[ "$_x" -lt "$_y" ] && return 1
		case $_a in *.*) _a=${_a#*.} ;; *) _a=0 ;; esac
		case $_b in *.*) _b=${_b#*.} ;; *) _b=0 ;; esac
	done
	return 1
}

# is_plain_tag T: succeeds for vX.Y.Z (numbers only).
is_plain_tag() {
	case $1 in
	v*.*.*) ;;
	*) return 1 ;;
	esac
	_n=${1#v}
	case $_n in
	*[!0-9.]* | .* | *. | *..*) return 1 ;;
	esac
	_dots=${_n#*.}
	_dots=${_dots#*.}
	case $_dots in *.* | '') return 1 ;; esac
	return 0
}

is_number() {
	case $1 in
	'' | *[!0-9]*) return 1 ;;
	esac
	return 0
}

# Numbers that come from the environment: one that is not a number would stop the script
# on its first sum.
case $FETCH_INTERVAL in '' | *[!0-9]*) FETCH_INTERVAL=$DAY ;; esac
case $STEP_TIMEOUT in '' | 0 | *[!0-9]*) STEP_TIMEOUT= ;; esac
case $FETCH_TIMEOUT in '' | *[!0-9]*) FETCH_TIMEOUT=120 ;; esac
case $TICK_BUDGET in '' | *[!0-9]*) TICK_BUDGET=1500 ;; esac
case $MIN_FREE_KB in '' | *[!0-9]*) MIN_FREE_KB=51200 ;; esac

# ---- run with a time limit ------------------------------------------------------------
# run_limited SECONDS COMMAND...: the command's exit status, or 124 when it ran out of
# time (about: the wait is counted in naps). Every command runs in a process group of its
# own (`setsid`, or job control where there is none) and a cut kills the whole group, so a
# grandchild cannot outlive the step or hold a pipe. The output goes wherever the caller
# redirects it, never to a `$(...)`. The seconds are cut down to what is left of the tick's
# budget (DEADLINE); RL_BUDGET_CUT says that happened. Polls a status file, because neither
# `timeout` nor a reliable `kill -0` on a child that has already ended is available
# everywhere.

SLEEP_FRACTIONS=0
if sleep 0.01 2>/dev/null; then
	SLEEP_FRACTIONS=1
fi

# rl_nap SPENT: wait a little (short naps at first, so a quick command costs little) and
# set RL_MS to the milliseconds counted, SPENT being the milliseconds waited so far.
rl_nap() {
	if [ "$SLEEP_FRACTIONS" = 0 ]; then
		sleep 1
		RL_MS=1000
	elif [ "$1" -lt 1000 ]; then
		sleep 0.02
		RL_MS=20
	else
		sleep 0.2
		RL_MS=200
	fi
}

# rl_kill SIGNAL PIDFILE: signal the group of the step whose leader's pid is in PIDFILE,
# or only the leader where groups are not available.
rl_kill() {
	_k_pid=$(cat "$2" 2>/dev/null)
	[ -n "$_k_pid" ] || return 0
	kill -s "$1" -- "-$_k_pid" 2>/dev/null || kill -s "$1" "$_k_pid" 2>/dev/null
	return 0
}

RL_FILE=
RL_BUDGET_CUT=0

# rl_abort: the tick was told to stop; take the step in progress down with it.
rl_abort() {
	[ -n "$RL_FILE" ] || return 0
	rl_kill TERM "$RL_FILE.pid"
	sleep 1
	rl_kill KILL "$RL_FILE.pid"
}

run_limited() {
	_rl_secs=$1
	shift
	RL_BUDGET_CUT=0
	if [ -n "$DEADLINE" ]; then
		_rl_rem=$((DEADLINE - $(real_now)))
		if [ "$_rl_rem" -lt "$_rl_secs" ]; then
			[ "$_rl_rem" -ge 0 ] || _rl_rem=0
			_rl_secs=$_rl_rem
			RL_BUDGET_CUT=1
		fi
	fi
	[ "$_rl_secs" -gt 0 ] || return 124
	RL_FILE="$STATE_DIR/.run.$$"
	rm -f "$RL_FILE" "$RL_FILE.pid" "$RL_FILE.tmp"
	(
		if [ -n "$SETSID" ]; then
			"$SETSID" "$@" </dev/null &
		else
			set -m
			"$@" </dev/null &
		fi
		_p=$!
		echo "$_p" >"$RL_FILE.pid"
		wait "$_p"
		_c=$?
		echo "$_c" >"$RL_FILE.tmp"
		mv -f "$RL_FILE.tmp" "$RL_FILE"
	) &
	_rl_sub=$!
	_rl_left=$((_rl_secs * 1000))
	_rl_spent=0
	while [ ! -e "$RL_FILE" ]; do
		if [ "$_rl_left" -le 0 ]; then
			rl_kill TERM "$RL_FILE.pid"
			_rl_grace=0
			while [ ! -e "$RL_FILE" ] && [ "$_rl_grace" -lt 2000 ]; do
				rl_nap 1000
				_rl_grace=$((_rl_grace + RL_MS))
			done
			rl_kill KILL "$RL_FILE.pid"
			wait "$_rl_sub" 2>/dev/null
			rm -f "$RL_FILE" "$RL_FILE.pid" "$RL_FILE.tmp"
			return 124
		fi
		rl_nap "$_rl_spent"
		_rl_left=$((_rl_left - RL_MS))
		_rl_spent=$((_rl_spent + RL_MS))
	done
	wait "$_rl_sub" 2>/dev/null
	_rl_code=$(cat "$RL_FILE" 2>/dev/null)
	rm -f "$RL_FILE" "$RL_FILE.pid" "$RL_FILE.tmp"
	is_number "$_rl_code" || _rl_code=1
	return "$_rl_code"
}

# ---- settings -------------------------------------------------------------------------

C_device=
C_python=
C_enabled=1
C_frontend=
C_clone=
C_config=

conf_load() {
	[ -f "$CONF" ] || return 1
	while IFS= read -r _line || [ -n "$_line" ]; do
		_line=${_line%"$(printf '\r')"}
		case $_line in
		'' | '#'*) continue ;;
		*=*) ;;
		*) continue ;;
		esac
		_k=${_line%%=*}
		_v=${_line#*=}
		case $_k in
		device) C_device=$_v ;;
		python) C_python=$_v ;;
		frontend) C_frontend=$_v ;;
		clone) C_clone=$_v ;;
		config) C_config=$_v ;;
		enabled)
			case $_v in
			false | 0 | no | off) C_enabled=0 ;;
			*) C_enabled=1 ;;
			esac
			;;
		esac
	done <"$CONF"
	[ -n "$C_frontend" ] || C_frontend="es-de ES-DE.AppImage"
	return 0
}

# ---- state ----------------------------------------------------------------------------

STATE_FILE=$STATE_DIR/state
BAD_DIR=$STATE_DIR/bad

state_reset() {
	for _k in $STATE_KEYS; do
		eval "S_$_k="
	done
	S_attempts=0
	S_env_failures=0
	S_probation=0
	S_pending=0
}

# state_assign KEY VALUE: KEY is one of STATE_KEYS and only letters and underscores.
state_assign() {
	eval "S_$1=\$2"
}

state_load() {
	state_reset
	[ -f "$STATE_FILE" ] || return 0
	while IFS= read -r _line || [ -n "$_line" ]; do
		case $_line in *=*) ;; *) continue ;; esac
		_k=${_line%%=*}
		_v=${_line#*=}
		case $_k in '' | *[!a-z_]*) continue ;; esac
		case " $STATE_KEYS " in
		*" $_k "*) state_assign "$_k" "$_v" ;;
		esac
	done <"$STATE_FILE"
	for _k in attempts env_failures; do
		eval "_v=\$S_$_k"
		is_number "$_v" || state_assign "$_k" 0
	done
	for _k in probation pending; do
		eval "_v=\$S_$_k"
		[ "$_v" = 1 ] || state_assign "$_k" 0
	done
	for _k in deferred_since last_fetch retry_after last_ok; do
		eval "_v=\$S_$_k"
		is_number "$_v" || state_assign "$_k" ""
	done
	case $S_good_sha$S_trying_sha$S_from_sha in
	*[!0-9a-f]*) S_good_sha= S_trying_sha= S_from_sha= ;;
	esac
	# These end up in file names and in git commands: only what a tag and a branch can be.
	for _k in good_tag trying_tag from_tag; do
		eval "_v=\$S_$_k"
		[ -z "$_v" ] || is_plain_tag "$_v" || state_assign "$_k" ""
	done
	for _k in good_branch from_branch; do
		eval "_v=\$S_$_k"
		case $_v in
		'') ;;
		-* | *[!A-Za-z0-9._/-]*) state_assign "$_k" "" ;;
		esac
	done
}

# state_drop_future: a clock that was wrong (no battery-backed RTC) can leave times in the
# state that lie ahead of now; they would hold the device back for no reason.
state_drop_future() {
	for _k in last_fetch deferred_since last_ok; do
		eval "_v=\$S_$_k"
		if [ -n "$_v" ] && [ "$_v" -gt $((NOW + DAY)) ]; then
			state_assign "$_k" ""
		fi
	done
	if [ -n "$S_retry_after" ] && [ "$S_retry_after" -gt $((NOW + 8 * DAY)) ]; then
		S_retry_after=
	fi
}

# state_save: write every key to a temporary file, flush it and rename it over the state.
state_save() {
	S_schema=1
	mkdir -p "$STATE_DIR" || return 1
	_tmp="$STATE_FILE.tmp.$$"
	{
		for _k in $STATE_KEYS; do
			eval "_v=\$S_$_k"
			printf '%s=%s\n' "$_k" "$_v"
		done
	} >"$_tmp" || {
		log "cannot write $_tmp"
		return 1
	}
	sync
	mv -f "$_tmp" "$STATE_FILE" || return 1
	sync
	return 0
}

is_bad() {
	[ -f "$BAD_DIR/$1" ]
}

# write_bad TAG SHA REASON
write_bad() {
	mkdir -p "$BAD_DIR" || return 1
	_tmp="$BAD_DIR/.$1.tmp.$$"
	_why=$(printf '%s' "$3" | tr '\n' ' ')
	printf 'sha=%s\ndate=%s\nreason=%s\n' "$2" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$_why" >"$_tmp" || return 1
	sync
	mv -f "$_tmp" "$BAD_DIR/$1"
	sync
}

# ---- lock -----------------------------------------------------------------------------

LOCK_DIR=$STATE_DIR/lock
LOCK_HELD=0

lock_claim() {
	echo "$$" >"$LOCK_DIR/pid"
	boot_id >"$LOCK_DIR/boot"
	LOCK_HELD=1
}

acquire_lock() {
	if mkdir "$LOCK_DIR" 2>/dev/null; then
		lock_claim
		return 0
	fi
	_old=$(cat "$LOCK_DIR/pid" 2>/dev/null)
	_old_boot=$(cat "$LOCK_DIR/boot" 2>/dev/null)
	_cur_boot=$(boot_id)
	if [ -z "$_old" ]; then
		# Someone is between mkdir and writing the pid, unless the folder is old.
		[ -n "$(find "$LOCK_DIR" -prune -mmin +1 2>/dev/null)" ] || return 1
	elif [ -n "$_old_boot" ] && [ -n "$_cur_boot" ] && [ "$_old_boot" != "$_cur_boot" ]; then
		: # written before the last restart: the pid belongs to somebody else now
	elif kill -0 "$_old" 2>/dev/null; then
		# A lock older than any run can last is stale even with a live pid.
		[ -n "$(find "$LOCK_DIR" -prune -mmin +180 2>/dev/null)" ] || return 1
	fi
	# Take it over by renaming it to a name of our own (atomic: only one tick wins), and
	# check that it is the lock we judged stale and not a fresh one that took its place.
	_stale="$LOCK_DIR.stale.$$"
	mv "$LOCK_DIR" "$_stale" 2>/dev/null || return 1
	if [ "$(cat "$_stale/pid" 2>/dev/null)" != "$_old" ]; then
		[ -e "$LOCK_DIR" ] || mv "$_stale" "$LOCK_DIR" 2>/dev/null
		rm -rf "$_stale"
		return 1
	fi
	rm -rf "$_stale"
	mkdir "$LOCK_DIR" 2>/dev/null || return 1
	lock_claim
	return 0
}

release_lock() {
	if [ "$LOCK_HELD" = 1 ]; then
		rm -rf "$LOCK_DIR"
		LOCK_HELD=0
	fi
}

# ---- processes ------------------------------------------------------------------------
# proc_has NAME...: 0 when a process is one of the names, judged by `comm` (the kernel keeps
# 15 characters) and the basename of argv[0] the way settings_state.running does; never by
# the rest of a command line. 1 when none is, 2 when /proc cannot be read.

proc_has() {
	[ -d "$PROC" ] && [ -r "$PROC" ] || return 2
	for _d in "$PROC"/[0-9]*; do
		[ -d "$_d" ] || continue
		_pid=${_d##*/}
		[ "$_pid" = "$$" ] && continue
		[ "$_pid" = "$PPID" ] && continue
		_comm=
		[ -r "$_d/comm" ] && IFS= read -r _comm <"$_d/comm" 2>/dev/null
		_argv0=
		if [ -r "$_d/cmdline" ]; then
			_argv0=$(tr '\0' '\n' <"$_d/cmdline" 2>/dev/null | head -n 1)
		fi
		_base=${_argv0##*/}
		for _n in "$@"; do
			_short=$(printf '%.15s' "$_n")
			if [ -n "$_comm" ] && [ "$_comm" = "$_short" ]; then
				return 0
			fi
			if [ -n "$_base" ] && [ "$_base" = "$_n" ]; then
				return 0
			fi
		done
	done
	return 1
}

menu_open() {
	set -f # a name is a name, never a pattern for the files of the current folder
	# shellcheck disable=SC2086
	set -- $C_frontend
	set +f
	proc_has "$@"
}

boot_id() {
	_b=
	[ -r "$BOOT_ID_FILE" ] && IFS= read -r _b <"$BOOT_ID_FILE" 2>/dev/null
	printf '%s' "$_b"
}

# ---- git ------------------------------------------------------------------------------

CLONE=

g() {
	git -C "$CLONE" "$@"
}

head_sha() {
	g rev-parse HEAD 2>/dev/null
}

# version_at REV: the __version__ in warpzone/__init__.py at REV.
version_at() {
	g show "$1:warpzone/__init__.py" 2>/dev/null |
		sed -n 's/^__version__ = "\(.*\)"[[:space:]]*$/\1/p' | head -n 1
}

# checkout_ref BRANCH SHA: back onto the branch when it still points at SHA, otherwise
# detached at SHA. Never resets, stashes or cleans.
checkout_ref() {
	if [ -n "$1" ]; then
		_at=$(g rev-parse -q --verify "refs/heads/$1" 2>/dev/null)
		if [ "$_at" = "$2" ]; then
			g checkout --quiet --no-overwrite-ignore "$1" -- >/dev/null 2>&1
			return $?
		fi
	fi
	g checkout --quiet --no-overwrite-ignore --detach "$2" >/dev/null 2>&1
}

# free_kb prints the free space, in KiB, of the file system of the clone (nothing if unknown).
free_kb() {
	set -- $(df -Pk "$CLONE" 2>/dev/null | tail -n 1)
	if is_number "${4:-}"; then
		printf '%s' "$4"
	fi
}

# heal_half_checkout A B TARGET: a checkout between the commits A and B was cut in the middle
# (a power cut, a full disk), so the tree is neither one nor the other. When every file that
# differs from HEAD is one that A and B differ in, the change is the updater's own and the
# checkout is done again, forced, onto TARGET; anything else is somebody's work and stays.
# Succeeds when the tree is consistent afterwards.
heal_half_checkout() {
	[ -n "$1" ] && [ -n "$2" ] || return 1
	_changed=$(g diff --name-only "$1" "$2" 2>/dev/null) || return 1
	_dirty=$(g diff --name-only HEAD 2>/dev/null) || return 1
	[ -n "$_dirty" ] || return 0
	_own=1
	_ifs=$IFS
	IFS='
'
	set -f
	for _f in $_dirty; do
		printf '%s\n' "$_changed" | grep -Fxq -- "$_f" || _own=0
	done
	set +f
	IFS=$_ifs
	if [ "$_own" = 0 ]; then
		log "the tree has changes that are not part of the update; left alone"
		return 1
	fi
	log "an update was cut in the middle; doing its checkout again"
	g checkout --quiet --force --no-overwrite-ignore --detach "$3" >/dev/null 2>&1
}

# detect_kind sets KIND to tag, adopted, dev, dirty or nogit, and BRANCH.
KIND=
BRANCH=
detect_kind() {
	KIND=nogit
	BRANCH=
	g rev-parse --git-dir >/dev/null 2>&1 || return 0
	_st=$(g status --porcelain --untracked-files=no 2>/dev/null) || {
		KIND=dirty
		return 0
	}
	if [ -n "$_st" ]; then
		KIND=dirty
		return 0
	fi
	BRANCH=$(g symbolic-ref -q --short HEAD 2>/dev/null) || BRANCH=
	if [ -z "$BRANCH" ]; then
		if g describe --exact-match --tags --match 'v[0-9]*' HEAD >/dev/null 2>&1; then
			KIND=tag
		else
			KIND=dev
		fi
	elif g merge-base --is-ancestor HEAD origin/main >/dev/null 2>&1; then
		KIND=adopted
	else
		KIND=dev
	fi
	return 0
}

is_dirty() {
	_st=$(g status --porcelain --untracked-files=no 2>/dev/null) || return 0
	[ -n "$_st" ]
}

# A lock left by a git that died blocks every later checkout. Remove it only when it is more
# than 10 minutes old and no git process is alive.
clear_stale_index_lock() {
	_lock=$(g rev-parse --git-path index.lock 2>/dev/null) || return 0
	case $_lock in /*) ;; *) _lock="$CLONE/$_lock" ;; esac
	[ -e "$_lock" ] || return 0
	[ -n "$(find "$_lock" -prune -mmin +10 2>/dev/null)" ] || return 0
	proc_has git
	case $? in
	1)
		rm -f "$_lock"
		log "removed an index.lock older than 10 minutes (no git was running)"
		;;
	*) log "an old index.lock exists but git may be running; left alone" ;;
	esac
	return 0
}

# ---- choosing a target ----------------------------------------------------------------

# pick_target prints the highest valid vX.Y.Z tag that is newer than what is checked out.
# Valid: not bad, an ancestor of origin/main, and its __version__ is the tag without the v.
pick_target() {
	_cur=$(version_at HEAD)
	_cur_num=$(printf '%s' "$_cur" | sed -n 's/^\([0-9][0-9]*\.[0-9][0-9]*\.[0-9][0-9]*\).*/\1/p')
	[ -n "$_cur_num" ] || _cur_num=0.0.0
	_cur_plain=0
	[ "$_cur" = "$_cur_num" ] && _cur_plain=1
	for _tag in $(g tag --list 'v*' --sort=-v:refname 2>/dev/null); do
		is_plain_tag "$_tag" || continue
		_ver=${_tag#v}
		if [ "$_ver" = "$_cur_num" ]; then
			[ "$_cur_plain" = 0 ] || continue
		else
			ver_gt "$_ver" "$_cur_num" || continue
		fi
		is_bad "$_tag" && continue
		g merge-base --is-ancestor "refs/tags/$_tag" origin/main >/dev/null 2>&1 || continue
		[ "$(version_at "refs/tags/$_tag")" = "$_ver" ] || continue
		printf '%s\n' "$_tag"
		return 0
	done
	return 1
}

fetch_if_due() {
	FETCHED=0
	if [ -n "$S_last_fetch" ]; then
		_jitter=$(printf '%s' "$C_device" | cksum | cut -d ' ' -f 1)
		_jitter=$((_jitter % (FETCH_INTERVAL / 24 + 1)))
		[ $((NOW - S_last_fetch)) -ge $((FETCH_INTERVAL + _jitter)) ] || return 0
	fi
	_ff="$STATE_DIR/.fetch.$$"
	if run_limited "$FETCH_TIMEOUT" git -C "$CLONE" fetch --tags origin >"$_ff" 2>&1; then
		S_last_fetch=$NOW
		FETCHED=1
	else
		_out=$(head -n 3 "$_ff" 2>/dev/null)
		case $_out in
		*"would clobber"*)
			# A published tag was moved. Git refuses it every time, so asking again at every
			# tick would only repeat the error: wait for the next fetch time.
			S_last_fetch=$NOW
			log "a tag that was already here moved upstream and was not taken ($(printf '%s' "$_out" | head -n 1))"
			;;
		*) log "could not fetch the remote ($(printf '%s' "$_out" | head -n 1)); using what is already here" ;;
		esac
	fi
	rm -f "$_ff"
	return 0
}

# ---- verification ---------------------------------------------------------------------

VERIFY_WHY=
VERIFY_PARTIAL=0
VERIFY_LOG=$STATE_DIR/last-verify.log
PY=
OVER=0

py_ok() {
	run_limited 30 "$1" -I -c 'import sys, tomllib; sys.exit(0 if sys.version_info >= (3, 11) else 1)' >/dev/null 2>&1
}

find_python() {
	for _c in "$C_python" $PY_FALLBACKS; do
		[ -n "$_c" ] || continue
		if py_ok "$_c"; then
			PY=$_c
			return 0
		fi
	done
	return 1
}

# wz_step SECONDS ARGS...: run `python -m warpzone ARGS` from the clone.
wz_step() {
	_to=$1
	shift
	if [ -n "$C_config" ]; then
		run_limited "$_to" "$PY" -m warpzone "$@" --config "$C_config"
	else
		run_limited "$_to" "$PY" -m warpzone "$@"
	fi
}

# verify_cut STATUS: the step ended because the tick's budget did. That says nothing about
# the release, so the verification gives up with 4 (it cannot judge).
verify_cut() {
	[ "$1" -eq 124 ] && [ "$RL_BUDGET_CUT" = 1 ] || return 1
	VERIFY_WHY="ran out of the time budget of this tick"
	return 0
}

# verify_version TAG: the three steps. TAG is empty when the version has no tag (then the
# version at HEAD is expected). 0 passed, 3 deferred, 1 failed (VERIFY_WHY says why), 4 could
# not be judged (out of time for the tick, or python imports another warpzone than the
# checkout's: both the environment's doing, not the release's).
verify_version() {
	VERIFY_WHY=
	VERIFY_PARTIAL=0
	if [ -n "$1" ]; then
		_expect=${1#v}
	else
		_expect=$(version_at HEAD)
	fi
	_t_import=${STEP_TIMEOUT:-120}
	_t_provision=${STEP_TIMEOUT:-900}
	_t_health=${STEP_TIMEOUT:-300}
	: >"$VERIFY_LOG" 2>/dev/null

	run_limited "$_t_import" "$PY" -I -c "$IMPORT_CODE" "$CLONE" >>"$VERIFY_LOG" 2>&1
	_r=$?
	if [ "$_r" -ne 0 ]; then
		verify_cut "$_r" && return 4
		if [ "$_r" -eq 7 ]; then
			VERIFY_WHY="python imports warpzone from outside $CLONE, not from this checkout"
			return 4
		fi
		VERIFY_WHY="import check failed (exit $_r)"
		return 1
	fi

	_vf="$STATE_DIR/.version.$$"
	run_limited "$_t_import" "$PY" -I -m warpzone --version >"$_vf" 2>>"$VERIFY_LOG"
	_r=$?
	_got=$(cat "$_vf" 2>/dev/null)
	rm -f "$_vf"
	if [ "$_r" -ne 0 ] || [ "$_got" != "warpzone $_expect" ]; then
		verify_cut "$_r" && return 4
		VERIFY_WHY="version mismatch: wanted warpzone $_expect, got '$_got' (exit $_r)"
		return 1
	fi

	wz_step "$_t_provision" provision device "$C_device" --unattended >>"$VERIFY_LOG" 2>&1
	_r=$?
	case $_r in
	0) ;;
	3)
		if [ "$OVER" = 1 ]; then
			VERIFY_PARTIAL=1
		else
			VERIFY_WHY="deferred by provision"
			return 3
		fi
		;;
	*)
		verify_cut "$_r" && return 4
		VERIFY_WHY="provision device --unattended failed (exit $_r)"
		return 1
		;;
	esac

	wz_step "$_t_health" health "$C_device" >>"$VERIFY_LOG" 2>&1
	_r=$?
	case $_r in
	0) ;;
	3)
		if [ "$OVER" = 1 ]; then
			VERIFY_PARTIAL=1
		else
			VERIFY_WHY="deferred by health"
			return 3
		fi
		;;
	*)
		verify_cut "$_r" && return 4
		VERIFY_WHY="health failed (exit $_r)"
		return 1
		;;
	esac
	return 0
}

# ---- state transitions ----------------------------------------------------------------

finish() {
	state_save || log "the state could not be saved"
	exit "$1"
}

mark_deferred() {
	S_pending=1
	[ -n "$S_deferred_since" ] || S_deferred_since=$NOW
	S_last_result=deferred
}

# settle_pending: after a run that completed. A step that was cut short by the deferral
# limit leaves the work pending; otherwise the deferral is over.
settle_pending() {
	if [ "$VERIFY_PARTIAL" = 1 ]; then
		S_pending=1
	else
		S_pending=0
		S_deferred_since=
	fi
}

clear_trying() {
	S_trying_tag=
	S_trying_sha=
	S_attempts=0
}

env_backoff() {
	S_env_failures=$((S_env_failures + 1))
	case $S_env_failures in
	1) _days=1 ;;
	2) _days=2 ;;
	3) _days=4 ;;
	*) _days=7 ;;
	esac
	S_retry_after=$((NOW + _days * DAY))
	S_deferred_since= # it failed for another reason: whoever waited, waited for nothing
	S_last_result=env
}

stuck() {
	S_last_result=stuck
	log "cannot go back to the previous version ($1); nothing else was touched"
	finish 1
}

# sync_good_reference: the version known to work. A first run takes what is checked out; a
# checkout moved by hand (when no update is in progress) becomes the new reference.
sync_good_reference() {
	[ -z "$S_trying_tag" ] || return 0
	_head=$(head_sha)
	[ -n "$_head" ] || return 0
	[ "$_head" != "$S_good_sha" ] || return 0
	if [ -n "$S_good_sha" ]; then
		log "the checkout was moved outside the updater; taking it as the reference"
		S_probation=0
		S_from_tag=
		S_from_sha=
		S_from_branch=
	fi
	S_good_sha=$_head
	S_good_branch=$BRANCH
	S_good_tag=$(g describe --exact-match --tags --match 'v[0-9]*' HEAD 2>/dev/null || true)
	is_plain_tag "$S_good_tag" || S_good_tag=
}

# reconcile_if_needed: run provision once per boot, or when a run was deferred. Sets
# RECONCILE to none, ok, deferred or failed.
RECONCILE=none
reconcile_if_needed() {
	RECONCILE=none
	_boot=$(boot_id)
	if [ "$S_pending" != 1 ] && [ "$_boot" = "$S_last_boot_id" ]; then
		return 0
	fi
	wz_step "${STEP_TIMEOUT:-900}" provision device "$C_device" --unattended >"$VERIFY_LOG" 2>&1
	_r=$?
	case $_r in
	0)
		RECONCILE=ok
		S_last_boot_id=$_boot
		S_pending=0
		S_deferred_since=
		;;
	3)
		if [ "$OVER" = 1 ]; then
			RECONCILE=ok
			S_last_boot_id=$_boot
		else
			RECONCILE=deferred
			mark_deferred
		fi
		;;
	*)
		RECONCILE=failed
		S_last_boot_id=$_boot
		S_pending=0
		S_deferred_since=
		log "provision device --unattended failed (exit $_r); see $VERIFY_LOG"
		;;
	esac
	return 0
}

# ---- the tick -------------------------------------------------------------------------

promote() {
	S_from_tag=$S_good_tag
	S_from_sha=$S_good_sha
	S_from_branch=$S_good_branch
	S_good_tag=$S_trying_tag
	S_good_sha=$S_trying_sha
	S_good_branch=
	clear_trying
	S_probation=1
	S_env_failures=0
	S_retry_after=
	S_last_result=ok
	S_last_ok=$NOW
	S_last_boot_id=$(boot_id)
	settle_pending
	log "now on $S_good_tag"
	finish 0
}

# differential SUSPECT_TAG SUSPECT_SHA REF_TAG REF_BRANCH REF_SHA: a check failed. Go back to
# the reference and check it: if it passes the suspect is bad, if it fails too the
# environment is at fault. Sets DV to bad, env, deferred or stuck. HEAD ends on the reference.
DV=
differential() {
	log "$1 failed its check ($VERIFY_WHY); checking the previous version to tell a bad release from a broken environment"
	if ! checkout_ref "$4" "$5"; then
		DV=stuck
		return 0
	fi
	verify_version "$3"
	case $? in
	0) DV=bad ;;
	3) DV=deferred ;;
	*) DV=env ;;
	esac
	return 0
}

# retry_suspect TAG SHA: the previous version passes and TAG did not. A failure that does not
# repeat (a service that was slow to come up) must not burn a release for good, so check
# it out and verify it once more. Returns the verification's status, or 5 when the checkout
# failed.
retry_suspect() {
	log "$1 failed once and the previous version passes; checking $1 once more before blaming it"
	g checkout --quiet --no-overwrite-ignore --detach "$2" >/dev/null 2>&1 || return 5
	verify_version "$1"
}

# back_to_good: HEAD on the version known to work; stuck when that cannot be done.
back_to_good() {
	checkout_ref "$S_good_branch" "$S_good_sha" || stuck "checkout of $S_good_sha failed"
}

# env_abort MESSAGE: the verification could not judge the release. Back to the version known
# to work, without blaming the release, and wait.
env_abort() {
	log "$1"
	back_to_good
	clear_trying
	env_backoff
	finish 1
}

# attempt: S_trying_* and S_attempts are set. Check it out, verify it, decide.
attempt() {
	if is_dirty; then
		log "the tree has local changes; not switching to $S_trying_tag"
		clear_trying
		S_last_result=dirty
		finish 0
	fi
	_free=$(free_kb)
	if [ -n "$_free" ] && [ "$_free" -lt "$MIN_FREE_KB" ]; then
		log "only $_free KiB are free next to $CLONE; not switching to $S_trying_tag"
		clear_trying
		env_backoff
		finish 1
	fi
	if ! state_save; then
		log "the state cannot be saved; not switching"
		exit 1
	fi
	_tag=$S_trying_tag
	_sha=$S_trying_sha
	if [ "$(head_sha)" != "$_sha" ]; then
		log "switching to $_tag"
		if ! g checkout --quiet --no-overwrite-ignore --detach "$_sha" >/dev/null 2>&1; then
			log "cannot check out $_tag; not a verdict on the release"
			heal_half_checkout "$S_good_sha" "$_sha" "$S_good_sha" || log "the tree is left as it is"
			clear_trying
			env_backoff
			finish 1
		fi
	fi
	verify_version "$_tag"
	case $? in
	0) promote ;;
	3)
		log "the check of $_tag was deferred ($VERIFY_WHY); it continues at the next tick"
		[ "$S_attempts" -gt 0 ] && S_attempts=$((S_attempts - 1))
		mark_deferred
		finish 0
		;;
	4) env_abort "$_tag could not be judged: $VERIFY_WHY" ;;
	esac
	_why=$VERIFY_WHY
	differential "$_tag" "$_sha" "$S_good_tag" "$S_good_branch" "$S_good_sha"
	case $DV in
	stuck) stuck "checkout of $S_good_sha failed" ;;
	bad)
		retry_suspect "$_tag" "$_sha"
		case $? in
		0)
			log "$_tag passed the second time"
			promote
			;;
		3)
			log "the second check of $_tag was deferred ($VERIFY_WHY); it continues at the next tick"
			[ "$S_attempts" -gt 0 ] && S_attempts=$((S_attempts - 1))
			mark_deferred
			finish 0
			;;
		4) env_abort "$_tag could not be judged the second time: $VERIFY_WHY" ;;
		5) env_abort "cannot check out $_tag again" ;;
		esac
		back_to_good
		write_bad "$_tag" "$_sha" "$_why (twice)"
		log "$_tag is bad ($_why, twice); back on the previous version"
		clear_trying
		S_env_failures=0
		S_retry_after=
		S_last_result=rolled-back
		settle_pending
		finish 1
		;;
	deferred)
		log "the previous version could not be checked now either; $_tag is not marked bad"
		clear_trying
		mark_deferred
		finish 0
		;;
	*)
		log "the previous version fails too, so the environment is at fault; $_tag is not marked bad"
		clear_trying
		env_backoff
		finish 1
		;;
	esac
}

# resume: a tick that finds a version being tried knows the last run did not finish.
resume() {
	if is_bad "$S_trying_tag"; then
		log "$S_trying_tag was marked bad in the meantime; going back to the previous version"
		back_to_good
		clear_trying
		return 0
	fi
	if [ "$S_attempts" -ge "$MAX_ATTEMPTS" ]; then
		log "$S_trying_tag has had $S_attempts attempts without a verdict; going back without marking it bad"
		back_to_good
		clear_trying
		S_retry_after=$((NOW + DAY))
		S_last_result=rolled-back
		finish 1
	fi
	S_attempts=$((S_attempts + 1))
	log "resuming the check of $S_trying_tag (attempt $S_attempts)"
	attempt
}

# probation_pass: the new version is accepted for good.
probation_pass() {
	S_probation=0
	S_from_tag=
	S_from_sha=
	S_from_branch=
	S_last_boot_id=$_boot
	S_last_ok=$NOW
	S_last_result=ok
	settle_pending
}

# probation_check: after a switch, the new version is checked again at the next boot.
probation_check() {
	[ "$S_probation" = 1 ] || return 0
	_boot=$(boot_id)
	[ -n "$_boot" ] && [ "$_boot" != "$S_last_boot_id" ] || return 0
	[ -n "$S_from_sha" ] || return 0
	log "probation: checking $S_good_tag after a restart"
	verify_version "$S_good_tag"
	case $? in
	0)
		probation_pass
		return 0
		;;
	3)
		mark_deferred
		finish 0
		;;
	4)
		log "$S_good_tag could not be judged after the restart: $VERIFY_WHY"
		S_last_boot_id=$_boot
		env_backoff
		finish 1
		;;
	esac
	_why=$VERIFY_WHY
	_tag=$S_good_tag
	_sha=$S_good_sha
	differential "$_tag" "$_sha" "$S_from_tag" "$S_from_branch" "$S_from_sha"
	case $DV in
	stuck) stuck "checkout of $S_from_sha failed" ;;
	bad)
		retry_suspect "$_tag" "$_sha"
		case $? in
		0)
			log "$_tag passed the second time"
			probation_pass
			return 0
			;;
		3)
			mark_deferred
			finish 0
			;;
		4)
			log "$_tag could not be judged the second time: $VERIFY_WHY"
			S_last_boot_id=$_boot
			env_backoff
			finish 1
			;;
		5) stuck "checkout of $_sha failed" ;;
		esac
		checkout_ref "$S_from_branch" "$S_from_sha" || stuck "checkout of $S_from_sha failed"
		write_bad "$_tag" "$_sha" "$_why (twice)"
		log "$_tag is bad after the restart ($_why, twice); back on the previous version"
		S_good_tag=$S_from_tag
		S_good_sha=$S_from_sha
		S_good_branch=$S_from_branch
		S_from_tag=
		S_from_sha=
		S_from_branch=
		S_probation=0
		S_last_boot_id=$_boot
		S_last_result=rolled-back
		finish 1
		;;
	deferred)
		S_good_tag=$S_from_tag
		S_good_sha=$S_from_sha
		S_good_branch=$S_from_branch
		S_from_tag=
		S_from_sha=
		S_from_branch=
		S_probation=0
		mark_deferred
		finish 0
		;;
	*)
		log "the previous version fails too, so the environment is at fault; staying on $_tag"
		if ! checkout_ref "" "$_sha"; then
			stuck "checkout of $_sha failed"
		fi
		S_last_boot_id=$_boot
		env_backoff
		finish 1
		;;
	esac
}

cmd_tick() {
	mkdir -p "$STATE_DIR" || {
		echo "updater: cannot create $STATE_DIR" >&2
		exit 1
	}
	if ! acquire_lock; then
		log "another tick holds the lock; nothing to do"
		exit 0
	fi
	trap release_lock EXIT
	trap 'rl_abort; exit 143' HUP INT TERM
	DEADLINE=$(($(real_now) + TICK_BUDGET))
	if ! conf_load; then
		log "no settings at $CONF; nothing to do"
		exit 0
	fi
	if [ "$C_enabled" = 0 ]; then
		log "self-update is turned off for this device"
		exit 0
	fi
	if [ -z "$C_device" ]; then
		log "the settings name no device; nothing to do"
		exit 0
	fi
	if [ -z "$C_clone" ]; then
		C_clone=$(cd "$(dirname "$0")/../.." 2>/dev/null && pwd)
	fi
	CLONE=$C_clone
	if ! command -v git >/dev/null 2>&1; then
		log "git is not installed"
		exit 1
	fi
	cd "$CLONE" 2>/dev/null || {
		log "cannot enter $CLONE"
		exit 1
	}
	NOW=$(now)
	state_load
	state_drop_future

	clear_stale_index_lock

	# The menu: wait for it to close, at most 72 hours from the first deferral. A menu that
	# is seen closed ends the deferral: the 72 hours are of an unbroken wait.
	menu_open
	_menu=$?
	if [ "$_menu" = 1 ]; then
		S_deferred_since=
	fi
	OVER=0
	if [ -n "$S_deferred_since" ] && [ $((NOW - S_deferred_since)) -ge "$DEFER_MAX" ]; then
		OVER=1
	fi
	case $_menu in
	0)
		if [ "$OVER" = 0 ]; then
			mark_deferred
			log "the menu is open; deferred since $S_deferred_since"
			finish 0
		fi
		log "the menu has been open for more than 72 hours; going on anyway"
		;;
	2) log "cannot tell whether the menu is open ($PROC is not readable); going on" ;;
	esac

	if ! find_python; then
		S_last_result=no-python
		log "no usable Python 3.11 or newer was found"
		finish 1
	fi

	fetch_if_due
	detect_kind
	if [ "$KIND" = dirty ]; then
		# A tree that an update of ours left half done is not somebody's work.
		_healed=0
		if [ -n "$S_trying_sha" ]; then
			heal_half_checkout "$S_good_sha" "$S_trying_sha" "$S_trying_sha" && _healed=1
		elif [ "$S_probation" = 1 ] && [ -n "$S_from_sha" ]; then
			heal_half_checkout "$S_from_sha" "$S_good_sha" "$S_good_sha" && _healed=1
		fi
		if [ "$_healed" = 1 ]; then
			detect_kind
		fi
	fi
	case $KIND in
	nogit)
		log "$CLONE is not a git checkout"
		exit 1
		;;
	dirty | dev)
		log "the checkout is $KIND: not moved (no reset, stash or clean)"
		reconcile_if_needed
		S_last_result=$KIND
		finish 0
		;;
	esac

	sync_good_reference

	if [ -n "$S_trying_tag" ]; then
		resume
	fi
	probation_check

	if [ -n "$S_retry_after" ] && [ "$S_retry_after" -gt "$NOW" ]; then
		log "waiting until $S_retry_after before trying a new release"
		finish 0
	fi

	_target=$(pick_target) || _target=
	if [ -n "$_target" ]; then
		S_trying_tag=$_target
		S_trying_sha=$(g rev-parse "refs/tags/$_target^{commit}")
		S_attempts=$((S_attempts + 1))
		log "new release $_target (running $(version_at HEAD))"
		attempt
	fi

	reconcile_if_needed
	case $RECONCILE in
	deferred) ;;
	*)
		S_last_result=ok
		if [ "$FETCHED" = 1 ] || [ "$RECONCILE" = ok ]; then
			S_last_ok=$NOW
		fi
		;;
	esac
	finish 0
}

cmd_menu() {
	conf_load
	menu_open
	_r=$?
	case $_r in
	0) echo "open" ;;
	1) echo "closed" ;;
	*) echo "unknown" ;;
	esac
	exit "$_r"
}

cmd_selftest() {
	_bad=0
	for _c in git sed grep tr cut find cksum sync sleep date kill mkdir mv rm cat head tail wc dirname; do
		if ! command -v "$_c" >/dev/null 2>&1; then
			echo "selftest: missing command: $_c"
			_bad=1
		fi
	done
	if ! { ver_gt 0.10.0 0.9.0 && ! ver_gt 0.9.0 0.10.0 && ! ver_gt 1.2.3 1.2.3 && ver_gt 1.0.0 0.99.99; }; then
		echo "selftest: version comparison is wrong"
		_bad=1
	fi
	for _t in v0.1.0 v10.20.30; do
		is_plain_tag "$_t" || {
			echo "selftest: $_t should be a release tag"
			_bad=1
		}
	done
	for _t in v0.1 v0.1.0rc1 v0.1.0-rc1 0.1.0 v1.2.3.4 v..1 v1.2. vx.y.z; do
		if is_plain_tag "$_t"; then
			echo "selftest: $_t should not be a release tag"
			_bad=1
		fi
	done
	_dir="${TMPDIR:-/tmp}/wz-selftest.$$"
	if mkdir "$_dir" 2>/dev/null; then
		STATE_DIR=$_dir
		STATE_FILE=$_dir/state
		BAD_DIR=$_dir/bad
		LOG=
		state_load
		S_good_tag=v0.1.0
		S_attempts=2
		S_last_result=ok
		state_save
		state_load
		if [ "$S_good_tag" != v0.1.0 ] || [ "$S_attempts" != 2 ] || [ "$S_last_result" != ok ]; then
			echo "selftest: the state does not survive a save and a load"
			_bad=1
		fi
		run_limited 5 true
		[ $? -eq 0 ] || {
			echo "selftest: cannot run a command"
			_bad=1
		}
		run_limited 5 sh -c 'exit 3'
		[ $? -eq 3 ] || {
			echo "selftest: lost an exit status"
			_bad=1
		}
		run_limited 1 sleep 30
		[ $? -eq 124 ] || {
			echo "selftest: a command that hangs is not cut off"
			_bad=1
		}
		rm -rf "$_dir"
	else
		echo "selftest: cannot create a temporary folder"
		_bad=1
	fi
	if [ "$_bad" -ne 0 ]; then
		exit 1
	fi
	echo "selftest: ok abi=$UPDATER_ABI"
	exit 0
}

case ${1:-} in
tick) cmd_tick ;;
menu) cmd_menu ;;
selftest) cmd_selftest ;;
*)
	echo "usage: updater.sh tick|menu|selftest" >&2
	exit 2
	;;
esac
