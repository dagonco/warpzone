"""Favourites and play statistics across frontends: ledgers and merge rules. Pure: no I/O.

Each device owns one JSON Lines file in the `wz-play-state` folder, keyed by hub system and ROM
path below it. Nothing but `plan` decides what a device publishes and what its gamelists
should say; `playstate_device` reads and writes the files. See development/design/play-state.md.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, tzinfo
from xml.etree import ElementTree as ET

from warpzone import artwork, gamelist
from warpzone.errors import WarpzoneError

VERSION = 1
# The change time of a favourite a device already had when it first ran: older than any
# recorded change, so it joins the other devices' favourites and undoes no recorded unset.
ADOPTED_AT = 1
# A last played time further ahead than this, as judged by the reader, comes from a wrong clock.
FUTURE_SLACK = 86400

ELEMENT = "warpzonePlayState"
_TIME = re.compile(r"\d{8}T\d{6}")
_TIME_FORMAT = "%Y%m%dT%H%M%S"
_OPEN = re.compile(r"<gameList(?:\s[^>]*)?>[ \t]*(?:\r?\n)?")
_CLOSE = re.compile(r"[ \t]*</gameList\s*>")
_BLOCK = re.compile(rf"[ \t]*<{ELEMENT}(?:\s*/>|\s*>.*?</{ELEMENT}\s*>)[ \t]*(?:\r?\n)?", re.DOTALL)
_SHARED_FIELDS = ("playcount", "favorite", "lastplayed")

Key = tuple[str, str]  # hub system, ROM path relative to it


class PlayStateError(WarpzoneError):
    """A gamelist cannot be edited safely."""


class LedgerError(WarpzoneError):
    """A ledger file cannot be trusted."""


@dataclass(frozen=True)
class Record:
    """What one device says about one game.

    `plays` is the replica's own counter as this device knows it, `included` how many plays
    of other replicas the count it left in its gamelist holds, `last` a UTC epoch.
    `fav` is None until the device has something to say.
    """

    plays: int = 0
    included: int = 0
    last: int | None = None
    fav: bool | None = None
    fav_at: int = 0


EMPTY = Record()


@dataclass(frozen=True)
class Ledger:
    device: str
    replica: str
    records: Mapping[Key, Record] = field(default_factory=dict)


@dataclass(frozen=True)
class Seen:
    """One game as a local gamelist shows it.

    `clock` is the UTC time the file was saved. `included` is what the file itself says it
    holds of other replicas, None when the file carries no element.
    """

    plays: int = 0
    last: int | None = None
    fav: bool = False
    clock: int = 0
    included: int | None = None


@dataclass(frozen=True)
class Wanted:
    """What has to change in a gamelist entry for one game; None leaves a value alone."""

    plays: int | None = None
    favorite: bool | None = None
    last: int | None = None


@dataclass(frozen=True)
class Merged:
    own: int = 0  # the reader's replica
    others: int = 0  # every other replica
    included: int = 0
    fav: bool | None = None
    fav_at: int = 0
    last: int | None = None

    @property
    def total(self) -> int:
        return self.own + self.others


@dataclass(frozen=True)
class Plan:
    observed: Ledger  # what the gamelist said: safe to publish while the frontend runs
    applied: Ledger  # the same once the gamelist says `wanted`
    wanted: Mapping[Key, Wanted]
    included: Mapping[Key, int]


# ------------------------------------------------------------------ the merge


def merge(ledgers: Sequence[Ledger], key: Key, replica: str, *, horizon: int) -> Merged:
    """The merged state of a game seen from `replica`. Independent of the order of `ledgers`.

    Devices of one replica read the same gamelist, so their counters are views of one
    counter: the highest counts. Replicas add up. The favourite with the latest change time
    wins, and on equal times `true`. The latest last played time wins, ignoring those after
    `horizon`.
    """
    counters: dict[str, int] = {}
    included = 0
    fav: tuple[int, bool] | None = None
    last: int | None = None
    for ledger in ledgers:
        record = ledger.records.get(key)
        if record is None:
            continue
        counters[ledger.replica] = max(counters.get(ledger.replica, 0), record.plays)
        if ledger.replica == replica:
            included = max(included, record.included)
        if record.fav is not None and (fav is None or (record.fav_at, record.fav) > fav):
            fav = (record.fav_at, record.fav)
        if record.last is not None and record.last <= horizon:
            last = record.last if last is None else max(last, record.last)
    own = counters.pop(replica, 0)
    return Merged(
        own,
        sum(counters.values()),
        included,
        None if fav is None else fav[1],
        0 if fav is None else fav[0],
        last,
    )


def _observe(mine: Record, look: Seen | None, before: Merged) -> Record:
    """The device's record after reading its gamelist.

    The count minus what it holds of other replicas is what this replica has played; the
    counter only grows, so an older file never takes plays back. A favourite that differs
    from the device's record was changed: it is stamped with the time the file was saved,
    and always after anything the device has seen, so a change made after seeing another
    wins even on a device whose clock is behind.
    """
    if look is None:
        return mine
    included = before.included if look.included is None else look.included
    plays = max(mine.plays, look.plays - included)
    last = mine.last
    if look.last is not None and (last is None or look.last > last):
        last = look.last
    fav, fav_at = mine.fav, mine.fav_at
    if mine.fav is None:
        if look.fav:
            fav, fav_at = True, ADOPTED_AT
    elif look.fav != mine.fav:
        if look.fav == before.fav:
            fav, fav_at = before.fav, before.fav_at
        else:
            fav, fav_at = look.fav, max(look.clock, max(mine.fav_at, before.fav_at) + 1)
    return Record(plays, included, last, fav, fav_at)


def plan(
    mine: Ledger,
    peers: Sequence[Ledger],
    seen: Mapping[Key, Seen],
    present: Callable[[Key], bool],
    *,
    horizon: int,
) -> Plan:
    """What the device publishes and what its gamelists should say.

    `seen` is every game of its gamelists, `peers` the ledgers it has received. A game for
    which `present` says no keeps its record and is left out of `wanted`: that is how state
    for a ROM this device lacks is preserved and ignored.
    """
    others = [p for p in peers if p.device != mine.device]
    keys = set(seen) | set(mine.records)
    for peer in others:
        keys |= set(peer.records)
    observed: dict[Key, Record] = {}
    applied: dict[Key, Record] = {}
    wanted: dict[Key, Wanted] = {}
    included: dict[Key, int] = {}
    for key in sorted(keys):
        look = seen.get(key)
        before = merge([mine, *others], key, mine.replica, horizon=horizon)
        record = _observe(mine.records.get(key, EMPTY), look, before)
        if record != EMPTY:
            observed[key] = record
        after = merge(
            [replace(mine, records={key: record}), *others], key, mine.replica, horizon=horizon
        )
        if not present(key):
            if record != EMPTY:
                applied[key] = record
            continue
        foreign = max(after.others, record.included)
        shown = look if look is not None else Seen()
        total = after.own + foreign
        change = Wanted(
            total if total != shown.plays else None,
            after.fav if after.fav is not None and after.fav != shown.fav else None,
            after.last
            if after.last is not None and (shown.last is None or after.last > shown.last)
            else None,
        )
        if change != Wanted():
            wanted[key] = change
        if foreign:
            included[key] = foreign
        done = replace(
            record,
            included=foreign,
            fav=record.fav if after.fav is None else after.fav,
            fav_at=record.fav_at if after.fav is None else after.fav_at,
        )
        if done != EMPTY:
            applied[key] = done
    return Plan(replace(mine, records=observed), replace(mine, records=applied), wanted, included)


# ------------------------------------------------------------------ times


def parse_time(text: str, zone: tzinfo | None) -> int | None:
    """A gamelist time, which is local to the device that wrote it, as a UTC epoch."""
    if not _TIME.fullmatch(text):
        return None
    try:
        naive = datetime.strptime(text, _TIME_FORMAT)
        aware = naive.astimezone() if zone is None else naive.replace(tzinfo=zone)
        return int(aware.timestamp())
    except (ValueError, OverflowError, OSError):
        return None


def format_time(epoch: int, zone: tzinfo | None) -> str:
    """A UTC epoch as a gamelist time, local to this device."""
    moment = datetime.fromtimestamp(epoch, UTC).astimezone(zone)
    return moment.strftime(_TIME_FORMAT)


# ------------------------------------------------------------------ ledger files


def _natural(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _record(line: dict[str, object]) -> tuple[Key, Record]:
    system, path = line.get("system"), line.get("path")
    if not isinstance(system, str) or not artwork.is_plain(system):
        raise LedgerError("a line has no valid system")
    if not isinstance(path, str) or artwork.relative(path) != path:
        raise LedgerError("a line has no valid path")
    plays, included = line.get("plays", 0), line.get("included", 0)
    last, fav, fav_at = line.get("last"), line.get("fav"), line.get("fav_at", 0)
    if not (_natural(plays) and _natural(included) and _natural(fav_at)):
        raise LedgerError("a line has a value that is not a count")
    if not (last is None or _natural(last)) or not (fav is None or isinstance(fav, bool)):
        raise LedgerError("a line has a value of the wrong type")
    return (system, path), Record(plays, included, last, fav, fav_at)  # type: ignore[arg-type]


def parse_ledger(text: str, device: str) -> Ledger:
    """A ledger file; raises `LedgerError` rather than reading half of one."""
    lines = [line for line in text.splitlines() if line.strip()]
    try:
        rows = [json.loads(line) for line in lines]
    except ValueError as exc:
        raise LedgerError(f"not JSON Lines ({exc})") from exc
    if not rows or not all(isinstance(r, dict) for r in rows):
        raise LedgerError("no header line")
    header = rows[0]
    if header.get("v") != VERSION:
        raise LedgerError(f"unknown version {header.get('v')!r}")
    replica = header.get("replica")
    if header.get("device") != device or not isinstance(replica, str) or not replica:
        raise LedgerError("the header does not name this device")
    return Ledger(device, replica, dict(_record(row) for row in rows[1:]))


def render_ledger(ledger: Ledger) -> str:
    """The file text. The same state gives the same bytes, so Syncthing sends nothing."""

    def dumps(value: dict[str, object]) -> str:
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))

    lines = [dumps({"v": VERSION, "device": ledger.device, "replica": ledger.replica})]
    for (system, path), record in sorted(ledger.records.items()):
        row: dict[str, object] = {"system": system, "path": path}
        if record.plays:
            row["plays"] = record.plays
        if record.included:
            row["included"] = record.included
        if record.last is not None:
            row["last"] = record.last
        if record.fav is not None:
            row["fav"] = record.fav
            row["fav_at"] = record.fav_at
        lines.append(dumps(row))
    return "\n".join(lines) + "\n"


# ------------------------------------------------------------------ reading gamelists


def _count(text: str | None) -> int:
    try:
        return max(0, int(text or "0"))
    except ValueError:
        return 0


def seen_games(
    entries: Sequence[artwork.Entry],
    system: str,
    base: str,
    included: Mapping[Key, int] | None,
    clock: int,
    zone: tzinfo | None,
    tags: Mapping[str, str] = artwork.STATE_TAGS,
) -> dict[Key, Seen]:
    """The games of a gamelist of hub `system`; `base` is its folder below the hub's one."""
    seen: dict[Key, Seen] = {}
    for entry in entries:
        rel = artwork.relative(entry.path)
        if entry.tag != "game" or rel is None:
            continue
        key = (system, base + rel)
        seen.setdefault(
            key,
            Seen(
                _count(entry.fields.get(tags["playcount"])),
                parse_time(entry.fields.get(tags["lastplayed"], ""), zone),
                entry.fields.get(tags["favorite"]) == "true",
                clock,
                None if included is None else included.get(key, 0),
            ),
        )
    return seen


