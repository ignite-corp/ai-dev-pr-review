"""Every self-review skip path still reports `Aggregate & Verdict` (AT-3119).

`review / aggregate / Aggregate & Verdict` is the context the AT-3114 rollout
makes required on this repository. It is composed from three names: the
caller job id in self-review.yml (`review`), the orchestrator's job id
(`aggregate`), and the aggregate workflow's job `name:`. It exists only when
all three jobs run: a skipped caller job reports the one-part `review`, and
AT-1967 measured a skipped orchestrator job reporting the two-part name. An
absent required check leaves the PR pending forever (the AT-2412 class).

Before AT-3119 self-review skipped the `review` job itself on two paths -- a
release pin bump whose tag does not exist yet (`guard`), and a fork PR -- so
#248 carried only a skipped `review` check. The skip now happens inside the
reusable workflows: `review` always runs, hands the reason down as the
orchestrator's opt-in `skip_reason` input, `prepare` and the reviewers do not
run, and the aggregate job runs only the step that reports the reason and
passes, plus the two target steps a cross-repository run still needs (see
`_UNGUARDED`).

Skip paths enumerated in self-review.yml, the orchestrator and the aggregate:
  1. release pin bump: the guard step finds no tag for the scripts pin
  2. fork PR: the head repository is not this repository (no secrets);
     read from the event in the `review` job, so it holds when guard failed
  3. guard failed on a same-repo PR: no reason is set, so the full review
     runs and the aggregate reports whatever happened (`always()`)
  4. prepare's own size / policy skips: the aggregate already runs on them
     (`always()`, AT-1975 / AT-2206); unchanged here
  5. a cancelled run: superseded by the run on the newer head, whose
     aggregate reports; unchanged here
What these tests can assert is the YAML and the shell the steps run. Whether
GitHub then attaches the check to the head SHA is only observable in a run.
"""

from __future__ import annotations

import os
import re
import shutil
import stat
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml
from pr_reachable_checks import job_context

_WORKFLOWS = Path(__file__).resolve().parents[2] / "workflows"
_SELF = _WORKFLOWS / "self-review.yml"
_ORCHESTRATOR = _WORKFLOWS / "base-ai-review-orchestrator.yml"
_AGGREGATE = _WORKFLOWS / "base-ai-review-aggregate.yml"
_PREPARE = _WORKFLOWS / "base-ai-review-prepare.yml"

_REQUIRED_CONTEXT = "review / aggregate / Aggregate & Verdict"
_REPORT_STEP = "Report a skip by design"
_GUARD_STEP = "pin"
_SKIP_GUARD = "inputs.skip_reason == ''"

# Aggregate steps that carry no skip guard of their own, each with the reason
# it does nothing on a skip:
#   - the resolve and target-mint steps are held byte-identical to their
#     copies in prepare and single (test_target_repo_threading.py); resolve
#     is shell over inputs, and the mint runs only on a cross run. On a
#     cross skip both still run, so an invalid cross call fails as it would
#     without a skip.
#   - the reviewer mint runs only when `Check reviewer App key present`,
#     which is guarded, reported a key.
_UNGUARDED = {
    "Resolve review target",
    "Mint target App token",
    "Mint reviewer App token",
}

_SELF_REPO = "ignite-corp/ai-dev-pr-review"
_PLACEHOLDER = "unused-review-skipped"


def _load(path: Path) -> dict[Any, Any]:
    doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(doc, dict), path.name
    return doc


def _call_spec(path: Path) -> dict[str, Any]:
    on = _load(path).get(True)
    assert isinstance(on, dict), path.name
    return dict(on["workflow_call"])


# The orchestrator secrets whose absence fails the call itself; a fork PR has
# none of them. Read from the spec, so a new required secret is covered.
_REQUIRED_SECRETS = tuple(
    name
    for name, spec in _call_spec(_ORCHESTRATOR)["secrets"].items()
    if spec.get("required")
)

_QUOTED = re.compile(r"('(?:[^']|'')*')")
_OPERAND = re.compile(r"[A-Za-z_][\w-]*(?:\.[\w-]+)+")


