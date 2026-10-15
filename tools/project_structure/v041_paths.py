"""Fail-closed Windows output boundary, shared by real writers (not just MCP).

Directory handles deny DELETE sharing while publication is in progress. Thus an
already checked ancestor cannot be renamed/replaced by a junction between check
and write. New directories are created one component at a time under a pinned
parent and pinned before use. Atomic replacement never opens an existing leaf
for writing. Publication uses a DELETE-capable source handle, never a source
pathname. Existing preconditions are read under a deny-WRITE/DELETE lease, then
retired by that same handle before exclusive publication. A conflicting insert
is never overwritten; an un-restorable retirement is explicitly recoverable.
This is NOT a single atomic compare/exchange or crash/power-loss recovery. The
leases cover ordinary opens, not preexisting writable mappings, privileged
handle duplication/owner WRITE_DAC, or hostile modifications after the handles
are closed. New file stages atomically deny FILE_WRITE_ATTRIBUTES, preventing
ordinary hardlink opens during data writes. Only our exact temporary ACE is
removed through the owned handle, and subsequent data writes are forbidden.
"""
from __future__ import annotations

import contextlib
import ctypes
import json
import os
from pathlib import Path
import stat
import struct
import threading
import uuid


class UnsafeOutputPath(ValueError):
    def __init__(self, message):
        super().__init__(f"UNSAFE_OUTPUT_PATH: {message}")


class PublicationError(OSError):
    def __init__(self, message, residual=None):
        self.residual = residual or []
        super().__init__(f"PUBLISH_FAILED: {message}; residual={self.residual}")


_ANY = object()
_local = threading.local()
_mutex = threading.RLock()


def _absolute(value):
    raw = os.fspath(value)
    path = Path(raw)
    if not path.is_absolute() or ".." in path.parts:
        raise UnsafeOutputPath("absolute root without traversal is required")
    if os.name == "nt":
        if raw.startswith(("\\\\", "//")) or any(":" in p for p in path.parts[1:]):
            raise UnsafeOutputPath("UNC, device and alternate stream paths are unsupported")
        if any(p.endswith((" ", ".")) for p in path.parts[1:]):
            raise UnsafeOutputPath("ambiguous Windows path component")
    return path


def _link(path):
    info = path.lstat()
    return stat.S_ISLNK(info.st_mode) or bool(getattr(info, "st_file_attributes", 0) & 0x400)


def _win_pin(path):
    from ctypes import wintypes
    api = ctypes.WinDLL("kernel32", use_last_error=True)
    create = api.CreateFileW
    create.argtypes = (wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
                       wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE)
    create.restype = wintypes.HANDLE
    close = api.CloseHandle
    close.argtypes = (wintypes.HANDLE,)
    # FILE_READ_ATTRIBUTES alone does not establish the needed Windows share
    # exclusion on directory renames. GENERIC_READ includes directory listing
    # access, so denial of FILE_SHARE_DELETE is enforced by the kernel.
    handle = create(str(path), 0x80000000, 3, None, 3, 0x02200000, None)
    if handle == ctypes.c_void_p(-1).value:
        raise UnsafeOutputPath(f"cannot pin directory {path}: {ctypes.WinError(ctypes.get_last_error())}")
    try:
        class Tags(ctypes.Structure):
            _fields_ = [("attributes", wintypes.DWORD), ("tag", wintypes.DWORD)]
        tags = Tags()
        query = api.GetFileInformationByHandleEx
        query.argtypes = (wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD)
        if not query(handle, 9, ctypes.byref(tags), ctypes.sizeof(tags)):
            raise UnsafeOutputPath(f"cannot inspect pinned directory {path}")
        if tags.attributes & 0x400 or not tags.attributes & 0x10:
            raise UnsafeOutputPath(f"directory is a reparse point or non-directory: {path}")
        final = api.GetFinalPathNameByHandleW
        final.argtypes = (wintypes.HANDLE, wintypes.LPWSTR, wintypes.DWORD, wintypes.DWORD)
        buf = ctypes.create_unicode_buffer(32768)
        length = final(handle, buf, len(buf), 0)
        actual = buf.value.removeprefix("\\\\?\\")
        if not length or length >= len(buf) or os.path.normcase(actual.rstrip("\\")) != os.path.normcase(str(path).rstrip("\\")):
            raise UnsafeOutputPath(f"path aliases a different physical directory: {path}")
    except BaseException:
        close(handle)
        raise
    return lambda: close(handle)


