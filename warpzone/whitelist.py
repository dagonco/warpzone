"""Managed device whitelists: the model, its file format, the changes and the migration (pure).

A managed `patterns-<device>.stignore` is fully determined by three sets: the systems that are
on as a whole, the includes (paths that are on) and the excludes (paths that are off inside
something that is on). Nothing here touches the disk or the network; `whitelist_store.py` reads
and writes the files. Paths are relative to the library folder, with `/` separators and no
leading slash (`roms/psx/Some Game (Europe).chd`). See "The whitelist model" in
development/design/library-ui.md.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Collection, Iterable, Iterator, Sequence
from dataclasses import dataclass
from functools import cached_property
from typing import Literal

FORMAT_VERSION = 1
HEADER = (
    "// Managed by warpzone (library UI). Edit it in the UI; hand edits are refused.",
    f"// warpzone-whitelist {FORMAT_VERSION}",
)
COMMON_INCLUDE = "#include patterns-common.stignore"
SECTIONS = ("Excluded", "Systems", "Games", "Other")
# Syncthing's ignore syntax on Linux: a backslash escapes the next character.
_SPECIAL = re.compile(r"([\\*?\[\]{}])")
_UNESCAPED_SPECIAL = frozenset("*?[]{}")
SYSTEM_RE = re.compile(r"^[a-z0-9_-]+$")
# Systems whose games sit one level down, in `roms/<system>/roms/`.
SUBROOT_SYSTEMS = frozenset({"xbox360", "wiiu", "model2"})
# The system folders below `extras/` (Switch updates and DLC).
EXTRAS_SYSTEMS = frozenset({"switch"})

State = Literal["on", "off", "partial"]


class Unlistable(ValueError):
    """A path that cannot be written as one whitelist line."""


class ParseError(ValueError):
    """A managed file that does not parse; `line` is 1-based (0: the file as a whole)."""

    def __init__(self, line: int, message: str) -> None:
        super().__init__(f"line {line}: {message}" if line else message)
        self.line = line
        self.message = message


class Refused(Exception):
    """A change that cannot be applied; `code` is the API's error code."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


# --------------------------------------------------------------------------- paths


def ancestors(path: str) -> Iterator[str]:
    """`path` and each of its parents, nearest first, stopping before the root (`a/b`: a/b, a)."""
    while path:
        yield path
        path = path.rpartition("/")[0]


def covers(parent: str, path: str) -> bool:
    """Whether `path` is `parent` or below it (the empty parent is the library itself)."""
    return not parent or path == parent or path.startswith(parent + "/")


def system_roots(system: str) -> tuple[str, ...]:
    """The paths a whole system is whitelisted by, in the order the file lists them.

    The same rule as `catalog.system_roots`: `roms/<system>`, `roms/<system>/roms` for the
    systems of `SUBROOT_SYSTEMS`, and for `switch` the three paths of its games, updates and DLC.
    """
    if system == "switch":
        return ("roms/switch", "extras/switch/updates", "extras/switch/dlc")
    if system in SUBROOT_SYSTEMS:
        return (f"roms/{system}/roms",)
    return (f"roms/{system}",)


def system_of_root(path: str) -> str | None:
    """The system a root path belongs to, or None when the path is no system root."""
    parts = path.split("/")
    if parts[0] == "roms" and 2 <= len(parts) <= 3 and SYSTEM_RE.match(parts[1]):
        name = parts[1]
        if len(parts) == 2 and name not in SUBROOT_SYSTEMS:
            return name
        if len(parts) == 3 and parts[2] == "roms" and name in SUBROOT_SYSTEMS:
            return name
        return None
    if path in ("extras/switch/updates", "extras/switch/dlc"):
        return "switch"
    return None


def system_of(path: str) -> str | None:
    """The system a path lies in: `roms/<system>/...` or `extras/<system>/...`."""
    parts = path.split("/")
    if (
        len(parts) >= 2
        and SYSTEM_RE.match(parts[1])
        and (parts[0] == "roms" or (parts[0] == "extras" and parts[1] in EXTRAS_SYSTEMS))
    ):
        return parts[1]
    return None


