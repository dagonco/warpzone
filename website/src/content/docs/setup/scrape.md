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
- `warpzone scrape` scrapes the games already in the library that have no entry or lack media.

It only adds. An existing gamelist entry is never changed (consoles write play data into it, and you may have edited it), a game with an entry only gets the media files it lacks, and no media file is ever overwritten. A game marked `<nomultiscrape>true</nomultiscrape>` is left alone. The design is in [Scraping](/warpzone/development/design/scrape/).

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

## Language and region

Which language and region the descriptions and the cover art come from is a matter of taste, so it lives in Skyscraper's `config.ini` too, and warpzone does not set it. In the `[main]` section (or under a section named after one platform, such as `[snes]`):

```ini
[main]
langPrios="es,en"              ; the languages you read, in order of preference
regionPrios="eu,us,jp,wor"     ; the regions to take art and titles from, in order
regionFromFilename="first"     ; the region named in the file name goes first
```

These are Skyscraper's `langPrios`, `regionPrios` and `regionFromFilename` options; the values above are only an example. Put `regionFromFilename="first"` if you want the region in the file name to win: without it (the default is `inline`) a European release can win over the one the file names. In a test, "Kirby Super Star (USA)" got the European title "Kirby's Fun Pak". Do not set `nameTemplate`: it makes Skyscraper ignore the title options warpzone passes. Everything the hub layout depends on (the frontend, the folders, the shape of the title, which media are fetched) warpzone passes on the command line, which Skyscraper puts before every section of `config.ini`, so your file cannot break it.

## What you get

For a game without an entry, warpzone adds a `<game>` with the name, description, rating, release date, developer, publisher, genre and number of players ScreenScraper has (empty fields are left out), and the media it finds:

| Kind | What it is |
|---|---|
| `covers` | front of the box |
| `screenshots` | a screenshot (the title screen when there is none) |
| `miximages` | ES-DE's composite of screenshot, box and logo |
| `marquees` | the logo |
| `3dboxes` | the box in 3D with the logo on the spine |
| `backcovers` | back of the box |
| `videos` | the video preview |

Not produced: `titlescreens` and `physicalmedia` (disc and cartridge scans). Skyscraper's ScreenScraper module does not fetch them, so scraped games go without them and ES-DE and the library UI fall back to the other kinds. Manuals and fanart are not fetched either.

A game takes about 10 seconds and 2.6 MB of media, most of it the video, so a library of 1,000 games is about three hours and 2.6 GB on the hub (the videos stay on the hub; consoles ignore them) plus about as much in Skyscraper's cache on the machine that scrapes (in the container, `skyscraper/cache/` on the NAS's disk).

Scraped systems: `nes`, `snes`, `n64`, `gb`, `gbc`, `gba`, `nds`, `genesis`, `mastersystem`, `gamegear`, `sega32x`, `atari2600`, `atari7800`, `atarilynx`, `atarijaguar`, `wonderswan`, `wonderswancolor`, `psx`, `ps2`, `psp`, `saturn`, `dreamcast`, `gc`, `wii`, `n3ds`, `mame`, `ps3`, `xbox` and `switch`. Not yet: `ps4`, `psvita`, `xbox360` and `wiiu`.

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