def _kernel():
    from ctypes import wintypes as w
    api = ctypes.WinDLL("kernel32", use_last_error=True)
    signatures = {
        "CreateFileW": ((w.LPCWSTR, w.DWORD, w.DWORD, ctypes.c_void_p, w.DWORD, w.DWORD, w.HANDLE), w.HANDLE),
        "CloseHandle": ((w.HANDLE,), w.BOOL),
        "GetFileInformationByHandle": ((w.HANDLE, ctypes.c_void_p), w.BOOL),
        "GetFinalPathNameByHandleW": ((w.HANDLE, w.LPWSTR, w.DWORD, w.DWORD), w.DWORD),
        "SetFileInformationByHandle": ((w.HANDLE, ctypes.c_int, ctypes.c_void_p, w.DWORD), w.BOOL),
        "SetFilePointerEx": ((w.HANDLE, ctypes.c_longlong, ctypes.c_void_p, w.DWORD), w.BOOL),
        "ReadFile": ((w.HANDLE, ctypes.c_void_p, w.DWORD, ctypes.POINTER(w.DWORD), ctypes.c_void_p), w.BOOL),
        "WriteFile": ((w.HANDLE, ctypes.c_void_p, w.DWORD, ctypes.POINTER(w.DWORD), ctypes.c_void_p), w.BOOL),
        "FlushFileBuffers": ((w.HANDLE,), w.BOOL),
        "LocalFree": ((ctypes.c_void_p,), ctypes.c_void_p),
        "GetCurrentProcess": ((), w.HANDLE),
    }
    for name, (args, result) in signatures.items():
        function = getattr(api, name)
        function.argtypes, function.restype = args, result
    return api


def _security_api():
    from ctypes import wintypes as w
    api = ctypes.WinDLL("advapi32", use_last_error=True)
    pointer = ctypes.c_void_p
    signatures = {
        "ConvertStringSecurityDescriptorToSecurityDescriptorW":
            ((w.LPCWSTR, w.DWORD, ctypes.POINTER(pointer), ctypes.POINTER(w.DWORD)), w.BOOL),
        "GetSecurityInfo": ((w.HANDLE, ctypes.c_int, w.DWORD, ctypes.POINTER(pointer),
                             ctypes.POINTER(pointer), ctypes.POINTER(pointer),
                             ctypes.POINTER(pointer), ctypes.POINTER(pointer)), w.DWORD),
        "SetSecurityInfo": ((w.HANDLE, ctypes.c_int, w.DWORD, pointer, pointer, pointer, pointer), w.DWORD),
        "DeleteAce": ((pointer, w.DWORD), w.BOOL),
        "OpenProcessToken": ((w.HANDLE, w.DWORD, ctypes.POINTER(w.HANDLE)), w.BOOL),
        "CreatePrivateObjectSecurityEx": ((pointer, pointer, ctypes.POINTER(pointer), pointer,
                                            w.BOOL, w.DWORD, w.HANDLE, pointer), w.BOOL),
        "DestroyPrivateObjectSecurity": ((ctypes.POINTER(pointer),), w.BOOL),
    }
    for name, (args, result) in signatures.items():
        function = getattr(api, name)
        function.argtypes, function.restype = args, result
    return api


# ACCESS_DENIED_ACE, no inheritance flags, FILE_WRITE_ATTRIBUTES, S-1-1-0.
# No grants are added. Windows inherits the parent's existing ACL at creation.
_STAGE_DENY_ACE = bytes.fromhex("0100140000010000010100000000000100000000")


