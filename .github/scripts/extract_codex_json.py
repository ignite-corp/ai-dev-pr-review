#!/usr/bin/env python3
"""Extract review verdict JSON from codex CLI stdout when the model emits text
instead of using sandbox tools to write review-codex.json directly.

Usage: extract_codex_json.py [--events] <input-log> <output-json>

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
from typing import Any

from github_pr_support import is_json_object
from review_status import stamp_model_status, write_verdict_json


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


def extract(log_text: str) -> dict[str, Any] | None:
    """Return the LAST JSON object containing summary/early_exit/issues."""
    candidates = _find_balanced_blocks(log_text)
    # Prefer the LAST valid candidate -- assistant's final response.
    for block in reversed(candidates):
        try:
            obj = json.loads(block)
        except json.JSONDecodeError:
            continue
        if is_json_object(obj) and REQUIRED.issubset(obj.keys()):
            return obj
    return None


def extract_events(log_text: str) -> dict[str, Any] | None:
    """Read completed assistant messages individually, newest valid verdict first."""
    for line in reversed(log_text.splitlines()):
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not is_json_object(event) or event.get("type") != "item.completed":
            continue
        item = event.get("item")
        if not is_json_object(item) or item.get("type") != "agent_message":
            continue
        text = item.get("text")
        if isinstance(text, str):
            verdict = extract(text)
            if verdict is not None:
                return verdict
    return None


def main(argv: list[str]) -> int:
    events_mode = len(argv) == 4 and argv[1] == "--events"
    if events_mode:
        argv = [argv[0], *argv[2:]]
    if len(argv) != 3:
        print(
            "Usage: extract_codex_json.py [--events] <input-log> <output-json>",
            file=sys.stderr,
        )
        return 2
    log_path = Path(argv[1])
    out_path = Path(argv[2])
    if not log_path.is_file():
        print(f"::error::Log file not found: {log_path}", file=sys.stderr)
        return 1
    reader = extract_events if events_mode else extract
    obj = reader(log_path.read_text(encoding="utf-8", errors="replace"))
    if obj is None:
        return 1
    stamp_model_status(obj)
    # Link-safe: the workspace is model-writable, so out_path may be a
    # symlink the model planted; do not write through it (AT-2941).
    write_verdict_json(out_path, obj)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
