"""Disposable real Git smoke tests; all Scion CLI responses are explicitly MOCKED.

No real Scion executable/server is used or claimed by this test module.
"""
from __future__ import annotations

import copy
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from dataclasses import FrozenInstanceError, replace
from pathlib import Path
from unittest.mock import patch

from tools.project_structure import vcs_backends as vcs
from tests.fixture_cleanup import cleanup_temporary_directory


SCION_ID = "c1" + "a" * 64
OTHER_ID = "c1" + "b" * 64
TOKEN = "test_mock-explicit-token-do-not-leak"


def mock_entry(identifier=SCION_ID):
    return {"id": identifier, "parents": [], "root_tree": "t1" + "c" * 64,
            "change_id": "d" * 32, "filtered": False,
            "meta": {"redacted": False, "meta_id": "m1" + "e" * 64,
                     "author": "Mock Author", "author_time": {"seconds": 1, "tz_offset_minutes": 0},
                     "committer": None, "committer_time": None, "creator": "mock", "message": "mock message"}}


def mock_envelope(command="show"):
    if command == "show":
        data = dict(mock_entry(), changes=[])
    else:
        data = {"from": SCION_ID, "path": None, "first_parent": False,
                "entries": [mock_entry()], "truncated": False}
    return {"schema": "scion.cli." + command + ".v1", "data": data}


def mock_output(envelope, returncode=0, stderr=b""):
    return vcs._Output(returncode, json.dumps(envelope).encode(), stderr)


class Fixture(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="dungeon-vcs-core-")
        self.addCleanup(cleanup_temporary_directory, temporary)
        self.base = Path(temporary.name).resolve()
        self.root = self.base / "project"
        self.root.mkdir()
        self.git_scope = vcs.VersionScope("main")
        self.scion_scope = vcs.VersionScope("experiment", "SCION", endpoint_profile="local-test")

    def scion_state(self, extra=""):
        metadata = self.root / ".scion"
        metadata.mkdir(exist_ok=True)
        (metadata / "state").write_text(
            f'base = "{SCION_ID}"\ntracking_ref = "main"\nsparse = ["src"]\ninaccessible = []\n' + extra,
            encoding="utf-8")
        return metadata / "state"

    def assert_layout(self, result):
        self.assertEqual(set(result), {"status", "availability", "source_revision", "data", "issues"})
        self.assertIn(result["status"], ("PASS", "PARTIAL", "FAIL"))
        self.assertEqual(result["availability"], result["source_revision"]["availability"])
        self.assertTrue(vcs.validate_revision(result["source_revision"]), result)
        self.assertIsInstance(result["data"], dict)
        self.assertTrue(all(isinstance(issue, dict) and "code" in issue and "message" in issue for issue in result["issues"]))


