"""Deterministic HTTP dispatcher tests; all workers and requests are fakes."""

from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import sys
import tempfile
import time
import unittest
from concurrent.futures import Executor, Future
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


MCP_DIR = Path(__file__).resolve().parents[1] / "mcp"
if str(MCP_DIR) not in sys.path:
    sys.path.insert(0, str(MCP_DIR))

# http_service imports these modules by their top-level names.
import server as bridge
import http_runtime as runtime
import http_service as service_module


class FakeMonitor:
    def done(self) -> bool:
        return False


class FakeProcess:
    def __init__(self) -> None:
        self.alive = True
        self.terminate_calls = 0
        self.kill_calls = 0
        self.start_calls = 0

    def is_alive(self) -> bool:
        return self.alive

    def start(self) -> None:
        self.start_calls += 1
        raise AssertionError("tests must never start a worker process")

    def terminate(self) -> None:
        self.terminate_calls += 1

    def kill(self) -> None:
        self.kill_calls += 1

    def join(self, timeout=None) -> None:
        return None

    def close(self) -> None:
        return None


class FakePipe:
    def __init__(self) -> None:
        self.sent: list[bytes] = []
        self.closed = False
        self.recv_error: BaseException | None = None

    def send_bytes(self, data: bytes) -> None:
        self.sent.append(data)

    def recv_bytes(self, maxlength: int | None = None) -> bytes:
        if self.recv_error is not None:
            raise self.recv_error
        return b""

    def poll(self) -> bool:
        return False

    def close(self) -> None:
        self.closed = True


class ImmediateExecutor(Executor):
    """Run only the tiny fake-pipe callbacks inline; never create threads."""

    def submit(self, fn, /, *args, **kwargs) -> Future:
        future = Future()
        try:
            future.set_result(fn(*args, **kwargs))
        except BaseException as exc:
            future.set_exception(exc)
        return future

    def shutdown(self, wait=True, *, cancel_futures=False) -> None:
        return None


class QueueThenFailExecutor(Executor):
    """Model submit failing after work may already have been enqueued."""

    def __init__(self) -> None:
        self.submissions: list[tuple[object, tuple, dict]] = []

    def submit(self, fn, /, *args, **kwargs) -> Future:
        self.submissions.append((fn, args, kwargs))
        raise RuntimeError("simulated executor submission failure")

    def shutdown(self, wait=True, *, cancel_futures=False) -> None:
        return None


class FakeSpawnProcess(FakeProcess):
    """Count Process.start without ever creating an operating-system process."""

    def start(self) -> None:
        self.start_calls += 1


class FakeContext:
    def __init__(self) -> None:
        self.parent = FakePipe()
        self.child = FakePipe()
        self.processes: list[FakeSpawnProcess] = []

    def Pipe(self, *, duplex: bool = True) -> tuple[FakePipe, FakePipe]:
        return self.parent, self.child

    def Process(self, *, target, args, name: str) -> FakeSpawnProcess:
        process = FakeSpawnProcess()
        self.processes.append(process)
        return process


def make_dispatcher(test: unittest.TestCase, root: Path, *, workers: int = 2,
                    queue_limit: int = 16, per_client_limit: int = 8,
                    per_session_limit: int = 4) -> runtime.Dispatcher:
    dispatcher = runtime.Dispatcher(
        str(root), workers=workers, queue_limit=queue_limit,
        per_client_limit=per_client_limit, per_session_limit=per_session_limit,
    )
    original_io = dispatcher.io
    dispatcher.io = ImmediateExecutor()
    original_io.shutdown(wait=True)
    dispatcher.monitor = FakeMonitor()
    dispatcher.workers = [
        runtime.Worker(FakeProcess(), FakePipe(), time.monotonic(), ready=True)
        for _ in range(workers)
    ]
    dispatchers = getattr(test, "_fake_dispatchers", None)
    if dispatchers is None:
        dispatchers = []
        setattr(test, "_fake_dispatchers", dispatchers)
    dispatchers.append(dispatcher)
    return dispatcher


def tool_request(request_id: int, name: str, root: Path) -> dict:
    return {
        "jsonrpc": "2.0", "id": request_id, "method": "tools/call",
        "params": {"name": name, "arguments": {"project_root": str(root)}},
    }


class SharedHTTPRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="dungeon-shared-http-")
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.project_a = self.base / "project-a"
        self.project_b = self.base / "project-b"
        self.cas_root = self.base / "project-cas"
        for root in (self.project_a, self.project_b, self.cas_root):
            root.mkdir()
        self._fake_dispatchers = []

    async def asyncTearDown(self) -> None:
        for dispatcher in self._fake_dispatchers:
            for job in dispatcher.jobs.values():
                if not job.future.done():
                    job.future.cancel()
            dispatcher.io.shutdown(wait=True)

    def dispatcher(self, **kwargs) -> runtime.Dispatcher:
        return make_dispatcher(self, self.base, **kwargs)

    def submit(self, dispatcher: runtime.Dispatcher, request_id: int, root: Path,
               name: str = "dungeon_project_probe", *, owner: str = "alice",
               session: str = "session-a") -> runtime.Job:
        return dispatcher.submit(
            owner, session, tool_request(request_id, name, root), str(root),
            bridge.RequestPolicy((str(root),), (str(root),) if name in bridge.WRITE_TOOL_NAMES else ()),
        )

    async def dispatch(self, dispatcher: runtime.Dispatcher) -> None:
        dispatcher._dispatch()
        # run_in_executor wraps the already-completed concurrent future in an
        # asyncio future; let its completion callback reach this event loop.
        await asyncio.sleep(0)

    async def test_queued_cancellation_releases_slot_and_completes_without_dispatch(self) -> None:
        dispatcher = self.dispatcher(workers=1, per_client_limit=1)
        job = self.submit(dispatcher, 1, self.project_a)

        dispatcher.cancel("alice", "session-a", 1)

        self.assertEqual(job.state, "CANCELLED")
        self.assertTrue(job.future.done())
        self.assertIsNone(job.future.result())
        self.assertNotIn(job, dispatcher.queue)
        self.assertEqual(dispatcher.workers[0].connection.sent, [])
        replacement = self.submit(dispatcher, 2, self.project_a)
        self.assertEqual(replacement.state, "QUEUED")

    async def test_queue_client_and_session_quotas_are_independent(self) -> None:
        queued = self.dispatcher(workers=1, queue_limit=1)
        self.submit(queued, 1, self.project_a)
        with self.assertRaisesRegex(runtime.AdmissionError, "QUEUE_FULL"):
            self.submit(queued, 2, self.project_b, owner="bob", session="session-b")

        client_limited = self.dispatcher(workers=1, per_client_limit=1)
        self.submit(client_limited, 1, self.project_a)
        with self.assertRaisesRegex(runtime.AdmissionError, "CLIENT_LIMIT"):
            self.submit(client_limited, 2, self.project_b, session="session-b")

        session_limited = self.dispatcher(workers=1, per_session_limit=1)
        self.submit(session_limited, 1, self.project_a)
        with self.assertRaisesRegex(runtime.AdmissionError, "SESSION_LIMIT"):
            self.submit(session_limited, 2, self.project_b)

    async def test_reads_for_one_project_are_serialized(self) -> None:
        dispatcher = self.dispatcher(workers=2)
        first = self.submit(dispatcher, 1, self.project_a)
        second = self.submit(dispatcher, 2, self.project_a)

        await self.dispatch(dispatcher)

        self.assertIs(dispatcher.workers[0].job, first)
        self.assertIsNone(dispatcher.workers[1].job)
        self.assertEqual(len(dispatcher.queue), 1)
        dispatcher._finish(first, {"ok": True}, "COMPLETED")
        dispatcher.workers[0].job = None
        await self.dispatch(dispatcher)
        self.assertIs(dispatcher.workers[0].job, second)

    async def test_cas_write_is_fifo_barrier_across_projects(self) -> None:
        dispatcher = self.dispatcher(workers=2)
        active_read = self.submit(dispatcher, 1, self.project_a)
        cas_write = self.submit(dispatcher, 2, self.cas_root, "dungeon_cas_put")
        later_read = self.submit(dispatcher, 3, self.project_b)

        await self.dispatch(dispatcher)

        self.assertIs(dispatcher.workers[0].job, active_read)
        self.assertIsNone(dispatcher.workers[1].job)
        self.assertEqual(list(dispatcher.queue), [cas_write, later_read])

        dispatcher._finish(active_read, {"ok": True}, "COMPLETED")
        dispatcher.workers[0].job = None
        await self.dispatch(dispatcher)
        self.assertIs(dispatcher.workers[0].job, cas_write)
        self.assertIsNone(dispatcher.workers[1].job)

        dispatcher.workers[0].job = None
        dispatcher._finish(cas_write, {"ok": True}, "COMPLETED")
        await self.dispatch(dispatcher)
        self.assertIs(dispatcher.workers[0].job, later_read)

    async def test_started_write_cancel_is_deferred_and_deadline_keeps_global_lock(self) -> None:
        dispatcher = self.dispatcher(workers=2)
        writer = self.submit(dispatcher, 1, self.cas_root, "dungeon_cas_put")
        waiting_read = self.submit(dispatcher, 2, self.project_b)
        waiting_read.deadline = writer.deadline + 30
        await self.dispatch(dispatcher)
        worker = dispatcher.workers[0]
        process = worker.process
        self.assertIs(worker.job, writer)

        dispatcher.cancel("alice", "session-a", 1)
        self.assertTrue(writer.cancel_requested)
        self.assertFalse(writer.future.done())
        self.assertEqual((process.terminate_calls, process.kill_calls), (0, 0))

        dispatcher._expire_and_prune(writer.deadline + 0.01)
        unknown = writer.future.result()
        self.assertEqual(unknown["result"]["structuredContent"]["status"], "UNKNOWN")
        self.assertTrue(writer.timed_out)
        self.assertIs(worker.job, writer)
        self.assertEqual((process.terminate_calls, process.kill_calls), (0, 0))

        await self.dispatch(dispatcher)
        self.assertIs(worker.job, writer)
        self.assertIsNone(dispatcher.workers[1].job)
        self.assertEqual(list(dispatcher.queue), [waiting_read])

    async def test_write_send_submit_failure_quarantines_unknown_without_killing_worker(self) -> None:
        dispatcher = self.dispatcher(workers=1)
        writer = self.submit(dispatcher, 1, self.cas_root, "dungeon_cas_put")
        worker = dispatcher.workers[0]
        executor = QueueThenFailExecutor()
        dispatcher.io = executor

        dispatcher._dispatch()

        self.assertEqual(len(executor.submissions), 1)
        self.assertTrue(dispatcher.failed)
        self.assertTrue(dispatcher.quarantined)
        self.assertFalse(dispatcher.ready)
        self.assertEqual(writer.state, "RUNNING")
        self.assertTrue(writer.future.done())
        self.assertEqual(
            writer.future.result()["result"]["structuredContent"]["status"], "UNKNOWN",
        )
        self.assertIs(worker.job, writer)
        self.assertEqual((worker.process.terminate_calls, worker.process.kill_calls), (0, 0))

    async def test_spawn_owns_and_retires_process_if_initial_receive_submit_fails(self) -> None:
        dispatcher = self.dispatcher(workers=1)
        dispatcher.workers = [None]
        context = FakeContext()
        dispatcher.context = context
        executor = QueueThenFailExecutor()
        dispatcher.io = executor

        with self.assertRaisesRegex(RuntimeError, "simulated executor submission failure"):
            dispatcher._spawn(0)

        worker = dispatcher.workers[0]
        self.assertIsNotNone(worker)
        self.assertIs(worker.connection, context.parent)
        self.assertIs(worker.process, context.processes[0])
        self.assertEqual(worker.process.start_calls, 1)
        self.assertTrue(context.child.closed)
        self.assertEqual(len(executor.submissions), 1)
        self.assertEqual(executor.submissions[0][0], context.parent.recv_bytes)
        self.assertTrue(dispatcher.failed)
        self.assertIsNotNone(worker.retiring)
        self.assertEqual((worker.process.terminate_calls, worker.process.kill_calls), (1, 0))

    async def test_operation_records_are_bound_to_owner_and_session(self) -> None:
        dispatcher = self.dispatcher(workers=1)
        job = self.submit(dispatcher, 7, self.project_a, owner="alice", session="s-a")

        self.assertEqual(dispatcher.operation(job.operation_id, "alice", "s-a")["operation_id"], job.operation_id)
        self.assertIsNone(dispatcher.operation(job.operation_id, "bob", "s-a"))
        self.assertIsNone(dispatcher.operation(job.operation_id, "alice", "s-b"))

    async def test_operation_id_collisions_retry_then_fail_without_overwrite(self) -> None:
        dispatcher = self.dispatcher(workers=1)
        first = self.submit(dispatcher, 1, self.project_a)
        unique = "f" * 32 if first.operation_id != "f" * 32 else "e" * 32
        with patch.object(runtime.uuid, "uuid4", side_effect=[
            SimpleNamespace(hex=first.operation_id), SimpleNamespace(hex=unique),
        ]) as generate:
            second = self.submit(dispatcher, 2, self.project_b)
        self.assertEqual(generate.call_count, 2)
        self.assertEqual(second.operation_id, unique)
        with patch.object(runtime.uuid, "uuid4", return_value=SimpleNamespace(hex=first.operation_id)) as generate:
            with self.assertRaisesRegex(runtime.AdmissionError, "OPERATION_ID_ALLOCATION_FAILED"):
                self.submit(dispatcher, 3, self.project_b)
        self.assertEqual(generate.call_count, 4)
        self.assertTrue(dispatcher.failed)
        self.assertFalse(dispatcher.ready)
        self.assertIs(dispatcher.jobs[first.operation_id], first)
        self.assertIs(dispatcher.jobs[second.operation_id], second)
        self.assertEqual(len(dispatcher.jobs), 2)

    def test_request_policy_is_exact_and_context_scoped(self) -> None:
        policy = bridge.RequestPolicy((str(self.project_a),), (str(self.project_a),))

        with bridge.request_policy(policy):
            self.assertEqual(bridge._selected_root({"project_root": str(self.project_a)}), self.project_a)
            with self.assertRaises(PermissionError):
                bridge._selected_root({"project_root": str(self.project_b)})
            self.assertEqual(bridge._writable_roots(), {self.project_a})

        self.assertIsNone(bridge._REQUEST_POLICY.get())


