"""The hub's container files: the pins and rules that keep the image reproducible and the example
free of secrets. The image itself is not built here (CI has no Docker step); these tests read the
files."""

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DOCKERFILE = ROOT / "docker" / "Dockerfile"
DOCKERIGNORE = ROOT / ".dockerignore"
COMPOSE = ROOT / "examples" / "compose.example.yaml"

DIGEST = re.compile(r"@sha256:[0-9a-f]{64}$")


def instructions(text: str) -> list[str]:
    """The Dockerfile's instructions, each on one line (continuations joined, comments dropped)."""
    joined = re.sub(r"\\\s*\n", " ", text)
    lines = []
    for line in joined.splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            lines.append(re.sub(r"\s+", " ", line))
    return lines


def stages(text: str) -> list[list[str]]:
    """The instructions of each build stage, in order."""
    result: list[list[str]] = []
    for line in instructions(text):
        if line.upper().startswith("FROM "):
            result.append([])
        if result:
            result[-1].append(line)
    return result


class DockerfileTest(unittest.TestCase):
    def setUp(self):
        self.text = DOCKERFILE.read_text(encoding="utf-8")
        self.lines = instructions(self.text)

    def test_every_base_image_is_pinned_by_digest(self):
        names = set()
        froms = [line for line in self.lines if line.upper().startswith("FROM ")]
        self.assertGreaterEqual(len(froms), 2)
        for line in froms:
            parts = line.split()
            image = parts[1]
            if image not in names:  # a stage name is not an image
                self.assertRegex(image, DIGEST, f"not pinned by digest: {line}")
            if len(parts) >= 4 and parts[2].upper() == "AS":
                names.add(parts[3])
        for line in self.lines:
            match = re.search(r"--from=(\S+)", line)
            if match and not match.group(1).isdigit():
                self.assertIn(match.group(1), names, f"unknown stage in: {line}")

    def test_ralibretro_is_pinned_by_commit_and_verified(self):
        match = re.search(r"^ARG RALIBRETRO_COMMIT=([0-9a-f]{40})$", self.text, re.MULTILINE)
        self.assertIsNotNone(match, "RALibretro must be pinned by a 40-character commit")
        self.assertRegex(self.text, r"(?m)^ARG RALIBRETRO_TAG=\S+$")
        self.assertIn('test "$(git rev-parse HEAD)" = "${RALIBRETRO_COMMIT}"', self.text)
        self.assertIn("HAVE_CHD=1", self.text)
        self.assertIn("-f Makefile.RAHasher", self.text)

    def test_has_a_healthcheck_on_healthz_without_curl(self):
        checks = [line for line in self.lines if line.startswith("HEALTHCHECK ")]
        self.assertEqual(len(checks), 1)
        self.assertIn("/healthz", checks[0])
        self.assertNotIn("curl", checks[0])
        self.assertNotIn("wget", checks[0])

    def test_pip_only_touches_the_local_package(self):
        for line in self.lines:
            for command in re.split(r"&&|;|\|", line):
                words = command.split()
                if "pip" not in words:
                    continue
                rest = words[words.index("pip") + 1 :]
                if rest[0] == "wheel":
                    self.assertEqual(rest[-1], ".", f"pip wheel of something else: {command}")
                    self.assertIn("--no-deps", rest)
                elif rest[0] == "install":
                    self.assertIn("--no-index", rest, f"pip install may reach an index: {command}")
                    self.assertIn("--no-deps", rest)
                    targets = [w for w in rest[1:] if not w.startswith("-")]
                    self.assertEqual(targets, ["/wheels/warpzone-*.whl"], command)
                else:
                    self.fail(f"unexpected pip command: {command}")

    def test_final_stage_has_no_build_tools_and_is_not_root(self):
        final = stages(self.text)[-1]
        joined = "\n".join(final)
        for word in ("apt-get", "build-essential", "gcc", "git ", "curl", "make "):
            self.assertNotIn(word, joined, f"{word!r} in the final stage")
        users = [line.split()[1] for line in final if line.startswith("USER ")]
        self.assertEqual(len(users), 1)
        self.assertNotIn(users[0].split(":")[0], ("0", "root"))
        self.assertIn("COPY --from=rahasher /out/RAHasher /usr/local/bin/RAHasher", joined)
        self.assertEqual(final[-1], 'CMD ["serve"]')
        self.assertIn('ENTRYPOINT ["warpzone"]', final)

    def test_installs_the_checkout_not_a_download(self):
        self.assertIn("COPY warpzone ./warpzone", self.text)
        self.assertNotRegex(self.text, r"pip install[^\n]*(git\+|https?://|warpzone==)")


class DockerignoreTest(unittest.TestCase):
    def test_context_is_only_what_the_wheel_needs(self):
        lines = [
            line.strip()
            for line in DOCKERIGNORE.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.startswith("#")
        ]
        self.assertEqual(lines[0], "*")
        included = {line[1:] for line in lines if line.startswith("!")}
        self.assertEqual(included, {"pyproject.toml", "README.md", "LICENSE", "warpzone"})
        for name in ("website", "tests", "node_modules", ".git"):
            self.assertNotIn(name, included)


class ComposeExampleTest(unittest.TestCase):
    def setUp(self):
        self.text = COMPOSE.read_text(encoding="utf-8")
        self.code = "\n".join(
            line for line in self.text.splitlines() if not line.lstrip().startswith("#")
        )

    def test_has_what_the_design_asks_for(self):
        self.assertIn("8780:8780", self.code)
        self.assertIn("restart: unless-stopped", self.code)
        self.assertRegex(self.code, r"mem_limit: \d+[mg]")
        self.assertRegex(self.code, r"- \./config:/config:ro")
        self.assertRegex(self.code, r"- \./warpzone\.env")
        self.assertIn("network_mode: host", self.text)  # the commented alternative
        self.assertRegex(self.code, r'user: "\d+:\d+"')
        self.assertNotRegex(self.code, r'user: "(0|root)\b')
        # The hub root is mounted at the same path inside and outside.
        mount = re.search(r"- (/\S+):(/\S+)\n", self.code)
        self.assertIsNotNone(mount)
        assert mount is not None
        self.assertEqual(mount.group(1), mount.group(2))
        self.assertNotRegex(mount.group(0), r":ro")

    def test_has_no_secret_looking_value(self):
        # No assignment of a key, token or password to a value, in the code or in a comment.
        for line in self.text.splitlines():
            match = re.search(r"(KEY|TOKEN|SECRET|PASSWORD)\w*\s*[=:]\s*(\S+)", line, re.IGNORECASE)
            if match:
                value = match.group(2).strip("\"'")
                self.assertRegex(
                    value,
                    r"^(<.*|/config/[\w.]+|[A-Z_]+)$|^$",
                    f"looks like a secret: {line.strip()}",
                )
        # No long random-looking string at all (a key, a token, a Syncthing device ID).
        without_urls = re.sub(r"https?://\S+", "", self.text)
        for token in re.findall(r"[A-Za-z0-9+/_=-]{32,}", without_urls):
            self.fail(f"long random-looking string: {token[:8]}...")
        self.assertNotRegex(self.text, r"\b[A-Z2-7]{7}(-[A-Z2-7]{7}){7}\b")


if __name__ == "__main__":
    unittest.main()
