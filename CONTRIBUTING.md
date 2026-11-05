# Contributing

Small, focused pull requests are welcome. New device profiles are the easiest contribution: add a TOML file under `warpzone/data/profiles/` (see `steamos-emudeck.toml`) and a test that loads it.

`warpzone` is standard library only and targets Python 3.11+. Please keep it that way, and never print or log API keys.

## Lint

```bash
uvx ruff check .
uvx ruff format --check .
uvx mypy==2.4.1
```

CI pins the ruff and mypy versions in `.github/workflows/ci.yml`. mypy runs in its default (non-strict) mode over the `warpzone` package, as configured in `pyproject.toml`; the tests are not type-checked.

## Tests

```bash
python3 -m unittest discover -s tests -v
```

## End-to-end test

The e2e test starts two real Syncthing instances on 127.0.0.1 in temporary directories. It is skipped unless you point it at a Syncthing v2 binary:

```bash
WARPZONE_E2E_SYNCTHING=/path/to/syncthing python3 -m unittest discover -s tests -k EndToEnd -v
```

It takes about two minutes and needs git (the updater part runs `updater.sh` against a throwaway repository). Set `WARPZONE_E2E_VERBOSE=1` to print the Syncthing logs and every command it ran; on failure they are printed anyway.

## Documentation

The documentation is a [Starlight](https://starlight.astro.build) site in `website/`, published at <https://dagonco.github.io/warpzone>. The pages are in `website/src/content/docs/`. The pages in `website/src/content/docs/development/design/` (start at `index.md`) are the contract for the implementation: if you change behaviour, change the page that describes it too.

To build the site (Node 22.23.4 or newer; the build also fails on a broken internal link):

```bash
cd website
npm ci
npm run build
```

## Releasing

Maintainers: see [Releasing](https://dagonco.github.io/warpzone/development/releasing/) (`website/src/content/docs/development/releasing.md`). User-visible changes go under `[Unreleased]` in [CHANGELOG.md](CHANGELOG.md).
