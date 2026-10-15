from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from tools.project_structure.cli import main
from tools.project_structure.projections import write_projections
from tools.project_structure.scanner import scan_project
from tools.project_structure.v03_index import load_object_index
from tools.project_structure.v03_manifest import load_manifest
from tools.project_structure.v03_scan import scan_project_v03
from tools.project_structure.v03_snapshots import create_snapshot_v03, snapshot_diff
from tools.project_structure.v03_validation import projection_statuses, status_data, validate_v03


SKLT = (
    "schema_version:\n    1\nid:\n    {object_id}\nobject_id:\n    {object_id}\n"
    "kind:\n    component\nresponsibility:\n    test component\ninputs:\n    Input\n"
    "outputs:\n    Output\ndepends_on:\n    dependency\neffects:\n    state\nstate:\n    {state}\n{source}{level}{storage}{retention}{isolation}"
)


class V03Tests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)

    def tearDown(self) -> None:
        from tests.fixture_cleanup import cleanup_temporary_directory
        cleanup_temporary_directory(self.temp_dir)

    def write_manifest(self, modules: list[dict] | None = None, ignore: list[str] | None = None, git_scopes: list[dict] | None = None) -> None:
        path = self.root / ".project" / "project.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "schema_version": 1,
            "project_id": "fixture.project",
            "modules": modules or [],
            "reserved_dirs": ["docs", "libs", "snapshots", "tools", "tests", ".project"],
            "ignore": ignore or [],
            "docs": {"project": "docs/project.md", "map": "docs/map.md", "architecture": "docs/architecture.md", "cronica": "docs/cronica"},
        }
        if git_scopes is not None:
            data["git_scopes"] = git_scopes
        path.write_text(json.dumps(data), encoding="utf-8")

    def write_sklt(self, module: str = "renderer", object_id: str = "renderer.settings", state: str = "SKLT", source: str = "", level: str = "", storage_state: str = "", retention: str = "", isolation: str = "") -> Path:
        path = self.root / module / f"{object_id.rsplit('.', 1)[-1]}.sklt"
        path.parent.mkdir(parents=True, exist_ok=True)
        source_block = f"source:\n    path: {source}\n    symbol: RendererSettings\n" if source else ""
        level_block = f"level:\n    {level}\n" if level else ""
        storage_block = f"storage_state:\n    {storage_state}\n" if storage_state else ""
        retention_block = f"retention:\n    {retention}\n" if retention else ""
        isolation_block = f"isolation:\n    {isolation}\n" if isolation else ""
        path.write_text(SKLT.format(
            object_id=object_id,
            state=state,
            source=source_block,
            level=level_block,
            storage=storage_block,
            retention=retention_block,
            isolation=isolation_block,
        ), encoding="utf-8")
        return path

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

    def prepare(self, object_id: str = "renderer.settings", state: str = "SKLT", source: str = "") -> None:
        self.write_manifest()
        self.write_docs()
        self.write_sklt(object_id=object_id, state=state, source=source)

    def run_cli(self, *args: str) -> tuple[int, str]:
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = main([*args, "--root", str(self.root)])
        return code, output.getvalue()

    def materialize(self) -> None:
        self.assertEqual(self.run_cli("exp")[0], 0)
        self.assertEqual(self.run_cli("index")[0], 0)

    def test_project_manifest_parsing(self) -> None:
        self.write_manifest()
        manifest, issues = load_manifest(self.root)
        self.assertEqual(issues, [])
        self.assertTrue(manifest.exists)
        self.assertEqual(manifest.project_id, "fixture.project")

    def test_explicit_module_registry_has_priority(self) -> None:
        self.write_manifest([{"id": "rendering", "path": "renderer"}])
        self.write_sklt("renderer", "renderer.settings")
        self.write_sklt("storage", "storage.db")
        snapshot, _, _ = scan_project_v03(self.root)
        self.assertEqual([module.name for module in snapshot.modules], ["rendering"])

    def test_ignore_rules_exclude_structural_files(self) -> None:
        self.write_manifest(ignore=["generated"])
        self.write_sklt("renderer", "renderer.settings")
        self.write_sklt("generated", "generated.object")
        snapshot, _, _ = scan_project_v03(self.root)
        self.assertEqual([module.name for module in snapshot.modules], ["renderer"])

    def test_schema_version_validation(self) -> None:
        self.prepare()
        path = self.root / "renderer" / "settings.sklt"
        path.write_text(path.read_text().replace("schema_version:\n    1", "schema_version:\n    2"), encoding="utf-8")
        snapshot, manifest, _ = scan_project_v03(self.root)
        issues = validate_v03(snapshot, manifest)
        self.assertTrue(any(issue.code == "UNSUPPORTED_SCHEMA_VERSION" for issue in issues))

    def test_unsupported_manifest_schema_version(self) -> None:
        self.write_manifest()
        path = self.root / ".project" / "project.json"
        path.write_text(path.read_text().replace('"schema_version": 1', '"schema_version": 99'), encoding="utf-8")
        _, issues = load_manifest(self.root)
        self.assertTrue(any(issue.code == "UNSUPPORTED_SCHEMA_VERSION" for issue in issues))

    def test_object_index_generation(self) -> None:
        self.prepare()
        self.materialize()
        data, issues = load_object_index(self.root)
        self.assertEqual(issues, [])
        self.assertIn("renderer.settings", data["objects"])

    def test_level_is_validated_and_indexed(self) -> None:
        self.write_manifest()
        self.write_docs()
        self.write_sklt(level="L2")
        snapshot, manifest, _ = scan_project_v03(self.root)
        self.assertFalse(any(issue.code == "UNKNOWN_LEVEL" for issue in validate_v03(snapshot, manifest)))
        self.assertEqual(self.run_cli("index")[0], 0)
        data, _ = load_object_index(self.root)
        self.assertEqual(data["objects"]["renderer.settings"]["level"], "L2")

    def test_unknown_level_is_reported_by_v03(self) -> None:
        self.write_manifest()
        self.write_docs()
        self.write_sklt(level="L7")
        snapshot, manifest, _ = scan_project_v03(self.root)
        self.assertTrue(any(issue.code == "UNKNOWN_LEVEL" for issue in validate_v03(snapshot, manifest)))

    def test_git_scopes_are_parsed(self) -> None:
        self.write_manifest(git_scopes=[
            {"id": "main", "mode": "MONOREPO", "repository": "."},
            {"id": "compiler", "mode": "REPOSITORY", "repository": "../compiler"},
        ])
        manifest, issues = load_manifest(self.root)
        self.assertEqual(issues, [])
        self.assertEqual([scope.scope_id for scope in manifest.git_scopes], ["main", "compiler"])
        self.assertEqual(manifest.known_git_scopes(), {"main", "compiler"})

    def test_invalid_git_scope_mode_is_reported(self) -> None:
        self.write_manifest(git_scopes=[{"id": "main", "mode": "FOLDER", "repository": "."}])
        _, issues = load_manifest(self.root)
        self.assertTrue(any(issue.code == "INVALID_GIT_SCOPE_MODE" for issue in issues))

    def test_duplicate_git_scope_is_reported(self) -> None:
        self.write_manifest(git_scopes=[
            {"id": "main", "mode": "MONOREPO", "repository": "."},
            {"id": "main", "mode": "REPOSITORY", "repository": "../other"},
        ])
        _, issues = load_manifest(self.root)
        self.assertTrue(any(issue.code == "DUPLICATE_GIT_SCOPE" for issue in issues))

    def test_legacy_project_defaults_to_main_scope(self) -> None:
        self.write_manifest()
        manifest, issues = load_manifest(self.root)
        self.assertEqual(issues, [])
        self.assertEqual(manifest.git_scopes, [])
        self.assertEqual(manifest.known_git_scopes(), {"main"})

    def test_unknown_git_scope_on_object_is_reported(self) -> None:
        self.write_manifest(git_scopes=[{"id": "main", "mode": "MONOREPO", "repository": "."}])
        self.write_docs()
        path = self.write_sklt()
        path.write_text(path.read_text(encoding="utf-8") + "git_scope:\n    compiler\n", encoding="utf-8")
        snapshot, manifest, _ = scan_project_v03(self.root)
        self.assertTrue(any(issue.code == "UNKNOWN_GIT_SCOPE" for issue in validate_v03(snapshot, manifest)))

    def test_declared_git_scope_on_object_is_accepted(self) -> None:
        self.write_manifest(git_scopes=[{"id": "compiler", "mode": "REPOSITORY", "repository": "../compiler"}])
        self.write_docs()
        path = self.write_sklt()
        path.write_text(path.read_text(encoding="utf-8") + "git_scope:\n    compiler\n", encoding="utf-8")
        snapshot, manifest, _ = scan_project_v03(self.root)
        self.assertFalse(any(issue.code == "UNKNOWN_GIT_SCOPE" for issue in validate_v03(snapshot, manifest)))
        self.assertEqual(self.run_cli("index")[0], 0)
        data, _ = load_object_index(self.root)
        entry = data["objects"]["renderer.settings"]
        self.assertEqual(entry["git_scope"], "compiler")
        self.assertEqual(entry["storage_state"], "LIVE")

    def test_status_reports_git_scopes_and_storage_axes(self) -> None:
        self.write_manifest(git_scopes=[{"id": "main", "mode": "MONOREPO", "repository": "."}])
        self.write_docs()
        self.write_sklt(level="L1", storage_state="PACKED", retention="PERMANENT", isolation="BOUNDED")
        self.materialize()
        code, output = self.run_cli("status", "--json")
        payload = json.loads(output)
        self.assertEqual(code, 0)
        self.assertEqual(payload["git_scopes"]["effective"], ["main"])
        self.assertEqual(payload["storage_axes"]["storage_state"], {"PACKED": 1})
        self.assertEqual(payload["storage_axes"]["retention"], ["PERMANENT"])
        self.assertEqual(payload["storage_axes"]["isolation"], {"BOUNDED": 1})
        self.assertEqual(payload["storage_axes"]["structural"], {"SKLT": 1, "RARE": 0})

    def test_inspect_uses_index(self) -> None:
        self.prepare()
        self.materialize()
        code, output = self.run_cli("inspect", "renderer.settings", "--json")
        payload = json.loads(output)
        self.assertEqual(code, 0)
        self.assertEqual(payload["object_id"], "renderer.settings")
        self.assertFalse(payload["index_rebuilt"])

    def test_stale_index_is_detected(self) -> None:
        self.prepare()
        self.materialize()
        self.write_sklt(state="RARE")
        code, output = self.run_cli("status", "--json")
        payload = json.loads(output)
        self.assertEqual(code, 0)
        self.assertEqual(payload["projections"]["objects_index"], "STALE")

    def test_source_refs_are_indexed(self) -> None:
        self.prepare(source="src/renderer/settings.rs")
        source = self.root / "src" / "renderer" / "settings.rs"
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_text("struct RendererSettings;\n", encoding="utf-8")
        self.materialize()
        data, _ = load_object_index(self.root)
        self.assertEqual(data["objects"]["renderer.settings"]["source"][0]["path"], "src/renderer/settings.rs")

    def test_missing_source_path_is_error(self) -> None:
        self.prepare(source="src/missing.rs")
        snapshot, manifest, _ = scan_project_v03(self.root)
        issues = validate_v03(snapshot, manifest)
        self.assertTrue(any(issue.code == "SOURCE_PATH_MISSING" for issue in issues))

    def test_status_human_output(self) -> None:
        self.prepare()
        self.materialize()
        code, output = self.run_cli("status")
        self.assertEqual(code, 0)
        self.assertIn("Dungeon Status", output)
        self.assertIn("Object index: CURRENT", output)

    def test_status_json_output(self) -> None:
        self.prepare()
        self.materialize()
        code, output = self.run_cli("status", "--json")
        payload = json.loads(output)
        self.assertEqual(code, 0)
        self.assertIn("validation", payload)
        self.assertIn("projections", payload)

    def test_validation_severity_keeps_stale_as_warning(self) -> None:
        self.prepare()
        self.materialize()
        self.write_sklt(state="RARE")
        code, output = self.run_cli("validate", "--json")
        payload = json.loads(output)
        self.assertEqual(code, 0)
        self.assertEqual(payload["status"], "PASS")
        self.assertTrue(any(issue["severity"] == "WARNING" for issue in payload["issues"]))

    def test_stale_exp_is_reported(self) -> None:
        self.prepare()
        self.materialize()
        (self.root / "renderer" / ".exp").write_text("sklt 99\nrare 0\n", encoding="utf-8")
        snapshot, manifest, _ = scan_project_v03(self.root)
        statuses, issues = projection_statuses(snapshot, manifest)
        self.assertEqual(statuses["exp"], "STALE")
        self.assertTrue(any(issue.code == "STALE_EXP" for issue in issues))

    def test_stale_grade_is_reported(self) -> None:
        self.prepare()
        self.materialize()
        (self.root / "docs" / ".grade").write_text("sklt 99\nrare 0\n", encoding="utf-8")
        snapshot, manifest, _ = scan_project_v03(self.root)
        statuses, issues = projection_statuses(snapshot, manifest)
        self.assertEqual(statuses["grade"], "STALE")
        self.assertTrue(any(issue.code == "STALE_GRADE" for issue in issues))

    def _make_snapshot(self, name: str) -> None:
        snapshot, manifest, _ = scan_project_v03(self.root)
        libraries = __import__("tools.project_structure.libraries", fromlist=["discover_libraries"]).discover_libraries(self.root)
        self.assertEqual(create_snapshot_v03(snapshot, name, manifest, libraries), [])

    def test_snapshot_diff_added_object(self) -> None:
        self.prepare()
        self.materialize()
        self._make_snapshot("before")
        self.write_sklt("renderer", "renderer.extra")
        self.materialize()
        self._make_snapshot("after")
        diff, issues = snapshot_diff(self.root, "before", "after")
        self.assertEqual(issues, [])
        self.assertIn("renderer.extra", diff["added"])

    def test_snapshot_diff_removed_object(self) -> None:
        self.prepare()
        self.write_sklt("renderer", "renderer.extra")
        self.materialize()
        self._make_snapshot("before")
        (self.root / "renderer" / "extra.sklt").unlink()
        self.materialize()
        self._make_snapshot("after")
        diff, _ = snapshot_diff(self.root, "before", "after")
        self.assertIn("renderer.extra", diff["removed"])

    def test_snapshot_diff_state_change(self) -> None:
        self.prepare(state="SKLT")
        self.materialize()
        self._make_snapshot("before")
        path = self.root / "renderer" / "settings.sklt"
        path.write_text(path.read_text().replace("state:\n    SKLT", "state:\n    RARE"), encoding="utf-8")
        self.materialize()
        self._make_snapshot("after")
        diff, _ = snapshot_diff(self.root, "before", "after")
        changed = {item["object_id"]: item for item in diff["changed"]}
        self.assertEqual(changed["renderer.settings"]["changes"]["state"], {"before": "SKLT", "after": "RARE"})

    def test_latest_snapshot_currentness(self) -> None:
        self.prepare()
        self.materialize()
        self._make_snapshot("before")
        snapshot, manifest, _ = scan_project_v03(self.root)
        data, issues = status_data(snapshot, manifest)
        self.assertEqual(issues, [])
        self.assertEqual(data["latest_snapshot"]["state"], "CURRENT")
        self.write_sklt(state="RARE")
        snapshot, manifest, _ = scan_project_v03(self.root)
        data, _ = status_data(snapshot, manifest)
        self.assertEqual(data["latest_snapshot"]["state"], "PROJECT_CHANGED")

    def test_existing_exp_behavior_remains_correct(self) -> None:
        self.prepare(state="RARE")
        code, _ = self.run_cli("exp")
        self.assertEqual(code, 0)
        self.assertEqual((self.root / "renderer" / ".exp").read_text(), "sklt 1\nrare 1\n")
        self.assertEqual((self.root / "docs" / ".grade").read_text(), "sklt 1\nrare 1\n")

    def test_end_to_end_v03_flow(self) -> None:
        self.prepare(state="SKLT")
        self.materialize()
        self._make_snapshot("before")
        path = self.root / "renderer" / "settings.sklt"
        path.write_text(path.read_text().replace("state:\n    SKLT", "state:\n    RARE"), encoding="utf-8")
        status_code, status_output = self.run_cli("status", "--json")
        self.assertEqual(status_code, 0)
        self.assertEqual(json.loads(status_output)["projections"]["exp"], "STALE")
        self.assertEqual(self.run_cli("exp")[0], 0)
        self.assertEqual(self.run_cli("index")[0], 0)
        self._make_snapshot("after")
        diff, issues = snapshot_diff(self.root, "before", "after")
        self.assertEqual(issues, [])
        self.assertEqual(diff["changed"][0]["changes"]["state"]["after"], "RARE")
        self.assertEqual(self.run_cli("validate")[0], 0)


if __name__ == "__main__":
    unittest.main()
