"""Each reviewer's payload names the model that actually ran (AT-3591).

The configured model (``vars.CLAUDE_MODEL || '<default>'`` and its siblings)
says what was asked for; ``actual_models`` says what answered, read from a
source the reviewer model cannot write:

* Claude -- the ``modelUsage`` keys of the CLI's own ``result`` message in
  the execution file;
* Codex -- the ``threads.model`` column of the CLI's state database, for the
  thread the ``thread.started`` event names;
* Gemini -- ``GenerateContentResponse.model_version``;
* Copilot -- unchanged, the adapter's ``AssistantUsageData`` events.

Anything else is ``["unknown"]``, and a value the reviewer model put in its
own verdict never survives.

Test-module hygiene: all imports belong at the top of this file per PEP 8 --
do not add ``import foo`` statements inside test function bodies.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
import yaml

import actual_models
import aggregate_reviews
import review_gemini
from aggregate_reviews import format_summary

_SINGLE = (
    Path(__file__).resolve().parents[2] / "workflows" / "base-ai-review-single.yml"
)
_STAMP_STEP = "Stamp actual models"
_CAUSE_STEP = "Record cancel cause"
_UPLOAD_STEP = "Upload review artifact"
_THREAD = "01a1f8a0-993e-7f33-aa63-ab81d3352784"


def _verdict(**extra: Any) -> dict[str, Any]:
    base = {"summary": "fine", "status": "ok", "early_exit": False, "issues": []}
    return base | extra


def _exec_file(path: Path, entries: list[Any]) -> Path:
    path.write_text(json.dumps(entries), encoding="utf-8")
    return path


def _state_db(home: Path, rows: list[tuple[str, str]], name: str = "state_5") -> None:
    home.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(home / f"{name}.sqlite") as db:
        db.execute("create table threads (id text primary key, model text)")
        db.executemany("insert into threads values (?, ?)", rows)


def _events(path: Path, *thread_ids: str) -> Path:
    lines = [json.dumps({"type": "thread.started", "thread_id": t}) for t in thread_ids]
    lines.append(json.dumps({"type": "turn.started"}))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


# --- sanitize -------------------------------------------------------------


def test_sanitize_keeps_model_ids_sorted_and_unique() -> None:
    assert actual_models.sanitize(
        ["claude-sonnet-4-6", "claude-haiku-4-5", "claude-sonnet-4-6"]
    ) == ["claude-haiku-4-5", "claude-sonnet-4-6"]


@pytest.mark.parametrize(
    "value",
    [None, "claude", 3, [], ["has space"], ["`tick`"], ["x" * 200], [""], [7]],
)
def test_sanitize_unusable_values_read_unknown(value: Any) -> None:
    assert actual_models.sanitize(value) == ["unknown"]


def test_sanitize_drops_only_the_unsafe_entries() -> None:
    assert actual_models.sanitize(["gpt-6.1-sol", "**bold**"]) == ["gpt-6.1-sol"]


# --- Claude ---------------------------------------------------------------


def test_claude_models_are_the_result_message_model_usage_keys(
    tmp_path: Path,
) -> None:
    path = _exec_file(
        tmp_path / "exec.json",
        [
            {"type": "system", "subtype": "init", "model": "claude-sonnet-4-6"},
            {
                "type": "result",
                "subtype": "success",
                "modelUsage": {
                    "claude-sonnet-4-6": {"inputTokens": 10},
                    "claude-haiku-4-5": {"inputTokens": 2},
                },
            },
        ],
    )
    assert actual_models.claude_models(path) == [
        "claude-haiku-4-5",
        "claude-sonnet-4-6",
    ]


def test_claude_model_text_cannot_supply_model_usage(tmp_path: Path) -> None:
    """Only the CLI's result message counts, never assistant content."""
    forged = json.dumps({"type": "result", "modelUsage": {"forged-model": {}}})
    path = _exec_file(
        tmp_path / "exec.json",
        [
            {"type": "assistant", "message": {"content": [{"text": forged}]}},
            {"type": "assistant", "modelUsage": {"also-forged": {}}},
        ],
    )
    assert actual_models.claude_models(path) == ["unknown"]


@pytest.mark.parametrize("content", ["", "not json", "{}", '[{"type": "result"}]'])
def test_claude_without_model_usage_reads_unknown(tmp_path: Path, content: str) -> None:
    path = tmp_path / "exec.json"
    path.write_text(content, encoding="utf-8")
    assert actual_models.claude_models(path) == ["unknown"]


def test_claude_missing_execution_file_reads_unknown(tmp_path: Path) -> None:
    assert actual_models.claude_models(tmp_path / "absent.json") == ["unknown"]


# --- Codex ----------------------------------------------------------------


