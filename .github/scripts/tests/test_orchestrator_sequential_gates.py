"""Job-level gating of the orchestrator's sequential mode (AT-2125).

Before AT-2125 the two guards in `review-gemini-s` were asymmetric: a failed
codex job was tolerated (`result == 'failure' || early_exit != 'true'`) while
claude had to be `success`, and `review-codex-s` carried an implicit
success() that skipped it on any claude failure. One dead reviewer therefore
took the reviewers after it down with it -- in the claude direction only.

The `if:` expressions cannot be executed here, so the tests below translate
the small GitHub-expression subset they use into Python and evaluate them
against every `needs.<job>.result` / `outputs.early_exit` combination that
matters. A failed, skipped or cancelled job exposes empty outputs; a job that
finished exposes whatever its extract step wrote.

Test-module hygiene: all imports belong at the top of this file per PEP 8 --
do not add `import foo` statements inside test function bodies.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
import yaml

_ORCHESTRATOR = (
    Path(__file__).resolve().parents[2] / "workflows" / "base-ai-review-orchestrator.yml"
)

_PARALLEL_CONDITION = "vars.REVIEW_MODE != 'sequential' && needs.prepare.outputs.skip == 'false'"
_SEQUENTIAL_HEAD_CONDITION = (
    "vars.REVIEW_MODE == 'sequential' && needs.prepare.outputs.skip == 'false'"
)


@dataclass(frozen=True)
class Job:
    """What `needs.<job>` exposes: the result, and the early_exit output."""

    result: str
    early_exit: str = ""


# A job that did not finish exposes no outputs at all.
FAILED = Job("failure")
SKIPPED = Job("skipped")
CANCELLED = Job("cancelled")
OK = Job("success", "false")
EARLY_EXIT = Job("success", "true")


def _jobs() -> dict[str, Any]:
    return dict(yaml.safe_load(_ORCHESTRATOR.read_text(encoding="utf-8"))["jobs"])


def _condition(job: str) -> str:
    return str(_jobs()[job]["if"])


def _evaluate(
    condition: str,
    *,
    cancelled: bool = False,
    mode: str = "sequential",
    prepare_skip: str = "false",
    needs: dict[str, Job] | None = None,
) -> bool:
    """Evaluate a job `if:` under the given run state.

    Supports exactly the subset the sequential jobs use: `${{ }}`, `!cancelled()`,
    `&&`, `||`, parentheses, `==` / `!=` against single-quoted literals, and the
    `vars.REVIEW_MODE`, `needs.prepare.outputs.skip`, `needs.<job>.result`,
    `needs.<job>.outputs.early_exit` operands. Anything else fails loudly rather
    than evaluating to something plausible.
    """
    jobs = needs or {}
    expr = condition.strip()
    if expr.startswith("${{") and expr.endswith("}}"):
        expr = expr[3:-2]
    expr = expr.replace("!cancelled()", " (not cancelled) ")
    expr = expr.replace("&&", " and ").replace("||", " or ")
    expr = expr.replace("vars.REVIEW_MODE", " mode ")
    expr = expr.replace("needs.prepare.outputs.skip", " prepare_skip ")
    expr = re.sub(r"needs\.([\w-]+)\.result", r' jobs["\1"].result ', expr)
    expr = re.sub(r"needs\.([\w-]+)\.outputs\.early_exit", r' jobs["\1"].early_exit ', expr)
    leftovers = re.findall(r"needs\.|vars\.|!(?!=)|always\(\)|success\(\)|failure\(\)", expr)
    assert not leftovers, f"unsupported token(s) {leftovers} in {condition!r}"
    return bool(
        eval(  # controlled input: the repo's own workflow file
            expr,
            {"__builtins__": {}},
            {
                "cancelled": cancelled,
                "mode": mode,
                "prepare_skip": prepare_skip,
                "jobs": jobs,
            },
        )
    )


class TestEvaluatorSanity:
    """The translator must reject what it does not model."""

    def test_rejects_always(self) -> None:
        with pytest.raises(AssertionError):
            _evaluate("always() && vars.REVIEW_MODE == 'sequential'")

    def test_rejects_unknown_needs_field(self) -> None:
        with pytest.raises(AssertionError):
            _evaluate("needs.review-claude-s.outputs.other != 'x'")

    def test_implicit_success_is_not_modelled(self) -> None:
        """A condition without a status function has an implicit success()."""
        condition = _condition("review-codex-s")
        assert "cancelled()" in condition


class TestParallelModeUntouched:
    """AT-2125 is confined to the `vars.REVIEW_MODE == 'sequential'` jobs."""

    @pytest.mark.parametrize("job", ["review-gemini-p", "review-codex-p", "review-claude-p"])
    def test_parallel_job_condition_is_unchanged(self, job: str) -> None:
        assert _condition(job) == _PARALLEL_CONDITION

    @pytest.mark.parametrize("job", ["review-gemini-p", "review-codex-p", "review-claude-p"])
    def test_parallel_job_needs_only_prepare(self, job: str) -> None:
        assert _jobs()[job]["needs"] == "prepare"

    def test_sequential_head_condition_is_unchanged(self) -> None:
        assert _condition("review-claude-s") == _SEQUENTIAL_HEAD_CONDITION


class TestSequentialGatesAreSymmetric:
    @pytest.mark.parametrize("job", ["review-codex-s", "review-gemini-s"])
    def test_no_reviewer_is_required_to_succeed(self, job: str) -> None:
        """The defect: `needs.review-claude-s.result == 'success'`."""
        assert "== 'success'" not in _condition(job)

    @pytest.mark.parametrize("job", ["review-codex-s", "review-gemini-s"])
    def test_downstream_job_suppresses_the_implicit_success_check(self, job: str) -> None:
        """Without a status function a failed upstream job skips this one."""
        condition = _condition(job)
        assert "!cancelled()" in condition
        assert "always()" not in condition, "always() lets a cancelled run start new jobs"

    @pytest.mark.parametrize("job", ["review-codex-s", "review-gemini-s"])
    def test_negated_status_function_is_wrapped_in_expression_braces(self, job: str) -> None:
        """A bare leading `!` is a YAML tag indicator (AT-2092)."""
        condition = _condition(job)
        assert condition.startswith("${{") and condition.endswith("}}")

    def test_gemini_has_one_clause_per_upstream_reviewer_in_the_same_shape(self) -> None:
        condition = _condition("review-gemini-s")
        for reviewer in ("claude", "codex"):
            clause = (
                f"(needs.review-{reviewer}-s.result == 'failure' || "
                f"needs.review-{reviewer}-s.outputs.early_exit != 'true')"
            )
            assert clause in condition, reviewer

    def test_credential_outage_caveat_is_documented_at_the_guard(self) -> None:
        """A dead-on-credentials reviewer reports success with no artifact.

        Run 33650133793 measured it. The comment exists so nobody reads the
        failure clause as protection against that family; deleting it is
        what this test refuses.
        """
        text = _ORCHESTRATOR.read_text(encoding="utf-8")
        assert "33650133793" in text
        assert "no verdict artifact" in text


class TestCodexGate:
    """`review-codex-s` against every `review-claude-s` outcome."""

    def _runs(self, claude: Job, **state: Any) -> bool:
        return _evaluate(_condition("review-codex-s"), needs={"review-claude-s": claude}, **state)

    def test_runs_after_a_normal_claude_review(self) -> None:
        assert self._runs(OK)

    def test_skipped_when_claude_requested_early_exit(self) -> None:
        assert not self._runs(EARLY_EXIT)

    def test_runs_when_claude_failed(self) -> None:
        """The AT-2125 change: a claude failure no longer skips codex."""
        assert self._runs(FAILED)

    def test_runs_when_claude_failed_after_writing_a_verdict(self) -> None:
        assert self._runs(Job("failure", "false"))

    def test_skipped_when_prepare_skipped_the_pr(self) -> None:
        assert not self._runs(SKIPPED, prepare_skip="true")

    def test_skipped_when_prepare_failed(self) -> None:
        """A failed prepare exposes no `skip` output at all."""
        assert not self._runs(SKIPPED, prepare_skip="")

    def test_skipped_in_parallel_mode(self) -> None:
        assert not self._runs(SKIPPED, mode="parallel")

    def test_skipped_when_the_mode_is_unset(self) -> None:
        assert not self._runs(SKIPPED, mode="")

    def test_nothing_new_starts_in_a_cancelled_run(self) -> None:
        assert not self._runs(CANCELLED, cancelled=True)
        assert not self._runs(OK, cancelled=True)


class TestGeminiGate:
    """`review-gemini-s` against every claude/codex outcome pair."""

    def _runs(self, claude: Job, codex: Job, **state: Any) -> bool:
        return _evaluate(
            _condition("review-gemini-s"),
            needs={"review-claude-s": claude, "review-codex-s": codex},
            **state,
        )

    def test_runs_after_two_normal_reviews(self) -> None:
        assert self._runs(OK, OK)

    def test_runs_when_claude_failed(self) -> None:
        """The AT-2125 change: a claude failure no longer skips gemini."""
        assert self._runs(FAILED, OK)

    def test_runs_when_codex_failed(self) -> None:
        """Pre-existing tolerance, kept."""
        assert self._runs(OK, FAILED)

    def test_runs_when_both_failed(self) -> None:
        assert self._runs(FAILED, FAILED)

    def test_skipped_when_claude_requested_early_exit(self) -> None:
        """codex was skipped by the same early exit and exposes no outputs."""
        assert not self._runs(EARLY_EXIT, SKIPPED)

    def test_skipped_when_codex_requested_early_exit(self) -> None:
        assert not self._runs(OK, EARLY_EXIT)

    def test_skipped_when_codex_requested_early_exit_after_a_claude_failure(self) -> None:
        assert not self._runs(FAILED, EARLY_EXIT)

    def test_skipped_when_prepare_skipped_the_pr(self) -> None:
        assert not self._runs(SKIPPED, SKIPPED, prepare_skip="true")

    def test_skipped_when_prepare_failed(self) -> None:
        assert not self._runs(SKIPPED, SKIPPED, prepare_skip="")

    def test_skipped_in_parallel_mode(self) -> None:
        assert not self._runs(SKIPPED, SKIPPED, mode="parallel")

    def test_nothing_new_starts_in_a_cancelled_run(self) -> None:
        assert not self._runs(OK, CANCELLED, cancelled=True)
        assert not self._runs(OK, OK, cancelled=True)


class TestOriginalDefectIsGone:
    """The exact shape shipped in v1.8.0, evaluated the same way.

    Pins the translator to the defect it exists to catch: the old gemini guard
    skipped on a claude failure and the old codex guard carried an implicit
    success() that this translator cannot express, which is why the old codex
    guard is not evaluated here.
    """

    _OLD_GEMINI = (
        "${{ always() && vars.REVIEW_MODE == 'sequential' && "
        "needs.prepare.outputs.skip == 'false' && "
        "needs.review-claude-s.result == 'success' && "
        "needs.review-claude-s.outputs.early_exit != 'true' && "
        "(needs.review-codex-s.result == 'failure' || "
        "needs.review-codex-s.outputs.early_exit != 'true') }}"
    )

    def test_old_gemini_guard_skipped_on_a_claude_failure(self) -> None:
        old = self._OLD_GEMINI.replace("always()", "!cancelled()")
        assert not _evaluate(old, needs={"review-claude-s": FAILED, "review-codex-s": OK})
        assert _evaluate(old, needs={"review-claude-s": OK, "review-codex-s": FAILED})