class FakeHeaders:
    def __init__(self, values: dict[str, object]):
        self.values = {name.casefold(): value for name, value in values.items()}

    def get(self, name: str, default=None):
        value = self.values.get(name.casefold(), default)
        return value[0] if isinstance(value, (list, tuple)) and value else value

    def getall(self, name: str, default=None):
        value = self.values.get(name.casefold())
        if value is None:
            return [] if default is None else default
        return list(value) if isinstance(value, (list, tuple)) else [value]


class SharedHTTPConfigTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="dungeon-http-config-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.path = self.root / "config.json"
        self.data = {
            "schema_version": 1, "home_root": str(MCP_DIR.parents[3]),
            "admin_token_env_var": "DUNGEON_HTTP_TEST_ADMIN",
            "clients": [{"id": "fixture", "token_env_var": "DUNGEON_HTTP_TEST_CLIENT",
                         "project_roots": [str(self.root)]}],
        }
        environment = patch.dict(os.environ, {
            "DUNGEON_HTTP_TEST_ADMIN": "a" * 40, "DUNGEON_HTTP_TEST_CLIENT": "b" * 40,
        })
        environment.start()
        self.addCleanup(environment.stop)

    def load(self, data=None):
        self.path.write_text(json.dumps(self.data if data is None else data), encoding="utf-8")
        return service_module.load_config(self.path)

    def test_defaults_are_read_only_and_secrets_are_not_in_repr(self):
        config = self.load()
        self.assertEqual(config.limits, service_module.DEFAULTS)
        self.assertEqual(config.clients[0].policy.writable_roots, ())
        self.assertNotIn("a" * 40, repr(config))
        self.assertNotIn("b" * 40, repr(config))

    def test_every_numeric_limit_rejects_out_of_bounds_and_booleans(self):
        for name, (lower, upper) in service_module.LIMITS.items():
            for value in (lower - 1, upper + 1, True):
                with self.subTest(name=name, value=value):
                    with self.assertRaisesRegex(ValueError, "Invalid bounded setting"):
                        self.load({**self.data, name: value})

    def test_duplicate_unknown_fields_and_foreign_home_are_rejected(self):
        self.path.write_text('{"schema_version":1,"schema_version":1}', encoding="utf-8")
        with self.assertRaises(ValueError):
            service_module.load_config(self.path)
        with self.assertRaises(ValueError):
            self.load({**self.data, "unknown": True})
        with self.assertRaisesRegex(ValueError, "full source clone"):
            self.load({**self.data, "home_root": str(self.root)})

    def test_credentials_must_be_dedicated_long_and_distinct(self):
        with self.assertRaisesRegex(ValueError, "dedicated DUNGEON_HTTP_"):
            self.load({**self.data, "admin_token_env_var": "PATH"})
        for token in ("short", "a" * 40):
            with patch.dict(os.environ, {"DUNGEON_HTTP_TEST_CLIENT": token}):
                with self.assertRaises(ValueError):
                    self.load()

    def test_paths_and_writable_roots_fail_closed(self):
        for changes in ({"project_roots": ["relative"]}, {"input_roots": ["relative"]},
                        {"writable_roots": [str(MCP_DIR.parents[3])]}):
            row = {**self.data["clients"][0], **changes}
            with self.subTest(changes=changes):
                with self.assertRaises((ValueError, PermissionError)):
                    self.load({**self.data, "clients": [row]})


