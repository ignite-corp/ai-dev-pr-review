"""Gemini's quota chain is one step: API key exhausted -> exclude (AT-3714).

Gemini has no subscription path. A 429 RESOURCE_EXHAUSTED from its API is
both a per-minute rate limit (transient, already retried with backoff) and
a used-up daily or billing quota, and the two are told apart only by the
QuotaFailure violations in the error's details. The classifier reads those
structured fields alone -- code, status and quotaId, never the message --
and excludes only on a quotaId in _QUOTA_EXHAUSTED_IDS. That list is empty
by the owner's decision of 2026-11-25: no exhaustion has been measured, so a
quota error stays a failure. The tests show the empty list is the only
reason.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
from typing import Any

import pytest
import yaml

import aggregate_reviews as aggregate
import review_gemini

ROOT = Path(__file__).resolve().parents[3]
WORKFLOWS = ROOT / ".github/workflows"
QUOTA = "included_quota_unavailable"
DAILY = "GenerateRequestsPerDayPerProjectPerModel"


class FakeApiError(Exception):
    """The fields google-genai's APIError carries: code, status, details."""

    def __init__(self, code: Any, status: Any, details: Any) -> None:
        super().__init__("Resource has been exhausted (e.g. check quota).")
        self.code = code
        self.status = status
        self.details = details


def quota_failure(*quota_ids: str) -> dict[str, Any]:
    return {
        "error": {
            "code": 429,
            "status": "RESOURCE_EXHAUSTED",
            "message": "free text a classifier must not read",
            "details": [
                {
                    "@type": "type.googleapis.com/google.rpc.QuotaFailure",
                    "violations": [{"quotaId": q} for q in quota_ids],
                },
                {
                    "@type": "type.googleapis.com/google.rpc.RetryInfo",
                    "retryDelay": "3s",
                },
            ],
        }
    }


def exhausted() -> FakeApiError:
    return FakeApiError(429, "RESOURCE_EXHAUSTED", quota_failure(DAILY))


def test_the_list_is_empty_until_an_exhaustion_is_measured() -> None:
    assert review_gemini._QUOTA_EXHAUSTED_IDS == frozenset()


def test_a_daily_quota_violation_is_a_failure_with_the_empty_list() -> None:
    assert review_gemini.quota_exhausted(exhausted()) is False


def test_the_empty_list_is_the_only_reason_it_is_not_excluded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(review_gemini, "_QUOTA_EXHAUSTED_IDS", frozenset({DAILY}))
    assert review_gemini.quota_exhausted(exhausted()) is True


@pytest.mark.parametrize(
    "error",
    [
        # a per-minute rate limit: listed ids only
        FakeApiError(
            429, "RESOURCE_EXHAUSTED", quota_failure("GenerateRequestsPerMinute")
        ),
        # no QuotaFailure at all
        FakeApiError(429, "RESOURCE_EXHAUSTED", {"error": {"code": 429}}),
        # the id, under another code or status
        FakeApiError(403, "RESOURCE_EXHAUSTED", quota_failure(DAILY)),
        FakeApiError(429, "UNAVAILABLE", quota_failure(DAILY)),
        FakeApiError("429", "RESOURCE_EXHAUSTED", quota_failure(DAILY)),
        # the id only in the message text
        FakeApiError(429, "RESOURCE_EXHAUSTED", {"error": {"message": DAILY}}),
        # malformed details
        FakeApiError(429, "RESOURCE_EXHAUSTED", None),
        FakeApiError(429, "RESOURCE_EXHAUSTED", {"error": {"details": "x"}}),
        FakeApiError(
            429, "RESOURCE_EXHAUSTED", {"error": {"details": [{"violations": "x"}]}}
        ),
        RuntimeError(f"429 RESOURCE_EXHAUSTED {DAILY}"),
    ],
)
def test_nothing_but_a_listed_violation_is_excluded(
    monkeypatch: pytest.MonkeyPatch, error: Exception
) -> None:
    monkeypatch.setattr(review_gemini, "_QUOTA_EXHAUSTED_IDS", frozenset({DAILY}))
    assert review_gemini.quota_exhausted(error) is False


def run_main(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, error: Exception
) -> dict[str, str]:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("GOOGLE_AI_API_KEY", "test-key")
    output = tmp_path / "outputs"
    output.write_text("")
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))

    def _raise(*args: Any, **kwargs: Any) -> Any:
        raise error

    monkeypatch.setattr(review_gemini, "_call_gemini_with_retry", _raise)
    monkeypatch.setattr(review_gemini.genai, "Client", lambda api_key=None: object())
    review_gemini.main()
    verdict = json.loads((tmp_path / review_gemini.REVIEW_FILE).read_text())
    assert verdict["status"] == "failed"
    return dict(line.split("=", 1) for line in output.read_text().splitlines())


def test_main_publishes_no_code_on_an_unlisted_exhaustion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert run_main(tmp_path, monkeypatch, exhausted()) == {}


def test_main_publishes_the_code_on_a_listed_exhaustion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(review_gemini, "_QUOTA_EXHAUSTED_IDS", frozenset({DAILY}))
    assert run_main(tmp_path, monkeypatch, exhausted()) == {"failure_code": QUOTA}


