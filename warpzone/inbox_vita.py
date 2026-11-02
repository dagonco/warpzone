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

A dump is recognised by `sce_sys/param.sfo` next to `eboot.bin` with a Vita title id
(`PCS[A-I]nnnnn`). Category `gd` and `gdc` are games; updates (`gp`) and DLC (`ac`) go to
review, and so does a dump that Vita3K would have to install (a `work.bin` licence or an
`sce_pfs` folder). A PS3 `PS3_GAME/PARAM.SFO` is not this layout. A game already in the
library is reported and never overwritten. `.vpk` and `.pkg` files keep their outcome.

The decisions (`is_title_id`, `category_kind`, `game_roots`, `file_name`) are pure; `install`
touches the disk. See docs/design.md.
"""

from __future__ import annotations

import os
import re
import shutil
import zipfile
from pathlib import Path

from warpzone import inbox, inbox_ps4
from warpzone.inbox import Context, ItemFailure, ItemResult, Options, UnitResult, whitelist_line

APP = "psvita/app"  # inside library/roms/
SYSTEM = "psvita"
SUFFIX = ".psvita"
_TITLE_ID = re.compile(r"PCS[A-I]\d{5}")
EBOOT = "eboot.bin"
SFO = "sce_sys/param.sfo"
# What a dump that Vita3K must install (a NoNpDrm / encrypted one) carries.
LICENCE = "sce_sys/package/work.bin"
PFS = "sce_pfs"
_GAME = {"gd", "gdc"}
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


def file_name(title: str, title_id: str) -> str:
    """The launcher file name for a game title (`.psvita` included)."""
    return inbox_ps4.shortcut_name(title, title_id) + SUFFIX


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


def install(ctx: Context, root: Path, rel: str, title_id: str, title: str) -> UnitResult:
    """File one recognised dump (`root` holds its `eboot.bin`) as a game folder and a launcher."""
    apply = ctx.opts.apply
    res = UnitResult(SYSTEM, title_id, "placed" if apply else "planned", folder=f"roms/{APP}")
    res.notes.append(f"detected: Vita game: {title_id} {title}".rstrip())
    res.lines.append(whitelist_line(APP, title_id))
    dest = ctx.roms.joinpath(*APP.split("/")) / title_id
    try:
        inbox_ps4._check_tree(root, rel)
        if dest.exists():
            res.outcome = "already"
            res.notes.append("kept as it is: the dump is not compared with the library copy")
            kept = inbox_ps4.read_sfo(dest / SFO).get("TITLE")
            title = kept if isinstance(kept, str) and kept else title
        elif apply:
            _place_folder(root, dest)
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
        roots = game_roots([s.rel[len(lead) :] for s in staged if s.rel.startswith(lead)])
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
        if not values:
            reason = f"{rel}: sce_sys/param.sfo cannot be read"
        elif not is_title_id(title_id):
            reason = _NOT_VITA.format(rel=rel, found=title_id or "no title id")
        elif (kind := category_kind(values.get("CATEGORY"))) != "game":
            reason = _fail_kind(rel, kind, str(title_id))
        elif (root / LICENCE).exists() or (root / PFS).exists():
            what = LICENCE if (root / LICENCE).exists() else PFS
            reason = (
                f"{rel}: it holds {what}, so Vita3K has to install it; install it there by hand "
                "(inbox only files decrypted dumps)"
            )
        else:
            unit = install(ctx, root, rel, str(title_id), title if isinstance(title, str) else "")
            result.units.append(unit)
            if unit.reason:
                result.reasons.append(unit.reason)
            return
        result.reasons.append(reason)


HANDLER = VitaHandler()
