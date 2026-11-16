"""The disk side of deleting a game from the library UI: what a plan needs to know (the parts,
the media, the hub gamelist, the arcade sets), the moves into `trash/` and back with their
rollback, the manifest and the pending cleanups files, the gamelist edits, the listing and the
purge. The decisions are in `trash.py` (pure); `trash_run.py` takes the locks and drives this.
See "Deleting a game" in development/design/library-ui.md.

Nothing here follows a symbolic link, and every path is looked at again with `lstat` right
before it is moved or removed. Nothing is ever copied and then deleted: a move is a `rename`.
"""

from __future__ import annotations

import errno
import os
import posixpath
import shutil
import stat
import threading
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from warpzone import artwork, catalog, gamelist, inbox_mame, safefile, trash
from warpzone.config import InboxSettings
from warpzone.errors import WarpzoneError
from warpzone.library_api import Library
from warpzone.trash import (
    F_ABSENT,
    F_DIR,
    F_FILE,
    F_LINK,
    F_OTHER,
    Arcade,
    Cleanup,
    Facts,
    Found,
    Listed,
    Manifest,
    MediaFound,
)

CATALOG_DIR = "catalog"
BACKUPS = "tools/logs/gamelist-backups"
MANIFEST_LIMIT = 16 * 1024 * 1024  # a manifest or a cleanups file over this is damaged
TRIES = 3  # a gamelist that keeps changing under a write is given up after this many

Rename = Callable[[Path, Path], None]


def _rename(src: Path, dst: Path) -> None:
    """The one place a deletion or a restore moves anything: `os.rename`, which fails (rather
    than copying) when the two places are on different filesystems or the target exists as a
    folder. A test replaces it to make a move fail."""
    os.rename(src, dst)


# --------------------------------------------------------------------------- places


def trash_path(root: Path) -> Path:
    return root / trash.TRASH_DIR


def entry_path(root: Path, entry: str) -> Path:
    return trash_path(root) / entry


def cleanups_path(root: Path) -> Path:
    return root / CATALOG_DIR / trash.CLEANUPS_FILE


def _reason(exc: OSError) -> str:
    return exc.strerror or type(exc).__name__


def link_above(root: Path, rel: str) -> bool:
    """Whether a folder between `root` and the last component of `rel` is a symbolic link, or
    is not there as a folder. A path below a link would be moved from, or into, another place."""
    here = root
    for part in rel.split("/")[:-1]:
        here = here / part
        try:
            info = os.lstat(here)
        except FileNotFoundError:
            return False  # nothing there: the path itself is absent
        except OSError:
            return True
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            return True
    return False


def lstat_found(root: Path, rel: str) -> Found:
    """What is at `root/rel`, without following a link. A folder is walked for its total size
    and its count of files (a link below it is counted and never followed)."""
    if link_above(root, rel):
        return Found(F_LINK)
    path = root / rel
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return Found(F_ABSENT)
    except OSError:
        return Found(F_OTHER)
    if stat.S_ISLNK(info.st_mode):
        return Found(F_LINK)
    if stat.S_ISREG(info.st_mode):
        return Found(F_FILE, info.st_size, info.st_mtime_ns)
    if stat.S_ISDIR(info.st_mode):
        try:
            size, files = tree_totals(path)
        except OSError:
            return Found(F_OTHER)
        return Found(F_DIR, size, info.st_mtime_ns, files)
    return Found(F_OTHER)


def tree_totals(path: Path) -> tuple[int, int]:
    """(bytes, files) below a folder: regular files add their size, every entry that is not a
    folder counts as a file, and a link to a folder is not entered."""
    size = files = 0
    stack = [path]
    while stack:
        with os.scandir(stack.pop()) as it:
            for entry in it:
                if entry.is_dir(follow_symlinks=False):
                    stack.append(Path(entry.path))
                    continue
                files += 1
                if entry.is_file(follow_symlinks=False):
                    size += entry.stat(follow_symlinks=False).st_size
    return size, files


# --------------------------------------------------------------------------- what a plan reads


