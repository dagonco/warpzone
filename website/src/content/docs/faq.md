---
title: "FAQ"
description: "Short answers to common questions."
---

Short answers to common questions. For something that is not working, see [Troubleshooting](/warpzone/troubleshooting/).

## Why Syncthing?

warpzone is a thin layer of conventions and automation on top of [Syncthing](https://syncthing.net). Syncthing does all the transfer between machines; warpzone configures it, files new games into the hub's library and removes from a device what its whitelist no longer allows. Syncthing already gives what the design needs: folders that are receive-only on a console (so a delete there can never reach the hub), folders that sync both ways (saves, settings), ignore patterns that work as a per-device whitelist, and versioning on the hub. See [concepts](/warpzone/overview/concepts/).

## Does warpzone download games or BIOS files?

Never. warpzone does not download games, BIOS files, keys or firmware, and the repository contains none. The BIOS checker compares what you have with a manifest of public names, sizes and hashes ([BIOS](/warpzone/setup/bios/)). New games come from dumps you drop in the hub's `inbox/` ([Adding games](/warpzone/setup/inbox/)).

## Which consoles are supported?

Two profiles ship with warpzone, in `warpzone/data/profiles/`:

| Profile | For |
|---|---|
| `steamos-emudeck` | SteamOS with EmuDeck (Steam Deck, Steam Machine) |
| `anbernic-rg351mp` | Anbernic RG351MP running dArkOS (EmulationStation and RetroArch, ROMs on an exFAT card) |

Guides: [Steam Deck / EmuDeck](/warpzone/guides/emudeck/), [Anbernic](/warpzone/guides/anbernic/) and [Steam Machine](/warpzone/guides/steam-machine/). Another kind of device is described with a profile ([Adding a new console](/warpzone/guides/new-console/)).

## Is it safe?

It is built so that a mistake costs as little as possible ([Safety](/warpzone/operation/safety/)):

- Commands that change files report first and act only with `--apply` (`prune`, `inbox`). `provision` has `--dry-run`, which prints the Syncthing calls it would make and sends none.
- The library is receive-only on devices, so a delete or an edit on a console never propagates to the hub.
- Devices are only ever added, folders not prefixed `wz-` are never touched, and a changed folder path is refused.
- `inbox` only adds files and never overwrites; what it cannot place goes to `inbox/_revisar/` with a reason.
- API keys are never printed or logged, and are redacted from dry-run output.

Turn on filesystem snapshots for the hub's games share as well. warpzone keeps no versions of the library, so a snapshot is what turns a bad edit on the hub into one rollback.

## Can I use it without a NAS?

The hub is the machine that holds the library and runs Syncthing. A NAS is the typical choice, not a requirement: any always-on machine that runs Syncthing v2, holds the library under `[hub].root`, exposes its GUI and REST API on a LAN address for the admin machine, is reachable by the consoles through Syncthing, and shares its hub root with the desktop that runs `inbox` can be the hub. warpzone does not run on the hub itself.

## How do updates work?

A console runs warpzone from a git clone and keeps itself on a release that works. A systemd user timer starts an updater script 45 seconds after boot and then every 15 minutes. The script picks the best release tag of its channel (`stable` by default, or `rc`), switches to it, and checks it: warpzone imports, `provision device <name> --unattended` runs, and `warpzone health <name>` passes. If a check fails, it goes back to the version it came from, and a release that proved bad is not tried again. It waits while ES-DE is open. You can pin a device to a tag or turn updates off in the config. See [Updating a device](/warpzone/operation/self-update/).

## How do I remove a game?

Removing a line from a whitelist does not delete the console's copy. To delete a game everywhere: delete it from the hub, wait until every console shows `need 0` in `warpzone status`, then remove the whitelist line. To only free space on one console, remove the line and run `warpzone prune` (add `--apply` once the report looks right). Details in [Troubleshooting](/warpzone/troubleshooting/#a-game-removed-from-a-whitelist-is-still-on-the-console) and [Whitelists and pruning](/warpzone/setup/whitelists/).

## Where is every command and option?

In the [command reference](/warpzone/reference/commands/). `warpzone doctor` checks a machine and prints the fix for each problem ([the doctor guide](/warpzone/operation/doctor/)).
