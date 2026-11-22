---
title: "Scraping"
---

**Status: implemented** (`warpzone scrape`, the doctor checks, and the scrape inside
`inbox --apply` with `--no-scrape`). This page is the contract for scraping game
metadata and media into the hub: `inbox --apply` scrapes the games it just placed, and
`warpzone scrape` fills in the games already in the library. The pages it touches
([Inbox](/warpzone/development/design/inbox/), [Configuration and command line](/warpzone/development/design/configuration/),
[Doctor](/warpzone/development/design/doctor/), [Hub, folders and ignores](/warpzone/development/design/hub-and-folders/),
[Code, testing and CI](/warpzone/development/design/code-and-testing/)) were updated in the same
pull requests as the code. The setup guide is [Scraping](/warpzone/setup/scrape/).

Until now the hub's gamelists and media came from ES-DE's own scraper, run by hand on a console.
A game `inbox` files arrives on every console without a description, a cover or a video, and the
[library UI](/warpzone/development/design/library-ui/#metadata-and-media) shows it bare. Scraping
closes that gap on the machine that files the game, in the layout ES-DE and the UI already read:

```
<hub root>/frontend/es-de/gamelists/<system>/gamelist.xml
<hub root>/frontend/media/<system>/<kind>/<stem>.<ext>
```

## Decisions

- **Source: ScreenScraper, through Skyscraper.** warpzone runs
  [Skyscraper](https://github.com/Gemba/skyscraper) (the Gemba fork) as an external tool, like
  `chdman`. ScreenScraper refused a developer id for warpzone, so there is no native client:
  Skyscraper talks to ScreenScraper with its own registered id. IGDB and libretro-thumbnails are
  not used. ScreenScraper identifies cartridge dumps by checksum and the rest by name, and the
  names `inbox` gives are the DATs' names, which it matches well.
- **The tool boundary.** Skyscraper reads the games from a staged folder outside the share and
  writes its gamelist and media into a temporary folder outside the share. It never writes to the
  hub. warpzone alone decides what reaches the hub and writes it, so the rules below (never
  change a field an entry has, never overwrite a file) do not depend on how Skyscraper merges
  gamelists.
- **Credentials stay in Skyscraper's config.** They are optional and live only in
  `~/.skyscraper/config.ini`. warpzone never reads their value, never prints them and never puts
  them on a command line (see [Credentials](#credentials)).
- **Correctness on the command line, taste in the config.** warpzone passes every option that the
  hub layout depends on (frontend, folders, artwork file, the shape of the title, which media are
  fetched), so a user's `config.ini` cannot break it: Skyscraper gives the command line precedence
  over every section of the config. Language and region, which are a matter of taste, are the
  user's: they stay in the user's config, and warpzone passes one region and one language only when
  `[inbox]` names them (see [Language and region](#language-and-region)).
- **When it runs.** Automatically in `inbox --apply` when Skyscraper is configured or found,
  only for what that run placed; a scrape failure is a warning and never blocks or undoes a
  placement. And on demand with `warpzone scrape` for games already in the library. A "Scrape"
  button in the library UI can come later on top of the same code.
  A nightly run from a scheduled task, `warpzone scrape --unattended`, is planned: see
  [Unattended scrape](/warpzone/development/design/scrape-unattended/).
- **Only adds.** A field an existing `<game>` entry has is never changed: consoles write play data
  into it and the user may have edited it. A game without an entry gets one; a game with an
  entry only gets what it lacks: the media files, and the text fields (`desc`, `genre` and the
  others of [Merge](#merge)) that it has no value for, which is what completes an entry ES-DE or
  the play state created bare. ES-DE's `<nomultiscrape>` is respected. No media file is ever
  overwritten. The one exception is explicit: `warpzone scrape --refresh-text` replaces the name,
  description and genre of existing entries with what Skyscraper's cache holds, and nothing else
  (see [Refreshing the text](#refreshing-the-text)).

## Scope and non-goals

In scope: the text fields and the media kinds listed in [Media kinds](#media-kinds), for the
systems in [Systems and platforms](#systems-and-platforms), into the hub's ES-DE gamelists and
media folders.

Not in scope:

- Changing or "improving" an existing scrape. A wrong match is fixed by the user (by editing the
  entry on a console, or by deleting the entry and its media and running `warpzone scrape`). The
  only change warpzone makes to the text of an existing entry, and only when asked, is
  [`--refresh-text`](#refreshing-the-text).
- Manuals and fanart. ScreenScraper has them and Skyscraper can fetch them, but the hub has no
  kind for them and neither the UI nor the consoles' frontends show them; they are not fetched.
- Title screens and physical media scans (`titlescreens`, `physicalmedia`). Skyscraper's
  ScreenScraper module does not fetch them, so scraped games go without; the library UI and ES-DE
  fall back to the other kinds.
- Running Skyscraper on a console. It runs where `inbox` runs: on a computer that mounts the share or in the
  hub container (see [In the hub image](#in-the-hub-image)).
- Downloading anything but metadata and media. warpzone never downloads games or BIOS files.

## Configuration

```toml
[inbox]
skyscraper = "/usr/local/bin/Skyscraper"   # optional; PATH is searched for Skyscraper when unset
```

Skyscraper is located like the other inbox tools: the `[inbox] skyscraper` path, else
`Skyscraper` on `PATH` (in the hub image it is on `PATH`, so the container needs no setting). A configured path that does not exist is a configuration error (exit 2).
When neither finds it, `inbox` does not scrape and says so once in its report; `warpzone scrape`
exits 2. The other `[inbox]` keys it uses are `root`, `work_dir` (the staged and temporary
folders and the logs live in `<work_dir>/scrape/`; without it, in `$XDG_CACHE_HOME/warpzone/scrape/`,
since a system temp folder may be small and a log should stay for the user to read) and
`tool_timeout_minutes`, which applies to every Skyscraper call. The
version tested is Skyscraper 3.21.1.

### Credentials

ScreenScraper works without an account. Its limits:

| Account | Threads | Requests per day |
|---|---|---|
| none (anonymous) | 1 | 10,000 |
| free account | 1 | 20,000 |
| contributor | more, by contribution level | more |

A user with an account puts it in Skyscraper's own config:

```ini
[screenscraper]
userCreds="user:pass"
```

warpzone never passes `-c` (so Skyscraper reads its default `~/.skyscraper/config.ini`, or
`$XDG_CONFIG_HOME/skyscraper/config.ini` in a build with XDG paths; in the hub image it is the
volume at `/skyscraper`, see [In the hub image](#in-the-hub-image)) and never `-u`, which would
show the credentials in the process list. It does not read their value: the only code that opens
`config.ini` is the presence check of `doctor` and of the scrape report, which goes through the
file line by line, tracks the `[section]` it is in, splits each line at the first `=` and keeps
only the key; the rest of the line is dropped without being stored, compared or printed. It
answers one question: is there a `userCreds` key in `[screenscraper]`. Skyscraper's own output
can name the account (a release build does not print the password, but warpzone does not rely on
that), so warpzone never prints it: see [Report](#report).

Threads are left to Skyscraper, which takes the number the account allows; warpzone never passes
`-t`.

### Language and region

Skyscraper 3.21.1 (`docs/CLIHELP.md`, `docs/CONFIGINI.md`, `docs/REGIONS.md`,
`docs/LANGUAGES.md`, `src/screenscraper.cpp`) works like this:

- ScreenScraper answers with every region's media, title and date and every language's
  description, and Skyscraper picks one of each **while it gathers**: `regionPrios` chooses the
  title, the date and each medium, `langPrios` the description and the genre. The cache keeps the
  one value picked, so the generate passes have nothing to choose. A game already in the cache
  keeps what it has until it is purged from it (`onlymissing` skips it), and warpzone itself never
  overwrites a medium or an entry's text on the hub, so a change of region or language reaches the
  games scraped from then on (see "Only adds" above and the setup guide). The text of the entries
  already on the hub is moved to what the cache holds by `--refresh-text`, which asks ScreenScraper
  for nothing (see [Refreshing the text](#refreshing-the-text)).
- The defaults are `eu us ss uk wor jp au ame de cus cn kr asi br sp fr gr it no dk nz nl pl ru se
  tw ca` and `en de fr es`. `regionFromFilename` (default `inline`) adds the region named in the
  file name, such as `(Europe)`, at its place in that list.
- `config.ini` has `region`, `regionPrios`, `lang` and `langPrios`. `regionPrios` and `langPrios`
  replace the defaults and have **no command-line option**. `--region CODE` and `--lang CODE` take
  one code each, put it first in the list (the defaults or the `config.ini` list) and, for the
  region, replace any region read from the file name. Skyscraper checks no code.

So warpzone cannot pass a priority list without writing `config.ini`, which it never does (it
would also have to merge with the `userCreds` the user keeps there). It passes the two single
options instead, on the **gather only**, and only when `[inbox]` sets them:

| `[inbox]` key | Skyscraper | Example | With the defaults after it |
|---|---|---|---|
| `scrape_region` | `--region` | `"sp"` | `sp eu us ss uk wor jp ...` |
| `scrape_language` | `--lang` | `"es"` | `es en de fr` |

The pair `scrape_region = "sp"` and `scrape_language = "es"` is the example of the docs: Spanish
first, then Europe and English, with the default order after that. A user who wants another
order (a different list, or `regionFromFilename`) sets it in `config.ini` as before, and the two
compose: the command line puts its one code first in whatever list `config.ini` holds. Each key
takes one code of the tables Skyscraper documents and anything else is a configuration error
(`config.SCRAPE_REGIONS`, `config.SCRAPE_LANGUAGES`); unset passes nothing and Skyscraper decides
as before. A language code is not a region code (`es` is Spanish, `sp` is Spain). `--region` also turns off the
region read from the file name, which `regionFromFilename` would otherwise place; a user who needs a game's own
region to win leaves `scrape_region` unset and uses `config.ini`. The guide also says not to set
`nameTemplate`, which makes Skyscraper ignore the title options warpzone passes.

### What warpzone passes

| On the command line | In `config.ini` terms | Why warpzone sets it |
|---|---|---|
| `-p <platform>` | | from the [platform table](#systems-and-platforms) |
| `-s screenscraper` (gather only) | | the one source |
| `--region <code>`, `--lang <code>` (gather only, when `[inbox]` sets them) | `region`, `lang` | the user's first choice of region and language (see [Language and region](#language-and-region)) |
| `-f esde` | `frontend="esde"` | ES-DE's media folder names and gamelist shape |
| `-i`, `-g`, `-o` | `inputFolder`, `gameListFolder`, `mediaFolder` | the staged and temporary folders, never the hub |
| `-a <file>` | `artworkXml` | warpzone's artwork files (see [Artwork files](#artwork-files)) |
| `--includefrom <file>` | | one batch of games (see [Flow](#flow)) |
| `--flags theinfront` | `theInFront="true"` | titles like the existing ES-DE scrape ("The Legend of Zelda", not "Legend of Zelda, The") |
| `--flags nobrackets` | `brackets="false"` | no "(USA)" or "[!]" in the title, like ES-DE |
| `--flags videos` | `videos="true"` | the hub keeps videos for the library UI |
| `--flags backcovers` | `backcovers="true"` | the hub has a `backcovers` kind |
| `--flags miximages` | | the composite goes to `miximages/`, the plain screenshot to `screenshots/` |
| `--flags relative` | `relativePaths="true"` | `./<file>` paths (warpzone rewrites them anyway, see [Merge](#merge)) |
| `--flags onlymissing` (gather only) | `onlyMissing="true"` | a game already in the cache costs no request, which makes runs resumable |
| `--flags unattend,nohints` | `unattend="true"`, `hints="false"` | no question and no tip on a run nobody answers |

The command line is built by a pure function (`scrape.skyscraper_args`), so a test fixes it
exactly and checks that no credential option can appear in it.

## Systems and platforms

`scrape.PLATFORMS` maps a hub system folder (ES-DE's system names, which is what `inbox` and the
frontend use) to Skyscraper's platform name. It is data, checked against Skyscraper's `peas.json`
and `platforms_idmap.csv` (3.21.1, the pinned `scrape.SKYSCRAPER_VERSION`; a test holds the 126
platform names and the ones with no ScreenScraper id). Every ES-DE system that has a usable
platform is in it; the setup guide lists them by maker in [Scraped systems](/warpzone/setup/scrape/#scraped-systems).
The rules that decide a system:

| Case | Platform | Note |
|---|---|---|
| ES-DE's name is a platform | the same name | most of the table: `nes`, `snes`, `psx`, `c64`, `pokemini`, `3do`, `msx`, `atarist`, ... |
| A regional or variant folder of one platform | that platform | `genesis`, `megadrivejp` -> `megadrive`; `famicom` -> `nes`; `sfc`, `snesna` -> `snes`; `sega32xjp`, `sega32xna` -> `sega32x`; `megacd`, `megacdjp` -> `segacd`; `saturnjp` -> `saturn`; `tg16`, `supergrafx` -> `pcengine`; `tg-cd` -> `pcenginecd`; `neogeocdjp` -> `neogeocd`; `mark3` -> `mastersystem` (the DAT `Sega - Master System - Mark III` is the same folder); `amiga600`, `amiga1200` -> `amiga`; `msx1` -> `msx`; `msxturbor` -> `msx2`; `tanodragon` -> `dragon32`; `to8` -> `moto` |
| ES-DE spells it differently | the platform's name | `n3ds` -> `3ds`, `cdimono1` -> `cdi`, `colecovision` -> `coleco`, `astrocde` -> `astrocade`, `amigacd32` -> `cd32`, `odyssey2` -> `videopac` (its alias `odyssey 2`) |
| A set of the MAME family | `mame` | `arcade`, `mame`, `mame-advmame`, `fba`, `fbneo`, `cps`, `cps1`, `cps2`, `cps3`, `model2`, `model3`, `stv`: ScreenScraper matches arcade sets by set name, `mame` lists the Capcom boards, Model 2, Model 3 and ST-V as aliases, and `arcade` has the same ScreenScraper system id (75) |
| An arcade board with a platform of its own | that platform | `neogeo`, `naomi`, `atomiswave`; `naomigd` -> `naomi` (the same ScreenScraper system) |

`mame` and not Skyscraper's `arcade` or `fba` platform for the family, so the library's arcade
folders get the same kind of cover (`mame` asks for the box art, `arcade` and `fba` for the
flyer). The family also shares one Skyscraper cache, the `mame` one, so a set already scraped
under one of these folders costs no request under another.

**Left out** (57 of the 195 systems of ES-DE 3.5; a system is added only when its games are one
file or a playlist the catalog already reads, and Skyscraper 3.21.1 has a platform for it):

- *No platform of its own*: `adam`, `android`, `androidapps`, `androidgames`, `archimedes`,
  `atarixe`, `electron`, `flash`, `fpinball`, `gamate`, `gx4000`, `lcdgames`, `lowresnx`, `lutro`,
  `mess`, `mugen`, `multivision`, `satellaview`, `sgb`, `spectravideo`, `sufami`, `supracan`,
  `uzebox`, `vpinball`, `wasm4`, `xboxone`, `zxnext`. Where `peas.json` names one as an alias of
  another platform (`satellaview`, `sufami` of `snes`) the alias only widens a name search; the
  request still goes to that platform's ScreenScraper system, so the games are not mapped.
- *A platform but no ScreenScraper id* (`platforms_idmap.csv` has none or `-1`): `chailove`
  (`love`), `j2me`, `ngage` and `symbian` (`symbian`), `naomi2`.
- *Games that are folders, launchers or scripts*, which the catalog would list as many files or as
  a launcher Skyscraper does not read: `ags`, `consolearcade`, `daphne`, `desktop`, `doom`, `dos`,
  `easyrpg`, `emulators`, `epic`, `kodi`, `laserdisc`, `lutris`, `openbor`, `pc`, `pcarcade`,
  `ports`, `quake`, `scummvm`, `solarus`, `steam`, `triforce`, `type-x`, `windows`, `windows3x`,
  `windows9x`. Each needs its own handling (like `ps4` below) before it can be added.

A file named in `--includefrom` is scraped whatever its extension (the named files replace the
scan of the folder, which is what Skyscraper's `formats` filter), so an ES-DE extension a platform
does not list, such as a disk image format of `c64`, needs nothing extra. The `formats` do matter
once more, when the gamelist is written: see the folder rule under [Flow](#flow), step "Stage".

A test checks that every system `inbox` can place (the values of `dat.SYSTEMS` and the systems of
the kind handlers) is in `PLATFORMS`, so a new system in `inbox` fails it until it is given a
platform, and that every platform in the table is one Skyscraper 3.21.1 defines and can ask
ScreenScraper about.

**Launchers and folders the frontend reads.** Five systems are scraped by what the frontend lists
and not by what the catalog holds as the game:

- *A PS4 or Vita game is a folder* (`roms/ps4/games/<CUSA>`, `roms/psvita/app/<TITLEID>`): Skyscraper
  cannot compute a cache id for a folder, and the folder's name says nothing ScreenScraper knows.
  The game's entry is its launcher, a part of the game: `roms/ps4/shortcuts/<Title>.desktop` or
  `roms/psvita/<Title>.psvita`, the file the catalog's `gamelist` path names
  (`scrape.entry_part`: the part the path ends with, the primary first). Staging links that file
  and not the primary, and ScreenScraper is asked by its name, which `inbox` writes from the
  game's title. A game with no launcher is not in the frontend, so it is not selected
  (`scrape.LAUNCHER_SYSTEMS`).
- *The frontend reads a folder below the system's one*: ES-DE lists PS4 from `roms/ps4/shortcuts/`
  and Wii U, Xbox 360 and Model 2 from `roms/<system>/roms/`, so its gamelist says `./Bloodborne.desktop` and
  `./Halo 3 (World).iso` where the catalog says `./shortcuts/Bloodborne.desktop` and
  `./roms/Halo 3 (World).iso` (the same shift as in
  [Frontend artwork](/warpzone/development/design/frontend-artwork/) and
  [Metadata and media](/warpzone/development/design/library-ui/#metadata-and-media)). The hub has
  no system list, so the library UI tries the shorter paths; a scrape cannot guess, because it
  writes. It uses `scrape.FRONTEND_BASES`: `roms/` for the systems of `whitelist.SUBROOT_SYSTEMS`
  (the ones whose games are kept below `roms/<system>/roms/`: Wii U, Xbox 360 and Model 2) and
  `shortcuts/` for PS4, the one fact
  that has no other source on the hub. `scrape.frontend_games` applies it to a system's games
  before anything else: the `gamelist` of each is cut to the frontend's path, and a game outside
  the folder (a PS4 package filed at the top, a PS4 game without a launcher) is dropped. From
  there the staged name, the entry's `<path>`, the media stem and the memory keys are all that
  path, and the catalog file is untouched. The Vita folder has no base.
- *Extensions.* Skyscraper's `ps4` platform lists no `.desktop`, so its calls also get
  `--addext '*.desktop'` (`scrape.EXTRA_EXTENSIONS`, like `.zcci`); its `psvita` platform lists
  `*.psvita`, `wiiu` lists `*.wua` among its formats and `xbox360` `*.iso`.

Skyscraper adds `.zip`, `.7z` and `.m3u` to every platform's formats, which covers the zips
`inbox` writes and the `.m3u` folders below.

## Flow

Both entry points run the same function on a list of games, one system at a time. A game is a
catalog entry (see [The catalog index](/warpzone/development/design/library-ui/#the-catalog-index)):
its `primary` part and its `gamelist` path. `inbox --apply` updates the catalog before it
scrapes, so the games it placed are there. For a system the frontend reads from a folder below its
own, or whose games are launchers (`ps4`, `psvita`, `wiiu`, `xbox360`, `model2`), the `gamelist` of each game
is first replaced by the frontend's path (`scrape.frontend_games`, see
[Systems and platforms](#systems-and-platforms)); everything below goes by that path.

1. **Select** (pure, `scrape.select`). From the catalog's games of the system, the hub gamelist
   and the media files present, it returns the games to scrape: those without an entry, and those
   with an entry (not marked `<nomultiscrape>`) that lack a file of a kind in
   [Media kinds](#media-kinds) or are *bare* (see [Merge](#merge)). `inbox` passes only the games it placed in this run; `scrape`
   passes all of the system's.
2. **Stage.** In `<work_dir>/scrape/<system>/input/`, each game gets a symbolic link at its
   `gamelist` path (subfolders kept; the folder rule below changes the name of a game one folder
   down) to the file on the share (for a PS4 or Vita game, its
   launcher and not its folder). Nothing is copied: a CHD is
   hundreds of MB and Skyscraper only reads it. An `.m3u` folder is staged as a file, a link named
   after the folder that points at the playlist inside it (`Game.m3u` -> `.../Game.m3u/Game.m3u`),
   so Skyscraper sees one game, searches by the name without disc tags and writes one entry. The
   map from staged name to game is kept for the merge; Skyscraper's paths are never trusted.
   A `.zcci` (Azahar's compressed 3DS image) is linked under its own name: Skyscraper resolves a
   link to the real file and would refuse a batch that names a `.cci` link to it. Its `3ds`
   platform lists only `*.3ds` and `*.cci`, so every Skyscraper call for that platform (gather
   and both generate passes) also gets `--addext '*.zcci'` (`scrape.EXTRA_EXTENSIONS`, one
   argument, extensions separated by a space). The media is `<name>.png`, as for a `.cci`.

   **The folder rule.** When Skyscraper writes the gamelist for a platform whose `formats`
   include `*.cue` (`psx`, `ps2`, `saturn`, `dreamcast`, `3do`, `segacd`, `pcenginecd`, `amiga`
   and others), a game exactly one folder below the input folder, alone in that folder among the
   files of the platform's formats, becomes a `<folder>` entry, and Skyscraper drops it unless the
   user's `config.ini` sets `addFolders` (`EmulationStation::assembleList`, 3.21.1). A disc game
   kept in its own folder (`roms/psx/Game/Game.cue`) would get no entry, and would be recorded as
   not found or only get its media. warpzone does not own `config.ini` and has no command-line
   option for it, so such a game is staged one folder deeper, under the folder of the real path
   above its own (`psx/Game/Game.cue`, `scrape.staged_name`), on every platform so that no list of
   platforms has to be kept. It is the real folder and not a made-up name because Skyscraper
   finds a staged file again by the end of its real path. The staged name is what the batch
   list, the media Skyscraper writes and the entry's `<path>` use; the merge keys the batch by it
   and writes the hub's entry and media under the game's `gamelist` path, as before. A game in
   the folder itself, or two or more folders deep, keeps its `gamelist` path as its staged name.
3. **Gather**, in batches of 100 games (an `--includefrom` list of staged paths): `Skyscraper -p
   <platform> -s screenscraper -i <input> --includefrom <batch> --flags
   onlymissing,videos,backcovers,unattend,nohints`. This is the only step that makes requests;
   it fills Skyscraper's cache and writes nothing else. A batch of 100 at about 10 seconds a game
   stays well inside `tool_timeout_minutes`, and an interrupted run loses at most the batch in
   progress, whose finished games are in the cache anyway.
4. **Generate**, from the cache, with no request (no `-s`): two passes over the same batch into
   `<work_dir>/scrape/<system>/out/`, each with its own `-g` and `-o`:
   - *main*: `-f esde -a artwork-esde.xml --flags miximages,videos,backcovers,theinfront,nobrackets,relative,unattend,nohints`.
     Its gamelist gives the text; its media folder gives `covers`, `screenshots`, `miximages`,
     `marquees`, `backcovers` and `videos`.
   - *3D box*: `-f esde -a artwork-3dbox.xml --flags theinfront,nobrackets,relative,unattend,nohints`
     (no videos, back covers or miximages to copy again); only its `covers/` is used, as `3dboxes`.
5. **Merge** the batch into the hub (see [Merge](#merge)), then delete the batch's output. The
   staged folder and the output are removed at the end of the system, and on an interruption.

A batch whose gather fails is not generated; the next batch is still tried unless the failure is
one that stops the run (see [Failures](#failures)).

## Merge

`scrape.plan_merge` is pure. It takes the batch's staged map (staged name to game) and the staged
folder, the hub gamelist's entries (parsed with `artwork.parse_gamelist`, so the size limit and
the encoding rules of [Frontend artwork](/warpzone/development/design/frontend-artwork/) apply),
Skyscraper's gamelist entries, the paths of the hub's media files (as `select` takes them), and
the files of the two passes' media folders, each with its type (regular file, symbolic link or
other). It returns a `MergePlan`: for each game a verdict (`added`, `filled`, `not found` or
`unchanged`), the entry to add or the fields to add to the entry the hub has (`fill`) and the
files to copy (pass, source, destination), plus what it dropped and why. The writer only
executes it; `scrape.render_entries` gives the text of the new entries, escaped, for the
indentation and newline style the writer copies from the file. Its rules:

- **Matching Skyscraper's entries.** An entry's `<path>` is taken as relative to the staged
  folder: `./Game.zip`, `Game.zip` and an absolute path inside the staged folder all mean
  `Game.zip`. A path outside it, one with `..`, or one not in the staged map is dropped with a
  warning. The entry written to the hub has the game's `gamelist` path (the frontend's, for the systems of
  [Systems and platforms](#systems-and-platforms)), never Skyscraper's.
- **Existing entries.** A `<game>` or `<folder>` of the hub gamelist whose `<path>` is the game's
  (`./` or not) is existing, and a field it has a value for is never changed (the explicit
  [`--refresh-text`](#refreshing-the-text) is a separate run, not part of this merge). It gets the media
  it lacks and, from Skyscraper's entry for the game, the fields of the "New entries" rule that
  it lacks or holds empty (`scrape.fill_fields`). It never gets a play statistic (`playcount`,
  `playtime`, `lastplayed`), `altemulator`, `hidden`, `favorite` or any other tag outside that
  list. One marked `<nomultiscrape>true</nomultiscrape>` gets nothing at all.
- **The name of an existing entry** is replaced only when it is the game's file name or the stem
  of it (what ES-DE shows, and the play state leaves out entirely, until someone gives the game a
  title): `Game.zip` or `Game`, compared with the base name of the entry's `<path>`, case
  sensitive. Any other name is a title and is kept. An entry with no `<name>` gets one. The one
  way a title is replaced is `--refresh-text`, which does it for every entry whose name differs
  from the cache's (see [Refreshing the text](#refreshing-the-text)).
- **Bare entries.** An entry with none of `desc`, `rating`, `releasedate`, `developer`,
  `publisher`, `genre` and `players` (whatever else it holds: play statistics,
  `altemulator`, a name) is bare (`scrape.is_bare`). `select` picks a bare entry even when it has
  every medium, so that its text is filled in. An entry that has some text and lacks other
  fields is filled whenever the game is scraped for another reason, but is not picked for that
  alone, since ScreenScraper has no rating or player count for many games.
- **New entries.** For a game without one: a `<game>` with the game's path and, from
  Skyscraper's entry, `name`, `desc`, `rating`, `releasedate`, `developer`, `publisher`, `genre`
  and `players` (the fields the library UI reads, `scrape.ENTRY_FIELDS`), when present and not
  empty. Nothing else is
  copied: no user field, no media path (ES-DE does not keep them in the gamelist). Text comes from
  the web, so it is XML-escaped and characters XML 1.0 cannot hold are dropped. An `.m3u` folder
  gets a `<game>` too, as ES-DE writes for a folder it reads as a file.
- **Media names.** Skyscraper names a file after the staged name without its last extension, in
  the same subfolder (`covers/Hacks/Game.png`). The hub's stem is the `gamelist` path without
  `./` and without its extension, except that a folder keeps its whole name, as the library UI
  and `artwork.build` read it: `covers/Game.m3u.png` for an `.m3u` folder staged as `Game.m3u`.
- **Which media.** A kind is copied only when the hub has no file of that kind for the stem in
  any of the extensions the UI reads (`png`, `jpg`, `jpeg`; `mp4`, `webm` for videos), and when
  the output has a regular file with one of those extensions. The extension is kept. A symbolic
  link in the output (Skyscraper's `symlink` option links videos into its cache) is skipped with
  a warning that names the option.

The I/O layer (`scrape_io.py`) applies the plan:

- **Media first, then the gamelist,** so an entry never names a game whose cover is still being
  written. Each file is written to `.<name>.wz-tmp` in the target folder, compared with the source
  by size, and moved into place; right before that the target folder is checked again and, if a
  file of that kind and stem has appeared, the copy is dropped and nothing is replaced. The name
  is taken with a hard link where the file system has them (which cannot replace a file), else with
  a rename right after the check. `wz-media` and `wz-esde` carry the fixed `*.wz-tmp` ignore line
  on both sides, as `wz-settings` has, so a scan never sends a half-written file.
- **The gamelist is edited as text** with the helpers the arcade and PS3 edits and the play state
  use (`gamelist`): the new entries go before `</gameList>`, the fields of a fill are set in the
  entry in place (an empty `<desc/>`, or the name that was the file name, is replaced; a missing
  tag is added as the entry's last child in the indentation of its `<path>`; `fill_entries`),
  indentation and newline style are copied, comments, every other tag of the entry and ES-DE's
  `<alternativeEmulator>` block are left as they are, and the result
  is checked to be well-formed before it is written to `.gamelist.xml.wz-tmp` beside the gamelist
  (`safefile.atomic_write`) and renamed. A missing gamelist is created, with the system folder,
  when there is an entry to add.
- **Re-check before replacing.** Consoles write the hub gamelist through Syncthing at any time.
  The file is read with its stamp (size, modification time, and its bytes); right before the
  rename it is read again, and if it changed the plan is made again from the new content and the
  write retried, three times, then the system's entries are left for the next run with a warning.
  The plan only adds, so making it again is always safe: an entry a console wrote for the same
  game in the meantime is kept as it is and the new one is not added, and a field it gained
  meanwhile is not replaced (`fill_entries` checks each field against the entry just read).
- **Backup.** Before the run's first change to a gamelist, a copy goes to
  `<root>/tools/logs/gamelist-backups/<system>-gamelist-<YYYYMMDD-HHMMSS>.xml`, as for the inbox's
  gamelist edits (outside every synced folder, so it reaches no console). `scrape` is a
  convergent command (see [Writing commands](/warpzone/development/design/configuration/#writing-commands)):
  it writes by default, takes `--dry-run`, only adds (a field with a value is never changed, except
  by the explicit [`--refresh-text`](#refreshing-the-text)), and
  a second run with nothing new changes nothing.
- **ES-DE keeps its gamelist in memory.** A console with ES-DE open writes back the gamelist it
  read at start, without the new entries. As in `inbox`, a gamelist written in the last 10 minutes
  (`recently_written`) gets one warning per run saying to quit ES-DE on every console and run
  `warpzone scrape` again if the entries do not stick. A later run adds them again from the cache,
  with no request; the media are already there (and a filled field a console dropped is filled
  again, since the entry lacks it again).

## Refreshing the text

`warpzone scrape --refresh-text` is the explicit exception to the merge rules above. The games
scraped before `[inbox] scrape_region` and `scrape_language` were set have their name, description
and genre in the default language, and the merge never replaces a field an entry has, so they
never get the text of the configured language even when Skyscraper's cache holds it. The option
replaces those three fields, and only those, from the cache.

- **No request.** It runs no gather, so ScreenScraper is never asked for anything and the cache is
  not changed. Per system it stages the games the way [Flow](#flow) does and runs the main
  generate pass of step 4 (`-f esde -a artwork-esde.xml` with the same flags, no `-s`, and no
  `--region` or `--lang`, which are gather options) in batches of 100, and reads that pass's
  gamelist only. The media that pass writes goes to the temporary folder and is deleted with the
  batch; nothing is copied to the hub. The 3D box pass does not run.
- **Which entries.** The games of the catalog that the hub gamelist has a `<game>` entry for, an
  entry not marked `<nomultiscrape>` (the user's opt-out holds here too). A `<folder>` entry is
  never changed, even when its path is a catalog game's. A game with no entry is never added by
  this option, and an entry no catalog game matches is not looked at (`scrape.refresh_candidates`).
- **What is replaced.** `scrape.REFRESH_FIELDS`: `name`, `desc` and `genre`. For each one that
  Skyscraper returns with a value that differs from the entry's, the entry's value is replaced
  (or the tag is added when the entry had none). A game the cache does not have, and a field
  Skyscraper returns empty, are left exactly as they are, so a field never becomes empty or goes
  away. No other field is read or written: play data, `altemulator`, `releasedate`, `developer`,
  `publisher`, `players`, `rating`, `hidden`, `favorite` and the image paths stay byte for byte
  (`scrape.refresh_fields`, `scrape.plan_refresh`). A name the user wrote is replaced like any
  other, which is why the dry run lists every name change. Two names never replace another, and
  the entry keeps its current name: the game's file name or its stem, which Skyscraper falls back
  to when it has no title (the test of "The name of an existing entry"); and a name with the same
  letters and digits in the same order as the current one, compared case-folded and without spaces
  or punctuation (`Sample Quest : Dark Version` against `Sample Quest - Dark Version` or
  `Sample Quest Of The Gb` against `Sample Quest of the GB`), since such a change carries no other
  language and is often a worse spelling. An accent is a difference, so `Camara` becomes `Cámara`.
- **Comparison.** Both sides are parsed values (`artwork.parse_gamelist` on the hub's file and on
  Skyscraper's, each stripped), after the removal of the characters XML 1.0 cannot hold that
  [Merge](#merge) applies, so `&quot;` or `&#39;` in the file against a plain quote in
  Skyscraper's text is not a difference, and a second run finds nothing to change.
- **The edit** is the text edit of [Merge](#merge): `gamelist.set_child` on the entry's block, so
  indentation, line endings, comments, every other tag and the `<alternativeEmulator>` block stay,
  and the result must be well-formed (`scrape.refresh_entries`). A change is left out, and its
  entry stays as it is, when the edit would not be a plain replacement that reads back with the new
  values: a value in a CDATA section, an element with attributes, an empty tag the parser reads as
  no value that would get a second tag, or an entry whose `<path>` the text spells another way
  (`&#38;`, a backslash) than the parsed one. These are counted per system and shown as `N left as
  they are (not editable)`, in a dry run and a real run alike: both compute their changes through
  `refresh_entries`, so the counts and the `renamed` lines are the same. The re-check, backup and rename of the merge apply: the
  file is read with its stamp, read again before the rename and the change planned again from the
  new content up to three times, and the backup of `<root>/tools/logs/gamelist-backups/` is made
  before the run's first change to a gamelist. A gamelist with nothing to change is not rewritten
  and gets no backup. The ES-DE warning of [Flow](#flow) is printed with this command's own name.
- **A convergent command.** It writes by default, keeps the backup and takes the inbox lock like
  `scrape`. `--dry-run` runs the same generate pass (Skyscraper from its cache; it is the only way
  to know the new text) and makes the same changes in memory, writes nothing to the hub, keeps no
  backup and takes no lock; its report is the one of a real run with "would refresh". Because it
  holds no lock, it never uses the folder or the log a locked run uses (`<scratch>/<system>` and
  `<system>.log`): it stages and generates in `<scratch>/<system>.dry-<random>` and logs to
  `<system>.dry-<random>.log`, so two dry runs do not collide. The folder is removed when it ends;
  the log too, unless a batch failed, so the failure line can name it.
- **Options.** `--system` limits it. It is refused with `--unattended` (an attended, deliberate
  operation, and the nightly run reports only news) and with `--retry-not-found` (it queries
  nothing), both as usage errors (exit 2). Without it, `scrape`, the scrape of `inbox --apply` and
  `scrape --unattended` run as before.
- **Memory.** Both memories of [The cache](#the-cache) are left as they are. The not-found one
  records what ScreenScraper answered to a request, and this option makes none. The incomplete one
  records what a merge left missing; a refresh that gives a bare entry text leaves a record that
  still says `bare`, which is harmless: the record is only read by the nightly run's skip, it is
  rewritten the next time the game is processed, and a game that lacks media is skipped as before.
- **Report.** warpzone's own lines, per system, then a totals line when more than one system was
  reached:

```
scrape: snes: would refresh the text of 12 of 40 entries (name 9, desc 12, genre 7); 3 not in the cache
  renamed    Sample Quest (USA).zip: "Sample Quest" -> "Quest de Muestra"
  ...
scrape: would refresh the text of 20 entries in 2 systems (name 15, desc 20, genre 11)
```

  "Of 40 entries" counts the entries it looked at and staged; "not in the cache" are those
  Skyscraper generated nothing for; "left as they are" are the changes of the bullet above that
  could not be made (shown when there are any). Every entry whose name changes has a `renamed` line, with the
  old and the new name in quotes (`(no name)` when the entry had none). The other fields are
  counted, not listed. The exit codes are those of `scrape`: `1` when a batch failed or a gamelist
  could not be read or written.

What this rests on in Skyscraper, none of it checked here with a real binary: that the generate
pass without `-s` reads only the cache and asks for nothing (the pass of step 4 already does, and
its gamelist is the only text `plan_merge` uses); that it writes a gamelist entry only for a game
the cache holds (as `plan_merge` already relies on for "not found"); and that the text in the cache
is the one picked when the game was gathered, so the option brings an entry to the language the
cache holds and cannot fetch a text the cache lacks (see [Language and region](#language-and-region)).

## Media kinds

| Hub kind | From Skyscraper | Pass |
|---|---|---|
| `covers` | box-2D, as is | main, `<output type="cover"/>` |
| `screenshots` | screenshot (title screen when there is none), as is | main, copied by Skyscraper with `miximages` |
| `miximages` | ES-DE's own miximage layout: framed screenshot, 3D box, physical media and logo | main, the `screenshot` output with `miximages` |
| `marquees` | wheel (the logo; ES-DE's `marquees` are logos) | main, `<output type="marquee" resource="wheel"/>` |
| `3dboxes` | box-2D with the `gamebox` effect, the logo on the spine | 3D box, its `cover` output |
| `backcovers` | box-2D-back, as is | main, with `backcovers` |
| `videos` | video, as is | main, with `videos` |
| `titlescreens`, `physicalmedia` | not available | none |

Skyscraper's ES-DE frontend handles only cover, backcover, fanart, manual, marquee, screenshot,
video and wheel; `miximages` and `3dboxes` come from the artwork files below. Skyscraper's
documentation calls the `miximages` flag ephemeral (it may go once ES-DE makes its own
miximages). If a later version drops it, the main pass outputs only plain images and a third pass
with the composite as its `screenshot` output feeds `miximages`, from the cache as well;
`doctor`'s version check (below) is where that shows.

## Artwork files

Skyscraper's default `artwork.xml` turns the `screenshot` output into a 640x480 composite
(screenshot, 3D box and wheel), which would land in the hub's `screenshots/`. warpzone never uses
the user's artwork file; it passes its own, shipped in `warpzone/data/skyscraper/`:

- `artwork-esde.xml`: a plain `cover` output (`<output type="cover" resource="cover"/>`), a `marquee` output from the `wheel` resource, and a
  `screenshot` output that is the miximage, laid out like the one ES-DE makes with its default
  settings (`MiximageGenerator.cpp`: 1280x960, include marquee, box and physical media). The
  canvas is transparent, as ES-DE's. The screenshot is inset 1060x800 at 150,80 (stretched, as ES-DE
  does for the usual aspect ratios) and scaled without smoothing (`transform="fast"`, Skyscraper's
  name for what ES-DE's default `MiximageScreenshotScaling` "sharp" does; Skyscraper's default
  smoothing blurs a 240x160 screen). Its frame is 12 px wide with rounded outer corners: a first
  `screenshot` layer, with the `rounded` and `stroke` effects, placed 12 px up and left of the
  screenshot, because Skyscraper draws a stroke outside the layer, in the layer's average colour,
  as ES-DE takes the frame colour from the screenshot; the plain screenshot goes on top.
  The wheel sits at the top right (it fits a 680x290 box, so a wide logo is wide and a tall one
  is tall, as in ES-DE), the 3D box at the bottom left (the cover in a 760x600 box with the
  `gamebox` effect, `side="wheel"`, as in the 3D box pass) and the physical media, which Skyscraper
  calls the `texture` resource (ScreenScraper's `support-2D`, a cartridge or disc), at the bottom
  next to the box. ES-DE adds the shadow (12 px down and to the right, blurred) to each image
  before it places it, so the content sits about 28 px in from the right edge for the wheel and
  about 30 px up from the bottom for the box and the media; the layers have the same shadow
  and the same negative `x` and `y`. A layer whose resource the game lacks is skipped, so a game
  without a wheel, box or physical media still gets its miximage. With `--flags miximages` this
  output goes to `miximages/` and the plain screenshot to `screenshots/`.

  Measured against a miximage ES-DE made for a GBA game (Tiny Toon Adventures; 1280x960, pixels),
  the frame is the same (138,68 to 1222,892), the logo's content runs from x 572 in both and
  ends at 1239 (ES-DE) and 1240, the box runs from y 341 to 924 (ES-DE) and 340 to 924, and the
  media from x 527 to 807 (both) with its bottom at 930 (both).

  What differs from ES-DE, because Skyscraper's artwork format cannot express it: the physical
  media has a fixed place (ES-DE puts it 32 px right of the box's image, whatever its width; the
  fixed x is the one ES-DE gives for a portrait GBA box, and it clears a landscape SNES box),
  the box is the cover with the `gamebox` effect and not ScreenScraper's own 3D box, the wheel
  fits a fixed box and ES-DE sizes it by its area (`calculateMarqueeSize`; Skyscraper trims a
  logo's transparent padding too, so both start from the logo's own shape). For the shapes that
  matter the box is close: a logo 4:1 comes out 680x170 (ES-DE: 682x170), one 1.5:1 comes out
  444x290 (ES-DE: 456x297), while a square one is 290x290 (ES-DE: 405x405) and a 2:1 one
  580x290 (ES-DE: 502x251). A square or tall cover gives a box about 430x598 (ES-DE's 3D box:
  463x600), a landscape one about 505x555 (ES-DE's: up to 620 wide). A screenshot is always
  stretched to the frame (ES-DE crops one wider than 16:10 or taller than 1:1) and black bars in
  a screenshot are kept (ES-DE removes letterboxes and pillarboxes).
- `artwork-3dbox.xml`: one `cover` output with the `gamebox` effect (`side="wheel"`), on a fixed
  600 by 600 transparent canvas (without a width the box comes out off centre and cropped).

They are data: a test parses them and checks the outputs and layers above, and the look is a
manual check against a miximage and a 3D box made by ES-DE.

`scrape` only adds media, so a miximage already on the hub keeps the old look. To rebuild them, move
the `miximages/` of the system away and run `warpzone scrape`: the games are in Skyscraper's cache,
so the rebuild makes no request to ScreenScraper.

## The cache

Skyscraper keeps what it gathers in its resource cache (`~/.skyscraper/cache/<platform>/`, or
`$XDG_CACHE_HOME/skyscraper/` in an XDG build), and warpzone uses the default location so the
user can manage it with Skyscraper's own `--cache` commands. The cache is what makes a run
resumable: with `onlymissing`, a game that has any data in it is not asked for again, so a run
stopped by the quota, the time limit or Ctrl-C goes on where it stopped, and a game whose entry a
console dropped is merged again with no request. The cost is that a game is never asked for again
once cached; to get media ScreenScraper added later, the user purges the game from the cache
(`Skyscraper -p <platform> --cache purge:...`) and runs `warpzone scrape`.

**Games ScreenScraper does not know.** They leave no data in Skyscraper's cache, so without help
every run would ask for them again. warpzone remembers them in its own small file on the machine
that scrapes, `$XDG_CACHE_HOME/warpzone/scrape-not-found.json` (next to the DAT cache, never on the
hub): one record per system and gamelist path, with the file's size and the date it was last
tried. `select` leaves out a game tried less than 30 days ago with the same size, and the report
counts them (`skipped 4 not found in the last 30 days`). `warpzone scrape --retry-not-found` tries
them all now. A game that is found later, or whose file changes size (a new dump under the same
name), drops its record. A file that cannot be read or parsed counts as empty and is rewritten.

**Games that stay incomplete.** A game ScreenScraper knows but has no video or back cover for is
in Skyscraper's cache, so it is never asked for again, yet `select` keeps finding it "missing
media". So every scrape (attended, or the one at the end of `inbox`) also records, in the same file
under the key `"incomplete"`, the games it left incomplete: per system and gamelist path, `{size,
missing: [kinds], bare, tried}`. A game is recorded only when its merge ended `added`, `filled` or
`unchanged`, the hub has an entry for it, none of its copies was dropped and the gamelist write
succeeded; `missing` is the kinds the hub still lacks after the merge, `bare` (written only when
true) says the entry has no text after the merge, and a game that lacks nothing and is not bare
loses its record. `scrape.skip_incomplete` is true when the size matches, the kinds a game lacks
now are among the recorded ones and, if its entry is bare now, the record says it stayed bare
(`bare`). Nothing in `select` reads the records: an attended `warpzone
scrape` ignores them and does the full pass, and only the
[nightly run](/warpzone/development/design/scrape-unattended/#the-incomplete-memory) skips on them.
An old file without the key, or a record without `bare`, still reads (a record with `missing`
empty and no `bare` says nothing and is dropped), and a record that cannot be read is dropped.

**Games ScreenScraper has no text for.** A bare entry is picked because its text is missing, so a
game for which ScreenScraper has no `desc`, genre or the like would be picked on every run. The
merge ends `unchanged` with the entry still bare, and the record above (`bare: true`, with the
size) is what stops the nightly run from picking it again; the record is dropped when the game is
next processed and its entry has text, and the game comes back when its file's size changes. The
same holds for a fill the writer could not apply although the gamelist was written (it finds no
block to edit, as for an entry with an XML comment inside it): the game is `unchanged`, not
`filled`, and is remembered as bare, so it is tried once and not every night. A fill that was
dropped because the gamelist could not be written records nothing. A game ScreenScraper does not know at all is `not found` and is in the not-found
memory above. An attended scrape ignores both and tries the bare entry again, as it does a game
missing a video.

A game is recorded only when Skyscraper itself said "not found" for it (a game it never got to,
after the quota, says nothing) and that answer did not come after a retry line. If ScreenScraper
is down, Skyscraper retries each request and then reports every game as not found, which
`classify_output` does not see as `NETWORK`; since the output is in order with one thread, the
lines between the previous answer and a game's belong to it, and a game answered after "Retrying
request with timeout of" or the too-busy line (`Outcome.retried`) is not recorded, so a bad day
does not hide a library for 30 days.

In the test, a game took about 10 seconds and 2.6 MB of media, most of it the video, so a library
of 1,000 games is about three hours and 2.6 GB on the hub, plus about as much in the cache on the
machine that scrapes. The videos stay on the hub: devices ignore `/*/videos` (see
[Hub, folders and ignores](/warpzone/development/design/hub-and-folders/)).

## In the hub image

Decided by the maintainer on 2026-11-13, replacing the deferral recorded here before: the hub's
container image ships Skyscraper, so the hub scrapes without a computer that mounts the share.
`scrape_io` runs it as a subprocess, as before; the image only has to put it on `PATH`. The
container paths of the [hub container guide](/warpzone/setup/hub-container/#scraping-in-the-container)
are these:

- **Built from source, pinned.** A stage of `docker/Dockerfile` builds Gemba's Skyscraper at the
  commit of tag 3.21.1 (the build stops if the tag points elsewhere, as for RALibretro and
  Dolphin) with Debian's Qt 6 and `qmake6`. `BUNDLE_QRC=1` puts the files Skyscraper otherwise
  installs under `/usr/local/etc/skyscraper` (`peas.json`, `platforms_idmap.csv`, the default
  artwork, the box and scanline images, `hints.xml`) inside the binary, which falls back to them
  when its home folder has none: one file, nothing to install, and nothing in the image that the
  user's volume could shadow. The Qt modules it links are Core, Gui (its images; no window is ever
  opened), Network, Xml and Sql, from the `libqt6*` runtime packages of Debian plus the SQLite
  driver; GPL-3.0, so it joins the licences paragraph of the guide, and the image is built by the
  user and not redistributed.
- **Its home is a volume.** Skyscraper (without XDG paths) keeps `config.ini`, the resource
  cache, imports and reports under `~/.skyscraper`, and creates what is missing at every start.
  The image's root is read-only and `/tmp` is memory, and the cache must survive (about 2.6 MB a
  game, and what makes a run stopped by the daily limit resumable), so the compose service mounts
  `./skyscraper` at `/skyscraper`, read-write and owned by the service's uid. `HOME` stays `/tmp`
  for everything else in the image (warpzone and the other tools), so
  `Skyscraper` on `PATH` is a two-line script that runs the real program (`/opt/skyscraper/Skyscraper`)
  with `HOME=/opt/skyscraper-home`, whose `.skyscraper` is a link to `/skyscraper`. The script is
  an `exec`, so the process group `runners.run_tool` kills at the time limit is still the
  program's. With nothing mounted the link dangles and Skyscraper stops at start.
- **warpzone is told where `config.ini` is.** The image sets `WARPZONE_SKYSCRAPER_DIR=/skyscraper`.
  `scrape.config_ini_path` returns `<that folder>/config.ini` when the variable is set, which is
  the only place warpzone looks (the presence check of `userCreds`, in `doctor` and in the scrape
  report). The credentials rule is unchanged: warpzone never reads the value, never prints it and
  never puts it on a command line; the file is created by the user on the NAS and is never in the
  image.
- **Doctor** checks Skyscraper in the container like anywhere else (see [Doctor](#doctor)).
- **The scrape of `inbox --apply` is the same code** in the container. Its staged folders and logs
  go to `[inbox] work_dir` (a mounted folder) and the not-found memory to `XDG_CACHE_HOME`
  (`/work/cache` in the compose example), as for any machine. The [unattended
  inbox](/warpzone/development/design/inbox-unattended/#scraping) scrapes what it places.

## In `inbox --apply`

After the catalog update, when Skyscraper is configured or found, `inbox --apply` scrapes the
games it placed in this run (not those "already in the library"), system by system, inside its
own lock. `--no-scrape` skips it for one run. The report mode (no `--apply`) runs no Skyscraper
and prints `scrape: <system>: would scrape N games` per system.

A scrape failure of any kind is a warning in the report: it never sends an item to review, never
changes the exit code and never undoes a placement. The report ends with the line to run later:
`warpzone scrape --system <system>`.

How it is done (`inbox_scrape.py`, called from `inbox._run` right after `_update_catalog`):

- **The games.** The units with outcome `placed` whose library folder is `roms/<system>` and whose
  system is in `scrape.PLATFORMS` (extras such as Switch updates are left out). `scrape.is_placed`
  keeps the catalog games whose primary path is one of those entries or lies inside one (an
  `.m3u` folder's playlist), or that has the entry as one of its parts or has it inside one (the
  update or the DLC folder of a PS4 game, placed for a game that is already there); `scrape_io.Options.only` carries them,
  so `select` sees nothing else of the library.
- **The lock.** `inbox` already holds `inbox/.warpzone.lock`, so it calls `scrape_io.run_placed`,
  which is `run_scrape` without taking the lock. `warpzone scrape` is the only caller that takes it.
- **The summary.** `scrape_io.run_held` is the same run with the lock held by the caller, and it
  returns a `scrape.Summary` instead of an exit code: per system the counts of the report (and the
  paths added and filled), why the run stopped early, how many games it did not try and which
  systems failed. `run_scrape` and `run_placed` return `Summary.exit_code`, so what `warpzone
  scrape` prints and its exit codes are the same. `run_held` also takes a `deadline`, a
  `still_mine` check and a `skip_incomplete` predicate for the unattended run (see
  [Unattended scrape](/warpzone/development/design/scrape-unattended/)).
- **Failures are warnings.** `Options.as_warning` sends the failure rows of the table below to
  `Reporter.warn` instead of the plain report, and any exception of the scrape is caught and
  warned (`scrape: failed (...)`). The return code of the scrape only decides whether the closing
  line `scrape: not finished; run `warpzone scrape --system <systems>` later` is printed. The
  return value of `inbox` stays the number of items for review.
- **A catalog that lacks the games.** When the catalog update failed (an exception, or a warning
  for a system), the placed games are not in the catalog and cannot be scraped. The scrape counts
  the placed entries of each system that no catalog game matches (also when the system has no
  catalog file) and warns `scrape: <system>: N placed games not in the catalog; run `warpzone
  catalog`, then `warpzone scrape --system <system>``. That counts as a failure of the run, so the
  closing `scrape: not finished; ...` line appears; the games of the system that are in the catalog
  are still scraped. Placement and the exit code of `inbox` are unaffected.
- **Without Skyscraper.** `inbox_core.find_tools` locates it like the other tools: a configured path
  that does not exist is a configuration error (exit 2, before anything is placed), else
  `Skyscraper` on `PATH`. When none is found, the report says once `scrape: Skyscraper not found; N
  games placed, not scraped.` with the hint, and only when something scrapable was placed. With
  `--no-scrape` it says `scrape: skipped (--no-scrape)` in the same cases. In the report mode the
  first line reads `scrape: Skyscraper not found; N games would not be scraped.`
- **Cache and logs.** Staged folders, logs and the Skyscraper config check are those of `warpzone
  scrape` (`<work_dir>/scrape/` from `[inbox] work_dir` or `--work-dir`).

## `warpzone scrape`

`warpzone scrape [--root PATH] [--system NAME ...] [--retry-not-found] [--refresh-text] [--dry-run] [--force-lock]` scrapes the games of the library
that have no gamelist entry, lack media or have a bare entry, for every system in the platform table, or only the
ones named with `--system` (a name not in the table is a usage error, exit 2). It reads `[inbox]` only, like `inbox`, and needs
the catalog: a system without a catalog file is skipped with the hint to run `warpzone catalog`.
It takes the inbox lock (`inbox/.warpzone.lock`, with its heartbeat, its stale rules and
`--force-lock`, see `development/design/inbox.md`), so it never runs while `inbox --apply`
places games or edits gamelists.

A planned `--unattended` option runs it once a night from a scheduled task, with a budget of
games and minutes, a lock wait and exit codes that report only news; it is specified in
[Unattended scrape](/warpzone/development/design/scrape-unattended/#the-command).

`--refresh-text` makes it a different run, which replaces the text of existing entries from the
cache: see [Refreshing the text](#refreshing-the-text). Without it, `--dry-run` runs no Skyscraper (no request, no change to the cache), writes nothing and takes no
lock. It prints,
per system, the games it would scrape, split into "no entry", "missing media" with the kinds and
"bare entry" (a game can be on two lines), and the totals, which is how a user sizes a first run
over a whole library before starting it.

## Report

warpzone prints its own lines, never Skyscraper's output. It reads that output only to classify
it (`scrape.classify_output`, pure, with the known lines of the tested version as fixtures), and
keeps it in `<work_dir>/scrape/<system>.log` (mode 0600, replaced by the next run, never on the
hub) for the user to read when a batch fails.

```
scrape: snes: 3 games (Skyscraper 3.21.1, ScreenScraper account: set)
  added      Kirby Super Star (USA).zip: covers, screenshots, miximages, marquees, 3dboxes, backcovers, videos
  filled     Chrono Trigger (USA).zip: videos, 3dboxes (entry kept)
  filled     Secret of Mana (USA).zip: fields desc, genre, developer
  not found  Example Homebrew (World).zip
scrape: snes: 1 added, 2 filled, 1 not found
```

`filled` means no new entry but something the hub lacked: media (`videos, 3dboxes`, and then the
entry is kept as it is), fields of the entry (`fields desc, genre`), or both (`videos; fields
desc`).

"not found" means the gather left no data for the game in the cache (ScreenScraper does not know
it, or the match was below Skyscraper's threshold): in the merge, Skyscraper's output has no entry
and no media for it, whether or not the hub has an entry. A game with an entry and nothing new to
fill (no medium and no field) is counted as unchanged, not listed. The kinds are listed in the order of the
[Media kinds](#media-kinds) table, a symbolic link or an entry that was dropped is a `warning`
line, and the games skipped because they were not found recently are counted in the last line.
`scrape.render_report` (pure, over a `Report` that holds the merged games) prints these lines, and
`scrape.stop_message`, `failed_message` and `gamelist_message` print the rows of the table below.

### Failures

| What happened | How it shows | Run |
|---|---|---|
| ScreenScraper's daily limit reached | `quota: ScreenScraper's daily request limit is reached; N games not tried. Run warpzone scrape again tomorrow.` | gather stops for every system; what is in the cache is still generated and merged |
| ScreenScraper closed, too busy for anonymous users, or Skyscraper refused | the reason, and for "too busy" the hint to add `userCreds` to `[screenscraper]` in `config.ini` | stops |
| No network | `ScreenScraper could not be reached` | stops |
| Skyscraper exits non-zero, cannot run, or is killed at the time limit | `Skyscraper failed on <system> (see <log>)` | that batch is skipped, the next one is tried |
| The gamelist changed three times during the write, or cannot be read | the reason; the entries are left for the next run | that system's batch is skipped |

Skyscraper's exit code says little (it exits 0 after "exiting nicely"), so the classification of
its output decides the first three rows; an output that matches nothing known is not a failure,
and the games without data are "not found".

**Exit codes of `scrape`.** `0` when every selected game was handled (scraped, filled, or not
found: a game ScreenScraper does not know stays unknown, and failing on it forever would hide the
failures that matter); `1` when the run could not finish (any row of the table above); `2` for a
usage or configuration error (no Skyscraper, an unknown system, a configured path that does not
exist, the lock held); `130` on Ctrl-C, after Skyscraper's process group is killed and the staged
and temporary folders are removed. Nothing on the hub is ever half-written, since every write is
a rename.

## Doctor

With an `[inbox]` table, `doctor` reports Skyscraper like the other optional inbox tools:

- `Skyscraper`: `[ok]` with the version (`Skyscraper --version`) when found; `[--]` when not
  found and not configured, saying that `inbox` will not scrape; `[!!]` when the configured path
  does not exist, is not executable or does not run; `[! ]` when the version is older than the
  tested one, since the flags warpzone passes may not exist there. The install hint is the
  project's releases page.
- `ScreenScraper account`: only when Skyscraper is found. `[ok]` "userCreds set in
  [screenscraper]" when the presence check finds the key; `[--]` "anonymous: 1 thread, 10,000
  requests a day; a free account doubles the requests" otherwise. The value is never read.

In the hub container (`WARPZONE_SKYSCRAPER_DIR` set) Skyscraper is checked like anywhere else, and
its `config.ini` is read from that folder. When the folder is not a directory (the volume is not
mounted) the `Skyscraper` line is `[--]`: "`/usr/local/bin/Skyscraper` is in the image, but nothing
is mounted at /skyscraper, where it keeps its config.ini and cache: inbox will not scrape", with
the fix `./skyscraper:/skyscraper` for the compose service, and Skyscraper is not run (it would
stop at start), so there is no account line. It is optional, `[--]` and not a problem, because the
`warpzone` service of the same image does not scrape and has no such volume.

## Code and testing

`scrape.py` holds the pure part: `PLATFORMS`, `FRONTEND_BASES`, `frontend_games`, `select`, `skyscraper_args`,
`classify_output`, `plan_merge` and the report. `scrape_io.py` stages, runs Skyscraper through
`runners.run_tool` (its own session, killed with its group at the time limit or on Ctrl-C), reads
the output and writes to the hub. The doctor's presence check is a pure function of the file's
lines plus a `Probes` read.

Unit tests, with no network and no Skyscraper:

- The platform table covers every system `inbox` places and has no other.
- `frontend_games` for each of the four systems: the shifted path, the games it drops, and that
  the staged name, the entry, the media stem and the not-found key all come out as the frontend
  names them; `select` and `plan_merge` then work as for any system. `scrape_io` tests stage a PS4
  and a Vita game as their launcher and run the stand-in tool over the four systems.
- `skyscraper_args` gives the exact command lines above, never `-u`, `-c` or `-t`, and no word
  from a fake `config.ini` holding credentials (`# gitleaks:allow`).
- The presence check finds `userCreds` only in `[screenscraper]`, handles comments, quotes and
  other sections, and its result and the doctor's output never hold the fake value.
- `select`: no entry, entry with missing kinds, entry complete, `<nomultiscrape>`, a media file in
  another extension counts as present, an `.m3u` folder's stem; a game not found 29 and 31 days
  ago, with the same and with another size, and with `--retry-not-found`.
- `plan_merge`: a field an existing entry has is never taken from Skyscraper, a bare entry gets
  its text, and its title only when the name was the file name; `fill_entries` keeps the file's
  indentation, line endings and every other tag; play statistics and `<nomultiscrape>` are never
  touched; new entries with only the listed
  fields and escaped text, absolute and `./` paths rewritten, paths outside the staged folder or
  unknown dropped, `.m3u` media renamed, symbolic links skipped, nothing planned over an existing
  file.
- `classify_output` on the quota, closed, too-busy, blacklisted and not-found lines copied from
  Skyscraper 3.21.1, and on unknown text.
- The writer with a temporary hub: a gamelist changed between read and rename is merged again; a
  media file that appears before the rename is kept and the copy dropped; the backup is made once
  per run; `--dry-run` writes nothing.
- `--refresh-text`: `refresh_fields` replaces the three fields, keeps one the cache gives empty,
  never touches another field or a `<nomultiscrape>` entry and sees no difference in an escape;
  `refresh_entries` keeps the file's indentation, line endings, comments and every other tag, adds
  a missing tag, leaves out an edit that would not read back, and a second pass changes nothing;
  `render_refresh` lists every name change. The writer tests use the stand-in tool: a dry run
  writes nothing to the hub, a backup is made only when the file changes, `--system` limits the
  run, no gather runs, and a run without the option is unchanged.
- One flow test runs `inbox --apply` and `scrape` with a stand-in `Skyscraper` script that writes a
  canned gamelist (with absolute paths) and media into the `-g` and `-o` it is given, and one that
  prints the quota line, and checks the hub's gamelist, media, report and exit code.

The image is checked by the `image` job of CI, on its manual and weekly runs: `Skyscraper
--version` as uid 65534 with `--read-only --tmpfs /tmp --cap-drop ALL` and a folder mounted at
`/skyscraper` (and a failure without it), and `warpzone doctor` in the container with `[ok]` for
`Skyscraper` and for the account of a fake `config.ini` on the volume, and the "nothing is mounted"
line without it. Unit tests cover the doctor's three cases and `config_ini_path` with the variable.

Manual checks, recorded in the pull request: a real Skyscraper run against ScreenScraper,
anonymous and with an account, on a few games of different systems (a cartridge `.zip`, a CHD, a
multi-disc `.m3u` folder, a NES or Atari 7800 dump with a header); the miximage and the 3D box
next to ES-DE's own; ES-DE on a console and the library UI showing the merged entries and media;
a run stopped half-way and resumed.
