---
title: "The hub container and the library UI"
description: "Build the warpzone image, run it on the hub with Compose, and open the library UI."
---

Build the warpzone image, run it on the hub with Compose, and open the library UI in a browser.

A NAS often ships a Python older than 3.11, and warpzone needs 3.11. So `warpzone serve` (the library UI) and `warpzone catalog` run in a small container on the hub: a slim official Python image with warpzone installed, the tools of `inbox` (RAHasher, `chdman`, `bsdtar`, `dolphin-tool`, `nsz` and `orbis-pkg-util`) and Skyscraper, which scrapes descriptions and media, built in, running one process as the user that owns the games share. Nothing else of warpzone needs the container. The consoles and `provision hub` run as before, on other machines, and so does `inbox` by default; it can also run in the container (see [Adding games from the container](#adding-games-from-the-container)). The contract is in [Library UI: the container](/warpzone/development/design/library-ui/#the-container).

You need Docker with Compose v2 on the hub (Synology Container Manager has both), the hub's Syncthing GUI on a LAN address, and the [private config](/warpzone/reference/configuration/) of your setup.

## Build the image

The image installs warpzone from the checkout you build in, so the checkout is the version you run. Check out a release tag (`vX.Y.Z` below; use the newest release that has `warpzone serve`):

```bash
git clone https://github.com/dagonco/warpzone.git && cd warpzone
git checkout vX.Y.Z
docker build -f docker/Dockerfile -t warpzone:local .
```

A NAS may have no git. Download the release instead (`curl -L https://github.com/dagonco/warpzone/archive/refs/tags/vX.Y.Z.tar.gz | tar xz`), then `cd` into the folder it makes and run the `docker build` line.

The build compiles four programs in stages of their own, so it needs the network and time: RAHasher from [RALibretro](https://github.com/RetroAchievements/RALibretro), Skyscraper from the [Gemba fork](https://github.com/Gemba/skyscraper) (against the Qt 6 libraries of Debian, with its configuration files built into the binary; a few minutes, and it builds at the same time as Dolphin), `dolphin-tool` from [Dolphin](https://github.com/dolphin-emu/dolphin) (only that program, with Dolphin's bundled libraries linked into it, but it still compiles Dolphin's emulation core, so it is by far the longest part: it takes about 13 minutes on a four-core x86 machine, where the whole build takes about 15, and several times that on a small NAS) and `orbis-pkg-util` from its [crate](https://crates.io/crates/orbis-pkg-util) with Cargo. `nsz` is installed from PyPI into a virtual environment of its own, so its packages are not warpzone's. The compilers stay in their stages: the image that runs holds Python, warpzone, the built or installed tools (about 75 MB on disk: 21 MB for `dolphin-tool`, 51 MB for `nsz` and its packages, 2 MB each for RAHasher and `orbis-pkg-util`, and the 2 MB Skyscraper binary), and the Debian packages `mame-tools` (`chdman`), `libarchive-tools` (`bsdtar`) and the Qt 6 libraries Skyscraper links (`libqt6gui6`, `libqt6network6`, `libqt6xml6`, `libqt6sql6` and `libqt6sql6-sqlite`), which add about 355 MB on disk to it (about 250 MB of it is the graphics and sound libraries `mame-tools` depends on: SDL2, Mesa and LLVM; Debian packages no `chdman` without them; the Qt libraries add about 105 MB more), and nothing to build with. Everything is pinned in [`docker/Dockerfile`](https://github.com/dagonco/warpzone/blob/main/docker/Dockerfile) and checked when it is fetched: the base images by digest, RALibretro, Skyscraper and Dolphin by commit (the build stops if the tag points elsewhere), `orbis-pkg-util` by the SHA-256 of its crate and the checksums in the crate's own `Cargo.lock`, and `nsz` with its dependencies by version and SHA-256 in [`docker/nsz-requirements.txt`](https://github.com/dagonco/warpzone/blob/main/docker/nsz-requirements.txt).

The tools are free software under their own licences, which differ from warpzone's MIT: RAHasher is GPL-3.0, Skyscraper is GPL-3.0, `dolphin-tool` is GPL-2.0-or-later, `nsz` is MIT and `orbis-pkg-util` is MIT or Apache-2.0 (`chdman` is GPL-2.0-or-later and `bsdtar` BSD, as Debian packages them). None of their source or binaries is in the warpzone repository; the image is built on your machine from the upstream releases, and you do not redistribute it. Keep it that way: if you share the image, you share those programs and their licences with it.

**Vita3K is not in the image**, and neither is anything else for PS Vita packages, so a PS Vita `.pkg` still needs `inbox` on a computer with Vita3K ([why](/warpzone/development/design/inbox/#ps-vita-packages-in-the-container)).

To build on another machine and carry the image over, name the NAS's architecture (`linux/amd64` for an Intel or AMD NAS) and move it as a file:

```bash
docker buildx build --platform linux/amd64 -f docker/Dockerfile -t warpzone:local --load .
docker save warpzone:local | gzip > warpzone-image.tar.gz
# copy the file to the NAS, then there:
gunzip -c warpzone-image.tar.gz | docker load
```

On a Mac with Apple silicon the `amd64` build runs under emulation and takes much longer; the Dockerfile builds natively for `arm64` too.

## Set up the folders and the compose file

Make a folder for the container on the NAS and copy [`examples/compose.example.yaml`](https://github.com/dagonco/warpzone/blob/main/examples/compose.example.yaml) into it as `compose.yaml`:

```
warpzone/
  compose.yaml
  warpzone.env          RA_API_KEY=... (create it even if empty)
  config/
    config.toml         your private config, with the container paths below
    hub.key             the hub Syncthing's API key
    ui.token            one line: the token the UI asks for
```

In `config.toml`, copied from [`examples/config.example.toml`](https://github.com/dagonco/warpzone/blob/main/examples/config.example.toml), the paths are the ones the container sees:

```toml
[hub]
root = "/srv/games"                      # the hub root, as on the NAS (use yours; on a Synology it is usually under /volume1/) and as mounted below
api_url = "https://192.168.1.10:8384"    # a LAN address of the NAS, not 127.0.0.1
api_cert_sha256 = "AB:CD:EF:..."         # the SHA-256 fingerprint of Syncthing's GUI certificate, see below
api_key_file = "/config/hub.key"

[inbox]
rahasher = "/usr/local/bin/RAHasher"     # optional: lets the server keep RA hashes up to date

[serve]
token_file = "/config/ui.token"
```

`api_cert_sha256` lets the container reach Syncthing over `https://` across the bridge network without the API key crossing it in the clear: warpzone accepts only the certificate with that fingerprint. Read it on the NAS from Syncthing's `https-cert.pem` (the file is in its config folder, `syncthing paths` shows where) with `openssl x509 -in https-cert.pem -noout -fingerprint -sha256`, and paste what follows the `=`. See [Getting the fingerprint](/warpzone/reference/configuration/#getting-the-fingerprint). Without the key, use `http://` as before.

Make the token a long random string (`head -c 24 /dev/urandom | base64 | tr -d '/+='` makes one) and keep it to one line. The same applies to `RA_API_KEY` in `warpzone.env`: your RetroAchievements web API key, as `RA_API_KEY=<key>`. Without it the RA game lists are not downloaded and the page shows no achievements.

In `compose.yaml`, change:

- `user:` to the uid and gid that own the games share (`id -u` and `id -g` of that user). The server writes the device whitelists in `library/` and the `catalog/` folder as this user, so it needs write access to them. The files in `config/` must be readable by it too.
- the two paths on the left of the `volumes:` lines, if your share or your config folder are elsewhere. The hub root keeps the same path on both sides: it is the path `[hub] root` names, so the same config is right inside and outside the container.
- `mem_limit`, if you want another limit (512 MB is enough for the server).

**Network.** On the default bridge network the container reaches Syncthing at `[hub] api_url`, so Syncthing's GUI must listen on a LAN address of the NAS (and the NAS firewall must let the container's network in). The GUI port answers both `https://` and `http://`, so the pinned `https://` URL needs nothing turned on in Syncthing. If Syncthing's GUI listens only on `127.0.0.1`, switch the service to the host network instead, as the comments in the compose file say: remove `ports:`, uncomment `network_mode: host` and use `http://127.0.0.1:8384` as `api_url` (and drop `api_cert_sha256`, or keep `https://` with it).

## First run

Do these once, in this order. `docker compose run --rm warpzone ...` starts a second container from the same service for a single command; the server does not need to be running.

1. **Check the config and the mounts.** `docker compose run --rm warpzone check --offline` loads the config and the profiles. If `serve` later stops with exit `2` and says `library/.stfolder` is not there, the hub root is not mounted at `[hub] root`.
2. **Convert the whitelists.** The UI only edits whitelists in its own format. See what the conversion would do, then do it:

   ```bash
   docker compose run --rm warpzone whitelist migrate --dry-run
   docker compose run --rm warpzone whitelist migrate
   ```

   Each file is proven to include the same paths as before and is saved first under `catalog/whitelists/`. A file that cannot be converted is refused and listed. See [`whitelist migrate`](/warpzone/reference/commands/#warpzone-whitelist-migrate).
3. **Build the catalog.** The first run reads every file of the library once to hash it, so on a large library it takes a while; later runs only look at sizes and dates:

   ```bash
   docker compose run --rm warpzone catalog --dry-run
   docker compose run --rm warpzone catalog
   ```
4. **Optional: the RetroAchievements hashes.** `catalog --ra-hash` reads every image of the systems RetroAchievements knows, so on a large library it runs for hours. Run it detached; it writes each system as it goes, and can be stopped and run again, picking up where it left off:

   ```bash
   docker compose run -d --name warpzone-ra-hash warpzone \
       catalog --ra-hash --rahasher /usr/local/bin/RAHasher
   docker logs -f warpzone-ra-hash        # Ctrl-C leaves it running
   docker stop warpzone-ra-hash           # stop it; run it again later to continue
   docker rm warpzone-ra-hash             # once it has finished or been stopped
   ```

   With `[inbox] rahasher` set, the server's own background catalog run hashes the games that lack a hash too, so you can also skip this step and let the server do it over time. Do not run both at once. If the container is killed with exit `137`, it ran out of memory: raise `mem_limit` and run it again.
5. **Optional: the RA game lists, achievements and your progress.** With `RA_API_KEY` set, the server downloads the lists once a day, and the achievements and badge images little by little on every background run. To get them now: `docker compose run --rm warpzone catalog --ra-lists` and then `docker compose run --rm warpzone catalog --ra-achievements`. To see your own progress, add `[ra]` with `user = "<your RA name>"` to the config.
6. **Start the server.**

   ```bash
   docker compose up -d
   docker compose ps          # "healthy" after about a minute
   docker compose logs -f
   ```

The server also updates the catalog by itself at start and every 6 hours, so games you add by hand show up without a command. `warpzone inbox --apply` updates it for the systems it files.

## Open the UI

Open `http://<the NAS's address>:8780/` in a browser on the home network, on a phone, a desktop or a handheld. The page asks for the token (the line in `config/ui.token`) once and keeps a cookie for 30 days. Change the token and every browser is asked again.

The traffic is plain HTTP and the token is one shared secret, so keep the port inside the home network. For access from outside, put your own reverse proxy with TLS in front of it. See [Authentication](/warpzone/development/design/library-ui/#authentication).

## Adding games from the container

`warpzone inbox` can run in this container as well as on a computer that mounts the share, with nothing installed on the NAS. The image ships `chdman` and `bsdtar` (on `PATH`, in `/usr/bin`), `dolphin-tool`, `nsz`, `orbis-pkg-util` and `Skyscraper` (on `PATH`, in `/usr/local/bin`) and RAHasher (at `/usr/local/bin/RAHasher`), and only Vita3K is missing. What a tool reads is never in the image: the DATs, `titledb`, the arcade DATs, your Switch keys (`switch_keys`, which `nsz` reads) and your PS Vita zRIF file (`vita_zrif`) are folders and files on the NAS that you mount read-only and name in `[inbox]`, and `inbox` needs a work folder with room, because the container's `/tmp` is memory. [Adding new games](/warpzone/setup/inbox/#where-inbox-runs) compares the two ways, lists the tools of each and gives the `compose.yaml` service, the `[inbox]` paths and the commands (`docker compose run --rm warpzone-inbox inbox --apply`). `docker compose run --rm warpzone-inbox doctor` checks them from inside.

To have the NAS file what you drop in `inbox/` by itself, run `docker compose run --rm -T warpzone-inbox inbox --apply --unattended` from a DSM scheduled task every 15 minutes, as root and with the full path of `docker`; [Adding games without being there](/warpzone/setup/inbox/#adding-games-without-being-there) has the task, what it holds back and when it exits non-zero. Give the service a CPU limit (`cpus` in the example) so SMB and Syncthing keep their share, with the same number in `[inbox] chdman_threads`, set `[inbox] chdman_flac = false` (chdman's FLAC codecs need more memory than the disc image; a 1.9 GB PS2 ISO was killed for lack of memory at the default 2 GiB limit, and with `createdvd` now always skipping FLAC and `chdman_flac = false` for CDs the same conversions take tens of MiB; `doctor` warns when it is left on), and put `[inbox] work_dir` on the `./work` volume. If Docker refuses the limit ("NanoCPUs can not be set", a Synology kernel without CFS quota), replace `cpus` with `cpu_shares: 512`, the tested fallback (a relative weight, no hard cap); [Running it in the container](/warpzone/setup/inbox/#running-it-in-the-container) has the details.

To have the hub fill in the art of games that did not come through `inbox`, add a second task, once a night, with the same service: `docker compose run --rm -T warpzone-inbox scrape --unattended`, daily at 03:00 as root. [Filling art every night](/warpzone/setup/scrape/#filling-art-every-night) has the task, what a night picks and how it shares the inbox lock.

## Scraping in the container

The image ships [Skyscraper](https://github.com/Gemba/skyscraper), so `warpzone inbox --apply` in the container scrapes the games it places (descriptions, covers, screenshots, videos) and `docker compose run --rm warpzone-inbox scrape` fills in the library, as [Scraping](/warpzone/setup/scrape/) describes. Skyscraper keeps its settings and everything it gathers in its home folder, `~/.skyscraper`, and the container's root is read-only and its `/tmp` is memory, so that folder is a volume you provide: the `warpzone-inbox` service of the compose example mounts `./skyscraper` at `/skyscraper`, and the image links Skyscraper's `~/.skyscraper` to it.

```bash
mkdir skyscraper && chown 1000:1000 skyscraper      # the uid and gid of `user:` in compose.yaml
```

Without an account nothing more is needed: Skyscraper works anonymously, with ScreenScraper's anonymous limits, and creates its folders in `skyscraper/` by itself the first time it runs. For a ScreenScraper account, and for the language and region of what you get, create `skyscraper/config.ini` with the same keys as [the setup guide](/warpzone/setup/scrape/#screenscraper-account-optional) (`userCreds` under `[screenscraper]`; `langPrios`, `regionPrios` and `regionFromFilename` under `[main]`), readable by the service's uid and by nobody else, since it holds the account's password (`chmod 600 skyscraper/config.ini`, owned by that uid). warpzone never reads, prints or passes it on a command line; `doctor` only checks that `userCreds` is there.

- **The cache is on disk, not in memory.** `skyscraper/cache/` grows by about 2.6 MB per game (a library of 1,000 games is about 2.6 GB); it is what lets a run that ScreenScraper's daily limit stopped go on the next day without asking again. Keep `./skyscraper` on the NAS's disk. The staged files and logs of a scrape go to `[inbox] work_dir` (`/work/tmp`), as before, and the list of games ScreenScraper did not know to `XDG_CACHE_HOME` (`/work/cache`).
- **Without the volume** Skyscraper stops when it starts: `inbox` places the games and warns that the scrape failed, and `docker compose run --rm warpzone-inbox doctor` shows `[--] Skyscraper` with "nothing is mounted at /skyscraper".
- `docker compose run --rm warpzone-inbox doctor` shows the version and whether the account is set. Updating Skyscraper is a change to `docker/Dockerfile`: it comes with a new warpzone version.

## Updating

Build the new version over the old tag and recreate the container:

```bash
cd warpzone                      # the checkout
git fetch --tags && git checkout vX.Y.Z
docker build -f docker/Dockerfile -t warpzone:local .
cd /path/to/the/compose/folder && docker compose up -d
```

`up -d` sees the new image and recreates the container. The catalog and the whitelists are on the share, so nothing is lost; only open pages need a reload. To go back, check out the previous tag, build again and `up -d`. An update of the pinned base image or of any of the tools in it is a change to `docker/Dockerfile` (or to `docker/nsz-requirements.txt`), so it comes with a new warpzone version. The first build after an update recompiles Dolphin and Skyscraper; Docker reuses the unchanged stages on the next ones.

## If it does not work

- `docker compose logs warpzone` prints why `serve` stopped. Exit `2` means no token (`token_file` unreadable by the uid, or empty), no `library/.stfolder` under `[hub] root`, or an address that cannot be listened on.
- A page that loads but shows no consoles or progress: the container cannot reach Syncthing at `api_url`. Try `docker compose run --rm warpzone status`. If it says the certificate "is not the one in hub.api_cert_sha256", nothing was sent: the message shows the fingerprint Syncthing presented. Compare it with the file; Syncthing renews its own GUI certificate about every 790 days, so after a renewal update `api_cert_sha256`.
- A change refused with a "not managed" message: that device's whitelist is still in the hand-written format; run `whitelist migrate` for it.
- A permission error writing `catalog/` or a whitelist: `user:` is not a uid that can write the share.
- `Permission denied` reading `library/patterns-<device>.stignore` (or `warpzone whitelist migrate` failing with `cannot read ...: Permission denied`) on a Synology, although `ls -l` shows `rwxrwxrwx`: the share grants access through ACL entries to the `administrators` group, not to your uid, and the container runs with only the primary group of `user:`. Uncomment `group_add:` in the compose file with the gid of that group (`101` on Synology; `getent group administrators` or `id` on the NAS shows it) and run `docker compose up -d` again.
