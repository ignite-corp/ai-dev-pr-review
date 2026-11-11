"""Tests for switch_claude_auth: signature parsing, org var switch, fail-safe."""

from __future__ import annotations

import io
import json
import re
import sys
import urllib.error
from pathlib import Path
from typing import Any

import pytest
import switch_claude_auth as sca

_NOW = 1_800_000_000.0
_FORCE_PATH = "/orgs/ignite-corp/actions/variables/CLAUDE_FORCE_API"
_UNTIL_PATH = "/orgs/ignite-corp/actions/variables/CLAUDE_FORCE_API_UNTIL"
_CREATE_PATH = "/orgs/ignite-corp/actions/variables"


class FakeResponse:
    def __init__(self, status: int, body: dict | None = None) -> None:
        self.status = status
        self._body = json.dumps(body).encode() if body is not None else b""

    def read(self) -> bytes:
        return self._body

    def __enter__(self) -> FakeResponse:
        return self

    def __exit__(self, *args: Any) -> None:
        return None


class FakeApi:
    """Programmable urllib.request.urlopen replacement.

    `responses` maps "METHOD path" to a FakeResponse, an HTTPError code
    (int), or an exception instance. Unlisted calls return 200 {}. A list
    is answered one item per call, the last item repeating.
    """

    def __init__(self, responses: dict[str, Any] | None = None) -> None:
        self.responses = responses or {}
        self.calls: list[tuple[str, str, dict | None]] = []

    def __call__(self, req: Any, timeout: int = 0) -> FakeResponse:
        path = req.full_url.replace(sca._API_BASE, "")
        body = json.loads(req.data) if req.data else None
        self.calls.append((req.get_method(), path, body))
        result = self.responses.get(f"{req.get_method()} {path}")
        if isinstance(result, list):
            result = result.pop(0) if len(result) > 1 else result[0]
        if result is None:
            return FakeResponse(200, {})
        if isinstance(result, int):
            raise urllib.error.HTTPError(req.full_url, result, "err", {}, io.BytesIO())  # type: ignore[arg-type]
        if isinstance(result, Exception):
            raise result
        return result


# --- Signature parsing ---


def test_signature_with_epoch_returns_epoch():
    text = "blah Claude AI usage limit reached|1800003600 blah"
    assert sca._detect_limit(text, _NOW).epoch == 1800003600


def test_signature_multiple_matches_uses_last():
    text = "Claude AI usage limit reached|1800000100\nClaude AI usage limit reached|1800000200\n"
    assert sca._detect_limit(text, _NOW).epoch == 1800000200


def test_signature_without_epoch_falls_back_to_now_plus_5h():
    text = "Error: Usage Limit Reached, try later"
    assert sca._detect_limit(text, _NOW).epoch == int(_NOW) + 5 * 3600


def test_no_signature_returns_none():
    text = json.dumps({"content": [{"text": "review completed fine"}]})
    assert sca._detect_limit(text, _NOW).epoch is None


# --- detect-and-switch ---


def _run_detect(monkeypatch, tmp_path, exec_text: str, api: FakeApi) -> None:
    exec_file = tmp_path / "exec.json"
    exec_file.write_text(exec_text)
    monkeypatch.setenv("EXEC_FILE", str(exec_file))
    monkeypatch.setattr(sca.time, "time", lambda: _NOW)
    monkeypatch.setattr(sca.urllib.request, "urlopen", api)
    sca.detect_and_switch("corp-token")


def test_detect_no_signature_makes_no_api_calls(monkeypatch, tmp_path):
    api = FakeApi()
    _run_detect(monkeypatch, tmp_path, "all good", api)
    assert api.calls == []


def test_detect_missing_exec_file_is_noop(monkeypatch):
    api = FakeApi()
    monkeypatch.setenv("EXEC_FILE", "/nonexistent/exec.json")
    monkeypatch.setattr(sca.urllib.request, "urlopen", api)
    sca.detect_and_switch("corp-token")
    assert api.calls == []


