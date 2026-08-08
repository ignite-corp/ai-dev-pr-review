#!/usr/bin/env python3
"""Extract review JSON from Claude's execution log file.

When claude-code-action writes review-claude.json via the Write tool,
this script is not needed. It serves as a fallback when the Write tool
fails to produce the file.

Usage: python3 extract_claude_review.py <execution_file>
"""

from __future__ import annotations

import json
import sys
from typing import Any

from review_status import stamp_model_status


_MAX_SCAN_BYTES = 50_000
_OUTPUT_FILE = "review-claude.json"


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
            if (
                isinstance(obj, dict)
                and "summary" in obj
                and "issues" in obj
                and "early_exit" in obj
                and isinstance(obj.get("issues"), list)
            ):
                return obj
        except json.JSONDecodeError:
            continue
    return None


def extract_review(exec_path: str) -> bool:
    """Parse execution log and write review-claude.json if found."""
    try:
        with open(exec_path, encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        print(f"Cannot parse {exec_path}: {e}", file=sys.stderr)
        return False

    entries = data if isinstance(data, list) else [data]
    for msg in reversed(entries):
        contents = msg.get("content", [])
        if not isinstance(contents, list):
            contents = [contents]
        for content in contents:
            text = (
                content.get("text", "") if isinstance(content, dict) else str(content)
            )
            obj = _try_parse_review(text)
            if obj is not None:
                stamp_model_status(obj)
                with open(_OUTPUT_FILE, "w", encoding="utf-8") as out:
                    json.dump(obj, out, indent=2, ensure_ascii=False)
                count = len(obj.get("issues", []))
                print(f"Extracted review-claude.json: {count} issues")
                return True
    print("No review JSON found in execution log")
    return False


def main() -> None:
    if len(sys.argv) != 2:
        print(f"Usage: {sys.argv[0]} <execution_file>", file=sys.stderr)
        sys.exit(1)
    if not extract_review(sys.argv[1]):
        sys.exit(1)


if __name__ == "__main__":
    main()
