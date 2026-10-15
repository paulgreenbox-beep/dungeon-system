from __future__ import annotations

import json
import os
import re
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path

from .model import Issue, ProjectSnapshot
from .parser import parse_projection
from .validator import module_counts, project_counts, render_counts
from .v041_paths import (output_boundary, json_bytes,
                        _Lease, _rename_handle, PublicationError)


SNAPSHOT_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


def _discard_stage(guard, directory, files, identities):
    """Remove only known owned identities/empty dirs; quarantine unknown residue."""
    residual = []
    for relative, file in files.items():
        try:
            if file.handle is None:
                file = _Lease(directory.path / relative)
            if file.identity != identities[relative]:
                raise ValueError("snapshot child identity changed; foreign leaf preserved")
            file.delete()
        except FileNotFoundError:
            pass
        except (OSError, ValueError) as exc:
            residual.append(str(exc))
        finally:
            file.close()
    for path in reversed(list(guard.pins)):
        if directory.path in path.parents:
            guard.pins.pop(path)()
    parents = {parent for relative in files for parent in (directory.path / relative).parents
               if directory.path in parent.parents}
    for path in sorted(parents, key=lambda p: len(p.parts), reverse=True):
        try:
            path.rmdir()
        except FileNotFoundError:
            pass
        except OSError as exc:
            residual.append(str(exc))
    try:
        directory.delete()
    except OSError as exc:
        residual.append(str(exc))
    if residual:
        quarantine = directory.path.parent / (".snapshot-rejected-" + uuid.uuid4().hex)
        try:
            _rename_handle(directory, quarantine)
        except OSError as exc:
            residual.append(str(exc))
        return [{"path": str(directory.path), "error": "; ".join(residual)}]
    return []


def create_snapshot(snapshot: ProjectSnapshot, snapshot_id: str, *, manifest_extra: dict | None = None) -> list[Issue]:
    if not SNAPSHOT_ID_RE.fullmatch(snapshot_id):
        return [Issue("INVALID_SNAPSHOT_ID", "snapshot name must contain only letters, numbers, '.', '_' or '-'")]
    snapshots_root = snapshot.root / "snapshots"
    target = snapshots_root / snapshot_id
    if target.exists():
        return [Issue("SNAPSHOT_EXISTS", f"snapshot '{snapshot_id}' already exists", target)]

    # Build all bytes before any publication, including the final v0.3 manifest.
    files = {}
    modules_manifest = []
    for module in snapshot.modules:
        module_token = "root" if module.name == "." else module.name.replace("/", "__")
        entry = {"name": module.name, "counts": module_counts(module),
                 "exp": f"modules/{module_token}/.exp", "sklt": [], "glif": []}
        if module.exp_path.exists():
            files[entry["exp"]] = module.exp_path.read_bytes()
        for kind, records in (("sklt", module.sklts), ("glif", module.glifs)):
            for record in records:
                relative = f"{kind}/{module_token}/{record.path.relative_to(module.path).as_posix()}"
                files[relative] = record.path.read_bytes()
                entry[kind].append(relative)
        modules_manifest.append(entry)
    for source, relative in ((snapshot.root / "docs/map.md", "map.md"), (snapshot.global_grade_path, "grade")):
        if source.exists():
            files[relative] = source.read_bytes()
    manifest = {"schema": "dungeon-snapshot/v1", "id": snapshot_id, "label": snapshot_id,
                "created_at": datetime.now(timezone.utc).isoformat(), "project_root": str(snapshot.root),
                "grade": project_counts(snapshot), "modules": modules_manifest,
                "source_code_copied": False, "git_history_copied": False, **(manifest_extra or {})}
    files["manifest.json"] = json_bytes(manifest)
    with output_boundary(snapshot.root, [target, *(target / p for p in files)]) as guard:
        guard.directory(snapshots_root)
        temporary_path = Path(tempfile.mkdtemp(prefix=f".{snapshot_id}.", dir=snapshots_root))
        directory = _Lease(temporary_path, directory=True)
        # Use the DELETE-capable root identity as the boundary's pin. Do not
        # close/reopen it before publication merely to make pathname rename work.
        guard.pins[temporary_path] = directory.close
        staged, identities = {}, {}
        try:
            for relative, content in files.items():
                destination = temporary_path / relative
                guard.directory(destination.parent)
                guard.inspect(destination)
                file = _Lease(destination, create=True)
                staged[relative], identities[relative] = file, file.identity
                file.write(content)
                file.flush()
            # Windows refuses directory rename while ANY child file is open,
            # even with SHARE_DELETE. Close children, never the stage-root
            # DELETE identity; then re-lease/validate children after rename.
            # This bounded correspondence check is NOT a hostile same-user
            # atomic guarantee for the entire child namespace during that gap.
            for file in staged.values():
                file.close()
            for path in reversed(list(guard.pins)):
                if temporary_path in path.parents:
                    guard.pins.pop(path)()
            guard.inspect(target)
            _rename_handle(directory, target)
            guard.pins.pop(temporary_path, None)
            guard.pins[target] = directory.close
            for relative, content in files.items():
                guard.inspect(target / relative)
                file = _Lease(target / relative)
                staged[relative] = file
                if file.identity != identities[relative] or file.read_bytes() != content:
                    raise PublicationError(f"snapshot child changed during publication: {relative}")
            expected = {str(Path(relative)) for relative in files}
            expected.update(str(parent) for relative in files for parent in Path(relative).parents if parent != Path("."))
            observed = set()
            for current, dirs, names in os.walk(target, followlinks=False):
                for name in dirs + names:
                    path = Path(current) / name
                    guard.inspect(path)
                    relative = str(path.relative_to(target))
                    if relative not in expected:
                        raise PublicationError(f"unexpected snapshot stage entry: {relative}")
                    observed.add(relative)
            if observed != expected:
                raise PublicationError("snapshot stage entries changed during publication")
        except BaseException as exc:
            residual = _discard_stage(guard, directory, staged, identities)
            if residual:
                raise PublicationError(str(exc), residual) from exc
            raise
        finally:
            for file in staged.values():
                file.close()
            guard.pins.pop(temporary_path, None)
            guard.pins.pop(target, None)
            directory.close()
    return []