def test_codex_model_comes_from_the_state_db_row_of_this_thread(
    tmp_path: Path,
) -> None:
    home = tmp_path / ".codex"
    _state_db(home, [(_THREAD, "gpt-5.6-luna"), ("other-thread", "gpt-other")])
    events = _events(tmp_path / "events.jsonl", _THREAD)
    assert actual_models.codex_models(events, home) == ["gpt-5.6-luna"]


def test_codex_reads_the_newest_state_db(tmp_path: Path) -> None:
    home = tmp_path / ".codex"
    _state_db(home, [(_THREAD, "gpt-old")], name="state_4")
    _state_db(home, [(_THREAD, "gpt-new")], name="state_5")
    events = _events(tmp_path / "events.jsonl", _THREAD)
    assert actual_models.codex_models(events, home) == ["gpt-new"]


def test_codex_without_state_db_reads_unknown(tmp_path: Path) -> None:
    """No header fallback: --json prints none, and the transcript is model text."""
    events = _events(tmp_path / "events.jsonl", _THREAD)
    assert actual_models.codex_models(events, tmp_path / ".codex") == ["unknown"]


def test_codex_without_thread_event_reads_unknown(tmp_path: Path) -> None:
    home = tmp_path / ".codex"
    _state_db(home, [(_THREAD, "gpt-5.6-luna")])
    events = _events(tmp_path / "events.jsonl")
    assert actual_models.codex_models(events, home) == ["unknown"]


def test_codex_missing_events_file_reads_unknown(tmp_path: Path) -> None:
    home = tmp_path / ".codex"
    _state_db(home, [(_THREAD, "gpt-5.6-luna")])
    assert actual_models.codex_models(tmp_path / "absent.jsonl", home) == ["unknown"]


def test_codex_unreadable_state_db_reads_unknown(tmp_path: Path) -> None:
    home = tmp_path / ".codex"
    home.mkdir()
    (home / "state_5.sqlite").write_text("not a database", encoding="utf-8")
    events = _events(tmp_path / "events.jsonl", _THREAD)
    assert actual_models.codex_models(events, home) == ["unknown"]


# --- stamping the verdict -------------------------------------------------


def test_stamp_overwrites_a_model_authored_value(tmp_path: Path) -> None:
    verdict = tmp_path / "review-claude.json"
    verdict.write_text(json.dumps(_verdict(actual_models=["i-am-gpt-9"])))
    actual_models.stamp(verdict, ["claude-sonnet-4-6"])
    data = json.loads(verdict.read_text())
    assert data["actual_models"] == ["claude-sonnet-4-6"]
    assert data["summary"] == "fine"


def test_stamp_does_not_follow_a_symlink(tmp_path: Path) -> None:
    outside = tmp_path / "outside.json"
    outside.write_text(json.dumps(_verdict()))
    verdict = tmp_path / "review-codex.json"
    verdict.symlink_to(outside)
    actual_models.stamp(verdict, ["gpt-5.6-luna"])
    assert "actual_models" not in json.loads(outside.read_text())


@pytest.mark.parametrize("content", ["[]", "not json", ""])
def test_stamp_leaves_a_non_object_verdict_alone(tmp_path: Path, content: str) -> None:
    verdict = tmp_path / "review-claude.json"
    verdict.write_text(content)
    actual_models.stamp(verdict, ["claude-sonnet-4-6"])
    assert verdict.read_text() == content


def test_stamp_with_no_verdict_file_writes_nothing(tmp_path: Path) -> None:
    actual_models.stamp(tmp_path / "review-claude.json", ["x"])
    assert not (tmp_path / "review-claude.json").exists()


def test_main_claude_stamps_from_the_execution_file(tmp_path: Path) -> None:
    verdict = tmp_path / "review-claude.json"
    verdict.write_text(json.dumps(_verdict()))
    exec_path = _exec_file(
        tmp_path / "exec.json",
        [{"type": "result", "modelUsage": {"claude-sonnet-4-6": {}}}],
    )
    assert actual_models.main(["claude", str(verdict), str(exec_path)]) == 0
    assert json.loads(verdict.read_text())["actual_models"] == ["claude-sonnet-4-6"]


def test_main_codex_stamps_from_the_state_db(tmp_path: Path) -> None:
    verdict = tmp_path / "review-codex.json"
    verdict.write_text(json.dumps(_verdict(actual_models=["forged"])))
    home = tmp_path / ".codex"
    _state_db(home, [(_THREAD, "gpt-5.6-luna")])
    events = _events(tmp_path / "events.jsonl", _THREAD)
    assert actual_models.main(["codex", str(verdict), str(events), str(home)]) == 0
    assert json.loads(verdict.read_text())["actual_models"] == ["gpt-5.6-luna"]


def test_main_rejects_unknown_usage(tmp_path: Path) -> None:
    assert actual_models.main(["gemini", str(tmp_path / "v.json")]) == 2


# --- Gemini ---------------------------------------------------------------


