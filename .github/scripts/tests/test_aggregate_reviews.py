"""Tests for aggregate_reviews severity normalization and validation."""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
import yaml

from aggregate_reviews import (
    _get_available,
    _has_full_reviewer_coverage,
    _prepare_failure_result,
    _head_is_stale,
    _size_skip_details,
    _has_early_exit,
    _normalize_severity,
    _normalize_status,
    _is_comment_only,
    _is_partial,
    _is_valid_review,
    apply_verdict_rules,
    format_prepare_failure_summary,
    format_size_skip_summary,
    format_summary,
    load_reviews,
    main,
    post_verdict,
    REVIEWER_NAMES,
)
from github_pr_support import REVIEW_MARKER


def _make_review(**overrides: Any) -> dict[str, Any]:
    """Create a minimal valid review payload.

    Carries ``status: "ok"`` because every emitter now ships ``status``;
    use ``_make_status_less_review`` to build a contract-violating payload.
    """
    base: dict[str, Any] = {
        "summary": "Test review",
        "status": "ok",
        "early_exit": False,
        "issues": [],
    }
    base.update(overrides)
    return base


def _make_status_less_review(**overrides: Any) -> dict[str, Any]:
    """Create a payload that violates the contract by omitting ``status``."""
    review = _make_review(**overrides)
    review.pop("status", None)
    return review


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
        "status": "ok",
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


def _error_payloads() -> dict[str, dict[str, Any] | None]:
    """One error-bearing fallback payload per reviewer (no issues)."""
    return {
        name: _make_review(
            summary=f"{name.title()} review failed: CLI exited 2",
            status="failed",
            error="cli_invocation_failed",
            error_detail="error: unexpected argument '--full-auto' found",
        )
        for name in REVIEWER_NAMES
    }


