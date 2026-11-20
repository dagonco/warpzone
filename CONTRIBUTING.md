# Contributing

To report a bug or ask for a feature, open an issue with one of the templates; they ask for the output that helps, and remind you to remove personal data first. Small, focused pull requests are welcome. New device profiles are the easiest contribution: add a TOML file under `warpzone/data/profiles/` (see `steamos-emudeck.toml`) and a test that loads it.

`warpzone` is standard library only and targets Python 3.11+. Please keep it that way, and never print or log API keys. The ground rules, pull request and commit conventions are in [AGENTS.md](AGENTS.md); they apply to people too.

## Checks

Run all of these before pushing; CI runs the same:

```bash
uvx ruff check .
uvx ruff format --check .
uvx mypy==2.4.1
python3 -m unittest discover -s tests -v
```

The end-to-end test starts two real Syncthing instances on 127.0.0.1 in temporary directories and is skipped unless you point it at a Syncthing v2 binary. It takes about two minutes and needs git:

```bash
WARPZONE_E2E_SYNCTHING=/path/to/syncthing python3 -m unittest discover -s tests -k EndToEnd -v
```

Set `WARPZONE_E2E_VERBOSE=1` to print the Syncthing logs and every command it ran; on failure they are printed anyway. What each test covers and how CI runs them is in [Code, testing and CI](https://dagonco.github.io/warpzone/development/design/code-and-testing/).

## Documentation

The documentation is a [Starlight](https://starlight.astro.build) site in `website/`, published at <https://dagonco.github.io/warpzone>. The pages in `website/src/content/docs/development/design/` (start at `index.md`) are the contract for the implementation: if you change behaviour, change the page that describes it too.

To build the site (Node 22.23.4 or newer; the build also fails on a broken internal link):

```bash
cd website
npm ci
npm run build
```

## Releasing

Maintainers: see [Releasing](https://dagonco.github.io/warpzone/development/releasing/). User-visible changes go under `[Unreleased]` in [CHANGELOG.md](CHANGELOG.md).