def _handle_dacl(lease):
    """Copy a handle's DACL and bounded ACE bytes, without pathname ACL calls."""
    acl, descriptor = ctypes.c_void_p(), ctypes.c_void_p()
    security = _security_api()
    status = security.GetSecurityInfo(lease.handle, 1, 4, None, None,
                                      ctypes.byref(acl), None, ctypes.byref(descriptor))
    if status:
        raise ctypes.WinError(status)
    try:
        if not acl.value:
            raise UnsafeOutputPath("null stage DACL is unsupported")
        header = ctypes.string_at(acl, 8)
        revision, _, size, count, _ = struct.unpack("<BBHHH", header)
        if size < 8:
            raise UnsafeOutputPath("invalid stage DACL")
        content = ctypes.string_at(acl, size)
        entries, offset = [], 8
        for _ in range(count):
            if offset + 4 > size:
                raise UnsafeOutputPath("invalid stage ACE header")
            length = struct.unpack_from("<H", content, offset + 2)[0]
            if length < 4 or offset + length > size:
                raise UnsafeOutputPath("invalid stage ACE size")
            entries.append(content[offset:offset + length])
            offset += length
        return content, (revision, tuple(entries))
    finally:
        lease.api.LocalFree(descriptor)


@contextlib.contextmanager
def _stage_security_attributes(kernel, parent):
    from ctypes import wintypes as w
    class Attributes(ctypes.Structure):
        _fields_ = [("length", w.DWORD), ("descriptor", ctypes.c_void_p), ("inherit", w.BOOL)]
    class Mapping(ctypes.Structure):
        _fields_ = [("read", w.DWORD), ("write", w.DWORD), ("execute", w.DWORD), ("all", w.DWORD)]
    creator, descriptor, parent_descriptor = (ctypes.c_void_p() for _ in range(3))
    token, parent_handle = w.HANDLE(), None
    security = _security_api()
    if not security.ConvertStringSecurityDescriptorToSecurityDescriptorW(
            "D:(D;;0x100;;;WD)", 1, ctypes.byref(creator), None):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        # A supplied DACL is NOT automatically combined with inherited grants
        # by CreateFileW. Ask Windows to compute inheritance, including Creator
        # Owner/group and generic-right mapping; never synthesize allow ACEs.
        parent_handle = kernel.CreateFileW(str(parent), 0x20000, 7, None, 3, 0x02200000, None)
        if parent_handle == ctypes.c_void_p(-1).value:
            parent_handle = None
            raise ctypes.WinError(ctypes.get_last_error())
        status = security.GetSecurityInfo(parent_handle, 1, 7, None, None, None, None,
                                          ctypes.byref(parent_descriptor))
        if status:
            raise ctypes.WinError(status)
        if not security.OpenProcessToken(kernel.GetCurrentProcess(), 0xA, ctypes.byref(token)):
            raise ctypes.WinError(ctypes.get_last_error())
        mapping = Mapping(0x120089, 0x120116, 0x1200A0, 0x1F01FF)
        if not security.CreatePrivateObjectSecurityEx(parent_descriptor, creator,
                ctypes.byref(descriptor), None, False, 1, token, ctypes.byref(mapping)):
            raise ctypes.WinError(ctypes.get_last_error())
        attributes = Attributes(ctypes.sizeof(Attributes), descriptor, False)
        yield ctypes.byref(attributes)
    finally:
        if descriptor:
            security.DestroyPrivateObjectSecurity(ctypes.byref(descriptor))
        if token:
            kernel.CloseHandle(token)
        if parent_descriptor:
            kernel.LocalFree(parent_descriptor)
        if parent_handle:
            kernel.CloseHandle(parent_handle)
        kernel.LocalFree(creator)


