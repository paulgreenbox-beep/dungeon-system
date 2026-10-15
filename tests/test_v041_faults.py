"""Bounded mutation/interrupt regressions, using the real writer APIs.

Run with bundled Python -B -m unittest -v tests.test_v041_faults on Windows.
Original hook points (before fixing the implementation):
  CAS: v03_cas.os.link, after _copy_hash/flush/hash comparison.
  Snapshot: snapshots.os.rename, after release_subtree(stage).
  Leaf: v041_paths.os.replace, after the last expected-bytes comparison.
  Adoption: v04_projects._atomic_bytes, before render/.exp, after other outputs.
For handle-native publication the first three hooks move to _rename_handle;
the hook still calls the original primitive and never disables sharing leases.

This is controlled mutation by an ordinary same-user file opener at a selected
boundary, NOT a proof against a hostile process with preexisting writable
mappings, duplicated handles, elevated privileges, or writes after return.
Forward contract: mutation is kernel-denied OR detected/fails closed; an inserted
destination is preserved, with any retired original explicitly recoverable.
No expectedFailure/skip-on-failure: these tests must gate the corresponding fix.
"""
from __future__ import annotations

import contextlib
import ctypes
import stat
import errno
import hashlib
import json
import os
from pathlib import Path
import tempfile
import struct
import time
import unittest
from unittest.mock import patch

from tools.project_structure import snapshots, v03_cas as cas, v041_paths as paths
from tools.project_structure import v04_projects as projects
from tools.project_structure.scanner import scan_project


SKLT = """schema_version: 1
id: fixture.object
object_id: fixture.object
kind: component
responsibility: bounded fault fixture
inputs:
outputs:
depends_on:
effects:
state: SKLT
"""


def tree(root):
    result = {}
    for path in root.rglob("*"):
        try:
            info = path.lstat()  # Do not classify an already absent enumeration entry as a directory.
        except FileNotFoundError:
            continue
        result[path.relative_to(root).as_posix()] = path.read_bytes() if stat.S_ISREG(info.st_mode) else None
    return result


@contextlib.contextmanager
def publication_hook(callback):
    """Inject only at the selected destination; retain real filesystem calls."""
    with contextlib.ExitStack() as stack:
        native = getattr(paths, "_rename_handle", None)
        if native is not None:
            def renamed(lease, destination):
                callback(Path(lease.path), Path(destination))
                return native(lease, destination)
            stack.enter_context(patch.object(paths, "_rename_handle", side_effect=renamed))
            if getattr(snapshots, "_rename_handle", None) is native:
                stack.enter_context(patch.object(snapshots, "_rename_handle", side_effect=renamed))
        # os is shared by the writer modules. Save real functions first, then
        # patch once; do not mock the complete API or the output guard.
        for name in ("link", "rename", "replace"):
            original = getattr(os, name)
            def intercepted(source, destination, *args, _original=original, **kwargs):
                callback(Path(source), Path(destination))
                return _original(source, destination, *args, **kwargs)
            stack.enter_context(patch.object(os, name, side_effect=intercepted))
        yield


