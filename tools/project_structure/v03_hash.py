from __future__ import annotations

import hashlib
from pathlib import Path

from .model import ProjectSnapshot
from .v03_manifest import ProjectManifest


def structural_hash(snapshot: ProjectSnapshot, manifest: ProjectManifest | None = None) -> str:
    digest = hashlib.sha256()
    files: list[Path] = []
    for module in snapshot.modules:
        files.extend(record.path for record in module.sklts)
        files.extend(record.path for record in module.glifs)
    if manifest is not None and manifest.path.exists():
        files.append(manifest.path)
    for path in sorted(set(files), key=lambda item: str(item).lower()):
        try:
            relative = path.relative_to(snapshot.root).as_posix()
            digest.update(relative.encode("utf-8"))
            digest.update(b"\0")
            digest.update(path.read_bytes())
            digest.update(b"\0")
        except OSError:
            continue
    return digest.hexdigest()
