from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .libraries import discover_libraries
from .model import Issue
from .projections import write_projections, projection_files
from .v03_cas import CasError, cas_get, cas_put, cas_status, cas_verify
from .v03_dependencies import normalize_dependencies
from .v03_evidence import (
    EvidenceError,
    create_evidence,
    evidence_for,
    evidence_store_status,
    inspect_evidence,
    manifest_path,
    seal_evidence,
    verify_evidence,
)
from .v03_hash import structural_hash
from .v03_index import build_object_index, index_path, load_object_index, rebuild_object_index, write_object_index
from .v03_issues import issue_summary, print_issues, severity_for
from .v03_manifest import load_manifest
from .v03_metadata import mark_artifact, metadata_updates, metadata_path
from .v041_paths import publish_files, json_bytes, validate_root, UnsafeOutputPath, PublicationError
from .v03_scan import scan_project_v03
from .v03_snapshots import create_snapshot_v03, snapshot_diff
from .v03_sources import parse_source_refs, validate_source_refs
from .v03_validation import _schema_issues, projection_statuses, status_data, validate_v03
from .validator import module_counts


def _add_root_argument(parser: argparse.ArgumentParser, suppressed: bool = False) -> None:
    parser.add_argument("--root", type=Path, default=argparse.SUPPRESS if suppressed else Path.cwd(), help="project root")


def _add_json_argument(parser: argparse.ArgumentParser, suppressed: bool = False) -> None:
    parser.add_argument("--json", action="store_true", default=argparse.SUPPRESS if suppressed else False, help="emit stable JSON")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Dungeon System structural tooling v0.4")
    _add_root_argument(parser)
    _add_json_argument(parser)
    subparsers = parser.add_subparsers(dest="command", required=True)
    commands = {
        "scan": "show project categories and structural files",
        "validate": "validate structural sources and derived artifacts",
        "exp": "regenerate module .exp, docs/.grade and metadata",
        "refresh": "publish exp, grade, index and state together",
        "index": "rebuild .project/objects.json",
        "status": "show operational project status",
    }
    for name, description in commands.items():
        command = subparsers.add_parser(name, help=description)
        _add_root_argument(command, suppressed=True)
        _add_json_argument(command, suppressed=True)
    inspect = subparsers.add_parser("inspect", help="inspect one object by object_id")
    _add_root_argument(inspect, suppressed=True)
    _add_json_argument(inspect, suppressed=True)
    inspect.add_argument("object_id")
    snapshot = subparsers.add_parser("snapshot", help="create a frozen structural snapshot")
    _add_root_argument(snapshot, suppressed=True)
    _add_json_argument(snapshot, suppressed=True)
    snapshot.add_argument("name")
    snapshot.add_argument("--evidence-id", action="append", default=[], help="link an existing evidence manifest")
    diff = subparsers.add_parser("snapshot-diff", help="compare two structural snapshots")
    _add_root_argument(diff, suppressed=True)
    _add_json_argument(diff, suppressed=True)
    diff.add_argument("old")
    diff.add_argument("new")
    cas_put_parser = subparsers.add_parser("cas-put", help="store exact file bytes in the configured local CAS")
    _add_root_argument(cas_put_parser, suppressed=True)
    _add_json_argument(cas_put_parser, suppressed=True)
    cas_put_parser.add_argument("file", type=Path)
    cas_get_parser = subparsers.add_parser("cas-get", help="retrieve a verified CAS object")
    _add_root_argument(cas_get_parser, suppressed=True)
    _add_json_argument(cas_get_parser, suppressed=True)
    cas_get_parser.add_argument("reference")
    cas_get_parser.add_argument("--output", required=True, type=Path)
    cas_verify_parser = subparsers.add_parser("cas-verify", help="verify a CAS object against its SHA-256")
    _add_root_argument(cas_verify_parser, suppressed=True)
    _add_json_argument(cas_verify_parser, suppressed=True)
    cas_verify_parser.add_argument("reference")
    cas_status_parser = subparsers.add_parser("cas-status", help="show local CAS availability and size")
    _add_root_argument(cas_status_parser, suppressed=True)
    _add_json_argument(cas_status_parser, suppressed=True)
    evidence_create_parser = subparsers.add_parser("evidence-create", help="capture a validated evidence draft as an immutable manifest")
    _add_root_argument(evidence_create_parser, suppressed=True)
    _add_json_argument(evidence_create_parser, suppressed=True)
    evidence_create_parser.add_argument("draft", type=Path)
    for command_name, help_text in (
        ("evidence-inspect", "show an evidence manifest"),
        ("evidence-verify", "verify an evidence manifest, its seal and referenced CAS objects"),
        ("evidence-seal", "immutably seal a verified evidence manifest"),
    ):
        command = subparsers.add_parser(command_name, help=help_text)
        _add_root_argument(command, suppressed=True)
        _add_json_argument(command, suppressed=True)
        command.add_argument("evidence_id")
    evidence_for_parser = subparsers.add_parser("evidence-for", help="list evidence linked to an object_id")
    _add_root_argument(evidence_for_parser, suppressed=True)
    _add_json_argument(evidence_for_parser, suppressed=True)
    evidence_for_parser.add_argument("object_id")
    from .v04_cli import add_commands
    add_commands(subparsers, _add_root_argument, _add_json_argument)
    return parser


