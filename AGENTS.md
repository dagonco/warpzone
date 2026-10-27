# AGENTS.md

Guidance for coding agents working on this repository. Humans: see [README.md](README.md) and [CONTRIBUTING.md](CONTRIBUTING.md).

## What this is

`warpzone` is a CLI that keeps one game library on a home server (the hub) and delivers it to several consoles through Syncthing: each console receives only what its whitelist on the hub allows, and emulator saves sync both ways. Read [docs/design.md](docs/design.md) before changing behaviour: it is the contract for the implementation, and any behaviour change updates it in the same pull request.

## Ground rules

- Python 3.11+ and the standard library only. No runtime dependencies, ever.
- Read-only by default: commands that change files report first and act only with `--apply` (see `prune`). Never overwrite or delete user data without a re-check right before acting.
- Keep decisions pure and I/O at the edges: a pure function decides (`prune.classify`, `bios.judge_*`), a thin layer touches the disk or the Syncthing API. Unit-test the pure part without a filesystem or network.
- Never print or log API keys or tokens. Redact them in dry-run output (`syncthing.redact`).
- The repository is public: no personal data in code, docs, tests or commits (hostnames, IP addresses, device IDs, user names, home paths, real API keys). Test fixtures use obvious fakes; mark them for gitleaks with a trailing `# gitleaks:allow` comment.
- warpzone never downloads BIOS files or games. Manifests hold names, sizes and hashes only.

## Layout

See "Code layout" in [docs/design.md](docs/design.md). In short: `cli.py` wires argparse and exit codes; `config.py` and `profiles.py` load and validate TOML; `model.py` expands them into desired folder specs (pure); `syncthing.py` is the REST client; `provision.py`, `prune.py` and `bios.py` implement the commands; `warpzone/data/` and `warpzone/profiles/` hold shipped TOML data.

## Checks

Run all of these before pushing; CI runs the same:

```bash
uvx ruff check .
uvx ruff format --check .
python3 -m unittest discover -s tests -v
```

The end-to-end test starts two real Syncthing instances and is skipped unless `WARPZONE_E2E_SYNCTHING` points at a Syncthing v2 binary (CI pins the version and its SHA-256 in `.github/workflows/ci.yml`):

```bash
WARPZONE_E2E_SYNCTHING=/path/to/syncthing python3 -m unittest discover -s tests -k EndToEnd -v
```

CI runs unit tests on Python 3.11 to 3.14 on Linux and macOS; the e2e test runs on Linux only. Behaviour that touches Syncthing can differ between the two (see below), so a local macOS e2e pass is not proof for Linux.

## Syncthing behaviour worth knowing

- Ignore patterns are applied only on a full scan of the folder. A whitelist that just synced does not take effect until the next scan, which is why device folders get a short `rescan_seconds`.
- After a full scan, a directory that just became ignored may still be recorded as not ignored (seen on Linux). Scan that directory on its own (`POST /rest/db/scan?folder=...&sub=...`, which answers when the scan is done) before trusting its `db/file` record.
- Syncthing never follows symlinks. EmuDeck exposes saves through symlinks, so save folders must point at the real directories.
- A folder whose `.stignore` includes a file that has not arrived yet is refused; `provision device` bootstraps with ignores that let only the whitelists through first.

## Pull requests and commits

- `main` is protected: changes land through pull requests, squash-merged, with all CI checks green. No force pushes.
- Commit messages are a single imperative title line in sentence case, no body and no trailers. Match `git log --oneline`.
- User-visible changes get an entry under `[Unreleased]` in [CHANGELOG.md](CHANGELOG.md) (Keep a Changelog). README usage sections are updated with new commands or options.
- Releases follow [docs/releasing.md](docs/releasing.md): a release pull request moves `[Unreleased]` into a version section and bumps `__version__`, then an annotated `vX.Y.Z` tag on `main` triggers the release workflow.
- Do not change `.github/workflows/` unless the task is about CI. Third-party actions stay pinned by commit SHA.
