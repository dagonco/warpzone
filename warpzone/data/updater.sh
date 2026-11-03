#!/bin/sh
# warpzone updater: keeps a device's git checkout on a release that works.
#
# POSIX sh, git and the usual tools only. It lives outside the warpzone package on purpose
# (a broken release must not be able to stop the tool that puts the good one back) and its
# rules are written in docs/design.md, "Autonomous updates (contract)".
#
#   updater.sh tick      one run of the timer: update, verify, roll back, reconcile
#   updater.sh install DEVICE [--config FILE] [--venv DIR] [--channel stable|rc] [-- ARGS...]
#                        the human install, from a git clone: check out the best release,
#                        make the venv, run `provision device`
#   updater.sh menu      exit 0 when the frontend is running, 1 when not, 2 when unknown
#   updater.sh selftest  check the tools this script needs and its own helpers
#   updater.sh order     read tags on the standard input, print those that are releases or
#                        candidates, best first (for the tests and for looking at it by hand)
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
#   WZ_RETIRED_MAX       retirement tags whose signature is checked per tick (default 50)
#   WZ_ALLOWED_SIGNERS   the allowed-signers file (ssh-keygen -Y format) that tags must be
#                        signed against, for tests (default: the one in the version known to
#                        work, warpzone/data/allowed_signers; empty or without keys, no
#                        signature is required)

# Every S_* variable is the state; state_save writes them to the file through eval, which
# the linter cannot follow, so it takes them for unused.
# shellcheck disable=SC2034
UPDATER_ABI=1

set -u

DAY=86400
DEFER_MAX=$((72 * 3600))
MAX_ATTEMPTS=2
IMPORT_CODE='import os, sys, warpzone.cli, warpzone.provision, warpzone.health; root = os.path.realpath(sys.argv[1]); here = os.path.realpath(warpzone.__file__); sys.exit(0 if here.startswith(root + os.sep) else 7)'
STATE_KEYS="schema good_tag good_sha good_branch trying_tag trying_sha from_tag from_sha from_branch attempts probation pending deferred_since last_fetch last_boot_id last_result last_ok retry_after env_failures suspect_tag suspect_sha pin_retired signing signers_version"

STATE_DIR=${WZ_STATE_DIR:-${XDG_STATE_HOME:-${HOME:-/nonexistent}/.local/state}/warpzone/updater}
CONF=${WZ_CONF:-$STATE_DIR/conf}
LOG=${WZ_LOG:-$(dirname "$STATE_DIR")/self-update.log}
PROC=${WZ_PROC:-/proc}
BOOT_ID_FILE=${WZ_BOOT_ID_FILE:-/proc/sys/kernel/random/boot_id}
FETCH_INTERVAL=${WZ_FETCH_INTERVAL:-$DAY}
PY_FALLBACKS=${WZ_PYTHON_FALLBACKS-/usr/bin/python3 python3}
SIGNERS_FILE=
SIGNERS_SRC=
SIGNERS_TMP=$STATE_DIR/.allowed_signers
SIGNERS_LAST=$STATE_DIR/allowed_signers.last
RETIRED_MAX=${WZ_RETIRED_MAX:-50}
RETIRED_SCAN=500
STEP_TIMEOUT=${WZ_STEP_TIMEOUT:-}
FETCH_TIMEOUT=${WZ_FETCH_TIMEOUT:-120}
TICK_BUDGET=${WZ_TICK_BUDGET:-1500}
MIN_FREE_KB=${WZ_MIN_FREE_KB:-51200}
DEADLINE=
SETSID=$(command -v setsid 2>/dev/null || true)

export GIT_TERMINAL_PROMPT=0

# ---- small helpers --------------------------------------------------------------------

