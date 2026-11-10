#!/usr/bin/env python3
"""Exit 0 only when a tag is annotated and carries one SSH signature that an allowed key made.

Used by the release workflow as a gate: a tag that the self-updating devices would refuse
(warpzone/data/updater.sh, `tag_signed`) must not get artifacts built and a release published.
It mirrors the updater's guards: an annotated tag, exactly one signature block, that block an
SSH one, the object naming the tag it is published under, and `git verify-tag` with only the
SSH verifier switched on and the machine's git configuration left unread. Unlike the device,
which tolerates a signers file without keys while no key has been published, an empty file
fails here: a release must always be signed.
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

TIMEOUT_SECONDS = 30
_BLOCK = re.compile(r"^-----BEGIN ", re.MULTILINE)
_SSH_BLOCK = re.compile(r"^-----BEGIN SSH SIGNATURE-----$", re.MULTILINE)
_KEY_LINE = re.compile(r"^\s*[^\s#]", re.MULTILINE)


class Refused(Exception):
    """The tag does not pass; the message is the reason."""


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    # The user's and the system's git configuration must not choose the verifier or the keys.
    env = {**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        stdin=subprocess.DEVNULL,
        env=env,
        timeout=TIMEOUT_SECONDS,
        check=False,
    )


def check_signers(path: Path) -> None:
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise Refused(f"the allowed signers file {path} cannot be read: {exc}") from exc
    if not _KEY_LINE.search(text):
        raise Refused(f"the allowed signers file {path} holds no key, so no tag can be trusted")


def verify(tag: str, signers: Path, repo: Path) -> str:
    """Return the message to print on success; raise Refused with the reason otherwise."""
    check_signers(signers)
    if shutil.which("ssh-keygen") is None:
        raise Refused("ssh-keygen is not installed, so no SSH signature can be checked")
    ref = f"refs/tags/{tag}"
    kind = _git(repo, "cat-file", "-t", ref)
    if kind.returncode != 0:
        raise Refused(f"there is no tag {tag} in {repo}")
    if kind.stdout.strip() != "tag":
        raise Refused(f"{tag} is not an annotated tag (it is a lightweight tag); sign it with -s")
    obj = _git(repo, "cat-file", "tag", ref)
    if obj.returncode != 0:
        raise Refused(f"cannot read the tag object of {tag}")
    blocks = len(_BLOCK.findall(obj.stdout))
    if blocks == 0:
        raise Refused(f"{tag} carries no signature; sign it with -s")
    if blocks != 1:
        raise Refused(f"{tag} must carry exactly one signature, it has {blocks}")
    # verify-tag picks the verifier from the block it finds, not from gpg.format: an OpenPGP or
    # X.509 block would be checked by gpg against a keyring and never reach the allowed signers.
    if not _SSH_BLOCK.search(obj.stdout):
        raise Refused(f"{tag} is not signed with an SSH signature (OpenPGP and X.509 are refused)")
    # The signature covers the object, which names the tag it was made for; the ref can say
    # anything. A signed v0.2.0 published as v0.3.0 is not v0.3.0.
    named = next(
        (ln[4:] for ln in obj.stdout.split("\n\n", 1)[0].splitlines() if ln[:4] == "tag "), ""
    )
    if named != tag:
        raise Refused(f"the signed object is for the tag '{named}', not for '{tag}'")
    result = _git(
        repo,
        *("-c", "gpg.format=ssh", "-c", "gpg.ssh.program=ssh-keygen", "-c", "gpg.program=false"),
        *("-c", "gpg.openpgp.program=false", "-c", "gpg.x509.program=false"),
        *("-c", f"gpg.ssh.allowedSignersFile={signers.resolve()}"),
        *("verify-tag", ref),
    )
    if result.returncode != 0:
        detail = (result.stderr.strip().splitlines() or ["no detail"])[-1]
        raise Refused(f"the signature of {tag} does not verify against {signers} ({detail})")
    return f"{tag} is signed by a key listed in {signers}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("tag", help="tag name, e.g. v0.4.0")
    parser.add_argument("--signers", required=True, type=Path, help="allowed signers file")
    parser.add_argument("--repo", default=Path("."), type=Path, help="repository (default: .)")
    args = parser.parse_args(argv)
    try:
        print(verify(args.tag, args.signers, args.repo))
    except Refused as exc:
        print(f"verify_tag_signature: {exc}", file=sys.stderr)
        return 1
    except (OSError, subprocess.TimeoutExpired) as exc:
        print(f"verify_tag_signature: cannot run git or ssh-keygen: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
