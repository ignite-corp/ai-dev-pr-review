"""Smoke test: review-schema.json is compatible with google-genai Schema validation.

google-genai accepts OpenAPI 3.0 subset only. JSON Schema type unions
("type": ["string", "null"]) are not supported; nullable fields must use
"type": "string", "nullable": true instead.
"""

from __future__ import annotations

import json
from pathlib import Path

SCHEMA_PATH = Path(__file__).parent.parent.parent / "schemas" / "review-schema.json"


def test_review_schema_passes_genai_validation() -> None:
    from google.genai import types

    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    # Raises pydantic.ValidationError if any field uses unsupported type union form.
    _ = types.Schema(**schema)


def test_review_schema_has_no_type_unions() -> None:
    """No field should use the JSON Schema union form ["X", "null"]."""
    schema_text = SCHEMA_PATH.read_text(encoding="utf-8")
    schema = json.loads(schema_text)

    def _check(node: object, path: str) -> None:
        if isinstance(node, dict):
            if isinstance(node.get("type"), list):
                raise AssertionError(
                    f"Type union found at {path}: {node['type']!r}. "
                    "Use \"nullable\": true instead."
                )
            for key, value in node.items():
                _check(value, f"{path}.{key}")
        elif isinstance(node, list):
            for i, item in enumerate(node):
                _check(item, f"{path}[{i}]")

    _check(schema, "$")


def test_nullable_fields_present() -> None:
    """file, line, suggestion must be marked nullable: true."""
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    item_props = schema["properties"]["issues"]["items"]["properties"]

    for field in ("file", "line", "suggestion"):
        prop = item_props[field]
        assert prop.get("nullable") is True, (
            f"issues.items.properties.{field} missing nullable: true"
        )
        assert isinstance(prop.get("type"), str), (
            f"issues.items.properties.{field}.type must be a single string"
        )
