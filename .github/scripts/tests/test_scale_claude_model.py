"""Tests for scale_claude_model: signature parsing, var updates, fail-safe."""

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

import scale_claude_model as scm

_NOW = 1_800_000_000.0
_PILOT_REPOS = json.dumps(
    ["ignite-pilot-org/repo-alpha", "ignite-pilot-org/repo-beta"]
)


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
        path = req.full_url.replace(scm._API_BASE, "")
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
    assert scm._parse_limit_epoch(text, _NOW) == 1799999000


def test_signature_multiple_matches_uses_last():
    text = (
        "Claude AI usage limit reached|100\n"
        "Claude AI usage limit reached|200\n"
    )
    assert scm._parse_limit_epoch(text, _NOW) == 200


def test_signature_without_epoch_falls_back_to_now_plus_5h():
    text = "Error: Usage Limit Reached, try later"
    assert scm._parse_limit_epoch(text, _NOW) == int(_NOW) + 5 * 3600


def test_no_signature_returns_none():
    text = json.dumps({"content": [{"text": "review completed fine"}]})
    assert scm._parse_limit_epoch(text, _NOW) is None


# --- detect-and-downgrade ---


def _run_detect(monkeypatch, tmp_path, exec_text: str, api: FakeApi) -> None:
    exec_file = tmp_path / "exec.json"
    exec_file.write_text(exec_text)
    monkeypatch.setenv("EXEC_FILE", str(exec_file))
    monkeypatch.setenv("MODEL_SCALE_REPOS", _PILOT_REPOS)
    monkeypatch.setattr(scm.urllib.request, "urlopen", api)
    scm.detect_and_downgrade("corp-token", "pilot-token")


def test_detect_no_signature_makes_no_api_calls(monkeypatch, tmp_path):
    api = FakeApi()
    _run_detect(monkeypatch, tmp_path, "all good", api)
    assert api.calls == []


def test_detect_missing_exec_file_is_noop(monkeypatch):
    api = FakeApi()
    monkeypatch.setenv("EXEC_FILE", "/nonexistent/exec.json")
    monkeypatch.setattr(scm.urllib.request, "urlopen", api)
    scm.detect_and_downgrade("corp-token", "pilot-token")
    assert api.calls == []


def test_detect_patches_org_and_all_pilot_repos_to_sonnet(monkeypatch, tmp_path):
    api = FakeApi(
        {
            "GET /orgs/ignite-corp/actions/variables/CLAUDE_MODEL_RESTORE_AT": 404,
            "PATCH /orgs/ignite-corp/actions/variables/CLAUDE_MODEL_RESTORE_AT": 404,
        }
    )
    _run_detect(
        monkeypatch, tmp_path, "Claude AI usage limit reached|1799999000", api
    )
    patches = [(m, p, b) for m, p, b in api.calls if m == "PATCH"]
    assert (
        "PATCH",
        "/orgs/ignite-corp/actions/variables/CLAUDE_MODEL",
        {"name": "CLAUDE_MODEL", "value": "claude-sonnet-4-6"},
    ) in patches
    for repo in json.loads(_PILOT_REPOS):
        assert (
            "PATCH",
            f"/repos/{repo}/actions/variables/CLAUDE_MODEL",
            {"name": "CLAUDE_MODEL", "value": "claude-sonnet-4-6"},
        ) in patches
    # RESTORE_AT missing -> created via POST
    assert (
        "POST",
        "/orgs/ignite-corp/actions/variables",
        {
            "name": "CLAUDE_MODEL_RESTORE_AT",
            "value": "1799999000",
            "visibility": "all",
        },
    ) in api.calls


def test_detect_keeps_existing_restore_epoch(monkeypatch, tmp_path):
    api = FakeApi(
        {
            "GET /orgs/ignite-corp/actions/variables/CLAUDE_MODEL_RESTORE_AT": FakeResponse(
                200, {"name": "CLAUDE_MODEL_RESTORE_AT", "value": "111"}
            )
        }
    )
    _run_detect(
        monkeypatch, tmp_path, "Claude AI usage limit reached|1799999000", api
    )
    writes = [
        (m, p) for m, p, _ in api.calls if m in ("PATCH", "POST") and "RESTORE" in p
    ]
    assert writes == []


