"""The Claude reviewer's time limit, raised for self-review only (AT-2876).

The 'Run Claude review' step in ``base-ai-review-single.yml`` was capped at a
literal 10 minutes. On this repository's PR #194 the step hit that cap in two
consecutive runs (38679319725 and 38819148513; the composite's inner step
logs "The action has timed out."), the second of which left no verdict file
and ended the round 2/3 with ``action_invocation_failed``. Every other
self-review run of the same day finished the step in 4 to 7.5 minutes, and no
consumer has reported the cap, so the fix is a pair of ``workflow_call``
inputs -- the step cap and the job cap above it -- that keep today's values
as their defaults and are raised only by ``self-review.yml``.

Two things must not rot, and they pull in opposite directions:

* self-review must keep passing a raised value (or #194's shape fails again);
* the base defaults must stay at the values consumers have always had, in
  both files that state them (single.yml declares them; the orchestrator
  repeats them because it forwards the inputs unconditionally).

A third thing follows from the first two: the job cap must clear the
running reviewer's step cap with headroom for the steps around it, or the
runner kills the job before the step's own failure path (error verdict
file, execution-log fallback) can run -- #194's
``action_invocation_failed`` shape, reachable by any caller that raises
only one of the two inputs. self-review's pair is pinned here, but a
caller's ``with:`` block is out of any test's reach, so single.yml's review
job carries a guard step, ahead of any checkout, that refuses the
misconfiguration at runtime for every reviewer. The guard is a shell
script, so the tests run it (the way ``test_filter_pr_diff`` runs its
step) instead of re-stating its comparison, and feed it strings so a
fractional value reaches it the way a caller's would.

Both are pure YAML, so there is no production code to import; the tests
read the workflow files the way the other workflow-pinning modules do.

Test-module hygiene: all imports belong at the top of this file per PEP 8 --
do not add ``import foo`` statements inside test function bodies.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

_WORKFLOWS = Path(__file__).resolve().parents[2] / "workflows"
_EXAMPLES = Path(__file__).resolve().parents[3] / "examples"

_STEP_INPUT = "claude-timeout-minutes"
# Named for the job, not the reviewer: single.yml's job is reviewer-agnostic
# and the input caps whichever reviewer runs there. The orchestrator forwards
# it to the claude jobs only (pinned below), which its description states.
_JOB_INPUT = "review-job-timeout-minutes"

# The values every consumer has always had: the literal 10 the claude step
# carried before AT-2876 and the literal 15 on the job above it.
_CONSUMER_STEP_MINUTES = 10
_CONSUMER_JOB_MINUTES = 15

# Gemini keeps its original literal cap. Codex now has a bounded
# two-attempt budget (AT-3117). It coincides with the claude default today, but it is a
# separate value: moving the claude default must not move this.
_OTHER_REVIEWER_STEPS = ("Run Gemini review",)
_OTHER_REVIEWER_STEP_MINUTES = 10
_CODEX_STEP_MINUTES = 21
_CODEX_JOB_MINUTES = 26
_JOB_EXPRESSION = (
    "${{ (inputs.reviewer == 'codex' && inputs.review-job-timeout-minutes < %d) && %d || inputs.review-job-timeout-minutes }}"
    % (_CODEX_JOB_MINUTES, _CODEX_JOB_MINUTES)
)

# The fail-fast guard in single.yml's review job: runs for every reviewer
# before any checkout, picks the running reviewer's step cap (the claude
# input, or the other reviewers' literal carried as an env constant), and
# exits 1 unless both values are whole minutes and the job cap clears the
# step cap by the guard's own headroom.
_GUARD_STEP = "Validate timeout inputs"
_GUARD_REVIEWER_ENV = "REVIEWER"
_GUARD_JOB_ENV = "JOB_MINUTES"
_GUARD_CLAUDE_STEP_ENV = "CLAUDE_STEP_MINUTES"
_GUARD_OTHER_STEP_ENV = "OTHER_STEP_MINUTES"
_GUARD_CODEX_STEP_ENV = "CODEX_STEP_MINUTES"
_GUARD_HEADROOM_ENV = "MIN_HEADROOM_MINUTES"
# The guard's MIN_HEADROOM_MINUTES, a value of its own: the defaults 10/15
# happen to encode the same gap (pinned once, explicitly, below), but moving
# a default must not move what the guard requires, and the other way round.
_MIN_JOB_HEADROOM_MINUTES = 5

_CLAUDE_JOBS = ("review-claude-p", "review-claude-s")


def _load(name: str) -> dict[Any, Any]:
    return dict(yaml.safe_load((_WORKFLOWS / name).read_text(encoding="utf-8")))


def _call_inputs(workflow: dict[Any, Any]) -> dict[str, Any]:
    # PyYAML reads the bare ``on:`` key as the boolean True.
    return dict(workflow[True]["workflow_call"]["inputs"])


def _single_step_list() -> list[dict[str, Any]]:
    return list(_load("base-ai-review-single.yml")["jobs"]["review"]["steps"])


def _single_steps() -> dict[str, dict[str, Any]]:
    return {
        str(step["name"]): dict(step) for step in _single_step_list() if "name" in step
    }


def _self_review_with() -> dict[str, Any]:
    return dict(_load("self-review.yml")["jobs"]["review"]["with"])


def _run_guard(
    reviewer: str, claude_step_minutes: str, job_minutes: str
) -> subprocess.CompletedProcess[str]:
    """Run the guard step's script as the runner would: ``bash -e`` with the
    step's own env block, its three input expressions replaced by the given
    values and its literal constants (the other reviewers' step cap, the
    headroom) left as the workflow states them. Values are strings so a
    fractional one reaches the script the way a caller's would."""
    env = {
        key: str(value) for key, value in _single_steps()[_GUARD_STEP]["env"].items()
    }
    env.update(
        {
            _GUARD_REVIEWER_ENV: reviewer,
            _GUARD_CLAUDE_STEP_ENV: claude_step_minutes,
            _GUARD_JOB_ENV: job_minutes,
        }
    )
    return subprocess.run(
        ["bash", "-e", "-c", _single_steps()[_GUARD_STEP]["run"]],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )


