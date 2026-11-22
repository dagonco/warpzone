"""`warpzone scrape --refresh-text`, the pure part: which fields of an entry a refresh changes, the
text edit of the gamelist and the report lines. No filesystem, no Skyscraper."""

import unittest
from typing import Any, ClassVar

from library_support import game
from test_scrape import ROOT, SKY_FIELDS, entry, staged_of
from warpzone import artwork, scrape

GAMELIST = (
    '<?xml version="1.0"?>\r\n<gameList>\r\n'
    "  <game>\r\n    <path>./A.zip</path>\r\n    <name>Sample Quest</name>\r\n"
    "    <desc>An English text</desc>\r\n    <genre>Role-playing</genre>\r\n"
    "    <rating>0.5</rating>\r\n    <playcount>7</playcount>\r\n    <favorite>true</favorite>\r\n"
    "    <image>./covers/A.png</image>\r\n  </game>\r\n"
    "  <game>\r\n    <path>./B.zip</path>\r\n    <name>B</name>\r\n    <playcount>2</playcount>\r\n"
    "  </game>\r\n"
    "  <!-- a note -->\r\n"
    "  <game>\r\n    <path>./C.zip</path>\r\n    <name>Mine</name>\r\n"
    "    <nomultiscrape>true</nomultiscrape>\r\n  </game>\r\n"
    "</gameList>\r\n"
)
WANTED = {"name": "Quest de Muestra", "desc": "Un texto en espanol", "genre": "Rol"}


def refresh(text, *wanted):
    new, applied, _ = scrape.refresh_entries(text, artwork.parse_gamelist(text), list(wanted))
    return new, applied


def left_out(text, *wanted):
    return scrape.refresh_entries(text, artwork.parse_gamelist(text), list(wanted))[2]


def one_game(body, path="./A.zip"):
    return (
        f'<?xml version="1.0"?>\n<gameList>\n<game>\n  <path>{path}</path>\n{body}</game>\n'
        "</gameList>\n"
    )


class FieldsTest(unittest.TestCase):
    def test_the_three_fields_are_replaced(self):
        have = entry("./A.zip", name="Sample Quest", desc="An English text", genre="Role-playing")
        changes = scrape.refresh_fields(have, WANTED)
        self.assertEqual(
            changes,
            (
                scrape.FieldChange("name", "Sample Quest", "Quest de Muestra"),
                scrape.FieldChange("desc", "An English text", "Un texto en espanol"),
                scrape.FieldChange("genre", "Role-playing", "Rol"),
            ),
        )

    def test_only_the_fields_that_differ_are_listed(self):
        have = entry("./A.zip", name="Quest de Muestra", desc="old", genre="Rol")
        (change,) = scrape.refresh_fields(have, WANTED)
        self.assertEqual(change.key, "desc")

    def test_an_empty_value_keeps_the_field(self):
        have = entry("./A.zip", name="Sample Quest", desc="An English text")
        self.assertEqual(scrape.refresh_fields(have, {"name": "", "desc": "  ", "genre": ""}), ())

    def test_a_missing_field_is_set(self):
        (change,) = scrape.refresh_fields(entry("./A.zip", name="X"), {"name": "X", "genre": "Rol"})
        self.assertEqual(change, scrape.FieldChange("genre", "", "Rol"))

    def test_no_other_field_is_ever_taken(self):
        have = entry("./A.zip", name="A")
        changes = scrape.refresh_fields(have, {**SKY_FIELDS, "playcount": "9", "favorite": "true"})
        self.assertEqual({c.key for c in changes}, {"name", "desc", "genre"})

    def test_a_value_is_cleaned_and_trimmed_before_it_is_compared(self):
        have = entry("./A.zip", name="Sample Quest")
        self.assertEqual(scrape.refresh_fields(have, {"name": " Sample\x01 Quest\n"}), ())

    def test_nomultiscrape_changes_nothing(self):
        have = entry("./A.zip", name="A", nomultiscrape="true")
        self.assertEqual(scrape.refresh_fields(have, WANTED), ())


