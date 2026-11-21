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

A file is deleted only if rules 1, 2, 3 and 6 hold and either rules 4a and 5 or rule 4b holds,
all re-checked right before each delete:

1. The folder is a warpzone whitelisted folder, receive-only, not paused, at the
   path the config says, and its marker directory (`.stfolder`) exists (an
   unmounted SD card must not look like an empty library).
2. The folder's ignores are the final `#include patterns-<device>.stignore`
   (the bootstrap ignores, `*`, would mark the whole library as ignored), they
   load without error, and the whitelist files are in sync with the hub (Syncthing
   reports their local and global versions equal, `provision_folders.in_sync`).
3. Syncthing flags it `local.ignored` after the full scan.
4. The hub's state allows it, in one of two ways:
   - **4a. The hub has it.** `global` is not deleted, ignored or invalid, and has the
     same kind (file or symlink).
   - **4b. The hub deleted it on purpose.** `global` is deleted and is neither ignored nor
     invalid, and all of these hold (see
     [Files the hub deleted on purpose](#files-the-hub-deleted-on-purpose)):
     - the folder is `wz-library` and the file is a regular file, never a symlink and never a
       directory;
     - the console's `patterns-_deleted.stignore` is in sync with the hub, read after the full
       scan: Syncthing reports that file's local and global versions equal (the same notion as
       the whitelist files in rule 2, and `global` is not deleted, ignored or invalid either),
       and its size on disk equals its local record;
     - the record parses, and an entry that has not expired lists the same path with the same
       size and a modification time within 2 s of the file's (FAT stores 2 s steps);
     - when that entry has a `sha1`, the file's SHA-1 equals it. A path can be in several
       entries; it is authorised when any one of them matches the file, so one with no `sha1`
       that matches the size and time is enough.

     Right before each delete all of this is checked again: the record file is still the
     version that was read (the same Syncthing local version, the same size and modification
     time on disk), `global` is still deleted, and the file's size and modification time are
     unchanged after it was hashed.
5. Under 4a, for regular files, the size equals the hub's and the modification time is
   within 2 s of the hub's (FAT stores 2 s steps). A file edited on the device
   after it was synced is reported, not deleted. Under 4b the hub has no copy to compare
   with, so the record's size and modification time (rule 4b) take its place.
6. It is not `.stfolder`, `.stignore`, `.stversions` or `patterns-*.stignore` at
   the folder root, nor a Syncthing temp file, and it lives in a directory
   whose real path is inside the folder root. Symlinks are unlinked, never
   followed. The subtree of any other Syncthing folder nested in the root (the
   save folders under `~/Emulation`) is skipped.

Files that are ignored but fail 4a and 4b, or 5, are listed as kept, with the reason; the
expected ones are only counted (see [Files the console generates](#files-the-console-generates)).
After deleting, directories that held deleted files, that Syncthing flags ignored, and
that are now empty are removed (`rmdir`, so a non-empty one is never touched). A full scan does
not always flag a directory that just became ignored (seen on Linux), so each candidate is
scanned on its own (`POST /rest/db/scan?folder=...&sub=...`) before its record is trusted.

The default is report only. `--apply` deletes. A run that would delete more than
`--max-gib` (default 50) refuses to delete anything, exits 1, and needs
`--force` or a higher cap; the cap counts the total over all folders. A run with
`--unattended` has no size cap and the half-of-device guard instead (see
[Unattended runs](#unattended-runs)).

Output is a per-folder summary (leftover files and bytes, grouped by the first
two path levels; `--list` prints every file), one line `<folder>: N file(s) the hub deleted on
purpose, <size>` when there are such files (they are listed with the leftovers), the unusual
files kept and why, one line with the count of files the console generates by itself, and the
count of files Syncthing does not know. Exit codes are those of the other
commands: `0` ok (a report with leftovers is still `0`), `1` for a runtime
error, a folder that was skipped as unsafe, a refused cap or files that could not
be deleted, `2` for usage or config errors.

`--notify-url URL` (http or https), with or without `--unattended`, POSTs one
JSON object per folder that has leftovers or files the hub deleted on purpose:
`{"device", "folder", "files", "bytes", "applied"}`, counting both. When applied,
`files` and `bytes` are what was actually deleted. It is meant for a Home
Assistant webhook. The URL may embed a secret, so it is never printed, and a
failed notification is a warning, not a failure of the run. No API key or path
appears in the payload. The sender is `notify.py` (`notify`, which raises `NotifyError`), shared
with `inbox --notify-url` (see [The run record](/warpzone/development/design/inbox/#the-run-record));
`prune` catches the error and prints it as a warning.

## Files the console generates

EmuDeck and some emulators write files into the library that the hub never has, and the
profile's `[library] ignore` list ignores them, so on every run they are ignored files the hub
cannot restore. Listing hundreds of them hides the few kept files that matter. `prune.is_generated`
(pure, no I/O) recognises them by path, relative to the folder root, with exact, case-sensitive
matching per path segment:

- `roms/<system>/metadata.txt` and `roms/<system>/systeminfo.txt` (one level below `roms`).
- Any `readme.txt` at any depth under `bios/`.
- Everything under `bios/PPSSPP`, `bios/HdPacks`, `bios/Mupen64plus`, `bios/fbneo`,
  `bios/kronos`, `bios/same_cdi`, `bios/ryujinx`, `bios/shadps4` and `bios/yuzu`.
- RetroArch's Flycast files directly in `bios/dc`: `vmu_save_*.bin`, `dc_nvmem.bin` and
  `vulkan_pipeline.cache`, plus everything under `bios/dc/data`.
- `bios/mame2003-plus/hiscore.dat` and everything under `bios/mame2003-plus/artwork`.

The list mirrors the profile's ignores, so a new ignore for a file an emulator writes by itself
is added to both. A file counts here only when its verdict is "the hub does not have it": a
matching file the hub does have in a different state is still listed as kept. These files are
still kept, never deleted; they are reported as one line, `<folder>: N file(s) emulators and
EmuDeck generate themselves`, in the normal and in the unattended output. The `kept ... ignored
file(s) the hub cannot restore` line then counts only the other files. The `--notify-url`
payload is unchanged.

## Files the hub deleted on purpose

A game turned off for a console (its whitelist line removed or an exclude added, so its files are
ignored there and wait for prune) and then deleted from the hub in the
[library UI](/warpzone/development/design/library-ui/#deleting-a-game) never receives the
deletion, because the path is ignored on the console, and rule 4a cannot hold: the hub has no
copy, so the file would be kept as "the hub does not have it" for ever. Rule 4b closes that gap
with the hub's own
[record of deleted games](/warpzone/development/design/library-ui/#the-record-of-deleted-games),
`patterns-_deleted.stignore` in the root of the library folder, which Syncthing delivers to
every console like the whitelists.

How the console uses it (`prune.read_record`, the only I/O that reads it, for `wz-library` only
and after the full scan):

- No file is the same as an empty record. A file that is not in sync with the hub, whose size on
  disk is not what Syncthing recorded, that is a link, that changes while it is read or that
  `trash_record.parse` does not accept (it returns `None`, never raises) authorises nothing.
- `trash_record.index` is applied with the current time, so an entry past its `expires` (180 days
  after the deletion) authorises nothing, even though it may still be in the file.
- The record alone never proves that the hub no longer has a file: it can briefly list a path that
  exists again, and the record and the hub's file index reach the console in no guaranteed order.
  So the console checks the hub's own state for that path, Syncthing's `global` record, which must
  be deleted. Until both have arrived the file is kept, as before, and the next run decides.
- `prune.classify` (pure) decides 4b without hashing and without I/O, from the `db/file` answer,
  the file on disk and the record's files for that path (`prune.record_matches`: same size,
  modification time within 2 s). `prune.sha1_ok` (pure) compares a record file's `sha1`, when it
  has one, with a digest.
- Hashing happens only in `apply_folder`, right before each delete and only when no matching entry
  lacks a `sha1`, in chunks of 1 MiB. A report never hashes, and neither does a run an unattended
  guard or the size cap refused. A file whose size or modification time changes while it is
  hashed is kept. After hashing, `record_unchanged`, `global` and the file's identity are checked
  once more before the unlink.

What is deleted and what is not:

- A file removed from the hub by hand, with `rm` or in the hub's file manager, is never enough:
  the hub's own deletion record says only that the hub lacks the file, which a wrong path, a
  failed mount or a mistake looks the same as. Only the library UI writes the record, so a copy
  with no entry in it stays and is reported as before.
- A copy whose size or modification time differs from the entry's, or whose content differs from
  its `sha1`, was edited on the console or is another file at the same path. It stays.
- A symlink or a directory at a recorded path stays.
- A copy whose entry expired, or whose trash entry was purged before the hub ran a version that
  writes the record, is not in the record and needs a manual delete.
- The other whitelisted folders (`wz-songs`) never use the record.

The unattended runs apply 4b too. These files count as leftovers for the half-of-device guard and
for `--max-gib`; see [Unattended runs](#unattended-runs).

## Unattended runs

A game turned off in the [library UI](/warpzone/development/design/library-ui/) must leave
the console without anyone running a command there, so each console that lists `library` runs
`prune <device> --apply --unattended` from a timer. This is the one exception to the rule that a
destructive command acts only when a person runs it with `--apply` (see
[Writing commands](/warpzone/development/design/configuration/#writing-commands)). Every safety
rule above still holds, file by file, with the re-check right before each delete. What changes:

- **No size cap.** `--max-gib` does not apply: one PS4 game is larger than the 50 GiB default.
  `--unattended` requires `--apply` and refuses `--max-gib` and `--force` (usage error).
- **The half-of-device guard.** For each folder, the bytes the run would delete (the leftovers
  and the files the hub deleted on purpose) are compared
  with the bytes of the regular files the walk found in that folder (what the device holds there,
  nested Syncthing folders excluded). When any folder would lose more than half
  (`2 * delete > held`), the run deletes nothing in any folder, prints why and exits 1.
  `prune.guard` (pure) decides it from the per-folder totals. It is what stops a whitelist that
  was emptied by mistake, or a library folder that briefly looks wrong, from wiping a console. A
  removal it refuses stays until the user runs `prune --apply` by hand, where the size cap and
  `--force` apply as usual; a device that holds a single game can only lose it that way.
- **A refusal leaves a trace.** Nobody reads the journal of the timer, so when `guard` names a
  folder the run writes `~/.local/state/warpzone/prune-guard.json` (next to the updater's
  state, mode 0600, replaced atomically through `safefile.write_file`): `version`, `time`
  (seconds since the epoch) and, per folder id, the bytes it would have deleted and the bytes the
  device holds. No file or folder names. `prune.render_guard_state` and `prune.parse_guard_state`
  are the pure ends of it; `record_guard_refusal` and `clear_guard_refusal` touch the disk. Any
  `--apply` run that goes on to settle clears the file: no folder was skipped or busy, and it
  either deleted or had nothing to delete (a manual `prune --apply` after the review does it). A
  report-only run, a busy or skipped run, and a refusal by the attended size cap leave it as it
  was. `prune_device` takes the state folder as `state_base` (the CLI passes
  `~/.local/state/warpzone`; None records nothing), and a failed write is a warning, not a
  failure of the run. `doctor` reads the file (see [Doctor](/warpzone/development/design/doctor/)).
  `health` does not: it decides whether a version works, and a refusal says nothing about that.
- **Busy is not a failure.** A full scan that gets no answer in time ("rescan queued", see
  [Syncthing API surface](/warpzone/development/design/code-and-testing/#syncthing-api-surface-used))
  means the device is still pulling: the run deletes nothing and exits 0, and the next tick tries
  again. Every other unsafe folder (no marker, bootstrap ignores, whitelists not in sync) still
  exits 1, so the unit shows as failed.
- **Output.** Only the summary lines; `--list` is ignored. `--notify-url` works as for any run.

The timer is installed by `provision device` for a device that lists `library`, unless its
config sets `auto_prune = false`: `~/.config/systemd/user/warpzone-prune.service` (`Type=oneshot`,
`ExecStart=<python> -m warpzone prune <device> --apply --unattended`, naming `--config` only when
it is not the default) and `warpzone-prune.timer` (`OnBootSec=15min`, `OnUnitActiveSec=1h`),
written and started like the [artwork timer](/warpzone/development/design/frontend-artwork/)
(rewritten only when their text differs, the same import check of the Python, the user manager
running without a login session). With `auto_prune = false` the units are removed if they exist.
