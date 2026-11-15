#!/bin/sh
# The entrypoint of the hub image: gets /tmp ready for Vita3K when the image has it, then runs
# `warpzone` with the arguments it was given (`serve` unless the command says otherwise).
#
# The container's root is read-only and /tmp is memory, so what Vita3K needs in /tmp has to be made
# at each start. The build 4334 binary has two paths of its own built in, which must be links to
# the bundle's `share` and `lib` folders, and Qt wants a private runtime folder.
#
# A first argument that is an absolute path runs that program instead of warpzone, after the same
# preparation: `docker run ... warpzone:local /usr/local/bin/Vita3K --version` is how the CI job
# checks Vita3K the way `warpzone inbox` runs it. No warpzone command starts with a slash.
set -eu

vita3k=/opt/vita3k

prepare_vita3k() {
    ln -sfn "$vita3k/share" /tmp/lWRUi || return 1
    ln -sfn "$vita3k/lib" /tmp/CMR || return 1
    mkdir -p "$XDG_RUNTIME_DIR" || return 1
    chmod 700 "$XDG_RUNTIME_DIR" || return 1
}

if [ -x "$vita3k/bin/Vita3K" ]; then
    : "${XDG_RUNTIME_DIR:=/tmp/xdg-runtime-$(id -u)}"
    export XDG_RUNTIME_DIR
    # warpzone and its other tools do not need any of it, so a failure here is only a warning.
    prepare_vita3k || echo "warpzone-entrypoint: could not prepare /tmp for Vita3K" >&2
fi

case "${1:-}" in
/*) exec "$@" ;;
esac
exec warpzone "$@"