class RevisionContractTests(Fixture):
    def test_scope_is_frozen_git_default_and_manifest_serialization(self):
        self.assertEqual(self.git_scope.backend, "GIT")
        self.assertEqual(self.git_scope.as_dict(), {"id": "main", "backend": "GIT", "root": ".",
                         "mode": "MONOREPO", "repository": ".", "default_ref": None, "endpoint_profile": None})
        with self.assertRaises(FrozenInstanceError):
            self.git_scope.root = "elsewhere"
        self.assertIsInstance(vcs.backend_for(self.root, self.git_scope), vcs.GitBackend)
        with self.assertRaises(ValueError):
            vcs.backend_for(self.root, replace(self.git_scope, backend="OTHER"))

    def test_revision_namespace_lengths_and_unavailable_semantics(self):
        valid = {"schema_version": 1, "backend": "GIT", "scope_id": "main", "revision": "a" * 40,
                 "ref": "main", "dirty_state": "CLEAN", "availability": "AVAILABLE"}
        self.assertTrue(vcs.validate_revision(valid))
        for length in range(39, 66):
            self.assertEqual(vcs.validate_revision(dict(valid, revision="a" * length)), length in (40, 64))
        self.assertTrue(vcs.validate_revision(dict(valid, backend="SCION", revision=SCION_ID, dirty_state="UNKNOWN")))
        for value in (None, {}, [], "a" * 64, True):
            self.assertFalse(vcs.validate_revision(value))
        bad_changes = ({"schema_version": True}, {"schema_version": 2}, {"backend": "CAS"},
                       {"scope_id": "../escape"}, {"availability": "PARTIAL"}, {"availability": []},
                       {"dirty_state": "UNAVAILABLE"}, {"dirty_state": []}, {"revision": "sha256:" + "a" * 64},
                       {"revision": SCION_ID}, {"revision": None}, {"ref": "bad\nref"})
        for changes in bad_changes:
            with self.subTest(changes=changes):
                self.assertFalse(vcs.validate_revision(dict(valid, **changes)))
        for availability in vcs.AVAILABILITIES - {"AVAILABLE"}:
            record = dict(valid, availability=availability, revision=None, dirty_state="UNKNOWN")
            self.assertTrue(vcs.validate_revision(record))
            self.assertFalse(vcs.validate_revision(dict(record, dirty_state="CLEAN")))
        for identifier in ("a" * 64, "c1" + "A" * 64, "b1" + "a" * 64, "c2" + "a" * 64):
            self.assertFalse(vcs.validate_revision(dict(valid, backend="SCION", revision=identifier)))

    def test_paths_reject_external_traversal_devices_before_subprocess(self):
        values = ("../outside", str(self.base), "C:/outside", "C:relative", "//host/share", "x/../y",
                  "src:stream", "NUL", "dir. ", "dir/aux.txt")
        for value in values:
            for field in ("root", "repository"):
                with self.subTest(value=value, field=field), patch.object(vcs, "_run") as run:
                    result = vcs.backend_for(self.root, replace(self.git_scope, **{field: value})).status()
                    self.assertNotEqual(result["status"], "PASS")
                    run.assert_not_called()
        with patch.object(vcs, "_run") as run:
            result = vcs.backend_for(self.root / ".." / "project", self.git_scope).status()
            self.assertEqual(result["issues"][0]["code"], "UNSAFE_ROOT")
            run.assert_not_called()

    def test_mock_reparse_is_rejected_before_resolve(self):
        original = Path.lstat

        def reparse(path):
            if path == self.root:
                from types import SimpleNamespace
                return SimpleNamespace(st_mode=0o040755, st_file_attributes=0x400)
            return original(path)

        with patch.object(Path, "lstat", reparse), patch.object(vcs, "_run") as run:
            result = vcs.backend_for(self.root, self.git_scope).status()
        self.assertEqual(result["issues"][0]["code"], "UNSAFE_ROOT")
        run.assert_not_called()

    def test_mock_git_root_mismatch_never_reads_head_or_status(self):
        (self.root / ".git").mkdir()
        with patch.object(vcs.GitBackend, "_git", return_value=str(self.base)) as git:
            result = vcs.backend_for(self.root, self.git_scope).status()
        self.assertEqual(result["issues"][0]["code"], "ROOT_MISMATCH")
        self.assertEqual(git.call_count, 1)


