# warpzone design

warpzone keeps one game library on a home server (the **hub**, typically a NAS)
and delivers it to every console you own (the **devices**: Steam Deck, Steam
Machine, a spare Mac...). Saves follow you from one device to the next. It is a
thin layer of conventions and automation on top of [Syncthing](https://syncthing.net):
warpzone never moves game data itself, it only configures Syncthing and keeps
each device's copy honest.

This document is the contract for the implementation. Anything not described
here is out of scope for v0.1.

## Goals

- One source of truth for ROMs, BIOS and extras: the hub. Devices only receive.
- Each device chooses what it gets with a whitelist that is **edited only on the
  hub**. No file is ever edited by hand on a device after provisioning.
- Saves sync in both directions between all devices, with history kept on the hub.
- Adding a device is one command.
- No dependencies beyond the Python standard library (3.11+), so the tool runs
  unchanged on SteamOS, macOS and any Linux box.

## Non-goals (v0.1)

- Moving, renaming or converting ROMs that are already in the library. New content goes through `warpzone inbox`, which only ever adds.
- Shipping or locating any copyrighted content (ROMs, BIOS, keys, firmware).
  The BIOS checker compares what the user has with public names, sizes and hashes.
- Running on the hub itself. Many NAS ship an old Python; the hub is driven
  remotely through its Syncthing REST API.

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
│   └── es-de/               ES-DE gamelists, collections ⇄
├── songs/                   hub: send-only → opted-in devices, per-device whitelist
├── tools/                   hub: send-only → devices (scripts, docs)
└── inbox/                   never synced; drop zone for new content (SMB)
```

Why the split:

- `library` is receive-only on devices, so an accidental delete on a device can
  never propagate to the hub.
- Saves need two-way sync; they live in their own folders so their direction
  cannot leak into the library.
- `songs` is not emulation (Clone Hero, YARG) and is mounted outside `~/Emulation`.

## Folders

Every folder warpzone manages has a stable Syncthing folder ID prefixed `wz-`.

| Folder ID | Hub path | Hub type | Device type | Device path (profile key) | Ignores |
|---|---|---|---|---|---|
| `wz-library` | `library` | sendonly | receiveonly | `library` | per-device whitelist |
| `wz-songs` | `songs` | sendonly | receiveonly | `songs` | per-device whitelist |
| `wz-tools` | `tools` | sendonly | receiveonly | `tools` | common only |
| `wz-media` | `frontend/media` | sendreceive | sendreceive | `media` | common only |
| `wz-esde` | `frontend/es-de` | sendreceive | sendreceive | `esde` | fixed whitelist: `gamelists`, `collections` |
| `wz-save-<emu>-<kind>` | `saves/<emu>/<kind>` | sendreceive | sendreceive | from profile | common only |
| `wz-bios-<emu>-<kind>` | `library/<hub path from profile>` | sendonly | receiveonly | from profile | fixed whitelist from the profile |

The `wz-library` ignore lines differ from the other whitelisted folders in one
way: fixed exclusions come before the `#include`, on both sides (see
[Library exclusions](#library-exclusions-and-bios-placements)). Save and
`wz-bios-*` entries may carry their own ignore list (a whitelist ending in `*`).

"Common only" and "fixed whitelist" folders get their ignore lines inline
through `POST /rest/db/ignores` (the common lines, then the fixed or
per-save lines), on the hub and on devices alike. They need no ignore files,
so the user only ever creates `patterns-*.stignore` in `library/` and `songs/`.

Folder-level settings:

- Save folders on the hub: versioning `staggered`, `maxAge` 30 days.
- Everything else: no versioning (the hub is the authority; filesystem
  snapshots on the hub are recommended in [safety.md](safety.md) instead).
- `fsWatcherEnabled: true` everywhere.

A device subscribes to a subset of folders, set in the private config
(`folders = ["library", "tools", "saves", "frontend", "songs", "bios"]`). `saves`
expands to every save folder of the device's profile; `frontend` expands to
`wz-media` and `wz-esde`; `bios` expands to every BIOS placement of the profile.

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

v0.1 ships `steamos-emudeck`. Paths use `~` and are expanded on the device.
It has four kinds of entry; the shipped file is the reference, this is its shape:

```toml
name = "steamos-emudeck"
description = "SteamOS with EmuDeck (Steam Deck, Steam Machine)"

[paths]
library = "~/Emulation"
songs = "~/Songs"
tools = "~/.local/share/warpzone/tools"
media = "~/Emulation/tools/downloaded_media"
esde = "~/ES-DE"

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
It is per device and has no entry (as is xemu's `eeprom.bin`). xemu's BIOS files
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

[devices.machine]
device_id = "ZZZZZZZ-..."
profile = "steamos-emudeck"
folders = ["library", "tools", "saves", "frontend", "songs"]

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
| `warpzone provision device <name>` | on the device | Reads the local Syncthing API key from the local `config.xml` (searching the default locations, including the SyncThingy flatpak; `--syncthing-config PATH` overrides), adds the hub device, creates the subscribed folders with the bootstrap above (`--timeout SECONDS`). When that Syncthing is the SyncThingy flatpak it first grants it the missing `flatpak override --user --filesystem=~/.var/app/<app>` for every folder under `~/.var/app` (flatpak hides other apps' data), and asks for a Syncthing restart. Idempotent. |
| `warpzone prune [device]` | on the device | Reports, and with `--apply` deletes, the files of the whitelisted folders that the device no longer whitelists (see [Pruning](#pruning)). Report only by default. |
| `warpzone bios` | on a device or the hub | Reports which BIOS and firmware files a library has, lacks or has wrong (see [BIOS check](#bios-check)). Read-only. Exit 1 if a required item is missing or wrong. |
| `warpzone inbox` | a desktop that mounts the hub's share | Identifies, converts and files new games dropped in `inbox/` (see [Inbox](#inbox)). Report only by default, `--apply` acts. Exit 1 if any item needs review. |
| `warpzone doctor` | any machine | Checklist of this machine: tools, config, local Syncthing, DATs, hub folder (see [Doctor](#doctor)). Offline, never changes anything. Exit 1 if a required check fails. |
| `warpzone status` | anywhere | Folder state, need counts, errors, receive-only local changes, for the hub (and the local device if any). |
| `warpzone render-ignores <device>` | anywhere | Prints the ignore lines a device folder would get. For debugging. |

Every writing command supports `--dry-run`, which prints the REST calls it
would make (method, path, JSON body with secrets redacted) and sends no write.
It still sends the GET requests it needs to compute the diff. Every command
takes `--config PATH` (default `$WARPZONE_CONFIG`, then the XDG path).

Exit codes: `0` ok, `1` runtime error, `2` usage or config error.

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
- **PS3, Xbox and Xbox 360 disc images.** See [Console disc images](#console-disc-images)
  below; they are identified by signature, not by DAT.
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

PS3, Xbox and Xbox 360 images have no Redump or No-Intro entry, so they are identified by
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
  - *PS3*: an ISO9660 primary volume descriptor at sector 16; the root must hold
    `PS3_DISC.SFB` and a `PS3_GAME` directory with `PARAM.SFO`, whose `TITLE_ID` and `TITLE`
    are read.
- **Claiming.** The handler claims an item (a file, a folder, or an archive listed by
  `zipfile` or `bsdtar -tf`) when a file name ends in `.dec.iso`, `.xiso.iso` or `.xiso`.
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
- **Placement.** `library/roms/ps3/`, `roms/xbox/` and `roms/xbox360/roms/` (the folder
  EmuDeck's ES-DE system for the 360 reads), under the file's own name. It is copied to
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
  inbox_console_iso.py  PS3, Xbox and Xbox 360 disc images: identify by signature, place
  flatpak.py         the SyncThingy flatpak: app id and the unit's commands
  doctor.py          machine checklist (`warpzone doctor`): checks, install hints, renderer
  ra.py              RetroAchievements: RAHasher wrapper, game list lookup
  localst.py         find and parse the local Syncthing config.xml
  data/bios.toml     BIOS manifest (names, sizes, hashes, signatures only)
  data/profiles/steamos-emudeck.toml
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
