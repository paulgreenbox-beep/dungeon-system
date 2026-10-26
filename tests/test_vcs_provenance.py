from __future__ import annotations

import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tests.fixture_cleanup import cleanup_temporary_directory
from tools.project_structure import v03_evidence, v03_snapshots, v04_trace, vcs_backends, vcs_service
from tools.project_structure.v03_cas import configured_store_root
from tools.project_structure.v03_manifest import load_manifest


GIT_REVISION = "a" * 40
SCION_REVISION = "c1" + "b" * 64


def revision(scope_id="main", backend="GIT", revision=None, *, dirty="CLEAN",
             availability="AVAILABLE", observed_at="fixture-time", ref="main"):
    if revision is None:
        revision = GIT_REVISION if backend == "GIT" else SCION_REVISION
    return {"schema_version": 1, "backend": backend, "scope_id": scope_id,
            "revision": revision, "ref": ref, "dirty_state": dirty,
            "availability": availability, "observed_at": observed_at}


def envelope(*records):
    return {"schema_version": 1,
            "scopes": {record["scope_id"]: record for record in records}}


class VcsProvenanceBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="dungeon-vcs-provenance-")
        self.addCleanup(cleanup_temporary_directory, self.temporary)
        self.base = Path(self.temporary.name).resolve()
        self.root = self.base / "project"
        self.root.mkdir()
        (self.root / ".project").mkdir()
        (self.root / "src").mkdir()
        (self.root / "src/source.py").write_text("value = 1\n", encoding="utf-8")

    def write_manifest(self, **updates):
        data = {"schema_version": 1, "project_id": "fixture.provenance",
                "modules": [], "evidence_store": {"type": "local-cas", "root": "../store"}}
        data.update(updates)
        (self.root / ".project/project.json").write_text(json.dumps(data), encoding="utf-8")
        return data

    def write_object(self, object_id="renderer.item", scope=None, level="L1", state="SKLT"):
        module = object_id.split(".", 1)[0]
        directory = self.root / module
        directory.mkdir(parents=True, exist_ok=True)
        source_path = "src/source.py" if module == "renderer" else f"{module}/source.py"
        source_file = self.root / source_path
        source_file.parent.mkdir(parents=True, exist_ok=True)
        source_file.write_text("value = 1\n", encoding="utf-8")
        fields = ["schema_version:\n    1", f"id:\n    {object_id}",
                  f"object_id:\n    {object_id}", "kind:\n    component",
                  "responsibility:\n    provenance fixture", "inputs:\n    input",
                  "outputs:\n    output", "depends_on:", "effects:\n    state",
                  f"state:\n    {state}", f"level:\n    {level}"]
        if scope is not None:
            fields.append(f"git_scope:\n    {scope}")
        fields.append(f"source:\n    path: {source_path}")
        path = directory / (object_id.rsplit(".", 1)[-1] + ".sklt")
        path.write_text("\n".join(fields) + "\n", encoding="utf-8")
        return path

    def evidence_draft(self, identity="EV-PROV-1", *, typed=None, revision_value=None):
        draft = {"id": identity, "objects": ["renderer.item"], "task_id": "RTX-PROV-1",
                 "scope": "PROTOTYPE", "execution": {"provider": "fixture.external",
                 "type": "tool", "id": "fixture-run"}, "inputs": {}, "artifacts": {},
                 "source": {"revision": revision_value, "files": ["src/source.py"]},
                 "result": {"status": "NOT_RUN"}}
        if typed is not None:
            draft["source_revision"] = typed
        return draft

    def test_manifest_defaults_and_legacy_git_scopes_remain_available(self):
        manifest, issues = load_manifest(self.root)
        self.assertEqual(issues, [])
        self.assertEqual([(s.scope_id, s.backend, s.root) for s in manifest.effective_version_scopes()],
                         [("main", "GIT", ".")])
        self.write_manifest(git_scopes=[{"id": "main", "mode": "MONOREPO", "repository": "."},
                                        {"id": "compiler", "mode": "REPOSITORY", "root": "engine", "repository": "engine"}])
        self.assertEqual(configured_store_root(self.root), self.base / "store")
        self.assertNotEqual(configured_store_root(self.root), self.root / "store")
        manifest, issues = load_manifest(self.root)
        self.assertEqual(issues, [])
        self.assertEqual([s.scope_id for s in manifest.effective_version_scopes()], ["main", "compiler"])

    def test_manifest_rejects_unsupported_duplicate_unsafe_and_token_fields(self):
        cases = [
            ({"version_scopes": [{"id": "lab", "backend": "OTHER", "root": "lab"}]}, "INVALID_VERSION_SCOPE"),
            ({"version_scopes": [{"id": "lab", "backend": "GIT", "root": "lab"},
                                  {"id": "lab", "backend": "SCION", "root": "lab"}]}, "DUPLICATE_VERSION_SCOPE"),
            ({"git_scopes": [{"id": "main", "mode": "MONOREPO", "repository": "."}],
              "version_scopes": [{"id": "main", "backend": "SCION", "root": "lab"}]}, "DUPLICATE_VERSION_SCOPE"),
            ({"version_scopes": [{"id": "lab", "backend": "SCION", "root": "../outside"}]}, "INVALID_VERSION_SCOPE"),
            ({"version_scopes": [{"id": "lab", "backend": "SCION", "root": "C:/outside"}]}, "INVALID_VERSION_SCOPE"),
            ({"version_scopes": [{"id": "lab", "backend": "SCION", "root": "lab", "token": "secret"}]}, "INVALID_VERSION_SCOPE"),
            ({"version_scopes": [{"id": "lab", "backend": "SCION", "root": "lab", "endpoint_profile": "lab"}]}, "INVALID_VERSION_SCOPE"),
        ]
        for data, expected in cases:
            with self.subTest(data=data):
                self.write_manifest(**data)
                _, issues = load_manifest(self.root)
                self.assertIn(expected, {issue.code for issue in issues})

    def test_catalog_scope_ids_and_exp_counts_are_stable(self):
        from tools.project_structure.projections import write_projections
        from tools.project_structure.scanner import scan_project
        from tools.project_structure.validator import project_counts
        from tools.project_structure.v03_scan import scan_project_v03

        self.write_object("renderer.main", "main", "L1", "SKLT")
        self.write_object("engine.compiler", "compiler", "L2", "RARE")
        self.write_object("lab.scion", "scion-lab", "L3", "SKLT")
        before = scan_project(self.root)
        self.assertEqual(project_counts(before), {"sklt": 3, "rare": 1})
        self.assertEqual(write_projections(before), [])
        projection_paths = [self.root / "docs/.grade", self.root / "renderer/.exp",
                            self.root / "engine/.exp", self.root / "lab/.exp"]
        projections_before = [path.read_bytes() for path in projection_paths]

        self.write_manifest(git_scopes=[{"id": "main", "mode": "MONOREPO", "repository": "."},
                                        {"id": "compiler", "mode": "REPOSITORY", "root": "engine", "repository": "engine"}],
                            version_scopes=[{"id": "scion-lab", "backend": "SCION", "root": "lab",
                                             "endpoint_profile": "LAB"}])
        catalog = __import__("tools.project_structure.v04_projects", fromlist=["project_catalog"]).project_catalog(self.root)
        self.assertEqual(catalog["status"], "PASS", catalog.get("issues"))
        self.assertEqual({key: (value["git_scope"], value["level"]) for key, value in catalog["objects"].items()},
                         {"renderer.main": ("main", "L1"), "engine.compiler": ("compiler", "L2"),
                          "lab.scion": ("scion-lab", "L3")})
        after = scan_project(self.root)
        self.assertEqual(project_counts(after), {"sklt": 3, "rare": 1})
        self.assertEqual(write_projections(after), [])
        self.assertEqual(projections_before, [path.read_bytes() for path in projection_paths])

        snapshot, _, scan_issues = scan_project_v03(self.root)
        self.assertFalse([issue for issue in scan_issues if issue.severity == "ERROR"], scan_issues)
        typed = {scope_id: revision(scope_id, backend, dirty="UNKNOWN" if backend == "SCION" else "CLEAN")
                 for scope_id, backend in (("main", "GIT"), ("compiler", "GIT"), ("scion-lab", "SCION"))}
        with patch.object(vcs_backends, "capture_revisions", side_effect=lambda root, scopes: {s.scope_id: typed[s.scope_id] for s in scopes}) as capture:
            result = vcs_service.object_revisions(self.root, list(catalog["objects"]))
        self.assertEqual(set(result["scopes"]), {"main", "compiler", "scion-lab"})
        self.assertEqual({s.scope_id for s in capture.call_args.args[1]}, {"main", "compiler", "scion-lab"})

    def test_refs_keep_typed_observation_and_preserve_legacy_alias(self):
        typed = revision("lab", "SCION", dirty="UNKNOWN")
        result = v04_trace._refs(self.root, {"path": "src/source.py", "source_revision": typed}, "source_refs")
        self.assertEqual(result[0]["source_revision"], typed)
        self.assertNotIn("revision", result[0])
        legacy = v04_trace._refs(self.root, {"path": "src/source.py", "source_revision": GIT_REVISION}, "source_refs")
        self.assertEqual(legacy[0]["revision"], GIT_REVISION)
        self.assertNotIn("source_revision", legacy[0])

    def test_refs_reject_malformed_or_ambiguous_typed_revision(self):
        typed = revision("lab", "SCION", dirty="UNKNOWN")
        malformed = {**typed, "scope_id": "../root"}
        for record in ({"path": "src/source.py", "source_revision": malformed},
                       {"path": "src/source.py", "source_revision": typed, "revision": GIT_REVISION}):
            with self.subTest(record=record), self.assertRaises(v04_trace.TraceError) as raised:
                v04_trace._refs(self.root, record, "source_refs")
            self.assertEqual(raised.exception.code, "INVALID_SOURCE_REF")

    def test_legacy_git_reference_still_uses_root_git_alias(self):
        self.write_object("renderer.item", "main")
        catalog = {"objects": {"renderer.item": {"object_id": "renderer.item", "git_scope": "main",
                   "source": [{"path": "src/source.py", "source_revision": GIT_REVISION}]}}, "issues": []}
        with patch.object(v04_trace, "_project_catalog", return_value=catalog), \
             patch.object(v04_trace, "_git_state", return_value={"commit": GIT_REVISION, "dirty": False}) as git:
            result = v04_trace.source_link_audit(self.root, ["renderer.item"])
        self.assertEqual(result["objects"][0]["source_refs"][0]["revision_state"], "CURRENT")
        git.assert_called_once_with(self.root)

    def test_legacy_git_sha_never_marks_scion_scoped_ref_current(self):
        self.write_manifest(version_scopes=[{"id": "scion-lab", "backend": "SCION", "root": "lab",
                                             "endpoint_profile": "LAB"}])
        self.write_object("lab.item", "scion-lab")
        catalog = {"objects": {"lab.item": {"object_id": "lab.item", "git_scope": "scion-lab",
                   "source": [{"path": "src/source.py", "source_revision": GIT_REVISION}]}}, "issues": []}
        with patch.object(v04_trace, "_project_catalog", return_value=catalog), \
             patch.object(v04_trace, "_git_state", return_value={"commit": GIT_REVISION, "dirty": False}) as git:
            result = v04_trace.source_link_audit(self.root, ["lab.item"])
        row = result["objects"][0]["source_refs"][0]
        self.assertEqual(row["revision_state"], "UNKNOWN")
        self.assertEqual(row["file_state"], "CURRENT")
        self.assertEqual(row["state"], "UNKNOWN")
        git.assert_not_called()

    def test_typed_audit_queries_declared_scope_not_root_git(self):
        self.write_manifest(version_scopes=[{"id": "scion-lab", "backend": "SCION", "root": "lab",
                                             "endpoint_profile": "LAB"}])
        self.write_object("lab.item", "scion-lab")
        captured = revision("scion-lab", "SCION", dirty="CLEAN")
        catalog = {"objects": {"lab.item": {"object_id": "lab.item", "git_scope": "scion-lab",
                   "source": [{"path": "src/source.py", "source_revision": captured}]}}, "issues": []}

        class Backend:
            def status(self):
                return {"source_revision": {**captured, "observed_at": "fresh"}}

        with patch.object(v04_trace, "_project_catalog", return_value=catalog), \
             patch.object(vcs_backends, "backend_for", side_effect=lambda root, scope: self.assertEqual((scope.scope_id, scope.backend), ("scion-lab", "SCION")) or Backend()), \
             patch.object(v04_trace, "_git_state", side_effect=AssertionError("typed Scion ref must not query root Git")):
            result = v04_trace.source_link_audit(self.root, ["lab.item"])
        row = result["objects"][0]["source_refs"][0]
        self.assertEqual(row["revision_state"], "CURRENT")
        self.assertEqual(row["file_state"], "CURRENT")

    def test_typed_audit_marks_unavailable_unknown_and_null_as_unknown(self):
        self.write_manifest(version_scopes=[{"id": "scion-lab", "backend": "SCION", "root": "lab",
                                             "endpoint_profile": "LAB"}])
        self.write_object("lab.item", "scion-lab")
        path = {"path": "src/source.py"}
        cases = [
            revision("scion-lab", "SCION", dirty="UNKNOWN", availability="AVAILABLE"),
            revision("scion-lab", "SCION", revision=None, dirty="UNKNOWN", availability="UNAVAILABLE"),
            revision("scion-lab", "SCION", dirty="UNKNOWN", availability="UNAVAILABLE"),
        ]
        for captured in cases:
            with self.subTest(captured=captured):
                ref = {**path, "source_revision": captured}
                row = v04_trace._audit_ref(self.root, ref, GIT_REVISION, "scion-lab")
                self.assertEqual(row["revision_state"], "UNKNOWN")
                self.assertEqual(row["file_state"], "CURRENT")
                self.assertEqual(row["state"], "UNKNOWN")
                self.assertEqual(row["semantic_state"], "UNKNOWN")

    def test_typed_audit_never_compares_scion_id_to_git_or_wrong_namespace(self):
        self.write_manifest(version_scopes=[{"id": "scion-lab", "backend": "SCION", "root": "lab",
                                             "endpoint_profile": "LAB"}])
        self.write_object("lab.item", "scion-lab")
        captured = revision("scion-lab", "SCION", dirty="CLEAN")
        from tools.project_structure.vcs_backends import backend_for

        class Backend:
            def __init__(self, current):
                self.current = current
            def status(self):
                return {"source_revision": self.current}

        stale = {**captured, "revision": "c1" + "c" * 64}
        with patch.object(vcs_backends, "backend_for", return_value=Backend(stale)):
            row = v04_trace._audit_ref(self.root, {"path": "src/source.py", "source_revision": captured}, GIT_REVISION, "scion-lab")
        self.assertEqual(row["revision_state"], "STALE")
        self.assertEqual(row["state"], "STALE")
        wrong_namespace = revision("main", "GIT", dirty="CLEAN")
        with patch.object(vcs_backends, "backend_for", return_value=Backend(wrong_namespace)):
            row = v04_trace._audit_ref(self.root, {"path": "src/source.py", "source_revision": captured}, GIT_REVISION, "scion-lab")
        self.assertEqual(row["revision_state"], "UNKNOWN")

    def test_snapshot_captures_mixed_scope_revisions_and_reads_old_schema(self):
        from tools.project_structure.libraries import discover_libraries
        from tools.project_structure.v03_scan import scan_project_v03

        self.write_manifest(version_scopes=[{"id": "scion-lab", "backend": "SCION", "root": "lab",
                                             "endpoint_profile": "LAB"}])
        self.write_object("renderer.item", "scion-lab")
        snapshot, manifest, issues = scan_project_v03(self.root)
        self.assertFalse([issue for issue in issues if issue.severity == "ERROR"], issues)
        mixed = {"main": revision("main", "GIT"),
                 "scion-lab": revision("scion-lab", "SCION", dirty="UNKNOWN")}
        with patch.object(vcs_backends, "capture_revisions", return_value=mixed):
            created = v03_snapshots.create_snapshot_v03(snapshot, "typed", manifest, discover_libraries(self.root))
        self.assertEqual(created, [])
        typed_manifest = json.loads((self.root / "snapshots/typed/manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(typed_manifest["revisions_schema"], 1)
        self.assertEqual(typed_manifest["revisions"], mixed)
        self.assertEqual(typed_manifest["revision_consistency"], "PER_SCOPE_OBSERVATIONS_NOT_ATOMIC")

        old_dir = self.root / "snapshots/legacy"
        old_dir.mkdir(parents=True)
        (old_dir / "manifest.json").write_text(json.dumps({"created_at": "2026-01-01T00:00:00+00:00",
            "objects": {}, "grade": {"sklt": 0, "rare": 0}}), encoding="utf-8")
        diff, diff_issues = v03_snapshots.snapshot_diff(self.root, "legacy", "typed")
        self.assertEqual(diff_issues, [])
        self.assertEqual(diff["revisions"], {"before": None, "after": mixed, "atomic": False})

    def test_evidence_rejects_bad_typed_envelope_and_requires_source_files(self):
        valid = envelope(revision("scion-lab", "SCION", dirty="UNKNOWN"))
        draft = self.evidence_draft(typed=valid)
        self.assertEqual(v03_evidence._draft_errors(draft), [])
        no_files = copy.deepcopy(draft)
        no_files["source"]["files"] = []
        self.assertTrue(any("require captured source files" in item for item in v03_evidence._draft_errors(no_files)))
        bad_namespace = {"schema_version": 1, "scopes": {"scion-lab": revision("main", "SCION", dirty="UNKNOWN")}}
        malformed = copy.deepcopy(draft)
        malformed["source_revision"] = bad_namespace
        self.assertTrue(any("correctly namespaced" in item for item in v03_evidence._draft_errors(malformed)))
        legacy_source = self.evidence_draft(revision_value=GIT_REVISION)
        self.assertEqual(v03_evidence._draft_errors(legacy_source), [])
        legacy_envelope = copy.deepcopy(legacy_source)
        legacy_envelope["source_revision"] = GIT_REVISION
        self.assertTrue(v03_evidence._draft_errors(legacy_envelope))

    def test_captured_typed_evidence_hashes_files_and_ignores_timestamp_guard(self):
        self.write_manifest(version_scopes=[{"id": "git-lab", "backend": "GIT", "root": "."}])
        self.write_object("renderer.item", "git-lab")
        observed = revision("git-lab", "GIT", dirty="DIRTY", observed_at="captured")
        requested = revision("git-lab", "GIT", dirty="DIRTY", observed_at="draft-time")
        draft = self.evidence_draft("EV-TYPED-1", typed=envelope(requested))
        draft_path = self.root / "draft.json"
        draft_path.write_text(json.dumps(draft), encoding="utf-8")
        with patch.object(vcs_service, "object_revisions", return_value=envelope(observed)):
            captured, issues = v03_evidence.create_evidence(self.root, draft_path)
        self.assertIsNotNone(captured, issues)
        self.assertEqual(captured["git"], {"commit": None, "dirty": None})
        self.assertIsNone(captured["source"]["revision"])
        self.assertEqual(captured["source_revision"], envelope(observed))
        source_file = captured["source"]["files"][0]
        self.assertRegex(source_file["sha256"], r"^[0-9a-f]{64}$")
        self.assertEqual(source_file["cas"], "sha256:" + source_file["sha256"])

    def test_unknown_typed_freshness_is_not_integrity_failure_and_never_proves(self):
        self.write_manifest(version_scopes=[{"id": "scion-lab", "backend": "SCION", "root": "lab",
                                             "endpoint_profile": "LAB"}])
        self.write_object("renderer.item", "scion-lab")
        captured_record = revision("scion-lab", "SCION", dirty="UNKNOWN", observed_at="captured")
        captured = envelope(captured_record)
        draft_path = self.root / "draft.json"
        draft_path.write_text(json.dumps(self.evidence_draft("EV-UNKNOWN-1", typed=captured)), encoding="utf-8")
        unavailable = revision("scion-lab", "SCION", revision=None, dirty="UNKNOWN",
                               availability="UNAVAILABLE", observed_at="later")
        first_observation = True
        def observe_revisions(*args, **kwargs):
            nonlocal first_observation
            if first_observation:
                first_observation = False
                return captured
            return envelope(unavailable)
        with patch.object(vcs_service, "object_revisions", side_effect=observe_revisions):
            evidence, create_issues = v03_evidence.create_evidence(self.root, draft_path)
            self.assertIsNotNone(evidence, create_issues)
            verified = v03_evidence.verify_evidence(self.root, evidence["id"])
        self.assertEqual(verified["integrity_state"], "VERIFIED")
        self.assertIn("SOURCE_REVISION_UNKNOWN", {item["code"] for item in verified["issues"]})
        self.assertNotEqual(verified["proof_state"], "VERIFIED")
        self.assertIn("VCS_PROOF_ADAPTER_UNSUPPORTED", {item["code"] for item in verified["proof_issues"]})

    def test_declared_scion_elsewhere_keeps_main_git_receipt_legacy(self):
        self.write_manifest(git_scopes=[{"id": "main", "mode": "MONOREPO", "repository": "."}],
                            version_scopes=[{"id": "scion-lab", "backend": "SCION", "root": "lab",
                                             "endpoint_profile": "LAB"}])
        self.write_object("renderer.item", "main")
        main_record = revision("main", "GIT", dirty="CLEAN")
        draft_path = self.root / "legacy-draft.json"
        draft_path.write_text(json.dumps(self.evidence_draft("EV-LEGACY-MAIN", revision_value=GIT_REVISION)), encoding="utf-8")
        with patch.object(vcs_service, "object_revisions", return_value=envelope(main_record)) as capture, \
             patch.object(v03_evidence, "_git_state", return_value={"commit": GIT_REVISION, "dirty": False}):
            evidence, issues = v03_evidence.create_evidence(self.root, draft_path)
        self.assertIsNotNone(evidence, issues)
        capture.assert_called_once_with(self.root.resolve(), ["renderer.item"])
        self.assertNotIn("source_revision", evidence)
        self.assertEqual(evidence["git"]["commit"], GIT_REVISION)
        self.assertEqual(evidence["source"]["revision"], GIT_REVISION)

    def test_old_git_evidence_is_rejected_if_selected_scope_backend_changes(self):
        self.write_object("renderer.item", "main")
        self.write_manifest(git_scopes=[{"id": "main", "mode": "MONOREPO", "repository": "."}])
        draft_path = self.root / "legacy-proof-draft.json"
        draft_path.write_text(json.dumps(self.evidence_draft("EV-SCOPE-CHANGE", revision_value=GIT_REVISION)), encoding="utf-8")
        with patch.object(v03_evidence, "_git_state", return_value={"commit": GIT_REVISION, "dirty": False}):
            evidence, issues = v03_evidence.create_evidence(self.root, draft_path)
            self.assertIsNotNone(evidence, issues)
            seal, seal_issues = v03_evidence.seal_evidence(self.root, evidence["id"])
            self.assertIsNotNone(seal, seal_issues)
            self.write_manifest(version_scopes=[{"id": "main", "backend": "SCION", "root": ".",
                                                 "endpoint_profile": "LAB"}])
            proof = __import__("tools.project_structure.v041_proof", fromlist=["evaluate_proof"]).evaluate_proof(
                self.root, {"object_id": "renderer.item", "scope": "PROTOTYPE",
                            "task_id": "RTX-PROV-1", "evidence_ids": [evidence["id"]]})
        self.assertIn("SOURCE_REVISION_BACKEND_MISMATCH", {item["code"] for item in proof["issues"]})


if __name__ == "__main__":
    unittest.main()
