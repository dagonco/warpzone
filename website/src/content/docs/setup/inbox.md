---
title: "Adding new games: the inbox"
---

How `warpzone inbox` identifies, converts and files new dumps, including Switch, 3DS, Wii and GameCube `.rvz`, PS3, PS4, PS Vita, Xbox, Xbox 360, Wii U and arcade.

Drop new dumps (files, folders or archives) in `inbox/` on the hub's share and run `warpzone inbox` on a desktop machine that mounts it (the NAS is too weak for conversions). Without `--apply` it only reports:

```
$ warpzone inbox
inbox: /Volumes/hub/inbox (report only), 2 item(s)
tools: chdman yes, bsdtar yes, rahasher yes, dolphin-tool no, nsz no, orbis-pkg-util no, vita3k no; DATs: dreamcast, gb, ...
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

Set the `[inbox]` table of the config (see [`examples/config.example.toml`](https://github.com/dagonco/warpzone/blob/main/examples/config.example.toml)). With `--root` and `--dats` it needs no config file.

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
vita_zrif = "~/keys/PSV_GAMES.tsv"            # optional: zRIF keys for PS Vita .pkg files
```

`bsdtar`, `dolphin_tool`, `nsz`, `orbis_pkg_util`, `vita3k` and `switch_keys` work the same way, and `work_dir` moves the temporary files (they can take several GiB). Run `warpzone doctor` to see what is missing ([the doctor guide](/warpzone/operation/doctor/)). Details in [the design document](/warpzone/development/design/inbox/).

## What it does for each item