# real_now: seconds on a clock that only goes forward, whatever WZ_NOW says: the uptime
# (first field of /proc/uptime) where there is one, because a device without a battery-backed
# clock gets its date set by NTP in the middle of a tick, and the date otherwise. The time
# budget is measured with it.
real_now() {
	if [ -r "$PROC/uptime" ]; then
		IFS=' ' read -r _up _rest <"$PROC/uptime" 2>/dev/null
		_up=${_up%%.*}
		if is_number "$_up"; then
			printf '%s\n' "$_up"
			return 0
		fi
	fi
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
		_size=$(wc -c <"$LOG" 2>/dev/null || echo 0)
		_size=${_size##* } # some wc print the number after spaces
		if is_number "$_size" && [ "$_size" -gt 65536 ]; then
			tail -n 200 "$LOG" >"$LOG.tmp" 2>/dev/null && mv -f "$LOG.tmp" "$LOG"
		fi
	fi
	printf '%s updater: %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$LOG_LINE" >>"$LOG" 2>/dev/null
	return 0
}

# log_daily MESSAGE: a message about the state of the world (not about something this tick
# did) that the next tick would only repeat: said when the remote was fetched, which is
# once a day.
log_daily() {
	[ "${FETCHED:-0}" = 1 ] || return 0
	log "$*"
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

# pad9 N: P is N with zeros in front up to nine digits; fails for a longer number.
pad9() {
	[ "${#1}" -le 9 ] || return 1
	P=000000000$1
	P=${P#"${P%?????????}"}
}

# rel_key TAG: RK is a key that sorts, as plain text, like selfupdate.tag_key orders the tag
# vX.Y.Z or vX.Y.ZrcN (v0.3.0rc1 < v0.3.0rc2 < v0.3.0, the numbers as numbers); fails for
# any other spelling. RK_NUM is the part of the key that is the three numbers.
RK=
RK_NUM=
rel_key() {
	RK=
	case $1 in v*) ;; *) return 1 ;; esac
	_rk_v=${1#v}
	_rk_stage=2
	_rk_rc=0
	case $_rk_v in
	*rc*)
		_rk_rc=${_rk_v#*rc}
		_rk_v=${_rk_v%%rc*}
		is_number "$_rk_rc" || return 1
		_rk_stage=1
		;;
	esac
	is_plain_tag "v$_rk_v" || return 1
	_rk_ifs=$IFS
	IFS=.
	set -f
	# shellcheck disable=SC2086
	set -- $_rk_v
	set +f
	IFS=$_rk_ifs
	pad9 "$1" || return 1
	RK_NUM=$P
	pad9 "$2" || return 1
	RK_NUM=$RK_NUM.$P
	pad9 "$3" || return 1
	RK_NUM=$RK_NUM.$P
	pad9 "$_rk_rc" || return 1
	RK=$RK_NUM.$_rk_stage.$P
	return 0
}

is_release_tag() {
	rel_key "$1"
}

# ver_key VERSION: VK is the key of a package version, as version_key gives it: an rcN or a
# plain release as for a tag, any other suffix before every rcN, and one that cannot be read
# before all.
VK=
ver_key() {
	if rel_key "v$1"; then
		VK=$RK
		return 0
	fi
	_vk_num=$(printf '%s' "$1" | sed -n 's/^\([0-9][0-9]*\.[0-9][0-9]*\.[0-9][0-9]*\).*/\1/p')
	if [ -n "$_vk_num" ] && rel_key "v$_vk_num"; then
		VK=$RK_NUM.0.000000000
	else
		VK=000000000.000000000.000000000.0.000000000
	fi
	return 0
}

# key_gt A B: succeeds when the key A sorts after the key B.
key_gt() {
	[ "$1" != "$2" ] && [ "$(printf '%s\n%s\n' "$1" "$2" | LC_ALL=C sort | tail -n 1)" = "$1" ]
}

# order_tags: the tags on the standard input that are releases or candidates, best first.
order_tags() {
	while IFS= read -r _ot_tag; do
		rel_key "$_ot_tag" || continue
		printf '%s %s\n' "$RK" "$_ot_tag"
	done | LC_ALL=C sort -r | while read -r _ot_key _ot_tag; do
		printf '%s\n' "$_ot_tag"
	done
}

# Numbers that come from the environment: one that is not a number would stop the script
# on its first sum.
case $FETCH_INTERVAL in '' | *[!0-9]*) FETCH_INTERVAL=$DAY ;; esac
case $STEP_TIMEOUT in '' | 0 | *[!0-9]*) STEP_TIMEOUT= ;; esac
case $FETCH_TIMEOUT in '' | *[!0-9]*) FETCH_TIMEOUT=120 ;; esac
case $TICK_BUDGET in '' | *[!0-9]*) TICK_BUDGET=1500 ;; esac
case $MIN_FREE_KB in '' | *[!0-9]*) MIN_FREE_KB=51200 ;; esac
case $RETIRED_MAX in '' | 0 | *[!0-9]*) RETIRED_MAX=50 ;; esac

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
# shellcheck disable=SC2329 # it is run by the trap of cmd_tick, in a string
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
C_channel=stable
C_pin=

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
		channel) C_channel=$_v ;;
		pin) C_pin=$_v ;;
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
	S_signing=0
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
	for _k in probation pending signing; do
		eval "_v=\$S_$_k"
		[ "$_v" = 1 ] || state_assign "$_k" 0
	done
	for _k in deferred_since last_fetch retry_after last_ok; do
		eval "_v=\$S_$_k"
		is_number "$_v" || state_assign "$_k" ""
	done
	case $S_good_sha$S_trying_sha$S_from_sha$S_suspect_sha in
	*[!0-9a-f]*)
		S_good_sha=''
		S_trying_sha=''
		S_from_sha=''
		S_suspect_sha=''
		;;
	esac
	# These end up in file names and in git commands: only what a tag and a branch can be.
	for _k in good_tag trying_tag from_tag suspect_tag; do
		eval "_v=\$S_$_k"
		[ -z "$_v" ] || is_release_tag "$_v" || state_assign "$_k" ""
	done
	[ -z "$S_pin_retired" ] || is_release_tag "$S_pin_retired" || S_pin_retired=
	case $S_signers_version in *[!A-Za-z0-9._+-]*) S_signers_version= ;; esac
	# A tag and its commit go together: one without the other is nothing.
	if [ -z "$S_trying_tag" ] || [ -z "$S_trying_sha" ]; then
		S_trying_tag=
		S_trying_sha=
		S_attempts=0
	fi
	if [ -z "$S_suspect_tag" ] || [ -z "$S_suspect_sha" ]; then
		S_suspect_tag=
		S_suspect_sha=
	fi
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
	[ "$(cat "$LOCK_DIR/pid" 2>/dev/null)" = "$$" ] || return 1
	boot_id >"$LOCK_DIR/boot"
	LOCK_HELD=1
	return 0
}

acquire_lock() {
	if mkdir "$LOCK_DIR" 2>/dev/null; then
		lock_claim
		return $?
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
	return $?
}

# shellcheck disable=SC2329 # it is run by the trap of cmd_tick
release_lock() {
	if [ "$LOCK_HELD" = 1 ]; then
		rm -f "$SIGNERS_TMP"
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
	_df=$(df -Pk "$CLONE" 2>/dev/null | tail -n 1)
	read -r _fs _blocks _used _avail _rest <<EOF
$_df
EOF
	if is_number "$_avail"; then
		printf '%s' "$_avail"
	fi
}

# heal_is_ours PATH A B: a file that differs from HEAD is the update's own only if it is gone,
# empty, or exactly the content it has in A or in B (what a checkout between them writes);
# anything else is somebody's edit, however it came to be in a file that the update changes.
# A path that git quotes (odd characters) is never taken for ours.
heal_is_ours() {
	case $1 in '"'*) return 1 ;; esac
	[ -e "$CLONE/$1" ] || return 0
	[ -s "$CLONE/$1" ] || return 0
	_h=$(g hash-object -- "$1" 2>/dev/null) || return 1
	[ -n "$_h" ] || return 1
	[ "$_h" = "$(g rev-parse -q --verify "$2:$1" 2>/dev/null)" ] && return 0
	[ "$_h" = "$(g rev-parse -q --verify "$3:$1" 2>/dev/null)" ] && return 0
	return 1
}

