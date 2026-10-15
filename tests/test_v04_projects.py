"""Discovery/adoption contracts, using exclusively temporary projects."""

from __future__ import annotations

import hashlib
import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
from project_structure import v04_projects as projects
from project_structure.v04_projects import adoption_apply, adoption_plan, project_catalog, project_probe
from project_structure.v03_manifest import load_manifest
from project_structure.v03_scan import scan_project_v03
from project_structure.v03_validation import projection_statuses


CANONICAL = """schema_version: 1
id: {object_id}
object_id: {object_id}
kind: component
responsibility: fixture responsibility declared by its author
inputs:
outputs:
depends_on:
{dependencies}effects:
state: {state}
{extra}"""


class ProjectAPITests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="dungeon-project-test-")
        from tests.fixture_cleanup import cleanup_temporary_directory
        self.addCleanup(cleanup_temporary_directory, temporary)
        self.root = Path(temporary.name).resolve()

    def write(self, relative: str, text: str) -> Path:
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    def sklt(self, relative: str = "render/arbitrary-name.sklt", object_id: str = "render.stable", *, state: str = "SKLT", dependencies: str = "", extra: str = "") -> Path:
        return self.write(relative, CANONICAL.format(object_id=object_id, state=state, dependencies=dependencies, extra=extra))

    def manifest(self, **updates) -> Path:
        return self.write(".project/project.json", json.dumps({"schema_version": 1, "project_id": "fixture.project", "modules": [], **updates}))

    def tree(self) -> dict:
        result = {}
        for path in self.root.rglob("*"):
            try:
                info = path.lstat()  # Windows enumeration may include an already absent name.
            except FileNotFoundError:
                continue
            if not stat.S_ISLNK(info.st_mode):
                result[path.relative_to(self.root).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest() if stat.S_ISREG(info.st_mode) else "DIRECTORY"
        return result

    def assert_blocked(self, result: dict, code: str) -> None:
        self.assertEqual(result["status"], "BLOCKED", result)
        self.assertFalse(result["applied"])
        self.assertIn(code, [item["code"] for item in result["issues"]], result)

    def test_all_five_modes_and_manifest_only(self) -> None:
        self.assertEqual(project_probe(self.root)["mode"], "NOT_DUNGEON")
        self.sklt()
        self.assertEqual(project_probe(self.root)["mode"], "SKLT_ONLY")
        self.manifest()
        probe = project_probe(self.root)
        self.assertEqual((probe["mode"], probe["project_mode"]), ("MANAGED", "DUNGEON"))
        self.assertFalse((self.root / ".project/objects.json").exists())
        self.write(".project/project.json", "{broken")
        self.assertEqual(project_probe(self.root)["mode"], "INVALID_MANAGED")
        self.assertEqual(project_probe(self.root / "absent")["mode"], "UNAVAILABLE")
        self.assertEqual(project_probe("relative/root")["mode"], "UNAVAILABLE")
        empty = self.root / "manifest-only"
        empty.mkdir()
        (empty / ".project").mkdir()
        (empty / ".project/project.json").write_text('{"project_id":"manifest.only"}', encoding="utf-8")
        self.assertEqual(project_probe(empty)["mode"], "MANAGED")

    def test_invalid_manifest_schema_paths_and_overlap(self) -> None:
        self.sklt()
        self.write("render/src/main.rs", "fn main() {}")
        for updates in ({"schema_version": 99}, {"schema_version": True},
                        {"modules": [{"id": "r", "path": "../outside"}]},
                        {"modules": [{"id": "r", "path": str(self.root / "render")}]},
                        {"modules": [{"id": "r", "path": "missing"}]},
                        {"modules": [{"id": "r", "path": "render"}, {"id": "s", "path": "render/src"}]},
                        {"docs": {"project": "../outside.md"}}):
            with self.subTest(updates=updates):
                self.manifest(**updates)
                self.assertEqual(project_probe(self.root)["mode"], "INVALID_MANAGED")
        self.manifest(modules=[{"id": "declared.module", "path": "render"}])
        self.assertEqual(project_probe(self.root)["mode"], "MANAGED")

    def test_read_only_plan_is_deterministic(self) -> None:
        self.sklt()
        self.write("render/view.glif", json.dumps({"object_id": "render.stable", "elements": []}))
        self.write("render/src/main.rs", "fn main() {}")
        before = self.tree()
        first = adoption_plan(self.root, "existing.project")
        second = adoption_plan(str(self.root), "existing.project")
        self.assertEqual(first, second)
        self.assertEqual(first["status"], "PASS", first)
        self.assertEqual(first["source_directories"], ["render/src"])
        self.assertEqual(first["glif_paths"], ["render/view.glif"])
        self.assertEqual(first["candidate_modules"][0]["object_ids"], ["render.stable"])
        self.assertTrue(first["candidate_modules"][0]["confirmed_on_disk"])
        project_probe(self.root)
        project_catalog(self.root)
        self.assertEqual(before, self.tree())
        json.dumps(first)

    def test_more_than_100_sklt_and_no_inline_contents(self) -> None:
        for number in range(139):
            self.sklt(f"structure/name-{number}.sklt", f"explicit.object{number}")
        self.sklt("snapshots/old.sklt", "snapshot.old")
        self.sklt("target-debug/generated.sklt", "generated.object")
        catalog = project_catalog(self.root)
        self.assertEqual(catalog["sklt_count"], 139)
        self.assertEqual(len(catalog["objects"]), 139)
        self.assertTrue(catalog["counts_exact"])
        self.assertEqual(len(catalog["sklt_paths"]), 139)
        self.assertNotIn("fixture responsibility", json.dumps(catalog))
        self.assertEqual(catalog["modules"][0]["object_count"], 139)

    def test_manifest_modules_control_raw_paths_and_ids(self) -> None:
        self.sklt("implementation/x.sklt", "original.identity")
        self.sklt("excluded/y.sklt", "excluded.identity")
        self.manifest(modules=[{"id": "logical.module", "path": "implementation"}])
        catalog = project_catalog(self.root)
        self.assertEqual(catalog["sklt_paths"], ["implementation/x.sklt"])
        self.assertEqual(set(catalog["objects"]), {"original.identity"})
        self.assertEqual(catalog["objects"]["original.identity"]["module"], "logical.module")

    def test_root_and_nested_files_are_counted_once(self) -> None:
        self.sklt("root.sklt", "root.stable")
        self.sklt("render/x.sklt", "render.stable")
        catalog = project_catalog(self.root)
        self.assertEqual(catalog["sklt_count"], 2)
        self.assertFalse(catalog["duplicates"])
        plan = adoption_plan(self.root)
        result = adoption_apply(self.root, plan, [item["id"] for item in plan["candidate_modules"]])
        self.assertEqual(result["status"], "PASS", result)
        self.assertEqual(set(project_catalog(self.root)["objects"]), {"root.stable", "render.stable"})
        snapshot, manifest, _ = scan_project_v03(self.root)
        self.assertEqual(len(snapshot.sklts), 2)
        self.assertEqual(projection_statuses(snapshot, manifest)[0]["grade"], "CURRENT")

    def test_declared_symbols_spans_explicit_ids_and_no_filename_ids(self) -> None:
        text = """project Demo:
    module Renderer:
        capability Draw:
            object_id: explicit.draw
            state: SKLT
            depends_on:
                capability Upload
            source:
                path: src/draw.rs
        command Render:
            requires:
                capability Draw
module Storage:
    capability Save:
        state: RARE
action ShowWarnings:
    use:
        capability Draw
"""
        path = self.write("sklt/unrelated-file.sklt", text)
        catalog = project_catalog(self.root)
        record = catalog["objects"]["explicit.draw"]
        self.assertEqual(record["identity_origin"], "OBJECT_ID")
        self.assertEqual(record["source"], [{"path": "src/draw.rs"}])
        self.assertEqual(record["dependencies"], [{"type": "declared", "id": "capability Upload"}])
        command_id = "declared:project:Demo/module:Renderer/command:Render"
        command = catalog["objects"][command_id]
        self.assertEqual(command["identity_origin"], "DECLARED_SYMBOL")
        self.assertIsNone(command["state"])
        fragment = "\n".join(text.splitlines()[record["line_start"] - 1:record["line_end"]])
        self.assertIn("capability Draw:", fragment)
        self.assertNotIn("command Render:", fragment)
        path.rename(path.with_name("renamed.sklt"))
        self.assertEqual(set(project_catalog(self.root)["objects"]), set(catalog["objects"]))
        self.assertFalse(any("unrelated-file" in key for key in catalog["objects"]))

    def test_declaration_adoption_blocked_without_inventing_fields(self) -> None:
        self.write("sklt/names.sklt", "module Render:\n    capability Draw:\n        description: draw\n")
        probe = project_probe(self.root)
        self.assertTrue(probe["can_plan_adoption"])
        self.assertFalse(probe["can_adopt"])
        self.assertFalse(probe["adoption_executable"])
        self.assertIn("UNSUPPORTED_ADOPTION_DIALECT", probe["adoption_blockers"])
        before = self.tree()
        plan = adoption_plan(self.root)
        self.assertIn("UNSUPPORTED_ADOPTION_DIALECT", [item["code"] for item in plan["issues"]])
        self.assert_blocked(adoption_apply(self.root, plan, ["sklt"]), "UNSUPPORTED_ADOPTION_DIALECT")
        self.assertEqual(self.tree(), before)

    def test_bare_declared_source_and_declaration_limit(self) -> None:
        self.write("sklt/source.sklt", "module Render:\n    source: src/main.rs\n    capability Draw:\n    command Run:\n")
        catalog = project_catalog(self.root)
        self.assertEqual(catalog["objects"]["declared:module:Render"]["source"], [{"path": "src/main.rs"}])
        with patch.object(projects, "CATALOG_OBJECT_LIMIT", 2):
            bounded = project_catalog(self.root)
            self.assertEqual(len(bounded["objects"]), 2)
            self.assertFalse(bounded["object_count_exact"])
            self.assertIn("CATALOG_LIMIT", [item["code"] for item in bounded["issues"]])

    def test_duplicate_canonical_and_declared_ids_never_overwrite(self) -> None:
        self.sklt("a/one.sklt", "same.id")
        self.sklt("b/two.sklt", "same.id", state="RARE")
        self.write("decl/one.sklt", "capability Draw:\n")
        self.write("decl/two.sklt", "capability Draw:\n")
        catalog = project_catalog(self.root)
        self.assertEqual(len(catalog["duplicates"]["same.id"]), 2)
        self.assertEqual(len(catalog["duplicates"]["declared:capability:Draw"]), 2)
        self.assertTrue(catalog["objects"]["same.id"]["identity_conflict"])
        self.assertEqual({item["state"] for item in catalog["duplicates"]["same.id"]}, {"SKLT", "RARE"})
        before = self.tree()
        self.assert_blocked(adoption_apply(self.root, adoption_plan(self.root), ["a", "b"]), "DUPLICATE_OBJECT_ID")
        self.assertEqual(self.tree(), before)

    def test_apply_reapply_preserves_bytes_ids_git_and_v03_metrics(self) -> None:
        source = self.sklt("render/not-the-id.sklt", "stable.explicit", state="RARE")
        original = source.read_bytes()
        plan = adoption_plan(self.root, "adopted.project")
        result = adoption_apply(self.root, plan, ["render"])
        self.assertEqual(result["status"], "PASS", result)
        self.assertEqual(result["mode"], "MANAGED")
        self.assertTrue(result["applied"])
        self.assertEqual(source.read_bytes(), original)
        self.assertFalse((self.root / ".git").exists())
        self.assertEqual((self.root / "render/.exp").read_text(), "sklt 1\nrare 1\n")
        self.assertEqual((self.root / "docs/.grade").read_text(), "sklt 1\nrare 1\n")
        index = json.loads((self.root / ".project/objects.json").read_text())
        self.assertEqual(set(index["objects"]), {"stable.explicit"})
        snapshot, manifest, _ = scan_project_v03(self.root)
        self.assertEqual(projection_statuses(snapshot, manifest)[0], {"exp": "CURRENT", "grade": "CURRENT", "objects_index": "CURRENT"})
        before = self.tree()
        replay = adoption_apply(self.root, json.loads(json.dumps(plan)), [plan["candidate_modules"][0]])
        self.assertEqual(replay["result"], "ALREADY_APPLIED", replay)
        self.assertEqual(replay["files_changed"], [])
        self.assertEqual(before, self.tree())
        self.assertTrue(result["unresolved_issues"])  # No documentation/snapshots fabricated.

    def test_apply_confirmed_subset_only(self) -> None:
        self.sklt("render/one.sklt", "one.stable")
        self.sklt("storage/two.sklt", "two.stable")
        original = self.tree()
        result = adoption_apply(self.root, adoption_plan(self.root), ["render"])
        self.assertEqual(result["status"], "PASS", result)
        manifest, _ = load_manifest(self.root)
        self.assertEqual([(item.module_id, item.path) for item in manifest.modules], [("render", "render")])
        self.assertEqual(set(project_catalog(self.root)["objects"]), {"one.stable"})
        self.assertEqual(original["storage/two.sklt"], self.tree()["storage/two.sklt"])
        self.assertFalse((self.root / "storage/.exp").exists())

    def test_ordinary_project_empty_adoption_has_no_inferred_architecture(self) -> None:
        self.write("src/main.rs", "fn main() {}")
        plan = adoption_plan(self.root)
        self.assertEqual(plan["candidate_modules"], [])
        result = adoption_apply(self.root, plan, [])
        self.assertEqual(result["status"], "PASS", result)
        self.assertEqual(json.loads((self.root / ".project/project.json").read_text())["modules"], [])
        self.assertEqual(project_probe(self.root)["mode"], "MANAGED")
        self.assertEqual(project_catalog(self.root)["objects"], {})
        self.assertFalse((self.root / ".git").exists())

    def test_stale_and_tampered_plan_and_recomputed_forgery(self) -> None:
        source = self.sklt()
        plan = adoption_plan(self.root)
        forged = json.loads(json.dumps(plan))
        forged["candidate_modules"][0]["path"] = "../outside"
        self.assert_blocked(adoption_apply(self.root, forged, ["render"]), "TAMPERED_ADOPTION_PLAN")
        forged["plan_id"] = "adopt-" + projects._hash({key: value for key, value in forged.items() if key != "plan_id"})
        self.assert_blocked(adoption_apply(self.root, forged, ["render"]), "TAMPERED_ADOPTION_PLAN")
        source.write_text(source.read_text() + "# input changed\n", encoding="utf-8")
        before = self.tree()
        self.assert_blocked(adoption_apply(self.root, plan, ["render"]), "STALE_ADOPTION_PLAN")
        self.assertEqual(before, self.tree())

    def test_replay_rejects_changed_inputs_and_different_selection(self) -> None:
        source = self.sklt()
        plan = adoption_plan(self.root)
        self.assertEqual(adoption_apply(self.root, plan, ["render"])["status"], "PASS")
        self.assert_blocked(adoption_apply(self.root, plan, []), "INVALID_ADOPTION_REQUEST")
        source.write_text(source.read_text() + "# changed\n", encoding="utf-8")
        before = self.tree()
        self.assert_blocked(adoption_apply(self.root, plan, ["render"]), "STALE_ADOPTION_PLAN")
        self.assertEqual(before, self.tree())

    def test_unowned_outputs_and_new_conflicts_are_not_overwritten(self) -> None:
        self.sklt()
        plan = adoption_plan(self.root)
        self.write("docs/.grade", "customer data\n")
        before = self.tree()
        self.assert_blocked(adoption_apply(self.root, plan, ["render"]), "STALE_ADOPTION_PLAN")
        replanned = adoption_plan(self.root)
        self.assert_blocked(adoption_apply(self.root, replanned, ["render"]), "DERIVED_OUTPUT_CONFLICT")
        self.assertEqual(before, self.tree())

    def test_publish_failure_rolls_back_only_created_files(self) -> None:
        self.sklt()
        plan = adoption_plan(self.root)
        before = self.tree()
        original = projects._atomic_bytes
        def fail(root, relative, content, expected):
            if relative == "render/.exp":
                raise OSError("simulated output failure")
            original(root, relative, content, expected)
        with patch.object(projects, "_atomic_bytes", side_effect=fail):
            result = adoption_apply(self.root, plan, ["render"])
        self.assert_blocked(result, "ADOPTION_PUBLISH_FAILED")
        self.assertEqual(self.tree(), before)

    def test_interrupted_publish_rolls_back_before_propagating(self) -> None:
        self.sklt()
        plan = adoption_plan(self.root)
        before = self.tree()
        original = projects._atomic_bytes
        def interrupted(root, relative, content, expected):
            if relative == "render/.exp":
                raise KeyboardInterrupt("bounded interruption after first output")
            original(root, relative, content, expected)
        with patch.object(projects, "_atomic_bytes", side_effect=interrupted):
            with self.assertRaises(KeyboardInterrupt):
                adoption_apply(self.root, plan, ["render"])
        self.assertEqual(self.tree(), before)

    def test_atomic_failure_residuals_are_not_silently_discarded(self) -> None:
        from tools.project_structure.v041_paths import PublicationError
        self.sklt()
        plan = adoption_plan(self.root)
        residual = str(self.root / ".project/.dungeon-retired-recovery")
        error = PublicationError("bounded recovery conflict", [{"path": residual, "error": "retained original"}])
        with patch.object(projects, "_atomic_bytes", side_effect=error):
            result = adoption_apply(self.root, plan, ["render"])
        self.assert_blocked(result, "ADOPTION_PUBLISH_FAILED")
        self.assertEqual(result["residual_files"], [residual])
        self.assertIn("ADOPTION_ROLLBACK_CONFLICT", [item["code"] for item in result["issues"]])

    def test_derived_hardlink_is_protected(self) -> None:
        self.sklt()
        self.write("original.data", "important bytes")
        (self.root / "docs").mkdir()
        try:
            os.link(self.root / "original.data", self.root / "docs/.grade")
        except OSError as exc:
            self.skipTest(str(exc))
        before = self.tree()
        result = adoption_apply(self.root, adoption_plan(self.root), ["render"])
        self.assert_blocked(result, "DERIVED_OUTPUT_CONFLICT")
        self.assertEqual(self.tree(), before)

    def test_discovery_limits_report_partial_counts_and_block_apply(self) -> None:
        for number in range(4):
            self.sklt(f"render/{number}.sklt", f"stable.o{number}")
        with patch("project_structure.v04_sklt.MAX_STRUCTURAL_FILES", 2):
            probe = project_probe(self.root)
            plan = adoption_plan(self.root)
            result = adoption_apply(self.root, plan, ["render"])
        self.assertFalse(probe["counts_exact"])
        self.assertEqual(probe["sklt_count"], 2)
        self.assert_blocked(result, "ADOPTION_UNAVAILABLE")

    def test_symlink_structural_inputs_and_output_parent_are_rejected(self) -> None:
        self.sklt()
        outside = self.root / "outside"
        outside.mkdir()
        target = outside / "secret.sklt"
        target.write_text(CANONICAL.format(object_id="outside.secret", state="SKLT", dependencies="", extra=""), encoding="utf-8")
        link = self.root / "render/linked.sklt"
        try:
            os.symlink(target, link)
        except OSError as exc:
            self.skipTest(f"symlink creation unavailable: {exc}")
        catalog = project_catalog(self.root)
        self.assertNotIn("render/linked.sklt", catalog["sklt_paths"])
        self.assertIn("UNSAFE_PATH", [item["code"] for item in catalog["issues"]])
        link.unlink()
        os.symlink(outside, self.root / "docs", target_is_directory=True)
        original = target.read_bytes()
        result = adoption_apply(self.root, adoption_plan(self.root), ["render"])
        self.assertEqual(result["status"], "BLOCKED")
        self.assertEqual(target.read_bytes(), original)
        self.assertFalse((outside / ".grade").exists())

    def test_missing_source_links_remain_unresolved_not_rewritten(self) -> None:
        source = self.sklt(extra="source:\n    path: render/src/missing.rs\n    symbol: Draw\n")
        before = source.read_bytes()
        result = adoption_apply(self.root, adoption_plan(self.root), ["render"])
        self.assertEqual(result["status"], "PASS", result)
        self.assertIn("SOURCE_PATH_MISSING", [item["code"] for item in result["unresolved_issues"]])
        self.assertEqual(source.read_bytes(), before)

    def test_existing_managed_manifest_and_module_registrations_are_preserved(self) -> None:
        self.sklt("render/x.sklt", "stable.original", state="RARE")
        self.sklt("storage/y.sklt", "stable.other")
        manifest = self.manifest(modules=[{"id": "rendering", "path": "render"}, {"id": "storage", "path": "storage"}], evidence_store={"type": "local-cas", "root": "../ExternalCAS"})
        original = manifest.read_bytes()
        result = adoption_apply(self.root, adoption_plan(self.root), ["rendering"])
        self.assertEqual(result["status"], "PASS", result)
        self.assertEqual(manifest.read_bytes(), original)
        self.assertEqual(set(result["registered_modules"]), {"rendering", "storage"})
        self.assertEqual((self.root / "docs/.grade").read_text(), "sklt 2\nrare 1\n")
        self.assertFalse((self.root.parent / "ExternalCAS").exists())

    def test_managed_empty_module_uses_v03_zero_metrics(self) -> None:
        (self.root / "empty").mkdir()
        self.manifest(modules=[{"id": "empty.module", "path": "empty"}])
        probe = project_probe(self.root)
        self.assertEqual(probe["mode"], "MANAGED")
        self.assertEqual(probe["module_count"], 1)
        self.assertEqual(probe["derived"]["objects_index"], "MISSING")
        result = adoption_apply(self.root, adoption_plan(self.root), ["empty.module"])
        self.assertEqual(result["status"], "PASS", result)
        self.assertEqual((self.root / "empty/.exp").read_text(), "sklt 0\nrare 0\n")

    def test_existing_explicit_root_module_keeps_recursive_ownership(self) -> None:
        self.sklt("root.sklt", "root.object")
        self.sklt("nested/x.sklt", "nested.object", state="RARE")
        manifest = self.manifest(modules=[{"id": "existing.root", "path": "."}])
        before = manifest.read_bytes()
        catalog = project_catalog(self.root)
        self.assertEqual(set(catalog["objects"]), {"root.object", "nested.object"})
        self.assertEqual({record["module"] for record in catalog["objects"].values()}, {"existing.root"})
        result = adoption_apply(self.root, adoption_plan(self.root), ["existing.root"])
        self.assertEqual(result["status"], "PASS", result)
        self.assertEqual(manifest.read_bytes(), before)
        self.assertEqual((self.root / ".exp").read_text(), "sklt 2\nrare 1\n")
        snapshot, _, _ = scan_project_v03(self.root)
        self.assertEqual(len(snapshot.sklts), 2)

    def test_manifest_nonrecursive_scope_and_invalid_recursive_type(self) -> None:
        self.sklt("render/root.sklt", "direct.object")
        self.sklt("render/nested/x.sklt", "nested.object")
        self.manifest(modules=[{"id": "direct", "path": "render", "recursive": False}, {"id": "nested", "path": "render/nested"}])
        catalog = project_catalog(self.root)
        self.assertEqual(catalog["mode"], "MANAGED")
        self.assertEqual(catalog["objects"]["direct.object"]["module"], "direct")
        self.assertEqual(catalog["objects"]["nested.object"]["module"], "nested")
        snapshot, _, _ = scan_project_v03(self.root)
        self.assertEqual(len(snapshot.sklts), 2)
        self.manifest(modules=[{"id": "direct", "path": "render", "recursive": "false"}])
        self.assertEqual(project_probe(self.root)["mode"], "INVALID_MANAGED")

    def test_declared_sources_cannot_be_overwritten_by_derived_outputs(self) -> None:
        self.sklt(extra="source:\n    path: docs/.grade\n")
        self.manifest()
        self.write("docs/.grade", "sklt 1\nrare 0\n")
        before = self.tree()
        result = adoption_apply(self.root, adoption_plan(self.root), ["render"])
        self.assert_blocked(result, "DERIVED_SOURCE_CONFLICT")
        self.assertEqual(before, self.tree())

    def test_dependency_to_unaccepted_module_blocks_without_writes(self) -> None:
        self.sklt("render/x.sklt", "render.object", dependencies="    storage.object\n")
        self.sklt("storage/x.sklt", "storage.object")
        before = self.tree()
        result = adoption_apply(self.root, adoption_plan(self.root), ["render"])
        self.assert_blocked(result, "UNKNOWN_OBJECT_DEPENDENCY")
        self.assertEqual(before, self.tree())

    def test_git_is_observed_and_unavailable_git_is_not_fabricated(self) -> None:
        try:
            run = subprocess.run(["git", "init", "--quiet", str(self.root)], capture_output=True, timeout=5)
        except (OSError, subprocess.SubprocessError) as exc:
            self.skipTest(str(exc))
        self.assertEqual(run.returncode, 0, run.stderr)
        probe = project_probe(self.root)
        self.assertTrue(probe["git_detected"])
        self.assertEqual(Path(probe["git"]["repository_root"]), self.root)
        self.assertIsNone(probe["git"]["head"])
        before = self.tree()
        adoption_plan(self.root)
        self.assertEqual(self.tree(), before)
        with patch.object(projects.subprocess, "run", side_effect=FileNotFoundError("git unavailable")):
            self.assertIsNone(project_probe(self.root)["git_detected"])


if __name__ == "__main__":
    unittest.main()