class NameTest(unittest.TestCase):
    def test_a_name_that_is_only_the_file_name_or_its_stem_never_replaces_the_name(self):
        have = entry("./Sample Quest (USA).zip", name="Sample Quest", desc="old")
        for bad in ("Sample Quest (USA).zip", "Sample Quest (USA)"):
            with self.subTest(bad=bad):
                changes = scrape.refresh_fields(have, {"name": bad, "desc": "new"})
                self.assertEqual([c.key for c in changes], ["desc"])

    def test_a_name_that_differs_only_in_case_spaces_or_punctuation_is_kept(self):
        cases = (
            ("Sample Quest : Dark Version", "Sample Quest - Dark Version"),
            ("Sample Quest of the GB", "Sample Quest Of The Gb"),
            ("SampleQuest II", "Sample Quest II"),
        )
        for old, new in cases:
            with self.subTest(old=old):
                have = entry("./Sample Quest (USA).zip", name=old, desc="old")
                changes = scrape.refresh_fields(have, {"name": new, "desc": "new"})
                self.assertEqual([c.key for c in changes], ["desc"])

    def test_a_name_that_gains_an_accent_or_another_word_replaces_it(self):
        for old, new in (("Sample Camara", "Sample Cámara"), ("Sample Quest 2", "Sample Quest II")):
            with self.subTest(old=old):
                have = entry("./Sample Quest (USA).zip", name=old)
                (change,) = scrape.refresh_fields(have, {"name": new})
                self.assertEqual(change.new, new)

    def test_a_real_title_replaces_it(self):
        have = entry("./Sample Quest (USA).zip", name="Sample Quest (USA)")
        (change,) = scrape.refresh_fields(have, {"name": "Quest de Muestra"})
        self.assertEqual(change.new, "Quest de Muestra")


class CandidatesTest(unittest.TestCase):
    def test_a_folder_entry_is_not_a_candidate_and_never_changes(self):
        g = game("snes", "roms/snes/Dir.m3u", "Dir")
        folder = entry("./Dir.m3u", "folder", name="Dir")
        self.assertEqual(scrape.refresh_candidates([g], [folder]), [])
        self.assertEqual(scrape.refresh_changes([folder], [entry("./Dir.m3u", name="New")]), ())

    def test_a_game_after_a_folder_with_the_same_path_is_the_one_edited(self):
        text = (
            '<?xml version="1.0"?>\n<gameList>\n<folder>\n  <path>./A.zip</path>\n'
            "  <name>Folder</name>\n</folder>\n<game>\n  <path>./A.zip</path>\n"
            "  <name>Game</name>\n</game>\n</gameList>\n"
        )
        new, changes = refresh(text, entry("./A.zip", name="Title"))
        self.assertEqual(len(changes), 1)
        self.assertEqual(new, text.replace("<name>Game</name>", "<name>Title</name>"))

    def test_only_games_with_an_entry_that_is_not_marked(self):
        a, b, c = (game("snes", f"roms/snes/{n}.zip", n) for n in "ABC")
        entries = [entry("./A.zip"), entry("./C.zip", nomultiscrape="true")]
        self.assertEqual(scrape.refresh_candidates([a, b, c], entries), [a])


class PlanTest(unittest.TestCase):
    def setUp(self):
        self.a = game("snes", "roms/snes/A.zip", "A")
        self.b = game("snes", "roms/snes/B.zip", "B")

    def test_one_entry_per_game_skyscraper_has_with_the_three_fields_only(self):
        sky = [entry(f"{ROOT}/A.zip", **SKY_FIELDS, playcount="9")]
        wanted, dropped = scrape.plan_refresh(staged_of(self.a, self.b), ROOT, sky)
        self.assertEqual(dropped, [])
        (item,) = wanted
        self.assertEqual(item.path, "./A.zip")
        self.assertEqual(
            item.fields,
            {"name": "Kirby Super Star", "desc": SKY_FIELDS["desc"], "genre": "Platform"},
        )

    def test_a_path_outside_the_staged_folder_or_for_another_game_is_dropped(self):
        sky = [entry("/elsewhere/A.zip", **SKY_FIELDS), entry("./Z.zip", **SKY_FIELDS)]
        wanted, dropped = scrape.plan_refresh(staged_of(self.a), ROOT, sky)
        self.assertEqual((wanted, len(dropped)), ([], 2))

    def test_empty_fields_are_left_out(self):
        sky = [entry("./A.zip", name="A", desc="", genre="  ")]
        (item,), _ = scrape.plan_refresh(staged_of(self.a), ROOT, sky)
        self.assertEqual(item.fields, {"name": "A"})


class ChangesTest(unittest.TestCase):
    def test_an_entry_that_is_not_in_the_gamelist_is_not_added(self):
        self.assertEqual(
            scrape.refresh_changes([entry("./A.zip")], [entry("./Z.zip", **WANTED)]), ()
        )

    def test_the_changes_follow_the_wanted_order_and_name_the_old_values(self):
        have = [entry("./A.zip", name="Old A"), entry("./B.zip", name="Old B")]
        changes = scrape.refresh_changes(
            have, [entry("./B.zip", name="New B"), entry("./A.zip", name="New A")]
        )
        self.assertEqual([c.path for c in changes], ["B.zip", "A.zip"])
        self.assertEqual(changes[0].rename, scrape.FieldChange("name", "Old B", "New B"))


