"""Bounded, read-only VCS observations; no repository initialization or mutations.

Scion 0.1.0 contracts were inspected at
7d445a0744e30f4dcb36a794b0ce42648157e1b9. Only ``log`` and ``show`` may
execute. Upstream ``status`` recovers pending operations and writes the index;
``diff`` opens a mutable working copy too. Neither is a read-only capability.
Local ``.scion/state`` is a freshly read working-copy *base*, not proof of a
clean tree or the current server ref. Pending operations invalidate that base.
Native Windows and WSL queries are deliberately unsupported: this adapter has
no verified WSL path/environment/process containment contract.

``scope.root`` selects the checkout relative to the authorized project root.
``repository`` is descriptive, but must also name a contained local path.
Results have status PASS/PARTIAL/FAIL, availability, source_revision, data and
issues. Availability describes access, separately from operation completeness.
No runtime dependency on Scion sources, JSON Schema, or a backend server API.
The executable, operator PATH and local Git metadata must be trusted. This is
not a sandbox for hostile Git configurations or concurrent filesystem swaps.
The older v03_evidence._git_state uses unbounded subprocess.run captures and
allows ancestor discovery. Its Git observation semantics are retained here,
but its subprocess implementation cannot safely be reused at this boundary.
"""
from __future__ import annotations

import json
import os
import re
import signal
import stat
import subprocess
import threading
import time
import tomllib
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PureWindowsPath
from urllib.parse import urlsplit


AVAILABILITIES = frozenset({"AVAILABLE", "UNAVAILABLE", "UNSUPPORTED_PLATFORM",
                            "NOT_CONFIGURED", "AUTH_REQUIRED", "SERVER_UNREACHABLE", "UNKNOWN"})
DIRTY_STATES = frozenset({"CLEAN", "DIRTY", "UNKNOWN"})
TIMEOUT_SECONDS = 10.0
MAX_OUTPUT_BYTES = 1024 * 1024  # combined stdout/stderr, enforced while reading
MAX_STATE_BYTES = 64 * 1024
MAX_HISTORY = 100
_NATIVE_WINDOWS = os.name == "nt"
_GIT_ID = re.compile(r"(?:[0-9a-fA-F]{40}|[0-9a-fA-F]{64})\Z")
_SCOPE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")
_PROFILE = re.compile(r"[A-Za-z][A-Za-z0-9_-]{0,63}\Z")
_SELECTOR = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/@:^~-]{0,255}\Z")
_SCION_REF = re.compile(r"[a-z0-9][a-z0-9._/-]{0,199}\Z")


@dataclass(frozen=True)
class VersionScope:
    scope_id: str
    backend: str = "GIT"
    root: str = "."
    mode: str = "MONOREPO"
    repository: str = "."
    default_ref: str | None = None
    endpoint_profile: str | None = None

    def as_dict(self) -> dict:
        return {"id": self.scope_id, "backend": self.backend, "root": self.root,
                "mode": self.mode, "repository": self.repository,
                "default_ref": self.default_ref, "endpoint_profile": self.endpoint_profile}


def _text(value, maximum=4096):
    return isinstance(value, str) and 0 < len(value) <= maximum and not any(
        ord(c) < 32 or ord(c) == 127 for c in value)


def _matches(pattern, value):
    return isinstance(value, str) and pattern.fullmatch(value) is not None


def _scion_id(value, kind="c"):
    return isinstance(value, str) and re.fullmatch(kind + r"1[0-9a-f]{64}", value) is not None


def _scion_ref(value):
    return (_matches(_SCION_REF, value) and ".." not in value
            and "//" not in value and not value.endswith("/"))


def validate_revision(record) -> bool:
    """Validate a typed observation, including unavailable/unknown observations.

    Extra provenance fields are permitted for versioned callers. Missing IDs
    can never be AVAILABLE or CLEAN/DIRTY. A known base may remain recorded
    when a service is unavailable, but cannot assert local cleanliness.
    """
    if not isinstance(record, dict) or type(record.get("schema_version")) is not int or record["schema_version"] != 1:
        return False
    if record.get("backend") not in ("GIT", "SCION") or not _matches(_SCOPE_ID, record.get("scope_id")):
        return False
    availability, dirty = record.get("availability"), record.get("dirty_state")
    if not isinstance(availability, str) or availability not in AVAILABILITIES or not isinstance(dirty, str) or dirty not in DIRTY_STATES:
        return False
    if "revision" not in record or "ref" not in record:
        return False
    reference = record["ref"]
    if reference is not None and not _text(reference, 256):
        return False
    revision = record["revision"]
    if revision is None:
        return availability != "AVAILABLE" and dirty == "UNKNOWN"
    if not (_matches(_GIT_ID, revision) if record["backend"] == "GIT" else _scion_id(revision)):
        return False
    return availability == "AVAILABLE" or dirty == "UNKNOWN"