def check_path(path: str) -> str | None:
    """Why `path` cannot be written as a whitelist line, or None when it can."""
    if not path:
        return "the path is empty"
    for part in path.split("/"):
        if not part:
            return "the path has an empty component (a leading, trailing or doubled slash)"
        if part in (".", ".."):
            return f"the path has a {part!r} component"
        if "\n" in part or "\r" in part or "\0" in part:
            return "the path holds a newline, a carriage return or a NUL"
        if part != part.strip():
            return "a component of the path starts or ends with whitespace"
    return None


def unlistable(path: str) -> bool:
    return check_path(path) is not None


def escape(path: str) -> str:
    """The anchored literal pattern for `path`: `/` plus the path with `\\*?[]{}` escaped."""
    return "/" + _SPECIAL.sub(r"\\\1", path)


def unescape(pattern: str) -> str:
    """The path an anchored literal pattern names; the reverse of `escape`.

    Raises `ValueError` for a pattern that is not anchored, holds an unescaped wildcard or
    ends in a lone backslash.
    """
    if not pattern.startswith("/"):
        raise ValueError("the pattern is not anchored (it must start with /)")
    out: list[str] = []
    chars = iter(pattern[1:])
    for char in chars:
        if char == "\\":
            nxt = next(chars, None)
            if nxt is None:
                raise ValueError("the pattern ends in a lone backslash")
            out.append(nxt)
        elif char in _UNESCAPED_SPECIAL:
            raise ValueError(f"unescaped {char!r} (wildcards are not allowed)")
        else:
            out.append(char)
    return "".join(out)


def _line(path: str) -> str:
    reason = check_path(path)
    if reason:
        raise Unlistable(f"{path!r}: {reason}")
    return escape(path)


# --------------------------------------------------------------------------- the model


@dataclass(frozen=True)
class Model:
    """What a device receives: whole systems, extra paths, and holes cut into either."""

    systems: frozenset[str] = frozenset()
    includes: frozenset[str] = frozenset()
    excludes: frozenset[str] = frozenset()

    @cached_property
    def roots(self) -> frozenset[str]:
        return frozenset(r for s in self.systems for r in system_roots(s))


def decide(model: Model, path: str) -> bool:
    """Whether `path` is on: no exclude covers it, and a system root or an include does."""
    chain = list(ancestors(path))
    if any(a in model.excludes for a in chain):
        return False
    return any(a in model.roots or a in model.includes for a in chain)


def normalize(model: Model) -> Model:
    """The one model for a set of paths on: redundant includes and excludes are dropped.

    An include below a system root or another include goes, and so does one below an exclude;
    an exclude that no root or surviving include covers goes (`*` already leaves it out), and
    so does one below another exclude. The order matters: the includes an exclude shadows are
    dropped before the exclude itself is judged.
    """
    roots = model.roots
    includes = {
        i
        for i in model.includes
        if not any(a in roots or (a in model.includes and a != i) for a in ancestors(i))
        and not any(a in model.excludes for a in ancestors(i))
    }
    holders = roots | includes
    excludes = {
        e
        for e in model.excludes
        if any(a in holders for a in ancestors(e))
        and not any(a in model.excludes and a != e for a in ancestors(e))
    }
    return Model(model.systems, frozenset(includes), frozenset(excludes))


# --------------------------------------------------------------------------- the file format


def _game_path(path: str) -> bool:
    return path.startswith(("roms/", "extras/switch/"))


