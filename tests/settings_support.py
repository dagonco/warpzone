"""Shared fixtures for the settings runner tests: a synthetic profile, a temporary home, a
temporary state directory and a fake /proc. Every value is made up."""

import io
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from warpzone import settings
from warpzone.profiles import DataFile, EmulatorSettings, Profile, SettingsFile

DEVICE = "deck"
FAKE_TOKEN = "tok-FAKE-0123456789"  # gitleaks:allow
FAKE_USER = "fake-user-name"

ALPHA_INI = f"""[Video]
res = 1
gpu = 0

[System]
language = 1

[Controls]
pad = x

[Web]
token = {FAKE_TOKEN}
nick_user = {FAKE_USER}
"""

BETA_JSON = """{
  "name": "box",
  "video": {
    "scale": 1,
    "vsync": true
  },
  "list": [1, 2]
}
"""


def profile() -> Profile:
    alpha = EmulatorSettings(
        id="alpha",
        process=("alpha",),
        files={
            "main": SettingsFile(
                id="main",
                path="~/.config/alpha/main.ini",
                format="ini",
                noise=("[Controls]*",),
                secret=("[Web]*_user",),
                device=("[Video]res*",),
                known=("[System]language",),
            )
        },
    )
    beta = EmulatorSettings(
        id="beta",
        process=("beta",),
        files={
            "config": SettingsFile(id="config", path="~/.config/beta/config.json", format="json")
        },
    )
    gamma = EmulatorSettings(
        id="gamma",
        process=("gamma",),
        files={
            "cfg": SettingsFile(id="cfg", path="~/.config/gamma/gamma.cfg", format="retroarch-cfg"),
            "opt": SettingsFile(
                id="opt",
                path="~/.config/gamma/{name}/{name}.opt",
                format="retroarch-cfg",
                create=True,
            ),
        },
        data={
            "eeprom": DataFile(
                id="eeprom", path="~/data/gamma/eeprom.bin", create_when="~/data/gamma"
            )
        },
    )
    return Profile(
        name="synthetic",
        description="",
        paths={"settings": "~/hub/settings"},
        settings={"alpha": alpha, "beta": beta, "gamma": gamma},
    )


class SettingsCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        root = Path(self._tmp.name)
        self.home = root / "home"
        self.state = root / "state"
        self.proc = root / "proc"
        self.home.mkdir()
        self.proc.mkdir()
        self.clock_value = datetime(2026, 1, 2, 3, 4, 5)
        self.env = settings.Env(self.home, self.state, self.proc, lambda: self.clock_value)
        self.ctx = settings.make_context(DEVICE, profile(), self.env)
        self.folder = self.ctx.folder
        self.folder.mkdir(parents=True)
        (self.folder / ".stfolder").mkdir()

    # -- files ----------------------------------------------------------------------------

    def put(self, relative: str, text: str | bytes) -> Path:
        path = self.home / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(text, bytes):
            path.write_bytes(text)
        else:
            path.write_text(text)
        return path

    def text(self, relative: str) -> str:
        return (self.home / relative).read_text()

    def layer(self, name: str, body: str) -> None:
        (self.folder / f"{name}.toml").write_text(f"format = 1\n{body}")

    def hub_file(self, relative: str, data: bytes) -> None:
        path = self.folder / "files" / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)

    def fake_process(self, pid: int, comm: str, argv: str | None = None) -> None:
        entry = self.proc / str(pid)
        entry.mkdir(parents=True, exist_ok=True)
        (entry / "comm").write_text(comm + "\n")
        (entry / "cmdline").write_bytes((argv or comm).encode() + b"\0")

    # -- state ----------------------------------------------------------------------------

    def state_text(self) -> str:
        """Everything under the state directory except backups, as one string."""
        if not self.state.exists():
            return ""
        return "".join(
            p.read_text()
            for p in sorted(self.state.rglob("*"))
            if p.is_file() and "backups" not in p.parts
        )

    def backups(self, emulator: str, file_id: str) -> list[Path]:
        folder = self.state / "backups" / emulator / file_id
        return sorted(folder.iterdir()) if folder.exists() else []

    def snapshot(self, emulator: str, file_id: str) -> dict | None:
        return settings.state.Snapshots(self.state).load(emulator, file_id)

    # -- running --------------------------------------------------------------------------

    def run_apply(self, **kwargs: object) -> tuple[int, list[str], list[str]]:
        out: list[str] = []
        err: list[str] = []
        code = settings.apply(self.ctx, out=out.append, err=err.append, **kwargs)  # type: ignore[arg-type]
        return code, out, err

    def run_audit(self, **kwargs: object) -> tuple[int, str]:
        buffer = io.StringIO()
        code = settings.audit(self.ctx, out=lambda line: buffer.write(line + "\n"), **kwargs)  # type: ignore[arg-type]
        return code, buffer.getvalue()
