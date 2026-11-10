---
title: "Code, testing and CI"
---

## Syncthing API surface used

All calls send `X-API-Key`. Base URL from config (hub) or the local
`config.xml` `<gui><address>` (device; HTTPS if `tls="true"`, self-signed allowed
only for loopback addresses).

- `GET /rest/system/status` → `myID`
- `GET /rest/config/devices`, `POST /rest/config/devices`
- `GET /rest/config/folders`, `POST /rest/config/folders`,
  `PATCH /rest/config/folders/{id}`
- `GET /rest/db/ignores?folder=`, `POST /rest/db/ignores?folder=` (`{"ignore": [...]}`)
- `GET /rest/db/status?folder=`, `GET /rest/db/file?folder=&file=`,
  `GET /rest/db/browse?folder=&levels=0`
- `POST /rest/db/scan?folder=`
- `GET /rest/system/connections`, `GET /rest/db/completion?folder=&device=` (the library UI's
  progress, read on the hub; see [Progress](/warpzone/development/design/library-ui/#progress))

`POST /rest/db/scan` answers only when the scan is done, and a folder that is pulling runs a
scan only after the pull, which on a slow device can take longer than the client's timeout
(30 s). A full scan that gets no answer in time is not an error: Syncthing keeps it queued and
runs it when the pull ends, so `provision` reports "rescan queued" and goes on, and `prune`
stops without deleting anything (its index would not be refreshed yet). A scan of a single
path (`&sub=`), whose result the caller reads right after, still fails on a timeout.

Only `prune` also reads `GET /rest/db/ignores`'s `error` field and
`GET /rest/db/status`'s `state`/`stateChanged` (see [Pruning](/warpzone/development/design/prune/)).
`settings capture` reads `GET /rest/db/status`'s `state` and `needTotalItems` and compares
the `local` and `global` records of `GET /rest/db/file` (see [Capture](/warpzone/development/design/emulator-settings/#capture)).
`settings apply` and `audit` never call Syncthing.

Target: Syncthing v2.x (tested in CI against a pinned release, v2.1.7 at the
time of writing). Verified behaviour that the design relies on: `POST
/rest/db/ignores` works on a paused folder and writes its `.stignore`; a missing
`#include` target makes the call return 200 with an `error` field instead of
failing; `GET /rest/db/file` answers 404 with a plain-text body for objects not
in the index; Syncthing creates a folder's root directory on its own.

## Code layout

```
warpzone/
  __init__.py        version
  __main__.py        python3 -m warpzone
  cli.py             argparse, exit codes, output
  errors.py          `WarpzoneError` (every error carries its `exit_code`) and the `EXIT_*` codes
  paths.py           `expand_home`: `~` against a given home (pure)
  report.py          `Reporter`: the per-folder notes, warnings and deferred flag a command hands back
  runners.py         the subprocess edge: flatpak, systemctl, the import check, the cut-off script check
  config.py          private config loading + validation (tomllib), dataclasses
  profiles.py        profile loading + validation
  model.py           folder specs: expands config + profiles into the desired state
  ignores.py         ignore rendering (common, per-device, bootstrap)
  syncthing.py       minimal REST client (urllib), redaction, dry-run recorder
  probes.py          `DeviceProbes`: the process table, subprocess checks, commands and clock a device
                     command reads, in one object a test can fake
  provision.py       the name the commands import: re-exports what `cli` and `health` call
  provision_folders.py  Syncthing folder steps both provisioning commands share (diff, patch, ignores, wait)
  provision_hub.py   `provision hub` (and `--additive`) and `check`: devices, folders, the whitelist bootstrap
  provision_device.py  `provision device`: the subscribed folders, then the steps below, in order
  provision_emulators.py  device edits for ES-DE's system list, RetroArch, shadPS4, Cemu and title links
  provision_artwork.py  frontend artwork on a device: the scope of the artwork folders, the gamelists
  provision_scripts.py  the ES-DE startup scripts, turning ES-DE's event scripts on, the Options menu entries
  provision_timers.py  the artwork, play state and unattended prune systemd user timers
  provision_updater.py  the autonomous update: settings file, live updater script, its timer
  prune.py           leftover detection (pure `classify`), the unattended guard (pure `guard`) and the state file of its refusal, delete, webhook
  bios.py            BIOS manifest, local overrides, pure `judge_*`, disk checks
  whitelist.py       managed whitelists: escape, parse, render, normalize, changes, matcher,
                     effect, migration parsing and classification, unsupported systems (pure)
  whitelist_history.py  the backups of a whitelist: names, comparison, diff, what a restore refuses (pure)
  whitelist_store.py whitelist files on the hub: read with a stamp, backups, guarded write,
                     hub scan, `whitelist migrate` and its proof walk, `whitelist restore`
  dat.py             Redump / No-Intro DAT parsing, cache, matching, disc grouping (pure)
  inbox_core.py      what the inbox pipeline and its handlers share: errors, options, results, staging
  inbox_lock.py      the `inbox/.warpzone.lock` file that keeps two `--apply` runs apart
  inbox.py           unpack, identify, convert, place, file the originals
  sfo.py             the key/value table of a PARAM.SFO (pure; PS3 discs and PS4 packages)
  inbox_switch.py    Switch games: identify with nsz, titledb names, decompress, place
  inbox_mame.py      arcade sets: MAME / FBNeo / 2003-Plus fit, emulator choice, gamelist backup and write
  inbox_console_iso.py  PS3, Xbox, Xbox 360 and Wii U images: identify by signature, place
  inbox_ps4.py       PS4 packages: extract with orbis-pkg-util, place a game, update or DLC
  inbox_vita.py      PS Vita game dumps: identify by param.sfo, place the folder and the .psvita file
  inbox_vita_pkg.py  PS Vita .pkg: header and zRIF checks, install with Vita3K in a work folder, judge the result
  shadps4.py         shadPS4's install directories and launch script edits (pure)
  cemu.py            Cemu's MLC path and game paths in its settings.xml (pure)
  titlelinks.py      symlinks from the library's game folders into an emulator's own (pure plan)
  inbox_n3ds.py      3DS images (`.3ds`, `.cci`): NCSD / NCCH header check, placed as `.cci`
  inbox_dolphin.py   Wii and GameCube `.rvz` / `.wia`: header check (disc type, magic, game ID), placed in `wii/` or `gc/`
  flatpak.py         the SyncThingy flatpak: app id and the unit's commands
  selfupdate.py      the updater's side in Python: paths, settings file, live copy rule, `self-update` wrapper
  versions.py        order of package versions and release tags (`version_key`, `tag_key`; pure)
  updatetimer.py     the systemd user units that start `updater.sh tick` (pure)
  settings.py        `settings apply`, `capture`, `audit`: loading, prompts, output
  settings_formats.py  format handlers ini, qt-ini, retroarch-cfg, yaml-flat (pure)
  settings_tree.py   format handlers json, toml (pure)
  settings_layers.py layer files: parse, validate, merge, render (pure) and atomic write
  settings_plan.py   apply plan, capture candidates and pre-selection, audit rows (pure)
  settings_state.py  device state: snapshots, backups, log, lock
  safefile.py        safe writes: atomic write, stamps, `guarded_edit`, JSON cache
  procs.py           process detection from `/proc` (`running`)
  esde.py            ES-DE startup script, `es_settings.xml` check and edit (pure)
  options_menu.py    the bash scripts of the ArkOS Options menu entries (pure)
  frontend.py        frontend system list rewrite, RetroArch key gap (pure)
  gamelist.py        ES-DE gamelist text edits: the per-game emulator and the helpers `playstate` shares (pure)
  artwork.py         frontend artwork: ES-DE gamelist -> the frontend's gamelist, scope ignores,
                     system names, timer units (pure; the reads are passed in)
  playstate.py       play state: ledgers, merge rules, times, its tags in a gamelist (pure)
  playstate_device.py  play state on a device: files, ES-DE gamelists, generated gamelists
  diskstatus.py      the disk a console reports to the hub: when to write, format, staleness (pure)
  diskstatus_io.py   the disk report: written on a console beside the ledgers, read on the hub
  saveconvert.py     save conversion: the `.dsv` footer, size rules, the three-way decision (pure)
  saveconvert_device.py  save conversion on a device: files, state, backups, the running emulator
  tomlw.py           TOML scalar and key writer (tomllib only reads)
  doctor.py          machine checklist (`warpzone doctor`): checks, install hints, renderer
  health.py          `warpzone health`: the dry runs and file checks that say a version works (decisions pure)
  ra.py              RetroAchievements: RAHasher wrapper, game list lookup and parsing (ids, points)
  catalog.py         catalog index: derivation of games and parts, system roots, title keys,
                     search, RA badge state, JSON encoding and merge (pure)
  catalog_store.py   `warpzone catalog`: the walk, hashing, RAHasher and RA list runs, guarded writes
  ra_data.py         RA achievements, the user's progress and their JSON cache files; the merge and totals (pure)
  ra_store.py        `catalog --ra-achievements`: achievements, progress and badge downloads, cache reads
  library_api.py     the library UI's API as pure functions: validation, `plan_changes`, responses
  queue.py           the play queue as pure decisions: schema, inferred states, views, release rule,
                     `plan` for `POST /api/v1/queue`, the answers (see play-queue.md)
  queue_store.py     the queue file on the hub: read with a stamp, backups, guarded write
  recommend.py       the home page's recommendation shelves: unfinished games, games like a recent one (pure)
  progress.py        per-device progress states from the hub's completion (pure)
  saves_index.py     saves per game for the library API: exact matching of the hub's `saves/` to games
                     (pure), the listing and its short cache
  serve.py           `warpzone serve`: http.server, sessions, the write lock, caches, background runs
  localst.py         find and parse the local Syncthing config.xml
  data/ui/index.html the library UI page (static, no build step)
  data/ui/kit.js     its DOM builder, pixel icons, system names, codes and colours, title parsing
  data/ui/api.js     its API client: requests, error messages, title key port
  data/ui/store.js   its state: catalog, coverage per console, progress poll, changes
  data/ui/components.js  its shared pieces: art, tiles, console slots, the system lever
  data/ui/queue.js   its play queue: the Up next section, the game's queue panel and their dialogs
  data/ui/views.js   its pages: home, system, search, the game page, consoles
  data/ui/app.js     its frame: router, search, confirmation dialog, keyboard and gamepad, login
  data/ui/style.css
  data/ui/placeholder.svg  the cover of a game with no scrape
  data/ui/favicon.svg
  data/updater.sh    the updater (POSIX sh): choose a release, verify it, roll it back
  data/bios.toml     BIOS manifest (names, sizes, hashes, signatures only)
  data/allowed_signers  the keys that sign releases, in the format of `ssh-keygen -Y` (see "Autonomous updates")
  data/profiles/steamos-emudeck.toml
  data/profiles/anbernic-rg351mp.toml
templates/
  patterns-common.stignore
  patterns-device.example.stignore
examples/config.example.toml
examples/bios.example.toml
examples/compose.example.yaml  the hub's container, as Compose runs it
docker/Dockerfile    the hub's container image: RAHasher built in one stage, warpzone from the checkout
.dockerignore        keeps everything but the package out of the image's build context
tests/               unittest; unit tests + e2e (two real Syncthing instances)
website/             documentation site (Starlight); the design pages are
                     website/src/content/docs/development/design/
```

`model.py` is pure: config + profiles in, list of desired folder specs out. All
I/O sits in `syncthing.py`, `localst.py` and the `provision_*` modules, so the diffing
logic is unit-tested without a network.

The [library UI](/warpzone/development/design/library-ui/) has no file left to add: its
modules and its `data/ui/` files are all in the block above, and so are the hub's container image
and its compose example. `tests/test_container_files.py` reads them: the base images pinned by
digest, RALibretro by commit, a health check, `pip` touching only the local wheel, no build tools
in the last stage, and no secret-looking value in the compose example. The image is not built in
CI, which has no Docker step.

## Testing

- Unit tests (stdlib `unittest`): config validation errors, profile parsing,
  model expansion, ignore rendering, diffing (desired vs actual → calls),
  redaction, `config.xml` parsing, dry-run output.
- A fake Syncthing (`http.server` in a thread) for provision flows, including
  the bootstrap wait loop and its timeout.
- The device commands (`provision device`, `frontend refresh`, `prune`) reach the machine only
  through a `DeviceProbes` (`probes.py`): the `/proc` to read, the import check, the
  `flatpak` and `systemctl` runners, the script check and the clock. A test passes one
  object with the pieces it fakes (`tests/support.py`: `device_probes`) instead of patching.
- End-to-end test, skipped unless `WARPZONE_E2E_SYNCTHING` points at a
  Syncthing binary: start a hub and a device instance in temp dirs, run
  `provision hub` and `provision device`, then assert that a whitelisted file
  reaches the device, a non-whitelisted one does not, and a save created on the
  device reaches the hub. It then removes a system from the whitelist and checks
  that `prune` reports the leftovers, that `--max-gib 0` blocks the delete, and
  that `--apply` removes only the leftovers (a device-only file and every
  whitelisted file stay). The same run covers the BIOS side: a plain BIOS file
  arrives through the library, `bios/cemu/keys.txt` reaches the Cemu folder only
  through its placement (a second file in the same hub folder does not, and the
  library does not create `bios/cemu`), and a symlink at `bios/ryujinx/keys` is left
  untouched.
- Profile and model unit tests cover the placement and library-exclusion tables
  (validation, overlaps, rendering order on the device and the hub); the provision
  tests cover skipped, created (`create_when`) and dry-run targets.
- Play state tests (`test_playstate.py`, `test_playstate_device.py`): the merge rules on a
  simulated fleet with explicit delivery of ledgers (a shared gamelist, a console, removal,
  offline devices, wrong clocks, random delivery orders), ledger files, byte-preserving edits,
  ES-DE open, closed and starting, the generated gamelists, and the unchanged shared folders.
- Artwork tests (`test_artwork.py`, `test_artwork_device.py`): profile validation, the
  device and hub folder specs (the hub's stay identical whatever the device order), the pure
  converter (escaping, paths that escape, user data, determinism, system names), `ensure_gamelists`
  in a temporary home (idempotence, frontend running, no `/proc`, dry-run, a changed file), the
  timer units and their systemctl calls, scope sync against the fake Syncthing, `frontend
  refresh` and the doctor lines.
- Save conversion tests (`test_saveconvert.py`, `test_saveconvert_device.py`): the footer
  (parse, strip, build, malformed input), every branch of the decision, the files in a temporary
  home (both directions, kept footer, conflicts and backups, invalid and temporary files, the
  emulator running or starting, `--dry-run`, the state), the profile table, the doctor line and the
  command; the system list wrapper is covered by `test_frontend.py` and `test_anbernic.py`.
- `anbernic-rg351mp` tests: the shipped profile and its parsing, `ignorePerms` in the folder
  specs, the pure rewrite and key gap, `provision device` in a temporary home (backups,
  second run, frontend or RetroArch running, no `/proc`, dry-run) and the doctor lines.
- `prune` unit tests with the fake Syncthing: the pure decision (`classify`),
  every refusal (bootstrap ignores, wrong type, missing marker, size cap),
  protected names, nested folders, symlinks, re-check before delete, webhook.
- `bios` tests: the pure judgements, manifest and override validation, and tmpdir
  libraries with tiny fake files and a test-only manifest (no real BIOS hashes):
  hashing only files of the expected size, content matches, local acceptance,
  skipped folders, zip members, exit codes.

- `inbox` tests: DAT parsing and the cache, matching (partial games, SHA1, ambiguous
  names, duplicate files), disc grouping, destination decisions and `_done` purge
  selection as pure functions; archive extraction against traversal, absolute paths and
  links; whole runs on a temporary hub with tiny DATs and stand-in `chdman`,
  `RAHasher`, `bsdtar` and `dolphin-tool` scripts, including one cartridge flow with no
  external tool; the CLI, with the API key coming from the environment.
- Switch tests (`test_inbox_switch.py`): kind, game-of-an-update, normalization, DLC name
  and bundle numbering, file names and length limits, the `nsz --info` parser and the PFS0
  header as pure functions; titledb region order and cache; whole runs on a temporary hub
  with a stand-in `nsz` (placement of games, updates, DLC and bundle parts, `.nsz`
  decompression and every way it can fail, name fallback, conflicts, review reasons, the
  keys content never shown).
- Arcade tests (`test_inbox_mame.py`): set database parsing and its cache, fit,
  set identification and the emulator decision as pure functions over tiny synthetic
  XML databases (BIOS alternatives, merged and split clones, devices, optional and nodump
  ROMs); the gamelist update as a pure text function (add, change, remove, comments,
  other entries and unknown elements untouched, CRLF); whole runs on a temporary hub
  (placement, rename, the emulator order, Flycast for Atomiswave and Naomi, gamelist and
  its backup, conflict,
  already in the library, review reasons, folders of zips, configuration errors).
- Console image tests (`test_inbox_console_iso.py`): claiming rules, PARAM.SFO, ISO9660 and
  XDVDFS parsing as pure functions of bytes; signature verdicts on tiny synthetic images
  built in the tests (a minimal ISO9660 with the PS3 files, XDVDFS volumes at offset 0 and
  at the XGD2, XGD3 and Redump offsets, the large offsets as sparse files); whole runs on a
  temporary hub (placement, the plain `.iso` hook with a PS2 and a PSP image that a DAT
  matches, already present, conflicts, the full Xbox image, wrong content under a claimed
  name, the gamelist `<altemulator>`, zip and 7z input).
- Settings tests: for every handler, the contract on synthetic fixtures shaped like the
  real files (BOM, CRLF, no final newline, duplicated keys, `\default` companions,
  `@Variant(...)` values, an unquoted RetroArch value, xemu's multi-line array of inline
  tables, Vita3K's sequences and document markers, nested JSON): `write(text, {}) == text`,
  only changed lines differ, `NotWritable`, typed round trips checked with `json` and
  `tomllib`. Pure tests for the pattern matcher, layer validation and merge, `plan_apply`
  (every branch of the `C`/`L`/`S` table, first run, new keys), `candidates`, pre-selection,
  discard and audit rows. Process detection over a fake `/proc` tree, including a process
  whose arguments name the emulator and warpzone's own process. Whole `apply`, `capture`
  and `audit` runs on a temporary home: backups and their rotation, atomic writes,
  idempotence (a second run writes nothing), held keys, a running emulator, a file changed
  between read and write, the lock, invalid layers changing nothing, scripted prompts,
  `--yes` and the pattern flags, a report without a terminal, conflict copies and the
  Syncthing in-sync check with the fake Syncthing. Fake login values (marked for gitleaks)
  must never appear in output, layers, snapshots or the log. The startup script rendering
  and the `es_settings.xml` check and edit as pure functions (value changed, line added,
  every other byte kept, CRLF); `provision device` writing the script and enabling the
  setting with a backup, leaving it alone while a fake `/proc` shows ES-DE running, and
  their dry run. The e2e test subscribes the device to `settings` and checks that a layer file
  travels both ways.
- A second e2e test starts from an empty hub: its folders exist and hold nothing. `provision
  hub` runs while the whitelists and a game are copied in, so the bootstrap waits for them;
  the test then checks the created `wz-*` folders, the final ignores, `check`, that a second
  `provision hub` reports every device and folder unchanged and leaves Syncthing's devices,
  folders and ignores as they were (the fake Syncthing keeps what was sent, a real one fills
  in its defaults), and that `provision device` makes a device receive the whitelisted game
  and not the other one.
- The updater end to end (`test_e2e_updater.py`, same two instances): `updater.sh install`
  sets a device up from a clone of a throwaway repository whose releases are this checkout's
  `warpzone` package, and `updater.sh tick` then runs the real three-step verification, which
  is what the differential verdict reads. A release whose REST client sends the API key under
  the wrong header (refused by a real Syncthing, not by a fake one) is marked bad because the
  version known to work passes the same check; with the device's Syncthing stopped, a good
  release and the version known to work both fail, so nothing is marked bad and the device
  backs off, and it installs that release once Syncthing is back and the day has passed. A
  release whose device whitelist starts with `*` passes its verification and hides the
  library; once it is retired the device goes down to the older release, whose `provision`
  puts its own ignores back on the device's Syncthing, and a new game arrives. The releases
  are built from the working tree, so the test needs neither the history nor the tags of the
  real repository.
- Updater script tests (`test_updater_sh.py`): `updater.sh` runs against real git
  repositories in temporary folders (an upstream with tagged releases, a clone, a fake
  `python` that behaves like warpzone according to the checked-out version, a fake `/proc`
  and a fake clock): a good update, the numeric order of tags, tags that are not ancestors of
  `origin/main` or whose version disagrees, a broken import, a wrong `--version`, a failing
  `provision`, a failing `health` and a hang (all bad when `good` passes and the release fails
  again), a failure that happens once (not bad), a step cut off with a grandchild alive (the
  grandchild dies and the pipe is free), the tick budget and the second look at a release
  that ran out of it, the budget on the uptime and not on the date, a user's edit of a file
  the release changes (during a deferral and during probation), a python that imports another
  warpzone, a checkout cut in the middle by a filter that kills the process group or fails
  (the tree is repaired), too little free space, an ignored file in the way, a menu that
  closes and opens again, a lock from another boot, times from the future, a moved tag, a
  broken environment (no tag burned, back-off of 1, 2, 4, 7 and 7 days), a bad tag that never returns,
  a power cut (a fake that kills its own process group; no tag burned, a stale lock taken
  over, the second attempt, the give-up with `retry_after`), a menu that opens during the
  check, the 72 hour limit, probation at the next boot, the kinds of checkout (`dev`, `dirty`,
  a loose commit, an adopted branch that comes back to its branch), the stale `index.lock`, the
  fetch interval of each channel and `--fetch-now`, state and settings that contain shell syntax, `install` (the best valid release
  of the channel that carries the script, the venv without pip with its `.pth` and shim,
  `provision device` and its arguments, every refusal before any change) and the venv repair (a
  venv that is gone, whose Python is gone, that cannot find the clone or whose `.pth` points
  elsewhere; one that works is left alone; `--clear` never reaches a folder not called `venv`, one
  that is not a venv, the clone's folder or the folder around the venv), and `selftest`. The syntax check,
  `selftest` and a full update run with every shell found among `sh`, `dash` and `bash`
  (`WZ_TEST_SHELL` picks the shell for all the others), and shellcheck runs when installed. CI
  runs this file in its own `updater` job (Ubuntu with dash as `sh`, and macOS), apart from the
  `test` job, which skips it with `WARPZONE_SKIP_UPDATER_SH=1`.
- Self-update tests (`test_selfupdate.py`, `test_update_timer.py`): tag and version
  comparison as pure functions (including the order `rc1` < `rc2` < release); the settings
  file (`render_conf`, which refuses a line break, and `parse_kv`), `updater_abi`,
  `install_decision` and the wrapper with a fake runner; the units' rendering and
  `ensure_update_timer` with a fake `systemctl` (first run, second run, an old unit rewritten,
  switched off, dry-run, no user manager, no live copy, failed enable); `ensure_updater` in
  temporary folders (first install, atomic renewal with `.prev`, the same script, a lower ABI,
  a syntax error, a failing, wrong or hanging `selftest`, no ABI, dry-run);
  `ensure_self_update` and `provision device` for both shipped profiles, with
  `self_update = false` and with a clone that is not a git checkout.
- `doctor` tests of the updater: the judges as pure functions (state, settings, script) and the
  check over a temporary home (healthy, nothing installed, another channel, a Python that is
  gone, stuck, recent bad tags, silent when the config turns it off).
- `doctor` tests: the renderer and exit code as pure functions; every check with fake
  probes (tool present, missing per platform, unrunnable, unrecognizable version; config
  missing, partial or invalid; Syncthing absent, answering, refusing, not on loopback;
  DATs old or absent; hub unmounted, incomplete or short of space); and that no key
  (RetroAchievements, Syncthing) appears anywhere in the output.

## CI (GitHub Actions)

`ci.yml` runs on every pull request, every Monday and by hand (`workflow_dispatch`), not on
pushes to `main`: `main` only takes a pull request that is up to date with it, squash-merged,
so its tree is the one the pull request already tested. The weekly run catches changes from
outside the code, such as new runner images or Python releases. `docs.yml` still runs on
`main`, because it publishes the website.

On a pull request a first job, `changes`, looks at the files it changes. `test`, `typecheck`
and `e2e` run only when something the tests read changes (code, tests, `pyproject.toml`,
`examples/`, `CHANGELOG.md`, the design pages); `updater` only when the package, its tests or
`pyproject.toml` change. `lint` and `secrets` always run, and a change to `ci.yml` runs
everything. A skipped job counts as passed for the required checks; the matrix jobs skip
their steps rather than the whole job, so their checks keep their names.

- `lint`: ruff check + ruff format --check.
- `typecheck`: mypy on the `warpzone` package.
- `test`: unittest on Linux with Python 3.11 and 3.14 and on macOS with Python 3.14, without
  the updater script's tests (`WARPZONE_SKIP_UPDATER_SH=1`).
- `updater`: `tests/test_updater_sh.py` on Ubuntu with dash as `sh` and on macOS.
- `e2e`: ubuntu-latest, downloads a pinned Syncthing release, verifies its
  SHA-256 against a value committed in the workflow, runs the e2e test.
- `secrets`: gitleaks over the full history.
- Every third-party action pinned by commit SHA, `permissions: contents: read`
  at workflow level, Dependabot for GitHub Actions.
