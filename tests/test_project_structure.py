from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from tests.fixture_cleanup import cleanup_temporary_directory

from tools.project_structure.projections import write_projections
from tools.project_structure.scanner import scan_project
from tools.project_structure.validator import validate_snapshot


SKLT_TEMPLATE = """id:
    {id}
object_id:
    {object_id}
kind:
    component
responsibility:
    {responsibility}
inputs:
    Input
outputs:
    Output
depends_on:
    dependency
effects:
    state
state:
    {state}
"""


class ProjectStructureTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)

    def tearDown(self) -> None:
        cleanup_temporary_directory(self.temp_dir)

    def write_sklt(self, module: str, name: str, object_id: str, state: str = "SKLT", record_id: str | None = None) -> Path:
        path = self.root / module / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            SKLT_TEMPLATE.format(
                id=record_id or object_id,
                object_id=object_id,
                responsibility=f"manage {object_id}",
                state=state,
            ),
            encoding="utf-8",
        )
        return path

    def write_glif(self, module: str, name: str, object_id: str, elements: list[dict] | None = None) -> Path:
        path = self.root / module / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"object_id": object_id, "elements": elements or []}), encoding="utf-8")
        return path

    def test_valid_single_sklt_and_glif(self) -> None:
        self.write_sklt("renderer", "settings.sklt", "renderer.settings")
        self.write_glif("renderer", "settings.glif", "renderer.settings", [{"id": "settings.save"}])
        snapshot = scan_project(self.root)
        self.assertEqual(validate_snapshot(snapshot, include_projections=False), [])

    def test_exp_and_global_aggregation(self) -> None:
        self.write_sklt("renderer", "frame.sklt", "renderer.frame", "RARE")
        self.write_sklt("renderer", "settings.sklt", "renderer.settings", "SKLT")
        self.write_glif("renderer", "settings.glif", "renderer.settings")
        self.write_sklt("storage", "db.sklt", "storage.db", "RARE")

        snapshot = scan_project(self.root)
        self.assertEqual(write_projections(snapshot), [])
        self.assertEqual((self.root / "renderer" / ".exp").read_text(), "sklt 2\nrare 1\n")
        self.assertEqual((self.root / "storage" / ".exp").read_text(), "sklt 1\nrare 1\n")
        self.assertEqual((self.root / "docs" / ".grade").read_text(), "sklt 3\nrare 2\n")
        self.assertEqual(validate_snapshot(scan_project(self.root)), [])

    def test_manual_projection_corruption_is_repaired(self) -> None:
        self.write_sklt("renderer", "settings.sklt", "renderer.settings", "RARE")
        snapshot = scan_project(self.root)
        self.assertEqual(write_projections(snapshot), [])
        (self.root / "renderer" / ".exp").write_text("sklt 99\nrare 0\n", encoding="utf-8")
        self.assertTrue(validate_snapshot(scan_project(self.root)))
        self.assertEqual(write_projections(scan_project(self.root)), [])
        self.assertEqual(validate_snapshot(scan_project(self.root)), [])

    def test_unknown_glif_object_is_reported(self) -> None:
        self.write_sklt("renderer", "settings.sklt", "renderer.settings")
        self.write_glif("renderer", "unknown.glif", "renderer.unknown")
        issues = validate_snapshot(scan_project(self.root), include_projections=False)
        self.assertTrue(any(issue.code == "GLIF_WITHOUT_SKLT" for issue in issues))

    def test_duplicate_object_id_and_invalid_state_are_reported(self) -> None:
        self.write_sklt("renderer", "one.sklt", "renderer.settings")
        self.write_sklt("renderer", "two.sklt", "renderer.settings", "BROKEN")
        issues = validate_snapshot(scan_project(self.root), include_projections=False)
        codes = {issue.code for issue in issues}
        self.assertIn("DUPLICATE_OBJECT_ID", codes)
        self.assertIn("INVALID_STATE", codes)

    def test_duplicate_glif_element_id_is_reported(self) -> None:
        self.write_sklt("renderer", "settings.sklt", "renderer.settings")
        self.write_glif(
            "renderer",
            "settings.glif",
            "renderer.settings",
            [{"id": "save"}, {"id": "save"}],
        )
        issues = validate_snapshot(scan_project(self.root), include_projections=False)
        self.assertTrue(any(issue.code == "DUPLICATE_GLIF_ELEMENT_ID" for issue in issues))

    def test_exp_does_not_write_when_structure_is_invalid(self) -> None:
        self.write_sklt("renderer", "settings.sklt", "renderer.settings", "BROKEN")
        issues = write_projections(scan_project(self.root))
        self.assertTrue(any(issue.code == "INVALID_STATE" for issue in issues))
        self.assertFalse((self.root / "renderer" / ".exp").exists())


    def write_sklt_with_level(self, module: str, name: str, object_id: str, level: str) -> Path:
        path = self.write_sklt(module, name, object_id)
        text = path.read_text(encoding="utf-8")
        path.write_text(text + f"level:\n    {level}\n", encoding="utf-8")
        return path

    def write_sklt_with_axes(self, object_id: str, block: str) -> Path:
        path = self.write_sklt("storage", "axes.sklt", object_id)
        text = path.read_text(encoding="utf-8")
        path.write_text(text + block, encoding="utf-8")
        return path

    def test_valid_storage_axes_are_accepted(self) -> None:
        self.write_sklt_with_axes(
            "storage.cas",
            "storage_state:\n    ARCHIVED\nretention:\n    PERMANENT\nflags:\n    - PINNED\n    - REPLICATED\n",
        )
        self.assertEqual(validate_snapshot(scan_project(self.root), include_projections=False), [])

    def test_invalid_storage_state_is_reported(self) -> None:
        self.write_sklt_with_axes("storage.cas", "storage_state:\n    SYNCED\n")
        issues = validate_snapshot(scan_project(self.root), include_projections=False)
        self.assertTrue(any(issue.code == "INVALID_STORAGE_STATE" for issue in issues))

    def test_invalid_retention_class_is_reported(self) -> None:
        self.write_sklt_with_axes("storage.cas", "retention:\n    TEMPORARY\n")
        issues = validate_snapshot(scan_project(self.root), include_projections=False)
        self.assertTrue(any(issue.code == "INVALID_RETENTION_CLASS" for issue in issues))

    def test_invalid_storage_flag_is_reported(self) -> None:
        self.write_sklt_with_axes("storage.cas", "flags:\n    - IMPORTANT\n")
        issues = validate_snapshot(scan_project(self.root), include_projections=False)
        self.assertTrue(any(issue.code == "INVALID_STORAGE_FLAG" for issue in issues))

    def test_invalid_isolation_level_is_reported(self) -> None:
        self.write_sklt_with_axes("scoping.git_scope", "isolation:\n    TIGHT\n")
        issues = validate_snapshot(scan_project(self.root), include_projections=False)
        self.assertTrue(any(issue.code == "INVALID_ISOLATION_LEVEL" for issue in issues))

    def test_missing_storage_state_defaults_to_live(self) -> None:
        self.write_sklt("storage", "cas.sklt", "storage.cas")
        snapshot = scan_project(self.root)
        self.assertEqual([record.storage_state for record in snapshot.sklts], ["LIVE"])
        self.assertEqual(validate_snapshot(snapshot, include_projections=False), [])

    def test_known_level_is_accepted(self) -> None:
        self.write_sklt_with_level("storage", "cas.sklt", "storage.cas", "L0")
        self.assertEqual(validate_snapshot(scan_project(self.root), include_projections=False), [])

    def test_unknown_level_is_reported(self) -> None:
        self.write_sklt_with_level("storage", "cas.sklt", "storage.cas", "L9")
        issues = validate_snapshot(scan_project(self.root), include_projections=False)
        self.assertTrue(any(issue.code == "UNKNOWN_LEVEL" for issue in issues))

    def test_missing_level_stays_valid(self) -> None:
        self.write_sklt("renderer", "settings.sklt", "renderer.settings")
        issues = validate_snapshot(scan_project(self.root), include_projections=False)
        self.assertFalse(any(issue.code == "UNKNOWN_LEVEL" for issue in issues))

if __name__ == "__main__":
    unittest.main()
