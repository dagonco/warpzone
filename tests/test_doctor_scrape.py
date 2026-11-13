"""The doctor's Skyscraper checks: the tool and its version, and the presence of the account in
Skyscraper's config (judged from the file's lines; the value is never kept or printed)."""

import dataclasses
import unittest
from pathlib import Path

from scrape_support import BANNER, FAKE_CONFIG, FAKE_CREDS
from test_doctor import TmpCase, by_name, context
from warpzone import doctor, doctor_scrape
from warpzone.doctor import FAIL, OK, OPTIONAL, WARN, RunOutput, check_skyscraper, render

TOOL = "/opt/sky/Skyscraper"


def version_run(banner: str = BANNER, code: int = 0):
    def run(command):
        if Path(command[0]).name != "Skyscraper":
            raise FileNotFoundError(2, "No such file or directory")
        return RunOutput(code, banner)

    return run


class VersionTests(unittest.TestCase):
    def test_the_tested_version_and_newer_are_ok(self) -> None:
        self.assertEqual(doctor_scrape.judge_version((3, 21, 1))[0], OK)
        self.assertEqual(doctor_scrape.judge_version((3, 22, 0))[0], OK)
        self.assertEqual(doctor_scrape.judge_version((4, 0, 0))[0], OK)

    def test_older_than_tested_is_a_warning_that_says_why(self) -> None:
        status, detail, fix = doctor_scrape.judge_version((3, 20, 9))
        self.assertEqual(status, WARN)
        self.assertIn("3.20.9 is older than 3.21.1", detail)
        self.assertIn("flags", detail)
        self.assertIn("github.com/Gemba/skyscraper/releases", fix)

    def test_no_version_is_a_warning(self) -> None:
        self.assertEqual(doctor_scrape.judge_version(None)[0], WARN)


class AccountTests(unittest.TestCase):
    def test_set_and_anonymous(self) -> None:
        status, detail, fix = doctor_scrape.judge_account(FAKE_CONFIG)
        self.assertEqual((status, detail, fix), (OK, "userCreds set in [screenscraper]", ""))
        status, detail, _ = doctor_scrape.judge_account("[main]\nuserCreds=x\n")
        self.assertEqual(status, OPTIONAL)
        self.assertIn("anonymous: 1 thread, 10,000 requests a day", detail)
        self.assertEqual(doctor_scrape.judge_account("")[0], OPTIONAL)

    def test_the_verdict_never_holds_the_value(self) -> None:
        for verdict in (doctor_scrape.judge_account(FAKE_CONFIG), doctor_scrape.judge_account("")):
            self.assertNotIn(FAKE_CREDS, " ".join(verdict))
            self.assertNotIn("fakepassword", " ".join(verdict))


