"""Play state on a device: reads and writes the ledgers and the gamelists.

The decisions are in `playstate`. This module imports no network code, because ES-DE waits for
it at startup. See development/design.md, "Play state".
"""

from __future__ import annotations

import dataclasses
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import tzinfo
from functools import partial
from pathlib import Path

from warpzone import artwork, esde, playstate, procs, safefile, settings_state
from warpzone.model import ESDE_ID
from warpzone.paths import expand_home
from warpzone.playstate import Key, Ledger, LedgerError, Plan, PlayStateError, Seen
from warpzone.profiles import Profile

Say = Callable[[str], None]


def _expand(path: str, home: Path) -> Path:
    return Path(expand_home(path, home))


def folder(profile: Profile, home: Path) -> Path | None:
    """The play state folder, once Syncthing has it (its marker keeps a card that is not
    mounted from getting a new folder)."""
    raw = profile.paths.get("play_state")
    if raw is None:
        return None
    path = _expand(raw, home)
    return path if (path / ".stfolder").exists() else None


def replica_of(device: str, profile: Profile) -> str:
    """Devices that share ES-DE's folder read one gamelist, so they are one replica."""
    return ESDE_ID if "esde" in profile.paths else device


def _read(path: Path) -> str:
    return path.read_bytes().decode("utf-8")


def load(where: Path, device: str, replica: str, warn: Say) -> tuple[Ledger, list[Ledger]] | None:
    """This device's ledger and the others'. None when its own cannot be trusted."""
    mine = Ledger(device, replica, {})
    peers: list[Ledger] = []
    for path in sorted(where.glob("*.jsonl")):
        if ".sync-conflict-" in path.name:
            continue
        name = path.stem
        try:
            ledger = playstate.parse_ledger(_read(path), name)
        except (OSError, UnicodeDecodeError, LedgerError) as exc:
            if name == device:
                warn(f"{path} cannot be read ({exc}), so the play state was left alone")
                return None
            warn(f"{path} cannot be read ({exc}), so that device is left out for now")
            continue
        if name == device:
            mine = dataclasses.replace(ledger, replica=replica)
        else:
            peers.append(ledger)
    return mine, peers


def publish(where: Path, ledger: Ledger) -> bool:
    """Write this device's file when it says something new."""
    path = where / f"{ledger.device}.jsonl"
    text = playstate.render_ledger(ledger).encode("utf-8")
    try:
        current: safefile.FileStamp | None = safefile.stamp(path)
        if path.read_bytes() == text:
            return False
    except FileNotFoundError:
        current = None
        if not ledger.records:
            return False
    return safefile.atomic_write(path, text, expect=current)


def _clock(path: Path, now: int) -> int:
    return min(int(path.stat().st_mtime), now)


# ------------------------------------------------------------------ ES-DE: a shared gamelist


@dataclass(frozen=True)
class _Gamelist:
    system: str
    path: Path
    text: str
    stamp: safefile.FileStamp
    included: Mapping[Key, int] | None


def _read_gamelists(
    root: Path, now: int, zone: tzinfo | None, warn: Say
) -> tuple[list[_Gamelist], dict[Key, Seen]]:
    lists: list[_Gamelist] = []
    seen: dict[Key, Seen] = {}
    for path in sorted(root.glob("*/gamelist.xml")):
        system = path.parent.name
        if not artwork.is_plain(system):
            continue
        try:
            stamp = safefile.stamp(path)
            text = _read(path)
            entries = artwork.parse_gamelist(text)
            clock = _clock(path, now)
        except (OSError, UnicodeDecodeError, artwork.ArtworkError) as exc:
            warn(f"{path} cannot be read ({exc}), so it was left alone")
            continue
        included = playstate.included_in(text, system)
        lists.append(_Gamelist(system, path, text, stamp, included))
        seen.update(playstate.seen_games(entries, system, "", included, clock, zone))
    return lists, seen