# heal_half_checkout A B TARGET: a checkout between the commits A and B was cut in the middle
# (a power cut, a full disk), so the tree is neither one nor the other. When every file that
# differs from HEAD is one that A and B differ in AND has the content of A or of B (or is
# gone or empty), the change is the updater's own and the checkout is done again, forced,
# onto TARGET; anything else is somebody's work and stays. Succeeds when the tree is
# consistent afterwards.
heal_half_checkout() {
	[ -n "$1" ] && [ -n "$2" ] || return 1
	_changed=$(g -c core.quotepath=off diff --name-only "$1" "$2" 2>/dev/null) || return 1
	_dirty=$(g -c core.quotepath=off diff --name-only HEAD 2>/dev/null) || return 1
	[ -n "$_dirty" ] || return 0
	_own=1
	_ifs=$IFS
	IFS='
'
	set -f
	for _f in $_dirty; do
		if ! printf '%s\n' "$_changed" | grep -Fxq -- "$_f" || ! heal_is_ours "$_f" "$1" "$2"; then
			_own=0
		fi
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

# ---- retired releases and signed tags -------------------------------------------------

# Retiring: a tag cannot be edited, so a release is withdrawn by another tag, an annotated
# `retired/vX.Y.Z` (or `retired/vX.Y.ZrcN`) whose message is the reason. When the allowed
# signers hold a key it only counts if it is signed by one, like any release (below); without
# keys it counts as it is. A lightweight tag, one whose name is not a release or a candidate,
# or one with a signature that does not verify, retires nothing and is logged. The reason is
# text for the log, never run.
RETIRED_TAGS=' '
RETIRED_INFO=
RETIRED_BAD=

# retired_ok TAG: the tag object retired/TAG sits on the commit of the release TAG (vX.Y.Z).
# What it points at is peeled first, so `git tag -s retired/v1.2.3 v1.2.3`, which nests the
# retirement in the release's own tag object, counts like one made on its commit. A signature
# covers the object and not the ref it is published under, so without this a signed
# retirement of one version could be published as the retirement of another.
retired_ok() {
	_ro_obj=$(g cat-file tag "refs/tags/retired/$1" 2>/dev/null </dev/null | sed -n -e '/^$/q' -e 's/^object //p' | head -n 1)
	[ -n "$_ro_obj" ] || return 1
	_ro_target=$(g rev-parse -q --verify "$_ro_obj^{commit}" 2>/dev/null </dev/null)
	_ro_commit=$(g rev-parse -q --verify "refs/tags/$1^{commit}" 2>/dev/null </dev/null)
	[ -n "$_ro_commit" ] && [ "$_ro_target" = "$_ro_commit" ]
}

# retired_load: the retirements the clone has (it takes them with the daily fetch), the newest
# versions first. A tag that is no retirement (not annotated, no version for a name, not on
# the commit of its release) is cheap to turn down and does not count towards the limit; at
# most RETIRED_MAX that are well formed get their signature checked, so that a pile of bad
# ones cannot use up the tick's budget, and RETIRED_SCAN tags are looked at in all. Needs
# signing_setup first.
retired_load() {
	RETIRED_TAGS=' '
	RETIRED_INFO=
	RETIRED_BAD=
	_rmore=0
	_rcount=0
	_rlines=0
	_rbadn=0
	_rf="$STATE_DIR/.retired.$$"
	g for-each-ref --sort=-v:refname --format='%(refname) %(objecttype) %(taggerdate:short) %(contents:subject)' refs/tags/retired/ 2>/dev/null | head -n "$RETIRED_SCAN" >"$_rf" 2>/dev/null
	while read -r _rref _rtype _rdate _rreason; do
		_rlines=$((_rlines + 1))
		_rtag=${_rref#refs/tags/retired/}
		_rok=0
		if rel_key "$_rtag" && [ "$_rtype" = tag ] && retired_ok "$_rtag"; then
			if [ "$_rcount" -ge "$RETIRED_MAX" ]; then
				_rmore=1
				break
			fi
			_rcount=$((_rcount + 1))
			tag_signed "retired/$_rtag" && _rok=1
		fi
		if [ "$_rok" = 1 ]; then
			RETIRED_TAGS="$RETIRED_TAGS$_rtag "
			RETIRED_INFO="$RETIRED_INFO$_rtag $_rdate $_rreason
"
		else
			_rbadn=$((_rbadn + 1))
			[ "$_rbadn" -gt 10 ] || RETIRED_BAD="$RETIRED_BAD $_rtag"
		fi
	done <"$_rf"
	rm -f "$_rf"
	if [ "$_rbadn" -gt 0 ]; then
		log_daily "retirement tags ignored ($_rbadn: not annotated, not a version, not on that release's commit, or not signed by an allowed key):$RETIRED_BAD"
	fi
	if [ "$_rmore" = 1 ]; then
		log_daily "more than $RETIRED_MAX retirement tags: only the newest $RETIRED_MAX were looked at"
	elif [ "$_rlines" -ge "$RETIRED_SCAN" ]; then
		log_daily "only the newest $RETIRED_SCAN retirement tags were scanned"
	fi
	return 0
}

is_retired() {
	case $RETIRED_TAGS in *" $1 "*) return 0 ;; esac
	return 1
}

# retired_why TAG: the date and reason of the retirement.
retired_why() {
	printf '%s' "$RETIRED_INFO" | while read -r _w_tag _w_rest; do
		if [ "$_w_tag" = "$1" ]; then
			printf '%s' "$_w_rest"
			break
		fi
	done
}

# ---- signed tags ----------------------------------------------------------------------
# Whoever controls the maintainer's account could otherwise put code on every device. With an
# allowed-signers file that holds at least one key, a tag counts (a release, a candidate, a
# pin, a retirement) only when it carries one SSH signature that `git verify-tag` accepts
# against that file. The file is warpzone/data/allowed_signers of the version known to work
# (S_good_sha), read from git into a temporary file: the keys travel with the version that is
# trusted, and a release cannot change the keys that check it. Without keys nothing is
# required and the log says so.

SIGN_ON=0
SIGN_WHY=

# signers_judge: set SIGN_ON from SIGNERS_FILE (and SIGN_WHY when no signature is required).
signers_judge() {
	SIGN_ON=0
	if [ -z "$SIGNERS_FILE" ] || [ ! -e "$SIGNERS_FILE" ]; then
		SIGN_WHY="there is no allowed signers file"
	elif [ ! -r "$SIGNERS_FILE" ]; then
		SIGN_ON=1 # a file that cannot be read is not a file without keys
	elif grep -Evq '^[[:space:]]*(#|$)' "$SIGNERS_FILE" 2>/dev/null; then
		SIGN_ON=1
	else
		SIGN_WHY="the allowed signers file holds no keys"
	fi
}

# keys_older A B: the version A is older than the version B.
keys_older() {
	ver_key "$1"
	_ko_a=$VK
	ver_key "$2"
	key_gt "$VK" "$_ko_a"
}

# signing_setup: once a version with keys has been trusted (`signing` in the state, the keys it
# had in allowed_signers.last and its version in `signers_version`), signatures stay required
# for good and the keys never go back: a pin or a retirement that takes the device down to an
# older release does not turn them off (that release has no file) or bring back the keys it
# listed and a newer release dropped (it has an older file). The last keys are then the ones
# used, and with none to use, every tag is refused. Keys are only replaced by those of a
# version that is as new or newer.
signing_setup() {
	rm -f "$SIGNERS_TMP"
	if [ -n "${WZ_ALLOWED_SIGNERS+x}" ]; then
		SIGNERS_FILE=$WZ_ALLOWED_SIGNERS
		SIGNERS_SRC=$WZ_ALLOWED_SIGNERS
		signers_judge
	else
		SIGNERS_FILE=$SIGNERS_TMP
		SIGNERS_SRC="warpzone/data/allowed_signers of the version known to work"
		if [ -n "$S_good_sha" ]; then
			g show "$S_good_sha:warpzone/data/allowed_signers" 2>/dev/null | head -n 1000 >"$SIGNERS_TMP" 2>/dev/null
		fi
		[ -s "$SIGNERS_TMP" ] || rm -f "$SIGNERS_TMP"
		signers_judge
		_good_ver=
		[ "$SIGN_ON" = 1 ] && _good_ver=$(version_at "$S_good_sha")
		if [ "$SIGN_ON" = 1 ] && { [ -z "$S_signers_version" ] || [ ! -s "$SIGNERS_LAST" ] || ! keys_older "$_good_ver" "$S_signers_version"; }; then
			S_signing=1
			S_signers_version=$_good_ver
			if cat "$SIGNERS_TMP" >"$SIGNERS_LAST.new" 2>/dev/null; then
				mv -f "$SIGNERS_LAST.new" "$SIGNERS_LAST"
			fi
		elif [ "$SIGN_ON" = 1 ] || [ "$S_signing" = 1 ]; then
			SIGN_ON=1
			SIGNERS_FILE=$SIGNERS_LAST
			SIGNERS_SRC="the last allowed signers this device trusted"
			log_daily "the version known to work has older or no allowed signers than ${S_signers_version:-before}: the last ones this device trusted are used"
		fi
	fi
	[ "$SIGN_ON" = 1 ] || log_daily "signatures are not required: $SIGN_WHY ($SIGNERS_SRC)"
	return 0
}

# tag_signed TAG: succeeds when no signature is required, or TAG is an annotated tag with
# exactly one signature block, an SSH one, that verifies, made for a tag of that very name.
# `verify-tag` chooses the verifier by
# the block it finds, not by gpg.format: a tag signed with OpenPGP or X.509 would be checked
# by gpg against the user's keyring and never reach the allowed signers. So any other block is
# refused, the other verifiers are switched off, and the user's git configuration is not read.
tag_signed() {
	[ "$SIGN_ON" = 1 ] || return 0
	command -v ssh-keygen >/dev/null 2>&1 || return 1
	_obj=$(g cat-file tag "refs/tags/$1" 2>/dev/null </dev/null) || return 1
	[ "$(printf '%s\n' "$_obj" | grep -c '^-----BEGIN ')" = 1 ] || return 1
	[ "$(printf '%s\n' "$_obj" | grep -c '^-----BEGIN SSH SIGNATURE-----$')" = 1 ] || return 1
	# The signature covers the object, which names the tag it was made for; the ref can say
	# anything. A signed v0.2.0 published as v0.3.0 is not v0.3.0.
	[ "$(printf '%s\n' "$_obj" | sed -n -e '/^$/q' -e 's/^tag //p' | head -n 1)" = "$1" ] || return 1
	run_limited 30 env GIT_CONFIG_GLOBAL=/dev/null GIT_CONFIG_NOSYSTEM=1 git -C "$CLONE" \
		-c gpg.format=ssh -c gpg.ssh.program=ssh-keygen -c gpg.program=false \
		-c gpg.openpgp.program=false -c gpg.x509.program=false \
		-c "gpg.ssh.allowedSignersFile=$SIGNERS_FILE" verify-tag "refs/tags/$1" >/dev/null 2>&1
}

# ---- choosing a target ----------------------------------------------------------------

# tag_valid TAG: TAG is an ancestor of origin/main, its __version__ is the tag without the v,
# and its signature verifies when one is required. Says why in the log when the signature
# is what fails.
tag_valid() {
	g merge-base --is-ancestor "refs/tags/$1" origin/main >/dev/null 2>&1 || return 1
	[ "$(version_at "refs/tags/$1")" = "${1#v}" ] || return 1
	if ! tag_signed "$1"; then
		log_daily "$1 is not a candidate: its signature does not verify against the allowed signers ($SIGNERS_SRC)"
		return 1
	fi
	return 0
}

# pick_pin: the pinned tag is the target whatever the channel, the running version and
# a retirement, once it is a real, valid tag of the project that is not already checked out.
pick_pin() {
	_psha=$(g rev-parse -q --verify "refs/tags/$C_pin^{commit}" 2>/dev/null) || _psha=
	if [ -z "$_psha" ]; then
		log_daily "the pinned tag $C_pin does not exist here; staying where it is"
		return 1
	fi
	[ "$_psha" != "$(head_sha)" ] || return 1
	if is_bad "$C_pin"; then
		log_daily "the pinned tag $C_pin was marked bad on this device; staying where it is"
		return 1
	fi
	tag_valid "$C_pin" || return 1
	PICK=$C_pin
	return 0
}

# target_ok TAG: TAG may be a target now. With a pin, only the pin (retired or not, of any
# channel); otherwise it is of the channel, not retired and not bad. Either way it is a valid
# tag (an ancestor of origin/main, its own version, signed when signatures are required).
# What was chosen at one tick is asked again at the next, because a retirement, the channel and
# the keys can change in between.
target_ok() {
	if [ -n "$C_pin" ]; then
		[ "$1" = "$C_pin" ] || return 1
	else
		case $C_channel in
		stable) is_plain_tag "$1" || return 1 ;;
		esac
		is_retired "$1" && return 1
	fi
	is_bad "$1" && return 1
	tag_valid "$1"
}

