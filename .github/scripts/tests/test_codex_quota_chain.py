"""Codex's quota-chain code reaches the aggregate on the trusted path (AT-3714).

The run step publishes included_quota_unavailable when the workspace quota
is out and there is no API key to retry on, or when the API attempt fails
on a message in codex_review_auth._API_QUOTA_MESSAGES -- empty until one is
measured (test_codex_auth_workflow.py runs the step). These tests carry the
code from that step to review_result.quota_unavailable, the way Copilot's
and Claude's already travel.
"""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
from typing import Any

import pytest
import yaml

import aggregate_reviews as aggregate
import codex_review_auth as auth

ROOT = Path(__file__).resolve().parents[3]
WORKFLOWS = ROOT / ".github/workflows"
QUOTA = "included_quota_unavailable"
PUBLISH = "Publish trusted Codex failure code"


def test_the_api_list_is_empty_until_a_message_is_measured() -> None:
    assert auth._API_QUOTA_MESSAGES == frozenset()


def single() -> dict[Any, Any]:
    return yaml.safe_load((WORKFLOWS / "base-ai-review-single.yml").read_text())


def single_step(name: str) -> dict[str, Any]:
    return next(s for s in single()["jobs"]["review"]["steps"] if s.get("name") == name)


def publish(tmp_path: Path, code: str, outcome: str = "success") -> dict[str, str]:
    output = tmp_path / "published"
    output.write_text("")
    result = subprocess.run(
        [
            "bash",
            "--noprofile",
            "--norc",
            "-e",
            "-o",
            "pipefail",
            "-c",
            single_step(PUBLISH)["run"],
        ],
        env={
            "PATH": os.environ["PATH"],
            "GITHUB_OUTPUT": str(output),
            "FAILURE_CODE": code,
            "STEP_OUTCOME": outcome,
        },
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return dict(line.split("=", 1) for line in output.read_text().splitlines())


def test_the_step_reads_the_run_steps_output() -> None:
    step = single_step(PUBLISH)
    assert step["id"] == "codex-failure"
    assert step["if"] == "inputs.reviewer == 'codex' && always()"
    assert step["env"]["FAILURE_CODE"] == "${{ steps.codex_run.outputs.failure_code }}"
    assert step["env"]["STEP_OUTCOME"] == "${{ steps.codex_run.outcome }}"


def test_publish_passes_the_quota_code(tmp_path: Path) -> None:
    assert publish(tmp_path, QUOTA) == {"code": QUOTA}


@pytest.mark.parametrize("code", ["", "INCLUDED_QUOTA_UNAVAILABLE", f"{QUOTA} ", "x"])
def test_publish_drops_anything_else(tmp_path: Path, code: str) -> None:
    assert publish(tmp_path, code) == {}


def test_publish_ignores_a_run_step_that_did_not_succeed(tmp_path: Path) -> None:
    assert publish(tmp_path, QUOTA, outcome="failure") == {}


def test_the_code_leaves_the_job_and_the_reusable_workflow() -> None:
    workflow = single()
    assert workflow["jobs"]["review"]["outputs"]["codex_failure"] == (
        "${{ steps.codex-failure.outputs.code }}"
    )
    assert workflow[True]["workflow_call"]["outputs"]["codex_failure"]["value"] == (
        "${{ jobs.review.outputs.codex_failure }}"
    )


def test_the_orchestrator_forwards_it_by_codex_slot_only() -> None:
    orchestrator = yaml.safe_load(
        (WORKFLOWS / "base-ai-review-orchestrator.yml").read_text()
    )
    forwarded = orchestrator["jobs"]["aggregate"]["with"]
    assert forwarded["codex_failure"] == forwarded["copilot_failure"].replace(
        "copilot", "codex"
    )


def test_the_aggregate_reads_it_from_its_input_only() -> None:
    workflow = yaml.safe_load((WORKFLOWS / "base-ai-review-aggregate.yml").read_text())
    declared = workflow[True]["workflow_call"]["inputs"]["codex_failure"]
    assert declared["type"] == "string" and declared["default"] == ""
    run = next(
        s for s in workflow["jobs"]["aggregate"]["steps"] if s.get("id") == "aggregate"
    )
    assert run["env"]["REVIEWER_FAILURE_CODEX"] == "${{ inputs.codex_failure }}"


def test_the_aggregate_lists_codex_on_the_trusted_code(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        aggregate,
        "REVIEWERS",
        {n: f"review-{n}.json" for n in ("claude", "codex", "gemini", "copilot")},
    )
    available: dict[str, dict[str, Any]] = {
        n: {} for n in ("claude", "gemini", "copilot")
    }
    monkeypatch.setenv("REVIEWER_FAILURE_CODEX", QUOTA)
    assert aggregate._quota_unavailable(available) == ["codex"]
    monkeypatch.setenv("REVIEWER_FAILURE_CODEX", "review_failed")
    assert aggregate._quota_unavailable(available) == []
