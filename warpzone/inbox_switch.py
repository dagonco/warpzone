"""Nintendo Switch support for `warpzone inbox`: identify, decompress, name and place.

A Switch dump (`.nsp`, or `.nsz` which is compressed) is identified by its TitleID and
version, named after blawar/titledb and filed in the library:

    roms/switch/<Title> [<TitleID>][v<version>][BASE].nsp
    extras/switch/updates/<Title>/<Title> [<UpdateTitleID>][v<version>][UPD].nsp
    extras/switch/dlc/<Title>/<Title> - <DLC name> [<DlcTitleID>][v<version>][DLC].nsp

The TitleID and version live in the CNMT, which sits inside an encrypted NCA, so they are
read with `nsz --info` (needs the console keys file). Without `nsz` or the keys, an `.nsp`
falls back to the `[TitleID][vN]` in its file name and the report says so. `.nsz` files are
decompressed with `nsz -D`, checked with `nsz -V` and by parsing the PFS0 header, and only
the `.nsp` reaches the library. `.xci` is never converted: it goes to review.

`inbox.py` loads `HANDLER` late (`kind_handlers`), so this module can import it. The
decisions (`classify`, `base_title_id`, `normalize_text`, `dlc_display_name`, `build_names`,
`parse_nsz_info`, `parse_pfs0`, `ids_from_filename`, `verdict`) are pure; the rest touches
the disk or runs `nsz`. The keys file is only passed to `nsz` by path: its content is never
read, printed or logged.
See docs/design.md.
"""

from __future__ import annotations

import hashlib
import html
import json
import os
import re
import shutil
import struct
import subprocess
import unicodedata
import weakref
import zipfile
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from warpzone import inbox
from warpzone.inbox import Context, InboxError, ItemFailure, ItemResult, Options, UnitResult

EXTENSIONS = frozenset({".nsp", ".nsz", ".xci", ".xcz"})
BASE, UPD, DLC = "BASE", "UPD", "DLC"
FOLDERS = {UPD: "extras/switch/updates", DLC: "extras/switch/dlc"}
BASE_FOLDER = "roms/switch"
CHUNK = 8 * 1024 * 1024
NAME_BYTES = 240  # a file name longer than 255 bytes does not fit on most file systems
TITLE_BYTES = 150
MAX_PFS0_FILES = 4096
SHORT_ACRONYM = 3  # all-caps words up to this long may be acronyms
# Small words of a title: written in lower case inside it, even when the source shouts.
SMALL_WORDS = frozenset(
    {"an", "and", "at", "by", "for", "from", "in", "of", "on", "or", "the", "to", "vs", "with"}
)
# Short all-caps words that are acronyms although they have a vowel. Any other short word
# with a vowel is an ordinary word (`PIT`) and loses its capitals.
KNOWN_ACRONYMS = frozenset(
    {"bgm", "dlc", "exp", "npc", "pvp", "rpg", "usa", "uk", "tv", "cd", "dvd", "pc", "ai", "ar"}
    | {"hp", "mp", "ui", "vr", "ex", "ost", "cpu", "gps", "led", "nes", "gba", "snes"}
)
# The CNMT title type (printed by nsz in hex) of each kind.
TITLE_TYPES = {0x80: BASE, 0x81: UPD, 0x82: DLC}
XCI_REASON = (
    "XCI cartridge images are not converted: no tool warpzone uses turns them into an NSP "
    "cleanly. Convert it to .nsp yourself and drop that, or keep the XCI outside the library"
)