@unittest.skipUnless(shutil.which("git"), "Git executable is unavailable")
class RealGitSmokeTests(Fixture):
    def git(self, *args):
        environment = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
        environment.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull)
        return subprocess.run([shutil.which("git"), "-C", str(self.root), *args], env=environment,
                              check=True, capture_output=True, text=True, timeout=10).stdout.strip()

    def initialize(self):
        self.git("init", "-b", "main")
        self.git("config", "user.name", "Disposable Fixture")
        self.git("config", "user.email", "fixture@example.invalid")
        self.git("config", "core.autocrlf", "false")
        (self.root / "tracked.txt").write_text("before\n", encoding="utf-8")
        self.git("add", "tracked.txt")
        self.git("commit", "-m", "fixture initial")
        return vcs.backend_for(self.root, self.git_scope)

    def metadata_bytes(self):
        return {p.relative_to(self.root).as_posix(): (p.read_bytes(), p.stat().st_mtime_ns)
                for p in (self.root / ".git").rglob("*") if p.is_file()}

    def test_real_git_clean_history_inspect_diff_preserve_metadata(self):
        backend = self.initialize()
        before = self.metadata_bytes()
        for result in (backend.status(), backend.inspect(), backend.history(), backend.diff()):
            self.assert_layout(result)
            self.assertEqual(result["status"], "PASS")
            self.assertEqual(result["source_revision"]["dirty_state"], "CLEAN")
        self.assertEqual(backend.history()["data"]["entries"][0]["id"], self.git("rev-parse", "HEAD"))
        self.assertEqual(backend.diff()["data"]["patch"], "")
        self.assertEqual(before, self.metadata_bytes())
        self.assertFalse((self.root / ".scion").exists())

    def test_real_git_staged_unstaged_untracked_and_detached(self):
        backend = self.initialize()
        initial = self.git("rev-parse", "HEAD")
        (self.root / "tracked.txt").write_text("staged\n", encoding="utf-8")
        self.git("add", "tracked.txt")
        (self.root / "tracked.txt").write_text("unstaged\n", encoding="utf-8")
        (self.root / "untracked.txt").write_text("new\n", encoding="utf-8")
        before = self.metadata_bytes()
        result = backend.diff()
        self.assertEqual(result["source_revision"]["dirty_state"], "DIRTY")
        self.assertIn("+unstaged", result["data"]["patch"])
        self.assertFalse(result["data"]["includes_untracked"])
        self.assertEqual(before, self.metadata_bytes())
        self.git("add", ".")
        self.git("commit", "-m", "second fixture")
        historical = backend.inspect(initial)
        self.assertEqual(historical["data"]["id"], initial)
        self.assertNotEqual(historical["source_revision"]["revision"], initial)
        self.git("checkout", "--detach", "HEAD")  # disposable fixture setup only
        self.assertIsNone(backend.status()["source_revision"]["ref"])

    def test_real_git_no_ancestor_fallback_or_external_metadata(self):
        self.initialize()
        child = self.root / "child"
        child.mkdir()
        result = vcs.backend_for(self.root, replace(self.git_scope, root="child")).status()
        self.assertEqual(result["availability"], "NOT_CONFIGURED")
        (child / ".git").write_text("gitdir: " + str(self.root / ".git"), encoding="utf-8")
        # A .git indirection does not grant an external authorized root.
        outside = self.base / "outside"
        outside.mkdir()
        (outside / ".git").write_text("gitdir: " + str(self.root / ".git"), encoding="utf-8")
        with patch.object(vcs, "_run") as run:
            result = vcs.backend_for(outside, self.git_scope).status()
        self.assertEqual(result["issues"][0]["code"], "UNSAFE_ROOT")
        run.assert_not_called()

    def test_real_git_repository_mode_with_a_contained_nested_checkout(self):
        self.initialize()
        backend = vcs.backend_for(self.base, replace(self.git_scope, mode="REPOSITORY", root="project", repository="project"))
        result = backend.status()
        self.assert_layout(result)
        self.assertEqual(result["status"], "PASS")

    def test_real_git_ambient_overrides_and_filters_cannot_redirect_observation(self):
        backend = self.initialize()
        expected = self.git("rev-parse", "HEAD")
        self.git("config", "core.fsmonitor", "untrusted-command-must-not-execute")
        self.git("config", "diff.external", "untrusted-command-must-not-execute")
        (self.root / "tracked.txt").write_text("changed\n", encoding="utf-8")
        with patch.dict(os.environ, {"GIT_DIR": str(self.base / "absent"), "GIT_WORK_TREE": str(self.base),
                                     "GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "core.fsmonitor",
                                     "GIT_CONFIG_VALUE_0": "untrusted-command-must-not-execute"}):
            result = backend.diff()
        self.assertEqual(result["status"], "PASS")
        self.assertEqual(result["source_revision"]["revision"], expected)

    def test_real_git_mixed_capture_freshness_and_duplicate_ids(self):
        self.initialize()
        self.scion_state()
        records = vcs.capture_revisions(self.root, [self.git_scope, self.scion_scope])
        self.assertTrue(all(vcs.validate_revision(record) for record in records.values()))
        self.assertEqual(records["main"]["availability"], "AVAILABLE")
        self.assertEqual(records["experiment"]["revision"], SCION_ID)
        self.assertEqual(records["experiment"]["dirty_state"], "UNKNOWN")
        (self.root / "tracked.txt").write_text("fresh change\n", encoding="utf-8")
        self.assertEqual(vcs.capture_revisions(self.root, [self.git_scope])["main"]["dirty_state"], "DIRTY")
        with self.assertRaises(ValueError):
            vcs.capture_revisions(self.root, [self.git_scope, self.git_scope])


