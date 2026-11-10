---
title: "EmuDeck and the steamos-emudeck profile"
---

What the shipped profile covers, why save folders point at real directories, and the few steps that stay manual.

A profile says where things live on one kind of device: [`warpzone/data/profiles/`](https://github.com/dagonco/warpzone/tree/main/warpzone/data/profiles). warpzone ships `steamos-emudeck` (Steam Deck, Steam Machine) and `anbernic-rg351mp` (see [the Anbernic guide](/warpzone/guides/anbernic/)). Profiles are plain TOML, so adding a setup is a small pull request. A save entry like `[saves.dolphin-gc]` maps to the folder ID `wz-save-dolphin-gc` and the hub path `saves/dolphin/gc`. A `[bios.cemu-keys]` placement maps to `wz-bios-cemu-keys`.

## Saves covered

RetroArch, Dolphin, PrimeHack (GC, Wii, states), DuckStation, PCSX2, melonDS, PPSSPP (saves, states), RPCS3 (savedata, trophy), Vita3K, MAME, RMG, ScummVM, Eden and Ryujinx (including the Switch user profiles, so a save keeps its owner on every console), Azahar, shadPS4, Cemu, Xenia (the Proton build or the native one), Flycast (only `vmu_save_*.bin` and `nvmem` files, never the BIOS next to them) and YARG (settings, scores, profiles, playlists and custom content; never `songcache.bin` or `badsongs.txt`).

YARG syncs `custom/` in lowercase, as YARG on Linux writes it, and ignores the `*.bak` files it leaves in `profiles/`. If the hub holds the content under `saves/yarg/data/Custom`, rename it to `custom`.

YARG's data comes with the `songs` group, not with `saves`: a console that lists `saves` but not `songs` gets nothing of YARG. If you stop listing `songs` on a console that had YARG, the next `warpzone provision hub` stops sharing `wz-save-yarg-data` and `wz-songs` with it; no file is deleted on either side. Remove the folders in the console's Syncthing (the files stay) so the hub stops showing them as offers.

## Syncthing on the console

On SteamOS the Syncthing that warpzone configures is usually the SyncThingy flatpak (`com.github.zocker_160.SyncThingy`). To keep it running in Game Mode, start it from a systemd user unit, `~/.config/systemd/user/syncthing.service`:

```
[Unit]
Description=Syncthing

[Service]
ExecStart=/usr/bin/flatpak run --command=syncthing com.github.zocker_160.SyncThingy serve --no-browser --no-restart
ExecStop=/usr/bin/flatpak kill com.github.zocker_160.SyncThingy
Restart=on-failure

[Install]
WantedBy=default.target
```

Then `systemctl --user daemon-reload` and `systemctl --user enable --now syncthing.service`.

`ExecStop` is not optional. `flatpak run` puts Syncthing in a scope of its own, outside the unit, so `systemctl --user stop syncthing` does not stop it. Without `ExecStop`, a restart leaves the old process holding the port and the new one loops in `activating (auto-restart)`. If you are already in that state, run `flatpak kill com.github.zocker_160.SyncThingy`, fix the unit and restart it. `warpzone doctor` warns when the unit is in that loop.

### Flatpak access to the emulators' folders

A flatpak cannot see the data folder of another flatpak: `~/.var/app` is hidden from it even with the `host` filesystem. Several save folders in the profile live there (RetroArch, Dolphin, PPSSPP, PrimeHack, Flycast), so with the SyncThingy flatpak they look empty and sync nothing, with no error.

The fix is one override per emulator, then a restart of Syncthing:

```
flatpak override --user --filesystem=~/.var/app/org.libretro.RetroArch com.github.zocker_160.SyncThingy
```

`warpzone provision device` does this for you when the local Syncthing is the SyncThingy flatpak: it adds the missing overrides for every save and BIOS folder of the profile under `~/.var/app/<app>` whose emulator is installed, leaves the ones that already exist alone, and tells you to restart Syncthing. With `--dry-run` it only prints the command. An emulator you install later is picked up the next time you run `provision device`. A native Syncthing needs none of this. `warpzone doctor` checks the overrides and prints the command for each missing one.

## shadPS4 saves

shadPS4 keeps saves per user, in `~/.local/share/shadPS4/home/<user id>/savedata/<save title id>/`, and the first user is `1000`. The profile syncs `home/1000/savedata`. Older shadPS4 releases wrote to `~/.local/share/shadPS4/savedata/`, which is the folder earlier warpzone releases synced; v0.21 writes nothing there (the folder stays empty), and EmuDeck's `~/Emulation/saves/shadps4/saves` link still points at it. The profile syncs only the new folder, not both, and only user `1000`, so keep playing as the first user.

**After upgrading warpzone**, run `warpzone provision device <name>` on each console (the updater timer does it by itself). If the console's `wz-save-shadps4-saves` folder is still at the old path, and that folder holds nothing, provision removes it from Syncthing and adds it again at the new path. It touches no file: removing a folder in Syncthing deletes nothing on disk. A console that has not written a save yet has no `home/1000/savedata` and keeps the old folder until it does; open a game once, then provision again. If the old folder holds files, provision moves it only when all of them are already on the hub, which is the usual case for the second console: the first one uploaded its saves from the new folder, and this one downloaded them into the old one. The folder is then removed and added again at the new path, nothing is deleted, and the saves arrive in `home/1000/savedata` by themselves; the copy in the old `savedata/` stays and can be deleted once the games load. If some of the files are not on the hub yet (saves from an older shadPS4, or a sync still running), provision refuses and changes nothing. Do not move files out of a folder Syncthing still syncs: it would announce them as deletions to the hub and to the other console. Wait for the sync and run provision again, or remove (or pause) the folder in Syncthing first (the files stay on disk), then move the files into `home/1000/savedata/` and run provision again.

**When both consoles already have saves in the new folder**, deal with it before the upgrade reaches the second one (a console with the updater timer provisions by itself). Once the folder is added at the new path, Syncthing merges the copies of the two consoles. A save that exists on one console only is copied to the other. A file with the same name on both and different content becomes a conflict: the newer one wins and the other is kept next to it as `*.sync-conflict-*`. Conflicts are resolved file by file, not game by game, so a game you do not move aside can end up with files from both consoles mixed (the newer `userdata0000` next to an older `backup0000`, say), which may not load. That happens when both consoles played the same game. So, for each game both have (compare `ls ~/.local/share/shadPS4/home/1000/savedata` on the two), pick the console whose progress you want to keep, and on the other one close shadPS4 and move that game's folder aside, for example `mv ~/.local/share/shadPS4/home/1000/savedata/<save title id> ~/shadps4-saves-aside/`. Provision, wait for the sync, and check that the game loads with the progress you expect before deleting the copy you moved aside.

## The symlink gotcha

EmuDeck exposes saves through symlinks under `~/Emulation/saves`. Syncthing never follows symlinks, so a folder pointed at the symlink syncs nothing and gives no error. The profile therefore points at the real directory of each emulator (for example RetroArch's saves under `~/.var/app/org.libretro.RetroArch/`). If you add a profile for another setup, resolve the links first (`readlink -f`).

Some save folders sit inside `~/Emulation`, which is the library root. That is fine: the library whitelist ignores them, and Syncthing supports nested folders when the parent ignores the child.

The same trap exists in `~/Emulation/bios`. EmuDeck turns `eden/keys`, `eden/firmware`, `azahar/keys`, `ryujinx/keys`, `flycast/bios` and `shadps4/sys_modules` into symlinks, while the hub's `library/bios/` holds real folders at those paths. The profile therefore makes the device's library ignore those paths, ahead of your whitelist so an edit cannot bring them back. It also ignores `roms/wiiu/mlc01`, where Cemu keeps its own installs and saves (its saves sync separately).

## shadPS4 install directory

shadPS4 has no package installer: it runs extracted games from its install directories. `warpzone inbox` extracts PS4 `.pkg` files into `library/roms/ps4/games/<CUSAnnnnn>/` and writes a shortcut in `roms/ps4/shortcuts/` that ES-DE lists ([Adding games](/warpzone/setup/inbox/#ps4-games)). For a console to find those folders, `~/Emulation/roms/ps4/games` must be one of shadPS4's install directories. The profile declares it (`[shadps4]`), and `warpzone provision device <name>` adds it to `~/.local/share/shadPS4/config.json` (shadPS4 v0.21 and later, `General.install_dirs`) and to `config.toml` (`GUI.installDirs`) when they exist, keeping the entries already there (EmuDeck's `~/Emulation/storage/shadps4/games` stays). It backs the file up first and skips it while shadPS4, its Qt launcher or the game is running, so close them and run `provision device` again if it says so. `warpzone doctor` flags a console whose config lacks the folder. If shadPS4 has never run there is no config yet: start it once, then provision.

shadPS4 reads DLC from a single folder (`General.addon_install_dir` in `config.json`, `GUI.addonInstallDir` in `config.toml`), laid out as `<title id>/<entry>/`. The profile points it at `~/Emulation/roms/ps4/dlc`, where `warpzone inbox` files PS4 DLC, and `provision device` sets it in the same edit and backup. Unlike the install directories this setting holds one folder, so it is replaced: if it pointed somewhere else that holds files, DLC you installed there by hand is no longer seen, and the run warns about it (nothing is moved or deleted). `warpzone doctor` flags a config whose DLC folder is not that one.

The shortcut and the version: EmuDeck's `shadps4.sh` takes the quoted path in the `Exec=` line and starts `Shadps4-qt.AppImage -d -g <path>`; `-d` means "the emulator version selected in the Qt launcher", so choose the version there once. With `[shadps4.launch]` ES-DE runs warpzone's launch script instead ([below](#shadps4-launch-script)), which still needs a version selected or downloaded in the Qt launcher. Add the whitelist lines that `inbox` prints (`!/roms/ps4/games/<CUSAnnnnn>` and the shortcut) to the console's whitelist.

### shadPS4 launch script

EmuDeck's launcher can crash: `Shadps4-qt.AppImage -d -g <eboot>` exits with 139 on some consoles, while `Shadps4-sdl.AppImage -g <eboot>` from the version folder runs fine. EmuDeck overwrites `shadps4.sh`, so warpzone does not edit it. A profile with `[shadps4.launch]` (`qt_ini`, `versions`, `script`, `find_rules`, `emulator`) makes `warpzone provision device <name>` do two things:

- Write `~/.local/share/warpzone/shadps4-launch.sh` (bash, outside `~/ES-DE`, so it never syncs and works when warpzone cannot import). Given a `.desktop` shortcut it takes the quoted path of the `Exec=` line, finds the emulator (`versionSelected` in `~/.local/share/shadPS4QtLauncher/qt_ui.ini`, which is the AppImage or its folder; if that is empty or gone, the first `path` of `versions.json`) and runs `exec <Shadps4-sdl.AppImage> -f true -g <eboot>`. With no version it exits `1` after saying `no shadPS4 version found: open the shadPS4 Qt launcher, go to Version Manager, then download and select a version`. ES-DE shows nothing of stderr, so the script also shows that message in a dialog (`zenity`, else `kdialog`, else `notify-send`, whichever the console has; it closes by itself after a minute) and appends it with the time to `~/.local/state/warpzone/shadps4-launch.log`. `warpzone doctor` (`shadPS4 version`) reports the same gap before you try a game: a warning while the console has no PS4 games, a problem once it has.
- Point the `SHADPS4` entry of `~/ES-DE/custom_systems/es_find_rules.xml` at that script. Only the text of that `<entry>` changes, every other byte is kept, the old file is backed up, and nothing is written while ES-DE runs (it is skipped with a warning; a missing file is never created). A second run changes nothing.

EmuDeck may rewrite `es_find_rules.xml` on an update. `warpzone doctor` (`shadPS4 launcher`) warns when the entry points somewhere else, and `provision device` fixes it again. `custom_systems` is not part of `wz-esde` (its whitelist only lets `gamelists` and `collections` through), so the file is local to each console and the script path is the same on every console because it is derived from the home directory.

## Cemu paths

Cemu keeps its saves below its MLC folder, `mlc_path` in `~/.config/Cemu/settings.xml`, and the profile syncs `~/Emulation/roms/wiiu/mlc01/usr/save`. EmuDeck sets `mlc_path` with a tool that fails on SteamOS 3.9, which leaves it empty: Cemu then saves in `~/.local/share/Cemu/mlc01`, which nothing syncs. The profile declares the paths (`[cemu]`), and `warpzone provision device <name>` sets `mlc_path` to `~/Emulation/roms/wiiu/mlc01` and adds `~/Emulation/roms/wiiu/roms` to `GamePaths` (Cemu's own game list; ES-DE does not need it), keeping the rest of the file and the game paths already there. It backs the file up first and skips it while Cemu is running, so close Cemu and run `provision device` again if it says so. If Cemu has never run there is no `settings.xml`: start Cemu once, finish its first-start dialog (it offers the community graphic packs, which include the title screen fix for New Super Mario Bros. U and New Super Luigi U), close it and provision again. If the folder Cemu used until then already holds game saves (files under `usr/save/00050000/` or `usr/save/00050002/`; the Mii Maker and account files Cemu creates on every start do not count), `mlc_path` is left alone with a warning, and `doctor` gives the same advice: move those saves into `~/Emulation/roms/wiiu/mlc01/usr/save` with Cemu closed and run `provision device` again. Titles, updates and DLC installed in the old folder's `usr/title` are not seen after the switch either; reinstall them or move them too. `warpzone doctor` flags a console whose paths differ.

## Vita3K games

`warpzone inbox` files a decrypted Vita dump in `library/roms/psvita/app/<TITLEID>/` with a `roms/psvita/<Title>.psvita` file that holds the title id ([Adding games](/warpzone/setup/inbox/#ps-vita-games)). ES-DE lists the `.psvita` file, and EmuDeck's `vita3k.sh` reads the title id from it and starts `Vita3K -Fr <TITLEID>`. Vita3K looks for the game in its own storage, `~/Emulation/storage/Vita3K/ux0/app/<TITLEID>`, which is not the library folder. The profile declares the bridge (`[links.vita3k]`: `source`, `target`, `process`, `match`, `require`), and one command keeps it up to date. A game whose `eboot.bin` is still encrypted also needs its licence in `ux0/license/<TITLEID>/<CONTENT_ID>.rif`, which `inbox` files in `roms/psvita/license/<TITLEID>/`; a second rule, `[links.vita3k-license]`, links that folder the same way (`noun = "license"` names it in messages; `create_target = true` makes `ux0/license` when `ux0` is there, because Vita3K only creates it when it installs a licence). Everything below applies to both rules, and `doctor` reports them as `Vita3K game links` and `Vita3K license links`:

- `warpzone provision device <name>` links every game of `roms/psvita/app` into `ux0/app`, and installs the ES-DE startup script `~/ES-DE/scripts/startup/warpzone-links.sh`, so a game that was whitelisted since the last start is linked when ES-DE starts, with no manual step. `warpzone links sync <name>` does the same by hand (`--dry-run` only says what it would do, `--quiet` prints nothing).
- A game is linked when its folder is named like a Vita title id and already holds `eboot.bin` and `sce_sys/param.sfo`, so a game that is still syncing is linked once it is complete. The link is absolute (`~/Emulation/storage/Vita3K/ux0/app/<TITLEID>` points at `~/Emulation/roms/psvita/app/<TITLEID>`).
- Only links are changed. A real folder in `ux0/app` (a game installed in Vita3K itself) is never touched and the game is not linked over it, a link that points somewhere else is left alone, and the only thing removed is a dangling link that points into the library folder (a game that was removed from the library). Removing a link never touches the game folder.
- Nothing is changed while Vita3K runs (or when that cannot be told, no `/proc`); run the command again after closing it. If `ux0/app` does not exist yet, Vita3K has never run there: start it once, then run it again.
- The startup script is bounded (`timeout 15`), discards its output and always exits `0`, like the other ES-DE scripts, so a failing warpzone never stops ES-DE.
- `warpzone doctor` (`Vita3K game links`, `Vita3K license links`) warns about library games or licences without a link, dangling links and a missing startup script.
- A licence folder that is already a real folder in `ux0/license/<TITLEID>` (one copied by hand, or installed by Vita3K) is left alone and not replaced by the link: it keeps working, and if you want the link, move that folder away first and run the command again.

Vita3K rebuilds its list of games from `ux0/app` by itself, so nothing else is written for it. This was read in Vita3K's public source, not run on a console: its game scan (`vita3k/app/src/apps_list.cpp`) lists the directories of `ux0/app` with `std::filesystem::is_directory`, which follows symbolic links, compares them (names and the write times of `sce_sys/param.sfo` and `icon0.png`) with its `apps-cache.xml`, and rescans when they differ; and launching with `-r <TITLEID>` needs the title in that list. It has no `ux0/temp` step for running a game (that folder is only used while installing). Whether Vita3K runs a game through the symbolic link on a real console, including reading `eboot.bin` and the data files, has not been tested.

## EmuDeck's own files

The profile ignores files EmuDeck creates that are not on the hub, so a receive-only console does not list them as local changes:

- `bios/PPSSPP/` and every `readme.txt` under `bios/`.
- The emulator-owned `bios/` folders `HdPacks`, `Mupen64plus`, `fbneo`, `kronos`, `same_cdi`, `ryujinx`, `shadps4` and `yuzu`.
- Each `roms/*/systeminfo.txt` and `metadata.txt`.
- RetroArch's Flycast VMUs and flash in `bios/dc/` (`vmu_save_*.bin`, `dc_nvmem.bin`), and what the same core writes next to them (`vulkan_pipeline.cache` and the `data` folder). These are ignored without `(?d)`, so a hub-side delete of `bios/dc` never takes them along. See [Dreamcast saves](#dreamcast-saves-retroarch-flycast).
- The high score list and artwork folder of RetroArch's mame2003-plus core (`bios/mame2003-plus/hiscore.dat` and `artwork/`).

EmuDeck also creates the empty folders `bios/mame/bios` and `bios/neocd`, and the mame2003-plus core creates `bios/mame2003-plus`. They are not ignored, because the hub may deliver BIOS files into them, so a console lists them as local changes until the hub has them too: create them, empty, in the hub's `bios/` folder.

These come from the profile's `[library] ignore` list. The hub's library folder ignores the hub paths of the BIOS placements, so each file belongs to one folder. A line may start with `(?d)`. Run `provision device` again after upgrading to apply a changed profile.

## Dreamcast saves (RetroArch Flycast)

By default the Flycast core in RetroArch shares up to eight VMU files (`vmu_save_A1.bin` and the rest) between all games and writes them, with the console flash `dc_nvmem.bin`, into RetroArch's system folder, `~/Emulation/bios/dc/`. That folder is inside the receive-only library, so the profile has to ignore those files (see above) and they do not sync. Without the ignore lines a Revert on the console would erase them.

To get Dreamcast saves synced, turn on the core option **Per-Game Visual Memory Units/Systems (VMU)** (shown as "Per-Game VMUs"; the key is `reicast_per_content_vmus`): start a Dreamcast game, open Quick Menu > Core Options and choose `VMU A1` (one card per game) or `All VMUs` (all eight per game). The VMUs then go to RetroArch's saves folder, which `retroarch-saves` syncs both ways. Cards already in `bios/dc/` are not migrated: every game starts with an empty one, so copy a card by hand if you want to keep it (the per-game file is named after the game's ID and the port, for example `<game id>.A1.bin`).

`warpzone doctor` warns when `Flycast.opt` exists and the option is off or missing. The standalone Flycast (`flycast-saves`) is unaffected.

## Vita3K syncs user 00

The profile syncs `~/Emulation/storage/Vita3K/ux0/user/00/savedata`. A user created in Vita3K's own window can get another id (`01`, for example), and that user's saves are then not synced. Keep the saves under user `00` on every console: use the default user, or delete the other one and keep `00`. If the saves are already under another id, close Vita3K and move `ux0/user/<id>/savedata` to `ux0/user/00/savedata`.

`warpzone doctor` reads `user-id:` in `~/.config/Vita3K/config.yml` and warns when it is not `00`.

## Xenia and Model 2 live inside `roms/`

EmuDeck keeps some emulator files inside the system folders of `~/Emulation/roms`: Xenia's content under `roms/xbox360/` and Model 2's files under `roms/model2/`. The games themselves are in the `roms/` subfolder of each.

In a device whitelist, name `xbox360/roms` and `model2/roms` rather than the whole system folder, so the emulator files stay on the device:

```
!/roms/xbox360/roms
!/roms/model2/roms
```

`warpzone inbox` already files Xbox 360 images under `library/roms/xbox360/roms/`.

## BIOS placements

Keys and firmware reach the emulators through **BIOS placements**, listed in the profile. Each is a receive-only folder `wz-bios-<emu>-<kind>` (send-only on the hub, under `library/`) that maps a hub folder under `library/bios/` to the emulator's real directory, with its own ignore list so only the intended files land. Add `bios` to the device's `folders` to opt in.

| Hub path | Lands in | Only |
|---|---|---|
| `library/bios/eden/keys` | `~/.local/share/eden/keys` | `prod.keys`, `title.keys` |
| `library/bios/eden/firmware` | `~/Emulation/storage/eden/nand/system/Contents/registered` (verify on a real device) | the folder |
| `library/bios/azahar` | `~/.local/share/azahar-emu/sysdata` | `aes_keys.txt` |
| `library/bios/cemu` | `~/.local/share/Cemu` | `keys.txt` |

A placement whose emulator is not installed is skipped with a warning, like a save folder. A profile may set `create_when` to create the missing folder when the emulator's data folder exists. Cemu's keys folder is created once `~/.config/Cemu` exists (EmuDeck writes that one, not `~/.local/share/Cemu`), so `keys.txt` arrives before Cemu first runs. Cemu needs it only for encrypted disc images (`.wud`, `.wux`), not for the `.wua` archives `inbox` files.

Because the keys now live in the emulators' folders, `warpzone bios` on a device no longer sees them in `~/Emulation/bios`. Run it against the hub's `library/` ([Checking BIOS files](/warpzone/setup/bios/)).

xemu's disk image (`xbox_hdd.qcow2`, which holds that console's Xbox saves) is deliberately not synced: it is per device, so an Xbox save stays on the console where it was made. A memory unit in xemu does not help: the games tried save to the disk image without offering it ([Profiles](/warpzone/development/design/profiles/)).

## Emulator settings

The profile also declares the settings files of thirteen emulators (language, region, graphics options) and ES-DE's scraper options, so a choice made on one console can reach the others. See [Emulator settings](/warpzone/setup/settings/).

One caveat belongs here. The xemu EEPROM is shared as a whole file, which gives every console the same Xbox serial number and signing key. A console that already had its own EEPROM may reject saves signed with the old key that sit inside its own xemu HDD image (which stays per device, as above). Leave the EEPROM out at capture if that matters ([Emulator settings](/warpzone/setup/settings/#the-xbox-eeprom-caveat)).

## Manual steps

These are done once per device, by hand.

**Ryujinx keys.** Ryujinx has no placement. Its `system/` folder would need the same `prod.keys` and `title.keys` that Eden already receives, and two Syncthing folders cannot share one hub folder cleanly (see [the design document](/warpzone/development/design/profiles/#library-exclusions-and-bios-placements)). Copy the two files from Eden's keys folder to `~/.config/Ryujinx/system/`.

**Ryujinx firmware.** Ryujinx cannot receive its firmware as files either. Install it from Ryujinx (Tools > Install Firmware), pointing at the Eden firmware folder, `~/Emulation/storage/eden/nand/system/Contents/registered`.

**RPCS3 firmware.** Install `PS3UPDAT.PUP` once from RPCS3 (File > Install Firmware). The copy in `bios/ps3/` is a staging file.
