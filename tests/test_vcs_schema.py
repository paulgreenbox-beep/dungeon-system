"""The published v1 JSON Schema admits the optional VersionScope extension."""
from __future__ import annotations
import copy
import json
from pathlib import Path
import unittest


class VcsJsonSchemaTests(unittest.TestCase):
    def test_versioned_revisions_match_published_schema_and_reject_wrong_namespaces(self):
        try:
            from jsonschema import Draft202012Validator
        except ImportError:
            self.skipTest("jsonschema is unavailable; this is not schema acceptance")
        schema = json.loads((Path(__file__).resolve().parents[1] / "docs/evidence-manifest-v1.schema.json").read_text(encoding="utf-8"))
        Draft202012Validator.check_schema(schema)
        revision_schema = {"$defs": schema["$defs"], "$ref": "#/$defs/typedRevision"}
        validator = Draft202012Validator(revision_schema)
        from tools.project_structure.vcs_backends import validate_revision
        record = {"schema_version": 1, "backend": "SCION", "scope_id": "lab", "revision": "c1" + "a" * 64,
                  "ref": "main", "dirty_state": "UNKNOWN", "availability": "AVAILABLE"}
        for changes, valid in (({}, True), ({"revision": "a" * 40}, False),
                               ({"backend": "GIT", "revision": "a" * 40, "dirty_state": "DIRTY"}, True),
                               ({"backend": "GIT", "revision": "a" * 48}, False),
                               ({"revision": None}, False),
                               ({"revision": None, "availability": "UNAVAILABLE"}, True),
                               ({"availability": "UNAVAILABLE", "dirty_state": "CLEAN"}, False),
                               ({"ref": "feature/x7"}, True),
                               ({"ref": None}, True),
                               ({"ref": "main\n"}, False),
                               ({"ref": "main\x00"}, False),
                               ({"ref": "main\x7f"}, False),
                               ({"scope_id": "lab\n"}, False),
                               ({"revision": "c1" + "a" * 64 + "\n"}, False),
                               ({"schema_version": True}, False)):
            value = {**record, **changes}
            with self.subTest(changes=changes):
                self.assertEqual(not list(validator.iter_errors(value)), valid)
                self.assertEqual(validate_revision(value), valid)
        # Validate real captured v1 payload shape, not only a hand-built sub-schema.
        from tests.test_vcs_provenance import VcsProvenanceBoundaryTests, envelope
        from tools.project_structure import v03_evidence, vcs_service
        from unittest.mock import patch
        fixture = VcsProvenanceBoundaryTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        fixture.write_manifest(version_scopes=[{"id": "lab", "backend": "SCION", "root": ".", "endpoint_profile": "LAB"}])
        fixture.write_object("renderer.item", "lab")
        draft = fixture.root / "draft.json"
        draft.write_text(json.dumps(fixture.evidence_draft()), encoding="utf-8")
        with patch.object(vcs_service, "object_revisions", return_value=envelope(record)):
            payload, issues = v03_evidence.create_evidence(fixture.root, draft)
        self.assertIsNotNone(payload, issues)
        self.assertEqual(list(Draft202012Validator(schema).iter_errors(payload)), [])
        invalid = copy.deepcopy(payload)
        invalid["source"]["files"] = []
        self.assertTrue(list(Draft202012Validator(schema).iter_errors(invalid)))
