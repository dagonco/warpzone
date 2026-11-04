"""ES-DE gamelist editing: the editors as they behave on awkward files (CRLF, missing entries,
entities, existing tags, indentation, comments).

The expected texts were taken from the two editors that existed before they shared a module
(the arcade emulator editor of `inbox` and the play state editor), so they pin their output
byte for byte. The two differ on purpose, and the cases show where: the emulator editor
skips entries and closing tags inside comments and keeps the file's own indentation, the
play state editor edits whatever its pattern finds.
"""

import unittest
from datetime import timedelta, timezone

from warpzone import inbox_mame, playstate
from warpzone.playstate import Wanted

CET = timezone(timedelta(hours=1))
EPOCH = 1_800_000_000  # 2027-01-15 08:00 UTC, 09:00 in CET

# Name -> (text, file name, emulator, edited text, action); no edited text: GamelistError.
MAME_CASES = {
    "crlf_add_entry": (
        (
            '<?xml version="1.0"?>\r\n'
            "<gameList>\r\n"
            "\t<game>\r\n"
            "\t\t<path>./a.zip</path>\r\n"
            "\t\t<name>A</name>\r\n"
            "\t</game>\r\n"
            "</gameList>\r\n"
        ),
        "b.zip",
        "E",
        (
            '<?xml version="1.0"?>\r\n'
            "<gameList>\r\n"
            "\t<game>\r\n"
            "\t\t<path>./a.zip</path>\r\n"
            "\t\t<name>A</name>\r\n"
            "\t</game>\r\n"
            "\t<game>\r\n"
            "\t\t<path>./b.zip</path>\r\n"
            "\t\t<altemulator>E</altemulator>\r\n"
            "\t</game>\r\n"
            "</gameList>\r\n"
        ),
        "added",
    ),
    "crlf_add_alt": (
        (
            '<?xml version="1.0"?>\r\n'
            "<gameList>\r\n"
            "\t<game>\r\n"
            "\t\t<path>./a.zip</path>\r\n"
            "\t\t<name>A</name>\r\n"
            "\t</game>\r\n"
            "</gameList>\r\n"
        ),
        "a.zip",
        "E",
        (
            '<?xml version="1.0"?>\r\n'
            "<gameList>\r\n"
            "\t<game>\r\n"
            "\t\t<path>./a.zip</path>\r\n"
            "\t\t<name>A</name>\r\n"
            "\t\t<altemulator>E</altemulator>\r\n"
            "\t</game>\r\n"
            "</gameList>\r\n"
        ),
        "added",
    ),
    "crlf_remove_alt": (
        (
            '<?xml version="1.0"?>\r\n'
            "<gameList>\r\n"
            "\t<game>\r\n"
            "\t\t<path>./a.zip</path>\r\n"
            "\t\t<name>A</name>\r\n"
            "\t\t<altemulator>E</altemulator>\r\n"
            "\t</game>\r\n"
            "</gameList>\r\n"
        ),
        "a.zip",
        None,
        (
            '<?xml version="1.0"?>\r\n'
            "<gameList>\r\n"
            "\t<game>\r\n"
            "\t\t<path>./a.zip</path>\r\n"
            "\t\t<name>A</name>\r\n"
            "\t</game>\r\n"
            "</gameList>\r\n"
        ),
        "removed",
    ),
    "crlf_change": (
        (
            '<?xml version="1.0"?>\r\n'
            "<gameList>\r\n"
            "\t<game>\r\n"
            "\t\t<path>./a.zip</path>\r\n"
            "\t\t<name>A</name>\r\n"
            "\t\t<altemulator>E</altemulator>\r\n"
            "\t</game>\r\n"
            "</gameList>\r\n"
        ),
        "a.zip",
        "F",
        (
            '<?xml version="1.0"?>\r\n'
            "<gameList>\r\n"
            "\t<game>\r\n"
            "\t\t<path>./a.zip</path>\r\n"
            "\t\t<name>A</name>\r\n"
            "\t\t<altemulator>F</altemulator>\r\n"
            "\t</game>\r\n"
            "</gameList>\r\n"
        ),
        "changed",
    ),
    "crlf_empty_self_closing_list": (
        "<gameList/>\r\n",
        "a.zip",
        "E",
        (
            "<gameList>\r\n"
            "\t<game>\r\n"
            "\t\t<path>./a.zip</path>\r\n"
            "\t\t<altemulator>E</altemulator>\r\n"
            "\t</game>\r\n"
            "</gameList>\r\n"
        ),
        "added",
    ),
    "missing_entry_without_emulator": (
        (
            '<?xml version="1.0"?>\n'
            "<gameList>\n"
            "\t<game>\n"
            "\t\t<path>./a.zip</path>\n"
            "\t\t<name>A</name>\n"
            "\t</game>\n"
            "</gameList>\n"
        ),
        "zz.zip",
        None,
        (
            '<?xml version="1.0"?>\n'
            "<gameList>\n"
            "\t<game>\n"
            "\t\t<path>./a.zip</path>\n"
            "\t\t<name>A</name>\n"
            "\t</game>\n"
            "</gameList>\n"
        ),
        "unchanged",
    ),
    "empty_self_closing_list": (
        '<?xml version="1.0"?>\n<gameList />\n',
        "a.zip",
        "E",
        (
            '<?xml version="1.0"?>\n'
            "<gameList>\n"
            "\t<game>\n"
            "\t\t<path>./a.zip</path>\n"
            "\t\t<altemulator>E</altemulator>\n"
            "\t</game>\n"
            "</gameList>\n"
        ),
        "added",
    ),
    "empty_list_takes_the_closing_indent": (
        "<gameList>\n  </gameList>\n",
        "a.zip",
        "E",
        (
            "<gameList>\n"
            "  \t<game>\n"
            "  \t\t<path>./a.zip</path>\n"
            "  \t\t<altemulator>E</altemulator>\n"
            "  \t</game>\n"
            "  </gameList>\n"
        ),
        "added",
    ),
    "no_gamelist": (
        "<other/>",
        "a.zip",
        "E",
        None,
        None,
    ),
    "nested_empty_list": (
        "<root>\n  <gameList>\n  </gameList>\n</root>\n",
        "a.zip",
        "E",
        (
            "<root>\n"
            "  <gameList>\n"
            "  \t<game>\n"
            "  \t\t<path>./a.zip</path>\n"
            "  \t\t<altemulator>E</altemulator>\n"
            "  \t</game>\n"
            "  </gameList>\n"
            "</root>\n"
        ),
        "added",
    ),
    "closing_tag_on_a_line_with_content": (
        "<gameList><game><path>./a.zip</path></game></gameList>",
        "b.zip",
        "E",
        (
            "<gameList><game><path>./a.zip</path></game>\n"
            "\t<game>\n"
            "\t\t<path>./b.zip</path>\n"
            "\t\t<altemulator>E</altemulator>\n"
            "\t</game>\n"
            "</gameList>"
        ),
        "added",
    ),
    "entities_match_the_decoded_name": (
        ("<gameList>\n\t<game>\n\t\t<path>./A &amp; B.zip</path>\n\t</game>\n</gameList>\n"),
        "A & B.zip",
        "X<Y",
        (
            "<gameList>\n"
            "\t<game>\n"
            "\t\t<path>./A &amp; B.zip</path>\n"
            "\t\t<altemulator>X&lt;Y</altemulator>\n"
            "\t</game>\n"
            "</gameList>\n"
        ),
        "added",
    ),
    "entities_in_the_current_emulator": (
        (
            "<gameList>\n"
            "\t<game>\n"
            "\t\t<path>./a.zip</path>\n"
            "\t\t<altemulator>X &lt; Y</altemulator>\n"
            "\t</game>\n"
            "</gameList>\n"
        ),
        "a.zip",
        "X < Y",
        (
            "<gameList>\n"
            "\t<game>\n"
            "\t\t<path>./a.zip</path>\n"
            "\t\t<altemulator>X &lt; Y</altemulator>\n"
            "\t</game>\n"
            "</gameList>\n"
        ),
        "unchanged",
    ),
    "entities_in_a_new_entry": (
        (
            '<?xml version="1.0"?>\n'
            "<gameList>\n"
            "\t<game>\n"
            "\t\t<path>./a.zip</path>\n"
            "\t\t<name>A</name>\n"
            "\t</game>\n"
            "</gameList>\n"
        ),
        "a&b.zip",
        "X>Y&Z",
        (
            '<?xml version="1.0"?>\n'
            "<gameList>\n"
            "\t<game>\n"
            "\t\t<path>./a.zip</path>\n"
            "\t\t<name>A</name>\n"
            "\t</game>\n"
            "\t<game>\n"
            "\t\t<path>./a&amp;b.zip</path>\n"
            "\t\t<altemulator>X&gt;Y&amp;Z</altemulator>\n"
            "\t</game>\n"
            "</gameList>\n"
        ),
        "added",
    ),
    "path_without_dot_slash": (
        ("<gameList>\n\t<game>\n\t\t<path>a.zip</path>\n\t</game>\n</gameList>\n"),
        "a.zip",
        "E",
        (
            "<gameList>\n"
            "\t<game>\n"
            "\t\t<path>a.zip</path>\n"
            "\t\t<altemulator>E</altemulator>\n"
            "\t</game>\n"
            "</gameList>\n"
        ),
        "added",
    ),
    "existing_tags_are_kept": (
        (
            "<gameList>\n"
            '\t<game id="1">\n'
            "\t\t<path>./a.zip</path>\n"
            "\t\t<name>A</name>\n"
            "\t\t<favorite>true</favorite>\n"
            "\t</game>\n"
            "</gameList>\n"
        ),
        "a.zip",
        "E",
        (
            "<gameList>\n"
            '\t<game id="1">\n'
            "\t\t<path>./a.zip</path>\n"
            "\t\t<name>A</name>\n"
            "\t\t<favorite>true</favorite>\n"
            "\t\t<altemulator>E</altemulator>\n"
            "\t</game>\n"
            "</gameList>\n"
        ),
        "added",
    ),
    "remove_keeps_the_other_tags": (
        (
            "<gameList>\n"
            "\t<game>\n"
            "\t\t<path>./a.zip</path>\n"
            "\t\t<altemulator>E</altemulator>\n"
            "\t\t<name>A</name>\n"
            "\t</game>\n"
            "</gameList>\n"
        ),
        "a.zip",
        None,
        (
            "<gameList>\n"
            "\t<game>\n"
            "\t\t<path>./a.zip</path>\n"
            "\t\t<name>A</name>\n"
            "\t</game>\n"
            "</gameList>\n"
        ),
        "removed",
    ),
    "remove_drops_an_entry_with_only_a_path": (
        (
            "<gameList>\n"
            "\t<game>\n"
            "\t\t<path>./a.zip</path>\n"
            "\t\t<altemulator>E</altemulator>\n"
            "\t</game>\n"
            "\t<game>\n"
            "\t\t<path>./b.zip</path>\n"
            "\t</game>\n"
            "</gameList>\n"
        ),
        "a.zip",
        None,
        ("<gameList>\n\t<game>\n\t\t<path>./b.zip</path>\n\t</game>\n</gameList>\n"),
        "removed",
    ),
    "remove_an_inline_emulator": (
        (
            "<gameList>\n"
            "\t<game><path>./a.zip</path><altemulator>E</altemulator><name>A</name></game>\n"
            "</gameList>\n"
        ),
        "a.zip",
        None,
        ("<gameList>\n\t<game><path>./a.zip</path><name>A</name></game>\n</gameList>\n"),
        "removed",
    ),
    "one_line_entry_gets_the_tag_inline": (
        "<gameList>\n\t<game><path>./a.zip</path></game>\n</gameList>\n",
        "a.zip",
        "E",
        (
            "<gameList>\n"
            "\t<game><path>./a.zip</path><altemulator>E</altemulator></game>\n"
            "</gameList>\n"
        ),
        "added",
    ),
    "self_closing_emulator_is_not_recognised": (
        (
            "<gameList>\n"
            "\t<game>\n"
            "\t\t<path>./a.zip</path>\n"
            "\t\t<altemulator/>\n"
            "\t</game>\n"
            "</gameList>\n"
        ),
        "a.zip",
        "E",
        (
            "<gameList>\n"
            "\t<game>\n"
            "\t\t<path>./a.zip</path>\n"
            "\t\t<altemulator/>\n"
            "\t\t<altemulator>E</altemulator>\n"
            "\t</game>\n"
            "</gameList>\n"
        ),
        "added",
    ),
    "entries_in_comments_are_ignored": (
        (
            "<gameList>\n"
            "<!--\n"
            "\t<game>\n"
            "\t\t<path>./a.zip</path>\n"
            "\t</game>\n"
            "-->\n"
            "\t<game>\n"
            "\t\t<path>./b.zip</path>\n"
            "\t</game>\n"
            "</gameList>\n"
        ),
        "a.zip",
        "E",
        (
            "<gameList>\n"
            "<!--\n"
            "\t<game>\n"
            "\t\t<path>./a.zip</path>\n"
            "\t</game>\n"
            "-->\n"
            "\t<game>\n"
            "\t\t<path>./b.zip</path>\n"
            "\t</game>\n"
            "\t<game>\n"
            "\t\t<path>./a.zip</path>\n"
            "\t\t<altemulator>E</altemulator>\n"
            "\t</game>\n"
            "</gameList>\n"
        ),
        "added",
    ),
    "closing_tag_in_a_comment_is_ignored": (
        (
            "<gameList>\n"
            "\t<game>\n"
            "\t\t<path>./b.zip</path>\n"
            "\t</game>\n"
            "<!-- </gameList> -->\n"
            "</gameList>\n"
        ),
        "a.zip",
        "E",
        (
            "<gameList>\n"
            "\t<game>\n"
            "\t\t<path>./b.zip</path>\n"
            "\t</game>\n"
            "<!-- </gameList> -->\n"
            "\t<game>\n"
            "\t\t<path>./a.zip</path>\n"
            "\t\t<altemulator>E</altemulator>\n"
            "\t</game>\n"
            "</gameList>\n"
        ),
        "added",
    ),
    "two_space_indentation_new_entry": (
        ("<gameList>\n  <game>\n    <path>./a.zip</path>\n  </game>\n</gameList>\n"),
        "b.zip",
        "E",
        (
            "<gameList>\n"
            "  <game>\n"
            "    <path>./a.zip</path>\n"
            "  </game>\n"
            "  <game>\n"
            "    <path>./b.zip</path>\n"
            "    <altemulator>E</altemulator>\n"
            "  </game>\n"
            "</gameList>\n"
        ),
        "added",
    ),
    "two_space_indentation_existing_entry": (
        (
            "<gameList>\n"
            "  <game>\n"
            "    <path>./a.zip</path>\n"
            "    <name>A</name>\n"
            "  </game>\n"
            "</gameList>\n"
        ),
        "a.zip",
        "E",
        (
            "<gameList>\n"
            "  <game>\n"
            "    <path>./a.zip</path>\n"
            "    <name>A</name>\n"
            "    <altemulator>E</altemulator>\n"
            "  </game>\n"
            "</gameList>\n"
        ),
        "added",
    ),
    "unindented_path_gets_the_default_tabs": (
        "<gameList>\n<game>\n<path>./a.zip</path>\n</game>\n</gameList>\n",
        "a.zip",
        "E",
        (
            "<gameList>\n"
            "<game>\n"
            "<path>./a.zip</path>\n"
            "\t\t<altemulator>E</altemulator>\n"
            "</game>\n"
            "</gameList>\n"
        ),
        "added",
    ),
    "unindented_list_new_entry": (
        "<gameList>\n<game>\n<path>./a.zip</path>\n</game>\n</gameList>\n",
        "b.zip",
        "E",
        (
            "<gameList>\n"
            "<game>\n"
            "<path>./a.zip</path>\n"
            "</game>\n"
            "\t<game>\n"
            "\t\t<path>./b.zip</path>\n"
            "\t\t<altemulator>E</altemulator>\n"
            "\t</game>\n"
            "</gameList>\n"
        ),
        "added",
    ),
    "entry_without_path_is_skipped": (
        "<gameList>\n\t<game>\n\t\t<name>x</name>\n\t</game>\n</gameList>\n",
        "b.zip",
        "E",
        (
            "<gameList>\n"
            "\t<game>\n"
            "\t\t<name>x</name>\n"
            "\t</game>\n"
            "\t<game>\n"
            "\t\t<path>./b.zip</path>\n"
            "\t\t<altemulator>E</altemulator>\n"
            "\t</game>\n"
            "</gameList>\n"
        ),
        "added",
    ),
    "trailing_spaces_after_the_last_tag": (
        ("<gameList>\n\t<game>\n\t\t<path>./a.zip</path>  \n\t</game>\n</gameList>\n"),
        "a.zip",
        "E",
        (
            "<gameList>\n"
            "\t<game>\n"
            "\t\t<path>./a.zip</path>  \n"
            "\t\t<altemulator>E</altemulator>\n"
            "\t</game>\n"
            "</gameList>\n"
        ),
        "added",
    ),
    "game_with_a_split_opening_tag": (
        (
            "<gameList>\n"
            "\t<game\n"
            '\t\tsource="x">\n'
            "\t\t<path>./a.zip</path>\n"
            "\t</game>\n"
            "</gameList>\n"
        ),
        "a.zip",
        "E",
        (
            "<gameList>\n"
            "\t<game\n"
            '\t\tsource="x">\n'
            "\t\t<path>./a.zip</path>\n"
            "\t\t<altemulator>E</altemulator>\n"
            "\t</game>\n"
            "</gameList>\n"
        ),
        "added",
    ),
    "path_with_attributes_is_not_an_entry": (
        (
            "<gameList>\n"
            "\t<game>\n"
            "\t\t<name>x</name>\n"
            '\t\t<path attr="1">./a.zip</path>\n'
            "\t</game>\n"
            "</gameList>\n"
        ),
        "a.zip",
        "E",
        (
            "<gameList>\n"
            "\t<game>\n"
            "\t\t<name>x</name>\n"
            '\t\t<path attr="1">./a.zip</path>\n'
            "\t</game>\n"
            "\t<game>\n"
            "\t\t<path>./a.zip</path>\n"
            "\t\t<altemulator>E</altemulator>\n"
            "\t</game>\n"
            "</gameList>\n"
        ),
        "added",
    ),
    "inline_path_then_indented_tags": (
        ("<gameList>\n\t<game><path>./a.zip</path>\n\t\t<name>x</name>\n\t</game>\n</gameList>\n"),
        "a.zip",
        "E",
        (
            "<gameList>\n"
            "\t<game><path>./a.zip</path>\n"
            "\t\t<name>x</name>\n"
            "\t\t<altemulator>E</altemulator>\n"
            "\t</game>\n"
            "</gameList>\n"
        ),
        "added",
    ),
}