# pick_target sets PICK to the best valid tag that is newer than what is checked out, or to
# the pin when there is one. Valid: it is of the channel (stable: vX.Y.Z; rc: those and
# vX.Y.ZrcN), not bad, not retired, an ancestor of origin/main, its __version__ is the tag
# without the v, and its signature verifies when one is required. Best is the highest
# version (rc1 < rc2 < the release). Never down, except when the running version is retired:
# then the best valid one, wherever it is. Fails when there is none.
PICK=
pick_target() {
	PICK=
	_cur=$(version_at HEAD)
	ver_key "$_cur"
	_cur_key=$VK
	_down=0
	if [ -n "$_cur" ] && is_retired "v$_cur"; then
		_down=1
		log "the running version v$_cur is retired ($(retired_why "v$_cur")); looking for the best valid release, even a lower one"
	fi
	if [ -n "$C_pin" ]; then
		pick_pin
		return
	fi
	for _tag in $(g tag --list 'v*' 2>/dev/null | order_tags); do
		rel_key "$_tag"
		if [ "$_down" = 0 ] && ! key_gt "$RK" "$_cur_key"; then
			break # the rest is lower still
		fi
		target_ok "$_tag" || continue
		PICK=$_tag
		return 0
	done
	return 1
}

# move_allowed TAG: moving to TAG is no way down, or is allowed to be one: the running
# version is retired, or TAG is the pin (and with a pin nothing else is a target).
move_allowed() {
	if [ -n "$C_pin" ]; then
		[ "$1" = "$C_pin" ]
		return
	fi
	_ma_cur=$(version_at HEAD)
	is_retired "v$_ma_cur" && return 0
	rel_key "$1" || return 1
	_ma_key=$RK
	ver_key "$_ma_cur"
	key_gt "$_ma_key" "$VK"
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

PY_CUT=0
py_ok() {
	run_limited 30 "$1" -I -c 'import sys, tomllib; sys.exit(0 if sys.version_info >= (3, 11) else 1)' >/dev/null 2>&1
	_pr=$?
	if [ "$_pr" -eq 124 ] && [ "$RL_BUDGET_CUT" = 1 ]; then
		PY_CUT=1
	fi
	return "$_pr"
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

# ---- the live copy of this script ----------------------------------------------------
# While a release is under trial, its `provision device` installs its own updater as the live
# copy (the file this script runs from). If the release is then rejected, the previous
# version's `provision` would not put the old one back (a lower ABI is never installed over a
# higher one, and a rollback may not run `provision` at all), and a script with a fault that
# `selftest` does not see would stop the device from ever updating again. So the live copy
# is kept in the state folder before a trial and put back, with no look at its ABI, whenever
# the device goes back; it is dropped when no trial or probation is on.

SELF=$0
case $SELF in
/*) ;;
*) SELF=$(pwd)/$SELF ;;
esac
LIVE_GOOD=$STATE_DIR/updater.sh.good

# resolve_file PATH: the physical path of a file (its folder with every symlink resolved).
resolve_file() {
	_rf_dir=$(cd -P "$(dirname "$1")" 2>/dev/null && pwd -P) || return 1
	printf '%s/%s' "$_rf_dir" "${1##*/}"
}