# ------------------------------------------------------------ self-review


def test_self_review_raises_the_claude_step_cap_above_the_consumer_default() -> None:
    passed = _self_review_with()
    assert _STEP_INPUT in passed, "self-review no longer raises the Claude cap"
    assert int(passed[_STEP_INPUT]) > _CONSUMER_STEP_MINUTES


def test_self_review_keeps_the_job_cap_above_its_own_step_cap() -> None:
    """A job cap without headroom over the step cap would kill the job first,
    and the step's own failure path (error verdict, execution-log fallback)
    with it."""
    passed = _self_review_with()
    assert _JOB_INPUT in passed, "self-review raised the step cap but not the job cap"
    assert (
        int(passed[_JOB_INPUT]) >= int(passed[_STEP_INPUT]) + _MIN_JOB_HEADROOM_MINUTES
    )


# ------------------------------------------------------- runtime guard


def test_timeout_guard_runs_before_any_checkout() -> None:
    """Fail fast: a misconfigured caller learns before a runner does any
    work. Only the reviewer-name check, which the guard's reviewer branch
    relies on, may precede it."""
    names = [step.get("name") for step in _single_step_list()]
    first_uses = next(i for i, step in enumerate(_single_step_list()) if "uses" in step)
    assert names.index(_GUARD_STEP) < first_uses
    assert names[: names.index(_GUARD_STEP)] == ["Validate reviewer input"]


def test_timeout_guard_runs_for_every_reviewer() -> None:
    """The job cap applies to the job whatever reviewer runs in it, and
    single.yml is a workflow_call entry point of its own, so a direct caller
    can pair a low job cap with the gemini/codex literal step cap too."""
    assert "if" not in _single_steps()[_GUARD_STEP]


def test_timeout_guard_reads_the_inputs_and_the_literals_it_compares() -> None:
    """The script compares what the env keys carry: the three inputs must be
    the inputs, the other reviewers' cap must be the literal their steps
    carry, and the headroom must be the guard's own value -- otherwise the
    executed tests below would prove nothing about the workflow. The block
    must hold exactly these keys: an additional ``${{ }}``-valued key that
    ``_run_guard`` does not substitute would reach the script as literal
    expression text, and every executed test would keep passing on it."""
    env = dict(_single_steps()[_GUARD_STEP]["env"])
    assert env.keys() == {
        _GUARD_REVIEWER_ENV,
        _GUARD_JOB_ENV,
        _GUARD_CLAUDE_STEP_ENV,
        _GUARD_OTHER_STEP_ENV,
        _GUARD_CODEX_STEP_ENV,
        _GUARD_HEADROOM_ENV,
    }, sorted(env)
    assert env[_GUARD_REVIEWER_ENV] == "${{ inputs.reviewer }}"
    assert env[_GUARD_JOB_ENV] == _JOB_EXPRESSION
    assert env[_GUARD_CLAUDE_STEP_ENV] == "${{ inputs.%s }}" % _STEP_INPUT
    assert env[_GUARD_OTHER_STEP_ENV] == _OTHER_REVIEWER_STEP_MINUTES
    assert env[_GUARD_CODEX_STEP_ENV] == _CODEX_STEP_MINUTES
    assert env[_GUARD_HEADROOM_ENV] == _MIN_JOB_HEADROOM_MINUTES


