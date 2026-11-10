---
title: "Profiles"
---

A profile describes where things live on one kind of device. Profiles are data
files shipped in the repo (`warpzone/data/profiles/<name>.toml`), because they are
the part other people can reuse and improve.

warpzone ships `steamos-emudeck` and `anbernic-rg351mp` (see
[Frontend and RetroArch overrides](#frontend-and-retroarch-overrides)). Those are the only
profiles: `profiles.available_profiles` lists the files of `warpzone/data/profiles/` and there
is no user profile folder, so a new profile is a pull request. `warpzone check` prints the
profiles the configured devices use, and a profile name the config uses that is not shipped is
refused with the list of the ones that are. Paths use `~` and are expanded on the device.

Besides `name` and `description`, a profile has the top-level flag `ignore_perms` and these
tables: `[paths]`, `[library]`, `[saves.*]`, `[bios.*]`, `[settings.*]`, `[es_systems]`,
`[retroarch]`, `[artwork]`, `[play_state]`, `[shadps4]`, `[cemu]`, `[links.*]`, `[options_menu]` and
`[updater]` (`profiles.parse_profile` refuses any other key). The shipped files are the reference; this is
the shape of the first four, and the others are described where their feature is
([Frontend and RetroArch overrides](#frontend-and-retroarch-overrides),
[shadPS4 install directories](#shadps4-install-directories), [Cemu paths](#cemu-paths), [Options menu](#options-menu), [Title links](/warpzone/development/design/title-links/),
[Frontend artwork](/warpzone/development/design/frontend-artwork/), [Play state](/warpzone/development/design/play-state/) and, for `[settings.*]`,
[Emulator settings](/warpzone/development/design/emulator-settings/#profile-entries)):

```toml
name = "steamos-emudeck"
description = "SteamOS with EmuDeck (Steam Deck, Steam Machine)"

[paths]
library = "~/Emulation"
songs = "~/Songs"
tools = "~/.local/share/warpzone/tools"
media = "~/Emulation/tools/downloaded_media"
esde = "~/ES-DE"
settings = "~/.local/share/warpzone/settings"

# Library paths the device never receives (before the whitelist).
[library]
ignore = ["/roms/wiiu/mlc01", "/bios/ryujinx/keys", "/bios/flycast/bios"]

# One entry per save folder. `path` is the REAL directory: EmuDeck exposes
# saves through symlinks under ~/Emulation/saves and Syncthing never follows
# symlinks, so pointing Syncthing at the symlink would sync nothing.
[saves.retroarch-saves]
path = "~/.var/app/org.libretro.RetroArch/config/retroarch/saves"
[saves.ryujinx-user]
path = "~/.config/Ryujinx/bis/user"
ignore = ["!/save", "!/saveMeta", "*"]   # a whitelist for this folder only

# BIOS placements: a hub folder delivered into the emulator's real directory.
[bios.cemu-keys]
hub = "bios/cemu"                         # below the hub's library/
path = "~/.local/share/Cemu"
ignore = ["!/keys.txt", "*"]
```

Save entries (what each emulator keeps, all real directories): RetroArch saves
and states; Dolphin GC, Wii and states; DuckStation, PCSX2, melonDS, MAME, RMG
(saves and states); PPSSPP (saves and states); RPCS3 (savedata and trophy);
Vita3K; ScummVM; PrimeHack (GC, Wii, states); Eden (`nand/user/save`, plus the
user profiles in `nand/system/save/8000000000000010`); Ryujinx (`bis/user`
restricted to `save` and `saveMeta`, plus the profiles in `bis/system/save`);
Azahar (sdmc, states); shadPS4 (`home/1000/savedata`, the first user); Cemu (`mlc01/usr/save`); Xenia (Proton:
`~/Emulation/roms/xbox360/content`, native: `~/.local/share/Xenia/content`;
EmuDeck installs one or the other, and each has its own hub folder);
Flycast (`vmu_save_*.bin` and the `nvmem` files only, from the folder that also
holds the standalone BIOS, which stays out); YARG, which is not EmuDeck
(`settings.json`, `scores`, `profiles`, `playlists` and `custom`, lowercase as
the Linux build writes it; never `songcache.bin` or `badsongs.txt`, which hold one
machine's absolute paths, nor the `*.bak` files in `profiles/`).
User profiles are synced so that a save keeps its owner on every console: a
Switch save belongs to a profile ID, and the same ID must exist everywhere.

The save key `<emu>-<kind>` maps to folder ID `wz-save-<emu>-<kind>` and hub
path `saves/<emu>/<kind>`. A save folder with an `ignore` list gets those
patterns (after `#include patterns-common.stignore`).

Save paths that do not exist on the device yet (the emulator was never opened,
or is not installed) are skipped with a warning, never created by warpzone, and
picked up by the next `provision` run. Skipping is per folder and never an
error, so the many entries for emulators a console does not have cost one
warning line each and nothing else. Some save folders sit inside `~/Emulation`, which is the
`wz-library` root; that is fine because the library whitelist ignores them, and
Syncthing supports nested folders when the parent ignores the child.

## Library exclusions and BIOS placements

EmuDeck replaces several folders inside `~/Emulation/bios` with symlinks to the
emulator's real directories: `eden/keys`, `eden/firmware`, `azahar/keys`,
`ryujinx/keys`, `flycast/bios`, `shadps4/sys_modules` (and the same for Citron,
Yuzu and Suyu). Syncthing never follows symlinks, and the hub's `library/bios/`
has real folders at several of those paths, so a library folder that tried to
sync them would conflict or fail. Cemu has the opposite problem: it keeps its
installs and saves in `roms/wiiu/mlc01`, which is per-device data. Two profile
tables deal with it:

- `[library] ignore`: anchored paths the device's library folder ignores. They
  are written **before** the `#include patterns-<device>.stignore` line (the
  first match wins and the whitelist ends with `*`, so nothing can un-ignore
  them), which means a whitelist edit on the hub cannot bring them back. The
  hub is not affected: it still holds and sends `roms/wiiu/mlc01` to nobody
  who whitelists it. A line may start with `(?d)` (Syncthing's "deletable"
  prefix) so the ignored file does not stop the hub's deletion of its parent
  folder. The shipped profile also lists EmuDeck's own files, which exist on
  every console and not on the hub: `bios/PPSSPP`, every `readme.txt` under `bios/`,
  the emulator-owned folders `bios/HdPacks`, `Mupen64plus`, `fbneo`, `kronos`,
  `same_cdi`, `ryujinx`, `shadps4` and `yuzu`, what RetroArch cores write into
  their system folders (Flycast's `bios/dc/vulkan_pipeline.cache` and `bios/dc/data`,
  mame2003-plus's `hiscore.dat` and `artwork`), and each
  `roms/*/systeminfo.txt` and `metadata.txt`. `bios/mame`, `bios/neocd` and
  `bios/mame2003-plus` are deliberately not ignored: a hub may deliver BIOS files
  there, so the hub holds them (empty if need be) to keep them off the local changes. Without it a receive-only library
  reports them as local changes (hundreds of files), and a revert would delete
  them. Only those files are named: no folder that holds games is ignored.
- `[bios.<emu>-<kind>]`: a **BIOS placement**. It maps a hub path below
  `library/` (`hub`) to the emulator's real directory on the device (`path`),
  through a receive-only folder `wz-bios-<emu>-<kind>` (send-only on the hub)
  whose own `ignore` list lets only the intended files through. Optional keys:
  `create_when` (create the target when that directory exists, i.e. the
  emulator is installed but never wrote this folder; otherwise a missing target
  is skipped like a save folder) and `verify = true` (the target is a best
  guess that was not confirmed on a device).

Shipped placements:

| Folder | Hub path | Device target | Delivered |
|---|---|---|---|
| `wz-bios-eden-keys` | `bios/eden/keys` | `~/.local/share/eden/keys` | `prod.keys`, `title.keys` |
| `wz-bios-eden-firmware` | `bios/eden/firmware` | `~/Emulation/storage/eden/nand/system/Contents/registered` (`verify`) | everything in the folder |
| `wz-bios-azahar-keys` | `bios/azahar` | `~/.local/share/azahar-emu/sysdata` | `aes_keys.txt` |
| `wz-bios-cemu-keys` | `bios/cemu` | `~/.local/share/Cemu` (`create_when` `~/.config/Cemu`) | `keys.txt` |

Keys travel in exactly these files and nowhere else; their content is never
printed. Eden firmware is uncertain: EmuDeck's `bios/eden/firmware` symlink
points at `~/.local/share/eden/nand/system/Contents/registered`, but the same
script sets Eden's `nand_directory` to `~/Emulation/storage/eden/nand`, so the
symlink is probably dangling and the second path is where Eden reads firmware.
Check it on a real device before relying on it.

The path of every placement is also added to the library exclusions of the
device **and of the hub's library folder**. The hub folder is then the only
owner of those files, so the same file is never in two folders on the hub, and
a device that does not subscribe to `bios` simply does not receive them from
the library either (a placement is the only way those paths travel). The hub
ignore lines come from the placements of every profile that a library
subscriber uses.

**Why nested folders.** A placement folder sits inside the hub's library
folder (`library/bios/cemu` inside `library`). Syncthing's FAQ says nesting is
"not supported, recommended or coded for, but it can be done successfully when
you know what you're doing", and the only thing to avoid is circular shares.
Making the parent ignore the child, as above, is the safe way to do it: the
parent never scans the child, so its `.stfolder` and `.stignore` markers and
its files belong to one folder only. The alternative of keeping the
placements outside `library/` (for example a top-level `bios-keys/`) would
avoid nesting but break the layout the BIOS checker and the inbox rely on
(`library/bios/...`), so it was not chosen. The same parent-ignores-child
arrangement already exists on devices, where save folders sit inside
`~/Emulation`.

**Not synced on purpose.** xemu's HDD image (`~/Emulation/storage/xemu/xbox_hdd.qcow2`)
holds that console's Xbox saves inside a single disk image that is rewritten
while the game runs, so two-way sync would corrupt it or leave conflict copies.
It is per device and has no entry, so Xbox saves do not travel between consoles. An xemu
memory unit (a raw 8 MiB FATX image attached to a controller slot) would be a small file
holding only saves, but in xemu 0.8.136 neither LEGO Star Wars nor FIFA Football 2004
offered it: both saved to the HDD image without asking, and the memory unit stayed
untouched. So there is no `saves.xemu-mu` entry. xemu's `eeprom.bin` has no folder either: it travels
as [system data](/warpzone/development/design/emulator-settings/#system-data) of the emulator settings. xemu's BIOS files
(`mcpx_1.0.bin`, `Complex_4627v1.03.bin`) are real files in `~/Emulation/bios`,
so the library delivers them as it always did.
Flycast's standalone BIOS and shadPS4's modules are not placed yet (they live
in folders the library now ignores): add a `[bios.*]` entry when a hub layout
for them exists.

**Ryujinx keys and firmware are manual on purpose.** Ryujinx reads `prod.keys`
and `title.keys` from `~/.config/Ryujinx/system`, the same two files that
`wz-bios-eden-keys` delivers from `library/bios/eden/keys`. A second placement
for Ryujinx would need a second folder ID (`wz-bios-ryujinx-keys`) over the
same hub directory, and that is not a clean arrangement: a Syncthing folder ID
is the same on both ends, so the folder cannot be shared once and received at
two paths; two hub folders over one directory would share its `.stfolder` and
`.stignore` and scan and index the same files twice, which is the overlap the
placements exist to avoid. Giving Ryujinx its own hub copy (`bios/ryujinx/keys`)
would duplicate the keys. So the [EmuDeck guide](/warpzone/guides/emudeck/#manual-steps) tells the user to copy the
two files once. Ryujinx firmware is installed from the emulator (Tools > Install
Firmware, pointing at the Eden firmware folder), not delivered as files, like
the PS3 firmware in RPCS3.

`warpzone bios` run on a device looks in the library folder, where the placed
files no longer are; run it on the hub (`--root`) for keys and firmware.

## Frontend and RetroArch overrides

The `anbernic-rg351mp` profile (an Anbernic RG351MP running dArkOS, with the ROMs on an
exFAT card at `/roms`) needs three things that `steamos-emudeck` does not. All three are
profile data, not code that tests the profile name.

- **`ignore_perms = true`** (top level). exFAT has no POSIX permissions (and no symlinks),
  so a receive-only library would list every file as a local change and the puller would
  try to `chmod`. Every folder of such a device is created with `ignorePerms: true`. The
  profile depends on no symlink: its RetroArch save folders are real directories on the root
  filesystem, its Nintendo DS saves are in a real folder on the card, and its library
  exclusions name no EmuDeck link. exFAT also compares file names
  without regard to case and forbids a few characters (`: * ? " < > |`); Syncthing reports
  a hub file that does not fit as an error on that file, so such names stay a matter for
  the hub's library.
- **`[save_convert.<key>]`** (`format`, `saves`, `path`, `process`, optional `es_system`):
  an emulator that keeps its saves in a shape the hub does not store. `saves` names the
  `[saves.*]` entry with the raw files, `path` is the emulator's own folder. With `es_system`,
  the `<command>` of that system in the override below runs `warpzone saves convert` before
  and after the original command, which keeps its bytes (`frontend.wrap_systems`, pure,
  idempotent; `doctor` compares the file without the wrapper). The rules are in
  [Save conversion](/warpzone/development/design/save-conversion/).
- **`create_when`** on a `[saves.*]` entry, with the meaning it has for `[bios.*]`: create
  the folder when that directory exists. RetroArch writes `saves/` and `states/` only on
  the first save, so without it the two folders would stay skipped until then.
- **`previous_paths`** on a `[saves.*]` entry: earlier values of `path`. When Syncthing has
  the folder at one of them, and that directory holds nothing but Syncthing's marker and
  `.stignore` (or does not exist), `provision device` removes the folder from Syncthing and
  adds it again at `path`, in the same run, unattended runs included. Removing a folder
  deletes no file, and the new folder starts from a fresh index, so nothing can be read as a
  mass delete. If the old directory holds anything, or the old path is not listed, the
  refusal for a changed path applies (`provision_folders.can_move`, `is_move`, pure apart
  from reading the directory). The new directory must exist (or be creatable with
  `create_when`); until then the old folder is left as it is. `steamos-emudeck` uses it for
  `shadps4-saves`, which moved from `~/.local/share/shadPS4/savedata` to
  `~/.local/share/shadPS4/home/1000/savedata`.
- **`[es_systems]`** (`source`, `override`, `prefix`, `process`; needs `paths.library`).
  The frontend reads `override` before `source`. `provision device` writes the override as
  the source with every `<path>` element that starts with `prefix` moved to
  `<library>/roms/`, so `<path>/roms/nes/</path>` becomes
  `<path>/roms/library/roms/nes/</path>`; every other byte (line endings, the commands,
  entries outside `prefix` such as `/opt/cmds/`) is kept (`frontend.rewrite_system_paths`,
  pure). Every system under `prefix` moves, including those the hub has no games for: the
  alternative of moving only the systems the hub has would make the override depend on the
  whitelist and go stale with every edit of it. Such a system points at a folder that stays
  empty, and the device's own `/roms/<system>` folders are not listed any more (an
  entry like `ports` also moves). The copy is made only while no process in `process`
  runs; when `/proc` is missing or the frontend runs it warns, names the command to rerun
  and writes nothing. A different existing override is backed up first (to the settings
  state folder, as for `settings apply`); a fresh one has nothing to back up; the write is
  atomic and refused when the file changed in between. A second run changes nothing.
- **`[retroarch]`** (`cfg`, `process`, `set`). Each file of `cfg` (a device may have two
  RetroArch builds with a config each) gets the keys of `set` with their values, `{library}`
  replaced by the library path: `system_directory` for BIOS files, and
  `savefiles_in_content_dir` and `savestates_in_content_dir` off with
  `savefile_directory` and `savestate_directory` pinned to the `[saves.*]` folders, so
  saves and states leave the ROM folders and can sync. The edit uses the `retroarch-cfg`
  handler of the settings layer (only the value changes, every other byte is kept), only
  while no process in `process` runs, with a backup before each edit. A missing file is
  never created (RetroArch writes it on its first start); a file where a wanted key is
  present twice or malformed is left alone with a warning. Saves already next to the ROMs
  are not moved.

Both overrides run at the end of `provision device`, for a device that subscribes to
`library`. `--dry-run` prints what would be written.

The RetroArch save layout is the same as on the Steam Deck and Steam Machine: all the
`sort_*` options are off on all three, so saves and states are flat files in one folder
named after the game (`<game>.srm`, `<game>.state`), and the hub folders are the same
`saves/retroarch/saves` and `saves/retroarch/states`. What differs is only which core wrote
a file; a state of one core does not load in another core or another version of it, which
warpzone cannot check.

## shadPS4 install directories

shadPS4 runs the extracted games it finds in its install directories, and the PS4 games that
`inbox` extracts land in `library/roms/ps4/games` ([PS4 packages](/warpzone/development/design/inbox/#ps4-packages)). A profile
with `[shadps4]` (`config`, `process`, `install_dirs`, optionally `addon_dir`) has `provision device` list that folder
there, for a device that subscribes to `library`, at the end of the same step as the other
overrides. The profile `steamos-emudeck` declares `{library}/roms/ps4/games`.

shadPS4 reads DLC from a single folder, `<addon folder>/<TITLE_ID>/<entry>/`, which is the
layout `inbox` files PS4 DLC in under `library/roms/ps4/dlc`. The optional key `addon_dir`
(same `{library}` expansion as `install_dirs`; `steamos-emudeck` declares
`{library}/roms/ps4/dlc`) makes `provision device` point that setting at it. A profile without
`addon_dir` leaves the setting alone.

- **Files.** Each path of `config` that exists is edited, chosen by its suffix. `config.json`
  (shadPS4 v0.21 and later) holds `General.install_dirs`, a list of
  `{"enabled": true, "path": "..."}`; `config.toml` (older versions, and still on disk after the
  move to JSON) holds `GUI.installDirs`, a list of strings. Both are edited because either may be
  the one a version reads. A missing file is never created (shadPS4 writes it on its first
  start); when neither exists the run warns to start shadPS4 once and run `provision` again.
- **Edit.** The existing entries stay and the missing folder is appended (a disabled entry for
  it is enabled; trailing slashes do not count as a difference). `config.json` must be
  exactly what `json.dumps(indent=2)` writes, which is shadPS4's layout, or it is left alone:
  the edit re-serialises it. In `config.toml` only the `installDirs` value is replaced (or the key
  or the `[GUI]` table added) and the result must read back as the original plus the new folders.
  Anything that does not read as expected is left alone with a warning (`shadps4.apply` is pure).
- **DLC folder.** With `addon_dir`, the same edit also sets `General.addon_install_dir`
  (`config.json`) or `GUI.addonInstallDir` (`config.toml`), a single path string that shadPS4
  defaults to `addcont` in its own user folder when empty. Unlike the install directories this
  value is replaced when it differs (trailing slashes do not count; unset always differs), with
  the file backed up first, in the same edit and backup as the install folders. The key is added
  when absent, and in `config.toml` only that value is replaced. If the old value points at a
  folder (or, when unset, the default `addcont` next to the config file) that exists and holds
  entries, the run warns that DLC installed there by hand is no longer seen; nothing is moved or
  deleted. A value that is not a string leaves the file alone with a warning.
- **Safety.** Written only while none of `process` runs (the emulator, its Qt launcher and the
  AppImages: `/proc` is read as for the other overrides, and a missing `/proc` is a warning), with
  the old file backed up first to the settings state folder and an atomic write that is refused if
  the file changed meanwhile. A second run changes nothing; `--dry-run` prints what it would add.
- **Doctor.** `doctor` reports `shadPS4 install folder`: a warning naming each existing file that
  lacks the folder or whose DLC folder is not the profile's `addon_dir` (with the command to
  run), or none found.
- **Why not the settings layer.** `settings apply` layers typed scalar keys from the hub, and its
  `json` and `toml` handlers write scalars only; this list is an array (of objects, in JSON). Its
  value is also derived from each console's own library path, not shared between consoles, so it
  is declared in the profile like the RetroArch paths.

## shadPS4 launch script

`[shadps4.launch]` (`qt_ini`, `versions`, `script`, `find_rules`, `emulator`, all declared by
the profile) replaces EmuDeck's `shadps4.sh` in ES-DE's find rules without touching that
script, which EmuDeck rewrites. `shadps4.render_launch_script` is pure: it returns the bash
text with the two Qt launcher files baked in as quoted absolute paths. The script lives in
`~/.local/share/warpzone/`, outside `~/ES-DE` (so `wz-esde` never syncs it) and not in the
Python package (so it runs when warpzone cannot import). `shadps4.point_find_rule` replaces the
text of the first `staticpath` entry of the named `<emulator>` and verifies that only it
changed; `provision device` backs the file up, refuses while ES-DE runs and uses an atomic
write that fails if the file changed meanwhile. `doctor` compares the script with the text
`provision` would write and reads the rule. The ES-DE startup scripts do not depend on any of
this: if EmuDeck puts its entry back, only PS4 launching goes back to the Qt launcher.

## Cemu paths

Cemu keeps its installs and saves below the MLC folder, `<mlc_path>` in
`~/.config/Cemu/settings.xml`; an empty value means its user data folder
(`~/.local/share/Cemu/mlc01`). Its own game list comes from `<GamePaths>`, a list of `<Entry>`
elements; ES-DE starts games with `-g <file>` and does not need it. EmuDeck sets both with an
`xmlstarlet` that does not start on SteamOS 3.9 (it lacks `libxml2.so.2`), so a new console keeps
EmuDeck's template: `mlc_path` empty, which puts the saves outside `saves.cemu-saves`
(`~/Emulation/roms/wiiu/mlc01/usr/save`), and a game path on an SD card that is not there. A
profile with `[cemu]` (`settings`, `process`, `mlc`, `default_mlc`, `game_path`; `mlc` and
`game_path` may use `{library}`) has `provision device` fix both, for a device that subscribes
to `library`, after the shadPS4 steps. The profile `steamos-emudeck` declares
`{library}/roms/wiiu/mlc01` (whose `usr/save` is the synced save folder) and
`{library}/roms/wiiu/roms` (where `inbox` files `.wua` archives).

- **Edit.** `<mlc_path>` is set to `mlc`, and `game_path` is appended to `<GamePaths>` unless it
  is listed (trailing slashes do not count); the other entries stay. Both must be children of
  the root `<content>`. Only those elements are edited, as text, so every other byte is kept
  (Cemu rewrites the whole file in its own layout when it saves anyway). A missing element is
  added as the root's last child. The result must parse to the original with only those values
  changed, and a file with an element twice, hidden in a comment or nested elsewhere is left
  alone with a warning (`cemu.apply` is pure).
- **No file.** A missing `settings.xml` is never created: Cemu shows its first-start dialog only
  while there is none, and that dialog is where it offers the community graphic packs. The run
  warns to start Cemu once, finish the dialog, close it and run `provision` again.
- **Paths.** `mlc` and `game_path` get `{library}` replaced and then a leading `~/` expanded,
  so `settings.xml` always holds absolute paths (Cemu does not expand `~`).
- **Saves in the old folder.** When `mlc_path` would change and the folder Cemu uses now
  (`mlc_path`, or `default_mlc` when empty) holds a game's or a demo's save, a file anywhere
  under `usr/save/00050000/` or `usr/save/00050002/` (`cemu.is_title_save`), `mlc_path` is left
  as it is, with a warning naming that folder and the synced one: changing it would hide those
  saves from Cemu. What Cemu creates on every start does not count: the Mii Maker folders
  `usr/save/00050010/1004a000`, `1004a100` and `1004a200` (`CreateDefaultMLCFiles` in
  `CemuApp.cpp`) and `usr/save/system/act/80000001/account.dat` (`Account.cpp`). A folder below
  those two that cannot be read counts as holding saves. A folder that resolves
  (`os.path.realpath`) to `mlc` is not another folder: nothing is held back and nothing is
  reported. The game path is still added. Moving the saves is left to the user, because a title
  may already have a save from the hub there. After the switch Cemu also stops seeing the titles,
  updates and DLC installed in the old folder's `usr/title`; the warning says so.
- **Safety.** As for shadPS4: written only while none of `process` runs (`Cemu`, and
  `Cemu.AppImage`), the old file backed up first to the settings state folder (`backups/cemu`),
  and an atomic write that is refused if the file changed meanwhile. A second run changes nothing;
  `--dry-run` prints what it would set.
- **Doctor.** `doctor` reports `Cemu paths`: a warning when the file is missing, does not read,
  has an `mlc_path` that is not `mlc` (naming the folder Cemu uses instead) or lacks the game path.
  When the folder Cemu uses holds game saves, as above, the fix it gives is to move them to
  `mlc`'s `usr/save` first, since `provision` alone would keep `mlc_path`.
- **Why not the settings layer.** It has no XML format, and both values follow each console's
  own library path, so they are declared in the profile like the shadPS4 list.

## Options menu

`[options_menu]` (`dir`, an absolute folder or one starting with `~/`) names the folder whose
executable `.sh` files the device's Options menu lists. Only the `anbernic-rg351mp` profile sets
it, to ArkOS's `/opt/system`. `provision device` writes one bash script there, `Warpzone.sh`,
built by the pure function `options_menu.render_script` and installed by
`provision_scripts.ensure_options_menu`, which also removes the entries of earlier versions
(`Warpzone Status.sh`, `Warpzone Update Now.sh`) when they carry the marker line.

The script reads local files only (no network), line by line and never sourced: the version
(`~/.local/share/warpzone/venv/bin/warpzone --version`), `channel` and `pin` from
`~/.local/state/warpzone/updater/conf`, and `good_tag`, `available`, `last_result` and
`last_fetch` (as a local date) from `~/.local/state/warpzone/updater/state`, and, only in the
status shown without a menu (below), the `[!!]` and `[! ]` lines of `warpzone doctor` (at most
8, each cut to 40 characters, then `+N more`).
Whatever is missing or unreadable shows as `unknown`, and the script always exits 0. With
`dialog` and a writable terminal (`/dev/tty1`, made writable with `sudo -n chmod` as ArkOS's
`msgbox` does; `WZ_TTY` moves it for tests) it shows a menu, driven by the controller through
ArkOS's `gptokeyb` (started only when none runs, stopped on exit). The menu does not run
`warpzone doctor` to open, so it opens at once; it comes back after every item, with the
version, the settings and the state read again (so **Install** is gone once the release is
installed), and only **Exit**, or cancelling the menu (Back), ends the script:

- **Status**: the text above in a message box, with `Doctor: see the Doctor item` in place of
  the doctor's lines.
- **Doctor**: an infobox (`Running doctor...`, with the terminal's echo off) while `warpzone
  doctor` runs (at most 60 seconds, as above; a run cut short says so), then its whole output,
  standard error included, folded to the box, in a `dialog --textbox` that scrolls.
- **Install `<tag>` now**, only when the state has `available`: the live updater's `sh
  ~/.local/share/warpzone/updater/updater.sh tick --now` when its `UPDATER_ABI` is 2 or higher
  (it does not need the clone's Python, so it can still put back a release that no longer
  starts), `warpzone self-update --now` otherwise. The update runs in the background, with
  `gptokeyb` stopped and the terminal's echo off, while a `dialog --gauge` shows the updater's
  `progress` file (`PERCENT TEXT`, see "Progress" in
  [Autonomous updates](/warpzone/development/design/autonomous-updates/)): the menu removes the
  file first, reads it every second and passes it in the gauge's `XXX` format until the update
  ends; while there is no file (an updater older than `UPDATER_ABI` 3 writes none) the bar is
  at 0 with `Working...`. Then the keys pressed meanwhile are dropped and a box shows the exit
  code and the last lines, or, when the updater says another tick holds the lock (a tick of the
  timer is running), that another update is running and to try again in a few minutes.
- **Follow `<other>` releases**: after a yes/no question that says an update never goes down,
  `warpzone self-update --channel <other>`, and its message.
- **Exit**.

Without `dialog` or the terminal it shows the status with `msgbox` (or prints it), with the
doctor's lines as above, and does nothing else.

`[updater]` has one key, `install`: `"auto"` (the default; the timer installs a new release) or
`"manual"` (the timer only looks for one and notes it; `warpzone self-update --now` installs it).
`provision device` writes it to the updater's settings as `install=`. Only `anbernic-rg351mp`
sets `"manual"`: EmulationStation is open whenever that console is on, so waiting for the menu
to close would wait for days, and not waiting would update in the middle of a session.

Writing follows the rules of every device write: each file is replaced atomically and only when
its content differs (`unchanged` otherwise; a lost executable bit is restored), nothing else in
the folder is read or touched, and a folder that does not exist is not created: `provision
device` warns and goes on. A file of the same name that lacks the marker line the scripts start
with is not warpzone's and is left alone with a warning. A dry run reports what it would write.
