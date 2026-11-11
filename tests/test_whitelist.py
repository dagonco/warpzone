import unittest

from warpzone import whitelist as wl
from warpzone.whitelist import (
    Game,
    GameChange,
    Model,
    Refused,
    SystemChange,
    Unlistable,
)

PSX_GAME = "roms/psx/Example Game (Europe).chd"
WII_GAME = "roms/wii/Example Game [ABCD01].rvz"
PS4_GAME = "roms/ps4/games/CUSA00000"
PS4_SHORTCUT = "roms/ps4/shortcuts/Example Game.desktop"

MANAGED = """\
// Managed by warpzone (library UI). Edit it in the UI; hand edits are refused.
// warpzone-whitelist 1
#include patterns-common.stignore

// Excluded
/roms/psx/Example Game (Europe).chd

// Systems
!/roms/gba
!/roms/psx
!/roms/switch
!/extras/switch/updates
!/extras/switch/dlc

// Games
!/roms/ps4/games/CUSA00000
!/roms/ps4/shortcuts/Example Game.desktop
!/roms/wii/Example Game \\[ABCD01\\].rvz

// Other
!/bios

// Folders
/roms/ps4/games/*
!/roms/ps4/games
/roms/ps4/shortcuts/*
!/roms/ps4/shortcuts
/extras/switch/*
!/extras/switch
/roms/ps4/*
!/roms/ps4
/roms/wii/*
!/roms/wii
/extras/*
!/extras
/roms/*
!/roms

*
"""

# The same file as 0.4.0rc1 and rc2 wrote it: no Folders section.
MANAGED_RC = MANAGED[: MANAGED.index("// Folders\n")] + "*\n"

MANAGED_MODEL = Model(
    frozenset({"gba", "psx", "switch"}),
    frozenset({PS4_GAME, PS4_SHORTCUT, WII_GAME, "bios"}),
    frozenset({PSX_GAME}),
)

# A hand-written whitelist of the shape the repository has used so far.
HAND = """\
// deck: what the deck receives
#include patterns-common.stignore

// systems
!/roms/snes
!/roms/gba
!/roms/psx

// single games
!/roms/wii/Example Game \\[ABCD01\\].rvz
!/roms/ps4/games/CUSA00000
!/roms/ps4/shortcuts/Example Game.desktop
!/roms/psvita/app/PCSE00000
!/roms/psvita/license/PCSE00000
!/roms/psvita/Example Vita Game.psvita
!/bios

*
"""


def game(ident: str, system: str, *parts: str) -> Game:
    return Game(ident, system, parts)


CATALOG = [
    game("a", "psx", PSX_GAME),
    game("b", "psx", "roms/psx/Other Game (USA).chd"),
    game("c", "gba", "roms/gba/Example Advance (USA).gba"),
    game("d", "ps4", PS4_GAME, PS4_SHORTCUT),
    game("e", "ps4", "roms/ps4/games/CUSA00001", "roms/ps4/shortcuts/Other.desktop"),
    game("f", "wii", WII_GAME),
    game("g", "wii", "roms/wii/Other Game [WXYZ01].rvz"),
    game("h", "switch", "roms/switch/Example Base.nsp", "extras/switch/updates/Example Update"),
]


class PathTests(unittest.TestCase):
    def test_escape_covers_every_special_character_and_round_trips(self) -> None:
        for char in "\\*?[]{}":
            path = f"roms/x/a{char}b"
            line = wl.escape(path)
            self.assertEqual(line, f"/roms/x/a\\{char}b")
            self.assertEqual(wl.unescape(line), path)
        plain = "roms/psx/Some Game (Europe) !#.chd"
        self.assertEqual(wl.escape(plain), "/" + plain)
        self.assertEqual(wl.unescape(wl.escape(plain)), plain)

    def test_unescape_refuses_what_it_cannot_read_literally(self) -> None:
        for bad in ("roms/x", "/roms/*", "/roms/a**", "/roms/[a]", "/roms/x\\", "/a{b,c}"):
            with self.assertRaises(ValueError, msg=bad):
                wl.unescape(bad)

    def test_unlistable_names(self) -> None:
        for bad in (
            "",
            "roms/a\nb",
            "roms/a\rb",
            "roms/a\0b",
            " roms/a",
            "roms/a /b",
            "roms/ a",
            "roms/a ",
            "roms//a",
            "roms/a/",
            "/roms/a",
            "roms/../a",
            "roms/./a",
            "roms/bad\udcff.chd",  # a non-UTF-8 name read with surrogateescape
        ):
            self.assertTrue(wl.unlistable(bad), repr(bad))
        self.assertFalse(wl.unlistable("roms/psx/Some Game (Europe).chd"))
        self.assertFalse(wl.unlistable("roms/a b/c  d"))

    def test_render_refuses_an_unlistable_path(self) -> None:
        with self.assertRaises(Unlistable):
            wl.render(Model(includes=frozenset({"roms/psx/bad\nname"})))

    def test_system_roots(self) -> None:
        self.assertEqual(wl.system_roots("psx"), ("roms/psx",))
        self.assertEqual(wl.system_roots("wiiu"), ("roms/wiiu/roms",))
        self.assertEqual(
            wl.system_roots("switch"),
            ("roms/switch", "extras/switch/updates", "extras/switch/dlc"),
        )
        self.assertEqual(wl.system_of_root("roms/xbox360/roms"), "xbox360")
        self.assertIsNone(wl.system_of_root("roms/xbox360"))
        self.assertEqual(wl.system_of_root("extras/switch/dlc"), "switch")
        self.assertIsNone(wl.system_of_root("roms/psx/Game.chd"))


