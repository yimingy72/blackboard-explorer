import unittest
from pathlib import Path

from tool_manifest import COMMANDS, WORKER_TOOLS_REQUIRED_COMMANDS, render_worker_tools


class WorkerToolsTests(unittest.TestCase):
    def test_checked_in_summary_matches_generator(self) -> None:
        expected = Path(__file__).parents[1] / "worker-tools.txt"
        rendered = render_worker_tools(
            {
                "python": "Python 3.14.8",
                "node": "v22.21.1",
                "sage": "10.9",
            }
        )
        self.assertEqual(rendered, expected.read_text())

    def test_manifest_scope_contains_advertised_base_commands(self) -> None:
        for command in WORKER_TOOLS_REQUIRED_COMMANDS:
            with self.subTest(command=command):
                self.assertIn(command, COMMANDS)

    def test_summary_uses_verified_absolute_paths(self) -> None:
        summary = (Path(__file__).parents[1] / "worker-tools.txt").read_text()
        for path in (
            "/usr/bin/python3",
            "/usr/local/bin/sage-python",
            "/opt/sage/bin/python",
            "/usr/local/bin/node",
            "/usr/local/bin/sage",
            "/usr/share/ghidra/support/analyzeHeadless",
            "/usr/share/john/password.lst",
        ):
            with self.subTest(path=path):
                self.assertIn(path, summary)


if __name__ == "__main__":
    unittest.main()
