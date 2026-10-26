"""Bounded, process-isolated executor for the optional shared HTTP transport.

No executor belongs to a chat. Workers run one invocation at a time, reuse the
stdio bridge, and never receive a caller's environment or working directory.
All methods of Dispatcher are called on its owning asyncio event loop.
"""

from __future__ import annotations

import asyncio
import ctypes
import json
import multiprocessing as mp
import os
import signal
import threading
import time
import uuid
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from typing import Any

import server as bridge


MAX_IPC_BYTES = 1_200_000


def _own_process_tree():
    """Only this newly spawned worker and its future descendants are owned."""
    if os.name != "nt":
        os.setsid()
        return None
    from ctypes import wintypes as wt

    class BasicLimits(ctypes.Structure):
        _fields_ = [("process_time", ctypes.c_int64), ("job_time", ctypes.c_int64),
                    ("flags", wt.DWORD), ("min_working_set", ctypes.c_size_t),
                    ("max_working_set", ctypes.c_size_t), ("active_processes", wt.DWORD),
                    ("affinity", ctypes.c_size_t), ("priority", wt.DWORD), ("scheduling", wt.DWORD)]

    class IoCounters(ctypes.Structure):
        _fields_ = [(name, ctypes.c_uint64) for name in
                    ("read_ops", "write_ops", "other_ops", "read_bytes", "write_bytes", "other_bytes")]

    class ExtendedLimits(ctypes.Structure):
        _fields_ = [("basic", BasicLimits), ("io", IoCounters),
                    ("process_memory", ctypes.c_size_t), ("job_memory", ctypes.c_size_t),
                    ("peak_process_memory", ctypes.c_size_t), ("peak_job_memory", ctypes.c_size_t)]

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p, wt.LPCWSTR]
    kernel.CreateJobObjectW.restype = wt.HANDLE
    kernel.GetCurrentProcess.restype = wt.HANDLE
    kernel.SetInformationJobObject.argtypes = [wt.HANDLE, ctypes.c_int, ctypes.c_void_p, wt.DWORD]
    kernel.SetInformationJobObject.restype = wt.BOOL
    kernel.AssignProcessToJobObject.argtypes = [wt.HANDLE, wt.HANDLE]
    kernel.AssignProcessToJobObject.restype = wt.BOOL
    kernel.CloseHandle.argtypes = [wt.HANDLE]
    handle = kernel.CreateJobObjectW(None, None)
    if not handle:
        raise OSError("Cannot create owned worker Job Object")
    limits = ExtendedLimits()
    limits.basic.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    if not kernel.SetInformationJobObject(handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
        kernel.CloseHandle(handle)
        raise OSError("Cannot set owned worker Job Object limits")
    if not kernel.AssignProcessToJobObject(handle, kernel.GetCurrentProcess()):
        kernel.CloseHandle(handle)
        raise OSError("Cannot attach owned worker to Job Object")
    # Keep the non-inheritable handle alive until OS process exit. Terminating
    # a read worker closes it and also terminates its own spawned VCS children.
    return handle


def _encode(value: Any) -> bytes:
    encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")
    if len(encoded) > MAX_IPC_BYTES:
        raise ValueError("Bounded worker message is too large")
    return encoded


def _decode(data: bytes) -> Any:
    return json.loads(data.decode("utf-8"), object_pairs_hook=bridge._unique_object,
                      parse_constant=bridge._reject_constant)


def _worker_main(connection, home_root: str) -> None:
    guard = None
    try:
        guard = _own_process_tree()
        # Fixed once at worker startup; never changed for a project/client.
        os.environ["DUNGEON_PROJECT_ROOT"] = home_root
        os.environ["DUNGEON_WRITABLE_ROOTS"] = "[]"
        bridge._backend_tools_path()
        connection.send_bytes(_encode({"ready": True}))
        while True:
            packet = _decode(connection.recv_bytes(MAX_IPC_BYTES))
            if packet.get("shutdown") is True:
                return
            policy = bridge.RequestPolicy(**{key: tuple(value) for key, value in packet["policy"].items()})
            with bridge.request_policy(policy):
                response = bridge._handle(packet["request"])
            connection.send_bytes(_encode({"operation_id": packet["operation_id"], "response": response}))
    except (EOFError, BrokenPipeError):
        return
    except BaseException:
        # No error messages, request bodies or credentials on worker stderr.
        # Parent treats a mutation's unexpected exit as UNKNOWN, not rollback.
        try:
            connection.send_bytes(_encode({"worker_failed": True}))
        except (OSError, ValueError):
            pass
    finally:
        connection.close()
        # The OS, not CloseHandle during an active operation, owns teardown.
        _ = guard


class AdmissionError(Exception):
    pass


@dataclass
class Job:
    operation_id: str
    owner: str
    session: str
    request: dict[str, Any]
    root: str
    write: bool
    policy: bridge.RequestPolicy
    deadline: float
    future: asyncio.Future
    state: str = "QUEUED"
    started: float | None = None
    finished: float | None = None
    cancel_requested: bool = False
    timed_out: bool = False
    response: dict[str, Any] | None = None
    request_id: Any = field(init=False)

    def __post_init__(self):
        self.request_id = self.request["id"]


@dataclass
class Worker:
    process: Any
    connection: Any
    born: float
    ready: bool = False
    job: Job | None = None
    retiring: float | None = None
    incoming: asyncio.Future | None = None
    outgoing: asyncio.Future | None = None


def job_error(job: Job, code: str, *, unknown: bool = False) -> dict[str, Any]:
    payload = {
        "status": "UNKNOWN" if unknown else "FAIL",
        "operation_id": job.operation_id,
        "issues": [{"code": code, "severity": "ERROR",
                    "message": "The write may still be running or have committed; do not retry."
                    if unknown else "The bounded service could not complete this invocation."}],
        "next_steps": ["Inspect /operations/<operation_id> with the original authenticated session."
                       if unknown else "Check service readiness and the operation status before retrying."],
    }
    return {"jsonrpc": "2.0", "id": job.request_id, "result": bridge._tool_result(payload, False)}


class Dispatcher:
    def __init__(self, home_root: str, *, workers: int = 2, queue_limit: int = 16,
                 request_timeout: float = 45, startup_timeout: float = 15,
                 per_client_limit: int = 8, per_session_limit: int = 4,
                 history_limit: int = 32, history_ttl: float = 300):
        self.home_root = home_root
        self.worker_count = workers
        self.queue_limit = queue_limit
        self.request_timeout = request_timeout
        self.startup_timeout = startup_timeout
        self.per_client_limit = per_client_limit
        self.per_session_limit = per_session_limit
        self.history_limit = history_limit
        self.history_ttl = history_ttl
        self.context = mp.get_context("spawn")
        # Pipe reads/writes must never block health, cancellation or deadlines
        # on the HTTP event loop. This pool is fixed, not per client/request.
        self.io = ThreadPoolExecutor(max_workers=workers * 2, thread_name_prefix="dungeon-ipc")
        self.io_closed = False
        self.workers: list[Worker | None] = [None] * workers
        self.queue: deque[Job] = deque()
        self.jobs: dict[str, Job] = {}
        self.restarts: deque[float] = deque()
        self.stopping = False
        self.quarantined = False
        self.failed = False
        self.monitor: asyncio.Task | None = None

    @property
    def ready(self) -> bool:
        return (self.monitor is not None and not self.monitor.done() and
                not (self.stopping or self.quarantined or self.failed)) and all(
            worker is not None and worker.ready and worker.retiring is None and worker.process.is_alive()
            for worker in self.workers)

    def _spawn(self, index: int) -> None:
        parent, child = self.context.Pipe(duplex=True)
        process = self.context.Process(target=_worker_main, args=(child, self.home_root),
                                       name=f"dungeon-http-worker-{index}")
        worker = Worker(process, parent, time.monotonic())
        try:
            process.start()
        except BaseException:
            parent.close()
            child.close()
            raise
        self.workers[index] = worker
        try:
            child.close()
            worker.incoming = asyncio.get_running_loop().run_in_executor(self.io, parent.recv_bytes, MAX_IPC_BYTES)
        except BaseException:
            # Register ownership BEFORE any potentially failing IO submission.
            # start()/monitor cleanup can now reap this newly spawned process.
            self.failed = True
            self._retire(worker)
            raise

    async def start(self) -> None:
        try:
            # Fail thread-allocation/resource pressure before accepting any
            # request or starting a mutator. submit() can queue work before a
            # thread-start failure, so these startup tasks only wait on a gate
            # that is always released, even if a later submission fails.
            gate = threading.Event()
            warmup = []
            try:
                for _ in range(self.worker_count * 2):
                    warmup.append(asyncio.get_running_loop().run_in_executor(self.io, gate.wait))
            finally:
                gate.set()
            await asyncio.gather(*warmup)
            for index in range(self.worker_count):
                self._spawn(index)
            self.monitor = asyncio.create_task(self._monitor(), name="dungeon-http-dispatcher")
            deadline = time.monotonic() + self.startup_timeout
            while not self.ready:
                if self.failed or self.quarantined or time.monotonic() >= deadline:
                    raise RuntimeError("Owned worker pool did not become ready")
                await asyncio.sleep(0.025)
        except BaseException:
            await self.close()
            raise

    def submit(self, owner: str, session: str, request: dict[str, Any], root: str,
               policy: bridge.RequestPolicy) -> Job:
        if not self.ready:
            raise AdmissionError("SERVICE_NOT_READY")
        active = [job for job in self.jobs.values() if job.finished is None]
        if len(self.queue) >= self.queue_limit:
            raise AdmissionError("QUEUE_FULL")
        if sum(job.owner == owner for job in active) >= self.per_client_limit:
            raise AdmissionError("CLIENT_LIMIT")
        if sum(job.session == session for job in active) >= self.per_session_limit:
            raise AdmissionError("SESSION_LIMIT")
        for _ in range(4):
            operation_id = uuid.uuid4().hex
            if operation_id not in self.jobs:
                break
        else:
            self.failed = True
            raise AdmissionError("OPERATION_ID_ALLOCATION_FAILED")
        job = Job(operation_id, owner, session, request, os.path.normcase(root),
                  request["params"]["name"] in bridge.WRITE_TOOL_NAMES, policy,
                  time.monotonic() + self.request_timeout, asyncio.get_running_loop().create_future())
        self.jobs[job.operation_id] = job
        self.queue.append(job)
        return job

    async def result(self, job: Job) -> dict[str, Any] | None:
        # Independent of the monitor's healthy deadline handling. If the
        # supervisor itself faults, callers still receive a bounded UNKNOWN
        # rather than waiting forever while readiness incorrectly stays green.
        try:
            return await asyncio.wait_for(asyncio.shield(job.future),
                                          max(0.1, job.deadline - time.monotonic() + 1))
        except asyncio.TimeoutError:
            self.failed = True
            unknown = job.write and job.state != "QUEUED"
            if unknown:
                self.quarantined = True
            response = job_error(job, "SUPERVISOR_UNAVAILABLE", unknown=unknown)
            if job.state == "QUEUED":
                self.queue.remove(job)
                self._finish(job, response, "NOT_STARTED")
            else:
                if not job.future.done():
                    job.future.set_result(response)
                if not job.write:
                    for worker in self.workers:
                        if worker is not None and worker.job is job:
                            self._retire(worker)
            return response

    def _finish(self, job: Job, response: dict[str, Any] | None, state: str) -> None:
        job.state = state
        job.response = response
        job.finished = time.monotonic()
        job.request = {}  # Do not retain argument bodies in history.
        if not job.future.done():
            job.future.set_result(response)

    def _retire(self, worker: Worker) -> None:
        if worker.retiring is not None:
            return
        if worker.job is not None and worker.job.write:
            # An IPC fault is not proof that the mutator has stopped. Fence
            # admissions and wait for its actual process exit/result instead.
            self.quarantined = True
            if not worker.job.future.done():
                worker.job.future.set_result(job_error(worker.job, "WRITE_TRANSPORT_UNKNOWN", unknown=True))
            return
        worker.retiring = time.monotonic()
        try:
            if os.name != "nt" and worker.ready:
                os.killpg(worker.process.pid, signal.SIGTERM)
            elif worker.process.is_alive():
                worker.process.terminate()
        except ProcessLookupError:
            pass
        except OSError:
            self.failed = True

    def cancel(self, owner: str, session: str, request_id: Any) -> None:
        for job in list(self.jobs.values()):
            if job.owner != owner or job.session != session or job.request_id != request_id or job.finished is not None:
                continue
            job.cancel_requested = True
            if job.state == "QUEUED":
                self.queue.remove(job)
                self._finish(job, None, "CANCELLED")
            elif not job.write:
                for worker in self.workers:
                    if worker is not None and worker.job is job:
                        job.state = "CANCELLING"
                        if not job.future.done():
                            job.future.set_result(None)
                        self._retire(worker)
            # Started writes deliberately keep their worker and exclusion lock.

    def cancel_session(self, owner: str, session: str) -> None:
        for job in list(self.jobs.values()):
            if job.owner == owner and job.session == session and job.finished is None:
                self.cancel(owner, session, job.request_id)

    def operation(self, operation_id: str, owner: str, session: str) -> dict[str, Any] | None:
        job = self.jobs.get(operation_id)
        if job is None or job.owner != owner or job.session != session:
            return None
        return {"operation_id": job.operation_id, "state": job.state, "write": job.write,
                "cancel_requested": job.cancel_requested, "deadline_exceeded": job.timed_out,
                "response": job.response if job.finished is not None else None}

    def operation_for_request(self, request_id: Any, owner: str, session: str) -> dict[str, Any] | None:
        for job in self.jobs.values():
            if job.request_id == request_id and job.owner == owner and job.session == session:
                return self.operation(job.operation_id, owner, session)
        return None

    def status(self) -> dict[str, Any]:
        return {"ready": self.ready, "draining": self.stopping, "quarantined": self.quarantined,
                "worker_failure": self.failed, "worker_limit": self.worker_count,
                "live_workers": sum(w is not None and w.process.is_alive() for w in self.workers),
                "running": sum(w is not None and w.job is not None for w in self.workers),
                "queued": len(self.queue), "queue_limit": self.queue_limit}

    def begin_shutdown(self) -> None:
        self.stopping = True
        while self.queue:
            job = self.queue.popleft()
            self._finish(job, job_error(job, "SERVICE_DRAINING"), "NOT_STARTED")
        for worker in self.workers:
            if worker is not None and worker.job is not None and not worker.job.write:
                self.cancel(worker.job.owner, worker.job.session, worker.job.request_id)

    @property
    def drained(self) -> bool:
        return not self.queue and all(w is None or w.job is None for w in self.workers)

    async def close(self) -> None:
        self.begin_shutdown()
        if (self.monitor is None or self.monitor.done()) and any(self.workers):
            # Reap in quarantine only: never restart admissions or replay jobs.
            self.failed = True
            if self.monitor is not None and not self.monitor.cancelled():
                self.monitor.exception()
            self.monitor = asyncio.create_task(self._monitor(), name="dungeon-http-drain")
        # There is intentionally no forced kill deadline for a mutator. The
        # HTTP service stays unready/draining until a started write settles.
        while not self.drained:
            await asyncio.sleep(0.025)
        for worker in self.workers:
            if worker is not None:
                self._retire(worker)  # Idle only: no operation can be torn down.
        if self.monitor is not None:
            await self.monitor
        if not self.io_closed:
            # Every slot has been reaped and both IPC futures have settled.
            self.io.shutdown(wait=True)
            self.io_closed = True

    def _expire_and_prune(self, now: float) -> None:
        for job in list(self.queue):
            if self.failed or self.quarantined:
                self.queue.remove(job)
                self._finish(job, job_error(job, "SERVICE_QUARANTINED_OR_UNAVAILABLE"), "NOT_STARTED")
            elif now >= job.deadline:
                self.queue.remove(job)
                job.timed_out = True
                self._finish(job, job_error(job, "QUEUE_DEADLINE_EXCEEDED"), "NOT_STARTED")
        for worker in self.workers:
            if worker is None or worker.job is None:
                continue
            job = worker.job
            if now >= job.deadline and not job.timed_out:
                job.timed_out = True
                if not job.future.done():
                    job.future.set_result(job_error(job, "WRITE_OUTCOME_UNKNOWN" if job.write else "READ_DEADLINE_EXCEEDED",
                                                   unknown=job.write))
                if not job.write:
                    job.state = "CANCELLING"
                    self._retire(worker)
        finished = sorted((j for j in self.jobs.values() if j.finished is not None), key=lambda j: j.finished)
        for job in finished:
            if len(finished) > self.history_limit or now - job.finished > self.history_ttl:
                self.jobs.pop(job.operation_id, None)
                finished = finished[1:]
            else:
                break

    def _dispatch(self) -> None:
        if not self.ready:
            return
        running = [w.job for w in self.workers if w is not None and w.job is not None]
        if any(job.write for job in running):
            return
        busy_roots = {job.root for job in running}
        for job in list(self.queue):
            # A writer is a FIFO barrier across ALL projects/CAS stores. Do not
            # bypass it with later reads and starve the mutation indefinitely.
            if job.write and running:
                break
            if job.root in busy_roots:
                continue
            worker = next((w for w in self.workers if w is not None and w.job is None and
                           (w.outgoing is None or w.outgoing.done())), None)
            if worker is None:
                break
            try:
                packet = _encode({"operation_id": job.operation_id, "request": job.request,
                                  "policy": asdict(job.policy)})
            except (ValueError, TypeError, RecursionError):
                self.queue.remove(job)
                self._finish(job, job_error(job, "INVALID_WORKER_MESSAGE"), "NOT_STARTED")
                continue
            self.queue.remove(job)
            worker.job = job
            job.state = "RUNNING"
            job.started = time.monotonic()
            try:
                worker.outgoing = asyncio.get_running_loop().run_in_executor(
                    self.io, worker.connection.send_bytes, packet)
            except (OSError, ValueError, RuntimeError):
                # An executor failure may happen after internal enqueue. Do
                # not assert NOT_STARTED or replay a potentially sent write.
                self.failed = True
                self._retire(worker)
                break
            running.append(job)
            busy_roots.add(job.root)
            if job.write:
                break

    async def _monitor(self) -> None:
        while True:
            now = time.monotonic()
            for index, worker in enumerate(self.workers):
                if worker is None:
                    continue
                try:
                    if worker.outgoing is not None and worker.outgoing.done():
                        outgoing = worker.outgoing
                        worker.outgoing = None
                        outgoing.result()
                    if worker.incoming is not None and worker.incoming.done():
                        incoming = worker.incoming
                        worker.incoming = None
                        packet = _decode(incoming.result())
                        if not isinstance(packet, dict):
                            raise ValueError("Invalid worker envelope")
                        if packet.get("ready") is True and worker.job is None:
                            worker.ready = True
                        elif worker.job is not None and packet.get("operation_id") == worker.job.operation_id:
                            job = worker.job
                            self._finish(job, packet.get("response"), "COMPLETED")
                            worker.job = None
                        else:
                            self._retire(worker)
                        if worker.retiring is None:
                            worker.incoming = asyncio.get_running_loop().run_in_executor(
                                self.io, worker.connection.recv_bytes, MAX_IPC_BYTES)
                except RuntimeError:
                    self.failed = True
                    self._retire(worker)
                except (EOFError, OSError, ValueError, TypeError):
                    self._retire(worker)
                if not worker.ready and now - worker.born > self.startup_timeout:
                    self.failed = True
                    self._retire(worker)
                if worker.retiring is not None and worker.process.is_alive() and now - worker.retiring > 0.5:
                    try:
                        if os.name != "nt" and worker.ready:
                            os.killpg(worker.process.pid, signal.SIGKILL)
                        else:
                            worker.process.kill()
                    except ProcessLookupError:
                        pass
                    except OSError:
                        self.failed = True
                if worker.process.is_alive():
                    continue
                tree_cleanup_known = True
                if os.name != "nt" and worker.ready:
                    # Parent exit alone does not prove its VCS subprocesses
                    # exited. Reap only this worker's separately owned group.
                    try:
                        os.killpg(worker.process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    except OSError:
                        self.failed = True
                        tree_cleanup_known = False
                if any(f is not None and not f.done() for f in (worker.incoming, worker.outgoing)):
                    # EOF/BrokenPipe unblocks owned IPC threads. Do not close a
                    # live reader's handle or spawn its replacement prematurely.
                    continue
                if worker.job is not None:
                    job = worker.job
                    if job.write:
                        self.quarantined = True
                        self._finish(job, job_error(job, "WRITE_WORKER_EXITED_UNKNOWN", unknown=True), "UNKNOWN")
                    else:
                        if not tree_cleanup_known:
                            self._finish(job, job_error(job, "READ_TREE_CLEANUP_UNKNOWN"), "FAILED")
                        else:
                            response = None if job.cancel_requested else job_error(job, "READ_WORKER_EXITED")
                            self._finish(job, response, "CANCELLED" if job.cancel_requested else "FAILED")
                worker.connection.close()
                worker.process.join(timeout=0)
                worker.process.close()
                self.workers[index] = None
                if not (self.stopping or self.quarantined or self.failed):
                    while self.restarts and now - self.restarts[0] > 60:
                        self.restarts.popleft()
                    if len(self.restarts) >= 3:
                        self.failed = True
                    else:
                        self.restarts.append(now)
                        try:
                            self._spawn(index)
                        except Exception:
                            self.failed = True
            self._expire_and_prune(now)
            if not self.stopping:
                self._dispatch()
            if self.stopping and all(w is None for w in self.workers):
                return
            await asyncio.sleep(0.025)
