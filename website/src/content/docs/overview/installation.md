---
title: "Installation"
description: "What warpzone needs and how to install it on a device and on the machine that runs the hub commands."
---

What warpzone needs and how to install it on a device and on the machine that runs the hub commands.

warpzone has no runtime dependencies. It is a Python program that uses only the standard library, so it runs unchanged on SteamOS, macOS and any Linux box. There are two ways to install it, for two jobs: a device that keeps itself up to date, and the machine you use to administer the hub.

## Requirements

- Syncthing v2.x on the hub and on every device (tested with v2.1.7).
- Python 3.11 or newer on every machine that runs `warpzone`.
- git on devices, because the updater works from a clone.
- The hub is driven remotely through its Syncthing REST API, so it does not need Python (many NAS ship an old one). Enable the GUI and API on a LAN address of the hub.

## On the admin machine

The admin machine runs the commands that talk to the hub (`provision hub`, `check`) and, if you want it, `inbox`. It needs a clone and nothing else:

```bash
git clone https://github.com/dagonco/warpzone.git && cd warpzone
python3 -m warpzone --help
python3 -m warpzone --version
```

Put the private config at `~/.config/warpzone/config.toml` (or pass `--config PATH` to each command), starting from [`examples/config.example.toml`](https://github.com/dagonco/warpzone/blob/main/examples/config.example.toml). Every key is described in [Configuration](/warpzone/reference/configuration/). The file holds the hub's API key reference and device IDs: keep it out of any repository, and the key file `chmod 600`.

A desktop that only runs `warpzone inbox` needs just the `[inbox]` table of the config, not `[hub]` or `[devices]` ([Adding games](/warpzone/setup/inbox/)). It may also need external tools such as `chdman`; `warpzone doctor` lists which ones and how to install them ([doctor](/warpzone/operation/doctor/)).

## On a device

A console or any other device that keeps itself up to date has one supported install: from a clone, with the updater script. A `pipx` or wheel install is not a supported device install.

Before you start, the device needs Python 3.11 or newer, git, Syncthing running, the private config in `~/.config/warpzone/config.toml`, and the hub provisioned (see the [quick start](/warpzone/overview/quick-start/), steps 1 to 4).

```bash
mkdir -p ~/.local/share/warpzone && cd ~/.local/share/warpzone
git clone https://github.com/dagonco/warpzone.git src
sh src/warpzone/data/updater.sh install deck
~/.local/share/warpzone/venv/bin/warpzone --version
```

`deck` is the device's name in the config (`[devices.deck]`). The script:

1. Refuses, before changing anything, a clone that has changes to tracked files or commits of its own, a device with no Python 3.11 or newer, and the case where no release can be installed.
2. Picks the newest release of the channel and checks that tag out. The default channel takes only `vX.Y.Z` tags.
3. Makes a venv next to the clone, with no pip and no network beyond git, and checks that this release imports with it.
4. Runs `warpzone provision device deck`, which finds the local Syncthing's API key in its `config.xml` (including the SyncThingy flatpak), adds the hub, creates the subscribed folders, and installs the updater and its timer.

The synopsis is `updater.sh install DEVICE [--config FILE] [--venv DIR] [--channel stable|rc] [-- ARGS...]`. `--config` points at a config in another place, `--venv` puts the venv elsewhere, `--channel rc` also takes release candidates, and what follows `--` goes to `provision device`.

Put `~/.local/share/warpzone/venv/bin` on your `PATH` to type `warpzone`, then run:

```bash
warpzone doctor
warpzone health deck
```

`doctor` checks this machine and prints the fix for each problem. `health` checks that this version works on the device and writes nothing.

## The autonomous updater

`provision device` also installs a systemd user timer that starts the updater 45 seconds after boot and every 15 minutes. Each run looks for a newer release on your channel, switches to it, verifies it with `warpzone health`, and goes back to the previous release if it fails. Devices without systemd get the install but no timer.

In the device's table of the config you can tune it, then run `warpzone provision device <name>` again to rewrite the settings:

| Key | Effect |
|---|---|
| `self_update = false` | Do not install the updater timer on this device. |
| `channel = "rc"` | Follow release candidates too. The default is `"stable"`. |
| `pin = "v0.3.0"` | Stay on one tag, whatever the channel. |

`warpzone self-update` runs one tick by hand. The layout on disk, the rules the updater follows, the log and unattended provisioning are in [Updates](/warpzone/operation/self-update/).

## Syncthing on the device

warpzone configures Syncthing; it does not install it. On SteamOS, keeping Syncthing running in Game Mode and giving the SyncThingy flatpak access to the emulators' folders are covered in [Steam Deck and EmuDeck](/warpzone/guides/emudeck/#syncthing-on-the-console).

## Verifying a download

Each release lists a `SHA256SUMS` file and carries a build provenance attestation:

```bash
sha256sum -c SHA256SUMS
gh attestation verify <file> --repo dagonco/warpzone
```
