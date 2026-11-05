---
title: "Updating a device"
---

A device runs warpzone from a git clone, with a venv next to it, and keeps itself on a release that works. An updater script (`warpzone/data/updater.sh`, POSIX `sh`) does the work, started by a systemd user timer. This is how it is laid out:

```
~/.local/share/warpzone/src                       the clone
~/.local/share/warpzone/venv                      the venv: no pip, a .pth to the clone, a bin/warpzone shim
~/.local/share/warpzone/updater/updater.sh        the live copy of the updater, outside the clone
~/.local/state/warpzone/updater/conf              its settings, written by provision device
~/.local/state/warpzone/updater/state, bad/       what it did and the releases it found bad
~/.local/state/warpzone/self-update.log           its log
```

There is one way to install a device. A `pipx` or wheel install is not a supported device install.

## Installing a device

On the device, from a clone, with Python 3.11 or newer, git, Syncthing running, and the config file in `~/.config/warpzone/config.toml` (see [Configuration](/warpzone/reference/configuration/)):

```bash
mkdir -p ~/.local/share/warpzone && cd ~/.local/share/warpzone
git clone https://github.com/dagonco/warpzone.git src
sh src/warpzone/data/updater.sh install deck
```

`deck` is the device's name in the config. `install` is `updater.sh install DEVICE [--config FILE] [--venv DIR] [--channel stable|rc] [-- ARGS...]`:

