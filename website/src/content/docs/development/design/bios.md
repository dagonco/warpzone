---
title: "BIOS check"
---

`warpzone bios` compares a library folder with a manifest shipped in the package
(`warpzone/data/bios.toml`) and reports what is present and correct, missing, or
has an unexpected hash. The contract:

- **Never downloads, never ships.** The manifest holds file names, sizes, md5
  hashes, zip member CRC32s and file signatures, all public metadata, and
  nothing else. The command only reads the library and writes nothing.
- **Data and curation.** File entries come from libretro-database
  `dat/System.dat`; the MAME zip sets (`neogeo.zip`, `naomi.zip`, `awbios.zip`,
  `bubsys.zip`, `ym2413.zip`, `qsound_hle.zip`) from MAME 0.282's own source.
  The header of `bios.toml` says where each part came from and how to
  regenerate it. Which items are required, and the groupings, are decided by
  hand.
- **Items.** An item is one thing to have and is satisfied when any one of its
  files is ok, which is how "one PlayStation BIOS of your region" is expressed.
  `any_name` items also accept a file of the same folder and size whose hash
  matches under another name, for emulators that identify a BIOS by content.
  An item with no hash (DS `firmware.bin`, which differs per console) checks the
  size only. Zip items live under `roms/` and pass when every listed member has
  the listed CRC32; extra members are ignored.
- **Kinds of check.** A file entry is judged in one of four ways (`check` in the
  manifest). `hash` (the default) is size and md5. `magic` is for official
  firmware that has no fixed hash (PS3 `PS3UPDAT.PUP`): the file must start with fixed bytes, and only that
  many bytes are read. `present` is existence of a non-empty file. `keys` is for
  a secret keys file (Cemu `keys.txt`): the file is read only to answer whether
  it looks like one (hex-led lines, `#` comments), and the verdict is a fixed
  text: its content is never printed, hashed or placed in a message. The Xbox
  flash BIOS has no fixed hash, so any 1 MiB file named `Complex_4627v1.03.bin`
  (the name EmuDeck's xemu config uses) or `Complex_4627.bin` passes on size
  alone. The xemu HDD image is not checked: it is per device, holds the saves and
  lives in `storage/xemu/`. The PS3 firmware and Cemu's `keys.txt` in `bios/` are
  staging copies (RPCS3 installs the firmware once; Cemu reads its keys from
  its own folder). Xbox, PS3 and Wii U are judged by the `xbox`, `ps3` and `wiiu`
  ROM folders; `mlc01/` (Cemu saves and installed titles) does not count as games.
- **Reading files.** The `bios/` folder is walked once, never following
  symlinks, and `skip_dirs` (`eden`, `azahar`: console keys and firmware) are
  neither listed nor opened. A file is hashed, in chunks, only when its size
  equals the expected one. Zips are read through their central directory.
- **Local overrides.** `bios.toml` next to the private config (or
  `--overrides PATH`) may list extra accepted md5 hashes per manifest file
  (`[saturn.accept]`) and items to skip (`[nds] unwanted = ["firmware"]`). A
  file accepted that way is reported "accepted (local)". Unknown systems, files
  or items in that file are errors, so a typo cannot silently accept nothing.
- **Games decide what is judged.** Each system lists the `roms/` folders that
  hold its games (`games` in the manifest, EmuDeck/ES-DE names, for example
  `segacd = [segacd, megacd, megacdjp]`). A folder has games when it holds any
  file other than the EmuDeck placeholders (`metadata.txt`, `systeminfo.txt`),
  downloaded media, hidden or OS junk files and the BIOS zips of the manifest
  (`is_game_file`, `has_games`; the walk stops at the first game). A system
  with no games is reported as one line, "no games, not checked", and never
  affects the exit code, so a missing BIOS for a console nobody plays is not a
  false alarm. `--system NAME` forces the check. The MAME sets (Neo Geo, NAOMI,
  Atomiswave, device sets) share `mame`, `arcade` and `fbneo` with their own
  folder, so any arcade game makes them count.
- **Eden firmware layout.** `skip_dirs` hides `eden/` from the manifest checks,
  but `bios/eden/firmware` gets one layout check (`judge_firmware`, pure, over
  the names directly inside it and whether each is a folder). Eden reads only
  loose `.nca` files in that folder, so subfolders and `.zip`/`.7z` archives are
  each reported as a `warning:` line (and one more when no loose `.nca` is
  there at all). Nothing is opened. It runs only when every system is checked
  and never changes the exit code.
- **Exit code.** `0` when no required item is missing or wrong; `1` otherwise;
  `2` for a usage or override-file error. A wrong file that sits next to an ok
  alternative is reported but does not fail the run. Optional items never fail it.
- **Root.** `--root` is the library folder (the one holding `bios/` and
  `roms/`). Without it: the first existing `library` path of the configured
  profiles, then `<hub root>/library`. The list of unknown files in `bios/` is
  only computed when every system is checked.

The decisions (`judge_file`, `judge_zip`, `judge_magic`, `judge_present`,
`judge_keys`, `judge_firmware`, `looks_like_keys`, `settle`, `unknown_files`,
`default_root`, `is_game_file`, `has_games`, `render`) are pure and unit-tested without a filesystem; `Library`
and `check_library` are the only parts that touch the disk.
