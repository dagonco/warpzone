# Checking a machine

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
  [--] dolphin-tool not found (only for compressed GameCube images (.rvz))
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
- The inbox tools (`chdman`, `bsdtar`, `RAHasher`, `dolphin-tool`, `nsz`), the Switch titledb folder and keys file (presence only).
- The DAT folder and how old the newest DAT is, and the optional arcade DAT files.
- The hub folder and its free space.
- Whether `RA_API_KEY` is set (never printed).

A tool or table you do not use is shown as optional, not as a problem. A desktop that only runs `inbox` does not need Syncthing. A machine with no `[inbox]` table in its config gets one line saying inbox is not set up, instead of the whole list of inbox tools, DATs and keys. A machine whose config has only an `[inbox]` table gets one `[ok]` note for `[hub]` and `[devices]`.

Long details and fixes are wrapped to the terminal width (100 columns when it is unknown). Lines of a fix that start with a space, such as a unit file, are not wrapped, so they can be copied.

## Syncthing in Game Mode (SteamOS)

On SteamOS with a local Syncthing, `doctor` also checks that Syncthing will run in Game Mode. Game Mode starts no desktop session, so only an enabled systemd user unit runs it; a desktop autostart entry is not enough.

If no unit exists, the fix shows one for the SyncThingy flatpak:

```
ExecStart=/usr/bin/flatpak run --command=syncthing com.github.zocker_160.SyncThingy serve --no-browser --no-restart
```

plus `ExecStop=/usr/bin/flatpak kill com.github.zocker_160.SyncThingy` (see [emudeck.md](emudeck.md#syncthing-on-the-console)), then `systemctl --user enable --now syncthing.service`, and a reminder to remove the autostart entry so two instances do not compete. It also warns when both a unit and an autostart entry start Syncthing.

With the SyncThingy flatpak, `doctor` also checks that Syncthing can see the emulators' data folders (`flatpak override --user --show`). For every installed emulator whose folder under `~/.var/app` is missing from the overrides it reports a problem and prints the `flatpak override` command to fix it. See [emudeck.md](emudeck.md#flatpak-access-to-the-emulators-folders).

The Syncthing unit is checked too: if `systemctl --user show syncthing` reports `activating (auto-restart)`, `doctor` warns that the unit keeps failing to start, usually because an old Syncthing still holds the port. With the SyncThingy flatpak the fix is `flatpak kill com.github.zocker_160.SyncThingy`, a unit with the `ExecStop` above, and a restart.

## Dreamcast VMUs (RetroArch Flycast)

When RetroArch's Flycast core has run (`Flycast.opt` exists in the RetroArch flatpak), `doctor` reads its `reicast_per_content_vmus` option. If it is `disabled` or missing, the core keeps the VMUs in `bios/dc/`, which warpzone does not sync, and the check warns with the menu path to change it. `VMU A1` and `All VMUs` are fine. See [emudeck.md](emudeck.md#dreamcast-saves-retroarch-flycast).

## Vita3K user

`doctor` reads `user-id:` in `~/.config/Vita3K/config.yml`. The profile syncs the saves of user `00`; if Vita3K's active user has another id, the check warns and says how to fix it. See [emudeck.md](emudeck.md#vita3k-syncs-user-00).

## Emulator settings

On a console that subscribes to the `settings` group, `doctor` finds the device from the local Syncthing's `config.xml` (a machine without it says nothing) and adds three lines. See [settings.md](settings.md).

- **Settings layers.** The local `wz-settings` folder and its marker exist, and `common.toml` and the device's layer parse and validate. A missing folder, an invalid layer, a conflict copy or a secret key in a layer is a problem; an emulator or file id the profile does not know is a warning.
- **ES-DE startup script.** The script is installed, executable and its Python exists, and ES-DE's custom event scripts are on (`CustomEventScripts` in `es_settings.xml`). Otherwise it warns and the fix is `warpzone provision device <name>`, run with ES-DE closed, or the menu path in ES-DE.
- **Last settings apply.** Read from `last-apply.json`: a failed run warns and names the log; keys held back warn and the fix is `warpzone settings capture`; no run yet is optional.

## Frontend

On a device that subscribes to `library` with a profile that moves the frontend and RetroArch to the library (`anbernic-rg351mp`), `doctor` finds the device the same way, through the local Syncthing's `config.xml`, and adds two lines, and two more when it lists `frontend`. See [anbernic.md](anbernic.md).

- **ES systems override.** `~/.emulationstation/es_systems.cfg` is exactly the system list of the OS with the ROM folders moved into the library. Missing or out of date (dArkOS updated its list) is a warning, and the fix is `warpzone provision device <name>` with EmulationStation closed.
- **RetroArch paths.** Each RetroArch config has the BIOS folder and the save and state settings the profile wants. A key that differs is named with its current value; a missing config or a key present twice is a warning.
- **Frontend artwork.** For a device that lists `frontend` and whose profile has `[artwork]`: every gamelist that `warpzone frontend refresh` would write exists and says what it should (new scrape, new cover or a game that arrived since: `stale`). Missing or stale ones are named and the fix is `warpzone frontend refresh <name>` with the frontend closed. It also warns when no scraped gamelist has arrived yet for the systems on the device (look at the `wz-esde` folder with `warpzone status`) and when the system list or a scraped file cannot be read.
- **Artwork timer.** Only when the profile sets `refresh`: `warpzone-artwork.timer` is installed in `~/.config/systemd/user`. Otherwise the fix is `warpzone provision device <name>`.

Details in [design.md](design.md#doctor).
