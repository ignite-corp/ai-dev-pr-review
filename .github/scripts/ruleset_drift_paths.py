#!/usr/bin/env python3
"""Report which ruleset key paths differ between two normalized documents.

Usage: ``ruleset_drift_paths.py <stored.json> <live.json>``

Both arguments are paths to JSON files holding a normalized ruleset body (the
stored config and the live one). Prints, one per line, the sorted and
de-duplicated key paths whose values differ, including paths present in only
one document.

Array indices are normalized to ``[]`` so paths aggregate across elements
(``rules[].parameters.foo``). Values under such a path are compared as a
multiset, so reordering an array is not drift while adding, removing or
changing an element is.

Only KEYS are printed, never values: a path segment is always a JSON object key
or ``[]``. That is what makes the output safe for this public repo's Actions
log -- the ruleset bodies themselves are private, their schema keys are not.
"""

from __future__ import annotations

import json
import sys
from collections import Counter, defaultdict

# Label for a document that is a bare scalar or empty container at the top.
_ROOT = "(root)"
_ARRAY = "[]"


def _child(path: str, key: str) -> str:
    return f"{path}.{key}" if path else key


def _flatten(node: object, path: str, out: dict[str, Counter[str]]) -> None:
    """Accumulate leaf values per normalized key path.

    Non-empty containers are walked; anything else (scalar, empty object, empty
    array) is a leaf recorded under its own path. Leaf values are counted, not
    emitted -- they never leave this module.
    """
    if isinstance(node, dict) and node:
        for key, value in node.items():
            _flatten(value, _child(path, str(key)), out)
    elif isinstance(node, list) and node:
        for item in node:
            _flatten(item, f"{path}{_ARRAY}", out)
    else:
        out[path or _ROOT][json.dumps(node, sort_keys=True)] += 1


def _flatten_doc(doc: object) -> dict[str, Counter[str]]:
    out: dict[str, Counter[str]] = defaultdict(Counter)
    _flatten(doc, "", out)
    return out


def drifted_paths(stored: object, live: object) -> list[str]:
    """Return the sorted key paths whose values differ between the two docs."""
    left = _flatten_doc(stored)
    right = _flatten_doc(live)
    return sorted(p for p in set(left) | set(right) if left.get(p) != right.get(p))


def main() -> int:
    if len(sys.argv) != 3:
        print("usage: ruleset_drift_paths.py <stored.json> <live.json>", file=sys.stderr)
        return 2
    with open(sys.argv[1], encoding="utf-8") as fh:
        stored = json.load(fh)
    with open(sys.argv[2], encoding="utf-8") as fh:
        live = json.load(fh)
    for path in drifted_paths(stored, live):
        print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
