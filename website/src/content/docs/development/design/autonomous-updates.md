---
title: "Autonomous updates (contract)"
---

This page is the contract for making a device keep itself on a working release with no
person logged in. Each piece says whether it exists. **Implemented** means it is in the code
today; **Not implemented** means the code does not do it yet and this text is what it must
do.

| Piece | State |
| --- | --- |
| `channel` and `pin` in the device config, validated | Implemented |
| Ordering of release candidates (`tag_key`, `version_key`) | Implemented |
| `provision device --unattended` and its exit code `3` | Implemented |
| `warpzone health` | Implemented |
| `warpzone/data/updater.sh tick`: lock, `enabled`, deferral while the menu is open (72 hours at most), fetch at a pace set by the channel, `tick --fetch-now`, the target (the best valid tag, see Channels), the three-step verification with a time limit, the differential verdict and its back-off, `bad/<tag>`, the state file, the attempts loop, probation, the kind of checkout, the stale `index.lock`, `menu` and `selftest` | Implemented (`tests/test_updater_sh.py`) |
| In `updater.sh`: the `rc` channel, retirement by `retired/vX.Y.Z` tags, `pin`, and the check of tag signatures against the `allowed_signers` of the running version | Implemented (`tests/test_updater_sh.py`) |
| `updater.sh install` (the human install from a `git clone`), the Python repair (`venv --without-pip --clear`, a `.pth` to the clone, the `bin/warpzone` shim), `UPDATER_ABI` | Implemented (`tests/test_updater_sh.py`) |
| The live copy `~/.local/share/warpzone/updater/updater.sh` (`provision_updater.ensure_updater`: atomic, after `sh -n` and `selftest`, `.prev` kept, never a lower ABI) and the settings file written by `provision device` (`selfupdate.render_conf`) | Implemented (`tests/test_update_timer.py`, `tests/test_selfupdate.py`) |
| The timer units that start `updater.sh tick`, and `warpzone self-update` as a wrapper of one tick | Implemented (`tests/test_update_timer.py`, `tests/test_cli.py`) |
| The `doctor` checks of the updater (timer, script, settings, state) | Implemented (`tests/test_doctor.py`) |
| `provision hub --additive` and `auto_hub` (see Hub folders from a console) | Implemented (`tests/test_provision.py`, `tests/test_cli.py`, `tests/test_config.py`) |

**Tag spelling.** A release is `vX.Y.Z` and a release candidate is `vX.Y.ZrcN`: the tag is
`v` plus `__version__`, in the PEP 440 spelling that [Releasing](/warpzone/development/releasing/) requires
(`v0.3.0-rc1` is not a tag warpzone knows). They order as `v0.3.0rc1` < `v0.3.0rc2` <
`v0.3.0`, and every number is compared as a number (`rc10` is after `rc2`).
`versions.tag_key` returns that key, or None for any other spelling; `version_key` gives
the same key for a package version, so a tag and the version it carries compare equal.
`parse_tag`, `newest_tag` and `decide` still look at plain releases only.

**One tick.** A single timer starts a single script. The unit keeps the name
`warpzone-self-update`, so rewriting it leaves no orphan unit. The timer has `OnBootSec=45s`
and `OnUnitActiveSec=15min` and no random delay (a delay also pushes back the start after
boot, when the menu may not be open yet); the service is a one-shot with
`TimeoutStartSec=30min` and `ExecStart=/bin/sh <updater> tick` (`updatetimer.py`, written by
`provision device`). The timer is a piece of its
own, separate from `updater.sh tick`: the script does not know what started it, so a platform
without systemd can start the same `tick` from another launcher. Each tick, in this order:

1. Take a lock; a tick that finds it held exits at once.
2. Read the configuration; if the device is not `enabled` (`self_update = false`), stop. With
   `install=manual` and no `--now`, only look for a release (see "Installing by hand" below).
3. If the menu is open, defer (see below); a tick with `--now` does not.
4. Look for an interpreter that can run warpzone (`no-python` when none).
5. Fetch the remote at most once per interval: one hour on the `rc` channel, 12 hours on
   `stable` or with a pin (a pin only needs new tags to find its target);
   `WZ_FETCH_INTERVAL` sets another interval, and `tick --fetch-now` fetches at once. The
   moment is offset by a number derived from the device name (a delay of up to a
   twenty-fourth of the interval, `cksum` of the name modulo that), so devices do not all
   arrive together. A fetch that fails is logged and tried again
   at the next tick; the tags already fetched are still used. Then repair the venv when it no
   longer works (see "Python repair" in [Self-update](/warpzone/development/design/self-update/)).
6. Pick the target tag. When there is one, switch to it and verify it. When there is none,
   reconcile (`provision device --unattended`) only if `/proc/sys/kernel/random/boot_id`
   differs from the last tick's, or `pending` is set. While the device waits out a back-off
   (`retry_after`) it neither tries a release nor reconciles.

