---
title: "Safety"
---

The rules `warpzone` follows so that a mistake on a console or in a command cannot damage the library.

- **Receive-only devices.** The library, songs and tools are receive-only on devices, so a delete or an edit on a console can never propagate to the hub.
- **Path changes are refused.** If a `wz-*` folder already exists in Syncthing with a different path than the config says, `warpzone` stops: changing a folder's path makes Syncthing see a mass delete. Remove the folder in Syncthing first (files stay on disk), then provision again. The exception is a save folder whose profile moved it (`previous_paths`) while its old directory is empty or all its files are already on the hub: provision removes and adds it by itself, since nothing on disk can be lost and no file leaves a folder Syncthing syncs. Never move files out of a folder Syncthing still syncs: it would announce them as deletions.
- **Additive only.** Devices are only ever added, never removed. Folders that are not prefixed `wz-` are never touched. Existing `wz-*` folders are patched only for the keys `warpzone` owns (label, path, type, devices, versioning, file watcher, and the rescan interval of whitelisted device folders). Everything else you tuned is left alone.
- **Pruning is report-first.** `warpzone prune` reports by default. `--apply` deletes only files Syncthing ignores and the hub still has, within a size cap ([Whitelists and pruning](/warpzone/setup/whitelists/#pruning-what-a-device-no-longer-whitelists)). A console that lists `library` also runs it from a timer, without the cap but never when it would delete more than half of what a folder holds ([Automatic pruning](/warpzone/setup/whitelists/#automatic-pruning)).
- **Dry run.** `provision` supports `--dry-run`: it reads state, prints the calls it would make (method, path, JSON body) and sends none.
- **Secrets.** API keys are never printed, logged or included in error messages, and are redacted from dry-run output.
- **Where it runs.** `provision device` talks to the local Syncthing on loopback (self-signed HTTPS is accepted only there) and reads the hub's API only to check that the whitelists exist before it changes anything. If the hub is unreachable it warns and carries on, because the bootstrap ignores keep it safe.
- **Inbox never overwrites.** `warpzone inbox` reports by default, only adds files, and sends anything it cannot place to review ([Adding games](/warpzone/setup/inbox/)).

## Snapshots on the hub

The hub is the authority, and `warpzone` keeps no versions of the library (saves get 30 days of staggered history). Turn on filesystem snapshots for the hub's games share (Btrfs on Synology, ZFS, or whatever your NAS offers). A bad edit or a deleted folder on the hub then costs one rollback instead of a re-download.
