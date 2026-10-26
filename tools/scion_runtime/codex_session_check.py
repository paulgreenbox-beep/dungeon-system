"""One explicit ephemeral read-only Codex session for installed MCP acceptance.

Uses the account's normal Codex CLI authentication; never reads or prints keys.
The session may only inspect the installed Dungeon plugin and call read tools.
"""
from __future__ import annotations
import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

REQUIRED = {"dungeon_vcs_status", "dungeon_vcs_inspect", "dungeon_vcs_history", "dungeon_vcs_diff"}


def validate_recorded_session(output: Path, code: int, filename: str = "result.json") -> dict:
    events = []
    for line in (output / "events.jsonl").read_text(encoding="utf-8").splitlines():
        try:
            events.append(json.loads(line))
        except ValueError:
            pass
    calls = []
    for event in events:
        item = event.get("item", {})
        if event.get("type") != "item.completed" or item.get("type") != "mcp_tool_call":
            continue
        result = item.get("result") or {}
        structured = result.get("structuredContent", result.get("structured_content"))
        if structured is None:
            try:
                structured = json.loads(result["content"][0]["text"])
            except (KeyError, IndexError, ValueError, TypeError):
                structured = {}
        calls.append({**{key: item.get(key) for key in ("server", "tool", "status", "error")},
                      "result_status": structured.get("status"), "arguments": item.get("arguments")})
    try:
        answer = json.loads((output / "final.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        answer = {}
    raw_names = answer.get("dungeon_tool_names", [])
    names = [name.removeprefix("mcp__dungeon_system__") for name in raw_names if isinstance(name, str)]
    called = {item.get("tool") for item in calls if item.get("status") == "completed"
              and item.get("result_status") == "PASS" and not item.get("error")}
    passed = (code == 0 and answer.get("dungeon_tool_count") == 34
              and len(names) == len(raw_names) == len(set(names)) == 34
              and REQUIRED <= set(names) and REQUIRED <= called)
    receipt = {"schema_version": 1, "status": "PASS" if passed else "NOT_ACCEPTED", "exit_code": code,
               "model": "gpt-6-luna", "effort": "max", "fresh_ephemeral_session": True,
               "reported_dungeon_tool_count": answer.get("dungeon_tool_count"),
               "logical_tool_names": names, "actual_mcp_calls": calls,
               "validation": "LOGICAL_NAME_NORMALIZATION_AND_ACTUAL_COMPLETED_MCP_RESULT_STATUS",
               "requires_independent_review": True}
    with (output / filename).open("x", encoding="utf-8") as stream:
        json.dump(receipt, stream, indent=2)
        stream.write("\n")
    return receipt


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--validate-existing", action="store_true", help="validate preserved raw events without rerunning or replacing the original receipt")
    args = parser.parse_args()
    if args.validate_existing:
        original = json.loads((args.output / "result.json").read_text(encoding="utf-8"))
        receipt = validate_recorded_session(args.output, original["exit_code"], "validated-result.json")
        print(json.dumps(receipt), flush=True)
        return 0 if receipt["status"] == "PASS" else 1
    root = Path(__file__).resolve().parents[2]
    executable = shutil.which("codex")
    if not executable:
        parser.error("authenticated Codex CLI is required")
    args.output.mkdir(parents=True, exist_ok=False)
    prompt = (
        "Perform only a read-only acceptance check of the installed dungeon-system@personal MCP plugin. "
        "Do not edit files, use shell commands, install anything, initialize anything, call write tools, "
        "or contact other plugins. Identify the exact number of Dungeon tools actually available in "
        "your tool inventory, and list their names. Never infer availability from source. "
        f"Use explicit project_root={json.dumps(str(root))} on every call. First call dungeon_project_probe. "
        "Then actually call dungeon_vcs_status with scope_id main, dungeon_vcs_inspect with scope_id main, "
        "dungeon_vcs_history with scope_id main and limit 1, and dungeon_vcs_diff with scope_id main. "
        "Return compact JSON only: dungeon_tool_count, dungeon_tool_names, called_tools with status for "
        "each, and limitations. If anything is unavailable or denied, report it honestly; do not substitute "
        "a shell/source inspection or fabricate a result. Existing Skills may guide safe navigation."
    )
    command = [executable, "exec", "--ignore-user-config", "--ephemeral", "--json", "--sandbox", "read-only",
               "--model", "gpt-6-luna", "--config", 'model_reasoning_effort="max"',
               # CLI dotted overrides take the table key literally; quotes belong
               # in TOML files, not inside this override key.
               "--config", 'plugins.dungeon-system@personal.enabled=true',
               "--config", 'approval_policy="never"', "--cd", str(root),
               "--output-last-message", str((args.output / "final.json").resolve()), prompt]
    # Child account credentials stay with the CLI; only safe explicit arguments are recorded.
    (args.output / "invocation.json").write_text(json.dumps({"argv": command[:-1], "prompt": prompt,
        "model": "gpt-6-luna", "effort": "max", "sandbox": "read-only", "ephemeral": True}, indent=2), encoding="utf-8")
    with (args.output / "events.jsonl").open("xb") as out, (args.output / "stderr.log").open("xb") as err:
        process = subprocess.Popen(command, cwd=root, stdin=subprocess.DEVNULL, stdout=out, stderr=err)
        try:
            code = process.wait(timeout=300)
        except BaseException:
            if process.poll() is None:
                subprocess.run(["taskkill.exe", "/PID", str(process.pid), "/T", "/F"],
                               stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=20)
                process.wait(timeout=20)
            raise
    receipt = validate_recorded_session(args.output, code)
    print(json.dumps(receipt), flush=True)
    return 0 if receipt["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
