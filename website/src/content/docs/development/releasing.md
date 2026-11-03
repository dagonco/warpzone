---
title: "Releasing"
---

Releases are cut by pushing a `vX.Y.Z` tag on `main`. The tag triggers `.github/workflows/release.yml`, which verifies, builds and publishes the GitHub Release. Nothing is uploaded to PyPI.

Versions follow [SemVer](https://semver.org/). The single source of the version is `__version__` in `warpzone/__init__.py`; `pyproject.toml` reads it. A pre-release uses the PEP 440 spelling in both the code and the tag (`0.2.0rc1` and `v0.2.0rc1`, not `0.2.0-rc.1`), so the tag can match the package version exactly; any version that is not plain `X.Y.Z` is published as a GitHub pre-release.

## Checklist

1. Start from an up-to-date `main` and a new branch, for example `release/vX.Y.Z`.
2. In `CHANGELOG.md`, rename the content of `[Unreleased]` into a new `## [X.Y.Z] - YYYY-MM-DD` section and leave `[Unreleased]` empty above it. Write for users, not commit by commit.
3. Update the link references at the bottom: `[Unreleased]` compares `vX.Y.Z...HEAD`, and add `[X.Y.Z]` comparing the previous tag with `vX.Y.Z`.
4. Set `__version__` in `warpzone/__init__.py` to `X.Y.Z`. Update the wheel URL and filename in the README install section.
5. Run the checks locally: `uvx ruff check .`, `uvx ruff format --check .`, `python3 -m unittest discover -s tests -v`. One test fails if `CHANGELOG.md` has no section for the current version.
6. Open a pull request, wait for green CI (lint, tests, e2e, secrets) and merge it.
7. Tag the merge commit on `main` with an annotated tag and push only that tag:

   ```bash
   git switch main && git pull --ff-only
   git tag -a vX.Y.Z -m "warpzone X.Y.Z"
   git push origin vX.Y.Z
   ```

8. Watch the `release` workflow in the Actions tab, then open the release and check its notes and assets. Devices with the updater timer ([Updating a device](/warpzone/operation/self-update/)) fetch once a day and check out the new tag at the tick after that.

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

## Signing a tag

Devices that update themselves accept only tags signed with the maintainer's SSH key, once its public key is in `warpzone/data/allowed_signers` of the version they are running. Until a key is there they accept unsigned tags and say so in their log. This covers release and candidate tags, a `pin` and the `retired/` tags below.

One-time setup, with a key used only for this (an `ed25519` key is enough):

```bash
ssh-keygen -t ed25519 -f ~/.ssh/warpzone-release -C "warpzone release"
git config gpg.format ssh
git config user.signingkey ~/.ssh/warpzone-release.pub
```

Put the public key in `warpzone/data/allowed_signers` through a normal pull request, one line per key (`<principal> namespaces="git" <key type> <public key>`; the namespace is the one `git` signs with). A device reads the keys from the version it runs, so a key must be added in a release before the first tag signed with it is needed: tags are signed from the release after that. The first release that carries keys is taken on trust, with no signature check, because nothing on the device says yet whom to trust: whoever can publish that release chooses the first key. After it, signatures stay required: even if a device later goes down to an old release without the file, it keeps the last keys it trusted. To rotate, add the new key in a release signed with the **previous** key (a release cannot change the keys that check itself, and one signed with the new key alone would be refused); devices that move to it trust both, and then you can sign with the new one. Tags must be signed with SSH: a tag signed with OpenPGP or X.509 is refused.

Sign the tag instead of just annotating it in step 7 of the checklist (`-s` replaces `-a`), and check it before pushing:

```bash
git tag -s vX.Y.Z -m "warpzone X.Y.Z"
git -c gpg.ssh.allowedSignersFile=warpzone/data/allowed_signers verify-tag vX.Y.Z
git push origin vX.Y.Z
```

An unsigned or lightweight tag, or one signed by a key that is not in the file, is ignored by every device that checks signatures. The workflow does not check signatures: it only needs the tag to match the package version.

## Publishing a release candidate

A release candidate is a normal release with a PEP 440 version. Set `__version__` to `X.Y.ZrcN` (for example `0.3.0rc1`), add a `## [0.3.0rc1] - YYYY-MM-DD` section to the changelog (the workflow refuses a tag without one), follow the checklist and tag `v0.3.0rc1` (signed, as above). The workflow publishes it as a GitHub pre-release. The final release `v0.3.0` is the next version and orders after every `rc`; `v0.3.0rc2` orders after `v0.3.0rc1` and `rc10` after `rc2`. Only devices with `channel = "rc"` follow candidates; those on `stable` ignore them until the release. Do not spell it `v0.3.0-rc1`: no device knows that tag.

## Retiring a release

A tag cannot be edited, and a published release must never be moved or reused. If a release turns out to be harmful, withdraw it with a second tag, `retired/vX.Y.Z` (or `retired/vX.Y.ZrcN`), annotated and signed like a release, on the release it withdraws, with the reason as its message:

```bash
git tag -s retired/v0.3.0 v0.3.0 -m "settings apply overwrote a captured key"
git push origin retired/v0.3.0
```

The tag must be named `retired/` plus the exact tag it withdraws and point at that release (its tag or its commit, which is the same to a device): a retirement made for another name or another release is ignored, and so is a signed object published under another name. There is no file to edit and no pull request: the tag is the retirement, and devices take it with their daily fetch. The `release` workflow does not run for it (it only runs for `v*.*.*`). When the allowed signers hold a key, a retirement that is not signed by one of them (or is only a lightweight tag) is ignored, so nobody without the key can pull a good version. From then on:

- no device picks the tag as a target;
- a device that runs it moves to the best release that is not retired, even a lower one, after verifying it like any update;
- a device with `pin` set to the tag stays on it (a pin wins) and records it in its state so that `doctor` can warn.

A retirement cannot be undone on a device that has already fetched it, so publish a fixed version as a new patch release, so devices have something to move up to.