def _frontend_open(names: Iterable[str], proc: Path, starting: bool) -> bool | None:
    """Whether ES-DE has its gamelists in memory; None when it cannot be told.

    A run from its `startup` script is below ES-DE, which waits for it and has read nothing
    yet, so with `starting` the processes above this one do not count.
    """
    try:
        return procs.running(names, proc=proc, skip_ancestors=starting)
    except procs.NoProc:
        return None


def sync_shared(
    device: str,
    profile: Profile,
    home: Path,
    *,
    say: Say,
    warn: Say,
    dry_run: bool = False,
    starting: bool = False,
    proc: Path = Path("/proc"),
    state_root: Path | None = None,
    now: Callable[[], float] = time.time,
    zone: tzinfo | None = None,
) -> None:
    """Merge the play state into ES-DE's gamelists, publishing what they say.

    Gamelists are written only while ES-DE has none in memory: closed, or starting (it reads
    them after its `startup` script). With ES-DE open the device only publishes. Each edit
    backs the file up, keeps every other byte and is refused if the file changed. With
    `dry_run` nothing is written, not even the lock file.
    """
    if "esde" not in profile.paths:
        return
    where = folder(profile, home)
    if where is None:
        say("play state: no play state folder yet, nothing to do")
        return
    root = (state_root or settings_state.state_dir(home=home)).parent / "play-state"
    if dry_run:  # it writes nothing, so it needs no lock, which would be a file of its own
        _sync_shared(
            device, profile, home, where, say, warn, dry_run, starting, proc, root, now, zone
        )
        return
    with settings_state.lock(root=root) as got:
        if not got:
            say("play state: another run is in progress, nothing done")
            return
        _sync_shared(
            device, profile, home, where, say, warn, dry_run, starting, proc, root, now, zone
        )


def _sync_shared(
    device: str,
    profile: Profile,
    home: Path,
    where: Path,
    say: Say,
    warn: Say,
    dry_run: bool,
    starting: bool,
    proc: Path,
    state_root: Path,
    now: Callable[[], float],
    zone: tzinfo | None,
) -> None:
    moment = int(now())
    loaded = load(where, device, replica_of(device, profile), warn)
    if loaded is None:
        return
    mine, peers = loaded
    esde_dir = _expand(profile.paths["esde"], home)
    lists, seen = _read_gamelists(esde_dir / "gamelists", moment, zone, warn)
    systems = {g.system for g in lists}
    library = _expand(profile.paths.get("library", ""), home) / "roms"

    def present(key: Key) -> bool:
        return key in seen or (key[0] in systems and (library / key[0] / key[1]).exists())

    plan = playstate.plan(mine, peers, seen, present, horizon=moment + playstate.FUTURE_SLACK)
    opened = _frontend_open(esde.ESDE_PROCESSES, proc, starting)
    if opened is not False:
        if dry_run:
            say("play state: dry-run: would only publish, ES-DE may be open")
        elif publish(where, plan.observed):
            say(f"play state: published {where / (device + '.jsonl')}")
        say("play state: ES-DE is open or cannot be ruled out, so its gamelists were left alone")
        return
    edits = _edits(lists, plan, zone, warn)
    if dry_run:
        for gamelist, _ in edits:
            say(f"play state: dry-run: would update {gamelist.path}")
        return
    publish(where, plan.applied)
    for gamelist, text in edits:
        outcome = safefile.guarded_edit(
            gamelist.path,
            text.encode("utf-8"),
            expect=gamelist.stamp,
            busy=lambda: _frontend_open(esde.ESDE_PROCESSES, proc, starting) is not False,
            backup=partial(
                settings_state.backup,
                gamelist.path,
                "play-state",
                gamelist.system,
                root=state_root,
            ),
        )
        if outcome is safefile.Edit.BUSY:
            warn(f"ES-DE started, so {gamelist.path} was left alone")
        elif outcome is safefile.Edit.CHANGED:
            warn(f"{gamelist.path} changed while it was being edited, so it was left alone")
        else:
            say(f"play state: updated {gamelist.path}")
    say(f"play state: {len(edits)} gamelists updated, {len(seen)} games read")


