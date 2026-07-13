import importlib
import json
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

SCRIPT_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(SCRIPT_DIR))

import review_gemini


def test_is_rate_limit_error_detects_429_in_message():
    class FakeExc(Exception):
        pass

    assert review_gemini._is_rate_limit_error(
        FakeExc("429 RESOURCE_EXHAUSTED. {'error': ...}")
    )
    assert review_gemini._is_rate_limit_error(FakeExc("RESOURCE_EXHAUSTED"))


def test_is_rate_limit_error_detects_status_code_attr():
    exc = type("FakeApiException", (Exception,), {})("rate limit")
    exc.status_code = 429
    assert review_gemini._is_rate_limit_error(exc)


def test_is_rate_limit_error_false_for_other_errors():
    class FakeExc(Exception):
        pass

    assert not review_gemini._is_rate_limit_error(FakeExc("400 INVALID_ARGUMENT"))
    assert not review_gemini._is_rate_limit_error(FakeExc("connection refused"))


def test_call_gemini_with_retry_eventually_succeeds(monkeypatch):
    """Simulate 429 on first two attempts, success on third."""
    monkeypatch.setattr(review_gemini, "_INITIAL_BACKOFF_SECONDS", 0.001)
    monkeypatch.setattr(review_gemini, "_BACKOFF_MULTIPLIER", 1.0)

    call_count = {"n": 0}

    class MockClient:
        class models:
            @staticmethod
            def generate_content(model, contents, config):
                call_count["n"] += 1
                if call_count["n"] < 3:
                    raise Exception("429 RESOURCE_EXHAUSTED")
                return {"text": "ok"}

    result = review_gemini._call_gemini_with_retry(MockClient(), "m", "c", "cfg")
    assert call_count["n"] == 3
    assert result == {"text": "ok"}


def test_call_gemini_with_retry_raises_after_exhaustion(monkeypatch):
    monkeypatch.setattr(review_gemini, "_INITIAL_BACKOFF_SECONDS", 0.001)
    monkeypatch.setattr(review_gemini, "_BACKOFF_MULTIPLIER", 1.0)
    monkeypatch.setattr(review_gemini, "_MAX_RETRY_ATTEMPTS", 2)

    class MockClient:
        class models:
            @staticmethod
            def generate_content(model, contents, config):
                raise Exception("429 RESOURCE_EXHAUSTED")

    with pytest.raises(Exception, match="RESOURCE_EXHAUSTED"):
        review_gemini._call_gemini_with_retry(MockClient(), "m", "c", "cfg")


_EVIDENCE_RULE_MARKER = "MUST quote the diff line(s) where the reference is used"


def test_build_user_prompt_includes_evidence_rule(monkeypatch):
    """AT-1526 prompt contract: findings require diff-line evidence."""
    monkeypatch.delenv("EXISTING_COMMENTS", raising=False)
    prompt = review_gemini.build_user_prompt("dummy diff")
    assert "EVIDENCE RULE:" in prompt
    assert _EVIDENCE_RULE_MARKER in prompt


def test_evidence_rule_present_on_all_prompt_surfaces():
    """AT-1526: the diff-citation rule must exist on every prompt surface."""
    repo_root = SCRIPT_DIR.parent.parent
    surfaces = [
        SCRIPT_DIR / "review_prompt.md",
        repo_root / ".github" / "workflows" / "base-ai-review-single.yml",
        repo_root / "examples" / "prompts" / "code-review-system.md",
    ]
    for surface in surfaces:
        assert _EVIDENCE_RULE_MARKER in surface.read_text(encoding="utf-8"), surface


_DIFF_SCOPE_MARKER = "report it at the nearest affected remaining line"


def test_build_user_prompt_includes_diff_scope_rule(monkeypatch):
    """AT-1528 prompt contract: findings anchor to added/changed lines; removal-caused defects report at the nearest remaining line."""
    monkeypatch.delenv("EXISTING_COMMENTS", raising=False)
    prompt = review_gemini.build_user_prompt("dummy diff")
    assert "DIFF SCOPE:" in prompt
    assert _DIFF_SCOPE_MARKER in prompt


def test_diff_scope_rule_present_on_all_prompt_surfaces():
    """AT-1528: the diff-scope rule must sit beside the evidence rule on every surface."""
    repo_root = SCRIPT_DIR.parent.parent
    surfaces = [
        SCRIPT_DIR / "review_prompt.md",
        repo_root / ".github" / "workflows" / "base-ai-review-single.yml",
        repo_root / "examples" / "prompts" / "code-review-system.md",
    ]
    for surface in surfaces:
        assert _DIFF_SCOPE_MARKER in surface.read_text(encoding="utf-8"), surface


