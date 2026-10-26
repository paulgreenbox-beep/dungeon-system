"""CLI and MCP transport contracts for the experimental VCS query API."""

from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import Mock, patch


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY_ROOT / "tools"))

from tools.project_structure import cli


SERVER_PATH = REPOSITORY_ROOT / "tools" / "codex-plugin" / "dungeon-system" / "mcp" / "server.py"
VCS_TOOL_NAMES = {
    "dungeon_vcs_status",
    "dungeon_vcs_inspect",
    "dungeon_vcs_history",
    "dungeon_vcs_diff",
}


def load_server():
    name = "dungeon_vcs_transport_server"
    cached = sys.modules.get(name)
    if cached is not None:
        return cached
    spec = importlib.util.spec_from_file_location(name, SERVER_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError("MCP server module could not be loaded")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


class VcsTransportTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="dungeon-vcs-transport-")
        from tests.fixture_cleanup import cleanup_temporary_directory

        self.addCleanup(cleanup_temporary_directory, temporary)
        self.root = Path(temporary.name).resolve()

    def install_service_stub(self, package_name: str, result: dict):
        module = ModuleType(f"{package_name}.vcs_service")
        module.vcs_query = Mock(return_value=result)
        return patch.dict(sys.modules, {module.__name__: module}), module.vcs_query

    def managed_root(self) -> Path:
        project_dir = self.root / ".project"
        project_dir.mkdir(exist_ok=True)
        manifest = project_dir / "project.json"
        manifest.write_text(json.dumps({"schema_version": 1, "project_id": "fixture.vcs", "modules": []}), encoding="utf-8")
        return self.root

    def test_cli_routes_all_six_operations_to_service_without_writes(self):
        cases = (
            ("vcs-status", [], "status", None, None, 20, None, None),
            ("vcs-inspect", ["--scope", "main", "--revision", "abc123"], "inspect", "main", "abc123", 20, None, None),
            ("vcs-history", ["--scope", "main", "--revision", "HEAD", "--limit", "7"], "history", "main", "HEAD", 7, None, None),
            ("vcs-diff", ["--scope", "engine"], "diff", "engine", None, 20, None, None),
            ("vcs-sparse-plan", ["--scope", "engine", "--object-id", "render.canvas"], "sparse-plan", "engine", None, 20, "render.canvas", None),
            ("vcs-migration-plan", ["--scope", "main", "--target-backend", "SCION"], "migration-plan", "main", None, 20, None, "SCION"),
        )
        response = {"status": "PARTIAL", "issues": [{"code": "REVIEW", "message": "review required"}], "entries": []}
        for command, options, operation, scope, revision, limit, object_id, target_backend in cases:
            with self.subTest(command=command):
                before = sorted(path.relative_to(self.root).as_posix() for path in self.root.rglob("*"))
                context, query = self.install_service_stub(cli.__package__, response)
                with context, contextlib.redirect_stdout(io.StringIO()) as output:
                    exit_code = cli.main([command, *options, "--root", str(self.root), "--json"])
                self.assertEqual(exit_code, 1)
                self.assertEqual(json.loads(output.getvalue()), response)
                query.assert_called_once_with(
                    self.root,
                    operation,
                    scope_id=scope,
                    revision=revision,
                    limit=limit,
                    object_id=object_id,
                    target_backend=target_backend,
                )
                after = sorted(path.relative_to(self.root).as_posix() for path in self.root.rglob("*"))
                self.assertEqual(after, before)

    def test_cli_exit_code_is_zero_only_for_pass(self):
        for status, expected_exit in (("PASS", 0), ("PARTIAL", 1), ("FAIL", 1)):
            with self.subTest(status=status):
                context, _ = self.install_service_stub(
                    cli.__package__, {"status": status, "issues": []},
                )
                with context, contextlib.redirect_stdout(io.StringIO()):
                    exit_code = cli.main(["vcs-status", "--root", str(self.root), "--json"])
                self.assertEqual(exit_code, expected_exit)

    def test_cli_rejects_out_of_range_history_and_command_specific_arguments(self):
        parser = cli.build_parser()
        for argv in (
            ["vcs-history", "--limit", "0"],
            ["vcs-history", "--limit", "101"],
            ["vcs-status", "--revision", "HEAD"],
            ["vcs-diff", "--revision", "HEAD"],
            ["vcs-sparse-plan"],
            ["vcs-migration-plan"],
            ["vcs-migration-plan", "--target-backend", "OTHER"],
        ):
            with self.subTest(argv=argv), contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit):
                    parser.parse_args(argv)

    def test_stdio_discovery_exposes_exactly_34_tools_and_marks_vcs_read_only(self):
        request = {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}
        environment = os.environ.copy()
        environment["DUNGEON_PROJECT_ROOT"] = str(REPOSITORY_ROOT)
        result = subprocess.run(
            [sys.executable, str(SERVER_PATH)],
            input=json.dumps(request) + "\n",
            text=True,
            capture_output=True,
            cwd=REPOSITORY_ROOT,
            env=environment,
            timeout=15,
            check=True,
        )
        response = json.loads(result.stdout)
        tools = response["result"]["tools"]
        self.assertEqual(len(tools), 34)
        self.assertEqual(len({tool["name"] for tool in tools}), 34)
        by_name = {tool["name"]: tool for tool in tools}
        self.assertTrue(VCS_TOOL_NAMES <= by_name.keys())
        for tool in tools:
            self.assertIsInstance(tool["annotations"]["readOnlyHint"], bool)
        for name in VCS_TOOL_NAMES:
            tool = by_name[name]
            self.assertTrue(tool["annotations"]["readOnlyHint"])
            self.assertFalse(tool["inputSchema"]["additionalProperties"])
            self.assertIn("project_root", tool["inputSchema"]["required"])
        self.assertEqual(by_name["dungeon_vcs_history"]["inputSchema"]["properties"]["limit"], {
            "type": "integer", "minimum": 1, "maximum": 100,
        })
        self.assertEqual(by_name["dungeon_vcs_status"]["inputSchema"]["properties"]["scope_id"], {
            "type": "string", "minLength": 1, "maxLength": 128,
        })
        for name in ("dungeon_vcs_inspect", "dungeon_vcs_history"):
            self.assertEqual(by_name[name]["inputSchema"]["properties"]["revision"], {
                "type": "string", "minLength": 1, "maxLength": 256,
            })

    def test_mcp_calls_service_for_managed_root_and_preserves_partial_result(self):
        root = self.managed_root()
        manifest_before = (root / ".project/project.json").read_bytes()
        result = {"status": "PARTIAL", "issues": [{"code": "BOUNDARY", "message": "partial evidence"}], "history": []}
        server = load_server()
        context, query = self.install_service_stub("project_structure", result)
        fake_projects = SimpleNamespace(project_probe=lambda selected_root: {"mode": "MANAGED"})
        with context, patch.object(server, "_core", return_value=(fake_projects, object())):
            payload, ok = server._call("dungeon_vcs_history", {
                "project_root": str(root), "scope_id": "main", "revision": "HEAD", "limit": 5,
            })
        self.assertTrue(ok)
        self.assertEqual(payload["status"], "PARTIAL")
        self.assertEqual(payload["issues"], result["issues"])
        self.assertEqual(payload["project_root"], str(root))
        self.assertEqual(payload["project_mode"], "DUNGEON")
        query.assert_called_once_with(
            root, "history", scope_id="main", revision="HEAD", limit=5,
        )
        wrapped = server._tool_result(payload, ok)
        self.assertFalse(wrapped["isError"])
        self.assertEqual(wrapped["structuredContent"]["status"], "PARTIAL")
        self.assertEqual((root / ".project/project.json").read_bytes(), manifest_before)

    def test_mcp_transport_rejects_traversal_before_query(self):
        server = load_server()
        context, query = self.install_service_stub("project_structure", {"status": "PASS", "issues": []})
        request = {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {
                "name": "dungeon_vcs_status",
                "arguments": {"project_root": str(self.root / ".." / "outside")},
            },
        }
        with context:
            response = server._handle(request)
        result = response["result"]
        self.assertTrue(result["isError"])
        self.assertEqual(result["structuredContent"]["issues"][0]["code"], "UNSAFE_OUTPUT_PATH")
        query.assert_not_called()

    def test_mcp_returns_safe_configuration_issue_outside_managed_mode(self):
        server = load_server()
        context, query = self.install_service_stub("project_structure", {"status": "PASS", "issues": []})
        with context:
            payload, ok = server._call("dungeon_vcs_status", {"project_root": str(self.root)})
        self.assertFalse(ok)
        self.assertEqual(payload["status"], "FAIL")
        self.assertEqual(payload["project_mode"], "NOT_DUNGEON")
        self.assertEqual(payload["issues"][0]["code"], "VCS_REQUIRES_MANAGED_PROJECT")
        query.assert_not_called()

    def test_mcp_schema_rejects_boolean_history_limit(self):
        server = load_server()
        with self.assertRaisesRegex(ValueError, "limit must be integer"):
            server._call("dungeon_vcs_history", {
                "project_root": str(self.root), "limit": True,
            })


if __name__ == "__main__":
    unittest.main()
