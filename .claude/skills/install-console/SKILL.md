---
name: install-console
description: Use when the user wants to set up warpzone on a new console or device (Steam Deck, Steam Machine, handheld, spare computer), enable its autonomous updater, or reinstall a device. Covers the device install with updater.sh, provision device and the first doctor and health checks.
---

# Install warpzone on a device

Runs on the device itself, not on the hub. There is one supported device install: a git clone plus `updater.sh install`. A `pipx` or wheel install is not supported. Full detail: [self-update.md](../../../docs/self-update.md) and the "Install" section of the [README](../../../README.md).

Never print the contents of the config file or any API key. Show commands and their output.

## Before you start

1. Confirm the device name. It is a table name under `[devices.<name>]` in the user's config (`$XDG_CONFIG_HOME/warpzone/config.toml`, normally `~/.config/warpzone/config.toml`; `$WARPZONE_CONFIG` or `--config PATH` override it). List the table names only, for example `grep '^\[devices\.' <config>`, and ask the user which one this machine is. Do not guess.
2. Check the device has Python 3.11 or newer (`python3 --version`), `git`, and a running Syncthing. If anything is missing, stop and tell the user.
3. A console new to warpzone has no `[devices.<name>]` table yet. Then, on the admin machine (the one that can reach the hub), with the user doing the edits and you never inventing values:
   - the user adds the table with the device's real Syncthing ID (Actions > Show ID) and profile; the template is [examples/config.example.toml](../../../examples/config.example.toml);
   - the user creates `patterns-<name>.stignore` in the hub's `library/` (and `songs/` if it subscribes), from [templates/patterns-device.example.stignore](../../../templates/patterns-device.example.stignore);
   - run `warpzone check`, then `warpzone provision hub --dry-run` and show it; after the user's OK run `warpzone provision hub` (README Quickstart step 3);
   - copy the updated config to the device (next point).
4. The config must be on the device. Copy it from the user's other machine (the same file works everywhere); never create one with made-up IDs. If `[hub]` uses `api_key_file`, copy that key file too, to the path the config names: a missing or empty key file is a config error and `provision device` exits `2`. Give it `chmod 600` and never print it.
5. The hub must already be provisioned and its whitelist `patterns-<device>.stignore` must exist on it, or `provision device` stops before touching anything.

## Install

```bash
mkdir -p ~/.local/share/warpzone && cd ~/.local/share/warpzone
git clone https://github.com/dagonco/warpzone.git src
sh src/warpzone/data/updater.sh install <device>
```

Options of `install` (from `updater.sh`): `[--config FILE] [--venv DIR] [--channel stable|rc] [-- ARGS...]`. Everything after `--` goes to `provision device`. Use `--channel rc` only if the user wants release candidates, and tell them to also set `channel = "rc"` under `[devices.<name>]` in the config so `provision device` keeps it.

What it does: refuses a dirty clone or a missing Python 3.11, checks out the newest release tag, makes a venv next to the clone, then runs `provision device <device>`, which also installs the updater's live copy, its settings and the systemd timer (45 seconds after boot, then every 15 minutes). Running `install` again is safe.

Reading the result of `provision device`:

- Exit `0`: done. Warnings are not failures; read them to the user.
- Exit `3`: something was deferred because ES-DE, an emulator or a file ES-DE rewrites was open. This is not a failure. Ask the user to close it and run `provision device <device>` again. Exit `3` only comes from `provision device --unattended` (what the updater runs); the install runs it without `--unattended` unless you pass it after `--`.
- Exit `1` or `2`: a real failure. Run through `updater.sh install`, any non-zero exit of `provision device` makes install stop with `provision device failed (exit N)` and exit `1`: read `N` and the message above it, fix the cause, and run the install again. Do not retry blindly.

To preview a re-run once the install has made the venv: `~/.local/share/warpzone/venv/bin/warpzone provision device <device> --dry-run` (it prints the REST calls it would make and sends none).

## Verify

```bash
~/.local/share/warpzone/venv/bin/warpzone --version
~/.local/share/warpzone/venv/bin/warpzone doctor
~/.local/share/warpzone/venv/bin/warpzone health <device>
```

`doctor` prints `[ok]`, `[! ]`, `[!!]` and `[--]` lines with a fix for each problem. `health` ends with `health: ok` and exits `0` when this version works. Interpret problems with the `diagnose` skill. Optionally put `~/.local/share/warpzone/venv/bin` on `PATH`.

On a systemd device, check the timer: `systemctl --user status warpzone-self-update.timer`.

## Rules

- The clone must stay on a release tag or a clean branch. Never leave local commits or edits in it: the updater will not move it.
- To turn the updater off, set `self_update = false` in the config and run `warpzone provision device <device>`; see [Turning it off](../../../docs/self-update.md#turning-it-off).
- Do not use `sudo` and do not edit Syncthing's config by hand.
