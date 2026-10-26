"""Integration proof through a real stdio JSON-RPC MCP client."""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


SERVER = Path(__file__).resolve().parents[1] / "mcp" / "server.py"
DUNGEON_ROOT = Path(__file__).resolve().parents[4]


def skeleton(identity: str) -> str:
    return f"""id: {identity}
object_id: {identity}
kind: component
responsibility: Explicit temporary test fixture
inputs:
outputs:
depends_on:
effects:
state: SKLT
source:
    path: src/main.rs symbol: main
"""


def tree(root: Path) -> dict:
    return {path.relative_to(root).as_posix(): path.read_bytes() for path in root.rglob("*") if path.is_file()}


class McpV04Integration(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="dungeon-v04-")
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)

    def client(self, calls: list[tuple[str, dict]], writable: list[Path] | None = None, extra_env: dict | None = None) -> list[dict]:
        requests = [{"jsonrpc": "2.0", "id": 0, "method": "initialize", "params": {
            "protocolVersion": "2025-11-25", "capabilities": {}, "clientInfo": {"name": "v04-integration", "version": "1"}}},
            {"jsonrpc": "2.0", "method": "notifications/initialized"}]
        requests.extend({"jsonrpc": "2.0", "id": number, "method": "tools/call", "params": {
            "name": name, "arguments": arguments}} for number, (name, arguments) in enumerate(calls, 1))
        env = {**os.environ, "DUNGEON_PROJECT_ROOT": str(DUNGEON_ROOT),
               "DUNGEON_WRITABLE_ROOTS": json.dumps([str(root) for root in writable or []]), **(extra_env or {})}
        process = subprocess.run([sys.executable, "-B", str(SERVER)],
                                 input="".join(json.dumps(request, ensure_ascii=False) + "\n" for request in requests),
                                 capture_output=True, text=True, encoding="utf-8", env=env, timeout=40)
        self.assertEqual(process.returncode, 0, process.stderr)
        replies = [json.loads(line) for line in process.stdout.splitlines()]
        self.assertEqual(len(replies), len(calls) + 1)
        manifest = json.loads((SERVER.parents[1] / ".codex-plugin/plugin.json").read_text(encoding="utf-8"))
        self.assertEqual(replies[0]["result"]["serverInfo"]["version"], manifest["version"])
        for reply in replies[1:]:
            self.assertIn("structuredContent", reply["result"])
            self.assertIn("issues", reply["result"]["structuredContent"])
            self.assertIn("next_steps", reply["result"]["structuredContent"])
            self.assertEqual(json.loads(reply["result"]["content"][0]["text"]), reply["result"]["structuredContent"])
        return [reply["result"] for reply in replies[1:]]

    def test_ordinary_repository_has_actionable_read_only_diagnosis(self) -> None:
        root = self.base / "обычный repo"
        (root / "src").mkdir(parents=True)
        (root / "src" / "main.rs").write_text("fn main() {}", encoding="utf-8")
        before = tree(root)
        replies = self.client([
            ("dungeon_project_probe", {"project_root": str(root)}),
            ("dungeon_connect_project", {"project_root": str(root)}),
            ("dungeon_adoption_plan", {"project_root": str(root)}),
        ])
        self.assertEqual(replies[0]["structuredContent"]["mode"], "NOT_DUNGEON")
        self.assertEqual(replies[0]["structuredContent"]["sklt_count"], 0)
        self.assertTrue(replies[0]["structuredContent"]["can_adopt"])
        self.assertTrue(replies[0]["structuredContent"]["next_steps"])
        self.assertEqual(replies[1]["structuredContent"]["mode"], "NOT_DUNGEON")
        self.assertFalse(replies[2]["isError"])
        self.assertEqual(before, tree(root))

    def test_over_100_sklts_are_paginated_and_inspected_without_an_index(self) -> None:
        root = self.base / "sklt-project"
        (root / "module").mkdir(parents=True)
        (root / "src").mkdir()
        (root / "src" / "main.rs").write_text("fn main() {}", encoding="utf-8")
        for number in range(121):
            (root / "module" / f"{number:03}.sklt").write_text(skeleton(f"object.{number:03}"), encoding="utf-8")
        before = tree(root)
        replies = self.client([
            ("dungeon_project_probe", {"project_root": str(root)}),
            ("dungeon_list_objects", {"project_root": str(root), "offset": 100, "limit": 10}),
            ("dungeon_inspect_object", {"project_root": str(root), "object_id": "object.100", "max_chars": 80}),
            ("dungeon_source_link_audit", {"project_root": str(root), "object_ids": ["object.100"]}),
        ])
        self.assertEqual(replies[0]["structuredContent"]["sklt_count"], 121)
        page = replies[1]["structuredContent"]
        self.assertEqual(len(page["objects"]), 10)
        self.assertEqual(page["next_offset"], 110)
        self.assertEqual(page["total"], 121)
        self.assertEqual(len(replies[2]["structuredContent"]["skeleton"]["text"]), 80)
        self.assertTrue(replies[2]["structuredContent"]["skeleton"]["truncated"])
        for reply in replies:
            self.assertFalse(reply["isError"], reply)
        self.assertFalse((root / ".project").exists())
        self.assertEqual(before, tree(root))

    def test_invalid_and_unavailable_modes_and_structured_argument_errors(self) -> None:
        root = self.base / "broken"
        (root / ".project").mkdir(parents=True)
        (root / ".project" / "project.json").write_text("{broken", encoding="utf-8")
        replies = self.client([
            ("dungeon_project_probe", {"project_root": str(root)}),
            ("dungeon_project_probe", {"project_root": str(self.base / "missing")}),
            ("dungeon_list_objects", {"project_root": str(root), "limit": True}),
        ])
        self.assertEqual(replies[0]["structuredContent"]["mode"], "INVALID_MANAGED")
        self.assertEqual(replies[1]["structuredContent"]["mode"], "UNAVAILABLE")
        self.assertTrue(replies[2]["isError"])
        self.assertEqual(replies[2]["structuredContent"]["issues"][0]["code"], "INVALID_ARGUMENT")

    def test_legacy_managed_status_and_default_write_guard(self) -> None:
        replies = self.client([
            ("dungeon_status", {"project_root": str(DUNGEON_ROOT)}),
            ("dungeon_refresh", {}),
        ])
        status = replies[0]["structuredContent"]
        self.assertFalse(replies[0]["isError"], status)
        self.assertEqual(status["status"], "PASS", status)
        self.assertEqual(status["validation"], "PASS", status)
        self.assertEqual(status["mode"], "MANAGED", status)
        self.assertEqual(status["project_mode"], "DUNGEON", status)
        self.assertEqual(status["objects"], 22, status)
        self.assertEqual(status["modules"], 2, status)
        self.assertEqual(status["errors"], 0, status)
        self.assertFalse(
            any(issue.get("code") == "INVALID_SKLT_SYNTAX" for issue in status.get("issues", [])),
            status,
        )
        self.assertTrue(replies[1]["isError"])
        self.assertIn(
            "PROJECT_READ_ONLY",
            [issue.get("code") for issue in replies[1]["structuredContent"].get("issues", [])],
            replies[1],
        )

    def test_explicit_adoption_preserves_ids_and_replay_changes_nothing(self) -> None:
        root = self.base / "adoption"
        (root / "renderer").mkdir(parents=True)
        original = skeleton("renderer.stable")
        sklt = root / "renderer" / "renamed.sklt"
        sklt.write_text(original, encoding="utf-8")
        plan = self.client([("dungeon_adoption_plan", {"project_root": str(root)})])[0]["structuredContent"]
        arguments = {"project_root": str(root), "plan": plan,
                     "accepted_modules": [item["id"] for item in plan["candidate_modules"]]}
        rejected = self.client([("dungeon_adoption_apply", arguments)])[0]
        self.assertTrue(rejected["isError"])
        self.assertFalse((root / ".project").exists())
        applied = self.client([("dungeon_adoption_apply", arguments)], [root])[0]
        self.assertFalse(applied["isError"], applied)
        self.assertTrue(applied["structuredContent"]["applied"])
        before_replay = tree(root)
        replay, inspect = self.client([
            ("dungeon_adoption_apply", arguments),
            ("dungeon_inspect_object", {"project_root": str(root), "object_id": "renderer.stable"}),
        ], [root])
        self.assertFalse(replay["isError"], replay)
        self.assertEqual(replay["structuredContent"]["result"], "ALREADY_APPLIED")
        self.assertEqual(before_replay, tree(root))
        self.assertEqual(sklt.read_text(encoding="utf-8"), original)
        self.assertEqual(inspect["structuredContent"]["object_id"], "renderer.stable")
        self.assertFalse((root / ".git").exists())

    def test_task_and_separate_coverage_on_sklt_only_through_mcp(self) -> None:
        root = self.base / "traces"
        (root / "module").mkdir(parents=True)
        (root / "module" / "x.sklt").write_text(skeleton("render.cuda"), encoding="utf-8")
        trace = {"task_id": "RTX-0042", "object_ids": ["render.cuda"], "scope": "PROTOTYPE",
                 "assigned_agent": "external://Tech_agent", "source_refs": [], "test_refs": [],
                 "required_checks": ["smoke"], "performed_checks": [], "evidence_refs": [],
                 "status": "IN_PROGRESS", "actual_result": {"note": "Fixture review; no execution proof"}}
        replies = self.client([
            ("dungeon_task_trace_write", {"project_root": str(root), "trace": trace}),
            ("dungeon_task_trace_get", {"project_root": str(root), "task_id": "RTX-0042"}),
            ("dungeon_task_trace_list", {"project_root": str(root), "object_id": "render.cuda", "limit": 1}),
            ("dungeon_coverage_set", {"project_root": str(root), "object_id": "render.cuda", "scope": "PROTOTYPE",
                                      "state": "PARTIAL", "rationale": "Explicit fixture source review", "task_id": "RTX-0042"}),
            ("dungeon_coverage_get", {"project_root": str(root), "object_id": "render.cuda"}),
        ], [root])
        for reply in replies:
            self.assertFalse(reply["isError"], reply)
        self.assertEqual(replies[1]["structuredContent"]["trace"]["task_id"], "RTX-0042")
        self.assertEqual(replies[2]["structuredContent"]["total"], 1)
        coverage = replies[4]["structuredContent"]["coverage"]["render.cuda"]
        self.assertEqual(coverage["PROTOTYPE"]["state"], "PARTIAL")
        self.assertEqual(coverage["PRODUCT"]["state"], "UNKNOWN")
        self.assertEqual(coverage["structural_state"], "SKLT")
        self.assertFalse((root / ".project" / "project.json").exists())
        self.assertFalse((root / ".project" / "objects.json").exists())

    def test_json_cli_and_discoverable_tool_contracts(self) -> None:
        root = self.base / "plain"
        root.mkdir()
        before = tree(root)
        output = subprocess.run([sys.executable, "-B", str(DUNGEON_ROOT / "tools" / "project-structure.py"),
                                 "project-probe", "--root", str(root), "--json"],
                                capture_output=True, text=True, encoding="utf-8", timeout=20)
        self.assertEqual(output.returncode, 0, output.stderr)
        self.assertEqual(json.loads(output.stdout)["mode"], "NOT_DUNGEON")
        request = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
        result = subprocess.run([sys.executable, "-B", str(SERVER)], input=json.dumps(request) + "\n",
                                capture_output=True, text=True, encoding="utf-8", timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)
        tools = json.loads(result.stdout)["result"]["tools"]
        names = [tool["name"] for tool in tools]
        self.assertEqual(len(names), len(set(names)))
        self.assertEqual(len(names), 34)
        for tool in tools:
            self.assertGreater(len(tool["description"]), 40)
            self.assertEqual(tool["inputSchema"]["type"], "object")
            self.assertEqual(tool["outputSchema"]["type"], "object")
            self.assertFalse(tool["inputSchema"]["additionalProperties"])
        self.assertIn("dungeon_connect_project", names)
        self.assertIn("dungeon_adoption_apply", names)
        self.assertEqual(before, tree(root))

    def test_ambiguous_identity_is_not_inspected_as_first_occurrence(self) -> None:
        root = self.base / "duplicates"
        (root / "module").mkdir(parents=True)
        for name in ("a", "b"):
            (root / "module" / f"{name}.sklt").write_text(skeleton("same.identity"), encoding="utf-8")
        before = tree(root)
        reply = self.client([("dungeon_inspect_object", {
            "project_root": str(root), "object_id": "same.identity"})])[0]
        self.assertTrue(reply["isError"])
        self.assertEqual(reply["structuredContent"]["status"], "BLOCKED")
        self.assertEqual(len(reply["structuredContent"]["occurrences"]), 2)
        self.assertNotIn("skeleton", reply["structuredContent"])
        self.assertEqual(before, tree(root))

    def test_exact_executable_proof_and_build_change_through_mcp(self) -> None:
        if not shutil.which("git"):
            self.skipTest("Git unavailable")
        root = self.base / "proof"
        (root / "module").mkdir(parents=True)
        (root / "module" / "render.sklt").write_text(skeleton("render.proof"), encoding="utf-8")
        (root / "src").mkdir()
        (root / "src" / "main.rs").write_text("fn main() {}", encoding="utf-8")
        (root / "tests").mkdir()
        (root / "tests" / "smoke.rs").write_text("// synthetic verification fixture", encoding="utf-8")
        (root / "app.exe").write_bytes(b"MZ synthetic fixture build 1; not a runnable application")
        (root / ".project").mkdir()
        (root / ".project" / "project.json").write_text(json.dumps({
            "schema_version": 1, "project_id": "mcp.proof", "modules": [{"id": "module", "path": "module"}],
            "evidence_store": {"type": "local-cas", "root": "../store"}}), encoding="utf-8")
        for arguments in (("init", "--quiet"), ("config", "user.name", "MCP Test"),
                          ("config", "user.email", "test@example.invalid"),
                          ("commit", "--allow-empty", "--quiet", "-m", "fixture")):
            subprocess.run(["git", "-C", str(root), *arguments], capture_output=True, check=True)
        revision = subprocess.run(["git", "-C", str(root), "rev-parse", "HEAD"],
                                  capture_output=True, text=True, check=True).stdout.strip()
        toolkit = self.base / "Toolkit"
        catalog = toolkit / "prototypes/ai-ui-test-tool-auto-probe/scheduler/container/environment-catalog.json"
        catalog.parent.mkdir(parents=True)
        catalog.write_text(json.dumps({"version": 1, "environments": [
            {"version": 1, "key": "docker-linux-level0", "kind": "container"}]}), encoding="utf-8")
        (toolkit / "docs").mkdir()
        (toolkit / "docs" / "INDEX.md").write_text("[catalog](../prototypes/ai-ui-test-tool-auto-probe/scheduler/container/environment-catalog.json)", encoding="utf-8")
        env = {"RUST_MAKEPAD_TOOLKIT_ROOT": str(toolkit)}
        paths = ["src/main.rs", "tests/smoke.rs"]
        digest = lambda relative: hashlib.sha256((root / relative).read_bytes()).hexdigest()
        execution = {"provider": "rust_makepad_toolkit", "type": "container", "id": "docker-linux-level0"}
        check_identity = {"id": "smoke", "type": "executable", "scope": "PROTOTYPE"}
        raw_receipt = {
            "schema_version": 2,
            "tool_version": "ai-ui-test-tool-auto-probe",
            "contract_version": "reliability-v2",
            "actions": [{
                "action_id": "fixture-1",
                "session_id": "synthetic-session",
                "timestamp_unix_ms": 1,
                "action": "assert_visible",
                "target": "render.proof.control",
                "result": {
                    "success": True,
                    "operation": "assert_visible",
                    "action_id": "fixture-1",
                    "phase": "complete",
                    "application_healthy": True,
                    "result": {
                        "assertion": "visible",
                        "passed": True,
                        "control": {"automation_id": "render.proof.control", "visible": True},
                    },
                },
            }],
        }
        raw_path = self.base / "synthetic-toolkit-reliability-v2.json"
        raw_path.write_text(json.dumps(raw_receipt), encoding="utf-8")
        raw_capture = self.client([
            ("dungeon_cas_put", {"project_root": str(root), "file_path": str(raw_path)})
        ], [root], env)[0]
        self.assertFalse(raw_capture["isError"], raw_capture)
        raw_reference = raw_capture["structuredContent"]["reference"]
        report = {"objects": ["render.proof"], "task_id": "BUILD-004", "scope": "PROTOTYPE",
                  "execution": execution, "source": {"revision": revision,
                      "files": [{"path": path, "sha256": digest(path)} for path in paths]},
                  "build": {"path": "app.exe", "sha256": digest("app.exe")},
                  "result": {"status": "PASS", "checks": [{**check_identity, "status": "PASS"}]},
                  "proof": {"adapter": "toolkit-reliability-v2", "schema_version": 2,
                            "raw": raw_reference,
                            "bindings": [{**check_identity, "action_id": "fixture-1",
                                          "operation": "assert_visible"}]}}
        report_path = self.base / "report.json"
        report_path.write_text(json.dumps(report), encoding="utf-8")
        captured = self.client([("dungeon_cas_put", {"project_root": str(root), "file_path": str(report_path)})], [root], env)[0]
        self.assertFalse(captured["isError"], captured)
        draft = {"id": "EV-MCP-004", "objects": ["render.proof"], "task_id": "BUILD-004", "scope": "PROTOTYPE",
                 "execution": execution, "inputs": {}, "artifacts": {
                     "result": captured["structuredContent"]["reference"], "raw": raw_reference},
                 "source": {"revision": revision, "files": paths}, "build": {"path": "app.exe"}, "result": {"status": "PASS"}}
        draft_path = self.base / "draft.json"
        draft_path.write_text(json.dumps(draft), encoding="utf-8")
        trace = {"task_id": "BUILD-004", "object_ids": ["render.proof"], "scope": "PROTOTYPE",
                 "source_refs": [paths[0]], "test_refs": [paths[1]],
                 "required_checks": [check_identity],
                 "performed_checks": [{**check_identity, "status": "PASS"}],
                 "evidence_refs": [draft["id"]], "status": "PASS",
                 "actual_result": {"status": "PASS"}}
        calls = [
            ("dungeon_evidence_create", {"project_root": str(root), "draft_path": str(draft_path)}),
            ("dungeon_evidence_seal", {"project_root": str(root), "evidence_id": draft["id"]}),
            ("dungeon_task_trace_write", {"project_root": str(root), "trace": trace}),
            ("dungeon_coverage_set", {"project_root": str(root), "object_id": "render.proof", "scope": "PROTOTYPE",
                                      "state": "VERIFIED", "rationale": "Synthetic Toolkit receipt fixture only; no application/runtime was executed",
                                      "task_id": "BUILD-004", "evidence_ids": [draft["id"]], "executable_path": "app.exe"}),
            ("dungeon_coverage_get", {"project_root": str(root), "object_id": "render.proof"}),
        ]
        replies = self.client(calls, [root], env)
        for reply in replies:
            self.assertFalse(reply["isError"], reply)
        coverage = replies[-1]["structuredContent"]["coverage"]["render.proof"]["PROTOTYPE"]
        self.assertEqual(coverage["state"], "VERIFIED", coverage)
        (root / "app.exe").write_bytes(b"MZ synthetic fixture build 2")
        before = tree(root)
        invalidated = self.client([calls[-1]], [root], env)[0]
        self.assertTrue(invalidated["isError"])
        coverage = invalidated["structuredContent"]["coverage"]["render.proof"]["PROTOTYPE"]
        self.assertEqual(coverage["state"], "UNKNOWN")
        self.assertEqual(coverage["recorded_state"], "VERIFIED")
        self.assertIn("BUILD_HASH_MISMATCH", [item["code"] for item in coverage["verification_issues"]])
        self.assertEqual(before, tree(root))


if __name__ == "__main__":
    unittest.main()
