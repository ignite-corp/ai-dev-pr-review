"""Claude follows the quota chain: subscription -> API key -> exclude (AT-3714).

On ai-dev-infra-common#1237 (run 45408958654, 2026-11-24) the OAuth attempt
hit its usage limit, switch_claude_auth.py detected it and set
CLAUDE_FORCE_API, and the round still ended 2/3: the switch only reached the
next run. The composite now retries once with the API key in the same run,
and publishes a trusted failure code when the chain ends in exclusion:

- the OAuth attempt hit a usage limit and there is no API key, or
- the API attempt hit a limit listed in API_LIMIT_ERROR_PREFIXES.

That list is empty by the owner's decision of 2026-11-25 KST, recorded on
AT-3714: no API-key
exhaustion has been measured in this fleet, so an API attempt that fails
stays a failure. The tests below show the empty list is the only reason it
is not exempt.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import subprocess
from typing import Any

import pytest
import yaml

import aggregate_reviews as aggregate
import switch_claude_auth as sca

ROOT = Path(__file__).resolve().parents[3]
ACTION = ROOT / ".github/actions/claude-review/action.yml"
WORKFLOWS = ROOT / ".github/workflows"
SCRIPT = ROOT / ".github/scripts/switch_claude_auth.py"
QUOTA = "included_quota_unavailable"
LIMIT_TEXT = "You've hit your limit \u00b7 resets 3am (UTC)"


def exec_text(*messages: dict[str, Any]) -> str:
    return json.dumps(list(messages))


def limited_run() -> str:
    """The shape the #1237 OAuth attempt logged: a rejected rate-limit
    event, an errored assistant message and an errored result, all
    carrying an SDK usage-limit prefix."""
    return exec_text(
        {"type": "system", "subtype": "init", "model": "claude-x"},
        {
            "type": "rate_limit_event",
            "rate_limit_info": {"status": "rejected", "resetsAt": 1_800_003_600},
        },
        {
            "type": "assistant",
            "error": "rate_limit",
            "message": {"content": [{"type": "text", "text": LIMIT_TEXT}]},
        },
        {
            "type": "result",
            "subtype": "success",
            "is_error": True,
            "result": LIMIT_TEXT,
        },
    )


def transient_429() -> str:
    return exec_text(
        {
            "type": "assistant",
            "error": "rate_limit",
            "message": {"content": [{"type": "text", "text": "API Error: 429"}]},
        },
        {"type": "result", "is_error": True, "result": "API Error: 429"},
    )


# --- the classifier ---------------------------------------------------------


def test_the_api_list_is_empty_until_a_message_is_measured() -> None:
    assert sca.API_LIMIT_ERROR_PREFIXES == ()


def test_an_oauth_usage_limit_is_a_limit() -> None:
    assert sca.attempt_limited(limited_run(), "oauth") is True


def test_the_same_limit_on_the_api_attempt_is_a_failure_with_the_empty_list() -> None:
    assert sca.attempt_limited(limited_run(), "api") is False


def test_the_empty_list_is_the_only_reason_the_api_attempt_is_not_a_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sca, "API_LIMIT_ERROR_PREFIXES", ("You've hit your",))
    assert sca.attempt_limited(limited_run(), "api") is True


def test_a_listed_prefix_without_a_structured_signal_is_not_a_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sca, "API_LIMIT_ERROR_PREFIXES", ("You've hit your",))
    text = exec_text({"type": "result", "is_error": True, "result": LIMIT_TEXT})
    assert sca.attempt_limited(text, "api") is False


@pytest.mark.parametrize("attempt", ["oauth", "api"])
def test_a_transient_429_is_never_a_limit(
    monkeypatch: pytest.MonkeyPatch, attempt: str
) -> None:
    monkeypatch.setattr(sca, "API_LIMIT_ERROR_PREFIXES", ("You've hit your",))
    assert sca.attempt_limited(transient_429(), attempt) is False


def test_the_legacy_cli_signature_does_not_reach_the_api_attempt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sca, "API_LIMIT_ERROR_PREFIXES", ("You've hit your",))
    assert sca.attempt_limited("Claude AI usage limit reached|1800003600", "api") is (
        False
    )


def run_classify(
    tmp_path: Path, attempt: str, text: str | None
) -> tuple[subprocess.CompletedProcess[str], dict[str, str]]:
    output = tmp_path / "outputs"
    env = {"PATH": os.environ["PATH"], "GITHUB_OUTPUT": str(output)}
    if text is not None:
        exec_file = tmp_path / "exec.json"
        exec_file.write_text(text)
        env["EXEC_FILE"] = str(exec_file)
    result = subprocess.run(
        ["python3", str(SCRIPT), "classify", attempt],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    values = (
        dict(line.split("=", 1) for line in output.read_text().splitlines())
        if output.exists()
        else {}
    )
    return result, values


def test_classify_needs_no_token_and_writes_its_answer(tmp_path: Path) -> None:
    result, values = run_classify(tmp_path, "oauth", limited_run())
    assert result.returncode == 0, result.stderr
    assert values == {"limited": "true"}


@pytest.mark.parametrize("text", [None, "", "not json", transient_429()])
def test_classify_answers_false_when_nothing_proves_a_limit(
    tmp_path: Path, text: str | None
) -> None:
    result, values = run_classify(tmp_path, "oauth", text)
    assert result.returncode == 0, result.stderr
    assert values == {"limited": "false"}


def test_classify_never_prints_the_error_text(tmp_path: Path) -> None:
    secretish = "You've hit your limit \u00b7 org 1234-private-detail"
    text = limited_run().replace(LIMIT_TEXT.replace("\u00b7", "\\u00b7"), secretish)
    assert "private-detail" in text
    result, values = run_classify(tmp_path, "oauth", text)
    # Still classified, so the text was read, and still not printed.
    assert values == {"limited": "true"}
    assert "private-detail" not in result.stdout + result.stderr


# --- the composite ----------------------------------------------------------


def action() -> dict[str, Any]:
    return yaml.safe_load(ACTION.read_text())


def composite_step(step_id: str) -> dict[str, Any]:
    return next(s for s in action()["runs"]["steps"] if s.get("id") == step_id)


def evaluate(expression: str, values: dict[str, Any]) -> Any:
    """The subset of the Actions expression language the steps use."""
    expression = expression.strip().removeprefix("${{").removesuffix("}}")
    expression = re.sub(
        r"(?:steps|inputs)\.[A-Za-z0-9_.-]+",
        lambda match: repr(values.get(match[0], "")),
        expression,
    )
    expression = expression.replace("&&", " and ").replace("||", " or ")
    expression = re.sub(r"!(?!=)", " not ", expression)
    return eval(expression, {"__builtins__": {}}, {})


AUTH_KEYS = ("anthropic_api_key", "claude_code_oauth_token")


def test_the_retry_runs_the_same_pinned_action_with_the_same_arguments() -> None:
    first = composite_step("claude-review")
    retry = composite_step("claude-review-api")
    assert retry["uses"] == first["uses"]
    assert retry["env"] == first["env"]
    assert retry.get("continue-on-error") is True
    assert {k: v for k, v in retry["with"].items() if k not in AUTH_KEYS} == {
        k: v for k, v in first["with"].items() if k not in AUTH_KEYS
    }
    # The retry is the API path: the key, and never the subscription token.
    assert retry["with"]["anthropic_api_key"] == "${{ inputs.anthropic_api_key }}"
    assert retry["with"]["claude_code_oauth_token"] == ""


def base_values(**overrides: Any) -> dict[str, Any]:
    values = {
        "inputs.force_api": "false",
        "inputs.claude_code_oauth_token": "oauth",
        "inputs.anthropic_api_key": "key",
        "steps.claude-review.outcome": "failure",
        "steps.claude-review.outputs.execution_file": "/tmp/exec.json",
        "steps.oauth-limit.outputs.limited": "",
        "steps.claude-review-api.outcome": "skipped",
        "steps.claude-review-api.outputs.execution_file": "",
        "steps.api-limit.outputs.limited": "",
    }
    values.update(overrides)
    return values


@pytest.mark.parametrize(
    ("overrides", "classified"),
    [
        ({}, True),
        ({"steps.claude-review.outcome": "success"}, False),
        ({"inputs.force_api": "true"}, False),
        ({"inputs.claude_code_oauth_token": ""}, False),
    ],
)
def test_only_a_failed_oauth_attempt_is_classified_as_oauth(
    overrides: dict[str, Any], classified: bool
) -> None:
    step = composite_step("oauth-limit")
    assert bool(evaluate(step["if"], base_values(**overrides))) is classified


@pytest.mark.parametrize(
    ("overrides", "retried"),
    [
        ({"steps.oauth-limit.outputs.limited": "true"}, True),
        ({"steps.oauth-limit.outputs.limited": "false"}, False),
        (
            {
                "steps.oauth-limit.outputs.limited": "true",
                "inputs.anthropic_api_key": "",
            },
            False,
        ),
    ],
)
def test_the_retry_runs_only_on_an_oauth_limit_with_a_key(
    overrides: dict[str, Any], retried: bool
) -> None:
    step = composite_step("claude-review-api")
    assert bool(evaluate(step["if"], base_values(**overrides))) is retried


@pytest.mark.parametrize(
    ("overrides", "classified"),
    [
        # the retry failed
        ({"steps.claude-review-api.outcome": "failure"}, True),
        ({"steps.claude-review-api.outcome": "success"}, False),
        # no retry: the first attempt was the API path itself
        ({"inputs.force_api": "true"}, True),
        ({"inputs.claude_code_oauth_token": ""}, True),
        ({"inputs.force_api": "true", "steps.claude-review.outcome": "success"}, False),
        # no retry, and the first attempt was OAuth
        ({}, False),
    ],
)
def test_the_api_classifier_reads_the_attempt_that_used_the_key(
    overrides: dict[str, Any], classified: bool
) -> None:
    step = composite_step("api-limit")
    assert bool(evaluate(step["if"], base_values(**overrides))) is classified


def test_the_api_classifier_reads_the_retrys_log_when_there_was_one() -> None:
    env = composite_step("api-limit")["env"]["EXEC_FILE"]
    retried = {
        "steps.claude-review-api.outcome": "failure",
        "steps.claude-review-api.outputs.execution_file": "/tmp/r",
    }
    assert evaluate(env, base_values(**retried)) == "/tmp/r"
    # No retry: the first attempt was the API path, so its own log.
    assert evaluate(env, base_values()) == "/tmp/exec.json"


@pytest.mark.parametrize("output", ["execution_file", "structured_output"])
def test_a_retry_that_wrote_no_log_never_falls_back_to_the_oauth_attempt(
    output: str,
) -> None:
    """A retry that died before writing its log (setup error, bad key) must
    not be classified on the OAuth log, which carries the limit signal: with
    a prefix listed, that would exempt Claude for an unrelated key failure."""
    values = base_values(
        **{
            "steps.claude-review-api.outcome": "failure",
            "steps.claude-review-api.outputs.execution_file": "",
            "steps.claude-review-api.outputs.structured_output": "",
            "steps.claude-review.outputs.structured_output": "{}",
        }
    )
    assert evaluate(composite_step("api-limit")["env"]["EXEC_FILE"], values) == ""
    assert evaluate(action()["outputs"][output]["value"], values) == ""


def keep_log(tmp_path: Path, key_present: str) -> tuple[Path, Path]:
    temp = tmp_path / "runner-temp"
    temp.mkdir()
    shared = temp / "claude-execution-output.json"
    shared.write_text(limited_run())
    output = tmp_path / "out"
    result = subprocess.run(
        [
            "bash",
            "--noprofile",
            "--norc",
            "-e",
            "-o",
            "pipefail",
            "-c",
            composite_step("keep-oauth-log")["run"],
        ],
        env={
            "PATH": os.environ["PATH"],
            "RUNNER_TEMP": str(temp),
            "GITHUB_OUTPUT": str(output),
            "EXEC_FILE": str(shared),
            "API_KEY_PRESENT": key_present,
        },
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    kept = Path(
        dict(line.split("=", 1) for line in output.read_text().splitlines())["path"]
    )
    return shared, kept


def test_a_retry_cannot_inherit_the_oauth_log_at_the_shared_path(
    tmp_path: Path,
) -> None:
    shared, kept = keep_log(tmp_path, "true")
    assert kept.read_text() == limited_run()
    assert not shared.exists()


def test_without_a_retry_the_oauth_log_stays_for_the_error_net(
    tmp_path: Path,
) -> None:
    shared, kept = keep_log(tmp_path, "false")
    assert kept.read_text() == limited_run()
    assert shared.read_text() == limited_run()


def failure_code(tmp_path: Path, **env: str) -> str:
    step = composite_step("failure-code")
    output = tmp_path / "out"
    result = subprocess.run(
        ["bash", "--noprofile", "--norc", "-e", "-o", "pipefail", "-c", step["run"]],
        env={"PATH": os.environ["PATH"], "GITHUB_OUTPUT": str(output), **env},
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return dict(line.split("=", 1) for line in output.read_text().splitlines())[
        "failure_code"
    ]


@pytest.mark.parametrize(
    ("env", "code"),
    [
        # subscription out, no API path: the chain ends in exclusion
        ({"OAUTH_LIMITED": "true", "API_KEY_PRESENT": "false"}, QUOTA),
        # subscription out, the key retried and was also out
        (
            {"OAUTH_LIMITED": "true", "API_KEY_PRESENT": "true", "API_LIMITED": "true"},
            QUOTA,
        ),
        # the key path alone, out
        ({"API_LIMITED": "true", "API_KEY_PRESENT": "true"}, QUOTA),
        # subscription out, the key retried and failed some other way
        (
            {
                "OAUTH_LIMITED": "true",
                "API_KEY_PRESENT": "true",
                "API_LIMITED": "false",
            },
            "",
        ),
        ({"OAUTH_LIMITED": "true", "API_KEY_PRESENT": "true"}, ""),
        # anything else stays a failure
        ({}, ""),
        ({"OAUTH_LIMITED": "false", "API_KEY_PRESENT": "false"}, ""),
    ],
)
def test_the_failure_code_is_published_only_where_the_chain_ends(
    tmp_path: Path, env: dict[str, str], code: str
) -> None:
    assert failure_code(tmp_path, **env) == code


def test_the_composite_exports_the_code_and_the_last_attempts_results() -> None:
    outputs = action()["outputs"]
    assert outputs["failure_code"]["value"] == (
        "${{ steps.failure-code.outputs.failure_code }}"
    )
    retried = base_values(
        **{
            "steps.claude-review-api.outcome": "success",
            "steps.claude-review-api.outputs.execution_file": "/tmp/r",
            "steps.claude-review-api.outputs.structured_output": "{}",
        }
    )
    assert evaluate(outputs["execution_file"]["value"], retried) == "/tmp/r"
    assert evaluate(outputs["structured_output"]["value"], retried) == "{}"
    assert evaluate(outputs["inner_outcome"]["value"], retried) == "success"
    assert evaluate(outputs["inner_outcome"]["value"], base_values()) == "failure"


def test_the_oauth_log_is_kept_for_the_auth_switch() -> None:
    """The retry overwrites the action's execution file, so the OAuth
    attempt's log is copied first and is what the caller's auth switch
    scans: the limit is still persisted for the next run."""
    keep = composite_step("keep-oauth-log")
    assert keep["if"].strip() == "steps.oauth-limit.outputs.limited == 'true'"
    outputs = action()["outputs"]
    assert outputs["oauth_execution_file"]["value"] == (
        "${{ steps.keep-oauth-log.outputs.path }}"
    )
    steps = [s.get("id") for s in action()["runs"]["steps"]]
    assert steps.index("keep-oauth-log") < steps.index("claude-review-api")
    switch = single_step("Switch Claude auth on usage limit")
    assert switch["env"]["EXEC_FILE"] == (
        "${{ steps.claude-review.outputs.oauth_execution_file"
        " || steps.claude-review.outputs.execution_file }}"
    )


# --- the trusted path to the aggregate --------------------------------------


def single_step(name: str) -> dict[str, Any]:
    single = yaml.safe_load((WORKFLOWS / "base-ai-review-single.yml").read_text())
    return next(s for s in single["jobs"]["review"]["steps"] if s.get("name") == name)


PUBLISH = "Publish trusted Claude failure code"


def publish(tmp_path: Path, code: str, outcome: str = "success") -> dict[str, str]:
    step = single_step(PUBLISH)
    output = tmp_path / "published"
    output.write_text("")
    result = subprocess.run(
        ["bash", "--noprofile", "--norc", "-e", "-o", "pipefail", "-c", step["run"]],
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


def test_the_quota_code_is_published() -> None:
    step = single_step(PUBLISH)
    assert step["id"] == "claude-failure"
    assert step["if"] == "inputs.reviewer == 'claude' && always()"
    assert (
        step["env"]["FAILURE_CODE"] == "${{ steps.claude-review.outputs.failure_code }}"
    )


def test_publish_passes_the_quota_code(tmp_path: Path) -> None:
    assert publish(tmp_path, QUOTA) == {"code": QUOTA}


@pytest.mark.parametrize(
    "code", ["", "INCLUDED_QUOTA_UNAVAILABLE", f"{QUOTA} ", "anything", f"{QUOTA}\nx=1"]
)
def test_publish_drops_anything_else(tmp_path: Path, code: str) -> None:
    assert publish(tmp_path, code) == {}


def test_publish_ignores_a_review_step_that_did_not_succeed(tmp_path: Path) -> None:
    assert publish(tmp_path, QUOTA, outcome="failure") == {}


def test_the_code_leaves_the_job_and_the_reusable_workflow() -> None:
    single = yaml.safe_load((WORKFLOWS / "base-ai-review-single.yml").read_text())
    assert single["jobs"]["review"]["outputs"]["claude_failure"] == (
        "${{ steps.claude-failure.outputs.code }}"
    )
    assert single[True]["workflow_call"]["outputs"]["claude_failure"]["value"] == (
        "${{ jobs.review.outputs.claude_failure }}"
    )


def test_the_orchestrator_forwards_it_by_claude_slot_only() -> None:
    orchestrator = yaml.safe_load(
        (WORKFLOWS / "base-ai-review-orchestrator.yml").read_text()
    )
    expression = orchestrator["jobs"]["aggregate"]["with"]["claude_failure"]
    copilot = orchestrator["jobs"]["aggregate"]["with"]["copilot_failure"]
    assert expression == copilot.replace("copilot", "claude")


def test_the_aggregate_reads_it_from_its_input_only() -> None:
    workflow = yaml.safe_load((WORKFLOWS / "base-ai-review-aggregate.yml").read_text())
    declared = workflow[True]["workflow_call"]["inputs"]["claude_failure"]
    assert declared["type"] == "string" and declared["default"] == ""
    run = next(
        s for s in workflow["jobs"]["aggregate"]["steps"] if s.get("id") == "aggregate"
    )
    assert run["env"]["REVIEWER_FAILURE_CLAUDE"] == "${{ inputs.claude_failure }}"


def test_the_aggregate_lists_claude_on_the_trusted_code(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("REVIEWER_FAILURE_CLAUDE", QUOTA)
    monkeypatch.setattr(
        aggregate,
        "REVIEWERS",
        {n: f"review-{n}.json" for n in ("claude", "codex", "gemini", "copilot")},
    )
    available = {n: {} for n in ("codex", "gemini", "copilot")}
    assert aggregate._quota_unavailable(available) == ["claude"]
    monkeypatch.setenv("REVIEWER_FAILURE_CLAUDE", "review_failed")
    assert aggregate._quota_unavailable(available) == []


# The exemption is trust-sensitive: with no API key, a positive OAuth reading
# takes Claude out of the required roster. So it rests on the structured
# signal and an SDK prefix only, never on the legacy CLI signature, which
# detect-and-switch still accepts -- on error text, or on a whole file that
# is not a JSON message list, where reviewed content can spell it.


@pytest.mark.parametrize(
    "text",
    [
        "Claude AI usage limit reached|1800003600",
        "diff --git a/x b/x\n+Claude AI usage limit reached|1800003600\n",
        exec_text(
            {
                "type": "result",
                "is_error": True,
                "result": "Claude AI usage limit reached|1800003600",
            }
        ),
        exec_text({"type": "result", "is_error": True, "result": LIMIT_TEXT}),
    ],
)
def test_the_oauth_exemption_needs_the_structured_signal(text: str) -> None:
    assert sca.attempt_limited(text, "oauth") is False


def test_a_log_that_could_not_be_kept_is_still_moved_out_of_the_retrys_way(
    tmp_path: Path,
) -> None:
    """The copy guard refuses a symlinked log, so nothing is kept; with a
    retry coming, the link must still leave the shared path, or the retry
    could be read on the OAuth log's signal."""
    temp = tmp_path / "runner-temp"
    temp.mkdir()
    target = tmp_path / "elsewhere.json"
    target.write_text(limited_run())
    shared = temp / "claude-execution-output.json"
    shared.symlink_to(target)
    output = tmp_path / "out"
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
            composite_step("keep-oauth-log")["run"],
        ],
        env={
            "PATH": os.environ["PATH"],
            "RUNNER_TEMP": str(temp),
            "GITHUB_OUTPUT": str(output),
            "EXEC_FILE": str(shared),
            "API_KEY_PRESENT": "true",
        },
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert output.read_text() == ""
    assert not shared.is_symlink() and not shared.exists()
    # Only the link went: what it pointed at is untouched.
    assert target.read_text() == limited_run()
