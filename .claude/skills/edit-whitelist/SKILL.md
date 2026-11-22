---
name: edit-whitelist
description: Use when the user wants a console to receive a game or system it does not have yet, or wants a game, system or folder gone from a console or from the whole library. Covers editing a device's patterns-<device>.stignore on the hub, the order required to delete safely, and prune.
---

# Edit a device's whitelist

A device receives only what its whitelist on the hub allows: the file `patterns-<device>.stignore` inside the synced `library/` folder on the hub (and in `songs/` for devices that subscribe to it). It is edited only on the hub, never on a console. Format, bootstrap and pruning are explained in [whitelists.md](../../../website/src/content/docs/setup/whitelists.md); safety rules in [safety.md](../../../website/src/content/docs/operation/safety.md).

Find the hub root from `[inbox] root` in the user's config (`$XDG_CONFIG_HOME/warpzone/config.toml`) if set; otherwise ask the user where the hub's share is mounted. Device names are the `[devices.<name>]` tables of that config; ask which console is meant. Do not print API keys.

## Before editing

- Keep the `#include` line first and the final `*` last; new `!` lines go between them. `inbox` prints the exact `whitelist:` lines for new games; use those verbatim. Xenia and Model 2 are named by their `roms` subfolder ([whitelists.md](../../../website/src/content/docs/setup/whitelists.md#how-whitelists-work)).
- If the second line is `// warpzone-whitelist`, the library UI manages the file and ends it with a generated `// Folders` section: prefer the UI, and see [whitelists.md](../../../website/src/content/docs/setup/whitelists.md#how-whitelists-work) before editing it by hand (the section must go and `warpzone whitelist migrate` run afterwards).
- Every change made through the UI, `whitelist migrate` or a restore keeps the file it replaced on the hub. To undo one, run `warpzone whitelist restore <device>` to list them, then with a backup name and `--dry-run` to review the diff (paths that come back on are downloaded again by the console), then without `--dry-run`; the UI's History button does the same ([commands.md](../../../website/src/content/docs/reference/commands.md#warpzone-whitelist-restore)).

## Add a game

1. Read the current `patterns-<device>.stignore` and show the user the line you will add. Check it is not already covered by a broader line.
2. Confirm the game exists on the hub (`library/roms/<system>/...`). If it does not, use the `add-games` skill first.
3. Add the line, keeping the `#include` first and `*` last. Back up the file first (copy it beside itself with a `.bak` suffix, remove the copy once the user is happy).
4. The edit reaches the console in seconds and takes effect within the device's `rescan_seconds` (default 5 minutes; [When an edit takes effect](../../../website/src/content/docs/setup/whitelists.md#when-an-edit-takes-effect)). To see it, run `warpzone status` on the console or press Rescan in its Syncthing GUI.

## Remove a game

Hard order. Whitelist-first loses the deletion: Syncthing stops syncing an ignored file but leaves the copy on the console.

Prefer the library UI when it runs a version that has it: **Delete from the hub** on the game's page follows this order by itself; see [Deleting a game](../../../website/src/content/docs/development/design/library-ui.md#deleting-a-game). The steps below are the manual way.

1. A game can be several parts. List every one on the hub and every whitelist line that names it (the lines `inbox` printed for it, and any in other devices' files). What a game's parts are is in [What a deletion removes](../../../website/src/content/docs/development/design/library-ui.md#what-a-deletion-removes); the usual paths:
   - PS4: `roms/ps4/games/<CUSAnnnnn>/` and `roms/ps4/shortcuts/<Title>.desktop`.
   - PS Vita: `roms/psvita/app/<TITLEID>/`, `roms/psvita/<Title>.psvita` and, if present, `roms/psvita/license/<TITLEID>/`.
   - Multi-disc (CHD systems): the `<game>.m3u/` folder with its discs and playlist.
   - Switch: the game in `roms/switch/`, plus its updates in `extras/switch/updates/<Title>/` and DLC in `extras/switch/dlc/<Title>/` (usually whitelisted by folder, so do not remove those lines while other games need them).
   - Other systems: usually one file or folder under `roms/<system>/`; check the folder for extras.

   Show the user the full list of paths and lines, which consoles hold them, and say that deleting from the hub propagates to every console that whitelists them. The hub is the source of truth: check the user has a snapshot ([Snapshots](../../../website/src/content/docs/operation/safety.md#snapshots-on-the-hub)).
2. Wait for the user's explicit OK to delete exactly that list. A dry run or listing is not an OK.
3. Delete every listed part from the hub (library path under the hub root). Re-check each path right before deleting; delete only what was approved.
4. Wait until the deletion has reached every console that whitelists the game: `warpzone status` on each shows `need 0` for `wz-library`. A console that is offline must reconnect first; removing the line before it does leaves its copy behind.
5. Only then remove every whitelist line for those parts from every `patterns-*.stignore` that names them.

If the user only wants the game off one console but kept on the hub, there is no deletion: remove the line, then clean that console with prune below.

## Prune: clean leftovers after a line was removed

On the console, `prune` reports and deletes nothing unless `--apply`. How it decides and its guards are in [Pruning](../../../website/src/content/docs/setup/whitelists.md#pruning-what-a-device-no-longer-whitelists).

```bash
warpzone prune            # report: files the device no longer whitelists
warpzone prune --list     # every file
```

Show the report and wait for the user's OK, then `warpzone prune --apply`. Never pass `--force` without a separate OK.

A console that lists `library` also prunes by itself every hour ([Automatic pruning](../../../website/src/content/docs/setup/whitelists.md#automatic-pruning)), so a removal is done within about an hour of the edit. When its guard holds a removal back (the run exits `1`), the removal waits for the manual `prune --apply` above; do not run `--unattended` by hand to get around that.

If the line was removed first and the game was then deleted with the library UI, prune removes the console copy because the hub's record of deleted games lists it ([Files the hub deleted on purpose](../../../website/src/content/docs/development/design/prune.md#files-the-hub-deleted-on-purpose)); it shows as `N file(s) the hub deleted on purpose` in the report. If the hub copy was deleted by hand, or before the hub kept the record, the console copy stays and prune keeps it (it is not in the record). Tell the user; deleting it on the console by hand needs their explicit OK per path.

## Never

- Edit a file on a console to change what it receives, or delete on a console expecting it to reach the hub (consoles are receive-only).
- Delete or move anything on the hub outside the approved paths.
