"""Ambiguous or non-standard JSON must not turn a raw FAIL into a PASS."""
from pathlib import Path
import tempfile
import unittest
from tools.project_structure.v03_evidence import EvidenceError, _load_json

class EvidenceJsonTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="dungeon-json-")
        from tests.fixture_cleanup import cleanup_temporary_directory
        self.addCleanup(cleanup_temporary_directory, self.temp)
        self.path = Path(self.temp.name) / "receipt.json"

    def test_duplicate_failure_then_success_is_rejected(self):
        self.path.write_bytes(b'{"status":"FAIL","status":"PASS"}')
        with self.assertRaisesRegex(EvidenceError, "duplicate JSON field"):
            _load_json(self.path, "execution result")

    def test_nested_raw_action_duplicates_are_rejected(self):
        self.path.write_bytes(b'{"actions":[{"result":{"success":false,"success":true}}]}')
        with self.assertRaisesRegex(EvidenceError, "duplicate JSON field"):
            _load_json(self.path, "Toolkit raw receipt")

    def test_nonfinite_tokens_and_overflow_are_rejected(self):
        for token in (b'NaN', b'Infinity', b'-Infinity', b'1e999'):
            with self.subTest(token=token):
                self.path.write_bytes(b'{"value":' + token + b'}')
                with self.assertRaisesRegex(EvidenceError, "non-finite"):
                    _load_json(self.path, "receipt")

    def test_unique_finite_standard_json_remains_readable(self):
        self.path.write_bytes(b'{"status":"PASS","value":1.25,"nested":{"ok":true}}')
        result = _load_json(self.path, "receipt")
        self.assertEqual(result, {"status": "PASS", "value": 1.25, "nested": {"ok": True}})
