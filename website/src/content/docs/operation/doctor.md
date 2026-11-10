---
title: "Checking a machine"
---

What `warpzone doctor` checks and how to read its output.

`warpzone doctor` is a checklist for the machine you run it on, in the spirit of `flutter doctor`. It prints one line per check and, for every problem, the exact fix for this platform (Homebrew on macOS, apt, dnf or pacman on Linux, a container on SteamOS).

| Marker | Meaning |
|---|---|
| `[ok]` | Fine. |
| `[! ]` | Warning. |
| `[!!]` | Problem. |
| `[--]` | Optional and not set up. |

It never installs or changes anything and makes no network calls (the one probe is a request to your local Syncthing on `127.0.0.1`). It exits `1` only when something required is broken, so it can run in a script. Use `--config PATH` for a config in another place.

```
$ warpzone doctor
Inbox tools
  [ok] chdman       chdman - MAME Compressed Hunks of Data (CHD) manager 0.289 (unknown) (/opt/homebrew/bin/chdman)
  [!!] bsdtar       not found (required by inbox)
                    fix: sudo apt install libarchive-tools
  [--] dolphin-tool not found (only for compressed GameCube images (.gcz))
                    fix: install Dolphin (brew install --cask dolphin); ...
...
1 problem(s), 0 warning(s), 4 optional item(s) not set up. Fix the [!!] lines above.
```

## What it covers

