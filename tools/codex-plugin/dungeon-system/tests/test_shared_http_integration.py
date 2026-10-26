"""Real two-client canary for the optional shared HTTP MCP service.

Run with the dedicated HTTP venv after its unit and worker-lifecycle tests pass:
    python -B tests/test_shared_http_integration.py

Set DUNGEON_HTTP_INTEGRATION_REPORT to an absolute JSON path for a sanitized,
bounded report. Credentials are generated per run and passed only in the
service child's environment; they are never written to the config or report.
"""

from __future__ import annotations

import ctypes
from concurrent.futures import ThreadPoolExecutor
import hashlib
import importlib.util
import importlib.metadata
import json
import os
import secrets
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[4]
PLUGIN_ROOT = Path(__file__).resolve().parents[1]
SERVICE_PATH = PLUGIN_ROOT / "mcp" / "http_service.py"
SERVER_PATH = PLUGIN_ROOT / "mcp" / "server.py"
EXPECTED_TOOL_COUNT = 34
PROTOCOL_VERSION = "2025-11-25"
HTTP_TIMEOUT = 8
MAX_HTTP_RESPONSE = 1_048_577
_CURRENT_SESSION = object()


def _load_server_contract():
    spec = importlib.util.spec_from_file_location("dungeon_http_integration_server", SERVER_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError("Cannot load the local MCP tool contract")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


SERVER = _load_server_contract()


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def _http_request(
    base_url: str,
    path: str,
    token: str,
    *,
    method: str = "GET",
    body: dict[str, Any] | None = None,
    session_id: str | None = None,
    protocol_version: str | None = None,
) -> tuple[int, Any, bytes]:
    headers = {"Authorization": f"Bearer {token}"}
    if body is not None:
        headers["Content-Type"] = "application/json"
        headers["Accept"] = "application/json, text/event-stream"
    if session_id is not None:
        headers["MCP-Session-Id"] = session_id
    if protocol_version is not None:
        headers["MCP-Protocol-Version"] = protocol_version
    data = None if body is None else json.dumps(
        body, ensure_ascii=False, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")
    request = urllib.request.Request(
        base_url + path, data=data, headers=headers, method=method
    )
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())
    try:
        response = opener.open(request, timeout=HTTP_TIMEOUT)
    except urllib.error.HTTPError as exc:
        response = exc
    except (OSError, TimeoutError, urllib.error.URLError):
        raise AssertionError("HTTP integration request could not reach its owned service") from None
    try:
        payload = response.read(MAX_HTTP_RESPONSE)
        if len(payload) >= MAX_HTTP_RESPONSE:
            raise AssertionError("HTTP integration response exceeded the bounded read size")
        return response.status, response.headers, payload
    finally:
        response.close()


def _json_body(payload: bytes) -> dict[str, Any]:
    try:
        value = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise AssertionError("HTTP integration endpoint returned non-JSON content") from None
    if not isinstance(value, dict):
        raise AssertionError("HTTP integration endpoint returned an unexpected JSON shape")
    return value


class _HttpMcpClient:
    def __init__(self, client_id: str, base_url: str, token: str):
        self.client_id = client_id
        self.base_url = base_url
        self._token = token
        self.session_id: str | None = None
        self.protocol_version: str | None = None

    def rpc(
        self,
        body: dict[str, Any],
        *,
        session_id: str | None | object = _CURRENT_SESSION,
        token: str | None = None,
    ) -> tuple[int, Any, bytes]:
        selected_session = self.session_id if session_id is _CURRENT_SESSION else session_id
        return _http_request(
            self.base_url,
            "/mcp",
            self._token if token is None else token,
            method="POST",
            body=body,
            session_id=selected_session,
            protocol_version=self.protocol_version,
        )

    def initialize(self) -> tuple[dict[str, Any], int]:
        status, headers, payload = self.rpc(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": PROTOCOL_VERSION,
                    "capabilities": {},
                    "clientInfo": {"name": self.client_id, "version": "integration"},
                },
            },
            session_id=None,
        )
        response = _json_body(payload) if status == 200 else {}
        self.session_id = headers.get("MCP-Session-Id")
        self.protocol_version = response.get("result", {}).get("protocolVersion")
        return response, status

    def initialized(self) -> int:
        status, _, _ = self.rpc({"jsonrpc": "2.0", "method": "notifications/initialized"})
        return status

    def call(self, request_id: int, name: str, arguments: dict[str, Any]):
        return self.rpc(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "method": "tools/call",
                "params": {"name": name, "arguments": arguments},
            }
        )

    def get(self, path: str, *, session_id: str | None = None):
        selected_session = self.session_id if session_id is None else session_id
        return _http_request(
            self.base_url,
            path,
            self._token,
            session_id=selected_session,
        )


