---
title: "Frontend artwork"
---

A frontend that is not ES-DE shows nothing of what the hub scraped: ES-DE keeps its
gamelists and media in its own layout and the other frontends do not read it. A profile with
`[artwork]` closes that gap with data only; nothing in the code tests a profile or a device
name. The table:

```toml
[artwork]
format = "emulationstation"        # the converter, a key of artwork.FORMATS
kinds = ["covers"]                 # hub media kinds to receive, best first
systems = "library"                # which systems: those with a folder in the library's roms/
media = "/roms/warpzone/media"     # where wz-media lands on the device
metadata = "/roms/warpzone/es-de"  # where wz-esde lands on the device
gamelists = "~/.emulationstation/gamelists"  # where the generated gamelists go
refresh = "15min"                  # optional: a systemd --user timer interval
```

`format` must be in `profiles.ARTWORK_FORMATS` (a test keeps it in step with
`artwork.FORMATS`), `kinds` a non-empty list without repeats of `covers`, `screenshots`,
`miximages`, `marquees`, `3dboxes`, `physicalmedia`. The table needs `paths.library` and, for
`emulationstation`, `[es_systems]` (the system list names the systems and its `process` tells
whether the frontend runs). It replaces `paths.media` and `paths.esde`, so setting both is an
error, and neither folder may be the library, inside it or inside each other (Syncthing
folders do not nest).

