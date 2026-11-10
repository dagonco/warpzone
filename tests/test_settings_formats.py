import unittest

from warpzone import settings_formats as sf
from warpzone.settings_formats import FormatError, NotWritable

# Synthetic fixtures: the structure of each emulator's file, with fake values.

EDEN = r"""[Audio]
volume\default=true
volume=100
output_engine\default=false
output_engine=cubeb
[Core]
use_multi_core\default=false
use_multi_core=true
[Core%20Settings]
mem_layout\default=true
mem_layout=0
[UI]
Paths\gamedirs\size=1
Paths\gamedirs\1\path="C:\\Games\\\"quoted\""
Paths\recentFiles=
[Renderer]
resolution_setup\default=true
resolution_setup=2
use_asynchronous_shaders=true
[WebService]
eden_token=fake
"""

AZAHAR = r"""[Layout]
layout_option\default=false
layout_option=1
custom_top_x\default=true
custom_top_x=0
[Controls]
profile\default=true
profile=0
button_a\default=false
button_a="engine:keyboard,code:44"
motion_device\default=true
motion_device=@Variant(\0\0\0\x84\0\0\0\x1\0\0\0\x1\0\0\0\0)
"""

DUCKSTATION = """[Main]
EmulationSpeed = 1
Language = en
[Hotkeys]
FastForward = Keyboard/Tab
FastForward = Pointer-0/Button1
[Main]
PauseOnFocusLoss = true
[Display]
Scale = 3
Aspect = 4:3
[Compact]
Tight=1
Other=2
"""

PCSX2 = """[UI]
SettingsVersion = 1
[Folders]
Bios = bios
SaveStates = sstates
SaveStates = other
[EmuCore/GS]
VsyncEnable = 0
upscale_multiplier = 1
[Misc]
Tight=1
Loose   =   2
Empty =
"""

PPSSPP = (
    "\ufeff# comment\r\n"
    "[General]\r\n"
    "FirstRun = False\r\n"
    "Language = en_US\r\n"
    "\r\n"
    "; another comment\r\n"
    "[Graphics]\r\n"
    "InternalResolution = 1\r\n"
    "FrameSkip = 0\r\n"
    "[Empty]\r\n"
)

DOLPHIN = """[Interface]
ConfirmStop = True
[Core]
CPUCore = 1
Overclock = 1.0
; trailing comment
"""

RETROARCH = """# a comment
#include "extra.cfg"
savestate_auto_save = "true"
video_scale = "3.000000"
cheevos_enable = true
menu_driver = "ozone" # tail
video_scale_dup = "1"
video_scale_dup = "2"
audio_device = ""
"""

VITA3K = """---
pref-path: /fake/path/
backend-renderer: Vulkan
lle-modules:
  - libfake
  - libother
cpu-pool-size: 4
flow-list: []
module-list: [a, b]
# a comment
user-id: 00
...
"""

VITA3K_SEQ0 = """backend-renderer: Vulkan
modules:
- one
- two
title: "x"
"""

ESDE = (
    '<?xml version="1.0"?>\n'
    '<bool name="ScrapeVideos" value="true" />\n'
    '<bool name="ScrapeCovers" value="false" />\n'
    '<int name="ScraperConnectionTimeout" value="30" />\n'
    '<float name="FakeScale" value="1.5" />\n'
    '<string name="ScraperRegion" value="eu" />\n'
    '<string name="ScraperLanguage" value="en" />\n'
    '<string name="ROMDirectory" value="/fake/roms" />\n'
    '<bool name="CustomEventScripts" value="true" />\n'
    '<string name="Quoted" value="a &amp; &quot;b&quot; &lt;c&gt;" />\n'
    "<string name='Single' value='it&apos;s' />\n"
    '<bool name="Dup" value="true" />\n'
    '<bool name="Dup" value="false" />\n'
    '<bool name="Odd" value="maybe" />\n'
    '<int name="BadInt" value="1.5" />\n'
    '<string name="Empty" value="" />\n'
    '<!-- <bool name="InComment" value="true" /> -->\n'
)

FIXTURES = {
    "esde-xml": [ESDE],
    "qt-ini": [EDEN, AZAHAR],
    "ini": [DUCKSTATION, PCSX2, PPSSPP, DOLPHIN],
    "retroarch-cfg": [RETROARCH],
    "yaml-flat": [VITA3K, VITA3K_SEQ0],
}


def variants(text: str) -> list[str]:
    plain = text.replace("\r\n", "\n")
    out = [text, plain, plain.replace("\n", "\r\n"), "\ufeff" + plain.removeprefix("\ufeff")]
    out.append(plain.rstrip("\r\n"))
    out.append(plain.replace("\n", "\r\n").rstrip("\r\n"))
    return out


def changed(before: str, after: str) -> list[int]:
    a, b = before.split("\n"), after.split("\n")
    assert len(a) == len(b), (len(a), len(b))
    return [i for i, (x, y) in enumerate(zip(a, b, strict=True)) if x != y]


