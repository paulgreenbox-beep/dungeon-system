"""v0.4.1 transport, authorization, and source-bridge regression tests."""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


REPO_ROOT = Path(__file__).resolve().parents[4]
SERVER_PATH = Path(__file__).resolve().parents[1] / "mcp" / "server.py"
SPEC = importlib.util.spec_from_file_location("dungeon_plugin_server_v041_test", SERVER_PATH)
assert SPEC and SPEC.loader
SERVER = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = SERVER
SPEC.loader.exec_module(SERVER)

EXPECTED_TOOLS = {
    "dungeon_project_probe", "dungeon_adoption_plan", "dungeon_adoption_apply",
    "dungeon_project_summary", "dungeon_list_modules", "dungeon_source_link_audit",
    "dungeon_task_trace_get", "dungeon_task_trace_list", "dungeon_task_trace_write",
    "dungeon_coverage_get", "dungeon_coverage_set", "dungeon_connect_project",
    "dungeon_status", "dungeon_orientation", "dungeon_list_sklt", "dungeon_read_sklt",
    "dungeon_list_objects", "dungeon_inspect_object", "dungeon_validate", "dungeon_refresh",
    "dungeon_snapshot", "dungeon_snapshot_diff", "dungeon_evidence_for",
    "dungeon_evidence_inspect", "dungeon_evidence_verify", "dungeon_evidence_create",
    "dungeon_evidence_seal", "dungeon_cas_status", "dungeon_cas_verify", "dungeon_cas_put",
    "dungeon_vcs_status", "dungeon_vcs_inspect", "dungeon_vcs_history", "dungeon_vcs_diff",
}
WRITE_TOOLS = {
    "dungeon_adoption_apply", "dungeon_task_trace_write", "dungeon_coverage_set",
    "dungeon_refresh", "dungeon_snapshot", "dungeon_evidence_create",
    "dungeon_evidence_seal", "dungeon_cas_put",
}


def rpc(method: str, request_id: int = 1, params: dict | None = None) -> dict:
    request = {"jsonrpc": "2.0", "id": request_id, "method": method}
    if params is not None:
        request["params"] = params
    return request


