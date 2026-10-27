#!/usr/bin/env python3
"""Validate Claude's structured output and write the host-owned verdict.

Older executions without structured output retain text extraction as a fallback.

Usage: python3 extract_claude_review.py <execution_file>
"""

from __future__ import annotations

import json
import os
import shlex
import sys
from pathlib import Path
from typing import Any, TypeGuard

from github_pr_support import is_json_array, is_json_object
from review_status import stamp_model_status, write_verdict_json


_MAX_SCAN_BYTES = 50_000
_OUTPUT_FILE = "review-claude.json"


def _valid_review(obj: object) -> TypeGuard[dict[str, Any]]:
    if not (
        is_json_object(obj)
        and isinstance(obj.get("summary"), str)
        and type(obj.get("early_exit")) is bool
    ):
        return False
    issues = obj.get("issues")
    if not is_json_array(issues):
        return False
    for issue in issues:
        if not (
            is_json_object(issue)
            and issue.get("severity") in ("critical", "major", "minor", "suggestion")
            and isinstance(issue.get("description"), str)
        ):
            return False
        if any(
            issue.get(key) is not None and not isinstance(issue[key], str)
            for key in ("file", "suggestion")
        ):
            return False
        if issue.get("line") is not None and type(issue["line"]) is not int:
            return False
    return True


def review_json_schema() -> dict[str, Any]:
    """Translate the shared Gemini nullable schema to standard JSON Schema."""
    path = Path(__file__).resolve().parents[1] / "schemas/review-schema.json"
    schema = json.loads(path.read_text(encoding="utf-8"))

    def convert(node: Any) -> None:
        if is_json_object(node):
            if node.pop("nullable", False):
                node["type"] = [node["type"], "null"]
            if node.get("type") == "object":
                node["additionalProperties"] = False
            for value in node.values():
                convert(value)
        elif is_json_array(node):
            for value in node:
                convert(value)

    convert(schema)
    return schema


def _write_review(obj: dict[str, Any]) -> bool:
    stamp_model_status(obj)
    # Link-safe: the workspace is model-writable, so _OUTPUT_FILE may be a
    # symlink; do not write through it (AT-2941).
    write_verdict_json(_OUTPUT_FILE, obj, indent=2, ensure_ascii=False)
    print(f"Extracted review-claude.json: {len(obj['issues'])} issues")
    return True


def _try_parse_review(text: str) -> dict[str, Any] | None:
    """Try to extract a review JSON object from text using json.loads.

    Scans only the last _MAX_SCAN_BYTES of text since the review JSON
    is typically the final output. Uses json.JSONDecoder.raw_decode
    which correctly handles braces inside string literals.
    """
    if len(text) > _MAX_SCAN_BYTES:
        text = text[-_MAX_SCAN_BYTES:]
    decoder = json.JSONDecoder()
    # Scan in reverse -- the review JSON is typically the last output
    positions = [i for i, c in enumerate(text) if c == "{"]
    for pos in reversed(positions):
        try:
            obj, _ = decoder.raw_decode(text, pos)
            if _valid_review(obj):
                return obj
        except json.JSONDecodeError:
            continue
    return None


def extract_review(exec_path: str, structured_output: str = "") -> bool:
    """Parse execution log and write review-claude.json if found."""
    if structured_output:
        try:
            obj = json.loads(structured_output)
        except json.JSONDecodeError:
            return False
        return _write_review(obj) if _valid_review(obj) else False
    try:
        with open(exec_path, encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        print(f"Cannot parse {exec_path}: {e}", file=sys.stderr)
        return False

    entries = data if is_json_array(data) else [data]
    # Prefer the SDK result over text that may include draft JSON or input quotes.
    for msg in reversed(entries):
        if not is_json_object(msg):
            continue
        if msg.get("type") == "result" and "structured_output" in msg:
            obj = msg["structured_output"]
            if msg.get("subtype") != "success" or msg.get("is_error") is not False:
                return False
            return _write_review(obj) if _valid_review(obj) else False
    for msg in reversed(entries):
        if not is_json_object(msg):
            continue
        message = msg.get("message")
        raw = (
            message.get("content", [])
            if is_json_object(message)
            else msg.get("content", [])
        )
        contents = raw if is_json_array(raw) else [raw]
        for content in contents:
            text = content.get("text", "") if is_json_object(content) else str(content)
            obj = _try_parse_review(text)
            if obj is not None:
                return _write_review(obj)
    print("No review JSON found in execution log")
    return False


def main() -> None:
    if sys.argv[1:] == ["--schema"]:
        print(shlex.quote(json.dumps(review_json_schema(), separators=(",", ":"))))
        return
    if len(sys.argv) != 2:
        print(f"Usage: {sys.argv[0]} <execution_file>", file=sys.stderr)
        sys.exit(1)
    if not extract_review(sys.argv[1], os.environ.get("STRUCTURED_OUTPUT", "")):
        sys.exit(1)


if __name__ == "__main__":
    main()