**The script lives outside warpzone.** The tick must be able to repair a warpzone that no
longer starts, so the selection and the rollback are in `warpzone/data/updater.sh`, POSIX
`sh`, standard tools and `git` only (about 250 lines), and not in Python. This is the one
exception to "decisions are pure Python"; tests run the script against real repositories
with a fake Python. The script is far longer than the first estimate of 250 lines, because
every decision above is spelled out in it. The live copy is `~/.local/share/warpzone/updater/updater.sh`, outside
the clone, so checking out any tag cannot remove or replace it. `provision` renews it
atomically, only after `sh -n` and `updater.sh selftest` pass, keeping the old file as
`.prev`. The script carries `UPDATER_ABI`, and a copy with a lower ABI is never installed
over a higher one, so a rollback does not downgrade the script. It reads its settings from
`~/.local/state/warpzone/updater/conf`, written by `provision` from `config.toml`
(`device`, `config`, `channel`, `pin`, `enabled`, `frontend`, and the `python` and `clone` it
runs), line by line and never with `source`, so a value cannot run anything. The script reads
`device`, `python` (the interpreter to try first), `enabled` (`false`, `0`, `no` or `off`
turn it off), `frontend` (the process names of the frontend, separated by spaces; default
`es-de ES-DE.AppImage`), `clone` (the git checkout; default two folders above the script; a tick
that ends up with no clone at all logs it and exits `1` without touching anything) and
`config` (passed as `--config`), `channel` (`stable` or `rc`; anything else is `stable`, with a
line in the log) and `pin` (a tag; one that is not a tag is ignored, with a line in the log).
For tests every path
can be moved: `WZ_STATE_DIR` (state, settings, `bad/`, lock), `WZ_CONF`, `WZ_LOG`, `WZ_PROC`,
`WZ_BOOT_ID_FILE`, `WZ_NOW` (the clock), `WZ_FETCH_INTERVAL`, `WZ_STEP_TIMEOUT`,
`WZ_TICK_BUDGET`, `WZ_FETCH_TIMEOUT`, `WZ_MIN_FREE_KB`, `WZ_PYTHON_FALLBACKS` and
`WZ_ALLOWED_SIGNERS`. A value
that is not a number is replaced by the default. A person installs a device with `git clone` and
`sh <clone>/warpzone/data/updater.sh install <device>`, which checks out the newest release,
creates the venv and runs `provision device` (see [Self-update](/warpzone/development/design/self-update/)). There is one way to
install; a `pipx` or wheel install is not a supported device install. The tests add
`WZ_PYTHON_FALLBACKS` pointing at a Python that can make a venv for `install` and the repair.

**Verifying a version.** A version passes when all of these exit 0 within a time limit:

1. `python -I -c "import warpzone.cli, warpzone.provision, warpzone.health, ..." <clone>`,
   which also checks that `warpzone.__file__` is under the clone (`-I` imports the installed
   package, which is the checkout's only for an editable install; an install somewhere else
   would pass or fail for reasons that are not the release's, so it exits 7 and counts as
   the environment, not as a failure of the release), and `python -I -m warpzone --version`
   printing `warpzone <tag without v>`.