class RoundTripTests(unittest.TestCase):
    def test_empty_changes_return_the_text_byte_for_byte(self) -> None:
        for name, texts in FIXTURES.items():
            h = sf.handler(name)
            for text in texts:
                for variant in variants(text):
                    with self.subTest(handler=name, text=variant[:40]):
                        self.assertEqual(h.write(variant, {}), variant)

    def test_handler_flags(self) -> None:
        self.assertFalse(sf.handler("ini").typed)
        self.assertTrue(sf.handler("ini").can_add)
        self.assertTrue(sf.handler("qt-ini").can_add)
        self.assertTrue(sf.handler("retroarch-cfg").can_add)
        self.assertFalse(sf.handler("yaml-flat").can_add)
        self.assertFalse(sf.handler("yaml-flat").typed)
        self.assertEqual(sf.handler("qt-ini").name, "qt-ini")

    def test_unknown_handler(self) -> None:
        with self.assertRaises(ValueError):
            sf.handler("nope")

    def test_every_untouched_key_reads_back_after_a_change(self) -> None:
        cases = {
            "qt-ini": (EDEN, {"[Audio]volume": "50"}),
            "ini": (DUCKSTATION, {"[Main]Language": "es"}),
            "retroarch-cfg": (RETROARCH, {"savestate_auto_save": "false"}),
            "yaml-flat": (VITA3K, {"backend-renderer": "OpenGL"}),
            "esde-xml": (ESDE, {"ScrapeVideos": False, "ScraperRegion": "us"}),
        }
        for name, (text, changes) in cases.items():
            h = sf.handler(name)
            before = h.read(text)
            after = h.read(h.write(text, changes))
            expected = dict(before.values) | changes
            with self.subTest(handler=name):
                self.assertEqual(after.values, expected)
                self.assertEqual(after.fixed, before.fixed)