def _rename_handle(lease, destination):
    """FileRenameInfo, ReplaceIfExists=FALSE: the kernel owns source identity.

    See Microsoft FILE_RENAME_INFO and SetFileInformationByHandle contracts.
    Kept as a narrow hook for controlled mutation tests, not a safety bypass.
    """
    from ctypes import wintypes as w
    class RenameInfo(ctypes.Structure):
        _fields_ = [("flags", w.DWORD), ("root", w.HANDLE),
                    ("length", w.DWORD), ("name", w.WCHAR * 1)]
    destination = _absolute(destination)
    encoded = str(destination).encode("utf-16-le")
    buffer = ctypes.create_string_buffer(max(ctypes.sizeof(RenameInfo), RenameInfo.name.offset + len(encoded) + 2))
    info = RenameInfo.from_buffer(buffer)
    info.flags, info.root, info.length = 0, None, len(encoded)
    ctypes.memmove(ctypes.addressof(buffer) + RenameInfo.name.offset, encoded, len(encoded))
    if not lease.api.SetFileInformationByHandle(lease.handle, 3, buffer, len(buffer)):
        raise ctypes.WinError(ctypes.get_last_error())
    lease.path = destination


class _Lease:
    """Identity handle with ordinary write/delete sharing denied until close."""
    def __init__(self, path, *, create=False, directory=False):
        if os.name != "nt":
            raise UnsafeOutputPath("handle-native publication requires Windows")
        from ctypes import wintypes as w
        self.path, self.api, self.keep = _absolute(path), _kernel(), False
        self._stage_acl, self._writes_finished, self._deleting = None, not create, False
        # Sharing alone does NOT deny hardlink creation. Supply a deny-only
        # security descriptor atomically at CREATE_NEW, before any data write.
        # FILE_WRITE_DATA replaces GENERIC_WRITE (which includes the denied
        # FILE_WRITE_ATTRIBUTES). WRITE_DAC permits exact handle-only removal.
        access = 0x80000000 | 0x10000 | (0x40000 | 2 if create else 0)
        attributes = _stage_security_attributes(self.api, self.path.parent) if create else contextlib.nullcontext(None)
        with attributes as security_attributes:
            self.handle = self.api.CreateFileW(str(self.path), access, 1, security_attributes,
                                               1 if create else 3, 0x02200000, None)
            create_error = ctypes.get_last_error()
        if self.handle == ctypes.c_void_p(-1).value:
            self.handle = None
            raise ctypes.WinError(create_error)
        try:
            class Information(ctypes.Structure):
                _fields_ = [("attributes", w.DWORD), ("creation", w.FILETIME),
                            ("access", w.FILETIME), ("write", w.FILETIME),
                            ("volume", w.DWORD), ("size_high", w.DWORD),
                            ("size_low", w.DWORD), ("links", w.DWORD),
                            ("index_high", w.DWORD), ("index_low", w.DWORD)]
            info = Information()
            if not self.api.GetFileInformationByHandle(self.handle, ctypes.byref(info)):
                raise ctypes.WinError(ctypes.get_last_error())
            if info.attributes & 0x400 or bool(info.attributes & 0x10) != directory or (not directory and info.links != 1):
                raise UnsafeOutputPath(f"unsafe leased identity: {self.path}")
            self.identity = (info.volume, info.index_high, info.index_low)
            if create:
                _, self._stage_acl = _handle_dacl(self)
                entries = self._stage_acl[1]
                if not entries or entries[0] != _STAGE_DENY_ACE or len(entries) < 2:
                    raise UnsafeOutputPath("stage ACL lacks the exact temporary deny and inherited permissions")
            buffer = ctypes.create_unicode_buffer(32768)
            length = self.api.GetFinalPathNameByHandleW(self.handle, buffer, len(buffer), 0)
            actual = buffer.value.removeprefix("\\\\?\\")
            if not length or length >= len(buffer) or os.path.normcase(actual) != os.path.normcase(str(self.path)):
                raise UnsafeOutputPath(f"leased identity aliases another path: {self.path}")
        except BaseException as exc:
            try:
                if create:
                    self.delete()  # An unvalidated, empty owned identity is never used.
            except OSError as cleanup:
                raise PublicationError(str(exc), [{"path": str(self.path), "error": str(cleanup)}]) from exc
            finally:
                self._writes_finished = True  # Never restore an unvalidated ACL.
                self.close()
            raise

    def close(self):
        if self.handle is not None:
            try:
                if not self._deleting:
                    self.finish_writing()
            finally:
                if not self.api.CloseHandle(self.handle):
                    raise ctypes.WinError(ctypes.get_last_error())
                self.handle = None

    def finish_writing(self):
        """Remove ONLY our known first ACE after data completion; prohibit writes."""
        if self._writes_finished:
            return
        self.flush()
        content, actual = _handle_dacl(self)
        if actual != self._stage_acl or actual[1][0] != _STAGE_DENY_ACE:
            raise UnsafeOutputPath("temporary stage ACL changed; no permissions overwritten")
        # Freeze tool data writes BEFORE removing the hardlink exclusion.
        # A failed ACL operation never re-enables writes on this identity.
        self._writes_finished = True
        replacement = ctypes.create_string_buffer(content)
        security = _security_api()
        if not security.DeleteAce(replacement, 0):
            raise ctypes.WinError(ctypes.get_last_error())
        status = security.SetSecurityInfo(self.handle, 1, 4, None, None, replacement, None)
        if status:
            raise ctypes.WinError(status)
        if _handle_dacl(self)[1] != (actual[0], actual[1][1:]):
            raise UnsafeOutputPath("stage ACL restoration did not preserve all other ACEs")

    def delete(self):
        flag = ctypes.c_ubyte(1)
        if not self.api.SetFileInformationByHandle(self.handle, 4, ctypes.byref(flag), ctypes.sizeof(flag)):
            raise ctypes.WinError(ctypes.get_last_error())
        self._deleting = True

    def read_chunks(self):
        from ctypes import wintypes as w
        if not self.api.SetFilePointerEx(self.handle, 0, None, 0):
            raise ctypes.WinError(ctypes.get_last_error())
        buffer, count = ctypes.create_string_buffer(1024 * 1024), w.DWORD()
        while True:
            if not self.api.ReadFile(self.handle, buffer, len(buffer), ctypes.byref(count), None):
                raise ctypes.WinError(ctypes.get_last_error())
            if not count.value:
                return
            yield buffer.raw[:count.value]

    def read_bytes(self):
        return b"".join(self.read_chunks())

    def write(self, content):
        if self._writes_finished:
            raise PublicationError("data writes after stage ACL restoration are forbidden")
        from ctypes import wintypes as w
        count = w.DWORD()
        for offset in range(0, len(content), 1024 * 1024):
            chunk = content[offset:offset + 1024 * 1024]
            buffer = ctypes.create_string_buffer(chunk)
            if not self.api.WriteFile(self.handle, buffer, len(chunk), ctypes.byref(count), None):
                raise ctypes.WinError(ctypes.get_last_error())
            if count.value != len(chunk):
                raise PublicationError("short write to pinned stage")

    def flush(self):
        if not self.api.FlushFileBuffers(self.handle):
            raise ctypes.WinError(ctypes.get_last_error())


