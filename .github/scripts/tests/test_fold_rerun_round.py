"""AT-3428: a re-run does not borrow an earlier attempt's posting evidence.

Measured on hyuk-hur/dev-dotfiles#254: run 42981508444 folded claude's
major into `inline-fallback-claude-r6` with five verdicts on the PR, and
attempt 1's aggregate, counting the same five, read round 6. The
aggregate-only attempt 2 reused the reviewer jobs but also counted attempt
1's own verdict, looked for round 7, and printed "fold count unknown".

Reading the earlier round on a re-run was tried and withdrawn (PR #292,
round 1): from the PR alone the aggregate cannot tell a reused reviewer job
from one that re-ran and whose posting step died, so the earlier attempt's
evidence would turn unknown coverage into a count. That keeps the AT-2553
decision in README ("the recovery aggregate reports unknown rather than
automatically carrying that older success"); re-running all jobs posts at
the new round. These cases pin it.
"""

from __future__ import annotations

from typing import Any

import pytest

import aggregate_reviews as aggregate
from github_pr_support import fold_count_line, inline_post_marker

_BOT = "github-actions[bot]"
_HEAD = "7532323d5764b2692a8446db682910c38f9c616e"
_RUN = "42981508444"


def _fold(round_number: int, head: str | None = _HEAD) -> dict[str, Any]:
    return {
        "login": _BOT,
        "author_type": "Bot",
        "body": f"<!-- inline-fallback-claude-r{round_number} -->\n"
        + fold_count_line(1, head)
        + "\n## [bot] claude Inline Review (fallback)\n\n- **a.py:1** -- x",
    }


def _inline(round_number: int, identity: str) -> dict[str, Any]:
    return {
        "login": _BOT,
        "author_type": "Bot",
        "posted_commit": _HEAD,
        "body": inline_post_marker("claude", round_number, identity)
        + "\n+ **major** (claude): finding",
    }


@pytest.fixture
def pr(monkeypatch: pytest.MonkeyPatch):
    # Attempt 2 counts six verdicts, attempt 1's among them: round 7.
    state: dict[str, Any] = {"folds": [], "inline": []}
    monkeypatch.setattr(aggregate, "INLINE_BOT_LOGIN", _BOT)
    monkeypatch.delenv("INLINE_REVIEW_RUN_ID", raising=False)
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.setenv("GITHUB_RUN_ID", _RUN)
    monkeypatch.setenv("GITHUB_RUN_ATTEMPT", "2")
    monkeypatch.setattr(aggregate, "fetch_round_count", lambda *a: 6)
    monkeypatch.setattr(aggregate, "_fetch_fold_comments", lambda *a: state["folds"])
    monkeypatch.setattr(aggregate, "_fetch_inline_posts", lambda *a: state["inline"])
    return state


def _folded() -> int | None:
    available = {"claude": {"issues": [{"severity": "major"}]}}
    return aggregate._folded_findings(available, "o/r", "254", _HEAD)["claude"]


@pytest.mark.parametrize("head", [_HEAD, None], ids=["with-head", "dotfiles-254"])
def test_earlier_rounds_fold_is_not_this_attempts(pr, head: str | None) -> None:
    pr["folds"] = [_fold(6, head)]
    assert _folded() is None


def test_earlier_attempts_inline_batch_is_not_this_attempts(pr) -> None:
    # A full re-run whose posting step died looks the same from the PR.
    pr["inline"] = [_inline(6, f"actions-{_RUN}-1")]
    assert _folded() is None


def test_a_full_re_run_posts_at_the_new_round(pr) -> None:
    pr["folds"] = [_fold(7)]
    assert _folded() == 1
    pr["folds"] = []
    pr["inline"] = [_inline(7, f"actions-{_RUN}-2")]
    assert _folded() == 0