def test_detect_sets_force_api_and_until_when_absent(monkeypatch, tmp_path):
    api = FakeApi(
        {
            f"GET {_UNTIL_PATH}": 404,
            f"PATCH {_UNTIL_PATH}": 404,
            f"PATCH {_FORCE_PATH}": 404,
            # GitHub answers a created variable with 201.
            f"POST {_CREATE_PATH}": FakeResponse(201, {}),
        }
    )
    _run_detect(monkeypatch, tmp_path, "Claude AI usage limit reached|1800003600", api)
    # UNTIL missing -> created via POST with the reset epoch.
    assert (
        "POST",
        _CREATE_PATH,
        {
            "name": "CLAUDE_FORCE_API_UNTIL",
            "value": "1800003600",
            "visibility": "all",
        },
    ) in api.calls
    # FORCE_API missing -> created via POST with value 'true'.
    assert (
        "POST",
        _CREATE_PATH,
        {"name": "CLAUDE_FORCE_API", "value": "true", "visibility": "all"},
    ) in api.calls


def test_detect_patches_existing_force_api_var(monkeypatch, tmp_path):
    api = FakeApi({f"GET {_UNTIL_PATH}": 404})
    _run_detect(monkeypatch, tmp_path, "Claude AI usage limit reached|5", api)
    assert (
        "PATCH",
        _FORCE_PATH,
        {"name": "CLAUDE_FORCE_API", "value": "true"},
    ) in api.calls


def _until_writes(api: FakeApi) -> list[str]:
    return [
        b["value"]
        for m, p, b in api.calls
        if m in ("PATCH", "POST") and b and b.get("name") == "CLAUDE_FORCE_API_UNTIL"
    ]


def _existing_until(value: str) -> FakeApi:
    return FakeApi(
        {
            f"GET {_UNTIL_PATH}": FakeResponse(
                200, {"name": "CLAUDE_FORCE_API_UNTIL", "value": value}
            )
        }
    )


def test_detect_keeps_existing_until_that_is_earlier(monkeypatch, tmp_path):
    api = _existing_until(str(int(_NOW) + 600))
    _run_detect(monkeypatch, tmp_path, "Claude AI usage limit reached|1800003600", api)
    assert _until_writes(api) == []
    # FORCE_API is still written.
    assert any(m == "PATCH" and p == _FORCE_PATH for m, p, _ in api.calls)


def test_detect_replaces_later_future_until_with_earlier_reset(monkeypatch, tmp_path):
    # min(existing, new): a later window does not outlive an earlier reset.
    api = _existing_until(str(int(_NOW) + 7200))
    _run_detect(monkeypatch, tmp_path, "Claude AI usage limit reached|1800003600", api)
    assert _until_writes(api) == ["1800003600"]
    assert any(m == "PATCH" and p == _FORCE_PATH for m, p, _ in api.calls)


def test_detect_replaces_expired_until(monkeypatch, tmp_path):
    api = _existing_until("111")
    _run_detect(monkeypatch, tmp_path, "Claude AI usage limit reached|1800003600", api)
    assert _until_writes(api) == ["1800003600"]


def test_detect_replaces_invalid_until(monkeypatch, tmp_path):
    api = _existing_until("not-an-int")
    _run_detect(monkeypatch, tmp_path, "Claude AI usage limit reached|1800003600", api)
    assert _until_writes(api) == ["1800003600"]


def test_detect_until_patch_falls_back_to_post_on_404(monkeypatch, tmp_path):
    api = FakeApi(
        {
            f"GET {_UNTIL_PATH}": 404,
            f"PATCH {_UNTIL_PATH}": 404,
        }
    )
    _run_detect(monkeypatch, tmp_path, "Claude AI usage limit reached|1800003600", api)
    assert (
        "POST",
        _CREATE_PATH,
        {
            "name": "CLAUDE_FORCE_API_UNTIL",
            "value": "1800003600",
            "visibility": "all",
        },
    ) in api.calls


# --- restore-if-due ---


def _run_restore(monkeypatch, api: FakeApi) -> None:
    monkeypatch.setattr(sca.urllib.request, "urlopen", api)
    sca.restore_if_due("corp-token")


def test_restore_missing_until_is_noop(monkeypatch):
    api = FakeApi({f"GET {_UNTIL_PATH}": 404})
    _run_restore(monkeypatch, api)
    assert all(m == "GET" for m, _, _ in api.calls)