class _Failure(Exception):
    def __init__(self, code, message, availability="UNKNOWN"):
        super().__init__(message)
        self.code, self.message, self.availability = code, message, availability


def _require(condition):
    if not condition:
        raise _Failure("INVALID_RESPONSE", "VCS response does not match the pinned CLI contract.")


def _physical(path: Path) -> Path:
    """Reject raw aliases before resolving them, including every ancestor."""
    if not path.is_absolute() or ".." in path.parts or str(path).startswith(("\\\\", "//")):
        raise _Failure("UNSAFE_ROOT", "VCS root must be an explicit contained physical path.", "NOT_CONFIGURED")
    for part in reversed((path, *path.parents)):
        try:
            info = part.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise _Failure("UNSAFE_ROOT", "Symlink or reparse traversal is not authorized.", "NOT_CONFIGURED")
    return path.resolve(strict=True)


def _relative(value):
    if not _text(value) or value != value.strip():
        raise _Failure("UNSAFE_ROOT", "Invalid local scope path.", "NOT_CONFIGURED")
    raw = value.replace("\\", "/")
    win = PureWindowsPath(raw)
    parts = raw.split("/")
    if (win.drive or raw.startswith("/") or any(p == ".." or ":" in p or
            (p not in ("", ".") and (p.endswith((".", " ")) or PureWindowsPath(p).is_reserved())) for p in parts)):
        raise _Failure("UNSAFE_ROOT", "External roots, traversal and device paths are not authorized.", "NOT_CONFIGURED")
    return Path(*parts)


def _contained(project, path, *, existing=True):
    try:
        path.relative_to(project)
        if existing:
            return _physical(path)
        # Inspect existing parents too; do not erase a junction in a missing path.
        nearest = path
        while not nearest.exists() and not nearest.is_symlink():
            nearest = nearest.parent
        _physical(nearest)
        path.resolve().relative_to(project)
        return path
    except (ValueError, OSError, RuntimeError):
        raise _Failure("UNSAFE_ROOT", "VCS path is missing or outside the authorized project.", "NOT_CONFIGURED") from None


