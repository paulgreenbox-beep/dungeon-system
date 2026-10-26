"""Publication checks use synthetic strings only, never captured credentials."""
from pathlib import Path
import hashlib
import json
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from tools.publication import verify


class PublicationTests(unittest.TestCase):
    def test_tree_snapshots_keep_real_residuals_and_ignore_only_absent_names(self):
        from tests.test_v04_projects import ProjectAPITests
        from tests.test_v041_faults import tree
        with tempfile.TemporaryDirectory(prefix="dungeon-snapshot-regression-") as temp:
            root = Path(temp)
            directory, file, ghost = root / "retained", root / "bytes.txt", root / "absent"
            directory.mkdir()
            file.write_bytes(b"exact fixture bytes")
            for snapshot, expected in (
                (lambda: ProjectAPITests.tree(SimpleNamespace(root=root)),
                 {"retained": "DIRECTORY", "bytes.txt": hashlib.sha256(file.read_bytes()).hexdigest()}),
                (lambda: tree(root), {"retained": None, "bytes.txt": file.read_bytes()}),
            ):
                with patch.object(Path, "rglob", return_value=iter((ghost, directory, file))):
                    self.assertEqual(snapshot(), expected)

    def test_tree_snapshots_propagate_inspection_errors(self):
        from tests.test_v04_projects import ProjectAPITests
        from tests.test_v041_faults import tree
        root = Path(tempfile.gettempdir()) / "synthetic-not-created"
        for snapshot in (lambda: ProjectAPITests.tree(SimpleNamespace(root=root)), lambda: tree(root)):
            with patch.object(Path, "rglob", return_value=iter((root / "entry",))):
                with patch.object(Path, "lstat", side_effect=PermissionError("synthetic inspection denial")):
                    with self.assertRaises(PermissionError):
                        snapshot()

    def test_regular_authored_source_is_allowed(self):
        self.assertEqual(verify.scan_content("tools/project_structure/model.py", b"value = 42\n"), [])

    def test_local_artifacts_and_binaries_are_denied_independently_of_gitignore(self):
        for path in ("artifacts/report.json", "Artifacts/capture.json", "archive/local/receipt.json", "snapshots/test/manifest.json",
                     "tools/codex-plugin/backups/source.py", "tools/hardening_v041/private.py",
                     "docs/audits/private-packet.json", ".project/objects.json", "scoping/.exp", "app.exe", "module.pyd",
                     ".project/http-service.lock", "nested/http-service.local.json"):
            with self.subTest(path=path):
                self.assertTrue(verify.path_issues(path))

    def test_nested_and_mixed_case_private_directories_are_denied(self):
        for path in ("nested/ARTIFACTS/capture.json", "vendor/BACKUPS/old.py", "nested/.Codex/config.toml", "Docs/Audits/private.json"):
            self.assertTrue(verify.path_issues(path), path)

    def test_json_quoted_credential_keys_are_scanned_and_redacted(self):
        synthetic = json.dumps({"pass" + "word": "synthetic" + "-credential-123456"})
        findings = verify.scan_content("example.json", synthetic.encode())
        self.assertTrue(any(row["rule"] == "credential-assignment" for row in findings))
        self.assertNotIn("-credential-", json.dumps(findings))

    def test_plugin_explicitly_forwards_backend_root_without_publishing_its_value(self):
        config = json.loads((verify.PLUGIN / ".mcp.json").read_text(encoding="utf-8"))["mcpServers"]["dungeon-system"]
        self.assertIn("DUNGEON_PROJECT_ROOT", config["env_vars"])
        self.assertIn("DUNGEON_PYTHON", config["env_vars"])
        self.assertNotIn("DUNGEON_PROJECT_ROOT", config["env"])
        self.assertEqual(config["env"]["DUNGEON_WRITABLE_ROOTS"], "[]")

    def test_only_explicit_public_audits_and_marketplace_are_allowed(self):
        for path in verify.PUBLIC_AUDITS | {".agents/plugins/marketplace.json"}:
            self.assertEqual(verify.path_issues(path), [], path)
        self.assertTrue(verify.path_issues(".agents/private.json"))

    def test_profile_paths_are_detected_without_logging_the_value(self):
        synthetic = "C:" + "\\" + "Users" + "\\" + "synthetic-person" + "\\" + "private"
        findings = verify.scan_content("example.txt", synthetic.encode())
        self.assertTrue(any(row["rule"] == "user-home-path" for row in findings))
        self.assertNotIn("synthetic-person", json.dumps(findings))

    def test_token_signatures_are_redacted(self):
        synthetic = "gh" + "p_" + "x" * 36
        findings = verify.scan_content("example.txt", ("header\n" + synthetic).encode())
        self.assertEqual(findings, [{"path": "example.txt", "rule": "github-token", "line": 2}])
        self.assertNotIn(synthetic, json.dumps(findings))

    def test_private_key_header_is_detected(self):
        synthetic = "-----" + "BEGIN " + "PRIVATE KEY" + "-----"
        self.assertTrue(any(row["rule"] == "private-key" for row in verify.scan_content("example.txt", synthetic.encode())))

    def test_binary_and_oversized_content_is_rejected(self):
        self.assertTrue(any(row["rule"] == "non-text-content" for row in verify.scan_content("data.txt", b"\0\1")))
        self.assertTrue(any(row["rule"] == "file-over-1-MiB" for row in verify.scan_content("data.txt", b"x" * (verify.MAX_FILE_BYTES + 1))))

    def test_export_paths_are_portable_and_not_device_aliases(self):
        for path in ("../outside.py", "/absolute.py", "folder\\outside.py", "drive:stream", "NUL.txt", "dir./file.py"):
            self.assertTrue(verify.path_issues(path), path)

    def test_git_links_are_rejected_before_blob_read(self):
        with patch.object(verify, "git", return_value=b"120000 " + b"a" * 40 + b" 0\tlink\0") as reader:
            with self.assertRaises(ValueError):
                verify.index_blobs()
            self.assertEqual(reader.call_count, 1)

    def test_complete_root_and_bundled_mit_license_match(self):
        license_bytes = (verify.ROOT / "LICENSE").read_bytes()
        self.assertTrue(verify.mit_check(license_bytes))
        self.assertFalse(verify.mit_check(b"MIT License\nshort label only"))
        self.assertEqual(license_bytes, (verify.PLUGIN / "LICENSE").read_bytes())


if __name__ == "__main__":
    unittest.main()
