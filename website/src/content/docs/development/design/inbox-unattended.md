---
title: "Unattended inbox"
---

**Status: partly implemented.** The pure decisions are in the code (`inbox_unattended.py`: the
fingerprint, readiness, the state file and the changes made to it, space, news and the exit code),
and so are `[inbox] chdman_threads` (see [Resource limits](#resource-limits)), the lock heartbeat
(see [Locking](#locking): `inbox_lock.py` with `beat`, `mode`, `Heartbeat`, `judge` and
`still_mine`, used today by an attended run and by `warpzone scrape`), the run records (written by
every attended `--apply`, with their purge to 50) and the webhook (`inbox --notify-url`, sent by an
attended run when some item went to review, with `notify.py` shared with `prune`). The rest
(`--unattended` in `inbox` with its per-item "still mine" checks, the state file I/O and the walk,
and the UI) is design, not implemented. The `Unattended inbox` line of `doctor` is implemented
(see [Doctor](#doctor)); it shows "no unattended run has recorded anything" until a run writes the
state. This page is the contract for running `warpzone inbox --apply` on the hub by itself, from
a scheduled task, with nobody watching. The
pages it touches when it is implemented ([Inbox](/warpzone/development/design/inbox/),
[Configuration and command line](/warpzone/development/design/configuration/),
[Pruning](/warpzone/development/design/prune/) for the shared webhook,
[Doctor](/warpzone/development/design/doctor/), [Library UI](/warpzone/development/design/library-ui/),
[Hub, folders and ignores](/warpzone/development/design/hub-and-folders/),
[Code, testing and CI](/warpzone/development/design/code-and-testing/)) and the guides
([Adding new games](/warpzone/setup/inbox/), [The hub container](/warpzone/setup/hub-container/))
are updated in the same pull request as the code.

Today a person runs `inbox` on a computer that mounts the share, or in the hub container, reads
the report and runs it again with `--apply`. The goal here is that a game dropped in `inbox/`
from any computer (an SMB copy, no warpzone installed there) is filed, added to the catalog and
shown in the library UI with nothing run by hand.

## Decisions

Decided by the maintainer before the design, not reopened here:

1. **Trigger.** A scheduled task of the NAS (on a Synology, DSM's Task Scheduler) runs every 15
   minutes, as root because Docker on the NAS needs it, and starts
   `docker compose run --rm` of the hub container's inbox service. The image carries the inbox
   tools (that work is done apart from this page).
2. **Stable items only.** An item is processed only once its size has held for a few minutes.
3. **Empty is cheap.** With nothing to do the run exits at once: no tool starts, nothing is logged.
4. **The report is in the library UI.** What the last runs placed, the whitelist lines to add and
   the items for review are visible there.
5. **An exception to the rule for destructive commands**, documented like the unattended prune
   (see [The exception](#the-exception)).
6. **Scraping** (changed on 2026-11-13, when Skyscraper entered the hub image: see
   [Scraping: In the hub image](/warpzone/development/design/scrape/#in-the-hub-image)). The
   unattended run scrapes the games it placed, as an attended `--apply` does; a scrape that fails
   is a warning and never changes a placement or the exit code (see [Scraping](#scraping)).

Decided by the maintainer on 2026-11-13, on the first draft of this page:

1. **The lock gets a heartbeat**, and an unattended run judges a lock stale when its modification
   time has not moved across two readings at least 10 minutes apart (see [Locking](#locking)). A
   fixed host name for the service was not chosen.
2. **Stability is judged across ticks:** the same fingerprint for 5 minutes, remembered in the
   state (see [The stable-size rule](#the-stable-size-rule)), not a second look within one run.
3. **An item is held after two runs died on it**, not after the first (a power cut alone should
   not hold an item).
4. **A newer PS4 update replaces the library's one in unattended runs too**, exactly as an
   attended `--apply` does, with the old package filed in `_done`.
5. **A loose `.cue` or `.gdi` at the root of `inbox/`**, with the files it names, is held, not
   processed (see [Items of several files](#items-of-several-files)).
6. **The state and the run records live in `catalog/inbox/`**, not in a hidden folder of `inbox/`.
7. **Exit code `1` only for news:** a newly held item, a new item for review or a new run-level
   error. Later runs that meet the same held items or the same error exit `0`; the state stays
   visible in doctor and the UI. DSM's email on abnormal termination must not repeat every 15
   minutes (see [Logging and exit codes](#logging-and-exit-codes)).
8. **`[inbox] chdman_threads`, `os.nice(10)` and `cpus` in the compose service**; no I/O priority
   (`ionice`) for now.
9. **No check on the host** before Docker starts: the empty inbox is detected by warpzone in the
   container.
10. **An empty run writes the heartbeat** (`checked` in the state) at most once an hour, so doctor
    and the UI can tell a stopped task from an empty inbox.

Decided by the maintainer on 2026-11-13, when the pure decisions were written; each changes the
sections it is named in:

1. **Each state entry keeps `first`**, when the item was first seen, besides `seen`, which is
   reset on every change of the fingerprint. "Waiting since" and doctor's "waiting more than two
   hours" use `first` (see [The memory](#the-memory)).
2. **A stored error keeps the `step` where it failed.** Only a run that gets past that step clears
   it; a run that finds the inbox empty can only clear the errors of step 1 (see
   [The memory](#the-memory)).
3. **The state is updated by read-modify-write.** A change is a pure function from the state to
   the state, applied again to what is read again when the write finds the file changed (see
   [The memory](#the-memory)).
4. **An original that cannot be moved to `_done` after its games were placed is held** and counts
   as news (see [The memory](#the-memory)).
5. **`MIN_FREE_GIB` lives in `inbox_core.py`**, and doctor and the unattended run both import it
   from there (see [Resource limits](#resource-limits)).
6. **The mount check** of step 1 asks for `library/.stfolder` and `library/roms` (see
   [The command](#the-command)).

**Done when** the maintainer drops a game in `inbox/` from a computer that has no warpzone, and
within two ticks of the timer it is in the catalog and on the library UI's inbox view, with
nothing run by hand.

## The exception

[Writing commands](/warpzone/development/design/configuration/#writing-commands) says that a
destructive command acts only when a person runs it with `--apply`, with one exception, the
[unattended prune](/warpzone/development/design/prune/#unattended-runs). This is the second, and
`configuration.md` names both. The case for it:

- **What it can do is bounded.** `inbox` only adds to the library and never overwrites a file
  there (`.part`, read back, re-checked right before the rename). What it moves are the originals,
  inside `inbox/`, to `_done/<date>/` or `_revisar/`. What it deletes are the folders of `_done`
  older than 7 days, as every `--apply` run does today.
- **The report a person reads first is replaced by three things:** the run record in the library
  UI ([The run record](#the-run-record-and-the-library-ui)), the 7 days the originals stay in
  `_done`, and the rule that a run never acts on an item that is still being written
  ([Readiness](#readiness)).
- **The one replacement is kept.** The one place where `inbox` replaces something in the
  library, a PS4 update newer than the one there (see
  [PS4 packages](/warpzone/development/design/inbox/#ps4-packages)), is done unattended exactly as
  in an attended `--apply`: the library holds a complete update at every moment, the replaced
  version is named in the record, and the package of the old update is still in `_done` from the
  run that placed it (for 7 days after that run).
- **What stays for a person.** Taking over a stale lock of an older warpzone (one with no
  heartbeat, see [Locking](#locking)) stays with `--force-lock`.

Every other rule of the [Inbox](/warpzone/development/design/inbox/) contract holds unchanged:
content identification, unsafe names and archives, the tool time limit, missing tools sending
only their item to review, whitelists never edited, the gamelist edits with their backup.

## How it runs

### What the user sets up once

1. **The inbox service.** The `warpzone-inbox` service of
   [Running it in the container](/warpzone/setup/inbox/#running-it-in-the-container), with the
   additions of [Resource limits](#resource-limits) and [Secrets and data](#secrets-and-data):
   `cpus`, the `keys` volume, and the work folder on a volume.
2. **The config.** `[inbox]` in the container's config with `root`, `dats_dir`, `work_dir` (a
   path on a mounted volume; required for `--unattended`), and the optional paths of the tools,
   keys and databases as the container sees them.
3. **A trial by hand**, from the folder with `compose.yaml`:

   ```bash
   docker compose run --rm -T warpzone-inbox doctor
   docker compose run --rm -T warpzone-inbox inbox                          # report only
   docker compose run --rm -T warpzone-inbox inbox --apply --unattended     # what the task runs
   ```

   With an empty inbox the last line prints nothing and exits `0`.
4. **The scheduled task.** On a Synology: Control Panel, Task Scheduler, Create, Scheduled Task,
   User-defined script. User `root`; daily, first run at 00:00, every 15 minutes, last run at
   23:45; the script:

   ```sh
   cd /path/to/the/compose/folder && /usr/local/bin/docker compose run --rm -T warpzone-inbox inbox --apply --unattended
   ```

   The full path of `docker` is needed because the task's `PATH` is short (`which docker` over
   SSH shows it). `-T` gives the container no terminal, which a scheduled task does not have.
   "Save output results" in the Task Scheduler's settings keeps each run's output in a folder of
   the user's choice (see [Logging](#logging-and-exit-codes)); its email "when the script
   terminates abnormally" arrives once per new problem, since a run exits non-zero only for news
   (see [Logging and exit codes](#logging-and-exit-codes)); the [webhook](#notifications) also
   reports what was placed. Other NAS
   systems use their own scheduler or root's crontab with the same line.

Root runs only `docker`. warpzone itself runs in the container as the uid of the service's
`user:` (the one that owns the games share), with no capabilities and a read-only root, as the
server does. A long-running service that watches the inbox was not chosen: the scheduler needs
no daemon, a container that is not running holds no memory between ticks, and a new image is
used at the next tick without restarting anything.

### The command

`warpzone inbox --apply --unattended [--notify-url URL]`. `--unattended` requires `--apply` and
refuses `--force-lock` (usage error, exit 2). A run goes through these steps, and stops at the
first that says so:

1. **Load the config** and find the hub root (`[inbox] root` or `--root`). `inbox/`,
   `library/.stfolder` and `library/roms` must all exist (as for `serve`, an unmounted share never
   looks like an empty library, and a share that is mounted but is not the hub root does not
   pass); otherwise it is a run-level error (see [The memory](#the-memory)).
2. **List the items** (`scan` and `scan_unverified`: directory listings only) and read the state
   file. With no item and no state entry to drop, the run writes the heartbeat if it is due
   (`checked` older than one hour) and exits `0`, printing nothing. No DAT, tool, lock or work
   folder is touched.
3. **Busy.** If `inbox/.warpzone.lock` exists and is not stale by the rules of
   [Locking](#locking), the run prints one line naming the holder (`busy: the unattended inbox on
   the hub has been working since 10:15`, or `a run from <host>`), records what it saw of the lock
   in `lock_seen` and exits `0`.
4. **Judge readiness** of each item (`inbox_unattended.decide`, pure; see [Readiness](#readiness)
   and [The memory](#the-memory)): waiting, ready or held. With no ready item the run writes the
   state if it changed and exits (`1` when an item was held for the first time in this run,
   else `0`).
5. **Check what the work needs**, as an attended run does at its start (DATs, the handlers'
   `validate`, configured paths), plus the work folder: set, and not on a memory filesystem.
   A failure is a run-level error.
6. **Take the lock** with a heartbeat. Lower its own CPU priority (`os.nice(10)`; the tools
   inherit it). Remove work folders a killed run left behind ([Resource limits](#resource-limits)).
7. **Process the ready items one at a time**, in scan order: check that the lock is still this
   run's, check free space, record the attempt, `process_item`, file the original away, record the
   outcome. A run started by an earlier tick takes only the items that were ready at its step 4;
   items that become ready meanwhile wait for the next tick.
8. **Finish** as an attended run does (purge `_done` folders older than 7 days, update the
   catalog for the systems it placed something in, scrape the games it placed: see
   [Scraping](#scraping)), then write the run record and the state, send the webhook, release the
   lock and exit.

An attended `--apply` (from a computer or in the container) is unchanged except for three things:
it writes a run record too, so the UI shows its result; its lock has the heartbeat; and it reads
nothing of the state file. It processes every item as today, held ones included, which is how a
person clears a hold (the next unattended tick drops the entries of items that are gone).

## Readiness

### The stable-size rule

A single run sees one moment, so stability is judged across ticks. For each item the run computes
a fingerprint (`inbox_unattended.fingerprint`, pure, over what a walk returns): for the item and
every entry under it, without following symbolic links and including hidden files (a copier's
temporary file inside a folder counts), the line `<kind> <size> <mtime_ns> <path relative to the
item>`; the lines are sorted, joined with NUL bytes and hashed with SHA-256. Only `stat` is called,
no file is read. The state keeps, per item, the fingerprint, when it was first seen with it
(`seen`), when the item itself was first seen (`first`), its file count and bytes.

An item is ready when the fingerprint now equals the recorded one and that one was first seen at
least `STABLE_SECONDS` (300, five minutes) ago by the judging run's clock. Any change (a byte
written, a file added, a size or a modification time that moved) records the new fingerprint with
the time now as `seen` and starts the wait again; `first` is kept, so the time an item has been
waiting is not lost by a copy that keeps writing. A clock that goes back (`seen` in the future of
the clock now) never makes an item ready: the run records `seen` again with the time now. With a tick of 15 minutes a new item is processed at the
second tick that sees it, so between 15 and 30 minutes after the copy ends.

Size alone is not enough: a Windows copy can set a file to its full size when it starts and fill
it afterwards, so the size holds while the content does not. Every write moves the modification time
on the hub's filesystem, which the fingerprint includes. Comparing two of the hub's own readings,
never a modification time against the clock, keeps a computer with a wrong clock, or a copier that
sets the source's old time at the end, from making an item look ready or never ready. The
30-second rule of an attended run (`SETTLE_SECONDS`) is not applied in an unattended run: the
fingerprint replaces it.

### Partial copies

A copy that stops and does not resume (a laptop that slept, a dropped network) leaves an item that
is stable but incomplete. It is processed: a file cut short matches no DAT, or fails its header
check or its archive listing, so the item goes to `_revisar/` with its reason and nothing is
placed. If the copier resumes later it fails, since the item has moved; the user copies again. The
guide recommends, for large copies over an unreliable link, copying into a folder whose name starts
with `_` (never an item) and moving it into `inbox/` when the copy is done: a move within the share
is instant and the item is then judged as new.

### Items of several files

A folder or an archive is one item, and its fingerprint covers every file in it: while a folder is
still receiving files, it keeps changing. A copy that pauses longer than five minutes between two
files of one folder is a partial copy as above.

A disc dropped as loose files at the top of `inbox/` (a `.cue` or `.gdi` next to its tracks) is
several items today, and the cue of such a set is followed only when its tracks are in the same
item. A person sees that in the report; an unattended run must not place half of it. So in an
unattended run a top-level `.cue` or `.gdi`, and every top-level file it names, is held with the
reason "a disc dropped as loose files: put the sheet and its tracks in one folder"
(`inbox_unattended.loose_sheets`, pure, over the top-level names and the sheets' references). The
user moves them into a folder, which is a new item. A loose `.bin` that no sheet names keeps
today's behaviour.

## The memory

The state of the unattended runs lives in `catalog/inbox/state.json` on the hub (next to the
catalog index: outside every Syncthing folder, covered by the hub's snapshots, readable by
`serve`). `warpzone catalog` leaves `catalog/inbox/` alone.

```json
{"v": 1, "checked": "2026-11-13T10:15:02Z",
 "items": {"Example Game (Europe).zip": {"fingerprint": "sha256:...", "files": 1, "bytes": 52428800,
                                          "first": "2026-11-13T10:00:01Z",
                                          "seen": "2026-11-13T10:00:01Z", "attempts": 0,
                                          "held": null}},
 "lock_seen": null,
 "error": null}
```

A held item is `"held": {"reason": "...", "at": "..."}`; an error is `{"message", "step",
"first", "count", "config"}`; `lock_seen` is `{"mtime_ns", "at"}`.

- **Keys** are the item names as the report shows them (`<name>`, or
  `_unverified/<system>/<name>`). Entries of names no longer in the inbox are dropped.
- **`first` and `seen`.** `first` is when the item was first seen under this name, kept through
  changes of the fingerprint; it restarts only for an item whose hold a change cleared (that item
  is new again). `seen` is when the current fingerprint was first seen and is reset on every
  change. Doctor's and the UI's "waiting since" read `first`.
- **Attempts.** Right before an item is processed, `attempts` goes up by one and the state is
  written. When the item has been filed away (to `_done` or `_revisar`) its entry is removed. An
  entry found with `attempts` above zero and the same fingerprint therefore belongs to a run that
  died while working on it (the container killed for memory, a power cut), since this run holds
  the lock. The item is tried once more; at the second death it is held with "the run stopped
  twice while working on this item; it may need more memory than the container's limit
  (`mem_limit`), or run inbox on a computer".
- **Held items** are not processed again while their fingerprint stays the same: `held` holds the
  reason and the time. An item is held when the run died twice on it, when its original could not
  be moved after its games were placed ("could not move the original: ..."; the games are in the
  library and the original is still in the inbox, so a retry would only report them as already
  there and fail to move it again), and for loose disc files. A hold of the last kind is news
  like any other (see [Logging and exit codes](#logging-and-exit-codes)). A changed fingerprint (the user replaced, renamed or touched it) clears the hold and the
  item waits as new; an attended `--apply` processes it whatever the state says.
- **Items for review are not held:** they are moved to `_revisar/` with their reason file, as
  today, so they leave the inbox and are never retried.
- **Run-level errors** (the config, the DATs, a missing `library/.stfolder`, a work folder on
  memory, a stale lock of an older warpzone) are kept in `error`: the message, the `step` of
  [The command](#the-command) where it failed, whether it is a configuration error (`config`,
  which decides exit `2` or `1`), when it first happened and how many runs met it since. The error
  stays until a run gets past its step: a run reaches a step when it starts it, and clears the
  errors of the steps before it. A run that finds the inbox empty stops at step 2 and so clears
  only the errors of step 1; a run with only waiting items stops at step 4 and leaves an error of
  step 5 or 6 where it is. Meeting the same message again keeps the step and raises the count.
  The first run that meets an error exits `2` (`1` for an unexpected failure outside an item) and
  sends the webhook; the runs after it that meet the same message exit `0` and only raise the
  count (see [Logging and exit codes](#logging-and-exit-codes)).
- **Writes: read, modify, write.** A change to the state is a pure function from a state to a
  state (`inbox_unattended.Change`; `update(state, *changes)` applies them in order). The writer
  reads the file, applies the changes, and writes with `safefile.atomic_write` against the stamp of
  the file as it was read; a file that changed in between is read again, **the same changes are
  applied to what was read again** and the write is retried (three times, then a warning), as the
  catalog files are. So a change never holds a state it read earlier, only what it is given. The
  changes are `with_checked`, `with_lock_seen`, `with_judged` (records the judged entries and drops
  those of names no longer in the inbox), `with_attempt`, `with_item_filed`, `with_held`,
  `with_error` and `with_error_cleared`. A state file that cannot be parsed is treated as empty
  with a warning and replaced; a damaged entry, error or lock note is dropped with a warning and
  the rest kept: the worst case is that items wait one more tick.

`inbox_unattended.render_state` and `parse_state` are the pure ends; `decide` takes an entry (or
none), what the walk shows now (`Observed`) and the time, and returns the verdict and the entry to
record. A loose disc file reaches it as `hold_reason`, and holds the item at the moment it would
be ready.

## Locking

The lock is the one of today (`inbox/.warpzone.lock`, created with `O_CREAT | O_EXCL`), so an
unattended run and a person's run from a computer on the mounted share never work at once: each
finds the other's lock. What a container changes is how a dead holder is recognised. Each
`docker compose run` gets a new host name (the container's id) and a pid in a namespace of its
own, so the "pid not alive on the same host" rule never applies, and a lock left by a container
killed for memory would block every tick for 12 hours.

- **Heartbeat.** The lock's JSON gains `"beat": 60` and `"mode": "unattended"` or `"attended"`.
  While it holds the lock, every run (attended too) touches the file's modification time every 60
  seconds from a thread (`inbox_lock.Heartbeat`), and only while the file still holds this run's
  host, pid and start time.
- **Stale by silence, judged on one clock.** An unattended run that finds a lock with `beat` in the
  way records its modification time and the time it looked in `lock_seen` and exits as busy. A
  later run that finds the same lock with the same modification time, at least 10 minutes later by
  its own clock, judges it stale ("no heartbeat for 10 minutes"). Comparing two readings of the
  file, never its time against the clock, keeps a computer whose clock is off from looking dead or
  alive. The unattended run then takes the lock over itself, with the rename-and-verify
  takeover that `--force-lock` uses (see Inbox), and says so in its record.
- **Older locks.** A lock without `beat` (an older warpzone) keeps today's rules (12 hours, or a
  pid of the same host), and an unattended run never takes it over: it is a run-level error that
  names it, and a person runs `--force-lock`.
- **Attended runs** look once, as today: a lock with `beat` whose modification time is more than
  10 minutes old by this machine's clock is reported as stale ("no heartbeat since 10:15"), and
  `--force-lock` takes it over; a person sees the time and decides, so the clock of one machine is
  enough there. The message says who holds it (`the unattended inbox on the hub`, from `mode`).
- **Still mine.** Before each item and before each move of an original, the run reads the lock;
  if it no longer holds this run's identity it stops at once (runtime error, exit 1) and leaves the
  item where it is. A rerun reports what it had placed as "already in the library".
- `warpzone scrape`, which takes the same lock, gets the heartbeat with it.

Implemented in `inbox_lock.py`: `hold(inbox, force=, now=, out=, mode="attended" | "unattended",
previous=)` yields a handle with `still_mine()`. `judge(reading, here, alive, unattended=,
previous=)` returns `live`, `stale`, `unknown` (an unattended run with one reading, or two too close
together, or a clock gone back: busy) or `legacy` (stale by the old rules, which an unattended run
never takes). A lock held by someone else, including one created between the look and taking it,
raises `LockBusy`, which carries the `LockReading` (the lock, its modification time and when it was
looked at) for `lock_seen`, so the unattended run can exit `0` for it; a stale lock the run may not
take (no `--force-lock`, or a legacy one unattended) raises `LockStale`. Both are `InboxError`s, so
an attended run still exits `2`. An unattended `hold` cannot be given `force`. The "Still mine" check
before each item and each move is not wired yet: `Held.still_mine()` exists, and the calls in
`inbox` come with `--unattended`, as the rest of the run.

Two ticks of the scheduler never overlap as two workers: the second container finds the first's
lock and exits as busy, costing one container start.

## Resource limits

- **One item at a time**, as today; each tool call within the item runs alone.
- **CPU.** The compose service sets `cpus: "2"` (or fewer than the NAS's cores), so SMB and
  Syncthing keep the rest. The run lowers its own priority with `os.nice(10)` and every tool
  inherits it. The standard library has no call for the I/O priority, and the I/O schedulers of
  NAS kernels vary, so none is set; if the share is slow while a conversion runs, a later change
  can start the tools under `ionice -c 3`.
- **chdman's threads.** chdman counts the host's cores, not the container's CPU limit, and each
  thread holds its own compressor. The optional key `[inbox] chdman_threads` (a whole number, at
  least 1; implemented) passes `--numprocessors N` to `chdman createcd` and `createdvd`
  (`inbox.make_chd`; unset passes nothing and chdman counts the cores, `verify` and `info` get
  nothing); the container guide sets it to the service's `cpus`.
- **Memory.** `mem_limit: 2g`, as the guide has it. A run killed for memory exits `137` in the
  task's output; [The memory](#the-memory) turns a second death on the same item into a hold with
  the reason, so the timer never loops on it.
- **Temporary space.** `work_dir` must be set and on a mounted volume: `--unattended` refuses a
  work folder on `tmpfs` or `ramfs` (`inbox_unattended.memory_fs`, pure, over
  `/proc/self/mountinfo`; elsewhere the check is skipped), since the container's `/tmp` is memory
  and a disc image would count against `mem_limit`. The per-item free space checks of the inbox
  (an archive's members plus 5%, a GCZ image, an NSZ's NSP, a PS4 or Vita package) are unchanged.
  At the start of a run that holds the lock, the `warpzone-inbox-*` folders in `work_dir` older
  than 24 hours are deleted (a killed run's leftovers; `stale_work_dirs`, pure, never follows a
  link), since they may hold a Vita licence.
- **Space on the hub.** Before each item the run compares the free space of the filesystem that
  holds `library/` with twice the item's bytes plus 20 GiB (the doctor's threshold for the hub,
  `MIN_FREE_GIB`, which lives in `inbox_core.py` and is imported from there by doctor and by the
  unattended run). Exactly enough fits; the shortfall is rounded up to whole GiB. An item that does not fit waits, it does not go to review, and its state says
  "waiting for space: N GiB more needed"; doctor shows it (`space_verdict`, pure).

## Safety rules that still hold

- Never overwrite: every placement goes through `.part`, read back, the re-check right before the
  rename, and "already in the library" or a conflict for a name that exists.
- Never edit a whitelist: the record lists the lines, the UI turns games on through its own guarded
  write.
- Re-check right before acting: the lock is checked before each item and each move; the
  fingerprint is taken again right before the item is processed, and an item that changed since it
  was judged ready waits again.
- Never follow symbolic links in the inbox, never extract unsafe archives, never write under a name
  a DAT gave that is not a plain file name.
- The `_done` purge deletes only date-named folders older than 7 days and does not follow links.
- Report-only runs stay read-only: they write no run record and no state.

## The run record and the library UI

### Files on the hub

Every `--apply` run that processed at least one item, attended or not, writes
`catalog/inbox/runs/<started>.json` (UTC, `20261113T101500Z`, with `-2`, `-3` for runs that
started in the same second), mode 0644, atomically. The newest 50 are kept; the writer deletes the
older ones (`select_records_purge`, pure). Paths in it are relative to the hub root; no absolute
path, host name, key or URL is written.

```json
{"v": 1, "id": "20261113T101500Z", "mode": "unattended",
 "started": "2026-11-13T10:15:00Z", "finished": "2026-11-13T10:21:40Z", "exit": 1,
 "items": [
   {"item": "Example Game (Europe).zip", "outcome": "done",
    "moved_to": "inbox/_done/2026-11-13/Example Game (Europe).zip",
    "games": [{"system": "psx", "entry": "Example Game (Europe).chd", "outcome": "placed",
               "path": "library/roms/psx/Example Game (Europe).chd",
               "whitelist": ["!/roms/psx/Example Game (Europe).chd"],
               "ra": ["RA: hash not linked"], "notes": []}],
    "reasons": [], "notes": []},
   {"item": "some dump.7z", "outcome": "review",
    "moved_to": "inbox/_revisar/some dump.7z", "games": [],
    "reasons": ["no DAT match: track01.bin"], "notes": []}],
 "counts": {"placed": 1, "already": 0, "review": 1, "held": 0, "waiting": 2},
 "whitelist": ["!/roms/psx/Example Game (Europe).chd"],
 "scrape": {"state": "done", "systems": ["psx"]},
 "purged": ["2026-11-05"], "warnings": []}
```

`outcome` of an item is `done`, `review` or `held`; of a game, the report's `placed`, `already`,
`exists` (a different file with that name is in the library; counted in `already`) or `review`
(its `path` is `null`). `held` is an item whose original could not be moved after its games were
placed: `moved_to` is `null`, its games are `placed`, and its `reasons` say "could not move the
original: ...". An attended run records it the same way and keeps sending it to review as today
(the report says `REVIEW`, the exit code counts it). `counts.waiting` is the items left in the inbox
untouched (an attended run's items still being copied; an unattended run's waiting ones), and
those are not in `items`. `scrape.state` is `done`, `not-finished` (with the systems to give to `warpzone scrape
--system`), `skipped` (`--no-scrape`, or no Skyscraper found) or absent when nothing scrapable was
placed; a `scrape` that is not `done` also has a short `message`. `reasons` and `notes` are the
report's text, with what the report already redacts (a zRIF, nsz's key lines) redacted the same
way, and every text of the record goes once more through `clean_text` (pure): each known secret
(`RA_API_KEY`, the webhook URL) and any base64 token of 128 characters or more is replaced by
`***`, the hub root is cut off the paths under it, and any other absolute path becomes `<path>`.
`build_record` is pure and is built from the same `ItemResult` values the printed report comes
from, so the two never disagree; `render_record` writes its JSON. The id is chosen where the file
is written (`inbox_unattended_io.write_record`), which tries `-2`, `-3` when a record of that
second exists, and then the older records are deleted. A record that cannot be written is a warning
and never changes the run.

### API

`serve` reads these files like the other hub files it serves (kept in memory, reloaded when size
or mtime changed, checked at most every 5 seconds) and writes none of them. Two read-only routes
join the [endpoints](/warpzone/development/design/library-ui/#endpoints):

| Method and path | Answers |
|---|---|
| `GET /api/v1/inbox` | the state of the unattended runs and the newest records |
| `GET /api/v1/inbox/runs/{id}` | one record in full, or `404 not-found` |

```json
{"checked": "2026-11-13T10:30:01Z", "busy": null, "error": null,
 "waiting": [{"item": "Other Game.zip", "since": "2026-11-13T10:30:01Z", "bytes": 734003200,
              "why": "settling"}],
 "held": [{"item": "Game.cue", "since": "2026-11-13T09:45:00Z",
           "reason": "a disc dropped as loose files: put the sheet and its tracks in one folder"}],
 "runs": [{"id": "20261113T101500Z", "mode": "unattended", "finished": "2026-11-13T10:21:40Z",
           "placed": 1, "already": 0, "review": 1, "exit": 1}],
 "last": {"...": "the newest record in full, each placed game with its catalog id"}}
```

`busy` is `{"since", "mode"}` while `inbox/.warpzone.lock` exists, else `null`. `why` is
`settling` or `space`. `runs` lists the newest 10 records, newest first. In `last` (and in a record
from the second route) each game gets `id`, the catalog game whose primary part is at `path`
(`null` when the catalog has none yet, for example before its update finished). A missing or
damaged state or record is the same as none (`checked` `null`, empty lists), never an error.
`library_api.inbox_response` is pure over the parsed files and the catalog.

### Page

An "Inbox" view on the page, reached from the header, where a dot shows when the newest record is
newer than the last one this browser opened (kept in `localStorage`). It shows the time of the last
check and any error; the newest run's placed games, each linking to its game page, where the usual
controls turn it on for a console; the whitelist lines as text; the items for review with their
reasons and the place of the original (`inbox/_revisar/...`); the held and waiting items; and the
older runs, opened one at a time through the second route. The view only reads.

## Notifications

`inbox --apply` takes `--notify-url URL` (http or https), attended or not, and POSTs one JSON
object per run that placed a game or has news in the sense of
[Logging and exit codes](#logging-and-exit-codes) (a held item or an error that is already in the
state is not sent again, so a broken config does not post every 15 minutes):

```json
{"source": "inbox", "mode": "unattended", "run": "20261113T101500Z",
 "placed": 1, "already": 0, "review": 1, "held": 0,
 "games": ["psx/Example Game (Europe).chd"], "review_items": ["some dump.7z"], "error": null}
```

The lists hold at most 20 names (`games` the placed games, `review_items` the items for review or
held). It is the prune webhook's code (`prune.notify`, moved to `notify.py` and shared): meant for
a Home Assistant webhook, the URL never printed, a failure a warning and never a failure of the
run, no API key or path outside the hub in the payload. The payload is built by `notify_payload`
from the record and the decision to send by `should_notify` (placed games or `news` for an
unattended run), both pure. An empty, busy or waiting-only run sends nothing.

Decided by the maintainer on 2026-11-13: an attended `--apply` sends the webhook **only when some
item went to review** (an item in `_revisar/`, or one whose original stayed in the inbox, which an
attended run still calls review), not for placed games alone; and an error raised before the run
starts (the config, the layout, the DATs) sends nothing, since no record exists for it. The
unattended rule above is unchanged.

## Secrets and data

- **RetroAchievements key.** `RA_API_KEY` from `warpzone.env` (`env_file:`), as the server has it;
  never on the task's command line, never in the record.
- **Switch keys and the zRIF table.** A `keys` folder next to `compose.yaml`, mounted read-only at
  `/keys`, outside the hub root (so outside every Syncthing folder and every share a console
  reads): `switch_keys = "/keys/prod.keys"`, `vita_zrif = "/keys/PSV_GAMES.tsv"`. The files are
  readable only by the service's uid (mode 0600 or 0640 with the share's group). warpzone never
  opens `prod.keys` (it is passed to `nsz`); a zRIF is on Vita3K's command line for under a second,
  where the NAS's process list can see it, as on a computer.
- **DATs and databases.** The `dats` folder, read-only at `/dats`: libretro-database's `metadat`,
  a clone of titledb (`titledb_dir`) and the arcade databases (`mame_dat`, `fbneo_dat`,
  `mame2003plus_dat`). The parsed caches go to `XDG_CACHE_HOME`, set to a folder of the work
  volume. warpzone never downloads them; updating them is a `git pull` by the user.
- **The webhook URL** is on the task's command line, which only the NAS's administrators see. It
  is never printed, logged or written to a record or a payload (`Options.notify_url` is kept out
  of the options' `repr`, and `clean_text` redacts it from every text of a record), and an error of
  the send names the status, not the address.
- **Skyscraper's folder.** The `skyscraper` folder next to `compose.yaml`, read-write at
  `/skyscraper` (see [Scraping in the container](/warpzone/setup/hub-container/#scraping-in-the-container)):
  its `config.ini` may hold the ScreenScraper password, which warpzone never reads, prints or
  passes, and its `cache/` is on disk. Not in the record, not in the webhook.

## Logging and exit codes

The run prints the same lines as an attended run (one block per item, the whitelist lines, the
summary) plus one line per newly seen or held item, to the task's output. An empty run prints
nothing; a busy run one line. The run records are the durable log, and `_revisar/<item>.reason.txt`
keeps each review's reason as today. warpzone writes no other log file.

Unlike the unattended prune, whose exit code tells the state on every tick, an unattended inbox
run exits non-zero only when it has news, so a scheduler that mails on an abnormal end mails once
per problem. What is still pending (held items, an error that persists) stays in the state, and
doctor and the UI show it on every look.

`news` (`inbox_unattended.news`, pure; `exit_code` turns its result into the exit code) compares the state as the run read it at its start with the
state it writes, plus the results of this run. News is any of:

- **a new item for review**: an item this run sent to `_revisar/` (it leaves the inbox, so each one
  is news once);
- **a newly held item**: an entry whose `held` is set in the state written and was not set, or
  belonged to another fingerprint, in the state read (an item whose hold was cleared by a change
  and is held again is news again). An original that could not be moved after a successful place
  is one of these;
- **a new run-level error**: an `error` in the state written whose message differs from the one in
  the state read, or with none there. A run that meets the same message raises `count` and has no
  news. A run that gets past the failed step clears `error`, so the same error coming back later
  is news again.

The codes:

- `0`: no news. Nothing to do, busy, only waiting items, every processed item placed or already in
  the library, and runs that meet only held items or an error already in the state.
- `1`: news of the first two kinds, or a new error that is not about the configuration (the lock
  taken away, a failure outside an item).
- `2`: a new configuration or run-level error (steps 1, 5 and 6, the lock of an older warpzone),
  and every usage error of the command line (`--unattended` without `--apply`), which is checked
  before anything is read.

An error met before the hub root is known (a config that does not load, no `[inbox] root`) cannot
be compared with a state and exits `2` on every run; `doctor`, run once after the setup, finds it
first. A run with news of several kinds exits `2` if one of them is a configuration error, else `1`.

## Doctor

With an `[inbox]` table and a readable hub root, `doctor` gets one line, `Unattended inbox`, in the
inbox group, on a computer as well as in the container (the state is on the share).
`doctor_inbox.judge_unattended_inbox` (pure, re-exported by `doctor`; over the parsed state, the
newest record, when the lock was taken and the clock) returns one verdict. When several of the
cases below hold, they share the line: the worst status, and the text and fix of each case that is
not `[ok]`. Implemented.

- `[--]` "no unattended run has recorded anything": no state file. A setup without the task. The
  fix is to schedule `warpzone inbox --apply --unattended` every 15 minutes on the hub (see
  [What the user sets up once](#what-the-user-sets-up-once)).
- `[!!]` a run-level error in the state: its message, since when and how many runs met it, with
  the fix that its kind implies: for a configuration error (`config`), to fix what the message
  says; for a runtime one, to read the task's output on the hub; both say that the next run that
  gets past its step clears it.
- `[! ]` the last check is more than two hours old: "the unattended inbox last ran N h ago; is the
  scheduled task enabled?" (the heartbeat writes at least hourly). A state with no `checked` at
  all is the same warning, worded as "records no check".
- `[! ]` held items: each with its reason, and the fix "look at it, run `warpzone inbox` to see the
  report, then `warpzone inbox --apply` by hand, or replace the item to retry".
- `[! ]` an item waiting for more than two hours since it was first seen (`first`, not `seen`,
  which a copy that keeps writing resets: a copy that keeps writing, a file a program keeps open,
  or no space). Held items are not counted here again.
- `[! ]` a lock held for more than six hours.
- `[ok]` otherwise: "last check N min ago; last run <date>: placed X, for review Y" (from the
  newest record; `review` there counts the items sent to review and the held ones). With no record
  yet, "no run has been recorded yet". A state file that cannot be parsed reads as an empty one and
  gets the "records no check" warning.

A line names at most three items and counts the rest.

`health` does not read it: it judges a console's version, not the hub.

## Scraping

Skyscraper is in the hub image since 2026-11-13, so the unattended run scrapes what it places,
with the code of an attended `--apply` (`inbox_scrape.after_apply`, step 8 of
[The command](#the-command)): after the catalog update, for exactly the games this run placed
(`scrape.is_placed`), system by system, inside the lock it already holds. The lock's heartbeat
runs meanwhile, so a scrape of minutes (a game takes about 10 seconds) is a live lock to the ticks
that find it, which exit `0` as busy. `--no-scrape` is accepted with `--unattended` (the task can
pass it); nothing implies it.

- **Empty stays cheap.** A run with no ready item, and a run that placed nothing scrapable (a
  Switch update, a system in `UNSCRAPED`, only `already` games), starts no Skyscraper.
- **A failure is a warning, never news.** The daily limit, a closed, busy or unreachable
  ScreenScraper, a Skyscraper that fails or hits the time limit, a missing `/skyscraper` volume,
  or a catalog that lacks the placed games: each is a warning line in the task's output, as in an
  attended run, and `scrape.state` of the record becomes `not-finished` with the systems to rerun.
  It never changes an item's outcome, never sends an item to review, never goes into the state's
  `error` and never counts in `news`, so the exit code is what [Logging and exit
  codes](#logging-and-exit-codes) says without it. This is the rule for every scrape failure, so
  none of them is "news once": a quota or a closed ScreenScraper would otherwise be news on the
  first tick of every day or hour it lasts, and DSM's mail on abnormal termination must not repeat
  for something a person cannot fix by looking at one run. The webhook is unchanged for the same
  reason (it reports the placed games; the record holds the scrape note).
- **Where it shows.** The run record's `scrape` note, which the UI's run list prints on the run
  ("scrape not finished: psx"), and the task's output. A standing cause (no volume, no Skyscraper,
  a configuration that does not load) is what `doctor` already reports on the `Skyscraper` line.
- **What a person does.** `warpzone scrape` (or `docker compose run --rm warpzone-inbox scrape`)
  scrapes everything in the library that lacks an entry or media, so the games a limited or failed
  run left are picked up by it, and by nothing else: the next ticks scrape only the games they
  place themselves, and never retry an earlier run's. A second scheduled task that runs `scrape`
  once a day, for the day's quota, is possible with the same container service (it takes the same
  lock) and is not part of this design.
- **Resources.** Skyscraper runs under the run's `os.nice(10)` like the other tools, one call at
  a time, within `mem_limit` (a gather holds little) and the `tool_timeout_minutes` of `[inbox]`.
  Its staged folders and logs go to `work_dir` and are removed with the run's other leftovers.

## Code and testing

- `inbox_unattended.py` (pure). Implemented: `fingerprint` and `observe`, `decide`,
  `loose_sheets`, `render_state` and `parse_state`, the state changes (`update` and the `with_*`
  functions), `heartbeat_due`, `space_verdict`, `memory_fs`, `stale_work_dirs`, `news` and
  `exit_code`, and for the record and the webhook `record_id`, `clean_text`, `build_record`,
  `render_record`, `select_records_purge`, `notify_payload` and `should_notify`.
- `inbox_unattended_io.py`: the record writes (`write_record`, `purge_records`; implemented,
  through `safefile.atomic_write` so a name is never replaced). Still to come: the walk with
  `stat`, the state write, the work folder cleanup, `os.nice`, the free space.
- `inbox_lock.py` (implemented): `beat` and `mode`, the `Heartbeat` thread, `judge` over two
  readings, `still_mine`; the per-item "still mine" checks come with `--unattended`.
- `notify.py`: the webhook POST, moved from `prune.py` (implemented; `NotifyError`).
- `library_api.inbox_response` (pure) and the two routes in `serve`; `doctor_inbox.judge_unattended_inbox`
  (pure, re-exported by `doctor`; implemented, with `inbox_unattended.newest_record`).
- `inbox.py`: `--unattended` in `run_inbox` (the early exit, the readiness, the per-item checks,
  the record, the exit code from `news`), `Options.unattended`, `chdman_threads`; the scrape after
  the catalog update is `inbox_scrape.after_apply` as in an attended run, and its result (a
  `ScrapeNote`, implemented) is the record's `scrape` note, an input of `build_record` and not of
  `news`. Implemented: the attended run's record and webhook (`_record_run`, `Options.notify_url`,
  `ItemResult.unmoved`).

The modules are added to the [code layout](/warpzone/development/design/code-and-testing/#code-layout).

Unit tests, pure parts first, with no Docker, no scheduler and no real tools:

- `fingerprint`: order of the listing does not matter; a size, an mtime, a new file or a hidden
  file changes it; a link is its own line. That the walk never follows a link is a test of
  `inbox_unattended_io`.
- `decide`: a new item, a changed one, a stable one before and after 300 seconds, a held item with
  the same and with another fingerprint, an entry with one and with two attempts, a name that is
  gone; a clock that goes back does not make an item ready; `first` survives a change.
- `loose_sheets`: a loose cue and the tracks it names, a gdi, a cue naming a file that is not
  there, a loose `.bin` alone, a cue inside a folder (not loose).
- `inbox_lock`: a lock with `beat` whose mtime moved is live, one unchanged for 10 minutes across
  two readings is stale on any host for an unattended run, an attended run judges the age on its own
  clock, a lock without `beat` keeps the 12-hour and pid rules and is
  never taken over unattended; the heartbeat stops touching a lock that is no longer this run's.
- `render_state`/`parse_state` round trip; a damaged state is empty, a damaged entry is dropped;
  the changes apply in order, never mutate their input and give the same result when applied to
  another state; an error keeps its step and is cleared only by a run that got past it; `render_record` from
  `ItemResult` values (placed, already, review, held), with no absolute path and the zRIF redacted;
  `select_records_purge` keeps 50.
- `space_verdict` at the edges (and `MIN_FREE_GIB` is one value for doctor and the run); `memory_fs` on `mountinfo` samples (a volume, `tmpfs`, a nested
  mount); `stale_work_dirs` by age, never a link.
- `news`: a review item in this run; an item held now and not before, held before with the same
  fingerprint (no news), held before with another fingerprint (news); an error new, the same
  (`count` up, no news), changed (news), cleared and back (news); an original held after a
  successful place; the exit code for each, `2` winning over `1`.
- The scrape of an unattended run: with a stand-in Skyscraper, a run that places a cartridge zip
  scrapes it and the record says `scrape.state` `done`; one that places nothing, or only `already`
  games, starts no Skyscraper; a stand-in that prints the quota line, one that fails and a missing
  `/skyscraper` leave the placement, the state and the exit code as they would be without
  Skyscraper, with `not-finished` in the record and no entry in `news`; `--no-scrape` skips it.
- `notify_payload` caps the lists at 20 and holds no path outside the hub; `should_notify` sends
  for placed games and for news, once per error, and nothing for an empty or waiting run.
- `judge_unattended_inbox` for each line above; `inbox_response` with missing, damaged and
  complete files and a placed game not yet in the catalog.
- The CLI: `--unattended` without `--apply`, or with `--force-lock`, exits 2; an empty inbox
  prints nothing, exits 0, creates no lock, reads no DAT (a `dats_dir` that does not exist does not
  matter) and writes only a due heartbeat.

Flow tests with a temporary hub and stand-in tools, the clock injected (`Options.now`):

- Two ticks: the first records a cartridge zip and a CHD system folder item, the second (five
  minutes later) places them, writes the record, updates the catalog and empties the state; the
  API then lists the games with their catalog ids.
- A file that grows between ticks waits; a folder that gains a file waits.
- A run that "dies" (an attempt left in the state): the next tick retries, the one after a second
  death holds it; touching the item clears the hold.
- A live lock: the tick exits 0 as busy. A silent lock with `beat`: taken over at the second tick
  10 minutes later. A lock without `beat`: a run-level error; over three ticks the exit codes are
  `2`, `0`, `0` and the webhook (a stand-in HTTP server) is sent once.
- A held item over three ticks: `1`, `0`, `0`; touched and held again: `1`.
- A PS4 update newer than the library's is replaced in an unattended run as in an attended one,
  with the old package still in `_done` and the replaced version in the record.

Manual checks on a NAS, recorded in the pull request: the scheduled task as written in the guide;
a game copied from macOS and from Windows over SMB, one of several GiB over Wi-Fi, and a copy cut
off half-way; a manual `--apply` from a computer while the task holds the lock and the reverse; a
container killed for memory (a small `mem_limit` and a PS2 image); the UI's inbox view and doctor's
line after each.