class EditTest(unittest.TestCase):
    def test_the_three_fields_are_replaced_and_every_other_byte_stays(self):
        new, changes = refresh(GAMELIST, entry("./A.zip", **WANTED))
        self.assertEqual([c.path for c in changes], ["A.zip"])
        self.assertEqual(
            new,
            GAMELIST.replace("Sample Quest", "Quest de Muestra")
            .replace("An English text", "Un texto en espanol")
            .replace("Role-playing", "Rol"),
        )
        entries = {e.path: e.fields for e in artwork.parse_gamelist(new)}
        kept = entries["./A.zip"]
        for key, value in (("playcount", "7"), ("favorite", "true"), ("rating", "0.5")):
            self.assertEqual(kept[key], value)
        self.assertEqual(kept["image"], "./covers/A.png")

    def test_a_missing_field_is_added_in_the_files_style(self):
        new, _ = refresh(GAMELIST, entry("./B.zip", genre="Rol"))
        self.assertIn(
            "    <playcount>2</playcount>\r\n    <genre>Rol</genre>\r\n  </game>\r\n", new
        )

    def test_the_text_is_escaped(self):
        new, _ = refresh(GAMELIST, entry("./A.zip", name="Q & A <1>"))
        self.assertIn("<name>Q &amp; A &lt;1&gt;</name>", new)
        self.assertEqual(
            {e.path: e.fields["name"] for e in artwork.parse_gamelist(new)}["./A.zip"],
            "Q & A <1>",
        )

    def test_a_second_pass_changes_nothing(self):
        wanted = [entry("./A.zip", **WANTED), entry("./B.zip", name="Bee", genre="Rol")]
        once, first = refresh(GAMELIST, *wanted)
        twice, second = refresh(once, *wanted)
        self.assertEqual((len(first), second, twice), (2, (), once))

    def test_an_escape_the_file_uses_is_not_a_difference(self):
        text = GAMELIST.replace("Sample Quest", "Say &quot;Hi&quot; &#39;x&#39;")
        _, changes = refresh(text, entry("./A.zip", name="Say \"Hi\" 'x'"))
        self.assertEqual(changes, ())

    def test_nothing_to_change_gives_the_same_text(self):
        new, changes = refresh(GAMELIST, entry("./A.zip", name="Sample Quest"), entry("./Z.zip"))
        self.assertEqual((new, changes), (GAMELIST, ()))

    def test_nomultiscrape_and_comments_are_left_alone(self):
        new, changes = refresh(GAMELIST, entry("./C.zip", **WANTED))
        self.assertEqual((new, changes), (GAMELIST, ()))

    def test_the_alternative_emulator_block_is_kept(self):
        text = (
            "<alternativeEmulator>\n  <label>Core</label>\n</alternativeEmulator>\n"
            '<?xml version="1.0"?>\n<gameList>\n<game>\n  <path>./A.zip</path>\n'
            "  <name>A</name>\n  <altemulator>Core</altemulator>\n</game>\n</gameList>\n"
        )
        new, changes = refresh(text, entry("./A.zip", name="B"))
        self.assertEqual(len(changes), 1)
        self.assertEqual(new, text.replace("<name>A</name>", "<name>B</name>"))

    def test_an_edit_that_would_not_read_back_is_left_out(self):
        text = (
            '<?xml version="1.0"?>\n<gameList>\n<game>\n  <path>./A.zip</path>\n'
            "  <desc><![CDATA[Old]]></desc>\n</game>\n</gameList>\n"
        )
        new, changes = refresh(text, entry("./A.zip", desc="New"))
        self.assertEqual((new, changes), (text, ()))

    def test_a_folder_entry_is_never_edited(self):
        text = (
            '<?xml version="1.0"?>\n<gameList>\n<folder>\n  <path>./Dir</path>\n'
            "  <name>Dir</name>\n</folder>\n</gameList>\n"
        )
        new, changes = refresh(text, entry("./Dir", name="Carpeta"))
        self.assertEqual((new, changes), (text, ()))


