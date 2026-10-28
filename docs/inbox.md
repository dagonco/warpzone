# Adding new games: the inbox

How `warpzone inbox` identifies, converts and files new dumps, including Switch, PS3, Xbox, Xbox 360 and arcade.

Drop new dumps (files, folders or archives) in `inbox/` on the hub's share and run `warpzone inbox` on a desktop machine that mounts it (the NAS is too weak for conversions). Without `--apply` it only reports:

```
$ warpzone inbox
inbox: /Volumes/hub/inbox (report only), 2 item(s)
tools: chdman yes, bsdtar yes, rahasher yes, dolphin-tool no; DATs: dreamcast, gb, ...
[1/2] some random name.zip
  nes: Adventure Island (USA).zip: would place
    path: library/roms/nes/Adventure Island (USA).zip
    RA: supported (Adventure Island)
    whitelist: !/roms/nes/Adventure Island (USA).zip
  would move to inbox/_done/2026-10-27/
[2/2] crash bash dump
  psx: Crash Bash (Europe) (En,Fr,De,Es,It).chd: would place
    path: library/roms/psx/Crash Bash (Europe) (En,Fr,De,Es,It).chd
    RA: hash not linked
    whitelist: !/roms/psx/Crash Bash (Europe) (En,Fr,De,Es,It).chd
  would move to inbox/_done/2026-10-27/
items: 2; games to place: 2, already in the library: 0; items for review: 0
report only: run again with --apply to convert and place
```

## Configuration

Set the `[inbox]` table of the config (see [`examples/config.example.toml`](../examples/config.example.toml)). With `--root` and `--dats` it needs no config file.

```toml
[inbox]
root = "/Volumes/hub"                       # the hub root as mounted here (holds inbox/ and library/)
dats_dir = "~/dats/libretro-database/metadat"  # redump/ and no-intro/ inside
titledb_dir = "~/dats/titledb"                # optional: a clone of blawar/titledb (Switch names)
chdman = "/opt/homebrew/bin/chdman"           # optional: otherwise looked up on PATH
rahasher = "~/tools/RAHasher"                 # optional: RetroAchievements hash check
mame_dat = "~/dats/mame0282lx.zip"            # optional: arcade (see "Arcade sets")
fbneo_dat = "~/dats/fbneo-arcade.dat"
mame2003plus_dat = "~/dats/mame2003-plus.xml"
```