def test_restore_not_due_makes_no_writes(monkeypatch):
    future = str(int(_NOW) + 3600)
    api = FakeApi({f"GET {_UNTIL_PATH}": FakeResponse(200, {"value": future})})
    monkeypatch.setattr(sca.time, "time", lambda: _NOW)
    _run_restore(monkeypatch, api)
    assert all(m == "GET" for m, _, _ in api.calls)


def test_restore_due_deletes_both_vars(monkeypatch):
    past = str(int(_NOW) - 3600)
    api = FakeApi({f"GET {_UNTIL_PATH}": FakeResponse(200, {"value": past})})
    monkeypatch.setattr(sca.time, "time", lambda: _NOW)
    _run_restore(monkeypatch, api)
    assert ("DELETE", _FORCE_PATH, None) in api.calls
    assert ("DELETE", _UNTIL_PATH, None) in api.calls


def test_restore_non_integer_until_deletes_both(monkeypatch):
    api = FakeApi({f"GET {_UNTIL_PATH}": FakeResponse(200, {"value": "not-an-int"})})
    _run_restore(monkeypatch, api)
    assert ("DELETE", _FORCE_PATH, None) in api.calls
    assert ("DELETE", _UNTIL_PATH, None) in api.calls


def test_restore_skips_when_until_renewed_before_delete(monkeypatch):
    # A detection renewed the window between the first read and the delete.
    past = str(int(_NOW) - 3600)
    renewed = str(int(_NOW) + 3600)
    api = FakeApi(
        {
            f"GET {_UNTIL_PATH}": [
                FakeResponse(200, {"value": past}),
                FakeResponse(200, {"value": renewed}),
            ]
        }
    )
    monkeypatch.setattr(sca.time, "time", lambda: _NOW)
    _run_restore(monkeypatch, api)
    assert all(m == "GET" for m, _, _ in api.calls)


def test_restore_keeps_until_renewed_after_force_delete(monkeypatch):
    past = str(int(_NOW) - 3600)
    renewed = str(int(_NOW) + 3600)
    api = FakeApi(
        {
            f"GET {_UNTIL_PATH}": [
                FakeResponse(200, {"value": past}),
                FakeResponse(200, {"value": past}),
                FakeResponse(200, {"value": renewed}),
            ]
        }
    )
    monkeypatch.setattr(sca.time, "time", lambda: _NOW)
    _run_restore(monkeypatch, api)
    assert ("DELETE", _FORCE_PATH, None) in api.calls
    assert ("DELETE", _UNTIL_PATH, None) not in api.calls
    # The renewed window gets its flag back after the delete.
    deleted_at = api.calls.index(("DELETE", _FORCE_PATH, None))
    assert (
        "PATCH",
        _FORCE_PATH,
        {"name": "CLAUDE_FORCE_API", "value": "true"},
    ) in api.calls[deleted_at:]


def test_restore_deletes_force_reset_while_until_was_deleted(monkeypatch):
    # A detection that wrote UNTIL just before our delete and FORCE just
    # after it would leave FORCE without UNTIL: a manual pin nothing clears.
    past = str(int(_NOW) - 3600)
    api = FakeApi(
        {
            f"GET {_UNTIL_PATH}": FakeResponse(200, {"value": past}),
            f"GET {_FORCE_PATH}": FakeResponse(200, {"value": "true"}),
        }
    )
    monkeypatch.setattr(sca.time, "time", lambda: _NOW)
    _run_restore(monkeypatch, api)
    assert api.calls.count(("DELETE", _FORCE_PATH, None)) == 2
    until_deleted_at = api.calls.index(("DELETE", _UNTIL_PATH, None))
    assert ("DELETE", _FORCE_PATH, None) in api.calls[until_deleted_at:]


def test_restore_keeps_force_when_until_renewed_after_its_delete(monkeypatch):
    # The detection wrote a new UNTIL after our UNTIL delete, then FORCE:
    # that is a live window, not a stray flag.
    past = str(int(_NOW) - 3600)
    renewed = str(int(_NOW) + 3600)
    api = FakeApi(
        {
            f"GET {_UNTIL_PATH}": [
                FakeResponse(200, {"value": past}),
                FakeResponse(200, {"value": past}),
                FakeResponse(200, {"value": past}),
                FakeResponse(200, {"value": renewed}),
            ],
            f"GET {_FORCE_PATH}": FakeResponse(200, {"value": "true"}),
        }
    )
    monkeypatch.setattr(sca.time, "time", lambda: _NOW)
    _run_restore(monkeypatch, api)
    assert api.calls.count(("DELETE", _FORCE_PATH, None)) == 1


