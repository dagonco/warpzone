"""PS Vita games for `warpzone inbox`: a decrypted dump filed as a game folder plus a launcher.

Vita3K runs the games it finds in `ux0/app/<TITLEID>`, and ES-DE lists only `.psvita` files,
whose launch command (`%INJECT%=%BASENAME%.psvita` in EmuDeck's system) passes the file's
content to Vita3K as the title to run. So a game dump found in a folder or an archive, either
at its root or inside one top folder, becomes:

- `library/roms/psvita/app/<TITLEID>/`: the dump as it is (`eboot.bin`, `sce_sys/param.sfo`
  and the data), copied beside the final name and renamed, so the destination never holds a
  partial game. A console links it into Vita3K's `ux0/app` (`[links.vita3k]` in the profile,
  run by `provision device` and at ES-DE's startup).
- `library/roms/psvita/<Title>.psvita`: a file whose whole content is the title id.
- `library/roms/psvita/license/<TITLEID>/<CONTENT_ID>.rif`: the game's 512-byte licence, when
  the dump brings one (`sce_sys/package/work.bin`, or a `.rif` in the item). A game whose
  `eboot.bin` is still an encrypted SELF cannot start in Vita3K without it, so one that needs
  a licence and has none goes to review. A console links the folder into `ux0/license`.

A dump is recognised by `sce_sys/param.sfo` next to `eboot.bin` with a Vita title id
(`PCS[A-I]nnnnn`). Category `gd` and `gdc` are games; updates (`gp`) and DLC (`ac`) go to
review, and so does a dump that Vita3K would have to install (an `sce_pfs` folder). A Vitamin
dump (it holds `sce_module/steroid.suprx`) always goes to review: Vita3K rejects that module,
so it is never placed even if the game might start. A PS3 `PS3_GAME/PARAM.SFO` is not this
layout. A game already in the library is reported and never overwritten. `.vpk` and `.pkg`
files keep their outcome.

The decisions (`is_title_id`, `category_kind`, `game_roots`, `is_vitamin`, `file_name`,
`eboot_needs_license`, `parse_license`, `license_sources`, `choose_license`) are pure;
`install` touches the disk. See development/design.md.
"""

from __future__ import annotations

import os
import re
import shutil
import struct
import zipfile
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import NamedTuple

from warpzone import inbox, inbox_ps4
from warpzone.inbox import Context, ItemFailure, ItemResult, Options, UnitResult, whitelist_line

APP = "psvita/app"  # inside library/roms/
SYSTEM = "psvita"
SUFFIX = ".psvita"
_TITLE_ID = re.compile(r"PCS[A-I]\d{5}")
EBOOT = "eboot.bin"
SFO = "sce_sys/param.sfo"
# The licence a NoNpDrm dump carries (the same 512 bytes as a `.rif`).
WORK_BIN = "sce_sys/package/work.bin"
# What a dump that Vita3K must install carries.
PFS = "sce_pfs"
LICENSE_DIR = "psvita/license"  # inside library/roms/, then <TITLEID>/<CONTENT_ID>.rif
LICENSE_SIZE = 512
CONTENT_ID_AT = 0x10  # the content id inside a licence: 0x30 bytes, NUL padded
SELF_HEAD = 0x10000  # how much of eboot.bin is read to tell whether it is encrypted
# The module a Vitamin dump carries; Vita3K refuses to load it.
VITAMIN_MODULE = "sce_module/steroid.suprx"
_GAME = {"gd", "gdc"}
_CONTENT_ID = re.compile(rb"[A-Z]{2}\d{4}-(PCS[A-I]\d{5})_\d{2}-[A-Z0-9]{16}")
_SELF_MAGIC = b"SCE\x00"
_SELF_TYPE_APP = 8
_NOT_VITA = (
    "{rel}: its param.sfo names {found}, which is not a Vita title id (PCSA00000 to PCSI99999)"
)


# ------------------------------------------------------------------ pure decisions


def is_title_id(value: object) -> bool:
    return isinstance(value, str) and _TITLE_ID.fullmatch(value) is not None


