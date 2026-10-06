"""The fold count, from the fold comment to the merge gate (AT-2553).

From round ROUND_CUTOFF_N on, post_inline_comments.py folds a reviewer's
minor/suggestion findings into one "Round Cutoff Summary" comment instead
of inline threads, and the inline fallback folds the same way when no
inline post could be made. A folded finding is not a review thread, and
the merge gate counts unresolved threads: on PR #179 round 7 it read 0
threads, 10 green checks and "Approved | minor/suggestion only", and the
PR merged with two findings sitting undispositioned in the summary body.

The count travels in the fold comment itself -- written by the posting
step under BOT_LOGIN, read back by the aggregate by author and marker --
because that is the one carrier the PR cannot author. The verdict file is
the reviewer's own JSON written over PR content, and a count recorded
there can be planted there, which is what the first design did and what
``test_a_planted_record_in_the_verdict_file_is_irrelevant`` pins.

These tests drive the real seam: post_inline_comments.main() posts to a
fake PR (``_PR``), and aggregate_reviews.main() reads that fake PR back the
way it reads the real one. The gh CLI is stubbed; the files are real.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest

import aggregate_reviews
import post_inline_comments
from github_pr_support import (
    REVIEWER_NAMES,
    fold_count_line,
    fold_markers_for,
    parse_fold_count,
)

# Three right-side lines in a.py, so up to three in-range findings.
_DIFF = "+++ b/a.py\n@@ -1,3 +1,3 @@\n+line1\n+line2\n+line3\n"
# Rounds already completed; the round in progress is one more, past the
# default cutoff of 5.
_PAST_CUTOFF = 6
_BELOW_CUTOFF = 0
_HEAD = "0123456789abcdef0123456789abcdef01234567"
_OLD_HEAD = "fedcba9876543210fedcba9876543210fedcba98"
# What the posting step and the aggregate both run as on Actions.
_BOT = "github-actions[bot]"


def _issue(line: int, description: str, severity: str = "minor") -> dict[str, Any]:
    return {
        "severity": severity,
        "file": "a.py",
        "line": line,
        "description": description,
        "suggestion": None,
    }


# Distinct wording, so batch-internal dedup keeps both.
_TWO_MINORS = [
    _issue(1, "the loader never closes the handle it opens"),
    _issue(2, "a stale cache key survives the rename"),
]


class _PR:
    """The surfaces of a PR both scripts touch, held as data.

    ``comments`` is what ``gh pr comment`` appended -- the fold comments --
    and ``inline`` what the Reviews API received, one entry per inline
    comment with the commit it was posted against. Both are read back by
    the aggregate's fetchers, stubbed to serve from here.
    """

    def __init__(self) -> None:
        self.comments: list[dict[str, str]] = []
        self.inline: list[dict[str, Any]] = []
        self.fallbacks: list[list[dict[str, Any]]] = []

    def add_comment(self, body: str, login: str = _BOT) -> None:
        self.comments.append({"login": login, "body": body})

    def fold_comment_bodies(self) -> list[str]:
        return [
            c["body"]
            for c in self.comments
            if "round-cutoff-" in c["body"] or "inline-fallback-" in c["body"]
        ]


def _write_review(work: Path, name: str, issues: list[dict[str, Any]]) -> Path:
    path = work / f"review-{name}.json"
    review = {
        "summary": f"{name} review",
        "status": "ok",
        "early_exit": False,
        "issues": issues,
    }
    path.write_text(json.dumps(review), encoding="utf-8")
    return path


def _read_review(work: Path, name: str) -> dict[str, Any]:
    return json.loads((work / f"review-{name}.json").read_text(encoding="utf-8"))


def _run_post(
    monkeypatch: pytest.MonkeyPatch,
    work: Path,
    pr: _PR,
    name: str,
    *,
    completed_rounds: int,
    head_sha_fails: bool = False,
    inline_fails: bool = False,
) -> dict[str, Any]:
    """Run post_inline_comments.main() for one reviewer against ``pr``.

    ``head_sha_fails`` / ``inline_fails`` drive the two routes to
    ``post_fallback``, which puts every finding in one PR comment instead of
    inline threads -- the second way findings leave the thread list.
    """
    posted: dict[str, Any] = {"inline": [], "folded": [], "fallback": []}
    (work / "pr.diff").write_text(_DIFF, encoding="utf-8")
    monkeypatch.setenv("PR_NUMBER", "179")
    monkeypatch.setenv("GITHUB_REPOSITORY", "o/r")
    monkeypatch.setattr(
        post_inline_comments, "fetch_existing_threads", lambda repo, pr: []
    )
    monkeypatch.setattr(
        post_inline_comments, "fetch_round_count", lambda repo, pr: completed_rounds
    )

    def head_sha(pr_number: str) -> str:
        if head_sha_fails:
            raise subprocess.CalledProcessError(1, ["gh", "pr", "view"])
        return _HEAD

    monkeypatch.setattr(post_inline_comments, "get_pr_head_sha", head_sha)

    def fake_gh(cmd: list[str], **kwargs: Any) -> SimpleNamespace:
        if cmd[:2] == ["gh", "api"]:
            # Marker-dedup check in _post_folded_comment, against the fake PR.
            marker = cmd[cmd.index("--arg") + 2]
            n = sum(1 for c in pr.comments if marker in c["body"])
            return SimpleNamespace(returncode=0, stdout=str(n), stderr="")
        if cmd[:3] == ["gh", "pr", "comment"]:
            pr.add_comment(kwargs["input"])
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        raise AssertionError(f"unexpected gh call: {cmd}")

    monkeypatch.setattr(subprocess, "run", fake_gh)

    def record_inline(
        repo: str, pr_number: str, sha: str, reviewer: str, comments: list
    ) -> bool:
        if inline_fails:
            return False
        posted["inline"] = comments
        for c in comments:
            pr.inline.append({"login": _BOT, "commits": {sha}, "body": c["body"]})
        return True

    real_summary = post_inline_comments.post_cutoff_summary
    real_fallback = post_inline_comments.post_fallback

    def record_summary(*args: Any, **kwargs: Any) -> None:
        posted["folded"] = args[4]
        real_summary(*args, **kwargs)

    def record_fallback(*args: Any, **kwargs: Any) -> None:
        posted["fallback"] = args[4]
        real_fallback(*args, **kwargs)

    monkeypatch.setattr(post_inline_comments, "post_inline_review", record_inline)
    monkeypatch.setattr(post_inline_comments, "post_cutoff_summary", record_summary)
    monkeypatch.setattr(post_inline_comments, "post_fallback", record_fallback)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "post_inline_comments.py",
            "--issues",
            str(work / f"review-{name}.json"),
            "--diff",
            str(work / "pr.diff"),
            "--reviewer",
            name,
        ],
    )
    post_inline_comments.main()
    return posted


def _run_aggregate(
    monkeypatch: pytest.MonkeyPatch,
    work: Path,
    pr: _PR,
    *,
    completed_rounds: int,
    head_sha: str = _HEAD,
    bot_login: str = _BOT,
) -> tuple[dict[str, Any], str]:
    """Run aggregate_reviews.main() over the verdict files, reading ``pr``.

    Returns what it wrote to $GITHUB_OUTPUT (the roster parsed) and the
    headline line of the verdict it would have posted.
    """
    output_path = work / "gh-output"
    monkeypatch.chdir(work)
    monkeypatch.setenv("GITHUB_OUTPUT", str(output_path))
    monkeypatch.setenv("PR_NUMBER", "179")
    monkeypatch.setenv("GITHUB_REPOSITORY", "o/r")
    monkeypatch.setenv("HEAD_SHA", head_sha)
    for name in REVIEWER_NAMES:
        monkeypatch.setenv(f"REVIEWER_RESULT_{name.upper()}", "success")
    monkeypatch.setattr(aggregate_reviews, "BOT_LOGIN", bot_login)
    monkeypatch.setattr(
        aggregate_reviews, "fetch_round_count", lambda repo, pr_number: completed_rounds
    )
    monkeypatch.setattr(
        aggregate_reviews,
        "_fetch_fold_comments",
        lambda repo, pr_number: list(pr.comments),
    )
    monkeypatch.setattr(
        aggregate_reviews,
        "_fetch_inline_posts",
        lambda repo, pr_number: list(pr.inline),
    )
    # _head_is_stale compares HEAD_SHA with the live head; the fake PR's is HEAD_SHA.
    monkeypatch.setattr(aggregate_reviews, "_head_is_stale", lambda: False)
    with patch("aggregate_reviews.post_verdict") as post_verdict:
        aggregate_reviews.main()
    comment = post_verdict.call_args.args[0]
    headline = next(
        line for line in comment.splitlines() if line.startswith("**Result:")
    )
    emitted = dict(
        line.split("=", 1)
        for line in output_path.read_text(encoding="utf-8").splitlines()
    )
    return {**emitted, "roster": json.loads(emitted["reviewer_roster"])}, headline


def _round(
    monkeypatch: pytest.MonkeyPatch,
    work: Path,
    pr: _PR,
    claude_issues: list[dict[str, Any]],
    *,
    completed_rounds: int,
    skip: tuple[str, ...] = (),
) -> dict[str, Any]:
    """One reviewer with findings, two without, each through the post step.

    ``skip`` names reviewers whose posting step never runs -- the step died
    -- so their verdict file is on disk and nothing of theirs is on the PR.
    """
    for name in REVIEWER_NAMES:
        _write_review(work, name, claude_issues if name == "claude" else [])
    posted = {}
    for name in REVIEWER_NAMES:
        if name in skip:
            continue
        posted[name] = _run_post(
            monkeypatch, work, pr, name, completed_rounds=completed_rounds
        )
    return posted


class TestTheFoldCommentCarriesTheCount:
    """post_inline_comments.py's half: what the fold comment says."""

    def test_the_cutoff_summary_carries_its_count_and_the_head(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        pr = _PR()
        _write_review(tmp_path, "claude", _TWO_MINORS)
        posted = _run_post(
            monkeypatch, tmp_path, pr, "claude", completed_rounds=_PAST_CUTOFF
        )
        assert len(posted["folded"]) == 2 and posted["inline"] == []
        [body] = pr.fold_comment_bodies()
        assert fold_markers_for("claude", _PAST_CUTOFF + 1)[0] in body
        assert parse_fold_count(body) == (2, _HEAD)

    def test_the_count_is_what_was_folded_not_what_was_reported(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # Three findings, one of them off the diff: two reach the summary.
        pr = _PR()
        issues = [*_TWO_MINORS, _issue(99, "a finding the diff does not carry")]
        _write_review(tmp_path, "claude", issues)
        posted = _run_post(
            monkeypatch, tmp_path, pr, "claude", completed_rounds=_PAST_CUTOFF
        )
        assert len(posted["folded"]) == 2
        [body] = pr.fold_comment_bodies()
        assert parse_fold_count(body) == (2, _HEAD)

    @pytest.mark.parametrize(
        ("route", "head"),
        [({"head_sha_fails": True}, None), ({"inline_fails": True}, _HEAD)],
    )
    def test_the_inline_fallback_is_a_fold_bound_to_the_round(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        route: dict[str, bool],
        head: str | None,
    ) -> None:
        # The round cutoff is not the only route off the thread list: when
        # no inline post can be made, post_fallback puts every finding in ONE
        # PR comment. It carries the count like the summary does, the head
        # when the step learned it, and a marker bound to the ROUND -- a
        # marker without the round let the first fallback on a PR suppress
        # every later one through the rerun dedup (AT-2553, round 9).
        pr = _PR()
        _write_review(tmp_path, "claude", _TWO_MINORS)
        posted = _run_post(
            monkeypatch, tmp_path, pr, "claude", completed_rounds=_BELOW_CUTOFF, **route
        )
        assert len(posted["fallback"]) == 2 and posted["folded"] == []
        [body] = pr.fold_comment_bodies()
        assert fold_markers_for("claude", _BELOW_CUTOFF + 1)[1] in body
        assert parse_fold_count(body) == (2, head)

    def test_a_second_fallback_round_posts_its_own_comment(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # The round-9 defect itself: with the old reviewer-only marker the
        # second round's fallback found the first round's comment and
        # posted nothing.
        pr = _PR()
        _write_review(tmp_path, "claude", _TWO_MINORS)
        _run_post(
            monkeypatch, tmp_path, pr, "claude", completed_rounds=0, inline_fails=True
        )
        _run_post(
            monkeypatch, tmp_path, pr, "claude", completed_rounds=1, inline_fails=True
        )
        bodies = pr.fold_comment_bodies()
        assert len(bodies) == 2
        assert fold_markers_for("claude", 1)[1] in bodies[0]
        assert fold_markers_for("claude", 2)[1] in bodies[1]

    def test_a_rerun_of_the_same_round_does_not_post_twice(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        pr = _PR()
        _write_review(tmp_path, "claude", _TWO_MINORS)
        _run_post(monkeypatch, tmp_path, pr, "claude", completed_rounds=_PAST_CUTOFF)
        _run_post(monkeypatch, tmp_path, pr, "claude", completed_rounds=_PAST_CUTOFF)
        assert len(pr.fold_comment_bodies()) == 1

    def test_a_posted_inline_batch_leaves_no_fold_comment(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        pr = _PR()
        _write_review(tmp_path, "claude", _TWO_MINORS)
        posted = _run_post(
            monkeypatch, tmp_path, pr, "claude", completed_rounds=_BELOW_CUTOFF
        )
        assert len(posted["inline"]) == 2
        assert pr.fold_comment_bodies() == []

    def test_a_major_past_the_cutoff_posts_inline(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        pr = _PR()
        issues = [*_TWO_MINORS, _issue(3, "credentials are logged", severity="major")]
        _write_review(tmp_path, "claude", issues)
        posted = _run_post(
            monkeypatch, tmp_path, pr, "claude", completed_rounds=_PAST_CUTOFF
        )
        assert len(posted["inline"]) == 3 and posted["folded"] == []
        assert pr.fold_comment_bodies() == []

    def test_the_verdict_file_is_never_written(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # The carrier is the PR. The file is the reviewer's, and the step
        # leaves it byte-for-byte as the reviewer wrote it on every route.
        pr = _PR()
        path = _write_review(tmp_path, "claude", _TWO_MINORS)
        before = path.read_bytes()
        _run_post(monkeypatch, tmp_path, pr, "claude", completed_rounds=_PAST_CUTOFF)
        _run_post(
            monkeypatch, tmp_path, pr, "claude", completed_rounds=0, inline_fails=True
        )
        _run_post(monkeypatch, tmp_path, pr, "claude", completed_rounds=1)
        assert path.read_bytes() == before
        assert sorted(p.name for p in tmp_path.iterdir()) == [
            "pr.diff",
            "review-claude.json",
        ]


class TestTheAggregateReadsTheCount:
    """aggregate_reviews.py's half: the roster, the output and the headline."""

    def test_a_fold_reaches_the_output_the_roster_and_the_headline(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        pr = _PR()
        posted = _round(
            monkeypatch, tmp_path, pr, _TWO_MINORS, completed_rounds=_PAST_CUTOFF
        )
        assert len(posted["claude"]["folded"]) == 2
        out, headline = _run_aggregate(
            monkeypatch, tmp_path, pr, completed_rounds=_PAST_CUTOFF
        )
        assert out["folded_findings_count"] == "2"
        assert out["roster"]["folded"] == {"claude": 2, "codex": 0, "gemini": 0}
        # Still Approved -- the verdict word is not this ticket's to change
        # -- but no longer a plain one.
        assert headline.startswith("**Result: [OK] Approved")
        assert "2 finding(s) folded into comments (claude 2)" in headline

    def test_the_fallback_fold_is_read_the_same_way(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        pr = _PR()
        for name in REVIEWER_NAMES:
            _write_review(tmp_path, name, _TWO_MINORS if name == "claude" else [])
        _run_post(
            monkeypatch,
            tmp_path,
            pr,
            "claude",
            completed_rounds=_BELOW_CUTOFF,
            inline_fails=True,
        )
        for name in ("codex", "gemini"):
            _run_post(monkeypatch, tmp_path, pr, name, completed_rounds=_BELOW_CUTOFF)
        out, headline = _run_aggregate(
            monkeypatch, tmp_path, pr, completed_rounds=_BELOW_CUTOFF
        )
        assert out["folded_findings_count"] == "2"
        assert "2 finding(s) folded into comments (claude 2)" in headline

    def test_the_count_is_the_sum_over_responded_reviewers(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        pr = _PR()
        for name in REVIEWER_NAMES:
            _write_review(tmp_path, name, _TWO_MINORS)
        for name in REVIEWER_NAMES:
            _run_post(monkeypatch, tmp_path, pr, name, completed_rounds=_PAST_CUTOFF)
        out, headline = _run_aggregate(
            monkeypatch, tmp_path, pr, completed_rounds=_PAST_CUTOFF
        )
        assert out["folded_findings_count"] == "6"
        assert (
            "6 finding(s) folded into comments (claude 2, codex 2, gemini 2)"
            in headline
        )

    def test_an_inline_batch_on_this_head_reads_as_zero(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # The zero is derived from the reviewer's own inline comments under
        # BOT_LOGIN on this head -- content the PR cannot author -- not from
        # anything in the verdict file.
        pr = _PR()
        posted = _round(
            monkeypatch, tmp_path, pr, _TWO_MINORS, completed_rounds=_BELOW_CUTOFF
        )
        assert len(posted["claude"]["inline"]) == 2
        out, headline = _run_aggregate(
            monkeypatch, tmp_path, pr, completed_rounds=_BELOW_CUTOFF
        )
        assert out["folded_findings_count"] == "0"
        assert out["roster"]["folded"] == {name: 0 for name in REVIEWER_NAMES}
        assert "folded" not in headline and "unknown" not in headline

    def test_a_major_past_the_cutoff_reads_as_zero(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        pr = _PR()
        issues = [*_TWO_MINORS, _issue(3, "credentials are logged", severity="major")]
        posted = _round(
            monkeypatch, tmp_path, pr, issues, completed_rounds=_PAST_CUTOFF
        )
        assert len(posted["claude"]["inline"]) == 3
        out, headline = _run_aggregate(
            monkeypatch, tmp_path, pr, completed_rounds=_PAST_CUTOFF
        )
        assert out["folded_findings_count"] == "0"
        assert "folded" not in headline

    def test_an_empty_payload_reads_as_zero_without_touching_the_pr(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # Nothing to fold, so nothing to read: the fetchers are not called.
        pr = _PR()
        _round(monkeypatch, tmp_path, pr, [], completed_rounds=_PAST_CUTOFF)

        def never(*args: Any) -> list:
            raise AssertionError(
                "the PR must not be read when no reviewer has findings"
            )

        out, _ = _run_aggregate(
            monkeypatch, tmp_path, pr, completed_rounds=_PAST_CUTOFF
        )
        monkeypatch.setattr(aggregate_reviews, "_fetch_fold_comments", never)
        monkeypatch.setattr(aggregate_reviews, "_fetch_inline_posts", never)
        out, _ = _run_aggregate(
            monkeypatch, tmp_path, pr, completed_rounds=_PAST_CUTOFF
        )
        assert out["folded_findings_count"] == "0"

    def test_a_reviewer_with_findings_and_nothing_on_the_pr_is_unknown(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # claude's posting step died: its verdict reports two findings and
        # the PR carries neither a fold comment nor an inline comment of
        # its own. The aggregate cannot tell that from "nothing folded" and
        # says unknown -- empty, the way an absent roster reads -- never 0.
        pr = _PR()
        _round(
            monkeypatch,
            tmp_path,
            pr,
            _TWO_MINORS,
            completed_rounds=_PAST_CUTOFF,
            skip=("claude",),
        )
        out, headline = _run_aggregate(
            monkeypatch, tmp_path, pr, completed_rounds=_PAST_CUTOFF
        )
        assert out["folded_findings_count"] == ""
        assert out["roster"]["folded"] == {"claude": None, "codex": 0, "gemini": 0}
        assert (
            "fold count unknown (claude: 2 finding(s) reported, none on this head"
            in headline
        )
        assert "folded into" not in headline

    def test_a_planted_record_in_the_verdict_file_is_irrelevant(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # The reproduction for the round-4 major (PRRT_kwDOSu4BQs6pS7aT):
        # the verdict file arrives carrying the first design's record,
        # claiming nothing was folded, on a round that folds two. Under the
        # verdict-file carrier a run that could not overwrite the file
        # reported the planted 0 (red at 877502e, driver kept in the PR
        # body). Here the file is never read for the count: the
        # gate-visible value is the comment's.
        pr = _PR()
        for name in REVIEWER_NAMES:
            path = _write_review(
                tmp_path, name, _TWO_MINORS if name == "claude" else []
            )
            payload = json.loads(path.read_text(encoding="utf-8"))
            payload["fold"] = {"folded": 0, "round": _PAST_CUTOFF + 1}
            path.write_text(json.dumps(payload), encoding="utf-8")
        for name in REVIEWER_NAMES:
            _run_post(monkeypatch, tmp_path, pr, name, completed_rounds=_PAST_CUTOFF)
        out, headline = _run_aggregate(
            monkeypatch, tmp_path, pr, completed_rounds=_PAST_CUTOFF
        )
        assert out["folded_findings_count"] == "2"
        assert out["roster"]["folded"]["claude"] == 2
        assert "2 finding(s) folded into comments (claude 2)" in headline
        # And the file still carries the plant: nothing rewrote it.
        assert _read_review(tmp_path, "claude")["fold"] == {
            "folded": 0,
            "round": _PAST_CUTOFF + 1,
        }

    def test_a_failed_reviewers_comment_does_not_count(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # Keyed on the same `available` the roster's `responded` is derived
        # from: a reviewer the roster lists as missing contributes nothing,
        # whatever it posted.
        pr = _PR()
        _round(monkeypatch, tmp_path, pr, _TWO_MINORS, completed_rounds=_PAST_CUTOFF)
        failed = _read_review(tmp_path, "claude")
        failed["status"] = "failed"
        (tmp_path / "review-claude.json").write_text(
            json.dumps(failed), encoding="utf-8"
        )
        out, _ = _run_aggregate(
            monkeypatch, tmp_path, pr, completed_rounds=_PAST_CUTOFF
        )
        assert "claude" in out["roster"]["missing"]
        assert out["roster"]["folded"] == {"codex": 0, "gemini": 0}
        assert out["folded_findings_count"] == "0"

    def test_a_wrong_count_line_disagrees_with_the_fold(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # Inversion control. The assertion that the output equals the fold
        # is only evidence if a wrong count would have failed it: make the
        # comment under-count by one and the gate-visible value disagrees
        # with what was actually folded; put the real line back and it
        # agrees.
        real_line = post_inline_comments.fold_count_line

        def under_count(count: int, head: str | None = None) -> str:
            return real_line(max(count - 1, 0), head)

        monkeypatch.setattr(post_inline_comments, "fold_count_line", under_count)
        pr = _PR()
        posted = _round(
            monkeypatch, tmp_path, pr, _TWO_MINORS, completed_rounds=_PAST_CUTOFF
        )
        folded = len(posted["claude"]["folded"])
        assert folded == 2
        out, headline = _run_aggregate(
            monkeypatch, tmp_path, pr, completed_rounds=_PAST_CUTOFF
        )
        assert out["folded_findings_count"] != str(folded)
        assert f"{folded} finding(s) folded" not in headline

        monkeypatch.setattr(post_inline_comments, "fold_count_line", real_line)
        pr = _PR()
        posted = _round(
            monkeypatch, tmp_path, pr, _TWO_MINORS, completed_rounds=_PAST_CUTOFF
        )
        out, headline = _run_aggregate(
            monkeypatch, tmp_path, pr, completed_rounds=_PAST_CUTOFF
        )
        assert out["folded_findings_count"] == str(len(posted["claude"]["folded"]))
        assert f"{folded} finding(s) folded" in headline


class TestWhatTheAggregateRefusesToRead:
    """The trust boundary: BOT_LOGIN, the round, the head, the line itself."""

    def _folding_round(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, pr: _PR
    ) -> None:
        _round(monkeypatch, tmp_path, pr, _TWO_MINORS, completed_rounds=_PAST_CUTOFF)

    def test_a_fold_comment_by_anyone_else_is_ignored(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # The PR author can post a comment carrying the marker and any
        # count. Only BOT_LOGIN's are read. With the real one ignored too,
        # the reviewer reads as unknown, never as the author's number.
        pr = _PR()
        self._folding_round(monkeypatch, tmp_path, pr)
        [real] = pr.fold_comment_bodies()
        pr.comments.clear()
        pr.add_comment(real.replace("fold-count: 2", "fold-count: 0"), login="mallory")
        out, headline = _run_aggregate(
            monkeypatch, tmp_path, pr, completed_rounds=_PAST_CUTOFF
        )
        assert out["roster"]["folded"]["claude"] is None
        assert out["folded_findings_count"] == ""
        assert (
            "fold count unknown (claude: 2 finding(s) reported, none on this head"
            in headline
        )

    def test_bot_login_spellings_are_normalized(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # GraphQL reports the author as `github-actions`, REST and the
        # BOT_LOGIN default as `github-actions[bot]`; neither spelling may
        # decide whether the comment is read (AT-2208).
        pr = _PR()
        self._folding_round(monkeypatch, tmp_path, pr)
        [real] = pr.fold_comment_bodies()
        pr.comments.clear()
        pr.add_comment(real, login="github-actions")
        out, _ = _run_aggregate(
            monkeypatch, tmp_path, pr, completed_rounds=_PAST_CUTOFF
        )
        assert out["roster"]["folded"]["claude"] == 2

    def test_an_earlier_rounds_comment_is_ignored(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # The comment is bound to the round in its marker. Read one round
        # later, it is not this round's, and nothing else of claude's is on
        # the PR for this round: unknown.
        pr = _PR()
        self._folding_round(monkeypatch, tmp_path, pr)
        out, _ = _run_aggregate(
            monkeypatch, tmp_path, pr, completed_rounds=_PAST_CUTOFF + 1
        )
        assert out["roster"]["folded"]["claude"] is None

    def test_a_comment_from_a_superseded_head_is_ignored(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # Same round number -- no verdict landed in between -- but the
        # comment names the head it was posted on, and this run is about
        # another one.
        pr = _PR()
        self._folding_round(monkeypatch, tmp_path, pr)
        out, _ = _run_aggregate(
            monkeypatch, tmp_path, pr, completed_rounds=_PAST_CUTOFF, head_sha=_OLD_HEAD
        )
        assert out["roster"]["folded"]["claude"] is None

    def test_a_comment_without_a_head_binds_on_the_round_alone(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # The fallback after a failed head lookup cannot name the head; its
        # comment is still this round's.
        pr = _PR()
        for name in REVIEWER_NAMES:
            _write_review(tmp_path, name, _TWO_MINORS if name == "claude" else [])
        _run_post(
            monkeypatch,
            tmp_path,
            pr,
            "claude",
            completed_rounds=_BELOW_CUTOFF,
            head_sha_fails=True,
        )
        for name in ("codex", "gemini"):
            _run_post(monkeypatch, tmp_path, pr, name, completed_rounds=_BELOW_CUTOFF)
        [body] = pr.fold_comment_bodies()
        assert parse_fold_count(body) == (2, None)
        out, _ = _run_aggregate(
            monkeypatch, tmp_path, pr, completed_rounds=_BELOW_CUTOFF
        )
        assert out["roster"]["folded"]["claude"] == 2

    @pytest.mark.parametrize(
        "line",
        [
            "<!-- fold-count: -1 -->",
            "<!-- fold-count: true -->",
            "<!-- fold-count: two -->",
            "<!-- fold-count:  -->",
            "",
        ],
        ids=["negative", "bool", "word", "blank", "absent"],
    )
    def test_a_marker_without_a_readable_count_reads_as_unknown(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, line: str
    ) -> None:
        pr = _PR()
        self._folding_round(monkeypatch, tmp_path, pr)
        [real] = pr.fold_comment_bodies()
        pr.comments.clear()
        pr.add_comment(real.replace(fold_count_line(2, _HEAD), line))
        out, headline = _run_aggregate(
            monkeypatch, tmp_path, pr, completed_rounds=_PAST_CUTOFF
        )
        assert out["roster"]["folded"]["claude"] is None
        assert (
            "fold count unknown (claude: 2 finding(s) reported, none on this head"
            in headline
        )

    def test_an_inline_batch_on_another_head_does_not_make_a_zero(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # claude posted inline last round, on the previous head, and died
        # this round. Those threads are bot-authored and claude's, and they
        # are not evidence about this round.
        pr = _PR()
        _round(monkeypatch, tmp_path, pr, _TWO_MINORS, completed_rounds=_BELOW_CUTOFF)
        for post in pr.inline:
            post["commits"] = {_OLD_HEAD}
        out, headline = _run_aggregate(
            monkeypatch, tmp_path, pr, completed_rounds=_BELOW_CUTOFF
        )
        assert out["roster"]["folded"]["claude"] is None
        assert (
            "fold count unknown (claude: 2 finding(s) reported, none on this head"
            in headline
        )

    def test_an_inline_post_by_another_reviewer_does_not_make_claudes_zero(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # codex posted inline on this head; claude did not. The reviewer is
        # read from the anchored prefix of the comment the step writes,
        # not from any mention of a name in the LLM-authored description.
        pr = _PR()
        for name in REVIEWER_NAMES:
            _write_review(tmp_path, name, _TWO_MINORS if name != "gemini" else [])
        _run_post(monkeypatch, tmp_path, pr, "codex", completed_rounds=_BELOW_CUTOFF)
        _run_post(monkeypatch, tmp_path, pr, "gemini", completed_rounds=_BELOW_CUTOFF)
        pr.inline.append(
            {
                "login": _BOT,
                "commits": {_HEAD},
                "body": "- **minor** (codex): this line mentions (claude): by name",
            }
        )
        out, _ = _run_aggregate(
            monkeypatch, tmp_path, pr, completed_rounds=_BELOW_CUTOFF
        )
        assert out["roster"]["folded"] == {"claude": None, "codex": 0, "gemini": 0}

    def test_without_a_head_no_inline_zero_is_derived(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # No HEAD_SHA means the aggregate cannot bind an inline post to this
        # run; it does not guess from posts on any head.
        pr = _PR()
        _round(monkeypatch, tmp_path, pr, _TWO_MINORS, completed_rounds=_BELOW_CUTOFF)
        out, _ = _run_aggregate(
            monkeypatch, tmp_path, pr, completed_rounds=_BELOW_CUTOFF, head_sha=""
        )
        assert out["roster"]["folded"]["claude"] is None
