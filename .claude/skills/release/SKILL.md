---
name: release
description: Use when the maintainer wants to publish a warpzone release or release candidate (vX.Y.Z or vX.Y.ZrcN), cut a version, tag main, or check a release that was published. Follows the releasing guide (website/src/content/docs/development/releasing.md) exactly.
---

# Publish a release

Follow [releasing.md](../../../website/src/content/docs/development/releasing.md) exactly; it is the contract and wins over this summary. The branch, commit and pull request rules are in `AGENTS.md`; do not touch `.github/workflows/`.

Confirm with the user first: the version (stable `X.Y.Z`, or a candidate `X.Y.ZrcN`; see [the version spelling](../../../website/src/content/docs/development/releasing.md#publishing-a-release-candidate)) and that it should ship now. Releases are grouped, not one per feature. Never reuse or move a tag that may have been downloaded.

## Steps

Each step is detailed in the checklist of [releasing.md](../../../website/src/content/docs/development/releasing.md#checklist).

1. `git switch main && git pull --ff-only && git switch -c release/vX.Y.Z`
2. Move `[Unreleased]` in `CHANGELOG.md` into a `## [X.Y.Z] - YYYY-MM-DD` section, write it for users, and fix the link references at the bottom.
3. Set `__version__` in `warpzone/__init__.py`.
4. Run the checks of the checklist (step 5) and show the output.
5. Commit with a single title line (for example `Prepare the X.Y.Z release`), push, `gh pr create`, wait with `gh pr checks --watch`, and squash-merge with the user's OK.
6. Tag the merge commit and verify it before pushing:

   ```bash
   git switch main && git pull --ff-only
   git tag -s vX.Y.Z -m "warpzone X.Y.Z"
   python3 scripts/verify_tag_signature.py vX.Y.Z --signers warpzone/data/allowed_signers
   git push origin vX.Y.Z
   ```

   The maintainer's key signs the tag (`-s`; see [Signing a tag](../../../website/src/content/docs/development/releasing.md#signing-a-tag)). If `warpzone/data/allowed_signers` holds no key line, stop: a key must be merged first. Pushing a tag cannot be undone cleanly, so get the user's explicit OK right before `git push origin vX.Y.Z`.
7. Watch the workflow: `gh run list --workflow release.yml --limit 3`, then `gh run watch <id>`.
8. Verify the result and show it:

   ```bash
   gh release view vX.Y.Z --json tagName,isPrerelease,assets --jq '{tagName,isPrerelease,assets:[.assets[].name]}'
   ```

   Expect the wheel, the sdist and `SHA256SUMS`, the changelog section as notes, and `isPrerelease` true only for a candidate.

## When it fails

Follow [When a release fails](../../../website/src/content/docs/development/releasing.md#when-a-release-fails) and, for a release whose code is bad, [Retiring a release](../../../website/src/content/docs/development/releasing.md#retiring-a-release). Deleting a tag locally and on the remote, and every other tag operation, needs the user's explicit OK each time.