def _edits(
    lists: Sequence[_Gamelist], plan: Plan, zone: tzinfo | None, warn: Say
) -> list[tuple[_Gamelist, str]]:
    edits: list[tuple[_Gamelist, str]] = []
    for gamelist in lists:
        system = gamelist.system
        changes = {k[1]: v for k, v in plan.wanted.items() if k[0] == system}
        included = {k[1]: v for k, v in plan.included.items() if k[0] == system}
        try:
            text = playstate.edit_gamelist(gamelist.text, changes, included, system, zone)
        except PlayStateError as exc:
            warn(f"{gamelist.path}: {exc}, so it was left alone")
            continue
        if text != gamelist.text:
            edits.append((gamelist, text))
    return edits


# ------------------------------------------------------------------ a generated gamelist


@dataclass(frozen=True)
class Generated:
    """The play state of a device whose gamelists `artwork` generates."""

    where: Path
    plan: Plan
    zone: tzinfo | None
    tags: Mapping[str, str]

    def overlay(self) -> dict[str, dict[str, str]]:
        """Fields for `artwork.plan_gamelists`, by `<hub>/<path>`; empty values remove one."""
        fields: dict[str, dict[str, str]] = {}
        for (system, rel), change in self.plan.wanted.items():
            one: dict[str, str] = {}
            if change.plays is not None:
                one[self.tags["playcount"]] = str(change.plays)
            if change.favorite is not None:
                one[self.tags["favorite"]] = "true" if change.favorite else ""
            if change.last is not None:
                one[self.tags["lastplayed"]] = playstate.format_time(change.last, self.zone)
            fields[f"{system}/{rel}"] = one
        return fields

    def publish(self, *, applied: bool) -> bool:
        return publish(self.where, self.plan.applied if applied else self.plan.observed)


def prepare_generated(
    device: str,
    profile: Profile,
    home: Path,
    layout: artwork.Layout,
    systems: Mapping[str, Sequence[artwork.FrontendSystem]],
    files: artwork.Files,
    *,
    warn: Say,
    now: Callable[[], float] = time.time,
    zone: tzinfo | None = None,
) -> Generated | None:
    """Read the frontend's gamelists and the ledgers. None when there is nothing to do yet."""
    where = folder(profile, home)
    if where is None:
        return None
    moment = int(now())
    loaded = load(where, device, replica_of(device, profile), warn)
    if loaded is None:
        return None
    mine, peers = loaded
    art = profile.artwork
    tags = artwork.FORMATS[art.format].state_tags if art is not None else artwork.STATE_TAGS
    seen: dict[Key, Seen] = {}
    scraped: set[str] = set()
    for hub, entries in systems.items():
        if files.read(layout.metadata / "gamelists" / hub / "gamelist.xml") is not None:
            scraped.add(hub)
        for system in entries:
            path = artwork.gamelist_path(layout, system)
            data = files.read(path)
            if data is None:
                continue
            try:
                games = artwork.parse_gamelist(data)
                clock = _clock(path, moment)
            except (OSError, artwork.ArtworkError) as exc:
                warn(f"{path} cannot be read ({exc}), so it counts for nothing this time")
                continue
            for key, look in playstate.seen_games(
                games, hub, system.base, None, clock, zone, tags
            ).items():
                seen.setdefault(key, look)

    def present(key: Key) -> bool:
        hub, rel = key
        return hub in scraped and files.exists(layout.roms / hub / rel)

    plan = playstate.plan(mine, peers, seen, present, horizon=moment + playstate.FUTURE_SLACK)
    return Generated(where, plan, zone, tags)