def test_existing_threads_prefix_caps_each_body(monkeypatch):
    """Per-body cap must match the Claude/Codex jq truncation (200 chars)."""
    long_body = "x" * 300
    short_body = "y" * 50
    threads = [
        {"author": "claude", "path": "a.py", "line": 1, "status": "unresolved", "body": long_body},
        {"author": "codex", "path": "b.py", "line": 2, "status": "resolved", "body": short_body},
    ]
    monkeypatch.setenv("EXISTING_COMMENTS", json.dumps(threads))
    monkeypatch.setenv("THREAD_COUNT", "2")
    prefix = review_gemini._existing_threads_prefix()
    capped = "x" * review_gemini._MAX_THREAD_BODY_CHARS + "..."
    assert capped in prefix
    assert long_body not in prefix
    assert short_body in prefix


def test_load_files_returns_empty_when_inputs_missing(tmp_path, monkeypatch):
    """load_files() must not raise when context.md / pr.diff are absent.

    The call site in main() lives outside the partial-fail try/except, so a
    FileNotFoundError there crashes the whole review job. Guard with
    Path.exists() and fall back to empty strings.
    """
    monkeypatch.chdir(tmp_path)
    assert not (tmp_path / "context.md").exists()
    assert not (tmp_path / "pr.diff").exists()

    context, diff = review_gemini.load_files()
    assert context == ""
    assert diff == ""


def test_load_files_reads_existing_inputs(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "context.md").write_text("ctx", encoding="utf-8")
    (tmp_path / "pr.diff").write_text("diff", encoding="utf-8")

    context, diff = review_gemini.load_files()
    assert context == "ctx"
    assert diff == "diff"


def test_main_raises_on_max_tokens_finish_reason(tmp_path, monkeypatch, capsys):
    """When Gemini returns finish_reason=MAX_TOKENS, main() must surface a clear
    RuntimeError via the existing partial-fail handler rather than letting the
    truncated JSON crash with an opaque JSONDecodeError.
    """
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("GOOGLE_AI_API_KEY", "test-key")

    fake_finish_reason = MagicMock()
    fake_finish_reason.name = "MAX_TOKENS"
    fake_candidate = MagicMock()
    fake_candidate.finish_reason = fake_finish_reason
    fake_usage = MagicMock()
    fake_usage.candidates_token_count = 8192
    fake_usage.prompt_token_count = 1000
    fake_usage.total_token_count = 9192
    fake_response = MagicMock()
    fake_response.candidates = [fake_candidate]
    fake_response.usage_metadata = fake_usage

    monkeypatch.setattr(
        review_gemini,
        "_call_gemini_with_retry",
        lambda *args, **kwargs: fake_response,
    )

    fake_client = MagicMock()
    fake_client.models = MagicMock()
    monkeypatch.setattr(
        review_gemini.genai, "Client", lambda api_key=None: fake_client
    )

    review_gemini.main()

    written = (tmp_path / review_gemini.REVIEW_FILE).read_text(encoding="utf-8")
    assert "truncated at MAX_OUTPUT_TOKENS" in written
    captured = capsys.readouterr()
    assert "truncated at MAX_OUTPUT_TOKENS" in captured.err


def test_max_output_tokens_env_override(monkeypatch):
    """GEMINI_MAX_OUTPUT_TOKENS env var must override the default at import time."""
    original = os.environ.get("GEMINI_MAX_OUTPUT_TOKENS")
    try:
        os.environ["GEMINI_MAX_OUTPUT_TOKENS"] = "16384"
        reloaded = importlib.reload(review_gemini)
        assert reloaded.MAX_OUTPUT_TOKENS == 16384
    finally:
        if original is None:
            os.environ.pop("GEMINI_MAX_OUTPUT_TOKENS", None)
        else:
            os.environ["GEMINI_MAX_OUTPUT_TOKENS"] = original
        importlib.reload(review_gemini)


def test_max_output_tokens_env_invalid_falls_back_to_default():
    """Invalid GEMINI_MAX_OUTPUT_TOKENS value falls back to default 32768 instead of crashing."""
    original = os.environ.get("GEMINI_MAX_OUTPUT_TOKENS")
    try:
        os.environ["GEMINI_MAX_OUTPUT_TOKENS"] = "not-a-number"
        reloaded = importlib.reload(review_gemini)
        assert reloaded.MAX_OUTPUT_TOKENS == 32768
    finally:
        if original is None:
            os.environ.pop("GEMINI_MAX_OUTPUT_TOKENS", None)
        else:
            os.environ["GEMINI_MAX_OUTPUT_TOKENS"] = original
        importlib.reload(review_gemini)
