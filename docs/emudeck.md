# EmuDeck and the steamos-emudeck profile

What the shipped profile covers, why save folders point at real directories, and the few steps that stay manual.

A profile says where things live on one kind of device: [`warpzone/data/profiles/`](../warpzone/data/profiles). warpzone 0.1.0 ships `steamos-emudeck` (Steam Deck, Steam Machine). Profiles are plain TOML, so adding a setup is a small pull request. A save entry like `[saves.dolphin-gc]` maps to the folder ID `wz-save-dolphin-gc` and the hub path `saves/dolphin/gc`. A `[bios.cemu-keys]` placement maps to `wz-bios-cemu-keys`.

## Saves covered

RetroArch, Dolphin, PrimeHack (GC, Wii, states), DuckStation, PCSX2, melonDS, PPSSPP (saves, states), RPCS3 (savedata, trophy), Vita3K, MAME, RMG, ScummVM, Eden and Ryujinx (including the Switch user profiles, so a save keeps its owner on every console), Azahar, shadPS4, Cemu, Xenia (the Proton build or the native one), Flycast (only `vmu_save_*.bin` and `nvmem` files, never the BIOS next to them) and YARG (settings, scores, profiles, playlists and custom content; never `songcache.bin` or `badsongs.txt`).

YARG syncs `custom/` in lowercase, as YARG on Linux writes it, and ignores the `*.bak` files it leaves in `profiles/`. If the hub holds the content under `saves/yarg/data/Custom`, rename it to `custom`.

## The symlink gotcha

EmuDeck exposes saves through symlinks under `~/Emulation/saves`. Syncthing never follows symlinks, so a folder pointed at the symlink syncs nothing and gives no error. The profile therefore points at the real directory of each emulator (for example RetroArch's saves under `~/.var/app/org.libretro.RetroArch/`). If you add a profile for another setup, resolve the links first (`readlink -f`).

Some save folders sit inside `~/Emulation`, which is the library root. That is fine: the library whitelist ignores them, and Syncthing supports nested folders when the parent ignores the child.

The same trap exists in `~/Emulation/bios`. EmuDeck turns `eden/keys`, `eden/firmware`, `azahar/keys`, `ryujinx/keys`, `flycast/bios` and `shadps4/sys_modules` into symlinks, while the hub's `library/bios/` holds real folders at those paths. The profile therefore makes the device's library ignore those paths, ahead of your whitelist so an edit cannot bring them back. It also ignores `roms/wiiu/mlc01`, where Cemu keeps its own installs and saves (its saves sync separately).

## EmuDeck's own files

The profile ignores files EmuDeck creates that are not on the hub, so a receive-only console does not list them as local changes:

- `bios/PPSSPP/` and every `readme.txt` under `bios/`.
- The emulator-owned `bios/` folders `HdPacks`, `Mupen64plus`, `fbneo`, `kronos`, `same_cdi`, `ryujinx`, `shadps4` and `yuzu`.
- Each `roms/*/systeminfo.txt` and `metadata.txt`.
- RetroArch's Flycast VMUs and flash in `bios/dc/` (`vmu_save_*.bin`, `dc_nvmem.bin`). These two are ignored without `(?d)`, so a hub-side delete of `bios/dc` never takes them along. See [Dreamcast saves](#dreamcast-saves-retroarch-flycast).

These come from the profile's `[library] ignore` list. The hub's library folder ignores the hub paths of the BIOS placements, so each file belongs to one folder. A line may start with `(?d)`. Run `provision device` again after upgrading to apply a changed profile.

## Dreamcast saves (RetroArch Flycast)

By default the Flycast core in RetroArch shares up to eight VMU files (`vmu_save_A1.bin` and the rest) between all games and writes them, with the console flash `dc_nvmem.bin`, into RetroArch's system folder, `~/Emulation/bios/dc/`. That folder is inside the receive-only library, so the profile has to ignore those files (see above) and they do not sync. Without the ignore lines a Revert on the console would erase them.

To get Dreamcast saves synced, turn on the core option **Per-Game Visual Memory Units/Systems (VMU)** (shown as "Per-Game VMUs"; the key is `reicast_per_content_vmus`): start a Dreamcast game, open Quick Menu > Core Options and choose `VMU A1` (one card per game) or `All VMUs` (all eight per game). The VMUs then go to RetroArch's saves folder, which `retroarch-saves` syncs both ways. Cards already in `bios/dc/` are not migrated: every game starts with an empty one, so copy a card by hand if you want to keep it (the per-game file is named after the game's ID and the port, for example `<game id>.A1.bin`).

`warpzone doctor` warns when `Flycast.opt` exists and the option is off or missing. The standalone Flycast (`flycast-saves`) is unaffected.

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

A placement whose emulator is not installed is skipped with a warning, like a save folder. A profile may set `create_when` to create the missing folder when the emulator's data folder exists.

Because the keys now live in the emulators' folders, `warpzone bios` on a device no longer sees them in `~/Emulation/bios`. Run it against the hub's `library/` ([bios.md](bios.md)).

xemu's disk image (`xbox_hdd.qcow2`, which holds that console's Xbox saves) is deliberately not synced: it is per device.

## Manual steps

These are done once per device, by hand.

**Ryujinx keys.** Ryujinx has no placement. Its `system/` folder would need the same `prod.keys` and `title.keys` that Eden already receives, and two Syncthing folders cannot share one hub folder cleanly (see [design.md](design.md#library-exclusions-and-bios-placements)). Copy the two files from Eden's keys folder to `~/.config/Ryujinx/system/`.

**Ryujinx firmware.** Ryujinx cannot receive its firmware as files either. Install it from Ryujinx (Tools > Install Firmware), pointing at the Eden firmware folder, `~/Emulation/storage/eden/nand/system/Contents/registered`.

**RPCS3 firmware.** Install `PS3UPDAT.PUP` once from RPCS3 (File > Install Firmware). The copy in `bios/ps3/` is a staging file.
