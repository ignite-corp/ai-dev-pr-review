"""Tests for post_inline_comments fuzzy dedup logic.

Covers the scenarios that the previous exact-match dedup missed,
including paraphrases that share token vocabulary, line-distance gating
on the same path, strong-similarity dedup across force-push line shifts,
and backward compatibility when no line is supplied.

Also covers the round-cutoff convergence backstop (RC-5): round counting,
the cutoff decision, and the fold into a single summary comment.
"""

from __future__ import annotations

import importlib
import json
import subprocess
import sys
from types import SimpleNamespace
from typing import Any

import post_inline_comments
from post_inline_comments import (
    _is_duplicate,
    _normalize_body,
    build_comments,
    fetch_round_count,
    post_cutoff_summary,
    round_cutoff_round,
)


def _thread(path: str, body: str, line: int | None = None) -> dict[str, Any]:
    """Build a thread dict matching fetch_existing_threads output."""
    return {"path": path, "line": line, "body": _normalize_body(body)}


def _issue(
    file: str, line: int, description: str, severity: str = "minor"
) -> dict[str, Any]:
    """Build an issue dict matching the review JSON schema."""
    return {
        "file": file,
        "line": line,
        "description": description,
        "severity": severity,
    }


class TestIsDuplicate:
    def test_exact_match_dedups(self) -> None:
        existing = [_thread("a.py", "Missing input validation on user_id", line=10)]
        assert _is_duplicate(
            "a.py",
            "Missing input validation on user_id",
            existing,
            line=10,
        )

    def test_reworded_dedups_at_low_threshold(self) -> None:
        # Two phrasings of the same concern share most tokens.
        existing = [
            _thread(
                "a.py",
                "Input validation missing for user_id parameter",
                line=10,
            )
        ]
        assert _is_duplicate(
            "a.py",
            "Missing input validation on user_id",
            existing,
            line=10,
            threshold=0.5,
        )

    def test_different_paths_no_dedup(self) -> None:
        existing = [_thread("a.py", "Missing input validation on user_id", line=10)]
        assert not _is_duplicate(
            "b.py",
            "Missing input validation on user_id",
            existing,
            line=10,
        )

    def test_identical_text_distant_lines_dedups(self) -> None:
        # Force-push/rebase shifts lines far beyond the window; identical
        # text (Jaccard 1.0 >= strong threshold) must dedup regardless.
        existing = [_thread("a.py", "Missing input validation on user_id", line=10)]
        assert _is_duplicate(
            "a.py",
            "Missing input validation on user_id",
            existing,
            line=16,  # delta 6, just past the +/-5 window
        )

    def test_identical_text_large_line_shift_dedups(self) -> None:
        # Real-world case: the same finding moved from line 271 to 162
        # (delta 109) after a force-push and was reposted 8 times.
        existing = [_thread("tf/sg.tf", "Egress open to 0.0.0.0/0", line=271)]
        assert _is_duplicate(
            "tf/sg.tf",
            "Egress open to 0.0.0.0/0",
            existing,
            line=162,
        )

    def test_moderate_similarity_distant_lines_no_dedup(self) -> None:
        # Jaccard 4/6 = 0.67 sits in the moderate band (0.6-0.8), which
        # still requires the line window -- distant lines must NOT dedup.
        existing = [_thread("a.py", "Missing input validation on user_id", line=10)]
        assert not _is_duplicate(
            "a.py",
            "Missing input validation user_id parameter",
            existing,
            line=100,
        )

    def test_moderate_similarity_within_window_dedups(self) -> None:
        # Same moderate-band pair (Jaccard 0.67) within +/-5 lines dedups.
        existing = [_thread("a.py", "Missing input validation on user_id", line=10)]
        assert _is_duplicate(
            "a.py",
            "Missing input validation user_id parameter",
            existing,
            line=12,
        )

    def test_identical_text_different_file_no_dedup(self) -> None:
        # The strong-similarity path skips the line window but never the
        # path check.
        existing = [_thread("a.py", "Missing input validation on user_id", line=10)]
        assert not _is_duplicate(
            "b.py",
            "Missing input validation on user_id",
            existing,
            line=200,
        )

    def test_empty_tokens_no_dedup(self) -> None:
        existing = [_thread("a.py", "Missing input validation on user_id", line=10)]
        # Body that normalises to no tokens must not dedup.
        assert not _is_duplicate("a.py", "   ", existing, line=10)

    def test_resolved_thread_still_dedups(self) -> None:
        # Threads are passed in with no isResolved field at this layer --
        # presence in `existing_threads` is enough to suppress the new post.
        # This guards against regressions where the caller starts filtering
        # by status before passing in.
        existing = [_thread("a.py", "Missing input validation on user_id", line=10)]
        assert _is_duplicate(
            "a.py",
            "Missing input validation on user_id",
            existing,
            line=10,
        )

    def test_no_line_param_backward_compat(self) -> None:
        # Old callers that don't pass `line` still get content-based dedup.
        existing = [_thread("a.py", "Missing input validation on user_id", line=10)]
        assert _is_duplicate(
            "a.py",
            "Missing input validation on user_id",
            existing,
        )

    def test_none_description_does_not_raise(self) -> None:
        # An issue payload with description=None must not crash dedup.
        existing = [_thread("a.py", "Missing input validation on user_id", line=10)]
        assert not _is_duplicate("a.py", None, existing, line=10)

    def test_none_thread_body_does_not_raise(self) -> None:
        # A thread fetched with body=None (explicit null) must not crash dedup.
        existing = [{"path": "a.py", "line": 10, "body": None}]
        assert not _is_duplicate(
            "a.py",
            "Missing input validation on user_id",
            existing,
            line=10,
        )


