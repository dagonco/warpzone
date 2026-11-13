---
title: "Save conversion"
---

Some emulators keep a cartridge save in a shape of their own. The hub keeps one canonical shape,
the raw dump, and a converter on the device that needs it reconciles the two, so a save made in
DraStic on the Anbernic RG351MP reaches melonDS on the Steam Deck and Steam Machine and the
other way round, with no manual step. It is data-driven: a profile names the emulator's format,
folder and process, and nothing in the code tests a device or profile name. Today there is one
format, `desmume-dsv`, and one rule, DraStic.

## Format

The hub stores Nintendo DS cartridge saves as raw dumps, `<game>.sav` in `saves/melonds/saves`
(the Syncthing folder `wz-save-melonds-saves`, which `steamos-emudeck` syncs as
`[saves.melonds-saves]`). A raw dump is exactly the cartridge memory, so its size is a power of
two from 512 bytes to 8 MiB.

DraStic keeps `/roms/nds/backup/<game>.dsv` and cannot read anything else there. A `.dsv` is the
same raw dump followed by a 122-byte footer from DeSmuME:

| Part | Size |
|---|---|
| Text `\|<--Snip above here to create a raw sav by excluding this DeSmuME savedata footer:` | 82 bytes |
| `size`, `padSize`, `type`, `addr_size`, `mem_size` (little-endian `u32`) | 20 bytes |
| Version (little-endian `u32`, 0) | 4 bytes |
| Cookie `\|-DESMUME SAVE-\|` | 16 bytes |

Names match across emulators (No-Intro ROM names): `<rom stem>.sav` on the hub side and
`<rom stem>.dsv` in DraStic's folder. Checked on a real console: the 65658-byte `.dsv` of a game
(65536 bytes of data and a footer with `size = 65536`) loads in melonDS once the last 122 bytes
are cut, and a raw melonDS `.sav` copied as `.dsv` with no footer loads in DraStic.

The conversion is therefore lossless in both directions, and `saveconvert.py` (pure) does it:

- `.dsv` to raw: if the file ends with the cookie, the footer must be well formed (version 0)
  and what precedes it must be a valid save size; the footer is cut. A file that does not end
  with the cookie is accepted only as a raw dump of a valid size.
- Raw to `.dsv`: the raw data is written alone, unless the `.dsv` being replaced has a valid
  footer whose `size` equals the new data's length, in which case that footer is appended again
  (what DraStic itself would write). A footer for another size is dropped, never reused.
- Anything else is malformed and is skipped with a warning, never converted: a `.dsv` that ends
  with the cookie but whose remaining length is not a save size, a bad footer, an empty file, a
  size that is not a power of two. The same goes for a `.sav` of an invalid size. The two sides
  of one game having different sizes (DraStic's and melonDS's game databases disagree) is also
  a skip: warpzone never trims or pads a save.

## Decision

For each game there are two sides (the raw `.sav` and the emulator's `.dsv`, taken as the raw
data inside it) and a stored base, the sha256 of the raw data the last time both sides were made
equal. Only the raw data is compared, never the footer or the date. `saveconvert.decide` is pure
and says one of:

| Situation | Action |
|---|---|
| Neither file exists | Nothing. |
| One file exists, the other does not | Copy it to the other side. A missing file is never taken as a deletion: warpzone never deletes or propagates a deletion, so a file the user removed comes back from the other side. |
| Both hold the same data | Nothing; the base is recorded. |
| Only one side differs from the base | Copy that side to the other. |
| Both differ from the base (or there is no base and they differ) | The most recently modified file wins (a tie goes to the hub's file). The loser is backed up before it is replaced, and the event is reported. |
| No base, one side is erased flash (every byte `0xFF`) and the other is not | The side with data wins and nothing is reported: an erased file (DraStic creates one when a game starts before any conversion) never overwrites progress. |
| A side is malformed, or the sizes differ | The game is skipped and left untouched. |

The base is recorded when a game is reconciled and kept in the state file (below). It is hashed
data, never a path, so renaming the folders loses nothing.

## When it runs

- **Around DraStic.** `provision device` wraps the `<command>` of the profile's `es_system`
  (`nds`) in the copy of `es_systems.cfg` it writes (see
  [Frontend and RetroArch overrides](/warpzone/development/design/profiles/#frontend-and-retroarch-overrides)),
  so EmulationStation runs `warpzone saves convert <device> --quiet || true` before the original
  command and again after it. The original command keeps its bytes. The wrapper never stops a
  game from starting: `|| true` hides any failure and `--quiet` prints nothing. The run uses the
  interpreter of the timer (`python -m warpzone`) and the same `--config` when the device was
  provisioned with one. A second `provision device` changes nothing. `doctor` compares the file
  without the wrapper and also checks that the wrapper is there.
- **On the timer.** `warpzone frontend refresh` also runs the conversion when the profile has
  `[save_convert.*]` and the device lists `saves`, so the `[artwork] refresh` timer (15 minutes)
  picks up what arrived from the hub while nobody played. It does nothing while DraStic runs.
  The conversion is the first thing `frontend refresh` does, so an error of the local Syncthing
  API in the artwork steps never skips it. It does not need the `frontend` folder group: on a
  device that lists `saves` and whose profile has `[save_convert.*]` and an `[artwork] refresh`
  interval, `provision device` installs that timer and `frontend refresh` runs only the
  conversion (the artwork steps are for a device that lists `frontend`). A profile with no
  `[artwork] refresh` has no timer to run it at, and saves are then converted only around the
  emulator and by hand.
- **By hand.** `warpzone saves convert <device> [--dry-run] [--quiet]`.

All three call the same function, so they agree.

## Safety

- **Never while DraStic runs.** The processes of the rule's `process` list are looked for
  before anything is read for writing, and again right before each write (`safefile.guarded_edit`).
  If it starts in between, nothing more is written. If the process table cannot be read, nothing
  is written either.
- **Atomic writes.** Each file is written to a temporary `.<name>.<pid>-<random>.wz-tmp` beside it (a name of its own for each write, so two writers never touch each other's file), synced,
  checked against the stamp (size, date, sha256) taken when the file was read, and renamed. A
  file that changed after it was read is left alone. The new file gets the modification time of
  the side it was copied from, not the time of the copy: Syncthing settles a conflict between
  two versions of a file by the newest date, so a copy dated "now" would always beat a newer
  save that another device sent in the meantime, and the real newer save would end up in a
  `*.sync-conflict-*` file. A save for which Syncthing still has a `.syncthing.<name>.tmp` in
  the folder (it is being received) is not created meanwhile. The temporary name never syncs: the profile's
  `[saves.*] ignore` list must contain `*.wz-tmp`, and the profile is refused without it.
  Syncthing's own temporary files (`.syncthing.*`), conflict copies (`*.sync-conflict-*`),
  hidden files, directories and symlinks are never saves. A conflict copy found in the saves
  folder or the emulator's folder is not touched, but it is reported in the command's output and
  in the `doctor` line (it means two devices wrote the same save at once, outside this
  converter's rules, and the user picks the copy to keep).
- **A backup before any overwrite.** Whatever file is about to be replaced, on either side,
  is first copied to `~/.local/state/warpzone/save-convert/backups/<rule>/<game>/`, the whole
  file (a `.dsv` with its footer), mode 0600, keeping the last 3 per game. A file that is new on
  its side has nothing to back up. The copy that loses a latest-wins conflict is kept apart, in
  `backups/<rule>.conflicts/<game>/`, with its own limit (the last 10 per game), so ordinary
  sessions never rotate it away; the warning and `doctor` name that folder.
- **The emulator's folder.** It is created, when missing, only to write a save into it; DraStic's
  launcher creates it too.
- **Deletions.** warpzone never deletes a save, and never propagates a deletion.
- **No device names.** The device, the paths and the process names all come from the profile.

## State

`~/.local/state/warpzone/save-convert/` (below `$XDG_STATE_HOME` when set), outside every synced
folder:

- `state.json`: the base of each game by rule, the last run (when, files written, games skipped
  with the reason, errors), and the latest-wins events (at most 20: when, game, which side won).
  A file that cannot be parsed is treated as empty; the next run compares without a base, which
  the rules above make safe (latest wins, with a backup).
- `backups/`: described above.
- `lock`: runs do not overlap (the launch wrapper waits for the timer's run).

`--dry-run` says what it would write and writes nothing, not even the state or the lock.

## Doctor

On a device that lists `saves` and whose profile has `[save_convert.*]`, `warpzone doctor` adds
one line under "Frontend", `Save conversion`: when the last run was and how many files it wrote;
a warning when it never ran, when it skipped games it could not read as a save (with the
names and the reason) or failed on some; and the latest-wins events with the game, the side
that won and where the other copy is. The `ES systems override` line also warns when a named
system lacks the wrapper.

## Profile

```toml
[saves.melonds-saves]
path = "/roms/warpzone/saves/melonds"   # a real folder on the exFAT card, the hub folder joins here
ignore = ["*.wz-tmp"]
create_when = "/roms"

[save_convert.drastic]
format = "desmume-dsv"
saves = "melonds-saves"        # the [saves.*] entry holding the raw .sav files
path = "/roms/nds/backup"      # the emulator's own folder (not synced)
process = ["drastic"]          # nothing is converted while one of these runs
es_system = "nds"              # optional: whose <command> is wrapped
```

`format` must be one of `saveconvert.FORMATS`; `saves` must name an entry with `*.wz-tmp` in
its `ignore`; `path` must be absolute (or start with `~/`) and neither inside the saves folder
nor containing it; `es_system` needs `[es_systems]` and names one system per rule. A new
emulator with the same kind of file is one more value in `FORMATS` and a codec for it.

## Save states

Save states are not portable between emulators and are not converted. DraStic's own
configuration is not touched either, so with its default `backup_in_savestates = 1` each of its
states carries the cartridge save inside it. Loading an old DraStic state therefore rolls the
game's save back to that moment in DraStic's file, and that rollback then travels to the other
devices like any other change (the previous content stays in the backups of the device that
receives it). Set `backup_in_savestates = 0` in `drastic.cfg` to avoid it; warpzone leaves that
choice to the user.
