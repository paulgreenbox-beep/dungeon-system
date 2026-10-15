from __future__ import annotations

from pathlib import Path

from .model import Issue


WARNING_CODES = frozenset({
    "STALE_EXP",
    "STALE_GRADE",
    "STALE_OBJECT_INDEX",
    "STALE_SNAPSHOT",
    "PROJECT_CHANGED_SINCE_SNAPSHOT",
    "MISSING_PROJECTION_METADATA",
    "MAP_MAY_BE_STALE",
    "MODULES_DOC_MAY_BE_STALE",
    "CIRCULAR_DEPENDENCY",
    "UNRESOLVED_TOOLKIT_REF",
})
INFO_CODES = frozenset({"NO_SNAPSHOTS", "LEGACY_MANIFEST", "LEGACY_SCHEMA"})


def severity_for(code: str) -> str:
    if code in WARNING_CODES:
        return "WARNING"
    if code in INFO_CODES:
        return "INFO"
    return "ERROR"


def issue_to_dict(issue: Issue, root: Path) -> dict[str, str]:
    path = ""
    if issue.path is not None:
        try:
            path = issue.path.resolve().relative_to(root.resolve()).as_posix()
        except ValueError:
            path = str(issue.path)
    return {
        "code": issue.code,
        "severity": severity_for(issue.code),
        "path": path,
        "message": issue.message,
        **({"line": str(issue.line)} if issue.line is not None else {}),
    }


def issue_summary(issues: list[Issue], root: Path) -> dict[str, object]:
    payload = [issue_to_dict(issue, root) for issue in issues]
    errors = sum(item["severity"] == "ERROR" for item in payload)
    warnings = sum(item["severity"] == "WARNING" for item in payload)
    infos = sum(item["severity"] == "INFO" for item in payload)
    return {
        "status": "PASS" if errors == 0 else "FAIL",
        "errors": errors,
        "warnings": warnings,
        "infos": infos,
        "issues": payload,
    }


def print_issues(issues: list[Issue], root: Path) -> None:
    for issue in issues:
        print(f"{severity_for(issue.code)} {issue.format(root)}")