_ID = re.compile(r"\[([0-9A-Fa-f]{16})\]\[v(\d+)\]")
_ROMAN = re.compile(r"^(?=[IVXLCDM]+$)M{0,4}(CM|CD|D?C{0,3})(XC|XL|L?X{0,3})(IX|IV|V?I{0,3})$")
_WORD = re.compile(r"[^\W\d_]+(?:'[^\W\d_]+)*")
_REGION_FILE = re.compile(r"^[A-Za-z]{2}\.[a-z]{2}\.json$")
_UNSAFE = re.compile(r'[\\/|*?<>"\x00-\x1f\x7f]')
# Typographic punctuation by code point: curly quotes, dashes, odd spaces, the ellipsis.
_PUNCTUATION = {
    **dict.fromkeys((0x2018, 0x2019, 0x201A, 0x201B, 0x2032), "'"),
    **dict.fromkeys((0x201C, 0x201D, 0x201E, 0x2033), '"'),
    **dict.fromkeys((0x2010, 0x2011, 0x2012, 0x2013, 0x2014, 0x2212), "-"),
    **dict.fromkeys((0x00A0, 0x2009, 0x202F), " "),
    0x2026: "...",
}
# Trademark, registered, copyright and service-mark signs are dropped.
_TRADEMARKS = dict.fromkeys((0x2122, 0x00AE, 0x00A9, 0x2120))


def default_keys(home: Path) -> Path:
    """Where the console keys file is looked for when `[inbox] switch_keys` is not set."""
    return home / ".switch" / "prod.keys"


class SwitchError(Exception):
    """One file cannot be placed; the message is the reason given for review."""


# ------------------------------------------------------------------ pure decisions


def classify(title_id: str) -> str:
    """BASE, UPD or DLC from the TitleID: a base game ends in 000, an update in 800."""
    tail = title_id[-3:]
    if tail == "000":
        return BASE
    if tail == "800":
        return UPD
    return DLC


def base_title_id(title_id: str) -> str:
    """The TitleID of the game an update or DLC belongs to (the TitleID itself for a game).

    An update is its game's id plus 0x800; a DLC lives in the id range right after its
    game's (the game's id plus 0x1000, plus the DLC index)."""
    kind = classify(title_id)
    if kind == BASE:
        return title_id
    value = int(title_id, 16)
    if kind == UPD:
        return f"{value - 0x800:016X}"
    return f"{(value & ~0xFFF) - 0x1000:016X}"


def ids_from_filename(name: str) -> tuple[str, int] | None:
    """(TitleID, version) from a `[0100...][v0]` in a file name."""
    m = _ID.search(name)
    return (m.group(1).upper(), int(m.group(2))) if m else None


def _shouting(word: str) -> bool:
    return word.isupper() and not _ROMAN.match(word)


def _acronym(plain: str) -> bool:
    """Is a short all-caps word an acronym (kept) rather than a shouted word?"""
    low = plain.lower()
    return len(plain) == 1 or low in KNOWN_ACRONYMS or not set(low) & set("aeiou")


def _unshout(text: str) -> str:
    """Words written in capitals become ordinary words. Roman numerals and short acronyms
    stay; the small words of a title (`OF`, `THE`) go to lower case inside it."""
    first = True

    def fix(m: re.Match[str]) -> str:
        nonlocal first
        word, lead = m.group(0), first
        first = False
        plain = word.replace("'", "")
        if not _shouting(plain):
            return word
        if plain.lower() in SMALL_WORDS:
            return word.capitalize() if lead else word.lower()
        if len(plain) > SHORT_ACRONYM or not _acronym(plain):
            return word.capitalize()
        return word

    return _WORD.sub(fix, text)


def normalize_text(text: str) -> str:
    """A titledb name as the library writes it: ASCII punctuation, no trademark signs, no
    path-hostile characters, no all-caps words (short acronyms and roman numerals stay)."""
    s = unicodedata.normalize("NFC", html.unescape(text))
    s = s.translate(_TRADEMARKS).translate(_PUNCTUATION)
    s = re.sub(r"\s*:\s*", " - ", s)
    s = _UNSAFE.sub(lambda m: "-" if m.group(0) in "\\/|" else "", s)
    s = re.sub(r"\s+", " ", s)
    s = re.sub(r"(?:\s*-)+\s*$", "", s)  # a dangling " -" left by a trailing colon
    s = _unshout(s)
    return s.strip(" .")


def truncate_bytes(text: str, limit: int) -> str:
    """`text` cut at a character boundary to at most `limit` UTF-8 bytes."""
    data = text.encode("utf-8")
    if len(data) <= limit:
        return text
    return data[:limit].decode("utf-8", errors="ignore").rstrip(" .-")