**Delivery.** A device that lists `frontend` gets `wz-media` and `wz-esde` at `media` and
`metadata`, both receive-only and with `ignorePerms` like the rest of the device. The hub
folders are the ones the Steam devices already share and stay exactly as they are (type,
versioning, ignores): `hub_specs` builds them from the static definitions, never from the
profile, so the order of the devices in the config cannot change them; a test builds the hub
specs with and without the console, in both orders. Only the device side differs, in its ignore
lines: `!/<system>/<kind>` per system and kind for the media folder (the hub's layout is
`<system>/<kind>/...`), `!/gamelists/<system>/gamelist.xml` per system for the ES-DE folder,
then `*`. The systems are the plain-named directories below `<library>/roms` at the time
`device_specs` is asked (`artwork.library_systems`); a name that would be a pattern
(`*`, `?`, `[`, spaces) is left out. So the scope follows the whitelist without being edited
per system, but it is computed, not read from Syncthing: `provision device` sets it, and so does
every `frontend refresh` (`provision_artwork.sync_artwork_scope`, which patches only ignores that differ
and only for folders Syncthing already has). With no system folder yet the ignores let nothing
in and `provision` says so. Covers of every game of an included system arrive, also those whose
game is not on the device (a single ROM of a big system whitelists the whole system's covers);
the ROM check below keeps them out of the gamelist, not off the card.

**Converter.** `artwork.Format` is a frontend's gamelist shape: the fields it keeps from ES-DE
(`source_fields`; ES-DE-only tags such as `altemulator` are not copied), the fields the
frontend writes itself (`user_fields`), a map from hub media kind to the tag that holds the
path, and a `render` function. A new frontend with another file shape is one more `Format` and
one more name in `ARTWORK_FORMATS`; planning, writing, the timer and the doctor do not change.
`artwork.build` is pure and takes the existence checks as functions:

- ES-DE's gamelist is read from the synced `metadata` folder. ES-DE writes an
  `<alternativeEmulator>` element before `<gameList>` (two top-level elements), so a file that
  does not parse as one document is read from its `<gameList>` block (found with plain
  searches: the first `<gameList` to the last `</gameList>`, so the cost is linear). Another
  machine writes the file, so a bad one is an `ArtworkError` and never a traceback: that
  includes an XML declaration that names an encoding the parser cannot use (`bogus`,
  `shift_jis`, `utf-16-le`), and a file over `MAX_GAMELIST_BYTES` (32 MiB), which is refused
  before parsing, and before reading wherever the reader has the path (`read_gamelist`: `playstate_device` and the library UI server). `frontend
  refresh` reports such a scraped gamelist as a problem of its system and writes the others.
  Entries with a path that is absolute or climbs out of the system folder (`..`) are dropped.
- An entry is kept only when its ROM exists on the device
  (`<library>/roms/<hub system>/<path>`).
- The cover is `<media>/<hub system>/<kind>/<path without extension>.png`, subfolders kept; the
  first kind of `kinds` whose file exists becomes the `<image>`, as an absolute path. Without a
  cover there is no `<image>`.
- Text is XML-escaped and characters XML 1.0 cannot hold are dropped. Entries keep the hub's
  order; the file layout is fixed, so the same input gives the same bytes.

**System names.** The gamelist goes under the frontend's *system name*, and the name is not
always the ROM folder: dArkOS's `ppsspp` reads `psp`, `mame2010` reads `mame`, `jaguar` reads
`atarijaguar`. So `artwork.frontend_systems` reads the system list (the OS's, after the same
rewrite as the override, which applies `[es_systems.rename]`: frontend folder -> hub folder,
so hub `pokemini` is the system `pokemonmini`) and takes `<name>` and the folder of each
entry; the name becomes a folder below the gamelists folder, so an entry whose name is not a
plain name (`artwork.is_plain`: letters, digits, `.`, `_` and `-`, not starting with a dot) is
skipped. A system whose folder is below the hub's system folder (`pico-8` reads `pico8/carts/`)
gets paths relative to it (the hub has no system list, so the library UI matches the same
shift by trying shorter paths, see [Metadata and media](/warpzone/development/design/library-ui/#metadata-and-media)). When several hub folders carry one name (`snes` has two entries),
the one the hub names alike wins and the rest are reported. One hub folder may feed several
names.

**User data.** The frontend rewrites its gamelist (`SaveGamelistsMode` is `on exit` on dArkOS)
with what the player does: `favorite`, `hidden`, `kidgame`, `playcount`, `lastplayed`. A
regeneration reads the existing file first and carries those fields over for the same path;
they are never taken from the hub's gamelists (a device that lists `play-state` gets the other
devices' statistics from their ledgers instead, see [Play state](/warpzone/development/design/play-state/)).
An entry the hub has no scrape for, but with user data on a ROM that is still there, is kept
with just that data. Descriptive fields and images always come from the hub, so editing a
description in the frontend's metadata editor is overwritten; edit the scrape on the hub.
No backup is made: the hub copy is the source and user data is kept. A gamelist whose
content already matches (`artwork.same_content`: same entries and known fields, whatever the
layout or the games listed without data) is not rewritten, so the frontend's own formatting
survives and a second run changes nothing.

**When it runs.** `provision_artwork.ensure_gamelists` is the writer, like `ensure_systems_override`: only
while no process in `[es_systems].process` runs (a missing `/proc` or a running frontend warns
and writes nothing, except when nothing would change), atomically, and refused when the file
changed after it was read. `provision device` calls it for a device that lists `frontend`;
`warpzone frontend refresh [device]` calls scope sync then the writer, for new scrapes and for
games that arrived later. To apply new scrapes without a manual step, a profile may set
`refresh`: `provision device` then writes `~/.config/systemd/user/warpzone-artwork.service`
(`Type=oneshot`, `ExecStart=<python> -m warpzone frontend refresh <device>`, naming `--config`
only when it is not the default) and `warpzone-artwork.timer` (`OnBootSec=5min`,
`OnUnitActiveSec=<refresh>`), then runs `systemctl --user daemon-reload`, `enable --now` and
`restart`. Units are rewritten only when their text differs; a second run makes no systemctl
call. The timer is the choice over a path unit because Syncthing writes many files per scrape
(a path unit fires per change) and because the same run also widens the scope. A run that
finds the frontend open changes nothing and exits 0, so the timer does not show as failed.
Like the ES-DE startup script it needs a Python that imports `warpzone` from outside the
current folder. The user manager must run without a login session (`loginctl enable-linger`;
dArkOS has it on).

The text of the four background timers (this one, the play state, prune and ES-DE settings
timers) is rendered by `timers.py` (pure), which also holds the unit names. Each service is
`Type=oneshot` with `Nice=10` and `IOSchedulingClass=idle`, so a refresh, merge, prune or
settings run does not compete with a game for the CPU or, on an I/O scheduler that honours the
class, the disk. Every word of `ExecStart=` goes through one quoting function, `exec_word`:
a word with anything but letters, digits and `_@+=:,./-` is double-quoted, with `\` and `"`
escaped, and with `%` and `$` doubled because systemd expands `%h` and `$NAME` in `ExecStart=`
(so a config path with a `$` reaches the command intact). `exec_command` reads a unit back the
same way, and the updater's unit (`updatetimer.py`) uses the same function. A console that
already has older units gets the new text from the next `provision device`, which rewrites a
unit whose text differs.

The doctor reports missing and stale gamelists and the timer (see [Doctor](/warpzone/development/design/doctor/)).