def render(model: Model) -> str:
    """The managed file for `model` (normalized first, so one set of paths is one file)."""
    model = normalize(model)
    games = sorted(i for i in model.includes if _game_path(i))
    other = sorted(i for i in model.includes if not _game_path(i))
    sections: list[tuple[str, list[str]]] = [
        ("Excluded", [_line(p) for p in sorted(model.excludes)]),
        ("Systems", ["!" + _line(r) for s in sorted(model.systems) for r in system_roots(s)]),
        ("Games", ["!" + _line(p) for p in games]),
        ("Other", ["!" + _line(p) for p in other]),
    ]
    lines = [*HEADER, COMMON_INCLUDE]
    for title, body in sections:
        if body:
            lines += ["", f"// {title}", *body]
    lines += ["", "*"]
    return "\n".join(lines) + "\n"


def parse(text: str) -> Model:
    """The model of a file in the managed shape; `ParseError` for anything else.

    The shape is strict (header, include, sections in order, a final `*`), but not
    canonical: `is_managed` also demands that the model renders back to the same bytes.
    """
    if not text.endswith("\n"):
        raise ParseError(0, "the file does not end with a newline")
    lines = text[:-1].split("\n")
    expected = [*HEADER, COMMON_INCLUDE]
    for number, want in enumerate(expected, 1):
        if number > len(lines) or lines[number - 1] != want:
            raise ParseError(number, f"expected {want!r}")
    excludes: set[str] = set()
    includes: set[str] = set()
    roots: set[str] = set()
    section: str | None = None
    seen: list[str] = []
    star = False
    for number, line in enumerate(lines, 1):
        if number <= len(expected) or not line:
            continue
        if star:
            raise ParseError(number, "a line after the final *")
        if line.startswith("// "):
            title = line[3:]
            if title not in SECTIONS or title in seen:
                raise ParseError(number, f"unexpected comment {line!r}")
            if seen and SECTIONS.index(title) < SECTIONS.index(seen[-1]):
                raise ParseError(number, f"section {title!r} is out of order")
            seen.append(title)
            section = title
            continue
        if line == "*":
            star = True
            continue
        if section is None:
            raise ParseError(number, "a pattern before any section")
        negated = section != "Excluded"
        if negated != line.startswith("!"):
            raise ParseError(number, f"{line!r} does not belong in the {section} section")
        try:
            path = unescape(line[1:] if negated else line)
        except ValueError as exc:
            raise ParseError(number, str(exc)) from exc
        reason = check_path(path)
        if reason:
            raise ParseError(number, reason)
        if section == "Excluded":
            excludes.add(path)
        elif section == "Systems":
            roots.add(path)
        else:
            includes.add(path)
    if not star:
        raise ParseError(0, "the file does not end with *")
    return Model(_systems_of_roots(roots), frozenset(includes), frozenset(excludes))


def _systems_of_roots(roots: Collection[str]) -> frozenset[str]:
    """The systems whose roots are all in `roots`; a root of a partial system is refused."""
    found = {s for r in roots if (s := system_of_root(r)) is not None}
    for r in roots:
        if system_of_root(r) is None:
            raise ParseError(0, f"{r!r} is not the root of a system")
    for system in found:
        if not set(system_roots(system)) <= set(roots):
            raise ParseError(0, f"the roots of {system!r} are not all there")
    return frozenset(found)


def is_managed(text: str) -> bool:
    """Whether the file is in the managed shape and renders back to exactly the same bytes."""
    try:
        return render(parse(text)) == text
    except (ParseError, Unlistable):
        return False


@dataclass(frozen=True)
class Reading:
    """What a whitelist file is: `managed`, or `needs-migration` (the API's words).

    `model` is what the file means when it can be known: its own for a managed file, the
    migration's classification (without the proof) for a hand-written one that parses. It is
    None for a file the migration's parser refuses, whose reasons are in `errors`.
    """

    status: Literal["managed", "needs-migration"]
    model: Model | None
    errors: tuple[str, ...] = ()


def read(text: str) -> Reading:
    if is_managed(text):
        return Reading("managed", parse(text))
    hand = parse_hand(text)
    if hand.errors:
        return Reading("needs-migration", None, hand.errors)
    return Reading("needs-migration", plan_migration(hand).model)


def etag(data: bytes | None) -> str:
    """The SHA-256 of the file's bytes; `"absent"` for a file that does not exist."""
    return "absent" if data is None else hashlib.sha256(data).hexdigest()


