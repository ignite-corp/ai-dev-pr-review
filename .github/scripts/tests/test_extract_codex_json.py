"""Smoke test: extract_codex_json correctly recovers JSON from codex stdout logs."""

from __future__ import annotations

import json
from pathlib import Path

from extract_codex_json import extract, extract_events, main, stamp_model_status


def test_extracts_last_json_with_required_fields():
    log = """
    Some non-JSON text before.
    {"summary": "x", "issues": []}
    More text.
    {"summary": "Final review", "early_exit": false, "issues": [{"severity":"minor","file":"a","line":1,"description":"d","suggestion":null}]}
    Trailing log line.
    """
    obj = extract(log)
    assert obj is not None
    assert obj["summary"] == "Final review"
    assert obj["early_exit"] is False
    assert len(obj["issues"]) == 1


def test_returns_none_when_no_valid_block():
    log = "Just some non-JSON text. {invalid json}"
    assert extract(log) is None


def test_event_reader_ignores_raw_tool_reasoning_and_incomplete_messages():
    verdict = {"summary": "candidate", "early_exit": False, "issues": []}
    text = json.dumps(verdict)
    agent = {"type": "item.completed", "item": {"type": "agent_message", "text": text}}
    assert extract_events(json.dumps(agent)) == verdict
    candidates = [
        verdict,
        {"type": "item.started", "item": {"type": "agent_message", "text": text}},
        {
            "type": "item.completed",
            "item": {
                "type": "command_execution",
                "text": text,
                "aggregated_output": text,
            },
        },
        {"type": "item.completed", "item": {"type": "reasoning", "text": text}},
    ]
    for event in candidates:
        assert extract_events(json.dumps(event)) is None


def test_events_cli_uses_the_existing_link_safe_stamped_writer(tmp_path: Path):
    verdict = {
        "summary": "candidate",
        "early_exit": False,
        "issues": [],
        "status": "failed",
    }
    event = {
        "type": "item.completed",
        "item": {"type": "agent_message", "text": json.dumps(verdict)},
    }
    events = tmp_path / "events.jsonl"
    events.write_text(json.dumps(event))
    outside = tmp_path / "untouched"
    outside.write_text("preserve me")
    output = tmp_path / "review.json"
    output.symlink_to(outside)
    assert main(["extract_codex_json.py", "--events", str(events), str(output)]) == 0
    assert outside.read_text() == "preserve me"
    assert not output.is_symlink()
    assert json.loads(output.read_text())["status"] == "ok"


def test_picks_last_valid_when_multiple_present():
    log = """
    {"summary": "first", "early_exit": false, "issues": []}
    {"summary": "second", "early_exit": false, "issues": []}
    """
    obj = extract(log)
    assert obj is not None
    assert obj["summary"] == "second"


def test_handles_nested_objects_in_issues():
    log = """
    {"summary": "x", "early_exit": false, "issues": [{"severity":"minor","file":"a","line":1,"description":"d","suggestion":"fix"}]}
    """
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