class RenderTests(unittest.TestCase):
    def test_the_design_example_renders_byte_for_byte(self) -> None:
        self.assertEqual(wl.render(MANAGED_MODEL), MANAGED)

    def test_parse_reads_it_back_and_the_file_is_managed(self) -> None:
        self.assertEqual(wl.parse(MANAGED), MANAGED_MODEL)
        self.assertTrue(wl.is_managed(MANAGED))

    def test_an_empty_section_is_left_out_with_its_comment(self) -> None:
        text = wl.render(Model(systems=frozenset({"gba"})))
        self.assertNotIn("Excluded", text)
        self.assertNotIn("Games", text)
        self.assertTrue(text.endswith("// Folders\n/roms/*\n!/roms\n\n*\n"))
        nothing = wl.render(Model())
        self.assertTrue(nothing.endswith("#include patterns-common.stignore\n\n*\n"))
        self.assertEqual(wl.parse(nothing), Model())

    def test_sorting_is_by_unescaped_code_point_order(self) -> None:
        model = Model(includes=frozenset({"roms/b/B", "roms/b/a", "roms/a/z", "roms/b/[x]"}))
        text = wl.render(model)
        lines = [ln for ln in text.split("// Folders")[0].splitlines() if ln.startswith("!")]
        self.assertEqual(
            lines,
            ["!/roms/a/z", "!/roms/b/B", "!/roms/b/[x]".replace("[x]", "\\[x\\]"), "!/roms/b/a"],
        )

    def test_render_normalizes_so_one_set_of_paths_is_one_file(self) -> None:
        loose = Model(
            frozenset({"gba"}),
            frozenset({"roms/gba/Game.gba", "roms/n64/a.z64", "roms/n64"}),
            frozenset({"roms/snes/Game.sfc", "roms/n64/b.z64"}),
        )
        text = wl.render(loose)
        self.assertEqual(wl.parse(text), wl.normalize(loose))
        self.assertNotIn("Game.gba", text)
        self.assertNotIn("snes", text)

    def test_round_trip_is_stable(self) -> None:
        for model in (MANAGED_MODEL, Model(), wl.parse(MANAGED)):
            text = wl.render(model)
            self.assertEqual(wl.render(wl.parse(text)), text)

    def test_a_tampered_file_is_not_managed(self) -> None:
        for text in (
            MANAGED.replace("// Systems", "// Systems\n// hand note"),
            MANAGED.replace("!/roms/gba\n!/roms/psx", "!/roms/psx\n!/roms/gba"),
            MANAGED + "\n",
            MANAGED.rstrip("\n"),
            MANAGED.replace("\n*\n", "\n!/roms/x\n*\n"),
            MANAGED.replace("// warpzone-whitelist 1", "// warpzone-whitelist 2"),
            HAND,
        ):
            self.assertFalse(wl.is_managed(text), text)
        # Removing a whole section's line is a valid, other model.
        self.assertTrue(wl.is_managed(MANAGED.replace("!/roms/gba\n", "")))

    def test_parse_refuses_a_wildcard_and_a_partial_system(self) -> None:
        with self.assertRaises(wl.ParseError):
            wl.parse(MANAGED.replace("!/roms/gba", "!/roms/g*"))
        with self.assertRaises(wl.ParseError):
            wl.parse(MANAGED.replace("!/extras/switch/dlc\n", ""))
        with self.assertRaises(wl.ParseError):
            wl.parse(MANAGED.replace("// Systems", "// Games"))

    def test_the_folders_section_is_derived_so_parse_skips_it(self) -> None:
        self.assertEqual(wl.parse(MANAGED), wl.parse(MANAGED_RC))
        self.assertEqual(wl.parse(MANAGED), MANAGED_MODEL)

    def test_a_round_trip_is_stable_for_models_with_folders(self) -> None:
        for model in (
            MANAGED_MODEL,
            Model(frozenset({"wiiu", "xbox360"}), frozenset({"roms/psvita/license/PCSE00000"})),
            Model(includes=frozenset({"roms/arcade/Set {1} [x]/Game.zip", "extras/switch/dlc/A"})),
            Model(frozenset({"psx"}), frozenset({"roms/psx/Odd/x"}), frozenset({"roms/psx/a/b"})),
        ):
            text = wl.render(model)
            self.assertTrue(wl.is_managed(text), text)
            self.assertEqual(wl.render(wl.parse(text)), text)

    def test_a_managed_file_without_the_folders_section_needs_the_migration(self) -> None:
        self.assertFalse(wl.is_managed(MANAGED_RC))
        reading = wl.read(MANAGED_RC)
        self.assertEqual(reading.status, "needs-migration")
        self.assertEqual(reading.model, MANAGED_MODEL)
        self.assertEqual(wl.read(MANAGED).status, "managed")

    def test_the_folders_section_must_be_byte_for_byte_the_derived_one(self) -> None:

        def pair(folder: str) -> str:
            return f"/{folder}/*\n!/{folder}\n"

        for text in (
            MANAGED.replace(pair("roms/wii"), ""),
            MANAGED.replace(
                pair("roms/ps4") + pair("roms/wii"), pair("roms/wii") + pair("roms/ps4")
            ),
            MANAGED.replace(pair("extras") + pair("roms"), pair("roms") + pair("extras")),
            MANAGED.replace(pair("roms"), pair("roms") + pair("roms/zz")),
            MANAGED.replace("!/roms/wii\n", ""),
            MANAGED.replace("/roms/wii/*\n", ""),
            MANAGED.replace("!/roms/wii\n", "!/roms/wii\n!/roms/wii\n"),
            MANAGED.replace("// Folders\n", "// Folders\n// note\n"),
            MANAGED.replace("// Folders\n", "// Folders\n!/bios\n"),  # well formed, not derived
            MANAGED.replace("\n// Folders\n", "\n\n// Folders\n"),
        ):
            self.assertFalse(wl.is_managed(text), text)
        # A Folders title with nothing under it is not what render writes either.
        self.assertFalse(wl.is_managed(MANAGED_RC.replace("*\n", "// Folders\n\n*\n")))

    def test_parse_refuses_a_folders_line_it_cannot_read(self) -> None:
        for bad in (
            MANAGED.replace("/roms/*\n!/roms\n", "roms/*\n!/roms\n"),
            MANAGED.replace("/roms/*\n!/roms\n", "/roms/*\n!/roms/*\n"),
            MANAGED.replace("/roms/*\n!/roms\n", "/ro*ms/*\n!/roms\n"),
            MANAGED.replace("/roms/*\n!/roms\n", "/roms\n!/roms\n"),
            MANAGED.replace("/roms/*\n!/roms\n", "/roms/*\n!/roms/\n"),
        ):
            with self.assertRaises(wl.ParseError, msg=bad):
                wl.parse(bad)
        with self.assertRaises(wl.ParseError):  # the Folders section comes last
            wl.parse(MANAGED.replace("// Other", "// Folders\n/roms/*\n!/roms\n\n// Other"))

    def test_etag(self) -> None:
        self.assertEqual(wl.etag(None), "absent")
        self.assertEqual(len(wl.etag(b"x")), 64)


