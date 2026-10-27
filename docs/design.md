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
│   ├── bios/
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

"Common only" and "fixed whitelist" folders get their ignore lines inline
through `POST /rest/db/ignores` (the common lines, then the fixed or
per-save lines), on the hub and on devices alike. They need no ignore files,
so the user only ever creates `patterns-*.stignore` in `library/` and `songs/`.

Folder-level settings:

- Save folders on the hub: versioning `staggered`, `maxAge` 30 days.
- Everything else: no versioning (the hub is the authority; filesystem
  snapshots on the hub are recommended in the README instead).
- `fsWatcherEnabled: true` everywhere.

A device subscribes to a subset of folders, set in the private config
(`folders = ["library", "tools", "saves", "frontend", "songs"]`). `saves`
expands to every save folder of the device's profile; `frontend` expands to
`wz-media` and `wz-esde`.

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

If the device's whitelist does not exist on the hub, provisioning stops before
step 1 with a clear error. It never creates a device folder that would pull the
whole library. The check reads the hub's API (`GET /rest/db/browse`) with the
hub credentials from the private config; if the hub is unreachable it warns and
continues, which is safe because the bootstrap ignores only let whitelists
through. On the hub side, `provision hub` cannot check files in a folder that
does not exist yet, so it creates the folder and warns about missing whitelists;
`warpzone check` is the authoritative report once the hub has scanned.

## Profiles

A profile describes where things live on one kind of device. Profiles are data
files shipped in the repo (`warpzone/profiles/<name>.toml`), because they are
the part other people can reuse and improve.

v0.1 ships `steamos-emudeck`. Paths use `~` and are expanded on the device.

```toml
name = "steamos-emudeck"
description = "SteamOS with EmuDeck (Steam Deck, Steam Machine)"

[paths]
library = "~/Emulation"
songs = "~/Songs"
tools = "~/.local/share/warpzone/tools"
media = "~/Emulation/tools/downloaded_media"
esde = "~/ES-DE"

# One entry per save folder. `path` is the REAL directory: EmuDeck exposes
# saves through symlinks under ~/Emulation/saves and Syncthing never follows
# symlinks, so pointing Syncthing at the symlink would sync nothing.
[saves.retroarch-saves]
path = "~/.var/app/org.libretro.RetroArch/config/retroarch/saves"
[saves.retroarch-states]
path = "~/.var/app/org.libretro.RetroArch/config/retroarch/states"
[saves.dolphin-gc]
path = "~/.var/app/org.DolphinEmu.dolphin-emu/data/dolphin-emu/GC"
[saves.dolphin-wii]
path = "~/.var/app/org.DolphinEmu.dolphin-emu/data/dolphin-emu/Wii"
[saves.dolphin-states]
path = "~/.var/app/org.DolphinEmu.dolphin-emu/data/dolphin-emu/StateSaves"
[saves.duckstation-saves]
path = "~/Emulation/saves/duckstation/saves"
[saves.duckstation-states]
path = "~/Emulation/saves/duckstation/states"
[saves.pcsx2-saves]
path = "~/Emulation/saves/pcsx2/saves"
[saves.pcsx2-states]
path = "~/Emulation/saves/pcsx2/states"
[saves.melonds-saves]
path = "~/Emulation/saves/melonds/saves"
[saves.melonds-states]
path = "~/Emulation/saves/melonds/states"
[saves.eden-saves]
path = "~/Emulation/storage/eden/nand/user/save"
[saves.ryujinx-user]
path = "~/.config/Ryujinx/bis/user"
ignore = ["!/save", "!/saveMeta", "*"]
[saves.azahar-saves]
path = "~/Emulation/storage/azahar/sdmc"
[saves.azahar-states]
path = "~/.local/share/azahar-emu/states"
[saves.shadps4-saves]
path = "~/.local/share/shadPS4/savedata"
[saves.cemu-saves]
path = "~/Emulation/roms/wiiu/mlc01/usr/save"
[saves.xenia-content]
path = "~/Emulation/roms/xbox360/content"
```

The save key `<emu>-<kind>` maps to folder ID `wz-save-<emu>-<kind>` and hub
path `saves/<emu>/<kind>`. A save folder with an `ignore` list gets those
patterns (after `#include patterns-common.stignore`).

