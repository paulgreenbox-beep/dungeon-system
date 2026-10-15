from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


LIST_FIELDS = frozenset({"inputs", "outputs", "depends_on", "effects", "flags"})
REQUIRED_FIELDS = (
    "id",
    "object_id",
    "kind",
    "responsibility",
    "inputs",
    "outputs",
    "depends_on",
    "effects",
    "state",
)
VALID_STATES = frozenset({"SKLT", "RARE"})
VALID_LEVELS = frozenset({"L0", "L1", "L2", "L3", "L4"})
STORAGE_STATES = frozenset({"LIVE", "CAS", "PACKED", "ARCHIVED", "OFFLINE", "MISSING", "CORRUPT"})
RETENTION_CLASSES = frozenset({"PERMANENT", "CACHE", "EPHEMERAL", "HOT"})
STORAGE_FLAGS = frozenset({"PINNED", "SEALED", "GC_CANDIDATE", "RESTORE_REQUIRED", "REPLICATED"})
DEFAULT_STORAGE_STATE = "LIVE"
GIT_SCOPE_MODES = frozenset({"MONOREPO", "REPOSITORY", "EXTERNAL"})
ISOLATION_LEVELS = frozenset({"SHARED", "BOUNDED", "ISOLATED", "EXTERNAL"})
DEFAULT_GIT_SCOPE = "main"
OPTIONAL_FIELDS = (
    "schema_version",
    "libraries",
    "source",
    "execution",
    "level",
    "storage_state",
    "retention",
    "flags",
    "git_scope",
    "isolation",
)


@dataclass(frozen=True)
class Issue:
    code: str
    message: str
    path: Path | None = None
    line: int | None = None

    def format(self, root: Path | None = None) -> str:
        location = ""
        if self.path is not None:
            path = self.path
            if root is not None:
                try:
                    path = path.relative_to(root)
                except ValueError:
                    pass
            location = str(path)
            if self.line is not None:
                location += f":{self.line}"
            location += ": "
        return f"{self.code}: {location}{self.message}"


@dataclass
class SkltRecord:
    path: Path
    fields: dict[str, Any] = field(default_factory=dict)
    line_by_key: dict[str, int] = field(default_factory=dict)

    @property
    def object_id(self) -> str | None:
        value = self.fields.get("object_id")
        return value.strip() if isinstance(value, str) and value.strip() else None

    @property
    def record_id(self) -> str | None:
        value = self.fields.get("id")
        return value.strip() if isinstance(value, str) and value.strip() else None

    @property
    def state(self) -> str | None:
        value = self.fields.get("state")
        return value.strip().upper() if isinstance(value, str) and value.strip() else None

    @property
    def level(self) -> str | None:
        value = self.fields.get("level")
        return value.strip().upper() if isinstance(value, str) and value.strip() else None

    @property
    def storage_state(self) -> str:
        value = self.fields.get("storage_state")
        return value.strip().upper() if isinstance(value, str) and value.strip() else DEFAULT_STORAGE_STATE

    @property
    def retention(self) -> str | None:
        value = self.fields.get("retention")
        return value.strip().upper() if isinstance(value, str) and value.strip() else None

    @property
    def flags(self) -> list[str]:
        value = self.fields.get("flags")
        if isinstance(value, list):
            return [item.strip().upper() for item in value if isinstance(item, str) and item.strip()]
        if isinstance(value, str):
            return [item.strip().upper() for item in value.replace(",", " ").split() if item]
        return []

    @property
    def git_scope(self) -> str | None:
        value = self.fields.get("git_scope")
        return value.strip() if isinstance(value, str) and value.strip() else None

    @property
    def isolation(self) -> str | None:
        value = self.fields.get("isolation")
        return value.strip().upper() if isinstance(value, str) and value.strip() else None

    def has_value(self, key: str) -> bool:
        value = self.fields.get(key)
        if isinstance(value, list):
            return bool(value)
        return isinstance(value, str) and bool(value.strip())


@dataclass
class GlifRecord:
    path: Path
    data: dict[str, Any] = field(default_factory=dict)
    object_id: str | None = None
    element_ids: list[str] = field(default_factory=list)


@dataclass
class ModuleSnapshot:
    name: str
    path: Path
    sklts: list[SkltRecord] = field(default_factory=list)
    glifs: list[GlifRecord] = field(default_factory=list)

    @property
    def exp_path(self) -> Path:
        return self.path / ".exp"


@dataclass
class ProjectSnapshot:
    root: Path
    modules: list[ModuleSnapshot] = field(default_factory=list)
    issues: list[Issue] = field(default_factory=list)

    @property
    def global_grade_path(self) -> Path:
        return self.root / "docs" / ".grade"

    @property
    def sklts(self) -> list[SkltRecord]:
        return [record for module in self.modules for record in module.sklts]

    @property
    def glifs(self) -> list[GlifRecord]:
        return [record for module in self.modules for record in module.glifs]
