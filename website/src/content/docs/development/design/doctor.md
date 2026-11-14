---
title: "Doctor"
---

`warpzone doctor [--config PATH]` checks the machine it runs on and prints a checklist,
in the spirit of `flutter doctor`: one line per check with a mark, a short detail and,
for every problem, the exact fix for this platform.

- **Offline and read-only.** It never installs, writes or deletes anything. The only
  network-like probe is `GET /rest/system/ping` on the local Syncthing, and only when its
  GUI address is a loopback one (a wildcard address is probed as `127.0.0.1`), with a
  2-second timeout; the key comes from the local `config.xml` and is sent as a header,
  never printed. `RA_API_KEY` is reported as "set" or "not set", nothing else.
- **Statuses.** `[ok]`; `[! ]` warning (works, worth a look: DATs older than 180 days,
  less than 20 GiB free on the hub, a tool that is on `PATH` but that `inbox` will not
  use); `[!!]` problem; `[--]` optional and not set up. Only `[!!]` counts for the exit code.
- **What is required.** Nothing is required of a feature the machine does not use. A
  missing config file or a missing `[hub]`, `[devices]` or `[inbox]` table is `[--]`, and
  the line says what the table would enable. The exception is a config with only an
  `[inbox]` table (a machine that just files games): `[hub]` and `[devices]` are then one
  `[ok]` note, "not used here", instead of two `[--]` items. A config with no `[inbox]`
  table (or no config at all) collapses the optional inbox-only items (the tools, the
  Switch `titledb_dir` and `switch_keys`, the DAT folder and arcade DATs, the hub
  folder and `RA_API_KEY`) into one `[ok]` line saying inbox is not set up on this
  machine (`collapse_inbox`, pure); items that are `[ok]`, `[! ]` or `[!!]` stay. Once an `[inbox]` table exists, `chdman`,
  `bsdtar`, `dats_dir`, the hub root with `inbox/` and `library/roms/` become required.
  `RAHasher`, `dolphin-tool` and `nsz` are always optional, and so are the Switch `titledb_dir` and `switch_keys` (a configured path that does not exist is a problem; the keys file is only checked to exist, never read). The three arcade DAT paths
  (`mame_dat`, `fbneo_dat`, `mame2003plus_dat`) are `[--]` when unset, `[!!]` when set to
  something that is not a file (`inbox` refuses to start) and `[ok]` otherwise. A config that does not load,
  a configured tool path that is not executable, a tool that cannot run, a local
  Syncthing whose API does not answer, and Python older than 3.11 are always problems.
