---
title: "Whitelists and pruning"
---

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

A whitelist the library UI manages (`// warpzone-whitelist` on its second line) ends with a generated `// Folders` section before the `*`. It delivers the folders above a partly whitelisted system (`roms`, `roms/psx`) as empty folders, so the hub sees the console as fully in sync; it never changes which files arrive. Do not edit the section. If you do edit a managed file by hand, delete the whole `// Folders` section (its comment and its lines, leaving the final `*`) and then run `warpzone whitelist migrate`: the migration refuses a file that still has the section, because its lines are wildcards, and writes it again itself. Deleting the section does not change which files the console receives. A hand-written file does not have the section: it is added by the migration. A whitelisted game the hub does not have delivers the empty folder of its system, which ES-DE hides. See [the file the UI writes](/warpzone/development/design/library-ui/#the-file-the-ui-writes).

Every device receives every whitelist but applies only its own. Start from [`templates/patterns-common.stignore`](https://github.com/dagonco/warpzone/blob/main/templates/patterns-common.stignore) and [`templates/patterns-device.example.stignore`](https://github.com/dagonco/warpzone/blob/main/templates/patterns-device.example.stignore). The same scheme applies to the `songs/` folder for devices that subscribe to it.

Xenia and Model 2 keep their own data inside the system folder, next to the games, which are in its `roms/` subfolder. Name `!/roms/xbox360/roms` and `!/roms/model2/roms` (not `!/roms/xbox360` or `!/roms/model2`) and the emulator data stays on the device. See [the EmuDeck guide](/warpzone/guides/emudeck/#xenia-and-model-2-live-inside-roms).

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

The algorithm and its safety rules are in [the design document](/warpzone/development/design/prune/).

### Automatic pruning

A device that lists `library` prunes by itself. `provision device` installs a `warpzone-prune` timer (a systemd user timer, 15 minutes after boot and then every hour) that runs `warpzone prune <device> --apply --unattended`. It applies every rule above, with the re-check right before each delete, but has no size cap, because one game can be larger than `--max-gib`. Instead it refuses to delete anything, in any folder, when a folder would lose more than half of what the device holds in it. That is what stops a whitelist emptied by mistake from wiping a console. The run prints why and exits `1`, the unit shows as failed, and the files stay until you run `prune --apply` by hand (where `--max-gib` and `--force` apply). Nobody watches that unit, so the run also leaves a small note on the device and `warpzone doctor` warns about it, naming the folders, until a later `prune --apply` run goes through. To clear it, check the whitelist, run `warpzone prune <device>` to review what would go, then `warpzone prune <device> --apply`. A device that holds a single game can only lose it that way. A folder that is still downloading is skipped without failing, and the next run tries again.

Set `auto_prune = false` on the device in the config to remove the timer and prune by hand only. The design is in [Unattended runs](/warpzone/development/design/prune/#unattended-runs).
