from __future__ import annotations

import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path

from tools.project_structure.cli import main
from tools.project_structure.v03_dependencies import normalize_dependencies
from tools.project_structure.v03_index import load_object_index
from tools.project_structure.v03_manifest import load_manifest
from tools.project_structure.v03_scan import scan_project_v03
from tools.project_structure.v03_validation import validate_v03


SKLT = """schema_version:
    1
id:
    renderer.settings
object_id:
    renderer.settings
kind:
    component
responsibility:
    UI execution boundary fixture
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
{execution}"""


class ToolkitIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.old_toolkit_root = os.environ.get("RUST_MAKEPAD_TOOLKIT_ROOT")

    def tearDown(self) -> None:
        if self.old_toolkit_root is None:
            os.environ.pop("RUST_MAKEPAD_TOOLKIT_ROOT", None)
        else:
            os.environ["RUST_MAKEPAD_TOOLKIT_ROOT"] = self.old_toolkit_root
        from tests.fixture_cleanup import cleanup_temporary_directory
        cleanup_temporary_directory(self.temp_dir)

    def write_manifest(self) -> None:
        (self.root / ".project").mkdir(parents=True, exist_ok=True)
        (self.root / ".project" / "project.json").write_text(json.dumps({
            "schema_version": 1,
            "project_id": "toolkit.fixture",
            "modules": [],
            "reserved_dirs": ["docs", "libs", "snapshots", "tools", "tests", ".project"],
            "ignore": [],
        }), encoding="utf-8")

    def write_docs(self) -> None:
        docs = self.root / "docs"
        (docs / "cronica").mkdir(parents=True, exist_ok=True)
        for name in ("project.md", "map.md", "modules.md", "architecture.md", "agent-guides.md"):
            (docs / name).write_text("# doc\n", encoding="utf-8")
        (docs / "cronica" / "README.md").write_text("# Cronica\n", encoding="utf-8")
        (self.root / "libs").mkdir(parents=True, exist_ok=True)
        (self.root / "libs" / "README.md").write_text("# Libraries\n", encoding="utf-8")
        (self.root / "snapshots").mkdir(parents=True, exist_ok=True)
        (self.root / "snapshots" / "README.md").write_text("# Snapshots\n", encoding="utf-8")

    def write_sklt(self, execution: str = "") -> None:
        path = self.root / "renderer" / "settings.sklt"
        path.parent.mkdir(parents=True, exist_ok=True)
        block = f"execution:\n    {execution}\n" if execution else ""
        path.write_text(SKLT.format(execution=block), encoding="utf-8")

    def prepare(self, execution: str = "") -> None:
        self.write_manifest()
        self.write_docs()
        self.write_sklt(execution)

    def run_cli(self, *args: str) -> tuple[int, str]:
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = main([*args, "--root", str(self.root)])
        return code, output.getvalue()

    def configure_catalog(self, keys: list[str]) -> None:
        toolkit = self.root / "fake-toolkit"
        catalog = toolkit / "prototypes" / "ai-ui-test-tool-auto-probe" / "scheduler" / "container" / "environment-catalog.json"
        catalog.parent.mkdir(parents=True, exist_ok=True)
        (toolkit / "docs").mkdir(parents=True, exist_ok=True)
        (toolkit / "docs" / "INDEX.md").write_text(
            "[environment catalog](../prototypes/ai-ui-test-tool-auto-probe/scheduler/container/environment-catalog.json)\n",
            encoding="utf-8",
        )
        catalog.write_text(json.dumps({
            "version": 1,
            "environments": [{"version": 1, "key": key, "kind": "container"} for key in keys],
        }), encoding="utf-8")
        os.environ["RUST_MAKEPAD_TOOLKIT_ROOT"] = str(toolkit)

    def materialize(self) -> None:
        self.assertEqual(self.run_cli("exp")[0], 0)
        self.assertEqual(self.run_cli("index")[0], 0)

    def test_container_ref_is_typed_in_object_index(self) -> None:
        self.configure_catalog(["docker-linux-level0"])
        self.prepare("provider: rust_makepad_toolkit kind: container id: docker-linux-level0 purpose: ai-ui-test")
        self.materialize()
        data, issues = load_object_index(self.root)
        self.assertEqual(issues, [])
        self.assertEqual(data["objects"]["renderer.settings"]["execution"], [{
            "type": "container",
            "provider": "rust_makepad_toolkit",
            "id": "docker-linux-level0",
            "purpose": "ai-ui-test",
        }])

    def test_unknown_container_key_is_error(self) -> None:
        self.configure_catalog(["docker-linux-level0"])
        self.prepare("provider: rust_makepad_toolkit kind: container id: missing-level0")
        code, output = self.run_cli("validate", "--json")
        payload = json.loads(output)
        self.assertEqual(code, 1)
        self.assertEqual(payload["status"], "FAIL")
        self.assertTrue(any(issue["code"] == "UNKNOWN_TOOLKIT_CONTAINER" for issue in payload["issues"]))

    def test_unresolved_external_provider_is_warning(self) -> None:
        self.prepare("provider: another_toolkit kind: container id: unknown-runtime")
        snapshot, manifest, _ = scan_project_v03(self.root)
        manifest, _ = load_manifest(self.root)
        issues = validate_v03(snapshot, manifest)
        self.assertTrue(any(issue.code == "UNRESOLVED_TOOLKIT_REF" for issue in issues))
        self.assertFalse(any(issue.code == "INVALID_TOOLKIT_REF" for issue in issues))

    def test_typed_container_dependency_is_preserved(self) -> None:
        self.prepare()
        path = self.root / "renderer" / "settings.sklt"
        path.write_text(path.read_text().replace("depends_on:\n    dependency", "depends_on:\n    container:docker-linux-level0"), encoding="utf-8")
        snapshot, _, _ = scan_project_v03(self.root)
        dependencies, issues = normalize_dependencies(snapshot, set())
        self.assertFalse(any(issue.code == "INVALID_TOOLKIT_REF" for issue in issues))
        self.assertEqual(dependencies["renderer.settings"][0].type, "container")
        self.assertEqual(dependencies["renderer.settings"][0].id, "docker-linux-level0")


if __name__ == "__main__":
    unittest.main()