class TestBuildCommentsBatchDedup:
    """Batch-internal dedup (RC-4): one reviewer emitting the same finding
    twice in a single round must post once, at the highest severity."""

    VALID_LINES = {"a.py": set(range(1, 100)), "b.py": set(range(1, 100))}

    def test_same_finding_two_severities_keeps_higher_when_higher_first(self) -> None:
        issues = [
            _issue("a.py", 10, "Missing input validation on user_id", "major"),
            _issue("a.py", 12, "Missing input validation on user_id", "minor"),
        ]
        comments, no_location, out_of_range = build_comments(
            issues, self.VALID_LINES, [], "claude"
        )
        assert len(comments) == 1
        assert "**major**" in comments[0]["body"]
        assert comments[0]["line"] == 10
        assert (no_location, out_of_range) == (0, 0)

    def test_same_finding_two_severities_replaces_with_higher(self) -> None:
        issues = [
            _issue("a.py", 10, "Missing input validation on user_id", "minor"),
            _issue("a.py", 12, "Missing input validation on user_id", "critical"),
        ]
        comments, _, _ = build_comments(issues, self.VALID_LINES, [], "claude")
        assert len(comments) == 1
        assert "**critical**" in comments[0]["body"]
        assert comments[0]["line"] == 12

    def test_equal_severity_duplicate_keeps_first(self) -> None:
        issues = [
            _issue("a.py", 10, "Missing input validation on user_id", "minor"),
            _issue("a.py", 12, "Missing input validation on user_id", "minor"),
        ]
        comments, _, _ = build_comments(issues, self.VALID_LINES, [], "claude")
        assert len(comments) == 1
        assert comments[0]["line"] == 10

    def test_distinct_findings_both_posted(self) -> None:
        issues = [
            _issue("a.py", 10, "Missing input validation on user_id", "major"),
            _issue("a.py", 50, "Hardcoded credentials in config loader", "major"),
        ]
        comments, _, _ = build_comments(issues, self.VALID_LINES, [], "claude")
        assert len(comments) == 2

    def test_same_finding_different_files_both_posted(self) -> None:
        issues = [
            _issue("a.py", 10, "Missing input validation on user_id", "major"),
            _issue("b.py", 10, "Missing input validation on user_id", "major"),
        ]
        comments, _, _ = build_comments(issues, self.VALID_LINES, [], "claude")
        assert len(comments) == 2

    def test_existing_thread_dedup_still_applies(self) -> None:
        existing = [_thread("a.py", "Missing input validation on user_id", line=10)]
        issues = [
            _issue("a.py", 10, "Missing input validation on user_id", "critical"),
        ]
        comments, _, _ = build_comments(issues, self.VALID_LINES, existing, "claude")
        assert comments == []


