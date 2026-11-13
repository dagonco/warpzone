"""Scraping with Skyscraper, the edges: staging the games, running the tool, writing to the hub.

`run_scrape` goes through the systems one by one. For each it picks the games that need a scrape
(`scrape.select`), stages them as symbolic links outside the share, runs Skyscraper on batches
of them (the gather, then the two generating passes, each in its own session through
`runners.run_tool`), reads what Skyscraper left in a temporary folder, lets `scrape.plan_merge`
decide what is new, and writes only that to the hub: the media first, never over a file, then
the new gamelist entries with one atomic replace that is re-checked right before the rename.
Skyscraper never writes to the hub. The decisions are in `scrape`; see
development/design/scrape.md ("Flow", "Merge", "The cache", "Failures").

Nothing here reads Skyscraper's credentials: the only look into its `config.ini` is
`credentials_set`, which answers whether the `userCreds` key is there and keeps nothing else.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import stat
import subprocess
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import date
from pathlib import Path

from warpzone import (
    artwork,
    catalog,
    catalog_store,
    gamelist,
    inbox_lock,
    inbox_mame,
    runners,
    safefile,
    scrape,
)
from warpzone.catalog import Game
from warpzone.errors import UsageError
from warpzone.report import Reporter

ESDE_GAMELISTS = ("frontend", "es-de", "gamelists")
MEDIA_ROOT = ("frontend", "media")
GAMELIST_NAME = "gamelist.xml"
NOT_FOUND_FILE = "scrape-not-found.json"
WRITE_TRIES = 3  # reads of the hub gamelist before the entries are left for the next run
VERSION_TIMEOUT = 30.0
ARTWORK_DIR = Path(__file__).resolve().parent / "data" / "skyscraper"
COPY_CHUNK = 1 << 20


class ScrapeError(UsageError):
    """The scrape cannot start (no Skyscraper, a hub root that is not one)."""


class HubError(Exception):
    """A file of the hub cannot be used; the message is the reason."""


# ------------------------------------------------------------------ options and places


def default_cache_dir(env: Mapping[str, str] | None = None) -> Path:
    """`$XDG_CACHE_HOME/warpzone` (`~/.cache/warpzone`): the not-found memory lives here and,
    without `[inbox] work_dir`, the staged and temporary folders and the logs."""
    env = os.environ if env is None else env
    base = env.get("XDG_CACHE_HOME")
    return (Path(base) if base else Path.home() / ".cache") / "warpzone"


def find_skyscraper(
    configured: str | None, which: Callable[[str], str | None] = shutil.which
) -> str | None:
    """The Skyscraper program: the configured path (which must be an executable file), else
    `Skyscraper` on PATH, else None."""
    if configured:
        if not (os.path.isfile(configured) and os.access(configured, os.X_OK)):
            raise ScrapeError(f"[inbox] skyscraper: {configured} is not an executable file")
        return configured
    return which("Skyscraper")


@dataclass(frozen=True)
class Options:
    root: Path  # the hub root as mounted here
    skyscraper: str  # the program
    systems: tuple[str, ...]
    explicit: bool  # the systems were named with --system
    timeout: float  # seconds one Skyscraper call may run
    work_dir: Path | None = None  # [inbox] work_dir; else the cache folder
    cache_dir: Path | None = None  # the not-found memory and the default work folder
    config_ini: Path | None = None  # Skyscraper's config.ini, for the presence check only
    dry_run: bool = False
    retry_not_found: bool = False
    force_lock: bool = False
    only: frozenset[str] | None = None  # `inbox`: scrape only the games under these primary paths
    as_warning: bool = False  # `inbox`: a failure is a warning of its report, not a plain line
    artwork_dir: Path = ARTWORK_DIR
    now: Callable[[], float] = time.time
    today: Callable[[], date] = date.today

    @property
    def cache(self) -> Path:
        return self.cache_dir or default_cache_dir()

    @property
    def scratch(self) -> Path:
        """Where staged folders, temporary output and logs go: `<work folder>/scrape`."""
        return (self.work_dir or self.cache) / "scrape"


def gamelist_path(root: Path, system: str) -> Path:
    return root.joinpath(*ESDE_GAMELISTS, system, GAMELIST_NAME)


def media_path(root: Path, system: str) -> Path:
    return root.joinpath(*MEDIA_ROOT, system)


# ------------------------------------------------------------------ reading the hub


@dataclass(frozen=True)
class HubGamelist:
    """The hub gamelist as read: its bytes, the stamp they had and the entries. A gamelist that
    does not exist is `EMPTY_GAMELIST` with no stamp."""

    text: str
    raw: bytes
    stamp: safefile.FileStamp | None
    entries: tuple[artwork.Entry, ...]

    @property
    def mtime(self) -> float | None:
        return self.stamp.mtime_ns / 1e9 if self.stamp else None


def read_hub_gamelist(path: Path) -> HubGamelist:
    """Read the hub gamelist together with its stamp, retrying when it changes under the read.
    `HubError` says why it cannot be used (too large, not XML, not UTF-8, always changing)."""
    for _ in range(WRITE_TRIES):
        try:
            raw = artwork.read_gamelist(path)
            stamp = safefile.stamp(path)
        except FileNotFoundError:
            return HubGamelist(gamelist.EMPTY_GAMELIST, b"", None, ())
        except artwork.ArtworkError as exc:
            raise HubError(f"{path.name} is {exc}") from exc
        except OSError as exc:
            raise HubError(f"cannot read {path.name}: {exc.strerror or exc}") from exc
        if hashlib.sha256(raw).hexdigest() != stamp.sha256:
            continue
        try:
            text = raw.decode("utf-8")
            entries = artwork.parse_gamelist(raw)
        except (UnicodeDecodeError, artwork.ArtworkError) as exc:
            raise HubError(f"cannot read {path.name}: {exc}") from exc
        return HubGamelist(text, raw, stamp, tuple(entries))
    raise HubError(f"{path.name} keeps changing")


def list_hub_media(media_root: Path) -> set[str]:
    """The hub's media files of a system as `<kind>/<stem>.<ext>` with the extension in lower
    case (what `scrape.select` takes). Symbolic links are listed too: a file is there."""
    found: set[str] = set()
    for kind in scrape.MEDIA_KINDS:
        base = media_root / kind
        for folder, _dirs, files in os.walk(base):
            rel_dir = Path(folder).relative_to(media_root).as_posix()
            for name in files:
                stem, ext = os.path.splitext(name)
                found.add(f"{rel_dir}/{stem}{ext.lower()}")
    return found


def list_output(media_dir: Path) -> dict[str, scrape.FileType]:
    """The files below Skyscraper's media folder of a pass, `<folder>/<name>.<ext>` with `/`, each
    with its type. Symbolic links are listed, not followed."""
    found: dict[str, scrape.FileType] = {}
    stack = [media_dir]
    while stack:
        folder = stack.pop()
        try:
            entries = list(os.scandir(folder))
        except OSError:
            continue
        for item in entries:
            rel = Path(item.path).relative_to(media_dir).as_posix()
            if item.is_symlink():
                found[rel] = "symlink"
            elif item.is_dir(follow_symlinks=False):
                stack.append(Path(item.path))
            elif item.is_file(follow_symlinks=False):
                found[rel] = "file"
            else:
                found[rel] = "other"
    return found


def read_skyscraper_gamelist(folder: Path) -> list[artwork.Entry]:
    """The entries of the gamelist Skyscraper wrote in `folder` (none when it wrote none)."""
    try:
        return artwork.parse_gamelist(artwork.read_gamelist(folder / GAMELIST_NAME))
    except FileNotFoundError:
        return []


# ------------------------------------------------------------------ account and version


def credentials_set(config_ini: Path | None) -> bool:
    """Whether Skyscraper's `config.ini` has `userCreds` in `[screenscraper]`. It reads the file
    line by line and `scrape.has_user_creds` looks at keys only; no value is kept."""
    if config_ini is None:
        return False
    try:
        with config_ini.open(encoding="utf-8", errors="replace") as lines:
            return scrape.has_user_creds(lines)
    except OSError:
        return False


def skyscraper_version(program: str) -> str | None:
    """The version `Skyscraper --version` prints, as `X.Y.Z`, or None."""
    try:
        proc = runners.run_tool([program, "--version"], VERSION_TIMEOUT, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        return None
    found = scrape.parse_version((proc.stdout or "") + (proc.stderr or ""))
    return ".".join(str(n) for n in found) if found else None


# ------------------------------------------------------------------ staging


def stage(
    library: Path, input_dir: Path, games: Sequence[Game]
) -> tuple[dict[str, Game], list[scrape.Dropped]]:
    """Link each game into `input_dir` at its `gamelist` path (subfolders kept) to the file on
    the share. Nothing is copied. An `.m3u` folder is linked as a file named after the folder
    that points at the playlist inside it. Returns the map from staged name to game and the
    games that could not be staged, each with the reason."""
    staged: dict[str, Game] = {}
    dropped: list[scrape.Dropped] = []
    for game in games:
        name = artwork.relative(game.gamelist)
        parts = game.primary.split("/")
        if name is None or any(p in ("", ".", "..") for p in parts) or game.primary[:1] == "/":
            dropped.append(scrape.Dropped(game.gamelist, "has a path warpzone will not stage"))
            continue
        target = library.joinpath(*parts)
        primary = next((p for p in game.parts if p.path == game.primary), None)
        if primary is not None and primary.type == catalog.DIR and name.lower().endswith(".m3u"):
            target = target / parts[-1]
        if not target.exists():
            dropped.append(scrape.Dropped(name, "is not on the share any more; skipped"))
            continue
        link = input_dir / name
        link.parent.mkdir(parents=True, exist_ok=True)
        link.unlink(missing_ok=True)
        link.symlink_to(target)
        staged[name] = game
    return staged, dropped


# ------------------------------------------------------------------ the writer


@dataclass(frozen=True)
class Written:
    """What the writer did with a plan: the copies and entries that reached the hub, the
    warnings, and why the entries were not written (None when they were, or none were due)."""

    copies: frozenset[scrape.MediaCopy]
    entries: frozenset[str]  # relative gamelist paths of the entries written
    dropped: tuple[scrape.Dropped, ...]
    gamelist_error: str | None = None


def _present(media_root: Path, dest: str) -> bool:
    """Whether the hub has a file of the kind and stem of `dest` (`<kind>/<stem>.<ext>`) in any
    extension the library UI reads."""
    kind, _, rest = dest.partition("/")
    stem = os.path.splitext(rest)[0]
    return any(
        os.path.lexists(media_root / kind / f"{stem}.{ext}") for ext in scrape.KIND_EXTENSIONS[kind]
    )


def copy_media(source: Path, media_root: Path, dest: str) -> bool:
    """Copy one file of Skyscraper's output into the hub's media folder without ever replacing a
    file. It goes to `.<name>.wz-tmp` beside the target, is compared with the source by size,
    and enters under its name only when no file of that kind and stem is there: right before
    that the folder is checked again, and the name is taken with a hard link where the file
    system has them (which cannot replace a file), else with a rename after the check. Returns
    False when a file appeared first and the copy was dropped; `OSError` when it cannot be
    copied."""
    target = media_root / dest
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(f".{target.name}{safefile.TMP_SUFFIX}")
    tmp.unlink(missing_ok=True)
    try:
        fd = os.open(source, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        with os.fdopen(fd, "rb") as reader, open(tmp, "xb") as writer:
            if not stat.S_ISREG(os.fstat(reader.fileno()).st_mode):
                raise OSError("not a regular file")
            while chunk := reader.read(COPY_CHUNK):
                writer.write(chunk)
            writer.flush()
            os.fsync(writer.fileno())
            expected = os.fstat(reader.fileno()).st_size
        os.chmod(tmp, 0o644)
        if tmp.stat().st_size != expected:
            raise OSError("the copy has another size than the original")
        if _present(media_root, dest):
            return False
        try:
            os.link(tmp, target)
        except FileExistsError:
            return False
        except OSError:  # no hard links here (a network share): the check above is all there is
            if _present(media_root, dest):
                return False
            os.replace(tmp, target)
        return True
    finally:
        tmp.unlink(missing_ok=True)


def _backup(run: Run, system: str, hub: HubGamelist, path: Path) -> None:
    """Keep a copy of the gamelist before the run's first change to it."""
    if path in run.backed_up or hub.stamp is None:
        return
    backup = inbox_mame.backup_path(run.opts.root, system, run.opts.now())
    try:
        safefile.write_file(backup, hub.raw, 0o644)
    except OSError as exc:
        raise HubError(f"cannot back up {path.name}: {exc.strerror or exc}") from exc
    run.backed_up.add(path)
    now = run.opts.now()
    if hub.mtime is not None and inbox_mame.recently_written(hub.mtime, now):
        run.rep.warn(scrape.recent_message(system, max(0, int((now - hub.mtime) // 60))))


def _rel_paths(entries: Sequence[artwork.Entry]) -> set[str]:
    return {r for e in entries if e.tag in ("game", "folder") and (r := artwork.relative(e.path))}


def write_entries(
    run: Run, system: str, entries: Sequence[artwork.Entry]
) -> tuple[frozenset[str], str | None]:
    """Add `entries` to the hub gamelist. The gamelist is read with its stamp, edited as text
    and replaced atomically; the replace refuses when the file changed since the read, and then
    the plan is made again from the new content (entries a console wrote in the meantime are
    left as they are). After `WRITE_TRIES` tries the entries wait for the next run. Returns the
    relative paths written and the reason when they were not."""
    if not entries:
        return frozenset(), None
    path = gamelist_path(run.opts.root, system)
    for _ in range(WRITE_TRIES):
        try:
            hub = read_hub_gamelist(path)
            existing = _rel_paths(hub.entries)
            todo = [e for e in entries if artwork.relative(e.path) not in existing]
            if not todo:
                return frozenset(), None
            data = scrape.add_entries(hub.text, todo).encode("utf-8")
            _backup(run, system, hub, path)
            written = safefile.atomic_write(path, data, expect=hub.stamp)
        except (HubError, gamelist.GamelistError) as exc:
            return frozenset(), str(exc)
        except OSError as exc:
            return frozenset(), f"cannot write {path.name}: {exc.strerror or exc}"
        if written:
            return frozenset(_rel_paths(todo)), None
    return frozenset(), f"{path.name} changed {WRITE_TRIES} times while it was written"


def write_plan(
    run: Run, system: str, plan: scrape.MergePlan, output: Mapping[scrape.Step, Path]
) -> Written:
    """Execute a merge plan: the media first, then the gamelist entries, so an entry never names
    a game whose cover is still being written. `output` maps each pass to its media folder."""
    media_root = media_path(run.opts.root, system)
    copied: set[scrape.MediaCopy] = set()
    dropped: list[scrape.Dropped] = []
    for copy in plan.copies:
        source = output[copy.step] / copy.source
        try:
            if copy_media(source, media_root, copy.dest):
                copied.add(copy)
            else:
                dropped.append(scrape.Dropped(copy.dest, "appeared meanwhile; kept as it is"))
        except OSError as exc:
            reason = f"could not be copied ({exc.strerror or exc}); left for the next run"
            dropped.append(scrape.Dropped(copy.dest, reason))
    written, error = write_entries(run, system, plan.entries)
    return Written(frozenset(copied), written, tuple(dropped), error)


def settle(plan: scrape.MergePlan, done: Written) -> tuple[scrape.GameMerge, ...]:
    """The plan's games as they came out: copies that did not land are gone, and an entry that
    was not written (a console wrote one first, or the gamelist could not be written) turns an
    `added` into `filled` or `unchanged`."""
    merges: list[scrape.GameMerge] = []
    for merge in plan.games:
        copies = tuple(c for c in merge.copies if c in done.copies)
        entry = merge.entry
        had_entry = merge.had_entry
        result = merge.result
        if entry is not None and artwork.relative(entry.path) not in done.entries:
            entry = None
            had_entry = had_entry or done.gamelist_error is None
        if result is scrape.Result.ADDED and entry is None:
            result = scrape.Result.FILLED if copies else scrape.Result.UNCHANGED
        elif result is scrape.Result.FILLED and not copies:
            result = scrape.Result.UNCHANGED
        merges.append(
            replace(merge, result=result, had_entry=had_entry, entry=entry, copies=copies)
        )
    return tuple(merges)


# ------------------------------------------------------------------ the run


@dataclass
class Run:
    """The state of one `scrape`: what it reports, what stopped it, what it has written."""

    opts: Options
    rep: Reporter
    version: str | None
    account: bool
    memory: dict[str, scrape.NotFoundRecords]
    stopped: scrape.Stop | None = None
    failed: bool = False
    not_tried: int = 0
    backed_up: set[Path] = field(default_factory=set)

    def fail(self, message: str) -> None:
        self.failed = True
        (self.rep.warn if self.opts.as_warning else self.rep.out)(message)

    @property
    def memory_path(self) -> Path:
        return self.opts.cache / NOT_FOUND_FILE


@dataclass
class SystemRun:
    """One system's staged games and where the temporary output goes."""

    system: str
    platform: str
    folder: Path  # <scratch>/<system>: staged input and the batches' output
    log: Path
    staged: dict[str, Game]

    @property
    def input(self) -> Path:
        return self.folder / "input"


def _open_log(path: Path) -> None:
    """Start the log of a system's Skyscraper output: mode 0600, replacing the last run's."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    os.close(fd)
    os.chmod(path, 0o600)


def _call(run: Run, sr: SystemRun, label: str, args: Sequence[str]) -> tuple[bool, str]:
    """Run Skyscraper once; (it succeeded, its output). Its output goes to the system's log and
    is read only to classify it, never printed. A non-zero exit, a program that cannot run and
    the time limit are failures."""
    text = ""
    ok = False
    try:
        proc = runners.run_tool(
            [run.opts.skyscraper, *args], run.opts.timeout, stdin=subprocess.DEVNULL
        )
        text = (proc.stdout or "") + (proc.stderr or "")
        ok = proc.returncode == 0
        note = f"exit code {proc.returncode}"
    except subprocess.TimeoutExpired:
        note = "killed at the time limit"
    except OSError as exc:
        note = f"could not run ({exc.strerror or exc})"
    with open(sr.log, "a", encoding="utf-8", errors="replace") as log:
        log.write(f"==== {label}: {note}\n{text}\n")
    return ok, text


def _chunks(names: Sequence[str], size: int) -> list[list[str]]:
    return [list(names[i : i + size]) for i in range(0, len(names), size)]


def _merge_batch(
    run: Run, sr: SystemRun, number: int, names: Sequence[str], outcome: scrape.Outcome
) -> tuple[tuple[scrape.GameMerge, ...], tuple[scrape.Dropped, ...]] | None:
    """Generate a batch from the cache and merge it into the hub. None when it failed."""
    opts = run.opts
    batch_file = sr.folder / f"batch-{number}.txt"
    gen = sr.folder / f"out-{number}"
    folders: dict[scrape.Step, tuple[Path, Path]] = {}
    for step, art in ((scrape.MAIN, scrape.ARTWORK_ESDE), (scrape.BOX, scrape.ARTWORK_3DBOX)):
        gl_dir, media_dir = gen / f"{step}-gamelist", gen / f"{step}-media"
        gl_dir.mkdir(parents=True)
        media_dir.mkdir(parents=True)
        folders[step] = (gl_dir, media_dir)
        args = scrape.skyscraper_args(
            step,
            sr.platform,
            input_dir=sr.input,
            batch=batch_file,
            gamelist_dir=gl_dir,
            media_dir=media_dir,
            artwork_file=opts.artwork_dir / art,
        )
        ok, _ = _call(run, sr, f"{step} {number}", args)
        if not ok:
            run.fail(scrape.failed_message(sr.system, str(sr.log)))
            return None
    try:
        sky_entries = read_skyscraper_gamelist(folders[scrape.MAIN][0])
        hub = read_hub_gamelist(gamelist_path(opts.root, sr.system))
        hub_media = list_hub_media(media_path(opts.root, sr.system))
    except (artwork.ArtworkError, OSError) as exc:
        run.fail(scrape.failed_message(sr.system, str(sr.log)) + f": {exc}")
        return None
    except HubError as exc:
        run.fail(scrape.gamelist_message(sr.system, str(exc)))
        return None
    plan = scrape.plan_merge(
        {name: sr.staged[name] for name in names},
        str(sr.input),
        hub.entries,
        hub_media,
        sky_entries,
        list_output(folders[scrape.MAIN][1]),
        list_output(folders[scrape.BOX][1]),
    )
    done = write_plan(run, sr.system, plan, {s: media for s, (_, media) in folders.items()})
    if done.gamelist_error:
        run.fail(scrape.gamelist_message(sr.system, done.gamelist_error))
    merges = settle(plan, done)
    records = scrape.remember_not_found(
        run.memory.get(sr.system, {}),
        merges,
        set(outcome.not_found),
        set(outcome.retried),
        opts.today(),
    )
    if records != run.memory.get(sr.system, {}):
        run.memory[sr.system] = records
        safefile.write_json_cache(run.memory_path, scrape.render_not_found(run.memory))
    return merges, plan.dropped + done.dropped


def _scrape_batch(
    run: Run, sr: SystemRun, number: int, names: Sequence[str], later: int
) -> tuple[tuple[scrape.GameMerge, ...], tuple[scrape.Dropped, ...]]:
    """Gather one batch, then merge it, and delete the batch's output and list afterwards (a
    system's output would otherwise pile up on a small disk). A disk that fills up or any other
    `OSError` is a failed batch: reported, exit 1, and the next batch is tried."""
    try:
        return _run_batch(run, sr, number, names, later)
    except OSError as exc:
        run.fail(scrape.failed_message(sr.system, str(sr.log)) + f": {exc.strerror or exc}")
        return (), ()
    finally:
        shutil.rmtree(sr.folder / f"out-{number}", ignore_errors=True)
        (sr.folder / f"batch-{number}.txt").unlink(missing_ok=True)


def _run_batch(
    run: Run, sr: SystemRun, number: int, names: Sequence[str], later: int
) -> tuple[tuple[scrape.GameMerge, ...], tuple[scrape.Dropped, ...]]:
    """Gather one batch, then merge it. `later` is how many games wait in the batches after it."""
    batch_file = sr.folder / f"batch-{number}.txt"
    batch_file.write_text("".join(f"{sr.input / n}\n" for n in names), encoding="utf-8")
    args = scrape.skyscraper_args(scrape.GATHER, sr.platform, input_dir=sr.input, batch=batch_file)
    ok, text = _call(run, sr, f"gather {number}", args)
    outcome = scrape.classify_output(text)
    if outcome.stop is not None:
        run.stopped = outcome.stop
        if outcome.stop is not scrape.Stop.QUOTA:
            return (), ()
        # What the cache holds is still generated and merged; the rest waits for tomorrow.
        run.not_tried += max(0, len(names) - outcome.answered) + later
    elif not ok:
        run.fail(scrape.failed_message(sr.system, str(sr.log)))
        return (), ()
    return _merge_batch(run, sr, number, names, outcome) or ((), ())


def _not_in_catalog(only: frozenset[str] | None, system: str, games: Sequence[Game]) -> list[str]:
    """The placed entries of `system` (`only`, library paths) that no catalog game matches."""
    mine = sorted(p for p in only or () if p.startswith(f"roms/{system}/"))
    return [p for p in mine if not any(scrape.is_placed(g, [p]) for g in games)]


def _missing_message(system: str, missing: Sequence[str]) -> str:
    noun = "game" if len(missing) == 1 else "games"
    return (
        f"scrape: {system}: {len(missing)} placed {noun} not in the catalog; "
        f"run `warpzone catalog`, then `warpzone scrape --system {system}`"
    )


def _scrape_system(run: Run, system: str) -> scrape.Selection | None:
    """One system. Returns what was selected (for the dry run), None when it was skipped."""
    opts, rep = run.opts, run.rep
    if run.stopped is not None and run.stopped is not scrape.Stop.QUOTA:
        return None
    try:
        doc, _ = catalog_store.read_state(catalog_store.system_path(opts.root, system))
    except OSError as exc:
        run.fail(f"scrape: {system}: cannot read the catalog ({exc.strerror or exc})")
        return None
    if doc is None:
        missing = _not_in_catalog(opts.only, system, [])
        if missing:  # `inbox`: the catalog update failed, so its games cannot be scraped now
            run.fail(_missing_message(system, missing))
            return None
        if (opts.root / "library" / "roms" / system).is_dir():
            rep.out(f"scrape: {system}: no catalog; run `warpzone catalog` first")
        elif opts.explicit:
            rep.out(f"scrape: {system}: nothing in the library")
        return None
    games = sorted(doc.games.values(), key=lambda g: g.gamelist)
    if opts.only is not None:
        missing = _not_in_catalog(opts.only, system, games)
        if missing:
            run.fail(_missing_message(system, missing))
        games = [g for g in games if scrape.is_placed(g, opts.only)]
    try:
        hub = read_hub_gamelist(gamelist_path(opts.root, system))
    except HubError as exc:
        run.fail(scrape.gamelist_message(system, str(exc)))
        return None
    selection = scrape.select(
        games,
        hub.entries,
        list_hub_media(media_path(opts.root, system)),
        not_found=run.memory.get(system, {}),
        today=opts.today(),
        retry_not_found=opts.retry_not_found,
    )
    if opts.dry_run:
        rep.out(scrape.render_dry_run(system, selection))
        return selection
    if run.stopped is not None:  # the quota is spent: count what is left for tomorrow
        run.not_tried += len(selection.picks)
        return selection
    merges: list[scrape.GameMerge] = []
    dropped: list[scrape.Dropped] = []
    if selection.picks:
        sr = SystemRun(
            system,
            scrape.PLATFORMS[system],
            opts.scratch / system,
            opts.scratch / f"{system}.log",
            {},
        )
        shutil.rmtree(sr.folder, ignore_errors=True)
        try:
            _open_log(sr.log)
            sr.input.mkdir(parents=True, mode=0o700)
            sr.staged, unstaged = stage(
                opts.root / "library", sr.input, [p.game for p in selection.picks]
            )
            dropped += unstaged
            batches = _chunks(list(sr.staged), scrape.BATCH_SIZE)
            for number, names in enumerate(batches, 1):
                later = sum(len(b) for b in batches[number:])
                got, warned = _scrape_batch(run, sr, number, names, later)
                merges += got
                dropped += warned
                if run.stopped is not None:
                    break  # `later` was counted with the batch that hit the limit
        finally:  # also on Ctrl-C: nothing staged or temporary stays behind
            shutil.rmtree(sr.folder, ignore_errors=True)
    rep.out(
        scrape.render_report(
            scrape.Report(
                system,
                tuple(merges),
                tuple(dropped),
                selection.skipped_not_found,
                run.version,
                run.account,
            )
        )
    )
    return selection


def run_scrape(opts: Options, rep: Reporter) -> int:
    """Scrape the library's games of `opts.systems` into the hub. Returns the exit code: 0 when
    every selected game was handled, 1 when the run could not finish. `UsageError` (exit 2) for
    a hub root that is not one or a lock that is held; Ctrl-C ends it with the staged and
    temporary folders removed."""
    root = opts.root
    if not (root / "inbox").is_dir() or not (root / "library" / "roms").is_dir():
        raise ScrapeError(
            f"{root} has no inbox/ and library/roms/ folders; is --root the hub root?"
        )
    if opts.dry_run:  # it reads only, so it needs no lock
        return _run(opts, rep)
    with inbox_lock.hold(
        root / "inbox",
        force=opts.force_lock,
        now=opts.now,
        out=rep.out,
        mode=inbox_lock.ATTENDED,
    ):
        return _run(opts, rep)


def run_placed(opts: Options, rep: Reporter) -> int:
    """`inbox --apply`: scrape the games of `opts.only`, with the inbox lock the caller already
    holds (taking it again would fail). The same flow and exit code as `run_scrape`."""
    return _run(opts, rep)


def _run(opts: Options, rep: Reporter) -> int:
    memory = scrape.parse_not_found(safefile.read_json_cache(opts.cache / NOT_FOUND_FILE))
    version = None if opts.dry_run else skyscraper_version(opts.skyscraper)
    run = Run(opts, rep, version, credentials_set(opts.config_ini), memory)
    selections: dict[str, scrape.Selection] = {}
    for system in opts.systems:
        picked = _scrape_system(run, system)
        if picked is not None:
            selections[system] = picked
    if opts.dry_run:
        rep.out(scrape.render_dry_totals(selections))
        return 0
    if run.stopped is not None:
        run.fail(scrape.stop_message(run.stopped, not_tried=run.not_tried, account=run.account))
    return 1 if run.failed else 0