@unittest.skipUnless(os.name == "nt", "Windows sharing/handle publication contract")
class WriterFaultTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="dungeon-faults-")
        self.addCleanup(self.cleanup_fixture)
        self.base = Path(self.temp.name).resolve()
        self.root = self.base / "project"
        (self.root / "render").mkdir(parents=True)
        (self.root / "render/object.sklt").write_text(SKLT, encoding="utf-8")
        (self.root / ".project").mkdir()
        (self.root / ".project/project.json").write_text(json.dumps({
            "schema_version": 1, "project_id": "fault.fixture",
            "modules": [{"id": "render", "path": "render"}],
            "evidence_store": {"type": "local-cas", "root": "../store"},
        }), encoding="utf-8")
        self.canary = self.base / "canary"
        self.canary.mkdir()
        (self.canary / "keep.txt").write_bytes(b"untouched external bytes")
        self.canary_before = tree(self.canary)
        self.addCleanup(lambda: self.assertEqual(self.canary_before, tree(self.canary)))
        self.leases, self.pins = [], set()
        original_init, original_pin = paths._Lease.__init__, paths._win_pin

        def initialized(lease, *args, **kwargs):
            try:
                return original_init(lease, *args, **kwargs)
            finally:
                self.leases.append(lease)

        def pinned(path):
            release = original_pin(path)
            identity = object()
            self.pins.add(identity)
            def closed():
                self.assertTrue(release(), "native directory CloseHandle failed")
                self.pins.remove(identity)
            return closed

        stack = contextlib.ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(patch.object(paths._Lease, "__init__", new=initialized))
        stack.enter_context(patch.object(paths, "_win_pin", new=pinned))
        self.addCleanup(self.assert_handles_closed)

    def assert_handles_closed(self):
        self.assertFalse(self.pins, "owned directory pins leaked before fixture cleanup")
        self.assertTrue(all(getattr(lease, "handle", None) is None for lease in self.leases),
                        "owned file/directory leases leaked before fixture cleanup")

    def cleanup_fixture(self):
        # Repeated native runs observed WinError 145 even after ALL owned
        # CloseHandle calls succeeded and enumeration showed an empty folder.
        # Retry only removal of this uniquely owned fixture, for <=175ms. No
        # errors are ignored, no gc/forced close hides leaks, and eventual
        # removal is mandatory. Assertion failures are never retried.
        for attempt in range(8):
            try:
                self.temp.cleanup()
                self.assertFalse(self.base.exists(), "fixture cleanup did not remove its root")
                return
            except OSError as exc:
                target = Path(exc.filename) if exc.filename else None
                if (getattr(exc, "winerror", None) != 145 or target is None
                        or not target.is_relative_to(self.base) or target.is_symlink()
                        or attempt == 7):
                    raise
                time.sleep(0.025)

    def attempt_write(self, path, content, event, *, insert=False):
        self.assertTrue(path.is_relative_to(self.base), path)
        event["attempts"] += 1
        try:
            with path.open("xb" if insert else "r+b") as stream:
                stream.write(content)
                stream.truncate()
                stream.flush()
                os.fsync(stream.fileno())
        except OSError as exc:
            # Only actual sharing/access denial is an acceptable blocked attack.
            self.assertTrue(getattr(exc, "winerror", None) in (5, 32, 33)
                            or (isinstance(exc, PermissionError) and exc.errno == errno.EACCES), exc)
            event["blocked"] = True
        else:
            event["mutated"] = True

    def test_cas_stage_mutation_cannot_publish_unhashed_bytes(self):
        source = self.root / "input.bin"
        original = b"original bounded CAS input"
        source.write_bytes(original)
        reference = "sha256:" + hashlib.sha256(original).hexdigest()
        destination = cas.cas_object_path(self.root, reference)
        event = {"attempts": 0, "mutated": False, "blocked": False}

        def mutate(stage, target):
            if target == destination and not event["attempts"]:
                self.attempt_write(stage, b"changed after capture hash", event)

        error = None
        result = None
        with publication_hook(mutate):
            try:
                result = cas.cas_put(self.root, source)
            except (cas.CasError, paths.PublicationError, paths.UnsafeOutputPath) as exc:
                error = exc
        self.assertEqual(event["attempts"], 1, "publication hook did not execute")
        if event["mutated"]:
            self.assertIsNotNone(error, "cas_put accepted changed stage bytes")
            self.assertFalse(destination.exists(), "failed capture left a corrupt CAS object")
        else:
            self.assertTrue(event["blocked"])
            self.assertIsNone(error, error)
            self.assertEqual(result[0], reference)
            self.assertEqual(destination.read_bytes(), original)
            self.assertTrue(cas.cas_verify(self.root, reference)[0])
        self.assertEqual(source.read_bytes(), original)
        self.assertEqual(tree(self.canary), self.canary_before)

    def test_snapshot_stage_replacement_is_blocked_or_rejected(self):
        destination = self.root / "snapshots/fault-checkpoint"
        event = {"attempts": 0, "mutated": False, "blocked": False}
        real_rename = os.rename

        def substitute(stage, target):
            if target != destination or event["attempts"]:
                return
            self.assertTrue(stage.is_relative_to(self.base), stage)
            event["attempts"] += 1
            backup = stage.with_name(stage.name + "-fixture-backup")
            try:
                real_rename(stage, backup)
            except OSError as exc:
                self.assertIn(getattr(exc, "winerror", None), (5, 32, 33), exc)
                event["blocked"] = True
            else:
                stage.mkdir()
                (stage / "substituted.txt").write_bytes(b"not the captured snapshot")
                event["mutated"] = True

        error = None
        issues = None
        with publication_hook(substitute):
            try:
                issues = snapshots.create_snapshot(scan_project(self.root), "fault-checkpoint")
            except (OSError, ValueError) as exc:
                error = exc
        self.assertEqual(event["attempts"], 1, "snapshot publication hook did not execute")
        if event["mutated"]:
            self.assertTrue(error is not None or issues, "substituted snapshot was accepted")
            self.assertFalse(destination.exists(), "invalid snapshot remained published")
        else:
            self.assertTrue(event["blocked"])
            self.assertIsNone(error, error)
            self.assertEqual(issues, [])
            manifest = json.loads((destination / "manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["id"], "fault-checkpoint")
            self.assertEqual((destination / "sklt/render/object.sklt").read_text(encoding="utf-8"), SKLT)
            self.assertFalse((destination / "substituted.txt").exists())
        self.assertEqual(tree(self.canary), self.canary_before)

    def test_leaf_expected_conflict_preserves_concurrent_destination(self):
        destination = self.root / "output.bin"
        original, replacement, concurrent = b"original bytes", b"requested bytes", b"concurrent bytes"
        destination.write_bytes(original)
        event = {"attempts": 0, "mutated": False, "blocked": False}

        def mutate(stage, target):
            if target == destination and not event["attempts"]:
                # A handle-native writer may have retired the preconditioned
                # leaf. A fresh insertion must still defeat exclusive publish.
                self.attempt_write(target, concurrent, event, insert=not target.exists())

        error = None
        with publication_hook(mutate):
            try:
                paths.atomic_bytes(self.root, destination, replacement, expected=original)
            except paths.PublicationError as exc:
                error = exc
        self.assertEqual(event["attempts"], 1, "leaf publication hook did not execute")
        if event["mutated"]:
            self.assertIsNotNone(error, "expected bytes changed but publication succeeded")
            self.assertEqual(destination.read_bytes(), concurrent, "concurrent output was overwritten")
            # If the original leaf was already claimed, it must remain
            # recoverable and be explicitly reported, not silently discarded.
            retired = [p for p in destination.parent.iterdir() if p.name.startswith(".dungeon-retired-")]
            for path in retired:
                self.assertEqual(path.read_bytes(), original)
                self.assertIn(str(path), [item["path"] for item in error.residual])
        else:
            self.assertTrue(event["blocked"])
            self.assertEqual(destination.read_bytes(), replacement if error is None else original)
        self.assertEqual(tree(self.canary), self.canary_before)

    def test_adoption_keyboardinterrupt_rolls_back_partial_publication(self):
        root = self.base / "adoption"
        (root / "render").mkdir(parents=True)
        (root / "render/object.sklt").write_text(SKLT, encoding="utf-8")
        before = tree(root)
        plan = projects.adoption_plan(root)
        self.assertEqual(plan["status"], "PASS", plan)
        original = projects._atomic_bytes
        event = {"attempts": 0}

        def interrupted(project_root, relative, content, expected):
            if relative == "render/.exp":
                event["attempts"] += 1
                self.assertTrue((root / ".project/project.json").exists(), "no partial publication before hook")
                raise KeyboardInterrupt("bounded adoption publication interruption")
            return original(project_root, relative, content, expected)

        response = None
        with patch.object(projects, "_atomic_bytes", side_effect=interrupted):
            try:
                response = projects.adoption_apply(root, plan, ["render"])
            except KeyboardInterrupt:
                pass  # Re-raising after successful rollback is acceptable.
        self.assertEqual(event["attempts"], 1, "adoption interruption hook did not execute")
        if response is not None:
            self.assertNotEqual(response["status"], "PASS", response)
            self.assertEqual(response.get("residual_files", []), [], response)
        self.assertEqual(tree(root), before, "KeyboardInterrupt left partially published adoption outputs")
        self.assertFalse((root / ".project/.adoption.lock").exists())
        self.assertEqual(tree(self.canary), self.canary_before)

    def test_retirement_cleanup_failure_accounts_for_published_identity(self):
        first, last = self.root / "first.bin", self.root / "last.bin"
        first.write_bytes(b"original first")
        last.write_bytes(b"original last")
        original_delete = paths._Lease.delete
        injected = []

        def fail_retirement(lease):
            if lease.path.name.startswith(".dungeon-retired-") and not injected:
                injected.append(lease.path)
                raise OSError("bounded retirement cleanup failure")
            return original_delete(lease)

        with patch.object(paths._Lease, "delete", new=fail_retirement):
            with self.assertRaises(paths.PublicationError) as caught:
                paths.publish_files(self.root, {first: b"new first", last: b"new last"})
        self.assertEqual(len(injected), 1)
        self.assertEqual(first.read_bytes(), b"original first")
        self.assertEqual(last.read_bytes(), b"original last")
        self.assertEqual(injected[0].read_bytes(), b"original first")
        self.assertIn(str(injected[0]), [item["path"] for item in caught.exception.residual])
        self.assertFalse(any(p.name.startswith(".dungeon-stage-") for p in self.root.iterdir()))

    def test_snapshot_child_mutation_in_windows_rename_gap_fails_closed(self):
        destination = self.root / "snapshots/child-mutation"
        event = {"attempts": 0, "mutated": False, "blocked": False}

        def mutate(stage, target):
            if target == destination and not event["attempts"]:
                self.attempt_write(stage / "sklt/render/object.sklt", b"controlled child mutation", event)

        with publication_hook(mutate):
            with self.assertRaises(paths.PublicationError):
                snapshots.create_snapshot(scan_project(self.root), "child-mutation")
        self.assertEqual(event["attempts"], 1)
        self.assertTrue(event["mutated"], "this fixture must exercise the documented child-handle gap")
        self.assertFalse(destination.exists(), "invalid child was left as a published snapshot")

    def test_snapshot_unknown_extra_is_quarantined_not_recursively_deleted(self):
        destination = self.root / "snapshots/extra-mutation"
        inserted = []

        def mutate(stage, target):
            if target == destination and not inserted:
                (stage / "unknown-extra.bin").write_bytes(b"foreign fixture bytes preserved")
                inserted.append(True)

        with publication_hook(mutate):
            with self.assertRaises(paths.PublicationError) as caught:
                snapshots.create_snapshot(scan_project(self.root), "extra-mutation")
        self.assertEqual(inserted, [True])
        self.assertFalse(destination.exists())
        self.assertEqual(len(caught.exception.residual), 1)
        quarantine = Path(caught.exception.residual[0]["path"])
        self.assertTrue(quarantine.is_relative_to(self.root / "snapshots"))
        self.assertTrue(quarantine.name.startswith(".snapshot-rejected-"))
        self.assertEqual((quarantine / "unknown-extra.bin").read_bytes(), b"foreign fixture bytes preserved")

    def test_native_multi_file_failure_and_keyboardinterrupt_roll_back(self):
        # Native counterpart of the parent's two legacy os.replace hooks.
        for kind in (OSError, KeyboardInterrupt):
            with self.subTest(kind=kind.__name__):
                first, last = self.root / (kind.__name__ + "-first"), self.root / (kind.__name__ + "-last")
                before = b"before-first" if kind is KeyboardInterrupt else None
                if before is not None:
                    first.write_bytes(before)
                last.write_bytes(b"before-last")
                real_rename = paths._rename_handle
                event = {"first": None, "last": 0}

                def interrupted(lease, target):
                    if lease.path.name.startswith(".dungeon-stage-"):
                        if target == last:
                            event["last"] += 1
                            self.assertIsNotNone(event["first"], "fault was not after partial publication")
                            self.assertEqual(event["first"].read_bytes(), b"after-first")
                            raise kind("bounded later-file interruption")
                        if target == first and event["first"] is None:
                            result = real_rename(lease, target)
                            event["first"] = lease
                            return result
                    return real_rename(lease, target)

                expected_error = KeyboardInterrupt if kind is KeyboardInterrupt else paths.PublicationError
                with patch.object(paths, "_rename_handle", side_effect=interrupted):
                    with self.assertRaises(expected_error) as caught:
                        paths.publish_files(self.root, {first: b"after-first", last: b"after-last"})
                self.assertEqual(event["last"], 1)
                if kind is OSError:
                    self.assertEqual(caught.exception.residual, [])
                self.assertEqual(first.read_bytes() if first.exists() else None, before)
                self.assertEqual(last.read_bytes(), b"before-last")
                self.assertFalse((self.root / ".dungeon-publish.lock").exists())

    def test_unpublished_stage_cleanup_error_reports_recoverable_path(self):
        destination = self.root / "new.bin"
        real_delete = paths._Lease.delete
        stages = []

        def blocked_cleanup(lease):
            if lease.path.name.startswith(".dungeon-stage-"):
                stages.append(lease.path)
                raise OSError("bounded stage cleanup failure")
            return real_delete(lease)

        with patch.object(paths._Lease, "delete", new=blocked_cleanup):
            with self.assertRaises(paths.PublicationError) as caught:
                # Deliberately invalid expected bytes: publication must not run.
                paths.atomic_bytes(self.root, destination, b"staged bytes", expected=b"missing old bytes")
        self.assertFalse(destination.exists())
        self.assertEqual(len(stages), 1)
        self.assertEqual(stages[0].read_bytes(), b"staged bytes")
        self.assertIn(str(stages[0]), [item["path"] for item in caught.exception.residual])

    def test_every_native_stage_data_write_denies_external_hardlink(self):
        # Exercise the real atomic/CAS/snapshot APIs, not only a staged_file
        # helper. The attempted alias lies OUTSIDE the selected project root.
        original_write = paths._Lease.write
        attempts = []

        def write(lease, content):
            alias = self.base / ("outside-stage-alias-" + str(len(attempts)))
            attempts.append(lease.path)
            with self.assertRaises(OSError) as caught:
                os.link(lease.path, alias)
            self.assertIn(getattr(caught.exception, "winerror", None), (5, 32, 33))
            self.assertFalse(alias.exists())
            return original_write(lease, content)

        source = self.root / "input.bin"
        source.write_bytes(b"bounded CAS hardlink fixture")
        with patch.object(paths._Lease, "write", new=write):
            paths.atomic_bytes(self.root, self.root / "output.bin", b"bounded atomic bytes")
            reference, _, _ = cas.cas_put(self.root, source)
            self.assertEqual(snapshots.create_snapshot(scan_project(self.root), "hardlink-checkpoint"), [])
        self.assertGreaterEqual(len(attempts), 4)
        self.assertTrue(any(p.name.startswith(".cas-put-") for p in attempts))
        self.assertTrue(any("snapshots" in p.parts for p in attempts))
        self.assertTrue(cas.cas_verify(self.root, reference)[0])
        self.assertEqual((self.root / "output.bin").read_bytes(), b"bounded atomic bytes")

    def test_stage_acl_removes_only_own_ace_preserves_inherited_deny_and_freezes_writes(self):
        # Add a controlled inheritable deny to an OWNED fixture directory.
        # The inherited counterpart must survive removal of our identical but
        # NON-inherited temporary ACE. Restore the fixture directory's ACL.
        parent = self.root / "custom-acl"
        parent.mkdir()
        api = paths._kernel()
        handle = api.CreateFileW(str(parent), 0x20000 | 0x40000, 7, None, 3, 0x02200000, None)
        self.assertNotEqual(handle, ctypes.c_void_p(-1).value)
        owner = type("FixtureHandle", (), {"api": api, "handle": handle})()
        original, _ = paths._handle_dacl(owner)
        injected = bytearray(paths._STAGE_DENY_ACE)
        injected[1] = 3  # OBJECT_INHERIT_ACE | CONTAINER_INHERIT_ACE.
        revision, reserved, size, count, reserved2 = struct.unpack("<BBHHH", original[:8])
        altered = struct.pack("<BBHHH", revision, reserved, size + len(injected), count + 1, reserved2)
        altered += bytes(injected) + original[8:]
        security = paths._security_api()

        def set_acl(content):
            status = security.SetSecurityInfo(handle, 1, 4, None, None, ctypes.create_string_buffer(content), None)
            if status:
                raise ctypes.WinError(status)

        try:
            set_acl(altered)
            parent_before = paths._handle_dacl(owner)[1]
            with paths.staged_file(self.root, parent) as stage:
                before = paths._handle_dacl(stage)[1]
                self.assertEqual(before[1][0], paths._STAGE_DENY_ACE)
                self.assertTrue(any(e[1] & 0x10 and e[4:] == paths._STAGE_DENY_ACE[4:] for e in before[1][1:]))
                stage.write(b"fully completed bytes")
                stage.flush()
                stage.finish_writing()
                self.assertEqual(paths._handle_dacl(stage)[1], (before[0], before[1][1:]))
                self.assertEqual(stage.read_bytes(), b"fully completed bytes")
                with self.assertRaises(paths.PublicationError):
                    stage.write(b"must never be written after ACL restoration")
            self.assertEqual(paths._handle_dacl(owner)[1], parent_before)
        finally:
            try:
                set_acl(original)
            finally:
                api.CloseHandle(handle)

    def test_acl_restoration_failure_accounts_for_published_identity(self):
        first, last = self.root / "first.bin", self.root / "last.bin"
        first.write_bytes(b"original first")
        last.write_bytes(b"original last")
        finish = paths._Lease.finish_writing
        attempts = []

        def fail_once(lease):
            if lease.path == first and not attempts:
                attempts.append(True)
                raise OSError("bounded handle ACL restoration failure")
            return finish(lease)

        with patch.object(paths._Lease, "finish_writing", new=fail_once):
            with self.assertRaises(paths.PublicationError) as caught:
                paths.publish_files(self.root, {first: b"new first", last: b"new last"})
        self.assertEqual(attempts, [True])
        self.assertEqual(first.read_bytes(), b"original first")
        self.assertEqual(last.read_bytes(), b"original last")
        recoveries = [Path(item["path"]) for item in caught.exception.residual]
        self.assertEqual(len(recoveries), 1)
        self.assertTrue(recoveries[0].name.startswith(".dungeon-retired-"))
        self.assertEqual(recoveries[0].read_bytes(), b"original first")

    def test_atomic_retirement_cleanup_failure_reports_both_identities(self):
        destination = self.root / "atomic.bin"
        destination.write_bytes(b"original atomic")
        delete = paths._Lease.delete
        retired = []

        def fail_retirement(lease):
            if lease.path.name.startswith(".dungeon-retired-"):
                retired.append(lease.path)
                raise OSError("bounded atomic retirement failure")
            return delete(lease)

        with patch.object(paths._Lease, "delete", new=fail_retirement):
            with self.assertRaises(paths.PublicationError) as caught:
                paths.atomic_bytes(self.root, destination, b"new atomic", expected=b"original atomic")
        self.assertEqual(len(retired), 1)
        self.assertEqual(destination.read_bytes(), b"new atomic")
        self.assertEqual(retired[0].read_bytes(), b"original atomic")
        self.assertEqual({item["path"] for item in caught.exception.residual}, {str(destination), str(retired[0])})

    def test_cas_export_uses_explicit_project_boundary_not_output_parent(self):
        source = self.root / "input.bin"
        source.write_bytes(b"bounded export bytes")
        reference, _, _ = cas.cas_put(self.root, source)
        before = tree(self.canary)
        with self.assertRaises(paths.UnsafeOutputPath) as caught:
            cas.cas_get(self.root, reference, self.canary / "outside-output.bin")
        self.assertTrue(str(caught.exception).startswith("UNSAFE_OUTPUT_PATH:"))
        self.assertEqual(tree(self.canary), before)
        self.assertEqual(cas.cas_get(self.root, reference, self.root / "export.bin"), len(b"bounded export bytes"))
        self.assertEqual((self.root / "export.bin").read_bytes(), b"bounded export bytes")


if __name__ == "__main__":
    unittest.main()
