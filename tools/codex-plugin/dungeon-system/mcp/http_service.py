"""Optional localhost Streamable HTTP MCP service; never started by the plugin.

Run manually with `serve --config <absolute-private-json>`. The legacy stdio
launcher/configuration remain unchanged. This module does not install anything.
"""

from __future__ import annotations

import argparse
import asyncio
import hmac
import ipaddress
import json
import math
import os
import re
import secrets
import signal
import socket
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import server as bridge
from http_runtime import AdmissionError, Dispatcher


MAX_BODY_BYTES = 1_048_576
TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{32,256}$")
ENV_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
SERVICE_VERSION = "0.1.0-proposed"
DEFAULTS = {
    "port": 8768, "workers": 2, "queue_limit": 16, "request_timeout_sec": 45,
    "body_timeout_sec": 5, "startup_timeout_sec": 15, "shutdown_grace_sec": 30,
    "session_idle_sec": 900, "max_sessions": 64, "max_sessions_per_client": 8,
    "max_requests_per_session": 2048, "max_connections": 48, "max_http_requests": 32,
    "per_client_limit": 8, "per_session_limit": 4, "history_limit": 32, "history_ttl_sec": 300,
}
LIMITS = {
    "port": (1024, 65535), "workers": (1, 4), "queue_limit": (1, 64),
    "request_timeout_sec": (1, 300), "body_timeout_sec": (1, 15),
    "startup_timeout_sec": (1, 60), "shutdown_grace_sec": (1, 300),
    "session_idle_sec": (30, 3600), "max_sessions": (1, 256),
    "max_sessions_per_client": (1, 32), "max_requests_per_session": (16, 4096),
    "max_connections": (8, 128), "max_http_requests": (4, 64),
    "per_client_limit": (1, 32), "per_session_limit": (1, 16),
    "history_limit": (1, 128), "history_ttl_sec": (30, 1800),
}


@dataclass(frozen=True)
class Client:
    client_id: str
    token: str = field(repr=False)
    policy: bridge.RequestPolicy


@dataclass(frozen=True)
class Config:
    home_root: str
    clients: tuple[Client, ...]
    admin_token: str = field(repr=False)
    limits: dict[str, int]
    host: str = "127.0.0.1"
    credential_env_names: tuple[str, ...] = ()

    @property
    def authority(self) -> str:
        return f"{self.host}:{self.limits['port']}"


def _roots(value: Any, label: str, *, required: bool = False) -> tuple[str, ...]:
    if not isinstance(value, list) or len(value) > 32 or any(not isinstance(x, str) for x in value):
        raise ValueError(f"{label} must be a bounded array of absolute physical roots")
    if required and not value:
        raise ValueError(f"{label} must not be empty")
    return tuple(str(bridge._read_root(x)) for x in value)


def _secret(env_name: Any) -> str:
    if (not isinstance(env_name, str) or not ENV_RE.fullmatch(env_name) or
        not env_name.startswith("DUNGEON_HTTP_") or len(env_name) > 128):
        raise ValueError("Credential configuration must name a dedicated DUNGEON_HTTP_ environment variable")
    token = os.environ.get(env_name, "")
    if not TOKEN_RE.fullmatch(token):
        raise ValueError(f"{env_name} must contain a 32..256 character URL-safe secret")
    return token