def list_media(
    root: Path, system: str, stems: frozenset[str]
) -> tuple[list[MediaFound], list[str]]:
    """The entries of `frontend/media/<system>/<kind>/` whose name without its last extension
    is one of `stems`, for every kind folder that exists, and the folders left alone because
    they are links. Only the folders a stem can be in are listed."""
    base = root / "frontend" / "media" / system
    shown = f"frontend/media/{system}"
    found: list[MediaFound] = []
    links: list[str] = []
    if link_above(root, f"frontend/media/{system}/x"):
        return found, [shown]
    try:
        kinds = sorted(os.scandir(base), key=lambda e: e.name)
    except FileNotFoundError:
        return found, links
    except OSError:
        return found, [shown]
    for kind in kinds:
        if kind.is_symlink():
            links.append(f"{shown}/{kind.name}")
            continue
        if not kind.is_dir(follow_symlinks=False):
            continue
        for sub in trash.media_dirs(stems):
            folder = Path(kind.path) / sub if sub else Path(kind.path)
            if sub and _link_inside(Path(kind.path), sub):
                links.append(f"{shown}/{kind.name}/{sub}")
                continue
            _list_folder(folder, f"{system}/{kind.name}", sub, stems, found)
    return found, links


def _link_inside(base: Path, sub: str) -> bool:
    here = base
    for part in sub.split("/"):
        here = here / part
        try:
            if stat.S_ISLNK(os.lstat(here).st_mode):
                return True
        except OSError:
            return False
    return False


def _list_folder(
    folder: Path, head: str, sub: str, stems: frozenset[str], found: list[MediaFound]
) -> None:
    try:
        entries = list(os.scandir(folder))
    except OSError:
        return  # not there (a stem's folder this kind has none of), or unreadable
    for entry in entries:
        rel = f"{sub}/{entry.name}" if sub else entry.name
        stem, ext = posixpath.splitext(rel)
        if not ext or stem not in stems:
            continue
        path = f"{head}/{rel}"
        try:
            if entry.is_symlink():
                found.append(MediaFound(path, F_LINK))
            elif entry.is_file(follow_symlinks=False):
                info = entry.stat(follow_symlinks=False)
                found.append(MediaFound(path, F_FILE, info.st_size, info.st_mtime_ns))
            else:
                found.append(MediaFound(path, F_OTHER))
        except OSError:
            found.append(MediaFound(path, F_OTHER))


def read_gamelist(root: Path, system: str) -> tuple[str | None, str | None]:
    """(the hub gamelist's text, why it cannot be changed). A file that is not there is
    `(None, None)`; a link, an unreadable or oversize file or one that is not UTF-8 is
    `(None, reason)`."""
    path = root / trash.gamelist_path(system)
    if link_above(root, trash.gamelist_path(system)):
        return None, "it is behind a link"
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return None, None
    except OSError as exc:
        return None, _reason(exc)
    if not stat.S_ISREG(info.st_mode):
        return None, "it is not a regular file"
    try:
        return artwork.read_gamelist(path).decode("utf-8"), None
    except (OSError, UnicodeDecodeError, artwork.ArtworkError) as exc:
        return None, getattr(exc, "strerror", None) or str(exc)


def trash_problem(root: Path, *, create: bool) -> str | None:
    """Why `trash/` cannot take a move, or None: it must be a real folder (made here when
    `create`) on the filesystem of `library/` and of `frontend/media/`. A dry run (`create`
    false) asks the same of the hub root where `trash/` would be made."""
    trash_dir = trash_path(root)
    try:
        info = os.lstat(trash_dir)
    except FileNotFoundError:
        if not create:
            if not os.access(root, os.W_OK):
                return "trash/ cannot be created in the hub root (it is not writable)"
            return _same_device(root, root)
        try:
            os.mkdir(trash_dir, 0o755)
        except OSError as exc:
            return f"trash/ cannot be created ({_reason(exc)})"
        return _same_device(root, trash_dir)
    except OSError as exc:
        return f"trash/ cannot be read ({_reason(exc)})"
    if not stat.S_ISDIR(info.st_mode):
        return "trash/ is not a folder"
    if not os.access(trash_dir, os.W_OK):
        return "trash/ is not writable"
    return _same_device(root, trash_dir)


