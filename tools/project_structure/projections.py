from __future__ import annotations

from pathlib import Path

from .model import Issue, ProjectSnapshot
from .validator import module_counts, project_counts, render_counts, validate_snapshot
from .v041_paths import publish_files


def projection_files(snapshot: ProjectSnapshot) -> dict[Path, bytes]:
    files = {module.exp_path: render_counts(module_counts(module)).encode("utf-8") for module in snapshot.modules}
    files[snapshot.global_grade_path] = render_counts(project_counts(snapshot)).encode("utf-8")
    return files


def write_projections(snapshot: ProjectSnapshot) -> list[Issue]:
    structural_issues = validate_snapshot(snapshot, include_projections=False)
    if structural_issues:
        return structural_issues
    publish_files(snapshot.root, projection_files(snapshot))
    return []