def first_match(text: str, path: str) -> bool:
    """Whether a file written by `render` leaves `path` on, by Syncthing's first-match rule.

    Test-only: the lines are anchored literals, `/D/*` and `*`. A pattern also matches
    everything below what it names (the implicit `/**`), and `/D/*` does not match `D` itself.
    """
    for raw in text.splitlines():
        if not raw or raw.startswith(("//", "#")):
            continue
        negated = raw.startswith("!")
        pattern = raw.removeprefix("!")
        if pattern == "*":
            return negated
        if pattern.endswith("/*"):
            hit = path.startswith(wl.unescape(pattern[:-2]) + "/")
        else:
            base = wl.unescape(pattern)
            hit = path == base or path.startswith(base + "/")
        if hit:
            return negated
    return True


class FoldersTests(unittest.TestCase):
    def test_the_folders_above_what_is_on(self) -> None:
        self.assertEqual(
            wl.parents(MANAGED_MODEL),
            [
                "roms/ps4/games",
                "roms/ps4/shortcuts",
                "extras/switch",
                "roms/ps4",
                "roms/wii",
                "extras",
                "roms",
            ],
        )
        self.assertEqual(wl.parents(Model(frozenset({"gba"}))), ["roms"])
        self.assertEqual(wl.parents(Model(includes=frozenset({"bios"}))), [])
        self.assertEqual(wl.parents(Model()), [])
        self.assertEqual(wl.parents(Model(frozenset({"wiiu"}))), ["roms/wiiu", "roms"])

    def test_nothing_is_added_below_a_folder_that_is_on(self) -> None:
        model = Model(
            frozenset({"psx"}),
            frozenset({"roms/psx/Deep/er/Game.chd"}),
            frozenset({"roms/psx/a/b"}),
        )
        self.assertEqual(wl.parents(model), ["roms"])
        self.assertEqual(
            wl.parents(Model(includes=frozenset({"roms/gba", "roms/gba/x"}))), ["roms"]
        )

    def test_the_section_is_the_last_before_the_final_star(self) -> None:
        text = wl.render(MANAGED_MODEL)
        titles = [ln for ln in text.splitlines() if ln.startswith("// ") and "Managed" not in ln]
        self.assertEqual(
            titles[1:], ["// Excluded", "// Systems", "// Games", "// Other", "// Folders"]
        )
        self.assertTrue(text.endswith("!/roms\n\n*\n"))

    def test_the_lines_are_escaped_and_only_the_last_star_is_a_wildcard(self) -> None:
        model = Model(includes=frozenset({"roms/arcade/Set {1}/Game.zip"}))
        self.assertEqual(wl.parents(model), ["roms/arcade/Set {1}", "roms/arcade", "roms"])
        text = wl.render(model)
        self.assertIn("/roms/arcade/Set \\{1\\}/*\n!/roms/arcade/Set \\{1\\}\n", text)
        self.assertEqual(wl.parse(text), model)

    def test_an_unlistable_folder_is_refused_like_any_path(self) -> None:
        with self.assertRaises(Unlistable):
            wl.render(Model(includes=frozenset({"roms/bad\nname/x"})))

    def test_files_are_decided_as_before_and_only_the_folders_are_added(self) -> None:
        files = [
            PSX_GAME,
            "roms/psx/Other Game (USA).chd",
            "roms/gba/Advance.gba",
            "roms/n64/Other.z64",
            "roms/wii/Example Game [ABCD01].rvz",
            "roms/wii/Other Game [WXYZ01].rvz",
            "roms/ps4/games/CUSA00000/eboot.bin",
            "roms/ps4/games/CUSA00001/eboot.bin",
            PS4_SHORTCUT,
            "roms/ps4/shortcuts/Other.desktop",
            "roms/ps4/dlc/CUSA00000/dlc.pkg",
            "roms/switch/Base.nsp",
            "extras/switch/updates/U/u.nsp",
            "extras/switch/base/b.nsp",
            "extras/other/o.txt",
            "bios/psx/scph.bin",
            "notes.txt",
        ]
        folders = {a for f in files for a in wl.ancestors(f) if a != f}
        for model in (MANAGED_MODEL, Model(), Model(frozenset({"wiiu"}), frozenset({PS4_GAME}))):
            text = wl.render(model)
            wanted = set(wl.parents(model))
            for path in files:
                self.assertEqual(first_match(text, path), wl.decide(model, path), (path, text))
            for path in folders:
                self.assertEqual(
                    first_match(text, path), wl.decide(model, path) or path in wanted, (path, text)
                )
            # The same file without the section: the decision of every file is the same.
            rc = text[: text.index("// Folders\n")] + "*\n" if wanted else text
            for path in files:
                self.assertEqual(first_match(rc, path), first_match(text, path), path)