def test_restore_warns_when_force_reset_fails(monkeypatch, capsys):
    past = str(int(_NOW) - 3600)
    renewed = str(int(_NOW) + 3600)
    api = FakeApi(
        {
            f"GET {_UNTIL_PATH}": [
                FakeResponse(200, {"value": past}),
                FakeResponse(200, {"value": past}),
                FakeResponse(200, {"value": renewed}),
            ],
            f"PATCH {_FORCE_PATH}": 500,
        }
    )
    monkeypatch.setattr(sca.time, "time", lambda: _NOW)
    _run_restore(monkeypatch, api)
    result = capsys.readouterr()
    assert "::warning::" in result.out
    assert "re-set failed" in result.err


def test_restore_reports_until_removed_meanwhile(monkeypatch, capsys):
    past = str(int(_NOW) - 3600)
    api = FakeApi(
        {
            f"GET {_UNTIL_PATH}": [
                FakeResponse(200, {"value": past}),
                FakeResponse(200, {"value": past}),
                404,
            ]
        }
    )
    monkeypatch.setattr(sca.time, "time", lambda: _NOW)
    _run_restore(monkeypatch, api)
    err = capsys.readouterr().err
    assert "kept" not in err
    assert "already removed" in err


def test_detect_does_not_set_force_when_until_write_fails(
    monkeypatch, tmp_path, capsys
):
    # FORCE without UNTIL is a manual pin; a failed UNTIL write must not
    # become one.
    api = FakeApi({f"GET {_UNTIL_PATH}": 404, f"PATCH {_UNTIL_PATH}": 500})
    _run_detect(monkeypatch, tmp_path, "Claude AI usage limit reached|5", api)
    assert not any(p == _FORCE_PATH for _, p, _ in api.calls)
    assert "::warning::" in capsys.readouterr().out


@pytest.mark.parametrize(
    "value",
    [
        "+1800003600",
        "1_800_003_600",
        "18000 03600",
        "-5",
        "1" * 19,
        "1800003600\u00a0",  # Unicode whitespace: bash does not trim it
    ],
)
def test_epoch_accepts_only_plain_digits(value):
    # One definition with the review step's bash check (AT-3439).
    assert sca._epoch(value) is None


def test_epoch_strips_surrounding_whitespace():
    assert sca._epoch(" 1800003600\n") == 1800003600


# --- Fail-safe ---


def test_api_error_does_not_raise(monkeypatch, tmp_path):
    api = FakeApi(
        {
            f"GET {_UNTIL_PATH}": 404,
            f"PATCH {_UNTIL_PATH}": urllib.error.URLError("connection refused"),
        }
    )
    _run_detect(monkeypatch, tmp_path, "Claude AI usage limit reached|5", api)


def test_api_403_does_not_raise(monkeypatch, tmp_path):
    api = FakeApi(
        {
            f"GET {_UNTIL_PATH}": 404,
            f"PATCH {_FORCE_PATH}": 403,
        }
    )
    _run_detect(monkeypatch, tmp_path, "Claude AI usage limit reached|5", api)


