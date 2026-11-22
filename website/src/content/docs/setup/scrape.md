---
title: "Scraping: descriptions and media"
---

How to give the games in the hub a description, covers, screenshots and videos, with Skyscraper and ScreenScraper.

A game that `warpzone inbox` files arrives on every console without a description, a cover or a video. warpzone fills that in on the machine that files the game, by running [Skyscraper](https://github.com/Gemba/skyscraper) (the Gemba fork), which gets the data from [ScreenScraper](https://www.screenscraper.fr). The results go into the hub's ES-DE gamelists and media folders, so every console and the [library UI](/warpzone/setup/hub-container/) show them:

```
<hub root>/frontend/es-de/gamelists/<system>/gamelist.xml
<hub root>/frontend/media/<system>/<kind>/<name>.<ext>
```

Two things trigger it:

- `warpzone inbox --apply` scrapes the games it has just placed, when Skyscraper is set up (it never scrapes anything else, and a scrape that fails never undoes a placement).
- `warpzone scrape` scrapes the games already in the library that have no entry, lack media or have an entry with no description or other text.

It only adds, unless you ask for [`--refresh-text`](#moving-existing-entries-to-your-language). A field an existing gamelist entry has is never changed (consoles write play data into it, and you may have edited it), a game with an entry only gets what it lacks (the media files, and the description, genre, developer, publisher, release date, players and rating the entry has no value for), and no media file is ever overwritten. An entry that holds only a name and play data, as ES-DE or the play state leave one, is filled in like this, and its name is replaced by the game's title only when it was just the file name. Play statistics, the emulator choice and favourites are never touched. A game marked `<nomultiscrape>true</nomultiscrape>` is left alone. The design is in [Scraping](/warpzone/development/design/scrape/).

## Install Skyscraper

warpzone is tested with Skyscraper 3.21.1 from the Gemba fork. It runs where `warpzone inbox` runs, not on a console: in the hub container, which ships it, or on a computer that mounts the share, where you install it.

- **In the hub container:** nothing to install. The image has Skyscraper on `PATH`; mount a folder for its config and cache at `/skyscraper` (the compose example does) and see [Scraping in the container](/warpzone/setup/hub-container/#scraping-in-the-container). Everything below applies there too, with `~/.skyscraper` being that folder.

On a computer:

- **macOS:** follow the macOS section of the project's [README](https://github.com/Gemba/skyscraper#readme). If you build it yourself, install Qt from Homebrew first (`brew install qt`).
- **Linux:** install the distribution package if there is one, or build it following the project's [installation instructions](https://gemba.github.io/skyscraper/INSTALLATION/). Prebuilt binaries are on the [releases page](https://github.com/Gemba/skyscraper/releases).

warpzone looks for `Skyscraper` on `PATH`. To use another copy, set its path in the `[inbox]` table of the config (a path that does not exist is a configuration error, exit code 2):

```toml
[inbox]
skyscraper = "/usr/local/bin/Skyscraper"
```

Run `warpzone doctor` to check it: it shows the version found (a warning when it is older than 3.21.1, since the options warpzone passes may not exist there) and whether a ScreenScraper account is set up. `tool_timeout_minutes` and `work_dir` of the same table apply to Skyscraper too: the staged and temporary files and the logs live in `<work_dir>/scrape/` (without `work_dir`, in `$XDG_CACHE_HOME/warpzone/scrape/`).

## ScreenScraper account (optional)

ScreenScraper works without an account. The limits (they are ScreenScraper's, and can change):

| Account | Threads | Requests per day |
|---|---|---|
| none (anonymous) | 1 | 10,000 |
| free account | 1 | 20,000 |
| contributor | more, by contribution level | more |

Skyscraper uses one thread and the number of threads the account allows; warpzone never sets it. When ScreenScraper is too busy for anonymous users, an account helps.

If you have an account, put it in **Skyscraper's own config**, never in warpzone's:

```ini
# ~/.skyscraper/config.ini (in the hub container: ./skyscraper/config.ini next to compose.yaml)
[screenscraper]
userCreds="USER:PASSWORD"
```

(`USER:PASSWORD` is a placeholder; Skyscraper builds that use XDG paths read `$XDG_CONFIG_HOME/skyscraper/config.ini` instead.) warpzone never reads the value, never prints it and never puts it on a command line: the only thing it checks, for `doctor` and the scrape report, is that a `userCreds` key exists in `[screenscraper]`. Keep the file private (`chmod 600 ~/.skyscraper/config.ini`; in the container, owned by the service's uid and mode 600).

## Region and language

ScreenScraper holds one box, screenshot and logo for each region of a game and one description for each language, and Skyscraper picks one of each when it gathers. Left alone it tries Europe, then the USA, then ScreenScraper's own, the UK, the world and Japan, and English first for the text. Name your own first choice in the `[inbox]` table of `config.toml`:

```toml
[inbox]
scrape_region = "sp"      # Spain first, then Skyscraper's own order (Europe, USA, ...)
scrape_language = "es"    # Spanish first, then English, German, French
```

That is Spanish first, then European and English, which is what most of a European library wants; both keys are optional and the values above are only an example. Each is one code: `scrape_region` a region (`sp` Spain, `eu` Europe, `wor` World, `uk`, `us`, `jp`; the [full list](https://github.com/Gemba/skyscraper/blob/3.21.1/docs/REGIONS.md)) and `scrape_language` a language (`es`, `en`, `fr`, `de`, `ja`; the [full list](https://github.com/Gemba/skyscraper/blob/3.21.1/docs/LANGUAGES.md)). A region decides the logo, the screenshot, the box and the title; a language decides the description and the genre. warpzone passes them as `--region` and `--lang` on the gather, and a code Skyscraper does not document is a configuration error. Unset, warpzone passes nothing.

Things to know:

- **Games already scraped keep what they have.** The setting reaches the games scraped from now on. warpzone never overwrites a medium or a field of an entry it already wrote (it only fills in what is empty), and Skyscraper keeps what it fetched in its cache. To move the name, description and genre of entries already on the hub to the text the cache holds, see [Moving existing entries to your language](#moving-existing-entries-to-your-language). To redo a game completely, delete its media files and its `<game>` entry from the hub's gamelist, purge the cache of that system (`Skyscraper -p <platform> --cache purge:m=screenscraper`, which cannot be undone; the hub image's `config.ini` and cache are on its `/skyscraper` volume), and run `warpzone scrape --system <system>`.
- **Only one code each.** Skyscraper's command line takes one region and one language, and puts it first in its order. For a longer order of your own, such as `regionPrios="sp,eu,wor,uk,us"`, use Skyscraper's `config.ini` in its `[main]` section (or one named after a platform, such as `[snes]`), as below; the two combine, and `scrape_region` goes first in whatever list `config.ini` holds.
- **The file name's region.** With `scrape_region` set, a region in the file name such as `(USA)` no longer goes first; `--region` replaces it. If you want each game's own region to win, leave `scrape_region` unset and put `regionFromFilename="first"` in `config.ini`. In a test, "Kirby Super Star (USA)" got the European title "Kirby's Fun Pak" with the defaults.

```ini
[main]
langPrios="es,en"              ; the languages you read, in order of preference
regionPrios="sp,eu,wor,uk,us"  ; the regions to take art and titles from, in order
regionFromFilename="first"     ; the region named in the file name goes first
```

Do not set `nameTemplate` in `config.ini`: it makes Skyscraper ignore the title options warpzone passes. Everything the hub layout depends on (the frontend, the folders, the shape of the title, which media are fetched) warpzone passes on the command line, which Skyscraper puts before every section of `config.ini`, so your file cannot break it.

## What you get

For a game without an entry, warpzone adds a `<game>` with the name, description, rating, release date, developer, publisher, genre and number of players ScreenScraper has (empty fields are left out), and the media it finds:

| Kind | What it is |
|---|---|
| `covers` | front of the box |
| `screenshots` | a screenshot (the title screen when there is none) |
| `miximages` | ES-DE's own composite: framed screenshot, 3D box, cartridge or disc and logo |
| `marquees` | the logo |
| `3dboxes` | the box in 3D with the logo on the spine |
| `backcovers` | back of the box |
| `videos` | the video preview |

Not produced: `titlescreens` and `physicalmedia` as kinds of their own. The cartridge or disc scan Skyscraper fetches appears only inside the miximage; ES-DE and the library UI fall back to the other kinds for the rest. Manuals and fanart are not fetched either.

The miximage follows the layout ES-DE itself makes by default. `scrape` only adds media, so miximages made before this layout keep the old look. To get the new one, move the `miximages/` folder of a system away and run `warpzone scrape` again: it rebuilds them from Skyscraper's cache without asking ScreenScraper for anything.

A game takes about 10 to 18 seconds (18 on a NAS) and 2.6 MB of media, most of it the video, so a library of 1,000 games is about three to five hours and 2.6 GB on the hub (the videos stay on the hub; consoles ignore them) plus about as much in Skyscraper's cache on the machine that scrapes (in the container, `skyscraper/cache/` on the NAS's disk).

## Scraped systems

Every system folder that [ES-DE](https://es-de.org) 3.5 names and that Skyscraper 3.21.1 has a platform for is scraped: 138 folders. A regional or variant folder is scraped as the platform it shares (`megadrivejp` and `genesis` as `megadrive`, `famicom` as `nes`, `sfc` and `snesna` as `snes`). `warpzone scrape --system NAME` takes any of these names.

| Maker | System folders |
|---|---|
| Nintendo | `nes`, `famicom`, `fds`, `snes`, `sfc`, `snesna`, `n64`, `n64dd`, `gb`, `gbc`, `gba`, `nds`, `n3ds`, `gc`, `wii`, `wiiu`, `switch`, `virtualboy`, `pokemini`, `gameandwatch` |
| Sega | `mastersystem`, `mark3`, `sg-1000`, `genesis`, `megadrive`, `megadrivejp`, `segacd`, `megacd`, `megacdjp`, `sega32x`, `sega32xjp`, `sega32xna`, `saturn`, `saturnjp`, `dreamcast`, `gamegear` |
| Sony | `psx`, `ps2`, `ps3`, `ps4`, `psp`, `psvita` |
| Microsoft | `xbox`, `xbox360` |
| Atari | `atari2600`, `atari5200`, `atari7800`, `atari800`, `atarijaguar`, `atarijaguarcd`, `atarilynx`, `atarist` |
| NEC | `pcengine`, `tg16`, `supergrafx`, `pcenginecd`, `tg-cd`, `pcfx`, `pc88`, `pc98` |
| SNK | `neogeo`, `neogeocd`, `neogeocdjp`, `ngp`, `ngpc` |
| Bandai | `wonderswan`, `wonderswancolor` |
| Arcade | `arcade`, `mame`, `mame-advmame`, `fba`, `fbneo`, `cps`, `cps1`, `cps2`, `cps3`, `model2`, `model3`, `stv`, `naomi`, `naomigd`, `atomiswave` |
| Commodore | `c64`, `plus4`, `vic20`, `amiga`, `amiga600`, `amiga1200`, `amigacd32`, `cdtv` |
| Other computers | `amstradcpc`, `apple2`, `apple2gs`, `bbcmicro`, `coco`, `dragon32`, `tanodragon`, `fm7`, `fmtowns`, `macintosh`, `moto`, `to8`, `msx`, `msx1`, `msx2`, `msxturbor`, `oric`, `samcoupe`, `ti99`, `trs-80`, `x1`, `x68000`, `zx81`, `zxspectrum`, `zmachine` |
| Other consoles and handhelds | `3do`, `cdimono1`, `colecovision`, `intellivision`, `vectrex`, `channelf`, `arcadia`, `astrocde`, `odyssey2`, `videopac`, `crvision`, `gamecom`, `gmaster`, `megaduck`, `pv1000`, `scv`, `supervision`, `vsmile` |
| Fantasy consoles and others | `pico8`, `tic80`, `arduboy`, `vircon32`, `palm` |

Notes:

- **Arcade.** The folders with sets named like MAME's (`arcade`, `mame`, `fba`, `fbneo`, the Capcom and Sega board folders) are asked of ScreenScraper as MAME sets, by set name, the same way `mame` always was. `neogeo`, `naomi`, `naomigd` and `atomiswave` have a platform of their own and use it.
- **PS4 and PS Vita.** A game is scraped by the name of its launcher (`<Title>.desktop` in `shortcuts/` for PS4, `<Title>.psvita` for Vita), so a game with no launcher is not scraped. A Wii U, Xbox 360 or Model 2 game is scraped by the name of its file, which ES-DE reads from `roms/<system>/roms/`.
- **Only systems with games.** A folder with no games, or no catalog yet, is skipped (`warpzone catalog` makes the catalog).

Not scraped, because Skyscraper 3.21.1 cannot do it for them:

- **No platform of its own:** `adam`, `android`, `androidapps`, `androidgames`, `archimedes`, `atarixe`, `electron`, `flash`, `fpinball`, `gamate`, `gx4000`, `lcdgames`, `lowresnx`, `lutro`, `mess`, `mugen`, `multivision`, `satellaview`, `sgb`, `spectravideo`, `sufami`, `supracan`, `uzebox`, `vpinball`, `wasm4`, `xboxone` and `zxnext`. For a few of them Skyscraper lists the name as an alias of another platform, which does not make ScreenScraper treat the games as that platform's.
- **A platform ScreenScraper has no system id for:** `chailove`, `j2me`, `naomi2`, `ngage` and `symbian`.
- **Games that are folders, launchers or scripts** (a game is not one file Skyscraper can read, so warpzone does not guess): `ags`, `consolearcade`, `daphne`, `desktop`, `doom`, `dos`, `easyrpg`, `emulators`, `epic`, `kodi`, `laserdisc`, `lutris`, `openbor`, `pc`, `pcarcade`, `ports`, `quake`, `scummvm`, `solarus`, `steam`, `triforce`, `type-x`, `windows`, `windows3x` and `windows9x`.

`warpzone scrape --system` with one of these is a usage error. The design behind the table is in [Systems and platforms](/warpzone/development/design/scrape/#systems-and-platforms).


## Scraping what `inbox` places

Nothing to do once Skyscraper is found: `warpzone inbox --apply` places the games, updates the catalog and then scrapes the games it placed in this run, system by system, while it still holds the inbox lock. The report shows the lines of the scrape before the final summary:

```
scrape: snes: 1 game (Skyscraper 3.21.1, ScreenScraper account: anonymous)
  added      Kirby Super Star (USA).zip: covers, screenshots, miximages, marquees, 3dboxes, backcovers, videos
scrape: snes: 1 added
```

- A scrape that fails (the daily limit is reached, ScreenScraper is closed or unreachable, Skyscraper fails or runs out of time) is a **warning**. The games stay placed, no item goes to review, `inbox` exits as it would have without the scrape, and the report ends with `scrape: not finished; run warpzone scrape --system <system> later`.
- Without Skyscraper the report says `scrape: Skyscraper not found; N games placed, not scraped`, and nothing else changes.
- `--no-scrape` skips the scrape for one run.
- Without `--apply` the report only says `scrape: <system>: would scrape N games`; Skyscraper does not run.

## Scraping the library: `warpzone scrape`

```bash
warpzone scrape --dry-run                # what it would do, per system; no Skyscraper, no changes
warpzone scrape                          # every scraped system
warpzone scrape --system snes gba        # only these
warpzone scrape --retry-not-found        # also the games ScreenScraper did not know recently
```

Use `--dry-run` first on a big library: it lists the games with no entry, the ones missing media and the ones with a bare entry, with their totals, which is how to size a first run. It needs the [catalog index](/warpzone/reference/commands/#warpzone-catalog); a system without one is skipped with the hint to run `warpzone catalog`. It takes the inbox lock while it writes (so it never runs while `inbox --apply` does), and `--force-lock` takes over a stale one.

The run is resumable. Skyscraper keeps what it gathers in its own cache, so a run stopped by the daily limit, the time limit or Ctrl-C goes on where it stopped when you run it again (tomorrow, for the limit), asking for nothing twice. Games ScreenScraper does not know are remembered for 30 days in `$XDG_CACHE_HOME/warpzone/scrape-not-found.json` on the computer that scrapes (never on the hub); `--retry-not-found` tries them now, and a game whose file changes size is tried again by itself.

Exit code `0` when every selected game was handled (a game ScreenScraper does not know is not a failure), `1` when the run could not finish, `2` for a usage or configuration error, `130` on Ctrl-C. warpzone prints its own lines, never Skyscraper's output, which is kept in `<work_dir>/scrape/<system>.log` (mode 0600) for you to read when a batch fails.

To fix a wrong match, edit the entry on a console, or delete the entry and its media files and run `warpzone scrape` again. To get media ScreenScraper added later for a game that was already scraped, purge that game from Skyscraper's cache (`Skyscraper -p <platform> --cache purge:...`) and run `warpzone scrape`.

If ES-DE is open on a console, it keeps its gamelist in memory and may write it back without the new entries. When a gamelist was written in the last 10 minutes warpzone warns about it: quit ES-DE on every console and run `warpzone scrape` again if the entries do not stick (the media are already there, and the entries come back from the cache with no request).

## Moving existing entries to your language

Entries scraped before you set `scrape_language` keep their name, description and genre in the old language, because `warpzone scrape` never changes a field an entry has. If Skyscraper's cache already holds the text in your language (it does for the games gathered after you set it), `--refresh-text` copies it into the entries:

```bash
warpzone scrape --refresh-text --dry-run --system snes   # list what would change
warpzone scrape --refresh-text --system snes             # do it
```

It asks ScreenScraper for nothing, so it needs no account and spends no daily request; it only runs Skyscraper on its cache. It replaces the `name`, `desc` and `genre` of the `<game>` entries and touches nothing else: no media file, and no other field (favorites, play counts, ratings, release dates and the rest stay as they are). A game the cache does not have, a field the cache has empty and an entry marked `<nomultiscrape>` stay as they are, a game with no entry is not added, and a name that Skyscraper only gives as the file name never replaces the entry's name. An entry whose text cannot be replaced in place (a value in a CDATA section, a tag with attributes) is left as it is and counted in the report as `left as they are (not editable)`; edit it by hand if you want it changed. Running it twice changes nothing the second time, and a gamelist with nothing to change is not rewritten. Like `scrape`, it keeps a backup of every gamelist it changes in `tools/logs/gamelist-backups/` on the hub.

A name that differs from the current one only in capital letters, spaces or punctuation is left as it is. **A name you corrected by hand is replaced too.** The option cannot tell a title ScreenScraper gave from one you wrote, so run `--dry-run` first (it makes the same changes in memory, runs Skyscraper on its cache in a folder of its own and writes nothing to the hub): it prints every name change as `"old name" -> "new name"`, and counts the other fields. If a name you want to keep shows up, edit it again afterwards, or mark the entry `<nomultiscrape>true</nomultiscrape>` before the run. A game whose text in the cache is still in the old language is rewritten with that same text, so nothing changes for it; to bring it over, purge it from the cache and scrape it again as described above.

If ES-DE is open on a console it may write its copy of the gamelist back over the change; quit ES-DE on every console and run the command again if the new text does not stick. `--refresh-text` cannot be used with `--unattended` or `--retry-not-found`.

## Filling art every night

A game that reaches the library without going through `inbox` (copied by hand, placed with `--no-scrape`, or left behind when a run hit ScreenScraper's daily limit) stays without art until someone runs `warpzone scrape`. On the hub you can leave that to a scheduled task that runs `warpzone scrape --unattended` once a night, with nobody watching.

What a night does:

- **It picks what is worth it.** New games with no entry go first, then games with an entry but no cover, screenshot or miximage, or an entry with no description or other text, then games ScreenScraper did not know whose 30 days have passed, and last games that only lack a video, back cover, 3D box or logo. It takes at most `--max-games` of them (default 500) and stops before a batch that would end after `--max-minutes` (default 180). What is left is picked up the next night, so a big library fills over several nights. On a NAS with a ScreenScraper account a night of 500 games took about two and a half hours and did not reach the quota.
- **It does nothing when there is nothing to do.** It exits at once, with no lock and no Skyscraper, so an up-to-date library costs nothing.
- **It skips games that cannot improve.** When ScreenScraper has no video or back cover for a game, the game would be "missing media" for ever. Every scrape remembers such games (per machine, next to the list of games ScreenScraper did not know) and the nightly run does not ask for them again; they come back when their file changes size or a media file they had goes missing. The same goes for a game whose entry has no text and for which ScreenScraper has none: it is tried once, remembered, and left alone by the nightly run from then on.
- **It takes the inbox lock.** If an `inbox` tick or a scrape by hand holds it, the run waits up to 30 minutes (a lock silent for 10 minutes is taken over). The inbox's own ticks during the run exit as busy and try again at the next one, so a game dropped at 03:30 is filed when the run ends, at most `--max-minutes` later.
- **It runs at low priority** and uses the same Skyscraper, cache and account as the rest.

On a Synology, in Control Panel, Task Scheduler, Create, Scheduled Task, User-defined script: user `root`, daily at 03:00 (ScreenScraper's daily limit has reset by then), script:

```sh
cd /path/to/the/compose/folder && /usr/local/bin/docker compose run --rm -T warpzone-inbox scrape --unattended
```

It is the same `warpzone-inbox` service as the [inbox task](/warpzone/setup/inbox/#adding-games-without-being-there), with its `/skyscraper` volume, work folder and memory and CPU limits; there is nothing new to configure. Try it by hand once from the compose folder, and use `--dry-run` of the attended command to see what it would find (the nightly run itself refuses `--dry-run`, `--force-lock` and `--retry-not-found`). Other NAS systems use their scheduler or root's crontab with the same line.

To stop a run that is going in the container, send the signal to the container itself: find it with `docker ps` and run `docker kill -s INT <container>`. `timeout` or Ctrl-C on a `docker compose run -T` client stops only the client; the scrape keeps running in the container and keeps the inbox lock. Stopped with the signal, `scrape` exits `130`, releases the lock, and the next run carries on from Skyscraper's cache.

Exit codes follow the inbox's rule: the run exits non-zero only for news, so DSM's email "when the script terminates abnormally" arrives once per problem, not every night.

- `0`: nothing to report. That covers nothing to do, the time budget spent, ScreenScraper's daily limit reached, a first busy night, and a problem already reported.
- `1`: a trouble that lasted its number of nights (ScreenScraper blacklisting the client on the first night; network trouble, ScreenScraper too busy, a system whose batches failed, the lock busy all night, or a run that died, on the second night in a row; maintenance on the third), or a new runtime error.
- `2`: a new configuration problem (no Skyscraper, `/skyscraper` not mounted, a scratch folder on memory, an `inbox/` or `catalog/` folder missing, a lock left by an older warpzone) or a usage error.

Add `--notify-url URL` to the task line to be told by a webhook (a JSON post, with no URL, path or key in it) when there is news; a night that only added art sends nothing.

To see how it is going, run `warpzone doctor` (in the container: `docker compose run --rm -T warpzone-inbox doctor`). The `Nightly scrape` line is optional until the first run, then says when it last ran, what it added and filled, and how much is left, with about how many nights that takes. It warns when the task has not checked in for 36 hours (is it enabled?), when a trouble has lasted as many nights as it takes to be news, and when the daily limit was reached 3 nights in a row: the library is larger than the account covers in a night, so it fills over more nights (a [ScreenScraper account](#screenscraper-account-optional) helps). What each run did is in `catalog/scrape/runs/` on the hub (the newest 50), and the state in `catalog/scrape/state.json`.

To force a full pass by hand, run `warpzone scrape` (in the container: `docker compose run --rm warpzone-inbox scrape`). An attended scrape ignores the memory of games that cannot improve and has no cap, which is also what to run after purging Skyscraper's cache. The contract is [Unattended scrape](/warpzone/development/design/scrape-unattended/).
