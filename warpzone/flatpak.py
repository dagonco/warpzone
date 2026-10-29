"""Facts about Syncthing as a flatpak (SyncThingy) on the consoles. Pure: no I/O.

The app id lives here and only here; `localst`, `doctor` and `provision` import it.

A flatpak cannot see the data folders of other flatpaks: `~/.var/app` is hidden even
with the `host` filesystem. A save folder under `~/.var/app/<app>` therefore looks empty
to SyncThingy until it is granted that folder with `flatpak override --user --filesystem`.
"""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path

SYNCTHINGY_APP = "com.github.zocker_160.SyncThingy"

# The command a systemd user unit runs. `flatpak run` puts the process in a scope of
# its own, so `systemctl --user stop` cannot stop it: the unit needs an `ExecStop` that
# asks flatpak to kill the app, or a restart leaves the old process holding the port.
SYNCTHINGY_EXEC = (
    f"/usr/bin/flatpak run --command=syncthing {SYNCTHINGY_APP} serve --no-browser --no-restart"
)
SYNCTHINGY_STOP = f"/usr/bin/flatpak kill {SYNCTHINGY_APP}"


def sandbox_dir(home: Path) -> Path:
    """The flatpak's own data folder, where its Syncthing keeps `config.xml`."""
    return home / ".var" / "app" / SYNCTHINGY_APP


def is_syncthingy_config(config_path: Path, home: Path) -> bool:
    """True when this `config.xml` belongs to the SyncThingy flatpak."""
    return sandbox_dir(home) in config_path.parents


def app_dir(app: str) -> str:
    """How `flatpak override` spells the data folder of `app`."""
    return f"~/.var/app/{app}"


def app_ids(paths: Iterable[str], home: Path) -> list[str]:
    """The apps whose data folder (`<home>/.var/app/<app>`) holds any of `paths`, in order
    of first use. SyncThingy's own folder is left out: it can always see it."""
    prefix = f"{home}/.var/app/"
    found: dict[str, None] = {}
    for path in paths:
        if path.startswith(prefix):
            app = path[len(prefix) :].split("/", 1)[0]
            if app and app != SYNCTHINGY_APP:
                found[app] = None
    return list(found)


def granted_apps(show_output: str, home: Path) -> set[str]:
    """The apps whose data folder is writable for SyncThingy, from the output of
    `flatpak override --user --show`. Negated (`!`) and read-only (`:ro`) entries grant
    nothing the sync can use; `~/`, `$HOME/` and the absolute home all spell the same
    folder."""
    granted: set[str] = set()
    for line in show_output.splitlines():
        key, sep, value = line.partition("=")
        if not sep or key.strip() != "filesystems":
            continue
        for entry in value.split(";"):
            entry = entry.strip()
            if not entry or entry.startswith("!"):
                continue
            path, _, mode = entry.partition(":")
            if mode == "ro":
                continue
            for spelling in ("$HOME/", f"{home}/"):
                if path.startswith(spelling):
                    path = "~/" + path[len(spelling) :]
            prefix = "~/.var/app/"
            if path.startswith(prefix):
                granted.add(path[len(prefix) :].rstrip("/"))
    return granted


def missing_apps(apps: Iterable[str], show_output: str, home: Path) -> list[str]:
    granted = granted_apps(show_output, home)
    return [app for app in apps if app not in granted]


def show_command() -> list[str]:
    return ["flatpak", "override", "--user", "--show", SYNCTHINGY_APP]


def override_command(apps: Iterable[str]) -> list[str]:
    """One `flatpak override` that grants SyncThingy every app in `apps`."""
    return [
        "flatpak",
        "override",
        "--user",
        *(f"--filesystem={app_dir(app)}" for app in apps),
        SYNCTHINGY_APP,
    ]
