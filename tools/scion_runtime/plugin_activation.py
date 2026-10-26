"""Prepare a reversible, content-verified snapshot and launcher check.

This module never reads Codex global configuration, installs a plugin, launches
the MCP server, or mutates the installed cache. Snapshot creation is an explicit
function call intended for the parent activation step after its runtime gates.
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path, PurePosixPath, PureWindowsPath
import re
import stat
from typing import Any


PLUGIN_NAME = "dungeon-system"
PERSONAL_MARKETPLACE = "personal"
CURRENT_INSTALLED_VERSION = "0.4.1"
RECOMMENDED_EXPERIMENTAL_VERSION = "0.4.2-rc1"
EXPECTED_TOOL_COUNT = 34
REQUIRED_EXPERIMENTAL_TOOLS = (
    "dungeon_vcs_status",
    "dungeon_vcs_inspect",
    "dungeon_vcs_history",
    "dungeon_vcs_diff",
)
SNAPSHOT_SCHEMA = "dungeon-plugin-cache-snapshot.v1"
_VERSION_RE = re.compile(r"\d+\.\d+\.\d+(?:-[0-9A-Za-z][0-9A-Za-z.-]*)?\Z")
_SERVER_VERSION_RE = re.compile(r"^VERSION\s*=\s*['\"]([^'\"]+)['\"]\s*$", re.MULTILINE)


class ActivationPlanError(ValueError):
    """The installed package or proposed snapshot path is unsafe or inconsistent."""


def _is_link(path: Path) -> bool:
    if path.is_symlink():
        return True
    is_junction = getattr(path, "is_junction", None)
    if callable(is_junction) and is_junction():
        return True
    try:
        attributes = path.lstat().st_file_attributes
    except (AttributeError, FileNotFoundError, OSError):
        return False
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return bool(attributes & reparse_flag)


def _absolute_path_before_resolve(path: Path, label: str) -> Path:
    try:
        raw = os.fspath(path)
    except TypeError as exc:
        raise ActivationPlanError(f"{label} must be a filesystem path") from exc
    if not isinstance(raw, str) or not raw or raw != raw.strip() or "\x00" in raw:
        raise ActivationPlanError(f"{label} must be a clean absolute path")
    windows = PureWindowsPath(raw)
    normalized = raw.replace("/", "\\")
    if (raw.startswith(("\\\\", "//")) or windows.drive.startswith("\\\\")
            or normalized.startswith(("\\\\?\\", "\\\\.\\", "\\??\\"))):
        raise ActivationPlanError(f"{label} must not be a UNC or device path")
    if any(part == ".." for part in re.split(r"[\\/]+", raw)):
        raise ActivationPlanError(f"{label} must not contain raw parent traversal")
    candidate = Path(raw)
    if not candidate.is_absolute():
        raise ActivationPlanError(f"{label} must be absolute")
    return candidate


def _check_ancestors_for_links(path: Path, label: str, *, allow_missing: bool) -> None:
    current = Path(path.anchor)
    if not current.anchor:
        raise ActivationPlanError(f"{label} must be absolute")
    if _is_link(current):
        raise ActivationPlanError(f"{label} has a link or reparse-point ancestor")
    for part in path.parts[1:]:
        current = current / part
        if _is_link(current):
            raise ActivationPlanError(f"{label} has a link or reparse-point ancestor")
        try:
            current.lstat()
        except FileNotFoundError as exc:
            if allow_missing:
                continue
            raise ActivationPlanError(f"{label} has a missing path component") from exc
        except OSError as exc:
            raise ActivationPlanError(f"{label} path ancestors cannot be inspected") from exc
        if _is_link(current):
            raise ActivationPlanError(f"{label} has a link or reparse-point ancestor")


def _directory(path: Path, label: str) -> Path:
    candidate = _absolute_path_before_resolve(path, label)
    _check_ancestors_for_links(candidate, label, allow_missing=False)
    if not candidate.is_dir():
        raise ActivationPlanError(f"{label} must be a directory")
    try:
        resolved = candidate.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise ActivationPlanError(f"{label} is unavailable") from exc
    _check_ancestors_for_links(candidate, label, allow_missing=False)
    if not resolved.is_dir():
        raise ActivationPlanError(f"{label} must be a directory")
    return resolved


def _walk_error(error: OSError) -> None:
    raise ActivationPlanError("package tree could not be fully inspected") from error


def _walk_regular_files(root: Path) -> list[Path]:
    files: list[Path] = []
    for current, directories, names in os.walk(root, topdown=True, onerror=_walk_error, followlinks=False):
        current_path = Path(current)
        for name in list(directories):
            path = current_path / name
            if _is_link(path):
                raise ActivationPlanError(f"package contains a link or junction: {path.relative_to(root)}")
        directories.sort()
        for name in sorted(names):
            path = current_path / name
            if _is_link(path):
                raise ActivationPlanError(f"package contains a link: {path.relative_to(root)}")
            try:
                mode = path.lstat().st_mode
            except OSError as exc:
                raise ActivationPlanError("package changed while being inspected") from exc
            if not stat.S_ISREG(mode):
                raise ActivationPlanError(f"package contains a non-regular file: {path.relative_to(root)}")
            files.append(path)
    return sorted(files, key=lambda item: item.relative_to(root).as_posix())


def _file_sha256(path: Path) -> tuple[int, str]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            size += len(chunk)
            digest.update(chunk)
    return size, digest.hexdigest()


def _inventory(root: Path) -> list[dict[str, Any]]:
    inventory = []
    for path in _walk_regular_files(root):
        size, digest = _file_sha256(path)
        inventory.append({"path": path.relative_to(root).as_posix(), "size_bytes": size, "sha256": digest})
    return inventory


def _inventory_digest(files: list[dict[str, Any]]) -> str:
    encoded = json.dumps(files, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _create_ignored_backup_root(repository: Path) -> Path:
    tools_dir = repository / "tools"
    plugin_dir = tools_dir / "codex-plugin"
    backup_root = plugin_dir / "backups"
    _absolute_path_before_resolve(backup_root, "backup destination")
    _check_ancestors_for_links(backup_root, "backup destination", allow_missing=True)
    for directory in (tools_dir, plugin_dir, backup_root):
        _check_ancestors_for_links(directory, "backup destination", allow_missing=True)
        try:
            directory.mkdir()
        except FileExistsError:
            pass
        except OSError as exc:
            raise ActivationPlanError("ignored backup directory could not be created") from exc
        _check_ancestors_for_links(directory, "backup destination", allow_missing=False)
        if not directory.is_dir():
            raise ActivationPlanError("backup destination component is not a directory")
    resolved = _directory(backup_root, "backup destination")
    if not resolved.is_relative_to(repository):
        raise ActivationPlanError("backup destination escaped the repository")
    return resolved


def _json_file(path: Path, label: str) -> dict[str, Any]:
    if _is_link(path):
        raise ActivationPlanError(f"{label} must not be a link")
    try:
        if path.stat().st_size > 1024 * 1024:
            raise ActivationPlanError(f"{label} is unexpectedly large")
        value = json.loads(path.read_text(encoding="utf-8"))
    except ActivationPlanError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ActivationPlanError(f"{label} cannot be read as JSON") from exc
    if not isinstance(value, dict):
        raise ActivationPlanError(f"{label} must contain a JSON object")
    return value


def _package_identity(root: Path, marketplace: str, plugin_name: str) -> str:
    parts = [part.casefold() for part in root.parts]
    if (len(parts) < 5 or parts[-1] == "" or parts[-2] != plugin_name.casefold()
            or parts[-3] != marketplace.casefold() or parts[-4] != "cache" or parts[-5] != "plugins"):
        raise ActivationPlanError("cache path must be plugins/cache/<marketplace>/<plugin>/<version>")
    version = root.name
    if not _VERSION_RE.fullmatch(version):
        raise ActivationPlanError("cache directory must use a semantic version")
    return version


def inspect_installed_launcher(
    cache_root: Path,
    *,
    marketplace: str = PERSONAL_MARKETPLACE,
    plugin_name: str = PLUGIN_NAME,
) -> dict[str, Any]:
    """Statically validate the personal-cache launcher without starting it."""
    root = _directory(cache_root, "installed plugin cache")
    version = _package_identity(root, marketplace, plugin_name)
    files = _walk_regular_files(root)
    if not files:
        raise ActivationPlanError("installed plugin cache is empty")

    manifest_path = root / ".codex-plugin" / "plugin.json"
    manifest = _json_file(manifest_path, "plugin manifest")
    if manifest.get("name") != plugin_name or manifest.get("version") != version:
        raise ActivationPlanError("cache path and plugin manifest identity/version differ")

    server_path = root / "mcp" / "server.py"
    if _is_link(server_path):
        raise ActivationPlanError("MCP server entrypoint must not be a link")
    try:
        server_text = server_path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise ActivationPlanError("MCP server entrypoint is unavailable") from exc
    match = _SERVER_VERSION_RE.search(server_text)
    if not match or match.group(1) != version:
        raise ActivationPlanError("MCP server version and plugin manifest version differ")

    config = _json_file(root / ".mcp.json", "plugin MCP configuration")
    servers = config.get("mcpServers")
    server = servers.get(plugin_name) if isinstance(servers, dict) else None
    if not isinstance(server, dict):
        raise ActivationPlanError("plugin MCP server entry is missing")
    command = server.get("command")
    arguments = server.get("args")
    if (not isinstance(command, str) or command.casefold() != "cmd.exe"
            or not isinstance(arguments, list) or len(arguments) != 3
            or not all(isinstance(item, str) for item in arguments)
            or arguments[0].casefold() != "/d" or arguments[1].casefold() != "/c"
            or arguments[2].replace("/", "\\").casefold() != "scripts\\launch_mcp.cmd"
            or server.get("cwd") != "."):
        raise ActivationPlanError("MCP configuration does not point to the supported Windows launcher")
    env = server.get("env", {})
    if not isinstance(env, dict) or env.get("DUNGEON_WRITABLE_ROOTS") != "[]":
        raise ActivationPlanError("MCP package must retain the explicit read-only writable-roots setting")
    allowlist = server.get("env_vars")
    if allowlist is not None:
        if (not isinstance(allowlist, list) or not all(isinstance(item, str) for item in allowlist)
                or not {"DUNGEON_PROJECT_ROOT", "DUNGEON_PYTHON"}.issubset(allowlist)):
            raise ActivationPlanError("MCP environment allowlist is missing required launcher variables")
        environment_names = list(allowlist)
        forwarding_mode = "allowlist"
    else:
        if not isinstance(env.get("DUNGEON_PROJECT_ROOT"), str) or not env["DUNGEON_PROJECT_ROOT"]:
            raise ActivationPlanError("MCP package has neither the project-root allowlist nor a configured root")
        environment_names = sorted(env)
        forwarding_mode = "fixed_env"

    launcher = root / "scripts" / "launch_mcp.cmd"
    if _is_link(launcher):
        raise ActivationPlanError("installed launcher must not be a link")
    try:
        launcher_text = launcher.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise ActivationPlanError("installed launcher is unavailable") from exc
    normalized = launcher_text.replace("/", "\\").casefold()
    static_checks = {
        "manifest_and_server_versions_match": "PASS",
        "mcp_config_targets_windows_launcher": "PASS",
        "launcher_targets_mcp_server": "PASS" if "dungeon_server=%~dp0..\\mcp\\server.py" in normalized else "FAIL",
        "python_3_11_or_newer_guard": "PASS" if "sys.version_info < (3, 11)" in normalized else "FAIL",
        "dungeon_python_override": "PASS" if "dungeon_python" in normalized else "FAIL",
        "read_only_writable_roots": "PASS",
    }

    return {
        "plugin": plugin_name,
        "marketplace": marketplace,
        "installed_version": version,
        "cache_root": str(root),
        "launcher_path": str(launcher),
        "command": [command, *arguments],
        "working_directory": str(root),
        "environment_forwarding_mode": forwarding_mode,
        "environment_variable_names": environment_names,
        "static_checks": static_checks,
        "static_status": "PASS" if all(value == "PASS" for value in static_checks.values()) else "PARTIAL",
    }


def prepare_launcher_verification(
    cache_root: Path,
    *,
    marketplace: str = PERSONAL_MARKETPLACE,
    plugin_name: str = PLUGIN_NAME,
    target_version: str = RECOMMENDED_EXPERIMENTAL_VERSION,
) -> dict[str, Any]:
    """Return a static launcher report and the required post-install live gate."""
    if not _VERSION_RE.fullmatch(target_version):
        raise ActivationPlanError("target version must use a semantic version")
    launcher = inspect_installed_launcher(cache_root, marketplace=marketplace, plugin_name=plugin_name)
    launcher["target_version"] = target_version
    launcher["live_session_gate"] = {
        "status": "NOT_RUN",
        "required_server_version": target_version,
        "expected_tool_count": EXPECTED_TOOL_COUNT,
        "required_experimental_tools": list(REQUIRED_EXPERIMENTAL_TOOLS),
        "protocol_sequence": [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
                "protocolVersion": "2025-11-25", "capabilities": {},
                "clientInfo": {"name": "codex-fresh-session-gate", "version": "1"},
            }},
            {"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list",
             "params": {"_meta": {"progressToken": 1}}},
        ],
        "requires_fresh_codex_conversation": True,
        "must_not_create_a_user_owned_task": True,
        "live_client_validation_performed": False,
    }
    return launcher


def snapshot_installed_cache(
    repository_root: Path,
    cache_root: Path,
    *,
    marketplace: str = PERSONAL_MARKETPLACE,
    plugin_name: str = PLUGIN_NAME,
    created_at: datetime | None = None,
) -> Path:
    """Copy one personal plugin-cache package into an ignored, new backup folder.

    The function is intentionally not called by this module at import time. It
    copies only the explicitly supplied package directory, never Codex config.
    It refuses overwrites and leaves an incomplete snapshot in place for review
    if an I/O or consistency error occurs; it does not delete or repair anything.
    """
    package = inspect_installed_launcher(cache_root, marketplace=marketplace, plugin_name=plugin_name)
    source = _directory(cache_root, "installed plugin cache")
    repository = _directory(repository_root, "repository root")
    ignore_file = repository / ".gitignore"
    try:
        ignore_lines = ignore_file.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError) as exc:
        raise ActivationPlanError("repository ignore rules cannot be verified") from exc
    if "/tools/codex-plugin/backups/" not in {line.strip() for line in ignore_lines}:
        raise ActivationPlanError("the repository does not explicitly ignore the plugin backup directory")

    backup_root_resolved = _create_ignored_backup_root(repository)

    moment = created_at or datetime.now(timezone.utc)
    if moment.tzinfo is None:
        raise ActivationPlanError("snapshot timestamp must include a timezone")
    stamp = moment.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    base_name = f"{plugin_name}-before-activation-{stamp}"
    snapshot = None
    for suffix in range(1000):
        candidate = backup_root_resolved / (base_name if suffix == 0 else f"{base_name}-{suffix:02d}")
        candidate = _absolute_path_before_resolve(candidate, "backup snapshot path")
        _check_ancestors_for_links(candidate, "backup snapshot path", allow_missing=True)
        try:
            candidate.mkdir()
            snapshot = _directory(candidate, "backup snapshot path")
            if not snapshot.is_relative_to(backup_root_resolved):
                raise ActivationPlanError("backup snapshot escaped the ignored backup directory")
            break
        except FileExistsError:
            continue
    if snapshot is None:
        raise ActivationPlanError("could not allocate a unique backup directory")

    copied_root = snapshot / "installed-cache"
    copied_root.mkdir()
    source_files = _inventory(source)
    for item in source_files:
        relative = PurePosixPath(item["path"])
        source_file = source.joinpath(*relative.parts)
        destination = copied_root.joinpath(*relative.parts)
        destination.parent.mkdir(parents=True, exist_ok=True)
        if _is_link(source_file):
            raise ActivationPlanError(f"source changed to a link during copy: {item['path']}")
        digest = hashlib.sha256()
        size = 0
        with source_file.open("rb") as read_stream, destination.open("xb") as write_stream:
            while chunk := read_stream.read(1024 * 1024):
                size += len(chunk)
                digest.update(chunk)
                write_stream.write(chunk)
        if size != item["size_bytes"] or digest.hexdigest() != item["sha256"]:
            raise ActivationPlanError(f"source changed while being copied: {item['path']}")

    copied_files = _inventory(copied_root)
    source_after = _inventory(source)
    if copied_files != source_files or source_after != source_files:
        raise ActivationPlanError("source and backup inventories differ; snapshot is incomplete")
    final_package = inspect_installed_launcher(cache_root, marketplace=marketplace, plugin_name=plugin_name)
    if final_package["installed_version"] != package["installed_version"]:
        raise ActivationPlanError("installed package identity changed during snapshot")

    manifest = {
        "schema": SNAPSHOT_SCHEMA,
        "created_utc": moment.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
        "identity": {"marketplace": marketplace, "plugin": plugin_name,
                     "installed_version": package["installed_version"]},
        "source_cache_path": str(source),
        "backup_package_path": "installed-cache",
        "file_count": len(source_files),
        "total_bytes": sum(item["size_bytes"] for item in source_files),
        "files": source_files,
        "inventory_sha256": _inventory_digest(source_files),
    }
    manifest_path = snapshot / "inventory.json"
    with manifest_path.open("x", encoding="utf-8", newline="\n") as stream:
        json.dump(manifest, stream, indent=2, sort_keys=True)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    return manifest_path


def verify_snapshot(snapshot_root: Path) -> dict[str, Any]:
    """Verify backup bytes against the snapshot's recorded inventory."""
    snapshot = _directory(snapshot_root, "snapshot directory")
    manifest = _json_file(snapshot / "inventory.json", "snapshot inventory")
    files = manifest.get("files")
    if (manifest.get("schema") != SNAPSHOT_SCHEMA or not isinstance(files, list)
            or manifest.get("inventory_sha256") != _inventory_digest(files)):
        raise ActivationPlanError("snapshot manifest schema or inventory digest is invalid")
    relative = manifest.get("backup_package_path")
    if relative != "installed-cache":
        raise ActivationPlanError("snapshot package path is invalid")
    package = snapshot / relative
    actual = _inventory(_directory(package, "snapshot package"))
    if actual != files:
        raise ActivationPlanError("snapshot package bytes do not match the recorded inventory")
    return {
        "valid": True,
        "identity": manifest.get("identity"),
        "file_count": len(actual),
        "total_bytes": sum(item["size_bytes"] for item in actual),
        "inventory_sha256": manifest["inventory_sha256"],
    }
