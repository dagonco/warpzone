---
title: "warpzone design"
---

warpzone keeps one game library on a home server (the **hub**, typically a NAS)
and delivers it to every console you own (the **devices**: Steam Deck, Steam
Machine, a spare Mac...). Saves follow you from one device to the next. It is a
thin layer of conventions and automation on top of [Syncthing](https://syncthing.net):
Syncthing does all the transfer between machines; warpzone configures it, files
new games into the hub's library and removes from a device what its whitelist no longer allows.

These pages are the contract for the implementation: a behaviour change updates the page
that describes it in the same pull request. Anything not described here is out of scope.
The pages after this one cover one subject each.

## Goals

- One source of truth for ROMs, BIOS and extras: the hub. Devices only receive.
- Each device chooses what it gets with a whitelist that is **edited only on the
  hub**, in the library UI. No file is ever edited by hand on a device after provisioning.
- Saves sync in both directions between all devices, with history kept on the hub.
- Emulator settings are changed in each emulator's own interface, on any device, and
  captured to the hub as key-level layers (common and per device) that every device
  applies. Config files are never copied whole and never edited by hand.
- Adding a device is one command.
- No dependencies beyond the Python standard library (3.11+), so the tool runs
  unchanged on SteamOS, macOS and any Linux box.

## Non-goals

- Moving, renaming or converting ROMs that are already in the library. New content goes through `warpzone inbox`, which only ever adds.
- Shipping or locating any copyrighted content (ROMs, BIOS, keys, firmware).
  The BIOS checker compares what the user has with public names, sizes and hashes.
- Running on the hub's own Python. Many NAS ship an old one, so on the hub warpzone runs in a
  container with its own Python (the library UI, `catalog`, the whitelist migration; see
  [The container](/warpzone/development/design/library-ui/#the-container)), and every other
  command drives the hub remotely through its Syncthing REST API.
- Per-game emulator settings (RetroArch core and game overrides, per-game INI files),
  controller mappings and input profiles. Settings travel only as single keys.
- Moving logins. RetroAchievements and other sessions are made on each console and
  never leave it.

## Pages

- [Hub, folders and ignores](/warpzone/development/design/hub-and-folders/): the hub layout, the Syncthing folders, the ignore files and the bootstrap problem.
- [Profiles](/warpzone/development/design/profiles/): where things live on a kind of device, library exclusions and BIOS placements, frontend and RetroArch overrides, shadPS4, Cemu paths.
  - [Title links](/warpzone/development/design/title-links/): symlinks from the library's games into an emulator's own folder.
  - [Frontend artwork](/warpzone/development/design/frontend-artwork/): the artwork folders and the generated gamelists.
  - [Play state](/warpzone/development/design/play-state/): favourites and play statistics across devices.
  - [Save conversion](/warpzone/development/design/save-conversion/): saves an emulator keeps in its own format (DraStic's `.dsv`), reconciled with the hub's raw ones.
- [Configuration and command line](/warpzone/development/design/configuration/): the private config, every command, the two rules for writing commands, exit codes.
- [Pruning](/warpzone/development/design/prune/): detecting and deleting what a device no longer whitelists.
- [BIOS check](/warpzone/development/design/bios/): the manifest and the judgements.
- [Inbox](/warpzone/development/design/inbox/): identifying, converting and filing new games, kind by kind.
  - [Unattended inbox](/warpzone/development/design/inbox-unattended/) (implemented): `inbox --apply --unattended` from a scheduled task on the hub, the stable-size rule, the memory of failed items, the heartbeat lock and the run record the library UI shows.
  - [Scraping](/warpzone/development/design/scrape/) (design, not implemented): metadata and media from ScreenScraper through Skyscraper, added to the hub's gamelists and media without changing what is there.
- [Library UI](/warpzone/development/design/library-ui/): the catalog index, the managed whitelists, the web page and its API, the container on the hub.
  - [Play queue](/warpzone/development/design/play-queue/) (file, API and page implemented): the games the user wants to play next, kept on the hub, and how queueing for a console turns the game on there.
- [Emulator settings](/warpzone/development/design/emulator-settings/): layers, apply, capture and audit.
- [Doctor](/warpzone/development/design/doctor/): the machine checklist.
- [Self-update](/warpzone/development/design/self-update/): the device install, the settings, the timer.
  - [Autonomous updates (contract)](/warpzone/development/design/autonomous-updates/): what the updater script guarantees, release channels, verification, rollback, `warpzone health`.
- [Code, testing and CI](/warpzone/development/design/code-and-testing/): the Syncthing API surface, the code layout, the tests and the CI jobs.