class EsdeXmlTests(unittest.TestCase):
    h = sf.handler("esde-xml")

    def test_flags(self) -> None:
        self.assertTrue(self.h.typed)
        self.assertFalse(self.h.can_add)
        self.assertEqual(self.h.name, "esde-xml")

    def test_read_is_typed_by_the_tag(self) -> None:
        r = self.h.read(ESDE)
        self.assertEqual(r.values["ScrapeVideos"], True)
        self.assertEqual(r.values["ScrapeCovers"], False)
        self.assertEqual(r.values["ScraperConnectionTimeout"], 30)
        self.assertEqual(r.values["FakeScale"], 1.5)
        self.assertEqual(r.values["ScraperRegion"], "eu")
        self.assertEqual(r.values["Empty"], "")
        self.assertEqual(r.values["Quoted"], 'a & "b" <c>')
        self.assertEqual(r.values["Single"], "it's")
        self.assertNotIn("InComment", r.values)

    def test_fixed_keys(self) -> None:
        r = self.h.read(ESDE)
        # Owned by warpzone, a path, repeated, or a value its tag does not allow.
        self.assertEqual(r.fixed, {"CustomEventScripts", "ROMDirectory", "Dup", "Odd", "BadInt"})
        self.assertFalse(r.fixed & set(r.values))

    def test_change_touches_only_its_value(self) -> None:
        out = self.h.write(ESDE, {"ScrapeVideos": False, "ScraperRegion": "us"})
        self.assertEqual(changed(ESDE, out), [1, 5])
        lines = out.split("\n")
        self.assertEqual(lines[1], '<bool name="ScrapeVideos" value="false" />')
        self.assertEqual(lines[5], '<string name="ScraperRegion" value="us" />')

    def test_an_unchanged_value_changes_no_byte(self) -> None:
        self.assertEqual(self.h.write(ESDE, {"ScrapeVideos": True, "FakeScale": 1.5}), ESDE)

    def test_int_float_and_string_values(self) -> None:
        out = self.h.write(ESDE, {"ScraperConnectionTimeout": 60, "FakeScale": 2.0, "Empty": "x"})
        r = self.h.read(out)
        self.assertEqual(r.values["ScraperConnectionTimeout"], 60)
        self.assertEqual(r.values["FakeScale"], 2.0)
        self.assertEqual(r.values["Empty"], "x")

    def test_special_characters_are_escaped_in_the_quote_style_of_the_element(self) -> None:
        out = self.h.write(ESDE, {"ScraperRegion": "a&b<c>\"d'e", "Single": "a&b<c>\"d'e"})
        r = self.h.read(out)
        self.assertEqual(r.values["ScraperRegion"], "a&b<c>\"d'e")
        self.assertEqual(r.values["Single"], "a&b<c>\"d'e")
        self.assertIn('value="a&amp;b&lt;c>&quot;d\'e"', out)
        self.assertIn("value='a&amp;b&lt;c>\"d&apos;e'", out)

    def test_a_type_mismatch_is_refused(self) -> None:
        for key, value in (
            ("ScrapeVideos", "false"),
            ("ScrapeVideos", 0),
            ("ScraperConnectionTimeout", True),
            ("ScraperConnectionTimeout", 1.5),
            ("FakeScale", 1),
            ("ScraperRegion", 3),
            ("ScraperRegion", True),
        ):
            with self.subTest(key=key, value=value), self.assertRaises(NotWritable) as cm:
                self.h.write(ESDE, {key: value})
            self.assertEqual(cm.exception.key, key)

    def test_values_that_cannot_be_stored_are_refused(self) -> None:
        for value in ("a\nb", "a\rb", "a\tb", "a\x00b"):
            with self.subTest(value=value), self.assertRaises(NotWritable):
                self.h.write(ESDE, {"ScraperRegion": value})
        with self.assertRaises(NotWritable):
            self.h.write(ESDE, {"FakeScale": float("nan")})

    def test_fixed_and_missing_keys_are_refused(self) -> None:
        for key, value in (
            ("CustomEventScripts", False),
            ("ROMDirectory", "/other"),
            ("Dup", False),
            ("Odd", True),
            ("BadInt", 2),
            ("InComment", False),
            ("NoSuchSetting", True),
        ):
            with self.subTest(key=key), self.assertRaises(NotWritable) as cm:
                self.h.write(ESDE, {key: value})
            self.assertEqual(cm.exception.key, key)

    def test_any_directory_path_or_dir_key_is_fixed(self) -> None:
        text = (
            '<string name="MediaDirectory" value="" />\n'
            '<string name="CacheDir" value="" />\n'
            '<string name="SomePath" value="" />\n'
            '<string name="DirectoryOfNothing" value="" />\n'
        )
        r = self.h.read(text)
        self.assertEqual(r.fixed, {"MediaDirectory", "CacheDir", "SomePath"})
        self.assertEqual(r.values, {"DirectoryOfNothing": ""})

    def test_crlf_bom_and_a_missing_final_newline_are_kept(self) -> None:
        text = "\ufeff" + ESDE.replace("\n", "\r\n").rstrip("\r\n")
        out = self.h.write(text, {"ScrapeVideos": False})
        self.assertEqual(
            out, text.replace('"ScrapeVideos" value="true"', '"ScrapeVideos" value="false"')
        )

    def test_a_file_with_a_root_element_works_too(self) -> None:
        text = '<?xml version="1.0"?>\n<settings>\n  <bool name="A" value="true"/>\n</settings>\n'
        out = self.h.write(text, {"A": False})
        self.assertEqual(out, text.replace("true", "false"))

    def test_multi_line_and_single_quoted_tags(self) -> None:
        text = "<bool\n  name='A'\n  value='true'\n/>\n"
        self.assertEqual(self.h.write(text, {"A": False}), text.replace("true", "false"))

    def test_an_attribute_value_that_looks_like_an_attribute(self) -> None:
        text = '<string name="A" note=\' value="x" \' value="y" />\n'
        self.assertEqual(self.h.read(text).values, {"A": "y"})
        self.assertEqual(self.h.write(text, {"A": "z"}), text.replace('"y"', '"z"'))

    def test_an_element_without_a_value_is_fixed(self) -> None:
        text = '<bool name="A" />\n'
        self.assertEqual(self.h.read(text).fixed, {"A"})

    def test_text_that_is_not_xml_is_a_format_error(self) -> None:
        for text in ('<bool name="A" value="true"', "<bool></int>", "not xml at all <"):
            with self.subTest(text=text), self.assertRaises(FormatError):
                self.h.read(text)
        with self.assertRaises(FormatError):
            self.h.write('<bool name="A" value="true"', {"A": False})

    def test_an_empty_file_has_no_keys(self) -> None:
        self.assertEqual(self.h.read("").values, {})
        self.assertEqual(self.h.read('<?xml version="1.0"?>\n').values, {})
        self.assertEqual(self.h.write("", {}), "")

    def test_other_settings_read_back_after_a_change(self) -> None:
        before = self.h.read(ESDE)
        after = self.h.read(self.h.write(ESDE, {"ScrapeCovers": True}))
        self.assertEqual(after.values, before.values | {"ScrapeCovers": True})
        self.assertEqual(after.fixed, before.fixed)


