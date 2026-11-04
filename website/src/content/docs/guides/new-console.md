---
title: "Adding a new console"
description: "How to describe a new kind of device with a profile."
---

How to describe a new kind of device with a profile.

Adding a device takes up to four pieces: a table in the config, a profile if it is a new kind of device, a whitelist on the hub, and `provision`. This page walks through them in order. Replace `<device>` with the name you pick.

## Is it a new kind of device?

A profile describes one kind of device, not one device. A second Steam Deck, or a Steam Machine, uses the existing `steamos-emudeck` profile ([Steam Deck and EmuDeck](/warpzone/guides/emudeck/), [Steam Machine](/warpzone/guides/steam-machine/)), and a second Anbernic RG351MP uses `anbernic-rg351mp` ([Anbernic](/warpzone/guides/anbernic/)). In that case skip step 3.

A different system (another handheld OS, a PC with its own emulators) needs a profile of its own.

## 1. Gather the facts

Before you write anything, find out on the device:

- Where the library should live, and whether that filesystem has permissions and symlinks. An exFAT card has neither, which a profile declares with `ignore_perms = true`.
- The real directory of each emulator's saves and states. Syncthing never follows symlinks, so resolve every link first with `readlink -f <path>`.
- Where emulators read keys and firmware from, and whether those folders are symlinks into the library.
- Whether Syncthing runs on the device and how (a package, a service, a flatpak).

## 2. Add the device to the config

Install Syncthing on the device and note its ID (Actions > Show ID). Then add a table to the private config:

```toml
[devices.<device>]
device_id = "EEEEEEE-EEEEEEE-EEEEEEE-EEEEEEE-EEEEEEE-EEEEEEE-EEEEEEE-EEEEEEE"
profile = "my-handheld"
folders = ["library", "saves"]
```

| Key | Meaning |
|---|---|
| `device_id` | The device's Syncthing ID. It must be different for each device. |
| `profile` | A profile name; an unknown one is rejected and the available ones are listed. |
| `folders` | Groups to subscribe to, any of `library`, `songs`, `tools`, `saves`, `frontend`, `bios`, `settings` and `play-state`. |
| `addresses` | Optional. Where the hub finds the device. Default `["dynamic"]`. |
| `rescan_seconds` | Optional. How often whitelisted folders rescan. Default 300, minimum 5. |
| `self_update`, `channel`, `pin` | Optional. The updater ([Updates](/warpzone/operation/self-update/)). |
| `auto_hub` | Optional, default `false`. Let `provision device` create on the hub the folders the device needs and the hub lacks (never a whitelisted one). |

The device name is lowercase letters, digits, `-` or `_`. A device can only subscribe to a group its profile has a place for: `library`, `songs`, `tools` and `settings` need the matching key under `[paths]`, and `frontend` needs `paths.media` and `paths.esde` or an `[artwork]` table. Otherwise warpzone refuses with an error naming the missing key. Every key is described in [Configuration](/warpzone/reference/configuration/).

## 3. Write a profile

A profile is a TOML file at `warpzone/data/profiles/<name>.toml`. The file name must match its `name` key. The smallest useful one gives a library path, the save folders and nothing else:

```toml
name = "my-handheld"
description = "A Linux handheld with RetroArch and ROMs on an SD card"

[paths]
library = "/mnt/sdcard/games"

# Paths in the library the device never receives, whatever its whitelist says.
[library]
ignore = ["(?d)/roms/*/gamelist.xml"]

# One entry per save folder. `path` is the REAL directory, never a symlink.
[saves.retroarch-saves]
path = "~/.config/retroarch/saves"
[saves.retroarch-states]
path = "~/.config/retroarch/states"
create_when = "~/.config/retroarch"

# A hub folder under library/ delivered into an emulator's own folder.
[bios.myemu-keys]
hub = "bios/myemu"
path = "~/.local/share/myemu"
ignore = ["!/keys.txt", "*"]
```

What each part means:

- `[paths]` holds `library`, `songs`, `tools`, `media`, `esde`, `settings` and `play_state`. Only the ones a device uses are needed. `~` is expanded on the device.
- `[saves.<emu>-<kind>]` has a `path`, optionally an `ignore` list (a whitelist that ends in `*`) and `create_when`. The key becomes the folder `wz-save-<emu>-<kind>` and the hub path `saves/<emu>/<kind>`, so both halves are single lowercase words. A path that does not exist yet is skipped with a warning, unless `create_when` names a directory that exists.
- `[library] ignore` lists paths below the library that the device never receives. They go ahead of the whitelist so an edit cannot bring them back. Start a line with `(?d)` to let Syncthing delete a folder the hub removed even when an ignored file is inside it.
- `[bios.<emu>-<kind>]` is a BIOS placement: a receive-only folder from the hub's `library/<hub>` to the emulator's real directory, with its own `ignore` list. The library ignores that hub folder automatically.
- `ignore_perms = true` at the top level is for a library on a filesystem without POSIX permissions.

Profiles can also describe settings files, a frontend's system list, artwork conversion, play state, shadPS4 and title links; the shipped profiles are the reference. The format is validated when it loads and an error names the key at fault ([the design document](/warpzone/development/design/#profiles)).

Profiles are read from the package's data folder, so a profile that is not part of the release a device runs is not found there. Contribute it as a pull request with a test that loads it ([CONTRIBUTING](https://github.com/dagonco/warpzone/blob/main/CONTRIBUTING.md)). Never put personal data in a profile: it is public.

## 4. Write the whitelist

In the hub's `library/` folder, make `patterns-<device>.stignore` from [`templates/patterns-device.example.stignore`](https://github.com/dagonco/warpzone/blob/main/templates/patterns-device.example.stignore), and do the same in `songs/` if the device subscribes to it. The name must match `[devices.<device>]`:

```
#include patterns-common.stignore
!/roms/gba
!/bios
*
```

Without this file `provision device` stops before it touches anything, so a device can never pull the whole library. See [Whitelists](/warpzone/setup/whitelists/).

## 5. Provision

From the admin machine:

```bash
warpzone check
warpzone render-ignores <device>
warpzone provision hub --dry-run
warpzone provision hub
```

`check` validates the config and the profile and lists missing whitelists. `render-ignores` prints the ignore lines the device's folders would get, which is a quick way to see what a profile change does. `provision hub` adds the device and creates its folders on the hub.

With `auto_hub = true` on a device, later changes that only need new folders on the hub (a release that adds a save folder, say) no longer need this step: the console creates them itself when it runs `provision device`, which the updater does after each release. Whitelisted folders (`library`, `songs`), a device that is new to an existing folder, and any change to a folder the hub already has still need `provision hub`; the console says so in a warning.

Then, on the device, with the config in `~/.config/warpzone/config.toml`:

```bash
mkdir -p ~/.local/share/warpzone && cd ~/.local/share/warpzone
git clone https://github.com/dagonco/warpzone.git src
sh src/warpzone/data/updater.sh install <device>
```

This runs `warpzone provision device <device>` for you (see [Installation](/warpzone/overview/installation/)). Use `--syncthing-config PATH` with `provision device` if the local Syncthing's `config.xml` is in an unusual place. A save folder whose emulator has not run yet is skipped with a warning and picked up by the next run.

## 6. Check it

```bash
warpzone status
warpzone doctor
warpzone health <device>
```

Then drop a game in the hub's `inbox/` and see it arrive ([Adding games](/warpzone/setup/inbox/)).
