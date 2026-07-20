"""Tests for switch_claude_auth: signature parsing, org var switch, fail-safe."""

from __future__ import annotations

import io
import json
import sys
import urllib.error
from pathlib import Path
from typing import Any

# Add parent dir to path for import
SCRIPT_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(SCRIPT_DIR))

import switch_claude_auth as sca  # noqa: E402

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
    (int), or an exception instance. Unlisted calls return 200 {}.
    """

    def __init__(self, responses: dict[str, Any] | None = None) -> None:
        self.responses = responses or {}
        self.calls: list[tuple[str, str, dict | None]] = []

    def __call__(self, req: Any, timeout: int = 0) -> FakeResponse:
        path = req.full_url.replace(sca._API_BASE, "")
        body = json.loads(req.data) if req.data else None
        self.calls.append((req.get_method(), path, body))
        result = self.responses.get(f"{req.get_method()} {path}")
        if result is None:
            return FakeResponse(200, {})
        if isinstance(result, int):
            raise urllib.error.HTTPError(req.full_url, result, "err", {}, io.BytesIO())  # type: ignore[arg-type]
        if isinstance(result, Exception):
            raise result
        return result


# --- Signature parsing ---


def test_signature_with_epoch_returns_epoch():
    text = "blah Claude AI usage limit reached|1799999000 blah"
    assert sca._parse_limit_epoch(text, _NOW) == 1799999000


def test_signature_multiple_matches_uses_last():
    text = (
        "Claude AI usage limit reached|100\n"
        "Claude AI usage limit reached|200\n"
    )
    assert sca._parse_limit_epoch(text, _NOW) == 200


def test_signature_without_epoch_falls_back_to_now_plus_5h():
    text = "Error: Usage Limit Reached, try later"
    assert sca._parse_limit_epoch(text, _NOW) == int(_NOW) + 5 * 3600


def test_no_signature_returns_none():
    text = json.dumps({"content": [{"text": "review completed fine"}]})
    assert sca._parse_limit_epoch(text, _NOW) is None


# --- detect-and-switch ---


def _run_detect(monkeypatch, tmp_path, exec_text: str, api: FakeApi) -> None:
    exec_file = tmp_path / "exec.json"
    exec_file.write_text(exec_text)
    monkeypatch.setenv("EXEC_FILE", str(exec_file))
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
        }
    )
    _run_detect(
        monkeypatch, tmp_path, "Claude AI usage limit reached|1799999000", api
    )
    # UNTIL missing -> created via POST with the reset epoch.
    assert (
        "POST",
        _CREATE_PATH,
        {
            "name": "CLAUDE_FORCE_API_UNTIL",
            "value": "1799999000",
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


def test_detect_keeps_existing_until_epoch(monkeypatch, tmp_path):
    api = FakeApi(
        {
            f"GET {_UNTIL_PATH}": FakeResponse(
                200, {"name": "CLAUDE_FORCE_API_UNTIL", "value": "111"}
            )
        }
    )
    _run_detect(
        monkeypatch, tmp_path, "Claude AI usage limit reached|1799999000", api
    )
    until_writes = [
        (m, p) for m, p, _ in api.calls if m in ("PATCH", "POST") and "UNTIL" in p
    ]
    assert until_writes == []
    # FORCE_API is still written (only the epoch is kept-first).
    assert any(m == "PATCH" and p == _FORCE_PATH for m, p, _ in api.calls)


def test_detect_until_patch_falls_back_to_post_on_404(monkeypatch, tmp_path):
    api = FakeApi(
        {
            f"GET {_UNTIL_PATH}": 404,
            f"PATCH {_UNTIL_PATH}": 404,
        }
    )
    _run_detect(monkeypatch, tmp_path, "Claude AI usage limit reached|5", api)
    assert (
        "POST",
        _CREATE_PATH,
        {
            "name": "CLAUDE_FORCE_API_UNTIL",
            "value": "5",
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
    api = FakeApi(
        {f"GET {_UNTIL_PATH}": FakeResponse(200, {"value": "not-an-int"})}
    )
    _run_restore(monkeypatch, api)
    assert ("DELETE", _FORCE_PATH, None) in api.calls
    assert ("DELETE", _UNTIL_PATH, None) in api.calls


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