def test_repo_var_patch_falls_back_to_post_on_404(monkeypatch, tmp_path):
    repo = "ignite-pilot-org/repo-alpha"
    api = FakeApi(
        {
            f"PATCH /repos/{repo}/actions/variables/CLAUDE_MODEL": 404,
            "GET /orgs/ignite-corp/actions/variables/CLAUDE_MODEL_RESTORE_AT": 404,
        }
    )
    _run_detect(monkeypatch, tmp_path, "Claude AI usage limit reached|5", api)
    assert (
        "POST",
        f"/repos/{repo}/actions/variables",
        {"name": "CLAUDE_MODEL", "value": "claude-sonnet-4-6"},
    ) in api.calls


def test_detect_repo_list_from_org_var_when_env_unset(monkeypatch, tmp_path):
    exec_file = tmp_path / "exec.json"
    exec_file.write_text("Claude AI usage limit reached|5")
    monkeypatch.setenv("EXEC_FILE", str(exec_file))
    monkeypatch.delenv("MODEL_SCALE_REPOS", raising=False)
    api = FakeApi(
        {
            "GET /orgs/ignite-corp/actions/variables/MODEL_SCALE_REPOS": FakeResponse(
                200, {"name": "MODEL_SCALE_REPOS", "value": _PILOT_REPOS}
            ),
            "GET /orgs/ignite-corp/actions/variables/CLAUDE_MODEL_RESTORE_AT": 404,
        }
    )
    monkeypatch.setattr(scm.urllib.request, "urlopen", api)
    scm.detect_and_downgrade("corp-token", "pilot-token")
    assert any(
        p == "/repos/ignite-pilot-org/repo-alpha/actions/variables/CLAUDE_MODEL"
        for _, p, _ in api.calls
    )


# --- restore-if-due ---


def _run_restore(monkeypatch, api: FakeApi) -> None:
    monkeypatch.setenv("MODEL_SCALE_REPOS", _PILOT_REPOS)
    monkeypatch.setattr(scm.urllib.request, "urlopen", api)
    scm.restore_if_due("corp-token", "pilot-token")


def test_restore_missing_var_is_noop(monkeypatch):
    api = FakeApi(
        {"GET /orgs/ignite-corp/actions/variables/CLAUDE_MODEL_RESTORE_AT": 404}
    )
    _run_restore(monkeypatch, api)
    assert all(m == "GET" for m, _, _ in api.calls)


def test_restore_not_due_makes_no_writes(monkeypatch):
    future = str(int(_NOW) + 3600)
    api = FakeApi(
        {
            "GET /orgs/ignite-corp/actions/variables/CLAUDE_MODEL_RESTORE_AT": FakeResponse(
                200, {"value": future}
            )
        }
    )
    monkeypatch.setattr(scm.time, "time", lambda: _NOW)
    _run_restore(monkeypatch, api)
    assert all(m == "GET" for m, _, _ in api.calls)


def test_restore_due_patches_default_and_deletes_marker(monkeypatch):
    past = str(int(_NOW) - 3600)
    api = FakeApi(
        {
            "GET /orgs/ignite-corp/actions/variables/CLAUDE_MODEL_RESTORE_AT": FakeResponse(
                200, {"value": past}
            ),
            "GET /orgs/ignite-corp/actions/variables/CLAUDE_MODEL_DEFAULT": FakeResponse(
                200, {"value": "claude-opus-4-8"}
            ),
        }
    )
    monkeypatch.setattr(scm.time, "time", lambda: _NOW)
    _run_restore(monkeypatch, api)
    assert (
        "PATCH",
        "/orgs/ignite-corp/actions/variables/CLAUDE_MODEL",
        {"name": "CLAUDE_MODEL", "value": "claude-opus-4-8"},
    ) in api.calls
    for repo in json.loads(_PILOT_REPOS):
        assert (
            "PATCH",
            f"/repos/{repo}/actions/variables/CLAUDE_MODEL",
            {"name": "CLAUDE_MODEL", "value": "claude-opus-4-8"},
        ) in api.calls
    assert (
        "DELETE",
        "/orgs/ignite-corp/actions/variables/CLAUDE_MODEL_RESTORE_AT",
        None,
    ) in api.calls