class NotEditableTest(unittest.TestCase):
    CASES: ClassVar[dict[str, Any]] = {
        "cdata name": ("  <name><![CDATA[Old]]></name>\n", {"name": "New"}),
        "attributes": ('  <desc lang="es">Old</desc>\n', {"desc": "New"}),
        "empty cdata": ("  <desc><![CDATA[]]></desc>\n", {"desc": "New"}),
    }

    def test_each_is_left_as_it_is_and_counted(self):
        for label, (body, fields) in self.CASES.items():
            with self.subTest(label):
                text = one_game(body)
                new, changes = refresh(text, entry("./A.zip", **fields))
                self.assertEqual((new, changes), (text, ()))
                self.assertEqual(len(left_out(text, entry("./A.zip", **fields))), 1)

    def test_a_path_the_text_spells_another_way_is_left_out(self):
        for path in ("./a&#38;b.zip", "./Hacks\\X.zip"):
            with self.subTest(path=path):
                text = one_game("  <name>Old</name>\n", path)
                wanted = entry("./a&b.zip" if "&" in path else "./Hacks/X.zip", name="New")
                self.assertEqual(refresh(text, wanted), (text, ()))
                self.assertEqual(len(left_out(text, wanted)), 1)

    def test_an_empty_tag_that_can_be_replaced_in_place_is(self):
        for body in ("  <desc></desc>\n", "  <desc/>\n", "  <desc></desc>\n  <desc>Old</desc>\n"):
            with self.subTest(body=body):
                new, changes = refresh(one_game(body), entry("./A.zip", desc="New"))
                self.assertEqual(len(changes), 1)
                self.assertEqual(new.count("<desc"), body.count("<desc"))
                self.assertEqual(artwork.parse_gamelist(new)[0].fields["desc"], "New")

    def test_an_entry_left_out_does_not_stop_the_others(self):
        text = one_game('  <desc lang="es">Old</desc>\n').replace(
            "</gameList>",
            "<game>\n  <path>./B.zip</path>\n  <name>B</name>\n</game>\n</gameList>",
        )
        new, applied, left = scrape.refresh_entries(
            text,
            artwork.parse_gamelist(text),
            [entry("./A.zip", desc="New"), entry("./B.zip", name="Bee")],
        )
        self.assertEqual(
            ([c.path for c in applied], [c.path for c in left]), (["B.zip"], ["A.zip"])
        )
        self.assertIn("<name>Bee</name>", new)
        self.assertIn('<desc lang="es">Old</desc>', new)


class RenderTest(unittest.TestCase):
    def changes(self):
        return (
            scrape.TextChange(
                "A.zip",
                (
                    scrape.FieldChange("name", "Sample Quest", "Quest de Muestra"),
                    scrape.FieldChange("desc", "x", "y"),
                ),
            ),
            scrape.TextChange("B.zip", (scrape.FieldChange("genre", "", "Rol"),)),
        )

    def test_every_name_change_is_listed_and_the_fields_counted(self):
        text = scrape.render_refresh(
            "snes", self.changes(), checked=40, not_in_cache=5, dry_run=True
        )
        self.assertEqual(
            text.splitlines(),
            [
                "scrape: snes: would refresh the text of 2 of 40 entries (name 1, desc 1, genre 1)"
                "; 5 not in the cache",
                '  renamed    A.zip: "Sample Quest" -> "Quest de Muestra"',
            ],
        )

    def test_the_real_run_says_refreshed(self):
        text = scrape.render_refresh(
            "snes", self.changes()[1:], checked=1, not_in_cache=0, dry_run=False
        )
        self.assertEqual(text, "scrape: snes: refreshed the text of 1 of 1 entry (genre 1)")

    def test_nothing_to_change(self):
        text = scrape.render_refresh("snes", (), checked=3, not_in_cache=0, dry_run=False)
        self.assertEqual(text, "scrape: snes: the text of 3 entries is up to date")

    def test_the_entries_left_out_are_counted(self):
        text = scrape.render_refresh(
            "snes", self.changes(), checked=40, not_in_cache=0, left_out=2, dry_run=False
        )
        self.assertTrue(text.splitlines()[0].endswith("; 2 left as they are (not editable)"))
        none = scrape.render_refresh(
            "snes", (), checked=3, not_in_cache=0, left_out=1, dry_run=True
        )
        self.assertEqual(
            none, "scrape: snes: no text changed in 3 entries; 1 left as they are (not editable)"
        )

    def test_warnings_are_listed(self):
        dropped = [scrape.Dropped("./Z.zip", "entry is not a staged game; dropped")]
        text = scrape.render_refresh(
            "snes", (), checked=1, not_in_cache=1, dropped=dropped, dry_run=True
        )
        self.assertIn("  warning    ./Z.zip: entry is not a staged game; dropped", text)

    def test_the_totals(self):
        self.assertEqual(
            scrape.render_refresh_totals(self.changes(), 2, dry_run=True),
            "scrape: would refresh the text of 2 entries in 2 systems (name 1, desc 1, genre 1)",
        )
        self.assertEqual(
            scrape.render_refresh_totals((), 2, dry_run=False), "scrape: no text to refresh"
        )


if __name__ == "__main__":
    unittest.main()
