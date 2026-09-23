"""`tests/eval/schema_check.py` — the stdlib schema-subset validator."""

from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any

_ROOT: Path = Path(__file__).resolve().parent.parent
_SPEC = importlib.util.spec_from_file_location(
    "schema_check", _ROOT / "tests" / "eval" / "schema_check.py"
)
assert _SPEC is not None and _SPEC.loader is not None
schema_check = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(schema_check)

SCHEMA: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "additionalProperties": False,
    "required": ["id", "sev", "items", "n"],
    "properties": {
        "id": {"type": "string"},
        "sev": {"enum": ["critical", "warning"]},
        "n": {"type": ["integer", "null"]},
        "v": {"const": "x/1"},
        "items": {"type": "array", "items": {"type": "object", "required": ["k"], "properties": {"k": {"type": "integer"}}}},
    },
}


class ValidateTests(unittest.TestCase):
    def test_valid_instance_has_no_problems(self) -> None:
        self.assertEqual(schema_check.validate(SCHEMA, {"id": "a", "sev": "warning", "n": None, "items": [{"k": 1}]}), [])

    def test_each_rule_reports(self) -> None:
        problems = schema_check.validate(
            SCHEMA, {"id": 3, "sev": "info", "n": True, "v": "y", "items": [{"z": 1}], "extra": 1}
        )
        joined = "\n".join(problems)
        for needle in ("expected type", "not in enum", "boolean where", "!= const", "missing required key 'k'", "unexpected key 'extra'"):
            self.assertIn(needle, joined, needle)

    def test_schema_without_dollar_schema_is_rejected(self) -> None:
        self.assertEqual(len(schema_check.validate({"type": "object"}, {})), 1)

    def test_shipped_schemas_validate_their_examples(self) -> None:
        for schema_path, example in schema_check.shipped_pairs():
            self.assertTrue(example.is_file(), example)
            self.assertEqual(schema_check.check_pair(schema_path, example), [], schema_path.name)
        self.assertGreaterEqual(len(schema_check.shipped_pairs()), 3)

    def test_cli_all_exits_zero_and_pair_mode_exits_one_on_problem(self) -> None:
        self.assertEqual(schema_check.main(["--all"]), 0)
        with tempfile.TemporaryDirectory() as tmp:
            s = Path(tmp) / "s.json"; i = Path(tmp) / "i.json"
            s.write_text(json.dumps(SCHEMA)); i.write_text(json.dumps({"id": "a"}))
            self.assertEqual(schema_check.main([str(s), str(i)]), 1)


if __name__ == "__main__":
    unittest.main()
