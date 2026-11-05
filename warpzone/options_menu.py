"""The "Warpzone" entry `provision device` adds to the ArkOS "Options" menu. Pure: no I/O.

ArkOS lists every executable `.sh` in `/opt/system` in its Options menu. The entry is a bash
script that reads warpzone's local files (never the network) and offers, with `dialog` and
ArkOS's controller-to-keyboard helper `gptokeyb` (what ArkOS's own `msgbox` uses):

- the status: version, channel, pin, the version known to work, a new release the timer found
  (`available`), the last result, the last fetch and the problems `warpzone doctor` reports;
- "Install <tag>", only when there is a new release: the live updater's `tick --now` (or
  `warpzone self-update --now` with an older updater), an update asked for by hand;
- the channel: `warpzone self-update --channel stable|rc`, after a yes/no question.

Without `dialog` or a writable terminal it shows the status the old way (`msgbox`, or standard
output) and does nothing else. It never fails hard: whatever is missing shows as `unknown`. See
development/design/profiles.md, "Options menu".
"""

from __future__ import annotations

ENTRY_NAME = "Warpzone.sh"
# Entries of earlier versions: `provision device` removes them when they carry the marker.
OLD_NAMES = ("Warpzone Status.sh", "Warpzone Update Now.sh")
# Every script starts with this line: a file of that name without it is not ours, and
# `provision device` leaves it alone.
MARKER = "# Written by `warpzone provision device`; changes are overwritten by the next run."
# The screen is small: a doctor line is cut to this many characters and at most this many
# lines are shown.
LINE_WIDTH = 40
MAX_DOCTOR_LINES = 8
# How long `warpzone doctor` may take before the status shows what it has.
DOCTOR_SECONDS = 60

_COMMON = f"""#!/bin/bash
{MARKER}
# Nothing here fails the menu: whatever is missing shows as unknown.
WZ_HOME=${{HOME:-/nonexistent}}
WZ="$WZ_HOME/.local/share/warpzone/venv/bin/warpzone"

# show TEXT: a message box when ArkOS has one, standard output otherwise.
show() {{
	if command -v msgbox >/dev/null 2>&1; then
		msgbox "$1"
	elif [ -x /usr/bin/msgbox ]; then
		/usr/bin/msgbox "$1"
	else
		printf '%s\\n' "$1"
	fi
}}
"""