def validate_snapshots(root: Path) -> list[Issue]:
    issues: list[Issue] = []
    snapshots_root = root / "snapshots"
    if not snapshots_root.exists():
        return issues
    for snapshot_dir in sorted(snapshots_root.iterdir(), key=lambda item: item.name.lower()):
        if not snapshot_dir.is_dir() or snapshot_dir.name.startswith("."):
            continue
        manifest_path = snapshot_dir / "manifest.json"
        if not manifest_path.exists():
            issues.append(Issue("INVALID_SNAPSHOT_MANIFEST", "snapshot is missing manifest.json", snapshot_dir))
            continue
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            issues.append(Issue("INVALID_SNAPSHOT_MANIFEST", f"cannot parse manifest.json: {exc}", manifest_path))
            continue
        if not isinstance(manifest, dict):
            issues.append(Issue("INVALID_SNAPSHOT_MANIFEST", "manifest must be a JSON object", manifest_path))
            continue
        required = ("schema", "id", "created_at", "project_root", "grade", "modules")
        for field_name in required:
            if field_name not in manifest:
                issues.append(Issue("INVALID_SNAPSHOT_MANIFEST", f"manifest is missing '{field_name}'", manifest_path))
        if manifest.get("schema") != "dungeon-snapshot/v1":
            issues.append(Issue("INVALID_SNAPSHOT_MANIFEST", "unsupported snapshot schema", manifest_path))
        if manifest.get("id") != snapshot_dir.name:
            issues.append(Issue("INVALID_SNAPSHOT_MANIFEST", "manifest id does not match directory name", manifest_path))
        grade = manifest.get("grade")
        if not isinstance(grade, dict) or not all(isinstance(grade.get(key), int) and grade[key] >= 0 for key in ("sklt", "rare")):
            issues.append(Issue("INVALID_SNAPSHOT_MANIFEST", "grade must contain non-negative integer sklt and rare", manifest_path))
        elif grade["rare"] > grade["sklt"]:
            issues.append(Issue("INVALID_SNAPSHOT_MANIFEST", "snapshot rare cannot exceed sklt", manifest_path))
        modules = manifest.get("modules")
        if not isinstance(modules, list):
            issues.append(Issue("INVALID_SNAPSHOT_MANIFEST", "modules must be an array", manifest_path))
        else:
            for module in modules:
                if not isinstance(module, dict) or not isinstance(module.get("name"), str) or not isinstance(module.get("counts"), dict):
                    issues.append(Issue("INVALID_SNAPSHOT_MANIFEST", "each module needs name and counts", manifest_path))
        grade_file = snapshot_dir / "grade"
        if grade_file.exists():
            actual, projection_issues = parse_projection(grade_file)
            issues.extend(projection_issues)
            if actual is not None and isinstance(grade, dict) and actual != {"sklt": grade.get("sklt"), "rare": grade.get("rare")}:
                issues.append(Issue("INVALID_SNAPSHOT_MANIFEST", "grade file does not match manifest grade", grade_file))
    return issues
