from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from pathlib import Path

from .model import Issue, ProjectSnapshot


SOURCE_REF_RE = re.compile(
    r"path\s*:\s*(?P<path>[^\s,]+)(?:\s+symbol\s*:\s*(?P<symbol>[^\s,]+))?(?:\s+span\s*:\s*(?P<span>[^\s,]+))?",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class SourceRef:
    path: str
    symbol: str | None = None
    span: str | None = None

    def as_dict(self) -> dict[str, str]:
        return {key: value for key, value in asdict(self).items() if value is not None}


def parse_source_refs(record) -> tuple[list[SourceRef], list[Issue]]:
    value = record.fields.get("source")
    if value is None or value == [] or value == "":
        return [], []
    if isinstance(value, list):
        text = " ".join(str(item) for item in value)
    elif isinstance(value, str):
        text = value
    else:
        return [], [Issue("INVALID_SOURCE_REF", "source must contain path-based references", record.path)]
    refs = [SourceRef(match.group("path"), match.group("symbol"), match.group("span")) for match in SOURCE_REF_RE.finditer(text)]
    if not refs:
        return [], [Issue("INVALID_SOURCE_REF", "source reference must contain path:", record.path)]
    return refs, []


def validate_source_refs(snapshot: ProjectSnapshot) -> tuple[dict[str, list[SourceRef]], list[Issue]]:
    refs_by_object: dict[str, list[SourceRef]] = {}
    issues: list[Issue] = []
    for record in snapshot.sklts:
        refs, parse_issues = parse_source_refs(record)
        issues.extend(parse_issues)
        if record.object_id:
            refs_by_object[record.object_id] = refs
        for ref in refs:
            source_path = Path(ref.path)
            candidate = source_path if source_path.is_absolute() else snapshot.root / source_path
            if not candidate.exists() or not candidate.is_file():
                issues.append(Issue("SOURCE_PATH_MISSING", f"source path does not exist: {ref.path}", record.path))
    return refs_by_object, issues
