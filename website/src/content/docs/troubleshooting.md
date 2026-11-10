---
title: "Troubleshooting"
description: "Symptoms, causes and fixes."
---

Symptoms, causes and fixes, grouped by where the problem shows up. When you are not sure where to start, run `warpzone doctor` on the machine that has the problem: it prints a fix for every `[!!]` and `[! ]` line ([the doctor guide](/warpzone/operation/doctor/)). On a console, `warpzone status` shows each `wz-*` folder, how much is outstanding (`need N`) and any errors.

## Syncthing and whitelists

### A game removed from a whitelist is still on the console

**Cause.** Removing a line from a whitelist only makes Syncthing stop syncing that path. The copy on the console is kept, on purpose: a whitelist edit can never delete anything by itself.

**Fix.** To reclaim the space, run `warpzone prune` on the console and, when the report looks right, `warpzone prune --apply` ([Whitelists and pruning](/warpzone/setup/whitelists/#pruning-what-a-device-no-longer-whitelists)). It deletes only files Syncthing ignores and the hub still has, unchanged.

To delete a game everywhere, do it in this order:

1. Delete the game from the hub.
2. Wait until every console shows `need 0` for that folder in `warpzone status` (run it on each console). The deletion reaches a console only while the game is still whitelisted there.
3. Remove the whitelist line.

If you remove the line first, the console keeps its copy, and `prune` will report it as a file the hub cannot restore and leave it alone. Delete it by hand in that case.

### A new whitelist line has no effect

**Cause.** Syncthing applies ignore patterns only on a full scan of the folder, and a whitelist file that has just synced does not reliably start one.

**Fix.** Wait: `warpzone` gives whitelisted device folders a short rescan interval, so an edit takes effect within 5 minutes by default. To apply it at once, press Rescan on the folder in the console's Syncthing GUI. To change the interval, set `rescan_seconds` for the device in the config (minimum 5). See [When an edit takes effect](/warpzone/setup/whitelists/#when-an-edit-takes-effect).

### A folder is refused because its `.stignore` includes a file that has not arrived

**Cause.** A device's `.stignore` is a single `#include patterns-<device>.stignore` line, and Syncthing refuses to sync a folder whose `.stignore` includes a file that is not there yet. On a fresh device nothing has arrived.

**Fix.** Do not write that `#include` by hand on a new folder. `warpzone provision device <name>` creates the folder with ignores that let only the whitelists through, waits for `patterns-<device>.stignore` and `patterns-common.stignore` to arrive, and only then switches to the `#include` ([Bootstrap on a device](/warpzone/setup/whitelists/#bootstrap-on-a-device)). If a whitelist is missing on the hub, the command stops and changes nothing on the device, with a message like `wz-library: patterns-<device>.stignore not found on the hub. Create it in the folder on the hub (see templates/), let the hub scan, and try again.` Create the file on the hub, let the hub scan, and run the command again. `warpzone check` lists missing whitelists (`MISSING ... (in <path>)`) without changing anything.

### A save folder syncs nothing and shows no error

**Cause.** Syncthing never follows symlinks. EmuDeck exposes saves under `~/Emulation/saves` through symlinks, so a folder pointed at the link looks empty and syncs nothing.

**Fix.** Point the folder at the real directory of the emulator. The shipped profile already does; if you write a profile for another setup, resolve the links first with `readlink -f`. See [the symlink gotcha](/warpzone/guides/emudeck/#the-symlink-gotcha).

### On SteamOS the saves of some emulators are not synced

**Symptom.** `warpzone doctor` reports `Flatpak access`: `the Syncthing flatpak cannot see ... flatpak hides other apps' data folders, so those save folders look empty and sync nothing`.

**Cause.** Syncthing runs as the SyncThingy flatpak, which cannot see the data folders of other flatpaks under `~/.var/app` until it is granted them.

**Fix.** Run `warpzone provision device <name>`, which makes the grant, or run the `flatpak override --user ...` command that `doctor` prints, then restart Syncthing. See [the EmuDeck guide](/warpzone/guides/emudeck/#flatpak-access-to-the-emulators-folders).

### `doctor` says the Syncthing REST API does not answer

**Cause.** Either Syncthing is not running, or the port belongs to something else. `doctor` says which: `the REST API at <url> does not answer` means it is probably not running, and `answered HTTP <code>` means the API key in `config.xml` was refused or another program owns that port.

**Fix.** Start Syncthing (the `fix:` line of `doctor` gives the command for your platform), or restart it and check its GUI address. On SteamOS, `doctor` also checks that Syncthing runs in Game Mode, which needs an enabled systemd user unit; see [Syncthing on the console](/warpzone/guides/emudeck/#syncthing-on-the-console).

### The Syncthing unit keeps restarting

**Symptom.** `doctor` warns `syncthing.service is activating (auto-restart): it keeps starting and failing, usually because another Syncthing already holds the port`.

**Cause.** An older Syncthing is still running. With the SyncThingy flatpak, `flatpak run` starts Syncthing in a scope of its own, so `systemctl --user stop` leaves the old process running.

**Fix.** Stop the old process, with `flatpak kill com.github.zocker_160.SyncThingy` for the flatpak (or find and stop the other Syncthing otherwise; `journalctl --user -u syncthing -n 20` shows the error). Then make the unit stop the flatpak too, with the `ExecStop` line from [the EmuDeck guide](/warpzone/guides/emudeck/#syncthing-on-the-console), and run `systemctl --user daemon-reload` and `systemctl --user restart syncthing`.

### `provision` refuses because a folder has another path

**Symptom.** `Syncthing has it at '<path>' but the config says '<path>'. Changing a folder path would look like a mass delete, so warpzone refuses.`

**Cause.** The `wz-*` folder already exists in Syncthing with a different path than the config. Changing a folder's path would make Syncthing see a mass delete.

**Fix.** Remove the folder in Syncthing (the files stay on disk), then run the provision command again. See [Safety](/warpzone/operation/safety/). If the message ends with `move them into ...`, the profile moved a save folder and the old directory still holds files: move them into the new directory first, then provision again; no manual removal is needed.

### `prune` deletes less than expected, or refuses

**Cause and fix.** `prune` is conservative by design:

- A file that exists only on the device, or that you edited, is reported as `kept ... the hub cannot restore` and never deleted.
- It refuses to delete more than `--max-gib` (default 50) in one run; pass `--force` if the number is right.
- It refuses to run while a device is still bootstrapping. Let `provision device` finish first.
- Without `--apply` it only reports.

### A receive-only folder reports local changes

**Symptom.** `warpzone status` prints `N local changes` for a receive-only folder.

**Cause.** Files exist on the console that are not on the hub, such as files EmuDeck creates. The shipped profile ignores the known ones.

**Fix.** Run `warpzone provision device <name>` again after upgrading, to apply a changed profile ([EmuDeck's own files](/warpzone/guides/emudeck/#emudecks-own-files)). Anything else is a file that is only on the console; look at it before deciding whether it belongs on the hub.

## Adding games

### An item went to `inbox/_revisar/`

**Cause.** `warpzone inbox` could not place it safely. Next to the item is a `<item>.reason.txt` that says why: no DAT match, an incomplete multi-disc game, a conversion or verify failure, a changed RetroAchievements hash, a conflict with a different file of the same name, or a missing tool. `inbox` exits `1` when any item needs review.

**Fix.** Read the reason and act on it, then drop the item in `inbox/` again. Nothing is ever overwritten, so retrying is safe. See [Adding games](/warpzone/setup/inbox/).

### The inbox does not support the format

**Symptom.** The reason is `not supported by inbox yet (<kind>: <file>)`, or a message about `.xci`, an encrypted 3DS image or a Vita update.

**Cause.** Some formats are not handled yet. The inbox sends these to review instead of guessing: `.vpk` files, Vita `.pkg` files that need installing, PS3 or Xbox games kept as extracted folders, and PS3 `.pkg` files. Others go to review with an instruction: `.xci` and `.xcz` are never converted, an encrypted 3DS image has to be decrypted first, and a full Redump-style Xbox image has to be converted to XISO.

**Fix.** Convert the dump to a supported form (the [inbox page](/warpzone/setup/inbox/) lists what each system accepts) and drop it again. The original stays in `_revisar/` until you remove it.

### The inbox says nothing to do, or skips an item

**Cause.** Items modified in the last 30 seconds are left alone because they may still be copying. Folders that start with `_`, hidden files, `@eaDir` and `.DS_Store` are never items.

**Fix.** Wait and run it again.

### A tool is missing

**Symptom.** An item goes to review with `needs bsdtar to unpack (not found)`, or `doctor` shows `[!!] bsdtar not found (required by inbox)`.

**Fix.** Run `warpzone doctor`: it lists the inbox tools (`chdman`, `bsdtar`, `RAHasher`, `dolphin-tool`, `nsz`, `orbis-pkg-util`, `Vita3K`) and prints the install command for your platform. A tool that is not on `PATH` can be named in the `[inbox]` table of the config.

### Arcade zips go to review with "no DAT match"

**Cause.** Arcade sets are identified against MAME, FinalBurn Neo or MAME 2003-Plus databases in XML, and none is configured.

**Fix.** Set at least one of `mame_dat`, `fbneo_dat` and `mame2003plus_dat` in `[inbox]` ([Arcade sets](/warpzone/setup/inbox/#arcade-sets)). `doctor` shows whether they are set.

### The game was placed but is not on the console

**Cause.** `warpzone` never edits whitelists. `inbox --apply` prints the whitelist lines for what it placed, and the console receives only what its whitelist allows.

**Fix.** Add the printed `!/roms/...` lines to the device's whitelist on the hub and wait for the next scan (see above).

### An ES-DE override does not stick

**Cause.** ES-DE keeps its gamelist in memory and rewrites it when a game is launched, so an edit made on the hub while ES-DE is open on a console is overwritten.

**Fix.** Quit ES-DE on every console and run `warpzone inbox --apply` again ([Gamelist edits](/warpzone/setup/inbox/#gamelist-edits)).

## Consoles and emulators

### A PS Vita game does not start in Vita3K

**Symptom.** Vita3K's log says `[decrypt_fself]: No klic provided for encrypted App` and `Failed to start game threads`.

**Cause.** The game's `eboot.bin` is encrypted, and Vita3K runs it only with the game's licence, a 512-byte `.rif` file at `ux0/license/<TITLEID>/<CONTENT_ID>.rif`.

**Fix.** Give `warpzone inbox` the licence along with the dump: either as `sce_sys/package/work.bin` inside the game (NoNpDrm), or as a `<CONTENT_ID>.rif` in the item, in `license/<TITLEID>/` or next to the game folder. The inbox checks it and files it as `library/roms/psvita/license/<TITLEID>/<CONTENT_ID>.rif`; the licence of a game that is already in the library is added when missing. Whitelist `!/roms/psvita/license/<TITLEID>`, and the console links that folder into Vita3K's `ux0/license` (`warpzone links sync <name>`, with Vita3K closed). A game that needs a licence and has none goes to review instead of being placed. See [PS Vita games](/warpzone/setup/inbox/#ps-vita-games) and [Vita3K games](/warpzone/guides/emudeck/#vita3k-games).

A dump that holds `sce_module/steroid.suprx` (a Vitamin dump) always goes to review: Vita3K rejects that module, so it is never placed. Get a clean dump instead, a `.pkg` with its `work.bin` or a NoNpDrm dump.

### A Vita game is in the library but not in Vita3K's list

**Cause.** Vita3K reads games from its own storage (`ux0/app/<TITLEID>`), not from the library. The profile links each game of `roms/psvita/app` there.

**Fix.** Run `warpzone links sync <name>` after closing Vita3K (`--dry-run` shows what it would do). Nothing is changed while Vita3K runs, and if `ux0/app` does not exist yet, start Vita3K once and run the command again. `doctor` reports `Vita3K game links` and `Vita3K license links`, including dangling links and a missing startup script. A real folder already in `ux0/app` or `ux0/license` is never replaced by a link.

### Vita3K saves are not synced

**Cause.** The profile syncs the saves of user `00`, and a user created in Vita3K's window can get another id. `doctor` warns `the active user is <id>, but warpzone syncs the saves of user 00`.

**Fix.** Close Vita3K, switch to user `00` (or delete the other user), and move `ux0/user/<id>/savedata` to `ux0/user/00/savedata` if the saves are there. See [Vita3K syncs user 00](/warpzone/guides/emudeck/#vita3k-syncs-user-00).

### Dreamcast saves are not synced

**Cause.** RetroArch's Flycast core keeps its VMUs in `bios/dc/` unless per-game VMUs are on, and warpzone does not sync that folder. `doctor` warns that `Flycast's per-game VMUs are off`.

**Fix.** Change the core option as the `doctor` fix says (it names the menu path), keeping `VMU A1` or `All VMUs`. See [Dreamcast saves](/warpzone/guides/emudeck/#dreamcast-saves-retroarch-flycast).

### EmuDeck undid a launcher or a setting

**Cause.** EmuDeck can rewrite files that warpzone installs, such as ES-DE's `es_find_rules.xml` entry for shadPS4, on an update.

**Fix.** `doctor` shows the check that fails (for example `shadPS4 launcher` or `ES-DE startup script`). Run `warpzone provision device <name>` with ES-DE closed. For emulator settings that do not apply or capture as expected, see [Emulator settings](/warpzone/setup/settings/).

## Updates

The log of the updater is `~/.local/state/warpzone/self-update.log` (under `$XDG_STATE_HOME` when set), and the output of its last check is `~/.local/state/warpzone/updater/last-verify.log`. The timer's own output is in the journal: `journalctl --user -u warpzone-self-update`. See [Updating a device](/warpzone/operation/self-update/).

### A release was marked bad and the device went back

**Symptom.** `doctor` warns `rolled back from a release that did not work: <tag> (<reason>). It will not try that release again`.

**Cause.** The release failed its check (it must import, pass `provision device --unattended` and pass `warpzone health`) and the previous version passed. It is not tried again. If the previous version fails too, the environment is at fault, not the release, and the tag is not marked bad: the device waits 1, 2, 4 and then 7 days.

**Fix.** Read `self-update.log` and `last-verify.log` for the reason. A newer release is picked up automatically. To try the same tag again, remove its file in `~/.local/state/warpzone/updater/bad/`. You can run the same check by hand with `warpzone health <device>`.

### The updater says it is stuck, dirty or dev

**Cause.** `stuck` means it could not put the last working version back and changed nothing else. `dirty` means the clone has changes to tracked files, and `dev` means it has commits of its own or is not on a release. The updater never discards your work, so it leaves such a checkout alone.

**Fix.** For `stuck`, look at the clone with git, put it on a release tag by hand, then run `warpzone self-update`. For `dirty` and `dev`, commit or discard the changes, or put the clone on a release tag.

### The update waits, or `provision device --unattended` exits 3

**Cause.** The updater defers while ES-DE is running, for at most 72 hours from the first deferral. Likewise, `provision device --unattended` exits `3` when something was left for later because ES-DE, an emulator or a file ES-DE rewrites was open.

**Fix.** Close the frontend and the emulator, then run `warpzone self-update` or the same `provision device` command again. See [Unattended provisioning](/warpzone/operation/self-update/#unattended-provisioning).

### The device is not updating

**Cause.** `doctor` names it: the timer is not installed, not enabled or not running (`this device is only updated by hand`), the live copy of the updater or its settings are missing or differ from the config, no Python 3.11 or newer was found, or there has been no successful run for 14 days.

**Fix.** Run `warpzone provision device <name>`, which rewrites the units, the live copy and the settings, or enable the timer with `systemctl --user enable --now warpzone-self-update.timer`. For no Python, install 3.11 or newer and run `warpzone self-update`. A device with `self_update = false` is never moved; see [Turning it off](/warpzone/operation/self-update/#turning-it-off).

### A pin keeps the device on an old release

**Cause.** With `pin` set under `[devices.<name>]`, the pinned tag is the target whatever the channel. If that release is retired, `doctor` warns that `the pinned release <tag> has been retired, and the pin keeps the device on it`.

**Fix.** Change or remove `pin` in the config, then run `warpzone provision device <name>`. See [Channel and pin](/warpzone/operation/self-update/#channel-and-pin).

### Python was replaced by an operating system update

**Cause.** The venv's interpreter no longer runs.

**Fix.** Nothing, normally: at the next tick the updater makes the venv again with another Python 3.11 or newer, if one is installed. It leaves a venv it did not make, and logs why. See [Python repair](/warpzone/operation/self-update/#python-repair).
