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

## The timer

`warpzone provision device <name>` installs two units in `~/.config/systemd/user/` and enables the timer, for every profile:

- `warpzone-self-update.service`, a one-shot that runs `<python> -m warpzone self-update` with the Python that ran `provision device` (the venv's).
- `warpzone-self-update.timer`: `OnCalendar=daily` with `Persistent=true` (a device that was off at the usual time runs it as soon as it is on), `OnBootSec=5min`, and a random delay of up to 30 minutes.

It does not depend on ES-DE or on any other part of the profile. `provision device` is idempotent here: units are rewritten only when their content differs, the timer is enabled only when it is not, and `--dry-run` prints what it would do. It installs nothing, and warns, when the Python cannot import warpzone from outside the current folder (run it from the venv) or when there is no systemd user manager (a device without systemd can schedule `warpzone self-update` with cron instead).

`warpzone doctor` warns on a device when the timer is not installed, not enabled or not running.

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