class MockScionTests(Fixture):
    def setUp(self):
        super().setUp()
        self.state_path = self.scion_state()
        self.backend = vcs.backend_for(self.root, self.scion_scope)
        self.environment = {"DUNGEON_SCION_LOCAL_TEST_ENDPOINT": "https://scion.example.invalid:8443",
                            "DUNGEON_SCION_LOCAL_TEST_TOKEN": TOKEN}

    def configured(self):
        # Explicit mock of supported Unix execution; does not run Scion or WSL.
        return patch.object(self.backend, "_configuration", return_value=("/trusted/scion", {
            "SCION_ENDPOINT": self.environment["DUNGEON_SCION_LOCAL_TEST_ENDPOINT"], "SCION_TOKEN": TOKEN}, TOKEN))

    def test_mock_native_windows_and_wsl_are_unsupported_without_process(self):
        before = self.state_path.read_bytes()
        for transport in ("native", "wsl"):
            with patch.dict(os.environ, dict(self.environment, DUNGEON_SCION_LOCAL_TEST_TRANSPORT=transport)), patch.object(vcs, "_NATIVE_WINDOWS", True), patch.object(vcs, "_run") as run:
                result = self.backend.status()
            self.assertEqual(result["status"], "PARTIAL")
            self.assertEqual(result["availability"], "UNSUPPORTED_PLATFORM")
            self.assertEqual(result["source_revision"]["revision"], SCION_ID)
            self.assert_layout(result)
            run.assert_not_called()
        self.assertEqual(before, self.state_path.read_bytes())

    def test_local_state_status_and_diff_never_invoke_cli_even_when_configured(self):
        before = {p.name: p.read_bytes() for p in self.state_path.parent.iterdir()}
        with self.configured(), patch.object(vcs, "_run") as run:
            for operation in (self.backend.status, self.backend.diff):
                result = operation()
                self.assert_layout(result)
                self.assertEqual(result["status"], "PARTIAL")
                self.assertEqual(result["source_revision"]["dirty_state"], "UNKNOWN")
            run.assert_not_called()
        self.assertEqual(before, {p.name: p.read_bytes() for p in self.state_path.parent.iterdir()})

    def test_pending_operation_invalidates_local_revision_and_does_not_recover(self):
        self.scion_state(f'\n[pending.Goto]\nfrom = "{SCION_ID}"\nto = "{OTHER_ID}"\n')
        before = self.state_path.read_bytes()
        with self.configured(), patch.object(vcs, "_run") as run:
            for operation in (self.backend.status, self.backend.inspect, self.backend.history, self.backend.diff):
                result = operation()
                self.assert_layout(result)
                self.assertIsNone(result["source_revision"]["revision"])
                self.assertEqual(result["source_revision"]["availability"], "UNKNOWN")
                self.assertEqual(result["issues"][0]["code"], "PENDING_OPERATION")
            run.assert_not_called()
        self.assertEqual(self.state_path.read_bytes(), before)

    def test_no_working_copy_no_initialization_and_no_parent_fallback(self):
        child = self.root / "child"
        child.mkdir()
        with patch.object(vcs, "_run") as run:
            result = vcs.backend_for(self.root, replace(self.scion_scope, root="child")).inspect()
        self.assertEqual(result["availability"], "NOT_CONFIGURED")
        self.assertFalse((child / ".scion").exists())
        run.assert_not_called()

    def test_local_sparse_root_and_lossless_hex_match_upstream_toml(self):
        self.scion_state('')
        text = self.state_path.read_text(encoding="utf-8").replace('sparse = ["src"]', 'sparse = ["", { hex = "7372632fff" }]')
        self.state_path.write_text(text, encoding="utf-8")
        with self.configured(), patch.object(vcs, "_run") as run:
            result = self.backend.status()
        self.assert_layout(result)
        self.assertEqual(result["source_revision"]["revision"], SCION_ID)
        run.assert_not_called()

    def test_sparse_text_and_hex_reject_traversal_devices_and_nul(self):
        for prefix in ("../escape", "/outside", "a//b", "a/../b", "NUL", "C:/outside", ".SCION", "a\\b", "a\x00b", "a" * 256):
            for representation in (json.dumps(prefix), '{ hex = "' + prefix.encode().hex() + '" }'):
                self.scion_state()
                text = self.state_path.read_text(encoding="utf-8").replace('sparse = ["src"]', 'sparse = [' + representation + ']')
                self.state_path.write_text(text, encoding="utf-8")
                with self.configured(), patch.object(vcs, "_run") as run:
                    result = self.backend.status()
                self.assertEqual(result["status"], "FAIL", representation)
                self.assertIsNone(result["source_revision"]["revision"])
                run.assert_not_called()

    def test_mock_implicit_working_copy_config_read_rejects_reparse(self):
        config = self.state_path.parent / "config.toml"
        config.write_text('endpoint = "https://ignored.invalid"', encoding="utf-8")
        original = Path.lstat

        def reparse(path):
            if path == config:
                from types import SimpleNamespace
                return SimpleNamespace(st_mode=0o100644, st_file_attributes=0x400)
            return original(path)

        with self.configured(), patch.object(Path, "lstat", reparse), patch.object(vcs, "_run") as run:
            result = self.backend.inspect()
        self.assertEqual(result["status"], "FAIL")
        self.assertEqual(result["issues"][0]["code"], "UNSAFE_ROOT")
        run.assert_not_called()

    def test_mock_valid_show_log_exact_argv_environment_and_scrubbing(self):
        for method, command in ((self.backend.inspect, "show"), (self.backend.history, "log")):
            envelope = mock_envelope(command)
            entry = envelope["data"] if command == "show" else envelope["data"]["entries"][0]
            entry["meta"]["message"] = "server echoed " + TOKEN
            with self.configured(), patch.object(vcs, "_run", return_value=mock_output(envelope, stderr=TOKEN.encode())) as run:
                result = method()
            self.assert_layout(result)
            self.assertEqual(result["status"], "PARTIAL")
            self.assertEqual(result["availability"], "AVAILABLE")
            self.assertNotIn(TOKEN, json.dumps(result))
            argv, cwd, env = run.call_args.args
            expected = ["/trusted/scion", "-C", str(self.root), "--json", command]
            expected += [SCION_ID] if command == "show" else ["--limit", "20", "--rev", SCION_ID]
            self.assertEqual(argv, expected)
            self.assertEqual(cwd, self.root)
            self.assertEqual(env["SCION_TOKEN"], TOKEN)
            self.assertNotIn(TOKEN, " ".join(argv))

    def test_mock_error_schema_auth_network_and_diagnostics_are_sanitized(self):
        for code, availability in (("UNAUTHENTICATED", "AUTH_REQUIRED"), ("PERMISSION_DENIED", "AUTH_REQUIRED"),
                                   ("UNAVAILABLE", "SERVER_UNREACHABLE"), ("NOT_FOUND", "UNKNOWN")):
            error = {"schema": "scion.cli.error.v1", "data": {"code": code, "message": TOKEN, "exit_code": 1}}
            with self.configured(), patch.object(vcs, "_run", return_value=mock_output(error, 1, TOKEN.encode())):
                result = self.backend.inspect()
            self.assert_layout(result)
            self.assertEqual(result["status"], "FAIL")
            self.assertEqual(result["availability"], availability)
            self.assertNotIn(TOKEN, json.dumps(result))

    def test_mock_json_recursively_rejects_partial_invalid_and_wrong_namespace(self):
        valid = mock_envelope()
        malformed = [b"{", b"[]", b'{"schema":"scion.cli.show.v1","schema":"scion.cli.show.v1","data":{}}',
                     b'{"schema":"scion.cli.show.v1","data":{"number":NaN}}']
        for keys, value in ((["schema"], "scion.cli.show.v2"), (["data", "id"], "a" * 64),
                            (["data", "root_tree"], SCION_ID), (["data", "parents"], ["bad"]),
                            (["data", "filtered"], "false"), (["data", "meta", "redacted"], 0),
                            (["data", "meta", "meta_id"], SCION_ID),
                            (["data", "meta", "author_time", "seconds"], True),
                            (["data", "meta", "author_time", "seconds"], 2**64),
                            (["data", "changes"], [{}])):
            envelope = copy.deepcopy(valid)
            target = envelope
            for key in keys[:-1]:
                target = target[key]
            target[keys[-1]] = value
            malformed.append(json.dumps(envelope).encode())
        for key in valid["data"]:
            envelope = copy.deepcopy(valid)
            del envelope["data"][key]
            malformed.append(json.dumps(envelope).encode())
        for raw in malformed:
            with self.subTest(raw=raw[:160]), self.configured(), patch.object(vcs, "_run", return_value=vcs._Output(0, raw, b"")):
                result = self.backend.inspect()
            self.assertEqual(result["status"], "FAIL")
            self.assertNotEqual(result["availability"], "AVAILABLE")

    def test_mock_nested_show_hunks_and_modes_are_semantically_validated(self):
        envelope = mock_envelope()
        change = {"path": "src/a.rs", "status": "modified", "binary": False,
                  "old_id": "b1" + "1" * 64, "new_id": "b1" + "2" * 64, "old_mode": 1, "new_mode": 1,
                  "hunks": [{"old_start": 1, "old_lines": 1, "new_start": 1, "new_lines": 1, "lines": ["-old", "+new"]}]}
        envelope["data"]["changes"] = [change]
        with self.configured(), patch.object(vcs, "_run", return_value=mock_output(envelope)):
            self.assertEqual(self.backend.inspect()["availability"], "AVAILABLE")
        for mutate in (lambda c: c.update(new_mode=True), lambda c: c.update(new_id=SCION_ID),
                       lambda c: c["hunks"][0].update(new_lines=2), lambda c: c["hunks"][0].update(old_start=-1),
                       lambda c: c["hunks"][0].update(lines=["invalid"]), lambda c: c.update(binary=True)):
            bad = copy.deepcopy(envelope)
            mutate(bad["data"]["changes"][0])
            with self.configured(), patch.object(vcs, "_run", return_value=mock_output(bad)):
                self.assertEqual(self.backend.inspect()["status"], "FAIL")

    def test_mock_log_counts_revision_and_nonzero_success_are_rejected(self):
        cases = []
        for key, value in (("entries", []), ("truncated", "false"), ("from", OTHER_ID)):
            bad = mock_envelope("log")
            bad["data"][key] = value
            cases.append(mock_output(bad))
        bad = mock_envelope("log")
        bad["data"]["entries"] *= 2
        cases.append(mock_output(bad))
        cases.append(mock_output(mock_envelope("log"), 1))
        for output in cases:
            with self.configured(), patch.object(vcs, "_run", return_value=output):
                self.assertEqual(self.backend.history(limit=1)["status"], "FAIL")
        bad = mock_envelope()
        bad["data"]["id"] = OTHER_ID
        with self.configured(), patch.object(vcs, "_run", return_value=mock_output(bad)):
            self.assertEqual(self.backend.inspect()["status"], "FAIL")

    def test_mock_missing_token_disables_home_credentials_and_reexchange(self):
        with patch.dict(os.environ, {"DUNGEON_SCION_LOCAL_TEST_ENDPOINT": "unix:///tmp/scion.sock"}, clear=True), patch.object(vcs, "_NATIVE_WINDOWS", False), patch.object(vcs, "_executable") as executable, patch.object(vcs, "_run") as run:
            result = self.backend.inspect()
        self.assertEqual(result["availability"], "AUTH_REQUIRED")
        self.assert_layout(result)
        executable.assert_not_called()
        run.assert_not_called()

    def test_mock_configuration_token_env_and_fixed_executable(self):
        with patch.dict(os.environ, dict(self.environment, SCION_SERVICE_SECRET="never-use", SCION_CA_FILE="ignore-ambient", SCION_TOKEN="ignore-ambient")), patch.object(vcs, "_NATIVE_WINDOWS", False), patch.object(vcs, "_executable", return_value="/trusted/scion") as executable:
            program, environment, token = self.backend._configuration()
        executable.assert_called_once_with("scion")
        self.assertEqual(program, "/trusted/scion")
        self.assertEqual(token, TOKEN)
        self.assertEqual(environment["SCION_TOKEN"], TOKEN)
        self.assertNotIn("SCION_SERVICE_SECRET", environment)
        self.assertNotIn("SCION_CA_FILE", environment)
        self.assertNotIn("HOME", environment)
        self.assertFalse(any(key.startswith("DUNGEON_SCION_") for key in environment))

    def test_invalid_endpoints_are_not_executed_and_never_echoed(self):
        invalid = ("http://host", "https://user:" + TOKEN + "@host", "https://host?token=" + TOKEN,
                   "https://host/#token", "https://host/route", "https://host:", "https://host:0", "https://ho st",
                   "unix://relative", "unix:///", "unix:///tmp/../secret", "https://%40host")
        for endpoint in invalid:
            with patch.dict(os.environ, dict(self.environment, DUNGEON_SCION_LOCAL_TEST_ENDPOINT=endpoint)), patch.object(vcs, "_run") as run:
                result = self.backend.inspect()
            self.assertEqual(result["availability"], "NOT_CONFIGURED")
            self.assertNotIn(TOKEN, json.dumps(result))
            run.assert_not_called()

    def test_mock_timeout_missing_executable_and_bad_selectors(self):
        for failure in (vcs._Failure("TIMEOUT", "VCS query exceeded its time limit.", "SERVER_UNREACHABLE"),
                        vcs._Failure("CLIENT_UNAVAILABLE", "VCS executable is not installed.", "UNAVAILABLE")):
            with self.configured(), patch.object(vcs, "_run", side_effect=failure):
                result = self.backend.inspect()
            self.assertEqual(result["availability"], failure.availability)
            self.assertEqual(result["status"], "FAIL")
        with self.configured(), patch.object(vcs, "_run") as run:
            for selector in ("--help", "bad\nrevision", "x;command", TOKEN + " "):
                self.assertEqual(self.backend.inspect(selector)["status"], "FAIL")
            for limit in (True, 0, 101, "20"):
                self.assertEqual(self.backend.history(limit)["status"], "FAIL")
            run.assert_not_called()

    def test_malformed_and_oversized_local_state_never_become_fresh(self):
        for value in ('base = "bad"\nsparse = []\ninaccessible = []', 'base = "unterminated', " " * (vcs.MAX_STATE_BYTES + 1)):
            self.state_path.write_text(value, encoding="utf-8")
            with patch.object(vcs, "_run") as run:
                result = self.backend.status()
            self.assertIsNone(result["source_revision"]["revision"])
            self.assertEqual(result["status"], "FAIL")
            run.assert_not_called()