# --------------------------------------------------------------------------- states


def part_states(model: Model, parts: Iterable[str]) -> list[bool]:
    return [decide(model, p) for p in parts]


def game_state(model: Model, parts: Sequence[str]) -> State:
    """`on` when every part is on, `off` when none is, else `partial`."""
    states = part_states(model, parts)
    if all(states):
        return "on"
    return "partial" if any(states) else "off"


@dataclass(frozen=True)
class Game:
    """A game as the model needs it: its id, its system and every path it needs."""

    id: str
    system: str
    parts: tuple[str, ...]


def system_state(model: Model, games: Iterable[Game]) -> Literal["all", "some", "none"]:
    """`all`, `some` or `none` of a system's games are on."""
    states = [game_state(model, g.parts) == "on" for g in games]
    if states and all(states):
        return "all"
    return "some" if any(states) else "none"


# --------------------------------------------------------------------------- changes


@dataclass(frozen=True)
class GameChange:
    game: Game
    on: bool


@dataclass(frozen=True)
class SystemChange:
    system: str
    on: bool


Change = GameChange | SystemChange


def set_game(model: Model, parts: Sequence[str], on: bool) -> Model:
    """Turn one game on or off, whatever its parts' state; `Refused("covered")` if it cannot."""
    for part in parts:
        reason = check_path(part)
        if reason:
            raise Refused("unlistable", f"{part!r}: {reason}")
    includes = set(model.includes)
    excludes = set(model.excludes)
    if on:
        excludes = {e for e in excludes if not any(covers(p, e) for p in parts)}
        for part in parts:
            if decide(Model(model.systems, frozenset(includes), frozenset(excludes)), part):
                continue
            if any(a in excludes for a in ancestors(part)):
                raise Refused("covered", f"an exclusion of a parent folder covers {part!r}")
            includes.add(part)
    else:
        includes = {i for i in includes if not any(covers(p, i) for p in parts)}
        for part in parts:
            if decide(Model(model.systems, frozenset(includes), frozenset(excludes)), part):
                excludes.add(part)
    return normalize(Model(model.systems, frozenset(includes), frozenset(excludes)))


def set_system(model: Model, system: str, on: bool) -> Model:
    """Turn a system on (what is below its roots falls away) or off (and all that is below goes)."""
    if on:
        return normalize(Model(model.systems | {system}, model.includes, model.excludes))
    roots = system_roots(system)

    def below(path: str) -> bool:
        return any(covers(r, path) for r in roots)

    return normalize(
        Model(
            model.systems - {system},
            frozenset(i for i in model.includes if not below(i)),
            frozenset(e for e in model.excludes if not below(e)),
        )
    )


@dataclass(frozen=True)
class Effect:
    """The games whose state differs between two models, by id."""

    turned_on: tuple[str, ...] = ()
    turned_off: tuple[str, ...] = ()
    other: tuple[str, ...] = ()  # to or from `partial`

    @property
    def changed(self) -> tuple[str, ...]:
        return tuple(sorted({*self.turned_on, *self.turned_off, *self.other}))


def effect(old: Model, new: Model, games: Iterable[Game]) -> Effect:
    """Evaluate every part of every game with both models and list the games that changed."""
    on: list[str] = []
    off: list[str] = []
    other: list[str] = []
    for game in games:
        before, after = game_state(old, game.parts), game_state(new, game.parts)
        if before == after:
            continue
        if after == "on":
            on.append(game.id)
        elif after == "off":
            off.append(game.id)
        else:
            other.append(game.id)
    return Effect(tuple(sorted(on)), tuple(sorted(off)), tuple(sorted(other)))


@dataclass(frozen=True)
class Applied:
    model: Model
    effect: Effect


