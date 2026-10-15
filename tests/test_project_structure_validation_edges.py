from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from tools.project_structure.scanner import scan_project
from tools.project_structure.validator import validate_snapshot


SKLT = """id:
    {object_id}
object_id:
    {object_id}
kind:
    component
responsibility:
    test object
inputs:
    Input
outputs:
    Output
depends_on:
    dependency
effects:
    state
state:
    SKLT
"""


class ValidationEdgeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)

    def tearDown(self) -> None:
        from tests.fixture_cleanup import cleanup_temporary_directory
        cleanup_temporary_directory(self.temp_dir)

    def write_sklt(self) -> Path:
        path = self.root / "renderer" / "settings.sklt"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(SKLT.format(object_id="renderer.settings"), encoding="utf-8")
        return path

    def test_invalid_sklt_syntax_is_reported(self) -> None:
        path = self.root / "renderer" / "broken.sklt"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("id renderer.settings\n", encoding="utf-8")
        issues = scan_project(self.root).issues
        self.assertTrue(any(issue.code == "INVALID_SKLT_SYNTAX" for issue in issues))

    def test_missing_glif_object_id_is_reported(self) -> None:
        self.write_sklt()
        path = self.root / "renderer" / "settings.glif"
        path.write_text(json.dumps({"elements": []}), encoding="utf-8")
        issues = validate_snapshot(scan_project(self.root), include_projections=False)
        self.assertTrue(any(issue.code == "MISSING_OBJECT_ID" for issue in issues))

    def test_explicit_broken_sklt_glif_reference_is_reported(self) -> None:
        path = self.write_sklt()
        path.write_text(path.read_text(encoding="utf-8") + "glif:\n    missing.glif\n", encoding="utf-8")
        issues = validate_snapshot(scan_project(self.root), include_projections=False)
        self.assertTrue(any(issue.code == "SKLT_WITH_BROKEN_GLIF_REF" for issue in issues))

    def test_invalid_exp_projection_is_reported(self) -> None:
        self.write_sklt()
        module = self.root / "renderer"
        (module / ".exp").write_text("rare nope\n", encoding="utf-8")
        (self.root / "docs").mkdir(parents=True, exist_ok=True)
        (self.root / "docs" / ".grade").write_text("sklt 1\nrare 0\n", encoding="utf-8")
        issues = validate_snapshot(scan_project(self.root))
        self.assertTrue(any(issue.code == "INVALID_EXP" for issue in issues))


if __name__ == "__main__":
    unittest.main()
