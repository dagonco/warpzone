---
name: edit-whitelist
description: Use when the user wants a console to receive a game or system it does not have yet, or wants a game, system or folder gone from a console or from the whole library. Covers editing a device's patterns-<device>.stignore on the hub, the order required to delete safely, and prune.
---

# Edit a device's whitelist

A device receives only what its whitelist on the hub allows. The whitelist is the file `patterns-<device>.stignore` inside the synced `library/` folder on the hub (and in `songs/` for devices that subscribe to it). It is edited only on the hub, never on a console. Reference: [whitelists.md](../../../docs/whitelists.md), rules in [safety.md](../../../docs/safety.md).

Find the hub root from `[inbox] root` in the user's config (`$XDG_CONFIG_HOME/warpzone/config.toml`) if set; otherwise ask the user where the hub's share is mounted. Device names are the `[devices.<name>]` tables of that config; ask which console is meant. Do not print API keys.

## File format

```
#include patterns-common.stignore
!/roms/nes
!/roms/psx/Some Game (USA)
!/bios
*
```

Keep the `#include` first and the final `*` last; new `!` lines go between them. Xenia and Model 2 data live next to the games, so name `!/roms/xbox360/roms` and `!/roms/model2/roms`, not the system folder ([whitelists.md](../../../docs/whitelists.md)). `inbox` prints the exact `whitelist:` lines for new games; use those verbatim.

## Add a game

1. Read the current `patterns-<device>.stignore` and show the user the line you will add. Check it is not already covered by a broader line.
2. Confirm the game exists on the hub (`library/roms/<system>/...`). If it does not, use the `add-games` skill first.
3. Add the line, keeping the `#include` first and `*` last. Back up the file first (copy it beside itself with a `.bak` suffix, remove the copy once the user is happy).
4. The edit syncs to the console in seconds and takes effect within 5 minutes (the folder's rescan interval). To see it, run `warpzone status` on the console or press Rescan in its Syncthing GUI.

## Remove a game

Hard order. Whitelist-first loses the deletion: Syncthing stops syncing an ignored file but leaves the copy on the console.

1. Show the user exactly what will go: the path on the hub, the whitelist line, and which consoles hold it. Say that deleting from the hub propagates to every console that whitelists it, and that the hub is the source of truth (check the user has a snapshot, see [Snapshots](../../../docs/safety.md#snapshots-on-the-hub)).
2. Wait for the user's explicit OK to delete. A dry run or listing is not an OK.
3. Delete the game from the hub (library path under the hub root). Re-check the path right before deleting; delete only what was approved.
4. Wait until the deletion reaches the console (`warpzone status` on the console shows no outstanding items).
5. Only then remove the whitelist line from `patterns-<device>.stignore`.

If the user only wants the game off one console but kept on the hub, there is no deletion: remove the line, then clean that console with prune below.

## Prune: clean leftovers after a line was removed

On the console, `prune` reports and deletes nothing unless `--apply`:

```bash
warpzone prune            # report: files the device no longer whitelists
warpzone prune --list     # every file
```

Show the report and wait for the user's OK, then `warpzone prune --apply`. It deletes only files that Syncthing ignores and the hub still has unchanged, refuses more than `--max-gib` (default 50) without `--force`, and keeps anything that exists only on the console. Never pass `--force` without a separate OK.

If the line was removed first and the hub copy was deleted afterwards, the console copy stays and prune keeps it too (it deletes only files the hub still has). Tell the user; deleting it on the console by hand needs their explicit OK per path.

## Never

- Edit a file on a console to change what it receives, or delete on a console expecting it to reach the hub (consoles are receive-only).
- Delete or move anything on the hub outside the approved paths.
