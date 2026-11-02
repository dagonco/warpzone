"""PS4 packages for `warpzone inbox`: unpack a game into a folder shadPS4 can run.

Current shadPS4 has no package installer. It runs extracted game folders found in its
install directories, and ES-DE lists only the `.desktop` shortcuts of `roms/ps4/shortcuts`,
which EmuDeck's `shadps4.sh` launcher reads. So, when `orbis-pkg-util` is configured
(`[inbox] orbis_pkg_util`, or on PATH), a base-game `.pkg` becomes:

- `library/roms/ps4/games/<CUSAxxxxx>/`: the folder `orbis-pkg-util extract` produces
  (`eboot.bin`, `sce_sys/param.sfo` and the game data), copied beside the final name and
  renamed, so the destination never holds a partial game. Consoles point shadPS4's
  `installDirs` at it (`[shadps4]` in the profile, set by `provision device`).
- `library/roms/ps4/shortcuts/<Title>.desktop`: the entry ES-DE lists. The launcher takes
  the one quoted string of its `Exec=` line as the game, and runs
  `Shadps4-qt.AppImage -d -g <path>`, which boots the version chosen in the Qt launcher.
  The path is absolute (nothing expands `~` or `$HOME` there), under the SteamOS home.

An update (patch) or DLC package is sent to review: it is not merged into the game. A game
already in the library is reported and never overwritten. Without the tool the package is
placed as it is, as before.

The decisions (`pkg_kind`, `category_kind`, `shortcut_name`, `desktop_text`) are pure
functions of bytes or text; `install` touches the disk. See docs/design.md.
"""

from __future__ import annotations

import os
import shutil
import struct
import tempfile
import unicodedata
from pathlib import Path

from warpzone import inbox
from warpzone.inbox import Context, ItemFailure, UnitResult, whitelist_line
from warpzone.sfo import MAX_SFO_BYTES, parse_sfo

GAMES = "ps4/games"  # inside library/roms/
SHORTCUTS = "ps4/shortcuts"
# The consoles are SteamOS devices whose library is ~/Emulation. A `.desktop` Exec line is
# not expanded by the launcher, so the home folder is written out.
DEVICE_LIBRARY = "/home/deck/Emulation"
EXEC_BINARY = "shadps4"
TYPE_AT, FLAGS_AT, HEADER_END = 0x74, 0x78, 0x7C
GAME_DATA = 0x1A  # content type "GD"
ADDITIONAL_CONTENT = 0x1B  # content type "AC" (DLC)
# Content flags that mark a patch package: FIRST_PATCH, and SUBSEQUENT / DELTA / CUMULATIVE
# patches (0x40000000 is set in all three).
PATCH_FLAGS = 0x100000 | 0x40000000
NAME_LIMIT = 100
_DROP = str.maketrans("", "", "\u2122\u00ae\u00a9\u2120'\u2018\u2019\"\u201c\u201d`")
_DASHES = str.maketrans({"\u2013": "-", "\u2014": "-", "\u2212": "-", ":": " -"})
_KEEP = frozenset(" -_.,()[]!+#")
NO_TOOL_NOTE = (
    "orbis-pkg-util is not configured ([inbox] orbis_pkg_util, or put it on PATH), so the .pkg "
    "was placed as it is; shadPS4 cannot run it until it is extracted"
)


# ------------------------------------------------------------------ pure decisions


def pkg_kind(head: bytes) -> str | None:
    """ "game", "update", "dlc" or "other" from the content type and flags of a package
    header (big-endian, at 0x74 and 0x78); None when the header is too short."""
    if len(head) < HEADER_END:
        return None
    content_type = struct.unpack_from(">I", head, TYPE_AT)[0]
    flags = struct.unpack_from(">I", head, FLAGS_AT)[0]
    if content_type == GAME_DATA:
        return "update" if flags & PATCH_FLAGS else "game"
    return "dlc" if content_type == ADDITIONAL_CONTENT else "other"


def category_kind(category: object) -> str:
    """ "game" (`gd`, `gdo` and the other game categories), "update" (`gp`), "dlc" (`ac`) or
    "other" for the CATEGORY of a param.sfo."""
    if not isinstance(category, str):
        return "other"
    if category.startswith("gd"):
        return "game"
    return {"gp": "update", "ac": "dlc"}.get(category, "other")