class IniTests(unittest.TestCase):
    h = sf.handler("ini")

    def test_read_duckstation(self) -> None:
        r = self.h.read(DUCKSTATION)
        self.assertEqual(r.values["[Main]EmulationSpeed"], "1")
        self.assertEqual(r.values["[Display]Aspect"], "4:3")
        # A section that appears twice is one section.
        self.assertEqual(r.values["[Main]PauseOnFocusLoss"], "true")
        self.assertEqual(r.fixed, {"[Hotkeys]FastForward"})
        self.assertNotIn("[Hotkeys]FastForward", r.values)

    def test_read_pcsx2_duplicate_key_and_gs_section(self) -> None:
        r = self.h.read(PCSX2)
        self.assertEqual(r.fixed, {"[Folders]SaveStates"})
        self.assertEqual(r.values["[EmuCore/GS]upscale_multiplier"], "1")
        self.assertEqual(r.values["[Folders]Bios"], "bios")
        self.assertEqual(r.values["[Misc]Loose"], "2")
        self.assertEqual(r.values["[Misc]Empty"], "")

    def test_read_is_case_sensitive(self) -> None:
        r = self.h.read("[A]\nKey = 1\nkey = 2\n")
        self.assertEqual(r.values, {"[A]Key": "1", "[A]key": "2"})
        self.assertEqual(r.fixed, frozenset())

    def test_comments_and_blank_lines_are_not_keys(self) -> None:
        r = self.h.read(PPSSPP)
        self.assertEqual(
            r.values,
            {
                "[General]FirstRun": "False",
                "[General]Language": "en_US",
                "[Graphics]InternalResolution": "1",
                "[Graphics]FrameSkip": "0",
            },
        )

    def test_dolphin_values_keep_inner_text(self) -> None:
        r = self.h.read(DOLPHIN)
        self.assertEqual(r.values["[Core]Overclock"], "1.0")

    def test_change_touches_only_its_line(self) -> None:
        out = self.h.write(DUCKSTATION, {"[Main]Language": "es"})
        self.assertEqual(changed(DUCKSTATION, out), [2])
        self.assertIn("\nLanguage = es\n", out)

    def test_change_keeps_the_spacing_around_equals(self) -> None:
        out = self.h.write(PCSX2, {"[Misc]Tight": "5", "[Misc]Loose": "9", "[Misc]Empty": "x"})
        self.assertEqual(changed(PCSX2, out), [10, 11, 12])
        lines = out.split("\n")
        self.assertEqual(lines[10], "Tight=5")
        self.assertEqual(lines[11], "Loose   =   9")
        self.assertEqual(lines[12], "Empty = x")

    def test_change_keeps_crlf_and_bom(self) -> None:
        out = self.h.write(PPSSPP, {"[Graphics]FrameSkip": "2"})
        self.assertEqual(out, PPSSPP.replace("FrameSkip = 0", "FrameSkip = 2"))

    def test_change_to_empty_value(self) -> None:
        out = self.h.write(DOLPHIN, {"[Core]CPUCore": ""})
        self.assertEqual(self.h.read(out).values["[Core]CPUCore"], "")

    def test_change_on_a_last_line_without_newline(self) -> None:
        text = "[A]\nKey = 1"
        self.assertEqual(self.h.write(text, {"[A]Key": "2"}), "[A]\nKey = 2")

    def test_add_to_existing_section_copies_the_section_style(self) -> None:
        out = self.h.write(DUCKSTATION, {"[Display]Fresh": "1", "[Compact]Fresh": "2"})
        lines = out.split("\n")
        self.assertEqual(lines[lines.index("Aspect = 4:3") + 1], "Fresh = 1")
        self.assertEqual(lines[lines.index("Other=2") + 1], "Fresh=2")
        self.assertEqual(len(lines), len(DUCKSTATION.split("\n")) + 2)

    def test_add_goes_to_the_last_occurrence_of_a_repeated_section(self) -> None:
        out = self.h.write(DUCKSTATION, {"[Main]Added": "v"})
        lines = out.split("\n")
        self.assertEqual(lines[lines.index("PauseOnFocusLoss = true") + 1], "Added = v")

    def test_add_keeps_order_of_several_keys(self) -> None:
        out = self.h.write(DOLPHIN, {"[Core]B": "1", "[Core]A": "2"})
        lines = out.split("\n")
        i = lines.index("Overclock = 1.0")
        self.assertEqual(lines[i + 1 : i + 3], ["B = 1", "A = 2"])
        self.assertEqual(lines[i + 3], "; trailing comment")

    def test_add_to_a_section_with_no_keys_goes_below_the_header(self) -> None:
        out = self.h.write(PPSSPP, {"[Empty]New": "1"})
        self.assertTrue(out.endswith("[Empty]\r\nNew = 1\r\n"))

    def test_add_new_section_at_the_end_with_the_file_line_ending(self) -> None:
        out = self.h.write(PPSSPP, {"[Fresh]A": "1", "[Fresh]B": "2"})
        self.assertTrue(out.startswith("\ufeff# comment\r\n"))
        self.assertTrue(out.endswith("[Empty]\r\n\r\n[Fresh]\r\nA = 1\r\nB = 2\r\n"))

    def test_add_new_section_after_a_blank_line_adds_no_second_blank(self) -> None:
        out = self.h.write("[A]\nx = 1\n\n", {"[B]y": "2"})
        self.assertEqual(out, "[A]\nx = 1\n\n[B]\ny = 2\n")

    def test_add_new_section_to_an_empty_file(self) -> None:
        self.assertEqual(self.h.write("", {"[B]y": "2"}), "[B]\ny = 2\n")

    def test_add_uses_lf_when_the_file_is_mostly_lf(self) -> None:
        text = "[A]\nx = 1\ny = 2\r\n"
        self.assertEqual(self.h.write(text, {"[A]z": "3"}), "[A]\nx = 1\ny = 2\r\nz = 3\n")

    def test_add_at_the_end_of_a_file_without_final_newline(self) -> None:
        out = self.h.write("[A]\nx = 1", {"[A]z": "3", "[B]w": "4"})
        self.assertEqual(out, "[A]\nx = 1\nz = 3\n\n[B]\nw = 4")

    def test_added_keys_read_back(self) -> None:
        out = self.h.write(PCSX2, {"[New]k": "v", "[UI]n": "1"})
        r = self.h.read(out)
        self.assertEqual(r.values["[New]k"], "v")
        self.assertEqual(r.values["[UI]n"], "1")

    def test_not_writable_for_a_duplicated_key(self) -> None:
        with self.assertRaises(NotWritable) as cm:
            self.h.write(PCSX2, {"[Folders]SaveStates": "x", "[UI]SettingsVersion": "2"})
        self.assertEqual(cm.exception.key, "[Folders]SaveStates")

    def test_not_writable_for_values_that_would_not_read_back(self) -> None:
        for bad in ("a\nb", " padded", "padded ", "x\r"):
            with self.subTest(value=bad), self.assertRaises(NotWritable):
                self.h.write(DOLPHIN, {"[Core]CPUCore": bad})

    def test_not_writable_for_a_non_string(self) -> None:
        with self.assertRaises(NotWritable):
            self.h.write(DOLPHIN, {"[Core]CPUCore": 1})  # type: ignore[dict-item]

    def test_not_writable_for_a_malformed_added_key(self) -> None:
        for bad in ("nosection", "[A]", "[A]k=v", "[A];k", "[A]k\nx"):
            with self.subTest(key=bad), self.assertRaises(NotWritable):
                self.h.write("[A]\nx = 1\n", {bad: "1"})

    def test_keys_before_any_section_are_fixed(self) -> None:
        text = "loose = 1\n[A]\nx = 1\n"
        r = self.h.read(text)
        self.assertEqual(r.fixed, {"[]loose"})
        with self.assertRaises(NotWritable):
            self.h.write(text, {"[]loose": "2"})
        with self.assertRaises(NotWritable):
            self.h.write(text, {"[]other": "2"})

    def test_failed_write_leaves_the_input_alone(self) -> None:
        text = PCSX2
        with self.assertRaises(NotWritable):
            self.h.write(text, {"[Folders]SaveStates": "x"})
        self.assertEqual(text, PCSX2)

    def test_equals_sign_in_the_value_is_kept(self) -> None:
        r = self.h.read("[A]\nurl = a=b\n")
        self.assertEqual(r.values["[A]url"], "a=b")