def _same_device(root: Path, place: Path) -> str | None:
    try:
        here = os.stat(place).st_dev
        for name in ("library", "frontend/media"):
            target = root / name
            if target.exists() and os.stat(target).st_dev != here:
                return f"trash/ is on another filesystem than {name}/: a move would be a copy"
    except OSError as exc:
        return f"trash/ cannot be checked ({_reason(exc)})"
    return None


class CleanupsReadError(WarpzoneError):
    """The pending cleanups file exists and cannot be read; the message names it."""


def read_cleanups(root: Path) -> tuple[list[Cleanup], safefile.FileStamp | None]:
    """The pending cleanups and the stamp the file had when it was read (None: no file).
    `CleanupsReadError` when it is there and cannot be read."""
    path = cleanups_path(root)
    shown = f"{CATALOG_DIR}/{trash.CLEANUPS_FILE}"
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return [], None
    except OSError as exc:
        raise CleanupsReadError(f"{shown} could not be read ({_reason(exc)})") from exc
    if not stat.S_ISREG(info.st_mode):
        raise CleanupsReadError(f"{shown} could not be read (it is not a regular file)")
    if info.st_size > MANIFEST_LIMIT:
        raise CleanupsReadError(f"{shown} could not be read (it is over 16 MiB)")
    try:
        stamp = safefile.stamp(path)
        data = path.read_bytes()
        items = trash.parse_cleanups(data)
    except OSError as exc:
        raise CleanupsReadError(f"{shown} could not be read ({_reason(exc)})") from exc
    except trash.CleanupsError as exc:
        raise CleanupsReadError(f"{shown} could not be read ({exc})") from exc
    return items, stamp


def write_cleanups(root: Path, items: Sequence[Cleanup], stamp: safefile.FileStamp | None) -> None:
    """Write the pending cleanups (the file goes when none is left), refused when the file
    changed since `stamp`. `OSError` when it cannot be written. The caller holds the server's
    write lock."""
    path = cleanups_path(root)
    if not items:
        if stamp is not None:
            path.unlink(missing_ok=True)
        return
    if not safefile.atomic_write(path, trash.encode_cleanups(items), expect=stamp):
        raise OSError(f"{CATALOG_DIR}/{trash.CLEANUPS_FILE} changed while it was being written")


class ArcadeCache:
    """The arcade set databases of `[inbox]`, read for a deletion and kept while the file's
    size and modification time hold. A DAT that is not configured or cannot be read is left out
    (and said once to `log`)."""

    def __init__(
        self,
        settings: InboxSettings,
        log: Callable[[str], None] = lambda text: None,
        cache_dir: Path | None = None,
    ) -> None:
        self.settings = settings
        self.log = log
        self._cache_dir = cache_dir
        self._held: dict[str, tuple[int, int, inbox_mame.SetDb]] = {}
        self._said: set[str] = set()
        self._lock = threading.Lock()

    def load(self) -> Arcade:
        dbs: list[inbox_mame.SetDb] = []
        with self._lock:
            for key, label, emulator in inbox_mame.SOURCES:
                value = getattr(self.settings, key)
                if not value:
                    continue
                db = self._one(key, Path(value), label, emulator)
                if db is not None:
                    dbs.append(db)
        return Arcade(tuple(dbs))

    def _one(
        self, key: str, path: Path, label: str | None, emulator: str | None
    ) -> inbox_mame.SetDb | None:
        try:
            info = path.stat()
            held = self._held.get(key)
            if held is not None and held[:2] == (info.st_size, info.st_mtime_ns):
                return held[2]
            if self._cache_dir is None:
                from warpzone import inbox  # late: it imports what imports this module

                self._cache_dir = inbox.default_cache_dir()
            machines, _, _ = inbox_mame.load_sets(path, self._cache_dir)
        except (OSError, WarpzoneError) as exc:
            if key not in self._said:
                self._said.add(key)
                self.log(f"warning: [inbox] {key} cannot be read for a deletion ({exc})")
            self._held.pop(key, None)
            return None
        self._said.discard(key)
        shown = label or "MAME"
        db = inbox_mame.SetDb(shown, emulator, machines)
        self._held[key] = (info.st_size, info.st_mtime_ns, db)
        return db


