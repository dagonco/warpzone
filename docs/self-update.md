# Updating a device

A device runs warpzone from a git clone with an editable install in a venv, for example:

```
~/.local/share/warpzone/src      the clone
~/.local/share/warpzone/venv     the venv with `pip install -e src`
```

`warpzone self-update` moves that clone to the newest release. `warpzone provision device` also installs a timer that runs it every day, so a device follows the releases without anyone logging in.

## The command

```bash
warpzone self-update            # on the device
warpzone self-update --dry-run  # only say what it would do
```

It finds the clone from the installed package (the folder that holds the `warpzone` package) and then:

1. If that folder is not a git checkout (a `pipx` or wheel install, or a package folder inside some other repository), it says so and exits `0`. Update that kind of install with the tool that installed it.
2. Runs `git fetch --tags`. If the remote cannot be reached, it prints a short message and exits `0`, so a device that is offline never fails a scheduled run.
3. Picks the highest tag of the form `vX.Y.Z` (pre-release tags such as `v0.3.0rc1` are ignored) and compares it with the running version.
4. Does nothing when the running version is the same, or newer: it never downgrades. A device on a development checkout, ahead of the newest tag, stays where it is.
5. When the tag is newer: refuses if the clone has local changes to tracked files (exit `1`, nothing touched; untracked files do not count), otherwise checks the tag out (`git checkout --detach`).

Exit codes: `0` for every outcome that needs no attention (updated, up to date, ahead, offline, not a checkout), `1` when an update was due and could not be done (local changes, a failed checkout or reinstall).

`--dry-run` still runs `git fetch --tags` (it only adds tags and remote branches) but changes nothing else and writes no log.

### The version after an update

`warpzone --version` prints `__version__` from `warpzone/__init__.py`, and an editable install imports the package straight from the clone. After the checkout, the next `warpzone` process therefore reports the new version at once; nothing has to be reinstalled. What can go stale is the install metadata in the venv (what `pip list` shows, the entry point script and the package list), and that can only change when `pyproject.toml` does. So `self-update` runs `python -m pip install -e <clone>` with the venv's Python only when `pyproject.toml` differs between the old and the new commit. If that fails it prints the command to run by hand and exits `1`. It finishes by asking the venv's Python which version it now imports and prints it.

### Log

Every run except `--dry-run` appends its messages, with a UTC timestamp, to `~/.local/state/warpzone/self-update.log` (under `$XDG_STATE_HOME` when that is set). The file is cut to its last 200 lines when it passes 64 KiB. The timer's own output is in the journal: `journalctl --user -u warpzone-self-update`.

## Unattended provisioning

`warpzone provision device <name> --unattended` is the form of `provision device` for a run nobody watches, such as a device that has just moved to a new release. It does what `provision device` does and nothing that could lose data or fail for a reason that a person must look at:

- A folder that Syncthing already has never gets the bootstrap ignores (`*`). If its ignores are not the final ones and the whitelist files are on disk, it gets the final ones; if they are not, the folder is left as it is, with a warning.
- It refuses to change the `type` (for example `receiveonly` to `sendreceive`, which would send the console's deletions to the hub) or the `versioning` of a folder that exists, and a changed path. It lists them and changes nothing.
- A folder whose marker (`.stfolder`) is missing, such as one on a card that is not mounted, stops the run before any change.
- A folder that is new for the device and that the hub does not have yet (or lacks its whitelists) is skipped with a warning. A new folder whose whitelists do not arrive in 30 seconds (`--timeout` changes it) stays on the bootstrap ignores, with a warning, and the next run finishes it.
- The SyncThingy flatpak grant is not made, because it needs a Syncthing restart: the command to run is a warning.
- It never runs `prune --apply`, `inbox --apply` or `settings capture`.

Exit codes: `0` when it finished (warnings included), `3` when something was left for later because ES-DE, an emulator or a file ES-DE rewrites was open (run it again once it is closed), and `1` or `2` for a failure.

## Checking a version

`warpzone health [device]` says whether the installed version works on this device. It is the check the autonomous update will run after it switches to a new release, and you can run it by hand at any time. It changes nothing of yours.

```bash
warpzone health deck       # on the device
warpzone health            # the device is the one whose ID matches the local Syncthing
```

It prints one line per check, then `health: ok` or how many failed, and exits `0` when it works and `1` when any check failed:

- The config and the profiles load.
- `settings apply`, `play-state sync`, `links sync` and `frontend refresh` run as dry runs (only the ones that apply to the device), each within the 15 seconds that ES-DE gives its startup scripts, without an error. A menu that is open does not fail it.
- The ES-DE startup scripts and the systemd units that `provision device` installs for the device exist, and the Python they run exists and can import warpzone. Where there is no systemd user manager, the units are skipped.

It is not `doctor`: it does not look at DATs or optional tools, and it does not prove that a game starts or that Syncthing syncs. `frontend refresh` reads the local Syncthing's REST API, so a Syncthing that is down fails that check.

## The timer

`warpzone provision device <name>` installs two units in `~/.config/systemd/user/` and enables the timer, for every profile:

- `warpzone-self-update.service`, a one-shot that runs `<python> -m warpzone self-update` with the Python that ran `provision device` (the venv's).
- `warpzone-self-update.timer`: `OnCalendar=daily` with `Persistent=true` (a device that was off at the usual time runs it as soon as it is on), `OnBootSec=5min`, and a random delay of up to 30 minutes.

It does not depend on ES-DE or on any other part of the profile. `provision device` is idempotent here: units are rewritten only when their content differs, the timer is enabled only when it is not, and `--dry-run` prints what it would do. It installs nothing, and warns, when the Python cannot import warpzone from outside the current folder (run it from the venv) or when there is no systemd user manager (a device without systemd can schedule `warpzone self-update` with cron instead).

`warpzone doctor` warns on a device when the timer is not installed, not enabled or not running.

### The updater script

`warpzone/data/updater.sh` is the script that will replace `warpzone self-update` as what the timer runs. It is POSIX `sh` and needs only `git` and the usual tools, so that it can put a working release back even when warpzone itself no longer starts. **It is not installed or started by anything yet**: `provision device` does not copy it, and the timer still runs `warpzone self-update`. The rules it follows are the contract in [design.md](design.md#autonomous-updates-contract); this is what you can run today.

```bash
sh warpzone/data/updater.sh tick       # one run of the timer
sh warpzone/data/updater.sh menu       # open (exit 0), closed (1) or unknown (2)
sh warpzone/data/updater.sh selftest   # its tools and helpers; prints "selftest: ok abi=N"
git tag --list | sh warpzone/data/updater.sh order   # the release tags, best first
sh warpzone/data/updater.sh retired < RETIRED        # the tags it retires, the lines it refuses
```

It reads `~/.local/state/warpzone/updater/conf` (`device`, `python`, `clone`, `enabled`, `frontend`, `config`, `channel`, `pin`, one `key=value` per line) and keeps its state in `state` and `bad/` next to it, a log in `~/.local/state/warpzone/self-update.log` and the output of the last check in `last-verify.log`. A tick:

1. Takes a lock and stops if another tick holds it, or if `enabled` is `false`.
2. Defers if the frontend (ES-DE) is running, for at most 72 hours from the first deferral; after that it goes on.
3. Fetches the remote at most once a day, then picks the best tag of its channel (`stable`: `vX.Y.Z`; `rc`: those and `vX.Y.ZrcN`, where `v0.3.0rc1` < `v0.3.0rc2` < `v0.3.0`) that is newer than the checkout, is an ancestor of `origin/main`, carries the same `__version__`, is not marked bad, is not listed in `RETIRED` (a file at the root of `main`) and, when `warpzone/data/allowed_signers` holds a key, has a tag signed by it. It never goes down, except when the version it runs has been retired: then it goes to the best valid one, even a lower one. With a `pin` the pinned tag is the target instead, whatever the channel, the running version or `RETIRED`; the log and the `pin_retired` key of the state say when the pin is retired.
4. Checks the tag out (after checking there is room for it) and verifies it in three steps with a time limit each, and 25 minutes for the whole tick: warpzone imports from the checkout and `--version` is the tag; `provision device <device> --unattended`; `warpzone health <device>`. A step that is cut off takes its whole process group with it.
5. If a step fails it goes back to the version it came from and runs the same check. If that passes, the new tag is checked once more, and only if it fails again is it bad and never tried again (`bad/<tag>`). If the previous version fails too, the environment is at fault: the tag is not blamed and the device waits 1, 2, 4 and then 7 days. Running out of the tick's time counts as the environment too.
6. A power cut or a kill halfway never counts against a tag: after two attempts without a verdict it goes back and waits a day. A checkout that was cut in the middle is finished again by the next tick, as long as the only changed files are the update's own.
7. A new release is checked once more after the next boot (probation), against the version it replaced.

It only moves a checkout that is on a release tag or on a clean branch contained in `origin/main`. A branch with commits of its own, a loose commit or a tree with changes is left exactly as it is (the script never runs `reset`, `stash` or `clean`) and only gets `provision device --unattended` once per boot. An `index.lock` older than 10 minutes with no `git` running is removed.

Without a key in the allowed-signers file (the repository ships it with comments only) no signature is required and the log says so. Not done yet: installing the script, the allowed-signers file and the settings (`provision`), the venv repair, the timer units that run it, and the `doctor` checks.

### Channel and pin

`channel` (`"stable"`, the default, or `"rc"`) and `pin` (a tag such as `v0.3.0` or `v0.3.0rc1`) can be set under `[devices.<name>]` next to `self_update`; the config is rejected when they are malformed. `updater.sh` uses them (see above); `warpzone self-update` does not: it still follows plain `vX.Y.Z` releases. The behaviour is in [design.md](design.md#autonomous-updates-contract), and how a release is signed, published as a candidate or retired is in [releasing.md](releasing.md).

### Turning it off

Set it in the config, so that `provision device` and `doctor` agree:

```toml
[devices.deck]
# ...
self_update = false
```

Then remove the timer once on the device:

```bash
systemctl --user disable --now warpzone-self-update.timer
rm ~/.config/systemd/user/warpzone-self-update.{service,timer}
systemctl --user daemon-reload
```

`provision device` does not remove an installed timer by itself. A device with `self_update = false` is still updated by hand with `warpzone self-update`.
