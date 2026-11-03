# warpzone design

warpzone keeps one game library on a home server (the **hub**, typically a NAS)
and delivers it to every console you own (the **devices**: Steam Deck, Steam
Machine, a spare Mac...). Saves follow you from one device to the next. It is a
thin layer of conventions and automation on top of [Syncthing](https://syncthing.net):
warpzone never moves game data itself, it only configures Syncthing and keeps
each device's copy honest.

This document is the contract for the implementation. Anything not described
here is out of scope for v0.2.

## Goals

- One source of truth for ROMs, BIOS and extras: the hub. Devices only receive.
- Each device chooses what it gets with a whitelist that is **edited only on the
  hub**. No file is ever edited by hand on a device after provisioning.
- Saves sync in both directions between all devices, with history kept on the hub.
- Emulator settings are changed in each emulator's own interface, on any device, and
  captured to the hub as key-level layers (common and per device) that every device
  applies. Config files are never copied whole and never edited by hand.
- Adding a device is one command.
- No dependencies beyond the Python standard library (3.11+), so the tool runs
  unchanged on SteamOS, macOS and any Linux box.

## Non-goals (v0.2)

- Moving, renaming or converting ROMs that are already in the library. New content goes through `warpzone inbox`, which only ever adds.
- Shipping or locating any copyrighted content (ROMs, BIOS, keys, firmware).
  The BIOS checker compares what the user has with public names, sizes and hashes.
- Running on the hub itself. Many NAS ship an old Python; the hub is driven
  remotely through its Syncthing REST API.
- Per-game emulator settings (RetroArch core and game overrides, per-game INI files),
  controller mappings and input profiles. Settings travel only as single keys.
- Moving logins. RetroAchievements and other sessions are made on each console and
  never leave it.

## Hub layout

```
<hub root>/
├── library/                 hub: send-only   → device: receive-only, per-device whitelist
│   ├── roms/<system>/       ES-DE system folder names
│   ├── bios/                some subfolders are also separate `wz-bios-*` folders (see Profiles)
│   ├── extras/              switch/{updates,dlc,mods,misc}, amiibo/, texturepacks/
│   ├── patterns-common.stignore
│   └── patterns-<device>.stignore
├── saves/<emulator>/<kind>/ hub: send-receive ⇄ devices: send-receive, hub keeps history
├── frontend/
│   ├── media/               ES-DE downloaded media     ⇄
│   ├── es-de/               ES-DE gamelists, collections ⇄
│   └── play-state/          one file per device: favourites and play statistics ⇄
├── songs/                   hub: send-only → opted-in devices, per-device whitelist
├── tools/                   hub: send-only → devices (scripts, docs)
├── settings/                hub: send-receive ⇄ devices: send-receive, emulator settings layers
└── inbox/                   never synced; drop zone for new content (SMB)
```

Why the split:

- `library` is receive-only on devices, so an accidental delete on a device can
  never propagate to the hub.
- Saves need two-way sync; they live in their own folders so their direction
  cannot leak into the library.
- `songs` is not emulation (Clone Hero, YARG) and is mounted outside `~/Emulation`.
- `settings` is written by `warpzone settings capture` on a device, so it is two-way
  like the saves (see [Emulator settings](#emulator-settings)).

## Folders

Every folder warpzone manages has a stable Syncthing folder ID prefixed `wz-`.

| Folder ID | Hub path | Hub type | Device type | Device path (profile key) | Ignores |
|---|---|---|---|---|---|
| `wz-library` | `library` | sendonly | receiveonly | `library` | per-device whitelist |
| `wz-songs` | `songs` | sendonly | receiveonly | `songs` | per-device whitelist |
| `wz-tools` | `tools` | sendonly | receiveonly | `tools` | common only |
| `wz-media` | `frontend/media` | sendreceive | sendreceive | `media` | common only |
| `wz-esde` | `frontend/es-de` | sendreceive | sendreceive | `esde` | fixed whitelist: `gamelists`, `collections` |
| `wz-play-state` | `frontend/play-state` | sendreceive | sendreceive | `play_state` | fixed `*.wz-tmp` |
| `wz-save-<emu>-<kind>` | `saves/<emu>/<kind>` | sendreceive | sendreceive | from profile | common only |
| `wz-bios-<emu>-<kind>` | `library/<hub path from profile>` | sendonly | receiveonly | from profile | fixed whitelist from the profile |
| `wz-settings` | `settings` | sendreceive | sendreceive | `settings` | common + fixed `*.wz-tmp` |

The `wz-library` ignore lines differ from the other whitelisted folders in one
way: fixed exclusions come before the `#include`, on both sides (see
[Library exclusions](#library-exclusions-and-bios-placements)). Save and
`wz-bios-*` entries may carry their own ignore list (a whitelist ending in `*`).

"Common only" and "fixed whitelist" folders get their ignore lines inline
through `POST /rest/db/ignores` (the common lines, then the fixed or
per-save lines), on the hub and on devices alike. They need no ignore files,
so the user only ever creates `patterns-*.stignore` in `library/` and `songs/`.

Folder-level settings:

- Save folders, `wz-settings` and `wz-play-state` on the hub: versioning `staggered`, `maxAge` 30 days.
- Everything else: no versioning (the hub is the authority; filesystem
  snapshots on the hub are recommended in [safety.md](safety.md) instead).
- `fsWatcherEnabled: true` everywhere.
- `ignorePerms: true` on every folder of a device whose profile sets `ignore_perms`
  (the library sits on exFAT, which has no POSIX permissions). It is set on the device's
  folders only; other profiles never get the key, and a folder that already exists is
  patched when it is missing.

A device subscribes to a subset of folders, set in the private config
(`folders = ["library", "tools", "saves", "frontend", "songs", "bios", "settings", "play-state"]`).
`saves` expands to every save folder of the device's profile; `frontend` expands to
`wz-media` and `wz-esde` (a profile with `[artwork]` places and limits them itself, see
[Frontend artwork](#frontend-artwork)); `bios` expands to every BIOS placement of the profile;
`settings` is `wz-settings`; `play-state` is `wz-play-state` and needs `frontend` (see
[Play state](#play-state)).

## Ignore files and the bootstrap problem

All whitelist files live **inside the synced folder on the hub** and are synced
to every device. Each device's own `.stignore` is a single line that never
changes after provisioning:

```
#include patterns-<device>.stignore
```

`patterns-common.stignore` (shipped as a template in `templates/`):

```
// Every device receives every whitelist; each one only applies its own.
!/patterns-*.stignore
(?d)@eaDir
(?d).DS_Store
(?d)._*
(?d)Thumbs.db
```

`patterns-<device>.stignore` starts with `#include patterns-common.stignore`,
lists what the device wants, and ends with `*` if it is a whitelist. The
hub's own `.stignore` is `#include patterns-common.stignore`.

**Bootstrap.** Syncthing refuses to sync a folder whose `.stignore` includes a
file that does not exist yet, and on a fresh device the whitelist has not
arrived. Provisioning therefore does, for each whitelisted folder:

1. Create the folder **paused**, with ignores set (through `POST /rest/db/ignores`) to
   ```
   !/patterns-*.stignore
   *
   ```
   so that only the whitelists can arrive.
2. Unpause and wait (default timeout 10 min) until `patterns-<device>.stignore`
   and `patterns-common.stignore` (which the device whitelist includes) exist in
   the folder root and their local version equals the global one
   (`GET /rest/db/file`).
3. Replace the ignores with `#include patterns-<device>.stignore` and trigger a
   rescan.

**Applying later edits.** After provisioning, the user edits
`patterns-<device>.stignore` on the hub only. It reaches the device through
normal sync, but Syncthing v2.1.7 re-evaluates ignores (and pulls files that
became un-ignored) only on a full scan of the folder. A scan of a single
sub-path does not do it, and a file synced in by the puller does not reliably
cause a full scan: it depends on the OS and the filesystem watcher, which made
the e2e test flaky on Linux CI. Without a bound, a device could wait for the
default `rescanIntervalS` (3600 s). Whitelisted device folders therefore get
`rescanIntervalS = rescan_seconds` (per-device config key, default 300,
minimum 5), so an edit applies within that time whatever the watcher does. A
rescan only stats files and hashes what changed.

**Hub bootstrap.** The hub has the opposite problem. Its folder is created with the final
ignores (`#include patterns-common.stignore`), so the first full scan walks the whole
library before the whitelists are in the hub's index, and a device waits for
`patterns-<device>.stignore` until the scanner happens to reach it. With a large library
on a slow disk that is hours. When `provision hub` **creates** a whitelisted folder
(library, songs) it therefore does, in order:

1. Create the folder paused and set the same bootstrap ignores the device uses
   (`!/patterns-*.stignore`, `*`). The fixed hub exclusions are not needed: `*` ignores
   everything the whitelists do not name.
2. Unpause and trigger a scan. With everything else ignored it reaches the whitelists at
   once, and Syncthing announces them to the devices in its first index batches.
3. Wait (`provision hub --timeout`, default 120 s) until `patterns-common.stignore` and the
   `patterns-<device>.stignore` of every subscribed device are in the hub's own index
   (`GET /rest/db/file`: a live `local` record, not deleted, not invalid).
4. Set the final ignores and scan again, which starts the full scan. Devices can by now
   fetch their whitelist while the scanner is still working on the rest.

If the whitelists are not in the index when the timeout runs out (they were never put on
the hub, or the hub is very slow), it warns and still sets the final ignores: the hub
folder is never left half-configured. Only creation does this; a folder that already
exists keeps the direct path (ignores set if they differ, folder resumed if paused). In
dry-run the calls are printed and nothing waits.

If the device's whitelist does not exist on the hub, provisioning stops before
step 1 with a clear error. It never creates a device folder that would pull the
whole library. The check reads the hub's API (`GET /rest/db/browse`) with the
hub credentials from the private config; if the hub is unreachable it warns and
continues, which is safe because the bootstrap ignores only let whitelists
through. On the hub side, `provision hub` cannot check files in a folder that
does not exist yet, so it creates the folder and warns about missing whitelists
(only if they are still missing after the hub bootstrap above);
`warpzone check` is the authoritative report once the hub has scanned.

## Profiles

A profile describes where things live on one kind of device. Profiles are data
files shipped in the repo (`warpzone/data/profiles/<name>.toml`), because they are
the part other people can reuse and improve.

warpzone ships `steamos-emudeck` and `anbernic-rg351mp` (see
[Frontend and RetroArch overrides](#frontend-and-retroarch-overrides)). Paths use `~` and
are expanded on the device.
It has five kinds of entry; the shipped file is the reference, this is the shape of
the first four (the fifth, `[settings.*]`, is described in
[Emulator settings](#profile-entries)):

```toml
name = "steamos-emudeck"
description = "SteamOS with EmuDeck (Steam Deck, Steam Machine)"

[paths]
library = "~/Emulation"
songs = "~/Songs"
tools = "~/.local/share/warpzone/tools"
media = "~/Emulation/tools/downloaded_media"
esde = "~/ES-DE"
settings = "~/.local/share/warpzone/settings"

# Library paths the device never receives (before the whitelist).
[library]
ignore = ["/roms/wiiu/mlc01", "/bios/ryujinx/keys", "/bios/flycast/bios"]

# One entry per save folder. `path` is the REAL directory: EmuDeck exposes
# saves through symlinks under ~/Emulation/saves and Syncthing never follows
# symlinks, so pointing Syncthing at the symlink would sync nothing.
[saves.retroarch-saves]
path = "~/.var/app/org.libretro.RetroArch/config/retroarch/saves"
[saves.ryujinx-user]
path = "~/.config/Ryujinx/bis/user"
ignore = ["!/save", "!/saveMeta", "*"]   # a whitelist for this folder only

# BIOS placements: a hub folder delivered into the emulator's real directory.
[bios.cemu-keys]
hub = "bios/cemu"                         # below the hub's library/
path = "~/.local/share/Cemu"
ignore = ["!/keys.txt", "*"]
```

Save entries (what each emulator keeps, all real directories): RetroArch saves
and states; Dolphin GC, Wii and states; DuckStation, PCSX2, melonDS, MAME, RMG
(saves and states); PPSSPP (saves and states); RPCS3 (savedata and trophy);
Vita3K; ScummVM; PrimeHack (GC, Wii, states); Eden (`nand/user/save`, plus the
user profiles in `nand/system/save/8000000000000010`); Ryujinx (`bis/user`
restricted to `save` and `saveMeta`, plus the profiles in `bis/system/save`);
Azahar (sdmc, states); shadPS4; Cemu (`mlc01/usr/save`); Xenia (Proton:
`~/Emulation/roms/xbox360/content`, native: `~/.local/share/Xenia/content`;
EmuDeck installs one or the other, and each has its own hub folder);
Flycast (`vmu_save_*.bin` and the `nvmem` files only, from the folder that also
holds the standalone BIOS, which stays out); YARG, which is not EmuDeck
(`settings.json`, `scores`, `profiles`, `playlists` and `custom`, lowercase as
the Linux build writes it; never `songcache.bin` or `badsongs.txt`, which hold one
machine's absolute paths, nor the `*.bak` files in `profiles/`).
User profiles are synced so that a save keeps its owner on every console: a
Switch save belongs to a profile ID, and the same ID must exist everywhere.

The save key `<emu>-<kind>` maps to folder ID `wz-save-<emu>-<kind>` and hub
path `saves/<emu>/<kind>`. A save folder with an `ignore` list gets those
patterns (after `#include patterns-common.stignore`).

Save paths that do not exist on the device yet (the emulator was never opened,
or is not installed) are skipped with a warning, never created by warpzone, and
picked up by the next `provision` run. Skipping is per folder and never an
error, so the many entries for emulators a console does not have cost one
warning line each and nothing else. Some save folders sit inside `~/Emulation`, which is the
`wz-library` root; that is fine because the library whitelist ignores them, and
Syncthing supports nested folders when the parent ignores the child.

### Library exclusions and BIOS placements

EmuDeck replaces several folders inside `~/Emulation/bios` with symlinks to the
emulator's real directories: `eden/keys`, `eden/firmware`, `azahar/keys`,
`ryujinx/keys`, `flycast/bios`, `shadps4/sys_modules` (and the same for Citron,
Yuzu and Suyu). Syncthing never follows symlinks, and the hub's `library/bios/`
has real folders at several of those paths, so a library folder that tried to
sync them would conflict or fail. Cemu has the opposite problem: it keeps its
installs and saves in `roms/wiiu/mlc01`, which is per-device data. Two profile
tables deal with it:

- `[library] ignore`: anchored paths the device's library folder ignores. They
  are written **before** the `#include patterns-<device>.stignore` line (the
  first match wins and the whitelist ends with `*`, so nothing can un-ignore
  them), which means a whitelist edit on the hub cannot bring them back. The
  hub is not affected: it still holds and sends `roms/wiiu/mlc01` to nobody
  who whitelists it. A line may start with `(?d)` (Syncthing's "deletable"
  prefix) so the ignored file does not stop the hub's deletion of its parent
  folder. The shipped profile also lists EmuDeck's own files, which exist on
  every console and not on the hub: `bios/PPSSPP`, every `readme.txt` under `bios/`,
  the emulator-owned folders `bios/HdPacks`, `Mupen64plus`, `fbneo`, `kronos`,
  `same_cdi`, `ryujinx`, `shadps4` and `yuzu`, and each
  `roms/*/systeminfo.txt` and `metadata.txt`. `bios/mame` and `bios/neocd` are
  deliberately not ignored: a hub may deliver BIOS files there. Without it a receive-only library
  reports them as local changes (hundreds of files), and a revert would delete
  them. Only those files are named: no folder that holds games is ignored.
- `[bios.<emu>-<kind>]`: a **BIOS placement**. It maps a hub path below
  `library/` (`hub`) to the emulator's real directory on the device (`path`),
  through a receive-only folder `wz-bios-<emu>-<kind>` (send-only on the hub)
  whose own `ignore` list lets only the intended files through. Optional keys:
  `create_when` (create the target when that directory exists, i.e. the
  emulator is installed but never wrote this folder; otherwise a missing target
  is skipped like a save folder) and `verify = true` (the target is a best
  guess that was not confirmed on a device).

Shipped placements:

| Folder | Hub path | Device target | Delivered |
|---|---|---|---|
| `wz-bios-eden-keys` | `bios/eden/keys` | `~/.local/share/eden/keys` | `prod.keys`, `title.keys` |
| `wz-bios-eden-firmware` | `bios/eden/firmware` | `~/Emulation/storage/eden/nand/system/Contents/registered` (`verify`) | everything in the folder |
| `wz-bios-azahar-keys` | `bios/azahar` | `~/.local/share/azahar-emu/sysdata` | `aes_keys.txt` |
| `wz-bios-cemu-keys` | `bios/cemu` | `~/.local/share/Cemu` | `keys.txt` |

Keys travel in exactly these files and nowhere else; their content is never
printed. Eden firmware is uncertain: EmuDeck's `bios/eden/firmware` symlink
points at `~/.local/share/eden/nand/system/Contents/registered`, but the same
script sets Eden's `nand_directory` to `~/Emulation/storage/eden/nand`, so the
symlink is probably dangling and the second path is where Eden reads firmware.
Check it on a real device before relying on it.

The path of every placement is also added to the library exclusions of the
device **and of the hub's library folder**. The hub folder is then the only
owner of those files, so the same file is never in two folders on the hub, and
a device that does not subscribe to `bios` simply does not receive them from
the library either (a placement is the only way those paths travel). The hub
ignore lines come from the placements of every profile that a library
subscriber uses.

**Why nested folders.** A placement folder sits inside the hub's library
folder (`library/bios/cemu` inside `library`). Syncthing's FAQ says nesting is
"not supported, recommended or coded for, but it can be done successfully when
you know what you're doing", and the only thing to avoid is circular shares.
Making the parent ignore the child, as above, is the safe way to do it: the
parent never scans the child, so its `.stfolder` and `.stignore` markers and
its files belong to one folder only. The alternative of keeping the
placements outside `library/` (for example a top-level `bios-keys/`) would
avoid nesting but break the layout the BIOS checker and the inbox rely on
(`library/bios/...`), so it was not chosen. The same parent-ignores-child
arrangement already exists on devices, where save folders sit inside
`~/Emulation`.

**Not synced on purpose.** xemu's HDD image (`~/Emulation/storage/xemu/xbox_hdd.qcow2`)
holds that console's Xbox saves inside a single disk image that is rewritten
while the game runs, so two-way sync would corrupt it or leave conflict copies.
It is per device and has no entry. xemu's `eeprom.bin` has no folder either: it travels
as [system data](#system-data) of the emulator settings. xemu's BIOS files
(`mcpx_1.0.bin`, `Complex_4627v1.03.bin`) are real files in `~/Emulation/bios`,
so the library delivers them as it always did.
Flycast's standalone BIOS and shadPS4's modules are not placed yet (they live
in folders the library now ignores): add a `[bios.*]` entry when a hub layout
for them exists.

**Ryujinx keys and firmware are manual on purpose.** Ryujinx reads `prod.keys`
and `title.keys` from `~/.config/Ryujinx/system`, the same two files that
`wz-bios-eden-keys` delivers from `library/bios/eden/keys`. A second placement
for Ryujinx would need a second folder ID (`wz-bios-ryujinx-keys`) over the
same hub directory, and that is not a clean arrangement: a Syncthing folder ID
is the same on both ends, so the folder cannot be shared once and received at
two paths; two hub folders over one directory would share its `.stfolder` and
`.stignore` and scan and index the same files twice, which is the overlap the
placements exist to avoid. Giving Ryujinx its own hub copy (`bios/ryujinx/keys`)
would duplicate the keys. So the [EmuDeck guide](emudeck.md#manual-steps) tells the user to copy the
two files once. Ryujinx firmware is installed from the emulator (Tools > Install
Firmware, pointing at the Eden firmware folder), not delivered as files, like
the PS3 firmware in RPCS3.

`warpzone bios` run on a device looks in the library folder, where the placed
files no longer are; run it on the hub (`--root`) for keys and firmware.

### Frontend and RetroArch overrides

The `anbernic-rg351mp` profile (an Anbernic RG351MP running dArkOS, with the ROMs on an
exFAT card at `/roms`) needs three things that `steamos-emudeck` does not. All three are
profile data, not code that tests the profile name.

- **`ignore_perms = true`** (top level). exFAT has no POSIX permissions (and no symlinks),
  so a receive-only library would list every file as a local change and the puller would
  try to `chmod`. Every folder of such a device is created with `ignorePerms: true`. The
  profile depends on no symlink: its save folders are real directories on the root
  filesystem, and its library exclusions name no EmuDeck link. exFAT also compares file names
  without regard to case and forbids a few characters (`: * ? " < > |`); Syncthing reports
  a hub file that does not fit as an error on that file, so such names stay a matter for
  the hub's library.
- **`create_when`** on a `[saves.*]` entry, with the meaning it has for `[bios.*]`: create
  the folder when that directory exists. RetroArch writes `saves/` and `states/` only on
  the first save, so without it the two folders would stay skipped until then.
- **`[es_systems]`** (`source`, `override`, `prefix`, `process`; needs `paths.library`).
  The frontend reads `override` before `source`. `provision device` writes the override as
  the source with every `<path>` element that starts with `prefix` moved to
  `<library>/roms/`, so `<path>/roms/nes/</path>` becomes
  `<path>/roms/library/roms/nes/</path>`; every other byte (line endings, the commands,
  entries outside `prefix` such as `/opt/cmds/`) is kept (`frontend.rewrite_system_paths`,
  pure). Every system under `prefix` moves, including those the hub has no games for: the
  alternative of moving only the systems the hub has would make the override depend on the
  whitelist and go stale with every edit of it. Such a system points at a folder that stays
  empty, and the device's own `/roms/<system>` folders are not listed any more (an
  entry like `ports` also moves). The copy is made only while no process in `process`
  runs; when `/proc` is missing or the frontend runs it warns, names the command to rerun
  and writes nothing. A different existing override is backed up first (to the settings
  state folder, as for `settings apply`); a fresh one has nothing to back up; the write is
  atomic and refused when the file changed in between. A second run changes nothing.
- **`[retroarch]`** (`cfg`, `process`, `set`). Each file of `cfg` (a device may have two
  RetroArch builds with a config each) gets the keys of `set` with their values, `{library}`
  replaced by the library path: `system_directory` for BIOS files, and
  `savefiles_in_content_dir` and `savestates_in_content_dir` off with
  `savefile_directory` and `savestate_directory` pinned to the `[saves.*]` folders, so
  saves and states leave the ROM folders and can sync. The edit uses the `retroarch-cfg`
  handler of the settings layer (only the value changes, every other byte is kept), only
  while no process in `process` runs, with a backup before each edit. A missing file is
  never created (RetroArch writes it on its first start); a file where a wanted key is
  present twice or malformed is left alone with a warning. Saves already next to the ROMs
  are not moved.

Both overrides run at the end of `provision device`, for a device that subscribes to
`library`. `--dry-run` prints what would be written.

The RetroArch save layout is the same as on the Steam Deck and Steam Machine: all the
`sort_*` options are off on all three, so saves and states are flat files in one folder
named after the game (`<game>.srm`, `<game>.state`), and the hub folders are the same
`saves/retroarch/saves` and `saves/retroarch/states`. What differs is only which core wrote
a file; a state of one core does not load in another core or another version of it, which
warpzone cannot check.

### shadPS4 install directories

shadPS4 runs the extracted games it finds in its install directories, and the PS4 games that
`inbox` extracts land in `library/roms/ps4/games` ([PS4 packages](#ps4-packages)). A profile
with `[shadps4]` (`config`, `process`, `install_dirs`) has `provision device` list that folder
there, for a device that subscribes to `library`, at the end of the same step as the other
overrides. The profile `steamos-emudeck` declares `{library}/roms/ps4/games`.

- **Files.** Each path of `config` that exists is edited, chosen by its suffix. `config.json`
  (shadPS4 v0.21 and later) holds `General.install_dirs`, a list of
  `{"enabled": true, "path": "..."}`; `config.toml` (older versions, and still on disk after the
  move to JSON) holds `GUI.installDirs`, a list of strings. Both are edited because either may be
  the one a version reads. A missing file is never created (shadPS4 writes it on its first
  start); when neither exists the run warns to start shadPS4 once and run `provision` again.
- **Edit.** The existing entries stay and the missing folder is appended (a disabled entry for
  it is enabled; trailing slashes do not count as a difference). `config.json` must be
  exactly what `json.dumps(indent=2)` writes, which is shadPS4's layout, or it is left alone:
  the edit re-serialises it. In `config.toml` only the `installDirs` value is replaced (or the key
  or the `[GUI]` table added) and the result must read back as the original plus the new folders.
  Anything that does not read as expected is left alone with a warning (`shadps4.apply` is pure).
- **Safety.** Written only while none of `process` runs (the emulator, its Qt launcher and the
  AppImages: `/proc` is read as for the other overrides, and a missing `/proc` is a warning), with
  the old file backed up first to the settings state folder and an atomic write that is refused if
  the file changed meanwhile. A second run changes nothing; `--dry-run` prints what it would add.
- **Doctor.** `doctor` reports `shadPS4 install folder`: a warning naming each existing file that
  lacks the folder (with the command to run), or none found.
- **Why not the settings layer.** `settings apply` layers typed scalar keys from the hub, and its
  `json` and `toml` handlers write scalars only; this list is an array (of objects, in JSON). Its
  value is also derived from each console's own library path, not shared between consoles, so it
  is declared in the profile like the RetroArch paths.

### shadPS4 launch script

`[shadps4.launch]` (`qt_ini`, `versions`, `script`, `find_rules`, `emulator`, all declared by
the profile) replaces EmuDeck's `shadps4.sh` in ES-DE's find rules without touching that
script, which EmuDeck rewrites. `shadps4.render_launch_script` is pure: it returns the bash
text with the two Qt launcher files baked in as quoted absolute paths. The script lives in
`~/.local/share/warpzone/`, outside `~/ES-DE` (so `wz-esde` never syncs it) and not in the
Python package (so it runs when warpzone cannot import). `shadps4.point_find_rule` replaces the
text of the first `staticpath` entry of the named `<emulator>` and verifies that only it
changed; `provision device` backs the file up, refuses while ES-DE runs and uses an atomic
write that fails if the file changed meanwhile. `doctor` compares the script with the text
`provision` would write and reads the rule. The ES-DE startup scripts do not depend on any of
this: if EmuDeck puts its entry back, only PS4 launching goes back to the Qt launcher.

### Title links

Vita3K reads games from `~/Emulation/storage/Vita3K/ux0/app/<TITLEID>`, while the library
delivers each title to `~/Emulation/roms/psvita/app/<TITLEID>` (the `inbox` placement, see
[PS Vita games](#ps-vita-games)). A profile declares the bridge with `[links.<id>]`:

```toml
[links.vita3k]
source = "{library}/roms/psvita/app"     # a folder per title (the library path is expanded)
target = "~/Emulation/storage/Vita3K/ux0/app"
process = ["Vita3K"]                      # nothing changes while it runs
match = 'PCS[A-I]\d{5}'                   # a title folder's whole name (default: any)
require = ["eboot.bin", "sce_sys/param.sfo"]   # files a title must already hold
noun = "game"                             # what a title is called in messages (default)
create_target = false                     # make `target` when it is missing (default false)
```

The shipped profile has a second rule for the licences of the games, `[links.vita3k-license]`:
`source = "{library}/roms/psvita/license"`, `target = "~/Emulation/storage/Vita3K/ux0/license"`,
the same `match`, no `require` (a title is a folder of `.rif` files, which have no fixed
names), `noun = "license"` and `create_target = true`. Vita3K creates `ux0/app` when it starts
but `ux0/license` only when it installs a licence (`io::init` makes `data`, `app`, `music`,
`picture`, `theme`, `video` and `user`), so with `create_target` a missing `target` is made
(one level, when its parent exists; `--dry-run` says "would create"), after the check for the
running emulator and only when there is a title to link. When the parent is missing too, Vita3K
has never run and the rule says so like for any other target. Vita3K reads the licence by path
(`ux0/license/<TITLEID>/<CONTENT_ID>.rif`), which a link to the library folder satisfies; its
"delete game" removes `ux0/license/<TITLEID>` with `remove_all`, which removes a symbolic link
and not what it points at.

`warpzone/titlelinks.py` (no network code) keeps one absolute symlink `target/<title>` ->
`source/<title>` per title:

- A title is a real folder (not a symlink) of `source` whose name fully matches `match` and that
  holds every `require` file. A game that is still syncing has no link until it is complete.
- `plan(source, ready, current)` is pure. A title without an entry is created. A real folder
  or file under that name, or a link that points elsewhere, is kept and reported. An entry that
  is a link to `source/<title>` is correct. The only thing removed is a dangling link whose
  parent is `source` (one that this module made for a game that is gone); a dangling link
  pointing anywhere else is not ours. Both lists are re-checked right before each change
  (`readlink` and `exists`), and a link that appears meanwhile (`FileExistsError`) is not an
  error.
- Nothing is changed while a process of `process` runs (`settings_state.running`) or when that
  cannot be told (no `/proc`); the run says so. A missing `source` is "nothing to link"; a
  missing `target` means the emulator has not run yet, and it is not created. `--dry-run`
  prints "would link" and "would remove" and changes nothing. A second run changes nothing.
- It runs from `provision device` (for a device that lists `library`; `ensure_title_links`),
  from `warpzone links sync <device> [--dry-run] [--quiet]` and from an ES-DE startup script.
  The script, `<esde>/scripts/startup/warpzone-links.sh`, is written like the other two
  (see [ES-DE startup script](#es-de-startup-script)): the same `env -u ... timeout 15 <python>
  -m warpzone links sync '<device>' [--config ...] --quiet >/dev/null 2>&1` and `exit 0`, so
  ES-DE is never held up or stopped by a failing warpzone. The command is routed before any
  network import, like `settings apply` and `play-state sync`.
- **Doctor.** `<process> <noun> links` (`Vita3K game links`, `Vita3K license links`): a warning
  for library titles without a link, dangling links, a missing `target` (unless the rule makes
  it and its parent exists), and a missing startup script.
- **Vita3K.** From its public source, the app list (`apps_list.cpp`) is built from the
  directories of `ux0/app` with `std::filesystem::is_directory` (which follows symlinks) and
  is kept in `gui-configs/apps-cache.xml`, which is rebuilt when the directory names or the
  write times of `sce_sys/param.sfo` and `icon0.png` differ; `-r <TITLEID>` needs the title in
  that list. No cache entry or `ux0/temp` content has to be written. Running a game through a
  symlink on a real console is untested.

### Frontend artwork

A frontend that is not ES-DE shows nothing of what the hub scraped: ES-DE keeps its
gamelists and media in its own layout and the other frontends do not read it. A profile with
`[artwork]` closes that gap with data only; nothing in the code tests a profile or a device
name. The table:

```toml
[artwork]
format = "emulationstation"        # the converter, a key of artwork.FORMATS
kinds = ["covers"]                 # hub media kinds to receive, best first
systems = "library"                # which systems: those with a folder in the library's roms/
media = "/roms/warpzone/media"     # where wz-media lands on the device
metadata = "/roms/warpzone/es-de"  # where wz-esde lands on the device
gamelists = "~/.emulationstation/gamelists"  # where the generated gamelists go
refresh = "15min"                  # optional: a systemd --user timer interval
```

`format` must be in `profiles.ARTWORK_FORMATS` (a test keeps it in step with
`artwork.FORMATS`), `kinds` a non-empty list without repeats of `covers`, `screenshots`,
`miximages`, `marquees`, `3dboxes`, `physicalmedia`. The table needs `paths.library` and, for
`emulationstation`, `[es_systems]` (the system list names the systems and its `process` tells
whether the frontend runs). It replaces `paths.media` and `paths.esde`, so setting both is an
error, and neither folder may be the library, inside it or inside each other (Syncthing
folders do not nest).

**Delivery.** A device that lists `frontend` gets `wz-media` and `wz-esde` at `media` and
`metadata`, both receive-only and with `ignorePerms` like the rest of the device. The hub
folders are the ones the Steam devices already share and stay exactly as they are (type,
versioning, ignores): `hub_specs` builds them from the static definitions, never from the
profile, so the order of the devices in the config cannot change them; a test builds the hub
specs with and without the console, in both orders. Only the device side differs, in its ignore
lines: `!/<system>/<kind>` per system and kind for the media folder (the hub's layout is
`<system>/<kind>/...`), `!/gamelists/<system>/gamelist.xml` per system for the ES-DE folder,
then `*`. The systems are the plain-named directories below `<library>/roms` at the time
`device_specs` is asked (`artwork.library_systems`); a name that would be a pattern
(`*`, `?`, `[`, spaces) is left out. So the scope follows the whitelist without being edited
per system, but it is computed, not read from Syncthing: `provision device` sets it, and so does
every `frontend refresh` (`provision.sync_artwork_scope`, which patches only ignores that differ
and only for folders Syncthing already has). With no system folder yet the ignores let nothing
in and `provision` says so. Covers of every game of an included system arrive, also those whose
game is not on the device (a single ROM of a big system whitelists the whole system's covers);
the ROM check below keeps them out of the gamelist, not off the card.

**Converter.** `artwork.Format` is a frontend's gamelist shape: the fields it keeps from ES-DE
(`source_fields`; ES-DE-only tags such as `altemulator` are not copied), the fields the
frontend writes itself (`user_fields`), a map from hub media kind to the tag that holds the
path, and a `render` function. A new frontend with another file shape is one more `Format` and
one more name in `ARTWORK_FORMATS`; planning, writing, the timer and the doctor do not change.
`artwork.build` is pure and takes the existence checks as functions:

- ES-DE's gamelist is read from the synced `metadata` folder. ES-DE writes an
  `<alternativeEmulator>` element before `<gameList>` (two top-level elements), so a file that
  does not parse as one document is read from its `<gameList>` block. Entries with a path that
  is absolute or climbs out of the system folder (`..`) are dropped.
- An entry is kept only when its ROM exists on the device
  (`<library>/roms/<hub system>/<path>`).
- The cover is `<media>/<hub system>/<kind>/<path without extension>.png`, subfolders kept; the
  first kind of `kinds` whose file exists becomes the `<image>`, as an absolute path. Without a
  cover there is no `<image>`.
- Text is XML-escaped and characters XML 1.0 cannot hold are dropped. Entries keep the hub's
  order; the file layout is fixed, so the same input gives the same bytes.

**System names.** The gamelist goes under the frontend's *system name*, and the name is not
always the ROM folder: dArkOS's `ppsspp` reads `psp`, `mame2010` reads `mame`, `jaguar` reads
`atarijaguar`. So `artwork.frontend_systems` reads the system list (the OS's, after the same
rewrite as the override, which applies `[es_systems.rename]`: frontend folder -> hub folder,
so hub `pokemini` is the system `pokemonmini`) and takes `<name>` and the folder of each
entry. A system whose folder is below the hub's system folder (`pico-8` reads `pico8/carts/`)
gets paths relative to it. When several hub folders carry one name (`snes` has two entries),
the one the hub names alike wins and the rest are reported. One hub folder may feed several
names.

**User data.** The frontend rewrites its gamelist (`SaveGamelistsMode` is `on exit` on dArkOS)
with what the player does: `favorite`, `hidden`, `kidgame`, `playcount`, `lastplayed`. A
regeneration reads the existing file first and carries those fields over for the same path;
they are never taken from the hub's gamelists (a device that lists `play-state` gets the other
devices' statistics from their ledgers instead, see [Play state](#play-state)).
An entry the hub has no scrape for, but with user data on a ROM that is still there, is kept
with just that data. Descriptive fields and images always come from the hub, so editing a
description in the frontend's metadata editor is overwritten; edit the scrape on the hub.
No backup is made: the hub copy is the source and user data is kept. A gamelist whose
content already matches (`artwork.same_content`: same entries and known fields, whatever the
layout or the games listed without data) is not rewritten, so the frontend's own formatting
survives and a second run changes nothing.

**When it runs.** `provision.ensure_gamelists` is the writer, like `ensure_systems_override`: only
while no process in `[es_systems].process` runs (a missing `/proc` or a running frontend warns
and writes nothing, except when nothing would change), atomically, and refused when the file
changed after it was read. `provision device` calls it for a device that lists `frontend`;
`warpzone frontend refresh [device]` calls scope sync then the writer, for new scrapes and for
games that arrived later. To apply new scrapes without a manual step, a profile may set
`refresh`: `provision device` then writes `~/.config/systemd/user/warpzone-artwork.service`
(`Type=oneshot`, `ExecStart=<python> -m warpzone frontend refresh <device>`, naming `--config`
only when it is not the default) and `warpzone-artwork.timer` (`OnBootSec=5min`,
`OnUnitActiveSec=<refresh>`), then runs `systemctl --user daemon-reload`, `enable --now` and
`restart`. Units are rewritten only when their text differs; a second run makes no systemctl
call. The timer is the choice over a path unit because Syncthing writes many files per scrape
(a path unit fires per change) and because the same run also widens the scope. A run that
finds the frontend open changes nothing and exits 0, so the timer does not show as failed.
Like the ES-DE startup script it needs a Python that imports `warpzone` from outside the
current folder. The user manager must run without a login session (`loginctl enable-linger`;
dArkOS has it on).

The doctor reports missing and stale gamelists and the timer (see [Doctor](#doctor)).

### Play state

Favourites and play statistics of every frontend reach the others. It is opt-in per device (the
`play-state` folder group) and data-driven: a profile gives the folder (`paths.play_state`) and
a gamelist to work on (`paths.esde`, or `[artwork]` with a format), so a new frontend is one more
`artwork.Format`, which names its three tags in `state_tags`. Nothing tests a device or profile
name, nothing needs SSH, and the hub runs no code: it only stores the files, like any folder.

**Transport.** One two-way folder, `wz-play-state` (`frontend/play-state` on the hub, with
history), holds one JSON Lines file per device, `<device>.jsonl`, written by that device only:

```
{"v":1,"device":"rg351mp","replica":"rg351mp"}
{"system":"gba","path":"Hacks/Beta.gba","plays":3,"included":2,"last":1793000000,"fav":false,"fav_at":1793000100}
```

Lines are keyed by hub system and ROM path below it, so a frontend's own system names
(`pokemonmini`, `ppsspp`) never reach the file, and sorted, so the same state gives the same
bytes and Syncthing sends nothing. The alternative was sending the Anbernic's data through
`wz-esde`, which would have meant changing that folder's type and ignores on the devices, and a
single shared file would conflict in Syncthing as soon as two devices wrote it. With one writer
per file there are no conflicts, and `wz-esde` and `wz-media` stay exactly as they are (a test
compares their hub and device specs with and without the group). A device whose file cannot be
read is left out of the merge with a warning; a device whose own file cannot be read changes
nothing. Conflict copies are ignored. Files of devices that left the config still count: their
plays happened.

**Replicas.** The trap is that Deck and Machine share one gamelist. A count that rises in the
shared file may be a play on this device or one Syncthing delivered, and each device claiming
the difference as its own would count every play twice. So the unit of counting is the
*replica*, a gamelist: every device with `paths.esde` is in the replica `wz-esde`, any other
device is a replica of its own. A device's `plays` is its view of its replica's counter. The
highest view counts within a replica and replicas add up, which is the requested sum across
devices once the devices that read the same file are taken as one. Plays made on Deck and
Machine at the same time while offline are still decided by `wz-esde`'s last-write-wins, as they
always were.

**Merge rules** (`playstate.plan` and `merge`, pure; a merge depends only on the set of ledgers):

- *Play count.* A grow-only counter per replica; shown value = own + other replicas. The shown
  value also holds other replicas' plays, so a device must subtract them to read its own:
  `plays = max(plays, shown - included)`. The first run has `included = 0`, so existing counts
  are absorbed once, by the replica that has them, and `max` makes an older gamelist harmless.
  `included` has to arrive together with the count it explains. If it lived only in the ledger,
  the shared gamelist could arrive first (they are separate Syncthing folders) and the merged
  plays would read as local ones. So a shared gamelist carries it, in a `<warpzonePlayState>`
  child of `<gameList>` listing only games with plays from elsewhere. ES-DE's `updateGamelist`
  reloads the file and replaces only changed `<game>` nodes, keeping unknown children, and
  reads only `game` and `folder`. A generated gamelist belongs to one device, which keeps
  `included` in its own ledger, because such a frontend may rewrite the whole file. If a future
  ES-DE dropped the element, the ledger takes over; two devices running before their ledgers
  cross could then overcount once, and the count would not keep growing (tested).
- *Favourite.* A last-writer-wins register per game with a change time. A value that differs
  from the device's record is a change, so an unset is recorded rather than being a missing
  value, and a device that still shows the old value receives the unset instead of bringing the
  favourite back. Equal times resolve to `true`, the same on every device. A favourite that a
  device already had at its first run is stamped `1`, older than any real change: first runs
  join their favourites and undo no recorded unset.
- *Last played.* The latest instant wins.
- `hidden` and `kidgame` stay per device. Scraped fields and media are not touched.

**Clocks.** Gamelists hold naive local time, and consoles differ (one reported EST while the
others use CET, with the same correct instant). Ledgers hold UTC epochs: a device converts with
its own zone when it reads a gamelist and when it writes one. A favourite's change time is the
gamelist's modification time, capped at the device's now: closer to the real change than the
moment of the run, which may be days later, and free of zones. Clocks can also be wrong, so a
change is always stamped after anything the device has seen for that game (a Lamport clock). A
change made after seeing another therefore wins on a slow clock, and a device whose clock ran
ahead can still be overridden once seen. Only changes that never saw each other are decided by
the clocks themselves. A last played time more than a day ahead of the reader is ignored as
a wrong clock, and stays in its ledger.

**Absent games.** A game that is neither in the gamelist nor on the device keeps its record
and gets nothing (`present`), so state waits for a ROM that may arrive and survives a ROM that
left. A game in the gamelist is updated even when its ROM is elsewhere (a shared file lists
what other devices have). A ROM that is here but missing from the gamelist gets an entry; ES-DE
fills in the name.

**ES-DE.** `gamelists/<system>/gamelist.xml` is edited only while ES-DE has no gamelist in
memory. ES-DE fires its `startup` event before `loadSystemConfigFile()` reads them and waits for
the script, so `<esde>/scripts/startup/warpzone-play-state.sh` runs `play-state sync
--frontend-starting`, which is the one case where the process is alive and the files are still
free. The flag makes `running` skip the processes above this one, so another ES-DE still counts.
With ES-DE open a run only reads and publishes. A `[play_state] refresh` timer
(`warpzone-play-state.timer`) publishes between starts, and merges when ES-DE is closed.
Edits (`edit_gamelist`, pure) change only the three tags and the element, keep every other byte
and line ending, must leave every other field of every entry equal, and never remove an
entry. Each file is backed up to `<state>/warpzone/play-state/backups` and written atomically,
refused if it changed. A run is bounded by the script's `timeout`, imports no network code, and
took about 30 ms on 767 games. Gamelists that say the same are not written.

**Generated gamelists.** `ensure_gamelists` reads the frontend's files and the ledgers
(`prepare_generated`) and hands the result to `artwork.build` as an overlay of user fields, so
`frontend refresh` and its timer do all the work, on the same "only while closed" rule. With
the frontend open the device publishes what it saved last and writes no gamelist. The ledger is
written before the gamelists: a run cut short between the two loses at worst the plays of that
moment, where the other order would count every merged play as played here.

**Known limits.** Deck and Machine still overwrite each other's gamelist when both change it
offline, so a favourite applied on one can look like an unset on the other if an older file
wins. Counts only grow, so resetting one in ES-DE comes back at the next merge. Latency is one
timer period plus Syncthing, or the next ES-DE start for what reaches a running ES-DE.

## Private configuration

Lives outside the repo, at `$XDG_CONFIG_HOME/warpzone/config.toml`
(default `~/.config/warpzone/config.toml`). It is the only place with personal
data. `examples/config.example.toml` documents every key.

```toml
[hub]
name = "nas"
device_id = "XXXXXXX-..."
root = "/volume1/games"                    # path on the hub's filesystem
api_url = "http://nas.local:8384"
api_key_file = "~/.config/warpzone/hub.key"  # or api_key = "..."
addresses = ["tcp://nas.local:22000"]        # optional, default ["dynamic"]

[devices.deck]
device_id = "YYYYYYY-..."
profile = "steamos-emudeck"
folders = ["library", "tools", "saves", "frontend"]
addresses = ["tcp://deck.local:22000"]       # optional, default ["dynamic"]
self_update = true                           # optional: install the self-update timer (default true)
channel = "stable"                           # optional: "stable" (default) or "rc"
pin = "v0.3.0"                               # optional: stay on this tag (vX.Y.Z or vX.Y.ZrcN)

[devices.machine]
device_id = "ZZZZZZZ-..."
profile = "steamos-emudeck"
folders = ["library", "tools", "saves", "frontend", "songs", "settings"]

[inbox]                                      # optional, read only by `warpzone inbox`
root = "/Volumes/hub"                      # the hub root as mounted on this machine
dats_dir = "~/dats/libretro-database/metadat"
work_dir = "~/tmp/warpzone"                  # optional; default: the system temp folder
chdman = "/opt/homebrew/bin/chdman"          # optional tools; PATH is searched when unset
bsdtar = "/usr/bin/bsdtar"
rahasher = "~/tools/RAHasher"                # only used when set
dolphin_tool = "/usr/local/bin/dolphin-tool"
mame_dat = "~/dats/mame0282lx.zip"            # optional arcade databases (see Inbox)
fbneo_dat = "~/dats/fbneo-arcade.dat"
mame2003plus_dat = "~/dats/mame2003-plus.xml"
gamelist_mame = "/Volumes/hub/frontend/es-de/gamelists/mame/gamelist.xml"  # optional
gamelist_ps3 = "/Volumes/hub/frontend/es-de/gamelists/ps3/gamelist.xml"  # optional
```

`warpzone inbox` reads only the `[inbox]` table, so a desktop that only files games
needs no `[hub]` or `[devices]`. API keys are never printed, logged or included in
error messages; the RetroAchievements key is read from `RA_API_KEY`, never from a file.

## Command line

`python3 -m warpzone <command>`; also installable as a `warpzone` script.

| Command | Runs on | Does |
|---|---|---|
| `warpzone check` | anywhere | Validates config + profiles, reports which whitelists are missing on the hub (via `GET /rest/db/browse`), never writes. Exit 1 if any are missing; `--offline` skips the hub. |
| `warpzone provision hub` | anywhere that reaches the hub API | Adds every configured device to the hub, creates/updates every `wz-*` folder on the hub and shares it with the devices that subscribe to it, sets the hub ignores. A newly created whitelisted folder first lets only the whitelists through and waits for them (`--timeout`, default 120 s) before the full scan, see "Hub bootstrap". Idempotent. |
| `warpzone provision device <name>` | on the device | Reads the local Syncthing API key from the local `config.xml` (searching the default locations, including the SyncThingy flatpak; `--syncthing-config PATH` overrides), adds the hub device, creates the subscribed folders with the bootstrap above (`--timeout SECONDS`). `--unattended` is for a run nobody watches and changes the rules as described in [Autonomous updates](#autonomous-updates-contract); it exits `3` when a write waits for the menu to close. When that Syncthing is the SyncThingy flatpak it first grants it the missing `flatpak override --user --filesystem=~/.var/app/<app>` for every folder under `~/.var/app` (flatpak hides other apps' data), and asks for a Syncthing restart. When the device subscribes to `settings`, it installs the ES-DE startup script that runs `settings apply` (see [ES-DE startup script](#es-de-startup-script)). When the device subscribes to `library` and its profile has `[es_systems]` or `[retroarch]`, it also writes the frontend's system list override and pins RetroArch's paths (see [Frontend and RetroArch overrides](#frontend-and-retroarch-overrides)). When the profile has `[shadps4]` it also lists the PS4 games folder in shadPS4's install directories (see [shadPS4 install directories](#shadps4-install-directories)). When the profile has `[links.*]` and the device lists `library`, it links the library games into the emulator's game folder and installs the ES-DE startup script that keeps them linked (see [Title links](#title-links)). When it lists `frontend` and the profile has `[artwork]`, it limits the artwork folders to the systems on the device, writes the gamelists and installs the refresh timer (see [Frontend artwork](#frontend-artwork)). Idempotent. |
| `warpzone frontend refresh [device]` | on the device | For a device whose profile has `[artwork]` and that lists `frontend`: widens the artwork folders' ignores to the systems that have games now (when the local Syncthing is found) and rewrites the frontend's gamelists from the synced ES-DE data, only while the frontend is closed. `--dry-run` prints what it would do. See [Frontend artwork](#frontend-artwork). |
| `warpzone play-state sync <device>` | on the device | For a device with ES-DE that lists `play-state`: publishes what its gamelists say and merges the other devices' ledgers into them, only while ES-DE is closed or starting (`--frontend-starting`, which its startup script passes). `--dry-run`, `--quiet`. Needs no network. See [Play state](#play-state). |
| `warpzone provision device <name>` | on the device | Reads the local Syncthing API key from the local `config.xml` (searching the default locations, including the SyncThingy flatpak; `--syncthing-config PATH` overrides), adds the hub device, creates the subscribed folders with the bootstrap above (`--timeout SECONDS`). When that Syncthing is the SyncThingy flatpak it first grants it the missing `flatpak override --user --filesystem=~/.var/app/<app>` for every folder under `~/.var/app` (flatpak hides other apps' data), and asks for a Syncthing restart. When the device subscribes to `settings`, it installs the ES-DE startup script that runs `settings apply` (see [ES-DE startup script](#es-de-startup-script)). When the device subscribes to `library` and its profile has `[es_systems]` or `[retroarch]`, it also writes the frontend's system list override and pins RetroArch's paths (see [Frontend and RetroArch overrides](#frontend-and-retroarch-overrides)). It then installs the self-update timer, for every profile (see [Self-update](#self-update)). Idempotent. |
| `warpzone links sync <device>` | on the device | For a profile with `[links.*]` and a device that lists `library`: makes each rule's `target` hold a symlink per library title, never while the emulator runs (see [Title links](#title-links)). `--dry-run` only says what it would do, `--quiet` prints nothing. ES-DE runs it at startup. |
| `warpzone prune [device]` | on the device | Reports, and with `--apply` deletes, the files of the whitelisted folders that the device no longer whitelists (see [Pruning](#pruning)). Report only by default. |
| `warpzone bios` | on a device or the hub | Reports which BIOS and firmware files a library has, lacks or has wrong (see [BIOS check](#bios-check)). Read-only. Exit 1 if a required item is missing or wrong. |
| `warpzone inbox` | a desktop that mounts the hub's share | Identifies, converts and files new games dropped in `inbox/` (see [Inbox](#inbox)). Report only by default, `--apply` acts. Exit 1 if any item needs review. |
| `warpzone self-update` | on the device | Moves the git checkout warpzone runs from to the newest `vX.Y.Z` tag when it is newer than `__version__` (see [Self-update](#self-update)). Needs no config. `--dry-run` reports only. Exit 0 for every outcome that needs no attention (including no network and not a checkout), 1 when a due update was refused or failed. |
| `warpzone health [device]` | on the device | Checks that this version works here: loads the config and the profiles, runs `settings apply`, `play-state sync`, `links sync` and `frontend refresh` as dry runs within the 15 seconds ES-DE gives its startup scripts, and checks that the startup scripts and the units exist with an interpreter that imports warpzone (see [`warpzone health`](#warpzone-health)). Writes no user data, needs no DATs or tools. Exit 0 when it works, 1 when a check failed. |
| `warpzone doctor` | any machine | Checklist of this machine: tools, config, local Syncthing, DATs, hub folder (see [Doctor](#doctor)). Offline, never changes anything. Exit 1 if a required check fails. |
| `warpzone settings apply` | on the device | Writes the layered emulator settings into each emulator's config file: only the layered keys, with a backup, never while the emulator runs (see [Emulator settings](#emulator-settings)). Run by ES-DE at startup and by hand. |
| `warpzone settings capture` | on the device | Shows what changed in the emulators' settings since the last sync and writes the keys the user chooses to the common or the device layer on the hub. |
| `warpzone settings audit` | on the device | Reports drift between the layers and the emulators' files. Read-only. Exit 1 on drift. |
| `warpzone status` | anywhere | Folder state, need counts, errors, receive-only local changes, for the hub (and the local device if any). |
| `warpzone render-ignores <device>` | anywhere | Prints the ignore lines a device folder would get. For debugging. |

Every writing command supports `--dry-run`, which prints the REST calls it
would make (method, path, JSON body with secrets redacted) and sends no write.
It still sends the GET requests it needs to compute the diff. Every command
takes `--config PATH` (default `$WARPZONE_CONFIG`, then the XDG path).

Exit codes: `0` ok, `1` runtime error (for `health`, a failed check, a config error included), `2` usage or config error, and `3` only for `provision device --unattended` (deferred).

`prune` has its own flags: `--apply`, `--max-gib N` (default 50), `--force`,
`--list`, `--notify-url URL`, `--timeout SECONDS` (wait for the rescan),
`--syncthing-config PATH`. It does not take `--dry-run`: without `--apply` it
is one. If the device name is left out, it is the configured device whose ID
matches the local Syncthing.

Idempotency rules for `provision`:

- Existing `wz-*` folders are updated in place with `PATCH`, only for the keys
  warpzone owns (`label`, `path`, `type`, `devices`, `versioning`,
  `fsWatcherEnabled`, and `rescanIntervalS` on whitelisted device folders).
  Unknown keys are left alone. `devices` is merged: devices
  already on the folder are kept, missing ones are added.
- Folders not prefixed `wz-` are never touched.
- Changing a folder's `path` on an existing folder is refused (it would make
  Syncthing see a mass delete); the user is told to remove the folder first.
- Devices are only ever added, never removed, by warpzone.

## Pruning

Removing a pattern from a device's whitelist stops Syncthing from syncing those
files but leaves the copies on the device. `warpzone prune` deletes them,
safely. It runs on the device against the local Syncthing, and only ever looks
at the folders warpzone manages **and** whitelists (`wz-library`, `wz-songs`).

### How a leftover is detected

warpzone does not parse ignore patterns. Syncthing already decides what is
ignored, and records the verdict in the folder database. Verified against
Syncthing v2.1.7 with two real instances (hub send-only, device receive-only):

- After a **full scan**, `GET /rest/db/file?folder=&file=` on a file that was
  synced and is now ignored answers `local.ignored: true`, `local.invalid:
  true`, `local.localFlags: 2`, `local.size: 0`, while `global` still holds the
  hub's record (`ignored: false`, real size, hub modification time). Directories
  behave the same. This is the signal. `GET /rest/db/ignores` has no "does this
  path match" call (it returns the patterns and an `expanded` list), which is
  why the matcher is not reimplemented.
- The flags are refreshed **only by a full scan**. Right after
  `POST /rest/db/ignores`, files still read `ignored: false` (only a directory
  was flagged); and after re-whitelisting, files still read `ignored: true`
  until the next scan. So `prune` always runs `POST /rest/db/scan` first and
  waits for it (`GET /rest/db/status`: `state` leaves `scanning`/`scan-waiting`;
  `stateChanged` moving or a grace period of a few seconds covers scans that
  start and end between two polls). It therefore does not depend on
  `rescan_seconds`.
- A file Syncthing never recorded (ignored since before it ever scanned it, for
  example something the user created, or a file copied in by hand) answers 404.
  After the full scan this means "not synced and not ours": it is counted and
  left alone, never listed as a leftover.
- A device-only file that was scanned **before** it became ignored is mirrored
  into `global` with `invalid`/`ignored` set, so `global` existing does not
  prove the hub has it. The hub's copy counts only when `global` is not
  `deleted`, `ignored` nor `invalid`.
- Not usable: `GET /rest/db/browse` lists the global tree, ignored or not, with
  no flag; `GET /rest/db/need` and `GET /rest/db/localchanged` came back empty
  for ignored files (they list what is outstanding or locally changed, not what
  is ignored).

`prune` walks the folder on disk (never following symlinks) and asks
`GET /rest/db/file` once per file; the answer is about 1 ms on loopback, so
100k files take on the order of a minute and a half.

### Safety rules

A file is deleted only if all of these hold, re-checked right before each
delete:

1. The folder is a warpzone whitelisted folder, receive-only, not paused, at the
   path the config says, and its marker directory (`.stfolder`) exists (an
   unmounted SD card must not look like an empty library).
2. The folder's ignores are the final `#include patterns-<device>.stignore`
   (the bootstrap ignores, `*`, would mark the whole library as ignored), they
   load without error, and the whitelist files are in sync with the hub.
3. Syncthing flags it `local.ignored` after the full scan.
4. The hub has it: `global` is not deleted, ignored or invalid, and has the same
   kind (file or symlink).
5. For regular files, the size equals the hub's and the modification time is
   within 2 s of the hub's (FAT stores 2 s steps). A file edited on the device
   after it was synced is reported, not deleted.
6. It is not `.stfolder`, `.stignore`, `.stversions` or `patterns-*.stignore` at
   the folder root, nor a Syncthing temp file, and it lives in a directory
   whose real path is inside the folder root. Symlinks are unlinked, never
   followed. The subtree of any other Syncthing folder nested in the root (the
   save folders under `~/Emulation`) is skipped.

Files that are ignored but fail 4 or 5 are listed as kept, with the reason. After
deleting, directories that held deleted files, that Syncthing flags ignored, and
that are now empty are removed (`rmdir`, so a non-empty one is never touched).

The default is report only. `--apply` deletes. A run that would delete more than
`--max-gib` (default 50) refuses to delete anything, exits 1, and needs
`--force` or a higher cap; the cap counts the total over all folders.

Output is a per-folder summary (leftover files and bytes, grouped by the first
two path levels; `--list` prints every file), the files kept and why, and the
count of files Syncthing does not know. Exit codes are those of the other
commands: `0` ok (a report with leftovers is still `0`), `1` for a runtime
error, a folder that was skipped as unsafe, a refused cap or files that could not
be deleted, `2` for usage or config errors.

`--notify-url URL` (http or https) POSTs one JSON object per folder that has
leftovers: `{"device", "folder", "files", "bytes", "applied"}`. When applied,
`files` and `bytes` are what was actually deleted. It is meant for a Home
Assistant webhook. The URL may embed a secret, so it is never printed, and a
failed notification is a warning, not a failure of the run. No API key or path
appears in the payload.

## BIOS check

`warpzone bios` compares a library folder with a manifest shipped in the package
(`warpzone/data/bios.toml`) and reports what is present and correct, missing, or
has an unexpected hash. The contract:

- **Never downloads, never ships.** The manifest holds file names, sizes, md5
  hashes, zip member CRC32s and file signatures, all public metadata, and
  nothing else. The command only reads the library and writes nothing.
- **Data and curation.** File entries come from libretro-database
  `dat/System.dat`; the MAME zip sets (`neogeo.zip`, `naomi.zip`, `awbios.zip`,
  `bubsys.zip`, `ym2413.zip`, `qsound_hle.zip`) from MAME 0.282's own source.
  The header of `bios.toml` says where each part came from and how to
  regenerate it. Which items are required, and the groupings, are decided by
  hand.
- **Items.** An item is one thing to have and is satisfied when any one of its
  files is ok, which is how "one PlayStation BIOS of your region" is expressed.
  `any_name` items also accept a file of the same folder and size whose hash
  matches under another name, for emulators that identify a BIOS by content.
  An item with no hash (DS `firmware.bin`, which differs per console) checks the
  size only. Zip items live under `roms/` and pass when every listed member has
  the listed CRC32; extra members are ignored.
- **Kinds of check.** A file entry is judged in one of four ways (`check` in the
  manifest). `hash` (the default) is size and md5. `magic` is for official
  firmware that has no fixed hash (PS3 `PS3UPDAT.PUP`): the file must start with fixed bytes, and only that
  many bytes are read. `present` is existence of a non-empty file. `keys` is for
  a secret keys file (Cemu `keys.txt`): the file is read only to answer whether
  it looks like one (hex-led lines, `#` comments), and the verdict is a fixed
  text: its content is never printed, hashed or placed in a message. The Xbox
  flash BIOS has no fixed hash, so any 1 MiB file named `Complex_4627v1.03.bin`
  (the name EmuDeck's xemu config uses) or `Complex_4627.bin` passes on size
  alone. The xemu HDD image is not checked: it is per device, holds the saves and
  lives in `storage/xemu/`. The PS3 firmware and Cemu's `keys.txt` in `bios/` are
  staging copies (RPCS3 installs the firmware once; Cemu reads its keys from
  its own folder). Xbox, PS3 and Wii U are judged by the `xbox`, `ps3` and `wiiu`
  ROM folders; `mlc01/` (Cemu saves and installed titles) does not count as games.
- **Reading files.** The `bios/` folder is walked once, never following
  symlinks, and `skip_dirs` (`eden`, `azahar`: console keys and firmware) are
  neither listed nor opened. A file is hashed, in chunks, only when its size
  equals the expected one. Zips are read through their central directory.
- **Local overrides.** `bios.toml` next to the private config (or
  `--overrides PATH`) may list extra accepted md5 hashes per manifest file
  (`[saturn.accept]`) and items to skip (`[nds] unwanted = ["firmware"]`). A
  file accepted that way is reported "accepted (local)". Unknown systems, files
  or items in that file are errors, so a typo cannot silently accept nothing.
- **Games decide what is judged.** Each system lists the `roms/` folders that
  hold its games (`games` in the manifest, EmuDeck/ES-DE names, for example
  `segacd = [segacd, megacd, megacdjp]`). A folder has games when it holds any
  file other than the EmuDeck placeholders (`metadata.txt`, `systeminfo.txt`),
  downloaded media, hidden or OS junk files and the BIOS zips of the manifest
  (`is_game_file`, `has_games`; the walk stops at the first game). A system
  with no games is reported as one line, "no games, not checked", and never
  affects the exit code, so a missing BIOS for a console nobody plays is not a
  false alarm. `--system NAME` forces the check. The MAME sets (Neo Geo, NAOMI,
  Atomiswave, device sets) share `mame`, `arcade` and `fbneo` with their own
  folder, so any arcade game makes them count.
- **Eden firmware layout.** `skip_dirs` hides `eden/` from the manifest checks,
  but `bios/eden/firmware` gets one layout check (`judge_firmware`, pure, over
  the names directly inside it and whether each is a folder). Eden reads only
  loose `.nca` files in that folder, so subfolders and `.zip`/`.7z` archives are
  each reported as a `warning:` line (and one more when no loose `.nca` is
  there at all). Nothing is opened. It runs only when every system is checked
  and never changes the exit code.
- **Exit code.** `0` when no required item is missing or wrong; `1` otherwise;
  `2` for a usage or override-file error. A wrong file that sits next to an ok
  alternative is reported but does not fail the run. Optional items never fail it.
- **Root.** `--root` is the library folder (the one holding `bios/` and
  `roms/`). Without it: the first existing `library` path of the configured
  profiles, then `<hub root>/library`. The list of unknown files in `bios/` is
  only computed when every system is checked.

The decisions (`judge_file`, `judge_zip`, `judge_magic`, `judge_present`,
`judge_keys`, `judge_firmware`, `looks_like_keys`, `settle`, `unknown_files`,
`default_root`, `is_game_file`, `has_games`, `render`) are pure and unit-tested without a filesystem; `Library`
and `check_library` are the only parts that touch the disk.

## Inbox

`warpzone inbox [--root PATH] [--dats PATH] [--work-dir PATH] [--apply]` files new
games dropped in `inbox/` into `library/roms/<system>/`. It runs on a desktop that
mounts the hub's share, because conversions are too heavy for a NAS, and it reads no
Syncthing state. The contract:

- **Layout.** `--root` (or `[inbox] root`) is the hub root as mounted on this machine:
  it must hold `inbox/` and `library/roms/`. Items are the top-level files and folders
  of `inbox/`. Hidden files, `@eaDir`, `#recycle`, `Thumbs.db` and any `_*` folder
  (`_done`, `_revisar`) are never items. An item modified in the last 30 seconds is left
  alone, since it may still be copying.
- **Report only by default.** Without `--apply` it unpacks into a local work folder,
  identifies, plans and prints, and writes nothing to the share. With `--apply` it
  converts, places and files the originals.
- **Untrusted content.** Archives are unpacked only into the local work folder (`.zip`
  with `zipfile`, `.7z` and `.rar` with `bsdtar`; archives inside archives up to two
  levels). A member with an absolute path, a `..` component or a NUL byte, or a symbolic
  link, makes the whole item go to review, and nothing is extracted before every name is
  checked. A cue or gdi is followed only if every file it names is a file of the item.
  Symbolic links in the inbox are skipped.
- **Identification by content.** Every file that is not a sidecar (`.cue`, `.gdi`,
  `.txt`, images, ...) is read once for size, CRC32 and SHA1, and looked up in the
  Redump and No-Intro DATs (clrmamepro format, libretro-database's `metadat/`). A rom
  matches on size and CRC32, and also SHA1 when the DAT has one; a game matches when all
  its roms are present, so a DAT that lists only the data track of a disc (as
  libretro's do) still matches, and the other tracks are taken from the cue or gdi that
  names it. File names never matter. Two games with exactly the same files cannot be
  told apart and go to review. The DAT file name maps to the system folder with a small
  table (`SYSTEMS` in `warpzone/dat.py`: PlayStation `psx`, PlayStation 2 `ps2`,
  PlayStation Portable `psp`, Saturn `saturn`, Dreamcast `dreamcast`, GameCube `gc`,
  NES `nes`, SNES `snes`, Nintendo 64 `n64`, Game Boy `gb`, Game Boy Color `gbc`, Game
  Boy Advance `gba`, Mega Drive / Genesis `genesis`, Master System `mastersystem`, Game
  Gear `gamegear`); a trailing No-Intro date stamp is ignored. DATs are parsed only when
  a lookup needs them (the system the file extensions and sizes point at first, and the
  search stops once every file is explained) and each parsed DAT is cached as JSON in
  `$XDG_CACHE_HOME/warpzone/dats`, keyed by the DAT's size and modification time.
- **Formats follow the library.** `psx`, `saturn`, `dreamcast`: `chdman createcd` from
  the cue or gdi, then `chdman verify`. `ps2`: `createdvd` for an `.iso`, `createcd`
  for cue/bin. A lone `.bin` without a cue gets a generated one (PlayStation only).
  `psp`: the plain `.iso`, copied and its CRC32 checked against the DAT. `gc`: an
  `.iso` stays an `.iso` (the report prints the `dolphin-tool convert -f rvz -c zstd
  -l 5 -b 131072` command; warpzone never converts it); an `.rvz`, `.gcz` or `.wia` is
  identified by expanding it with `dolphin-tool convert -f iso` into the work folder and
  the compressed file itself is what gets placed, as `<game>.rvz`. Cartridge systems: a
  `<game>.zip` with the rom under its DAT name, always the same bytes for the same game.
  Names are the DAT's game names. A multi-disc game (`(Disc N)` in the DAT name) of a
  CHD system goes in `<game without disc tag>.m3u/` with every disc and a
  `<game>.m3u` playlist; GameCube discs stay loose files, as in the library. A game is
  placed only when every disc the DAT lists is in the item (`incomplete multi-disc`
  otherwise); a disc that is the only one its DAT lists is a plain game.
- **RetroAchievements (optional).** With `[inbox] rahasher` set, the RA hash of the
  source (the cue, gdi, iso or rom; a Redump cue of a Dreamcast disc is turned into a
  temporary gdi, which RAHasher needs) is compared with the hash of the result (the
  CHD or zip). If they differ, nothing is placed and the item goes to review. With
  `RA_API_KEY` in the environment the hash is looked up in the console's game list (one
  request per console per run, `User-Agent: warpzone/<version>`) and reported as
  `supported (<title>)`, `hash not linked` or `linked, no achievements yet`. A lookup
  failure is reported by kind only; the key is never printed or put in an error. RA
  never decides whether a game is filed: only a changed hash does.
- **Never overwrites.** The result is copied to `<name>.part`, read back and compared,
  checked against the DAT where it is a plain file, and only then renamed. If the name
  exists: the same content (equal chdman data SHA1, equal zip members, equal SHA1) is
  reported as "already in the library" and the item counts as done; anything else goes
  to review as a conflict. An existing `<game>.m3u/` folder is never merged into.
- **Where the originals go.** Items fully handled go to `inbox/_done/<YYYY-MM-DD>/`.
  Items with anything that could not be placed go to `inbox/_revisar/` with
  `<item>.reason.txt` (no DAT match, ambiguous match, files not in any DAT, incomplete
  multi-disc, conversion or verify failed, RA hash changed, conflict, missing tool,
  unsafe archive, not supported yet). Games of such an item that could be placed are
  placed; running the item again reports them as already in the library. Names that are
  taken get ` (2)`, ` (3)`, ... On every `--apply` run, folders of `_done` whose name is a
  date older than 7 days are deleted; nothing else in `_done` is touched, and a symbolic
  link is not followed. Without `--apply` the run only says what it would delete.
- **Whitelists are never edited.** The report prints, per placed game, the whitelist
  line to add (`!/roms/psx/Silent Hill (Europe) (En,Fr,De,Es,It).chd`; Syncthing
  wildcards in names are escaped) and a combined list at the end.
- **Missing tools.** `chdman`, `bsdtar`, `RAHasher` and `dolphin-tool` are located with
  `[inbox]` config keys or PATH (RAHasher only when configured). A tool that an item
  needs and is missing sends that item to review with a clear reason; other items
  continue. A configured path that does not exist is a configuration error.
- **Arcade (MAME) sets.** See [Arcade sets](#arcade-sets) below; they do not go through
  DAT matching or conversion.
- **Switch games.** See [Switch games](#switch-games) below; they do not go through DAT
  matching or conversion.
- **PS3, Xbox, Xbox 360 and Wii U images.** See [Console disc images](#console-disc-images)
  below; they are identified by signature, not by DAT.
- **3DS images.** See [3DS images](#3ds-images) below; `.3ds` and `.cci` files are
  identified by their NCSD and NCCH headers, not by DAT.
- **PS4 packages.** See [PS4 packages](#ps4-packages) below; with `orbis-pkg-util` they are
  extracted into a game folder with a shortcut.
- **Out of scope.** Vita, and PS3 or Xbox content that is not a disc image (`.pkg`,
  `.rap`, an extracted `PS3_GAME` or `default.xbe` folder). Their files are recognised by
  extension or folder and go to review with "not supported by inbox yet". A disc image kept as a
  single `.bin` for several tracks is not identified (the DATs list one file per track).
- **Exit code.** `0` when every item is placed or already in the library; `1` when any
  item went to review; `2` for a usage or configuration error.

The decisions (`skip_name`, `unsafe_member`, `select_purge`, `plan_units`,
`destination_verdict`, `whitelist_line`, `resolve_refs`, `unsupported_reason`,
`match_dat`, `group_discs`, `parse_dat`) are pure and unit-tested; conversions are
tested with stand-in `chdman` and `RAHasher` scripts, and one test runs the cartridge
flow end to end with no external tool.

### Switch games

A Switch dump is identified by its TitleID and version, not by a DAT, named after
blawar/titledb and filed under the library's existing conventions. The code is in
`warpzone/inbox_switch.py`, a second handler in `inbox.kind_handlers()`.

- **Which items.** A top-level `.nsp`, `.nsz`, `.xci` or `.xcz`, or a folder or archive
  (`.zip` by its directory, `.7z` and `.rar` by `bsdtar -tf`) holding at least one. The item
  is taken whole: its Switch files are placed, and any other file in it (sidecars aside) is
  named in the review reason. Archives are unpacked into the work folder with the same
  safety checks as the rest of the inbox.
- **Identification.** The CNMT is inside an encrypted NCA, so `nsz -i --depth 3 --keys
  <keys> <file>` reads it: the output is parsed for the `Cnmt` blocks (`titleId`, and
  `version` and `titleType`, both in hex). `nsz` also prints key material in its report, so
  its output is never shown: lines mentioning a key are dropped from anything quoted.
  A file with several CNMTs goes to review; a CNMT `titleType` that disagrees with the
  TitleID is a review; the file wins over a different `[TitleID][vN]` in its name (noted).
  Without `nsz` or the keys file, an `.nsp` takes `[TitleID][vN]` from its name and says
  so; an `.nsz` is then reviewed. hactool is not used.
- **Kind.** From the TitleID: ends in `000` game, `800` update, otherwise DLC. The game of
  an update is its id minus `0x800`; of a DLC, `(id & ~0xFFF) - 0x1000`.
- **Names.** Game: `roms/switch/<Title> [<TitleID>][v<N>][BASE].nsp`. Update:
  `extras/switch/updates/<Title>/<Title> [<UpdateTitleID>][v<N>][UPD].nsp`. DLC:
  `extras/switch/dlc/<Title>/<Title> - <DLC> [<DlcTitleID>][v<N>][DLC].nsp`. `<Title>` is
  the game's name in the first titledb region file that has it (`US.en.json`, then the
  other English ones, then the rest; a full-game entry beats a demo). Normalization:
  typographic quotes and dashes become ASCII, trademark signs go, `:` becomes ` - `, path
  characters (`\ / | * ? < > "` and controls) are dropped or replaced, spaces are collapsed,
  and a word written in capitals is capitalized unless it is a roman numeral, a one-letter
  word or a known short acronym (`DLC`, `BGM`), and the small words of a title (`of`, `the`)
  go to lower case. A DLC's own name has the game's title removed from its start (also the
  title without its ` - subtitle`). Names are cut to fit 255 bytes, keeping the `[...]` tail.
- **Bundle parts.** A titledb entry whose `ids` lists several ids is a bundle. If its own
  id comes first it is the pack itself and is left out; the other ids, in listed order,
  are its parts. A DLC with no standalone entry in the region file that knows it is named
  `<Bundle> (part N of M)`. A standalone entry (no `ids`, or only its own) names the DLC
  itself.
- **Compressed files.** `.nsz` is decompressed with `nsz -D --keys <keys> -o <work>` only
  with `--apply`; the report only reads it with `-i`. `nsz` exits 0 on some damaged
  inputs, so the output is accepted only if `nsz` exited 0 and printed `[DONE]`, its PFS0
  header parses (magic, entry table, member bounds), and `nsz -V` exits 0 and prints
  `[VERIFIED]`. Anything else is a review and the partial output is deleted with the work
  folder. Only the `.nsp` reaches the library. `.xci` and `.xcz` are reviewed with the
  reason that no tool converts them cleanly.
- **Never overwrites.** The library is indexed once per run by `[TitleID][vN]` in file
  names (`roms/switch`, `extras/switch/updates`, `extras/switch/dlc`). The same TitleID and
  version, under any name, with identical bytes (size, then SHA-256) is "already in the
  library"; different bytes are a review. Placement goes through `<name>.part`, read back
  and renamed, as everywhere else.
- **Whitelists.** A game prints its `!/roms/switch/...` line. Updates and DLC print none;
  the report says once that they are usually whitelisted by folder
  (`!/extras/switch/updates`, `!/extras/switch/dlc`).
- **Configuration.** `[inbox] nsz` (the tool; PATH otherwise), `titledb_dir` (a clone of
  blawar/titledb), `switch_keys` (default `~/.switch/prod.keys`). A configured
  `titledb_dir` or `switch_keys` that does not exist stops the run (exit 2); unset or absent
  ones only make the affected files go to review or fall back to file names. The titledb
  region files are indexed into small JSON caches under `$XDG_CACHE_HOME/warpzone/titledb`,
  keyed by size and modification time. The keys file is never opened by warpzone.

### Console disc images

PS3, Xbox, Xbox 360 and Wii U images have no Redump or No-Intro entry, so they are identified by
signature. The code is in `warpzone/inbox_console_iso.py`, which is both a kind handler in
`inbox.kind_handlers()` and the one `UnmatchedHandler` in `inbox.unmatched_handlers()`.

- **Signatures.** Reads are bounded (a few KiB at fixed offsets, directories up to 1 MiB,
  a PARAM.SFO up to 64 KiB); an image is never read whole to be identified.
  - *XDVDFS* (Xbox, Xbox 360): at each candidate partition offset (0, 0xFDA0000 XGD2,
    0x2080000 XGD3, 0x18300000 Redump Xbox) the volume descriptor at +0x10000 must start
    with `MICROSOFT*XBOX*MEDIA`; root sector (u32 at +0x14) and size (u32 at +0x18) give
    the root directory, whose entries form a binary tree (`u16 left`, `u16 right` in
    4-byte units, `u32 sector`, `u32 size`, `u8 attributes`, `u8 name length`, name) that
    is walked with a visited set. The root executable decides: `default.xbe` at offset 0
    is Xbox, `default.xex` at 0, 0xFDA0000 or 0x2080000 is Xbox 360, `default.xbe` at
    0x18300000 is a full Xbox image that is not placed. Any other combination is not
    recognised.
  - *Wii U* (`.wua`, a Cemu ZArchive): the last 16 bytes of the file are a big-endian u64
    total size, the u32 version `0x61bf3a01` and the u32 magic `0x169f52d6` (the end of a
    144-byte footer), and the size must equal the file's own. A `.wua` name is only ever
    judged this way and no other name is judged as a ZArchive.
  - *PS3*: an ISO9660 primary volume descriptor at sector 16; the root must hold
    `PS3_DISC.SFB` and a `PS3_GAME` directory with `PARAM.SFO`, whose `TITLE_ID` and `TITLE`
    are read.
- **Claiming.** The handler claims an item (a file, a folder, or an archive listed by
  `zipfile` or `bsdtar -tf`) when a file name ends in `.dec.iso`, `.xiso.iso`, `.xiso` or `.wua`.
  Names alone never identify: a claimed file whose content is not recognised goes to
  review. Every `.iso` or `.xiso` in a claimed item is sniffed; other non-sidecar files are
  named in the review reason.
- **Plain `.iso`.** A plain `.iso` must keep going through the DATs, so it is not claimed.
  After `identify()` the files left unmatched are offered to `unmatched_handlers()`; this
  handler sniffs the unmatched `.iso` files and places the ones that are PS3, Xbox or
  Xbox 360 (or sends a full Xbox image to review) instead of reporting "no DAT match". A
  PS2, PSP or GameCube image that a DAT matches is therefore never read for a signature,
  and one that no DAT matches and no signature fits ends in review as before. The cost is
  that an unmatched plain `.iso` is hashed once, like any other unmatched file.
- **Placement.** `library/roms/ps3/`, `roms/xbox/`, `roms/xbox360/roms/` (the folder
  EmuDeck's ES-DE system for the 360 reads) and `roms/wiiu/roms/` (where Cemu's game path
  points), under the file's own name. It is copied to
  `<name>.part`, read back and renamed; an existing name with the same size and SHA1 is
  "already in the library", a different one a review (nothing replaced). The report shows
  `detected: <platform>` (for PS3 also the TITLE_ID and title), the path and the whitelist
  line (`!/roms/xbox360/roms/<name>`).
- **Full Xbox images.** A game partition at 0x18300000 is flagged, never placed: xemu
  cannot boot it. The review reason says to convert it to XISO with extract-xiso.
- **PS3 emulator.** A PS3 `.iso` needs ES-DE's alternative emulator `RPCS3 ISO
  (Standalone)`; the system default is a shortcut launcher. `[inbox] gamelist_ps3`
  (default `<root>/frontend/es-de/gamelists/ps3/gamelist.xml`) gets
  `<altemulator>RPCS3 ISO (Standalone)</altemulator>` for the game, written with the same
  helpers as the arcade gamelist (`inbox_mame.update_gamelist_entry`, atomic write,
  backup under `tools/logs/gamelist-backups/` before the run's first change). Report mode prints "would add". It
  is also done for a game that was already in the library. If the gamelist cannot be
  updated the item goes to review and a rerun retries it.

### 3DS images

A `.3ds` or `.cci` file has no DAT here, so it is identified by its headers. The code is in
`warpzone/inbox_n3ds.py`, a kind handler in `inbox.kind_handlers()` (it is not an
`UnmatchedHandler`).

- **Claiming.** The handler claims an item (a file, a folder, or an archive listed with
  `inbox_console_iso.names_in`) when a file name ends in `.3ds` or `.cci`, case-insensitively.
  `.3dsx` and `.cia` are not claimed. Names alone never identify: a claimed file whose headers are
  not valid goes to review. Other non-sidecar files of a claimed item are named in the review
  reason.
- **Verdict.** `judge(head, ncch, file_size)` is pure and reads nothing; `detect` reads the first
  0x200 bytes and the 0x200 bytes at the offset of partition 0, never more.
  - *NCSD*: the magic `NCSD` at 0x100; the image size (u32 little-endian at 0x104, in media units
    of 0x200 bytes) must be non-zero and not bigger than the file (trailing padding is
    allowed, a truncated dump is not); partition 0 (the offset u32 at 0x120, in media units) must
    exist and leave room for an NCCH header inside the image.
  - *NCCH*: the magic `NCCH` at +0x100 of partition 0; the flags byte at +0x18F must have the
    NoCrypto bit (0x04). Without it the status is `encrypted`.
  - Statuses are `ok`, `encrypted` and `invalid`. Both of the last two go to review with a
    reason; an encrypted image says that Azahar opens only decrypted images.
- **Placement.** `library/roms/n3ds/<stem>.cci`, for a `.3ds` as well as for a `.cci`: the
  file is renamed on placement and its bytes are not changed. It uses the same copy as the console
  images (`inbox_console_iso.place_file`: `<name>.part`, `inbox.copy_verified`, rename, a re-check
  right before the rename), so nothing is replaced: an existing name with the same size and SHA1
  is "already in the library" and a different one a review (`inbox.destination_verdict`). In report
  mode an existing name is "already exists" and nothing is written, not even the folder. The
  report shows `detected: Nintendo 3DS image (decrypted)`, the path and the whitelist line
  (`!/roms/n3ds/<name>.cci`).
- **One target name.** Two files of one item that would be filed under the same name (`X.3ds`
  and `X.cci`) are not placed together: the second goes to review.

### PS4 packages

A PS4 `.pkg` is recognised by `inbox_console_iso` (magic `\x7fCNT`, content id at 0x40,
title id `CUSAnnnnn`) and handled by `warpzone/inbox_ps4.py`. Current shadPS4 has no package
installer: it runs extracted game folders found in its install directories, and ES-DE
lists only the `.desktop` files of `roms/ps4/shortcuts`, whose command on the consoles is
`%EMULATOR_SHADPS4% %ROM%` (EmuDeck's `shadps4.sh`).

- **Tool.** `[inbox] orbis_pkg_util` (PATH otherwise): `orbis-pkg-util extract -q -o <dir>
  <pkg>`. Reported in the `tools:` line and by `doctor` as an optional inbox tool. Without it
  the `.pkg` is placed as it is in `roms/ps4/` with a note saying so (the earlier behaviour).
- **Kind.** The header says what the package is before anything is unpacked: the u32
  big-endian content type at 0x74 (`0x1A` game data, `0x1B` additional content) and the
  content flags at 0x78 (`0x100000` or `0x40000000` mark a patch). An update or DLC goes to
  review with a message saying that updates are not merged (nothing is extracted, even when
  the base game is in the library). After extraction the `CATEGORY` of `sce_sys/param.sfo`
  decides again (`gd*` game, `gp` update, `ac` DLC) and its `TITLE_ID` must equal the header's.
- **Game folder.** The extraction goes to a temporary folder in the local work folder
  (`work_dir`), which must have the package's size free. It must hold `eboot.bin` and
  `sce_sys/param.sfo` and no symbolic link. The folder is then copied file by file with a
  read-back (`inbox.copy_verified`) to `library/roms/ps4/games/<CUSAxxxxx>.part/` and
  renamed, so the destination never holds a partial game and a stale `.part` of an earlier
  run is replaced. A game folder already in the library is reported as "already in the
  library", not compared and never touched; the package is not unpacked.
- **Shortcut.** `library/roms/ps4/shortcuts/<Title>.desktop`, written the same way (`.part`,
  rename), never over another file. The name comes from the `TITLE` of `param.sfo` (trademark
  signs and quotes dropped, `:` as ` -`, only letters, digits, marks and `-_.,()[]!+#`
  kept, 100 characters at most). A file of that name that already runs this game is kept;
  one that runs another game gets ` [CUSAxxxxx]` in its name. A game that was already in
  the library gets a missing shortcut from the `TITLE` of its own `param.sfo`.
  ```
  [Desktop Entry]
  Type=Application
  Name=<Title>
  Exec=shadps4 "/home/deck/Emulation/roms/ps4/games/<CUSAxxxxx>/eboot.bin"
  ```
  EmuDeck's launcher greps the `Exec=` line of the shortcut, takes the one quoted string
  as the game and runs `Shadps4-qt.AppImage -d -g <path>`. In the Qt launcher `-d` is the
  alias of `-e default`: the version selected in its settings. Nothing expands `~` or
  `$HOME` on that path, so it is written out under the SteamOS home (`DEVICE_LIBRARY`).
- **Whitelists.** Two lines per game: `!/roms/ps4/games/<CUSAxxxxx>` and
  `!/roms/ps4/shortcuts/<Title>.desktop`. A report without `--apply` does not unpack
  anything, so it cannot name the shortcut and prints only the first.
- **Install directory.** The consoles must list `~/Emulation/roms/ps4/games` as an install
  directory of shadPS4. See [shadPS4 install directories](#shadps4-install-directories).

### PS Vita games

A decrypted Vita dump is recognised and filed by `warpzone/inbox_vita.py`, a kind handler
registered after the console image handler (so a `.pkg` or anything that handler claims keeps
its outcome). Vita3K runs games from `ux0/app/<TITLEID>`; ES-DE lists only `.psvita` files,
whose system definition launches `%EMULATOR_VITA3K% -r %INJECT%=%BASENAME%.psvita`, and
EmuDeck's `vita3k.sh` reads the file's content as the title id (`Vita3K -Fr <id>`).

- **Claim.** A folder or archive (`.zip`, or `.7z` and `.rar` with `bsdtar`) whose file list
  has `sce_sys/param.sfo` and `eboot.bin` in the same folder, either at the top or one folder
  below it (`game_roots`, pure). Other layouts (`PS3_GAME/PARAM.SFO`, `PSP_GAME/PARAM.SFO`) are
  not claimed. A folder holding archives is not opened.
- **Identify.** `parse_sfo` (shared with PS3 and PS4) on `sce_sys/param.sfo`: `TITLE_ID` must
  fully match `PCS[A-I]\d{5}`, and `CATEGORY` decides (`gd`, `gdc` game; `gp` update; `ac`
  DLC; anything else is not a game). Anything but a game, a title id that is not a Vita one,
  several games in one item, a symbolic link inside the game, or a dump that holds
  `sce_pfs` (Vita3K has to install it) goes to review with the reason. `.vpk` files and `.pkg` files keep their earlier outcome.
- **Vitamin.** A dump whose game folder holds `sce_module/steroid.suprx` (`is_vitamin`, pure,
  case-insensitive, relative to the game root) is a Vitamin dump. Vita3K rejects that module,
  so it ALWAYS goes to review, checked before anything else about the dump (also in report
  mode), with a reason that says so and suggests a clean dump (a `.pkg` with its `work.bin`, or
  NoNpDrm). It is never placed even if the game might start, and nothing already in the
  library is touched: no game folder, no launcher.
- **Game folder.** The dump (the top folder of an archive is dropped) is copied file by file
  with a read-back (`inbox.copy_verified`; hidden and junk names are skipped) to
  `library/roms/psvita/app/<TITLEID>.part/` and renamed, as for PS4. A folder already in the
  library is reported as "already in the library", not compared and never touched.
- **Launcher.** `library/roms/psvita/<Title>.psvita`, named like the PS4 shortcut
  (`shortcut_name`), holding only the title id with no newline, written through `.part` and a
  rename and never over another file. One that holds this id is kept; one that holds another
  id gets ` [<TITLEID>]` in the new name. A game already in the library gets a missing
  launcher from the `TITLE` of its own `param.sfo`.
- **Licence.** Vita3K runs the games of `ux0/app/<TITLEID>` whose `eboot.bin` is still an
  encrypted SELF only with the licence `ux0/license/<TITLEID>/<CONTENT_ID>.rif`
  (`get_license`, `packages/src/license.cpp`, loads that path by the content id of the game's
  `param.sfo`; a missing or unreadable file leaves the key all zeros). Inbox files the licence
  in `library/roms/psvita/license/<TITLEID>/<CONTENT_ID>.rif`:
  - *Sources* (`license_sources`, pure): `sce_sys/package/work.bin` of the game (NoNpDrm,
    the same 512-byte structure as a `.rif`), or a `*.rif` directly in the game folder or
    beside it (the item's top), or in `license/<TITLEID>/` at either place.
  - *Check* (`parse_license`, `choose_license`, pure): exactly 512 bytes; the content id is
    the NUL-terminated text of the 0x30 bytes at offset 0x10 (`SceNpDrmLicense.content_id`)
    and must be `XXnnnn-PCS[A-I]nnnnn_nn-` plus 16 characters of `[A-Z0-9]`, so it is safe as
    a file name; its title id (characters 7 to 15, as Vita3K's `copy_license` takes it) must be
    the game's; and, when `param.sfo` has a `CONTENT_ID`, it must be that one, because the
    file name is what Vita3K looks up. Several candidates are fine only when they are the same
    bytes; different licences, a symbolic link or a file that is not a licence go to review.
    The file is filed under the content id it holds, not the name it came with.
  - *Placement:* a `.part` write read back and renamed, never over a file. The library copy is
    compared before anything is placed: the same bytes are "already there", other bytes make
    the item go to review without placing the game. A game already in the library gets a
    missing licence like it gets a missing launcher. A licence that comes along when none is
    needed is filed too.
  - *Does the game need one?* (`eboot_needs_license`, pure, on the first 64 KiB of
    `eboot.bin`.) Read from Vita3K (`vita3k/packages/src/sce_utils.cpp`): `decrypt_fself`,
    called for every module including the eboot (`modules/module_parent.cpp`, with
    `license.rif[title_id].key`), returns the buffer untouched when `is_fself_encrypted` is
    false, which is `seg_infos->encryption != 2` for the first segment info found at
    `section_info_offset` (offset 0x58 of the header, `encryption` is the u64 at +0x18), and
    fails with `LOG_ERROR("No klic provided for encrypted App")` when the self is encrypted,
    `AppInfoHeader.self_type == SelfType::APP` (0x08, a u32 at +12 of the app info at offset
    0x38) and the klic is all zeros (`Invalid SELF: file is either not a SELF or is still
    encrypted` is the answer to a file without the `SCE\0` magic). The magic alone is not the
    test: Vita3K's own "decrypt selfs" writes a SELF with the same magic and `encryption = 2`
    (`decrypt_fself`'s output header). So a licence is needed exactly when the file starts with
    `SCE\0`, is encrypted and is an APP. A file with another magic needs none. A SELF header
    that is shorter than 0x80 bytes or points outside the bytes read cannot be judged and goes
    to review. A game that needs a licence and has none goes to review, and nothing is placed.
- **Whitelists.** Up to three lines per game: `!/roms/psvita/app/<TITLEID>`,
  `!/roms/psvita/license/<TITLEID>` (only when there is a licence) and
  `!/roms/psvita/<Title>.psvita`. Nothing is unpacked by a tool, so the report mode prints
  them all.
- **On the console.** The folders must be linked into Vita3K's own storage, see
  [Title links](#title-links).

### Arcade sets

Arcade zips are not unpacked or converted: a MAME set is a zip whose name and members
the emulator needs exactly as they are, so `inbox` only identifies, names and places it.
The code is in `warpzone/inbox_mame.py`, which plugs into `inbox.py` through a small
registry (`inbox.kind_handlers()`; a handler has `validate`, `claims` and `process`, and
`_identify_and_place` offers every item to the handlers before the usual path).

- **Which items.** A top-level `.zip`, or a folder with nothing but `.zip` files (and
  sidecars), is taken as arcade when at least one member (size and CRC32, read from the
  zip directory) is in a configured database. Anything else, and every item when no
  database is configured, takes the usual path.
- **Databases.** `[inbox] mame_dat` (MAME's `-listxml`, or the official `mameXXXXlx.zip`,
  whose first `.xml` or `.dat` member is read), `fbneo_dat` (the FinalBurn Neo
  "ClrMame Pro XML, Arcade only" DAT) and `mame2003plus_dat` (`mame2003-plus.xml`).
  All three are XML with `machine` or `game` elements and are parsed the same way with a
  streaming `iterparse` (a listxml is 300 MB): a set is its `name`, `cloneof`, `romof`,
  `isbios` / `isdevice` (a not-runnable set is a BIOS, which is how 2003-Plus marks
  them), its `rom` elements that have a CRC32 and are neither `nodump` nor `optional`
  (with `merge` and `bios`), its `device_ref`s and its `disk` names. Sets with nothing to
  check are dropped and each set's devices are flattened to the ones that have ROMs. The
  index is cached as JSON in the DAT cache folder (`sets-*.json`), keyed by the file's
  size and modification time. A configured path that is not a file is a configuration
  error (exit 2), checked before anything runs.
- **Fit.** A set fits a database when every required ROM is present, where a ROM is the
  pair (size, CRC32) and present means in the dropped zip or in a zip of
  `library/roms/mame/` named after a set the set depends on: its parent (`cloneof`), its
  BIOS (`romof`), its devices and theirs. The set's own name is never read from the
  library (the dropped zip stands in for it). A set that lists several alternative
  `bios` ROMs needs one complete alternative. Members may be anywhere in the zip: recent
  merged sets keep clones in sub-folders. The zip is read back member by member before it
  is trusted (`testzip`).
- **Which set.** For each database: the set named like the file when it shares at least
  one member with the zip (judged as that set, fit or not, so a file called `galpanic.zip`
  never silently becomes a different clone); otherwise the sets that fit, taking the top
  parent among them (a merged zip fits its parent and each of its clones), then the one
  that explains most members, games before BIOS and device sets; otherwise the set
  lacking the fewest files.
- **Decision.** The databases are tried in the order FinalBurn Neo, MAME, MAME
  2003-Plus (the ordered tuple `SOURCES`), only the configured ones. The first whose set
  fits decides: MAME means the ES-DE default (no `<altemulator>`; an override left in the
  gamelist is removed), the others their ES-DE name, `FinalBurn Neo` and `MAME 2003-Plus`.
  FinalBurn Neo comes before MAME because of RetroAchievements: EmuDeck turns them off
  for the "MAME - Current" core (`cheevos_enable = "false"` in its `mame.cfg`) and the RA
  arcade sets target FinalBurn Neo, so a CPS1 game such as dino or ddsom only earns
  achievements there. A set FinalBurn Neo does not have simply falls through to MAME. When none fits the zip goes to review, and the reason has one line per
  database with the missing files (name and CRC32) grouped by the zip they belong to:
  `needs neogeo.zip`, `needs qsound_hle.zip or qsound.zip`, `<zip> in the library lacks`,
  `<set>.zip lacks`. If only some databases are configured, the others are simply not
  considered. BIOS and device sets (`neogeo`, `qsound_hle`) are filed with no emulator
  entry. The notes of a placed set say why earlier emulators did not fit.
- **Atomiswave and Naomi (Flycast).** These Sega boards hang in MAME but run in Flycast
  (checked on hardware). A game is one of them when the `romof` chain in MAME's database
  ends in `awbios` (Atomiswave) or `naomi`, `naomi2`, `naomigd` (the Naomi family): a clone's
  `romof` is its parent, whose `romof` is the BIOS, so the chain is followed up to a
  small depth. If MAME's set is complete (same fit rule as above) the decision is
  `<altemulator>Flycast</altemulator>` (the ES-DE label of the RetroArch core) whatever the
  order above; an incomplete set is reviewed as usual, never sent to Flycast. Flycast
  reads its BIOS from `library/bios/dc/` (`awbios.zip`, `naomi.zip`, same names as
  MAME), not from `roms/mame/`. When the matching zip is not in `bios/dc/` the game is
  still placed and a note tells the user to copy it from `roms/mame/`; warpzone never
  copies BIOS files.
- **Name and placement.** The result is `library/roms/mame/<set>.zip`, the set name of the
  database that decided, whatever the file was called; the bytes are the original's. It is
  copied to `<name>.part`, read back and compared member by member, then renamed. If the
  name exists, equal members mean "already in the library" and different ones a review
  (conflict, nothing replaced). `roms/arcade/` is never written. The whitelist line is
  `!/roms/mame/<set>.zip`. Originals go to `_done` or `_revisar` as for other systems.
- **ES-DE gamelist.** `[inbox] gamelist_mame` (default
  `<root>/frontend/es-de/gamelists/mame/gamelist.xml`) holds per-game overrides:
  `<game><path>./dino.zip</path><altemulator>FinalBurn Neo</altemulator></game>`. For
  every game set or confirmed by a run, including one that was already in the library, the
  entry of `<set>.zip` is brought in line: added when missing, its `<altemulator>` changed
  when different, and removed when the default now fits (an entry that held nothing else
  is removed whole). The file is edited as text so nothing else changes: ES-DE writes
  several top-level elements, so it is not parsed and rewritten; entries are found by
  their `<path>`, XML comments are skipped, indentation and the newline style are copied,
  and the result is checked to be well-formed (when the original was) before it is
  written. It is written to `gamelist.xml.part` and renamed. Before the run's first
  change to a gamelist, the file is copied to `<root>/tools/logs/gamelist-backups/
  <system>-gamelist-<YYYYMMDD-HHMMSS>.xml` (`<system>` is `mame` or `ps3`). The backup is
  not written next to the gamelist because that folder is synced both ways and the copy
  would reach every console; `tools/logs` is excluded by the ignores of `wz-tools` (a
  fixed `/logs` line), which `provision hub` applies to an existing folder too. A missing file is created only when
  an override is needed. Report mode prints each change as "would ...". If the gamelist
  cannot be updated after the zip was placed, the item goes to review and a rerun
  ("already in the library") retries it.
- **ES-DE keeps its gamelist in memory.** On a console ES-DE reads the gamelist once and
  rewrites it when a game is launched, so an edit made on the hub while ES-DE is open on
  a console is overwritten when that console next saves. warpzone cannot see the
  consoles, but a console that just played writes the file back through Syncthing, so
  with `--apply` and before the first edit of a gamelist it compares the file's
  modification time with the clock (`recently_written`, window 10 minutes). Within the
  window it prints a warning, once per gamelist per run, saying to quit ES-DE on every
  console and run `inbox` again if the change does not stick. It never refuses: the edit
  is made either way. The same applies to the PS3 gamelist. A run's own write counts as a
  modification, so a second `--apply` within 10 minutes warns about it too.
- **Not covered.** CHD disks of a set are not in the zip: they are listed in a note, not
  checked. `.7z` and `.rar` are not opened for arcade. Samples are not checked. warpzone
  never downloads the databases.

## Emulator settings

Every emulator keeps its settings in its own files on each console. The user changes a
setting in the emulator's own interface, on whichever console is at hand;
`warpzone settings capture` takes it from there to the hub, and every console applies it
with `warpzone settings apply`, which ES-DE runs at startup. The hub holds **key-level
layers**, never whole config files: those also hold controller mappings, GPU ids, window
geometry, absolute paths and logins, which differ from one console to the next.

### Layers on the hub

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
  may know it). A secret key in a layer (see [Safety rules](#safety-rules-1)) is never
  applied and is reported by name only.
- **Written whole.** `capture` writes a layer file sorted by emulator, file and key, under a
  header line saying warpzone writes it. Comments added by hand on the hub are not kept.

### Profile entries

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

### Format handlers

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

### Device state

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

### Apply

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

### ES-DE startup script

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
- The same mechanism installs `warpzone-links.sh` (for a device that lists `library` and whose profile has `[links.*]`), which runs `links sync` and so links the games that arrived since the last start before ES-DE lists them (see [Title links](#title-links)).
- Apply runs once per ES-DE start, possibly before Syncthing has pulled the newest layers.
  What arrives later applies at the next start, or at once with `warpzone settings apply`.

### Capture

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

### Audit

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

### System data

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

### Safety rules

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

## Doctor

`warpzone doctor [--config PATH]` checks the machine it runs on and prints a checklist,
in the spirit of `flutter doctor`: one line per check with a mark, a short detail and,
for every problem, the exact fix for this platform.

- **Offline and read-only.** It never installs, writes or deletes anything. The only
  network-like probe is `GET /rest/system/ping` on the local Syncthing, and only when its
  GUI address is a loopback one (a wildcard address is probed as `127.0.0.1`), with a
  2-second timeout; the key comes from the local `config.xml` and is sent as a header,
  never printed. `RA_API_KEY` is reported as "set" or "not set", nothing else.
- **Statuses.** `[ok]`; `[! ]` warning (works, worth a look: DATs older than 180 days,
  less than 20 GiB free on the hub, a tool that is on `PATH` but that `inbox` will not
  use); `[!!]` problem; `[--]` optional and not set up. Only `[!!]` counts for the exit code.
- **What is required.** Nothing is required of a feature the machine does not use. A
  missing config file or a missing `[hub]`, `[devices]` or `[inbox]` table is `[--]`, and
  the line says what the table would enable. The exception is a config with only an
  `[inbox]` table (a machine that just files games): `[hub]` and `[devices]` are then one
  `[ok]` note, "not used here", instead of two `[--]` items. A config with no `[inbox]`
  table (or no config at all) collapses the optional inbox-only items (the tools, the
  Switch `titledb_dir` and `switch_keys`, the DAT folder and arcade DATs, the hub
  folder and `RA_API_KEY`) into one `[ok]` line saying inbox is not set up on this
  machine (`collapse_inbox`, pure); items that are `[ok]`, `[! ]` or `[!!]` stay. Once an `[inbox]` table exists, `chdman`,
  `bsdtar`, `dats_dir`, the hub root with `inbox/` and `library/roms/` become required.
  `RAHasher`, `dolphin-tool` and `nsz` are always optional, and so are the Switch `titledb_dir` and `switch_keys` (a configured path that does not exist is a problem; the keys file is only checked to exist, never read). The three arcade DAT paths
  (`mame_dat`, `fbneo_dat`, `mame2003plus_dat`) are `[--]` when unset, `[!!]` when set to
  something that is not a file (`inbox` refuses to start) and `[ok]` otherwise. A config that does not load,
  a configured tool path that is not executable, a tool that cannot run, a local
  Syncthing whose API does not answer, and Python older than 3.11 are always problems.
- **Game Mode (SteamOS).** With a local Syncthing on SteamOS, `Game Mode` says whether
  Syncthing runs without a desktop session. Game Mode starts no desktop session, so
  `~/.config/autostart/*.desktop` entries never run there; only a systemd user unit
  does. `judge_game_mode` is pure over the `.service` files of `~/.config/systemd/user`
  (an `ExecStart` that runs syncthing), the names linked in its `default.target.wants`
  (enabled) and the autostart entries (an `Exec` that runs syncthing). An enabled unit
  is `[ok]`; with an autostart entry as well it is `[! ]` (two instances compete for
  the port); an autostart entry alone, a unit that is not enabled, or nothing at all
  is `[! ]` with a fix that shows a minimal unit for the SyncThingy flatpak
  (`ExecStart=/usr/bin/flatpak run --command=syncthing com.github.zocker_160.SyncThingy serve --no-browser --no-restart`),
  `systemctl --user enable --now syncthing.service` and, when there is one, the autostart
  entry to remove. Fix lines that start with a space are code and are never wrapped.
  The unit has `ExecStop=/usr/bin/flatpak kill com.github.zocker_160.SyncThingy`: `flatpak
  run` puts the process in a scope of its own, so `systemctl --user stop` does not reach
  it. `Syncthing unit` (`judge_unit_state`, pure over `systemctl --user show -p
  ActiveState,SubState syncthing`) is `[ok]` when the unit is active, says nothing when it
  is stopped or absent, and is `[! ]` when it is `activating` or in `auto-restart` (a start
  that keeps failing, normally the old process holding the port), with the fix: `flatpak
  kill`, the unit with `ExecStop` unless the unit already has one, and a restart.
  The flatpak app id and its commands live in `warpzone/flatpak.py`.
- **Flatpak access.** With the SyncThingy flatpak as the local Syncthing (its `config.xml`
  is in the flatpak's own folder), `Flatpak access` reads `flatpak override --user --show`
  and compares it with the folders under `~/.var/app/<app>` that the configured devices'
  profiles use (every shipped profile when the config does not load), for the apps that are
  installed. Missing overrides are `[!!]`, because those save folders sync nothing; the fix
  is one `flatpak override` per app plus a restart. `granted_apps` and `missing_apps` in
  `warpzone/flatpak.py` are pure. A native Syncthing is not checked.
- **Flycast VMUs.** When RetroArch's `Flycast.opt` exists, `judge_flycast_vmus` (pure, over
  the file text) reads `reicast_per_content_vmus`: `VMU A1` and `All VMUs` are `[ok]`;
  `disabled` or a missing key is `[! ]`, because the core then writes the shared VMUs into
  `bios/dc`, which the profile's library ignores (`/bios/dc/vmu_save_*.bin`,
  `/bios/dc/dc_nvmem.bin`) keep out of sync. The fix names the core option to change.
- **Vita3K user.** When `~/.config/Vita3K/config.yml` has a `user-id:`, `judge_vita3k_user`
  (pure) is `[ok]` for `00` and `[! ]` for anything else: the profile syncs
  `ux0/user/00/savedata` only. A missing file or key says nothing.
- **Emulator settings.** On a device whose config subscribes it to `settings`. Doctor is
  offline, so it finds the device in the local Syncthing `config.xml`: the configured
  device whose ID is listed there and whose `paths.settings` is the path of the
  `wz-settings` folder (the hub lists the same IDs but keeps that folder under its own root,
  so it gets no such lines); with no local Syncthing, or no such device, the group is
  silent. The lines are `Settings layers` (the local `wz-settings` folder and its marker exist, `[!!]` if not; `common.toml` and
  the device's layer parse and validate; a conflict copy or a secret key in a layer is
  `[!!]`, an unknown emulator or file id `[! ]`); `ES-DE startup script` (present,
  executable, and its interpreter exists, `[! ]` otherwise with `warpzone provision device
  <name>` as the fix; `CustomEventScripts` not `true` in `es_settings.xml` is `[! ]` with
  `warpzone provision device <name>` run with ES-DE closed, or the menu path, as the fix); `Last settings apply` (from `last-apply.json`, whose `time`,
  `exit`, `written` and `held` fields it reads: a failed run is `[! ]` with the log path,
  held keys are `[! ]` with `warpzone settings capture` as the fix, no run yet is `[--]`).
  The script and the `CustomEventScripts` judges share the one `ES-DE startup script` line,
  which shows the text of whichever is not `[ok]`; a missing `es_settings.xml` is `[! ]` too
  (start ES-DE once). `judge_settings_layers`, `judge_esde_script`,
  `judge_event_scripts` and `judge_last_apply` are pure.
- **Frontend.** On a device whose config subscribes it to `library` and whose profile has
  `[es_systems]` or `[retroarch]`. The device is found as for `settings`, through the
  `wz-library` folder in the local Syncthing `config.xml` (its path is `paths.library`);
  any other machine, and a profile without those tables, gets no line. `ES systems
  override` is `[ok]` when the override is exactly `rewrite_system_paths` of the source;
  missing or out of date (the source changed, or someone edited it) is `[! ]`, and so is a
  missing source. `RetroArch paths` reads each file of `cfg` and is `[ok]` when every
  wanted key has its value; a key that differs is named with its current value, a missing
  file or a key present twice is `[! ]`. The fix is `warpzone provision device <name>` with
  the frontend or RetroArch closed. `judge_systems_override` and `judge_retroarch_paths`
  are pure. A device whose profile has `[artwork]` and whose config lists `frontend` gets two
  more lines. `Frontend artwork` plans the gamelists exactly as `frontend refresh` would
  (`artwork.plan_gamelists`, with the reads going through the probes) and is `[ok]` when every
  gamelist is current, `[! ]` when some are missing or stale (named, with
  `warpzone frontend refresh <name>` as the fix, frontend closed), when no scraped gamelist has
  arrived yet for the systems on the device (the sync is the thing to look at), or when the
  system list or a scraped file is unreadable. `Artwork timer` (only when the profile sets
  `refresh`) is `[ok]` when `warpzone-artwork.timer` is installed and `[! ]` otherwise.
  `judge_artwork` is pure.
- **Exit codes.** `0` when no check is a problem, `1` otherwise.
- **Install hints** live in one table in `warpzone/doctor.py` (`TOOLS` and the `*_HINTS`
  dicts), keyed by platform family: `macos`, `debian`, `fedora`, `arch`, `steamos`,
  `linux` (any other Linux) and `other`, with an `any` fallback. A hint is added only
  when it is known to be right for that family. SteamOS has a read-only root, so its hints
  point at a container instead of a system package.
- **Structure.** Each check is a function of a `Context` that returns `Result` records
  (name, group, status, detail, fix). Everything it reads from the machine (`which`,
  running a tool, the environment, the home folder, the clock, free space, the HTTP probe)
  is a field of `Probes`, so the tests use fakes and no real tools. `render` and
  `exit_code` are pure. `render` wraps details and fixes to the terminal width
  (`shutil.get_terminal_size`, 100 when it is unknown, never narrower than 30 columns
  of text): continuation lines hang under the start of the detail, a fix under its text,
  and a word longer than the line (a path, a URL) is never cut. The config is read as far as it can be: the `[inbox]` values are
  used by the other checks even when another table is invalid.

## Self-update

Devices run warpzone from a git clone with an editable install, so updating is moving the
clone to a release tag. `selfupdate.py` holds the decisions as pure functions (`parse_tag`,
`newest_tag`, `version_key`, `tag_key`, `decide`) and a runner-based `run_self_update`, so the tests use
a real repository on disk (a bare repository as the remote) and no network.

- **Checkout.** The root is the folder that holds the `warpzone` package. It must be the
  top level of a git work tree (`git rev-parse --show-toplevel`); otherwise (a wheel, a
  package folder inside another repository, no git) the command says so and exits 0.
- **Which tag.** `git fetch --tags`, then the highest tag matching `vX.Y.Z` exactly,
  compared as numbers. Pre-release tags are ignored (until the channels of "Autonomous
  updates" are implemented). A pre-release running version (`0.3.0rc1`) sorts before its
  release, and `rc1` before `rc2`. A fetch that fails or times out (300 s) is "could
  not reach the remote": a message and exit 0.
- **Never a downgrade.** If the running `__version__` is the same as or newer than the
  newest tag, nothing happens.
- **Dirty tree.** Only when an update is due: local changes to tracked files
  (`git status --porcelain --untracked-files=no`) make it refuse with exit 1.
- **Update.** `git checkout --detach refs/tags/<tag>`. The editable install follows the
  clone, so `warpzone --version` (which prints `__version__`) shows the new version in the
  next process. `python -m pip install -e <root>` with the running interpreter is run only
  when `pyproject.toml` differs between the old and the new commit (entry points and the
  package list are the only install metadata that can go stale); a failure prints that
  command and exits 1. The command ends by asking the interpreter which version it imports.
- **Log.** Each message, with a UTC timestamp, is appended to
  `~/.local/state/warpzone/self-update.log` (`$XDG_STATE_HOME` when set), cut to its last 200
  lines past 64 KiB. `--dry-run` writes none.

### Timer

`provision device` calls `ensure_update_timer` for every device, whatever its profile and
folders. `updatetimer.py` renders the two units (pure): `warpzone-self-update.service`
(`Type=oneshot`, `ExecStart=<python> -m warpzone self-update`, the interpreter that ran
`provision device`, quoted for systemd) and `warpzone-self-update.timer` (`OnCalendar=daily`,
`Persistent=true`, `OnBootSec=5min`, `RandomizedDelaySec=30min`, `WantedBy=timers.target`)
in `~/.config/systemd/user/`. The units are rewritten only when their content differs
(mode 0644, atomically); `daemon-reload`, `enable --now` and, when the timer was already
running with other units, `restart` follow only when something changed or the timer is not
enabled and active. It installs nothing, with a warning, when the interpreter cannot import
warpzone from outside the current folder or `systemctl --user show-environment` fails (no
user manager); `--dry-run` runs only the read-only `systemctl` queries and prints what it
would do. `self_update = false` under `[devices.<name>]` skips it (and `doctor` stays
silent); an installed timer is never removed by `provision`.

`doctor` has `Self-update timer` (group `Updates`): on a Linux machine that is a configured
device subscribed to `library` without `self_update = false` (found through the local
`wz-library` folder, as for the frontend), `judge_update_timer` is `[ok]` when both units
exist and the timer is enabled and active, and `[! ]` when the units are missing, the timer
is not enabled or not active, or systemd cannot be asked.

### Autonomous updates (contract)

This section is the contract for making a device keep itself on a working release with no
person logged in. Each piece says whether it exists. **Implemented** means it is in the code
today; **Not implemented** means the code does not do it yet and this text is what it must
do. Until then `warpzone self-update` behaves as described above.

| Piece | State |
| --- | --- |
| `channel` and `pin` in the device config, validated | Implemented |
| Ordering of release candidates (`tag_key`, `version_key`) | Implemented |
| `provision device --unattended` and its exit code `3` | Implemented |
| `warpzone health` | Implemented |
| `warpzone/data/updater.sh tick`: lock, `enabled`, deferral while the menu is open (72 hours at most), fetch once a day, the target (the highest `vX.Y.Z` tag that is an ancestor of `origin/main` and whose `__version__` matches), the three-step verification with a time limit, the differential verdict and its back-off, `bad/<tag>`, the state file, the attempts loop, probation, the kind of checkout, the stale `index.lock`, `menu` and `selftest` | Implemented (`tests/test_updater_sh.py`) |
| Everything else in this section (installing the script, the settings file written by `provision`, the Python repair, channels, `RETIRED`, `pin`, signed tags, the timer units, `self-update` as a wrapper, the `doctor` checks, `provision hub --additive`) | Not implemented |

**Tag spelling.** A release is `vX.Y.Z` and a release candidate is `vX.Y.ZrcN`: the tag is
`v` plus `__version__`, in the PEP 440 spelling that [releasing.md](releasing.md) requires
(`v0.3.0-rc1` is not a tag warpzone knows). They order as `v0.3.0rc1` < `v0.3.0rc2` <
`v0.3.0`, and every number is compared as a number (`rc10` is after `rc2`).
`selfupdate.tag_key` returns that key, or None for any other spelling; `version_key` gives
the same key for a package version, so a tag and the version it carries compare equal.
`parse_tag`, `newest_tag` and `decide` still look at plain releases only.

**One tick.** A single timer starts a single script. The unit keeps the name
`warpzone-self-update`, so rewriting it leaves no orphan unit. The timer has `OnBootSec=45s`
and `OnUnitActiveSec=15min` and no random delay (a delay also pushes back the start after
boot, when the menu may not be open yet); the service is a one-shot with
`TimeoutStartSec=30min` and `ExecStart=/bin/sh <updater> tick` (the units are not written
yet; `updater.sh tick` exists and nothing starts it). The timer is a piece of its
own, separate from `updater.sh tick`: the script does not know what started it, so a platform
without systemd can start the same `tick` from another launcher. Each tick, in this order:

1. Take a lock; a tick that finds it held exits at once.
2. Read the configuration; if the device is not `enabled` (`self_update = false`), stop.
3. If the menu is open, defer (see below).
4. Repair the Python environment (see below). Today the script only looks for an
   interpreter that can run it (`no-python` when none).
5. Fetch the remote at most once a day. The moment is offset by a number derived from the
   device name (a delay of up to a twenty-fourth of the interval, `cksum` of the name modulo
   that), so devices do not all arrive together. A fetch that fails is logged and tried again
   at the next tick; the tags already fetched are still used.
6. Pick the target tag. When there is one, switch to it and verify it. When there is none,
   reconcile (`provision device --unattended`) only if `/proc/sys/kernel/random/boot_id`
   differs from the last tick's, or `pending` is set. While the device waits out a back-off
   (`retry_after`) it neither tries a release nor reconciles.

**The script lives outside warpzone.** The tick must be able to repair a warpzone that no
longer starts, so the selection and the rollback are in `warpzone/data/updater.sh`, POSIX
`sh`, standard tools and `git` only (about 250 lines), and not in Python. This is the one
exception to "decisions are pure Python"; tests run the script against real repositories
with a fake Python. The script is far longer than the first estimate of 250 lines, because
every decision above is spelled out in it. The live copy is `~/.local/share/warpzone/updater/updater.sh`, outside
the clone, so checking out any tag cannot remove or replace it. `provision` renews it
atomically, only after `sh -n` and `updater.sh selftest` pass, keeping the old file as
`.prev`. The script carries `UPDATER_ABI`, and a copy with a lower ABI is never installed
over a higher one, so a rollback does not downgrade the script. It reads its settings from
`~/.local/state/warpzone/updater/conf`, written by `provision` from `config.toml`
(`device`, `config`, `channel`, `pin`, `enabled`, `frontend`, and the `python` and `clone` it
runs), line by line and never with `source`, so a value cannot run anything. The script reads
`device`, `python` (the interpreter to try first), `enabled` (`false`, `0`, `no` or `off`
turn it off), `frontend` (the process names of the frontend, separated by spaces; default
`es-de ES-DE.AppImage`), `clone` (the git checkout; default two folders above the script) and
`config` (passed as `--config`); `channel` and `pin` are not read yet. For tests every path
can be moved: `WZ_STATE_DIR` (state, settings, `bad/`, lock), `WZ_CONF`, `WZ_LOG`, `WZ_PROC`,
`WZ_BOOT_ID_FILE`, `WZ_NOW` (the clock), `WZ_FETCH_INTERVAL`, `WZ_STEP_TIMEOUT`,
`WZ_TICK_BUDGET`, `WZ_FETCH_TIMEOUT`, `WZ_MIN_FREE_KB` and `WZ_PYTHON_FALLBACKS`. A value
that is not a number is replaced by the default. A person installs a device with `git clone` and
`sh <clone>/warpzone/data/updater.sh install`, which checks out the newest release, creates
the venv and runs `provision device`. There is one way to install; a `pipx` or wheel install
is not a supported device install.

**Verifying a version.** A version passes when all of these exit 0 within a time limit:

1. `python -I -c "import warpzone.cli, warpzone.provision, warpzone.health, ..." <clone>`,
   which also checks that `warpzone.__file__` is under the clone (`-I` imports the installed
   package, which is the checkout's only for an editable install; an install somewhere else
   would pass or fail for reasons that are not the release's, so it exits 7 and counts as
   the environment, not as a failure of the release), and `python -I -m warpzone --version`
   printing `warpzone <tag without v>`.
2. `python -m warpzone provision device <device> --unattended`.
3. `warpzone health` (see [`warpzone health`](#warpzone-health)): loads the config and the
   profiles, runs `settings apply`, `play-state sync`, `links sync` and `frontend refresh` as
   dry runs within the 15 seconds ES-DE allows its startup scripts, and checks that the
   scripts and units exist with a valid interpreter.

Each step has a time limit (`WZ_STEP_TIMEOUT`, by default 120 s for the import and the
version, 900 s for `provision` and 300 s for `health`), and the whole tick has a **budget**
of 25 minutes from its start (`WZ_TICK_BUDGET`, 1500 s), under the service's
`TimeoutStartSec=30min`: the budget cuts down the limit of every step, so two verifications
(the release and, after a failure, the one it came from) cannot add up to more than the
service allows. A step that runs out of its own time fails (a release that hangs is a bad
release). A step that was cut short by the budget says nothing about the release: the
verification gives up as "could not be judged", the device goes back to the version it came
from without marking anything bad, and it counts as an environment failure (the back-off
below). Every step runs in a process group of its own (`setsid` where it exists, job control
otherwise) and a cut kills the whole group, SIGTERM and then SIGKILL, so a grandchild cannot
outlive the step; the output of a step goes to a file, never to a `$(...)` that a grandchild
could hold open (this includes `git fetch`, limited by `WZ_FETCH_TIMEOUT`, 120 s). When the
tick itself receives SIGTERM it takes the step in progress down with it. The budget is
measured on the uptime (the first field of `/proc/uptime`, which only goes forward) and on
the date only where there is none: a device without a battery-backed clock gets its date set
by NTP in the middle of a tick, and a jump of the date must not make every later step run out
of time. A budget that runs out while looking for a Python is the environment (`env`), not
`no-python`. The script has no
`timeout` command to lean on (macOS has none), so it polls a status file. The output of the
steps goes to `last-verify.log` next to the state.

`doctor` is not used for this because it depends on the environment (DATs, tools). The check
does not prove that a game starts or that Syncthing syncs; that is what retirement is for.
`provision device --unattended` never does what could lose data: it does not apply the
bootstrap ignores to a folder that already exists (it uses the final ones when the
whitelists are on disk, and otherwise leaves the folder alone), it refuses to change `type`
or `versioning` of an existing folder, it turns a new folder in an existing group and a slow
`wait_for_files` into warnings, and it aborts without changes when a folder that should
exist has no `.stfolder` (a card that is not mounted). It never runs `prune --apply`,
`inbox --apply` or `settings capture`.

How `provision device --unattended` does that, and what it leaves as it was:

- **Existing whitelisted folder.** `_provision_whitelisted` never puts the bootstrap
  ignores (`*`) on a folder Syncthing already has. If its ignores are not the final ones, it
  checks that every whitelist file is on disk in the folder: when they are, it sets the final
  ignores and rescans; when one is not, it warns and changes nothing. (Done by hand, the
  bootstrap would leave a populated library ignored if the hub could not deliver the lists.)
  A folder that is new in this run still goes through the bootstrap.
- **`type`, `versioning`, path.** Before any write, every existing folder is compared with the
  config. A `type` or `versioning` that `_folder_patch` would change, or a path that
  differs, raises `ProvisionError` listing every folder, and nothing is changed.
- **Marker.** Before any write, each folder Syncthing has must hold its marker (`markerName`
  of the folder, `.stfolder` by default) on disk. A missing one raises `ProvisionError` and
  changes nothing. A folder with `skip_if_missing` whose directory is gone is skipped as
  before, and an unattended run does not create that directory for a folder Syncthing
  already has.
- **New folders.** A folder that is not in the device's Syncthing yet and fails the hub
  pre-check (the folder is not on the hub, or its whitelists are not) is warned about and
  left out; the same failure for an existing folder is still an error. The wait for the
  whitelists of a new folder is `UNATTENDED_WAIT_SECONDS` (30 s) unless `--timeout` is
  given, and running out of time is a warning: the folder stays on the bootstrap ignores
  and the next run finishes it.
- **Flatpak.** `ensure_flatpak_access` grants nothing and warns with the `flatpak override`
  command and the restart it needs.
- **Exit code.** `0` ok (warnings do not change it), `3` deferred, any other value failed.
  The run does all it can, then exits `3` when any write was held back because the menu
  (or an emulator, or the ES-DE settings file) was open, which is what `Reporter.deferred`
  records: `_busy`, the ES-DE settings and find-rule checks, and `titlelinks.sync`. Not being
  able to tell (no `/proc`) is a warning, not a deferral, because waiting would never help.
  An error exits `1`, also when something was deferred before it.

#### `warpzone health`

`warpzone health [device]` answers one question: does the installed version work on this
device? It exits `0` when it does and `1` when any check failed, and it writes no user data.
Without a device name it is the configured device whose ID matches the local Syncthing (as
`prune` does). Each check prints one line (`ok`, `FAIL` or `skip`), then `health: ok` or the
number of failed checks. `--config` and `--syncthing-config` work as elsewhere. In order:

1. **Config and profiles.** The same load every command does, including that each device
   subscribes only to folder groups its profile defines. A load error is a failed check and
   nothing else runs.
2. **The dry runs.** Only those that apply to the device, as `plan_steps` decides:
   `settings apply` when it lists `settings`; `play-state sync` when it lists `play-state`,
   its profile has an ES-DE folder and no `[artwork]` (a device with `[artwork]` merges the
   play state in `frontend refresh`); `links sync` when it lists `library` and its profile has
   `[links.*]`; `frontend refresh` when its profile has `[artwork]` and it lists `frontend`.
   They run in this process, not through the startup scripts, which would write. A step fails
   when it raises, when `settings apply` returns non-zero, or when it takes longer than the
   15 seconds (`STARTUP_SECONDS`) of the `timeout 15` that ES-DE's scripts use. The time is
   measured, not enforced: the caller bounds the whole run. Warnings are counted and are not
   failures, and a menu that is open is not a failure either, since a dry run never waits for it.
3. **Scripts and units.** `expected_files` lists what `provision device` leaves for this
   device: the ES-DE startup scripts (`settings`, `library` with `[links.*]`, `play-state`), the
   artwork and play state timers when the profile sets `refresh`, and the self-update units
   unless `self_update = false`. A script must exist, be executable and run an interpreter that
   exists and imports `warpzone` from outside the current folder. A service must exist and its
   first `ExecStart` word must exist (and import `warpzone` when it runs `-m warpzone`). A
   timer must exist. Services and timers are `skip`ped, not failed, when there is no systemd
   user manager (`systemctl --user show-environment` fails), since `provision device` installs
   none there. Enabled or active timers are not checked.

**What each dry run does and does not write.** `settings apply --dry-run` takes no lock, writes
no backup, snapshot or log line and no `last apply` record. `links sync --dry-run` only says
what it would do. `play-state sync --dry-run` used to take the play state lock, which creates
a lock file and its folder; a dry run now takes no lock. `frontend refresh` builds its local
Syncthing client with `dry_run=True`, which sends only `GET`, and its gamelist step takes the
`dry_run` path, which also publishes no ledger. So the Syncthing configuration, the
gamelists, the ledgers, the emulators' files and the state folders are the same before and
after (a test compares the whole home). The one thing it needs from the network is the local
Syncthing's REST API for `frontend refresh` (and for the device name when none is given):
unreachable, that step fails, and the differential verdict tells that from a bad version. With
no local Syncthing found, `frontend refresh` runs without the scope check and warns, as the
command itself does.

It is not `doctor`: it does not look at DATs or optional tools, does not run the hub checks,
and does not prove that a game starts or that Syncthing syncs. The decisions are pure
(`plan_steps`, `expected_files`, `judge_step`, `judge_file`, `exit_code` in `health.py`) and
the tests run it against a fake `/proc` and a fake Syncthing.

**Differential verdict.** The script does not trust the exit codes of a version that may be
broken. It knows three outcomes: 0 passed, 3 deferred (the menu opened during the check),
anything else failed. After a failure it goes back to the `good` version and runs the same
check:

- `good` passes: the new version is checked once more (it is checked out again and goes
  through the same three steps), so that a failure that does not repeat, such as a service that
  was slow to come up, does not burn a release for good. Only if it fails again is it
  bad, and `bad/<tag>` is written. If it passes the second time it is accepted. The same
  second look is given to a release that fails its probation check. When there is no time
  left in the tick for the second look (a release that hangs spends 900 s on the first), or
  the checkout for it fails, the release is only **under suspicion**: `suspect_tag` and
  `suspect_sha` go in the state, the device stays on `good` without a back-off, and the
  next tick (15 minutes later) looks at it first thing, with the whole budget; if it fails
  again it is bad, if it passes it is accepted. Without this, a release that hangs `provision`
  would never be burned and would cost 25 minutes of every back-off.
- `good` fails too: the environment is at fault (Syncthing down, a broken config). The tag
  is not marked bad; the device stays on `good` and retries after 1, 2, 4 and then 7 days
  (`env_failures` counts the failures in a row and a success resets it).
- `good` answers 3 (the menu opened during the second check): nothing can be concluded. The
  tag is not marked bad, `trying` is cleared and the next tick starts again.
- The verification could not be judged (it ran out of the tick's budget, or python imports
  a warpzone that is not the checkout's): no tag is blamed, the device goes back to `good`
  and backs off as for an environment failure.
- Checking out the target fails (for example a file in the way): that says nothing about the
  release, so it is treated like a broken environment.

**State.** `~/.local/state/warpzone/updater/state`, `key=value` lines, written only by the
script, to a temporary file that is renamed. Keys: `schema`, `good_tag`, `good_sha`,
`trying_tag`, `trying_sha`, `from_sha`, `attempts`, `probation`, `pending`,
`deferred_since`, `last_fetch`, `last_boot_id`, `last_result`, `retry_after`,
`env_failures`, and four that the script adds: `good_branch` (the branch to return to when
the version it started from was a clean branch inside `origin/main`, empty for a tag),
`from_tag` and `from_branch` (with `from_sha`, the version before the last switch, kept while
the new one is on probation), `last_ok` (when something last went well, for `doctor`) and
`suspect_tag` with `suspect_sha` (a release that failed once and has to be looked at again,
see the differential verdict). A tag and its sha go together in `trying` and in `suspect`:
one without the other is dropped, and `attempts` with it.
`last_result` is one of `ok`, `deferred`, `env`, `rolled-back`, `dirty`,
`dev`, `no-python`, `stuck`. `bad/<tag>` is a file holding `sha=`, `date=` and `reason=`
lines. The log stays in `self-update.log` (cut to its last 200 lines past 64 KiB). Values are
read as data: a key that is not on the list or not made of lowercase letters and underscores
is ignored, a number that is not a number is reset, and nothing is ever evaluated. `doctor`
(not written yet) reads the state and warns about a rollback, `stuck`, `dirty`, or more than
14 days without a success.

The exit status of a tick is `0` when nothing needs attention (updated, up to date, deferred,
a checkout that is `dev` or `dirty`, a lock held by another tick, `enabled` off) and `1` when
a release was rolled back, the environment is at fault, the previous version could not be
checked out (`stuck`), no Python was found, or the tick could not run. A run that was killed
leaves no status at all.

**Bad versions and the loop.**

- Before the checkout, `trying_*` and `attempts + 1` are written and synced to disk. A tick
  that finds `trying` already set knows the previous run died halfway (power cut, kill,
  hang) and resumes by verifying the tag (checking it out first when the checkout had not
  happened), counting another attempt. A check that ends in a deferral (3) is not an
  attempt and gives its count back, so a long deferral never uses them up.
- **A run that died halfway is not a failure of the version.** Only a verification that
  failed, followed by a passing `good`, writes `bad/<tag>`. A power cut never does.
- A tag is switched to at most twice without a verdict. When `attempts` reaches 2 and the
  runs keep dying, the device returns to `good` without marking the tag bad, sets
  `retry_after` one day ahead and resets `attempts`.
- A `bad` tag never comes back, and a tick that finds `trying` set for a tag that has
  become bad meanwhile goes back to `good` and clears `trying` instead of resuming.
- **A checkout the updater itself cut in the middle** (a power cut, a full disk during
  `git checkout`) leaves a tree that is neither one commit nor the other, and every later
  tick would see a dirty tree. When `trying` is set and every file that differs from HEAD
  is one that the two commits involved differ in, **and** whose content is that of one of the
  two commits (`git hash-object` against the blobs; a file that is gone or empty counts too,
  and a path that git has to quote never does), the change is the updater's own and the
  checkout is done again with `--force` onto the target; a failed checkout, including the
  one of a second look, is repaired the same way, back to where it started. A file that the
  two commits do not differ in, or that differs in content (the user edited a file that the
  release also changes, which can go unnoticed for hours of deferral), is somebody's work:
  the tree stays `dirty` and untouched. A new version on probation is not repaired at all,
  because no checkout of ours is in progress then. Before switching,
  the script checks that the clone's file system has at least `WZ_MIN_FREE_KB` (50 MiB)
  free, and if not it counts as an environment failure without touching the tree. Every
  checkout uses `--no-overwrite-ignore`, so an ignored file of the user's that a release
  also tracks stops the checkout instead of being overwritten.
- The new version stays on `probation` until it passes the check on the next boot; a
  failure there goes through the differential verdict.
- After probation there is no automatic rollback: a later failure comes from a change in
  the system, not from the version. New tags are still tried.
- If checking out `good` fails, the result is `stuck` and nothing else is touched.
- `.git/index.lock` is removed only when it is older than 10 minutes and no `git` process
  is alive (found in `/proc` like the menu; where `/proc` cannot be read the lock stays).
- The lock is a folder (`mkdir` is atomic) holding the pid and the boot id, which the
  tick reads back after writing to be sure it is the one that owns it. A tick that
  finds it held by a live process exits at once; one that finds a dead pid, a lock written
  before the last boot (the pid may belong to somebody else now) or a lock older than three
  hours, takes it over: it renames the lock to a name of its own (only one tick wins that),
  checks that what it moved is the lock it judged stale, and only then makes a new one.
- Times in the state that lie in the future (a clock without a battery-backed RTC can
  have been wrong when they were written) are dropped when the state is read: `last_fetch`,
  `deferred_since` and `last_ok` more than a day ahead, `retry_after` more than 8 days ahead.
  A tag or a branch in the state that is not a plain `vX.Y.Z` or a plain branch name is
  dropped as well, because they end up in file names and git commands.
- If `git fetch` refuses because a tag already here was moved upstream ("would clobber"),
  `last_fetch` is updated anyway and the refusal is logged, so that it is not retried at
  every tick.
- The probation check runs at the first tick after a boot (`boot_id` differs from the one
  recorded when the release was installed) and compares with `from_*`, the version before
  the switch. If the new release fails and the old one passes, the new release is marked bad
  and the device goes back; if both fail, the device stays on the new release (an environment
  that is not ready yet after a boot, such as Syncthing starting, must not downgrade it) and
  backs off like any environment failure. If the second look at the new release cannot be
  made (no time, or the checkout fails), the device stays on the previous version, which is
  the reference again, and the release is under suspicion as described above.

**Deferring while the menu is open.** The script scans `/proc/*/comm` and `argv[0]` the way
`settings_state.running` does (`WZ_PROC` points it at a fake tree in tests, and a test checks
that both agree). The names come from `esde.ESDE_PROCESSES` (unconfirmed on a console, as
that module says) or from `es_systems.process`, depending on the profile. ES-DE stays alive
during a game, so nothing updates while playing either. When it is open the tick sets
`pending=1`, sets `deferred_since` if it was not set, and exits 0; the 15 minute timer tries
again. A tick that finds the menu closed clears `deferred_since`, so the 72 hours are of an
unbroken wait and a menu that opens again days later starts a new one; an environment failure
and a failed reconcile clear it too. The frontend's process names are split into words with
pattern expansion off, so a name is never a glob. If it opens during the check, `--unattended` exits 3: `trying` is kept without a
rollback and the next tick continues. **The deferral lasts 72 hours at most**: once
`deferred_since` is older than that, the update runs anyway, because on some devices the
menu looks open for as long as the console is on, and every write is already guarded by
`_busy`. Past the limit a step that still answers 3 counts as passed and leaves `pending`
set, since otherwise a release could never be accepted on such a device. Where `/proc`
cannot be read, the script goes on (waiting would never help). `updater.sh menu` prints
`open`, `closed` or `unknown` and exits 0, 1 or 2; a test compares it with
`settings_state.running` on the same fake `/proc` trees.

**Python.** The interpreter must run `python3 -I -c "import tomllib, sys"` and be 3.11 or
newer; the candidates are the one in `conf`, `/usr/bin/python3` and `python3`. With none the
result is `no-python` and nothing rolls back. With an interpreter but a broken venv, the
script recreates it with `python3 -m venv --without-pip --clear <venv>`, plus a `.pth` file
that points at the clone and a `bin/warpzone` shim that runs `python -m warpzone`. That
needs no network and no setuptools, and it keeps the path written into units and scripts.
Only `venv/` is cleared; its parent folder also holds the two-way `play-state` folder and is
never deleted.

**Channels.** `channel = "stable"` (default) considers only `vX.Y.Z` tags; `channel = "rc"`
considers those and `vX.Y.ZrcN`. A candidate is valid when it matches the channel, is not
under `bad/` and not retired, its tag is an ancestor of `origin/main`, and the `__version__`
read with `git show <tag>:warpzone/__init__.py` equals the tag without its `v`. That
reproduces, without a server, the central check of `verify` in `release.yml`. The target is
the best valid candidate in `tag_key` order (the tests cover this order, also against
`git -c versionsort.suffix=rc for-each-ref --sort=-v:refname`). A device never moves to a
lower version than the one it runs, except when that one is retired (below).

**`RETIRED`.** A tag cannot be edited, so a release is withdrawn in a file named `RETIRED` at
the root of `main`, one entry per line: `vX.Y.Z YYYY-MM-DD reason`. The script reads it with
`git fetch origin main` and `git show origin/main:RETIRED`. A retired tag is never a
candidate. When the running version is retired the device goes to the best valid candidate,
even a lower one, and a test of the file's format keeps it parseable.

**`pin`.** `pin = "vX.Y.Z"` (or `vX.Y.ZrcN`) keeps the device on that tag whatever the
channel: the target is the pin, even when it is lower than the running version. The tag must
exist and passes the same verification as any other. A pin wins over `RETIRED`; `doctor`
warns when the pinned tag is retired.

**Kind of checkout.** Decided from `git symbolic-ref -q HEAD` and `git merge-base`:

- HEAD on a release tag: managed.
- On a branch, clean, with HEAD contained in `origin/main`: adopted (a plain `git clone`
  leaves a user there); it moves to tags like a managed one.
- Commits of its own, a loose commit or a dirty tree: `dev` or `dirty`. It is not moved, but
  it is still reconciled, and `doctor` warns. The script never runs `reset`, `stash` or
  `clean`.

**Signed tags.** Release tags are signed with an SSH key, and the script runs `git verify-tag`
against an allowed-signers file before it trusts a tag; a tag that does not verify is not a
candidate. Without it, whoever controls the maintainer's account could run code on every
device, and each holds the hub's API key. Rotating the key means shipping a new
allowed-signers file to the devices.

**Never unattended.** `prune --apply`, `inbox --apply` and `settings capture`. The shadPS4
version is the user's choice and is only read. `ensure_flatpak_access` asks for a Syncthing
restart; unattended, that is a warning.

**What a rollback does not undo.** It reverts code only: not what `provision` wrote, nor the
data in `wz-settings`, `wz-play-state` and the saves.

## Syncthing API surface used

All calls send `X-API-Key`. Base URL from config (hub) or the local
`config.xml` `<gui><address>` (device; HTTPS if `tls="true"`, self-signed allowed
only for loopback addresses).

- `GET /rest/system/status` → `myID`
- `GET /rest/config/devices`, `POST /rest/config/devices`
- `GET /rest/config/folders`, `POST /rest/config/folders`,
  `PATCH /rest/config/folders/{id}`
- `GET /rest/db/ignores?folder=`, `POST /rest/db/ignores?folder=` (`{"ignore": [...]}`)
- `GET /rest/db/status?folder=`, `GET /rest/db/file?folder=&file=`,
  `GET /rest/db/browse?folder=&levels=0`
- `POST /rest/db/scan?folder=`

Only `prune` also reads `GET /rest/db/ignores`'s `error` field and
`GET /rest/db/status`'s `state`/`stateChanged` (see [Pruning](#pruning)).
`settings capture` reads `GET /rest/db/status`'s `state` and `needTotalItems` and compares
the `local` and `global` records of `GET /rest/db/file` (see [Capture](#capture)).
`settings apply` and `audit` never call Syncthing.

Target: Syncthing v2.x (tested in CI against a pinned release, v2.1.7 at the
time of writing). Verified behaviour that the design relies on: `POST
/rest/db/ignores` works on a paused folder and writes its `.stignore`; a missing
`#include` target makes the call return 200 with an `error` field instead of
failing; `GET /rest/db/file` answers 404 with a plain-text body for objects not
in the index; Syncthing creates a folder's root directory on its own.

## Code layout

```
warpzone/
  __init__.py        version
  __main__.py        python3 -m warpzone
  cli.py             argparse, exit codes, output
  config.py          private config loading + validation (tomllib), dataclasses
  profiles.py        profile loading + validation
  model.py           folder specs: expands config + profiles into the desired state
  ignores.py         ignore rendering (common, per-device, bootstrap)
  syncthing.py       minimal REST client (urllib), redaction, dry-run recorder
  provision.py       hub and device provisioning (diff desired vs actual → calls)
  prune.py           leftover detection (pure `classify`), delete, webhook
  bios.py            BIOS manifest, local overrides, pure `judge_*`, disk checks
  dat.py             Redump / No-Intro DAT parsing, cache, matching, disc grouping (pure)
  inbox.py           unpack, identify, convert, place, file the originals
  inbox_switch.py    Switch games: identify with nsz, titledb names, decompress, place
  inbox_mame.py      arcade sets: MAME / FBNeo / 2003-Plus fit, emulator choice, ES-DE gamelist
  inbox_console_iso.py  PS3, Xbox, Xbox 360 and Wii U images: identify by signature, place
  inbox_ps4.py       PS4 packages: extract with orbis-pkg-util, place the game folder, shortcut
  inbox_vita.py      PS Vita game dumps: identify by param.sfo, place the folder and the .psvita file
  titlelinks.py      symlinks from the library's game folders into an emulator's own (pure plan)
  inbox_n3ds.py      3DS images (`.3ds`, `.cci`): NCSD / NCCH header check, placed as `.cci`
  flatpak.py         the SyncThingy flatpak: app id and the unit's commands
  selfupdate.py      `self-update`: newest release tag, checkout, log (decisions pure)
  updatetimer.py     the systemd user units of the self-update timer (pure)
  settings.py        `settings apply`, `capture`, `audit`: loading, prompts, output
  settings_formats.py  format handlers ini, qt-ini, retroarch-cfg, yaml-flat (pure)
  settings_tree.py   format handlers json, toml (pure)
  settings_layers.py layer files: parse, validate, merge, render (pure) and atomic write
  settings_plan.py   apply plan, capture candidates and pre-selection, audit rows (pure)
  settings_state.py  device state: snapshots, backups, log, lock, atomic write, processes
  esde.py            ES-DE startup script, `es_settings.xml` check and edit (pure)
  frontend.py        frontend system list rewrite, RetroArch key gap (pure)
  artwork.py         frontend artwork: ES-DE gamelist -> the frontend's gamelist, scope ignores,
                     system names, timer units (pure; the reads are passed in)
  playstate.py       play state: ledgers, merge rules, times, gamelist edits (pure)
  playstate_device.py  play state on a device: files, ES-DE gamelists, generated gamelists
  tomlw.py           TOML scalar and key writer (tomllib only reads)
  doctor.py          machine checklist (`warpzone doctor`): checks, install hints, renderer
  health.py          `warpzone health`: the dry runs and file checks that say a version works (decisions pure)
  ra.py              RetroAchievements: RAHasher wrapper, game list lookup
  localst.py         find and parse the local Syncthing config.xml
  data/updater.sh    the updater (POSIX sh): choose a release, verify it, roll it back
  data/bios.toml     BIOS manifest (names, sizes, hashes, signatures only)
  data/profiles/steamos-emudeck.toml
  data/profiles/anbernic-rg351mp.toml
templates/
  patterns-common.stignore
  patterns-device.example.stignore
examples/config.example.toml
examples/bios.example.toml
tests/               unittest; unit tests + e2e (two real Syncthing instances)
docs/design.md       this file
```

`model.py` is pure: config + profiles in, list of desired folder specs out. All
I/O sits in `syncthing.py`, `localst.py` and `provision.py`, so the diffing
logic is unit-tested without a network.

## Testing

- Unit tests (stdlib `unittest`): config validation errors, profile parsing,
  model expansion, ignore rendering, diffing (desired vs actual → calls),
  redaction, `config.xml` parsing, dry-run output.
- A fake Syncthing (`http.server` in a thread) for provision flows, including
  the bootstrap wait loop and its timeout.
- End-to-end test, skipped unless `WARPZONE_E2E_SYNCTHING` points at a
  Syncthing binary: start a hub and a device instance in temp dirs, run
  `provision hub` and `provision device`, then assert that a whitelisted file
  reaches the device, a non-whitelisted one does not, and a save created on the
  device reaches the hub. It then removes a system from the whitelist and checks
  that `prune` reports the leftovers, that `--max-gib 0` blocks the delete, and
  that `--apply` removes only the leftovers (a device-only file and every
  whitelisted file stay). The same run covers the BIOS side: a plain BIOS file
  arrives through the library, `bios/cemu/keys.txt` reaches the Cemu folder only
  through its placement (a second file in the same hub folder does not, and the
  library does not create `bios/cemu`), and a symlink at `bios/ryujinx/keys` is left
  untouched.
- Profile and model unit tests cover the placement and library-exclusion tables
  (validation, overlaps, rendering order on the device and the hub); the provision
  tests cover skipped, created (`create_when`) and dry-run targets.
- Play state tests (`test_playstate.py`, `test_playstate_device.py`): the merge rules on a
  simulated fleet with explicit delivery of ledgers (a shared gamelist, a console, removal,
  offline devices, wrong clocks, random delivery orders), ledger files, byte-preserving edits,
  ES-DE open, closed and starting, the generated gamelists, and the unchanged shared folders.
- Artwork tests (`test_artwork.py`, `test_artwork_device.py`): profile validation, the
  device and hub folder specs (the hub's stay identical whatever the device order), the pure
  converter (escaping, paths that escape, user data, determinism, system names), `ensure_gamelists`
  in a temporary home (idempotence, frontend running, no `/proc`, dry-run, a changed file), the
  timer units and their systemctl calls, scope sync against the fake Syncthing, `frontend
  refresh` and the doctor lines.
- `anbernic-rg351mp` tests: the shipped profile and its parsing, `ignorePerms` in the folder
  specs, the pure rewrite and key gap, `provision device` in a temporary home (backups,
  second run, frontend or RetroArch running, no `/proc`, dry-run) and the doctor lines.
- `prune` unit tests with the fake Syncthing: the pure decision (`classify`),
  every refusal (bootstrap ignores, wrong type, missing marker, size cap),
  protected names, nested folders, symlinks, re-check before delete, webhook.
- `bios` tests: the pure judgements, manifest and override validation, and tmpdir
  libraries with tiny fake files and a test-only manifest (no real BIOS hashes):
  hashing only files of the expected size, content matches, local acceptance,
  skipped folders, zip members, exit codes.

- `inbox` tests: DAT parsing and the cache, matching (partial games, SHA1, ambiguous
  names, duplicate files), disc grouping, destination decisions and `_done` purge
  selection as pure functions; archive extraction against traversal, absolute paths and
  links; whole runs on a temporary hub with tiny DATs and stand-in `chdman`,
  `RAHasher`, `bsdtar` and `dolphin-tool` scripts, including one cartridge flow with no
  external tool; the CLI, with the API key coming from the environment.
- Switch tests (`test_inbox_switch.py`): kind, game-of-an-update, normalization, DLC name
  and bundle numbering, file names and length limits, the `nsz --info` parser and the PFS0
  header as pure functions; titledb region order and cache; whole runs on a temporary hub
  with a stand-in `nsz` (placement of games, updates, DLC and bundle parts, `.nsz`
  decompression and every way it can fail, name fallback, conflicts, review reasons, the
  keys content never shown).
- Arcade tests (`test_inbox_mame.py`): set database parsing and its cache, fit,
  set identification and the emulator decision as pure functions over tiny synthetic
  XML databases (BIOS alternatives, merged and split clones, devices, optional and nodump
  ROMs); the gamelist update as a pure text function (add, change, remove, comments,
  other entries and unknown elements untouched, CRLF); whole runs on a temporary hub
  (placement, rename, the emulator order, Flycast for Atomiswave and Naomi, gamelist and
  its backup, conflict,
  already in the library, review reasons, folders of zips, configuration errors).
- Console image tests (`test_inbox_console_iso.py`): claiming rules, PARAM.SFO, ISO9660 and
  XDVDFS parsing as pure functions of bytes; signature verdicts on tiny synthetic images
  built in the tests (a minimal ISO9660 with the PS3 files, XDVDFS volumes at offset 0 and
  at the XGD2, XGD3 and Redump offsets, the large offsets as sparse files); whole runs on a
  temporary hub (placement, the plain `.iso` hook with a PS2 and a PSP image that a DAT
  matches, already present, conflicts, the full Xbox image, wrong content under a claimed
  name, the gamelist `<altemulator>`, zip and 7z input).
- Settings tests: for every handler, the contract on synthetic fixtures shaped like the
  real files (BOM, CRLF, no final newline, duplicated keys, `\default` companions,
  `@Variant(...)` values, an unquoted RetroArch value, xemu's multi-line array of inline
  tables, Vita3K's sequences and document markers, nested JSON): `write(text, {}) == text`,
  only changed lines differ, `NotWritable`, typed round trips checked with `json` and
  `tomllib`. Pure tests for the pattern matcher, layer validation and merge, `plan_apply`
  (every branch of the `C`/`L`/`S` table, first run, new keys), `candidates`, pre-selection,
  discard and audit rows. Process detection over a fake `/proc` tree, including a process
  whose arguments name the emulator and warpzone's own process. Whole `apply`, `capture`
  and `audit` runs on a temporary home: backups and their rotation, atomic writes,
  idempotence (a second run writes nothing), held keys, a running emulator, a file changed
  between read and write, the lock, invalid layers changing nothing, scripted prompts,
  `--yes` and the pattern flags, a report without a terminal, conflict copies and the
  Syncthing in-sync check with the fake Syncthing. Fake login values (marked for gitleaks)
  must never appear in output, layers, snapshots or the log. The startup script rendering
  and the `es_settings.xml` check and edit as pure functions (value changed, line added,
  every other byte kept, CRLF); `provision device` writing the script and enabling the
  setting with a backup, leaving it alone while a fake `/proc` shows ES-DE running, and
  their dry run. The e2e test subscribes the device to `settings` and checks that a layer file
  travels both ways.
- A second e2e test starts from an empty hub: its folders exist and hold nothing. `provision
  hub` runs while the whitelists and a game are copied in, so the bootstrap waits for them;
  the test then checks the created `wz-*` folders, the final ignores, `check`, and that
  `provision device` makes a device receive the whitelisted game and not the other one.
- Updater script tests (`test_updater_sh.py`): `updater.sh` runs against real git
  repositories in temporary folders (an upstream with tagged releases, a clone, a fake
  `python` that behaves like warpzone according to the checked-out version, a fake `/proc`
  and a fake clock): a good update, the numeric order of tags, tags that are not ancestors of
  `origin/main` or whose version disagrees, a broken import, a wrong `--version`, a failing
  `provision`, a failing `health` and a hang (all bad when `good` passes and the release fails
  again), a failure that happens once (not bad), a step cut off with a grandchild alive (the
  grandchild dies and the pipe is free), the tick budget and the second look at a release
  that ran out of it, the budget on the uptime and not on the date, a user's edit of a file
  the release changes (during a deferral and during probation), a python that imports another
  warpzone, a checkout cut in the middle by a filter that kills the process group or fails
  (the tree is repaired), too little free space, an ignored file in the way, a menu that
  closes and opens again, a lock from another boot, times from the future, a moved tag, a
  broken environment (no tag burned, back-off of 1, 2, 4, 7 and 7 days), a bad tag that never returns,
  a power cut (a fake that kills its own process group; no tag burned, a stale lock taken
  over, the second attempt, the give-up with `retry_after`), a menu that opens during the
  check, the 72 hour limit, probation at the next boot, the kinds of checkout (`dev`, `dirty`,
  a loose commit, an adopted branch that comes back to its branch), the stale `index.lock`, the
  daily fetch, state and settings that contain shell syntax, and `selftest`. The syntax check,
  `selftest` and a full update run with every shell found among `sh`, `dash` and `bash`
  (`WZ_TEST_SHELL` picks the shell for all the others), and shellcheck runs when installed.
- Self-update tests (`test_selfupdate.py`, `test_update_timer.py`): tag and version
  comparison as pure functions (including the order `rc1` < `rc2` < release); whole runs against a bare repository used as the remote
  (update, up to date, ahead, dirty tree, untracked files, dry-run, remote gone, timeout,
  not a checkout, `pyproject.toml` changed or not, the log); the units' rendering and
  `ensure_update_timer` with a fake `systemctl` (first run, second run, changed unit,
  switched off, dry-run, no user manager, failed enable); `provision device` for both shipped
  profiles and with `self_update = false`; the doctor judge and check.
- `doctor` tests: the renderer and exit code as pure functions; every check with fake
  probes (tool present, missing per platform, unrunnable, unrecognizable version; config
  missing, partial or invalid; Syncthing absent, answering, refusing, not on loopback;
  DATs old or absent; hub unmounted, incomplete or short of space); and that no key
  (RetroAchievements, Syncthing) appears anywhere in the output.

## CI (GitHub Actions)

- `lint`: ruff check + ruff format --check.
- `test`: unittest on Python 3.11, 3.12, 3.13, 3.14, ubuntu-latest and macos-latest.
- `e2e`: ubuntu-latest, downloads a pinned Syncthing release, verifies its
  SHA-256 against a value committed in the workflow, runs the e2e test.
- `secrets`: gitleaks over the full history.
- Every third-party action pinned by commit SHA, `permissions: contents: read`
  at workflow level, Dependabot for GitHub Actions.