def gather(
    root: Path,
    lib: Library,
    game_id: str,
    *,
    arcade: ArcadeCache | None,
) -> Facts:
    """Everything `trash.plan_delete` reads from the disk for one game. An unknown game gives
    empty facts (the plan refuses it first)."""
    found = lib.by_id.get(game_id)
    if found is None:
        return Facts()
    system, game = found
    parts = {p.path: lstat_found(root / "library", p.path) for p in game.parts}
    others = [g for g in lib.systems.get(system, {}).values() if g.id != game.id]
    media, links = list_media(root, system, trash.own_stems(game, others))
    text, error = read_gamelist(root, system)
    loaded = None
    if system in trash.ARCADE_SYSTEMS:
        loaded = arcade.load() if arcade is not None else Arcade()
    try:
        read_cleanups(root)
        cleanups_problem = None
    except CleanupsReadError as exc:
        cleanups_problem = str(exc)
    return Facts(
        parts,
        media,
        links,
        text,
        error,
        loaded,
        trash_problem(root, create=False),
        cleanups_problem,
    )


# --------------------------------------------------------------------------- moves


@dataclass(frozen=True)
class Move:
    """One path to move, relative to the hub root, and what it must still be right before."""

    path: str
    type: str  # catalog.FILE or catalog.DIR
    size: int | None = None  # a file: the size and the mtime the plan saw
    mtime_ns: int | None = None


def moves_of_plan(plan: trash.DeletePlan) -> list[Move]:
    return [
        *(Move(trash.LIBRARY + f.path, f.type, f.size, f.mtime_ns or None) for f in plan.files),
        *(Move(trash.MEDIA + m.path, catalog.FILE, m.size, m.mtime_ns or None) for m in plan.media),
    ]


def moves_of_manifest(m: Manifest, paths: Sequence[str]) -> list[Move]:
    known = {f.path: f for f in (*m.files, *m.media)}
    return [Move(p, known[p].type) for p in paths if p in known]


class MoveError(Exception):
    """A move that cannot be made. `unsafe` is a path that is no longer what it was (a link, or
    changed since the plan); the rest is an I/O failure. `done` are the paths moved before it."""

    def __init__(self, path: str, why: str, *, unsafe: bool, done: list[str]) -> None:
        super().__init__(f"{path}: {why}")
        self.path = path
        self.why = why
        self.unsafe = unsafe
        self.done = done


def _check_source(base: Path, move: Move, *, strict: bool) -> str | None:
    """Why `base/move.path` is no longer what it was (None when it is): looked at with `lstat`
    right before the move. `strict` also compares a file's size and mtime with the plan's."""
    if link_above(base, move.path):
        return "a folder above it is a link"
    try:
        info = os.lstat(base / move.path)
    except OSError as exc:
        return f"cannot be read ({_reason(exc)})"
    if stat.S_ISLNK(info.st_mode):
        return "is a symbolic link"
    wanted = stat.S_ISREG if move.type == catalog.FILE else stat.S_ISDIR
    if not wanted(info.st_mode):
        return f"is no longer a {move.type}"
    if strict and move.type == catalog.FILE:
        if move.size is not None and info.st_size != move.size:
            return "changed size since the plan"
        if move.mtime_ns is not None and info.st_mtime_ns != move.mtime_ns:
            return "changed since the plan"
    return None