def strip_title(title: str, name: str) -> str:
    """`name` without a leading `title` (and the separator after it). A title that has a
    subtitle (`Game - Edition`) is also matched by its first part (`Game`). `name` itself
    when nothing matches, or stripping would leave nothing or cut a word."""
    heads = [title]
    while " - " in heads[-1]:
        heads.append(heads[-1].rsplit(" - ", 1)[0])
    for head in heads:
        if name.lower().startswith(head.lower()):
            rest = name[len(head) :]
            if rest and rest[0] in " -,":
                rest = rest.lstrip(" -,")
                if rest:
                    return rest
    return name


def dlc_display_name(
    title: str, own_name: str | None, bundle: tuple[str, int, int] | None
) -> str | None:
    """The DLC part of the file name: its own titledb name, else `<bundle> (part N of M)`
    when titledb only lists it inside a bundle; None when titledb knows neither."""
    if own_name:
        return strip_title(title, normalize_text(own_name)) or None
    if bundle:
        name, index, total = bundle
        return f"{strip_title(title, normalize_text(name))} (part {index} of {total})"
    return None


def build_names(
    kind: str, title_id: str, version: int, title: str, dlc_name: str | None = None
) -> tuple[str, str]:
    """(library folder, file name) for a game; `title` is the already normalized name of
    the game. Updates and DLC get a sub-folder named after the game."""
    title = truncate_bytes(title, TITLE_BYTES)
    tag = f"[{title_id}][v{version}][{kind}].nsp"
    if kind == DLC:
        if not dlc_name:
            raise ValueError("a DLC needs a name")
        room = NAME_BYTES - len(tag.encode()) - len(title.encode()) - len(" -  ")
        head = f"{title} - {truncate_bytes(dlc_name, max(room, 20))}"
    else:
        head = title
    folder = BASE_FOLDER if kind == BASE else f"{FOLDERS[kind]}/{title}"
    return folder, f"{head} {tag}"


def library_folder(kind: str) -> str:
    return BASE_FOLDER if kind == BASE else FOLDERS[kind]


def verdict(exists: bool, identical: bool | None) -> str:
    """ "place", "already" (the library has the same bytes) or "conflict"."""
    if not exists:
        return "place"
    return "already" if identical else "conflict"


@dataclass(frozen=True)
class Cnmt:
    title_id: str
    version: int
    title_type: int | None = None


def parse_nsz_info(text: str) -> list[Cnmt]:
    """The CNMT records in the output of `nsz --info --depth 3`: a `Cnmt` heading followed
    by `titleId = ...`, `version = ...` and `titleType = ...` (the last two in hex).
    Everything else in the output is ignored, key lines included."""
    records: list[Cnmt] = []
    current: dict[str, int | str] | None = None

    def close() -> None:
        nonlocal current
        if current and "tid" in current and "version" in current:
            kind = current.get("type")
            records.append(
                Cnmt(
                    str(current["tid"]).upper(),
                    int(current["version"]),
                    kind if isinstance(kind, int) else None,
                )
            )
        current = None

    for raw in text.splitlines():
        line = raw.strip()
        if line == "Cnmt":
            close()
            current = {}
            continue
        if current is None:
            continue
        key, sep, value = (p.strip() for p in line.partition("="))
        try:
            if sep and key == "titleId" and "tid" not in current:
                current["tid"] = value if re.fullmatch(r"[0-9A-Fa-f]{16}", value) else ""
            elif sep and key == "version" and "version" not in current:
                current["version"] = int(value, 16)
            elif sep and key == "titleType" and "type" not in current:
                current["type"] = int(value, 16)
        except ValueError:
            current = None
            continue
        if line.startswith("File Path:"):
            close()
    close()
    return [r for r in records if r.title_id]


def scrub(text: str) -> str:
    """Output of `nsz` that is safe to show: nsz prints key material in some of its
    reports, so every line that mentions a key is dropped."""
    return "\n".join(line for line in text.splitlines() if "key" not in line.lower())


