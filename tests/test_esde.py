import shlex
import unittest
from pathlib import Path

from warpzone import esde

SETTINGS = (
    '<?xml version="1.0"?>\n'
    '<bool name="BackgroundJoystickInput" value="false" />\n'
    '<bool name="CustomEventScripts" value="false" />\n'
    '<int name="MaxVRAM" value="256" />\n'
    '<string name="MediaDirectory" value="/home/user/ES-DE/downloaded_media" />\n'
)
WITHOUT = SETTINGS.replace('<bool name="CustomEventScripts" value="false" />\n', "")


def command_tokens(script: str) -> list[str]:
    """The tokens of the script's command, as `sh` would split them."""
    body = [line for line in script.splitlines() if not line.startswith("#")]
    return shlex.split(" ".join(line.rstrip("\\").strip() for line in body))


class ScriptTests(unittest.TestCase):
    def test_exact_text(self) -> None:
        text = esde.render_startup_script("/opt/wz/bin/python", "deck")
        self.assertEqual(
            text,
            "#!/bin/sh\n"
            "# Written by warpzone provision device, which rewrites it. Applies the emulator\n"
            "# settings from the hub. ES-DE waits for it, so it is bounded and never fails.\n"
            "env -u LD_LIBRARY_PATH -u LD_PRELOAD -u PYTHONHOME -u PYTHONPATH \\\n"
            "  timeout 15 '/opt/wz/bin/python' -m warpzone \\\n"
            "  settings apply --device 'deck' --quiet >/dev/null 2>&1\n"
            "exit 0\n",
        )

    def test_odd_values_stay_one_word_each(self) -> None:
        python = "/home/user name/it's/$(touch x)/python"
        device = "d e;`reboot`"
        tokens = command_tokens(esde.render_startup_script(python, device))
        self.assertIn(python, tokens)
        self.assertIn(device, tokens)
        self.assertEqual(tokens.count("-m"), 1)
        self.assertEqual(len(tokens), 23)

    def test_the_interpreter_can_be_read_back(self) -> None:
        for python in ("/opt/wz/bin/python", "/home/user name/it's/python"):
            text = esde.render_startup_script(python, "deck")
            self.assertEqual(esde.script_interpreter(text), python)

    def test_a_foreign_script_has_no_interpreter(self) -> None:
        self.assertIsNone(esde.script_interpreter("#!/bin/sh\necho hello\n"))
        self.assertIsNone(esde.script_interpreter("#!/bin/sh\necho 'unterminated\n"))
        self.assertIsNone(esde.script_interpreter(""))

    def test_paths(self) -> None:
        base = Path("/home/user/ES-DE")
        self.assertEqual(
            esde.script_path(base), base / "scripts" / "startup" / "warpzone-settings.sh"
        )
        self.assertEqual(esde.settings_path(base), base / "settings" / "es_settings.xml")


class ReadTests(unittest.TestCase):
    def test_values(self) -> None:
        self.assertIs(esde.event_scripts_enabled(SETTINGS), False)
        self.assertIs(
            esde.event_scripts_enabled(SETTINGS.replace('"false" />\n<int', '"true" />\n<int')),
            True,
        )
        self.assertIs(
            esde.event_scripts_enabled('<bool name="CustomEventScripts" value="true" />\n'),
            True,
        )
        self.assertIs(
            esde.event_scripts_enabled("<bool name='CustomEventScripts' value='true'/>"), True
        )

    def test_absent_or_unusable_is_none(self) -> None:
        self.assertIsNone(esde.event_scripts_enabled(WITHOUT))
        self.assertIsNone(esde.event_scripts_enabled(""))
        self.assertIsNone(esde.event_scripts_enabled('<?xml version="1.0"?>\n'))
        self.assertIsNone(esde.event_scripts_enabled("<bool name="))
        self.assertIsNone(esde.event_scripts_enabled(SETTINGS + "<unclosed>\n"))
        self.assertIsNone(
            esde.event_scripts_enabled('<bool name="CustomEventScripts" value="maybe" />\n')
        )
        # only a bool of that name counts
        self.assertIsNone(
            esde.event_scripts_enabled('<string name="CustomEventScripts" value="true" />\n')
        )

    def test_a_document_with_a_root_element_is_read_too(self) -> None:
        text = '<settings>\n  <bool name="CustomEventScripts" value="true" />\n</settings>\n'
        self.assertIs(esde.event_scripts_enabled(text), True)


