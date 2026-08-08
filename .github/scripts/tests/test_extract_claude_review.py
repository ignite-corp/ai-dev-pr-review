"""Tests for extract_claude_review status stamping (AT-1799 contract)."""

from __future__ import annotations

import json
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(SCRIPT_DIR))

from extract_claude_review import extract_review, stamp_model_status


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
