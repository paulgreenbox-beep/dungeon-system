"""Service-boundary tests: disposable structures, mock Scion, real Git MCP."""
from __future__ import annotations

import copy
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from tools.project_structure import vcs_service as service
from tests.fixture_cleanup import cleanup_temporary_directory


class VcsServiceTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="dungeon-vcs-service-")
        self.addCleanup(cleanup_temporary_directory, temporary)
        self.root = Path(temporary.name).resolve()
        (self.root / ".project").mkdir()
        self.manifest = {"schema_version": 1, "project_id": "fixture.service",
                         "git_scopes": [{"id": "main", "mode": "MONOREPO", "repository": "."}],
                         "version_scopes": [{"id": "experiment", "backend": "SCION", "root": "lab", "endpoint_profile": "LAB"}]}
        self.write_manifest()
        (self.root / "lab").mkdir()
        (self.root / "lab" / "source.py").write_text("value = 1\n", encoding="utf-8")
        (self.root / "lab" / "object.sklt").write_text(
            "id:\n    lab.object\nobject_id:\n    lab.object\nkind:\n    component\n"
            "responsibility:\n    test\ninputs:\n    input\noutputs:\n    output\n"
            "depends_on:\neffects:\n    state\nstate:\n    SKLT\nlevel:\n    L1\n"
            "git_scope:\n    experiment\nsource:\n    path: lab/source.py\n", encoding="utf-8")

    def write_manifest(self):
        (self.root / ".project" / "project.json").write_text(json.dumps(self.manifest), encoding="utf-8")

    def tree(self):
        return {p.relative_to(self.root).as_posix(): p.read_bytes()
                for p in self.root.rglob("*") if p.is_file()}

    def test_sparse_plan_uses_declared_source_and_scope_relative_prefix_only(self):
        before = self.tree()
        result = service.vcs_query(self.root, "sparse-plan", object_id="lab.object")
        self.assertEqual(result["status"], "PASS")
        self.assertEqual(result["prefixes"], ["source.py"])
        self.assertEqual(result["dependency_closure"], "NOT_COMPUTED")
        self.assertFalse(result["writes"])
        self.assertEqual(before, self.tree())

    def test_sparse_plan_rejects_source_outside_declared_scope(self):
        path = self.root / "lab" / "object.sklt"
        path.write_text(path.read_text(encoding="utf-8").replace("lab/source.py", "other/source.py"), encoding="utf-8")
        result = service.vcs_query(self.root, "sparse-plan", object_id="lab.object")
        self.assertEqual(result["status"], "FAIL")
        self.assertEqual(result["issues"][0]["code"], "SOURCE_OUTSIDE_VERSION_SCOPE")

    def test_migration_plan_lists_impacts_but_never_changes_history_or_files(self):
        (self.root / "snapshots" / "old").mkdir(parents=True)
        (self.root / "snapshots" / "old" / "manifest.json").write_text("{}", encoding="utf-8")
        (self.root / "evidence" / "manifests").mkdir(parents=True)
        (self.root / "evidence" / "manifests" / "EV-old.json").write_text("{}", encoding="utf-8")
        before = self.tree()
        result = service.vcs_query(self.root, "migration-plan", target_backend="GIT", scope_id="experiment")
        self.assertEqual(result["status"], "PASS")
        self.assertEqual(result["affected_objects"][0]["object_id"], "lab.object")
        self.assertEqual(result["affected_levels"], ["L1"])
        self.assertEqual(result["snapshot_refs_to_review"], ["old"])
        self.assertEqual(result["evidence_refs_to_review"], ["EV-old"])
        self.assertEqual(before, self.tree())

    def test_invalid_scope_and_boolean_limit_are_structured_issues(self):
        self.assertEqual(service.vcs_query(self.root, "history", limit=True)["status"], "FAIL")
        self.assertEqual(service.vcs_query(self.root, "status", scope_id="missing")["issues"][0]["code"], "UNKNOWN_VERSION_SCOPE")
        self.manifest["version_scopes"][0]["backend"] = "invented"
        self.write_manifest()
        self.assertEqual(service.vcs_query(self.root, "status")["issues"][0]["code"], "INVALID_VERSION_SCOPE")

    def test_root_alias_is_rejected_before_manifest_load(self):
        with patch.object(service, "_no_reparse", return_value=False), patch.object(service, "load_manifest") as load:
            self.assertEqual(service.vcs_query(self.root, "status")["status"], "FAIL")
            load.assert_not_called()

    def test_unavailable_scion_does_not_turn_git_result_into_failure(self):
        from tools.project_structure import vcs_backends
        class Backend:
            def __init__(self, scope):
                self.scope = scope
            def status(self):
                return {"status": "PASS" if self.scope.backend == "GIT" else "FAIL"}
        with patch.object(vcs_backends, "backend_for", side_effect=lambda root, scope: Backend(scope)):
            result = service.vcs_query(self.root, "status")
        self.assertEqual(result["status"], "PARTIAL")
        self.assertEqual(result["scopes"]["main"]["status"], "PASS")
        self.assertEqual(result["scopes"]["experiment"]["status"], "FAIL")

    def test_revision_guards_ignore_timestamp_but_unknown_is_not_current(self):
        record = {"schema_version": 1, "backend": "SCION", "scope_id": "experiment",
                  "revision": "c1" + "a" * 64, "ref": "main", "dirty_state": "UNKNOWN",
                  "availability": "AVAILABLE", "observed_at": "first"}
        before = {"schema_version": 1, "scopes": {"experiment": record}}
        after = copy.deepcopy(before)
        after["scopes"]["experiment"]["observed_at"] = "second"
        self.assertTrue(service.provenance_equivalent(before, after))
        self.assertFalse(service.provenance_matches(before, after))
        after["scopes"]["experiment"]["backend"] = "GIT"
        self.assertTrue(service.provenance_errors(after))

    @unittest.skipUnless(shutil.which("git"), "real Git unavailable")
    def test_actual_stdio_mcp_vcs_status_uses_real_git_without_writable_root(self):
        def git(*args):
            return subprocess.run(["git", "-C", str(self.root), *args], check=True,
                                  capture_output=True, timeout=10)
        git("init", "-b", "main")
        git("-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "commit", "--allow-empty", "-m", "fixture")
        server = Path(__file__).resolve().parents[1] / "tools/codex-plugin/dungeon-system/mcp/server.py"
        env = dict(os.environ, DUNGEON_PROJECT_ROOT=str(self.root), DUNGEON_WRITABLE_ROOTS="[]")
        requests = [{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2024-11-05", "capabilities": {}, "clientInfo": {"name": "fixture", "version": "1"}}},
                    {"jsonrpc": "2.0", "method": "notifications/initialized"},
                    {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "dungeon_vcs_status", "arguments": {"project_root": str(self.root), "scope_id": "main"}}}]
        before = self.tree()
        run = subprocess.run([sys.executable, "-B", str(server)], input="".join(json.dumps(row) + "\n" for row in requests),
                             capture_output=True, text=True, encoding="utf-8", env=env, timeout=20)
        self.assertEqual(run.returncode, 0, run.stderr)
        replies = {row["id"]: row for row in map(json.loads, run.stdout.splitlines())}
        payload = replies[2]["result"]["structuredContent"]
        self.assertEqual(payload["status"], "PASS", payload)
        self.assertEqual(payload["scopes"]["main"]["source_revision"]["backend"], "GIT")
        self.assertEqual(before, self.tree())


if __name__ == "__main__":
    unittest.main()
