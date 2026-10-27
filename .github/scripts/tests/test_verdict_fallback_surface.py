"""post_verdict leaves a trace on each fallback it takes (AT-2597).

Two fallbacks used to be silent. A timed-out review call posted the verdict
as a plain comment with no note, so a blocking verdict read the same as a
policy-skip comment. And an approve with no reviewer App token went out
under the default identity without a word in the log.
"""

from __future__ import annotations

import subprocess
from typing import Any
from unittest.mock import patch

import pytest

from aggregate_reviews import _REASON_REVIEW_FAILED, post_verdict


def _ok(*_args: Any, **_kwargs: Any) -> Any:
    return type("R", (), {"returncode": 0, "stderr": "", "stdout": ""})()


def _timeout(*_args: Any, **_kwargs: Any) -> Any:
    raise subprocess.TimeoutExpired(cmd="gh", timeout=1)


@pytest.fixture(autouse=True)
def _pr_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PR_NUMBER", "42")
    monkeypatch.setenv("GITHUB_REPOSITORY", "owner/repo")
    monkeypatch.setenv("GH_TOKEN", "default-token")
    monkeypatch.delenv("REVIEWER_TOKEN", raising=False)


def _post(verdict: str, run: Any, *, comment_only: bool = False) -> list[str]:
    bodies: list[str] = []

    def fake_comment(_pr: str, _repo: str, body: str) -> bool:
        bodies.append(body)
        return True

    with (
        patch("aggregate_reviews._fold_prior_round_items"),
        patch("aggregate_reviews._dismiss_replaced_approvals"),
        patch("aggregate_reviews._post_comment", side_effect=fake_comment),
        patch("aggregate_reviews.subprocess.run", side_effect=run),
    ):
        post_verdict(verdict, verdict, comment_only=comment_only, unreviewed_majors=0)
    return bodies


@pytest.mark.parametrize("verdict", ["approve", "request_changes"])
def test_review_timeout_fallback_comment_names_the_intended_verdict(
    verdict: str,
) -> None:
    bodies = _post(verdict, _timeout)
    assert len(bodies) == 1
    assert f"Intended verdict: **{verdict}**" in bodies[0]
    assert "timed out" in bodies[0]


def _fail(*_args: Any, **_kwargs: Any) -> Any:
    return type("R", (), {"returncode": 1, "stderr": "forbidden", "stdout": ""})()


def test_review_failure_and_timeout_notes_differ_only_in_reason() -> None:
    timed_out = _post("approve", _timeout)[0]
    failed = _post("approve", _fail)[0]
    note = "> [!] Intended verdict: **approve** (review API {}, posted as comment)"
    assert timed_out.endswith(note.format("timed out"))
    assert failed.endswith(note.format("failed"))


def _annotations(err: str) -> list[str]:
    return [line for line in err.splitlines() if line.startswith("::")]


def test_approve_without_app_token_is_a_notice_not_a_warning(
    capsys: pytest.CaptureFixture[str],
) -> None:
    # Auto-approve on with no App is a supported setup whose approve can
    # land, so the absent token is noted; only a real fallback warns.
    _post("approve", _ok)
    notes = _annotations(capsys.readouterr().err)
    assert len(notes) == 1
    assert notes[0].startswith("::notice::")
    # It points at the note the failure fallback actually writes.
    assert f"'{_REASON_REVIEW_FAILED}'" in notes[0]


def test_identity_fallback_notice_asserts_neither_outcome(
    capsys: pytest.CaptureFixture[str],
) -> None:
    # Whether GH_TOKEN may approve depends on the org's Actions setting, so
    # the notice conditions the fallback rather than predicting it.
    _post("approve", _ok)
    notice = _annotations(capsys.readouterr().err)[0]
    assert "cannot" not in notice
    assert "if the API rejects it" in notice


def test_approve_with_blank_app_token_is_a_notice(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("REVIEWER_TOKEN", "   ")
    _post("approve", _ok)
    assert _annotations(capsys.readouterr().err)[0].startswith("::notice::")


@pytest.mark.parametrize("run", [_fail, _timeout], ids=["failed", "timed_out"])
@pytest.mark.parametrize("verdict", ["approve", "request_changes"])
def test_review_fallback_is_a_warning(
    verdict: str, run: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    _post(verdict, run)
    warnings = [
        n for n in _annotations(capsys.readouterr().err) if n.startswith("::warning::")
    ]
    assert len(warnings) == 1
    assert "falling back to comment" in warnings[0]


def test_multiline_review_error_stays_inside_one_annotation(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # A workflow command ends at the first newline; gh's error text has
    # several, so an unescaped one would strand the rest as bare log lines.
    # An approve under the App token emits no notice and is not retried,
    # so the fallback warning is the only line.
    monkeypatch.setenv("REVIEWER_TOKEN", "app-token")

    def fail_multiline(*_args: Any, **_kwargs: Any) -> Any:
        stderr = "HTTP 422: Unprocessable\nValidation failed\n"
        return type("R", (), {"returncode": 1, "stderr": stderr, "stdout": ""})()

    _post("approve", fail_multiline)
    lines = capsys.readouterr().err.splitlines()
    assert len(lines) == 1
    assert lines[0].startswith("::warning::Post review failed, falling back to")
    assert lines[0].endswith("HTTP 422: Unprocessable%0AValidation failed")


def test_approve_with_app_token_emits_no_annotation(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("REVIEWER_TOKEN", "app-token")
    _post("approve", _ok)
    assert _annotations(capsys.readouterr().err) == []


def test_request_changes_without_app_token_is_a_notice(
    capsys: pytest.CaptureFixture[str],
) -> None:
    # request_changes uses the App identity when present, so its absence is
    # noted the same way as for approve.
    _post("request_changes", _ok)
    notes = _annotations(capsys.readouterr().err)
    assert len(notes) == 1
    assert notes[0].startswith("::notice::")


def test_comment_only_approve_without_app_token_emits_no_annotation(
    capsys: pytest.CaptureFixture[str],
) -> None:
    # Auto-approve off posts a comment by design; no App identity is expected.
    _post("approve", _ok, comment_only=True)
    assert _annotations(capsys.readouterr().err) == []
