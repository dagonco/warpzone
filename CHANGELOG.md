# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog 1.1.0](https://keepachangelog.com/en/1.1.0/), and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- `warpzone self-update`, run on a device: fetches the tags of the git clone warpzone runs from and checks out the newest `vX.Y.Z` tag when it is newer than the running version. It never downgrades, refuses a clone with local changes, says so and exits cleanly when warpzone is not a git checkout, and exits `0` with a short message when the remote cannot be reached. `--dry-run` only reports. It logs to `~/.local/state/warpzone/self-update.log`, and reinstalls the editable package only when `pyproject.toml` changed (an editable install reports the new `warpzone --version` by itself). See [docs/self-update.md](docs/self-update.md).
- `provision device` installs and enables a systemd user timer (`warpzone-self-update.timer`: daily, `Persistent=true`, and five minutes after boot) that runs `warpzone self-update`, for every profile and independent of ES-DE. It is idempotent and honours `--dry-run`. `warpzone doctor` warns when a device does not have it. A device turns it off with `self_update = false` in its `[devices.<name>]` table. See [docs/self-update.md](docs/self-update.md).
- The end-to-end test also starts from an empty hub (no whitelist, ROM or BIOS in its folders), adds the whitelists while `provision hub` waits for them, and checks the created folders, the final ignores and that a device receives a whitelisted game.
- A second shipped profile, `anbernic-rg351mp`, for an Anbernic RG351MP running dArkOS with its games on an exFAT card. The library goes in `/roms/library`; the folders of the device ignore permissions (`ignore_perms` in a profile sets Syncthing's `ignorePerms`); and `provision device` writes `~/.emulationstation/es_systems.cfg` as a copy of the OS's system list with the ROM folders moved into the library, and points both RetroArch configs at the library's BIOS folder and at the save and state folders that sync (the same hub folders as the Steam Deck). Both files are written only while EmulationStation or RetroArch is closed, backed up before an edit, and left unchanged by a second run. `warpzone doctor` flags a missing or outdated override and RetroArch keys that are not set. Profiles gain the tables `[es_systems]` and `[retroarch]`, and a save entry may set `create_when`. See [docs/anbernic.md](docs/anbernic.md).
- Scraped artwork and descriptions for a frontend that is not ES-DE. A profile gains an `[artwork]` table (`format`, `kinds`, `systems`, `media`, `metadata`, `gamelists`, optional `refresh`), and `anbernic-rg351mp` uses it: a device that lists `frontend` receives the hub's `wz-media` and `wz-esde` receive-only, limited to the covers and the gamelists of the systems that have a folder in its library, and `provision device` converts the ES-DE gamelists into EmulationStation's own, with absolute `<image>` paths to the covers that exist, only for games that are on the device, under the name the system list gives each system (`[es_systems.rename]` honoured; `pokemini` becomes `pokemonmini`, `psp` becomes `ppsspp`). The converter is picked by the format name, so another frontend is one more small converter. Favourites, play counts and last played times that the frontend saved are kept. New `warpzone frontend refresh` widens the scope and rewrites the gamelists, only while the frontend is closed, and `provision device` installs a `systemd --user` timer that runs it. `warpzone doctor` reports missing or stale gamelists and the timer. The hub's folders and the Steam devices are unchanged. See [docs/anbernic.md](docs/anbernic.md#artwork) and [docs/design.md](docs/design.md#frontend-artwork).
- `warpzone inbox` recognises Wii U `.wua` files (Cemu's ZArchive) by the footer at the end of the file, reading only its last 16 bytes, and files them in `library/roms/wiiu/roms/` under their own name, also when they arrive inside a `.zip`, `.7z` or `.rar`. A `.wua` with a wrong or truncated footer goes to review. See [docs/inbox.md](docs/inbox.md).
- `warpzone inbox` recognises PS4 `.pkg` files by the magic `\x7fCNT` and the content id (`XXnnnn-CUSAnnnnn_nn-...`) in the header, also inside an archive, and reports the title id (`CUSA...`). With [`orbis-pkg-util`](https://crates.io/crates/orbis-pkg-util) configured (`[inbox] orbis_pkg_util`, or on PATH; reported in the `tools:` line and by `warpzone doctor`) a base game is extracted in the local work folder, checked (`eboot.bin`, `sce_sys/param.sfo`) and placed as a folder in `library/roms/ps4/games/<CUSAnnnnn>/` through a `.part` copy and a rename, with a `library/roms/ps4/shortcuts/<Title>.desktop` that ES-DE lists and EmuDeck's shadPS4 launcher runs; both whitelist lines are printed. A game already in the library is reported and not touched, and an update or DLC package goes to review (updates are not merged). Without the tool the `.pkg` is filed in `library/roms/ps4/` as before, with a note. A PS3 `.pkg` or a `.pkg` with an unexpected header keeps going to review. See [docs/inbox.md](docs/inbox.md#ps4-games).

## [0.2.0] - 2026-11-02

### Added

- Emulator settings layer. The hub keeps key-level layers (`settings/common.toml`, `settings/<device>.toml`) in a new two-way folder `wz-settings` that a device joins with the `settings` group, and the `steamos-emudeck` profile declares the settings files of eleven emulators (Eden, RetroArch, DuckStation, PCSX2, Dolphin, Azahar, melonDS, PPSSPP, Vita3K, xemu, Ryujinx). `warpzone settings capture` takes a setting changed in an emulator's own interface to the layers (`--seed` fills them from a console that is already configured), `warpzone settings apply` writes the layers into the emulators' files and `warpzone settings audit` reports the drift. Apply never writes while the emulator runs, never overwrites a change that was not captured (unless `--overwrite-local`), changes only the layered keys, backs up each file first and touches nothing when a layer is invalid. Logins never reach the hub. See [docs/settings.md](docs/settings.md).
- `provision device` installs an ES-DE startup script that runs `settings apply`, and turns on ES-DE's custom event scripts when ES-DE is closed. `warpzone doctor` checks the layers, the script and the last apply.

### Fixed

- Syncthing as a flatpak (SyncThingy) cannot see other apps' `~/.var/app/<app>` folders, so the RetroArch, Dolphin, PPSSPP, PrimeHack and Flycast save folders looked empty to it and synced nothing. `provision device` now adds the missing `flatpak override --user --filesystem=~/.var/app/<app>` for each of them (idempotent, reported in `--dry-run`) and asks for a Syncthing restart. `warpzone doctor` checks the overrides and prints the command for each missing one.
- The Syncthing unit that `warpzone doctor` suggests for the SyncThingy flatpak now has `ExecStop=/usr/bin/flatpak kill ...`. Without it `systemctl --user stop` leaves the flatpak process running and a restart loops in `activating (auto-restart)`. `doctor` warns about a unit in that state and shows the fix, and the unit is documented in `docs/emudeck.md`.
- `warpzone doctor` warns when Vita3K's active user is not `00`. The profile syncs the saves of user `00`, and a user created in Vita3K's window can get another id, which left its saves unsynced.
- Dreamcast: the `steamos-emudeck` profile now ignores `/bios/dc/vmu_save_*.bin` and `/bios/dc/dc_nvmem.bin` in the device library. RetroArch's Flycast core writes them there, so a receive-only console listed them as local changes and a revert would have erased them. `warpzone doctor` warns when the core option for per-game VMUs (`reicast_per_content_vmus`) is off, since only per-game VMUs end up in RetroArch's saves folder and sync. Run `provision device` again to apply the new ignore lines.

## [0.1.0] - 2026-10-28

First public release. warpzone keeps one game library on a home server and delivers it to every console through Syncthing, with saves that sync both ways.

### Added

- Hub and device provisioning (`provision hub`, `provision device`) with a per-device whitelist kept on the hub and edited only there. Library, songs and tools are receive-only on devices.
- Whitelist bootstrap on the hub and on devices, so a new folder never scans or pulls the whole library before the whitelists are in place. Whitelist edits take effect within 5 minutes (`rescan_seconds`).
- Two-way save folders for the emulators of the `steamos-emudeck` profile, pointed at the real emulator directories because Syncthing never follows the symlinks EmuDeck creates. Hub history is kept for 30 days.
- BIOS placements: keys and firmware (Eden, Azahar, Cemu) delivered into each emulator's own folder as receive-only folders with their own ignore lists.
- `warpzone prune` reports, and with `--apply` deletes, files a device no longer whitelists, only when the hub still has them unchanged and within a size cap.
- `warpzone bios` checks BIOS and firmware per system against names, sizes and hashes (no BIOS is shipped or downloaded), with local overrides in `bios.toml`.
- `warpzone inbox` identifies new discs and cartridges by content against Redump and No-Intro DATs, converts them (CHD, zip, ISO), verifies them with `chdman` and RetroAchievements hashes, and files them without overwriting anything.
- `warpzone inbox` files Nintendo Switch `.nsp` and `.nsz` games, updates and DLC by TitleID and version, named after titledb.
- `warpzone inbox` files arcade sets by choosing the emulator the set fits (FinalBurn Neo, MAME, MAME 2003-Plus, or Flycast for Naomi and Atomiswave) and writing the ES-DE per-game override with a backup.
- `warpzone inbox` files PS3, Xbox and Xbox 360 disc images by signature.
- `warpzone doctor` checks the tools, config, Syncthing, DATs and hub folder of a machine and prints the fix for each problem.
- `warpzone status`, `check` and `render-ignores` to inspect folder state, validate the config and preview a device's ignore lines. `provision` supports `--dry-run`.

[Unreleased]: https://github.com/dagonco/warpzone/compare/v0.2.0...HEAD
[0.2.0]: https://github.com/dagonco/warpzone/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/dagonco/warpzone/releases/tag/v0.1.0
