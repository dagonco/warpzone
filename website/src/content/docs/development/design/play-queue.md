---
title: "Play queue"
---

**Status: implemented: the file, the inferred states, the HTTP API and the page (the Queue
panel of the game page, the Up next section).** This is the design of the play queue of the
[library UI](/warpzone/development/design/library-ui/), with the maintainer's answers to the
review's questions recorded in [Decisions](#decisions). It is the contract for the code in
`queue.py`, `queue_store.py`, the two routes of `serve.py` and `data/ui/queue.js`.
[Implementation notes](#implementation-notes) lists the places where the code settled something
the design left open, and [The page](#the-page) the same for the UI.

The play queue is an ordered list of the games the user wants to play next, kept on the hub and
shown by the library UI: a "Queue" button on each game page and an "Up next" section on the home
page. Its point is the console: queueing a game for a console turns the game on in that console's
whitelist, so the game is already downloaded when the user picks the console up. Finishing it or
dropping it can turn it off again, and the console's prune timer frees the space later.

This is the first state the server keeps that is not a whitelist. Everything else the UI shows is
read from files other commands write (the catalog, the gamelists, the play-state ledgers, the RA
caches). The queue therefore needs its own file, its own conflict check and its own backups, and
a rule for how a queue change and a whitelist change compose.

## Decisions

These are the choices of this design. Items 1, 3, 6, 7, 10 and 11 were confirmed by the
maintainer in review.

1. Every entry and every collection carries an `owner` from format `1`. The UI has one shared
   token and no users (see [Authentication](/warpzone/development/design/library-ui/#authentication)),
   so today every entry has the one owner `default`; when users come, each user's entries carry
   that user's name and the file keeps its format.
2. The queue lives in `catalog/queue.json` on the hub, outside every Syncthing folder. No console
   receives it; a Decky plugin reads it through the API.
3. A game in the queue is named by its catalog id, with a snapshot of its system, primary path,
   title and title key, so an entry whose game left the library is shown and can be relinked,
   never dropped silently. A relink is always proposed by the server and confirmed by the user,
   never automatic, even when there is a single candidate.
4. The queue records **intent**, the whitelist is the **effect**. A request that changes both
   writes the queue first, so every failure in between leaves a state the page can show and
   repair with one click, and nothing ever leaks space without a record.
5. The queue only removes what it added. It records, per console, whether the game was already
   there when it was queued; it never turns off a game that was on before, and it never adds an
   exclusion line.
6. Releasing a console (turning the game off when it is finished, dropped or removed) is never
   automatic: the page asks, with the consoles the queue turned on pre-checked.
7. `playing` is shown when the game was played since it was queued and within the last 14 days,
   and `finished` is suggested from the RetroAchievements award, but neither is ever written by
   the server on its own: a `GET` never writes, and only the user's request changes the file.
8. Writes stay `POST` only, like every other write of the API: one route takes a list of
   operations, so there is one write path, one lock and one validation order.
9. Collections (manual lists, no whitelist effect) have their schema in the same file from
   format `1`, so adding them later needs no format change.
10. The queue and the switches stay separate: `POST /api/v1/changes` neither reads nor writes the
    queue, and a manual switch does not end a claim (see
    [Releasing never takes what the user had](#releasing-never-takes-what-the-user-had)).
11. Finished and dropped entries are kept forever. The server never drops an entry to make room;
    the only bound is the size of the file, and a write that would pass it is refused with a clear
    error (see [Limits](#limits)).

## The file

### Location

```
<hub root>/catalog/
├── queue.json                    the queue and the collections
└── queue-backups/<UTC time>.json the files the server replaced, 50 newest
```

`catalog/` is already outside every Syncthing folder and covered by the hub's filesystem
snapshots (see [Snapshots on the hub](/warpzone/operation/safety/#snapshots-on-the-hub)).
`warpzone catalog` and `inbox` write only `catalog/systems/` and `catalog/ra/` and never read or
touch `queue.json`. The only writer is `warpzone serve`; the conflict check below still guards
against a hand edit or a restored backup.

### Schema

UTF-8 JSON written with sorted keys and a final newline, so one queue always gives the same
bytes. The order of `queue` is the order of the queue and is kept as written.

```json
{
  "format": 1,
  "updated": "2026-11-10T20:00:00Z",
  "queue": [
    {
      "owner": "default",
      "game": {"id": "5d0c1f3a9b2e7c48", "system": "psx",
               "primary": "roms/psx/Example Game (Europe).chd",
               "title": "Example Game (Europe)", "key": "example game"},
      "state": "queued",
      "added": "2026-11-01T18:00:00Z",
      "changed": "2026-11-01T18:00:00Z",
      "ended": null,
      "ra_dismissed": null,
      "devices": {
        "deck": {"before": "off", "since": "2026-11-01T18:00:00Z", "end": null, "until": null},
        "machine": {"before": "on", "since": "2026-11-01T18:00:00Z", "end": null, "until": null}
      }
    }
  ],
  "collections": {
    "3f2a9c1e": {"owner": "default", "title": "Couch co-op",
                 "created": "2026-11-02T10:00:00Z",
                 "games": [{"id": "…", "system": "…", "primary": "…", "title": "…", "key": "…"}]}
  }
}
```

- `format`: `1`. A reader takes a file of a higher format as read-only (see
  [Validation, the conflict check and writing](#validation-the-conflict-check-and-writing)).
- `updated`: the time of the last write, UTC, ISO 8601 with `Z`, whole seconds, like every time
  in the file.
- `owner`: whose entry it is, a name of 1 to 32 characters among lower-case letters, digits, `-`
  and `_`. While the UI has one token there is one user, and its name is `default`: the server
  writes `default` in every entry it makes and answers and changes only the entries of `default`.
  Entries of any other owner (written by a later warpzone that knows users) are kept byte for byte,
  never shown, changed or counted. A user's queue order is the order of that user's entries in
  `queue`, and `position` counts that user's active entries only. When users exist, the owner of a
  request is the authenticated user, and nothing else in the file changes.
- `game`: the **game reference** (below). One entry per owner and game id: queueing a game that
  is in the user's queue is refused, queueing a game whose entry has ended starts it again
  (below).
- `state`: `queued`, `playing`, `finished` or `dropped`. The first two are **active**, the other
  two **ended**. `added` is when the entry was last queued, `changed` when `state` last changed,
  `ended` when it became ended (`null` while active).
- `ra_dismissed`: the date of the RetroAchievements award whose "mark as finished?" suggestion
  the user dismissed, or `null` (see [Inferred states](#inferred-states)).
- `devices`: the **claims**, one per console the game was queued for (empty when it was queued
  for none). `before` is the game's state on that console when it was queued for it (`off`,
  `on` or `partial`, read from the whitelist at the `etag` the request named); `since` is when;
  `end` is `null` while the claim is open, `released` when the user asked for the game to be
  turned off there, `kept` when the user asked for it to stay; `until` is when it ended.
- `collections`: keyed by an id of 8 hex digits the server draws at random when a collection is
  made. `owner` is as for an entry. `games` is an ordered list of game references, unique by id.
  Collections never touch a whitelist. Format `1` readers validate and keep them; the operations that edit them come later
  and need no new format.

The queue **holds** a game on a console when that console's claim has `before` `off` and `end`
`null`: the queue wants the game on there, and it is the queue's to turn off. A claim with
`before` `on` or `partial` never holds: the user had the game (or part of it) there first, so
the queue turns a partial game fully on when it is queued but never turns it off.

A file that does not match this schema exactly (an unknown key, a wrong type, a time that is not
in the form above, two entries with the same owner and id, a claim for a device name that is not a valid device
name, an `ended` that is set while the entry is active or missing while it is ended, an `end` without
an `until` or the other way round) is **damaged** (below).

### Game references

`game` keeps the catalog id and a snapshot taken when the entry was written:

- `id`: the catalog id, the first 16 hex digits of the SHA-256 of the primary part's path (see
  [Games and parts](/warpzone/development/design/library-ui/#games-and-parts)). The API names
  games by it, so the queue does too. It stays the same as long as the primary part keeps its
  path, which the library never changes on its own.
- `system`, `primary`, `title` and `key`: the system folder, the primary path, the catalog title
  and its [title key](/warpzone/development/design/library-ui/#title-key), as the catalog had
  them. They are never used to find the game while its id is in the catalog; they exist to show
  an entry whose game is gone and to look for where it went.

`queue.resolve` (pure) finds each reference in the catalog and gives one of:

| Result | When | The page |
|---|---|---|
| `present` | the id is in the catalog | the game as usual |
| `moved` | the id is not, and some catalog games have the same `key`: first those of the same `system`, then the others, at most 10, games already in the queue left out | the stored title, "No longer in the library. Is it one of these?" and a relink button per candidate |
| `missing` | the id is not, and no game has the `key` | the stored title, greyed, with only "Remove" |

What each case means in practice:

- **A catalog run, a rescan, a new scrape:** the id does not change; nothing happens.
- **Re-filed under a new path** (a `.pkg` replaced by its installed folder, a cue sheet filed
  again as a CHD through `inbox`, a file renamed by hand): the new copy has a new id. While the
  old copy is still there the entry keeps pointing at it and is `present`; once the old copy is
  deleted and the catalog drops it, the entry becomes `moved`, with the new copy as a candidate.
  The server only proposes the candidates; relinking is the user's confirmed action (`relink`,
  below), never automatic, not even with one candidate in the same system. It keeps the state, the dates and the order,
  ends every open claim as `kept` (the claims were about the old parts, and the new game's state
  on each console says nothing about who put it there) and takes a new snapshot.
- **Removed from the library:** `missing`. The whitelist lines of the removed parts stay in the
  whitelists, as they do today for any game that leaves the library; the queue cannot turn off a
  game whose parts it no longer knows, so a held claim of a missing game can only end as `kept`.

Collections use the same references and the same resolution.

## Inferred states

The server never writes `playing` or `finished` by itself. It works out what to show when it
answers, from data it already reads, with two pure functions:

- `queue.shown_state(entry, play, now)`: an entry stored as `queued` is shown as `playing`, with
  `inferred: true`, when its game's play totals (the `totals.last` of
  [play statistics](/warpzone/development/design/library-ui/#endpoints)) say it was played at or
  after `added` and at most 14 days before `now`. An entry stored as `playing` is shown as
  `playing`, whatever the ledgers say. Ended entries are shown as stored.
- `queue.suggestion(entry, award)`: an active entry gets `{"state": "finished", "award": kind,
  "date": date}` when the game's RetroAchievements `progress.award` (see
  [Achievements](/warpzone/development/design/library-ui/#achievements)) is any of
  `beaten-softcore`, `beaten-hardcore`, `completed` and `mastered`, its date is at or after
  `added`, and it is later than `ra_dismissed`. An award from before the game was queued (a
  replay) suggests nothing. Without `[ra] user` or a downloaded progress there is no suggestion.

Accepting a suggestion is the same request as setting the state by hand, with the same release
dialog. Dismissing it stores the award's date in `ra_dismissed`, so the same award does not ask
again and a later, higher award does.

## How a queue change composes with the whitelist

### One request, queue first

Queueing for a console, releasing a console and the repair action are each **one request** that
writes the queue and the whitelists of the consoles it touches. The page never sends two
requests for one user action, so there is no window in which the page holds half a change.

Inside the request the queue file is written **before** the whitelists. Every failure between the
two writes then leaves a claim that disagrees with the whitelist, which the page shows and repairs
(`queue.device_view` below), and never a whitelist change with no record:

| Action | Queue write | Then the whitelist | If the whitelist write fails |
|---|---|---|---|
| queue for a console | a claim with `before` and `end: null` | game on, unless `before` is `on` | the claim holds, the game is off: shown as "not on the console yet", with "Turn on" |
| release a console | the claim's `end` becomes `released` | game off | the game is still on: shown as "still on the console", with "Remove" |
| keep on a console | the claim's `end` becomes `kept` | nothing | (no whitelist write) |
| remove an entry that holds consoles | the claims end (as above) and the entry stays; a second queue write deletes it once every whitelist was written | game off on the released consoles | the entry stays, showing the console that still has the game |

The opposite order would turn a failure after the whitelist write into a game that is on a
console with no claim, and a retry would then record it as `before: on`, so finishing it would
never free the space. Writing the queue first is the order whose failures are visible and
harmless.

### Per console, after reading

`queue.device_view` (pure) compares each claim with the game's state on that console (from the
whitelist, as `game_state` gives it in the API) and gives one of:

| View | When | The page offers |
|---|---|---|
| `on` | the queue holds the game and it is on | the console's progress |
| `not-on` | the queue holds the game and it is `off` or `partial` (a failed write, or the user turned it off with the switch) | "Turn on" |
| `kept` | `before` is `on` or `partial`, or `end` is `kept` | nothing: the game is the user's, not the queue's |
| `released` | `end` is `released` and the game is off | nothing |
| `not-released` | `end` is `released`, `before` was `off`, and the game is still on | "Remove" |
| `unknown` | the device left the config, its whitelist is `needs-migration`, or the game is `missing` or `moved` | nothing |

"Turn on" and "Remove" are the `sync` operation: it applies the claim's intent again on one
console, with the usual conflict check. The server never repairs on its own; a page that shows a
disagreement is the only trigger.

### Releasing never takes what the user had

A release turns the game off on a console only when the queue held it there (`before` `off`).
`queue.release_change` (pure) also checks the model of the console's whitelist: it removes the
game's includes and nothing else. When a part of the game is still on after that, because the
whole system was turned on, or a parent folder included, after the game was queued, the release
does not add an exclusion line: the console is reported in `skipped` with `reason:
"covered"`, the claim ends as `kept`, and the game stays. A game the user had on the console
before queueing it is never turned off by the queue, whatever the request says: asking to release
a console the queue does not hold is refused with `not-held`.

Two owners can each have an entry for the same game on the same console. A release turns the game
off only when no other entry, of any owner, has an open claim (any `before`) for that game on that
console; otherwise the console is reported in `skipped` with `reason: "claimed"`, the claim ends
as `kept`, and the game stays. With the one owner of today this never happens, since there is one
entry per game.

The switches of the game page, the system levers and `POST /api/v1/changes` do not read or write
the queue. A user who turns a held game off with the switch sees `not-on` on the queue entry; one
who turns it on again sees `on`. Finishing the entry later still asks before releasing.

### Releasing is asked, never automatic

When an entry is finished, dropped or removed, or a console is taken off an active entry, the
request must name every console the queue holds for it, each in `release` or in `keep`; a held
console in neither is refused with `bad-request`. So the server never chooses. The page asks,
in one dialog:

- each console the queue holds, with a checkbox **checked**: "Remove from deck (frees 3.2 GB;
  gone within about an hour)";
- each console where the game was before (`before` `on` or `partial`), without a checkbox: "Stays
  on machine: it was there before you queued it";
- each console whose release would be `covered`, without a checkbox: "Stays on deck: the whole
  system is on there".

The time comes from [Removal reaches the console](/warpzone/development/design/library-ui/#removal-reaches-the-console):
the console applies the whitelist within `rescan_seconds` and its prune timer deletes the files
within the hour, with every guard of unattended pruning. Saves are never touched: they live in
their own folders, and prune does not delete them. Queueing the game again before prune ran costs
nothing, since the files are still there.

## HTTP API

The same server, port, authentication and limits as the rest of the API (see
[HTTP API](/warpzone/development/design/library-ui/#http-api)): every route needs the session
cookie or the bearer token, a cookie-authenticated write must carry an `Origin` equal to the
`Host`, every write is `Content-Type: application/json` with a body of at most 64 KiB, and writes
are `POST` only. The server holds its one write lock for queue writes as for whitelist writes, so
a queue request and a `POST /api/v1/changes` never interleave.

| Method and path | Answers |
|---|---|
| `GET /api/v1/queue?history=N` | the queue, resolved against the catalog, the whitelists, the play totals and the RA progress |
| `POST /api/v1/queue` | applies a list of operations (below); with `"dry_run": true` writes nothing |

### `GET /api/v1/queue`

```json
{"state": "ok", "etag": "sha256:…", "updated": "2026-11-10T20:00:00Z",
 "entries": [
   {"id": "5d0c1f3a9b2e7c48", "system": "psx", "title": "Example Game (Europe)",
    "name": "Example Game", "cover": true, "size": 512000000,
    "found": "present", "candidates": [],
    "state": "queued", "shown": "playing", "inferred": true,
    "suggestion": {"state": "finished", "award": "beaten-hardcore",
                   "date": "2026-11-09T21:00:00+00:00"},
    "added": "2026-11-01T18:00:00Z", "changed": "2026-11-01T18:00:00Z", "ended": null,
    "play": {"plays": 4, "last": 1793000000, "fav": null, "playtime": 5400},
    "devices": {"deck": {"before": "off", "end": null, "game": "on", "view": "on"},
                "machine": {"before": "on", "end": null, "game": "on", "view": "kept"}}}],
 "history": [], "history_total": 0,
 "size": 48213, "limit": 4194304,
 "collections": [{"id": "3f2a9c1e", "title": "Couch co-op", "games": 12}]}
```

- `state`: `ok`; `absent` (no file yet: an empty queue, `etag` `absent`); `damaged` (the file
  cannot be read, is not UTF-8 JSON, is over 16 MiB or does not match the schema: `entries` and
  `history` are empty and every write is refused with `queue-damaged`); `newer` (a format above
  `1`: `entries` and `history` are empty, since a reader cannot trust keys it does not know, and
  every write is refused with `queue-damaged`). With `damaged` or `newer`, a `message` names the
  file and the cause, never the hub's path.
- `etag`: `sha256:<hex>` of the file's bytes, or `absent`, echoed in `expect_queue`.
- `entries`: the user's active entries in queue order; `history`: the user's ended ones, latest
  `ended` first, at most `N` of them (`history`: a whole number from 0 to 10000, default 50;
  anything else is `400 bad-request`), and `history_total` how many there are in all.
- `size` is the file's size in bytes and `limit` the [size limit](#limits) of a write.
- `name`, `cover`, `size` and `play` are as in the compact game list, and are absent for a
  game that is not `present`. `candidates` lists `{"id", "system", "title", "name"}` for a `moved`
  game and is empty otherwise.
- `devices`: per claim, `game` is the game's state on that console as in the system list
  (`on`, `off`, `partial`, `unsupported`, `unknown`) and `view` is `queue.device_view`.
- `collections`: the collections, by title, with their game count. Their content gets a route
  when collections are built.

### `POST /api/v1/queue`

```json
{"ops": [
   {"op": "add", "game": "5d0c1f3a9b2e7c48", "devices": ["deck"], "position": null},
   {"op": "state", "game": "0a1b2c3d4e5f6071", "state": "finished",
    "release": ["deck"], "keep": []}],
 "expect_queue": "sha256:…",
 "expect": {"deck": "sha256:…"},
 "dry_run": false}
```

`expect_queue` is the queue's `etag`. `expect` holds the whitelist `etag` of every console an
operation reads or writes, exactly as in `POST /api/v1/changes`. The operations, applied in
order to one model of the queue:

| `op` | Fields | Does |
|---|---|---|
| `add` | `game`, `devices`, `position` | queues a game for zero or more consoles, at `position` among the active entries (`null`: last). A game whose entry has ended starts again: `state` `queued`, a new `added`, and new claims (the old ones have all ended). Refused with `already-queued` while it is active |
| `devices` | `game`, `add`, `release`, `keep` | changes the consoles of an active entry: new claims for `add`, ended claims for `release` and `keep` |
| `move` | `game`, `position` | moves an active entry |
| `state` | `game`, `state`, `release`, `keep` | sets the state. Going to `finished` or `dropped` ends every held claim, each named in `release` or `keep`; going back to `queued` or `playing` from an ended state opens no claim (`devices` does) |
| `remove` | `game`, `release`, `keep` | deletes the entry, after ending its held claims as for `state` |
| `sync` | `game`, `device` | applies the claim's intent again on one console (`not-on` turns on, `not-released` turns off); refused with `in-sync` when the view is neither |
| `relink` | `game`, `to` | points a `moved` or `missing` entry at another catalog game (see [Game references](#game-references)) |
| `dismiss` | `game` | stores the date of the current award suggestion in `ra_dismissed` |

The answer is `{"dry_run": …, "queue": <the GET body after the change>, "devices": […],
"skipped": […]}`, with `devices` and `skipped` as in the answer of `POST /api/v1/changes` (the
files written, the games turned on and off, the bytes, the warnings, and the consoles left alone
with a reason). With `"dry_run": true` the server validates and computes everything, writes
nothing and answers with `"dry_run": true`; the page uses it for the confirmation dialog.

### Validation, the conflict check and writing

`queue.plan` (pure) takes the request, the queue as read, the config, the catalog and the
whitelists as read, and the time, and returns either the new queue, the whitelist changes per
console and the second queue (for a `remove` that ends claims), or the first error, in this
order: the body's shape and the [limits](#limits); the queue is not `damaged` or `newer`; each
`add` names a catalog game, each device named is known (a `keep` may name a console that left
the config) and has a managed whitelist (`not-managed` otherwise); `expect_queue` and an
`expect` for every console an operation reads or writes (`add`, `devices.add`, `release`,
`sync`; not `keep`) are given (`expect-missing`) and equal the current `etag`s (`stale`); then
the operations in order, each with its own checks: the entry exists, the position is valid, the
console supports the game's system (an `add` for a console with the system in `exclude_systems`
is refused with `unsupported`), the release rules above; last the size limit, which needs the
rendered file. The whitelist changes are the same change items as in `POST /api/v1/changes` (`game
on` and `game off` for one device) and go through the same per-device planning, so the effect
check (the game reaches its state and no other game changes) is the one the switches use.

Then the server takes the write lock and:

1. Reads `queue.json` again with its stamp (`safefile.stamp`) and compares the SHA-256 with
   `expect_queue`; reads each touched whitelist again and compares it with its `expect`. Any
   mismatch refuses the whole request (`stale`) before anything is written.
2. Plans again on what it just read (the pure step above).
3. Copies the current `queue.json`, when there is one, to `catalog/queue-backups/<UTC time>.json`
   and keeps the 50 newest.
4. Writes the new queue with `safefile.atomic_write` and the stamp from step 1 (`expect=None`
   for a file that did not exist), which compares once more right before the rename.
5. Writes each whitelist exactly as
   [Reading, the conflict check and writing](/warpzone/development/design/library-ui/#reading-the-conflict-check-and-writing)
   describes, steps 3 to 5 (backup, guarded atomic write, hub scan).
6. For a `remove` that ended claims, and only when every whitelist of step 5 was written, writes
   the second queue the same way, with the stamp of the file written in step 4.

A failure in step 4 writes no whitelist and answers `500` `write-failed` (or `stale`, when the file
changed during the write), with `"queue": true` in the error. A failure in step 5 or 6 answers
`500` with the body of a success (`devices` with what each whitelist did, `skipped`) in which
`"queue": true` says that the queue file was written, plus an `error`: `write-failed` for the
device (step 5) or with `"queue": true` for the file (step 6). Each file is whole either way,
and the next `GET` shows the claims that disagree with their whitelists. The server keeps the
parsed queue in memory for `GET` and reads it again when its size or mtime changed, checked at
most every 5 seconds, like the other files; its own writes drop it at once.

### Errors

The error body and the existing codes are those of
[Validation and errors](/warpzone/development/design/library-ui/#validation-and-errors). A
queue error that is about the queue file rather than a whitelist carries `"queue": true` instead
of `device`. New codes:

| Status | `code` | When |
|---|---|---|
| 404 | `not-found` | also: a game that has no entry, for every operation but `add` |
| 409 | `already-queued` | `add` for a game whose entry is active |
| 409 | `not-held` | `release` names a console the queue does not hold for that game |
| 409 | `in-sync` | `sync` on a claim whose view is not `not-on` or `not-released` |
| 409 | `queue-damaged` | the queue file is `damaged` or `newer`; it is never overwritten (restore a backup) |
| 409 | `queue-full` | an `add` past the limit of active entries |
| 409 | `queue-too-large` | the new file would be over the size limit and larger than the current one; nothing is written |
| 412 | `stale` | also: `expect_queue` differs from the file as it is now |
| 428 | `expect-missing` | also: no `expect_queue` |

### Limits

- At most 200 active entries per owner (`queue-full`): a queue is a list of what comes next,
  not a backlog of everything.
- Finished and dropped entries are never dropped by the server: the history is kept forever.
- At most 50 operations per request and at most 8 consoles per entry; a `position` from 0 to the
  number of the user's active entries.
- At most 100 collections, 1000 games per collection and 100 characters per collection title, all
  inside the size limit.
- **Size.** A write whose rendered file would be over 4 MiB, and larger than the file it
  replaces, is refused with `queue-too-large` before anything is written, the whitelists
  included. The message gives the file's size and the limit and says what makes room: removing
  ended entries or collection games. A write that does not make the file larger is always
  accepted, so the user can make room even when the file is past the limit (after a hand edit or
  a restored backup). Nothing is ever dropped to fit.
- A file over 16 MiB is `damaged`; no write of the server can produce one, only a hand edit.

Why 4 MiB, now that the history is never trimmed: an entry with two claims is about 400 bytes,
so 4 MiB holds about 10,000 entries, more than a game finished or dropped every day for 25
years, next to 200 active ones and room for collections. The file is parsed only when it changes
and written whole on each change; at 4 MiB that is a few tens of milliseconds on a NAS, under the
write lock, and the 50 backups take at most 200 MiB. A larger limit would make every queue change
slower for a history that no household reaches; a smaller one (the 1 MiB first proposed, about
2,500 entries) could be reached in a decade of steady use. `GET` answers only the latest 50
ended entries unless asked for more, so the size of the history does not weigh on the page.

## What the page shows

- **Game page:** a "Play queue" panel under the console switches, with an "Add to queue" button.
  It opens a picker where "Add to queue" is one click and no console is preselected; an optional
  collapsed section, "Also put it on a console", lists the consoles that support the system.
  Before confirming a console, a dry run says what it does: "Turns it on for deck (3.2 GB
  to download)", "Already on machine: it will stay there when you finish it". Once queued, the
  button becomes the entry's state, with "Playing", "Finished", "Dropped" and "Remove from queue",
  and each console's view under it.
- **Home:** an "Up next" section after the consoles and before "Continue playing": the active
  entries in order, each with cover, name, the console it is queued for with that console's
  progress state (from `/api/v1/progress`: progress is per console, not per game, so it says
  "deck: downloading 42%", not the game's own share), a "Playing" mark when shown as playing, the
  RetroAchievements suggestion ("You beat it. Mark it finished?") with "Yes" and "Not now", and
  the repair buttons of `not-on` and `not-released`. Moving an entry uses "Move up" and "Move
  down" buttons, so it works with the keyboard and a gamepad; dragging is not needed. A short
  "Recently finished" list follows, collapsed, with "Show all" asking for the rest of the
  history. When `size` passes 90% of `limit` the section says the queue file is nearly full and
  how to make room. The section is absent when the queue is empty and
  the file is `absent`; a `damaged` or `newer` file shows one line saying so and nothing else.
- **System grid and search:** a small "Queued" mark (or "Playing") on the tiles and search rows
  of queued games, from the queue the page already loaded.
- **Release dialog:** as in [Releasing is asked](#releasing-is-asked-never-automatic).
- **Conflicts:** a `stale` answer reloads the queue and the devices and says the queue changed
  elsewhere, as the switches do.

## Pure decisions and I/O

- **Pure** (`queue.py`): `parse` (bytes to the model, or `damaged` or `newer` with a reason),
  `render` (the deterministic encoding), `resolve`, `shown_state`, `suggestion`, `device_view`,
  `release_change`, `size_check` and `plan`, and the response builders of both routes.
- **I/O** (`queue_store.py`): reading with a stamp, the backups and their rotation, the guarded
  atomic write. `serve`: the routes, the lock and the order of the writes; the whitelist writes
  reuse `whitelist_store`.

Tests: the schema's round trip and every way a file is `damaged` or `newer`; `resolve` with a
present, a re-filed (same system and other system) and a removed game; `shown_state` and
`suggestion` at the edges of their windows and with an award from before the queue; every
operation of `plan`, with `before` `off`, `on` and `partial`, a whole system turned on after
queueing (`covered`), a device that left the config, an unsupported system and a
`needs-migration` whitelist; `device_view` for each row of its table; the API against a fake hub
root (auth, origin, `stale` on the queue and on a whitelist, dry runs, the limits, a write over the size limit refused with nothing written and one that
shrinks a file past the limit accepted, two owners holding one game, entries of another owner
kept byte for byte)
and the failure of each write step (a failure in step 4 writes no whitelist; a failure in step 5
leaves a claim the next `GET` shows as `not-on` or `not-released`; a failure in step 6 leaves the
entry).

## Implementation notes

Where the code settled something this page left open, or reads it more narrowly:

- **The plan is made once.** `POST` reads the queue and the whitelists fresh, plans, then takes
  the lock and compares the `etag` of the queue and of every whitelist the plan looked at, written
  or not (`QueuePlan.reads`), with the one the plan used (step 1); a second plan on identical
  inputs would give the same result, so there is none. The size limit
  is checked last, on the rendered file, because only the result has a size.
- **Held, kept, auto-ended.** Finishing, dropping or removing ends every open claim: the held
  ones as the request says, and the ones that hold nothing (`before` `on` or `partial`) as
  `kept` without being named, so an ended entry never keeps a claim open and blocks another
  owner's release. `keep` may name any open claim; `release` only a held one (`not-held`
  otherwise, also for a game that left the library: its consoles can only be kept). A console
  named in both lists is `bad-request`; `release` and `keep` on any other `state` change are
  `bad-request`.
- **A release that did not reach the whitelist is pending.** A claim ended `released` with
  `before` `off` whose game is still on its console (the view `not-released`) is still owed:
  `remove`, and finishing or dropping an active entry, must name it in `release` (which turns
  the game off again) or `keep`, like a held claim; `add` and `devices.add` over it record
  `before: "off"` again, and a restart through `add` keeps such a claim on the entry. A closed
  claim (released and really off, or `kept`) is closed.
- **`moved` needs a candidate.** The candidates are computed after leaving out the games already
  in the queue; an entry with none left is `missing`.
- **One write when nothing was released.** A `remove` whose claims all end `kept` (or that had
  none) deletes the entry in the first write. A request that changes no entry (a `sync`) writes
  no queue file at all.
- **`devices` and `add` details.** A new claim replaces an ended one for the same console; `add`
  on a console with an open claim is `bad-request`. An ended entry is not changed by `devices`
  (`bad-request`: queue it again first). A restarted entry also clears `ra_dismissed`. A `move`
  takes a position from 0 to the number of the other active entries; `position` for `add` from
  0 to the number of active entries.
- **`relink`** takes any catalog game that has no entry (active or ended) of the owner
  (`already-queued` otherwise) and refuses an entry that is still `present` (`bad-request`).
  **`dismiss`** stores the award's date as a UTC time in the file's form; with no suggestion to
  dismiss it is `bad-request`.
- **A game removed earlier in the same request** cannot be named again in it (`bad-request`).
- **The award's date** is compared to the second, in UTC; a date without a time counts as
  midnight UTC.
- **An unreadable file** (permissions) is `damaged` like a corrupt one. The hub's `catalog/`
  folder must exist for the first write (an unmounted hub root creates nothing); the answer
  names `catalog/`, the log the hub root.
- **The file** is compact JSON with sorted keys; entries of other owners are kept as parsed and
  written back identically.

## The page

Where the page settled something this page left open (all of it is in `data/ui/queue.js`, with
the data and the writes in `store.js` and the tile mark in `components.js`):

- **Every write is one `POST /api/v1/queue`** with the `etag` the page last saw as `expect_queue`
  and, as `expect`, the whitelist `etag` of every console the operations name. A dialog that
  needs numbers sends a dry run first; the write that follows takes the `etag`s current at that
  moment, so a change made elsewhere while the dialog was open is a `stale`, never an overwrite.
  After any error the page loads the queue again; after a change the answer's `queue` replaces
  it. A switch or a lever (`POST /api/v1/changes`) does not touch the queue, but the page asks
  for the queue again afterwards, since the `view` of each claim depends on the whitelists.
- **The picker** makes the plain list the default path: "Add to queue" is one click and no
  console is ever preselected (the main use is a backlog, and a console is optional). The
  consoles are a collapsed section, "Also put it on a console"; opening it and ticking a
  console runs the dry run again at every change of the selection and fills one line under each
  chosen console from the game's state there (`off`: "Turns it on for deck (3.2 GB to
  download)"; `on`: "Already on machine: it will stay there when you finish it"; `partial`:
  "Completes it on machine ... It will stay there when you finish it"). A console that cannot
  take the game (a system it does not run, a whitelist that needs migration, an unknown state,
  a game that cannot be listed, a console the entry already holds) is listed but disabled, with
  the reason. The page keeps no memory of the consoles chosen. The same dialog adds consoles to
  an active entry (`devices`), with the list open since it is the point there, and queues an
  ended game again (`add`).
- **The release dialog** opens when finishing, dropping or removing an entry, or taking one
  console off it, and the queue holds the game on a console the page can turn it off on. It
  runs a dry run with every such console in `release` and draws the answer: a console in
  `skipped` (`covered`, `claimed`) has no checkbox and says why it stays; the others have a
  checked box ("Remove from deck", with the bytes freed and "gone within about an hour"). A
  console the user unticks goes in `keep`. Consoles where the game was there before are listed
  as staying. A console the queue holds but cannot release (a console that left the config, a
  state that is not known, a game that left the library) goes in `keep` without being asked.
  With no console that can be released there is nothing to ask and the request goes straight
  out, with every held claim named in `keep`. A pending console (the view `not-released`, from
  a release whose whitelist write failed) counts as held, so the dialog offers it again.
- **Up next** lists the first 10 active entries and a "Show all N" button for the rest. Each
  entry has "Up" and "Down" (`move`, to the neighbouring position; disabled at the ends), "Finished",
  "Dropped" and "Remove", and under its name one line per console: its progress from
  `/api/v1/progress` when the view is `on`, "not on the console yet" with "Turn on" for `not-on`,
  "still on the console" with "Remove" for `not-released`, and the plain state otherwise. The
  suggestion has "Yes" (the same dialog as "Finished") and "Not now" (`dismiss`). A `moved`
  entry lists its candidates as buttons; one opens a confirmation (what a relink keeps and
  ends) before `relink` is sent. A `missing` entry has only "Remove".
- **Recently finished** is a collapsed list of the first 10 ended entries the page has, with
  "Show all N" (asks for `history=N`, at most 10000, and keeps that number for later loads),
  "Queue again" per game and "Forget" (`remove` of an ended entry), which is the way to make
  room when the file is nearly full.
- **The game page** adds to the entry's state the buttons "Playing" (or "Back to queued"),
  "Finished", "Dropped" and "Remove from queue", "Add a console", and "Take off" on each console
  where the claim is open (the release dialog for a console the queue holds, a plain `keep`
  otherwise). The panel says "N of M" for the position and has no move buttons.
- **There is no page of its own:** the queue is the section of the home page.

## Out of scope

- Users themselves (logins, roles, a token per person): the file carries owners from format `1`,
  but the server knows only `default` until users exist.
- Editing collections, and any whitelist effect of a collection ("send this collection to the
  deck"). The schema is reserved; the routes come later.
- Per-game download progress (see [Progress](/warpzone/development/design/library-ui/#progress)).
- A command-line `warpzone queue`. The file can be read with any JSON tool; only `serve` writes
  it.
- Syncing the queue to consoles, or showing it in a frontend on the console. A Decky plugin can
  read it through the API.
