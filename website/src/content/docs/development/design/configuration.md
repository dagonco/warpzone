---
title: "Configuration and command line"
---

## Private configuration

Lives outside the repo, at `$XDG_CONFIG_HOME/warpzone/config.toml`
(default `~/.config/warpzone/config.toml`). It is the only place with personal
data. `examples/config.example.toml` documents every key.

```toml
[hub]
name = "nas"
device_id = "XXXXXXX-..."
root = "/volume1/games"                    # path on the hub's filesystem (a Synology path as an example)
api_url = "http://nas.local:8384"
api_key_file = "~/.config/warpzone/hub.key"  # or api_key = "..."
addresses = ["tcp://nas.local:22000"]        # optional, default ["dynamic"]

[devices.deck]
device_id = "YYYYYYY-..."
profile = "steamos-emudeck"
folders = ["library", "tools", "saves", "frontend"]
addresses = ["tcp://deck.local:22000"]       # optional, default ["dynamic"]
self_update = true                           # optional: install the self-update timer (default true)
channel = "stable"                           # optional: "stable" (default) or "rc"
pin = "v0.3.0"                               # optional: stay on this tag (vX.Y.Z or vX.Y.ZrcN)
auto_hub = false                             # optional: create the hub folders it lacks (default false)
auto_prune = true                            # optional: the unattended prune timer (default true with "library")
exclude_systems = ["ps4", "switch"]          # optional: systems the device does not run (default: none, it runs all)

[devices.machine]
device_id = "ZZZZZZZ-..."
profile = "steamos-emudeck"
folders = ["library", "tools", "saves", "frontend", "songs", "settings"]

[inbox]                                      # optional, read only by `warpzone inbox`
root = "/Volumes/hub"                      # the hub root as mounted on this machine
dats_dir = "~/dats/libretro-database/metadat"
work_dir = "~/tmp/warpzone"                  # optional; default: the system temp folder
chdman = "/opt/homebrew/bin/chdman"          # optional tools; PATH is searched when unset
bsdtar = "/usr/bin/bsdtar"
rahasher = "~/tools/RAHasher"                # only used when set
dolphin_tool = "/usr/local/bin/dolphin-tool"
mame_dat = "~/dats/mame0282lx.zip"            # optional arcade databases (see Inbox)
fbneo_dat = "~/dats/fbneo-arcade.dat"
mame2003plus_dat = "~/dats/mame2003-plus.xml"
gamelist_mame = "/Volumes/hub/frontend/es-de/gamelists/mame/gamelist.xml"  # optional
gamelist_ps3 = "/Volumes/hub/frontend/es-de/gamelists/ps3/gamelist.xml"  # optional

[serve]                                      # optional, read only by `warpzone serve`
listen = "0.0.0.0:8780"                      # optional, the default
token_file = "/config/ui.token"              # optional; else WARPZONE_UI_TOKEN

[ra]                                         # optional, read by `serve` and `catalog --ra-achievements`
user = "ExampleUser"                         # optional; the RA user whose progress is shown
```

