# AGENTS.md

Guidance for coding agents working on this repository. Humans: see [README.md](README.md) and [CONTRIBUTING.md](CONTRIBUTING.md).

## What this is

`warpzone` is a CLI that keeps one game library on a home server (the hub) and delivers it to several consoles through Syncthing: each console receives only what its whitelist on the hub allows, and emulator saves sync both ways. Read [the design index](website/src/content/docs/development/design/index.md) before changing behaviour: the design pages in `website/src/content/docs/development/design/` are the contract for the implementation, and any behaviour change updates the page that describes it in the same pull request.

## Ground rules

- Python 3.11+ and the standard library only. No runtime dependencies, ever.
- Writing commands follow one of two rules (see "Writing commands" in `development/design/configuration.md`). Destructive ones, which delete or move user data (`prune`, `inbox`), report first and act only with `--apply`, and have no `--dry-run`. Convergent ones, which keep a backup of any file they edit (`provision`, `settings`, `play-state`, `links`, `frontend refresh`), write by default and accept `--dry-run`. Read-only commands (`check`, `bios`, `doctor`, `health`, `status`, `render-ignores`, `settings audit`) write no user data. Never overwrite or delete user data without a re-check right before acting.
- Keep decisions pure and I/O at the edges: a pure function decides (`prune.classify`, `bios.judge_*`), a thin layer touches the disk or the Syncthing API. Unit-test the pure part without a filesystem or network.
- Never print or log API keys or tokens. Redact them in dry-run output (`syncthing.redact`).
- The repository is public: no personal data in code, docs, tests or commits (hostnames, IP addresses, device IDs, user names, home paths, real API keys). Test fixtures use obvious fakes; mark them for gitleaks with a trailing `# gitleaks:allow` comment.
- warpzone never downloads BIOS files or games. Manifests hold names, sizes and hashes only.

## Layout

See "Code layout" in [website/src/content/docs/development/design/code-and-testing.md](website/src/content/docs/development/design/code-and-testing.md); a test fails when a module is missing from it. In short: `cli.py` wires argparse and exit codes; `config.py` and `profiles.py` load and validate TOML; `model.py` expands them into desired folder specs (pure); `syncthing.py` is the REST client; `provision_hub.py`, `provision_device.py` (with the other `provision_*` modules; `provision.py` is only the name `cli` imports), `prune.py` and `bios.py` implement the commands; `probes.py` groups what a device command reads from the machine, so tests fake one object; `warpzone/data/` (with `data/profiles/`) holds shipped TOML data.

## Documentation website

All documentation lives in `website/src/content/docs/` (Starlight, Astro; English only) and is published at https://dagonco.github.io/warpzone by `.github/workflows/docs.yml`. Code comments name pages by their path under that folder (for example `development/design/inbox.md`). Link between pages with absolute paths that include the base (`/warpzone/setup/inbox/#ps4-games`); the build fails on a broken link or anchor. Link to files outside the site (`examples/`, `templates/`, source) with `https://github.com/dagonco/warpzone/blob/main/...`. Node dependencies live only in `website/`, pinned to exact versions; the Python package stays dependency-free.

```bash
cd website && npm ci && npm run build
```

Agent skills for common tasks (install a console, add games, edit a whitelist, diagnose, release) live in `.claude/skills/`; keep them in step with the docs they link to.

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

The tests of the updater script (`tests/test_updater_sh.py`) run `warpzone/data/updater.sh` against real git repositories, take about two minutes, and use shellcheck when it is installed (`brew install shellcheck` or `apt install shellcheck`). CI runs them in their own `updater` job (Ubuntu with dash as `sh`, and macOS), and the `test` job skips them through `WARPZONE_SKIP_UPDATER_SH=1`. Locally the plain command above runs everything, that file included; to run only that suite, or everything else, or that suite with another shell as `sh`:

```bash
python3 -m unittest discover -s tests -p test_updater_sh.py -v
WARPZONE_SKIP_UPDATER_SH=1 python3 -m unittest discover -s tests -v
WZ_TEST_SHELL=dash python3 -m unittest discover -s tests -p test_updater_sh.py -v
```

CI runs unit tests on Linux with Python 3.11 and 3.14 and on macOS with Python 3.14 (the updater script's tests apart); the e2e test runs on Linux only. Behaviour that touches Syncthing can differ between the two (see below), so a local macOS e2e pass is not proof for Linux.

## Syncthing behaviour worth knowing

- Ignore patterns are applied only on a full scan of the folder. A whitelist that just synced does not take effect until the next scan, which is why device folders get a short `rescan_seconds`.
- After a full scan, a directory that just became ignored may still be recorded as not ignored (seen on Linux). Scan that directory on its own (`POST /rest/db/scan?folder=...&sub=...`, which answers when the scan is done) before trusting its `db/file` record.
- Syncthing never follows symlinks. EmuDeck exposes saves through symlinks, so save folders must point at the real directories.
- A folder whose `.stignore` includes a file that has not arrived yet is refused; `provision device` bootstraps with ignores that let only the whitelists through first.

## Pull requests and commits

- `main` is protected: changes land through pull requests, squash-merged, with all CI checks green. No force pushes.
- Commit messages are a single imperative title line in sentence case, no body and no trailers. Match `git log --oneline`.
- User-visible changes get an entry under `[Unreleased]` in [CHANGELOG.md](CHANGELOG.md) (Keep a Changelog). The guides and the reference pages in `website/src/content/docs/` are updated with new commands or options.
- Releases follow [website/src/content/docs/development/releasing.md](website/src/content/docs/development/releasing.md): a release pull request moves `[Unreleased]` into a version section and bumps `__version__`, then an annotated `vX.Y.Z` tag on `main` triggers the release workflow.
- Do not change `.github/workflows/` unless the task is about CI. Third-party actions stay pinned by commit SHA.