def _evaluate(expression: str, state: dict[str, str]) -> str:
    """Evaluate a `${{ }}` value expression the way the runner does.

    Only the subset the `review` job uses: `&&`, `||`, parentheses, `==` /
    `!=`, single-quoted literals and context operands. GitHub's `&&` / `||`
    return an operand and treat '' as false, as Python's `and` / `or` do, at
    the same precedence. Every operand must be in ``state``: one the test
    does not model is a site it has not reasoned about, not an empty string.
    """
    body = expression.strip()
    assert body.startswith("${{") and body.endswith("}}"), expression
    parts = _QUOTED.split(body[3:-2])
    for i in range(0, len(parts), 2):
        code = parts[i].replace("&&", " and ").replace("||", " or ")
        parts[i] = _OPERAND.sub(lambda m: f"S[{m.group(0)!r}]", code)
    return str(eval("".join(parts), {"__builtins__": {}}, {"S": state}))


def _steps(path: Path, job: str) -> list[dict[str, Any]]:
    return list(_load(path)["jobs"][job]["steps"])


def _conjuncts(condition: object) -> list[str]:
    """The top-level `&&` terms of an `if:`, so a disjunction cannot pass.

    A clause found anywhere in the text would accept `a == '' || b`, which
    still runs whenever `b` holds.
    """
    body = str(condition).strip().removeprefix("${{").removesuffix("}}")
    return [part.strip() for part in body.split("&&")]


def _step_name(step: dict[str, Any]) -> str:
    return str(step.get("name") or step.get("uses") or step.get("id"))


def _step(path: Path, job: str, key: str) -> dict[str, Any]:
    for step in _steps(path, job):
        if key in (step.get("name"), step.get("id")):
            return step
    raise AssertionError(f"{path.name}: no step {key!r} in job {job!r}")


def _run_step(
    step: dict[str, Any], cwd: Path, env: dict[str, str]
) -> tuple[subprocess.CompletedProcess[str], dict[str, str]]:
    """Run a step's shell under bash -e as the runner does; return its outputs."""
    out = cwd / "github_output"
    out.write_text("", encoding="utf-8")
    full = {
        "PATH": os.environ["PATH"],
        "GITHUB_OUTPUT": str(out),
        "GITHUB_STEP_SUMMARY": str(cwd / "summary.md"),
        **env,
    }
    result = subprocess.run(
        ["bash", "-e", "-c", str(step["run"])],
        cwd=cwd,
        env=full,
        capture_output=True,
        text=True,
        check=False,
    )
    outputs = dict(
        line.split("=", 1)
        for line in out.read_text(encoding="utf-8").splitlines()
        if "=" in line
    )
    return result, outputs


def test_the_required_context_is_composed_from_the_three_jobs() -> None:
    """A rename at any level renames the context the ruleset will require."""
    self_jobs = _load(_SELF)["jobs"]
    orch_jobs = _load(_ORCHESTRATOR)["jobs"]
    agg_jobs = _load(_AGGREGATE)["jobs"]
    assert self_jobs["review"]["uses"] == "./.github/workflows/" + _ORCHESTRATOR.name
    assert orch_jobs["aggregate"]["uses"] == "./.github/workflows/" + _AGGREGATE.name
    composed = " / ".join(
        [
            job_context("review", self_jobs["review"]),
            job_context("aggregate", orch_jobs["aggregate"]),
            job_context("aggregate", agg_jobs["aggregate"]),
        ]
    )
    assert composed == _REQUIRED_CONTEXT


