"""The local Codex reviewer must build the Actions prompt and fail the same way.

The prompt is the part that has to be exact: context.md, then the
unresolved-thread block, then the trusted review_prompt.md that ships in this
repository (a PR in the reviewed repository cannot reach it). The thread block
is not rebuilt here -- it is the same function the Claude reviewer uses, which
is the same text base-ai-review-single.yml writes in shell for both.

The verdict handling mirrors the workflow's: a direct write is trusted only
when it parses, a legacy file name is promoted, a text answer is recovered by
extract_codex_json.py, and every remaining path still leaves a "failed"
verdict so the aggregate counts a FAILED reviewer rather than an absent one.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

SCRIPT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SCRIPT_DIR))

import review_codex_local  # noqa: E402
from local_review_config import workflow_defaults  # noqa: E402
from reviewer_prompts import existing_threads_block  # noqa: E402


@pytest.fixture
def workdir(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    return tmp_path


def _verdict(workdir: Path) -> dict:
    return json.loads(
        (workdir / review_codex_local.REVIEW_FILE).read_text(encoding="utf-8")
    )


def _stub_cli(monkeypatch, exit_code: int, log: str, writes=None):
    def fake_run_cli(prompt, model):
        if writes is not None:
            Path(review_codex_local.REVIEW_FILE).write_text(writes)
        return exit_code, log

    monkeypatch.setattr(review_codex_local, "run_cli", fake_run_cli)


# --- prompt -----------------------------------------------------------------


def test_prompt_is_context_then_threads_then_the_trusted_prompt(workdir):
    Path("context.md").write_text("CONTEXT BODY\n", encoding="utf-8")
    threads = json.dumps([{"path": "a.py", "status": "unresolved", "body": "x"}])
    prompt = review_codex_local.build_prompt("1", threads)

    trusted = review_codex_local.TRUSTED_PROMPT.read_text(encoding="utf-8")
    assert prompt.startswith("CONTEXT BODY\n")
    assert prompt.endswith(trusted)
    assert existing_threads_block("1", threads) in prompt
    assert prompt.index("Existing review threads") < prompt.index(trusted)


def test_prompt_without_threads_omits_the_block(workdir):
    Path("context.md").write_text("CONTEXT BODY\n", encoding="utf-8")
    prompt = review_codex_local.build_prompt("0", "")
    assert "Existing review threads" not in prompt


def test_prompt_survives_a_missing_context(workdir):
    prompt = review_codex_local.build_prompt("0", "")
    assert prompt == review_codex_local.TRUSTED_PROMPT.read_text(encoding="utf-8")


def test_model_is_the_workflow_default_when_unset(workdir, monkeypatch):
    monkeypatch.delenv("CODEX_MODEL", raising=False)
    seen: dict[str, str] = {}

    def fake_run_cli(prompt, model):
        seen["model"] = model
        return 0, ""

    monkeypatch.setattr(review_codex_local, "run_cli", fake_run_cli)
    review_codex_local.main()
    assert seen["model"] == workflow_defaults()["CODEX_MODEL"]


# --- verdict handling -------------------------------------------------------


def test_a_direct_write_is_kept_and_stamped(workdir, monkeypatch):
    _stub_cli(
        monkeypatch,
        0,
        "",
        writes=json.dumps({"summary": "s", "early_exit": True, "issues": []}),
    )
    review_codex_local.main()
    # stamp_model_status re-derives a missing status from early_exit.
    assert _verdict(workdir)["status"] == "early_exit"


def test_a_model_emitted_failed_status_is_not_trusted(workdir, monkeypatch):
    """ "failed" is reserved for infrastructure paths (AT-1799)."""
    _stub_cli(
        monkeypatch,
        0,
        "",
        writes=json.dumps(
            {"summary": "s", "status": "failed", "early_exit": False, "issues": []}
        ),
    )
    review_codex_local.main()
    assert _verdict(workdir)["status"] == "ok"


def test_a_legacy_verdict_name_is_promoted(workdir, monkeypatch):
    def fake_run_cli(prompt, model):
        Path("verdict-openai.json").write_text(
            json.dumps({"summary": "s", "issues": []})
        )
        return 0, ""

    monkeypatch.setattr(review_codex_local, "run_cli", fake_run_cli)
    review_codex_local.main()
    verdict = _verdict(workdir)
    assert verdict["summary"] == "s"
    assert verdict["early_exit"] is False


def test_a_text_answer_is_recovered_from_the_run_log(workdir, monkeypatch):
    review = {"summary": "found one", "early_exit": False, "issues": []}
    _stub_cli(monkeypatch, 0, f"thinking...\n{json.dumps(review)}\ndone\n")
    review_codex_local.main()
    assert _verdict(workdir)["summary"] == "found one"


def test_output_without_a_verdict_fails_loudly(workdir, monkeypatch):
    _stub_cli(monkeypatch, 0, "I had trouble reading the diff.\n")
    review_codex_local.main()
    verdict = _verdict(workdir)
    assert verdict["status"] == "failed"
    assert verdict["error"] == "output_unparseable"


def test_a_nonzero_exit_fails_loudly_with_the_last_log_line(workdir, monkeypatch):
    _stub_cli(monkeypatch, 3, "starting\n\x1b[31mfatal: no credentials\x1b[0m\n")
    review_codex_local.main()
    verdict = _verdict(workdir)
    assert verdict["status"] == "failed"
    assert verdict["error"] == "cli_invocation_failed"
    assert verdict["error_detail"] == "fatal: no credentials"
    assert "exited 3" in verdict["summary"]


def test_a_malformed_direct_write_fails_loudly(workdir, monkeypatch):
    _stub_cli(monkeypatch, 0, "some output", writes="{not json")
    review_codex_local.main()
    verdict = _verdict(workdir)
    assert verdict["status"] == "failed"
    assert verdict["error"] == "output_unparseable"


def test_every_failure_path_leaves_a_verdict_file(workdir, monkeypatch):
    for exit_code, log in ((-1, ""), (1, "boom"), (0, "no json here")):
        (workdir / review_codex_local.REVIEW_FILE).unlink(missing_ok=True)
        _stub_cli(monkeypatch, exit_code, log)
        review_codex_local.main()
        assert _verdict(workdir)["status"] == "failed"


def test_an_uninstalled_cli_is_reported_not_raised(workdir, monkeypatch):
    def missing(*args, **kwargs):
        raise FileNotFoundError("codex")

    monkeypatch.setattr(review_codex_local.subprocess, "run", missing)
    assert review_codex_local.run_cli("prompt", "model") == (-1, "")


# --- log tail ---------------------------------------------------------------


def test_log_tail_strips_ansi_and_blank_lines():
    assert (
        review_codex_local.log_tail("a\n\x1b[1mlast line\x1b[0m\n\n\n") == "last line"
    )


def test_log_tail_is_capped():
    tail = review_codex_local.log_tail("x" * 500)
    assert len(tail) == review_codex_local._LOG_TAIL_CHARS


def test_log_tail_of_nothing_is_empty():
    assert review_codex_local.log_tail("") == ""
