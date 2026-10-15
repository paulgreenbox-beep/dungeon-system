"""Bounded proof plumbing regressions; synthetic receipts never run a GPU/EXE."""
from __future__ import annotations

import json
import os
import stat
import subprocess
import unittest
from pathlib import Path
from unittest.mock import patch

from tests.test_v04_trace import TraceTests as _Fixture
from tools.project_structure import v04_trace as api
from tools.project_structure.v03_cas import cas_object_path, cas_put
from tools.project_structure.v03_evidence import (
    create_evidence, evidence_applicability, manifest_path, seal_evidence, verify_evidence,
)
from tools.project_structure.v041_proof import evaluate_proof, skeleton_obligation_sha256


class ProofTests(unittest.TestCase):
    setUp = _Fixture.setUp
    good = _Fixture.good
    bad = _Fixture.bad
    prepare_evidence = _Fixture.prepare_evidence
    evidence = _Fixture.evidence
    verify_coverage = _Fixture.verify_coverage

    def setup_proof(self, **kwargs):
        self.prepare_evidence()
        return self.evidence(**kwargs)

    def update_trace(self, **changes):
        previous = self.good(api.task_trace_get(self.root, self.trace["task_id"]))
        self.trace.update(changes)
        return self.good(api.task_trace_write(self.root, self.trace, previous["hash"]))

    def assert_proof(self, response, state, code):
        self.bad(response, code)
        self.assertEqual(response["proof_state"], state, response)

    def failed_trace(self, status):
        self.setup_proof()
        self.good(self.verify_coverage())
        stored = api._coverage_path(self.root, "renderer.render", "PROTOTYPE").read_bytes()
        self.update_trace(performed_checks=[{"id": "smoke", "type": "executable", "scope": "PROTOTYPE", "status": status}])
        self.assert_proof(self.verify_coverage(), "CONFLICT", "TRACE_EVIDENCE_CONFLICT")
        read = self.bad(api.coverage_get(self.root), "TRACE_EVIDENCE_CONFLICT")
        effective = read["coverage"]["renderer.render"]["PROTOTYPE"]
        self.assertEqual((effective["recorded_state"], effective["state"], effective["proof_state"]), ("VERIFIED", "UNKNOWN", "CONFLICT"))
        self.assertEqual(stored, api._coverage_path(self.root, "renderer.render", "PROTOTYPE").read_bytes())
        evidence = verify_evidence(self.root, "EV-TRACE-1")
        self.assertEqual((evidence["state"], evidence["integrity_state"], evidence["proof_state"]), ("SEALED", "SEALED", "CONFLICT"))

    def test_cas_pass_cannot_override_trace_skipped(self):
        self.failed_trace("SKIPPED")

    def test_cas_pass_cannot_override_trace_unknown(self):
        self.failed_trace("UNKNOWN")

    def test_cas_pass_cannot_override_trace_not_run(self):
        self.failed_trace("NOT_RUN")

    def test_cas_pass_cannot_override_trace_fail(self):
        self.failed_trace("FAIL")

    def test_actual_result_fail_and_task_not_run_are_conflicts(self):
        self.setup_proof()
        for changes in ({"actual_result": {"status": "FAIL"}}, {"actual_result": "FAIL"}, {"status": "NOT_RUN", "actual_result": {"status": "PASS"}}):
            with self.subTest(changes=changes):
                self.update_trace(status="DONE", actual_result={"status": "PASS"})
                self.update_trace(**changes)
                self.assert_proof(self.verify_coverage(), "CONFLICT", "TASK_RESULT_CONFLICT")

    def test_required_runtime_cannot_be_replaced_by_unit_without_build(self):
        self.setup_proof(executable=False, report=lambda r: {**r, "result": {"status": "PASS", "checks": [{"id": "smoke", "type": "unit", "status": "PASS"}]}})
        self.update_trace(required_checks=[{"id": "smoke", "type": "runtime", "scope": "PROTOTYPE"}], performed_checks=[{"id": "smoke", "type": "unit", "scope": "PROTOTYPE", "status": "PASS"}])
        response = self.verify_coverage(executable_path=None)
        self.assert_proof(response, "CONFLICT", "CHECK_TYPE_MISMATCH")
        self.assertIn("RUNTIME_BUILD_EVIDENCE_MISSING", [i["code"] for i in response["issues"]])

    def test_runtime_pass_requires_exact_executable_capture(self):
        self.setup_proof(executable=False, report=lambda r: {**r, "result": {"status": "PASS", "checks": [{"id": "smoke", "type": "runtime", "status": "PASS"}]}})
        self.update_trace(required_checks=[{"id": "smoke", "type": "runtime"}], performed_checks=[{"id": "smoke", "type": "runtime", "status": "PASS"}])
        self.bad(self.verify_coverage(executable_path=None), "RUNTIME_BUILD_EVIDENCE_MISSING")

    def test_required_check_scope_is_part_of_identity(self):
        self.setup_proof()
        self.update_trace(required_checks=[{"id": "smoke", "type": "executable", "scope": "PRODUCT"}])
        self.assert_proof(self.verify_coverage(), "CONFLICT", "CHECK_SCOPE_MISMATCH")

    def test_task_object_and_scope_report_mutations_rejected(self):
        self.setup_proof(report=lambda r: {**r, "task_id": "foreign-task", "objects": ["foreign.object"], "scope": "PRODUCT"})
        self.assert_proof(self.verify_coverage(), "CONFLICT", "EXECUTION_RESULT_MISMATCH")

    def test_exact_build_path_rejects_same_bytes_at_other_path(self):
        self.setup_proof()
        other = self.root / "other.exe"
        other.write_bytes((self.root / "app.exe").read_bytes())
        self.assert_proof(self.verify_coverage(executable_path=other), "CONFLICT", "BUILD_HASH_MISMATCH")

    def test_structured_pass_with_original_audit_fail_log_is_conflict(self):
        self.prepare_evidence()
        log = self.root / "audit-failure.log"
        log.write_text("INDEPENDENT AUDIT ADVERSARIAL LOG\nsmoke: FAIL\n0 passed; 1 failed; GPU NOT_RUN\n", encoding="utf-8")
        reference, _, _ = cas_put(self.root, log)
        self.evidence(extra_artifacts={"log-0": reference})
        self.assert_proof(self.verify_coverage(), "CONFLICT", "PROOF_CONFLICT")
        verification = verify_evidence(self.root, "EV-TRACE-1")
        self.assertEqual((verification["state"], verification["proof_state"]), ("SEALED", "CONFLICT"))

    def test_canonical_toolkit_failure_cannot_match_pass_claim(self):
        def failure(raw):
            raw["actions"][0]["result"].update(success=False, error={"code": "ELEMENT_NOT_VISIBLE"})
            return raw
        self.setup_proof(raw_report=failure)
        self.assert_proof(self.verify_coverage(), "CONFLICT", "PROOF_CONFLICT")

    def test_renamed_stdout_failure_vetoes_supported_positive_receipt(self):
        self.prepare_evidence()
        log = self.root / "runner.stdout"
        log.write_text("smoke: FAIL\n0 passed; 1 failed; GPU NOT_RUN\n", encoding="utf-8")
        reference, _, _ = cas_put(self.root, log)
        self.evidence(extra_artifacts={"stdout": reference})
        self.assert_proof(self.verify_coverage(), "CONFLICT", "PROOF_CONFLICT")
        verification = verify_evidence(self.root, "EV-TRACE-1")
        self.assertEqual((verification["integrity_state"], verification["proof_state"]), ("SEALED", "CONFLICT"))

    def test_unusual_artifact_failure_vetoes_valid_known_raw(self):
        self.prepare_evidence()
        log = self.root / "renamed.bin"
        log.write_bytes(b"smoke: FAIL\n")
        reference, _, _ = cas_put(self.root, log)
        manifest = self.evidence(extra_artifacts={"unusual": reference})
        self.assertNotEqual(manifest["artifacts"]["raw"], reference)
        self.assert_proof(self.verify_coverage(), "CONFLICT", "PROOF_CONFLICT")

    def test_unknown_stderr_extra_fails_closed(self):
        self.prepare_evidence()
        path = self.root / "runner.stderr"
        path.write_bytes(b"opaque runner PASS\n")
        reference, _, _ = cas_put(self.root, path)
        self.evidence(extra_artifacts={"stderr": reference})
        self.assert_proof(self.verify_coverage(), "INCOMPLETE", "PROOF_UNVERIFIED")

    def test_unknown_png_extra_fails_closed(self):
        self.prepare_evidence()
        path = self.root / "screenshot.png"
        path.write_bytes(b"\x89PNG\r\n\x1a\nopaque image")
        reference, _, _ = cas_put(self.root, path)
        self.evidence(extra_artifacts={"audit": reference})
        self.assert_proof(self.verify_coverage(), "INCOMPLETE", "PROOF_UNVERIFIED")

    def test_toolkit_assertion_observation_must_match_canonical_success(self):
        def contradiction(raw):
            raw["actions"][0]["result"]["result"]["control"]["visible"] = False
            return raw
        self.setup_proof(raw_report=contradiction)
        self.assert_proof(self.verify_coverage(), "CONFLICT", "PROOF_CONFLICT")

    def test_unknown_raw_format_fails_closed(self):
        self.setup_proof(raw_text="opaque custom runner PASS\n")
        self.assert_proof(self.verify_coverage(), "INCOMPLETE", "PROOF_UNVERIFIED")

    def test_missing_raw_correspondence_and_unknown_version_fail_closed(self):
        self.setup_proof(report=lambda r: {**r, "proof": {"adapter": "unknown", "schema_version": 123}})
        self.assert_proof(self.verify_coverage(), "INCOMPLETE", "PROOF_UNVERIFIED")

    def test_raw_action_binding_cannot_be_substituted(self):
        def swap(report):
            report["proof"]["bindings"][0]["action_id"] = "absent-action"
            return report
        self.setup_proof(report=swap)
        self.assert_proof(self.verify_coverage(), "CONFLICT", "PROOF_CONFLICT")

    def test_positive_synthetic_plumbing_uses_shared_evaluator(self):
        manifest = self.setup_proof()
        self.good(self.verify_coverage())
        self.assertEqual(manifest["structure"]["objects"]["renderer.render"]["obligation_sha256"], skeleton_obligation_sha256(self.root, "renderer/render.sklt"))
        record = {"object_id": "renderer.render", "scope": "PROTOTYPE", "task_id": self.trace["task_id"], "evidence_ids": self.trace["evidence_refs"], "executable_path": "app.exe"}
        for response in (evaluate_proof(self.root, record), evidence_applicability(self.root, "EV-TRACE-1", object_id="renderer.render", scope="PROTOTYPE", task_id=self.trace["task_id"], executable_path="app.exe")):
            self.good(response)
            self.assertEqual(response["proof_state"], "VERIFIED")
        self.assertEqual(verify_evidence(self.root, "EV-TRACE-1")["proof_state"], "VERIFIED")
        current = self.good(api.coverage_get(self.root))["coverage"]["renderer.render"]
        self.assertEqual(current["PROTOTYPE"]["state"], "VERIFIED")
        self.assertEqual(current["PRODUCT"]["state"], "UNKNOWN")

    def test_source_and_executable_mutations_are_stale(self):
        self.setup_proof()
        self.good(self.verify_coverage())
        for name, code in (("src/render.py", "SOURCE_HASH_MISMATCH"), ("app.exe", "BUILD_HASH_MISMATCH")):
            with self.subTest(name=name):
                path = self.root / name
                original = path.read_bytes()
                path.write_bytes(b"changed bytes")
                read = self.bad(api.coverage_get(self.root), code)
                self.assertEqual(read["coverage"]["renderer.render"]["PROTOTYPE"]["proof_state"], "STALE")
                path.write_bytes(original)
                self.good(api.coverage_get(self.root))

    def test_git_revision_mutation_is_stale(self):
        self.setup_proof()
        self.good(self.verify_coverage())
        subprocess.run(["git", "-C", str(self.root), "commit", "--allow-empty", "--quiet", "-m", "new revision"], check=True, capture_output=True)
        read = self.bad(api.coverage_get(self.root), "SOURCE_REVISION_MISMATCH")
        self.assertEqual(read["coverage"]["renderer.render"]["PROTOTYPE"]["proof_state"], "STALE")

    def test_responsibility_mutation_leaves_seal_but_stales_current_proof(self):
        self.setup_proof()
        self.good(self.verify_coverage())
        original = manifest_path(self.root, "EV-TRACE-1").read_bytes()
        sklt = self.root / "renderer/render.sklt"
        sklt.write_text(sklt.read_text(encoding="utf-8").replace("Render fixture", "Render another contract"), encoding="utf-8")
        read = self.bad(api.coverage_get(self.root), "SKELETON_OBLIGATION_CHANGED")
        self.assertEqual(read["coverage"]["renderer.render"]["PROTOTYPE"]["proof_state"], "STALE")
        result = verify_evidence(self.root, "EV-TRACE-1")
        self.assertEqual((result["state"], result["integrity_state"], result["proof_state"]), ("SEALED", "SEALED", "STALE"))
        self.assertEqual(original, manifest_path(self.root, "EV-TRACE-1").read_bytes())

    def test_legacy_missing_obligation_hash_is_incomplete_without_rewrite(self):
        self.setup_proof(sealed=False)
        path = manifest_path(self.root, "EV-TRACE-1")
        data = json.loads(path.read_text(encoding="utf-8"))
        del data["structure"]["objects"]["renderer.render"]["obligation_sha256"]
        path.write_text(json.dumps(data), encoding="utf-8")
        seal, issues = seal_evidence(self.root, "EV-TRACE-1")
        self.assertIsNotNone(seal, issues)
        before = path.read_bytes()
        self.assert_proof(self.verify_coverage(), "INCOMPLETE", "SKELETON_OBLIGATION_HASH_MISSING")
        verification = verify_evidence(self.root, "EV-TRACE-1")
        self.assertEqual((verification["integrity_state"], verification["proof_state"]), ("SEALED", "INCOMPLETE"))
        self.assertIn("SKELETON_OBLIGATION_HASH_MISSING", [item["code"] for item in verification["proof_issues"]])
        self.assertEqual(before, path.read_bytes())

    def test_cas_mutation_is_invalid(self):
        manifest = self.setup_proof()
        self.good(self.verify_coverage())
        raw = cas_object_path(self.root, manifest["artifacts"]["raw"])
        raw.chmod(stat.S_IREAD | stat.S_IWRITE)
        raw.write_bytes(b"corrupted CAS bytes")
        result = self.bad(api.coverage_get(self.root), "BROKEN_CAS_REFERENCE")
        self.assertEqual(result["coverage"]["renderer.render"]["PROTOTYPE"]["proof_state"], "INVALID")

    def test_missing_linked_evidence_and_foreign_project_rejected(self):
        self.setup_proof()
        self.good(self.verify_coverage())
        manifest = manifest_path(self.root, "EV-TRACE-1")
        temporary = manifest.with_suffix(".held")
        manifest.rename(temporary)
        self.bad(api.coverage_get(self.root), "EVIDENCE_NOT_FOUND")
        temporary.rename(manifest)
        self.good(api.coverage_get(self.root))
        config = self.root / ".project/project.json"
        project = json.loads(config.read_text(encoding="utf-8"))
        project["project_id"] = "foreign.project"
        config.write_text(json.dumps(project), encoding="utf-8")
        self.bad(api.coverage_get(self.root), "EVIDENCE_PROJECT_MISMATCH")

    def test_original_native_receipt_is_not_reclassified_as_verified(self):
        # Read original audit data only. It lacks the versioned adapter contract;
        # synthetic source/build around it cannot establish factual correspondence.
        original = Path(__file__).resolve().parents[1] / "docs/audits/dungeon-independent-audit-v04-evidence/toolkit-native-evidence.json"
        try:
            before = original.read_bytes()
        except FileNotFoundError:
            self.skipTest("Private audit replay packet is intentionally excluded from public distribution.")
        self.setup_proof(raw_report=lambda raw: json.loads(before))
        self.assert_proof(self.verify_coverage(), "INCOMPLETE", "PROOF_UNVERIFIED")
        self.assertEqual(before, original.read_bytes())

    def test_create_preflights_destinations_before_cas_capture(self):
        self.prepare_evidence()
        source = self.root / "draft.json"
        source.write_text(json.dumps({"id": "EV-UNSAFE", "objects": ["renderer.render"], "execution": {"provider": "rust_makepad_toolkit", "type": "container", "id": "docker-linux-level0"}, "source": {"files": ["src/render.py"]}, "result": {"status": "PASS"}}), encoding="utf-8")
        from tools.project_structure.v041_paths import UnsafeOutputPath
        with patch("tools.project_structure.v03_evidence.output_boundary", side_effect=UnsafeOutputPath("fixture destination refused")), patch("tools.project_structure.v03_evidence.cas_put") as capture:
            result, issues = create_evidence(self.root, source)
            self.assertIsNone(result)
            self.assertEqual(issues[0].code, "UNSAFE_OUTPUT_PATH")
            capture.assert_not_called()

    def test_original_audit_trace_inputs_replayed_without_modifying_packet(self):
        audit = Path(__file__).resolve().parents[1] / "docs/audits/dungeon-independent-audit-v04-evidence/evidence-probe-results.json"
        try:
            original = audit.read_bytes()
        except FileNotFoundError:
            self.skipTest("Private audit replay packet is intentionally excluded from public distribution.")
        scenarios = json.loads(original)["scenarios"]
        self.setup_proof(executable=False)
        for scenario in scenarios:
            if not scenario["scenario"].startswith("trace-conflict-"):
                continue
            with self.subTest(scenario=scenario["scenario"]):
                trace = scenario["observed"]["trace"]
                self.update_trace(**{key: trace[key] for key in ("required_checks", "performed_checks", "status", "actual_result")})
                self.assert_proof(self.verify_coverage(executable_path=None), "CONFLICT", "TRACE_EVIDENCE_CONFLICT")
        self.assertEqual(original, audit.read_bytes())

    def test_real_evidence_junction_preflight_has_zero_cas_or_external_writes(self):
        if os.name != "nt":
            self.skipTest("Windows junction fixture")
        self.prepare_evidence()
        outside = self.parent / "outside"
        outside.mkdir()
        canary = outside / "keep.txt"
        canary.write_bytes(b"owned canary")
        link = self.root / "evidence"
        subprocess.run(["cmd.exe", "/d", "/c", "mklink", "/J", str(link), str(outside)], check=True, capture_output=True)
        self.addCleanup(lambda: link.rmdir() if link.exists() else None)
        draft = self.root / "draft.json"
        draft.write_text(json.dumps({"id": "EV-JUNCTION", "objects": ["renderer.render"], "execution": {"provider": "rust_makepad_toolkit", "type": "container", "id": "docker-linux-level0"}, "source": {"files": ["src/render.py"]}, "result": {"status": "PASS"}}), encoding="utf-8")
        with patch("tools.project_structure.v03_evidence.cas_put") as capture:
            result, issues = create_evidence(self.root, draft)
            self.assertIsNone(result)
            self.assertEqual(issues[0].code, "UNSAFE_OUTPUT_PATH")
            capture.assert_not_called()
        self.assertEqual(canary.read_bytes(), b"owned canary")
        self.assertEqual([path.name for path in outside.iterdir()], ["keep.txt"])
        self.assertFalse((self.parent / "store").exists())


del _Fixture

if __name__ == "__main__":
    unittest.main()