def category_kind(category: object) -> str:
    """ "game" (`gd`, `gdc`), "update" (`gp`), "dlc" (`ac`) or "other" for the CATEGORY of a
    Vita param.sfo."""
    if not isinstance(category, str):
        return "other"
    if category in _GAME:
        return "game"
    return {"gp": "update", "ac": "dlc"}.get(category, "other")


def game_roots(names: list[str] | tuple[str, ...]) -> list[str]:
    """The folders (`""` for the top, else `Top/`) that hold `eboot.bin` and
    `sce_sys/param.sfo`, among file names with `/` separators: the top level or one folder
    below it. Sorted."""
    files = {n[2:] if n.startswith("./") else n for n in names}
    roots: set[str] = set()
    for name in files:
        parts = name.split("/")
        if len(parts) not in (2, 3) or parts[-2:] != ["sce_sys", "param.sfo"]:
            continue
        prefix = parts[0] + "/" if len(parts) == 3 else ""
        if prefix + EBOOT in files:
            roots.add(prefix)
    return sorted(roots)


def is_vitamin(names: list[str] | tuple[str, ...], root: str) -> bool:
    """Whether the dump under `root` (`""` or `Top/`) holds `sce_module/steroid.suprx`, the
    module of a Vitamin dump, among file names with `/` separators (case does not matter)."""
    wanted = (root + VITAMIN_MODULE).lower()
    return any((n[2:] if n.startswith("./") else n).lower() == wanted for n in names)


class License(NamedTuple):
    content_id: str
    title_id: str


def eboot_needs_license(head: bytes) -> bool:
    """Whether Vita3K needs the game's licence to run this `eboot.bin`, from its first bytes.

    Vita3K (`decrypt_fself`, `packages/src/sce_utils.cpp`) reads a file that starts with
    `SCE\\0` as a SELF: its first segment is plain when the segment info at the header's
    offset 0x58 has `encryption == 2` (field at +0x18). It logs "No klic provided for
    encrypted App" and fails when that segment is not plain, the app info (offset 0x38,
    `self_type` at +12) says APP (8) and the key of the licence is all zeros. The magic alone
    does not tell: a decrypted dump keeps it. Not a SELF (no magic): no licence. A SELF whose
    header cannot be read raises `ValueError`."""
    if head[:4] != _SELF_MAGIC:
        return False
    bad = ValueError("eboot.bin starts like a SELF (SCE header) but its header cannot be read")
    if len(head) < 0x80:
        raise bad
    app_info = struct.unpack_from("<Q", head, 0x38)[0]
    segments = struct.unpack_from("<Q", head, 0x58)[0]
    if app_info + 16 > len(head) or segments + 0x20 > len(head):
        raise bad
    self_type = struct.unpack_from("<I", head, app_info + 12)[0]
    encryption = struct.unpack_from("<Q", head, segments + 0x18)[0]
    return encryption != 2 and self_type == _SELF_TYPE_APP


def parse_license(data: bytes) -> License:
    """The content id and title id of a licence (`.rif` or `work.bin`: 512 bytes, the content id
    at 0x10), or `ValueError` with the reason."""
    if len(data) != LICENSE_SIZE:
        raise ValueError(f"is not {LICENSE_SIZE} bytes long, as a licence is")
    raw = data[CONTENT_ID_AT : CONTENT_ID_AT + 0x30].split(b"\0", 1)[0]
    found = _CONTENT_ID.fullmatch(raw)
    if not found:
        raise ValueError("its content id (at 0x10) is not a Vita one (XXnnnn-PCSAnnnnn_nn-...)")
    return License(raw.decode("ascii"), found.group(1).decode("ascii"))


def license_sources(names: Sequence[str], root: str, title_id: str) -> list[str]:
    """The files of an item that may be the game's licence, sorted: the game's
    `sce_sys/package/work.bin`, and a `.rif` directly in the game folder or beside it (the
    item's top), or in `license/<TITLEID>/` at either place. `names` have `/` separators and
    `root` is `""` or `Top/`."""
    places = {"", root}
    homes = places | {f"{p}license/{title_id}/" for p in places}
    found = set()
    for name in names:
        folder, _, base = name.rpartition("/")
        folder = folder + "/" if folder else ""
        if name == root + WORK_BIN or (base.lower().endswith(".rif") and folder in homes):
            found.add(name)
    return sorted(found)