class _WindowsProcessObserver:
    """Track the owned process tree by PID plus a held process handle/birth time.

    CIM supplies the bounded PID/parent snapshot. OpenProcess and
    GetProcessTimes pin each discovered process identity, so a reused PID is
    never mistaken for the earlier child and no unrelated process is stopped.
    """

    _PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    _SYNCHRONIZE = 0x00100000
    _WAIT_OBJECT_0 = 0
    _WAIT_TIMEOUT = 0x102

    def __init__(self, root_pid: int):
        self.root_pid = root_pid
        self.available = False
        self.error = False
        self._kernel = None
        self._owned: dict[tuple[int, int], tuple[int, int]] = {}
        if os.name != "nt":
            return
        try:
            self._kernel = ctypes.WinDLL("kernel32", use_last_error=True)
            self._kernel.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
            self._kernel.OpenProcess.restype = ctypes.c_void_p
            self._kernel.GetProcessTimes.argtypes = [
                ctypes.c_void_p,
                ctypes.c_void_p,
                ctypes.c_void_p,
                ctypes.c_void_p,
                ctypes.c_void_p,
            ]
            self._kernel.GetProcessTimes.restype = ctypes.c_int
            self._kernel.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
            self._kernel.WaitForSingleObject.restype = ctypes.c_uint32
            self._kernel.CloseHandle.argtypes = [ctypes.c_void_p]
            self._kernel.CloseHandle.restype = ctypes.c_int
            self._capture(root_pid, 0)
        except (AttributeError, OSError, TypeError, ValueError):
            self.error = True

    @staticmethod
    def _cim_rows() -> list[tuple[int, int, int]] | None:
        script = (
            "$ErrorActionPreference='Stop'; "
            "$rows=@(Get-CimInstance -ClassName Win32_Process | Where-Object { $null -ne $_.CreationDate } | ForEach-Object { "
            "[pscustomobject]@{pid=[int]$_.ProcessId; parent=[int]$_.ParentProcessId; "
            "birth=[long]($_.CreationDate.ToUniversalTime().ToFileTimeUtc())} }); "
            "ConvertTo-Json -InputObject $rows -Compress -Depth 2"
        )
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        try:
            completed = subprocess.run(
                ["powershell.exe", "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", script],
                capture_output=True,
                text=True,
                timeout=5,
                creationflags=flags,
                check=False,
            )
            if completed.returncode != 0 or not completed.stdout.strip():
                return None
            raw = json.loads(completed.stdout)
            if isinstance(raw, dict):
                raw = [raw]
            if not isinstance(raw, list):
                return None
            rows = []
            for item in raw:
                if not isinstance(item, dict):
                    continue
                rows.append((int(item["pid"]), int(item["parent"]), int(item["birth"])))
            return rows
        except (OSError, subprocess.SubprocessError, ValueError, TypeError, KeyError, json.JSONDecodeError):
            return None

    def _birth_and_handle(self, pid: int) -> tuple[int, int] | None:
        assert self._kernel is not None

        class _FileTime(ctypes.Structure):
            _fields_ = [("low", ctypes.c_uint32), ("high", ctypes.c_uint32)]

        access = self._PROCESS_QUERY_LIMITED_INFORMATION | self._SYNCHRONIZE
        handle = self._kernel.OpenProcess(access, 0, pid)
        if not handle:
            return None
        created = _FileTime()
        exited = _FileTime()
        kernel = _FileTime()
        user = _FileTime()
        if not self._kernel.GetProcessTimes(
            handle, ctypes.byref(created), ctypes.byref(exited), ctypes.byref(kernel), ctypes.byref(user)
        ):
            self._kernel.CloseHandle(handle)
            return None
        birth = (int(created.high) << 32) | int(created.low)
        return int(handle), birth

    def _capture(self, pid: int, expected_birth: int) -> bool:
        opened = self._birth_and_handle(pid)
        if opened is None:
            return False
        handle, birth = opened
        # CIM_DATETIME has microsecond precision; GetProcessTimes has 100 ns
        # precision. The held handle remains the authoritative process identity.
        if expected_birth and abs(birth - expected_birth) >= 10:
            self._kernel.CloseHandle(handle)
            return False
        identity = (pid, birth)
        if identity in self._owned:
            self._kernel.CloseHandle(handle)
        else:
            self._owned[identity] = (handle, 0 if pid == self.root_pid else 1)
        return True

    def observe(self) -> None:
        if os.name != "nt" or self._kernel is None:
            return
        rows = self._cim_rows()
        if rows is None:
            self.error = True
            return
        by_pid = {pid: (parent, birth) for pid, parent, birth in rows}
        root = by_pid.get(self.root_pid)
        root_identity = next((key for key in self._owned if key[0] == self.root_pid), None)
        if root_identity is None:
            if root is None or not self._capture(self.root_pid, root[1]):
                self.error = True
                return
            root_identity = next(key for key in self._owned if key[0] == self.root_pid)
        if root is not None and abs(root[1] - root_identity[1]) >= 10:
            self.error = True
            return
        root_handle = self._owned[root_identity][0]
        root_alive = (
            root is not None
            and abs(root[1] - root_identity[1]) < 10
            and self._kernel.WaitForSingleObject(root_handle, 0) == self._WAIT_TIMEOUT
        )
        known = {root_identity[0]: root_identity[1]} if root_alive else {}
        known.update(
            {pid: birth for (pid, birth), (handle, _) in self._owned.items()
             if pid != self.root_pid and self._kernel.WaitForSingleObject(handle, 0) == self._WAIT_TIMEOUT}
        )
        frontier = set(known)
        while frontier:
            next_frontier: set[int] = set()
            for pid, (parent, birth) in by_pid.items():
                if parent not in frontier or pid in known:
                    continue
                if not self._capture(pid, birth):
                    continue
                identity = next(key for key in self._owned if key[0] == pid and abs(key[1] - birth) < 10)
                if self._kernel.WaitForSingleObject(self._owned[identity][0], 0) == self._WAIT_TIMEOUT:
                    known[pid] = identity[1]
                    next_frontier.add(pid)
            frontier = next_frontier
        self.available = True

    @property
    def observed_descendants(self) -> int:
        return sum(1 for pid, _ in self._owned if pid != self.root_pid)

    def active_descendants(self) -> int | None:
        if not self.available or self._kernel is None:
            return None
        return sum(
            1 for (pid, _), (handle, _) in self._owned.items()
            if pid != self.root_pid and self._kernel.WaitForSingleObject(handle, 0) == self._WAIT_TIMEOUT
        )

    def close(self) -> None:
        if self._kernel is None:
            return
        for handle, _ in self._owned.values():
            self._kernel.CloseHandle(handle)
        self._owned.clear()


class SharedHttpTwoClientIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._service: subprocess.Popen | None = None
        cls._observer: _WindowsProcessObserver | None = None
        cls._endpoint_verified = False
        cls._test_completed = False
        cls._checks: list[dict[str, Any]] = []
        cls._report: dict[str, Any] = {
            "schema_version": 1,
            "result": "INCOMPLETE",
            "transport": "streamable-http",
            "client_count": 2,
            "session_count": 2,
            "fixture_mode": "two independent synthetic SKLT_ONLY roots",
            "fixture_write_grants": 0,
            "cas_write_test": "NOT_RUN",
            "queue_cancel_http_test": "NOT_RUN",
            "checks": cls._checks,
            "python_version": sys.version.split()[0],
            "aiohttp_version": importlib.metadata.version("aiohttp"),
            "server_version": SERVER.VERSION,
            "source_sha256": {
                name: hashlib.sha256((PLUGIN_ROOT / "mcp" / name).read_bytes()).hexdigest()
                for name in ("server.py", "http_service.py", "http_runtime.py")
            },
        }
        cls._temporary = tempfile.TemporaryDirectory(prefix="dungeon-http-two-client-")
        cls._temporary_path = Path(cls._temporary.name)
        cls.addClassCleanup(cls._finalize)

        cls._root_a = cls._make_sklt_project("client-a", "SyntheticA")
        cls._root_b = cls._make_sklt_project("client-b", "SyntheticB")
        cls._port = cls._reserve_loopback_port()
        cls._base_url = f"http://127.0.0.1:{cls._port}"
        cls._admin_token = secrets.token_urlsafe(48)
        cls._token_a = secrets.token_urlsafe(48)
        cls._token_b = secrets.token_urlsafe(48)
        cls._config_path = cls._temporary_path / "service.private.json"
        cls._write_private_config()
        cls._start_service()
        cls._client_a = _HttpMcpClient("synthetic_client_a", cls._base_url, cls._token_a)
        cls._client_b = _HttpMcpClient("synthetic_client_b", cls._base_url, cls._token_b)

    @classmethod
    def _make_sklt_project(cls, directory: str, project_name: str) -> Path:
        root = cls._temporary_path / directory
        sklt = root / "sklt"
        sklt.mkdir(parents=True)
        (sklt / "project.sklt").write_text(f"project {project_name}:\n", encoding="utf-8")
        (sklt / "module.sklt").write_text("module Test:\n", encoding="utf-8")
        return root.resolve()

    @staticmethod
    def _reserve_loopback_port() -> int:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
            listener.bind(("127.0.0.1", 0))
            return int(listener.getsockname()[1])

    @classmethod
    def _write_private_config(cls) -> None:
        data = {
            "schema_version": 1,
            "home_root": str(REPO_ROOT.resolve()),
            "port": cls._port,
            "workers": 2,
            "queue_limit": 8,
            "request_timeout_sec": 30,
            "body_timeout_sec": 5,
            "startup_timeout_sec": 20,
            "shutdown_grace_sec": 5,
            "session_idle_sec": 120,
            "max_sessions": 8,
            "max_sessions_per_client": 4,
            "max_requests_per_session": 64,
            "max_connections": 16,
            "max_http_requests": 8,
            "per_client_limit": 4,
            "per_session_limit": 4,
            "history_limit": 16,
            "history_ttl_sec": 120,
            "admin_token_env_var": "DUNGEON_HTTP_INTEGRATION_ADMIN",
            "clients": [
                {
                    "id": "synthetic_client_a",
                    "token_env_var": "DUNGEON_HTTP_INTEGRATION_CLIENT_A",
                    "project_roots": [str(cls._root_a)],
                    "writable_roots": [],
                    "input_roots": [],
                },
                {
                    "id": "synthetic_client_b",
                    "token_env_var": "DUNGEON_HTTP_INTEGRATION_CLIENT_B",
                    "project_roots": [str(cls._root_b)],
                    "writable_roots": [],
                    "input_roots": [],
                },
            ],
        }
        cls._config_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
        try:
            cls._config_path.chmod(0o600)
        except OSError:
            pass
        encoded = cls._config_path.read_text(encoding="utf-8")
        secrets_absent = all(token not in encoded for token in (cls._admin_token, cls._token_a, cls._token_b))
        if not secrets_absent:
            raise AssertionError("Generated credentials must not be stored in the service config")

    @classmethod
    def _start_service(cls) -> None:
        child_env = os.environ.copy()
        for key in tuple(child_env):
            if key.startswith("DUNGEON_HTTP_"):
                child_env.pop(key, None)
        child_env.update(
            {
                "DUNGEON_HTTP_INTEGRATION_ADMIN": cls._admin_token,
                "DUNGEON_HTTP_INTEGRATION_CLIENT_A": cls._token_a,
                "DUNGEON_HTTP_INTEGRATION_CLIENT_B": cls._token_b,
            }
        )
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        options: dict[str, Any] = {}
        if os.name != "nt":
            options["start_new_session"] = True
        cls._service = subprocess.Popen(
            [sys.executable, "-B", str(SERVICE_PATH), "serve", "--config", str(cls._config_path)],
            cwd=str(REPO_ROOT),
            env=child_env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
            creationflags=flags,
            **options,
        )
        if os.name == "nt":
            cls._observer = _WindowsProcessObserver(cls._service.pid)
            cls._observer.observe()

        deadline = time.monotonic() + 45
        last_status = None
        while time.monotonic() < deadline:
            if cls._observer is not None:
                cls._observer.observe()
            if cls._service.poll() is not None:
                raise AssertionError("Isolated HTTP service exited before authenticated readiness")
            try:
                status, _, payload = _http_request(
                    cls._base_url, "/healthz", cls._admin_token
                )
            except AssertionError:
                time.sleep(0.2)
                continue
            last_status = status
            if status in {401, 403, 404}:
                raise AssertionError("Selected loopback port did not authenticate as this test service")
            if status in {200, 503}:
                health = _json_body(payload)
                if health.get("service") != "dungeon-shared-http":
                    raise AssertionError("Selected loopback endpoint is not the isolated Dungeon service")
                cls._endpoint_verified = True
                if status == 200 and health.get("ready") is True and health.get("live_workers") == 2:
                    cls._report["dispatcher_live_workers_at_ready"] = health["live_workers"]
                    cls._report["windows_process_descendants_observed_at_ready"] = (
                        cls._observer.observed_descendants if cls._observer and cls._observer.available else None
                    )
                    cls._report["windows_active_descendants_at_ready"] = (
                        cls._observer.active_descendants() if cls._observer else None
                    )
                    return
            time.sleep(0.2)
        raise AssertionError(f"Isolated service did not become ready (last HTTP status: {last_status})")

    def _check(self, name: str, condition: bool) -> None:
        self._checks.append({"name": name, "result": "PASS" if condition else "FAIL"})
        self.assertTrue(condition, f"HTTP integration check failed: {name}")

    @staticmethod
    def _tool_payload(response: dict[str, Any]) -> dict[str, Any]:
        try:
            result = response["result"]
            payload = result["structuredContent"]
        except (KeyError, TypeError):
            raise AssertionError("MCP tool response omitted structured content") from None
        if not isinstance(payload, dict):
            raise AssertionError("MCP tool response had an unexpected content shape")
        return payload

    def test_two_independent_clients_and_project_boundaries(self) -> None:
        init_a, status_a = self._client_a.initialize()
        init_b, status_b = self._client_b.initialize()
        self._check(
            "both clients initialized distinct authenticated sessions",
            status_a == status_b == 200
            and isinstance(self._client_a.session_id, str)
            and isinstance(self._client_b.session_id, str)
            and self._client_a.session_id != self._client_b.session_id
            and init_a.get("result", {}).get("protocolVersion") == PROTOCOL_VERSION
            and init_b.get("result", {}).get("protocolVersion") == PROTOCOL_VERSION,
        )
        initialized_a = self._client_a.initialized()
        initialized_b = self._client_b.initialized()
        self._check("both clients completed notifications/initialized", initialized_a == initialized_b == 202)

        listed_a = self._client_a.rpc({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
        listed_b = self._client_b.rpc({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
        self._check("both sessions listed tools successfully", listed_a[0] == listed_b[0] == 200)
        tools_a = _json_body(listed_a[2]).get("result", {}).get("tools")
        tools_b = _json_body(listed_b[2]).get("result", {}).get("tools")
        expected_tools = SERVER._tool_list()
        self._check(
            "34 tools and schemas match the stdio server contract",
            isinstance(tools_a, list)
            and isinstance(tools_b, list)
            and len(tools_a) == len(tools_b) == EXPECTED_TOOL_COUNT
            and tools_a == tools_b == expected_tools,
        )
        schema_json = json.dumps(expected_tools, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        self._report["tool_count"] = len(tools_a) if isinstance(tools_a, list) else 0
        self._report["tool_schema_sha256"] = hashlib.sha256(schema_json.encode("utf-8")).hexdigest()

        invalid_token = secrets.token_urlsafe(48)
        unauthenticated = _http_request(self._base_url, "/healthz", invalid_token)
        self._check("unknown bearer token denied", unauthenticated[0] == 401)

        cross_session = self._client_b.rpc(
            {"jsonrpc": "2.0", "id": 77, "method": "tools/list", "params": {}},
            session_id=self._client_a.session_id,
        )
        self._check("session cannot be used by another principal", cross_session[0] == 404)

        with ThreadPoolExecutor(max_workers=2) as pool:
            future_a = pool.submit(self._client_a.call, 101, "dungeon_project_probe", {"project_root": str(self._root_a)})
            future_b = pool.submit(self._client_b.call, 202, "dungeon_project_probe", {"project_root": str(self._root_b)})
            probe_a = future_a.result(timeout=40)
            probe_b = future_b.result(timeout=40)
        self._check("concurrent probes completed through two HTTP clients", probe_a[0] == probe_b[0] == 200)
        rpc_a, rpc_b = _json_body(probe_a[2]), _json_body(probe_b[2])
        payload_a, payload_b = self._tool_payload(rpc_a), self._tool_payload(rpc_b)
        self._check(
            "each concurrent probe stayed on its exact SKLT_ONLY root",
            not rpc_a.get("result", {}).get("isError", True)
            and not rpc_b.get("result", {}).get("isError", True)
            and payload_a.get("project_root") == str(self._root_a)
            and payload_b.get("project_root") == str(self._root_b)
            and payload_a.get("project_mode") == payload_b.get("project_mode") == "SKLT_ONLY",
        )

        wrong_root = self._client_a.call(
            303, "dungeon_project_probe", {"project_root": str(self._root_b)}
        )
        self._check("client cannot probe another client's root", wrong_root[0] == 403)
        missing_root = self._client_a.call(305, "dungeon_project_probe", {})
        self._check("HTTP calls cannot inherit the coordinator's home root", missing_root[0] == 403)
        get_mcp = self._client_a.get("/mcp")
        self._check("non-SSE GET MCP is explicitly refused", get_mcp[0] == 405)
        denied_admin = _http_request(self._base_url, "/admin/drain", self._token_a, method="POST", body={})
        self._check("tool client cannot drain the service", denied_admin[0] == 403)

        # The second candidate has the same source-home lease. It must fail
        # before accepting connections, without affecting the first service.
        candidate_env = os.environ.copy()
        candidate_env.update({
            "DUNGEON_HTTP_INTEGRATION_ADMIN": self._admin_token,
            "DUNGEON_HTTP_INTEGRATION_CLIENT_A": self._token_a,
            "DUNGEON_HTTP_INTEGRATION_CLIENT_B": self._token_b,
        })
        candidate = subprocess.run(
            [sys.executable, "-B", str(SERVICE_PATH), "serve", "--config", str(self._config_path)],
            env=candidate_env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            timeout=10, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        self._check("a second service cannot steal the active source-home lease", candidate.returncode == 1)

        before_write_attempt = self._fixture_fingerprint(self._root_a)
        denied_write = self._client_a.call(
            304, "dungeon_refresh", {"project_root": str(self._root_a)}
        )
        after_write_attempt = self._fixture_fingerprint(self._root_a)
        self._check(
            "write capability denied before dispatch and fixture stayed unchanged",
            denied_write[0] == 403
            and "Dungeon-Operation-Id" not in denied_write[1]
            and before_write_attempt == after_write_attempt,
        )

        operation_id = probe_a[1].get("Dungeon-Operation-Id")
        self._check("completed tool call returned an operation identifier", isinstance(operation_id, str) and bool(operation_id))
        if not isinstance(operation_id, str) or not operation_id:
            return
        operation_a = self._client_a.get("/operations/" + urllib.parse.quote(operation_id, safe=""))
        operation_b = self._client_b.get("/operations/" + urllib.parse.quote(operation_id, safe=""))
        self._check(
            "operation status is visible only to its original client and session",
            operation_a[0] == 200
            and _json_body(operation_a[2]).get("state") == "COMPLETED"
            and operation_b[0] == 404,
        )
        request_lookup = self._client_a.get(
            "/operations?" + urllib.parse.urlencode({"request_id": json.dumps(101)})
        )
        foreign_request_lookup = self._client_b.get(
            "/operations?" + urllib.parse.urlencode({"request_id": json.dumps(101)})
        )
        self._check(
            "request-id operation lookup is owner scoped",
            request_lookup[0] == 200
            and _json_body(request_lookup[2]).get("operation_id") == operation_id
            and foreign_request_lookup[0] == 404,
        )

        duplicate = self._client_a.call(
            101, "dungeon_project_probe", {"project_root": str(self._root_a)}
        )
        duplicate_json = _json_body(duplicate[2]) if duplicate[0] == 400 else {}
        self._check(
            "duplicate JSON-RPC ID is rejected before a second operation",
            duplicate[0] == 400
            and duplicate_json.get("error", {}).get("code") == -32600
            and "Dungeon-Operation-Id" not in duplicate[1],
        )

        health = _http_request(self._base_url, "/healthz", self._admin_token)
        health_body = _json_body(health[2]) if health[0] == 200 else {}
        self._check(
            "authenticated health reports the ready bounded pool",
            health[0] == 200
            and health_body.get("service") == "dungeon-shared-http"
            and health_body.get("ready") is True
            and health_body.get("worker_limit") == 2
            and health_body.get("live_workers") == 2
            and health_body.get("queued") == 0,
        )
        if self._observer is not None:
            self._observer.observe()
            self._report["windows_process_descendants_observed_before_shutdown"] = (
                self._observer.observed_descendants if self._observer.available else None
            )
            self._check(
                "owned Windows descendants were observed with lifetime handles",
                self._observer.available and self._observer.observed_descendants >= 2,
            )
        self._report["protocol_version"] = PROTOCOL_VERSION
        deleted = _http_request(self._base_url, "/mcp", self._token_a, method="DELETE",
                                session_id=self._client_a.session_id, protocol_version=PROTOCOL_VERSION)
        after_delete = self._client_a.rpc({"jsonrpc": "2.0", "id": 401, "method": "ping"})
        other_ping = self._client_b.rpc({"jsonrpc": "2.0", "id": 401, "method": "ping"})
        self._check("DELETE closes only the caller's session",
                    deleted[0] == 204 and after_delete[0] == 404 and other_ping[0] == 200)
        type(self)._test_completed = True

    @staticmethod
    def _fixture_fingerprint(root: Path) -> tuple[tuple[str, str], ...]:
        entries: list[tuple[str, str]] = []
        for current, directories, files in os.walk(root, topdown=True, followlinks=False):
            directories[:] = sorted(d for d in directories if not (Path(current) / d).is_symlink())
            for name in sorted(files):
                path = Path(current) / name
                relative = path.relative_to(root).as_posix()
                entries.append((relative, hashlib.sha256(path.read_bytes()).hexdigest()))
        return tuple(sorted(entries))

    @classmethod
    def _try_authenticated_admin(cls, command: str) -> bool:
        if cls._service is None or cls._service.poll() is not None:
            return False
        path = {"health": "/healthz", "drain": "/admin/drain", "stop": "/admin/shutdown"}[command]
        status, _, payload = _http_request(
            cls._base_url,
            path,
            cls._admin_token,
            method="GET" if command == "health" else "POST",
            body=None if command == "health" else {},
        )
        value = _json_body(payload) if payload else {}
        if command == "health":
            valid = status in {200, 503} and value.get("service") == "dungeon-shared-http"
            if valid:
                cls._endpoint_verified = True
            return valid
        return status == 202 and value.get("status") == "DRAINING"

    @classmethod
    def _finalize(cls) -> None:
        cleanup = {"coordinator_exited": True, "admin_drain_sent": False, "admin_stop_sent": False}
        try:
            process = cls._service
            if process is not None and process.poll() is None:
                if not cls._endpoint_verified:
                    try:
                        cls._try_authenticated_admin("health")
                    except AssertionError:
                        pass
                if cls._endpoint_verified:
                    for command, field in (("drain", "admin_drain_sent"), ("stop", "admin_stop_sent")):
                        try:
                            cleanup[field] = cls._try_authenticated_admin(command)
                        except AssertionError:
                            cleanup[field] = False
                try:
                    process.wait(timeout=25)
                except subprocess.TimeoutExpired:
                    cleanup["coordinator_exited"] = False
                else:
                    cleanup["service_exit_code"] = process.returncode
            elif process is not None:
                cleanup["service_exit_code"] = process.returncode

            if cls._observer is not None:
                cls._observer.observe()
                if process is not None and process.poll() is not None:
                    deadline = time.monotonic() + 8
                    while time.monotonic() < deadline:
                        active = cls._observer.active_descendants()
                        if active == 0:
                            break
                        time.sleep(0.2)
                        cls._observer.observe()
                cleanup["windows_process_descendants_observed"] = cls._observer.observed_descendants
                cleanup["windows_process_descendants_active_after_stop"] = cls._observer.active_descendants()
                cleanup["windows_process_lifetime_observation"] = (
                    "OBSERVED" if cls._observer.available else "UNAVAILABLE"
                )
                if cleanup["windows_process_descendants_active_after_stop"] not in {0, None}:
                    cleanup["worker_processes_cleaned_up"] = False
                elif cls._observer.available:
                    cleanup["worker_processes_cleaned_up"] = True
                cls._observer.close()
            else:
                cleanup["windows_process_lifetime_observation"] = "NOT_APPLICABLE"
            if process is not None and process.poll() is None:
                cleanup["coordinator_exited"] = False
            admin_ok = not cls._endpoint_verified or (
                cleanup["admin_drain_sent"] and cleanup["admin_stop_sent"]
            )
            cleanup_ok = (
                cleanup["coordinator_exited"]
                and cleanup.get("service_exit_code") == 0
                and cleanup.get("worker_processes_cleaned_up", True)
                and admin_ok
            )
            cls._report["cleanup"] = cleanup
            cls._report["result"] = "PASS" if cls._test_completed and cleanup_ok else "FAIL"
        finally:
            report_path = os.environ.get("DUNGEON_HTTP_INTEGRATION_REPORT")
            try:
                if report_path:
                    target = Path(report_path)
                    if not target.is_absolute():
                        raise RuntimeError("Integration report path must be absolute") from None
                    if target.resolve().is_relative_to(cls._temporary_path.resolve()):
                        raise RuntimeError("Integration report path must be outside the temporary fixtures") from None
                    try:
                        encoded_report = json.dumps(cls._report, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
                        if any(token in encoded_report for token in (cls._admin_token, cls._token_a, cls._token_b)):
                            raise RuntimeError("Credential value detected in integration report")
                        target.parent.mkdir(parents=True, exist_ok=True)
                        target.write_text(
                            encoded_report,
                            encoding="utf-8",
                        )
                    except OSError:
                        raise RuntimeError("Could not write the sanitized integration report") from None
            finally:
                cls._temporary.cleanup()
        if cls._report.get("result") != "PASS":
            raise AssertionError("HTTP integration canary or owned-process cleanup did not pass")


if __name__ == "__main__":
    unittest.main(verbosity=2)