@contextlib.contextmanager
def staged_file(root, parent, *, prefix=".dungeon-stage-"):
    with output_boundary(root) as guard:
        guard.directory(parent)
        lease = _Lease(Path(parent) / (prefix + uuid.uuid4().hex), create=True)
        try:
            yield lease
        finally:
            errors = []
            try:
                if not lease.keep:
                    lease.delete()
            except OSError as exc:
                errors.append(str(exc))
            finally:
                try:
                    lease.close()
                except (OSError, ValueError) as exc:
                    errors.append(str(exc))
            if errors:
                raise PublicationError("stage cleanup failed", [{"path": str(lease.path), "error": "; ".join(errors)}])


@contextlib.contextmanager
def _claimed_leaf(path):
    lease = None
    try:
        try:
            lease = _Lease(path)
        except OSError as exc:
            if getattr(exc, "winerror", None) not in (2, 3):
                raise PublicationError(f"cannot claim leaf {path}: {exc}") from exc
        yield lease
    finally:
        if lease:
            lease.close()


def _publish_staged(guard, stage, path, *, expected=_ANY, exclusive=False):
    path = guard.inspect(path)
    old = None
    retired = None
    try:
        try:
            old = _Lease(path)
        except OSError as exc:
            if getattr(exc, "winerror", None) not in (2, 3):
                raise PublicationError(f"cannot claim existing leaf {path}: {exc}") from exc
        before = old.read_bytes() if old else None
        if exclusive and old:
            raise FileExistsError(str(path))
        if expected is not _ANY and before != expected:
            raise PublicationError(f"concurrent output change: {path}")
        if old:
            retired = path.parent / (".dungeon-retired-" + uuid.uuid4().hex)
            _rename_handle(old, retired)
        try:
            _rename_handle(stage, path)  # exclusive; never overwrite a new insertion
        except BaseException as exc:
            residual = []
            if old and retired:
                try:
                    _rename_handle(old, path)  # exclusive restoration, same identity
                except OSError as rollback:
                    residual.append({"path": str(retired), "error": str(rollback)})
            if isinstance(exc, (KeyboardInterrupt, SystemExit)) and not residual:
                raise
            if not old and isinstance(exc, FileExistsError):
                raise
            raise PublicationError(f"exclusive publication conflict: {path}: {exc}", residual) from exc
        stage.keep = True
        try:
            stage.finish_writing()
        except BaseException as exc:
            residual = [{"path": str(path), "error": "new output was published"}]
            if retired:
                residual.append({"path": str(retired), "error": "original retirement preserved"})
            raise PublicationError("published output; temporary ACL restoration failed", residual) from exc
        if old:
            try:
                old.delete()
            except BaseException as exc:
                raise PublicationError("published output; retirement cleanup failed", [
                    {"path": str(path), "error": "new output was published"},
                    {"path": str(retired), "error": str(exc)}]) from exc
    finally:
        if old:
            old.close()


