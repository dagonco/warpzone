---
title: "The hub container and the library UI"
description: "Build the warpzone image, run it on the hub with Compose, and open the library UI."
---

Build the warpzone image, run it on the hub with Compose, and open the library UI in a browser.

A NAS often ships a Python older than 3.11, and warpzone needs 3.11. So `warpzone serve` (the library UI) and `warpzone catalog` run in a small container on the hub: a slim official Python image with warpzone installed and RAHasher (the RetroAchievements hasher) built in, running one process as the user that owns the games share. Nothing else of warpzone needs the container. The consoles, `provision hub` and `inbox` run as before, on other machines. The contract is in [Library UI: the container](/warpzone/development/design/library-ui/#the-container).

You need Docker with Compose v2 on the hub (Synology Container Manager has both), the hub's Syncthing GUI on a LAN address, and the [private config](/warpzone/reference/configuration/) of your setup.

## Build the image

The image installs warpzone from the checkout you build in, so the checkout is the version you run. Check out a release tag (`vX.Y.Z` below; use the newest release that has `warpzone serve`):

```bash
git clone https://github.com/dagonco/warpzone.git && cd warpzone
git checkout vX.Y.Z
docker build -f docker/Dockerfile -t warpzone:local .
```

A NAS may have no git. Download the release instead (`curl -L https://github.com/dagonco/warpzone/archive/refs/tags/vX.Y.Z.tar.gz | tar xz`), then `cd` into the folder it makes and run the `docker build` line.

The build compiles RAHasher from [RALibretro](https://github.com/RetroAchievements/RALibretro) in a first stage, so it needs the network, a few minutes and, on a small NAS, a while longer. The compiler stays in that stage: the image that runs holds Python, warpzone and the RAHasher binary, and nothing to build with. The base image and RALibretro are pinned by digest and by commit in [`docker/Dockerfile`](https://github.com/dagonco/warpzone/blob/main/docker/Dockerfile).

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
root = "/volume1/games"                  # the hub root, as on the NAS (and as mounted below)
api_url = "http://192.168.1.10:8384"     # a LAN address of the NAS, not 127.0.0.1
api_key_file = "/config/hub.key"

[inbox]
rahasher = "/usr/local/bin/RAHasher"     # optional: lets the server keep RA hashes up to date

[serve]
token_file = "/config/ui.token"
```

Make the token a long random string (`head -c 24 /dev/urandom | base64 | tr -d '/+='` makes one) and keep it to one line. The same applies to `RA_API_KEY` in `warpzone.env`: your RetroAchievements web API key, as `RA_API_KEY=<key>`. Without it the RA game lists are not downloaded and the page shows no achievements.

In `compose.yaml`, change:

- `user:` to the uid and gid that own the games share (`id -u` and `id -g` of that user). The server writes the device whitelists in `library/` and the `catalog/` folder as this user, so it needs write access to them. The files in `config/` must be readable by it too.
- the two paths on the left of the `volumes:` lines, if your share or your config folder are elsewhere. The hub root keeps the same path on both sides: it is the path `[hub] root` names, so the same config is right inside and outside the container.
- `mem_limit`, if you want another limit (512 MB is enough for the server).

**Network.** On the default bridge network the container reaches Syncthing at `[hub] api_url`, so Syncthing's GUI must listen on a LAN address of the NAS (and the NAS firewall must let the container's network in). If Syncthing's GUI listens only on `127.0.0.1`, switch the service to the host network instead, as the comments in the compose file say: remove `ports:`, uncomment `network_mode: host` and use `http://127.0.0.1:8384` as `api_url`.

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

## Updating

Build the new version over the old tag and recreate the container:

```bash
cd warpzone                      # the checkout
git fetch --tags && git checkout vX.Y.Z
docker build -f docker/Dockerfile -t warpzone:local .
cd /path/to/the/compose/folder && docker compose up -d
```

`up -d` sees the new image and recreates the container. The catalog and the whitelists are on the share, so nothing is lost; only open pages need a reload. To go back, check out the previous tag, build again and `up -d`. An update of the pinned base image or of RAHasher is a change to `docker/Dockerfile`, so it comes with a new warpzone version.

## If it does not work

- `docker compose logs warpzone` prints why `serve` stopped. Exit `2` means no token (`token_file` unreadable by the uid, or empty), no `library/.stfolder` under `[hub] root`, or an address that cannot be listened on.
- A page that loads but shows no consoles or progress: the container cannot reach Syncthing at `api_url`. Try `docker compose run --rm warpzone status`.
- A change refused with a "not managed" message: that device's whitelist is still in the hand-written format; run `whitelist migrate` for it.
- A permission error writing `catalog/` or a whitelist: `user:` is not a uid that can write the share.
- `Permission denied` reading `library/patterns-<device>.stignore` (or `warpzone whitelist migrate` failing with `cannot read ...: Permission denied`) on a Synology, although `ls -l` shows `rwxrwxrwx`: the share grants access through ACL entries to the `administrators` group, not to your uid, and the container runs with only the primary group of `user:`. Uncomment `group_add:` in the compose file with the gid of that group (`101` on Synology; `getent group administrators` or `id` on the NAS shows it) and run `docker compose up -d` again.
