"""A required status check must be reachable on a PR with any base branch.

`test` (ci.yml) and `guard` (self-review.yml) are the two required status
checks on main. Both workflows used to carry `on.pull_request.branches:
[main]`, which filters on the PR's BASE branch, so on a stacked PR -- one
based on a topic branch -- neither ran. The check was then absent rather than
failing, and an absent required check shows as no gate at all: AT-2412
measured PR #172 as CLEAN with nothing attached, and AT-2413 measured #170 the
same way, where a hand-dispatched review round raised three majors.

Retargeting such a PR onto main does not repair it either: a base change is
the `edited` activity type, which is not in the default `opened` /
`synchronize` / `reopened` set, so no run is provoked.

What these tests can assert is the trigger set in the YAML. Whether GitHub
then attaches the check run to the head SHA, and whether the merge box counts
it, is only observable in a real run.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml
from pr_reachable_checks import job_context

_WORKFLOWS = Path(__file__).resolve().parents[2] / "workflows"
_EXAMPLE = (
    Path(__file__).resolve().parents[3] / "examples" / "consumer-thin-trigger.yml"
)

# The required status-check contexts on main, each with the workflow whose job
# id reports it. Read from the live ruleset with
# `gh api repos/<org>/<repo>/rules/branches/main`.
_REQUIRED = {"test": "ci.yml", "guard": "self-review.yml"}

# The activity types `pull_request` fires on when no `types:` is declared.
_DEFAULT_TYPES = frozenset({"opened", "synchronize", "reopened"})


# `dict[Any, Any]`, not `dict[str, Any]`: YAML 1.1 parses the bare key `on`
# as `True`, so a workflow document's keys are not all strings, and
# `dict[str, Any]` makes the `True` lookup below ill-typed. It went unreported
# because pyright's default exclude of dot-directories meant `.github/` was
# never analyzed (AT-2418).
def _workflow(filename: str) -> dict[Any, Any]:
    doc = yaml.safe_load((_WORKFLOWS / filename).read_text(encoding="utf-8"))
    assert isinstance(doc, dict), filename
    return doc


def _triggers(doc: dict[Any, Any]) -> dict[Any, Any]:
    """The `on:` mapping. PyYAML 1.1 parses the bare key `on` as True."""
    on = doc.get(True, doc.get("on"))
    assert isinstance(on, dict), "expected a mapping of event name to filters"
    return on


def _assert_no_branch_or_path_filter(on: dict[Any, Any], who: str) -> None:
    """A `pull_request` filtered by base branch or by path skips some PRs.

    `branches:` filters on the PR's BASE branch, so a PR based on a topic
    branch gets no run; `paths:` skips a PR whose files it excludes. Either
    way the check is absent, not skipped-as-success, and the merge box reads
    an absent required check as no gate (AT-2412).
    """
    filters = on["pull_request"] or {}
    for key in ("branches", "branches-ignore", "paths", "paths-ignore"):
        assert key not in filters, (
            f"{who} has a `{key}:` filter, so some PRs get no run; "
            "an absent required check reads as no gate (AT-2412)"
        )


def _missing_default_types(on: dict[Any, Any]) -> set[str]:
    """Default types a declared `types:` list leaves out; none if undeclared."""
    filters = on["pull_request"] or {}
    return set(_DEFAULT_TYPES - set(filters.get("types", _DEFAULT_TYPES)))


@pytest.mark.parametrize(("context", "filename"), sorted(_REQUIRED.items()))
class TestRequiredCheckIsReachableFromAnyBase:
    def test_context_names_a_job_in_its_workflow(
        self, context: str, filename: str
    ) -> None:
        """Renaming the job renames the context, which breaks the ruleset.

        A job id lookup alone does not see that rename: GitHub reports a job
        under its `name:` when one is set, so the context must be resolved.
        `job_context` is the resolver the footgun audit already uses, and is
        the only place that rule is written down.
        """
        jobs = _workflow(filename)["jobs"]
        assert context in jobs, filename
        assert job_context(context, jobs[context]) == context, filename

    def test_pull_request_is_a_trigger(self, context: str, filename: str) -> None:
        assert "pull_request" in _triggers(_workflow(filename)), context

    def test_pull_request_has_no_branch_or_path_filter(
        self, context: str, filename: str
    ) -> None:
        """The defect: `branches: [main]` filters on the PR's base branch."""
        _assert_no_branch_or_path_filter(_triggers(_workflow(filename)), context)

    def test_declared_types_include_the_defaults(
        self, context: str, filename: str
    ) -> None:
        """A `types:` list replaces the default set rather than adding to it.

        Dropping `reopened` fails the AT-2412 way: a branch pushed while the PR
        is closed and the PR then reopened gets no run on its new head, so the
        required check is absent. No declared list means the defaults apply.
        """
        missing = _missing_default_types(_triggers(_workflow(filename)))
        assert not missing, (
            f"{context} gets no run on {sorted(missing)}; "
            "an absent required check reads as no gate (AT-2412)"
        )


# Consumers copy the thin trigger, so its `pull_request` filters are theirs.
# Its `AI Code Review` context is required on the consumer's branch, not here,
# so it is outside `_REQUIRED` but fails the same way.
def _consumer_example_triggers() -> dict[Any, Any]:
    doc = yaml.safe_load(_EXAMPLE.read_text(encoding="utf-8"))
    assert isinstance(doc, dict), _EXAMPLE.name
    return _triggers(doc)


def test_consumer_example_has_no_branch_or_path_filter() -> None:
    _assert_no_branch_or_path_filter(
        _consumer_example_triggers(), "the consumer example"
    )


def test_consumer_example_declares_the_default_types() -> None:
    missing = _missing_default_types(_consumer_example_triggers())
    assert not missing, f"the consumer example gets no run on {sorted(missing)}"


def test_a_job_name_displaces_the_job_id_as_the_context() -> None:
    """Why the guard above resolves instead of looking the job id up.

    `test` is still a job id here, so the id lookup stays green while the job
    reports as `Unit Tests` and the ruleset's `test` context never arrives --
    an absent required check once more, from a rename this time.
    """
    jobs = {"test": {"name": "Unit Tests"}}
    assert "test" in jobs
    assert job_context("test", jobs["test"]) == "Unit Tests"


class TestCiWorkflow:
    def test_push_stays_on_main(self) -> None:
        """Widening `push` too would double every PR's run."""
        assert _triggers(_workflow("ci.yml"))["push"]["branches"] == ["main"]

    def test_can_be_run_by_hand(self) -> None:
        """Recovery path for a head SHA that never got a run (AT-2412)."""
        assert "workflow_dispatch" in _triggers(_workflow("ci.yml"))


class TestSelfReviewWorkflow:
    def test_can_be_run_by_hand(self) -> None:
        assert "workflow_dispatch" in _triggers(_workflow("self-review.yml"))

    def test_default_activity_types_are_not_relied_on_for_retargeting(self) -> None:
        """`edited` (a base change) is absent, so the base filter must stay gone.

        Keeping the declared types narrow is only safe because the workflow now
        runs whatever the base is; if a `branches:` filter ever comes back, the
        run would have to be provoked by an `edited` event this list excludes.
        That filter's absence is pinned by
        `TestRequiredCheckIsReachableFromAnyBase`, not here, as is the
        presence of the default types. Only `edited` is asserted here: pinning
        the whole list would also fail on an addition that leaves this
        property intact.
        """
        types = _triggers(_workflow("self-review.yml"))["pull_request"]["types"]
        assert "edited" not in types
