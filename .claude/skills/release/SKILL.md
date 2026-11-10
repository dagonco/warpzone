---
name: release
description: Use when the maintainer wants to publish a warpzone release or release candidate (vX.Y.Z or vX.Y.ZrcN), cut a version, tag main, or check a release that was published. Follows the releasing guide (website/src/content/docs/development/releasing.md) exactly.
---

# Publish a release

Follow [releasing.md](../../../website/src/content/docs/development/releasing.md) exactly; it is the contract and wins over this summary. Releases are published by pushing a signed tag on `main`, which triggers `.github/workflows/release.yml`. Nothing goes to PyPI.

`main` is protected: changes land through a pull request, squash-merged, with all CI checks green. No force pushes, no direct pushes to `main`. Commit messages are a single imperative title line, no body, no trailers. Do not touch `.github/workflows/`.

Confirm with the user first: the version and that it should ship now. Releases are grouped, not one per feature.

## Version

- Stable: `X.Y.Z`, tag `vX.Y.Z`.
- Candidate: PEP 440 spelling `X.Y.ZrcN`, tag `vX.Y.ZrcN`. Never `-rc1`. It is published as a GitHub pre-release and only devices with `channel = "rc"` follow it. The final `vX.Y.Z` orders after every `rc`.
- Never reuse or move a tag that may have been downloaded.

## Steps

1. Start from an up-to-date `main` and a new branch such as `release/vX.Y.Z`: `git switch main && git pull --ff-only && git switch -c release/vX.Y.Z`.
2. In `CHANGELOG.md`, move the content of `[Unreleased]` into a new `## [X.Y.Z] - YYYY-MM-DD` section (same heading form for an rc, for example `## [0.3.0rc1] - ...`) and leave `[Unreleased]` empty above it. Write for users, not commit by commit.
3. Update the link references at the bottom: `[Unreleased]` compares `vX.Y.Z...HEAD`, and add `[X.Y.Z]` comparing the previous tag with `vX.Y.Z`.
4. Set `__version__` in `warpzone/__init__.py` to the version.
5. Run the checks, and show the output:

   ```bash
   uvx ruff check .
   uvx ruff format --check .
   python3 -m unittest discover -s tests -v
   ```

   One test fails if `CHANGELOG.md` has no section for the current version.
6. Commit (single title line, for example `Prepare the X.Y.Z release`), push the branch and open a pull request with `gh pr create`. Wait for green checks (`gh pr checks --watch`), then squash-merge with the user's OK.
7. Tag the merge commit on `main` and push only that tag. The `verify` job of the workflow refuses a tag that is not annotated and signed with an SSH key listed in `warpzone/data/allowed_signers` of the tagged commit, so always sign the tag (`-s`) and verify it before pushing; if the file holds no key line (only comments), stop: a key must be merged first.

   ```bash
   git switch main && git pull --ff-only
   git tag -s vX.Y.Z -m "warpzone X.Y.Z"
   python3 scripts/verify_tag_signature.py vX.Y.Z --signers warpzone/data/allowed_signers
   git push origin vX.Y.Z
   ```

   Pushing a tag cannot be undone cleanly, so get the user's explicit OK right before it.
8. Watch the release workflow: `gh run list --workflow release.yml --limit 3`, then `gh run watch <id>`. Its jobs are `verify`, `build` and `release`.
9. Verify the result:

   ```bash
   gh release view vX.Y.Z --json tagName,isPrerelease,assets --jq '{tagName,isPrerelease,assets:[.assets[].name]}'
   ```

   Expect the wheel, the sdist and `SHA256SUMS`, the changelog section as notes, and `isPrerelease` true for an rc and false for a stable release.

Devices with the updater timer fetch every hour on `rc` and every 12 hours on `stable`, and move to the new tag in that tick (later if the menu is open); `warpzone self-update --now` on a device looks at once ([self-update.md](../../../website/src/content/docs/operation/self-update.md)).

## When it fails

See "When a release fails" in [releasing.md](../../../website/src/content/docs/development/releasing.md#when-a-release-fails). In short: if `verify` fails (a tag whose signature does not verify included), nothing was published; delete the tag locally and on the remote (`git tag -d vX.Y.Z`, `git push origin :refs/tags/vX.Y.Z`), fix it through a normal pull request and tag again, each with the user's OK. For transient `build` or `release` failures re-run the failed jobs. A wrong release whose code is bad is never fixed by moving the tag: publish a new patch version, and withdraw the bad one with a signed `retired/vX.Y.Z` tag if needed ([Retiring a release](../../../website/src/content/docs/development/releasing.md#retiring-a-release)).
