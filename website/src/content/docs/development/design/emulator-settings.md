---
title: "Emulator settings"
---

Every emulator keeps its settings in its own files on each console. The user changes a
setting in the emulator's own interface, on whichever console is at hand;
`warpzone settings capture` takes it from there to the hub, and every console applies it
with `warpzone settings apply`, which ES-DE runs at startup. The hub holds **key-level
layers**, never whole config files: those also hold controller mappings, GPU ids, window
geometry, absolute paths and logins, which differ from one console to the next.

## Layers on the hub

```
settings/                       hub: send-receive ⇄ devices that subscribe to `settings`
├── common.toml                 keys every device gets
├── <device>.toml               keys of one device; a key here wins over common.toml
└── files/<emulator>/<id>       system data, whole files, the same on every device
```

**Why a folder of its own.** Captures are written on a device, so the folder must be
send-receive there. `wz-tools` is receive-only on devices: a capture written into it would
never leave the console, and a revert would erase it. `wz-settings` (hub path `settings`,
device path `paths.settings`, `~/.local/share/warpzone/settings` in the shipped profile) is
send-receive on both ends like a save folder, and the hub's staggered versioning (30 days)
is the history of every layer change. A device opts in with the `settings` group. Its
ignore lines are the common ones plus a fixed `*.wz-tmp` (the temporary file of an atomic
write). The layer files may also be edited on the hub, as the whitelists are; nothing else
edits them.

Layers, with generic values:

```toml
# settings/common.toml
format = 1

[eden.qt-config]
"[System]language_index" = "1"
"[Renderer]gpu_accuracy" = "1"

[retroarch.cfg]
savestate_auto_save = "true"

[retroarch."opt:Flycast"]
reicast_region = "Europe"

[ryujinx.config]
system_language = "AmericanEnglish"

[xemu.config]
"general.show_welcome" = false
```

```toml
# settings/deck.toml
format = 1

[eden.qt-config]
"[System]use_docked_mode" = "0"
"[Renderer]resolution_setup" = "2"

[ryujinx.config]
docked_mode = false
res_scale = 1
```

- **Shape.** A top-level `format = 1`. Every other top-level table is an emulator id of the
  device's profile, each of its tables a file id (`[<emulator>.<file>]`; an instance of a
  templated file is `<file>:<name>`, as in `"opt:Flycast"`), and each entry a key in the
  file's canonical form (see [Format handlers](#format-handlers)) with its value.
- **Values** are TOML scalars: strings, integers, floats and booleans. Line-oriented
  formats hold raw strings; `json` and `toml` keep the type they read. Arrays, tables and
  null are never captured or written.
- **Merge.** The effective value of a key is the device layer's when it has the key, else
  common's. A device cannot unset a common key, only give it another value. Removing a key
  from the layers stops managing it: the console keeps the value last written and nothing is
  restored.
