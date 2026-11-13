"""The library UI's static files ship with the package and stay inside the API contract.

The page is plain HTML, CSS and JavaScript with no build step, so there is nothing to compile
and nothing that would notice a drift between it and the design. These checks read the files as
text: the endpoints and error codes the page knows must be the ones in the design page, and the
page must keep working under the policy the server sends (`default-src 'self'`). The one check
that runs code is the syntax check with `node --check`, skipped where node is not installed.
"""

import re
import shutil
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
UI = ROOT / "warpzone" / "data" / "ui"
DESIGN_DIR = ROOT / "website" / "src" / "content" / "docs" / "development" / "design"
DESIGN = DESIGN_DIR / "library-ui.md"
QUEUE_DESIGN = DESIGN_DIR / "play-queue.md"
SVG_NAMESPACE = "http://www.w3.org/2000/svg"
SCRIPTS = ["api.js", "kit.js", "store.js", "components.js", "queue.js", "views.js", "app.js"]
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


def top_level_rules(css: str) -> list[tuple[str, str]]:
    """(selector, body) of each rule outside at-rules such as `@media`, comments removed."""
    css = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
    rules: list[tuple[str, str]] = []
    depth, outside, inside, selector = 0, "", "", ""
    for ch in css:
        if ch == "{":
            if depth == 0:
                selector, inside = outside.strip(), ""
            depth += 1
            outside = ""
        elif ch == "}":
            depth -= 1
            if depth == 0 and not selector.startswith("@"):
                rules.append((selector, inside))
            outside = ""
        elif depth == 0:
            outside += ch
        else:
            inside += ch
    return rules


def design_endpoints() -> set[tuple[str, str]]:
    """(method, path) of every row of the design's endpoint table."""
    text = DESIGN.read_text(encoding="utf-8")
    rows = re.findall(r"^\| `(GET|POST) (/\S+)` \|", text, re.MULTILINE)
    return {(method, normalize(path)) for method, path in rows}