def test_restore_uses_fallback_default_when_var_missing(monkeypatch):
    past = str(int(_NOW) - 3600)
    api = FakeApi(
        {
            "GET /orgs/ignite-corp/actions/variables/CLAUDE_MODEL_RESTORE_AT": FakeResponse(
                200, {"value": past}
            ),
            "GET /orgs/ignite-corp/actions/variables/CLAUDE_MODEL_DEFAULT": 404,
        }
    )
    monkeypatch.setattr(scm.time, "time", lambda: _NOW)
    _run_restore(monkeypatch, api)
    assert (
        "PATCH",
        "/orgs/ignite-corp/actions/variables/CLAUDE_MODEL",
        {"name": "CLAUDE_MODEL", "value": "claude-opus-4-8"},
    ) in api.calls


# --- Fail-safe ---


def test_api_error_does_not_raise(monkeypatch, tmp_path):
    api = FakeApi(
        {
            "PATCH /orgs/ignite-corp/actions/variables/CLAUDE_MODEL": urllib.error.URLError(
                "connection refused"
            )
        }
    )
    _run_detect(monkeypatch, tmp_path, "Claude AI usage limit reached|5", api)


def test_api_403_does_not_raise(monkeypatch, tmp_path):
    api = FakeApi(
        {
            "PATCH /orgs/ignite-corp/actions/variables/CLAUDE_MODEL": 403,
            "GET /orgs/ignite-corp/actions/variables/CLAUDE_MODEL_RESTORE_AT": 404,
        }
    )
    _run_detect(monkeypatch, tmp_path, "Claude AI usage limit reached|5", api)


def test_main_exits_zero_on_unexpected_error(monkeypatch, tmp_path):
    exec_file = tmp_path / "exec.json"
    exec_file.write_text("Claude AI usage limit reached|5")
    monkeypatch.setenv("EXEC_FILE", str(exec_file))
    monkeypatch.setenv("GH_TOKEN_CORP", "corp-token")
    monkeypatch.setenv("GH_TOKEN_PILOT", "pilot-token")
    monkeypatch.setattr(sys, "argv", ["scale_claude_model.py", "detect-and-downgrade"])

    def boom(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("unexpected")

    monkeypatch.setattr(scm, "detect_and_downgrade", boom)
    scm.main()  # must not raise / exit non-zero


def test_missing_corp_token_is_noop(monkeypatch):
    api = FakeApi()
    monkeypatch.setattr(scm.urllib.request, "urlopen", api)
    monkeypatch.delenv("GH_TOKEN_CORP", raising=False)
    monkeypatch.setattr(sys, "argv", ["scale_claude_model.py", "restore-if-due"])
    scm.main()
    assert api.calls == []


# --- Public-log policy ---


def test_output_never_contains_pilot_repo_names(monkeypatch, tmp_path, capsys):
    summary = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    api = FakeApi(
        {
            "PATCH /repos/ignite-pilot-org/repo-alpha/actions/variables/CLAUDE_MODEL": 404,
            "POST /repos/ignite-pilot-org/repo-alpha/actions/variables": 403,
            "PATCH /repos/ignite-pilot-org/repo-beta/actions/variables/CLAUDE_MODEL": urllib.error.URLError(
                "timed out"
            ),
            "GET /orgs/ignite-corp/actions/variables/CLAUDE_MODEL_RESTORE_AT": 404,
        }
    )
    _run_detect(monkeypatch, tmp_path, "Claude AI usage limit reached|5", api)
    result = capsys.readouterr()
    captured = result.out + result.err
    if summary.exists():
        captured += summary.read_text()
    for name in ("repo-alpha", "repo-beta"):
        assert name not in captured


def test_dry_run_makes_no_write_calls(monkeypatch, tmp_path):
    monkeypatch.setenv("DRY_RUN", "true")
    api = FakeApi(
        {"GET /orgs/ignite-corp/actions/variables/CLAUDE_MODEL_RESTORE_AT": 404}
    )
    _run_detect(monkeypatch, tmp_path, "Claude AI usage limit reached|5", api)
    assert all(m == "GET" for m, _, _ in api.calls)
