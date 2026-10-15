"""Strict cleanup for owned ``TemporaryDirectory`` test fixtures."""
from __future__ import annotations

from dataclasses import asdict, dataclass
import json
import os
import stat
import sys
import tempfile
import time
from typing import Callable


_WINDOWS_DIRECTORY_NOT_EMPTY = 145
_REPARSE_POINT = 0x400


@dataclass(frozen=True)
class CleanupRetry:
    attempt: int
    retry_limit: int
    winerror: int
    owned_root: str
    failing_path: str
    entries: tuple[str, ...] | None
    entries_error: str | None


def _owned_root(temp_dir: tempfile.TemporaryDirectory) -> str:
    if not isinstance(temp_dir, tempfile.TemporaryDirectory):
        raise TypeError("cleanup requires its owning TemporaryDirectory object")

    name = os.fsdecode(os.fspath(temp_dir.name))
    if not os.path.isabs(name):
        raise ValueError("TemporaryDirectory path must be absolute")
    root = os.path.normcase(os.path.abspath(name))
    temp_root = os.path.normcase(os.path.abspath(tempfile.gettempdir()))
    if os.path.dirname(root) != temp_root:
        raise ValueError("TemporaryDirectory must be a direct child of tempfile.gettempdir()")
    return root


def _is_real_directory(path: str) -> bool:
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return False
    except OSError:
        return False
    return (
        stat.S_ISDIR(info.st_mode)
        and not stat.S_ISLNK(info.st_mode)
        and not (getattr(info, "st_file_attributes", 0) & _REPARSE_POINT)
    )


def _retry_path_is_owned_directory(root: str, path: str) -> bool:
    path = os.path.normcase(os.path.abspath(path))
    try:
        if os.path.commonpath((root, path)) != root:
            return False
    except ValueError:
        return False

    if not _is_real_directory(root):
        return False

    relative = os.path.relpath(path, root)
    current = root
    if relative == os.curdir:
        return True
    for component in _path_parts(relative):
        current = os.path.join(current, component)
        if not os.path.lexists(current):
            return True
        if not _is_real_directory(current):
            return False
    return True


def _path_parts(path: str) -> tuple[str, ...]:
    """Split a relative path without interpreting it against the process cwd."""
    drive, tail = os.path.splitdrive(path)
    del drive
    return tuple(part for part in tail.split(os.sep) if part not in ("", os.curdir))


def _entry_snapshot(path: str) -> tuple[tuple[str, ...] | None, str | None]:
    if not _is_real_directory(path):
        return None, "directory absent or not a real directory at snapshot time"
    try:
        with os.scandir(path) as entries:
            return tuple(sorted(entry.name for entry in entries)), None
    except OSError as exc:
        return None, f"{type(exc).__name__}: {exc}"


def _report_retry(event: CleanupRetry) -> None:
    print(
        "FIXTURE_CLEANUP_RETRY "
        + json.dumps(asdict(event), ensure_ascii=False, sort_keys=True),
        file=sys.stderr,
        flush=True,
    )


def cleanup_temporary_directory(
    temp_dir: tempfile.TemporaryDirectory,
    *,
    max_retries: int = 3,
    delay_seconds: float = 0.025,
    on_retry: Callable[[CleanupRetry], None] | None = None,
) -> None:
    """Remove one owned fixture; retry only contained Windows WinError 145.

    A retry is permitted only while the exact TemporaryDirectory root is a
    real directory directly under the active temp root, and the reported
    failing directory remains inside that owned tree. All other errors
    propagate. Returning successfully also requires the owned root to be gone.
    """
    if not isinstance(max_retries, int) or isinstance(max_retries, bool) or max_retries < 0:
        raise ValueError("max_retries must be a non-negative integer")
    if delay_seconds < 0:
        raise ValueError("delay_seconds must be non-negative")

    root = _owned_root(temp_dir)
    report = on_retry if on_retry is not None else _report_retry

    for attempt in range(1, max_retries + 2):
        if os.path.lexists(root) and not _is_real_directory(root):
            raise OSError(f"owned fixture root is not a real directory: {root}")
        try:
            temp_dir.cleanup()
        except OSError as exc:
            if getattr(exc, "winerror", None) != _WINDOWS_DIRECTORY_NOT_EMPTY or attempt > max_retries:
                raise

            raw_failure = exc.filename if exc.filename is not None else root
            failure = os.path.abspath(os.fsdecode(os.fspath(raw_failure)))
            if not _retry_path_is_owned_directory(root, failure):
                raise

            entries, entries_error = _entry_snapshot(failure)
            report(CleanupRetry(
                attempt=attempt,
                retry_limit=max_retries,
                winerror=_WINDOWS_DIRECTORY_NOT_EMPTY,
                owned_root=root,
                failing_path=os.path.normcase(failure),
                entries=entries,
                entries_error=entries_error,
            ))
            if delay_seconds:
                time.sleep(delay_seconds * (2 ** (attempt - 1)))
            continue

        if os.path.lexists(root):
            raise RuntimeError(f"TemporaryDirectory.cleanup returned but left the fixture root: {root}")
        return
