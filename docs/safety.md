# Safety

The rules `warpzone` follows so that a mistake on a console or in a command cannot damage the library.

- **Receive-only devices.** The library, songs and tools are receive-only on devices, so a delete or an edit on a console can never propagate to the hub.
- **Path changes are refused.** If a `wz-*` folder already exists in Syncthing with a different path than the config says, `warpzone` stops: changing a folder's path makes Syncthing see a mass delete. Remove the folder in Syncthing first (files stay on disk), then provision again.
- **Additive only.** Devices are only ever added, never removed. Folders that are not prefixed `wz-` are never touched. Existing `wz-*` folders are patched only for the keys `warpzone` owns (label, path, type, devices, versioning, file watcher, and the rescan interval of whitelisted device folders). Everything else you tuned is left alone.
- **Pruning is opt-in.** `warpzone prune` reports by default. `--apply` deletes only files Syncthing ignores and the hub still has, within a size cap ([whitelists.md](whitelists.md#pruning-what-a-device-no-longer-whitelists)).
- **Dry run.** `provision` supports `--dry-run`: it reads state, prints the calls it would make (method, path, JSON body) and sends none.
- **Secrets.** API keys are never printed, logged or included in error messages, and are redacted from dry-run output.
- **Where it runs.** `provision device` talks to the local Syncthing on loopback (self-signed HTTPS is accepted only there) and reads the hub's API only to check that the whitelists exist before it changes anything. If the hub is unreachable it warns and carries on, because the bootstrap ignores keep it safe.
- **Inbox never overwrites.** `warpzone inbox` reports by default, only adds files, and sends anything it cannot place to review ([inbox.md](inbox.md)).

## Snapshots on the hub

The hub is the authority, and `warpzone` keeps no versions of the library (saves get 30 days of staggered history). Turn on filesystem snapshots for the hub's games share (Btrfs on Synology, ZFS, or whatever your NAS offers). A bad edit or a deleted folder on the hub then costs one rollback instead of a re-download.