_STATUS = f"""
# `warpzone doctor` may take a while: say so on the screen meanwhile.
printf 'Warpzone: reading the status...\\n'
# The status reads local files only (never the network).
UPDATER_DIR="$WZ_HOME/.local/state/warpzone/updater"
# kv FILE KEY: the value of the last `KEY=value` line of FILE, empty when there is none.
kv() {{
	[ -r "$1" ] || return 0
	sed -n "s/^$2=//p" "$1" 2>/dev/null | tr -d '\\r' | tail -n 1
}}

# or_unknown VALUE LABEL: VALUE, or LABEL when it is empty.
or_unknown() {{
	if [ -n "$1" ]; then printf '%s' "$1"; else printf '%s' "${{2:-unknown}}"; fi
}}

VERSION=
if [ -x "$WZ" ]; then
	VERSION=$("$WZ" --version 2>/dev/null | head -n 1)
fi
CHANNEL=$(kv "$UPDATER_DIR/conf" channel)
PIN=$(kv "$UPDATER_DIR/conf" pin)
GOOD=$(kv "$UPDATER_DIR/state" good_tag)
AVAILABLE=$(kv "$UPDATER_DIR/state" available)
RESULT=$(kv "$UPDATER_DIR/state" last_result)
FETCH=$(kv "$UPDATER_DIR/state" last_fetch)
FMT='+%Y-%m-%d %H:%M'
case $FETCH in
'' | *[!0-9]*) FETCH= ;;
*) FETCH=$(date -d "@$FETCH" "$FMT" 2>/dev/null || date -r "$FETCH" "$FMT" 2>/dev/null) ;;
esac

PROBLEMS=
if [ -x "$WZ" ]; then
	if command -v timeout >/dev/null 2>&1; then
		DOCTOR=$(timeout {DOCTOR_SECONDS} "$WZ" doctor 2>/dev/null)
	else
		DOCTOR=$("$WZ" doctor 2>/dev/null)
	fi
	ISSUE='^ *\\[(!!|! )\\]'
	PROBLEMS=$(printf '%s\\n' "$DOCTOR" | grep -E "$ISSUE" | sed 's/^ *//' | cut -c1-{LINE_WIDTH})
fi
COUNT=$(printf '%s\\n' "$PROBLEMS" | grep -c .)
if [ ! -x "$WZ" ]; then
	DOCTOR_TEXT=unknown
elif [ "$COUNT" -eq 0 ]; then
	DOCTOR_TEXT="no problems"
else
	DOCTOR_TEXT=$(printf '%s\\n' "$PROBLEMS" | head -n {MAX_DOCTOR_LINES})
	[ "$COUNT" -le {MAX_DOCTOR_LINES} ] || DOCTOR_TEXT="$DOCTOR_TEXT
+$((COUNT - {MAX_DOCTOR_LINES})) more"
fi

TEXT="Version: $(or_unknown "$VERSION")
Channel: $(or_unknown "$CHANNEL")
Pin: $(or_unknown "$PIN" none)
Good tag: $(or_unknown "$GOOD")
New release: $(or_unknown "$AVAILABLE" none)
Last result: $(or_unknown "$RESULT")
Last fetch: $(or_unknown "$FETCH")
Doctor: $DOCTOR_TEXT"
"""