@pytest.mark.parametrize(
    ("reviewer", "claude_step_minutes", "job_minutes", "named_cap"),
    [
        pytest.param(
            "claude",
            "20",
            "15",
            _STEP_INPUT,
            id="claude step raised above the default job cap",
        ),
        pytest.param("claude", "10", "10", _STEP_INPUT, id="claude equal caps"),
        pytest.param(
            "claude", "20", "21", _STEP_INPUT, id="claude job cap only 1 minute over"
        ),
        pytest.param(
            "claude",
            "10",
            "14",
            _STEP_INPUT,
            id="claude headroom one short of the default gap",
        ),
        pytest.param(
            "gemini",
            "10",
            "8",
            "the gemini step cap",
            id="gemini job cap below the literal step cap",
        ),
        pytest.param(
            "codex",
            "10",
            "14",
            "the codex step cap",
            id="codex headroom one short of the default gap",
        ),
    ],
)
def test_timeout_guard_refuses_a_job_cap_without_headroom(
    reviewer: str, claude_step_minutes: str, job_minutes: str, named_cap: str
) -> None:
    result = _run_guard(reviewer, claude_step_minutes, job_minutes)
    assert result.returncode == 1
    assert result.stdout.startswith("::error::"), result.stdout
    assert f"{_JOB_INPUT} ({job_minutes}) must exceed {named_cap} (" in result.stdout
    assert f"by at least {_MIN_JOB_HEADROOM_MINUTES} minutes" in result.stdout


@pytest.mark.parametrize(
    ("reviewer", "claude_step_minutes", "job_minutes", "offender"),
    [
        pytest.param("claude", "10.5", "25", _STEP_INPUT, id="fractional step cap"),
        pytest.param("claude", "10", "15.5", _JOB_INPUT, id="fractional job cap"),
        pytest.param("claude", "ten", "15", _STEP_INPUT, id="not a number"),
        pytest.param("claude", "0", "5", _STEP_INPUT, id="zero step cap"),
        pytest.param(
            "claude", "08", "25", _STEP_INPUT, id="leading zero, invalid octal"
        ),
        pytest.param(
            "claude", "010", "14", _STEP_INPUT, id="leading zero, octal 8 would pass"
        ),
        pytest.param(
            "claude", "10", "015", _JOB_INPUT, id="leading zero on the job cap"
        ),
        pytest.param(
            "gemini", "10", "0", _JOB_INPUT, id="zero job cap, other reviewer"
        ),
    ],
)
def test_timeout_guard_refuses_a_value_that_is_not_whole_minutes(
    reviewer: str, claude_step_minutes: str, job_minutes: str, offender: str
) -> None:
    """Only a positive canonical integer may reach the arithmetic. ``[ -lt ]``
    on a fractional value exits 2 with a complaint on stderr, which an ``if``
    reads as false; ``$(( ))`` reads a leading zero as octal, so ``010`` is 8
    (and ``08`` aborts the script with no ``::error::``); and a zero cap is
    the killed-step shape itself. Without the shape check the guard passed
    every one of these. The message names only the input the caller has to
    fix: for gemini/codex the step cap is a literal and never the cause."""
    result = _run_guard(reviewer, claude_step_minutes, job_minutes)
    assert result.returncode == 1
    assert result.stderr == ""
    assert result.stdout.startswith("::error::"), result.stdout
    assert "must be whole minutes of at least 1, without leading zeros" in result.stdout
    offending_value = job_minutes if offender == _JOB_INPUT else claude_step_minutes
    assert f"::error::{offender} ({offending_value}) must" in result.stdout
    other = _STEP_INPUT if offender == _JOB_INPUT else _JOB_INPUT
    assert other not in result.stdout


