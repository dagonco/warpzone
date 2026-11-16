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

The build compiles four programs and downloads a fifth in stages of their own, so it needs the network and time: RAHasher from [RALibretro](https://github.com/RetroAchievements/RALibretro), Skyscraper from the [Gemba fork](https://github.com/Gemba/skyscraper) (against the Qt 6 libraries of Debian, with its configuration files built into the binary; a few minutes, and it builds at the same time as Dolphin), `dolphin-tool` from [Dolphin](https://github.com/dolphin-emu/dolphin) (only that program, with Dolphin's bundled libraries linked into it, but it still compiles Dolphin's emulation core, so it is by far the longest part: it takes about 13 minutes on a four-core x86 machine, where the whole build takes about 15, and several times that on a small NAS) and `orbis-pkg-util` from its [crate](https://crates.io/crates/orbis-pkg-util) with Cargo. Vita3K is not built: the stage downloads the x86_64 AppImage of a [numbered build](https://github.com/Vita3K/Vita3K-builds/releases) and unpacks it (a minute). `nsz` is installed from PyPI into a virtual environment of its own, so its packages are not warpzone's. The compilers stay in their stages: the image that runs holds Python, warpzone, the built or installed tools (about 75 MB on disk: 21 MB for `dolphin-tool`, 51 MB for `nsz` and its packages, 2 MB each for RAHasher and `orbis-pkg-util`, the 2 MB Skyscraper binary, and the Vita3K bundle, a self-contained folder with its own Qt 6 and Mesa that adds about 121 MB to the compressed image), and the Debian packages `mame-tools` (`chdman`), `libarchive-tools` (`bsdtar`) and the Qt 6 libraries Skyscraper links (`libqt6gui6`, `libqt6network6`, `libqt6xml6`, `libqt6sql6` and `libqt6sql6-sqlite`), which add about 355 MB on disk to it (about 250 MB of it is the graphics and sound libraries `mame-tools` depends on: SDL2, Mesa and LLVM; Debian packages no `chdman` without them; the Qt libraries add about 105 MB more), and nothing to build with. Everything is pinned in [`docker/Dockerfile`](https://github.com/dagonco/warpzone/blob/main/docker/Dockerfile) and checked when it is fetched: the base images by digest, RALibretro, Skyscraper and Dolphin by commit (the build stops if the tag points elsewhere), Vita3K by the SHA-256 of its AppImage (build 4334), `orbis-pkg-util` by the SHA-256 of its crate and the checksums in the crate's own `Cargo.lock`, and `nsz` with its dependencies by version and SHA-256 in [`docker/nsz-requirements.txt`](https://github.com/dagonco/warpzone/blob/main/docker/nsz-requirements.txt).

The tools are free software under their own licences, which differ from warpzone's MIT: RAHasher is GPL-3.0, Skyscraper is GPL-3.0, `dolphin-tool` is GPL-2.0-or-later, Vita3K is GPL-2.0 (its bundle also carries Qt 6, Mesa and other libraries under their own licences), `nsz` is MIT and `orbis-pkg-util` is MIT or Apache-2.0 (`chdman` is GPL-2.0-or-later and `bsdtar` BSD, as Debian packages them). None of their source or binaries is in the warpzone repository; the image is built on your machine from the upstream releases, and you do not redistribute it. Keep it that way: if you share the image, you share those programs and their licences with it.

**Vita3K is in the image on x86_64 only.** It has no build for other CPUs that is used here, so an image built on an arm64 machine (a Mac with Apple silicon building natively, an ARM NAS) builds without it, `warpzone doctor` shows `[--] Vita3K` there, and a PS Vita `.pkg` goes to review with "missing tool" until you run `inbox` on a computer with Vita3K. On x86_64 it needs nothing installed: only your zRIF file, mounted read-only ([how it runs](/warpzone/development/design/inbox/#ps-vita-packages-in-the-container)). Moving to another Vita3K build is a change to `docker/Dockerfile`, so it comes with a new warpzone version.

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

The traffic is plain HTTP and the token is one shared secret, so keep the port inside the home network and never forward it on the router. To reach the UI from outside the home, use a tunnel with a login in front of it; [Opening the UI from outside the home](#opening-the-ui-from-outside-the-home) shows one. See [Authentication](/warpzone/development/design/library-ui/#authentication).

## Deleting a game from the UI

A game's page ends with **Delete from the hub**. It first shows everything that would go: each file and folder with its size, the art and other media of every kind and the gamelist entry, the whitelist lines of each console, and the saves, which stay. **Move to trash** then moves it all to `trash/` at the hub root, outside every Syncthing folder. Every console that has the game deletes it as part of the normal sync, and its whitelist lines are removed by the server once that console has received the deletion, so a console that is off keeps its lines until it comes back and syncs. The **Trash** view lists what was deleted, with the days left before it is purged (7 days), **Restore** and **Delete now**. A restored game is back in the library; the consoles whose lines were already removed get it as a newly added game, off until you turn it on. The hub's free space grows only when an entry is purged.

A deletion is refused while an inbox or a scrape runs (try again when it ends), and for an arcade set that other sets of the library need as their parent or BIOS (the dialog names them). To check that, `serve` reads the arcade DATs `[inbox]` names (`mame_dat`, `fbneo_dat`, `mame2003plus_dat`). If they live under the hub root, which the `warpzone` service already mounts at the same path, nothing else is needed. Only DATs kept outside the hub root must be mounted read-only into the `warpzone` service at the same paths as in the `warpzone-inbox` service. With none readable, deleting arcade games is refused. The design is in [Deleting a game](/warpzone/development/design/library-ui/#deleting-a-game).

## Adding games from the container

`warpzone inbox` can run in this container as well as on a computer that mounts the share, with nothing installed on the NAS. The image ships `chdman` and `bsdtar` (on `PATH`, in `/usr/bin`), `dolphin-tool`, `nsz`, `orbis-pkg-util`, `Skyscraper` and, on x86_64, `Vita3K` (on `PATH`, in `/usr/local/bin`) and RAHasher (at `/usr/local/bin/RAHasher`). What a tool reads is never in the image: the DATs, `titledb`, the arcade DATs, your Switch keys (`switch_keys`, which `nsz` reads) and your PS Vita zRIF file (`vita_zrif`) are folders and files on the NAS that you mount read-only and name in `[inbox]`, and `inbox` needs a work folder with room, because the container's `/tmp` is memory. [Adding new games](/warpzone/setup/inbox/#where-inbox-runs) compares the two ways, lists the tools of each and gives the `compose.yaml` service, the `[inbox]` paths and the commands (`docker compose run --rm warpzone-inbox inbox --apply`). `docker compose run --rm warpzone-inbox doctor` checks them from inside.

To have the NAS file what you drop in `inbox/` by itself, run `docker compose run --rm -T warpzone-inbox inbox --apply --unattended` from a DSM scheduled task every 15 minutes, as root and with the full path of `docker`; [Adding games without being there](/warpzone/setup/inbox/#adding-games-without-being-there) has the task, what it holds back and when it exits non-zero. Give the service a CPU limit (`cpus` in the example) so SMB and Syncthing keep their share, with the same number in `[inbox] chdman_threads`, set `[inbox] chdman_flac = false` (chdman's FLAC codecs need more memory than the disc image; a 1.9 GB PS2 ISO was killed for lack of memory at the default 2 GiB limit, and with `createdvd` now always skipping FLAC and `chdman_flac = false` for CDs the same conversions take tens of MiB; `doctor` warns when it is left on), and put `[inbox] work_dir` on the `./work` volume. If Docker refuses the limit ("NanoCPUs can not be set", a Synology kernel without CFS quota), replace `cpus` with `cpu_shares: 512`, the tested fallback (a relative weight, no hard cap); [Running it in the container](/warpzone/setup/inbox/#running-it-in-the-container) has the details.

To have the hub fill in the art of games that did not come through `inbox`, add a second task, once a night, with the same service: `docker compose run --rm -T warpzone-inbox scrape --unattended`, daily at 03:00 as root. [Filling art every night](/warpzone/setup/scrape/#filling-art-every-night) has the task, what a night picks and how it shares the inbox lock.

## Scraping in the container

The image ships [Skyscraper](https://github.com/Gemba/skyscraper), so `warpzone inbox --apply` in the container scrapes the games it places (descriptions, covers, screenshots, videos) and `docker compose run --rm warpzone-inbox scrape` fills in the library, as [Scraping](/warpzone/setup/scrape/) describes. Skyscraper keeps its settings and everything it gathers in its home folder, `~/.skyscraper`, and the container's root is read-only and its `/tmp` is memory, so that folder is a volume you provide: the `warpzone-inbox` service of the compose example mounts `./skyscraper` at `/skyscraper`, and the image links Skyscraper's `~/.skyscraper` to it.

```bash
mkdir skyscraper && chown 1000:1000 skyscraper      # the uid and gid of `user:` in compose.yaml
```

Without an account nothing more is needed: Skyscraper works anonymously, with ScreenScraper's anonymous limits, and creates its folders in `skyscraper/` by itself the first time it runs. For a ScreenScraper account, and for a region and language order of your own beyond the first choice that `scrape_region` and `scrape_language` give ([Region and language](/warpzone/setup/scrape/#region-and-language)), create `skyscraper/config.ini` with the same keys as [the setup guide](/warpzone/setup/scrape/#screenscraper-account-optional) (`userCreds` under `[screenscraper]`; `langPrios`, `regionPrios` and `regionFromFilename` under `[main]`), readable by the service's uid and by nobody else, since it holds the account's password (`chmod 600 skyscraper/config.ini`, owned by that uid). warpzone never reads, prints or passes it on a command line; `doctor` only checks that `userCreds` is there.

- **The cache is on disk, not in memory.** `skyscraper/cache/` grows by about 2.6 MB per game (a library of 1,000 games is about 2.6 GB); it is what lets a run that ScreenScraper's daily limit stopped go on the next day without asking again. Keep `./skyscraper` on the NAS's disk. The staged files and logs of a scrape go to `[inbox] work_dir` (`/work/tmp`), as before, and the list of games ScreenScraper did not know to `XDG_CACHE_HOME` (`/work/cache`).
- **Without the volume** Skyscraper stops when it starts: `inbox` places the games and warns that the scrape failed, and `docker compose run --rm warpzone-inbox doctor` shows `[--] Skyscraper` with "nothing is mounted at /skyscraper".
- `docker compose run --rm warpzone-inbox doctor` shows the version and whether the account is set. Updating Skyscraper is a change to `docker/Dockerfile`: it comes with a new warpzone version.

## Opening the UI from outside the home

`warpzone serve` speaks plain HTTP and does no TLS, so it never faces the internet by itself. This is one worked example of a way to reach it from a phone or a laptop away from home with nothing opened on the router: a [Cloudflare Tunnel](https://developers.cloudflare.com/cloudflare-one/networks/connectors/cloudflare-tunnel/) with [Cloudflare Access](https://developers.cloudflare.com/cloudflare-one/access-controls/) in front. You can use any other reverse proxy with TLS instead; the UI only needs what [Authentication](/warpzone/development/design/library-ui/#authentication) lists. It is optional, and the UI works the same without it.

It gives you:

- HTTPS from anywhere, with Cloudflare's certificate.
- A login (Access) before any request reaches the hub: a visitor who is not on your list never gets to the UI's page, let alone its token prompt.
- No open port and no port forwarding. A `cloudflared` connector on your network dials out to Cloudflare, and Cloudflare sends the requests down that connection.

The UI's own token stays as a second layer, so a visit asks for two things: the Cloudflare login, then the token. Step 7 makes the UI accept the Cloudflare login as the only prompt; the token then stays for anything that does not come through Cloudflare.

You need a domain whose DNS is on Cloudflare (a free account is enough) and a Zero Trust account (the free plan covers a few users). The example uses `ui.example.com` for the address, `<team-name>` for your Zero Trust team name, `<hub-lan-address>` for the hub's address on the home network and `you@example.com` for your e-mail. Use your own, and keep them out of anything you commit.

**Order matters.** Make sure an Access application covers the hostname (steps 1 and 2) before the tunnel gets a public route for it (step 4). A published hostname with no Access application in front of it is open to the whole internet until you add one, so do it in this order and the UI is never public, not even for a minute.

1. **Pick a way to log in.** If Access already works for your other services, you have one: skip to step 2. Otherwise, in the Cloudflare dashboard open Zero Trust, then Integrations, then Identity providers. The zero-setup choice is the built-in **One-time PIN**: Cloudflare e-mails a code to the address that tries to log in, and only the addresses your policy allows get one. To use Google instead, follow [Cloudflare's Google guide](https://developers.cloudflare.com/cloudflare-one/integrations/identity-providers/google/): make an OAuth client of type Web application in the Google Cloud console with the authorized redirect URI `https://<team-name>.cloudflareaccess.com/cdn-cgi/access/callback`, then add Google under Identity providers with the client ID and secret. Your team name is under Zero Trust, Settings, Team name and domain.
2. **Have an Access application cover the hostname.** Under Zero Trust, Access controls, Applications, check first whether one already does. A wildcard application on the zone (a hostname such as `*.example.com`) covers a new subdomain such as `ui.example.com` by itself, with whatever policy it has: read that policy and make sure it lets in only the people you want to see the library. If none covers it, choose **Create new application**, then **Self-hosted and private**, add the public hostname `ui.example.com` and an Access policy with the action **Allow** and the rule **Include**, selector **Emails**, value `you@example.com` (add the e-mail of anyone else who may use the UI; nobody else is let in, because Access denies by default). Select your identity provider and choose a session duration. See [Cloudflare's guide](https://developers.cloudflare.com/cloudflare-one/access-controls/applications/http-apps/self-hosted-public-app/). Until the route of step 4 exists, the hostname answers nothing.
3. **Choose the tunnel.**

   - **You already run a tunnel that reaches the hub or the home network** (for other services). Add no service. The connector of that tunnel only has to reach the hub's port 8780 on the LAN, so the route of step 4 points at `http://<hub-lan-address>:8780`. Go to step 4.
   - **You have no tunnel yet.** Under Networking, Tunnels, choose **Create a tunnel**, type **Cloudflared**, give it a name such as `warpzone`, and copy only the token from the install command it shows (the long string after `--token`, or the value of `TUNNEL_TOKEN`). Do not run the command it shows; the container does that. Put the token in a file next to `compose.yaml`, mode 600, and never commit, paste or print it:

     ```bash
     cd /path/to/the/compose/folder
     ( umask 077; read -r -s -p "Tunnel token: " token && printf 'TUNNEL_TOKEN=%s\n' "$token" > tunnel.env )
     ```

     This keeps the token out of your shell history and out of the process list. Anyone who holds it can run a tunnel to your hostname, so treat it like the API key. If it leaks, delete the tunnel in the dashboard and make another. Then uncomment the `cloudflared` service at the end of [`compose.yaml`](https://github.com/dagonco/warpzone/blob/main/examples/compose.example.yaml) and start it:

     ```bash
     docker compose up -d cloudflared
     docker compose logs cloudflared      # "Registered tunnel connection" a few times
     ```

     It is in the same project as the `warpzone` service, so it reaches the UI on Compose's own network as `http://warpzone:8780`. (If you run the `warpzone` service with `network_mode: host`, that name does not resolve: use `http://localhost:8780` and run `cloudflared` on the host network too.)
4. **Add the public hostname route.** Open the tunnel, then the **Routes** tab, **Add route**, **Published application**: subdomain `ui`, your domain, and the service URL: `http://<hub-lan-address>:8780` for a tunnel you already had, `http://warpzone:8780` for the one above. Cloudflare makes the DNS record for `ui.example.com` by itself.
5. **Check that Access is in front.** From a computer that is on your home network or a VPN, a name can resolve to a private address and look open without the internet ever seeing it, so force the public edge. Look up an address of Cloudflare's edge for the hostname with a public resolver (`dig +short ui.example.com @1.1.1.1`), and ask for the page through it:

   ```bash
   curl -sI --resolve ui.example.com:443:<cloudflare-edge-ip> https://ui.example.com/
   ```

   The answer must be a redirect (`302`) whose `location` is on `<team-name>.cloudflareaccess.com`. A `200` or a page from the UI means no application covers the hostname: remove the route and go back to step 2.
6. **Test it from outside.** On a phone off the home Wi-Fi (mobile data), open `https://ui.example.com`. You should get Cloudflare's login first, then, after you log in with the allowed e-mail, the UI's token prompt. Enter the token from `config/ui.token`. Try an address that is not on the list: it must be refused at the login, before the UI.
7. **Optional: skip the token prompt.** Having logged in with Cloudflare, typing the UI token as well is redundant. Give `warpzone serve` your team's host and the application's AUD tag and it accepts the token Access attaches to each request it lets through, so the page opens straight after the Cloudflare login. Find the AUD tag under Zero Trust, Access controls, Applications: choose **Edit** on the application that covers the hostname and copy the **Application Audience (AUD) Tag** under Additional settings. A wildcard application (`*.example.com`) has one AUD tag for every hostname it covers, so the same value works for each of them. Your team's host is `<team-name>.cloudflareaccess.com`. In `config.toml`:

   ```toml
   [serve]
   token_file = "/config/ui.token"
   access_team_domain = "<team-name>.cloudflareaccess.com"
   access_aud = "<the application's AUD tag>"
   # access_emails = ["you@example.com"]     # optional: also require one of these e-mails
   ```

   Restart the container (`docker compose restart warpzone`); the log says `Cloudflare Access sign-in is on`. Reload `https://ui.example.com`: after the Cloudflare login the UI opens with no token prompt. `access_emails` is a second filter on the e-mail inside the token; leave it out and your Access policy alone decides who gets in, which is the usual choice.

   The token is still required to start, and still opens the UI for everything that does not pass Access: the LAN address, a console, a script. Nothing about the Cloudflare login can be forged: the UI checks Cloudflare's signature on the token, the application's AUD tag and the expiry, using the signing keys it downloads from `https://<team-name>.cloudflareaccess.com/cdn-cgi/access/certs` (it needs to reach that address; if it cannot, it simply asks for the token as before and the log says so once). Only the `Cf-Access-Jwt-Assertion` header counts, never a cookie.

   To make Access the only way in from outside, publish the UI's port to the host's loopback only, so nothing on the home network can reach it either. In `compose.yaml` change `ports:` to `- "127.0.0.1:8780:8780"`. The `cloudflared` service of the example still reaches `http://warpzone:8780` over Compose's own network; a `cloudflared` that runs on the host's own network (outside this project) points its route at `http://localhost:8780` instead. If the `warpzone` service uses `network_mode: host` there is no `ports:`; set `listen = "127.0.0.1:8780"` in `[serve]` for the same effect. The price is that the LAN address `http://<the NAS's address>:8780` stops answering, which cuts off every client that is not a browser, such as the Decky plugin on a console, unless it reaches the public hostname with an Access service token next to its bearer token. Browsers at home go through the public hostname too. If you want the LAN address as well, leave `ports:` as it is: the token still guards it.

A few things to know:

- **Host and scheme.** `cloudflared` sends the request to the UI with the `Host` header the browser used (`ui.example.com`), unless you set an HTTP Host Header override on the route; leave that empty. The UI's check that a write comes from its own page compares the `Origin` with that `Host`, so an override makes every change you make fail with a `bad-origin` error. The UI also marks its cookie `Secure` when the request carries `X-Forwarded-Proto: https`, which Cloudflare's edge sets on the requests it forwards; if you do not see `Secure` on the `wz_session` cookie in the browser's developer tools, nothing breaks: the cookie is just not restricted to HTTPS (it is still sent only to your HTTPS address). See [Authentication](/warpzone/development/design/library-ui/#authentication).
- **The login can expire.** When the Access session ends, a request the page makes may get Cloudflare's login page instead of JSON, and the UI shows an error. Reload the page and log in again. With step 7 the UI never asks for the token behind Access, and a request that reaches it without a valid Access token is asked for the token as always.
- **Consoles keep the LAN address.** A client that is not a browser, such as the Decky plugin with its bearer token, cannot pass Access's login, so consoles keep using the hub's LAN address, not the public hostname. That is why the LAN port stays published unless you accept cutting them off (step 7).
- **The LAN port stays.** Unless you published it to the loopback only (step 7), `8780` still answers on the home network as before. Only the tunnel reaches the UI from outside.
- **Updating `cloudflared`.** The example pins an exact version, not `latest`, so an update is a change you make on purpose. Look up the newest release on the [`cloudflared` releases page](https://github.com/cloudflare/cloudflared/releases), change the tag in `compose.yaml`, and run `docker compose pull cloudflared && docker compose up -d cloudflared`. Cloudflare drops very old versions, so update it every few months. A tunnel you already had is updated the way you already update it.
- **Taking it down.** Delete the route in the dashboard (and, for the example's tunnel, run `docker compose stop cloudflared`); the hostname then answers nothing.

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
