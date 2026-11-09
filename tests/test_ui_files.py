"""The library UI's static files ship with the package and stay inside the API contract.

The page is plain HTML, CSS and JavaScript with no build step, so there is nothing to compile
and nothing that would notice a drift between it and the design. These checks read the files as
text: the endpoints and error codes the page knows must be the ones in the design page, and the
page must keep working under the policy the server sends (`default-src 'self'`).
"""

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
UI = ROOT / "warpzone" / "data" / "ui"
DESIGN = ROOT / "website" / "src" / "content" / "docs" / "development" / "design" / "library-ui.md"
SVG_NAMESPACE = "http://www.w3.org/2000/svg"
SCRIPTS = ["api.js", "kit.js", "store.js", "components.js", "views.js", "app.js"]
FILES = ["index.html", *SCRIPTS, "style.css", "placeholder.svg", "favicon.svg"]


def read(name: str) -> str:
    return (UI / name).read_text(encoding="utf-8")


def scripts() -> str:
    """Every script of the page as one text, for the checks that do not care which file."""
    return "\n".join(read(name) for name in SCRIPTS)


def normalize(path: str) -> str:
    """A path with its placeholders unified: `{system}` and `${x}` both become `{}`."""
    path = re.sub(r"\$\{[^}]*\}|\{[^}]*\}", "{}", path)
    return path.split("?", 1)[0]


def design_endpoints() -> set[tuple[str, str]]:
    """(method, path) of every row of the design's endpoint table."""
    text = DESIGN.read_text(encoding="utf-8")
    rows = re.findall(r"^\| `(GET|POST) (/\S+)` \|", text, re.MULTILINE)
    return {(method, normalize(path)) for method, path in rows}


def design_error_codes() -> set[str]:
    text = DESIGN.read_text(encoding="utf-8")
    return set(re.findall(r"^\| \d{3} \| `([a-z-]+)` \|", text, re.MULTILINE))


# Endpoints the API has ahead of the page: a later change paints them and removes the entry, so
# the check below keeps failing for any other endpoint the page forgets.
AHEAD_OF_THE_UI = {("GET", "/api/v1/recent")}


def js_requests() -> set[tuple[str, str]]:
    """(method, path) of every `request('METHOD', '/api/v1/...')` call in api.js."""
    pattern = r"request\(\s*'(GET|POST)',\s*[`'](/api/v1/[^`']*)[`']"
    return {(m, normalize(p)) for m, p in re.findall(pattern, read("api.js"))}