def parse_pfs0(head: bytes, file_size: int) -> list[str]:
    """The member names of a PFS0 container whose first bytes are `head`. Raises
    SwitchError when the header does not describe a file of `file_size` bytes."""
    if len(head) < 16 or head[:4] != b"PFS0":
        raise SwitchError("not a PFS0 container (bad magic)")
    count, strings = struct.unpack("<II", head[4:12])
    if not 0 < count <= MAX_PFS0_FILES:
        raise SwitchError(f"PFS0 header lists {count} file(s)")
    table = 16 + 24 * count
    data_start = table + strings
    if data_start > file_size or len(head) < data_start:
        raise SwitchError("PFS0 header is larger than the file")
    names: list[str] = []
    for i in range(count):
        offset, size, name_at, _ = struct.unpack("<QQII", head[16 + 24 * i : 40 + 24 * i])
        if data_start + offset + size > file_size:
            raise SwitchError("PFS0 member runs past the end of the file")
        end = head.find(b"\0", table + name_at, data_start)
        if name_at >= strings or end < 0:
            raise SwitchError("PFS0 member name is outside the string table")
        names.append(head[table + name_at : end].decode("utf-8", errors="replace"))
    return names


# ------------------------------------------------------------------ titledb


def region_files(directory: Path) -> list[Path]:
    """The region files of a titledb clone, US English first, then the other English ones,
    then the rest."""
    found = [p for p in directory.iterdir() if p.is_file() and _REGION_FILE.match(p.name)]

    def rank(p: Path) -> tuple[int, int, str]:
        english = p.name.split(".")[1] == "en"
        return (0 if p.name == "US.en.json" else 1 if english else 2, 0, p.name)

    return sorted(found, key=rank)


@dataclass
class Region:
    names: dict[str, str] = field(default_factory=dict)  # TitleID -> name
    solo: set[str] = field(default_factory=set)  # TitleIDs with a standalone entry
    bundles: dict[str, tuple[str, int, int]] = field(default_factory=dict)  # part -> bundle


def parse_region(data: object) -> Region:
    """Names and bundle membership from one titledb region file (keyed by nsuId).

    An entry with no `ids`, or only its own, is standalone. An entry whose `ids` lists
    several is a bundle: when its own id comes first it is the pack itself and is left out
    of the parts, otherwise it is one of them; the parts are numbered in listed order.
    A title listed twice keeps its first full-game entry over a demo."""
    region = Region()
    if not isinstance(data, dict):
        return region
    demos: set[str] = set()
    for entry in data.values():
        if not isinstance(entry, dict):
            continue
        tid, name = entry.get("id"), entry.get("name")
        if not (isinstance(tid, str) and isinstance(name, str) and name.strip()):
            continue
        tid = tid.upper()
        demo = bool(entry.get("isDemo"))
        if tid not in region.names or (tid in demos and not demo):
            region.names[tid] = name
            if demo:
                demos.add(tid)
            else:
                demos.discard(tid)
        raw = entry.get("ids")
        ids = list(dict.fromkeys(i.upper() for i in raw if isinstance(i, str))) if raw else []
        if not ids or ids == [tid]:
            region.solo.add(tid)
            continue
        parts = ids[1:] if ids[0] == tid else ids
        for index, part in enumerate(parts, 1):
            region.bundles.setdefault(part, (name, index, len(parts)))
    return region


def load_region(path: Path, cache_dir: Path | None) -> Region:
    """`parse_region` of a file, cached as small JSON keyed by the file's size and mtime."""
    stat = path.stat()
    stamp = [stat.st_size, stat.st_mtime_ns]
    cache = cache_dir / (path.name + ".cache") if cache_dir else None
    if cache and cache.is_file():
        try:
            saved = json.loads(cache.read_text(encoding="utf-8"))
            if saved.get("stamp") == stamp:
                return Region(
                    saved["names"],
                    set(saved["solo"]),
                    {k: (v[0], v[1], v[2]) for k, v in saved["bundles"].items()},
                )
        except (OSError, ValueError, KeyError, IndexError, TypeError):
            pass
    try:
        region = parse_region(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, ValueError) as exc:
        raise SwitchError(f"titledb: cannot read {path.name} ({exc})") from exc
    if cache:
        try:
            cache.parent.mkdir(parents=True, exist_ok=True)
            tmp = cache.with_name(cache.name + ".tmp")
            tmp.write_text(
                json.dumps(
                    {
                        "stamp": stamp,
                        "names": region.names,
                        "solo": sorted(region.solo),
                        "bundles": region.bundles,
                    }
                ),
                encoding="utf-8",
            )
            os.replace(tmp, cache)
        except OSError:
            pass
    return region