def test_jaccard_threshold_env_override(monkeypatch: Any) -> None:
    """Setting JACCARD_THRESHOLD via env should make dedup stricter or looser."""
    # Force a fresh import with the env set
    monkeypatch.setenv("JACCARD_THRESHOLD", "0.95")
    sys.modules.pop("post_inline_comments", None)
    mod = importlib.import_module("post_inline_comments")
    # The module-level _JACCARD_THRESHOLD should reflect the env value
    assert getattr(mod, "_JACCARD_THRESHOLD", None) == 0.95


def test_strong_jaccard_env_override(monkeypatch: Any) -> None:
    """Setting DEDUP_STRONG_JACCARD via env should tune the strong threshold."""
    # Force a fresh import with the env set
    monkeypatch.setenv("DEDUP_STRONG_JACCARD", "0.9")
    sys.modules.pop("post_inline_comments", None)
    mod = importlib.import_module("post_inline_comments")
    # The module-level DEDUP_STRONG_JACCARD should reflect the env value
    assert getattr(mod, "DEDUP_STRONG_JACCARD", None) == 0.9


def _cutoff_issue(**overrides: Any) -> dict[str, Any]:
    """Create a minimal issue payload as found in review-<name>.json."""
    base: dict[str, Any] = {
        "severity": "minor",
        "file": "a.py",
        "line": 2,
        "description": "minor finding",
        "suggestion": None,
    }
    base.update(overrides)
    return base


class TestRoundCutoffRound:
    """Decision logic: enabled flag, severity gate, round threshold."""

    def _patch_round(self, monkeypatch: Any, completed: int) -> None:
        monkeypatch.setattr(
            post_inline_comments, "fetch_round_count", lambda repo, pr: completed
        )

    def test_below_cutoff_returns_none(self, monkeypatch: Any) -> None:
        # 3 completed rounds -> current round 4 < default 5: normal posting.
        self._patch_round(monkeypatch, 3)
        assert round_cutoff_round("o/r", "1", [_cutoff_issue()]) is None

    def test_minor_only_at_cutoff_returns_round(self, monkeypatch: Any) -> None:
        # 4 completed rounds -> current round 5 >= default 5: cutoff.
        self._patch_round(monkeypatch, 4)
        issues = [_cutoff_issue(), _cutoff_issue(severity="suggestion", file="b.py")]
        assert round_cutoff_round("o/r", "1", issues) == 5

    def test_major_present_returns_none(self, monkeypatch: Any) -> None:
        self._patch_round(monkeypatch, 9)
        issues = [_cutoff_issue(), _cutoff_issue(severity="major")]
        assert round_cutoff_round("o/r", "1", issues) is None

    def test_critical_present_returns_none(self, monkeypatch: Any) -> None:
        self._patch_round(monkeypatch, 9)
        assert (
            round_cutoff_round("o/r", "1", [_cutoff_issue(severity="critical")]) is None
        )

    def test_unknown_severity_fails_open(self, monkeypatch: Any) -> None:
        # Aliases like "high" are normalized only in the aggregate stage;
        # here anything outside minor/suggestion must block the cutoff.
        self._patch_round(monkeypatch, 9)
        assert round_cutoff_round("o/r", "1", [_cutoff_issue(severity="high")]) is None

    def test_env_override_lowers_cutoff(self, monkeypatch: Any) -> None:
        monkeypatch.setenv("ROUND_CUTOFF_N", "2")
        self._patch_round(monkeypatch, 1)  # current round 2
        assert round_cutoff_round("o/r", "1", [_cutoff_issue()]) == 2

    def test_env_override_raises_cutoff(self, monkeypatch: Any) -> None:
        monkeypatch.setenv("ROUND_CUTOFF_N", "10")
        self._patch_round(monkeypatch, 8)  # current round 9 < 10
        assert round_cutoff_round("o/r", "1", [_cutoff_issue()]) is None

    def test_invalid_env_uses_default(self, monkeypatch: Any) -> None:
        monkeypatch.setenv("ROUND_CUTOFF_N", "not-a-number")
        self._patch_round(monkeypatch, 4)  # current round 5 >= default 5
        assert round_cutoff_round("o/r", "1", [_cutoff_issue()]) == 5

    def test_disabled_via_env(self, monkeypatch: Any) -> None:
        monkeypatch.setenv("ROUND_CUTOFF_ENABLED", "false")
        self._patch_round(monkeypatch, 99)
        assert round_cutoff_round("o/r", "1", [_cutoff_issue()]) is None