def _clean(text: str) -> str:
    return " ".join("".join(c for c in text if unicodedata.category(c)[0] != "C").split())


def shortcut_name(title: str, title_id: str) -> str:
    """A file name (without `.desktop`) for a game title: trademark signs and quotes are
    dropped, `:` becomes ` -`, and anything but letters, digits, marks and `-_.,()[]!+#` is
    removed. An empty result is the title id."""
    text = title.translate(_DROP).translate(_DASHES)
    text = "".join(c for c in text if c in _KEEP or unicodedata.category(c)[0] in ("L", "N", "M"))
    text = _clean(text)[:NAME_LIMIT].strip(" .")
    return text or title_id


def exec_path(title_id: str) -> str:
    return f"{DEVICE_LIBRARY}/roms/{GAMES}/{title_id}/eboot.bin"


def exec_line(title_id: str) -> str:
    return f'Exec={EXEC_BINARY} "{exec_path(title_id)}"'


def desktop_text(title: str, title_id: str) -> str:
    """The shortcut ES-DE lists. `Exec=` carries exactly one quoted string, the path to the
    game's `eboot.bin`; EmuDeck's launcher hands that to shadPS4's Qt launcher."""
    name = _clean(title) or title_id
    return f"[Desktop Entry]\nType=Application\nName={name}\n{exec_line(title_id)}\n"


# ------------------------------------------------------------------ disk


def _fail_kind(rel: str, kind: str, title_id: str) -> str:
    if kind == "update":
        return (
            f"{rel}: this is an update (patch) for {title_id}; updates are not merged into the "
            "extracted game yet, so apply it by hand"
        )
    if kind == "dlc":
        return (
            f"{rel}: this is DLC (additional content) for {title_id}; DLC is not installed by "
            "inbox yet"
        )
    return f"{rel}: this package is not a game (unexpected content type)"


def _extract(tool: str, pkg: Path, out: Path) -> None:
    code, text = inbox._run([tool, "extract", "-q", "-o", str(out), str(pkg)])
    if code != 0:
        raise ItemFailure(f"{pkg.name}: orbis-pkg-util could not extract it ({inbox._tail(text)})")


def _check_tree(root: Path, rel: str) -> int:
    """Number of files in the extracted folder; refuses anything but plain files and folders."""
    count = 0
    for base, dirs, names in os.walk(root):
        for name in [*dirs, *names]:
            if os.path.islink(os.path.join(base, name)):
                raise ItemFailure(f"{rel}: the extracted package holds a symbolic link, refused")
        count += len(names)
    return count


def read_sfo(path: Path) -> dict[str, str | int]:
    try:
        if path.stat().st_size > MAX_SFO_BYTES:
            return {}
        return parse_sfo(path.read_bytes())
    except OSError:
        return {}


def _copy_tree(src: Path, dest: Path) -> None:
    dest.mkdir()
    for base, dirs, names in os.walk(src):
        target = dest / Path(base).relative_to(src)
        for d in sorted(dirs):
            (target / d).mkdir()
        for n in sorted(names):
            inbox.copy_verified(Path(base) / n, target / n)


def _place_folder(src: Path, dest: Path) -> None:
    part = dest.with_name(dest.name + ".part")
    if part.exists():
        shutil.rmtree(part)
    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        _copy_tree(src, part)
        if dest.exists():
            raise ItemFailure(f"{dest.name}: appeared in the library while copying")
        os.rename(part, dest)
    finally:
        if part.exists():
            shutil.rmtree(part, ignore_errors=True)


def _write_text(dest: Path, text: str) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_name(dest.name + ".part")
    try:
        part.write_text(text, encoding="utf-8")
        if dest.exists():
            raise ItemFailure(f"{dest.name}: appeared in the library while writing")
        os.rename(part, dest)
    finally:
        part.unlink(missing_ok=True)