def test_main_exits_zero_on_unexpected_error(monkeypatch, tmp_path):
    exec_file = tmp_path / "exec.json"
    exec_file.write_text("Claude AI usage limit reached|5")
    monkeypatch.setenv("EXEC_FILE", str(exec_file))
    monkeypatch.setenv("GH_TOKEN", "corp-token")
    monkeypatch.setattr(sys, "argv", ["switch_claude_auth.py", "detect-and-switch"])

    def boom(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("unexpected")

    monkeypatch.setattr(sca, "detect_and_switch", boom)
    sca.main()  # must not raise / exit non-zero


def test_missing_token_is_noop(monkeypatch):
    api = FakeApi()
    monkeypatch.setattr(sca.urllib.request, "urlopen", api)
    monkeypatch.delenv("GH_TOKEN", raising=False)
    monkeypatch.setattr(sys, "argv", ["switch_claude_auth.py", "restore-if-due"])
    sca.main()
    assert api.calls == []


# --- Public-log policy ---


def test_output_never_contains_org_name(monkeypatch, tmp_path, capsys):
    summary = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    api = FakeApi(
        {
            f"GET {_UNTIL_PATH}": 404,
            f"PATCH {_UNTIL_PATH}": 404,
            f"PATCH {_FORCE_PATH}": 403,
        }
    )
    _run_detect(monkeypatch, tmp_path, "Claude AI usage limit reached|5", api)
    result = capsys.readouterr()
    captured = result.out + result.err
    if summary.exists():
        captured += summary.read_text()
    assert "ignite-corp" not in captured


def test_dry_run_makes_no_write_calls(monkeypatch, tmp_path):
    monkeypatch.setenv("DRY_RUN", "true")
    api = FakeApi({f"GET {_UNTIL_PATH}": 404})
    _run_detect(monkeypatch, tmp_path, "Claude AI usage limit reached|5", api)
    assert all(m == "GET" for m, _, _ in api.calls)


# --- SDK execution-file detection (AT-3408) ---
#
# The execution file claude-code-action writes is a JSON array of Agent SDK
# messages. These fixtures are built from the SDK 0.3.316 type declarations
# (SDKAssistantMessage, SDKRateLimitEvent, SDKResultSuccess), not from a
# captured real failure: none has been captured yet, and this repo is public.

_LIMIT_TEXT = "You've hit your limit \u00b7 resets 3am (UTC)"
_DEAD_PIN_TEXT = (
    "There's an issue with the selected model (claude-x). It may not exist "
    "or you may not have access to it."
)


def _init_msg() -> dict[str, Any]:
    return {
        "type": "system",
        "subtype": "init",
        "model": "claude-x",
        "uuid": "u-init",
        "session_id": "s",
    }


def _assistant_msg(text: str, error: str | None = None) -> dict[str, Any]:
    msg: dict[str, Any] = {
        "type": "assistant",
        "message": {
            "id": "msg_1",
            "type": "message",
            "role": "assistant",
            "model": "<synthetic>",
            "content": [{"type": "text", "text": text}],
            "stop_reason": "stop_sequence",
        },
        "parent_tool_use_id": None,
        "uuid": "u-asst",
        "session_id": "s",
    }
    if error is not None:
        msg["error"] = error
    return msg


def _rate_limit_event(status: str, **info: Any) -> dict[str, Any]:
    return {
        "type": "rate_limit_event",
        "rate_limit_info": {"status": status, **info},
        "uuid": "u-rl",
        "session_id": "s",
    }


def _result_msg(
    text: str, *, is_error: bool = True, api_error_status: int | None = None
) -> dict[str, Any]:
    return {
        "type": "result",
        "subtype": "success",
        "is_error": is_error,
        "api_error_status": api_error_status,
        "num_turns": 1,
        "result": text,
        "stop_reason": "stop_sequence",
        "total_cost_usd": 0,
        "uuid": "u-result",
        "session_id": "s",
    }


def _exec(*messages: dict[str, Any]) -> str:
    return json.dumps(list(messages))


def test_oauth_limit_with_resets_at_uses_sdk_reset_epoch():
    text = _exec(
        _init_msg(),
        _rate_limit_event(
            "rejected", resetsAt=1_800_003_600, rateLimitType="five_hour"
        ),
        _assistant_msg(_LIMIT_TEXT, error="rate_limit"),
        _result_msg(_LIMIT_TEXT, api_error_status=429),
    )
    assert sca._detect_limit(text, _NOW).epoch == 1_800_003_600


def test_oauth_limit_without_resets_at_falls_back_to_now_plus_5h():
    text = _exec(
        _init_msg(),
        _assistant_msg(_LIMIT_TEXT, error="rate_limit"),
        _result_msg(_LIMIT_TEXT, api_error_status=429),
    )
    assert sca._detect_limit(text, _NOW).epoch == int(_NOW) + 5 * 3600


def test_oauth_limit_uses_overage_resets_at_when_resets_at_absent():
    text = _exec(
        _rate_limit_event("rejected", overageResetsAt=1_800_007_200),
        _result_msg("You're out of extra usage \u00b7 resets 5am"),
    )
    assert sca._detect_limit(text, _NOW).epoch == 1_800_007_200


def test_reset_epoch_in_milliseconds_is_normalized_to_seconds():
    text = _exec(
        _rate_limit_event("rejected", resetsAt=1_800_003_600_000),
        _assistant_msg(_LIMIT_TEXT, error="rate_limit"),
    )
    assert sca._detect_limit(text, _NOW).epoch == 1_800_003_600


def test_reset_ignores_events_that_were_not_rejected():
    # The structured signal came from the assistant error; a warning event
    # may describe another window, so the reset falls back to now + 5h.
    text = _exec(
        _rate_limit_event("allowed_warning", resetsAt=1_800_600_000),
        _assistant_msg(_LIMIT_TEXT, error="rate_limit"),
    )
    assert sca._detect_limit(text, _NOW).epoch == int(_NOW) + 5 * 3600


def _limit_with_reset(value: Any) -> str:
    return _exec(
        _rate_limit_event("rejected", resetsAt=value),
        _assistant_msg(_LIMIT_TEXT, error="rate_limit"),
    )


def test_reset_more_than_7_days_ahead_falls_back_to_now_plus_5h():
    # Keep-first would hold an implausible reset until cleared by hand.
    text = _limit_with_reset(int(_NOW) + 8 * 24 * 3600)
    assert sca._detect_limit(text, _NOW).epoch == int(_NOW) + 5 * 3600


def test_reset_in_the_past_falls_back_to_now_plus_5h():
    text = _limit_with_reset(int(_NOW) - 60)
    assert sca._detect_limit(text, _NOW).epoch == int(_NOW) + 5 * 3600


def test_reset_exactly_7_days_ahead_is_accepted():
    text = _limit_with_reset(int(_NOW) + 7 * 24 * 3600)
    assert sca._detect_limit(text, _NOW).epoch == int(_NOW) + 7 * 24 * 3600


def test_non_finite_reset_falls_back_without_raising():
    for value in (float("inf"), float("nan")):
        text = _limit_with_reset(value)
        assert sca._detect_limit(text, _NOW).epoch == int(_NOW) + 5 * 3600


def test_legacy_epoch_out_of_range_falls_back_to_now_plus_5h():
    for epoch in (int(_NOW) + 8 * 24 * 3600, int(_NOW) - 60):
        text = _exec(_result_msg(f"Claude AI usage limit reached|{epoch}"))
        assert sca._detect_limit(text, _NOW).epoch == int(_NOW) + 5 * 3600


def test_non_string_error_value_does_not_raise():
    msg = _assistant_msg(_LIMIT_TEXT)
    msg["error"] = {"type": "rate_limit"}
    assert sca._detect_limit(_exec(msg), _NOW).epoch is None


def test_billing_error_with_credits_prefix_switches():
    text = _exec(
        _assistant_msg("You're out of usage credits", error="billing_error"),
        _result_msg("You're out of usage credits"),
    )
    assert sca._detect_limit(text, _NOW).epoch == int(_NOW) + 5 * 3600


def test_dead_model_pin_does_not_switch():
    text = _exec(
        _init_msg(),
        _assistant_msg(_DEAD_PIN_TEXT, error="model_not_found"),
        _result_msg(_DEAD_PIN_TEXT, api_error_status=404),
    )
    assert sca._detect_limit(text, _NOW).epoch is None


def test_invalid_request_does_not_switch():
    text = _exec(
        _assistant_msg("API Error: 400 invalid_request_error", error="invalid_request"),
        _result_msg("API Error: 400 invalid_request_error", api_error_status=400),
    )
    assert sca._detect_limit(text, _NOW).epoch is None


def test_transient_429_rate_limit_without_prefix_does_not_switch():
    text = _exec(
        {
            "type": "system",
            "subtype": "api_retry",
            "attempt": 1,
            "max_retries": 10,
            "retry_delay_ms": 1000,
            "error_status": 429,
            "error": "rate_limit",
            "uuid": "u-retry",
            "session_id": "s",
        },
        _rate_limit_event("allowed_warning", resetsAt=1_800_003_600),
        _assistant_msg("API Error: 429 rate_limit_error", error="rate_limit"),
        _result_msg("API Error: 429 rate_limit_error", api_error_status=429),
    )
    assert sca._detect_limit(text, _NOW).epoch is None


def test_overloaded_does_not_switch():
    text = _exec(
        _assistant_msg("API Error: 529 Overloaded", error="overloaded"),
        _result_msg("API Error: 529 Overloaded", api_error_status=529),
    )
    assert sca._detect_limit(text, _NOW).epoch is None


def test_prefix_without_structured_signal_does_not_switch():
    # A review whose prose happens to open with an SDK prefix, on a run with
    # no limit error and no rejected rate-limit event.
    prose = "You've reached your conclusion too early here."
    text = _exec(_assistant_msg(prose), _result_msg(prose, is_error=False))
    assert sca._detect_limit(text, _NOW).epoch is None


def test_rejected_event_without_prefix_does_not_switch():
    text = _exec(
        _rate_limit_event("rejected", resetsAt=1_800_003_600),
        _result_msg("API Error: 429 rate_limit_error", api_error_status=429),
    )
    assert sca._detect_limit(text, _NOW).epoch is None


def test_legacy_pattern_in_result_text_still_switches():
    text = _exec(_result_msg("Claude AI usage limit reached|1800003600"))
    assert sca._detect_limit(text, _NOW).epoch == 1800003600


def test_legacy_phrase_in_reviewed_content_does_not_switch():
    # Reviewing this very script puts "usage limit reached" into tool results
    # and review prose; only error text may carry the legacy signature.
    tool_result = {
        "type": "user",
        "message": {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": "t1",
                    "content": "_LIMIT_FALLBACK_RE = 'usage limit reached'",
                }
            ],
        },
        "uuid": "u-user",
        "session_id": "s",
    }
    text = _exec(
        tool_result,
        _assistant_msg("The 'Claude AI usage limit reached|123' pattern is dead."),
        _result_msg("review done", is_error=False),
    )
    assert sca._detect_limit(text, _NOW).epoch is None


