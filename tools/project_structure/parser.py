from __future__ import annotations

import json
import re
from pathlib import Path

from .model import Issue, LIST_FIELDS, REQUIRED_FIELDS, GlifRecord, SkltRecord, VALID_STATES


KEY_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_.-]*):(?:[ \t]*(.*))?$")


def _normalise_list(lines: list[str]) -> list[str]:
    values: list[str] = []
    for line in lines:
        value = line.strip()
        if value.startswith("-"):
            value = value[1:].strip()
        if value:
            values.append(value)
    return values


def _normalise_scalar(lines: list[str]) -> str:
    return " ".join(line.strip() for line in lines if line.strip()).strip()


def parse_sklt(path: Path) -> tuple[SkltRecord, list[Issue]]:
    issues: list[Issue] = []
    record = SkltRecord(path=path)
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except UnicodeDecodeError as exc:
        issues.append(Issue("INVALID_SKLT_SYNTAX", f"file is not UTF-8: {exc}", path))
        return record, issues
    except OSError as exc:
        issues.append(Issue("INVALID_SKLT_SYNTAX", f"cannot read file: {exc}", path))
        return record, issues

    current_key: str | None = None
    current_lines: list[str] = []
    current_line = 0

    def flush() -> None:
        nonlocal current_key, current_lines, current_line
        if current_key is None:
            return
        if current_key in record.fields:
            issues.append(Issue("DUPLICATE_SKLT_FIELD", f"field '{current_key}' appears more than once", path, current_line))
        elif current_key in LIST_FIELDS:
            record.fields[current_key] = _normalise_list(current_lines)
        else:
            record.fields[current_key] = _normalise_scalar(current_lines)
        record.line_by_key.setdefault(current_key, current_line)
        current_key = None
        current_lines = []
        current_line = 0

    for number, raw_line in enumerate(lines, start=1):
        if not raw_line.strip() or raw_line.lstrip().startswith("#"):
            continue
        if raw_line[:1].isspace():
            if current_key is None:
                issues.append(Issue("INVALID_SKLT_SYNTAX", "indented value has no field", path, number))
            else:
                current_lines.append(raw_line)
            continue
        match = KEY_RE.match(raw_line)
        if match is None:
            issues.append(Issue("INVALID_SKLT_SYNTAX", "expected 'field: value'", path, number))
            continue
        flush()
        current_key = match.group(1)
        current_line = number
        inline_value = match.group(2) or ""
        current_lines = [inline_value] if inline_value else []
    flush()

    for required in REQUIRED_FIELDS:
        if required not in record.fields:
            issues.append(Issue("MISSING_SKLT_FIELD", f"missing required field '{required}'", path))
        elif required not in LIST_FIELDS and not record.has_value(required):
            issues.append(Issue("MISSING_SKLT_FIELD", f"field '{required}' must not be empty", path, record.line_by_key.get(required)))

    if "state" in record.fields and record.state not in VALID_STATES:
        issues.append(Issue("INVALID_STATE", f"state must be one of {sorted(VALID_STATES)}", path, record.line_by_key.get("state")))

    return record, issues


def parse_glif(path: Path) -> tuple[GlifRecord, list[Issue]]:
    issues: list[Issue] = []
    record = GlifRecord(path=path)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        issues.append(Issue("INVALID_GLIF_SYNTAX", f"cannot parse JSON: {exc}", path))
        return record, issues

    if not isinstance(data, dict):
        issues.append(Issue("INVALID_GLIF_SYNTAX", "top-level JSON value must be an object", path))
        return record, issues
    record.data = data

    object_id = data.get("object_id")
    if not isinstance(object_id, str) or not object_id.strip():
        issues.append(Issue("MISSING_OBJECT_ID", "GLIF must contain a non-empty string object_id", path))
    else:
        record.object_id = object_id.strip()

    elements = data.get("elements")
    if not isinstance(elements, list):
        issues.append(Issue("INVALID_GLIF_ELEMENTS", "GLIF elements must be an array", path))
        return record, issues

    for index, element in enumerate(elements):
        if not isinstance(element, dict):
            issues.append(Issue("INVALID_GLIF_ELEMENT", f"element at index {index} must be an object", path))
            continue
        element_id = element.get("id")
        if not isinstance(element_id, str) or not element_id.strip():
            issues.append(Issue("MISSING_GLIF_ELEMENT_ID", f"element at index {index} has no valid id", path))
            continue
        record.element_ids.append(element_id.strip())
    return record, issues


def parse_projection(path: Path) -> tuple[dict[str, int] | None, list[Issue]]:
    issues: list[Issue] = []
    values: dict[str, int] = {}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError) as exc:
        return None, [Issue("INVALID_EXP", f"cannot read projection: {exc}", path)]

    for number, raw_line in enumerate(lines, start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        if len(parts) != 2 or parts[0] not in {"sklt", "rare"}:
            issues.append(Issue("INVALID_EXP", "expected exactly 'sklt N' or 'rare N'", path, number))
            continue
        if parts[0] in values:
            issues.append(Issue("INVALID_EXP", f"duplicate projection key '{parts[0]}'", path, number))
            continue
        try:
            value = int(parts[1])
        except ValueError:
            issues.append(Issue("INVALID_EXP", f"projection value for '{parts[0]}' must be an integer", path, number))
            continue
        if value < 0:
            issues.append(Issue("INVALID_EXP", "projection values cannot be negative", path, number))
            continue
        values[parts[0]] = value

    for key in ("sklt", "rare"):
        if key not in values:
            issues.append(Issue("INVALID_EXP", f"projection is missing '{key}'", path))
    if not issues and values["rare"] > values["sklt"]:
        issues.append(Issue("INVALID_EXP", "rare cannot exceed sklt", path))
    return values if not issues else None, issues