class QtIniTests(unittest.TestCase):
    h = sf.handler("qt-ini")

    def test_read_eden_keys_as_written(self) -> None:
        r = self.h.read(EDEN)
        self.assertEqual(r.values["[Audio]volume"], "100")
        self.assertEqual(r.values["[Core%20Settings]mem_layout"], "0")
        self.assertEqual(r.values["[UI]Paths\\gamedirs\\size"], "1")
        self.assertEqual(r.values["[UI]Paths\\gamedirs\\1\\path"], r'"C:\\Games\\\"quoted\""')
        self.assertEqual(r.values["[UI]Paths\\recentFiles"], "")
        self.assertEqual(r.fixed, frozenset())

    def test_companions_are_not_keys(self) -> None:
        r = self.h.read(EDEN)
        self.assertFalse([k for k in r.values if k.endswith("\\default")])

    def test_read_azahar_variants_raw(self) -> None:
        r = self.h.read(AZAHAR)
        self.assertEqual(
            r.values["[Controls]motion_device"], r"@Variant(\0\0\0\x84\0\0\0\x1\0\0\0\x1\0\0\0\0)"
        )
        self.assertEqual(r.values["[Controls]button_a"], '"engine:keyboard,code:44"')

    def test_change_flips_a_true_companion(self) -> None:
        out = self.h.write(EDEN, {"[Audio]volume": "50"})
        self.assertEqual(changed(EDEN, out), [1, 2])
        lines = out.split("\n")
        self.assertEqual(lines[1:3], ["volume\\default=false", "volume=50"])
        self.assertEqual(self.h.read(out).values["[Audio]volume"], "50")

    def test_change_leaves_a_false_companion_alone(self) -> None:
        out = self.h.write(EDEN, {"[Core]use_multi_core": "false"})
        self.assertEqual(changed(EDEN, out), [7])

    def test_change_adds_a_missing_companion_after_the_key(self) -> None:
        out = self.h.write(EDEN, {"[Renderer]use_asynchronous_shaders": "false"})
        lines = out.split("\n")
        i = lines.index("use_asynchronous_shaders=false")
        self.assertEqual(lines[i + 1], "use_asynchronous_shaders\\default=false")
        self.assertEqual(len(lines), len(EDEN.split("\n")) + 1)
        self.assertEqual(self.h.read(out).values["[Renderer]use_asynchronous_shaders"], "false")

    def test_no_companion_is_added_when_the_file_has_none(self) -> None:
        text = "[A]\nx=1\ny=2\n"
        self.assertEqual(self.h.write(text, {"[A]x": "5"}), "[A]\nx=5\ny=2\n")

    def test_change_to_a_variant_value(self) -> None:
        new = r"@Variant(\0\0\0\x84\0\0\0\x2)"
        out = self.h.write(AZAHAR, {"[Controls]motion_device": new})
        self.assertEqual(changed(AZAHAR, out), [10, 11])
        self.assertEqual(self.h.read(out).values["[Controls]motion_device"], new)

    def test_change_quoted_value_with_escapes_verbatim(self) -> None:
        new = r'"D:\\Other\\\"x\""'
        out = self.h.write(EDEN, {"[UI]Paths\\gamedirs\\1\\path": new})
        self.assertEqual(self.h.read(out).values["[UI]Paths\\gamedirs\\1\\path"], new)
        self.assertEqual(out.split("\n")[13], "Paths\\gamedirs\\1\\path=" + new)

    def test_add_to_existing_section_writes_key_then_companion(self) -> None:
        out = self.h.write(EDEN, {"[Audio]fresh": "7"})
        lines = out.split("\n")
        i = lines.index("output_engine=cubeb")
        self.assertEqual(lines[i + 1 : i + 3], ["fresh=7", "fresh\\default=false"])
        self.assertEqual(lines[i + 3], "[Core]")

    def test_add_new_section_with_companions(self) -> None:
        out = self.h.write(EDEN, {"[Brand%20New]x": "1"})
        self.assertTrue(
            out.endswith("eden_token=fake\n\n[Brand%20New]\nx=1\nx\\default=false\n"), out[-80:]
        )

    def test_add_without_companions_in_the_file(self) -> None:
        out = self.h.write("[A]\nx=1\n", {"[A]y": "2"})
        self.assertEqual(out, "[A]\nx=1\ny=2\n")

    def test_add_copies_the_crlf_ending(self) -> None:
        text = EDEN.replace("\n", "\r\n")
        out = self.h.write(text, {"[Audio]fresh": "7"})
        self.assertNotIn("\n", out.replace("\r\n", ""))
        self.assertIn("fresh=7\r\nfresh\\default=false\r\n", out)

    def test_a_key_may_not_be_a_companion(self) -> None:
        with self.assertRaises(NotWritable):
            self.h.write(EDEN, {"[Audio]volume\\default": "true"})

    def test_duplicated_key_is_fixed(self) -> None:
        text = "[A]\nx=1\nx=2\ny=3\n"
        self.assertEqual(self.h.read(text).fixed, {"[A]x"})
        with self.assertRaises(NotWritable) as cm:
            self.h.write(text, {"[A]x": "9"})
        self.assertEqual(cm.exception.key, "[A]x")

    def test_duplicated_companion_makes_the_key_fixed(self) -> None:
        text = "[A]\nx\\default=true\nx\\default=false\nx=1\n"
        r = self.h.read(text)
        self.assertEqual(r.fixed, {"[A]x"})
        self.assertNotIn("[A]x", r.values)