def test_detect_empty_exec_file_makes_no_api_calls_and_logs(
    monkeypatch, tmp_path, capsys
):
    api = FakeApi()
    _run_detect(monkeypatch, tmp_path, "", api)
    assert api.calls == []
    assert "no usage-limit signature" in capsys.readouterr().err


def test_detect_missing_exec_file_logs(monkeypatch, capsys):
    api = FakeApi()
    monkeypatch.setenv("EXEC_FILE", "/nonexistent/exec.json")
    monkeypatch.setattr(sca.urllib.request, "urlopen", api)
    sca.detect_and_switch("corp-token")
    assert api.calls == []
    assert "no execution file" in capsys.readouterr().err


def test_detect_unset_exec_file_logs(monkeypatch, capsys):
    api = FakeApi()
    monkeypatch.delenv("EXEC_FILE", raising=False)
    monkeypatch.setattr(sca.urllib.request, "urlopen", api)
    sca.detect_and_switch("corp-token")
    assert api.calls == []
    assert "no execution file" in capsys.readouterr().err


def test_detect_no_signature_logs_messages_scanned(monkeypatch, tmp_path, capsys):
    api = FakeApi()
    text = _exec(_init_msg(), _result_msg("review done", is_error=False))
    _run_detect(monkeypatch, tmp_path, text, api)
    assert api.calls == []
    assert "no usage-limit signature (2 messages scanned" in capsys.readouterr().err


