"""Tests for pr_reachable_checks: PR-reachability footgun detection.

Test-module hygiene: all imports belong at the top of this file per PEP 8 --
do not add `import foo` statements inside test function bodies.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

from pr_reachable_checks import _classify, _event_set, _names, count_unreachable


def _write(wf_dir: Path, name: str, body: str) -> None:
    wf_dir.joinpath(name).write_text(textwrap.dedent(body), encoding="utf-8")


# ---------------------------------------------------------------------------
# _event_set
# ---------------------------------------------------------------------------


def test_event_set_string() -> None:
    assert _event_set("push") == {"push"}


def test_event_set_list() -> None:
    assert _event_set(["push", "pull_request"]) == {"push", "pull_request"}


def test_event_set_dict() -> None:
    assert _event_set({"push": {"branches": ["main"]}, "pull_request": None}) == {
        "push",
        "pull_request",
    }


def test_event_set_none() -> None:
    assert _event_set(None) == set()


# ---------------------------------------------------------------------------
# _names
# ---------------------------------------------------------------------------


def test_names_uses_job_display_name_and_id_and_workflow_name() -> None:
    doc = {"name": "ci-main", "jobs": {"lint": {"name": "Lint & Type Check"}, "build": {}}}
    assert _names(doc) == {"ci-main", "lint", "Lint & Type Check", "build"}


# ---------------------------------------------------------------------------
# count_unreachable -- the real footgun scenarios
# ---------------------------------------------------------------------------


def test_push_only_required_check_is_a_footgun(tmp_path: Path) -> None:
    # A job that only runs on push to main can never report on a PR.
    _write(
        tmp_path,
        "ci-main.yml",
        """
        name: ci-main
        on:
          push:
            branches: [main]
        jobs:
          lint:
            name: Lint & Type Check
          test:
            name: Unit & Integration Tests
        """,
    )
    contexts = ["Lint & Type Check", "Unit & Integration Tests"]
    assert count_unreachable(str(tmp_path), contexts) == 2


def test_pr_triggered_check_is_reachable(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "ci-pr.yml",
        """
        name: ci-pr
        on:
          pull_request:
            branches: [main]
        jobs:
          lint-and-test:
            name: lint-and-test
        """,
    )
    assert count_unreachable(str(tmp_path), ["lint-and-test"]) == 0


def test_yaml_on_key_parsed_as_boolean_true_is_handled(tmp_path: Path) -> None:
    # PyYAML parses the bare key ``on`` as boolean True; the classifier must
    # still recover the trigger. If mishandled, this PR job looks push-only.
    _write(
        tmp_path,
        "pr.yml",
        """
        name: pr
        on:
          pull_request:
        jobs:
          check:
            name: check
        """,
    )
    pr_names, _ = _classify(str(tmp_path))
    assert "check" in pr_names
    assert count_unreachable(str(tmp_path), ["check"]) == 0


def test_composed_context_matches_terminal_job_segment(tmp_path: Path) -> None:
    # A reusable/nested context "review / aggregate / Aggregate & Verdict"
    # resolves via its terminal segment to a callable workflow's job.
    _write(
        tmp_path,
        "review.yml",
        """
        name: review
        on:
          workflow_call:
        jobs:
          aggregate:
            name: Aggregate & Verdict
        """,
    )
    ctx = "review / aggregate / Aggregate & Verdict"
    assert count_unreachable(str(tmp_path), [ctx]) == 0


def test_unknown_external_context_is_not_flagged(tmp_path: Path) -> None:
    # A context matching no local workflow (external app / other reporter) is
    # never a footgun -- conservative to avoid breaking the audit.
    _write(
        tmp_path,
        "ci.yml",
        """
        name: ci
        on: [pull_request]
        jobs:
          build:
            name: build
        """,
    )
    assert count_unreachable(str(tmp_path), ["some-external/check"]) == 0


def test_workflow_call_reusable_treated_as_reachable(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "reusable.yml",
        """
        name: reusable
        on:
          workflow_call:
        jobs:
          scan:
            name: scan
        """,
    )
    assert count_unreachable(str(tmp_path), ["scan"]) == 0


def test_same_name_on_pr_and_push_is_reachable(tmp_path: Path) -> None:
    # If any PR-triggered workflow produces the context, it is reachable even
    # when a push-only workflow shares the name.
    _write(
        tmp_path,
        "push.yml",
        """
        name: push-flow
        on:
          push:
            branches: [main]
        jobs:
          shared:
            name: shared-check
        """,
    )
    _write(
        tmp_path,
        "pr.yml",
        """
        name: pr-flow
        on: [pull_request]
        jobs:
          shared:
            name: shared-check
        """,
    )
    assert count_unreachable(str(tmp_path), ["shared-check"]) == 0
