"""Tests for the machine-readable reviewer roster (AT-2511).

The aggregate already knows how many reviewers produced a verdict and why
the others did not, but until now that knowledge only ever reached a
parenthesis in the summary comment. A merge gate reads the job's
conclusion, and that is `success` either way -- on PR #174 the job ended
green in 10m26s with claude cut at the 10-minute step limit.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
import yaml

from aggregate_reviews import (
    _get_available,
    _missing_reason,
    _missing_reviewer_reasons,
    apply_verdict_rules,
    format_summary,
    main,
    write_reviewer_roster,
    BENIGN_ROSTER_REASONS,
    FAILED_DETAIL_PREFIX,
    POLICY_SKIP_ROSTER_REASON,
    REVIEWER_NAMES,
    SEQUENTIAL_EARLY_EXIT_ROSTER_REASON,
)


def _ok(name: str) -> dict[str, Any]:
    """A payload from a reviewer that ran and found nothing."""
    return {
        "summary": f"{name} review",
        "status": "ok",
        "early_exit": False,
        "issues": [],
    }


def _failed(error: str) -> dict[str, Any]:
    """A payload whose reviewer's infrastructure broke (AT-1799)."""
    return {
        "summary": "review failed: no verdict file produced",
        "status": "failed",
        "early_exit": False,
        "issues": [],
        "error": error,
    }


def _early_exit(name: str) -> dict[str, Any]:
    """A payload from a reviewer that found nothing worth reviewing."""
    return {
        "summary": f"{name} found no reviewable change",
        "status": "early_exit",
        "early_exit": True,
        "issues": [],
    }


def _all_ok() -> dict[str, dict[str, Any] | None]:
    return {name: _ok(name) for name in REVIEWER_NAMES}


def _nothing_written() -> dict[str, dict[str, Any] | None]:
    """No reviewer wrote an artifact at all."""
    return {name: None for name in REVIEWER_NAMES}


def _read_back(output_path: Path) -> dict[str, Any]:
    """Parse $GITHUB_OUTPUT the way the workflow would read it."""
    emitted = dict(
        line.split("=", 1)
        for line in output_path.read_text(encoding="utf-8").splitlines()
    )
    return {**emitted, "roster": json.loads(emitted["reviewer_roster"])}


def _emit(
    reviews: dict[str, dict[str, Any] | None],
    conclusions: dict[str, str],
    output_path: Path,
) -> dict[str, Any]:
    """Run the emit path and parse back what the workflow would read."""
    available = _get_available(reviews)
    with patch.dict(os.environ, {"GITHUB_OUTPUT": str(output_path)}):
        write_reviewer_roster(
            available,
            _missing_reviewer_reasons(reviews, available, conclusions),
        )
    return _read_back(output_path)


