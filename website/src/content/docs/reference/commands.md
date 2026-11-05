---
title: "Commands"
description: "Every warpzone command, its options and its exit codes."
---

Every command of the `warpzone` command line and of the device-side updater script, with its options, what it changes and what it exits with. The examples use fake device names; replace them with the names in your [config file](/warpzone/reference/configuration/).

```bash
python3 -m warpzone <command>     # always works
warpzone <command>                # when the venv's bin/ is on PATH
warpzone --version                # prints "warpzone X.Y.Z" and exits 0
```

## Reading this page

**Where a command runs.** `check`, `provision hub`, `status`, `render-ignores`, `bios` and `inbox` run on any machine that can reach what they need (the hub's Syncthing API, or the library on a disk). `provision device`, `prune`, `frontend refresh`, `play-state sync`, `links sync`, `settings`, `self-update` and `health` run on a console, because they read its local Syncthing and write its files. `doctor` runs anywhere and only queries the local Syncthing on `127.0.0.1`.

**What changes files.** Commands report first and act only when you ask:

| Command | Changes anything? |
|---|---|
| `check`, `status`, `render-ignores`, `bios`, `doctor`, `health`, `settings audit` | No. Read only. |
| `prune`, `inbox` | Report only. They delete or move files only with `--apply`. |
| `provision hub`, `provision device`, `frontend refresh`, `play-state sync`, `links sync`, `settings apply`, `settings capture` | Yes, and each accepts `--dry-run` to show what it would do and change nothing. |
| `self-update` | Yes: it runs one tick of the updater, which can switch the checkout to a newer release. It has no `--dry-run`. |

**`--config PATH`.** Almost every leaf command takes `--config PATH`: the private config file. Without it warpzone uses `$WARPZONE_CONFIG`, then `$XDG_CONFIG_HOME/warpzone/config.toml`, then `~/.config/warpzone/config.toml`. The option goes after the full command (`warpzone provision hub --config FILE`, not before `provision`). `self-update` is the exception: it needs no config and takes only `--now`.

**`--dry-run`.** For commands that talk to Syncthing (`provision`, `frontend refresh`) it prints the REST calls it would make (method, path and JSON body, with API keys redacted) and sends no write; it still sends the `GET` requests it needs to work out the difference. For `play-state sync`, `links sync` and `settings`, it says what it would do and writes nothing.

**Safety rule.** Never delete or overwrite user data without checking again right before acting. See [Safety](/warpzone/operation/safety/) and [Pruning what a device no longer whitelists](/warpzone/setup/whitelists/#pruning-what-a-device-no-longer-whitelists).

### Exit codes

| Code | Meaning |
|---|---|
| `0` | Success. For a report command, nothing needs attention. |
| `1` | A runtime error, or a report found something to fix (see each command). `health` uses it for any failed check, a config error included. |
| `2` | Usage or configuration error: a bad option (argparse), an unreadable or invalid config, an unknown device name, or, for `bios` and `inbox`, a library, hub root or DAT folder that cannot be used. Printed as `warpzone: config error: ...` or `warpzone: error: ...`. |
| `3` | Deferred. Only `provision device --unattended`: a write waits for the menu or an emulator to close; run it again later. |
| `130` | Interrupted with Ctrl-C. |

Every error warpzone raises on purpose derives from one base class and carries its own exit code, which defaults to `1`; the usage and config errors carry `2`. Errors are printed to standard error as `warpzone: error: ...` or `warpzone: config error: ...`. API keys never appear in any output.

## Commands at a glance

| Command | One line |
|---|---|
| [`check`](#warpzone-check) | Validate the config and find missing whitelists on the hub. |
| [`provision hub`](#warpzone-provision-hub) | Configure the hub's Syncthing. |
| [`provision device`](#warpzone-provision-device) | Configure this console. |
| [`status`](#warpzone-status) | Folder state on the hub and on this machine. |
| [`render-ignores`](#warpzone-render-ignores) | Print the ignore lines a device would get. |
| [`prune`](#warpzone-prune) | Report or delete files a device no longer whitelists. |
| [`bios`](#warpzone-bios) | Which BIOS and firmware files a library has, lacks or has wrong. |
| [`inbox`](#warpzone-inbox) | Identify, convert and file new games. |
| [`frontend refresh`](#warpzone-frontend-refresh) | Update artwork scope and rewrite a frontend's gamelists. |
| [`play-state sync`](#warpzone-play-state-sync) | Share favourites and play statistics. |
| [`links sync`](#warpzone-links-sync) | Link library games into an emulator's own folder. |
| [`settings apply`](#warpzone-settings-apply) | Write the layered emulator settings into the emulators' files. |
| [`settings capture`](#warpzone-settings-capture) | Take changed emulator settings to the layers on the hub. |
| [`settings audit`](#warpzone-settings-audit) | Report drift between the layers and the files. |
| [`self-update`](#warpzone-self-update) | Run one tick of the updater now. |
| [`health`](#warpzone-health) | Check that this version works on this device. |
| [`doctor`](#warpzone-doctor) | Check this machine and say how to fix it. |
| [`updater.sh`](#the-updater-script-updatersh) | The device-side script: `tick`, `install`, `menu`, `selftest`, `order`. |

## `warpzone check`

```
warpzone check [--config PATH] [--offline]
```

Validates the config and the shipped profiles it names, builds every folder each device should get (so a device that subscribes to a folder its profile does not define fails here), and then asks the hub which whitelist files are missing. It writes nothing.

| Option | Default | Meaning |
|---|---|---|
| `--config PATH` | see above | The private config file. |
| `--offline` | off | Validate the files only; do not contact the hub. |

Prints `config ok: N device(s), profiles: ...` first. For each whitelisted hub folder it prints `whitelists present` or `MISSING <file>`, or says the folder is not configured on the hub yet.

Exit `0` when the config is valid and nothing is missing (always with `--offline` once the config is valid); `1` when a whitelist is missing or a folder is not on the hub; `2` for an invalid config.

```bash
warpzone check
warpzone check --offline
```

## `warpzone provision hub`

```
warpzone provision hub [--config PATH] [--dry-run] [--timeout SECONDS] [--additive]
```

Configures the hub's Syncthing through its REST API: checks that the API belongs to the device named in `[hub]`, adds every configured device, creates or updates every `wz-*` folder and shares it with the devices that subscribe to it, and sets the hub's ignores. It is idempotent: run it once, then again whenever you add a device or a folder group.

With `--additive` it does much less: it only creates the `wz-*` folders the hub lacks, and only those that are not whitelisted (`wz-library` and `wz-songs` are left to a plain `provision hub`, with a warning). A folder the hub has is never changed, and where it differs from the config (path, type, a device it is not shared with) the run only warns, and so it does for a `wz-*` folder the hub has paused (it stays paused; `provision hub` finishes it). A missing folder is not created at a path another hub folder already uses. It is what a device with `auto_hub = true` does for itself, so it is safe to run from anywhere.

| Option | Default | Meaning |
|---|---|---|
| `--config PATH` | see above | The private config file. |
| `--dry-run` | off | Print the REST calls it would make and make none. |
| `--timeout SECONDS` | `120` | How long a newly created whitelisted folder waits for its whitelist files to be indexed before the full scan starts. Not used with `--additive`. |
| `--additive` | off | Only create the missing, non-whitelisted `wz-*` folders; change nothing that exists. |

Exit `0` when it finished; `1` for a runtime error (for example the API answers as another device than `hub.device_id`); `2` for a config error.

```bash
warpzone provision hub --dry-run
warpzone provision hub
warpzone provision hub --additive --dry-run
```

See [Whitelists](/warpzone/setup/whitelists/) for the bootstrap it performs.

## `warpzone provision device`

```
warpzone provision device NAME [--config PATH] [--dry-run] [--syncthing-config PATH]
                               [--timeout SECONDS] [--unattended]
```

Run on the console named `NAME` in the config. It reads the local Syncthing's API key from its `config.xml`, adds the hub, and creates the folders the device subscribes to, starting each whitelisted folder on bootstrap ignores until its whitelist has arrived. When the device has `auto_hub = true` it first creates on the hub the folders of this device that the hub lacks (the same as `provision hub --additive`, limited to this device's folders); a failure there is a warning, not an error. Depending on the device's folders and profile it also installs the ES-DE startup scripts, writes the frontend's system list and pins RetroArch's paths, adds shadPS4 install directories, links title folders, writes artwork gamelists, installs the user timers, and installs the live copy of the updater with its settings. It refuses to run when the local Syncthing is a different device than `devices.NAME.device_id`. Idempotent.

| Option | Default | Meaning |
|---|---|---|
| `NAME` | required | Device name from the config (`[devices.NAME]`). |
| `--config PATH` | see above | The private config file. |
| `--dry-run` | off | Print the REST calls and the file changes it would make; make none. |
| `--syncthing-config PATH` | search the usual places | The local Syncthing `config.xml`, or its folder. |
| `--timeout SECONDS` | `600`; `30` with `--unattended` | How long to wait for the whitelist files to arrive from the hub. |
| `--unattended` | off | For a run nobody watches (the updater uses it). Never changes the type or versioning of a folder, never puts the bootstrap ignores on an existing folder, skips new folders the hub lacks, aborts when a folder's marker is missing, and only warns about the flatpak grant. |

Exit codes: `0` finished (warnings included); `1` runtime error; `2` config error or unknown device; `3` only with `--unattended`, when a write waits for the menu or an emulator to close (run it again once it is closed).

```bash
warpzone provision device deck --dry-run
warpzone provision device deck
```

The unattended rules are in [Unattended provisioning](/warpzone/operation/self-update/#unattended-provisioning).

## `warpzone status`

```
warpzone status [--config PATH]
```

Prints one line per `wz-*` folder on the hub (state, files, outstanding items, errors, receive-only local changes) and then the same for the local Syncthing when this machine has one. Read only. Exit `0`; `1` when the hub cannot be reached; `2` for an invalid config.

```bash
warpzone status
```

## `warpzone render-ignores`

```
warpzone render-ignores NAME [--config PATH]
```

Prints the ignore lines each folder of device `NAME` would get (the bootstrap lines in comments, then the final ones), then the hub library's own `.stignore`. Useful for debugging a whitelist. Read only; needs no Syncthing. Exit `0`, or `2` for an unknown device.

| Option | Default | Meaning |
|---|---|---|
| `NAME` | required | Device name from the config. |
| `--config PATH` | see above | The private config file. |

```bash
warpzone render-ignores deck
```

## `warpzone prune`

```
warpzone prune [NAME] [--config PATH] [--syncthing-config PATH] [--apply]
               [--max-gib GIB] [--force] [--list] [--notify-url URL] [--timeout SECONDS]
```

On a console, lists the files in its whitelisted folders (library and songs) that the device's whitelist no longer lets through, and with `--apply` deletes them and the folders that end up empty. Without `--apply` it is a report and deletes nothing. It rescans first, reads Syncthing's own record of each file, and checks every file again right before deleting it; a file that changed meanwhile is left alone.

| Option | Default | Meaning |
|---|---|---|
| `NAME` | the device matching the local Syncthing | Device name from the config. |
| `--config PATH` | see above | The private config file. |
| `--syncthing-config PATH` | search the usual places | The local Syncthing `config.xml`, or its folder. |
| `--apply` | off | Delete the leftovers. |
| `--max-gib GIB` | `50` | Refuse to delete more than this in one run. |
| `--force` | off | Ignore `--max-gib`. |
| `--list` | off | List every file (otherwise the report shows a sample). |
| `--notify-url URL` | none | `POST` a JSON summary (`device`, `folder`, `files`, `bytes`, `applied`) to this `http://` or `https://` URL. The URL is never printed, because it can hold a secret. |
| `--timeout SECONDS` | `600` | How long to wait for the rescan. |

There is no `--dry-run`: the default already is one. Exit `0` when it finished cleanly (a report that lists leftovers is still `0`); `1` when a folder was skipped, the `--max-gib` limit refused the deletion, or some files could not be deleted; `2` for a config error.

```bash
warpzone prune deck --list
warpzone prune deck --apply
```

## `warpzone bios`

```
warpzone bios [--config PATH] [--root PATH] [--system NAME ...] [--all] [--overrides PATH]
```

Reports which BIOS and firmware files a library has, lacks or has wrong, judged against the manifest shipped in `warpzone/data/bios.toml` (names, sizes and hashes only; warpzone never downloads BIOS files). Read only. See [BIOS](/warpzone/setup/bios/).

| Option | Default | Meaning |
|---|---|---|
| `--config PATH` | see above | The private config file. Only needed to find the library when `--root` is not given. |
| `--root PATH` | this device's library, else the hub's | Library folder holding `bios/` and `roms/`. |
| `--system NAME ...` | all systems | Check only these systems (repeatable). |
| `--all` | off | List unknown files too. |
| `--overrides PATH` | `bios.toml` next to the config file | Local file with accepted hashes and unwanted items; see [`bios.toml`](/warpzone/reference/configuration/#biostoml-local-overrides). A path you pass must exist. |

It works without a config when `--root` is given. Exit `0` when nothing is missing or wrong; `1` when the report has problems; `2` when the library cannot be found or has no `bios/` or `roms/` folder, or the overrides file is invalid.

```bash
warpzone bios --root /path/to/library --system saturn nds
```

## `warpzone inbox`

```
warpzone inbox [--config PATH] [--root PATH] [--dats PATH] [--apply] [--force-lock] [--work-dir PATH]
```

Identifies the new games dropped in `inbox/` against the Redump and No-Intro DATs, converts them where needed, and files them in `library/roms/`. Without `--apply` it only reports what it would do. With `--apply` it converts, places the files and moves the originals out of the inbox. See [Adding games](/warpzone/setup/inbox/).

| Option | Default | Meaning |
|---|---|---|
| `--config PATH` | see above | The private config file; only its `[inbox]` table is read. |
| `--root PATH` | `[inbox] root` | The hub root as mounted on this machine, holding `inbox/` and `library/`. |
| `--dats PATH` | `[inbox] dats_dir` | Folder with the Redump and No-Intro DATs. |
| `--apply` | off | Convert, place and move the originals. |
| `--force-lock` | off | With `--apply`, take over a stale `inbox/.warpzone.lock` (older than 12 hours, or held by a process that is gone from this machine). A live lock is never taken. |
| `--work-dir PATH` | `[inbox] work_dir`, else the system temp folder | Local folder for unpacked archives and conversions. |

The external tools (`chdman`, `bsdtar`, `rahasher`, ...) come from the `[inbox]` table or `PATH`. The environment variable `RA_API_KEY`, when set, is used for RetroAchievements lookups. Without a `--root` or `--dats` (and no value in the config) it stops with exit `2`, and so does a hub root without `inbox/` and `library/roms/`. With `--apply` only one run works on a hub's inbox at a time, through the lock file `inbox/.warpzone.lock`; a second run exits `2` naming the host, process and start time that hold it. Exit `0` when every item was placed (or would be); `1` when some items need attention.

```bash
warpzone inbox
warpzone inbox --apply
```

## `warpzone frontend refresh`

```
warpzone frontend refresh [NAME] [--config PATH] [--dry-run] [--syncthing-config PATH]
```

On a console whose profile has an `[artwork]` table and whose device lists the `frontend` folder group: widens the artwork folders' ignores to the systems that have games on the device (when the local Syncthing is found) and rewrites the frontend's own gamelists from the synced ES-DE data. It writes the gamelists only while the frontend is closed; never run it while the frontend is open.

| Option | Default | Meaning |
|---|---|---|
| `NAME` | the device matching the local Syncthing | Device name from the config. |
| `--config PATH` | see above | The private config file. |
| `--dry-run` | off | Print what it would do; change nothing. |
| `--syncthing-config PATH` | search the usual places | The local Syncthing `config.xml`, or its folder. |

Exit `0` when it finished; `2` when the profile has no `[artwork]` table or the device does not list `frontend`.

```bash
warpzone frontend refresh rg351mp --dry-run
```

## `warpzone play-state sync`

```
warpzone play-state sync NAME [--config PATH] [--dry-run] [--quiet] [--frontend-starting]
```

On a console that lists the `play-state` folder group: publishes this device's favourites and play statistics and merges the other devices' into ES-DE's gamelists. The gamelists wait until ES-DE is closed. ES-DE runs it at startup, and a timer (the profile's `[play_state] refresh`) runs it between starts. A device whose profile has `[artwork]` is refused: its `frontend refresh` does this work.

| Option | Default | Meaning |
|---|---|---|
| `NAME` | required | Device name from the config. |
| `--config PATH` | see above | The private config file. |
| `--dry-run` | off | Say what it would do; write nothing. |
| `--quiet` | off | Print nothing. |
| `--frontend-starting` | off | Set by ES-DE's startup script: ES-DE waits for the command and has read no gamelist yet. |

Exit `0` when it finished; `2` for a config error (the device does not list `play-state`, or the profile has `[artwork]`).

```bash
warpzone play-state sync deck --dry-run
```

## `warpzone links sync`

```
warpzone links sync NAME [--config PATH] [--dry-run] [--quiet]
```

On a console that lists the `library` folder group and whose profile has `[links.*]` rules: makes each rule's target folder hold one symlink per library game (for example Vita3K's own game and licence folders). Nothing changes while the emulator runs. `provision device` runs it, and ES-DE runs it at startup.

| Option | Default | Meaning |
|---|---|---|
| `NAME` | required | Device name from the config. |
| `--config PATH` | see above | The private config file. |
| `--dry-run` | off | Say what it would do; change nothing. |
| `--quiet` | off | Print nothing. |

Exit `0` when it finished; `2` when the device does not list `library` or the profile has no `[links.*]` table.

```bash
warpzone links sync deck --dry-run
```

## `warpzone settings apply`

```
warpzone settings apply [--config PATH] [--device NAME] [--emulator ID ...] [--dry-run]
                        [--quiet] [--overwrite-local]
```

Writes the keys of the hub's settings layers (`common.toml`, then the device's own layer) into the emulators' files: only the layered keys, with a backup, and never while the emulator runs. ES-DE runs it at every start. See [Emulator settings](/warpzone/setup/settings/).

| Option | Default | Meaning |
|---|---|---|
| `--config PATH` | see above | The private config file. |
| `--device NAME` | the device matching the local Syncthing | Device name from the config. With it, no local Syncthing is needed. |
| `--emulator ID ...` | all | Only these emulators (repeatable). |
| `--dry-run` | off | Print the plan; write nothing. |
| `--quiet` | off | Print nothing. |
| `--overwrite-local` | off | Also write keys changed in an emulator and not captured yet (normally held back). |

Exit `0` when it finished (also when another run holds the lock, or nothing was layered); `1` for an invalid layer or a file that could not be read, parsed or written; `2` for a config error. A refused real run (exit `2`) is recorded in the device's settings log.

```bash
warpzone settings apply --dry-run
```

## `warpzone settings capture`

```
warpzone settings capture [--config PATH] [--device NAME] [--emulator ID ...] [--dry-run]
                          [--syncthing-config PATH] [--seed] [--key PATTERN ...]
                          [--include-new] [--yes] [--to-common PATTERN ...]
                          [--to-device PATTERN ...] [--discard PATTERN ...] [--offline]
```

Shows what changed in the emulators' own settings since the last sync and writes the keys you choose to the common layer or this device's layer on the hub. It needs `/proc` (Linux), skips an emulator that is running, and refuses while a layer has a conflict copy or while the local Syncthing does not report `wz-settings` idle and in sync.

| Option | Default | Meaning |
|---|---|---|
| `--config PATH` | see above | The private config file. |
| `--device NAME` | the device matching the local Syncthing | Device name from the config. |
| `--emulator ID ...` | all | Only these emulators (repeatable). |
| `--dry-run` | off | Ask or decide, print, write nothing. |
| `--syncthing-config PATH` | search the usual places | The local Syncthing `config.xml`, or its folder. |
| `--seed` | off | Also propose the `known` keys (and `--key` ones) of files with no baseline yet. |
| `--key PATTERN ...` | none | With `--seed` only: also propose keys matching `<emulator>/<file>/<key>`. |
| `--include-new` | off | Also propose keys new to a file. |
| `--yes` | off | Without a terminal, take every pre-selection. |
| `--to-common PATTERN ...` | none | Without a terminal, send matching `<emulator>/<file>/<key>` to the common layer. |
| `--to-device PATTERN ...` | none | Without a terminal, send matching keys to this device's layer. |
| `--discard PATTERN ...` | none | Without a terminal, discard matching keys (the layer value returns at the next apply). |
| `--offline` | off | Do not ask Syncthing whether `wz-settings` is up to date. |

Without a terminal and without `--yes`, `--to-common`, `--to-device` or `--discard`, it is a report and writes nothing. `--key` without `--seed` is an error. Exit `0` when it finished (also for a report); `1` for a refusal (Syncthing not in sync, a conflict copy, an invalid layer, a layer that changed meanwhile) or an error on a file; `2` for a config error.

```bash
warpzone settings capture --seed --dry-run
warpzone settings capture --to-common 'eden/qt-config/[System]*' --discard 'retroarch/*'
```

## `warpzone settings audit`

```
warpzone settings audit [--config PATH] [--device NAME] [--emulator ID ...] [--all]
```

Reports drift between the layers and the emulators' files, key by key. Read only, and it writes no state either.

| Option | Default | Meaning |
|---|---|---|
| `--config PATH` | see above | The private config file. |
| `--device NAME` | the device matching the local Syncthing | Device name from the config. |
| `--emulator ID ...` | all | Only these emulators (repeatable). |
| `--all` | off | Also list keys that are in sync. |

Exit `0` when everything is in sync; `1` when anything is pending, changed here, not writable or invalid (a running emulator and a missing file do not count).

```bash
warpzone settings audit --emulator eden retroarch
```

## `warpzone self-update`

```
warpzone self-update [--now] [--channel stable|rc]
```

On a device: runs one tick of the live copy of the updater (`sh ~/.local/share/warpzone/updater/updater.sh tick`), exactly as its timer does, and exits with the script's code. `--now` is an update asked for by hand (`updater.sh tick --now`): it fetches the remote even when the fetch interval has not passed, does not wait while the menu is open or for a back-off, and on a device whose profile installs by hand (`[updater] install = "manual"`) it is what installs a new release. With an older live copy of the script it runs `tick --fetch-now`, which still waits for the menu. `--channel stable|rc` runs no tick: it sets the channel of this device (the `device` and `config` of the updater's settings) in its config, with a backup `config.toml.bak-<date>` next to it, and in the updater's settings; it exits `1` with a message when the config cannot be edited safely (no `[devices.<name>]` table of its own, an inline table), and then the config is to be edited by hand. It needs no config. Without a live copy it says so and exits `1`. See [Updating a device](/warpzone/operation/self-update/#warpzone-self-update).

```bash
warpzone self-update --now
```

## `warpzone health`

```
warpzone health [NAME] [--config PATH] [--syncthing-config PATH]
```

On a device: checks that this version of warpzone works here. It loads the config and the profiles, runs `settings apply`, `play-state sync`, `links sync` and `frontend refresh` as dry runs (the ones that apply to the device), each within the 15 seconds ES-DE gives its startup scripts, and checks that the startup scripts and the timer units exist and run an interpreter that imports warpzone. It writes no user data and needs no DATs or tools. The updater runs it as the last step of verifying a release.

| Option | Default | Meaning |
|---|---|---|
| `NAME` | the device matching the local Syncthing | Device name from the config. |
| `--config PATH` | see above | The private config file. |
| `--syncthing-config PATH` | search the usual places | The local Syncthing `config.xml`, or its folder. |

Prints one line per check, then `health: ok` or how many failed. Exit `0` when it works; `1` when any check failed, a config error included.

```bash
warpzone health deck
```

See [Checking a version](/warpzone/operation/self-update/#checking-a-version).

## `warpzone doctor`

```
warpzone doctor [--config PATH]
```

A checklist for this machine: tools, config, local Syncthing, DATs, the hub folder, the updater and the timers, each with the exact fix for this platform. Offline except for a request to your local Syncthing on `127.0.0.1`, read only, and it works without a valid config. Exit `0` unless a required check failed, then `1`. See [Checking a machine](/warpzone/operation/doctor/).

```bash
warpzone doctor
```

## The updater script (`updater.sh`)

`warpzone/data/updater.sh` is POSIX `sh` and needs only `git` and the usual tools. It lives outside the warpzone package on purpose, so that a broken release cannot stop the tool that puts the good one back. A device runs its live copy, `~/.local/share/warpzone/updater/updater.sh`; the first install runs the copy inside the clone. The rules it follows are in [Updating a device](/warpzone/operation/self-update/#the-updater-script).

```
updater.sh tick [--fetch-now] [--now]
updater.sh install DEVICE [--config FILE] [--venv DIR] [--channel stable|rc] [-- ARGS...]
updater.sh menu
updater.sh selftest
updater.sh order
```

Any other first word prints `usage: updater.sh tick|install|menu|selftest|order` to standard error and exits `2`.

### `updater.sh tick`

One run of the timer: take the lock, defer while the frontend is open (up to 72 hours), fetch at most once an hour on `rc` and once every 12 hours on `stable` or with a pin (`WZ_FETCH_INTERVAL` overrides; `--fetch-now` fetches at once), choose the best tag of the channel (or the pin), check it out, verify it in three steps (warpzone imports and `--version` is the tag; `provision device DEVICE --unattended`; `warpzone health DEVICE`), roll back if a step fails, and reconcile. Its options are `--fetch-now` (fetch at once) and `--now` (an update asked for by hand: fetch at once, and neither the open menu nor a back-off holds it back); any other prints `usage: updater.sh tick [--fetch-now] [--now]` and exits `2`. With `install=manual` in its settings, a tick without `--now` only fetches and notes the best release in the state key `available`. Exit `0` when the tick ended without a failure (nothing to do, up to date, switched and verified, deferred, a dirty or development checkout left alone, another tick holding the lock, self-update turned off); `1` when it ended in a rollback, a missing Python, too little free space, an environment failure or an unusable checkout; `143` when stopped by `HUP`, `INT` or `TERM`.

```bash
sh ~/.local/share/warpzone/updater/updater.sh tick
```

### `updater.sh install`

The human install, run from a fresh `git clone`: checks out the best release of the channel, makes the venv next to the clone and runs `python -m warpzone provision device DEVICE`, which installs the live copy, writes the settings and starts the timer. Everything that can be refused is refused before anything changes.

| Option | Default | Meaning |
|---|---|---|
| `DEVICE` | required | Device name from the config. |
| `--config FILE` | none | Passed on to `provision device` as `--config`; without it `provision device` uses its own default. |
| `--venv DIR` | next to the clone | Where to make the venv. |
| `--channel stable\|rc` | `stable` | `stable` takes only `vX.Y.Z` tags; `rc` also takes `vX.Y.ZrcN`. |
| `-- ARGS...` | none | Everything after `--` goes to `provision device`. |

Exit `0` when it finished; `1` when it refused or failed (not a git checkout, a dirty clone, no Python 3.11 or newer, no installable release, an update running); `2` for bad arguments (usage is printed).

```bash
sh src/warpzone/data/updater.sh install deck
sh src/warpzone/data/updater.sh install deck --channel rc -- --dry-run
```

### `updater.sh menu`

Says whether the frontend (ES-DE, or the profile's own frontend) is running: prints `open` and exits `0`, prints `closed` and exits `1`, or prints `unknown` and exits `2`.

### `updater.sh selftest`

Checks that the commands the script needs exist and that its own helpers work (version comparison, tag rules, state save and load, command time limits, quoting). Prints `selftest: ok abi=N` and exits `0`, or prints each problem and exits `1`. `provision device` runs it on a new copy before it replaces the live one.

### `updater.sh order`

Reads tags on standard input and prints those that are releases or release candidates, best first. For the tests and for looking at it by hand. Always exits `0`.

```bash
git tag --list | sh warpzone/data/updater.sh order
```

### Environment variables

The script reads its settings from a `key=value` file (see [the updater settings file](/warpzone/reference/configuration/#the-updater-settings-file)). These environment variables move its files or change its limits, mainly for tests.

| Variable | Default | Meaning |
|---|---|---|
| `WZ_STATE_DIR` | `$XDG_STATE_HOME/warpzone/updater` | Folder of `state`, `conf`, `bad/` and the lock. |
| `WZ_CONF` | `$WZ_STATE_DIR/conf` | The settings file. |
| `WZ_LOG` | `self-update.log` next to `WZ_STATE_DIR` | The log. |
| `WZ_PROC` | `/proc` | Where `/proc` is. |
| `WZ_BOOT_ID_FILE` | `/proc/sys/kernel/random/boot_id` | The boot id. |
| `WZ_NOW` | the clock | The time, as seconds since the epoch. |
| `WZ_FETCH_INTERVAL` | one day | Seconds between fetches. |
| `WZ_STEP_TIMEOUT` | built in | Seconds for each step of a verification. |
| `WZ_TICK_BUDGET` | `1500` | Seconds a whole tick may take (under the service's 30 minutes). |
| `WZ_FETCH_TIMEOUT` | `120` | Seconds for the fetch. |
| `WZ_MIN_FREE_KB` | `51200` | Free space in KiB needed before switching. |
| `WZ_PYTHON_FALLBACKS` | `/usr/bin/python3 python3` | Interpreters to try after the one in the settings. |
| `WZ_ALLOWED_SIGNERS` | the file in the version known to work | The allowed-signers file (`ssh-keygen -Y` format) that tags must be signed against; empty or without keys, no signature is required. |