def design_error_codes() -> set[str]:
    """The codes of the error tables of the library API and of the play queue."""
    codes: set[str] = set()
    for path in (DESIGN, QUEUE_DESIGN):
        text = path.read_text(encoding="utf-8")
        codes |= set(re.findall(r"^\| \d{3} \| `([a-z-]+)` \|", text, re.MULTILINE))
    return codes


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
            if name != "store.js":
                self.assertNotIn("localStorage", text, name)
            self.assertNotIn("sessionStorage", text, name)

    def test_the_only_thing_kept_in_the_browser_is_the_last_inbox_run_seen(self):
        # `localStorage` is used by store.js alone, for the id of the newest inbox run the
        # browser has opened (the header dot), always inside try/catch; never the token.
        text = read("store.js")
        calls = re.findall(r"localStorage\.(\w+)\(([^)]*)\)", text)
        self.assertEqual(
            sorted(calls), [("getItem", "INBOX_SEEN_KEY"), ("setItem", "INBOX_SEEN_KEY, id")]
        )
        self.assertIn("const INBOX_SEEN_KEY = 'wz_inbox_seen';", text)
        for line in text.splitlines():
            if "localStorage" in line and not line.lstrip().startswith("//"):
                self.assertRegex(line, r"try \{.*\} catch", line)
        self.assertNotRegex(text, r"setInboxSeen\([^)]*token", "no token in storage")

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
        # /healthz is for the container's health check; the media and badge routes are <img> and
        # <video> URLs, which the browser loads.
        needed = {e for e in design_endpoints() if e[1] != "/healthz"}
        used = js_requests() | {
            ("GET", normalize(p))
            for p in re.findall(
                r"`(/api/v1/(?:games/\$\{[^`]*media/|ra/badges/)[^`]*)`", read("api.js")
            )
        }
        self.assertEqual(needed - used, set())

    def test_every_error_code_has_a_message(self):
        codes = design_error_codes()
        self.assertIn("stale", codes)
        self.assertIn("queue-damaged", codes)
        text = read("api.js")
        missing = sorted(code for code in codes if f"case '{code}':" not in text)
        self.assertEqual(missing, [], "api.js humanError has no message for these codes")

    def test_every_queue_operation_is_sent_by_the_page(self):
        # The operations of `POST /api/v1/queue`: one the page never sends is dead weight on the
        # hub, and one the server does not know is refused with `bad-request`.
        from warpzone import queue

        sent = set(re.findall(r"\bop: '([a-z]+)'", read("queue.js")))
        self.assertEqual(sent, set(queue._FIELDS))

    def test_the_queue_picker_preselects_no_console(self):
        # Queueing is one click and the consoles are optional: no console is ticked by default,
        # and nothing is remembered between visits (no cookie, no storage).
        text = read("queue.js")
        self.assertNotIn("document.cookie", text)
        self.assertIn("const sel = new Set();", text)
        self.assertIn("Also put it on a console", text)

    def test_queue_classes_are_defined_and_prefixed(self):
        # CSS class names are global: the queue's start with `q-`, each one is styled, and no
        # other script uses a `q-` class (a shared name once broke every home shelf).
        css = read("style.css")
        used: set[str] = set()
        for name in ("queue.js", "components.js"):
            for value in re.findall(r"class: '([^']*)'", read(name)):
                used |= {c for c in value.split() if c.startswith("q-")}
        self.assertTrue(used)
        for cls in sorted(used):
            self.assertRegex(css, rf"\.{re.escape(cls)}(?![\w-])", cls)
        for name in ("api.js", "kit.js", "store.js", "views.js", "app.js"):
            self.assertNotRegex(read(name), r"class: '[^']*\bq-", name)

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
        for kind in library_api.MEDIA_KINDS:
            self.assertIn(kind, text, kind)

    def test_nothing_the_api_does_not_provide_is_drawn(self):
        # The page draws what the API serves (play history, saves, achievements, records, video)
        # and nothing it does not: a high-score table or an attract mode have no data behind them.
        text = scripts().lower()
        for banned in ("high score", "attract mode", "leaderboard"):
            self.assertNotIn(banned, text, banned)

    def test_the_placeholder_is_used_as_a_mask(self):
        self.assertIn("placeholder.svg", read("style.css"))

    def test_a_class_has_one_layout(self):
        # Two parts of the page once used the same class name with different layouts; the later
        # rule silently rewrote the earlier one (the home rows became a two-column grid).
        displays: dict[str, set[str]] = {}
        for selector, body in top_level_rules(read("style.css")):
            found = re.search(r"(?:^|;)\s*display\s*:\s*([^;]+)", body)
            if not found:
                continue
            for one in selector.split(","):
                one = one.strip()
                if re.fullmatch(r"[a-z]*\.[\w-]+", one):
                    displays.setdefault(one, set()).add(found.group(1).strip())
        clashes = {name: sorted(values) for name, values in displays.items() if len(values) > 1}
        self.assertEqual(clashes, {}, "one class name with two layouts; rename one of them")

    @unittest.skipUnless(shutil.which("node"), "node is not on PATH, so the scripts are not parsed")
    def test_every_script_parses(self):
        # The page loads them as classic scripts (`<script src defer>`, no type="module"). A
        # plain `node --check file.js` would parse a file as CommonJS, or as a module when it
        # has `import`, so each script is compiled as a classic script with `vm.Script`, the
        # parse `node --check` does, and nothing runs. The text checks above never notice a
        # syntax error.
        html = read("index.html")
        self.assertNotIn('type="module"', html)
        node = shutil.which("node")
        assert node is not None
        program = (
            "const fs = require('fs'), vm = require('vm'); const file = process.argv[1];"
            " new vm.Script(fs.readFileSync(file, 'utf8'), { filename: file });"
        )
        for name in SCRIPTS:
            try:
                proc = subprocess.run(
                    [node, "-e", program, str(UI / name)],
                    capture_output=True,
                    text=True,
                    timeout=60,
                    check=False,
                )
            except subprocess.TimeoutExpired:
                self.fail(f"{name}: node did not finish parsing in 60 seconds")
            self.assertEqual(proc.returncode, 0, f"{name} does not parse:\n{proc.stderr}")

    def test_package_data_includes_the_ui(self):
        pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
        self.assertRegex(pyproject, r'"warpzone\.data"\s*=\s*\[[^\]]*"ui/\*"')


if __name__ == "__main__":
    unittest.main()
