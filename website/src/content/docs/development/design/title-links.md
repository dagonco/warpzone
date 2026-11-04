---
title: "Title links"
---

Vita3K reads games from `~/Emulation/storage/Vita3K/ux0/app/<TITLEID>`, while the library
delivers each title to `~/Emulation/roms/psvita/app/<TITLEID>` (the `inbox` placement, see
[PS Vita games](/warpzone/development/design/inbox/#ps-vita-games)). A profile declares the bridge with `[links.<id>]`:

```toml
[links.vita3k]
source = "{library}/roms/psvita/app"     # a folder per title (the library path is expanded)
target = "~/Emulation/storage/Vita3K/ux0/app"
process = ["Vita3K"]                      # nothing changes while it runs
match = 'PCS[A-I]\d{5}'                   # a title folder's whole name (default: any)
require = ["eboot.bin", "sce_sys/param.sfo"]   # files a title must already hold
noun = "game"                             # what a title is called in messages (default)
create_target = false                     # make `target` when it is missing (default false)
```

The shipped profile has a second rule for the licences of the games, `[links.vita3k-license]`:
`source = "{library}/roms/psvita/license"`, `target = "~/Emulation/storage/Vita3K/ux0/license"`,
the same `match`, no `require` (a title is a folder of `.rif` files, which have no fixed
names), `noun = "license"` and `create_target = true`. Vita3K creates `ux0/app` when it starts
but `ux0/license` only when it installs a licence (`io::init` makes `data`, `app`, `music`,
`picture`, `theme`, `video` and `user`), so with `create_target` a missing `target` is made
(one level, when its parent exists; `--dry-run` says "would create"), after the check for the
running emulator and only when there is a title to link. When the parent is missing too, Vita3K
has never run and the rule says so like for any other target. Vita3K reads the licence by path
(`ux0/license/<TITLEID>/<CONTENT_ID>.rif`), which a link to the library folder satisfies; its
"delete game" removes `ux0/license/<TITLEID>` with `remove_all`, which removes a symbolic link
and not what it points at.

`warpzone/titlelinks.py` (no network code) keeps one absolute symlink `target/<title>` ->
`source/<title>` per title:

- A title is a real folder (not a symlink) of `source` whose name fully matches `match` and that
  holds every `require` file. A game that is still syncing has no link until it is complete.
- `plan(source, ready, current)` is pure. A title without an entry is created. A real folder
  or file under that name, or a link that points elsewhere, is kept and reported. An entry that
  is a link to `source/<title>` is correct. The only thing removed is a dangling link whose
  parent is `source` (one that this module made for a game that is gone); a dangling link
  pointing anywhere else is not ours. Both lists are re-checked right before each change
  (`readlink` and `exists`), and a link that appears meanwhile (`FileExistsError`) is not an
  error.
- Nothing is changed while a process of `process` runs (`procs.running`) or when that
  cannot be told (no `/proc`); the run says so. A missing `source` is "nothing to link"; a
  missing `target` means the emulator has not run yet, and it is not created. `--dry-run`
  prints "would link" and "would remove" and changes nothing. A second run changes nothing.
- It runs from `provision device` (for a device that lists `library`; `ensure_title_links`),
  from `warpzone links sync <device> [--dry-run] [--quiet]` and from an ES-DE startup script.
  The script, `<esde>/scripts/startup/warpzone-links.sh`, is written like the other two
  (see [ES-DE startup script](/warpzone/development/design/emulator-settings/#es-de-startup-script)): the same `env -u ... timeout 15 <python>
  -m warpzone links sync '<device>' [--config ...] --quiet >/dev/null 2>&1` and `exit 0`, so
  ES-DE is never held up or stopped by a failing warpzone. The command is routed before any
  network import, like `settings apply` and `play-state sync`.
- **Doctor.** `<process> <noun> links` (`Vita3K game links`, `Vita3K license links`): a warning
  for library titles without a link, dangling links, a missing `target` (unless the rule makes
  it and its parent exists), and a missing startup script.
- **Vita3K.** From its public source, the app list (`apps_list.cpp`) is built from the
  directories of `ux0/app` with `std::filesystem::is_directory` (which follows symlinks) and
  is kept in `gui-configs/apps-cache.xml`, which is rebuilt when the directory names or the
  write times of `sce_sys/param.sfo` and `icon0.png` differ; `-r <TITLEID>` needs the title in
  that list. No cache entry or `ux0/temp` content has to be written. Running a game through a
  symlink on a real console is untested.