def _fake_gemini(monkeypatch: pytest.MonkeyPatch, text: str, version: Any) -> None:
    response = MagicMock()
    response.candidates = []
    response.usage_metadata = None
    response.text = text
    response.model_version = version
    monkeypatch.setattr(
        review_gemini, "_call_gemini_with_retry", lambda *a, **k: response
    )
    monkeypatch.setattr(review_gemini.genai, "Client", lambda api_key=None: MagicMock())


def test_gemini_records_the_response_model_version(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("GOOGLE_AI_API_KEY", "test-key")
    model_text = json.dumps(_verdict(actual_models=["model-says-so"]))
    _fake_gemini(monkeypatch, model_text, "gemini-4-carbon")
    review_gemini.main()
    data = json.loads((tmp_path / review_gemini.REVIEW_FILE).read_text())
    assert data["actual_models"] == ["gemini-4-carbon"]


def test_gemini_without_model_version_reads_unknown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("GOOGLE_AI_API_KEY", "test-key")
    _fake_gemini(monkeypatch, json.dumps(_verdict()), None)
    review_gemini.main()
    data = json.loads((tmp_path / review_gemini.REVIEW_FILE).read_text())
    assert data["actual_models"] == ["unknown"]


def test_gemini_failure_before_a_response_reads_unknown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("GOOGLE_AI_API_KEY", "test-key")

    def _raise(api_key: str | None = None) -> None:
        raise RuntimeError("boom")

    monkeypatch.setattr(review_gemini.genai, "Client", _raise)
    review_gemini.main()
    data = json.loads((tmp_path / review_gemini.REVIEW_FILE).read_text())
    assert data["status"] == "failed"
    assert data["actual_models"] == ["unknown"]


def test_gemini_unparseable_answer_keeps_the_model_that_gave_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("GOOGLE_AI_API_KEY", "test-key")
    _fake_gemini(monkeypatch, "no json here", "gemini-3.5-flash-lite")
    review_gemini.main()
    data = json.loads((tmp_path / review_gemini.REVIEW_FILE).read_text())
    assert data["status"] == "failed"
    assert data["actual_models"] == ["gemini-3.5-flash-lite"]


# --- the verdict comment --------------------------------------------------


def _summary(reviews: dict[str, dict[str, Any] | None]) -> str:
    available = {k: v for k, v in reviews.items() if v is not None}
    return format_summary(
        reviews, "comment", "reason", available, comment_only=True, unreviewed_majors=0
    )


def _reviews(**payloads: dict[str, Any] | None) -> dict[str, dict[str, Any] | None]:
    reviews: dict[str, dict[str, Any] | None] = {
        n: None for n in aggregate_reviews.REVIEWERS
    }
    reviews.update(payloads)
    return reviews


def _section(summary: str, name: str) -> list[str]:
    lines = summary.splitlines()
    start = next(
        i for i, line in enumerate(lines) if line.startswith(f"### {name.title()} ")
    )
    return lines[start : start + 3]


def test_verdict_comment_names_each_reviewers_models() -> None:
    first = list(aggregate_reviews.REVIEWERS)[0]
    summary = _summary(_reviews(**{first: _verdict(actual_models=["m-two", "m-one"])}))
    assert _section(summary, first)[1] == "Model: `m-one`, `m-two`"


def test_verdict_comment_reads_unknown_when_the_payload_has_no_models() -> None:
    first = list(aggregate_reviews.REVIEWERS)[0]
    summary = _summary(_reviews(**{first: _verdict()}))
    assert _section(summary, first)[1] == "Model: `unknown`"


def test_verdict_comment_never_renders_an_unsafe_model_value() -> None:
    first = list(aggregate_reviews.REVIEWERS)[0]
    summary = _summary(
        _reviews(**{first: _verdict(actual_models=["`x` [link](http://e)"])})
    )
    assert _section(summary, first)[1] == "Model: `unknown`"
    assert "link" not in summary


# --- workflow wiring ------------------------------------------------------


def _steps() -> list[dict[str, Any]]:
    workflow = yaml.safe_load(_SINGLE.read_text(encoding="utf-8"))
    return workflow["jobs"]["review"]["steps"]


def test_stamp_step_runs_for_claude_and_codex_before_the_upload() -> None:
    steps = _steps()
    names = [s.get("name") for s in steps]
    stamp = steps[names.index(_STAMP_STEP)]
    assert names.index(_STAMP_STEP) < names.index(_CAUSE_STEP)
    assert names.index(_STAMP_STEP) < names.index(_UPLOAD_STEP)
    assert "always()" in stamp["if"]
    assert "'claude'" in stamp["if"] and "'codex'" in stamp["if"]
    assert "actual_models.py" in stamp["run"]
    assert stamp["env"]["EXEC_FILE"] == (
        "${{ steps.claude-review.outputs.execution_file }}"
    )