# Name -> (text, changes, included, edited text); no edited text: PlayStateError.
STATE_CASES = {
    "crlf_values": (
        (
            '<?xml version="1.0"?>\r\n'
            "<gameList>\r\n"
            "\t<game>\r\n"
            "\t\t<path>./a.zip</path>\r\n"
            "\t\t<name>A</name>\r\n"
            "\t</game>\r\n"
            "</gameList>\r\n"
        ),
        {"a.zip": Wanted(plays=3, favorite=True, last=EPOCH)},
        {},
        (
            '<?xml version="1.0"?>\r\n'
            "<gameList>\r\n"
            "\t<game>\r\n"
            "\t\t<path>./a.zip</path>\r\n"
            "\t\t<name>A</name>\r\n"
            "\t\t<favorite>true</favorite>\r\n"
            "\t\t<playcount>3</playcount>\r\n"
            "\t\t<lastplayed>20270115T090000</lastplayed>\r\n"
            "\t</game>\r\n"
            "</gameList>\r\n"
        ),
    ),
    "crlf_new_entry": (
        (
            '<?xml version="1.0"?>\r\n'
            "<gameList>\r\n"
            "\t<game>\r\n"
            "\t\t<path>./a.zip</path>\r\n"
            "\t\t<name>A</name>\r\n"
            "\t</game>\r\n"
            "</gameList>\r\n"
        ),
        {"b.zip": Wanted(plays=1)},
        {},
        (
            '<?xml version="1.0"?>\r\n'
            "<gameList>\r\n"
            "\t<game>\r\n"
            "\t\t<path>./a.zip</path>\r\n"
            "\t\t<name>A</name>\r\n"
            "\t</game>\r\n"
            "\t<game>\r\n"
            "\t\t<path>./b.zip</path>\r\n"
            "\t\t<playcount>1</playcount>\r\n"
            "\t</game>\r\n"
            "</gameList>\r\n"
        ),
    ),
    "crlf_element": (
        (
            '<?xml version="1.0"?>\r\n'
            "<gameList>\r\n"
            "\t<game>\r\n"
            "\t\t<path>./a.zip</path>\r\n"
            "\t\t<name>A</name>\r\n"
            "\t</game>\r\n"
            "</gameList>\r\n"
        ),
        {},
        {"a.zip": 2},
        (
            '<?xml version="1.0"?>\r\n'
            "<gameList>\r\n"
            "\t<warpzonePlayState>\r\n"
            '\t\t<included path="./a.zip" plays="2" />\r\n'
            "\t</warpzonePlayState>\r\n"
            "\t<game>\r\n"
            "\t\t<path>./a.zip</path>\r\n"
            "\t\t<name>A</name>\r\n"
            "\t</game>\r\n"
            "</gameList>\r\n"
        ),
    ),
    "new_entry": (
        (
            '<?xml version="1.0"?>\n'
            "<gameList>\n"
            "\t<game>\n"
            "\t\t<path>./a.zip</path>\n"
            "\t\t<name>A</name>\n"
            "\t</game>\n"
            "</gameList>\n"
        ),
        {"b.zip": Wanted(plays=1, favorite=True, last=EPOCH)},
        {},
        (
            '<?xml version="1.0"?>\n'
            "<gameList>\n"
            "\t<game>\n"
            "\t\t<path>./a.zip</path>\n"
            "\t\t<name>A</name>\n"
            "\t</game>\n"
            "\t<game>\n"
            "\t\t<path>./b.zip</path>\n"
            "\t\t<favorite>true</favorite>\n"
            "\t\t<playcount>1</playcount>\n"
            "\t\t<lastplayed>20270115T090000</lastplayed>\n"
            "\t</game>\n"
            "</gameList>\n"
        ),
    ),
    "new_entry_with_nothing_to_say": (
        (
            '<?xml version="1.0"?>\n'
            "<gameList>\n"
            "\t<game>\n"
            "\t\t<path>./a.zip</path>\n"
            "\t\t<name>A</name>\n"
            "\t</game>\n"
            "</gameList>\n"
        ),
        {"b.zip": Wanted(favorite=False)},
        {},
        (
            '<?xml version="1.0"?>\n'
            "<gameList>\n"
            "\t<game>\n"
            "\t\t<path>./a.zip</path>\n"
            "\t\t<name>A</name>\n"
            "\t</game>\n"
            "</gameList>\n"
        ),
    ),
    "element_added": (
        (
            '<?xml version="1.0"?>\n'
            "<gameList>\n"
            "\t<game>\n"
            "\t\t<path>./a.zip</path>\n"
            "\t\t<name>A</name>\n"
            "\t</game>\n"
            "</gameList>\n"
        ),
        {},
        {"a.zip": 2},
        (
            '<?xml version="1.0"?>\n'
            "<gameList>\n"
            "\t<warpzonePlayState>\n"
            '\t\t<included path="./a.zip" plays="2" />\n'
            "\t</warpzonePlayState>\n"
            "\t<game>\n"
            "\t\t<path>./a.zip</path>\n"
            "\t\t<name>A</name>\n"
            "\t</game>\n"
            "</gameList>\n"
        ),
    ),
    "element_replaced": (
        (
            '<?xml version="1.0"?>\n'
            "<gameList>\n"
            "\t<warpzonePlayState>\n"
            '\t\t<included path="./x.zip" plays="9" />\n'
            "\t</warpzonePlayState>\n"
            "\t<game>\n"
            "\t\t<path>./a.zip</path>\n"
            "\t\t<name>A</name>\n"
            "\t</game>\n"
            "</gameList>\n"
        ),
        {},
        {"a.zip": 2},
        (
            '<?xml version="1.0"?>\n'
            "<gameList>\n"
            "\t<warpzonePlayState>\n"
            '\t\t<included path="./a.zip" plays="2" />\n'
            "\t</warpzonePlayState>\n"
            "\t<game>\n"
            "\t\t<path>./a.zip</path>\n"
            "\t\t<name>A</name>\n"
            "\t</game>\n"
            "</gameList>\n"
        ),
    ),
    "element_emptied": (
        (
            '<?xml version="1.0"?>\n'
            "<gameList>\n"
            "\t<warpzonePlayState>\n"
            '\t\t<included path="./x.zip" plays="9" />\n'
            "\t</warpzonePlayState>\n"
            "\t<game>\n"
            "\t\t<path>./a.zip</path>\n"
            "\t\t<name>A</name>\n"
            "\t</game>\n"
            "</gameList>\n"
        ),
        {},
        {},
        (
            '<?xml version="1.0"?>\n'
            "<gameList>\n"
            "\t<warpzonePlayState>\n"
            "\t</warpzonePlayState>\n"
            "\t<game>\n"
            "\t\t<path>./a.zip</path>\n"
            "\t\t<name>A</name>\n"
            "\t</game>\n"
            "</gameList>\n"
        ),
    ),
    "nothing_to_do": (
        (
            '<?xml version="1.0"?>\n'
            "<gameList>\n"
            "\t<game>\n"
            "\t\t<path>./a.zip</path>\n"
            "\t\t<name>A</name>\n"
            "\t</game>\n"
            "</gameList>\n"
        ),
        {},
        {},
        (
            '<?xml version="1.0"?>\n'
            "<gameList>\n"
            "\t<game>\n"
            "\t\t<path>./a.zip</path>\n"
            "\t\t<name>A</name>\n"
            "\t</game>\n"
            "</gameList>\n"
        ),
    ),
    "same_element_is_left_alone": (
        (
            '<?xml version="1.0"?>\n'
            "<gameList>\n"
            "\t<warpzonePlayState>\n"
            '\t\t<included path="./a.zip" plays="2" />\n'
            "\t</warpzonePlayState>\n"
            "\t<game>\n"
            "\t\t<path>./a.zip</path>\n"
            "\t\t<name>A</name>\n"
            "\t</game>\n"
            "</gameList>\n"
        ),
        {},
        {"a.zip": 2},
        (
            '<?xml version="1.0"?>\n'
            "<gameList>\n"
            "\t<warpzonePlayState>\n"
            '\t\t<included path="./a.zip" plays="2" />\n'
            "\t</warpzonePlayState>\n"
            "\t<game>\n"
            "\t\t<path>./a.zip</path>\n"
            "\t\t<name>A</name>\n"
            "\t</game>\n"
            "</gameList>\n"
        ),
    ),
    "entities_match_the_decoded_path": (
        ("<gameList>\n\t<game>\n\t\t<path>./A &amp; B.zip</path>\n\t</game>\n</gameList>\n"),
        {"A & B.zip": Wanted(plays=2)},
        {},
        (
            "<gameList>\n"
            "\t<game>\n"
            "\t\t<path>./A &amp; B.zip</path>\n"
            "\t\t<playcount>2</playcount>\n"
            "\t</game>\n"
            "</gameList>\n"
        ),
    ),
    "entities_in_new_entry_and_element": (
        (
            '<?xml version="1.0"?>\n'
            "<gameList>\n"
            "\t<game>\n"
            "\t\t<path>./a.zip</path>\n"
            "\t\t<name>A</name>\n"
            "\t</game>\n"
            "</gameList>\n"
        ),
        {"A & B <1>.zip": Wanted(plays=2)},
        {"A & B <1>.zip": 1},
        (
            '<?xml version="1.0"?>\n'
            "<gameList>\n"
            "\t<warpzonePlayState>\n"
            '\t\t<included path="./A &amp; B &lt;1&gt;.zip" plays="1" />\n'
            "\t</warpzonePlayState>\n"
            "\t<game>\n"
            "\t\t<path>./a.zip</path>\n"
            "\t\t<name>A</name>\n"
            "\t</game>\n"
            "\t<game>\n"
            "\t\t<path>./A &amp; B &lt;1&gt;.zip</path>\n"
            "\t\t<playcount>2</playcount>\n"
            "\t</game>\n"
            "</gameList>\n"
        ),
    ),
    "existing_tags_are_replaced_or_removed": (
        (
            "<gameList>\n"
            "\t<game>\n"
            "\t\t<path>./a.zip</path>\n"
            "\t\t<playcount>1</playcount>\n"
            "\t\t<favorite>true</favorite>\n"
            "\t\t<lastplayed>20200101T000000</lastplayed>\n"
            "\t\t<name>A</name>\n"
            "\t</game>\n"
            "</gameList>\n"
        ),
        {"a.zip": Wanted(plays=7, favorite=False, last=EPOCH)},
        {},
        (
            "<gameList>\n"
            "\t<game>\n"
            "\t\t<path>./a.zip</path>\n"
            "\t\t<playcount>7</playcount>\n"
            "\t\t<lastplayed>20270115T090000</lastplayed>\n"
            "\t\t<name>A</name>\n"
            "\t</game>\n"
            "</gameList>\n"
        ),
    ),
    "self_closing_tag_is_replaced": (
        (
            "<gameList>\n"
            "\t<game>\n"
            "\t\t<path>./a.zip</path>\n"
            "\t\t<favorite/>\n"
            "\t</game>\n"
            "</gameList>\n"
        ),
        {"a.zip": Wanted(favorite=True)},
        {},
        (
            "<gameList>\n"
            "\t<game>\n"
            "\t\t<path>./a.zip</path>\n"
            "\t\t<favorite>true</favorite>\n"
            "\t</game>\n"
            "</gameList>\n"
        ),
    ),
    "remove_an_inline_tag": (
        ("<gameList>\n\t<game><path>./a.zip</path><favorite>true</favorite></game>\n</gameList>\n"),
        {"a.zip": Wanted(favorite=False)},
        {},
        "<gameList>\n\t<game><path>./a.zip</path></game>\n</gameList>\n",
    ),
    "one_line_entry_gets_the_tag_inline": (
        "<gameList>\n\t<game><path>./a.zip</path></game>\n</gameList>\n",
        {"a.zip": Wanted(plays=4)},
        {},
        ("<gameList>\n\t<game><path>./a.zip</path><playcount>4</playcount></game>\n</gameList>\n"),
    ),
    "two_space_indentation_existing_entry": (
        ("<gameList>\n  <game>\n    <path>./a.zip</path>\n  </game>\n</gameList>\n"),
        {"a.zip": Wanted(plays=4)},
        {},
        (
            "<gameList>\n"
            "  <game>\n"
            "    <path>./a.zip</path>\n"
            "    <playcount>4</playcount>\n"
            "  </game>\n"
            "</gameList>\n"
        ),
    ),
    "two_space_indentation_new_entry": (
        ("<gameList>\n  <game>\n    <path>./a.zip</path>\n  </game>\n</gameList>\n"),
        {"b.zip": Wanted(plays=4)},
        {},
        (
            "<gameList>\n"
            "  <game>\n"
            "    <path>./a.zip</path>\n"
            "  </game>\n"
            "\t<game>\n"
            "\t\t<path>./b.zip</path>\n"
            "\t\t<playcount>4</playcount>\n"
            "\t</game>\n"
            "</gameList>\n"
        ),
    ),
    "unindented_path_gets_no_indent": (
        "<gameList>\n<game>\n<path>./a.zip</path>\n</game>\n</gameList>\n",
        {"a.zip": Wanted(plays=4)},
        {},
        (
            "<gameList>\n"
            "<game>\n"
            "<path>./a.zip</path>\n"
            "<playcount>4</playcount>\n"
            "</game>\n"
            "</gameList>\n"
        ),
    ),
    "path_after_another_tag": (
        (
            "<gameList>\n"
            "\t<game>\n"
            "\t\t<name>x</name>\n"
            "\t\t<path>./a.zip</path>\n"
            "\t</game>\n"
            "</gameList>\n"
        ),
        {"a.zip": Wanted(plays=4)},
        {},
        (
            "<gameList>\n"
            "\t<game>\n"
            "\t\t<name>x</name>\n"
            "\t\t<path>./a.zip</path>\n"
            "\t\t<playcount>4</playcount>\n"
            "\t</game>\n"
            "</gameList>\n"
        ),
    ),
    "entries_in_comments_are_edited": (
        ("<gameList>\n<!--\n\t<game>\n\t\t<path>./a.zip</path>\n\t</game>\n-->\n</gameList>\n"),
        {"a.zip": Wanted(plays=4)},
        {},
        (
            "<gameList>\n"
            "<!--\n"
            "\t<game>\n"
            "\t\t<path>./a.zip</path>\n"
            "\t\t<playcount>4</playcount>\n"
            "\t</game>\n"
            "-->\n"
            "</gameList>\n"
        ),
    ),
    "folders_are_not_games": (
        ("<gameList>\n\t<folder>\n\t\t<path>./a.zip</path>\n\t</folder>\n</gameList>\n"),
        {"a.zip": Wanted(plays=4)},
        {},
        (
            "<gameList>\n"
            "\t<folder>\n"
            "\t\t<path>./a.zip</path>\n"
            "\t</folder>\n"
            "\t<game>\n"
            "\t\t<path>./a.zip</path>\n"
            "\t\t<playcount>4</playcount>\n"
            "\t</game>\n"
            "</gameList>\n"
        ),
    ),
    "only_the_first_duplicate_is_edited": (
        (
            "<gameList>\n"
            "\t<game>\n"
            "\t\t<path>./a.zip</path>\n"
            "\t</game>\n"
            "\t<game>\n"
            "\t\t<path>./a.zip</path>\n"
            "\t</game>\n"
            "</gameList>\n"
        ),
        {"a.zip": Wanted(plays=4)},
        {},
        (
            "<gameList>\n"
            "\t<game>\n"
            "\t\t<path>./a.zip</path>\n"
            "\t\t<playcount>4</playcount>\n"
            "\t</game>\n"
            "\t<game>\n"
            "\t\t<path>./a.zip</path>\n"
            "\t</game>\n"
            "</gameList>\n"
        ),
    ),
    "no_gamelist": (
        "<other/>",
        {"a.zip": Wanted(plays=4)},
        {},
        None,
    ),
    "self_closing_gamelist": (
        "<gameList />\n",
        {"a.zip": Wanted(plays=4)},
        {},
        None,
    ),
    "no_closing_tag": (
        "<gameList>\n",
        {"a.zip": Wanted(plays=4)},
        {},
        None,
    ),
    "closing_tag_on_a_line_with_content": (
        "<gameList>\n\t<game><path>./a.zip</path></game></gameList>",
        {"b.zip": Wanted(plays=4)},
        {},
        (
            "<gameList>\n"
            "\t<game><path>./a.zip</path></game>\t<game>\n"
            "\t\t<path>./b.zip</path>\n"
            "\t\t<playcount>4</playcount>\n"
            "\t</game>\n"
            "</gameList>"
        ),
    ),
    "closing_tag_in_a_comment_breaks_the_edit": (
        ("<gameList>\n\t<game>\n\t\t<path>./a.zip</path>\n\t</game>\n<!-- </gameList> -->\n"),
        {"b.zip": Wanted(plays=4)},
        {},
        None,
    ),
    "opening_tag_with_content_after_it": (
        ("<gameList>\t<game>\n\t\t<path>./a.zip</path>\n\t</game>\n</gameList>\n"),
        {},
        {"a.zip": 1},
        (
            "<gameList>\t\n"
            "\t<warpzonePlayState>\n"
            '\t\t<included path="./a.zip" plays="1" />\n'
            "\t</warpzonePlayState>\n"
            "<game>\n"
            "\t\t<path>./a.zip</path>\n"
            "\t</game>\n"
            "</gameList>\n"
        ),
    ),
    "trailing_spaces_after_the_last_tag": (
        ("<gameList>\n\t<game>\n\t\t<path>./a.zip</path>  \n\t</game>\n</gameList>\n"),
        {"a.zip": Wanted(plays=4)},
        {},
        (
            "<gameList>\n"
            "\t<game>\n"
            "\t\t<path>./a.zip</path>  \n"
            "\t\t<playcount>4</playcount>\n"
            "\t</game>\n"
            "</gameList>\n"
        ),
    ),
    "path_with_attributes": (
        (
            "<gameList>\n"
            "\t<game>\n"
            "\t\t<name>x</name>\n"
            '\t\t<path attr="1">./a.zip</path>\n'
            "\t</game>\n"
            "</gameList>\n"
        ),
        {"a.zip": Wanted(plays=4)},
        {},
        (
            "<gameList>\n"
            "\t<game>\n"
            "\t\t<name>x</name>\n"
            '\t\t<path attr="1">./a.zip</path>\n'
            "\t\t<playcount>4</playcount>\n"
            "\t</game>\n"
            "</gameList>\n"
        ),
    ),
    "inline_path_then_indented_tags": (
        ("<gameList>\n\t<game><path>./a.zip</path>\n\t\t<name>x</name>\n\t</game>\n</gameList>\n"),
        {"a.zip": Wanted(plays=4)},
        {},
        (
            "<gameList>\n"
            "\t<game><path>./a.zip</path>\n"
            "\t\t<name>x</name>\n"
            "<playcount>4</playcount>\n"
            "\t</game>\n"
            "</gameList>\n"
        ),
    ),
}


class EmulatorEditorTests(unittest.TestCase):
    def test_every_case_keeps_its_exact_output(self) -> None:
        for name, (text, file_name, emulator, expected, action) in MAME_CASES.items():
            with self.subTest(name):
                if expected is None:
                    with self.assertRaises(inbox_mame.GamelistError):
                        edit_emulator(text, file_name, emulator)
                else:
                    self.assertEqual(edit_emulator(text, file_name, emulator), (expected, action))


class StateEditorTests(unittest.TestCase):
    def test_every_case_keeps_its_exact_output(self) -> None:
        for name, (text, changes, included, expected) in STATE_CASES.items():
            with self.subTest(name):
                if expected is None:
                    with self.assertRaises(playstate.PlayStateError):
                        edit_state(text, changes, included)
                else:
                    self.assertEqual(edit_state(text, changes, included), expected)


def edit_emulator(text: str, file_name: str, emulator: str | None) -> tuple[str, str]:
    return inbox_mame.update_gamelist_entry(text, file_name, emulator)


def edit_state(text: str, changes: dict, included: dict) -> str:
    return playstate.edit_gamelist(text, changes, included, "gba", CET)


if __name__ == "__main__":
    unittest.main()
