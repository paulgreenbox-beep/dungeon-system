from __future__ import annotations

import json
import os
import re
from dataclasses import asdict, dataclass
from pathlib import Path

from .model import Issue, ProjectSnapshot


TOOLKIT_PROVIDER = "rust_makepad_toolkit"
TOOLKIT_INDEX_RELATIVE = "docs/INDEX.md"
SUPPORTED_KINDS = frozenset({"container", "tool"})
_PAIR_RE = re.compile(r"(?P<key>provider|kind|type|id|purpose)\s*:\s*(?P<value>[A-Za-z0-9_.:/-]+)", re.IGNORECASE)
_LINK_RE = re.compile(r"\]\((?P<link>[^)]*environment-catalog\.json)\)", re.IGNORECASE)
_TOKEN_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:/-]*$")


@dataclass(frozen=True)
class ToolkitRef:
    provider: str
    kind: str
    id: str
    purpose: str | None = None

    def as_dict(self) -> dict[str, str]:
        data = {"type": self.kind, "provider": self.provider, "id": self.id}
        if self.purpose:
            data["purpose"] = self.purpose
        return data


def _raw_execution(record) -> str | None:
    value = record.fields.get("execution")
    if value is None or value == "" or value == []:
        return None
    if isinstance(value, list):
        return " ".join(str(item) for item in value if str(item).strip())
    if isinstance(value, str):
        return value
    return None


def parse_toolkit_refs(record) -> tuple[list[ToolkitRef], list[Issue]]:
    raw = _raw_execution(record)
    if raw is None:
        return [], []
    matches = list(_PAIR_RE.finditer(raw))
    values: dict[str, str] = {}
    issues: list[Issue] = []
    for match in matches:
        key = match.group("key").lower()
        if key == "type":
            key = "kind"
        if key in values:
            issues.append(Issue("INVALID_TOOLKIT_REF", f"execution contains duplicate '{key}'", record.path))
        values[key] = match.group("value")
    if not matches:
        return [], [Issue("INVALID_TOOLKIT_REF", "execution must contain provider:, kind: and id:", record.path)]
    missing = [key for key in ("provider", "kind", "id") if not values.get(key)]
    if missing:
        issues.append(Issue("INVALID_TOOLKIT_REF", f"execution is missing: {', '.join(missing)}", record.path))
        return [], issues
    for key in ("provider", "kind", "id", "purpose"):
        value = values.get(key)
        if value and not _TOKEN_RE.fullmatch(value):
            issues.append(Issue("INVALID_TOOLKIT_REF", f"execution {key} is not a stable token", record.path))
    kind = values["kind"].lower()
    if kind not in SUPPORTED_KINDS:
        issues.append(Issue("INVALID_TOOLKIT_REF", f"execution kind must be one of {sorted(SUPPORTED_KINDS)}", record.path))
    if issues:
        return [], issues
    return [ToolkitRef(values["provider"], kind, values["id"], values.get("purpose"))], []


def _toolkit_root_candidates(project_root: Path) -> list[Path]:
    candidates: list[Path] = []
    configured = os.environ.get("RUST_MAKEPAD_TOOLKIT_ROOT")
    if configured:
        candidates.append(Path(configured).expanduser())
    candidates.append(project_root.parent / "Rust_Makepad_Toolkit")
    return [candidate.resolve() for candidate in candidates if candidate]


def _environment_catalog(project_root: Path) -> tuple[Path | None, str | None]:
    for toolkit_root in _toolkit_root_candidates(project_root):
        index_path = toolkit_root / TOOLKIT_INDEX_RELATIVE
        if not index_path.is_file():
            continue
        try:
            index_text = index_path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        link = _LINK_RE.search(index_text)
        if not link:
            continue
        catalog_path = (index_path.parent / link.group("link")).resolve()
        if catalog_path.is_file():
            return catalog_path, str(index_path)
    return None, None


def _load_environment_catalog(project_root: Path) -> tuple[dict | None, Path | None, str | None]:
    catalog_path, index_path = _environment_catalog(project_root)
    if catalog_path is None:
        return None, None, index_path
    try:
        data = json.loads(catalog_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None, catalog_path, index_path
    return data if isinstance(data, dict) else None, catalog_path, index_path


def validate_toolkit_refs(snapshot: ProjectSnapshot) -> tuple[dict[str, list[ToolkitRef]], list[Issue]]:
    refs_by_object: dict[str, list[ToolkitRef]] = {}
    issues: list[Issue] = []
    catalog: dict | None = None
    catalog_path: Path | None = None
    index_path: str | None = None
    loaded_catalog = False
    for record in snapshot.sklts:
        refs, parse_issues = parse_toolkit_refs(record)
        issues.extend(parse_issues)
        if record.object_id:
            refs_by_object[record.object_id] = refs
        for ref in refs:
            if ref.provider != TOOLKIT_PROVIDER:
                issues.append(Issue("UNRESOLVED_TOOLKIT_REF", f"external provider is not locally resolved: {ref.provider}", record.path))
                continue
            if not loaded_catalog:
                catalog, catalog_path, index_path = _load_environment_catalog(snapshot.root)
                loaded_catalog = True
            if catalog is None:
                location = index_path or "Rust_Makepad_Toolkit/docs/INDEX.md"
                issues.append(Issue("UNRESOLVED_TOOLKIT_REF", f"cannot resolve Toolkit catalog through {location}", record.path))
                continue
            if ref.kind != "container":
                issues.append(Issue("UNRESOLVED_TOOLKIT_REF", f"Toolkit tool id is not checked by the environment catalog: {ref.id}", record.path))
                continue
            environments = catalog.get("environments", [])
            match = next((item for item in environments if isinstance(item, dict) and item.get("key") == ref.id), None)
            if match is None or match.get("kind") != "container":
                issues.append(Issue("UNKNOWN_TOOLKIT_CONTAINER", f"unknown Toolkit container '{ref.id}'", record.path))
    return refs_by_object, issues


def flatten_toolkit_refs(refs_by_object: dict[str, list[ToolkitRef]]) -> list[ToolkitRef]:
    return [ref for object_id in sorted(refs_by_object) for ref in refs_by_object[object_id]]