class UiFilesTest(unittest.TestCase):
    def test_files_exist(self):
        for name in FILES:
            self.assertTrue((UI / name).is_file(), name)

    def test_page_loads_its_own_files_only(self):
        html = read("index.html")
        for name in ("style.css", *SCRIPTS):
            self.assertIn(name, html)
        self.assertNotRegex(html, r"https?://", "no external resource")

    def test_every_file_of_the_folder_is_known(self):
        # A script that is not in the page never runs; a file that is not listed is not tested.
        self.assertEqual(sorted(p.name for p in UI.iterdir()), sorted(FILES))
        html = read("index.html")
        order = [html.index(f'src="{name}"') for name in SCRIPTS]
        self.assertEqual(order, sorted(order), "scripts load in dependency order")

    def test_page_is_clean_under_the_content_security_policy(self):
        # The server sends Content-Security-Policy: default-src 'self': no inline script, no
        # inline style (attribute or element), no handler attributes, no data: URL.
        html = read("index.html")
        self.assertNotRegex(html, r"<script(?![^>]*\ssrc=)")
        self.assertNotRegex(html, r"<style")
        self.assertNotRegex(html, r"\sstyle=")
        self.assertNotRegex(html, r"\son[a-z]+=")
        for name in ("index.html", "style.css", *SCRIPTS):
            self.assertNotRegex(read(name), r"""["'(]data:""", name)
        for name in SCRIPTS:
            self.assertNotRegex(read(name), r"setAttribute\(\s*'style'", name)
            self.assertNotIn("cssText", read(name), name)

    def test_nothing_external_is_fetched(self):
        for name in ("style.css", *SCRIPTS):
            # The SVG namespace is an identifier for createElementNS, not an address to fetch.
            self.assertNotRegex(read(name).replace(SVG_NAMESPACE, ""), r"https?://", name)

    def test_token_is_not_stored(self):
        for name in SCRIPTS:
            text = read(name)
            self.assertNotIn("localStorage", text, name)
            self.assertNotIn("sessionStorage", text, name)

    def test_no_html_injection(self):
        # The page builds nodes; it never parses strings that carry catalog or scrape text.
        for name in SCRIPTS:
            text = read(name)
            for bad in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval("):
                self.assertNotIn(bad, text, name)

    def test_requests_are_in_the_design(self):
        unknown = js_requests() - design_endpoints()
        self.assertEqual(unknown, set(), "api.js calls endpoints the design does not list")

    def test_every_path_in_the_scripts_is_a_design_path(self):
        paths = {path for _, path in design_endpoints()}
        for name in SCRIPTS:
            found = {
                normalize(p) for p in re.findall(r"/api/v1/(?:\$\{[^}]*\}|[^`'\"\s$])*", read(name))
            }
            self.assertLessEqual(found, paths, name)

    def test_the_ui_uses_every_endpoint_it_needs(self):
        # /healthz is for the container's health check; the media route is an <img> URL.
        needed = {e for e in design_endpoints() if e[1] != "/healthz"} - AHEAD_OF_THE_UI
        used = js_requests() | {
            ("GET", normalize(p))
            for p in re.findall(r"`(/api/v1/games/\$\{[^`]*media/[^`]*)`", read("api.js"))
        }
        self.assertEqual(needed - used, set())

    def test_every_error_code_has_a_message(self):
        codes = design_error_codes()
        self.assertIn("stale", codes)
        text = read("api.js")
        missing = sorted(code for code in codes if f"case '{code}':" not in text)
        self.assertEqual(missing, [], "api.js humanError has no message for these codes")

    def test_every_state_the_api_can_send_has_a_display(self):
        # The states of "HTTP API" and "Progress" in the design: the page must name each one.
        text = scripts()
        states = [
            "unknown",
            "unsupported",
            "needs-migration",
            "missing",
            "not-shared",
            "paused",
            "offline",
            "pending",
            "downloading",
            "done",
        ]
        for state in states:
            self.assertRegex(text, rf"""['"]{state}['"]|\b{state}:""", state)
        design = DESIGN.read_text(encoding="utf-8")
        for state in states:
            self.assertIn(f"`{state}`", design, f"the design does not describe {state}")

    def test_replace_children_is_never_given_a_null(self):
        # `replaceChildren(null)` inserts the text "null"; an optional node goes in as a spread.
        for name in SCRIPTS:
            self.assertNotRegex(read(name), r"replaceChildren\([^;()]*\?[^;()]*:\s*null\)", name)

    def test_the_page_reads_the_catalog_state(self):
        text = scripts()
        self.assertIn("api.catalog()", text)
        self.assertIn("has not been built yet", text)

    def test_every_media_kind_the_api_serves_is_shown(self):
        # A kind the API lists but the page never draws would be dead weight on the hub.
        from warpzone import library_api

        text = read("views.js")
        # The API serves these before the page paints them; the PR that draws them removes this.
        not_painted_yet = {"videos", "backcovers"}
        for kind in library_api.MEDIA_KINDS:
            if kind not in not_painted_yet:
                self.assertIn(kind, text, kind)

    def test_nothing_the_api_does_not_provide_is_drawn(self):
        # Play statistics, saves, achievement lists, records and video are later work: they must
        # not appear, not even as placeholders.
        text = scripts().lower()
        for banned in (
            "continue playing",
            "high score",
            "most played",
            "playtime",
            "video",
            "attract",
            "save state",
        ):
            self.assertNotIn(banned, text, banned)

    def test_the_placeholder_is_used_as_a_mask(self):
        self.assertIn("placeholder.svg", read("style.css"))

    def test_package_data_includes_the_ui(self):
        pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
        self.assertRegex(pyproject, r'"warpzone\.data"\s*=\s*\[[^\]]*"ui/\*"')


if __name__ == "__main__":
    unittest.main()