Save paths that do not exist on the device yet (the emulator was never opened)
are skipped with a warning, never created by warpzone, and picked up by the
next `provision` run. Some save folders sit inside `~/Emulation`, which is the
`wz-library` root; that is fine because the library whitelist ignores them, and
Syncthing supports nested folders when the parent ignores the child.

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
root = "/Volumes/games"                      # the hub root as mounted on this machine
dats_dir = "~/dats/libretro-database/metadat"
work_dir = "~/tmp/warpzone"                  # optional; default: the system temp folder
chdman = "/opt/homebrew/bin/chdman"          # optional tools; PATH is searched when unset
bsdtar = "/usr/bin/bsdtar"
rahasher = "~/tools/RAHasher"                # only used when set
dolphin_tool = "/usr/local/bin/dolphin-tool"
```

`warpzone inbox` reads only the `[inbox]` table, so a desktop that only files games
needs no `[hub]` or `[devices]`. API keys are never printed, logged or included in
error messages; the RetroAchievements key is read from `RA_API_KEY`, never from a file.

## Command line

`python3 -m warpzone <command>`; also installable as a `warpzone` script.

| Command | Runs on | Does |
|---|---|---|
| `warpzone check` | anywhere | Validates config + profiles, reports which whitelists are missing on the hub (via `GET /rest/db/browse`), never writes. Exit 1 if any are missing; `--offline` skips the hub. |
| `warpzone provision hub` | anywhere that reaches the hub API | Adds every configured device to the hub, creates/updates every `wz-*` folder on the hub and shares it with the devices that subscribe to it, sets the hub ignores. Idempotent. |
| `warpzone provision device <name>` | on the device | Reads the local Syncthing API key from the local `config.xml` (searching the default locations, including the SyncThingy flatpak; `--syncthing-config PATH` overrides), adds the hub device, creates the subscribed folders with the bootstrap above (`--timeout SECONDS`). Idempotent. |
| `warpzone prune [device]` | on the device | Reports, and with `--apply` deletes, the files of the whitelisted folders that the device no longer whitelists (see [Pruning](#pruning)). Report only by default. |
| `warpzone bios` | on a device or the hub | Reports which BIOS and firmware files a library has, lacks or has wrong (see [BIOS check](#bios-check)). Read-only. Exit 1 if a required item is missing or wrong. |
| `warpzone inbox` | a desktop that mounts the hub's share | Identifies, converts and files new games dropped in `inbox/` (see [Inbox](#inbox)). Report only by default, `--apply` acts. Exit 1 if any item needs review. |
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
  hashes and zip member CRC32s, all public metadata, and nothing else. The
  command only reads the library and writes nothing.
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
- **Exit code.** `0` when no required item is missing or wrong; `1` otherwise;
  `2` for a usage or override-file error. A wrong file that sits next to an ok
  alternative is reported but does not fail the run. Optional items never fail it.
- **Root.** `--root` is the library folder (the one holding `bios/` and
  `roms/`). Without it: the first existing `library` path of the configured
  profiles, then `<hub root>/library`. The list of unknown files in `bios/` is
  only computed when every system is checked.

The decisions (`judge_file`, `judge_zip`, `settle`, `unknown_files`,
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
- **Out of scope.** Switch, MAME/arcade, Vita, PS3 and Xbox. Switch, Vita, PS3 and Xbox
  files are recognised by extension or folder and go to review with "not supported by
  inbox yet"; arcade sets simply have no DAT match. A disc image kept as a single `.bin`
  for several tracks is not identified (the DATs list one file per track).
- **Exit code.** `0` when every item is placed or already in the library; `1` when any
  item went to review; `2` for a usage or configuration error.

The decisions (`skip_name`, `unsafe_member`, `select_purge`, `plan_units`,
`destination_verdict`, `whitelist_line`, `resolve_refs`, `unsupported_reason`,
`match_dat`, `group_discs`, `parse_dat`) are pure and unit-tested; conversions are
tested with stand-in `chdman` and `RAHasher` scripts, and one test runs the cartridge
flow end to end with no external tool.

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
  ra.py              RetroAchievements: RAHasher wrapper, game list lookup
  localst.py         find and parse the local Syncthing config.xml
  profiles/steamos-emudeck.toml
  data/bios.toml     BIOS manifest (names, sizes, hashes only)
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
  whitelisted file stay).
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

## CI (GitHub Actions)

- `lint`: ruff check + ruff format --check.
- `test`: unittest on Python 3.11, 3.12, 3.13, 3.14, ubuntu-latest and macos-latest.
- `e2e`: ubuntu-latest, downloads a pinned Syncthing release, verifies its
  SHA-256 against a value committed in the workflow, runs the e2e test.
- `secrets`: gitleaks over the full history.
- Every third-party action pinned by commit SHA, `permissions: contents: read`
  at workflow level, Dependabot for GitHub Actions.

## Roadmap (not v0.1)

- Home Assistant: Syncthing folder sensors, script notifications.
- Switch save migration between emulators (one-shot, with backup).
- More profiles (macOS, Windows, Batocera).
