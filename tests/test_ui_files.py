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
FILES = ["index.html", "api.js", "app.js", "style.css", "placeholder.svg", "favicon.svg"]


def read(name: str) -> str:
    return (UI / name).read_text(encoding="utf-8")


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
        for name in ("style.css", "api.js", "app.js"):
            self.assertIn(name, html)
        self.assertNotRegex(html, r"https?://", "no external resource")

    def test_page_is_clean_under_the_content_security_policy(self):
        # The server sends Content-Security-Policy: default-src 'self': no inline script, no
        # inline style (attribute or element), no handler attributes, no data: URL.
        html = read("index.html")
        self.assertNotRegex(html, r"<script(?![^>]*\ssrc=)")
        self.assertNotRegex(html, r"<style")
        self.assertNotRegex(html, r"\sstyle=")
        self.assertNotRegex(html, r"\son[a-z]+=")
        for name in ("index.html", "app.js", "api.js", "style.css"):
            self.assertNotRegex(read(name), r"""["'(]data:""", name)
        self.assertNotRegex(read("app.js"), r"setAttribute\(\s*'style'")

    def test_nothing_external_is_fetched(self):
        for name in ("app.js", "api.js", "style.css"):
            self.assertNotRegex(read(name), r"https?://", name)

    def test_token_is_not_stored(self):
        for name in ("app.js", "api.js"):
            text = read(name)
            self.assertNotIn("localStorage", text, name)
            self.assertNotIn("sessionStorage", text, name)

    def test_no_html_injection(self):
        # The page builds nodes; it never parses strings that carry catalog or scrape text.
        for name in ("app.js", "api.js"):
            text = read(name)
            for bad in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval("):
                self.assertNotIn(bad, text, name)

    def test_requests_are_in_the_design(self):
        unknown = js_requests() - design_endpoints()
        self.assertEqual(unknown, set(), "api.js calls endpoints the design does not list")

    def test_every_path_in_the_scripts_is_a_design_path(self):
        paths = {path for _, path in design_endpoints()}
        for name in ("app.js", "api.js"):
            found = {
                normalize(p) for p in re.findall(r"/api/v1/(?:\$\{[^}]*\}|[^`'\"\s$])*", read(name))
            }
            self.assertLessEqual(found, paths, name)

    def test_the_ui_uses_every_endpoint_it_needs(self):
        # /healthz is for the container's health check; the media route is an <img> URL.
        needed = {e for e in design_endpoints() if e[1] != "/healthz"}
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
        text = read("app.js")
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
        self.assertNotRegex(read("app.js"), r"replaceChildren\([^;]*:\s*null\)")

    def test_the_page_reads_the_catalog_state(self):
        text = read("app.js")
        self.assertIn("api.catalog()", text)
        self.assertIn("has not been built yet", text)

    def test_package_data_includes_the_ui(self):
        pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
        self.assertRegex(pyproject, r'"warpzone\.data"\s*=\s*\[[^\]]*"ui/\*"')


if __name__ == "__main__":
    unittest.main()
