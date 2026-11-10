# warpzone

[![ci](https://github.com/dagonco/warpzone/actions/workflows/ci.yml/badge.svg)](https://github.com/dagonco/warpzone/actions/workflows/ci.yml)
[![release](https://img.shields.io/github/v/release/dagonco/warpzone)](https://github.com/dagonco/warpzone/releases/latest)

Keep one game library on a home server and deliver it to every console you own, with saves that follow you from one device to the next.

`warpzone` is a thin layer of conventions and automation on top of [Syncthing](https://syncthing.net). Syncthing does all the transfer between machines; warpzone configures it, files new games into the hub's library and removes from a device what its whitelist no longer allows.

## How it works

The server (the hub) owns the library. Devices only receive it, and each device chooses what it gets with a whitelist that is edited only on the hub, so a 2 TB library can feed a 512 GB console. Emulator saves, settings and frontend data sync both ways, with history kept on the hub. `warpzone` is a Python 3.11+ command line tool that uses the standard library only, so it runs unchanged on SteamOS, macOS and any Linux box.

## Documentation

Everything else is on the documentation website: <https://dagonco.github.io/warpzone>.

- [Installation](https://dagonco.github.io/warpzone/overview/installation/) and [Quick start](https://dagonco.github.io/warpzone/overview/quick-start/)
- [Commands](https://dagonco.github.io/warpzone/reference/commands/)
- [Safety rules](https://dagonco.github.io/warpzone/operation/safety/)
- [Design](https://dagonco.github.io/warpzone/development/design/), [Contributing](CONTRIBUTING.md), [Changelog](CHANGELOG.md)

The site's sources are in [`website/`](website).

## Legal

`warpzone` ships and locates no ROMs, BIOS files, keys or firmware, and never will. `warpzone bios` and `warpzone inbox` only compare what you already have with public file names, sizes and hashes. It only moves files you already have between machines you own. Use your own legally obtained dumps.

## License

[MIT](LICENSE)
