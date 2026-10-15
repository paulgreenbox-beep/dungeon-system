from __future__ import annotations

import re
import unittest
from pathlib import Path

from tools.project_structure.v041_ids import (
    MAX_OBJECT_ID_LENGTH,
    OBJECT_ID_RE,
    metadata_key,
    valid_object_id,
)
from tools.project_structure.v04_sklt import parse_declarations


class ObjectIdGrammarTests(unittest.TestCase):
    def test_preserves_canonical_dotted_ids(self) -> None:
        for object_id in (
            "renderer.settings",
            "render.layer-1",
            "A_2",
            "a",
            "renderer:settings",
            "file:stream",
        ):
            with self.subTest(object_id=object_id):
                self.assertTrue(valid_object_id(object_id))
                self.assertIsNotNone(OBJECT_ID_RE.fullmatch(object_id))

    def test_accepts_root_declared_ids(self) -> None:
        for object_id in (
            "declared:project:Demo",
            "declared:module:Renderer",
            "declared:capability:Draw",
            "declared:command:Render",
            "declared:action:ShowWarnings",
        ):
            with self.subTest(object_id=object_id):
                self.assertTrue(valid_object_id(object_id))

    def test_accepts_nested_declared_ids_emitted_by_parser(self) -> None:
        text = (
            "project Demo.v2:\n"
            "    module Renderer:GPU:\n"
            "        capability Draw.v1:\n"
            "        command Render:\n"
            "    action Reset:\n"
            "module Storage:\n"
            "    capability Save:\n"
        )
        records, issues = parse_declarations(
            Path("nested.sklt"), text, "module/nested.sklt", "module"
        )

        self.assertEqual(issues, [])
        ids = [record["object_id"] for record in records]
        self.assertEqual(
            ids,
            [
                "declared:project:Demo.v2",
                "declared:project:Demo.v2/module:Renderer:GPU",
                "declared:project:Demo.v2/module:Renderer:GPU/capability:Draw.v1",
                "declared:project:Demo.v2/module:Renderer:GPU/command:Render",
                "declared:project:Demo.v2/action:Reset",
                "declared:module:Storage",
                "declared:module:Storage/capability:Save",
            ],
        )
        self.assertTrue(all(valid_object_id(object_id) for object_id in ids))

    def test_rejects_paths_and_malformed_namespaces(self) -> None:
        invalid_ids = (
            "",
            "../secret",
            "/absolute/object",
            "C:\\temp\\object",
            "C:/temp/object",
            "\\\\server\\share\\object",
            "//server/share/object",
            "declared:project:Demo/../module:Renderer",
            "declared:project:Demo//module:Renderer",
            "declared:project:Demo/",
            "declared:capability:Draw/command:Run",
            "renderer.settings\n",
            "renderer.settings\r\n",
        )
        for object_id in invalid_ids:
            with self.subTest(object_id=object_id):
                self.assertFalse(valid_object_id(object_id))
        self.assertFalse(valid_object_id(None))
        self.assertFalse(valid_object_id(123))

    def test_enforces_the_1024_character_limit(self) -> None:
        self.assertEqual(MAX_OBJECT_ID_LENGTH, 1024)
        self.assertTrue(valid_object_id("a" * MAX_OBJECT_ID_LENGTH))
        self.assertFalse(valid_object_id("a" * (MAX_OBJECT_ID_LENGTH + 1)))

    def test_action_id_is_valid_even_if_coverage_support_is_separate(self) -> None:
        # Grammar validity is independent from a caller's object-kind support.
        self.assertTrue(valid_object_id("declared:action:ShowWarnings"))


class MetadataKeyTests(unittest.TestCase):
    def test_colliding_readable_prefixes_get_distinct_safe_keys(self) -> None:
        first = "declared:project:Demo/module:Renderer"
        second = "declared:project:Demo-module:Renderer"
        first_key = metadata_key(first)
        second_key = metadata_key(second)

        self.assertEqual(first_key[:-65], second_key[:-65])
        self.assertNotEqual(first_key, second_key)
        for key in (first_key, second_key):
            self.assertRegex(key, re.compile(r"^[A-Za-z0-9._-]+$"))
            self.assertNotIn("/", key)
            self.assertNotIn("\\", key)
            self.assertNotIn(":", key)

    def test_optional_hash_disambiguates_metadata_variants(self) -> None:
        object_id = "renderer.settings"
        first = metadata_key(object_id, "a" * 64)
        second = metadata_key(object_id, "b" * 64)
        self.assertNotEqual(first, second)
        self.assertTrue(first.endswith("-" + "a" * 64))
        self.assertTrue(second.endswith("-" + "b" * 64))
        with self.assertRaises(ValueError):
            metadata_key(object_id, "not-a-sha256")

    def test_metadata_key_rejects_invalid_ids(self) -> None:
        with self.assertRaises(ValueError):
            metadata_key("../escape")

    def test_colon_ids_remain_logical_and_do_not_enter_filename_keys(self) -> None:
        key = metadata_key("file:stream")
        self.assertNotIn(":", key)
        self.assertNotIn("/", key)
        self.assertNotIn("\\", key)


if __name__ == "__main__":
    unittest.main()