class NormalizeTests(unittest.TestCase):
    def test_each_rule(self) -> None:
        model = Model(
            frozenset({"psx"}),
            frozenset(
                {
                    "roms/psx/Covered by a root.chd",
                    "roms/n64/dir",
                    "roms/n64/dir/Covered by an include.z64",
                    "roms/snes/dir/Shadowed.sfc",
                    "bios",
                }
            ),
            frozenset(
                {
                    "roms/snes/dir",
                    "roms/atari/Useless.a26",
                    "roms/psx/Hole.chd",
                    "roms/n64/dir/hole.z64",
                    "roms/n64/dir/hole.z64/inner",
                }
            ),
        )
        self.assertEqual(
            wl.normalize(model),
            Model(
                frozenset({"psx"}),
                frozenset({"roms/n64/dir", "bios"}),
                frozenset({"roms/psx/Hole.chd", "roms/n64/dir/hole.z64"}),
            ),
        )

    def test_it_is_idempotent_and_keeps_what_is_on(self) -> None:
        model = Model(
            frozenset({"gba"}),
            frozenset({"roms/gba/x", "roms/a/b", "roms/a/b/c", "roms/q"}),
            frozenset({"roms/a/b/c", "roms/a/x", "roms/q/sub", "roms/z"}),
        )
        once = wl.normalize(model)
        self.assertEqual(wl.normalize(once), once)
        for path in (
            "roms/gba/x",
            "roms/a/b",
            "roms/a/b/c",
            "roms/a/b/d",
            "roms/a/x",
            "roms/q",
            "roms/q/sub",
            "roms/q/sub/deep",
            "roms/z",
        ):
            self.assertEqual(wl.decide(model, path), wl.decide(once, path), path)

    def test_decide(self) -> None:
        self.assertTrue(wl.decide(MANAGED_MODEL, "roms/psx/Other Game (USA).chd"))
        self.assertFalse(wl.decide(MANAGED_MODEL, PSX_GAME))
        self.assertTrue(wl.decide(MANAGED_MODEL, "roms/ps4/games/CUSA00000/eboot.bin"))
        self.assertFalse(wl.decide(MANAGED_MODEL, "roms/ps4/games/CUSA00001"))
        self.assertFalse(wl.decide(MANAGED_MODEL, "roms/ps4"))
        self.assertTrue(wl.decide(MANAGED_MODEL, "extras/switch/dlc/Some DLC/file.nsp"))
        self.assertTrue(wl.decide(MANAGED_MODEL, "bios/psx/scph.bin"))
        self.assertFalse(wl.decide(MANAGED_MODEL, "biosx"))


