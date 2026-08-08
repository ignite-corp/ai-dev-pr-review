#!/usr/bin/env python3
"""Extract review verdict JSON from codex CLI stdout when the model emits text
instead of using sandbox tools to write review-codex.json directly.

Usage: extract_codex_json.py <input-log> <output-json>

The codex log contains the user prompt, internal events, then the assistant's
text response (which should contain a JSON object with summary/early_exit/issues).
We find the LAST balanced { ... } block that parses as JSON and contains all
three required fields.

Exits 0 if extracted, 1 if no valid JSON found.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from review_status import stamp_model_status


def _find_balanced_blocks(text: str) -> list[str]:
    """Return all top-level balanced {...} blocks, in source order."""
    blocks: list[str] = []
    depth = 0
    start = -1
    for i, ch in enumerate(text):
        if ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0 and start >= 0:
                blocks.append(text[start : i + 1])
                start = -1
    return blocks


REQUIRED = {"summary", "early_exit", "issues"}


def extract(log_text: str) -> dict | None:
    """Return the LAST JSON object containing summary/early_exit/issues."""
    candidates = _find_balanced_blocks(log_text)
    # Prefer the LAST valid candidate -- assistant's final response.
    for block in reversed(candidates):
        try:
            obj = json.loads(block)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict) and REQUIRED.issubset(obj.keys()):
            return obj
    return None


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        print("Usage: extract_codex_json.py <input-log> <output-json>", file=sys.stderr)
        return 2
    log_path = Path(argv[1])
    out_path = Path(argv[2])
    if not log_path.is_file():
        print(f"::error::Log file not found: {log_path}", file=sys.stderr)
        return 1
    obj = extract(log_path.read_text(encoding="utf-8", errors="replace"))
    if obj is None:
        return 1
    stamp_model_status(obj)
    out_path.write_text(json.dumps(obj), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