- **Validation is all or nothing.** A layer that does not parse, a missing or unknown
  `format`, or a value of the wrong kind (a non-string for a raw format, a RetroArch value
  with `"`) makes `apply` change nothing: common without the device layer would put another
  console's choices in place. An unknown emulator or file id is a warning (another profile
  may know it). A secret key in a layer (see [Safety rules](#safety-rules)) is never
  applied and is reported by name only.
- **Written whole.** `capture` writes a layer file sorted by emulator, file and key, under a
  header line saying warpzone writes it. Comments added by hand on the hub are not kept.

## Profile entries

Each emulator is declared as data in the profile:

```toml
[settings.eden]
process = ["eden"]

[settings.eden.files.qt-config]
path = "~/.config/eden/qt-config.ini"
format = "qt-ini"
noise = ["[Controls]*", "[UI]UILayout\\*", "[UI]Paths\\recentFiles", "[Renderer]vulkan_device"]
secret = ["[WebService]*_token", "[WebService]*_username"]
device = ["[System]device_name", "[System]use_docked_mode", "[Renderer]resolution_setup"]
known = ["[System]language_index", "[System]region_index"]

[settings.retroarch.files.opt]
path = "~/.var/app/org.libretro.RetroArch/config/retroarch/config/{name}/{name}.opt"
format = "retroarch-cfg"
create = true

[settings.xemu.data.eeprom]
path = "~/Emulation/storage/xemu/eeprom.bin"
create_when = "~/Emulation/storage/xemu"
```

- `process`: the names its running process has (see [Process detection](#apply)). Required.
- `files.<id>`: `path` (`~` is expanded; `{name}` makes a templated file, every occurrence
  standing for the same name, whose instances are the existing paths that match and any
  instance a layer names, each with the id `<id>:<name>`),
  `format` (a handler below) and `create` (apply creates the file when it is missing and
  has layered keys; default false, where a missing file means the emulator was never opened
  and is skipped).
- `noise`: keys `capture` never proposes: window geometry, recent files, play time, run
  counts, analytics ids, controller and input sections, hotkeys, GPU ids, absolute paths,
  first-run flags. A noise key set in a layer by hand is still applied.
- `secret`: login usernames, tokens, passwords. Never captured, applied, snapshotted,
  printed or logged.
- `device`: keys that `capture` pre-selects for the device layer: resolution, docked or
  handheld mode, device name, screen layouts, anisotropy.
- `known`: keys that `capture --seed` proposes: language, region, system user name and the
  other settings meant to be shared.
- `data.<id>`: a system data file with its `path` and, optionally, `create_when` (a
  directory whose existence means the emulator is installed, as for BIOS placements; see
  [System data](#system-data)).
- `verify = true` on an emulator: a path or process name not yet confirmed on a device, as
  for BIOS placements.
- Patterns match the canonical key. `*` matches any run of characters and `?` one
  character; nothing else is special, so `[` is literal. Ids are `[a-z0-9-]+`.

The shipped `steamos-emudeck` profile declares eleven emulators:

| Emulator | File ids | Path | Format |
|---|---|---|---|
| `eden` | `qt-config` | `~/.config/eden/qt-config.ini` | `qt-ini` |
| `retroarch` | `cfg`, `opt` | `~/.var/app/org.libretro.RetroArch/config/retroarch/retroarch.cfg`, `.../retroarch/config/{name}/{name}.opt` | `retroarch-cfg` |
| `duckstation` | `settings` | `~/.local/share/duckstation/settings.ini` | `ini` |
| `pcsx2` | `ini` | `~/.config/PCSX2/inis/PCSX2.ini` | `ini` |
| `dolphin` | `dolphin`, `gfx` | `~/.var/app/org.DolphinEmu.dolphin-emu/config/dolphin-emu/Dolphin.ini`, `GFX.ini` | `ini` |
| `azahar` | `qt-config` | `~/.config/azahar-emu/qt-config.ini` | `qt-ini` |
| `melonds` | `config` | `~/.var/app/net.kuribo64.melonDS/config/melonDS/melonDS.toml` | `toml` |
| `ppsspp` | `ini` | `~/.var/app/org.ppsspp.PPSSPP/config/ppsspp/PSP/SYSTEM/ppsspp.ini` | `ini` |
| `vita3k` | `config` | `~/.config/Vita3K/config.yml` | `yaml-flat` |
| `xemu` | `config` | `~/.var/app/app.xemu.xemu/data/xemu/xemu/xemu.toml` | `toml` |
| `ryujinx` | `config` | `~/.config/Ryujinx/Config.json` | `json` |

Files that hold only a login (Dolphin's `RetroAchievements.ini`, PPSSPP's achievements
token) are not declared at all.

## Format handlers

A handler is pure, text in and text out:

```python
Value = str | int | float | bool


@dataclass(frozen=True)
class ReadResult:
    values: dict[str, Value]  # canonical key -> value, every key it can read and write
    fixed: frozenset[str]  # keys present but not writable


class Handler(Protocol):
    name: str
    typed: bool  # True: values keep their type; False: raw strings
    can_add: bool  # can it add a key the file does not have

    def read(self, text: str) -> ReadResult: ...
    def write(self, text: str, changes: Mapping[str, Value]) -> str: ...
```

The contract, tested for every handler:

- `write(text, {}) == text`, byte for byte: a UTF-8 BOM, the line endings (LF or CRLF; added
  lines use the file's), the final newline or its absence, comments, blank lines, order,
  and the spacing and quoting of every untouched key.
- After `write(text, changes)`, `read` returns the new value of each changed key and the old
  value of every other key, and only the lines (for `json` and `toml`, the value spans) of
  the changed keys differ.
- `write` raises `NotWritable` for a key in `fixed`, and for a missing key when `can_add`
  is false. Text that is not UTF-8 is an error for that file. Neither ever changes a byte.

| Format | Used by | Canonical key | Value | Adds keys |
|---|---|---|---|---|
| `ini` | DuckStation, PCSX2, Dolphin, PPSSPP | `[Section]Key` | raw text after `=`, trimmed | at the end of its section; a missing section at the end of the file |
| `qt-ini` | Eden, Azahar | `[Section]Key`, with Qt's `\` and `%20` as written | raw text after `=`: quotes, escapes and `@Variant(...)` as written | as `ini` |
| `retroarch-cfg` | `retroarch.cfg`, core `.opt` files | `key` | the text inside the quotes, or the bare value | at the end, as `key = "value"` |
| `yaml-flat` | Vita3K | `key` | raw text after `: ` on a top-level `key: scalar` line | no |
| `json` | Ryujinx | dotted path of objects, `hotkeys.screenshot` | typed scalar | no |
| `toml` | melonDS, xemu | dotted path of tables and key, `Instance0.Firmware.Language` | typed scalar | after the table's last key; a missing table at the end |

- `ini`: a key that appears twice in a section is `fixed` (PCSX2 writes `[Folders]SaveStates`
  twice; DuckStation repeats a hotkey that has several bindings). A section that appears
  twice is one section. Keys are case-sensitive: profiles and layers use the emulator's own
  spelling. A changed line keeps its spacing around `=`; an added line copies the style of
  its section (`Key = Value` or `Key=Value`).
- `qt-ini`: `Key\default` lines are companions, not keys. Eden and Azahar ignore a stored
  value while its companion says `true`, so writing `Key` also writes `Key\default=false`
  (added after it when the file has companions and this key has none).
- `retroarch-cfg`: comments and `#include` lines are kept. A value with `"` cannot be
  written (RetroArch has no escape). A written line is always quoted; an unquoted value
  (`cheevos_enable = true`, as left by a hand edit) is read the same as a quoted one.
- `yaml-flat`: the `---` and `...` markers, block and flow sequences and every indented
  line belong to the key before them, which is `fixed`. YAML is never interpreted beyond
  this; Vita3K writes every key, so not adding keys loses nothing.
- `json`: the text is parsed with `json` and scanned once to record the span of every
  scalar leaf. A write replaces spans with `json.dumps(value, ensure_ascii=False)` and the
  result must parse to the expected document. Arrays, null and object keys with a `.` are
  `fixed`.
- `toml`: a key is writable when it is a `key = value` line under a standard `[table]`
  header (or before the first one) with a single-line scalar value. Dotted keys, inline
  tables, arrays, arrays of tables and multi-line values are `fixed` (xemu's
  `input.gamepad_mappings` spans several lines). The result must parse with `tomllib` to the
  expected values; scalars are written by `tomlw.py`, since `tomllib` only reads.

Equality is typed (`1`, `1.0`, `true` and `"1"` all differ), which is exact for the raw
formats and safe for the typed ones, since a value is always read back from the same
emulator.

## Device state

`apply` and `capture` keep their state on the device, outside every synced folder, in
`$XDG_STATE_HOME/warpzone/settings/` (default `~/.local/state/warpzone/settings/`):

- `snapshots/<emulator>/<file>.json`: for each **tracked** key (readable, writable, not
  noise, not secret), the value this console had when warpzone last synchronised that key.
  `snapshots/<emulator>/data.json` holds the sha256 of each system data file.
- `backups/<emulator>/<file>/<YYYYMMDD-HHMMSS>-<basename>`: the file as it was before each
  write, mode 0600, the last 10 kept per file. Backups are whole files and may hold the
  emulator's login tokens; they never leave the device.
- `log`: one line per run and one per change (emulator, file, key name, never a value),
  rotated at 1 MiB with one old copy kept.
- `last-apply.json`: time, exit code and counts of the last apply, read by `doctor`.
- `lock`: an advisory lock (`fcntl.flock`) that `apply` and `capture` hold. A run that finds
  it taken stops at once: apply with exit 0 and a log line, capture with exit 1.

The snapshot is what tells a change made in an emulator's interface (the current value
differs from the snapshot) from one that came from the hub (the layer differs, the current
value still equals the snapshot).

## Apply

`warpzone settings apply [--device NAME] [--emulator ID ...] [--dry-run] [--quiet]
[--overwrite-local]` runs on the device, offline: it reads the local copy of `wz-settings`
and never calls Syncthing. Without `--device` the device is the configured one whose ID
matches the local Syncthing, as for `prune`; the startup script always passes it.

1. Take the lock, load the config and the device's profile. When the settings folder or its
   `.stfolder` marker is missing, log "no settings folder" and exit 0. Load and validate both
   layers (all or nothing). Conflict copies (`*.sync-conflict-*`) are ignored and logged.
2. For each emulator of the profile: when one of its `process` names is running, skip its
   files and data (logged as running). Otherwise read each declared file that exists, once,
   and decide every key (`plan_apply`, pure). For a layered key with effective value `L`,
   current value `C` and snapshot value `S`:
   - `C == L`: nothing to write; `S := C`.
   - `C != L` and (`S` is absent or `C == S`): the console has not changed it since the last
     sync, so write `L`; `S := L`.
   - `C != L` and `C != S`: changed in the emulator and not captured yet. **Held**: not
     written, `S` kept, logged. `--overwrite-local` writes it anyway.
   - a `fixed` key, or a missing one where the handler cannot add keys: cannot write,
     logged.

   A tracked key that is not layered gets `S := C` only where `S` is absent (the first run,
   or a key new to the file), never otherwise: a change made in the interface stays visible
   to `capture` through any number of applies.
3. Render the new text (the handler, pure). When it equals the old text nothing is written.
   Otherwise copy the file to the backups; then, right before writing, check again that the
   emulator is not running and that the file's size, modification time and sha256 are still
   those read in step 2 (if not, the file is skipped this run, logged); write
   `.<name>.wz-tmp` in the same directory with the file's mode, `fsync` it, `os.replace` it
   over the file and `fsync` the directory. A config file that is a symlink is written
   through to its target, and the link stays.
4. System data files follow the same rules on the whole file, with hashes for values and
   the hub copy in `files/<emulator>/<id>` as `L`. A missing local file is created, with its
   directories, only when the entry's `create_when` directory exists; otherwise it is
   skipped like a missing config file.
5. Write the snapshots (atomically), `last-apply.json` and the log.

Idempotent: a second run with nothing new writes no file, makes no backup and leaves the
snapshots unchanged. `--quiet` prints nothing; otherwise one line per key written or held
and a summary. `--dry-run` prints the plan and writes nothing, not even the snapshots or
the log. Exit codes: `0` when every file was handled (held keys and running emulators are
not failures), `1` when a layer is invalid or a file could not be read, parsed or written,
`2` for usage or config errors.

**Process detection.** On Linux, `apply` and `capture` walk `/proc/<pid>`, skip their own
process and its parent, and compare two things with the profile's exact names: `comm`
(the kernel's process name, at most 15 characters, compared with the name cut to 15) and
the base name of `argv[0]` from `cmdline`. They never search the rest of a command line, so
`warpzone settings apply --emulator retroarch`, an editor that has a config file open, or
EmuDeck's launcher scripts (`bash .../retroarch.sh`) never count as the emulator. Entries
that cannot be read (other users, processes that just ended) are skipped. Flatpak
emulators are ordinary host processes with the same names. Without `/proc` (macOS) both
commands refuse with an error.

**Speed.** At ES-DE startup a run has to stay well under a second on a Steam Deck: it
imports no network code (`cli` imports `provision`, `prune`, `doctor`, `inbox` and the
Syncthing client only inside the commands that use them, and a test checks it), reads only
the declared files that exist, and writes nothing when nothing changed.

## ES-DE startup script

ES-DE runs every file in `<application data>/scripts/<event>/` when that event fires, in
lexicographic order, waiting for each to finish, and only when *Enable custom event
scripts* is on (Main menu > Other settings; `CustomEventScripts` in
`~/ES-DE/settings/es_settings.xml`, off by default). The `startup` event fires once when ES-DE
starts.

`provision device`, for a device that subscribes to `settings` and whose profile has
`paths.esde`, writes `<esde>/scripts/startup/warpzone-settings.sh` (mode 0755, missing
directories created, rewritten only when its content differs, printed with `--dry-run`):

```sh
#!/bin/sh
# Written by warpzone provision device, which rewrites it. Applies the emulator
# settings from the hub. ES-DE waits for it, so it is bounded and never fails.
env -u LD_LIBRARY_PATH -u LD_PRELOAD -u PYTHONHOME -u PYTHONPATH \
  timeout 15 '/home/user/.local/share/pipx/venvs/warpzone/bin/python' -m warpzone \
  settings apply --device 'deck' --config '/srv/warpzone/config.toml' --quiet >/dev/null 2>&1
exit 0
```

The `--config '<path>'` part is there only when the config file `provision` used (`--config`,
`WARPZONE_CONFIG`, `XDG_CONFIG_HOME`) is not `~/.config/warpzone/config.toml`, as an
absolute path: ES-DE does not have the user's environment, so without it `apply` would not
find the config. Provisioning again with another config rewrites the script.

- The interpreter is the absolute path of the Python that runs `provision`, because ES-DE in
  Game Mode does not have the user's `PATH`. `provision` first checks that this interpreter
  imports `warpzone` from outside the current directory (run from `/`, without `PYTHONPATH`
  and `PYTHONHOME`, as the script runs it); when it cannot (warpzone run from a clone) it
  warns and installs nothing, and leaves `es_settings.xml` alone.
- `env -u` drops what ES-DE's AppImage sets for its own libraries, which could break the
  system's Python. `timeout` bounds how long ES-DE waits. The output is discarded:
  `apply` logs to its own file, so a failure never shows in ES-DE or stops it.
- A run that fails before it starts (the config is missing or invalid, the device is not in
  it; exit 2) still leaves a line in the log and a `last-apply.json` with that exit code, so
  `doctor` reports a failed run instead of "no run yet". `--dry-run` leaves nothing.
- `~/ES-DE` is also the device path of `wz-esde`, whose whitelist lets only `gamelists` and
  `collections` through, so the script is never synced.
- `provision` also turns the scripts on. When `CustomEventScripts` in `es_settings.xml` is
  not `true` (`event_scripts_enabled`, pure), it sets it, only while ES-DE is not running:
  ES-DE keeps its settings in memory and rewrites the file when it quits, so an edit made
  while it runs would be lost. ES-DE is detected as `apply` detects an emulator (`comm` and
  the base name of `argv[0]`, names in `esde.ESDE_PROCESSES`: `es-de` and `ES-DE.AppImage`,
  to be confirmed on a device). The edit (`enable_event_scripts`, pure) changes only the
  `value` of the existing `<bool name="CustomEventScripts" .../>` element, or adds that one
  line after the last `<bool .../>` line with its indentation and line ending; every other
  byte stays as it was, and the result must still parse as the same settings plus that one.
  The old file is copied first to
  `$XDG_STATE_HOME/warpzone/settings/backups/esde/es_settings/<YYYYMMDD-HHMMSS>-es_settings.xml`
  and the new one written atomically, as `apply` writes a config file. While ES-DE runs,
  provision warns (quit ES-DE and run it again) and leaves the file alone, as it does
  when `/proc` is missing and ES-DE cannot be detected; a missing file (ES-DE never
  started), one that is not UTF-8 or does not parse is a warning too, never created or
  rewritten. `--dry-run` says what it would
  change. `doctor` still checks the setting.
- Other files in `scripts/` are never touched. A device that stops subscribing to
  `settings` keeps the script, which then finds no settings folder and does nothing.
- The same mechanism installs `warpzone-links.sh` (for a device that lists `library` and whose profile has `[links.*]`), which runs `links sync` and so links the games that arrived since the last start before ES-DE lists them (see [Title links](/warpzone/development/design/title-links/)).
- Apply runs once per ES-DE start, possibly before Syncthing has pulled the newest layers.
  What arrives later applies at the next start, or at once with `warpzone settings apply`.

## Capture

`warpzone settings capture [--device NAME] [--emulator ID ...] [--seed] [--key PATTERN
...] [--include-new] [--yes] [--to-common PATTERN ...] [--to-device PATTERN ...]
[--discard PATTERN ...] [--offline] [--syncthing-config PATH] [--dry-run]` runs on the
device. `--key` without `--seed` is a usage error (exit 2).

1. Refuse (exit 1) when the device has no settings folder. Take the lock (a run that finds it
   held refuses with exit 1; a report and `--dry-run` take none). Unless `--offline`, ask the
   local Syncthing (API key from its `config.xml`, as `prune` does; `--syncthing-config`
   points at it) whether `wz-settings` is idle with nothing to pull and
   whether the local record of each layer file equals the global one (`GET /rest/db/status`,
   `GET /rest/db/file`); otherwise refuse with exit 1, since the layers may be about to
   change. Refuse as well while a layer has a conflict copy, naming it: the user keeps one
   version on the hub and deletes the other.
2. Skip, with a note, every emulator that is running (its files may not hold its latest
   changes yet) and every file that has no snapshot ("no baseline: run `settings apply`
   first, or `capture --seed`"; for a system data file, whose baseline `apply` would take from
   the current file, "run `capture --seed --key '<emulator>/data/<id>'`").
3. Candidates (`candidates`, pure), among tracked keys: those in the snapshot with `C != S`
   and `C` different from the effective layered value. Keys new to the file (not in the
   snapshot; usually defaults written by an emulator update) are only counted, unless
   `--include-new`. With `--seed`, also every tracked key that matches the file's `known`
   patterns or a `--key` pattern and whose `C` differs from the effective layered value,
   snapshot or not: this is how a console configured before warpzone fills the layers the
   first time. `--key` and the decision flags match `<emulator>/<file>/<key>`, for example
   `eden/qt-config/[System]*`.
4. Pre-selection: the device layer when this device's layer has the key; common when
   common has it; the device layer when the key matches `device`; common otherwise.
5. Decisions. On a terminal, one prompt per key, grouped by emulator and file:

   ```
   eden  qt-config  [Renderer]resolution_setup
     here 3   before 2   common -   deck 2
     [c]ommon  [D]evice (deck)  [x] discard  [s]kip  [q]uit >
   ```

   The pre-selected answer is in capitals and Enter takes it; `q` skips everything left.
   A system data file is shown with its size and the first 12 hex digits of each sha256,
   never its content, and offers only common, discard, skip and quit. Without a terminal,
   keys are decided by the `--to-common`, `--to-device` and `--discard` patterns (the first
   that matches, in that order), the rest by `--yes` (take the pre-selection) or not at all;
   with no decision flag the run is a report, which writes nothing, snapshots included. The
   flags also apply on a terminal: a key they decide is not asked about. `--dry-run` asks or
   decides, prints the result and writes nothing.
6. Effects, applied once every key is decided:
   - common: the key is set in `common.toml` and removed from this device's layer, where it
     would keep winning. A note names any other device layer that overrides it.
   - device: the key is set in `<device>.toml`.
   - discard: nothing reaches the hub. For a layered key, the layer value returns at the
     next apply.
   - skip: nothing; the key is proposed again next time.

   A key whose value a layer cannot hold (a double quote in a RetroArch value, a line break)
   is not captured: capture names the key and the reason, never the value, treats it as
   skipped (snapshot kept) and exits 1 at the end; the other keys still go. The resulting
   layers are validated as `apply` validates them before anything is written, and a run whose
   result would be invalid writes nothing.

   Right before writing, each layer file is read again and must equal what was loaded
   (otherwise nothing is written and capture exits 1); the same goes for a hub copy of a data
   file and for the local data file, which must still hash as it did when proposed. Files are
   written to a `.<name>.wz-tmp` beside them and renamed. A data file chosen for common is
   copied to `files/<emulator>/<id>` the same way; one over 1 MiB is left out with a note.
7. Snapshot: `S := C` for every key decided common, device or discard and for every new key
   counted; a skipped key keeps `S`. A file that had no snapshot (`--seed`) gets one for all
   its tracked keys, as a first apply would make it. Nothing is written with `--dry-run`.

The output ends with what changed in each layer file (keys set, keys removed) and the count
of new keys that were not shown. Exit codes: `0` ok, `1` for a refusal or an error, `2` for
usage or config errors.

## Audit

`warpzone settings audit [--device NAME] [--emulator ID ...] [--all]` reads the layers, the
files and the snapshots and writes nothing, state included. Per emulator and file:

```
eden  qt-config  ~/.config/eden/qt-config.ini
  in sync        14 keys
  pending        [Renderer]resolution_setup  here 3, hub 2      apply writes it
  changed here   [System]language_index      here 4, hub 1      capture proposes it; apply holds it
  cannot write   [Folders]SaveStates         duplicated in the file
  changed here   3 keys not on the hub (--all lists them)
retroarch  opt:Flycast  no file (apply creates it)
xemu  eeprom  pending: the hub copy differs, unchanged here since the last apply
dolphin  running: its files may not hold its latest changes
summary: 2 pending, 2 changed here, 1 cannot write
```

`--all` also lists the keys in sync and each changed key that is not on the hub. Values are
cut to 40 characters. Secret keys never appear: they are never layered, snapshotted or
listed, and one found in a layer is reported by name only. Exit codes: `0` when everything
is in sync, `1` when anything is pending, changed, not writable or invalid, `2` for usage
or config errors.

## System data

Some settings live outside the config files: the Xbox EEPROM
(`~/Emulation/storage/xemu/eeprom.bin`: language and video mode, but also the serial number,
MAC address and hard disk key), Azahar's system configuration
(`~/Emulation/storage/azahar/nand/data/00000000000000000000000000000000/sysdata/00010017/00000000/config`:
language, region, user name, birthday) and Vita3K's user
(`~/Emulation/storage/Vita3K/ux0/user/00/user.xml`). They are profile `data` entries, kept on
the hub as whole files in `settings/files/<emulator>/<id>`, common to every device (there is
no device layer for them), at most 1 MiB each. `apply` delivers them with the same rules as
keys (held, backup, atomic write), comparing hashes; `capture` proposes the whole file when
it changed since the snapshot.

They are delivered by `apply`, not by a receive-only folder as BIOS placements are: a
placement cannot carry a capture back to the hub, would replace the file while the emulator
runs, and would report the emulator's own rewrite as a local change to revert. A shared
EEPROM gives every console the same Xbox identity. xemu's hard disk image stays per device
and unsynced, so a save that a game signed with a console's old disk key may be refused
there after the first apply.

## Safety rules

- Only layered keys are written, and nothing else in a file changes: the handler contract
  guarantees it and the tests check it byte for byte.
- Never while the emulator runs: checked before reading and again right before each
  replace.
- Every write is atomic (a temporary file in the same directory, then `os.replace`) and
  comes after a backup. A file that changed after it was read is left alone for that run.
- `apply` never overwrites a change made in an emulator's interface that was not captured
  or discarded yet (held keys), unless `--overwrite-local`.
- Secrets never reach the hub. The profile's `secret` patterns, plus a built-in net (a key
  whose last part contains `token`, `password`, `passwd`, `secret`, `apikey` or `api_key`,
  in any case), are left out of capture, snapshots, audit and logs, and rejected in layers.
  Login usernames are named per emulator, because the same word also names things meant to
  be shared (the DS firmware's user name).
- State, backups and logs live outside every synced folder. Noise keys keep paths of one
  machine out of the layers.
- `capture` writes the hub only while the local copy of `wz-settings` is in sync and has no
  conflict copies, and the hub keeps 30 days of versions of every layer.