class V041TransportTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="dungeon-v041-")
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)

    def wire(self, requests: list[dict | str], *, writable_roots: str = "[]") -> list[dict]:
        body = "".join(
            (request if isinstance(request, str) else json.dumps(request, ensure_ascii=False)) + "\n"
            for request in requests
        )
        env = {
            **os.environ,
            "DUNGEON_PROJECT_ROOT": str(REPO_ROOT),
            "DUNGEON_WRITABLE_ROOTS": writable_roots,
        }
        process = subprocess.run(
            [sys.executable, "-B", str(SERVER_PATH)],
            input=body,
            capture_output=True,
            text=True,
            encoding="utf-8",
            env=env,
            timeout=40,
        )
        self.assertEqual(process.returncode, 0, process.stderr)
        return [json.loads(line) for line in process.stdout.splitlines()]

    def test_jsonrpc_rejects_malformed_missing_and_non_2_0_envelopes(self) -> None:
        replies = self.wire([
            '{"jsonrpc":"2.0","id":1,"method":',
            json.dumps({"id": 2, "method": "ping"}),
            rpc("ping", 3) | {"jsonrpc": "1.0"},
            rpc("ping", 4) | {"extra": True},
        ])
        self.assertEqual([item["error"]["code"] for item in replies], [-32700, -32600, -32600, -32600])
        self.assertEqual([item["id"] for item in replies], [None, 2, 3, 4])

    def test_params_and_tool_arguments_must_be_objects(self) -> None:
        replies = self.wire([
            rpc("ping", 1, []),
            rpc("tools/call", 2, {"name": "dungeon_project_probe", "arguments": []}),
            rpc("tools/call", 3, {"name": "dungeon_project_probe", "arguments": None}),
        ])
        self.assertEqual([item["error"]["code"] for item in replies], [-32602, -32602, -32602])
        self.assertTrue(all("object" in item["error"]["message"] for item in replies))

    def test_unknown_schema_fields_tools_and_methods_fail_closed(self) -> None:
        root = self.base / "plain"
        root.mkdir()
        replies = self.wire([
            rpc("tools/call", 1, {"name": "dungeon_project_probe", "arguments": {
                "project_root": str(root), "surprise": True,
            }}),
            rpc("tools/call", 2, {"name": "dungeon_not_a_tool", "arguments": {}}),
            rpc("tools/call", 3, {"name": "dungeon_project_probe", "arguments": {}, "surprise": True}),
            rpc("tools/not-a-method", 4),
        ])
        self.assertEqual(replies[0]["result"]["structuredContent"]["issues"][0]["code"], "INVALID_ARGUMENT")
        self.assertEqual(replies[1]["result"]["structuredContent"]["issues"][0]["code"], "INVALID_ARGUMENT")
        self.assertEqual(replies[2]["error"]["code"], -32602)
        self.assertEqual(replies[3]["error"]["code"], -32601)

    def test_normal_initialize_ping_list_and_read_chain_keep_all_30_tools(self) -> None:
        root = self.base / "plain"
        root.mkdir()
        requests = [
            rpc("initialize", 0, {
                "protocolVersion": "2025-11-25",
                "capabilities": {"experimental": {"codex/auth-change": {}}, "elicitation": {"form": {}, "url": {}}},
                "clientInfo": {"name": "v041-transport", "title": "Codex-shaped client", "version": "1"},
            }),
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            rpc("ping", 1),
            rpc("tools/list", 2, {"_meta": {"progressToken": 1}}),
            rpc("tools/call", 3, {"name": "dungeon_project_probe", "arguments": {"project_root": str(root)},
                                  "_meta": {"progressToken": "read-probe", "project_root": "C:/not-used"}}),
        ]
        replies = self.wire(requests)
        self.assertEqual([reply["id"] for reply in replies], [0, 1, 2, 3])
        self.assertEqual(replies[0]["result"]["serverInfo"]["version"], "0.4.2-rc1")
        self.assertEqual(replies[1]["result"], {})
        tools = replies[2]["result"]["tools"]
        names = {tool["name"] for tool in tools}
        self.assertEqual(names, EXPECTED_TOOLS)
        self.assertEqual(len(tools), 34)
        for tool in tools:
            self.assertFalse(tool["inputSchema"]["additionalProperties"])
            if tool["name"] in WRITE_TOOLS:
                self.assertIn("project_root", tool["inputSchema"]["required"])
        for reply in replies:
            self.assertLessEqual(len(json.dumps(reply, ensure_ascii=False).encode("utf-8")), SERVER.MAX_RESPONSE_BYTES)
        self.assertEqual(replies[3]["result"]["structuredContent"]["mode"], "NOT_DUNGEON")

    def test_standard_metadata_is_typed_and_does_not_relax_unknown_arguments(self) -> None:
        requests = [rpc("ping", index, {"_meta": value})
                    for index, value in enumerate((None, [], 1, "metadata"), 1)]
        requests += [rpc("ping", index, {"_meta": {"progressToken": value}})
                     for index, value in enumerate((True, None, [], {}), 5)]
        requests.append('{"jsonrpc":"2.0","id":9,"method":"ping","params":{"_meta":{"progressToken":1e999}}}')
        requests += [rpc("ping", index, {"_meta": {"progressToken": value, "vendor/opaque": {"ok": True}}})
                     for index, value in enumerate(("progress", 0, 1.5), 10)]
        requests.append(rpc("tools/call", 13, {"name": "dungeon_project_probe", "arguments": {
            "project_root": str(self.base), "_meta": {},
        }, "_meta": {"progressToken": 2}}))
        replies = self.wire(requests)
        self.assertEqual([reply["error"]["code"] for reply in replies[:9]], [-32602] * 9)
        self.assertEqual([reply["result"] for reply in replies[9:12]], [{}, {}, {}])
        self.assertEqual(replies[12]["result"]["structuredContent"]["issues"][0]["code"], "INVALID_ARGUMENT")

    def test_request_size_boolean_integer_and_limit_validation(self) -> None:
        root = self.base / "plain"
        root.mkdir()
        oversized = '{"jsonrpc":"2.0","id":9,"method":"ping"}' + (" " * (1_048_600))
        replies = self.wire([
            oversized,
            rpc("tools/call", 1, {"name": "dungeon_list_objects", "arguments": {
                "project_root": str(root), "limit": True,
            }}),
            rpc("tools/call", 2, {"name": "dungeon_list_objects", "arguments": {
                "project_root": str(root), "limit": 501,
            }}),
        ])
        self.assertEqual(replies[0]["error"]["code"], -32600)
        for reply in replies[1:]:
            self.assertTrue(reply["result"]["isError"])
            self.assertEqual(reply["result"]["structuredContent"]["issues"][0]["code"], "INVALID_ARGUMENT")

    def test_all_eight_writes_are_denied_for_empty_and_malformed_allowlists(self) -> None:
        arguments = {"project_root": str(REPO_ROOT), "unparsed_payload_field": {"bad": True}}
        requests = [
            rpc("tools/call", index, {"name": name, "arguments": arguments})
            for index, name in enumerate(sorted(WRITE_TOOLS), 1)
        ]
        for allowlist in ("[]", "not-json"):
            with self.subTest(allowlist=allowlist):
                replies = self.wire(requests, writable_roots=allowlist)
                self.assertEqual(len(replies), len(WRITE_TOOLS))
                for reply in replies:
                    content = reply["result"]["structuredContent"]
                    self.assertTrue(reply["result"]["isError"])
                    self.assertEqual(content["issues"][0]["code"], "PROJECT_READ_ONLY")

    def test_exact_root_authorization_and_atomic_refresh_cli_grouping(self) -> None:
        allowed = self.base / "allowed"
        sibling = self.base / "sibling"
        for root, project_id in ((allowed, "fixture.allowed"), (sibling, "fixture.sibling")):
            (root / ".project").mkdir(parents=True)
            (root / ".project" / "project.json").write_text(
                json.dumps({"project_id": project_id}), encoding="utf-8"
            )
        with patch.dict(os.environ, {
            "DUNGEON_PROJECT_ROOT": str(REPO_ROOT),
            "DUNGEON_WRITABLE_ROOTS": json.dumps([str(allowed)]),
        }):
            SERVER._backend_tools_path()
            from project_structure import v041_paths
            validated: list[str] = []

            def record_validation(root: Path) -> None:
                validated.append(os.fspath(root))

            with patch.object(v041_paths, "validate_root", side_effect=record_validation), patch.object(
                SERVER, "_cli", side_effect=[
                    ({"status": "PASS", "files_changed": [".project/state.json"]}, True),
                    ({"status": "PASS"}, True),
                ],
            ) as cli:
                result, ok = SERVER._call("dungeon_refresh", {"project_root": str(allowed)})
            self.assertTrue(ok)
            self.assertIn(str(allowed), validated)
            self.assertEqual([call.args for call in cli.call_args_list], [
                (allowed.resolve(), "refresh"), (allowed.resolve(), "validate"),
            ])
            self.assertEqual([step["command"] for step in result["steps"]], ["refresh", "validate"])
            with patch.object(v041_paths, "validate_root", side_effect=record_validation):
                with self.assertRaises(PermissionError):
                    SERVER._call("dungeon_refresh", {"project_root": str(sibling)})

    def test_raw_traversal_is_structured_unsafe_and_declared_ids_use_shared_grammar(self) -> None:
        root = self.base / "sklt-only"
        root.mkdir()
        (root / "nested.sklt").write_text(
            "project Demo:\n    module UI:\n        capability Draw:\n",
            encoding="utf-8",
        )
        alias = str(root.parent / "missing" / ".." / root.name)
        unsafe = SERVER._handle(rpc("tools/call", 1, {
            "name": "dungeon_project_probe", "arguments": {"project_root": alias},
        }))
        self.assertEqual(
            unsafe["result"]["structuredContent"]["issues"][0]["code"],
            "UNSAFE_OUTPUT_PATH",
        )

        listing, ok = SERVER._call("dungeon_list_objects", {"project_root": str(root)})
        self.assertTrue(ok)
        declared_id = "declared:project:Demo/module:UI/capability:Draw"
        self.assertIn(declared_id, {item["object_id"] for item in listing["objects"]})
        inspected, ok = SERVER._call("dungeon_inspect_object", {
            "project_root": str(root), "object_id": declared_id,
        })
        self.assertTrue(ok)
        self.assertEqual(inspected["object_id"], declared_id)
        with self.assertRaisesRegex(ValueError, "logical object ID"):
            SERVER._call("dungeon_inspect_object", {
                "project_root": str(root), "object_id": "declared:project:Demo/filename.sklt",
            })

    def test_backend_unsafe_path_issue_is_not_downgraded(self) -> None:
        result = SERVER._tool_result({
            "status": "FAIL",
            "issues": [{"code": "OPERATION_FAILED", "message": "UnsafeOutputPath: UNSAFE_OUTPUT_PATH: rejected"}],
        }, False)
        self.assertEqual(result["structuredContent"]["issues"][0]["code"], "UNSAFE_OUTPUT_PATH")
        self.assertTrue(result["isError"])
        bounded = SERVER._tool_result({"status": "PASS", "payload": "x" * 150_000}, True)
        self.assertTrue(bounded["isError"])
        self.assertEqual(bounded["structuredContent"]["issues"][0]["code"], "RESPONSE_TOO_LARGE")
        self.assertLessEqual(len(json.dumps(bounded, ensure_ascii=False).encode("utf-8")), SERVER.MAX_RESPONSE_BYTES)


if __name__ == "__main__":
    unittest.main()
