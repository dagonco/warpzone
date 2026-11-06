---
title: "Library UI"
---

The library UI is a small web application that runs in a container on the hub. It shows the
whole catalog (per system, and with a search that groups the versions of one title across
systems), with the cover, the description and the other data of the ES-DE scrape on the hub,
a RetroAchievements badge, and the state of each game on each console. It lets the user turn a
game or a whole system on or off for one console or for all of them. A JSON API serves the
page and, later, a Decky plugin (a separate project; this page only keeps the API usable for
it).

Underneath, the UI does one thing: it rewrites `library/patterns-<device>.stignore`. It never
deletes, moves or renames anything in the library. What it writes is limited to the device
whitelists, the catalog index (`catalog/`) and the backups of the whitelists it replaced.
Removing a game from a console reaches the console through the normal path: Syncthing delivers
the new whitelist, the console's rescan applies it, and the console's prune timer deletes the
leftovers (see [Removal reaches the console](#removal-reaches-the-console)).

## Decisions

These were decided before the design and are not reopened here:

1. Games and whole systems can be turned on and off. Turning a game off on a console that has
   its whole system adds an exclusion line instead of being refused.
2. The UI owns the whole whitelist file and rewrites it entirely: atomic write, refused when the
   file changed since it was read, paths escaped for Syncthing's ignore syntax. A one-time
   migration converts the hand-written whitelists into the UI's format; the included paths stay
   the same and hand comments are lost.
3. A removed game disappears from the console by itself: `prune --apply` runs unattended from a
   timer on each console, with no size cap, but refuses to delete anything when the run would
   delete more than half of what the device holds in a folder.
4. A console runs every system unless its device table says otherwise (`exclude_systems` in
   the private config, which an enrollment wizard will write; the wizard is a later piece of
   work). The UI does not let the user give a console an excluded system, and `check` reports a
   whitelist that names one.
5. The progress of each console is read on the hub from `GET /rest/db/completion`; no console
   API key is needed.
6. warpzone runs on the hub in a container (the same image planned for running `inbox` on the
   hub).
7. The catalog stores the RetroAchievements hash of each game and a cached copy of RA's game
   lists per console.
8. A catalog index built by `warpzone catalog`, and updated by `inbox` when it files a game, is
   the foundation the UI reads.
9. Games without a scrape show a generic placeholder; nothing else is designed for them yet.
10. Standard library only: `warpzone serve` uses `http.server`, and the page is static HTML, CSS
    and JavaScript with no build step, shipped in `warpzone/data/ui/`.

## The catalog index

### Location and format

The index lives on the hub, in `catalog/` at the hub root, next to `library/` and outside every
Syncthing folder, so no console receives it and the hub's filesystem snapshots cover it (see
[Snapshots on the hub](/warpzone/operation/safety/#snapshots-on-the-hub)):

```
<hub root>/catalog/
├── systems/<system>.json      one file per system folder of library/roms/
├── ra/<console id>.json       RetroAchievements game lists, cached
└── whitelists/<device>/       backups of the whitelists the UI and the migration replaced
```

Each file is UTF-8 JSON written with sorted keys, so the same catalog always gives the same
bytes. One file per system keeps a write small (a scan of one system, or `inbox` filing one game,
rewrites one file) and lets two writers that touch different systems never meet. Every write
goes through `safefile.atomic_write` with the stamp of the file as it was read: a file that
changed in between is read again, the change is applied again, and the write retried (three
times, then a warning). A file whose content would not change is not written. A missing or
unreadable `catalog/` is not an error for the UI: it shows an empty library and says that
`warpzone catalog` has not run.

### Games and parts

A **game** is what the user turns on or off. A **part** is one path below `library/` that the
game needs on a console, written as one whitelist line. Most games have one part; some have
several, and a game is on for a console only when every part is.

```json
{
  "format": 1,
  "system": "psvita",
  "updated": "2026-11-06T10:00:00Z",
  "games": {
    "5d0c1f3a9b2e7c48": {
      "title": "Example Game",
      "key": "example game",
      "primary": "roms/psvita/app/PCSE00000",
      "parts": [
        {"path": "roms/psvita/app/PCSE00000", "type": "dir", "size": 3221225472, "files": 812, "mtime": 1761900000},
        {"path": "roms/psvita/license/PCSE00000", "type": "dir", "size": 512, "files": 1, "mtime": 1761900000},
        {"path": "roms/psvita/Example Game.psvita", "type": "file", "size": 9, "mtime": 1761900000, "sha1": "…"}
      ],
      "gamelist": "./Example Game.psvita",
      "size": 3221225993,
      "added": "2026-10-28",
      "dat": null,
      "ra": null,
      "flags": []
    }
  }
}
```

- `id` (the key of `games`): the first 16 hex digits of the SHA-256 of `primary`. It stays the
  same as long as the primary part keeps its path, which the library never changes (see the
  non-goals in the [design index](/warpzone/development/design/)). The API names games by it.
- `primary`: the part that identifies the game (the rom file, the `.m3u` folder, the PS4 game
  folder, the Vita app folder, the Switch base game).
- `parts`: every path the game needs, in the order the derivation below gives. A `file` part has
  `size`, `mtime` (whole seconds) and `sha1`; a `dir` part has the total `size`, the count of
  `files` and the newest `mtime`, and no hash (a PS4 game folder holds thousands of files and tens
  of GiB, and nothing reads a hash of it).
- `title`: the display title before any scrape (below); `key`: its [title key](#title-key).
- `gamelist`: the path the ES-DE gamelist of the system uses for this game, which is how its
  metadata and media are found.
- `size`: the sum of the parts. `added`: the date `inbox` filed it, or the date a scan first saw
  it; a later scan keeps it.
- `dat`: the DAT game name when `inbox` identified the game by DAT, else `null`. A scan never
  looks DATs up (the hub has none, and a CHD's hash is not the DAT's).
- `ra`: see [RetroAchievements](#retroachievements).
- `flags`: `unlistable` when a part cannot be written as a whitelist line (see
  [Escaping](#escaping)); the UI shows such a game and refuses to change it.

### How parts are derived

`catalog.derive(system, listing)` is pure: it takes the stat listing of the system's folders
(name, type, size, mtime; the text of `.desktop`, `.psvita`, `.cue`, `.gdi` and `.m3u` files,
which are small) and returns the games. The rules, by system:

- **Default.** Each entry of `roms/<system>/` is a game, except hidden names, the junk names of
  `patterns-common.stignore`, `*.part` files, and EmuDeck's `systeminfo.txt` and `metadata.txt`.
  A directory whose name ends in `.m3u` (the multi-disc layout `inbox` writes) is one game with
  one part, the folder; so is a `.ps3` directory in `ps3`. Any other directory is walked, and
  each entry in it follows the same rule. In a folder, a `.cue`, `.gdi` or `.m3u` file claims the
  files it names as parts of its game (`inbox.resolve_refs`); every other file is a game.
- **`ps4`.** Each folder `roms/ps4/games/<CUSA>/` is a game. Its parts are the folder and every
  `roms/ps4/shortcuts/*.desktop` whose `Exec=` path ends in `/games/<CUSA>/eboot.bin`. The title
  is the shortcut's name; without one it is the `CUSA` id. A `.pkg` left in `roms/ps4/` (filed
  without `orbis-pkg-util`) is a game of one part. `gamelist` is the shortcut.
- **`psvita`.** Each folder `roms/psvita/app/<TITLEID>/` is a game. Its parts are the folder,
  `roms/psvita/license/<TITLEID>/` when it exists, and every `roms/psvita/*.psvita` whose content
  is that id. The title is the launcher's name. `gamelist` is the launcher.
- **`switch`.** Each file of `roms/switch/` is a game. A folder of `extras/switch/updates/` or
  `extras/switch/dlc/` is a part of the base game whose TitleID its files carry (`[<id>]` in the
  name; the base of an update is its id minus `0x800`, of a DLC `(id & ~0xFFF) - 0x1000`, as in
  [Switch games](/warpzone/development/design/inbox/#switch-games)). A folder that matches no base
  game in the library is listed by the API as unclaimed and has no toggle.
- **`xbox360`, `wiiu`, `model2`.** The games are the entries of `roms/<system>/roms/`, by the
  default rule. The rest of the system folder (Xenia's content, Cemu's `mlc01`, Model 2's
  emulator files) is not part of any game.

The **system roots** are the paths a whole system is whitelisted by (`catalog.system_roots`,
pure): `roms/<system>` by default, `roms/<system>/roms` for `xbox360`, `wiiu` and `model2`, and
for `switch` the three paths `roms/switch`, `extras/switch/updates` and `extras/switch/dlc`.

### Title key

The title key groups the versions of a game across systems, so a search for "metal gear" shows
one card with the Game Boy Color and the PlayStation versions of *Metal Gear Solid*.
`catalog.title_key(title)` is pure:

1. Remove every `(...)` and `[...]` group (regions, languages, disc numbers, ids, versions).
2. Decompose (NFKD), drop combining marks, case-fold.
3. Remove apostrophes, write `&` as `and`, turn every other character that is not a letter or
   a digit into a space.
4. Collapse spaces, drop a leading `the` and a `the` that ends the title or comes right before
   a subtitle (the No-Intro form `Legend of Zelda, The - ...`).

The title comes from file names, never from a scrape, so the key is stable and exists for
unscraped games. Roman numerals and digits are not unified (`vii` and `7` are different keys).

### Building and updating it

`warpzone catalog` walks `library/roms/` and `library/extras/switch/` under the hub root,
derives the games of each system and writes the system files that changed. It is incremental:
a `file` part keeps its stored `sha1` while its size and mtime are unchanged, so after the
first run a scan only stats. Games whose primary part is gone are dropped; the fields only
`inbox` knows (`dat`, `added`, `ra`) are kept for games that stay. It is convergent: it writes
only warpzone's own files and accepts `--dry-run`, which prints what would change per system.

`inbox --apply` updates the index after filing. For each system it placed something in, it
lists that system (stat only, hashes reused) and runs the same derivation, then merges what it
knows about what it just wrote: the SHA-1 it already computed, the DAT name, the filing date and
the RA hash. One derivation path serves both writers, so a PS4 shortcut, a Vita licence or a
Switch update filed by `inbox` lands in the right game exactly as a scan would put it there. A
failure to update the index is a warning in the inbox report, never a reason to send an item to
review: the next `catalog` run picks the game up.

### RetroAchievements

A game's `ra` is `null` or `{"hash": "<32 hex>", "of": "<part path>", "size": N, "mtime": T}`:
the RA hash of one part (the primary part; for an `.m3u` folder, its first disc), and the size
and mtime that part had when it was hashed. A part that changed since is hashed again.

- `inbox` writes it when `[inbox] rahasher` is set: it is the hash of the result, which it
  already compares with the source's.
- `warpzone catalog --ra-hash` is the one-time pass for the existing library: it runs RAHasher
  (`[inbox] rahasher`, or `--rahasher PATH`) on every game of a system in `ra.CONSOLE_IDS` that
  has no hash or a stale one. It reads every image once, so on a large library it runs for hours;
  it can be stopped and run again, and it writes each system file as it finishes the system.
- `warpzone catalog --ra-lists` downloads `API_GetGameList` (`i=<console>&h=1`) for each console
  of `ra.CONSOLE_IDS` the library has, with the key from `RA_API_KEY` (never printed, logged or
  put in an error, as in `ra.py`), and writes `catalog/ra/<console id>.json`:
  `{"format": 1, "console": 12, "fetched": "<UTC time>", "games": {"<hash>": {"id": N,
  "title": "...", "achievements": N, "points": N}}}`. A list younger than 7 days is not fetched
  again unless `--force`. A failed download keeps the cached file and is reported by kind only.
  `ra.parse_game_list` also keeps each game's RA `ID` and `Points` for this.

The badge state of a game (`catalog.ra_state`, pure, from `ra` and the cached list) is one of:
`no-hash`, `no-list` (the console is not in `ra.CONSOLE_IDS` or no list is cached), `not-linked`,
`linked` (the hash is RA's but the game has no achievements yet) and `supported` (with the count
and points, and the link `https://retroachievements.org/game/<id>`). The page shows text only;
no RA image or logo is fetched or shipped.

## Metadata and media

The UI reads the ES-DE scrape that is already on the hub, read-only:

- `frontend/es-de/gamelists/<system>/gamelist.xml`, parsed with `artwork.parse_gamelist`. A
  game's entry is the one whose `<path>` equals its `gamelist`. The API passes on `name`, `desc`,
  `rating`, `releasedate`, `developer`, `publisher`, `genre` and `players`.
- `frontend/media/<system>/<kind>/<stem>.<ext>`, where `<stem>` is the gamelist path without
  `./` and without its extension (a folder entry keeps its whole name, as in `artwork.build`),
  for the kinds `covers`, `miximages`, `screenshots`, `titlescreens`, `marquees` and `3dboxes`,
  and the extensions `png`, `jpg` and `jpeg`.

A game with no gamelist entry shows its catalog title, and a game with no cover shows
`data/ui/placeholder.svg`. Unscraped games get nothing more in this phase.

## The whitelist model

### The file the UI writes

A managed whitelist is fully determined by three sets: the **systems** that are on as a whole,
the **includes** (paths that are on: game parts and other paths such as `bios`) and the
**excludes** (paths that are off inside something that is on). `whitelist.render` (pure) writes
them in this order:

```
// Managed by warpzone (library UI). Edit it in the UI; hand edits are refused.
// warpzone-whitelist 1
#include patterns-common.stignore

// Excluded
/roms/psx/Example Game (Europe).chd

// Systems
!/roms/gba
!/roms/psx
!/roms/switch
!/extras/switch/updates
!/extras/switch/dlc

// Games
!/roms/ps4/games/CUSA00000
!/roms/ps4/shortcuts/Example Game.desktop
!/roms/wii/Mario Kart Wii \[RMCE01\].rvz

// Other
!/bios

*
```

- **Excluded** comes first. Syncthing applies the first pattern that matches and a directory
  pattern covers everything under it, so an exclusion before every `!` line always wins. That
  gives the model one rule: a path is on when no exclude covers it and a system root or an
  include does (`whitelist.decide`, pure).
- **Systems** lists each system's roots, systems in name order. **Games** lists the includes
  below `roms/` and `extras/switch/`, **Other** the rest; both sorted by path (code point order
  of the unescaped path). A section with no lines is left out, comment included. The file always
  ends with `*` and one newline.
- The model is kept **normalized** (`whitelist.normalize`, pure): an include covered by a system
  root or by another include is dropped, an include covered by an exclude is dropped, and an
  exclude not covered by any system root or include is dropped, since `*` already leaves it out.
  So there is exactly one file for one set of paths on, and rendering is deterministic.
- Exclusions carry no `(?d)`: what they leave out on a console is deleted by prune, not by
  Syncthing.

### Changes

A change turns one game or one system on or off for one device (`whitelist.apply`, pure, with the
game's parts from the catalog):

- **Game on.** Excludes equal to or below one of its parts are removed; a part that is still off
  gets an include. If an exclude of a parent folder still covers a part (only a migrated file can
  have one), the change is refused with `covered`.
- **Game off.** Includes equal to or below one of its parts are removed; a part still on (its
  system is on as a whole, or a parent folder is included) gets an exclude.
- **System on.** The system's roots are added; its includes and excludes are dropped by
  normalization, so every game of it is on, including games added later.
- **System off.** The roots are removed and so are all the includes and excludes below them.

After a change, `whitelist.effect` (pure) evaluates every part of every catalog game with the old
and the new model and lists the games that turned on and off. The change is applied only when
the target reached the state asked for and no other game changed state (for a system change,
only games of that system); anything else is refused with `cannot-apply` and nothing is written.

The state of a game on a device is `on` (every part on), `off` (none) or `partial` (some;
turning it on or off fixes that), or `unsupported` when the system is in the device's
`exclude_systems`. A system is `all`, `some` or `none` by its games, and the API also says whether it is on
as a whole.

### Escaping

`whitelist.escape` (pure) writes a path as an anchored literal pattern: `/` plus the path with a
backslash before each of `\`, `*`, `?`, `[`, `]`, `{` and `}` (Syncthing's ignore syntax on Linux,
where `\` is the escape character); `whitelist.unescape` reverses it. `inbox`'s whitelist lines
(`inbox_core.whitelist_line`) use the same function. Lines start with `/` or `!/`, so a name that
begins with `!`, `#` or `(?` needs nothing more. A path that holds a newline, a NUL, a carriage
return, or leading or trailing whitespace on any component cannot be written as a line: the
catalog flags its game `unlistable` and the UI never writes it. Matching is case-sensitive, as
Syncthing's is on Linux, so a path is written with the exact case it has on the hub.

### Reading, the conflict check and writing

A whitelist file is **managed** when it parses as the UI's format and renders back to exactly
the same bytes (`render(parse(text)) == text`). The API reports each device's whitelist as
`managed`, `needs-migration` (it exists in another shape, hand-edited included) or `missing`. A
file that is not managed is shown read-only, evaluated with the migration's parser where it can
be, and every write to it is refused with `not-managed` until `warpzone whitelist migrate` has
run: the UI never guesses what a hand edit meant.

Every read hands out an `etag`, the SHA-256 of the file's bytes (`"absent"` for a missing file).
A write names, for each device it touches, the `etag` it was computed from. The server holds one
lock for all whitelist writes and, inside it, for each device:

1. Reads the file again and compares its SHA-256 with the given `etag`; a mismatch refuses the
   whole request (`stale`) before any file is written.
2. Applies the change to the model of what it just read and checks the effect (above).
3. Copies the current file to `catalog/whitelists/<device>/<UTC time>.stignore` (the 50 newest
   are kept per device).
4. Writes the rendered file with `safefile.atomic_write` and the stamp taken in step 1, which
   compares once more right before the rename. Its temporary file sits beside the whitelist for
   the length of the write only.
5. Asks the hub's Syncthing to scan that one file (`POST /rest/db/scan?folder=wz-library&sub=
   patterns-<device>.stignore`) so the devices hear of it at once; a failed scan is a warning,
   since the hub's watcher picks the file up anyway.

A missing file (`etag` `"absent"`) is created, for a configured device that lists `library`.

### Supported systems

A device runs every system of the library unless its table in the private config lists some in
`exclude_systems` (folder names below `library/roms/`, plain names only: lower-case letters,
digits, `-` and `_`, no repeats; `config.load` refuses anything else). The default is an empty
list, so a system that reaches the hub is offered to every console at once. The list is meant
to be written when a console is enrolled, by a wizard that asks which systems to leave out
(planned, not part of this design); until then it is edited in the config.

A write that turns on a game or a system in the device's `exclude_systems` is refused with
`unsupported`, before anything is read from disk. Turning such a thing off is allowed, so a
whitelist that already names one can be cleaned up from the UI. `warpzone check` reports every
whitelist that names an excluded system (`whitelist.unsupported`, pure): a system root, or an
include or exclude below `roms/<system>/`.

### The migration

`warpzone whitelist migrate [device ...] [--root PATH] [--dry-run]` converts each named
whitelist (every configured device that lists `library` when none is named) into the UI's format.
It runs where the hub root is mounted (the container) and is convergent: a managed file is left
alone, and every file it replaces is backed up first, like a UI write. Hand comments are lost;
the backup keeps them.

1. **Parse** (`whitelist.parse_hand`, pure). Accepted lines: blank lines, `//` comments,
   `#include patterns-common.stignore` (once, first), anchored literal patterns with or without
   `!` (`/path`, `!/path`, escapes allowed), and a final `*`. Anything else (an unanchored
   pattern, an unescaped wildcard, `**`, `(?i)` or `(?d)`, another `#include`, a line after `*`, a
   missing `*`) refuses the file with the line numbers: the user rewrites those lines as literal
   paths by hand and runs the migration again.
2. **Drop lines with no effect.** A line whose path is equal to or below an earlier line's path
   is shadowed (the earlier line decides first) and is dropped. An exclusion with no later `!`
   line at its path or above is redundant with `*` and is dropped. Both are listed in the report.
3. **Classify.** When every root of a system appears as an `!` line, the system is on and those
   lines are its roots; every other `!` line is an include, every other line an exclude. Then
   normalize.
4. **Prove.** Walk `library/` on the hub (files and directories, no symlink followed, skipping
   `.stfolder`, `.stversions` and the junk names of `patterns-common.stignore`, which are the same
   on both sides). For every path, evaluate the old lines with first-match semantics
   (`whitelist.decide_lines`, pure) and the new model with `whitelist.decide`. The two sets of
   paths on must be equal. If they differ, the file is refused and the report lists the first
   paths that differ (the one shape the model cannot hold is an include inside an exclusion
   inside another include).
5. **Write** as in [Reading, the conflict check and writing](#reading-the-conflict-check-and-writing),
   with the stamp of the file as parsed in step 1.

`--dry-run` prints the new file and the dropped lines and writes nothing. The exit code is `1`
when any file was refused. The e2e test checks the matcher against Syncthing itself: for a set of
paths and hand-written whitelists, the paths `decide_lines` keeps are exactly the ones that
reach a real device.

## Removal reaches the console

Turning a game off changes only the whitelist on the hub. It leaves the console as follows:

1. The hub announces the new `patterns-<device>.stignore`, and the console receives it.
2. The console's library folder applies it on its next full scan, within `rescan_seconds`
   (default 300, see [Applying later edits](/warpzone/development/design/hub-and-folders/#ignore-files-and-the-bootstrap-problem)).
   Syncthing stops syncing the game and leaves the files.
3. The console's prune timer runs `warpzone prune <device> --apply --unattended` every hour,
   which deletes the files with all the safety rules of [Pruning](/warpzone/development/design/prune/#unattended-runs)
   and no size cap, and refuses to delete anything when that would take more than half of
   what the device holds in a folder.

So a removed game is gone from a console that is on within about an hour and five minutes. Turning
it on again before the prune ran costs nothing: the files are still there and Syncthing finds
them in sync. A removal the half-of-device guard refused stays on the console until the user
runs `prune --apply` by hand.

## HTTP API

`warpzone serve` serves the page and the API from one port (`[serve] listen`, default
`0.0.0.0:8780`) with `http.server.ThreadingHTTPServer`. Responses are JSON (`application/json;
charset=utf-8`) except the static files and the media. Every response carries
`X-Content-Type-Options: nosniff` and the page `Content-Security-Policy: default-src 'self'`. No
CORS headers are sent: a Decky plugin calls the API from its Python backend, not from the
Steam client's web view. A request body is JSON, at most 64 KiB.

The server reads the catalog files, the gamelists, the RA lists and the whitelists from the hub
root and keeps them in memory, reloading a file when its size or mtime changed (checked at most
every 5 seconds). It runs `catalog` in a background thread at start and every 6 hours, and
`catalog --ra-lists` once a day when `RA_API_KEY` is set (the 7-day age rule makes most of those
runs no-ops).

### Authentication

The UI is meant for a home LAN and uses one shared token: `WARPZONE_UI_TOKEN` in the
environment, or the first line of `[serve] token_file`. `serve` refuses to start without one
(exit `2`), and never prints, logs or echoes it.

- The page asks for the token once and sends it to `POST /api/v1/session` (`{"token": "..."}`).
  A right token answers `204` with a cookie `wz_session=<HMAC-SHA256 of the token over
  "warpzone-ui-session">; HttpOnly; SameSite=Strict; Path=/; Max-Age=2592000` (30 days); a
  wrong one answers `401` after one second. The cookie needs no server state, survives a
  restart and stops working when the token changes or when it expires; the next request then
  answers `401` and the page asks for the token again. There is no logout route: the page keeps
  no token and the cookie is `HttpOnly`, so ending a session means clearing the cookie or
  changing the token.
- A client that is not a browser (the Decky plugin) sends `Authorization: Bearer <token>`.
- Every `/api/` route except `POST /api/v1/session` needs one of the two, compared with
  `hmac.compare_digest`; the static files and `GET /healthz` do not. A write authenticated by the
  cookie must carry an `Origin` equal to `http://<Host>`, and every write must be
  `Content-Type: application/json`, which together keep other pages from posting to it.

The traffic is plain HTTP, so anyone who can read the LAN can read the token. Access from
outside the home goes through the user's own reverse proxy with TLS, which is out of scope.

### Endpoints

| Method and path | Answers |
|---|---|
| `GET /healthz` | `{"ok": true, "version": "..."}`, for the container's health check |
| `POST /api/v1/session` | logs in (above) |
| `GET /api/v1/devices` | the devices that list `library` |
| `GET /api/v1/systems` | every system of the catalog, with its state per device |
| `GET /api/v1/systems/{system}/games` | every game of a system, compact |
| `GET /api/v1/games/{id}` | one game in full |
| `GET /api/v1/games/{id}/media/{kind}` | the game's image of that kind, or `404` |
| `GET /api/v1/search?q=...` | games whose title key matches, grouped by key |
| `GET /api/v1/progress` | the [progress state](#progress) of each device |
| `GET /api/v1/catalog` | whether the catalog is built, and the state of the background run |
| `POST /api/v1/changes` | turns games and systems on and off |
| `POST /api/v1/catalog/refresh` | starts a `catalog` run now (`202`; `409` while one runs) |

`GET /api/v1/devices`:

```json
[{"name": "deck", "profile": "steamos-emudeck", "exclude_systems": ["ps4"],
  "whitelist": {"state": "managed", "etag": "sha256:…"}}]
```

`whitelist.state` is `managed` (the UI's format), `needs-migration` (a hand-written file, or one
that cannot be read: every write for the device is refused with `not-managed`) or `missing` (no
file yet: a write creates it, inside an existing `library/`). `etag` is `sha256:<hex>` of the
file's bytes, or `absent` for a missing file; a client echoes it exactly as it got it in
`expect`.

`GET /api/v1/catalog`:

```json
{"ready": true, "systems": 7, "games": 412, "updated": "2026-11-06T10:00:00Z",
 "running": false,
 "last_run": {"started": "2026-11-06T10:00:00Z", "finished": "2026-11-06T10:00:09Z",
              "ok": true, "message": "..."}}
```

`ready` is false while the server has no catalog file to show (a hub where `warpzone catalog`
has not run yet), `running` is true while a background run is in progress, and `last_run` is
`null` until the first run of this server process ends. `updated` is `null` without a catalog. A
page uses it to say "the catalog has not been built yet", to show a running refresh and to
reload when `running` goes back to false.

`GET /api/v1/systems/{system}/games` (one entry per game; all of them, no paging: a system of
several thousand arcade sets is a few hundred KiB):

```json
{"system": "psx", "games": [
  {"id": "5d0c1f3a9b2e7c48", "title": "Example Game", "name": "Example Game", "size": 512000000,
   "added": "2026-10-28", "cover": true, "ra": "supported", "flags": [],
   "devices": {"deck": "on", "machine": "off", "rg351mp": "unsupported"}}],
 "unclaimed": []}
```

A game's state on a device is `on`, `off`, `partial` (some of its parts), `unsupported` (the
system is in the device's `exclude_systems`) or `unknown` (the device's whitelist is
`needs-migration`, so its effect cannot be known). `flags` holds `unlistable` when a part has a
path that cannot be written as a whitelist line (the game cannot be changed from the UI).
`unclaimed` lists the Switch update and DLC folders that no base game claims (empty for every
other system).

`name` is the gamelist's `<name>` when there is one, else the catalog title. `GET /api/v1/systems`
gives per system `{"system", "games", "size", "devices": {"deck": {"state": "some", "whole":
false}}}`, where `state` is `all`, `some` or `none` of the games turned on, `unsupported` or
`unknown` (as for a game) and `whole` says the system has its own line in the whitelist.

`GET /api/v1/games/{id}` adds `parts` (path, type, size), `dat`, `meta` (the gamelist fields
above), `media` (the kinds that exist), `ra` (`{"state", "achievements", "points", "url"}` as
known) and `versions` (the other games with the same title key: id, system, title).

`GET /api/v1/search?q=metal+gear`: the query goes through the same normalization as a title
(without step 1); a game matches when each word of the query is a prefix of a word of its key.
Groups are ranked by key equal to the query, then key starting with it, then the rest, and then
by key; at most 100 groups:

```json
{"query": "metal gear", "groups": [
  {"key": "metal gear solid", "title": "Metal Gear Solid", "games": [
    {"id": "…", "system": "gbc", "title": "Metal Gear Solid", "devices": {"deck": "off"}},
    {"id": "…", "system": "psx", "title": "Metal Gear Solid", "devices": {"deck": "on"}}]}]}
```

`POST /api/v1/changes`:

```json
{"changes": [
   {"device": "deck", "system": "psx", "game": "5d0c1f3a9b2e7c48", "on": false},
   {"device": "*", "system": "gba", "on": true}],
 "expect": {"deck": "sha256:…", "machine": "sha256:…"},
 "dry_run": false}
```

A change without `game` is about the whole system. `"device": "*"` means every device that lists
`library` and whose profile supports the system; the others are reported in `skipped`, not
refused. The changes are grouped by device and applied in order, so one request writes each
whitelist once. The answer:

```json
{"dry_run": false, "devices": [
   {"device": "deck", "written": true, "etag": "sha256:…", "on": ["…"], "off": ["5d0c1f3a9b2e7c48"],
    "bytes_on": 0, "bytes_off": 512000000, "warnings": ["…"]}],
 "skipped": [{"device": "rg351mp", "system": "gba", "reason": "unsupported"}]}
```

`etag` is the new file's for a device that was written and its current one for the rest.
`warnings` appears only when there is something to say, today a failed request to the hub's
Syncthing to scan the new file (the file is written anyway and the hub's watcher picks it up).

With `"dry_run": true` the server validates and computes the effect and writes nothing; the page
uses it to show what a change does before the user confirms it.

### Validation and errors

`library_api.plan_changes` (pure) takes the request, the config, the profiles, the catalog and
the whitelists as read, and returns either the new file for each device or the first error, in
this order: the body's shape; each device, system and game is known (a game must belong to the
system named with it); each device's whitelist is managed; an `expect` is given for every device
the request touches and equals its current `etag`; nothing of an unsupported system is turned on
for a named device; each change applies and its effect is the one asked for. Only then does the
server take the lock and write, re-checking each file as described above. A request that fails
validation writes nothing. If writing one device fails after another was written (a disk error),
the answer is `500` with `write-failed` (or `stale`, when the file changed during the write)
and the per-device results of the files already written, each file being whole either way.

Errors are `{"error": {"code": "...", "message": "...", "device": "...", "game": "..."}}` (the
last two when they apply):

| Status | `code` | When |
|---|---|---|
| 400 | `bad-request` | malformed JSON, a body over 64 KiB, an unknown key, a wrong type |
| 401 | `unauthorized` | no session cookie or bearer token, or a wrong one |
| 403 | `bad-origin` | a cookie-authenticated write from another origin |
| 404 | `not-found` | an unknown device, system, game, media kind or route |
| 409 | `not-managed` | the device's whitelist needs `warpzone whitelist migrate` |
| 409 | `covered` | a part stays off under an exclusion of a parent folder |
| 409 | `cannot-apply` | the change would not reach its state or would change other games |
| 409 | `unlistable` | a part's path cannot be written as a whitelist line |
| 409 | `catalog-running` | `catalog/refresh` while a run is in progress |
| 412 | `stale` | an `expect` differs from the file as it is now |
| 422 | `unsupported` | turning on a system in the device's `exclude_systems` |
| 428 | `expect-missing` | a touched device has no `expect` |
| 500 | `write-failed` | a whitelist could not be written (a disk error); the answer also lists the devices already written |
| 500 | `internal` | an unexpected failure of the server (the log has the exception name, never a token) |
| 503 | `hub-unreachable` | only `progress`: the hub's API did not answer |

## Progress

`GET /api/v1/progress` reads the hub's Syncthing, never a console's: for each device that lists
`library`, `GET /rest/system/connections` (is it connected) and `GET /rest/db/completion?folder=
wz-library&device=<id>` (`completion`, `needBytes`, `needItems`, `remoteState`). The answers are
cached for 15 seconds, since a completion over a large folder costs the hub a walk of its
database. `progress.state` (pure) turns them into one state per device:

| State | When |
|---|---|
| `offline` | the device is not connected |
| `not-shared` | `remoteState` is `notSharing` or the folder is not shared with the device |
| `paused` | `remoteState` is `paused` |
| `pending` | connected, nothing to download, and the device's whitelist was written less than its `rescan_seconds` plus 60 s ago (it has not applied it yet) |
| `downloading` | `needBytes` > 0; with `percent`, the floor of `completion` |
| `done` | connected and `completion` is 100 |

```json
[{"device": "deck", "state": "downloading", "percent": 42, "need_bytes": 31000000000,
  "need_items": 812, "checked": "2026-11-06T10:00:00Z"}]
```

The write time comes from the newest backup in `catalog/whitelists/<device>/` (the server keeps
no other state). Progress is per console, not per game. Before this is relied on, the e2e test
must show how the hub counts files a device ignores: if a whitelisted device never reaches
`completion` 100 because the hub counts its ignored files as needed, `downloading` is computed
instead from `GET /rest/db/remoteneed?folder=wz-library&device=<id>` filtered through the
device's whitelist (`whitelist.decide`), and per-game progress comes with it.

## The container

warpzone runs on the hub in a container, because a NAS's own Python is often older than 3.11. The
image (`docker/Dockerfile`) is a slim official Python image pinned by digest, with warpzone
installed from a release and RAHasher built in a separate stage (the "inbox on the hub" work will
add its tools to the same image). It runs `warpzone serve` as the user that owns the games share.
An example Compose file is `examples/compose.example.yaml`.

| What | Where in the container | Why |
|---|---|---|
| the hub root | the same path as `[hub] root`, read-write | the whitelists in `library/`, `catalog/`; reads `library/` and `frontend/` |
| the config folder | `/config`, read-only, with `WARPZONE_CONFIG=/config/config.toml` | `[hub]`, `[devices.*]`, `[serve]`, `[inbox] rahasher`, the hub API key file |
| `WARPZONE_UI_TOKEN` | environment (or `[serve] token_file` in `/config`) | the UI token |
| `RA_API_KEY` | environment, optional | the RA game lists |
| port 8780 | published on the LAN | the page and the API |

Mounting the hub root at the path the config already names keeps one config valid inside and
outside the container. `serve` refuses to start (exit `2`) when `[hub] root` has no
`library/.stfolder`, so an unmounted share never looks like an empty library. The hub's
Syncthing API must be reachable at `[hub] api_url` from the container: a LAN address works from
a bridge network; when Syncthing listens only on loopback, the container uses the host network.
The image has a health check on `GET /healthz`.

## Pure decisions and I/O

As everywhere in warpzone, a pure function decides and a thin layer touches the disk or the
network:

- **Pure:** `catalog.derive`, `catalog.system_roots`, `catalog.title_key`, `catalog.search`,
  `catalog.ra_state`, the catalog's JSON encoding and merge; `whitelist.escape`, `unescape`,
  `parse`, `parse_hand`, `render`, `normalize`, `apply`, `decide`, `decide_lines`, `effect`,
  `unsupported` and the migration's classification; `library_api.plan_changes` and every
  response builder; `progress.state`; `prune.guard`.
- **I/O:** `catalog_store` (the walk, hashing, RAHasher and the RA download through `ra.py`, the
  guarded writes), `whitelist_store` (reading with a stamp, backups, the guarded write, the hub
  scan, the migration's walk), `serve` (sockets, sessions, the lock, the background runs, the
  caches).

Tests: the derivation on listings of each layout (PS4 with and without its shortcut, Vita with
and without a licence, Switch with updates and DLC and an unclaimed folder, `.m3u` folders,
`xbox360/roms`); the title keys of No-Intro, Redump and Switch names; the round trip
`parse(render(model))`, normalization and every change against `decide`; escaping of every
special character and refusal of unlistable names; the migration on hand-written files
(shadowed lines, redundant exclusions, a file refused for a wildcard, a file refused by the
proof); the API against a fake hub root and the fake Syncthing (auth, origin, `stale`,
`not-managed`, `unsupported`, dry runs, a `*` change with a skipped device); and the progress
states. The e2e test adds the matcher check above and the completion check.

## Out of scope

- Deleting, moving or renaming anything in the library. The UI only decides what each console
  receives.
- Games without a scrape beyond the placeholder, and scraping itself.
- `songs/` and its whitelists.
- Arcade dependencies: a MAME game whose parent or BIOS set is another zip does not list that zip
  as a part. Turning on the whole system is the safe way for arcade systems.
- Per-game progress, unless the completion check above calls for it.
- Users and roles, TLS, and access from outside the LAN.
- The Decky plugin itself; only the API above is kept for it.
- Editing a whitelist by hand once it is managed (it is refused until migrated again), and
  wildcard lines in whitelists.