class RetroArchTests(unittest.TestCase):
    h = sf.handler("retroarch-cfg")

    def test_read_quoted_unquoted_and_empty(self) -> None:
        r = self.h.read(RETROARCH)
        self.assertEqual(r.values["savestate_auto_save"], "true")
        self.assertEqual(r.values["video_scale"], "3.000000")
        self.assertEqual(r.values["cheevos_enable"], "true")
        self.assertEqual(r.values["menu_driver"], "ozone")
        self.assertEqual(r.values["audio_device"], "")
        self.assertEqual(r.fixed, {"video_scale_dup"})

    def test_comments_and_includes_are_not_keys(self) -> None:
        r = self.h.read(RETROARCH)
        self.assertFalse([k for k in r.values if k.startswith("#")])
        self.assertNotIn("include", r.values)

    def test_change_a_quoted_value_keeps_the_trailing_text(self) -> None:
        out = self.h.write(RETROARCH, {"menu_driver": "xmb"})
        self.assertEqual(changed(RETROARCH, out), [5])
        self.assertIn('\nmenu_driver = "xmb" # tail\n', out)

    def test_change_an_unquoted_value_quotes_it(self) -> None:
        out = self.h.write(RETROARCH, {"cheevos_enable": "false"})
        self.assertEqual(changed(RETROARCH, out), [4])
        self.assertIn('\ncheevos_enable = "false"\n', out)
        self.assertEqual(self.h.read(out).values["cheevos_enable"], "false")

    def test_change_to_empty_and_from_empty(self) -> None:
        out = self.h.write(RETROARCH, {"video_scale": "", "audio_device": "hw:1"})
        r = self.h.read(out)
        self.assertEqual(r.values["video_scale"], "")
        self.assertEqual(r.values["audio_device"], "hw:1")

    def test_value_with_a_quote_is_rejected(self) -> None:
        with self.assertRaises(NotWritable) as cm:
            self.h.write(RETROARCH, {"video_scale": 'a"b'})
        self.assertEqual(cm.exception.key, "video_scale")
        with self.assertRaises(NotWritable):
            self.h.write(RETROARCH, {"brand_new": 'a"b'})

    def test_value_with_a_newline_is_rejected(self) -> None:
        with self.assertRaises(NotWritable):
            self.h.write(RETROARCH, {"video_scale": "a\nb"})

    def test_duplicated_key_is_not_writable(self) -> None:
        with self.assertRaises(NotWritable):
            self.h.write(RETROARCH, {"video_scale_dup": "3"})

    def test_add_goes_to_the_end_quoted(self) -> None:
        out = self.h.write(RETROARCH, {"fresh_key": "1"})
        self.assertEqual(out, RETROARCH + 'fresh_key = "1"\n')
        self.assertEqual(self.h.read(out).values["fresh_key"], "1")

    def test_add_copies_crlf_and_missing_final_newline(self) -> None:
        text = 'a = "1"\r\nb = "2"'
        self.assertEqual(
            self.h.write(text, {"c": "3", "d": "4"}), 'a = "1"\r\nb = "2"\r\nc = "3"\r\nd = "4"'
        )

    def test_add_to_an_empty_file(self) -> None:
        self.assertEqual(self.h.write("", {"c": "3"}), 'c = "3"\n')

    def test_add_with_a_malformed_key_is_rejected(self) -> None:
        for bad in ("", "a b", "a=b", "#c", 'a"b', "a\nb"):
            with self.subTest(key=bad), self.assertRaises(NotWritable):
                self.h.write(RETROARCH, {bad: "1"})

    def test_unterminated_quote_is_fixed(self) -> None:
        r = self.h.read('a = "oops\nb = "1"\n')
        self.assertEqual(r.fixed, {"a"})
        self.assertEqual(r.values, {"b": "1"})

    def test_core_opt_file(self) -> None:
        text = 'reicast_region = "Europe"\nreicast_cable_type = "TV (RGB)"\n'
        out = self.h.write(text, {"reicast_region": "Japan"})
        self.assertEqual(out, 'reicast_region = "Japan"\nreicast_cable_type = "TV (RGB)"\n')