- **Hub API.** In the config group, from the config alone (doctor never contacts the hub):
  `[ok]` for `https://` with `api_cert_sha256` ("pinned"; `warpzone check` proves the pin
  matches), for `https://` on loopback and for `https://` elsewhere without a pin (with a note
  that Syncthing's own self-signed certificate fails default verification); `[! ]` for plain
  `http://` away from loopback, because the API key crosses the network in the clear, with the
  fix "use https:// and set hub.api_cert_sha256". Plain `http://` on loopback says nothing. An
  invalid pin, or a pin with `http://`, is a config error like any other (`judge_hub_api`, pure).
- **Where inbox runs.** `warpzone inbox` runs on a computer that mounts the hub's share
  or in the hub container (see [Inbox](/warpzone/development/design/inbox/)), and with an
  `[inbox]` table `doctor` checks the way in use, found from the machine and never asked.
  The probe `Probes.in_container` is true when the runtime left a marker in the root of the
  container (`/.dockerenv` for Docker, `/run/.containerenv` for Podman); tests replace it.
  `Where inbox runs` says which way it found. On a computer the tool checks are the ones
  above (the `[inbox]` path, else `PATH`) with the install command of the platform as fix.
  In the container they differ in four ways. The tools are the image's: besides `PATH`, a
  tool is looked for where the image puts it (`IMAGE_TOOL_PATHS`, today
  `/usr/local/bin/RAHasher`; `known_tool_paths` is pure), and `RAHasher` found there with
  no `[inbox] rahasher` is `[! ]` with the line to set. A missing tool has the fix "this hub
  image does not ship it" (run inbox on a computer, or build the image with it) instead of
  an install command, since the root of a container is read-only and rebuilt from the
  image; `chdman` and `bsdtar` stay `[!!]` there as on a computer, so an image without them
  fails (the image ships both from Debian packages, in `/usr/bin`, so `PATH` finds them and
  `IMAGE_TOOL_PATHS` lists only the tools outside `PATH`). `dolphin-tool`, `nsz` and
  `orbis-pkg-util` are in `/usr/local/bin`, also on `PATH`, so they are `[ok]` with no setting;
  Vita3K is the one tool the image lacks, so it is the one line of the tools table that is
  `[--]` there with the "does not ship it" fix. The CI `image` job runs `doctor` in the built
  image and fails when a tool of the image is not `[ok]`. `Hub root for inbox` is `[! ]` when `[inbox] root` is unset but `[hub] root` is
  there, because `inbox` needs its own `root` (the same path as `[hub] root` in the
  container). `Work folder` is `[! ]` when `work_dir` is unset (`/tmp` is memory in the
  container), or when it does not exist and its parent is not a writable folder (`inbox` creates
  the folder itself, so a missing leaf under a mounted parent is `[ok]` with "created on the
  first run"), and the missing-DATs fix says to mount a folder.
  Image tools that cannot run (a wrong architecture) are `[!!]` with "build the image
  again". `chdman memory` (`check_chdman_flac`, one line in the same group, with an `[inbox]`
  table) is `[! ]` with the fix `chdman_flac = false` when `[inbox] chdman_flac` is `true` (the
  default) and inbox runs in the container or the memory available to it is below 8 GiB;
  otherwise there is no line. The memory is the cgroup limit of the process, read through
  `Probes.memory_limit` (`/sys/fs/cgroup/memory.max`, else the v1
  `memory/memory.limit_in_bytes`; `max` or a huge number is no limit, `parse_memory_limit` is
  pure), and `judge_chdman_flac` is the pure judge. See [Inbox](/warpzone/development/design/inbox/).
- **Skyscraper.** With an `[inbox]` table, two lines in the inbox tools group (see
  [Scraping](/warpzone/development/design/scrape/#doctor)): `Skyscraper` is `[ok]` with its version,
  `[--]` when not found and not configured (`inbox` then does not scrape), `[!!]` when the
  configured path does not exist, is not executable or does not run, and `[! ]` when the version is
  older than 3.21.1. `ScreenScraper account` appears only when Skyscraper is found: `[ok]` when a
  `userCreds` key is in `[screenscraper]` of its `config.ini`, `[--]` (anonymous, with the limits)
  otherwise. The value is never read; the check is a pure function of the file's lines. In the hub
  container Skyscraper is in the image and is checked like anywhere else, with its `config.ini` on the
  volume `WARPZONE_SKYSCRAPER_DIR` names (`/skyscraper`); without that volume the line is `[--]`
  ("nothing is mounted at /skyscraper") with the fix to mount it, and Skyscraper is not run.
- **Leftover folders.** On a configured device, `Leftover folders` is `[! ]` when the local
  Syncthing `config.xml` has a `wz-*` folder that none of the device's config describes (a
  group taken out of its `folders`), and says nothing otherwise. Doctor is offline, so it
  finds the device as for `settings`: the configured device whose ID is listed in
  `config.xml` and one of whose folders is there at the path the config gives it (the hub
  lists the same IDs but keeps its folders under its own root, so it gets no line). The text
  is `provision_folders.undescribed` (pure), the one `provision device` prints as a warning;
  the fix says to put the group back in `devices.<name>.folders` and run `warpzone provision
  device <name>`, or to remove the folders in the device's Syncthing (their files stay).
  Nothing in `doctor` or `health` reported such a folder before.
- **Game Mode (SteamOS).** With a local Syncthing on SteamOS, `Game Mode` says whether
  Syncthing runs without a desktop session. Game Mode starts no desktop session, so
  `~/.config/autostart/*.desktop` entries never run there; only a systemd user unit
  does. `judge_game_mode` is pure over the `.service` files of `~/.config/systemd/user`
  (an `ExecStart` that runs syncthing), the names linked in its `default.target.wants`
  (enabled) and the autostart entries (an `Exec` that runs syncthing). An enabled unit
  is `[ok]`; with an autostart entry as well it is `[! ]` (two instances compete for
  the port); an autostart entry alone, a unit that is not enabled, or nothing at all
  is `[! ]` with a fix that shows a minimal unit for the SyncThingy flatpak
  (`ExecStart=/usr/bin/flatpak run --command=syncthing com.github.zocker_160.SyncThingy serve --no-browser --no-restart`),
  `systemctl --user enable --now syncthing.service` and, when there is one, the autostart
  entry to remove. Fix lines that start with a space are code and are never wrapped.
  The unit has `ExecStop=/usr/bin/flatpak kill com.github.zocker_160.SyncThingy`: `flatpak
  run` puts the process in a scope of its own, so `systemctl --user stop` does not reach
  it. `Syncthing unit` (`judge_unit_state`, pure over `systemctl --user show -p
  ActiveState,SubState syncthing`) is `[ok]` when the unit is active, says nothing when it
  is stopped or absent, and is `[! ]` when it is `activating` or in `auto-restart` (a start
  that keeps failing, normally the old process holding the port), with the fix: `flatpak
  kill`, the unit with `ExecStop` unless the unit already has one, and a restart.
  The flatpak app id and its commands live in `warpzone/flatpak.py`.
- **Flatpak access.** With the SyncThingy flatpak as the local Syncthing (its `config.xml`
  is in the flatpak's own folder), `Flatpak access` reads `flatpak override --user --show`
  and compares it with the folders under `~/.var/app/<app>` that the configured devices'
  profiles use (every shipped profile when the config does not load), for the apps that are
  installed. Missing overrides are `[!!]`, because those save folders sync nothing; the fix
  is one `flatpak override` per app plus a restart. `granted_apps` and `missing_apps` in
  `warpzone/flatpak.py` are pure. A native Syncthing is not checked.
- **Flycast VMUs.** When RetroArch's `Flycast.opt` exists, `judge_flycast_vmus` (pure, over
  the file text) reads `reicast_per_content_vmus`: `VMU A1` and `All VMUs` are `[ok]`;
  `disabled` or a missing key is `[! ]`, because the core then writes the shared VMUs into
  `bios/dc`, which the profile's library ignores (`/bios/dc/vmu_save_*.bin`,
  `/bios/dc/dc_nvmem.bin`) keep out of sync. The fix names the core option to change.
- **Vita3K user.** When `~/.config/Vita3K/config.yml` has a `user-id:`, `judge_vita3k_user`
  (pure) is `[ok]` for `00` and `[! ]` for anything else: the profile syncs
  `ux0/user/00/savedata` only. A missing file or key says nothing.
- **Emulator settings.** On a device whose config subscribes it to `settings`. Doctor is
  offline, so it finds the device in the local Syncthing `config.xml`: the configured
  device whose ID is listed there and whose `paths.settings` is the path of the
  `wz-settings` folder (the hub lists the same IDs but keeps that folder under its own root,
  so it gets no such lines); with no local Syncthing, or no such device, the group is
  silent. The lines are `Settings layers` (the local `wz-settings` folder and its marker exist, `[!!]` if not; `common.toml` and
  the device's layer parse and validate; a conflict copy or a secret key in a layer is
  `[!!]`, an unknown emulator or file id `[! ]`); `ES-DE startup script` (present,
  executable, and its interpreter exists, `[! ]` otherwise with `warpzone provision device
  <name>` as the fix; `CustomEventScripts` not `true` in `es_settings.xml` is `[! ]` with
  `warpzone provision device <name>` run with ES-DE closed, or the menu path, as the fix); `Last settings apply` (from `last-apply.json`, whose `time`,
  `exit`, `written` and `held` fields it reads: a failed run is `[! ]` with the log path,
  held keys are `[! ]` with `warpzone settings capture` as the fix, no run yet is `[--]`).
  The script and the `CustomEventScripts` judges share the one `ES-DE startup script` line,
  which shows the text of whichever is not `[ok]`; a missing `es_settings.xml` is `[! ]` too
  (start ES-DE once). `judge_settings_layers`, `judge_esde_script`,
  `judge_event_scripts` and `judge_last_apply` are pure.
- **Frontend.** On a device whose config subscribes it to `library` and whose profile has
  `[es_systems]` or `[retroarch]`. The device is found as for `settings`, through the
  `wz-library` folder in the local Syncthing `config.xml` (its path is `paths.library`);
  any other machine, and a profile without those tables, gets no line. `ES systems
  override` is `[ok]` when the override is exactly `rewrite_system_paths` of the source;
  missing or out of date (the source changed, or someone edited it) is `[! ]`, and so is a
  missing source. `RetroArch paths` reads each file of `cfg` and is `[ok]` when every
  wanted key has its value; a key that differs is named with its current value, a missing
  file or a key present twice is `[! ]`. The fix is `warpzone provision device <name>` with
  the frontend or RetroArch closed. `judge_systems_override` and `judge_retroarch_paths`
  are pure. A device whose profile has `[artwork]` and whose config lists `frontend` gets two
  more lines. `Frontend artwork` plans the gamelists exactly as `frontend refresh` would
  (`artwork.plan_gamelists`, with the reads going through the probes) and is `[ok]` when every
  gamelist is current, `[! ]` when some are missing, unreadable or stale (named, with
  `warpzone frontend refresh <name>` as the fix, frontend closed; for an unreadable one the
  fix adds that the refresh keeps a copy first), when no scraped gamelist has
  arrived yet for the systems on the device (the sync is the thing to look at), or when the
  system list or a scraped file is unreadable. `Artwork timer` (only when the profile sets
  `refresh`) is `[ok]` when `warpzone-artwork.timer` is installed and `[! ]` otherwise.
  `judge_artwork` is pure.
  A device whose profile has `[shadps4.launch]` also gets `shadPS4 version`: the emulator the
  launch script would run, found the way the script finds it (`shadps4.select_version`, pure:
  the `versionSelected=` line of `qt_ui.ini`, then the `path` entries of `versions.json` in
  order, each an executable AppImage or a folder holding `Shadps4-sdl.AppImage`; the tests
  run the script and doctor on the same fixtures). `[ok]` names it. With none, ES-DE shows
  nothing (the script's stderr goes nowhere), so it is `[! ]` while the device holds no PS4
  games (a file in `roms/ps4/games`) and `[!!]` once it does, with the fix "open the
  shadPS4 Qt launcher, go to Version Manager, then download and select a version" and the
  log `~/.local/state/warpzone/shadps4-launch.log`. `judge_shadps4_version` is pure.
- **Unattended prune.** Group `Pruning`, on a device found through `wz-library` (as for the
  frontend) whose config subscribes it to `library` without `auto_prune = false`. One line,
  `Unattended prune`: `judge_prune_guard` (pure) is `[ok]` when `prune-guard.json` is not
  in `~/.local/state/warpzone`, and `[! ]` when it is: the folders the timer's run refused to
  touch because they would lose more than half of what the device holds, how long ago, and as
  the fix `warpzone prune <name>` to review and `warpzone prune <name> --apply` by hand. A file
  that does not parse is `[! ]` too, with the same fix, which rewrites it. The file and the
  rules that write and clear it are in [Unattended runs](/warpzone/development/design/prune/#unattended-runs).
  It is never `[!!]`: nothing is broken, a removal is waiting for a person.
- **Unattended inbox.** Group `Inbox`, with an `[inbox]` table and a hub root that can be read
  (`[inbox] root`, else `[hub] root`; without one `Hub folder` already says so), on a computer as
  well as in the container, because the state is on the share. One line, `Unattended inbox`:
  `judge_unattended_inbox` (pure, in `doctor_inbox.py` and re-exported by `doctor.py`) judges the
  state file `catalog/inbox/state.json`, the newest run record under `catalog/inbox/runs/` and
  the age of the lock `inbox/.warpzone.lock`, against the clock; the check only reads them through
  the probes (`is_file`, `read_text`, `list_dir`, `mtime`, `now`). No state file is `[--]` (no
  unattended run has recorded anything) with the fix that schedules the task, so a hub that does
  not use the feature is not alarmed. Otherwise a run-level error is `[!!]` (its message, since
  when, how many runs, and the fix its step implies); `[! ]` for a last check more than two hours
  old, held items, an item waiting more than two hours since `first`, and a lock held more than
  six hours; `[ok]` with the last check and the last run. Findings share the line, the worst
  status first. The rules are in
  [Unattended inbox](/warpzone/development/design/inbox-unattended/#doctor). `health` does not
  read it.
- **Exit codes.** `0` when no check is a problem, `1` otherwise.
- **Install hints** live in one table in `warpzone/doctor_hints.py` (the `*_HINTS` dicts, with `TOOLS` in
  `warpzone/doctor_tools.py`), keyed by platform family: `macos`, `debian`, `fedora`, `arch`, `steamos`,
  `linux` (any other Linux) and `other`, with an `any` fallback. A hint is added only
  when it is known to be right for that family. SteamOS has a read-only root, so its hints
  point at a container instead of a system package.
- **Structure.** Each check is a function of a `Context` that returns `Result` records
  (name, group, status, detail, fix). Everything it reads from the machine (`which`,
  running a tool, the environment, the home folder, the clock, free space, the HTTP probe)
  is a field of `Probes`, so the tests use fakes and no real tools. `render` and
  `exit_code` are pure. `render` wraps details and fixes to the terminal width
  (`shutil.get_terminal_size`, 100 when it is unknown, never narrower than 30 columns
  of text): continuation lines hang under the start of the detail, a fix under its text,
  and a word longer than the line (a path, a URL) is never cut. The config is read as far as it can be: the `[inbox]` values are
  used by the other checks even when another table is invalid.