def load_config(path: str | Path) -> Config:
    config_path = Path(path)
    if not config_path.is_absolute() or config_path.stat().st_size > 131_072:
        raise ValueError("Config must be an absolute private JSON file, at most 128 KiB")
    data = json.loads(config_path.read_text(encoding="utf-8"), object_pairs_hook=bridge._unique_object,
                      parse_constant=bridge._reject_constant)
    allowed = {*DEFAULTS, "schema_version", "home_root", "host", "admin_token_env_var", "clients"}
    if (not isinstance(data, dict) or set(data) - allowed or
        type(data.get("schema_version")) is not int or data["schema_version"] != 1):
        raise ValueError("Unsupported service configuration")
    if data.get("host", "127.0.0.1") != "127.0.0.1":
        raise ValueError("Only IPv4 loopback binding is supported")
    home = bridge._checked_root(data.get("home_root", ""))
    source_home = Path(__file__).resolve().parents[4]
    if home != source_home or not (home / "tools/project_structure").is_dir():
        raise ValueError("home_root must be the full source clone containing this service")
    limits = dict(DEFAULTS)
    for name, bounds in LIMITS.items():
        value = data.get(name, limits[name])
        if type(value) is not int or not bounds[0] <= value <= bounds[1]:
            raise ValueError(f"Invalid bounded setting: {name}")
        limits[name] = value
    if limits["max_http_requests"] > limits["max_connections"] - 4:
        raise ValueError("Reserve at least four HTTP connections for control requests")
    rows = data.get("clients")
    if not isinstance(rows, list) or not 1 <= len(rows) <= 32:
        raise ValueError("Configure 1..32 authenticated clients")
    admin_token = _secret(data.get("admin_token_env_var"))
    credential_names = [data["admin_token_env_var"]]
    seen_ids: set[str] = set()
    seen_tokens = {admin_token}
    clients = []
    for row in rows:
        if not isinstance(row, dict) or set(row) - {"id", "token_env_var", "project_roots", "writable_roots", "input_roots"}:
            raise ValueError("Unknown client configuration field")
        name = row.get("id")
        if not isinstance(name, str) or not bridge.NAME_RE.fullmatch(name) or len(name) > 64 or name in seen_ids:
            raise ValueError("Client IDs must be unique bounded identifiers")
        token = _secret(row.get("token_env_var"))
        if token in seen_tokens:
            raise ValueError("Admin and client credentials must all be distinct")
        roots = _roots(row.get("project_roots"), "project_roots", required=True)
        writable = _roots(row.get("writable_roots", []), "writable_roots")
        inputs = _roots(row.get("input_roots", []), "input_roots")
        if not {Path(x) for x in writable} <= {Path(x) for x in roots}:
            raise ValueError("writable_roots must be a subset of the same client's project_roots")
        clients.append(Client(name, token, bridge.RequestPolicy(roots, writable, inputs)))
        credential_names.append(row["token_env_var"])
        seen_ids.add(name)
        seen_tokens.add(token)
    return Config(str(home), tuple(clients), admin_token, limits, credential_env_names=tuple(credential_names))


