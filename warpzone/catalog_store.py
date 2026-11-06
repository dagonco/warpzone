"""`warpzone catalog`: walk the library, hash new files, write the system files that changed.

The decisions are in `catalog.py` (pure). Here is what touches the disk: the walk of a
system's folders, the SHA-1 of file parts that have none, and the guarded writes of
`catalog/systems/<system>.json` (see "Location and format" in
development/design/library-ui.md). It writes only under `catalog/` at the hub root.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Collection, Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path

from warpzone import catalog, safefile
from warpzone.errors import UsageError
from warpzone.inbox_core import file_sha1

CATALOG_DIR = "catalog"
WRITE_TRIES = 3  # a system file that keeps changing under us is given up after this many
MAX_TEXT = 1 << 20  # launchers, shortcuts and cue sheets are far smaller than this


class CatalogError(UsageError):
    """The hub root cannot be catalogued (no `library/roms`)."""


@dataclass(frozen=True)
class SystemResult:
    system: str
    status: str  # written | would-write | unchanged | conflict | error
    games: int = 0
    added: int = 0
    removed: int = 0
    changed: int = 0
    message: str = ""

    @property
    def failed(self) -> bool:
        return self.status in ("conflict", "error")


def system_path(root: Path, system: str) -> Path:
    return root / CATALOG_DIR / "systems" / f"{system}.json"


def library_systems(root: Path) -> list[str]:
    """The system folders of `library/roms/`, in name order."""
    roms = root / "library" / "roms"
    try:
        names = os.listdir(roms)
    except OSError as exc:
        raise CatalogError(f"{roms}: {exc.strerror or exc}") from exc
    return sorted(n for n in names if not catalog.is_junk(n) and (roms / n).is_dir())


def walk(library: Path, system: str) -> catalog.Listing:
    """Stat every file and folder below the system's roots. Symlinks are not followed
    (Syncthing never does) and hidden or junk names are left out."""
    nodes: list[catalog.Node] = []
    texts: dict[str, str] = {}
    for root in catalog.system_roots(system):
        stack = [root]
        while stack:
            rel = stack.pop()
            try:
                entries = list(os.scandir(library / rel))
            except OSError:
                continue
            for entry in entries:
                if catalog.is_junk(entry.name):
                    continue
                path = f"{rel}/{entry.name}"
                try:
                    if entry.is_symlink():
                        continue
                    st = entry.stat(follow_symlinks=False)
                    if entry.is_dir(follow_symlinks=False):
                        nodes.append(catalog.Node(path, catalog.DIR, 0, int(st.st_mtime)))
                        stack.append(path)
                    elif entry.is_file(follow_symlinks=False):
                        nodes.append(catalog.Node(path, catalog.FILE, st.st_size, int(st.st_mtime)))
                        if (
                            entry.name.lower().endswith(catalog.TEXT_SUFFIXES)
                            and st.st_size <= MAX_TEXT
                        ):
                            texts[path] = (library / path).read_text(
                                encoding="utf-8", errors="replace"
                            )
                except OSError:
                    continue  # it vanished or cannot be read: the next run sees it again
    return catalog.Listing(nodes, texts)


def read_state(path: Path) -> tuple[catalog.SystemDoc | None, safefile.FileStamp | None]:
    """The system file as stored, with the stamp it had while it was read. A file that is
    absent gives `(None, None)`; one that is not a system file gives `(None, stamp)`."""
    for _ in range(WRITE_TRIES):
        try:
            before = safefile.stamp(path)
        except FileNotFoundError:
            return None, None
        data = path.read_bytes()
        if safefile.stamp(path) == before:
            return catalog.decode(data), before
    raise OSError(f"{path} keeps changing")


def _hash_parts(
    library: Path, parts: list[catalog.Part], cache: dict[tuple[str, int, int], str]
) -> dict[str, str]:
    """The SHA-1 of each file part; one that changed while it was read is left for later."""
    hashes: dict[str, str] = {}
    for part in parts:
        token = (part.path, part.size, part.mtime)
        if token not in cache:
            full = library / part.path
            try:
                digest = file_sha1(full)
                st = full.stat()
            except OSError:
                continue
            if (st.st_size, int(st.st_mtime)) != (part.size, part.mtime):
                continue
            cache[token] = digest
        hashes[part.path] = cache[token]
    return hashes


def update_system(
    root: Path,
    system: str,
    *,
    today: str,
    updated: str,
    dry_run: bool = False,
    dat: Mapping[str, str] | None = None,
    out: Callable[[str], None] = lambda text: None,
) -> SystemResult:
    """Bring `catalog/systems/<system>.json` in line with the library.

    The write is guarded by the stamp of the file as it was read: when the file changed in
    between it is read again, the change applied again and the write retried. A file whose
    games would not change is not written. `dry_run` hashes nothing and writes nothing.
    """
    library = root / "library"
    path = system_path(root, system)
    try:
        listing = walk(library, system)
        derived = catalog.derive(system, listing)
        cache: dict[tuple[str, int, int], str] = {}
        for _ in range(WRITE_TRIES):
            doc, stamp = read_state(path)
            previous = doc.games if doc else {}
            games = catalog.merge(previous, derived, today=today, dat=dat)
            if not dry_run:
                pending = catalog.pending_hashes(games)
                if pending:
                    out(f"{system}: hashing {len(pending)} file(s)")
                games = catalog.fill_hashes(games, _hash_parts(library, pending, cache))
            delta = catalog.diff(previous, games)
            counts = SystemResult(
                system,
                "unchanged",
                games=len(games),
                added=len(delta.added),
                removed=len(delta.removed),
                changed=len(delta.changed),
            )
            if (doc is not None and not delta) or (stamp is None and not games):
                return counts
            if dry_run:
                return replace(counts, status="would-write")
            data = catalog.encode(system, games, updated)
            if safefile.atomic_write(path, data, expect=stamp):
                return replace(counts, status="written")
        return SystemResult(system, "conflict", message="the file kept changing, try again")
    except OSError as exc:
        return SystemResult(system, "error", message=exc.strerror or str(exc))


def _clock(now: datetime | None) -> tuple[str, str]:
    stamp = now or datetime.now(UTC)
    return stamp.date().isoformat(), stamp.strftime("%Y-%m-%dT%H:%M:%SZ")


def _describe(result: SystemResult) -> str:
    if result.status in ("conflict", "error"):
        return f"{result.system}: {result.status}: {result.message}"
    verb = {"written": "written", "would-write": "would write", "unchanged": "unchanged"}[
        result.status
    ]
    delta = ""
    if result.status != "unchanged":
        delta = f" (+{result.added} -{result.removed} ~{result.changed})"
    return f"{result.system}: {result.games} game(s), {verb}{delta}"


def run_catalog(
    root: Path,
    *,
    dry_run: bool,
    out: Callable[[str], None],
    now: datetime | None = None,
) -> list[SystemResult]:
    """`warpzone catalog`: every system of `library/roms/`. Returns the results; the caller
    exits 1 when any `failed`."""
    if not (root / "library" / "roms").is_dir():
        raise CatalogError(f"{root} has no library/roms/ folder; is --root the hub root?")
    today, updated = _clock(now)
    out(f"catalog: {root / CATALOG_DIR}{' (dry run)' if dry_run else ''}")
    results = []
    for system in library_systems(root):
        result = update_system(root, system, today=today, updated=updated, dry_run=dry_run, out=out)
        results.append(result)
        out(_describe(result))
    return results


def update_after_inbox(
    root: Path,
    systems: Collection[str],
    *,
    dat: Mapping[str, str],
    out: Callable[[str], None],
    warn: Callable[[str], None],
    now: datetime | None = None,
) -> None:
    """What `inbox --apply` runs after filing: the same derivation as `catalog` for each
    system it placed something in, merged with the DAT names it knows (by primary path).
    A failure is a warning: the next `catalog` run picks the game up."""
    today, updated = _clock(now)
    for system in sorted(systems):
        result = update_system(root, system, today=today, updated=updated, dat=dat, out=out)
        if result.failed:
            warn(f"catalog: {_describe(result)}; run `warpzone catalog` later")
        elif result.status == "written":
            out(f"catalog: {_describe(result)}")