class TestSelfReviewNeverSkipsTheCallerJob:
    def test_review_runs_unless_the_run_is_cancelled(self) -> None:
        """The fork and guard conditions used to sit here and skip the job.

        `!cancelled()`, not the implicit success(): a failed guard sets no
        reason, so the full review runs and its aggregate reports the outcome
        rather than the check going absent behind a skipped caller job.
        """
        assert _load(_SELF)["jobs"]["review"]["if"] == "${{ !cancelled() }}"

    def test_guard_publishes_its_reason(self) -> None:
        assert (
            _load(_SELF)["jobs"]["guard"]["outputs"]["skip_reason"]
            == "${{ steps.pin.outputs.skip_reason }}"
        )

    @staticmethod
    def _state(event: str, head_repo: str, guard_reason: str) -> dict[str, str]:
        state = {
            "needs.guard.outputs.skip_reason": guard_reason,
            "github.event_name": event,
            "github.event.pull_request.head.repo.full_name": head_repo,
            "github.repository": _SELF_REPO,
        }
        for secret in _REQUIRED_SECRETS:
            state[f"secrets.{secret}"] = ""
        return state

    # name -> (event, head repo, guard's reason, whether the review is
    # skipped). A failed guard publishes no reason, the same state as a guard
    # that found the tag, so the fork rows hold either way.
    _PATHS = {
        "same-repo": ("pull_request", _SELF_REPO, "", False),
        "release-pin-bump": ("pull_request", _SELF_REPO, "release pin bump", True),
        "fork": ("pull_request", "someone/ai-dev-pr-review", "", True),
        "deleted-fork": ("pull_request", "", "", True),
        "fork-release": ("pull_request", "someone/x", "release pin bump", True),
        "dispatch": ("workflow_dispatch", "", "", False),
        # A trigger not declared today fails closed: skipped unless its head
        # is this repository.
        "other-event-fork": ("pull_request_target", "someone/x", "", True),
        "other-event-no-head": ("merge_group", "", "", True),
    }

    @pytest.mark.parametrize("path", sorted(_PATHS))
    def test_the_reason_reaches_the_orchestrator_on_every_path(self, path: str) -> None:
        event, head_repo, guard_reason, skipped = self._PATHS[path]
        review = _load(_SELF)["jobs"]["review"]
        state = self._state(event, head_repo, guard_reason)
        reason = _evaluate(review["with"]["skip_reason"], state)
        assert bool(reason) is skipped, path
        if guard_reason:
            assert reason == guard_reason
        elif skipped:
            assert "fork" in reason

    @pytest.mark.parametrize("path", sorted(_PATHS))
    @pytest.mark.parametrize("secret", _REQUIRED_SECRETS)
    def test_a_required_secret_is_never_empty_on_a_skip(
        self, secret: str, path: str
    ) -> None:
        """A fork PR reads every secret as empty; the call must still start.

        On a skip the value is never read, so a placeholder stands in; on a
        review the real secret passes unchanged.
        """
        event, head_repo, guard_reason, skipped = self._PATHS[path]
        state = self._state(event, head_repo, guard_reason)
        state[f"secrets.{secret}"] = "real"
        value = _load(_SELF)["jobs"]["review"]["secrets"][secret]
        assert _evaluate(value, state) == (_PLACEHOLDER if skipped else "real")


class TestGuardSetsTheReason:
    @pytest.fixture
    def workdir(self, tmp_path: Path) -> Path:
        (tmp_path / ".github" / "workflows").mkdir(parents=True)
        for workflow in (
            _PREPARE,
            _WORKFLOWS / "base-ai-review-single.yml",
            _AGGREGATE,
        ):
            shutil.copy(workflow, tmp_path / ".github" / "workflows" / workflow.name)
        return tmp_path

    def _fake_git(self, workdir: Path, exit_code: int) -> str:
        bindir = workdir / "bin"
        bindir.mkdir()
        git = bindir / "git"
        git.write_text(f"#!/bin/sh\nexit {exit_code}\n", encoding="utf-8")
        git.chmod(git.stat().st_mode | stat.S_IEXEC)
        return f"{bindir}{os.pathsep}{os.environ['PATH']}"

    def _guard(self, workdir: Path, *, tag_exists: bool) -> dict[str, str]:
        step = _step(_SELF, "guard", _GUARD_STEP)
        result, outputs = _run_step(
            step, workdir, {"PATH": self._fake_git(workdir, 0 if tag_exists else 2)}
        )
        assert result.returncode == 0, result.stderr
        return outputs

    def test_a_same_repo_pr_with_its_tag_is_reviewed(self, workdir: Path) -> None:
        outputs = self._guard(workdir, tag_exists=True)
        assert outputs.get("skip_reason", "") == ""

    def test_a_release_pin_bump_is_skipped_with_a_reason(self, workdir: Path) -> None:
        outputs = self._guard(workdir, tag_exists=False)
        assert "does not exist yet" in outputs["skip_reason"]