def test_detect_sdk_limit_sets_until_from_resets_at(monkeypatch, tmp_path):
    monkeypatch.setattr(sca.time, "time", lambda: _NOW)
    api = FakeApi({f"GET {_UNTIL_PATH}": 404})
    text = _exec(
        _rate_limit_event("rejected", resetsAt=1_800_003_600),
        _assistant_msg(_LIMIT_TEXT, error="rate_limit"),
        _result_msg(_LIMIT_TEXT, api_error_status=429),
    )
    _run_detect(monkeypatch, tmp_path, text, api)
    assert (
        "PATCH",
        _UNTIL_PATH,
        {"name": "CLAUDE_FORCE_API_UNTIL", "value": "1800003600"},
    ) in api.calls
    assert (
        "PATCH",
        _FORCE_PATH,
        {"name": "CLAUDE_FORCE_API", "value": "true"},
    ) in api.calls


def test_detect_log_names_prefix_but_not_the_rest_of_the_text(
    monkeypatch, tmp_path, capsys
):
    summary = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    api = FakeApi({f"GET {_UNTIL_PATH}": 404})
    tail = "PRIVATE-TAIL-0123"
    text = _exec(
        _assistant_msg("You've hit your limit " + tail, error="rate_limit"),
        _result_msg("You've hit your limit " + tail, api_error_status=429),
    )
    _run_detect(monkeypatch, tmp_path, text, api)
    result = capsys.readouterr()
    captured = result.out + result.err + summary.read_text()
    assert "You've hit your" in captured
    assert tail not in captured


