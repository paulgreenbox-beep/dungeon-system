from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import unittest
import uuid

from tools.project_structure.v041_paths import publish_files
from tools.scion_runtime.windows_capture import (
    CaptureError,
    REPOSITORY_ROOT,
    _absolute_windows_path,
)


class WindowsCaptureTests(unittest.TestCase):
    def test_rejects_traversal_before_path_resolution(self):
        value = str(REPOSITORY_ROOT / "artifacts" / "scion-runtime-002" / "attempt" / ".." / "project")
        with self.assertRaises(CaptureError) as raised:
            _absolute_windows_path(value, "fixture")
        self.assertEqual(raised.exception.code, "INVALID_FIXTURE_PATH")

    @unittest.skipUnless(os.name == "nt", "the real capture writer is Windows-only")
    def test_manual_synthetic_fixture_is_labeled_notreal_and_fails_closed(self):
        # A public source clone intentionally has no prior runtime receipts.
        # Create only this test-output parent, never require private gate data.
        artifacts = REPOSITORY_ROOT / "artifacts" / "scion-runtime-002"
        artifacts.mkdir(parents=True, exist_ok=True)
        artifacts = artifacts.resolve(strict=True)
        attempt = artifacts / f"windows-capture-NOTREAL-{uuid.uuid4().hex}"
        project = attempt / "project"
        output = attempt / "capture.json"
        revision = "c1" + "a" * 64
        state = (f'base = "{revision}"\n'
                 'tracking_ref = "main"\n'
                 "sparse = []\n"
                 "inaccessible = []\n").encode("utf-8")
        # This synthetic state exists only in a uniquely named NOTREAL test
        # fixture; no Scion checkout or source tree is initialized or altered.
        publish_files(artifacts, {
            project / ".scion" / "state": state,
            project / "renderer" / "settings.rs": b"// synthetic NOTREAL settings\n",
            project / "renderer" / "frame.rs": b"// synthetic NOTREAL frame\n",
        })

        process = subprocess.run(
            [sys.executable, "-m", "tools.scion_runtime.windows_capture",
             "--fixture-root", str(project), "--scion-revision", revision,
             "--output", str(output)],
            cwd=REPOSITORY_ROOT, stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            timeout=120, check=False, text=True, encoding="utf-8",
        )
        receipt = json.loads(output.read_text(encoding="utf-8"))
        self.assertEqual(json.loads(process.stdout), receipt)
        self.assertEqual(receipt["fixture_label"], "NOTREAL")
        self.assertEqual(process.returncode, 0 if receipt["status"] == "CAPTURED" else 2)
        self.assertEqual(receipt["snapshot"]["revision_consistency"], "PER_SCOPE_OBSERVATIONS_NOT_ATOMIC")
        revisions = receipt["snapshot"]["scopes"]
        self.assertEqual(set(revisions), {"main", "storage"})
        self.assertEqual((revisions["main"]["backend"], revisions["main"]["revision"],
                          revisions["main"]["dirty_state"], revisions["main"]["availability"]),
                         ("SCION", revision, "UNKNOWN", "UNSUPPORTED_PLATFORM"))
        self.assertEqual(revisions["storage"]["backend"], "GIT")
        self.assertRegex(receipt["nested_git_commit"], r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
        self.assertEqual(revisions["storage"]["revision"], receipt["nested_git_commit"])
        self.assertNotEqual(revisions["storage"]["revision"], revisions["main"]["revision"])
        self.assertEqual(receipt["source"]["path"], "renderer/settings.rs")
        self.assertRegex(receipt["source"]["sha256"], r"^[0-9a-f]{64}$")
        self.assertEqual(receipt["source"]["cas"], "sha256:" + receipt["source"]["sha256"])
        self.assertIn(receipt["evidence"]["integrity_state"], ("VERIFIED", "SEALED"))
        self.assertFalse(receipt["proof"]["verified"])
        self.assertNotEqual(receipt["proof"]["state"], "VERIFIED")
        self.assertIn("VCS_PROOF_ADAPTER_UNSUPPORTED", receipt["proof"]["issue_codes"])
        self.assertIn(receipt["status"], ("CAPTURED", "CAPTURED_UNSEALED"))
        if receipt["status"] == "CAPTURED_UNSEALED":
            self.assertIn("EVIDENCE_SEAL_BLOCKED", {item["code"] for item in receipt["errors"]})
        else:
            self.assertIsNotNone(receipt["evidence"]["seal_sha256"])


if __name__ == "__main__":
    unittest.main()