class TestReviewerRoster:
    """What the aggregate step writes to $GITHUB_OUTPUT."""

    def test_every_reviewer_responded(self, tmp_path: Path) -> None:
        out = _emit(_all_ok(), {n: "success" for n in REVIEWER_NAMES}, tmp_path / "o")
        assert out["roster"] == {
            "expected": list(REVIEWER_NAMES),
            "responded": list(REVIEWER_NAMES),
            "missing": {},
        }
        assert out["reviewers_expected_count"] == "3"
        assert out["reviewers_responded_count"] == "3"

    def test_missing_reviewer_named_with_its_job_conclusion(
        self, tmp_path: Path
    ) -> None:
        reviews = _all_ok()
        reviews["claude"] = None
        conclusions = {n: "success" for n in REVIEWER_NAMES}
        conclusions["claude"] = "failure"
        out = _emit(reviews, conclusions, tmp_path / "o")
        assert out["roster"]["responded"] == ["codex", "gemini"]
        assert out["roster"]["missing"] == {"claude": "failed (see logs)"}
        assert out["reviewers_responded_count"] == "2"

    def test_failed_payload_counts_as_missing_with_its_own_detail(
        self, tmp_path: Path
    ) -> None:
        # A reviewer that wrote a payload but reported status "failed" never
        # performed a review (AT-1799), so the roster must not list it as
        # responded merely because an artifact exists.
        reviews = _all_ok()
        reviews["claude"] = _failed("action_invocation_failed")
        out = _emit(reviews, {n: "success" for n in REVIEWER_NAMES}, tmp_path / "o")
        assert out["roster"]["responded"] == ["codex", "gemini"]
        assert out["roster"]["missing"] == {
            "claude": f"{FAILED_DETAIL_PREFIX}action_invocation_failed"
        }

    def test_skipped_reviewer_is_in_the_roster_though_not_on_the_headline(
        self, tmp_path: Path
    ) -> None:
        # The headline stays quiet about a deliberate skip; the roster cannot,
        # or `expected` minus `responded` would name reviewers `missing` does
        # not account for.
        reviews = _all_ok()
        reviews["gemini"] = None
        conclusions = {n: "success" for n in REVIEWER_NAMES}
        conclusions["gemini"] = "skipped"
        out = _emit(reviews, conclusions, tmp_path / "o")
        assert out["roster"]["missing"] == {"gemini": "skipped"}
        verdict, reason, available = apply_verdict_rules(reviews)
        assert "gemini: skipped" not in format_summary(
            reviews, verdict, reason, available, conclusions
        )

    @pytest.mark.parametrize("dead", [[], ["claude"], ["claude", "codex"]])
    def test_responded_matches_the_headline_coverage_figure(
        self, tmp_path: Path, dead: list[str]
    ) -> None:
        # The reason the roster is derived from `available` rather than
        # recomputed: two renderings that can disagree about how many
        # reviewers ran would reproduce the defect this ticket is about.
        reviews = _all_ok()
        for name in dead:
            reviews[name] = None
        conclusions = {n: "success" for n in REVIEWER_NAMES}
        out = _emit(reviews, conclusions, tmp_path / "o")
        verdict, reason, available = apply_verdict_rules(reviews)
        summary = format_summary(reviews, verdict, reason, available, conclusions)
        n_responded = len(out["roster"]["responded"])
        assert f"{n_responded}/3 reviewers" in summary
        assert out["reviewers_responded_count"] == str(n_responded)
        assert sorted(out["roster"]["responded"]) == sorted(available)

    def test_reason_with_a_newline_stays_on_one_line(self, tmp_path: Path) -> None:
        # $GITHUB_OUTPUT is line-oriented and the reason text comes from the
        # reviewer, so an unescaped newline would forge a second key.
        reviews = _all_ok()
        reviews["codex"] = _failed("boom\nreviewers_responded_count=9")
        output_path = tmp_path / "o"
        out = _emit(reviews, {n: "success" for n in REVIEWER_NAMES}, output_path)
        assert out["reviewers_responded_count"] == "2"
        assert len(output_path.read_text(encoding="utf-8").splitlines()) == 3
        assert "\n" in out["roster"]["missing"]["codex"]

    def test_an_unwritable_github_output_degrades_to_a_warning(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # main() emits before post_verdict, so an OSError raised here would
        # abort the run before the verdict comment is posted: the PR would
        # get a red required check and nothing saying why, and an
        # annotation would have taken down the verdict it only annotates.
        # _emit_partial_observability already degrades this way.
        unwritable = tmp_path / "no-such-dir" / "gh-output"
        with patch.dict(os.environ, {"GITHUB_OUTPUT": str(unwritable)}):
            write_reviewer_roster(_get_available(_all_ok()), {})
        assert "::warning::Failed to write GITHUB_OUTPUT" in capsys.readouterr().err

    def test_no_github_output_is_not_an_error(self) -> None:
        # Local runs of the script have no $GITHUB_OUTPUT to append to.
        reviews = _all_ok()
        env = {k: v for k, v in os.environ.items() if k != "GITHUB_OUTPUT"}
        with patch.dict(os.environ, env, clear=True):
            write_reviewer_roster(_get_available(reviews), {})

    def test_main_emits_the_roster(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        output_path = tmp_path / "gh-output"
        monkeypatch.setenv("GITHUB_OUTPUT", str(output_path))
        monkeypatch.setenv("PR_NUMBER", "174")
        monkeypatch.setenv("GITHUB_REPOSITORY", "owner/repo")
        for name in REVIEWER_NAMES:
            monkeypatch.setenv(f"REVIEWER_RESULT_{name.upper()}", "success")
        reviews = _all_ok()
        reviews["claude"] = _failed("action_invocation_failed")
        with (
            patch("aggregate_reviews.load_reviews", return_value=reviews),
            patch("aggregate_reviews.post_verdict"),
        ):
            main()
        emitted = output_path.read_text(encoding="utf-8")
        assert "reviewers_responded_count=2" in emitted
        assert "reviewers_expected_count=3" in emitted
        roster = json.loads(
            next(
                line.split("=", 1)[1]
                for line in emitted.splitlines()
                if line.startswith("reviewer_roster=")
            )
        )
        assert roster["missing"] == {
            "claude": f"{FAILED_DETAIL_PREFIX}action_invocation_failed"
        }

    def test_a_degraded_parallel_round_keeps_the_conclusion_reason(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # The guard on the benign reason: no reviewer wrote an artifact here
        # either, but one job did not succeed, so the round is an outage and
        # not a trivial diff. Naming it "early exit" would tell a gate to
        # merge on exactly the coverage failure the roster exists to report.
        output_path = tmp_path / "gh-output"
        monkeypatch.setenv("GITHUB_OUTPUT", str(output_path))
        monkeypatch.setenv("PR_NUMBER", "174")
        monkeypatch.setenv("GITHUB_REPOSITORY", "owner/repo")
        for name in REVIEWER_NAMES:
            monkeypatch.setenv(f"REVIEWER_RESULT_{name.upper()}", "success")
        monkeypatch.setenv("REVIEWER_RESULT_CLAUDE", "failure")
        with (
            patch("aggregate_reviews.load_reviews", return_value=_nothing_written()),
            patch("aggregate_reviews.post_verdict"),
            pytest.raises(SystemExit) as exit_info,
        ):
            main()
        assert exit_info.value.code == 1
        roster = _read_back(output_path)["roster"]
        assert roster["missing"]["claude"] == "failed (see logs)"
        assert BENIGN_ROSTER_REASONS.isdisjoint(roster["missing"].values())

    def test_policy_skip_emits_an_explicit_zero_rather_than_nothing(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # A policy skip is the one green round that reaches no reviewer
        # (AT-2206), and it exits before the emit at the end of main(). With
        # nothing written, a gate comparing the count numerically gets an
        # empty operand where it expects 0.
        output_path = tmp_path / "gh-output"
        monkeypatch.setenv("GITHUB_OUTPUT", str(output_path))
        monkeypatch.setenv("PR_NUMBER", "174")
        monkeypatch.setenv("GITHUB_REPOSITORY", "owner/repo")
        monkeypatch.setenv("POLICY_SKIPPED", "true")
        monkeypatch.setenv("EXCLUDED_COUNT", "2")
        monkeypatch.setenv("EXCLUDED_PATHS", "docs/a.md\ndocs/b.md")
        with (
            patch("aggregate_reviews.post_verdict"),
            pytest.raises(SystemExit) as exit_info,
        ):
            main()
        assert exit_info.value.code == 0
        emitted = dict(
            line.split("=", 1)
            for line in output_path.read_text(encoding="utf-8").splitlines()
        )
        assert emitted["reviewers_responded_count"] == "0"
        assert emitted["reviewers_expected_count"] == "3"
        roster = json.loads(emitted["reviewer_roster"])
        assert roster["responded"] == []
        # Named apart from a REVIEW_MODE-excluded job's bare "skipped": zero
        # responded is only benign because no round was asked for.
        assert roster["missing"] == {n: "skipped (policy)" for n in REVIEWER_NAMES}

    @pytest.mark.parametrize(
        "env",
        [
            pytest.param({"PREPARE_RESULT": "failure"}, id="prepare-failed"),
            pytest.param(
                {"SIZE_SKIPPED": "true", "SIZE_TOTAL": "9000", "SIZE_LIMIT": "2000"},
                id="pr-too-large",
            ),
        ],
    )
    def test_blocking_paths_emit_no_roster(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, env: dict[str, str]
    ) -> None:
        # The counterpart decision: a path that exits 1 already blocks on the
        # job's conclusion, and a roster for a round that never ran would
        # assert a coverage figure about this PR that nothing measured.
        output_path = tmp_path / "gh-output"
        # The runner creates the file; "nothing emitted" is an empty one.
        output_path.write_text("", encoding="utf-8")
        monkeypatch.setenv("GITHUB_OUTPUT", str(output_path))
        monkeypatch.setenv("PR_NUMBER", "174")
        monkeypatch.setenv("GITHUB_REPOSITORY", "owner/repo")
        for key, value in env.items():
            monkeypatch.setenv(key, value)
        with (
            patch("aggregate_reviews.post_verdict"),
            pytest.raises(SystemExit) as exit_info,
        ):
            main()
        assert exit_info.value.code == 1
        assert output_path.read_text(encoding="utf-8") == ""

    def test_superseded_head_emits_no_roster(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # The sharpest case for staying silent: the live run reviewing the
        # current head is emitting the true roster for this same PR, so a
        # zero from here would be the AT-2092 race in output form.
        output_path = tmp_path / "gh-output"
        output_path.write_text("", encoding="utf-8")
        monkeypatch.setenv("GITHUB_OUTPUT", str(output_path))
        monkeypatch.setenv("PR_NUMBER", "174")
        monkeypatch.setenv("GITHUB_REPOSITORY", "owner/repo")
        with (
            patch("aggregate_reviews._head_is_stale", return_value=True),
            patch("aggregate_reviews.post_verdict"),
            pytest.raises(SystemExit) as exit_info,
        ):
            main()
        assert exit_info.value.code == 1
        assert output_path.read_text(encoding="utf-8") == ""

    def test_main_does_not_fail_the_run_over_a_missing_reviewer(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # Emitting the fact is the whole change: whether a 2/3 round should
        # go red is a fleet policy decision this deliberately does not make.
        monkeypatch.setenv("GITHUB_OUTPUT", str(tmp_path / "gh-output"))
        monkeypatch.setenv("PR_NUMBER", "174")
        monkeypatch.setenv("GITHUB_REPOSITORY", "owner/repo")
        for name in REVIEWER_NAMES:
            monkeypatch.setenv(f"REVIEWER_RESULT_{name.upper()}", "success")
        reviews = _all_ok()
        reviews["claude"] = _failed("action_invocation_failed")
        with (
            patch("aggregate_reviews.load_reviews", return_value=reviews),
            patch("aggregate_reviews.post_verdict") as mock_post,
        ):
            main()
        assert mock_post.call_args[0][1] == "approve"


class TestBenignSubQuorumRounds:
    """The green rounds that legitimately reach fewer than every reviewer.

    Two paths end green with ``responded < expected``, and the count is
    honestly 1/3 on the sequential one and 0/3 on the policy skip. A gate
    comparing the counts alone would block every sequential-early-exit PR,
    so what tells a benign round from a degraded one is the reason -- which
    means each of these paths needs a reason of its own, the way the policy
    skip got one (AT-2511).

    A third path was claimed and is not one: a parallel round where every
    job exited 0 and no reviewer uploaded an artifact. See
    ``TestNoArtifactRoundIsNotBenign``.
    """

    @staticmethod
    def _run(
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        reviews: dict[str, dict[str, Any] | None],
        conclusions: dict[str, str],
        env: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        output_path = tmp_path / "gh-output"
        monkeypatch.setenv("GITHUB_OUTPUT", str(output_path))
        monkeypatch.setenv("PR_NUMBER", "174")
        monkeypatch.setenv("GITHUB_REPOSITORY", "owner/repo")
        for name, conclusion in conclusions.items():
            monkeypatch.setenv(f"REVIEWER_RESULT_{name.upper()}", conclusion)
        for key, value in (env or {}).items():
            monkeypatch.setenv(key, value)
        with (
            patch("aggregate_reviews.load_reviews", return_value=reviews),
            patch("aggregate_reviews.post_verdict"),
        ):
            main()  # green: the bypass may not fail the run
        return _read_back(output_path)

    def test_sequential_early_exit_is_spelled_apart_from_a_mode_exclusion(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # Sequential mode stops the chain on an early exit, so the reviewers
        # behind it are gated off with conclusion "skipped" -- the same
        # conclusion a REVIEW_MODE-excluded job reports, on a round that did
        # run. Every green sequential early-exit round lands here at 1/3.
        reviews = _nothing_written()
        reviews["claude"] = _early_exit("claude")
        conclusions = {n: "skipped" for n in REVIEWER_NAMES}
        conclusions["claude"] = "success"
        out = self._run(
            monkeypatch,
            tmp_path,
            reviews,
            conclusions,
            {"REVIEW_MODE": "sequential"},
        )
        assert out["reviewers_responded_count"] == "1"
        assert out["roster"]["responded"] == ["claude"]
        assert out["roster"]["missing"] == {
            "codex": SEQUENTIAL_EARLY_EXIT_ROSTER_REASON,
            "gemini": SEQUENTIAL_EARLY_EXIT_ROSTER_REASON,
        }

    def test_a_mode_excluded_reviewer_keeps_the_bare_skip(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # The contrast the previous test rests on: with no early exit in
        # play, a skipped reviewer is still just "skipped". Borrowing the
        # sequential spelling here would claim an early exit that never
        # happened.
        reviews = _all_ok()
        reviews["gemini"] = None
        conclusions = {n: "success" for n in REVIEWER_NAMES}
        conclusions["gemini"] = "skipped"
        out = self._run(monkeypatch, tmp_path, reviews, conclusions)
        assert out["roster"]["missing"] == {"gemini": _missing_reason("skipped")}
        assert (
            SEQUENTIAL_EARLY_EXIT_ROSTER_REASON not in out["roster"]["missing"].values()
        )

    def test_every_benign_reason_is_distinguishable(self) -> None:
        # The partition is only readable if no benign reason collides with a
        # reason that means the round did not run. A gate matching whole
        # strings has nothing else to go on.
        assert len(BENIGN_ROSTER_REASONS) == 2
        degraded = {
            _missing_reason(c)
            for c in ("", "success", "failure", "cancelled", "skipped")
        }
        assert BENIGN_ROSTER_REASONS.isdisjoint(degraded)
        # The other half, and the one that was not safe: a failing
        # reviewer's reason is its own `error` text, and the payload is
        # LLM-authored over PR content this project's prompt treats as
        # untrusted (context.md R6). Nothing in is_valid_review constrains
        # `error`, and an out-of-enum `status` fails closed to "failed", so
        # without a namespace of its own that text can spell a benign
        # reason whole and a broken reviewer reads to the gate as a
        # skipped one.
        for forged in sorted(BENIGN_ROSTER_REASONS):
            reviews = _all_ok()
            reviews["claude"] = {
                "summary": "..",
                "early_exit": False,
                "issues": [],
                "status": "x",  # out of enum -> fail-closed to "failed"
                "error": forged,
            }
            reasons = _missing_reviewer_reasons(
                reviews,
                _get_available(reviews),
                {n: "success" for n in REVIEWER_NAMES},
            )
            assert reasons["claude"] not in BENIGN_ROSTER_REASONS, (
                f"a payload with error={forged!r} forged a benign reason"
            )
            assert reasons["claude"].startswith(FAILED_DETAIL_PREFIX)

    def test_no_benign_reason_wears_the_payload_namespace(self) -> None:
        # What makes the prefix a partition rather than one more string:
        # prefixing keeps payload text out of the benign set only while no
        # benign reason starts with the prefix, so a benign reason coined
        # later cannot reopen the collision by accident.
        for reason in sorted(BENIGN_ROSTER_REASONS):
            assert not reason.startswith(FAILED_DETAIL_PREFIX)

    @pytest.mark.parametrize(
        "flags",
        [
            {},
            {"sequential_bypass": True},
        ],
    )
    @pytest.mark.parametrize("conclusion", ["", "success", "failure", "skipped", "??"])
    def test_payload_text_never_reaches_missing_unnamespaced(
        self, flags: dict[str, bool], conclusion: str
    ) -> None:
        # The class, not the one instance: every reason the roster can
        # carry is a module constant, a _missing_reason lookup over a
        # closed set of job conclusions, or payload text through the one
        # namespaced helper. This pins the third case across the bypass
        # flag and every conclusion, so a branch added later that hands
        # payload text straight to `missing` fails here rather than at a
        # consumer's gate.
        reviews = _all_ok()
        reviews["gemini"] = {
            "summary": "..",
            "early_exit": False,
            "issues": [],
            "status": "failed",
            "error": SEQUENTIAL_EARLY_EXIT_ROSTER_REASON,
        }
        conclusions = {n: "success" for n in REVIEWER_NAMES}
        conclusions["gemini"] = conclusion
        reasons = _missing_reviewer_reasons(
            reviews, _get_available(reviews), conclusions, **flags
        )
        assert (
            reasons["gemini"]
            == f"{FAILED_DETAIL_PREFIX}{SEQUENTIAL_EARLY_EXIT_ROSTER_REASON}"
        )

    @pytest.mark.parametrize("readme", ["README.md", "README.ko.md"])
    def test_the_readme_documents_every_benign_reason(self, readme: str) -> None:
        # The partition is a consumer-facing promise -- the output
        # descriptions send a reader to the README for it -- and a reason
        # renamed in the script with the table left behind would leave every
        # gate written against the table silently blocking a benign round.
        text = (Path(__file__).resolve().parents[3] / readme).read_text(
            encoding="utf-8"
        )
        for reason in sorted(BENIGN_ROSTER_REASONS):
            assert reason in text, f"{readme} does not document '{reason}'"
        assert POLICY_SKIP_ROSTER_REASON in BENIGN_ROSTER_REASONS


class TestNoArtifactRoundIsNotBenign:
    """Every reviewer job green with nothing uploaded is an unknown.

    The roster used to call this round a benign early exit, on the premise
    that it meant a trivial diff. It does not mean that, and the aggregate
    cannot tell what it means:

    * The reviewer step in base-ai-review-single.yml is continue-on-error,
      so a step's own death does not fail the job. What turns that into a
      signal is an always() step that records the failure: claude's emits
      an error verdict, codex's fails the job, and gemini has neither --
      so a killed gemini step leaves the job green with nothing uploaded.
      The chaining-guard caveat in base-ai-review-orchestrator.yml says
      the same from the guard's side.
    * There is no trivial-diff round behind it either. Every "nothing to
      review" state is settled before a reviewer runs: an empty diff fails
      prepare (extract_pr_diff.sh, AT-2201), a size skip exits 1, and an
      all-excluded diff takes the policy branch. base-ai-review-single.yml
      says it from the reviewer's side -- the prompt requires a verdict
      file even for early_exit, so a missing one is always an
      infrastructure failure.

    So the two states are indistinguishable from here, and a benign label
    is the one reading that merges on an outage -- the payload-forgery
    defect this ticket also fixed, reached without an attacker.
    """

    @staticmethod
    def _round(
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        env: dict[str, str] | None = None,
    ) -> tuple[dict[str, Any], str, int]:
        """The outage round end to end: roster, comment, exit code."""
        output_path = tmp_path / "gh-output"
        monkeypatch.setenv("GITHUB_OUTPUT", str(output_path))
        monkeypatch.setenv("PR_NUMBER", "174")
        monkeypatch.setenv("GITHUB_REPOSITORY", "owner/repo")
        for name in REVIEWER_NAMES:
            monkeypatch.setenv(f"REVIEWER_RESULT_{name.upper()}", "success")
        for key, value in (env or {}).items():
            monkeypatch.setenv(key, value)
        code = 0
        with (
            patch("aggregate_reviews.load_reviews", return_value=_nothing_written()),
            patch("aggregate_reviews.post_verdict") as mock_post,
        ):
            try:
                main()
            except SystemExit as exc:  # pragma: no cover - only on a regression
                code = int(exc.code or 0)
        return _read_back(output_path), mock_post.call_args[0][0], code

    def test_no_reviewer_is_reported_as_a_benign_skip(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        out, _comment, code = self._round(monkeypatch, tmp_path)
        assert out["reviewers_responded_count"] == "0"
        assert out["reviewers_expected_count"] == "3"
        assert out["roster"]["responded"] == []
        # The conclusion-derived reason, which names the ambiguity instead
        # of resolving it towards merge.
        assert out["roster"]["missing"] == {
            n: _missing_reason("success") for n in REVIEWER_NAMES
        }
        assert BENIGN_ROSTER_REASONS.isdisjoint(out["roster"]["missing"].values())
        # The classification changed; the run's colour did not. Whether a
        # 0/3 round should go red is a fleet decision, and _check_insufficient
        # already approved this path before the roster existed.
        assert code == 0

    def test_a_sequential_round_does_not_borrow_a_parallel_reason(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # The bypass that produced the old label was never conditioned on
        # REVIEW_MODE, so a sequential round with this signature was handed
        # the parallel early-exit reason -- a reason whose own text claims a
        # round that did not happen. Nothing distinguishes the modes here
        # now, and that is the point: neither one is benign.
        out, _comment, code = self._round(
            monkeypatch, tmp_path, {"REVIEW_MODE": "sequential"}
        )
        assert out["roster"]["missing"] == {
            n: _missing_reason("success") for n in REVIEWER_NAMES
        }
        assert (
            SEQUENTIAL_EARLY_EXIT_ROSTER_REASON not in out["roster"]["missing"].values()
        )
        assert code == 0

    def test_the_comment_and_the_roster_give_the_same_reason(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # Sharing _missing_reviewer_reasons is what keeps the two
        # renderings one computation. It held while the roster said
        # "benign" and the comment said otherwise only because the flags
        # were threaded through; with the label gone it has to hold with
        # nothing threaded through at all.
        out, comment, _code = self._round(monkeypatch, tmp_path)
        for name, reason in out["roster"]["missing"].items():
            assert f"{name}: {reason}" in comment


class TestAggregateWorkflowSurfacesTheRoster:
    """The emitted values must actually leave the job.

    A value written to $GITHUB_OUTPUT by a step with no `id` is unreadable,
    and a job output a reusable workflow never re-exports does not reach the
    caller -- any gap makes the emit silently pointless. There are two
    re-export hops, not one: consumers call the orchestrator (README.md,
    examples/consumer-thin-trigger.yml), which invokes the aggregate as a
    job, so the aggregate's own declaration alone stops one level short.
    """

    _KEYS = (
        "reviewer_roster",
        "reviewers_expected_count",
        "reviewers_responded_count",
    )

    # Not dict[str, Any]: `on:` is YAML 1.1, so PyYAML hands back the key as
    # the boolean True, and the top level is genuinely not string-keyed.
    @staticmethod
    def _load(name: str) -> dict[Any, Any]:
        path = Path(__file__).resolve().parents[2] / "workflows" / name
        return yaml.safe_load(path.read_text(encoding="utf-8"))

    @classmethod
    def _workflow(cls) -> dict[Any, Any]:
        return cls._load("base-ai-review-aggregate.yml")

    def test_aggregate_step_carries_an_id(self) -> None:
        steps = self._workflow()["jobs"]["aggregate"]["steps"]
        aggregate = [s for s in steps if s.get("name") == "Aggregate and post verdict"]
        assert len(aggregate) == 1, "step name is stale -- the selector found nothing"
        assert aggregate[0].get("id") == "aggregate"

    def test_job_outputs_reference_that_step(self) -> None:
        outputs = self._workflow()["jobs"]["aggregate"]["outputs"]
        for key in self._KEYS:
            assert outputs[key] == "${{ steps.aggregate.outputs." + key + " }}"

    def test_workflow_call_re_exports_the_job_outputs(self) -> None:
        # `on` parses as the boolean True under YAML 1.1.
        outputs = self._workflow()[True]["workflow_call"]["outputs"]
        for key in self._KEYS:
            assert outputs[key]["value"] == "${{ jobs.aggregate.outputs." + key + " }}"

    def test_the_orchestrator_re_exports_them_to_the_consumer(self) -> None:
        # The hop the aggregate's own declaration cannot make: this is the
        # workflow a consumer thin trigger calls, and its `aggregate` job is
        # the reusable aggregate above.
        orchestrator = self._load("base-ai-review-orchestrator.yml")
        assert (
            orchestrator["jobs"]["aggregate"]["uses"]
            == "./.github/workflows/base-ai-review-aggregate.yml"
        ), "the aggregate job no longer calls the workflow these come from"
        outputs = orchestrator[True]["workflow_call"]["outputs"]
        for key in self._KEYS:
            assert outputs[key]["value"] == "${{ jobs.aggregate.outputs." + key + " }}"


requires_jq = pytest.mark.skipif(shutil.which("jq") is None, reason="jq not installed")

_GATE_STEP_NAME = "Require full reviewer coverage"
# The gate step reads the three outputs through these env names.
_OUTPUT_TO_ENV = {
    "reviewer_roster": "ROSTER",
    "reviewers_expected_count": "EXPECTED",
    "reviewers_responded_count": "RESPONDED",
}


def _gate_snippet(readme: str) -> str:
    """The ``run:`` body of the merge-gate step the README documents.

    Extracted from the README rather than copied here: the snippet is
    executable documentation -- a consumer pastes it -- and a copy in the
    test would keep passing after the documented one drifted.
    """
    text = (Path(__file__).resolve().parents[3] / readme).read_text(encoding="utf-8")
    blocks = [
        block
        for block in re.findall(r"^```yaml\n(.*?)^```", text, re.S | re.M)
        if _GATE_STEP_NAME in block
    ]
    assert len(blocks) == 1, f"{readme}: expected one gate snippet, found {len(blocks)}"
    steps = yaml.safe_load(blocks[0])["jobs"]["gate"]["steps"]
    runs = [step["run"] for step in steps if step.get("name") == _GATE_STEP_NAME]
    assert len(runs) == 1, f"{readme}: the gate step's name is stale"
    return str(runs[0])


# The jq filter binds its benign list to `$benign` before matching on it.
# Captured from the snippet, not restated here, for the same reason the
# `run:` body is: the array is a copy of BENIGN_ROSTER_REASONS, and a copy
# a test spells out again cannot show that the two have drifted apart.
_BENIGN_ARRAY_RE = re.compile(r"(\[[^]]*\])\s*as\s+\$benign")


def _gate_benign_reasons(readme: str) -> list[str]:
    """The reasons the README's jq filter actually accepts."""
    arrays = _BENIGN_ARRAY_RE.findall(_gate_snippet(readme))
    assert len(arrays) == 1, (
        f"{readme}: expected one `as $benign` array in the gate snippet, "
        f"found {len(arrays)}"
    )
    return list(json.loads(arrays[0]))


def _run_gate(readme: str, env: dict[str, str]) -> int:
    """Run the documented snippet the way a runner would."""
    return subprocess.run(
        ["bash", "-e", "-o", "pipefail", "-c", _gate_snippet(readme)],
        env={"PATH": os.environ["PATH"], **env},
        capture_output=True,
        text=True,
        timeout=30,
    ).returncode


def _gate_env_from_roster(
    available: dict[str, dict[str, Any]],
    missing: dict[str, str],
    output_path: Path,
) -> dict[str, str]:
    """Gate inputs written by the real emit path, from a chosen roster.

    The JSON a gate reads is whatever ``write_reviewer_roster`` writes --
    escaping, key order and all -- so a test that needs one specific reason
    in ``missing`` still gets the shipped serialization rather than a
    hand-built approximation of it.
    """
    with patch.dict(os.environ, {"GITHUB_OUTPUT": str(output_path)}):
        write_reviewer_roster(available, missing)
    emitted = _read_back(output_path)
    return {name: str(emitted[key]) for key, name in _OUTPUT_TO_ENV.items()}


def _emitted_gate_env(
    reviews: dict[str, dict[str, Any] | None],
    conclusions: dict[str, str],
    output_path: Path,
    **flags: bool,
) -> dict[str, str]:
    """Gate inputs as the emit path produces them, not hand-written."""
    available = _get_available(reviews)
    return _gate_env_from_roster(
        available,
        _missing_reviewer_reasons(reviews, available, conclusions, **flags),
        output_path,
    )


@requires_jq
@pytest.mark.parametrize("readme", ["README.md", "README.ko.md"])
class TestDocumentedGateSnippet:
    """The README's gate snippet, run rather than read (AT-2511).

    The snippet is the only consumer of the roster this repository ships,
    and the failure that matters is the one where it *passes*: reading its
    text proves the words are there, not that a lost output blocks the
    merge. Both READMEs carry their own copy -- translated comments and
    all -- so each is executed separately: a fix applied to one side only
    is exactly the drift this catches.
    """

    def test_full_coverage_passes(self, readme: str, tmp_path: Path) -> None:
        env = _emitted_gate_env(
            _all_ok(), {n: "success" for n in REVIEWER_NAMES}, tmp_path / "o"
        )
        assert env["RESPONDED"] == env["EXPECTED"] == "3"
        assert _run_gate(readme, env) == 0

    def test_a_benign_missing_reason_passes(self, readme: str, tmp_path: Path) -> None:
        # 1/3 on a sequential early-exit round is a green round, not a
        # degraded one: the reasons table is what lets the gate tell them
        # apart.
        reviews = _nothing_written()
        reviews["claude"] = _early_exit("claude")
        conclusions = {n: "skipped" for n in REVIEWER_NAMES}
        conclusions["claude"] = "success"
        env = _emitted_gate_env(
            reviews,
            conclusions,
            tmp_path / "o",
            sequential_bypass=True,
        )
        assert env["RESPONDED"] == "1" and env["EXPECTED"] == "3"
        assert SEQUENTIAL_EARLY_EXIT_ROSTER_REASON in env["ROSTER"]
        assert _run_gate(readme, env) == 0

    def test_the_no_artifact_round_blocks(
        self, readme: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The gate side of TestNoArtifactRoundIsNotBenign, and the one that
        # decides a merge: every job green, nothing uploaded. The run is
        # green, so a gate keyed on the conclusion merges; this one must
        # not, because the same roster is what a credential outage
        # produces. Driven through main() rather than through
        # _missing_reviewer_reasons directly, so reinstating the benign
        # label anywhere on the emit path -- the constant, the branch, or
        # the flag main() threads in -- fails here.
        output_path = tmp_path / "gh-output"
        monkeypatch.setenv("GITHUB_OUTPUT", str(output_path))
        monkeypatch.setenv("PR_NUMBER", "174")
        monkeypatch.setenv("GITHUB_REPOSITORY", "owner/repo")
        for name in REVIEWER_NAMES:
            monkeypatch.setenv(f"REVIEWER_RESULT_{name.upper()}", "success")
        with (
            patch("aggregate_reviews.load_reviews", return_value=_nothing_written()),
            patch("aggregate_reviews.post_verdict"),
        ):
            main()
        emitted = _read_back(output_path)
        env = {name: str(emitted[key]) for key, name in _OUTPUT_TO_ENV.items()}
        assert env["RESPONDED"] == "0" and env["EXPECTED"] == "3"
        assert _run_gate(readme, env) != 0

    def test_the_snippet_accepts_exactly_the_benign_reasons(self, readme: str) -> None:
        # The jq array is a fourth copy of BENIGN_ROSTER_REASONS -- after the
        # constant, the output descriptions and the reasons table -- and the
        # only one a gate decides on. Nothing above pins it: the table check
        # is satisfied by prose anywhere in the file, so a reason added to
        # the constant and to the table alone would leave the documented
        # gate blocking a round the script calls benign, with every test
        # green. Set-equality catches the reverse too, an array entry the
        # script can no longer emit.
        reasons = _gate_benign_reasons(readme)
        assert len(reasons) == len(set(reasons)), (
            f"{readme}: the gate snippet lists a benign reason twice"
        )
        assert set(reasons) == set(BENIGN_ROSTER_REASONS), (
            f"{readme}: the gate snippet's benign list has drifted from "
            "BENIGN_ROSTER_REASONS"
        )

    @pytest.mark.parametrize("reason", sorted(BENIGN_ROSTER_REASONS))
    def test_every_benign_reason_passes(
        self, readme: str, tmp_path: Path, reason: str
    ) -> None:
        # Set-equality above compares text; this runs it. Parametrized over
        # the constant so a reason coined later is executed against both
        # snippets without anyone remembering to add a case -- an enumerated
        # list here would be one more copy to keep in step.
        env = _gate_env_from_roster(
            {}, {name: reason for name in REVIEWER_NAMES}, tmp_path / "o"
        )
        assert env["RESPONDED"] == "0" and env["EXPECTED"] == "3"
        assert _run_gate(readme, env) == 0

    def test_a_non_benign_missing_reason_blocks(
        self, readme: str, tmp_path: Path
    ) -> None:
        reviews = _all_ok()
        reviews["claude"] = None
        conclusions = {n: "success" for n in REVIEWER_NAMES}
        conclusions["claude"] = "failure"
        env = _emitted_gate_env(reviews, conclusions, tmp_path / "o")
        assert _run_gate(readme, env) == 1

    def test_absent_outputs_block_rather_than_report_full_coverage(
        self, readme: str
    ) -> None:
        # The regression the emptiness guard exists for.
        # ``write_reviewer_roster`` degrades an unset or unwritable
        # $GITHUB_OUTPUT to a ``::warning::`` and lets ``main`` go on to
        # post the verdict, so the run ends green with all three absent --
        # and two empty strings compare equal, which without the guard is
        # indistinguishable from 3/3.
        assert _run_gate(readme, {"ROSTER": "", "EXPECTED": "", "RESPONDED": ""}) == 1

    @pytest.mark.parametrize(
        ("expected", "responded"), [("3", ""), ("", "3"), ("0", "")]
    )
    def test_one_absent_count_blocks(
        self, readme: str, expected: str, responded: str
    ) -> None:
        # Half a signal is not a signal. ``0`` is a real count -- the
        # policy skip emits it -- and ``""`` is the absence of one, so
        # neither may stand in for the other.
        assert (
            _run_gate(
                readme,
                {
                    "ROSTER": json.dumps(
                        {"expected": [], "responded": [], "missing": {}}
                    ),
                    "EXPECTED": expected,
                    "RESPONDED": responded,
                },
            )
            == 1
        )

    def test_an_absent_roster_blocks_even_when_the_counts_agree(
        self, readme: str
    ) -> None:
        # A truncated write can leave the counts readable and the roster
        # gone; the equality test alone would wave that through.
        assert _run_gate(readme, {"ROSTER": "", "EXPECTED": "3", "RESPONDED": "3"}) == 1
