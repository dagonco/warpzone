---
title: "Hub, folders and ignores"
---

## Hub layout

```
<hub root>/
├── library/                 hub: send-only   → device: receive-only, per-device whitelist
│   ├── roms/<system>/       ES-DE system folder names
│   ├── bios/                some subfolders are also separate `wz-bios-*` folders (see Profiles)
│   ├── extras/              switch/{updates,dlc,mods,misc}, amiibo/, texturepacks/
│   ├── patterns-common.stignore
│   └── patterns-<device>.stignore
├── saves/<emulator>/<kind>/ hub: send-receive ⇄ devices: send-receive, hub keeps history
├── frontend/
│   ├── media/               ES-DE downloaded media     ⇄
│   ├── es-de/               ES-DE gamelists, collections ⇄
│   └── play-state/          per device: favourites and play statistics, disk report ⇄
├── songs/                   hub: send-only → opted-in devices, per-device whitelist
├── tools/                   hub: send-only → devices (scripts, docs)
├── settings/                hub: send-receive ⇄ devices: send-receive, emulator settings layers
├── catalog/                 never synced; the catalog index, whitelist backups, the play queue and the inbox's run records (`catalog/inbox/runs/`, see Library UI and Inbox)
└── inbox/                   never synced; drop zone for new content (SMB)
```

Why the split:

- `library` is receive-only on devices, so an accidental delete on a device can
  never propagate to the hub.
- Saves need two-way sync; they live in their own folders so their direction
  cannot leak into the library.
- `songs` is not emulation (Clone Hero, YARG) and is mounted outside `~/Emulation`. YARG's own
  data (`wz-save-yarg-data`) travels with it, not with `saves`.
- `settings` is written by `warpzone settings capture` on a device, so it is two-way
  like the saves (see [Emulator settings](/warpzone/development/design/emulator-settings/)).

## Folders

Every folder warpzone manages has a stable Syncthing folder ID prefixed `wz-`.

| Folder ID | Hub path | Hub type | Device type | Device path (profile key) | Ignores |
|---|---|---|---|---|---|
| `wz-library` | `library` | sendonly | receiveonly | `library` | per-device whitelist |
| `wz-songs` | `songs` | sendonly | receiveonly | `songs` | per-device whitelist |
| `wz-tools` | `tools` | sendonly | receiveonly | `tools` | common only |
| `wz-media` | `frontend/media` | sendreceive | sendreceive | `media` | common + fixed `*.wz-tmp`, and on a device `/*/videos` |
| `wz-esde` | `frontend/es-de` | sendreceive | sendreceive | `esde` | fixed `*.wz-tmp`, then a whitelist: `gamelists`, `collections` |
| `wz-play-state` | `frontend/play-state` | sendreceive | sendreceive | `play_state` | fixed `*.wz-tmp` |
| `wz-save-<emu>-<kind>` | `saves/<emu>/<kind>` | sendreceive | sendreceive | from profile | common only |
| `wz-bios-<emu>-<kind>` | `library/<hub path from profile>` | sendonly | receiveonly | from profile | fixed whitelist from the profile |
| `wz-settings` | `settings` | sendreceive | sendreceive | `settings` | common + fixed `*.wz-tmp` |

