# Releasing

Releases are cut by pushing a `vX.Y.Z` tag on `main`. The tag triggers `.github/workflows/release.yml`, which verifies, builds and publishes the GitHub Release. Nothing is uploaded to PyPI.

Versions follow [SemVer](https://semver.org/). The single source of the version is `__version__` in `warpzone/__init__.py`; `pyproject.toml` reads it. A pre-release uses the PEP 440 spelling in both the code and the tag (`0.2.0rc1` and `v0.2.0rc1`, not `0.2.0-rc.1`), so the tag can match the package version exactly; any version that is not plain `X.Y.Z` is published as a GitHub pre-release.

## Checklist

1. Start from an up-to-date `main` and a new branch, for example `release/vX.Y.Z`.
2. In `CHANGELOG.md`, rename the content of `[Unreleased]` into a new `## [X.Y.Z] - YYYY-MM-DD` section and leave `[Unreleased]` empty above it. Write for users, not commit by commit.
3. Update the link references at the bottom: `[Unreleased]` compares `vX.Y.Z...HEAD`, and add `[X.Y.Z]` comparing the previous tag with `vX.Y.Z`.
4. Set `__version__` in `warpzone/__init__.py` to `X.Y.Z`. Update the wheel URL in the README install section.
5. Run the checks locally: `uvx ruff check .`, `uvx ruff format --check .`, `python3 -m unittest discover -s tests -v`. One test fails if `CHANGELOG.md` has no section for the current version.
6. Open a pull request, wait for green CI (lint, tests, e2e, secrets) and merge it.
7. Tag the merge commit on `main` with an annotated tag and push only that tag:

   ```bash
   git switch main && git pull --ff-only
   git tag -a vX.Y.Z -m "warpzone X.Y.Z"
   git push origin vX.Y.Z
   ```

8. Watch the `release` workflow in the Actions tab, then open the release and check its notes and assets.

## What the workflow does

- `verify`: fails unless the tag is `v` plus the package version, the tagged commit is contained in `main`, and `CHANGELOG.md` has a non-empty section for that version. Then it runs lint and the unit tests (the e2e test is not repeated: the tagged commit already passed it in CI on `main`).
- `build`: builds the sdist and wheel with a pinned `build`, writes `SHA256SUMS`, creates a build provenance attestation for the wheel and sdist, and uploads everything as a workflow artifact. This job has `id-token` and `attestations` write access but no `contents: write`.
- `release`: the only job with `contents: write`. It creates the GitHub Release for the tag with the changelog section as notes (`scripts/changelog_section.py`) and attaches the wheel, the sdist and `SHA256SUMS`.

Workflow runs are serialised by a concurrency group, so two tags pushed together release one after the other.

Users can verify a download with `sha256sum -c SHA256SUMS` and `gh attestation verify <file> --repo dagonco/warpzone`.

## When a release fails

- `verify` fails (tag and version differ, missing changelog section, tag not on main, lint or test failure): nothing was published. Delete the tag locally and on the remote, fix the problem through a normal pull request, then tag again.

  ```bash
  git tag -d vX.Y.Z
  git push origin :refs/tags/vX.Y.Z
  ```

- `build` or `release` fails for a transient reason (network, runner): re-run the failed jobs from the Actions tab. Re-running `release` is safe only if no release exists for the tag yet.
- The release exists but is wrong (bad notes, wrong asset): edit the notes in the GitHub UI, or `gh release delete vX.Y.Z` (keep the tag) and re-run the workflow. If the code itself is wrong, do not move the tag: publish a new patch version instead.
- Never reuse a version number that users may already have downloaded.
