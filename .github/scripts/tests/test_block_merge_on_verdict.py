"""AT-3114: BLOCK_MERGE_ON_VERDICT turns a blocking verdict into a red check.

The aggregate job ended green on request_changes and on an approval it
withheld, so a repository whose rulesets require no approval could merge
under either. With the flag on, the job fails on those verdicts -- after
the verdict is posted, so the red check always has its explanation on the
PR. Off (the default everywhere), nothing changes.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from aggregate_reviews import REVIEWER_NAMES, main

WORKFLOWS = Path(__file__).resolve().parents[2] / "workflows"
_NAMES = list(REVIEWER_NAMES)


def _review(issues: list[dict[str, Any]]) -> dict[str, Any]:
    return {"summary": "review", "status": "ok", "early_exit": False, "issues": issues}


def _major(file: str, reviewer: str) -> dict[str, Any]:
    return {
        "severity": "major",
        "file": file,
        "line": 1,
        "description": f"a defect in {file}",
        "suggestion": None,
        "reviewer": reviewer,
    }


def _clean() -> dict[str, Any]:
    return {n: _review([]) for n in _NAMES}


def _request_changes() -> dict[str, Any]:
    # Two reviewers on one file: major consensus.
    reviews = _clean()
    for n in _NAMES[:2]:
        reviews[n] = _review([_major("a.py", n)])
    return reviews


def _unreviewed_major() -> dict[str, Any]:
    # One reviewer, no consensus: approve, with the formal approval withheld.
    reviews = _clean()
    reviews[_NAMES[0]] = _review([_major("a.py", _NAMES[0])])
    return reviews


def _quorum_short() -> dict[str, Any]:
    # Two of three clear MIN_REVIEWERS_FOR_VERDICT, so approve -- withheld.
    reviews: dict[str, Any] = _clean()
    reviews[_NAMES[-1]] = None
    return reviews


@pytest.fixture(autouse=True)
def _output_sink(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # main() appends the roster and review_result to $GITHUB_OUTPUT; on
    # Actions that is the live step's file, so keep it in tmp.
    monkeypatch.setenv("GITHUB_OUTPUT", str(tmp_path / "gh-output"))
    # Partial reviewers append a table to the job summary; empty skips it.
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", "")


def _run(
    monkeypatch: pytest.MonkeyPatch,
    reviews: dict[str, Any],
    flag: str | None,
    *,
    posted: bool = True,
    mode: str = "parallel",
) -> tuple[int | None, Any]:
    monkeypatch.setenv("PR_NUMBER", "42")
    monkeypatch.setenv("GITHUB_REPOSITORY", "owner/repo")
    monkeypatch.setenv("REVIEW_MODE", mode)
    for name in _NAMES:
        monkeypatch.setenv(f"REVIEWER_RESULT_{name.upper()}", "success")
    if flag is None:
        monkeypatch.delenv("BLOCK_MERGE_ON_VERDICT", raising=False)
    else:
        monkeypatch.setenv("BLOCK_MERGE_ON_VERDICT", flag)
    code: int | None = None
    with (
        patch("aggregate_reviews.load_reviews", return_value=reviews),
        patch("aggregate_reviews.post_verdict", return_value=posted) as post,
        patch("aggregate_reviews.fetch_round_count", return_value=0),
        patch("aggregate_reviews._fetch_fold_comments", return_value=[]),
        patch("aggregate_reviews._fetch_inline_posts", return_value=[]),
        # AT-2843's thread reader, landing alongside this; create=True so
        # this file holds on either side of that merge.
        patch(
            "aggregate_reviews._fetch_disposition_threads", return_value=[], create=True
        ),
    ):
        try:
            main()
        except SystemExit as exc:
            code = exc.code if isinstance(exc.code, int) else 1
    return code, post


@pytest.mark.parametrize(
    "reviews,verdict",
    [
        pytest.param(_request_changes(), "request_changes", id="request-changes"),
        pytest.param(_unreviewed_major(), "approve", id="approve-unreviewed-major"),
        pytest.param(_quorum_short(), "approve", id="approve-quorum-short"),
    ],
)
def test_flag_on_fails_after_posting_a_blocking_verdict(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    reviews: dict[str, Any],
    verdict: str,
) -> None:
    code, post = _run(monkeypatch, reviews, "true")
    assert code == 1
    # The gate's own exit, not another path that also exits 1.
    err = capsys.readouterr().err
    assert "BLOCK_MERGE_ON_VERDICT is on" in err
    assert "see the verdict comment" in err
    # Posted first: the red check has its explanation on the PR.
    post.assert_called_once()
    assert post.call_args.args[1] == verdict


def test_flag_on_still_fails_when_the_post_did_not_land_and_says_so(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # post_verdict returns False when the review timed out and its fallback
    # comment failed too: nothing on the PR explains the red check, so the
    # run log has to, rather than pointing at a comment that is not there.
    code, _ = _run(monkeypatch, _request_changes(), "true", posted=False)
    assert code == 1
    err = capsys.readouterr().err
    assert "the verdict was not posted" in err
    assert "see the verdict comment" not in err


@pytest.mark.parametrize(
    "reviews",
    [
        pytest.param(_request_changes(), id="request-changes"),
        pytest.param(_unreviewed_major(), id="approve-unreviewed-major"),
        pytest.param(_quorum_short(), id="approve-quorum-short"),
    ],
)
@pytest.mark.parametrize("flag", [None, "false", "", "yes"])
def test_flag_off_keeps_the_job_green(
    monkeypatch: pytest.MonkeyPatch, reviews: dict[str, Any], flag: str | None
) -> None:
    code, post = _run(monkeypatch, reviews, flag)
    assert code is None
    post.assert_called_once()


def _sequential_early_exit(issues: list[dict[str, Any]]) -> dict[str, Any]:
    # One reviewer ended the sequence early; the rest never ran.
    reviews: dict[str, Any] = {n: None for n in _NAMES}
    reviews[_NAMES[0]] = {**_review(issues), "status": "early_exit"}
    return reviews


def test_flag_on_leaves_a_sequential_early_exit_green(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The early exit is sub-quorum by design (sequential_bypass): the short
    # roster is not a withhold the flag acts on.
    code, post = _run(
        monkeypatch, _sequential_early_exit([]), "true", mode="sequential"
    )
    assert code is None
    assert post.call_args.args[1] == "approve"


def test_flag_on_still_fails_a_sequential_early_exit_with_a_major(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reviews = _sequential_early_exit([_major("a.py", _NAMES[0])])
    code, post = _run(monkeypatch, reviews, "true", mode="sequential")
    assert code == 1
    assert post.call_args.args[1] == "approve"


def test_flag_on_leaves_a_downgraded_changes_requested_green(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Two of three partial (an error alongside their payload, so still
    # available): request_changes is downgraded to "comment" -- a single
    # full survivor is not enough signal to block, flag or no flag.
    reviews = _request_changes()
    for n in _NAMES[:2]:
        reviews[n] = {**reviews[n], "error": "truncated"}
    code, post = _run(monkeypatch, reviews, "true")
    assert code is None
    assert post.call_args.args[1] == "comment"


@pytest.mark.parametrize("flag", ["true", "TRUE", "True"])
def test_flag_on_leaves_a_clean_approval_green(
    monkeypatch: pytest.MonkeyPatch, flag: str
) -> None:
    code, post = _run(monkeypatch, _clean(), flag)
    assert code is None
    assert post.call_args.args[1] == "approve"


def test_aggregate_workflow_wires_the_flag_default_false() -> None:
    text = (WORKFLOWS / "base-ai-review-aggregate.yml").read_text(encoding="utf-8")
    assert re.search(
        r"^\s+BLOCK_MERGE_ON_VERDICT: \$\{\{ vars\.BLOCK_MERGE_ON_VERDICT \|\| 'false' \}\}$",
        text,
        re.MULTILINE,
    )
