"""Bounded native-Linux stdio check of the installed Dungeon MCP server.

This is a direct MCP transport check, not a Codex session or a WSL bridge.
It launches only the owned disposable TestCluster and the installed server,
uses read-only MCP tools, and preserves all run-05 receipts.
"""
from __future__ import annotations

import hashlib
import argparse
import json
import os
from pathlib import Path
import re
import selectors
import shutil
import socket
import stat
import subprocess
import sys
import time
import tomllib
from urllib.parse import urlsplit


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
ARTIFACT_ROOT = REPOSITORY_ROOT / "artifacts/scion-runtime-002"
ATTEMPT = "run-05"
PRIVATE_PARENT: Path
RUN_ROOT = Path()
PROJECT_ROOT = Path()
CONTEXT_PATH = Path()
STOP_PATH = ARTIFACT_ROOT / ATTEMPT / "stop"
READY_PATH = STOP_PATH.with_name(STOP_PATH.name + ".ready.json")
MIRROR_ROOT = ARTIFACT_ROOT / "run-04/project"
OUTPUT_ROOT = Path()
HELPER_RECEIPT: Path
SERVER_PATH: Path
UPSTREAM_PIN = "7d445a0744e30f4dcb36a794b0ce42648157e1b9"
EXPECTED_SCION_SHA256 = "e9233886e8fa970af79fcf6e8dcbd44f45b0f33b25da67953f6a87698bf52d7f"
TOOLS_EXPECTED = 34
SCION_ID = re.compile(r"c1[0-9a-f]{64}\Z")
VCS_TOOLS = {
    "dungeon_vcs_status", "dungeon_vcs_inspect", "dungeon_vcs_history", "dungeon_vcs_diff",
}
READ_TOOLS = VCS_TOOLS | {"dungeon_project_probe", "dungeon_status", "dungeon_orientation"}


