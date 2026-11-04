"""The inbox modules form a one-way stack: `inbox_core` at the bottom, the kind handlers
(`inbox_mame`, `inbox_switch`, ...) on it, and `inbox` on top registering them. A cycle
between them was once hidden by importing the handlers inside a function, so the check
reads every `import`, at any depth, instead of just importing the modules."""

import ast
import subprocess
import sys
import unittest
from pathlib import Path

PACKAGE = Path(__file__).resolve().parent.parent / "warpzone"
ROOT = PACKAGE.parent


def inbox_modules(package: Path = PACKAGE) -> list[str]:
    return sorted(p.stem for p in package.glob("inbox*.py"))


def inbox_imports(package: Path = PACKAGE) -> dict[str, set[str]]:
    """For each inbox module, the inbox modules it imports (module level or not)."""
    modules = set(inbox_modules(package))
    graph: dict[str, set[str]] = {}
    for name in modules:
        tree = ast.parse((package / f"{name}.py").read_text(encoding="utf-8"))
        found: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                targets = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.level == 0:
                base = node.module or ""
                targets = [base, *(f"{base}.{a.name}" for a in node.names)]
            else:
                continue
            for target in targets:
                parts = target.split(".")
                if parts[0] == "warpzone" and len(parts) > 1 and parts[1] in modules:
                    found.add(parts[1])
        graph[name] = found - {name}
    return graph


def find_cycle(graph: dict[str, set[str]]) -> list[str] | None:
    """One import cycle as a list of module names, or None when there is none."""
    done: set[str] = set()

    def visit(node: str, path: list[str]) -> list[str] | None:
        if node in path:
            return [*path[path.index(node) :], node]
        if node in done:
            return None
        for nxt in sorted(graph.get(node, ())):
            cycle = visit(nxt, [*path, node])
            if cycle:
                return cycle
        done.add(node)
        return None

    for start in sorted(graph):
        cycle = visit(start, [])
        if cycle:
            return cycle
    return None


class FindCycleTests(unittest.TestCase):
    def test_a_loop_is_found_and_a_stack_is_not(self) -> None:
        self.assertEqual(find_cycle({"a": {"b"}, "b": {"c"}, "c": {"a"}}), ["a", "b", "c", "a"])
        self.assertIsNone(find_cycle({"a": {"b", "c"}, "b": {"c"}, "c": set()}))

    def test_the_old_shape_is_a_cycle(self) -> None:
        # inbox registered the handlers (in a function), and the handlers imported inbox
        self.assertIsNotNone(find_cycle({"inbox": {"inbox_mame"}, "inbox_mame": {"inbox"}}))


class InboxImportTests(unittest.TestCase):
    def test_there_are_inbox_modules_to_check(self) -> None:
        self.assertIn("inbox_core", inbox_modules())
        self.assertIn("inbox", inbox_modules())

    def test_the_inbox_modules_import_each_other_without_a_cycle(self) -> None:
        graph = inbox_imports()
        self.assertIsNone(find_cycle(graph), f"import cycle between inbox modules: {graph}")

    def test_the_core_imports_no_other_inbox_module(self) -> None:
        self.assertEqual(inbox_imports()["inbox_core"], set())

    def test_the_handlers_never_import_the_pipeline(self) -> None:
        for name, imports in inbox_imports().items():
            if name not in ("inbox", "inbox_core"):
                self.assertNotIn("inbox", imports, f"{name} imports inbox.py")

    def test_each_module_imports_on_its_own(self) -> None:
        for name in inbox_modules():
            with self.subTest(module=name):
                done = subprocess.run(
                    [
                        sys.executable,
                        "-c",
                        f"import sys; sys.path.insert(0, {str(ROOT)!r}); import warpzone.{name}",
                    ],
                    capture_output=True,
                    text=True,
                    check=False,
                )
                self.assertEqual(done.returncode, 0, done.stderr)


if __name__ == "__main__":
    unittest.main()
