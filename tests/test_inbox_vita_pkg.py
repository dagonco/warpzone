import base64
import io
import os
import shutil
import struct
import tempfile
import unittest
import zlib
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import ClassVar
from unittest import mock

from inbox_support import make_stub
from test_inbox import Base
from test_inbox_vita import ENCRYPTED, rif, vita_sfo
from warpzone import inbox, inbox_core
from warpzone import inbox_vita_pkg as vp
from warpzone.config import InboxSettings
from warpzone.inbox_core import InboxError, Tools

TITLE_ID = "PCSF00024"
CONTENT_ID = f"EP9000-{TITLE_ID}_00-PDEUAP1648614960"
PKG_NAME = f"{CONTENT_ID}_bg_1_0000000000000000000000000000000000000000.pkg"
APP = f"psvita/app/{TITLE_ID}"
LICENSE = f"psvita/license/{TITLE_ID}/{CONTENT_ID}.rif"
LAUNCHER = "psvita/GRAVITY RUSH.psvita"
RIF = rif()


def make_zrif(data: bytes, zdict: bytes = vp.ZRIF_DICT) -> str:
    """A zRIF for a (fake) licence: zlib with the zRIF dictionary, then base64."""
    c = zlib.compressobj(9, zdict=zdict)
    return base64.b64encode(c.compress(data) + c.flush()).decode("ascii")


ZRIF = make_zrif(RIF)


def vita_pkg(
    content_id: str = CONTENT_ID,
    content_type: int = 0x15,
    sfo: bytes | None = None,
    pkg_type: int = 2,
    ext: bool = True,
    total: int | None = None,
    magic: bytes = vp.PKG_MAGIC,
) -> bytes:
    """A synthetic Vita package: the header, the unencrypted metadata (content type and the
    param.sfo's place) and the param.sfo, then some opaque data."""
    sfo = vita_sfo() if sfo is None else sfo
    meta_at, sfo_at = 0x280, 0x400
    meta = struct.pack(">III", 2, 4, content_type)
    meta += struct.pack(">IIII", 14, 8, sfo_at, len(sfo))
    meta += struct.pack(">II", 99, 3) + b"xyz"
    data = bytearray(sfo_at + len(sfo) + 0x200)
    data[0:4] = magic
    struct.pack_into(">HH", data, 4, 0x8000, pkg_type)
    struct.pack_into(">II", data, 8, meta_at, 3)
    data[0x30 : 0x30 + len(content_id)] = content_id.encode()
    if ext:
        data[0xC0:0xC4] = vp.EXT_MAGIC
    data[meta_at : meta_at + len(meta)] = meta
    data[sfo_at : sfo_at + len(sfo)] = sfo
    data[-0x200:] = b"\xaa" * 0x200  # stands for the encrypted files
    struct.pack_into(">Q", data, 0x18, len(data) if total is None else total)
    return bytes(data)


def info(pkg: bytes) -> vp.PkgInfo | None:
    return vp.read_pkg(io.BytesIO(pkg))


class DictionaryTests(unittest.TestCase):
    def test_the_dictionary_is_pkg2zips(self) -> None:
        self.assertEqual(len(vp.ZRIF_DICT), 1024)
        self.assertEqual(zlib.adler32(vp.ZRIF_DICT), 0x627D1D5D)
        self.assertEqual(vp.ZRIF_DICT_ID, 0x627D1D5D)
        self.assertEqual(vp.ZRIF_DICT[:880], bytes(880))
        self.assertTrue(vp.ZRIF_DICT.endswith(b"\xef\xcd\xab\x89\x67\x45\x23\x01"))


