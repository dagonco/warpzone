# warpzone

[![ci](https://github.com/dagonco/warpzone/actions/workflows/ci.yml/badge.svg)](https://github.com/dagonco/warpzone/actions/workflows/ci.yml)
[![release](https://img.shields.io/github/v/release/dagonco/warpzone)](https://github.com/dagonco/warpzone/releases/latest)

Keep one game library on a home server and deliver it to every console you own, with saves that follow you from one device to the next.

You have a NAS full of ROMs, a Steam Deck, maybe a Steam Machine, maybe a spare Mac. Each one has its own copy, its own idea of which games it wants, and its own saves. `warpzone` is a thin layer of conventions and automation on top of [Syncthing](https://syncthing.net) that makes the server the single source of truth. It never moves a byte of game data itself: it only configures Syncthing and keeps each device's setup honest.

```bash
warpzone check                      # validate the config, list missing whitelists
warpzone provision hub              # once: set up the hub's Syncthing (remote API)
warpzone provision device deck      # on each console: one command
warpzone prune                      # on a console: report (or --apply delete) what it no longer whitelists
warpzone bios                       # which BIOS and firmware files the library has, lacks or has wrong
warpzone inbox                      # report (or --apply) how new games in inbox/ would be filed
warpzone doctor                     # check this machine: tools, config, Syncthing, DATs, hub; prints the fix for each problem
```

## The problem

- Copying a 2 TB library to a 512 GB device means picking games by hand, every time.
- Editing a "what do I want here" list on each device, over SSH or with a gamepad, does not scale past one device.
- Saves live in a dozen emulator-specific places, and syncing them naively syncs the wrong things.
- One accidental delete on a console should never reach the server.

`warpzone` fixes this with three rules: the hub owns the library, devices only receive it, and each device chooses what it gets with a whitelist that is **edited only on the hub**.

## Hub layout

```
<hub root>/
├── library/                 hub: send-only   -> device: receive-only, per-device whitelist
│   ├── roms/<system>/       ES-DE system folder names
│   ├── bios/
│   ├── extras/              switch/{updates,dlc,mods,misc}, amiibo/, texturepacks/
│   ├── patterns-common.stignore
│   └── patterns-<device>.stignore
├── saves/<emulator>/<kind>/ hub: send-receive <-> devices: send-receive, hub keeps 30 days of history
├── frontend/
│   ├── media/               ES-DE downloaded media     <->
│   └── es-de/               ES-DE gamelists, collections <->
├── songs/                   hub: send-only -> opted-in devices, per-device whitelist
├── tools/                   hub: send-only -> devices (scripts, docs)
└── inbox/                   never synced; drop zone for new content (SMB)
```

Saves get their own folders so their two-way direction can never leak into the library. `songs` is not emulation (Clone Hero, YARG) and lands outside `~/Emulation`.

## Requirements

- Syncthing v2.x on the hub and on every device (tested against v2.1.7).
- Python 3.11+ on the machine that runs `warpzone`. Standard library only: it runs unchanged on SteamOS, macOS and any Linux box.
- The hub is driven remotely through its Syncthing REST API, so it does not need Python (many NAS ship an old one). Enable the GUI/API on a LAN address of the hub.

## Install

From a release (see [CHANGELOG.md](CHANGELOG.md) for what each one contains):

```bash
pipx install https://github.com/dagonco/warpzone/releases/download/v0.5.0/warpzone-0.5.0-py3-none-any.whl
# or, without pipx:
python3 -m pip install --user https://github.com/dagonco/warpzone/releases/download/v0.5.0/warpzone-0.5.0-py3-none-any.whl
warpzone --version
```

Each release also lists a `SHA256SUMS` file and carries a build provenance attestation (`gh attestation verify <file> --repo dagonco/warpzone`).

From source:

```bash
git clone https://github.com/dagonco/warpzone.git && cd warpzone
python3 -m warpzone --help        # run from the clone, nothing to install
pip install .                     # optional: gives you a `warpzone` command
```

## Quickstart

**1. Write the private config.** Copy [`examples/config.example.toml`](examples/config.example.toml) to `~/.config/warpzone/config.toml` and fill in the device IDs (Syncthing: Actions > Show ID) and the hub's API key (Actions > Settings > GUI). This file is the only place with personal data; keep the key in a file with `chmod 600`. The same file is used on every machine, so copy it to each device (or keep it in your dotfiles).

**2. Put the whitelists on the hub.** In the hub's `library/` folder, copy [`templates/patterns-common.stignore`](templates/patterns-common.stignore) and make one `patterns-<device>.stignore` per device from [`templates/patterns-device.example.stignore`](templates/patterns-device.example.stignore). Do the same in `songs/` for devices that subscribe to it.

**3. Check, then configure the hub.**

```bash
warpzone check
warpzone provision hub --dry-run    # prints the REST calls it would make, makes none
warpzone provision hub
```

This adds every configured device, creates the `wz-*` folders on the hub and shares each with the devices that subscribe to it. It is idempotent: run it again whenever you add a device.

**4. Provision each device, on the device.**

```bash
warpzone provision device deck
```

It finds the local Syncthing's API key in its `config.xml` (including the SyncThingy flatpak), adds the hub, and creates the subscribed folders. Save folders whose directory does not exist yet (you never opened that emulator) are skipped with a warning and picked up by the next run. Use `--syncthing-config PATH` if your `config.xml` is somewhere unusual.

`warpzone status` shows folder state, outstanding items and receive-only local changes. `warpzone render-ignores deck` prints the ignore lines a device folder would get.

## How whitelists work

The whitelist for a device is an ordinary Syncthing ignore file that lives **inside the synced folder on the hub**, so it syncs to every device like any other file. A device's own `.stignore` is a single line that never changes after provisioning:

```
#include patterns-deck.stignore
```

To change what a device gets, edit `patterns-deck.stignore` on the hub (over SMB, SSH, wherever). No file is ever edited by hand on a device.

The edit syncs to the device within seconds, but Syncthing only applies a changed whitelist on a full scan of the folder, and a freshly synced ignore file does not reliably trigger one (whether it does depends on the OS and the filesystem watcher). So `warpzone` sets a short rescan interval on whitelisted device folders: **a whitelist edit takes effect within 5 minutes**. Tune it per device with `rescan_seconds` in the config (minimum 5; a rescan only stats files, so it is cheap). To apply an edit immediately, press Rescan on the folder in the device's Syncthing GUI.

A whitelist starts with `#include patterns-common.stignore`, lists what the device wants, and ends with `*`:

```
#include patterns-common.stignore
!/roms/nes
!/roms/psx/Some Game (USA)
!/bios
*
```

Every device receives every whitelist but applies only its own.

**Bootstrap.** Syncthing refuses to sync a folder whose `.stignore` includes a file that has not arrived yet, and on a fresh device nothing has. So `provision device` first creates the folder with ignores that let only the whitelists through, waits (default 10 minutes, `--timeout`) until `patterns-<device>.stignore` and `patterns-common.stignore` are on the device, and only then switches to the `#include`. If the device's whitelist is missing on the hub, it stops before touching anything, so it can never pull the whole library.

Folders without a whitelist (tools, media, saves, ES-DE) get their fixed ignore lines directly through the API; they need no files.

## Pruning what a device no longer whitelists

When you take something off a device's whitelist, Syncthing stops syncing it but leaves the files on the device. Run `prune` on the device to clean up. It only reports unless you add `--apply`:

```
$ warpzone prune
wz-library: 412 leftover file(s), 38.2 GiB
  roms/n64: 380 file(s), 36.9 GiB
  roms/psx: 32 file(s), 1.3 GiB
wz-library: kept 1 ignored file(s) the hub cannot restore
  roms/n64/my-patch.z64 (the hub does not have it)
total: 412 leftover file(s), 38.2 GiB
report only: run again with --apply to delete

$ warpzone prune --apply
...
wz-library: deleted 412 file(s), 38.2 GiB, removed 9 empty folder(s)
```

It asks Syncthing which files it ignores (after a full rescan) instead of guessing from the patterns, and it deletes a file only if the hub also has it, unchanged: anything that exists only on the device, or that you edited, is reported and kept. It refuses to delete more than `--max-gib` (default 50) in one run unless you pass `--force`, never touches `.stfolder`, `.stignore`, the whitelist files or other Syncthing folders, and refuses to run while a device is still bootstrapping. `--list` prints every file; `--notify-url URL` POSTs a JSON summary (`device`, `folder`, `files`, `bytes`, `applied`) to a webhook, for example a Home Assistant automation. Details in [`docs/design.md`](docs/design.md#pruning).

## Checking BIOS files

Many emulators refuse to start a game without the right BIOS or firmware, and the failure message rarely says which file. `warpzone bios` reads the library and tells you, per system, what is there, what is missing and what has an unexpected hash:

```
$ warpzone bios --root /path/to/library
psx: Sony PlayStation
  ok                  bios: SCPH1001.BIN (matches scph1001.bin)
saturn: Sega Saturn
  wrong (required)    bios: sega_101.bin: md5 cb37ed68..., expected 85ec9ca4...
ps2: Sony PlayStation 2
  missing (required)  bios: one of ps2-0100j-20000117.bin, ps2-0100jd-20000117.bin, ... (66 in all)
                      unlocks: PlayStation 2 games; one dump of the console region you play is enough
3do: Panasonic 3DO: no games in roms/{3do}, not checked
unknown files in bios/: 3
  (list them with --all)
items: 12 ok, 1 missing, 1 wrong
2 required item(s) missing or wrong
```

It is read-only and exits 1 when a required item is missing or wrong, so it also works in a script. Only systems that have games are judged: if the ROM folders of a system (for example `roms/3do`) hold nothing but EmuDeck placeholders, media or the BIOS zips themselves, the system is shown as "no games, not checked" and never affects the exit code. Naming it with `--system` checks it anyway. Without `--root` it uses this device's `~/Emulation` (from the profile) or, on the hub, `<hub root>/library`. `--system psx ps2` limits the check, `--all` lists the unknown files in `bios/`.

Some items are "one of": a PlayStation needs one BIOS dump, not all of them. Zip sets for MAME (`neogeo.zip`, `naomi.zip`, `awbios.zip`, ...) are checked in `roms/mame/` by member name and CRC32. Only files of the expected size are read.

If you keep a modified or region-free BIOS on purpose, accept its hash in a local file, `bios.toml` next to your `config.toml` (or `--overrides PATH`), and it is reported as "accepted (local)". See [`examples/bios.example.toml`](examples/bios.example.toml).

The manifest only holds file names, sizes and hashes, taken from [libretro-database](https://github.com/libretro/libretro-database) and MAME. `warpzone` never downloads a BIOS and this repository contains none.

## Adding new games: the inbox

Drop new dumps (files, folders or archives) in `inbox/` on the hub's share and run `warpzone inbox` on a desktop machine that mounts it (the NAS is too weak for conversions). Without `--apply` it only reports:

```
$ warpzone inbox
inbox: /Volumes/hub/inbox (report only), 2 item(s)
tools: chdman yes, bsdtar yes, rahasher yes, dolphin-tool no; DATs: dreamcast, gb, ...
[1/2] some random name.zip
  nes: Adventure Island (USA).zip: would place
    path: library/roms/nes/Adventure Island (USA).zip
    RA: supported (Adventure Island)
    whitelist: !/roms/nes/Adventure Island (USA).zip
  would move to inbox/_done/2026-10-27/
[2/2] crash bash dump
  psx: Crash Bash (Europe) (En,Fr,De,Es,It).chd: would place
    path: library/roms/psx/Crash Bash (Europe) (En,Fr,De,Es,It).chd
    RA: hash not linked
    whitelist: !/roms/psx/Crash Bash (Europe) (En,Fr,De,Es,It).chd
  would move to inbox/_done/2026-10-27/
items: 2; games to place: 2, already in the library: 0; items for review: 0
report only: run again with --apply to convert and place
```

What it does for each item:

1. **Unpacks** `.zip` (and `.7z` / `.rar` with `bsdtar`) into a local work folder, never onto the share. Archive members are untrusted: absolute paths, `..` and symbolic links are refused.
2. **Identifies by content**, whatever the files are called: size and CRC32 (and SHA1, when the DAT has it) against the Redump and No-Intro DATs of [libretro-database](https://github.com/libretro/libretro-database) (`metadat/redump`, `metadat/no-intro`). A game matches when all its DAT roms are present. DATs are parsed only when needed and cached in `$XDG_CACHE_HOME/warpzone/dats`.
3. **Converts** to what the library already holds:

   | System | Result |
   |---|---|
   | `psx`, `saturn`, `dreamcast` | `<game>.chd` (`chdman createcd` from cue/bin or gdi), then `chdman verify` |
   | `ps2` | `<game>.chd` (`createdvd` for an `.iso`, `createcd` for cue/bin), then `verify` |
   | `psp` | the plain `.iso`, its CRC32 checked against the DAT |
   | `gc` | `.iso` stays `.iso` (the report shows the `dolphin-tool convert -f rvz ...` command); an `.rvz` is kept as it is |
   | `nes`, `snes`, `n64`, `gb`, `gbc`, `gba`, `genesis`, `mastersystem`, `gamegear` | `<game>.zip` holding the ROM under its DAT name |

   A multi-disc game of a CHD system goes in `<game without disc tag>.m3u/` with every disc and a `<game>.m3u` playlist, and only when all its discs are in the item; otherwise it goes to review.
4. **RetroAchievements** (optional). With `rahasher` configured it hashes the source and the result and refuses to place a game whose hash changed. With `RA_API_KEY` in the environment it looks the hash up and reports `supported (<title>)` or `hash not linked`. The key is never printed.
5. **Places** the result in `library/roms/<system>/` (copied to `<name>.part`, read back, then renamed). It never overwrites: the same content is reported as "already in the library", different content under the same name goes to review.
6. **Files the original**: to `inbox/_done/<date>/` (on every `--apply`, `_done` folders named like a date and older than 7 days are deleted, nothing else), or to `inbox/_revisar/` with a `<item>.reason.txt` (no DAT match, incomplete multi-disc, conversion or verify failed, RA hash changed, conflict, missing tool).

Items modified in the last 30 seconds are left alone (they may still be copying), and `_*` folders, hidden files, `@eaDir` and `.DS_Store` are never items. warpzone does not edit whitelists: add the printed lines to the device whitelists yourself. Exit code 1 when any item needs review.

Switch, Vita, PS3 and Xbox are not supported by `inbox` yet; those items go to review ("not supported by inbox yet"). Arcade sets are handled separately, below.

Configure it in the `[inbox]` table of the config (see [`examples/config.example.toml`](examples/config.example.toml)); with `--root` and `--dats` it needs no config file:

```toml
[inbox]
root = "/Volumes/hub"                       # the hub root as mounted here (holds inbox/ and library/)
dats_dir = "~/dats/libretro-database/metadat"  # redump/ and no-intro/ inside
chdman = "/opt/homebrew/bin/chdman"           # optional: otherwise looked up on PATH
rahasher = "~/tools/RAHasher"                 # optional: RetroAchievements hash check
mame_dat = "~/dats/mame0282lx.zip"            # optional: arcade (see "Arcade (MAME) sets")
fbneo_dat = "~/dats/fbneo-arcade.dat"
mame2003plus_dat = "~/dats/mame2003-plus.xml"
```

`bsdtar` and `dolphin_tool` work the same way, and `work_dir` moves the temporary files (they can take several GiB). Details in [`docs/design.md`](docs/design.md#inbox).

### Arcade (MAME) sets

An arcade `.zip` (or a folder of them) is identified by the size and CRC32 of its members, not unpacked, and filed as it is in `library/roms/mame/<set>.zip`. This needs at least one of three databases in XML, set in `[inbox]`; without any of them arcade zips take the usual path above and go to review ("no DAT match").

| Key | File | Used for |
|---|---|---|
| `mame_dat` | MAME's `-listxml`, or the official `mameXXXXlx.zip` of a [MAME release](https://github.com/mamedev/mame/releases) as it is (the zip holds the listxml) | the ES-DE default emulator, "MAME - Current" |
| `fbneo_dat` | `FinalBurn Neo (ClrMame Pro XML, Arcade only).dat` from the `dats/` folder of [FBNeo](https://github.com/libretro/FBNeo) | "FinalBurn Neo" |
| `mame2003plus_dat` | `mame2003-plus.xml` from the `metadata/` folder of [mame2003-plus-libretro](https://github.com/libretro/mame2003-plus-libretro) | "MAME 2003-Plus" |

Use the MAME version your frontend's core runs. A set *fits* an emulator when every required ROM (not `nodump`, not `optional`) of the set, its parent, its BIOS and its devices is present, counting the BIOS and device zips already in `library/roms/mame/`. The first emulator that fits wins: MAME (the default, nothing is written), else FinalBurn Neo, else MAME 2003-Plus. Otherwise the zip goes to review, and the reason lists the exact files missing per emulator and the zip they belong to (`MAME 0.282: mslug: needs neogeo.zip (2 file(s): sp-s2.sp1 9036d879, ...)`).

The file keeps (or gets) the set's short name, `<set>.zip`, which MAME requires: a zip called anything else is renamed after the best-fitting database. Merged, split and non-merged sets are all accepted as long as the members match; a clone's zip is completed by its parent's zip already in the library. Nothing is ever overwritten: the same members are "already in the library", a different file under that name goes to review.

For an emulator other than the default, `--apply` writes the ES-DE per-game override into the hub's gamelist (`frontend/es-de/gamelists/mame/gamelist.xml`, or `gamelist_mame`):

```xml
<game>
	<path>./dino.zip</path>
	<altemulator>FinalBurn Neo</altemulator>
</game>
```

Only that game's entry is added, changed or (when the default now fits) stripped of its override; everything else in the file stays as it is. The file is written to `.part` and renamed, and `gamelist.xml.bak` keeps the file as it was before the run. The report mode prints what it would write. CHD disks of a set are listed as a note but not checked, and sets must be zips (`.7z` and `.rar` are not opened for arcade).

## Checking a machine

`warpzone doctor` is a checklist for the machine you run it on, in the spirit of `flutter doctor`. It prints one line per check (`[ok]`, `[! ]` warning, `[!!]` problem, `[--]` optional and not set up) and, for every problem, the exact fix for this platform (Homebrew on macOS, apt, dnf or pacman on Linux, a container on SteamOS). It never installs or changes anything, makes no network calls (the one probe is a request to your local Syncthing on `127.0.0.1`), and exits `1` only when something required is broken, so it can run in a script.

```
$ warpzone doctor
Inbox tools
  [ok] chdman       chdman - MAME Compressed Hunks of Data (CHD) manager 0.289 (unknown) (/opt/homebrew/bin/chdman)
  [!!] bsdtar       not found (required by inbox)
                    fix: sudo apt install libarchive-tools
  [--] dolphin-tool not found (only for compressed GameCube images (.rvz))
                    fix: install Dolphin (brew install --cask dolphin); ...
...
1 problem(s), 0 warning(s), 4 optional item(s) not set up. Fix the [!!] lines above.
```

It covers warpzone and Python, the config file (and what each table enables), the local Syncthing and its REST API, the inbox tools (`chdman`, `bsdtar`, `RAHasher`, `dolphin-tool`), the DAT folder and how old the newest DAT is, the optional arcade DAT files, the hub folder and its free space, and whether `RA_API_KEY` is set (never printed). A tool or table you do not use is shown as optional, not as a problem: for example, a desktop that only runs `inbox` does not need Syncthing. Use `--config PATH` for a config in another place.

## The EmuDeck symlink gotcha

EmuDeck exposes saves through symlinks under `~/Emulation/saves`. Syncthing never follows symlinks, so a folder pointed at the symlink syncs nothing and gives no error. The `steamos-emudeck` profile therefore points at the real directory of each emulator (for example RetroArch's saves under `~/.var/app/org.libretro.RetroArch/`). If you add a profile for another setup, resolve the links first (`readlink -f`).

Some save folders sit inside `~/Emulation`, which is the library root. That is fine: the library whitelist ignores them, and Syncthing supports nested folders when the parent ignores the child.

## Safety

- **Receive-only devices.** The library, songs and tools are receive-only on devices, so a delete or an edit on a console can never propagate to the hub.
- **Path changes are refused.** If a `wz-*` folder already exists in Syncthing with a different path than the config says, `warpzone` stops: changing a folder's path makes Syncthing see a mass delete. Remove the folder in Syncthing first (files stay on disk), then provision again.
- **Additive only.** Devices are only ever added, never removed. Folders that are not prefixed `wz-` are never touched. Existing `wz-*` folders are patched only for the keys `warpzone` owns (label, path, type, devices, versioning, file watcher, and the rescan interval of whitelisted device folders); everything else you tuned is left alone.
- **Pruning is opt-in.** `warpzone prune` reports by default; `--apply` deletes only files Syncthing ignores and the hub still has, within a size cap.
- **Dry run.** Every writing command supports `--dry-run`: it reads state, prints the calls it would make (method, path, JSON body) and sends none.
- **Secrets.** API keys are never printed, logged or included in error messages, and are redacted from dry-run output.
- **Where it runs.** `provision device` talks to the local Syncthing on loopback (self-signed HTTPS is accepted only there) and reads the hub's API only to check that the whitelists exist before it changes anything. If the hub is unreachable it warns and carries on, because the bootstrap ignores keep it safe.

### Snapshots on the hub

The hub is the authority, and `warpzone` keeps no versions of the library (saves get 30 days of staggered history). Turn on filesystem snapshots for the hub's games share (Btrfs on Synology, ZFS, or whatever your NAS offers). A bad edit or a deleted folder on the hub then costs one rollback instead of a re-download.

## Profiles

A profile says where things live on one kind of device: [`warpzone/profiles/`](warpzone/profiles). v0.1 ships `steamos-emudeck` (Steam Deck, Steam Machine). Profiles are plain TOML so adding a setup is a small pull request. A save entry like `[saves.dolphin-gc]` maps to the folder ID `wz-save-dolphin-gc` and the hub path `saves/dolphin/gc`.

## Roadmap

Not in v0.1:

- Home Assistant: Syncthing folder sensors and script notifications.
- Switch save migration between emulators (one-shot, with backup).
- More profiles (macOS, Windows, Batocera).

## Legal

`warpzone` ships and locates no ROMs, BIOS files, keys or firmware, and never will. `warpzone bios` and `warpzone inbox` only compare what you already have with public file names, sizes and hashes. It only moves files you already have between machines you own. Use your own legally obtained dumps.

## License

[MIT](LICENSE)
