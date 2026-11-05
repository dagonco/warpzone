---
title: "Anbernic RG351MP (dArkOS)"
---

The `anbernic-rg351mp` profile is for an Anbernic RG351MP that runs dArkOS: EmulationStation as the frontend, RetroArch for most systems, and a games partition (`/roms`) formatted as exFAT. It is a plain TOML file in [`warpzone/data/profiles/`](https://github.com/dagonco/warpzone/tree/main/warpzone/data/profiles).

## What it does differently

- **The library has its own folder, `/roms/library`.** Games land in `/roms/library/roms/<system>` and BIOS files in `/roms/library/bios`, the same layout as on the hub, and dArkOS's own `/roms/<system>` folders are left alone.
- **No symlinks, no permissions.** exFAT has neither. Every folder of the device is created with Syncthing's `ignorePerms` on (the profile sets `ignore_perms = true`), so the hub's file modes do not show up as local changes. Nothing in the profile relies on a symlink: the save folders are real directories on the root filesystem, outside the card.
- **No ES-DE, EmuDeck, flatpak, songs or emulator settings layer.** The profile has no path for `tools`, `songs` or `settings`, so a device that lists one of them is refused with a clear message. `frontend` is accepted, but it is not ES-DE's folders: it delivers the scraped artwork, see [Artwork](#artwork).

## Config

```toml
[devices.rg351mp]
device_id = "XXXXXXX-XXXXXXX-XXXXXXX-XXXXXXX-XXXXXXX-XXXXXXX-XXXXXXX-XXXXXXX"
profile = "anbernic-rg351mp"
folders = ["library", "saves", "frontend"]
```

`library`, `saves` and `frontend` are the folders that make sense here. `bios` is accepted but empty (the profile has no BIOS placements; BIOS files arrive through the library, from `bios/` in your whitelist). Whitelist systems by the folder names dArkOS uses; a name on the hub that dArkOS spells differently (for example `megacd` and `segacd`) needs the hub folder renamed or a copy under the other name.

Then, on the device (the user that runs Syncthing), with EmulationStation and RetroArch closed:

```bash
warpzone provision device rg351mp
```

## The system list override

EmulationStation reads `~/.emulationstation/es_systems.cfg` before `/etc/emulationstation/es_systems.cfg`. `provision device` writes the first as a copy of the second in which every `<path>/roms/X</path>` becomes `<path>/roms/library/roms/X</path>`. Everything else in the file is kept byte for byte, including the entries outside `/roms/` such as `/opt/cmds/`.

Every system moves, also those the hub has no games for: they point at empty folders. Moving only the systems the hub has would tie the override to your whitelist and make it stale at every edit. The side effect is that the device's own `/roms/<system>` folders (for example `ports`) are not listed any more; keep that in mind before you put anything there.

The file is written only while EmulationStation is not running, a different existing file is backed up first, and a second run changes nothing. If dArkOS updates its system list, `warpzone doctor` reports the override as out of date; run `provision device` again.

## RetroArch

dArkOS ships two RetroArch builds, each with its own config (`~/.config/retroarch/retroarch.cfg` and `~/.config/retroarch32/retroarch.cfg`). `provision device` sets in both:

| Key | Value |
|---|---|
| `system_directory` | `/roms/library/bios` |
| `savefiles_in_content_dir` | `false` |
| `savestates_in_content_dir` | `false` |
| `savefile_directory` | `~/.config/retroarch/saves` |
| `savestate_directory` | `~/.config/retroarch/states` |

With the first two off, saves and states stop being written next to the ROMs and go to the two folders that sync, which are the same hub folders (`saves/retroarch/saves` and `saves/retroarch/states`) that the Steam Deck and Steam Machine use. Both builds write to the same two folders. Only the value of those keys changes; the old file is backed up first, nothing is edited while RetroArch runs, and a file that RetroArch has not created yet is never created. Saves that already sit next to the ROMs are not moved.

The save layout matches the Steam devices: all `sort_*` options are off on every device, so each save is one file named after the game in one folder. A save only loads in the same core (and a state in the same core version), which depends on the games you play and which core each device uses for them.

## Artwork

dArkOS's EmulationStation reads `<rom folder>/gamelist.xml` for descriptions and images, and nothing writes it. The hub already holds what you scraped with ES-DE (`frontend/es-de/gamelists` and `frontend/media`), so the `frontend` group delivers a slice of it and warpzone converts it. The profile's `[artwork]` table says everything device-specific:

| Key | Value here | Meaning |
|---|---|---|
| `format` | `emulationstation` | The converter that writes this frontend's gamelist. |
| `kinds` | `["covers"]` | Which media kinds arrive. Add `screenshots`, `marquees`... to receive more (and use more space). |
| `systems` | `library` | The systems that have a folder in `/roms/library/roms`, so it follows your whitelist. |
| `media`, `metadata` | `/roms/warpzone/media`, `/roms/warpzone/es-de` | Where the hub's `wz-media` and `wz-esde` land, on the card, outside the library. |
| `gamelists` | `{rompath}` | Where the generated gamelists go: next to the ROMs, because EmulationStation saves there and prefers that file. |
| `refresh` | `15min` | How often the timer looks for new scrapes. |

What happens:

- **Only what is needed arrives.** The two folders are receive-only here, with ignore lines that let in only the covers (`<system>/covers`) and the gamelists of the systems on the device. The Steam Deck and Steam Machine receive the same hub folders in full and nothing about them changes. After you add a system to a whitelist, its artwork follows once its games have arrived (the next `frontend refresh`, or the timer). Covers of a whole system arrive even when only a few of its games are on the device.
- **The gamelist is rebuilt from it.** For each system, `<image>` is the absolute path of the cover that exists (`<media>/<system>/covers/<game>.png`, subfolders kept), the descriptions, ratings, dates, developer, publisher, genre and player count are copied, and a game whose ROM is not on the device is left out. The file goes under the name EmulationStation gives the system, not the folder: hub `pokemini` becomes `pokemonmini` (`[es_systems.rename]`), hub `mame` becomes `mame2010` and hub `psp` becomes `ppsspp`. If two systems share a name, one is chosen and the other is reported.
- **Your own data stays.** EmulationStation saves favourites, play counts and last played time in that same file (on exit). A rebuild carries them over; only the scraped fields are replaced, so edit a description on the hub, not on the device. To share them with the other consoles, see [Favourites and play statistics](#favourites-and-play-statistics).
- **Only while EmulationStation is closed.** Like the system list override, the files are written only when it is not running (it rewrites them when it quits). A rebuild that would change nothing leaves the files alone.
- **New scrapes apply by themselves.** `provision device` installs a `systemd --user` timer (`warpzone-artwork.timer`) that runs `warpzone frontend refresh` every 15 minutes. If EmulationStation is open the run changes nothing and the next one tries again. It uses the Python that runs `provision`, which must be able to import warpzone from outside a clone (install it with pipx).

To refresh by hand, with EmulationStation closed:

```bash
warpzone frontend refresh rg351mp
```

It first brings the ignore lines up to date with the systems on the device (if it finds the local Syncthing), then rewrites the gamelists. `--dry-run` prints what it would write.

To set this up on an existing device: add `"frontend"` to its `folders`, run `warpzone provision hub` (it adds the device to `wz-media` and `wz-esde` and changes nothing else), then `warpzone provision device rg351mp` on the device. Covers alone are a few hundred MB for fifteen classic systems; more `kinds` mean more space.

## Favourites and play statistics

With `play-state` in `folders`, favourites, play counts and last played times travel between this device and the others, in both directions. The profile already says where: `paths.play_state` is `/roms/warpzone/play-state`, and the `[artwork]` timer does the work, so this device needs no table of its own and no SSH.

```toml
[devices.rg351mp]
folders = ["library", "saves", "frontend", "play-state"]
```

Add it to every device that takes part, run `warpzone provision hub`, then `warpzone provision device <name>` on each, with the frontend closed.

- **What you see.** A favourite follows the latest change on any device, and removing one sticks. The play count is the sum of this device and the ES-DE consoles. Last played is the latest, shown in this device's time zone.
- **When.** Each `frontend refresh` publishes what EmulationStation saved (on exit) and merges the others, every 15 minutes while it is closed. While it is open only publishing happens, and the merge waits.
- **Games that are not here.** Their state waits in the files and applies if the game arrives. `hidden` and `kidgame` stay on this device.

See [the design document](/warpzone/development/design/play-state/) for the rules.

## Options menu entries

`provision device` adds two entries to the ArkOS "Options" menu (the profile's `[options_menu]` table points at `/opt/system`, where ArkOS lists every executable `.sh`):

- **Warpzone Status** shows, without the network: the warpzone version, the updater's channel and pin, the version it last settled on (`good tag`), a new release the console found and has not installed (`New release`), the result of its last run, when it last looked for a release, and the problems `warpzone doctor` finds (at most 8 lines, cut to fit the screen). Anything it cannot read shows as `unknown`.
- **Warpzone Update Now** runs `warpzone self-update --now`: the console looks for a release right away, installs and verifies it, and shows how it ended. It does not wait for the menu to close, since it is started from it.

The console never installs a release on its own: EmulationStation is open whenever it is on, so its timer only looks for releases (the profile's `[updater] install = "manual"`) and Warpzone Status shows when there is one. Install it with Warpzone Update Now, from the menu, with no game running.

Only these two files are written, they are overwritten at every `provision device` run, and nothing else in `/opt/system` is touched. If the folder does not exist, the step is skipped with a warning. The status entry only reads what the updater already keeps on the device, so it works without SSH and without a connection.

## Doctor

On the device, `warpzone doctor` adds two lines under "Frontend": `ES systems override` and `RetroArch paths`; with `frontend` in the folders it adds `Frontend artwork` (gamelists missing or stale) and `Artwork timer`, and with `play-state` it adds `Play state`. See [the doctor guide](/warpzone/operation/doctor/#frontend).
