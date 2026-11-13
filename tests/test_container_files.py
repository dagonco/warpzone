"""The hub's container files: the pins and rules that keep the image reproducible and the example
free of secrets. The image itself is not built here (the weekly `image` job of CI builds it); these
tests read the files."""

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DOCKERFILE = ROOT / "docker" / "Dockerfile"
DOCKERIGNORE = ROOT / ".dockerignore"
COMPOSE = ROOT / "examples" / "compose.example.yaml"
NSZ_REQUIREMENTS = ROOT / "docker" / "nsz-requirements.txt"

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

    def test_dolphin_is_pinned_by_commit_and_verified_and_only_dolphin_tool_is_built(self):
        match = re.search(r"^ARG DOLPHIN_COMMIT=([0-9a-f]{40})$", self.text, re.MULTILINE)
        self.assertIsNotNone(match, "Dolphin must be pinned by a 40-character commit")
        self.assertRegex(self.text, r"(?m)^ARG DOLPHIN_TAG=\S+$")
        self.assertIn('test "$(git rev-parse HEAD)" = "${DOLPHIN_COMMIT}"', self.text)
        self.assertIn("--target dolphin-tool", self.text)
        self.assertIn("-DUSE_SYSTEM_LIBS=OFF", self.text)
        self.assertIn("-DENABLE_QT=OFF", self.text)

    def test_orbis_pkg_util_is_pinned_by_checksum_and_built_from_its_lock_file(self):
        self.assertRegex(self.text, r"(?m)^ARG ORBIS_PKG_UTIL_SHA256=[0-9a-f]{64}$")
        self.assertIn('echo "${ORBIS_PKG_UTIL_SHA256}  orbis.crate" | sha256sum -c -', self.text)
        self.assertIn("cargo build --release --locked", self.text)

    def test_skyscraper_is_pinned_by_commit_verified_and_built_with_its_files_bundled(self):
        match = re.search(r"^ARG SKYSCRAPER_COMMIT=([0-9a-f]{40})$", self.text, re.MULTILINE)
        self.assertIsNotNone(match, "Skyscraper must be pinned by a 40-character commit")
        self.assertRegex(self.text, r"(?m)^ARG SKYSCRAPER_TAG=3\.21\.1$")
        self.assertIn('test "$(git rev-parse HEAD)" = "${SKYSCRAPER_COMMIT}"', self.text)
        # One file, with peas.json and the rest inside it: nothing to install under /usr/local/etc.
        self.assertIn("BUNDLE_QRC=1 qmake6", self.text)
        self.assertNotIn("make install", self.text)

    def test_skyscraper_keeps_its_home_on_the_volume(self):
        final = "\n".join(stages(self.text)[-1])
        self.assertIn("COPY --from=skyscraper /out/Skyscraper /opt/skyscraper/Skyscraper", final)
        # The script on PATH gives the program its own HOME, whose .skyscraper is the volume.
        self.assertIn("HOME=/opt/skyscraper-home; export HOME", final)
        self.assertIn('exec /opt/skyscraper/Skyscraper "$@"', final)
        self.assertIn("ln -s /skyscraper /opt/skyscraper-home/.skyscraper", final)
        # warpzone's own HOME stays on /tmp, and it is told where Skyscraper's config is.
        self.assertIn("HOME=/tmp", final)
        self.assertIn("WARPZONE_SKYSCRAPER_DIR=/skyscraper", final)
        # Nothing of it is created in the image: the volume is the user's.
        self.assertNotIn("mkdir /skyscraper", final)

    def test_nsz_is_installed_from_hashed_wheels_in_its_own_environment(self):
        installs = [line for line in self.lines if "/opt/nsz/bin/python install" in line]
        self.assertEqual(len(installs), 1, installs)
        for flag in ("--require-hashes", "--no-deps", "--only-binary=:all:"):
            self.assertIn(flag, installs[0])
        self.assertIn("python -m venv --without-pip /opt/nsz", self.text)
        # Every package of the requirements file is pinned to a version and has hashes.
        requirements = NSZ_REQUIREMENTS.read_text(encoding="utf-8")
        entries = re.findall(
            r"(?m)^([A-Za-z0-9_.-]+)==(\S+)(?: ;[^\\]*)? \\\n((?:\s+--hash=\S+.*\n)+)", requirements
        )
        self.assertIn("nsz", [name for name, _, _ in entries])
        self.assertEqual(len(entries), len(re.findall(r"(?m)^\S+==", requirements)))
        for name, _, hashes in entries:
            self.assertRegex(hashes, r"--hash=sha256:[0-9a-f]{64}", name)

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
                elif rest[0] == "--python" and rest[2] == "install":
                    # nsz, in a virtual environment of its own: hashed wheels only.
                    self.assertEqual(rest[1], "/opt/nsz/bin/python", command)
                    self.assertIn("--require-hashes", rest)
                    self.assertIn("--no-deps", rest)
                    self.assertIn("--only-binary=:all:", rest)
                    self.assertEqual(rest[rest.index("-r") + 1], "/tmp/nsz-requirements.txt")
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
        for word in ("build-essential", "gcc", "git ", "curl", "make "):
            self.assertNotIn(word, joined, f"{word!r} in the final stage")
        users = [line.split()[1] for line in final if line.startswith("USER ")]
        self.assertEqual(len(users), 1)
        self.assertNotIn(users[0].split(":")[0], ("0", "root"))
        self.assertIn("COPY --from=rahasher /out/RAHasher /usr/local/bin/RAHasher", joined)
        self.assertIn("COPY --from=dolphin /out/dolphin-tool /usr/local/bin/dolphin-tool", joined)
        self.assertIn("COPY --from=orbis /out/orbis-pkg-util /usr/local/bin/orbis-pkg-util", joined)
        self.assertIn("COPY --from=nsz /opt/nsz /opt/nsz", joined)
        self.assertIn("ln -s /opt/nsz/bin/nsz /usr/local/bin/nsz", joined)
        self.assertEqual(final[-1], 'CMD ["serve"]')
        self.assertIn('ENTRYPOINT ["warpzone"]', final)

    def test_final_stage_installs_only_the_inbox_tools_and_cleans_apt(self):
        final = stages(self.text)[-1]
        runs = [line for line in final if "apt-get" in line]
        self.assertEqual(len(runs), 1, runs)
        run = runs[0]
        self.assertIn("apt-get update", run)
        self.assertIn("apt-get install -y --no-install-recommends", run)
        self.assertIn("rm -rf /var/lib/apt/lists/*", run)
        self.assertLess(run.index("apt-get install"), run.index("rm -rf /var/lib/apt/lists"))
        install = run.split("apt-get install", 1)[1].split("&&")[0]
        packages = [w for w in install.split() if not w.startswith("-")]
        # chdman is in mame-tools, bsdtar in libarchive-tools (Debian trixie); Skyscraper links
        # Qt 6 (core, gui for images, network, xml, sql) and loads Qt's SQLite driver.
        self.assertEqual(
            sorted(packages),
            [
                "libarchive-tools",
                "libqt6gui6",
                "libqt6network6",
                "libqt6sql6",
                "libqt6sql6-sqlite",
                "libqt6xml6",
                "mame-tools",
            ],
        )

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
        self.assertEqual(
            included,
            {"pyproject.toml", "README.md", "LICENSE", "warpzone", "docker/nsz-requirements.txt"},
        )
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

    def test_inbox_service_mounts_keys_and_data_read_only_and_never_the_share_for_them(self):
        inbox = self.code.split("warpzone-inbox:", 1)[1]
        self.assertRegex(inbox, r"- \./dats:/dats:ro")
        self.assertRegex(inbox, r"- \./keys:/keys:ro")
        self.assertRegex(inbox, r"- \./work:/work\n")
        self.assertIn('profiles: ["inbox"]', inbox)
        self.assertRegex(inbox, r'user: "\d+:\d+"')
        # The keys and data the tools read are named in the comments, with the paths mounted.
        for key in ("switch_keys", "vita_zrif", "titledb_dir", "dats_dir", "work_dir"):
            self.assertRegex(self.text, rf"#\s+{key} = \"/(keys|dats|work)/", key)

    def test_the_inbox_service_keeps_skyscraper_config_and_cache_on_a_volume(self):
        inbox = self.code.split("warpzone-inbox:", 1)[1]
        # Read-write (the cache grows by about 2.6 MB a game), on disk and not in the tmpfs.
        self.assertRegex(inbox, r"- \./skyscraper:/skyscraper\n")
        self.assertNotRegex(inbox, r"- \./skyscraper:/skyscraper:ro")
        # config.ini may hold the ScreenScraper account: the comments say warpzone never reads it.
        self.assertIn("config.ini", self.text)
        self.assertIn("userCreds", self.text)

    def test_has_no_secret_looking_value(self):
        # No assignment of a key, token or password to a value, in the code or in a comment.
        for line in self.text.splitlines():
            if re.match(r"\s*-\s+\S+:/\S+", line):  # a volume: `./keys:/keys:ro` names a folder
                continue
            match = re.search(r"(KEY|TOKEN|SECRET|PASSWORD)\w*\s*[=:]\s*(\S+)", line, re.IGNORECASE)
            if match:
                value = match.group(2).strip("\"'")
                self.assertRegex(
                    value,
                    r"^(<.*|/(config|keys)/[\w.]+|[A-Z_]+)$|^$",
                    f"looks like a secret: {line.strip()}",
                )
        # No long random-looking string at all (a key, a token, a Syncthing device ID).
        without_urls = re.sub(r"https?://\S+", "", self.text)
        for token in re.findall(r"[A-Za-z0-9+/_=-]{32,}", without_urls):
            self.fail(f"long random-looking string: {token[:8]}...")
        self.assertNotRegex(self.text, r"\b[A-Z2-7]{7}(-[A-Z2-7]{7}){7}\b")


if __name__ == "__main__":
    unittest.main()
