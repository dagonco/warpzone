# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog 1.1.0](https://keepachangelog.com/en/1.1.0/), and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Fixed

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

[Unreleased]: https://github.com/dagonco/warpzone/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/dagonco/warpzone/releases/tag/v0.1.0