def _emit(payload: object, as_json: bool) -> None:
    if as_json:
        print(json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=True))


def _root_snapshot(root: Path):
    snapshot, manifest, manifest_issues = scan_project_v03(root)
    libraries = discover_libraries(root)
    return snapshot, manifest, libraries, manifest_issues


def _source_validation(snapshot, manifest, libraries) -> list[Issue]:
    issues = list(snapshot.issues)
    issues.extend(_schema_issues(snapshot, libraries, manifest))
    issues.extend(libraries.issues)
    issues.extend(validate_source_refs(snapshot)[1])
    issues.extend(normalize_dependencies(snapshot, set(libraries.by_id))[1])
    from .validator import validate_snapshot

    issues.extend(validate_snapshot(snapshot, include_projections=False))
    return issues


def _run_scan(root: Path, as_json: bool) -> int:
    snapshot, manifest, libraries, manifest_issues = _root_snapshot(root)
    docs_root = root / "docs"
    snapshot_dirs = [path.name for path in (root / "snapshots").iterdir() if path.is_dir() and not path.name.startswith(".")] if (root / "snapshots").is_dir() else []
    payload = {
        "root": str(root),
        "manifest": {"path": ".project/project.json", "exists": manifest.exists, "project_id": manifest.project_id, "schema_version": manifest.schema_version},
        "modules": [{"id": module.name, "path": module.path.relative_to(root).as_posix() if module.path != root else ".", "sklt": len(module.sklts), "rare": module_counts(module)["rare"], "glif": len(module.glifs)} for module in snapshot.modules],
        "docs": {name: (docs_root / name).exists() for name in ("project.md", "map.md", "modules.md", "architecture.md", "agent-guides.md")},
        "snapshots": snapshot_dirs,
        "libraries": [descriptor.library_id for descriptor in libraries.descriptors],
        "objects": len(snapshot.sklts),
        "glif_projections": len(snapshot.glifs),
        "issues": issue_summary(list(snapshot.issues) + manifest_issues + libraries.issues, root),
    }
    if as_json:
        _emit(payload, True)
    else:
        print(f"root: {root}")
        print(f"manifest: {'CURRENT' if manifest.exists else 'LEGACY_DEFAULTS'} ({manifest.project_id})")
        print("PROJECT MODULES")
        if not snapshot.modules:
            print("  <none>")
        for module in payload["modules"]:
            print(f"  {module['id']}: sklt={module['sklt']} rare={module['rare']} glif={module['glif']}")
        print(f"DOCS: {sum(payload['docs'].values())}/{len(payload['docs'])} present")
        print(f"SNAPSHOTS: {len(snapshot_dirs)}")
        print(f"LIBRARIES: {len(libraries.descriptors)}")
        print(f"OBJECTS: {len(snapshot.sklts)}  GLIF: {len(snapshot.glifs)}")
        all_issues = list(snapshot.issues) + manifest_issues + libraries.issues
        if all_issues:
            print_issues(all_issues, root)
    return 0 if issue_summary(list(snapshot.issues) + manifest_issues + libraries.issues, root)["errors"] == 0 else 1