class SingletonLease:
    """OS advisory lifetime lock, not a PID file and not a project writer lease."""

    def __init__(self, config: Config):
        self.path = Path(config.home_root) / ".project/http-service.lock"
        self.file = None
        self.config = config

    def __enter__(self):
        bridge._checked_root(self.path.parent)
        if os.path.lexists(self.path) and (
            self.path.is_symlink() or getattr(self.path.lstat(), "st_file_attributes", 0) & 0x400
        ):
            raise ValueError("Unsafe singleton lock path")
        descriptor = os.open(self.path, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
        self.file = os.fdopen(descriptor, "r+b", buffering=0)
        try:
            if self.file.seek(0, os.SEEK_END) == 0:
                self.file.write(b" ")
            self.file.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.file.seek(0)
            self.file.write(b" " + json.dumps({"pid": os.getpid(), "endpoint": self.config.authority}).encode("ascii"))
            self.file.truncate()
        except BaseException:
            self.file.close()
            self.file = None
            raise RuntimeError("Another service may own the source-home lease, or its path is unavailable") from None
        return self

    def __exit__(self, *args):
        if self.file is not None:
            self.file.close()  # OS releases the lock. Never delete someone else's PID/lock file.
            self.file = None


@dataclass
class Session:
    session_id: str
    owner: str
    protocol: str
    touched: float
    initialized: bool = False
    used_ids: set = field(default_factory=set)


def _id_key(value: Any):
    return ("number", value) if type(value) in {int, float} else (type(value).__name__, value)


def _accepted_types(header: str) -> set[str]:
    accepted = set()
    for part in header.split(","):
        fields = [field.strip().lower() for field in part.split(";")]
        quality = 1.0
        for field in fields[1:]:
            if field.startswith("q="):
                try:
                    quality = float(field[2:])
                except ValueError:
                    quality = 0
        if 0 < quality <= 1:
            accepted.add(fields[0])
    return accepted


class Service:
    def __init__(self, config: Config, dispatcher: Dispatcher):
        self.config = config
        self.dispatcher = dispatcher
        self.sessions: dict[str, Session] = {}
        self.stop_event = asyncio.Event()
        self.active_http = 0
        self.header_arrived = lambda transport: None

    def authenticate(self, headers) -> Client | str | None:
        authorization = headers.get("Authorization", "")
        if not authorization.startswith("Bearer "):
            return None
        token = authorization[7:]
        if not TOKEN_RE.fullmatch(token):
            return None
        if hmac.compare_digest(token, self.config.admin_token):
            return "admin"
        for client in self.config.clients:
            if hmac.compare_digest(token, client.token):
                return client
        return None

    def boundary(self, request) -> bool:
        for name in ("Host", "Authorization", "Origin", "MCP-Session-Id", "MCP-Protocol-Version"):
            if len(request.headers.getall(name, [])) > 1:
                return False
        try:
            if not ipaddress.ip_address(request.remote or "").is_loopback:
                return False
        except ValueError:
            return False
        if request.headers.get("Host", "") not in {self.config.authority, f"localhost:{self.config.limits['port']}"}:
            return False
        origin = request.headers.get("Origin")
        return origin is None or origin in {f"http://{self.config.authority}", f"http://localhost:{self.config.limits['port']}"}

    def json_response(self, value, *, status: int = 200, headers=None):
        from aiohttp import web
        response = web.Response(body=json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8"),
                                status=status, content_type="application/json", headers=headers)
        response.force_close()
        return response

    def empty_response(self, status: int, *, headers=None):
        from aiohttp import web
        response = web.Response(status=status, headers=headers)
        response.force_close()
        return response

    def expire_sessions(self) -> None:
        now = time.monotonic()
        for session_id, session in list(self.sessions.items()):
            ttl = self.config.limits["session_idle_sec"] if session.initialized else self.config.limits["body_timeout_sec"] * 4
            if now - session.touched > ttl:
                self.dispatcher.cancel_session(session.owner, session_id)
                del self.sessions[session_id]

    def session_for(self, request, client: Client) -> Session | None:
        session = self.sessions.get(request.headers.get("MCP-Session-Id", ""))
        if session is None or session.owner != client.client_id:
            return None
        session.touched = time.monotonic()
        return session

    async def handle(self, request):
        from aiohttp import web
        self.header_arrived(request.transport)
        if not self.boundary(request):
            return self.json_response({"error": "LOCAL_ORIGIN_REQUIRED"}, status=403)
        principal = self.authenticate(request.headers)
        if principal is None:
            return self.json_response({"error": "AUTHENTICATION_REQUIRED"}, status=401,
                                      headers={"WWW-Authenticate": 'Bearer realm="Dungeon MCP"'})
        self.expire_sessions()
        if request.path == "/healthz" and request.method == "GET":
            state = self.dispatcher.status()
            return self.json_response({"service": "dungeon-shared-http", "version": SERVICE_VERSION, **state},
                                      status=200 if state["ready"] else 503)
        if request.path in {"/admin/drain", "/admin/shutdown"} and request.method == "POST":
            if principal != "admin":
                return self.json_response({"error": "ADMIN_REQUIRED"}, status=403)
            self.dispatcher.begin_shutdown()
            stop_requested = request.path == "/admin/shutdown"
            if stop_requested:
                self.stop_event.set()
            return self.json_response({"status": "DRAINING", "stop_requested": stop_requested,
                                       "writes_are_not_force_killed": True}, status=202)
        if principal == "admin":
            return self.json_response({"error": "ADMIN_TOKEN_IS_NOT_A_TOOL_CAPABILITY"}, status=403)
        if request.path == "/operations" and request.method == "GET":
            encoded_id = request.query.get("request_id", "")
            if len(encoded_id.encode("utf-8")) > 256:
                return self.empty_response(400)
            try:
                identifier = json.loads(encoded_id, parse_constant=bridge._reject_constant)
            except ValueError:
                return self.empty_response(400)
            if (isinstance(identifier, bool) or not isinstance(identifier, (str, int, float, type(None))) or
                (isinstance(identifier, float) and not math.isfinite(identifier))):
                return self.empty_response(400)
            operation = self.dispatcher.operation_for_request(identifier, principal.client_id,
                                                              request.headers.get("MCP-Session-Id", ""))
            return self.json_response(operation or {"error": "OPERATION_NOT_FOUND"}, status=200 if operation else 404)
        if request.path.startswith("/operations/") and request.method == "GET":
            operation_id = request.path[len("/operations/"):]
            operation = self.dispatcher.operation(operation_id, principal.client_id,
                                                  request.headers.get("MCP-Session-Id", ""))
            return self.json_response(operation or {"error": "OPERATION_NOT_FOUND"}, status=200 if operation else 404)
        if request.path != "/mcp":
            return self.json_response({"error": "NOT_FOUND"}, status=404)
        version = request.headers.get("MCP-Protocol-Version")
        if version is not None and version not in bridge.HANDSHAKE_VERSIONS:
            return self.json_response({"error": "UNSUPPORTED_PROTOCOL_VERSION"}, status=400)
        if request.method == "GET":
            return self.empty_response(405, headers={"Allow": "POST, DELETE"})
        if request.method not in {"POST", "DELETE"}:
            return self.empty_response(405, headers={"Allow": "POST, DELETE"})
        if self.active_http >= self.config.limits["max_http_requests"]:
            return self.json_response({"error": "HTTP_REQUEST_LIMIT"}, status=429, headers={"Retry-After": "1"})
        self.active_http += 1
        try:
            return await self._mcp(request, principal)
        except (asyncio.TimeoutError, TimeoutError):
            return self.json_response({"error": "BODY_READ_TIMEOUT"}, status=408)
        except web.HTTPRequestEntityTooLarge:
            return self.json_response({"error": "REQUEST_TOO_LARGE"}, status=413)
        except (ValueError, TypeError, UnicodeDecodeError, RecursionError):
            return self.json_response(bridge._rpc_error(bridge._MISSING, -32700, "Invalid JSON request"), status=400)
        finally:
            self.active_http -= 1

    async def _mcp(self, request, client: Client):
        if request.method == "DELETE":
            session = self.session_for(request, client)
            if session is None:
                return self.empty_response(404)
            version = request.headers.get("MCP-Protocol-Version", session.protocol)
            if version != session.protocol:
                return self.empty_response(400)
            self.dispatcher.cancel_session(client.client_id, session.session_id)
            del self.sessions[session.session_id]
            return self.empty_response(204)
        accept = _accepted_types(request.headers.get("Accept", ""))
        if not {"application/json", "text/event-stream"} <= accept:
            return self.json_response({"error": "ACCEPT_JSON_AND_EVENT_STREAM_REQUIRED"}, status=406)
        if request.content_type != "application/json" or request.charset not in {None, "utf-8", "UTF-8"}:
            return self.json_response({"error": "UTF8_JSON_REQUIRED"}, status=415)
        if request.headers.get("Content-Encoding", "identity").lower() != "identity":
            return self.json_response({"error": "CONTENT_ENCODING_NOT_SUPPORTED"}, status=415)
        if request.content_length is not None and request.content_length > MAX_BODY_BYTES:
            return self.json_response({"error": "REQUEST_TOO_LARGE"}, status=413)
        raw = await asyncio.wait_for(request.read(), self.config.limits["body_timeout_sec"])
        if len(raw) > MAX_BODY_BYTES:
            return self.json_response({"error": "REQUEST_TOO_LARGE"}, status=413)
        rpc = json.loads(raw.decode("utf-8"), object_pairs_hook=bridge._unique_object,
                         parse_constant=bridge._reject_constant)
        # Reject escaped lone surrogates before any invocation is admitted.
        json.dumps(rpc, ensure_ascii=False, allow_nan=False).encode("utf-8")
        error = bridge._request_error(rpc)
        if error is not None:
            return self.json_response(error, status=400)
        method, params = rpc["method"], rpc.get("params", {})
        request_id = rpc.get("id", bridge._MISSING)
        if request_id is not bridge._MISSING and len(json.dumps(request_id).encode("utf-8")) > 256:
            return self.json_response(bridge._rpc_error(None, -32600, "HTTP request ID exceeds 256 bytes"), status=400)
        if method == "initialize":
            if request_id is bridge._MISSING or request.headers.get("MCP-Session-Id"):
                return self.empty_response(400)
            if not self.dispatcher.ready:
                return self.json_response({"error": "SERVICE_NOT_READY"}, status=503)
            if (len(self.sessions) >= self.config.limits["max_sessions"] or
                sum(s.owner == client.client_id for s in self.sessions.values()) >= self.config.limits["max_sessions_per_client"]):
                return self.json_response({"error": "SESSION_LIMIT"}, status=429, headers={"Retry-After": "1"})
            response = bridge._handle(rpc)
            version = response["result"]["protocolVersion"]
            for _ in range(4):
                session_id = secrets.token_urlsafe(32)
                if session_id not in self.sessions:
                    break
            else:
                return self.json_response({"error": "SESSION_ID_ALLOCATION_FAILED"}, status=503)
            session = Session(session_id, client.client_id, version, time.monotonic())
            session.used_ids.add(_id_key(request_id))
            self.sessions[session_id] = session
            response["result"]["instructions"] = (
                "Shared HTTP: pass explicit project_root on EVERY tool call. Each client has operator-defined capabilities. "
                "Do not automatically retry an UNKNOWN write; inspect its operation_id first. " + response["result"]["instructions"])
            return self.json_response(response, headers={"MCP-Session-Id": session_id, "MCP-Protocol-Version": version})
        session = self.session_for(request, client)
        if session is None:
            return self.empty_response(404 if request.headers.get("MCP-Session-Id") else 400)
        if request.headers.get("MCP-Protocol-Version", session.protocol) != session.protocol:
            return self.json_response({"error": "PROTOCOL_VERSION_MISMATCH"}, status=400)
        if request_id is bridge._MISSING:
            if method == "notifications/initialized":
                session.initialized = True
            elif method == "notifications/cancelled":
                identifier = params.get("requestId", bridge._MISSING)
                if (identifier is not bridge._MISSING and not isinstance(identifier, bool) and
                    isinstance(identifier, (str, int, float, type(None))) and
                    not (isinstance(identifier, float) and not math.isfinite(identifier))):
                    self.dispatcher.cancel(client.client_id, session.session_id, identifier)
            return self.empty_response(202)
        if _id_key(request_id) in session.used_ids:
            return self.json_response(bridge._rpc_error(request_id, -32600, "Duplicate request ID in this session"), status=400)
        if len(session.used_ids) >= self.config.limits["max_requests_per_session"]:
            return self.json_response({"error": "SESSION_REQUEST_BUDGET_EXHAUSTED_INITIALIZE_A_NEW_SESSION"}, status=429)
        if not session.initialized:
            return self.json_response(bridge._rpc_error(request_id, -32000, "Session is not initialized"), status=400)
        if not self.dispatcher.ready:
            return self.json_response({"error": "SERVICE_NOT_READY"}, status=503)
        if method != "tools/call":
            session.used_ids.add(_id_key(request_id))
            return self.json_response(bridge._handle(rpc))
        arguments = params.get("arguments", {})
        if params["name"] not in bridge.TOOL_BY_NAME:
            return self.json_response(bridge._rpc_error(request_id, -32602, "Unknown tool"), status=400)
        try:
            # Pure lexical admission here. Target filesystem probing happens
            # inside the bounded worker, not on the HTTP control-plane loop.
            selected = bridge._root_path(bridge._required_string(arguments, "project_root"))
            if selected not in {Path(root) for root in client.policy.project_roots}:
                raise PermissionError("Project is not authorized")
            if params["name"] in bridge.WRITE_TOOL_NAMES and selected not in {Path(root) for root in client.policy.writable_roots}:
                raise PermissionError("Project is read-only")
        except (ValueError, TypeError, PermissionError, OSError):
            return self.json_response(bridge._rpc_error(request_id, -32602, "Explicit authorized physical project_root required"), status=403)
        try:
            # Narrow the worker's capability to this one project. Another
            # project registered to the same client is not an implicit input
            # directory for this operation; extra inputs require input_roots.
            write_scope = (str(selected),) if selected in {Path(root) for root in client.policy.writable_roots} else ()
            call_policy = bridge.RequestPolicy((str(selected),), write_scope, client.policy.input_roots)
            job = self.dispatcher.submit(client.client_id, session.session_id, rpc, str(selected), call_policy)
        except AdmissionError as exc:
            return self.json_response({"error": str(exc)}, status=429 if self.dispatcher.ready else 503,
                                      headers={"Retry-After": "1"})
        session.used_ids.add(_id_key(request_id))
        # A disconnect is not an MCP cancellation. Keep the admitted job alive
        # and its locks held even if this HTTP coroutine is interrupted.
        response = await self.dispatcher.result(job)
        return self.empty_response(204) if response is None else self.json_response(
            response, headers={"Dungeon-Operation-Id": job.operation_id})


async def serve(config: Config) -> None:
    from aiohttp import web

    # Auth material stays in the coordinator's immutable config, not in worker
    # or backend subprocess environments. This happens once, before spawn;
    # it is not request/client routing and changes no persistent OS setting.
    for name in config.credential_env_names:
        os.environ.pop(name, None)
    dispatcher = Dispatcher(config.home_root, workers=config.limits["workers"],
                            queue_limit=config.limits["queue_limit"], request_timeout=config.limits["request_timeout_sec"],
                            startup_timeout=config.limits["startup_timeout_sec"], per_client_limit=config.limits["per_client_limit"],
                            per_session_limit=config.limits["per_session_limit"], history_limit=config.limits["history_limit"],
                            history_ttl=config.limits["history_ttl_sec"])
    service = Service(config, dispatcher)

    class BoundedServer(web.Server):
        def __init__(self):
            super().__init__(service.handle, handler_cancellation=False, access_log=None,
                             keepalive_timeout=2, lingering_time=0, read_bufsize=16_384,
                             auto_decompress=False)
            self.header_deadlines = {}

        def connection_made(self, handler, transport):
            if len(self.connections) >= config.limits["max_connections"]:
                transport.close()
                return
            super().connection_made(handler, transport)
            self.header_deadlines[transport] = asyncio.get_running_loop().call_later(
                config.limits["body_timeout_sec"], transport.close)

        def headers_received(self, transport):
            timer = self.header_deadlines.pop(transport, None)
            if timer is not None:
                timer.cancel()

        def connection_lost(self, handler, exc):
            # Server's public connection_lost hook receives the handler only.
            # Expired timers are bounded by max_connections and removed here.
            transport = self._connections.get(handler)
            self.headers_received(transport)
            super().connection_lost(handler, exc)

    loop = asyncio.get_running_loop()
    for signum in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(signum, service.stop_event.set)
        except NotImplementedError:
            signal.signal(signum, lambda *_: loop.call_soon_threadsafe(service.stop_event.set))
    with SingletonLease(config):
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        runner = None
        try:
            if os.name == "nt":
                listener.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            listener.bind((config.host, config.limits["port"]))
            listener.listen(config.limits["max_connections"])
            listener.setblocking(False)
            await dispatcher.start()
            server = BoundedServer()
            service.header_arrived = server.headers_received
            runner = web.ServerRunner(server, shutdown_timeout=config.limits["body_timeout_sec"])
            await runner.setup()
            site = web.SockSite(runner, listener)
            await site.start()
            print(json.dumps({"status": "READY", "endpoint": f"http://{config.authority}/mcp",
                              "worker_limit": config.limits["workers"]}), flush=True)
            await service.stop_event.wait()
            dispatcher.begin_shutdown()
            # Keep authenticated health/admin available while writes drain.
            # Exceeding grace is observable, never a reason to kill a writer.
            grace = time.monotonic() + config.limits["shutdown_grace_sec"]
            warned = False
            while not dispatcher.drained:
                if not warned and time.monotonic() >= grace:
                    print('{"status":"DRAINING_WRITE_STILL_ACTIVE"}', flush=True)
                    warned = True
                await asyncio.sleep(0.05)
        finally:
            await dispatcher.close()
            if runner is not None:
                await runner.cleanup()
            listener.close()


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def admin_request(config: Config, command: str) -> int:
    endpoint = {"health": "/healthz", "drain": "/admin/drain", "stop": "/admin/shutdown"}[command]
    request = urllib.request.Request(f"http://{config.authority}{endpoint}",
                                     data=None if command == "health" else b"",
                                     method="GET" if command == "health" else "POST",
                                     headers={"Authorization": f"Bearer {config.admin_token}"})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())
    try:
        with opener.open(request, timeout=5) as response:
            payload = json.loads(response.read(65_536).decode("utf-8"), object_pairs_hook=bridge._unique_object,
                                 parse_constant=bridge._reject_constant)
            # Do not echo arbitrary response text (including reflected tokens)
            # if the configured port accidentally belongs to another service.
            if command == "health":
                if not isinstance(payload, dict) or payload.get("service") != "dungeon-shared-http":
                    raise ValueError("Unexpected health response")
                keys = ("ready", "draining", "quarantined", "worker_failure", "worker_limit",
                        "live_workers", "running", "queued", "queue_limit")
                if any(type(payload.get(key)) not in {int, bool} for key in keys):
                    raise ValueError("Unexpected health field")
                summary = {"service": "dungeon-shared-http", **{key: payload[key] for key in keys}}
            else:
                if not isinstance(payload, dict) or payload.get("status") != "DRAINING":
                    raise ValueError("Unexpected shutdown response")
                summary = {"status": "DRAINING", "stop_requested": command == "stop",
                           "writes_are_not_force_killed": True}
            print(json.dumps(summary))
            return 0
    except urllib.error.HTTPError as exc:
        print(json.dumps({"status": "UNAVAILABLE", "http_status": exc.code}))
        return 1
    except (OSError, ValueError, TypeError, urllib.error.URLError):
        print('{"status":"UNAVAILABLE"}')
        return 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("serve", "health", "drain", "stop"))
    parser.add_argument("--config", required=True, help="Absolute operator-controlled private JSON config")
    args = parser.parse_args()
    try:
        config = load_config(args.config)
        if args.command == "serve":
            asyncio.run(serve(config))
            return 0
        return admin_request(config, args.command)
    except (ValueError, TypeError, OSError, RuntimeError, ImportError):
        # Do not leak private paths, credential values or request bodies.
        print('{"status":"STARTUP_OR_CONFIGURATION_FAILED","next_steps":["Check the private config, dependency, owned-worker support, port and source-home lease."]}',
              file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