def choose_license(
    found: Mapping[str, bytes], title_id: str, content_id: str | None
) -> tuple[str, bytes] | None:
    """The one licence among the candidate files (`name -> bytes`), as `(content id, bytes)`,
    `None` when there are none, `ValueError` with the reason when one cannot be used: it is not
    a licence, it belongs to another game, there are several, or its content id is not the one
    in the game's param.sfo (`content_id`; Vita3K looks the file up by that name)."""
    chosen: dict[str, tuple[str, bytes]] = {}
    for name, data in sorted(found.items()):
        try:
            lic = parse_license(data)
        except ValueError as exc:
            raise ValueError(f"{name}: {exc}") from None
        if lic.title_id != title_id:
            raise ValueError(f"{name}: it is the licence of {lic.title_id}, not of {title_id}")
        if lic.content_id in chosen and chosen[lic.content_id][1] != data:
            raise ValueError(
                f"{chosen[lic.content_id][0]} and {name} hold different licences for "
                f"{lic.content_id}"
            )
        chosen.setdefault(lic.content_id, (name, data))
    if not chosen:
        return None
    if len(chosen) > 1:
        raise ValueError(f"several licences for {title_id} ({', '.join(sorted(chosen))})")
    (cid, (name, data)), *_ = chosen.items()
    if content_id and cid != content_id:
        raise ValueError(
            f"{name}: its content id is {cid}, but the game's param.sfo says {content_id}, "
            "and Vita3K looks the licence up by that name"
        )
    return cid, data


def file_name(title: str, title_id: str) -> str:
    """The launcher file name for a game title (`.psvita` included)."""
    return inbox_ps4.shortcut_name(title, title_id) + SUFFIX


def _vitamin_reason(rel: str) -> str:
    return (
        f"{rel}: this is a Vitamin dump (it holds {VITAMIN_MODULE}), and Vita3K does not support "
        "it (it rejects that module), so it is not placed even if the game might start; get a "
        "clean dump instead (a .pkg with its work.bin, or a NoNpDrm dump)"
    )


def _fail_kind(rel: str, kind: str, title_id: str) -> str:
    if kind == "update":
        return (
            f"{rel}: this is an update (patch) for {title_id}; updates are not merged into the "
            "game yet, so apply it in Vita3K by hand"
        )
    if kind == "dlc":
        return (
            f"{rel}: this is DLC (additional content) for {title_id}; DLC is not installed by "
            "inbox yet"
        )
    return f"{rel}: its param.sfo says this is not a game (unexpected category)"


# ------------------------------------------------------------------ disk


def _copy_tree(src: Path, dest: Path) -> None:
    dest.mkdir()
    for base, dirs, names in os.walk(src):
        dirs[:] = sorted(d for d in dirs if not inbox.skip_name(d))
        target = dest / Path(base).relative_to(src)
        for d in dirs:
            (target / d).mkdir()
        for n in sorted(names):
            if not inbox.skip_name(n):
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


def _license_path(ctx: Context, title_id: str, content_id: str) -> Path:
    return ctx.roms.joinpath(*LICENSE_DIR.split("/")) / title_id / f"{content_id}.rif"


def _license_check(path: Path, data: bytes) -> bool:
    """Whether the licence still has to be written; a different file in the way is a failure
    (a licence is never overwritten)."""
    if not path.exists():
        return True
    if path.read_bytes() != data:
        raise ItemFailure(
            f"library/roms/{LICENSE_DIR}/{path.parent.name}/{path.name} already holds a "
            "different licence; it is not overwritten"
        )
    return False