Use `--dry-run` first on a big library: it lists the games with no entry and the ones missing media, with their totals, which is how to size a first run. It needs the [catalog index](/warpzone/reference/commands/#warpzone-catalog); a system without one is skipped with the hint to run `warpzone catalog`. It takes the inbox lock while it writes (so it never runs while `inbox --apply` does), and `--force-lock` takes over a stale one.

The run is resumable. Skyscraper keeps what it gathers in its own cache, so a run stopped by the daily limit, the time limit or Ctrl-C goes on where it stopped when you run it again (tomorrow, for the limit), asking for nothing twice. Games ScreenScraper does not know are remembered for 30 days in `$XDG_CACHE_HOME/warpzone/scrape-not-found.json` on the computer that scrapes (never on the hub); `--retry-not-found` tries them now, and a game whose file changes size is tried again by itself.

Exit code `0` when every selected game was handled (a game ScreenScraper does not know is not a failure), `1` when the run could not finish, `2` for a usage or configuration error, `130` on Ctrl-C. warpzone prints its own lines, never Skyscraper's output, which is kept in `<work_dir>/scrape/<system>.log` (mode 0600) for you to read when a batch fails.

To fix a wrong match, edit the entry on a console, or delete the entry and its media files and run `warpzone scrape` again. To get media ScreenScraper added later for a game that was already scraped, purge that game from Skyscraper's cache (`Skyscraper -p <platform> --cache purge:...`) and run `warpzone scrape`.

If ES-DE is open on a console, it keeps its gamelist in memory and may write it back without the new entries. When a gamelist was written in the last 10 minutes warpzone warns about it: quit ES-DE on every console and run `warpzone scrape` again if the entries do not stick (the media are already there, and the entries come back from the cache with no request).

## Filling art every night

A game that reaches the library without going through `inbox` (copied by hand, placed with `--no-scrape`, or left behind when a run hit ScreenScraper's daily limit) stays without art until someone runs `warpzone scrape`. On the hub you can leave that to a scheduled task that runs `warpzone scrape --unattended` once a night, with nobody watching.

What a night does:

- **It picks what is worth it.** New games with no entry go first, then games with an entry but no cover, screenshot or miximage, then games ScreenScraper did not know whose 30 days have passed, and last games that only lack a video, back cover, 3D box or logo. It takes at most `--max-games` of them (default 500) and stops before a batch that would end after `--max-minutes` (default 180). What is left is picked up the next night, so a big library fills over several nights.
- **It does nothing when there is nothing to do.** It exits at once, with no lock and no Skyscraper, so an up-to-date library costs nothing.
- **It skips games that cannot improve.** When ScreenScraper has no video or back cover for a game, the game would be "missing media" for ever. Every scrape remembers such games (per machine, next to the list of games ScreenScraper did not know) and the nightly run does not ask for them again; they come back when their file changes size or a media file they had goes missing.
- **It takes the inbox lock.** If an `inbox` tick or a scrape by hand holds it, the run waits up to 30 minutes (a lock silent for 10 minutes is taken over). The inbox's own ticks during the run exit as busy and try again at the next one, so a game dropped at 03:30 is filed when the run ends, at most `--max-minutes` later.
- **It runs at low priority** and uses the same Skyscraper, cache and account as the rest.

On a Synology, in Control Panel, Task Scheduler, Create, Scheduled Task, User-defined script: user `root`, daily at 03:00 (ScreenScraper's daily limit has reset by then), script:

```sh
cd /path/to/the/compose/folder && /usr/local/bin/docker compose run --rm -T warpzone-inbox scrape --unattended
```

It is the same `warpzone-inbox` service as the [inbox task](/warpzone/setup/inbox/#adding-games-without-being-there), with its `/skyscraper` volume, work folder and memory and CPU limits; there is nothing new to configure. Try it by hand once from the compose folder, and use `--dry-run` of the attended command to see what it would find (the nightly run itself refuses `--dry-run`, `--force-lock` and `--retry-not-found`). Other NAS systems use their scheduler or root's crontab with the same line.

Exit codes follow the inbox's rule: the run exits non-zero only for news, so DSM's email "when the script terminates abnormally" arrives once per problem, not every night.

- `0`: nothing to report. That covers nothing to do, the time budget spent, ScreenScraper's daily limit reached, a first busy night, and a problem already reported.
- `1`: a trouble that lasted its number of nights (ScreenScraper blacklisting the client on the first night; network trouble, ScreenScraper too busy, a system whose batches failed, the lock busy all night, or a run that died, on the second night in a row; maintenance on the third), or a new runtime error.
- `2`: a new configuration problem (no Skyscraper, `/skyscraper` not mounted, a scratch folder on memory, an `inbox/` or `catalog/` folder missing, a lock left by an older warpzone) or a usage error.

Add `--notify-url URL` to the task line to be told by a webhook (a JSON post, with no URL, path or key in it) when there is news; a night that only added art sends nothing.

To see how it is going, run `warpzone doctor` (in the container: `docker compose run --rm -T warpzone-inbox doctor`). The `Nightly scrape` line is optional until the first run, then says when it last ran, what it added and filled, and how much is left, with about how many nights that takes. It warns when the task has not checked in for 36 hours (is it enabled?), when a trouble has lasted as many nights as it takes to be news, and when the daily limit was reached 3 nights in a row: the library is larger than the account covers in a night, so it fills over more nights (a [ScreenScraper account](#screenscraper-account-optional) helps). What each run did is in `catalog/scrape/runs/` on the hub (the newest 50), and the state in `catalog/scrape/state.json`.

To force a full pass by hand, run `warpzone scrape` (in the container: `docker compose run --rm warpzone-inbox scrape`). An attended scrape ignores the memory of games that cannot improve and has no cap, which is also what to run after purging Skyscraper's cache. The contract is [Unattended scrape](/warpzone/development/design/scrape-unattended/).