class SkyscraperCheckTests(TmpCase):
    def check(self, config: str | None = None, **kw):
        kw.setdefault("run", version_run())
        ctx = context(self.tmp, config, **kw)
        return ctx, check_skyscraper(ctx)

    def write_ini(self, ctx, text: str) -> None:
        ini = ctx.probes.home / ".skyscraper" / "config.ini"
        ini.parent.mkdir(parents=True)
        ini.write_text(text)
        ctx.probes.read_text = lambda path: ini.read_text() if path == ini else ""
        ctx.probes.is_dir = lambda path: path == ini.parent

    def test_found_on_path_is_ok_with_the_version(self) -> None:
        _, results = self.check("[inbox]\n", tools={"Skyscraper": TOOL})
        sky = by_name(results, "Skyscraper")
        self.assertEqual((sky.status, sky.group), (OK, "Inbox tools"))
        self.assertEqual(sky.detail, f"3.21.1 ({TOOL})")

    def test_not_found_is_optional_and_says_inbox_will_not_scrape(self) -> None:
        _, results = self.check("[inbox]\n")
        self.assertEqual([r.name for r in results], ["Skyscraper"])
        self.assertEqual(results[0].status, OPTIONAL)
        self.assertIn("inbox will not scrape", results[0].detail)
        self.assertIn("releases", results[0].fix)

    def test_a_configured_path_that_is_missing_is_a_problem(self) -> None:
        _, results = self.check('[inbox]\nskyscraper = "/x/Skyscraper"\n')
        self.assertEqual([r.name for r in results], ["Skyscraper"])
        self.assertEqual(results[0].status, FAIL)
        self.assertIn("/x/Skyscraper is not an executable file", results[0].detail)

    def test_a_configured_path_is_used_over_path(self) -> None:
        _, results = self.check(
            '[inbox]\nskyscraper = "/x/Skyscraper"\n',
            tools={"Skyscraper": TOOL},
            executables=["/x/Skyscraper"],
        )
        self.assertEqual(by_name(results, "Skyscraper").detail, "3.21.1 (/x/Skyscraper)")

    def test_one_that_does_not_run_is_a_problem(self) -> None:
        def broken(command):
            raise OSError(8, "Exec format error")

        _, results = self.check("[inbox]\n", tools={"Skyscraper": TOOL}, run=broken)
        self.assertEqual(results[0].status, FAIL)
        self.assertIn("cannot be run (Exec format error)", results[0].detail)

    def test_an_old_version_is_a_warning(self) -> None:
        _, results = self.check(
            "[inbox]\n", tools={"Skyscraper": TOOL}, run=version_run("Skyscraper v3.19.0\n")
        )
        sky = by_name(results, "Skyscraper")
        self.assertEqual(sky.status, WARN)
        self.assertIn("3.19.0 is older than 3.21.1", sky.detail)

    def test_in_the_hub_container_it_says_to_scrape_elsewhere(self) -> None:
        _, results = self.check("[inbox]\n", in_container=lambda: True)
        self.assertEqual([r.name for r in results], ["Skyscraper"])
        self.assertEqual(results[0].status, OPTIONAL)
        self.assertEqual(results[0].fix, "run scrape on a computer that mounts the share")

    def test_the_account_line_appears_only_when_skyscraper_is_found(self) -> None:
        _, results = self.check("[inbox]\n")
        self.assertNotIn("ScreenScraper account", [r.name for r in results])

    def test_the_account_is_read_from_the_config_ini(self) -> None:
        ctx = context(self.tmp, "[inbox]\n", tools={"Skyscraper": TOOL}, run=version_run())
        self.write_ini(ctx, FAKE_CONFIG)
        account = by_name(check_skyscraper(ctx), "ScreenScraper account")
        self.assertEqual((account.status, account.detail), (OK, "userCreds set in [screenscraper]"))

    def test_without_credentials_it_is_anonymous(self) -> None:
        ctx = context(self.tmp, "[inbox]\n", tools={"Skyscraper": TOOL}, run=version_run())
        self.write_ini(ctx, '[main]\nlangPrios="en"\n;userCreds="user:pass"\n')
        account = by_name(check_skyscraper(ctx), "ScreenScraper account")
        self.assertEqual(account.status, OPTIONAL)
        self.assertIn("anonymous", account.detail)

    def test_no_config_ini_is_anonymous(self) -> None:
        _, results = self.check("[inbox]\n", tools={"Skyscraper": TOOL})
        self.assertEqual(by_name(results, "ScreenScraper account").status, OPTIONAL)

    def test_the_output_never_holds_the_credentials(self) -> None:
        ctx = context(self.tmp, "[inbox]\n", tools={"Skyscraper": TOOL}, run=version_run())
        self.write_ini(ctx, FAKE_CONFIG)
        text = "\n".join(render(check_skyscraper(ctx)))
        self.assertIn("userCreds set in [screenscraper]", text)
        self.assertNotIn(FAKE_CREDS, text)
        self.assertNotIn("fakepassword", text)
        results = check_skyscraper(ctx)
        self.assertNotIn(FAKE_CREDS, repr([dataclasses.asdict(r) for r in results]))

    def test_it_is_part_of_the_doctor_in_the_tool_group(self) -> None:
        self.assertIn(check_skyscraper, doctor.CHECKS)
        ctx = context(self.tmp, "[inbox]\n", tools={"Skyscraper": TOOL}, run=version_run())
        self.assertEqual({r.group for r in check_skyscraper(ctx)}, {"Inbox tools"})

    def test_without_an_inbox_table_the_optional_lines_collapse(self) -> None:
        ctx = context(self.tmp, "")
        collapsed = doctor.collapse_inbox(check_skyscraper(ctx))
        self.assertNotIn("Skyscraper", [r.name for r in collapsed])


if __name__ == "__main__":
    unittest.main()