def test_main_records_the_structured_fields_for_measurement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    run_main(tmp_path, monkeypatch, exhausted())
    err = capsys.readouterr().err
    line = next(
        row for row in err.splitlines() if row.startswith("Gemini API error fields:")
    )
    assert "code=429" in line and "status=RESOURCE_EXHAUSTED" in line
    assert DAILY in line
    assert "free text" not in line


# --- the trusted path ---------------------------------------------------------

PUBLISH = "Publish trusted Gemini failure code"


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


def test_the_step_reads_the_review_steps_output() -> None:
    step = single_step(PUBLISH)
    assert step["id"] == "gemini-failure"
    assert step["if"] == "inputs.reviewer == 'gemini' && always()"
    assert (
        step["env"]["FAILURE_CODE"] == "${{ steps.gemini-review.outputs.failure_code }}"
    )
    assert step["env"]["STEP_OUTCOME"] == "${{ steps.gemini-review.outcome }}"


def test_publish_passes_only_the_quota_code(tmp_path: Path) -> None:
    assert publish(tmp_path, QUOTA) == {"code": QUOTA}
    assert publish(tmp_path, "x") == {}
    assert publish(tmp_path, QUOTA, outcome="failure") == {}


def test_the_code_leaves_the_job_and_the_reusable_workflow() -> None:
    workflow = single()
    assert workflow["jobs"]["review"]["outputs"]["gemini_failure"] == (
        "${{ steps.gemini-failure.outputs.code }}"
    )
    assert workflow[True]["workflow_call"]["outputs"]["gemini_failure"]["value"] == (
        "${{ jobs.review.outputs.gemini_failure }}"
    )


def test_the_orchestrator_forwards_it_by_gemini_slot_only() -> None:
    orchestrator = yaml.safe_load(
        (WORKFLOWS / "base-ai-review-orchestrator.yml").read_text()
    )
    forwarded = orchestrator["jobs"]["aggregate"]["with"]
    assert forwarded["gemini_failure"] == forwarded["copilot_failure"].replace(
        "copilot", "gemini"
    )


def test_the_aggregate_reads_it_from_its_input_only() -> None:
    workflow = yaml.safe_load((WORKFLOWS / "base-ai-review-aggregate.yml").read_text())
    declared = workflow[True]["workflow_call"]["inputs"]["gemini_failure"]
    assert declared["type"] == "string" and declared["default"] == ""
    run = next(
        s for s in workflow["jobs"]["aggregate"]["steps"] if s.get("id") == "aggregate"
    )
    assert run["env"]["REVIEWER_FAILURE_GEMINI"] == "${{ inputs.gemini_failure }}"


def test_the_aggregate_lists_gemini_on_the_trusted_code(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        aggregate,
        "REVIEWERS",
        {n: f"review-{n}.json" for n in ("claude", "codex", "gemini", "copilot")},
    )
    available: dict[str, dict[str, Any]] = {
        n: {} for n in ("claude", "codex", "copilot")
    }
    monkeypatch.setenv("REVIEWER_FAILURE_GEMINI", QUOTA)
    assert aggregate._quota_unavailable(available) == ["gemini"]


# #324 R1: the measurement line writes quotaIds from the response body to a
# log GitHub scans for workflow commands, so only id-shaped values reach it;
# and the real retry wrapper hands main() the API error itself, which is
# what the classifier reads.


def test_the_measurement_line_prints_only_id_shaped_quota_ids(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    forged = FakeApiError(
        429,
        "RESOURCE_EXHAUSTED",
        quota_failure(DAILY, "x\n::error::forged", "a b"),
    )
    run_main(tmp_path, monkeypatch, forged)
    err = capsys.readouterr().err
    line = next(
        row for row in err.splitlines() if row.startswith("Gemini API error fields:")
    )
    assert f"quota_ids={DAILY},-,-" in line
    assert "::error::forged" not in err.replace(line, "")
    assert "forged" not in line


def test_the_retry_wrapper_hands_main_the_api_error_itself(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(review_gemini, "_QUOTA_EXHAUSTED_IDS", frozenset({DAILY}))
    monkeypatch.setattr(review_gemini.time, "sleep", lambda seconds: None)
    error = exhausted()
    calls: list[int] = []

    class Models:
        def generate_content(self, **kwargs: Any) -> Any:
            calls.append(1)
            raise error

    class Client:
        models = Models()

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("GOOGLE_AI_API_KEY", "test-key")
    output = tmp_path / "outputs"
    output.write_text("")
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))
    monkeypatch.setattr(review_gemini.genai, "Client", lambda api_key=None: Client())
    review_gemini.main()
    # Every attempt ran: a 429 is retried, then the same error surfaces.
    assert len(calls) == review_gemini._MAX_RETRY_ATTEMPTS
    assert output.read_text() == f"failure_code={QUOTA}\n"


def test_the_measurement_line_prints_only_an_id_shaped_status(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # isupper() alone admits this: no lower-case letter in it.
    forged = FakeApiError(
        429, "RESOURCE_EXHAUSTED\n::ERROR::FORGED", quota_failure(DAILY)
    )
    run_main(tmp_path, monkeypatch, forged)
    err = capsys.readouterr().err
    line = next(
        row for row in err.splitlines() if row.startswith("Gemini API error fields:")
    )
    assert "status=- " in line
    assert "::ERROR::FORGED" not in err
