"""Real spawned-worker lifecycle tests with a controlled, synthetic backend.

These exercise OS/IPC ownership, NOT real Dungeon write/crash atomicity. They
never open a user project or CAS. Run outside socket-blocking Windows sandboxes.
"""
from __future__ import annotations

import asyncio
import ctypes
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

MCP_DIR = Path(__file__).resolve().parents[1] / "mcp"
sys.path.insert(0, str(MCP_DIR))
import http_runtime as runtime
import server as bridge


def controlled_worker(connection, home_root):
    guard = runtime._own_process_tree()
    try:
        connection.send_bytes(runtime._encode({"ready": True}))
        while True:
            packet = runtime._decode(connection.recv_bytes(runtime.MAX_IPC_BYTES))
            args = packet["request"]["params"]["arguments"]
            mode = args.get("fixture_mode", "echo")
            if mode == "crash":
                return
            child = None
            if mode == "child":
                child = subprocess.Popen(
                    [sys.executable, "-B", "-c", "import time; time.sleep(20)"],
                    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
                Path(args["marker"]).write_text(str(child.pid), encoding="ascii")
            time.sleep(args.get("fixture_delay", 0))
            if mode == "write":
                Path(args["marker"]).write_text("finished", encoding="ascii")
            if child is not None:
                child.wait(timeout=25)
            connection.send_bytes(runtime._encode({
                "operation_id": packet["operation_id"],
                "response": {"jsonrpc": "2.0", "id": packet["request"]["id"],
                             "result": {"fixture": True}},
            }))
    except (EOFError, BrokenPipeError):
        pass
    finally:
        connection.close()
        _ = guard


class RealWorkerLifecycle(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="dungeon-owned-workers-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.dispatcher = runtime.Dispatcher(str(self.root), workers=2, request_timeout=5)
        replacement = patch.object(runtime, "_worker_main", controlled_worker)
        replacement.start()
        self.addCleanup(replacement.stop)
        await self.dispatcher.start()

    async def asyncTearDown(self):
        await asyncio.wait_for(self.dispatcher.close(), 15)
        self.assertTrue(all(worker is None for worker in self.dispatcher.workers))
        self.assertTrue(self.dispatcher.io_closed)

    def submit(self, identifier, *, write=False, **arguments):
        rpc = {"jsonrpc": "2.0", "id": identifier, "method": "tools/call", "params": {
            "name": "dungeon_cas_put" if write else "dungeon_project_probe",
            "arguments": {"project_root": str(self.root), **arguments},
        }}
        return self.dispatcher.submit("fixture", "session", rpc, str(self.root),
                                      bridge.RequestPolicy((str(self.root),), (str(self.root),)))

    async def until(self, predicate, timeout=8):
        deadline = time.monotonic() + timeout
        while not predicate():
            if time.monotonic() >= deadline:
                self.fail("Controlled worker condition did not settle within bound")
            await asyncio.sleep(0.025)

    @unittest.skipUnless(os.name == "nt", "Windows Job Object descendant ownership check")
    async def test_read_cancel_reaps_owned_descendant_and_replaces_worker(self):
        marker = self.root / "owned-child.pid"
        job = self.submit(1, fixture_mode="child", fixture_delay=10, marker=str(marker))
        await self.until(marker.exists)
        child_pid = int(marker.read_text(encoding="ascii"))
        from ctypes import wintypes as wt
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.argtypes = [wt.DWORD, wt.BOOL, wt.DWORD]
        kernel.OpenProcess.restype = wt.HANDLE
        kernel.WaitForSingleObject.argtypes = [wt.HANDLE, wt.DWORD]
        kernel.WaitForSingleObject.restype = wt.DWORD
        kernel.CloseHandle.argtypes = [wt.HANDLE]
        handle = kernel.OpenProcess(0x00100000, False, child_pid)  # SYNCHRONIZE
        self.assertTrue(handle, "Need a lifetime handle, not a reusable PID")
        try:
            self.dispatcher.cancel("fixture", "session", 1)
            self.assertIsNone(await self.dispatcher.result(job))
            await self.until(lambda: job.finished is not None and self.dispatcher.ready)
            self.assertEqual(job.state, "CANCELLED")
            self.assertEqual(kernel.WaitForSingleObject(handle, 0), 0, "Owned descendant survived cancellation")
            self.assertEqual(len(self.dispatcher.restarts), 1)
            reply = await self.dispatcher.result(self.submit(2))
            self.assertTrue(reply["result"]["fixture"])
        finally:
            kernel.CloseHandle(handle)

    async def test_write_deadline_cancel_and_drain_wait_for_actual_completion(self):
        marker = self.root / "completed-write.txt"
        writer = self.submit(1, write=True, fixture_mode="write", fixture_delay=0.8, marker=str(marker))
        writer.deadline = time.monotonic() + 0.25
        await self.until(lambda: writer.state == "RUNNING")
        queued = self.submit(2)
        self.dispatcher.cancel("fixture", "session", 2)
        self.assertEqual(queued.state, "CANCELLED")
        self.dispatcher.cancel("fixture", "session", 1)
        response = await self.dispatcher.result(writer)
        self.assertEqual(response["result"]["structuredContent"]["status"], "UNKNOWN")
        self.assertFalse(marker.exists())
        self.assertIsNone(writer.finished)
        self.assertTrue(any(worker.job is writer for worker in self.dispatcher.workers if worker))
        self.dispatcher.begin_shutdown()
        self.assertFalse(self.dispatcher.ready)
        self.assertFalse(self.dispatcher.drained)
        await self.until(lambda: writer.finished is not None)
        self.assertEqual(writer.state, "COMPLETED")
        self.assertEqual(marker.read_text(encoding="ascii"), "finished")
        self.assertTrue(writer.cancel_requested)
        self.assertTrue(writer.timed_out)
        operation = self.dispatcher.operation(writer.operation_id, "fixture", "session")
        self.assertTrue(operation["response"]["result"]["fixture"])

    async def test_unexpected_write_worker_exit_quarantines_without_replay(self):
        writer = self.submit(1, write=True, fixture_mode="crash")
        response = await self.dispatcher.result(writer)
        self.assertEqual(response["result"]["structuredContent"]["status"], "UNKNOWN")
        self.assertTrue(self.dispatcher.quarantined)
        self.assertFalse(self.dispatcher.ready)
        # EOF can deliver UNKNOWN before the OS reports process exit. The
        # coordinator must retain the writer/barrier until actual reaping.
        if writer.finished is None:
            self.assertTrue(any(worker and worker.job is writer for worker in self.dispatcher.workers))
        await self.until(lambda: writer.finished is not None)
        self.assertEqual(writer.state, "UNKNOWN")
        self.assertEqual(len(self.dispatcher.restarts), 0)
        with self.assertRaisesRegex(runtime.AdmissionError, "SERVICE_NOT_READY"):
            self.submit(2)


if __name__ == "__main__":
    unittest.main()
