"""Quota transitions consume CLI terminal events, never reviewer-controlled prose."""

import json
import io
import os
import stat
import subprocess
import sys
import urllib.error
from datetime import UTC, datetime
from email.message import Message
from pathlib import Path
from typing import Any

import pytest

import codex_review_auth as auth


def _events(tmp_path: Path, *events: dict) -> Path:
    path = tmp_path / "events.jsonl"
    path.write_text("".join(json.dumps(e) + "\n" for e in events))
    return path


NOW = datetime(2026, 10, 27, 11, 0, tzinfo=UTC).timestamp()
LIMIT = "You've hit your usage limit. Try again later."


def test_quota_terminal_failure_has_bounded_probe_interval(tmp_path: Path) -> None:
    events = _events(tmp_path, {"type": "turn.failed", "error": {"message": LIMIT}})
    assert auth.quota_until(events, 1, NOW) == NOW + 5 * 3600
    assert auth.quota_until(events, 0, NOW) is None


@pytest.mark.parametrize(
    "message",
    [
        "You've hit your usage limit. To get more access now, send a request to your admin or try again later.",
        "Your workspace is out of credits. Add credits to continue.",
        "Your workspace is out of credits. Ask your workspace owner to refill in order to continue.",
        "You hit your spend cap set in your workspace. Increase your spend cap to continue.",
        "You hit your spend cap set by the owner of your workspace. Ask an owner to increase your spend cap to continue.",
    ],
)
def test_official_workspace_limit_messages(tmp_path: Path, message: str) -> None:
    events = _events(tmp_path, {"type": "turn.failed", "error": {"message": message}})
    assert auth.quota_until(events, 1, NOW) == NOW + 5 * 3600


@pytest.mark.parametrize(
    "event",
    [
        {"type": "item.completed", "item": {"type": "agent_message", "text": LIMIT}},
        {
            "type": "item.completed",
            "item": {"type": "command_execution", "aggregated_output": LIMIT},
        },
        {"type": "turn.failed", "error": {"message": "HTTP 429 rate limit exceeded"}},
        {"type": "turn.failed", "error": {"message": "Unauthorized: token expired"}},
        {"type": "turn.failed", "error": {"message": "connection timed out"}},
        {"type": "turn.failed", "error": {"message": "model not available"}},
        {"type": "turn.failed", "error": {"message": "Tool failed: " + LIMIT}},
        {
            "type": "turn.failed",
            "error": {
                "message": "You hit your spend cap set by your workspace admin. Contact your admin to increase your cap."
            },
        },
        {"type": "error", "message": LIMIT},
    ],
)
def test_lookalikes_do_not_trigger_paid_switch(tmp_path: Path, event: dict) -> None:
    assert auth.quota_until(_events(tmp_path, event), 1, NOW) is None


def test_completed_turn_and_nested_terminal_json_are_not_failures(
    tmp_path: Path,
) -> None:
    failed = {"type": "turn.failed", "error": {"message": LIMIT}}
    nested = {
        "type": "item.completed",
        "item": {"type": "agent_message", "text": json.dumps(failed)},
    }
    assert auth.quota_until(_events(tmp_path, nested), 1, NOW) is None
    assert (
        auth.quota_until(_events(tmp_path, failed, {"type": "turn.completed"}), 1, NOW)
        is None
    )


@pytest.mark.parametrize(
    ("suffix", "expected"),
    [
        (
            "Try again at 1:30 PM.",
            datetime(2026, 10, 27, 13, 31, tzinfo=UTC).timestamp(),
        ),
        (
            "Try again at Oct 28th, 2026 1:30 PM.",
            datetime(2026, 10, 28, 13, 31, tzinfo=UTC).timestamp(),
        ),
        ("Try again at 1:30 AM.", NOW + 5 * 3600),
    ],
)
def test_utc_reset_is_preferred_but_not_rounded_early(
    tmp_path: Path, suffix: str, expected: float
) -> None:
    event = {
        "type": "turn.failed",
        "error": {"message": "You've hit your usage limit. " + suffix},
    }
    assert auth.quota_until(_events(tmp_path, event), 1, NOW) == expected


class Variables:
    def __init__(self, values: dict[str, str] | None = None):
        self.values = dict(values or {})
        self.writes: list[tuple[str, str | None]] = []

    def get(self, name: str) -> str | None:
        return self.values.get(name)

    def set(self, name: str, value: str) -> None:
        self.writes.append((name, value))
        self.values[name] = value

    def delete(self, name: str) -> None:
        self.writes.append((name, None))
        self.values.pop(name, None)