def transfer(
    src: Path,
    dst: Path,
    moves: Sequence[Move],
    *,
    strict: bool,
    rename: Rename | None = None,
) -> list[str]:
    """Move each path from below `src` to the same relative path below `dst`, in order,
    creating the folders there. Right before each move the source is looked at again
    (`_check_source`) and the target must not exist. Returns the paths moved; `MoveError`
    (with what was moved before it) stops at the first one that cannot be."""
    do = rename or _rename
    done: list[str] = []
    for move in moves:
        bad = _check_source(src, move, strict=strict)
        if bad is not None:
            raise MoveError(move.path, bad, unsafe=True, done=done)
        target = dst / move.path
        if link_above(dst, move.path):
            raise MoveError(
                move.path, "a folder at its destination is a link", unsafe=True, done=done
            )
        try:
            if os.path.lexists(target):
                raise FileExistsError(errno.EEXIST, os.strerror(errno.EEXIST))
            target.parent.mkdir(parents=True, exist_ok=True)
            do(src / move.path, target)
        except OSError as exc:
            raise MoveError(move.path, _reason(exc), unsafe=False, done=done) from exc
        done.append(move.path)
    return done


def undo(src: Path, dst: Path, done: Sequence[str], *, rename: Rename | None = None) -> list[str]:
    """Put the paths `transfer(src, dst, ...)` moved back where they were, last first. Returns
    the paths that could not be put back (still below `dst`): one whose place is taken again, or
    whose move fails."""
    do = rename or _rename
    stuck: list[str] = []
    for path in reversed(done):
        source, target = dst / path, src / path
        try:
            if os.path.lexists(target) or not os.path.lexists(source):
                stuck.append(path)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            do(source, target)
        except OSError:
            stuck.append(path)
    return stuck


def discard_entry(root: Path, entry: str) -> bool:
    """Remove an entry folder that holds only its manifest and empty folders. False (and
    nothing removed but the empty folders) when something else is in it."""
    top = entry_path(root, entry)
    try:
        for here, dirs, files in os.walk(top, topdown=False, followlinks=False):
            for name in files:
                if Path(here) == top and name == trash.MANIFEST:
                    os.unlink(Path(here) / name)
            for name in dirs:
                os.rmdir(Path(here) / name)
        os.rmdir(top)
    except OSError:
        return False
    return True


# --------------------------------------------------------------------------- entries


def create_entry(root: Path, entry: str) -> None:
    """Make `trash/<entry>/` (`FileExistsError` when the name is taken)."""
    os.mkdir(entry_path(root, entry), 0o755)


def write_manifest(root: Path, manifest: Manifest) -> None:
    safefile.write_file(
        entry_path(root, manifest.entry) / trash.MANIFEST, trash.encode_manifest(manifest), 0o644
    )


def entry_kind(root: Path, entry: str) -> str:
    """`dir` for a real folder named like an entry, `absent` when there is none, `other` for a
    link or a file of that name (never an entry)."""
    if not trash.valid_entry(entry):
        return F_ABSENT
    try:
        info = os.lstat(entry_path(root, entry))
    except FileNotFoundError:
        return F_ABSENT
    except OSError:
        return F_OTHER
    return F_DIR if stat.S_ISDIR(info.st_mode) else F_OTHER


def read_manifest(root: Path, entry: str) -> Manifest | None:
    """The manifest of an entry, or None when it is damaged (missing, a link, too big, not
    this shape)."""
    path = entry_path(root, entry) / trash.MANIFEST
    try:
        info = os.lstat(path)
        if not stat.S_ISREG(info.st_mode) or info.st_size > MANIFEST_LIMIT:
            return None
        data = path.read_bytes()
    except OSError:
        return None
    return trash.parse_manifest(data, entry)


def entry_names(root: Path) -> list[str]:
    """The entry folders of `trash/`, by name: real folders whose name matches. Anything else
    in `trash/` is not an entry and is never touched."""
    try:
        names = os.listdir(trash_path(root))
    except OSError:
        return []
    return sorted(n for n in names if entry_kind(root, n) == F_DIR)


def list_entries(root: Path) -> list[Listed]:
    return [Listed(name, read_manifest(root, name)) for name in entry_names(root)]


def purge_entry(root: Path, entry: str) -> int:
    """Remove a whole entry folder and return the bytes it held. `OSError` when it cannot be
    removed whole (what is left stays for the next purge); `FileNotFoundError` when it is not
    a real folder. `shutil.rmtree` does not follow a link inside it."""
    if entry_kind(root, entry) != F_DIR:
        raise FileNotFoundError(entry)
    path = entry_path(root, entry)
    try:
        size, _ = tree_totals(path)
    except OSError:
        size = 0
    shutil.rmtree(path)
    return size


