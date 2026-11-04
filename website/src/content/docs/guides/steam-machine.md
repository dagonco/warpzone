---
title: "Steam Machine"
description: "Setting up a Steam Machine as a warpzone device."
---

Setting up a Steam Machine as a warpzone device.

A Steam Machine runs SteamOS with EmuDeck, the same stack as a Steam Deck, so it uses the same `steamos-emudeck` profile. There is nothing machine-specific in warpzone: the profile already says where each emulator keeps its saves, which library paths the device ignores and where keys and firmware land. What differs is your own choices: the device name, its ID, its whitelist, and usually a subscription to `songs`.

This page is the short path. The profile's details (what is covered, the symlink gotcha, the manual steps) are in [Steam Deck and EmuDeck](/warpzone/guides/emudeck/) and apply unchanged.

## Before you start

- EmuDeck is installed and the emulators you use have been opened once. A save folder whose directory does not exist yet is skipped with a warning and picked up by the next `provision device`.
- Syncthing v2.x runs on the machine. On SteamOS it is usually the SyncThingy flatpak, started from a systemd user unit so it also runs in Game Mode ([Syncthing on the console](/warpzone/guides/emudeck/#syncthing-on-the-console)).
- Python 3.11 or newer and git are installed.
- The hub is already provisioned for your other devices ([Quick start](/warpzone/overview/quick-start/)).

## 1. Add the device to the config

Add a table to the private config (on the admin machine and, later, on the Steam Machine itself). Use the device ID from Syncthing (Actions > Show ID) on the Steam Machine:

```toml
[devices.machine]
device_id = "CCCCCCC-CCCCCCC-CCCCCCC-CCCCCCC-CCCCCCC-CCCCCCC-CCCCCCC-CCCCCCC"
profile = "steamos-emudeck"
folders = ["library", "tools", "saves", "frontend", "songs", "bios", "settings"]
```

Subscribe to what you want:

| Folder group | What the Steam Machine gets |
|---|---|
| `library` | ROMs, BIOS and extras, limited by its whitelist. |
| `tools` | The hub's scripts and docs. |
| `saves` | Every save folder of the profile, two-way. |
| `frontend` | ES-DE media, gamelists and collections, two-way. |
| `songs` | Clone Hero and YARG songs into `~/Songs`, limited by its whitelist. |
| `bios` | Keys and firmware delivered into each emulator's own folder (see [BIOS placements](/warpzone/guides/emudeck/#bios-placements)). |
| `settings` | Emulator settings layers ([Emulator settings](/warpzone/setup/settings/)). |

`play-state` is also available: it shares favourites and play statistics with your other frontends and needs `frontend`.

## 2. Write its whitelists on the hub

Make `patterns-machine.stignore` in the hub's `library/` folder, from [`templates/patterns-device.example.stignore`](https://github.com/dagonco/warpzone/blob/main/templates/patterns-device.example.stignore). The device name must match `[devices.machine]`. Whitelist what fits its storage and what you will play; it can take different systems than your other devices:

```
#include patterns-common.stignore
!/roms/gba
!/roms/psx
!/roms/switch
!/roms/xbox360/roms
!/bios
!/extras/switch/updates
!/extras/switch/dlc
*
```

Name `xbox360/roms` and `model2/roms`, not the whole system, so Xenia's and Model 2's own files stay on the device ([the EmuDeck guide](/warpzone/guides/emudeck/#xenia-and-model-2-live-inside-roms)). Because `songs` is a whitelisted folder too, make `patterns-machine.stignore` in the hub's `songs/` folder as well, from the same template. See [Whitelists](/warpzone/setup/whitelists/).

## 3. Configure the hub and install on the machine

On the admin machine:

```bash
warpzone check
warpzone provision hub --dry-run
warpzone provision hub
```

Then, on the Steam Machine, with the config in `~/.config/warpzone/config.toml`:

```bash
mkdir -p ~/.local/share/warpzone && cd ~/.local/share/warpzone
git clone https://github.com/dagonco/warpzone.git src
sh src/warpzone/data/updater.sh install machine
```

`machine` is the name in `[devices.machine]`. The script installs the newest release and runs `warpzone provision device machine`, which adds the hub, creates the subscribed folders, grants the SyncThingy flatpak access to the emulators' `~/.var/app` folders (it then asks you to restart Syncthing), installs the ES-DE startup scripts and installs the updater timer. Restart Syncthing when it says so, then run `provision device` again if some folders were skipped. See [Installation](/warpzone/overview/installation/) and [Updates](/warpzone/operation/self-update/).

## 4. Songs

The `songs` group puts the hub's `songs/` folder, filtered by `songs/patterns-machine.stignore`, in `~/Songs`. warpzone does not configure the games: add `~/Songs` as a song folder in Clone Hero or YARG yourself.

YARG's own data (settings, scores, profiles, playlists and `custom/`) is a save folder, `yarg-data`, under `~/.config/unity3d/YARC/YARG/release`, synced two-way through the `saves` group. The song cache and the list of bad songs never sync, because they hold the absolute paths of one machine. `scores/` is a SQLite database: close the game on one console before playing on another.

## 5. Sharing with a Steam Deck

The Steam Deck and the Steam Machine use the same profile, so they share the same hub folders:

- **Saves** are the same folders: a save made on one is there on the other once Syncthing has synced.
- **ES-DE data** (`wz-esde`, `wz-media`) is one shared set of gamelists and media. When both devices change the same gamelist while offline, the last write wins, so a favourite can look unset on the other device if an older file wins. warpzone counts plays per gamelist, so a play on one device is not counted twice on the other.
- **Settings** come from `settings/common.toml` for both, and `settings/machine.toml` holds keys only for this device. Capture on whichever console is configured the way you like ([Emulator settings](/warpzone/setup/settings/)).
- **Whitelists** are independent: each device has its own `patterns-<device>.stignore`.

## 6. Check it

```bash
warpzone status
warpzone doctor
warpzone health machine
```

`doctor` checks Syncthing in Game Mode, the flatpak overrides, the updater timer and the emulator-specific items, and prints the fix for each problem. After that, change what the machine receives by editing its whitelist on the hub; an edit takes effect within 5 minutes.

## Manual steps

A few things stay manual, as on a Steam Deck: Ryujinx keys and firmware, and RPCS3 firmware. They are listed under [Manual steps](/warpzone/guides/emudeck/#manual-steps) in the EmuDeck guide.
