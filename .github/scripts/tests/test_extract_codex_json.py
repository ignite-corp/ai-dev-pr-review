"""Smoke test: extract_codex_json correctly recovers JSON from codex stdout logs."""

from __future__ import annotations

import sys
from pathlib import Path

# Add parent dir to path for import
SCRIPT_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(SCRIPT_DIR))

from extract_codex_json import extract


def test_extracts_last_json_with_required_fields():
    log = '''
    Some non-JSON text before.
    {"summary": "x", "issues": []}
    More text.
    {"summary": "Final review", "early_exit": false, "issues": [{"severity":"minor","file":"a","line":1,"description":"d","suggestion":null}]}
    Trailing log line.
    '''
    obj = extract(log)
    assert obj is not None
    assert obj["summary"] == "Final review"
    assert obj["early_exit"] is False
    assert len(obj["issues"]) == 1


def test_returns_none_when_no_valid_block():
    log = "Just some non-JSON text. {invalid json}"
    assert extract(log) is None


def test_picks_last_valid_when_multiple_present():
    log = '''
    {"summary": "first", "early_exit": false, "issues": []}
    {"summary": "second", "early_exit": false, "issues": []}
    '''
    obj = extract(log)
    assert obj["summary"] == "second"


def test_handles_nested_objects_in_issues():
    log = '''
    {"summary": "x", "early_exit": false, "issues": [{"severity":"minor","file":"a","line":1,"description":"d","suggestion":"fix"}]}
    '''
    obj = extract(log)
    assert obj is not None
    assert obj["issues"][0]["suggestion"] == "fix"


if __name__ == "__main__":
    # Manual run
    test_extracts_last_json_with_required_fields()
    test_returns_none_when_no_valid_block()
    test_picks_last_valid_when_multiple_present()
    test_handles_nested_objects_in_issues()
    print("All tests passed.")
