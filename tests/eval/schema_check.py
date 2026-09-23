#!/usr/bin/env python3
"""Stdlib JSON-Schema subset validator for the shipped eval/runtime schemas.

Checks `type`, `enum`, `const`, `required`, `properties`,
`additionalProperties: false` and `items` recursively — the subset the
`run-record`, `finding-v3`, `review-output-v3` and `verdict` schemas use.
It is not a full JSON Schema validator (no `pattern`, `format`, `$ref`,
`oneOf`); the schemas are written so that this subset is what matters
at runtime, and CI stays dependency-free (AGENTS.md Rule #2 posture for
tooling that runs on every PR).

    python3 tests/eval/schema_check.py SCHEMA INSTANCE
    python3 tests/eval/schema_check.py --all      # shipped schemas vs their examples
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

SCHEMAS_DIR: Path = Path(__file__).resolve().parent / "schemas"
EXAMPLES_DIR: Path = SCHEMAS_DIR / "examples"
TYPE_MAP: dict[str, tuple[type, ...]] = {
    "object": (dict,),
    "array": (list,),
    "string": (str,),
    "integer": (int,),
    "number": (int, float),
    "boolean": (bool,),
    "null": (type(None),),
}


def _walk(schema: dict[str, Any], instance: Any, where: str, out: list[str]) -> None:
    expected = schema.get("type")
    if expected is not None:
        types: list[str] = expected if isinstance(expected, list) else [expected]
        allowed: tuple[type, ...] = tuple(t for name in types for t in TYPE_MAP.get(name, ()))
        if isinstance(instance, bool) and "boolean" not in types:
            out.append(f"{where}: boolean where {expected} expected")
            return
        if allowed and not isinstance(instance, allowed):
            out.append(f"{where}: expected type {expected}, got {type(instance).__name__}")
            return
    if "enum" in schema and instance not in schema["enum"]:
        out.append(f"{where}: value {instance!r} not in enum {schema['enum']}")
    if "const" in schema and instance != schema["const"]:
        out.append(f"{where}: value {instance!r} != const {schema['const']!r}")
    if isinstance(instance, dict):
        props: dict[str, Any] = schema.get("properties", {})
        for key in schema.get("required", []):
            if key not in instance:
                out.append(f"{where}: missing required key {key!r}")
        for key, value in instance.items():
            if key in props:
                _walk(props[key], value, f"{where}.{key}", out)
            elif schema.get("additionalProperties") is False:
                out.append(f"{where}: unexpected key {key!r} (additionalProperties is false)")
    elif isinstance(instance, list) and isinstance(schema.get("items"), dict):
        for index, item in enumerate(instance):
            _walk(schema["items"], item, f"{where}[{index}]", out)


def validate(schema: dict[str, Any], instance: Any, *, label: str = "instance") -> list[str]:
    """Return a list of human-readable problems (empty when valid)."""
    problems: list[str] = []
    if not isinstance(schema, dict) or "$schema" not in schema:
        return [f"{label}: schema must be an object carrying '$schema'"]
    _walk(schema, instance, label, problems)
    return problems


def load(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def check_pair(schema_path: Path, instance_path: Path) -> list[str]:
    try:
        schema = load(schema_path)
    except (OSError, json.JSONDecodeError) as exc:
        return [f"{schema_path}: cannot load ({exc})"]
    try:
        instance = load(instance_path)
    except (OSError, json.JSONDecodeError) as exc:
        return [f"{instance_path}: cannot load ({exc})"]
    return validate(schema, instance, label=str(instance_path))


def shipped_pairs() -> list[tuple[Path, Path]]:
    pairs: list[tuple[Path, Path]] = []
    for schema_path in sorted(SCHEMAS_DIR.glob("*.schema.json")):
        stem: str = schema_path.name[: -len(".schema.json")]
        example: Path = EXAMPLES_DIR / f"{stem}.example.json"
        pairs.append((schema_path, example))
    return pairs


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("schema", nargs="?", help="schema path")
    parser.add_argument("instance", nargs="?", help="instance path")
    parser.add_argument("--all", action="store_true", help="check every shipped schema against its example")
    args = parser.parse_args(argv)
    problems: list[str] = []
    checked: int = 0
    if args.all:
        for schema_path, example in shipped_pairs():
            if not example.is_file():
                problems.append(f"{example}: missing example for {schema_path.name}")
                continue
            problems.extend(check_pair(schema_path, example))
            checked += 1
    elif args.schema and args.instance:
        problems.extend(check_pair(Path(args.schema), Path(args.instance)))
        checked = 1
    else:
        parser.error("give SCHEMA INSTANCE or --all")
    for line in problems:
        print(f"FAIL {line}")
    print(f"schema_check: {checked} pair(s), {len(problems)} problem(s)")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
