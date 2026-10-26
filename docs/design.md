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

- Moving, renaming, verifying or converting ROMs (that is igir's job, see the roadmap).
- Shipping or locating any copyrighted content (ROMs, BIOS, keys, firmware).
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
```

API keys are never printed, logged or included in error messages.

## Command line

`python3 -m warpzone <command>`; also installable as a `warpzone` script.

| Command | Runs on | Does |
|---|---|---|
| `warpzone check` | anywhere | Validates config + profiles, reports which whitelists are missing on the hub (via `GET /rest/db/browse`), never writes. Exit 1 if any are missing; `--offline` skips the hub. |
| `warpzone provision hub` | anywhere that reaches the hub API | Adds every configured device to the hub, creates/updates every `wz-*` folder on the hub and shares it with the devices that subscribe to it, sets the hub ignores. Idempotent. |
| `warpzone provision device <name>` | on the device | Reads the local Syncthing API key from the local `config.xml` (searching the default locations, including the SyncThingy flatpak; `--syncthing-config PATH` overrides), adds the hub device, creates the subscribed folders with the bootstrap above (`--timeout SECONDS`). Idempotent. |
| `warpzone status` | anywhere | Folder state, need counts, errors, receive-only local changes, for the hub (and the local device if any). |
| `warpzone render-ignores <device>` | anywhere | Prints the ignore lines a device folder would get. For debugging. |

Every writing command supports `--dry-run`, which prints the REST calls it
would make (method, path, JSON body with secrets redacted) and sends no write.
It still sends the GET requests it needs to compute the diff. Every command
takes `--config PATH` (default `$WARPZONE_CONFIG`, then the XDG path).

Exit codes: `0` ok, `1` runtime error, `2` usage or config error.

Idempotency rules for `provision`:

- Existing `wz-*` folders are updated in place with `PATCH`, only for the keys
  warpzone owns (`label`, `path`, `type`, `devices`, `versioning`,
  `fsWatcherEnabled`). Unknown keys are left alone. `devices` is merged: devices
  already on the folder are kept, missing ones are added.
- Folders not prefixed `wz-` are never touched.
- Changing a folder's `path` on an existing folder is refused (it would make
  Syncthing see a mass delete); the user is told to remove the folder first.
- Devices are only ever added, never removed, by warpzone.

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
  localst.py         find and parse the local Syncthing config.xml
  profiles/steamos-emudeck.toml
templates/
  patterns-common.stignore
  patterns-device.example.stignore
examples/config.example.toml
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
  device reaches the hub.

## CI (GitHub Actions)

- `lint`: ruff check + ruff format --check.
- `test`: unittest on Python 3.11, 3.12, 3.13, 3.14, ubuntu-latest and macos-latest.
- `e2e`: ubuntu-latest, downloads a pinned Syncthing release, verifies its
  SHA-256 against a value committed in the workflow, runs the e2e test.
- `secrets`: gitleaks over the full history.
- Every third-party action pinned by commit SHA, `permissions: contents: read`
  at workflow level, Dependabot for GitHub Actions.

## Roadmap (not v0.1)

- `warpzone prune`: remove files a device no longer whitelists, report-only by default.
- `warpzone inbox`: verify, rename (No-Intro/Redump via igir) and file new content.
- BIOS manifest with hashes and a `warpzone bios` checker.
- Home Assistant: Syncthing folder sensors, script notifications.
- Switch save migration between emulators (one-shot, with backup).
- More profiles (macOS, Windows, Batocera).
