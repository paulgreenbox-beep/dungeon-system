from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from tools.project_structure.cli import main
from tools.project_structure.libraries import discover_libraries


SKLT = (
    "id:\n    renderer.settings\nobject_id:\n    renderer.settings\nkind:\n    component\n"
    "responsibility:\n    test\ninputs:\n    Input\noutputs:\n    Output\n"
    "depends_on:\n    dependency\neffects:\n    state\nstate:\n    RARE\n"
)


class ProjectEnvironmentCommandTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)

    def tearDown(self) -> None:
        from tests.fixture_cleanup import cleanup_temporary_directory
        cleanup_temporary_directory(self.temp_dir)

    def prepare_project(self) -> None:
        (self.root / "renderer").mkdir(parents=True)
        (self.root / "renderer" / "settings.sklt").write_text(SKLT, encoding="utf-8")
        docs = self.root / "docs"
        (docs / "cronica").mkdir(parents=True)
        for name in ("project.md", "map.md", "modules.md", "architecture.md", "agent-guides.md"):
            (docs / name).write_text("# doc\n", encoding="utf-8")
        (docs / "cronica" / "README.md").write_text("# Cronica\n", encoding="utf-8")

    def test_invalid_library_descriptor_is_reported(self) -> None:
        path = self.root / "libs" / "broken" / "lib.json"
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({"id": "broken.library"}), encoding="utf-8")
        registry = discover_libraries(self.root)
        self.assertTrue(any(issue.code == "INVALID_LIBRARY_DESCRIPTOR" for issue in registry.issues))

    def test_snapshot_cli_command_creates_checkpoint(self) -> None:
        self.prepare_project()
        self.assertEqual(main(["exp", "--root", str(self.root)]), 0)
        self.assertEqual(main(["snapshot", "--root", str(self.root), "cli-checkpoint"]), 0)
        self.assertTrue((self.root / "snapshots" / "cli-checkpoint" / "manifest.json").exists())


if __name__ == "__main__":
    unittest.main()
