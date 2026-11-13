---
title: "Inbox"
---

`warpzone inbox [--root PATH] [--dats PATH] [--work-dir PATH] [--apply] [--notify-url URL]` files new
games dropped in `inbox/` into `library/roms/<system>/`. It reads no Syncthing state. It
runs in one of two places, with the same code and the same contract:

- **On a computer that mounts the hub's share** (the usual way, because conversions are
  heavy for a NAS). The tools are installed there and found on `PATH` or at the `[inbox]`
  paths; `--root` is the mount point on that computer.
- **In the hub container** (`docker/Dockerfile`, see
  [the container](/warpzone/development/design/library-ui/#the-container)). The tools are the
  image's: `chdman`, `bsdtar`, `dolphin-tool`, `nsz` and `orbis-pkg-util` on its `PATH`, and
  RAHasher at `/usr/local/bin/RAHasher` (used only when `[inbox] rahasher` is set, as
  everywhere). Vita3K is the one tool the image lacks (see
  [PS Vita packages in the container](#ps-vita-packages-in-the-container)). The hub root is
  mounted at the same path as `[hub] root`, so `--root` is that path. The DATs, the keys
  (`switch_keys`, `vita_zrif`), `titledb_dir`, the arcade DATs and a work folder are
  volumes: keys and big data are never part of the image, and `inbox` reads them where
  `[inbox]` says (the container's `/tmp` is memory, and `HOME`, so the DAT cache, is
  there too unless `XDG_CACHE_HOME` is moved). Nothing in `inbox` tells the two apart; only
  `warpzone doctor` does, to check the right things (see
  [Doctor](/warpzone/development/design/doctor/)).

The contract:

- **Layout.** `--root` (or `[inbox] root`) is the hub root as mounted on this machine:
  it must hold `inbox/` and `library/roms/`. Items are the top-level files and folders
  of `inbox/`. Hidden files, `@eaDir`, `#recycle`, `Thumbs.db` and any `_*` folder
  (`_done`, `_revisar`, `_unverified`) are never items of that scan; the only `_*` folder
  that holds items is `_unverified/<system>/` (see
  [Roms no DAT knows](#roms-no-dat-knows)). An item modified in the last 30 seconds is left
  alone, since it may still be copying.
- **Report only by default.** Without `--apply` it unpacks into a local work folder,
  identifies, plans and prints, and writes nothing to the share. With `--apply` it
  converts, places and files the originals.
- **One writer at a time.** `--apply` (never a report) takes `inbox/.warpzone.lock`
  (`inbox_lock.py`) after the layout checks and before it scans: created with
  `O_CREAT | O_EXCL`, holding JSON with the host name, pid, start time, `beat` (the heartbeat
  interval, 60) and `mode` (`attended` here; `unattended` is for the design in
  [Unattended inbox](/warpzone/development/design/inbox-unattended/#locking)). While the run
  holds it, a thread (`inbox_lock.Heartbeat`) touches the file's modification time every 60
  seconds, and only while the file still holds this run's host, pid and start time
  (`inbox_lock.still_mine`); it ends with the lock. Another run that finds the lock exits with
  `LockBusy`, an `InboxError` (usage exit code) naming the holder and the time; the same goes for
  a lock somebody else created between the look and taking it. Messages call it
  `inbox/.warpzone.lock`, never a path outside the hub. `inbox_lock.judge` (pure) decides whether
  a lock in the way is stale, by one look. With a heartbeat: its modification time is more than 10
  minutes old by this machine's clock (`no heartbeat since 10:15`), or its pid is not alive on the
  same host (`os.kill(pid, 0)`; a pid of another host cannot be checked). Without `beat` (a lock of
  an older warpzone): older than 12 hours (an unreadable lock, such as one still being written,
  goes by the age of the file), or the same pid rule; a run longer than 12 hours is no
  longer stale on that count, because its heartbeat keeps the lock fresh. A stale lock is taken
  over only with `--force-lock` (`LockStale` says so otherwise), and with a report line; a live one
  never. The takeover renames the stale file to a name of its own (atomic) and deletes it only if
  what it renamed is what was judged; otherwise it was somebody's live lock and goes back (with
  `link`, or with `rename` on a share without hard links, only when nothing is at the lock's path;
  if it cannot go back it is kept, named in the error, and never deleted). The
  heartbeat ends when the lock is gone or not this run's, but a read or a touch that fails on the
  share only skips that beat, and releasing the lock waits at most 5 seconds for a touch stuck on
  a stalled share. The lock is released in a `finally` (errors
  and Ctrl-C), after the heartbeat stops, and only if it still holds this run's host, pid and
  start time. It is a hidden file, so scanning skips it. A `SIGKILL` or a power cut leaves it
  behind, which is what the stale rules are for. The `mode` parameter of `inbox_lock.hold` also
  accepts `unattended`, which judges two readings of the lock instead of one look; see the design
  page above.
- **Untrusted content.** Archives are unpacked only into the local work folder (`.zip`
  with `zipfile`, `.7z` and `.rar` with `bsdtar`; archives inside archives up to two
  levels). A member with an absolute path, a `..` component or a NUL byte, or a symbolic
  link, makes the whole item go to review, and nothing is extracted before every name is
  checked. The free space is checked before extracting too: the sum of the member sizes
  (from the zip directory, or from the fifth column of `bsdtar -tvf`; the names are taken
  from `-tf`, never from that listing) plus 5% must fit in the work folder, or the item
  goes to review, and so does an archive with a listing line that has no decimal size. A
  member name that is not UTF-8 is read with replacement characters and never stops the
  run. A cue or gdi is followed only if every file it names is a file of the item.
  Symbolic links in the inbox are skipped.
- **Identification by content.** Every file that is not a sidecar (`.cue`, `.gdi`,
  `.txt`, images, ...) is read once for size, CRC32 and SHA1, and looked up in the
  Redump and No-Intro DATs (clrmamepro format, libretro-database's `metadat/`). A rom
  matches on size and CRC32, and also SHA1 when the DAT has one; a game matches when all
  its roms are present, so a DAT that lists only the data track of a disc (as
  libretro's do) still matches, and the other tracks are taken from the cue or gdi that
  names it. File names never matter. Two games with exactly the same files cannot be
  told apart and go to review. The DAT file name maps to the system folder with a small
  table (`SYSTEMS` in `warpzone/dat.py`: PlayStation `psx`, PlayStation 2 `ps2`,
  PlayStation Portable `psp`, Saturn `saturn`, Dreamcast `dreamcast`, GameCube `gc`,
  NES `nes`, SNES `snes`, Nintendo 64 `n64`, Game Boy `gb`, Game Boy Color `gbc`, Game
  Boy Advance `gba`, Nintendo DS `nds`, Mega Drive / Genesis `genesis`, Master System `mastersystem`, Game
  Gear `gamegear`, Atari 2600 `atari2600`, Atari 7800 `atari7800`, Atari Lynx
  `atarilynx`, Atari Jaguar `atarijaguar`, WonderSwan `wonderswan`, WonderSwan Color
  `wonderswancolor`, 32X `sega32x`); a trailing No-Intro date stamp is ignored. DATs are parsed only when
  a lookup needs them (the system the file extensions and sizes point at first, and the
  search stops once every file is explained) and each parsed DAT is cached as JSON in
  `$XDG_CACHE_HOME/warpzone/dats`, keyed by the DAT's size and modification time.
- **NES and Atari 7800 dumps with a header.** A file that starts with the iNES magic
  `NES\x1a` is also hashed without its first 16 bytes, and one with `ATARI7800` at offset 1
  (an A78 header) without its first 128 (`FileInfo.without_header`). In a NES or Atari
  7800 DAT, when the whole file matches nothing, `match_dat` looks that data up among the
  DAT's headerless entries (No-Intro lists NES ones as `.unh`, 16 bytes smaller than the
  headered dump, and 7800 ones as `.bin`; `HEADERED_EXTENSIONS` in `warpzone/dat.py`), and
  only a SHA1 equal to the DAT's counts. The same verified dump circulates with
  different headers (iNES against NES 2.0 fields, junk in the reserved bytes) and the
  data is what is verified. The game is filed under the DAT's game name as usual, and the
  file is kept as it is, header included: the match carries roms that describe the file
  (its size, hashes and a `.nes` or `.a78` name), so the zip holds the original bytes and the
  size/CRC32 check after the copy compares those. A whole-file match always wins, two names
  with the same data are ambiguous as before, and a file with no header or no
  headerless match goes to review as "no DAT match".
- **Roms no DAT knows.** See [Roms no DAT knows](#roms-no-dat-knows) below; a file the user
  drops in `inbox/_unverified/<system>/` and no DAT identifies is filed as it is.
- **Formats follow the library.** `psx`, `saturn`, `dreamcast`: `chdman createcd` from
  the cue or gdi, then `chdman verify`. `ps2`: `createdvd` for an `.iso`, `createcd`
  for cue/bin. A lone `.bin` without a cue gets a generated one (PlayStation only).
  `psp`: the plain `.iso`, copied and its CRC32 checked against the DAT. `gc`: an
  `.iso` stays an `.iso` (the report prints the `dolphin-tool convert -f rvz -c zstd
  -l 5 -b 131072` command; warpzone never converts it); a `.gcz` is identified by
  expanding it with `dolphin-tool convert -f iso` into the work folder (the image size is
  read from the GCZ header, and the conversion does not start unless that size plus 5%
  fits in the work folder; a file without the GCZ magic skips the check and is rejected
  by `dolphin-tool`) and the compressed
  file itself is what gets placed, as `<game>.gcz`. An `.rvz` or `.wia` never gets here:
  it is read by its header (see [Compressed Wii and GameCube images](#compressed-wii-and-gamecube-images)). Cartridge systems: a
  `<game>.zip` with the rom under its DAT name, always the same bytes for the same game.
  Names are the DAT's game names. A multi-disc game (`(Disc N)` in the DAT name) of a
  CHD system goes in `<game without disc tag>.m3u/` with every disc and a
  `<game>.m3u` playlist; GameCube discs stay loose files, as in the library. A game is
  placed only when every disc the DAT lists is in the item (`incomplete multi-disc`
  otherwise); a disc that is the only one its DAT lists is a plain game.
- **LibCrypt subchannel files.** Redump ships an `.sbi` with each LibCrypt-protected PAL
  PlayStation disc. A CHD does not keep the subchannel, and DuckStation and Beetle PSX read
  `<disc>.sbi` next to the disc instead. `find_sbi` (pure) takes the `.sbi` in the folder
  of the disc's cue (or of its data track when there is no cue) whose name is the cue's,
  the track's or the DAT game's. A game with one goes in a `<game>.m3u/` folder like a
  multi-disc game, with each disc's `.sbi` named after the disc (`<game>.sbi`), so the
  catalog, the whitelist line and the library UI see one entry. An `.sbi` that names no
  disc of the item goes to review as a file not in any DAT. An identical folder,
  `.sbi` included, is "already in the library"; one without the `.sbi` is a conflict.
- **Names are untrusted too.** A DAT is a file from outside, like an archive, and its
  names become paths: the game name is the entry in `library/roms/<system>/` and each rom
  name a member of the zip. Before anything is prepared, `unsafe_names` checks the entry
  and every game and rom name of the game with `unsafe_entry` (pure). A name that is
  empty, is `.` or `..`, or holds a `/`, a `\` or a control character must be a plain
  file name and is not: the game goes to review with
  `refused: the name '<name>' in the DAT has a path separator` and nothing is written
  under that name, while the other games of the item are filed as usual. The name of a
  rom from `_unverified/` is the file's own and gets the same check (`in the item`).
  Control characters are printed as `?` in the report and in the reason file.
- **RetroAchievements (optional).** With `[inbox] rahasher` set, the RA hash of the
  source (the cue, gdi, iso or rom; a Redump cue of a Dreamcast disc is turned into a
  temporary gdi, which RAHasher needs) is compared with the hash of the result (the
  CHD or zip). If they differ, nothing is placed and the item goes to review. With
  `RA_API_KEY` in the environment the hash is looked up in the console's game list (one
  request per console per run, `User-Agent: warpzone/<version>`) and reported as
  `supported (<title>)`, `hash not linked` or `linked, no achievements yet`. A lookup
  failure is reported by kind only; the key is never printed or put in an error. RA
  never decides whether a game is filed: only a changed hash does.
- **Never overwrites.** The result is copied to `<name>.part`, read back and compared,
  checked against the DAT where it is a plain file, and only then renamed. If the name
  exists: the same content (equal chdman data SHA1, equal zip members, equal SHA1) is
  reported as "already in the library" and the item counts as done; anything else goes
  to review as a conflict. An existing `<game>.m3u/` folder is never merged into.
- **Where the originals go.** Items fully handled go to `inbox/_done/<YYYY-MM-DD>/`.
  Items with anything that could not be placed go to `inbox/_revisar/` with
  `<item>.reason.txt` (no DAT match, ambiguous match, files not in any DAT, incomplete
  multi-disc, conversion or verify failed, RA hash changed, conflict, missing tool,
  unsafe archive, a DAT name that is not a plain file name, not supported yet). Games of such an item that could be placed are
  placed; running the item again reports them as already in the library. Names that are
  taken get ` (2)`, ` (3)`, ... On every `--apply` run, folders of `_done` whose name is a
  date older than 7 days are deleted; nothing else in `_done` is touched, and a symbolic
  link is not followed. Without `--apply` the run only says what it would delete.
- **Whitelists are never edited.** The report prints, per placed game, the whitelist
  line to add (`!/roms/psx/Silent Hill (Europe) (En,Fr,De,Es,It).chd`; Syncthing
  wildcards in names are escaped) and a combined list at the end.
- **Catalog.** After filing, `--apply` updates the catalog index of every system it placed
  something in (the same derivation `warpzone catalog` runs, plus the SHA-1, DAT name, DAT serials,
  filing date and RA hash it knows); a failure is a warning, never a review. See
  [Building and updating it](/warpzone/development/design/library-ui/#building-and-updating-it).
- **Scraping.** After the catalog update, `--apply` scrapes the games it placed with Skyscraper
  when it is configured or found; `--no-scrape` skips it. A failure is a warning, never a review
  and never an exit code. Without `--apply` the report says how many games would be scraped.
  See [Scraping](/warpzone/development/design/scrape/#in-inbox---apply).
- **Run record and webhook.** Every `--apply` run that processed at least one item writes a
  run record, `catalog/inbox/runs/<id>.json`, built from the same results the report is printed
  from; `--notify-url URL` POSTs a JSON summary when some item went to review. Both are
  described in [The run record](#the-run-record) below.
- **Missing tools.** `chdman`, `bsdtar`, `RAHasher` and `dolphin-tool` are located with
  `[inbox]` config keys or PATH (RAHasher only when configured). A tool that an item
  needs and is missing sends that item to review with a clear reason; other items
  continue. A configured path that does not exist is a configuration error.
- **chdman's threads.** `[inbox] chdman_threads` (a whole number, at least 1, optional) is carried
  in `Tools.chdman_threads` and `make_chd` adds `--numprocessors N` to `createcd` and
  `createdvd`; without it chdman counts the cores of the host, which in a container is more than
  its CPU limit. `verify` and `info` run as before. See
  [Resource limits](/warpzone/development/design/inbox-unattended/#resource-limits).
- **Time limit of the tools.** Every call of an external tool (`chdman`, `bsdtar`, `RAHasher`,
  `dolphin-tool`, `nsz`, `orbis-pkg-util`, the listing and info calls as much as the
  conversions) may run for `[inbox] tool_timeout_minutes` (a whole number, at least 1,
  default 240) and is killed after that. The limit is one number for all tools, carried in
  `Tools.timeout` as seconds (and in `SwitchEnv.timeout` for `nsz`), never in a global. A
  killed tool fails only its item, like any other tool failure: the item goes to review with
  "chdman did not finish within 240 minutes" (`inbox_core.timeout_reason`; the archive or
  package name leads the message for `bsdtar` and `orbis-pkg-util`), the run goes on with the
  next item and the lock is released as usual. The tool starts in a session of its own
  (`runners.run_tool`, which `RAHasher` uses as well) and a killed tool is killed with every
  process it started (`SIGKILL` to the group), so nothing is left writing in the work folder
  when the item goes to review; the same happens when the run is interrupted (Ctrl-C
  included), because a session of its own no longer gets the terminal's signals and
  `run_tool` kills it itself. A tool cannot prompt on the terminal, so one that needs an
  answer fails at once instead of waiting. A process of the tool that left the group is out
  of reach, and collecting what is left of the pipes after the kill is bounded, so such a
  process cannot hold the run. Without a limit a hung tool would keep the run
  alive and the lock taken until it counts as stale. 240 minutes is generous for a conversion
  of several GB and stays well under the 12 hours after which a lock is stale. The exception
  is `RAHasher`, whose hash is optional: when it is killed the RA line says the hash is
  unavailable (or, after a conversion, the item fails as for any RAHasher failure), as before.
  Vita3K keeps its own, shorter limits (see [PS Vita packages](#ps-vita-packages)); it starts in
  a session of its own as well, and a Vita3K that runs past its limit is killed with every
  process it started.
- **Arcade (MAME) sets.** See [Arcade sets](#arcade-sets) below; they do not go through
  DAT matching or conversion.
- **Switch games.** See [Switch games](#switch-games) below; they do not go through DAT
  matching or conversion.
- **PS3, Xbox, Xbox 360 and Wii U images.** See [Console disc images](#console-disc-images)
  below; they are identified by signature, not by DAT.
- **3DS images.** See [3DS images](#3ds-images) below; `.3ds` and `.cci` files are
  identified by their NCSD and NCCH headers, not by DAT.
- **Wii and GameCube `.rvz` and `.wia`.** See
  [Compressed Wii and GameCube images](#compressed-wii-and-gamecube-images) below; they are
  identified by the disc header copy they carry, not by DAT and not with `dolphin-tool`.
- **PS4 packages.** See [PS4 packages](#ps4-packages) below; with `orbis-pkg-util` they are
  extracted into a game folder with a shortcut; an update and a DLC are filed beside the
  game they belong to.
- **PS Vita packages.** See [PS Vita packages](#ps-vita-packages) below; Vita3K installs them
  with the zRIF of a key file the user keeps, and the result is filed like a Vita dump.
- **Out of scope.** Vita `.vpk` packages, and PS3 or Xbox content that is not a disc image
  (`.pkg`, `.rap`, an extracted `PS3_GAME` or `default.xbe` folder). Their files are recognised by
  extension or folder and go to review with "not supported by inbox yet". A disc image kept as a
  single `.bin` for several tracks is not identified (the DATs list one file per track).
- **Exit code.** `0` when every item is placed or already in the library; `1` when any
  item went to review; `2` for a usage or configuration error.

## The run record

`--apply` writes `catalog/inbox/runs/<id>.json` for every run that processed at least one item
(an item left alone because it changed in the last 30 seconds does not count; a run with only
those, an empty inbox and a report write nothing). The contract of the file, its id and the
50 records kept is in
[The run record and the library UI](/warpzone/development/design/inbox-unattended/#the-run-record-and-the-library-ui);
this is what an attended run decides:

- `mode` is `attended` and `exit` is `1` when any item needs attention, else `0`.
- An item is `done` (filed in `_done/`), `review` (sent to `_revisar/`) or `held`: its original
  could not be moved after the run was through with it (a read-only share, a permission), so it
  is still in `inbox/` and `moved_to` is `null`. The attended run keeps sending such an item to
  review as before (the report says `REVIEW`, the exit code is `1`, and the next `--apply`
  tries it again); the record only says what happened to the original.
- A game's `outcome` is the report's: `placed`, `already`, `exists` (a different file with that name is
  there) or `review`. Items still being copied are counted in `counts.waiting`, not listed.
- No absolute path, key or URL is written. Paths are relative to the hub root, and any text that
  holds another absolute path, a secret (`RA_API_KEY`, the webhook URL) or a long base64 token
  (a zRIF) is redacted. The handlers redact what they print at the source; the record repeats it
  on every text as a second line.
- Writing the record is not part of the placement: if it fails, the run says so as a warning
  and goes on. The writer deletes the records older than the newest 50.
- The `scrape` note is what `inbox_scrape.after_apply` returns: `done`, `not-finished` (with the
  systems to give to `warpzone scrape --system`) or `skipped` (`--no-scrape`, no Skyscraper),
  and absent when nothing scrapable was placed.

`--notify-url URL` (http or https; it needs `--apply` to do anything) POSTs one JSON object when
some item went to review (`_revisar/`) or stayed in the inbox as above. A run that places games
without review sends nothing, and neither does a report, a run with nothing to process or an
error that stops the run before it starts:

```json
{"source": "inbox", "mode": "attended", "run": "20261113T101500Z",
 "placed": 1, "already": 0, "review": 1, "held": 0,
 "games": ["psx/Example Game (Europe).chd"], "review_items": ["some dump.7z"], "error": null}
```

The lists hold at most 20 names. The sender is the one `prune` uses (`notify.py`): the URL is
never printed (it may carry a token), a failure is a warning and never a failure of the run, and
the payload holds no API key and no path. The decision to send (`should_notify`) and the payload
(`notify_payload`) are pure.

The decisions (`skip_name`, `unsafe_member`, `unsafe_entry`, `select_purge`, `plan_units`,
`destination_verdict`, `whitelist_line`, `resolve_refs`, `unsupported_reason`,
`unverified_reason`, `unverified_match`,
`match_dat`, `group_discs`, `parse_dat`) are pure and unit-tested; conversions are
tested with stand-in `chdman` and `RAHasher` scripts, and one test runs the cartridge
flow end to end with no external tool.

## Roms no DAT knows

Homebrew and aftermarket releases are in no DAT, so by default they go to review as "no DAT
match". The user can say that a file is such a rom by dropping it in
`inbox/_unverified/<system>/`, where `<system>` is the library folder name (`nes`, `snes`,
`gba`, ...). The folder is only a permission to file what no DAT explains; it never skips
verification.

- **Items.** `scan_unverified` lists the files and folders inside each
  `_unverified/<system>/` (hidden and junk names, symbolic links and anything directly in
  `_unverified/` that is not a system folder are no items; the report names the stray ones). Each is
  processed like any item, with its system folder name as `unverified`, and shown as
  `_unverified/<system>/<name>`. Its original goes to `_done/<date>/` or `_revisar/` under its
  own name like the others.
- **A DAT wins.** The item goes through the whole pipeline first: the kind handlers, then the
  DAT lookup in every system. A file a DAT matches is filed the normal way, under the DAT's
  game name and into the DAT's system, whatever folder it was in, and is not marked.
- **What is left.** Only the files nothing explained (the "no DAT match" ones) are looked at, by
  `unverified_reason` (pure). A file is refused, with a reason that goes to `_revisar/`, when
  its system is not one of the cartridge systems `inbox` stores
  (`UNVERIFIED_EXTENSIONS` in `inbox_core.py`: `nes`, `snes`, `n64`, `gb`, `gbc`, `gba`,
  `genesis`, `mastersystem`, `gamegear`; disc systems, `psp`, `gc` and the rest are not),
  when its extension is not one the system accepts (`nes` `.nes`; `snes` `.sfc`, `.smc`; `n64`
  `.z64`, `.n64`, `.v64`; `gb` `.gb`; `gbc` `.gbc`, `.gb`; `gba` `.gba`; `genesis` `.md`,
  `.gen`, `.smd`, `.bin`; `mastersystem` `.sms`; `gamegear` `.gg`), when it is empty, or, for
  `nes`, when it does not start with the iNES magic `NES\x1a`. These are the only header
  checks the code knows; the other systems are checked by extension alone. Sidecars (text,
  images, ...) travel with the item and are ignored, as always.
- **Filing.** A file that passes becomes a `Unit` with `unverified` set, whose match is
  built by `unverified_match`: a game with one rom that is the file itself (its size, CRC32
  and SHA-1). A loose file, or a file in a folder item, gives the game its own name without
  the extension and the rom its own base name. When the item is an archive and exactly one
  of its files is a rom to file, the game takes the archive's name without its extension (the
  curated, No-Intro-style name the library follows; the member's name is often bare) and
  the rom is named `<archive name><the member's extension>`, so the zip and its member
  match as for a DAT game. An archive with several roms keeps the base name of each member.
  Everything after that is the normal path of a cartridge
  game: `<name>.zip` in `library/roms/<system>/` holding the rom under its own file name, the
  copy to `.part`, the read-back and the size/CRC32 check, the re-check right before the
  rename, no overwrite (the same content is "already in the library", anything else is a
  conflict and goes to review) and the RA hash of the source and the result when
  `[inbox] rahasher` is set.
- **Report.** The unit line reads `would place (unverified)` without `--apply`, and
  `placed` (or `already in the library`) with it, both followed by
  `, not in any DAT (filed from _unverified)`.
- **Catalog.** The catalog game gets the flag `unverified` in `flags` and no `dat` (see
  [The catalog index](/warpzone/development/design/library-ui/#the-catalog-index)).

## Switch games

A Switch dump is identified by its TitleID and version, not by a DAT, named after
blawar/titledb and filed under the library's existing conventions. The code is in
`warpzone/inbox_switch.py`, a second handler in `inbox.kind_handlers()`.

- **Which items.** A top-level `.nsp`, `.nsz`, `.xci` or `.xcz`, or a folder or archive
  (`.zip` by its directory, `.7z` and `.rar` by `bsdtar -tf`) holding at least one. The item
  is taken whole: its Switch files are placed, and any other file in it (sidecars aside) is
  named in the review reason. Archives are unpacked into the work folder with the same
  safety checks as the rest of the inbox.
- **Identification.** The CNMT is inside an encrypted NCA, so `nsz -i --depth 3 --keys
  <keys> <file>` reads it: the output is parsed for the `Cnmt` blocks (`titleId`, and
  `version` and `titleType`, both in hex). `nsz` also prints key material in its report, so
  its output is never shown: lines mentioning a key are dropped from anything quoted.
  A file with several CNMTs goes to review; a CNMT `titleType` that disagrees with the
  TitleID is a review; the file wins over a different `[TitleID][vN]` in its name (noted).
  Without `nsz` or the keys file, an `.nsp` takes `[TitleID][vN]` from its name and says
  so; an `.nsz` is then reviewed. hactool is not used.
- **Kind.** From the TitleID: ends in `000` game, `800` update, otherwise DLC. The game of
  an update is its id minus `0x800`; of a DLC, `(id & ~0xFFF) - 0x1000`.
- **Names.** Game: `roms/switch/<Title> [<TitleID>][v<N>][BASE].nsp`. Update:
  `extras/switch/updates/<Title>/<Title> [<UpdateTitleID>][v<N>][UPD].nsp`. DLC:
  `extras/switch/dlc/<Title>/<Title> - <DLC> [<DlcTitleID>][v<N>][DLC].nsp`. `<Title>` is
  the game's name in the first titledb region file that has it (`US.en.json`, then the
  other English ones, then the rest; a full-game entry beats a demo). Normalization:
  typographic quotes and dashes become ASCII, trademark signs go, `:` becomes ` - `, path
  characters (`\ / | * ? < > "` and controls) are dropped or replaced, spaces are collapsed,
  and a word written in capitals is capitalized unless it is a roman numeral, a one-letter
  word or a known short acronym (`DLC`, `BGM`), and the small words of a title (`of`, `the`)
  go to lower case. A DLC's own name has the game's title removed from its start (also the
  title without its ` - subtitle`). Names are cut to fit 255 bytes, keeping the `[...]` tail.
- **Bundle parts.** A titledb entry whose `ids` lists several ids is a bundle. If its own
  id comes first it is the pack itself and is left out; the other ids, in listed order,
  are its parts. A DLC with no standalone entry in the region file that knows it is named
  `<Bundle> (part N of M)`. A standalone entry (no `ids`, or only its own) names the DLC
  itself.
- **Compressed files.** `.nsz` is decompressed with `nsz -D --keys <keys> -o <work>` only
  with `--apply`; the report only reads it with `-i`. Before `-D` the work folder must have
  free space of the exact size of the `.nsp` plus 5%; a smaller folder is a review. The
  size is computed from the headers alone (`inbox_switch.nsp_size`, never the whole file),
  the way `nsz -D` builds it without `-F`: the new header takes the room the old one took
  (the names keep their length, `.ncz` becomes `.nca`), the members follow with no gap, the
  other members as they are and each `.ncz` as the NCA it holds. That NCA is the first
  0x4000 bytes (stored plain) plus the sizes of the sections in the `NCZSECTN` table that
  follows them. A `.nsz` whose headers cannot be read this way (or whose first section does
  not start right after the NCA header) falls back to the size of the `.nsz`, a lower bound
  (the `.nsp` is never smaller), and the review says so. `nsz` exits 0 on some damaged
  inputs, so the output is accepted only if `nsz` exited 0 and printed `[DONE]`, its PFS0
  header parses (magic, entry table, member bounds), and `nsz -V` passes
  `inbox_switch.judge_nsz_verify(exit code, output)`, a pure function that returns
  (accepted, note). It accepts when `nsz` exits 0, prints no `CORRUPTED`, `MISMATCH`,
  `Traceback` or error line, and reports at least one NCA, each of them `[VERIFIED]` or
  `[TICKETLESS]`. `nsz` does not hash-check the NCAs of a ticketless dump and prints
  `[TICKETLESS]` for them (`[NSP SHA256]` lines only appear when it compares against an
  original file, so they count for nothing). All `[VERIFIED]` is accepted with no note; any
  `[TICKETLESS]` (alone or mixed with `[VERIFIED]`) is accepted with the note
  `not hash-checked by nsz: ticketless dump` in the item's report. Anything else is a review
  and the partial output is deleted with the work folder. Only the `.nsp` reaches the library. `.xci` and `.xcz` are reviewed with the
  reason that no tool converts them cleanly.
- **Never overwrites.** The library is indexed once per run by `[TitleID][vN]` in file
  names (`roms/switch`, `extras/switch/updates`, `extras/switch/dlc`). The same TitleID and
  version, under any name, with identical bytes (size, then SHA-256) is "already in the
  library"; different bytes are a review. Placement goes through `<name>.part`, read back
  and renamed, as everywhere else.
- **Whitelists.** A game prints its `!/roms/switch/...` line. Updates and DLC print none;
  the report says once that they are usually whitelisted by folder
  (`!/extras/switch/updates`, `!/extras/switch/dlc`).
- **Configuration.** `[inbox] nsz` (the tool; PATH otherwise), `titledb_dir` (a clone of
  blawar/titledb), `switch_keys` (default `~/.switch/prod.keys`). A configured
  `titledb_dir` or `switch_keys` that does not exist stops the run (exit 2); unset or absent
  ones only make the affected files go to review or fall back to file names. The titledb
  region files are indexed into small JSON caches under `$XDG_CACHE_HOME/warpzone/titledb`,
  keyed by size and modification time. The keys file is never opened by warpzone.

## Console disc images

PS3, Xbox, Xbox 360 and Wii U images have no Redump or No-Intro entry, so they are identified by
signature. The code is in `warpzone/inbox_console_iso.py`, which is both a kind handler in
`inbox.kind_handlers()` and the one `UnmatchedHandler` in `inbox.unmatched_handlers()`.

- **Signatures.** Reads are bounded (a few KiB at fixed offsets, directories up to 1 MiB,
  a PARAM.SFO up to 64 KiB); an image is never read whole to be identified.
  - *XDVDFS* (Xbox, Xbox 360): at each candidate partition offset (0, 0xFDA0000 XGD2,
    0x2080000 XGD3, 0x18300000 Redump Xbox) the volume descriptor at +0x10000 must start
    with `MICROSOFT*XBOX*MEDIA`; root sector (u32 at +0x14) and size (u32 at +0x18) give
    the root directory, whose entries form a binary tree (`u16 left`, `u16 right` in
    4-byte units, `u32 sector`, `u32 size`, `u8 attributes`, `u8 name length`, name) that
    is walked with a visited set. The root executable decides: `default.xbe` at offset 0
    is Xbox, `default.xex` at 0, 0xFDA0000 or 0x2080000 is Xbox 360, `default.xbe` at
    0x18300000 is a full Xbox image that is not placed. Any other combination is not
    recognised.
  - *Wii U* (`.wua`, a Cemu ZArchive): the last 16 bytes of the file are a big-endian u64
    total size, the u32 version `0x61bf3a01` and the u32 magic `0x169f52d6` (the end of a
    144-byte footer), and the size must equal the file's own. A `.wua` name is only ever
    judged this way and no other name is judged as a ZArchive.
  - *PS3*: an ISO9660 primary volume descriptor at sector 16; the root must hold
    `PS3_DISC.SFB` and a `PS3_GAME` directory with `PARAM.SFO`, whose `TITLE_ID` and `TITLE`
    are read.
- **Claiming.** The handler claims an item (a file, a folder, or an archive listed by
  `zipfile` or `bsdtar -tf`) when a file name ends in `.dec.iso`, `.xiso.iso`, `.xiso` or `.wua`.
  Names alone never identify: a claimed file whose content is not recognised goes to
  review. Every `.iso` or `.xiso` in a claimed item is sniffed; other non-sidecar files are
  named in the review reason.
- **Plain `.iso`.** A plain `.iso` must keep going through the DATs, so it is not claimed.
  After `identify()` the files left unmatched are offered to `unmatched_handlers()`; this
  handler sniffs the unmatched `.iso` files and places the ones that are PS3, Xbox or
  Xbox 360 (or sends a full Xbox image to review) instead of reporting "no DAT match". A
  PS2, PSP or GameCube image that a DAT matches is therefore never read for a signature,
  and one that no DAT matches and no signature fits ends in review as before. The cost is
  that an unmatched plain `.iso` is hashed once, like any other unmatched file.
- **Placement.** `library/roms/ps3/`, `roms/xbox/`, `roms/xbox360/roms/` (the folder
  EmuDeck's ES-DE system for the 360 reads) and `roms/wiiu/roms/` (where Cemu's game path
  points), under the file's own name. It is copied to
  `<name>.part`, read back and renamed; an existing name with the same size and SHA1 is
  "already in the library", a different one a review (nothing replaced). The report shows
  `detected: <platform>` (for PS3 also the TITLE_ID and title), the path and the whitelist
  line (`!/roms/xbox360/roms/<name>`).
- **Full Xbox images.** A game partition at 0x18300000 is flagged, never placed: xemu
  cannot boot it. The review reason says to convert it to XISO with extract-xiso.
- **PS3 emulator.** A PS3 `.iso` needs ES-DE's alternative emulator `RPCS3 ISO
  (Standalone)`; the system default is a shortcut launcher. `[inbox] gamelist_ps3`
  (default `<root>/frontend/es-de/gamelists/ps3/gamelist.xml`) gets
  `<altemulator>RPCS3 ISO (Standalone)</altemulator>` for the game, written with the same
  helpers as the arcade gamelist (`inbox_mame.update_gamelist_entry`, atomic write,
  backup under `tools/logs/gamelist-backups/` before the run's first change). Report mode prints "would add". It
  is also done for a game that was already in the library. If the gamelist cannot be
  updated the item goes to review and a rerun retries it.

## 3DS images

A `.3ds` or `.cci` file has no DAT here, so it is identified by its headers. The code is in
`warpzone/inbox_n3ds.py`, a kind handler in `inbox.kind_handlers()` (it is not an
`UnmatchedHandler`).

- **Claiming.** The handler claims an item (a file, a folder, or an archive listed with
  `inbox_console_iso.names_in`) when a file name ends in `.3ds` or `.cci`, case-insensitively.
  `.3dsx` and `.cia` are not claimed. Names alone never identify: a claimed file whose headers are
  not valid goes to review. Other non-sidecar files of a claimed item are named in the review
  reason.
- **Verdict.** `judge(head, ncch, file_size)` is pure and reads nothing; `detect` reads the first
  0x200 bytes and the 0x200 bytes at the offset of partition 0, never more.
  - *NCSD*: the magic `NCSD` at 0x100; the image size (u32 little-endian at 0x104, in media units
    of 0x200 bytes) must be non-zero and not bigger than the file (trailing padding is
    allowed, a truncated dump is not); partition 0 (the offset u32 at 0x120, in media units) must
    exist and leave room for an NCCH header inside the image.
  - *NCCH*: the magic `NCCH` at +0x100 of partition 0; the flags byte at +0x18F must have the
    NoCrypto bit (0x04). Without it the status is `encrypted`.
  - Statuses are `ok`, `encrypted` and `invalid`. Both of the last two go to review with a
    reason; an encrypted image says that Azahar opens only decrypted images.
- **Placement.** `library/roms/n3ds/<stem>.cci`, for a `.3ds` as well as for a `.cci`: the
  file is renamed on placement and its bytes are not changed. It uses the same copy as the console
  images (`inbox_console_iso.place_file`: `<name>.part`, `inbox.copy_verified`, rename, a re-check
  right before the rename), so nothing is replaced: an existing name with the same size and SHA1
  is "already in the library" and a different one a review (`inbox.destination_verdict`). In report
  mode an existing name is "already exists" and nothing is written, not even the folder. The
  report shows `detected: Nintendo 3DS image (decrypted)`, the path and the whitelist line
  (`!/roms/n3ds/<name>.cci`).
- **One target name.** Two files of one item that would be filed under the same name (`X.3ds`
  and `X.cci`) are not placed together: the second goes to review.

## Compressed Wii and GameCube images

The WIA and RVZ containers of Dolphin start with a fixed header followed by the first 0x80
bytes of the disc, so an image is identified without expanding it. The code is in
`warpzone/inbox_dolphin.py`, a kind handler in `inbox.kind_handlers()`.

- **Claiming.** The handler claims an item (a file, a folder, or an archive listed with
  `inbox_console_iso.names_in`) when a file name ends in `.rvz` or `.wia`, case-insensitively.
  Names alone never identify: a claimed file whose header is not valid goes to review. Other
  non-sidecar files of a claimed item are named in the review reason. `.gcz` is a different
  format (no copy of the disc header) and keeps the `dolphin-tool` path of the DAT pipeline.
- **Verdict.** `judge(head, file_size)` is pure; `detect` reads the first 0xD8 bytes and
  nothing else. Big-endian fields: the magic `RVZ\x01` or `WIA\x01` at 0; the size of the
  whole file at 0x2C (0 means not recorded, otherwise it must equal the real size, which
  catches a copy that stopped early); the disc type u32 at 0x48 (1 GameCube, 2 Wii); the disc
  header at 0x58: game ID (6 bytes, `[A-Z0-9]`), the Wii magic `5D1C9EA3` at +0x18, the
  GameCube magic `C2339F3D` at +0x1C, the title (64 bytes) at +0x20. The disc type and the
  magic must agree and exactly one magic may be present. Anything else is `invalid` with a
  reason (no magic, header cut short, size mismatch, unknown disc type, magic mismatch, bad
  game ID).
- **Placement.** `library/roms/wii/` or `library/roms/gc/` (the ES-DE systems Dolphin serves),
  named `<name> [<GAME ID>]` with the original extension in lower case (`target_name`). The
  name is the file's stem without a trailing `[<GAME ID>]` of that game (any case), with
  control characters and `/ \ * ? " < > |` dropped, `:` written as ` -` and blanks collapsed.
  When that leaves nothing, or only the game ID, the disc header's title stands in: it keeps
  only ASCII and is cleaned the same way; an empty title leaves `[<GAME ID>]`. Set names
  (`Game (USA) (En,Fr,Es)`) read better than header titles, which often have no spaces. The
  file is copied unchanged with `inbox_console_iso.place_file` and `destination_verdict`, as
  for 3DS images: an existing name with the same size and SHA1 is "already in the library", a
  different one is a review, and report mode writes nothing.
- **No DAT.** The Redump DAT of a compressed image cannot be matched without hashing the
  expanded disc, which needs `dolphin-tool`. The name therefore comes from the file and the
  header, and a GameCube `.rvz` is no longer named after the DAT; the same game dropped under
  two file names is filed twice. Two files of one item filed under the same
  name: the second goes to review.

## PS4 packages

A PS4 `.pkg` is recognised by `inbox_console_iso` (magic `\x7fCNT`, content id at 0x40,
title id `CUSAnnnnn`) and handled by `warpzone/inbox_ps4.py`. Current shadPS4 has no package
installer: it runs extracted game folders found in its install directories, and ES-DE
lists only the `.desktop` files of `roms/ps4/shortcuts`, whose command on the consoles is
`%EMULATOR_SHADPS4% %ROM%` (EmuDeck's `shadps4.sh`).

- **Tool.** `[inbox] orbis_pkg_util` (PATH otherwise): `orbis-pkg-util extract -q -o <dir>
  <pkg>`. Reported in the `tools:` line and by `doctor` as an optional inbox tool. Without it
  the `.pkg` is placed as it is in `roms/ps4/` with a note saying so (the earlier behaviour).
- **Kind.** The header says what the package is before anything is unpacked: the u32
  big-endian content type at 0x74 (`0x1A` game data; `0x1B` additional content and `0x1C`
  additional license, a DLC with no data) and the content flags at 0x78 (`0x100000` or
  `0x40000000` mark a patch). The content id (at 0x40) and the PFS image size (u64 at 0x418,
  0 for a DLC without data) are read from the same header. After extraction the `CATEGORY` of
  `sce_sys/param.sfo` decides again (`gd*` game, `gp` update, `ac*` DLC) and must agree with
  the header, and its `TITLE_ID` must equal the header's. A package of another content type
  goes to review.
- **Game folder.** The extraction goes to a temporary folder in the local work folder
  (`work_dir`), which must have the package's size free. It must hold `eboot.bin` and
  `sce_sys/param.sfo` and no symbolic link. The folder is then copied file by file with a
  read-back (`inbox.copy_verified`) to `library/roms/ps4/games/<CUSAxxxxx>.part/` and
  renamed, so the destination never holds a partial game and a stale `.part` of an earlier
  run is replaced. A game folder already in the library is reported as "already in the
  library", not compared and never touched; the package is not unpacked.
- **Shortcut.** `library/roms/ps4/shortcuts/<Title>.desktop`, written the same way (`.part`,
  rename), never over another file. The name comes from the `TITLE` of `param.sfo` (trademark
  signs and quotes dropped, `:` as ` -`, only letters, digits, marks and `-_.,()[]!+#`
  kept, 100 characters at most). A file of that name that already runs this game is kept;
  one that runs another game gets ` [CUSAxxxxx]` in its name. A game that was already in
  the library gets a missing shortcut from the `TITLE` of its own `param.sfo`.
  ```
  [Desktop Entry]
  Type=Application
  Name=<Title>
  Exec=shadps4 "/home/deck/Emulation/roms/ps4/games/<CUSAxxxxx>/eboot.bin"
  ```
  EmuDeck's launcher greps the `Exec=` line of the shortcut, takes the one quoted string
  as the game and runs `Shadps4-qt.AppImage -d -g <path>`. In the Qt launcher `-d` is the
  alias of `-e default`: the version selected in its settings. Nothing expands `~` or
  `$HOME` on that path, so it is written out under the SteamOS home (`DEVICE_LIBRARY`).
- **Update.** shadPS4 overlays a sibling folder `<game folder>-UPDATE` on the base game, file
  by file and read-only, so an update becomes `library/roms/ps4/games/<CUSAxxxxx>-UPDATE/`,
  holding what `orbis-pkg-util extract` produces from the patch package (copied through
  `.part` and renamed like a game). The base game `games/<CUSAxxxxx>/` must already be in the
  library, else the package goes to review ("install the game first"; nothing is extracted).
  The extracted `param.sfo` must have the header's `TITLE_ID` and `CATEGORY` `gp`, and an
  `APP_VER`. When `games/<CUSAxxxxx>-UPDATE/` already exists, `update_action` compares the
  `APP_VER` of the package with the one in the library copy's `sce_sys/param.sfo`, numerically
  per dot part (`01.09` is 1.9): the same version is "already in the library" and nothing is
  touched, an older one goes to review ("older than the update in the library, vX"), a newer one
  replaces it. A library copy with no readable `APP_VER` goes to review.
  This replacement is the one exception to "nothing in the library is overwritten". It is
  acceptable because the original package of the old update was filed in `inbox/_done` by the
  run that placed it. The replacement is built so the library holds a complete update at every
  moment: the new folder is copied (with read-back) to `<CUSAxxxxx>-UPDATE.part`; right before
  acting, the library copy is read again and must still have the version that was compared
  (else review); the old folder is renamed to `.old`, `.part` is renamed into place and `.old`
  is deleted. A failing rename puts the old folder back, and a `.old` found beside a missing
  update (a run that stopped between the two renames) is restored before comparing. The unit
  note says which versions were replaced. A report without `--apply` cannot know the new
  `APP_VER` before extracting, so it says it would extract the package and compare, and shows
  the version in the library.
- **DLC.** shadPS4 looks for a DLC in `<addon_install_dir>/<TITLE_ID of the running game>/
  <entry>/`, where an entry holds `sce_sys/param.sfo` (`CATEGORY` starting with `ac`, and a
  `CONTENT_ID`) and the license (`sce_sys/license.info`, else `license.dat`); an entry with
  no data still loads. A DLC therefore becomes
  `library/roms/ps4/dlc/<CUSAxxxxx>/<entitlement label>/`, the label being the part of the
  content id after its second `-` (`UP9000-CUSA00900_00-SPEXPANSIONDLC03` gives
  `SPEXPANSIONDLC03`; `entitlement_label` accepts only `[A-Za-z0-9_]`, anything else goes to
  review before extraction). **Which game.** If the inbox item is a folder whose name is
  exactly a title id (`inbox/CUSA03173/`, at any depth inside it; `item_title_id`, fed by
  `ConsoleIsoHandler.process`, which sees the item), the DLC is filed for that game, whatever
  title id the DLC itself carries (`UP9000-CUSA00900_00-SPEXPANSIONDLC03` in `inbox/CUSA03173/`
  goes to `dlc/CUSA03173/SPEXPANSIONDLC03/`). Any other item (a folder with another name, an
  archive, a loose file) files the DLC for its own title id: the header's, which `param.sfo`
  must confirm. Either way `games/<target>/` must already be in the library (only the folder
  is checked, not its contents) or the DLC goes to review naming the missing title id and the
  `inbox/<CUSAnnnnn>/` folder option; the whitelist line is `!/roms/ps4/dlc/<target>`. An
  entry folder already in the library is "already in the library", not compared and never
  touched. The extracted `param.sfo` must have the header's `TITLE_ID`, a `CATEGORY`
  starting with `ac` and a `CONTENT_ID` equal to the header's, and the tree holds no symbolic
  link. A DLC without data (PFS size 0 in the header) makes `orbis-pkg-util` write `sce_sys`
  and then exit 1 ("cannot open outer PFS ... source buffer is too short"); inbox accepts
  that exit status only when the header's PFS size is 0 and `sce_sys/param.sfo` was written,
  and any other failure is the usual extraction failure. The folder is placed with the
  `.part` and rename approach.
- **No shortcut.** Updates and DLC get no shortcut: the game's own one starts the game, and
  shadPS4 finds the update and the DLC beside it. The disk space check is the one for games.
- **Whitelists.** A game has two lines: `!/roms/ps4/games/<CUSAxxxxx>` and
  `!/roms/ps4/shortcuts/<Title>.desktop`. An update has `!/roms/ps4/games/<CUSAxxxxx>-UPDATE`
  and a DLC `!/roms/ps4/dlc/<CUSAxxxxx>` (the title folder, so one line serves all its DLC).
  A report without `--apply` does not unpack anything, so for a game it cannot name the
  shortcut and prints only the first line.
- **Install directories.** The consoles must list `~/Emulation/roms/ps4/games` as an install
  directory of shadPS4, and `~/Emulation/roms/ps4/dlc` as its `addon_install_dir`. See
  [shadPS4 install directories](/warpzone/development/design/profiles/#shadps4-install-directories)
  and [the EmuDeck guide](/warpzone/guides/emudeck/#shadps4-install-directory).
- **Catalog.** `games/<CUSAxxxxx>-UPDATE/` and `dlc/<CUSAxxxxx>/` are parts of the game
  `<CUSAxxxxx>`, not games of their own (see [the library UI](/warpzone/development/design/library-ui/)).
- **In the container.** `orbis-pkg-util` is in the hub image, at `/usr/local/bin/orbis-pkg-util`.
  It is a Rust program, MIT or Apache-2.0, published on crates.io (the project is
  `videobitva/orbis`; it has no release binaries and no versioned tag, only the crate). The
  image builds the published crate in its own stage: the download must have the SHA-256 the
  Dockerfile pins, and `cargo build --release --locked` uses the `Cargo.lock` packaged in the
  crate, which pins every dependency with its checksum. Only the binary reaches the last stage.
  The program has no `--version` option, so `doctor` only looks for it (as on a computer) and
  the image check runs `--help`.

## PS Vita games

A decrypted Vita dump is recognised and filed by `warpzone/inbox_vita.py`, a kind handler
registered after the console image handler (so a `.pkg` or anything that handler claims keeps
its outcome; a Vita `.pkg` is installed and handed back here, see
[PS Vita packages](#ps-vita-packages)). Vita3K runs games from `ux0/app/<TITLEID>`; ES-DE lists only `.psvita` files,
whose system definition launches `%EMULATOR_VITA3K% -r %INJECT%=%BASENAME%.psvita`, and
EmuDeck's `vita3k.sh` reads the file's content as the title id (`Vita3K -Fr <id>`).

- **Claim.** A folder or archive (`.zip`, or `.7z` and `.rar` with `bsdtar`) whose file list
  has `sce_sys/param.sfo` and `eboot.bin` in the same folder, either at the top or one folder
  below it (`game_roots`, pure). Other layouts (`PS3_GAME/PARAM.SFO`, `PSP_GAME/PARAM.SFO`) are
  not claimed. A folder holding archives is not opened.
- **Identify.** `parse_sfo` (shared with PS3 and PS4) on `sce_sys/param.sfo`: `TITLE_ID` must
  fully match `PCS[A-I]\d{5}`, and `CATEGORY` decides (`gd`, `gdc` game; `gp` update; `ac`
  DLC; anything else is not a game). Anything but a game, a title id that is not a Vita one,
  several games in one item, a symbolic link inside the game, or a dump that holds
  `sce_pfs` (Vita3K has to install it) goes to review with the reason. `.vpk` files keep their
  earlier outcome.
- **Vitamin.** A dump whose game folder holds `sce_module/steroid.suprx` (`is_vitamin`, pure,
  case-insensitive, relative to the game root) is a Vitamin dump. Vita3K rejects that module,
  so it ALWAYS goes to review, checked before anything else about the dump (also in report
  mode), with a reason that says so and suggests a clean dump (a `.pkg` with its `work.bin`, or
  NoNpDrm). It is never placed even if the game might start, and nothing already in the
  library is touched: no game folder, no launcher.
- **Game folder.** The dump (the top folder of an archive is dropped) is copied file by file
  with a read-back (`inbox.copy_verified`; hidden and junk names are skipped) to
  `library/roms/psvita/app/<TITLEID>.part/` and renamed, as for PS4. A folder already in the
  library is reported as "already in the library", not compared and never touched.
- **Launcher.** `library/roms/psvita/<Title>.psvita`, named like the PS4 shortcut
  (`shortcut_name`), holding only the title id with no newline, written through `.part` and a
  rename and never over another file. One that holds this id is kept; one that holds another
  id gets ` [<TITLEID>]` in the new name. A game already in the library gets a missing
  launcher from the `TITLE` of its own `param.sfo`.
- **Licence.** Vita3K runs the games of `ux0/app/<TITLEID>` whose `eboot.bin` is still an
  encrypted SELF only with the licence `ux0/license/<TITLEID>/<CONTENT_ID>.rif`
  (`get_license`, `packages/src/license.cpp`, loads that path by the content id of the game's
  `param.sfo`; a missing or unreadable file leaves the key all zeros). Inbox files the licence
  in `library/roms/psvita/license/<TITLEID>/<CONTENT_ID>.rif`:
  - *Sources* (`license_sources`, pure): `sce_sys/package/work.bin` of the game (NoNpDrm,
    the same 512-byte structure as a `.rif`), or a `*.rif` directly in the game folder or
    beside it (the item's top), or in `license/<TITLEID>/` at either place.
  - *Check* (`parse_license`, `choose_license`, pure): exactly 512 bytes; the content id is
    the NUL-terminated text of the 0x30 bytes at offset 0x10 (`SceNpDrmLicense.content_id`)
    and must be `XXnnnn-PCS[A-I]nnnnn_nn-` plus 16 characters of `[A-Z0-9]`, so it is safe as
    a file name; its title id (characters 7 to 15, as Vita3K's `copy_license` takes it) must be
    the game's; and, when `param.sfo` has a `CONTENT_ID`, it must be that one, because the
    file name is what Vita3K looks up. Several candidates are fine only when they are the same
    bytes; different licences, a symbolic link or a file that is not a licence go to review.
    The file is filed under the content id it holds, not the name it came with.
  - *Placement:* a `.part` write read back and renamed, never over a file. The library copy is
    compared before anything is placed: the same bytes are "already there", other bytes make
    the item go to review without placing the game. A game already in the library gets a
    missing licence like it gets a missing launcher. A licence that comes along when none is
    needed is filed too.
  - *Does the game need one?* (`eboot_needs_license`, pure, on the first 64 KiB of
    `eboot.bin`.) Read from Vita3K (`vita3k/packages/src/sce_utils.cpp`): `decrypt_fself`,
    called for every module including the eboot (`modules/module_parent.cpp`, with
    `license.rif[title_id].key`), returns the buffer untouched when `is_fself_encrypted` is
    false, which is `seg_infos->encryption != 2` for the first segment info found at
    `section_info_offset` (offset 0x58 of the header, `encryption` is the u64 at +0x18), and
    fails with `LOG_ERROR("No klic provided for encrypted App")` when the self is encrypted,
    `AppInfoHeader.self_type == SelfType::APP` (0x08, a u32 at +12 of the app info at offset
    0x38) and the klic is all zeros (`Invalid SELF: file is either not a SELF or is still
    encrypted` is the answer to a file without the `SCE\0` magic). The magic alone is not the
    test: Vita3K's own "decrypt selfs" writes a SELF with the same magic and `encryption = 2`
    (`decrypt_fself`'s output header). So a licence is needed exactly when the file starts with
    `SCE\0`, is encrypted and is an APP. A file with another magic needs none. A SELF header
    that is shorter than 0x80 bytes or points outside the bytes read cannot be judged and goes
    to review. A game that needs a licence and has none goes to review, and nothing is placed.
- **Whitelists.** Up to three lines per game: `!/roms/psvita/app/<TITLEID>`,
  `!/roms/psvita/license/<TITLEID>` (only when there is a licence) and
  `!/roms/psvita/<Title>.psvita`. Nothing is unpacked by a tool, so the report mode prints
  them all.
- **On the console.** The folders must be linked into Vita3K's own storage, see
  [Title links](/warpzone/development/design/title-links/).

## PS Vita packages

A Vita `.pkg` (as PlayStation Store packages and NoPayStation hand them out: both layers
encrypted, the licence apart, as a zRIF) is claimed by `inbox_console_iso` like every `.pkg`
and handled by `warpzone/inbox_vita_pkg.py`, which installs it with Vita3K and hands the
decrypted game folder and its licence to `inbox_vita.install`. warpzone never downloads
packages or keys.

- **Header.** Read before anything runs, and in report mode too, from the parts of the file
  that are not encrypted (big-endian, as `pkg2zip` and Vita3K's `PkgHeader` read them): magic
  `\x7fPKG` at 0, type 2 (PSP/Vita; PS3 is 1) at 0x06, the extended header magic `\x7fext` at
  0xC0, the metadata offset (u32 at 0x08) and entry count (u32 at 0x0C), the total size (u64 at
  0x18) and the content id (0x30 bytes at 0x30, NUL padded). The metadata entries are `u32 id,
  u32 size, data`; entry 2 is the content type, entry 14 the offset and size (two u32) of the
  `param.sfo`, which is stored unencrypted in the package. Reads are bounded: 0x100 bytes of
  header, up to 64 KiB of metadata (at most 64 entries) and a `param.sfo` up to 64 KiB.
  `read_pkg`, `parse_header`, `parse_meta` are pure on bytes or a file object.
- **Kind.** Content type 0x15 is a Vita app, 0x16 DLC, 0x1F a theme, 0x18 and 0x1D PlayStation
  Mobile. Any other `\x7fPKG` (PS3, or the PSP and PS1 types 0x06, 0x07, 0x0E, 0x0F, 0x10) is
  not this module's and keeps the "not supported by inbox yet" outcome. DLC, a theme or PSM goes
  to review with the reason. For an app (`pkg_problem`, pure): the content id must be a Vita one
  (`XXnnnn-PCS[A-I]nnnnn_nn-` and 16 characters), the total size must equal the file's size
  (a truncated download goes to review), the `param.sfo` must be readable with the `TITLE_ID`
  of the content id and the same `CONTENT_ID` (one without it goes to review: the installed
  game could not be checked against the package), and its `CATEGORY` decides as
  for dumps: `gd` or `gdc` go on, an update (`gp`) or anything else goes to review. Nothing is
  run for a package that goes to review.
- **zRIF.** `[inbox] vita_zrif` names a file the user keeps (a configured path that is not a
  file is a configuration error). It holds either lines of `CONTENT_ID<TAB>zRIF` (blank lines
  and `#` comments skipped; any other line makes the file unreadable, and the reason names the
  line by its number only), or a TSV whose first line names the columns `Content ID` and `zRIF`
  (NoPayStation's `PSV_GAMES.tsv`), of which only those two columns are read; the links and
  everything else are ignored (`parse_zrif_table`, pure). A field may be quoted with double
  quotes, in the header too. A row whose zRIF is empty, `MISSING`
  or `NOT REQUIRED` gives no key; a content id listed twice with different zRIFs is refused.
  The file is read once per run. A package whose content id has no zRIF there, or with no file
  configured, goes to review saying so. A zRIF is the base64 of a zlib stream compressed with
  a preset 1024-byte dictionary (`zrif_dict` of `pkg2zip_zrif.c`, mmozeiko/pkg2zip; the
  stream's DICTID is its adler32, 0x627d1d5d); its base64 padding may be left out.
  `decode_zrif` (pure) checks the zlib header for
  that dictionary, decompresses with a bound and requires exactly 512 bytes; `license_for`
  then reads it with `inbox_vita.parse_license` and requires the package's content id. A PSM
  licence (1024 bytes) or the licence of another game goes to review.
- **Secrecy.** A zRIF is a key: it never appears in the report, a reason file or a log. The
  command line shown has it replaced with `***` (`shown_command`), and so does any line of
  Vita3K's output that is quoted (`failure_line`). Reasons about the key file never quote a
  line of it. Vita3K takes the zRIF only on its command line (`--zrif`), so it is visible in
  the process list of the machine while Vita3K runs (under a second for a 200 MiB package).
- **Tool.** `[inbox] vita3k` is the app bundle (`Vita3K.app`) or a binary (a relative path is
  made absolute); unset, inbox uses `/Applications/Vita3K.app` on macOS when its
  `Contents/MacOS/Vita3K` is executable, and `vita3k` (or `Vita3K`) on PATH otherwise. Reported
  in the `tools:` line and by `doctor` as an optional inbox tool; `doctor` does not run it
  (that would write Vita3K's config in the user's data folder), and warns when inbox would
  refuse it (see below). Without it a package goes to review, in report mode too, unless the
  game is already in the library.
- **Isolation.** Vita3K has no option for where `--pkg` installs (`-c` is not read on that
  path, and on macOS it ignores `HOME`), and it deletes `ux0/app/<TITLEID>` before
  installing, so it must never run against its own data or a console's. Its portable mode is
  used instead, in a new folder of the local work folder (`work_dir`, which must have 1.1 times
  the package's size free): Vita3K takes a `portable/` folder, when there is one, for all its
  data (`fs`, config, log, cache); where it looks for that folder is read from `init_paths`
  (`app/src/app_init.cpp`). The configured path is first made absolute and resolved through
  every symbolic link (`resolve_tool`), so a link such as `/usr/local/bin/vita3k` to the binary
  inside the bundle counts as the bundle. Vita3K is then run only when its portable folder is
  known to be the work folder's (`vita3k_problem`, pure, on the resolved path, `sys.platform`
  and the first 16 bytes of the program); otherwise the package goes to review with the
  reason, in report mode too, and nothing is run:
  - *macOS:* Vita3K looks four levels above `Contents/Resources` of the bundle it runs from,
    and does not resolve a link, so the folder gets a symbolic link `Vita3K.app` to the
    resolved bundle and an empty `portable/`, and the program run is
    `<folder>/Vita3K.app/Contents/MacOS/Vita3K` (`launch_plan`, pure). A binary that is not
    inside an app bundle is refused: it would look for `portable/` next to itself, or use the
    user's `~/Library/Application Support/Vita3K`.
  - *Linux:* Vita3K reads `$APPIMAGE` whenever it is set and takes the folder next to it, and
    when that folder exists every path (fs, config, cache, log) goes there and none comes from
    XDG or `HOME`. So a plain ELF binary runs with `APPIMAGE` set to `<folder>/Vita3K`. An
    AppImage (an ELF file with `AI` and type 1 or 2 at offset 8) is refused, because its
    runtime overwrites `APPIMAGE` with its own path, and so is a file that is not ELF (a
    wrapper script could run Vita3K anywhere, a Flatpak for one). This has not been tried with
    a real Linux build.
  - *Anything else* (a bundle on Linux, another system) is refused.

  Then `Vita3K --version` runs first: in a new portable folder it writes `config.yml`,
  without which `--pkg` stops with exit code 134. If `portable/config.yml` is not there afterwards, Vita3K is not using
  the folder, and the package goes to review before `--pkg` ever runs. Then
  `Vita3K --pkg <file> --zrif <zRIF>` runs in that folder with a one-hour limit (past it, Vita3K is killed with every process it
  started, through `runners.run_tool`); it opens no
  window and needs no firmware. The work folder is deleted afterwards (the link is removed,
  never followed); if it cannot be deleted, a warning names it, since it holds the licence.
- **Result.** Vita3K exits 0 even when the install fails, so the result is judged on disk
  (`judge_installed`, pure): `portable/fs/ux0/app/<TITLEID>/` must hold `eboot.bin` and a
  `sce_sys/param.sfo` with the package's `TITLE_ID` and `CONTENT_ID` and a game `CATEGORY`, no
  `sce_pfs`, and `portable/fs/ux0/license/<TITLEID>/<CONTENT_ID>.rif` must be the 512 bytes
  the zRIF decodes to. A non-zero exit code or any of these missing goes to review with the
  reason and the line of Vita3K's output that says why (`header signature is invalid`,
  `Failed to decode zRIF string`, `Failed to load pkg file`), with the zRIF redacted. Both
  layers are removed (no `sce_pfs`, no `sce_sys/package`), but `eboot.bin` stays an encrypted
  SELF, so the licence is required to run the game.
- **Filing.** `inbox_vita.install` with that game folder and the licence passed explicitly
  (`license_sources` would not find it: it is in `ux0/license`, outside the game folder, and
  there is no `work.bin`). Everything there holds: the `.part` copy and rename of the folder,
  the licence check against the library before anything is placed, the launcher, and the
  three whitelist lines. A game already in `library/roms/psvita/app/<TITLEID>/` is not
  installed again: Vita3K is not run (nor needed), and the licence (from the zRIF) and the
  launcher (from the package's `param.sfo`) are added when they are missing.
- **Report mode.** Nothing runs. Everything the report needs is known from the header, the
  `param.sfo` and the zRIF, so it prints the three whitelist lines and the command it would
  run (`would install it with Vita3K: Vita3K --pkg <file> --zrif ***`).

### PS Vita packages in the container

Vita3K is not in the hub image, so in the container a PS Vita `.pkg` goes to review with the
reason "missing tool" and `doctor` lists Vita3K as optional and not found. This is a known gap,
and `inbox` for those packages runs on a computer with Vita3K. What was looked at, on
2026-11-13:

- **Vita3K itself.** GPL-2.0. It has one release, the rolling `continuous` one, whose files are
  named `*-latest` and replaced on every build, so no download can be pinned by hash for long, and
  there is no versioned tag to build from. Its `--pkg` mode opens no window, but the program is
  one executable whose source includes the Qt 6 main window and initialises SDL 3 (`vita3k/main.cpp`),
  so a binary needs those libraries in the image, and a build from a pinned commit needs them
  and about 28 submodules. Neither is a sound, pinned and small option, so neither is in the
  image.
- **`pkg2zip`** (mmozeiko/pkg2zip). Unlicense, a small C program with no dependencies, tagged
  (v1.8, commit 086284d), and it takes a zRIF on the command line (`pkg2zip -x <pkg> <zRIF>`). But
  the repository is archived (last change in 2024), and its output is made for a real Vita
  (VitaShell and NoNpDrm: a `work.bin` licence in the game folder), not the layout Vita3K's own
  installer writes and `judge_installed` checks (`ux0/license/<TITLEID>/<CONTENT_ID>.rif`). Using
  it needs a second install path in `inbox_vita_pkg` (what it writes, where the licence goes, how
  the result is judged) that can only be checked against real packages, which is work of its
  own and not part of the image. If it is done, the Dockerfile gains one small build stage like
  the `orbis-pkg-util` one.
- **Anything else.** No other command line tool that installs a package in Vita3K's layout was
  found.

## Arcade sets

Arcade zips are not unpacked or converted: a MAME set is a zip whose name and members
the emulator needs exactly as they are, so `inbox` only identifies, names and places it.
The code is in `warpzone/inbox_mame.py`, which plugs into `inbox.py` through a small
registry (`inbox.kind_handlers()`; a handler has `validate`, `claims` and `process`, and
`_identify_and_place` offers every item to the handlers before the usual path).

- **Which items.** A top-level `.zip`, or a folder with nothing but `.zip` files (and
  sidecars), is taken as arcade when at least one member (size and CRC32, read from the
  zip directory) is in a configured database. Anything else, and every item when no
  database is configured, takes the usual path.
- **Databases.** `[inbox] mame_dat` (MAME's `-listxml`, or the official `mameXXXXlx.zip`,
  whose first `.xml` or `.dat` member is read), `fbneo_dat` (the FinalBurn Neo
  "ClrMame Pro XML, Arcade only" DAT) and `mame2003plus_dat` (`mame2003-plus.xml`).
  All three are XML with `machine` or `game` elements and are parsed the same way with a
  streaming `iterparse` (a listxml is 300 MB): a set is its `name`, `cloneof`, `romof`,
  `isbios` / `isdevice` (a not-runnable set is a BIOS, which is how 2003-Plus marks
  them), its `rom` elements that have a CRC32 and are neither `nodump` nor `optional`
  (with `merge` and `bios`), its `device_ref`s and its `disk` names. Sets with nothing to
  check are dropped and each set's devices are flattened to the ones that have ROMs. The
  index is cached as JSON in the DAT cache folder (`sets-*.json`), keyed by the file's
  size and modification time. A configured path that is not a file is a configuration
  error (exit 2), checked before anything runs.
- **Fit.** A set fits a database when every required ROM is present, where a ROM is the
  pair (size, CRC32) and present means in the dropped zip or in a zip of
  `library/roms/mame/` named after a set the set depends on: its parent (`cloneof`), its
  BIOS (`romof`), its devices and theirs. The set's own name is never read from the
  library (the dropped zip stands in for it). A set that lists several alternative
  `bios` ROMs needs one complete alternative. Members may be anywhere in the zip: recent
  merged sets keep clones in sub-folders. The zip is read back member by member before it
  is trusted (`testzip`).
- **Which set.** For each database: the set named like the file when it shares at least
  one member with the zip (judged as that set, fit or not, so a file called `galpanic.zip`
  never silently becomes a different clone); otherwise the sets that fit, taking the top
  parent among them (a merged zip fits its parent and each of its clones), then the one
  that explains most members, games before BIOS and device sets; otherwise the set
  lacking the fewest files.
- **Decision.** The databases are tried in the order FinalBurn Neo, MAME, MAME
  2003-Plus (the ordered tuple `SOURCES`), only the configured ones. The first whose set
  fits decides: MAME means the ES-DE default (no `<altemulator>`; an override left in the
  gamelist is removed), the others their ES-DE name, `FinalBurn Neo` and `MAME 2003-Plus`.
  FinalBurn Neo comes before MAME because of RetroAchievements: EmuDeck turns them off
  for the "MAME - Current" core (`cheevos_enable = "false"` in its `mame.cfg`) and the RA
  arcade sets target FinalBurn Neo, so a CPS1 game such as dino or ddsom only earns
  achievements there. A set FinalBurn Neo does not have simply falls through to MAME. When none fits the zip goes to review, and the reason has one line per
  database with the missing files (name and CRC32) grouped by the zip they belong to:
  `needs neogeo.zip`, `needs qsound_hle.zip or qsound.zip`, `<zip> in the library lacks`,
  `<set>.zip lacks`. If only some databases are configured, the others are simply not
  considered. BIOS and device sets (`neogeo`, `qsound_hle`) are filed with no emulator
  entry. The notes of a placed set say why earlier emulators did not fit.
- **Atomiswave and Naomi (Flycast).** These Sega boards hang in MAME but run in Flycast
  (checked on hardware). A game is one of them when the `romof` chain in MAME's database
  ends in `awbios` (Atomiswave) or `naomi`, `naomi2`, `naomigd` (the Naomi family): a clone's
  `romof` is its parent, whose `romof` is the BIOS, so the chain is followed up to a
  small depth. If MAME's set is complete (same fit rule as above) the decision is
  `<altemulator>Flycast</altemulator>` (the ES-DE label of the RetroArch core) whatever the
  order above; an incomplete set is reviewed as usual, never sent to Flycast. Flycast
  reads its BIOS from `library/bios/dc/` (`awbios.zip`, `naomi.zip`, same names as
  MAME), not from `roms/mame/`. When the matching zip is not in `bios/dc/` the game is
  still placed and a note tells the user to copy it from `roms/mame/`; warpzone never
  copies BIOS files.
- **Name and placement.** The result is `library/roms/mame/<set>.zip`, the set name of the
  database that decided, whatever the file was called; the bytes are the original's. The
  set name comes from the database, so it gets the same check as a DAT's game name
  (`unsafe_entry`): one that is not a plain file name sends the zip to review and nothing
  is written. It is
  copied to `<name>.part`, read back and compared member by member, then renamed. If the
  name exists, equal members mean "already in the library" and different ones a review
  (conflict, nothing replaced). `roms/arcade/` is never written. The whitelist line is
  `!/roms/mame/<set>.zip`. Originals go to `_done` or `_revisar` as for other systems.
- **ES-DE gamelist.** `[inbox] gamelist_mame` (default
  `<root>/frontend/es-de/gamelists/mame/gamelist.xml`) holds per-game overrides:
  `<game><path>./dino.zip</path><altemulator>FinalBurn Neo</altemulator></game>`. For
  every game set or confirmed by a run, including one that was already in the library, the
  entry of `<set>.zip` is brought in line: added when missing, its `<altemulator>` changed
  when different, and removed when the default now fits (an entry that held nothing else
  is removed whole). The file is edited as text so nothing else changes: ES-DE writes
  several top-level elements, so it is not parsed and rewritten; entries are found by
  their `<path>`, XML comments are skipped, indentation and the newline style are copied,
  and the result is checked to be well-formed (when the original was) before it is
  written. It is written to `gamelist.xml.part` and renamed. Before the run's first
  change to a gamelist, the file is copied to `<root>/tools/logs/gamelist-backups/
  <system>-gamelist-<YYYYMMDD-HHMMSS>.xml` (`<system>` is `mame` or `ps3`). The backup is
  not written next to the gamelist because that folder is synced both ways and the copy
  would reach every console; `tools/logs` is excluded by the ignores of `wz-tools` (a
  fixed `/logs` line), which `provision hub` applies to an existing folder too. A missing file is created only when
  an override is needed. Report mode prints each change as "would ...". If the gamelist
  cannot be updated after the zip was placed, the item goes to review and a rerun
  ("already in the library") retries it.
- **ES-DE keeps its gamelist in memory.** On a console ES-DE reads the gamelist once and
  rewrites it when a game is launched, so an edit made on the hub while ES-DE is open on
  a console is overwritten when that console next saves. warpzone cannot see the
  consoles, but a console that just played writes the file back through Syncthing, so
  with `--apply` and before the first edit of a gamelist it compares the file's
  modification time with the clock (`recently_written`, window 10 minutes). Within the
  window it prints a warning, once per gamelist per run, saying to quit ES-DE on every
  console and run `inbox` again if the change does not stick. It never refuses: the edit
  is made either way. The same applies to the PS3 gamelist. A run's own write counts as a
  modification, so a second `--apply` within 10 minutes warns about it too.
- **Not covered.** CHD disks of a set are not in the zip: they are listed in a note, not
  checked. `.7z` and `.rar` are not opened for arcade. Samples are not checked. warpzone
  never downloads the databases.