class TestOrchestratorSkipInput:
    def test_input_is_opt_in(self) -> None:
        spec = _call_spec(_ORCHESTRATOR)["inputs"]["skip_reason"]
        assert spec["required"] is False
        assert spec["default"] == ""
        assert spec["type"] == "string"

    def test_prepare_does_not_run_on_a_skip(self) -> None:
        """prepare checks out the scripts pin, which a release PR lacks."""
        assert _load(_ORCHESTRATOR)["jobs"]["prepare"]["if"] == _SKIP_GUARD

    def test_every_reviewer_job_is_skipped_with_prepare(self) -> None:
        """A skipped prepare alone does not skip the sequential reviewers.

        Their `!cancelled()` replaces the implicit success() that would carry
        a skipped dependency through, so each reviewer job must also require
        `needs.prepare.outputs.skip == 'false'`, which an unrun prepare
        leaves empty. Otherwise a reviewer runs on a skip with the
        placeholder as its API key.
        """
        jobs = _load(_ORCHESTRATOR)["jobs"]
        reviewers = [
            job_id
            for job_id, body in jobs.items()
            if str(body.get("uses", "")).endswith("base-ai-review-single.yml")
        ]
        assert reviewers
        # Closed: a new orchestrator job fails here until it is reasoned about.
        assert set(jobs) == {"prepare", "aggregate", *reviewers}
        for job_id in reviewers:
            body = jobs[job_id]
            needs = body["needs"]
            assert "prepare" in ([needs] if isinstance(needs, str) else needs)
            assert "needs.prepare.outputs.skip == 'false'" in _conjuncts(body["if"]), (
                job_id
            )

    def test_aggregate_still_runs_and_gets_the_reason(self) -> None:
        aggregate = _load(_ORCHESTRATOR)["jobs"]["aggregate"]
        assert aggregate["if"] == "always()"
        assert aggregate["with"]["skip_reason"] == "${{ inputs.skip_reason }}"


class TestAggregateShortCircuits:
    def test_input_is_opt_in(self) -> None:
        spec = _call_spec(_AGGREGATE)["inputs"]["skip_reason"]
        assert spec["required"] is False
        assert spec["default"] == ""
        assert spec["type"] == "string"

    def test_the_report_runs_only_on_a_skip(self) -> None:
        step = _step(_AGGREGATE, "aggregate", _REPORT_STEP)
        assert step["if"] == "inputs.skip_reason != ''"
        assert step["env"]["SKIP_REASON"] == "${{ inputs.skip_reason }}"

    def test_every_other_step_is_skipped_on_a_skip(self) -> None:
        """Anything left running could fail the check a skip must pass.

        The scripts checkout fails outright on a release PR (its tag does not
        exist yet), and the aggregate script would post a verdict for a review
        that never ran.
        """
        unguarded = [
            _step_name(step)
            for step in _steps(_AGGREGATE, "aggregate")
            if _step_name(step) not in _UNGUARDED | {_REPORT_STEP}
            and _SKIP_GUARD not in _conjuncts(step.get("if", ""))
        ]
        assert not unguarded

    def test_the_reviewer_mint_hangs_off_a_guarded_step(self) -> None:
        mint = _step(_AGGREGATE, "aggregate", "Mint reviewer App token")
        assert "steps.reviewer_key.outputs.present == 'true'" in _conjuncts(mint["if"])
        key = _step(_AGGREGATE, "aggregate", "reviewer_key")
        assert _SKIP_GUARD in _conjuncts(key["if"])

    def test_the_report_passes_and_says_why(self, tmp_path: Path) -> None:
        step = _step(_AGGREGATE, "aggregate", _REPORT_STEP)
        result, _ = _run_step(
            step, tmp_path, {"SKIP_REASON": "fork PR\n::error::injected"}
        )
        assert result.returncode == 0, result.stderr
        summary = (tmp_path / "summary.md").read_text(encoding="utf-8")
        assert "fork PR" in summary
        assert "skipped by design" in summary
        # The reason is caller text: a newline in it must not open a second
        # workflow-command line.
        assert not any(
            line.startswith("::error::") for line in result.stdout.splitlines()
        )
