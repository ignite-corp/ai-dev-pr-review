"""Tests for aggregate_reviews severity normalization and validation."""

from __future__ import annotations

import json
import logging
import os
import re
import shlex
import shutil
import subprocess
from pathlib import Path
from collections.abc import Sequence
from typing import Any
from unittest.mock import patch

import pytest
import yaml

from review_status import stamp_model_status, write_verdict_json
from aggregate_reviews import (
    _count_unreviewed_majors,
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
    _policy_skip_details,
    apply_verdict_rules,
    format_policy_skip_summary,
    format_prepare_failure_summary,
    format_size_skip_summary,
    format_summary,
    load_reviews,
    main,
    post_verdict,
    FAILED_DETAIL_PREFIX,
    REVIEWER_NAMES,
    SizeBreach,
    REVIEWERS,
    _STALE_COMMENTS_QUERY,
    _STALE_REVIEWS_QUERY,
)
from github_pr_support import REVIEW_MARKER, normalize_bot_login

# The two reasons _check_insufficient can return when nothing is available.
# Spelled out here rather than imported so a change to either prose breaks the
# assertions that discriminate the paths instead of travelling with them.
_INDETERMINATE_REASON = (
    f"0/{len(REVIEWER_NAMES)} LLM responses -- no reviewer output, cause indeterminate"
)
_ALL_FAILED_REASON = (
    f"0/{len(REVIEWER_NAMES)} LLM responses -- all failed, manual review required"
)


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


