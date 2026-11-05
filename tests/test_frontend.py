import unittest
from pathlib import Path
from typing import ClassVar

from warpzone import frontend

SOURCE = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\r\n'
    "<systemList>\r\n"
    "\t<system>\r\n"
    "\t\t<name>nes</name>\r\n"
    "\t\t<path>/roms/nes/</path>\r\n"
    "\t\t<command>run -L core.so %ROM% # /roms/nes is not a path element</command>\r\n"
    "\t</system>\r\n"
    "\t<system>\r\n"
    "\t\t<name>amiga</name>\r\n"
    "\t\t<path>/roms/amiga</path>\r\n"
    "\t</system>\r\n"
    "\t<system>\r\n"
    "\t\t<name>pico-8</name>\r\n"
    "\t\t<path>/roms/pico-8/carts/</path>\r\n"
    "\t</system>\r\n"
    "\t<system>\r\n"
    "\t\t<name>cmds</name>\r\n"
    "\t\t<path>/opt/cmds/</path>\r\n"
    "\t</system>\r\n"
    "</systemList>\r\n"
)
PREFIX = "/roms/"
TARGET = "/roms/library/roms/"


class RewriteTests(unittest.TestCase):
    def test_renames_a_system_folder_the_hub_calls_differently(self) -> None:
        src = "<path>/roms/pokemonmini/</path>\n<path>/roms/gb/</path>\n"
        renames = {"pokemonmini": "pokemini"}
        out = frontend.rewrite_system_paths(src, PREFIX, TARGET, renames)
        self.assertIn(f"<path>{TARGET}pokemini/</path>", out)
        self.assertIn(f"<path>{TARGET}gb/</path>", out)
        self.assertEqual(frontend.rewrite_system_paths(out, PREFIX, TARGET, renames), out)
        self.assertTrue(frontend.systems_current(src, out, PREFIX, TARGET, renames))

    def test_only_path_elements_below_the_prefix_change(self) -> None:
        out = frontend.rewrite_system_paths(SOURCE, PREFIX, TARGET)
        self.assertEqual(
            out,
            SOURCE.replace("<path>/roms/nes/", "<path>/roms/library/roms/nes/")
            .replace("<path>/roms/amiga<", "<path>/roms/library/roms/amiga<")
            .replace("<path>/roms/pico-8/carts/", "<path>/roms/library/roms/pico-8/carts/"),
        )

    def test_every_other_byte_is_kept(self) -> None:
        out = frontend.rewrite_system_paths(SOURCE, PREFIX, TARGET)
        self.assertIn("\r\n", out)
        self.assertIn("<path>/opt/cmds/</path>", out)
        self.assertIn("# /roms/nes is not a path element", out)

    def test_an_entry_already_below_the_target_is_not_rewritten_twice(self) -> None:
        once = frontend.rewrite_system_paths(SOURCE, PREFIX, TARGET)
        self.assertEqual(frontend.rewrite_system_paths(once, PREFIX, TARGET), once)

    def test_whitespace_inside_the_element_is_tolerated(self) -> None:
        out = frontend.rewrite_system_paths("<path>\n  /roms/gb/\n</path>", PREFIX, TARGET)
        self.assertEqual(out, "<path>\n  /roms/library/roms/gb/\n</path>")


class CurrentTests(unittest.TestCase):
    def test_current_means_the_rewrite_of_the_source(self) -> None:
        out = frontend.rewrite_system_paths(SOURCE, PREFIX, TARGET)
        self.assertTrue(frontend.systems_current(SOURCE, out, PREFIX, TARGET))
        self.assertFalse(frontend.systems_current(SOURCE, SOURCE, PREFIX, TARGET))
        self.assertFalse(frontend.systems_current(SOURCE, out + " ", PREFIX, TARGET))
        self.assertFalse(frontend.systems_current(SOURCE, None, PREFIX, TARGET))


NDS_LIST = (
    "<systemList>\r\n"
    "\t<system>\r\n\t\t<name>nds</name>\r\n"
    "\t\t<command>sudo perfmax %GOVERNOR% %ROM%; /usr/local/bin/drastic.sh %ROM%; "
    "sudo perfnorm</command>\r\n\t</system>\r\n"
    "\t<system>\r\n\t\t<name>gba</name>\r\n\t\t<command>run %ROM% &amp; wait</command>\r\n"
    "\t</system>\r\n"
    "</systemList>\r\n"
)
CONVERT = frontend.convert_command("/venv/bin/python", "ark")