class TitleDb:
    """Name lookups over a local clone of blawar/titledb. Region files are read one at a
    time, in `region_files` order, and only as far as a lookup needs."""

    def __init__(self, directory: Path, cache_dir: Path | None = None) -> None:
        self.directory = directory
        self.cache_dir = cache_dir
        try:
            self.files = region_files(directory)
        except OSError as exc:
            raise SwitchError(f"titledb_dir {directory}: {exc.strerror or exc}") from exc
        if not self.files:
            raise SwitchError(f"no region files (like US.en.json) in titledb_dir {directory}")
        self.loaded: list[Region] = []

    def _more(self) -> bool:
        if len(self.loaded) >= len(self.files):
            return False
        self.loaded.append(load_region(self.files[len(self.loaded)], self.cache_dir))
        return True

    def _first(self, found: Callable[[Region], object]) -> object:
        i = 0
        while True:
            while i >= len(self.loaded):
                if not self._more():
                    return None
            hit = found(self.loaded[i])
            if hit:
                return hit
            i += 1

    def name(self, title_id: str) -> str | None:
        """The titledb name of a game (or of a pack, when that is all there is)."""
        hit = self._first(lambda r: r.names.get(title_id))
        return hit if isinstance(hit, str) else None

    def dlc(self, title_id: str) -> tuple[str | None, tuple[str, int, int] | None]:
        """(own name, bundle part) of a DLC: the first region that knows it decides. A
        standalone entry names it; otherwise it is a part of a bundle; otherwise a pack
        entry names it."""

        def found(r: Region) -> object:
            if title_id in r.solo:
                return (r.names[title_id], None)
            if title_id in r.bundles:
                return (None, r.bundles[title_id])
            if title_id in r.names:
                return (r.names[title_id], None)
            return None

        hit = self._first(found)
        return hit if isinstance(hit, tuple) else (None, None)


# ------------------------------------------------------------------ environment


@dataclass(frozen=True)
class SwitchEnv:
    library: Path  # <hub root>/library
    nsz: str | None
    keys: Path | None
    titledb_dir: Path | None
    apply: bool
    copy: Callable[[Path, Path], None]  # copies and reads back; raises SwitchError
    cache_dir: Path | None = None


@dataclass
class SwitchState:
    """What is shared by the files of one run: the titledb and an index of the library."""

    titledb: TitleDb | None = None
    index: dict[tuple[str, int], list[Path]] | None = None


@dataclass
class SwitchOutcome:
    source: str  # the file as it was dropped
    outcome: str  # placed | already | planned | exists | review
    kind: str = ""
    folder: str = ""  # library folder of an update or DLC; "" for a game (roms/switch)
    entry: str = ""  # name inside the folder
    notes: list[str] = field(default_factory=list)
    reason: str = ""


def _keys_ok(env: SwitchEnv) -> bool:
    return bool(env.keys) and env.keys.is_file()


def run_nsz(command: Sequence[str]) -> tuple[int, str]:
    proc = subprocess.run(
        list(command),
        capture_output=True,
        text=True,
        check=False,
        errors="replace",
        stdin=subprocess.DEVNULL,
    )
    return proc.returncode, (proc.stdout or "") + "\n" + (proc.stderr or "")


def _tail(text: str) -> str:
    lines = [line for line in scrub(text.replace("\r", "\n")).splitlines() if line.strip()]
    return lines[-1].strip()[:200] if lines else "no output"


def read_cnmt(env: SwitchEnv, path: Path) -> list[Cnmt]:
    """The CNMT records of a `.nsp` or `.nsz`, read by `nsz --info`."""
    assert env.nsz and env.keys
    code, text = run_nsz([env.nsz, "-i", "--depth", "3", "--keys", str(env.keys), str(path)])
    if code != 0:
        raise SwitchError(f"nsz could not read {path.name} ({_tail(text)})")
    return parse_nsz_info(text)


