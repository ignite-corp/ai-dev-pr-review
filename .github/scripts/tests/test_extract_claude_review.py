"""Tests for extract_claude_review status stamping (AT-1799 contract)."""

from __future__ import annotations

import json

import pytest


from extract_claude_review import extract_review, review_json_schema, stamp_model_status


def test_stamp_status_derives_ok_when_missing() -> None:
    obj = {"summary": "x", "early_exit": False, "issues": []}
    stamp_model_status(obj)
    assert obj["status"] == "ok"


def test_stamp_status_derives_early_exit_from_flag() -> None:
    obj = {"summary": "x", "early_exit": True, "issues": []}
    stamp_model_status(obj)
    assert obj["status"] == "early_exit"


def test_stamp_status_keeps_valid_model_value() -> None:
    obj = {"summary": "x", "early_exit": False, "issues": [], "status": "early_exit"}
    stamp_model_status(obj)
    assert obj["status"] == "early_exit"


def test_stamp_status_replaces_invalid_model_value() -> None:
    obj = {"summary": "x", "early_exit": False, "issues": [], "status": "weird"}
    stamp_model_status(obj)
    assert obj["status"] == "ok"


def test_stamp_status_rederives_model_emitted_failed() -> None:
    # "failed" is reserved for infrastructure paths -- never trusted from
    # model output; re-derive from the early_exit flag instead.
    obj = {"summary": "x", "early_exit": False, "issues": [], "status": "failed"}
    stamp_model_status(obj)
    assert obj["status"] == "ok"
    obj = {"summary": "x", "early_exit": True, "issues": [], "status": "failed"}
    stamp_model_status(obj)
    assert obj["status"] == "early_exit"


def test_extract_review_writes_status(tmp_path, monkeypatch) -> None:
    """End-to-end: extracted payload lands on disk with a stamped status."""
    monkeypatch.chdir(tmp_path)
    review = {"summary": "fine", "early_exit": False, "issues": []}
    exec_log = [{"content": [{"text": f"prefix {json.dumps(review)}"}]}]
    exec_path = tmp_path / "execution.json"
    exec_path.write_text(json.dumps(exec_log), encoding="utf-8")

    assert extract_review(str(exec_path)) is True
    written = json.loads((tmp_path / "review-claude.json").read_text())
    assert written["status"] == "ok"
    assert written["summary"] == "fine"


def test_extract_structured_result_before_assistant_text(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    review = {
        "summary": "structured",
        "status": "ok",
        "early_exit": False,
        "issues": [
            {
                "severity": "major",
                "file": None,
                "line": None,
                "description": "issue",
                "suggestion": None,
            }
        ],
    }
    log = [
        {
            "type": "assistant",
            "content": [{"text": json.dumps(dict(review, summary="text"))}],
        },
        {
            "type": "result",
            "subtype": "success",
            "is_error": False,
            "structured_output": review,
        },
    ]
    path = tmp_path / "execution.json"
    path.write_text(json.dumps(log))
    assert extract_review(str(path))
    assert json.loads((tmp_path / "review-claude.json").read_text()) == review


@pytest.mark.parametrize(
    "patch",
    [
        {"summary": 4},
        {"early_exit": "false"},
        {"issues": [{}]},
        {"issues": [{"severity": "bogus", "description": "x"}]},
        {"issues": [{"severity": "minor", "description": "x", "line": True}]},
    ],
)
def test_reject_invalid_model_payload(tmp_path, monkeypatch, patch) -> None:
    monkeypatch.chdir(tmp_path)
    review = {"summary": "fine", "early_exit": False, "issues": [], **patch}
    path = tmp_path / "execution.json"
    path.write_text(json.dumps([{"content": [{"text": json.dumps(review)}]}]))
    assert not extract_review(str(path))
    assert not (tmp_path / "review-claude.json").exists()


def test_reject_structured_output_from_failed_result(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    path = tmp_path / "execution.json"
    path.write_text(
        json.dumps(
            [
                {
                    "type": "result",
                    "subtype": "error_max_turns",
                    "is_error": True,
                    "structured_output": {
                        "summary": "bad",
                        "early_exit": False,
                        "issues": [],
                    },
                }
            ]
        )
    )
    assert not extract_review(str(path))


def test_structured_action_output_without_execution_file(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    review = {"summary": "action output", "early_exit": False, "issues": []}
    assert extract_review("", json.dumps(review))
    assert (
        json.loads((tmp_path / "review-claude.json").read_text())["summary"]
        == "action output"
    )


def test_legacy_sdk_assistant_text_fallback(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    path = tmp_path / "execution.json"
    path.write_text(
        json.dumps(
            [
                {
                    "type": "assistant",
                    "message": {
                        "content": [
                            {
                                "type": "text",
                                "text": json.dumps(
                                    {
                                        "summary": "legacy",
                                        "early_exit": False,
                                        "issues": [],
                                    }
                                ),
                            }
                        ]
                    },
                }
            ]
        )
    )
    assert extract_review(str(path))
    assert (
        json.loads((tmp_path / "review-claude.json").read_text())["summary"] == "legacy"
    )


def test_generated_schema_uses_standard_null_types() -> None:
    schema = review_json_schema()
    assert schema["additionalProperties"] is False
    issue = schema["properties"]["issues"]["items"]
    assert issue["additionalProperties"] is False
    assert issue["properties"]["file"]["type"] == ["string", "null"]
    assert issue["properties"]["line"]["type"] == ["integer", "null"]