# live_usable: this script is the live copy itself, WZ_LIVE or
# ~/.local/share/warpzone/updater/updater.sh, compared by physical path. Anything else (the
# clone's own script run by hand, a copy somewhere else, a path that reaches either through
# a symlink) is never kept or replaced.
live_usable() {
	[ -f "$SELF" ] || return 1
	_lu_live=${WZ_LIVE:-${HOME:-/nonexistent}/.local/share/warpzone/updater/updater.sh}
	_lu_self=$(resolve_file "$SELF") || return 1
	_lu_live=$(resolve_file "$_lu_live") || return 1
	[ "$_lu_self" = "$_lu_live" ]
}

# live_valid FILE: a script worth running: not empty (a power cut can leave a file of no
# bytes), with its UPDATER_ABI line, and that `sh -n` parses.
live_valid() {
	[ -s "$1" ] || return 1
	grep -q '^UPDATER_ABI=[0-9][0-9]*[[:space:]]*$' "$1" 2>/dev/null || return 1
	sh -n "$1" >/dev/null 2>&1
}

# live_save: keep the live copy as it is before the first switch of a trial. Not again while
# a copy is kept: a trial that is resumed finds the candidate's script live by then. Written
# to a temporary file, flushed and renamed, as the state is.
live_save() {
	[ ! -e "$LIVE_GOOD" ] || return 0
	live_usable || return 0
	if ! live_valid "$SELF"; then
		log "the live updater is empty or damaged; no copy of it is kept for the trial"
		return 0
	fi
	if cp "$SELF" "$LIVE_GOOD.tmp" 2>/dev/null && sync && mv -f "$LIVE_GOOD.tmp" "$LIVE_GOOD"; then
		sync
		return 0
	fi
	rm -f "$LIVE_GOOD.tmp"
	log "could not keep a copy of the live updater before the trial"
	return 0
}

# live_restore: put the kept copy back as the live one, atomically (a rename: this very run
# keeps reading the file it started with), whatever the ABI of the one it replaces. A kept
# copy that is empty or damaged is not put back, and the live one is left as it is.
live_restore() {
	[ -f "$LIVE_GOOD" ] || return 0
	live_usable || return 0
	cmp -s "$LIVE_GOOD" "$SELF" && return 0
	if ! live_valid "$LIVE_GOOD"; then
		log "the kept copy of the updater is empty or damaged; not put back, the live one is left as it is"
		return 0
	fi
	_lr_tmp="$SELF.restore.$$"
	if cp "$LIVE_GOOD" "$_lr_tmp" 2>/dev/null && chmod 755 "$_lr_tmp" && sync && mv -f "$_lr_tmp" "$SELF"; then
		sync
		log "put back the updater script that was live before the trial"
	else
		rm -f "$_lr_tmp"
		log "could not put back the updater script that was live before the trial"
	fi
	return 0
}

# ---- the venv -------------------------------------------------------------------------
# The venv has no pip and no copy of warpzone: a `.pth` file puts the clone on its path and
# `bin/warpzone` is a shim that runs `python -m warpzone`. That needs no network and no
# setuptools, so it can be made again whenever the interpreter it was made with is gone (an
# operating system update), and the path of its Python, which units and ES-DE scripts hold,
# stays the same. `--clear` is only ever run on a folder that is called `venv`, does not exist
# or holds the mark `warpzone-venv` that this script writes when it makes one, or is noted in
# `venv-owned` in the state folder (written before `--clear`, so a power cut between the clear
# and the mark loses nothing), so never a venv of the user's or the one of an older install;
# is not a symlink, and does not hold the clone: never on the folder around it, where the play
# state lives.

LOCATE_CODE='import importlib.util, os, sys; s = importlib.util.find_spec("warpzone"); root = os.path.realpath(sys.argv[1]); here = os.path.realpath(s.origin) if s is not None and s.origin else ""; sys.exit(0 if here.startswith(root + os.sep) else 7)'
VENV_LOG=$STATE_DIR/last-venv.log
VENV_MARK=warpzone-venv
VENV_OWNED=$STATE_DIR/venv-owned

# venv_noted VENV: warpzone made VENV, as the note in the state folder says.
venv_noted() {
	[ -f "$VENV_OWNED" ] && grep -Fxq -- "$1" "$VENV_OWNED"
}

# venv_note VENV: write that warpzone makes VENV (flushed: it must survive the cut that the
# `--clear` that follows may be).
venv_note() {
	venv_noted "$1" && return 0
	printf '%s\n' "$1" >>"$VENV_OWNED" && sync
}

# venv_adopt VENV: a venv that works, that has the `warpzone-clone.pth` only this script
# writes, and lacks the mark (a cut kept it from being written), gets the mark and the note.
venv_adopt() {
	[ -f "$1/$VENV_MARK" ] && return 0
	for _va_p in "$1"/lib/python*/site-packages/warpzone-clone.pth; do
		[ -f "$_va_p" ] || continue
		venv_note "$1" || return 0
		printf 'made by warpzone updater.sh; it may make this venv again (--clear)\n' >"$1/$VENV_MARK"
		return 0
	done
	return 0
}

# sh_quote S: S in single quotes, for a line of a script.
sh_quote() {
	printf "'%s'" "$(printf '%s' "$1" | sed "s/'/'\\\\''/g")"
}

# venv_works PYTHON: PYTHON runs (Python 3.11 or newer) and warpzone, found without running
# it, is a file of the clone.
venv_works() {
	py_ok "$1" || return 1
	run_limited 60 "$1" -I -c "$LOCATE_CODE" "$CLONE" >/dev/null 2>&1
}

