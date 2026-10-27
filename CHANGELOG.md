# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog 1.1.0](https://keepachangelog.com/en/1.1.0/), and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.2.0] - 2026-10-27

### Added

- `warpzone prune [device]`: deletes the files a device no longer whitelists. It asks Syncthing which files it ignores (after a full rescan) and deletes a file only if the hub still has it unchanged; files that exist only on the device are reported and kept. Report only by default, `--apply` deletes, `--max-gib` (default 50) caps one run unless `--force`, and `--notify-url` POSTs a JSON summary to a webhook.

## [0.1.0] - 2026-10-27

First release.

### Added

- Hub-and-device layout: one Syncthing hub owns the game library and every console receives it. Folders are named `wz-*`: library, saves per emulator, ES-DE media and gamelists, songs and tools.
- `warpzone check`: validates the config, profiles and whitelist files, lists devices whose whitelist is missing on the hub, and checks the hub unless `--offline` is given.
- `warpzone provision hub`: configures the hub's Syncthing through its remote REST API. It adds every configured device, creates the `wz-*` folders and shares each with the devices that subscribe to it. It is idempotent.
- `warpzone provision device <name>`: configures the local Syncthing of a console in one command. It finds the API key in Syncthing's `config.xml` (including the SyncThingy flatpak), adds the hub and creates the subscribed folders. Save folders whose directory does not exist yet are skipped with a warning and picked up by the next run.
- Per-device whitelists: each device chooses what it gets with a `patterns-<device>.stignore` file that lives in the synced folder on the hub and is edited only there.
- Whitelist bootstrap: a fresh device first receives only the whitelist files, and `provision device` switches to the final ignores once they have arrived (`--timeout`, default 10 minutes). If the device's whitelist is missing on the hub it stops before touching anything, so a device can never pull the whole library by mistake.
- `warpzone status`: shows folder state, outstanding items and receive-only local changes.
- `warpzone render-ignores <device>`: prints the ignore lines a device folder would get.
- `--dry-run` on every writing command: reads state, prints the calls it would make with API keys redacted, and sends nothing.
- `--version` flag.
- `steamos-emudeck` device profile for the Steam Deck and Steam Machine. Save folders point at the real emulator directories, because Syncthing does not follow the symlinks EmuDeck creates under `~/Emulation/saves`.
- Whitelist edits on the hub take effect on a device within 5 minutes. Syncthing only applies a changed whitelist on a full scan of the folder, and a freshly synced ignore file does not reliably trigger one, so whitelisted device folders get a periodic rescan. Tune it per device with `rescan_seconds` (default 300, minimum 5).
- Continuous integration: ruff, unit tests on Linux and macOS for Python 3.11 to 3.14, secret scanning, and an end-to-end test that runs two real Syncthing instances and exercises the whole provisioning flow.
- Safety rule: library, songs and tools are receive-only on devices, so a delete or edit on a console never reaches the hub.
- Safety rule: a `wz-*` folder that already exists in Syncthing with a different path than the config is refused, because changing a path makes Syncthing see a mass delete.
- Safety rule: devices are only ever added, never removed.
- Safety rule: only `wz-*` folders are touched. On existing ones, only the keys `warpzone` owns are patched (label, path, type, devices, versioning, file watcher and the rescan interval of whitelisted device folders).
- Safety rule: API keys are never printed, logged or included in error messages.
- Release process: tag-triggered GitHub Actions workflow, a maintainer checklist in `docs/releasing.md`, and this changelog.

[Unreleased]: https://github.com/dagonco/warpzone/compare/v0.2.0...HEAD
[0.2.0]: https://github.com/dagonco/warpzone/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/dagonco/warpzone/releases/tag/v0.1.0
