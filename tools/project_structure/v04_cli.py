"""CLI entrypoints for project adoption and implementation trace metadata."""
from __future__ import annotations

import json
from pathlib import Path


COMMANDS = {
    "project-probe", "project-summary", "modules", "objects", "object-inspect",
    "adoption-plan", "adoption-apply", "source-link-audit", "task-trace-write",
    "task-trace-get", "task-trace-list", "coverage-get", "coverage-set",
}


def add_commands(subparsers, add_root, add_json) -> None:
    parsers = {}
    for name in sorted(COMMANDS):
        command = subparsers.add_parser(name, help=name.replace("-", " "))
        add_root(command, suppressed=True)
        add_json(command, suppressed=True)
        parsers[name] = command
    parsers["adoption-plan"].add_argument("--project-id")
    parsers["adoption-apply"].add_argument("plan", type=Path)
    parsers["adoption-apply"].add_argument("--accept-module", action="append", default=[])
    parsers["object-inspect"].add_argument("object_id")
    parsers["objects"].add_argument("--module")
    for name in ("modules", "objects", "task-trace-list"):
        parsers[name].add_argument("--offset", type=int, default=0)
        parsers[name].add_argument("--limit", type=int, default=50)
    parsers["source-link-audit"].add_argument("--object-id", action="append")
    parsers["task-trace-write"].add_argument("draft", type=Path)
    parsers["task-trace-write"].add_argument("--expected-hash")
    parsers["task-trace-get"].add_argument("task_id")
    parsers["task-trace-list"].add_argument("--object-id")
    parsers["coverage-get"].add_argument("--object-id")
    coverage = parsers["coverage-set"]
    coverage.add_argument("object_id")
    coverage.add_argument("--scope", required=True, choices=("PROTOTYPE", "PRODUCT"))
    coverage.add_argument("--state", required=True, choices=("UNKNOWN", "NOT_STARTED", "PARTIAL", "IMPLEMENTED", "VERIFIED"))
    coverage.add_argument("--rationale", required=True)
    coverage.add_argument("--evidence-id", action="append", default=[])
    coverage.add_argument("--task-id")
    coverage.add_argument("--executable", type=Path)


def _read_json(root: Path, path: Path) -> dict:
    selected = path if path.is_absolute() else root / path
    if selected.stat().st_size > 1_048_576:
        raise ValueError("input JSON is too large")
    value = json.loads(selected.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("input JSON must be an object")
    return value


def paginate(rows: list, offset: int = 0, limit: int = 50) -> dict:
    if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 500:
        raise ValueError("offset must be non-negative and limit must be 1..500")
    end = min(offset + limit, len(rows))
    return {"items": rows[offset:end], "total": len(rows), "offset": offset,
            "next_offset": end if end < len(rows) else None, "truncated": end < len(rows)}


def run_command(root: Path, args) -> dict:
    from .v04_projects import adoption_apply, adoption_plan, project_catalog, project_probe
    from .v04_trace import coverage_get, coverage_set, source_link_audit, task_trace_get, task_trace_list, task_trace_write
    name = args.command
    if name == "project-probe":
        return project_probe(root)
    if name == "adoption-plan":
        return adoption_plan(root, project_id=args.project_id)
    if name == "adoption-apply":
        return adoption_apply(root, _read_json(root, args.plan), args.accept_module)
    if name in {"project-summary", "modules", "objects", "object-inspect"}:
        catalog = project_catalog(root)
        if name == "project-summary":
            summaries = [{key: row.get(key) for key in ("id", "path", "object_count", "sklt_count", "dialects")}
                         for row in catalog.get("modules", [])]
            return {**project_probe(root), "modules": paginate(summaries),
                    "object_count": len(catalog.get("objects", {})), "issues": catalog.get("issues", [])}
        if name == "modules":
            summaries = [{key: row.get(key) for key in ("id", "path", "object_count", "sklt_count", "dialects")}
                         for row in catalog.get("modules", [])]
            return {"status": catalog.get("status", "PASS"), "modules": paginate(summaries, args.offset, args.limit), "issues": catalog.get("issues", [])}
        if name == "objects":
            rows = [{"object_id": key, **record} for key, record in sorted(catalog.get("objects", {}).items())
                    if args.module is None or record.get("module") == args.module]
            return {"status": catalog.get("status", "PASS"), "objects": paginate(rows, args.offset, args.limit), "issues": catalog.get("issues", [])}
        if args.object_id in catalog.get("duplicates", {}):
            return {"status": "BLOCKED", "object_id": args.object_id,
                    "issues": [{"code": "DUPLICATE_OBJECT_ID", "message": "Inspect each SKLT occurrence and resolve the ambiguous identity."}]}
        record = catalog.get("objects", {}).get(args.object_id)
        if record is None:
            return {"status": "NOT_FOUND", "object_id": args.object_id, "issues": [{"code": "OBJECT_NOT_FOUND", "message": "Select an object from objects before inspecting it."}]}
        return {"status": "PASS", "object_id": args.object_id, **record, "issues": catalog.get("issues", [])}
    if name == "source-link-audit":
        return source_link_audit(root, args.object_id)
    if name == "task-trace-write":
        return task_trace_write(root, _read_json(root, args.draft), expected_hash=args.expected_hash)
    if name == "task-trace-get":
        return task_trace_get(root, args.task_id)
    if name == "task-trace-list":
        return task_trace_list(root, object_id=args.object_id, offset=args.offset, limit=args.limit)
    if name == "coverage-get":
        return coverage_get(root, object_id=args.object_id)
    if name == "coverage-set":
        return coverage_set(root, args.object_id, args.scope, args.state, args.rationale,
                            evidence_ids=args.evidence_id, task_id=args.task_id, executable_path=args.executable)
    raise ValueError(f"unsupported v0.4 command: {name}")


def run(root: Path, args) -> int:
    try:
        payload = run_command(root, args)
    except (ValueError, OSError, TypeError, KeyError) as exc:
        payload = {"status": "FAIL", "issues": [{"code": "INVALID_OPERATION", "message": str(exc)}],
                   "next_steps": ["Probe the project and review the operation inputs."]}
    payload.setdefault("project_root", str(root))
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 1 if payload.get("status") in {"FAIL", "NOT_FOUND", "BLOCKED", "ERROR"} else 0
