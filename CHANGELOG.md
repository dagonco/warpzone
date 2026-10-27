# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog 1.1.0](https://keepachangelog.com/en/1.1.0/), and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- `warpzone inbox` files arcade (MAME) sets. A dropped `.zip` (or a folder of them) is identified by the size and CRC32 of its members against up to three XML databases, set in `[inbox]`: `mame_dat` (MAME's `-listxml`, or the official `mameXXXXlx.zip` as it is), `fbneo_dat` (the FinalBurn Neo arcade DAT) and `mame2003plus_dat` (the MAME 2003-Plus DAT). A set fits an emulator when every required ROM of the set, its parent, BIOS and devices is present, counting the BIOS and device zips already in `library/roms/mame/`; MAME is tried first (the ES-DE default), then FinalBurn Neo, then MAME 2003-Plus, and a set that fits none goes to review with the exact missing files and the zip they belong to. The zip is placed as `library/roms/mame/<set>.zip` (renamed after the set when it has another name), never overwriting. With `--apply`, an emulator other than the default is written as `<altemulator>` in the ES-DE gamelist (`gamelist_mame`, default `<root>/frontend/es-de/gamelists/mame/gamelist.xml`): only that game's entry is added, changed or, when the default now fits, stripped of its override; the rest of the file is kept as it is, it is written atomically and `gamelist.xml.bak` keeps the previous version. The listxml is streamed and cached as a compact index in the DAT cache folder.
- `warpzone doctor` checks the three optional arcade DAT paths.
- `warpzone bios` covers Xbox (xemu: `mcpx_1.0.bin` by md5 and a 1 MiB flash BIOS by name and size, `Complex_4627v1.03.bin` as EmuDeck names it), PlayStation 3 (RPCS3: `ps3/PS3UPDAT.PUP`, by its `SCEUF` signature) and Wii U (Cemu: optional `cemu/keys.txt`, checked for presence and shape and never printed or hashed). The manifest gains three kinds of check (`magic`, `present`, `keys`) next to the md5 one. The `xbox`, `ps3` and `wiiu` ROM folders decide whether each system is judged; Cemu's `mlc01/` does not count as games.

### Changed

- The shipped profiles moved from `warpzone/profiles/` to `warpzone/data/profiles/`, so the folder no longer shares its name with the `warpzone/profiles.py` module, and building the wheel no longer warns about packages missing from the setuptools configuration. Nothing changes for users: profiles are still loaded by name.

## [0.5.0] - 2026-10-27

### Added

- `warpzone doctor [--config PATH]`: a checklist for the machine it runs on, in the spirit of `flutter doctor`. One line per check (`[ok]`, `[! ]` warning, `[!!]` problem, `[--]` optional and not set up) with a short detail and, for every problem, the exact fix for the platform (Homebrew, apt, dnf, pacman, or a container on SteamOS). It checks warpzone and Python, the config file and which tables it has (and what each enables), the local Syncthing and whether its REST API answers on the loopback address, the inbox tools (`chdman`, `bsdtar`, `RAHasher`, `dolphin-tool`, `nsz`) with their version lines, the DAT folder and the age of the newest DAT, the hub folder with its free space, and whether `RA_API_KEY` is set. Offline apart from that one local request, never installs or changes anything, never prints a key. Exit 1 only when a required check fails.

## [0.4.0] - 2026-10-27

### Added

- `warpzone inbox [--root PATH] [--dats PATH] [--apply]`: files new games dropped in `inbox/` into the library. Report only by default. For each item (a file or folder; `.zip`, `.7z` and `.rar` are unpacked into a local work folder, with unsafe members refused) it identifies every file by content, with size and CRC32 and SHA1 where the DAT has it, against Redump and No-Intro DATs in clrmamepro format (libretro-database's `metadat/`); converts to the library's format (CHD for PlayStation, PlayStation 2, Saturn and Dreamcast, a plain `.iso` for PSP, a zip named after the DAT for cartridge systems, `.iso` or `.rvz` kept for GameCube); and places it in `library/roms/<system>/` without ever replacing a file. Multi-disc games go in a `<game>.m3u/` folder with a playlist, and only when every disc is there. Originals move to `inbox/_done/<date>/` (folders older than 7 days are deleted on `--apply`); anything that cannot be placed moves to `inbox/_revisar/` with a `<item>.reason.txt`. The report gives each item's outcome, final path, RetroAchievements status and the whitelist line to add; warpzone never edits a whitelist. Optional RetroAchievements check with RAHasher (the hash must survive the conversion) and a lookup with `RA_API_KEY`. New `[inbox]` config table (`root`, `dats_dir`, `work_dir`, `chdman`, `bsdtar`, `rahasher`, `dolphin_tool`). Switch, MAME/arcade, Vita, PS3 and Xbox are not supported yet and go to review.

## [0.3.0] - 2026-10-27

### Added

- `warpzone bios [--root PATH] [--system NAME ...] [--all] [--overrides PATH]`: read-only check of the BIOS and firmware files of a library against a manifest shipped in the package. It reports, per system, what is ok, missing (required or optional) or has a wrong hash, and counts the unknown files in `bios/`; exit 1 if a required item is missing or wrong. The manifest covers PlayStation, PlayStation 2, Saturn, Dreamcast, Sega CD, PC Engine CD, 3DO, Game Boy Advance, Nintendo DS, Lynx and the MAME sets (Neo Geo, Atomiswave, NAOMI and device sets), and holds only file names, sizes and hashes; warpzone never downloads a BIOS. Files are hashed only when their size matches, "one of" items accept any regional dump, systems whose ROM folders hold no games are shown as "no games, not checked" and never fail the run (`--system` forces a check), and an optional local `bios.toml` next to the config accepts extra hashes ("accepted (local)") or skips items.

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

[Unreleased]: https://github.com/dagonco/warpzone/compare/v0.5.0...HEAD
[0.5.0]: https://github.com/dagonco/warpzone/compare/v0.4.0...v0.5.0
[0.4.0]: https://github.com/dagonco/warpzone/compare/v0.3.0...v0.4.0
[0.3.0]: https://github.com/dagonco/warpzone/compare/v0.2.0...v0.3.0
[0.2.0]: https://github.com/dagonco/warpzone/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/dagonco/warpzone/releases/tag/v0.1.0
