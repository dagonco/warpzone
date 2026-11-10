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
  fails. `Hub root for inbox` is `[! ]` when `[inbox] root` is unset but `[hub] root` is
  there, because `inbox` needs its own `root` (the same path as `[hub] root` in the
  container). `Work folder` is `[! ]` when `work_dir` is unset (`/tmp` is memory in the
  container) or is not a mounted folder, and the missing-DATs fix says to mount a folder.
  Image tools that cannot run (a wrong architecture) are `[!!]` with "build the image
  again".
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
  gamelist is current, `[! ]` when some are missing or stale (named, with
  `warpzone frontend refresh <name>` as the fix, frontend closed), when no scraped gamelist has
  arrived yet for the systems on the device (the sync is the thing to look at), or when the
  system list or a scraped file is unreadable. `Artwork timer` (only when the profile sets
  `refresh`) is `[ok]` when `warpzone-artwork.timer` is installed and `[! ]` otherwise.
  `judge_artwork` is pure.
- **Exit codes.** `0` when no check is a problem, `1` otherwise.
- **Install hints** live in one table in `warpzone/doctor.py` (`TOOLS` and the `*_HINTS`
  dicts), keyed by platform family: `macos`, `debian`, `fedora`, `arch`, `steamos`,
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
