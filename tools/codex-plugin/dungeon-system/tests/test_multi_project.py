"""Safety and routing tests for the stateless multi-project MCP bridge."""

from __future__ import annotations

import importlib.util
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


SERVER_PATH = Path(__file__).resolve().parents[1] / "mcp" / "server.py"
SPEC = importlib.util.spec_from_file_location("dungeon_plugin_server_test", SERVER_PATH)
assert SPEC and SPEC.loader
SERVER = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = SERVER
SPEC.loader.exec_module(SERVER)


class MultiProjectTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        base = Path(self.temporary.name)
        self.home = base / "dungeon-home"
        self.managed = base / "another-managed"
        self.sklt_only = base / "ai-revit"

        for root, project_id in ((self.home, "dungeon.home"), (self.managed, "other.managed")):
            (root / ".project").mkdir(parents=True)
            (root / ".project" / "project.json").write_text(
                json.dumps({"project_id": project_id}), encoding="utf-8"
            )
        (self.home / "tools").mkdir()
        (self.home / "tools" / "project-structure.py").write_text("# marker\n", encoding="utf-8")

        (self.sklt_only / "sklt").mkdir(parents=True)
        (self.sklt_only / "sklt" / "project.sklt").write_text("project AIRevit:\n", encoding="utf-8")
        (self.sklt_only / "sklt" / "module.sklt").write_text("module Test:\n", encoding="utf-8")
        (self.sklt_only / "snapshots").mkdir()
        (self.sklt_only / "snapshots" / "old.sklt").write_text("old\n", encoding="utf-8")
        (self.sklt_only / "target-build").mkdir()
        (self.sklt_only / "target-build" / "generated.sklt").write_text("generated\n", encoding="utf-8")

        env = patch.dict(os.environ, {"DUNGEON_PROJECT_ROOT": str(self.home)}, clear=False)
        env.start()
        self.addCleanup(env.stop)
        removed = patch.dict(os.environ, {"DUNGEON_WRITABLE_ROOTS": "[]"}, clear=False)
        removed.start()
        self.addCleanup(removed.stop)

    def test_sklt_only_connect_and_read_do_not_create_an_index(self) -> None:
        selected = {"project_root": str(self.sklt_only)}
        info, ok = SERVER._call("dungeon_connect_project", selected)
        self.assertTrue(ok)
        self.assertEqual(info["project_mode"], "SKLT_ONLY")
        self.assertEqual(info["sklt_files"], 2)
        self.assertFalse(info["write_enabled"])

        listing, ok = SERVER._call("dungeon_list_sklt", selected)
        self.assertTrue(ok)
        self.assertEqual(listing["paths"], ["sklt/module.sklt", "sklt/project.sklt"])
        content, ok = SERVER._call(
            "dungeon_read_sklt", {**selected, "sklt_path": "sklt/project.sklt"}
        )
        self.assertTrue(ok)
        self.assertEqual(content["text"], "project AIRevit:\n")
        self.assertFalse((self.sklt_only / ".project").exists())

    def test_write_requires_explicit_writable_managed_root(self) -> None:
        # An empty allowlist denies before project-mode inspection.
        with self.assertRaisesRegex(PermissionError, "read-only"):
            SERVER._call("dungeon_refresh", {"project_root": str(self.sklt_only)})
        # An explicitly authorized SKLT_ONLY root reaches the separate mode guard.
        with patch.dict(os.environ, {"DUNGEON_WRITABLE_ROOTS": json.dumps([str(self.sklt_only)])}):
            with self.assertRaisesRegex(ValueError, "managed Dungeon project"):
                SERVER._call("dungeon_refresh", {"project_root": str(self.sklt_only)})
        with self.assertRaisesRegex(PermissionError, "explicit project_root"):
            SERVER._call("dungeon_refresh", {})
        with self.assertRaisesRegex(PermissionError, "read-only"):
            SERVER._call("dungeon_refresh", {"project_root": str(self.managed)})
        self.assertFalse((self.sklt_only / ".project").exists())

    def test_allowlisted_refresh_targets_only_the_selected_managed_root(self) -> None:
        with patch.dict(os.environ, {"DUNGEON_WRITABLE_ROOTS": json.dumps([str(self.managed)])}):
            with patch.object(SERVER, "_cli", return_value=({"status": "PASS"}, True)) as cli:
                result, ok = SERVER._call("dungeon_refresh", {"project_root": str(self.managed)})
        self.assertTrue(ok)
        self.assertEqual(result["status"], "PASS")
        self.assertEqual([call.args for call in cli.call_args_list], [
            (self.managed.resolve(), "refresh"),
            (self.managed.resolve(), "validate"),
        ])

    def test_future_exact_allowlist_does_not_broaden_or_deny_other_roots(self) -> None:
        future_root = self.managed.parent / "future-managed"
        self.assertFalse(future_root.exists())

        nested = self.managed / "nested-managed"
        (nested / ".project").mkdir(parents=True)
        (nested / ".project" / "project.json").write_text(
            json.dumps({"project_id": "nested.managed"}), encoding="utf-8"
        )
        before = {
            root: {
                path.relative_to(root).as_posix(): path.read_bytes()
                for path in root.rglob("*")
                if path.is_file()
            }
            for root in (self.home, self.managed)
        }

        # A not-yet-created exact entry must not invalidate a different exact,
        # existing authorization, and must not authorize neighboring roots.
        configured = json.dumps([str(future_root), str(self.managed)])
        with patch.dict(os.environ, {"DUNGEON_WRITABLE_ROOTS": configured}):
            with patch.object(SERVER, "_cli", return_value=({"status": "PASS"}, True)) as cli:
                result, ok = SERVER._call("dungeon_refresh", {"project_root": str(self.managed)})
            self.assertTrue(ok)
            self.assertEqual(result["status"], "PASS")
            self.assertEqual([call.args[1] for call in cli.call_args_list], ["refresh", "validate"])

            with self.assertRaises(ValueError):
                SERVER._call("dungeon_refresh", {"project_root": str(future_root)})
            with self.assertRaisesRegex(PermissionError, "read-only"):
                SERVER._call("dungeon_refresh", {"project_root": str(nested)})
            with self.assertRaisesRegex(PermissionError, "read-only"):
                SERVER._call("dungeon_refresh", {"project_root": str(self.home)})

        self.assertFalse(future_root.exists())
        for root, files in before.items():
            self.assertEqual(
                files,
                {
                    path.relative_to(root).as_posix(): path.read_bytes()
                    for path in root.rglob("*")
                    if path.is_file()
                },
            )

    def test_paths_cannot_escape_the_selected_project(self) -> None:
        with self.assertRaisesRegex(ValueError, "absolute directory path"):
            SERVER._call("dungeon_connect_project", {"project_root": "ai-revit"})
        with self.assertRaisesRegex(ValueError, "escapes Dungeon System root"):
            SERVER._call(
                "dungeon_read_sklt",
                {"project_root": str(self.sklt_only), "sklt_path": "../outside.sklt"},
            )
        with self.assertRaisesRegex(ValueError, "active project SKLT"):
            SERVER._call(
                "dungeon_read_sklt",
                {"project_root": str(self.sklt_only), "sklt_path": "snapshots/old.sklt"},
            )


if __name__ == "__main__":
    unittest.main()
