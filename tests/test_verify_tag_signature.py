"""Tests for scripts/verify_tag_signature.py, the release workflow's signature gate.

Each test builds a throwaway git repository and ed25519 keys in a temporary folder, isolated
from the git configuration of the machine (same environment handling as test_updater_sh.py).
"""

import contextlib
import importlib.util
import io
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location(
    "verify_tag_signature", ROOT / "scripts" / "verify_tag_signature.py"
)
verify_tag_signature = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(verify_tag_signature)

GIT = shutil.which("git")
SSH_KEYGEN = shutil.which("ssh-keygen")

GIT_ENV = {
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_SYSTEM": os.devnull,
    "GIT_AUTHOR_NAME": "Test",
    "GIT_AUTHOR_EMAIL": "test@example.invalid",
    "GIT_COMMITTER_NAME": "Test",
    "GIT_COMMITTER_EMAIL": "test@example.invalid",
    "GIT_TERMINAL_PROMPT": "0",
}


def git(cwd: Path, *args: str, stdin: str | None = None) -> str:
    proc = subprocess.run(
        ["git", "-C", str(cwd), *args],
        capture_output=True,
        text=True,
        input=stdin,
        env={**os.environ, **GIT_ENV},
        check=False,
    )
    if proc.returncode != 0:
        raise AssertionError(f"git {' '.join(args)}: {proc.stderr}")
    return proc.stdout.strip()


class Key:
    """An SSH key made for one test, and the allowed-signers line that trusts it."""

    def __init__(self, folder: Path, name: str) -> None:
        self.private = folder / name
        subprocess.run(
            ["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", "t", "-f", str(self.private)],
            check=True,
            capture_output=True,
        )
        public = self.private.with_suffix(".pub").read_text().strip()
        self.allowed = f'test@example.invalid namespaces="git" {public}\n'


def _can_sign_with_ssh() -> bool:
    """True when this git can make an SSH-signed tag (git 2.34 or newer, ssh-keygen present)."""
    if not (GIT and SSH_KEYGEN):
        return False
    with tempfile.TemporaryDirectory(prefix="wz-sigprobe-") as tmp:
        folder = Path(tmp)
        try:
            key = Key(folder, "probe")
            repo = folder / "repo"
            subprocess.run(
                ["git", "init", "-q", str(repo)], check=True, env={**os.environ, **GIT_ENV}
            )
            git(repo, "commit", "-q", "--allow-empty", "-m", "probe")
            git(
                repo,
                *("-c", "gpg.format=ssh", "-c", f"user.signingkey={key.private}"),
                *("tag", "-s", "-m", "probe", "probe"),
            )
        except (AssertionError, OSError, subprocess.CalledProcessError):
            return False
    return True