def included_in(text: str, system: str) -> dict[Key, int] | None:
    """The plays of other replicas the file says it holds; None when it does not say."""
    block = _BLOCK.search(text)
    if block is None:
        return None
    try:
        root = ET.fromstring(block.group(0).strip())
    except ET.ParseError:
        return None
    found: dict[Key, int] = {}
    for node in root:
        rel = artwork.relative(node.get("path", ""))
        if rel is not None:
            found[(system, rel)] = _count(node.get("plays"))
    return found


# ------------------------------------------------------------------ editing gamelists


def element(included: Mapping[str, int], eol: str = "\n") -> str:
    """The element a shared gamelist carries: it says what its counts hold of other replicas.

    It sits in the same file as the counts, so a device that receives the file knows how to
    read them whichever Syncthing folder arrives first. ES-DE keeps unknown children of
    `<gameList>` and reads only `game` and `folder`.
    """
    lines = [f"\t<{ELEMENT}>"]
    for rel, plays in sorted(included.items()):
        path = gamelist.escape("./" + rel).replace('"', "&quot;")
        lines.append(f'\t\t<included path="{path}" plays="{plays}" />')
    lines.append(f"\t</{ELEMENT}>")
    return eol.join(lines) + eol


def _set_tag(block: str, tag: str, value: str | None, eol: str) -> str:
    whole = re.compile(rf"<{tag}>[^<]*</{tag}\s*>|<{tag}\s*/>")
    if value is None:
        line = re.compile(rf"[ \t]*(?:{whole.pattern})[ \t]*(?:\r?\n)?")
        return line.sub("", block, count=1)
    text = f"<{tag}>{gamelist.escape(value)}</{tag}>"
    if whole.search(block):
        return whole.sub(lambda _: text, block, count=1)
    indent = gamelist.child_indent(block, "", line_start_only=True)
    return gamelist.add_child(block, text, eol, indent)


