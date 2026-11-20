# AGENTS.md

Guidance for coding agents working on this repository. Humans: see [README.md](README.md) and [CONTRIBUTING.md](CONTRIBUTING.md).

## What this is

`warpzone` is a CLI that keeps one game library on a home server (the hub) and delivers it to several consoles through Syncthing: each console receives only what its whitelist on the hub allows, and emulator saves sync both ways. Read [the design index](website/src/content/docs/development/design/index.md) before changing behaviour: the design pages in `website/src/content/docs/development/design/` are the contract for the implementation, and any behaviour change updates the page that describes it in the same pull request.

## Ground rules

- Python 3.11+ and the standard library only. No runtime dependencies, ever.
- A command that writes follows one of two rules ("Writing commands" in `development/design/configuration.md`, which also lists which command is which). A destructive one, which deletes or moves user data (`prune`, `inbox`), reports first, acts only with `--apply` and has no `--dry-run`. Every other writing command is convergent: it writes by default, keeps a backup of any file it edits and accepts `--dry-run`. Read-only commands write no user data. Never overwrite or delete user data without a re-check right before acting.
- Keep decisions pure and I/O at the edges: a pure function decides (`prune.classify`, `bios.judge_*`), a thin layer touches the disk or the Syncthing API. Unit-test the pure part without a filesystem or network.
- Never print or log API keys or tokens. Redact them in dry-run output (`syncthing.redact`).
- The repository is public: no personal data in code, docs, tests or commits (hostnames, IP addresses, device IDs, user names, home paths, real API keys). Test fixtures use obvious fakes; mark them for gitleaks with a trailing `# gitleaks:allow` comment.
- warpzone never downloads BIOS files or games. Manifests hold names, sizes and hashes only.
- The design pages are the source of truth for design. A docstring states the contract in one or two lines and, for a design rule, names the page and section instead of restating it; a longer comment is kept only for a workaround or constraint tied to that exact line. Comments describe what the code does now, never a change or its history, and refer to a design section by name, not by number.

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
uvx mypy==2.4.1
python3 -m unittest discover -s tests -v
```

mypy checks only the `warpzone` package, in strict mode (`pyproject.toml`). Fix a type error in the code; use `# type: ignore[code]` only where the standard library's typing is wrong, with a short reason.

The end-to-end test starts two real Syncthing instances and is skipped unless `WARPZONE_E2E_SYNCTHING` points at a Syncthing v2 binary. The plain test command above also runs the updater script's tests (`tests/test_updater_sh.py`, about two minutes); `WARPZONE_SKIP_UPDATER_SH=1` skips them. What each test covers and how CI splits them is in "Testing" and "CI" in `development/design/code-and-testing.md`.

```bash
WARPZONE_E2E_SYNCTHING=/path/to/syncthing python3 -m unittest discover -s tests -k EndToEnd -v
python3 -m unittest discover -s tests -p test_updater_sh.py -v
WARPZONE_SKIP_UPDATER_SH=1 python3 -m unittest discover -s tests -v
WZ_TEST_SHELL=dash python3 -m unittest discover -s tests -p test_updater_sh.py -v
```

The e2e test runs on Linux only in CI. Syncthing behaves differently on macOS and Linux (see below), so a local macOS e2e pass is not proof for Linux.

## Syncthing behaviour worth knowing

Each quirk is explained on the page named; read it before changing Syncthing code.

- Ignore patterns apply only on a full scan, so a whitelist that just synced waits for the next scan (hence `rescan_seconds`): `development/design/hub-and-folders.md`, "Ignore files and the bootstrap problem".
- After a full scan a directory that just became ignored may still be recorded as not ignored (seen on Linux); scan it on its own before trusting its `db/file` record: `development/design/prune.md`, "Safety rules".
- A folder removed and added again under the same ID while the hub connection stays open can end up without the hub receiving its index, with no error: `development/design/profiles.md`, "Frontend and RetroArch overrides" (`previous_paths`), and `provision_folders.hub_disconnected`.
- Syncthing never follows symlinks, and EmuDeck exposes saves through them, so a save folder must point at the real directory: `troubleshooting.md`, "A save folder syncs nothing and shows no error".
- A folder whose `.stignore` includes a file that has not arrived yet is refused, so `provision device` bootstraps with ignores that let only the whitelists through: `development/design/hub-and-folders.md`, "Ignore files and the bootstrap problem".

## Pull requests and commits

- `main` is protected: changes land through pull requests, squash-merged, with all CI checks green. No force pushes.
- Commit messages are a single imperative title line in sentence case, no body and no trailers. Match `git log --oneline`.
- User-visible changes get an entry under `[Unreleased]` in [CHANGELOG.md](CHANGELOG.md) (Keep a Changelog). The guides and the reference pages in `website/src/content/docs/` are updated with new commands or options.
- Releases follow [website/src/content/docs/development/releasing.md](website/src/content/docs/development/releasing.md); the maintainer signs and pushes the tag.
- Do not change `.github/workflows/` unless the task is about CI. Third-party actions stay pinned by commit SHA.