def apply(
    model: Model,
    change: Change,
    games: Collection[Game],
    excluded_systems: Collection[str] = (),
) -> Applied:
    """Apply one change and check its effect; `Refused` (nothing to write) when it does not hold.

    The change is applied only when the target reached the state asked for and no other game
    changed state (for a system change, only games of that system). The codes are `unsupported`
    (turning on something in `excluded_systems`), `unlistable`, `covered` and `cannot-apply`.
    """
    if isinstance(change, GameChange):
        target_system = change.game.system
        if change.on and target_system in excluded_systems:
            raise Refused("unsupported", f"{target_system!r} is not run by this device")
        new = set_game(model, change.game.parts, change.on)
        result = effect(model, new, games)
        reached = game_state(new, change.game.parts) == ("on" if change.on else "off")
        allowed = {change.game.id}
    else:
        target_system = change.system
        if change.on and target_system in excluded_systems:
            raise Refused("unsupported", f"{target_system!r} is not run by this device")
        new = set_system(model, change.system, change.on)
        result = effect(model, new, games)
        wanted: State = "on" if change.on else "off"
        members = [g for g in games if g.system == change.system]
        reached = (change.system in new.systems) == change.on and all(
            game_state(new, g.parts) == wanted for g in members
        )
        allowed = {g.id for g in members}
    if not reached or not set(result.changed) <= allowed:
        raise Refused("cannot-apply", "the change does not turn on or off exactly what was asked")
    return Applied(new, result)


# --------------------------------------------------------------------------- supported systems


@dataclass(frozen=True)
class Unsupported:
    system: str
    kind: Literal["system", "include", "exclude"]
    path: str


def unsupported(model: Model, excluded_systems: Collection[str]) -> list[Unsupported]:
    """What the whitelist names of the systems a device does not run, sorted."""
    found: list[Unsupported] = []
    for name in sorted(set(excluded_systems) & model.systems):
        found.append(Unsupported(name, "system", system_roots(name)[0]))
    for path in sorted(model.includes):
        system = system_of(path)
        if system is not None and system in excluded_systems:
            found.append(Unsupported(system, "include", path))
    for path in sorted(model.excludes):
        system = system_of(path)
        if system is not None and system in excluded_systems:
            found.append(Unsupported(system, "exclude", path))
    return found


# --------------------------------------------------------------------------- the migration


@dataclass(frozen=True)
class HandLine:
    """One pattern of a hand-written whitelist. `path` is empty for the final `*`."""

    number: int
    on: bool  # a `!` line
    path: str


@dataclass(frozen=True)
class HandParse:
    lines: tuple[HandLine, ...]
    errors: tuple[str, ...]


def parse_hand(text: str) -> HandParse:
    """Read a hand-written whitelist with the migration's strict rules.

    Accepted: blank lines, `//` comments, `#include patterns-common.stignore` (once, before
    any pattern), anchored literal patterns with or without `!` (escapes allowed) and a final
    `*`. Anything else, a missing include or a missing `*` is an error, listed with its line.
    """
    errors: list[str] = []
    lines: list[HandLine] = []
    included = star = False

    def bad(number: int, message: str) -> None:
        errors.append(f"line {number}: {message}" if number else message)

    for number, raw in enumerate(text.split("\n"), 1):
        if not raw.strip() or raw.startswith("//"):
            continue
        if star:
            bad(number, "a line after the final *")
        elif raw.startswith("#include"):
            if raw != COMMON_INCLUDE:
                bad(number, f"only {COMMON_INCLUDE!r} can be included")
            elif included:
                bad(number, "the include appears twice")
            elif lines:
                bad(number, "the include must come before the patterns")
            else:
                included = True
        elif raw == "*":
            star = True
            lines.append(HandLine(number, False, ""))
        elif raw.startswith("(?"):
            bad(number, "the (?i) and (?d) modifiers are not accepted")
        else:
            on = raw.startswith("!")
            try:
                path = unescape(raw[1:] if on else raw)
            except ValueError as exc:
                bad(number, f"{exc}; write literal paths")
                continue
            reason = check_path(path)
            if reason:
                bad(number, reason)
            else:
                lines.append(HandLine(number, on, path))
    if not included:
        bad(0, f"the file does not start with {COMMON_INCLUDE!r}")
    if not star:
        bad(0, "the file does not end with *")
    return HandParse(tuple(lines), tuple(errors))


