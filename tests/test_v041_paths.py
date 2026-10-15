"""Actual Windows writers: external bytes are canaries, never test outputs."""
import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from tools.project_structure.cli import main
from tools.project_structure.scanner import scan_project
from tools.project_structure.projections import write_projections
from tools.project_structure.snapshots import create_snapshot
from tools.project_structure.v03_cas import cas_put
from tools.project_structure.v03_metadata import atomic_write_json
from tools.project_structure.v041_paths import (UnsafeOutputPath, PublicationError,
    output_boundary, atomic_bytes, publish_files, validate_root)


SKLT = """schema_version: 1
id: fixture.object
object_id: fixture.object
kind: component
responsibility: fixture
inputs:
outputs:
depends_on:
effects:
state: SKLT
"""


def tree(root):
    return {str(p.relative_to(root)): p.read_bytes() for p in root.rglob("*") if p.is_file()}


class OutputBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="dungeon-hardening-")
        self.base = Path(self.temp.name)
        self.root = self.base / "project"
        self.root.mkdir()
        (self.root / "render").mkdir()
        (self.root / "render/object.sklt").write_text(SKLT)
        (self.root / ".project").mkdir()
        (self.root / ".project/project.json").write_text(json.dumps({
            "schema_version": 1, "project_id": "fixture", "modules": [{"id": "render", "path": "render"}],
            "docs": {}, "evidence_store": {"type": "local-cas", "root": "../store"}}))
        self.external = self.base / "canary"
        self.external.mkdir()
        (self.external / "keep.txt").write_bytes(b"must not change")
        self.links = []

    def tearDown(self):
        # Delete only exact fixture-owned junction entries, never their targets.
        for link in reversed(self.links):
            if os.path.lexists(link):
                os.rmdir(link)
        from tests.fixture_cleanup import cleanup_temporary_directory
        cleanup_temporary_directory(self.temp)  # Exhausted/other errors remain failures.

    def junction(self, path, target=None):
        target = target or self.external
        result = subprocess.run(["cmd.exe", "/d", "/c", "mklink", "/J", str(path), str(target)], capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(path.is_junction())
        self.links.append(path)

    def test_refresh_docs_junction_no_first_write_cli_and_direct_writer(self):
        self.junction(self.root / "docs")
        before = tree(self.external)
        snapshot = scan_project(self.root)
        with self.assertRaises(UnsafeOutputPath):
            write_projections(snapshot)
        self.assertFalse((self.root / "render/.exp").exists())
        with contextlib.redirect_stdout(io.StringIO()) as output:
            code = main(["refresh", "--root", str(self.root), "--json"])
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(output.getvalue())["issues"][0]["code"], "UNSAFE_OUTPUT_PATH")
        self.assertEqual(tree(self.external), before)
        self.assertFalse((self.external / ".grade").exists())
        self.assertFalse((self.root / "render/.exp").exists())
        self.assertFalse((self.root / ".project/objects.json").exists())

    def test_existing_external_grade_is_not_overwritten(self):
        (self.external / ".grade").write_bytes(b"customer grade")
        self.junction(self.root / "docs")
        before = tree(self.external)
        with self.assertRaises(UnsafeOutputPath):
            write_projections(scan_project(self.root))
        self.assertEqual(tree(self.external), before)

    def test_snapshot_parent_junction_is_rejected(self):
        self.junction(self.root / "snapshots")
        before = tree(self.external)
        with self.assertRaises(UnsafeOutputPath):
            create_snapshot(scan_project(self.root), "checkpoint")
        self.assertEqual(tree(self.external), before)

    def test_index_metadata_parent_junction_is_rejected(self):
        redirected = self.root / "metadata"
        self.junction(redirected)
        before = tree(self.external)
        with self.assertRaises(UnsafeOutputPath):
            atomic_write_json(redirected / "objects.json", {}, root=self.root)
        self.assertEqual(tree(self.external), before)

    def test_cas_parent_junction_is_rejected(self):
        store = self.base / "store"
        store.mkdir()
        self.junction(store / "cas")
        before = tree(self.external)
        with self.assertRaises(UnsafeOutputPath):
            cas_put(self.root, self.root / "render/object.sklt")
        self.assertEqual(tree(self.external), before)

    def test_configured_cas_alias_is_rejected(self):
        self.junction(self.base / "store")
        before = tree(self.external)
        with self.assertRaises(UnsafeOutputPath):
            cas_put(self.root, self.root / "render/object.sklt")
        self.assertEqual(tree(self.external), before)

    def test_raw_alias_traversal_unc_and_hardlink_are_rejected(self):
        alias = self.base / "alias"
        self.junction(alias, self.root)
        for root in (alias, self.root / ".." / "project", Path(r"\\localhost\C$\project")):
            with self.subTest(root=str(root)), self.assertRaises(UnsafeOutputPath):
                validate_root(root)
        target = self.root / "linked.json"
        os.link(self.external / "keep.txt", target)
        before = tree(self.external)
        with self.assertRaises(UnsafeOutputPath):
            atomic_bytes(self.root, target, b"overwrite")
        self.assertEqual(tree(self.external), before)

    def test_pinned_ancestor_cannot_be_replaced_and_handle_is_released(self):
        parent = self.root / "mutable"
        parent.mkdir()
        with output_boundary(self.root, [parent / "result.json"]):
            with self.assertRaises(OSError):
                parent.rename(self.root / "renamed")
            atomic_bytes(self.root, parent / "result.json", b"safe")
        parent.rename(self.root / "renamed")
        self.assertEqual((self.root / "renamed/result.json").read_bytes(), b"safe")

    def test_absolute_and_traversal_outputs_cannot_escape_root(self):
        before = tree(self.external)
        for target in (self.external / "outside.json", self.root / ".." / "canary/outside.json"):
            with self.subTest(target=str(target)), self.assertRaises(UnsafeOutputPath):
                atomic_bytes(self.root, target, b"must not be published")
        self.assertEqual(tree(self.external), before)

    def test_live_stage_lease_denies_external_hardlink_and_write(self):
        from tools.project_structure.v041_paths import staged_file
        alias = self.external / "stage-alias"
        before = tree(self.external)
        with staged_file(self.root, self.root) as stage:
            with self.assertRaises(OSError) as caught:
                os.link(stage.path, alias)
            self.assertIn(getattr(caught.exception, "winerror", None), (5, 32, 33))
            with self.assertRaises(OSError):
                stage.path.write_bytes(b"attacker data")
            stage.write(b"only owned stage bytes")
            stage.flush()
            self.assertEqual(stage.read_bytes(), b"only owned stage bytes")
        self.assertFalse(alias.exists())
        self.assertEqual(tree(self.external), before)

    def test_task_and_coverage_actual_writers_reject_metadata_junction(self):
        from tools.project_structure.v04_trace import task_trace_write, coverage_set
        self.junction(self.root / ".project/task-traces")
        before = tree(self.external)
        result = task_trace_write(self.root, {})
        self.assertEqual(result["status"], "FAIL", result)
        self.assertIn("UNSAFE_OUTPUT_PATH", [item["code"] for item in result["issues"]])
        self.assertEqual(tree(self.external), before)
        os.rmdir(self.links.pop())
        self.junction(self.root / ".project/coverage")
        result = coverage_set(self.root, "fixture.object", "PROTOTYPE", "UNKNOWN", "bounded fixture")
        self.assertEqual(result["status"], "FAIL", result)
        self.assertIn("UNSAFE_OUTPUT_PATH", [item["code"] for item in result["issues"]])
        self.assertEqual(tree(self.external), before)

    def test_preflight_all_paths_before_any_publication(self):
        self.junction(self.root / "unsafe")
        with self.assertRaises(UnsafeOutputPath):
            publish_files(self.root, {self.root / "first": b"first", self.root / "unsafe/last": b"last"})
        self.assertFalse((self.root / "first").exists())

    def test_locked_publication_rolls_back_owned_files(self):
        first = self.root / "first"
        last = self.root / "last"
        first.write_bytes(b"before-first")
        last.write_bytes(b"before-last")
        from ctypes import wintypes
        import ctypes
        api = ctypes.WinDLL("kernel32", use_last_error=True)
        api.CreateFileW.restype = wintypes.HANDLE
        api.CreateFileW.argtypes = (wintypes.LPCWSTR,wintypes.DWORD,wintypes.DWORD,ctypes.c_void_p,wintypes.DWORD,wintypes.DWORD,wintypes.HANDLE)
        api.CloseHandle.argtypes = (wintypes.HANDLE,)
        handle = api.CreateFileW(str(last), 0x80000000, 1, None, 3, 0, None)
        self.assertNotEqual(handle, ctypes.c_void_p(-1).value)
        try:
            with self.assertRaises(PublicationError) as caught:
                publish_files(self.root, {first: b"after", last: b"after"})
            self.assertEqual(caught.exception.residual, [])
            self.assertEqual(first.read_bytes(), b"before-first")
            self.assertEqual(last.read_bytes(), b"before-last")
        finally:
            api.CloseHandle(handle)

    def test_new_files_are_removed_on_later_publish_failure(self):
        first = self.root / "new-first"
        last = self.root / "last"
        last.write_bytes(b"before")
        from tools.project_structure import v041_paths as paths
        real_rename = paths._rename_handle
        def fail_last(lease, destination):
            if destination == last and lease.path.name.startswith(".dungeon-stage-"):
                raise OSError("fixture later publication failed")
            return real_rename(lease, destination)
        with patch.object(paths, "_rename_handle", side_effect=fail_last):
            with self.assertRaises(PublicationError) as caught:
                publish_files(self.root, {first: b"after", last: b"after"})
        self.assertEqual(caught.exception.residual, [])
        self.assertFalse(first.exists())
        self.assertEqual(last.read_bytes(), b"before")

    def test_interrupted_and_concurrent_publication_fail_closed(self):
        first = self.root / "first"
        last = self.root / "last"
        first.write_bytes(b"before")
        last.write_bytes(b"before")
        from tools.project_structure import v041_paths as paths
        real_rename = paths._rename_handle
        def interrupted(lease, destination):
            if destination == last and lease.path.name.startswith(".dungeon-stage-"):
                raise KeyboardInterrupt("fixture interruption")
            return real_rename(lease, destination)
        with patch.object(paths, "_rename_handle", side_effect=interrupted):
            with self.assertRaises(KeyboardInterrupt):
                publish_files(self.root, {first: b"after", last: b"after"})
        self.assertEqual(first.read_bytes(), b"before")
        (self.root / ".dungeon-publish.lock").write_bytes(b"concurrent owner")
        with self.assertRaises(PublicationError):
            publish_files(self.root, {first: b"after"})
        self.assertEqual(first.read_bytes(), b"before")
        self.assertEqual((self.root / ".dungeon-publish.lock").read_bytes(), b"concurrent owner")

    def test_positive_refresh_and_snapshot(self):
        with contextlib.redirect_stdout(io.StringIO()) as output:
            code = main(["refresh", "--root", str(self.root), "--json"])
        self.assertEqual(code, 0, output.getvalue())
        self.assertTrue((self.root / ".project/objects.json").exists())
        self.assertEqual(create_snapshot(scan_project(self.root), "safe"), [])
        self.assertTrue((self.root / "snapshots/safe/manifest.json").is_file())

    def test_ignored_fixture_artifacts_do_not_pollute_structural_validation(self):
        (self.root / "artifacts").mkdir()
        (self.root / "artifacts/not-a-project.sklt").write_text("invalid fixture dialect")
        manifest = self.root / ".project/project.json"
        data = json.loads(manifest.read_text())
        data["ignore"] = ["artifacts"]
        data["modules"] = []
        manifest.write_text(json.dumps(data))
        with contextlib.redirect_stdout(io.StringIO()) as output:
            code = main(["refresh", "--root", str(self.root), "--json"])
        self.assertEqual(code, 0, output.getvalue())
        index = json.loads((self.root / ".project/objects.json").read_text())
        self.assertEqual(list(index["objects"]), ["fixture.object"])
        self.assertFalse((self.root / "artifacts/.exp").exists())

    def test_inspect_missing_or_stale_index_is_read_only(self):
        before = tree(self.root)
        with contextlib.redirect_stdout(io.StringIO()) as output:
            code = main(["inspect", "fixture.object", "--root", str(self.root), "--json"])
        self.assertEqual(code, 0, output.getvalue())
        payload = json.loads(output.getvalue())
        self.assertTrue(payload["index_computed_in_memory"])
        self.assertFalse(payload["index_rebuilt"])
        self.assertEqual(tree(self.root), before)
        stale = self.root / ".project/objects.json"
        stale.write_bytes(b'{"schema":"dungeon-object-index/v1","objects":{},"input_hash":"stale"}')
        before = tree(self.root)
        with contextlib.redirect_stdout(io.StringIO()) as output:
            code = main(["inspect", "fixture.object", "--root", str(self.root), "--json"])
        self.assertEqual(code, 0, output.getvalue())
        self.assertEqual(tree(self.root), before)