def test_detect_warns_on_existing_until_more_than_7_days_ahead(
    monkeypatch, tmp_path, capsys
):
    far = str(int(_NOW) + 8 * 24 * 3600)
    api = FakeApi({f"GET {_UNTIL_PATH}": FakeResponse(200, {"value": far})})
    monkeypatch.setattr(sca.time, "time", lambda: _NOW)
    _run_detect(monkeypatch, tmp_path, "Claude AI usage limit reached|5", api)
    result = capsys.readouterr()
    captured = result.out + result.err
    assert "::warning::" in captured
    assert f"CLAUDE_FORCE_API_UNTIL={far}" in captured
    # min(existing, new): the far epoch is replaced by the detected reset.
    assert _until_writes(api) == [str(int(_NOW) + 5 * 3600)]


def test_far_until_warning_prints_the_parsed_integer_not_the_raw_value(
    monkeypatch, tmp_path, capsys
):
    # int() accepts surrounding whitespace, newlines included; the raw value
    # must not carry a line break into the workflow-command stream.
    far = int(_NOW) + 8 * 24 * 3600
    raw = f" {far}\n"
    api = FakeApi({f"GET {_UNTIL_PATH}": FakeResponse(200, {"value": raw})})
    _run_detect(monkeypatch, tmp_path, "Claude AI usage limit reached|5", api)
    out = capsys.readouterr().out
    assert f"::warning::CLAUDE_FORCE_API_UNTIL={far} is" in out


def test_detect_no_warning_for_existing_until_within_7_days(
    monkeypatch, tmp_path, capsys
):
    near = str(int(_NOW) + 6 * 24 * 3600)
    api = FakeApi({f"GET {_UNTIL_PATH}": FakeResponse(200, {"value": near})})
    monkeypatch.setattr(sca.time, "time", lambda: _NOW)
    _run_detect(monkeypatch, tmp_path, "Claude AI usage limit reached|5", api)
    result = capsys.readouterr()
    assert "::warning::" not in result.out + result.err


# --- Prefix parity with the pinned SDK (AT-3408) ---

_FIXTURES = Path(__file__).parent / "fixtures"
_PREFIX_FIXTURE = _FIXTURES / "claude-agent-sdk-0.3.316-usage-limit-prefixes.json"
_ACTION_YML = (
    Path(__file__).resolve().parents[2] / "actions" / "claude-review" / "action.yml"
)


def test_prefixes_match_pinned_sdk_declaration():
    doc = json.loads(_PREFIX_FIXTURE.read_text(encoding="ascii"))
    assert doc["sdk_version"] == sca._PREFIXES_SDK_VERSION
    assert tuple(doc["prefixes"]) == sca.USAGE_LIMIT_ERROR_PREFIXES


def test_prefix_list_is_pinned_to_the_action_the_composite_runs():
    # A claude-code-action bump can bring a different SDK, and with it a
    # different prefix list. Re-read that SDK's sdk.d.ts, update the list,
    # the fixture and both constants, then this passes again.
    pins = re.findall(
        r"uses: anthropics/claude-code-action@([0-9a-f]{40})",
        _ACTION_YML.read_text(encoding="utf-8"),
    )
    assert pins, "no claude-code-action pin found in the composite"
    assert set(pins) == {sca._PREFIXES_ACTION_SHA}
    doc = json.loads(_PREFIX_FIXTURE.read_text(encoding="ascii"))
    assert doc["action_sha"] == sca._PREFIXES_ACTION_SHA
