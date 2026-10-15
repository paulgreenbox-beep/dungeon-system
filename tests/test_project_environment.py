from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from tools.project_structure.cli import main
from tools.project_structure.libraries import discover_libraries, validate_library_refs
from tools.project_structure.project_validation import validate_project
from tools.project_structure.projections import write_projections
from tools.project_structure.scanner import scan_project
from tools.project_structure.snapshots import create_snapshot, validate_snapshots
from tests.fixture_cleanup import cleanup_temporary_directory


SKLT_TEMPLATE = (
    "id:\n    {object_id}\nobject_id:\n    {object_id}\nkind:\n    component\n"
    "responsibility:\n    test object\ninputs:\n    Input\noutputs:\n    Output\n"
    "depends_on:\n    dependency\neffects:\n    state\nstate:\n    {state}\n{optional}\n"
)


class ProjectEnvironmentTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)

    def tearDown(self) -> None:
        cleanup_temporary_directory(self.temp_dir)

    def write_sklt(self, module: str = "renderer", object_id: str = "renderer.settings", state: str = "RARE", optional: str = "") -> Path:
        path = self.root / module / f"{object_id.rsplit('.', 1)[-1]}.sklt"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(SKLT_TEMPLATE.format(object_id=object_id, state=state, optional=optional), encoding="utf-8")
        return path

    def write_project_docs(self) -> None:
        docs = self.root / "docs"
        (docs / "cronica").mkdir(parents=True, exist_ok=True)
        for name in ("project.md", "map.md", "modules.md", "architecture.md", "agent-guides.md"):
            (docs / name).write_text(f"# {name}\n", encoding="utf-8")
        (docs / "cronica" / "README.md").write_text("# Cronica\n", encoding="utf-8")
        (self.root / "libs").mkdir(parents=True, exist_ok=True)
        (self.root / "libs" / "README.md").write_text("# Libraries\n", encoding="utf-8")
        (self.root / "snapshots").mkdir(parents=True, exist_ok=True)
        (self.root / "snapshots" / "README.md").write_text("# Snapshots\n", encoding="utf-8")

    def test_reserved_directories_are_not_modules_or_exp_units(self) -> None:
        self.write_sklt()
        self.write_sklt("libs", "libs.fake", "SKLT")
        self.write_sklt("snapshots/old", "snapshots.fake", "RARE")
        snapshot = scan_project(self.root)
        self.assertEqual([module.name for module in snapshot.modules], ["renderer"])
        self.assertEqual(write_projections(snapshot), [])
        self.assertEqual((self.root / "docs" / ".grade").read_text(), "sklt 1\nrare 1\n")

    def test_valid_library_descriptor(self) -> None:
        descriptor = self.root / "libs" / "example" / "lib.json"
        descriptor.parent.mkdir(parents=True, exist_ok=True)
        descriptor.write_text(json.dumps({
            "id": "example.library",
            "version": "1.0.0",
            "source": "external",
            "purpose": "test",
            "license": "unknown",
            "used_by": [],
        }), encoding="utf-8")
        registry = discover_libraries(self.root)
        self.assertEqual(registry.issues, [])
        self.assertIn("example.library", registry.by_id)

    def test_duplicate_library_id_is_reported(self) -> None:
        payload = {"id": "same.library", "version": "1", "source": "external", "purpose": "test", "license": "unknown", "used_by": []}
        for name in ("one", "two"):
            path = self.root / "libs" / name / "lib.json"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(payload), encoding="utf-8")
        registry = discover_libraries(self.root)
        self.assertTrue(any(issue.code == "DUPLICATE_LIBRARY_ID" for issue in registry.issues))

    def test_unknown_library_reference_is_reported(self) -> None:
        self.write_sklt(optional="libraries:\n    missing.library")
        registry = discover_libraries(self.root)
        issues = validate_library_refs(scan_project(self.root), registry)
        self.assertTrue(any(issue.code == "UNKNOWN_LIBRARY" for issue in issues))

    def test_snapshot_creation_and_current_exp_are_independent(self) -> None:
        self.write_project_docs()
        self.write_sklt()
        self.assertEqual(write_projections(scan_project(self.root)), [])
        current_exp = (self.root / "renderer" / ".exp").read_text()
        current_grade = (self.root / "docs" / ".grade").read_text()
        snapshot = scan_project(self.root)
        self.assertEqual(validate_project(snapshot), [])
        self.assertEqual(create_snapshot(snapshot, "checkpoint-1"), [])
        target = self.root / "snapshots" / "checkpoint-1"
        manifest = json.loads((target / "manifest.json").read_text())
        self.assertEqual(manifest["grade"], {"sklt": 1, "rare": 1})
        self.assertTrue((target / "grade").exists())
        self.assertEqual((self.root / "renderer" / ".exp").read_text(), current_exp)
        self.assertEqual((self.root / "docs" / ".grade").read_text(), current_grade)
        self.assertEqual(validate_snapshots(self.root), [])

    def test_invalid_snapshot_manifest_is_reported(self) -> None:
        path = self.root / "snapshots" / "broken" / "manifest.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{}", encoding="utf-8")
        issues = validate_snapshots(self.root)
        self.assertTrue(any(issue.code == "INVALID_SNAPSHOT_MANIFEST" for issue in issues))

    def test_existing_cli_commands_remain_available(self) -> None:
        self.write_sklt()
        self.assertEqual(main(["scan", "--root", str(self.root)]), 0)
        self.assertEqual(main(["exp", "--root", str(self.root)]), 0)
        self.assertEqual(main(["inspect", "--root", str(self.root), "renderer.settings"]), 0)


if __name__ == "__main__":
    unittest.main()