def test_timeout_guard_passes_every_configuration_that_actually_runs() -> None:
    """Positive control: the consumer defaults single.yml declares, the pair
    self-review passes, and the defaults as the gemini and codex jobs
    receive them from the orchestrator."""
    defaults = _call_inputs(_load("base-ai-review-single.yml"))
    default_step = str(defaults[_STEP_INPUT]["default"])
    default_job = str(defaults[_JOB_INPUT]["default"])
    raised = _self_review_with()
    for reviewer, claude_step_minutes, job_minutes in (
        ("claude", default_step, default_job),
        ("claude", str(raised[_STEP_INPUT]), str(raised[_JOB_INPUT])),
        ("gemini", default_step, default_job),
        ("codex", default_step, str(_CODEX_JOB_MINUTES)),
    ):
        result = _run_guard(reviewer, claude_step_minutes, job_minutes)
        assert result.returncode == 0, (
            reviewer,
            claude_step_minutes,
            job_minutes,
            result.stdout,
        )
        assert result.stdout == ""
        assert result.stderr == ""


# ---------------------------------------------------------- base defaults


def test_single_defaults_are_the_values_consumers_always_had() -> None:
    inputs = _call_inputs(_load("base-ai-review-single.yml"))
    assert inputs[_STEP_INPUT]["default"] == _CONSUMER_STEP_MINUTES
    assert inputs[_JOB_INPUT]["default"] == _CONSUMER_JOB_MINUTES
    assert inputs[_STEP_INPUT]["type"] == "number"
    assert inputs[_JOB_INPUT]["type"] == "number"
    assert not inputs[_STEP_INPUT].get("required", False)
    assert not inputs[_JOB_INPUT].get("required", False)
    # The defaults clear the guard by exactly its headroom: a consumer that
    # sets neither input must keep passing it, so a default that moves alone
    # has to move the other with it.
    assert _CONSUMER_JOB_MINUTES - _CONSUMER_STEP_MINUTES == _MIN_JOB_HEADROOM_MINUTES


def test_orchestrator_defaults_match_single_defaults() -> None:
    """The orchestrator forwards the inputs unconditionally, so its defaults
    are what a consumer actually gets; they must be single.yml's own."""
    single = _call_inputs(_load("base-ai-review-single.yml"))
    orchestrator = _call_inputs(_load("base-ai-review-orchestrator.yml"))
    for name in (_STEP_INPUT, _JOB_INPUT):
        assert orchestrator[name]["default"] == single[name]["default"], name
        assert orchestrator[name]["type"] == single[name]["type"], name
        assert not orchestrator[name].get("required", False), name


# ---------------------------------------------------------------- wiring


def test_claude_step_and_job_caps_read_the_inputs() -> None:
    """The literal caps are gone: both keys are the input expressions, so a
    caller's value is what the runner enforces."""
    review_job = _load("base-ai-review-single.yml")["jobs"]["review"]
    assert review_job["timeout-minutes"] == _JOB_EXPRESSION
    claude = _single_steps()["Run Claude review"]
    assert claude["timeout-minutes"] == "${{ inputs.%s }}" % _STEP_INPUT


def test_other_reviewer_step_caps_are_unchanged() -> None:
    steps = _single_steps()
    for name in _OTHER_REVIEWER_STEPS:
        assert steps[name]["timeout-minutes"] == _OTHER_REVIEWER_STEP_MINUTES, name


def test_orchestrator_forwards_step_to_claude_and_job_to_claude_and_codex() -> None:
    jobs = _load("base-ai-review-orchestrator.yml")["jobs"]
    # Positive control: a renamed or dropped claude job would otherwise send
    # every job down the else branch and the forwarding would go unchecked.
    assert set(_CLAUDE_JOBS) <= jobs.keys(), sorted(jobs)
    for job_name, job in jobs.items():
        passed = dict(job.get("with") or {})
        if job_name in _CLAUDE_JOBS:
            assert passed.get(_STEP_INPUT) == "${{ inputs.%s }}" % _STEP_INPUT, job_name
            assert passed.get(_JOB_INPUT) == "${{ inputs.%s }}" % _JOB_INPUT, job_name
        elif job_name in {"review-codex-p", "review-codex-s"}:
            assert _STEP_INPUT not in passed, job_name
            assert passed.get(_JOB_INPUT) == "${{ inputs.%s }}" % _JOB_INPUT, job_name
        else:
            assert _STEP_INPUT not in passed, job_name
            assert _JOB_INPUT not in passed, job_name


def test_consumer_thin_trigger_example_leaves_the_caps_at_their_defaults() -> None:
    """The example consumers copy must not carry the raised value: the raise
    is this repository's, not the fleet's."""
    example = yaml.safe_load(
        (_EXAMPLES / "consumer-thin-trigger.yml").read_text(encoding="utf-8")
    )
    passed = dict(example["jobs"]["review"]["with"])
    assert _STEP_INPUT not in passed
    assert _JOB_INPUT not in passed
