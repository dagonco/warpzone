---
title: "Self-update"
---

A device runs warpzone from a git clone with a venv next to it, and an updater script keeps the
clone on a release that works (the contract is in [Autonomous updates (contract)](/warpzone/development/design/autonomous-updates/)). There
is one way to install a device, from the clone itself: `git clone` the repository, then
`sh <clone>/warpzone/data/updater.sh install <device>`. A `pipx` or wheel install is not a
supported device install.

`selfupdate.py` holds what warpzone knows about the script, as pure functions apart from
`run_tick`: where the live copy, the settings, the state and the `bad/` folder are
(`live_path`, `conf_path`, `state_path`, `bad_dir`), `render_conf` and `parse_kv` for the settings file, `updater_abi` and
`install_decision` for renewing the live copy, `parse_bad`, `frontend_names`, and the wrapper
`run_tick`. The tests use real git repositories for the script and fakes for the rest.

- **Install (`updater.sh install DEVICE [--config FILE] [--venv DIR] [--channel stable|rc] [-- ARGS...]`).**
  Run from the clone, with a Python 3.11 or newer. Everything that can be refused is refused
  before anything changes: the clone must be on a release tag or on a clean branch inside
  `origin/main` (a tree with changes or commits of its own is refused, with no `reset`,
  `stash` or `clean`), a Python must exist, and a release must pass the rules of a tick
  (channel, ancestor of `origin/main`, its `__version__`, not retired, signed when signatures
  are required) and carry `warpzone/data/updater.sh`. The tags are fetched first when the
  remote answers. It then checks out the best such release of the channel (`stable` unless
  `--channel rc`), makes the venv (see below) unless it already works, checks that the release
  imports with it, and runs `python -m warpzone provision device DEVICE [--config FILE]
  [ARGS...]` from the clone, which installs the live copy, writes the settings and starts the
  timer. It takes the same lock as a tick. The venv is `venv` next to the clone unless
  `--venv` says another folder; a relative `--config` is taken from the folder the person ran
  it in. Exit `0` when it all worked, `2` for a usage error, `1` otherwise (and `provision`'s
  own code when it fails).
