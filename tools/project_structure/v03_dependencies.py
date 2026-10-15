from __future__ import annotations

from dataclasses import asdict, dataclass

from .model import Issue, ProjectSnapshot
from .v03_toolkit import validate_toolkit_refs


@dataclass(frozen=True)
class Dependency:
    type: str
    id: str

    def as_dict(self) -> dict[str, str]:
        return asdict(self)


def library_refs(record) -> list[str]:
    value = record.fields.get("libraries")
    if isinstance(value, list):
        return [item.strip() for item in value if isinstance(item, str) and item.strip()]
    if isinstance(value, str):
        return [item for item in value.replace(",", " ").split() if item]
    return []


def normalize_dependencies(snapshot: ProjectSnapshot, library_ids: set[str]) -> tuple[dict[str, list[Dependency]], list[Issue]]:
    object_ids = {record.object_id for record in snapshot.sklts if record.object_id}
    result: dict[str, list[Dependency]] = {}
    issues: list[Issue] = []
    _, toolkit_issues = validate_toolkit_refs(snapshot)
    issues.extend(toolkit_issues)
    for record in snapshot.sklts:
        if not record.object_id:
            continue
        for library_id in library_refs(record):
            if library_id not in library_ids:
                issues.append(Issue("UNKNOWN_LIBRARY", f"unknown library reference '{library_id}'", record.path))
        dependencies: list[Dependency] = []
        raw_value = record.fields.get("depends_on", [])
        raw_dependencies = raw_value if isinstance(raw_value, list) else [raw_value]
        for raw in raw_dependencies:
            if not isinstance(raw, str) or not raw.strip():
                continue
            dependency_id = raw.strip()
            if dependency_id.startswith("library:"):
                dependency = Dependency("library", dependency_id.removeprefix("library:"))
            elif dependency_id.startswith("tool:"):
                dependency = Dependency("tool", dependency_id.removeprefix("tool:"))
            elif dependency_id.startswith("container:"):
                dependency = Dependency("container", dependency_id.removeprefix("container:"))
            elif dependency_id.startswith("external:"):
                dependency = Dependency("external", dependency_id.removeprefix("external:"))
            elif dependency_id.startswith("object:"):
                dependency = Dependency("object", dependency_id.removeprefix("object:"))
            elif dependency_id in library_ids:
                dependency = Dependency("library", dependency_id)
            elif dependency_id in object_ids:
                dependency = Dependency("object", dependency_id)
            elif "." not in dependency_id:
                dependency = Dependency("external", dependency_id)
            else:
                dependency = Dependency("external", dependency_id)
                issues.append(Issue("UNKNOWN_OBJECT_DEPENDENCY", f"unknown object dependency '{dependency_id}'", record.path))
            if dependency.id == record.object_id:
                issues.append(Issue("SELF_DEPENDENCY", f"object '{record.object_id}' depends on itself", record.path))
            if dependency.type == "library" and dependency.id not in library_ids:
                issues.append(Issue("UNKNOWN_LIBRARY", f"unknown library dependency '{dependency.id}'", record.path))
            dependencies.append(dependency)
        result[record.object_id] = dependencies

    graph = {object_id: [dependency.id for dependency in dependencies if dependency.type == "object"] for object_id, dependencies in result.items()}
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(node: str) -> None:
        if node in visiting:
            issues.append(Issue("CIRCULAR_DEPENDENCY", f"circular object dependency includes '{node}'"))
            return
        if node in visited:
            return
        visiting.add(node)
        for dependency in graph.get(node, []):
            visit(dependency)
        visiting.remove(node)
        visited.add(node)

    for object_id in graph:
        visit(object_id)
    return result, issues
