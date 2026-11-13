---
title: "Adding new games: the inbox"
---

How `warpzone inbox` identifies, converts and files new dumps, including Switch, 3DS, Wii and GameCube `.rvz`, PS3, PS4, PS Vita, Xbox, Xbox 360, Wii U and arcade.

Drop new dumps (files, folders or archives) in `inbox/` on the hub's share and run `warpzone inbox`, either on a computer that mounts the share or in the hub's container ([the two ways](#where-inbox-runs)). Conversions are heavy, so a computer is the usual choice. Without `--apply` it only reports:

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

## Where inbox runs

The command and its result are the same in both places; what differs is where the tools come from and what the `[inbox]` paths mean.

- **On a computer that mounts the hub's share** (a desktop or laptop). You install the tools there, keep the DATs in a folder there, and `[inbox] root` is where the share is mounted on that computer. This is the usual way: conversions use that computer's processor, and the files cross the network once.
- **In the hub's container**, the one from [the hub container guide](/warpzone/setup/hub-container/). The tools come from the image, so nothing is installed on the NAS; the share is read where it lives (no network), and every path in `[inbox]` is a path inside the container. Use it when you have no computer to spare. A NAS processor is slow with `chdman`, and with `dolphin-tool` and `nsz` on big images.

| | On a computer | In the hub container |
|---|---|---|
| Tools | installed by you (Homebrew, apt, ...), found on `PATH` or at the `[inbox]` paths | shipped by the image, at the image's paths |
| Config | the config file of that computer | the config folder mounted at `/config` (`WARPZONE_CONFIG`) |
| `[inbox] root` | where the share is mounted on that computer | the hub root as mounted in the container: the same path as `[hub] root` |
| `dats_dir` | a clone of libretro-database on that computer | a folder you mount into the container (the image holds no DATs) |
| `rahasher` | the RAHasher you built, optional | `"/usr/local/bin/RAHasher"`, the copy in the image; inbox uses it only when this is set |
| `work_dir` | optional (the system temp folder) | set it to a mounted folder: the container's `/tmp` is memory |

Which tools exist in each place:

| Tool | Needed for | On a computer | In the hub image |
|---|---|---|---|
| `chdman` | CHD conversions (`psx`, `ps2`, `saturn`, `dreamcast`) | install `mame-tools` or `rom-tools` | `/usr/bin/chdman` (on `PATH`), from Debian's `mame-tools` |
| `bsdtar` | `.7z` and `.rar` archives | install `libarchive-tools` (ships with macOS) | `/usr/bin/bsdtar` (on `PATH`), from Debian's `libarchive-tools` |
| `RAHasher` | the RetroAchievements hash check | build it, set `rahasher` | `/usr/local/bin/RAHasher` |
| `dolphin-tool` | `.gcz` GameCube images | install Dolphin | `/usr/local/bin/dolphin-tool` (on `PATH`), built from Dolphin's source |
| `nsz` | Switch metadata, `.nsz` decompression | `uv tool install nsz` | `/usr/local/bin/nsz` (on `PATH`), in its own virtual environment |
| `orbis-pkg-util` | PS4 `.pkg` games | `cargo install orbis-pkg-util` | `/usr/local/bin/orbis-pkg-util` (on `PATH`), built from its crate |
| `Skyscraper` | scraping what `inbox --apply` places and `warpzone scrape` | see [Scraping](/warpzone/setup/scrape/#install-skyscraper) | `/usr/local/bin/Skyscraper` (on `PATH`), built from the Gemba fork; its `config.ini` and cache live on the volume mounted at `/skyscraper` |
| `vita3k` | PS Vita `.pkg` games | install it | not in the image |

An item that needs a tool the place lacks goes to `inbox/_revisar/` with the reason "missing tool"; the other items are filed. In the container that is a PS Vita `.pkg`: Vita3K is not in the image (see [PS Vita packages](/warpzone/development/design/inbox/#ps-vita-packages-in-the-container)), so run those on a computer. `chdman`, `bsdtar`, `dolphin-tool`, `nsz`, `orbis-pkg-util` and `Skyscraper` need no `[inbox]` setting in the container: inbox finds them on `PATH`. When the image gains a tool it is on the image's `PATH`, and this table is the place that says so.

What a tool needs besides itself never goes into the image. The Switch keys file (`switch_keys`), the PS Vita zRIF file (`vita_zrif`), `titledb_dir` and the arcade DATs are folders or files you mount from the NAS, read-only, and name in `[inbox]`.

### Running it in the container

Add a service for it to `compose.yaml`, next to `warpzone`. It repeats the mounts and limits of that service, adds the DATs and a work folder, and a profile keeps `docker compose up -d` from starting it:

```yaml
  warpzone-inbox:
    image: warpzone:local
    profiles: ["inbox"]
    user: "1000:1000"               # the same uid and gid as the warpzone service
    # group_add: ["101"]            # if the warpzone service needs it
    init: true
    read_only: true
    tmpfs:
      - /tmp
    cap_drop:
      - ALL
    security_opt:
      - no-new-privileges:true
    mem_limit: 2g                   # more than the server: conversions and archives
    volumes:
      - /srv/games:/srv/games             # the hub root (an example path), read-write, same path as [hub] root
      - ./config:/config:ro
      - ./dats:/dats:ro                   # libretro-database, and titledb and the arcade DATs if you use them (see below)
      - ./keys:/keys:ro                   # your Switch keys and PS Vita zRIF file; not on the share, not in the image
      - ./work:/work                      # unpacked archives and conversions: room for a few GiB
      - ./skyscraper:/skyscraper          # Skyscraper's config.ini and cache (see Scraping), read-write
    env_file:
      - ./warpzone.env                    # RA_API_KEY, to look RA hashes up
    environment:
      XDG_CACHE_HOME: /work/cache         # the parsed DATs are cached here
```

In the same `config.toml` the container reads (`/config/config.toml`):

```toml
[inbox]
root = "/srv/games"                          # the hub root as mounted in the container (example path)
dats_dir = "/dats/libretro-database/metadat"  # redump/ and no-intro/ inside
work_dir = "/work/tmp"
rahasher = "/usr/local/bin/RAHasher"          # optional
titledb_dir = "/dats/titledb"                 # optional: Switch names (a clone of blawar/titledb)
switch_keys = "/keys/prod.keys"               # Switch .nsp and .nsz: nsz reads it, warpzone only passes the path
vita_zrif = "/keys/PSV_GAMES.tsv"             # PS Vita .pkg: needs Vita3K, which the image lacks
mame_dat = "/dats/mame0282lx.zip"             # optional: arcade (see "Arcade sets")
```

[`examples/compose.example.yaml`](https://github.com/dagonco/warpzone/blob/main/examples/compose.example.yaml) has the same service, with the `[inbox]` keys as comments. Make the `dats`, `keys`, `work` and `skyscraper` folders next to `compose.yaml` (`work` and `skyscraper` writable by the service's uid; `keys` readable by it and by nobody else if the NAS has other users), put your keys file in `keys/`, and clone the DATs into `dats/`: `git clone --depth 1 --filter=blob:none --sparse https://github.com/libretro/libretro-database dats/libretro-database && git -C dats/libretro-database sparse-checkout set metadat/redump metadat/no-intro`. warpzone never downloads them. Then, from the folder with `compose.yaml`:

```bash
docker compose run --rm warpzone-inbox inbox            # report only
docker compose run --rm warpzone-inbox inbox --apply
docker compose run --rm warpzone-inbox doctor           # checks the image's tools and the mounts
```

`docker compose run` starts a second container from the service for one command, so the server can keep running; the lock described below keeps two `--apply` runs from working on the inbox at once. To use the running server's container instead, mount the same `dats` and `work` folders in the `warpzone` service, raise its `mem_limit`, and run `docker compose exec warpzone warpzone inbox`.

`warpzone doctor` tells which way it is running by itself (inside a container it checks the image's tools and the mounted folders; elsewhere the tools on `PATH` or at the configured paths), so the same command is right in both places. See [the doctor guide](/warpzone/operation/doctor/).

## Configuration

Set the `[inbox]` table of the config (see [`examples/config.example.toml`](https://github.com/dagonco/warpzone/blob/main/examples/config.example.toml)). The example below is for a computer; in the container the same keys hold paths inside it, as in [Running it in the container](#running-it-in-the-container). With `--root` and `--dats` it needs no config file.

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

`bsdtar`, `dolphin_tool`, `nsz`, `orbis_pkg_util`, `vita3k` and `switch_keys` work the same way, and `work_dir` moves the temporary files (they can take several GiB). A tool that hangs is killed after `tool_timeout_minutes` (default 240) and only its item goes to review, so an unattended run does not stay stuck. Run `warpzone doctor` to see what is missing ([the doctor guide](/warpzone/operation/doctor/)). Details in [the design document](/warpzone/development/design/inbox/).

## What it does for each item

1. **Unpacks** `.zip` (and `.7z` / `.rar` with `bsdtar`) into a local work folder, never onto the share. Archive members are untrusted: absolute paths, `..` and symbolic links are refused. The names a DAT gives are untrusted too: a game, set or rom name that is not a plain file name (it holds a `/`, a `\` or a control character, or is `.` or `..`) sends that game to review and nothing is written under it.
2. **Identifies by content**, whatever the files are called: size and CRC32 (and SHA1, when the DAT has it) against the Redump and No-Intro DATs of [libretro-database](https://github.com/libretro/libretro-database) (`metadat/redump`, `metadat/no-intro`). A game matches when all its DAT roms are present. DATs are parsed only when needed and cached in `$XDG_CACHE_HOME/warpzone/dats`.
3. **Converts** to what the library already holds:

   | System | Result |
   |---|---|
   | `psx`, `saturn`, `dreamcast` | `<game>.chd` (`chdman createcd` from cue/bin or gdi), then `chdman verify` |
   | `ps2` | `<game>.chd` (`createdvd` for an `.iso`, `createcd` for cue/bin), then `verify` |
   | `psp` | the plain `.iso`, its CRC32 checked against the DAT |
   | `gc` | `.iso` stays `.iso` (the report shows the `dolphin-tool convert -f rvz ...` command); a `.gcz` is identified with `dolphin-tool` and kept as it is. An `.rvz` or `.wia` is read by its header instead, see [Wii and GameCube compressed images](#wii-and-gamecube-compressed-images) |
   | `nes`, `snes`, `n64`, `gb`, `gbc`, `gba`, `nds`, `genesis`, `mastersystem`, `gamegear`, `atari2600`, `atari7800`, `atarilynx`, `atarijaguar`, `wonderswan`, `wonderswancolor`, `sega32x` | `<game>.zip` holding the ROM under its DAT name |

   A multi-disc game of a CHD system goes in `<game without disc tag>.m3u/` with every disc and a `<game>.m3u` playlist, and only when all its discs are in the item; otherwise it goes to review.

   A PlayStation disc that comes with an `.sbi` (the LibCrypt protection data of some PAL games, named like its cue or data track) also goes in a `<game>.m3u/` folder, with the `.sbi` renamed after the disc, because a CHD does not keep that data and DuckStation and Beetle PSX read it from `<disc>.sbi` next to the disc. The report shows `note: LibCrypt: <game>.sbi goes with its disc`.
4. **Checks RetroAchievements** (optional). With `rahasher` configured it hashes the source and the result and refuses to place a game whose hash changed. With `RA_API_KEY` in the environment it looks the hash up and reports `supported (<title>)` or `hash not linked`. The key is never printed.
5. **Places** the result in `library/roms/<system>/` (copied to `<name>.part`, read back, then renamed). It never overwrites: the same content is reported as "already in the library", different content under the same name goes to review.
6. **Files the original** in `inbox/_done/<date>/`, or in `inbox/_revisar/` with a `<item>.reason.txt` (no DAT match (unless you dropped the file in `_unverified/<system>/`), incomplete multi-disc, conversion or verify failed, RA hash changed, conflict, missing tool). On every `--apply`, `_done` folders named like a date and older than 7 days are deleted, nothing else.

**One `--apply` at a time.** While it works, `--apply` holds a lock file, `inbox/.warpzone.lock`, with the host name, the process id and the start time of the run, and removes it when it ends (also after an error or Ctrl-C). A second `--apply` on the same hub, from this machine or another desktop that mounts the share, stops with exit code 2 and says who holds the lock and since when; a report without `--apply` never needs the lock. If a run was killed and left its lock behind, the lock counts as stale once it is older than 12 hours or when its process is gone from the machine that took it (a lock from another machine cannot be checked, so it only goes stale by age). A stale lock is never taken silently: the refusal says so, and `warpzone inbox --apply --force-lock` takes it over. A lock that is still live is never taken, with or without `--force-lock`.

Items modified in the last 30 seconds are left alone (they may still be copying), and `_*` folders (except the `<system>` folders inside `_unverified`, see [Homebrew and other roms no DAT knows](#homebrew-and-other-roms-no-dat-knows)), hidden files, `@eaDir` and `.DS_Store` are never items. warpzone does not edit whitelists: add the printed lines to the device whitelists yourself ([Whitelists and pruning](/warpzone/setup/whitelists/)). Exit code 1 when any item needs review.

A PS Vita dump kept as a game folder is filed (see [PS Vita games](#ps-vita-games)), and a Vita `.pkg` is installed with Vita3K first (see [PS Vita packages](#ps-vita-packages)). `.vpk` files, PS3 or Xbox games kept as extracted folders and PS3 `.pkg` files are not supported yet and go to review ("not supported by inbox yet").

## What each run leaves behind

Every `--apply` that processed at least one item writes a record of the run to `catalog/inbox/runs/` on the hub: what was placed (with the whitelist line of each game), what went to `_revisar` and why, and where each original went. The newest 50 are kept. The record holds no absolute path, key or zRIF.

To be told when something needs you, add `--notify-url URL` (an `http://` or `https://` address, for example a Home Assistant webhook). It sends one JSON summary when some item went to review, and nothing otherwise. The address is never printed, and a failed send is only a warning.

```bash
warpzone inbox --apply --notify-url http://homeassistant.local:8123/api/webhook/your-id
```

## Descriptions and media

With Skyscraper installed (on `PATH`, or `skyscraper` in the `[inbox]` table), `warpzone inbox --apply` also scrapes the games it has just placed: it adds their gamelist entry, covers, screenshots and videos to the hub, after the files are placed and the catalog is updated. A failure there is only a warning, and `--no-scrape` skips it for one run. Without `--apply` the report says how many games would be scraped. Set-up, ScreenScraper accounts, language and region, and `warpzone scrape` for the games already in the library are in [Scraping](/warpzone/setup/scrape/). The hub container ships Skyscraper, so `inbox` there scrapes too; it needs the `./skyscraper` folder mounted at `/skyscraper` (see [Scraping in the container](/warpzone/setup/hub-container/#scraping-in-the-container)).

## Homebrew and other roms no DAT knows

Homebrew, translations and aftermarket releases are in no Redump or No-Intro DAT, so `inbox` sends them to review as "no DAT match". When you know a file is one of those, say so by dropping it (or the archive that holds it) in `inbox/_unverified/<system>/`, with `<system>` the library folder name: `nes`, `snes`, `n64`, `gb`, `gbc`, `gba`, `genesis`, `mastersystem` or `gamegear`. For example `inbox/_unverified/nes/My Homebrew.nes`. Create the folder yourself; `inbox` never does.

```bash
warpzone inbox          # shows "would place (unverified)"
warpzone inbox --apply
```

- The folder is a permission, not a shortcut. A file a DAT recognises is filed the normal way, under the DAT's name, and is not marked.
- A file nothing recognises is filed as `library/roms/<system>/<file name>.zip`, with the rom inside under the same name. A loose file keeps its own name. An archive that holds exactly one rom gives its name to both the entry and the rom inside, so name the archive the way you want the game called (`Witch n' Wiz (World) (Aftermarket) (Unl).zip` is filed as `Witch n' Wiz (World) (Aftermarket) (Unl).zip`, whatever the rom inside is called); an archive with several roms keeps the name of each rom. The report says `not in any DAT (filed from _unverified)`, and the game is marked `unverified` in the catalog.
- Before filing, `inbox` checks that the file is plausibly for that system: an extension the system accepts (`.nes` for `nes`, `.gba` for `gba`, ...), not empty, and for `nes` the `NES` iNES header. A file that fails, or a system folder `inbox` cannot file without a DAT (discs, PSP, GameCube, anything not in the list above), goes to review with the reason; a file placed directly in `_unverified/`, outside a system folder, is left alone and the report names it.
- Nothing is overwritten, the original goes to `_done/` and the RetroAchievements hash is computed as for any game. Whitelist lines are printed as usual.

## Switch games

`.nsp` and `.nsz` files (loose, in a folder or in an archive) are identified by what is inside them, named after [blawar/titledb](https://github.com/blawar/titledb) and filed the way the library already has them:

| Kind | Where | Name |
|---|---|---|
| Game | `library/roms/switch/` | `<Title> [<TitleID>][v<version>][BASE].nsp` |
| Update | `library/extras/switch/updates/<Title>/` | `<Title> [<UpdateTitleID>][v<version>][UPD].nsp` |
| DLC | `library/extras/switch/dlc/<Title>/` | `<Title> - <DLC name> [<DlcTitleID>][v<version>][DLC].nsp` |

The TitleID decides the kind (a game ends in `000`, an update in `800`, anything else is DLC). The version comes from the file's CNMT, read with [`nsz`](https://github.com/nicoboss/nsz) (`uv tool install nsz`), which needs your console's `prod.keys` (`[inbox] switch_keys`, default `~/.switch/prod.keys`). warpzone only hands the path to `nsz` and never reads or prints the file.

Titles come from a local clone of titledb (`git clone --depth 1 https://github.com/blawar/titledb`, set `titledb_dir` to it; `git pull` updates it). Punctuation is made ASCII (`:` becomes ` -`, curly quotes become straight), words in capitals are written normally (short acronyms and roman numerals stay), and unsafe characters are dropped. A DLC that titledb only lists inside a bundle is named `<Bundle> (part N of M)` in the bundle's order.

- `.nsz` is decompressed with `nsz -D` into the work folder and only the `.nsp` is placed. Because `nsz` exits 0 on some damaged files, the result is checked again: `nsz -V` must verify it and its PFS0 header must parse. A ticketless dump has no hash check in `nsz` (it prints `[TICKETLESS]` instead of `[VERIFIED]`); it is placed all the same, with the note "not hash-checked by nsz: ticketless dump".
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
| An update (patch) of that game | `library/roms/ps4/games/<CUSAnnnnn>-UPDATE/` |
| A DLC of that game | `library/roms/ps4/dlc/<CUSAnnnnn>/<entitlement label>/` |

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
- The header shows whether a package is a game, an update or a DLC before anything is unpacked, and the `CATEGORY` of `param.sfo` is checked again afterwards.
- An update and a DLC need the base game in the library: without it they go to review ("install the game first"). For a DLC, see [PS4 DLC](#ps4-dlc) for how the game is chosen.
- A shortcut with that name that runs another game is kept, and the new one gets the title id in its name.
- Without `orbis-pkg-util` the `.pkg` is placed as it is in `library/roms/ps4/` with a note saying so; shadPS4 cannot run it from there.

### PS4 updates

shadPS4 overlays a folder named like the game with `-UPDATE` added over the game, file by file, so the extracted patch goes to `library/roms/ps4/games/<CUSAnnnnn>-UPDATE/` and needs no shortcut of its own:

```
  ps4: CUSA03173-UPDATE: placed
    path: library/roms/ps4/games/CUSA03173-UPDATE
    note: detected: PS4 package: CUSA03173
    whitelist: !/roms/ps4/games/CUSA03173-UPDATE
```

If the library already has that update, inbox compares the `APP_VER` of both (numerically: `01.09` is newer than `01.08`, older than `01.10`):

| The package is | Result |
|---|---|
| the same version | "already in the library"; nothing is touched |
| older | review: "older than the update in the library, v01.10" |
| newer | the library update is replaced; the report says which versions (`replaced the update v01.05 with v01.09`) |

Replacing is the one case where inbox changes a file that is already in the library, and it is safe to do: the package of the old update was filed in `inbox/_done` when it was placed. The new folder is copied and read back beside the old one, checked once more, and swapped in with renames, so the library always holds a complete update, the old or the new. A report without `--apply` cannot know the version of the package before unpacking it, so it prints the version in the library and says it would unpack and compare.

### PS4 DLC

shadPS4 looks for DLC in its `addon_install_dir`, in `<that folder>/<title id of the game>/<entry>/`. The entry is named after the last part of the content id (`UP9000-CUSA00900_00-SPEXPANSIONDLC03` gives `SPEXPANSIONDLC03`), so a DLC goes to `library/roms/ps4/dlc/<CUSAnnnnn>/<entitlement label>/`:

```
  ps4: CUSA00900/SPEXPANSIONDLC03: placed
    path: library/roms/ps4/dlc/CUSA00900/SPEXPANSIONDLC03
    note: detected: PS4 package: CUSA00900
    whitelist: !/roms/ps4/dlc/CUSA00900
```

The whitelist line is the title folder, so one line serves every DLC of that game.

Which game a DLC belongs to is decided by the inbox item. Normally it is the title id inside the package (`CUSA00900` above). When the title id of the DLC is not the one of the game you have, put the package in an inbox folder named after your game, for example `inbox/CUSA03173/dlc.pkg`: the DLC is then filed for that game, whatever title id it carries.

```
  ps4: CUSA03173/SPEXPANSIONDLC03: placed
    path: library/roms/ps4/dlc/CUSA03173/SPEXPANSIONDLC03
    note: detected: PS4 package: CUSA00900
    note: filed for CUSA03173: the inbox folder is named after that game
    whitelist: !/roms/ps4/dlc/CUSA03173
```

Either way the game folder `library/roms/ps4/games/<CUSAnnnnn>/` must already exist, or the DLC goes to review naming the missing title id. Only a folder whose whole name is a title id counts; an archive, or a folder with any other name, files the DLC for the title id it carries. A DLC that is already in the library is reported as "already in the library" and is not compared. A DLC with no data of its own (what some stores sell as an "additional license") makes `orbis-pkg-util` write the license files and then stop with an error about the PFS; inbox accepts that only when the package header says the PFS is empty and `param.sfo` was written, and anything else goes to review as an extraction failure.

The consoles need `~/Emulation/roms/ps4/dlc` as shadPS4's `addon_install_dir`; `provision device` sets it for profiles that declare it ([the EmuDeck guide](/warpzone/guides/emudeck/#shadps4-install-directory)).

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

1. Install Vita3K. On macOS put `Vita3K.app` in `/Applications` (the default), or set `vita3k` in `[inbox]` to the app (a link to the binary inside it works too). On Linux use a plain Vita3K binary, not the AppImage, set in `vita3k` or on PATH as `vita3k`; only the macOS app has been tried.
2. Keep the zRIFs in a file and set `[inbox] vita_zrif` to it. Either one line per game, the content id and the zRIF separated by a tab (lines starting with `#` are comments):

   ```
   EP9000-PCSF00024_00-PDEUAP1648614960<TAB><zRIF>
   ```

   or a TSV with a header row, such as NoPayStation's `PSV_GAMES.tsv`: only the `Content ID` and `zRIF` columns are read, the rest (links included) is ignored. Fields may be in double quotes, and a zRIF may lack its trailing `=`.
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

The report runs nothing: the title id, the title and the licence are read from the package header, its unencrypted `param.sfo` and the zRIF, so all three whitelist lines are known before installing. `--apply` then runs Vita3K in a folder of its own inside the local work folder (it needs about the package's size free) and never touches Vita3K's own data or a console's: it uses Vita3K's portable mode (on macOS a link to the app next to an empty `portable/` folder, on Linux `APPIMAGE` pointing into that folder), and runs `Vita3K --version` first, so that if Vita3K does not use that folder nothing is installed. A Vita3K that cannot be pointed at that folder is never run, and the package goes to review saying why: on macOS a binary that is not inside `Vita3K.app`, on Linux an AppImage (its runtime sets `APPIMAGE` itself) or a wrapper script. `warpzone doctor` warns about such a Vita3K too. Vita3K exits with success even when an install fails, so warpzone checks what it left: the game folder with `eboot.bin` and a `param.sfo` of the same title id, content id and a game category, no `sce_pfs`, and a licence equal to the zRIF. The work folder is deleted afterwards.

- The zRIF is a key and is never printed or written to a reason file: the command line is shown with `***` in its place. While Vita3K runs (under a second for a 200 MiB package) it is on Vita3K's command line, the only way Vita3K takes it, so another user of the machine could see it in the process list.
- A package whose content id has no zRIF in the file, or a zRIF that does not decode to that game's licence, goes to review saying so, without running Vita3K. So does a package whose `param.sfo` has no content id. So does an update, DLC, a theme or a PlayStation Mobile package, and a package smaller than its header says (an incomplete download). PS3 and PSP packages keep their "not supported by inbox yet" outcome.
- A game already in the library is not installed again; its licence and launcher are added if they are missing.
- If the work folder cannot be deleted afterwards, a warning names it: delete it by hand, it holds the game's licence.
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
