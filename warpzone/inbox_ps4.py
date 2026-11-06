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

An update (patch) package becomes `library/roms/ps4/games/<CUSAxxxxx>-UPDATE/`, which shadPS4
overlays on the base game file by file; the base game must already be in the library. A newer
update replaces the one in the library (the only case where inbox replaces anything: the
original package of the old one was filed in `inbox/_done` by its own run), the same version
is kept and an older one goes to review. A DLC package becomes
`library/roms/ps4/dlc/<CUSAxxxxx>/<entitlement label>/`, the layout of shadPS4's
`addon_install_dir`, for a game that is in the library: the game named by the item folder
when that folder is called after a title id (`inbox/CUSA03173/`), else the DLC's own title. A
game already in the library is reported and never overwritten. Without the tool the package is
placed as it is, as before.

The decisions (`pkg_kind`, `parse_header`, `category_kind`, `entitlement_label`,
`app_version_key`, `update_action`, `item_title_id`, `shortcut_name`, `desktop_text`) are pure
functions of bytes or text; `install` touches the disk. See development/design/inbox.md.
"""

from __future__ import annotations

import os
import re
import shutil
import struct
import tempfile
import unicodedata
from dataclasses import dataclass
from pathlib import Path

from warpzone import inbox_core
from warpzone.inbox_core import Context, ItemFailure, UnitResult, whitelist_line
from warpzone.sfo import MAX_SFO_BYTES, parse_sfo

GAMES = "ps4/games"  # inside library/roms/
DLC = "ps4/dlc"
UPDATE_SUFFIX = "-UPDATE"  # shadPS4 overlays `<game folder>-UPDATE` on the game folder
SHORTCUTS = "ps4/shortcuts"
# The consoles are SteamOS devices whose library is ~/Emulation. A `.desktop` Exec line is
# not expanded by the launcher, so the home folder is written out.
DEVICE_LIBRARY = "/home/deck/Emulation"
EXEC_BINARY = "shadps4"
CONTENT_ID_AT, CONTENT_ID_END = 0x40, 0x64
TYPE_AT, FLAGS_AT, HEADER_END = 0x74, 0x78, 0x7C
PFS_SIZE_AT = 0x418  # u64, big-endian: 0 for a DLC without data
HEADER_BYTES = PFS_SIZE_AT + 8
GAME_DATA = 0x1A  # content type "GD"
# Content types "AC" (additional content) and "AL" (additional license: a DLC without data).
ADDITIONAL_CONTENT = (0x1B, 0x1C)
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
    return "dlc" if content_type in ADDITIONAL_CONTENT else "other"


@dataclass(frozen=True)
class Header:
    kind: str  # see `pkg_kind`
    content_id: str  # `UP9000-CUSA00900_00-SPEXPANSIONDLC03`
    pfs_size: int | None  # None when the header read stopped before 0x420


def parse_header(head: bytes) -> Header | None:
    """The kind, content id and PFS image size of a package header; None when it is too short
    for the kind."""
    kind = pkg_kind(head)
    if kind is None:
        return None
    raw = head[CONTENT_ID_AT:CONTENT_ID_END].split(b"\x00")[0]
    pfs = struct.unpack_from(">Q", head, PFS_SIZE_AT)[0] if len(head) >= HEADER_BYTES else None
    return Header(kind, raw.decode("ascii", "replace"), pfs)


_LABEL = re.compile(r"[A-Za-z0-9_]+")


def entitlement_label(content_id: str) -> str | None:
    """The part of a content id after its second `-` (the DLC's folder name under its title),
    or None when it is missing or not safe as a folder name."""
    parts = content_id.split("-", 2)
    if len(parts) != 3 or not _LABEL.fullmatch(parts[2]):
        return None
    return parts[2]


def app_version_key(text: object) -> tuple[int, ...] | None:
    """`"01.09"` as `(1, 9)`, to compare versions numerically per dot part; None when it is
    not dot-separated digits."""
    if not isinstance(text, str) or not text:
        return None
    parts = text.split(".")
    if not all(p.isascii() and p.isdigit() for p in parts):
        return None
    return tuple(int(p) for p in parts)


_TITLE_ID = re.compile(r"CUSA\d{5}")


def item_title_id(name: str, is_dir: bool) -> str | None:
    """The title id an inbox item names: the name of a folder that is exactly a `CUSAnnnnn`
    (the way to say which game a DLC belongs to), else None."""
    return name if is_dir and _TITLE_ID.fullmatch(name) else None


def update_action(new: object, old: str | None) -> str:
    """What to do with an update of APP_VER `new` when the library holds `old` (None: no update
    there): "place", "already" (same version), "newer" (replace), "older", or "bad_new" /
    "bad_old" when a version cannot be read."""
    new_key = app_version_key(new)
    if new_key is None:
        return "bad_new"
    if old is None:
        return "place"
    old_key = app_version_key(old)
    if old_key is None:
        return "bad_old"
    width = max(len(new_key), len(old_key))
    a = new_key + (0,) * (width - len(new_key))
    b = old_key + (0,) * (width - len(old_key))
    return "already" if a == b else ("newer" if a > b else "older")


def category_kind(category: object) -> str:
    """ "game" (`gd`, `gdo` and the other game categories), "update" (`gp`), "dlc" (`ac`) or
    "other" for the CATEGORY of a param.sfo."""
    if not isinstance(category, str):
        return "other"
    if category.startswith("gd"):
        return "game"
    if category.startswith("ac"):
        return "dlc"
    return "update" if category == "gp" else "other"


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


_NOUN = {"game": "a game", "update": "an update (patch)", "dlc": "DLC", "other": "not a game"}


def _fail_kind(rel: str) -> str:
    return f"{rel}: this package is not a game, update or DLC (unexpected content type)"


def _extract(tool: str, pkg: Path, out: Path, *, empty_pfs: bool = False) -> None:
    """Unpack with orbis-pkg-util. A DLC without data (empty PFS in the header) makes it exit 1
    after writing `sce_sys`; that is accepted when `sce_sys/param.sfo` was written."""
    code, text = inbox_core.run([tool, "extract", "-q", "-o", str(out), str(pkg)])
    if code == 0:
        return
    if empty_pfs and code == 1 and (out / "sce_sys" / "param.sfo").is_file():
        return
    raise ItemFailure(f"{pkg.name}: orbis-pkg-util could not extract it ({inbox_core.tail(text)})")


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
            inbox_core.copy_verified(Path(base) / n, target / n)


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


@dataclass
class _Job:
    ctx: Context
    path: Path
    rel: str
    title_id: str
    tool: str
    header: Header
    res: UnitResult
    item_title: str | None = None  # the title id the inbox item folder is named after
    work: Path | None = None

    @property
    def games(self) -> Path:
        return self.ctx.roms.joinpath(*GAMES.split("/"))

    def need_game(self, what: str, title_id: str, advice: str) -> None:
        if not (self.games / title_id).is_dir():
            raise ItemFailure(
                f"{self.rel}: this {what} is for {title_id}, which is not in the library; " + advice
            )

    def unpack(self, *, empty_pfs: bool = False) -> Path:
        """Extract the package into the local work folder and return the output folder."""
        self.ctx.work.mkdir(parents=True, exist_ok=True)
        need = self.path.stat().st_size
        if shutil.disk_usage(self.ctx.work).free < need * 1.1:
            raise ItemFailure(
                f"{self.rel}: not enough free space to unpack it ({need // 2**20} MiB)"
            )
        self.work = Path(tempfile.mkdtemp(prefix="ps4-", dir=self.ctx.work))
        out = self.work / "game"
        _extract(self.tool, self.path, out, empty_pfs=empty_pfs)
        return out

    def checked_sfo(self, out: Path, expected: str) -> dict[str, str | int]:
        """The param.sfo of the extracted package, after the checks every kind shares: it
        exists, the tree holds no link, the title id and the category agree with the header."""
        sfo_path = out / "sce_sys" / "param.sfo"
        if not sfo_path.is_file():
            raise ItemFailure(f"{self.rel}: the extracted package has no sce_sys/param.sfo")
        _check_tree(out, self.rel)
        values = read_sfo(sfo_path)
        if values.get("TITLE_ID") != self.title_id:
            raise ItemFailure(
                f"{self.rel}: its param.sfo names {values.get('TITLE_ID') or 'no title id'}, but "
                f"the package header says {self.title_id}"
            )
        found = category_kind(values.get("CATEGORY"))
        if found != expected:
            raise ItemFailure(
                f"{self.rel}: its param.sfo has CATEGORY {values.get('CATEGORY') or 'none'} "
                f"({_NOUN[found]}), but the package header says {_NOUN[expected]}"
            )
        return values

    def cleanup(self) -> None:
        if self.work is not None:
            shutil.rmtree(self.work, ignore_errors=True)


def _game(job: _Job) -> None:
    ctx, res, rel, title_id = job.ctx, job.res, job.rel, job.title_id
    res.lines.append(whitelist_line(GAMES, title_id))
    dest = job.games / title_id
    if dest.exists():
        _existing(ctx, dest, title_id, res)
        return
    if not ctx.opts.apply:
        res.notes.append(
            "would extract it with orbis-pkg-util; the shortcut is named after the title "
            "in its param.sfo, which is known once extracted"
        )
        return
    out = job.unpack()
    if not (out / "eboot.bin").is_file():
        raise ItemFailure(f"{rel}: the extracted package has no eboot.bin")
    values = job.checked_sfo(out, "game")
    title = values.get("TITLE")
    title = title if isinstance(title, str) else ""
    _place_folder(out, dest)
    _shortcut(ctx, title, title_id, res)


def _library_version(folder: Path) -> str:
    """APP_VER of the update in `folder`; empty when it cannot be read."""
    version = read_sfo(folder / "sce_sys" / "param.sfo").get("APP_VER")
    return version if isinstance(version, str) else ""


def _replace_folder(src: Path, dest: Path, old_version: str) -> None:
    """Swap `dest` for a copy of `src`, leaving either the old or the new complete folder.

    The copy goes to `.part` first; right before acting, the library copy must still have the
    version that was compared. Then `dest` is renamed to `.old`, `.part` into place, and `.old`
    deleted. A rename that fails puts the old folder back."""
    part = dest.with_name(dest.name + ".part")
    old = dest.with_name(dest.name + ".old")
    for stale in (part, old):
        if stale.exists():
            shutil.rmtree(stale)
    try:
        _copy_tree(src, part)
        if _library_version(dest) != old_version:
            raise ItemFailure(
                f"{dest.name}: changed in the library while the new one was copied, left as it is"
            )
        os.rename(dest, old)
        try:
            os.rename(part, dest)
        except OSError:
            os.rename(old, dest)
            raise
        shutil.rmtree(old, ignore_errors=True)
    finally:
        if part.exists():
            shutil.rmtree(part, ignore_errors=True)


def _update(job: _Job) -> None:
    ctx, res, rel, title_id = job.ctx, job.res, job.rel, job.title_id
    apply = ctx.opts.apply
    job.need_game("update", title_id, "install the game first")
    name = f"{title_id}{UPDATE_SUFFIX}"
    res.entry = name
    res.lines.append(whitelist_line(GAMES, name))
    dest = job.games / name
    left = dest.with_name(name + ".old")
    if apply and not dest.exists() and left.is_dir():
        os.rename(left, dest)  # a replacement stopped between its two renames
        res.notes.append(f"restored {name} from {left.name}: an earlier replacement stopped")
    old: str | None = None
    if dest.exists():
        old = _library_version(dest)
        if not old:
            raise ItemFailure(
                f"{rel}: the update in the library has no readable APP_VER in its "
                "sce_sys/param.sfo, so the new one cannot be compared; check it by hand"
            )
        if not apply:
            res.notes.append(
                f"the library has this update at v{old}; would extract it and compare its "
                "APP_VER: newer replaces the library copy, the same version is kept, older "
                "goes to review"
            )
            return
    elif not apply:
        res.notes.append("would extract it with orbis-pkg-util")
        return
    out = job.unpack()
    values = job.checked_sfo(out, "update")
    new = values.get("APP_VER")
    action = update_action(new, old)
    if action == "bad_new":
        raise ItemFailure(f"{rel}: its param.sfo has no readable APP_VER")
    if action == "older":
        raise ItemFailure(f"{rel}: v{new} is older than the update in the library, v{old}")
    if action == "already":
        res.outcome = "already"
        res.notes.append(f"kept as it is: the update in the library is the same version, v{old}")
    elif action == "newer":
        assert old is not None
        _replace_folder(out, dest, old)
        res.notes.append(f"replaced the update v{old} with v{new}")
    else:
        _place_folder(out, dest)


def _dlc(job: _Job) -> None:
    ctx, res, rel, own = job.ctx, job.res, job.rel, job.title_id
    content_id = job.header.content_id
    label = entitlement_label(content_id)
    if label is None:
        raise ItemFailure(
            f"{rel}: its content id ({content_id or 'missing'}) has no usable entitlement "
            "label (the part after the second '-'), so the DLC cannot be filed"
        )
    target = job.item_title or own
    job.need_game(
        "DLC",
        target,
        "install that game first, or drop the DLC inside an inbox folder named after the game "
        "it belongs to (inbox/CUSAnnnnn/)",
    )
    if target != own:
        res.notes.append(f"filed for {target}: the inbox folder is named after that game")
    res.entry = f"{target}/{label}"
    res.folder = f"roms/{DLC}"
    res.lines.append(whitelist_line(DLC, target))
    dest = ctx.roms.joinpath(*DLC.split("/")) / target / label
    if dest.exists():
        res.outcome = "already"
        res.notes.append("kept as it is: the package is not compared with the library copy")
        return
    if not ctx.opts.apply:
        res.notes.append("would extract it with orbis-pkg-util")
        return
    out = job.unpack(empty_pfs=job.header.pfs_size == 0)
    values = job.checked_sfo(out, "dlc")
    if values.get("CONTENT_ID") != content_id:
        raise ItemFailure(
            f"{rel}: its param.sfo CONTENT_ID ({values.get('CONTENT_ID') or 'none'}) differs "
            f"from the package header ({content_id})"
        )
    _place_folder(out, dest)


_HANDLERS = {"game": _game, "update": _update, "dlc": _dlc}


def install(
    ctx: Context,
    path: Path,
    rel: str,
    title_id: str,
    tool: str,
    item_title: str | None = None,
) -> UnitResult:
    """File one recognised PS4 package: a game as an extracted folder plus a shortcut, an
    update as `<CUSA>-UPDATE`, a DLC under `dlc/<CUSA>/<label>`. `item_title` is the title id
    the inbox item folder is named after (`item_title_id`); a DLC is filed for that game."""
    res = UnitResult(
        "ps4", title_id, "placed" if ctx.opts.apply else "planned", folder=f"roms/{GAMES}"
    )
    res.notes.append(f"detected: PS4 package: {title_id}")
    job: _Job | None = None
    try:
        with path.open("rb") as f:
            header = parse_header(f.read(HEADER_BYTES)) or Header("other", "", None)
        job = _Job(ctx, path, rel, title_id, tool, header, res, item_title)
        handler = _HANDLERS.get(header.kind)
        if handler is None:
            raise ItemFailure(_fail_kind(rel))
        handler(job)
    except ItemFailure as exc:
        res.outcome, res.reason = "review", str(exc)
    except OSError as exc:
        res.outcome, res.reason = "review", f"{rel}: {exc.strerror or exc}"
    finally:
        if job is not None:
            job.cleanup()
    if res.outcome == "review":
        res.lines.clear()
    return res