class RealBoundedRunnerTests(Fixture):
    """Run only the explicit test Python interpreter, never real Scion."""
    def test_real_output_budget_applies_during_stdout_and_stderr_reads(self):
        script = "import os; os.write(1,b'x'*4096); os.write(2,b'y'*4096)"
        with patch.object(vcs, "MAX_OUTPUT_BYTES", 1024):
            with self.assertRaises(vcs._Failure) as caught:
                vcs._run([sys.executable, "-c", script], self.root, dict(os.environ))
        self.assertEqual(caught.exception.code, "OUTPUT_LIMIT")

    def test_real_timeout_kills_descendant_that_holds_output_pipe(self):
        # Parent exits immediately; its child keeps the pipe open. Both the
        # private POSIX group and Windows job must still be terminated.
        script = "import subprocess,sys,pathlib; p=subprocess.Popen([sys.executable,'-c','import time; time.sleep(30)']); pathlib.Path('child.pid').write_text(str(p.pid)); print(p.pid,flush=True)"
        started = time.monotonic()
        with patch.object(vcs, "TIMEOUT_SECONDS", 0.5):
            with self.assertRaises(vcs._Failure) as caught:
                vcs._run([sys.executable, "-c", script], self.root, dict(os.environ))
        self.assertEqual(caught.exception.code, "TIMEOUT")
        self.assertLess(time.monotonic() - started, 4)
        pid = int((self.root / "child.pid").read_text())
        if os.name == "nt":
            import ctypes
            from ctypes import wintypes
            api = ctypes.WinDLL("kernel32", use_last_error=True)
            api.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
            api.OpenProcess.restype = wintypes.HANDLE
            api.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
            api.CloseHandle.argtypes = [wintypes.HANDLE]
            handle = api.OpenProcess(0x100000, False, pid)  # SYNCHRONIZE
            if handle:
                try:
                    self.assertEqual(api.WaitForSingleObject(handle, 1000), 0)
                finally:
                    api.CloseHandle(handle)
        else:
            proc_state = Path("/proc") / str(pid) / "stat"
            if proc_state.exists():
                self.assertEqual(proc_state.read_text().split()[2], "Z")
            else:
                with self.assertRaises(ProcessLookupError):
                    os.kill(pid, 0)


if __name__ == "__main__":
    unittest.main()