`warpzone inbox` reads only the `[inbox]` table, so a computer (or the hub container) that only files games
needs no `[hub]` or `[devices]`. API keys are never printed, logged or included in
error messages; the RetroAchievements key is read from `RA_API_KEY`, never from a file.
`warpzone serve` reads `[hub]`, `[devices.*]`, `[serve]`, `[ra]` and `[inbox] rahasher`; its token
comes from `WARPZONE_UI_TOKEN` or `[serve] token_file` and is never printed either (see
[Library UI](/warpzone/development/design/library-ui/#authentication)).

## Command line

`python3 -m warpzone <command>`; also installable as a `warpzone` script.

| Command | Runs on | Does |
|---|---|---|
| `warpzone check` | anywhere | Validates config + profiles, reports which whitelists are missing on the hub (via `GET /rest/db/browse`), never writes. Exit 1 if any are missing; `--offline` skips the hub. Where it can read a device's whitelist (the hub root with `--root`, or the device's own library folder), it also reports a whitelist that names a system in the device's `exclude_systems` (exit 1), see [Supported systems](/warpzone/development/design/library-ui/#supported-systems). |
| `warpzone provision hub` | anywhere that reaches the hub API | Adds every configured device to the hub, creates/updates every `wz-*` folder on the hub and shares it with the devices that subscribe to it, sets the hub ignores. A newly created whitelisted folder first lets only the whitelists through and waits for them (`--timeout`, default 120 s) before the full scan, see "Hub bootstrap". Idempotent. `--additive` does only the creation of the `wz-*` folders the hub lacks, never a whitelisted one and never a change to a folder the hub has, see [Hub folders from a console](/warpzone/development/design/autonomous-updates/#hub-folders-from-a-console-auto_hub). |
| `warpzone provision device <name>` | on the device | Reads the local Syncthing API key from the local `config.xml` (searching the default locations, including the SyncThingy flatpak; `--syncthing-config PATH` overrides), adds the hub device, creates the subscribed folders with the bootstrap above (`--timeout SECONDS`). With `auto_hub = true` it first creates on the hub the folders of the device that the hub lacks (see [Hub folders from a console](/warpzone/development/design/autonomous-updates/#hub-folders-from-a-console-auto_hub)). `--unattended` is for a run nobody watches and changes the rules as described in [Autonomous updates](/warpzone/development/design/autonomous-updates/); it exits `3` when a write waits for the menu to close. When that Syncthing is the SyncThingy flatpak it first grants it the missing `flatpak override --user --filesystem=~/.var/app/<app>` for every folder under `~/.var/app` (flatpak hides other apps' data), and asks for a Syncthing restart. When the device subscribes to `settings`, it installs the ES-DE startup script that runs `settings apply` (see [ES-DE startup script](/warpzone/development/design/emulator-settings/#es-de-startup-script)). When the device subscribes to `library` and its profile has `[es_systems]` or `[retroarch]`, it also writes the frontend's system list override and pins RetroArch's paths (see [Frontend and RetroArch overrides](/warpzone/development/design/profiles/#frontend-and-retroarch-overrides)). When the profile has `[shadps4]` it also lists the PS4 games folder in shadPS4's install directories (see [shadPS4 install directories](/warpzone/development/design/profiles/#shadps4-install-directories)). When the profile has `[cemu]` it also sets Cemu's MLC path and adds the library's Wii U folder to its game paths (see [Cemu paths](/warpzone/development/design/profiles/#cemu-paths)). When the profile has `[links.*]` and the device lists `library`, it links the library games into the emulator's game folder and installs the ES-DE startup script that keeps them linked (see [Title links](/warpzone/development/design/title-links/)). When it lists `frontend` and the profile has `[artwork]`, it limits the artwork folders to the systems on the device, writes the gamelists and installs the refresh timer (see [Frontend artwork](/warpzone/development/design/frontend-artwork/)). When it lists `library` and `auto_prune` is not `false`, it installs the unattended prune timer (see [Unattended runs](/warpzone/development/design/prune/#unattended-runs)). Idempotent. |
| `warpzone frontend refresh [device]` | on the device | For a device whose profile has `[artwork]` and that lists `frontend`: widens the artwork folders' ignores to the systems that have games now (when the local Syncthing is found) and rewrites the frontend's gamelists from the synced ES-DE data, only while the frontend is closed. `--dry-run` prints what it would do. See [Frontend artwork](/warpzone/development/design/frontend-artwork/). |
| `warpzone play-state sync <device>` | on the device | For a device with ES-DE that lists `play-state`: publishes what its gamelists say and merges the other devices' ledgers into them, only while ES-DE is closed or starting (`--frontend-starting`, which its startup script passes). `--dry-run`, `--quiet`. Needs no network. See [Play state](/warpzone/development/design/play-state/). |
| `warpzone links sync <device>` | on the device | For a profile with `[links.*]` and a device that lists `library`: makes each rule's `target` hold a symlink per library title, never while the emulator runs (see [Title links](/warpzone/development/design/title-links/)). `--dry-run` only says what it would do, `--quiet` prints nothing. ES-DE runs it at startup. |
| `warpzone saves convert <device>` | on the device | For a profile with `[save_convert.*]` and a device that lists `saves`: reconciles the hub's raw saves with the emulator's own files (DraStic's `.dsv`), never while the emulator runs, with a backup before any overwrite (see [Save conversion](/warpzone/development/design/save-conversion/)). `--dry-run` only says what it would do, `--quiet` prints nothing. EmulationStation runs it around the emulator and `frontend refresh` runs it on the timer. |
| `warpzone prune [device]` | on the device | Reports, and with `--apply` deletes, the files of the whitelisted folders that the device no longer whitelists (see [Pruning](/warpzone/development/design/prune/)). Report only by default. `--apply --unattended` is what the prune timer runs: no size cap, the half-of-device guard instead (see [Unattended runs](/warpzone/development/design/prune/#unattended-runs)). |
| `warpzone catalog` | where the hub root is mounted (the hub's container) | Builds and updates the catalog index in `catalog/` from `library/` (see [The catalog index](/warpzone/development/design/library-ui/#the-catalog-index)). `--root PATH` (default `[hub] root`), `--dry-run`. `--ra-hash` hashes the games that lack an RA hash with RAHasher (`--rahasher PATH`, else `[inbox] rahasher`); `--ra-lists` refreshes the cached RA game lists (`RA_API_KEY`; `--force` ignores their age); `--dats [--dats-dir PATH]` fills the DAT `serials` of games that have none from the DAT folder of `inbox`. |
| `warpzone whitelist migrate [device ...]` | where the hub root is mounted | Converts hand-written `patterns-<device>.stignore` files into the library UI's format, proving on the hub's tree that the included paths stay the same, with a backup of each file it replaces (see [The migration](/warpzone/development/design/library-ui/#the-migration)). `--root PATH`, `--dry-run`. Exit 1 if any file was refused. |
| `warpzone serve` | in the hub's container | Serves the library UI and its JSON API (see [Library UI](/warpzone/development/design/library-ui/)). Writes only device whitelists, the catalog and the whitelist backups, on request. Runs until stopped; exit 2 without a token or when `[hub] root` has no `library/.stfolder`. |
| `warpzone bios` | on a device or the hub | Reports which BIOS and firmware files a library has, lacks or has wrong (see [BIOS check](/warpzone/development/design/bios/)). Read-only. Exit 1 if a required item is missing or wrong. |
| `warpzone inbox` | a computer that mounts the hub's share, or the hub's container | Identifies, converts and files new games dropped in `inbox/` (see [Inbox](/warpzone/development/design/inbox/)). Report only by default, `--apply` acts. Exit 1 if any item needs review. |
| `sh <clone>/warpzone/data/updater.sh install <device>` | on the device, from a fresh `git clone` | The one way to install a device: checks out the best release of the channel, makes the venv next to the clone, and runs `provision device` (see [Self-update](/warpzone/development/design/self-update/)). |
| `warpzone self-update` | on the device | Runs one tick of the updater script's live copy (`sh ~/.local/share/warpzone/updater/updater.sh tick`), as the timer does, and exits with its code (see [Self-update](/warpzone/development/design/self-update/)); `--now` passes `--now` (`--fetch-now` to an older live copy). Needs no config. Without a live copy it says so and exits 1. `--channel stable\|rc [--dry-run]` runs no tick: it sets the device's channel in its config and in the updater's settings. |
| `warpzone health [device]` | on the device | Checks that this version works here: loads the config and the profiles, runs `settings apply`, `play-state sync`, `links sync` and `frontend refresh` as dry runs within the 15 seconds ES-DE gives its startup scripts, and checks that the startup scripts and the units exist with an interpreter that imports warpzone (see [`warpzone health`](/warpzone/development/design/autonomous-updates/#warpzone-health)). Writes no user data, needs no DATs or tools. Exit 0 when it works, 1 when a check failed. |
| `warpzone doctor` | any machine | Checklist of this machine: tools, config, local Syncthing, DATs, hub folder (see [Doctor](/warpzone/development/design/doctor/)). Offline, never changes anything. Exit 1 if a required check fails. |
| `warpzone settings apply` | on the device | Writes the layered emulator settings into each emulator's config file: only the layered keys, with a backup, never while the emulator runs (see [Emulator settings](/warpzone/development/design/emulator-settings/)). Run by ES-DE at startup and by hand. |
| `warpzone settings capture` | on the device | Shows what changed in the emulators' settings since the last sync and writes the keys the user chooses to the common or the device layer on the hub. |
| `warpzone settings audit` | on the device | Reports drift between the layers and the emulators' files. Read-only. Exit 1 on drift. |
| `warpzone status` | anywhere | Folder state, need counts, errors, receive-only local changes, for the hub (and the local device if any). |
| `warpzone render-ignores <device>` | anywhere | Prints the ignore lines a device folder would get. For debugging. |

`--dry-run` belongs to the commands that write by default: `provision hub`, `provision
device`, `frontend refresh`, `play-state sync`, `links sync`, `saves convert`, `settings apply`,
`settings capture`, `catalog` and `whitelist migrate`. For `provision` it prints the REST calls the command would make (method,
path, JSON body with secrets redacted) and sends no write, though it still sends the GET
requests it needs to compute the diff; the others say what they would write. Commands that act
only with `--apply` (`prune`, `inbox`) have no `--dry-run`: the report is the dry run, see
[Writing commands](#writing-commands). `check`, `bios`, `doctor`, `status`, `render-ignores`,
`settings audit`, `health` and a `self-update` tick do not write user data and take none.

Every command takes `--config PATH` (default `$WARPZONE_CONFIG`, then the XDG path) except
`self-update`: it needs no config and runs the installed updater with the settings `provision
device` wrote (`--now`), or, with `--channel stable|rc`, edits the config those settings name.

Exit codes: `0` ok, `1` runtime error (for `health`, a failed check, a config error included),
`2` usage or config error, `3` only for `provision device --unattended` (deferred), `130` when
a command is interrupted with Ctrl-C and `143` for an updater tick killed by `HUP`, `INT` or
`TERM`. Every error warpzone reports derives from `WarpzoneError`, which carries its exit code
(`errors.py`); `UsageError`, the base of `ConfigError`, `ProfileError`, `ModelError`,
`BiosError` and `InboxError`, makes it `2`. So a usage error of `bios` or `inbox` (a missing
`--root`, a configured file that does not exist) exits `2`, not `1`, and `1` stays for what went
wrong while working (a missing BIOS, an item sent to review).

`prune` has its own flags: `--apply`, `--unattended` (only with `--apply`, not with
`--max-gib` or `--force`), `--max-gib N` (default 50), `--force`,
`--list`, `--notify-url URL`, `--timeout SECONDS` (wait for the rescan),
`--syncthing-config PATH`. It does not take `--dry-run`: without `--apply` it
is one. If the device name is left out, it is the configured device whose ID
matches the local Syncthing.

### Writing commands

There are two kinds of command that write, and the rule follows from what a mistake costs.

**Destructive commands report first and act only with `--apply`.** They delete or move data
the user owns and cannot get back from a backup: `prune` (files of a whitelisted folder the
device no longer whitelists) and `inbox` (converts, places and moves the originals of the games
in `inbox/`). Without `--apply` they read, plan and print, and change nothing; the report is
the dry run, so they take no `--dry-run`. `prune` re-checks each file right before deleting it
and has a size cap on top (`--max-gib`).

**One exception: the unattended prune.** A game turned off in the library UI must leave the
console by itself, so the prune timer runs `prune --apply --unattended` with nobody watching. It
keeps every safety rule of `prune` and its re-check before each delete, drops the size cap (one
PS4 game is larger than the default) and refuses to delete anything when a folder would lose
more than half of what the device holds in it (see
[Unattended runs](/warpzone/development/design/prune/#unattended-runs)). No other destructive
command runs unattended.

**Convergent commands write by default and accept `--dry-run`.** They bring the machine to
the state the config and the profile describe: running one again changes nothing, and what
they change is either warpzone's own (Syncthing folders it prefixes with `wz-`, its units,
scripts and state files, symlinks it made) or a file that an emulator or the frontend owns,
which they edit on one key or one line, with a backup first and never while that program runs.
They are `provision hub`, `provision device`, `frontend refresh`, `play-state sync`,
`links sync`, `saves convert`, `settings apply`, `settings capture` (which writes only the keys the user
chooses), `catalog` (which writes only `catalog/`) and `whitelist migrate` (which keeps a backup
of each whitelist and proves the included paths unchanged). `--dry-run` says what they would do and writes nothing. `self-update` is the
timer's own convergent step and takes no `--dry-run`: it moves the clone to a release that
passes the checks, and goes back to the last good one when it fails (see
[Self-update](/warpzone/development/design/self-update/)). `self-update --channel stable|rc` is
convergent like the others: it edits one line of the device's config (with a backup
`config.toml.bak-<date>`) and `channel=` in the updater's settings, and takes `--dry-run`, which
it accepts only with `--channel`.

`serve` is a server, not a run: it writes only on request, one device whitelist at a time,
with the convergent rule's safeguards (a backup of the file it replaces, refused when the file
changed since the client read it, a re-check right before the rename) and a `dry_run` on every
request instead of `--dry-run`.

**Read-only commands** never write user data: `check`, `bios`, `doctor`, `health`, `status`,
`render-ignores` and `settings audit`.

A new command that deletes or moves user data is destructive and follows the first rule;
anything else follows the second. Either way, never overwrite or delete user data without a
re-check right before acting.

### Idempotency rules for `provision`

- Existing `wz-*` folders are updated in place with `PATCH`, only for the keys
  warpzone owns (`label`, `path`, `type`, `devices`, `versioning`,
  `fsWatcherEnabled`, and `rescanIntervalS` on whitelisted device folders).
  Unknown keys are left alone. `devices` is merged: devices
  already on the folder are kept, missing ones are added.
- Folders not prefixed `wz-` are never touched.
- Changing a folder's `path` on an existing folder is refused (it would make
  Syncthing see a mass delete); the user is told to remove the folder first.
- Devices are only ever added, never removed, by warpzone.
