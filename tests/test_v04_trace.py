from __future__ import annotations

import copy
import hashlib
import json
import os
import shutil
import stat
import subprocess
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from tools.project_structure import v04_trace as api
from tools.project_structure.v03_cas import cas_object_path, cas_put
from tools.project_structure.v03_evidence import create_evidence, file_sha256, manifest_path, seal_evidence, verify_evidence


class TraceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        from tests.fixture_cleanup import cleanup_temporary_directory
        self.addCleanup(cleanup_temporary_directory, self.temporary)
        self.parent = Path(self.temporary.name)
        self.root = self.parent / "project"
        self.root.mkdir()
        (self.root / "src").mkdir()
        (self.root / "src/render.py").write_text("def render(): return 42\n", encoding="utf-8")
        (self.root / "tests").mkdir()
        (self.root / "tests/test_render.py").write_text("assert render() == 42\n", encoding="utf-8")
        (self.root / "renderer").mkdir()
        (self.root / "renderer/render.sklt").write_text(
            "schema_version:\n    1\nid:\n    renderer.render\nobject_id:\n    renderer.render\n"
            "kind:\n    component\nresponsibility:\n    Render fixture\ninputs:\n    scene\noutputs:\n    pixels\n"
            "depends_on:\neffects:\n    pixels\nstate:\n    SKLT\nsource:\n    path: src/render.py\n", encoding="utf-8")
        self.catalog = {"objects": {"renderer.render": {"object_id": "renderer.render", "identity_origin": "EXPLICIT", "module": "renderer", "sklt": "renderer/render.sklt", "state": "SKLT", "dependencies": [], "source": [{"path": "src/render.py"}], "glif": []}}, "modules": [], "issues": []}
        self.catalog_patch = patch.object(api, "_project_catalog", side_effect=lambda root: copy.deepcopy(self.catalog))
        self.catalog_patch.start()
        self.addCleanup(self.catalog_patch.stop)
        self.trace = {"task_id": "RTX-0042", "object_ids": ["renderer.render"], "scope": "PROTOTYPE", "assigned_agent": "external://Tech_agent", "source_refs": [{"path": "src/render.py"}], "test_refs": [{"path": "tests/test_render.py"}], "required_checks": ["smoke"], "performed_checks": [], "evidence_refs": [], "status": "IN_PROGRESS", "actual_result": None}

    def good(self, value):
        self.assertEqual(value["status"], "PASS", value)
        return value

    def bad(self, value, code=None):
        self.assertEqual(value["status"], "FAIL", value)
        self.assertTrue(value["issues"], value)
        if code:
            self.assertIn(code, [item["code"] for item in value["issues"]], value)
        return value

    def prepare_evidence(self, *, with_git=True):
        if with_git:
            if not shutil.which("git"):
                self.skipTest("Git unavailable")
            for args in (("init", "--quiet"), ("config", "user.name", "Trace Test"), ("config", "user.email", "trace@example.invalid"), ("commit", "--allow-empty", "--quiet", "-m", "fixture")):
                subprocess.run(["git", "-C", str(self.root), *args], check=True, capture_output=True)
        config = self.root / ".project/project.json"
        config.parent.mkdir(exist_ok=True)
        config.write_text(json.dumps({"schema_version": 1, "project_id": "trace.fixture", "modules": [], "evidence_store": {"type": "local-cas", "root": "../store"}}), encoding="utf-8")
        toolkit = self.parent / "Toolkit"
        catalog = toolkit / "prototypes/ai-ui-test-tool-auto-probe/scheduler/container/environment-catalog.json"
        catalog.parent.mkdir(parents=True)
        catalog.write_text(json.dumps({"version": 1, "environments": [{"version": 1, "key": "docker-linux-level0", "kind": "container"}]}), encoding="utf-8")
        (toolkit / "docs").mkdir()
        (toolkit / "docs/INDEX.md").write_text("[catalog](../prototypes/ai-ui-test-tool-auto-probe/scheduler/container/environment-catalog.json)\n", encoding="utf-8")
        self.env_patch = patch.dict(os.environ, {"RUST_MAKEPAD_TOOLKIT_ROOT": str(toolkit)})
        self.env_patch.start()
        self.addCleanup(self.env_patch.stop)
        (self.root / "app.exe").write_bytes(b"MZ fixture executable build 1")
        self.revision = api._git_state(self.root)["commit"]

    def evidence(self, identity="EV-TRACE-1", *, report=None, sealed=True, executable=True, raw_report=None, raw_text=None, extra_artifacts=None):
        execution = {"provider": "rust_makepad_toolkit", "type": "container", "id": "docker-linux-level0"}
        files = [{"path": name, "sha256": file_sha256(self.root / name)} for name in ("src/render.py", "tests/test_render.py")]
        source = {"revision": self.revision, "files": files}
        build = {"path": "app.exe", "sha256": file_sha256(self.root / "app.exe")}
        envelope = {"task_id": self.trace["task_id"], "scope": self.trace["scope"], "objects": self.trace["object_ids"], "execution": execution, "source": source, "result": {"status": "PASS", "checks": [{"id": "smoke", "status": "PASS", "type": "executable" if executable else "integrity", "scope": self.trace["scope"]}]}}
        # Synthetic canonical Toolkit receipt: plumbing only; no EXE is run.
        raw = {"schema_version": 2, "tool_version": "ai-ui-test-tool-auto-probe", "contract_version": "reliability-v2", "actions": [{
            "action_id": "fixture-1", "session_id": "fixture-session", "timestamp_unix_ms": 1,
            "action": "assert_visible", "target": "fixture.control", "result": {"success": True, "operation": "assert_visible", "action_id": "fixture-1", "phase": "complete", "application_healthy": True, "result": {"assertion": "visible", "passed": True, "control": {"automation_id": "fixture.control", "visible": True}}}}]}
        if raw_report is not None:
            raw = raw_report(raw)
        raw_path = self.root / f"{identity}-raw.json"
        raw_path.write_text(json.dumps(raw) if raw_text is None else raw_text, encoding="utf-8")
        raw_reference, _, _ = cas_put(self.root, raw_path)
        envelope["proof"] = {"adapter": "toolkit-reliability-v2", "schema_version": 2, "raw": raw_reference, "bindings": [{
            **{key: envelope["result"]["checks"][0][key] for key in ("id", "type", "scope")}, "action_id": "fixture-1", "operation": "assert_visible"}]}
        if executable:
            envelope["build"] = build
        if report is not None:
            envelope = report(envelope) if callable(report) else report
        output = self.root / f"{identity}-result.json"
        output.write_text(json.dumps(envelope), encoding="utf-8")
        reference, _, _ = cas_put(self.root, output)
        draft = {"id": identity, "objects": self.trace["object_ids"], "task_id": self.trace["task_id"], "scope": self.trace["scope"], "execution": execution, "inputs": {}, "artifacts": {"result": reference, "raw": raw_reference}, "source": {"revision": self.revision, "files": [item["path"] for item in files]}, "result": {"status": "PASS"}}
        draft["artifacts"].update(extra_artifacts or {})
        if executable:
            draft["build"] = {"path": "app.exe"}
        path = self.root / f"{identity}-draft.json"
        path.write_text(json.dumps(draft), encoding="utf-8")
        value, issues = create_evidence(self.root, path)
        self.assertIsNotNone(value, issues)
        if sealed:
            seal, issues = seal_evidence(self.root, identity)
            self.assertIsNotNone(seal, issues)
        self.trace["evidence_refs"].append(identity)
        self.trace["status"] = "DONE"
        self.trace["actual_result"] = {"status": "PASS"}
        self.trace["performed_checks"] = [{"id": "smoke", "type": "executable" if executable else "integrity", "scope": self.trace["scope"], "status": "PASS"}]
        self.good(api.task_trace_write(self.root, self.trace))
        return value

    def verify_coverage(self, **kwargs):
        return api.coverage_set(self.root, "renderer.render", "PROTOTYPE", "VERIFIED", "Captured smoke checks apply to current source and build", evidence_ids=self.trace["evidence_refs"], task_id=self.trace["task_id"], executable_path=kwargs.pop("executable_path", self.root / "app.exe"), **kwargs)

    def test_reads_do_not_create_project_metadata(self):
        self.assertFalse((self.root / ".project").exists())
        self.bad(api.task_trace_get(self.root, "RTX-0042"), "TASK_NOT_FOUND")
        self.assertEqual(self.good(api.task_trace_list(self.root))["traces"], [])
        coverage = self.good(api.coverage_get(self.root))["coverage"]["renderer.render"]
        self.assertEqual(coverage["PROTOTYPE"]["state"], "UNKNOWN")
        self.assertEqual(coverage["PRODUCT"]["state"], "UNKNOWN")
        self.good(api.source_link_audit(self.root))
        self.assertFalse((self.root / ".project").exists())

    def test_task_linkage_roundtrip_conflict_and_pagination(self):
        first = self.good(api.task_trace_write(self.root, self.trace))
        read = self.good(api.task_trace_get(self.root, self.trace["task_id"]))
        self.assertEqual(read["trace"], first["trace"])
        self.assertEqual(read["hash"], first["hash"])
        self.trace["status"] = "DONE"
        self.trace["actual_result"] = {"note": "external result declaration only"}
        self.bad(api.task_trace_write(self.root, self.trace), "CONFLICT")
        self.bad(api.task_trace_write(self.root, self.trace, "0" * 64), "CONFLICT")
        changed = self.good(api.task_trace_write(self.root, self.trace, first["hash"]))
        self.assertNotEqual(changed["hash"], first["hash"])
        another = {**self.trace, "task_id": "RTX-0043"}
        self.good(api.task_trace_write(self.root, another))
        page = self.good(api.task_trace_list(self.root, "renderer.render", 0, 1))
        self.assertEqual(page["total"], 2)
        self.assertEqual(page["next_offset"], 1)
        self.assertEqual(len(page["traces"]), 1)
        self.assertIsNone(self.good(api.task_trace_list(self.root, offset=1, limit=1))["next_offset"])
        self.bad(api.task_trace_list(self.root, limit=0), "INVALID_PAGINATION")
        self.assertFalse((self.root / ".project/objects.json").exists())

    def test_dynamic_catalog_import_and_real_sklt_only(self):
        self.catalog_patch.stop()
        from tools.project_structure.v04_projects import project_catalog
        with patch.object(api.importlib, "import_module", return_value=SimpleNamespace(project_catalog=project_catalog)) as imported:
            self.good(api.coverage_get(self.root, "renderer.render"))
            imported.assert_called_with(".v04_projects", "tools.project_structure")
        self.assertFalse((self.root / ".project").exists())
        self.good(api.task_trace_write(self.root, self.trace))
        self.good(api.coverage_set(self.root, "renderer.render", "PROTOTYPE", "PARTIAL", "Implementation is partial according to explicit source review"))
        catalog = project_catalog(self.root)
        self.assertEqual(catalog["mode"], "SKLT_ONLY")
        self.assertFalse((self.root / ".project/project.json").exists())
        self.assertFalse((self.root / ".project/objects.json").exists())

    def test_unsupported_declared_kind_reports_capability_error_for_valid_nested_id(self):
        identity = "declared:project:Demo/module:Renderer/capability:Draw"
        record = copy.deepcopy(self.catalog["objects"]["renderer.render"])
        record.update(object_id=identity, dialect="DECLARATIONS", kind="future-unsupported-kind")
        self.catalog["objects"] = {identity: record}
        result = self.bad(api.coverage_get(self.root, identity), "UNSUPPORTED_OBJECT_KIND")
        self.assertNotIn("INVALID_OBJECT_ID", [item["code"] for item in result["issues"]])
        self.bad(api.coverage_set(self.root, identity, "PROTOTYPE", "PARTIAL", "reviewed"), "UNSUPPORTED_OBJECT_KIND")
        self.assertFalse((self.root / ".project").exists())

    def test_unrelated_unsupported_metadata_preserves_selective_operations(self):
        self.catalog_patch.stop()
        from tools.project_structure.v04_projects import project_catalog
        metadata = self.root / "metadata"
        metadata.mkdir()
        for name in ("constraints", "progress", "unresolved"):
            (metadata / f"{name}.sklt").write_text(f"{name}:\n    external metadata fixture\n", encoding="utf-8")
        catalog = project_catalog(self.root)
        self.assertEqual(catalog["mode"], "SKLT_ONLY")
        self.assertEqual(catalog["status"], "FAIL")
        self.assertEqual(len([item for item in catalog["issues"] if item["severity"] == "ERROR"]), 3)
        before = {path.relative_to(self.root).as_posix(): path.read_bytes() for path in self.root.rglob("*") if path.is_file()}
        audit = self.good(api.source_link_audit(self.root, ["renderer.render"]))
        selected = self.good(api.coverage_get(self.root, "renderer.render"))
        self.assertEqual(audit["issues"], catalog["issues"])
        self.assertEqual(selected["issues"], catalog["issues"])
        self.assertEqual(audit["objects"][0]["source_refs"][0]["semantic_state"], "UNKNOWN")
        self.assertEqual(selected["coverage"]["renderer.render"]["PROTOTYPE"]["state"], "UNKNOWN")
        all_coverage = self.bad(api.coverage_get(self.root))
        self.assertEqual(all_coverage["issues"], catalog["issues"])
        self.assertIn("renderer.render", all_coverage["coverage"])
        self.bad(api.task_trace_get(self.root, self.trace["task_id"]), "TASK_NOT_FOUND")
        self.good(api.task_trace_list(self.root))
        self.assertEqual(before, {path.relative_to(self.root).as_posix(): path.read_bytes() for path in self.root.rglob("*") if path.is_file()})
        self.assertFalse((self.root / ".project").exists())
        written = self.good(api.task_trace_write(self.root, self.trace))
        coverage = self.good(api.coverage_set(self.root, "renderer.render", "PROTOTYPE", "PARTIAL", "Source reviewed; implementation remains partial", task_id=self.trace["task_id"]))
        self.assertEqual(written["issues"], catalog["issues"])
        self.assertEqual(coverage["issues"], catalog["issues"])
        self.assertEqual(self.good(api.coverage_get(self.root, "renderer.render"))["coverage"]["renderer.render"]["PROTOTYPE"]["state"], "PARTIAL")
        self.assertEqual(project_catalog(self.root)["mode"], "SKLT_ONLY")
        self.assertFalse((self.root / ".project/project.json").exists())
        self.assertFalse((self.root / ".project/objects.json").exists())

    def test_invalid_managed_and_unavailable_catalogs_still_block(self):
        for mode in ("INVALID_MANAGED", "UNAVAILABLE"):
            with self.subTest(mode=mode):
                self.catalog["mode"] = mode
                self.catalog["issues"] = [{"code": "INVALID_PROJECT_MANIFEST" if mode == "INVALID_MANAGED" else "UNAVAILABLE_PROJECT_ROOT", "severity": "ERROR", "message": "fixture catalog is unusable"}]
                results = (
                    api.source_link_audit(self.root, ["renderer.render"]),
                    api.task_trace_write(self.root, self.trace),
                    api.coverage_set(self.root, "renderer.render", "PROTOTYPE", "PARTIAL", "Partial fixture"),
                    api.coverage_get(self.root, "renderer.render"),
                )
                for result in results:
                    self.bad(result, "CATALOG_ISSUES")
                    self.assertEqual(result["catalog_issues"], self.catalog["issues"])
                self.assertFalse((self.root / ".project").exists())

    def test_ambiguous_selected_ids_still_block_all_linkage(self):
        original = copy.deepcopy(self.catalog)
        variants = (
            {"identity_conflict": True},
            {"occurrences": [{"sklt": "renderer/render.sklt"}, {"sklt": "renderer/other.sklt"}]},
            {"duplicates": {"renderer.render": [{}, {}]}},
            {"issues": [{"code": "DUPLICATE_OBJECT_ID", "severity": "ERROR", "object_id": "renderer.render", "message": "ambiguous fixture identity"}]},
        )
        for variant in variants:
            with self.subTest(variant=variant):
                self.catalog = copy.deepcopy(original)
                if "duplicates" in variant or "issues" in variant:
                    self.catalog.update(variant)
                else:
                    self.catalog["objects"]["renderer.render"].update(variant)
                for result in (
                    api.source_link_audit(self.root, ["renderer.render"]),
                    api.task_trace_write(self.root, self.trace),
                    api.coverage_set(self.root, "renderer.render", "PROTOTYPE", "PARTIAL", "Partial fixture"),
                    api.coverage_get(self.root, "renderer.render"),
                ):
                    self.bad(result, "AMBIGUOUS_OBJECT_ID")
                self.assertFalse((self.root / ".project").exists())

    def test_unrelated_duplicate_does_not_ambiguate_selected_id(self):
        self.catalog["objects"]["renderer.duplicate"] = {**self.catalog["objects"]["renderer.render"], "object_id": "renderer.duplicate", "identity_conflict": True}
        self.catalog["duplicates"] = {"renderer.duplicate": [{}, {}]}
        self.catalog["issues"] = [{"code": "DUPLICATE_OBJECT_ID", "severity": "ERROR", "path": "renderer/render.sklt", "object_id": "renderer.duplicate", "message": "another identity is ambiguous"}]
        self.good(api.source_link_audit(self.root, ["renderer.render"]))
        self.good(api.coverage_get(self.root, "renderer.render"))
        self.assertFalse((self.root / ".project").exists())
        self.good(api.task_trace_write(self.root, self.trace))
        self.good(api.coverage_set(self.root, "renderer.render", "PROTOTYPE", "PARTIAL", "Partial fixture"))
        self.bad(api.coverage_get(self.root, "renderer.duplicate"), "AMBIGUOUS_OBJECT_ID")

    def test_selected_file_errors_and_missing_ids_are_not_ignored(self):
        self.catalog["issues"] = [{"code": "UNREADABLE_SKLT", "severity": "ERROR", "path": "renderer/render.sklt", "message": "selected file cannot be fully read"}]
        for result in (
            api.source_link_audit(self.root, ["renderer.render"]),
            api.task_trace_write(self.root, self.trace),
            api.coverage_set(self.root, "renderer.render", "PROTOTYPE", "PARTIAL", "Partial fixture"),
            api.coverage_get(self.root, "renderer.render"),
        ):
            self.bad(result, "OBJECT_CATALOG_ISSUES")
        for result in (
            api.source_link_audit(self.root, ["missing.object"]),
            api.task_trace_write(self.root, {**self.trace, "object_ids": ["missing.object"]}),
            api.coverage_set(self.root, "missing.object", "PROTOTYPE", "PARTIAL", "Partial fixture"),
            api.coverage_get(self.root, "missing.object"),
        ):
            self.bad(result, "UNKNOWN_OBJECT_ID")
        self.assertFalse((self.root / ".project").exists())

    def test_concurrent_updates_cannot_both_accept_the_same_hash(self):
        first = self.good(api.task_trace_write(self.root, self.trace))
        with ThreadPoolExecutor(max_workers=2) as workers:
            results = list(workers.map(lambda status: api.task_trace_write(self.root, {**self.trace, "status": status}, first["hash"]), ("DONE", "PARTIAL")))
        self.assertEqual(sorted(value["status"] for value in results), ["FAIL", "PASS"])
        self.bad(next(value for value in results if value["status"] == "FAIL"), "CONFLICT")

    def test_scope_coverage_is_distinct_from_structural_state(self):
        self.good(api.coverage_set(self.root, "renderer.render", "PROTOTYPE", "PARTIAL", "CPU path implemented; CUDA path incomplete"))
        result = self.good(api.coverage_get(self.root, "renderer.render"))["coverage"]["renderer.render"]
        self.assertEqual(result["structural_state"], "SKLT")
        self.assertEqual(result["PROTOTYPE"]["state"], "PARTIAL")
        self.assertEqual(result["PRODUCT"]["state"], "UNKNOWN")
        self.bad(api.coverage_set(self.root, "renderer.render", "PRODUCT", "NOT_STARTED", ""), "RATIONALE_REQUIRED")
        self.good(api.coverage_set(self.root, "renderer.render", "PRODUCT", "NOT_STARTED", "Inspected src/render.py: no product implementation declared or present"))
        self.bad(api.coverage_set(self.root, "renderer.render", "PRODUCT", "SKLT", "structural state"), "INVALID_COVERAGE_STATE")

    def test_source_audit_current_missing_stale_and_symbol_unknown(self):
        digest = file_sha256(self.root / "src/render.py")
        self.catalog["objects"]["renderer.render"]["source"] = [{"path": "src/render.py", "sha256": digest}, {"path": "src/missing.py"}, {"path": "src/render.py", "sha256": digest, "symbol": "render"}]
        rows = self.good(api.source_link_audit(self.root, ["renderer.render"]))["objects"][0]["source_refs"]
        self.assertEqual([row["state"] for row in rows], ["CURRENT", "MISSING", "UNKNOWN"])
        self.assertEqual(rows[2]["symbol_state"], "UNKNOWN")
        self.assertEqual(rows[2]["semantic_state"], "UNKNOWN")
        (self.root / "src/render.py").write_text("changed\n", encoding="utf-8")
        rows = self.good(api.source_link_audit(self.root))["objects"][0]["source_refs"]
        self.assertEqual(rows[0]["state"], "STALE")
        self.assertEqual(rows[2]["file_state"], "STALE")
        self.assertEqual(self.catalog["objects"]["renderer.render"]["source"][0]["sha256"], digest)

    def test_rejected_paths_and_sizes_leave_metadata_absent(self):
        for bad_path in ("../outside.py", str(self.parent / "outside.py"), "src/render.py:stream", "C:relative.py"):
            with self.subTest(path=bad_path):
                self.bad(api.task_trace_write(self.root, {**self.trace, "source_refs": [{"path": bad_path}]}))
        self.bad(api.task_trace_write(self.root, {**self.trace, "actual_result": "X" * (api.MAX_METADATA_BYTES + 1)}), "METADATA_TOO_LARGE")
        self.bad(api.task_trace_write(self.root, {**self.trace, "object_ids": ["missing.object"]}), "UNKNOWN_OBJECT_ID")
        self.bad(api.task_trace_write(self.root, {**self.trace, "assigned_agent": {"role": "admin"}}), "INVALID_AGENT_REF")
        self.assertFalse((self.root / ".project").exists())

    def test_verified_exact_executable_and_recheck_get_after_change(self):
        self.prepare_evidence()
        manifest = self.evidence()
        self.assertEqual(manifest["build"]["sha256"], hashlib.sha256((self.root / "app.exe").read_bytes()).hexdigest())
        self.assertIn(f"sha256:{manifest['build']['sha256']}", manifest["inputs"].values())
        self.good(self.verify_coverage())
        before = api._coverage_path(self.root, "renderer.render", "PROTOTYPE").read_bytes()
        self.assertEqual(self.good(api.coverage_get(self.root))["coverage"]["renderer.render"]["PROTOTYPE"]["state"], "VERIFIED")
        (self.root / "app.exe").write_bytes(b"MZ different executable build 2")
        result = self.bad(api.coverage_get(self.root))
        state = result["coverage"]["renderer.render"]["PROTOTYPE"]
        self.assertEqual(state["state"], "UNKNOWN")
        self.assertEqual(state["recorded_state"], "VERIFIED")
        self.assertIn("BUILD_HASH_MISMATCH", [issue["code"] for issue in result["issues"]])
        self.assertEqual(before, api._coverage_path(self.root, "renderer.render", "PROTOTYPE").read_bytes())
        self.bad(self.verify_coverage(), "BUILD_HASH_MISMATCH")

    def test_source_hash_and_revision_changes_invalidate_verified(self):
        self.prepare_evidence()
        self.evidence()
        self.good(self.verify_coverage())
        source = self.root / "src/render.py"
        original = source.read_bytes()
        source.write_bytes(b"new source")
        self.bad(api.coverage_get(self.root), "SOURCE_HASH_MISMATCH")
        source.write_bytes(original)
        self.good(api.coverage_get(self.root))
        subprocess.run(["git", "-C", str(self.root), "commit", "--allow-empty", "--quiet", "-m", "new revision"], check=True, capture_output=True)
        self.bad(api.coverage_get(self.root), "SOURCE_REVISION_MISMATCH")

    def test_report_build_mismatch_and_textual_pass_are_not_proof(self):
        self.prepare_evidence()
        self.evidence(report=lambda envelope: {**envelope, "build": {"path": "app.exe", "sha256": "0" * 64}})
        self.bad(self.verify_coverage(), "EXECUTION_RESULT_MISMATCH")

    def test_bare_textual_pass_in_cas_is_not_execution_proof(self):
        self.prepare_evidence()
        self.evidence(report="PASS")
        self.bad(self.verify_coverage(), "INVALID_EVIDENCE_PROOF")

    def test_report_task_object_and_scope_must_match_manifest(self):
        self.prepare_evidence()
        self.evidence(report=lambda envelope: {**envelope, "task_id": "ANOTHER-TASK", "objects": ["another.object"], "scope": "PRODUCT"})
        self.bad(self.verify_coverage(), "EXECUTION_RESULT_MISMATCH")

    def test_same_bytes_at_another_executable_path_are_not_exact_build(self):
        self.prepare_evidence()
        self.evidence()
        other = self.root / "other.exe"
        other.write_bytes((self.root / "app.exe").read_bytes())
        self.bad(self.verify_coverage(executable_path=other), "BUILD_HASH_MISMATCH")

    def test_executable_check_requires_build_even_if_caller_omits_path(self):
        self.prepare_evidence()
        self.evidence(executable=False, report=lambda envelope: {**envelope, "result": {"status": "PASS", "checks": [{"id": "smoke", "status": "PASS", "type": "executable"}]}})
        self.bad(self.verify_coverage(executable_path=None), "EXECUTABLE_HASH_REQUIRED")

    def test_unsealed_forged_and_corrupt_evidence_rejected(self):
        self.prepare_evidence()
        self.evidence(sealed=False)
        self.bad(self.verify_coverage(), "IMMUTABLE_EVIDENCE_REQUIRED")
        seal, issues = seal_evidence(self.root, "EV-TRACE-1")
        self.assertIsNotNone(seal, issues)
        self.good(self.verify_coverage())
        path = manifest_path(self.root, "EV-TRACE-1")
        path.chmod(stat.S_IREAD | stat.S_IWRITE)
        manifest = json.loads(path.read_text(encoding="utf-8"))
        manifest["task_id"] = "FORGED-TASK"
        path.write_text(json.dumps(manifest), encoding="utf-8")
        self.bad(api.coverage_get(self.root), "BROKEN_EVIDENCE_SEAL")
        self.assertEqual(verify_evidence(self.root, "EV-TRACE-1")["state"], "BROKEN")

    def test_cas_corruption_invalidates_coverage(self):
        self.prepare_evidence()
        manifest = self.evidence()
        self.good(self.verify_coverage())
        blob = cas_object_path(self.root, manifest["artifacts"]["result"])
        blob.chmod(stat.S_IREAD | stat.S_IWRITE)
        blob.write_bytes(b"PASS")
        self.bad(api.coverage_get(self.root), "BROKEN_CAS_REFERENCE")

    def test_structured_pass_without_checks_rejected(self):
        self.prepare_evidence()
        self.evidence(report=lambda envelope: {**envelope, "result": {"status": "PASS"}})
        self.bad(self.verify_coverage(), "CHECK_RESULTS_REQUIRED")

    def test_required_checks_object_task_and_scope_are_enforced(self):
        self.prepare_evidence()
        self.evidence()
        wrong = api.coverage_set(self.root, "renderer.render", "PRODUCT", "VERIFIED", "Wrong scope", evidence_ids=self.trace["evidence_refs"], task_id=self.trace["task_id"], executable_path="app.exe")
        self.bad(wrong, "TASK_SCOPE_MISMATCH")
        data = self.good(api.task_trace_get(self.root, self.trace["task_id"]))
        self.trace["required_checks"].append("gpu")
        self.good(api.task_trace_write(self.root, self.trace, data["hash"]))
        self.bad(self.verify_coverage(), "REQUIRED_CHECKS_MISSING")

    def test_sources_only_evidence_can_verify_without_executable(self):
        self.prepare_evidence()
        self.evidence(executable=False)
        self.good(self.verify_coverage(executable_path=None))
        self.bad(self.verify_coverage(), "BUILD_HASH_MISMATCH")

    def test_missing_git_cannot_promote_coverage(self):
        self.prepare_evidence(with_git=False)
        self.evidence()
        self.bad(self.verify_coverage(), "SOURCE_REVISION_UNKNOWN")


if __name__ == "__main__":
    unittest.main()