def _write_license(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    part = path.with_name(path.name + ".part")
    try:
        part.write_bytes(data)
        if part.read_bytes() != data:
            raise ItemFailure(f"the copy of {path.name} did not read back identical")
        if path.exists():
            raise ItemFailure(f"{path.name}: appeared in the library while writing")
        os.rename(part, path)
    finally:
        part.unlink(missing_ok=True)


def _launcher(ctx: Context, title: str, title_id: str, res: UnitResult) -> None:
    """Make sure `roms/psvita/` has a `.psvita` file naming the game, never replacing one.

    A file that already holds this title id is kept; one that holds another id (the same
    title in another region) gets the title id in the new file's name."""
    base = file_name(title, title_id)
    for name in (base, f"{base[: -len(SUFFIX)]} [{title_id}]{SUFFIX}"):
        path = ctx.roms / SYSTEM / name
        shown = f"library/roms/{SYSTEM}/{name}"
        res.lines.append(whitelist_line(SYSTEM, name))
        if path.exists():
            if path.read_text(encoding="utf-8", errors="replace").strip() == title_id:
                res.notes.append(f"launcher: {shown} (already there)")
                return
            res.lines.pop()
            continue
        if not ctx.opts.apply:
            res.notes.append(f"launcher: {shown} (would be written)")
        else:
            inbox_ps4._write_text(path, title_id)
            res.notes.append(f"launcher: {shown}")
        return
    raise ItemFailure(f"{base}: a launcher for another game is already there")


def install(
    ctx: Context,
    root: Path,
    rel: str,
    title_id: str,
    title: str,
    license: tuple[str, bytes] | None = None,
) -> UnitResult:
    """File one recognised dump (`root` holds its `eboot.bin`) as a game folder, its licence
    (`(content id, bytes)`, when it has one) and a launcher."""
    apply = ctx.opts.apply
    res = UnitResult(SYSTEM, title_id, "placed" if apply else "planned", folder=f"roms/{APP}")
    res.notes.append(f"detected: Vita game: {title_id} {title}".rstrip())
    res.lines.append(whitelist_line(APP, title_id))
    dest = ctx.roms.joinpath(*APP.split("/")) / title_id
    lic_path = _license_path(ctx, title_id, license[0]) if license else None
    try:
        inbox_ps4._check_tree(root, rel)
        # Look at the licence before touching the library: a different one in the way stops it.
        write_license = bool(license and lic_path and _license_check(lic_path, license[1]))
        if dest.exists():
            res.outcome = "already"
            res.notes.append("kept as it is: the dump is not compared with the library copy")
            kept = inbox_ps4.read_sfo(dest / SFO).get("TITLE")
            title = kept if isinstance(kept, str) and kept else title
        elif apply:
            _place_folder(root, dest)
        if license and lic_path:
            shown = f"library/roms/{LICENSE_DIR}/{title_id}/{lic_path.name}"
            res.lines.append(whitelist_line(LICENSE_DIR, title_id))
            if not write_license:
                res.notes.append(f"licence: {shown} (already there)")
            elif apply:
                _write_license(lic_path, license[1])
                res.notes.append(f"licence: {shown}")
            else:
                res.notes.append(f"licence: {shown} (would be written)")
        _launcher(ctx, title, title_id, res)
    except ItemFailure as exc:
        res.outcome, res.reason = "review", str(exc)
    except OSError as exc:
        res.outcome, res.reason = "review", f"{rel}: {exc.strerror or exc}"
    if res.outcome == "review":
        res.lines.clear()
    return res


# ------------------------------------------------------------------ the inbox handler


def _names_in(ctx: Context, item: Path) -> list[str]:
    """File paths inside a folder (relative to it) or an archive (zip, or 7z / rar with bsdtar)."""
    if item.is_dir():
        names: list[str] = []
        for base, dirs, files in os.walk(item):
            dirs[:] = [d for d in dirs if not inbox.skip_name(d)]
            where = Path(base).relative_to(item).as_posix()
            names.extend(
                n if where == "." else f"{where}/{n}" for n in files if not inbox.skip_name(n)
            )
        return names
    if item.suffix.lower() not in inbox.ARCHIVE_EXTENSIONS:
        return []
    try:
        if item.suffix.lower() == ".zip":
            with zipfile.ZipFile(item) as z:
                return z.namelist()
        if ctx.tools.bsdtar:
            code, text = inbox._run([ctx.tools.bsdtar, "-tf", str(item)])
            return text.splitlines() if code == 0 else []
    except (zipfile.BadZipFile, OSError):
        pass
    return []


def _read_head(path: Path) -> bytes:
    with path.open("rb") as f:
        return f.read(SELF_HEAD)


def _read_license(path: Path) -> bytes:
    """At most one byte more than a licence holds: a longer file is not one."""
    if path.is_symlink():
        raise ValueError(f"{path.name}: a symbolic link is not accepted as a licence")
    with path.open("rb") as f:
        return f.read(LICENSE_SIZE + 1)


def _needs_license(title_id: str) -> str:
    return (
        "its eboot.bin is still encrypted, and Vita3K cannot run it without the game's "
        "licence ('No klic provided for encrypted App'); add its sce_sys/package/work.bin "
        f"(NoNpDrm) or its <CONTENT_ID>.rif (in license/{title_id}/ or next to the game folder)"
    )


class VitaHandler:
    """A folder or archive that holds a Vita game dump (see the module docstring)."""

    def validate(self, opts: Options) -> None:
        return None

    def claims(self, ctx: Context, item: Path) -> bool:
        if item.is_symlink():
            return False
        return bool(game_roots(_names_in(ctx, item)))

    def process(self, ctx: Context, item: Path, work: Path, result: ItemResult) -> None:
        staged, notes, failures = inbox.stage_item(item, work, ctx.tools)
        result.notes.extend(notes)
        if failures:
            result.reasons.extend(failures)
            return
        lead = "" if item.is_dir() else item.name + "/"  # an archive's files sit under its name
        by_rel = {s.rel: s for s in staged}
        names = [s.rel[len(lead) :] for s in staged if s.rel.startswith(lead)]
        roots = game_roots(names)
        if not roots:
            result.reasons.append(
                f"{item.name}: no Vita game found (eboot.bin next to sce_sys/param.sfo)"
            )
            return
        if len(roots) > 1:
            where = ", ".join(r or "." for r in roots)
            result.reasons.append(
                f"{item.name}: {len(roots)} games in one item ({where}); "
                "drop each one as its own item"
            )
            return
        eboot = by_rel[lead + roots[0] + EBOOT].path
        root, rel = eboot.parent, item.name
        values = inbox_ps4.read_sfo(root / SFO)
        title_id, title = values.get("TITLE_ID"), values.get("TITLE")
        if is_vitamin(names, roots[0]):
            reason = _vitamin_reason(rel)
        elif not values:
            reason = f"{rel}: sce_sys/param.sfo cannot be read"
        elif not is_title_id(title_id):
            reason = _NOT_VITA.format(rel=rel, found=title_id or "no title id")
        elif (kind := category_kind(values.get("CATEGORY"))) != "game":
            reason = _fail_kind(rel, kind, str(title_id))
        elif (root / PFS).exists():
            reason = (
                f"{rel}: it holds {PFS}, so Vita3K has to install it; install it there by hand "
                "(inbox only files decrypted dumps)"
            )
        else:
            sources = {
                name: by_rel[lead + name].path
                for name in license_sources(names, roots[0], str(title_id))
            }
            sfo_content = values.get("CONTENT_ID")
            try:
                lic = choose_license(
                    {name: _read_license(path) for name, path in sources.items()},
                    str(title_id),
                    sfo_content if isinstance(sfo_content, str) else None,
                )
                if lic is None and eboot_needs_license(_read_head(eboot)):
                    raise ValueError(_needs_license(str(title_id)))
            except ValueError as exc:
                result.reasons.append(f"{rel}: {exc}")
                return
            except OSError as exc:
                result.reasons.append(f"{rel}: {exc.strerror or exc}")
                return
            unit = install(
                ctx, root, rel, str(title_id), title if isinstance(title, str) else "", lic
            )
            result.units.append(unit)
            if unit.reason:
                result.reasons.append(unit.reason)
            return
        result.reasons.append(reason)


HANDLER = VitaHandler()