def check_pfs0_file(path: Path) -> list[str]:
    size = path.stat().st_size
    with path.open("rb") as f:
        head = f.read(16)
        if len(head) == 16 and head[:4] == b"PFS0":
            count, strings = struct.unpack("<II", head[4:12])
            if 0 < count <= MAX_PFS0_FILES:
                head += f.read(24 * count + strings)
    return parse_pfs0(head, size)


def decompress(env: SwitchEnv, source: Path, scratch: Path) -> Path:
    """`nsz -D` into `scratch`, then proof that the result is sound: nsz exited cleanly and
    reported DONE, the PFS0 header parses, and `nsz -V` verifies every NCA hash. (nsz exits 0
    on some corrupted inputs, which is why it is checked a second time.)"""
    assert env.nsz and env.keys
    out = scratch / "dec"
    out.mkdir(parents=True, exist_ok=True)
    code, text = run_nsz([env.nsz, "-D", "--keys", str(env.keys), "-o", str(out), str(source)])
    produced = sorted(out.glob("*.nsp"))
    if code != 0 or "[DONE]" not in text or len(produced) != 1:
        raise SwitchError(f"nsz could not decompress {source.name} ({_tail(text)})")
    nsp = produced[0]
    try:
        check_pfs0_file(nsp)
    except SwitchError as exc:
        raise SwitchError(f"decompressed {source.name} is not a valid NSP ({exc})") from exc
    code, text = run_nsz([env.nsz, "-V", "--keys", str(env.keys), str(nsp)])
    if code != 0 or "[VERIFIED]" not in text:
        raise SwitchError(f"decompressed {source.name} failed nsz verification ({_tail(text)})")
    return nsp


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as f:
        while chunk := f.read(CHUNK):
            digest.update(chunk)
    return digest.hexdigest()


def same_bytes(a: Path, b: Path) -> bool:
    try:
        return a.stat().st_size == b.stat().st_size and sha256(a) == sha256(b)
    except OSError:
        return False


def library_index(library: Path) -> dict[tuple[str, int], list[Path]]:
    """Every Switch file of the library by (TitleID, version), wherever it is named."""
    found: dict[tuple[str, int], list[Path]] = {}
    roots = [library / BASE_FOLDER, *(library / f for f in FOLDERS.values())]
    for root in roots:
        for base, dirs, names in os.walk(root):
            dirs[:] = [d for d in dirs if d != "@eaDir"]
            for name in names:
                ids = ids_from_filename(name)
                if ids and name.lower().endswith(".nsp"):
                    found.setdefault(ids, []).append(Path(base) / name)
    return found


# ------------------------------------------------------------------ one file


def identify(env: SwitchEnv, path: Path, rel: str, notes: list[str]) -> Cnmt:
    """TitleID and version of a file: from the CNMT when nsz and the keys are there, else
    (for an `.nsp`) from its name, with a note saying so."""
    named = ids_from_filename(path.name)
    if env.nsz and _keys_ok(env):
        records = read_cnmt(env, path)
        if len(records) > 1:
            ids = ", ".join(r.title_id for r in records)
            raise SwitchError(f"{rel}: holds {len(records)} titles ({ids}); split it first")
        if records:
            record = records[0]
            if named and named != (record.title_id, record.version):
                notes.append(
                    f"the file name says {named[0]} v{named[1]}, the file itself "
                    f"{record.title_id} v{record.version}; using the file"
                )
            return record
        if path.suffix.lower() == ".nsz":
            raise SwitchError(f"{rel}: nsz found no CNMT in it")
        notes.append("nsz found no CNMT; TitleID and version taken from the file name")
    elif path.suffix.lower() == ".nsz":
        raise SwitchError(f"{rel}: an .nsz needs nsz and the keys file ([inbox] switch_keys)")
    else:
        notes.append(
            "TitleID and version taken from the file name (nsz or the keys file not available)"
        )
    if not named:
        raise SwitchError(
            f"{rel}: cannot tell what it is: no nsz and keys to read it, and no "
            "[TitleID][vN] in its name"
        )
    return Cnmt(named[0], named[1])


