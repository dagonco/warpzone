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
6. warpzone runs on the hub in a container (the same image `inbox` can run in on the hub).
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
├── ra/games/<game id>.json    the achievements of one RA game, cached
├── ra/user/summary.json       the user's completion progress, cached
├── ra/user/games/<id>.json    when the user unlocked each achievement of one game, cached
├── ra/badges/<name>[_lock].png  badge images, cached
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
  against the DAT games that have one rom; else, for an `.m3u` folder, each disc named in the
  playlist inside it, by exact name against the DAT game names (`inbox` names a disc file after
  its DAT game), with the union of the discs' serials in disc order, all discs needed. Nothing is
  compared case-insensitively or cleaned up. A key the DATs give two different serial lists is
  not guessed at. A game with no exact match is left without serials and counted as unmatched.
- **Writes:** only the `serials` of `catalog/systems/<system>.json`, with the scan's guarded atomic
  write and its changed-since-read check (read again, up to three tries, then a `conflict`). A
  file whose games would not change is not written, and `--dry-run` writes nothing (the DAT parse
  cache, which is not user data, is the one thing a dry run may still write).
- **Output and exit code:** per system, `filled / already had / unmatched` (or `to fill` in a dry
  run), `no DAT with serials` for a system whose DATs are missing or list none, and the first
  unmatched games by path. Exit `1` when a system file could not be read or written, `2` for a
  usage error (no usable DAT folder, `--dats-dir` or `--force` without `--dats`), as for the other
  passes; unmatched games are not a failure.

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

The UI reads the ES-DE scrape that is already on the hub, read-only:

- `frontend/es-de/gamelists/<system>/gamelist.xml`, parsed with `artwork.parse_gamelist`. A
  game's entry is the one whose `<path>` equals its `gamelist`. The API passes on `name`, `desc`,
  `rating`, `releasedate`, `developer`, `publisher`, `genre` and `players`.
- `frontend/media/<system>/<kind>/<stem>.<ext>`, where `<stem>` is the gamelist path without
  `./` and without its extension (a folder entry keeps its whole name, as in `artwork.build`),
  for the kinds `covers`, `miximages`, `screenshots`, `titlescreens`, `marquees`, `3dboxes`,
  `physicalmedia` (the scan of the cartridge or disc), `backcovers` and `videos`, and the
  extensions `png`, `jpg` and `jpeg` (`mp4` and `webm` for `videos`; where a stem has more than
  one extension the first in each list wins).

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

- **Home:** a "Now showing" carousel of games that have art, the state and download progress of
  each console (from `/api/v1/progress`) with its disk as a bar of used and free space (from
  `disk`; nothing for a console that reported none), a "Continue playing" row (from
  `/api/v1/recent`: cover, name, the console that played it last and when), the systems with
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
  exists only when the dates are known, and a long list shows the first 48 until asked.
- **Search:** results grouped by title key across systems, with the same per-console switches.
- **Consoles:** the state of each console and what it holds.

What the API does not provide is not drawn, not even as a placeholder (no high-score table, no
attract mode), and a piece whose data is missing is left out rather than shown empty. Long system names are shortened to a code
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