class ChangeTests(unittest.TestCase):
    def applied(self, model: Model, change: wl.Change, **kw: object) -> wl.Applied:
        return wl.apply(model, change, CATALOG, **kw)  # type: ignore[arg-type]

    def test_game_on_adds_an_include_per_part(self) -> None:
        got = self.applied(Model(), GameChange(CATALOG[3], True))
        self.assertEqual(got.model.includes, {PS4_GAME, PS4_SHORTCUT})
        self.assertEqual(got.effect.turned_on, ("d",))

    def test_game_on_removes_the_exclusion_instead_of_adding_an_include(self) -> None:
        model = Model(frozenset({"psx"}), excludes=frozenset({PSX_GAME}))
        got = self.applied(model, GameChange(CATALOG[0], True))
        self.assertEqual(got.model, Model(frozenset({"psx"})))

    def test_game_off_inside_a_system_adds_an_exclude(self) -> None:
        got = self.applied(Model(frozenset({"psx"})), GameChange(CATALOG[0], False))
        self.assertEqual(got.model, Model(frozenset({"psx"}), excludes=frozenset({PSX_GAME})))
        self.assertEqual(got.effect.turned_off, ("a",))

    def test_game_off_drops_its_includes(self) -> None:
        model = Model(includes=frozenset({PS4_GAME, PS4_SHORTCUT, WII_GAME}))
        got = self.applied(model, GameChange(CATALOG[3], False))
        self.assertEqual(got.model, Model(includes=frozenset({WII_GAME})))

    def test_game_on_and_off_again_returns_to_the_start(self) -> None:
        start = Model(frozenset({"psx"}))
        for item in CATALOG:
            on = self.applied(start, GameChange(item, True)).model
            off = self.applied(on, GameChange(item, False)).model
            self.assertEqual(wl.game_state(on, item.parts), "on", item.id)
            self.assertEqual(wl.game_state(off, item.parts), "off", item.id)

    def test_game_on_under_a_parent_exclusion_is_covered(self) -> None:
        model = Model(frozenset({"psx"}), excludes=frozenset({"roms/psx"}))
        # The exclusion sits above the part: normalized it is kept (a root covers it).
        with self.assertRaises(Refused) as ctx:
            self.applied(model, GameChange(CATALOG[0], True))
        self.assertEqual(ctx.exception.code, "covered")

    def test_system_on_drops_what_is_below_and_turns_on_every_game(self) -> None:
        model = Model(
            includes=frozenset({PS4_GAME, "roms/psx/Other Game (USA).chd"}),
            excludes=frozenset(),
        )
        got = self.applied(model, SystemChange("psx", True))
        self.assertEqual(got.model, Model(frozenset({"psx"}), frozenset({PS4_GAME})))
        self.assertEqual(got.effect.turned_on, ("a",))  # "b" was already on

    def test_system_on_removes_the_holes_below_its_roots(self) -> None:
        hole = game("p", "psx", "roms/psx/a.chd")
        other = game("q", "psx", "roms/psx/b.chd")
        model = Model(frozenset({"psx"}), excludes=frozenset({"roms/psx/a.chd"}))
        got = wl.apply(model, SystemChange("psx", True), [hole, other])
        self.assertEqual(got.model, Model(frozenset({"psx"})))
        self.assertEqual(got.effect.turned_on, ("p",))
        sub_hole = game("r", "psx", "roms/psx/sub/a.chd")
        sub_other = game("s", "psx", "roms/psx/sub/b.chd")
        model = Model(
            includes=frozenset({"roms/psx/sub"}), excludes=frozenset({"roms/psx/sub/a.chd"})
        )
        got = wl.apply(model, SystemChange("psx", True), [sub_hole, sub_other])
        self.assertEqual(got.model, Model(frozenset({"psx"})))
        self.assertEqual(got.effect.turned_on, ("r",))

    def test_system_off_drops_the_roots_and_everything_below(self) -> None:
        got = self.applied(MANAGED_MODEL, SystemChange("psx", False))
        self.assertEqual(
            got.model,
            Model(
                frozenset({"gba", "switch"}), frozenset({PS4_GAME, PS4_SHORTCUT, WII_GAME, "bios"})
            ),
        )
        self.assertEqual(got.effect.turned_off, ("b",))
        got = self.applied(MANAGED_MODEL, SystemChange("switch", False))
        self.assertEqual(got.effect.turned_off, ("h",))
        self.assertNotIn("switch", got.model.systems)

    def test_a_change_that_touches_another_game_is_refused(self) -> None:
        shared = [
            game("x", "psx", "roms/psx/Shared.cue", "roms/psx/Shared.bin"),
            game("y", "psx", "roms/psx/Other.cue", "roms/psx/Shared.bin"),
        ]
        with self.assertRaises(Refused) as ctx:
            wl.apply(Model(), GameChange(shared[0], True), shared)
        self.assertEqual(ctx.exception.code, "cannot-apply")

    def test_a_system_change_that_does_not_reach_every_game_is_refused(self) -> None:
        # An include above the system keeps its games on.
        model = Model(includes=frozenset({"roms"}))
        with self.assertRaises(Refused) as ctx:
            self.applied(model, SystemChange("psx", False))
        self.assertEqual(ctx.exception.code, "cannot-apply")

    def test_unsupported_systems_cannot_be_turned_on_but_can_be_turned_off(self) -> None:
        with self.assertRaises(Refused) as ctx:
            self.applied(Model(), SystemChange("ps4", True), excluded_systems=("ps4",))
        self.assertEqual(ctx.exception.code, "unsupported")
        with self.assertRaises(Refused):
            self.applied(Model(), GameChange(CATALOG[3], True), excluded_systems=("ps4",))
        model = Model(frozenset({"ps4"}))
        got = self.applied(model, SystemChange("ps4", False), excluded_systems=("ps4",))
        self.assertEqual(got.model, Model())

    def test_an_unlistable_part_is_refused(self) -> None:
        bad = game("z", "psx", "roms/psx/bad\nname.chd")
        with self.assertRaises(Refused) as ctx:
            wl.apply(Model(), GameChange(bad, True), [bad])
        self.assertEqual(ctx.exception.code, "unlistable")

    def test_every_change_agrees_with_decide(self) -> None:
        # Property: after any change, exactly the asked games moved.
        models = [Model(), MANAGED_MODEL, Model(frozenset({"psx", "ps4"}))]
        for model in models:
            for item in CATALOG:
                for on in (True, False):
                    try:
                        got = self.applied(model, GameChange(item, on))
                    except Refused:
                        continue
                    for other in CATALOG:
                        if other.id != item.id:
                            self.assertEqual(
                                wl.game_state(model, other.parts),
                                wl.game_state(got.model, other.parts),
                            )
                    self.assertEqual(wl.game_state(got.model, item.parts), "on" if on else "off")
            for system in ("psx", "ps4", "gba", "wii", "switch"):
                members = [g for g in CATALOG if g.system == system]
                for on in (True, False):
                    # None of these models has an exclusion above a root: never refused.
                    got = self.applied(model, SystemChange(system, on))
                    for other in CATALOG:
                        state = wl.game_state(got.model, other.parts)
                        if other.system == system:
                            self.assertEqual(state, "on" if on else "off", other.id)
                        else:
                            self.assertEqual(state, wl.game_state(model, other.parts), other.id)
                    self.assertEqual(system in got.model.systems, on)
                    self.assertTrue(members)

    def test_system_changes_from_a_model_with_holes_never_refuse(self) -> None:
        # Holes and includes below the roots must not stop a system from turning on or off.
        models = [
            Model(frozenset({"psx"}), excludes=frozenset({PSX_GAME})),
            Model(
                frozenset({"psx", "switch"}), excludes=frozenset({"roms/switch/Example Base.nsp"})
            ),
            Model(
                includes=frozenset({"roms/gba"}),
                excludes=frozenset({"roms/gba/Example Advance (USA).gba"}),
            ),
        ]
        for model in models:
            for system in ("psx", "gba", "switch"):
                for on in (True, False):
                    self.applied(model, SystemChange(system, on))

    def test_states(self) -> None:
        self.assertEqual(wl.game_state(MANAGED_MODEL, CATALOG[3].parts), "on")
        self.assertEqual(wl.game_state(MANAGED_MODEL, CATALOG[4].parts), "off")
        partial = Model(includes=frozenset({PS4_GAME}))
        self.assertEqual(wl.game_state(partial, CATALOG[3].parts), "partial")
        psx = [g for g in CATALOG if g.system == "psx"]
        self.assertEqual(wl.system_state(MANAGED_MODEL, psx), "some")
        self.assertEqual(wl.system_state(Model(frozenset({"psx"})), psx), "all")
        self.assertEqual(wl.system_state(Model(), psx), "none")

    def test_effect_lists_games_that_become_partial(self) -> None:
        old = Model(includes=frozenset({PS4_GAME, PS4_SHORTCUT}))
        new = Model(includes=frozenset({PS4_GAME}))
        got = wl.effect(old, new, CATALOG)
        self.assertEqual(got.other, ("d",))
        self.assertEqual(got.changed, ("d",))


