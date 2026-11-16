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

Underneath, the UI does one thing for consoles: it rewrites `library/patterns-<device>.stignore`.
What it writes is limited to the device whitelists, the catalog index (`catalog/`), the backups
of the whitelists it replaced and the
[play queue](/warpzone/development/design/play-queue/) (`catalog/queue.json`, with its own
backups). It can also put one of those backups back (see
[History and restore](#history-and-restore)).
Removing a game from a console reaches the console through the normal path: Syncthing delivers
the new whitelist, the console's rescan applies it, and the console's prune timer deletes the
leftovers (see [Removal reaches the console](#removal-reaches-the-console)).

The one thing it does to the library itself is delete a whole game from the hub, on the user's
confirmed request: it moves the game's files, its media and its hub gamelist entry into a trash
at the hub root, where they can be restored for 7 days, and removes the game's whitelist lines
from each console once that console has received the deletion (see
[Deleting a game](#deleting-a-game)). It never renames anything in the library, never moves a
file anywhere but into its trash and back, never deletes part of a game, and never deletes a
save.

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
6. warpzone runs on the hub in a container (the same image `inbox` can run in on the hub).
7. The catalog stores the RetroAchievements hash of each game and a cached copy of RA's game
   lists per console.
8. A catalog index built by `warpzone catalog`, and updated by `inbox` when it files a game, is
   the foundation the UI reads.
9. Games without a scrape show a generic placeholder; nothing else is designed for them yet.
10. Standard library only: `warpzone serve` uses `http.server`, and the page is static HTML, CSS
    and JavaScript with no build step, shipped in `warpzone/data/ui/`.
11. A whole game can be deleted from the hub (0.4.0rc5). It goes to `trash/` at the hub root,
    outside every Syncthing folder, one entry per deletion, restorable from the page and purged
    after 7 days. The whitelist lines of the game are removed by the server, per console, only
    once that console is fully in sync after the deletion. An arcade set that another set of
    the library depends on is refused. Saves are never deleted.

## The catalog index

### Location and format

The index lives on the hub, in `catalog/` at the hub root, next to `library/` and outside every
Syncthing folder, so no console receives it and the hub's filesystem snapshots cover it (see
[Snapshots on the hub](/warpzone/operation/safety/#snapshots-on-the-hub)):

```
<hub root>/catalog/
├── systems/<system>.json      one file per system folder of library/roms/
├── ra/<console id>.json       RetroAchievements game lists, cached
├── ra/games/<game id>.json    the achievements of one RA game, cached
├── ra/user/summary.json       the user's completion progress, cached
├── ra/user/games/<id>.json    when the user unlocked each achievement of one game, cached
├── ra/badges/<name>[_lock].png  badge images, cached
├── inbox/runs/<id>.json       the run record of each `inbox --apply` (the newest 50; see Inbox)
├── cleanups.json              the whitelist lines of deleted games still to remove (see Deleting a game)
└── whitelists/<device>/       backups of the whitelists the UI, the migration and a restore replaced
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
      "serials": [],
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
  looks DATs up (the hub has none, and a CHD's hash is not the DAT's); only the opt-in
  [`catalog --dats`](#dat-serials) pass does, and it fills `serials`, not `dat`.
- `serials`: the serials the DAT lists for the game (Redump's `serial`; a disc listed once per
  serial gathers them all, and a multi-disc game those of all its discs), in file order, or `[]`.
  Set by `inbox` like `dat`, kept by a scan, filled for older games by
  [`catalog --dats`](#dat-serials), and absent in files written before it existed (read as `[]`). A value with commas is split. The [saves index](#saves-per-game) uses them to find a
  Dreamcast game's memory card.
- `ra`: see [RetroAchievements](#retroachievements).
- `flags`: `unlistable` when a part cannot be written as a whitelist line (see
  [Escaping](#escaping)); the UI shows such a game and refuses to change it. `unverified` when
  `inbox` filed the game from `inbox/_unverified/<system>/` because no DAT knows it (see
  [Roms no DAT knows](/warpzone/development/design/inbox/#roms-no-dat-knows)); `inbox` sets it,
  a scan keeps it, and it goes away when `inbox` later records a DAT name for the game.
  Files written before the flag existed simply never have it, and a reader ignores flags it does
  not know.

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
  `roms/ps4/shortcuts/*.desktop` whose `Exec=` path ends in `/games/<CUSA>/eboot.bin`. The
  folders `roms/ps4/games/<CUSA>-UPDATE/` (its update) and `roms/ps4/dlc/<CUSA>/` (its DLC)
  are parts of game `<CUSA>` too, not games; a `<CUSA>-UPDATE` folder whose base game is not
  in the library stays a game of its own. The title
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

### DAT serials

`inbox` records the DAT serials of the games it files. The games that were already in the library
have none, and a scan cannot know them (the hub has no DATs and a CHD's hash is not the DAT's).
`warpzone catalog --dats [--dats-dir PATH] [--force] [--dry-run]` fills them in, as a pass of its
own after the scan, from the DAT folder `inbox` uses (`[inbox] dats_dir`, or `--dats-dir`),
through `inbox`'s parse cache (`dat.DatLibrary`).

- **Which games:** those with no `serials`, in every system whose DATs list serials (in practice
  the Redump ones; the pass does not name any system). With `--force` the others too: a single
  exact match that differs replaces their serials, no match keeps them.
- **Exact identity only** (`catalog.dat_serials`, pure). In this order: the `dat` name `inbox`
  stored, when a DAT game has exactly that name; else the SHA-1 and size of the primary file part
  against the DAT games that have one rom; else, for a single file, its name without the
  extension against the DAT game names (`inbox` names a file after its DAT game, so a single CHD
  filed before `dat` was recorded is found this way); else, for an `.m3u` folder, each disc named
  in the playlist inside it, by exact name against the DAT game names, with the union of the
  discs' serials in disc order, all discs needed. Nothing is
  compared case-insensitively or cleaned up. A key the DATs give two different serial lists is
  not guessed at. A game the DAT identifies but gives no serial (most No-Intro games) is left as it
  is and counted as `no serial in DAT`; one no DAT identifies is left without serials and counted
  as unmatched. The decision is `catalog.identify` (serials, `()` for no serial in the DAT, or
  None), and `fill_serials` sorts the games into filled, had, no serial and unmatched.
- **Writes:** only the `serials` of `catalog/systems/<system>.json`, with the scan's guarded atomic
  write and its changed-since-read check (read again, up to three tries, then a `conflict`). A
  file whose games would not change is not written, and `--dry-run` writes nothing (the DAT parse
  cache, which is not user data, is the one thing a dry run may still write).
- **Output and exit code:** per system, `filled / already had / no serial in DAT / unmatched` (`to
  fill` instead of `filled` in a dry run), `no DAT with serials` for a system whose DATs are
  missing or list none, and the first unmatched games by path (the `no serial in DAT` games are
  only counted). Exit `1` when a system file could not be read or written, `2` for a
  usage error (no usable DAT folder, `--dats-dir` or `--force` without `--dats`), as for the other
  passes; neither unmatched games nor games with no serial in their DAT are a failure.

### RetroAchievements

A game's `ra` is `null` or `{"hash": "<32 hex>", "of": "<part path>", "size": N, "mtime": T}`:
the RA hash of one part (the primary part; for an `.m3u` folder, its first disc), and the size
and mtime that part had when it was hashed. A part that changed since is hashed again.

`ra.CONSOLE_IDS` maps the ES-DE system folder name under `roms/` to RA's console id (the numbers
of rcheevos, which RAHasher and the web API share). It holds the consoles RAHasher can hash and
RA has sets for: the Nintendo, Sony, Sega, NEC, SNK, Atari and Bandai systems, the classic
consoles (3DO, ColecoVision, Intellivision, Vectrex, Odyssey 2, ...), arcade (`arcade`, `mame`,
`fbneo`, `neogeo`, `cps1` to `cps3`: RA files them all under Arcade), and some home computers and
fantasy consoles (`dos`, `msx`, `c64`, `zxspectrum`, `tic80`, ...). When ES-DE has several folder
names for one RA console, each is listed (`genesis` and `megadrive`, `pcengine` and `tg16`,
`ngp` and `ngpc`, ...). Not listed: Wii, Wii U, Xbox and PS3 (no sets), Switch, PS4 and Vita
(RA has no console for them), and consoles RAHasher cannot hash (Atari 5200, Atari ST, Amiga,
CD-i). A system outside the table has no hash, no list and the `no-list` state.

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
and points, and the link `https://retroachievements.org/game/<id>`). The page never loads an
image from RA's servers (its policy forbids it); the badge images it shows are cached on the hub
and served by the API (see below). No RA logo is shipped.

### Achievements and progress

`warpzone catalog --ra-achievements` (and every background run of `serve` that has the key) goes
on from the cached game lists. It is separate from `--ra-lists` so each cache has its own
refresh period, and it never runs on a page load. The web API is called one request at a time
with a pause of half a second, from `ra.py`, through the same injectable fetch as the lists; the
key is never printed, logged or put in an error (a failed call is reported by its kind only, and
three failures in a row stop the section, since a wrong key or a rate limit will not mend
itself). Every file is JSON with sorted keys, written with the guarded atomic write.

| What | Call | Cache | Fresh for |
|---|---|---|---|
| achievements of a game | `API_GetGameExtended` (`i=<game id>`), one per library game with achievements | `ra/games/<game id>.json` | 14 days |
| completion progress | `API_GetUserCompletionProgress` (`u=<user>`, `c=500`, paged), one call per 500 games the user has on RA | `ra/user/summary.json` | 5 hours |
| unlock dates | `API_GetGameInfoAndUserProgress` (`g=<game id>`, `u=<user>`), one per library game the user has played | `ra/user/games/<game id>.json` | until the progress line of the game changes |
| badge images | `https://media.retroachievements.org/Badge/<badge>.png` and `<badge>_lock.png`, two per achievement | `ra/badges/` | for good |

- Achievements: per game `players` and `players_hardcore` (distinct players) and, per
  achievement, `id`, `title`, `description`, `points`, `true_points` (RA's TrueRatio), `badge`,
  the number of players who unlocked it in any mode and in hardcore, `type` (`progression`,
  `win_condition`, `missable`, or null when RA gives none) and RA's display order. The rarity
  shown is those counts over the game's players, computed when answering.
- Progress: the user is `[ra] user` (see [configuration](/warpzone/reference/configuration/#ra)).
  Without it nothing is asked about a user and every other answer is the same. The summary line
  of a game has the unlock counts (any mode and hardcore), the date of the last unlock and the
  highest award (`beaten-softcore`, `beaten-hardcore`, `completed`, `mastered`) with its date, so
  awards need no call of their own. A game's unlock dates are downloaded again only when its line
  changed (counts or last date), and only for games of the library that the user has played. A
  cache of another user is ignored, not reused, and so are unlock dates that do not match the
  game's current progress line or whose game's achievements were not downloaded (the counts of
  the progress line stand in).
- Badges: the name must be letters and digits (at most 20), the URL is built from it and is
  https on `media.retroachievements.org` only, a redirect is an error, the body is at most 256
  KiB, the content type must be `image/png` and the body must start with the PNG signature. A
  file is written only when all of that holds. The badges of the games the user has played come
  first.
- Budget: a first pass over a library with N RA games that has P played games costs
  `N + ceil(G / 500) + P` calls (`G` is the number of games the user has on RA) plus two images
  per achievement. Later passes cost only what became stale: `serve` spends at most 60 API
  calls and 300 images per background run, so a large library fills in over a few days, and
  `catalog --ra-achievements` on the command line has no limit. Every page of the completion
  progress is one call of that budget. The progress goes first, then the achievements, then
  the unlock dates; a progress download the budget cannot finish is dropped whole (the cached
  one stays) and tried again by the next run.
- Not found: a game or an image RA answers `404` for is not a failure and does not count
  towards the three in a row. A game is noted in `ra/games/<game id>.missing` and asked again
  after 14 days; an image is noted in `ra/badges/<name>[_lock].missing` and not asked again.

Pure (`ra_data.py`, and the parsers of `ra.py`): the cache files' encoding, freshness,
`ra_data.game_view` (the merge of a game's achievements with the user's unlocks, the rarity and
the totals) and `ra_data.summary_view` (the records). I/O (`ra_store.py`, `serve`): the calls,
the pauses, the downloads and the guarded writes.

## Metadata and media

The UI reads the ES-DE scrape that is already on the hub, read-only (the one exception is a
deletion, which moves the game's media and gamelist entry into the trash; see
[Deleting a game](#deleting-a-game)):

- `frontend/es-de/gamelists/<system>/gamelist.xml`, read with `artwork.read_gamelist` (a file over 32 MiB is
  refused without being loaded, and the system shows no metadata) and parsed with
  `artwork.parse_gamelist`. A
  game's entry is the one whose `<path>` equals its `gamelist`, or else that path with its
  leading folders dropped one by one (see below). The API passes on `name`, `desc`,
  `rating`, `releasedate`, `developer`, `publisher`, `genre` and `players`.
- `frontend/media/<system>/<kind>/<stem>.<ext>`, where `<stem>` is the gamelist path without
  `./` and without its extension (a folder entry keeps its whole name, as in `artwork.build`),
  for the kinds `covers`, `miximages`, `screenshots`, `titlescreens`, `marquees`, `3dboxes`,
  `physicalmedia` (the scan of the cartridge or disc), `backcovers` and `videos`, and the
  extensions `png`, `jpg` and `jpeg` (`mp4` and `webm` for `videos`; where a stem has more than
  one extension the first in each list wins).

**A frontend that reads a folder below the system's one.** The catalog's `gamelist` is relative
to the hub's system folder (`./shortcuts/Example.desktop`), but a frontend may read a folder
below it (`artwork.FrontendSystem.base`: ES-DE lists PS4 from `roms/ps4/shortcuts/`, pico-8
from `roms/pico-8/carts/`) and then records its entries and names its media without that part
(`./Example.desktop`, `covers/Example.png`). The frontend's system list says which folder, but
it lives on the devices and not on the hub, so the server does not read it: it looks for the
entry under the exact `gamelist`, then under the same path with its first folder dropped, then
the next (`library_api.gamelist_paths`, `game_entry`). The media stem is the one of the entry
found (a `<folder>` entry keeps its whole name); a game with no entry looks for media under the
same paths in the same order. The exact path always wins, so a system whose frontend reads the
whole folder is unaffected. The catalog file does not change. The cost of reading the base this
way is that a game in a subfolder with no entry could take the entry of another game with the
same file name at the top level; the file names `inbox` writes make that unlikely.

The videos stay on the hub. `wz-media` reaches a device that receives the whole folder with
`/*/videos` in its ignores, and a profile with `[artwork]` receives only the kinds it lists,
none of which is `videos` (see [Hub and folders](/warpzone/development/design/hub-and-folders/)).
A device that already held videos keeps the files it has (an ignored file is left alone) and
gets no new ones.

A game with no gamelist entry shows its catalog title, and a game with no cover shows
`data/ui/placeholder.svg`. Unscraped games get nothing more in this phase.

## Saves per game

The game answer says whether the hub holds emulator saves for the game. The hub keeps them in
`saves/<emulator>/<kind>/` (see [Hub, folders and ignores](/warpzone/development/design/hub-and-folders/)),
and the server only reads them: it never writes below `saves/` and never follows a symlink (a link
is skipped, so nothing outside the hub is listed).

`GET /api/v1/games/{id}` adds a `saves` object, always present:

```json
{"data": true, "states": 2, "newest": "2026-11-08T21:14:03Z", "emulators": ["retroarch"]}
```

`data` is true when a battery or memory save was found, `states` counts the save states, `newest`
is the latest modification time of everything found (UTC, `null` when nothing was) and `emulators`
names the hub folders (`saves/<emulator>`) it came from, sorted. A game with nothing found has
`{"data": false, "states": 0, "newest": null, "emulators": []}`; that also covers a hub with no
`saves/` folder. The compact entries of a system's games do not carry it.

Matching is exact and never guesses: no fuzzy or case-insensitive comparison, no partial names.
What cannot be attributed to one game is left out, so `data: false` means "not found", not "the
game has no save".

### What is matched

`saves_index.match_game` (pure) takes the index of the listing, the system and the primary path of
the game, and the DAT serials the catalog holds for it. The catalog records no other game IDs
beyond what the paths carry, so only these are matched:

| Emulator folder | Matched by | Counts as |
|---|---|---|
| `retroarch/saves` | the stem of the primary part (`Game.chd`, `Game.cue` and the `Game.m3u` folder of a multi-disc game all give `Game`, the name RetroArch loads) plus `.srm`, `.rtc`, `.sav`, `.mcr`, `.mcd`, `.eep`, `.fla`, `.sra`, `.mpk`, `.nv` or `.dsv` | save data |
| `retroarch/states` | the stem plus `.state`, `.state<N>` or `.state.auto` (the `.png` thumbnails are not counted) | one state each |
| `melonds/saves`, `melonds/states` | the stem of a `nds` game plus `.sav`; plus `.ml<N>` | data; states |
| `flycast/saves` | an arcade game (`naomi`, `naomi2`, `atomiswave`): the primary file's name **with its extension** plus `_vmu_save_A1.bin`, `.nvmem`, `.nvmem2` or `-main.nvmem` (`game.zip.nvmem`); a `dreamcast` game: its disc's product number (see below) plus `_vmu_save_A1.bin`, or, when the disc header has none, the primary file's name with its extension plus the same suffix | data |
| `vita3k/saves` | the folder `<TITLEID>` equals the folder name of `roms/psvita/app/<TITLEID>` | data |
| `shadps4/saves` | the folder `<user>/<CUSAnnnnn>` equals the name of `roms/ps4/games/<CUSAnnnnn>` (also its `-UPDATE` folder) | data |
| `eden/saves` | the folder `<account>/<profile>/<TitleID>` equals the `[<TitleID>]` of the Switch game's file name (hex, any case) | data |

Flycast keeps port A1's card per game (its `PerGameVmu` option is on by default); the other
ports use the shared `vmu_save_<port>.bin`, which belongs to no game. A console game's card is
named after `settings.content.gameId`, the `product_number` field of the disc's header (10
bytes) with trailing spaces and NULs cut and anything after a NUL dropped, each of ` /\:*?|<>"`
replaced by `_`; an arcade game's after `settings.content.fileName`, the content file's name
with its extension. Warpzone never reads a disc image. For a Dreamcast game it compares the
game's `serials` with the card's name instead, keeping only the letters and digits of both
sides, because Redump spells the same number with other separators (`T-45001D-05` in the DAT,
`T-45001D05` in the header, `T7016D  50` becoming `T7016D__50` in the file name). Any one serial
may match. A disc whose Redump serial is not its header's number (a bare `51064` for
`MK-51064`, or a catalog number such as `952-0158-50`) gets no match from that serial, and
neither does a game with no `serials` (filed before the field existed and not yet filled by
`catalog --dats`, or not identified in a DAT): both show no saves,
which is "not found" like the rest.

RetroArch's save files can sit in a subfolder of the hub folder when the player sorts them by core
or by content folder; two levels are looked into. Two rules keep a stem from being taken for
another game's: a file in a folder named after a system belongs to a game of that system only, and
a file in any other folder (or none) is left out when the same stem is a game of another system
RetroArch runs, so `Same Name.srm` is nobody's when `gb` and `gbc` both have `Same Name`.
RetroArch matching does not apply to `switch`, `ps4`, `psvita`, `ps3`, `wiiu`, `xbox`, `xbox360`,
`model2` and `n3ds`, which standalone emulators run. For the title-ID emulators `newest` is the
latest file below the title's folder (at most 2000 files are looked at); a folder with no file
below it is not a save.

### What is not matched

- **Shared memory cards:** PCSX2 (`Mcd001.ps2`), DuckStation (`shared_card_*.mcd`), Dolphin's GC
  memory cards, Flycast's shared `vmu_save_<port>.bin` (every port but A1) and `dc_nvmem.bin`,
  and RetroArch cores that share one card between games. A card holds many games.
- **Dreamcast cards of games with no `serials`** (known limit): not filed by `inbox` since the
  field existed (`warpzone catalog --dats` fills those it can identify exactly), or with a Redump
  serial that differs from the disc's header number.
- **Emulators that name saves by an ID the catalog does not record:** Dolphin (game ID and Wii
  title), PCSX2 and DuckStation states and per-game cards (serial), PPSSPP (disc ID), RPCS3 (PS3
  title ID), Azahar (3DS title ID), Cemu (Wii U title ID) and Xenia (title ID). They can be added
  when the catalog records the ID.
- **Ryujinx** (numbered save folders that map to a title through a metadata file), **MAME**,
  **ScummVM**, **RMG** and **PrimeHack** (whose folders are Dolphin's).

### The index and its cost

Listing `saves/` for every request would cost a walk per game on a library of 1,000+ games. The
server lists only the folders in the table above (the RetroArch folders two levels down, the
title-ID emulators down to the title folder) and keeps the result as an index: stem to hits, and
emulator and title ID to hits. The index is listed again when it is more than 60 seconds old (the
media listing's interval), under a lock, so concurrent requests share one listing; a request
inside that time is a dictionary lookup, and a change in `saves/` shows within a minute. No file is
opened or read, only listed and `stat`ed. The catalog's stems are kept per catalog load, not per
request.

## The page

The page is static HTML, CSS and JavaScript in `warpzone/data/ui/`, with no build step, and it
only uses what the API above provides. A strict `Content-Security-Policy: default-src 'self'`
applies, so there is no inline script, handler or style attribute, no external font and no
request to another origin; dynamic styling sets CSS custom properties from script.

- **Home:** a "Now showing" carousel of eight games that have art, drawn at random on each
  visit from eight random systems once every system's list was loaded, the state and download progress of
  each console (from `/api/v1/progress`) with its disk as a bar of used and free space (from
  `disk`; nothing for a console that reported none), a "Continue playing" row (from
  `/api/v1/recent`: cover, name, the console that played it last and when), the two
[recommendation shelves](#recommendations) from `/api/v1/recommendations` ("Unfinished", with
when the game was last played, its play time and its achievements, and "Because you play X",
with the genres, developer and year it shares with X; each is hidden when empty), the systems with
  their coverage per console, the newest games and, at the very end and with little weight, a
  records panel: the RetroAchievements points, games played, beaten and mastered and the
  achievements unlocked (from `/api/v1/ra/summary`), the share unlocked per system, and the most
  played games (from the `play` totals of the system lists). Each part is absent when its data
  is: no play history, no `[ra] user`, nothing downloaded yet.
- **System:** the game grid with an insert or eject switch per console on each tile (and, from
  the `play` totals and `region` of the list, a star for a favourite, a mark with the number of
  plays and a region chip), and a
  NONE, SOME or ALL lever per console for the whole system. A lever asks for confirmation with
  the dry run of the change (games that change and size). Turning a system on overrides its
  exclusions, and the dialog says so.
- **Game:** the main art area plays the video (`videos`: muted, with controls, `preload="none"`
  and a screenshot as poster, so nothing is downloaded until it is played) and falls back to a
  screenshot or mix image when there is none, or when the browser cannot play it; the switcher
  offers the video, the cover, the mix image, the screenshot, the title screen and the back
  cover. A refresh of the page never rebuilds the media column, so a video keeps playing. Under
  the title: the regions and language codes (`region`, `languages`) and a "Verified" seal when
  `verified` is true. Then the cartridge or disc scan (`physicalmedia`) and the 3D box when the
  scrape has them, a switch per console and for all of them, the play history (plays, last
  played and playtime in all, and per console with its favourite star), the saves the hub holds
  (battery save, number of states, last save, emulator; nothing when none was found), the
  metadata, and the achievements wall. The wall draws the badges the API serves (the unlocked
  image for an unlocked achievement, the locked one for the rest, and a plain tile where the
  image answers `404`), with title, description, points, rarity, a hardcore mark and the unlock
  date, a progress bar and the award (beaten, completed or mastered). It shows only what is
  known: with `available: false` it says the list is not downloaded and shows the totals; with
  `progress: null` it lists the achievements without marking any; with `dates: false` it shows
  the counts from the summary and leaves the tiles unmarked. The filter (all, unlocked, locked)
  exists only when the dates are known, and a long list shows the first 48 until asked. At the
  very end, set apart, a "Delete from the hub" button opens the
  [deletion dialog](#the-dry-run-and-the-confirmation).
- **Trash:** reached from the header, from `GET /api/v1/trash`: one row per entry (title,
  system, date of the deletion, size, days left, and a mark on an incomplete entry) with
  Restore and Delete now, each behind a confirmation (Delete now names the game and the size it
  frees; Restore names the game and its size and says which consoles will download it again,
  those with a pending row for the entry in the same answer, or that none does and the game
  comes back off everywhere; its button is disabled once pressed, so a double click sends one
  restore), and below them the consoles that still
  have whitelist lines of deleted games, with what they wait for (see
  [The trash](#the-trash)).
- **Search:** results grouped by title key across systems, with the same per-console switches.
- **Play queue:** an "Up next" section on the home page, a "Play queue" panel on the game page
  and a "Queued" mark on the tiles and search rows of queued games, all from
  `GET /api/v1/queue`; see [Play queue](/warpzone/development/design/play-queue/#what-the-page-shows).
- **Consoles:** the state of each console and what it holds.
- **Inbox:** reached from the header, where a dot shows when the newest run record is newer than
  the last one this browser opened (its id is kept in `localStorage`, read and written inside
  `try`/`catch`, and also in memory: where storage is blocked the dot clears for the visit). From
  `GET /api/v1/inbox`: the time of the last check and any error, the newest run (its placed games
  link to their game pages, where the usual switches turn them on for a console; a game the
  catalog does not have yet is plain text), the whitelist lines as text, the items for review with
  their reasons and where the original is, the held and waiting items, and the older runs, each
  opened with `GET /api/v1/inbox/runs/{id}`. The page polls the answer once a minute. It only
  reads; see [the unattended inbox](/warpzone/development/design/inbox-unattended/#page).

What the API does not provide is not drawn, not even as a placeholder (no high-score table, no
attract mode), and a piece whose data is missing is left out rather than shown empty. The side
rail and the Systems page list the systems grouped by maker (Nintendo, Sony, Sega, Microsoft,
Atari, NEC, SNK, Bandai, Commodore, then Other for a system the page does not know, by name,
and Arcade last), by first release year within a maker; the rail heads each group with the
maker's name. Long system names are shortened to a code
(for example GBA) with the full name in a `title` attribute. On a phone (390 px and up) the
navigation is a bar at the bottom and the console switches sit within thumb reach. The page keeps
keyboard and gamepad navigation, visible focus and `prefers-reduced-motion`, and a toggle turns
the subtle CRT overlay off (kept in a cookie).

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

// Folders
/roms/ps4/games/*
!/roms/ps4/games
/roms/ps4/shortcuts/*
!/roms/ps4/shortcuts
/extras/switch/*
!/extras/switch
/roms/ps4/*
!/roms/ps4
/roms/wii/*
!/roms/wii
/extras/*
!/extras
/roms/*
!/roms

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
- **Folders** comes last, right before the `*`: for every folder above a path that is on and
  that is not on itself (`whitelist.parents`, pure), deepest first, `/<folder>/*` and then
  `!/<folder>`. Everything below such a folder has already been decided by the lines above, so
  the first line keeps the rest of the folder out, as `*` did, and the second delivers the
  folder itself and nothing in it. Without the section Syncthing creates the folder for the
  files it holds (a console that receives one game of `roms/ps2` needs `roms` and `roms/ps2`)
  but records it as a local change of the console, which the hub counts as needed forever: the
  console reads as never fully in sync. The section is derived from the model, so `parse` skips
  its lines (after checking that each is `/<folder>/*` or `!/<folder>`) and `is_managed` proves
  it, since a file whose section differs by one byte from the one `render` writes does not
  render back to itself. It changes which files arrive for no path: only the folders it lists
  go from ignored to delivered, as directories. One visible effect: a console whose whitelist
  includes a game the hub no longer has receives the empty folder of that game's system
  (`roms/gc`), which ES-DE hides. The same rule covers a folder that is not a system, such as
  `extras` and `extras/switch` for the Switch updates and DLC, and the system folder of Xbox 360,
  Wii U and Model 2, which are whitelisted by their `roms/` subfolder.
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
- **Lines of a deleted game** (`whitelist.drop_lines`, only for
  [the cleanup](#whitelist-cleanup)). The includes and excludes recorded for the game when it
  was deleted are removed where they are still in the model; nothing is added, since the paths
  no longer exist on the hub. `set_game` is not used: turning a game off adds an exclude under
  a system that is on, which would leave a line for a path that is gone.

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
run: the UI never guesses what a hand edit meant. A file written by 0.4.0rc1 or rc2 has no
Folders section, so it no longer renders back to itself and reads as `needs-migration` too: one
`warpzone whitelist migrate` rewrites it (the proof compares the same paths as for any hand file,
since `decide` is unchanged), and until then the console keeps receiving exactly the files it did.

Every read hands out an `etag`, the SHA-256 of the file's bytes (`"absent"` for a missing file).
A write names, for each device it touches, the `etag` it was computed from. The server holds one
lock for all whitelist writes and, inside it, for each device (the slow reads of the catalog and
the hub happen before the lock is taken, and the questions to the hub's Syncthing after it is
released, so a slow or down Syncthing never holds up a write):

1. Reads the file again and compares its SHA-256 with the given `etag`; a mismatch refuses the
   whole request (`stale`) before any file is written.
2. Applies the change to the model of what it just read and checks the effect (above).
3. Copies the current file to `catalog/whitelists/<device>/<UTC time>.stignore` (the 50 newest
   are kept per device). A file that did not exist leaves an empty backup: the time in the name
   is what [progress](#progress) reads as the time of the write, so a whitelist the UI creates is
   `pending` like any other edit.
4. Writes the rendered file with `safefile.atomic_write` and the stamp taken in step 1, which
   compares once more right before the rename. Its temporary file sits beside the whitelist for
   the length of the write only.

Once the lock is released, the server asks the hub's Syncthing to scan each file it wrote
(`POST /rest/db/scan?folder=wz-library&sub=patterns-<device>.stignore`) so the devices hear of
it at once; a failed scan is a warning, since the hub's watcher picks the file up anyway.

A missing file (`etag` `"absent"`) is created, for a configured device that lists `library`.

### History and restore

Every write above keeps the file it replaced, so a device's whitelist has a history: the 50
newest backups in `catalog/whitelists/<device>/`, named by the UTC time of the write
(`20261106T100000.123456Z.stignore`). A restore writes one of them back. It is a whitelist write
like any other, so everything in [Reading, the conflict check and writing](#reading-the-conflict-check-and-writing)
holds: the same lock, the `etag` of the file as the caller saw it, a backup of the file being
replaced (so a restore can itself be undone, and it is a write for [progress](#progress)), the
atomic write that re-checks the stamp, and the scan after the lock is released. Nothing is
restored from a path the caller gives: a backup is named by its exact file name, which must
match the pattern above and exist in that device's folder.

The decisions are pure (`whitelist_history`, and `library_api.plan_restore` on top of it):

- **List.** The backup names, newest first; a file in the folder that is not named like a backup
  is not one. Each is compared with the current file.
- **Compare.** The paths the two files name, read as lines (`!/path` turns a path on, `/path`
  leaves it out; comments, `#include` and `*` are not paths). The paths a restore turns **on**
  are those with a `!` line in the backup and none in the current file, plus those the current
  file excludes and the backup does not; the paths it turns **off** are the reverse. This is a
  comparison of lines, not of files: a path below a folder that is on is not looked up. For
  managed files, the API also gives the games it turns on and off and the bytes (`on`, `off`,
  `bytes_on`, `bytes_off`, from `whitelist.effect` on the two models; null when either file is
  hand-written and unreadable). The unified diff of the two texts goes with it.
- **Warn.** A restore that turns paths on says so: the console downloads them again, and they
  may be games or systems that were taken off it since.
- **Refuse.** An empty backup is the file as it was before the whitelist existed (the write that
  created it left an empty backup, to record the time). Written back it would be an empty ignore
  file, which lets every file through, so it is listed but cannot be restored (`not-restorable`).
- **Same file.** A backup equal to the current file writes nothing and makes no backup.

A backup of a hand-written whitelist (from before [the migration](#the-migration)) can be
restored. The UI marks it (`managed: false`) and the file is not managed again until the next
`warpzone whitelist migrate`. So is a managed backup from before the Folders section existed
(a file written by 0.4.0rc1 or rc2). The comparison of a restore ignores the Folders section:
its lines are derived, so they are never read as a path turned on or off.

`warpzone whitelist restore <device> [backup]` does the same from the command line, where the hub
root is mounted. Without a backup it lists them, newest first, each with its time and how it
differs from the current file. With one (its name, or the start of it when that is enough to
tell it from the others) it prints the unified diff, the paths that turn on and off and the
warning, then writes; `--dry-run` stops before the write. It refuses, and exits 1, when the
file changed since it read it.

In the page, each console on the Consoles view has a History button. It opens a dialog with the
backups, newest first; picking one loads its diff, the games it would turn back on and off and
what that downloads or frees, and a Restore button that sends the `etag` the diff was computed
from. A whitelist that changed in the meantime is refused (`stale`) and the dialog shows the
list again.

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

## Deleting a game

Turning a game off keeps it on the hub. Deleting it takes it out of the library: its files go
to the [trash](#the-trash), the hub's Syncthing announces the deletion, every console that
receives the game deletes it as part of the normal sync, and the server removes the game's
whitelist lines from each console afterwards. The order matters and is the one the manual
procedure follows (`.claude/skills/edit-whitelist/SKILL.md`, "Remove a game"): **the hub first,
the whitelist lines last, per console.** A line removed before the console has received the
deletion makes Syncthing ignore the path there, so the deletion never reaches it, and prune does
not help: it deletes only files the hub still has. The console would keep its copy forever.

### What a deletion removes

A deletion is about one catalog game as a whole, as `catalog.derive` groups it, never about a
part of one (one disc, one update, one DLC):

- **Its parts**, every path of `parts` below `library/`: a Switch base game with the
  `extras/switch/updates` and `extras/switch/dlc` folders its TitleID claims, a PS4 game folder
  with its `-UPDATE` folder, its DLC folder and its shortcuts, a Vita app with its licence and
  `.psvita` launcher, an `.m3u` folder with its discs and `.sbi` files, a PS3 folder, an entry of
  `roms/<system>/roms/` for `xbox360`, `wiiu` and `model2`, or the one file of most games.
- **Its media**: for every kind folder that exists under `frontend/media/<system>/` (not only
  the kinds the UI reads, `library_api.MEDIA_KINDS`, so `manuals`, `fanart` or a kind ES-DE adds
  later never leave files behind) and each stem the lookup tries (`media_stem` of every path of
  `gamelist_paths`, in the folder form and the file form), every regular file
  `frontend/media/<system>/<kind>/<stem>.<ext>` whatever its extension: a file whose name without
  its last extension is the stem, compared exactly (so the stem `Game` takes `Game.png` and
  `Game.pdf` but not `Game.m3u.png`, which is the folder form's). A stem that another catalog game
  of the same system resolves to (through its own `gamelist_paths`) is left out, so the shorter
  paths the lookup tries never take another game's art. A link, or a file in a kind or system
  folder that is a link, is never moved or followed (see
  [Media and byte ranges](#media-and-byte-ranges)); it is named in `warnings`.
- **Its hub gamelist entry**: every `<game>` or `<folder>` block of
  `frontend/es-de/gamelists/<system>/gamelist.xml` whose `<path>` is one of the game's
  `gamelist_paths` and not another game's, removed as text (`gamelist.drop_entries`, pure, on the
  block parser of `update_entry`). The file is read under the 32 MiB limit of
  `artwork.read_gamelist`, written through `safefile.atomic_write` (a temporary file renamed
  over it, and refused when the file changed since it was read, in which case it is read and
  edited again, up to three times). The file is stamped (size, mtime, sha256) before it is read
  and the text edited is exactly the bytes that stamp hashed: a file that Syncthing replaced
  in between is read again, never written back from the old text. The file as it was is first copied to
  `tools/logs/gamelist-backups/<system>-gamelist-<YYYYMMDD-HHMMSS>.xml`, as `inbox` and `scrape`
  do (`inbox_mame.backup_path`; outside every synced folder). A name already taken gets a `-2`,
  `-3` suffix, so two edits in one second never share a backup. The removed blocks are kept in the
  entry's manifest for a restore. A gamelist that cannot be read or written, or whose backup
  cannot be named or made (any `OSError` of the edit), leaves the entry in
  place with a warning, and the deletion goes on: ES-DE does not show an entry whose file is
  missing.
- **Its catalog record**: the server takes the game's record out of its system file
  (`catalog_store.remove_game`: nothing is walked or hashed, and only that record changes), and
  then asks for a background run (`Refresher.request()`). A request while a run is in progress
  makes one more run start when that one ends, since the run in progress may have listed the
  system before the move. A restore puts the record back (`catalog_store.put_game`); a record of
  the same id that a run made in the meantime keeps what it has and gains what only the old
  record knew (the day it was added, the DAT name, serials, the RA entry, flags, the SHA-1 of a
  part whose size and mtime are the same).

Never removed: saves (nothing below `saves/` is touched; the dialog shows what the hub holds for
the game, from [Saves per game](#saves-per-game), and says it stays), BIOS files, the parent or
BIOS set of an arcade game (below), an unclaimed Switch folder, the folders above the parts (an
emptied folder stays), and the play queue (an entry of the deleted game resolves as `missing`,
or `moved` when another version exists; see
[Game references](/warpzone/development/design/play-queue/#game-references)).

### Refusals

The plan (`trash.plan_delete`, pure, from the catalog, the listing of the parts and the media,
the gamelist, the whitelists as read and the arcade sets) refuses, in this order:

1. An unknown game, or one none of whose parts exists on disk: `404 not-found`.
2. A part that is a symbolic link, is not of the type the catalog records (`file` or `dir`), or
   is equal to, inside or above a part of another catalog game, or is a system root itself
   (`roms/xbox360/roms`, `extras/switch/updates`: a line for one is never recorded for
   removal): `409 unsafe-path`, naming the path. A part that is absent is listed in `absent` and skipped; the rest is deleted.
3. A device that lists `library` whose whitelist is `needs-migration`: `409 not-managed`, naming
   it. Its lines could not be found or removed reliably.
4. An arcade set that another set of the library needs (below): `409 has-dependants`, or
   `409 dats-missing` when the server cannot tell.
5. `trash/` that cannot be created, or that is on another filesystem than `library/` or
   `frontend/media/` (`st_dev` differs), so a move would be a copy: `409 trash-unavailable`.
   Nothing is ever copied and then deleted.
6. A pending cleanups file that exists but cannot be read: `500 read-failed`, naming it. A
   deletion whose lines could not be recorded would leave them behind.

And, for the apply only, the hub lock (see [Concurrency](#concurrency)): `409 hub-busy` while
`inbox/.warpzone.lock` exists, whoever holds it and however old it is (a stale lock is for a
person to take over, with `inbox --force-lock`).

**Arcade dependencies.** The catalog does not model parent and clone sets, so the check is made
at deletion time, for a game of an arcade system folder (`mame`, `arcade`, `fbneo`, `neogeo`,
`cps1`, `cps2`, `cps3`), from the arcade DATs `inbox` uses (`[inbox] mame_dat`, `fbneo_dat`,
`mame2003plus_dat`, read with `inbox_mame.load_sets` and its parse cache, and kept in memory while
their size and mtime hold). The set's name is the primary file's name without `.zip`. A
dependant is another zip of the same system folder whose set, in any configured DAT, has that name
in its `cloneof` or `romof` chain or among its devices (`inbox_mame.pool_names`). The refusal names
every dependant (`dependants`: the id, title and file name of each), so the user deletes them
first or keeps the set. Deleting a clone never deletes its parent or BIOS: those are games of
their own, deleted only when asked. With no arcade DAT configured or readable by `serve`, a
deletion in an arcade system folder is refused with `409 dats-missing`, since nothing tells
whether other sets depend on it. `serve` reads the DATs at the paths `[inbox]` names: DATs under
the hub root need nothing more, since the container mounts the hub root at its own path, and only
DATs outside it must be mounted read-only into the `serve` container at the same paths (see
[the hub container guide](/warpzone/setup/hub-container/#deleting-a-game-from-the-ui)).
A set no DAT knows has no dependants by name and is deleted.

### The dry run and the confirmation

A deletion moves data the user owns out of the library, so it follows the destructive rule of
[Writing commands](/warpzone/development/design/configuration/#writing-commands): it reports first
and acts only when confirmed. The confirmation in the page plays the role of `--apply`.

1. The game page's button sends `POST /api/v1/games/{id}/delete` with `{"dry_run": true}`. The
   answer lists every file and folder with its size (a folder is walked for its total and count,
   no link followed), the parts absent on disk, the media files of every kind folder with their
   sizes, whether a
   gamelist entry is removed, the whitelist lines of each device (as text, `!/path` or `/path`),
   the saves the hub keeps, whether the hub lock is held (`busy`), the purge date, and `expect`:
   `sha256:<hex>` of the canonical JSON (sorted keys, no spaces) of `files`, `media`, `gamelist`
   and `whitelists`.
2. The dialog shows all of it and says what follows: the files go to the trash for 7 days, the
   hub's free space grows only when the entry is purged, every console that has the game deletes
   it, saves stay. Its button is "Move to trash", disabled while `busy` is set.
3. The confirmation sends `{"dry_run": false, "expect": "<expect>"}`. The server plans again
   from what is on disk now; a plan whose `expect` differs (a file changed, a media file appeared,
   a whitelist line was added for the game) is refused with `412 stale`, whose error carries the
   new plan as `plan` (the answer of a dry run), and the dialog shows it. No `expect` is `428 expect-missing`.

### The order of a deletion

Under the hub lock and the server's write lock (see [Concurrency](#concurrency)):

1. Plan again and compare with `expect`. Every refusal applies again.
2. Record the whitelist lines in [the pending cleanups](#whitelist-cleanup), before anything
   moves. A line cannot be removed before the hub has recorded the deletion, so recording first
   is safe, and a server that stops after the moves still knows the lines.
3. Create `trash/<entry>/` and write its manifest with `"state": "moving"` and every path to move.
4. Move each part, then each media file, to the same relative path inside the entry, creating
   the folders there. A move never replaces what is at its target and never copies: a file is
   moved with `os.link` and `os.unlink` (the same inode and mtime; `os.link` refuses an existing
   target atomically), a folder with `os.rename` after a look at its target right before (a
   file system without hard links does the same for files). Right before each move the path is checked again with
   `lstat`: not a link, the type of the plan, and for a file the size and mtime of the plan. A
   path that no longer matches stops the deletion with `unsafe-path` and rolls it back (below).
5. Remove the gamelist entries, with the backup; the removed blocks go into the manifest.
6. Rewrite the manifest with `"state": "complete"`, then update the catalog.

Then, with the locks released, the server asks the hub's Syncthing to scan what changed, so the
deletion is announced at once: `POST /rest/db/scan?folder=wz-library&sub=<part>` for each part,
`wz-media` with `sub=<system>/<kind>/<file>` for each media file, and `wz-esde` with
`sub=gamelists/<system>/gamelist.xml`. A failed scan is a warning; the hub's watcher picks the
change up anyway, and the cleanup waits for the hub's index in any case.

**A move that fails halfway** is rolled back, the safer of the two choices: the server moves the
paths already moved back to their places in reverse order, removes the lines it recorded in step
2 and the entry folder, and answers `500 move-failed` with `"incomplete": false`. The library is
as it was. The entry folder is removed only when this deletion made it and it is a real folder
(`lstat`, never a link): if its name turns out to be taken (a link, or a folder made in the
meantime), nothing at that name is touched, the recorded lines go, and the answer is
`500 write-failed`. A move back that fails too leaves the entry and its manifest as they are, and the
error carries `"incomplete": true` and the paths still in the trash. The Trash view shows the
entry as incomplete, and Restore puts back whatever is there. Its recorded lines stay pending,
but the cleanup removes a line only for a path that is gone from the hub (below), so a part that
never left keeps its line. An entry the server finds in `moving` (a server stopped in the middle)
is incomplete in the same way. The 7-day purge never removes an incomplete entry; only Delete now
does.

### The trash

`trash/` sits at the hub root, next to `library/` and `catalog/`, outside every Syncthing folder,
so nothing in it reaches a console, and on the same filesystem as the library, so moving in and
out is a rename. The hub's filesystem snapshots cover it. One folder per deletion:

```
<hub root>/trash/
└── 20261116T101500Z-5d0c1f3a9b2e7c48/       <UTC time of the deletion>-<game id>[-N]
    ├── manifest.json
    ├── library/roms/psvita/app/PCSE00000/   each part, at its path below the hub root
    ├── library/roms/psvita/license/PCSE00000/
    ├── library/roms/psvita/Example Game.psvita
    └── frontend/media/psvita/covers/Example Game.png   each media file, likewise
```

An entry name matches `^\d{8}T\d{6}Z-[0-9a-f]{16}(-\d+)?$` (a second deletion of the same game in
the same second, after a restore, takes `-2`). A route names an entry only by that exact name, and
a folder of `trash/` that does not match is not an entry and is never touched. The manifest is
UTF-8 JSON with sorted keys, written with `safefile.atomic_write`:

```json
{
  "format": 1,
  "entry": "20261116T101500Z-5d0c1f3a9b2e7c48",
  "state": "complete",
  "deleted": "2026-11-16T10:15:00Z",
  "purge_after": "2026-11-23T10:15:00Z",
  "system": "psvita",
  "id": "5d0c1f3a9b2e7c48",
  "name": "Example Game",
  "game": {"title": "Example Game", "key": "example game", "primary": "roms/psvita/app/PCSE00000",
           "parts": ["…as in the catalog file…"], "gamelist": "./Example Game.psvita",
           "size": 3221225993, "added": "2026-10-28", "dat": null, "serials": [], "ra": null,
           "flags": []},
  "files": [{"path": "library/roms/psvita/app/PCSE00000", "type": "dir", "size": 3221225472, "files": 812},
            {"path": "library/roms/psvita/Example Game.psvita", "type": "file", "size": 9}],
  "media": [{"path": "frontend/media/psvita/covers/Example Game.png", "size": 81234}],
  "gamelist": {"path": "frontend/es-de/gamelists/psvita/gamelist.xml",
               "entries": ["<game>\n\t\t<path>./Example Game.psvita</path>\n\t\t…\n\t</game>"],
               "backup": "tools/logs/gamelist-backups/psvita-gamelist-20261116-111500.xml"},
  "whitelists": {"deck": {"include": ["roms/psvita/app/PCSE00000", "roms/psvita/Example Game.psvita"],
                          "exclude": []}}
}
```

- `state` is `moving` (written before the first move), `complete` (after the last) or `restoring`
  (before a restore's first move). The API shows `moving` and `restoring` found on disk outside a
  running request as `incomplete`.
- `game` is the catalog record as the system file held it, so a restore gives back the fields
  only `inbox` knows (`added`, `dat`, `serials`, `ra`, `flags`). `name` is the gamelist name, else
  the title.
- Every `path` is relative to the hub root and names both the original place and, below
  `trash/<entry>/`, the trashed copy. A reader accepts only paths below `library/` or
  `frontend/media/`, with no empty, `.` or `..` component and no leading `/`; a manifest with any
  other path is damaged.
- `media` lists the files moved from every kind folder, whichever kinds the system has, so a
  restore puts back and a purge removes all of them.
- `gamelist` is `null` when the game had no entry. `whitelists` is what was recorded for the
  cleanup, kept for the record only (the cleanup reads [its own file](#whitelist-cleanup)).
- A manifest that cannot be read, or is not this shape, makes the entry `damaged`: listed under
  its folder name, never restored or purged by itself; Delete now removes it.

**Restore** (`POST /api/v1/trash/{entry}/restore`), under the hub lock and the write lock:

1. The entry is looked at again once the locks are held: one that a purge removed in between
   answers `404 not-found`, not damaged. Each path of the manifest is located with `lstat` on
   both sides. A path that exists at its
   original place (a copy filed again by `inbox`, new art from a scrape, a file put back by hand)
   is a conflict: the restore is refused with `409 restore-conflict`, naming the paths, and nothing
   moves. A path in neither place (an incomplete entry) is skipped with a warning.
2. Write the manifest with `"state": "restoring"`, then move each path back (parts first, then
   media), creating missing parent folders, with the same check right before each move. A failure
   moves the restored paths back into the trash and answers `500 move-failed`, as for a deletion.
   A path that appears at its place between the check of step 1 and its move is never replaced:
   it is a conflict like the others (`409 restore-conflict`, naming it, after the paths already
   restored went back into the trash).
3. Put each saved gamelist block back before `</gameList>`, with the backup, unless the gamelist
   already has an entry with that `<path>`, which is kept (ES-DE may have written one back). An
   unreadable gamelist is a warning.
4. Put the saved catalog record (`game` of the manifest) back into its system file, merged into
   the record of the same id when a catalog run already made one (the primary path is the same,
   so the id is too): the merge keeps what that run has and adds what only the old record knew
   (`added`, `dat`, `serials`, `ra`, `flags`). The server does not derive the system itself;
   it asks for a background catalog run, which derives it again from the files that are back and
   corrects what changed. The play queue finds the game `present` again.
5. Cancel the game's cleanups that are still pending: those lines are still in the whitelists, so
   the consoles that had the game receive it again. Lines already removed are **not** added back:
   on those consoles the restored game is off, like a newly added game, and is turned on as usual.
   The answer and the dialog say which consoles are which.
6. Remove the entry folder (by then only the manifest and empty folders), then scan as after a
   deletion.

**Delete now** (`POST /api/v1/trash/{entry}/purge`) and the **automatic purge** remove the whole
entry folder, under the write lock only, since nothing outside `trash/` changes. The entry's path
must be a real directory directly below `trash/` (not a link); it is removed with
`shutil.rmtree`, which does not follow links inside it. This is the one place where warpzone
deletes library files on the hub, and only files a deletion moved there. The background task of
`serve` purges each `complete` entry whose `purge_after` has passed (7 days, `TRASH_DAYS`);
incomplete and damaged entries wait for Delete now. A purge that fails partway answers
`500 purge-failed`. Purging an entry does not touch its pending cleanups.

The folder is first renamed, in one step, to `trash/.purging-<entry>`, and that is what is
removed. A removal that fails partway therefore leaves a folder that is not an entry: the
listing, Restore and the manifest reader never see it, so a half-purged game cannot be restored
with its old catalog record. The next pass of the background task, or Delete now again on the
same entry name, removes what is left; it looks only at real folders directly below `trash/`
and never follows a link.

### Whitelist cleanup

The lines of a deleted game wait in `catalog/cleanups.json` until each console has received the
deletion:

```json
{
  "format": 1,
  "cleanups": [
    {"entry": "20261116T101500Z-5d0c1f3a9b2e7c48", "id": "5d0c1f3a9b2e7c48",
     "system": "psvita", "title": "Example Game",
     "deleted": "2026-11-16T10:15:00Z", "recorded": "2026-11-16T10:15:02Z",
     "paths": ["roms/psvita/app/PCSE00000", "roms/psvita/license/PCSE00000",
               "roms/psvita/Example Game.psvita"],
     "devices": {"deck": {"include": ["roms/psvita/app/PCSE00000", "roms/psvita/Example Game.psvita"],
                          "exclude": []}}}
  ]
}
```

- **What is recorded.** For each device that lists `library`, the includes and excludes of its
  managed whitelist that are equal to or below one of the game's parts (`paths`). A device whose
  whitelist has none (it does not have the game, or has the whole system, whose root line stays)
  is not listed, and a game no whitelist names gets no entry. The file is UTF-8 JSON with sorted
  keys, written with the guarded atomic write under the write lock; a missing file is an empty
  list, and a file that cannot be read stops new deletions (`read-failed`) and the cleanup (logged
  once).
- **When the hub has recorded the deletion.** `recorded` is `null` until the hub's Syncthing has
  the deletion in its index: the cleanup asks `GET /rest/db/file?folder=wz-library&file=<path>`
  for each of `paths` and sets `recorded` to the time of that check once every answer has its
  global record `deleted` (or Syncthing does not know the path). An answer with no global record
  to read (a 200 without a `global` mapping) is not a recorded deletion. Without this, a console
  could read as fully in sync before the hub had announced anything. The check is made again on
  every pass, also for an entry that has `recorded`: a path filed again and deleted again is live
  in the hub's index until it records the second deletion, and `recorded` goes back to `null`
  until it has.
- **When a console has received it.** A device is ready when a reading of the hub taken after
  `recorded` shows it connected, with `remoteState` neither `paused` nor `notSharing`, and
  `GET /rest/db/completion?folder=wz-library&device=<id>` gives `completion` 100, `needBytes` 0,
  `needItems` 0 and `needDeletes` 0 (the questions of [Progress](#progress), asked fresh, not from
  its 15-second cache). `needItems` counts files, folders and links to fetch, not deletions: a
  console whose only pending work is the deletion answers `completion` 95 (Syncthing's cap while
  only deletions remain), `needItems` 0 and `needDeletes` 1, so `needDeletes` must be 0 as well.
  An answer without `needDeletes` is unknown, not ready. `needItems` above 0 does not hold the
  lines when everything else is done and the hub's `GET /rest/db/remoteneed?folder=wz-library&
  device=<id>` (read page by page, at most 1000 names) lists nothing at or below a deleted
  path: a console keeps empty system folders its whitelist does not take (EmuDeck creates
  them), which the hub counts as needed items for ever (a real hub answered `completion` 100,
  `needBytes` 0, `needDeletes` 0 and `needItems` 6, all six such folders). Without that list
  (no answer, or more names than the limit) the device is not ready. A console
  that is offline keeps its lines until it reconnects and finishes syncing, however long that
  takes; a console that never reads as done keeps them, and the Trash view shows it waiting.
- **Removing the lines.** For a ready device, under the write lock: every recorded path that
  exists again on the hub (`lstat` below `library/`: restored, or filed again by `inbox`) is
  dropped from the entry without touching the whitelist, since its line now serves the game that
  is there. The other lines are removed with `whitelist.drop_lines`, the model is normalized and
  the file is written as any whitelist write (read with its stamp, a backup in
  `catalog/whitelists/<device>/`, the guarded atomic write, then a scan of the file). The effect
  check holds: a write that would change the state of a catalog game is not made, and the lines
  stay (logged). A file changed in between is read again on the next pass. A whitelist that became
  `needs-migration` keeps its lines until it is migrated. The device leaves the entry once
  written, and an entry with no device left leaves the file.
- **Devices that leave.** A device no longer in the config, or that no longer lists `library`, is
  dropped from every entry without a write.
- **Cadence.** A background thread of `serve` runs at start and then every 60 seconds
  (`CLEANUP_SECONDS`) while the file or the trash has entries: first the cleanup, then the purge
  of expired entries. A pass that cannot reach the hub's Syncthing does nothing and tries again on
  the next one, and no pass holds the write lock while it asks the hub anything.

A cleanup write is a whitelist write like a change from the page: the device shows `pending` in
[Progress](#progress) for a moment, the backup is in its history, and a client that holds the old
`etag` is refused with `stale` and reads again.

### What reaches the consoles

- **The library.** The hub's `wz-library` announces the deleted paths, and every console that
  receives them (by a game line or by its whole system) deletes its copy as a normal sync, with no
  prune involved. A restore downloads the game again where its lines are still there.
- **Media and gamelists.** `wz-media` and `wz-esde` are two-way, so the media deletion and the
  gamelist edit reach every console that has them, and a restore reaches them back. ES-DE keeps
  its gamelist in memory and writes it when it exits, so a console running ES-DE may write the
  removed entry back to the hub. That is harmless: ES-DE does not show an entry whose file is
  missing and the catalog does not list the game. A profile with `[artwork]` regenerates its
  gamelists from the hub's copy.
- **Saves** stay on the hub and on every console.

### Concurrency

Two locks, always taken in this order:

- **The hub lock**, `inbox/.warpzone.lock`, shared with `inbox --apply` and `scrape`. A deletion or
  a restore is refused with `409 hub-busy` while it exists (an inbox may be filing into the same
  system or writing the same gamelist, a scrape may be adding the game's art), and otherwise takes
  it for its own duration with `inbox_lock.hold(..., mode="attended", task="delete")`, so neither
  can start in the middle; that lasts seconds. The inbox API's `busy.task` is then `delete`, and
  the Inbox view says "The library UI". An unattended inbox or scrape that finds it busy behaves as
  for any live holder.
- **The server's write lock**, the one of every whitelist and queue write, held for the moves, the
  gamelist and catalog writes and the cleanups file, so a change request, a queue write or the
  cleanup never sees a game half deleted. The purge and the cleanup take only this one.

## HTTP API

`warpzone serve` serves the page and the API from one port (`[serve] listen`, default
`0.0.0.0:8780`) with `http.server.ThreadingHTTPServer`. Responses are JSON (`application/json;
charset=utf-8`) except the static files and the media. Every response carries
`X-Content-Type-Options: nosniff` and the page `Content-Security-Policy: default-src 'self'`. No
CORS headers are sent: a Decky plugin calls the API from its Python backend, not from the
Steam client's web view. A request body is JSON, at most 64 KiB.

The server reads the catalog files, the gamelists, the RA lists, the whitelists and the
[play-state ledgers](/warpzone/development/design/play-state/) from the hub root and keeps them in memory, reloading a file when its size or mtime changed (checked at most
every 5 seconds). It runs `catalog` in a background thread at start and every 6 hours, and
`catalog --ra-lists` once a day when `RA_API_KEY` is set (the 7-day age rule makes most of those
runs no-ops). With the key, every run also does a bounded share of `catalog --ra-achievements`
(see [Achievements and progress](#achievements-and-progress)). Another thread runs the
[whitelist cleanup](#whitelist-cleanup) and the purge of the trash every 60 seconds while there
is anything to do.

### Authentication

The UI is meant for a home LAN and uses one shared token: `WARPZONE_UI_TOKEN` in the
environment, or the first line of `[serve] token_file`. `serve` refuses to start without one,
or with one shorter than 16 characters (exit `2`; `openssl rand -hex 32` makes a good one), and
never prints, logs or echoes it.

- The page asks for the token once and sends it to `POST /api/v1/session` (`{"token": "..."}`).
  A right token answers `204` with a cookie `wz_session=<HMAC-SHA256 of the token over
  "warpzone-ui-session">; HttpOnly; SameSite=Strict; Path=/; Max-Age=2592000` (30 days); a
  wrong one answers `401` after a wait (see below). The value is `<expires>.<mac>`: `expires` is
  the Unix time the session ends (the login time plus the `Max-Age`, 30 days) and `mac` the
  HMAC-SHA256 under the token of `"warpzone-ui-session.<expires>"`. The server keeps no state
  and checks both on every request, so the session ends at `expires` even if the browser keeps
  sending the cookie, a changed `expires` breaks the mac, and a cookie made by an older version
  (no expiry) is not accepted. It survives a restart and stops working when the token changes
  or when it expires; the next request then answers `401` and the page asks for the token
  again. There is no logout route: the page keeps
  no token and the cookie is `HttpOnly`, so ending a session means clearing the cookie or
  changing the token. The server is plain HTTP, so the cookie carries `Secure` only when the
  login request came through a TLS reverse proxy: it has an `X-Forwarded-Proto` header whose
  first comma-separated value is `https` (any case, spaces ignored). Without that header the
  cookie is as above and works over plain HTTP on a LAN. The header is believed from any
  client, which is safe for this one use: it can only add the attribute, so a client that
  lies over plain HTTP only stops its own browser from sending the cookie back. The login
  route is the only place that sets the cookie, and the page's script never reads it.
- A client that is not a browser (the Decky plugin) sends `Authorization: Bearer <token>`.
- Optionally, a valid Cloudflare Access token stands for a session, so a browser that came
  through Access is never asked for the UI token (see [Cloudflare Access](#cloudflare-access)).
- Every `/api/` route except `POST /api/v1/session` needs one of the three, compared with
  `hmac.compare_digest` (the Access token is verified, see below); the static files and
  `GET /healthz` do not. The session cookie is
  looked for among all the cookies of the request (other applications on the same host share
  the jar), and every `wz_session` value is compared. A write authenticated by the cookie must
  carry an `Origin` equal to `http://<Host>` or `https://<Host>` (a TLS reverse proxy keeps the
  `Host` header the browser used), and every write must be `Content-Type: application/json`,
  which together keep other pages from posting to it. A write authenticated by an Access
  token needs the same `Origin`: only a write by the bearer token is exempt, because a browser
  never adds it by itself.
- Writes are `POST` only: any other method on an `/api/` route is `404 not-found`.
- A wrong token, at login or as a bearer, is answered `401` after a wait, and the waits are
  shared: failures leave the server one second apart in all, however many connections send
  them, so guessing in parallel gains nothing. A request with no credentials, or the right
  ones, never waits.
- Each waiting failure holds its connection, so at most 8 wait at once. While 8 are waiting,
  every further attempt to authenticate, that is a `POST` to the session route or a request
  with a `Bearer` token, is answered `429` (`too-many-attempts`) with a `Retry-After` header
  (1 second) at once, before the offered token is compared: a right token is refused too, and
  the refusal tells a guesser nothing, so guesses still leave one second apart. Looking at
  the queue, comparing the token and taking a place are one step under a lock, so a burst of
  parallel guesses has at most 8 of them compared and the others are refused unread. A request
  with a valid session cookie is not affected, also when it carries a wrong bearer token next
  to it (the cookie answers, as when the queue is not full), and neither are requests that
  offer no credentials (`401`, as always) or the static files. This keeps a flood of wrong
  tokens from using up the 64 connections the server serves.

#### Cloudflare Access

The UI is often reached from outside the home through a Cloudflare Tunnel with Cloudflare
Access in front. Access has already logged the person in, so asking for the UI token as well
adds nothing. With `[serve] access_team_domain` and `access_aud` set, `serve` accepts the
token Access attaches to every request it lets through as proof of that login. The UI token
stays: `serve` still refuses to start without one, and it is what works for any request that
did not pass Access (the LAN address, a console, a port that is reachable some other way).

- **Keys.** `access_team_domain` is the team's host (`<team-name>.cloudflareaccess.com`, a bare
  host: no scheme, path or port) and `access_aud` is the application's AUD tag (Zero Trust,
  Access controls, Applications, Edit the application, Additional settings, "Application
  Audience (AUD) Tag"). They are set together or not at all: a config with one of them is an
  error, so every command that loads the config fails, not only `serve`. The optional `access_emails` lists the e-mails allowed in; it needs
  both of the others. Nothing else changes when they are absent.
- **Which token.** The `Cf-Access-Jwt-Assertion` header, which `cloudflared` forwards. The
  `CF_Authorization` cookie is never read: Cloudflare does not promise to pass it, and a cookie
  is what a page on another site can make a browser send.
- **Checks** (`access_jwt.py`, pure, standard library only). The token is a compact JWT whose
  three parts are strict unpadded base64url (at most 8 KiB). The header must say `alg:
  RS256` and name a `kid` that is in the team's key set; `none`, the HMAC algorithms and
  every other value are refused before any key is used. The signature is RSASSA-PKCS1-v1_5
  with SHA-256: the signature raised to the key's exponent modulo its modulus must equal,
  byte for byte, the block `00 01 FF..FF 00 || DigestInfo || SHA-256(header.payload)` of the
  modulus's length, compared with `hmac.compare_digest`. Only then are the claims believed:
  `iss` is `https://<access_team_domain>`; `aud` (a string, or a list as Access sends it) holds
  the AUD tag; `exp` is in the future; `nbf` and `iat`, when present, are at most 60 seconds
  ahead; `type`, when present, is `app` (an `org` token is the team's global session, not
  this application's); and with `access_emails`, the `email` claim is one of them, in any case.
  Without `access_emails` the application's Access policy is the only filter on who gets in.
  A key set entry is used only when it is RSA with a `kid`, a modulus of at least 2048 bits
  and an odd exponent of at least 3.
- **Keys of the team** (`access_keys.py`). The key set is `https://<access_team_domain>/cdn-cgi/access/certs`
  (JWKS: `keys` with `kid`, `n`, `e`). It is fetched on the first token, then again after an
  hour and whenever a token names a `kid` that is not in it (Access rotates its key every
  six weeks and keeps the old one valid for seven days), and never more than once a minute,
  so a stream of tokens with made-up `kid`s costs one fetch a minute. The fetch uses `urllib`
  over `https` with a 5 second timeout, follows no redirect and reads at most 256 KiB. The keys
  are kept in memory only. A fetch that fails never grants anything: no key means no
  token is accepted and the request goes on to the UI token. It is logged once per streak of
  failures (the kind of failure, never a token), and again when it recovers. Keys already
  fetched keep working for up to 24 hours when later fetches fail.
- **In the request.** Before the cookie and bearer checks, a request with that header that
  verifies is authenticated as `access`, for reads and writes alike, with no wait. If it
  comes with the right `Authorization: Bearer` (a client that is not a browser, passing Access
  with a service token), the request is a `bearer` one and keeps the bearer's exemption from the
  `Origin` check; the comparison is a plain `compare_digest`, with no wait. A header that
  is absent, does not verify or arrives while Access is not set up counts as no credential at
  all: it costs no wait and takes no place in the queue of failures, because it is not a guess
  at the UI token, and the request is judged by its cookie and bearer as if the header were not
  there. A valid token next to a wrong bearer is accepted
  (as `access`).
- **Why a token was refused.** The reason of a refusal (the wrong audience or issuer, an
  expired token, an unknown key, and so on; never the token) is logged once per reason until a
  token is accepted, so a wrong `access_aud` or team domain, or a clock that is off, shows up in
  the log.
- **The page.** It needs no change. It asks for the token only after an `/api/` answer of `401`;
  with a valid Access token the first calls (`devices`, `systems`, the catalog) are answered,
  so the prompt never opens. If the Access login ends while the page is open, the next call
  has no valid header and the page asks for the UI token as before.
- **What it trusts.** A valid token proves that Cloudflare signed it for the application whose
  AUD tag is configured, so a request that did not pass Access cannot carry one. Like any
  bearer credential it works for whoever holds it until its `exp` (the session duration set
  in Access), so a token copied out of a logged-in browser is as good as that session. The
  server does not check that the request really came from the tunnel: publishing the port only to
  the tunnel's host keeps the home network away from the UI (a container on the same Compose
  network can still reach it), and the UI token still guards the rest. The sign-in is an
  ambient credential, like a `SameSite=Lax` cookie: Cloudflare adds it to every request the
  browser makes to the hostname, so another site can make a browser send a cross-site
  top-level `GET`, which is read-only and whose answer that site cannot read. A write still
  needs an `Origin` equal to the `Host`, which another site cannot give.

The server also bounds what a stranger can hold. A connection that sends nothing for 15
seconds, also in the middle of a body, is closed; at most 64 connections are served at once and
the next ones are closed without an answer. A `Content-Length` that is not a number is `400`.

A body over 64 KiB is answered `400 bad-request`, and the answer must reach the client: closing a
socket that still holds unread bytes makes the kernel reset the connection, and a client that
was still sending would then see a network error in place of the JSON error. So a body of up to
1 MiB (`MAX_DRAIN`) is read and thrown away in chunks of 64 KiB, never held in memory, before the
answer goes out; a client that stalls in the middle of it is dropped as for any other body. A
`Content-Length` over 1 MiB, or of more than 9 digits, is not read at all: the server answers
`400` at once and closes the connection, so the bytes still to come are never taken for a request.
A client that keeps sending may see a reset there; the server spends no memory or time on it.

Receiving a body, the read and the discard alike, has also a total time limit of 60 seconds
(`BODY_SECONDS`, four times the silence limit), because the silence limit alone is a limit on
each read: a client sending one byte just inside it, again and again, would hold a thread for
as long as it liked. The body is read in pieces of at most 64 KiB, one read of the socket at a
time, the limit is checked between pieces, and the timeout of each read shrinks to the time
that remains. When it runs out the server stops reading, logs `body not received`, and closes
the connection without an answer, as for a body that stalls. A client that sends 1 MiB at
18 KiB/s or faster is not affected.

The traffic is plain HTTP, so anyone who can read the LAN can read the token. warpzone itself
does no TLS: access from outside the home goes through a reverse proxy or tunnel with TLS that
the user runs, and that proxy is not part of warpzone. One worked example, a Cloudflare Tunnel
with Cloudflare Access in front (which can also stand in for the token prompt, see
[Cloudflare Access](#cloudflare-access)), is in [Opening the UI from outside the
home](/warpzone/setup/hub-container/#opening-the-ui-from-outside-the-home). What the UI needs
from any such proxy is the two things above: it keeps the `Host` header the browser used (so
the `Origin` check passes) and it sets `X-Forwarded-Proto: https` (so the cookie is `Secure`).
A client that is not a browser, such as the Decky plugin, cannot pass an identity provider's
login, so it keeps using the LAN address (the port stays published to the LAN). Publishing
the port to the loopback only cuts such a client off, unless it goes through Access with a
service token and sends its bearer next to it.

### Endpoints

| Method and path | Answers |
|---|---|
| `GET /healthz` | `{"ok": true, "version": "..."}`, for the container's health check |
| `POST /api/v1/session` | logs in (above) |
| `GET /api/v1/devices` | the devices that list `library` |
| `GET /api/v1/systems` | every system of the catalog, with its state per device |
| `GET /api/v1/systems/{system}/games` | every game of a system, compact |
| `GET /api/v1/games/{id}` | one game in full |
| `GET /api/v1/games/{id}/media/{kind}` | the game's image or video of that kind (with [byte ranges](#media-and-byte-ranges)), or `404` |
| `GET /api/v1/search?q=...` | games whose title key matches, grouped by key |
| `GET /api/v1/ra/summary` | the library's RetroAchievements games and the user's [progress](#achievements) in them, per system and in all |
| `GET /api/v1/ra/badges/{badge}/{variant}` | a cached RA badge image (`variant` is `unlocked` or `locked`), or `404` |
| `GET /api/v1/recent?limit=N` | the games played most recently on any device, newest first |
| `GET /api/v1/recommendations` | the [Unfinished](#recommendations) shelf and the "Because you play" shelf of the home page |
| `GET /api/v1/progress` | the [progress state](#progress) of each device |
| `GET /api/v1/catalog` | whether the catalog is built, and the state of the background run |
| `GET /api/v1/devices/{device}/backups` | the device's whitelist [backups](#history-and-restore), newest first, each compared with the current file, and the current `etag` |
| `GET /api/v1/devices/{device}/backups/{name}` | one backup: its diff against the current file, the paths and games a restore turns on and off |
| `POST /api/v1/devices/{device}/restore` | writes a backup over the device's whitelist |
| `POST /api/v1/changes` | turns games and systems on and off |
| `GET /api/v1/inbox` | the state of the [unattended inbox](/warpzone/development/design/inbox-unattended/#api) and its newest run records, the newest in full |
| `GET /api/v1/inbox/runs/{id}` | one run record in full, or `404 not-found` |
| `GET /api/v1/queue?history=N` | the [play queue](/warpzone/development/design/play-queue/#get-apiv1queue), resolved against the catalog, the whitelists, the play totals and the RA progress |
| `POST /api/v1/queue` | applies a list of [queue operations](/warpzone/development/design/play-queue/#post-apiv1queue), with the whitelist changes they imply, in one request |
| `POST /api/v1/catalog/refresh` | starts a `catalog` run now (`202`; `409` while one runs) |
| `POST /api/v1/games/{id}/delete` | plans a [deletion](#deleting-a-game) (`dry_run`), or moves the game to the trash |
| `GET /api/v1/trash` | the [trash](#the-trash) entries, newest first, and the pending whitelist cleanups |
| `POST /api/v1/trash/{entry}/restore` | puts an entry back in the library |
| `POST /api/v1/trash/{entry}/purge` | deletes an entry for good (Delete now) |

`GET /api/v1/devices`:

```json
[{"name": "deck", "profile": "steamos-emudeck", "exclude_systems": ["ps4"],
  "whitelist": {"state": "managed", "etag": "sha256:…"},
  "disk": {"total": 512000000000, "free": 120000000000, "library": null,
           "at": "2026-11-09T10:00:00Z"}}]
```

`whitelist.state` is `managed` (the UI's format), `needs-migration` (a hand-written file, or one
that cannot be read: every write for the device is refused with `not-managed`) or `missing` (no
file yet: a write creates it, inside an existing `library/`). `etag` is `sha256:<hex>` of the
file's bytes, or `absent` for a missing file; a client echoes it exactly as it got it in
`expect`.

`disk` is what the console last reported about the filesystem that holds its library (see
[Disk space](#disk-space)): `total` and `free` in bytes, `at` the time it was measured (UTC, ISO
8601). `library` is the bytes the library folder uses and is `null` unless a report carries it
(devices do not measure it today; a client that wants the figure sums the sizes of what the
whitelist turns on, which the API already gives). A console that never reported, a report
older than 7 days or dated more than a day ahead, and a file that cannot be read all give
`{"total": null, "free": null, "library": null, "at": null}`; `disk` is always present and never
an error.

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

`region`, `languages` and `verified` are in each entry of the list and in the game's detail.
`region` is the list of regions found in the parenthesised tags of the game's name, as No-Intro
and Redump write them (`["USA", "Europe"]`), and `languages` the language codes of the tag that
lists them, with the first subtag in lower case (`["en", "fr", "zh-Hant"]`). The name read is the
DAT name when `inbox` recorded one (`dat`), else the file or folder name of the primary part.
Tags that are neither (`Rev 1`, `Disc 2`, `Beta`) are skipped, and a name with none gives empty
lists. `verified` is `true` only when `dat` is set, that is, when `inbox` matched the dump to a
DAT by content and recorded the DAT's name in the catalog; the key is absent otherwise, never
`false`, because a game filed before `inbox` kept the DAT name (or by hand) has not failed a
check, it has not been checked.

`name` is the gamelist's `<name>` when there is one, else the catalog title. `GET /api/v1/systems`
gives per system `{"system", "games", "size", "devices": {"deck": {"state": "some", "whole":
false}}}`, where `state` is `all`, `some` or `none` of the games turned on, `unsupported` or
`unknown` (as for a game) and `whole` says the system has its own line in the whitelist.

`GET /api/v1/games/{id}` adds `parts` (path, type, size), `dat`, `meta` (the gamelist fields
above), `media` (the kinds that exist), `ra` (`{"state", "achievements", "points", "url"}` as
known), `achievements` ([below](#achievements)), `versions` (the other games with the same title
key: id, system, title) and `saves` (see [Saves per game](#saves-per-game)).

Play statistics come from the ledgers in `frontend/play-state/*.jsonl` (see [Play
state](/warpzone/development/design/play-state/)), read again when a file changed (checked at
most every 5 seconds); a ledger that cannot be read, or is over 32 MiB (`playstate_device.read_ledger`,
which reads at most one byte past the limit), is left out, and the ledgers of devices that
left the config still count. A ledger line is keyed by hub system and ROM path below it, and a
catalog game is found under its `gamelist` path (what a frontend records), else under its
primary path below `roms/<system>/`. Lines for a ROM the catalog does not hold are ignored.
`GET /api/v1/games/{id}` adds `play`:

```json
{"devices": {"deck": {"replica": "wz-esde", "plays": 3, "last": 1793000000, "fav": true,
                      "playtime": 5400},
             "rg351mp": {"replica": "rg351mp", "plays": 1, "last": 1792000000, "fav": null,
                         "playtime": 0}},
 "totals": {"plays": 4, "last": 1793000000, "fav": true, "playtime": 5400}}
```

`last` is a UTC epoch (null: never, or a time more than a day ahead of the server, which comes
from a wrong clock), `fav` the device's favourite (null: it says nothing) and `playtime` the
seconds its frontend recorded (0: none, also for a frontend that records none). Devices that
read one gamelist (Deck and Machine) share a `replica` and report the same counters, so
`totals` counts a replica once (its highest value) and adds the replicas up; `totals.last` is
the latest and `totals.fav` the favourite with the latest change, as the devices merge them. A
game with no record has `"devices": {}` and `{"plays": 0, "last": null, "fav": null,
"playtime": 0}`. Each entry of `GET /api/v1/systems/{system}/games` carries `play` too: the
same `totals`, or `null` for a game with no record.

`GET /api/v1/recent?limit=N` (`limit`: a whole number from 1 to 100, default 20; anything else
is `400 bad-request`) answers the games with a last played time, newest first, ties by game id:

```json
{"games": [{"id": "5d0c1f3a9b2e7c48", "system": "psx", "title": "Example Game",
            "name": "Example Game", "cover": true, "last": 1793000000, "device": "deck",
            "plays": 4, "playtime": 5400}]}
```

`device` is the device whose ledger has that `last`; two devices of one replica share it, and
the one whose name sorts last is named. `plays` and `playtime` are the totals above.

#### Recommendations

`GET /api/v1/recommendations` answers the two shelves of the home page. It reads only what the
hub already has (the play-state ledgers above, the RetroAchievements cache of
[Achievements and progress](#achievements-and-progress) and the gamelist fields `genre`,
`developer` and `releasedate`) and never makes a request to another machine. A game missing a
piece of data just does not qualify for the rule that needs it. The rules are pure functions in
`recommend.py`; `library_api` collects the data into one candidate per game.

```json
{"unfinished": [{"id": "5d0c1f3a9b2e7c48", "system": "psx", "title": "Example Game",
                 "name": "Example Game", "cover": true, "last": 1790000000, "plays": 3,
                 "playtime": 7200, "ra": {"unlocked": 12, "total": 40}}],
 "similar": {"to": {"id": "a1b2c3d4e5f60718", "system": "snes", "title": "Other Game",
                    "name": "Other Game", "cover": true},
             "games": [{"id": "0f9e8d7c6b5a4321", "system": "gba", "title": "Tiny",
                        "name": "Tiny", "cover": false, "genres": ["Platform"],
                        "developer": "Example Studio", "year": 1996}]}}
```

`unfinished` is a list and `similar` is `null` when its shelf is empty (the page hides an empty
shelf). In `unfinished`, `last` is the UTC epoch of the last activity (below) and `ra` is `null`
for a game RetroAchievements has no achievements for. In `similar.games`, `genres` are the
genres shared with `similar.to` (as the game's own gamelist spells them), `developer` is the
developer when it is the seed's too (else `null`), and `year` is the game's release year when it
is within 5 years of the seed's (else `null`).

What every rule reads, for each catalog game:

- **Play:** the `totals` of [`play`](#endpoints) (plays, play time, last played).
- **RetroAchievements:** the unlocked count (the larger of the softcore and hardcore counts of
  the user's progress line), the achievements available, the highest award and the date of the
  last unlock (read to the day, UTC). It counts only when the cached progress is of the
  configured `[ra] user`; without a user, a cache or a game on RA these are all zero.
- **Gamelist:** `genre` split at commas, slashes and semicolons, `developer` (compared ignoring
  case and repeated spaces) and the first four digits of `releasedate` (a year from 1950 to
  2100; anything else is no year). A game with no genre, no developer, no play record and no
  unlocks is not looked at.
- **Availability:** a game is *on a console* when some console shows it `on` or `partial`;
  else it *fits* when some console that takes the system (`off`, not `unsupported`, not
  `unknown`) has a disk report (see [Disk space](#disk-space)) with at least as many bytes free
  as the game's `size`, and the UI can switch the game (no `unlistable` flag); else it is
  neither. Consoles that never reported a disk contribute only the first case.
- **Activity:** the later of the last play and the last unlock date, ignoring any time more than
  a day ahead of the server (a wrong clock). A game with no date has no activity date.

**Unfinished** lists games the user started and left:

1. *Started:* at least 600 seconds of play time, or at least one unlocked achievement. A play
   count alone does not count (a frontend that records no play time would make every launched
   game look started).
2. *Left:* the activity date is at least 14 days before now. What was touched more recently is
   in "Continue playing".
3. *Not finished:* the user has no award (beaten, completed or mastered) for the game and has
   not unlocked every achievement. If any version of the same title key (another system) is
   finished, none of them is listed.
4. Order: on a console, then fits, then neither; within each, the most recent activity first,
   then the most play time, then the title (ignoring case), the system and the id.
5. One game per title key (the best placed of a title that exists on several systems), at most
   12.

**Because you play X** is one shelf that starts from one game, the *seed*:

1. The seed is the game with the most recent activity that has a genre or a developer, provided
   the activity is at most 90 days old; ties go to the most play time, then title and id. With
   no such game there is no shelf (`similar` is `null`).
2. A candidate resembles the seed when it shares at least one genre (ignoring case) or has the
   same developer. Its score is 3 for each shared genre, 3 for the same developer, and for the
   release year 2 when it is within 2 years of the seed's or 1 when within 5. A year never
   makes a match on its own.
3. Left out: any game with a play, play time or unlocked achievement of its own; any title key
   in which another version has any of those; the seed's title key; and every title key of the
   Unfinished shelf, so the two shelves never show the same title.
4. Order: on a console, then fits, then neither; within each, the higher score first, then the
   title (ignoring case), the system and the id.
5. One game per title key, at most 12. The shelf is shown only when it has games.

The order never depends on the order the catalog was read in, so two requests over the same data
give the same shelves.

#### Achievements

`achievements` of a game is `null` unless RetroAchievements has achievements for it (the `ra`
state is `supported`). Otherwise:

```json
{"available": true, "fetched": "2026-11-09T10:00:00Z",
 "players": 12000, "players_hardcore": 9000,
 "totals": {"count": 40, "points": 400, "unlocked": 12, "unlocked_hardcore": 10,
            "points_unlocked": 120, "points_unlocked_hardcore": 100},
 "progress": {"user": "ExampleUser", "fetched": "2026-11-09T09:00:00Z", "dates": true,
              "award": {"kind": "beaten-hardcore", "date": "2026-10-01T20:00:00+00:00"},
              "profile": "https://retroachievements.org/user/ExampleUser"},
 "list": [
   {"id": 1234, "title": "First Steps", "description": "Finish the first stage.",
    "points": 5, "true_points": 8, "badge": "250336", "type": "progression", "order": 1,
    "rarity": {"players": 11000, "hardcore_players": 8000,
               "percent": 91.7, "hardcore_percent": 88.9},
    "unlocked": {"softcore": "2026-09-30T20:00:00+00:00", "hardcore": null}}]}
```

- `available` is false until the achievement list has been downloaded: `list` is empty,
  `players` is 0 and `totals.count` and `totals.points` come from RA's game list.
- `percent` is the share of the game's players who unlocked the achievement in any mode, and
  `hardcore_percent` the share of its hardcore players who did so in hardcore, with one decimal.
- `unlocked` is `null` for an achievement the user does not have. `progress` is `null` without
  `[ra] user` or before the user's progress was downloaded; `progress.dates` is false while the
  unlock dates of this game are not downloaded yet (every `unlocked` is then `null`, and the
  `totals` unlocked counts come from the summary line, with the points `null`). `award` is `null`
  or `{"kind", "date"}` with `kind` one of `beaten-softcore`, `beaten-hardcore`, `completed`
  (softcore mastery) and `mastered` (hardcore mastery).
- A badge's image is `/api/v1/ra/badges/{badge}/unlocked` or `.../locked`; it answers `404`
  until the image has been downloaded, and a page falls back to a plain tile.

`GET /api/v1/ra/summary`:

```json
{"user": "ExampleUser", "fetched": "2026-11-09T09:00:00Z",
 "total": {"games": 120, "achievements": 3200, "points": 31000, "played": 40, "beaten": 12,
           "mastered": 3, "unlocked": 900, "unlocked_hardcore": 700, "points_unlocked": 8800,
           "points_unlocked_hardcore": 7000, "points_complete": true},
 "systems": [{"system": "psx", "games": 30, "achievements": 800, "points": 8000, "played": 10,
              "beaten": 4, "mastered": 1, "unlocked": 200, "unlocked_hardcore": 150,
              "points_unlocked": 2000, "points_unlocked_hardcore": 1500,
              "points_complete": true}]}
```

It counts the games of the library that RA has achievements for (`games`, `achievements` and
`points` are what could be won). `played` are those with an unlock; `beaten` and `mastered` are
the games whose highest award is that kind (a mastered game is not also counted as beaten).
Without a user, or before the progress was downloaded, `user` may still be set but `fetched` and
every progress field (`played` to `points_complete`) are `null`. `points_unlocked*` add up the
games whose unlock dates are downloaded and `points_complete` says whether all the played games
are. The systems are in name order, and only systems with such games appear.

`GET /api/v1/ra/badges/{badge}/{variant}` answers `image/png` with `Cache-Control: private,
max-age=86400`, under the same authentication as the rest, or `404 not-found` for a name that is
not a badge name, another variant or an image not downloaded.

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

### The inbox

`serve` reads `catalog/inbox/state.json`, the newest records of `catalog/inbox/runs/` and the
lock `inbox/.warpzone.lock` like the other hub files (kept in memory, read again when size or
mtime changed, checked at most every 5 seconds) and writes none of them. A record is served only
under an id of the form `20261113T101500Z` or `20261113T101500Z-2` (anything else is
`404 not-found` without touching the disk). Files are opened without following a final link and
without waiting on a named pipe: a state file or record that is a link or not a regular file is
no file, and one over 8 MiB, not JSON or (for a record) whose `id` is not its file name is damaged.
A lock that is a link, not a regular file, over 64 KiB or unreadable still makes the inbox busy,
with mode `unknown`. `busy` is `{"since", "mode", "task"}`, where `task` is `scrape` when a
scrape holds the lock and `null` otherwise; the Inbox view says "A scrape" instead of "A run".
The JSON is in [the unattended inbox](/warpzone/development/design/inbox-unattended/#api).

### Media and byte ranges

The media endpoint answers `200` with `Content-Type` by extension (`image/png`, `image/jpeg`,
`video/mp4`, `video/webm`), `Accept-Ranges: bytes` and the `Cache-Control` of the other media,
after the same authentication and the same checks of the path as before (a symlink anywhere
between `frontend/media/` and the file is `404`; see below). A video does not play on Safari and iOS without byte ranges, so the
endpoint takes a `Range` header with one range:

| Request | Answer |
|---|---|
| none | `200`, the whole file |
| `bytes=a-b`, `bytes=a-`, `bytes=-n` | `206` with `Content-Range: bytes first-last/size` and `Content-Length` of the part; a `b` past the end is cut to the end |
| a range that starts at or past the end, `bytes=-0`, or any range of an empty file | `416` with `Content-Range: bytes */size` |
| several ranges, another unit, or a header that is not a range | `200`, the whole file (a server may ignore a `Range`) |

`library_api.parse_range` (pure) decides this. The server opens the file once, reads it in
chunks of 64 KiB and never holds more than one chunk; `HEAD` sends the headers only. A client
that closes the connection in the middle (a player seeking) is not an error.

**Symbolic links in the media folder.** Syncthing replicates symbolic links as links, and the
media folder syncs both ways, so a console can make a link appear in it. The server treats any
link below `frontend/media/` as hostile:

- `safe_file(root, base, parts)` resolves `root` (the media folder; the UI folder for the static
  files) once and requires the path `base/parts` to be exactly its own resolved path below it. A
  link in the system folder, the kind folder, a subfolder or the file itself, even one that points
  to another place inside `root`, makes the file `404`; so does a `..`, an absolute name or a file
  outside `root`. Before this check a kind folder that was a link took the place of the root the
  file was compared with, so the link's target was served.
- The media listing does not list a system or kind folder that is a link (`os.walk` enters its
  top folder even when it is a link), so such a folder adds no kinds to a game either.
- The file is opened with `O_NOFOLLOW` and `O_NONBLOCK`, `fstat`ed, and served only when it is a
  regular file; the size and the bytes come from that one open file, so a file replaced by a link
  (or by a FIFO, which would block a thread) after the check is `404`. This narrows the gap
  between the check and the open; it does not close it: only the last component is not followed,
  so a folder above the file swapped for a link in that instant is still followed. Closing it
  would need `openat2` with `RESOLVE_BENEATH`, which is Linux only, or opening each component
  from a directory descriptor.
- The static route uses the same `safe_file` with the UI folder as root, so it follows no link
  either. It still reads the file with a plain `read_bytes` (the UI folder is part of the
  installation, not a synced folder). The badge route checks `is_symlink` on the file only and its
  folder is the hub's own `ra/badges/`, which is not synced.

The backup endpoints (see [History and restore](#history-and-restore)):

```json
{"device": "deck", "etag": "sha256:…",
 "backups": [{"name": "20261106T100000.123456Z.stignore", "time": "2026-11-06T10:00:00Z",
              "size": 412, "empty": false, "same": false, "included": 2, "excluded": 0,
              "summary": "2 path(s) back on"}]}
```

`GET …/backups/{name}` answers the same fields as a restore, below, without `written`. `POST
…/restore` takes `{"backup": "<name>", "expect": "sha256:…", "dry_run": false}` (`expect` is the
`etag` of the current whitelist, required) and answers:

```json
{"device": "deck", "backup": "20261106T100000.123456Z.stignore", "time": "2026-11-06T10:00:00Z",
 "etag": "sha256:…", "same": false, "managed": true,
 "diff": ["--- current", "+++ 20261106T100000.123456Z", "@@ -4,0 +5 @@", "+!/roms/psx"],
 "included": ["roms/psx"], "excluded": [], "on": ["<game id>"], "off": [],
 "bytes_on": 5368709120, "bytes_off": 0, "warning": "restoring turns 1 path(s) back on: …",
 "written": true, "dry_run": false}
```

`etag` is the new file's once `written`, else the current one; `warnings` (a failed scan) appears
when there is one. The errors are those of any whitelist write, in this order: `bad-request` (the
shape), `not-found` (an unknown device, or a backup that is not in its folder), `not-restorable`
(an empty backup), `expect-missing` and `stale`, and `write-failed`.

### Deleting and the trash

`POST /api/v1/games/{id}/delete` takes `{"dry_run": true}` or `{"dry_run": false, "expect":
"sha256:…"}` (`dry_run` is required; see
[The dry run and the confirmation](#the-dry-run-and-the-confirmation)). The dry run answers:

```json
{"dry_run": true, "game": {"id": "5d0c1f3a9b2e7c48", "system": "psvita", "title": "Example Game",
                           "name": "Example Game"},
 "expect": "sha256:…",
 "files": [{"path": "roms/psvita/app/PCSE00000", "type": "dir", "size": 3221225472, "files": 812},
           {"path": "roms/psvita/Example Game.psvita", "type": "file", "size": 9}],
 "absent": [], "size": 3221225993,
 "media": [{"path": "psvita/covers/Example Game.png", "size": 81234}], "media_size": 81234,
 "gamelist": true,
 "whitelists": {"deck": ["!/roms/psvita/app/PCSE00000", "!/roms/psvita/Example Game.psvita"]},
 "saves": {"data": true, "states": 0, "newest": "2026-11-08T21:14:03Z", "emulators": ["vita3k"]},
 "busy": null, "purge_after": "2026-11-23T10:15:00Z", "warnings": []}
```

`files` are below `library/`, `media` below `frontend/media/`; `gamelist` says whether an entry
is removed; `whitelists` has the lines as they are written in each device's file (devices with
none are left out); `saves` is the object of [Saves per game](#saves-per-game), kept;
`busy` is `null` or the inbox API's `{"since", "mode", "task"}`; `purge_after` is when the entry
would be purged if made now. The apply answers:

```json
{"dry_run": false, "entry": "20261116T101500Z-5d0c1f3a9b2e7c48",
 "purge_after": "2026-11-23T10:15:00Z", "moved": {"files": 2, "media": 1, "bytes": 3221307227},
 "gamelist": "removed", "pending": {"deck": 2}, "warnings": []}
```

`gamelist` is `removed`, `none` (no entry) or `failed` (a warning says why); `pending` counts the
lines recorded per device; `warnings` holds failed scans and skipped links.

`GET /api/v1/trash`:

```json
{"size": 3221307227,
 "entries": [{"entry": "20261116T101500Z-5d0c1f3a9b2e7c48", "id": "5d0c1f3a9b2e7c48",
              "system": "psvita", "title": "Example Game", "name": "Example Game",
              "deleted": "2026-11-16T10:15:00Z", "purge_after": "2026-11-23T10:15:00Z",
              "days_left": 7, "size": 3221307227, "files": 2, "media": 1, "state": "complete"}],
 "pending": [{"device": "deck", "entry": "20261116T101500Z-5d0c1f3a9b2e7c48",
              "title": "Example Game", "lines": 2, "since": "2026-11-16T10:15:00Z",
              "waiting": "offline"}]}
```

`state` is `complete`, `incomplete` or `damaged` (then only `entry` and `state` are certain, the
rest `null`). `days_left` is the whole days until `purge_after`, rounded up, never below 0, and
`null` for an entry the purge skips. `waiting` says what a device's lines wait for: `hub` (the
hub has not recorded the deletion), `offline`, `paused`, `not-shared`, `syncing` (connected, not
done yet), `not-managed` (its whitelist needs migration) or `unknown` (the hub's Syncthing did not
answer).

`POST /api/v1/trash/{entry}/restore` takes `{}` and answers:

```json
{"entry": "20261116T101500Z-5d0c1f3a9b2e7c48",
 "game": {"id": "5d0c1f3a9b2e7c48", "system": "psvita", "title": "Example Game"},
 "restored": {"files": 2, "media": 1}, "gamelist": "restored",
 "kept": ["deck"], "removed": ["machine"], "warnings": []}
```

`gamelist` is `restored`, `present` (an entry with that path was there), `none` or `failed`;
`kept` are the devices whose lines were still pending (they receive the game again) and `removed`
those whose lines were already removed (the game is off there). `POST
/api/v1/trash/{entry}/purge` takes `{}` and answers `{"entry": "…", "purged": true, "bytes":
3221307227}`. An entry name that does not match the pattern, or is not in `trash/`, is
`404 not-found` without touching the disk.

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
last two when they apply; an error about the queue file carries `"queue": true` instead of
`device`, and the queue's own codes are in [Errors](/warpzone/development/design/play-queue/#errors);
an error about the trash carries `entry`, `has-dependants` carries `dependants`, `unsafe-path` and
`restore-conflict` carry `paths`, and `move-failed` carries `incomplete` and, when true, `paths`):

| Status | `code` | When |
|---|---|---|
| 400 | `bad-request` | malformed JSON, a body over 64 KiB, an unknown key, a wrong type |
| 401 | `unauthorized` | no session cookie or bearer token, or a wrong one |
| 403 | `bad-origin` | a cookie-authenticated write from another origin |
| 404 | `not-found` | an unknown device, system, game, media kind, trash entry or route, or a method other than `GET`, `HEAD` and `POST` |
| 409 | `not-managed` | the device's whitelist needs `warpzone whitelist migrate` |
| 409 | `covered` | a part stays off under an exclusion of a parent folder |
| 409 | `cannot-apply` | the change would not reach its state or would change other games |
| 409 | `unlistable` | a part's path cannot be written as a whitelist line |
| 409 | `not-restorable` | restoring a backup that is empty (taken before the whitelist existed), or a trash entry whose manifest is damaged |
| 409 | `catalog-running` | `catalog/refresh` while a run is in progress |
| 409 | `hub-busy` | deleting or restoring a game while `inbox/.warpzone.lock` exists (an inbox or a scrape is running) |
| 409 | `unsafe-path` | a part of the game to delete is a symbolic link, is not of the type the catalog records, belongs to another game too, or changed right before its move |
| 409 | `has-dependants` | deleting an arcade set that another set of the library needs as its parent, BIOS or device |
| 409 | `dats-missing` | deleting a game of an arcade system while `serve` can read none of the arcade DATs |
| 409 | `trash-unavailable` | `trash/` cannot be created, or is on another filesystem than the library or the media |
| 409 | `restore-conflict` | restoring an entry while a path it would put back exists again |
| 412 | `stale` | an `expect` differs from the file as it is now, or a deletion's plan changed since its dry run |
| 422 | `unsupported` | turning on a system in the device's `exclude_systems` |
| 428 | `expect-missing` | a touched device has no `expect`, or a deletion is applied without one |
| 429 | `too-many-attempts` | an attempt to sign in (the session route or a bearer token) while 8 failed ones already wait; has a `Retry-After` header |
| 500 | `write-failed` | a whitelist could not be written (a disk error); the answer also lists the devices already written |
| 500 | `read-failed` | a whitelist, or the pending cleanups file, exists but could not be read (permissions on the share); the message names the file and the cause, the log has the full path |
| 500 | `move-failed` | a move into or out of the trash failed; the moves were rolled back, unless `incomplete` is true |
| 500 | `purge-failed` | a trash entry could not be removed whole |
| 500 | `internal` | an unexpected failure of the server (the log has the exception name, never a token) |
| 503 | `hub-unreachable` | only `progress`: the hub's API did not answer |

## Progress

`GET /api/v1/progress` reads the hub's Syncthing, never a console's: for each device that lists
`library`, `GET /rest/system/connections` (is it connected) and `GET /rest/db/completion?folder=
wz-library&device=<id>` (`completion`, `needBytes`, `needItems`, `remoteState`). The answers are
cached for 15 seconds, since a completion over a large folder costs the hub a walk of its
database. A failed question (`503 hub-unreachable`) is cached for 5 seconds as well, so a down
Syncthing is not asked again by every poll of every open page; the first poll after that asks
again. `progress.state` (pure) turns them into one state per device:

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
no other state); a whitelist the UI created has an empty backup for that. Progress is per console, not per game. Before this is relied on, the e2e test
must show how the hub counts files a device ignores: if a whitelisted device never reaches
`completion` 100 because the hub counts its ignored files as needed, `downloading` is computed
instead from `GET /rest/db/remoteneed?folder=wz-library&device=<id>` filtered through the
device's whitelist (`whitelist.decide`), and per-game progress comes with it.

## Disk space

The hub cannot see a console's disk, only Syncthing's completion, so each console says it. A
console that lists `play-state` writes `<device>.status.json` into the play state folder
(`wz-play-state`, `frontend/play-state` on the hub) and the hub reads it for `disk` in
`GET /api/v1/devices`.

```json
{"v":1,"device":"deck","at":1793000000,"total":512000000000,"free":120000000000}
```

- **Why that folder.** The library folders are receive-only on a console, so nothing a console
  writes there reaches the hub; `wz-play-state` is the folder that is two-way for every
  console, holds one file per writer (so there is no conflict) and nothing reads from it
  but the ledger readers, which glob `*.jsonl` and so never see a `.status.json`; no frontend
  looks into it. Its ignore `*.wz-tmp` already covers the atomic write's temporary file. The
  hub keeps 30 days of versions of that folder, which is why the writer is sparing (below).
  The cost is that a console without `play-state` reports no disk (`null` fields).
- **What it measures.** `shutil.disk_usage` of the profile's `paths.library`, the filesystem the
  library folder sits on, in whole MiB (rounded down). Nothing is written until the library
  folder's `.stfolder` marker exists, so an unmounted card is not reported as the disk below
  it. The bytes the library uses are not measured: walking a library of tens of thousands of
  files on a card is not cheap, and the hub knows what a whitelist turns on.
- **Who writes it, and when.** The job that already runs on the console: `play-state sync` (its
  timer and ES-DE's startup script) and, for a profile with `[artwork]`, `frontend refresh`.
  It writes only when there is no usable report yet, the size changed, the free space moved by
  1% of the disk or more, or the report is a day old (a heartbeat, so the hub can tell a quiet
  console from a silent one). It is atomic and refused when the file changed since it was
  read. A failure to measure or write is a warning and never fails the job. With `--dry-run`
  it says it would write and writes nothing.
- **What the hub does with it.** It reads the files on each request (they are a few dozen bytes)
  and serves them as `disk`. A file that is not UTF-8 JSON of version `1`, names another device,
  has a missing or negative number, or has `free` above `total`, is the same as no file. A report
  older than 7 days (a console that stopped) or more than a day ahead of the hub (a wrong
  clock) is shown as null rather than as the last value, so the page never states a disk
  that may be long gone.

## The container

warpzone runs on the hub in a container, because a NAS's own Python is often older than 3.11. The
image (`docker/Dockerfile`) is a slim official Python image pinned by digest, with warpzone
installed from the checkout the image is built from (a release tag, as a wheel, with no index
access) and RAHasher built with CHD support in a separate stage from RALibretro, pinned by tag and
commit; only its binary reaches the last stage, at `/usr/local/bin/RAHasher`. The last stage also
installs `mame-tools` (`chdman`) and `libarchive-tools` (`bsdtar`) with `apt-get install
--no-install-recommends` and removes the apt lists in the same layer, so `inbox` can convert discs
and unpack `.7z` and `.rar` there. They land in `/usr/bin`, on `PATH`, so `doctor` needs no path
for them. The tools Debian does not package are built or installed in stages of their own, and
only the result reaches the last stage, all on `PATH` in `/usr/local/bin`:

- **`dolphin-tool`** is built from Dolphin's source at an annotated tag, checked against the
  pinned commit, with the submodules the build needs fetched at the commits that commit records.
  Only the `dolphin-tool` target is built, with Dolphin's bundled libraries linked statically
  (`USE_SYSTEM_LIBS=OFF`) and the Qt, NoGUI, Vulkan, X11, EGL, sound, controller, update,
  analytics, RetroAchievements, UPnP and GBA parts switched off, so the binary needs only the C and
  C++ runtime libraries the last stage already has. It still compiles Dolphin's emulation core,
  because `dolphin-tool` links it; that is most of the image build time (see the CI job below).
- **`nsz`** is installed from PyPI into a virtual environment of its own, `/opt/nsz`, from
  `docker/nsz-requirements.txt`: every package, `nsz` and its dependencies, pinned to an exact
  version with its SHA-256 (`pip install --require-hashes --no-deps --only-binary=:all:`, so a
  package with no wheel for the architecture fails the build rather than compiling). Only
  `/opt/nsz/bin/nsz` is linked onto `PATH`, so warpzone's own Python has none of its packages.
- **`orbis-pkg-util`** is built with Cargo from the crate on crates.io: the download must match a
  pinned SHA-256 and the build uses the `Cargo.lock` packaged in the crate (`--locked`). The
  stage's base image (`rust`, slim, trixie) is pinned by digest.
- **Vita3K** is not built: its own stage downloads the x86_64 AppImage of a numbered build
  of `Vita3K/Vita3K-builds` (build 4334), checks the pinned SHA-256 and unpacks it with
  `--appimage-extract`; the bundle is copied to `/opt/vita3k` and `/usr/local/bin/Vita3K` is a
  link to its `bin/Vita3K`. On any other CPU the stage leaves the folder empty and the image
  has no Vita3K. The link and the entrypoint are explained in
  [PS Vita packages in the container](/warpzone/development/design/inbox/#ps-vita-packages-in-the-container).

Each is licensed apart from warpzone's MIT: RAHasher GPL-3.0, `dolphin-tool` GPL-2.0-or-later,
Vita3K GPL-2.0 (its bundle also carries Qt 6, Mesa and others under their own licences),
`nsz` MIT, `orbis-pkg-util` MIT or Apache-2.0, and the Debian packages as Debian says. Only the
Dockerfile, the entrypoint script and the requirements file are in the repository, never the
sources or the binaries: the image is built by whoever runs it, from the pinned upstream tags and
checksums.
Keys and big data (`switch_keys`, `vita_zrif`, `titledb_dir`, the DATs) are mounted, never
copied into it (see [where inbox runs](/warpzone/setup/inbox/#where-inbox-runs)).
The CI `image` job (weekly and manual runs; it builds the image, checks every tool and runs
`warpzone doctor` inside it, and runs Vita3K the way `inbox` does) is what shows that a pin or a base image change still builds.
The entry point is `docker/entrypoint.sh` (installed as `/usr/local/bin/warpzone-entrypoint`), which runs
`warpzone` and the default command is `serve`, so `docker compose run --rm warpzone catalog ...` runs the other commands. The
image runs as a non-root user and the Compose file sets `user:` to the uid and gid that own the
games share. An example Compose file is `examples/compose.example.yaml`, and the steps are in
[the hub container guide](/warpzone/setup/hub-container/).

| What | Where in the container | Why |
|---|---|---|
| the hub root | the same path as `[hub] root`, read-write | the whitelists in `library/`, `catalog/`; reads `library/` and `frontend/` |
| the config folder | `/config`, read-only, with `WARPZONE_CONFIG=/config/config.toml` | `[hub]`, `[devices.*]`, `[serve]`, `[inbox] rahasher`, the hub API key file |
| `WARPZONE_UI_TOKEN` | environment (or `[serve] token_file` in `/config`) | the UI token |
| `RA_API_KEY` | environment, optional | the RA game lists, achievements, progress and badges |
| port 8780 | published on the LAN | the page and the API |

Mounting the hub root at the path the config already names keeps one config valid inside and
outside the container. `serve` refuses to start (exit `2`) when `[hub] root` has no
`library/.stfolder`, so an unmounted share never looks like an empty library. The hub's
Syncthing API must be reachable at `[hub] api_url` from the container: a LAN address works from
a bridge network (use `https://` with `[hub] api_cert_sha256` there, so the API key does not cross the
network in the clear, see [the hub's API over the network](/warpzone/development/design/configuration/#the-hubs-api-over-the-network));
when Syncthing listens only on loopback, the container uses the host network.
The image has a health check on `GET /healthz` (Python's standard library, port 8780 inside the container).

## Pure decisions and I/O

As everywhere in warpzone, a pure function decides and a thin layer touches the disk or the
network:

- **Pure:** `catalog.derive`, `catalog.system_roots`, `catalog.title_key`, `catalog.search`,
  `catalog.ra_state`, the catalog's JSON encoding and merge; `recommend` (the two shelves' rules);
  `ra_data` (the achievement
  cache's encoding, `game_view`, `summary_view`) and the parsers of `ra.py`; `whitelist.escape`, `unescape`,
  `parse`, `parse_hand`, `render`, `normalize`, `apply`, `decide`, `decide_lines`, `effect`,
  `unsupported` and the migration's classification; `whitelist_history` (backup names, the
  comparison of two files, the diff, what a restore refuses and warns about); `queue` (the play queue's schema,
  inferred states and `plan`); `library_api.plan_changes` and every
  response builder (the play statistics included: `game_play`, `attach_play`, `recent_response`);
  `saves_index.build_index`, `match_game` and `saves_response`; `library_api.parse_record`,
  `inbox_busy`, `inbox_response` and `run_response` (the unattended inbox's answers); `progress.state`; `prune.guard`;
  `diskstatus` (`measure`, `should_write`, `render`, `parse`, `stale`, `api_disk`); `trash`
  (`plan_delete` with the media stems, the arcade dependants and the plan's `expect`, the entry
  names, the manifest's and the cleanups file's encoding and validation, which cleanup is ready,
  what is expired, `days_left`), `whitelist.drop_lines` and `gamelist.drop_entries`.
- **Orchestration:** `trash_run` (`Trasher`: the hub lock with `task: "delete"` and then the
  write lock, the plan again, the order of a deletion with its rollback, the restore, the
  purge, and the background task every 60 seconds that purges after 7 days and cleans the
  whitelists), over the I/O of `trash_store` and the pure `trash`.
- **I/O:** `catalog_store` (the walk, hashing, RAHasher and the RA download through `ra.py`, the
  guarded writes), `diskstatus_io` (the disk report's write on a console and read on the hub),
  `queue_store` (the queue file: reading with a stamp, backups, the guarded write),
  `whitelist_store` (reading with a stamp, backups, the guarded write, the hub
  scan, the migration's walk, the restore), `saves_index.scan` and `SavesCache` (the listing of `saves/` and its
  60-second cache), `trash_store` (the listing for a plan, the moves and their rollback, the
  manifest and cleanups writes, the restore, the purge, the questions to the hub for the
  cleanup), `serve` (sockets, sessions, the locks, the background runs, the caches).

Tests: the derivation on listings of each layout (PS4 with and without its shortcut, Vita with
and without a licence, Switch with updates and DLC and an unclaimed folder, `.m3u` folders,
`xbox360/roms`); the title keys of No-Intro, Redump and Switch names; the round trip
`parse(render(model))`, normalization and every change against `decide`; escaping of every
special character and refusal of unlistable names; the migration on hand-written files
(shadowed lines, redundant exclusions, a file refused for a wildcard, a file refused by the
proof); the API against a fake hub root and the fake Syncthing (auth, origin, `stale`,
`not-managed`, `unsupported`, dry runs, a `*` change with a skipped device; the backups listed and
restored, an empty one refused); deletion on a fake hub root (every layout of the derivation
moved whole and restored to the same bytes, media of a kind the UI does not read moved too, media stems shared with another game left alone, a
symlinked part refused, an arcade parent refused with its clones named and a missing DAT refused,
`stale` after a change between the dry run and the apply, `hub-busy`, a failed move rolled back
and an unrecoverable one left incomplete, a restore conflict, Delete now, the purge after 7 days
skipping incomplete entries) and the cleanup against the fake Syncthing (no write before the hub
records the deletion, none for an offline or syncing device, lines removed once done, a path that
reappeared kept, a restore cancelling what is pending); and the progress
states; the disk report (when to write, parsing damaged and foreign files, staleness, the writer
on a fake disk and the two jobs that call it, the hub reader and the API with missing, stale and
damaged files). The e2e test adds the matcher check above and the completion check.

## Out of scope

- Moving or renaming anything in the library, and deleting anything but a whole catalog game:
  one disc, update or DLC of a game, an unclaimed Switch folder, a BIOS file, a save.
- Emptying the trash before 7 days other than entry by entry, and restoring a purged entry (the
  hub's snapshots are the way back then).
- Games without a scrape beyond the placeholder, and scraping itself.
- `songs/` and its whitelists.
- Arcade dependencies in the catalog: a MAME game whose parent or BIOS set is another zip does not
  list that zip as a part. Turning on the whole system is the safe way for arcade systems. A
  deletion checks the dependants itself (see [Refusals](#refusals)).
- Per-game progress, unless the completion check above calls for it.
- Users and roles, and TLS: the server is plain HTTP. Access from outside the LAN is the
  user's own proxy or tunnel in front of it, with a worked example in the hub container guide
  (see [Authentication](#authentication)); warpzone ships and runs none.
- The Decky plugin itself; only the API above is kept for it.
- Editing a whitelist by hand once it is managed (it is refused until migrated again, and the
  migration takes it only after its `// Folders` section is deleted, since the hand-file parser
  refuses the wildcards of those lines), and wildcard lines in whitelists.