The server reads the catalog files, the gamelists, the RA lists, the whitelists and the
[play-state ledgers](/warpzone/development/design/play-state/) from the hub root and keeps them in memory, reloading a file when its size or mtime changed (checked at most
every 5 seconds). It runs `catalog` in a background thread at start and every 6 hours, and
`catalog --ra-lists` once a day when `RA_API_KEY` is set (the 7-day age rule makes most of those
runs no-ops). With the key, every run also does a bounded share of `catalog --ra-achievements`
(see [Achievements and progress](#achievements-and-progress)).

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
  changing the token.
- A client that is not a browser (the Decky plugin) sends `Authorization: Bearer <token>`.
- Every `/api/` route except `POST /api/v1/session` needs one of the two, compared with
  `hmac.compare_digest`; the static files and `GET /healthz` do not. The session cookie is
  looked for among all the cookies of the request (other applications on the same host share
  the jar), and every `wz_session` value is compared. A write authenticated by the cookie must
  carry an `Origin` equal to `http://<Host>` or `https://<Host>` (a TLS reverse proxy keeps the
  `Host` header the browser used), and every write must be `Content-Type: application/json`,
  which together keep other pages from posting to it.
- Writes are `POST` only: any other method on an `/api/` route is `404 not-found`.
- A wrong token, at login or as a bearer, is answered `401` after a wait, and the waits are
  shared: failures leave the server one second apart in all, however many connections send
  them, so guessing in parallel gains nothing. A request with no credentials, or the right
  ones, never waits.

The server also bounds what a stranger can hold. A connection that sends nothing for 15
seconds, also in the middle of a body, is closed; at most 64 connections are served at once and
the next ones are closed without an answer. A `Content-Length` that is not a number is `400`.

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
| `GET /api/v1/games/{id}/media/{kind}` | the game's image or video of that kind (with [byte ranges](#media-and-byte-ranges)), or `404` |
| `GET /api/v1/search?q=...` | games whose title key matches, grouped by key |
| `GET /api/v1/ra/summary` | the library's RetroAchievements games and the user's [progress](#achievements) in them, per system and in all |
| `GET /api/v1/ra/badges/{badge}/{variant}` | a cached RA badge image (`variant` is `unlocked` or `locked`), or `404` |
| `GET /api/v1/recent?limit=N` | the games played most recently on any device, newest first |
| `GET /api/v1/progress` | the [progress state](#progress) of each device |
| `GET /api/v1/catalog` | whether the catalog is built, and the state of the background run |
| `POST /api/v1/changes` | turns games and systems on and off |
| `POST /api/v1/catalog/refresh` | starts a `catalog` run now (`202`; `409` while one runs) |

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
most every 5 seconds); a ledger that cannot be read is left out, and the ledgers of devices that
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

### Media and byte ranges

The media endpoint answers `200` with `Content-Type` by extension (`image/png`, `image/jpeg`,
`video/mp4`, `video/webm`), `Accept-Ranges: bytes` and the `Cache-Control` of the other media,
after the same authentication and the same checks of the path as before (a symlink that leaves
the kind's folder is `404`). A video does not play on Safari and iOS without byte ranges, so the
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
| 404 | `not-found` | an unknown device, system, game, media kind or route, or a method other than `GET`, `HEAD` and `POST` |
| 409 | `not-managed` | the device's whitelist needs `warpzone whitelist migrate` |
| 409 | `covered` | a part stays off under an exclusion of a parent folder |
| 409 | `cannot-apply` | the change would not reach its state or would change other games |
| 409 | `unlistable` | a part's path cannot be written as a whitelist line |
| 409 | `catalog-running` | `catalog/refresh` while a run is in progress |
| 412 | `stale` | an `expect` differs from the file as it is now |
| 422 | `unsupported` | turning on a system in the device's `exclude_systems` |
| 428 | `expect-missing` | a touched device has no `expect` |
| 500 | `write-failed` | a whitelist could not be written (a disk error); the answer also lists the devices already written |
| 500 | `read-failed` | a whitelist exists but could not be read (permissions on the share); the message names the file and the cause, the log has the full path |
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
commit; only its binary reaches the last stage, at `/usr/local/bin/RAHasher` (the other tools of `inbox` are not in the image; see
[where inbox runs](/warpzone/setup/inbox/#where-inbox-runs)). The entry point is `warpzone` and the default
command `serve`, so `docker compose run --rm warpzone catalog ...` runs the other commands. The
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
a bridge network; when Syncthing listens only on loopback, the container uses the host network.
The image has a health check on `GET /healthz` (Python's standard library, port 8780 inside the container).

## Pure decisions and I/O

As everywhere in warpzone, a pure function decides and a thin layer touches the disk or the
network:

- **Pure:** `catalog.derive`, `catalog.system_roots`, `catalog.title_key`, `catalog.search`,
  `catalog.ra_state`, the catalog's JSON encoding and merge; `ra_data` (the achievement
  cache's encoding, `game_view`, `summary_view`) and the parsers of `ra.py`; `whitelist.escape`, `unescape`,
  `parse`, `parse_hand`, `render`, `normalize`, `apply`, `decide`, `decide_lines`, `effect`,
  `unsupported` and the migration's classification; `library_api.plan_changes` and every
  response builder (the play statistics included: `game_play`, `attach_play`, `recent_response`);
  `saves_index.build_index`, `match_game` and `saves_response`; `progress.state`; `prune.guard`;
  `diskstatus` (`measure`, `should_write`, `render`, `parse`, `stale`, `api_disk`).
- **I/O:** `catalog_store` (the walk, hashing, RAHasher and the RA download through `ra.py`, the
  guarded writes), `diskstatus_io` (the disk report's write on a console and read on the hub),
  `whitelist_store` (reading with a stamp, backups, the guarded write, the hub
  scan, the migration's walk), `saves_index.scan` and `SavesCache` (the listing of `saves/` and its
  60-second cache), `serve` (sockets, sessions, the lock, the background runs, the
  caches).

Tests: the derivation on listings of each layout (PS4 with and without its shortcut, Vita with
and without a licence, Switch with updates and DLC and an unclaimed folder, `.m3u` folders,
`xbox360/roms`); the title keys of No-Intro, Redump and Switch names; the round trip
`parse(render(model))`, normalization and every change against `decide`; escaping of every
special character and refusal of unlistable names; the migration on hand-written files
(shadowed lines, redundant exclusions, a file refused for a wildcard, a file refused by the
proof); the API against a fake hub root and the fake Syncthing (auth, origin, `stale`,
`not-managed`, `unsupported`, dry runs, a `*` change with a skipped device); and the progress
states; the disk report (when to write, parsing damaged and foreign files, staleness, the writer
on a fake disk and the two jobs that call it, the hub reader and the API with missing, stale and
damaged files). The e2e test adds the matcher check above and the completion check.

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
