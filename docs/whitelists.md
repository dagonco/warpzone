# Whitelists and pruning

How a device chooses what it receives, how a new device bootstraps safely, and how to clean up what a device no longer whitelists.

## How whitelists work

The whitelist for a device is an ordinary Syncthing ignore file that lives **inside the synced folder on the hub**, so it syncs to every device like any other file. A device's own `.stignore` is a single line that never changes after provisioning:

```
#include patterns-deck.stignore
```

To change what a device gets, edit `patterns-deck.stignore` on the hub (over SMB, SSH, wherever). No file is ever edited by hand on a device.

A whitelist starts with `#include patterns-common.stignore`, lists what the device wants, and ends with `*`:

```
#include patterns-common.stignore
!/roms/nes
!/roms/psx/Some Game (USA)
!/bios
*
```

Every device receives every whitelist but applies only its own. Start from [`templates/patterns-common.stignore`](../templates/patterns-common.stignore) and [`templates/patterns-device.example.stignore`](../templates/patterns-device.example.stignore). The same scheme applies to the `songs/` folder for devices that subscribe to it.

Folders without a whitelist (tools, media, saves, ES-DE) get their fixed ignore lines directly through the API; they need no files.

## When an edit takes effect

The edit syncs to the device within seconds, but Syncthing only applies a changed whitelist on a full scan of the folder, and a freshly synced ignore file does not reliably trigger one (whether it does depends on the OS and the filesystem watcher). So `warpzone` sets a short rescan interval on whitelisted device folders: **a whitelist edit takes effect within 5 minutes**.

Tune it per device with `rescan_seconds` in the config (minimum 5; a rescan only stats files, so it is cheap). To apply an edit immediately, press Rescan on the folder in the device's Syncthing GUI.

## Bootstrap on the hub

A whitelisted folder (library, songs) that `provision hub` **creates** is first set to let only the whitelists through. The command then waits (default 120 seconds, `--timeout`) until `patterns-common.stignore` and every subscribed device's `patterns-<device>.stignore` are in the hub's index, and only then sets the final ignores, which starts the full scan.

Without this, the first scan of a big library on a slow NAS can take hours to reach the whitelists, and every `provision device` waits for them. If the whitelists never show up, the command warns and sets the final ignores anyway. A folder that already exists is not touched this way.

## Bootstrap on a device

Syncthing refuses to sync a folder whose `.stignore` includes a file that has not arrived yet, and on a fresh device nothing has. So `provision device` first creates the folder with ignores that let only the whitelists through, waits (default 10 minutes, `--timeout`) until `patterns-<device>.stignore` and `patterns-common.stignore` are on the device, and only then switches to the `#include`.

If the device's whitelist is missing on the hub, the command stops before touching anything, so a device can never pull the whole library.

## Pruning what a device no longer whitelists

When you take something off a device's whitelist, Syncthing stops syncing it but leaves the files on the device. Run `prune` on the device to clean up. It only reports unless you add `--apply`:

```
$ warpzone prune
wz-library: 412 leftover file(s), 38.2 GiB
  roms/n64: 380 file(s), 36.9 GiB
  roms/psx: 32 file(s), 1.3 GiB
wz-library: kept 1 ignored file(s) the hub cannot restore
  roms/n64/my-patch.z64 (the hub does not have it)
total: 412 leftover file(s), 38.2 GiB
report only: run again with --apply to delete

$ warpzone prune --apply
...
wz-library: deleted 412 file(s), 38.2 GiB, removed 9 empty folder(s)
```

It asks Syncthing which files it ignores (after a full rescan) instead of guessing from the patterns, and it deletes a file only if the hub also has it, unchanged. Anything that exists only on the device, or that you edited, is reported and kept.

| Guard or option | Effect |
|---|---|
| `--max-gib N` (default 50) | Refuses to delete more than N GiB in one run unless you pass `--force`. |
| Protected paths | Never touches `.stfolder`, `.stignore`, the whitelist files or other Syncthing folders. |
| Bootstrap | Refuses to run while a device is still bootstrapping. |
| `--list` | Prints every file. |
| `--notify-url URL` | POSTs a JSON summary (`device`, `folder`, `files`, `bytes`, `applied`) to a webhook, for example a Home Assistant automation. |

The algorithm and its safety rules are in [design.md](design.md#pruning).