The `wz-library` ignore lines differ from the other whitelisted folders in one
way: fixed exclusions come before the `#include`, on both sides (see
[Library exclusions](/warpzone/development/design/profiles/#library-exclusions-and-bios-placements)). Save and
`wz-bios-*` entries may carry their own ignore list (a whitelist ending in `*`).

The hub's `frontend/media/<system>/videos` (ES-DE's video previews, tens of MiB each) are for
the library UI only, so a device that receives the whole `wz-media` ignores `/*/videos`. The line
is in the device's ignores and not the hub's, since Syncthing ignores are local to each side and
the hub must keep (and serve) the videos. `provision device` sets the ignores of an existing
folder too, so a device converges on its next run; files already on the device are left where
they are. A profile with `[artwork]` receives only the kinds it lists (never `videos`).

"Common only" and "fixed whitelist" folders get their ignore lines inline
through `POST /rest/db/ignores` (the common lines, then the fixed or
per-save lines), on the hub and on devices alike. They need no ignore files,
so the user only ever creates `patterns-*.stignore` in `library/` and `songs/`.

Folder-level settings:

- Save folders, `wz-settings` and `wz-play-state` on the hub: versioning `staggered`, `maxAge` 30 days.
- Everything else: no versioning (the hub is the authority; filesystem
  snapshots on the hub are recommended in [Safety](/warpzone/operation/safety/) instead).
- `fsWatcherEnabled: true` everywhere.
- `ignorePerms: true` on every folder of a device whose profile sets `ignore_perms`
  (the library sits on exFAT, which has no POSIX permissions). It is set on the device's
  folders only; other profiles never get the key, and a folder that already exists is
  patched when it is missing.

A device subscribes to a subset of folders, set in the private config
(`folders = ["library", "tools", "saves", "frontend", "songs", "bios", "settings", "play-state"]`).
`saves` expands to every save folder of the device's profile that belongs to `saves`, which is
every one unless its entry names another group (`group = "songs"` on `yarg-data`: YARG's data
follows the `songs` group, so a device without `songs` gets neither); `frontend` expands to
`wz-media` and `wz-esde` (a profile with `[artwork]` places and limits them itself, see
[Frontend artwork](/warpzone/development/design/frontend-artwork/)); `bios` expands to every BIOS placement of the profile;
`settings` is `wz-settings`; `play-state` is `wz-play-state` and needs `frontend` (see
[Play state](/warpzone/development/design/play-state/)).

### Devices that stop subscribing

A device in the config that no longer subscribes to a `wz-*` folder (a group taken out of its
`folders`, or a profile entry moved to another group) is taken off that hub folder by the next
`provision hub`. No file is deleted and no folder is removed. A device the config does not name
keeps its shares, and a `wz-*` folder no device subscribes to is only reported. The console's
Syncthing keeps the folder, so the hub shows it as an offer from that console: remove the
folder in the console's Syncthing (its files stay), and do not accept the offer on the hub.

The decision is `provision_hub.unsubscribed` (pure): the entries of the hub's folder whose
device ID is one of the config's devices and not one of the folder's subscribers, in the
folder's order. The hub's own entry is never one of them, and every other entry is sent back
as it was read, with all its fields (`introducedBy`, `encryptionPassword`). Syncthing replaces
the whole device list on a `PATCH`, and the list was read before the waits of the folders that
came first (a new whitelisted folder waits up to `--timeout`), so the run reads the folder
again right before it writes, in a dry run too, and decides again: a device someone added by
hand in that time is not lost.

The removal is convergent, not destructive: running the same config again changes nothing,
and putting the group back in the device's `folders` shares the folder again. Only a person
runs it. The timers run `provision device --unattended`, which at most creates a missing folder on
the hub (`auto_hub`) and never changes the device list of a folder the hub already has, and
`provision hub --additive` (also `auto_hub`) changes nothing on a folder the hub has: it only warns that a device the folder is shared with no longer subscribes.

`--dry-run` prints `would stop sharing with <device>` and the `PATCH` it would send. If the
hub's Syncthing auto-accepts folders from the device, or the dropped entry names an
introducer, Syncthing may share the folder again, so the run warns; warpzone does not change
device settings. A `wz-*` folder that no spec describes but a configured device is on (a
group taken out of every device, or an admin checkout older than a console's release that
does not know a folder yet) is only reported, never changed. Run `provision hub` from the
release the consoles run, so that two checkouts do not add and remove the same share.

The console has the mirror of that report. Its own Syncthing keeps the `wz-*` folder of a
group it no longer subscribes to, and `provision device` only walks the folders its config
describes, so `provision_folders.undescribed` (pure, shared by `provision device` and
`doctor`) names every `wz-*` folder of the device's Syncthing that none of its specs
describes, sorted by folder ID, with its path. `provision device` reports each as a warning
after its folders (in a dry run and in an unattended run too) and never writes anything for
it: removing the folder is a person's decision, in the console's Syncthing, and its files stay.
All the specs of the device count as described, also those skipped because their directory
does not exist yet. `doctor` reports the same condition (see
[Doctor](/warpzone/development/design/doctor/)).

## Ignore files and the bootstrap problem

All whitelist files live **inside the synced folder on the hub** and are synced
to every device. Each device's own `.stignore` is a single line that never
changes after provisioning:

```
#include patterns-<device>.stignore
```

`patterns-common.stignore` (shipped as a template in `templates/`):

```
// Every device receives every whitelist; each one only applies its own.
!/patterns-*.stignore
(?d)@eaDir
(?d).DS_Store
(?d)._*
(?d)Thumbs.db
```

`patterns-<device>.stignore` starts with `#include patterns-common.stignore`,
lists what the device wants, and ends with `*` if it is a whitelist. The
hub's own `.stignore` is `#include patterns-common.stignore`.

**Bootstrap.** Syncthing refuses to sync a folder whose `.stignore` includes a
file that does not exist yet, and on a fresh device the whitelist has not
arrived. Provisioning therefore does, for each whitelisted folder:

1. Create the folder **paused**, with ignores set (through `POST /rest/db/ignores`) to
   ```
   !/patterns-*.stignore
   *
   ```
   so that only the whitelists can arrive.
2. Unpause and wait (default timeout 10 min) until `patterns-<device>.stignore`
   and `patterns-common.stignore` (which the device whitelist includes) exist in
   the folder root and their local version equals the global one
   (`GET /rest/db/file`).
3. Replace the ignores with `#include patterns-<device>.stignore` and trigger a
   rescan.

**Folders above a partial include.** Syncthing matches an ignore line against a path and, as
the implicit `/**` it adds to every pattern, against everything below it, and the first line
that matches decides. A whitelist that turns on one game of a system (`!/roms/psx/Some Game`)
therefore leaves `roms` and `roms/psx` matched only by the final `*`: the console creates them
for the game's files, never takes the hub's record for them, and the hub counts them as needed
forever (the bytes are complete, the items are not). No line in the fixed part can fix this: a
`!` line that matches a folder also matches everything under it, so it would deliver whole
systems, and the part after the `#include` is never reached. The pair of lines belongs in the
whitelist itself, after every other line: `/<folder>/*` keeps what is below the folder out and
`!/<folder>` delivers the folder alone. The library UI's file carries them as its derived
`// Folders` section; see [the file the UI writes](/warpzone/development/design/library-ui/#the-file-the-ui-writes).
A hand-written whitelist does not have them and keeps the defect until it is migrated.

**Applying later edits.** After provisioning, `patterns-<device>.stignore` changes
on the hub only, written by the [library UI](/warpzone/development/design/library-ui/#the-whitelist-model). It reaches the device through
normal sync, but Syncthing v2.1.7 re-evaluates ignores (and pulls files that
became un-ignored) only on a full scan of the folder. A scan of a single
sub-path does not do it, and a file synced in by the puller does not reliably
cause a full scan: it depends on the OS and the filesystem watcher, which made
the e2e test flaky on Linux CI. Without a bound, a device could wait for the
default `rescanIntervalS` (3600 s). Whitelisted device folders therefore get
`rescanIntervalS = rescan_seconds` (per-device config key, default 300,
minimum 5), so an edit applies within that time whatever the watcher does. A
rescan only stats files and hashes what changed.

**Hub bootstrap.** The hub has the opposite problem. Its folder is created with the final
ignores (`#include patterns-common.stignore`), so the first full scan walks the whole
library before the whitelists are in the hub's index, and a device waits for
`patterns-<device>.stignore` until the scanner happens to reach it. With a large library
on a slow disk that is hours. When `provision hub` **creates** a whitelisted folder
(library, songs) it therefore does, in order:

1. Create the folder paused and set the same bootstrap ignores the device uses
   (`!/patterns-*.stignore`, `*`). The fixed hub exclusions are not needed: `*` ignores
   everything the whitelists do not name.
2. Unpause and trigger a scan. With everything else ignored it reaches the whitelists at
   once, and Syncthing announces them to the devices in its first index batches.
3. Wait (`provision hub --timeout`, default 120 s) until `patterns-common.stignore` and the
   `patterns-<device>.stignore` of every subscribed device are in the hub's own index
   (`GET /rest/db/file`: a live `local` record, not deleted, not invalid).
4. Set the final ignores and scan again, which starts the full scan. Devices can by now
   fetch their whitelist while the scanner is still working on the rest.

If the whitelists are not in the index when the timeout runs out (they were never put on
the hub, or the hub is very slow), it warns and still sets the final ignores: the hub
folder is never left half-configured. Only creation does this; a folder that already
exists keeps the direct path (ignores set if they differ, folder resumed if paused). In
dry-run the calls are printed and nothing waits.

If the device's whitelist does not exist on the hub, provisioning stops before
step 1 with a clear error. It never creates a device folder that would pull the
whole library. The check reads the hub's API (`GET /rest/db/browse`) with the
hub credentials from the private config; if the hub is unreachable it warns and
continues, which is safe because the bootstrap ignores only let whitelists
through. On the hub side, `provision hub` cannot check files in a folder that
does not exist yet, so it creates the folder and warns about missing whitelists
(only if they are still missing after the hub bootstrap above);
`warpzone check` is the authoritative report once the hub has scanned.