def resolve_names(db: TitleDb, cnmt: Cnmt) -> tuple[str, str, str]:
    """(kind, library folder, file name) for an identified file, from titledb."""
    kind = classify(cnmt.title_id)
    if cnmt.title_type in TITLE_TYPES and TITLE_TYPES[cnmt.title_type] != kind:
        raise SwitchError(
            f"{cnmt.title_id}: the CNMT says {TITLE_TYPES[cnmt.title_type]} but the TitleID "
            f"says {kind}"
        )
    game = base_title_id(cnmt.title_id)
    raw = db.name(game)
    if not raw:
        what = "the game" if kind == BASE else f"its game ({game})"
        raise SwitchError(f"{cnmt.title_id}: {what} is not in titledb")
    title = normalize_text(raw)
    if not title:
        raise SwitchError(f"{game}: the titledb name {raw!r} is empty once normalized")
    dlc: str | None = None
    if kind == DLC:
        dlc = dlc_display_name(title, *db.dlc(cnmt.title_id))
        if not dlc:
            raise SwitchError(f"{cnmt.title_id}: this DLC is not in titledb")
    folder, name = build_names(kind, cnmt.title_id, cnmt.version, title, dlc)
    return kind, folder, name


def _titledb(env: SwitchEnv, state: SwitchState) -> TitleDb:
    if state.titledb is None:
        if not env.titledb_dir:
            raise SwitchError("titledb_dir is not set in [inbox]; Switch games cannot be named")
        if not env.titledb_dir.is_dir():
            raise SwitchError(f"titledb_dir {env.titledb_dir} is not a folder")
        state.titledb = TitleDb(env.titledb_dir, env.cache_dir)
    return state.titledb


def process_file(
    env: SwitchEnv, state: SwitchState, path: Path, rel: str, scratch: Path
) -> SwitchOutcome:
    """Identify one dropped file and, with `apply`, place it. Never raises for a problem
    with the file: the outcome is `review` with the reason."""
    out = SwitchOutcome(rel, "review", entry=rel)
    try:
        _process(env, state, path, rel, scratch, out)
    except SwitchError as exc:
        out.outcome, out.reason = "review", str(exc)
    except OSError as exc:
        out.outcome, out.reason = "review", f"{rel}: {exc.strerror or exc}"
    return out


def _process(
    env: SwitchEnv, state: SwitchState, path: Path, rel: str, scratch: Path, out: SwitchOutcome
) -> None:
    ext = path.suffix.lower()
    if ext in (".xci", ".xcz"):
        raise SwitchError(f"{rel}: {XCI_REASON}")
    cnmt = identify(env, path, rel, out.notes)
    db = _titledb(env, state)
    kind, folder, name = resolve_names(db, cnmt)
    out.kind = kind
    rel_folder = folder
    out.folder = "" if kind == BASE else FOLDERS[kind]
    out.entry = name if kind == BASE else f"{rel_folder.rsplit('/', 1)[1]}/{name}"
    dest = env.library / rel_folder / name
    if state.index is None:
        state.index = library_index(env.library)
    known = [p for p in state.index.get((cnmt.title_id, cnmt.version), []) if p.exists()]
    if ext == ".nsz":
        out.notes.append("compressed: it is decompressed to .nsp for the library")
    if not env.apply:
        out.outcome = "exists" if known or dest.exists() else "planned"
        if out.outcome == "exists":
            out.notes.append("already in the library (content is compared with --apply)")
        return
    source = decompress(env, path, scratch) if ext == ".nsz" else path
    if ext == ".nsp":
        check_pfs0_file(source)
    existing = dest if dest.exists() else known[0] if known else None
    state_ = verdict(existing is not None, same_bytes(existing, source) if existing else None)
    if state_ == "already":
        out.outcome = "already"
        return
    if state_ == "conflict":
        assert existing is not None
        shown = existing.relative_to(env.library).as_posix()
        raise SwitchError(
            f"{name}: a different file for {cnmt.title_id} v{cnmt.version} is already in the "
            f"library ({shown})"
        )
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_name(dest.name + ".part")
    part.unlink(missing_ok=True)
    try:
        env.copy(source, part)
        if dest.exists():
            raise SwitchError(f"{name}: appeared in the library while copying")
        os.rename(part, dest)
    finally:
        part.unlink(missing_ok=True)
    state.index.setdefault((cnmt.title_id, cnmt.version), []).append(dest)
    out.outcome = "placed"
    shutil.rmtree(scratch, ignore_errors=True)  # a decompressed copy can be many GiB