class TestErrorPayloadExclusion:
    """Error-bearing fallback payloads must not count as live reviewers."""

    def test_error_payload_without_issues_excluded(self) -> None:
        reviews: dict[str, dict[str, Any] | None] = {
            "codex": _make_review(
                summary="Codex review failed: CLI exited 2",
                status="failed",
                error="cli_invocation_failed",
            )
        }
        assert _get_available(reviews) == {}

    def test_error_payload_with_issues_still_included(self) -> None:
        # Partial failures that produced issues keep contributing (existing rule).
        reviews: dict[str, dict[str, Any] | None] = {
            "codex": _make_review(error="truncated", issues=[_make_issue()])
        }
        assert "codex" in _get_available(reviews)

    def test_all_error_payloads_yield_comment_not_benign_approve(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Jobs conclude "success" (continue-on-error) but every reviewer wrote
        # an error payload -> must NOT be treated as a benign skip.
        for name in REVIEWER_NAMES:
            monkeypatch.setenv(f"REVIEWER_RESULT_{name.upper()}", "success")
        verdict, reason, _ = apply_verdict_rules(_error_payloads())
        assert verdict == "comment"
        assert "all failed" in reason
        assert "benign skip" not in reason


class TestNormalFullResponses:
    """Regression: three live reviewers with no issues still approve."""

    def test_three_reviewers_no_issues_approves(self) -> None:
        reviews: dict[str, dict[str, Any] | None] = {
            name: _make_named_review(name, []) for name in REVIEWER_NAMES
        }
        verdict, reason, _ = apply_verdict_rules(reviews)
        assert verdict == "approve"
        assert "3/3" in reason
        assert "no issues" in reason


class TestMainAllErrorPayloadsFail:
    """main() must exit 1 when every reviewer produced an error payload."""

    def test_main_all_error_payloads_exits_nonzero(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # All job conclusions "success" (reviewer steps are continue-on-error),
        # but error payloads exist -> no benign bypass, CI must fail.
        for name in REVIEWER_NAMES:
            monkeypatch.setenv(f"REVIEWER_RESULT_{name.upper()}", "success")
        monkeypatch.setenv("PR_NUMBER", "42")
        monkeypatch.setenv("GITHUB_REPOSITORY", "owner/repo")

        with (
            patch(
                "aggregate_reviews.load_reviews",
                return_value=_error_payloads(),
            ),
            patch("aggregate_reviews.post_verdict") as mock_post,
            pytest.raises(SystemExit) as excinfo,
        ):
            main()

        assert excinfo.value.code == 1
        posted_verdict = mock_post.call_args[0][1]
        assert posted_verdict == "comment"


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


class TestApproveQuorumGate:
    """Formal approval requires a minimum quorum of available reviewers."""

    def _run(
        self,
        monkeypatch: pytest.MonkeyPatch,
        verdict: str,
        *,
        approve_quorum: bool,
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
            post_verdict(
                "body", verdict, comment_only=False, approve_quorum=approve_quorum
            )

        return commands

    def test_approve_without_quorum_posts_comment(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        commands = self._run(monkeypatch, "approve", approve_quorum=False)
        assert not any("review" in cmd for cmd in commands)
        assert any("comment" in cmd for cmd in commands)

    def test_approve_with_quorum_submits_review(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        commands = self._run(monkeypatch, "approve", approve_quorum=True)
        assert any("review" in cmd and "--approve" in cmd for cmd in commands)

    def test_request_changes_unaffected_by_quorum(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        commands = self._run(monkeypatch, "request_changes", approve_quorum=False)
        assert any("review" in cmd and "--request-changes" in cmd for cmd in commands)

    def test_main_benign_skip_withholds_formal_approval(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Benign skip: verdict "approve" with 0 available payloads -> main()
        # must request the comment downgrade (approve_quorum=False).
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
            main()

        assert mock_post.call_args[0][1] == "approve"
        assert mock_post.call_args.kwargs["approve_quorum"] is False

    def test_main_full_quorum_allows_formal_approval(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("PR_NUMBER", "42")
        monkeypatch.setenv("GITHUB_REPOSITORY", "owner/repo")

        reviews: dict[str, dict[str, Any] | None] = {
            name: _make_named_review(name, []) for name in REVIEWER_NAMES
        }
        with (
            patch("aggregate_reviews.load_reviews", return_value=reviews),
            patch("aggregate_reviews.post_verdict") as mock_post,
        ):
            main()

        assert mock_post.call_args[0][1] == "approve"
        assert mock_post.call_args.kwargs["approve_quorum"] is True

    def _run_main(
        self,
        monkeypatch: pytest.MonkeyPatch,
        reviews: dict[str, Any],
        conclusions: dict[str, str],
    ) -> Any:
        """Drive main() over a reviewer payload set, returning the post mock."""
        monkeypatch.setenv("PR_NUMBER", "42")
        monkeypatch.setenv("GITHUB_REPOSITORY", "owner/repo")
        for name in REVIEWER_NAMES:
            monkeypatch.setenv(
                f"REVIEWER_RESULT_{name.upper()}", conclusions.get(name, "success")
            )

        with (
            patch("aggregate_reviews.load_reviews", return_value=reviews),
            patch("aggregate_reviews.post_verdict") as mock_post,
        ):
            main()
        return mock_post

    def test_main_absent_reviewer_withholds_formal_approval(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # AT-2124: two live reviewers clear MIN_REVIEWERS_FOR_VERDICT, so the
        # verdict is "approve" -- but gemini never ran, and a formal APPROVED
        # review would assert coverage that never happened.
        monkeypatch.setenv("REVIEW_MODE", "parallel")
        names = list(REVIEWER_NAMES)
        reviews: dict[str, Any] = {n: _make_named_review(n, []) for n in names}
        reviews["gemini"] = None

        mock_post = self._run_main(monkeypatch, reviews, {"gemini": "failure"})

        assert mock_post.call_args[0][1] == "approve"
        assert mock_post.call_args.kwargs["approve_quorum"] is False

    def test_main_absent_reviewer_leaves_verdict_and_exit_unchanged(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The verdict gate is untouched: MIN_REVIEWERS_FOR_VERDICT is still 2,
        # so one reviewer's outage must not turn the required check red.
        monkeypatch.setenv("REVIEW_MODE", "parallel")
        reviews: dict[str, Any] = {n: _make_named_review(n, []) for n in REVIEWER_NAMES}
        reviews["gemini"] = None

        # main() returning at all is the exit-code assertion: an insufficient
        # response set would have raised SystemExit(1).
        mock_post = self._run_main(monkeypatch, reviews, {"gemini": "failure"})

        assert mock_post.call_args[0][1] == "approve"
        assert "2/3 LLM responses" in mock_post.call_args[0][0]

    def test_main_failed_status_reviewer_withholds_formal_approval(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A payload with status "failed" is an infrastructure failure, never a
        # performed review (AT-1799 contract), so coverage is incomplete.
        monkeypatch.setenv("REVIEW_MODE", "parallel")
        reviews: dict[str, Any] = {n: _make_named_review(n, []) for n in REVIEWER_NAMES}
        reviews["codex"] = _make_review(status="failed", error="provider 500")

        mock_post = self._run_main(monkeypatch, reviews, {})

        assert mock_post.call_args[0][1] == "approve"
        assert mock_post.call_args.kwargs["approve_quorum"] is False

    def test_main_early_exit_reviewer_counts_as_having_run(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # An early exit is a judgement the reviewer reached after reading the
        # diff. All three ran, so the full configured set is covered.
        monkeypatch.setenv("REVIEW_MODE", "parallel")
        reviews: dict[str, Any] = {n: _make_named_review(n, []) for n in REVIEWER_NAMES}
        reviews["claude"]["status"] = "early_exit"
        reviews["claude"]["early_exit"] = True

        mock_post = self._run_main(monkeypatch, reviews, {})

        assert mock_post.call_args[0][1] == "approve"
        assert mock_post.call_args.kwargs["approve_quorum"] is True

    def test_main_sequential_early_exit_cascade_withholds_approval(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Sequential mode: claude early-exits, so codex and gemini are skipped
        # by design. Claude ran; the other two did not.
        monkeypatch.setenv("REVIEW_MODE", "sequential")
        reviews: dict[str, Any] = {n: None for n in REVIEWER_NAMES}
        reviews["claude"] = _make_review(status="early_exit", early_exit=True)

        mock_post = self._run_main(
            monkeypatch,
            reviews,
            {"codex": "skipped", "gemini": "skipped"},
        )

        assert mock_post.call_args[0][1] == "approve"
        assert mock_post.call_args.kwargs["approve_quorum"] is False

    def test_main_sequential_skipped_tail_withholds_approval(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Sequential mode: claude and codex ran (codex early-exited), which
        # skips gemini. Two available payloads clear the verdict gate, but
        # gemini never saw the diff.
        monkeypatch.setenv("REVIEW_MODE", "sequential")
        reviews: dict[str, Any] = {n: None for n in REVIEWER_NAMES}
        reviews["claude"] = _make_named_review("claude", [])
        reviews["codex"] = _make_review(status="early_exit", early_exit=True)

        mock_post = self._run_main(monkeypatch, reviews, {"gemini": "skipped"})

        assert mock_post.call_args[0][1] == "approve"
        assert mock_post.call_args.kwargs["approve_quorum"] is False


class TestFullReviewerCoverage:
    """AT-2124: approve_quorum is full coverage, not an availability count."""

    def test_all_configured_reviewers_present_is_covered(self) -> None:
        available = {n: _make_named_review(n, []) for n in REVIEWER_NAMES}
        assert _has_full_reviewer_coverage(available) is True

    def test_missing_reviewer_is_not_covered(self) -> None:
        names = list(REVIEWER_NAMES)
        available = {n: _make_named_review(n, []) for n in names[:-1]}
        assert _has_full_reviewer_coverage(available) is False

    def test_empty_available_is_not_covered(self) -> None:
        assert _has_full_reviewer_coverage({}) is False


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


class TestStatusContract:
    """AT-1799: explicit `status` is the single source of truth."""

    def test_status_failed_excluded_without_error_key(self) -> None:
        # Fail-closed even when the legacy `error` signal is absent.
        reviews: dict[str, dict[str, Any] | None] = {
            "codex": _make_review(status="failed")
        }
        assert _get_available(reviews) == {}

    def test_status_ok_counted(self) -> None:
        reviews: dict[str, dict[str, Any] | None] = {
            "codex": _make_review(status="ok")
        }
        assert "codex" in _get_available(reviews)

    def test_status_ok_wins_over_error_key(self) -> None:
        # A partial failure that still reports "ok" keeps counting.
        reviews: dict[str, dict[str, Any] | None] = {
            "codex": _make_review(status="ok", error="transient")
        }
        assert "codex" in _get_available(reviews)

    def test_status_early_exit_counted_and_drives_early_exit(self) -> None:
        review = _make_review(status="early_exit")
        reviews: dict[str, dict[str, Any] | None] = {"codex": review}
        available = _get_available(reviews)
        assert "codex" in available
        assert _has_early_exit(available)

    def test_status_ok_overrides_early_exit_flag(self) -> None:
        review = _make_review(status="ok", early_exit=True)
        assert not _has_early_exit({"codex": review})

    def test_status_early_exit_enables_sequential_bypass(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("REVIEW_MODE", "sequential")
        names = list(REVIEWER_NAMES)
        reviews: dict[str, dict[str, Any] | None] = {n: None for n in REVIEWER_NAMES}
        reviews[names[0]] = _make_review(status="early_exit")
        verdict, reason, _ = apply_verdict_rules(reviews)
        assert verdict == "approve"
        assert "no issues" in reason

    def test_status_failed_counts_as_partial(self) -> None:
        assert _is_partial(_make_review(status="failed"))
        assert not _is_partial(_make_review(status="ok"))

    def test_status_failed_labeled_in_summary(self) -> None:
        reviews: dict[str, dict[str, Any] | None] = {
            n: None for n in REVIEWER_NAMES
        }
        reviews[list(REVIEWER_NAMES)[0]] = _make_review(status="failed")
        summary = format_summary(reviews, "comment", "reason", {}, comment_only=True)
        assert "status=failed" in summary


class TestUnknownStatusFailClosed:
    """AT-1799: unknown status values must never count as a performed review."""

    def test_unknown_status_excluded_and_warns(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        reviews: dict[str, dict[str, Any] | None] = {
            "codex": _make_review(status="weird")
        }
        assert _get_available(reviews) == {}
        captured = capsys.readouterr()
        assert "::warning" in captured.err
        assert "weird" in captured.err

    def test_unknown_status_normalized_to_failed(self) -> None:
        review = _make_review(status="weird")
        assert _normalize_status("codex", review) == "failed"
        assert review["status"] == "failed"

    def test_unknown_status_disqualifies_benign_skip(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # All jobs "success" but one artifact carries an unknown status:
        # the payload exists, so the benign trivial-diff skip must not fire.
        for name in REVIEWER_NAMES:
            monkeypatch.setenv(f"REVIEWER_RESULT_{name.upper()}", "success")
        reviews: dict[str, dict[str, Any] | None] = {n: None for n in REVIEWER_NAMES}
        reviews[list(REVIEWER_NAMES)[0]] = _make_review(status="weird")
        verdict, reason, _ = apply_verdict_rules(reviews)
        assert verdict == "comment"
        assert "benign skip" not in reason
        assert "all failed" in reason


class TestMissingStatusFailClosed:
    """AT-1954: `status` is mandatory -- it is never inferred from other keys.

    Every emitter now ships `status`, so a payload without it is a contract
    violation and must fail closed rather than resolve to a passing review
    (the AT-1792 failure mode: a dead reviewer counted as a clean pass).
    """

    def test_missing_status_normalized_to_failed_and_warns(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        review = _make_status_less_review()
        assert _normalize_status("codex", review) == "failed"
        assert review["status"] == "failed"
        captured = capsys.readouterr()
        assert "::warning" in captured.err
        assert "missing status" in captured.err
        assert "fail-closed" in captured.err

    def test_clean_payload_without_status_is_not_a_passing_review(self) -> None:
        # issues == [] and no error: the shape that used to infer "ok".
        review = _make_status_less_review()
        reviews: dict[str, dict[str, Any] | None] = {"codex": review}
        assert _get_available(reviews) == {}
        # _get_available stamps the fail-closed status, as load_reviews does
        # before _is_partial runs in main().
        assert _is_partial(review)

    def test_missing_status_not_inferred_from_error_key(self) -> None:
        # error + issues used to infer "ok"; it must now fail closed.
        review = _make_status_less_review(error="truncated", issues=[_make_issue()])
        assert _normalize_status("codex", review) == "failed"
        assert _get_available({"codex": review}) == {}

    def test_missing_status_not_inferred_from_early_exit_flag(self) -> None:
        review = _make_status_less_review(early_exit=True)
        assert _normalize_status("codex", review) == "failed"
        assert not _has_early_exit({"codex": review})

    def test_missing_status_disqualifies_benign_skip(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # All jobs "success" but one artifact omits status: the payload
        # exists and fails closed, so the benign trivial-diff skip must not fire.
        for name in REVIEWER_NAMES:
            monkeypatch.setenv(f"REVIEWER_RESULT_{name.upper()}", "success")
        reviews: dict[str, dict[str, Any] | None] = {n: None for n in REVIEWER_NAMES}
        reviews[list(REVIEWER_NAMES)[0]] = _make_status_less_review()
        verdict, reason, _ = apply_verdict_rules(reviews)
        assert verdict == "comment"
        assert "benign skip" not in reason
        assert "all failed" in reason

    def test_normalization_is_idempotent_and_stamped(self) -> None:
        review = _make_status_less_review()
        first = _normalize_status("codex", review)
        assert review["status"] == first
        assert _normalize_status("codex", review) == first


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


class TestClaudeInfrastructureFailure:
    """AT-1837: a dead Claude reviewer must be counted as failed, not absent.

    A dependabot-triggered run had claude-code-action reject the actor
    ("Workflow initiated by non-human actor"), so no artifact was produced.
    The aggregate then saw an ABSENT reviewer, which only lowers the count,
    and blamed a benign "early-exit or no-output" -- the AT-1792 shape.
    """

    @staticmethod
    def _claude_failed() -> dict[str, Any]:
        """The error verdict the claude path emits when it wrote nothing."""
        detail = "claude-code-action outcome=success; no execution log produced"
        return _make_review(
            summary=f"Claude review failed: no verdict file produced -- {detail}",
            status="failed",
            error="action_invocation_failed",
            error_detail=detail,
        )

    def _reviews_with_dead_claude(self) -> dict[str, dict[str, Any] | None]:
        reviews: dict[str, dict[str, Any] | None] = {
            name: _make_named_review(name, []) for name in REVIEWER_NAMES
        }
        reviews["claude"] = self._claude_failed()
        return reviews

    def test_absent_artifact_is_reported_as_benign_no_output(self) -> None:
        # Baseline the pre-fix shape: an absent payload is indistinguishable
        # from a benign skip, which is why the emitter must not leave one.
        reviews: dict[str, dict[str, Any] | None] = {
            name: _make_named_review(name, []) for name in REVIEWER_NAMES
        }
        reviews["claude"] = None
        conclusions = {name: "success" for name in REVIEWER_NAMES}
        verdict, reason, available = apply_verdict_rules(reviews)
        summary = format_summary(reviews, verdict, reason, available, conclusions)
        assert "claude: early-exit or no-output" in summary
        assert "failed" not in summary

    def test_failed_payload_excluded_but_verdict_still_reached(self) -> None:
        verdict, reason, available = apply_verdict_rules(
            self._reviews_with_dead_claude()
        )
        assert "claude" not in available
        assert set(available) == {"codex", "gemini"}
        assert verdict == "approve"
        assert "2/3" in reason

    def test_failed_payload_named_on_headline_and_in_section(self) -> None:
        reviews = self._reviews_with_dead_claude()
        conclusions = {name: "success" for name in REVIEWER_NAMES}
        verdict, reason, available = apply_verdict_rules(reviews)
        summary = format_summary(reviews, verdict, reason, available, conclusions)
        assert "claude: action_invocation_failed" in summary
        assert "(partial: action_invocation_failed)" in summary
        assert "early-exit or no-output" not in summary

    def test_failed_payload_counts_as_partial(self) -> None:
        assert _is_partial(self._claude_failed())

    def test_dead_claude_alone_does_not_fail_ci(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # One reviewer down stays merge-non-blocking: the remaining two meet
        # MIN_REVIEWERS_FOR_VERDICT, so the aggregate reports an honest 2/3.
        for name in REVIEWER_NAMES:
            monkeypatch.setenv(f"REVIEWER_RESULT_{name.upper()}", "success")
        monkeypatch.setenv("PR_NUMBER", "737")
        monkeypatch.setenv("GITHUB_REPOSITORY", "owner/repo")
        with (
            patch(
                "aggregate_reviews.load_reviews",
                return_value=self._reviews_with_dead_claude(),
            ),
            patch("aggregate_reviews.post_verdict") as mock_post,
        ):
            main()
        assert mock_post.call_args[0][1] == "approve"

    def test_dead_claude_plus_one_more_fails_ci(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Zero margin made visible: a second outage drops below quorum.
        for name in REVIEWER_NAMES:
            monkeypatch.setenv(f"REVIEWER_RESULT_{name.upper()}", "success")
        monkeypatch.setenv("PR_NUMBER", "737")
        monkeypatch.setenv("GITHUB_REPOSITORY", "owner/repo")
        reviews = self._reviews_with_dead_claude()
        reviews["codex"] = _make_review(
            summary="Codex review failed: CLI exited 2",
            status="failed",
            error="cli_invocation_failed",
        )
        with (
            patch("aggregate_reviews.load_reviews", return_value=reviews),
            patch("aggregate_reviews.post_verdict"),
            pytest.raises(SystemExit) as excinfo,
        ):
            main()
        assert excinfo.value.code == 1

    def test_failed_payload_disqualifies_benign_skip(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Claude's error verdict is an artifact: all-jobs-success must not be
        # read as a trivial-diff skip.
        for name in REVIEWER_NAMES:
            monkeypatch.setenv(f"REVIEWER_RESULT_{name.upper()}", "success")
        reviews: dict[str, dict[str, Any] | None] = {n: None for n in REVIEWER_NAMES}
        reviews["claude"] = self._claude_failed()
        verdict, reason, _ = apply_verdict_rules(reviews)
        assert verdict == "comment"
        assert "benign skip" not in reason


_SINGLE_WORKFLOW = (
    Path(__file__).resolve().parents[2] / "workflows" / "base-ai-review-single.yml"
)
_ERROR_VERDICT_STEP = "Emit Claude error verdict (no verdict file)"

requires_jq = pytest.mark.skipif(shutil.which("jq") is None, reason="jq not installed")


def _error_verdict_step_script() -> str:
    """Return the run: body of the claude error-verdict step."""
    workflow = yaml.safe_load(_SINGLE_WORKFLOW.read_text(encoding="utf-8"))
    for step in workflow["jobs"]["review"]["steps"]:
        if step.get("name") == _ERROR_VERDICT_STEP:
            return str(step["run"])
    raise AssertionError(f"step not found: {_ERROR_VERDICT_STEP}")


@requires_jq
class TestClaudeErrorVerdictStep:
    """The emitter and the aggregate must agree (AT-1837).

    Runs the workflow step's own shell body, then feeds what it wrote to
    the aggregate's loader -- the seam that silently produced nothing when
    claude-code-action died.
    """

    @staticmethod
    def _run(workdir: Path, exec_file: str, outcome: str = "success") -> None:
        result = subprocess.run(
            ["bash", "-c", _error_verdict_step_script()],
            cwd=workdir,
            env={
                "PATH": os.environ["PATH"],
                "EXEC_FILE": exec_file,
                "STEP_OUTCOME": outcome,
            },
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode == 0, result.stderr

    def test_no_execution_log_emits_failed_verdict(self, tmp_path: Path) -> None:
        self._run(tmp_path, "")
        payload = json.loads((tmp_path / "review-claude.json").read_text())
        assert payload["status"] == "failed"
        assert payload["error"] == "action_invocation_failed"
        assert payload["early_exit"] is False
        assert payload["issues"] == []
        assert "no execution log" in payload["error_detail"]

    def test_unparseable_execution_log_is_distinguished(self, tmp_path: Path) -> None:
        exec_file = tmp_path / "execution.json"
        exec_file.write_text("[]")
        self._run(tmp_path, str(exec_file))
        payload = json.loads((tmp_path / "review-claude.json").read_text())
        assert payload["error"] == "output_unparseable"

    def test_existing_verdict_file_is_left_untouched(self, tmp_path: Path) -> None:
        original = _make_named_review("claude", [])
        (tmp_path / "review-claude.json").write_text(json.dumps(original))
        self._run(tmp_path, "")
        assert json.loads((tmp_path / "review-claude.json").read_text()) == original

    def test_emitted_verdict_is_excluded_by_the_aggregate(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._run(tmp_path, "")
        for name in ("codex", "gemini"):
            (tmp_path / f"review-{name}.json").write_text(
                json.dumps(_make_named_review(name, []))
            )
        monkeypatch.chdir(tmp_path)
        reviews = load_reviews()
        available = _get_available(reviews)
        assert reviews["claude"] is not None
        assert "claude" not in available
        assert set(available) == {"codex", "gemini"}


_ORCHESTRATOR_WORKFLOW = (
    Path(__file__).resolve().parents[2]
    / "workflows"
    / "base-ai-review-orchestrator.yml"
)


class TestSizeSkipVerdict:
    """A PR over PR_SIZE_LIMIT must produce a visible, actionable failure.

    Before AT-1975 the aggregate job was gated off on this path, so the
    required context was never reported and the PR sat Pending with nothing
    failed to explain it. The block itself is not new -- only the signal is.
    """

    @staticmethod
    def _set_size_env(
        monkeypatch: pytest.MonkeyPatch, total: str = "4200", limit: str = "3000"
    ) -> None:
        monkeypatch.setenv("SIZE_SKIPPED", "true")
        monkeypatch.setenv("SIZE_TOTAL", total)
        monkeypatch.setenv("SIZE_LIMIT", limit)
        monkeypatch.setenv("PR_NUMBER", "42")
        monkeypatch.setenv("GITHUB_REPOSITORY", "owner/repo")

    def test_size_skip_exits_nonzero(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._set_size_env(monkeypatch)
        with (
            patch("aggregate_reviews.post_verdict") as mock_post,
            pytest.raises(SystemExit) as excinfo,
        ):
            main()
        assert excinfo.value.code == 1
        assert mock_post.call_args[0][1] == "request_changes"

    def test_size_skip_body_names_the_measurement(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._set_size_env(monkeypatch, total="4200", limit="3000")
        with (
            patch("aggregate_reviews.post_verdict") as mock_post,
            pytest.raises(SystemExit),
        ):
            main()
        body = mock_post.call_args[0][0]
        assert "4200" in body
        assert "3000" in body

    def test_size_skip_body_names_both_remedies(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Reason without remedy only fixes half of "blocked and cannot tell why"."""
        self._set_size_env(monkeypatch)
        with (
            patch("aggregate_reviews.post_verdict") as mock_post,
            pytest.raises(SystemExit),
        ):
            main()
        body = mock_post.call_args[0][0]
        assert "Split this PR" in body
        assert "PR_SIZE_LIMIT" in body

    def test_size_skip_body_carries_the_review_marker(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Without the marker the comment escapes stale-comment minimization."""
        self._set_size_env(monkeypatch)
        with (
            patch("aggregate_reviews.post_verdict") as mock_post,
            pytest.raises(SystemExit),
        ):
            main()
        assert mock_post.call_args[0][0].startswith(REVIEW_MARKER)

    def test_size_skip_never_reads_reviewer_artifacts(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The whole point is that no reviewer ran, so none can be waited on."""
        self._set_size_env(monkeypatch)
        with (
            patch("aggregate_reviews.load_reviews") as mock_load,
            patch("aggregate_reviews.post_verdict"),
            pytest.raises(SystemExit),
        ):
            main()
        mock_load.assert_not_called()

    def test_missing_numbers_degrade_to_unknown(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A blank measurement must still block, not crash or pass silently."""
        monkeypatch.setenv("SIZE_SKIPPED", "true")
        monkeypatch.setenv("SIZE_TOTAL", "")
        monkeypatch.setenv("SIZE_LIMIT", "")
        monkeypatch.setenv("PR_NUMBER", "42")
        monkeypatch.setenv("GITHUB_REPOSITORY", "owner/repo")
        with (
            patch("aggregate_reviews.post_verdict") as mock_post,
            pytest.raises(SystemExit) as excinfo,
        ):
            main()
        assert excinfo.value.code == 1
        assert "unknown" in mock_post.call_args[0][0]

    @pytest.mark.parametrize("value", ["false", "", "False", "no"])
    def test_normal_size_takes_the_normal_path(
        self, monkeypatch: pytest.MonkeyPatch, value: str
    ) -> None:
        """Regression: PRs under the limit must be unaffected."""
        monkeypatch.setenv("SIZE_SKIPPED", value)
        assert _size_skip_details() is None

    def test_unset_takes_the_normal_path(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Consumers pinned to an older tag send no size inputs at all."""
        monkeypatch.delenv("SIZE_SKIPPED", raising=False)
        assert _size_skip_details() is None

    def test_unset_still_reaches_the_reviewer_pipeline(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """End-to-end regression guard for the untouched path."""
        monkeypatch.delenv("SIZE_SKIPPED", raising=False)
        for name in REVIEWER_NAMES:
            monkeypatch.setenv(f"REVIEWER_RESULT_{name.upper()}", "success")
        monkeypatch.setenv("PR_NUMBER", "42")
        monkeypatch.setenv("GITHUB_REPOSITORY", "owner/repo")
        with (
            patch(
                "aggregate_reviews.load_reviews",
                return_value={n: None for n in REVIEWER_NAMES},
            ) as mock_load,
            patch("aggregate_reviews.post_verdict") as mock_post,
        ):
            main()
        mock_load.assert_called_once()
        assert mock_post.call_args[0][1] == "approve"

    def test_summary_is_ascii(self) -> None:
        """Public-repo invariant, asserted at the point the string is built."""
        format_size_skip_summary("4200", "3000").encode("ascii")


class TestAggregateJobIsNotSizeGated:
    """The orchestrator must never gate the aggregate job on the size skip.

    This is the defect itself: a required context that is not reported stays
    Pending forever. The reviewer jobs stay gated -- that is the cost saving.
    """

    @staticmethod
    def _jobs() -> dict[str, Any]:
        return dict(
            yaml.safe_load(_ORCHESTRATOR_WORKFLOW.read_text(encoding="utf-8"))["jobs"]
        )

    def test_aggregate_does_not_reference_the_skip_output(self) -> None:
        condition = str(self._jobs()["aggregate"].get("if", ""))
        assert "outputs.skip" not in condition

    def test_every_reviewer_job_still_references_the_skip_output(self) -> None:
        jobs = self._jobs()
        reviewers = [name for name in jobs if name.startswith("review-")]
        assert reviewers, "no reviewer jobs found -- selector is stale"
        for name in reviewers:
            assert "outputs.skip" in str(jobs[name].get("if", "")), name

    def test_aggregate_receives_the_size_inputs(self) -> None:
        supplied = dict(self._jobs()["aggregate"].get("with", {}))
        for key in ("size_skipped", "size_total", "size_limit"):
            assert key in supplied, key


_PREPARE_WORKFLOW = (
    Path(__file__).resolve().parents[2] / "workflows" / "base-ai-review-prepare.yml"
)
_AGGREGATE_WORKFLOW = (
    Path(__file__).resolve().parents[2] / "workflows" / "base-ai-review-aggregate.yml"
)


def _workflow(path: Path) -> dict[str, Any]:
    return dict(yaml.safe_load(path.read_text(encoding="utf-8")))


def _guard_script(path: Path) -> str:
    """The run: body of the tree/diff guard in a reviewer-side workflow."""
    job = next(iter(_workflow(path)["jobs"].values()))
    for step in job["steps"]:
        if step.get("name") == "Confirm the tree matches the diff":
            return str(step["run"])
    raise AssertionError(f"guard step not found in {path.name}")


class TestReviewTreeMatchesTheDiff:
    """The tree the reviewers read must be the commit the diff is about.

    On workflow_dispatch there is no pull_request payload, so a checkout of
    `github.event.pull_request.head.sha || github.ref` resolves the dispatched
    ref -- `main` unless the caller passed --ref. The diff stayed correct
    because prepare resolves the head through the API, so a reviewer read one
    tree and reasoned about another tree's diff, and the run reported success
    (AT-2038).
    """

    def test_prepare_publishes_the_head_it_resolved(self) -> None:
        wf = _workflow(_PREPARE_WORKFLOW)
        assert "head_sha" in wf[True]["workflow_call"]["outputs"]
        assert "head_sha" in wf["jobs"]["prepare"]["outputs"]

    def test_every_reviewer_side_job_is_given_that_head(self) -> None:
        jobs = _workflow(_ORCHESTRATOR_WORKFLOW)["jobs"]
        callers = [n for n, j in jobs.items() if "uses" in j and n != "prepare"]
        assert callers, "no reusable-calling jobs found -- selector is stale"
        for name in callers:
            supplied = dict(jobs[name].get("with") or {})
            assert "head_sha" in supplied, name

    def test_prepare_is_not_given_its_own_output(self) -> None:
        """It produces head_sha; consuming it would be a cycle."""
        supplied = dict(_workflow(_ORCHESTRATOR_WORKFLOW)["jobs"]["prepare"].get("with") or {})
        assert "head_sha" not in supplied

    @pytest.mark.parametrize("path", [_SINGLE_WORKFLOW, _AGGREGATE_WORKFLOW])
    def test_checkout_prefers_the_resolved_head(self, path: Path) -> None:
        job = next(iter(_workflow(path)["jobs"].values()))
        checkout = next(
            s for s in job["steps"] if str(s.get("name", "")).startswith("Checkout caller repo")
        )
        ref = str(checkout["with"]["ref"])
        assert "inputs.head_sha" in ref
        assert ref.index("inputs.head_sha") < ref.index("github.ref")

    @pytest.mark.parametrize("path", [_SINGLE_WORKFLOW, _AGGREGATE_WORKFLOW])
    def test_guard_passes_when_the_tree_matches(self, path: Path, tmp_path: Path) -> None:
        head = _git_repo_at(tmp_path)
        assert _run_guard(_guard_script(path), tmp_path, head) == 0

    @pytest.mark.parametrize("path", [_SINGLE_WORKFLOW, _AGGREGATE_WORKFLOW])
    def test_guard_fails_when_the_tree_is_a_different_commit(
        self, path: Path, tmp_path: Path
    ) -> None:
        _git_repo_at(tmp_path)
        other = "0" * 40
        assert _run_guard(_guard_script(path), tmp_path, other) == 1

    @pytest.mark.parametrize("path", [_SINGLE_WORKFLOW, _AGGREGATE_WORKFLOW])
    def test_guard_allows_the_size_skip_path(self, path: Path, tmp_path: Path) -> None:
        """prepare never resolves a head when it skips, so empty must pass."""
        _git_repo_at(tmp_path)
        assert _run_guard(_guard_script(path), tmp_path, "") == 0


def _git_repo_at(root: Path) -> str:
    env = {"PATH": os.environ["PATH"], "HOME": str(root),
           "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    (root / "f").write_text("x", encoding="utf-8")
    for args in (["init", "-q"], ["add", "f"], ["commit", "-qm", "c"]):
        subprocess.run(["git", *args], cwd=root, env=env, check=True,
                       capture_output=True, timeout=30)
    out = subprocess.run(["git", "rev-parse", "HEAD"], cwd=root, env=env,
                         check=True, capture_output=True, text=True, timeout=30)
    return out.stdout.strip()


def _run_guard(script: str, cwd: Path, head_sha: str) -> int:
    return subprocess.run(
        ["bash", "-c", script], cwd=cwd,
        env={"PATH": os.environ["PATH"], "HEAD_SHA": head_sha},
        capture_output=True, timeout=30,
        check=False,  # the exit code IS the assertion here
    ).returncode


class TestPrepareFailureVerdict:
    """A run whose prepare job failed must produce a visible, actionable failure.

    AT-1975 took the aggregate out of the size gate; the `prepare.result ==
    'success'` gate stayed, so every other way prepare can die -- invalid
    PR_SIZE_LIMIT, unresolvable head, the AT-2038 tree/diff assert, a checkout
    or API error -- still produced no context at all and left the required
    check Pending forever (AT-2087).
    """

    @staticmethod
    def _set_prepare_env(
        monkeypatch: pytest.MonkeyPatch,
        result: str = "failure",
        run_url: str = "https://github.example/o/r/actions/runs/9",
    ) -> None:
        monkeypatch.setenv("PREPARE_RESULT", result)
        monkeypatch.setenv("RUN_URL", run_url)
        monkeypatch.setenv("PR_NUMBER", "42")
        monkeypatch.setenv("GITHUB_REPOSITORY", "owner/repo")

    def test_prepare_failure_exits_nonzero(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Blocking is the point: no reviewer ran, so nothing vouched for this PR."""
        self._set_prepare_env(monkeypatch)
        with (
            patch("aggregate_reviews.post_verdict") as mock_post,
            pytest.raises(SystemExit) as excinfo,
        ):
            main()
        assert excinfo.value.code == 1
        assert mock_post.call_args[0][1] == "request_changes"

    def test_prepare_failure_names_the_job_to_open(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A bare "prepare failed" is not actionable -- name a place to look."""
        self._set_prepare_env(monkeypatch)
        with (
            patch("aggregate_reviews.post_verdict") as mock_post,
            pytest.raises(SystemExit),
        ):
            main()
        body = mock_post.call_args[0][0]
        assert "Prepare Review Context" in body
        assert "https://github.example/o/r/actions/runs/9" in body

    def test_prepare_failure_names_the_known_causes(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The log says what broke; the body says what the candidates are."""
        self._set_prepare_env(monkeypatch)
        with (
            patch("aggregate_reviews.post_verdict") as mock_post,
            pytest.raises(SystemExit),
        ):
            main()
        body = mock_post.call_args[0][0]
        assert "PR_SIZE_LIMIT" in body
        assert "re-run" in body

    def test_prepare_failure_reports_the_result_verbatim(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The result is quoted, not classified -- it saves a wrong-cause hunt.

        This asserted on `cancelled` until AT-2092, when the guard that
        suppresses a superseded run took that value over. `skipped` replaces
        it only as a value distinct from `failure` -- prepare is a root job
        with no `if:`, so it can never actually report `skipped`. What is
        under test is that the result is quoted rather than classified, and
        any non-success value shows that.
        """
        self._set_prepare_env(monkeypatch, result="skipped")
        with (
            patch("aggregate_reviews.post_verdict") as mock_post,
            pytest.raises(SystemExit),
        ):
            main()
        assert "skipped" in mock_post.call_args[0][0]

    def test_prepare_failure_carries_the_review_marker(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Without the marker the comment escapes stale-comment minimization."""
        self._set_prepare_env(monkeypatch)
        with (
            patch("aggregate_reviews.post_verdict") as mock_post,
            pytest.raises(SystemExit),
        ):
            main()
        assert mock_post.call_args[0][0].startswith(REVIEW_MARKER)

    def test_prepare_failure_never_reads_reviewer_artifacts(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The reviewer jobs are skipped when prepare dies, so none can be awaited."""
        self._set_prepare_env(monkeypatch)
        with (
            patch("aggregate_reviews.load_reviews") as mock_load,
            patch("aggregate_reviews.post_verdict"),
            pytest.raises(SystemExit),
        ):
            main()
        mock_load.assert_not_called()

    def test_prepare_failure_survives_an_empty_run_url(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A caller on an older tag sends no RUN_URL; the verdict must still post."""
        self._set_prepare_env(monkeypatch, run_url="")
        with (
            patch("aggregate_reviews.post_verdict") as mock_post,
            pytest.raises(SystemExit),
        ):
            main()
        assert "Prepare Review Context" in mock_post.call_args[0][0]

    def test_prepare_failure_precedes_the_size_path(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A failed prepare publishes no size numbers, so the size body cannot apply."""
        self._set_prepare_env(monkeypatch)
        monkeypatch.setenv("SIZE_SKIPPED", "")
        monkeypatch.setenv("SIZE_TOTAL", "")
        monkeypatch.setenv("SIZE_LIMIT", "")
        with (
            patch("aggregate_reviews.post_verdict") as mock_post,
            pytest.raises(SystemExit),
        ):
            main()
        body = mock_post.call_args[0][0]
        assert "prepare job reported" in body
        assert "PR too large" not in body

    def test_missing_pr_number_still_fails_the_job(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """No PR number means no comment -- but the context must still be red.

        A check that fails with the explanation only in the runner log is
        strictly better than a check that never appears. post_verdict already
        exits 1 on an unusable PR_NUMBER; this pins that the prepare-failure
        path reaches it rather than returning 0.
        """
        monkeypatch.setenv("PREPARE_RESULT", "failure")
        monkeypatch.delenv("PR_NUMBER", raising=False)
        monkeypatch.setenv("GITHUB_REPOSITORY", "owner/repo")
        with (
            patch("aggregate_reviews.load_reviews") as mock_load,
            pytest.raises(SystemExit) as excinfo,
        ):
            main()
        assert excinfo.value.code == 1
        mock_load.assert_not_called()

    @pytest.mark.parametrize("value", ["success", "", "SUCCESS", "  success  "])
    def test_successful_prepare_takes_the_normal_path(
        self, monkeypatch: pytest.MonkeyPatch, value: str
    ) -> None:
        """Regression: the untouched path must stay untouched."""
        monkeypatch.setenv("PREPARE_RESULT", value)
        assert _prepare_failure_result() is None

    def test_unset_takes_the_normal_path(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Consumers pinned to an older tag send no prepare_result at all."""
        monkeypatch.delenv("PREPARE_RESULT", raising=False)
        assert _prepare_failure_result() is None

    def test_unset_still_reaches_the_reviewer_pipeline(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """End-to-end regression guard for the normal path."""
        monkeypatch.delenv("PREPARE_RESULT", raising=False)
        monkeypatch.delenv("SIZE_SKIPPED", raising=False)
        for name in REVIEWER_NAMES:
            monkeypatch.setenv(f"REVIEWER_RESULT_{name.upper()}", "success")
        monkeypatch.setenv("PR_NUMBER", "42")
        monkeypatch.setenv("GITHUB_REPOSITORY", "owner/repo")
        with (
            patch(
                "aggregate_reviews.load_reviews",
                return_value={n: None for n in REVIEWER_NAMES},
            ) as mock_load,
            patch("aggregate_reviews.post_verdict") as mock_post,
        ):
            main()
        mock_load.assert_called_once()
        assert mock_post.call_args[0][1] == "approve"

    def test_summary_is_ascii(self) -> None:
        """Public-repo invariant, asserted at the point the string is built."""
        format_prepare_failure_summary("failure", "https://x/y").encode("ascii")


class TestAggregateJobIsNotPrepareGated:
    """The orchestrator must not gate the aggregate job on prepare's result.

    Same defect as TestAggregateJobIsNotSizeGated, different door: a required
    context that is not reported stays Pending forever. v1.6.0 widened the hole
    by adding the AT-2038 tree/diff assert as a new way for prepare to fail.
    """

    @staticmethod
    def _aggregate() -> dict[str, Any]:
        return dict(_workflow(_ORCHESTRATOR_WORKFLOW)["jobs"]["aggregate"])

    def test_aggregate_does_not_reference_the_prepare_result(self) -> None:
        condition = str(self._aggregate().get("if", ""))
        assert "prepare.result" not in condition

    def test_aggregate_does_not_gate_on_the_reviewer_jobs(self) -> None:
        """They are skipped whenever prepare skips or dies, and that is correct."""
        condition = str(self._aggregate().get("if", ""))
        assert "review-" not in condition

    def test_aggregate_condition_suppresses_the_implicit_success_check(self) -> None:
        """A plain condition would re-gate the job on every upstream job.

        GitHub applies an implicit success() unless the expression contains one
        of always/cancelled/failure/success -- losing that word would restore
        the hole from the other side.
        """
        condition = str(self._aggregate().get("if", ""))
        assert any(
            fn in condition
            for fn in ("always()", "cancelled()", "failure()", "success()")
        ), condition

    def test_aggregate_still_runs_after_a_cancellation(self) -> None:
        """Cancellation must be guarded inside the job, never on the job.

        `!cancelled()` skips the aggregate, and AT-1967 Phase 0 measured what a
        skipped aggregate reports: the two-part name `review / aggregate`, not
        the three-part `review / aggregate / Aggregate & Verdict` the consumer
        rulesets require. The required context is then never reported at all
        and the PR sits Pending forever -- not the Success GitHub documents for
        a job skipped by its `if:`. The posting is stopped in the script
        instead (TestSupersededHeadPostsNoVerdict).
        """
        condition = str(self._aggregate().get("if", ""))
        assert "always()" in condition, condition
        assert "cancelled" not in condition, condition

    def test_aggregate_receives_the_prepare_result(self) -> None:
        """Not gating on it is only half: the verdict has to be able to say it."""
        assert "prepare_result" in dict(self._aggregate().get("with", {}))

    def test_aggregate_workflow_forwards_it_to_the_script(self) -> None:
        """A declared input nothing reads would render an empty verdict."""
        job = next(iter(_workflow(_AGGREGATE_WORKFLOW)["jobs"].values()))
        step = next(
            s for s in job["steps"] if s.get("name") == "Aggregate and post verdict"
        )
        env = dict(step.get("env", {}))
        assert "inputs.prepare_result" in str(env.get("PREPARE_RESULT", ""))
        assert "run_id" in str(env.get("RUN_URL", ""))

    def test_aggregate_workflow_forwards_the_head_it_reviewed(self) -> None:
        """The staleness guard compares it; unforwarded, nothing is stale."""
        job = next(iter(_workflow(_AGGREGATE_WORKFLOW)["jobs"].values()))
        step = next(
            s for s in job["steps"] if s.get("name") == "Aggregate and post verdict"
        )
        assert "inputs.head_sha" in str(dict(step.get("env", {})).get("HEAD_SHA", ""))
        assert "head_sha" in dict(self._aggregate().get("with", {}))

    def test_aggregate_workflow_forwards_every_reviewer_result(self) -> None:
        """The verdict names who died; an unforwarded one is a silent gap."""
        job = next(iter(_workflow(_AGGREGATE_WORKFLOW)["jobs"].values()))
        step = next(
            s for s in job["steps"] if s.get("name") == "Aggregate and post verdict"
        )
        env = dict(step.get("env", {}))
        supplied = dict(self._aggregate().get("with", {}))
        for name in REVIEWER_NAMES:
            assert f"{name}_result" in supplied, name
            assert f"inputs.{name}_result" in str(
                env.get(f"REVIEWER_RESULT_{name.upper()}", "")
            ), name


class TestSupersededHeadPostsNoVerdict:
    """A run whose head has moved on must reach the aggregate and post nothing.

    cancel-in-progress kills the previous run on every new commit. The job has
    to keep running -- its name is the required status context and a skipped
    job never reports the three-part name (AT-1967 Phase 0) -- so the guard is
    here, in the script, not on the job's `if:` (AT-2092).

    The guard is head staleness, not cancellation. Reviewer conclusions cannot
    tell a cancelled run from a dead reviewer: a cancel lands wherever the
    reviewers happen to be and produces every mixture, so the four shapes
    below are all reachable from one cancel. Each is asserted twice, once with
    a superseded head and once with a current one.
    """

    _REVIEWED = "a" * 40
    _NEWER = "b" * 40

    # Every mixture of reviewer conclusions one cancel can leave behind.
    # Live timings (gemini 18s, codex 40s, claude 1m18s) put a cancel in the
    # ~60s window on the second or third of these; two of the three cancelled
    # runs in this repo's history landed on the fourth.
    _SHAPES = [
        pytest.param(["cancelled", "cancelled", "cancelled"], id="all-cancelled"),
        pytest.param(["cancelled", "success", "success"], id="one-cancelled"),
        pytest.param(["cancelled", "cancelled", "success"], id="two-cancelled"),
        pytest.param(["success", "success", "success"], id="none-cancelled"),
        pytest.param(["cancelled", "skipped", "skipped"], id="sequential-mode"),
    ]

    @classmethod
    def _set_env(
        cls,
        monkeypatch: pytest.MonkeyPatch,
        results: list[str],
        head_sha: str | None = None,
    ) -> None:
        monkeypatch.setenv("PREPARE_RESULT", "success")
        monkeypatch.setenv("PR_NUMBER", "42")
        monkeypatch.setenv("GITHUB_REPOSITORY", "owner/repo")
        monkeypatch.delenv("SIZE_SKIPPED", raising=False)
        if head_sha is None:
            monkeypatch.delenv("HEAD_SHA", raising=False)
        else:
            monkeypatch.setenv("HEAD_SHA", head_sha)
        for name, value in zip(REVIEWER_NAMES, results):
            monkeypatch.setenv(f"REVIEWER_RESULT_{name.upper()}", value)

    @staticmethod
    def _head_lookup(sha: str = "", returncode: int = 0) -> Any:
        """Patch the one `gh api` call the guard makes."""
        return patch(
            "aggregate_reviews.subprocess.run",
            return_value=subprocess.CompletedProcess(
                args=[], returncode=returncode, stdout=sha, stderr=""
            ),
        )

    @pytest.mark.parametrize("results", _SHAPES)
    def test_a_superseded_head_posts_nothing(
        self, monkeypatch: pytest.MonkeyPatch, results: list[str]
    ) -> None:
        """One rule covers every shape a cancel can leave behind."""
        self._set_env(monkeypatch, results, head_sha=self._REVIEWED)
        with (
            self._head_lookup(self._NEWER),
            patch("aggregate_reviews.post_verdict") as mock_post,
            pytest.raises(SystemExit) as excinfo,
        ):
            main()
        mock_post.assert_not_called()
        assert excinfo.value.code == 1

    @pytest.mark.parametrize("results", _SHAPES)
    def test_a_current_head_still_posts_its_verdict(
        self, monkeypatch: pytest.MonkeyPatch, results: list[str]
    ) -> None:
        """Manual cancel, dead reviewer, timeout: indistinguishable, and this
        run still owes the PR the honest partial verdict naming who died
        (AT-1837), in either review mode.
        """
        self._set_env(monkeypatch, results, head_sha=self._REVIEWED)
        with (
            self._head_lookup(self._REVIEWED),
            patch(
                "aggregate_reviews.load_reviews",
                return_value={n: None for n in REVIEWER_NAMES},
            ),
            patch("aggregate_reviews.post_verdict") as mock_post,
        ):
            try:
                main()
            except SystemExit:
                pass
        mock_post.assert_called_once()

    def test_a_superseded_head_never_reads_reviewer_artifacts(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Every download is continue-on-error, so nothing else stops it."""
        self._set_env(
            monkeypatch, ["cancelled"] * 3, head_sha=self._REVIEWED
        )
        with (
            self._head_lookup(self._NEWER),
            patch("aggregate_reviews.load_reviews") as mock_load,
            pytest.raises(SystemExit),
        ):
            main()
        mock_load.assert_not_called()

    def test_a_superseded_head_does_not_report_success(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Exit 0 would be a green required check for an unreviewed commit."""
        self._set_env(
            monkeypatch, ["success"] * 3, head_sha=self._REVIEWED
        )
        with self._head_lookup(self._NEWER), pytest.raises(SystemExit) as excinfo:
            main()
        assert excinfo.value.code != 0

    @pytest.mark.parametrize(
        "kwargs",
        [
            pytest.param({"returncode": 1}, id="api-error"),
            pytest.param({"sha": ""}, id="empty-response"),
        ],
    )
    def test_an_undeterminable_head_still_posts(
        self, monkeypatch: pytest.MonkeyPatch, kwargs: dict[str, Any]
    ) -> None:
        """Rate limit, network, a deleted PR: fail toward posting.

        A redundant verdict is visible and the live run's supersedes it; a
        dropped one is the silence this file exists to prevent.
        """
        self._set_env(monkeypatch, ["success"] * 3, head_sha=self._REVIEWED)
        with (
            self._head_lookup(**kwargs),
            patch(
                "aggregate_reviews.load_reviews",
                return_value={n: None for n in REVIEWER_NAMES},
            ),
            patch("aggregate_reviews.post_verdict") as mock_post,
        ):
            main()
        mock_post.assert_called_once()

    def test_a_timed_out_head_lookup_still_posts(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Same direction for the exception path, which returns no result."""
        self._set_env(monkeypatch, ["success"] * 3, head_sha=self._REVIEWED)
        with patch(
            "aggregate_reviews.subprocess.run",
            side_effect=subprocess.TimeoutExpired(cmd="gh", timeout=1),
        ):
            assert _head_is_stale() is False

    def test_no_head_is_not_stale(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """prepare resolved none -- it failed (AT-2087) or skipped (AT-1975).

        Both still owe the PR a verdict, and there is nothing to compare.
        """
        self._set_env(monkeypatch, ["skipped"] * 3, head_sha=None)
        with patch("aggregate_reviews.subprocess.run") as mock_run:
            assert _head_is_stale() is False
        mock_run.assert_not_called()

    def test_failed_prepare_still_posts_its_verdict(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Regression guard for AT-2087, merged in the same PR as the defect."""
        self._set_env(monkeypatch, ["skipped"] * 3, head_sha=None)
        monkeypatch.setenv("PREPARE_RESULT", "failure")
        with (
            patch("aggregate_reviews.post_verdict") as mock_post,
            pytest.raises(SystemExit),
        ):
            main()
        assert mock_post.call_args[0][1] == "request_changes"
        assert "prepare job reported" in mock_post.call_args[0][0]

    def test_size_skip_still_posts_its_verdict(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Regression guard for AT-1975: prepare succeeds, reviewers skip."""
        self._set_env(monkeypatch, ["skipped"] * 3, head_sha=None)
        monkeypatch.setenv("SIZE_SKIPPED", "true")
        monkeypatch.setenv("SIZE_TOTAL", "5000")
        monkeypatch.setenv("SIZE_LIMIT", "3000")
        with (
            patch("aggregate_reviews.post_verdict") as mock_post,
            pytest.raises(SystemExit),
        ):
            main()
        assert "PR too large" in mock_post.call_args[0][0]

    def test_the_head_is_compared_after_normalization(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`gh` returns a trailing newline; a raw compare would call it stale."""
        self._set_env(monkeypatch, ["success"] * 3, head_sha=self._REVIEWED)
        with self._head_lookup(self._REVIEWED + "\n"):
            assert _head_is_stale() is False

    def test_an_unusable_pr_number_is_not_stale(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """No PR to ask about -- post_verdict reports that failure itself."""
        self._set_env(monkeypatch, ["success"] * 3, head_sha=self._REVIEWED)
        monkeypatch.delenv("PR_NUMBER", raising=False)
        with patch("aggregate_reviews.subprocess.run") as mock_run:
            assert _head_is_stale() is False
        mock_run.assert_not_called()
