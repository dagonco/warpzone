"""How to install what is missing, per platform family: the hint tables and `hint_for`.

The one place that says how to install things; the checks put the right entry in their fix.
See development/design/doctor.md.
"""

from __future__ import annotations

from collections.abc import Mapping

from warpzone.flatpak import SYNCTHINGY_APP

#
# The one place that says how to install things, per platform family. A family is what
# `platform_family` returns: macos, debian (also Ubuntu and derivatives), fedora (also
# RHEL-likes), arch, steamos (Arch, but with a read-only root), or linux (anything else).
# `hint_for` falls back from the exact family to "linux" and then to "any". Only add a
# command here when it is known to be right for that family.

STEAMOS_ROOT = (
    "SteamOS has a read-only root. `warpzone inbox` is meant for a desktop that mounts the "
    "hub's share, so install this there. To run it on SteamOS anyway, use a container "
    "(distrobox with a Debian image) and the Debian command inside it"
)

REPO_URL_LIBRETRO = "https://github.com/libretro/libretro-database"
DAT_CLONE_COMMAND = (
    f"git clone --depth 1 --filter=blob:none --sparse {REPO_URL_LIBRETRO} && "
    "git -C libretro-database sparse-checkout set metadat/redump metadat/no-intro"
)
DAT_CLONE_HINT = (
    f"{DAT_CLONE_COMMAND}; then set dats_dir in [inbox] to the metadat folder of that clone"
)
DAT_MOUNT_HINT = (
    f"the image holds no DATs: run `{DAT_CLONE_COMMAND}` next to compose.yaml, mount that "
    "folder into the container as a volume, and set dats_dir in [inbox] to its metadat "
    "folder as the container sees it"
)
RAHASHER_BUILD = (
    "build it from https://github.com/RetroAchievements/RALibretro with CHD support: "
    "`git clone --recursive https://github.com/RetroAchievements/RALibretro && "
    "cd RALibretro && make -f Makefile.RAHasher HAVE_CHD=1`; "
    "then set rahasher in [inbox] to the RAHasher binary it builds"
)

HintTable = Mapping[str, str]

# The fix for a tool the hub image does not have: the install commands of the platform families
# are for a computer, and a container's root is read-only and rebuilt from the image.
IMAGE_MISSING_TOOL = (
    "this hub image does not ship it: run `warpzone inbox` on a computer that mounts the "
    "hub's share (install the tool there), or build the image with it "
    "(https://dagonco.github.io/warpzone/setup/inbox/#where-inbox-runs)"
)

CHDMAN_HINTS: HintTable = {
    "macos": "brew install rom-tools",
    "debian": "sudo apt install mame-tools",
    "fedora": "sudo dnf install mame-tools",
    "arch": "sudo pacman -S mame-tools",
    "steamos": f"{STEAMOS_ROOT}: sudo apt install mame-tools",
    "any": "install MAME's tools (the package that ships `chdman`) with your package manager",
}
BSDTAR_HINTS: HintTable = {
    "macos": "bsdtar ships with macOS (/usr/bin/bsdtar); check that it is not hidden by PATH",
    "debian": "sudo apt install libarchive-tools",
    "fedora": "sudo dnf install bsdtar",
    "arch": "sudo pacman -S libarchive",
    "steamos": f"{STEAMOS_ROOT}: sudo apt install libarchive-tools",
    "any": "install libarchive's `bsdtar` with your package manager",
}
RAHASHER_HINTS: HintTable = {"any": RAHASHER_BUILD}
DOLPHIN_HINTS: HintTable = {
    "macos": "install Dolphin (brew install --cask dolphin); dolphin-tool is inside the app: "
    "set dolphin_tool in [inbox] to /Applications/Dolphin.app/Contents/MacOS/dolphin-tool",
    "linux": "dolphin-tool ships with Dolphin; install Dolphin, or with the Flatpak use "
    "`flatpak run --command=dolphin-tool org.DolphinEmu.dolphin-emu`",
    "any": "dolphin-tool ships with Dolphin; install Dolphin",
}
NSZ_HINTS: HintTable = {"any": "uv tool install nsz (or: pipx install nsz)"}
ORBIS_PKG_UTIL_HINTS: HintTable = {
    "any": "cargo install orbis-pkg-util (the binary lands in ~/.cargo/bin; set orbis_pkg_util "
    "in [inbox] to it if that folder is not on PATH)"
}
VITA3K_HINTS: HintTable = {
    "macos": "download Vita3K for macOS from https://vita3k.org and put Vita3K.app in "
    "/Applications, or set vita3k in [inbox] to the app",
    "any": "get a plain (not AppImage) Vita3K binary for Linux from https://vita3k.org and "
    "set vita3k in [inbox] to it, or put it on PATH as `vita3k`",
}

TITLEDB_REPO = "https://github.com/blawar/titledb"
TITLEDB_HINT = (
    f"git clone --depth 1 {TITLEDB_REPO}, then set titledb_dir in [inbox] to that clone "
    "(update it with git pull); without it Switch games cannot be named"
)
KEYS_HINT = (
    "put your console's prod.keys at ~/.switch/prod.keys, or set switch_keys in [inbox] to "
    "its path; warpzone never reads the file, it only hands the path to nsz"
)
SYNCTHING_HINTS: HintTable = {
    "macos": "brew install syncthing && brew services start syncthing",
    "debian": "sudo apt install syncthing && systemctl --user enable --now syncthing",
    "fedora": "sudo dnf install syncthing && systemctl --user enable --now syncthing",
    "arch": "sudo pacman -S syncthing && systemctl --user enable --now syncthing",
    "steamos": f"flatpak install flathub {SYNCTHINGY_APP} "
    "(the read-only root rules out system packages)",
    "any": "install Syncthing from https://syncthing.net/downloads/ and start it",
}
SYNCTHING_START_HINTS: HintTable = {
    "macos": "brew services start syncthing (or open the Syncthing app)",
    "steamos": "start SyncThingy (or `systemctl --user start syncthing` if you use the service)",
    "linux": "systemctl --user enable --now syncthing",
    "any": "start Syncthing",
}


def hint_for(table: HintTable, family: str) -> str:
    """The hint for `family`; a specific Linux family falls back to "linux", then "any"."""
    if family in table:
        return table[family]
    if family not in ("macos", "other") and "linux" in table:
        return table["linux"]
    return table["any"]