2. `python -m warpzone provision device <device> --unattended`.
3. `warpzone health` (see [`warpzone health`](#warpzone-health)): loads the config and the
   profiles, runs `settings apply`, `play-state sync`, `links sync` and `frontend refresh` as
   dry runs within the 15 seconds ES-DE allows its startup scripts, and checks that the
   scripts and units exist with a valid interpreter.

Each step has a time limit (`WZ_STEP_TIMEOUT`, by default 120 s for the import and the
version, 900 s for `provision` and 300 s for `health`), and the whole tick has a **budget**
of 25 minutes from its start (`WZ_TICK_BUDGET`, 1500 s), under the service's
`TimeoutStartSec=30min`: the budget cuts down the limit of every step, so two verifications
(the release and, after a failure, the one it came from) cannot add up to more than the
service allows. A step that runs out of its own time fails (a release that hangs is a bad
release). A step that was cut short by the budget says nothing about the release: the
verification gives up as "could not be judged", the device goes back to the version it came
from without marking anything bad, and it counts as an environment failure (the back-off
below). Every step runs in a process group of its own (`setsid` where it exists, job control
otherwise) and a cut kills the whole group, SIGTERM and then SIGKILL, so a grandchild cannot
outlive the step; the output of a step goes to a file, never to a `$(...)` that a grandchild
could hold open (this includes `git fetch`, limited by `WZ_FETCH_TIMEOUT`, 120 s). When the
tick itself receives SIGTERM it takes the step in progress down with it. The budget is
measured on the uptime (the first field of `/proc/uptime`, which only goes forward) and on
the date only where there is none: a device without a battery-backed clock gets its date set
by NTP in the middle of a tick, and a jump of the date must not make every later step run out
of time. A budget that runs out while looking for a Python is the environment (`env`), not
`no-python`. The script has no
`timeout` command to lean on (macOS has none), so it polls a status file. The output of the
steps goes to `last-verify.log` next to the state.

`doctor` is not used for this because it depends on the environment (DATs, tools). The check
does not prove that a game starts or that Syncthing syncs; that is what retirement is for.
`provision device --unattended` never does what could lose data: it does not apply the
bootstrap ignores to a folder that already exists (it uses the final ones when the
whitelists are on disk, and otherwise leaves the folder alone), it refuses to change `type`
or `versioning` of an existing folder, it turns a new folder in an existing group and a slow
`wait_for_files` into warnings, and it aborts without changes when a folder that should
exist has no `.stfolder` (a card that is not mounted). It never runs `prune --apply`,
`inbox --apply` or `settings capture`.

How `provision device --unattended` does that, and what it leaves as it was:

- **Existing whitelisted folder.** `_provision_whitelisted` never puts the bootstrap
  ignores (`*`) on a folder Syncthing already has. If its ignores are not the final ones, it
  checks that every whitelist file is on disk in the folder: when they are, it sets the final
  ignores and rescans; when one is not, it warns and changes nothing. (Done by hand, the
  bootstrap would leave a populated library ignored if the hub could not deliver the lists.)
  A folder that is new in this run still goes through the bootstrap.
- **`type`, `versioning`, path.** Before any write, every existing folder is compared with the
  config. A `type` or `versioning` that `_folder_patch` would change, or a path that
  differs, raises `ProvisionError` listing every folder, and nothing is changed.
- **Marker.** Before any write, each folder Syncthing has must hold its marker (`markerName`
  of the folder, `.stfolder` by default) on disk. A missing one raises `ProvisionError` and
  changes nothing. A folder with `skip_if_missing` whose directory is gone is skipped as
  before, and an unattended run does not create that directory for a folder Syncthing
  already has.
- **New folders.** A folder that is not in the device's Syncthing yet and fails the hub
  pre-check (the folder is not on the hub, or its whitelists are not) is warned about and
  left out; the same failure for an existing folder is still an error. The wait for the
  whitelists of a new folder is `UNATTENDED_WAIT_SECONDS` (30 s) unless `--timeout` is
  given, and running out of time is a warning: the folder stays on the bootstrap ignores
  and the next run finishes it.
- **Flatpak.** `ensure_flatpak_access` grants nothing and warns with the `flatpak override`
  command and the restart it needs.
- **Exit code.** `0` ok (warnings do not change it), `3` deferred, any other value failed.
  The run does all it can, then exits `3` when any write was held back because the menu
  (or an emulator, or the ES-DE settings file) was open, which is what `Reporter.deferred`
  records: `_busy`, the ES-DE settings and find-rule checks, and `titlelinks.sync`. Not being
  able to tell (no `/proc`) is a warning, not a deferral, because waiting would never help.
  An error exits `1`, also when something was deferred before it.

## Hub folders from a console (`auto_hub`)

Without it, a folder a release adds (a new save folder, say) has no counterpart on the hub
until someone runs `provision hub`, so the console's folder only waits for the hub. With
`auto_hub = true` under `[devices.<name>]` (default `false`, because it is a write to the
hub from a console), `provision device` first creates on the hub the folders it lacks. The
same step is `provision hub --additive`, which a person can run from anywhere. The rules:

- **Only creation.** `provision_hub.additive_plan` (pure) takes the hub specs and the hub's
  folders and returns what to create. A folder is created only when the hub does not have its
  ID, it is a `wz-*` folder and it is not whitelisted (`library` and `songs` stay with a
  person: their whitelists must be there and the first scan of a library takes hours). A
  folder the hub has is never patched, never re-pathed, never has its type, versioning,
  devices, ignores or paused flag changed, so a console with an old config or channel reverts
  nothing and there is no question of whose config wins. Where an existing folder differs
  from the config (path, type, or a subscribing device it is not shared with) the run only
  warns, naming `provision hub` as the way to change it. A missing folder whose path another
  hub folder already has (compared without a trailing slash and in any case, whatever that
  folder's ID) is not created either, with a warning: two folders on one directory would each
  sync it their own way. A `wz-*` folder the hub has paused is reported too (a creation that
  stopped after the POST looks like this) and left paused, naming `provision hub` as the way
  to finish it.
- **Only what the device needs.** From a device, the run is limited to the folders of that
  device that this run is going to provision (the ones left out because a save directory is
  missing are not made on the hub). The hub folder is shared with every device that
  subscribes to it in the config, as `provision hub` would, and a subscriber the hub does not
  know yet is added as a device first.
- **Safe to write.** The API must answer as `hub.device_id` (else `ProvisionError`, nothing
  written). The hub's folders are read again right before each creation (after any device it
  adds, since `POST /rest/config/folders` replaces a folder with the same ID), and a folder
  that appeared in between is left alone. A new folder is created paused, gets its ignores and is
  then resumed, as `provision hub` does. If setting the ignores or resuming fails, the folder
  stays paused and the run gives one warning for that folder ID (its pending notes are
  dropped, so they cannot end up in a later line for the same ID) and goes on.
- **Never a failure of the device.** From `provision device`, an unreachable hub, a refused
  key or another device behind the API is a warning (`auto_hub: ...`) and the run goes on
  with the hub as it is. A console must not roll back a release because the hub is away. A
  whitelisted folder the hub lacks is still reported by the hub pre-check.
- **Dry run.** `--dry-run` of either command sends no write to the hub. Syncthing offers no
  way to leave a marker on the hub through its API, so two consoles racing to create the same
  folder are only separated by the re-read above.
- **Unattended.** `provision device --unattended` does it too: it is the case this exists
  for.

## `warpzone health`

`warpzone health [device]` answers one question: does the installed version work on this
device? It exits `0` when it does and `1` when any check failed, and it writes no user data.
Without a device name it is the configured device whose ID matches the local Syncthing (as
`prune` does). Each check prints one line (`ok`, `FAIL` or `skip`), then `health: ok` or the
number of failed checks. `--config` and `--syncthing-config` work as elsewhere. In order:

1. **Config and profiles.** The same load every command does, including that each device
   subscribes only to folder groups its profile defines. A load error is a failed check and
   nothing else runs.
2. **The dry runs.** Only those that apply to the device, as `plan_steps` decides:
   `settings apply` when it lists `settings`; `play-state sync` when it lists `play-state`,
   its profile has an ES-DE folder and no `[artwork]` (a device with `[artwork]` merges the
   play state in `frontend refresh`); `links sync` when it lists `library` and its profile has
   `[links.*]`; `frontend refresh` when its profile has `[artwork]` and it lists `frontend`.
   They run in this process, not through the startup scripts, which would write. A step fails
   when it raises, when `settings apply` returns non-zero, or when it takes longer than the
   15 seconds (`STARTUP_SECONDS`) of the `timeout 15` that ES-DE's scripts use. The time is
   measured, not enforced: the caller bounds the whole run. Warnings are counted and are not
   failures, and a menu that is open is not a failure either, since a dry run never waits for it.
3. **Scripts and units.** `expected_files` lists what `provision device` leaves for this
   device: the ES-DE startup scripts (`settings`, `library` with `[links.*]`, `play-state`), the
   artwork and play state timers when the profile sets `refresh`, and the self-update units
   unless `self_update = false`. A script must exist, be executable and run an interpreter that
   exists and imports `warpzone` from outside the current folder. A service must exist and its
   first `ExecStart` word must exist (and import `warpzone` when it runs `-m warpzone`); the
   self-update service runs `/bin/sh <live copy> tick`, so the live copy must exist too. A
   timer must exist. Services and timers are `skip`ped, not failed, when there is no systemd
   user manager (`systemctl --user show-environment` fails), since `provision device` installs
   none there. Enabled or active timers are not checked.

**What each dry run does and does not write.** `settings apply --dry-run` takes no lock, writes
no backup, snapshot or log line and no `last apply` record. `links sync --dry-run` only says
what it would do. `play-state sync --dry-run` used to take the play state lock, which creates
a lock file and its folder; a dry run now takes no lock. `frontend refresh` builds its local
Syncthing client with `dry_run=True`, which sends only `GET`, and its gamelist step takes the
`dry_run` path, which also publishes no ledger. So the Syncthing configuration, the
gamelists, the ledgers, the emulators' files and the state folders are the same before and
after (a test compares the whole home). The one thing it needs from the network is the local
Syncthing's REST API for `frontend refresh` (and for the device name when none is given):
unreachable, that step fails, and the differential verdict tells that from a bad version. With
no local Syncthing found, `frontend refresh` runs without the scope check and warns, as the
command itself does.

It is not `doctor`: it does not look at DATs or optional tools, does not run the hub checks,
and does not prove that a game starts or that Syncthing syncs. The decisions are pure
(`plan_steps`, `expected_files`, `judge_step`, `judge_file`, `exit_code` in `health.py`) and
the tests run it against a fake `/proc` and a fake Syncthing.

**Differential verdict.** The script does not trust the exit codes of a version that may be
broken. It knows three outcomes: 0 passed, 3 deferred (the menu opened during the check),
anything else failed. After a failure it goes back to the `good` version and runs the same
check:

- `good` passes: the new version is checked once more (it is checked out again and goes
  through the same three steps), so that a failure that does not repeat, such as a service that
  was slow to come up, does not burn a release for good. Only if it fails again is it
  bad, and `bad/<tag>` is written. If it passes the second time it is accepted. The same
  second look is given to a release that fails its probation check. When there is no time
  left in the tick for the second look (a release that hangs spends 900 s on the first), or
  the checkout for it fails, the release is only **under suspicion**: `suspect_tag` and
  `suspect_sha` go in the state, the device stays on `good` without a back-off, and the
  next tick (15 minutes later) looks at it first thing, with the whole budget. If it passes it
  is accepted. If it fails, the previous version is checked again as in any first look: it
  passes, and the release is bad; it fails too, and the environment is at fault (back-off,
  nothing burned, since the release may have been fine and the environment is what changed
  in between); there is no time left to tell, and the release stays under suspicion. A
  suspicion is dropped when somebody moves the checkout by hand or when the suspect is no
  longer newer than what is checked out: the second look never goes down. Without this, a release that hangs `provision`
  would never be burned and would cost 25 minutes of every back-off.
- `good` fails too: the environment is at fault (Syncthing down, a broken config). The tag
  is not marked bad; the device stays on `good` and retries after 1, 2, 4 and then 7 days
  (`env_failures` counts the failures in a row and a success resets it).
- `good` answers 3 (the menu opened during the second check): nothing can be concluded. The
  tag is not marked bad, `trying` is cleared and the next tick starts again.
- The verification could not be judged (it ran out of the tick's budget, or python imports
  a warpzone that is not the checkout's): no tag is blamed, the device goes back to `good`
  and backs off as for an environment failure.
- Checking out the target fails (for example a file in the way): that says nothing about the
  release, so it is treated like a broken environment.

**State.** `~/.local/state/warpzone/updater/state`, `key=value` lines, written only by the
script, to a temporary file that is renamed. Keys: `schema`, `good_tag`, `good_sha`,
`trying_tag`, `trying_sha`, `from_sha`, `attempts`, `probation`, `pending`,
`deferred_since`, `last_fetch`, `last_boot_id`, `last_result`, `retry_after`,
`env_failures`, and four that the script adds: `good_branch` (the branch to return to when
the version it started from was a clean branch inside `origin/main`, empty for a tag),
`from_tag` and `from_branch` (with `from_sha`, the version before the last switch, kept while
the new one is on probation), `last_ok` (when something last went well, for `doctor`) and
`suspect_tag` with `suspect_sha` (a release that failed once and has to be looked at again,
see the differential verdict), and `pin_retired` (the pinned tag while it is retired,
empty otherwise), `signing` (`1` once signatures have been required) and
`signers_version` (the version whose keys are in `allowed_signers.last`). A tag and its sha
go together in `trying` and in `suspect`:
one without the other is dropped, and `attempts` with it.
`last_result` is one of `ok`, `deferred`, `env`, `rolled-back`, `dirty`,
`dev`, `no-python`, `stuck`. `bad/<tag>` is a file holding `sha=`, `date=` and `reason=`
lines. The log stays in `self-update.log` (cut to its last 200 lines past 64 KiB). Values are
read as data: a key that is not on the list or not made of lowercase letters and underscores
is ignored, a number that is not a number is reset, and nothing is ever evaluated. `doctor`
reads the state and warns about a rollback, `stuck`, `dirty`, or more than
14 days without a success.

The exit status of a tick is `0` when nothing needs attention (updated, up to date, deferred,
a checkout that is `dev` or `dirty`, a lock held by another tick, `enabled` off) and `1` when
a release was rolled back, the environment is at fault, the previous version could not be
checked out (`stuck`), no Python was found, or the tick could not run. A tick killed by `HUP`,
`INT` or `TERM` stops its running step and exits `143` (the same trap in `install`); one killed
by `KILL` or a power cut leaves no status at all.

**Bad versions and the loop.**

- Before the checkout, `trying_*` and `attempts + 1` are written and synced to disk. A tick
  that finds `trying` already set knows the previous run died halfway (power cut, kill,
  hang) and resumes by verifying the tag (checking it out first when the checkout had not
  happened), counting another attempt. A check that ends in a deferral (3) is not an
  attempt and gives its count back, so a long deferral never uses them up.
- **A run that died halfway is not a failure of the version.** Only a verification that
  failed, followed by a passing `good`, writes `bad/<tag>`. A power cut never does.
- A tag is switched to at most twice without a verdict. When `attempts` reaches 2 and the
  runs keep dying, the device returns to `good` without marking the tag bad, sets
  `retry_after` one day ahead and resets `attempts`.
- A `bad` tag never comes back, and a tick that finds `trying` set for a tag that has
  become bad meanwhile goes back to `good` and clears `trying` instead of resuming.
- **A checkout the updater itself cut in the middle** (a power cut, a full disk during
  `git checkout`) leaves a tree that is neither one commit nor the other, and every later
  tick would see a dirty tree. When `trying` is set and every file that differs from HEAD
  is one that the two commits involved differ in, **and** whose content is that of one of the
  two commits (`git hash-object` against the blobs; a file that is gone or empty counts too,
  and a path that git has to quote never does), the change is the updater's own and the
  checkout is done again with `--force` onto the target; a failed checkout, including the
  one of a second look, is repaired the same way, back to where it started. A file that the
  two commits do not differ in, or that differs in content (the user edited a file that the
  release also changes, which can go unnoticed for hours of deferral), is somebody's work:
  the tree stays `dirty` and untouched. A new version on probation is not repaired at all,
  because no checkout of ours is in progress then. Before switching,
  the script checks that the clone's file system has at least `WZ_MIN_FREE_KB` (50 MiB)
  free, and if not it counts as an environment failure without touching the tree. Every
  checkout uses `--no-overwrite-ignore`, so an ignored file of the user's that a release
  also tracks stops the checkout instead of being overwritten.
- The new version stays on `probation` until it passes the check on the next boot; a
  failure there goes through the differential verdict.
- After probation there is no automatic rollback: a later failure comes from a change in
  the system, not from the version. New tags are still tried.
- If checking out `good` fails, the result is `stuck` and nothing else is touched.
- `.git/index.lock` is removed only when it is older than 10 minutes and no `git` process
  is alive (found in `/proc` like the menu; where `/proc` cannot be read the lock stays).
- The lock is a folder (`mkdir` is atomic) holding the pid and the boot id, which the
  tick reads back after writing to be sure it is the one that owns it. A tick that
  finds it held by a live process exits at once; one that finds a dead pid, a lock written
  before the last boot (the pid may belong to somebody else now) or a lock older than three
  hours, takes it over: it renames the lock to a name of its own (only one tick wins that),
  checks that what it moved is the lock it judged stale, and only then makes a new one.
- Times in the state that lie in the future (a clock without a battery-backed RTC can
  have been wrong when they were written) are dropped when the state is read: `last_fetch`,
  `deferred_since` and `last_ok` more than a day ahead, `retry_after` more than 8 days ahead.
  A tag or a branch in the state that is not a plain `vX.Y.Z` or a plain branch name is
  dropped as well, because they end up in file names and git commands.
- If `git fetch` refuses because a tag already here was moved upstream ("would clobber"),
  `last_fetch` is updated anyway and the refusal is logged, so that it is not retried at
  every tick.
- The probation check runs at the first tick after a boot (`boot_id` differs from the one
  recorded when the release was installed) and compares with `from_*`, the version before
  the switch. If the new release fails and the old one passes, the new release is marked bad
  and the device goes back; if both fail, the device stays on the new release (an environment
  that is not ready yet after a boot, such as Syncthing starting, must not downgrade it) and
  backs off like any environment failure. If the second look at the new release cannot be
  made (no time, or the checkout fails), the device stays on the previous version, which is
  the reference again, and the release is under suspicion as described above.

**Installing by hand.** `tick --now` is an update asked for by hand (`warpzone self-update
--now`, or "Install <tag> now" in the Warpzone entry of the Options menu, which runs from the menu
itself): it fetches at once, the open menu does not defer it, `retry_after` does not hold it
back, and a step the menu still holds back (exit 3) counts as passed and leaves `pending` set,
as past the 72 hours. A game cannot be running when it is asked for, and every write is still
guarded by `_busy`. `tick --fetch-now` keeps its meaning: fetch at once, and wait for the menu.
A device whose profile sets `[updater] install = "manual"` (the settings key `install`) never
installs from the timer: a tick without `--now` takes the lock, fetches when due, finds the best
target as usual (signatures, retirements, the pin) and writes it to the state key `available`
(`last_result=available`, or `ok` when there is none; `last_ok` when it fetched), then ends. It
does not wait for the menu, verify or reconcile, not even on a dirty or development checkout; the
next `--now` does all of that and clears `available`. A trial or a probation in progress is the
exception: it was asked for by hand, and the timer finishes it the usual way (menu wait
included), so a release that no longer starts is still put back without anybody asking; such a
tick tries nothing new (no second look at a suspect, no new release) and ends by noting the best
target in `available`. An unknown value
is logged and read as `auto`. `UPDATER_ABI` is 2 from the version that knows `--now`;
`selfupdate.run_tick` sends `--now` to a live copy of that ABI or higher and `--fetch-now` to an
older one.

**Deferring while the menu is open.** The script scans `/proc/*/comm` and `argv[0]` the way
`procs.running` does (`WZ_PROC` points it at a fake tree in tests, and a test checks
that both agree). The names come from `esde.ESDE_PROCESSES` (unconfirmed on a console, as
that module says) or from `es_systems.process`, depending on the profile. ES-DE stays alive
during a game, so nothing updates while playing either. When it is open the tick sets
`pending=1`, sets `deferred_since` if it was not set, and exits 0; the 15 minute timer tries
again. A tick that finds the menu closed clears `deferred_since`, so the 72 hours are of an
unbroken wait and a menu that opens again days later starts a new one; an environment failure
and a failed reconcile clear it too. The frontend's process names are split into words with
pattern expansion off, so a name is never a glob. If it opens during the check, `--unattended` exits 3: `trying` is kept without a
rollback and the next tick continues. **The deferral lasts 72 hours at most**: once
`deferred_since` is older than that, the update runs anyway, because on some devices the
menu looks open for as long as the console is on, and every write is already guarded by
`_busy`. Past the limit a step that still answers 3 counts as passed and leaves `pending`
set, since otherwise a release could never be accepted on such a device. Where `/proc`
cannot be read, the script goes on (waiting would never help). `updater.sh menu` prints
`open`, `closed` or `unknown` and exits 0, 1 or 2; a test compares it with
`procs.running` on the same fake `/proc` trees.

**Python.** The interpreter must run `python3 -I -c "import tomllib, sys"` and be 3.11 or
newer; the candidates are the one in `conf`, `/usr/bin/python3` and `python3`. With none the
result is `no-python` and nothing rolls back. With an interpreter but a broken venv, the
script recreates it with `python3 -m venv --without-pip --clear <venv>`, plus a `.pth` file
that points at the clone and a `bin/warpzone` shim that runs `python -m warpzone`. That
needs no network and no setuptools, and it keeps the path written into units and scripts.
Only `venv/` is cleared; its parent folder also holds the two-way `play-state` folder and is
never deleted.

**Channels.** `channel = "stable"` (default) considers only `vX.Y.Z` tags; `channel = "rc"`
considers those and `vX.Y.ZrcN`. A candidate is valid when it matches the channel, is not
under `bad/` and not retired, its tag is an ancestor of `origin/main`, its `__version__`
read with `git show <tag>:warpzone/__init__.py` equals the tag without its `v`, and its
signature verifies when one is required (below). That reproduces, without a server, the
central check of `verify` in `release.yml`. The target is the best valid candidate that is
higher than the running version. The order is `tag_key`'s: the script builds a text key from
the three numbers, the stage and the candidate number, each padded to nine digits (a number
with more digits is not a version it knows), and sorts it; `updater.sh order` prints tags in
that order, and the tests compare it with `versions.tag_key` and with
`git -c versionsort.suffix=rc for-each-ref --sort=-v:refname`. A running version that is not
a release or a candidate (`0.3.0.dev0`) sorts before every candidate of its numbers, as
`version_key` does. A device never moves to a lower version than the one it runs, except when
that one is retired (below) or the tag is the `pin`. A device on a candidate whose channel is
then set to `stable` moves up to the release when it exists and otherwise stays.

**Retiring.** A tag cannot be edited, so a release is withdrawn by another tag: an
annotated `retired/vX.Y.Z` (or `retired/vX.Y.ZrcN`) whose message is the reason, normally on
the retired release's commit and signed like a release. There is no file to edit: a file in
`main` would let whoever controls the account (but not the signing key) withdraw the good
version and force a device down. The script looks a retirement up in the clone's tags, which
it takes with its regular fetch, so a device sees one within its fetch interval (12 hours at
most). It looks it up by name, `retired/vX`, and only for a tag the tick asks about: the
running version, the pin, a version being tried or under suspicion, and each candidate once
that candidate has proved a valid release (so tags that anyone can publish cost no lookup).
It never goes through the `retired/` tags, so however many of them anyone publishes, they
cannot hide a real retirement or use up the tick's time. When the allowed signers hold a key, a retirement counts only if its tag
passes the same check as a release (below); without keys it counts unsigned. A lightweight
tag, a tag that does not sit on the commit of the release it names (what it points at is
peeled, so `git tag -s retired/v0.3.0 v0.3.0`, which nests it in the release's tag object,
counts), the retirement of a tag that is no valid release and a signature that does not
verify retire nothing and are logged (once a day) when the tick looks them up; a name that is
not a release or a candidate (`retired/latest`, `retired/v0.3.0-rc1`) is never looked up. The reason, taken
from the tag's subject, is only text for the log; nothing in a tag is run. A retired tag is
never a candidate. When the running version is retired the device goes to the best valid
candidate of its channel, even a lower one (there is no floor), and stays where it is when
there is none. A retirement that a device has fetched stays in its clone: it cannot be undone
from the remote, so a fix is a new release. The `release` workflow only runs on `v*.*.*`, so
a `retired/` tag does not start it.

**`pin`.** `pin = "vX.Y.Z"` (or `vX.Y.ZrcN`) keeps the device on that tag whatever the
channel: the target is the pin, even when it is lower than the running version, and no other
tag is a target while it is set (the choice is made again at every tick, also for a
version that was being tried or looked at a second time, which is dropped if it is no longer
a target). The tag must exist in the clone, be an ancestor of
`origin/main`, carry its own version and, when signatures are required, a good signature;
then it passes the same verification as any other tag, with the same differential verdict, so
a pin is never marked bad without the previous version passing the same check (and when the
environment is at fault, it is not blamed). A pin that was marked bad is not tried again until
someone removes `bad/<tag>`. A pin wins over a retirement: it is followed, and the state key
`pin_retired` holds the pinned tag while it is retired (empty otherwise), with a line in the
log, so that `doctor` can warn.

**Kind of checkout.** Decided from `git symbolic-ref -q HEAD` and `git merge-base`:

- HEAD on a release tag: managed.
- On a branch, clean, with HEAD contained in `origin/main`: adopted (a plain `git clone`
  leaves a user there); it moves to tags like a managed one.
- Commits of its own, a loose commit or a dirty tree: `dev` or `dirty`. It is not moved, but
  it is still reconciled, and `doctor` warns. The script never runs `reset`, `stash` or
  `clean`.

**Signed tags.** Release tags are signed with an SSH key, and the script trusts a tag
(a release, a candidate, the pin, a retirement) only when `git verify-tag` accepts it against
an allowed-signers file. Without it, whoever controls the maintainer's account could run code
on every device, and each holds the hub's API key. The check is hardened because
`verify-tag` chooses the verifier by the signature block it finds, not by `gpg.format`: a
tag signed with OpenPGP would be checked by `gpg` against the user's keyring and never reach
the allowed signers. A signature also covers only the tag object, not the ref it is
published under, so the `tag` line of the object must be exactly the tag's name (a signed
`v0.2.0` published as `v0.3.0`, or a signed `retired/v0.2.0` as `retired/v0.3.0`, is refused;
`__version__` already covers a release, this covers the rest). So the tag object must be
annotated and hold exactly one signature block, an SSH one (`-----BEGIN SSH SIGNATURE-----`; any other `-----BEGIN` line refuses the
tag), the check runs with `gpg.openpgp.program`, `gpg.x509.program` and `gpg.program` set to
`false`, `gpg.ssh.program=ssh-keygen`, and `GIT_CONFIG_GLOBAL=/dev/null
GIT_CONFIG_NOSYSTEM=1`, so the user's git configuration does not change the outcome. A tag
that does not verify (unsigned, lightweight, signed by another key or with another format) is
not a candidate, and the log says so (once a day).

The keys are `warpzone/data/allowed_signers` (the format of `ssh-keygen -Y`) **of the version
known to work** (`good_sha`), read with `git show` into a temporary file at every tick. They
travel with the version that is trusted, a release cannot change the keys that check it, and
no file next to the script can be missing or swapped. A release signed by a trusted key can
carry new keys, which apply from the moment a device is running it (rotation). The file ships
with comments only: no key is in the repository until the maintainer adds one. When the running
version has no such file, or no line in it that is not a comment, no signature is required
and the log says so; a file that cannot be read, or a missing `ssh-keygen` while there are
keys, rejects every tag (fail closed). `WZ_ALLOWED_SIGNERS` replaces the file for tests.

Once a version with keys has been the one known to work, signatures stay required for good:
the state key `signing` is `1`, the keys it had are kept in `allowed_signers.last` next to
the state and the key `signers_version` holds its `__version__`. The keys never go back: when
the version known to work is older than `signers_version` (a `pin`, or a retirement, took the
device down to an old release), its `allowed_signers`, absent or listing keys that a newer
release dropped, is ignored and the last keys are used, and the log says so; `.last` is only
replaced by the keys of a version that is as new or newer. With no copy to use, every tag is
refused. When the copy of the keys of the version known to work cannot be written (a full
disk) and `signers_version` is older than that version, no move down is accepted in that tick
(a retirement, a pin, a version being tried, a suspect), with a line in the log: the stale
version could let the next tick bring back a key that this version dropped. The next tick
tries the copy again. Trust is by first use: the first release that
carries keys is taken without a signature check (nothing on the device yet says whom to
trust), and so is any release before it; from then on a release has to be signed by keys the
device already trusts, which includes the release that rotates them.
[Releasing](/warpzone/development/releasing/) says how to sign.

**Never unattended.** `prune --apply`, `inbox --apply` and `settings capture`. The shadPS4
version is the user's choice and is only read. `ensure_flatpak_access` asks for a Syncthing
restart; unattended, that is a warning.

**What a rollback does not undo.** It reverts code only: not what `provision` wrote, nor the
data in `wz-settings`, `wz-play-state` and the saves.
