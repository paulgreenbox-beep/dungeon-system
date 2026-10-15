from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .model import Issue, ProjectSnapshot


REQUIRED_DESCRIPTOR_FIELDS = ("id", "version", "source", "purpose", "license", "used_by")


@dataclass
class LibraryDescriptor:
    path: Path
    data: dict[str, Any]

    @property
    def library_id(self) -> str | None:
        value = self.data.get("id")
        return value.strip() if isinstance(value, str) and value.strip() else None


@dataclass
class LibraryRegistry:
    root: Path
    descriptors: list[LibraryDescriptor] = field(default_factory=list)
    issues: list[Issue] = field(default_factory=list)

    @property
    def by_id(self) -> dict[str, LibraryDescriptor]:
        return {descriptor.library_id: descriptor for descriptor in self.descriptors if descriptor.library_id}


def discover_libraries(root: Path) -> LibraryRegistry:
    root = root.resolve()
    registry = LibraryRegistry(root=root)
    libs_root = root / "libs"
    if not libs_root.exists():
        return registry

    for library_dir in sorted(libs_root.iterdir(), key=lambda item: item.name.lower()):
        if not library_dir.is_dir() or library_dir.name.startswith("."):
            continue
        descriptor_path = library_dir / "lib.json"
        if not descriptor_path.exists():
            registry.issues.append(Issue("INVALID_LIBRARY_DESCRIPTOR", "library directory must contain lib.json", library_dir))
            continue
        try:
            data = json.loads(descriptor_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            registry.issues.append(Issue("INVALID_LIBRARY_DESCRIPTOR", f"cannot parse lib.json: {exc}", descriptor_path))
            continue
        if not isinstance(data, dict):
            registry.issues.append(Issue("INVALID_LIBRARY_DESCRIPTOR", "descriptor must be a JSON object", descriptor_path))
            continue
        descriptor = LibraryDescriptor(path=descriptor_path, data=data)
        registry.descriptors.append(descriptor)
        for field_name in REQUIRED_DESCRIPTOR_FIELDS:
            if field_name not in data:
                registry.issues.append(Issue("INVALID_LIBRARY_DESCRIPTOR", f"missing required field '{field_name}'", descriptor_path))
        if "id" in data and (not isinstance(data["id"], str) or not data["id"].strip()):
            registry.issues.append(Issue("INVALID_LIBRARY_DESCRIPTOR", "id must be a non-empty string", descriptor_path))
        for field_name in ("version", "source", "purpose", "license"):
            if field_name in data and (not isinstance(data[field_name], str) or not data[field_name].strip()):
                registry.issues.append(Issue("INVALID_LIBRARY_DESCRIPTOR", f"{field_name} must be a non-empty string", descriptor_path))
        if "used_by" in data and (
            not isinstance(data["used_by"], list)
            or any(not isinstance(item, str) or not item.strip() for item in data["used_by"])
        ):
            registry.issues.append(Issue("INVALID_LIBRARY_DESCRIPTOR", "used_by must be an array of strings", descriptor_path))

    seen: dict[str, LibraryDescriptor] = {}
    for descriptor in registry.descriptors:
        if not descriptor.library_id:
            continue
        if descriptor.library_id in seen:
            registry.issues.append(Issue("DUPLICATE_LIBRARY_ID", f"library id '{descriptor.library_id}' is duplicated", descriptor.path))
        else:
            seen[descriptor.library_id] = descriptor
    return registry


def _library_refs(record) -> list[str]:
    value = record.fields.get("libraries")
    if isinstance(value, list):
        return [item.strip() for item in value if isinstance(item, str) and item.strip()]
    if isinstance(value, str):
        return [item for item in value.replace(",", " ").split() if item]
    return []


def validate_library_refs(snapshot: ProjectSnapshot, registry: LibraryRegistry) -> list[Issue]:
    issues: list[Issue] = []
    known_ids = set(registry.by_id)
    for record in snapshot.sklts:
        for library_id in _library_refs(record):
            if library_id not in known_ids:
                issues.append(Issue("UNKNOWN_LIBRARY", f"SKLT references unknown library '{library_id}'", record.path))
    return issues