# --------------------------------------------------------------------------- the gamelist


@dataclass(frozen=True)
class GamelistEdit:
    """How an edit of the hub gamelist went: `status` is `removed`, `restored`, `present`,
    `none` or `failed`; `blocks` the entries taken out; `backup` where the file as it was went
    (below the hub root); `warning` says why it failed."""

    status: str
    blocks: tuple[str, ...] = ()
    backup: str | None = None
    warning: str | None = None
    paths: tuple[str, ...] = field(default=())


def _backup_name(root: Path, system: str, when: datetime) -> tuple[Path, str]:
    base = inbox_mame.backup_path(root, system, when.timestamp())
    path, count = base, 1
    while path.exists():
        count += 1
        path = base.with_name(f"{base.stem}-{count}{base.suffix}")
    return path, path.relative_to(root).as_posix()


def _edit_gamelist(
    root: Path,
    system: str,
    when: datetime,
    edit: Callable[[str], tuple[str, GamelistEdit]],
    *,
    create: Callable[[], tuple[str, GamelistEdit]] | None = None,
) -> GamelistEdit:
    """Read the hub gamelist, apply `edit(text) -> (new text, result)`, copy the file as it was
    to `tools/logs/gamelist-backups/` and write the new one unless the file changed since it was
    read (read again, up to three times). A file that is not there is made by `create` when
    given."""
    path = root / trash.gamelist_path(system)
    backup, shown = _backup_name(root, system, when)
    for _ in range(TRIES):
        text, error = read_gamelist(root, system)
        if error is not None:
            return GamelistEdit("failed", warning=f"the gamelist cannot be read ({error})")
        try:
            if text is None:
                if create is None:
                    return GamelistEdit("none")
                new, result = create()
                if safefile.atomic_write(path, new.encode("utf-8"), expect=None):
                    return result
                continue
            before = safefile.stamp(path)
            new, result = edit(text)
            if new == text:
                return result
            backup.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, backup)
            if safefile.atomic_write(path, new.encode("utf-8"), expect=before):
                return GamelistEdit(
                    result.status, result.blocks, shown, result.warning, result.paths
                )
        except (OSError, gamelist.GamelistError) as exc:
            reason = getattr(exc, "strerror", None) or str(exc)
            return GamelistEdit("failed", warning=f"the gamelist cannot be changed ({reason})")
    return GamelistEdit("failed", warning="the gamelist kept changing: its entry was not touched")


def drop_gamelist(root: Path, system: str, paths: Sequence[str], when: datetime) -> GamelistEdit:
    """Take the entries of `paths` out of the hub gamelist of `system`, with the backup."""

    def edit(text: str) -> tuple[str, GamelistEdit]:
        new, blocks = gamelist.drop_entries(text, paths)
        return new, GamelistEdit("removed" if blocks else "none", tuple(blocks))

    return _edit_gamelist(root, system, when, edit)


def restore_gamelist(
    root: Path, system: str, blocks: Sequence[str], when: datetime
) -> GamelistEdit:
    """Put saved entries back into the hub gamelist (one it already has an entry for is kept);
    a gamelist that is gone is made."""

    def edit(text: str) -> tuple[str, GamelistEdit]:
        new, restored, _ = gamelist.restore_entries(text, blocks)
        return new, GamelistEdit("restored" if restored else "present", paths=tuple(restored))

    def create() -> tuple[str, GamelistEdit]:
        new, restored, _ = gamelist.restore_entries(gamelist.EMPTY_GAMELIST, blocks)
        return new, GamelistEdit("restored", paths=tuple(restored))

    return _edit_gamelist(root, system, when, edit, create=create)


def paths_at(root: Path, paths: Sequence[str]) -> set[str]:
    """Which of `paths` (relative to `root`) are there, by `lstat` (a link counts as there)."""
    return {p for p in paths if os.path.lexists(root / p)}