class ZrifTests(unittest.TestCase):
    def test_a_zrif_decodes_to_its_licence(self) -> None:
        self.assertEqual(vp.decode_zrif(ZRIF), RIF)
        self.assertEqual(vp.license_for(ZRIF, CONTENT_ID), RIF)

    def test_what_is_not_a_zrif(self) -> None:
        plain = base64.b64encode(zlib.compress(RIF)).decode()
        other = make_zrif(RIF, zdict=b"another dictionary" * 10)
        stream = base64.b64decode(ZRIF)
        cut = base64.b64encode(stream[: len(stream) // 2]).decode()
        for bad, word in (
            ("not base64 at all!", "base64"),
            ("", "zRIF"),
            (plain, "preset dictionary"),
            (other, "another dictionary"),
            (make_zrif(rif(size=256)), "256 bytes"),
            (make_zrif(bytes(1024)), "1024 bytes"),
            (make_zrif(bytes(4096)), "more than"),
            (cut, "cut short"),
            ("A" * 3000, "too long"),
        ):
            with self.assertRaisesRegex(ValueError, word, msg=word):
                vp.decode_zrif(bad)

    def test_a_reason_never_quotes_the_zrif(self) -> None:
        for bad in (ZRIF[:-4] + "!!!!", make_zrif(rif(size=100))):
            with self.assertRaises(ValueError) as caught:
                vp.decode_zrif(bad)
            self.assertNotIn(bad, str(caught.exception))
            self.assertNotIn(bad[:12], str(caught.exception))

    def test_the_licence_must_be_the_packages(self) -> None:
        other = make_zrif(rif("EP9000-PCSB00360_00-OTHEROTHEROTHER0"))
        with self.assertRaisesRegex(ValueError, "licence of EP9000-PCSB00360"):
            vp.license_for(other, CONTENT_ID)
        with self.assertRaisesRegex(ValueError, "cannot be used"):
            vp.license_for(make_zrif(rif("not a content id")), CONTENT_ID)


class TableTests(unittest.TestCase):
    def test_plain_lines(self) -> None:
        text = f"# my keys\n\n{CONTENT_ID}\t{ZRIF}\r\nEP9000-PCSB00001_00-AAAAAAAAAAAAAAAA\tKO5x\n"
        table = vp.parse_zrif_table(text)
        self.assertEqual(table.keys[CONTENT_ID], ZRIF)
        self.assertEqual(len(table.keys), 2)

    def test_a_bad_line_is_named_by_its_number_only(self) -> None:
        secret = "KO5ifR1dQ+eHBlAAAAAAAAAAAAAAAAAA"  # gitleaks:allow
        with self.assertRaisesRegex(ValueError, "line 2 ") as caught:
            vp.parse_zrif_table(f"{CONTENT_ID}\t{ZRIF}\n{CONTENT_ID} {secret}\n")
        self.assertNotIn(secret, str(caught.exception))

    def test_a_nopaystation_tsv_reads_two_columns(self) -> None:
        head = "Title ID\tRegion\tName\tPKG direct link\tzRIF\tContent ID\tLast Modification Date"
        rows = [
            f"{TITLE_ID}\tEU\tGravity Rush\thttp://example.invalid/a.pkg\t{ZRIF}\t{CONTENT_ID}\t",
            "PCSB00002\tEU\tNo key\thttp://example.invalid/b.pkg\tMISSING\t"
            "EP9000-PCSB00002_00-AAAAAAAAAAAAAAAA\t",
            "PCSB00003\tEU\tFree\t\tNOT REQUIRED\tEP9000-PCSB00003_00-AAAAAAAAAAAAAAAA\t",
            "short row",
        ]
        text = "﻿" + "\r\n".join([head, *rows]) + "\r\n"
        table = vp.parse_zrif_table(text)
        self.assertEqual(dict(table.keys), {CONTENT_ID: ZRIF})

    def test_a_content_id_listed_twice(self) -> None:
        same = vp.parse_zrif_table(f"{CONTENT_ID}\t{ZRIF}\n{CONTENT_ID}\t{ZRIF}\n")
        self.assertEqual(dict(same.keys), {CONTENT_ID: ZRIF})
        other = make_zrif(rif(key=b"\x22" * 16))
        both = vp.parse_zrif_table(f"{CONTENT_ID}\t{ZRIF}\n{CONTENT_ID}\t{other}\n")
        self.assertEqual(dict(both.keys), {})
        self.assertEqual(both.doubled, {CONTENT_ID})

    def test_an_empty_file(self) -> None:
        self.assertEqual(dict(vp.parse_zrif_table("").keys), {})


class HeaderTests(unittest.TestCase):
    def test_a_vita_game_package(self) -> None:
        found = info(vita_pkg())
        assert found is not None
        self.assertEqual(found.content_id, CONTENT_ID)
        self.assertEqual(found.title_id, TITLE_ID)
        self.assertEqual(found.kind, "app")
        self.assertEqual(found.title, "GRAVITY RUSH™")
        self.assertEqual(found.sfo["CATEGORY"], "gd")
        self.assertIsNone(vp.pkg_problem("x.pkg", found, len(vita_pkg())))

    def test_what_is_not_a_vita_package(self) -> None:
        self.assertIsNone(info(vita_pkg(pkg_type=1)))  # PS3
        self.assertIsNone(info(vita_pkg(ext=False)))
        self.assertIsNone(info(vita_pkg(magic=b"\x7fCNT")))  # PS4
        self.assertIsNone(info(vita_pkg(content_type=0x07)))  # PSP
        self.assertIsNone(info(vita_pkg(content_type=0x06)))  # PS1
        self.assertIsNone(info(vita_pkg()[:0x80]))
        self.assertIsNone(info(b""))

    def test_the_content_type(self) -> None:
        for content_type, kind in ((0x15, "app"), (0x16, "dlc"), (0x1F, "theme"), (0x18, "psm")):
            found = info(vita_pkg(content_type=content_type))
            self.assertEqual(found.kind if found else None, kind)
        self.assertIsNone(vp.vita_kind(0x99))

    def test_metadata_cut_short(self) -> None:
        self.assertEqual(vp.parse_meta(struct.pack(">II", 2, 8) + b"abc", 5), {})
        self.assertEqual(vp.parse_meta(struct.pack(">II", 2, 1) + b"a", 5), {2: b"a"})

    def test_why_a_package_is_not_installed(self) -> None:
        def problem(pkg: bytes, size: int | None = None) -> str | None:
            found = info(pkg)
            assert found is not None
            return vp.pkg_problem("x.pkg", found, len(pkg) if size is None else size)

        cases = (
            (vita_pkg(), 10, "incomplete"),
            (vita_pkg(content_type=0x16), None, "DLC"),
            (vita_pkg(content_type=0x1F), None, "theme"),
            (vita_pkg(content_type=0x18), None, "PlayStation Mobile"),
            (vita_pkg(sfo=vita_sfo(category="gp")), None, "update"),
            (vita_pkg(sfo=vita_sfo(category="zz")), None, "not a game"),
            (vita_pkg(sfo=vita_sfo(title_id="PCSB00001")), None, "PCSB00001"),
            (vita_pkg(sfo=b"junk"), None, "param.sfo"),
            (vita_pkg(content_id="UP0001-NPUZ00001_00-AAAAAAAAAAAAAAAA"), None, "not a Vita"),
        )
        for pkg, size, word in cases:
            self.assertIn(word, problem(pkg, size) or "", word)
        other = vita_sfo().replace(b"PDEUAP1648614960", b"OTHEROTHEROTHER0")
        self.assertIn("content id", problem(vita_pkg(sfo=other)) or "")


class LaunchTests(unittest.TestCase):
    RUN = Path("/work/vita3k-1")

    def test_an_app_bundle_is_linked_into_the_work_folder(self) -> None:
        for tool in ("/Applications/Vita3K.app", "/Applications/Vita3K.app/"):
            plan = vp.launch_plan(tool, self.RUN)
            self.assertEqual(plan.program, "/work/vita3k-1/Vita3K.app/Contents/MacOS/Vita3K")
            self.assertEqual(plan.link, self.RUN / "Vita3K.app")
            self.assertEqual(plan.target, "/Applications/Vita3K.app")
            self.assertEqual(plan.portable, self.RUN / "portable")
            self.assertEqual(dict(plan.env), {})

    def test_the_binary_inside_a_bundle_is_the_same(self) -> None:
        plan = vp.launch_plan("/Apps/V.app/Contents/MacOS/Vita3K", self.RUN)
        self.assertEqual(plan.program, "/work/vita3k-1/Vita3K.app/Contents/MacOS/Vita3K")
        self.assertEqual(plan.target, "/Apps/V.app")

    def test_a_plain_binary_gets_appimage_in_the_work_folder(self) -> None:
        plan = vp.launch_plan("/usr/bin/vita3k", self.RUN)
        self.assertEqual(plan.program, "/usr/bin/vita3k")
        self.assertIsNone(plan.link)
        self.assertEqual(dict(plan.env), {"APPIMAGE": "/work/vita3k-1/Vita3K"})
        self.assertEqual(Path(plan.env["APPIMAGE"]).parent / "portable", plan.portable)

    def test_the_commands_and_what_is_shown(self) -> None:
        plan = vp.launch_plan("/Applications/Vita3K.app", self.RUN)
        self.assertEqual(vp.version_command(plan)[1:], ["--version"])
        argv = vp.install_command(plan, Path("/in/x.pkg"), ZRIF)
        self.assertEqual(argv[1:], ["--pkg", "/in/x.pkg", "--zrif", ZRIF])
        shown = vp.shown_command(argv, ZRIF)
        self.assertNotIn(ZRIF, shown)
        self.assertTrue(shown.endswith("--zrif ***"), shown)
        line = vp.failure_line(f"start\n[x] zrif {ZRIF} header signature is invalid\nbye\n", ZRIF)
        self.assertEqual(line, "[x] zrif *** header signature is invalid")
        self.assertEqual(vp.failure_line("", ZRIF), "no output")
        self.assertEqual(vp.failure_line("a\nlast\n", ZRIF), "last")


class JudgeTests(unittest.TestCase):
    INFO = vp.PkgInfo(CONTENT_ID, "app", 1, {})
    SFO: ClassVar[dict[str, str | int]] = {
        "TITLE_ID": TITLE_ID,
        "CATEGORY": "gd",
        "CONTENT_ID": CONTENT_ID,
    }

    def judge(self, **kw) -> str | None:  # type: ignore[no-untyped-def]
        found = {"folder": True, "eboot": True, "sfo": self.SFO, "licence": RIF, **kw}
        return vp.judge_installed("x.pkg", self.INFO, RIF, vp.Installed(**found))

    def test_a_good_install(self) -> None:
        self.assertIsNone(self.judge())

    def test_what_makes_an_install_bad(self) -> None:
        for kw, word in (
            ({"folder": False}, "did not install"),
            ({"eboot": False}, "eboot.bin"),
            ({"sfo": {}}, "param.sfo"),
            ({"sfo": {**self.SFO, "TITLE_ID": "PCSB00001"}}, "PCSB00001"),
            ({"sfo": {**self.SFO, "CATEGORY": "gp"}}, "not a game"),
            ({"sfo": {**self.SFO, "CONTENT_ID": "X"}}, "content id X"),
            ({"pfs": True}, "sce_pfs"),
            ({"licence": None}, "no licence"),
            ({"licence": rif(key=b"\x33" * 16)}, "not the one"),
        ):
            self.assertIn(word, self.judge(**kw) or "", word)


class ToolTests(unittest.TestCase):
    def test_vita3k_is_found_as_an_app_a_binary_or_on_the_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            app = Path(tmp) / "Vita3K.app"
            exe = make_stub(app / "Contents" / "MacOS", "Vita3K", "")
            none = lambda name: None  # noqa: E731
            find = inbox_core.find_tools
            self.assertEqual(find(InboxSettings(vita3k=str(app)), which=none).vita3k, str(app))
            self.assertEqual(find(InboxSettings(vita3k=exe), which=none).vita3k, exe)
            self.assertEqual(
                find(InboxSettings(), which=none, vita3k_default=str(app)).vita3k, str(app)
            )
            on_path = find(InboxSettings(), which=lambda n: f"/usr/bin/{n}", vita3k_default=None)
            self.assertEqual(on_path.vita3k, "/usr/bin/vita3k")
            gone = str(Path(tmp) / "Gone.app")
            self.assertIsNone(find(InboxSettings(), which=none, vita3k_default=gone).vita3k)
            for bad in (gone, tmp):
                with self.assertRaisesRegex(InboxError, "vita3k"):
                    find(InboxSettings(vita3k=bad), which=none)
        self.assertIn("vita3k no", inbox._describe_tools(Tools()))
        self.assertIn("vita3k yes", inbox._describe_tools(Tools(vita3k="/x")))
        self.assertEqual(
            inbox_core.vita3k_executable("/A/V.app/"), "/A/V.app/Contents/MacOS/Vita3K"
        )
        self.assertEqual(inbox_core.vita3k_executable("/usr/bin/vita3k"), "/usr/bin/vita3k")


class FakeVita3K:
    """Stands in for Vita3K: finds its portable folder the way the real one does, writes
    config.yml on --version and installs a decrypted game and its licence on --pkg."""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        self.portable = True  # --version writes portable/config.yml
        self.installs = True
        self.sfo = vita_sfo()
        self.licence: bytes | None = None  # what it writes; default: the decoded zRIF
        self.pfs = False
        self.code = 0
        self.output = "Vita3K v0.2.1 4269-9409e2ec"

    def __call__(
        self, argv: Sequence[str], cwd: Path, env: Mapping[str, str], timeout: float
    ) -> tuple[int, str]:
        self.calls.append(list(argv))
        program = Path(argv[0])
        if ".app" in argv[0]:
            folder = program.parents[3] / "portable"
            assert program.exists(), "the link to the bundle is made first"
        else:
            folder = Path(env["APPIMAGE"]).parent / "portable"
        if argv[1:] == ["--version"]:
            if self.portable and folder.is_dir():
                (folder / "config.yml").write_text("pref-path: ''\n")
            return 0, "Vita3K v0.2.1 4269-9409e2ec"
        assert argv[1] == "--pkg" and argv[3] == "--zrif", argv
        assert (folder / "config.yml").is_file(), "--pkg without config.yml crashes"
        if self.installs:
            app = folder / "fs" / "ux0" / "app" / TITLE_ID
            (app / "sce_sys").mkdir(parents=True)
            (app / "eboot.bin").write_bytes(ENCRYPTED)
            (app / "sce_sys" / "param.sfo").write_bytes(self.sfo)
            (app / "data.psarc").write_bytes(b"DATA" * 100)
            if self.pfs:
                (app / "sce_pfs").mkdir()
            lic = folder / "fs" / "ux0" / "license" / TITLE_ID / f"{CONTENT_ID}.rif"
            lic.parent.mkdir(parents=True)
            d = zlib.decompressobj(zdict=vp.ZRIF_DICT)
            data = d.decompress(base64.b64decode(argv[4])) + d.flush()
            lic.write_bytes(self.licence if self.licence is not None else data)
        return self.code, f"zrif {argv[4]}\n{self.output}"


class FlowBase(Base):
    def setUp(self) -> None:
        super().setUp()
        self.vita3k = FakeVita3K()
        patch = mock.patch.object(vp, "RUN", self.vita3k)
        patch.start()
        self.addCleanup(patch.stop)
        self.keys = self.tmp / "keys" / "zrif.txt"
        self.keys.parent.mkdir()
        self.keys.write_text(f"{CONTENT_ID}\t{ZRIF}\n")
        self.app_bundle = self.tmp / "Applications" / "Vita3K.app"
        make_stub(self.app_bundle / "Contents" / "MacOS", "Vita3K", "")

    def go(self, *, tool: bool = True, keys: bool = True, **kw) -> int:  # type: ignore[no-untyped-def]
        tools = Tools(
            chdman=self.chdman, rahasher=None, vita3k=str(self.app_bundle) if tool else None
        )
        settings = InboxSettings(vita_zrif=str(self.keys) if keys else None)
        return super().go(tools=tools, settings=settings, **kw)

    def assert_secret_kept(self) -> None:
        self.assertNotIn(ZRIF, self.out)
        for p in self.inbox.rglob("*.txt"):
            self.assertNotIn(ZRIF, p.read_text())

    def assert_review(self, item: str, *words: str) -> None:
        self.assertEqual(self.review(), [item, f"{item}.reason.txt"])
        for word in words:
            self.assertIn(word, self.reason(item))
        self.assertEqual(self.library(), [])
        self.assert_secret_kept()

    def installs(self) -> list[list[str]]:
        return [c for c in self.vita3k.calls if "--pkg" in c]


class InstallTests(FlowBase):
    def test_a_package_is_installed_and_filed_like_a_dump(self) -> None:
        self.drop(PKG_NAME, vita_pkg())
        self.assertEqual(self.go(), 0, self.out)
        self.assertEqual(
            self.library(),
            [
                LAUNCHER,
                f"{APP}/data.psarc",
                f"{APP}/eboot.bin",
                f"{APP}/sce_sys/param.sfo",
                LICENSE,
            ],
        )
        self.assertEqual((self.roms / LICENSE).read_bytes(), RIF)
        self.assertEqual((self.roms / APP / "eboot.bin").read_bytes(), ENCRYPTED)
        self.assertEqual((self.roms / LAUNCHER).read_bytes(), TITLE_ID.encode())
        self.assertEqual(self.done(), [PKG_NAME])
        version, install = self.vita3k.calls
        self.assertEqual(version[1:], ["--version"])
        self.assertTrue(version[0].endswith("/Vita3K.app/Contents/MacOS/Vita3K"), version[0])
        self.assertEqual(install[1], "--pkg")
        self.assertEqual(install[3:], ["--zrif", ZRIF])
        self.assertIn(f"detected: Vita package: {CONTENT_ID}", self.out)
        self.assertIn(f"installed with Vita3K: Vita3K --pkg {PKG_NAME} --zrif ***", self.out)
        self.assertIn(f"licence: library/roms/{LICENSE}", self.out)
        for line in (APP, f"psvita/license/{TITLE_ID}", "psvita/GRAVITY RUSH.psvita"):
            self.assertIn(f"whitelist: !/roms/{line}", self.out)
        self.assert_secret_kept()

    def test_nothing_is_left_in_the_work_folder_or_partial_in_the_library(self) -> None:
        self.drop(PKG_NAME, vita_pkg())
        self.assertEqual(self.go(), 0, self.out)
        self.assertEqual(list((self.tmp / "work").iterdir()), [])
        self.assertEqual([p.name for p in self.roms.rglob("*.part")], [])
        self.assertTrue((self.app_bundle / "Contents" / "MacOS" / "Vita3K").is_file())

    def test_report_only_runs_nothing_and_prints_every_line(self) -> None:
        self.drop(PKG_NAME, vita_pkg())
        self.assertEqual(self.go(apply=False), 0, self.out)
        self.assertEqual(self.vita3k.calls, [])
        self.assertEqual(self.library(), [])
        self.assertIn("would place", self.out)
        self.assertIn(f"would install it with Vita3K: Vita3K --pkg {PKG_NAME} --zrif ***", self.out)
        self.assertIn(f"licence: library/roms/{LICENSE} (would be written)", self.out)
        for line in (APP, f"psvita/license/{TITLE_ID}", "psvita/GRAVITY RUSH.psvita"):
            self.assertIn(f"whitelist: !/roms/{line}", self.out)
        self.assert_secret_kept()

    def test_a_package_inside_an_archive(self) -> None:
        self.zip_of("pack.zip", {PKG_NAME: vita_pkg()})
        self.assertEqual(self.go(), 0, self.out)
        self.assertIn(LICENSE, self.library())
        self.assertIn(f"{APP}/eboot.bin", self.library())

    def test_a_nopaystation_tsv_works_as_the_key_file(self) -> None:
        self.keys.write_text(
            "Title ID\tRegion\tName\tPKG direct link\tzRIF\tContent ID\r\n"
            f"{TITLE_ID}\tEU\tGravity Rush\thttp://example.invalid/x.pkg\t{ZRIF}\t{CONTENT_ID}\r\n"
        )
        self.drop(PKG_NAME, vita_pkg())
        self.assertEqual(self.go(), 0, self.out)
        self.assertIn(LICENSE, self.library())

    def test_a_game_in_the_library_runs_nothing_but_gets_its_licence(self) -> None:
        folder = self.roms / APP
        (folder / "sce_sys").mkdir(parents=True)
        (folder / "eboot.bin").write_bytes(b"MINE")
        (folder / "sce_sys" / "param.sfo").write_bytes(vita_sfo())
        self.drop(PKG_NAME, vita_pkg())
        self.assertEqual(self.go(tool=False), 0, self.out)
        self.assertEqual(self.vita3k.calls, [])
        self.assertEqual((folder / "eboot.bin").read_bytes(), b"MINE")
        self.assertEqual((self.roms / LICENSE).read_bytes(), RIF)
        self.assertEqual((self.roms / LAUNCHER).read_bytes(), TITLE_ID.encode())
        self.assertIn("already in the library", self.out)

    def test_a_rerun_changes_nothing(self) -> None:
        self.drop(PKG_NAME, vita_pkg())
        self.assertEqual(self.go(), 0, self.out)
        first = self.library()
        self.drop("again.pkg", vita_pkg())
        self.assertEqual(self.go(), 0, self.out)
        self.assertEqual(self.library(), first)
        self.assertEqual(len(self.installs()), 1)

    def test_a_plain_binary_runs_with_appimage_in_the_work_folder(self) -> None:
        binary = make_stub(self.tmp / "bin2", "vita3k", "")
        self.app_bundle = Path(binary)
        self.drop(PKG_NAME, vita_pkg())
        self.assertEqual(self.go(), 0, self.out)
        self.assertEqual(self.vita3k.calls[0][0], binary)
        self.assertIn(LICENSE, self.library())


class ReviewTests(FlowBase):
    def test_no_zrif_in_the_file(self) -> None:
        self.keys.write_text("EP9000-PCSB00001_00-AAAAAAAAAAAAAAAA\tKO5x\n")
        self.drop(PKG_NAME, vita_pkg())
        self.assertEqual(self.go(), 1, self.out)
        self.assert_review(PKG_NAME, f"no zRIF for {CONTENT_ID}", "zrif.txt", "vita_zrif")
        self.assertEqual(self.vita3k.calls, [])

    def test_no_key_file_configured(self) -> None:
        self.drop(PKG_NAME, vita_pkg())
        self.assertEqual(self.go(keys=False), 1, self.out)
        self.assert_review(PKG_NAME, "[inbox] vita_zrif", "never downloads")
        self.assertEqual(self.vita3k.calls, [])

    def test_a_key_file_that_cannot_be_read_line_by_line(self) -> None:
        self.keys.write_text(f"{CONTENT_ID}\t{ZRIF}\nnot a key line\n")
        self.drop(PKG_NAME, vita_pkg())
        self.assertEqual(self.go(), 1, self.out)
        self.assert_review(PKG_NAME, "line 2")

    def test_a_key_file_that_is_set_must_exist(self) -> None:
        self.keys.unlink()
        self.drop(PKG_NAME, vita_pkg())
        with self.assertRaisesRegex(InboxError, "vita_zrif"):
            self.go()

    def test_a_zrif_of_another_game(self) -> None:
        other = make_zrif(rif("EP9000-PCSB00360_00-OTHEROTHEROTHER0"))
        self.keys.write_text(f"{CONTENT_ID}\t{other}\n")
        self.drop(PKG_NAME, vita_pkg())
        self.assertEqual(self.go(), 1, self.out)
        self.assert_review(PKG_NAME, "the zRIF listed for", "EP9000-PCSB00360")
        self.assertNotIn(other, self.out + self.reason(PKG_NAME))
        self.assertEqual(self.vita3k.calls, [])

    def test_a_zrif_that_does_not_decode(self) -> None:
        self.keys.write_text(f"{CONTENT_ID}\tAAAAnotazrif\n")
        self.drop(PKG_NAME, vita_pkg())
        self.assertEqual(self.go(apply=False), 1, self.out)
        self.assertIn("zRIF", self.out)
        self.assertNotIn("AAAAnotazrif", self.out)

    def test_an_update_goes_to_review_without_running_anything(self) -> None:
        self.drop(PKG_NAME, vita_pkg(sfo=vita_sfo(category="gp")))
        self.assertEqual(self.go(), 1, self.out)
        self.assert_review(PKG_NAME, "update", TITLE_ID)
        self.assertEqual(self.vita3k.calls, [])

    def test_dlc_and_themes_go_to_review(self) -> None:
        self.drop("dlc.pkg", vita_pkg(content_type=0x16))
        self.drop("theme.pkg", vita_pkg(content_type=0x1F))
        self.assertEqual(self.go(), 2, self.out)  # two items for review
        self.assertIn("DLC", self.reason("dlc.pkg"))
        self.assertIn("theme", self.reason("theme.pkg"))
        self.assertEqual(self.vita3k.calls, [])

    def test_a_truncated_download(self) -> None:
        self.drop(PKG_NAME, vita_pkg()[:-16])
        self.assertEqual(self.go(), 1, self.out)
        self.assert_review(PKG_NAME, "incomplete")

    def test_without_vita3k(self) -> None:
        self.drop(PKG_NAME, vita_pkg())
        self.assertEqual(self.go(tool=False, apply=False), 1, self.out)
        self.assertIn("Vita3K is needed", self.out)
        self.assertIn("[inbox] vita3k", self.out)

    def test_vita3k_that_does_not_use_the_work_folder_installs_nothing(self) -> None:
        self.vita3k.portable = False
        self.drop(PKG_NAME, vita_pkg())
        self.assertEqual(self.go(), 1, self.out)
        self.assert_review(PKG_NAME, "did not use its work folder", "nothing was installed")
        self.assertEqual(self.installs(), [])
        self.assertEqual(list((self.tmp / "work").iterdir()), [])

    def test_vita3k_exits_0_but_installs_nothing(self) -> None:
        self.vita3k.installs = False
        self.vita3k.output = "header signature is invalid"
        self.drop(PKG_NAME, vita_pkg())
        self.assertEqual(self.go(), 1, self.out)
        self.assert_review(PKG_NAME, "did not install it", "header signature is invalid")
        self.assertEqual(list((self.tmp / "work").iterdir()), [])

    def test_vita3k_failing_with_an_exit_code(self) -> None:
        self.vita3k.code = 134
        self.drop(PKG_NAME, vita_pkg())
        self.assertEqual(self.go(), 1, self.out)
        self.assert_review(PKG_NAME, "exit code 134")

    def test_an_installed_game_that_does_not_match(self) -> None:
        for attr, value, word in (
            ("sfo", vita_sfo(title_id="PCSB00001"), "PCSB00001"),
            ("sfo", vita_sfo(category="gp"), "not a game"),
            ("licence", rif(key=b"\x44" * 16), "not the one the zRIF decodes to"),
            ("pfs", True, "sce_pfs"),
        ):
            with self.subTest(word=word):
                self.vita3k = FakeVita3K()
                setattr(self.vita3k, attr, value)
                with mock.patch.object(vp, "RUN", self.vita3k):
                    for p in self.inbox.glob("_revisar/*"):
                        p.unlink()
                    self.drop(PKG_NAME, vita_pkg())
                    self.assertEqual(self.go(), 1, self.out)
                    self.assert_review(PKG_NAME, word)

    def test_a_ps3_package_keeps_its_outcome(self) -> None:
        self.drop("ps3.pkg", vita_pkg(pkg_type=1))
        self.assertEqual(self.go(), 1, self.out)
        self.assertIn("not supported by inbox yet (PS3", self.reason("ps3.pkg"))
        self.assertNotIn("Vita", self.reason("ps3.pkg"))

    def test_a_psp_package_keeps_its_outcome(self) -> None:
        self.drop("psp.pkg", vita_pkg(content_type=0x07))
        self.assertEqual(self.go(), 1, self.out)
        self.assertIn("not supported by inbox yet", self.reason("psp.pkg"))


class LayoutTests(unittest.TestCase):
    def test_the_work_folder_link_never_takes_the_bundle_with_it(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            app = Path(tmp) / "Vita3K.app"
            make_stub(app / "Contents" / "MacOS", "Vita3K", "")
            run = Path(tmp) / "run"
            run.mkdir()
            plan = vp.launch_plan(str(app), run)
            assert plan.link is not None
            plan.link.symlink_to(plan.target)
            self.assertTrue(os.path.isfile(plan.program))
            shutil.rmtree(run)
            self.assertTrue((app / "Contents" / "MacOS" / "Vita3K").is_file())


if __name__ == "__main__":
    unittest.main()