class YamlFlatTests(unittest.TestCase):
    h = sf.handler("yaml-flat")

    def test_read_vita3k(self) -> None:
        r = self.h.read(VITA3K)
        self.assertEqual(
            r.values,
            {
                "pref-path": "/fake/path/",
                "backend-renderer": "Vulkan",
                "cpu-pool-size": "4",
                "user-id": "00",
            },
        )
        self.assertEqual(r.fixed, {"lle-modules", "flow-list", "module-list"})

    def test_sequence_at_the_key_indentation_belongs_to_the_key(self) -> None:
        r = self.h.read(VITA3K_SEQ0)
        self.assertEqual(r.values, {"backend-renderer": "Vulkan", "title": '"x"'})
        self.assertEqual(r.fixed, {"modules"})

    def test_markers_are_neither_keys_nor_continuations(self) -> None:
        r = self.h.read("---\na: 1\n...\n---\nb: 2\n...\n")
        self.assertEqual(r.values, {"a": "1", "b": "2"})
        self.assertEqual(r.fixed, frozenset())

    def test_change_touches_only_its_line(self) -> None:
        out = self.h.write(VITA3K, {"cpu-pool-size": "8", "pref-path": "/other/"})
        self.assertEqual(changed(VITA3K, out), [1, 6])
        self.assertIn("\ncpu-pool-size: 8\n", out)
        self.assertTrue(out.startswith("---\n"))
        self.assertTrue(out.endswith("...\n"))

    def test_change_a_key_with_an_empty_value(self) -> None:
        out = self.h.write("a:\nb: 1\n", {"a": "x"})
        self.assertEqual(out, "a: x\nb: 1\n")

    def test_change_keeps_crlf(self) -> None:
        text = VITA3K.replace("\n", "\r\n")
        out = self.h.write(text, {"cpu-pool-size": "8"})
        self.assertEqual(out, text.replace("cpu-pool-size: 4", "cpu-pool-size: 8"))

    def test_adding_a_key_is_not_writable(self) -> None:
        with self.assertRaises(NotWritable) as cm:
            self.h.write(VITA3K, {"fresh": "1"})
        self.assertEqual(cm.exception.key, "fresh")

    def test_fixed_keys_are_not_writable(self) -> None:
        for key in ("lle-modules", "flow-list", "module-list"):
            with self.subTest(key=key), self.assertRaises(NotWritable):
                self.h.write(VITA3K, {key: "x"})

    def test_values_that_would_not_read_back_are_rejected(self) -> None:
        for bad in ("a\nb", "[x]", "a #b", " pad"):
            with self.subTest(value=bad), self.assertRaises(NotWritable):
                self.h.write(VITA3K, {"cpu-pool-size": bad})

    def test_duplicated_key_is_fixed(self) -> None:
        r = self.h.read("a: 1\na: 2\nb: 3\n")
        self.assertEqual(r.fixed, {"a"})
        self.assertEqual(r.values, {"b": "3"})


