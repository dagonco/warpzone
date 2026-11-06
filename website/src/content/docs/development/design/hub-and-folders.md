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
│   └── play-state/          one file per device: favourites and play statistics ⇄
├── songs/                   hub: send-only → opted-in devices, per-device whitelist
├── tools/                   hub: send-only → devices (scripts, docs)
├── settings/                hub: send-receive ⇄ devices: send-receive, emulator settings layers
├── catalog/                 never synced; the catalog index and whitelist backups (see Library UI)
└── inbox/                   never synced; drop zone for new content (SMB)
```

Why the split:

- `library` is receive-only on devices, so an accidental delete on a device can
  never propagate to the hub.
- Saves need two-way sync; they live in their own folders so their direction
  cannot leak into the library.
- `songs` is not emulation (Clone Hero, YARG) and is mounted outside `~/Emulation`.
- `settings` is written by `warpzone settings capture` on a device, so it is two-way
  like the saves (see [Emulator settings](/warpzone/development/design/emulator-settings/)).

## Folders

Every folder warpzone manages has a stable Syncthing folder ID prefixed `wz-`.

| Folder ID | Hub path | Hub type | Device type | Device path (profile key) | Ignores |
|---|---|---|---|---|---|
| `wz-library` | `library` | sendonly | receiveonly | `library` | per-device whitelist |
| `wz-songs` | `songs` | sendonly | receiveonly | `songs` | per-device whitelist |
| `wz-tools` | `tools` | sendonly | receiveonly | `tools` | common only |
| `wz-media` | `frontend/media` | sendreceive | sendreceive | `media` | common only |
| `wz-esde` | `frontend/es-de` | sendreceive | sendreceive | `esde` | fixed whitelist: `gamelists`, `collections` |
| `wz-play-state` | `frontend/play-state` | sendreceive | sendreceive | `play_state` | fixed `*.wz-tmp` |
| `wz-save-<emu>-<kind>` | `saves/<emu>/<kind>` | sendreceive | sendreceive | from profile | common only |
| `wz-bios-<emu>-<kind>` | `library/<hub path from profile>` | sendonly | receiveonly | from profile | fixed whitelist from the profile |
| `wz-settings` | `settings` | sendreceive | sendreceive | `settings` | common + fixed `*.wz-tmp` |

The `wz-library` ignore lines differ from the other whitelisted folders in one
way: fixed exclusions come before the `#include`, on both sides (see
[Library exclusions](/warpzone/development/design/profiles/#library-exclusions-and-bios-placements)). Save and
`wz-bios-*` entries may carry their own ignore list (a whitelist ending in `*`).

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
`saves` expands to every save folder of the device's profile; `frontend` expands to
`wz-media` and `wz-esde` (a profile with `[artwork]` places and limits them itself, see
[Frontend artwork](/warpzone/development/design/frontend-artwork/)); `bios` expands to every BIOS placement of the profile;
`settings` is `wz-settings`; `play-state` is `wz-play-state` and needs `frontend` (see
[Play state](/warpzone/development/design/play-state/)).

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
