"""The entries `provision device` adds to the ArkOS "Options" menu. Pure: no I/O.

ArkOS lists every executable `.sh` in `/opt/system` in its Options menu. Each entry is a bash
script that reads warpzone's local files (never the network), then shows the result with
ArkOS's `msgbox` helper, or prints it when there is none. They never fail hard: whatever is
missing shows as `unknown`. See development/design/profiles.md, "Options menu".
"""

from __future__ import annotations

STATUS_NAME = "Warpzone Status.sh"
UPDATE_NAME = "Warpzone Update Now.sh"
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
# The status reads local files only.
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
Last result: $(or_unknown "$RESULT")
Last fetch: $(or_unknown "$FETCH")
Doctor: $DOCTOR_TEXT"
show "$TEXT"
exit 0
"""

_UPDATE = f"""
if [ ! -x "$WZ" ]; then
	show "warpzone is not installed here (unknown)"
	exit 0
fi
OUT=$("$WZ" self-update --now 2>&1)
RC=$?
LAST=$(printf '%s\\n' "$OUT" | grep -v '^$' | tail -n 6 | cut -c1-{LINE_WIDTH})
[ -n "$LAST" ] || LAST="(no output)"
show "Update check finished (exit $RC)
$LAST"
exit 0
"""


def render_status_script() -> str:
    """The bash script of the "Warpzone Status" entry."""
    return _COMMON + _STATUS


def render_update_script() -> str:
    """The bash script of the "Warpzone Update Now" entry."""
    return _COMMON + _UPDATE


def entries() -> dict[str, str]:
    """File name -> script text, for every entry `provision device` installs."""
    return {STATUS_NAME: render_status_script(), UPDATE_NAME: render_update_script()}
