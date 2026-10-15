from __future__ import annotations

import ctypes
import os
import tempfile
import unittest
from pathlib import Path

from tests.fixture_cleanup import CleanupRetry, cleanup_temporary_directory


def _directory_not_empty(path: Path) -> OSError:
    error = OSError("synthetic transient directory-not-empty error")
    error.winerror = 145
    error.filename = str(path)
    return error


class FixtureCleanupTests(unittest.TestCase):
    def test_transient_winerror145_is_reported_retried_and_fully_removed(self) -> None:
        temp_dir = tempfile.TemporaryDirectory()
        root = Path(temp_dir.name)
        payload = root / "nested" / "payload.txt"
        payload.parent.mkdir()
        payload.write_text("fixture", encoding="utf-8")
        original_cleanup = temp_dir.cleanup
        calls = 0
        events: list[CleanupRetry] = []

        def fail_once() -> None:
            nonlocal calls
            calls += 1
            if calls == 1:
                raise _directory_not_empty(payload.parent)
            original_cleanup()

        temp_dir.cleanup = fail_once
        try:
            cleanup_temporary_directory(
                temp_dir, max_retries=2, delay_seconds=0, on_retry=events.append
            )
            self.assertEqual(calls, 2)
            self.assertEqual(len(events), 1)
            self.assertEqual(events[0].winerror, 145)
            self.assertEqual(events[0].failing_path, os.path.normcase(str(payload.parent)))
            self.assertEqual(events[0].entries, ("payload.txt",))
            self.assertIsNone(events[0].entries_error)
            self.assertFalse(os.path.lexists(root))
        finally:
            temp_dir.cleanup = original_cleanup
            if os.path.lexists(root):
                cleanup_temporary_directory(temp_dir, delay_seconds=0)

    def test_persistent_winerror145_propagates_after_bounded_retries(self) -> None:
        temp_dir = tempfile.TemporaryDirectory()
        root = Path(temp_dir.name)
        payload = root / "still-owned.txt"
        payload.write_text("keep until cleanup succeeds", encoding="utf-8")
        original_cleanup = temp_dir.cleanup
        calls = 0
        events: list[CleanupRetry] = []

        def stay_locked() -> None:
            nonlocal calls
            calls += 1
            raise _directory_not_empty(root)

        temp_dir.cleanup = stay_locked
        try:
            with self.assertRaises(OSError) as raised:
                cleanup_temporary_directory(
                    temp_dir, max_retries=2, delay_seconds=0, on_retry=events.append
                )
            self.assertEqual(raised.exception.winerror, 145)
            self.assertEqual(calls, 3)
            self.assertEqual(len(events), 2)
            self.assertTrue(root.is_dir())
            self.assertTrue(payload.is_file())
        finally:
            temp_dir.cleanup = original_cleanup
            if os.path.lexists(root):
                cleanup_temporary_directory(temp_dir, delay_seconds=0)

    @unittest.skipUnless(os.name == "nt", "requires a Windows file handle with delete sharing denied")
    def test_locked_file_error_is_not_suppressed_and_removes_after_close(self) -> None:
        temp_dir = tempfile.TemporaryDirectory()
        root = Path(temp_dir.name)
        payload = root / "locked.bin"
        payload.write_bytes(b"locked fixture data")

        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        create_file = kernel32.CreateFileW
        create_file.argtypes = (
            wintypes.LPCWSTR,
            wintypes.DWORD,
            wintypes.DWORD,
            ctypes.c_void_p,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.HANDLE,
        )
        create_file.restype = wintypes.HANDLE
        close_handle = kernel32.CloseHandle
        close_handle.argtypes = (wintypes.HANDLE,)
        close_handle.restype = wintypes.BOOL
        handle = create_file(str(payload), 0x80000000, 0, None, 3, 0x80, None)
        invalid_handle = ctypes.c_void_p(-1).value
        if handle == invalid_handle:
            temp_dir.cleanup()
            raise ctypes.WinError(ctypes.get_last_error())

        events: list[CleanupRetry] = []
        try:
            with self.assertRaises(OSError) as raised:
                cleanup_temporary_directory(
                    temp_dir, delay_seconds=0, on_retry=events.append
                )
            self.assertNotEqual(getattr(raised.exception, "winerror", None), 145)
            self.assertEqual(events, [])
            self.assertTrue(root.is_dir())
            self.assertTrue(payload.is_file())
        finally:
            close_handle(handle)

        cleanup_temporary_directory(temp_dir, delay_seconds=0)
        self.assertFalse(os.path.lexists(root))


if __name__ == "__main__":
    unittest.main()
