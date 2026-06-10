import sys
from pathlib import Path

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