class DecodeTests(unittest.TestCase):
    def test_utf8_with_bom_keeps_the_bom(self) -> None:
        self.assertEqual(sf.decode(b"\xef\xbb\xbf[A]\n"), "\ufeff[A]\n")

    def test_utf8_text(self) -> None:
        self.assertEqual(sf.decode("caf\u00e9".encode()), "caf\u00e9")

    def test_other_encodings_are_a_format_error(self) -> None:
        with self.assertRaises(FormatError):
            sf.decode("caf\u00e9".encode("latin-1"))
        with self.assertRaises(FormatError):
            sf.decode(b"\xff\xfeA\x00")

    def test_multibyte_text_round_trips_through_a_handler(self) -> None:
        text = "[A]\nname = caf\u00e9 \u65e5\u672c\n"
        h = sf.handler("ini")
        self.assertEqual(h.write(text, {}), text)
        self.assertEqual(h.read(text).values["[A]name"], "caf\u00e9 \u65e5\u672c")


class MatchTests(unittest.TestCase):
    def test_star_and_question_mark(self) -> None:
        self.assertTrue(sf.match("[WebService]*_token", "[WebService]eden_token"))
        self.assertTrue(sf.match("*", ""))
        self.assertTrue(sf.match("a?c", "abc"))
        self.assertFalse(sf.match("a?c", "ac"))
        self.assertFalse(sf.match("a?c", "abbc"))

    def test_full_match_and_case_sensitive(self) -> None:
        self.assertFalse(sf.match("abc", "abcd"))
        self.assertFalse(sf.match("bc", "abc"))
        self.assertFalse(sf.match("abc", "ABC"))

    def test_bracket_is_literal(self) -> None:
        self.assertTrue(sf.match("[Controls]*", "[Controls]button_a"))
        self.assertFalse(sf.match("[Controls]*", "Controls]button_a"))
        self.assertFalse(sf.match("[ab]", "a"))
        self.assertTrue(sf.match("[ab]", "[ab]"))

    def test_backslash_is_literal(self) -> None:
        self.assertTrue(sf.match("[UI]UILayout\\*", "[UI]UILayout\\geometry"))
        self.assertFalse(sf.match("[UI]UILayout\\*", "[UI]UILayoutgeometry"))
        self.assertTrue(sf.match("a\\d", "a\\d"))
        self.assertFalse(sf.match("a\\d", "ad"))

    def test_regex_metacharacters_are_literal(self) -> None:
        self.assertTrue(sf.match("a.b+c(d)|e^$", "a.b+c(d)|e^$"))
        self.assertFalse(sf.match("a.b", "axb"))
        self.assertTrue(sf.match("a{2}", "a{2}"))

    def test_newline_in_the_key(self) -> None:
        self.assertTrue(sf.match("a*", "a\nb"))


class SecretTests(unittest.TestCase):
    def test_builtin_secret_words(self) -> None:
        for key in (
            "cheevos_token",
            "[WebService]eden_token",
            "[Cheevos]Token",
            "netplay_password",
            "[A]PASSWD",
            "[A]client_secret",
            "[A]ApiKey",
            "[A]my_api_key",
            "web.service.token",
            "a.b.api_key",
        ):
            with self.subTest(key=key):
                self.assertTrue(sf.is_builtin_secret(key))

    def test_only_the_last_part_counts(self) -> None:
        for key in (
            "[Instance0.Firmware]Username",
            "Instance0.Firmware.Username",
            "[Token]name",
            "token.name",
            "[A]Language",
            "video_scale",
        ):
            with self.subTest(key=key):
                self.assertFalse(sf.is_builtin_secret(key))
        self.assertTrue(sf.is_builtin_secret("[WebService]token_display"))

    def test_words_constant(self) -> None:
        self.assertEqual(
            sf.BUILTIN_SECRET_WORDS, ("token", "password", "passwd", "secret", "apikey", "api_key")
        )


if __name__ == "__main__":
    unittest.main()