class FakeRequest:
    def __init__(self, method: str, path: str, headers: dict[str, object], body: bytes = b""):
        self.method = method
        self.path = path
        self.headers = FakeHeaders(headers)
        self.body = body
        self.content_length = len(body)
        content_type = self.headers.get("Content-Type", "application/json")
        self.content_type = content_type.split(";", 1)[0].strip().lower()
        self.charset = "utf-8"
        self.remote = "127.0.0.1"
        self.transport = object()

    async def read(self) -> bytes:
        return self.body


@unittest.skipUnless(importlib.util.find_spec("aiohttp"), "aiohttp is optional for the shared HTTP service")
class SharedHTTPServiceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="dungeon-shared-http-service-")
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.project = self.base / "project"
        self.project.mkdir()
        self.dispatcher = make_dispatcher(self, self.base, workers=1)
        self.policy = bridge.RequestPolicy((str(self.project),), ())
        self.alice = service_module.Client("alice", "a" * 40, self.policy)
        self.bob = service_module.Client("bob", "b" * 40, self.policy)
        self.config = service_module.Config(
            str(self.base), (self.alice, self.bob), "c" * 40,
            dict(service_module.DEFAULTS),
        )
        self.service = service_module.Service(self.config, self.dispatcher)

    async def asyncTearDown(self) -> None:
        for job in self.dispatcher.jobs.values():
            if not job.future.done():
                job.future.cancel()
        self.dispatcher.io.shutdown(wait=True)

    def make_request(self, *, method: str = "POST", path: str = "/mcp",
                     body: dict | None = None, token: str | None = None,
                     headers: dict[str, object] | None = None) -> FakeRequest:
        values: dict[str, object] = {
            "Host": self.config.authority,
            "Authorization": f"Bearer {token or self.alice.token}",
            "Origin": f"http://{self.config.authority}",
            "Accept": "application/json, text/event-stream",
            "Content-Type": "application/json",
        }
        values.update(headers or {})
        raw = b"" if body is None else json.dumps(body).encode("utf-8")
        return FakeRequest(method, path, values, raw)

    async def initialize(self) -> tuple[str, str]:
        request = self.make_request(body={
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {
                "protocolVersion": bridge.PROTOCOL_VERSION,
                "capabilities": {}, "clientInfo": {"name": "test", "version": "1"},
            },
        })
        response = await self.service.handle(request)
        self.assertEqual(response.status, 200)
        return response.headers["MCP-Session-Id"], response.headers["MCP-Protocol-Version"]

    def session_headers(self, session_id: str, protocol: str) -> dict[str, object]:
        return {"MCP-Session-Id": session_id, "MCP-Protocol-Version": protocol}

    async def send_rpc(self, session_id: str, protocol: str, rpc: dict,
                       *, token: str | None = None):
        return await self.service.handle(self.make_request(
            body=rpc, token=token, headers=self.session_headers(session_id, protocol),
        ))

    def response_json(self, response) -> dict:
        return json.loads(response.body.decode("utf-8"))

    async def test_auth_origin_and_duplicate_boundary_headers(self) -> None:
        unauthorized = await self.service.handle(self.make_request(token="x" * 40))
        self.assertEqual(unauthorized.status, 401)

        foreign_origin = await self.service.handle(self.make_request(
            headers={"Origin": "https://example.invalid"},
        ))
        self.assertEqual(foreign_origin.status, 403)

        duplicated_origin = await self.service.handle(self.make_request(
            headers={"Origin": [f"http://{self.config.authority}"] * 2},
        ))
        self.assertEqual(duplicated_origin.status, 403)

    async def test_handshake_initialization_duplicate_ids_and_session_owner(self) -> None:
        session_id, protocol = await self.initialize()
        self.assertEqual(protocol, bridge.PROTOCOL_VERSION)

        repeated_initialize = await self.service.handle(self.make_request(
            body={"jsonrpc": "2.0", "id": 9, "method": "initialize", "params": {
                "protocolVersion": protocol, "capabilities": {}, "clientInfo": {},
            }},
            headers=self.session_headers(session_id, protocol),
        ))
        self.assertEqual(repeated_initialize.status, 400)

        early_ping = await self.send_rpc(session_id, protocol, {
            "jsonrpc": "2.0", "id": 2, "method": "ping",
        })
        self.assertEqual(self.response_json(early_ping)["error"]["code"], -32000)

        initialized = await self.send_rpc(session_id, protocol, {
            "jsonrpc": "2.0", "method": "notifications/initialized",
        })
        self.assertEqual(initialized.status, 202)

        ping = {"jsonrpc": "2.0", "id": 2, "method": "ping"}
        self.assertEqual((await self.send_rpc(session_id, protocol, ping)).status, 200)
        duplicate = await self.send_rpc(session_id, protocol, ping)
        self.assertEqual(duplicate.status, 400)
        self.assertIn("Duplicate request ID", self.response_json(duplicate)["error"]["message"])

        other_owner = await self.send_rpc(
            session_id, protocol,
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            token=self.bob.token,
        )
        self.assertEqual(other_owner.status, 404)

    async def test_session_id_collisions_retry_then_fail_without_overwrite(self) -> None:
        with patch.object(service_module.secrets, "token_urlsafe", return_value="first-session"):
            first_id, _ = await self.initialize()
        first = self.service.sessions[first_id]
        with patch.object(service_module.secrets, "token_urlsafe", side_effect=[
            first_id, "second-session",
        ]) as generate:
            second_id, _ = await self.initialize()
        self.assertEqual(generate.call_count, 2)
        self.assertEqual(second_id, "second-session")
        with patch.object(service_module.secrets, "token_urlsafe", return_value=first_id) as generate:
            response = await self.service.handle(self.make_request(body={
                "jsonrpc": "2.0", "id": 1, "method": "initialize",
                "params": {"protocolVersion": bridge.PROTOCOL_VERSION, "capabilities": {}, "clientInfo": {}},
            }))
        self.assertEqual(response.status, 503)
        self.assertEqual(generate.call_count, 4)
        self.assertIs(self.service.sessions[first_id], first)
        self.assertEqual(len(self.service.sessions), 2)

    async def test_http_operations_are_owner_and_session_scoped(self) -> None:
        job = self.dispatcher.submit(
            "alice", "session-a", tool_request(5, "dungeon_project_probe", self.project),
            str(self.project), self.policy,
        )
        path = f"/operations/{job.operation_id}"

        own = await self.service.handle(self.make_request(
            method="GET", path=path, headers={"MCP-Session-Id": "session-a"},
        ))
        self.assertEqual(own.status, 200)
        self.assertEqual(self.response_json(own)["operation_id"], job.operation_id)

        other_session = await self.service.handle(self.make_request(
            method="GET", path=path, headers={"MCP-Session-Id": "session-b"},
        ))
        self.assertEqual(other_session.status, 404)
        other_owner = await self.service.handle(self.make_request(
            method="GET", path=path, token=self.bob.token,
            headers={"MCP-Session-Id": "session-a"},
        ))
        self.assertEqual(other_owner.status, 404)

    async def test_tool_calls_require_explicit_root_and_write_capability(self) -> None:
        session_id, protocol = await self.initialize()
        await self.send_rpc(session_id, protocol, {
            "jsonrpc": "2.0", "method": "notifications/initialized",
        })

        missing_root = await self.send_rpc(session_id, protocol, {
            "jsonrpc": "2.0", "id": 2, "method": "tools/call",
            "params": {"name": "dungeon_project_probe", "arguments": {}},
        })
        self.assertEqual(missing_root.status, 403)

        read_only_write = await self.send_rpc(session_id, protocol, {
            "jsonrpc": "2.0", "id": 3, "method": "tools/call",
            "params": {"name": "dungeon_cas_put", "arguments": {"project_root": str(self.project)}},
        })
        self.assertEqual(read_only_write.status, 403)
        self.assertEqual(self.dispatcher.jobs, {})

        original_result = self.dispatcher.result
        result_jobs = []

        async def immediate_result(job):
            result_jobs.append(job)
            return {"jsonrpc": "2.0", "id": job.request_id, "result": {"fake": True}}

        self.dispatcher.result = immediate_result
        allowed_read = await self.send_rpc(session_id, protocol, {
            "jsonrpc": "2.0", "id": 4, "method": "tools/call",
            "params": {"name": "dungeon_project_probe", "arguments": {"project_root": str(self.project)}},
        })
        self.dispatcher.result = original_result

        self.assertEqual(allowed_read.status, 200)
        self.assertEqual(allowed_read.headers["Dungeon-Operation-Id"], result_jobs[0].operation_id)
        self.assertEqual(result_jobs[0].root, os.path.normcase(str(self.project)))


if __name__ == "__main__":
    unittest.main()