def _run_validate(root: Path, as_json: bool) -> int:
    snapshot, manifest, libraries, manifest_issues = _root_snapshot(root)
    issues = list(manifest_issues) + validate_v03(snapshot, manifest, libraries)
    summary = issue_summary(issues, root)
    if as_json:
        _emit(summary, True)
    else:
        print(summary["status"])
        if issues:
            print_issues(issues, root)
    return 0 if summary["status"] == "PASS" else 1


def _run_exp(root: Path, as_json: bool) -> int:
    snapshot, manifest, libraries, manifest_issues = _root_snapshot(root)
    issues = _source_validation(snapshot, manifest, libraries) + manifest_issues
    summary = issue_summary(issues, root)
    if summary["errors"]:
        if as_json:
            _emit(summary, True)
        else:
            print_issues(issues, root)
        return 1
    input_hash = structural_hash(snapshot, manifest)
    files = projection_files(snapshot)
    metadata = metadata_updates(root, {"artifacts": {
        "exp": {"input_hash": input_hash, "path": "<module>/.exp"},
        "grade": {"input_hash": input_hash, "path": "docs/.grade"}}})
    files[metadata_path(root)] = json_bytes(metadata)
    publish_files(root, files)
    payload = {"status": "PASS", "input_hash": input_hash, "modules": [module.name for module in snapshot.modules], "grade": {"sklt": len(snapshot.sklts), "rare": sum(record.state == "RARE" for record in snapshot.sklts)}}
    if as_json:
        _emit(payload, True)
    else:
        print("EXP UPDATED")
        for module in snapshot.modules:
            counts = module_counts(module)
            print(f"{module.name}: sklt {counts['sklt']} rare {counts['rare']}")
        print("docs/.grade: updated")
    return 0


def _run_index(root: Path, as_json: bool) -> int:
    snapshot, manifest, libraries, manifest_issues = _root_snapshot(root)
    issues = _source_validation(snapshot, manifest, libraries) + manifest_issues
    summary = issue_summary(issues, root)
    if summary["errors"]:
        if as_json:
            _emit(summary, True)
        else:
            print_issues(issues, root)
        return 1
    data, index_issues = rebuild_object_index(root, snapshot, manifest, libraries)
    if index_issues and any(severity_for(issue.code) == "ERROR" for issue in index_issues):
        if as_json:
            _emit(issue_summary(index_issues, root), True)
        else:
            print_issues(index_issues, root)
        return 1
    payload = {"status": "PASS", "path": ".project/objects.json", "objects": len(data.get("objects", {})) if data else 0, "issues": issue_summary(index_issues, root)["issues"]}
    if as_json:
        _emit(payload, True)
    else:
        print(f"INDEX UPDATED: {payload['objects']} object(s)")
        if index_issues:
            print_issues(index_issues, root)
    return 0


def _run_refresh(root: Path, as_json: bool) -> int:
    snapshot, manifest, libraries, manifest_issues = _root_snapshot(root)
    issues = _source_validation(snapshot, manifest, libraries) + manifest_issues
    data, index_issues = build_object_index(snapshot, manifest, libraries)
    issues += index_issues
    summary = issue_summary(issues, root)
    if summary["errors"]:
        _emit(summary, as_json)
        if not as_json:
            print_issues(issues, root)
        return 1
    input_hash = structural_hash(snapshot, manifest)
    artifacts = {"exp": {"input_hash": input_hash, "path": "<module>/.exp"},
                 "grade": {"input_hash": input_hash, "path": "docs/.grade"},
                 "objects_index": {"input_hash": input_hash, "path": ".project/objects.json"}}
    files = projection_files(snapshot)
    files[index_path(root)] = json_bytes(data)
    files[metadata_path(root)] = json_bytes(metadata_updates(root, {"artifacts": artifacts}))
    publish_files(root, files)
    _emit({"status": "PASS", "issues": [], "next_steps": ["validate"],
           "input_hash": input_hash, "files_changed": [str(p.relative_to(root)) for p in files]}, as_json)
    if not as_json:
        print("REFRESH PUBLISHED")
    return 0


