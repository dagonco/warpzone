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
  change an existing entry, never overwrite a file) do not depend on how Skyscraper merges
  gamelists.
- **Credentials stay in Skyscraper's config.** They are optional and live only in
  `~/.skyscraper/config.ini`. warpzone never reads their value, never prints them and never puts
  them on a command line (see [Credentials](#credentials)).
- **Correctness on the command line, taste in the config.** warpzone passes every option that the
  hub layout depends on (frontend, folders, artwork file, the shape of the title, which media are
  fetched), so a user's `config.ini` cannot break it: Skyscraper gives the command line precedence
  over every section of the config. Language and region, which are a matter of taste, stay in the
  user's config and warpzone never sets them (see [Language and region](#language-and-region)).
- **When it runs.** Automatically in `inbox --apply` when Skyscraper is configured or found,
  only for what that run placed; a scrape failure is a warning and never blocks or undoes a
  placement. And on demand with `warpzone scrape` for games already in the library. A "Scrape"
  button in the library UI can come later on top of the same code.
- **Only adds.** An existing `<game>` entry's text is never changed: consoles write play data
  into it and the user may have edited it. A game without an entry gets one; a game with an
  entry only gets the media files it lacks. ES-DE's `<nomultiscrape>` is respected. No media file
  is ever overwritten.

## Scope and non-goals

In scope: the text fields and the media kinds listed in [Media kinds](#media-kinds), for the
systems in [Systems and platforms](#systems-and-platforms), into the hub's ES-DE gamelists and
media folders.

Not in scope:

- Changing or "improving" an existing scrape. A wrong match is fixed by the user (by editing the
  entry on a console, or by deleting the entry and its media and running `warpzone scrape`).
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

`langPrios`, `regionPrios` and `regionFromFilename` live in `config.ini` and warpzone does not
set them. The setup guide (written with the implementation) says how to change them and gives
these values:

```ini
[main]
langPrios="es,en"            ; any order of the languages the user reads; this is an example
regionPrios="eu,us,jp,wor"
regionFromFilename="first"
```

`regionFromFilename="first"` puts the regions of the file name first. Without it (the default is
`inline`) the European release wins over the one named in the file: "Kirby Super Star (USA)" got
the European title "Kirby's Fun Pak" in a test. The guide also says not to set `nameTemplate`,
which makes Skyscraper ignore the title options warpzone passes.

### What warpzone passes

| On the command line | In `config.ini` terms | Why warpzone sets it |
|---|---|---|
| `-p <platform>` | | from the [platform table](#systems-and-platforms) |
| `-s screenscraper` (gather only) | | the one source |
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

`scrape.PLATFORMS` maps a hub system folder to Skyscraper's platform name. It is data checked
against Skyscraper's `peas.json` (the platform list it ships; 3.21.1 at the time of writing):

| Hub system | Skyscraper platform | Note |
|---|---|---|
| `nes`, `snes`, `n64`, `gb`, `gbc`, `gba`, `nds`, `mastersystem`, `gamegear`, `atari2600`, `atari7800`, `atarilynx`, `wonderswan`, `wonderswancolor` | same name | |
| `genesis` | `megadrive` | the one cartridge system whose name differs |
| `sega32x` | `sega32x` | same name in both, though other tools say `32x` |
| `atarijaguar` | `atarijaguar` | same name in both, though dArkOS says `jaguar`; its formats are `.j64` and `.jag`, plus `.zip` |
| `psx`, `ps2`, `psp`, `saturn`, `dreamcast`, `gc`, `wii` | same name | CHD, ISO and RVZ are in Skyscraper's formats |
| `n3ds` | `3ds` | |
| `mame` | `mame` | ScreenScraper matches arcade sets by set name |
| `ps3`, `xbox`, `switch` | same name | matched by name; `inbox` keeps the file's own name for these |

Left out for now: `ps4` and `psvita` (the gamelist entry is a launcher, a `.desktop` or `.psvita`
file, whose name ScreenScraper cannot match), and `xbox360` and `wiiu` (their games live below
`roms/<system>/roms/`, which the frontend reads as the system folder, so their gamelist paths
need the same shift as in [Frontend artwork](/warpzone/development/design/frontend-artwork/)).
They are in `scrape.UNSCRAPED` with that reason, and a test checks that every system `inbox` can
place (the values of `dat.SYSTEMS` and the systems of the kind handlers) is in exactly one of the
two. A new system in `inbox` therefore fails the test until it is given a platform or a reason.
Skyscraper adds `.zip`, `.7z` and `.m3u` to every platform's formats, which covers the zips
`inbox` writes and the `.m3u` folders below.

## Flow

Both entry points run the same function on a list of games, one system at a time. A game is a
catalog entry (see [The catalog index](/warpzone/development/design/library-ui/#the-catalog-index)):
its `primary` part and its `gamelist` path. `inbox --apply` updates the catalog before it
scrapes, so the games it placed are there.

1. **Select** (pure, `scrape.select`). From the catalog's games of the system, the hub gamelist
   and the media files present, it returns the games to scrape: those without an entry, and those
   with an entry (not marked `<nomultiscrape>`) that lack a file of a kind in
   [Media kinds](#media-kinds). `inbox` passes only the games it placed in this run; `scrape`
   passes all of the system's.
2. **Stage.** In `<work_dir>/scrape/<system>/input/`, each game gets a symbolic link at its
   `gamelist` path (subfolders kept) to the file on the share. Nothing is copied: a CHD is
   hundreds of MB and Skyscraper only reads it. An `.m3u` folder is staged as a file, a link named
   after the folder that points at the playlist inside it (`Game.m3u` -> `.../Game.m3u/Game.m3u`),
   so Skyscraper sees one game, searches by the name without disc tags and writes one entry. The
   map from staged name to game is kept for the merge; Skyscraper's paths are never trusted.
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
`unchanged`), the entry to add and the files to copy (pass, source, destination), plus what it
dropped and why. The writer only executes it; `scrape.render_entries` gives the text of the new
entries, escaped, for the indentation and newline style the writer copies from the file. Its rules:

- **Matching Skyscraper's entries.** An entry's `<path>` is taken as relative to the staged
  folder: `./Game.zip`, `Game.zip` and an absolute path inside the staged folder all mean
  `Game.zip`. A path outside it, one with `..`, or one not in the staged map is dropped with a
  warning. The entry written to the hub has the catalog's `gamelist` path, never Skyscraper's.
- **Existing entries.** A `<game>` or `<folder>` of the hub gamelist whose `<path>` is the game's
  (`./` or not) is existing: nothing of it is changed, and only media are considered. One marked
  `<nomultiscrape>true</nomultiscrape>` gets nothing at all.
- **New entries.** For a game without one: a `<game>` with the catalog's path and, from
  Skyscraper's entry, `name`, `desc`, `rating`, `releasedate`, `developer`, `publisher`, `genre`
  and `players` (the fields the library UI reads), when present and not empty. Nothing else is
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
- **The gamelist is edited as text** with the helpers the arcade and PS3 edits use
  (`inbox_mame`): the new entries go before `</gameList>`, indentation and newline style are
  copied, comments and ES-DE's `<alternativeEmulator>` block are left as they are, and the result
  is checked to be well-formed before it is written to `.gamelist.xml.wz-tmp` beside the gamelist
  (`safefile.atomic_write`) and renamed. A missing gamelist is created, with the system folder,
  when there is an entry to add.
- **Re-check before replacing.** Consoles write the hub gamelist through Syncthing at any time.
  The file is read with its stamp (size, modification time, and its bytes); right before the
  rename it is read again, and if it changed the plan is made again from the new content and the
  write retried, three times, then the system's entries are left for the next run with a warning.
  The plan only adds, so making it again is always safe: an entry a console wrote for the same
  game in the meantime is kept as it is and the new one is not added.
- **Backup.** Before the run's first change to a gamelist, a copy goes to
  `<root>/tools/logs/gamelist-backups/<system>-gamelist-<YYYYMMDD-HHMMSS>.xml`, as for the inbox's
  gamelist edits (outside every synced folder, so it reaches no console). `scrape` is a
  convergent command (see [Writing commands](/warpzone/development/design/configuration/#writing-commands)):
  it writes by default, takes `--dry-run`, only adds, and a second run with nothing new changes
  nothing.
- **ES-DE keeps its gamelist in memory.** A console with ES-DE open writes back the gamelist it
  read at start, without the new entries. As in `inbox`, a gamelist written in the last 10 minutes
  (`recently_written`) gets one warning per run saying to quit ES-DE on every console and run
  `warpzone scrape` again if the entries do not stick. A later run adds them again from the cache,
  with no request; the media are already there.

## Media kinds

| Hub kind | From Skyscraper | Pass |
|---|---|---|
| `covers` | box-2D, as is | main, `<output type="cover"/>` |
| `screenshots` | screenshot (title screen when there is none), as is | main, copied by Skyscraper with `miximages` |
| `miximages` | composite of screenshot, box and logo | main, the `screenshot` output with `miximages` |
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
  `screenshot` output that is the miximage: ES-DE's miximage layout, 1280x960, with the screenshot
  as the background, the box at the bottom left and the logo at the top. With `--flags miximages`
  this output goes to `miximages/` and the plain screenshot to `screenshots/`.
- `artwork-3dbox.xml`: one `cover` output with the `gamebox` effect (`side="wheel"`), on a fixed
  600 by 600 transparent canvas (without a width the box comes out off centre and cropped).

They are data: a test parses them and checks the outputs above, and the look is a manual check
against a miximage and a 3D box made by ES-DE.

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
  system is in `scrape.PLATFORMS` (extras such as Switch updates, and the systems in `UNSCRAPED`,
  are left out). `scrape.is_placed` keeps the catalog games whose primary path is one of those
  entries or lies inside one (an `.m3u` folder's playlist); `scrape_io.Options.only` carries them,
  so `select` sees nothing else of the library.
- **The lock.** `inbox` already holds `inbox/.warpzone.lock`, so it calls `scrape_io.run_placed`,
  which is `run_scrape` without taking the lock. `warpzone scrape` is the only caller that takes it.
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

`warpzone scrape [--root PATH] [--system NAME ...] [--retry-not-found] [--dry-run] [--force-lock]` scrapes the games of the library
that have no gamelist entry or lack media, for every system in the platform table, or only the
ones named with `--system` (a name not in the table is a usage error, exit 2, which says why a
system is left out when it is in `UNSCRAPED`). It reads `[inbox]` only, like `inbox`, and needs
the catalog: a system without a catalog file is skipped with the hint to run `warpzone catalog`.
It takes the inbox lock (`inbox/.warpzone.lock`, with its heartbeat, its stale rules and
`--force-lock`, see `development/design/inbox.md`), so it never runs while `inbox --apply` places games or edits gamelists.

`--dry-run` runs no Skyscraper (no request, no change to the cache), writes nothing and takes no
lock. It prints,
per system, the games it would scrape, split into "no entry" and "missing media" with the kinds,
and the totals, which is how a user sizes a first run over a whole library before starting it.

## Report

warpzone prints its own lines, never Skyscraper's output. It reads that output only to classify
it (`scrape.classify_output`, pure, with the known lines of the tested version as fixtures), and
keeps it in `<work_dir>/scrape/<system>.log` (mode 0600, replaced by the next run, never on the
hub) for the user to read when a batch fails.

```
scrape: snes: 3 games (Skyscraper 3.21.1, ScreenScraper account: set)
  added      Kirby Super Star (USA).zip: covers, screenshots, miximages, marquees, 3dboxes, backcovers, videos
  filled     Chrono Trigger (USA).zip: videos, 3dboxes (entry kept)
  not found  Example Homebrew (World).zip
scrape: snes: 1 added, 1 filled, 1 not found
```

"not found" means the gather left no data for the game in the cache (ScreenScraper does not know
it, or the match was below Skyscraper's threshold): in the merge, Skyscraper's output has no entry
and no media for it, whether or not the hub has an entry. A game with an entry and nothing new to
fill is counted as unchanged, not listed. The kinds are listed in the order of the
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

`scrape.py` holds the pure part: `PLATFORMS` and `UNSCRAPED`, `select`, `skyscraper_args`,
`classify_output`, `plan_merge` and the report. `scrape_io.py` stages, runs Skyscraper through
`runners.run_tool` (its own session, killed with its group at the time limit or on Ctrl-C), reads
the output and writes to the hub. The doctor's presence check is a pure function of the file's
lines plus a `Probes` read.

Unit tests, with no network and no Skyscraper:

- The platform table covers every system `inbox` places, in `PLATFORMS` or `UNSCRAPED`, and has no
  system twice.
- `skyscraper_args` gives the exact command lines above, never `-u`, `-c` or `-t`, and no word
  from a fake `config.ini` holding credentials (`# gitleaks:allow`).
- The presence check finds `userCreds` only in `[screenscraper]`, handles comments, quotes and
  other sections, and its result and the doctor's output never hold the fake value.
- `select`: no entry, entry with missing kinds, entry complete, `<nomultiscrape>`, a media file in
  another extension counts as present, an `.m3u` folder's stem; a game not found 29 and 31 days
  ago, with the same and with another size, and with `--retry-not-found`.
- `plan_merge`: existing entries left as they are byte for byte, new entries with only the listed
  fields and escaped text, absolute and `./` paths rewritten, paths outside the staged folder or
  unknown dropped, `.m3u` media renamed, symbolic links skipped, nothing planned over an existing
  file.
- `classify_output` on the quota, closed, too-busy, blacklisted and not-found lines copied from
  Skyscraper 3.21.1, and on unknown text.
- The writer with a temporary hub: a gamelist changed between read and rename is merged again; a
  media file that appears before the rename is kept and the copy dropped; the backup is made once
  per run; `--dry-run` writes nothing.
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

## Deferred

Decided by the maintainer to leave for later (2026-11-13):

1. **`xbox360`, `wiiu`, `ps4` and `psvita`.** Scraping them needs the frontend-base shift and, for
   the launchers, a name to search by (the shortcut's or launcher's title). They stay in
   `UNSCRAPED` until then.
