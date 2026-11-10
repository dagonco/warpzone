---
name: edit-whitelist
description: Use when the user wants a console to receive a game or system it does not have yet, or wants a game, system or folder gone from a console or from the whole library. Covers editing a device's patterns-<device>.stignore on the hub, the order required to delete safely, and prune.
---

# Edit a device's whitelist

A device receives only what its whitelist on the hub allows. The whitelist is the file `patterns-<device>.stignore` inside the synced `library/` folder on the hub (and in `songs/` for devices that subscribe to it). It is edited only on the hub, never on a console. Reference: [whitelists.md](../../../website/src/content/docs/setup/whitelists.md), rules in [safety.md](../../../website/src/content/docs/operation/safety.md).

Find the hub root from `[inbox] root` in the user's config (`$XDG_CONFIG_HOME/warpzone/config.toml`) if set; otherwise ask the user where the hub's share is mounted. Device names are the `[devices.<name>]` tables of that config; ask which console is meant. Do not print API keys.

## File format

```
#include patterns-common.stignore
!/roms/nes
!/roms/psx/Some Game (USA)
!/bios
*
```

Keep the `#include` first and the final `*` last; new `!` lines go between them. Xenia and Model 2 data live next to the games, so name `!/roms/xbox360/roms` and `!/roms/model2/roms`, not the system folder ([whitelists.md](../../../website/src/content/docs/setup/whitelists.md)). `inbox` prints the exact `whitelist:` lines for new games; use those verbatim.

A whitelist the library UI manages (`// warpzone-whitelist` on its second line) is not edited by hand. Every change made through the UI, `whitelist migrate` or a restore keeps the file it replaced under `catalog/whitelists/<device>/` on the hub (the 50 newest). To undo a change, run `warpzone whitelist restore <device>` where the hub root is mounted to list them, then `warpzone whitelist restore <device> <backup> --dry-run` to see the diff and the paths that would come back on (the console downloads those again), and without `--dry-run` to restore it. The UI's History button on the Consoles page does the same. See [reference/commands.md](../../../website/src/content/docs/reference/commands.md#warpzone-whitelist-restore).

## Add a game

1. Read the current `patterns-<device>.stignore` and show the user the line you will add. Check it is not already covered by a broader line.
2. Confirm the game exists on the hub (`library/roms/<system>/...`). If it does not, use the `add-games` skill first.
3. Add the line, keeping the `#include` first and `*` last. Back up the file first (copy it beside itself with a `.bak` suffix, remove the copy once the user is happy).
4. The edit syncs to the console in seconds and takes effect within the device's `rescan_seconds` (default 300, so 5 minutes; set per device in the config). To see it, run `warpzone status` on the console or press Rescan in its Syncthing GUI.

## Remove a game

Hard order. Whitelist-first loses the deletion: Syncthing stops syncing an ignored file but leaves the copy on the console.

1. A game can be several parts. List every one on the hub and every whitelist line that names it (the lines `inbox` printed for it, and any in other devices' files):
   - PS4: `roms/ps4/games/<CUSAnnnnn>/` and `roms/ps4/shortcuts/<Title>.desktop`.
   - PS Vita: `roms/psvita/app/<TITLEID>/`, `roms/psvita/<Title>.psvita` and, if present, `roms/psvita/license/<TITLEID>/`.
   - Multi-disc (CHD systems): the `<game>.m3u/` folder with its discs and playlist.
   - Switch: the game in `roms/switch/`, plus its updates in `extras/switch/updates/<Title>/` and DLC in `extras/switch/dlc/<Title>/` (usually whitelisted by folder, so do not remove those lines while other games need them).
   - Other systems: usually one file or folder under `roms/<system>/`; check the folder for extras.

   Layouts: [inbox.md](../../../website/src/content/docs/setup/inbox.md). Show the user the full list of paths and lines, which consoles hold them, and say that deleting from the hub propagates to every console that whitelists them. The hub is the source of truth: check the user has a snapshot ([Snapshots](../../../website/src/content/docs/operation/safety.md#snapshots-on-the-hub)).
2. Wait for the user's explicit OK to delete exactly that list. A dry run or listing is not an OK.
3. Delete every listed part from the hub (library path under the hub root). Re-check each path right before deleting; delete only what was approved.
4. Wait until the deletion has reached every console that whitelists the game: `warpzone status` on each shows `need 0` for `wz-library` (deletions count in `need`). A console that is offline must reconnect first; removing the line before it does leaves its copy behind.
5. Only then remove every whitelist line for those parts from every `patterns-*.stignore` that names them.

If the user only wants the game off one console but kept on the hub, there is no deletion: remove the line, then clean that console with prune below.

## Prune: clean leftovers after a line was removed

On the console, `prune` reports and deletes nothing unless `--apply`:

```bash
warpzone prune            # report: files the device no longer whitelists
warpzone prune --list     # every file
```

Show the report and wait for the user's OK, then `warpzone prune --apply`. It deletes only files that Syncthing ignores and the hub still has unchanged, refuses more than `--max-gib` (default 50) without `--force`, and keeps anything that exists only on the console. Never pass `--force` without a separate OK.

A console that lists `library` also prunes by itself: its `warpzone-prune` timer runs `prune --apply --unattended` every hour (unless `auto_prune = false`), with no size cap, so a removal is done within about an hour and five minutes of the edit. It deletes nothing when a folder would lose more than half of what the device holds; that run exits `1` and the removal waits for the manual `prune --apply` above. Do not run `--unattended` by hand to get around that.

If the line was removed first and the hub copy was deleted afterwards, the console copy stays and prune keeps it too (it deletes only files the hub still has). Tell the user; deleting it on the console by hand needs their explicit OK per path.

## Never

- Edit a file on a console to change what it receives, or delete on a console expecting it to reach the hub (consoles are receive-only).
- Delete or move anything on the hub outside the approved paths.