def _ensure_index(root: Path, snapshot, manifest, libraries) -> tuple[dict | None, list[Issue], bool]:
    current_hash = structural_hash(snapshot, manifest)
    data, issues = load_object_index(root)
    metadata_path = root / ".project" / "state.json"
    metadata_data = {}
    if metadata_path.exists():
        try:
            metadata_data = json.loads(metadata_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            metadata_data = {}
    current = data is not None and not issues and data.get("input_hash") == current_hash and metadata_data.get("artifacts", {}).get("objects_index", {}).get("input_hash") == current_hash
    if current:
        return data, issues, False
    # Navigation never grants write authority or repairs derived files implicitly.
    rebuilt, rebuild_issues = build_object_index(snapshot, manifest, libraries)
    return rebuilt, issues + rebuild_issues, True


def _run_inspect(root: Path, object_id: str, as_json: bool) -> int:
    snapshot, manifest, libraries, manifest_issues = _root_snapshot(root)
    data, issues, rebuilt = _ensure_index(root, snapshot, manifest, libraries)
    if data is None:
        issues.extend(manifest_issues)
        if as_json:
            _emit({"status": "FAIL", "issues": issue_summary(issues, root)["issues"]}, True)
        else:
            print_issues(issues, root)
        return 1
    object_data = data.get("objects", {}).get(object_id)
    if object_data is None:
        payload = {"status": "NOT_FOUND", "object_id": object_id, "index_rebuilt": False, "index_computed_in_memory": rebuilt}
        if as_json:
            _emit(payload, True)
        else:
            print(f"NOT_FOUND: object_id '{object_id}'")
        return 1
    payload = {"status": "PASS", "object_id": object_id, "index_rebuilt": False, "index_computed_in_memory": rebuilt, **object_data}
    if as_json:
        _emit(payload, True)
    else:
        print(f"Object: {object_id}")
        print(f"Module: {object_data.get('module')}")
        print(f"State: {object_data.get('state')}")
        print(f"SKLT: {object_data.get('sklt')}")
        print("GLIF:")
        for path in object_data.get("glif", []):
            print(f"  {path}")
        print("Source:")
        for source in object_data.get("source", []):
            print(f"  {source}")
        print("Dependencies:")
        for dependency in object_data.get("dependencies", []):
            print(f"  {dependency}")
    return 0


def _run_status(root: Path, as_json: bool) -> int:
    snapshot, manifest, libraries, manifest_issues = _root_snapshot(root)
    data, issues = status_data(snapshot, manifest, libraries)
    issues = manifest_issues + issues
    summary = issue_summary(issues, root)
    store_status = evidence_store_status(root)
    payload = {**data, "evidence_store": store_status, **{key: summary[key] for key in ("errors", "warnings", "infos", "issues")}}
    if as_json:
        _emit(payload, True)
    else:
        print("Dungeon Status")
        print(f"Validation: {data['validation']}")
        print(f"Modules: {data['modules']}")
        print(f"Objects: {data['objects']}")
        print(f"SKLT: {data['sklt']}")
        print(f"RARE: {data['rare']}")
        print(f"Object index: {data['projections']['objects_index']}")
        print(f"EXP: {data['projections']['exp']}")
        print(f"Grade: {data['projections']['grade']}")
        print(f"Latest snapshot: {data['latest_snapshot']['id'] or '<none>'} ({data['latest_snapshot']['state']})")
        print(f"Evidence store: {store_status['cas']['state']} ({store_status['cas']['objects']} CAS objects, {store_status['manifests']} manifests, {store_status['sealed']} sealed)")
        print(f"Errors: {summary['errors']}")
        print(f"Warnings: {summary['warnings']}")
        if issues:
            print_issues(issues, root)
    return 0 if data["validation"] == "PASS" else 1


def _run_snapshot(root: Path, name: str, as_json: bool, evidence_ids: list[str] | None = None) -> int:
    evidence_ids = evidence_ids or []
    missing_evidence: list[str] = []
    for evidence_id in evidence_ids:
        try:
            if not manifest_path(root, evidence_id).is_file():
                missing_evidence.append(evidence_id)
        except EvidenceError:
            missing_evidence.append(evidence_id)
    if missing_evidence:
        issues = [Issue("EVIDENCE_NOT_FOUND", f"cannot link missing or invalid evidence '{evidence_id}'") for evidence_id in missing_evidence]
        if as_json:
            _emit(issue_summary(issues, root), True)
        else:
            print_issues(issues, root)
        return 1
    snapshot, manifest, libraries, manifest_issues = _root_snapshot(root)
    issues = manifest_issues + validate_v03(snapshot, manifest, libraries)
    summary = issue_summary(issues, root)
    if summary["errors"]:
        if as_json:
            _emit(summary, True)
        else:
            print_issues(issues, root)
        return 1
    create_issues = create_snapshot_v03(snapshot, name, manifest, libraries, evidence_ids=evidence_ids)
    if create_issues:
        if as_json:
            _emit(issue_summary(create_issues, root), True)
        else:
            print_issues(create_issues, root)
        return 1
    payload = {"status": "PASS", "snapshot_id": name, "path": f"snapshots/{name}"}
    if as_json:
        _emit(payload, True)
    else:
        print(f"created: {root / 'snapshots' / name}")
    return 0


def _resolve_input_path(root: Path, path: Path) -> Path:
    return path.resolve() if path.is_absolute() else (root / path).resolve()


def _run_evidence_command(root: Path, command: str, args, as_json: bool) -> int:
    if command == "cas-put":
        try:
            reference, reused, size = cas_put(root, _resolve_input_path(root, args.file))
            payload = {"status": "PASS", "reference": reference, "reused": reused, "bytes": size}
            if as_json:
                _emit(payload, True)
            else:
                print(f"{reference} ({size} bytes, {'reused' if reused else 'stored'})")
            return 0
        except CasError as exc:
            payload = {"status": "FAIL", "error": str(exc)}
            if as_json:
                _emit(payload, True)
            else:
                print(f"ERROR: {exc}")
            return 1
    if command == "cas-get":
        try:
            raw_output = args.output if args.output.is_absolute() else root / args.output
            size = cas_get(root, args.reference, raw_output)
            payload = {"status": "PASS", "reference": args.reference, "output": str(_resolve_input_path(root, args.output)), "bytes": size}
            if as_json:
                _emit(payload, True)
            else:
                print(f"CAS object retrieved ({size} bytes)")
            return 0
        except CasError as exc:
            payload = {"status": "FAIL", "error": str(exc)}
            if as_json:
                _emit(payload, True)
            else:
                print(f"ERROR: {exc}")
            return 1
    if command == "cas-verify":
        try:
            valid, error, size = cas_verify(root, args.reference)
        except CasError as exc:
            valid, error, size = False, str(exc), None
        payload = {"status": "PASS" if valid else "FAIL", "reference": args.reference, "bytes": size, "error": error}
        if as_json:
            _emit(payload, True)
        else:
            print(f"{'VERIFIED' if valid else 'BROKEN'}: {args.reference}" + (f" — {error}" if error else ""))
        return 0 if valid else 1
    if command == "cas-status":
        payload = cas_status(root).as_dict()
        if as_json:
            _emit(payload, True)
        else:
            print(f"CAS: {payload['state']}")
            print(f"Root: {payload['root'] or '<not configured>'}")
            print(f"Objects: {payload['objects']}; bytes: {payload['bytes']}")
        return 0 if payload["state"] != "MISCONFIGURED" else 1
    if command == "evidence-create":
        draft_path = _resolve_input_path(root, args.draft)
        payload, issues = create_evidence(root, draft_path)
        if payload is None:
            summary = issue_summary(issues, root)
            if as_json:
                _emit(summary, True)
            else:
                print_issues(issues, root)
            return 1
        result = {"status": "CAPTURED", "evidence_id": payload["id"], "manifest": f"evidence/manifests/{payload['id']}.json", "warnings": issue_summary(issues, root)["issues"]}
        if as_json:
            _emit(result, True)
        else:
            print(f"CAPTURED: {payload['id']} -> {result['manifest']}")
            if issues:
                print_issues(issues, root)
        return 0
    if command == "evidence-inspect":
        payload, issues = inspect_evidence(root, args.evidence_id)
        if payload is None:
            if as_json:
                _emit({"status": "FAIL", "issues": issue_summary(issues, root)["issues"]}, True)
            else:
                print_issues(issues, root)
            return 1
        if as_json:
            _emit(payload, True)
        else:
            print(json.dumps(payload, indent=2, ensure_ascii=False))
        return 0
    if command == "evidence-verify":
        payload = verify_evidence(root, args.evidence_id)
        if as_json:
            _emit(payload, True)
        else:
            print(f"{payload['state']}: {args.evidence_id}")
            for issue in payload["issues"]:
                print(f"{issue['severity']} {issue['code']}: {issue['message']}")
        return 0 if payload["state"] != "BROKEN" else 1
    if command == "evidence-seal":
        payload, issues = seal_evidence(root, args.evidence_id)
        if payload is None:
            if as_json:
                _emit(issue_summary(issues, root), True)
            else:
                print_issues(issues, root)
            return 1
        result = {"status": "SEALED", **payload}
        if as_json:
            _emit(result, True)
        else:
            print(f"SEALED: {args.evidence_id}")
        return 0
    if command == "evidence-for":
        records, issues = evidence_for(root, args.object_id)
        payload = {"object_id": args.object_id, "evidence": records, "issues": issue_summary(issues, root)["issues"]}
        if as_json:
            _emit(payload, True)
        else:
            print(f"Evidence for {args.object_id}: {len(records)}")
            for item in records:
                print(f"  {item['id']}: {item['state']} / {item['result']}")
        return 0 if not issues else 1
    return 2


def _run_diff(root: Path, old: str, new: str, as_json: bool) -> int:
    diff, issues = snapshot_diff(root, old, new)
    if issues:
        if as_json:
            _emit({"status": "FAIL", "issues": issue_summary(issues, root)["issues"]}, True)
        else:
            print_issues(issues, root)
        return 1
    if as_json:
        _emit({"status": "PASS", **diff}, True)
    else:
        print(f"Snapshot diff: {old} -> {new}")
        print(f"Added: {', '.join(diff['added']) or '<none>'}")
        print(f"Removed: {', '.join(diff['removed']) or '<none>'}")
        for change in diff["changed"]:
            print(f"Changed: {change['object_id']} {change['changes']}")
        print(f"Grade: SKLT {diff['grade']['sklt']['before']} -> {diff['grade']['sklt']['after']}; RARE {diff['grade']['rare']['before']} -> {diff['grade']['rare']['after']}")
    return 0


def _dispatch(args, root) -> int:
    as_json = bool(args.json)
    from .v04_cli import COMMANDS as v04_commands, run as run_v04
    if args.command in v04_commands:
        return run_v04(root, args)
    if args.command == "scan":
        return _run_scan(root, as_json)
    if args.command == "validate":
        return _run_validate(root, as_json)
    if args.command == "exp":
        return _run_exp(root, as_json)
    if args.command == "index":
        return _run_index(root, as_json)
    if args.command == "refresh":
        return _run_refresh(root, as_json)
    if args.command == "inspect":
        return _run_inspect(root, args.object_id, as_json)
    if args.command == "status":
        return _run_status(root, as_json)
    if args.command == "snapshot":
        return _run_snapshot(root, args.name, as_json, args.evidence_id)
    if args.command == "snapshot-diff":
        return _run_diff(root, args.old, args.new, as_json)
    if args.command in {
        "cas-put", "cas-get", "cas-verify", "cas-status",
        "evidence-create", "evidence-inspect", "evidence-verify",
        "evidence-seal", "evidence-for",
    }:
        return _run_evidence_command(root, args.command, args, as_json)
    return 2


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        # Preserve raw aliases/traversal until boundary validation has run.
        from .v04_sklt import checked_root
        raw_root = Path(args.root).absolute()
        if ".." in raw_root.parts:
            raise UnsafeOutputPath("root traversal is not permitted")
        root = checked_root(raw_root)
        return _dispatch(args, root)
    except (UnsafeOutputPath, PublicationError) as exc:
        code = "UNSAFE_OUTPUT_PATH" if isinstance(exc, UnsafeOutputPath) else "PUBLISH_FAILED"
        payload = {"status": "FAIL", "issues": [{"code": code, "severity": "ERROR", "message": str(exc)}],
                   "next_steps": ["Repair the explicit physical output paths and retry."],
                   "residual_files": getattr(exc, "residual", [])}
        _emit(payload, bool(args.json))
        if not args.json:
            print(str(exc))
        return 1


if __name__ == "__main__":
    sys.exit(main())
