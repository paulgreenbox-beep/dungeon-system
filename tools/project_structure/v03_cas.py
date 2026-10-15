from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO
from .v041_paths import (output_boundary, atomic_bytes, UnsafeOutputPath,
                        staged_file, _claimed_leaf, _publish_staged)
from .v04_sklt import checked_root, is_link


CAS_REF_RE = re.compile(r"^sha256:([0-9a-f]{64})$")
CAS_OBJECT_RE = re.compile(r"^[0-9a-f]{64}$")


class CasError(ValueError):
    pass


@dataclass(frozen=True)
class CasStatus:
    state: str
    configured: bool
    root: str | None
    object_count: int = 0
    bytes: int = 0

    def as_dict(self) -> dict[str, object]:
        return {
            "state": self.state,
            "configured": self.configured,
            "root": self.root,
            "objects": self.object_count,
            "bytes": self.bytes,
        }


def configured_store_root(project_root: Path) -> Path:
    project_root = checked_root(project_root)
    manifest_path = project_root / ".project" / "project.json"
    try:
        data = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CasError(f"cannot read project manifest: {exc}") from exc
    if not isinstance(data, dict):
        raise CasError("project manifest must be a JSON object")
    config = data.get("evidence_store")
    if not isinstance(config, dict) or config.get("type") != "local-cas":
        raise CasError("evidence_store.type must be 'local-cas'")
    raw_root = config.get("root")
    if not isinstance(raw_root, str) or not raw_root.strip():
        raise CasError("evidence_store.root must be a non-empty path")
    candidate = Path(os.path.expandvars(raw_root.strip())).expanduser()
    if not candidate.is_absolute():
        candidate = project_root / candidate
    for part in (candidate, *candidate.parents):
        if os.path.lexists(part) and is_link(part):
            raise UnsafeOutputPath(f"configured CAS path traverses a reparse point: {part}")
    resolved = candidate.resolve()
    try:
        resolved.relative_to(project_root)
    except ValueError:
        return resolved
    raise CasError("evidence_store.root must resolve outside the project repository")


def cas_root(project_root: Path) -> Path:
    return configured_store_root(project_root) / "cas" / "sha256"


def evidence_root(project_root: Path) -> Path:
    return project_root.resolve() / "evidence"


def cas_object_path(project_root: Path, digest_ref: str) -> Path:
    match = CAS_REF_RE.fullmatch(digest_ref)
    if not match:
        raise CasError("CAS reference must have the form sha256:<64 lowercase hex characters>")
    digest = match.group(1)
    return cas_root(project_root) / digest[:2] / digest[2:4] / digest


def hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return f"sha256:{digest.hexdigest()}"


def _copy_hash(source: BinaryIO, destination: BinaryIO) -> str:
    digest = hashlib.sha256()
    for chunk in iter(lambda: source.read(1024 * 1024), b""):
        digest.update(chunk)
        destination.write(chunk)
    return f"sha256:{digest.hexdigest()}"


def cas_put(project_root: Path, source: Path) -> tuple[str, bool, int]:
    # The configured external CAS is an explicitly named destination, not an
    # authorization to follow arbitrary links beneath it.
    store = configured_store_root(project_root)
    with output_boundary(project_root), output_boundary(store) as guard:
        return _cas_put_guarded(project_root, source, guard)


def _cas_put_guarded(project_root: Path, source: Path, guard) -> tuple[str, bool, int]:
    source = source.resolve()
    if not source.exists() or not source.is_file():
        raise CasError(f"input is not a regular file: {source}")
    try:
        root = cas_root(project_root)
        with source.open("rb") as stream:
            source_digest = hashlib.sha256()
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                source_digest.update(chunk)
        digest_ref = f"sha256:{source_digest.hexdigest()}"
        target = cas_object_path(project_root, digest_ref)
        guard.inspect(target)
        guard.directory(target.parent)
        with _claimed_leaf(target) as existing:
            if existing:
                digest, size = hashlib.sha256(), 0
                for chunk in existing.read_chunks():
                    digest.update(chunk)
                    size += len(chunk)
                actual = f"sha256:{digest.hexdigest()}"
                if actual != digest_ref:
                    raise CasError(f"existing CAS object is corrupt: expected {digest_ref}, found {actual}")
                return digest_ref, True, size

        with staged_file(guard.root, target.parent, prefix=".cas-put-") as stage:
            with source.open("rb") as input_stream:
                copied_ref = _copy_hash(input_stream, stage)
                stage.flush()
            if copied_ref != digest_ref:
                raise CasError("source file changed while it was being captured")
            # Hash the very same leased identity that the kernel will rename.
            digest, size = hashlib.sha256(), 0
            for chunk in stage.read_chunks():
                digest.update(chunk)
                size += len(chunk)
            if f"sha256:{digest.hexdigest()}" != digest_ref:
                raise CasError("pinned CAS stage differs from captured source")
            try:
                _publish_staged(guard, stage, target, expected=None, exclusive=True)
            except FileExistsError:
                with _claimed_leaf(target) as existing:
                    if existing is None:
                        raise CasError("concurrent CAS object disappeared")
                    digest, size = hashlib.sha256(), 0
                    for chunk in existing.read_chunks():
                        digest.update(chunk)
                        size += len(chunk)
                    if f"sha256:{digest.hexdigest()}" != digest_ref:
                        raise CasError(f"concurrent CAS object is corrupt: {target}")
                    return digest_ref, True, size
            # Unsupported exclusive atomic publication fails closed; do not
            # fall back to an in-place, partially visible write.
            return digest_ref, False, size
    except OSError as exc:
        raise CasError(f"CAS write failed: {exc}") from exc


def cas_verify(project_root: Path, digest_ref: str) -> tuple[bool, str | None, int | None]:
    target = cas_object_path(project_root, digest_ref)
    if not target.is_file():
        return False, "CAS object is missing", None
    try:
        actual = hash_file(target)
        size = target.stat().st_size
    except OSError as exc:
        return False, f"CAS object cannot be read: {exc}", None
    if actual != digest_ref:
        return False, f"CAS hash mismatch: expected {digest_ref}, found {actual}", size
    return True, None, size


def cas_get(project_root: Path, digest_ref: str, output: Path) -> int:
    valid, error, size = cas_verify(project_root, digest_ref)
    if not valid:
        raise CasError(error or "CAS verification failed")
    source = cas_object_path(project_root, digest_ref)
    output = output.expanduser().absolute()
    content = source.read_bytes()
    if "sha256:" + hashlib.sha256(content).hexdigest() != digest_ref:
        raise CasError("CAS object changed while being retrieved")
    try:
        atomic_bytes(project_root, output, content, exclusive=True)
    except FileExistsError as exc:
        raise CasError(f"output already exists; choose another path: {output}") from exc
    return int(size or 0)


def cas_status(project_root: Path) -> CasStatus:
    try:
        configured = configured_store_root(project_root)
    except CasError:
        return CasStatus("MISCONFIGURED", False, None)
    root = configured / "cas" / "sha256"
    if not root.is_dir():
        return CasStatus("NOT_INITIALIZED", True, str(configured))
    count = 0
    byte_count = 0
    try:
        for path in root.rglob("*"):
            if path.is_file() and CAS_OBJECT_RE.fullmatch(path.name):
                count += 1
                byte_count += path.stat().st_size
    except OSError:
        return CasStatus("UNAVAILABLE", True, str(configured), count, byte_count)
    return CasStatus("AVAILABLE", True, str(configured), count, byte_count)
