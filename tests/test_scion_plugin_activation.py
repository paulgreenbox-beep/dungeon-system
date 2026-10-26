from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from tools.scion_runtime.plugin_activation import (
    ActivationPlanError,
    prepare_launcher_verification,
    snapshot_installed_cache,
    verify_snapshot,
)


class ScionPluginActivationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.temp_root = Path(self.temp_dir.name)
        self.repository = self.temp_root / "repository"
        self.repository.mkdir()
        (self.repository / ".gitignore").write_text(
            "/tools/codex-plugin/backups/\n", encoding="utf-8"
        )
        self.profile = self.temp_root / "profile"
        self.cache = self.profile / ".codex" / "plugins" / "cache" / "personal" / "dungeon-system" / "0.4.1"
        (self.cache / ".codex-plugin").mkdir(parents=True)
        (self.cache / "mcp").mkdir()
        (self.cache / "scripts").mkdir()
        (self.cache / ".codex-plugin" / "plugin.json").write_text(
            json.dumps({"name": "dungeon-system", "version": "0.4.1"}), encoding="utf-8"
        )
        (self.cache / ".mcp.json").write_text(json.dumps({
            "mcpServers": {"dungeon-system": {
                "command": "cmd.exe",
                "args": ["/d", "/c", "scripts\\launch_mcp.cmd"],
                "cwd": ".",
                "env_vars": ["DUNGEON_PROJECT_ROOT", "DUNGEON_PYTHON"],
                "env": {"DUNGEON_WRITABLE_ROOTS": "[]"},
            }}
        }), encoding="utf-8")
        (self.cache / "mcp" / "server.py").write_text('VERSION = "0.4.1"\n', encoding="utf-8")
        (self.cache / "scripts" / "launch_mcp.cmd").write_text(
            '@echo off\n'
            'set "DUNGEON_SERVER=%~dp0..\\mcp\\server.py"\n'
            'if defined DUNGEON_PYTHON goto :configured_probe\n'
            'python -c "import sys; raise SystemExit(sys.version_info < (3, 11))"\n',
            encoding="utf-8",
        )
        (self.cache / "README.md").write_bytes(b"package-only bytes\x00\x01")
        self.global_config = self.profile / ".codex" / "config.toml"
        self.global_config.parent.mkdir(parents=True, exist_ok=True)
        self.global_config.write_text('token = "test_mock_scion_plugin_activation_canary"\n', encoding="utf-8")

    def tearDown(self) -> None:
        from tests.fixture_cleanup import cleanup_temporary_directory
        cleanup_temporary_directory(self.temp_dir)

    def test_prepares_static_launcher_and_fresh_session_gate_without_launching(self) -> None:
        plan = prepare_launcher_verification(self.cache)
        self.assertEqual(plan["marketplace"], "personal")
        self.assertEqual(plan["installed_version"], "0.4.1")
        self.assertEqual(plan["target_version"], "0.4.2-rc1")
        self.assertEqual(plan["command"], ["cmd.exe", "/d", "/c", "scripts\\launch_mcp.cmd"])
        self.assertEqual(plan["static_status"], "PASS")
        self.assertEqual(plan["environment_forwarding_mode"], "allowlist")
        gate = plan["live_session_gate"]
        self.assertEqual(gate["status"], "NOT_RUN")
        self.assertEqual(gate["expected_tool_count"], 34)
        self.assertEqual(gate["required_experimental_tools"], [
            "dungeon_vcs_status", "dungeon_vcs_inspect", "dungeon_vcs_history", "dungeon_vcs_diff"
        ])
        self.assertEqual(gate["protocol_sequence"][0]["method"], "initialize")
        self.assertEqual(gate["protocol_sequence"][1]["method"], "notifications/initialized")
        self.assertEqual(gate["protocol_sequence"][2]["params"]["_meta"]["progressToken"], 1)
        self.assertTrue(gate["requires_fresh_codex_conversation"])
        self.assertFalse(gate["live_client_validation_performed"])
        self.assertNotIn("must-not-be-read-or-copied", json.dumps(plan))

    def test_snapshot_is_copy_only_unique_and_hash_verified(self) -> None:
        original = {path.relative_to(self.cache).as_posix(): path.read_bytes()
                    for path in self.cache.rglob("*") if path.is_file()}
        moment = datetime(2026, 10, 16, 12, 30, tzinfo=timezone.utc)
        first_manifest = snapshot_installed_cache(self.repository, self.cache, created_at=moment)
        first = verify_snapshot(first_manifest.parent)
        self.assertTrue(first["valid"])
        self.assertEqual(first["identity"], {
            "marketplace": "personal", "plugin": "dungeon-system", "installed_version": "0.4.1"
        })
        self.assertEqual(first["file_count"], len(original))
        copied = first_manifest.parent / "installed-cache"
        self.assertEqual({path.relative_to(copied).as_posix(): path.read_bytes()
                          for path in copied.rglob("*") if path.is_file()}, original)
        self.assertEqual({path.relative_to(self.cache).as_posix(): path.read_bytes()
                          for path in self.cache.rglob("*") if path.is_file()}, original)
        self.assertFalse((first_manifest.parent / "installed-cache" / "config.toml").exists())
        self.assertEqual(self.global_config.read_text(encoding="utf-8"), 'token = "test_mock_scion_plugin_activation_canary"\n')

        second_manifest = snapshot_installed_cache(self.repository, self.cache, created_at=moment)
        self.assertNotEqual(first_manifest.parent, second_manifest.parent)
        self.assertTrue(verify_snapshot(second_manifest.parent)["valid"])

    def test_snapshot_verifier_detects_byte_changes(self) -> None:
        manifest_path = snapshot_installed_cache(
            self.repository,
            self.cache,
            created_at=datetime(2026, 10, 16, 12, 30, tzinfo=timezone.utc),
        )
        changed = manifest_path.parent / "installed-cache" / "README.md"
        changed.write_bytes(b"changed")
        with self.assertRaisesRegex(ActivationPlanError, "do not match"):
            verify_snapshot(manifest_path.parent)

    def test_rejects_wrong_identity_manifest_version_and_launcher(self) -> None:
        with self.assertRaisesRegex(ActivationPlanError, "cache path"):
            prepare_launcher_verification(self.cache, marketplace="dungeon-system-source")

        manifest_path = self.cache / ".codex-plugin" / "plugin.json"
        manifest_path.write_text(json.dumps({"name": "other", "version": "0.4.1"}), encoding="utf-8")
        with self.assertRaisesRegex(ActivationPlanError, "identity/version"):
            prepare_launcher_verification(self.cache)

        manifest_path.write_text(json.dumps({"name": "dungeon-system", "version": "0.4.1"}), encoding="utf-8")
        launcher_path = self.cache / "scripts" / "launch_mcp.cmd"
        launcher_path.write_text("@echo off\n", encoding="utf-8")
        plan = prepare_launcher_verification(self.cache)
        self.assertEqual(plan["static_status"], "PARTIAL")
        self.assertEqual(plan["static_checks"]["launcher_targets_mcp_server"], "FAIL")
        self.assertEqual(plan["static_checks"]["python_3_11_or_newer_guard"], "FAIL")

    def test_snapshot_requires_explicit_ignored_backup_path(self) -> None:
        (self.repository / ".gitignore").write_text("# no backup ignore rule\n", encoding="utf-8")
        with self.assertRaisesRegex(ActivationPlanError, "explicitly ignore"):
            snapshot_installed_cache(
                self.repository,
                self.cache,
                created_at=datetime(2026, 10, 16, 12, 30, tzinfo=timezone.utc),
            )
        self.assertFalse((self.repository / "tools" / "codex-plugin" / "backups").exists())

    def test_rejects_raw_traversal_and_unc_before_path_resolution(self) -> None:
        traversal_cache = Path(str(self.cache.parent / ".." / self.cache.name))
        with self.assertRaisesRegex(ActivationPlanError, "raw parent traversal"):
            prepare_launcher_verification(traversal_cache)

        traversal_repository = Path(str(self.repository.parent / ".." / self.repository.name))
        with self.assertRaisesRegex(ActivationPlanError, "raw parent traversal"):
            snapshot_installed_cache(traversal_repository, self.cache)

        unc_cache = Path(r"\\server\share\plugins\cache\personal\dungeon-system\0.4.1")
        with self.assertRaisesRegex(ActivationPlanError, "UNC or device"):
            prepare_launcher_verification(unc_cache)

        with self.assertRaisesRegex(ActivationPlanError, "UNC or device"):
            verify_snapshot(Path(r"\\server\share\backup"))

    def test_rejects_symlink_ancestors_for_cache_and_backup_paths(self) -> None:
        cache_alias = self.temp_root / "cache-ancestor-link"
        try:
            os.symlink(self.profile / ".codex", cache_alias, target_is_directory=True)
        except (OSError, NotImplementedError) as exc:
            self.skipTest(f"directory symlink creation is unavailable: {exc}")
        with self.assertRaisesRegex(ActivationPlanError, "link or reparse-point ancestor"):
            prepare_launcher_verification(
                cache_alias / "plugins" / "cache" / "personal" / "dungeon-system" / "0.4.1"
            )

        linked_repository = self.temp_root / "repository-with-linked-tools"
        linked_repository.mkdir()
        (linked_repository / ".gitignore").write_text(
            "/tools/codex-plugin/backups/\n", encoding="utf-8"
        )
        external = self.temp_root / "outside-repository"
        external.mkdir()
        try:
            os.symlink(external, linked_repository / "tools", target_is_directory=True)
        except (OSError, NotImplementedError) as exc:
            self.skipTest(f"directory symlink creation is unavailable: {exc}")
        with self.assertRaisesRegex(ActivationPlanError, "link or reparse-point ancestor"):
            snapshot_installed_cache(linked_repository, self.cache)
        self.assertEqual(list(external.iterdir()), [])

    def test_ancestor_reparse_guards_do_not_depend_on_symlink_privilege(self) -> None:
        import tools.scion_runtime.plugin_activation as activation

        cache_ancestor = self.profile / ".codex"
        with patch.object(activation, "_is_link", side_effect=lambda path: path == cache_ancestor):
            with self.assertRaisesRegex(ActivationPlanError, "link or reparse-point ancestor"):
                prepare_launcher_verification(self.cache)

        backup_ancestor = self.repository / "tools"
        with patch.object(activation, "_is_link", side_effect=lambda path: path == backup_ancestor):
            with self.assertRaisesRegex(ActivationPlanError, "link or reparse-point ancestor"):
                snapshot_installed_cache(self.repository, self.cache)
        self.assertFalse(backup_ancestor.exists())


if __name__ == "__main__":
    unittest.main()