@pytest.mark.parametrize(("first", "second"), [(300, 800), (800, 300)])
def test_switch_keeps_earlier_expiry_and_restore_is_scoped(
    first: int,
    second: int,
) -> None:
    store = Variables({"CLAUDE_FORCE_API": "true"})
    auth.persist(store, int(NOW + first), NOW)
    auth.persist(store, int(NOW + second), NOW)
    assert store.values[auth.UNTIL] == str(int(NOW + 300))
    assert store.values[auth.FORCE] == "true"
    assert auth.api_override(store, NOW)
    auth.restore(store, NOW + 299)
    assert auth.FORCE in store.values
    auth.restore(store, NOW + 300)
    assert store.values == {"CLAUDE_FORCE_API": "true"}


def test_partial_state_never_permanently_forces_api() -> None:
    assert not auth.api_override(Variables({auth.FORCE: "true"}), NOW)
    assert not auth.api_override(
        Variables({auth.FORCE: "true", auth.UNTIL: "bad"}), NOW
    )
    assert not auth.api_override(
        Variables({auth.FORCE: "true", auth.UNTIL: str(int(NOW))}), NOW
    )
    assert auth.api_override(Variables({auth.UNTIL: str(int(NOW + 20))}), NOW)


def test_control_plane_read_failure_is_not_treated_as_missing() -> None:
    class Broken(Variables):
        def get(self, name: str) -> str | None:
            raise auth.ControlPlaneError("HTTP 403")

    store = Broken()
    with pytest.raises(auth.ControlPlaneError):
        auth.persist(store, int(NOW + 300), NOW)
    assert not store.writes


@pytest.mark.parametrize(
    ("token", "key", "until", "mode"),
    [
        ("test-token", "test-key", None, "access-token"),
        ("test-token", "", None, "access-token"),
        ("", "test-key", None, "api-key"),
        ("test-token", "test-key", int(NOW + 500), "api-key"),
        ("test-token", "test-key", int(NOW - 1), "access-token"),
    ],
)
def test_auth_preference_and_credential_isolation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    token: str,
    key: str,
    until: int | None,
    mode: str,
) -> None:
    monkeypatch.setenv("CODEX_ACCESS_TOKEN", token)
    monkeypatch.setenv("OPENAI_API_KEY", key)
    monkeypatch.setenv("GH_TOKEN", "test-control-token")
    calls = []

    def login(args: list[str], **kwargs: Any) -> subprocess.CompletedProcess:
        calls.append((args, kwargs))
        assert not any(
            "KEY" in k or "TOKEN" in k or "SECRET" in k for k in kwargs["env"]
        )
        assert token not in args and key not in args
        directory = Path(kwargs["env"]["CODEX_HOME"])
        (directory / "auth.json").write_text(json.dumps({"kind": args[-1]}))
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(auth.subprocess, "run", login)
    store = Variables({auth.UNTIL: str(until)}) if until else Variables()
    auth.prepare(tmp_path, store, NOW)
    assert json.loads((tmp_path / "review-auth.json").read_text())["mode"] == mode
    assert json.loads((tmp_path / "auth.json").read_text())["kind"] == "--with-" + mode
    assert calls[-1][1]["input"] == (token if mode == "access-token" else key)
    for name in ["auth.json", "review-auth.json"]:
        assert stat.S_IMODE((tmp_path / name).stat().st_mode) == 0o600


@pytest.mark.parametrize(("token", "key", "forced"), [("", "", False), ("", "", True)])
def test_missing_selected_credential_does_not_run_other_auth(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    token: str,
    key: str,
    forced: bool,
) -> None:
    monkeypatch.setenv("CODEX_ACCESS_TOKEN", token)
    monkeypatch.setenv("OPENAI_API_KEY", key)
    (tmp_path / "review-quota.json").write_text('{"until":9999999999}')
    store = Variables({auth.UNTIL: str(int(NOW + 500))}) if forced else Variables()
    with pytest.raises(ValueError, match="credential is missing"):
        auth.prepare(tmp_path, store, NOW)
    assert not (tmp_path / "review-quota.json").exists()