class TestFetchRoundCount:
    def test_sums_pages_across_endpoints(self, monkeypatch: Any) -> None:
        # --paginate emits one jq count per page; both endpoints contribute.
        outputs = iter(["2\n1\n", "3\n"])
        calls: list[list[str]] = []

        def fake_run(cmd: list[str], **kwargs: Any) -> SimpleNamespace:
            calls.append(cmd)
            return SimpleNamespace(returncode=0, stdout=next(outputs), stderr="")

        monkeypatch.setattr(subprocess, "run", fake_run)
        assert fetch_round_count("o/r", "7") == 6
        assert len(calls) == 2
        assert "repos/o/r/pulls/7/reviews" in calls[0]
        assert "repos/o/r/issues/7/comments" in calls[1]

    def test_filters_on_bot_and_marker(self, monkeypatch: Any) -> None:
        captured: list[str] = []

        def fake_run(cmd: list[str], **kwargs: Any) -> SimpleNamespace:
            captured.append(cmd[cmd.index("--jq") + 1])
            return SimpleNamespace(returncode=0, stdout="0\n", stderr="")

        monkeypatch.setattr(subprocess, "run", fake_run)
        fetch_round_count("o/r", "7")
        for jq_filter in captured:
            assert '"Bot"' in jq_filter
            assert "<!-- multi-llm-review -->" in jq_filter

    def test_api_failure_fails_open_to_zero(self, monkeypatch: Any) -> None:
        def fake_run(cmd: list[str], **kwargs: Any) -> SimpleNamespace:
            return SimpleNamespace(returncode=1, stdout="", stderr="boom")

        monkeypatch.setattr(subprocess, "run", fake_run)
        assert fetch_round_count("o/r", "7") == 0

    def test_timeout_fails_open_to_zero(self, monkeypatch: Any) -> None:
        def fake_run(cmd: list[str], **kwargs: Any) -> SimpleNamespace:
            raise subprocess.TimeoutExpired(cmd=cmd, timeout=1)

        monkeypatch.setattr(subprocess, "run", fake_run)
        assert fetch_round_count("o/r", "7") == 0


class TestPostCutoffSummary:
    _COMMENTS = [
        {"path": "a.py", "line": 3, "side": "RIGHT", "body": "- **minor**: x"},
        {"path": "b.py", "line": 9, "side": "RIGHT", "body": "? **suggestion**: y"},
    ]

    def test_posts_single_comment_with_template(self, monkeypatch: Any) -> None:
        recorded: dict[str, Any] = {}

        def fake_run(cmd: list[str], **kwargs: Any) -> SimpleNamespace:
            if cmd[:2] == ["gh", "api"]:
                # Marker-dedup check: no existing summary comment.
                return SimpleNamespace(returncode=0, stdout="0", stderr="")
            recorded["cmd"] = cmd
            recorded["body"] = kwargs.get("input")
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        monkeypatch.setattr(subprocess, "run", fake_run)
        post_cutoff_summary("o/r", "7", "claude", 6, self._COMMENTS)
        assert recorded["cmd"][:3] == ["gh", "pr", "comment"]
        body = recorded["body"]
        assert "<!-- round-cutoff-claude-r6 -->" in body
        assert "R6 convergence cutoff" in body
        assert "recommended for follow-up" in body
        assert "a.py:3" in body
        assert "b.py:9" in body

    def test_existing_marker_skips_post(self, monkeypatch: Any) -> None:
        posted: list[list[str]] = []

        def fake_run(cmd: list[str], **kwargs: Any) -> SimpleNamespace:
            if cmd[:2] == ["gh", "api"]:
                return SimpleNamespace(returncode=0, stdout="1", stderr="")
            posted.append(cmd)
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        monkeypatch.setattr(subprocess, "run", fake_run)
        post_cutoff_summary("o/r", "7", "claude", 6, self._COMMENTS)
        assert posted == []


