---
title: "Quick start"
description: "From nothing to a first console receiving games, in a few steps."
---

From nothing to a first console receiving games, in a few steps.

You end up with a hub that offers its library to one console, and that console receiving only the games its whitelist allows. Plan on a few steps and some waiting for the first sync. Adding more consoles repeats steps 4 and 5.

## Before you start

- [Syncthing](https://syncthing.net) v2.x runs on the hub and on the console (tested with v2.1.7).
- The hub's Syncthing GUI and API listen on a LAN address, because `warpzone` drives the hub through its REST API.
- The console has Python 3.11 or newer and git.
- You know the hub's device ID and API key, and the console's device ID. In the Syncthing GUI: Actions > Show ID, and Actions > Settings > GUI for the key.

The [installation page](/warpzone/overview/installation/) has the details. The console is called `deck` below; use the name you choose in the config.

## 1. Get warpzone on the machine that drives the hub

Any machine that can reach the hub's API will do. From a clone, with nothing to install:

```bash
git clone https://github.com/dagonco/warpzone.git && cd warpzone
python3 -m warpzone --help
```

Commands below say `warpzone`; from the clone type `python3 -m warpzone` instead.

## 2. Write the private config

Copy [`examples/config.example.toml`](https://github.com/dagonco/warpzone/blob/main/examples/config.example.toml) to `~/.config/warpzone/config.toml` and fill in:

- `[hub]`: `name`, `device_id`, `root` (the hub root as the hub's own filesystem sees it), `api_url` and the API key (`api_key_file` is preferred; keep the file `chmod 600`).
- `[devices.deck]`: the console's `device_id`, a `profile` (`steamos-emudeck` for a Steam Deck or Steam Machine, `anbernic-rg351mp` for the Anbernic) and the `folders` it subscribes to.

```toml
[devices.deck]
device_id = "BBBBBBB-BBBBBBB-BBBBBBB-BBBBBBB-BBBBBBB-BBBBBBB-BBBBBBB-BBBBBBB"
profile = "steamos-emudeck"
folders = ["library", "tools", "saves", "frontend", "bios", "settings"]
```

This file is the only place with personal data. The same file works on every machine, so you copy it to the console later. Every key is described in [Configuration](/warpzone/reference/configuration/).

## 3. Put the whitelists on the hub

In the hub's `library/` folder, copy [`templates/patterns-common.stignore`](https://github.com/dagonco/warpzone/blob/main/templates/patterns-common.stignore) and make one `patterns-<device>.stignore` per device from [`templates/patterns-device.example.stignore`](https://github.com/dagonco/warpzone/blob/main/templates/patterns-device.example.stignore). For the console above that is `patterns-deck.stignore`:

```
#include patterns-common.stignore
!/roms/nes
!/roms/snes
!/bios
*
```

Do the same in `songs/` for devices that subscribe to it. How patterns are matched and when an edit takes effect is in [Whitelists](/warpzone/setup/whitelists/).

## 4. Check, then configure the hub

```bash
warpzone check
warpzone provision hub --dry-run    # prints the REST calls it would make, makes none
warpzone provision hub
```

`check` validates the config and lists whitelists that are missing on the hub. `provision hub` adds every configured device and creates the `wz-*` folders on the hub. It is idempotent: run it again whenever you add a device.

## 5. Install and provision the console

On the console, put the config at `~/.config/warpzone/config.toml`, then install from a clone with the updater script:

```bash
mkdir -p ~/.local/share/warpzone && cd ~/.local/share/warpzone
git clone https://github.com/dagonco/warpzone.git src
sh src/warpzone/data/updater.sh install deck
~/.local/share/warpzone/venv/bin/warpzone --version
```

> Until 0.3.0 is released, install with `--channel rc`: the newest stable release, v0.2.0, has no `updater.sh`. For example `sh src/warpzone/data/updater.sh install deck --channel rc`.

`install` checks out the newest release, makes a venv next to the clone and runs `warpzone provision device deck`. That step finds the local Syncthing's API key, adds the hub, creates the subscribed folders and installs the updater and its timer. Folders whose emulator is not installed yet are skipped with a warning and picked up by the next run. See [Installation](/warpzone/overview/installation/) and [Updates](/warpzone/operation/self-update/).

## 6. Check that it works

On the console:

```bash
warpzone status     # folder state and outstanding items
warpzone doctor     # checks this machine and prints the fix for each problem
```

Within a few minutes the whitelisted games start to arrive. After that, edit `patterns-deck.stignore` on the hub to change what the console gets. A whitelist edit takes effect within 5 minutes.

## Next

- Adding games: drop dumps in the hub's `inbox/` and run `warpzone inbox` ([Adding games](/warpzone/setup/inbox/)).
- BIOS and firmware: [BIOS](/warpzone/setup/bios/).
- Console details: [Steam Deck and EmuDeck](/warpzone/guides/emudeck/), [Steam Machine](/warpzone/guides/steam-machine/), [Anbernic](/warpzone/guides/anbernic/).
- Another kind of device: [Adding a new console](/warpzone/guides/new-console/).
- The terms used above: [Concepts](/warpzone/overview/concepts/).
