"""The units of the artwork, play state, prune and settings timers (pure)."""

import re
import unittest
from pathlib import Path

from warpzone import timers, updatetimer

# Paths that systemd would read differently from how they were written, if they were not escaped.
AWKWARD = [
    "/home/u/$HOME/config.toml",
    "/home/u/${X}/config.toml",
    "/home/u/100%/config.toml",
    "/home/u/%h/config.toml",
    "/home/u/a dir/config.toml",
    '/home/u/"quoted"/config.toml',
    "/home/u/back\\slash/config.toml",
    '/h/$a %b "c" \\d/config.toml',
]


def exec_line(service: str) -> str:
    (line,) = [x for x in service.splitlines() if x.startswith("ExecStart=")]
    return line


def unescaped_specifiers(service: str) -> list[str]:
    """The `%` and `$` of the `ExecStart=` line that are not doubled: systemd expands them."""
    return re.findall(r"[%$]", re.sub(r"%%|\$\$", "", exec_line(service)))


def services(python: str, config: Path | None) -> dict[str, str]:
    return {
        "artwork": timers.render_artwork_units(python, "deck", config, "6h")[0],
        "play-state": timers.render_play_state_units(python, "deck", config, "15min")[0],
        "prune": timers.render_prune_units(python, "deck", config)[0],
        "settings": timers.render_settings_units(python, "deck", config)[0],
    }


class EscapingTests(unittest.TestCase):
    def test_a_config_path_comes_back_as_the_same_words(self) -> None:
        for path in AWKWARD:
            for name, service in services("/opt/wz/bin/python", Path(path)).items():
                with self.subTest(timer=name, path=path):
                    words = timers.exec_command(service)
                    assert words is not None
                    self.assertEqual(words[-2:], ["--config", path])
                    self.assertEqual(unescaped_specifiers(service), [])

    def test_a_python_path_comes_back_as_the_same_word(self) -> None:
        for path in AWKWARD:
            for name, service in services(path, None).items():
                with self.subTest(timer=name, path=path):
                    words = timers.exec_command(service)
                    assert words is not None
                    self.assertEqual(words[0], path)
                    self.assertEqual(unescaped_specifiers(service), [])

    def test_a_dollar_is_doubled_so_systemd_does_not_expand_it(self) -> None:
        service = timers.render_prune_units("/p", "deck", Path("/h/$HOME/c.toml"))[0]
        self.assertIn('--config "/h/$$HOME/c.toml"\n', service)

    def test_the_updater_unit_quotes_with_the_same_function(self) -> None:
        for path in AWKWARD:
            with self.subTest(path=path):
                service = updatetimer.render_service(path)
                self.assertEqual(timers.exec_command(service), ["/bin/sh", path, "tick"])
                self.assertEqual(unescaped_specifiers(service), [])


class PriorityTests(unittest.TestCase):
    def test_the_services_run_at_low_priority_for_the_cpu_and_the_disk(self) -> None:
        for name, service in services("/p", None).items():
            with self.subTest(timer=name):
                self.assertIn("Type=oneshot\n", service)
                self.assertIn("Nice=10\n", service)
                self.assertIn("IOSchedulingClass=idle\n", service)

    def test_the_timers_do_not_set_a_priority(self) -> None:
        for timer in (
            timers.render_artwork_units("/p", "deck", None, "6h")[1],
            timers.render_play_state_units("/p", "deck", None, "15min")[1],
            timers.render_prune_units("/p", "deck", None)[1],
            timers.render_settings_units("/p", "deck", None)[1],
        ):
            self.assertNotIn("Nice", timer)
            self.assertNotIn("IOScheduling", timer)


class NamesTests(unittest.TestCase):
    def test_the_unit_names(self) -> None:
        self.assertEqual(timers.ARTWORK_UNIT, "warpzone-artwork")
        self.assertEqual(timers.PLAY_STATE_UNIT, "warpzone-play-state")
        self.assertEqual(timers.PRUNE_UNIT, "warpzone-prune")
        self.assertEqual(timers.SETTINGS_UNIT, "warpzone-settings")


if __name__ == "__main__":
    unittest.main()
