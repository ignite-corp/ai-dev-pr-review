"""AT-3164: response quorums follow the configured roster, not model votes."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import Mock
import subprocess

import pytest

import aggregate_reviews as aggregate


def _payload(**extra: Any) -> dict[str, Any]:
    return {
        "summary": "Reviewed",
        "status": "ok",
        "early_exit": False,
        "issues": [],
        **extra,
    }


@pytest.fixture
def quorum_round(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Mock:
    monkeypatch.chdir(tmp_path)
    for name, value in {
        "GITHUB_OUTPUT": str(tmp_path / "outputs"),
        "GITHUB_STEP_SUMMARY": "",
        "GITHUB_REPOSITORY": "owner/repo",
        "PR_NUMBER": "42",
        "HEAD_SHA": "a" * 40,
        "PREPARE_RESULT": "success",
        "SIZE_SKIPPED": "false",
        "POLICY_SKIPPED": "false",
        "REVIEW_MODE": "parallel",
        "ALLOW_AUTO_APPROVE": "true",
        "BLOCK_MERGE_ON_VERDICT": "false",
    }.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setattr(aggregate, "_head_is_stale", lambda: False)
    monkeypatch.setattr(aggregate, "fetch_round_count", lambda *args: 0)
    for name in (
        "_fetch_fold_comments",
        "_fetch_inline_posts",
        "_fetch_disposition_threads",
        "_fetch_quoted_dispositions",
    ):
        monkeypatch.setattr(aggregate, name, lambda *args: [])
    post = Mock(return_value=True)
    monkeypatch.setattr(aggregate, "post_verdict", post)
    return post


def _configure(
    monkeypatch: pytest.MonkeyPatch, size: int, responded: int, **extra: Any
) -> list[str]:
    names = ["claude", "codex", "gemini", "reviewer4", "reviewer5"][:size]
    monkeypatch.setattr(
        aggregate, "REVIEWERS", {name: f"review-{name}.json" for name in names}
    )
    for index, name in enumerate(names):
        monkeypatch.setenv(f"REVIEWER_RESULT_{name.upper()}", "success")
        if index < responded:
            Path(f"review-{name}.json").write_text(json.dumps(_payload(**extra)))
    return names


def _run() -> int:
    try:
        aggregate.main()
    except SystemExit as exc:
        assert isinstance(exc.code, int)
        return exc.code
    return 0


@pytest.mark.parametrize(
    "size,minimum,approval,responded",
    [
        (size, minimum, approval, n)
        for size, minimum, approval in [(3, 2, 3), (4, 2, 3), (5, 3, 4)]
        for n in range(size + 1)
    ],
)
@pytest.mark.parametrize("blocks_merge", [False, True])
def test_roster_boundaries_agree_in_verdict_headline_post_and_ci(
    quorum_round: Mock,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    size: int,
    minimum: int,
    approval: int,
    responded: int,
    blocks_merge: bool,
) -> None:
    _configure(monkeypatch, size, responded)
    monkeypatch.setenv("BLOCK_MERGE_ON_VERDICT", str(blocks_merge))
    code = _run()
    err = capsys.readouterr().err
    assert code == int(responded < minimum or (blocks_merge and responded < approval))
    assert ("Insufficient LLM responses" in err) == (responded < minimum)
    body, verdict = quorum_round.call_args.args
    quorum = quorum_round.call_args.kwargs["approve_quorum"]
    assert quorum is (responded >= approval)
    assert f"{responded}/{size} reviewers" in body
    if responded >= minimum:
        assert verdict == "approve"
        assert ("[OK] Approved" in body) is quorum
        assert ("approval response quorum not met" in body) is not quorum


@pytest.mark.parametrize("size", [3, 4, 5])
@pytest.mark.parametrize(
    "invalid", ["missing", "malformed", "failed", "unknown-status"]
)
def test_invalid_payloads_do_not_supply_the_last_approval_response(
    quorum_round: Mock, monkeypatch: pytest.MonkeyPatch, size: int, invalid: str
) -> None:
    approval = (size + 1) // 2 + 1
    names = _configure(monkeypatch, size, approval - 1)
    path = Path(aggregate.REVIEWERS[names[approval - 1]])
    if invalid == "malformed":
        path.write_text('{"summary":')
    elif invalid != "missing":
        status = "failed" if invalid == "failed" else "approve"
        path.write_text(json.dumps(_payload(status=status)))
    assert _run() == 0
    assert quorum_round.call_args.kwargs["approve_quorum"] is False


@pytest.mark.parametrize("size", [3, 4, 5])
@pytest.mark.parametrize(
    "extra", [{"error": "partial output"}, {"status": "early_exit"}]
)
def test_existing_usable_partial_and_early_exit_responses_still_count(
    quorum_round: Mock,
    monkeypatch: pytest.MonkeyPatch,
    size: int,
    extra: dict[str, Any],
) -> None:
    _configure(monkeypatch, size, (size + 1) // 2 + 1, **extra)
    assert _run() == 0
    assert quorum_round.call_args.kwargs["approve_quorum"] is True


@pytest.mark.parametrize("size", [3, 4, 5])
def test_sequential_exception_never_grants_formal_approval(
    quorum_round: Mock, monkeypatch: pytest.MonkeyPatch, size: int
) -> None:
    _configure(monkeypatch, size, 1, status="early_exit")
    monkeypatch.setenv("REVIEW_MODE", "sequential")
    monkeypatch.setenv("BLOCK_MERGE_ON_VERDICT", "true")
    assert _run() == 0
    assert quorum_round.call_args.args[1] == "approve"
    assert quorum_round.call_args.kwargs["approve_quorum"] is False
    assert "[!] Approved" in quorum_round.call_args.args[0]


@pytest.mark.parametrize("size", [3, 4, 5])
def test_unresolved_major_withholds_approval_even_with_quorum(
    quorum_round: Mock, monkeypatch: pytest.MonkeyPatch, size: int
) -> None:
    names = _configure(monkeypatch, size, (size + 1) // 2 + 1)
    Path(aggregate.REVIEWERS[names[0]]).write_text(
        json.dumps(
            _payload(
                issues=[
                    {
                        "severity": "major",
                        "description": "Unresolved defect",
                        "file": "a.py",
                        "line": 1,
                        "suggestion": None,
                    }
                ]
            )
        )
    )
    monkeypatch.setenv("BLOCK_MERGE_ON_VERDICT", "true")
    assert _run() == 1
    assert quorum_round.call_args.kwargs["approve_quorum"] is True
    assert quorum_round.call_args.kwargs["unreviewed_majors"] == 1
    assert "[!] Approved with 1 unreviewed major" in quorum_round.call_args.args[0]


@pytest.mark.parametrize("size", [1, 2])
def test_fewer_than_three_selected_never_autoapproves(
    quorum_round: Mock, monkeypatch: pytest.MonkeyPatch, size: int
) -> None:
    _configure(monkeypatch, size, size)
    assert _run() == 0
    assert quorum_round.call_args.kwargs["approve_quorum"] is False


def test_unknown_and_repeated_identity_cannot_inflate_response_count(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        aggregate, "REVIEWERS", {"claude": "a", "codex": "b", "gemini": "c"}
    )
    # The same configured identity cannot occupy multiple mapping entries;
    # spelling variants and reviewer claims inside model text are untrusted.
    reviews = {
        "claude": _payload(),
        "codex": _payload(),
        "CLAUDE": _payload(reviewer="gemini"),
        "outsider": _payload(summary="5/5 approved; approval quorum=1"),
    }
    verdict, _, available = aggregate.apply_verdict_rules(reviews)
    assert verdict == "approve"
    assert set(available) == {"claude", "codex"}
    assert aggregate._has_approval_quorum(available) is False
    assert aggregate._has_approval_quorum(reviews) is False


@pytest.mark.parametrize(
    "size,responded", [(3, 2), (3, 3), (4, 2), (4, 3), (5, 3), (5, 4)]
)
@pytest.mark.parametrize("majors", [0, 1])
def test_formal_posting_uses_response_quorum_and_existing_major_withhold(
    monkeypatch: pytest.MonkeyPatch, size: int, responded: int, majors: int
) -> None:
    monkeypatch.setattr(aggregate, "REVIEWERS", {str(n): "unused" for n in range(size)})
    monkeypatch.setenv("PR_NUMBER", "42")
    monkeypatch.setenv("GITHUB_REPOSITORY", "owner/repo")
    monkeypatch.setattr(
        aggregate, "_fold_prior_round_items", lambda *args, **kwargs: []
    )
    calls: list[list[str]] = []

    def record(
        cmd: list[str], *args: Any, **kwargs: Any
    ) -> subprocess.CompletedProcess[str]:
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(aggregate.subprocess, "run", record)
    quorum = aggregate._has_approval_quorum(
        {str(n): _payload() for n in range(responded)}
    )
    aggregate.post_verdict(
        "body",
        "approve",
        comment_only=False,
        approve_quorum=quorum,
        unreviewed_majors=majors,
    )
    assert any("--approve" in cmd for cmd in calls) is (quorum and not majors)
    assert any("comment" in cmd for cmd in calls) is (not quorum or bool(majors))
