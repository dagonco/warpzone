---
title: "Configuration (TOML)"
description: "Every key of the private config file and of a device profile."
---

warpzone reads TOML in four places. This page lists every table and key each one accepts, with its type, default and validation rules, taken from the code.

| File | Written by | Where | Section |
|---|---|---|---|
| The private config | you | `$XDG_CONFIG_HOME/warpzone/config.toml` (default `~/.config/warpzone/config.toml`) | [The private config](#the-private-config) |
| A device profile | shipped with warpzone | `warpzone/data/profiles/<name>.toml` | [Device profiles](#device-profiles) |
| The settings layers | `settings capture`, or you on the hub | `settings/common.toml` and `settings/<device>.toml` in the hub's `settings` folder | [Settings layers](#settings-layers) |
| The updater settings | `provision device` | `~/.local/state/warpzone/updater/conf` | [The updater settings file](#the-updater-settings-file) |
| Local BIOS overrides | you | `bios.toml` next to the config | [`bios.toml`](#biostoml-local-overrides) |

Every table rejects unknown keys, so a typo is an error and not a silently ignored setting. Validation collects all problems and reports them together, each prefixed with the file name and the key path (`devices.deck.folders: ...`). A config error exits with code `2` (see [Commands](/warpzone/reference/commands/#exit-codes)).

## The private config

Copy [`examples/config.example.toml`](https://github.com/dagonco/warpzone/blob/main/examples/config.example.toml) to the path above and fill it in. It holds the hub's API key and device IDs, so keep it out of any repository. The file is found in this order: the `--config PATH` option, the `WARPZONE_CONFIG` environment variable, then `$XDG_CONFIG_HOME/warpzone/config.toml`, then `~/.config/warpzone/config.toml`. A missing file is an error, except that `warpzone inbox` treats a missing file as "all defaults" for its `[inbox]` table.

Top-level tables: `[hub]` (required), `[devices.<name>]` (at least one), `[inbox]` (optional), `[serve]` (optional) and `[ra]` (optional). Anything else is an unknown key.

### `[hub]`

The hub is the machine that holds the library and runs the authoritative Syncthing.

| Key | Type | Default | Required | Rules |
|---|---|---|---|---|
| `name` | string | none | yes | Non-empty after trimming. The hub's display name in Syncthing on the devices. |
| `device_id` | string | none | yes | A Syncthing device ID: 8 groups of 7 characters from `A-Z` and `2-7`, separated by `-`. Must differ from every device's ID. |
| `root` | string | none | yes | Non-empty and absolute (a relative path is a config error). The absolute path of the hub root on the hub's own filesystem (the folder holding `library/`, `saves/`, `frontend/`, `songs/`, `tools/` and `settings/`), as seen by the hub's Syncthing, not by the machine running warpzone. A trailing `/` is removed. |
| `api_url` | string | none | yes | Must start with `http://` or `https://`. The base URL of the hub's Syncthing GUI and API. A trailing `/` is removed. |
| `api_key` | string | none | one of the two | Non-empty. The hub's API key, inline. |
| `api_key_file` | string | none | one of the two | A path to a file holding the key (`~` is expanded). The file must be readable and not empty; its contents are trimmed. Keep it mode 600 and out of any repository. |
| `addresses` | array of strings | `["dynamic"]` | no | Non-empty, every item a non-empty string. Where devices find the hub; set explicit `tcp://host:22000` addresses if discovery is off. |

Set exactly one of `api_key` and `api_key_file`: both or neither is an error. API keys are never printed or logged; dry runs redact them.

### `[devices.<name>]`

One table per console. The table name is the device's name, used in commands (`warpzone provision device <name>`) and in whitelist file names (`patterns-<name>.stignore`). It must match `^[a-z0-9][a-z0-9_-]*$` (lowercase letters, digits, `-` and `_`, starting with a letter or digit). At least one device is required.

| Key | Type | Default | Required | Rules |
|---|---|---|---|---|
| `device_id` | string | none | yes | Same format as the hub's. Must be unique across the hub and all devices. `provision device` refuses to run if the local Syncthing is a different device. |
| `profile` | string | none | yes | Non-empty and the name of a [shipped profile](#device-profiles) (`anbernic-rg351mp`, `steamos-emudeck`). |
| `folders` | array of strings | none | yes | Non-empty. Each item must be one of the folder groups below; repeats are dropped. The profile must define where each group lives (see [`[paths]`](#paths)), or every command that loads the config refuses the device. |
| `addresses` | array of strings | `["dynamic"]` | no | Same rules as the hub's. Where the hub finds this device. |
| `rescan_seconds` | integer | `300` | no | At least `5` (a boolean is rejected). A whitelist edited on the hub reaches the device within seconds but is only applied on a full scan, so the whitelisted folders (`library`, `songs`) are rescanned this often. |
| `self_update` | boolean | `true` | no | Whether `provision device` installs the updater and its timer. `false` writes `enabled=false` in the [updater settings](#the-updater-settings-file) and installs nothing new. |
| `channel` | string | `"stable"` | no | `"stable"` (only `vX.Y.Z` tags) or `"rc"` (also `vX.Y.ZrcN`). |
| `pin` | string | none | no | A release tag such as `v0.3.0` or `v0.3.0rc1` (`v0.3.0-rc1` is rejected). The device stays on this tag, whatever the channel. |
| `auto_prune` | boolean | `true` | no | Whether `provision device` installs the `warpzone-prune` timer, which runs `prune --apply --unattended` every hour so that what leaves the device's whitelist is deleted by itself. Only for a device that lists `library`. `false` removes the units if they exist. See [Automatic pruning](/warpzone/setup/whitelists/#automatic-pruning). |
| `auto_hub` | boolean | `false` | no | Whether `provision device` first creates on the hub the folders this device needs and the hub lacks, using the hub's API key from this config. It never creates a whitelisted folder (`library`, `songs`) and never changes a folder the hub already has. See [Hub folders from a console](/warpzone/development/design/autonomous-updates/#hub-folders-from-a-console-auto_hub). |
| `exclude_systems` | array of strings | `[]` | no | Systems (folder names below `library/roms/`) the device does not run: plain names of lower-case letters, digits, `-` and `_`, no repeats. The default is empty, so the device runs every system the hub holds. The [library UI](/warpzone/development/design/library-ui/#supported-systems) never turns one of them on for the device, and `warpzone check` reports a whitelist that names one. |

Folder groups for `folders`:

| Group | What the device gets | Direction |
|---|---|---|
| `library` | ROMs, BIOS and extras the device's whitelist allows | receive-only |
| `songs` | Clone Hero and YARG songs the device's whitelist allows | receive-only |
| `tools` | Scripts and documents | receive-only |
| `saves` | Every `[saves.*]` folder of the profile | two-way |
| `frontend` | ES-DE media, gamelists and collections (or the scraped artwork, for a profile with `[artwork]`) | two-way (receive-only artwork) |
| `bios` | Keys and firmware delivered into each emulator's own folder (the profile's `[bios.*]`) | receive-only |
| `settings` | The emulator settings layers | two-way |
| `play-state` | Favourites and play statistics shared with the other frontends; needs `frontend` as well | two-way |

A device that lists `play-state` without `frontend` is refused, and so is one that lists a group its profile has no path for (the Anbernic profile has no `tools`, `songs` or `settings`).

### `[inbox]`

Optional. Only `warpzone inbox` and `warpzone doctor` read it. `inbox` works with just this table, with no `[hub]` or `[devices]`, on a desktop that mounts the hub's games share. Every key is a non-empty string, and `~` is expanded. A tool path that is set (`chdman`, `bsdtar`, `rahasher`, `dolphin_tool`, `nsz`, `orbis_pkg_util`) must be an executable file, `vita3k` must be an app bundle with its binary or an executable file, and `vita_zrif` must be a file, or `inbox` stops.

| Key | Default | Meaning |
|---|---|---|
| `root` | none (use `--root`) | The hub root as mounted on this machine: the folder holding `inbox/` and `library/`. This is not `[hub].root`, which is the path on the hub. |
| `dats_dir` | none (use `--dats`) | Folder with the Redump and No-Intro DATs in clrmamepro format, such as libretro-database's `metadat/` folder, or one flat folder of `.dat` files. warpzone never downloads them. |
| `work_dir` | the system temp folder | Local folder for unpacked archives and conversions (several GiB). |
| `chdman` | `chdman` on `PATH` | Converts discs to CHD. |
| `bsdtar` | `bsdtar` on `PATH` | Unpacks `.7z` and `.rar`. |
| `rahasher` | none (used only when set) | RAHasher from RALibretro, built with CHD support: checks that a conversion keeps the RetroAchievements hash. |
| `dolphin_tool` | `dolphin-tool` on `PATH` | Reads compressed GameCube `.gcz` images. `.rvz` and `.wia` need no tool. |
| `nsz` | `nsz` on `PATH` | Reads the title ID and version of Switch `.nsp` and `.nsz` files, and decompresses `.nsz`. |
| `orbis_pkg_util` | `orbis-pkg-util` on `PATH` | Unpacks PS4 `.pkg` games. Without it the package is filed as it is. |
| `vita3k` | `/Applications/Vita3K.app` on macOS, else `vita3k` on `PATH` | Vita3K, which installs PS Vita `.pkg` games: on macOS the app bundle (or a link to its binary), on Linux a plain binary (an AppImage is refused). It runs only in a work folder of its own, never in its own data. Without it such a package goes to review. |
| `vita_zrif` | none | A file you keep with the zRIF of each PS Vita `.pkg`: `CONTENT_ID<TAB>zRIF` lines, or a TSV with a header row such as NoPayStation's `PSV_GAMES.tsv`, of which only the `Content ID` and `zRIF` columns are read. warpzone never prints a zRIF and never downloads keys. |
| `titledb_dir` | none | A local clone of `blawar/titledb`, which names Switch games. |
| `switch_keys` | `~/.switch/prod.keys` | Your console's keys file. warpzone only passes the path to `nsz`; it never reads or prints the file. |
| `mame_dat` | none | MAME's `listxml`, or an official `mameXXXXlx.zip` as it is. |
| `fbneo_dat` | none | FinalBurn Neo "ClrMame Pro XML, Arcade only". |
| `mame2003plus_dat` | none | `metadata/mame2003-plus.xml` of MAME 2003-Plus. |
| `gamelist_mame` | `<root>/frontend/es-de/gamelists/mame/gamelist.xml` | The ES-DE gamelist that holds per-game emulator overrides for arcade sets. |
| `gamelist_ps3` | `<root>/frontend/es-de/gamelists/ps3/gamelist.xml` | The ES-DE gamelist that records the alternative emulator for PS3 games. |

The environment variable `RA_API_KEY`, when set, is used for RetroAchievements lookups; it is not a config key. See [Adding games](/warpzone/setup/inbox/) for how each tool is used.

### `[serve]`

Optional. Only `warpzone serve` reads it (together with `[hub]`, `[devices.*]` and `[inbox] rahasher`).

| Key | Default | Meaning |
|---|---|---|
| `listen` | `"0.0.0.0:8780"` | `HOST:PORT` to listen on (`[::1]:8780` for IPv6). `--listen` overrides it. |
| `token_file` | none | A file whose first line is the UI token, at least 16 characters (`openssl rand -hex 32` makes one). The environment variable `WARPZONE_UI_TOKEN` takes precedence. |

`serve` refuses to start (exit `2`) without a token. The token is never printed or logged. See [`warpzone serve`](/warpzone/reference/commands/#warpzone-serve).

### `[ra]`

Optional. Read by `warpzone serve` and `warpzone catalog --ra-achievements`.

| Key | Default | Meaning |
|---|---|---|
| `user` | none | Your RetroAchievements user name, 1 to 40 letters, digits, `_`, `-` or `.`. With it (and `RA_API_KEY`) the hub downloads your unlocked achievements and awards for the games of the library, and the library UI shows your progress. Without it, achievements are listed with no progress. It is your public RA name, not a secret; the API key is, and stays in the environment. |

### A complete example

Every ID, address, path and key below is fake.

```toml
[hub]
name = "hub"
device_id = "AAAAAAA-AAAAAAA-AAAAAAA-AAAAAAA-AAAAAAA-AAAAAAA-AAAAAAA-AAAAAAA"  # gitleaks:allow
root = "/srv/games"
api_url = "http://hub.example.lan:8384"
api_key_file = "~/.config/warpzone/hub.key"
# api_key = "paste-the-key-here"          # use this or api_key_file, never both
addresses = ["tcp://hub.example.lan:22000"]

[devices.deck]
device_id = "BBBBBBB-BBBBBBB-BBBBBBB-BBBBBBB-BBBBBBB-BBBBBBB-BBBBBBB-BBBBBBB"  # gitleaks:allow
profile = "steamos-emudeck"
folders = ["library", "tools", "saves", "frontend", "bios", "settings", "play-state"]
addresses = ["tcp://deck.example.lan:22000"]
rescan_seconds = 300
self_update = true
auto_hub = false
auto_prune = true
channel = "stable"
# pin = "v0.3.0"
# exclude_systems = ["ps4", "switch"]

[devices.handheld]
device_id = "CCCCCCC-CCCCCCC-CCCCCCC-CCCCCCC-CCCCCCC-CCCCCCC-CCCCCCC-CCCCCCC"  # gitleaks:allow
profile = "anbernic-rg351mp"
folders = ["library", "saves", "frontend"]
channel = "rc"

[inbox]
root = "/mnt/hub"
dats_dir = "~/dats/libretro-database/metadat"
work_dir = "~/tmp/warpzone"
chdman = "/usr/local/bin/chdman"
rahasher = "~/tools/RAHasher"
nsz = "~/.local/bin/nsz"
titledb_dir = "~/dats/titledb"
switch_keys = "~/.switch/prod.keys"
mame_dat = "~/dats/mame0282lx.zip"

[ra]
user = "ExampleUser"
```

## Device profiles

A profile describes where things live on one kind of device: its folders, saves, BIOS locations, emulator settings and frontend. Profiles are data files shipped with warpzone, in `warpzone/data/profiles/`, and there is no per-user profile directory: `profile` in a device table names one of them, and a new one is added to the repository. An unknown name is an error that lists the available ones.

| Profile | For |
|---|---|
| `steamos-emudeck` | SteamOS with EmuDeck (Steam Deck, Steam Machine). Uses `paths`, `library`, `saves`, `bios`, `settings`, `play_state`, `shadps4`, `cemu` and `links`. |
| `anbernic-rg351mp` | An Anbernic RG351MP running dArkOS, with ROMs on an exFAT card. Uses `ignore_perms`, `paths`, `es_systems`, `retroarch`, `saves`, `artwork` and `library`. |

The file name (without `.toml`) must equal the profile's `name`. Paths use `~`, expanded on the device. A profile fails to load when any table breaks the rules below; the shipped ones are checked by the test suite.

Top-level keys:

| Key | Type | Default | Rules |
|---|---|---|---|
| `name` | string | none (required) | Matches `^[a-z0-9][a-z0-9-]*$` and the file name. |
| `description` | string | `""` | Free text. |
| `ignore_perms` | boolean | `false` | The device's folders are on a filesystem without POSIX permissions (exFAT): its Syncthing folders ignore permissions. |

### `[paths]`

Where each folder group lives on the device. Every value is a non-empty string; only these keys are allowed.

| Key | Folder group it serves |
|---|---|
| `library` | `library` (and the base of `[bios.*]`, `{library}` placeholders and `[artwork]`) |
| `songs` | `songs` |
| `tools` | `tools` |
| `media` | `frontend` (the hub's `frontend/media`) |
| `esde` | `frontend` (the hub's `frontend/es-de`) |
| `settings` | `settings` |
| `play_state` | `play-state`. Needs `paths.esde` or `[artwork]`, and must not be the library folder or inside it. |

A device that lists a group whose path the profile lacks is refused. A profile with `[artwork]` must not set `media` or `esde`: the artwork table replaces them.

### `[saves.<emulator>-<kind>]`

One table per save folder, delivered with the `saves` group (two-way). The key is two lowercase alphanumeric words joined by one hyphen (`retroarch-saves`, `dolphin-gc`). It becomes the folder `wz-save-<emulator>-<kind>` and the hub path `saves/<emulator>/<kind>`.

| Key | Type | Default | Rules |
|---|---|---|---|
| `path` | string | none (required) | The REAL directory. Syncthing never follows symlinks, and EmuDeck exposes saves through symlinks, so point at the real directory. |
| `ignore` | array of strings | `[]` | Syncthing ignore lines for this folder only, such as `["!/save", "!/saveMeta", "*"]`. |
| `create_when` | string | none | A directory. When `path` is missing and this one exists (the emulator is installed but has not written its saves yet), `path` is created. Without it a missing `path` is skipped. |

### `[library]`

| Key | Type | Default | Rules |
|---|---|---|---|
| `ignore` | array of strings | `[]` | Library paths the device never receives, whatever its whitelist says. Each starts with `/` (anchored, and not just `/`), optionally preceded by `(?d)` so Syncthing may delete the file with its parent folder. These lines go ahead of the whitelist. The folders delivered by `[bios.*]` are added automatically. |

### `[bios.<emulator>-<kind>]`

A hub folder under `library/` that lands in the emulator's real directory, delivered with the `bios` group (receive-only). Needed because the library cannot deliver these files: EmuDeck puts symlinks at the same place. The key has the same shape as a save key and becomes `wz-bios-<emulator>-<kind>`.

| Key | Type | Default | Rules |
|---|---|---|---|
| `hub` | string | none (required) | A plain relative path below the hub's `library/`, like `bios/cemu`: segments of letters, digits, `.`, `_` and `-`, no `..`. No two placements may overlap. |
| `path` | string | none (required) | The emulator's real directory on the device. |
| `ignore` | array of strings | `[]` | Ignore lines for this folder, such as `["!/keys.txt", "*"]`. |
| `create_when` | string | none | As for saves. |
| `verify` | boolean | `false` | Marks a target that is a best guess not confirmed on a device. |

### `[settings.<emulator>]`

Declares an emulator's files for the [settings layers](#settings-layers). The id matches `^[a-z0-9-]+$`.

| Key | Type | Default | Rules |
|---|---|---|---|
| `process` | array of strings | none (required) | Non-empty list of the names the running process has; nothing is written while one runs. |
| `verify` | boolean | `false` | A path or process name not yet confirmed on a device. |
| `files.<id>` | table | none | A settings file; see below. |
| `data.<id>` | table | none | A system data file synced whole; see below. |

`[settings.<emulator>.files.<id>]` (ids match `^[a-z0-9-]+$`, and `data` is reserved):

| Key | Type | Default | Rules |
|---|---|---|---|
| `path` | string | none (required) | `~` is expanded. `{name}` is the only placeholder, and only in the directory part: it makes a templated file whose instances are named `<id>:<name>` in a layer. |
| `format` | string | none (required) | One of `ini`, `qt-ini`, `retroarch-cfg`, `yaml-flat`, `json`, `toml`. |
| `create` | boolean | `false` | Apply creates the file when missing and when it has layered keys. Otherwise a missing file means the emulator never ran and is skipped. |
| `noise` | array of strings | `[]` | Key patterns `capture` never proposes (window geometry, recent files, controller sections). A noise key set in a layer by hand is still applied. |
| `secret` | array of strings | `[]` | Key patterns that are never captured, applied, snapshotted, printed or logged. A key whose last part contains `token`, `password`, `passwd`, `secret`, `apikey` or `api_key` is secret as well, always. |
| `device` | array of strings | `[]` | Keys `capture` pre-selects for the device layer (resolution, docked mode). |
| `known` | array of strings | `[]` | Keys that `capture --seed` proposes (language, region). |

Patterns match the file's canonical key form: `*` is any run of characters, `?` one character, and nothing else is special.

`[settings.<emulator>.data.<id>]`: `path` (string, required, no `{` or `}`) and `create_when` (string, optional, as for saves).

### `[es_systems]`

For a frontend that reads a system list: warpzone writes a copy of the list with every ROM folder moved into the library, only while the frontend is closed. Needs `paths.library`.

| Key | Type | Default | Rules |
|---|---|---|---|
| `source` | string | none (required) | The list that ships with the operating system. |
| `override` | string | none (required) | The copy the frontend reads first. |
| `prefix` | string | none (required) | An absolute folder starting and ending with `/`. Entries outside it are not touched. |
| `process` | array of strings | none (required) | The frontend's process names (the kernel cuts them to 15 characters). |
| `rename` | table of strings | `{}` | Folder names the system list and the hub spell differently: `list folder = hub folder`. |

### `[retroarch]`

Pins keys of `retroarch.cfg`, only while RetroArch is not running.

| Key | Type | Default | Rules |
|---|---|---|---|
| `cfg` | array of strings | none (required) | Config files of each build; non-empty. |
| `process` | array of strings | none (required) | RetroArch's process names; non-empty. |
| `set` | table of strings | none (required) | Non-empty `key = "value"` table. A value is a string without `"`; `{library}` is the only placeholder, and needs `paths.library`. |

### `[artwork]`

Scraped artwork and metadata for a frontend that is not ES-DE. The hub's media and ES-DE folders arrive receive-only, limited to `kinds` of the systems selected by `systems`, and a converter writes the frontend's gamelists. Needs `paths.library`, and `[es_systems]` when `format` is `emulationstation`.

| Key | Type | Default | Rules |
|---|---|---|---|
| `format` | string | none (required) | `emulationstation`. |
| `kinds` | array of strings | none (required) | Non-empty, no repeats, from `covers`, `screenshots`, `miximages`, `marquees`, `3dboxes`, `physicalmedia`. |
| `systems` | string | `"library"` | `library`: every system folder below the library's `roms/`. |
| `media` | string | none (required) | Folder for the media. Must not be the library folder or inside it, and must be separate from `metadata`. |
| `metadata` | string | none (required) | Folder for the ES-DE metadata, with the same restrictions. |
| `gamelists` | string | none (required) | Where the converter writes the frontend's gamelists (`{rompath}` for next to the ROMs). |
| `refresh` | string | none | An interval `<n>min` or `<n>h` (for example `15min`, `6h`; `n` at least 1). With it, `provision device` installs a user timer that runs `warpzone frontend refresh` that often. |

### `[play_state]`

| Key | Type | Default | Rules |
|---|---|---|---|
| `refresh` | string | none | An interval like `15min` or `6h`. With it, `provision device` installs a timer that publishes this device's favourites and play statistics. Needs `paths.play_state`, and is not allowed together with `[artwork]` (its own `refresh` does this). |

### `[shadps4]`

Adds install directories to shadPS4's config files, sets its DLC folder and, optionally, a launch script.

| Key | Type | Default | Rules |
|---|---|---|---|
| `config` | array of strings | none (required) | Non-empty paths ending in `.json` or `.toml`; each is edited only if it exists. |
| `process` | array of strings | none (required) | shadPS4 and its launcher; a file is edited only while none runs. |
| `install_dirs` | array of strings | none (required) | Non-empty absolute paths (or `~`); `{library}` is the only placeholder. Existing entries are kept. |
| `addon_dir` | string | none | The folder shadPS4 reads DLC from: an absolute path (or `~`); `{library}` is the only placeholder. Replaces `General.addon_install_dir` / `GUI.addonInstallDir` when it differs. Without it the setting is left alone. |
| `launch` | table | none | `qt_ini`, `versions`, `script` and `find_rules` (each an absolute path or starting with `~/`) and `emulator` (the ES-DE emulator name), all required non-empty strings without `"` or line breaks. |

### `[cemu]`

Sets Cemu's MLC path and adds a game path in its `settings.xml`.

| Key | Type | Default | Rules |
|---|---|---|---|
| `settings` | string | none (required) | Cemu's `settings.xml`, an absolute path or one starting with `~/`; edited only if it exists. |
| `process` | array of strings | none (required) | Cemu's process names; the file is edited only while none runs. |
| `mlc` | string | none (required) | The MLC folder `mlc_path` is set to (an absolute path, or starting with `~/` or `{library}/`). Its `usr/save` should be the profile's Cemu save folder. |
| `default_mlc` | string | none (required) | The folder Cemu uses while `mlc_path` is empty (absolute or `~/`). `mlc_path` is not changed while its `usr/save/00050000/` or `usr/save/00050002/` holds a file (a game's or a demo's save). |
| `game_path` | string | none (required) | Added to `GamePaths` (as `mlc`); existing entries are kept. |

`{library}` is the only placeholder, needs `paths.library`, and is allowed only in `mlc` and `game_path`. A leading `~/` is expanded, so `settings.xml` gets absolute paths.

### `[links.<key>]`

One symlink per game folder of the library, inside an emulator's own game folder, kept by `warpzone links sync`.

| Key | Type | Default | Rules |
|---|---|---|---|
| `source` | string | none (required) | Absolute, `~/` or starting with `{library}`; the folder holding one folder per title. |
| `target` | string | none (required) | Same rules; where the emulator looks for them. |
| `process` | array of strings | none (required) | The emulator's process names; nothing changes while it runs. |
| `match` | string | `".+"` | A regular expression that a title's folder name must fully match. |
| `require` | array of strings | `[]` | Relative file paths a title folder must hold (no leading `/`, no `..`). |
| `noun` | string | `"game"` | What a title is called in messages (`game`, `license`). |
| `create_target` | boolean | `false` | Create a missing `target` when its parent exists. |

## Settings layers

The hub keeps emulator settings as layers of single keys in its `settings` folder: `common.toml` for every device and `<device>.toml` for one device, whose keys win. `settings capture` writes them; you can also edit them on the hub (comments are not kept when `capture` rewrites a file). See [Emulator settings](/warpzone/setup/settings/) for how they are used.

```toml
format = 1

[eden.qt-config]
"[System]language_index" = "1"

[retroarch.cfg]
savestate_auto_save = "true"

# A templated file's instance is named <file>:<name>.
[retroarch."opt:Flycast"]
reicast_threaded_rendering = "enabled"
```

| Part | Rules |
|---|---|
| `format` | Required, the integer `1`. A missing or other value rejects the whole layer. |
| `[<emulator>.<file>]` | An emulator and a file declared by the device's profile. An unknown emulator or file is a warning (another profile may know it). A templated file (`{name}` in its path) must be named `<file>:<name>`, with a plain file name; a plain file must not have an instance. |
| Keys | The key in the file's canonical form, with a string, integer, float or boolean value. Arrays, tables and null are rejected. Line-oriented formats (`ini`, `qt-ini`, `retroarch-cfg`, `yaml-flat`) hold raw strings only; a value cannot contain a line break, and a RetroArch value cannot contain `"`. |
| Secret keys | A key matching the file's `secret` patterns, or whose last part contains a secret word (`token`, `password`, `passwd`, `secret`, `apikey`, `api_key`), is an error. |

Validation is all or nothing: if a layer is invalid, `settings apply` changes nothing. The device layer's value wins per key, and a device cannot unset a common key. System data files are not in the layers: they sit whole in `settings/files/<emulator>/<id>`.

## The updater settings file

`provision device` writes `~/.local/state/warpzone/updater/conf` (under `$XDG_STATE_HOME` when set) from the device's table and profile, and rewrites it at each run; do not edit it by hand. The updater script reads it line by line and never runs it. The format is one `key=value` per line, with `#` comment lines; a value cannot contain a line break or a NUL.

| Key | Written from | Meaning |
|---|---|---|
| `device` | the device name | Which device's `provision device` and `health` the updater runs. Empty: the tick does nothing. |
| `config` | `--config`, when given | The config file path passed on; empty when not given. |
| `clone` | the checkout warpzone runs from | The git clone the updater moves. When empty the script uses the clone around itself. |
| `python` | the interpreter that runs warpzone | The venv's Python, repaired by the updater when it breaks. |
| `channel` | `channel` | `stable` or `rc`. |
| `pin` | `pin` | A tag, or empty for none. |
| `enabled` | `self_update` | `true` or `false`. The script treats `false`, `0`, `no` and `off` as off, and anything else as on. |
| `frontend` | the profile | Space-separated process names that show the frontend's menu is open. The profile's `[es_systems] process` when it has one, otherwise ES-DE's names. When empty the script uses `es-de ES-DE.AppImage`. |

The environment variables that move or tune the updater are listed under [the updater script](/warpzone/reference/commands/#environment-variables). How a tick uses these settings is in [Updating a device](/warpzone/operation/self-update/).

## `bios.toml` (local overrides)

Optional, read by `warpzone bios`: `bios.toml` next to the config file, or the file named by `--overrides` (which must then exist). The shipped manifest, `warpzone/data/bios.toml`, holds names, sizes and hashes only. Top-level tables are system names of the manifest; an unknown system is an error.

```toml
[saturn.accept]
"sega_101.bin" = ["00000000000000000000000000000000"]

[nds]
unwanted = ["firmware"]
```

| Key | Type | Rules |
|---|---|---|
| `<system>.accept` | table | `path = [md5, ...]`. The path must be a file of that system in the manifest, checked by hash; each value is a list of 32-digit md5 hashes. A file with one of them is accepted and reported as "accepted (local)"; the size must still match. |
| `<system>.unwanted` | array of strings | Item names (the labels in the report) you do not want to be told about; each must be an item of that system. |

## Environment variables

| Variable | Used by | Meaning |
|---|---|---|
| `WARPZONE_CONFIG` | every command with `--config` | The config file when `--config` is not given. |
| `XDG_CONFIG_HOME` | config path, `bios.toml` | Base of `warpzone/config.toml`; default `~/.config`. |
| `XDG_STATE_HOME` | settings state, updater | Base of `warpzone/settings/` and `warpzone/updater/`; default `~/.local/state`. |
| `XDG_CACHE_HOME` | `inbox` | Base of the parsed-DAT cache (`warpzone/dats`); default `~/.cache`. |
| `RA_API_KEY` | `inbox` | RetroAchievements API key for hash lookups (never printed). |