class UnsupportedTests(unittest.TestCase):
    def test_names_the_excluded_systems_in_roots_includes_and_excludes(self) -> None:
        model = Model(
            frozenset({"switch", "gba"}),
            frozenset({PS4_GAME, WII_GAME, "extras/switch/updates/Update", "bios"}),
            frozenset({"roms/ps4/shortcuts/Hole.desktop", PSX_GAME}),
        )
        got = wl.unsupported(model, ["ps4", "switch"])
        self.assertEqual(
            [(u.system, u.kind, u.path) for u in got],
            [
                ("switch", "system", "roms/switch"),
                ("switch", "include", "extras/switch/updates/Update"),
                ("ps4", "include", PS4_GAME),
                ("ps4", "exclude", "roms/ps4/shortcuts/Hole.desktop"),
            ],
        )
        self.assertEqual(wl.unsupported(model, []), [])
        self.assertEqual(wl.unsupported(model, ["nes"]), [])


class HandParseTests(unittest.TestCase):
    def test_a_real_shaped_file_parses(self) -> None:
        hand = wl.parse_hand(HAND)
        self.assertEqual(hand.errors, ())
        paths = [(ln.on, ln.path) for ln in hand.lines]
        self.assertIn((True, "roms/snes"), paths)
        self.assertIn((True, WII_GAME), paths)
        self.assertEqual(paths[-1], (False, ""))

    def test_refusals_carry_the_line_numbers(self) -> None:
        cases = {
            "!/roms/ps4/shortcuts/*.desktop": "wildcard",
            "!/roms/psx/**": "wildcard",
            "!roms/psx": "anchored",
            "roms/psx": "anchored",
            "(?i)/roms/psx": "modifiers",
            "(?d)/roms/psx": "modifiers",
            "#include other.stignore": "included",
            "!/roms/psx/": "empty component",
            "!/roms/ps\\": "backslash",
            "# a comment hash": "anchored",
        }
        for line, why in cases.items():
            text = HAND.replace("!/bios\n", line + "\n")
            hand = wl.parse_hand(text)
            bad = [e for e in hand.errors if why in e]
            self.assertTrue(bad, (line, hand.errors))
            self.assertTrue(bad[0].startswith("line 16:"), (line, bad))

    def test_structure_errors(self) -> None:
        no_star = HAND.replace("\n*\n", "\n")
        self.assertIn("does not end with *", " ".join(wl.parse_hand(no_star).errors))
        after = HAND + "!/roms/nes\n"
        self.assertIn("after the final *", " ".join(wl.parse_hand(after).errors))
        self.assertEqual(wl.parse_hand(HAND + "\n// trailing note\n").errors, ())
        twice = HAND.replace("// systems", "#include patterns-common.stignore\n// systems")
        self.assertIn("twice", " ".join(wl.parse_hand(twice).errors))
        late = HAND.replace("#include patterns-common.stignore\n", "").replace(
            "!/bios\n", "#include patterns-common.stignore\n!/bios\n"
        )
        self.assertIn("before the patterns", " ".join(wl.parse_hand(late).errors))
        missing = HAND.replace("#include patterns-common.stignore\n", "")
        self.assertIn("does not start with", " ".join(wl.parse_hand(missing).errors))
        crlf = HAND.replace("\n", "\r\n")
        self.assertTrue(wl.parse_hand(crlf).errors)

    def test_a_managed_file_of_the_release_candidates_parses_as_a_hand_file_too(self) -> None:
        # The file 0.4.0rc1 and rc2 wrote has no Folders section, so it is a valid hand file and
        # `whitelist migrate` rewrites it. A file with the section is managed, never migrated.
        hand = wl.parse_hand(MANAGED_RC)
        self.assertEqual(hand.errors, ())
        self.assertEqual(wl.plan_migration(hand).model, MANAGED_MODEL)
        self.assertTrue(wl.parse_hand(MANAGED).errors)


