"""`warpzone catalog`: walk the library, hash new files, write the system files that changed.

The decisions are in `catalog.py` (pure). Here is what touches the disk: the walk of a
system's folders, the SHA-1 of file parts that have none, the RAHasher pass, the download of
the RetroAchievements game lists (through `ra.py`) and the guarded writes of
`catalog/systems/<system>.json` and `catalog/ra/<console id>.json` (see "Location and format"
and "RetroAchievements" in development/design/library-ui.md). It writes only under
`catalog/` at the hub root.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from warpzone import catalog, ra, safefile
from warpzone.errors import UsageError
from warpzone.inbox_core import file_sha1

CATALOG_DIR = "catalog"
WRITE_TRIES = 3  # a system file that keeps changing under us is given up after this many
MAX_TEXT = 1 << 20  # launchers, shortcuts and cue sheets are far smaller than this
RA_BATCH = 10  # games hashed between two writes of a system file (an interrupted pass loses this)

# What runs RAHasher: (system, path) -> RA hash, or None when it cannot make one.
RaHasher = Callable[[str, Path], str | None]
# What `inbox` knows about the games it filed: primary path -> (RA hash, path it is the hash of).
RaKnown = Mapping[str, tuple[str, str]]


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
    ra_hashed: int = 0  # RA hashes recorded by this run
    ra_failed: int = 0  # games RAHasher could not hash (or whose file changed while it ran)
    ra_left: int = 0  # games still without a current RA hash (all of them, in a dry run)

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


def _ra_known_entries(
    games: Sequence[catalog.Game],
    targets: Mapping[str, catalog.RaTarget],
    known: RaKnown,
) -> dict[str, dict[str, Any]]:
    """The `ra` of the games `inbox` just filed: its hash, with the size and mtime the part
    has now, when the part it hashed is the one the derivation would hash."""
    out: dict[str, dict[str, Any]] = {}
    for game in games:
        target = targets.get(game.id)
        found = known.get(game.primary)
        if target and found and found[1] == target.of:
            out[game.id] = catalog.ra_entry(found[0], target)
    return out


def _ra_hash_games(
    library: Path,
    system: str,
    pending: Sequence[catalog.Game],
    targets: Mapping[str, catalog.RaTarget],
    hasher: RaHasher,
    cache: dict[tuple[str, int, int], str | None],
    tried: set[str],
    out: Callable[[str], None],
) -> tuple[dict[str, dict[str, Any]], int]:
    """Run RAHasher on the part of each game: (the `ra` of those that got a hash, how many
    did not). A part that changed while it was read is left for the next run."""
    found: dict[str, dict[str, Any]] = {}
    failed = 0
    for game in pending:
        target = targets[game.id]
        token = (target.of, target.size, target.mtime)
        if token not in cache:
            out(f"{system}: RA hash of {target.of}")
            full = library / target.of
            digest = hasher(system, full)
            try:
                st = full.stat()
                unchanged = (st.st_size, int(st.st_mtime)) == (target.size, target.mtime)
            except OSError:
                unchanged = False
            cache[token] = digest if unchanged else None
        digest = cache[token]
        if digest is None:
            tried.add(game.id)
            failed += 1
        else:
            found[game.id] = catalog.ra_entry(digest, target)
    return found, failed


def update_system(
    root: Path,
    system: str,
    *,
    today: str,
    updated: str,
    dry_run: bool = False,
    dat: Mapping[str, str] | None = None,
    serials: Mapping[str, tuple[str, ...]] | None = None,
    ra_known: RaKnown | None = None,
    ra_hasher: RaHasher | None = None,
    ra_limit: int | None = None,
    ra_tried: set[str] | None = None,  # games RAHasher failed on during this run
    out: Callable[[str], None] = lambda text: None,
) -> SystemResult:
    """Bring `catalog/systems/<system>.json` in line with the library.

    The write is guarded by the stamp of the file as it was read: when the file changed in
    between it is read again, the change applied again and the write retried. A file whose
    games would not change is not written. `dry_run` hashes nothing and writes nothing.

    `ra_known` is what `inbox` just filed (see `RaKnown`). With `ra_hasher`, at most
    `ra_limit` games whose part has no current RA hash are hashed (those in `ra_tried`, which
    RAHasher already failed on in this run, are left alone); `ra_left` in the result says how
    many more can be tried, so the caller calls again until it is 0.
    """
    library = root / "library"
    path = system_path(root, system)
    tried = ra_tried if ra_tried is not None else set()
    try:
        listing = walk(library, system)
        derived = catalog.derive(system, listing)
        targets = catalog.ra_targets(system, derived, listing)
        cache: dict[tuple[str, int, int], str] = {}
        ra_cache: dict[tuple[str, int, int], str | None] = {}
        hashed = failed = left = 0
        for _ in range(WRITE_TRIES):
            doc, stamp = read_state(path)
            previous = doc.games if doc else {}
            games = catalog.merge(previous, derived, today=today, dat=dat, serials=serials)
            if not dry_run:
                pending = catalog.pending_hashes(games)
                if pending:
                    out(f"{system}: hashing {len(pending)} file(s)")
                games = catalog.fill_hashes(games, _hash_parts(library, pending, cache))
            if ra_known:
                games = catalog.with_ra(games, _ra_known_entries(games, targets, ra_known))
            if ra_hasher is not None:
                waiting = catalog.ra_pending(games, targets)
                if dry_run:
                    left = len(waiting)
                else:
                    pool = [g for g in waiting if g.id not in tried]
                    todo = pool[:ra_limit]
                    if todo:
                        out(f"{system}: RA hashing {len(todo)} of {len(pool)} game(s)")
                    found, failed = _ra_hash_games(
                        library, system, todo, targets, ra_hasher, ra_cache, tried, out
                    )
                    games = catalog.with_ra(games, found)
                    hashed = len(found)
                    left = len(pool) - len(todo)
            delta = catalog.diff(previous, games)
            counts = SystemResult(
                system,
                "unchanged",
                games=len(games),
                added=len(delta.added),
                removed=len(delta.removed),
                changed=len(delta.changed),
                ra_hashed=hashed,
                ra_failed=failed,
                ra_left=left,
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
    text = f"{result.system}: {result.games} game(s), {verb}{delta}"
    if result.status == "would-write" and result.ra_left:
        text += f"; {result.ra_left} game(s) to RA-hash"
    elif result.ra_hashed or result.ra_failed:
        text += f"; RA: {result.ra_hashed} hashed"
        if result.ra_failed:
            text += f", {result.ra_failed} could not be hashed"
    return text


def _combine(first: SystemResult, last: SystemResult) -> SystemResult:
    """The result of two calls for one system (the RA pass calls until nothing is left)."""
    if last.failed:
        return replace(
            last,
            ra_hashed=first.ra_hashed + last.ra_hashed,
            ra_failed=first.ra_failed + last.ra_failed,
        )
    status = "written" if "written" in (first.status, last.status) else last.status
    return replace(
        last,
        status=status,
        added=first.added + last.added,
        removed=first.removed + last.removed,
        changed=first.changed + last.changed,
        ra_hashed=first.ra_hashed + last.ra_hashed,
        ra_failed=first.ra_failed + last.ra_failed,
    )


def resolve_rahasher(path: str | None) -> str:
    """The RAHasher to run: an executable file, or a usage error that says what is missing."""
    if not path:
        raise CatalogError("no RAHasher: pass --rahasher PATH or set `rahasher` in [inbox]")
    expanded = os.path.expanduser(path)
    if not (os.path.isfile(expanded) and os.access(expanded, os.X_OK)):
        raise CatalogError(f"RAHasher: {path} is not an executable file")
    return expanded


def rahasher_runner(program: str) -> RaHasher:
    """`ra.hash_image` with `program` as RAHasher."""

    def run(system: str, path: Path) -> str | None:
        return ra.hash_image(program, system, path)

    return run


def run_catalog(
    root: Path,
    *,
    dry_run: bool,
    out: Callable[[str], None],
    now: datetime | None = None,
    ra_hasher: RaHasher | None = None,
) -> list[SystemResult]:
    """`warpzone catalog`: every system of `library/roms/`. Returns the results; the caller
    exits 1 when any `failed`. With `ra_hasher`, the games of the systems in
    `ra.CONSOLE_IDS` whose part has no current RA hash are hashed, `RA_BATCH` at a time,
    each batch written before the next starts (so a stopped run loses little)."""
    if not (root / "library" / "roms").is_dir():
        raise CatalogError(f"{root} has no library/roms/ folder; is --root the hub root?")
    today, updated = _clock(now)
    out(f"catalog: {root / CATALOG_DIR}{' (dry run)' if dry_run else ''}")
    results = []
    for system in library_systems(root):
        tried: set[str] = set()
        result = update_system(
            root,
            system,
            today=today,
            updated=updated,
            dry_run=dry_run,
            ra_hasher=ra_hasher,
            ra_limit=RA_BATCH,
            ra_tried=tried,
            out=out,
        )
        while ra_hasher is not None and not dry_run and not result.failed and result.ra_left:
            more = update_system(
                root,
                system,
                today=today,
                updated=updated,
                ra_hasher=ra_hasher,
                ra_limit=RA_BATCH,
                ra_tried=tried,
                out=out,
            )
            result = _combine(result, more)
        results.append(result)
        out(_describe(result))
    return results


# ------------------------------------------------------------------ RA game lists


@dataclass(frozen=True)
class ListResult:
    console: int
    system: str
    status: str  # fetched | fresh | would-fetch | kept | failed
    games: int = 0
    message: str = ""  # the kind of failure, never the URL

    @property
    def failed(self) -> bool:
        """No list at all for the console: the download failed and there was no cache."""
        return self.status == "failed"


def ra_list_path(root: Path, console: int) -> Path:
    return root / CATALOG_DIR / "ra" / f"{console}.json"


def read_ra_list(path: Path) -> tuple[catalog.RaList | None, safefile.FileStamp | None]:
    """The cached list with the stamp it had while it was read; `(None, stamp)` when the file
    is not a list of this format, `(None, None)` when it is absent."""
    for _ in range(WRITE_TRIES):
        try:
            before = safefile.stamp(path)
        except FileNotFoundError:
            return None, None
        data = path.read_bytes()
        if safefile.stamp(path) == before:
            return catalog.decode_ra_list(data), before
    raise OSError(f"{path} keeps changing")


def ra_list_consoles(root: Path) -> list[tuple[int, str]]:
    """(console id, system) for each system of the library that is in `ra.CONSOLE_IDS`."""
    return sorted((ra.CONSOLE_IDS[s], s) for s in library_systems(root) if s in ra.CONSOLE_IDS)


def _update_ra_list(
    root: Path,
    console: int,
    system: str,
    *,
    key: str,
    fetch: ra.Fetch,
    force: bool,
    dry_run: bool,
    now: datetime,
    warn: Callable[[str], None],
) -> ListResult:
    path = ra_list_path(root, console)
    cached, stamp = read_ra_list(path)
    if cached and not force and catalog.ra_list_fresh(cached.fetched, now):
        return ListResult(console, system, "fresh", len(cached.games))
    if dry_run:
        return ListResult(console, system, "would-fetch", len(cached.games) if cached else 0)
    label = f"RA list of console {console} ({system})"
    try:
        games = ra.fetch_game_list(console, key, fetch)
    except ra.FetchError as exc:
        if cached:
            warn(f"{label}: download failed ({exc}); keeping the list from {cached.fetched}")
            return ListResult(console, system, "kept", len(cached.games), str(exc))
        warn(f"{label}: download failed ({exc}); there is no cached list")
        return ListResult(console, system, "failed", 0, str(exc))
    data = catalog.encode_ra_list(console, games, now.strftime("%Y-%m-%dT%H:%M:%SZ"))
    for _ in range(WRITE_TRIES):
        if safefile.atomic_write(path, data, expect=stamp):
            return ListResult(console, system, "fetched", len(games))
        _, stamp = read_ra_list(path)  # it changed under us: write over what is there now
    warn(f"{label}: the file kept changing, try again")
    return ListResult(console, system, "failed" if not cached else "kept", len(games), "conflict")


def run_ra_lists(
    root: Path,
    *,
    key: str | None,
    force: bool,
    dry_run: bool,
    out: Callable[[str], None],
    warn: Callable[[str], None],
    fetch: ra.Fetch | None = None,
    now: datetime | None = None,
) -> list[ListResult]:
    """`warpzone catalog --ra-lists`: refresh `catalog/ra/<console id>.json` for each console
    of `ra.CONSOLE_IDS` the library has. A list younger than 7 days is not downloaded again
    unless `force`. A failed download keeps the cached file and is reported by kind only. The
    caller exits 1 when any result `failed` (a console left with no list at all)."""
    if not key:
        raise CatalogError(f"no RetroAchievements key: set {ra.KEY_ENV} in the environment")
    if not (root / "library" / "roms").is_dir():
        raise CatalogError(f"{root} has no library/roms/ folder; is --root the hub root?")
    stamp = now or datetime.now(UTC)
    results = []
    for console, system in ra_list_consoles(root):
        try:
            result = _update_ra_list(
                root,
                console,
                system,
                key=key,
                fetch=fetch or ra.http_fetch,
                force=force,
                dry_run=dry_run,
                now=stamp,
                warn=warn,
            )
        except OSError as exc:
            warn(f"RA list of console {console} ({system}): {exc.strerror or exc}")
            result = ListResult(console, system, "failed", 0, "OSError")
        results.append(result)
        verb = {
            "fetched": f"fetched, {result.games} game(s)",
            "fresh": f"up to date, {result.games} game(s)",
            "would-fetch": "would download",
            "kept": "download failed, cached list kept",
            "failed": "download failed, no list",
        }[result.status]
        out(f"RA list {console} ({system}): {verb}")
    return results


def update_after_inbox(
    root: Path,
    systems: Collection[str],
    *,
    dat: Mapping[str, str],
    out: Callable[[str], None],
    warn: Callable[[str], None],
    serials: Mapping[str, tuple[str, ...]] | None = None,
    ra_known: RaKnown | None = None,
    now: datetime | None = None,
) -> None:
    """What `inbox --apply` runs after filing: the same derivation as `catalog` for each
    system it placed something in, merged with the DAT names, serials and RA hashes it knows (by
    primary path; see `RaKnown`).
    A failure is a warning: the next `catalog` run picks the game up."""
    today, updated = _clock(now)
    for system in sorted(systems):
        result = update_system(
            root,
            system,
            today=today,
            updated=updated,
            dat=dat,
            serials=serials,
            ra_known=ra_known,
            out=out,
        )
        if result.failed:
            warn(f"catalog: {_describe(result)}; run `warpzone catalog` later")
        elif result.status == "written":
            out(f"catalog: {_describe(result)}")
