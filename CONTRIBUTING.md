# Contributing

Small, focused pull requests are welcome. New device profiles are the easiest contribution: add a TOML file under `warpzone/profiles/` (see `steamos-emudeck.toml`) and a test that loads it.

`warpzone` is standard library only and targets Python 3.11+. Please keep it that way, and never print or log API keys.

## Lint

```bash
uvx ruff check .
uvx ruff format --check .
```

CI pins the ruff version in `.github/workflows/ci.yml`.

## Tests

```bash
python3 -m unittest discover -s tests -v
```

## End-to-end test

The e2e test starts two real Syncthing instances on 127.0.0.1 in temporary directories. It is skipped unless you point it at a Syncthing v2 binary:

```bash
WARPZONE_E2E_SYNCTHING=/path/to/syncthing python3 -m unittest discover -s tests -k EndToEnd -v
```

It takes about a minute. Set `WARPZONE_E2E_VERBOSE=1` to print the Syncthing logs and every `warpzone` command it ran; on failure they are printed anyway.

## Documentation

`docs/design.md` is the contract for the implementation. If you change behaviour, change it there too.

## Releasing

Maintainers: see [docs/releasing.md](docs/releasing.md). User-visible changes go under `[Unreleased]` in [CHANGELOG.md](CHANGELOG.md).