def _values(change: Wanted, zone: tzinfo | None) -> list[tuple[str, str | None]]:
    values: list[tuple[str, str | None]] = []
    if change.favorite is not None:
        values.append(("favorite", "true" if change.favorite else None))
    if change.plays is not None:
        values.append(("playcount", str(change.plays)))
    if change.last is not None:
        values.append(("lastplayed", format_time(change.last, zone)))
    return values


def _new_game(rel: str, change: Wanted, zone: tzinfo | None, eol: str) -> str:
    lines = ["\t<game>", f"\t\t<path>{gamelist.escape('./' + rel)}</path>"]
    for tag, value in _values(change, zone):
        if value is not None:
            lines.append(f"\t\t<{tag}>{value}</{tag}>")
    lines.append("\t</game>")
    return eol.join(lines) + eol


def _same_besides_state(before: str, after: str) -> bool:
    def body(text: str) -> dict[tuple[str, str], dict[str, str]]:
        return {
            (e.tag, e.path): {k: v for k, v in e.fields.items() if k not in _SHARED_FIELDS}
            for e in artwork.parse_gamelist(text)
        }

    old, new = body(before), body(after)
    return all(new.get(key) == fields for key, fields in old.items())


def edit_gamelist(
    text: str,
    changes: Mapping[str, Wanted],
    included: Mapping[str, int],
    system: str,
    zone: tzinfo | None,
) -> str:
    """`text` with the changes of the games named by path; every other byte is kept.

    Only the three tags change, or the entry is added when the file lacks the game (its ROM
    is on this device). Entries of games that are not here stay. Raises `PlayStateError`
    when the file has no `<gameList>` or the result would say anything else.
    """
    had_element = _BLOCK.search(text) is not None
    if not changes and not included and not had_element:
        return text
    if included_in(text, system) == {(system, k): v for k, v in included.items()} and not changes:
        return text
    eol = gamelist.eol_of(text)
    done: set[str] = set()

    def game(match: re.Match[str]) -> str:
        block = match.group(0)
        try:
            path = (ET.fromstring(block).findtext("path") or "").strip()
        except ET.ParseError:
            return block
        rel = artwork.relative(path)
        if rel is None or rel not in changes or rel in done:
            return block
        done.add(rel)
        for tag, value in _values(changes[rel], zone):
            block = _set_tag(block, tag, value, eol)
        return block

    new = _BLOCK.sub("", text)
    new = gamelist.expand_empty_list(new, eol)
    edited, at = [], 0
    for match in gamelist.game_blocks(new):
        edited += [new[at : match.start()], game(match)]
        at = match.end()
    new = "".join(edited) + new[at:]
    opening = _OPEN.search(new)
    if opening is None or opening.group(0).rstrip().endswith("/>"):
        raise PlayStateError("the file has no <gameList> to edit")
    closing = gamelist.last_outside_comments(_CLOSE, new)
    if closing is None:
        raise PlayStateError("the file has no </gameList>")
    extra = "".join(
        _new_game(rel, change, zone, eol)
        for rel, change in sorted(changes.items())
        if rel not in done and any(value for _, value in _values(change, zone))
    )
    new = new[: closing.start()] + extra + new[closing.start() :]
    if included or had_element:
        at = opening.end()
        lead = "" if opening.group(0).endswith("\n") else eol
        new = new[:at] + lead + element(included, eol) + new[at:]
    try:
        if not _same_besides_state(text, new):
            raise PlayStateError("the edit would change something else in the gamelist")
    except artwork.ArtworkError as exc:
        raise PlayStateError(f"the gamelist cannot be read ({exc})") from exc
    return new