class _WindowsJob:
    """Close-to-kill job owns the process and any descendants (no taskkill shell)."""
    def __init__(self, process):
        import ctypes
        from ctypes import wintypes as w

        class Basic(ctypes.Structure):
            _fields_ = [("per_process", ctypes.c_int64), ("per_job", ctypes.c_int64),
                        ("flags", w.DWORD), ("minimum", ctypes.c_size_t), ("maximum", ctypes.c_size_t),
                        ("active", w.DWORD), ("affinity", ctypes.c_size_t), ("priority", w.DWORD), ("scheduling", w.DWORD)]

        class Io(ctypes.Structure):
            _fields_ = [(name, ctypes.c_uint64) for name in ("read_ops", "write_ops", "other_ops", "read_bytes", "write_bytes", "other_bytes")]

        class Extended(ctypes.Structure):
            _fields_ = [("basic", Basic), ("io", Io), ("process_memory", ctypes.c_size_t),
                        ("job_memory", ctypes.c_size_t), ("peak_process", ctypes.c_size_t), ("peak_job", ctypes.c_size_t)]

        self.api = ctypes.WinDLL("kernel32", use_last_error=True)
        self.api.CreateJobObjectW.argtypes = [ctypes.c_void_p, w.LPCWSTR]
        self.api.CreateJobObjectW.restype = w.HANDLE
        self.api.SetInformationJobObject.argtypes = [w.HANDLE, ctypes.c_int, ctypes.c_void_p, w.DWORD]
        self.api.AssignProcessToJobObject.argtypes = [w.HANDLE, w.HANDLE]
        self.api.CloseHandle.argtypes = [w.HANDLE]
        self.handle = self.api.CreateJobObjectW(None, None)
        limits = Extended()
        limits.basic.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not self.handle or not self.api.SetInformationJobObject(self.handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
            self.close()
            raise OSError("process containment unavailable")
        if not self.api.AssignProcessToJobObject(self.handle, w.HANDLE(int(process._handle))):
            self.close()
            raise OSError("process containment unavailable")

    def close(self):
        if self.handle:
            self.api.CloseHandle(self.handle)
            self.handle = None


@dataclass(frozen=True)
class _Output:
    returncode: int
    stdout: bytes
    stderr: bytes


def _run(argv, cwd, env) -> _Output:
    """Bound bytes during reads, bound wall time including inherited pipe handles.

    Executables are trusted local CLI programs. A Windows job is assigned
    immediately after spawn, before reading output; inability to contain fails
    closed. POSIX commands get a private session/process group.
    """
    process, job = None, None
    buffers, finished = [bytearray(), bytearray()], [threading.Event(), threading.Event()]
    overflow, lock = threading.Event(), threading.Lock()
    readers = []
    deadline = time.monotonic() + TIMEOUT_SECONDS
    try:
        process = subprocess.Popen(argv, cwd=cwd, env=env, stdin=subprocess.DEVNULL,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   start_new_session=os.name != "nt",
                                   creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        if os.name == "nt":
            job = _WindowsJob(process)

        def read(stream, index):
            try:
                while chunk := os.read(stream.fileno(), 8192):
                    with lock:
                        remaining = MAX_OUTPUT_BYTES - sum(map(len, buffers))
                        buffers[index].extend(chunk[:remaining])
                        if len(chunk) > remaining:
                            overflow.set()
                            return
            except OSError:
                overflow.set()  # truncated pipe is not a successful observation
            finally:
                finished[index].set()

        for index, stream in enumerate((process.stdout, process.stderr)):
            thread = threading.Thread(target=read, args=(stream, index), daemon=True)
            readers.append(thread)
            thread.start()
        while True:
            if overflow.is_set():
                raise _Failure("OUTPUT_LIMIT", "VCS output exceeded the bounded capture limit.")
            if process.poll() is not None and all(done.is_set() for done in finished):
                return _Output(process.returncode, bytes(buffers[0]), bytes(buffers[1]))
            if time.monotonic() >= deadline:
                raise _Failure("TIMEOUT", "VCS query exceeded its time limit.", "SERVER_UNREACHABLE")
            overflow.wait(min(0.01, max(0, deadline - time.monotonic())))
    except FileNotFoundError:
        raise _Failure("CLIENT_UNAVAILABLE", "VCS executable is not installed.", "UNAVAILABLE") from None
    except OSError:
        raise _Failure("PROCESS_UNAVAILABLE", "VCS process could not be started or contained.", "UNAVAILABLE") from None
    finally:
        if job is not None:
            job.close()
        if process is not None:
            if os.name != "nt":
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            if process.poll() is None:
                process.kill()
            process.wait(timeout=2)
            for thread in readers:
                thread.join(timeout=1)
            for stream in (process.stdout, process.stderr):
                if stream:
                    stream.close()


def _executable(name):
    # Never search the checkout/current directory implicitly, even on Windows.
    paths = [p for p in os.get_exec_path() if p and Path(p).is_absolute()]
    for directory in paths:
        candidate = Path(directory) / (name + ".exe" if os.name == "nt" else name)
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
    raise _Failure("CLIENT_UNAVAILABLE", "VCS executable is not installed on the trusted PATH.", "UNAVAILABLE")


def _json(raw):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            _require(key not in result)
            result[key] = value
        return result

    def reject(_):
        _require(False)

    try:
        return json.loads(raw.decode("utf-8"), object_pairs_hook=unique, parse_constant=reject, parse_float=reject)
    except (ValueError, UnicodeError, RecursionError):
        raise _Failure("INVALID_RESPONSE", "VCS returned malformed or incomplete JSON.") from None


def _object(value, required, optional=()):
    _require(isinstance(value, dict) and set(required) <= value.keys()
             and value.keys() <= set(required) | set(optional))


def _time_view(value):
    _object(value, ("seconds", "tz_offset_minutes"))
    _require(all(type(v) is int and -(2**63) <= v < 2**63 for v in value.values()))


def _repo_path(value, *, root_allowed=False):
    """Upstream byte paths, with additional cross-platform path restrictions.

    An empty sparse prefix is the whole tree. Hex TOML representations are
    decoded before checking traversal, NUL, component sizes and device names.
    They are observations only and are never joined to a filesystem path.
    """
    if isinstance(value, dict):
        _object(value, ("hex",))
        _require(isinstance(value["hex"], str) and len(value["hex"]) <= 8192
                 and re.fullmatch(r"(?:[0-9a-f]{2})*", value["hex"]) is not None)
        raw = bytes.fromhex(value["hex"])
    else:
        _require(isinstance(value, str))
        raw = value.encode("utf-8")
    _require(len(raw) <= 4096)
    if not raw:
        _require(root_allowed)
        return
    _require(not raw.startswith(b"/") and not raw.endswith(b"/") and not any(c in raw for c in (b"\x00", b"\\", b":")))
    for component in raw.split(b"/"):
        _require(0 < len(component) <= 255 and component not in (b".", b"..")
                 and component.lower() != b".scion" and not component.endswith((b".", b" ")))
        name = component.decode("utf-8", errors="replace")
        _require(not PureWindowsPath(name).is_reserved())


def _entry(value, *, show=False):
    fields = ("id", "parents", "root_tree", "change_id", "filtered", "meta")
    _object(value, fields + (("changes",) if show else ()))
    _require(_scion_id(value["id"]) and _scion_id(value["root_tree"], "t"))
    _require(isinstance(value["parents"], list) and all(_scion_id(p) for p in value["parents"]))
    _require(len(set(value["parents"])) == len(value["parents"]) and value["id"] not in value["parents"])
    _require(_matches(re.compile(r"[0-9a-f]{32}\Z"), value["change_id"]) and type(value["filtered"]) is bool)
    meta = value["meta"]
    optional = ("author", "author_time", "committer", "committer_time", "creator", "message")
    _object(meta, ("meta_id", "redacted"), optional)
    _require(_scion_id(meta["meta_id"], "m") and type(meta["redacted"]) is bool)
    for key in optional:
        if meta.get(key) is not None:
            if key.endswith("_time"):
                _time_view(meta[key])
            else:
                _require(isinstance(meta[key], str))
    if meta["redacted"]:
        _require(all(meta.get(key) is None for key in optional))
    if show:
        _require(isinstance(value["changes"], list))
        for change in value["changes"]:
            _change(change)
        _require(len({c["path"] for c in value["changes"]}) == len(value["changes"]))


def _change(change):
    _object(change, ("path", "status", "binary", "hunks"), ("old_id", "new_id", "old_mode", "new_mode"))
    _repo_path(change["path"])
    _require(change["status"] in ("added", "deleted", "modified", "mode_changed") and type(change["binary"]) is bool)
    for side in ("old", "new"):
        identifier, mode = change.get(side + "_id"), change.get(side + "_mode")
        _require(identifier is None or _scion_id(identifier, "b"))
        _require(mode is None or (type(mode) is int and mode in (1, 2, 3)))
        _require((identifier is None) == (mode is None))
    _require(isinstance(change["hunks"], list) and not (change["binary"] and change["hunks"]))
    for hunk in change["hunks"]:
        _object(hunk, ("old_start", "old_lines", "new_start", "new_lines", "lines"))
        _require(all(type(hunk[k]) is int and 0 <= hunk[k] < 2**32
                     for k in ("old_start", "old_lines", "new_start", "new_lines")))
        _require(isinstance(hunk["lines"], list) and all(isinstance(line, str) and line.startswith((" ", "+", "-")) for line in hunk["lines"]))
        _require(sum(line[0] in " -" for line in hunk["lines"]) == hunk["old_lines"]
                 and sum(line[0] in " +" for line in hunk["lines"]) == hunk["new_lines"])


def _scion_payload(raw, operation, limit=MAX_HISTORY):
    envelope = _json(raw)
    _object(envelope, ("schema", "data"))
    if envelope["schema"] == "scion.cli.error.v1":
        data = envelope["data"]
        _object(data, ("code", "message", "exit_code"), ("subcode",))
        _require(isinstance(data["code"], str) and isinstance(data["message"], str)
                 and type(data["exit_code"]) is int and 1 <= data["exit_code"] <= 4
                 and (data.get("subcode") is None or isinstance(data["subcode"], str)))
        availability = {"UNAUTHENTICATED": "AUTH_REQUIRED", "PERMISSION_DENIED": "AUTH_REQUIRED",
                        "UNAVAILABLE": "SERVER_UNREACHABLE"}.get(data["code"], "UNKNOWN")
        # Never expose upstream error messages, subcodes or stderr.
        raise _Failure("QUERY_FAILED", "Scion query failed; external diagnostic text was suppressed.", availability)
    _require(envelope["schema"] == "scion.cli." + operation + ".v1")
    data = envelope["data"]
    if operation == "show":
        _entry(data, show=True)
    else:
        _object(data, ("from", "first_parent", "entries", "truncated"), ("path",))
        _require(_scion_id(data["from"]) and type(data["first_parent"]) is bool and type(data["truncated"]) is bool)
        _require(data.get("path") is None and data["first_parent"] is False)
        _require(isinstance(data["entries"], list) and 0 < len(data["entries"]) <= limit)
        for entry in data["entries"]:
            _entry(entry)
        _require(data["entries"][0]["id"] == data["from"]
                 and len({entry["id"] for entry in data["entries"]}) == len(data["entries"]))
    return data


class VcsBackend:
    def __init__(self, root: Path, scope: VersionScope):
        self.root, self.scope = Path(root), scope

    def _checkout(self):
        _require(isinstance(self.scope, VersionScope))
        if not _matches(_SCOPE_ID, self.scope.scope_id) or self.scope.mode not in ("MONOREPO", "REPOSITORY", "EXTERNAL"):
            raise _Failure("INVALID_SCOPE", "Invalid version scope configuration.", "NOT_CONFIGURED")
        project = _physical(self.root)
        _contained(project, project / _relative(self.scope.repository), existing=False)
        checkout = _contained(project, project / _relative(self.scope.root))
        if not checkout.is_dir():
            raise _Failure("INVALID_SCOPE", "The VCS root is not a directory.", "NOT_CONFIGURED")
        return project, checkout

    def _record(self, availability="UNKNOWN", revision=None, dirty="UNKNOWN", reference=None):
        return {"schema_version": 1, "backend": self.scope.backend, "scope_id": self.scope.scope_id,
                "revision": revision, "ref": reference, "dirty_state": dirty, "availability": availability,
                "observed_at": datetime.now(timezone.utc).isoformat()}

    def _result(self, status, record, data=None, issues=None):
        return {"status": status, "availability": record["availability"], "source_revision": record,
                "data": data or {}, "issues": issues or []}

    def _failed(self, failure, record=None):
        record = dict(record or self._record())
        record.update(availability=failure.availability, dirty_state="UNKNOWN")
        return self._result("FAIL", record, issues=[{"code": failure.code, "message": failure.message}])

    def _perform(self, operation, revision=None, limit=20):
        try:
            return self._observe(operation, revision, limit)
        except _Failure as failure:
            return self._failed(failure)
        except (OSError, ValueError, RuntimeError, TypeError, RecursionError, subprocess.SubprocessError):
            return self._failed(_Failure("OBSERVATION_FAILED", "VCS observation could not be safely completed."))

    def status(self):
        return self._perform("status")

    def inspect(self, revision=None):
        return self._perform("inspect", revision)

    def history(self, limit=20, revision=None):
        return self._perform("history", revision, limit)

    def diff(self):
        return self._perform("diff")


class GitBackend(VcsBackend):
    def _git(self, checkout, *args):
        # No inherited Git directory/config overrides, credential prompts or lazy fetch.
        env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
        env.update(GIT_OPTIONAL_LOCKS="0", GIT_TERMINAL_PROMPT="0", GIT_NO_LAZY_FETCH="1",
                   GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull, GIT_ATTR_NOSYSTEM="1")
        result = _run([_executable("git"), "--no-pager", "--no-optional-locks", "-c", "core.fsmonitor=false",
                       "-c", "core.untrackedCache=false", "-C", str(checkout), *args], checkout, env)
        if result.returncode:
            raise _Failure("GIT_QUERY_FAILED", "Git query failed; external diagnostic text was suppressed.", "UNAVAILABLE")
        try:
            return result.stdout.decode("utf-8")
        except UnicodeError:
            raise _Failure("INVALID_RESPONSE", "Git response is not valid UTF-8.") from None

    def _observe(self, operation, revision, limit):
        project, checkout = self._checkout()
        marker = checkout / ".git"
        if not marker.exists():
            raise _Failure("NOT_A_REPOSITORY", "The exact scope root has no Git repository.", "NOT_CONFIGURED")
        _contained(project, marker)
        if marker.is_file():
            text = _read_local(marker, MAX_STATE_BYTES).decode("utf-8").strip()
            if not text.startswith("gitdir: "):
                raise _Failure("UNSAFE_ROOT", "Invalid Git metadata indirection.", "NOT_CONFIGURED")
            target = Path(text[8:])
            if not target.is_absolute():
                target = checkout / target
            # Worktree indirections must stay under this authorized project too.
            _contained(project, target)
        top = Path(self._git(checkout, "rev-parse", "--show-toplevel").strip())
        if _physical(top) != checkout:
            raise _Failure("ROOT_MISMATCH", "Git discovered a different checkout root.", "NOT_CONFIGURED")
        for arg in ("--absolute-git-dir", "--git-common-dir"):
            metadata = Path(self._git(checkout, "rev-parse", arg).strip())
            if not metadata.is_absolute():
                metadata = checkout / metadata
            metadata = _contained(project, metadata)
            if (metadata / "objects/info/alternates").exists():
                raise _Failure("UNSAFE_ROOT", "External Git object alternates are unsupported.", "NOT_CONFIGURED")
        head = self._git(checkout, "rev-parse", "--verify", "HEAD").strip()
        _require(_matches(_GIT_ID, head))
        porcelain = self._git(checkout, "status", "--porcelain=v1", "-z", "--untracked-files=normal", "--ignore-submodules=none")
        dirty = "DIRTY" if porcelain else "CLEAN"
        # symbolic-ref exits nonzero for a detached HEAD; rev-parse returns HEAD.
        reference = self._git(checkout, "rev-parse", "--abbrev-ref", "HEAD").strip()
        reference = None if reference == "HEAD" else reference
        _require(reference is None or _text(reference, 256))
        record = self._record("AVAILABLE", head, dirty, reference)
        if operation == "status":
            return self._result("PASS", record, {"root": self.scope.root})
        if type(limit) is not int or not 1 <= limit <= MAX_HISTORY:
            raise _Failure("INVALID_LIMIT", "History limit must be an integer between 1 and 100.", "NOT_CONFIGURED")
        selected = head
        if revision is not None:
            if not _matches(_SELECTOR, revision):
                raise _Failure("INVALID_REVISION", "Invalid revision selector.", "NOT_CONFIGURED")
            selected = self._git(checkout, "rev-parse", "--verify", "--end-of-options", revision + "^{commit}").strip()
            _require(_matches(_GIT_ID, selected))
        if operation in ("history", "inspect"):
            text = self._git(checkout, "log", "-n", str(limit if operation == "history" else 1),
                             "--format=%H%x00%P%x00%aI%x00%an%x00%s%x00", selected, "--")
            fields = text.split("\x00")
            _require(len(fields) >= 6 and not fields[-1].strip() and (len(fields) - 1) % 5 == 0)
            entries = []
            for i in range(0, len(fields) - 1, 5):
                identifier, parents, stamp, author, message = fields[i:i + 5]
                identifier = identifier.strip()
                _require(_matches(_GIT_ID, identifier) and all(_matches(_GIT_ID, p) for p in parents.split()))
                datetime.fromisoformat(stamp)
                entries.append({"id": identifier, "parents": parents.split(), "author_time": stamp, "author": author, "message": message})
            _require(entries[0]["id"] == selected and len(entries) <= (limit if operation == "history" else 1))
            # source_revision describes the observed checkout, not a historical query.
            return self._result("PASS", record, {"entries": entries} if operation == "history" else entries[0])
        patch = self._git(checkout, "diff", "--no-ext-diff", "--no-textconv", "--ignore-submodules=none", head, "--")
        return self._result("PASS", record, {"patch": patch, "comparison": "HEAD_TO_TRACKED_WORKTREE", "includes_untracked": False})


def _read_local(path, maximum):
    info = path.stat()
    if not stat.S_ISREG(info.st_mode) or info.st_size > maximum:
        raise _Failure("INVALID_LOCAL_STATE", "Local VCS state is not a bounded regular file.", "UNKNOWN")
    with path.open("rb") as stream:
        data = stream.read(maximum + 1)
    if len(data) > maximum:
        raise _Failure("INVALID_LOCAL_STATE", "Local VCS state exceeds the size limit.", "UNKNOWN")
    return data


def _endpoint(value):
    if not _text(value) or any(c.isspace() for c in value) or any(c in value for c in ("@", "?", "#", "\\", "%")):
        return False
    try:
        url = urlsplit(value)
        if url.scheme == "https":
            host_ok = url.hostname and (":" in url.hostname or re.fullmatch(r"[A-Za-z0-9._-]+", url.hostname))
            return bool(host_ok) and not url.netloc.endswith(":") and url.path in ("", "/") and (url.port is None or 0 < url.port <= 65535)
        return url.scheme == "unix" and not url.netloc and len(url.path) > 1 and url.path.startswith("/") and ".." not in url.path.split("/")
    except ValueError:
        return False


def _scrub(value, secret):
    if isinstance(value, str):
        return value.replace(secret, "[REDACTED]") if secret else value
    if isinstance(value, dict):
        return {key: _scrub(item, secret) for key, item in value.items()}
    if isinstance(value, list):
        return [_scrub(item, secret) for item in value]
    return value


class ScionBackend(VcsBackend):
    def _state(self, project, checkout):
        path = checkout / ".scion/state"
        if not path.exists():
            raise _Failure("NOT_A_WORKING_COPY", "The exact scope root has no Scion working-copy state.", "NOT_CONFIGURED")
        _contained(project, path)
        try:
            state = tomllib.loads(_read_local(path, MAX_STATE_BYTES).decode("utf-8"))
        except (ValueError, UnicodeError):
            raise _Failure("INVALID_LOCAL_STATE", "Scion working-copy state is malformed.") from None
        _require(_scion_id(state.get("base")))
        _require(state.get("endpoint") is None or isinstance(state["endpoint"], str))
        _require(state.get("tracking_ref") is None or _scion_ref(state["tracking_ref"]))
        for field in ("sparse", "inaccessible"):
            _require(isinstance(state.get(field), list))
            for path_value in state[field]:
                _repo_path(path_value, root_allowed=True)
        if state.get("pending") is not None:
            raise _Failure("PENDING_OPERATION", "Scion has a pending operation; no current revision is asserted and recovery is disabled.")
        return state

    def _configuration(self):
        profile = self.scope.endpoint_profile
        if not _matches(_PROFILE, profile):
            raise _Failure("PROFILE_REQUIRED", "An explicit Scion endpoint profile is required.", "NOT_CONFIGURED")
        prefix = "DUNGEON_SCION_" + profile.upper().replace("-", "_") + "_"
        endpoint = os.environ.get(prefix + "ENDPOINT")
        if not _endpoint(endpoint):
            raise _Failure("INVALID_ENDPOINT", "Profile endpoint must be HTTPS or an absolute Unix socket URL without credentials or query.", "NOT_CONFIGURED")
        transport = os.environ.get(prefix + "TRANSPORT", "native")
        if transport not in ("native", "wsl"):
            raise _Failure("INVALID_TRANSPORT", "Scion transport must be native or wsl.", "NOT_CONFIGURED")
        if transport == "wsl" or _NATIVE_WINDOWS:
            raise _Failure("UNSUPPORTED_TRANSPORT", "Native Windows Scion and unverified WSL execution are unsupported.", "UNSUPPORTED_PLATFORM")
        token = os.environ.get(prefix + "TOKEN") or os.environ.get("SCION_TOKEN")
        if not _text(token, 16384) or token != token.strip():
            raise _Failure("TOKEN_REQUIRED", "An explicit token environment variable is required; credential-file fallback and re-exchange are disabled.", "AUTH_REQUIRED")
        env = {k: v for k, v in os.environ.items() if not k.startswith(("SCION_", "DUNGEON_SCION_"))}
        # Pinned ctx.endpoint() reads home config even with an explicit endpoint.
        # Its only config_dir lookup uses HOME. Omit it for this child process
        # so no home config/credentials are consulted; the parent is unchanged.
        env.pop("HOME", None)
        env.update(SCION_ENDPOINT=endpoint, SCION_TOKEN=token, SCION_LOG="off")
        ca_file = os.environ.get(prefix + "CA_FILE")
        if ca_file:
            # CA is explicitly operator-supplied, never a manifest command or key.
            ca = _physical(Path(ca_file))
            if not ca.is_file():
                raise _Failure("INVALID_CA", "Profile CA file is not a physical regular file.", "NOT_CONFIGURED")
            env["SCION_CA_FILE"] = str(ca)
        return _executable("scion"), env, token

    def _observe(self, operation, revision, limit):
        project, checkout = self._checkout()
        state = self._state(project, checkout)
        reference = state.get("tracking_ref")
        record = self._record(revision=state["base"], reference=reference)
        local = {"base": state["base"], "tracking_ref": reference, "observation": "LOCAL_WORKING_COPY_BASE",
                 "pending": False, "capabilities": {"status": False, "inspect": True, "history": True, "diff": False}}
        issues = [{"code": "DIRTY_STATE_UNKNOWN", "message": "Scion status can recover operations and write its index; cleanliness was not checked."}]
        try:
            executable, env, token = self._configuration()
        except _Failure as failure:
            record["availability"] = failure.availability
            issues.append({"code": failure.code, "message": failure.message})
            if operation == "diff":
                issues.append({"code": "UNSUPPORTED_OPERATION", "message": "Scion working-copy diff may mutate state and is disabled."})
            return self._result("PARTIAL", record, local, issues)
        if operation in ("status", "diff"):
            if operation == "diff":
                issues.append({"code": "UNSUPPORTED_OPERATION", "message": "Scion working-copy diff may mutate state and is disabled."})
            return self._result("PARTIAL", record, local, issues)
        if type(limit) is not int or not 1 <= limit <= MAX_HISTORY:
            return self._failed(_Failure("INVALID_LIMIT", "History limit must be an integer between 1 and 100.", "NOT_CONFIGURED"), record)
        if revision is not None and not _matches(_SELECTOR, revision):
            return self._failed(_Failure("INVALID_REVISION", "Invalid revision selector.", "NOT_CONFIGURED"), record)
        selected = state["base"] if revision is None else revision
        command = "show" if operation == "inspect" else "log"
        args = [executable, "-C", str(checkout), "--json", command]
        args += [selected] if command == "show" else ["--limit", str(limit), "--rev", selected]
        try:
            # ctx.endpoint() eagerly reads working-copy config, even when the
            # profile endpoint wins. Validate this implicit read before the CLI.
            config = checkout / ".scion/config.toml"
            if config.exists() or config.is_symlink():
                _contained(project, config)
                _read_local(config, MAX_STATE_BYTES)
            output = _run(args, checkout, env)
            payload = _scion_payload(output.stdout, command, limit)
            _require(output.returncode == 0)
            if _scion_id(selected):
                _require(payload["id" if command == "show" else "from"] == selected)
            # The query may target a historical revision or server ref; it does
            # not replace the freshly observed local base in source_revision.
            record["availability"] = "AVAILABLE"
            if command == "show" and payload["filtered"] or command == "log" and any(e["filtered"] for e in payload["entries"]):
                issues.append({"code": "FILTERED_VIEW", "message": "Scion returned an authorization-filtered view."})
            return _scrub(self._result("PARTIAL", record, payload, issues), token)
        except _Failure as failure:
            return self._failed(failure, record)


def backend_for(root: Path, scope: VersionScope) -> VcsBackend:
    if not isinstance(scope, VersionScope):
        raise ValueError("scope must be a VersionScope")
    backend = {"GIT": GitBackend, "SCION": ScionBackend}.get(scope.backend)
    if backend is None:
        raise ValueError("unsupported VCS backend")
    return backend(root, scope)


def capture_revisions(root: Path, scopes) -> dict:
    """Sequential fresh observations, never a cross-repository atomic snapshot."""
    result = {}
    for scope in scopes:
        if scope.scope_id in result:
            raise ValueError("duplicate version scope id")
        result[scope.scope_id] = backend_for(root, scope).status()["source_revision"]
    return result