class TestMainRoundCutoff:
    """End-to-end main() behavior around the cutoff gate."""

    _DIFF = "+++ b/a.py\n@@ -1,3 +1,3 @@\n line1\n+line2\n line3\n"

    def _write_inputs(self, tmp_path: Any, issues: list[dict[str, Any]]) -> None:
        review = {"summary": "s", "early_exit": False, "issues": issues}
        (tmp_path / "review-claude.json").write_text(json.dumps(review))
        (tmp_path / "pr.diff").write_text(self._DIFF)

    def _run_main(self, monkeypatch: Any, tmp_path: Any) -> None:
        monkeypatch.setenv("PR_NUMBER", "7")
        monkeypatch.setenv("GITHUB_REPOSITORY", "o/r")
        monkeypatch.setattr(
            post_inline_comments, "fetch_existing_threads", lambda repo, pr: []
        )
        monkeypatch.setattr(
            sys,
            "argv",
            [
                "post_inline_comments.py",
                "--issues",
                str(tmp_path / "review-claude.json"),
                "--diff",
                str(tmp_path / "pr.diff"),
                "--reviewer",
                "claude",
            ],
        )
        post_inline_comments.main()

    def test_cutoff_suppresses_inline_and_folds(
        self, monkeypatch: Any, tmp_path: Any
    ) -> None:
        self._write_inputs(tmp_path, [_cutoff_issue()])
        monkeypatch.setattr(
            post_inline_comments, "fetch_round_count", lambda repo, pr: 5
        )
        folded: dict[str, Any] = {}

        def record_summary(
            repo: str,
            pr: str,
            reviewer: str,
            round_number: int,
            comments: list[dict[str, Any]],
        ) -> None:
            folded.update(round_number=round_number, comments=comments)

        def fail_inline(*args: Any, **kwargs: Any) -> bool:
            raise AssertionError("inline review must not be posted under cutoff")

        monkeypatch.setattr(post_inline_comments, "post_cutoff_summary", record_summary)
        monkeypatch.setattr(post_inline_comments, "post_inline_review", fail_inline)
        self._run_main(monkeypatch, tmp_path)
        assert folded["round_number"] == 6
        assert len(folded["comments"]) == 1
        assert folded["comments"][0]["path"] == "a.py"

    def test_below_cutoff_posts_inline_normally(
        self, monkeypatch: Any, tmp_path: Any
    ) -> None:
        self._write_inputs(tmp_path, [_cutoff_issue()])
        monkeypatch.setattr(
            post_inline_comments, "fetch_round_count", lambda repo, pr: 0
        )
        posted: dict[str, Any] = {}

        def record_inline(
            repo: str,
            pr: str,
            sha: str,
            reviewer: str,
            comments: list[dict[str, Any]],
        ) -> bool:
            posted.update(comments=comments)
            return True

        def fail_summary(*args: Any, **kwargs: Any) -> None:
            raise AssertionError("cutoff summary must not be posted below cutoff")

        monkeypatch.setattr(post_inline_comments, "get_pr_head_sha", lambda pr: "abc")
        monkeypatch.setattr(post_inline_comments, "post_inline_review", record_inline)
        monkeypatch.setattr(post_inline_comments, "post_cutoff_summary", fail_summary)
        self._run_main(monkeypatch, tmp_path)
        assert len(posted["comments"]) == 1

    def test_major_at_cutoff_posts_inline_normally(
        self, monkeypatch: Any, tmp_path: Any
    ) -> None:
        # Distinct description so batch-internal dedup keeps both findings.
        self._write_inputs(
            tmp_path,
            [
                _cutoff_issue(),
                _cutoff_issue(
                    severity="major",
                    line=3,
                    description="hardcoded credentials in loader",
                ),
            ],
        )
        monkeypatch.setattr(
            post_inline_comments, "fetch_round_count", lambda repo, pr: 9
        )
        posted: dict[str, Any] = {}

        def record_inline(
            repo: str,
            pr: str,
            sha: str,
            reviewer: str,
            comments: list[dict[str, Any]],
        ) -> bool:
            posted.update(comments=comments)
            return True

        def fail_summary(*args: Any, **kwargs: Any) -> None:
            raise AssertionError("cutoff must not apply when a major is present")

        monkeypatch.setattr(post_inline_comments, "get_pr_head_sha", lambda pr: "abc")
        monkeypatch.setattr(post_inline_comments, "post_inline_review", record_inline)
        monkeypatch.setattr(post_inline_comments, "post_cutoff_summary", fail_summary)
        self._run_main(monkeypatch, tmp_path)
        assert len(posted["comments"]) == 2