@unittest.skipUnless(_can_sign_with_ssh(), "git or ssh-keygen cannot make SSH signatures here")
class VerifyTagSignatureTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory(prefix="wz-verify-tag-")
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.repo = self.root / "repo"
        subprocess.run(
            ["git", "init", "-q", "-b", "main", str(self.repo)],
            check=True,
            env={**os.environ, **GIT_ENV},
        )
        git(self.repo, "commit", "-q", "--allow-empty", "-m", "release")
        self.maintainer = Key(self.root, "maintainer")
        self.stranger = Key(self.root, "stranger")
        self.signers = self.root / "allowed_signers"
        self.signers.write_text("# test signers\n" + self.maintainer.allowed)

    def sign(self, name: str, key: Key) -> None:
        git(
            self.repo,
            *("-c", "gpg.format=ssh", "-c", f"user.signingkey={key.private}"),
            *("tag", "-s", "-m", name, name),
        )

    def run_main(self, tag: str, signers: Path | None = None) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        argv = [tag, "--signers", str(signers or self.signers), "--repo", str(self.repo)]
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = verify_tag_signature.main(argv)
        return code, out.getvalue(), err.getvalue()

    def test_a_tag_signed_by_a_listed_key_passes(self) -> None:
        self.sign("v0.1.0", self.maintainer)
        code, _, err = self.run_main("v0.1.0")
        self.assertEqual((code, err), (0, ""))

    def test_an_annotated_tag_without_a_signature_fails(self) -> None:
        git(self.repo, "tag", "-a", "-m", "v0.1.0", "v0.1.0")
        code, out, err = self.run_main("v0.1.0")
        self.assertEqual((code, out), (1, ""))
        self.assertIn("signature", err)

    def test_a_lightweight_tag_fails(self) -> None:
        git(self.repo, "tag", "v0.1.0")
        code, _, err = self.run_main("v0.1.0")
        self.assertEqual(code, 1)
        self.assertIn("not an annotated tag", err)

    def test_a_tag_signed_by_a_key_that_is_not_listed_fails(self) -> None:
        self.sign("v0.1.0", self.stranger)
        code, _, err = self.run_main("v0.1.0")
        self.assertEqual(code, 1)
        self.assertIn("does not verify", err)

    def test_a_signers_file_with_only_comments_fails(self) -> None:
        self.sign("v0.1.0", self.maintainer)
        empty = self.root / "empty_signers"
        empty.write_text("# no keys yet\n\n   \n")
        code, _, err = self.run_main("v0.1.0", empty)
        self.assertEqual(code, 1)
        self.assertIn("no key", err)

    def test_a_missing_signers_file_fails(self) -> None:
        self.sign("v0.1.0", self.maintainer)
        code, _, err = self.run_main("v0.1.0", self.root / "nope")
        self.assertEqual(code, 1)
        self.assertIn("allowed signers", err)

    def test_a_tag_that_does_not_exist_fails(self) -> None:
        code, _, err = self.run_main("v9.9.9")
        self.assertEqual(code, 1)
        self.assertIn("v9.9.9", err)

    def test_a_signed_object_published_under_another_name_fails(self) -> None:
        self.sign("v0.1.0", self.maintainer)
        git(self.repo, "update-ref", "refs/tags/v0.2.0", "refs/tags/v0.1.0")
        code, _, err = self.run_main("v0.2.0")
        self.assertEqual(code, 1)
        self.assertIn("v0.1.0", err)

    def test_a_tag_with_an_openpgp_block_is_refused_before_gpg_runs(self) -> None:
        commit = git(self.repo, "rev-parse", "HEAD")
        body = (
            f"object {commit}\ntype commit\ntag v0.1.0\n"
            "tagger Test <test@example.invalid> 1700000000 +0000\n\nv0.1.0\n"
            "-----BEGIN PGP SIGNATURE-----\n\nnot a real signature\n-----END PGP SIGNATURE-----\n"
        )
        sha = git(self.repo, "hash-object", "-t", "tag", "-w", "--stdin", stdin=body)
        git(self.repo, "update-ref", "refs/tags/v0.1.0", sha)
        code, _, err = self.run_main("v0.1.0")
        self.assertEqual(code, 1)
        self.assertIn("SSH signature", err)

    def test_a_tag_with_two_signature_blocks_is_refused(self) -> None:
        self.sign("v0.1.0", self.maintainer)
        raw = git(self.repo, "cat-file", "tag", "v0.1.0")
        block = raw[raw.index("-----BEGIN SSH SIGNATURE-----") :]
        sha = git(self.repo, "hash-object", "-t", "tag", "-w", "--stdin", stdin=raw + "\n" + block)
        git(self.repo, "update-ref", "refs/tags/v0.1.0", sha)
        code, _, err = self.run_main("v0.1.0")
        self.assertEqual(code, 1)
        self.assertIn("one signature", err)

    def test_the_configuration_of_the_machine_is_not_read(self) -> None:
        # a global configuration that points verification at a key-less file must not matter,
        # and one that asks for another verifier must not run it
        self.sign("v0.1.0", self.maintainer)
        config = self.root / "gitconfig"
        config.write_text("[gpg]\n\tformat = openpgp\n\tprogram = false\n")
        old = os.environ.get("GIT_CONFIG_GLOBAL")
        os.environ["GIT_CONFIG_GLOBAL"] = str(config)
        try:
            code, _, err = self.run_main("v0.1.0")
        finally:
            if old is None:
                del os.environ["GIT_CONFIG_GLOBAL"]
            else:
                os.environ["GIT_CONFIG_GLOBAL"] = old
        self.assertEqual((code, err), (0, ""))


if __name__ == "__main__":
    unittest.main()