# ------------------------------------------------------------------ the inbox handler

_STATE: weakref.WeakKeyDictionary[Context, SwitchState] = weakref.WeakKeyDictionary()


def _copy(src: Path, dest: Path) -> None:
    try:
        inbox.copy_verified(src, dest)
    except ItemFailure as exc:
        raise SwitchError(str(exc)) from exc


def is_switch_name(name: str) -> bool:
    return os.path.splitext(name)[1].lower() in EXTENSIONS


class SwitchHandler:
    """Switch dumps: an `.nsp` / `.nsz` / `.xci` / `.xcz` in the inbox, or a folder or archive
    holding at least one. The item is taken over whole: its Switch files are placed and
    anything else in it is named in the review reason."""

    def validate(self, opts: Options) -> None:
        s = opts.settings
        if s.titledb_dir and not Path(s.titledb_dir).is_dir():
            raise InboxError(f"[inbox] titledb_dir: {s.titledb_dir} is not a folder")
        if s.switch_keys and not Path(s.switch_keys).is_file():
            raise InboxError(f"[inbox] switch_keys: {s.switch_keys} is not a file")

    def claims(self, ctx: Context, item: Path) -> bool:
        if item.is_symlink():
            return False
        if item.is_file():
            return (
                self._archive_has_switch(ctx, item)
                if _is_archive(item)
                else is_switch_name(item.name)
            )
        for _, dirs, names in os.walk(item):
            dirs[:] = [d for d in dirs if not inbox.skip_name(d)]
            if any(is_switch_name(n) for n in names if not inbox.skip_name(n)):
                return True
        return False

    def _archive_has_switch(self, ctx: Context, item: Path) -> bool:
        try:
            if item.suffix.lower() == ".zip":
                with zipfile.ZipFile(item) as z:
                    return any(is_switch_name(n) for n in z.namelist())
            if ctx.tools.bsdtar:
                code, text = inbox._run([ctx.tools.bsdtar, "-tf", str(item)])
                return code == 0 and any(is_switch_name(n) for n in text.splitlines())
        except (zipfile.BadZipFile, OSError):
            pass
        return False

    def process(self, ctx: Context, item: Path, work: Path, result: ItemResult) -> None:
        opts = ctx.opts
        s = opts.settings
        env = SwitchEnv(
            library=opts.root / "library",
            nsz=ctx.tools.nsz,
            keys=Path(s.switch_keys) if s.switch_keys else default_keys(Path.home()),
            titledb_dir=Path(s.titledb_dir) if s.titledb_dir else None,
            apply=opts.apply,
            copy=_copy,
            cache_dir=opts.cache_dir.parent / "titledb" if opts.cache_dir else None,
        )
        state = _STATE.setdefault(ctx, SwitchState())
        staged, notes, failures = inbox.stage_item(item, work, ctx.tools)
        result.notes.extend(notes)
        if failures:
            result.reasons.extend(failures)
            return
        others = [x.rel for x in staged if not is_switch_name(x.path.name)]
        others = [
            r for r in others if os.path.splitext(r)[1].lower() not in inbox.SIDECAR_EXTENSIONS
        ]
        for n, x in enumerate(f for f in staged if is_switch_name(f.path.name)):
            out = process_file(env, state, x.path, x.rel, work / f"sw{n}")
            unit = UnitResult(
                "switch",
                out.entry,
                out.outcome,
                notes=out.notes,
                reason=out.reason,
                folder=out.folder,
            )
            result.units.append(unit)
            if out.outcome == "review":
                result.reasons.append(out.reason)
        if others:
            shown = ", ".join(others[:5]) + (", ..." if len(others) > 5 else "")
            result.reasons.append(f"not a Switch file, drop it as its own item: {shown}")


def _is_archive(path: Path) -> bool:
    return path.suffix.lower() in inbox.ARCHIVE_EXTENSIONS


HANDLER = SwitchHandler()