1. **Unpacks** `.zip` (and `.7z` / `.rar` with `bsdtar`) into a local work folder, never onto the share. Archive members are untrusted: absolute paths, `..` and symbolic links are refused.
2. **Identifies by content**, whatever the files are called: size and CRC32 (and SHA1, when the DAT has it) against the Redump and No-Intro DATs of [libretro-database](https://github.com/libretro/libretro-database) (`metadat/redump`, `metadat/no-intro`). A game matches when all its DAT roms are present. DATs are parsed only when needed and cached in `$XDG_CACHE_HOME/warpzone/dats`.
3. **Converts** to what the library already holds:

   | System | Result |
   |---|---|
   | `psx`, `saturn`, `dreamcast` | `<game>.chd` (`chdman createcd` from cue/bin or gdi), then `chdman verify` |
   | `ps2` | `<game>.chd` (`createdvd` for an `.iso`, `createcd` for cue/bin), then `verify` |
   | `psp` | the plain `.iso`, its CRC32 checked against the DAT |
   | `gc` | `.iso` stays `.iso` (the report shows the `dolphin-tool convert -f rvz ...` command); a `.gcz` is identified with `dolphin-tool` and kept as it is. An `.rvz` or `.wia` is read by its header instead, see [Wii and GameCube compressed images](#wii-and-gamecube-compressed-images) |
   | `nes`, `snes`, `n64`, `gb`, `gbc`, `gba`, `genesis`, `mastersystem`, `gamegear` | `<game>.zip` holding the ROM under its DAT name |

   A multi-disc game of a CHD system goes in `<game without disc tag>.m3u/` with every disc and a `<game>.m3u` playlist, and only when all its discs are in the item; otherwise it goes to review.
4. **Checks RetroAchievements** (optional). With `rahasher` configured it hashes the source and the result and refuses to place a game whose hash changed. With `RA_API_KEY` in the environment it looks the hash up and reports `supported (<title>)` or `hash not linked`. The key is never printed.
5. **Places** the result in `library/roms/<system>/` (copied to `<name>.part`, read back, then renamed). It never overwrites: the same content is reported as "already in the library", different content under the same name goes to review.
6. **Files the original** in `inbox/_done/<date>/`, or in `inbox/_revisar/` with a `<item>.reason.txt` (no DAT match, incomplete multi-disc, conversion or verify failed, RA hash changed, conflict, missing tool). On every `--apply`, `_done` folders named like a date and older than 7 days are deleted, nothing else.

**One `--apply` at a time.** While it works, `--apply` holds a lock file, `inbox/.warpzone.lock`, with the host name, the process id and the start time of the run, and removes it when it ends (also after an error or Ctrl-C). A second `--apply` on the same hub, from this machine or another desktop that mounts the share, stops with exit code 2 and says who holds the lock and since when; a report without `--apply` never needs the lock. If a run was killed and left its lock behind, the lock counts as stale once it is older than 12 hours or when its process is gone from the machine that took it (a lock from another machine cannot be checked, so it only goes stale by age). A stale lock is never taken silently: the refusal says so, and `warpzone inbox --apply --force-lock` takes it over. A lock that is still live is never taken, with or without `--force-lock`.

Items modified in the last 30 seconds are left alone (they may still be copying), and `_*` folders, hidden files, `@eaDir` and `.DS_Store` are never items. warpzone does not edit whitelists: add the printed lines to the device whitelists yourself ([Whitelists and pruning](/warpzone/setup/whitelists/)). Exit code 1 when any item needs review.

A PS Vita dump kept as a game folder is filed (see [PS Vita games](#ps-vita-games)), and a Vita `.pkg` is installed with Vita3K first (see [PS Vita packages](#ps-vita-packages)). `.vpk` files, PS3 or Xbox games kept as extracted folders and PS3 `.pkg` files are not supported yet and go to review ("not supported by inbox yet").

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

## 3DS games

`.3ds` and `.cci` files (the two names of one format, a cartridge image) are recognised by their header, not by a DAT and not by their name alone, whether they are loose, in a folder or in an archive. Only two 0x200-byte pieces are read:

- the NCSD container: the magic `NCSD` at 0x100, and an image size (u32 at 0x104, counted in 0x200-byte units) that is not bigger than the file;
- partition 0: an NCCH header whose flags mark it as decrypted (NoCrypto).

A decrypted image is filed in `library/roms/n3ds/<name>.cci`. A `.3ds` is renamed to `.cci` on the way in and its bytes do not change, so the same game dropped as `.3ds` and as `.cci` is the same file ("already in the library"). Nothing is ever overwritten: a different file under that name goes to review. The report shows what was detected, the destination and the whitelist line (`!/roms/n3ds/<name>.cci`).

An encrypted image goes to review: Azahar, the 3DS emulator of the consoles, opens only decrypted images, so decrypt it first and drop the result. A file whose header is not valid (no `NCSD` or `NCCH` magic, a size bigger than the file, a truncated dump) goes to review with the reason. `.3dsx` homebrew and `.cia` installers are not images and are not handled here.

## Wii and GameCube compressed images

An `.rvz` or `.wia` file (Dolphin's compressed disc formats) holds a copy of the disc header, so it is identified by reading the first 0xD8 bytes of the file, loose, in a folder or in an archive. No tool and no DAT is needed (`dolphin-tool` is not used for these):

- the magic `RVZ\x01` or `WIA\x01` at the start;
- the file size recorded at 0x2C, which must match the real size (a smaller file is a copy that stopped early);
- the disc type at 0x48 (1 GameCube, 2 Wii), which must agree with the magic inside the disc header at 0x58: `5D1C9EA3` for Wii, `C2339F3D` for GameCube;
- the game ID (6 letters or digits) and the title from that disc header.

The image is filed with the bytes unchanged, because Dolphin (the emulator ES-DE launches for both systems) opens compressed images as they are:

| Disc | Placed in | Name |
|---|---|---|
| Wii | `library/roms/wii/` | `<name> [<GAME ID>].rvz` (or `.wia`) |
| GameCube | `library/roms/gc/` | `<name> [<GAME ID>].rvz` (or `.wia`) |

The name is the file's own, with unsafe characters dropped and `:` written as ` -`, and the game ID added: `Mario Kart Wii (USA) (En,Fr,Es).rvz` is filed as `Mario Kart Wii (USA) (En,Fr,Es) [RMCE01].rvz`. A file name that already ends in that `[<GAME ID>]` keeps it once. When the file name is empty or only the game ID (`RMCE01.rvz`), the title in the disc header stands in, for example `MarioKartWii [RMCE01].rvz`. A compressed image cannot be matched to a Redump entry without expanding it, so the name does not come from a DAT, and the same game dropped under two file names is filed twice. Nothing is overwritten: same size and SHA1 is "already in the library", anything else under that name goes to review. A file that is not an image, a truncated copy or a header whose disc type and magic disagree goes to review with the reason. The report shows the detection, the path and the whitelist line (`!/roms/wii/<name>`). A plain Wii `.iso` is not handled yet and goes to review as "no DAT match".

## PS3, PS4, Xbox, Xbox 360 and Wii U images

No Redump or No-Intro DAT can match these five, so they are identified by their signature, read in a few small pieces (never the whole image):

| Image | Signature | Placed in |
|---|---|---|
| PS3, decrypted (`*.dec.iso`) | ISO9660 with `PS3_DISC.SFB` and `PS3_GAME/PARAM.SFO` (its `TITLE_ID` and `TITLE` are reported) | `library/roms/ps3/` |
| Xbox, XISO (`*.xiso.iso`, `*.xiso`) | XDVDFS (`MICROSOFT*XBOX*MEDIA` at 0x10000) with `default.xbe` in the root | `library/roms/xbox/` |
| Xbox 360 (plain `*.iso`) | XDVDFS partition at 0xFDA0000 (XGD2) or 0x2080000 (XGD3), or an extracted XISO at 0, with `default.xex` in the root | `library/roms/xbox360/roms/` |
| Wii U (`*.wua`, a Cemu ZArchive) | the 144-byte footer at the end of the file: its last 16 bytes are the file size (u64), the version `0x61bf3a01` and the magic `0x169f52d6` (all big-endian), and the size must equal the real size of the file | `library/roms/wiiu/roms/` |
| PS4 (`*.pkg`) | the first 0x80 bytes: magic `\x7fCNT` and, at 0x40, a content id shaped like `XXnnnn-CUSAnnnnn_nn-...` (the title id `CUSAnnnnn` is reported) | `library/roms/ps4/games/<CUSAnnnnn>/` and a shortcut, see [PS4 games](#ps4-games) |

Xbox and Xbox 360 are told apart by the root executable, not by the offset. An item whose file names end in `.dec.iso`, `.xiso.iso`, `.xiso`, `.wua` or `.pkg` (loose, in a folder or in an archive) is taken over. A plain `.iso` is left to the DATs first, so PS2, PSP and GameCube images are never touched, and only an `.iso` that no DAT explains is then sniffed and placed if it is one of these.

The file keeps its name and nothing is overwritten (same size and SHA1 is "already in the library", anything else goes to review). The report shows what was detected, the destination and the whitelist line (`!/roms/xbox360/roms/<name>`).

A `.wua` is judged by its footer only, by reading its last 16 bytes: a truncated file, a file with a wrong footer or another kind of image renamed to `.wua` goes to review and is not placed, and a ZArchive named `.iso` is not recognised. It goes to `roms/wiiu/roms/` (not `roms/wiiu/`) because that is the game path Cemu is pointed at. It also works inside a `.zip`, `.7z` or `.rar` (7z and rar need `bsdtar`), like the other images here.

A `.pkg` is judged by that header only. A PS Vita `.pkg` (magic `\x7fPKG`) is installed with Vita3K, see [PS Vita packages](#ps-vita-packages). A PS3 `.pkg` or any `.pkg` with another magic or content id is not placed and goes to review as "not supported by inbox yet". It also works inside a `.zip`, `.7z` or `.rar`.

A full Redump-style Xbox image (game partition at 0x18300000) is never placed, because xemu cannot boot it. It goes to review with the instruction to convert it to XISO with [extract-xiso](https://github.com/XboxDev/extract-xiso).

A PS3 `.iso` needs ES-DE's alternative emulator `RPCS3 ISO (Standalone)` (the system default is a shortcut launcher). So `--apply` also records `<altemulator>RPCS3 ISO (Standalone)</altemulator>` for the game in the hub's ES-DE gamelist (`frontend/es-de/gamelists/ps3/gamelist.xml`, or `gamelist_ps3`), the same way as for arcade sets: only that game's entry is touched and a copy of the previous file is kept ([gamelist edits](#gamelist-edits)).

## PS4 games

shadPS4 has no package installer: it runs extracted game folders, and ES-DE lists only the `.desktop` shortcuts of `roms/ps4/shortcuts`. With [`orbis-pkg-util`](https://crates.io/crates/orbis-pkg-util) (`cargo install orbis-pkg-util`; set `[inbox] orbis_pkg_util` when `~/.cargo/bin` is not on PATH) `--apply` therefore turns a base-game `.pkg` into:

| What | Where |
|---|---|
| The extracted game (`eboot.bin`, `sce_sys/param.sfo`, data) | `library/roms/ps4/games/<CUSAnnnnn>/` |
| The shortcut ES-DE lists | `library/roms/ps4/shortcuts/<Title>.desktop` |

```
  ps4: CUSA30992: placed
    path: library/roms/ps4/games/CUSA30992
    note: detected: PS4 package: CUSA30992
    note: shortcut: library/roms/ps4/shortcuts/Teenage Mutant Ninja Turtles - Shredders Revenge.desktop
    whitelist: !/roms/ps4/games/CUSA30992
    whitelist: !/roms/ps4/shortcuts/Teenage Mutant Ninja Turtles - Shredders Revenge.desktop
```

The package is unpacked in the local work folder (free space about the size of the `.pkg`), `eboot.bin` and `sce_sys/param.sfo` must be in the result, and the folder is copied to `<CUSAnnnnn>.part`, read back and renamed, so the library never holds a half game. The title in `param.sfo` names the shortcut. Add both printed whitelist lines to the device whitelists. A report without `--apply` does not unpack anything, so it prints only the folder line.

```
[Desktop Entry]
Type=Application
Name=<Title>
Exec=shadps4 "/home/deck/Emulation/roms/ps4/games/<CUSAnnnnn>/eboot.bin"
```

EmuDeck's `shadps4.sh` reads the quoted path of the `Exec=` line and runs shadPS4's Qt launcher with it, which boots the version you selected there. The path is written out because nothing expands `~` in it; it assumes the SteamOS home `/home/deck`. The consoles must also have `~/Emulation/roms/ps4/games` in shadPS4's install directories: `provision device` adds it for profiles that declare it ([the EmuDeck guide](/warpzone/guides/emudeck/#shadps4-install-directory)).

- A game already in the library is reported and never replaced or compared; its shortcut is created if it is missing.
- An update (patch) or DLC package goes to review: they are not merged into the game yet. The header shows it before anything is unpacked, and the `CATEGORY` of `param.sfo` is checked again afterwards.
- A shortcut with that name that runs another game is kept, and the new one gets the title id in its name.
- Without `orbis-pkg-util` the `.pkg` is placed as it is in `library/roms/ps4/` with a note saying so; shadPS4 cannot run it from there.

## PS Vita games

Vita3K runs the games it finds in `ux0/app/<TITLEID>`, and ES-DE lists only `.psvita` files, whose content EmuDeck's launcher passes to Vita3K as the title to run. A game dump (what a dumper leaves: `eboot.bin`, `sce_sys/param.sfo` and the data, no `sce_pfs`; its licence is covered below), loose in a folder or inside a `.zip`, `.7z` or `.rar`, at the top level or inside one top folder, is recognised by `sce_sys/param.sfo` next to `eboot.bin`, with a Vita title id (`PCS[A-I]` and five digits) and category `gd` or `gdc`. `--apply` places:

| What | Where |
|---|---|
| The dump as it is | `library/roms/psvita/app/<TITLEID>/` |
| The launcher ES-DE lists | `library/roms/psvita/<Title>.psvita`, whose whole content is the title id |
| The game's licence, when the item has one | `library/roms/psvita/license/<TITLEID>/<CONTENT_ID>.rif` |

```
  psvita: PCSF00024: placed
    path: library/roms/psvita/app/PCSF00024
    note: detected: Vita game: PCSF00024 GRAVITY RUSH
    note: launcher: library/roms/psvita/GRAVITY RUSH.psvita
    whitelist: !/roms/psvita/app/PCSF00024
    whitelist: !/roms/psvita/GRAVITY RUSH.psvita
```

A game with a licence prints a `note: licence: ...` line and a third whitelist line, `!/roms/psvita/license/<TITLEID>`.

The folder is copied file by file with a read-back to `<TITLEID>.part` and renamed, so the library never holds a half game. Add the printed whitelist lines to the device whitelists. Unlike PS4, nothing needs extracting, so a report without `--apply` already prints them.

- A game already in the library is reported and never replaced or compared; its launcher is written if it is missing. A launcher that already holds this title id is kept ("already there"); one with the same name that holds another id (the same title in another region) is kept, and the new one gets the title id in its name.
- An update (`gp`) or DLC (`ac`) goes to review with a message, as does any other category. A dump with a title id that is not a Vita one (a PS4 game folder, for example) goes to review, and so does one that holds an `sce_pfs` folder: Vita3K has to install those, so install them there by hand, or drop the `.pkg` instead ([PS Vita packages](#ps-vita-packages)).
- **Licence.** A game installed by Vita3K keeps its `eboot.bin` encrypted (it starts with `SCE\0` and its first segment is not plain), and Vita3K runs it only with the licence `ux0/license/<TITLEID>/<CONTENT_ID>.rif` (512 bytes); without it the log says `[decrypt_fself]: No klic provided for encrypted App` and `Failed to start game threads`. Along with the game, inbox accepts its licence as `sce_sys/package/work.bin` inside the game (NoNpDrm), or as a `<CONTENT_ID>.rif` in the item, in `license/<TITLEID>/` or loose next to the game folder (or inside it). The file is checked (512 bytes, a Vita content id at offset 0x10, the same title id as the game, and the content id in the game's `param.sfo`) and filed under the content id it holds, whatever its name, through a `.part` copy that is read back. A licence already in the library is kept when it is the same file and is never overwritten when it differs (the item goes to review before anything is placed). The licence of a game that is already in the library is added when it is missing.
- A game that needs a licence and has none goes to review, saying so, and nothing is placed. Whether it needs one is read from `eboot.bin` as Vita3K does (the first 64 KiB, the SELF header only): a decrypted `eboot.bin` (as Vita3K's own "decrypt selfs" leaves it) keeps the `SCE\0` magic and needs none, and an `eboot.bin` with that magic and an unreadable header goes to review. A licence that comes along when the game does not need it is filed anyway. A file that is not a licence, one of another game or build, or several different ones go to review with the reason.
- A Vitamin dump always goes to review, whether it is a loose folder or inside an archive. It is recognised by `sce_module/steroid.suprx` inside the game folder; Vita3K rejects that module, so the dump is never placed, even if the game might start, and the reason suggests a clean dump instead (a `.pkg` with its `work.bin`, or a NoNpDrm dump). What is already in the library is not touched.
- An item with several games, a symbolic link inside the game, or an unreadable `param.sfo` goes to review. A PS3 game (`PS3_GAME/PARAM.SFO`) is not this layout and keeps its "not supported by inbox yet" outcome.
- The consoles link the game folder and the licence folder into Vita3K's own storage, see [the EmuDeck guide](/warpzone/guides/emudeck/#vita3k-games).

## PS Vita packages

A Vita `.pkg` (a PlayStation Store package, as NoPayStation lists them) is encrypted, and its licence comes apart as a zRIF, a short text key. With [Vita3K](https://vita3k.org) on this machine and the game's zRIF in a key file you keep, `--apply` installs the package with Vita3K and files the result exactly like a [game dump](#ps-vita-games): the game folder, the licence and the launcher. warpzone never downloads packages or keys.

1. Install Vita3K. On macOS put `Vita3K.app` in `/Applications` (the default), or set `vita3k` in `[inbox]` to the app or its binary. Elsewhere `vita3k` on PATH is used; only the macOS app has been tried.
2. Keep the zRIFs in a file and set `[inbox] vita_zrif` to it. Either one line per game, the content id and the zRIF separated by a tab (lines starting with `#` are comments):

   ```
   EP9000-PCSF00024_00-PDEUAP1648614960<TAB><zRIF>
   ```

   or a TSV with a header row, such as NoPayStation's `PSV_GAMES.tsv`: only the `Content ID` and `zRIF` columns are read, the rest (links included) is ignored.
3. Drop the `.pkg` in `inbox/`, loose or in an archive, and run the report:

```
[1/1] UP4034-PCSE00567_00-LAMULANAEX000000_bg_1_....pkg
  psvita: PCSE00567: would place
    path: library/roms/psvita/app/PCSE00567
    note: detected: Vita game: PCSE00567 LA-MULANA EX
    note: would install it with Vita3K: Vita3K --pkg UP4034-PCSE00567_00-LAMULANAEX000000_bg_1_....pkg --zrif ***
    note: licence: library/roms/psvita/license/PCSE00567/UP4034-PCSE00567_00-LAMULANAEX000000.rif (would be written)
    note: launcher: library/roms/psvita/LA-MULANA EX.psvita (would be written)
    whitelist: !/roms/psvita/app/PCSE00567
    whitelist: !/roms/psvita/license/PCSE00567
    whitelist: !/roms/psvita/LA-MULANA EX.psvita
  note: detected: Vita package: UP4034-PCSE00567_00-LAMULANAEX000000
```

The report runs nothing: the title id, the title and the licence are read from the package header, its unencrypted `param.sfo` and the zRIF, so all three whitelist lines are known before installing. `--apply` then runs Vita3K in a folder of its own inside the local work folder (it needs about the package's size free) and never touches Vita3K's own data or a console's: it uses Vita3K's portable mode, with a link to the app and an empty `portable/` folder, and runs `Vita3K --version` first, so that if Vita3K does not use that folder nothing is installed. Vita3K exits with success even when an install fails, so warpzone checks what it left: the game folder with `eboot.bin` and a `param.sfo` of the same title id, content id and a game category, no `sce_pfs`, and a licence equal to the zRIF. The work folder is deleted afterwards.

- The zRIF is a key and is never printed or written to a reason file: the command line is shown with `***` in its place. While Vita3K runs (under a second for a 200 MiB package) it is on Vita3K's command line, the only way Vita3K takes it, so another user of the machine could see it in the process list.
- A package whose content id has no zRIF in the file, or a zRIF that does not decode to that game's licence, goes to review saying so, without running Vita3K. So does an update, DLC, a theme or a PlayStation Mobile package, and a package smaller than its header says (an incomplete download). PS3 and PSP packages keep their "not supported by inbox yet" outcome.
- A game already in the library is not installed again; its licence and launcher are added if they are missing.
- If Vita3K fails, the reason quotes its last error line (`header signature is invalid` for a zRIF of another game, `Failed to load pkg file` for an unreadable package), with the zRIF redacted.

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
