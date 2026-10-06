"""The runner consumes verdict facts, never a green aggregate exit alone."""

from __future__ import annotations

import json
from pathlib import Path
import subprocess
from typing import Any

import pytest
import yaml

import aggregate_reviews as aggregate

WORKFLOWS = Path(__file__).resolve().parents[2] / "workflows"


@pytest.fixture
def result_round(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.chdir(tmp_path)
    output = tmp_path / "outputs"
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", "")
    monkeypatch.setenv("GITHUB_REPOSITORY", "target/repository")
    monkeypatch.setenv("PR_NUMBER", "42")
    monkeypatch.setenv("HEAD_SHA", "a" * 40)
    monkeypatch.setenv("PREPARE_RESULT", "success")
    monkeypatch.setenv("SIZE_SKIPPED", "false")
    monkeypatch.setenv("POLICY_SKIPPED", "false")
    monkeypatch.setenv("REVIEW_MODE", "parallel")
    monkeypatch.setenv("ALLOW_AUTO_APPROVE", "false")
    for name in aggregate.REVIEWERS:
        monkeypatch.setenv(f"REVIEWER_RESULT_{name.upper()}", "success")
    monkeypatch.setattr(aggregate, "_head_is_stale", lambda: False)
    monkeypatch.setattr(aggregate, "post_verdict", lambda *args, **kwargs: True)
    monkeypatch.setattr(aggregate, "fetch_round_count", lambda *args: 0)
    monkeypatch.setattr(aggregate, "_fetch_fold_comments", lambda *args: [])
    monkeypatch.setattr(aggregate, "_fetch_inline_posts", lambda *args: [])
    monkeypatch.setattr(
        aggregate.subprocess,
        "run",
        lambda *args, **kwargs: pytest.fail("unexpected external call"),
    )
    return output


def payload(**extra: Any) -> dict[str, Any]:
    return {
        "summary": "Review completed",
        "status": "ok",
        "early_exit": False,
        "issues": [],
        **extra,
    }


def read_result(output: Path) -> dict[str, Any]:
    matches = [
        line.split("=", 1)[1]
        for line in output.read_text().splitlines()
        if line.startswith("review_result=")
    ]
    assert len(matches) == 1, "every completed aggregate path emits exactly one result"
    return json.loads(matches[0])


@pytest.mark.parametrize("mode", ["parallel", "sequential"])
def test_full_review_exports_the_exact_target_and_posted_result(
    result_round: Path, monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    monkeypatch.setenv("REVIEW_MODE", mode)
    monkeypatch.setattr(
        aggregate,
        "load_reviews",
        lambda: {name: payload() for name in aggregate.REVIEWERS},
    )
    aggregate.main()
    result = read_result(result_round)
    assert result == {
        "version": 1,
        "target_repo": "target/repository",
        "pr_number": "42",
        "head_sha": "a" * 40,
        "verdict": "approve",
        "reason": "reviewed",
        "expected": list(aggregate.REVIEWERS),
        "responded": list(aggregate.REVIEWERS),
        "partial": [],
        "unreviewed_majors": 0,
        "policy_skipped": False,
        "posted": True,
    }


def test_partial_error_payload_is_not_hidden_by_full_availability(
    result_round: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    reviews = {name: payload() for name in aggregate.REVIEWERS}
    reviews["claude"] = payload(error="private provider detail")
    monkeypatch.setattr(aggregate, "load_reviews", lambda: reviews)
    aggregate.main()
    result = read_result(result_round)
    assert result["responded"] == list(aggregate.REVIEWERS)
    assert result["partial"] == ["claude"]
    assert "private provider detail" not in json.dumps(result)


def test_missing_artifacts_keep_old_exit_policy_but_export_no_responses(
    result_round: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        aggregate, "load_reviews", lambda: {name: None for name in aggregate.REVIEWERS}
    )
    aggregate.main()
    result = read_result(result_round)
    assert result["verdict"] == "approve"
    assert result["responded"] == []
    assert set(result["partial"]) == set(aggregate.REVIEWERS)


def test_unreviewed_major_is_exported_even_when_the_verdict_is_approve(
    result_round: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    reviews = {name: payload() for name in aggregate.REVIEWERS}
    reviews["claude"] = payload(
        issues=[{"severity": "major", "description": "Unreviewed finding"}]
    )
    monkeypatch.setattr(aggregate, "load_reviews", lambda: reviews)
    aggregate.main()
    result = read_result(result_round)
    assert result["verdict"] == "approve"
    assert result["unreviewed_majors"] == 1


@pytest.mark.parametrize(
    ("kind", "exit_code"), [("prepare", 1), ("size", 1), ("policy", 0), ("stale", 1)]
)
def test_early_paths_export_facts_without_changing_existing_exit_codes(
    result_round: Path, monkeypatch: pytest.MonkeyPatch, kind: str, exit_code: int
) -> None:
    if kind == "prepare":
        monkeypatch.setenv("PREPARE_RESULT", "failure")
    elif kind == "size":
        monkeypatch.setenv("SIZE_SKIPPED", "true")
        monkeypatch.setenv("SIZE_TOTAL", "3001")
        monkeypatch.setenv("SIZE_LIMIT", "3000")
    elif kind == "policy":
        monkeypatch.setenv("POLICY_SKIPPED", "true")
        monkeypatch.setenv("EXCLUDED_PATHS", "fixture.bin")
    else:
        monkeypatch.setattr(aggregate, "_head_is_stale", lambda: True)
    with pytest.raises(SystemExit) as exc:
        aggregate.main()
    assert exc.value.code == exit_code
    result = read_result(result_round)
    assert (
        result["reason"]
        == {
            "prepare": "prepare_failed",
            "size": "size_skipped",
            "policy": "policy_skipped",
            "stale": "head_superseded",
        }[kind]
    )
    assert result["responded"] == []
    assert result["policy_skipped"] is (kind == "policy")
    assert result["posted"] is (kind != "stale")


def test_unmarked_comment_does_not_claim_a_published_verdict(
    result_round: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        aggregate,
        "load_reviews",
        lambda: {name: payload() for name in aggregate.REVIEWERS},
    )
    monkeypatch.setattr(
        aggregate, "format_summary", lambda *args, **kwargs: "not a verdict"
    )
    aggregate.main()
    assert read_result(result_round)["posted"] is False


def test_fatal_post_failure_exports_no_published_verdict(
    result_round: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        aggregate,
        "load_reviews",
        lambda: {name: payload() for name in aggregate.REVIEWERS},
    )

    def fail(*args: Any, **kwargs: Any) -> bool:
        raise SystemExit(1)

    monkeypatch.setattr(aggregate, "post_verdict", fail)
    with pytest.raises(SystemExit) as exc:
        aggregate.main()
    assert exc.value.code == 1
    assert read_result(result_round)["posted"] is False


def test_unsuccessful_post_return_is_not_hidden_by_a_green_exit(
    result_round: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        aggregate,
        "load_reviews",
        lambda: {name: payload() for name in aggregate.REVIEWERS},
    )
    monkeypatch.setattr(aggregate, "post_verdict", lambda *args, **kwargs: False)
    # Timeout fallback failure historically returns without failing the job.
    aggregate.main()
    assert read_result(result_round)["posted"] is False


def test_missing_head_remains_unknown_in_the_export(
    result_round: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HEAD_SHA", "")
    monkeypatch.setenv("PREPARE_RESULT", "failure")
    with pytest.raises(SystemExit):
        aggregate.main()
    assert read_result(result_round)["head_sha"] == ""


def test_unwritable_result_output_does_not_change_the_existing_exit_policy(
    result_round: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("GITHUB_OUTPUT", str(result_round.parent))
    monkeypatch.setattr(
        aggregate,
        "load_reviews",
        lambda: {name: payload() for name in aggregate.REVIEWERS},
    )
    aggregate.main()
    assert "Failed to write review_result" in capsys.readouterr().err


def test_result_leaves_the_step_job_and_both_reusable_workflow_hops() -> None:
    aggregate_workflow = yaml.safe_load(
        (WORKFLOWS / "base-ai-review-aggregate.yml").read_text()
    )
    orchestrator = yaml.safe_load(
        (WORKFLOWS / "base-ai-review-orchestrator.yml").read_text()
    )
    assert aggregate_workflow["jobs"]["aggregate"]["outputs"]["review_result"] == (
        "${{ steps.aggregate.outputs.review_result }}"
    )
    for workflow in (aggregate_workflow, orchestrator):
        events = workflow.get("on", workflow.get(True))
        assert events["workflow_call"]["outputs"]["review_result"]["value"] == (
            "${{ jobs.aggregate.outputs.review_result }}"
        )


@pytest.mark.parametrize("fallback_ok", [False, True])
def test_timed_out_review_returns_the_actual_comment_fallback_outcome(
    monkeypatch: pytest.MonkeyPatch, fallback_ok: bool
) -> None:
    monkeypatch.setenv("PR_NUMBER", "42")
    monkeypatch.setenv("GITHUB_REPOSITORY", "target/repository")
    monkeypatch.setattr(
        aggregate, "_fold_prior_round_items", lambda *args, **kwargs: []
    )

    def timeout(*args: Any, **kwargs: Any) -> None:
        raise subprocess.TimeoutExpired("gh", 1)

    monkeypatch.setattr(aggregate.subprocess, "run", timeout)
    monkeypatch.setattr(aggregate, "_post_comment", lambda *args, **kwargs: fallback_ok)
    assert (
        aggregate.post_verdict(
            "body", "approve", comment_only=False, unreviewed_majors=0
        )
        is fallback_ok
    )


@pytest.mark.parametrize("post_ok", [False, True])
def test_withheld_review_only_returns_success_after_its_comment_lands(
    monkeypatch: pytest.MonkeyPatch, post_ok: bool
) -> None:
    monkeypatch.setenv("PR_NUMBER", "42")
    monkeypatch.setenv("GITHUB_REPOSITORY", "target/repository")
    monkeypatch.setattr(
        aggregate, "_fold_prior_round_items", lambda *args, **kwargs: []
    )
    monkeypatch.setattr(aggregate, "_post_comment", lambda *args, **kwargs: post_ok)
    if post_ok:
        assert (
            aggregate.post_verdict(
                "body", "approve", comment_only=False, unreviewed_majors=1
            )
            is True
        )
    else:
        with pytest.raises(SystemExit) as exc:
            aggregate.post_verdict(
                "body", "approve", comment_only=False, unreviewed_majors=1
            )
        assert exc.value.code == 1