def test_failed_token_login_does_not_silently_select_api(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CODEX_ACCESS_TOKEN", "test-token")
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")

    def login(credential: str, mode: str, directory: Path) -> bytes:
        if mode == "access-token":
            raise ValueError("Codex access-token login failed")
        return b'{"kind":"api-key"}'

    monkeypatch.setattr(auth, "_login", login)
    with pytest.raises(ValueError, match="access-token login failed"):
        auth.prepare(tmp_path, Variables(), NOW)
    assert not (tmp_path / "review-auth.json").exists()


@pytest.mark.parametrize(
    "failure", ["fallback_login", "fallback_read", "fallback_write"]
)
def test_optional_fallback_oserror_preserves_token_and_never_persists_api_window(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("CODEX_ACCESS_TOKEN", "test-token")
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("GH_TOKEN", "test-control-token")
    home = tmp_path / ".codex"
    logins: list[str] = []

    def login(args: list[str], **kwargs: Any) -> subprocess.CompletedProcess:
        mode = args[-1]
        logins.append(mode)
        if mode == "--with-api-key" and failure == "fallback_login":
            raise OSError("injected fallback login failure")
        (Path(kwargs["env"]["CODEX_HOME"]) / "auth.json").write_text(
            json.dumps({"kind": mode})
        )
        return subprocess.CompletedProcess(args, 0, "", "")

    original_read = Path.read_bytes

    def read(path: Path) -> bytes:
        if path == home / "review-api-login/auth.json" and failure == "fallback_read":
            raise OSError("injected fallback read failure")
        return original_read(path)

    original_write = auth._write_private

    def write(path: Path, data: bytes) -> None:
        if path == home / "review-api-auth.json" and failure == "fallback_write":
            raise OSError("injected fallback write failure")
        original_write(path, data)

    monkeypatch.setattr(auth.subprocess, "run", login)
    monkeypatch.setattr(Path, "read_bytes", read)
    monkeypatch.setattr(auth, "_write_private", write)
    store = Variables()
    monkeypatch.setattr(auth, "OrgVariables", lambda token: store)
    auth.prepare(home, store, NOW)
    assert logins == ["--with-api-key", "--with-access-token"]
    assert json.loads((home / "auth.json").read_text())["kind"] == "--with-access-token"
    assert json.loads((home / "review-auth.json").read_text())["mode"] == "access-token"
    assert not (home / "review-api-auth.json").exists()
    events = _events(tmp_path, {"type": "turn.failed", "error": {"message": LIMIT}})
    assert auth.retry(home, events, 1, NOW) == auth.NO_API_FALLBACK
    assert not (home / "review-quota.json").exists()
    monkeypatch.setattr(sys, "argv", ["codex_review_auth.py", "persist"])
    assert auth.main() == 0
    assert not store.writes


@pytest.mark.parametrize("failure", ["selected_login", "selected_write"])
def test_selected_token_oserror_remains_fatal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    monkeypatch.setenv("CODEX_ACCESS_TOKEN", "test-token")
    monkeypatch.setenv("OPENAI_API_KEY", "")

    def login(credential: str, mode: str, destination: Path) -> bytes:
        if failure == "selected_login":
            raise OSError("injected selected login failure")
        return b'{"kind":"token"}'

    def write(path: Path, data: bytes) -> None:
        raise OSError("injected selected write failure")

    monkeypatch.setattr(auth, "_login", login)
    monkeypatch.setattr(auth, "_write_private", write)
    with pytest.raises(OSError, match="injected selected"):
        auth.prepare(tmp_path, Variables(), NOW)
    assert not (tmp_path / "review-auth.json").exists()
    assert not (tmp_path / "review-quota.json").exists()


def test_quota_retry_replaces_auth_without_inheriting_tokens_and_is_not_recursive(
    tmp_path: Path,
) -> None:
    (tmp_path / "review-auth.json").write_text('{"mode":"access-token"}')
    (tmp_path / "auth.json").write_text('{"kind":"token"}')
    (tmp_path / "review-api-auth.json").write_text('{"kind":"api"}')
    events = _events(tmp_path, {"type": "turn.failed", "error": {"message": LIMIT}})
    assert auth.retry(tmp_path, events, 1, NOW) == 0
    assert json.loads((tmp_path / "auth.json").read_text()) == {"kind": "api"}
    assert (
        json.loads((tmp_path / "review-quota.json").read_text())["until"]
        == NOW + 5 * 3600
    )
    assert auth.retry(tmp_path, events, 1, NOW) == auth.NO_RETRY


def test_no_api_fallback_remains_an_explicit_failure(tmp_path: Path) -> None:
    (tmp_path / "review-auth.json").write_text('{"mode":"access-token"}')
    events = _events(tmp_path, {"type": "turn.failed", "error": {"message": LIMIT}})
    assert auth.retry(tmp_path, events, 1, NOW) == auth.NO_API_FALLBACK
    assert not (tmp_path / "review-quota.json").exists()


def test_token_only_quota_failure_does_not_persist_or_force_missing_api_auth(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("CODEX_ACCESS_TOKEN", "test-token")
    monkeypatch.setenv("OPENAI_API_KEY", "")
    monkeypatch.setenv("GH_TOKEN", "test-control-token")
    monkeypatch.setattr(sys, "argv", ["codex_review_auth.py", "persist"])
    store = Variables()
    monkeypatch.setattr(auth, "OrgVariables", lambda token: store)
    logins: list[str] = []

    def login(credential: str, mode: str, destination: Path) -> bytes:
        logins.append(mode)
        return b'{"kind":"test-token"}'

    monkeypatch.setattr(auth, "_login", login)
    home = tmp_path / ".codex"
    now = auth.time.time()
    auth.prepare(home, store, now)
    events = _events(tmp_path, {"type": "turn.failed", "error": {"message": LIMIT}})
    assert auth.retry(home, events, 1, now) == auth.NO_API_FALLBACK
    quota_marker_created = (home / "review-quota.json").exists()
    assert auth.main() == 0
    # Exercise the next complete selection before asserting observations:
    # the old marker forced API auth here even though no API key existed.
    auth.prepare(home, store, now + 1)
    assert not quota_marker_created
    assert not store.writes
    assert logins == ["access-token", "access-token"]
    assert json.loads((home / "review-auth.json").read_text())["mode"] == "access-token"


def test_token_only_caller_can_use_token_during_another_callers_api_window(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture,
) -> None:
    monkeypatch.setenv("CODEX_ACCESS_TOKEN", "test-token")
    monkeypatch.setenv("OPENAI_API_KEY", "")
    store = Variables({auth.UNTIL: str(int(NOW + 300)), auth.FORCE: "true"})
    before = dict(store.values)
    monkeypatch.setattr(
        auth, "_login", lambda token, mode, destination: b'{"kind":"token"}'
    )
    auth.prepare(tmp_path, store, NOW)
    assert (
        json.loads((tmp_path / "review-auth.json").read_text())["mode"]
        == "access-token"
    )
    assert "fallback is unavailable for this caller" in capsys.readouterr().out
    assert store.values == before and not store.writes


def test_no_quota_uses_reserved_no_retry_status(tmp_path: Path) -> None:
    (tmp_path / "review-auth.json").write_text('{"mode":"access-token"}')
    events = _events(tmp_path, {"type": "turn.completed"})
    assert auth.NO_API_FALLBACK == 3
    assert auth.NO_RETRY == 4
    assert auth.retry(tmp_path, events, 0, NOW) == auth.NO_RETRY


def test_retry_auth_write_does_not_follow_a_stale_symlink(tmp_path: Path) -> None:
    outside = tmp_path / "untouched"
    outside.write_text("original")
    (tmp_path / "auth.new").symlink_to(outside)
    (tmp_path / "auth.json").symlink_to(outside)
    auth._write_private(tmp_path / "auth.json", b"replacement")
    assert outside.read_text() == "original"
    assert (tmp_path / "auth.json").read_bytes() == b"replacement"
    assert not (tmp_path / "auth.json").is_symlink()


def test_switch_does_not_claim_success_when_flag_write_fails(
    capsys: pytest.CaptureFixture,
) -> None:
    class Partial(Variables):
        def set(self, name: str, value: str) -> None:
            if name == auth.FORCE:
                raise auth.ControlPlaneError("HTTP 403")
            super().set(name, value)

    store = Partial()
    with pytest.raises(auth.ControlPlaneError):
        auth.persist(store, int(NOW + 300), NOW)
    assert auth.api_override(store, NOW)
    assert "persisted" not in capsys.readouterr().out
    auth.restore(store, NOW + 300)
    assert not store.values


def test_restore_preserves_a_concurrently_changed_window() -> None:
    class Changing(Variables):
        def __init__(self):
            super().__init__({auth.UNTIL: str(int(NOW - 1))})
            self.reads = 0

        def get(self, name: str) -> str | None:
            self.reads += 1
            if self.reads == 2:
                self.values[auth.UNTIL] = str(int(NOW + 100))
            return super().get(name)

    store = Changing()
    with pytest.raises(auth.ControlPlaneError, match="changed"):
        auth.restore(store, NOW)
    assert not store.writes


def test_restore_reports_a_window_changed_after_flag_deletion(
    capsys: pytest.CaptureFixture,
) -> None:
    class Changing(Variables):
        def delete(self, name: str) -> None:
            super().delete(name)
            if name == auth.FORCE:
                self.values[auth.UNTIL] = str(int(NOW + 100))

    store = Changing({auth.UNTIL: str(int(NOW - 1)), auth.FORCE: "true"})
    with pytest.raises(auth.ControlPlaneError, match="changed"):
        auth.restore(store, NOW)
    assert "restored" not in capsys.readouterr().out
    assert auth.api_override(store, NOW)


@pytest.mark.parametrize(
    "fixture_name",
    [
        "codex-0.147.0-quota.jsonl",
        # 0.166.0 renders "You've" with U+2019 where 0.147.0 printed an
        # ASCII apostrophe (AT-3174); captured offline from the real CLI,
        # with the character stored as its JSON escape (test_ascii_only).
        # Its reset reads "Oct 27th, 2026 2:04 PM" rather than "2:04 PM"
        # only because it was captured on a later day: both versions print
        # the bare time when the reset falls on the current local date
        # (format_retry_timestamp is identical in the two tags).
        "codex-0.166.0-quota.jsonl",
    ],
)
def test_real_cli_offline_quota_fixture(fixture_name: str) -> None:
    fixture = Path(__file__).parent / "fixtures" / fixture_name
    assert (
        auth.quota_until(fixture, 1, NOW)
        == datetime(2026, 10, 27, 14, 5, tzinfo=UTC).timestamp()
    )


@pytest.mark.parametrize("apostrophe", ["'", "\u2019"])
def test_named_model_quota_uses_same_retry_path(
    tmp_path: Path, apostrophe: str
) -> None:
    message = f"You{apostrophe}ve hit your usage limit for codex_other. Switch to another model now, or try again later."
    event = {"type": "turn.failed", "error": {"message": message}}
    assert auth.quota_until(_events(tmp_path, event), 1, NOW) == NOW + 5 * 3600


@pytest.mark.parametrize("status", [401, 403, 429, 500])
def test_control_plane_http_errors_do_not_become_absent_state(
    monkeypatch: pytest.MonkeyPatch,
    status: int,
) -> None:
    def error(request: Any, timeout: int) -> None:
        assert (
            request.full_url
            == "https://api.github.com/orgs/ignite-corp/actions/variables/" + auth.UNTIL
        )
        raise urllib.error.HTTPError(
            request.full_url, status, "error", Message(), io.BytesIO()
        )

    monkeypatch.setattr(auth.urllib.request, "urlopen", error)
    with pytest.raises(auth.ControlPlaneError, match=str(status)):
        auth.OrgVariables("test-control-token").get(auth.UNTIL)


def test_control_plane_absence_is_only_404(monkeypatch: pytest.MonkeyPatch) -> None:
    def missing(request: Any, timeout: int) -> None:
        raise urllib.error.HTTPError(
            request.full_url, 404, "absent", Message(), io.BytesIO()
        )

    monkeypatch.setattr(auth.urllib.request, "urlopen", missing)
    assert auth.OrgVariables("test-control-token").get(auth.UNTIL) is None


def test_switch_creates_only_central_coded_variables(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = []

    class Response:
        def __enter__(self) -> Response:
            return self

        def __exit__(self, *args: Any) -> None:
            pass

        def read(self) -> bytes:
            return b"{}"

    def request(req: Any, timeout: int) -> Response:
        method = req.get_method()
        body = json.loads(req.data) if req.data else None
        calls.append((method, req.full_url, body))
        assert req.full_url.startswith(
            "https://api.github.com/orgs/ignite-corp/actions/variables"
        )
        assert "test-control-token" not in req.full_url
        if method in {"GET", "PATCH"}:
            raise urllib.error.HTTPError(
                req.full_url, 404, "absent", Message(), io.BytesIO()
            )
        return Response()

    monkeypatch.setattr(auth.urllib.request, "urlopen", request)
    auth.persist(auth.OrgVariables("test-control-token"), int(NOW + 100), NOW)
    posts = [body for method, _, body in calls if method == "POST"]
    assert posts == [
        {"name": auth.UNTIL, "value": str(int(NOW + 100)), "visibility": "all"},
        {"name": auth.FORCE, "value": "true", "visibility": "all"},
    ]


@pytest.mark.parametrize(
    ("mode", "annotation"),
    [("prepare", "error"), ("persist", "warning"), ("restore", "warning")],
)
def test_fatal_prepare_is_an_error_but_control_plane_jobs_keep_warning_annotations(
    tmp_path: Path,
    mode: str,
    annotation: str,
) -> None:
    home = tmp_path / "home"
    (home / ".codex").mkdir(parents=True)
    (home / ".codex/review-quota.json").write_text('{"until":9999999999}')
    result = subprocess.run(
        [sys.executable, str(Path(auth.__file__)), mode],
        env={"PATH": os.environ["PATH"], "HOME": str(home)},
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 2
    assert result.stderr.startswith(f"::{annotation}::"), result.stderr
