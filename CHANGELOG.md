# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog 1.1.0](https://keepachangelog.com/en/1.1.0/), and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- The home page of the library UI has two new shelves, hidden when empty, from `GET /api/v1/recommendations`. "Unfinished" lists games you started (at least 10 minutes of play time, or an achievement unlocked) and left at least 14 days ago, without a RetroAchievements award. "Because you play X" starts from the game you touched most recently (within 90 days) and lists games you have not played that share a genre or the developer with it, closer release years scoring higher. Both prefer games that are on a console, then games that fit in the free space a console reported, and show at most 12 games, one per title across systems. They use only data the hub already holds (play history, the RetroAchievements cache and the ES-DE gamelists), with no requests to other machines.
- `warpzone catalog --dats [--dats-dir PATH] [--force] [--dry-run]` fills the DAT serials of the games already in the library that have none, from the DAT folder `inbox` uses, so the Flycast memory cards of Dreamcast games filed before serials were recorded can be matched to their game. It matches only by exact identity (the DAT name `inbox` stored, the SHA-1 and size of the file, the exact file name, or the exact names of the discs of an `.m3u`), leaves the rest unmatched, and writes only under `catalog/`.
- `warpzone whitelist restore <device> [backup] [--dry-run]` and a History button on each console of the library UI put an earlier whitelist back. Every change to a device's whitelist already kept the file it replaced under `catalog/whitelists/<device>/` (50 per device). Without a backup the command lists them, newest first, with how each differs from the current file; with one it shows the diff and the paths that turn on and off, warns when paths come back on (the console downloads them again), backs up the current file and writes through the same guarded write as the UI (refused if the whitelist changed since it was read). The UI lists the backups with their diff, the games and bytes a restore changes, and a Restore button; it uses new endpoints under `/api/v1/devices/{device}/` with the usual `expect` etag, token and origin checks. An empty backup (taken before the whitelist existed) cannot be restored. See [History and restore](https://dagonco.github.io/warpzone/development/design/library-ui/#history-and-restore).
- The hub image ships `chdman` (Debian's `mame-tools`) and `bsdtar` (`libarchive-tools`), so `warpzone inbox` can convert discs to CHD and unpack `.7z` and `.rar` in the container. The image grows by about 250 MB, most of it the graphics and sound libraries `mame-tools` depends on (SDL2, Mesa, LLVM); `dolphin-tool`, `nsz`, `orbis-pkg-util` and Vita3K are still not in it. Rebuild the image to get them. See [Where inbox runs](https://dagonco.github.io/warpzone/setup/inbox/#where-inbox-runs).

- The play queue of the library UI has its backend: `GET /api/v1/queue` and `POST /api/v1/queue` keep an ordered list of the games to play next in `catalog/queue.json` on the hub. Queueing a game for a console turns it on in that console's whitelist in the same request (the queue is written first, so a failure leaves a state the page can repair); finishing, dropping or removing an entry proposes turning it off again, only on the consoles where the queue turned it on, and only when you confirm. The queue is written atomically with an `expect_queue` etag, keeps 50 backups in `catalog/queue-backups/`, keeps finished and dropped entries forever (a write that would push the file past 4 MiB is refused), and reports a game that left the library with relink candidates instead of dropping it. The page (the Queue button and the Up next section) comes next. See [Play queue](https://dagonco.github.io/warpzone/development/design/play-queue/).

- `warpzone inbox` files roms no DAT knows (homebrew, translations, aftermarket releases) when you say so: a file or archive dropped in `inbox/_unverified/<system>/` (`nes`, `snes`, `n64`, `gb`, `gbc`, `gba`, `genesis`, `mastersystem`, `gamegear`) that no DAT matches is filed as `library/roms/<system>/<file name>.zip` (an archive that holds exactly one rom gives the entry its own name, so name it the way you want the game called), after checking that its extension fits the system and, for `nes`, that it has the iNES header. A file a DAT does match is filed the normal way, and nothing is overwritten. The report says `would place (unverified)` and `not in any DAT (filed from _unverified)`, and the catalog marks the game with the flag `unverified`. See [Homebrew and other roms no DAT knows](https://dagonco.github.io/warpzone/setup/inbox/#homebrew-and-other-roms-no-dat-knows).

### Changed

- `warpzone prune` no longer lists the files EmuDeck and the emulators write by themselves among the ignored files it kept (`roms/*/metadata.txt` and `systeminfo.txt`, `bios/PPSSPP`, every `readme.txt` under `bios/`, the Flycast files in `bios/dc`, and the other emulator-owned `bios/` folders the profile ignores). They are still kept, and now counted in one line, `N file(s) emulators and EmuDeck generate themselves`, so the files it lists are the unusual ones. See [Files the console generates](https://dagonco.github.io/warpzone/development/design/prune/#files-the-console-generates).

### Fixed

- The library UI finds the description, name and cover of a game whose frontend reads a folder below the system's one, such as the PS4 games (ES-DE lists `roms/ps4/shortcuts/`, so its gamelist and media say `./Bloodborne.desktop` where the catalog says `./shortcuts/Bloodborne.desktop`). Before, those games showed no scraped data and no image although both were on the hub. The server now also tries the catalog's gamelist path with its leading folders dropped, after the exact path; nothing changes for the systems that already matched. See [Metadata and media](https://dagonco.github.io/warpzone/development/design/library-ui/#metadata-and-media).
- `warpzone inbox` identifies NES dumps whose 16-byte iNES header differs from the one No-Intro lists. A file that starts with `NES\x1a` and matches no entry whole is tried again without its header against the DAT's headerless (`.unh`) entries by SHA-1, and on a match it is filed under the DAT's game name with its own header unchanged. Before, such a dump was reported as "no DAT match". See [Identification by content](https://dagonco.github.io/warpzone/development/design/inbox/).
- A shadPS4 with no version installed or selected in its Qt launcher is no longer silent. The launch script (rewritten by `warpzone provision device`) shows its error in a dialog (`zenity`, else `kdialog`, else `notify-send`) that closes by itself and appends it to `~/.local/state/warpzone/shadps4-launch.log`; before, ES-DE showed nothing and the game just did not start. `warpzone doctor` reports `shadPS4 version` on a device whose profile has `[shadps4.launch]`: a warning while the device holds no PS4 games, a problem once it does, with the fix (open the Qt launcher, Version Manager, download and select a version).

## [0.4.0rc1] - 2026-11-10

The library gets a web app. `warpzone serve` runs it on the hub: a catalog of the whole library with covers, videos, descriptions, achievements, play statistics, saves and the disk of each console, where you turn games and whole systems on and off per console by rewriting the device whitelists. Behind it are a catalog index of the library (`warpzone catalog`) and RetroAchievements hashes for far more systems, a hub container for a NAS with an old Python, and `inbox` support for PS4 updates and DLC. A console now also deletes what you turn off by itself, with a guard that holds back a suspicious removal.

**Upgrading from 0.3.0:** nothing breaks, but a few steps unlock the new parts. Run `warpzone whitelist migrate` on the hub before the library UI can change a device's whitelist (until then it refuses writes with `not-managed`). Devices with the updater timer move to 0.4.0 and get the new provisioning on their own; any other device needs `provision device` again. That run installs the `warpzone-prune` timer (a device that lists `library` now deletes what leaves its whitelist, so check the whitelist first or set `auto_prune = false`), adds the ignore that keeps videos on the hub, and sets shadPS4's DLC folder. The disk report of a console starts with its next `play-state sync` or `frontend refresh`. To run the UI in a container, follow [The hub container and the library UI](https://dagonco.github.io/warpzone/setup/hub-container/).

### Added

- `warpzone serve [--listen HOST:PORT]` serves the library UI from the hub, with plain HTML, CSS and JavaScript (no build step, standard library only), for a phone, a desktop and a handheld with a d-pad. It has an arcade look with a neon accent per system, a "Now showing" carousel and a "Continue playing" row, a search that groups the versions of a title, a page for each game, per-console switches for a game or a whole system (a system change asks for confirmation and says how much it would download or remove), the progress and disk of each console, and a rescan of the library. A change rewrites the device whitelists, is refused if a whitelist changed since the client read it, and backs the file up first. The server needs a token of at least 16 characters (`WARPZONE_UI_TOKEN` or `[serve] token_file`), rate-limits wrong tokens, drops idle connections, and accepts a cookie from the `https://` origin of a TLS reverse proxy. New optional `[serve]` table with `listen` and `token_file`.
- The game pages show more of what the hub knows: the game's video (muted, nothing downloaded until it is played), the title screen, back cover, cartridge or disc scan and 3D box, regions and languages read from the No-Intro or Redump tags, a seal for a dump `inbox` matched to a DAT, the play history per console with favourites and total playtime, the saves the hub holds, and an achievements wall. The media endpoint serves ES-DE `videos` and `backcovers` and answers HTTP `Range` requests, which Safari and iOS need to play a video.
- Play statistics. A device publishes the playtime ES-DE records as an optional `playtime` field in its play-state ledger (the ledger version stays `1` and an older warpzone ignores the field), and the API adds it up across devices. See [Play state](https://dagonco.github.io/warpzone/development/design/play-state/).
- Saves per game: the API says whether the hub holds save data for a game, how many save states and the newest modification time. Matching is exact and read-only for RetroArch, melonDS, Flycast, Vita3K, shadPS4 and Eden; shared memory cards and emulators that name saves by an ID the catalog does not record are left out. A Dreamcast game's Flycast card is named after the disc's product number, matched through the new optional `serials` list of the catalog that `warpzone inbox --apply` records from the Redump DAT, so Dreamcast games filed before this release show no saves until they are filed again. See [Saves per game](https://dagonco.github.io/warpzone/development/design/library-ui/#saves-per-game).
- Disk per console: a console that lists `play-state` writes `<device>.status.json` with the size and free space of its library disk, only when the free space moved by 1% or a day passed, and the UI shows it as a bar. A console that never reported, or reported over 7 days ago, shows nothing. See [Disk space](https://dagonco.github.io/warpzone/development/design/library-ui/#disk-space).
- `warpzone catalog [--root PATH] [--dry-run]` builds the catalog index of the library under `catalog/` at the hub root (outside every Syncthing folder, so no console receives it): one file per system with each game, the library paths it needs, its size, its SHA-1 and a title key that groups its versions across systems. It is incremental and writes only under `catalog/`. `warpzone inbox --apply` keeps the index up to date for the systems it placed games in; a failure there is a warning, never a review.
- RetroAchievements (RA) in the catalog. `warpzone inbox --apply` records the RA hash of each game it files when `[inbox] rahasher` is set. `warpzone catalog --ra-hash [--rahasher PATH]` hashes the games already in the library, in a way that can be stopped and run again, and `--ra-lists [--force]` downloads the RA game list of each console the library has (the key comes from `RA_API_KEY` and is never printed). Hashes and lists now cover every system RA has sets for that the library can hold, not only the 15 systems `inbox` files; Wii, Wii U, Xbox, PS3, Switch, PS4 and Vita are not covered because RA has no sets for them. See [RetroAchievements](https://dagonco.github.io/warpzone/development/design/library-ui/#retroachievements).
- `warpzone catalog --ra-achievements [--force]` downloads the achievement list and badge images of each library game RA has achievements for. With the new optional `[ra] user = "<RA user name>"` it also downloads that user's progress, awards and unlock dates; without it the achievements are listed with no progress. A failed call is reported by kind and never fails the pass, and `warpzone serve` does a bounded share on every background run. See [Achievements and progress](https://dagonco.github.io/warpzone/development/design/library-ui/#achievements-and-progress).
- Unattended prune: a game turned off for a console now leaves it by itself. `warpzone prune <device> --apply --unattended` has no size cap (one PS4 game is larger than the default 50 GiB) and instead deletes nothing, in any folder, when a folder would lose more than half of what the device holds in it (a whitelist emptied by mistake, a library folder that briefly looks wrong); it says why and exits `1`, and the removal waits for a `prune --apply` by hand. A folder that is still pulling deletes nothing and exits `0`. Every other safety rule of `prune` holds, and `--unattended` refuses `--max-gib` and `--force`.
- `provision device` installs the `warpzone-prune` systemd user timer on a device that lists `library` (15 minutes after boot, then every hour). The new `auto_prune = false` device key turns it off and removes the units. `warpzone doctor` warns, in a new `Unattended prune` line, when the guard has held a removal back, and names the folders and how long ago. See [Whitelists](https://dagonco.github.io/warpzone/setup/whitelists/#automatic-pruning).
- `warpzone whitelist migrate [device ...] [--root PATH] [--dry-run]` converts the hand-written `patterns-<device>.stignore` files on the hub into the managed format the library UI writes. Before writing it drops lines with no effect, proves on the hub's `library/` that exactly the same paths stay on, saves the old file under `catalog/whitelists/<device>/` and has the hub's Syncthing scan the new one. A file with a wildcard or any other pattern it cannot read literally is refused with its line numbers and left as it is; hand comments are lost.
- `exclude_systems = ["ps4", "switch"]` under a device lists the systems it does not run (default: none). `warpzone check` reports a whitelist that names one and takes `--root PATH` to read the whitelists on a mounted hub.
- `warpzone inbox` places PS4 updates and DLC for shadPS4 (with `orbis-pkg-util`). An update goes to `library/roms/ps4/games/<CUSAnnnnn>-UPDATE/` and needs the game in the library; a newer update replaces the one there, the same version is kept and an older one goes to review. DLC goes to `library/roms/ps4/dlc/<CUSAnnnnn>/<entitlement label>/`, for the game named by the inbox folder it was dropped in or else by its own title id. See [PS4 updates](https://dagonco.github.io/warpzone/setup/inbox/#ps4-updates) and [PS4 DLC](https://dagonco.github.io/warpzone/setup/inbox/#ps4-dlc).
- `provision device` points shadPS4's DLC folder at `library/roms/ps4/dlc` through the new optional profile key `[shadps4] addon_dir` (set in `steamos-emudeck`), replacing the old value after a backup and warning when DLC installed by hand sits in the folder it stops reading. `warpzone doctor` flags a config whose DLC folder differs. See [shadPS4 install directory](https://dagonco.github.io/warpzone/guides/emudeck/#shadps4-install-directory).
- `docker/Dockerfile` and `examples/compose.example.yaml` run `warpzone serve` and `warpzone catalog` on the hub in a container, for a NAS whose own Python is older than 3.11. The image ships RAHasher (with CHD support) and runs as the uid and gid that own the games share. See [The hub container and the library UI](https://dagonco.github.io/warpzone/setup/hub-container/).
- The docs explain the two places `warpzone inbox` runs, on a computer that mounts the hub's share or in the hub container, and `warpzone doctor` detects which one is in use and checks it (a new `Where inbox runs` group, including the hub root, the work folder and the mounted DATs). Nothing changes on a computer. See [Where inbox runs](https://dagonco.github.io/warpzone/setup/inbox/#where-inbox-runs).

### Changed

- The devices that receive the whole `wz-media` folder now ignore `/*/videos`, so videos stay on the hub. A device that already has the folder gets the ignore on its next `provision device`; the videos it already holds stay where they are (delete `media/*/videos` by hand to free the space).

## [0.3.0] - 2026-11-06

Consoles now keep themselves on a working release, the inbox takes in many more systems, warpzone supports a second device profile (the Anbernic RG351MP), and favourites, play statistics and Nintendo DS saves travel between devices. This section sums up the six release candidates; each of them, below, has the full detail.

**Upgrading from 0.2.0 needs a reinstall of each device** with `sh <clone>/warpzone/data/updater.sh install <device>`: `pipx` and wheel installs are no longer supported and are not migrated. See [Installing a device](https://dagonco.github.io/warpzone/operation/self-update/#installing-a-device).

### Added

- Autonomous updates. A systemd user timer fetches new releases (every hour on the `rc` channel, every 12 hours on `stable`), checks each one (it imports, `provision device --unattended` works, `warpzone health` passes) and goes back to the previous release when a check fails. It honours release channels, a `pin`, withdrawn releases (`retired/vX.Y.Z` tags) and signed tags, and waits while the frontend is open. `warpzone self-update` runs it by hand (`--now` installs at once, `--channel stable|rc` switches channel), and a profile can set `[updater] install = "manual"` so the timer only notes a release. See [Updating a device](https://dagonco.github.io/warpzone/operation/self-update/).
- `warpzone health [device]` checks that a version works on a device, and `provision device --unattended` is the provisioning run nobody watches.
- `warpzone inbox` takes in PS4 `.pkg` games (with `orbis-pkg-util`), PS Vita dumps, licences and `.pkg` games (with Vita3K and a zRIF file), decrypted 3DS images, Wii U `.wua` files and Wii and GameCube `.rvz`/`.wia` images (read from their header, with no tool). Games reach shadPS4 and Vita3K with no manual step (`[shadps4]`, `[links.<id>]`, `warpzone links sync`). `inbox --apply` takes a lock so two runs cannot work on one inbox. See [Adding games](https://dagonco.github.io/warpzone/setup/inbox/).
- A second shipped profile, `anbernic-rg351mp`, with scraped covers and descriptions converted for its frontend (`warpzone frontend refresh`) and a Warpzone entry in its Options menu that shows the status, runs doctor, installs updates and switches channel. See [the Anbernic guide](https://dagonco.github.io/warpzone/guides/anbernic/).
- Favourites and play statistics travel between frontends and devices through the two-way `wz-play-state` folder (`warpzone play-state sync`). See [Favourites and play statistics](https://dagonco.github.io/warpzone/guides/anbernic/#favourites-and-play-statistics).
- Nintendo DS saves travel between DraStic on the Anbernic and melonDS on SteamOS: `warpzone saves convert` runs around each DraStic session and on the frontend timer. See [Save conversion](https://dagonco.github.io/warpzone/development/design/save-conversion/).
- `auto_hub = true` under a device, and `warpzone provision hub --additive`, create on the hub the folders a device needs and the hub lacks, and never change existing ones.
- `warpzone settings` covers Xenia Canary, its console language included.
- A documentation website at <https://dagonco.github.io/warpzone>, and agent skills in `.claude/skills/` for the common tasks.

### Changed

- **Breaking:** a device is installed only from a clone, with `updater.sh install`, and `provision device` installs the updater and its timer unless `self_update = false`. Run `provision device` again on each existing device after reinstalling.
- `warpzone doctor` has many new checks (updater, artwork, play state, links, shadPS4, RetroArch, Cemu paths, save conversion, Vita3K), so it can warn about a device that was fine on 0.2.0 until it is provisioned again.

### Fixed

- PS4 games no longer crash when started from ES-DE: `provision device` starts them through shadPS4's SDL emulator.
- On SteamOS, `provision device` sets Cemu's MLC and game paths, so Wii U saves land in the synced folder, and the files RetroArch cores write into the BIOS folder are ignored instead of showing up as local changes.
- `provision` no longer times out while a folder is downloading, and `prune` stops on a busy folder instead of deleting anything.
- Many updater fixes around signatures, retirements, rollbacks and stale bytecode, so a bad release or a fake tag cannot keep a device on the wrong version.

### Removed

- Installing a device with `pipx` or the release wheel.

### Security

- `warpzone/data/allowed_signers` lists the maintainer's release key: devices accept only release, candidate, pin and `retired/` tags signed with it.

## [0.3.0rc6] - 2026-11-05

Sixth release candidate of 0.3.0: Nintendo DS saves travel between the Anbernic's DraStic and melonDS on the Steam Deck and Steam Machine, `warpzone settings` covers Xenia, the Anbernic's Warpzone menu shows install progress and keeps looking for releases, and `provision` no longer times out while a folder is downloading.

### Added

- Nintendo DS saves travel between DraStic on the Anbernic RG351MP and melonDS on the Steam Deck and Steam Machine. The new `warpzone saves convert <device> [--dry-run] [--quiet]` reconciles the hub's raw `<game>.sav` files (now synced to `/roms/warpzone/saves/melonds` on the Anbernic) with DraStic's `/roms/nds/backup/<game>.dsv`, which is the same data plus a 122-byte DeSmuME footer. Per game, when only one side changed it is copied to the other; when both changed the most recently modified file wins and the other is kept in `~/.local/state/warpzone/save-convert/backups/<rule>.conflicts/` (apart from the routine backups, so it is not rotated away); a save that exists on one side is copied to the other, deletions are never propagated, and a file that is not a valid save (or two sides of different sizes) is skipped with a warning. It never acts while DraStic runs, writes atomically and gives each copy the date of the file it came from, so Syncthing's own conflict resolution does not prefer it over a newer save from another device; Syncthing conflict files in the saves folder are reported and never touched. `provision device` wraps the `nds` command of the system list override so the conversion runs right before DraStic starts and right after it exits (it never stops a game from starting), and `frontend refresh` runs it on the existing timer (also on a device that lists `saves` but not `frontend`). `warpzone doctor` has a new `Save conversion` line. The profile table is `[save_convert.<key>]`; see [Save conversion](https://dagonco.github.io/warpzone/development/design/save-conversion/). DraStic's own configuration is untouched, so loading an old DraStic save state still rolls the cartridge save back (set `backup_in_savestates = 0` in `drastic.cfg` to avoid it).
- `warpzone settings` covers Xenia Canary (the Proton build EmuDeck installs): its `xenia-canary.config.toml`, and `xconfig.settings` as a system data file, which is where Canary keeps the console language and country. Set the language once in Xenia (Console > Open console settings), capture with `--key 'xenia/data/*'`, and the other consoles get it at the next apply.

### Changed

- The Warpzone menu of the Anbernic comes back after each item, with the status read again, until you choose Exit (or press Back). "Install vX now" shows a progress bar with the step the update is on (looking for releases, switching, each of the three checks) instead of a still box for the whole update. When the timer's own run is in progress, Install says so and to try again in a few minutes. A new "Doctor" item runs `warpzone doctor` and shows its whole report in a box that scrolls; the menu no longer runs doctor to open, so it opens at once, and Status points at the Doctor item.

### Fixed

- On a device with `install = "manual"` (the Anbernic), the timer no longer stops looking for releases after an update installed from the menu. The check after the next restart waited for EmulationStation to close, which does not happen there, so the device stayed on probation and the timer deferred every 15 minutes without fetching, for up to 72 hours. The timer now finishes an interrupted update and runs that check without waiting for the menu, goes back if the check fails, and then notes a newer release as it always does; it still never installs one on its own.
- `provision device` and `provision hub` no longer fail with "cannot reach ... (timed out)" when a folder is downloading while they run. Syncthing answers a scan only once it has run, and it runs it only after the download ends, which on a slow device takes more than the 30-second timeout. They now report "rescan queued" and go on; Syncthing runs the scan when the download ends. `prune` stops with a "folder is busy" message instead of deleting anything, and can run again when the folder is idle.
- On SteamOS, the files RetroArch's Flycast and mame2003-plus cores write into the BIOS folder (`bios/dc/vulkan_pipeline.cache`, `bios/dc/data`, `bios/mame2003-plus/hiscore.dat` and `artwork`) are ignored, so they no longer show up as local changes of the library. Run `provision device` to apply it.

## [0.3.0rc5] - 2026-11-05

Fifth release candidate of 0.3.0: the inbox installs PS Vita `.pkg` games, the Anbernic gets one Warpzone menu that installs updates and switches channel, and Cemu saves on SteamOS land in the synced folder.

### Added

- `warpzone self-update --channel stable|rc` makes a device follow stable releases or release candidates: it changes the channel in the device's config, keeping a backup next to it, and in the updater's settings.
- `warpzone inbox` installs PS Vita `.pkg` games with Vita3K. The zRIF (licence key) of each game comes from a file you keep, named by the new `[inbox] vita_zrif` key: `CONTENT_ID<TAB>zRIF` lines, or a NoPayStation-style TSV of which only the `Content ID` and `zRIF` columns are read. Before anything runs, also in report mode, the package header, its `param.sfo` and the zRIF are checked (the zRIF must decode to that game's licence), so the report already prints the three whitelist lines. With `--apply`, Vita3K (`[inbox] vita3k`, by default `/Applications/Vita3K.app` on macOS or `vita3k` on PATH) installs the package in its portable mode inside the local work folder, never in its own data: it must be the app bundle on macOS or a plain binary on Linux, and any other Vita3K (an AppImage, a binary outside the bundle, a wrapper script) is never run; the result is checked on disk, because Vita3K reports success even when it fails, and filed like a Vita dump: the game folder, the licence and the `.psvita` launcher. Updates, DLC, themes, PlayStation Mobile packages, incomplete downloads and packages without their zRIF go to review with the reason. A zRIF is never printed or written to a reason file. `warpzone doctor` lists Vita3K among the inbox tools and warns about one inbox would refuse.

### Changed

- On the Anbernic, the Options menu has one entry, "Warpzone", instead of two: a small menu, driven by the controller, with the status, "Install vX now" when the console found a new release, and the channel. The old "Warpzone Status" and "Warpzone Update Now" entries are removed.

### Fixed

- On SteamOS with EmuDeck, `provision device` sets Cemu's MLC path to `~/Emulation/roms/wiiu/mlc01` and adds `~/Emulation/roms/wiiu/roms` to its game paths in `~/.config/Cemu/settings.xml` (a new `[cemu]` profile table), which EmuDeck leaves unset on SteamOS 3.9. Until now Cemu saved in `~/.local/share/Cemu/mlc01`, where warpzone does not sync, and its game list was empty. Only those two values change; the file is backed up first, skipped while Cemu runs, never created, and `mlc_path` stays as it is while the folder Cemu used holds game saves (`doctor` then says to move them). `warpzone doctor` reports `Cemu paths`.
- The Cemu keys folder (`wz-bios-cemu-keys`) is created once `~/.config/Cemu` exists, so `keys.txt` arrives before Cemu first runs instead of being skipped.

## [0.3.0rc4] - 2026-11-05

Fourth release candidate of 0.3.0: the Anbernic installs releases only from its Options menu, where "Warpzone Update Now" now works with the menu open, and Wii and GameCube `.rvz` images keep their own names.

### Added

- `warpzone inbox` identifies `.rvz` and `.wia` images by reading their header, with no `dolphin-tool` and no DAT: it tells a Wii disc from a GameCube one, reads the game ID and the title, and files the image unchanged as `roms/wii/<name> [<GAME ID>].rvz` (or `roms/gc/`), where the name is the file's own (for example `Game (USA) (En,Fr,Es)`) and the header's title stands in when the file name is only the game ID. A truncated copy or a header that does not check out goes to review with the reason. `dolphin-tool` is now needed only for `.gcz`.
- A profile can set `[updater] install = "manual"`: the device's timer then only looks for a release and notes it, and `warpzone self-update --now` installs it. The Anbernic profile does, because EmulationStation is open whenever that console is on: the timer waited up to 72 hours for the menu to close, so the console took days to update. "Warpzone Status" shows the release found (`New release`).
- `warpzone inbox --apply` takes a lock file, `inbox/.warpzone.lock`, so two runs (for example from two desktops that mount the hub) cannot work on the same inbox at once. A second run stops with a message saying who holds the lock and since when. A lock left behind by a run that died (older than 12 hours, or its process gone from the same machine) is reported as stale and taken over only with the new `--force-lock` option. A report run needs no lock.

### Fixed

- `warpzone self-update --now` (and the "Warpzone Update Now" entry of the Anbernic's Options menu, which is started from the menu) no longer waits for the menu to close, nor for the back-off after a failed check: it is an update asked for by hand. `updater.sh tick --now` is that update; `--fetch-now` still waits for the menu.
- `provision hub` resumes a `wz-*` folder the hub has paused even when its ignores are already right, which is how a folder looks when an additive run stopped half-way. Devices still leave such a folder alone.

## [0.3.0rc3] - 2026-11-04

Third release candidate of 0.3.0: release candidates now reach devices within the hour, hubs can get their folders from the consoles (`auto_hub`), the Anbernic shows warpzone's state in its Options menu, and this release carries the key that signs every later release.
### Added

- On the Anbernic (ArkOS) profile, `provision device` adds "Warpzone Status" and "Warpzone Update Now" to the Options menu (a new `[options_menu]` profile table pointing at `/opt/system`): the first shows the version, the updater's state and what `warpzone doctor` reports, without the network; the second runs `warpzone self-update --now`.
- `auto_hub = true` under a device lets `provision device` create on the hub the folders the device needs and the hub lacks, and `warpzone provision hub --additive` does the same from anywhere. Both only create: whitelisted folders and folders the hub already has are never created or changed, and no folder is created at a path another hub folder uses.
- `warpzone self-update --now` (and `updater.sh tick --fetch-now`) looks for a new release right away instead of waiting for the next fetch; it still waits while the menu is open.

### Changed

- The updater looks for new releases every hour on the `rc` channel and every 12 hours on `stable` (or with a pin), instead of once a day, so a release candidate reaches devices soon after it is published.

### Fixed

- An additive hub run (`provision hub --additive`, `auto_hub`) warns about a `wz-*` folder the hub has paused instead of leaving it silently, and a folder whose creation fails after it was made gives one clean warning; a reply cut short by the Syncthing API (`IncompleteRead`, `BadStatusLine`) is a Syncthing error instead of a traceback; and `hub.root` must be an absolute path.
- The updater stops with a log line when it finds no clone to work on, instead of acting on whatever folder it runs from; a retirement is looked up by name for the tags the tick considers (the running version, the pin, a version being tried or under suspicion and each valid candidate) instead of going through a limited number of `retired/` tags, so no amount of fake retirement tags can hide a real one, also after a trial that was given up; and no move to a lower version is accepted in a tick where the copy of the allowed signers failed.

### Security

- `warpzone/data/allowed_signers` now lists the maintainer's release key. A device that runs this release keeps trusting that key, and from the next release on it only accepts release, candidate, pin and `retired/` tags signed with it.

## [0.3.0rc2] - 2026-11-04

Second release candidate of 0.3.0: the documentation moves to a website, the updater gets safer around signatures and rollbacks, and a large internal clean-up lands with no change in behaviour.

### Added

- A documentation website at <https://dagonco.github.io/warpzone>, built with Starlight and published from `main`. The guides moved from `docs/` into it, and the README is now a short introduction.
- Agent skills in `.claude/skills/` for installing a console, adding games through the inbox, editing a whitelist, diagnosing a machine and publishing a release.

### Changed

- Internal clean-up with no change in behaviour: safe file writes, the inbox framework, ES-DE gamelist editing and device provisioning now live in their own modules, with more tests around concurrent edits.

### Fixed

- Updater: fake `retired/` tags can no longer push a real retirement out of the per-tick limit, a failed copy of the trusted keys no longer leaves their version recorded (and a missing copy refuses older keys), and the note that lets the updater clear its own venv is dropped once the venv is marked.
- Editing an ES-DE gamelist no longer trips on awkward but valid files. The arcade and disc inbox replace a self-closing `<altemulator/>` instead of adding a second one, and the play state sync accepts an empty `<gameList/>` and ignores `<game>` and `</gameList>` inside XML comments.
- Updater: after going back to the previous release, Python could run the cached bytecode of the bad release when both files had the same size and timestamp, so a bad release could be taken for a broken environment. The bytecode caches are now cleared on every checkout and before each check.

## [0.3.0rc1] - 2026-11-03

First release candidate of 0.3.0. Consoles can now keep themselves on a working release, the inbox takes in more systems, and warpzone supports a second device profile.

Highlights:

- **Autonomous updates.** Devices fetch new releases on their own, check them, roll back a bad one and obey release channels, a pin, signed tags and withdrawn releases.
- **More games through the inbox.** PS4, PS Vita (with its licence), 3DS and Wii U `.wua`.
- **Anbernic RG351MP profile**, with scraped covers and descriptions for frontends other than ES-DE.
- **Favourites and play statistics** shared between frontends and devices.

Upgrading from 0.2.0 needs a reinstall of each device: see the breaking items under Changed and Removed. Only devices with `channel = "rc"` follow release candidates.

### Added

Autonomous updates ([Updating a device](https://dagonco.github.io/warpzone/operation/self-update/)):

- A device updates itself. A systemd user timer starts the updater after boot and every 15 minutes; it fetches at most once a day, checks a new release (it imports, `provision device --unattended` works, `warpzone health` passes) and goes back to the previous release when a check fails, remembering the bad one so it is never tried again. It waits while ES-DE is open (at most 72 hours), a power cut does not count against a release, and `self_update = false` turns it off. See [The updater script](https://dagonco.github.io/warpzone/operation/self-update/#the-updater-script).
- Release channels: `channel = "stable"` (the default) follows `vX.Y.Z` tags and `channel = "rc"` also follows candidates, which order correctly (`v0.3.0rc1` < `v0.3.0rc2` < `v0.3.0`). See [Channel and pin](https://dagonco.github.io/warpzone/operation/self-update/#channel-and-pin).
- `pin = "vX.Y.Z"` keeps a device on one tag whatever the channel. See [Channel and pin](https://dagonco.github.io/warpzone/operation/self-update/#channel-and-pin).
- Withdrawn releases: an annotated `retired/vX.Y.Z` tag stops devices from picking that release, and a device that runs it moves to the best valid one, even a lower one. See [Retiring a release](https://dagonco.github.io/warpzone/development/releasing/#retiring-a-release).
- Signed releases: once a key is listed in `warpzone/data/allowed_signers`, devices accept only releases, candidates, pins and retirements that carry a valid SSH signature. The file ships without keys, so nothing is required yet and the log says so. See [Signing a tag](https://dagonco.github.io/warpzone/development/releasing/#signing-a-tag).
- A venv whose Python was replaced by an operating system update is made again at the next tick, with no network. See [Python repair](https://dagonco.github.io/warpzone/operation/self-update/#python-repair).
- `warpzone self-update` runs one tick of the updater by hand, exactly as the timer does (it has no `--dry-run`). See [warpzone self-update](https://dagonco.github.io/warpzone/operation/self-update/#warpzone-self-update).
- `warpzone health [device]` checks that a version of warpzone works on a device, and is the last check before an update is kept. See [Checking a version](https://dagonco.github.io/warpzone/operation/self-update/#checking-a-version).
- `warpzone provision device --unattended` for runs nobody watches: it never changes an existing folder's type, versioning or path, warns instead of failing, and exits `3` when a write waits for ES-DE or an emulator to close. See [Unattended provisioning](https://dagonco.github.io/warpzone/operation/self-update/#unattended-provisioning).
- `warpzone doctor` checks the timer, the updater's live copy and settings, and its state (a recent rollback, a stuck or dirty checkout, a retired pin, signatures not required, no success for 14 days). See [The updater script](https://dagonco.github.io/warpzone/operation/self-update/#the-updater-script).
- Two optional keys under `[devices.<name>]`, `channel` and `pin`, validated when the config is loaded and shown in `examples/config.example.toml`. See [Channel and pin](https://dagonco.github.io/warpzone/operation/self-update/#channel-and-pin).

Games ([Adding games](https://dagonco.github.io/warpzone/setup/inbox/)):

- `warpzone inbox` installs PS4 games: it recognises `.pkg` files (also inside an archive), and with [`orbis-pkg-util`](https://crates.io/crates/orbis-pkg-util) extracts a base game into `library/roms/ps4/games/<CUSAnnnnn>/` with an ES-DE shortcut. Updates and DLC go to review, and without the tool the `.pkg` is filed as before. See [PS4 games](https://dagonco.github.io/warpzone/setup/inbox/#ps4-games).
- `warpzone inbox` installs PS Vita games from a decrypted dump (folder or archive) into `library/roms/psvita/app/<TITLEID>/`, with a launcher file for ES-DE and Vita3K. Updates, DLC, Vitamin dumps and other cases that Vita3K cannot run go to review with the reason. See [PS Vita games](https://dagonco.github.io/warpzone/setup/inbox/#ps-vita-games).
- `warpzone inbox` files the licence a Vita game needs (`work.bin` or a `.rif`), checks it and puts it in `library/roms/psvita/license/<TITLEID>/`. A game that is encrypted and has no licence goes to review instead of failing silently in Vita3K. See [PS Vita games](https://dagonco.github.io/warpzone/setup/inbox/#ps-vita-games).
- `warpzone inbox` recognises 3DS images (`.3ds`, `.cci`) by their headers and files decrypted ones as `.cci`; an encrypted image, which Azahar cannot open, goes to review with that reason. See [3DS games](https://dagonco.github.io/warpzone/setup/inbox/#3ds-games).
- `warpzone inbox` recognises Wii U `.wua` files (Cemu) by their footer, also inside an archive, and a damaged one goes to review. See [Wii U images](https://dagonco.github.io/warpzone/setup/inbox/#ps3-ps4-xbox-xbox-360-and-wii-u-images).
- Games reach the emulators with no manual step. `provision device` adds the PS4 games folder to shadPS4's install directories (profile table `[shadps4]`), and `provision device`, the new `warpzone links sync <name>` and an ES-DE startup script keep one symlink per Vita game and licence inside Vita3K's own folders (profile table `[links.<id>]`). Only links are touched, never while the emulator runs. See [shadPS4 install directory](https://dagonco.github.io/warpzone/guides/emudeck/#shadps4-install-directory) and [Vita3K games](https://dagonco.github.io/warpzone/guides/emudeck/#vita3k-games).

Other devices and frontends:

- A second shipped profile, `anbernic-rg351mp`, for an Anbernic RG351MP running dArkOS with an exFAT card: the library in `/roms/library`, a system list override and the RetroArch BIOS, save and state paths, written only while the frontend is closed and backed up first. Profiles gain `ignore_perms`, `[es_systems]`, `[retroarch]` and `create_when` for it. See [the Anbernic guide](https://dagonco.github.io/warpzone/guides/anbernic/).
- Scraped covers and descriptions for a frontend that is not ES-DE (`[artwork]` in a profile, used by the Anbernic profile). The device receives only the covers and gamelists of the systems it has, `provision device` converts them to the frontend's own format keeping favourites and play counts, and `warpzone frontend refresh` (also run by a timer) renews them while the frontend is closed. See [Artwork](https://dagonco.github.io/warpzone/guides/anbernic/#artwork).
- Favourites and play statistics travel between frontends and devices through a new two-way folder, `wz-play-state`, that a device joins by listing `play-state` in `folders`. Removing a favourite sticks, play counts add up across devices without counting a play twice, and `warpzone play-state sync` (also run by a timer and at ES-DE startup) applies it. See [Favourites and play statistics](https://dagonco.github.io/warpzone/guides/anbernic/#favourites-and-play-statistics) and [Play state](https://dagonco.github.io/warpzone/development/design/play-state/).

### Changed

- **Breaking:** a device is installed only from a clone, with `sh <clone>/warpzone/data/updater.sh install <device>` (options `--config`, `--venv`, `--channel stable|rc`). It checks out the best release, makes the venv, installs the updater and its timer, and runs `provision device`. Existing `pipx` or wheel installs are not migrated: reinstall with this command. See [Installing a device](https://dagonco.github.io/warpzone/operation/self-update/#installing-a-device).
- **Breaking:** `provision device` now also installs the updater and its timer on every device that has not set `self_update = false`, and the timer runs `updater.sh tick` (the script that ships with the version, copied outside the clone). When warpzone does not run from a git clone it installs nothing and warns. Run `provision device` again on each existing device. See [The timer](https://dagonco.github.io/warpzone/operation/self-update/#the-timer).
- `warpzone doctor` has new checks (updater, artwork, play state, links, shadPS4, RetroArch and the Anbernic system list), so it can warn about a device that was fine on 0.2.0 until it is provisioned again. See [the doctor guide](https://dagonco.github.io/warpzone/operation/doctor/).
- The whitelist template and [Whitelists and pruning](https://dagonco.github.io/warpzone/setup/whitelists/) say that Xenia and Model 2 keep their data inside the system folder, so naming `xbox360/roms` and `model2/roms` already covers the games and leaves the emulator data on the device.
- The end-to-end test also starts from an empty hub and checks that the folders, ignores and a whitelisted game arrive. This is a contributor change. See [AGENTS.md](AGENTS.md).

### Fixed

- PS4 games no longer crash on a console when started from ES-DE: EmuDeck's Qt launcher can exit with 139 on the arguments it passes, so `provision device` starts the game through the SDL emulator of the version selected in the launcher (EmuDeck's own script is never edited, and `doctor` warns when EmuDeck puts its entry back). See [shadPS4 launch script](https://dagonco.github.io/warpzone/guides/emudeck/#shadps4-launch-script).
- `warpzone play-state sync --dry-run` no longer creates its lock file. See [the design document](https://dagonco.github.io/warpzone/development/design/play-state/).

### Removed

- Installing a device with `pipx` or the release wheel is no longer supported, and the README no longer documents it. See [Installing a device](https://dagonco.github.io/warpzone/operation/self-update/#installing-a-device).

## [0.2.0] - 2026-11-02

### Added

- Emulator settings layer. The hub keeps key-level layers (`settings/common.toml`, `settings/<device>.toml`) in a new two-way folder `wz-settings` that a device joins with the `settings` group, and the `steamos-emudeck` profile declares the settings files of eleven emulators (Eden, RetroArch, DuckStation, PCSX2, Dolphin, Azahar, melonDS, PPSSPP, Vita3K, xemu, Ryujinx). `warpzone settings capture` takes a setting changed in an emulator's own interface to the layers (`--seed` fills them from a console that is already configured), `warpzone settings apply` writes the layers into the emulators' files and `warpzone settings audit` reports the drift. Apply never writes while the emulator runs, never overwrites a change that was not captured (unless `--overwrite-local`), changes only the layered keys, backs up each file first and touches nothing when a layer is invalid. Logins never reach the hub. See [Emulator settings](https://dagonco.github.io/warpzone/setup/settings/).
- `provision device` installs an ES-DE startup script that runs `settings apply`, and turns on ES-DE's custom event scripts when ES-DE is closed. `warpzone doctor` checks the layers, the script and the last apply.

### Fixed

- Syncthing as a flatpak (SyncThingy) cannot see other apps' `~/.var/app/<app>` folders, so the RetroArch, Dolphin, PPSSPP, PrimeHack and Flycast save folders looked empty to it and synced nothing. `provision device` now adds the missing `flatpak override --user --filesystem=~/.var/app/<app>` for each of them (idempotent, reported in `--dry-run`) and asks for a Syncthing restart. `warpzone doctor` checks the overrides and prints the command for each missing one.
- The Syncthing unit that `warpzone doctor` suggests for the SyncThingy flatpak now has `ExecStop=/usr/bin/flatpak kill ...`. Without it `systemctl --user stop` leaves the flatpak process running and a restart loops in `activating (auto-restart)`. `doctor` warns about a unit in that state and shows the fix, and the unit is documented in [the EmuDeck guide](https://dagonco.github.io/warpzone/guides/emudeck/).
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

[Unreleased]: https://github.com/dagonco/warpzone/compare/v0.4.0rc1...HEAD
[0.4.0rc1]: https://github.com/dagonco/warpzone/compare/v0.3.0...v0.4.0rc1
[0.3.0]: https://github.com/dagonco/warpzone/compare/v0.2.0...v0.3.0
[0.3.0rc6]: https://github.com/dagonco/warpzone/compare/v0.3.0rc5...v0.3.0rc6
[0.3.0rc5]: https://github.com/dagonco/warpzone/compare/v0.3.0rc4...v0.3.0rc5
[0.3.0rc4]: https://github.com/dagonco/warpzone/compare/v0.3.0rc3...v0.3.0rc4
[0.3.0rc3]: https://github.com/dagonco/warpzone/compare/v0.3.0rc2...v0.3.0rc3
[0.3.0rc2]: https://github.com/dagonco/warpzone/compare/v0.3.0rc1...v0.3.0rc2
[0.3.0rc1]: https://github.com/dagonco/warpzone/compare/v0.2.0...v0.3.0rc1
[0.2.0]: https://github.com/dagonco/warpzone/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/dagonco/warpzone/releases/tag/v0.1.0
