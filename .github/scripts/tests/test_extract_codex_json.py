"""Smoke test: extract_codex_json correctly recovers JSON from codex stdout logs."""

from __future__ import annotations

import sys
from pathlib import Path

# Add parent dir to path for import
SCRIPT_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(SCRIPT_DIR))

from extract_codex_json import extract, stamp_model_status


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


def test_stamp_status_derives_ok_when_missing():
    obj = {"summary": "x", "early_exit": False, "issues": []}
    stamp_model_status(obj)
    assert obj["status"] == "ok"


def test_stamp_status_derives_early_exit_from_flag():
    obj = {"summary": "x", "early_exit": True, "issues": []}
    stamp_model_status(obj)
    assert obj["status"] == "early_exit"


def test_stamp_status_keeps_valid_model_value():
    obj = {"summary": "x", "early_exit": False, "issues": [], "status": "early_exit"}
    stamp_model_status(obj)
    assert obj["status"] == "early_exit"


def test_stamp_status_replaces_invalid_model_value():
    obj = {"summary": "x", "early_exit": False, "issues": [], "status": "weird"}
    stamp_model_status(obj)
    assert obj["status"] == "ok"


def test_stamp_status_rederives_model_emitted_failed():
    # "failed" is reserved for infrastructure paths -- never trusted from
    # model output; re-derive from the early_exit flag instead.
    obj = {"summary": "x", "early_exit": False, "issues": [], "status": "failed"}
    stamp_model_status(obj)
    assert obj["status"] == "ok"
    obj = {"summary": "x", "early_exit": True, "issues": [], "status": "failed"}
    stamp_model_status(obj)
    assert obj["status"] == "early_exit"


if __name__ == "__main__":
    # Manual run
    test_extracts_last_json_with_required_fields()
    test_returns_none_when_no_valid_block()
    test_picks_last_valid_when_multiple_present()
    test_handles_nested_objects_in_issues()
    test_stamp_status_derives_ok_when_missing()
    test_stamp_status_derives_early_exit_from_flag()
    test_stamp_status_keeps_valid_model_value()
    test_stamp_status_replaces_invalid_model_value()
    test_stamp_status_rederives_model_emitted_failed()
    print("All tests passed.")