# make_venv BASE VENV: make VENV again with the interpreter BASE, without pip, put the .pth
# to the clone in it and the shim, and check that warpzone imports from it.
make_venv() {
	_mv_base=$1
	_mv_venv=$2
	case $_mv_venv in
	/*) ;;
	*)
		log "not repairing $_mv_venv: it is not an absolute path"
		return 1
		;;
	esac
	if [ "${_mv_venv##*/}" != venv ] || [ -L "$_mv_venv" ]; then
		log "not repairing $_mv_venv: it must be a folder called venv, and not a symlink"
		return 1
	fi
	case $CLONE/ in
	"$_mv_venv"/*)
		log "not repairing $_mv_venv: the clone is inside it"
		return 1
		;;
	esac
	if [ -e "$_mv_venv" ] && { [ ! -d "$_mv_venv" ] || { [ ! -f "$_mv_venv/$VENV_MARK" ] && ! venv_noted "$_mv_venv"; }; }; then
		log "not repairing $_mv_venv: it was not made by warpzone (it has no $VENV_MARK mark), so it is left as it is; move it away, or choose another folder with --venv"
		return 1
	fi
	if ! venv_note "$_mv_venv"; then
		log "not repairing $_mv_venv: cannot note it in $VENV_OWNED"
		return 1
	fi
	log "making the venv $_mv_venv again with $_mv_base"
	if ! run_limited 120 "$_mv_base" -m venv --without-pip --clear "$_mv_venv" >"$VENV_LOG" 2>&1; then
		log "could not make the venv ($(head -n 1 "$VENV_LOG" 2>/dev/null))"
		return 1
	fi
	_mv_py=$_mv_venv/bin/python
	_mv_site="$STATE_DIR/.site.$$"
	run_limited 30 "$_mv_py" -I -c 'import sysconfig; print(sysconfig.get_path("purelib"))' >"$_mv_site" 2>>"$VENV_LOG"
	IFS= read -r _mv_lib <"$_mv_site"
	rm -f "$_mv_site"
	if [ -z "$_mv_lib" ] || ! mkdir -p "$_mv_lib" || ! printf '%s\n' "$CLONE" >"$_mv_lib/warpzone-clone.pth"; then
		log "could not put the clone on the path of the venv"
		return 1
	fi
	_mv_shim="$_mv_venv/bin/warpzone"
	{
		printf '#!/bin/sh\n'
		printf 'exec %s -m warpzone "$@"\n' "$(sh_quote "$_mv_py")"
	} >"$_mv_shim.tmp"
	if ! chmod 755 "$_mv_shim.tmp" || ! mv -f "$_mv_shim.tmp" "$_mv_shim"; then
		log "could not write $_mv_shim"
		return 1
	fi
	if ! printf 'made by warpzone updater.sh; it may make this venv again (--clear)\n' >"$_mv_venv/$VENV_MARK"; then
		log "could not write the mark $_mv_venv/$VENV_MARK"
		return 1
	fi
	if ! venv_works "$_mv_py"; then
		log "the venv $_mv_venv was made, but warpzone cannot be found from it"
		return 1
	fi
	return 0
}

# repair_python: the interpreter of the settings is the venv's. When that does not work (it is
# gone, or the venv no longer finds the clone) and another Python 3.11 or newer is there, make
# the venv again. Sets PY to the venv's Python when it works afterwards.
repair_python() {
	case $C_python in
	/*/bin/python | /*/bin/python3 | /*/bin/python3.*) ;;
	*) return 0 ;;
	esac
	_rp_dir=${C_python%/bin/*}
	if [ "${_rp_dir##*/}" != venv ]; then
		if [ -f "$_rp_dir/pyvenv.cfg" ] && ! venv_works "$C_python"; then
			log_daily "$_rp_dir looks like a venv that does not work, but its folder is not called venv; not repairing it"
		fi
		return 0
	fi
	venv_works "$C_python" && return 0
	for _rp_c in $PY_FALLBACKS; do
		[ "$_rp_c" != "$C_python" ] || continue
		py_ok "$_rp_c" || continue
		make_venv "$_rp_c" "$_rp_dir" || return 1
		find_python || return 1
		return 0
	done
	log_daily "the venv $_rp_dir does not work, and there is no other Python 3.11 or newer to make it again with"
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
	S_last_result="env"
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
		S_suspect_tag=
		S_suspect_sha=
	fi
	S_good_sha=$_head
	S_good_branch=$BRANCH
	S_good_tag=$(g describe --exact-match --tags --match 'v[0-9]*' HEAD 2>/dev/null || true)
	is_release_tag "$S_good_tag" || S_good_tag=
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
SECOND_LOOK=0
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
	4) DV=budget ;;
	*) DV="env" ;;
	esac
	return 0
}

# keep_suspect: in a second look, the release stays under suspicion for another one.
keep_suspect() {
	if [ "$SECOND_LOOK" = 1 ]; then
		S_suspect_tag=$_tag
		S_suspect_sha=$_sha
	fi
}

# retry_suspect TAG SHA: the previous version passes and TAG did not. A failure that does not
# repeat (a service that was slow to come up) must not burn a release for good, so check
# it out and verify it once more. Returns the verification's status, or 5 when the checkout
# failed.
retry_suspect() {
	log "$1 failed once and the previous version passes; checking $1 once more before blaming it"
	_cur=$(head_sha)
	if ! g checkout --quiet --no-overwrite-ignore --detach "$2" >/dev/null 2>&1; then
		heal_half_checkout "$_cur" "$2" "$_cur" || log "the tree is left as it is"
		return 5
	fi
	verify_version "$1"
}

# back_to_good: HEAD on the version known to work; stuck when that cannot be done.
back_to_good() {
	checkout_ref "$S_good_branch" "$S_good_sha" || stuck "checkout of $S_good_sha failed"
	live_restore
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

# attempt: S_trying_* and S_attempts are set. Check it out, verify it, decide. SECOND_LOOK=1
# is the look at a release that was already under suspicion: if it fails again it is bad.
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
	live_save
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
	[ "$DV" = stuck ] || live_restore
	case $DV in
	stuck) stuck "checkout of $S_good_sha failed" ;;
	bad)
		if [ "$SECOND_LOOK" = 1 ]; then
			# It failed a second time and the previous version passes just now: bad.
			write_bad "$_tag" "$_sha" "$_why (second look)"
			log "$_tag is bad ($_why, again); back on the previous version"
			clear_trying
			S_env_failures=0
			S_retry_after=
			S_last_result=rolled-back
			settle_pending
			finish 1
		fi
		retry_suspect "$_tag" "$_sha"
		_rr=$?
		case $_rr in
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
		esac
		_why2=$VERIFY_WHY
		back_to_good
		case $_rr in
		4 | 5)
			# No time or no way to look again now: the release stays under suspicion and the
			# next tick looks at it first thing, with a whole budget. It is not the
			# environment's fault, so there is no back-off.
			S_suspect_tag=$_tag
			S_suspect_sha=$_sha
			log "$_tag failed once and could not be looked at again ($_why2); it will at the next tick"
			;;
		*)
			write_bad "$_tag" "$_sha" "$_why (twice)"
			log "$_tag is bad ($_why, twice); back on the previous version"
			;;
		esac
		clear_trying
		S_env_failures=0
		S_retry_after=
		S_last_result=rolled-back
		settle_pending
		finish 1
		;;
	deferred)
		log "the previous version could not be checked now either; $_tag is not marked bad"
		keep_suspect
		clear_trying
		mark_deferred
		finish 0
		;;
	budget)
		if [ "$SECOND_LOOK" = 1 ]; then
			# Nothing proves it either way: it stays under suspicion, with no back-off.
			log "the previous version could not be checked in the time left; $_tag stays under suspicion"
			keep_suspect
			clear_trying
			S_last_result=rolled-back
			finish 1
		fi
		log "no time left to tell a bad release from a broken environment; $_tag is not marked bad"
		clear_trying
		env_backoff
		finish 1
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
	if ! target_ok "$S_trying_tag"; then
		log "$S_trying_tag is not a target any more (marked bad, retired, off the channel or not signed); going back to the previous version"
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
	rm -f "$LIVE_GOOD"
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
		_rr=$?
		case $_rr in
		0)
			log "$_tag passed the second time"
			probation_pass
			return 0
			;;
		3)
			mark_deferred
			finish 0
			;;
		esac
		_why2=$VERIFY_WHY
		checkout_ref "$S_from_branch" "$S_from_sha" || stuck "checkout of $S_from_sha failed"
		live_restore
		case $_rr in
		4 | 5)
			S_suspect_tag=$_tag
			S_suspect_sha=$_sha
			log "$_tag failed after the restart and could not be looked at again ($_why2); it will at the next tick"
			;;
		*)
			write_bad "$_tag" "$_sha" "$_why (twice)"
			log "$_tag is bad after the restart ($_why, twice); back on the previous version"
			;;
		esac
		# HEAD is on the previous version, which passed: that is the reference again.
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
		live_restore
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

