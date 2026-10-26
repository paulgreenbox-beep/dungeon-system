"""Explicit disposable-fixture copy, NOT a Windows/WSL Scion transport.

The actual working copy remains on ext4. A byte-checked Windows mirror allows
the existing Windows-only writer to capture its source bytes and local base.
No token/context, production path, checkout recovery or history is copied.
"""
from __future__ import annotations
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import stat


def physical(path: Path, *, missing=False) -> None:
    if not path.is_absolute() or ".." in path.parts:
        raise ValueError("absolute physical fixture path required")
    for item in (*reversed(path.parents), path):
        try:
            info = item.lstat()
        except FileNotFoundError:
            if missing and item == path:
                continue
            raise
        if stat.S_ISLNK(info.st_mode):
            raise ValueError("symlink fixture route refused")


def inventory(root: Path, token: bytes) -> dict[str, str]:
    result = {}
    total = 0
    for current, directories, files in os.walk(root, followlinks=False):
        for name in directories + files:
            path = Path(current) / name
            info = path.lstat()
            if stat.S_ISLNK(info.st_mode) or not (stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode)):
                raise ValueError("fixture contains a link or special file")
            if stat.S_ISREG(info.st_mode):
                total += info.st_size
                if total > 32 * 1024 * 1024:
                    raise ValueError("fixture transfer exceeds 32 MiB")
                data = path.read_bytes()
                if token and token in data:
                    raise ValueError("test credential must not enter the shared fixture")
                result[path.relative_to(root).as_posix()] = hashlib.sha256(data).hexdigest()
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=("export", "import-metadata"))
    parser.add_argument("--context", type=Path, required=True)
    parser.add_argument("--mirror", type=Path, required=True)
    parser.add_argument("--receipt", type=Path, required=True)
    args = parser.parse_args()
    if os.name == "nt":
        parser.error("explicit Linux fixture setup only")
    physical(args.context)
    context = json.loads(args.context.read_text(encoding="utf-8"))
    source = Path(context["fixture_root_linux"])
    physical(source)
    if source != args.context.parent / "project" or not str(source).startswith("/var/tmp/dungeon-scion-runtime-002-"):
        parser.error("actual ext4 checkout must belong to the private test context")
    mirror = args.mirror
    physical(mirror, missing=args.operation == "export")
    if (not str(mirror).startswith("/mnt/c/") or mirror.name != "project"
            or mirror.parent.parent.name != "scion-runtime-002" or mirror.parent.parent.parent.name != "artifacts"):
        parser.error("shared mirror must be artifacts/scion-runtime-002/<attempt>/project")
    physical(args.receipt, missing=True)
    if args.receipt.exists():
        parser.error("refuse to replace a previous transfer receipt")
    token = context["token"].encode()
    if args.operation == "export":
        if mirror.exists():
            parser.error("mirror must be absent")
        before = inventory(source, token)
        shutil.copytree(source, mirror)
        if inventory(mirror, token) != before:
            raise RuntimeError("exported fixture bytes differ from actual CLI checkout")
        files = before
    else:
        original_state = (source / ".scion/state").read_bytes()
        for relative in ("renderer/settings.rs", "renderer/frame.rs", ".scion/state"):
            if (source / relative).read_bytes() != (mirror / relative).read_bytes():
                raise RuntimeError("actual source/base changed before metadata transfer")
        files = inventory(mirror, token)
        selected = (".project", "storage", "docs", "snapshots", "evidence", "renderer/settings.sklt", "renderer/.exp")
        for relative in selected:
            original, destination = mirror / relative, source / relative
            physical(original)
            physical(destination, missing=True)
            if destination.exists():
                raise RuntimeError("metadata import would replace an existing fixture entry")
            if original.is_dir():
                shutil.copytree(original, destination)
            else:
                shutil.copy2(original, destination)
        if (source / ".scion/state").read_bytes() != original_state:
            raise RuntimeError("actual CLI working-copy state changed")
        for relative, digest in files.items():
            if not relative.startswith(".scion/") and hashlib.sha256((source / relative).read_bytes()).hexdigest() != digest:
                raise RuntimeError("metadata transfer byte mismatch")
    receipt = {"schema_version": 1, "status": "PASS", "operation": args.operation,
               "kind": "EXPLICIT_DISPOSABLE_FIXTURE_COPY_NOT_WSL_ADAPTER",
               "actual_scion_revision": context["revisions"][-1], "files": files,
               "scion_state_forged": False, "credentials_transferred": False}
    with args.receipt.open("x", encoding="utf-8") as stream:
        json.dump(receipt, stream, indent=2)
        stream.write("\n")
    print(json.dumps({"status": "PASS", "operation": args.operation, "files": len(files)}), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
