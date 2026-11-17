---
title: "Anbernic RG40XX V (Knulli)"
---

The `anbernic-rg40xxv` profile is for an Anbernic RG40XX V that runs [Knulli](https://knulli.org), the Batocera-based system for Allwinner H700 handhelds: EmulationStation as the frontend, RetroArch for most systems, DraStic for the Nintendo DS, and a data partition (`/userdata`) formatted as exFAT. It was checked on Knulli 20260511 (built on Batocera 42) with Syncthing 2.1.7. Nothing in it is specific to the RG40XX V, so the other handhelds Knulli runs on with the same layout can use it too.

Knulli is not a general-purpose Linux, which shapes most of this page: the root filesystem is rebuilt at every update and only `/userdata` survives, everything runs as root, there is no systemd, no git, no `ssh-keygen` and no package manager for tools. So warpzone is installed by hand, nothing updates it, and nothing runs on a timer.

## What it does differently

- **The library has its own folder, `/userdata/warpzone/library`.** Games land in `/userdata/warpzone/library/roms/<system>` and BIOS files in `/userdata/warpzone/library/bios`, the same layout as on the hub. Knulli's own `/userdata/roms` is left alone.
- **No symlinks, no permissions.** exFAT has neither. Every folder of the device is created with Syncthing's `ignorePerms` on (`ignore_perms = true`), and nothing in the profile relies on a symlink.
- **RetroArch is configured through `knulli.conf`.** Knulli writes RetroArch's config again at every launch, so a key set in `retroarch.cfg` is lost. Its launcher copies every `global.retroarch.<key>` line of `/userdata/system/knulli.conf` into that config last, so that is where the profile puts the keys (`[retroarch] format = "batocera-conf"`, see [Frontend and RetroArch overrides](/warpzone/development/design/profiles/#frontend-and-retroarch-overrides)).
- **Only the hub's systems move.** Knulli keeps ports, tools and game engines in `/userdata/roms` too (PortMaster, pygame, the tools menu), so the system list copy moves only the systems the profile names (`[es_systems] systems`); the others keep reading `/userdata/roms`.
- **No ES-DE, songs, tools or emulator settings layer.** A device that lists `tools`, `songs` or `settings` is refused with a clear message. `frontend` is accepted and delivers the scraped artwork, see [Artwork](#artwork).

## Before you start

Enable SSH in Knulli (system settings, services) and change the root password. Every command below runs on the console as root, over SSH.

### Syncthing v2

Knulli ships Syncthing 1.x, and warpzone needs v2. Download the `linux-arm64` release from [Syncthing's releases](https://github.com/syncthing/syncthing/releases) on a computer, check its SHA-256 against the release's signed `sha256sum.txt.asc`, and copy the binary to the console:

```bash
mkdir -p /userdata/system/warpzone/bin
# copy the `syncthing` binary of the release here, then:
chmod 755 /userdata/system/warpzone/bin/syncthing
/userdata/system/warpzone/bin/syncthing --version
```

Generate its identity and config in `/userdata/system/configs/syncthing`, the folder Knulli's own scripts read (see [Saves](#saves)):

```bash
STNODEFAULTFOLDER=1 /userdata/system/warpzone/bin/syncthing generate \
    --home=/userdata/system/configs/syncthing --gui-user=admin --gui-password=- --no-port-probing
```

It prints the device ID; it goes in the config below. The GUI listens on `127.0.0.1` only. If the console syncs only at home (or over a VPN to the hub), turn off global discovery, relays and NAT traversal in `config.xml` before the first start, and list the hub's addresses in `[hub] addresses`.

Start it from a user service, `/userdata/system/services/syncthing_wz`:

```sh
#!/bin/sh
# warpzone: Syncthing v2 for the hub's folders. Knulli's own `syncthing` service is v1 and
# stays off; the two must not run together (same folder, same ports).
BIN=/userdata/system/warpzone/bin/syncthing
CONF=/userdata/system/configs/syncthing
LOG=/userdata/system/logs/syncthing-wz.log
PID=/var/run/syncthing-wz.pid

case "$1" in
start)
    [ -x "$BIN" ] || exit 1
    STNODEFAULTFOLDER=1 HOME=/userdata/system start-stop-daemon -S -b -q -m -p "$PID" -x "$BIN" -- \
        serve --home="$CONF" --no-browser --no-upgrade --no-port-probing \
        --log-file="$LOG" --log-max-size=1048576 --log-max-old-files=1
    ;;
stop)
    start-stop-daemon -K -q -p "$PID" && rm -f "$PID"
    ;;
restart)
    "$0" stop
    sleep 2
    "$0" start
    ;;
status)
    [ -f "$PID" ] && kill -0 "$(cat "$PID")" 2>/dev/null && echo running || echo stopped
    ;;
esac
```

```bash
chmod 755 /userdata/system/services/syncthing_wz
knulli-services enable syncthing_wz
knulli-services start syncthing_wz
```

Never enable Knulli's own `syncthing` service next to it.

### warpzone

`python3 -m venv` does not work on exFAT (it makes a symlink), and `updater.sh` needs git, so the venv is made by hand. warpzone is standard library only, and Knulli's Python 3.12 is enough:

```bash
V=/userdata/system/warpzone/venv
mkdir -p $V/bin $V/lib/python3.12/site-packages
cp "$(readlink -f /usr/bin/python3)" $V/bin/python3
printf 'home = /usr/bin\ninclude-system-site-packages = false\nversion = 3.12.8\n' > $V/pyvenv.cfg
# copy the `warpzone` folder of a release into $V/lib/python3.12/site-packages/
```

Then a small launcher, `/userdata/system/warpzone/bin/warpzone`, that names Syncthing's folder:

```sh
#!/bin/sh
export STHOMEDIR=/userdata/system/configs/syncthing
exec /userdata/system/warpzone/venv/bin/python3 -m warpzone "$@"
```

To update warpzone, replace the `warpzone` folder in `site-packages` with the one of the new release and run `provision device` again.

## Config

```toml
[devices.rg40xxv]
device_id = "XXXXXXX-XXXXXXX-XXXXXXX-XXXXXXX-XXXXXXX-XXXXXXX-XXXXXXX-XXXXXXX"
profile = "anbernic-rg40xxv"
folders = ["library", "saves", "frontend"]
self_update = false
auto_prune = false
```

`self_update` and `auto_prune` must be off: both install `systemd --user` timers. Put the config at `/userdata/system/.config/warpzone/config.toml` on the console (`$HOME` is `/userdata/system`), with the hub's API key file it names, like on any other device.

On the hub, make `patterns-rg40xxv.stignore` in `library/` before the first `provision hub` (see [Whitelists](/warpzone/setup/whitelists/)). Whitelist systems by the hub's folder names: the profile maps Knulli's names to them (`megadrive` to `genesis`, `segacd` to `megacd`, `lynx` to `atarilynx`, and so on).

Then, on the console, with EmulationStation closed (the screen goes black until it starts again):

```bash
/etc/init.d/S31emulationstation stop
/userdata/system/warpzone/bin/warpzone provision device rg40xxv
/etc/init.d/S31emulationstation start
```

`provision device` creates the Syncthing folders, writes the system list copy and adds the two `global.retroarch.*` lines to `knulli.conf` (backing the file up first). It needs EmulationStation closed only for the system list and the gamelists; the folders it can create with EmulationStation open, and says what it left for later.

## The system list copy

Knulli's EmulationStation reads `/userdata/system/configs/emulationstation/es_systems.cfg` in place of `/usr/share/emulationstation/es_systems.cfg`. `provision device` writes the first as a copy of the second in which the `<path>` of each system the profile names moves from `/userdata/roms/<system>` to the library, under the hub's folder name. Every other byte is kept. A moved system lists only the hub's games: ROMs left in its `/userdata/roms` folder stay on the card but are not listed any more.

EmulationStation looks for systems only when it starts, so a system whose first game arrives while it runs appears after a restart or "Update gamelists".

## RetroArch

`provision device` sets in `knulli.conf`:

- `global.retroarch.system_directory` to the library's `bios` folder, so the cores read the BIOS files the hub delivers. They no longer see `/userdata/bios`: copy what Knulli put there that a core needs (blueMSX's `Machines` and `Databases`, for example) to the hub's `library/bios`.
- `global.retroarch.savefile_directory` to `/userdata/warpzone/saves/retroarch`, the folder that syncs with the hub's `saves/retroarch/saves`, the same as on the Steam Deck and the RG351MP.

Save states stay where Knulli keeps them (`/userdata/saves/<system>`): its save state manager looks for them there, so they are not synced. Saves made before `provision device` stay in `/userdata/saves/<system>`; copy the `.srm` files to `/userdata/warpzone/saves/retroarch` once.

## Saves

Syncthing's file watcher gets no events on Knulli's exFAT card, so a new save would wait for the folder's next full scan. Knulli already solves it: after every game, its `game-end` script asks Syncthing to scan when `/userdata` is not ext4. It does so when a service whose name contains `syncthing` is enabled, `syncthing.autoscan=1` is set in `knulli.conf` (the default) and Syncthing's config is in `/userdata/system/configs/syncthing`, which is why the setup above uses those names. A save reaches the hub a few seconds after you quit the game, as long as the console is awake and connected: quit games from the menu, and give it a moment before turning the console off.

Nintendo DS saves work like on the [RG351MP](/warpzone/guides/anbernic/#nintendo-ds-saves): DraStic keeps `.dsv` files in `/userdata/saves/nds/advanced_drastic/saves`, and `warpzone saves convert` reconciles them with the hub's raw `.sav` files around every DraStic launch.

## Artwork

With `frontend`, the console receives the hub's ES-DE scrape (the covers and the logos, for the systems in its library) in `/userdata/warpzone/media` and `/userdata/warpzone/es-de`, and the `batocera` converter writes each system's `gamelist.xml` next to its ROMs, keeping what the player did (favourites, play count, last played and Knulli's `<gametime>`). The scrape itself is made once, with ES-DE on any computer that writes to the hub's `frontend/` folders (see [Frontend artwork](/warpzone/development/design/frontend-artwork/)).

Knulli has no systemd, so nothing rewrites the gamelists by itself. After a new scrape, or when games arrived, run with EmulationStation closed:

```bash
/etc/init.d/S31emulationstation stop
/userdata/system/warpzone/bin/warpzone frontend refresh rg40xxv
/etc/init.d/S31emulationstation start
```

To run it from the console, put a script in `/userdata/roms/ports` that starts those three lines in a detached process (`setsid`, after a few seconds) and returns at once: it cannot stop EmulationStation while EmulationStation waits for it.

Knulli's own scraper is still usable on the console: what it downloads next to the ROMs (`images/`, `videos/`, `manuals/`) is kept out of the library folder by the profile.

## Doctor

`warpzone doctor` on the console checks the system list copy, the RetroArch keys in `knulli.conf`, the save conversion and the generated gamelists, as on the RG351MP.