class WrapTests(unittest.TestCase):
    def test_the_command_is_quiet_and_never_fails(self) -> None:
        self.assertEqual(CONVERT, "/venv/bin/python -m warpzone saves convert ark --quiet || true")
        self.assertEqual(
            frontend.convert_command("/a b/python", "ark", Path("/c d/config.toml")),
            "'/a b/python' -m warpzone saves convert ark --quiet --config '/c d/config.toml' "
            "|| true",
        )

    def test_the_named_system_runs_the_conversion_before_and_after(self) -> None:
        out = frontend.wrap_systems(NDS_LIST, {"nds"}, CONVERT)
        self.assertIn(
            f"<command>{CONVERT}; sudo perfmax %GOVERNOR% %ROM%; /usr/local/bin/drastic.sh "
            f"%ROM%; sudo perfnorm; {CONVERT}</command>",
            out,
        )

    def test_every_other_byte_is_kept(self) -> None:
        out = frontend.wrap_systems(NDS_LIST, {"nds"}, CONVERT)
        self.assertEqual(out.replace(f"{CONVERT}; ", "").replace(f"; {CONVERT}", ""), NDS_LIST)
        self.assertIn("run %ROM% &amp; wait", out)

    def test_wrapping_twice_changes_nothing(self) -> None:
        once = frontend.wrap_systems(NDS_LIST, {"nds"}, CONVERT)
        self.assertEqual(frontend.wrap_systems(once, {"nds"}, CONVERT), once)
        other = frontend.convert_command("/other/python", "ark")
        again = frontend.wrap_systems(once, {"nds"}, other)
        self.assertEqual(again.count("saves convert"), 2)
        self.assertEqual(frontend.unwrap_systems(again, {"nds"}), NDS_LIST)

    def test_unwrap_gives_the_original_back(self) -> None:
        once = frontend.wrap_systems(NDS_LIST, {"nds"}, CONVERT)
        self.assertEqual(frontend.unwrap_systems(once, {"nds"}), NDS_LIST)
        self.assertEqual(frontend.unwrap_systems(once, set()), once)
        self.assertEqual(frontend.unwrap_systems(once, {"gba"}), once)

    def test_a_command_with_surrounding_whitespace_keeps_it(self) -> None:
        text = "<system><name>nds</name><command>\n  run %ROM%\n</command></system>"
        out = frontend.wrap_systems(text, {"nds"}, CONVERT)
        self.assertEqual(frontend.unwrap_systems(out, {"nds"}), text)
        self.assertTrue(out.startswith(f"<system><name>nds</name><command>\n  {CONVERT}; run"))

    def test_the_command_is_escaped_for_xml(self) -> None:
        command = frontend.convert_command("/a&b/python", "ark")
        out = frontend.wrap_systems(NDS_LIST, {"nds"}, command)
        self.assertIn("/a&amp;b/python", out)
        self.assertEqual(frontend.unwrap_systems(out, {"nds"}), NDS_LIST)
        self.assertEqual(frontend.wrapped_systems(out, {"nds"}), {"nds"})

    def test_wrapped_systems_names_those_that_have_it(self) -> None:
        out = frontend.wrap_systems(NDS_LIST, {"nds"}, CONVERT)
        self.assertEqual(frontend.wrapped_systems(out, {"nds", "gba", "missing"}), {"nds"})
        self.assertEqual(frontend.wrapped_systems(NDS_LIST, {"nds"}), set())

    def test_a_wrapped_copy_is_current_when_the_system_is_unwrapped_for_the_comparison(
        self,
    ) -> None:
        source = NDS_LIST.replace("<command>", "<path>/roms/nds/</path><command>", 1)
        plain = frontend.rewrite_system_paths(source, PREFIX, TARGET)
        wrapped = frontend.wrap_systems(plain, {"nds"}, CONVERT)
        self.assertFalse(frontend.systems_current(source, wrapped, PREFIX, TARGET))
        self.assertTrue(frontend.systems_current(source, wrapped, PREFIX, TARGET, None, {"nds"}))
        self.assertTrue(frontend.systems_current(source, plain, PREFIX, TARGET, None, {"nds"}))
        self.assertFalse(
            frontend.systems_current(source, wrapped + " ", PREFIX, TARGET, None, {"nds"})
        )


class RetroArchGapTests(unittest.TestCase):
    CFG = (
        'system_directory = "/roms/bios"\n'
        'savefiles_in_content_dir = "true"\n'
        'savestates_in_content_dir = "false"\n'
        'sort_savefiles_enable = "false"\n'
    )
    WANT: ClassVar[dict[str, str]] = {
        "system_directory": "/roms/library/bios",
        "savefiles_in_content_dir": "false",
        "savestates_in_content_dir": "false",
    }

    def test_lists_the_keys_that_differ(self) -> None:
        gap = frontend.retroarch_gap(self.CFG, self.WANT)
        self.assertEqual(
            gap.differs,
            {"system_directory": "/roms/bios", "savefiles_in_content_dir": "true"},
        )
        self.assertEqual(gap.unwritable, frozenset())

    def test_a_missing_key_differs_with_no_current_value(self) -> None:
        gap = frontend.retroarch_gap("", {"system_directory": "/x"})
        self.assertEqual(gap.differs, {"system_directory": None})

    def test_a_key_present_twice_cannot_be_written(self) -> None:
        text = self.CFG + 'system_directory = "/other"\n'
        gap = frontend.retroarch_gap(text, self.WANT)
        self.assertEqual(gap.unwritable, frozenset({"system_directory"}))
        self.assertNotIn("system_directory", gap.differs)

    def test_apply_changes_only_those_values(self) -> None:
        out = frontend.retroarch_apply(self.CFG, self.WANT)
        self.assertEqual(
            out,
            'system_directory = "/roms/library/bios"\n'
            'savefiles_in_content_dir = "false"\n'
            'savestates_in_content_dir = "false"\n'
            'sort_savefiles_enable = "false"\n',
        )
        self.assertEqual(frontend.retroarch_gap(out, self.WANT).differs, {})


if __name__ == "__main__":
    unittest.main()
