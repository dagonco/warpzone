---
title: "Pruning"
---

Removing a pattern from a device's whitelist stops Syncthing from syncing those
files but leaves the copies on the device. `warpzone prune` deletes them,
safely. It runs on the device against the local Syncthing, and only ever looks
at the folders warpzone manages **and** whitelists (`wz-library`, `wz-songs`).

## How a leftover is detected

warpzone does not parse ignore patterns. Syncthing already decides what is
ignored, and records the verdict in the folder database. Verified against
Syncthing v2.1.7 with two real instances (hub send-only, device receive-only):

- After a **full scan**, `GET /rest/db/file?folder=&file=` on a file that was
  synced and is now ignored answers `local.ignored: true`, `local.invalid:
  true`, `local.localFlags: 2`, `local.size: 0`, while `global` still holds the
  hub's record (`ignored: false`, real size, hub modification time). Directories
  behave the same. This is the signal. `GET /rest/db/ignores` has no "does this
  path match" call (it returns the patterns and an `expanded` list), which is
  why the matcher is not reimplemented.
- The flags are refreshed **only by a full scan**. Right after
  `POST /rest/db/ignores`, files still read `ignored: false` (only a directory
  was flagged); and after re-whitelisting, files still read `ignored: true`
  until the next scan. So `prune` always runs `POST /rest/db/scan` first and
  waits for it (`GET /rest/db/status`: `state` leaves `scanning`/`scan-waiting`;
  `stateChanged` moving or a grace period of a few seconds covers scans that
  start and end between two polls). It therefore does not depend on
  `rescan_seconds`.
- A file Syncthing never recorded (ignored since before it ever scanned it, for
  example something the user created, or a file copied in by hand) answers 404.
  After the full scan this means "not synced and not ours": it is counted and
  left alone, never listed as a leftover.
- A device-only file that was scanned **before** it became ignored is mirrored
  into `global` with `invalid`/`ignored` set, so `global` existing does not
  prove the hub has it. The hub's copy counts only when `global` is not
  `deleted`, `ignored` nor `invalid`.
- Not usable: `GET /rest/db/browse` lists the global tree, ignored or not, with
  no flag; `GET /rest/db/need` and `GET /rest/db/localchanged` came back empty
  for ignored files (they list what is outstanding or locally changed, not what
  is ignored).

`prune` walks the folder on disk (never following symlinks) and asks
`GET /rest/db/file` once per file; the answer is about 1 ms on loopback, so
100k files take on the order of a minute and a half.

## Safety rules

A file is deleted only if all of these hold, re-checked right before each
delete:

1. The folder is a warpzone whitelisted folder, receive-only, not paused, at the
   path the config says, and its marker directory (`.stfolder`) exists (an
   unmounted SD card must not look like an empty library).
2. The folder's ignores are the final `#include patterns-<device>.stignore`
   (the bootstrap ignores, `*`, would mark the whole library as ignored), they
   load without error, and the whitelist files are in sync with the hub.
3. Syncthing flags it `local.ignored` after the full scan.
4. The hub has it: `global` is not deleted, ignored or invalid, and has the same
   kind (file or symlink).
5. For regular files, the size equals the hub's and the modification time is
   within 2 s of the hub's (FAT stores 2 s steps). A file edited on the device
   after it was synced is reported, not deleted.
6. It is not `.stfolder`, `.stignore`, `.stversions` or `patterns-*.stignore` at
   the folder root, nor a Syncthing temp file, and it lives in a directory
   whose real path is inside the folder root. Symlinks are unlinked, never
   followed. The subtree of any other Syncthing folder nested in the root (the
   save folders under `~/Emulation`) is skipped.

Files that are ignored but fail 4 or 5 are listed as kept, with the reason. After
deleting, directories that held deleted files, that Syncthing flags ignored, and
that are now empty are removed (`rmdir`, so a non-empty one is never touched).

The default is report only. `--apply` deletes. A run that would delete more than
`--max-gib` (default 50) refuses to delete anything, exits 1, and needs
`--force` or a higher cap; the cap counts the total over all folders.

Output is a per-folder summary (leftover files and bytes, grouped by the first
two path levels; `--list` prints every file), the files kept and why, and the
count of files Syncthing does not know. Exit codes are those of the other
commands: `0` ok (a report with leftovers is still `0`), `1` for a runtime
error, a folder that was skipped as unsafe, a refused cap or files that could not
be deleted, `2` for usage or config errors.

`--notify-url URL` (http or https) POSTs one JSON object per folder that has
leftovers: `{"device", "folder", "files", "bytes", "applied"}`. When applied,
`files` and `bytes` are what was actually deleted. It is meant for a Home
Assistant webhook. The URL may embed a secret, so it is never printed, and a
failed notification is a warning, not a failure of the run. No API key or path
appears in the payload.