class EnableTests(unittest.TestCase):
    def test_flips_only_the_value(self) -> None:
        new = esde.enable_event_scripts(SETTINGS)
        self.assertEqual(
            new,
            SETTINGS.replace(
                'name="CustomEventScripts" value="false"', 'name="CustomEventScripts" value="true"'
            ),
        )
        self.assertIs(esde.event_scripts_enabled(new), True)

    def test_already_true_is_returned_unchanged(self) -> None:
        on = esde.enable_event_scripts(SETTINGS)
        self.assertEqual(esde.enable_event_scripts(on), on)

    def test_missing_element_is_added_after_the_last_bool(self) -> None:
        new = esde.enable_event_scripts(WITHOUT)
        lines = new.splitlines()
        at = lines.index('<bool name="BackgroundJoystickInput" value="false" />')
        self.assertEqual(lines[at + 1], '<bool name="CustomEventScripts" value="true" />')
        self.assertEqual(
            new.replace('<bool name="CustomEventScripts" value="true" />\n', ""), WITHOUT
        )
        self.assertIs(esde.event_scripts_enabled(new), True)

    def test_the_new_line_takes_indentation_and_line_ending(self) -> None:
        indented = (
            '<settings>\r\n\t<bool name="A" value="1" />\r\n'
            '\t<int name="B" value="2" />\r\n</settings>\r\n'
        )
        new = esde.enable_event_scripts(indented)
        self.assertEqual(
            new,
            '<settings>\r\n\t<bool name="A" value="1" />\r\n'
            '\t<bool name="CustomEventScripts" value="true" />\r\n'
            '\t<int name="B" value="2" />\r\n</settings>\r\n',
        )

    def test_crlf_is_kept_when_flipping(self) -> None:
        crlf = SETTINGS.replace("\n", "\r\n")
        new = esde.enable_event_scripts(crlf)
        self.assertEqual(new, crlf.replace('value="false" />\r\n<int', 'value="true" />\r\n<int'))

    def test_a_missing_final_newline_stays_missing(self) -> None:
        text = WITHOUT.replace(
            '<string name="MediaDirectory" value="/home/user/ES-DE/downloaded_media" />\n', ""
        )
        text = text.replace(
            '<int name="MaxVRAM" value="256" />\n', '<bool name="Last" value="true" />'
        )
        new = esde.enable_event_scripts(text)
        self.assertEqual(new, text + '\n<bool name="CustomEventScripts" value="true" />')

    def test_no_bool_line_adds_it_after_the_last_line(self) -> None:
        text = '<?xml version="1.0"?>\n<int name="MaxVRAM" value="256" />\n'
        new = esde.enable_event_scripts(text)
        self.assertEqual(new, text + '<bool name="CustomEventScripts" value="true" />\n')

    def test_a_file_with_only_the_declaration_gets_the_line(self) -> None:
        new = esde.enable_event_scripts('<?xml version="1.0"?>\n')
        self.assertEqual(
            new, '<?xml version="1.0"?>\n<bool name="CustomEventScripts" value="true" />\n'
        )

    def test_every_other_byte_is_kept(self) -> None:
        odd = SETTINGS.replace(
            '<int name="MaxVRAM" value="256" />',
            '<!-- kept -->\n<string name="Unicode" value="café &amp; co" />\n\n'
            '  <int name="MaxVRAM" value="256"/>',
        )
        new = esde.enable_event_scripts(odd)
        self.assertEqual(new.replace('value="true"', 'value="false"', 1), odd)

    def test_a_bom_is_kept(self) -> None:
        text = "﻿" + SETTINGS
        new = esde.enable_event_scripts(text)
        self.assertTrue(new.startswith("﻿<?xml"))
        self.assertIs(esde.event_scripts_enabled(new), True)

    def test_an_unparsable_file_is_refused(self) -> None:
        for text in ("<bool name=", SETTINGS + "<unclosed>\n", "not xml at all <<"):
            with self.assertRaises(esde.EsdeError):
                esde.enable_event_scripts(text)

    def test_an_element_without_a_value_is_refused(self) -> None:
        with self.assertRaises(esde.EsdeError):
            esde.enable_event_scripts('<bool name="CustomEventScripts" />\n')

    def test_the_processes_are_the_documented_names(self) -> None:
        self.assertEqual(esde.ESDE_PROCESSES, ("es-de", "ES-DE.AppImage"))


if __name__ == "__main__":
    unittest.main()