def _shortcut(ctx: Context, title: str, title_id: str, res: UnitResult) -> None:
    """Make sure `roms/ps4/shortcuts/` has an entry for the game, never replacing a file.

    A file with the title's name that already runs this game is kept as it is; one that
    runs another game (the same title in another region) gets the title id in its name."""
    folder = ctx.roms.joinpath(*SHORTCUTS.split("/"))
    text = desktop_text(title, title_id)
    base = shortcut_name(title, title_id)
    for name in (f"{base}.desktop", f"{base} [{title_id}].desktop"):
        path = folder / name
        shown = f"library/roms/{SHORTCUTS}/{name}"
        res.lines.append(whitelist_line(SHORTCUTS, name))
        if path.exists():
            if exec_line(title_id) in path.read_text(encoding="utf-8", errors="replace"):
                res.notes.append(f"shortcut: {shown} (already there)")
                return
            res.lines.pop()
            continue
        if not ctx.opts.apply:
            res.notes.append(f"shortcut: {shown} (would be written)")
        else:
            _write_text(path, text)
            res.notes.append(f"shortcut: {shown}")
        return
    raise ItemFailure(f"{base}.desktop: a shortcut for another game is already there")


def _existing(ctx: Context, dest: Path, title_id: str, res: UnitResult) -> None:
    """The game is in the library: leave it alone, but make sure it has its shortcut."""
    res.outcome = "already"
    res.notes.append("kept as it is: the package is not compared with the library copy")
    values = read_sfo(dest / "sce_sys" / "param.sfo")
    title = values.get("TITLE")
    if isinstance(title, str) and title:
        _shortcut(ctx, title, title_id, res)
    else:
        res.notes.append("no readable sce_sys/param.sfo in the library copy, shortcut not checked")


def install(ctx: Context, path: Path, rel: str, title_id: str, tool: str) -> UnitResult:
    """File one recognised PS4 package as an extracted game plus a shortcut."""
    apply = ctx.opts.apply
    res = UnitResult("ps4", title_id, "placed" if apply else "planned", folder=f"roms/{GAMES}")
    res.notes.append(f"detected: PS4 package: {title_id}")
    res.lines.append(whitelist_line(GAMES, title_id))
    dest = ctx.roms.joinpath(*GAMES.split("/")) / title_id
    work: Path | None = None
    try:
        with path.open("rb") as f:
            kind = pkg_kind(f.read(HEADER_END))
        if kind != "game":
            raise ItemFailure(_fail_kind(rel, kind or "other", title_id))
        if dest.exists():
            _existing(ctx, dest, title_id, res)
            return res
        if not apply:
            res.notes.append(
                "would extract it with orbis-pkg-util; the shortcut is named after the title "
                "in its param.sfo, which is known once extracted"
            )
            return res
        ctx.work.mkdir(parents=True, exist_ok=True)
        need = path.stat().st_size
        if shutil.disk_usage(ctx.work).free < need * 1.1:
            raise ItemFailure(f"{rel}: not enough free space to unpack it ({need // 2**20} MiB)")
        work = Path(tempfile.mkdtemp(prefix="ps4-", dir=ctx.work))
        out = work / "game"
        _extract(tool, path, out)
        if not (out / "eboot.bin").is_file():
            raise ItemFailure(f"{rel}: the extracted package has no eboot.bin")
        sfo_path = out / "sce_sys" / "param.sfo"
        if not sfo_path.is_file():
            raise ItemFailure(f"{rel}: the extracted package has no sce_sys/param.sfo")
        _check_tree(out, rel)
        values = read_sfo(sfo_path)
        if values.get("TITLE_ID") != title_id:
            raise ItemFailure(
                f"{rel}: its param.sfo names {values.get('TITLE_ID') or 'no title id'}, but the "
                f"package header says {title_id}"
            )
        sfo_kind = category_kind(values.get("CATEGORY"))
        if sfo_kind != "game":
            raise ItemFailure(_fail_kind(rel, sfo_kind, title_id))
        title = values.get("TITLE")
        title = title if isinstance(title, str) else ""
        _place_folder(out, dest)
        _shortcut(ctx, title, title_id, res)
    except ItemFailure as exc:
        res.outcome, res.reason = "review", str(exc)
    except OSError as exc:
        res.outcome, res.reason = "review", f"{rel}: {exc.strerror or exc}"
    finally:
        if work is not None:
            shutil.rmtree(work, ignore_errors=True)
    if res.outcome == "review":
        res.lines.clear()
    return res
