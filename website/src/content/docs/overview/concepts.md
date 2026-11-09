---
title: "Concepts: hub and devices"
description: "The hub owns the library, devices receive it, and whitelists choose what each device gets."
---

The hub owns the library, devices receive it, and whitelists choose what each device gets.

A handful of ideas explain everything warpzone does. This page defines them and shows how they fit together.

## The hub

The hub is the machine that holds the one authoritative library, usually a NAS. It runs Syncthing, and warpzone drives it remotely through its REST API. Everything lives under one **hub root** (`[hub].root` in the config):

```
<hub root>/
├── library/     roms/<system>/, bios/, extras/, and the whitelists
├── saves/       <emulator>/<kind>/, one folder per kind of save
├── frontend/    media/, es-de/, play-state/
├── settings/    emulator settings layers
├── songs/       Clone Hero and YARG songs
├── tools/       scripts and docs
└── inbox/       never synced: the drop zone for new content
```

The hub owns the library. Nothing a device does can change it: on devices the library is receive-only.

## Devices

A device is a console, handheld or PC that receives from the hub. Each one is a table in the private config, named by you:

```toml
[devices.deck]
device_id = "BBBBBBB-BBBBBBB-BBBBBBB-BBBBBBB-BBBBBBB-BBBBBBB-BBBBBBB-BBBBBBB"
profile = "steamos-emudeck"
folders = ["library", "tools", "saves", "frontend", "bios", "settings"]
```

- `device_id` is the Syncthing ID of that device.
- `profile` says where things live on that kind of device (see below).
- `folders` lists the groups it subscribes to: `library`, `songs`, `tools`, `saves`, `frontend`, `bios`, `settings` and `play-state`.

Devices are only ever added to the hub's Syncthing, never removed, and warpzone only touches folders whose ID starts with `wz-`.

## Profiles

A profile is a TOML file shipped with warpzone that describes one kind of device: the library path, the real directory of each emulator's saves, where BIOS files must land, which library paths the device must never receive. warpzone ships `steamos-emudeck` (Steam Deck and Steam Machine) and `anbernic-rg351mp`. Profiles are data, so supporting another setup is a small pull request ([Adding a new console](/warpzone/guides/new-console/)).

## Syncthing folders

Everything warpzone manages is a Syncthing folder with a stable ID prefixed `wz-`:

| Folder | Hub path | On devices | What it carries |
|---|---|---|---|
| `wz-library` | `library` | receive-only, whitelisted | ROMs, BIOS, extras |
| `wz-songs` | `songs` | receive-only, whitelisted | Clone Hero and YARG songs |
| `wz-tools` | `tools` | receive-only | scripts and docs |
| `wz-save-<emu>-<kind>` | `saves/<emu>/<kind>` | two-way | one emulator's saves or states |
| `wz-bios-<emu>-<kind>` | `library/<path>` | receive-only | keys and firmware delivered into an emulator's own folder |
| `wz-media`, `wz-esde` | `frontend/media`, `frontend/es-de` | two-way (receive-only for a profile with `[artwork]`, such as the Anbernic) | ES-DE media (not the `videos`, which stay on the hub), gamelists and collections |
| `wz-play-state` | `frontend/play-state` | two-way | favourites and play statistics, one file per device, and the console's disk space for the library UI |
| `wz-settings` | `settings` | two-way | emulator settings layers |

The split is deliberate. The library is receive-only so a delete on a console never reaches the hub. Saves need two-way sync, so each kind has its own folder and that direction cannot leak into the library. The hub keeps 30 days of history of the save folders, `wz-settings` and `wz-play-state`, and nothing else is versioned, so turn on filesystem snapshots for the hub's share ([Safety](/warpzone/operation/safety/)). The full table is in [the design document](/warpzone/development/design/hub-and-folders/#folders).

## Whitelists

A device chooses what it receives with a whitelist: an ordinary Syncthing ignore file named `patterns-<device>.stignore`, stored inside `library/` (and `songs/`) **on the hub**. It syncs to every device like any other file, and each device applies only its own. A whitelist includes the shared `patterns-common.stignore`, lists what the device wants with `!` lines, and ends with `*`:

```
#include patterns-common.stignore
!/roms/nes
!/roms/psx/Some Game (USA)
!/bios
*
```

The device's own `.stignore` is one line, `#include patterns-<device>.stignore`, and never changes after provisioning. You change what a device gets by editing its whitelist on the hub, never on the device. An edit takes effect within 5 minutes. Details, the bootstrap that keeps a new device from pulling the whole library, and cleaning up with `warpzone prune` are in [Whitelists](/warpzone/setup/whitelists/).

## Saves

Each emulator's saves and states are mapped to their own two-way folder. The profile names the real directory of each one, because Syncthing never follows symlinks and EmuDeck exposes saves through them. A profile key such as `[saves.dolphin-gc]` becomes the folder `wz-save-dolphin-gc` and the hub path `saves/dolphin/gc`. A save folder whose directory does not exist yet (the emulator was never opened) is skipped with a warning and picked up by the next `provision device`. See [Steam Deck and EmuDeck](/warpzone/guides/emudeck/).

## BIOS placements

Some emulators read keys and firmware from folders that EmuDeck turns into symlinks, which the library cannot fill. A profile therefore lists **BIOS placements**: receive-only folders that carry a hub folder under `library/bios/` into the emulator's real directory. A device opts in with the `bios` group. `warpzone bios` checks which BIOS and firmware files you have, lack or have wrong, against public names, sizes and hashes ([BIOS](/warpzone/setup/bios/)).

## The inbox

`inbox/` on the hub is the drop zone for new dumps. It is never synced. You drop files, folders or archives there and run `warpzone inbox` on a desktop that mounts the hub's share. It identifies each item against Redump and No-Intro DATs, converts what needs converting, and files it under `library/roms/<system>/`. It reports first and acts only with `--apply`, and it never overwrites. It also prints the whitelist line to add for each game ([Adding games](/warpzone/setup/inbox/)).

## The settings layer

Emulator settings (language, region, graphics) can follow you too. The hub holds **layers of single keys** in `settings/`: `common.toml` for keys every device gets and `<device>.toml` for one device, which wins over common. `warpzone settings capture` takes a change made in an emulator to the hub, and `warpzone settings apply` writes the layers on another console (ES-DE runs it at startup). Whole config files are never shared, because they also hold controller mappings, GPU ids and absolute paths. See [Emulator settings](/warpzone/setup/settings/).

## The private config

One file, `~/.config/warpzone/config.toml`, holds everything personal: the hub's address and API key, and the device IDs. It is the only place with personal data, it is never committed, and the same file works on every machine. See [Configuration](/warpzone/reference/configuration/).
