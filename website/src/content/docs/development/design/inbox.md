---
title: "Inbox"
---

`warpzone inbox [--root PATH] [--dats PATH] [--work-dir PATH] [--apply]` files new
games dropped in `inbox/` into `library/roms/<system>/`. It runs on a desktop that
mounts the hub's share, because conversions are too heavy for a NAS, and it reads no
Syncthing state. The contract:

- **Layout.** `--root` (or `[inbox] root`) is the hub root as mounted on this machine:
  it must hold `inbox/` and `library/roms/`. Items are the top-level files and folders
  of `inbox/`. Hidden files, `@eaDir`, `#recycle`, `Thumbs.db` and any `_*` folder
  (`_done`, `_revisar`) are never items. An item modified in the last 30 seconds is left
  alone, since it may still be copying.
- **Report only by default.** Without `--apply` it unpacks into a local work folder,
  identifies, plans and prints, and writes nothing to the share. With `--apply` it
  converts, places and files the originals.
- **Untrusted content.** Archives are unpacked only into the local work folder (`.zip`
  with `zipfile`, `.7z` and `.rar` with `bsdtar`; archives inside archives up to two
  levels). A member with an absolute path, a `..` component or a NUL byte, or a symbolic
  link, makes the whole item go to review, and nothing is extracted before every name is
  checked. A cue or gdi is followed only if every file it names is a file of the item.
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
  Boy Advance `gba`, Mega Drive / Genesis `genesis`, Master System `mastersystem`, Game
  Gear `gamegear`); a trailing No-Intro date stamp is ignored. DATs are parsed only when
  a lookup needs them (the system the file extensions and sizes point at first, and the
  search stops once every file is explained) and each parsed DAT is cached as JSON in
  `$XDG_CACHE_HOME/warpzone/dats`, keyed by the DAT's size and modification time.
- **Formats follow the library.** `psx`, `saturn`, `dreamcast`: `chdman createcd` from
  the cue or gdi, then `chdman verify`. `ps2`: `createdvd` for an `.iso`, `createcd`
  for cue/bin. A lone `.bin` without a cue gets a generated one (PlayStation only).
  `psp`: the plain `.iso`, copied and its CRC32 checked against the DAT. `gc`: an
  `.iso` stays an `.iso` (the report prints the `dolphin-tool convert -f rvz -c zstd
  -l 5 -b 131072` command; warpzone never converts it); an `.rvz`, `.gcz` or `.wia` is
  identified by expanding it with `dolphin-tool convert -f iso` into the work folder and
  the compressed file itself is what gets placed, as `<game>.rvz`. Cartridge systems: a
  `<game>.zip` with the rom under its DAT name, always the same bytes for the same game.
  Names are the DAT's game names. A multi-disc game (`(Disc N)` in the DAT name) of a
  CHD system goes in `<game without disc tag>.m3u/` with every disc and a
  `<game>.m3u` playlist; GameCube discs stay loose files, as in the library. A game is
  placed only when every disc the DAT lists is in the item (`incomplete multi-disc`
  otherwise); a disc that is the only one its DAT lists is a plain game.
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
  unsafe archive, not supported yet). Games of such an item that could be placed are
  placed; running the item again reports them as already in the library. Names that are
  taken get ` (2)`, ` (3)`, ... On every `--apply` run, folders of `_done` whose name is a
  date older than 7 days are deleted; nothing else in `_done` is touched, and a symbolic
  link is not followed. Without `--apply` the run only says what it would delete.
- **Whitelists are never edited.** The report prints, per placed game, the whitelist
  line to add (`!/roms/psx/Silent Hill (Europe) (En,Fr,De,Es,It).chd`; Syncthing
  wildcards in names are escaped) and a combined list at the end.
- **Missing tools.** `chdman`, `bsdtar`, `RAHasher` and `dolphin-tool` are located with
  `[inbox]` config keys or PATH (RAHasher only when configured). A tool that an item
  needs and is missing sends that item to review with a clear reason; other items
  continue. A configured path that does not exist is a configuration error.
- **Arcade (MAME) sets.** See [Arcade sets](#arcade-sets) below; they do not go through
  DAT matching or conversion.
- **Switch games.** See [Switch games](#switch-games) below; they do not go through DAT
  matching or conversion.
- **PS3, Xbox, Xbox 360 and Wii U images.** See [Console disc images](#console-disc-images)
  below; they are identified by signature, not by DAT.
- **3DS images.** See [3DS images](#3ds-images) below; `.3ds` and `.cci` files are
  identified by their NCSD and NCCH headers, not by DAT.
- **PS4 packages.** See [PS4 packages](#ps4-packages) below; with `orbis-pkg-util` they are
  extracted into a game folder with a shortcut.
- **Out of scope.** Vita `.vpk` packages, and PS3 or Xbox content that is not a disc image
  (`.pkg`, `.rap`, an extracted `PS3_GAME` or `default.xbe` folder). Their files are recognised by
  extension or folder and go to review with "not supported by inbox yet". A disc image kept as a
  single `.bin` for several tracks is not identified (the DATs list one file per track).
- **Exit code.** `0` when every item is placed or already in the library; `1` when any
  item went to review; `2` for a usage or configuration error.

The decisions (`skip_name`, `unsafe_member`, `select_purge`, `plan_units`,
`destination_verdict`, `whitelist_line`, `resolve_refs`, `unsupported_reason`,
`match_dat`, `group_discs`, `parse_dat`) are pure and unit-tested; conversions are
tested with stand-in `chdman` and `RAHasher` scripts, and one test runs the cartridge
flow end to end with no external tool.

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
  with `--apply`; the report only reads it with `-i`. `nsz` exits 0 on some damaged
  inputs, so the output is accepted only if `nsz` exited 0 and printed `[DONE]`, its PFS0
  header parses (magic, entry table, member bounds), and `nsz -V` exits 0 and prints
  `[VERIFIED]`. Anything else is a review and the partial output is deleted with the work
  folder. Only the `.nsp` reaches the library. `.xci` and `.xcz` are reviewed with the
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
  big-endian content type at 0x74 (`0x1A` game data, `0x1B` additional content) and the
  content flags at 0x78 (`0x100000` or `0x40000000` mark a patch). An update or DLC goes to
  review with a message saying that updates are not merged (nothing is extracted, even when
  the base game is in the library). After extraction the `CATEGORY` of `sce_sys/param.sfo`
  decides again (`gd*` game, `gp` update, `ac` DLC) and its `TITLE_ID` must equal the header's.
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
- **Whitelists.** Two lines per game: `!/roms/ps4/games/<CUSAxxxxx>` and
  `!/roms/ps4/shortcuts/<Title>.desktop`. A report without `--apply` does not unpack
  anything, so it cannot name the shortcut and prints only the first.
- **Install directory.** The consoles must list `~/Emulation/roms/ps4/games` as an install
  directory of shadPS4. See [shadPS4 install directories](/warpzone/development/design/profiles/#shadps4-install-directories).

## PS Vita games

A decrypted Vita dump is recognised and filed by `warpzone/inbox_vita.py`, a kind handler
registered after the console image handler (so a `.pkg` or anything that handler claims keeps
its outcome). Vita3K runs games from `ux0/app/<TITLEID>`; ES-DE lists only `.psvita` files,
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
  `sce_pfs` (Vita3K has to install it) goes to review with the reason. `.vpk` files and `.pkg` files keep their earlier outcome.
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
  database that decided, whatever the file was called; the bytes are the original's. It is
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