class LineMatcher:
    """First-match evaluation of hand-written lines, as Syncthing does it.

    The first line whose path is the path or one of its parents decides: `!` means on,
    anything else (a plain pattern, `*`) off. A path no line matches is not ignored.
    """

    def __init__(self, lines: Iterable[HandLine]) -> None:
        self.first: dict[str, tuple[int, bool]] = {}
        for index, line in enumerate(lines):
            self.first.setdefault(line.path, (index, line.on))

    def on(self, path: str) -> bool:
        best: tuple[int, bool] | None = None
        for candidate in (*ancestors(path), ""):
            hit = self.first.get(candidate)
            if hit is not None and (best is None or hit[0] < best[0]):
                best = hit
        return True if best is None else best[1]


def decide_lines(lines: Sequence[HandLine], path: str) -> bool:
    """Whether the hand-written `lines` leave `path` on (first match wins)."""
    return LineMatcher(lines).on(path)


@dataclass(frozen=True)
class MigrationPlan:
    model: Model
    kept: tuple[HandLine, ...]
    shadowed: tuple[HandLine, ...]
    redundant: tuple[HandLine, ...]


def plan_migration(hand: HandParse) -> MigrationPlan:
    """Drop the lines with no effect, classify the rest and normalize (no proof yet).

    A line at or below an earlier line's path is shadowed. A plain line with no later `!`
    line at its path or above is redundant with the final `*`. When every root of a system
    appears as a `!` line the system is on; every other `!` line is an include and every
    other plain line an exclude.
    """
    patterns = [line for line in hand.lines if line.path]
    seen: set[str] = set()
    kept: list[HandLine] = []
    shadowed: list[HandLine] = []
    for line in patterns:
        if any(a in seen for a in ancestors(line.path)):
            shadowed.append(line)
        else:
            seen.add(line.path)
            kept.append(line)
    later_on: set[str] = set()
    redundant: list[HandLine] = []
    survivors: list[HandLine] = []
    for line in reversed(kept):
        if line.on:
            later_on.add(line.path)
            survivors.append(line)
        elif any(a in later_on for a in ancestors(line.path)):
            survivors.append(line)
        else:
            redundant.append(line)
    survivors.reverse()
    redundant.reverse()
    on_paths = {line.path for line in survivors if line.on}
    systems = {s for p in on_paths if (s := system_of_root(p)) is not None}
    systems = {s for s in systems if set(system_roots(s)) <= on_paths}
    roots = {r for s in systems for r in system_roots(s)}
    model = normalize(
        Model(
            frozenset(systems),
            frozenset(line.path for line in survivors if line.on and line.path not in roots),
            frozenset(line.path for line in survivors if not line.on),
        )
    )
    return MigrationPlan(model, tuple(survivors), tuple(shadowed), tuple(redundant))


class Prover:
    """Compares a hand-written whitelist with the model it migrates to, path by path.

    `differs` is the proof's test; `skip` says that a directory needs no visit, since it is
    off on both sides and no line of either sits below it, so everything under it is off too.
    """

    def __init__(self, lines: Sequence[HandLine], model: Model) -> None:
        self.old = LineMatcher(lines)
        self.model = model
        self.interesting: set[str] = set()
        for path in [line.path for line in lines] + [
            *model.roots,
            *model.includes,
            *model.excludes,
        ]:
            self.interesting.update(ancestors(path))

    def differs(self, path: str) -> bool:
        return self.old.on(path) != decide(self.model, path)

    def skip(self, directory: str) -> bool:
        return (
            directory not in self.interesting
            and not self.old.on(directory)
            and not decide(self.model, directory)
        )

    def probes(self) -> list[str]:
        """The paths the lines themselves name, so a line for a game not on the hub is proven."""
        return sorted(self.interesting)