# conf_check: what the settings say about the channel and the pin, made fit to use.
conf_check() {
	case $C_channel in
	stable | rc) ;;
	*)
		log "the channel '$C_channel' is not stable or rc; using stable"
		C_channel=stable
		;;
	esac
	if [ -n "$C_pin" ] && ! rel_key "$C_pin"; then
		log "the pin '$C_pin' is not a tag such as v0.3.0 or v0.3.0rc1; ignoring it"
		C_pin=
	fi
	return 0
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
		if [ "$PY_CUT" = 1 ]; then
			S_last_result="env"
			log "the time budget ran out while looking for python; that is not a missing python"
			finish 1
		fi
		S_last_result=no-python
		log "no usable Python 3.11 or newer was found"
		finish 1
	fi

	conf_check
	fetch_if_due
	detect_kind
	if [ "$KIND" = dirty ]; then
		# A tree that an update of ours left half done is not somebody's work.
		if [ -n "$S_trying_sha" ] && heal_half_checkout "$S_good_sha" "$S_trying_sha" "$S_trying_sha"; then
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

	# Only a checkout on a release is looked after: a developer's clone is not touched, and
	# neither is the venv that belongs to it.
	repair_python || true

	# No trial and no probation: nothing to put back, so the kept copy of the updater goes.
	if [ -z "$S_trying_tag" ] && [ "$S_probation" != 1 ]; then
		rm -f "$LIVE_GOOD"
	fi
	sync_good_reference

	# The keys are the ones of the version known to work, so this comes after the reference.
	signing_setup
	retired_load
	S_pin_retired=
	if [ -n "$C_pin" ] && is_retired "$C_pin"; then
		S_pin_retired=$C_pin
		log_daily "the pinned tag $C_pin is retired ($(retired_why "$C_pin")); the pin wins, and it stays"
	fi

	if [ -n "$S_trying_tag" ]; then
		resume
	fi
	if [ -n "$S_suspect_tag" ]; then
		# A release that failed once, while the previous version passed, and could not be
		# looked at again for lack of time: this is that second look, with a whole budget.
		if ! target_ok "$S_suspect_tag" || ! move_allowed "$S_suspect_tag"; then
			# bad, retired, off the channel or unsigned meanwhile, or not newer than what is
			# checked out any more: never down (unless it is the pin, or the running version
			# has been retired)
			S_suspect_tag=
			S_suspect_sha=
		else
			log "second look at $S_suspect_tag"
			S_trying_tag=$S_suspect_tag
			S_trying_sha=$S_suspect_sha
			S_suspect_tag=
			S_suspect_sha=
			S_attempts=1
			SECOND_LOOK=1
			attempt
		fi
	fi
	probation_check

	if [ -n "$S_retry_after" ] && [ "$S_retry_after" -gt "$NOW" ]; then
		log "waiting until $S_retry_after before trying a new release"
		finish 0
	fi

	pick_target || true
	_target=$PICK
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

install_usage() {
	echo "usage: updater.sh install DEVICE [--config FILE] [--venv DIR] [--channel stable|rc] [-- ARGS...]" >&2
	echo "  checks out the best release of the channel, makes the venv next to the clone and runs" >&2
	echo "  python -m warpzone provision device DEVICE [--config FILE] [ARGS...]" >&2
	exit 2
}

install_fail() {
	echo "updater: $*" >&2
	exit 1
}