_MENU = """
# The live updater runs the update itself when it knows `tick --now`: it does not need the
# clone's Python, so it can still put back a release that no longer starts.
LIVE="$WZ_HOME/.local/share/warpzone/updater/updater.sh"
update_now() {
	if [ -f "$LIVE" ] && grep -Eq '^UPDATER_ABI=([2-9]|[1-9][0-9]+)[[:space:]]*$' "$LIVE"; then
		OUT=$(sh "$LIVE" tick --now 2>&1)
		RC=$?
	elif [ -x "$WZ" ]; then
		OUT=$("$WZ" self-update --now 2>&1)
		RC=$?
	else
		OUT="warpzone is not installed here (unknown)"
		RC=1
	fi
	LAST=$(printf '%s\\n' "$OUT" | grep -v '^$' | tail -n 6 | fold -s -w WIDTH)
	[ -n "$LAST" ] || LAST="(no output)"
	RESULT="Update finished (exit $RC)
$LAST"
}

TTY=${WZ_TTY:-/dev/tty1}
# WZ_TTY is for tests: a plain file as the terminal, no controller helper and no font.
GPTK=/opt/inttools/gptokeyb
[ -z "${WZ_TTY:-}" ] || GPTK=
if [ -z "${WZ_TTY:-}" ] && [ -e "$TTY" ] && [ ! -w "$TTY" ]; then
	sudo -n chmod 666 "$TTY" >/dev/null 2>&1
fi
# Without dialog, the terminal or the controller helper, a box would wait for keys that never
# come: show the status the old way instead.
CAN=1
command -v dialog >/dev/null 2>&1 || CAN=
[ -w "$TTY" ] || CAN=
[ -z "$GPTK" ] || [ -x "$GPTK" ] || CAN=
if [ -z "$CAN" ]; then
	show "$TEXT"
	exit 0
fi

# The font ArkOS's own Wifi entry uses, so the boxes fit; put back on exit as ArkOS does.
FONT_SET=
if [ -n "$GPTK" ]; then
	FONT=Lat7-TerminusBold22x11
	[ ! -e /dev/input/by-path/platform-odroidgo2-joypad-event-joystick ] || FONT=Lat7-Terminus16
	sudo -n setfont "/usr/share/consolefonts/$FONT.psf.gz" >/dev/null 2>&1 && FONT_SET=1
fi

# The controller drives `dialog` through gptokeyb, as in ArkOS's own msgbox: started when none
# runs, stopped (kill -9, as ArkOS does) while an update runs and on exit.
GPTK_PID=
gptk_up() {
	[ -n "$GPTK" ] || return 0
	if pgrep -x gptokeyb >/dev/null 2>&1 || pgrep -f oga_controls >/dev/null 2>&1; then
		return 0
	fi
	sudo -n chmod 666 /dev/uinput >/dev/null 2>&1
	SDL_GAMECONTROLLERCONFIG_FILE=/opt/inttools/gamecontrollerdb.txt \\
		"$GPTK" -1 dialog -c /opt/inttools/keys.gptk >/dev/null 2>&1 &
	GPTK_PID=$!
}
gptk_down() {
	[ -z "$GPTK_PID" ] || kill -9 "$GPTK_PID" >/dev/null 2>&1
	GPTK_PID=
}
# drain: drop the keys pressed while nothing was listening, so they do not close the next box.
drain() {
	while read -r -s -t 0.2 -n 1 _ <"$TTY" 2>/dev/null; do :; done
}
[ -f /opt/inttools/noshadows.dialogrc ] && export DIALOGRC=/opt/inttools/noshadows.dialogrc
# shellcheck disable=SC2329 # run by the trap
finish() {
	gptk_down
	if [ -n "$FONT_SET" ]; then
		sudo -n setfont /usr/share/consolefonts/Lat7-Terminus20x10.psf.gz >/dev/null 2>&1
	fi
	printf '\\033c' >"$TTY" 2>/dev/null
}
trap finish EXIT
printf '\\033c\\e[?25l' >"$TTY" 2>/dev/null
gptk_up

box() {
	dialog --clear --title Warpzone --msgbox "$1" 20 50 >"$TTY" 2>&1
}

case $CHANNEL in
rc) OTHER=stable ;;
*) OTHER=rc ;;
esac
SUMMARY="$(or_unknown "$VERSION")
Channel: $(or_unknown "$CHANNEL")"
[ -z "$AVAILABLE" ] || SUMMARY="$SUMMARY
New release: $AVAILABLE"
set -- status "Status"
[ -z "$AVAILABLE" ] || set -- "$@" install "Install $AVAILABLE now"
set -- "$@" channel "Follow $OTHER releases" exit "Exit"
CHOICE=$(dialog --clear --no-tags --title Warpzone --menu "$SUMMARY" 15 50 4 "$@" 2>&1 >"$TTY")
case $CHOICE in
status) box "$TEXT" ;;
install)
	gptk_down
	stty -F "$TTY" -echo >/dev/null 2>&1
	dialog --title Warpzone --infobox "Installing $AVAILABLE... (a few minutes)" 5 50 >"$TTY" 2>&1
	update_now
	stty -F "$TTY" echo >/dev/null 2>&1
	drain
	gptk_up
	box "$RESULT"
	;;
channel)
	ASK="Follow $OTHER releases from now on?
(An update never goes down: the version installed now stays until a newer one comes.)"
	if dialog --clear --title Warpzone --yesno "$ASK" 10 50 >"$TTY" 2>&1; then
		if [ -x "$WZ" ]; then
			OUT=$("$WZ" self-update --channel "$OTHER" 2>&1)
		else
			OUT="warpzone is not installed here (unknown)"
		fi
		box "$(printf '%s\\n' "$OUT" | sed 's/^self-update: //' | fold -s -w WIDTH | tail -n 10)"
	fi
	;;
esac
exit 0
""".replace("WIDTH", str(LINE_WIDTH))


def render_script() -> str:
    """The bash script of the "Warpzone" entry."""
    return _COMMON + _STATUS + _MENU


def entries() -> dict[str, str]:
    """File name -> script text, for every entry `provision device` installs."""
    return {ENTRY_NAME: render_script()}
