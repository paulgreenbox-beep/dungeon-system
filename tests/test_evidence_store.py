from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

from tools.project_structure.cli import main
from tools.project_structure.v03_cas import cas_object_path, cas_verify
from tools.project_structure.v03_evidence import create_evidence, file_sha256, validate_cronica_evidence_refs, verify_evidence


class EvidenceStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        temp_root = Path(self.temp_dir.name)
        self.root = temp_root / "project"
        self.root.mkdir()
        self.store = temp_root / "store"
        self.old_toolkit_root = os.environ.get("RUST_MAKEPAD_TOOLKIT_ROOT")
        self.toolkit = temp_root / "Rust_Makepad_Toolkit"
        catalog = self.toolkit / "prototypes" / "ai-ui-test-tool-auto-probe" / "scheduler" / "container" / "environment-catalog.json"
        catalog.parent.mkdir(parents=True)
        (self.toolkit / "docs").mkdir()
        (self.toolkit / "docs" / "INDEX.md").write_text(
            "[Trusted container environment catalog](../prototypes/ai-ui-test-tool-auto-probe/scheduler/container/environment-catalog.json)\n",
            encoding="utf-8",
        )
        catalog.write_text(json.dumps({
            "version": 1,
            "environments": [{"version": 1, "key": "docker-linux-level0", "kind": "container"}],
        }), encoding="utf-8")
        os.environ["RUST_MAKEPAD_TOOLKIT_ROOT"] = str(self.toolkit)
        self.write_manifest()
        self.write_docs()
        self.write_sklt()
        self.assertEqual(self.run_cli("exp")[0], 0)
        self.assertEqual(self.run_cli("index")[0], 0)
        self.assertEqual(self.run_cli("snapshot", "baseline")[0], 0)

    def tearDown(self) -> None:
        if self.old_toolkit_root is None:
            os.environ.pop("RUST_MAKEPAD_TOOLKIT_ROOT", None)
        else:
            os.environ["RUST_MAKEPAD_TOOLKIT_ROOT"] = self.old_toolkit_root
        from tests.fixture_cleanup import cleanup_temporary_directory
        cleanup_temporary_directory(self.temp_dir)

    def write_manifest(self, store_root: str = "../store") -> None:
        path = self.root / ".project" / "project.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({
            "schema_version": 1,
            "project_id": "evidence.fixture",
            "modules": [],
            "reserved_dirs": [".project", "docs", "evidence", "libs", "snapshots", "tests", "tools"],
            "ignore": [],
            "evidence_store": {"type": "local-cas", "root": store_root},
        }), encoding="utf-8")

    def write_docs(self) -> None:
        docs = self.root / "docs"
        (docs / "cronica").mkdir(parents=True, exist_ok=True)
        for name in ("project.md", "map.md", "modules.md", "architecture.md", "agent-guides.md"):
            (docs / name).write_text("# doc\n", encoding="utf-8")
        (docs / "cronica" / "README.md").write_text("# Cronica\n", encoding="utf-8")
        (self.root / "libs").mkdir()
        (self.root / "libs" / "README.md").write_text("# Libraries\n", encoding="utf-8")
        (self.root / "snapshots").mkdir()
        (self.root / "snapshots" / "README.md").write_text("# Snapshots\n", encoding="utf-8")

    def write_sklt(self) -> None:
        module = self.root / "renderer"
        module.mkdir(parents=True)
        (module / "settings.sklt").write_text(
            "schema_version:\n    1\n"
            "id:\n    renderer.settings\n"
            "object_id:\n    renderer.settings\n"
            "kind:\n    component\n"
            "responsibility:\n    evidence fixture\n"
            "inputs:\n    Configuration\n"
            "outputs:\n    Settings\n"
            "depends_on:\n"
            "effects:\n    settings\n"
            "state:\n    SKLT\n",
            encoding="utf-8",
        )

    def run_cli(self, *args: str) -> tuple[int, str]:
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = main([*args, "--root", str(self.root)])
        return code, output.getvalue()

    def capture_blob(self, filename: str, content: bytes) -> str:
        source = self.root / filename
        source.write_bytes(content)
        code, output = self.run_cli("cas-put", filename, "--json")
        self.assertEqual(code, 0, output)
        return json.loads(output)["reference"]

    def create_evidence(self, evidence_id: str, reference: str) -> Path:
        draft = {
            "id": evidence_id,
            "objects": ["renderer.settings"],
            "structure": {"snapshot": "baseline"},
            "execution": {
                "provider": "rust_makepad_toolkit",
                "type": "container",
                "id": "docker-linux-level0",
            },
            "inputs": {"scenario": reference},
            "artifacts": {"stdout": reference},
            "result": {"status": "PASS"},
        }
        draft_path = self.root / f"{evidence_id}-draft.json"
        draft_path.write_text(json.dumps(draft), encoding="utf-8")
        code, output = self.run_cli("evidence-create", str(draft_path), "--json")
        self.assertEqual(code, 0, output)
        return self.root / "evidence" / "manifests" / f"{evidence_id}.json"

    def test_cas_put_get_verify_and_deduplication(self) -> None:
        contents = b"exact evidence bytes\x00\x01"
        reference = self.capture_blob("capture.bin", contents)
        second = self.capture_blob("capture-copy.bin", contents)
        self.assertEqual(reference, second)

        valid, error, size = cas_verify(self.root, reference)
        self.assertTrue(valid, error)
        self.assertEqual(size, len(contents))

        output_path = self.root / "retrieved.bin"
        code, output = self.run_cli("cas-get", reference, "--output", str(output_path), "--json")
        self.assertEqual(code, 0, output)
        self.assertEqual(output_path.read_bytes(), contents)

        code, output = self.run_cli("cas-get", reference, "--output", str(output_path), "--json")
        self.assertEqual(code, 1)
        self.assertIn("already exists", json.loads(output)["error"])

    def test_manifest_git_structure_toolkit_and_seal_are_linked(self) -> None:
        if shutil.which("git") is None:
            self.skipTest("Git executable is unavailable")
        subprocess.run(["git", "-C", str(self.root), "init", "--quiet"], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(self.root), "config", "user.name", "Evidence Test"], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(self.root), "config", "user.email", "evidence-test@example.invalid"], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(self.root), "commit", "--allow-empty", "--quiet", "-m", "fixture baseline"], check=True, capture_output=True)
        reference = self.capture_blob("result.log", b"PASS\n")
        path = self.create_evidence("EV-20260923-001", reference)
        manifest = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(manifest["project"]["id"], "evidence.fixture")
        self.assertRegex(manifest["git"]["commit"], r"^[0-9a-f]{40,64}$")
        self.assertTrue(manifest["git"]["dirty"])
        self.assertEqual(manifest["structure"]["snapshot"], "baseline")
        self.assertEqual(manifest["structure"]["objects"]["renderer.settings"]["state"], "SKLT")
        self.assertEqual(manifest["execution"]["id"], "docker-linux-level0")

        code, output = self.run_cli("evidence-verify", "EV-20260923-001", "--json")
        self.assertEqual(code, 0, output)
        self.assertEqual(json.loads(output)["state"], "VERIFIED")

        code, output = self.run_cli("evidence-seal", "EV-20260923-001", "--json")
        self.assertEqual(code, 0, output)
        seal = json.loads(output)
        self.assertEqual(seal["format"], "dungeon.seal.v1")
        self.assertTrue(seal["sealed"])
        # Immutable API publication and seal hash are authoritative. Pathname
        # chmod is deliberately not used: a replaceable leaf could redirect it.
        frozen = path.read_bytes()
        code, duplicate = self.run_cli("evidence-create", str(self.root / "EV-20260923-001-draft.json"), "--json")
        self.assertEqual(code, 1, duplicate)
        self.assertIn("EVIDENCE_EXISTS", duplicate)
        self.assertEqual(path.read_bytes(), frozen)

        code, output = self.run_cli("evidence-verify", "EV-20260923-001", "--json")
        self.assertEqual(code, 0, output)
        self.assertEqual(json.loads(output)["state"], "SEALED")

        records, _ = __import__("tools.project_structure.v03_evidence", fromlist=["evidence_for"]).evidence_for(self.root, "renderer.settings")
        self.assertEqual(records[0]["id"], "EV-20260923-001")
        self.assertEqual(records[0]["state"], "SEALED")

    def test_seal_detects_manifest_rewrite(self) -> None:
        reference = self.capture_blob("result.log", b"PASS\n")
        path = self.create_evidence("EV-20260923-002", reference)
        self.assertEqual(self.run_cli("evidence-seal", "EV-20260923-002")[0], 0)
        path.chmod(stat.S_IWRITE | stat.S_IREAD)
        manifest = json.loads(path.read_text(encoding="utf-8"))
        manifest["result"]["status"] = "TEST_FAILED"
        path.write_text(json.dumps(manifest), encoding="utf-8")
        code, output = self.run_cli("evidence-verify", "EV-20260923-002", "--json")
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(output)["state"], "BROKEN")

    def test_unknown_container_and_missing_object_are_rejected(self) -> None:
        reference = self.capture_blob("result.log", b"PASS\n")
        draft_path = self.root / "invalid-draft.json"
        draft_path.write_text(json.dumps({
            "id": "EV-20260923-003",
            "objects": ["missing.object"],
            "execution": {"provider": "rust_makepad_toolkit", "type": "container", "id": "fake"},
            "inputs": {},
            "artifacts": {"stdout": reference},
            "result": {"status": "PASS"},
        }), encoding="utf-8")
        code, output = self.run_cli("evidence-create", str(draft_path), "--json")
        self.assertEqual(code, 1)
        self.assertTrue(any(issue["code"] == "UNKNOWN_EVIDENCE_OBJECT" for issue in json.loads(output)["issues"]))

        draft = json.loads(draft_path.read_text(encoding="utf-8"))
        draft["objects"] = ["renderer.settings"]
        draft["execution"]["id"] = "fake"
        draft_path.write_text(json.dumps(draft), encoding="utf-8")
        code, output = self.run_cli("evidence-create", str(draft_path), "--json")
        self.assertEqual(code, 1)
        self.assertTrue(any(issue["code"] == "UNKNOWN_TOOLKIT_CONTAINER" for issue in json.loads(output)["issues"]))

    def test_snapshot_can_link_evidence_and_status_tolerates_missing_cas(self) -> None:
        reference = self.capture_blob("result.log", b"PASS\n")
        self.create_evidence("EV-20260923-004", reference)
        code, output = self.run_cli("snapshot", "after-evidence", "--evidence-id", "EV-20260923-004", "--json")
        self.assertEqual(code, 0, output)
        snapshot = json.loads((self.root / "snapshots" / "after-evidence" / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(snapshot["evidence_ids"], ["EV-20260923-004"])

        self.write_manifest("../not-created")
        code, output = self.run_cli("status", "--json")
        self.assertEqual(code, 0, output)
        payload = json.loads(output)
        self.assertEqual(payload["validation"], "PASS")
        self.assertEqual(payload["evidence_store"]["cas"]["state"], "NOT_INITIALIZED")

    def test_cronica_evidence_refs_and_reserved_evidence_directory(self) -> None:
        reference = self.capture_blob("result.log", b"PASS\n")
        self.create_evidence("EV-20260923-005", reference)
        entry = self.root / "docs" / "cronica" / "0001-evidence.md"
        entry.write_text("# Evidence\n\n```text\nevidence_ids: [EV-20260923-005]\n```\n", encoding="utf-8")
        self.assertEqual(validate_cronica_evidence_refs(self.root), [])
        entry.write_text("# Evidence\n\n```text\nevidence_ids: [EV-MISSING]\n```\n", encoding="utf-8")
        issues = validate_cronica_evidence_refs(self.root)
        self.assertEqual([issue.code for issue in issues], ["UNKNOWN_CRONICA_EVIDENCE"])
        code, output = self.run_cli("validate", "--json")
        self.assertEqual(code, 1)
        self.assertTrue(any(issue["code"] == "UNKNOWN_CRONICA_EVIDENCE" for issue in json.loads(output)["issues"]))
        entry.write_text("# Evidence\n\n```text\nevidence_ids: [EV-20260923-005]\n```\n", encoding="utf-8")

        stray = self.root / "evidence" / "stray.sklt"
        stray.parent.mkdir(parents=True, exist_ok=True)
        stray.write_text("object_id:\n    evidence.stray\nstate:\n    SKLT\n", encoding="utf-8")
        code, output = self.run_cli("scan", "--json")
        self.assertEqual(code, 0, output)
        self.assertEqual(json.loads(output)["objects"], 1)

    def test_verify_reports_malformed_manifest_without_crashing(self) -> None:
        manifest_dir = self.root / "evidence" / "manifests"
        manifest_dir.mkdir(parents=True)
        (manifest_dir / "EV-20260923-malformed.json").write_text(json.dumps({
            "format": "dungeon.evidence.v1",
            "id": "EV-20260923-malformed",
            "created_at": "2026-09-23T00:00:00+00:00",
            "project": {"id": "evidence.fixture"},
            "objects": [[]],
            "git": {"commit": None, "dirty": None},
            "structure": {"snapshot": None, "objects": {"renderer.settings": {"state": [], "sklt": "renderer/settings.sklt"}}},
            "execution": {"provider": "rust_makepad_toolkit", "type": "container", "id": "docker-linux-level0"},
            "inputs": {},
            "artifacts": {},
            "result": {"status": "PASS"},
        }), encoding="utf-8")
        code, output = self.run_cli("evidence-verify", "EV-20260923-malformed", "--json")
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(output)["state"], "BROKEN")

    def build_draft(self, evidence_id: str = "EV-BUILD-001") -> Path:
        (self.root / "app.exe").write_bytes(b"MZ exact executable fixture")
        (self.root / "source.py").write_text("print('fixture')\n", encoding="utf-8")
        reference = self.capture_blob("build-result.json", b'{"status":"PASS"}')
        path = self.root / "build-draft.json"
        path.write_text(json.dumps({
            "id": evidence_id, "objects": ["renderer.settings"], "task_id": "BUILD-001", "scope": "PROTOTYPE",
            "execution": {"provider": "rust_makepad_toolkit", "type": "container", "id": "docker-linux-level0"},
            "inputs": {}, "artifacts": {"result": reference}, "result": {"status": "PASS"},
            "build": {"path": "app.exe"}, "source": {"files": ["source.py"]},
        }), encoding="utf-8")
        return path

    def test_build_and_source_hashes_are_captured_from_actual_files(self) -> None:
        payload, issues = create_evidence(self.root, self.build_draft())
        self.assertIsNotNone(payload, issues)
        self.assertEqual(payload["task_id"], "BUILD-001")
        self.assertEqual(payload["build"]["sha256"], file_sha256(self.root / "app.exe"))
        self.assertEqual(payload["inputs"]["executable"], "sha256:" + payload["build"]["sha256"])
        source = payload["source"]
        self.assertIsNone(source["revision"])
        self.assertEqual(source["provenance"], "local-files")
        self.assertEqual(source["files"][0]["sha256"], file_sha256(self.root / "source.py"))
        self.assertEqual(source["files"][0]["cas"], payload["inputs"]["source-0"])
        self.assertEqual(verify_evidence(self.root, payload["id"])["state"], "VERIFIED")
        (self.root / "app.exe").write_bytes(b"MZ another build")
        verification = verify_evidence(self.root, payload["id"])
        self.assertEqual(verification["state"], "BROKEN")
        self.assertIn("BUILD_HASH_MISMATCH", [item["code"] for item in verification["issues"]])

    def test_capture_rejects_fabricated_build_source_hash_and_revision(self) -> None:
        path = self.build_draft()
        original = json.loads(path.read_text(encoding="utf-8"))
        for section, value in (
            ("build", {"path": "app.exe", "sha256": "0" * 64}),
            ("source", {"files": [{"path": "source.py", "sha256": "0" * 64}]}),
            ("source", {"revision": "0" * 40, "files": ["source.py"]}),
            ("build", {"path": "../outside.exe"}),
        ):
            with self.subTest(section=section, value=value):
                draft = {**original, section: value}
                path.write_text(json.dumps(draft), encoding="utf-8")
                payload, issues = create_evidence(self.root, path)
                self.assertIsNone(payload)
                self.assertTrue(issues)
                self.assertFalse((self.root / "evidence/manifests/EV-BUILD-001.json").exists())

    def test_optional_link_fields_are_validated_without_type_crashes(self) -> None:
        path = self.build_draft()
        original = json.loads(path.read_text(encoding="utf-8"))
        for key, value in (("source", []), ("build", []), ("scope", []), ("task_id", {}), ("source", {"files": [[]]})):
            with self.subTest(key=key, value=value):
                path.write_text(json.dumps({**original, key: value}), encoding="utf-8")
                payload, issues = create_evidence(self.root, path)
                self.assertIsNone(payload)
                self.assertTrue(issues)

    def test_extended_and_legacy_manifests_match_existing_json_schema(self) -> None:
        try:
            from jsonschema import Draft202012Validator
        except ImportError:
            self.skipTest("jsonschema is unavailable")
        schema = json.loads((Path(__file__).resolve().parents[1] / "docs/evidence-manifest-v1.schema.json").read_text(encoding="utf-8"))
        Draft202012Validator.check_schema(schema)
        validator = Draft202012Validator(schema)
        payload, issues = create_evidence(self.root, self.build_draft())
        self.assertIsNotNone(payload, issues)
        self.assertEqual(list(validator.iter_errors(payload)), [])
        reference = self.capture_blob("legacy.log", b"PASS")
        legacy_path = self.create_evidence("EV-LEGACY-001", reference)
        legacy = json.loads(legacy_path.read_text(encoding="utf-8"))
        self.assertEqual(list(validator.iter_errors(legacy)), [])


if __name__ == "__main__":
    unittest.main()
