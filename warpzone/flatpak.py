"""Facts about Syncthing as a flatpak (SyncThingy) on the consoles. Pure: no I/O.

The app id lives here and only here; `localst`, `doctor` and `provision` import it.
"""

from __future__ import annotations

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
