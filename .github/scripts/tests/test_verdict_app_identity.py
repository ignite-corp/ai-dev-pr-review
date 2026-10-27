"""The reviewer App posts every formal verdict when its token is present (AT-2597).

Before, the App token was used for ``approve`` only, so on a repository with
the App a ``request_changes`` review went out as github-actions[bot] and a
gate that read the App's identity saw approvals and missed blocks. GH_TOKEN
stays the fallback for repositories without the App.
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import patch

import pytest

import aggregate_reviews
from aggregate_reviews import _fold_prior_round_items, post_verdict
from github_pr_support import REVIEW_MARKER

VERDICTS = ("approve", "request_changes")
FLAGS = {"approve": "--approve", "request_changes": "--request-changes"}


@pytest.fixture(autouse=True)
def _pr_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PR_NUMBER", "42")
    monkeypatch.setenv("GITHUB_REPOSITORY", "owner/repo")
    monkeypatch.setenv("GH_TOKEN", "default-token")
    monkeypatch.delenv("REVIEWER_TOKEN", raising=False)


def _post(verdict: str, *, comment_only: bool = False) -> list[tuple[list[str], str]]:
    """Every gh call post_verdict makes, with the GH_TOKEN it ran under."""
    calls: list[tuple[list[str], str]] = []

    def fake_run(args: list[str], **kwargs: Any) -> Any:
        env = kwargs.get("env") or {}
        calls.append((args, env.get("GH_TOKEN", "<ambient>")))
        return type("R", (), {"returncode": 0, "stderr": "", "stdout": ""})()

    with (
        patch("aggregate_reviews._fold_prior_round_items", return_value=[]),
        patch("aggregate_reviews._post_comment", return_value=True),
        patch("aggregate_reviews.subprocess.run", side_effect=fake_run),
    ):
        post_verdict("body", verdict, comment_only=comment_only, unreviewed_majors=0)
    return calls


def _review_call(calls: list[tuple[list[str], str]], verdict: str) -> str:
    reviews = [token for args, token in calls if args[:3] == ["gh", "pr", "review"]]
    assert len(reviews) == 1
    assert any(FLAGS[verdict] in args for args, _ in calls)
    return reviews[0]


@pytest.mark.parametrize("verdict", VERDICTS)
def test_review_posts_under_the_app_token_when_present(
    verdict: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("REVIEWER_TOKEN", "app-token")
    assert _review_call(_post(verdict), verdict) == "app-token"


@pytest.mark.parametrize("verdict", VERDICTS)
@pytest.mark.parametrize("token", [None, "", "   "], ids=["unset", "empty", "blank"])
def test_review_falls_back_to_gh_token_without_the_app(
    verdict: str, token: str | None, monkeypatch: pytest.MonkeyPatch
) -> None:
    if token is not None:
        monkeypatch.setenv("REVIEWER_TOKEN", token)
    assert _review_call(_post(verdict), verdict) == "default-token"


@pytest.mark.parametrize("verdict", VERDICTS)
def test_missing_app_token_is_noted_for_every_formal_verdict(
    verdict: str, capsys: pytest.CaptureFixture[str]
) -> None:
    _post(verdict)
    notes = [ln for ln in capsys.readouterr().err.splitlines() if ln.startswith("::")]
    assert len(notes) == 1
    assert notes[0].startswith("::notice::No reviewer App token;")
    assert verdict in notes[0]


@pytest.mark.parametrize("verdict", VERDICTS)
def test_app_token_present_emits_no_annotation(
    verdict: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("REVIEWER_TOKEN", "app-token")
    _post(verdict)
    assert [
        ln for ln in capsys.readouterr().err.splitlines() if ln.startswith("::")
    ] == []


@pytest.mark.parametrize("verdict", VERDICTS)
def test_comment_only_posts_no_review_and_no_notice(
    verdict: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("REVIEWER_TOKEN", "app-token")
    calls = _post(verdict, comment_only=True)
    assert not [args for args, _ in calls if args[:3] == ["gh", "pr", "review"]]
    assert [
        ln for ln in capsys.readouterr().err.splitlines() if ln.startswith("::")
    ] == []


def test_fold_dismisses_a_same_head_changes_requested_without_reading_its_author() -> (
    None
):
    # The App's CHANGES_REQUESTED must fold on the same path as its
    # approvals. The fold keys on the marker, never the author, so the
    # reviews query must not even fetch one.
    assert "author" not in aggregate_reviews._STALE_REVIEWS_QUERY
    reviews = [
        {
            "id": "R_cr",
            "state": "CHANGES_REQUESTED",
            "body": REVIEW_MARKER + "\nx",
            "commit": {"oid": "head"},
        },
        {
            "id": "R_ap",
            "state": "APPROVED",
            "body": REVIEW_MARKER + "\nx",
            "commit": {"oid": "head"},
        },
    ]
    mutations: list[tuple[str, str]] = []

    def fake_nodes(query: str, *_args: Any, **_kwargs: Any) -> list[dict[str, Any]]:
        return reviews if query is aggregate_reviews._STALE_REVIEWS_QUERY else []

    def fake_mutation(_query: str, node_id: str, label: str) -> None:
        mutations.append((label, node_id))

    with (
        patch("aggregate_reviews.fetch_paginated_nodes", side_effect=fake_nodes),
        patch("aggregate_reviews._run_gql_mutation", side_effect=fake_mutation),
    ):
        standing = _fold_prior_round_items("42", "owner/repo", head_sha="head")
    assert ("dismiss", "R_cr") in mutations
    assert standing == ["R_ap"]
    assert json.dumps(mutations).count("R_ap") == 0


def _post_with(verdict: str, fails_under: set[str]) -> tuple[list[str], list[str], int]:
    """Review-call tokens, fallback comment bodies and dismiss calls.

    A review call fails when it runs under a token in ``fails_under``.
    """
    tokens: list[str] = []
    comments: list[str] = []

    def fake_run(args: list[str], **kwargs: Any) -> Any:
        token = (kwargs.get("env") or {}).get("GH_TOKEN", "<ambient>")
        tokens.append(token)
        code = 1 if token in fails_under else 0
        return type("R", (), {"returncode": code, "stderr": "denied", "stdout": ""})()

    def fake_comment(_pr: str, _repo: str, body: str) -> bool:
        comments.append(body)
        return True

    with (
        patch("aggregate_reviews._fold_prior_round_items", return_value=[]),
        patch("aggregate_reviews._dismiss_replaced_approvals") as dismiss,
        patch("aggregate_reviews._post_comment", side_effect=fake_comment),
        patch("aggregate_reviews.subprocess.run", side_effect=fake_run),
    ):
        post_verdict("body", verdict, comment_only=False, unreviewed_majors=0)
    return tokens, comments, dismiss.call_count


def test_request_changes_retries_under_gh_token_when_the_app_post_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A bad App token must not turn a blocking review into a comment when
    # GH_TOKEN, which may always request changes, can post it.
    monkeypatch.setenv("REVIEWER_TOKEN", "app-token")
    tokens, comments, dismissed = _post_with("request_changes", {"app-token"})
    assert tokens == ["app-token", "default-token"]
    assert comments == []
    assert dismissed == 1


@pytest.mark.parametrize("ambient_var", ["GH_TOKEN", "GITHUB_TOKEN"])
def test_request_changes_is_not_retried_when_the_app_token_is_the_ambient_one(
    ambient_var: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # A cross run hands both variables the target's App token; a retry would
    # re-send the same call under the same identity. gh reads GH_TOKEN, then
    # GITHUB_TOKEN, so either one carrying the App token is the same identity.
    monkeypatch.delenv("GH_TOKEN", raising=False)
    monkeypatch.setenv(ambient_var, "app-token")
    monkeypatch.setenv("REVIEWER_TOKEN", "app-token")
    tokens, comments, dismissed = _post_with("request_changes", {"app-token"})
    assert tokens == ["app-token"]
    assert len(comments) == 1
    assert "Intended verdict: **request_changes** (review API failed" in comments[0]
    assert dismissed == 0
    assert "retrying under GH_TOKEN" not in capsys.readouterr().err


def test_request_changes_falls_back_to_comment_when_both_tokens_fail(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("REVIEWER_TOKEN", "app-token")
    tokens, comments, dismissed = _post_with(
        "request_changes", {"app-token", "default-token"}
    )
    assert tokens == ["app-token", "default-token"]
    assert len(comments) == 1
    assert "Intended verdict: **request_changes** (review API failed" in comments[0]
    assert dismissed == 0


def test_approve_is_not_retried_under_gh_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # github-actions[bot] cannot approve, so a retry could not help.
    monkeypatch.setenv("REVIEWER_TOKEN", "app-token")
    tokens, comments, _ = _post_with("approve", {"app-token"})
    assert tokens == ["app-token"]
    assert len(comments) == 1


def test_request_changes_without_the_app_is_not_retried() -> None:
    tokens, comments, _ = _post_with("request_changes", {"default-token"})
    assert tokens == ["default-token"]
    assert len(comments) == 1


def test_request_changes_notice_names_the_identity_not_a_rejection(
    capsys: pytest.CaptureFixture[str],
) -> None:
    # GH_TOKEN may request changes, so the approve notice's rejection clause
    # does not apply; what changes is the identity the review carries.
    _post("request_changes")
    notice = capsys.readouterr().err.splitlines()[0]
    assert notice.startswith("::notice::No reviewer App token;")
    assert "if the API rejects it" not in notice
    assert "reviewer App identity" in notice


def test_retry_warning_keeps_multiline_stderr_inside_one_annotation(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # A workflow command ends at the first newline; the retry warning
    # interpolates gh's stderr, which has several.
    monkeypatch.setenv("REVIEWER_TOKEN", "app-token")

    def fail_app_multiline(_args: list[str], **kwargs: Any) -> Any:
        app = (kwargs.get("env") or {}).get("GH_TOKEN") == "app-token"
        stderr = "HTTP 401: Bad credentials\nRequires authentication\n" if app else ""
        return type("R", (), {"returncode": int(app), "stderr": stderr, "stdout": ""})()

    with (
        patch("aggregate_reviews._fold_prior_round_items", return_value=[]),
        patch("aggregate_reviews._dismiss_replaced_approvals"),
        patch("aggregate_reviews._post_comment", return_value=True),
        patch("aggregate_reviews.subprocess.run", side_effect=fail_app_multiline),
    ):
        post_verdict("body", "request_changes", comment_only=False, unreviewed_majors=0)
    lines = capsys.readouterr().err.splitlines()
    assert len(lines) == 1
    assert lines[0].startswith("::warning::Post review under the reviewer App token")
    assert lines[0].endswith("HTTP 401: Bad credentials%0ARequires authentication")