class OutputBoundary:
    def __init__(self, root):
        self.root = _absolute(root)
        self.pins = {}
        self.created = []

    def pin(self, path):
        if path in self.pins:
            return
        if _link(path) or not path.is_dir():
            raise UnsafeOutputPath(f"unsafe directory: {path}")
        if os.name != "nt":
            # No equivalent deny-rename lease is available through pathlib.
            # Do not silently promise TOCTOU protection on another platform.
            raise UnsafeOutputPath("pinned output publication currently requires Windows")
        self.pins[path] = _win_pin(path)

    def inspect(self, value):
        path = _absolute(value) if Path(value).is_absolute() else self.root / value
        try:
            path.relative_to(self.root)
        except ValueError as exc:
            raise UnsafeOutputPath(f"output escapes explicit root: {path}") from exc
        if ".." in path.parts or (os.name == "nt" and any(":" in p or p.endswith((".", " ")) for p in path.parts[1:])):
            raise UnsafeOutputPath(f"unsafe output component: {path}")
        for ancestor in reversed(path.parent.parents):
            if os.path.lexists(ancestor):
                self.pin(ancestor)
        if os.path.lexists(path.parent):
            self.pin(path.parent)
        if os.path.lexists(path):
            if _link(path):
                raise UnsafeOutputPath(f"output is a link/reparse point: {path}")
            info = path.stat()
            if path.is_file() and info.st_nlink > 1:
                raise UnsafeOutputPath(f"output is hard-linked: {path}")
        return path

    def directory(self, path):
        path = self.inspect(path)
        missing = []
        current = path
        while not os.path.lexists(current):
            missing.append(current)
            current = current.parent
        self.pin(current)
        for item in reversed(missing):
            try:
                item.mkdir()
                self.created.append(item)
            except FileExistsError:
                pass
            self.pin(item)
        self.pin(path)
        return path

    def close(self):
        for release in reversed(list(self.pins.values())):
            release()
        self.pins.clear()

    def remove_empty_created(self):
        # Called only after all owned file publications were rolled back.
        for directory in reversed(self.created):
            release = self.pins.pop(directory, None)
            if release:
                release()
            try:
                directory.rmdir()
            except OSError:
                pass  # Never delete a non-empty or concurrently changed tree.

    def release_subtree(self, root):
        for path in reversed(list(self.pins)):
            if path == root or root in path.parents:
                self.pins.pop(path)()