- warpzone and Python.
- The config file, and what each table enables.
- The local Syncthing and its REST API.
- RetroArch's Flycast option for per-game VMUs, when the core has run.
- Vita3K's active user, when Vita3K has a config.
- The emulator settings layers, the ES-DE startup script and the last `settings apply`, on a device that subscribes to `settings`.
- The frontend's system list override and RetroArch's paths, and the generated artwork gamelists and their timer, on a device whose profile sets them (`anbernic-rg351mp`).
- The save conversion, on a device that lists `saves` and whose profile has `[save_convert.*]` (`anbernic-rg351mp`): when it last ran, the games it skipped as not valid saves, and the times the newest file won when both sides had changed (see [Save conversion](/warpzone/development/design/save-conversion/)). The system list override must also wrap the emulator's command.
- shadPS4's install folder (`~/Emulation/roms/ps4/games` in its config files) and DLC folder (`~/Emulation/roms/ps4/dlc`), on a device whose profile declares it (`steamos-emudeck`).
- Cemu's paths in `~/.config/Cemu/settings.xml`, on a device whose profile declares them (`steamos-emudeck`): `mlc_path` must be `~/Emulation/roms/wiiu/mlc01`, the folder whose `usr/save` warpzone syncs (an empty value means Cemu keeps its saves in `~/.local/share/Cemu/mlc01`, which nothing syncs), and `GamePaths` must list `~/Emulation/roms/wiiu/roms`. The fix is `warpzone provision device <name>` with Cemu closed; with no settings file yet, start Cemu once first.
- The shadPS4 launcher: warpzone's launch script exists, is executable and is the text `provision device` writes, and ES-DE's `SHADPS4` find rule runs it (EmuDeck can put its own entry back). The fix is `warpzone provision device <name>` with ES-DE closed.
- The shadPS4 version, on the same device: whether the Qt launcher has a version selected (`~/.local/share/shadPS4QtLauncher/qt_ui.ini`) or installed (`versions.json`) for the launch script to run. Without one PS4 games do nothing when started, because ES-DE does not show the script's error. It is a warning while the device holds no PS4 games and a problem once it does. The fix is to open the shadPS4 Qt launcher, go to Version Manager, then download and select a version; no warpzone command is needed. The script also logs each failed launch to `~/.local/state/warpzone/shadps4-launch.log`.
- Vita3K's game links (`~/Emulation/storage/Vita3K/ux0/app/<TITLEID>`) and licence links (`ux0/license/<TITLEID>`), the ES-DE startup script that keeps them, and dangling links, on a device whose profile declares `[links.*]` (`steamos-emudeck`). The fix is `warpzone links sync <name>` with Vita3K closed.
- The updater, on a Linux device (see [Updating a device](/warpzone/operation/self-update/)): its timer (installed, running the updater, enabled), the live copy of the script, its settings against the config, and its state. A warning for a rollback in the last 14 days, a `dirty` or `dev` checkout, a retired `pin`, signatures not required, and more than 14 days without a successful run; a problem when it is `stuck`.
- The unattended prune, on a device that lists `library` without `auto_prune = false`: a warning when its timer refused to delete anything because a folder would lose more than half of what the device holds (it names the folders and how long ago, and the fix is to check the whitelist, run `warpzone prune <name>` to review and then `warpzone prune <name> --apply` by hand). See [Automatic pruning](/warpzone/setup/whitelists/#automatic-pruning).
- The frontend's system list override and RetroArch's paths, on a device whose profile sets them (`anbernic-rg351mp`).
- The inbox tools (`chdman`, `bsdtar`, `RAHasher`, `dolphin-tool`, `nsz`, `orbis-pkg-util`, `Vita3K`; Vita3K is only found, never run, and on macOS `/Applications/Vita3K.app` counts; a Vita3K that inbox would refuse, such as an AppImage, is a warning), the Switch titledb folder and keys file (presence only).
- The DAT folder and how old the newest DAT is, and the optional arcade DAT files.
- The hub folder and its free space.
- Where `inbox` runs, with an `[inbox]` table: on a computer or in the hub container, found from the machine. In the container it checks the tools of the image (RAHasher at `/usr/local/bin/RAHasher`; a tool the image lacks says so instead of giving an install command), `[inbox] root`, the mounted DATs and the work folder. See [where inbox runs](/warpzone/setup/inbox/#where-inbox-runs).
- Whether `RA_API_KEY` is set (never printed).

A tool or table you do not use is shown as optional, not as a problem. A computer or container that only runs `inbox` does not need Syncthing. A machine with no `[inbox]` table in its config gets one line saying inbox is not set up, instead of the whole list of inbox tools, DATs and keys. A machine whose config has only an `[inbox]` table gets one `[ok]` note for `[hub]` and `[devices]`.

Long details and fixes are wrapped to the terminal width (100 columns when it is unknown). Lines of a fix that start with a space, such as a unit file, are not wrapped, so they can be copied.

## Syncthing in Game Mode (SteamOS)

On SteamOS with a local Syncthing, `doctor` also checks that Syncthing will run in Game Mode. Game Mode starts no desktop session, so only an enabled systemd user unit runs it; a desktop autostart entry is not enough.

If no unit exists, the fix shows one for the SyncThingy flatpak:

```
ExecStart=/usr/bin/flatpak run --command=syncthing com.github.zocker_160.SyncThingy serve --no-browser --no-restart
```

plus `ExecStop=/usr/bin/flatpak kill com.github.zocker_160.SyncThingy` (see [the EmuDeck guide](/warpzone/guides/emudeck/#syncthing-on-the-console)), then `systemctl --user enable --now syncthing.service`, and a reminder to remove the autostart entry so two instances do not compete. It also warns when both a unit and an autostart entry start Syncthing.

With the SyncThingy flatpak, `doctor` also checks that Syncthing can see the emulators' data folders (`flatpak override --user --show`). For every installed emulator whose folder under `~/.var/app` is missing from the overrides it reports a problem and prints the `flatpak override` command to fix it. See [the EmuDeck guide](/warpzone/guides/emudeck/#flatpak-access-to-the-emulators-folders).

The Syncthing unit is checked too: if `systemctl --user show syncthing` reports `activating (auto-restart)`, `doctor` warns that the unit keeps failing to start, usually because an old Syncthing still holds the port. With the SyncThingy flatpak the fix is `flatpak kill com.github.zocker_160.SyncThingy`, a unit with the `ExecStop` above, and a restart.

## Dreamcast VMUs (RetroArch Flycast)

When RetroArch's Flycast core has run (`Flycast.opt` exists in the RetroArch flatpak), `doctor` reads its `reicast_per_content_vmus` option. If it is `disabled` or missing, the core keeps the VMUs in `bios/dc/`, which warpzone does not sync, and the check warns with the menu path to change it. `VMU A1` and `All VMUs` are fine. See [the EmuDeck guide](/warpzone/guides/emudeck/#dreamcast-saves-retroarch-flycast).

## Vita3K user

`doctor` reads `user-id:` in `~/.config/Vita3K/config.yml`. The profile syncs the saves of user `00`; if Vita3K's active user has another id, the check warns and says how to fix it. See [the EmuDeck guide](/warpzone/guides/emudeck/#vita3k-syncs-user-00).

## Emulator settings

On a console that subscribes to the `settings` group, `doctor` finds the device from the local Syncthing's `config.xml` (a machine without it says nothing) and adds three lines. See [Emulator settings](/warpzone/setup/settings/).

- **Settings layers.** The local `wz-settings` folder and its marker exist, and `common.toml` and the device's layer parse and validate. A missing folder, an invalid layer, a conflict copy or a secret key in a layer is a problem; an emulator or file id the profile does not know is a warning.
- **ES-DE startup script.** The script is installed, executable and its Python exists, and ES-DE's custom event scripts are on (`CustomEventScripts` in `es_settings.xml`). Otherwise it warns and the fix is `warpzone provision device <name>`, run with ES-DE closed, or the menu path in ES-DE.
- **Last settings apply.** Read from `last-apply.json`: a failed run warns and names the log; keys held back warn and the fix is `warpzone settings capture`; no run yet is optional.

## Frontend

On a device that subscribes to `library` with a profile that moves the frontend and RetroArch to the library (`anbernic-rg351mp`), `doctor` finds the device the same way, through the local Syncthing's `config.xml`, and adds two lines, and two more when it lists `frontend`. See [the Anbernic guide](/warpzone/guides/anbernic/).

- **ES systems override.** `~/.emulationstation/es_systems.cfg` is exactly the system list of the OS with the ROM folders moved into the library (and, for a system whose saves are converted, its command wrapped in the conversion). Missing or out of date (dArkOS updated its list) is a warning, and the fix is `warpzone provision device <name>` with EmulationStation closed.
- **Save conversion.** The last run of `warpzone saves convert`: a warning when it never ran, failed on some game, or skipped games that are not valid saves (named, with the reason); the games where both sides had changed and the newest won are listed, with where the other copy is kept. The fix is to correct or remove the file named, then `warpzone saves convert <name>`.
- **RetroArch paths.** Each RetroArch config has the BIOS folder and the save and state settings the profile wants. A key that differs is named with its current value; a missing config or a key present twice is a warning.
- **Frontend artwork.** For a device that lists `frontend` and whose profile has `[artwork]`: every gamelist that `warpzone frontend refresh` would write exists and says what it should (new scrape, new cover or a game that arrived since: `stale`). Missing or stale ones are named and the fix is `warpzone frontend refresh <name>` with the frontend closed. It also warns when no scraped gamelist has arrived yet for the systems on the device (look at the `wz-esde` folder with `warpzone status`) and when the system list or a scraped file cannot be read.
- **Artwork timer.** Only when the profile sets `refresh`: `warpzone-artwork.timer` is installed in `~/.config/systemd/user`. Otherwise the fix is `warpzone provision device <name>`.
- **Play state.** For a device that lists `play-state`: its folder is set up in Syncthing and, with ES-DE, the startup script and `warpzone-play-state.timer` are installed. Otherwise the fix is `warpzone provision device <name>`.

Details in [the design document](/warpzone/development/design/doctor/).
