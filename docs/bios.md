# Checking BIOS files

What `warpzone bios` reports, how it decides, and how to accept a BIOS you changed on purpose.

Many emulators refuse to start a game without the right BIOS or firmware, and the failure message rarely says which file. `warpzone bios` reads the library and tells you, per system, what is there, what is missing and what has an unexpected hash:

```
$ warpzone bios --root /path/to/library
psx: Sony PlayStation
  ok                  bios: SCPH1001.BIN (matches scph1001.bin)
saturn: Sega Saturn
  wrong (required)    bios: sega_101.bin: md5 cb37ed68..., expected 85ec9ca4...
ps2: Sony PlayStation 2
  missing (required)  bios: one of ps2-0100j-20000117.bin, ps2-0100jd-20000117.bin, ... (66 in all)
                      unlocks: PlayStation 2 games; one dump of the console region you play is enough
3do: Panasonic 3DO: no games in roms/{3do}, not checked
unknown files in bios/: 3
  (list them with --all)
items: 12 ok, 1 missing, 1 wrong
2 required item(s) missing or wrong
```

## Behaviour

- It is read-only and exits 1 when a required item is missing or wrong, so it also works in a script.
- Only systems that have games are judged. If the ROM folders of a system (for example `roms/3do`) hold nothing but EmuDeck placeholders, media or the BIOS zips themselves, the system is shown as "no games, not checked" and never affects the exit code. Naming it with `--system` checks it anyway.
- Without `--root` it uses this device's `~/Emulation` (from the profile) or, on the hub, `<hub root>/library`.
- `--system psx ps2` limits the check. `--all` lists the unknown files in `bios/`.

## What is checked and how

| Item | Check |
|---|---|
| Most consoles | md5 of the file against the manifest. Only files of the expected size are read. |
| "One of" items | A PlayStation needs one BIOS dump, not all of them. |
| MAME zip sets (`neogeo.zip`, `naomi.zip`, `awbios.zip`, ...) | Member name and CRC32, in `roms/mame/`. |
| PS3 system software (`bios/ps3/PS3UPDAT.PUP`) | Signature, since there is no fixed hash. |
| Cemu `bios/cemu/keys.txt` (optional, only for WUD/WUX disc images) | Presence and shape. The content is never printed or hashed. |
| Xbox | `mcpx_1.0.bin` by md5 and a 1 MiB flash BIOS (`Complex_4627v1.03.bin`, the name EmuDeck's xemu config uses) by name and size. |

The PS3 firmware and `keys.txt` are staging copies: RPCS3 installs the firmware once, and Cemu reads its keys from its own folder.

`warpzone bios` also warns when `bios/eden/firmware` holds subfolders or `.zip`/`.7z` archives. Eden reads only loose `.nca` files directly in that folder, so unpack a firmware release there rather than leaving it in a folder or an archive. The warning does not affect the exit code and appears only when every system is checked.

## Accepting your own BIOS

If you keep a modified or region-free BIOS on purpose, accept its hash in a local file, `bios.toml` next to your `config.toml` (or `--overrides PATH`). It is then reported as "accepted (local)". See [`examples/bios.example.toml`](../examples/bios.example.toml).

## Where the data comes from

The manifest only holds file names, sizes and hashes, taken from [libretro-database](https://github.com/libretro/libretro-database) and MAME. `warpzone` never downloads a BIOS and this repository contains none. Details in [design.md](design.md#bios-check).