@pytest.fixture(autouse=True)
def _no_live_gh(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail any test in this module that calls ``subprocess.run`` on ``gh``.

    Every main() path in the aggregate is driven from this file, and every
    one of them can reach a `gh` call. A test that forgets to patch
    ``subprocess.run`` does not fail -- it quietly hits the live GitHub
    API, which is how ``test_main_hands_both_readers_the_same_count`` came
    to run `gh pr view 42 --json labels --repo owner/repo` against the
    real API on every CI run, and to error outright on a machine with no
    `gh` on PATH.

    A sweep someone remembers to run is not a check, so the ban lives here
    rather than in a script. Tests that patch
    ``aggregate_reviews.subprocess.run`` themselves override this for
    their own duration, which is the intended way to exercise a `gh`
    path. Non-`gh` shell-outs are delegated untouched -- several tests in
    the suite legitimately drive `jq` and `bash`.

    What it covers, exactly, because a guard whose docstring outruns its
    reach is the same defect one level up: calls through
    ``subprocess.run``, in either spelling -- an argv sequence or a single
    command string, which is split rather than compared whole -- and
    ``gh`` named by bare name or by absolute path. ``subprocess.check_output``
    and ``subprocess.call`` route through ``run`` and so are covered too.
    It does NOT cover ``subprocess.Popen`` used directly, nor a module that
    holds its own reference to ``run`` taken before this fixture applies.
    Neither is reachable from ``aggregate_reviews`` today, which calls only
    ``subprocess.run``; a change to either would need this widened with it.
    """
    real = subprocess.run

    def guard(cmd: Any, *args: Any, **kwargs: Any) -> Any:
        if isinstance(cmd, (list, tuple)):
            argv = [str(c) for c in cmd]
        else:
            # A string command is the same call in the other spelling.
            # Comparing it whole made basename() see "gh pr view ..." and
            # let it past; splitting is what makes the two spellings one
            # check.
            try:
                argv = shlex.split(str(cmd))
            except ValueError:
                argv = [str(cmd)]
        if argv and os.path.basename(argv[0]) == "gh":
            raise AssertionError(
                "test reached the live GitHub API: "
                + " ".join(argv)
                + " -- patch aggregate_reviews.subprocess.run in this test"
            )
        return real(cmd, *args, **kwargs)

    monkeypatch.setattr(subprocess, "run", guard)


def test_no_live_gh_fixture_is_armed() -> None:
    """Positive control for ``_no_live_gh``.

    Without this, a green suite is indistinguishable from a fixture that
    silently stopped matching -- a rename of the binary, a change to the
    argv shape, or an autouse registration that no longer applies.
    """
    with pytest.raises(AssertionError, match="live GitHub API"):
        subprocess.run(["gh", "pr", "view", "1"])
    # A string command is the same call in the other spelling, and the
    # naive normalisation made the whole string argv[0], so basename()
    # compared "gh pr view 1" against "gh" and let it through.
    with pytest.raises(AssertionError, match="live GitHub API"):
        subprocess.run("gh pr view 1")
    # An absolute path is still gh.
    with pytest.raises(AssertionError, match="live GitHub API"):
        subprocess.run(["/usr/bin/gh", "pr", "view", "1"])
    # ...and it must not break the non-gh shell-outs other tests rely on,
    # in either spelling.
    done = subprocess.run(["echo", "ok"], capture_output=True, text=True)
    assert done.stdout.strip() == "ok"
    done = subprocess.run("echo ok", shell=True, capture_output=True, text=True)
    assert done.stdout.strip() == "ok"


def _two_majors_no_consensus() -> dict[str, Any]:
    """A full roster whose majors reach verdict "approve".

    Both majors come from one reviewer on different files:
    _check_major_consensus needs two reviewers on one file, so this is the
    measured no-consensus shape. Module level because two classes build it
    -- the withhold gate and the one-count contract.
    """
    names = list(REVIEWER_NAMES)
    reviews: dict[str, Any] = {n: _make_named_review(n, []) for n in names}
    reviews[names[0]] = _make_named_review(
        names[0],
        [
            _make_issue(severity="major", file="a.py", description="first major"),
            _make_issue(severity="major", file="b.py", description="second major"),
        ],
    )
    return reviews


def _post_and_capture(
    monkeypatch: pytest.MonkeyPatch,
    verdict: str = "approve",
    *,
    approve_quorum: bool = True,
    unreviewed_majors: int = 0,
) -> list[list[str]]:
    """Drive post_verdict with the gh calls recorded instead of run.

    One scaffold for both withhold gates: they set the same env, record
    with the same fake, patch the same pair, and differ only in which of
    post_verdict's keywords they vary -- quorum for AT-2511, the major
    count for AT-2823 -- so each passes its own axis and takes the
    default for the other.
    """
    monkeypatch.setenv("PR_NUMBER", "42")
    monkeypatch.setenv("GITHUB_REPOSITORY", "owner/repo")
    monkeypatch.setenv("GH_TOKEN", "default-token")

    commands: list[list[str]] = []

    def fake_run(cmd: list[str], *args: Any, **kwargs: Any) -> Any:
        commands.append(cmd)
        return type("R", (), {"returncode": 0, "stderr": "", "stdout": ""})()

    with (
        patch("aggregate_reviews._fold_prior_round_items"),
        patch("aggregate_reviews.subprocess.run", side_effect=fake_run),
    ):
        post_verdict(
            "body",
            verdict,
            comment_only=False,
            approve_quorum=approve_quorum,
            unreviewed_majors=unreviewed_majors,
        )
    return commands


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


class TestAllAbsentIsIndeterminate:
    """all-success + 0-available -> comment, cause indeterminate (AT-2569);
    1-failure + 0-available -> comment, all failed."""

    def _empty_reviews(self) -> dict[str, dict[str, Any] | None]:
        return {name: None for name in REVIEWER_NAMES}

    def test_all_success_no_payload_does_not_approve(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # All REVIEWER_RESULT_* = "success", zero review files -> the
        # indeterminate 0-response path. Nothing was reviewed, and the same
        # signature is a credential outage, so it must not approve.
        for name in REVIEWER_NAMES:
            monkeypatch.setenv(f"REVIEWER_RESULT_{name.upper()}", "success")
        verdict, reason, _ = apply_verdict_rules(self._empty_reviews())
        assert verdict == "comment"
        assert reason == _INDETERMINATE_REASON

    def test_one_failure_no_payload_returns_comment(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # One reviewer failed -> the indeterminate path must not fire.
        names = list(REVIEWER_NAMES)
        monkeypatch.setenv(f"REVIEWER_RESULT_{names[0].upper()}", "failure")
        for name in names[1:]:
            monkeypatch.setenv(f"REVIEWER_RESULT_{name.upper()}", "success")
        verdict, reason, _ = apply_verdict_rules(self._empty_reviews())
        assert verdict == "comment"
        assert reason == _ALL_FAILED_REASON

    def test_missing_env_var_disqualifies_indeterminate(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Empty/missing env var means unknown conclusion -> the
        # indeterminate path must not fire.
        for name in REVIEWER_NAMES:
            monkeypatch.delenv(f"REVIEWER_RESULT_{name.upper()}", raising=False)
        verdict, _, _ = apply_verdict_rules(self._empty_reviews())
        assert verdict == "comment"


class TestAllAbsentRoundFailsCi:
    """AT-2569: a round with every job green and no valid payload fails CI.

    Driven through main() with the real load_reviews reading the working
    directory, so the exit code and the posted verdict are the ones the
    aggregate job would produce. The same signature is what a credential
    outage produces, so the round must neither approve nor end green.
    """

    def _run_main(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> tuple[int, str, str]:
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("GITHUB_OUTPUT", str(tmp_path / "gh-output"))
        monkeypatch.setenv("PR_NUMBER", "1")
        monkeypatch.setenv("GITHUB_REPOSITORY", "owner/repo")
        for name in REVIEWER_NAMES:
            monkeypatch.setenv(f"REVIEWER_RESULT_{name.upper()}", "success")
        code = 0
        with patch("aggregate_reviews.post_verdict") as mock_post:
            try:
                main()
            except SystemExit as exc:
                code = int(exc.code or 0)
        comment, verdict = mock_post.call_args[0][:2]
        return code, verdict, comment

    def test_nothing_loaded_exits_1_with_comment_verdict(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        code, verdict, comment = self._run_main(monkeypatch, tmp_path)
        assert (code, verdict) == (1, "comment")
        assert _INDETERMINATE_REASON in comment

    def test_three_schema_invalid_payloads_exit_1_with_comment_verdict(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # Files exist but fail _is_valid_review, so load_reviews yields None
        # for each: the same round as no files at all.
        for name in REVIEWER_NAMES:
            (tmp_path / REVIEWERS[name]).write_text('{"issues": 5}', encoding="utf-8")
        code, verdict, comment = self._run_main(monkeypatch, tmp_path)
        assert (code, verdict) == (1, "comment")
        assert _INDETERMINATE_REASON in comment


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

    def test_all_error_payloads_yield_comment_not_approve(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Jobs conclude "success" (continue-on-error) but every reviewer wrote
        # an error payload -> the indeterminate path must not fire.
        for name in REVIEWER_NAMES:
            monkeypatch.setenv(f"REVIEWER_RESULT_{name.upper()}", "success")
        verdict, reason, _ = apply_verdict_rules(_error_payloads())
        assert verdict == "comment"
        assert reason == _ALL_FAILED_REASON


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
        # but error payloads exist -> no bypass, CI must fail.
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


class TestMainAllAbsentExitsNonzero:
    """main() fails CI when all reviewer jobs succeeded but none responded.

    The indeterminate 0-response path used to post approve and exit 0 (R4);
    since AT-2569 it posts comment and exits 1, because the same signature
    is what a credential outage produces.
    """

    def test_main_all_absent_exits_nonzero(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # All REVIEWER_RESULT_* = "success", no review files -> the
        # indeterminate 0-response path -> exit 1.
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
            pytest.raises(SystemExit) as excinfo,
        ):
            main()

        assert excinfo.value.code == 1
        # The verdict is still posted before the exit.
        assert mock_post.call_count == 1
        call_args = mock_post.call_args
        posted_verdict = call_args[0][1]
        assert posted_verdict == "comment"


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
            patch("aggregate_reviews._fold_prior_round_items"),
            patch("aggregate_reviews.subprocess.run", side_effect=fake_run),
        ):
            post_verdict("body", verdict, comment_only=False, unreviewed_majors=0)

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
            patch("aggregate_reviews._fold_prior_round_items"),
            patch("aggregate_reviews.subprocess.run", side_effect=fake_run),
        ):
            post_verdict(
                "body",
                verdict,
                comment_only=comment_only,
                unreviewed_majors=0,
            )

        return commands

    def _has_review_request_changes(self, commands: list[list[str]]) -> bool:
        return any("review" in cmd and "--request-changes" in cmd for cmd in commands)

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

    def test_approve_without_quorum_posts_comment(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        commands = _post_and_capture(monkeypatch, "approve", approve_quorum=False)
        assert not any("review" in cmd for cmd in commands)
        assert any("comment" in cmd for cmd in commands)

    def test_approve_with_quorum_submits_review(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        commands = _post_and_capture(monkeypatch, "approve", approve_quorum=True)
        assert any("review" in cmd and "--approve" in cmd for cmd in commands)

    def test_request_changes_unaffected_by_quorum(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        commands = _post_and_capture(
            monkeypatch, "request_changes", approve_quorum=False
        )
        assert any("review" in cmd and "--request-changes" in cmd for cmd in commands)

    def test_main_zero_responses_withholds_formal_approval(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # 0 available payloads -> main() must request the comment
        # downgrade (approve_quorum=False) whatever the verdict.
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
            pytest.raises(SystemExit),
        ):
            main()

        assert mock_post.call_args[0][1] == "comment"
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


class TestUndispositionedMajorsGate:
    """AT-2823: majors nobody dispositioned withhold the formal approval.

    Measured on ignite-corp/ai-dev-infra-max#10: a complete 3/3 roster, two
    majors with no consensus, headline "Approved with 2 unreviewed major
    issue(s)" -- and an APPROVED review from the reviewer App over the top
    of it, which turns the PR page green and fills the ruleset's single
    required approval. A short roster already withholds (AT-2511); both
    states mean "this round is not finished", so both take the same branch.
    """

    def test_approve_with_majors_posts_comment(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        commands = _post_and_capture(monkeypatch, unreviewed_majors=2)
        assert not any("review" in cmd for cmd in commands)
        assert any("comment" in cmd for cmd in commands)

    def test_approve_without_majors_submits_review(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Positive control: the withhold is keyed on the major count, not on
        # approve itself -- a clean round still gets its formal approval.
        commands = _post_and_capture(monkeypatch, unreviewed_majors=0)
        assert any("review" in cmd and "--approve" in cmd for cmd in commands)

    def _run_main_recording_gh(
        self,
        monkeypatch: pytest.MonkeyPatch,
        reviews: dict[str, Any],
        *,
        allow_auto_approve: bool = True,
    ) -> list[tuple[list[str], str]]:
        """Drive main() with post_verdict REAL, recording the gh calls.

        Named apart from ``TestApproveQuorumGate._run_main``, which mocks
        post_verdict and returns the mock: these differ in what they
        leave running and in what they hand back, so one name for both
        would make a reader of either class expect the other's contract.
        Returns (argv, body) pairs, so a test can assert on what was
        posted as well as on whether a review went up at all.
        """
        monkeypatch.setenv("PR_NUMBER", "10")
        monkeypatch.setenv("GITHUB_REPOSITORY", "owner/repo")
        monkeypatch.setenv("GH_TOKEN", "default-token")
        monkeypatch.setenv("REVIEWER_TOKEN", "reviewer-app-token")
        monkeypatch.setenv(
            "ALLOW_AUTO_APPROVE", "true" if allow_auto_approve else "false"
        )
        monkeypatch.setenv("REVIEW_MODE", "parallel")
        for name in REVIEWER_NAMES:
            monkeypatch.setenv(f"REVIEWER_RESULT_{name.upper()}", "success")

        calls: list[tuple[list[str], str]] = []

        def fake_run(cmd: list[str], *args: Any, **kwargs: Any) -> Any:
            calls.append((cmd, kwargs.get("input", "")))
            return type("R", (), {"returncode": 0, "stderr": "", "stdout": ""})()

        with (
            patch("aggregate_reviews.load_reviews", return_value=reviews),
            patch("aggregate_reviews._fold_prior_round_items"),
            patch("aggregate_reviews.subprocess.run", side_effect=fake_run),
        ):
            main()
        return calls

    def test_main_full_roster_with_majors_withholds_approval(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls = self._run_main_recording_gh(monkeypatch, _two_majors_no_consensus())

        assert not any("review" in cmd for cmd, _ in calls), (
            "a formal APPROVED review must not be posted over undispositioned majors"
        )
        bodies = [body for cmd, body in calls if "comment" in cmd]
        assert len(bodies) == 1
        assert "Approved with 2 unreviewed major issue(s)" in bodies[0]
        assert "Auto-approve withheld" in bodies[0]
        assert "[OK]" not in bodies[0]

    def test_main_clean_round_still_approves(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Positive control through the same path: 3/3, no issues at all.
        reviews: dict[str, Any] = {n: _make_named_review(n, []) for n in REVIEWER_NAMES}
        calls = self._run_main_recording_gh(monkeypatch, reviews)

        assert any("review" in cmd and "--approve" in cmd for cmd, _ in calls)

    def test_main_minor_only_round_still_approves(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The withhold is keyed on majors, not on "issues exist": a round of
        # minors and suggestions is finished and keeps its approval.
        names = list(REVIEWER_NAMES)
        reviews: dict[str, Any] = {n: _make_named_review(n, []) for n in names}
        reviews[names[0]] = _make_named_review(
            names[0],
            [_make_issue(severity="minor"), _make_issue(severity="suggestion")],
        )
        calls = self._run_main_recording_gh(monkeypatch, reviews)

        assert any("review" in cmd and "--approve" in cmd for cmd, _ in calls)

    def test_headline_names_the_comment_downgrade(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # AT-2240's rule applied to this withhold: the headline must not
        # describe an outcome other than the one actually posted.
        calls = self._run_main_recording_gh(monkeypatch, _two_majors_no_consensus())

        body = next(body for cmd, body in calls if "comment" in cmd)
        assert (
            "[!] Approved with 2 unreviewed major issue(s) | comment only"
            " | 3/3 reviewers" in body
        )
        # The end-to-end guard for the segment TestSummaryLabels pins at
        # unit level: a colon here would claim the full roster caused the
        # downgrade.
        assert "comment only: 3/3" not in body

    def test_withhold_note_says_how_it_clears(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The defect this feature was reviewed for is that a blocked
        # reader is told the approval is withheld and given no way to act.
        # The note is the whole user interface, so it has to name what
        # re-runs a round in this repo's terms, and it must not promise
        # that a reply alone clears the withhold -- the aggregate never
        # sees a disposition (AT-2843).
        calls = self._run_main_recording_gh(monkeypatch, _two_majors_no_consensus())
        body = next(body for cmd, body in calls if "comment" in cmd)

        assert "Auto-approve withheld" in body
        assert "The next round decides again" in body
        assert "push a commit, or re-run the workflow" in body
        assert "posts once a round ends with no major raised" in body
        assert "A human approving this PR satisfies a required-review rule" in body

    def test_coverage_reason_wins_when_both_withholds_apply(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Documented precedence in _approval_withhold_reason: with a short
        # roster AND majors, coverage is named first, because the missing
        # reviewer may yet raise findings nobody has seen. Nothing covered
        # this before -- the only test that did was removed with the
        # withdrawn label, and the behaviour it checks is not the label's.
        #
        # This owns the PROSE axis of the combined case and only that.
        # Whether the combination also retires the standing same-head
        # approval is a separate question this harness cannot see -- it
        # records gh argv, not GraphQL mutations -- and being named for
        # the case while checking one axis of it is how the combined case
        # stayed broken through a round that tested "both directions".
        # The other axis is TestSameHeadApprovalStandsUntilReplaced::
        # test_a_quorum_short_round_with_majors_still_folds.
        reviews = _two_majors_no_consensus()
        reviews[list(REVIEWER_NAMES)[-1]] = None
        calls = self._run_main_recording_gh(monkeypatch, reviews)

        assert not any("review" in cmd for cmd, _ in calls)
        body = next(body for cmd, body in calls if "comment" in cmd)
        assert "not every configured reviewer produced a verdict" in body
        # Positive control: the majors really are present in this fixture,
        # so the coverage reason winning is a precedence and not an
        # absence of majors.
        assert "major issue(s) raised this round" not in body
        assert "unreviewed major issue(s)" in body

    def test_killswitch_off_gives_one_explanation(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # One downgrade, one explanation. With ALLOW_AUTO_APPROVE off the
        # killswitch already owns the downgrade and the headline names it;
        # the withhold note must not append a second, different reason for
        # the same comment. Keying the withhold on majors makes this the
        # common case for any round that raises one with auto-approve off.
        reviews = _two_majors_no_consensus()

        # Positive control: the same round with auto-approve ON does emit
        # the withhold note, so its absence below is this branch and not a
        # fixture that never produces the string.
        on = next(
            body
            for cmd, body in self._run_main_recording_gh(monkeypatch, reviews)
            if "comment" in cmd
        )
        assert "Auto-approve withheld" in on

        calls = self._run_main_recording_gh(
            monkeypatch, reviews, allow_auto_approve=False
        )
        assert not any("review" in cmd for cmd, _ in calls)
        off = next(body for cmd, body in calls if "comment" in cmd)
        assert "posted as comment (auto-approve off)" in off
        assert "Auto-approve withheld" not in off


class TestMajorCountIsOneComputation:
    """AT-2823 follow-up: one count, read by the headline and by the post.

    main()'s comment and _count_unreviewed_majors' docstring both claim the
    count is computed once and read twice. It was two independent calls that
    agreed only because both were handed the same ``available`` -- nothing
    stopped a caller passing format_summary a different mapping and
    reintroducing the headline/posted-event disagreement AT-2823 closed.
    The count is a parameter now, the way ``approve_quorum`` already is.
    """

    def test_both_readers_require_the_count(self) -> None:
        # Fail closed at the signature. The gate is `unreviewed_majors > 0`,
        # so 0 is the permissive value, and a default of 0 makes "the caller
        # forgot" indistinguishable from "a clean round" -- the same
        # indistinguishability this parameter exists to prevent, one layer
        # up. Omitting it must be a TypeError, not a formal APPROVED review
        # over majors nobody counted.
        # The omitted argument is what these two assert on, so the type
        # checker is reporting the test's subject, not a defect (AT-2418).
        with pytest.raises(TypeError, match="unreviewed_majors"):
            format_summary(  # pyright: ignore[reportCallIssue]
                {}, "approve", "reason", {}, comment_only=False, approve_quorum=True
            )
        with pytest.raises(TypeError, match="unreviewed_majors"):
            post_verdict(  # pyright: ignore[reportCallIssue]
                "body", "approve", comment_only=False, approve_quorum=True
            )

    def test_headline_renders_the_count_it_is_given(self) -> None:
        # A recount of ``available`` would render 1; the parameter renders 2.
        reviews = {
            "claude": _make_named_review("claude", [_make_issue(severity="major")])
        }
        summary = format_summary(
            reviews,
            "approve",
            "reason",
            reviews,
            comment_only=False,
            approve_quorum=True,
            unreviewed_majors=2,
        )
        assert "Approved with 2 unreviewed major issue(s)" in summary

    def test_main_hands_both_readers_the_same_count(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("PR_NUMBER", "42")
        monkeypatch.setenv("GITHUB_REPOSITORY", "owner/repo")
        for name in REVIEWER_NAMES:
            monkeypatch.setenv(f"REVIEWER_RESULT_{name.upper()}", "success")

        reviews = _two_majors_no_consensus()

        # Keep fold evidence offline too; these findings have no current
        # posts, so their fold count is unknown, without changing the major count.
        with (
            patch("aggregate_reviews.load_reviews", return_value=reviews),
            patch(
                "aggregate_reviews.format_summary", return_value="body"
            ) as mock_summary,
            patch("aggregate_reviews.post_verdict") as mock_post,
            patch("aggregate_reviews.fetch_round_count", return_value=0),
            patch("aggregate_reviews._fetch_fold_comments", return_value=[]),
            patch("aggregate_reviews._fetch_inline_posts", return_value=[]),
        ):
            main()

        # Positive control: the round really does carry majors, so an
        # equality of two zeroes cannot pass this.
        assert mock_post.call_args.kwargs["unreviewed_majors"] == 2
        assert (
            mock_summary.call_args.kwargs["unreviewed_majors"]
            == mock_post.call_args.kwargs["unreviewed_majors"]
        )


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


class TestHeadlineAgreesWithPostedEvent:
    """AT-2240: the headline and the posted event must never disagree.

    Before the fix, format_summary's comment_only came only from
    ALLOW_AUTO_APPROVE while the quorum downgrade was decided separately
    inside post_verdict -- so with auto-approve on and one reviewer dead,
    the headline could read "[OK] Approved" while post_verdict silently
    posted a plain comment instead of an approval. main() now computes
    approve_quorum once and passes the same value to both.
    """

    def test_quorum_downgrade_reflected_in_the_posted_headline(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("PR_NUMBER", "42")
        monkeypatch.setenv("GITHUB_REPOSITORY", "owner/repo")
        monkeypatch.setenv("GH_TOKEN", "default-token")
        monkeypatch.setenv("ALLOW_AUTO_APPROVE", "true")  # auto-approve is ON
        monkeypatch.setenv("REVIEW_MODE", "parallel")
        for name in REVIEWER_NAMES:
            monkeypatch.setenv(f"REVIEWER_RESULT_{name.upper()}", "success")

        reviews: dict[str, Any] = {n: _make_named_review(n, []) for n in REVIEWER_NAMES}
        reviews["gemini"] = None  # one reviewer dead -> quorum short

        posted_bodies: list[str] = []

        def fake_run(cmd: list[str], *args: Any, **kwargs: Any) -> Any:
            if "comment" in cmd:
                posted_bodies.append(kwargs.get("input", ""))
            elif "review" in cmd:
                pytest.fail("a formal review must not be posted without full quorum")
            return type("R", (), {"returncode": 0, "stderr": "", "stdout": ""})()

        with (
            patch("aggregate_reviews.load_reviews", return_value=reviews),
            patch("aggregate_reviews._fold_prior_round_items"),
            patch("aggregate_reviews.subprocess.run", side_effect=fake_run),
        ):
            main()

        assert len(posted_bodies) == 1
        body = posted_bodies[0]
        assert (
            "[!] Approved | comment only: not every reviewer responded (2/3 reviewers)"
            in body
        )
        assert "[OK]" not in body
        assert "Auto-approve withheld" in body


class TestCommentOnlyToggle:
    """_is_comment_only maps ALLOW_AUTO_APPROVE to the comment-only killswitch."""

    def test_default_is_comment_only(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("ALLOW_AUTO_APPROVE", raising=False)
        assert _is_comment_only() is True

    def test_false_is_comment_only(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ALLOW_AUTO_APPROVE", "false")
        assert _is_comment_only() is True

    def test_true_disables_comment_only(self, monkeypatch: pytest.MonkeyPatch) -> None:
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
        reviews: dict[str, dict[str, Any] | None] = {"codex": _make_review(status="ok")}
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
        reviews: dict[str, dict[str, Any] | None] = {n: None for n in REVIEWER_NAMES}
        reviews[list(REVIEWER_NAMES)[0]] = _make_review(status="failed")
        summary = format_summary(
            reviews,
            "comment",
            "reason",
            {},
            comment_only=True,
            unreviewed_majors=0,
        )
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

    def test_unknown_status_disqualifies_indeterminate(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # All jobs "success" but one artifact carries an unknown status:
        # the payload exists, so the indeterminate path must not fire.
        for name in REVIEWER_NAMES:
            monkeypatch.setenv(f"REVIEWER_RESULT_{name.upper()}", "success")
        reviews: dict[str, dict[str, Any] | None] = {n: None for n in REVIEWER_NAMES}
        reviews[list(REVIEWER_NAMES)[0]] = _make_review(status="weird")
        verdict, reason, _ = apply_verdict_rules(reviews)
        assert verdict == "comment"
        assert reason == _ALL_FAILED_REASON


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

    def test_missing_status_disqualifies_indeterminate(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # All jobs "success" but one artifact omits status: the payload
        # exists and fails closed, so the indeterminate path must not fire.
        for name in REVIEWER_NAMES:
            monkeypatch.setenv(f"REVIEWER_RESULT_{name.upper()}", "success")
        reviews: dict[str, dict[str, Any] | None] = {n: None for n in REVIEWER_NAMES}
        reviews[list(REVIEWER_NAMES)[0]] = _make_status_less_review()
        verdict, reason, _ = apply_verdict_rules(reviews)
        assert verdict == "comment"
        assert reason == _ALL_FAILED_REASON

    def test_normalization_is_idempotent_and_stamped(self) -> None:
        review = _make_status_less_review()
        first = _normalize_status("codex", review)
        assert review["status"] == first
        assert _normalize_status("codex", review) == first


class TestSummaryLabels:
    """format_summary's headline is three independent axes (AT-2240):
    verdict, posting, and reviewer coverage. Each is asserted separately so a
    change to one axis cannot silently break another.
    """

    def test_request_changes_comment_only_label(self) -> None:
        summary = format_summary(
            {},
            "request_changes",
            "reason",
            {},
            comment_only=True,
            unreviewed_majors=0,
        )
        assert "[!] Changes Requested | posted as comment (auto-approve off)" in summary

    def test_request_changes_active_label(self) -> None:
        summary = format_summary(
            {},
            "request_changes",
            "reason",
            {},
            comment_only=False,
            unreviewed_majors=0,
        )
        assert "[X] Changes Requested | 0/3 reviewers" in summary
        assert "posted as comment" not in summary

    def test_approve_comment_only_label(self) -> None:
        # AT-2240 claim 2: comment_only alone (no majors, full quorum) is an
        # administrative posting choice, not a review-quality problem, so the
        # icon stays [OK] and the word "Approved" is never dropped.
        summary = format_summary(
            {},
            "approve",
            "reason",
            {},
            comment_only=True,
            approve_quorum=True,
            unreviewed_majors=0,
        )
        assert (
            "[OK] Approved | posted as comment (auto-approve off) | 0/3 reviewers"
            in summary
        )

    def test_approve_clean_label(self) -> None:
        reviews = {n: _make_named_review(n, []) for n in REVIEWER_NAMES}
        summary = format_summary(
            reviews,
            "approve",
            "reason",
            reviews,
            comment_only=False,
            approve_quorum=True,
            unreviewed_majors=_count_unreviewed_majors(reviews),
        )
        assert "[OK] Approved | 3/3 reviewers" in summary

    def test_comment_only_takes_a_colon_only_when_coverage_is_the_reason(
        self,
    ) -> None:
        """The colon introduces a reason, so it needs one to introduce.

        AT-2511 built ``| comment only: <coverage>`` for the quorum
        shortfall, where the coverage segment IS the reason -- "comment
        only: not every reviewer responded (2/3 reviewers)" reads
        correctly. AT-2823 widened the branch to every withhold without
        widening the prose, so the majors-only path rendered "comment
        only: 3/3 reviewers", where the colon makes a full roster look
        like the cause of its own downgrade. The majors are the cause,
        and the label already names them, so that path takes the plain
        axis separator instead.
        """
        majors = [_make_issue(severity="major")]
        full = {n: _make_named_review(n, []) for n in REVIEWER_NAMES}
        full["claude"] = _make_named_review("claude", majors)

        # Majors only, full roster: no reason-prose, so no colon.
        summary = format_summary(
            full,
            "approve",
            "reason",
            full,
            comment_only=False,
            approve_quorum=True,
            unreviewed_majors=1,
        )
        assert (
            "[!] Approved with 1 unreviewed major issue(s) | comment only"
            " | 3/3 reviewers" in summary
        )
        assert "comment only: 3/3" not in summary

        # Quorum short: the coverage segment is the reason, so it keeps
        # the colon -- AT-2511's wording is unchanged by this.
        short = {n: _make_named_review(n, []) for n in list(REVIEWER_NAMES)[:2]}
        summary = format_summary(
            short,
            "approve",
            "reason",
            short,
            comment_only=False,
            approve_quorum=False,
            unreviewed_majors=0,
        )
        assert (
            "[!] Approved | comment only: not every reviewer responded"
            " (2/3 reviewers)" in summary
        )

        # Both: coverage is named first as the withhold reason, so the
        # colon form is right here too.
        summary = format_summary(
            short,
            "approve",
            "reason",
            short,
            comment_only=False,
            approve_quorum=False,
            unreviewed_majors=1,
        )
        assert (
            "[!] Approved with 1 unreviewed major issue(s) | comment only:"
            " not every reviewer responded (2/3 reviewers)" in summary
        )

    def test_approve_with_unreviewed_major_issues_loses_ok_icon(self) -> None:
        # AT-2240 claim 1: apply_verdict_rules keeps a major-without-consensus
        # issue at verdict "approve" -- the headline must say so instead of
        # rendering a bare, unqualified [OK].
        reviews = {
            "claude": _make_named_review("claude", [_make_issue(severity="major")]),
            "codex": _make_named_review("codex", []),
            "gemini": _make_named_review("gemini", []),
        }
        verdict, reason, available = apply_verdict_rules(reviews)
        assert verdict == "approve"
        summary = format_summary(
            reviews,
            verdict,
            reason,
            available,
            comment_only=False,
            approve_quorum=True,
            unreviewed_majors=_count_unreviewed_majors(available),
        )
        # This expectation changed twice and both changes are owed a
        # reason. Before AT-2823 it read "| 3/3 reviewers" with no
        # posting axis, correct then because nothing was downgraded. A
        # major now withholds the formal approval, and AT-2240's own rule
        # is that the headline must not describe an outcome other than the
        # one posted -- so the posting axis has to appear. It appears
        # without a colon because the coverage figure is not the reason
        # for the downgrade; that was the second change, and the earlier
        # "comment only: 3/3 reviewers" here was this test being bent to
        # the code's output rather than the output being fixed.
        assert (
            "[!] Approved with 1 unreviewed major issue(s) | comment only"
            " | 3/3 reviewers" in summary
        )
        assert "[OK]" not in summary

    def test_approve_without_quorum_loses_ok_icon_and_states_coverage(self) -> None:
        # AT-2240 claim 3: format_summary's comment_only alone cannot reflect
        # the quorum downgrade computed via approve_quorum -- it must be
        # passed in explicitly and change the headline.
        reviews = {n: _make_named_review(n, []) for n in REVIEWER_NAMES}
        del reviews["gemini"]
        summary = format_summary(
            reviews,
            "approve",
            "reason",
            reviews,
            comment_only=False,
            approve_quorum=False,
            unreviewed_majors=_count_unreviewed_majors(reviews),
        )
        assert (
            "[!] Approved | comment only: not every reviewer responded"
            " (2/3 reviewers)" in summary
        )
        assert "[OK]" not in summary

    def test_approve_comment_only_and_without_quorum_states_both_reasons(self) -> None:
        # Review-round follow-up: comment_only and approve_quorum_short can
        # both be true (auto-approve off on a PR where a reviewer also
        # didn't respond). The comment_only branch used to win outright and
        # silently drop the quorum reason -- the coverage segment must name
        # the quorum shortfall regardless of comment_only.
        reviews = {n: _make_named_review(n, []) for n in REVIEWER_NAMES}
        del reviews["gemini"]
        summary = format_summary(
            reviews,
            "approve",
            "reason",
            reviews,
            comment_only=True,
            approve_quorum=False,
            unreviewed_majors=_count_unreviewed_majors(reviews),
        )
        assert (
            "[!] Approved | posted as comment (auto-approve off) |"
            " not every reviewer responded (2/3 reviewers)" in summary
        )
        assert "[OK]" not in summary

    def test_comment_verdict_label_states_coverage(self) -> None:
        summary = format_summary({}, "comment", "reason", {}, unreviewed_majors=0)
        assert "[!] Comment Only | 0/3 reviewers" in summary


class TestClaudeInfrastructureFailure:
    """AT-1837: a dead Claude reviewer must be counted as failed, not absent.

    A dependabot-triggered run had claude-code-action reject the actor
    ("Workflow initiated by non-human actor"), so no artifact was produced.
    The aggregate then saw an ABSENT reviewer, which only lowers the count,
    and blamed it on "early-exit or no-output" -- the AT-1792 shape.
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

    def test_absent_artifact_is_reported_as_early_exit_or_no_output(self) -> None:
        # Baseline the pre-fix shape: an absent payload gets the roster
        # reason its job conclusion yields, so nothing on the summary says
        # an outage happened -- which is why the emitter must not leave one.
        reviews: dict[str, dict[str, Any] | None] = {
            name: _make_named_review(name, []) for name in REVIEWER_NAMES
        }
        reviews["claude"] = None
        conclusions = {name: "success" for name in REVIEWER_NAMES}
        verdict, reason, available = apply_verdict_rules(reviews)
        summary = format_summary(
            reviews,
            verdict,
            reason,
            available,
            conclusions,
            unreviewed_majors=_count_unreviewed_majors(available),
        )
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
        # AT-2123: a payload with normalized status "failed" never counts as
        # a performed review, so its section must not read as a count of
        # findings ("N issue(s)") -- that framing implies looking, and a
        # failed reviewer never looked. The reason still names it, on both
        # the headline and the section header.
        reviews = self._reviews_with_dead_claude()
        conclusions = {name: "success" for name in REVIEWER_NAMES}
        verdict, reason, available = apply_verdict_rules(reviews)
        summary = format_summary(
            reviews,
            verdict,
            reason,
            available,
            conclusions,
            unreviewed_majors=_count_unreviewed_majors(available),
        )
        # The headline note and the roster share one derivation (AT-2511),
        # but only the roster value is compared as a whole string, so the
        # namespacing prefix that keeps reviewer-authored text from
        # spelling a benign roster reason comes off for the prose -- left
        # on it doubles the detail's own wording. The reviewer's own
        # section states the detail unprefixed for the same reason.
        assert "claude: action_invocation_failed" in summary
        assert f"claude: {FAILED_DETAIL_PREFIX}" not in summary
        assert "### Claude -- [ ] not run (action_invocation_failed)" in summary
        assert "issue(s)" not in summary.split("### Claude")[1].split("###")[0]
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

    def test_failed_payload_disqualifies_indeterminate(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Claude's error verdict is an artifact: all-jobs-success must not
        # reach the indeterminate 0-response path.
        for name in REVIEWER_NAMES:
            monkeypatch.setenv(f"REVIEWER_RESULT_{name.upper()}", "success")
        reviews: dict[str, dict[str, Any] | None] = {n: None for n in REVIEWER_NAMES}
        reviews["claude"] = self._claude_failed()
        verdict, reason, _ = apply_verdict_rules(reviews)
        assert verdict == "comment"
        assert reason == _ALL_FAILED_REASON


class TestNotRunVsPartialVsClean:
    """AT-2123: a reviewer never invoked must not render as "N issue(s)".

    Splits the old single ``error``-key handling into two distinct cases and
    fixes the rendering for each, plus the unrelated case that must not
    change: a reviewer that ran fully and genuinely found nothing.
    """

    def test_not_run_reviewer_has_no_issue_count(self) -> None:
        # The pilot census regression case (AT-2123 comment): codex's own
        # CLI never got past authentication, so it self-writes an error
        # verdict with status "failed" and issues=[] -- it never looked.
        review = _make_review(
            summary="Codex review failed: CLI exited 2",
            status="failed",
            error="cli_invocation_failed",
        )
        summary = format_summary(
            {"codex": review},
            "comment",
            "reason",
            {},
            unreviewed_majors=0,
        )
        section = summary.split("### Codex")[1]
        assert "issue(s)" not in section
        assert "### Codex -- [ ] not run (cli_invocation_failed)" in summary

    def test_partial_reviewer_keeps_existing_rendering(self) -> None:
        # Ran, died partway, but surfaced real issues before dying -- status
        # stays "ok"/"early_exit" with an `error` alongside. This must keep
        # rendering exactly as before: a count plus a `(partial: ...)` tag.
        review = _make_review(
            status="ok",
            error="truncated",
            issues=[_make_issue()],
        )
        summary = format_summary(
            {"codex": review},
            "comment",
            "reason",
            {},
            unreviewed_majors=0,
        )
        assert "### Codex -- 1 issue(s) [!] (partial: truncated)" in summary

    def test_clean_zero_issues_reviewer_still_says_zero(self) -> None:
        # The regression case this ticket exists to protect: a reviewer that
        # ran to completion and genuinely found nothing must still say
        # "0 issue(s)" -- that is the truthful rendering for this case, and
        # it must be visibly different from the not-run case above.
        review = _make_review(status="ok")
        summary = format_summary(
            {"codex": review},
            "comment",
            "reason",
            {},
            unreviewed_majors=0,
        )
        assert "### Codex -- 0 issue(s)" in summary
        assert "not run" not in summary

    def test_all_three_cases_render_distinctly_in_one_summary(self) -> None:
        reviews: dict[str, dict[str, Any] | None] = {
            "codex": _make_review(
                summary="Codex review failed: CLI exited 2",
                status="failed",
                error="cli_invocation_failed",
            ),
            "claude": _make_review(
                status="ok", error="truncated", issues=[_make_issue()]
            ),
            "gemini": _make_review(status="ok"),
        }
        summary = format_summary(reviews, "comment", "reason", {}, unreviewed_majors=0)
        assert "### Codex -- [ ] not run (cli_invocation_failed)" in summary
        assert "### Claude -- 1 issue(s) [!] (partial: truncated)" in summary
        assert "### Gemini -- 0 issue(s)" in summary


_SINGLE_WORKFLOW = (
    Path(__file__).resolve().parents[2] / "workflows" / "base-ai-review-single.yml"
)
_ERROR_VERDICT_STEP = "Emit Claude error verdict (no verdict file)"

requires_jq = pytest.mark.skipif(shutil.which("jq") is None, reason="jq not installed")


def _step_script(name: str) -> str:
    """Return the run: body of a named step of the single-reviewer workflow."""
    workflow = yaml.safe_load(_SINGLE_WORKFLOW.read_text(encoding="utf-8"))
    for step in workflow["jobs"]["review"]["steps"]:
        if step.get("name") == name:
            return str(step["run"])
    raise AssertionError(f"step not found: {name}")


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
            ["bash", "-c", _step_script(_ERROR_VERDICT_STEP)],
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


_CODEX_RUN_STEP = "Run Codex review"
_NORMALIZE_STEP = "Normalize review file name"
_PLANTED_SUMMARY = "No issues found."
_VERDICT_JSON = '{"summary": "real", "early_exit": false, "issues": []}'
# One file, two JSON documents: the shape `jq -e` answered "true" for,
# because its exit status comes from the last value it printed.
_MULTI_DOCUMENT = '{"note": "chatter"}\n' + _VERDICT_JSON
# What the runner gives a `run:` block on Linux when the step names no
# shell of its own: bash -e. A step body that aborts mid-way therefore
# aborts here too -- running it under a plain `bash -c` hid a step that
# died at its first command and left no verdict file behind at all.
_RUNNER_SHELL = ["bash", "-e", "-c"]
_REPO_ROOT = Path(__file__).resolve().parents[3]
_PROMOTE_SCRIPT = Path(__file__).resolve().parents[1] / "promote_legacy_verdict.sh"


def _logical_lines(text: str) -> list[tuple[int, str]]:
    """Code only: comments dropped, `\\`-continued lines joined.

    Each entry is tagged with the physical line it started on. Comments
    go first because a backslash at the end of one does not continue it
    in bash, and joining first let `rm -rf -- "$x"  # note \\` swallow
    the whole of the next line.
    """
    joined: list[tuple[int, str]] = []
    buffer = ""
    start = None
    for number, line in enumerate(text.splitlines(), start=1):
        if start is None:
            start = number
        # A `#` only starts a comment at the beginning of a word, so a
        # quoted hash or a `${name#./}` expansion does not blank the
        # rest of the line.
        code = re.split(r"(?:^|\s)#", line, maxsplit=1)[0]
        if code.endswith("\\"):
            buffer += code[:-1] + " "
            continue
        joined.append((start, buffer + code))
        buffer, start = "", None
    if buffer:
        joined.append((start or 1, buffer))
    return joined


def _unguarded_destructive_commands(text: str) -> list[str]:
    """Every `rm`/`mv` in `text` whose own arguments lack `--`.

    Arguments stop at the next shell separator, so a marker belonging
    to a neighbour cannot vouch for a command that has none. The
    shapes it is known to handle -- caught and not-flagged alike --
    are the parametrized cases of TestTheDestructiveCommandScan; the
    KNOWN LIMITS below are the shapes outside that catalogue.

    KNOWN LIMITS. Each below was established by running the scan, and
    they are sorted by the direction they fail in. The list is not
    exhaustive, and absence from it is not a clearance: run an
    uncatalogued shape against the scan rather than assuming the scan
    handles it.

    SILENT -- a real call goes unreported: one reached through `eval`
    or a variable (`$CMD -rf "$x"`), and one standing after a
    whitespace-preceded `#` in a string, which blanks the rest of the
    line (`echo "a # b"; rm -rf "$x"` yields nothing). Tolerated rather
    than chased: the scan is a backstop, and require_verdict_name
    refuses a bad name before any destructive call runs.

    NOISY -- the suite goes red with no unguarded call in the script.
    Two shapes come from having no quoting model: a `;` or `|` inside
    an operand ends the argument capture before the marker, so a
    guarded `rm -rf "a;b" -- "$x"` is reported; and a
    whitespace-preceded `rm `/`mv ` in a string reads as a command, so
    `echo "will rm -rf the file"` is too. A third has a separate cause
    -- _logical_lines does not track here-docs, so a body line reaches
    the scan as ordinary code and `rm -rf x` inside one is reported.
    Recognise these as scan artefacts, not a missing marker. Whether
    any is present is not a question the reader has to take on trust:
    test_every_destructive_command_ends_its_options scans the whole
    script and reports every offender, so a green suite means none of
    the three is in it, and a red one names the line. On the string
    shape, the `::notice::`/`::error::` prefix is not what keeps the
    suite green: it helps only directly before the word, so
    `echo "::notice::could not rm -rf $x"` would still be reported.
    """
    offenders: list[str] = []
    for number, code in _logical_lines(text):
        for match in re.finditer(r"(?:^|[;&|(`{]|\s)(rm|mv)\s+([^;&|]*)", code):
            command, arguments = match.group(1), match.group(2)
            if not re.search(r"(^|\s)--(\s|$)", arguments):
                offenders.append(f"{number}: {command} {arguments.strip()}")
    return offenders


requires_codex_step_tools = pytest.mark.skipif(
    any(shutil.which(tool) is None for tool in ("jq", "python3")),
    reason="jq or python3 not installed",
)


class _CodexStepHarness:
    """Mechanics only: run the two codex steps the way the runner does.

    No tests live here. The three classes below ask three different
    questions of the same two step bodies -- which verdict wins, whose
    file it is, and what the net does when the run step never finished --
    and they share these helpers rather than three copies of a stub CLI.
    """

    @staticmethod
    def _link_scripts(workdir: Path, scripts_root: Path | None = None) -> None:
        """Both steps reach the promotion script through the scripts copy.

        That copy lives under RUNNER_TEMP, outside the workspace the model
        can write (AT-2941), so it is linked there and not at the checkout
        path. `scripts_root` stands in for a consumer pinned to an older
        release tag, whose checkout does not carry a script this YAML calls.
        """
        link = workdir / "runner-temp" / "ai-dev-pr-review"
        link.parent.mkdir(exist_ok=True)
        if not link.exists():
            link.symlink_to(scripts_root or _REPO_ROOT)

    @staticmethod
    def _stub_codex(workdir: Path, script: str) -> Path:
        """Install a `codex` on PATH that acts out one run of the CLI."""
        bin_dir = workdir / "bin"
        bin_dir.mkdir(exist_ok=True)
        stub = bin_dir / "codex"
        stub.write_text(f"#!/usr/bin/env bash\n{script}\n", encoding="utf-8")
        stub.chmod(0o755)
        return bin_dir

    @classmethod
    def _run_codex_step(
        cls,
        workdir: Path,
        *,
        writes: Sequence[tuple[str, str]] = (),
        log: str = "codex: done",
        exit_code: int = 0,
        touches: str | None = None,
        links: Sequence[tuple[str, Path]] = (),
        stalls: bool = False,
        timeout: float = 60,
        scripts_root: Path | None = None,
        marks_run: str | None = None,
    ) -> None:
        """Run 'Run Codex review' with a CLI that acts out one run.

        `touches` is a workspace write the model can be induced to make --
        `codex exec --sandbox workspace-write` lets it write the tree it is
        reviewing. `links` are symlinks it can create there the same way:
        only the directory holding the link has to be writable, never the
        file it names (AT-2941). `stalls` never returns, so the caller can
        kill the step the way timeout-minutes does.
        """
        runner_temp = workdir / "runner-temp"
        runner_temp.mkdir(exist_ok=True)
        (runner_temp / "review_prompt.md").write_text("review this", encoding="utf-8")
        cls._link_scripts(workdir, scripts_root)
        body = ""
        if marks_run is not None:
            body += f"touch {marks_run}\n"
        if touches is not None:
            body += f"touch {touches}\n"
        for name, link_target in links:
            body += f"ln -s -- {shlex.quote(str(link_target))} {shlex.quote(name)}\n"
        for name, text in writes:
            body += f"cat > {name} <<'VERDICT'\n{text}\nVERDICT\n"
        if stalls:
            body += "sleep 5\n"
        else:
            body += f"printf '%s\\n' {json.dumps(log)}\nexit {exit_code}\n"
        bin_dir = cls._stub_codex(workdir, body)
        result = subprocess.run(
            _RUNNER_SHELL + [_step_script(_CODEX_RUN_STEP)],
            cwd=workdir,
            env={
                "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
                "RUNNER_TEMP": str(runner_temp),
                "GITHUB_OUTPUT": str(workdir / "github-output"),
                "CODEX_MODEL": "stub-model",
                "TMPDIR": str(runner_temp / "codex-tmp"),
                "THREAD_COUNT": "0",
                "EXISTING_COMMENTS": "",
            },
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        assert result.returncode == 0, result.stderr

    @classmethod
    def _run_codex_step_cancelled(cls, workdir: Path) -> None:
        """Kill the step mid-CLI, the way timeout-minutes does.

        What the net then inherits is the real state of that path: the
        candidates cleared and the marker written, but no error verdict,
        because the step never reached the code that writes one.
        """
        with pytest.raises(subprocess.TimeoutExpired):
            cls._run_codex_step(workdir, stalls=True, timeout=2)

    @staticmethod
    def _plant(path: Path) -> None:
        """Write a verdict file as a PR that committed one leaves it.

        Nothing is back-dated. The guarantee is that the run clears these
        names before the CLI starts, so how old the file is never enters
        into it -- and a test that depended on its age would be testing a
        clock this code no longer reads.
        """
        path.write_text(
            json.dumps(
                {
                    "summary": _PLANTED_SUMMARY,
                    "status": "ok",
                    "early_exit": False,
                    "issues": [],
                }
            ),
            encoding="utf-8",
        )

    @staticmethod
    def _mark(workdir: Path) -> None:
        """Stand in for the marker 'Run Codex review' writes before the CLI."""
        runner_temp = workdir / "runner-temp"
        runner_temp.mkdir(exist_ok=True)
        (runner_temp / "codex-start").write_text("", encoding="utf-8")

    @classmethod
    def _run_normalize_step(
        cls, workdir: Path, scripts_root: Path | None = None
    ) -> None:
        """Run 'Normalize review file name' over the workdir as it stands."""
        runner_temp = workdir / "runner-temp"
        runner_temp.mkdir(exist_ok=True)
        cls._link_scripts(workdir, scripts_root)
        result = subprocess.run(
            _RUNNER_SHELL + [_step_script(_NORMALIZE_STEP)],
            cwd=workdir,
            env={
                "PATH": os.environ["PATH"],
                "RUNNER_TEMP": str(runner_temp),
                "REVIEWER": "codex",
            },
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode == 0, result.stderr

    @staticmethod
    def _verdict(workdir: Path) -> dict[str, Any]:
        return dict(json.loads((workdir / "review-codex.json").read_text()))

    @staticmethod
    def _aggregate_sees(
        workdir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """What load_reviews / _get_available make of the workdir's files."""
        for name in ("claude", "gemini"):
            (workdir / f"review-{name}.json").write_text(
                json.dumps(_make_named_review(name, []))
            )
        monkeypatch.chdir(workdir)
        reviews = load_reviews()
        return dict(reviews), dict(_get_available(reviews))

    @staticmethod
    def _old_pin(tmp_path: Path) -> Path:
        """A pinned checkout from before the promotion script existed."""
        root = tmp_path / "old-pin"
        (root / ".github" / "scripts").mkdir(parents=True)
        return root


@requires_codex_step_tools
class TestCodexLegacyVerdictPromotion(_CodexStepHarness):
    """Which verdict wins, and what may never win (AT-2424).

    The base prompt may still instruct the model to write the older name
    verdict-openai.json, so promotion to review-codex.json exists. It used
    to run only after 'Run Codex review' had already written an error
    verdict to review-codex.json on every one of its failure paths: the
    canonical file was therefore present, the promotion was skipped whole,
    and the model's real verdict was discarded while the aggregate
    reported that codex had produced nothing.

    Precedence is one question asked once: a target is this run's answer
    if STAMPING it yields a verdict, the same test a candidate gets.
    Asking it of the raw file instead held the target to a stricter
    standard -- the legacy schema carries no early_exit -- and a
    legacy-named file then overwrote a canonical one written in it.

    These run the steps' own shell bodies against a stub CLI and follow
    what they wrote into the aggregate's loader, the seam a discarded
    verdict never reached.
    """

    def test_a_legacy_named_verdict_survives_the_unparseable_error_verdict(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The reproduction: the model wrote its verdict under the old name
        # and printed nothing parseable, so the step's own extraction
        # fallback synthesized "no parseable verdict JSON in output".
        self._run_codex_step(
            tmp_path,
            writes=[
                (
                    "verdict-openai.json",
                    json.dumps(
                        {
                            "summary": "Codex reviewed the diff",
                            "status": "ok",
                            "early_exit": False,
                            "issues": [_make_issue(severity="major")],
                        }
                    ),
                )
            ],
        )
        verdict = self._verdict(tmp_path)
        assert verdict["summary"] == "Codex reviewed the diff"
        assert "error" not in verdict
        reviews, available = self._aggregate_sees(tmp_path, monkeypatch)
        assert "codex" in available
        assert available["codex"]["issues"] == reviews["codex"]["issues"]

    def test_a_legacy_named_verdict_survives_the_cli_failure_verdict(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Same masking through the other failure path: a non-zero exit
        # after the model had already written its verdict.
        self._run_codex_step(
            tmp_path,
            writes=[
                (
                    "verdict-codex.json",
                    json.dumps(
                        {
                            "summary": "Codex reviewed the diff",
                            "status": "ok",
                            "early_exit": False,
                            "issues": [],
                        }
                    ),
                )
            ],
            exit_code=3,
        )
        assert self._verdict(tmp_path)["summary"] == "Codex reviewed the diff"
        _, available = self._aggregate_sees(tmp_path, monkeypatch)
        assert "codex" in available

    def test_a_promoted_legacy_verdict_carries_a_status_the_aggregate_accepts(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The legacy schema has neither early_exit nor status, and the
        # aggregate fails closed on a missing status (AT-1954). Promotion
        # that leaves both absent hands it a verdict it must discard, which
        # is the same loss by a later route.
        self._run_codex_step(
            tmp_path,
            writes=[
                (
                    "verdict-openai.json",
                    json.dumps({"summary": "Codex reviewed the diff", "issues": []}),
                )
            ],
        )
        verdict = self._verdict(tmp_path)
        assert verdict["early_exit"] is False
        assert verdict["status"] == "ok"
        _, available = self._aggregate_sees(tmp_path, monkeypatch)
        assert "codex" in available

    def test_a_direct_write_in_the_legacy_schema_reaches_the_aggregate(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The workflow's own inline stamping was a second copy of the rule
        # and had drifted: it added a status but never back-filled
        # early_exit, so a direct write in the legacy schema failed
        # is_valid_review and the aggregate rendered `Codex -- [ ] N/A`
        # for a review that had been produced.
        self._run_codex_step(
            tmp_path,
            writes=[
                (
                    "review-codex.json",
                    json.dumps({"summary": "direct write", "issues": []}),
                )
            ],
        )
        verdict = self._verdict(tmp_path)
        assert verdict["early_exit"] is False
        assert verdict["status"] == "ok"
        _, available = self._aggregate_sees(tmp_path, monkeypatch)
        assert "codex" in available

    def test_a_verdict_written_to_the_canonical_name_outranks_a_legacy_one(
        self, tmp_path: Path
    ) -> None:
        # The canonical file is written in the LEGACY SCHEMA, with no
        # status and no early_exit, because that is the case that tells a
        # fixed precedence rule from a broken one. The earlier version of
        # this test wrote a full-schema canonical, which passes either
        # way: the target was being held to the raw shape test while
        # candidates were held to the stamped one, so exactly the payload
        # the stamping exists for lost to a legacy name.
        #
        # Both files are written BY THE RUN, the only way this case can
        # arise now that the clear empties the tree first -- and the only
        # way the test means anything at all. Writing the legacy file
        # after the step returned left it absent while promote ran.
        self._run_codex_step(
            tmp_path,
            writes=[
                (
                    "review-codex.json",
                    json.dumps({"summary": "this run", "issues": []}),
                ),
                (
                    "verdict-openai.json",
                    json.dumps(
                        {
                            "summary": "the legacy name",
                            "status": "ok",
                            "early_exit": False,
                            "issues": [],
                        }
                    ),
                ),
            ],
        )
        verdict = self._verdict(tmp_path)
        assert verdict["summary"] == "this run"
        # And it went through the same stamping a promoted candidate gets,
        # which is what makes the two comparable in the first place.
        assert verdict["early_exit"] is False
        assert verdict["status"] == "ok"

    def test_a_truncated_canonical_file_does_not_outrank_a_legacy_verdict(
        self, tmp_path: Path
    ) -> None:
        # The early return asked only whether the target was non-empty, so
        # a half-written canonical file beat a complete legacy one and the
        # run ended on an error verdict with the real review on disk beside
        # it -- this ticket's own loss mode, by a shorter route.
        self._run_codex_step(
            tmp_path,
            writes=[
                ("review-codex.json", '{"summ'),
                (
                    "verdict-openai.json",
                    json.dumps({"summary": "the real verdict", "issues": []}),
                ),
            ],
        )
        assert self._verdict(tmp_path)["summary"] == "the real verdict"

    def test_an_unreadable_candidate_does_not_block_the_next_one(
        self, tmp_path: Path
    ) -> None:
        # The copy of the loop that redirected jq straight onto the target
        # truncated it on a candidate jq could not read and then stopped
        # looking, so a real verdict under the second name was lost. One
        # script, one temp-file write, one answer.
        self._mark(tmp_path)
        (tmp_path / "verdict-openai.json").write_text("not json at all")
        (tmp_path / "verdict-codex.json").write_text(
            json.dumps({"summary": "the real verdict", "issues": []})
        )
        self._run_normalize_step(tmp_path)
        assert self._verdict(tmp_path)["summary"] == "the real verdict"

    @pytest.mark.parametrize(
        "junk",
        [
            "not json at all",
            "[]",
            '"a string"',
            "null",
            '{"foo": 1}',
            # Two documents in one file. The shape test used to be `jq -e`,
            # which takes its exit status from the LAST value, so a stray
            # object followed by a real verdict answered "true" -- and the
            # stamping emits one object per input document, so what got
            # installed was a file no json.load can read.
            _MULTI_DOCUMENT,
            _VERDICT_JSON + "\n" + _VERDICT_JSON,
            _VERDICT_JSON + "\nnot json",
        ],
        ids=[
            "unparseable",
            "array",
            "string",
            "null",
            "partial-object",
            "stray-object-then-verdict",
            "verdict-twice",
            "verdict-then-garbage",
        ],
    )
    def test_a_malformed_legacy_verdict_leaves_an_honest_error_verdict(
        self, tmp_path: Path, junk: str
    ) -> None:
        # Default-deny: promotion may not put something the aggregate
        # cannot read into the canonical file, and the error verdict it
        # would displace is the truthful answer here, log tail and all.
        # `null` and the partial object are the ones the stamping does NOT
        # reject -- both come out of it as objects -- so the shape test
        # after it is what denies them.
        self._run_codex_step(tmp_path, writes=[("verdict-openai.json", junk)])
        verdict = self._verdict(tmp_path)
        assert verdict["status"] == "failed"
        assert verdict["error"] == "output_unparseable"

    def test_whatever_is_installed_as_the_verdict_is_one_json_document(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The property the shape test exists to guarantee, asked of the
        # file rather than of its last value: the aggregate reads these
        # with json.load, which raises on the second document. A
        # multi-document promotion therefore cost the review entirely --
        # and silently, because it displaced the honest error verdict and
        # its log tail on the way.
        self._run_codex_step(
            tmp_path, writes=[("verdict-openai.json", _MULTI_DOCUMENT)]
        )
        body = (tmp_path / "review-codex.json").read_text(encoding="utf-8")
        # Raises "Extra data" on a multi-document file, which is the
        # assertion: the aggregate reads these the same way.
        verdict = json.loads(body)
        assert verdict["error"] == "output_unparseable"
        assert verdict["error_detail"], "the diagnostic must survive"
        reviews, available = self._aggregate_sees(tmp_path, monkeypatch)
        assert "codex" not in available
        assert reviews["codex"] is not None, "readable, and honestly failed"

    @pytest.mark.parametrize(
        "payload",
        ["[]", "null", '"a string"', _MULTI_DOCUMENT],
        ids=["array", "null", "string", "multi-document"],
    )
    def test_a_direct_write_that_is_not_one_json_object_leaves_an_error_verdict(
        self, tmp_path: Path, payload: str
    ) -> None:
        # The inline filter indexed these and errored, and the `&&` made
        # that the step's exit status: under the runner's bash -e a model
        # writing `[]` killed the step before any fallback ran. The shared
        # stamper reports "not a verdict" and the caller writes its own.
        self._run_codex_step(tmp_path, writes=[("review-codex.json", payload)])
        verdict = self._verdict(tmp_path)
        assert verdict["status"] == "failed"
        assert verdict["error"] == "output_unparseable"

    def test_unreadable_candidates_leave_no_truncated_target_behind(
        self, tmp_path: Path
    ) -> None:
        # Default-deny leaves the caller its own error verdict to write,
        # not a zero-byte file that 'Verify Codex verdict file' would read
        # as no verdict at all while the log says a promotion happened.
        self._mark(tmp_path)
        (tmp_path / "verdict-openai.json").write_text("not json at all")
        (tmp_path / "verdict-codex.json").write_text("[]")
        self._run_normalize_step(tmp_path)
        assert not (tmp_path / "review-codex.json").exists()
        assert not (tmp_path / "review-codex.json.tmp").exists()


@requires_codex_step_tools
class TestCodexVerdictProvenance(_CodexStepHarness):
    """Whose file is this? (AT-2424)

    The caller repo is checked out at the workspace root, so every name
    involved -- review-codex.json as much as the legacy ones, and the
    temp paths beside them -- is a path the PR under review can commit,
    as a file, a directory or a symlink. The run step CLEARS them
    immediately before the CLI, so afterwards existence is itself the
    proof that this run wrote the file; no timestamp is compared, because
    the CLI runs with --sandbox workspace-write and a model can be
    induced to touch a file it did not create.

    When the script that performs the clear is not in the pinned
    checkout, the step skips the review rather than running it without
    provenance, and these follow that verdict into the aggregate too.
    """

    def test_a_verdict_file_the_pr_checked_out_is_not_promoted(
        self, tmp_path: Path
    ) -> None:
        # The working tree is the PR head. A verdict-openai.json committed
        # by the PR is there before the reviewer starts, and promoting it
        # would let the PR under review write its own reviewer verdict on
        # every path where the CLI fails.
        self._plant(tmp_path / "verdict-openai.json")
        self._plant(tmp_path / "verdict-codex.json")
        self._run_codex_step(tmp_path)
        verdict = self._verdict(tmp_path)
        assert verdict["status"] == "failed"
        assert verdict["summary"] != _PLANTED_SUMMARY
        # Cleared, not merely passed over: nothing downstream can read them
        # back, and no later step has to ask again where they came from.
        assert not (tmp_path / "verdict-openai.json").exists()
        assert not (tmp_path / "verdict-codex.json").exists()

    def test_a_canonical_verdict_file_the_pr_checked_out_is_cleared(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The legacy names were cleared while review-codex.json was not,
        # and the run step's direct-write branch takes whatever is there:
        # a PR could ship its own approving verdict under the canonical
        # name on EVERY path, not only the failing ones. The argument for
        # clearing the legacy names applies to this one verbatim.
        self._plant(tmp_path / "review-codex.json")
        self._run_codex_step(tmp_path)
        verdict = self._verdict(tmp_path)
        assert verdict["summary"] != _PLANTED_SUMMARY
        assert verdict["status"] == "failed"
        _, available = self._aggregate_sees(tmp_path, monkeypatch)
        assert "codex" not in available

    def test_touching_a_checked_out_verdict_file_does_not_promote_it(
        self, tmp_path: Path
    ) -> None:
        # The CLI runs with --sandbox workspace-write, so a model induced
        # by the PR can touch a file it did not create. Provenance that
        # rested on "newer than the start of the run" was therefore
        # answerable by the attacker it existed to stop; the file being
        # gone is not.
        self._plant(tmp_path / "verdict-openai.json")
        self._run_codex_step(tmp_path, touches="verdict-openai.json")
        assert self._verdict(tmp_path)["summary"] != _PLANTED_SUMMARY

    def test_a_symlink_at_the_temp_path_is_not_written_through(
        self, tmp_path: Path
    ) -> None:
        # Every temp path the step writes used to be "<target>.tmp" -- a
        # path in the PR's own checkout. Committed as a symlink it turned
        # a verdict write into a write to whatever it named. Both writers
        # are exercised here: the promotion, and the direct-write stamping
        # that runs on the file it promotes.
        victim = tmp_path / "victim.txt"
        victim.write_text("ORIGINAL", encoding="utf-8")
        (tmp_path / "review-codex.json.tmp").symlink_to(victim)
        self._run_codex_step(
            tmp_path,
            writes=[
                (
                    "verdict-openai.json",
                    json.dumps({"summary": "the real verdict", "issues": []}),
                )
            ],
        )
        assert victim.read_text(encoding="utf-8") == "ORIGINAL"
        assert self._verdict(tmp_path)["summary"] == "the real verdict"

    def test_a_directory_at_the_temp_path_does_not_kill_the_step(
        self, tmp_path: Path
    ) -> None:
        # The same PR-controlled path, committed as a directory: the
        # redirect fails, and under the runner's bash -e the step died
        # before any fallback could write a verdict. Same denial class as
        # a directory at a candidate name, one path over.
        (tmp_path / "review-codex.json.tmp").mkdir()
        self._run_codex_step(
            tmp_path,
            writes=[
                (
                    "verdict-openai.json",
                    json.dumps({"summary": "the real verdict", "issues": []}),
                )
            ],
        )
        assert self._verdict(tmp_path)["summary"] == "the real verdict"

    def test_symlinked_codex_verdict_is_replaced_not_followed(
        self, tmp_path: Path
    ) -> None:
        # review-codex.json is a workspace path, and the CLI runs with
        # --sandbox workspace-write, so the model can plant it as a symlink
        # pointing at a file outside the workspace -- the scripts snapshot,
        # or any runner-writable path. The host-side error-verdict write
        # must land a regular file at that name, never follow the link onto
        # the file it points at (AT-2941).
        # The harness's RUNNER_TEMP stand-in, where the scripts snapshot lives.
        (tmp_path / "runner-temp").mkdir(exist_ok=True)
        outside = tmp_path / "runner-temp" / "scripts-snapshot"
        outside.write_text("do not overwrite me", encoding="utf-8")
        self._run_codex_step(
            tmp_path,
            links=[("review-codex.json", outside)],
            exit_code=1,
        )
        assert outside.read_text(encoding="utf-8") == "do not overwrite me"
        assert not (tmp_path / "review-codex.json").is_symlink()
        assert self._verdict(tmp_path)["status"] == "failed"

    def test_symlinked_codex_verdict_from_stdout_extract_is_not_followed(
        self, tmp_path: Path
    ) -> None:
        # The stdout-extract fallback (gpt-5-mini / low-reasoning models)
        # reaches review-codex.json when the CLI wrote no sandbox file. A
        # symlink to an empty file outside the workspace makes that path
        # non-empty-test false, so the extractor runs; it too must not write
        # through the link (AT-2941).
        (tmp_path / "runner-temp").mkdir(exist_ok=True)
        outside = tmp_path / "runner-temp" / "empty-file"
        outside.write_text("", encoding="utf-8")
        verdict = {"summary": "real", "early_exit": False, "issues": []}
        self._run_codex_step(
            tmp_path,
            links=[("review-codex.json", outside)],
            log=json.dumps(verdict),
            exit_code=0,
        )
        assert outside.read_text(encoding="utf-8") == ""
        assert not (tmp_path / "review-codex.json").is_symlink()
        # The extractor wrote a real verdict at the workspace name.
        assert self._verdict(tmp_path)["summary"] == "real"

    @pytest.mark.parametrize("exit_code", [0, 1])
    def test_an_older_script_without_write_still_yields_an_error_verdict(
        self, tmp_path: Path, exit_code: int
    ) -> None:
        # Self-review runs this YAML against scripts pinned to the last
        # release, which may not carry the `write` verb yet. The error
        # branches must still leave a failed verdict, and still not follow
        # a link at the name (AT-2941).
        scripts_root = tmp_path / "pinned"
        shutil.copytree(_REPO_ROOT / ".github", scripts_root / ".github")
        promote = scripts_root / ".github/scripts/promote_legacy_verdict.sh"
        real = promote.with_name("promote_real.sh")
        promote.rename(real)
        promote.write_text(
            "#!/usr/bin/env bash\n"
            '[ "${1:-}" = write ] && { echo "usage" >&2; exit 2; }\n'
            f'exec bash {shlex.quote(str(real))} "$@"\n',
            encoding="utf-8",
        )
        workdir = tmp_path / "work"
        workdir.mkdir()
        (workdir / "runner-temp").mkdir()
        outside = workdir / "runner-temp" / "scripts-snapshot"
        outside.write_text("do not overwrite me", encoding="utf-8")
        self._run_codex_step(
            workdir,
            links=[("review-codex.json", outside)],
            exit_code=exit_code,
            scripts_root=scripts_root,
        )
        assert outside.read_text(encoding="utf-8") == "do not overwrite me"
        assert not (workdir / "review-codex.json").is_symlink()
        assert self._verdict(workdir)["status"] == "failed"

    def test_a_refused_write_is_not_installed_by_the_fallback(
        self, tmp_path: Path
    ) -> None:
        # The direct-write fallback is for a script without the verb (usage
        # exit 2) only. A `write` that refuses its payload exits 1, and that
        # refusal must stand rather than be installed anyway (AT-2941).
        scripts_root = tmp_path / "pinned"
        shutil.copytree(_REPO_ROOT / ".github", scripts_root / ".github")
        promote = scripts_root / ".github/scripts/promote_legacy_verdict.sh"
        real = promote.with_name("promote_real.sh")
        promote.rename(real)
        promote.write_text(
            "#!/usr/bin/env bash\n"
            '[ "${1:-}" = write ] && { cat > /dev/null; exit 1; }\n'
            f'exec bash {shlex.quote(str(real))} "$@"\n',
            encoding="utf-8",
        )
        workdir = tmp_path / "work"
        workdir.mkdir()
        with pytest.raises(AssertionError):
            self._run_codex_step(workdir, exit_code=1, scripts_root=scripts_root)
        assert not (workdir / "review-codex.json").exists()

    def test_a_directory_at_a_candidate_name_does_not_kill_the_step(
        self, tmp_path: Path
    ) -> None:
        # What the PR commits under these names is the PR's choice, and a
        # directory is one of them. `rm -f` fails on a directory, the
        # script runs under set -e and the step under the runner's bash -e,
        # so a plain -f would have turned "commit a directory" into "no
        # codex review, ever" -- a denial the PR under review controls.
        (tmp_path / "verdict-openai.json").mkdir()
        self._run_codex_step(tmp_path)
        assert self._verdict(tmp_path)["status"] == "failed"
        assert not (tmp_path / "verdict-openai.json").exists()

    def test_a_directory_at_the_target_is_replaced_not_moved_into(
        self, tmp_path: Path
    ) -> None:
        # `mv` moves its source INSIDE a directory sitting at the
        # destination and succeeds, so the promotion notice named a
        # promotion that had not happened and the verdict ended up at
        # review-codex.json/promote-verdict.XXXXXX. The clear defends
        # every other write in this script against a non-regular file;
        # this one did not.
        (tmp_path / "review-codex.json").mkdir()
        (tmp_path / "verdict-openai.json").write_text(
            json.dumps({"summary": "the real verdict", "issues": []})
        )
        self._mark(tmp_path)
        self._run_normalize_step(tmp_path)
        assert (tmp_path / "review-codex.json").is_file()
        assert self._verdict(tmp_path)["summary"] == "the real verdict"

    def test_a_checkout_without_the_script_skips_the_review(
        self, tmp_path: Path
    ) -> None:
        # The scripts come from the pinned release tag while this YAML can
        # come from a PR, so a script added in that PR is not in the
        # checkout yet. Running anyway was the wrong half of the choice:
        # promotion was skipped, but the clear is what stops a PR-supplied
        # verdict and the direct-write branch needs no script at all. The
        # review is skipped instead -- the CLI is never even started.
        ran = tmp_path / "the-cli-ran"
        self._run_codex_step(
            tmp_path, scripts_root=self._old_pin(tmp_path), marks_run=str(ran)
        )
        assert not ran.exists()
        verdict = self._verdict(tmp_path)
        assert verdict["status"] == "failed"
        assert verdict["error"] == "provenance_unavailable"
        assert "skipped" in verdict["summary"]

    def test_a_checkout_without_the_script_takes_no_file_the_pr_committed(
        self, tmp_path: Path
    ) -> None:
        # Both names, because the hole was that the guard covered only the
        # legacy ones: with no clear, a review-codex.json the PR committed
        # was read by the direct-write branch as this run's own verdict,
        # on every path rather than only the failing ones.
        self._plant(tmp_path / "verdict-openai.json")
        self._plant(tmp_path / "review-codex.json")
        self._run_codex_step(tmp_path, scripts_root=self._old_pin(tmp_path))
        self._run_normalize_step(tmp_path)
        verdict = self._verdict(tmp_path)
        assert verdict["summary"] != _PLANTED_SUMMARY
        assert verdict["error"] == "provenance_unavailable"

    def test_the_skipped_review_is_reported_as_a_reviewer_that_did_not_run(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A skip that the aggregate renders as a silent absence would trade
        # one unnoticed failure for another, so this follows it into the
        # real renderer rather than stopping at the file.
        self._run_codex_step(tmp_path, scripts_root=self._old_pin(tmp_path))
        _, available = self._aggregate_sees(tmp_path, monkeypatch)
        assert "codex" not in available
        summary = format_summary(
            load_reviews(),
            "comment",
            "reason",
            {},
            unreviewed_majors=0,
        )
        assert "### Codex -- [ ] not run (provenance_unavailable)" in summary


@requires_codex_step_tools
class TestCodexNormalizeNet(_CodexStepHarness):
    """The net for a run step that never reached its own promotion.

    'Normalize review file name' runs with always(), for a 'Run Codex
    review' that was cancelled or killed by its timeout-minutes. The
    marker says the run step took responsibility for the verdict file --
    by clearing the name, or by writing an infrastructure verdict there
    itself -- so without one nothing on disk is this run's and the net
    clears rather than promotes, with or without the script.
    """

    def test_the_normalize_step_promotes_over_an_empty_canonical_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The net for a run step that was cancelled or timed out before it
        # could promote anything: a zero-byte file is "nothing written"
        # here too, and testing existence alone let a truncated file mask a
        # good legacy verdict. Followed all the way to the aggregate,
        # because a promotion the aggregate then discards saves nothing --
        # the legacy schema carries no status, and the aggregate fails
        # closed on a missing one (AT-1954).
        self._mark(tmp_path)
        (tmp_path / "review-codex.json").write_text("")
        (tmp_path / "verdict-openai.json").write_text(
            json.dumps({"summary": "Codex reviewed the diff", "issues": []})
        )
        self._run_normalize_step(tmp_path)
        verdict = self._verdict(tmp_path)
        assert verdict["summary"] == "Codex reviewed the diff"
        assert verdict["early_exit"] is False
        assert verdict["status"] == "ok"
        _, available = self._aggregate_sees(tmp_path, monkeypatch)
        assert "codex" in available

    # BOTH steps, in the order and the configuration an ordinary Codex
    # failure takes in production: the script present, the run step
    # finishing with an error verdict of its own, then the net. Every
    # other test that chains the real steps either uses an older pin or
    # kills the step mid-CLI, and that gap is what let the status flip
    # through ten green checks. Parametrized over the two ways the run
    # step arrives at an error verdict, because the net must leave both.
    @pytest.mark.parametrize(
        ("step_kwargs", "error_kind"),
        [
            ({"log": "boom", "exit_code": 3}, "cli_invocation_failed"),
            ({"writes": [("review-codex.json", "[]")]}, "output_unparseable"),
        ],
        ids=["cli-exited-nonzero", "cli-wrote-a-non-verdict"],
    )
    def test_the_net_leaves_the_verdict_the_run_step_wrote_alone(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        step_kwargs: dict[str, Any],
        error_kind: str,
    ) -> None:
        # Half of the pair. The run step writes status "failed" when the
        # reviewer could not run; the net then promoted over that file and
        # the stamping re-derived the status to "ok", re-admitting a
        # reviewer that never ran into the aggregate's coverage count.
        # Our own verdict is not model output and is not re-judged.
        self._run_codex_step(tmp_path, **step_kwargs)
        assert self._verdict(tmp_path)["status"] == "failed"
        self._run_normalize_step(tmp_path)
        verdict = self._verdict(tmp_path)
        assert verdict["status"] == "failed"
        assert verdict["error"] == error_kind
        _, available = self._aggregate_sees(tmp_path, monkeypatch)
        assert "codex" not in available

    def test_a_model_emitted_failed_status_in_a_direct_write_is_re_derived(
        self, tmp_path: Path
    ) -> None:
        # The other half, and the reason the test above cannot be had by
        # reading the payload: "failed" with an error field is a shape a
        # MODEL can emit, and AT-1799 reserves that status for
        # infrastructure. The caller declares whose file it is; a model
        # cannot select infrastructure treatment for itself by choosing
        # what to write.
        self._run_codex_step(
            tmp_path,
            writes=[
                (
                    "review-codex.json",
                    json.dumps(
                        {
                            "summary": "I call this an infrastructure failure",
                            "status": "failed",
                            "early_exit": False,
                            "issues": [],
                            "error": "cli_invocation_failed",
                        }
                    ),
                )
            ],
        )
        assert self._verdict(tmp_path)["status"] == "ok"

    def test_the_net_stamps_a_verdict_the_run_step_never_reached(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The fall-through in --target-is-ours mode. A run step killed
        # between the CLI's write and its own promotion leaves raw model
        # output at the canonical name -- not yet a verdict, because the
        # legacy schema has no early_exit -- and model semantics are right
        # for exactly that. An infrastructure verdict never reaches this
        # path: every one the workflow writes is already shape-valid.
        self._mark(tmp_path)
        (tmp_path / "review-codex.json").write_text(
            json.dumps({"summary": "killed before stamping", "issues": []})
        )
        self._run_normalize_step(tmp_path)
        verdict = self._verdict(tmp_path)
        assert verdict["summary"] == "killed before stamping"
        assert verdict["status"] == "ok"
        assert verdict["early_exit"] is False
        _, available = self._aggregate_sees(tmp_path, monkeypatch)
        assert "codex" in available

    def test_the_net_does_not_trust_a_model_emitted_failed_status(
        self, tmp_path: Path
    ) -> None:
        # "failed" is reserved for infrastructure paths and is never
        # trusted from model output (AT-1799). The run step's own direct
        # write re-derives it; a promotion that kept it would make the two
        # paths disagree about the same file.
        self._mark(tmp_path)
        (tmp_path / "verdict-openai.json").write_text(
            json.dumps(
                {
                    "summary": "Codex reviewed the diff",
                    "status": "failed",
                    "early_exit": False,
                    "issues": [],
                }
            )
        )
        self._run_normalize_step(tmp_path)
        assert self._verdict(tmp_path)["status"] == "ok"

    def test_a_cancelled_run_leaves_the_net_nothing_the_pr_checked_out(
        self, tmp_path: Path
    ) -> None:
        # The net's provenance is the run step's clear, so this walks the
        # real sequence rather than asserting it: the PR's file is on disk,
        # the step is killed mid-CLI exactly as timeout-minutes kills it,
        # and the net runs over what that leaves.
        self._plant(tmp_path / "verdict-openai.json")
        self._run_codex_step_cancelled(tmp_path)
        assert (tmp_path / "runner-temp" / "codex-start").exists()
        assert not (tmp_path / "review-codex.json").exists()
        self._run_normalize_step(tmp_path)
        assert not (tmp_path / "review-codex.json").exists()

    def test_the_net_clears_without_the_script_when_the_cli_never_started(
        self, tmp_path: Path
    ) -> None:
        # The two steps answered the same question opposite ways: the run
        # step fails closed when the script is missing, the net did
        # nothing at all. That is the one path where no infrastructure
        # verdict exists to displace the PR's file either, because the run
        # step never got far enough to write one -- so 'Verify Codex
        # verdict file' would have reported it as a verdict present.
        self._plant(tmp_path / "review-codex.json")
        self._run_normalize_step(tmp_path, scripts_root=self._old_pin(tmp_path))
        assert not (tmp_path / "review-codex.json").exists()

    def test_the_normalize_step_promotes_nothing_when_the_cli_never_started(
        self, tmp_path: Path
    ) -> None:
        # No marker means the CLI never ran, so nothing was cleared and
        # whatever is on disk is the PR's -- including a canonical file,
        # which 'Verify Codex verdict file' would otherwise report as a
        # verdict present. The net clears instead of promoting, so that
        # step fails honestly.
        self._plant(tmp_path / "verdict-openai.json")
        self._plant(tmp_path / "review-codex.json")
        self._run_normalize_step(tmp_path)
        assert not (tmp_path / "review-codex.json").exists()
        assert not (tmp_path / "verdict-openai.json").exists()


class TestVerdictWriteDoesNotFollowLinks:
    """The Python verdict writers share one link-safe write (AT-2941).

    extract_codex_json.py, extract_claude_review.py and review_gemini.py
    all write their verdict through review_status.write_verdict_json, which
    must replace a symlink at the path rather than write through it -- the
    host-side mirror of promote_legacy_verdict.sh's temp+mv. The workspace
    is model-writable, so the path can be a link the model planted.
    """

    def test_a_symlinked_output_is_replaced_not_followed(self, tmp_path: Path) -> None:
        outside = tmp_path / "outside"
        outside.write_text("do not overwrite me", encoding="utf-8")
        target = tmp_path / "review-x.json"
        target.symlink_to(outside)
        write_verdict_json(target, {"summary": "real", "issues": []})
        assert outside.read_text(encoding="utf-8") == "do not overwrite me"
        assert not target.is_symlink()
        assert json.loads(target.read_text(encoding="utf-8"))["summary"] == "real"

    def test_the_local_shim_writer_does_not_follow_a_symlink(
        self, tmp_path: Path
    ) -> None:
        # The local shims run their CLI over a PR checkout too, so their
        # writer takes the same link-safe path.
        from local_reviewer_support import write_verdict

        outside = tmp_path / "outside"
        outside.write_text("do not overwrite me", encoding="utf-8")
        target = tmp_path / "review-claude.json"
        target.symlink_to(outside)
        write_verdict(str(target), {"summary": "real", "issues": []})
        assert outside.read_text(encoding="utf-8") == "do not overwrite me"
        assert not target.is_symlink()
        assert json.loads(target.read_text(encoding="utf-8"))["summary"] == "real"

    def test_a_plain_path_is_written(self, tmp_path: Path) -> None:
        target = tmp_path / "review-x.json"
        write_verdict_json(target, {"summary": "real", "issues": []})
        assert json.loads(target.read_text(encoding="utf-8"))["summary"] == "real"
        # The umask-derived mode the writers it replaced produced.
        mask = os.umask(0)
        os.umask(mask)
        assert target.stat().st_mode & 0o777 == 0o666 & ~mask


@requires_codex_step_tools
class TestPromotionScriptAgreesWithItsCallers:
    """The script's contracts, checked rather than asserted.

    None of these is visible in a diff: that its jq stamping does what
    review_status.stamp_model_status does, that the two steps calling it
    name the same file -- their fail-closed property is that neither
    promotes when the other cannot -- and that it touches only the plain
    file name it was handed.
    """

    # The cases that separate the candidate rules: a status the model may
    # emit is kept (including "ok" beside early_exit true, which the
    # Python keeps too), "failed" is never trusted from model output, an
    # unknown status is re-derived, and a missing early_exit defaults.
    PAYLOADS = [
        {"summary": "s", "issues": [], "early_exit": False},
        {"summary": "s", "issues": [], "early_exit": True},
        {"summary": "s", "issues": [], "status": "ok", "early_exit": True},
        {"summary": "s", "issues": [], "status": "early_exit", "early_exit": False},
        {"summary": "s", "issues": [], "status": "failed", "early_exit": False},
        {"summary": "s", "issues": [], "status": "failed", "early_exit": True},
        {"summary": "s", "issues": [], "status": "banana", "early_exit": False},
        {"summary": "s", "issues": []},
    ]

    @pytest.mark.parametrize("payload", PAYLOADS, ids=range(len(PAYLOADS)))
    def test_the_scripts_stamping_matches_stamp_model_status(
        self, tmp_path: Path, payload: dict[str, Any]
    ) -> None:
        (tmp_path / "verdict-openai.json").write_text(json.dumps(payload))
        result = subprocess.run(
            ["bash", str(_PROMOTE_SCRIPT), "promote", "review-codex.json"],
            cwd=tmp_path,
            env={"PATH": os.environ["PATH"], "RUNNER_TEMP": str(tmp_path)},
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode == 0, result.stderr
        promoted = json.loads((tmp_path / "review-codex.json").read_text())

        expected = dict(payload)
        expected.setdefault("early_exit", False)
        stamp_model_status(expected)

        assert promoted["status"] == expected["status"]
        assert promoted["early_exit"] == expected["early_exit"]

    def test_the_run_step_keeps_no_copy_of_the_status_rule(self) -> None:
        # The inline direct-write filter was the third copy of the AT-1799
        # rule and had already diverged from the other two. The step now
        # calls the script for that too, so the rule is written once.
        body = _step_script(_CODEX_RUN_STEP)
        assert ".status = (" not in body
        assert '.status == "ok"' not in body
        assert "stamp review-codex.json" in body

    @pytest.mark.parametrize(
        ("argv", "expected_status"),
        [
            (["promote"], "ok"),
            (["promote", "--target-is-ours"], "failed"),
        ],
        ids=["model-output-is-re-derived", "our-verdict-is-preserved"],
    )
    def test_whose_target_it_is_comes_from_the_caller(
        self, tmp_path: Path, argv: list[str], expected_status: str
    ) -> None:
        """The two modes, asked of one identical file.

        Measured at the script rather than through the step, because the
        step's later `stamp` call re-derives the status anyway and so
        hides which mode `promote` used -- the first guard-strip of this
        distinction passed for exactly that reason. A verdict that is
        shape-valid and says "failed" is the one payload where the modes
        must disagree, and it is a payload a model can write, which is
        why the answer cannot be read off the file.
        """
        payload = {
            "summary": "failed, by whoever wrote this",
            "status": "failed",
            "early_exit": False,
            "issues": [],
            "error": "cli_invocation_failed",
        }
        (tmp_path / "review-codex.json").write_text(json.dumps(payload))
        result = subprocess.run(
            ["bash", str(_PROMOTE_SCRIPT), *argv, "review-codex.json"],
            cwd=tmp_path,
            env={"PATH": os.environ["PATH"], "RUNNER_TEMP": str(tmp_path)},
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode == 0, result.stderr
        written = json.loads((tmp_path / "review-codex.json").read_text())
        assert written["status"] == expected_status

    def test_target_is_ours_drops_a_symlinked_target_before_reading_it(
        self, tmp_path: Path
    ) -> None:
        # The `ours` fast path asks is_verdict of the target, and jq reads
        # through a link, so a link to a valid-looking verdict outside the
        # workspace used to stand as this run's verdict (AT-2941).
        outside = tmp_path / "outside.json"
        outside.write_text(
            json.dumps(
                {"summary": "s", "status": "ok", "early_exit": False, "issues": []}
            )
        )
        workdir = tmp_path / "work"
        workdir.mkdir()
        (workdir / "review-codex.json").symlink_to(outside)
        result = subprocess.run(
            [
                "bash",
                str(_PROMOTE_SCRIPT),
                "promote",
                "--target-is-ours",
                "review-codex.json",
            ],
            cwd=workdir,
            env={"PATH": os.environ["PATH"], "RUNNER_TEMP": str(tmp_path)},
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode == 0, result.stderr
        assert not (workdir / "review-codex.json").is_symlink()
        assert json.loads(outside.read_text())["summary"] == "s"

    def test_a_symlinked_legacy_candidate_is_not_promoted(self, tmp_path: Path) -> None:
        # A legacy candidate planted as a link between the clear and the
        # in-run promotion must not have the link's target promoted onto
        # the canonical verdict (AT-2941).
        outside = tmp_path / "outside.json"
        outside.write_text(
            json.dumps({"summary": "from outside", "early_exit": False, "issues": []})
        )
        workdir = tmp_path / "work"
        workdir.mkdir()
        (workdir / "verdict-openai.json").symlink_to(outside)
        result = subprocess.run(
            ["bash", str(_PROMOTE_SCRIPT), "promote", "review-codex.json"],
            cwd=workdir,
            env={"PATH": os.environ["PATH"], "RUNNER_TEMP": str(tmp_path)},
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode == 0, result.stderr
        assert not (workdir / "review-codex.json").exists()
        assert not (workdir / "verdict-openai.json").is_symlink()
        assert json.loads(outside.read_text())["summary"] == "from outside"

    @pytest.mark.parametrize("payload", ["", "not json", "[]", '{"foo": 1}'])
    def test_write_refuses_a_payload_that_is_not_a_verdict(
        self, tmp_path: Path, payload: str
    ) -> None:
        result = subprocess.run(
            ["bash", str(_PROMOTE_SCRIPT), "write", "review-codex.json"],
            cwd=tmp_path,
            env={"PATH": os.environ["PATH"], "RUNNER_TEMP": str(tmp_path)},
            input=payload,
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode != 0
        assert not (tmp_path / "review-codex.json").exists()

    def test_write_installs_a_verdict(self, tmp_path: Path) -> None:
        verdict = {
            "summary": "s",
            "status": "failed",
            "early_exit": False,
            "issues": [],
        }
        result = subprocess.run(
            ["bash", str(_PROMOTE_SCRIPT), "write", "review-codex.json"],
            cwd=tmp_path,
            env={"PATH": os.environ["PATH"], "RUNNER_TEMP": str(tmp_path)},
            input=json.dumps(verdict),
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode == 0, result.stderr
        assert json.loads((tmp_path / "review-codex.json").read_text()) == verdict
        # The umask-derived mode a plain redirect would have produced, the
        # same rule the Python writer follows.
        mask = os.umask(0)
        os.umask(mask)
        mode = (tmp_path / "review-codex.json").stat().st_mode & 0o777
        assert mode == 0o666 & ~mask

    @pytest.mark.parametrize("mode", ["clear", "promote", "stamp", "write"])
    @pytest.mark.parametrize(
        "target",
        [
            "../outside.txt",
            "/etc/passwd",
            "sub/dir.json",
            "",
            "..",
            # Refused rather than merely marked: the `--` on every rm and
            # mv settles those two, but `clear -rf` would still sweep the
            # legacy candidates and leave the TARGET standing, which is a
            # PR-committed review-codex.json read as this run's verdict.
            # jq reads a dash-leading name as an option besides.
            "-rf",
            "--help",
        ],
    )
    def test_the_script_takes_only_a_plain_file_name(
        self, tmp_path: Path, mode: str, target: str
    ) -> None:
        # Measured before the guard existed: `clear ../outside.txt`
        # deleted a file outside the working directory. Both call sites
        # pass literals today, and this script exists precisely to be the
        # one place that rm -rf happens, so the one place should not
        # accept an argument it cannot bound.
        outside = tmp_path / "outside.txt"
        outside.write_text("SHOULD SURVIVE", encoding="utf-8")
        workdir = tmp_path / "work"
        workdir.mkdir()
        result = subprocess.run(
            ["bash", str(_PROMOTE_SCRIPT), mode, target],
            cwd=workdir,
            env={"PATH": os.environ["PATH"], "RUNNER_TEMP": str(tmp_path)},
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode == 2, result.stdout
        assert "refusing to operate" in result.stderr
        assert outside.read_text(encoding="utf-8") == "SHOULD SURVIVE"

    def test_both_steps_name_the_same_promotion_script(self) -> None:
        paths = {
            match
            for step in (_CODEX_RUN_STEP, _NORMALIZE_STEP)
            for match in re.findall(r'PROMOTE="([^"]+)"', _step_script(step))
        }
        assert len(paths) == 1, paths
        # And it is a real file, not a path that merely agrees with itself:
        # the guard around every call treats "not there" as "do nothing",
        # so a typo would disable the promotion in silence on both sides.
        named = paths.pop().replace("${RUNNER_TEMP}/ai-dev-pr-review/", "", 1)
        assert (_REPO_ROOT / named).is_file()


_ORCHESTRATOR_WORKFLOW = (
    Path(__file__).resolve().parents[2]
    / "workflows"
    / "base-ai-review-orchestrator.yml"
)


class TestTheDestructiveCommandScan:
    """The `--` convention across the script, and the scan that checks it.

    UNGATED, deliberately. These read the script source and run
    regexes: no subprocess, no jq, no python3 beyond this interpreter.
    A jq skip-if here would stop the marker being enforced across the
    whole script on a machine that lacks jq, and say only "skipped".

    The parametrized cases below are the catalogue the helper's
    docstring points here for: the shapes the scan must catch, and the
    ones it must not flag. They run on every suite execution, so a
    shape listed here is one the scan is known to handle and not one it
    is merely meant to.
    """

    @pytest.mark.parametrize(
        "line",
        [
            'rm -rf "$name"',
            'mv "$tmp" "$target"',
            'rm -f -- "$tmp" && rm -rf "$dir"',
            'rm -rf "$name" || echo -- skipped',
            'if [ -e "$x" ]; then rm -rf "$x"; fi',
            'out=$(rm -rf "$x")',
            'out=`rm -rf "$x"`',
            'echo "#"; rm -rf "$x"',
            '[ "$x" = "#" ] && rm -rf "$x"',
            'rm -rf \\\n    "$name"',
            'rm -rf -- "$x"  # note \\\n    rm -rf "$y"',
        ],
        ids=[
            "bare-rm",
            "bare-mv",
            "second-command-on-the-line",
            "marker-belongs-to-a-neighbour",
            "after-then",
            "inside-a-substitution",
            "inside-a-backtick-substitution",
            "after-a-quoted-hash",
            "after-a-quoted-hash-in-a-test",
            "split-over-a-line-continuation",
            "after-a-comment-ending-in-a-backslash",
        ],
    )
    def test_the_scan_catches_an_unguarded_command(self, line: str) -> None:
        assert _unguarded_destructive_commands(line)

    @pytest.mark.parametrize(
        "line",
        [
            'rm -rf -- "$name"',
            'mv -- "$tmp" "$target"',
            'rm -f -- "$tmp" && rm -rf -- "$dir"',
            '# rm -rf "$name"',
            'confirm "$name"',
            'rm -rf -- "$name"  # rm -rf without a marker, in prose',
            'rm -rf -- "${name#./}"',
            'rm -rf -- \\\n    "$name"',
        ],
        ids=[
            "guarded-rm",
            "guarded-mv",
            "both-guarded",
            "a-comment",
            "a-word-ending-in-rm",
            "prose-after-the-code",
            "a-hash-inside-an-expansion",
            "guarded-over-a-line-continuation",
        ],
    )
    def test_the_scan_does_not_cry_wolf(self, line: str) -> None:
        assert not _unguarded_destructive_commands(line)

    def test_every_destructive_command_ends_its_options(self) -> None:
        """Every `rm` and `mv` in the script takes `--` before its operands.

        Asserted against the SOURCE: require_verdict_name rejects a
        dash-leading name before any of these run, so a behavioural
        test would pass whether or not the marker were present. This
        one fails when a marker is dropped from an invocation the scan
        can see. jq is excluded -- see the script for why.
        """
        offenders = _unguarded_destructive_commands(
            _PROMOTE_SCRIPT.read_text(encoding="utf-8")
        )
        assert not offenders, "missing an end-of-options marker: " + "; ".join(
            offenders
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

    def test_unset_takes_the_normal_path(self, monkeypatch: pytest.MonkeyPatch) -> None:
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
            # The all-absent round fails CI after posting (AT-2569).
            pytest.raises(SystemExit),
        ):
            main()
        mock_load.assert_called_once()
        assert mock_post.call_args[0][1] == "comment"

    def test_summary_is_ascii(self) -> None:
        """Public-repo invariant, asserted at the point the string is built."""
        format_size_skip_summary(
            [SizeBreach("4200", "added", "PR_SIZE_LIMIT_ADDITIONS", "3000")]
        ).encode("ascii")


class TestSizeSkipPerAxis:
    """Per-axis size limits (AT-3052).

    With no split var prepare reports the combined axis, and everything the
    aggregate renders must read exactly as it did before; with one, the
    headline, remedies and verdict line name the var each axis was held to.
    """

    _COMBINED_BODY = "\n".join(
        [
            REVIEW_MARKER,
            "## [bot] Multi-LLM Review Summary",
            "",
            "**Result: [X] Review skipped -- PR too large** -- 4200 changed"
            " lines exceeds the limit of 3000",
            "",
            "---",
            "",
            "No reviewer ran, so this check reports a failure rather than a"
            " verdict. That is deliberate: a required check that is never"
            " reported stays Pending forever with nothing to act on.",
            "",
            "To proceed, either:",
            "",
            "1. **Split this PR** so each part is at or under 3000 changed lines."
            " Over by 1200 changed lines; split into at least 2 PRs.",
            "2. **Raise the limit for this repository** by setting the"
            " `PR_SIZE_LIMIT` repository variable above 4200"
            " (Settings -> Secrets and variables -> Actions -> Variables),"
            " then re-run this workflow.",
            "",
            "Raising the limit makes the reviewers read a larger diff, which"
            " costs more and reviews less closely -- splitting is preferred"
            " where it is possible.",
        ]
    )

    @staticmethod
    def _run(
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
        **env: str,
    ) -> tuple[str, str]:
        """Run main() on the size-skip path; return (posted body, stdout)."""
        monkeypatch.setenv("SIZE_SKIPPED", "true")
        monkeypatch.setenv("PR_NUMBER", "42")
        monkeypatch.setenv("GITHUB_REPOSITORY", "owner/repo")
        for key in ("SIZE_EXCEEDED", "SIZE_AXES", "SIZE_ADDITIONS", "SIZE_DELETIONS"):
            monkeypatch.delenv(key, raising=False)
        for key, value in env.items():
            monkeypatch.setenv(key, value)
        with (
            patch("aggregate_reviews.post_verdict") as mock_post,
            pytest.raises(SystemExit) as excinfo,
        ):
            main()
        assert excinfo.value.code == 1
        assert mock_post.call_args[0][1] == "request_changes"
        return mock_post.call_args[0][0], capsys.readouterr().out

    @pytest.mark.parametrize(
        "prepare_env",
        [
            {},  # a prepare older than AT-3052 sends no exceeded/axes
            {"SIZE_EXCEEDED": "PR_SIZE_LIMIT", "SIZE_AXES": "changed"},
        ],
        ids=["pre-AT-3052", "combined"],
    )
    def test_only_pr_size_limit_renders_byte_identically(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
        prepare_env: dict[str, str],
    ) -> None:
        body, out = self._run(
            monkeypatch,
            capsys,
            SIZE_TOTAL="4200",
            SIZE_LIMIT="3000",
            SIZE_ADDITIONS="4000",
            SIZE_DELETIONS="200",
            **prepare_env,
        )
        assert body == self._COMBINED_BODY
        assert (
            "Final verdict: request_changes -- review skipped, 4200 changed"
            " lines exceeds PR_SIZE_LIMIT 3000"
        ) in out

    def test_additions_over_their_limit_name_that_var(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        body, out = self._run(
            monkeypatch,
            capsys,
            SIZE_EXCEEDED="PR_SIZE_LIMIT_ADDITIONS",
            SIZE_AXES="added",
            SIZE_TOTAL="2500",
            SIZE_LIMIT="2000",
        )
        assert (
            "**Result: [X] Review skipped -- PR too large** -- 2500 added lines"
            " exceeds PR_SIZE_LIMIT_ADDITIONS 2000"
        ) in body
        assert "at or under 2000 added lines" in body
        assert "`PR_SIZE_LIMIT_ADDITIONS` repository variable above 2500" in body
        assert "`PR_SIZE_LIMIT` " not in body
        assert (
            "Final verdict: request_changes -- review skipped, 2500 added"
            " lines exceeds PR_SIZE_LIMIT_ADDITIONS 2000"
        ) in out

    def test_an_axis_without_its_own_var_names_pr_size_limit(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        body, out = self._run(
            monkeypatch,
            capsys,
            SIZE_EXCEEDED="PR_SIZE_LIMIT",
            SIZE_AXES="deleted",
            SIZE_TOTAL="3500",
            SIZE_LIMIT="3000",
        )
        assert "-- 3500 deleted lines exceeds PR_SIZE_LIMIT 3000" in body
        assert "`PR_SIZE_LIMIT` repository variable above 3500" in body
        assert "review skipped, 3500 deleted lines exceeds PR_SIZE_LIMIT 3000" in out

    def test_both_axes_exceeded_join_their_parts(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        body, out = self._run(
            monkeypatch,
            capsys,
            SIZE_EXCEEDED="PR_SIZE_LIMIT_ADDITIONS PR_SIZE_LIMIT_DELETIONS",
            SIZE_AXES="added deleted",
            SIZE_TOTAL="1500 2000",
            SIZE_LIMIT="1000 1200",
        )
        joined = (
            "1500 added lines exceeds PR_SIZE_LIMIT_ADDITIONS 1000;"
            " 2000 deleted lines exceeds PR_SIZE_LIMIT_DELETIONS 1200"
        )
        assert f"**Result: [X] Review skipped -- PR too large** -- {joined}" in body
        assert "at or under 1000 added lines and 1200 deleted lines" in body
        assert (
            "the `PR_SIZE_LIMIT_ADDITIONS` repository variable above 1500 and"
            " the `PR_SIZE_LIMIT_DELETIONS` repository variable above 2000"
        ) in body
        assert f"Final verdict: request_changes -- review skipped, {joined}" in out

    def test_short_lists_degrade_to_unknown(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A measurement missing for an entry still blocks, and says so."""
        body, _ = self._run(
            monkeypatch,
            capsys,
            SIZE_EXCEEDED="PR_SIZE_LIMIT_ADDITIONS PR_SIZE_LIMIT_DELETIONS",
            SIZE_AXES="added deleted",
            SIZE_TOTAL="1500",
            SIZE_LIMIT="1000",
        )
        assert "unknown deleted lines exceeds PR_SIZE_LIMIT_DELETIONS unknown" in body
        # An overage cannot be computed from "unknown", so none is claimed.
        assert "Over by" not in body

    def test_both_axes_exceeded_name_each_overage_and_one_split_count(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        body, _ = self._run(
            monkeypatch,
            capsys,
            SIZE_EXCEEDED="PR_SIZE_LIMIT_ADDITIONS PR_SIZE_LIMIT_DELETIONS",
            SIZE_AXES="added deleted",
            SIZE_TOTAL="1500 2500",
            SIZE_LIMIT="1000 1200",
        )
        assert (
            "at or under 1000 added lines and 1200 deleted lines."
            " Over by 500 added lines and 1300 deleted lines;"
            " split into at least 3 PRs."
        ) in body


class TestSizeWarning:
    """A PR approaching its size limit is warned, not blocked (AT-2415).

    prepare reports each check at or over PR_SIZE_WARN_PERCENT of its limit
    in SIZE_WARNED / SIZE_WARN_AXES / SIZE_WARN_TOTAL / SIZE_WARN_LIMIT. The
    aggregate renders one line per entry under the headline and changes
    nothing else: not the headline, not the verdict, not the exit code.
    """

    _WARN_KEYS = ("SIZE_WARNED", "SIZE_WARN_AXES", "SIZE_WARN_TOTAL", "SIZE_WARN_LIMIT")

    @classmethod
    def _run(cls, monkeypatch: pytest.MonkeyPatch, **env: str) -> tuple[str, str]:
        """Run main() on the reviewed path; return (posted body, verdict).

        Every reviewer returns a clean payload, so the round is a real review
        that approves and exits 0. An all-absent round fails CI (AT-2569),
        which would hide whether the warning left the exit code alone.
        """
        monkeypatch.delenv("SIZE_SKIPPED", raising=False)
        for key in cls._WARN_KEYS:
            monkeypatch.delenv(key, raising=False)
        for name in REVIEWER_NAMES:
            monkeypatch.setenv(f"REVIEWER_RESULT_{name.upper()}", "success")
        monkeypatch.setenv("PR_NUMBER", "42")
        monkeypatch.setenv("GITHUB_REPOSITORY", "owner/repo")
        for key, value in env.items():
            monkeypatch.setenv(key, value)
        with (
            patch(
                "aggregate_reviews.load_reviews",
                return_value={n: _make_named_review(n, []) for n in REVIEWER_NAMES},
            ),
            patch("aggregate_reviews.post_verdict") as mock_post,
        ):
            main()
        return mock_post.call_args[0][0], mock_post.call_args[0][1]

    def test_under_the_threshold_nothing_is_added(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        body, _ = self._run(monkeypatch, SIZE_WARNED="")
        assert "[!] size:" not in body

    def test_a_warned_check_adds_one_line_and_changes_nothing_else(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        plain_body, plain_verdict = self._run(monkeypatch)
        body, verdict = self._run(
            monkeypatch,
            SIZE_WARNED="PR_SIZE_LIMIT",
            SIZE_WARN_AXES="changed",
            SIZE_WARN_TOTAL="2526",
            SIZE_WARN_LIMIT="3000",
        )
        line = (
            "\n\n> [!] size: 2526/3000 changed lines (84% of PR_SIZE_LIMIT); the"
            " review is skipped above the limit. Consider splitting."
        )
        assert line in body
        assert verdict == plain_verdict
        assert body.replace(line, "", 1) == plain_body

    def test_per_axis_warnings_name_each_var(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        body, _ = self._run(
            monkeypatch,
            SIZE_WARNED="PR_SIZE_LIMIT_ADDITIONS PR_SIZE_LIMIT",
            SIZE_WARN_AXES="added deleted",
            SIZE_WARN_TOTAL="1700 2500",
            SIZE_WARN_LIMIT="2000 3000",
        )
        assert (
            "> [!] size: 1700/2000 added lines (85% of PR_SIZE_LIMIT_ADDITIONS);"
            in body
        )
        assert "> [!] size: 2500/3000 deleted lines (83% of PR_SIZE_LIMIT);" in body

    def test_an_unparseable_entry_is_dropped_not_rendered(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A short list cannot invent a percentage; the review still posts."""
        body, _ = self._run(
            monkeypatch,
            SIZE_WARNED="PR_SIZE_LIMIT_ADDITIONS PR_SIZE_LIMIT",
            SIZE_WARN_AXES="added deleted",
            SIZE_WARN_TOTAL="1700",
            SIZE_WARN_LIMIT="2000",
        )
        assert body.count("[!] size:") == 1


_POLICY_SKIP_MARKER = "<!-- lens:skipped reason=policy-excluded-only files=2 -->"


class TestPolicySkipVerdict:
    """A PR whose every file is policy-excluded is skipped and reports success.

    prepare drops the hunks of files matching `.github/lens-ignore` before any
    reviewer reads the diff (AT-2206). When nothing is left the reviewer jobs
    are skipped as they are for size, but the outcome differs: the content is
    not review material by the consumer's own rule, so the check passes, and
    the comment carries a marker a stricter consumer gate can key on.
    """

    @staticmethod
    def _set_policy_env(
        monkeypatch: pytest.MonkeyPatch,
        paths: str = "secrets/key.pem\nconfig/prod.env -> config/live.env",
    ) -> None:
        monkeypatch.setenv("SIZE_SKIPPED", "false")
        monkeypatch.setenv("POLICY_SKIPPED", "true")
        monkeypatch.setenv("EXCLUDED_COUNT", str(len(paths.splitlines())))
        monkeypatch.setenv("EXCLUDED_PATHS", paths)
        monkeypatch.setenv("PR_NUMBER", "42")
        monkeypatch.setenv("GITHUB_REPOSITORY", "owner/repo")
        monkeypatch.delenv("HEAD_SHA", raising=False)

    def test_policy_skip_exits_zero(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._set_policy_env(monkeypatch)
        with (
            patch("aggregate_reviews.post_verdict") as mock_post,
            pytest.raises(SystemExit) as excinfo,
        ):
            main()
        assert excinfo.value.code == 0
        mock_post.assert_called_once()

    def test_policy_skip_is_a_comment_never_an_approval(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Nothing was reviewed, so no APPROVED review may attest that it was."""
        self._set_policy_env(monkeypatch)
        monkeypatch.setenv("ALLOW_AUTO_APPROVE", "true")
        with (
            patch("aggregate_reviews.post_verdict") as mock_post,
            pytest.raises(SystemExit),
        ):
            main()
        assert mock_post.call_args[0][1] == "comment"

    def test_policy_skip_posts_through_the_comment_branch(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """End to end through post_verdict: `gh pr comment`, never `gh pr review`."""
        self._set_policy_env(monkeypatch)
        monkeypatch.setenv("ALLOW_AUTO_APPROVE", "true")
        with (
            patch("aggregate_reviews._fold_prior_round_items"),
            patch("aggregate_reviews.subprocess.run") as mock_run,
            pytest.raises(SystemExit) as excinfo,
        ):
            mock_run.return_value.returncode = 0
            main()
        assert excinfo.value.code == 0
        commands = [call.args[0] for call in mock_run.call_args_list]
        assert commands == [
            ["gh", "pr", "comment", "42", "--body-file", "-", "--repo", "owner/repo"]
        ]

    def test_policy_skip_body_carries_both_markers_in_order(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """REVIEW_MARKER keeps stale-comment minimization; the skip marker is exact."""
        self._set_policy_env(monkeypatch)
        with (
            patch("aggregate_reviews.post_verdict") as mock_post,
            pytest.raises(SystemExit),
        ):
            main()
        body = mock_post.call_args[0][0]
        lines = body.splitlines()
        assert lines[0] == REVIEW_MARKER
        assert lines[1] == _POLICY_SKIP_MARKER

    def test_policy_skip_body_names_the_paths_and_the_rule_file(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._set_policy_env(monkeypatch)
        with (
            patch("aggregate_reviews.post_verdict") as mock_post,
            pytest.raises(SystemExit),
        ):
            main()
        body = mock_post.call_args[0][0]
        assert (
            "**Result: [OK] Review skipped -- only policy-excluded files changed**"
            in body
        )
        assert ".github/lens-ignore" in body
        assert "- `secrets/key.pem`" in body
        assert "- `config/prod.env -> config/live.env`" in body
        assert "2 file(s) excluded by policy" in body

    def test_policy_skip_prints_the_final_verdict(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        self._set_policy_env(monkeypatch)
        with patch("aggregate_reviews.post_verdict"), pytest.raises(SystemExit):
            main()
        assert (
            "Final verdict: none -- review skipped, 2 policy-excluded file(s) only"
            in capsys.readouterr().out
        )

    def test_policy_skip_never_reads_reviewer_artifacts(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._set_policy_env(monkeypatch)
        with (
            patch("aggregate_reviews.load_reviews") as mock_load,
            patch("aggregate_reviews.post_verdict"),
            pytest.raises(SystemExit),
        ):
            main()
        mock_load.assert_not_called()

    def test_size_skip_precedes_the_policy_skip(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The policy step never runs on a size skip, so size owns the verdict."""
        self._set_policy_env(monkeypatch)
        monkeypatch.setenv("SIZE_SKIPPED", "true")
        monkeypatch.setenv("SIZE_TOTAL", "4200")
        monkeypatch.setenv("SIZE_LIMIT", "3000")
        with (
            patch("aggregate_reviews.post_verdict") as mock_post,
            pytest.raises(SystemExit) as excinfo,
        ):
            main()
        assert excinfo.value.code == 1
        assert "PR too large" in mock_post.call_args[0][0]

    def test_missing_paths_still_skip_with_a_placeholder(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._set_policy_env(monkeypatch, paths="")
        with (
            patch("aggregate_reviews.post_verdict") as mock_post,
            pytest.raises(SystemExit) as excinfo,
        ):
            main()
        assert excinfo.value.code == 0
        body = mock_post.call_args[0][0]
        assert "files=0 -->" in body
        assert "(paths not reported)" in body

    @pytest.mark.parametrize("value", ["false", "", "False", "no"])
    def test_normal_run_takes_the_normal_path(
        self, monkeypatch: pytest.MonkeyPatch, value: str
    ) -> None:
        monkeypatch.setenv("POLICY_SKIPPED", value)
        assert _policy_skip_details() is None

    def test_unset_takes_the_normal_path(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Consumers pinned to an older tag send no policy inputs at all."""
        monkeypatch.delenv("POLICY_SKIPPED", raising=False)
        assert _policy_skip_details() is None

    def test_summary_is_ascii(self) -> None:
        format_policy_skip_summary(["secrets/key.pem"]).encode("ascii")

    def test_policy_skip_body_cannot_be_broken_by_a_path(self) -> None:
        """prepare sanitizes first; the aggregate renders defensively anyway."""
        body = format_policy_skip_summary(["tick`.pem", "esc\x1b[0m.txt"])
        assert "- `tick\u02cb.pem`" in body
        assert "- `esc\\x1b[0m.txt`" in body
        assert "tick`" not in body
        assert "\x1b" not in body


class TestPartialExclusionOnTheVerdict:
    """A diff with some files removed by policy says so on the verdict, paths only."""

    def test_no_exclusion_adds_no_line(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("EXCLUDED_COUNT", raising=False)
        monkeypatch.delenv("EXCLUDED_PATHS", raising=False)
        reviews = {n: _make_review() for n in REVIEWER_NAMES}
        body = format_summary(
            reviews,
            "approve",
            "3/3 LLM responses -- no issues",
            reviews,
            unreviewed_majors=_count_unreviewed_majors(reviews),
        )
        assert "excluded by policy" not in body

    def test_zero_count_adds_no_line(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("EXCLUDED_COUNT", "0")
        monkeypatch.setenv("EXCLUDED_PATHS", "")
        reviews = {n: _make_review() for n in REVIEWER_NAMES}
        body = format_summary(
            reviews,
            "approve",
            "3/3 LLM responses -- no issues",
            reviews,
            unreviewed_majors=_count_unreviewed_majors(reviews),
        )
        assert "excluded by policy" not in body

    def test_exclusion_is_listed_under_the_headline(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("EXCLUDED_COUNT", "2")
        monkeypatch.setenv(
            "EXCLUDED_PATHS", "secrets/key.pem\nconfig/prod.env -> config/live.env"
        )
        reviews = {n: _make_review() for n in REVIEWER_NAMES}
        body = format_summary(
            reviews,
            "approve",
            "3/3 LLM responses -- no issues",
            reviews,
            unreviewed_majors=_count_unreviewed_majors(reviews),
        )
        lines = body.splitlines()
        headline = next(
            i for i, line in enumerate(lines) if line.startswith("**Result:")
        )
        note = (
            "> [i] 2 file(s) excluded by policy (.github/lens-ignore):"
            " `secrets/key.pem`, `config/prod.env -> config/live.env`"
        )
        assert lines[headline + 2] == note
        assert lines[headline + 1] == ""

    def test_count_falls_back_to_the_path_list(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("EXCLUDED_COUNT", "")
        monkeypatch.setenv("EXCLUDED_PATHS", "secrets/key.pem")
        reviews = {n: _make_review() for n in REVIEWER_NAMES}
        body = format_summary(
            reviews,
            "approve",
            "3/3 LLM responses -- no issues",
            reviews,
            unreviewed_majors=_count_unreviewed_majors(reviews),
        )
        assert (
            "> [i] 1 file(s) excluded by policy (.github/lens-ignore): `secrets/key.pem`"
            in body
        )

    def test_verdict_note_cannot_be_broken_by_a_path(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """prepare sanitizes first; the aggregate renders defensively anyway."""
        monkeypatch.setenv("EXCLUDED_COUNT", "1")
        monkeypatch.setenv("EXCLUDED_PATHS", "tick`\x1b[0m.pem")
        reviews = {n: _make_review() for n in REVIEWER_NAMES}
        body = format_summary(
            reviews,
            "approve",
            "3/3 LLM responses -- no issues",
            reviews,
            unreviewed_majors=_count_unreviewed_majors(reviews),
        )
        assert "(.github/lens-ignore): `tick\u02cb\\x1b[0m.pem`" in body
        assert "tick`" not in body
        assert "\x1b" not in body

    def test_partial_exclusion_does_not_skip(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Only the all-excluded case skips; a partial diff is reviewed."""
        monkeypatch.setenv("POLICY_SKIPPED", "false")
        monkeypatch.setenv("EXCLUDED_COUNT", "1")
        monkeypatch.setenv("EXCLUDED_PATHS", "secrets/key.pem")
        assert _policy_skip_details() is None


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
        for key in (
            "size_skipped",
            "size_total",
            "size_limit",
            "size_additions",
            "size_deletions",
            "size_exceeded",
            "size_axes",
        ):
            assert key in supplied, key
            # Threaded from prepare's output of the same name (AT-3052).
            assert supplied[key] == f"${{{{ needs.prepare.outputs.{key} }}}}", key
        # The size flag itself, not the combined `skip` that a policy skip
        # also sets -- otherwise a policy skip renders as PR too large.
        assert supplied["size_skipped"] == "${{ needs.prepare.outputs.size_skipped }}"


_PREPARE_WORKFLOW = (
    Path(__file__).resolve().parents[2] / "workflows" / "base-ai-review-prepare.yml"
)
_AGGREGATE_WORKFLOW = (
    Path(__file__).resolve().parents[2] / "workflows" / "base-ai-review-aggregate.yml"
)


def _workflow(path: Path) -> dict[str | bool, Any]:
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
        supplied = dict(
            _workflow(_ORCHESTRATOR_WORKFLOW)["jobs"]["prepare"].get("with") or {}
        )
        assert "head_sha" not in supplied

    @pytest.mark.parametrize("path", [_SINGLE_WORKFLOW, _AGGREGATE_WORKFLOW])
    def test_checkout_prefers_the_resolved_head(self, path: Path) -> None:
        job = next(iter(_workflow(path)["jobs"].values()))
        checkout = next(
            s
            for s in job["steps"]
            if str(s.get("name", "")).startswith("Checkout caller repo")
        )
        ref = str(checkout["with"]["ref"])
        assert "inputs.head_sha" in ref
        # The fallback is the resolve step's `fallback_ref`: the caller's event
        # SHA or `github.ref` when the repository under review is the caller,
        # nothing on a cross-repository run (AT-2520; the shape is pinned in
        # test_target_repo_threading.py).
        assert ref.index("inputs.head_sha") < ref.index(
            "steps.target.outputs.fallback_ref"
        )

    @pytest.mark.parametrize("path", [_SINGLE_WORKFLOW, _AGGREGATE_WORKFLOW])
    def test_guard_passes_when_the_tree_matches(
        self, path: Path, tmp_path: Path
    ) -> None:
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
    env = {
        "PATH": os.environ["PATH"],
        "HOME": str(root),
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@t",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@t",
    }
    (root / "f").write_text("x", encoding="utf-8")
    for args in (["init", "-q"], ["add", "f"], ["commit", "-qm", "c"]):
        subprocess.run(
            ["git", *args],
            cwd=root,
            env=env,
            check=True,
            capture_output=True,
            timeout=30,
        )
    out = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=root,
        env=env,
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    return out.stdout.strip()


def _run_guard(script: str, cwd: Path, head_sha: str) -> int:
    return subprocess.run(
        ["bash", "-c", script],
        cwd=cwd,
        env={"PATH": os.environ["PATH"], "HEAD_SHA": head_sha},
        capture_output=True,
        timeout=30,
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

    def test_unset_takes_the_normal_path(self, monkeypatch: pytest.MonkeyPatch) -> None:
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
            # The all-absent round fails CI after posting (AT-2569).
            pytest.raises(SystemExit),
        ):
            main()
        mock_load.assert_called_once()
        assert mock_post.call_args[0][1] == "comment"

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
        self._set_env(monkeypatch, ["cancelled"] * 3, head_sha=self._REVIEWED)
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
        self._set_env(monkeypatch, ["success"] * 3, head_sha=self._REVIEWED)
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
            # The all-absent round fails CI after posting (AT-2569).
            pytest.raises(SystemExit),
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


_GITHUB_ACTIONS_SPELLINGS = (
    "github-actions[bot]",
    "github-actions",
    "app/github-actions",
)
_APPROVER_SPELLINGS = (
    "ignite-ai-review-approver[bot]",
    "ignite-ai-review-approver",
)


class TestNormalizeBotLogin:
    """One app, three surfaces, one comparison key.

    REST ``.user.login`` carries ``[bot]``, GraphQL ``author.login`` drops
    it, and ``gh pr view --json author`` prefixes ``app/``. Any pair of
    spellings of the same app must compare equal after normalization, and
    no spelling of one app may collide with another.
    """

    @pytest.mark.parametrize(
        ("left", "right"),
        [
            (a, b)
            for spellings in (_GITHUB_ACTIONS_SPELLINGS, _APPROVER_SPELLINGS)
            for a in spellings
            for b in spellings
        ],
    )
    def test_spellings_of_the_same_app_compare_equal(
        self, left: str, right: str
    ) -> None:
        assert normalize_bot_login(left) == normalize_bot_login(right)

    @pytest.mark.parametrize(
        ("left", "right"),
        [(a, b) for a in _GITHUB_ACTIONS_SPELLINGS for b in _APPROVER_SPELLINGS],
    )
    def test_different_apps_stay_different(self, left: str, right: str) -> None:
        assert normalize_bot_login(left) != normalize_bot_login(right)

    @pytest.mark.parametrize("login", ["hyuk-hur", "octocat", ""])
    def test_a_human_login_is_unchanged(self, login: str) -> None:
        assert normalize_bot_login(login) == login

    def test_case_is_preserved(self) -> None:
        """Every surface reports canonical casing; no folding, by design."""
        assert normalize_bot_login("Github-Actions[bot]") == "Github-Actions"


def _stale_pages(comments: list[dict[str, Any]], reviews: list[dict[str, Any]]) -> Any:
    """Stand in for ``fetch_paginated_nodes``, keyed on the query's field."""

    def fake(query: str, field: str, *args: Any, **kwargs: Any) -> list[dict[str, Any]]:
        return {"comments": comments, "reviews": reviews}[field]

    return fake


_STALE_MARKED = f"{REVIEW_MARKER}\nprior round"
_HEAD = "b" * 40
_OLD_HEAD = "a" * 40


def _minimize_stale(
    comments: list[dict[str, Any]],
    reviews: list[dict[str, Any]],
) -> list[tuple[str, str]]:
    """Run ``_fold_prior_round_items`` over the given pages, as of ``_HEAD``.

    Returns the ``(node_id, label)`` of every mutation it issued, in order.
    """
    from aggregate_reviews import _fold_prior_round_items

    issued: list[tuple[str, str]] = []

    def record(query: str, node_id: str, label: str) -> None:
        issued.append((node_id, label))

    with (
        patch(
            "aggregate_reviews.fetch_paginated_nodes",
            side_effect=_stale_pages(comments, reviews),
        ),
        patch("aggregate_reviews._run_gql_mutation", side_effect=record),
    ):
        _fold_prior_round_items("42", "owner/repo", head_sha=_HEAD)
    return issued


def _comment(
    comment_id: str, login: str | None, body: str = _STALE_MARKED
) -> dict[str, Any]:
    """A comments-page node.

    The query selects ``id``, ``isMinimized`` and ``body``, which is what
    the fold reads. ``author`` is carried anyway, so a case can name who
    posted an item and an author predicate added later has something to
    match -- which is how the control tests below detect one.
    """
    return {
        "id": comment_id,
        "author": None if login is None else {"login": login},
        "isMinimized": False,
        "body": body,
    }


def _review(
    review_id: str,
    login: str | None,
    state: str,
    body: str = _STALE_MARKED,
    commit: str | None = _OLD_HEAD,
) -> dict[str, Any]:
    """A reviews-page node, as ``_comment`` is for the other query.

    The query selects ``id``, ``state``, ``body`` and ``commit { oid }``;
    ``author`` is carried for the same reason. The review is of a
    superseded head unless a case says otherwise, so the fold cases above
    the same-head class read as they did before that distinction existed.
    """
    return {
        "id": review_id,
        "author": None if login is None else {"login": login},
        "state": state,
        "body": body,
        "commit": None if commit is None else {"oid": commit},
    }


class TestFoldPriorRoundItemsKeysOnMarker:
    """A prior-round item is one whose body carries ``REVIEW_MARKER``.

    The fold used to match the author's login against ``BOT_LOGIN`` as
    well, and that one login never covered every identity the pipeline
    posts under: the reviewer App's ``APPROVED`` reviews carry the App's
    login (AT-2599), and on the local driver the verdict is the operator's
    (AT-2208). The marker is this pipeline's own HTML comment, so it is the
    one thing every verdict shares and nothing else carries by accident.

    Each refusal case below shares a page with the accepted case and
    differs from it in exactly one predicate, so a refusal is attributable
    to that predicate alone -- and the test fails if that predicate is
    later dropped, because the paired node would then be folded too.
    """

    _OWN = _review("R_own", "github-actions", "APPROVED")
    _OWN_FOLDED = [("R_own", "dismiss"), ("R_own", "minimize")]

    def test_prior_round_comment_and_review_of_the_default_bot_are_folded(
        self,
    ) -> None:
        issued = _minimize_stale(
            comments=[_comment("C_1", "github-actions")],
            reviews=[_review("R_1", "github-actions", "CHANGES_REQUESTED")],
        )
        assert issued == [
            ("C_1", "minimize"),
            ("R_1", "dismiss"),
            ("R_1", "minimize"),
        ]

    def test_own_approved_marker_review_is_dismissed_and_minimized(
        self,
    ) -> None:
        """The defect of AT-2599: a prior head's APPROVED review of our own
        was never dismissed, so it kept satisfying a ruleset whose
        dismiss_stale_reviews_on_push is off on a head no review had seen."""
        issued = _minimize_stale(comments=[], reviews=[self._OWN])
        assert issued == self._OWN_FOLDED

    def test_reviewer_app_approved_marker_review_is_dismissed_and_minimized(
        self,
    ) -> None:
        """The approval is posted with the App's token, under the App's
        login; the fold does not need to know that login."""
        issued = _minimize_stale(
            comments=[],
            reviews=[_review("R_app", "ignite-ai-review-approver", "APPROVED")],
        )
        assert issued == [("R_app", "dismiss"), ("R_app", "minimize")]

    def test_a_marker_review_by_any_other_author_is_dismissed_too(
        self,
    ) -> None:
        """By design. Authorship is not read: the local driver's verdict is
        posted by a human (the operator), another bot quoting the marker is
        housekeeping, and an author filter would spare one path's prior
        round while folding the other's. This is the control for the
        anchor below -- it fails if an author or type check is added."""
        issued = _minimize_stale(
            comments=[_comment("C_human", "hyuk-hur")],
            reviews=[
                _review("R_human", "hyuk-hur", "APPROVED"),
                _review("R_other_bot", "gemini-code-assist", "APPROVED"),
            ],
        )
        assert issued == [
            ("C_human", "minimize"),
            ("R_human", "dismiss"),
            ("R_human", "minimize"),
            ("R_other_bot", "dismiss"),
            ("R_other_bot", "minimize"),
        ]

    def test_items_without_the_marker_are_left_alone(self) -> None:
        """The anchor: with no author read, the marker is the whole of
        what separates a prior-round item from everything else on the PR."""
        issued = _minimize_stale(
            comments=[
                _comment("C_own", "github-actions"),
                _comment("C_unmarked", "github-actions", body="prior round"),
            ],
            reviews=[
                self._OWN,
                _review("R_unmarked", "github-actions", "APPROVED", body="LGTM"),
                _review("R_human_plain", "hyuk-hur", "APPROVED", body="LGTM"),
            ],
        )
        assert issued == [("C_own", "minimize"), *self._OWN_FOLDED]

    def test_an_already_minimized_comment_is_not_minimized_again(self) -> None:
        minimized = _comment("C_done", "github-actions")
        minimized["isMinimized"] = True
        issued = _minimize_stale(
            comments=[minimized, _comment("C_own", "github-actions")], reviews=[]
        )
        assert issued == [("C_own", "minimize")]

    def test_a_review_that_quotes_a_verdict_is_left_alone(self) -> None:
        """GitHub's "Quote reply" copies the quoted body's raw markdown,
        HTML comments included, so a human review that quotes a verdict and
        approves carries the marker -- behind "> ". The marker's position
        is what tells the two apart: every verdict this pipeline posts
        opens with it. Paired with the folded node, so dropping the
        position test fails here and dropping the marker test fails the
        anchor below."""
        quoted = f"> {_STALE_MARKED}\n\nAgreed, approving."
        issued = _minimize_stale(
            comments=[_comment("C_quote", "hyuk-hur", body=quoted)],
            reviews=[self._OWN, _review("R_quote", "hyuk-hur", "APPROVED", quoted)],
        )
        assert issued == self._OWN_FOLDED

    def test_blank_lines_before_the_marker_still_fold(self) -> None:
        """Blank lines ahead of the marker render nothing and change no
        body's authorship, and are what a hand-assembled body picks up."""
        issued = _minimize_stale(
            comments=[],
            reviews=[_review("R_pad", None, "APPROVED", f"\r\n\n{_STALE_MARKED}")],
        )
        assert issued == [("R_pad", "dismiss"), ("R_pad", "minimize")]

    def test_an_indented_code_block_marker_is_left_alone(self) -> None:
        """Four leading spaces make an indented code block, so the marker
        renders as literal text: that is someone documenting the marker,
        not a verdict. Every producer emits it at column 0. Paired with the
        folded node, as the quote case above is."""
        indented = f"    {_STALE_MARKED}\n\nThe marker every verdict opens with."
        issued = _minimize_stale(
            comments=[_comment("C_doc", "hyuk-hur", body=indented)],
            reviews=[self._OWN, _review("R_doc", "hyuk-hur", "APPROVED", indented)],
        )
        assert issued == self._OWN_FOLDED

    @pytest.mark.parametrize("state", ["COMMENTED", "PENDING", "DISMISSED"])
    def test_a_state_the_dismiss_mutation_refuses_is_not_dismissed(
        self, state: str
    ) -> None:
        """``dismissPullRequestReview`` accepts approved or rejected reviews
        only; issuing it for anything else would log a warning every round
        for as long as the review exists."""
        issued = _minimize_stale(
            comments=[],
            reviews=[self._OWN, _review("R_other", "github-actions", state)],
        )
        assert issued == self._OWN_FOLDED

    def test_an_approval_of_the_current_head_is_returned_not_dismissed(
        self,
    ) -> None:
        """The one item the fold leaves standing; ``post_verdict`` folds it
        through ``_dismiss_replaced_approvals`` once a review of its own is
        on the PR. A CHANGES_REQUESTED of the same head goes up front, and
        an APPROVED of a superseded head as before."""
        from aggregate_reviews import _fold_prior_round_items

        issued: list[tuple[str, str]] = []
        pages = _stale_pages(
            [],
            [
                _review("R_same", None, "APPROVED", commit=_HEAD),
                _review("R_same_block", None, "CHANGES_REQUESTED", commit=_HEAD),
                self._OWN,
            ],
        )
        with (
            patch("aggregate_reviews.fetch_paginated_nodes", side_effect=pages),
            patch(
                "aggregate_reviews._run_gql_mutation",
                side_effect=lambda q, i, label: issued.append((i, label)),
            ),
        ):
            standing = _fold_prior_round_items("42", "owner/repo", head_sha=_HEAD)
        assert standing == ["R_same"]
        assert issued == [
            ("R_same_block", "dismiss"),
            ("R_same_block", "minimize"),
            *self._OWN_FOLDED,
        ]

    def test_dismiss_replaced_approvals_folds_the_ids_it_is_given(self) -> None:
        from aggregate_reviews import _dismiss_replaced_approvals

        issued: list[tuple[str, str]] = []
        with patch(
            "aggregate_reviews._run_gql_mutation",
            side_effect=lambda q, i, label: issued.append((i, label)),
        ):
            _dismiss_replaced_approvals(["R_a", "R_b"])
        assert issued == [
            ("R_a", "dismiss"),
            ("R_a", "minimize"),
            ("R_b", "dismiss"),
            ("R_b", "minimize"),
        ]

    def test_the_reviews_query_requests_the_commit(self) -> None:
        """Pinned on the shape for the reason the author test below gives:
        a head comparison against a field the server never sent would
        treat every approval as superseded and fold it up front."""
        assert "commit { oid }" in _STALE_REVIEWS_QUERY

    @pytest.mark.parametrize(
        "query",
        [_STALE_COMMENTS_QUERY, _STALE_REVIEWS_QUERY],
        ids=["comments", "reviews"],
    )
    def test_the_stale_queries_do_not_request_the_author(self, query: str) -> None:
        """The fold reads no author, so neither query asks for one.

        Asserted on the query rather than on behaviour because a predicate
        cannot be caught reading a field the server never sent: an author
        check added back here would be dead against production and alive
        only against fixtures that still carry the key. The cases above
        pin the fold's behaviour; this pins the shape it sees.
        """
        assert "author" not in query


_NOT_LOOKED_UP = object()


def _post_verdict_over(
    monkeypatch: pytest.MonkeyPatch,
    reviews: list[dict[str, Any]],
    verdict: str,
    *,
    head: str = _HEAD,
    comment_only: bool = False,
    approve_quorum: bool = True,
    unreviewed_majors: int = 0,
    review_rc: int = 0,
    live_head: str | None | object = _NOT_LOOKED_UP,
) -> tuple[list[list[str]], list[tuple[str, str]], int]:
    """Run ``post_verdict`` with the stale fold live over ``reviews``.

    Returns the gh commands run and the ``(node_id, label)`` of every
    mutation issued -- both in order, so a dismissal can be placed before
    or after the review that replaces it -- and how many pages were
    fetched. ``review_rc`` is what ``gh pr review`` exits with; every
    other gh call succeeds.

    ``live_head`` is what ``_current_pr_head`` answers: a SHA, or None for
    a lookup that failed. Left at its default, the lookup fails the test
    if it is made at all -- a run that has ``HEAD_SHA`` has nothing to
    look up, so every case that passes a ``head`` also pins that no
    ``gh api`` call is spent on one.
    """
    monkeypatch.setenv("PR_NUMBER", "42")
    monkeypatch.setenv("GITHUB_REPOSITORY", "owner/repo")
    monkeypatch.setenv("HEAD_SHA", head)

    commands: list[list[str]] = []
    issued: list[tuple[str, str]] = []
    fetches = 0

    def fake_run(cmd: list[str], *args: Any, **kwargs: Any) -> Any:
        commands.append(cmd)
        rc = review_rc if "review" in cmd else 0
        return type("R", (), {"returncode": rc, "stderr": "", "stdout": ""})()

    def record(query: str, node_id: str, label: str) -> None:
        issued.append((node_id, label))

    def lookup(pr_number: str, repo: str) -> str | None:
        assert live_head is not _NOT_LOOKED_UP, "live head looked up with HEAD_SHA set"
        return live_head  # type: ignore[return-value]

    pages = _stale_pages([], reviews)

    def counted(*args: Any, **kwargs: Any) -> list[dict[str, Any]]:
        nonlocal fetches
        fetches += 1
        return pages(*args, **kwargs)

    with (
        patch("aggregate_reviews.fetch_paginated_nodes", side_effect=counted),
        patch("aggregate_reviews._run_gql_mutation", side_effect=record),
        patch("aggregate_reviews._current_pr_head", side_effect=lookup),
        patch("aggregate_reviews.subprocess.run", side_effect=fake_run),
    ):
        post_verdict(
            "body",
            verdict,
            comment_only=comment_only,
            approve_quorum=approve_quorum,
            unreviewed_majors=unreviewed_majors,
        )
    return commands, issued, fetches


class TestSameHeadApprovalStandsUntilReplaced:
    """An APPROVED review of the current head survives a round that
    found nothing wrong with it, and only such a round.

    The fold runs at the top of ``post_verdict``, before the round knows
    whether it will post a review at all, and four branches post none.
    Three are transient -- the approve-quorum withhold, the
    ``comment_only`` downgrade, and the comment fallback when ``gh pr
    review`` fails -- and dismissing a same-head approval ahead of those
    turns a PR that was green on this head into one blocked by a
    transient failure of the re-run, with nothing on the PR to say why.
    The fourth, AT-2823's undispositioned-majors withhold, is a finding
    about this head rather than a flake, so it folds the approval itself
    once its comment is up; left standing, the withhold would do nothing
    on the same-head re-run its own note asks the reader to make.
    An approval of a superseded head is different: no
    review has seen the current head, so it goes up front whatever the
    round then does (AT-2599's invariant), and so does a CHANGES_REQUESTED
    of either head, whose blocking force the required check's exit code
    carries anyway.
    """

    _SAME = _review("R_same", None, "APPROVED", commit=_HEAD)
    _OLD = _review("R_old", None, "APPROVED", commit=_OLD_HEAD)
    _OLD_FOLDED = [("R_old", "dismiss"), ("R_old", "minimize")]
    _SAME_FOLDED = [("R_same", "dismiss"), ("R_same", "minimize")]

    @pytest.mark.parametrize(
        "kwargs",
        [
            {"approve_quorum": False},
            {"comment_only": True},
            {"review_rc": 1},
        ],
        ids=["quorum-withheld", "comment-only", "review-api-failed"],
    )
    def test_a_round_that_posts_no_review_leaves_the_same_head_approval(
        self, monkeypatch: pytest.MonkeyPatch, kwargs: dict[str, Any]
    ) -> None:
        commands, issued, _ = _post_verdict_over(
            monkeypatch, [self._SAME, self._OLD], "approve", **kwargs
        )
        assert any("comment" in cmd for cmd in commands)
        assert issued == self._OLD_FOLDED

    def test_the_majors_withhold_folds_the_same_head_approval(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """AT-2823's withhold is a finding, not a flake, so it folds.

        The carve-out above exists because re-blocking a PR that was green
        on this head would be a regression *caused by a transient failure
        of the re-run* -- a reviewer that did not answer, the killswitch,
        an API error. An undispositioned major is none of those: it is a
        substantive finding about this very head. Leaving the approval
        standing would make the withhold do nothing at all on a same-head
        re-run, which is exactly the action the withhold note tells the
        reader to take, so the PR would stay green while the comment said
        the approval was withheld.
        """
        commands, issued, _ = _post_verdict_over(
            monkeypatch, [self._SAME, self._OLD], "approve", unreviewed_majors=2
        )
        assert any("comment" in cmd for cmd in commands)
        assert not any("review" in cmd for cmd in commands)
        assert issued == [*self._OLD_FOLDED, *self._SAME_FOLDED]

    def test_a_quorum_short_round_with_majors_still_folds(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Both withholds at once is still a finding about this head.

        Coverage is named first in the prose -- the missing reviewer may
        yet raise more -- but which reason gets *said* is a different
        question from whether this head is vouched for. A major was found
        on it either way, so the standing approval goes. Treating the
        combination as transient would reopen AT-2823 behind a second,
        unrelated fault: head X approved cleanly, then a same-head re-run
        where one reviewer dies and another raises a major leaves the PR
        green while the comment says the approval was withheld.
        """
        _, issued, _ = _post_verdict_over(
            monkeypatch,
            [self._SAME, self._OLD],
            "approve",
            approve_quorum=False,
            unreviewed_majors=2,
        )
        assert issued == [*self._OLD_FOLDED, *self._SAME_FOLDED]

    def test_the_majors_fold_happens_with_the_killswitch_off_too(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """comment_only suppresses the second explanation, not the fold.

        With ALLOW_AUTO_APPROVE off the headline already says why nothing
        was posted as a review, so the withhold note would be a second
        reason for one downgrade -- that suppression is right. What is
        not right is skipping the dismissal with it: a clean round can
        have approved head X while the killswitch was on, and a same-head
        re-run that raises a major while it is off would leave that
        APPROVED standing over an undispositioned major, which is the
        outcome this whole change exists to close.
        """
        _, issued, _ = _post_verdict_over(
            monkeypatch,
            [self._SAME, self._OLD],
            "approve",
            comment_only=True,
            unreviewed_majors=2,
        )
        assert issued == [*self._OLD_FOLDED, *self._SAME_FOLDED]

    def test_the_majors_withhold_folds_against_the_live_head_too(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """With no HEAD_SHA, the dismissal is not a no-op -- checked, not assumed.

        The fold leaves a same-head approval standing on this path as well,
        comparing against the head GitHub reports now, so ``standing`` is
        non-empty here and the majors withhold really does dismiss. That is
        the intended reading rather than an inherited one: the live head is
        established as this round's by the lookup -- _head_is_stale let the
        run post because nothing showed it superseded -- so an approval of
        it is an approval of the head the major was found on.
        """
        _, issued, _ = _post_verdict_over(
            monkeypatch,
            [self._SAME, self._OLD],
            "approve",
            head="",
            live_head=_HEAD,
            unreviewed_majors=2,
        )
        assert issued == [*self._OLD_FOLDED, *self._SAME_FOLDED]

    def test_with_no_head_at_all_the_majors_withhold_adds_nothing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The other half of the same question: when the lookup fails too,
        # the fold already sent every approval up front, so `standing` is
        # empty and the new dismissal is genuinely a no-op -- no approval
        # is folded twice.
        _, issued, _ = _post_verdict_over(
            monkeypatch,
            [self._SAME],
            "approve",
            head="",
            live_head=None,
            unreviewed_majors=2,
        )
        assert issued == self._SAME_FOLDED

    @pytest.mark.parametrize(
        "verdict, flag",
        [("approve", "--approve"), ("request_changes", "--request-changes")],
    )
    def test_a_posted_review_replaces_the_same_head_approval(
        self, monkeypatch: pytest.MonkeyPatch, verdict: str, flag: str
    ) -> None:
        """Dismissed after the review lands, not before: the order is what
        keeps the PR green across the gap, and a re-run that reaches a
        different verdict on the same head still retires the old one."""
        commands, issued, fetches = _post_verdict_over(
            monkeypatch, [self._SAME, self._OLD], verdict
        )
        assert issued == [*self._OLD_FOLDED, *self._SAME_FOLDED]
        assert any("review" in cmd and flag in cmd for cmd in commands)
        # The pages are read once, before the review is posted; a re-read
        # afterwards would find this round's own review and dismiss it.
        assert fetches == 2

    def test_a_comment_verdict_leaves_the_same_head_approval(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _, issued, _ = _post_verdict_over(
            monkeypatch, [self._SAME, self._OLD], "comment"
        )
        assert issued == self._OLD_FOLDED

    def test_a_same_head_changes_requested_goes_up_front(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Unchanged: the required check is red for as long as the verdict
        is, so the review's loss blocks nothing on its own."""
        blocking = _review("R_block", None, "CHANGES_REQUESTED", commit=_HEAD)
        _, issued, _ = _post_verdict_over(
            monkeypatch, [blocking], "approve", approve_quorum=False
        )
        assert issued == [("R_block", "dismiss"), ("R_block", "minimize")]

    @pytest.mark.parametrize("head", ["", "   "])
    def test_with_no_head_sha_the_live_head_stands_in(
        self, monkeypatch: pytest.MonkeyPatch, head: str, capsys: pytest.CaptureFixture
    ) -> None:
        """No HEAD_SHA means prepare never resolved one (AT-2087, AT-1975).
        Both of those paths post a verdict and, with the killswitch off,
        no review -- so folding a same-head approval up front there is the
        green-to-blocked outcome the carve-out exists to prevent. The head
        GitHub reports now is compared instead, and the run log says so."""
        _, issued, _ = _post_verdict_over(
            monkeypatch,
            [self._SAME, self._OLD],
            "approve",
            head=head,
            approve_quorum=False,
            live_head=_HEAD,
        )
        assert issued == self._OLD_FOLDED
        assert (
            f"::notice::HEAD_SHA unset; prior-round approvals compared against {_HEAD}"
        ) in capsys.readouterr().err

    def test_with_no_head_at_all_every_approval_goes_up_front(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        """No HEAD_SHA and a failed head lookup: nothing can vouch that a
        standing approval is of this head, so it is folded as a superseded
        one would be -- the direction that is visible and self-healing
        rather than silent -- and the log names the degraded comparison."""
        _, issued, _ = _post_verdict_over(
            monkeypatch,
            [self._SAME],
            "approve",
            head="",
            approve_quorum=False,
            live_head=None,
        )
        assert issued == self._SAME_FOLDED
        assert "::notice::HEAD_SHA unset and the PR head lookup failed" in (
            capsys.readouterr().err
        )

    def test_a_review_with_no_commit_goes_up_front(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``PullRequestReview.commit`` is nullable; a review that names no
        commit cannot be vouched for as of this head either."""
        orphan = _review("R_orphan", None, "APPROVED", commit=None)
        _, issued, _ = _post_verdict_over(
            monkeypatch, [orphan], "approve", approve_quorum=False
        )
        assert issued == [("R_orphan", "dismiss"), ("R_orphan", "minimize")]
