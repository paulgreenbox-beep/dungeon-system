"""Synthetic NOTREAL parser fixtures; never actual Codex/Scion runtime receipts."""
from __future__ import annotations
import json
from pathlib import Path
import tempfile
import unittest

from tools.scion_runtime.codex_session_check import REQUIRED, validate_recorded_session


class RecordedSessionParserTests(unittest.TestCase):
    def fixture(self, *, qualified=True, failed_tool=None, no_events=False):
        temporary = tempfile.TemporaryDirectory(prefix="dungeon-NOTREAL-session-parser-")
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        names = sorted(REQUIRED) + [f"dungeon_NOTREAL_fixture_{i}" for i in range(30)]
        (root / "final.json").write_text(json.dumps({"dungeon_tool_count": 34,
            "dungeon_tool_names": [("mcp__dungeon_system__" if qualified else "") + name for name in names]}), encoding="utf-8")
        events = [] if no_events else [{"type": "item.completed", "item": {"type": "mcp_tool_call",
            "server": "NOTREAL-synthetic-fixture", "tool": name, "status": "completed", "error": None,
            "arguments": {}, "result": {"structuredContent": {"status": "FAIL" if name == failed_tool else "PASS"}}}}
            for name in sorted(REQUIRED)]
        (root / "events.jsonl").write_text("\n".join(json.dumps(event) for event in events), encoding="utf-8")
        return root

    def test_qualified_names_are_logical_names_not_missing_tools(self):
        result = validate_recorded_session(self.fixture(), 0)
        self.assertEqual(result["status"], "PASS")
        self.assertEqual(len(result["logical_tool_names"]), 34)
        self.assertTrue(result["requires_independent_review"])

    def test_completed_transport_with_failed_tool_result_is_not_acceptance(self):
        result = validate_recorded_session(self.fixture(failed_tool="dungeon_vcs_history"), 0)
        self.assertEqual(result["status"], "NOT_ACCEPTED")

    def test_model_inventory_without_actual_calls_is_not_acceptance(self):
        result = validate_recorded_session(self.fixture(no_events=True), 0)
        self.assertEqual(result["status"], "NOT_ACCEPTED")


if __name__ == "__main__":
    unittest.main()