class GateError(RuntimeError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def _physical(path: Path, *, allow_missing_leaf: bool = False) -> None:
    if not path.is_absolute() or ".." in path.parts:
        raise GateError("PATH_NOT_ABSOLUTE_PHYSICAL")
    missing = []
    for item in (*reversed(path.parents), path):
        try:
            info = item.lstat()
        except FileNotFoundError:
            missing.append(item)
            continue
        except OSError as exc:
            raise GateError("PATH_INSPECTION_FAILED") from exc
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise GateError("REPARSE_PATH_REJECTED")
    if missing and not (allow_missing_leaf and missing == [path]):
        raise GateError("PATH_COMPONENT_MISSING")


def _under(path: Path, root: Path) -> bool:
    try:
        path.resolve(strict=True).relative_to(root.resolve(strict=True))
        return True
    except (OSError, ValueError):
        return False


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _inventory(root: Path) -> dict[str, dict]:
    result: dict[str, dict] = {}
    pending = [root]
    while pending:
        directory = pending.pop()
        for entry in sorted(os.scandir(directory), key=lambda item: item.name):
            path = Path(entry.path)
            info = path.lstat()
            relative = path.relative_to(root).as_posix()
            row = {"mode": stat.S_IMODE(info.st_mode)}
            if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
                row.update(kind="link", target=os.readlink(path))
            elif stat.S_ISDIR(info.st_mode):
                row["kind"] = "directory"
                pending.append(path)
            elif stat.S_ISREG(info.st_mode):
                row.update(kind="file", size=info.st_size, sha256=_sha256(path))
            else:
                row.update(kind="special", size=info.st_size)
            result[relative] = row
    return dict(sorted(result.items()))


def _write_json_new(path: Path, value: object, secret: str = "") -> None:
    data = (json.dumps(value, indent=2, ensure_ascii=True) + "\n").encode("utf-8")
    if secret and secret.encode("utf-8") in data:
        raise GateError("CREDENTIAL_IN_RECEIPT")
    with path.open("xb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())


def _redact(value: object, secret: str) -> object:
    if isinstance(value, str):
        return value.replace(secret, "[REDACTED]") if secret else value
    if isinstance(value, list):
        return [_redact(item, secret) for item in value]
    if isinstance(value, dict):
        return {key: _redact(item, secret) for key, item in value.items()}
    return value


class StdioMcp:
    def __init__(self, process: subprocess.Popen):
        self.process = process
        self.next_id = 1
        self.buffer = bytearray()
        self.requests: list[dict] = []
        self.responses: list[dict] = []

    def _send(self, request: dict) -> None:
        if self.process.stdin is None:
            raise GateError("MCP_STDIN_CLOSED")
        wire = (json.dumps(request, separators=(",", ":"), ensure_ascii=True) + "\n").encode("utf-8")
        if len(wire) > 1_048_576:
            raise GateError("MCP_REQUEST_TOO_LARGE")
        try:
            self.process.stdin.write(wire)
            self.process.stdin.flush()
        except (BrokenPipeError, OSError) as exc:
            raise GateError("MCP_SERVER_PIPE_CLOSED") from exc

    def _read_reply(self, timeout: float) -> dict:
        if self.process.stdout is None:
            raise GateError("MCP_STDOUT_CLOSED")
        deadline = time.monotonic() + timeout
        descriptor = self.process.stdout.fileno()
        with selectors.DefaultSelector() as selector:
            selector.register(descriptor, selectors.EVENT_READ)
            while True:
                newline = self.buffer.find(b"\n")
                if newline >= 0:
                    line = bytes(self.buffer[:newline]).strip()
                    del self.buffer[:newline + 1]
                    if not line:
                        continue
                    try:
                        response = json.loads(line)
                    except (UnicodeError, json.JSONDecodeError) as exc:
                        raise GateError("MCP_RESPONSE_INVALID_JSON") from exc
                    if not isinstance(response, dict):
                        raise GateError("MCP_RESPONSE_NOT_OBJECT")
                    return response
                if len(self.buffer) > 4 * 1024 * 1024:
                    raise GateError("MCP_RESPONSE_TOO_LARGE")
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise GateError("MCP_RESPONSE_TIMEOUT")
                if self.process.poll() is not None:
                    raise GateError("MCP_SERVER_EXITED_EARLY")
                if not selector.select(remaining):
                    raise GateError("MCP_RESPONSE_TIMEOUT")
                try:
                    chunk = os.read(descriptor, 65536)
                except OSError as exc:
                    raise GateError("MCP_STDOUT_READ_FAILED") from exc
                if not chunk:
                    raise GateError("MCP_SERVER_EOF")
                self.buffer.extend(chunk)

    def notify(self, method: str, params: dict | None = None) -> None:
        request = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            request["params"] = params
        self.requests.append(request)
        self._send(request)

    def request(self, method: str, params: dict | None = None, timeout: float = 30) -> dict:
        request_id = self.next_id
        self.next_id += 1
        request = {"jsonrpc": "2.0", "id": request_id, "method": method}
        if params is not None:
            request["params"] = params
        self.requests.append(request)
        self._send(request)
        while True:
            response = self._read_reply(timeout)
            if response.get("id") == request_id:
                self.responses.append(response)
                return response

    def call_tool(self, name: str, arguments: dict) -> tuple[dict, object]:
        if name not in READ_TOOLS:
            raise GateError("NON_READONLY_TOOL_REFUSED")
        response = self.request("tools/call", {"name": name, "arguments": arguments})
        result = response.get("result")
        if not isinstance(result, dict):
            return response, None
        payload = result.get("structuredContent")
        if isinstance(payload, dict):
            return response, payload
        for item in result.get("content", []):
            if isinstance(item, dict) and item.get("type") == "text":
                try:
                    parsed = json.loads(item.get("text", ""))
                except (TypeError, json.JSONDecodeError):
                    continue
                if isinstance(parsed, dict):
                    return response, parsed
        return response, None


def _scope(payload: object) -> dict:
    if not isinstance(payload, dict):
        return {}
    scopes = payload.get("scopes")
    if isinstance(scopes, dict) and isinstance(scopes.get("main"), dict):
        return scopes["main"]
    return payload


def _source_record(scope: dict) -> dict:
    record = scope.get("source_revision")
    return record if isinstance(record, dict) else {}


def _tool_payload_status(payload: object) -> str | None:
    return payload.get("status") if isinstance(payload, dict) and isinstance(payload.get("status"), str) else None


def _private_context() -> dict:
    _physical(CONTEXT_PATH)
    info = CONTEXT_PATH.stat()
    if info.st_size > 64 * 1024 or stat.S_IMODE(info.st_mode) & 0o077:
        raise GateError("PRIVATE_CONTEXT_PERMISSIONS_INVALID")
    try:
        context = json.loads(CONTEXT_PATH.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise GateError("PRIVATE_CONTEXT_INVALID") from exc
    if not isinstance(context, dict):
        raise GateError("PRIVATE_CONTEXT_INVALID")
    if (context.get("schema_version") != 1 or context.get("upstream_commit") != UPSTREAM_PIN
            or context.get("fixture_root_linux") != str(PROJECT_ROOT)):
        raise GateError("PRIVATE_CONTEXT_SCOPE_MISMATCH")
    revisions = context.get("revisions")
    if (not isinstance(revisions, list) or len(revisions) != 3
            or any(not isinstance(value, str) or not SCION_ID.fullmatch(value) for value in revisions)
            or len(set(revisions)) != 3):
        raise GateError("PRIVATE_CONTEXT_REVISIONS_INVALID")
    if not isinstance(context.get("token"), str) or not context["token"].strip():
        raise GateError("PRIVATE_CONTEXT_CREDENTIAL_MISSING")
    endpoint = urlsplit(context.get("endpoint", ""))
    if (endpoint.scheme != "https" or endpoint.hostname not in {"127.0.0.1", "localhost", "::1"}
            or endpoint.port is None or endpoint.username or endpoint.password or endpoint.query or endpoint.fragment):
        raise GateError("TESTCLUSTER_ENDPOINT_INVALID")
    ca_file = Path(context.get("ca_file", ""))
    if not ca_file.is_absolute() or not _under(ca_file, RUN_ROOT) or ca_file.is_symlink() or not ca_file.is_file():
        raise GateError("TESTCLUSTER_CA_INVALID")
    return context


def _binary_contract() -> tuple[Path, str, Path, str]:
    _physical(HELPER_RECEIPT)
    try:
        receipt = json.loads(HELPER_RECEIPT.read_text(encoding="utf-8"))
        helper = Path(receipt["binary"]["path"])
        helper_hash = receipt["binary"]["sha256"]
        scion = Path(receipt["scion_binary_after"]["path"])
        scion_hash = receipt["scion_binary_after"]["sha256"]
    except (OSError, UnicodeError, json.JSONDecodeError, KeyError, TypeError) as exc:
        raise GateError("EXISTING_HELPER_RECEIPT_INVALID") from exc
    if receipt.get("status") != "HELPER_BUILT_RUNTIME_NOT_RUN" or receipt.get("upstream_commit") != UPSTREAM_PIN:
        raise GateError("EXISTING_HELPER_RECEIPT_NOT_READY")
    if (not helper.is_absolute() or not _under(helper, PRIVATE_PARENT)
            or not scion.is_absolute() or not _under(scion, PRIVATE_PARENT)):
        raise GateError("EXISTING_BINARY_OUTSIDE_PRIVATE_ROOT")
    _physical(helper)
    _physical(scion)
    if (not helper.is_file() or not scion.is_file() or not os.access(helper, os.X_OK)
            or not os.access(scion, os.X_OK) or not re.fullmatch(r"[0-9a-f]{64}", helper_hash)
            or scion_hash != EXPECTED_SCION_SHA256 or _sha256(helper) != helper_hash
            or _sha256(scion) != EXPECTED_SCION_SHA256):
        raise GateError("EXISTING_BINARY_HASH_MISMATCH")
    if helper.read_bytes()[:4] != b"\x7fELF" or scion.read_bytes()[:4] != b"\x7fELF":
        raise GateError("EXISTING_BINARY_NOT_LINUX_ELF")
    _physical(SERVER_PATH)
    if not SERVER_PATH.is_file():
        raise GateError("INSTALLED_MCP_SERVER_MISSING")
    return helper, helper_hash, scion, EXPECTED_SCION_SHA256


def _configure(bootstrap: Path, installed_server: Path) -> None:
    global PRIVATE_PARENT, RUN_ROOT, PROJECT_ROOT, CONTEXT_PATH, OUTPUT_ROOT
    global HELPER_RECEIPT, SERVER_PATH
    HELPER_RECEIPT = bootstrap
    SERVER_PATH = installed_server
    _physical(HELPER_RECEIPT)
    try:
        receipt = json.loads(HELPER_RECEIPT.read_text(encoding="utf-8"))
        helper = Path(receipt["binary"]["path"])
    except (OSError, UnicodeError, json.JSONDecodeError, KeyError, TypeError) as exc:
        raise GateError("EXISTING_HELPER_RECEIPT_INVALID") from exc
    if not helper.is_absolute() or len(helper.parents) < 3:
        raise GateError("EXISTING_BINARY_PATH_INVALID")
    PRIVATE_PARENT = helper.parents[2]
    RUN_ROOT = PRIVATE_PARENT / ATTEMPT
    PROJECT_ROOT = RUN_ROOT / "project"
    CONTEXT_PATH = RUN_ROOT / "context.json"
    OUTPUT_ROOT = RUN_ROOT / "linux-mcp-gate-01"
    if not SERVER_PATH.is_absolute():
        raise GateError("INSTALLED_MCP_SERVER_PATH_INVALID")


def _start_helper(helper: Path, scion: Path) -> subprocess.Popen:
    if RUN_ROOT.exists() or PROJECT_ROOT.exists() or CONTEXT_PATH.exists():
        raise GateError("RUN05_PRIVATE_PATH_ALREADY_EXISTS")
    if STOP_PATH.parent.exists() or STOP_PATH.exists() or READY_PATH.exists():
        raise GateError("RUN05_STOP_PATH_ALREADY_EXISTS")
    _physical(PRIVATE_PARENT)
    _physical(REPOSITORY_ROOT / "artifacts/scion-runtime-002")
    _physical(MIRROR_ROOT)
    RUN_ROOT.mkdir(mode=0o700)
    os.chmod(RUN_ROOT, 0o700)
    STOP_PATH.parent.mkdir(mode=0o700)
    OUTPUT_ROOT.mkdir(mode=0o700)
    helper_env = {
        "PATH": "/usr/bin:/bin",
        "HOME": str(RUN_ROOT),
        "TMPDIR": str(RUN_ROOT),
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
    }
    command = [str(helper), "--fixture-root", str(PROJECT_ROOT), "--context", str(CONTEXT_PATH),
               "--stop-file", str(STOP_PATH), "--binary", str(scion)]
    try:
        return subprocess.Popen(command, cwd=RUN_ROOT, env=helper_env, stdin=subprocess.DEVNULL,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, close_fds=True)
    except OSError as exc:
        raise GateError("TESTCLUSTER_HELPER_START_FAILED") from exc


def _wait_ready(process: subprocess.Popen, timeout: float = 90) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise GateError("TESTCLUSTER_HELPER_EXITED_BEFORE_READY")
        if READY_PATH.exists():
            _physical(READY_PATH)
            try:
                ready = json.loads(READY_PATH.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, json.JSONDecodeError) as exc:
                raise GateError("TESTCLUSTER_READY_INVALID") from exc
            if (ready.get("status") != "READY" or ready.get("context_path") != str(CONTEXT_PATH)
                    or ready.get("fixture_root_linux") != str(PROJECT_ROOT)):
                raise GateError("TESTCLUSTER_READY_SCOPE_MISMATCH")
            return ready
        time.sleep(0.2)
    raise GateError("TESTCLUSTER_READY_TIMEOUT")


def _run_transfer(secret: str, receipt_path: Path) -> dict:
    script = REPOSITORY_ROOT / "tools/scion_runtime/fixture_transfer.py"
    env = {"PATH": "/usr/bin:/bin", "HOME": str(RUN_ROOT), "TMPDIR": str(RUN_ROOT),
           "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8", "PYTHONDONTWRITEBYTECODE": "1"}
    command = [sys.executable, "-B", str(script), "import-metadata", "--context", str(CONTEXT_PATH),
               "--mirror", str(MIRROR_ROOT), "--receipt", str(receipt_path)]
    try:
        completed = subprocess.run(command, cwd=REPOSITORY_ROOT, env=env, stdin=subprocess.DEVNULL,
                                   stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                   timeout=60, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        raise GateError("METADATA_TRANSFER_FAILED") from exc
    if completed.returncode != 0:
        raise GateError("METADATA_TRANSFER_REJECTED")
    try:
        transfer = json.loads(receipt_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise GateError("METADATA_TRANSFER_RECEIPT_INVALID") from exc
    if (transfer.get("status") != "PASS" or transfer.get("operation") != "import-metadata"
            or transfer.get("scion_state_forged") is not False
            or transfer.get("credentials_transferred") is not False):
        raise GateError("METADATA_TRANSFER_ASSERTION_FAILED")
    if secret.encode("utf-8") in receipt_path.read_bytes():
        raise GateError("CREDENTIAL_IN_RECEIPT")
    return transfer


def _mcp_environment(context: dict, scion: Path) -> dict[str, str]:
    return {
        "PATH": f"{scion.parent}:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
        "HOME": str(RUN_ROOT),
        "TMPDIR": str(OUTPUT_ROOT),
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "PYTHONNOUSERSITE": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
        "DUNGEON_PROJECT_ROOT": str(REPOSITORY_ROOT),
        "DUNGEON_WRITABLE_ROOTS": "[]",
        "DUNGEON_SCION_RUNTIME_ENDPOINT": context["endpoint"],
        "DUNGEON_SCION_RUNTIME_TOKEN": context["token"],
        "DUNGEON_SCION_RUNTIME_CA_FILE": str(context["ca_file"]),
        "DUNGEON_SCION_RUNTIME_TRANSPORT": "native",
    }


def _start_server(context: dict, scion: Path) -> subprocess.Popen:
    try:
        return subprocess.Popen([sys.executable, "-B", str(SERVER_PATH)], cwd=REPOSITORY_ROOT,
                                env=_mcp_environment(context, scion), stdin=subprocess.PIPE,
                                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                bufsize=0, close_fds=True)
    except OSError as exc:
        raise GateError("INSTALLED_MCP_SERVER_START_FAILED") from exc


def _stop_server(process: subprocess.Popen | None) -> int | None:
    if process is None:
        return None
    try:
        if process.stdin is not None and not process.stdin.closed:
            process.stdin.close()
        try:
            return process.wait(timeout=8)
        except subprocess.TimeoutExpired:
            process.terminate()
            try:
                return process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                return process.wait(timeout=5)
    except OSError:
        if process.poll() is None:
            process.kill()
        return process.wait(timeout=5)


def _stop_helper(process: subprocess.Popen | None) -> int | None:
    if process is None:
        return None
    if not STOP_PATH.exists():
        with STOP_PATH.open("xb") as stream:
            stream.flush()
            os.fsync(stream.fileno())
    try:
        return process.wait(timeout=30)
    except subprocess.TimeoutExpired:
        process.terminate()
        try:
            return process.wait(timeout=8)
        except subprocess.TimeoutExpired:
            process.kill()
            return process.wait(timeout=5)


def _listener_closed(context: dict) -> bool:
    endpoint = urlsplit(context["endpoint"])
    host, port = endpoint.hostname, endpoint.port
    for _ in range(8):
        try:
            connection = socket.create_connection((host, port), timeout=0.35)
        except OSError:
            return True
        else:
            connection.close()
            time.sleep(0.25)
    return False


def execute() -> tuple[dict, list[dict], list[dict], int]:
    if os.name != "posix" or not Path("/proc/sys/kernel/ostype").read_text().startswith("Linux"):
        raise GateError("NATIVE_LINUX_REQUIRED")
    if str(REPOSITORY_ROOT).startswith("/mnt/c/") is False or not REPOSITORY_ROOT.is_dir():
        raise GateError("DUNGEON_SOURCE_LINUX_MOUNT_REQUIRED")
    helper, helper_hash, scion, scion_hash = _binary_contract()

    helper_process = None
    server_process = None
    client = None
    context = None
    transfer = None
    before = None
    after = None
    assertions: dict[str, bool] = {}
    vcs_summary: dict[str, dict] = {}
    server_exit = helper_exit = None
    listener_closed = None
    status = "BLOCKED"
    error_code = None
    secret = ""
    project_hashes = {}

    try:
        helper_process = _start_helper(helper, scion)
        ready = _wait_ready(helper_process)
        context = _private_context()
        secret = context["token"]
        if ready.get("revisions") != context["revisions"]:
            raise GateError("READY_CONTEXT_REVISION_MISMATCH")
        state_bytes = (PROJECT_ROOT / ".scion/state").read_bytes()
        state = tomllib.loads(state_bytes.decode("utf-8"))
        if state.get("base") != context["revisions"][-1]:
            raise GateError("SCION_STATE_BASE_MISMATCH")
        for relative in ("renderer/settings.rs", "renderer/frame.rs", ".scion/state"):
            source = MIRROR_ROOT / relative
            checkout = PROJECT_ROOT / relative
            if not source.is_file() or not checkout.is_file() or source.read_bytes() != checkout.read_bytes():
                raise GateError("ACTUAL_SOURCE_OR_STATE_MISMATCH")
            project_hashes[relative] = _sha256(checkout)

        transfer_path = OUTPUT_ROOT / "metadata-import.json"
        transfer = _run_transfer(secret, transfer_path)
        before = _inventory(PROJECT_ROOT)
        server_hash = _sha256(SERVER_PATH)
        server_process = _start_server(context, scion)
        client = StdioMcp(server_process)

        init = client.request("initialize", {
            "protocolVersion": "2025-11-25", "capabilities": {},
            "clientInfo": {"name": "linux-scion-mcp-gate", "version": "1"},
        })
        if init.get("error") or init.get("result", {}).get("serverInfo", {}).get("name") != "dungeon-system":
            raise GateError("MCP_INITIALIZE_FAILED")
        client.notify("notifications/initialized")
        listed = client.request("tools/list", {})
        tools = listed.get("result", {}).get("tools", [])
        names = [item.get("name") for item in tools if isinstance(item, dict)]
        if (listed.get("error") or len(tools) != TOOLS_EXPECTED or len(names) != TOOLS_EXPECTED
                or not VCS_TOOLS.issubset(names) or not {"dungeon_project_probe", "dungeon_status", "dungeon_orientation"}.issubset(names)):
            raise GateError("INSTALLED_MCP_TOOL_INVENTORY_MISMATCH")

        root_arg = str(PROJECT_ROOT)
        call_payloads: dict[str, object] = {}

        def call(name: str, args: dict) -> object:
            response, payload = client.call_tool(name, args)
            if response.get("error"):
                raise GateError("MCP_TOOL_RPC_ERROR")
            call_payloads[name] = payload
            return payload

        probe = call("dungeon_project_probe", {"project_root": root_arg})
        probe_mode = (probe.get("mode", probe.get("project_mode")) if isinstance(probe, dict) else None)
        assertions["explicit_root_managed_probe"] = probe_mode in {"MANAGED", "DUNGEON"}
        if not assertions["explicit_root_managed_probe"]:
            raise GateError("MCP_PROJECT_PROBE_NOT_MANAGED")
        call("dungeon_status", {"project_root": root_arg})
        call("dungeon_orientation", {"project_root": root_arg})

        base = context["revisions"][-1]
        vcs_requests = {
            "dungeon_vcs_status": {"project_root": root_arg, "scope_id": "main"},
            "dungeon_vcs_inspect": {"project_root": root_arg, "scope_id": "main", "revision": base},
            "dungeon_vcs_history": {"project_root": root_arg, "scope_id": "main", "revision": base, "limit": 3},
            "dungeon_vcs_diff": {"project_root": root_arg, "scope_id": "main"},
        }
        for name, arguments in vcs_requests.items():
            payload = call(name, arguments)
            scope = _scope(payload)
            source = _source_record(scope)
            vcs_summary[name] = {
                "tool_payload_status": _tool_payload_status(payload),
                "availability": scope.get("availability", source.get("availability")),
                "backend": source.get("backend"),
                "revision": source.get("revision"),
                "dirty_state": source.get("dirty_state"),
            }

        for name in ("dungeon_vcs_inspect", "dungeon_vcs_history"):
            row = vcs_summary[name]
            assertions[name + "_actual_available"] = row["availability"] == "AVAILABLE"
            assertions[name + "_actual_revision"] = row["backend"] == "SCION" and row["revision"] == base
            assertions[name + "_dirty_unknown"] = row["dirty_state"] == "UNKNOWN"
        diff_payload = call_payloads["dungeon_vcs_diff"]
        assertions["diff_fail_closed_unsupported"] = "UNSUPPORTED_OPERATION" in json.dumps(diff_payload)
        assertions["four_explicit_main_vcs_queries"] = all(
            vcs_summary[name]["backend"] in {"SCION", None} for name in VCS_TOOLS
        )

        after = _inventory(PROJECT_ROOT)
        assertions["mcp_read_only_inventory_unchanged"] = before == after
        assertions["source_and_state_preserved"] = all(
            _sha256(MIRROR_ROOT / relative) == digest
            and _sha256(PROJECT_ROOT / relative) == digest
            for relative, digest in project_hashes.items()
        )
        if all(assertions.values()):
            status = "PASS"
        else:
            status = "NOT_ACCEPTED"
    except GateError as exc:
        error_code = exc.code
    except Exception as exc:
        error_code = "UNEXPECTED_" + type(exc).__name__.upper()
    finally:
        server_exit = _stop_server(server_process)
        helper_exit = _stop_helper(helper_process)
        if context is not None:
            listener_closed = _listener_closed(context)
        if server_process is not None and server_exit == 0:
            assertions["installed_mcp_server_exited_cleanly"] = True
        elif server_process is not None:
            assertions["installed_mcp_server_exited_cleanly"] = False
        if helper_process is not None and helper_exit == 0:
            assertions["owned_testcluster_exited_cleanly"] = True
        elif helper_process is not None:
            assertions["owned_testcluster_exited_cleanly"] = False
        if context is not None:
            assertions["testcluster_listener_closed"] = listener_closed is True
        if status == "PASS" and not all(assertions.values()):
            status = "NOT_ACCEPTED"

    if OUTPUT_ROOT.exists():
        requests = _redact(client.requests if client else [], secret)
        responses = _redact(client.responses if client else [], secret)
        safe_assertions = dict(assertions)
        summary = {
            "schema_version": 1,
            "task": "DUNGEON-SCION-RUNTIME-002",
            "status": status,
            "transport": "installed_mcp_stdio_native_linux",
            "codex_session_claimed": False,
            "installed_server": {"path": str(SERVER_PATH), "sha256": _sha256(SERVER_PATH) if SERVER_PATH.is_file() else None,
                                 "server_name": "dungeon-system", "version": "0.4.2-rc1",
                                 "tool_count": TOOLS_EXPECTED if client else None,
                                 "tool_names": sorted(item.get("name", "") for item in tools) if client and "tools" in locals() else []},
            "project_root": str(PROJECT_ROOT),
            "source_root": str(REPOSITORY_ROOT),
            "binary": {"scion_sha256": scion_hash, "helper_sha256": helper_hash},
            "scion_revisions": context.get("revisions", []) if context else [],
            "source_and_state_sha256": project_hashes,
            "metadata_transfer": {"status": transfer.get("status"), "file_count": len(transfer.get("files", {}))}
            if transfer else None,
            "vcs_queries": vcs_summary,
            "assertions": safe_assertions,
            "inventory": {"before": before, "after": after, "unchanged": before == after}
            if before is not None else None,
            "teardown": {"mcp_server_exit_code": server_exit, "testcluster_exit_code": helper_exit,
                         "listener_closed": listener_closed, "stop_signal_created": STOP_PATH.exists()},
            "errors": ([{"code": error_code}] if error_code else []),
            "limitations": ["Scion status and working-copy diff are intentionally not reported as AVAILABLE.",
                            "Scion inspect/history responses may have tool status PARTIAL while source availability is AVAILABLE."],
        }
        if client:
            _write_json_new(OUTPUT_ROOT / "mcp-requests.json", requests, secret)
            _write_json_new(OUTPUT_ROOT / "mcp-responses.json", responses, secret)
        _write_json_new(OUTPUT_ROOT / "result.json", summary, secret)
        for path in OUTPUT_ROOT.iterdir():
            if path.is_file() and secret and secret.encode("utf-8") in path.read_bytes():
                raise GateError("CREDENTIAL_IN_RECEIPT")
    else:
        summary = {"schema_version": 1, "task": "DUNGEON-SCION-RUNTIME-002",
                   "status": status, "errors": ([{"code": error_code}] if error_code else []),
                   "teardown": {"mcp_server_exit_code": server_exit,
                                "testcluster_exit_code": helper_exit,
                                "stop_signal_created": STOP_PATH.exists()}}

    return summary, client.requests if client else [], client.responses if client else [], 0 if status == "PASS" else 2


def main() -> int:
    if os.name != "posix":
        print(json.dumps({"status": "BLOCKED", "error": "NATIVE_LINUX_REQUIRED"}))
        return 2
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bootstrap", type=Path, required=True,
                        help="existing private helper-build receipt")
    parser.add_argument("--installed-server", type=Path, required=True,
                        help="installed MCP server.py on the native Linux filesystem view")
    args = parser.parse_args()
    try:
        _configure(args.bootstrap, args.installed_server)
        summary, _, _, code = execute()
    except GateError as exc:
        print(json.dumps({"status": "BLOCKED", "error": exc.code}))
        return 2
    print(json.dumps({"status": summary["status"], "vcs_queries": summary["vcs_queries"],
                      "teardown": summary["teardown"], "receipt": str(OUTPUT_ROOT / "result.json")},
                     ensure_ascii=True))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