# cmd_install DEVICE [options]: the human install, from a git clone. Everything that can be
# refused is refused before anything changes: the clone must be on a release tag or a clean
# branch inside origin/main, a Python 3.11 or newer must exist, and a release must pass the
# rules of a tick (channel, ancestor of origin/main, its own version, not retired, signed
# when signatures are required) and carry this script. Then it checks the release out, makes
# the venv (kept when it already works) and runs `provision device`, which installs the live
# copy of this script, writes its settings and starts the timer.
cmd_install() {
	_i_device=
	_i_config=
	_i_venv=
	_i_channel=stable
	_i_start=$(pwd)
	while [ $# -gt 0 ]; do
		case $1 in
		--config)
			[ $# -ge 2 ] || install_usage
			_i_config=$2
			shift 2
			;;
		--venv)
			[ $# -ge 2 ] || install_usage
			_i_venv=$2
			shift 2
			;;
		--channel)
			[ $# -ge 2 ] || install_usage
			case $2 in stable | rc) ;; *) install_usage ;; esac
			_i_channel=$2
			shift 2
			;;
		--)
			shift
			break
			;;
		-*) install_usage ;;
		*)
			[ -z "$_i_device" ] || install_usage
			_i_device=$1
			shift
			;;
		esac
	done
	[ -n "$_i_device" ] || install_usage
	# what is left in "$@" goes to provision

	command -v git >/dev/null 2>&1 || install_fail "git is not installed"
	CLONE=$(cd "$(dirname "$0")/../.." 2>/dev/null && pwd)
	if [ -z "$CLONE" ] || ! g rev-parse --git-dir >/dev/null 2>&1; then
		install_fail "$(dirname "$0")/../.. is not a git checkout: install from a clone (git clone <repository>)"
	fi
	mkdir -p "$STATE_DIR" || install_fail "cannot create $STATE_DIR"
	if ! acquire_lock; then
		install_fail "an update is running (the lock in $STATE_DIR is held); try again in a few minutes"
	fi
	trap release_lock EXIT
	trap 'rl_abort; exit 143' HUP INT TERM
	C_channel=$_i_channel
	C_pin=
	find_python || install_fail "no Python 3.11 or newer was found (tried: $PY_FALLBACKS)"
	_i_py=$PY

	if run_limited "$FETCH_TIMEOUT" git -C "$CLONE" fetch --tags origin >"$STATE_DIR/.fetch.$$" 2>&1; then
		log "fetched the tags of the remote"
	else
		log "could not fetch the remote ($(head -n 1 "$STATE_DIR/.fetch.$$" 2>/dev/null)); using the tags already here"
	fi
	rm -f "$STATE_DIR/.fetch.$$"
	detect_kind
	case $KIND in
	dirty) install_fail "$CLONE has changes to tracked files: commit or discard them, or install from a fresh clone" ;;
	dev) install_fail "$CLONE has commits of its own, or is not on a release: install from a fresh clone" ;;
	nogit) install_fail "$CLONE is not a git checkout" ;;
	esac

	state_reset
	S_good_sha=$(head_sha)
	signing_setup
	[ "$SIGN_ON" = 1 ] || log "signatures are not required: $SIGN_WHY"
	retired_load
	_i_pick=
	for _tag in $(g tag --list 'v*' 2>/dev/null | order_tags); do
		target_ok "$_tag" || continue
		g cat-file -e "refs/tags/$_tag:warpzone/data/updater.sh" 2>/dev/null || continue
		_i_pick=$_tag
		break
	done
	[ -n "$_i_pick" ] ||
		install_fail "no release of the $_i_channel channel can be installed: none that is valid carries warpzone/data/updater.sh"

	if [ "$(g rev-parse "refs/tags/$_i_pick^{commit}")" = "$(head_sha)" ]; then
		log "the clone is on $_i_pick already"
	else
		log "checking out $_i_pick"
		g checkout --quiet --no-overwrite-ignore --detach "refs/tags/$_i_pick" >/dev/null 2>&1 ||
			install_fail "could not check out $_i_pick"
	fi

	[ -n "$_i_venv" ] || _i_venv=$(dirname "$CLONE")/venv
	case $_i_venv in /*) ;; *) _i_venv=$_i_start/$_i_venv ;; esac
	if venv_works "$_i_venv/bin/python"; then
		log "the venv $_i_venv works; keeping it"
		venv_adopt "$_i_venv"
	else
		make_venv "$_i_py" "$_i_venv" || install_fail "the venv $_i_venv could not be made (see $VENV_LOG)"
	fi
	_i_vpy=$_i_venv/bin/python
	PY=$_i_vpy
	run_limited "${STEP_TIMEOUT:-120}" "$_i_vpy" -I -c "$IMPORT_CODE" "$CLONE" >"$VERIFY_LOG" 2>&1 ||
		install_fail "warpzone $_i_pick does not import with $_i_vpy (see $VERIFY_LOG)"

	log "running provision device $_i_device"
	# From the clone, as a tick runs it: `-m` puts the current folder first on the path, and the
	# folder the person happens to be in must not be able to supply another warpzone.
	case $_i_config in '' | /*) ;; *) _i_config=$_i_start/$_i_config ;; esac
	cd "$CLONE" || install_fail "cannot enter $CLONE"
	if [ -n "$_i_config" ]; then
		"$_i_vpy" -m warpzone provision device "$_i_device" --config "$_i_config" "$@"
	else
		"$_i_vpy" -m warpzone provision device "$_i_device" "$@"
	fi
	_i_rc=$?
	[ "$_i_rc" -eq 0 ] || install_fail "provision device failed (exit $_i_rc); fix what it says and run the install again"
	log "installed warpzone ${_i_pick#v} for $_i_device in $CLONE (venv $_i_venv)"
	exit 0
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

cmd_order() {
	order_tags
	exit 0
}

cmd_selftest() {
	_bad=0
	for _c in git sed grep tr cut find cksum cp cmp sync sleep date kill mkdir mv rm cat head tail wc dirname sort; do
		if ! command -v "$_c" >/dev/null 2>&1; then
			echo "selftest: missing command: $_c"
			_bad=1
		fi
	done
	ver_key 0.3.0rc2
	_k1=$VK
	ver_key 0.3.0
	_k2=$VK
	ver_key 0.10.0rc1
	_k3=$VK
	ver_key 0.3.0.dev0
	_k4=$VK
	if ! { key_gt "$_k2" "$_k1" && key_gt "$_k3" "$_k2" && key_gt "$_k1" "$_k4" && ! key_gt "$_k2" "$_k2"; }; then
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
	for _t in v0.1.0 v10.20.30 v0.3.0rc1 v0.3.0rc12; do
		rel_key "$_t" || {
			echo "selftest: $_t should be a release or a candidate"
			_bad=1
		}
	done
	for _t in v0.1 v0.3.0-rc1 v0.3.0rc 0.3.0 v0.3.0rc1rc2 v1.2.3.4 v0.3.0beta1; do
		if rel_key "$_t"; then
			echo "selftest: $_t should not be a release or a candidate"
			_bad=1
		fi
	done
	if [ "$(printf '%s\n' v0.3.0 v0.3.0rc2 v0.10.0rc1 v0.3.0rc10 v0.9.0 | order_tags | tr '\n' ' ')" != "v0.10.0rc1 v0.9.0 v0.3.0 v0.3.0rc10 v0.3.0rc2 " ]; then
		echo "selftest: the order of the tags is wrong"
		_bad=1
	fi
	_dir="${TMPDIR:-/tmp}/wz-selftest.$$"
	if mkdir "$_dir" 2>/dev/null; then
		STATE_DIR=$_dir
		STATE_FILE=$_dir/state
		BAD_DIR=$_dir/bad
		LOG=
		state_load
		S_good_tag=v0.1.0
		S_trying_tag=v0.1.0
		S_trying_sha=0123456789abcdef
		S_attempts=2
		S_last_result=ok
		state_save
		state_load
		if [ "$S_good_tag" != v0.1.0 ] || [ "$S_attempts" != 2 ] || [ "$S_last_result" != ok ]; then
			echo "selftest: the state does not survive a save and a load"
			_bad=1
		fi
		if ! run_limited 5 true; then
			echo "selftest: cannot run a command"
			_bad=1
		fi
		run_limited 5 sh -c 'exit 3'
		_rc=$?
		if [ "$_rc" -ne 3 ]; then
			echo "selftest: lost an exit status"
			_bad=1
		fi
		run_limited 1 sleep 30
		_rc=$?
		if [ "$_rc" -ne 124 ]; then
			echo "selftest: a command that hangs is not cut off"
			_bad=1
		fi
		rm -rf "$_dir"
	else
		echo "selftest: cannot create a temporary folder"
		_bad=1
	fi
	if [ "$(sh_quote "a'b c")" != "'a'\\''b c'" ]; then
		echo "selftest: sh_quote is wrong"
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
install)
	shift
	cmd_install "$@"
	;;
menu) cmd_menu ;;
selftest) cmd_selftest ;;
order) cmd_order ;;
*)
	echo "usage: updater.sh tick|install|menu|selftest|order" >&2
	exit 2
	;;
esac