class MigrationTests(unittest.TestCase):
    def plan(self, text: str) -> wl.MigrationPlan:
        hand = wl.parse_hand(text)
        self.assertEqual(hand.errors, ())
        return wl.plan_migration(hand)

    def test_classification(self) -> None:
        plan = self.plan(HAND)
        self.assertEqual(plan.model.systems, {"snes", "gba", "psx"})
        self.assertEqual(
            plan.model.includes,
            {
                WII_GAME,
                PS4_GAME,
                PS4_SHORTCUT,
                "roms/psvita/app/PCSE00000",
                "roms/psvita/license/PCSE00000",
                "roms/psvita/Example Vita Game.psvita",
                "bios",
            },
        )
        self.assertEqual(plan.model.excludes, frozenset())
        self.assertEqual((plan.shadowed, plan.redundant), ((), ()))
        self.assertTrue(wl.is_managed(wl.render(plan.model)))

    def test_switch_needs_all_three_roots_to_be_a_system(self) -> None:
        full = HAND.replace(
            "!/roms/psx\n", "!/roms/switch\n!/extras/switch/updates\n!/extras/switch/dlc\n"
        )
        self.assertEqual(self.plan(full).model.systems, {"snes", "gba", "switch"})
        partial = HAND.replace("!/roms/psx\n", "!/roms/switch\n")
        plan = self.plan(partial)
        self.assertNotIn("switch", plan.model.systems)
        self.assertIn("roms/switch", plan.model.includes)

    def test_shadowed_and_redundant_lines_are_dropped_and_reported(self) -> None:
        text = (
            HAND.replace(
                "// systems\n",
                "/roms/psx/Hole (Europe).chd\n"  # a real hole: it comes before the system line
                "// systems\n",
            )
            .replace(
                "// single games\n",
                "/roms/n64/Unused Game.z64\n// single games\n",  # redundant with the final *
            )
            .replace(
                "!/bios\n",
                "!/bios\n"
                "!/roms/psx/Already Covered.chd\n"  # shadowed by !/roms/psx
                "/roms/snes/Too Late.sfc\n",  # shadowed by !/roms/snes
            )
        )
        plan = self.plan(text)
        self.assertEqual([ln.path for ln in plan.redundant], ["roms/n64/Unused Game.z64"])
        self.assertEqual(
            sorted(ln.path for ln in plan.shadowed),
            ["roms/psx/Already Covered.chd", "roms/snes/Too Late.sfc"],
        )
        # The hole comes before !/roms/psx, so it stays as an exclusion.
        self.assertEqual(plan.model.excludes, {"roms/psx/Hole (Europe).chd"})

    def test_decide_lines_is_first_match_wins(self) -> None:
        lines = wl.parse_hand(
            HAND.replace("// systems\n", "// systems\n/roms/psx/Hole.chd\n")
        ).lines
        self.assertFalse(wl.decide_lines(lines, "roms/psx/Hole.chd"))
        self.assertTrue(wl.decide_lines(lines, "roms/psx/Other.chd"))
        self.assertTrue(wl.decide_lines(lines, "roms/psx/Dir/Other.cue"))
        self.assertTrue(wl.decide_lines(lines, "roms/psx"))
        self.assertFalse(wl.decide_lines(lines, "roms/n64"))
        self.assertFalse(wl.decide_lines(lines, "roms/n64/Game.z64"))
        self.assertTrue(wl.decide_lines(lines, "roms/wii/Example Game [ABCD01].rvz"))
        self.assertFalse(wl.decide_lines(lines, "roms/wii/Example Game [ABCD01].rvz.bak"))
        self.assertTrue(wl.decide_lines(lines, "roms/ps4/games/CUSA00000/eboot.bin"))
        self.assertTrue(wl.decide_lines(lines, "bios/psx/scph5501.bin"))
        # Without a final * a path no line matches is not ignored, as in Syncthing.
        self.assertTrue(wl.decide_lines([], "anything"))

    def paths_of(self) -> list[str]:
        return [
            "roms",
            "roms/snes",
            "roms/snes/Game.sfc",
            "roms/gba/Game.gba",
            "roms/psx",
            "roms/psx/Hole.chd",
            "roms/psx/Game/disc.cue",
            "roms/n64/Game.z64",
            WII_GAME,
            "roms/wii/Other.rvz",
            "roms/ps4/games/CUSA00000/eboot.bin",
            "roms/ps4/games/CUSA00001/eboot.bin",
            PS4_SHORTCUT,
            "roms/ps4/shortcuts/Other.desktop",
            "roms/psvita/app/PCSE00000/eboot.bin",
            "roms/psvita/license/PCSE00000/license.rif",
            "roms/psvita/Example Vita Game.psvita",
            "roms/psvita/Another.psvita",
            "bios/psx/scph5501.bin",
            "extras/switch/updates/X",
        ]

    def test_the_proof_passes_for_a_faithful_migration(self) -> None:
        text = HAND.replace("// systems\n", "// systems\n/roms/psx/Hole.chd\n")
        plan = self.plan(text)
        prover = wl.Prover(wl.parse_hand(text).lines, plan.model)
        self.assertEqual([p for p in self.paths_of() if prover.differs(p)], [])
        self.assertEqual([p for p in prover.probes() if prover.differs(p)], [])

    def test_the_proof_catches_an_include_inside_an_exclusion_inside_an_include(self) -> None:
        text = HAND.replace(
            "// systems\n",
            "// systems\n!/roms/psx/Dir/Keep.cue\n/roms/psx/Dir\n",
        )
        plan = self.plan(text)
        prover = wl.Prover(wl.parse_hand(text).lines, plan.model)
        self.assertEqual(
            [p for p in prover.probes() if prover.differs(p)], ["roms/psx/Dir/Keep.cue"]
        )

    def test_skip_prunes_only_what_is_off_and_holds_nothing_on(self) -> None:
        plan = self.plan(HAND)
        prover = wl.Prover(wl.parse_hand(HAND).lines, plan.model)
        self.assertTrue(prover.skip("roms/n64"))
        self.assertTrue(prover.skip("roms/wii/Other Dir"))
        self.assertFalse(prover.skip("roms/psx"))  # on
        self.assertFalse(prover.skip("roms"))  # an ancestor of lines
        self.assertFalse(prover.skip("roms/ps4/games"))  # holds the on CUSA folder

    def test_hand_and_model_agree_on_a_set_of_shapes(self) -> None:
        shapes = [
            HAND,
            HAND.replace("// systems\n", "// systems\n/roms/psx/Hole.chd\n/roms/snes/Dir\n"),
            "#include patterns-common.stignore\n*\n",
            "#include patterns-common.stignore\n"
            "!/roms/ps3\n/roms/ps3/Skip.ps3\n!/roms/ps3/Keep.ps3\n*\n",
            "#include patterns-common.stignore\n/roms/psx/A\n!/roms/psx\n!/roms/psx/B\n*\n",
        ]
        for text in shapes:
            plan = self.plan(text)
            prover = wl.Prover(wl.parse_hand(text).lines, plan.model)
            candidates = [*self.paths_of(), *prover.probes()]
            for path in candidates:
                self.assertEqual(
                    wl.LineMatcher(wl.parse_hand(text).lines).on(path),
                    wl.decide(plan.model, path),
                    (text, path),
                )

    def test_read_reports_the_status_and_the_meaning(self) -> None:
        managed = wl.read(MANAGED)
        self.assertEqual((managed.status, managed.model), ("managed", MANAGED_MODEL))
        hand = wl.read(HAND)
        self.assertEqual(hand.status, "needs-migration")
        assert hand.model is not None
        self.assertIn("psx", hand.model.systems)
        bad = wl.read("!/roms/*\n")
        self.assertEqual(bad.status, "needs-migration")
        self.assertIsNone(bad.model)
        self.assertTrue(bad.errors)


if __name__ == "__main__":
    unittest.main()