- **Python repair.** The settings name the venv's interpreter (`python`). At each tick, when
  that is `<folder>/venv/bin/python` and it does not run, or the venv no longer finds the clone,
  and another Python 3.11 or newer exists, the script makes the venv again with
  `python -m venv --without-pip --clear <folder>`, adds `warpzone-clone.pth` (the clone's path)
  to its `site-packages` and writes `bin/warpzone`, a shim that runs
  `python -m warpzone "$@"`. That needs no network and no setuptools (unlike `pip install -e`,
  which wants `setuptools>=77`), and the path of the venv's Python, which units and ES-DE scripts
  hold, stays the same. Everything runs with `-m warpzone`. `--clear` is run only on a folder
  that is called `venv`, does not exist or holds the mark `warpzone-venv` (which the script
  writes when it makes one, so a venv of the user's, or the one of an older install, is never
  cleared: `install` refuses it and a tick logs it and leaves it; the mark is also noted in
  `venv-owned` in the state folder before the `--clear`, so a power cut between the clear and
  the mark still leaves a venv that may be repaired, and `install` marks a working venv that
  has `warpzone-clone.pth` and no mark), is not a symlink and does not
  hold the clone; never on the folder around it, which holds `play-state`. A venv that works
  is left alone (it is judged by finding `warpzone` without importing it). A tick repairs only
  a checkout on a release (a tag or a clean branch inside `origin/main`), never a `dev` or
  `dirty` one.
- **The live copy.** `provision_updater.ensure_updater` puts the script that ships with the running
  version at `~/.local/share/warpzone/updater/updater.sh`, outside the clone. The new text is
  written beside it, checked with `sh -n` and with `sh <copy> selftest` (which must print
  `selftest: ok abi=N` for the script's own `UPDATER_ABI`), then the old file is saved as
  `updater.sh.prev` and the copy replaces it with a rename, so a tick that is running keeps the
  file it started with. `selfupdate.install_decision` is the rule: no live copy, or another
  script of an ABI that is not lower, installs; the same text is current; a copy of a lower ABI
  than the live one is never installed (a rollback to an older release does not downgrade the
  script); a script with no `UPDATER_ABI` is refused. A failed check leaves the live copy as
  it was and warns. The new text goes to a temporary file with a name of its own in the same
  folder, and what is live is read again, and judged by the same rule, just before the rename,
  so two `provision` runs at once cannot install a script that the other has not checked.
  **A copy that is kept during a trial.** The `provision --unattended` of a release under trial
  installs that release's updater, and a script with a fault `selftest` does not see would
  then stop the device from updating, with the previous version's `provision` unable to put the
  old one back (a lower ABI is never installed over a higher one, and a rollback may not run
  `provision`). So before the first switch of a trial the tick copies the live script to
  `updater/updater.sh.good` in the state folder (not again while it is there, since a resumed
  trial finds the candidate's script live) and, whenever it goes back to the previous version
  (a bad release, an environment failure, a trial given up, a failed probation), it puts that
  copy back with a rename, whatever the ABI of the script it replaces. The copy is dropped at
  the next tick when no trial or probation is on, and when probation passes. It is written to
  a temporary file, flushed with `sync` and renamed, and a live script is only kept, and a kept
  one only put back, when it is not empty, has its `UPDATER_ABI=` line and passes `sh -n`
  (a power cut can leave a file of no bytes); otherwise the live one is left as it is and the
  log says so. Only the live copy itself is ever kept or replaced: the running script, with
  every symlink resolved (`cd -P`, `pwd -P`), must be `~/.local/share/warpzone/updater/updater.sh`
  (`WZ_LIVE` moves it for tests), so the clone's own script, run by hand or through a symlink,
  is never touched.
- **The settings.** `provision_updater.ensure_self_update` writes
  `~/.local/state/warpzone/updater/conf` from the device's table in `config.toml`:
  `device`, `config` (the absolute path of the config file), `clone` (the git checkout warpzone
  runs from), `python` (the interpreter that ran `provision`), `channel`, `pin` (empty for
  none), `enabled` (from `self_update`) and `frontend` (the process names of the frontend:
  `[es_systems].process` of the profile, else ES-DE's). `selfupdate.render_conf` writes one
  `key=value` per line and raises on a value with a line break, so nothing in it can be
  executed. The file is rewritten only when its content differs. A device with
  `self_update = false` gets no updater, and one that already has settings gets `enabled=false`
  there, so its timer's ticks stop. Nothing is installed, with a warning, when the clone is not
  a git checkout or the interpreter cannot import warpzone from outside the current folder.
- **`warpzone self-update`.** A wrapper: it runs `sh <live copy> tick`, as the timer does, and
  exits with its code. It needs no config and has no `--dry-run`. Without a live copy it says
  so and exits `1`.

## Timer

`provision device` calls `ensure_self_update` for every device, whatever its profile and
folders; its last step is `ensure_update_timer`. `updatetimer.py` renders the two units (pure):
`warpzone-self-update.service` (`Type=oneshot`, `TimeoutStartSec=30min`,
`ExecStart=/bin/sh <live copy> tick`, the path quoted for systemd) and
`warpzone-self-update.timer` (`OnBootSec=45s`, `OnUnitActiveSec=15min`, no random delay,
`WantedBy=timers.target`) in `~/.config/systemd/user/`. The timer is a piece of its own, apart
from the script: another launcher can start the same `tick`. The units are rewritten only when
their content differs (mode 0644, atomically); `daemon-reload`, `enable --now` and, when the
timer was already running with other units, `restart` follow only when something changed or the
timer is not enabled and active. It installs nothing, with a warning, when the live copy is not
there or `systemctl --user show-environment` fails (no user manager); `--dry-run` runs only the
read-only `systemctl` queries and prints what it would do. `self_update = false` under
`[devices.<name>]` skips it; an installed timer is never removed by `provision`.

`doctor` checks the updater in group `Updates`, on a Linux machine that is a configured device
subscribed to `library` without `self_update = false` (found through the local `wz-library`
folder, as for the frontend):

- `Self-update timer` (`judge_update_timer`): `[ok]` when both units exist, the service runs
  `/bin/sh <live copy> tick`, and the timer is enabled and active; `[! ]` when the units are
  missing, the service runs something else (an old unit that ran `python -m warpzone
  self-update`), the timer is not enabled or not active, or systemd cannot be asked.
- `Updater script` (`judge_updater_script`): `[! ]` when the live copy is missing or differs
  from the one that ships with this version; `[ok]` when it is the same, or has a higher ABI
  (after a rollback).
- `Updater settings` (`judge_updater_conf`): `[! ]` when the file is missing, when `device`,
  `config`, `channel`, `pin`, `enabled` or `frontend` differ from what the config says, or when
  `python` is not executable or `clone` is not a git checkout.
- `Updater state` (`judge_updater_state`): reads `state` and `bad/`. One line per problem,
  `[! ]` unless noted: the updater has not run yet; a rollback in the last 14 days (a `bad/<tag>`
  file that recent, or `last_result=rolled-back`); `stuck` (`[!!]`); a `dirty` or `dev`
  checkout, which the updater does not move; `no-python`; `pin_retired`; signatures not required
  (`signing` is not `1`); no successful run (`last_ok`) in more than 14 days, or none at all.
  When none applies, one `[ok]` line.