@contextlib.contextmanager
def output_boundary(root, outputs=()):
    """Preflight ALL output paths before creating even an output directory."""
    root = _absolute(root)
    with _mutex:
        active = getattr(_local, "boundaries", {})
        existing = active.get(root)
        if existing is not None:
            for output in outputs:
                existing.inspect(output)
            yield existing
            return
        guard = OutputBoundary(root)
        try:
            # Root itself may not exist yet (explicit configured CAS store).
            guard.inspect(root / ".boundary-preflight")
            for output in outputs:
                guard.inspect(output)
            active[root] = guard
            _local.boundaries = active
            yield guard
        finally:
            active.pop(root, None)
            guard.close()


def validate_root(root):
    with output_boundary(root) as guard:
        if not guard.root.is_dir():
            raise UnsafeOutputPath("project root must be an existing directory")
        guard.pin(guard.root)
        return guard.root


def atomic_bytes(root, path, content, *, expected=_ANY, exclusive=False):
    with output_boundary(root, [path]) as guard:
        path = guard.inspect(path)
        guard.directory(path.parent)
        if expected is _ANY:
            with _claimed_leaf(path) as leaf:
                expected = leaf.read_bytes() if leaf else None
        with staged_file(root, path.parent) as stage:
            stage.write(content)
            stage.flush()
            _publish_staged(guard, stage, path, expected=expected, exclusive=exclusive)


def json_bytes(data):
    return (json.dumps(data, indent=2, ensure_ascii=False) + "\n").encode("utf-8")


def publish_files(root, outputs):
    """Stage every file, reject changed preconditions, restore owned bytes on error."""
    lock = Path(root) / ".dungeon-publish.lock"
    with output_boundary(root, [*outputs, lock]) as guard:
        before = {p: p.read_bytes() if p.is_file() else None for p in outputs}
        written = {}
        staged = {}
        lock_lease = None
        with contextlib.ExitStack() as stack:
            try:
                try:
                    lock_lease = _Lease(lock, create=True)
                except FileExistsError as exc:
                    raise PublicationError("CONCURRENT_WRITE: publication lock is already held") from exc
                # Prepare/fsync every file before publishing the first destination.
                for path, content in outputs.items():
                    stage = stack.enter_context(staged_file(root, path.parent))
                    stage.write(content)
                    stage.flush()
                    staged[path] = stage
                for path, content in outputs.items():
                    if before[path] != content:
                        _publish_staged(guard, staged[path], path, expected=before[path])
                        written[path] = staged[path]
                    else:
                        with _claimed_leaf(path) as leaf:
                            if leaf is None or leaf.read_bytes() != before[path]:
                                raise PublicationError(f"concurrent output change: {path}")
            except BaseException as exc:
                residual = list(getattr(exc, "residual", []))
                # A rename may have succeeded before retirement cleanup raised.
                # Register that still-leased identity before rollback accounting.
                for path, stage in staged.items():
                    if stage.keep and path not in written:
                        written[path] = stage
                for path, stage in reversed(list(written.items())):
                    try:
                        # The owned identity is still leased: rollback never
                        # deletes a possibly substituted pathname.
                        stage.delete()
                        stage.close()
                        if before[path] is not None:
                            atomic_bytes(root, path, before[path], expected=None, exclusive=True)
                        residual = [item for item in residual
                                    if not (item.get("path") == str(path)
                                            and item.get("error") == "new output was published")]
                    except (OSError, ValueError) as rollback:
                        residual.append({"path": str(path), "error": str(rollback)})
                if isinstance(exc, (KeyboardInterrupt, SystemExit)) and not residual:
                    raise
                raise PublicationError(str(exc), residual) from exc
            finally:
                if lock_lease:
                    try:
                        lock_lease.delete()
                    finally:
                        lock_lease.close()
                if len(written) < len(outputs):
                    # ExitStack closes/deletes stages before empty-dir cleanup.
                    stack.close()
                    guard.remove_empty_created()