1. It refuses, before changing anything, a clone that has changes to tracked files or commits of its own, a device with no Python 3.11 or newer, and the case where no release can be installed. The clone must be on a release tag or on a clean branch inside `origin/main`; a fresh `git clone` is.
2. It fetches the tags (when the remote answers) and picks the best release of the channel with the same rules as a tick (see [The updater script](#the-updater-script)): the highest `vX.Y.Z` tag, or with `--channel rc` also `vX.Y.ZrcN`, that is an ancestor of `origin/main`, carries its own version, is not retired, is signed when signatures are required, and has `warpzone/data/updater.sh` in it. It checks that tag out (detached).
3. It makes the venv next to the clone (`--venv DIR` for another place) with `python3 -m venv --without-pip`, puts a `.pth` to the clone in it and a `bin/warpzone` shim that runs `python -m warpzone`. A venv that already works is kept. It checks that this release imports with it.
4. It runs `python -m warpzone provision device deck`, adding `--config FILE` when you gave one and whatever follows `--`. That is the step that installs the live copy of the updater, writes its settings and starts the timer (see below).

Run `~/.local/share/warpzone/venv/bin/warpzone doctor` afterwards; put `~/.local/share/warpzone/venv/bin` on your `PATH` to type `warpzone`.

To change `channel`, `pin` or anything else in the device's table, edit the config and run `warpzone provision device <name>` again: it rewrites the settings file. For the channel alone, `warpzone self-update --channel stable` (or `rc`) on the device does both: it changes that one line of its config, keeping a copy as `config.toml.bak-<date>`, and the updater's settings, so it holds from the next tick. An update never goes down, so a device on `v0.3.0rc4` that switches to `stable` stays there until a stable release newer than it comes out.

## Python repair

The settings name the venv's interpreter (`python`). At each tick, if it is `<folder>/venv/bin/python` and it no longer runs (an operating system update replaced the Python it was made with), or the venv no longer finds the clone, and another Python 3.11 or newer is installed, the script makes the venv again:

```
python3 -m venv --without-pip --clear <folder>
```

then writes `warpzone-clone.pth` (the clone's path) in its `site-packages` and `bin/warpzone`. It needs no network and no setuptools, and the path of the venv's Python, which the units and ES-DE's scripts hold, does not change. warpzone is always started as `python -m warpzone`. `--clear` is only run on a folder that is called `venv`, does not exist or holds the mark `warpzone-venv` that the script writes when it makes a venv (it also notes the folder in `~/.local/state/warpzone/updater/venv-owned` before clearing it, in case a power cut comes between the clear and the mark), is not a symlink and does not hold the clone: never on the folder around it, where `play-state` lives, and never on a venv of yours or the one of an older install (`install` refuses it and says to move it away or use `--venv`; a tick leaves it and logs it). A tick only repairs a checkout on a release, not one with commits of its own or changes. If no other Python exists the script changes nothing and says so in the log.

## `warpzone self-update`

```bash
warpzone self-update            # on the device
warpzone self-update --now      # also look for a new release right away
```

It runs one tick of the live copy (`sh ~/.local/share/warpzone/updater/updater.sh tick`), exactly as the timer does, prints what the script prints and exits with its code. `--now` is an update asked for by hand (`updater.sh tick --now`): it fetches the remote even when the interval below has not passed, for example to take a release candidate as soon as it is published, and neither the open menu nor a back-off holds it back. On a device that installs by hand (`[updater] install = "manual"`, the Anbernic) it is what installs a new release. With a live copy of the script older than `--now`, it runs `tick --fetch-now`, which still waits for the menu. It needs no config and has no `--dry-run`. Without a live copy it says so and exits `1`. What a tick does is in [The updater script](#the-updater-script).

### Log

The script appends its messages, with a UTC timestamp, to `~/.local/state/warpzone/self-update.log` (under `$XDG_STATE_HOME` when that is set), cut to its last 200 lines past 64 KiB. The output of its last check is in `~/.local/state/warpzone/updater/last-verify.log`. The timer's own output is in the journal: `journalctl --user -u warpzone-self-update`.

## Unattended provisioning

`warpzone provision device <name> --unattended` is the form of `provision device` for a run nobody watches, such as a device that has just moved to a new release. It does what `provision device` does and nothing that could lose data or fail for a reason that a person must look at:

- A folder that Syncthing already has never gets the bootstrap ignores (`*`). If its ignores are not the final ones and the whitelist files are on disk, it gets the final ones; if they are not, the folder is left as it is, with a warning.
- It refuses to change the `type` (for example `receiveonly` to `sendreceive`, which would send the console's deletions to the hub) or the `versioning` of a folder that exists, and a changed path. It lists them and changes nothing.
- A folder whose marker (`.stfolder`) is missing, such as one on a card that is not mounted, stops the run before any change.
- A folder that is new for the device and that the hub does not have yet (or lacks its whitelists) is skipped with a warning. A new folder whose whitelists do not arrive in 30 seconds (`--timeout` changes it) stays on the bootstrap ignores, with a warning, and the next run finishes it.
- On a device with `auto_hub = true` it creates on the hub the folders of the device that the hub lacks, apart from whitelisted ones (see [Hub folders from a console](/warpzone/development/design/autonomous-updates/#hub-folders-from-a-console-auto_hub)). A failure there is a warning, and an existing hub folder is never changed.
- The SyncThingy flatpak grant is not made, because it needs a Syncthing restart: the command to run is a warning.
- It never runs `prune --apply`, `inbox --apply` or `settings capture`.

Exit codes: `0` when it finished (warnings included), `3` when something was left for later because ES-DE, an emulator or a file ES-DE rewrites was open (run it again once it is closed), and `1` or `2` for a failure.

## Checking a version

`warpzone health [device]` says whether the installed version works on this device. It is the check the updater runs after it switches to a new release, and you can run it by hand at any time. It changes nothing of yours.

```bash
warpzone health deck       # on the device
warpzone health            # the device is the one whose ID matches the local Syncthing
```

It prints one line per check, then `health: ok` or how many failed, and exits `0` when it works and `1` when any check failed:

- The config and the profiles load.
- `settings apply`, `play-state sync`, `links sync` and `frontend refresh` run as dry runs (only the ones that apply to the device), each within the 15 seconds that ES-DE gives its startup scripts, without an error. A menu that is open does not fail it.
- The ES-DE startup scripts and the systemd units that `provision device` installs for the device exist, and the Python they run exists and can import warpzone. The updater's service must run `sh` with a script that exists. Where there is no systemd user manager, the units are skipped.

It is not `doctor`: it does not look at DATs or optional tools, and it does not prove that a game starts or that Syncthing syncs. `frontend refresh` reads the local Syncthing's REST API, so a Syncthing that is down fails that check.

## The timer

`warpzone provision device <name>` installs two units in `~/.config/systemd/user/` and enables the timer, for every profile:

- `warpzone-self-update.service`, a one-shot with `TimeoutStartSec=30min` that runs `/bin/sh ~/.local/share/warpzone/updater/updater.sh tick`.
- `warpzone-self-update.timer`: `OnBootSec=45s` and `OnUnitActiveSec=15min`, with no random delay (a delay also pushes back the start after boot, when the menu may not be open yet).

The timer is a piece of its own, apart from the script: the script does not know what started it, so a platform without systemd can start the same `tick` from another launcher (cron, for example). It does not depend on ES-DE or on any other part of the profile. `provision device` is idempotent here: units are rewritten only when their content differs, the timer is enabled only when it is not, and `--dry-run` prints what it would do. It installs nothing, and warns, when the live copy is not there or there is no systemd user manager.

Each run of `provision device` also renews the live copy of the script and its settings:

- **The live copy.** The script that ships with the running version is written beside `~/.local/share/warpzone/updater/updater.sh`, checked with `sh -n` and with `sh <copy> selftest`, and only then replaces the live one with a rename (a tick that is running keeps the file it started with). The old one is kept as `updater.sh.prev`. A script with a lower `UPDATER_ABI` than the live one is never installed, so a rollback to an older release does not downgrade the script; a check that fails leaves the live one as it was and warns. While a release is on trial, the updater keeps the live copy as `~/.local/state/warpzone/updater/updater.sh.good` and puts it back when the device goes back to the previous version, so a rejected release cannot leave its own updater in charge. The kept copy is flushed to disk, and one that is empty or damaged (a power cut can leave a file of no bytes) is not put back.
- **The settings** (`~/.local/state/warpzone/updater/conf`), written from the device's table in `config.toml`: `device`, `config`, `clone`, `python`, `channel`, `pin`, `enabled` (from `self_update`), `frontend` (the frontend's process names, from the profile) and `install` (`auto`, or `manual` when the profile's `[updater] install` says so). One `key=value` per line; the script reads it line by line and never runs it.

`provision device` installs nothing, and warns, when the folder warpzone runs from is not a git checkout, or when its Python cannot import warpzone from outside the current folder: set the device up with `updater.sh install`.

`warpzone doctor` checks the timer (its units run the updater and the timer is enabled and running), the live copy, the settings against the config, and the updater's state: a rollback in the last 14 days, `stuck`, a `dirty` or `dev` checkout, a retired `pin`, signatures not required, no success for 14 days.

### The updater script

`warpzone/data/updater.sh` is POSIX `sh` and needs only `git` and the usual tools, so that it can put a working release back even when warpzone itself no longer starts. The rules it follows are the contract in [the design document](/warpzone/development/design/autonomous-updates/).

```bash
sh warpzone/data/updater.sh tick       # one run of the timer
sh warpzone/data/updater.sh tick --fetch-now   # the same, fetching even when not due
sh warpzone/data/updater.sh tick --now   # asked for by hand: no wait for the menu
sh warpzone/data/updater.sh install DEVICE   # the install above
sh warpzone/data/updater.sh menu       # open (exit 0), closed (1) or unknown (2)
sh warpzone/data/updater.sh selftest   # its tools and helpers; prints "selftest: ok abi=N"
git tag --list | sh warpzone/data/updater.sh order   # the release tags, best first
```

It reads `~/.local/state/warpzone/updater/conf` and keeps its state in `state` and `bad/` next to it, a log in `~/.local/state/warpzone/self-update.log` and the output of the last check in `last-verify.log`. A tick:

1. Takes a lock and stops if another tick holds it, or if `enabled` is `false`.
2. Defers if the frontend (ES-DE) is running, for at most 72 hours from the first deferral; after that it goes on. A tick with `--now` does not wait. On an `install=manual` device a timer tick only looks for a release and notes it in `available`; when an update asked for by hand left a trial or a probation, it finishes that first, without waiting for the menu, and never tries a release of its own.
3. Looks for a Python 3.11 or newer, fetches the remote at most once an hour on the `rc` channel and once every 12 hours on `stable` or with a pin (each device a little later, by up to a twenty-fourth of the interval; `WZ_FETCH_INTERVAL` sets another interval, `tick --fetch-now` fetches at once), then repairs the venv if it no longer works (see [Python repair](#python-repair)), and picks the best tag of its channel (`stable`: `vX.Y.Z`; `rc`: those and `vX.Y.ZrcN`, where `v0.3.0rc1` < `v0.3.0rc2` < `v0.3.0`) that is newer than the checkout, is an ancestor of `origin/main`, carries the same `__version__`, is not marked bad, is not retired (a signed tag `retired/vX.Y.Z`, see [Releasing](/warpzone/development/releasing/#retiring-a-release)) and, when the `warpzone/data/allowed_signers` of the version it runs holds a key, has a tag signed by it with SSH. What was chosen at one tick is checked again at the next, so a version that is retired, or off the channel, or no longer signed, while it was being tried is dropped. It never goes down, except when the version it runs has been retired: then it goes to the best valid one, even a lower one. With a `pin` the pinned tag is the target instead, whatever the channel, the running version or a retirement; the log and the `pin_retired` key of the state say when the pin is retired.
4. Checks the tag out (after checking there is room for it) and verifies it in three steps with a time limit each, and 25 minutes for the whole tick: warpzone imports from the checkout and `--version` is the tag; `provision device <device> --unattended`; `warpzone health <device>`. A step that is cut off takes its whole process group with it.
5. If a step fails it goes back to the version it came from and runs the same check. If that passes, the new tag is checked once more, and only if it fails again is it bad and never tried again (`bad/<tag>`). If the previous version fails too, the environment is at fault: the tag is not blamed and the device waits 1, 2, 4 and then 7 days. Running out of the tick's time counts as the environment too.
6. A power cut or a kill halfway never counts against a tag: after two attempts without a verdict it goes back and waits a day. A checkout that was cut in the middle is finished again by the next tick, as long as the only changed files are the update's own.
7. A new release is checked once more after the next boot (probation), against the version it replaced.

It only moves a checkout that is on a release tag or on a clean branch contained in `origin/main`. A branch with commits of its own, a loose commit or a tree with changes is left exactly as it is (the script never runs `reset`, `stash` or `clean`) and only gets `provision device --unattended` once per boot. An `index.lock` older than 10 minutes with no `git` running is removed.

Without a key in the allowed-signers file (the repository ships it with comments only) no signature is required and the log says so; `warpzone doctor` warns about it.

### Channel and pin

`channel` (`"stable"`, the default, or `"rc"`) and `pin` (a tag such as `v0.3.0` or `v0.3.0rc1`) can be set under `[devices.<name>]` next to `self_update`; the config is rejected when they are malformed. `provision device` writes them to the updater's settings. The behaviour is in [the design document](/warpzone/development/design/autonomous-updates/), and how a release is signed, published as a candidate or retired is in [Releasing](/warpzone/development/releasing/).

### Turning it off

Set it in the config, so that `provision device` and `doctor` agree:

```toml
[devices.deck]
# ...
self_update = false
```

Then run `warpzone provision device <name>` once: it writes `enabled=false` in the updater's settings, so the next tick does nothing, and it neither installs the updater nor the timer on a device that never had them. To remove the timer as well:

```bash
systemctl --user disable --now warpzone-self-update.timer
rm ~/.config/systemd/user/warpzone-self-update.{service,timer}
systemctl --user daemon-reload
```

`provision device` does not remove an installed timer by itself. A device with `self_update = false` can still be moved by hand: check out the tag you want in the clone.
