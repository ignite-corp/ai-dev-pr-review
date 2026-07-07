"""Tests for aggregate_reviews severity normalization and validation."""

from __future__ import annotations

import logging
from typing import Any
from unittest.mock import patch

import pytest

from aggregate_reviews import (
    _normalize_severity,
    _is_comment_only,
    _is_valid_review,
    apply_verdict_rules,
    format_summary,
    main,
    post_verdict,
    REVIEWER_NAMES,
)


def _make_review(**overrides: Any) -> dict[str, Any]:
    """Create a minimal valid review payload."""
    base: dict[str, Any] = {
        "summary": "Test review",
        "early_exit": False,
        "issues": [],
    }
    base.update(overrides)
    return base


def _make_issue(**overrides: Any) -> dict[str, Any]:
    """Create a minimal valid issue."""
    base: dict[str, Any] = {
        "severity": "minor",
        "file": "foo.py",
        "line": 1,
        "description": "test issue",
        "suggestion": None,
    }
    base.update(overrides)
    return base


class TestNormalizeSeverity:
    def test_standard_severity_unchanged(self) -> None:
        review = _make_review(issues=[_make_issue(severity="critical")])
        _normalize_severity(review)
        assert review["issues"][0]["severity"] == "critical"

    @pytest.mark.parametrize(
        "input_sev,expected",
        [
            ("high", "major"),
            ("medium", "minor"),
            ("low", "suggestion"),
            ("info", "suggestion"),
            ("warning", "minor"),
            ("note", "suggestion"),
        ],
    )
    def test_alias_mapped(self, input_sev: str, expected: str) -> None:
        review = _make_review(issues=[_make_issue(severity=input_sev)])
        _normalize_severity(review)
        assert review["issues"][0]["severity"] == expected

    @pytest.mark.parametrize(
        "input_sev,expected",
        [
            ("HIGH", "major"),
            ("CRITICAL", "critical"),
            ("Major", "major"),
            ("Minor", "minor"),
            ("SUGGESTION", "suggestion"),
        ],
    )
    def test_case_insensitive(self, input_sev: str, expected: str) -> None:
        review = _make_review(issues=[_make_issue(severity=input_sev)])
        _normalize_severity(review)
        assert review["issues"][0]["severity"] == expected

    def test_unknown_severity_left_untouched(self) -> None:
        review = _make_review(issues=[_make_issue(severity="unknown_value")])
        _normalize_severity(review)
        assert review["issues"][0]["severity"] == "unknown_value"

    def test_unknown_severity_logs_warning(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        with caplog.at_level(logging.WARNING):
            review = _make_review(issues=[_make_issue(severity="unknown_value")])
            _normalize_severity(review)
        assert "Unknown severity" in caplog.text
        assert "unknown_value" in caplog.text

    def test_error_mapped_to_major(self) -> None:
        review = _make_review(issues=[_make_issue(severity="error")])
        _normalize_severity(review)
        assert review["issues"][0]["severity"] == "major"

    def test_multiple_issues_normalized(self) -> None:
        review = _make_review(
            issues=[
                _make_issue(severity="high"),
                _make_issue(severity="medium"),
                _make_issue(severity="critical"),
            ]
        )
        _normalize_severity(review)
        assert review["issues"][0]["severity"] == "major"
        assert review["issues"][1]["severity"] == "minor"
        assert review["issues"][2]["severity"] == "critical"

    def test_non_dict_data_ignored(self) -> None:
        _normalize_severity("not a dict")
        _normalize_severity(None)
        _normalize_severity([])

    def test_missing_issues_key_ignored(self) -> None:
        _normalize_severity({"summary": "test"})


class TestIsValidReviewWithNormalization:
    def test_review_with_aliased_severity_valid_after_normalization(self) -> None:
        review = _make_review(issues=[_make_issue(severity="high")])
        assert not _is_valid_review(review)  # invalid before
        _normalize_severity(review)
        assert _is_valid_review(review)  # valid after

    def test_review_with_unknown_severity_rejected_after_normalization(self) -> None:
        review = _make_review(issues=[_make_issue(severity="blocker")])
        _normalize_severity(review)
        assert not _is_valid_review(review)

    def test_review_without_issues_valid(self) -> None:
        review = _make_review()
        assert _is_valid_review(review)


def _make_named_review(name: str, issues: list[dict[str, Any]]) -> dict[str, Any]:
    """Create a valid review payload tagged with reviewer name."""
    return {
        "summary": f"{name} review",
        "early_exit": False,
        "issues": [{**i, "reviewer": name} for i in issues],
    }


class TestCriticalThreshold:
    """CRITICAL_THRESHOLD=2: one critical issue must NOT block; two must block."""

    def _reviews_with_criticals(self, count: int) -> dict[str, dict[str, Any] | None]:
        reviews: dict[str, dict[str, Any] | None] = {n: None for n in REVIEWER_NAMES}
        names = list(REVIEWER_NAMES)
        for i in range(min(count, len(names))):
            reviews[names[i]] = _make_named_review(
                names[i],
                [_make_issue(severity="critical")],
            )
        return reviews

    def test_one_critical_does_not_trigger_request_changes(self) -> None:
        # With CRITICAL_THRESHOLD=2, a single critical should not block.
        # Provide 2 reviewers (to meet MIN_REVIEWERS_FOR_VERDICT) but only 1 critical.
        names = list(REVIEWER_NAMES)
        reviews: dict[str, dict[str, Any] | None] = {n: None for n in REVIEWER_NAMES}
        reviews[names[0]] = _make_named_review(
            names[0], [_make_issue(severity="critical")]
        )
        reviews[names[1]] = _make_named_review(names[1], [])
        with patch("aggregate_reviews.CRITICAL_THRESHOLD", 2):
            verdict, _, _ = apply_verdict_rules(reviews)
        assert verdict == "approve"

    def test_two_criticals_trigger_request_changes(self) -> None:
        # Two criticals from different reviewers should block with threshold=2.
        reviews: dict[str, dict[str, Any] | None] = {}
        names = list(REVIEWER_NAMES)
        for name in names:
            reviews[name] = _make_named_review(name, [_make_issue(severity="critical")])
        with patch("aggregate_reviews.CRITICAL_THRESHOLD", 2):
            verdict, _, _ = apply_verdict_rules(reviews)
        assert verdict == "request_changes"


class TestMajorConsensusOverlap:
    """MAJOR_CONSENSUS_OVERLAP=0.5: 40% word overlap must NOT trigger consensus."""

    def test_40_percent_overlap_no_consensus(self) -> None:
        # desc_a has 10 words; desc_b shares 4 (40%) -> below new 0.5 threshold.
        desc_a = "alpha bravo charlie delta echo foxtrot golf hotel india juliet"
        desc_b = "alpha bravo charlie delta kilo lima mike november oscar papa"
        names = list(REVIEWER_NAMES)
        reviews: dict[str, dict[str, Any] | None] = {n: None for n in REVIEWER_NAMES}
        reviews[names[0]] = _make_named_review(
            names[0],
            [_make_issue(severity="major", file="app/foo.py", description=desc_a)],
        )
        reviews[names[1]] = _make_named_review(
            names[1],
            [_make_issue(severity="major", file="app/foo.py", description=desc_b)],
        )
        reviews[names[2]] = _make_named_review(names[2], [])
        with patch("aggregate_reviews.MAJOR_CONSENSUS_OVERLAP", 0.5):
            verdict, _, _ = apply_verdict_rules(reviews)
        # 40% overlap < 0.5 threshold -> consensus NOT triggered -> approve
        assert verdict == "approve"

    def test_60_percent_overlap_triggers_consensus(self) -> None:
        # 6 shared words out of 10 = 60% -> above 0.5 threshold.
        desc_a = "alpha bravo charlie delta echo foxtrot golf hotel india juliet"
        desc_b = "alpha bravo charlie delta echo foxtrot kilo lima mike november"
        names = list(REVIEWER_NAMES)
        reviews: dict[str, dict[str, Any] | None] = {n: None for n in REVIEWER_NAMES}
        reviews[names[0]] = _make_named_review(
            names[0],
            [_make_issue(severity="major", file="app/foo.py", description=desc_a)],
        )
        reviews[names[1]] = _make_named_review(
            names[1],
            [_make_issue(severity="major", file="app/foo.py", description=desc_b)],
        )
        reviews[names[2]] = _make_named_review(names[2], [])
        with patch("aggregate_reviews.MAJOR_CONSENSUS_OVERLAP", 0.5):
            verdict, _, _ = apply_verdict_rules(reviews)
        assert verdict == "request_changes"


class TestAllEarlyExitBenign:
    """all-success + 0-available -> approve benign; 1-failure + 0-available -> comment."""

    def _empty_reviews(self) -> dict[str, dict[str, Any] | None]:
        return {name: None for name in REVIEWER_NAMES}

    def test_all_success_no_payload_approves(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # All REVIEWER_RESULT_* = "success", zero review files -> benign skip APPROVE.
        for name in REVIEWER_NAMES:
            monkeypatch.setenv(f"REVIEWER_RESULT_{name.upper()}", "success")
        verdict, reason, _ = apply_verdict_rules(self._empty_reviews())
        assert verdict == "approve"
        assert "benign skip" in reason

    def test_one_failure_no_payload_returns_comment(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # One reviewer failed -> not benign -> comment verdict.
        names = list(REVIEWER_NAMES)
        monkeypatch.setenv(f"REVIEWER_RESULT_{names[0].upper()}", "failure")
        for name in names[1:]:
            monkeypatch.setenv(f"REVIEWER_RESULT_{name.upper()}", "success")
        verdict, reason, _ = apply_verdict_rules(self._empty_reviews())
        assert verdict == "comment"
        assert "benign skip" not in reason

    def test_missing_env_var_not_benign(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Empty/missing env var means unknown conclusion -> not benign.
        for name in REVIEWER_NAMES:
            monkeypatch.delenv(f"REVIEWER_RESULT_{name.upper()}", raising=False)
        verdict, _, _ = apply_verdict_rules(self._empty_reviews())
        assert verdict == "comment"


class TestDependabotThresholdScoping:
    """Author-scoped thresholds: dependabot gets CRITICAL_THRESHOLD=2, humans get 1."""

    def _two_reviewer_reviews_with_critical(self) -> dict[str, dict[str, Any] | None]:
        names = list(REVIEWER_NAMES)
        reviews: dict[str, dict[str, Any] | None] = {n: None for n in REVIEWER_NAMES}
        reviews[names[0]] = _make_named_review(
            names[0], [_make_issue(severity="critical")]
        )
        reviews[names[1]] = _make_named_review(names[1], [])
        return reviews

    def test_dependabot_threshold_2_one_critical_approves(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # dependabot PR: 1 critical -> approve (threshold=2 requires 2 to block).
        monkeypatch.setenv("PR_AUTHOR", "dependabot[bot]")
        monkeypatch.setenv("DEPENDABOT_CRITICAL_THRESHOLD", "2")
        with patch("aggregate_reviews.CRITICAL_THRESHOLD", 2):
            verdict, _, _ = apply_verdict_rules(
                self._two_reviewer_reviews_with_critical()
            )
        assert verdict == "approve"

    def test_dependabot_threshold_2_two_criticals_blocked(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # dependabot PR: 2 criticals -> request_changes (threshold=2 triggered).
        monkeypatch.setenv("PR_AUTHOR", "dependabot[bot]")
        names = list(REVIEWER_NAMES)
        reviews: dict[str, dict[str, Any] | None] = {n: None for n in REVIEWER_NAMES}
        for name in names:
            reviews[name] = _make_named_review(name, [_make_issue(severity="critical")])
        with patch("aggregate_reviews.CRITICAL_THRESHOLD", 2):
            verdict, _, _ = apply_verdict_rules(reviews)
        assert verdict == "request_changes"

    def test_human_threshold_1_one_critical_blocked(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Human PR: 1 critical -> request_changes (threshold=1, original behavior).
        monkeypatch.setenv("PR_AUTHOR", "hyuk-hur")
        with patch("aggregate_reviews.CRITICAL_THRESHOLD", 1):
            verdict, _, _ = apply_verdict_rules(
                self._two_reviewer_reviews_with_critical()
            )
        assert verdict == "request_changes"


class TestMainAllEarlyExitBenign:
    """main() must not exit non-zero when all reviewer jobs succeeded.

    Regression guard for R4: benign early-exit must post approve and exit 0.
    """

    def test_main_all_early_exit_does_not_exit_nonzero(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # All REVIEWER_RESULT_* = "success", no review files -> benign skip -> exit 0.
        for name in REVIEWER_NAMES:
            monkeypatch.setenv(f"REVIEWER_RESULT_{name.upper()}", "success")
        monkeypatch.setenv("PR_NUMBER", "42")
        monkeypatch.setenv("GITHUB_REPOSITORY", "owner/repo")

        with (
            patch(
                "aggregate_reviews.load_reviews",
                return_value={n: None for n in REVIEWER_NAMES},
            ),
            patch("aggregate_reviews.post_verdict") as mock_post,
        ):
            # main() should complete without raising SystemExit
            main()

        # Verify approve verdict was posted
        assert mock_post.call_count == 1
        call_args = mock_post.call_args
        posted_verdict = call_args[0][1]
        assert posted_verdict == "approve"


class TestReviewerToken:
    """post_verdict uses REVIEWER_TOKEN only for the approve review call."""

    def _run(
        self, monkeypatch: pytest.MonkeyPatch, verdict: str
    ) -> dict[str, str] | None:
        monkeypatch.setenv("PR_NUMBER", "42")
        monkeypatch.setenv("GITHUB_REPOSITORY", "owner/repo")
        monkeypatch.setenv("GH_TOKEN", "default-token")

        captured: dict[str, dict[str, str] | None] = {"env": None}

        def fake_run(*args: Any, **kwargs: Any) -> Any:
            captured["env"] = kwargs.get("env")
            return type("R", (), {"returncode": 0, "stderr": "", "stdout": ""})()

        with (
            patch("aggregate_reviews._minimize_stale_bot_items"),
            patch("aggregate_reviews.subprocess.run", side_effect=fake_run),
        ):
            post_verdict("body", verdict, comment_only=False)

        return captured["env"]

    def test_approve_with_reviewer_token_uses_it(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("REVIEWER_TOKEN", "app-token")
        env = self._run(monkeypatch, "approve")
        assert env is not None
        assert env["GH_TOKEN"] == "app-token"

    def test_approve_without_reviewer_token_unchanged(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("REVIEWER_TOKEN", "   ")
        env = self._run(monkeypatch, "approve")
        assert env is not None
        assert env["GH_TOKEN"] == "default-token"

    def test_request_changes_does_not_use_reviewer_token(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("REVIEWER_TOKEN", "app-token")
        env = self._run(monkeypatch, "request_changes")
        assert env is not None
        assert env["GH_TOKEN"] == "default-token"


class TestCommentOnlyGating:
    """ALLOW_AUTO_APPROVE gates ALL formal review events (approve + request_changes)."""

    def _run(
        self, monkeypatch: pytest.MonkeyPatch, verdict: str, *, comment_only: bool
    ) -> list[list[str]]:
        monkeypatch.setenv("PR_NUMBER", "42")
        monkeypatch.setenv("GITHUB_REPOSITORY", "owner/repo")
        monkeypatch.setenv("GH_TOKEN", "default-token")

        commands: list[list[str]] = []

        def fake_run(cmd: list[str], *args: Any, **kwargs: Any) -> Any:
            commands.append(cmd)
            return type("R", (), {"returncode": 0, "stderr": "", "stdout": ""})()

        with (
            patch("aggregate_reviews._minimize_stale_bot_items"),
            patch("aggregate_reviews.subprocess.run", side_effect=fake_run),
        ):
            post_verdict("body", verdict, comment_only=comment_only)

        return commands

    def _has_review_request_changes(self, commands: list[list[str]]) -> bool:
        return any(
            "review" in cmd and "--request-changes" in cmd for cmd in commands
        )

    def _has_pr_comment(self, commands: list[list[str]]) -> bool:
        return any("comment" in cmd for cmd in commands)

    def test_request_changes_comment_only_downgrades_to_comment(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        commands = self._run(monkeypatch, "request_changes", comment_only=True)
        assert not self._has_review_request_changes(commands)
        assert self._has_pr_comment(commands)

    def test_request_changes_not_comment_only_submits_review(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        commands = self._run(monkeypatch, "request_changes", comment_only=False)
        assert self._has_review_request_changes(commands)

    def test_approve_comment_only_downgrades_to_comment(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        commands = self._run(monkeypatch, "approve", comment_only=True)
        assert not any("review" in cmd for cmd in commands)
        assert self._has_pr_comment(commands)


class TestCommentOnlyToggle:
    """_is_comment_only maps ALLOW_AUTO_APPROVE to the comment-only killswitch."""

    def test_default_is_comment_only(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("ALLOW_AUTO_APPROVE", raising=False)
        assert _is_comment_only() is True

    def test_false_is_comment_only(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ALLOW_AUTO_APPROVE", "false")
        assert _is_comment_only() is True

    def test_true_disables_comment_only(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("ALLOW_AUTO_APPROVE", "true")
        assert _is_comment_only() is False


class TestSummaryLabels:
    """format_summary header reflects the comment-only killswitch."""

    def test_request_changes_comment_only_label(self) -> None:
        summary = format_summary(
            {}, "request_changes", "reason", {}, comment_only=True
        )
        assert "Changes recommended (comment only -- auto-approve disabled)" in summary
        assert "Changes Requested" not in summary

    def test_request_changes_active_label(self) -> None:
        summary = format_summary(
            {}, "request_changes", "reason", {}, comment_only=False
        )
        assert "Changes Requested" in summary

    def test_approve_comment_only_label(self) -> None:
        summary = format_summary({}, "approve", "reason", {}, comment_only=True)
        assert "comment only -- auto-approve disabled" in summary
