---
title: "Unattended scrape"
---

**Status: implemented, except the guides.**
`warpzone scrape --unattended` runs the steps below (`scrape_unattended_run`) and doctor shows its
`Nightly scrape` line (`doctor_scrape.judge_nightly`). Still to come: the setup guides
([Scraping](/warpzone/setup/scrape/), [The hub container](/warpzone/setup/hub-container/)) that
tell how to schedule it.
This page is
the contract for running `warpzone scrape` on the hub by itself, once a night, from a scheduled
task, with nobody watching. The pages it touches when it is implemented ([Scraping](/warpzone/development/design/scrape/),
[Unattended inbox](/warpzone/development/design/inbox-unattended/),
[Inbox](/warpzone/development/design/inbox/),
[Configuration and command line](/warpzone/development/design/configuration/),
[Doctor](/warpzone/development/design/doctor/),
[Library UI](/warpzone/development/design/library-ui/),
[Code, testing and CI](/warpzone/development/design/code-and-testing/)) and the guides
([Scraping](/warpzone/setup/scrape/), [The hub container](/warpzone/setup/hub-container/)) are
updated in the same pull requests as the code.

The [unattended inbox](/warpzone/development/design/inbox-unattended/#scraping) scrapes the games
it places, and only those. A game that reaches the library any other way (placed with the scrape
turned off, copied by hand, or left behind by a run that hit ScreenScraper's daily limit) stays
without art until a person runs `warpzone scrape`. The goal here is that the hub fills it in on
its own, a few hundred games a night, inside the limits of ScreenScraper and of the NAS.

## Decisions

Decided by the maintainer on 2026-11-14, before this page:

1. **An incomplete memory.** Skyscraper's cache means a game ScreenScraper has no video or back
   cover for is never asked for again, yet it stays "missing media" for ever. The nightly run
   remembers such games and skips them, with no expiry (see
   [The incomplete memory](#the-incomplete-memory)).
2. **Only the nightly run skips on it.** An attended `warpzone scrape` ignores the memory and does
   the full pass, which is what a person runs after purging Skyscraper's cache.
3. **The budget is flags on the task line** (`--max-games 500 --max-minutes 180` by default), not
   a new `[scrape]` table of the config.
4. **Any missing kind counts**, as in `scrape`, but in tiers: new games and games missing a core
   kind go first (see [What gets picked](#what-gets-picked-each-night)).
5. **The webhook only for news**, never for a night that only added art.
6. **Streak thresholds** per kind of trouble, counted in nights (see
   [News and exit codes](#news-and-exit-codes)).
7. **No yielding of the lock to the inbox between batches** for now.
8. **`catalog` is not run before planning.** `serve` rebuilds it every 6 hours and the inbox
   updates it for what it places.
9. **The schedule is daily at 03:00** (see [The scheduled task](#the-scheduled-task)).

**Done when** a game the maintainer places in the library with the scrape turned off has its gamelist
entry and its art on the NAS by the next morning, with nothing run by hand.

## The command

`warpzone scrape --unattended [--notify-url URL] [--max-games N] [--max-minutes N] [--system NAME ...] [--root PATH]`

- `--unattended` refuses `--dry-run`, `--force-lock` and `--retry-not-found`: each is a usage
  error, exit `2`, checked before anything is read.
- `--max-games` (default 500) and `--max-minutes` (default 180) are accepted only with
  `--unattended`.
- `scrape` is a convergent command (see
  [Writing commands](/warpzone/development/design/configuration/#writing-commands)): it writes by
  default, only adds and keeps a backup of any gamelist it edits. So no exception to that rule is
  needed. `configuration.md` names `scrape --unattended` as a convergent command that runs on a
  schedule, which is a note, not a third rule.
- Everything else is the attended command: the platform table, the flow of
  [Scraping](/warpzone/development/design/scrape/#flow), the merge rules (an existing entry is
  never changed, no media file is overwritten) and the not-found memory.

## What gets picked each night

`scrape_unattended.plan_night` is pure: from the selections of every system (what
`scrape.select` returns for the catalog, the gamelists and the media present), the incomplete
memory and the not-found memory (it needs the records' ages for tier 3), it returns a `Night`:
the ordered systems, the chosen games per system (as primary library paths, which
`Night.only()` flattens into the `only` option of `scrape_io`), how many were deferred past the
cap per system, how many were skipped as incomplete, and the backlog (their total, what is left
after the cap). Picks are grouped into four tiers and, inside a tier, ordered by system and path:

1. games with no gamelist entry;
2. games with an entry but missing a core kind (`covers`, `screenshots` or `miximages`);
3. not-found games whose 30 days have passed (`select` already lets them through; the tier comes
   from the age of the record);
4. games missing only extra kinds (`videos`, `backcovers`, `3dboxes` or `marquees`).

The cap `--max-games` is global across systems, so a night's work is the first N of that order.
The next night starts again from what `select` finds, so the backlog drains by itself.

### The incomplete memory

`gather` runs with `onlymissing`, so a game that is in Skyscraper's cache is never asked for
again. A game ScreenScraper has without a video or a back cover (common) therefore keeps "missing
media" for ever: `select` would pick it every night, the merge would come out `unchanged` at no
request, and still each night would pay for staging and two generate passes (the miximage and 3D
box compositing is CPU-heavy on a NAS) and spend a place in the cap that a new game needed.
Retrying such a game after any amount of time gains nothing unless the user purges the cache.

So `scrape` keeps a second memory in the same per-machine file as the
[not-found memory](/warpzone/development/design/scrape/#the-cache)
(`$XDG_CACHE_HOME/warpzone/scrape-not-found.json`, `/work/cache/warpzone/` in the container),
under a new key `"incomplete"`: for each system and gamelist path, `{size, missing: [kinds],
tried}`. Old files without the key still parse.

- **Written by every scrape on the machine,** attended or not, through
  `remember_incomplete(records, merges, picks, written, today, lost)`. A record is written only for
  a game whose verdict is `added`, `filled` or `unchanged`, that has an entry in the hub after the
  merge, with no dropped copy (`lost`), and only when the gamelist write succeeded (`written`).
  `missing` is what the pick lacked minus the kinds the merge added; a game that lacks nothing
  loses its record.
- **Read only by the nightly run.** `skip_incomplete(pick, record)` is true when the game has an
  entry, the size matches and the kinds the game lacks now are a subset of the recorded ones.
  There is no time limit.
- **A game comes back** when its file's size changes, or when a file it had goes missing (a kind
  that is missing now and was not recorded). Deleting the entry and its media and scraping again
  also works, since the record is rewritten by that scrape.
- **The attended `warpzone scrape` ignores the memory** and does the full pass.

### Not-found retry

The not-found memory exists already (see
[The cache](/warpzone/development/design/scrape/#the-cache)) and the nightly run reuses it
unchanged: a game Skyscraper said was not found is skipped for 30 days while its size holds, is
recorded only after a plain "not found" and never after a retry line, and comes back into the
selection by itself when the 30 days pass, as tier 3. `--retry-not-found` stays an attended
option.

## Quota and time budget

ScreenScraper counts its API calls and media downloads against a daily quota (see
[Credentials](/warpzone/development/design/scrape/#credentials)). The requests per new game are
estimated at 1 to 10; **this is not verified and is to be measured** in the manual check on the
NAS. On that estimate, an anonymous account (10,000 a day) covers about 1,000 games. A night of
500 games uses about half, leaving the rest for the day's inbox scrapes, and takes about 83
minutes at 10 seconds a game.

- **The quota is hit.** The existing behaviour stands: the gather stops for every system and what
  is cached is still generated and merged. In an unattended run this is the outcome `quota` and
  not a failure.
- **The time budget runs out.** `batch_fits(now, deadline, games)` is true when
  `now + games * EST_SECONDS_PER_GAME <= deadline`, with the estimate at 10 seconds. The run
  checks it before each batch and stops when the next one would not fit. The first batch of a run
  is always allowed. That is the outcome `budget`, not a failure.
- **Neither is news.** What is left shows as `Night.backlog` in the state, the run record and
  doctor's line.

## Locking with the inbox

The nightly run takes the same lock as the inbox, `inbox/.warpzone.lock`, with its heartbeat. That
keeps one Skyscraper on the cache and on ScreenScraper's one-thread limit, and serializes the
not-found memory and the gamelist writes with the inbox's own scrape.

- **A new optional key, `"task": "scrape"`.** `LockInfo` renders it only when set; the attended
  `warpzone scrape` sets it too. `inbox_lock.describe` then says "held by the nightly scrape on
  the hub" (today it would call any unattended lock "the unattended inbox"), and the inbox API's
  `busy` gains `task` (see [Library UI](/warpzone/development/design/library-ui/)). A lock
  without the key, or with an unknown `mode`, parses as before, and older warpzones ignore the
  key.
- **The wait loop.** The nightly run is once a day, so it has no `lock_seen` from an earlier run
  to compare with. If it finds the lock busy, it waits inside the run: it polls every 60 seconds,
  for at most `LOCK_WAIT_SECONDS` (1800), and takes its own first reading as `previous` for
  `judge(unattended=True)`.
  - A lock silent for 10 minutes is taken over with the existing rename-and-verify.
  - A lock still live after 30 minutes gives the outcome `busy`, exit `0`.
  - A legacy lock (no heartbeat) is a run-level error, as in the inbox.
- **Still mine.** Before each batch the run calls `still_mine()`. If the lock is no longer its
  own it stops: a runtime error, which is news.
- **The inbox waits.** The inbox's ticks during the run exit `0` as busy, so a game dropped at
  03:30 waits until the run ends (at most the time budget). That is acceptable at that hour, and
  the run does not yield the lock between batches for now.

## State and run records

Both live on the hub next to `catalog/inbox/`, outside every Syncthing folder, and
`warpzone catalog` leaves them alone.

**`catalog/scrape/state.json`:**

```json
{"v": 1, "checked": "2026-11-14T03:00:02Z",
 "running": {"run": "20261114T030000Z", "started": "2026-11-14T03:00:02Z"},
 "last": {"run": "20261113T030000Z", "finished": "2026-11-13T04:23:10Z", "outcome": "budget"},
 "backlog": {"left": 1240, "at": "2026-11-13T04:23:10Z"},
 "error": null,
 "trouble": null}
```

- `last.outcome` is `done`, `budget`, `quota`, `busy`, `stopped`, `failed` or `nothing` (see
  [Outcome and trouble of a night](#outcome-and-trouble-of-a-night)).
- `running` is set when the lock is taken and cleared at the end. A later night that finds it
  still set knows the earlier run died (the trouble `died`, see below).
- `backlog` is what the last night that planned left for the next ones (`Night.backlog`); a night
  that did not plan (a busy lock) keeps it.
- `error` is `{message, step, first, count, config}` and `trouble` is `{kind, systems, first,
  count, notified}` (see [News and exit codes](#news-and-exit-codes)).
- A part that cannot be parsed (a wrong type, an unknown `outcome` or `kind`, a `count` below 1)
  is dropped with a warning and the rest is kept. A file that is not JSON or has another `v`
  reads as empty with a warning.
- The state is updated by read-modify-write with pure `Change`s, the same pattern as
  [the inbox's state](/warpzone/development/design/inbox-unattended/#the-memory): a change is a
  function from a state to a state, applied again to what was read again when the write finds the
  file changed. A file that cannot be parsed reads as empty with a warning.

### Outcome and trouble of a night

`scrape_unattended.judge_night(summary, lock)` is pure and maps what the run left (its
`scrape.Summary`, or none when no Skyscraper ran, and whether the wait for the lock ended with the
lock taken or still busy) to the outcome and to the kind of trouble the night adds to a streak:

| The run | Outcome | Trouble |
|---|---|---|
| The lock was still live after the wait | `busy` | `lock` |
| No summary: nothing to do | `nothing` | none |
| Stop reason `quota` | `quota` | `quota` |
| Stop reason `blacklisted`, `closed`, `busy` or `network` | `stopped` | the same name |
| Stop reason `lock lost` | `stopped` | `lock` |
| No such stop, but a system's batches failed (or the run counts as failed), including a night that also ran out of time | `failed` | `failed`, with the systems |
| Stop reason `deadline`, nothing failed | `budget` | none |
| No stop, nothing failed | `done` | none |

A stop wins over failed systems (the failures are most likely its effect), and the quota wins
over every other stop. `stopped` is therefore a night that ended early on a condition other than
the budget and the quota, and the trouble says which one; `failed` is a night that went through
its games with systems that did not work. A lost lock is also a runtime error in the run
(see [Locking](#locking-with-the-inbox)); the run records it with `with_error` when it does.

The streak of the kind keeps the nights in a row in `trouble`. A night of the same kind raises
`count` and keeps `first` and `notified`; a night of another kind starts a new streak, so it
replaces the old one; a night with no trouble (`done`, `budget`, `nothing`) clears it. The quota
night is a trouble of its own so that the streak can be counted: `trouble.kind` is `quota`,
`notified` is never set (`NEWS_AFTER` has no night for it), and doctor reads a `quota` trouble
with `count` 3 or more as its "quota 3 nights" warning (`QUOTA_WARN_NIGHTS`). That a quota night
replaces another streak is what "a night with the outcome `quota` clears the streak" means for
the other kinds.

`died` is found at the start of a run: `with_started` finds `running` still set, counts the night
as `died` (a streak of its own) and sets `running` for the new run. The end of that run keeps
the `died` count it just made even when the night itself is clean (`with_night(..., died=True)`),
so two runs in a row that die reach the threshold, and a night after that with no trouble clears
it. The end of a run clears `running` only when it is that run's own.

**`catalog/scrape/runs/<id>.json`**, the newest 50 kept:

- Written for runs that started Skyscraper or had news.
- It holds `v`, `id`, `started`, `finished`, the `outcome`, the counts per system (`added`,
  `filled`, `not_found`, `unchanged`, `skipped_not_found`, `skipped_incomplete`, `deferred`; a
  system that was deferred and never reached has zeros and its `deferred`), the paths of the
  added and filled games (`added`, `filled`, 200 each at most), `failed_systems`, the `stop`
  reason (or null), `budget_spent` (true when the time budget ended the run), the `warnings` (20
  at most), the `exit` code and whether the webhook was sent (`notified`).
- Paths are library paths below the hub root (`library/roms/<system>/<name>`); no absolute path,
  host name, key or URL is written (`clean_text` runs over every text).

The record id, the purge to 50, the newest-record lookup and the text cleaning
(`record_id`, `select_records_purge`, `newest_record` and `clean_text` of `inbox_unattended`) are
reused. `write_record` and `purge_records` of `inbox_unattended_io` take the folder (and, for
`write_record`, the render function) as keyword arguments, and the read-modify-write of the state
takes a `StateFile` (its path, its parse and render functions, the name shown in messages) as
`update_file_checked` and `read_file`; the inbox's `update_state_checked` and `read_state` are
those calls with the inbox's `StateFile`. No new shared module. The folders are
`scrape_unattended.state_path(root)` and `runs_dir(root)` (`catalog/scrape/state.json` and
`catalog/scrape/runs/`).

## News and exit codes

`news(before, after)` is pure and compares the state as the run read it with the state it writes,
using streak thresholds `NEWS_AFTER`, counted in nights:

| Trouble | Night it becomes news |
|---|---|
| ScreenScraper blacklisted the client (`blacklisted`) | 1st |
| `network`, ScreenScraper too busy (`busy`), `failed` (a system's batches failed), `lock` (busy all night), `died` | 2nd in a row |
| ScreenScraper closed for maintenance (`closed`) | 3rd in a row |
| `quota` | never |

- A new run-level `error` is news the first time, with the inbox's rules: kept by message and
  step, the `count` goes up on repeats, and a run that gets past the step clears it.
- `trouble.notified` makes each streak be reported once: the run marks it (`with_notified`) when
  it reports the news, whether or not a webhook was sent, and the streak is news only while it
  is not marked. A night with the outcome `done`, `budget` or `nothing` clears the streak, and
  one with the outcome `quota` replaces it with the quota's own streak, which is never news.
- **Exit codes:**
  - `0`: no news. This covers nothing to do, the budget spent, the quota reached, the first busy
    night, and repeats of a known problem.
  - `1`: runtime news.
  - `2`: new configuration news (no Skyscraper, `/skyscraper` not a directory, the scratch folder
    on `tmpfs`, the hub layout, a legacy lock) and every usage error. An error met before the hub
    root is known, or with no `catalog/` folder, exits `2` on every run, as in the inbox.
  - A run with news of several kinds exits `2` if one is a configuration error, else `1`.
- **Webhook.** `--notify-url` posts through `notify.notify`, only for news, never for a night that
  only added art:

  ```json
  {"source": "scrape", "mode": "unattended", "run": "20261114T030000Z",
   "added": 120, "filled": 30, "not_found": 4, "left": 1240, "problem": "network"}
  ```

  `notify_payload(record, news, left)` builds it (`problem` is the kind of the first streak in
  the news, `error` for a run-level error, and the error's text is not in it) and
  `should_notify(news)` decides when. The URL never appears in output
  or records (`clean_text`, kept out of `repr`), a failed send is a warning, and no key or path
  outside the hub is in the payload.

The point is the same as in the inbox: a scheduler that mails on an abnormal end mails once per
problem, and what persists stays in the state, where doctor shows it on every look.

## Run steps

`scrape_unattended_run.py` (above `scrape_io.py`; `cli.py` calls it) goes through these steps and
stops at the first that says so:

1. **Usage checks** (the refusals of [The command](#the-command)).
2. **Config, hub root and layout**: `inbox/`, `library/.stfolder`, `library/roms` and `catalog/`
   must exist.
3. **Read the state.** Check that Skyscraper is found, that the `WARPZONE_SKYSCRAPER_DIR` folder is
   a directory, and that the scratch folder (`<work_dir>/scrape`, or `/work/cache`) is not on a
   memory filesystem (`memory_fs`).
4. **Plan without the lock** (`plan_night`, over `scrape_io.select_all`). With nothing to do,
   write `checked`, exit `0`, take no lock and start no Skyscraper. An empty night is cheap, and it
   writes nothing else: no `last`, no record. It only clears what the night got past (an error of
   an earlier step, a trouble streak, a backlog that is now zero). A system whose catalog or
   gamelist cannot be read is left out of the plan with a warning.
5. **Take the lock** with the wait loop of [Locking](#locking-with-the-inbox), then note the run in
   the state (`with_started`, which is how a run that died is found) and lower the run's priority
   with `os.nice(10)`, a failure of which is a warning. The time budget counts from the start of
   the run, so a night that waited for the lock has that much less to work.
6. **Run `scrape_io`** with `only=chosen`, the deadline, `still_mine` and `skip_incomplete`. It
   selects again under the lock, which is the re-check right before acting, so a game the inbox
   scraped meanwhile is not repeated. A failure of the run that is not a game's (an exception)
   is a runtime error recorded with its step, with the paths and the URL cut out of its text.
7. **Finish**: send the webhook if there is news, write the record (the webhook first, so the
   record says whether it was sent), update the state, release the lock and exit. The `left` of
   the state, the record and the webhook is `Night.backlog`: the games the cap deferred. Games a
   stop left untried are chosen again by the next night, not counted there.

To make the record possible, `scrape_io` returns a summary of the run (the merges per system, the
stop reason, the games not tried, the systems whose batches failed, the budget spent); today the
per-game merges are only printed. The attended command's output and exit codes do not change.

The run does not run `catalog` first: `serve` rebuilds the catalog every 6 hours and the inbox
updates it for what it places. A stale catalog is what doctor's catalog line is for.

## Doctor

`Nightly scrape`, in the tools group under `Skyscraper`. `doctor_scrape.judge_nightly(state,
record, now)` is pure and returns one verdict; when several cases hold they share the line, as for
the [unattended inbox's](/warpzone/development/design/inbox-unattended/#doctor):

- `[--]` no state file: schedule `warpzone scrape --unattended` (see
  [The scheduled task](#the-scheduled-task)).
- `[!!]` a run-level error, with how long it has lasted and how many runs met it.
- `[! ]` the last check is more than 36 hours old: "is the scheduled task enabled?".
- `[! ]` a trouble streak at or above its threshold in the table above, or a quota streak of 3
  nights or more (the quota is not news, but a quota reached every night means the library is
  larger than the account can cover).
- `[ok]` "last run <date>: added X, filled Y, not found Z; N left (about D nights at M a night)".

Details the code settles:

- The line is shown in the inbox tools group, right after `Skyscraper`, under the conditions of
  the unattended inbox's line (an `[inbox]` table and a hub root that can be read).
- The thresholds are those of `NEWS_AFTER` and `QUOTA_WARN_NIGHTS`; a streak is shown while it
  lasts, whether or not it was already reported.
- `M` is not stored anywhere (the flags are on the task line), so it is the size of the last
  night: the games Skyscraper was asked about, the `added`, `filled`, `not_found` and `unchanged`
  of the newest run record, over every system. `N` is the state's `backlog.left`. The estimate is
  `ceil(N / M)` and is shown only when the last run ended on its cap or its time budget
  (outcome `done` or `budget`) and `M` is not zero; after a stop (the quota, a failure) a night
  did not show what it can do, so only `N left` is printed. With nothing left it says "nothing
  left", and with no backlog in the state it leaves the part out.
- With no run record (empty nights write none, and the newest 50 are kept) the line dates the run
  from the state's `last.finished` and says "no record kept"; with no run at all yet it says so.
- A state file the parser could not read whole (it reads as empty, or a part is dropped) adds
  the parser's warning to the line as a `[! ]` finding. A run record that cannot be parsed is no
  record.

## Interface

Later. The doctor line, the run record and the webhook are enough for now, and the library UI's
Inbox view already shows the lock with its new `task` label ("held by the nightly scrape"). A
follow-up can add one line, "last nightly scrape ...", to `GET /api/v1/inbox` and its view.

## The scheduled task

On a Synology, in Task Scheduler: Create, Scheduled Task, User-defined script, user `root` (Docker
on the NAS needs it), daily at 03:00. ScreenScraper's daily quota has reset by then and the hour
is outside the busy ones:

```sh
cd /path/to/the/compose/folder && /usr/local/bin/docker compose run --rm -T warpzone-inbox scrape --unattended
```

`--notify-url URL` is optional. The task uses the same compose service as the inbox's (`/skyscraper`,
the work volume, `mem_limit: 2g`, `cpus`), so there is nothing new to configure beyond the line.
The DSM mail on an abnormal end arrives once per problem, since the run exits non-zero only for
news. Other NAS systems use their own scheduler or root's crontab with the same line.

## Out of scope

- Changing or improving a scrape that exists (see
  [Scraping](/warpzone/development/design/scrape/#scope-and-non-goals)).
- Yielding the lock to the inbox between batches; this can be revisited if items wait too long.
- A `[scrape]` config table for the budget.
- Running `catalog` before planning.
- A "last nightly scrape" line in the library UI.
- A catch-up scrape inside the first inbox tick after 03:00, which would break the inbox's "empty
  is cheap" rule.
- Scraping on a console, and the systems in `UNSCRAPED`.

## Code and testing

- `scrape_unattended.py` (pure): `plan_night`, `judge_night`, the state and record
  parse and render, the pure changes, `news` (a list of `NewsItem`), `exit_code`,
  `build_record`, `notify_payload` and `should_notify`.
- `scrape_unattended_run.py`: the run, step by step.
- `scrape.py`: the incomplete memory (`remember_incomplete`, `skip_incomplete` and the section of
  the memory file) and `batch_fits`. `scrape_io.py`: the summary and the `deadline`, `still_mine` and
  `skip_incomplete` inputs. `inbox_lock.py`: the `task` key. `doctor_scrape.py`: `judge_nightly`.
- The modules are added to the
  [code layout](/warpzone/development/design/code-and-testing/#code-layout).

Unit tests, pure parts first, with no Docker, no scheduler and no real Skyscraper:

- `plan_night`: tier order, the global cap, a new missing kind or a size change picking a game
  again, a not-found game at 29 and 31 days.
- The incomplete memory: the subset skip, an attended `select` unaffected, old memory files still
  parsing.
- The state and record: round trip with damaged parts dropped, changes pure and order-stable, each
  `NEWS_AFTER` streak and the `notified` flag, an error kept by step and cleared, `exit_code` with
  `2` winning over `1`, a payload with no URL or absolute path, the purge to 50.
- The lock: round trip with and without `task`, a lock without it and one with an unknown mode
  still parse, the `describe` text.
- `judge_nightly` for each case.

Flow tests with a temporary hub, a stand-in Skyscraper and the clock and sleep injected:

- Usage errors exit `2`; nothing to do exits `0`, writes only `checked` and takes no lock.
- A capped night scrapes N games and writes the record; the deadline stops before the second
  batch; a lost lock stops the run.
- The quota exits `0`. Network trouble over three nights exits `0`, `1`, `0`, with the webhook
  sent once to a stand-in server.
- A live lock gives `busy`, `0`; a silent lock is taken over after 10 minutes of the fake clock;
  a configuration error over three nights exits `2`, `0`, `0`; a leftover `running` is reported
  as `died`.

Manual checks on the NAS, recorded in the pull request: the task runs at 03:00 and writes its
first record; doctor's line is `[ok]`; an inbox tick during the run exits busy; and the requests
per game are measured against the quota, to replace the estimate above.
