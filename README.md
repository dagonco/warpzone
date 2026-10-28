# warpzone

[![ci](https://github.com/dagonco/warpzone/actions/workflows/ci.yml/badge.svg)](https://github.com/dagonco/warpzone/actions/workflows/ci.yml)
[![release](https://img.shields.io/github/v/release/dagonco/warpzone)](https://github.com/dagonco/warpzone/releases/latest)

Keep one game library on a home server and deliver it to every console you own, with saves that follow you from one device to the next.

You have a NAS full of ROMs, a Steam Deck, maybe a Steam Machine, maybe a spare Mac. Each one has its own copy, its own idea of which games it wants, and its own saves. `warpzone` is a thin layer of conventions and automation on top of [Syncthing](https://syncthing.net) that makes the server the single source of truth. It never moves a byte of game data itself: it only configures Syncthing and keeps each device's setup honest.

## The problem

- Copying a 2 TB library to a 512 GB device means picking games by hand, every time.
- Editing a "what do I want here" list on each device, over SSH or with a gamepad, does not scale past one device.
- Saves live in a dozen emulator-specific places, and syncing them naively syncs the wrong things.
- One accidental delete on a console should never reach the server.

`warpzone` fixes this with three rules: the hub owns the library, devices only receive it, and each device chooses what it gets with a whitelist that is **edited only on the hub**.

## How it works

```
                       hub (NAS)
         ┌───────────────────────────────────┐
         │ library/   roms, bios, extras     │ ──► whitelisted, receive-only
         │ songs/     Clone Hero, YARG       │ ──► whitelisted, receive-only
         │ tools/     scripts, docs          │ ──► receive-only
         │ saves/     per emulator           │ ◄─► two-way, 30 days of history
         │ frontend/  ES-DE media, gamelists │ ◄─► two-way
         │ inbox/     new dumps (not synced) │
         └───────────────────────────────────┘
                 │ Syncthing           │
                 ▼                     ▼
          Steam Deck            Steam Machine  ...
          (patterns-deck)       (patterns-machine)
```

Each `library/` and `songs/` folder holds one `patterns-<device>.stignore` per device. A device applies only its own, so the same folder gives each console a different slice of the library. Saves get their own folders so their two-way direction can never leak into the library. Keys and firmware that EmuDeck symlinks get their own receive-only folders (see [docs/emudeck.md](docs/emudeck.md)). `songs` is not emulation (Clone Hero, YARG) and lands outside `~/Emulation`.

### Hub layout

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

## Requirements

- Syncthing v2.x on the hub and on every device (tested with v2.1.7).
- Python 3.11+ on the machine that runs `warpzone`. Standard library only: it runs unchanged on SteamOS, macOS and any Linux box.
- The hub is driven remotely through its Syncthing REST API, so it does not need Python (many NAS ship an old one). Enable the GUI/API on a LAN address of the hub.

## Install

From the [v0.1.0 release](https://github.com/dagonco/warpzone/releases/tag/v0.1.0):

```bash
pipx install https://github.com/dagonco/warpzone/releases/download/v0.1.0/warpzone-0.1.0-py3-none-any.whl
# or, without pipx:
python3 -m pip install --user https://github.com/dagonco/warpzone/releases/download/v0.1.0/warpzone-0.1.0-py3-none-any.whl
warpzone --version
```

Each release also lists a `SHA256SUMS` file and carries a build provenance attestation (`gh attestation verify <file> --repo dagonco/warpzone`).

From a clone, with nothing to install:

```bash
git clone https://github.com/dagonco/warpzone.git && cd warpzone
python3 -m warpzone --help
```

## Quickstart

1. **Write the private config.** Copy [`examples/config.example.toml`](examples/config.example.toml) to `~/.config/warpzone/config.toml`. Fill in the device IDs (Syncthing: Actions > Show ID) and the hub's API key (Actions > Settings > GUI). This file is the only place with personal data; keep the key in a file with `chmod 600`. The same file works on every machine.
2. **Put the whitelists on the hub.** In the hub's `library/` folder, copy [`templates/patterns-common.stignore`](templates/patterns-common.stignore) and make one `patterns-<device>.stignore` per device from [`templates/patterns-device.example.stignore`](templates/patterns-device.example.stignore). Do the same in `songs/` for devices that subscribe to it. See [docs/whitelists.md](docs/whitelists.md).
3. **Check, then configure the hub.**

   ```bash
   warpzone check
   warpzone provision hub --dry-run    # prints the REST calls it would make, makes none
   warpzone provision hub
   ```

   This adds every configured device and creates the `wz-*` folders on the hub. It is idempotent: run it again whenever you add a device.
4. **Provision each device, on the device.**

   ```bash
   warpzone provision device deck
   ```

   It finds the local Syncthing's API key in its `config.xml` (including the SyncThingy flatpak), adds the hub, and creates the subscribed folders. Save folders whose directory does not exist yet are skipped with a warning and picked up by the next run. Use `--syncthing-config PATH` if your `config.xml` is somewhere unusual.

## Commands

| Command | What it does |
|---|---|
| `warpzone check` | Validates the config and lists missing whitelists. |
| `warpzone provision hub` | Sets up the hub's Syncthing through its REST API. Once, then whenever you add a device. |
| `warpzone provision device <name>` | On a console: adds the hub and creates the subscribed folders. |
| `warpzone status` | Folder state, outstanding items and receive-only local changes. |
| `warpzone render-ignores <name>` | Prints the ignore lines a device folder would get. |
| `warpzone prune` | Reports (or, with `--apply`, deletes) files a device no longer whitelists. |
| `warpzone bios` | Which BIOS and firmware files the library has, lacks or has wrong. |
| `warpzone inbox` | Reports (or, with `--apply`, files) new games dropped in `inbox/`. |
| `warpzone doctor` | Checks this machine and prints the fix for each problem. |

`provision` supports `--dry-run`. `prune` and `inbox` report first and act only with `--apply`.

## Safety

- Library, songs and tools are **receive-only** on devices: a delete or edit on a console never reaches the hub.
- Only `wz-*` folders are touched, devices are only added, and a changed folder path is refused.
- `prune` and `inbox` report by default. `inbox` never overwrites, and `prune` deletes only files the hub still has unchanged.
- API keys are never printed, logged or put in error messages.

Turn on filesystem snapshots for the hub's share. The full list of rules is in [docs/safety.md](docs/safety.md).

## Documentation

| Guide | Covers |
|---|---|
| [docs/whitelists.md](docs/whitelists.md) | How whitelists work, rescan, bootstrap, pruning. |
| [docs/inbox.md](docs/inbox.md) | Adding games: DAT-based discs and cartridges, Switch, PS3 and Xbox, arcade. |
| [docs/bios.md](docs/bios.md) | The BIOS and firmware check. |
| [docs/doctor.md](docs/doctor.md) | Checking a machine. |
| [docs/emudeck.md](docs/emudeck.md) | The EmuDeck profile, the symlink gotcha, manual steps. |
| [docs/safety.md](docs/safety.md) | Safety rules and hub snapshots. |
| [docs/design.md](docs/design.md) | The implementation contract. |
| [CONTRIBUTING.md](CONTRIBUTING.md), [docs/releasing.md](docs/releasing.md), [CHANGELOG.md](CHANGELOG.md) | Contributing, releasing, history. |

## Legal

`warpzone` ships and locates no ROMs, BIOS files, keys or firmware, and never will. `warpzone bios` and `warpzone inbox` only compare what you already have with public file names, sizes and hashes. It only moves files you already have between machines you own. Use your own legally obtained dumps.

## License

[MIT](LICENSE)
