---
name: add-games
description: Use when the user wants to add new games, ROMs, disc images, Switch files, 3DS, PS3, PS4, Vita, Xbox, Wii U or arcade dumps to the warpzone library. Every new game goes through warpzone inbox; never copy files into the library by hand.
---

# Add games through the inbox

Every new game goes through `warpzone inbox`, without exception. It identifies the dump by content, converts it to the format the library holds, verifies it, places it and files the original. Reference: [inbox.md](../../../website/src/content/docs/setup/inbox.md).

`inbox` reports first and acts only with `--apply`. It runs on a computer that mounts the hub's share (the usual way, since a NAS is slow at conversions) or in the hub's container, not on a console; the two ways and their tools are in [inbox.md](../../../website/src/content/docs/setup/inbox.md#where-inbox-runs), and `warpzone doctor` checks the one in use.

## Hard rules

- Never copy, move or rename files into `library/` or around `inbox/` yourself, and never edit `inbox/_done` or `inbox/_revisar`. If `inbox` cannot place a format, stop and tell the user that support has to be added to warpzone first (a code change with tests and docs). Do not work around it.
- Do not run `--apply` until the user has seen the report and said yes.
- Never print API keys (`RA_API_KEY` is optional and is never echoed by warpzone), and never print, copy into chat or look up a zRIF: the user keeps them in the `vita_zrif` file, and warpzone shows them only as `***`.
- warpzone downloads no games, BIOS files, DATs or keys. If something is missing, the user supplies their own legally obtained copy.

## Steps

1. Check the setup with `warpzone doctor`. `inbox` needs the `[inbox]` table in the config (`root`, `dats_dir`, optional tool paths) or `--root` and `--dats`. Missing tools are shown with their fix; installing them changes the machine, so ask the user first and install only what the dumps need (for example `chdman` for discs, `nsz` plus the user's keys for Switch `.nsz`, `orbis-pkg-util` for PS4 `.pkg`, Vita3K plus the user's zRIF file in `[inbox] vita_zrif` for PS Vita `.pkg`).
2. Make sure the dumps are in `inbox/` on the hub's share (the user drops them there). Items modified in the last 30 seconds are skipped on purpose; wait for copies to finish.
3. Report, which changes nothing:

   ```bash
   warpzone inbox
   ```

   Add `--root PATH`, `--dats PATH` or `--work-dir PATH` only when the config does not hold them. Show the user the output.
4. Go through the report with the user. For each item it states the system, the destination path and the whitelist line (`!/roms/...`). Items for review (no DAT match, incomplete multi-disc, encrypted 3DS, `.xci`, update or DLC packages (a PS4 one is placed when its base game is in the library; it goes to review when the game is missing, or when the update is older than the one in the library), a Vita `.pkg` with no zRIF in the `vita_zrif` file, "not supported by inbox yet") are not placed. A Vita `.pkg` without its zRIF needs the user to add the line to their key file ([PS Vita packages](../../../website/src/content/docs/setup/inbox.md#ps-vita-packages)); do not search for keys yourself. If the format is unsupported, apply the hard rule above; once support has been added, the user moves the item from `inbox/_revisar/` back to `inbox/` to try again.
5. After the user's explicit OK:

   ```bash
   warpzone inbox --apply
   ```

   Exit code `1` means some item needs review; read its `inbox/_revisar/<item>.reason.txt` to the user. Nothing is overwritten: the same content is "already in the library", different content under the same name goes to review.
6. Arcade sets and PS3 games can get an ES-DE `<altemulator>` entry, which `--apply` writes into the hub's gamelist. Ask the user to quit ES-DE on the consoles first, or the edit may be overwritten ([Gamelist edits](../../../website/src/content/docs/setup/inbox.md#gamelist-edits)).
7. warpzone does not edit whitelists. Hand the printed `whitelist:` lines to the `edit-whitelist` skill for the consoles that should get the games.

## Check

Run `warpzone inbox` again: the items should be gone, or listed as already in the library. Optionally run `warpzone bios` to check BIOS coverage for the new system ([bios.md](../../../website/src/content/docs/setup/bios.md)).