`bsdtar`, `dolphin_tool`, `nsz` and `switch_keys` work the same way, and `work_dir` moves the temporary files (they can take several GiB). Run `warpzone doctor` to see what is missing ([doctor.md](doctor.md)). Details in [design.md](design.md#inbox).

## What it does for each item

1. **Unpacks** `.zip` (and `.7z` / `.rar` with `bsdtar`) into a local work folder, never onto the share. Archive members are untrusted: absolute paths, `..` and symbolic links are refused.
2. **Identifies by content**, whatever the files are called: size and CRC32 (and SHA1, when the DAT has it) against the Redump and No-Intro DATs of [libretro-database](https://github.com/libretro/libretro-database) (`metadat/redump`, `metadat/no-intro`). A game matches when all its DAT roms are present. DATs are parsed only when needed and cached in `$XDG_CACHE_HOME/warpzone/dats`.
3. **Converts** to what the library already holds:

   | System | Result |
   |---|---|
   | `psx`, `saturn`, `dreamcast` | `<game>.chd` (`chdman createcd` from cue/bin or gdi), then `chdman verify` |
   | `ps2` | `<game>.chd` (`createdvd` for an `.iso`, `createcd` for cue/bin), then `verify` |
   | `psp` | the plain `.iso`, its CRC32 checked against the DAT |
   | `gc` | `.iso` stays `.iso` (the report shows the `dolphin-tool convert -f rvz ...` command); an `.rvz` is kept as it is |
   | `nes`, `snes`, `n64`, `gb`, `gbc`, `gba`, `genesis`, `mastersystem`, `gamegear` | `<game>.zip` holding the ROM under its DAT name |

   A multi-disc game of a CHD system goes in `<game without disc tag>.m3u/` with every disc and a `<game>.m3u` playlist, and only when all its discs are in the item; otherwise it goes to review.
4. **Checks RetroAchievements** (optional). With `rahasher` configured it hashes the source and the result and refuses to place a game whose hash changed. With `RA_API_KEY` in the environment it looks the hash up and reports `supported (<title>)` or `hash not linked`. The key is never printed.
5. **Places** the result in `library/roms/<system>/` (copied to `<name>.part`, read back, then renamed). It never overwrites: the same content is reported as "already in the library", different content under the same name goes to review.
6. **Files the original** in `inbox/_done/<date>/`, or in `inbox/_revisar/` with a `<item>.reason.txt` (no DAT match, incomplete multi-disc, conversion or verify failed, RA hash changed, conflict, missing tool). On every `--apply`, `_done` folders named like a date and older than 7 days are deleted, nothing else.

Items modified in the last 30 seconds are left alone (they may still be copying), and `_*` folders, hidden files, `@eaDir` and `.DS_Store` are never items. warpzone does not edit whitelists: add the printed lines to the device whitelists yourself ([whitelists.md](whitelists.md)). Exit code 1 when any item needs review.

Vita is not supported yet, and neither are PS3 or Xbox games kept as extracted folders or `.pkg` files. Those items go to review ("not supported by inbox yet").

## Switch games

`.nsp` and `.nsz` files (loose, in a folder or in an archive) are identified by what is inside them, named after [blawar/titledb](https://github.com/blawar/titledb) and filed the way the library already has them:

| Kind | Where | Name |
|---|---|---|
| Game | `library/roms/switch/` | `<Title> [<TitleID>][v<version>][BASE].nsp` |
| Update | `library/extras/switch/updates/<Title>/` | `<Title> [<UpdateTitleID>][v<version>][UPD].nsp` |
| DLC | `library/extras/switch/dlc/<Title>/` | `<Title> - <DLC name> [<DlcTitleID>][v<version>][DLC].nsp` |

The TitleID decides the kind (a game ends in `000`, an update in `800`, anything else is DLC). The version comes from the file's CNMT, read with [`nsz`](https://github.com/nicoboss/nsz) (`uv tool install nsz`), which needs your console's `prod.keys` (`[inbox] switch_keys`, default `~/.switch/prod.keys`). warpzone only hands the path to `nsz` and never reads or prints the file.

Titles come from a local clone of titledb (`git clone --depth 1 https://github.com/blawar/titledb`, set `titledb_dir` to it; `git pull` updates it). Punctuation is made ASCII (`:` becomes ` -`, curly quotes become straight), words in capitals are written normally (short acronyms and roman numerals stay), and unsafe characters are dropped. A DLC that titledb only lists inside a bundle is named `<Bundle> (part N of M)` in the bundle's order.

- `.nsz` is decompressed with `nsz -D` into the work folder and only the `.nsp` is placed. Because `nsz` exits 0 on some damaged files, the result is checked again: `nsz -V` must verify it and its PFS0 header must parse.
- `.xci` and `.xcz` are never converted (no tool does it cleanly); they go to review with the reason.
- Without `nsz` or the keys file, an `.nsp` falls back to the `[TitleID][vN]` in its file name and the report says so. A `.nsz` cannot be read and goes to review. A title titledb does not know, or an `.nsp` holding several titles, goes to review.
- A file is never overwritten. The check is by TitleID and version wherever the library has it: the same bytes are "already in the library", different bytes go to review.
- Games print their whitelist line (`!/roms/switch/...`). Updates and DLC are usually whitelisted by folder (`!/extras/switch/updates`, `!/extras/switch/dlc`), so the report says that once instead of one line per file.

## PS3, Xbox and Xbox 360 disc images

No Redump or No-Intro DAT can match these three, so they are identified by their signature, read in a few small pieces (never the whole image):

| Image | Signature | Placed in |
|---|---|---|
| PS3, decrypted (`*.dec.iso`) | ISO9660 with `PS3_DISC.SFB` and `PS3_GAME/PARAM.SFO` (its `TITLE_ID` and `TITLE` are reported) | `library/roms/ps3/` |
| Xbox, XISO (`*.xiso.iso`, `*.xiso`) | XDVDFS (`MICROSOFT*XBOX*MEDIA` at 0x10000) with `default.xbe` in the root | `library/roms/xbox/` |
| Xbox 360 (plain `*.iso`) | XDVDFS partition at 0xFDA0000 (XGD2) or 0x2080000 (XGD3), or an extracted XISO at 0, with `default.xex` in the root | `library/roms/xbox360/roms/` |

Xbox and Xbox 360 are told apart by the root executable, not by the offset. An item whose file names end in `.dec.iso`, `.xiso.iso` or `.xiso` (loose, in a folder or in an archive) is taken over. A plain `.iso` is left to the DATs first, so PS2, PSP and GameCube images are never touched, and only an `.iso` that no DAT explains is then sniffed and placed if it is one of these.

The file keeps its name and nothing is overwritten (same size and SHA1 is "already in the library", anything else goes to review). The report shows what was detected, the destination and the whitelist line (`!/roms/xbox360/roms/<name>`).

A full Redump-style Xbox image (game partition at 0x18300000) is never placed, because xemu cannot boot it. It goes to review with the instruction to convert it to XISO with [extract-xiso](https://github.com/XboxDev/extract-xiso).

A PS3 `.iso` needs ES-DE's alternative emulator `RPCS3 ISO (Standalone)` (the system default is a shortcut launcher). So `--apply` also records `<altemulator>RPCS3 ISO (Standalone)</altemulator>` for the game in the hub's ES-DE gamelist (`frontend/es-de/gamelists/ps3/gamelist.xml`, or `gamelist_ps3`), the same way as for arcade sets: only that game's entry is touched and a copy of the previous file is kept ([gamelist edits](#gamelist-edits)).

## Arcade sets

An arcade `.zip` (or a folder of them) is identified by the size and CRC32 of its members, not unpacked, and filed as it is in `library/roms/mame/<set>.zip`. This needs at least one of three databases in XML, set in `[inbox]`. Without any of them, arcade zips take the usual path above and go to review ("no DAT match").

| Key | File | Used for |
|---|---|---|
| `mame_dat` | MAME's `-listxml`, or the official `mameXXXXlx.zip` of a [MAME release](https://github.com/mamedev/mame/releases) as it is (the zip holds the listxml) | the ES-DE default emulator, "MAME - Current" |
| `fbneo_dat` | `FinalBurn Neo (ClrMame Pro XML, Arcade only).dat` from the `dats/` folder of [FBNeo](https://github.com/libretro/FBNeo) | "FinalBurn Neo" |
| `mame2003plus_dat` | `mame2003-plus.xml` from the `metadata/` folder of [mame2003-plus-libretro](https://github.com/libretro/mame2003-plus-libretro) | "MAME 2003-Plus" |

Use the MAME version your frontend's core runs. The listxml is streamed and cached as a compact index in the DAT cache folder.

A set *fits* an emulator when every required ROM (not `nodump`, not `optional`) of the set, its parent, its BIOS and its devices is present, counting the BIOS and device zips already in `library/roms/mame/`. The first emulator that fits wins: FinalBurn Neo, else MAME (the ES-DE default, so no override is written), else MAME 2003-Plus. FinalBurn Neo goes first because of RetroAchievements: EmuDeck turns them off for the "MAME - Current" core and the RA arcade sets target FinalBurn Neo, so CPS1 games such as `dino` or `ddsom` only earn achievements there.

Sega Atomiswave and Naomi games are the exception: they hang in MAME but run in Flycast. They are recognised from the `romof` chain in the MAME database (BIOS `awbios`, `naomi`, `naomi2` or `naomigd`), and when MAME's set is complete they get `<altemulator>Flycast</altemulator>`. Flycast reads its BIOS from `bios/dc/` (same file names as MAME). If `library/bios/dc/` lacks the matching zip, the game is still placed and the report tells you to copy that zip from `roms/mame/` yourself; warpzone never copies BIOS files.

A set that fits no emulator goes to review, and the reason lists the exact files missing per emulator and the zip they belong to (`MAME 0.282: mslug: needs neogeo.zip (2 file(s): sp-s2.sp1 9036d879, ...)`).

The file keeps (or gets) the set's short name, `<set>.zip`, which MAME requires: a zip called anything else is renamed after the best-fitting database. Merged, split and non-merged sets are all accepted as long as the members match, and a clone's zip is completed by its parent's zip already in the library. Nothing is ever overwritten: the same members are "already in the library", a different file under that name goes to review.

CHD disks of a set are listed as a note but not checked. Sets must be zips (`.7z` and `.rar` are not opened for arcade).

## Gamelist edits

For an emulator other than the default, `--apply` writes the ES-DE per-game override into the hub's gamelist (`frontend/es-de/gamelists/mame/gamelist.xml`, or `gamelist_mame`):

```xml
<game>
	<path>./dino.zip</path>
	<altemulator>FinalBurn Neo</altemulator>
</game>
```

- Only that game's entry is added, changed or (when the default now fits) stripped of its override. Everything else in the file stays as it is.
- The file is written to `.part` and renamed. The file as it was before the run is copied to `<root>/tools/logs/gamelist-backups/<system>-gamelist-<YYYYMMDD-HHMMSS>.xml`, outside the synced gamelist folder, so the backup never reaches the consoles (`wz-tools` ignores `/logs`).
- The report mode prints what it would write.

**Quit ES-DE on the consoles first.** ES-DE keeps its gamelist in memory and rewrites it when a game is launched, so an edit made on the hub while ES-DE is open on a console is overwritten. With `--apply`, if the gamelist was modified in the last 10 minutes (a console that just played writes it back through Syncthing), `inbox` warns, still makes the edit, and tells you to quit ES-DE on every console and run it again if the change does not stick.
