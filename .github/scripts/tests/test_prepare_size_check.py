"""Tests for prepare's `Check PR size` step (AT-3052).

The step is executed here as the shell script it is, against a stubbed `gh`,
the way test_prepare_pr_metadata.py runs `Resolve PR refs`: the outputs and
the PR comment a run would really produce are observed, not asserted about.

Two regimes. With neither split variable set the combined gate runs exactly
as it did before AT-3052 -- the outputs it already had and its comment are
byte-identical. With either set, the combined check is dropped and each
axis is checked against its own variable, an unset one falling back to
PR_SIZE_LIMIT.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import textwrap
from pathlib import Path
from typing import Any

import pytest
import yaml

from github_pr_support import (
    ADDITIONS_SIZE_AXIS,
    ADDITIONS_SIZE_VAR,
    COMBINED_SIZE_AXIS,
    COMBINED_SIZE_VAR,
    DELETIONS_SIZE_AXIS,
    DELETIONS_SIZE_VAR,
    SIZE_WARN_PERCENT_MAX,
    SIZE_WARN_PERCENT_MIN,
    describe_size_breach,
    describe_size_overage,
    describe_size_warning,
)

WORKFLOW = (
    Path(__file__).resolve().parents[2] / "workflows" / "base-ai-review-prepare.yml"
)
STEP_NAME = "Check PR size"
REPO = "ignite-corp/ai-dev-pr-review"

_EXPRESSION = re.compile(r"\$\{\{(.+?)\}\}")
# `vars.NAME || 'default'`: GitHub yields the default when the var is unset.
_VAR_OR_DEFAULT = re.compile(
    r"^vars\.(?P<name>[A-Z_][A-Z0-9_]*)\s*\|\|\s*'(?P<default>[^']*)'$"
)


def _step() -> dict[str, Any]:
    workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    for step in workflow["jobs"]["prepare"]["steps"]:
        if step.get("name") == STEP_NAME:
            return step
    raise AssertionError(f"step not found in {WORKFLOW.name}: {STEP_NAME}")


def _render(text: str, repo_vars: dict[str, str]) -> str:
    """Evaluate the step's expressions as a run with `repo_vars` would.

    Unmapped expressions raise, so a new one cannot silently become "".
    """
    fixed = {
        "steps.target_token.outputs.token || github.token": "gh-token",
        "inputs.pr_number || github.event.pull_request.number": "35",
        "inputs.target_repo || github.repository": REPO,
    }

    def replace(match: re.Match[str]) -> str:
        expression = match.group(1).strip()
        if expression in fixed:
            return fixed[expression]
        var = _VAR_OR_DEFAULT.match(expression)
        if var:
            return repo_vars.get(var.group("name")) or var.group("default")
        raise AssertionError(f"unmapped workflow expression: {expression}")

    return _EXPRESSION.sub(replace, text)


def _write_gh_stub(tmp_path: Path) -> Path:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    stub = bin_dir / "gh"
    stub.write_text(
        textwrap.dedent(
            """\
            #!/bin/bash
            if [ "$1 $2" = "pr view" ]; then
              cat "$PR_FIXTURE"
            elif [ "$1 $2" = "pr comment" ]; then
              shift 2
              while [ "$#" -gt 0 ]; do
                if [ "$1" = "--body" ]; then printf '%s' "$2" > "$COMMENT_LOG"; fi
                shift
              done
            else
              echo "unexpected gh invocation: $*" >&2; exit 1
            fi
            """
        ),
        encoding="utf-8",
    )
    stub.chmod(0o755)
    return bin_dir


def _run(
    tmp_path: Path, *, additions: int, deletions: int, repo_vars: dict[str, str]
) -> tuple[subprocess.CompletedProcess[str], dict[str, str], str | None]:
    """Run the step; return (process, step outputs, posted comment or None)."""
    step = _step()
    fixture = tmp_path / "pr.json"
    fixture.write_text(json.dumps({"additions": additions, "deletions": deletions}))
    output = tmp_path / "github_output"
    output.touch()
    comment = tmp_path / "comment.txt"
    env = {
        "PATH": f"{_write_gh_stub(tmp_path)}{os.pathsep}{os.environ['PATH']}",
        "GITHUB_OUTPUT": str(output),
        "PR_FIXTURE": str(fixture),
        "COMMENT_LOG": str(comment),
        "LENS_TARGET_REPOSITORY": REPO,
    }
    for key, value in step["env"].items():
        env[key] = _render(str(value), repo_vars)
    result = subprocess.run(
        ["bash", "-e", "-o", "pipefail", "-c", _render(step["run"], repo_vars)],
        env=env,
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=30,
    )
    outputs: dict[str, str] = {}
    for line in output.read_text(encoding="utf-8").splitlines():
        key, _, value = line.partition("=")
        outputs[key] = value
    posted = comment.read_text(encoding="utf-8") if comment.exists() else None
    return result, outputs, posted


# ------------------------------------------------------------ combined path


def test_only_pr_size_limit_keeps_the_combined_gate_byte_identical(tmp_path):
    result, outputs, posted = _run(
        tmp_path, additions=4000, deletions=200, repo_vars={"PR_SIZE_LIMIT": "3000"}
    )
    assert result.returncode == 0, result.stderr
    assert outputs["skip"] == "true"
    assert outputs["total"] == "4200"
    assert outputs["limit"] == "3000"
    assert outputs["exceeded"] == "PR_SIZE_LIMIT"
    assert outputs["axes"] == "changed"
    assert outputs["additions"] == "4000"
    assert outputs["deletions"] == "200"
    # The comment as it read before AT-3052, with only the AT-2415 overage
    # sentence inserted between the measurement and the skip notice.
    assert posted == (
        "[!] PR too large (4200 lines changed, limit 3000)."
        " Over by 1200 changed lines; split into at least 2 PRs. Skipping AI review"
        " -- the aggregate verdict below explains how to proceed."
    )


def test_combined_gate_default_limit_reviews_a_small_pr(tmp_path):
    result, outputs, posted = _run(tmp_path, additions=10, deletions=5, repo_vars={})
    assert result.returncode == 0, result.stderr
    assert outputs["skip"] == "false"
    assert outputs["total"] == "15"
    assert outputs["limit"] == "3000"
    assert outputs["exceeded"] == ""
    assert outputs["additions"] == "10"
    assert outputs["deletions"] == "5"
    assert posted is None


def test_combined_gate_counts_deletions_toward_the_one_limit(tmp_path):
    """Regression: with no split var a deletion-heavy PR still skips."""
    result, outputs, _ = _run(tmp_path, additions=100, deletions=5000, repo_vars={})
    assert result.returncode == 0, result.stderr
    assert outputs["skip"] == "true"


# --------------------------------------------------------------- split path


def test_deletions_heavy_pr_with_a_high_deletions_limit_is_reviewed(tmp_path):
    result, outputs, posted = _run(
        tmp_path,
        additions=100,
        deletions=5000,
        repo_vars={"PR_SIZE_LIMIT_DELETIONS": "10000"},
    )
    assert result.returncode == 0, result.stderr
    assert outputs["skip"] == "false"
    assert outputs["exceeded"] == ""
    assert outputs["additions"] == "100"
    assert outputs["deletions"] == "5000"
    assert posted is None


def test_additions_over_their_limit_skip_naming_that_var(tmp_path):
    result, outputs, posted = _run(
        tmp_path,
        additions=2500,
        deletions=10,
        repo_vars={"PR_SIZE_LIMIT_ADDITIONS": "2000"},
    )
    assert result.returncode == 0, result.stderr
    assert outputs["skip"] == "true"
    assert outputs["exceeded"] == "PR_SIZE_LIMIT_ADDITIONS"
    assert outputs["axes"] == "added"
    assert outputs["total"] == "2500"
    assert outputs["limit"] == "2000"
    assert posted is not None
    assert "2500 added lines exceeds PR_SIZE_LIMIT_ADDITIONS 2000" in posted


def test_an_unset_axis_falls_back_to_pr_size_limit(tmp_path):
    result, outputs, posted = _run(
        tmp_path,
        additions=3500,
        deletions=10,
        repo_vars={"PR_SIZE_LIMIT_DELETIONS": "10000"},
    )
    assert result.returncode == 0, result.stderr
    assert outputs["skip"] == "true"
    assert outputs["exceeded"] == "PR_SIZE_LIMIT"
    assert outputs["axes"] == "added"
    assert outputs["total"] == "3500"
    assert outputs["limit"] == "3000"
    assert posted is not None
    assert "3500 added lines exceeds PR_SIZE_LIMIT 3000" in posted


def test_the_combined_check_is_dropped_once_a_split_var_is_set(tmp_path):
    """2900 + 2900 is over the combined 3000, but each axis is under it."""
    result, outputs, _ = _run(
        tmp_path,
        additions=2900,
        deletions=2900,
        repo_vars={"PR_SIZE_LIMIT_ADDITIONS": "3000"},
    )
    assert result.returncode == 0, result.stderr
    assert outputs["skip"] == "false"


def test_both_axes_exceeded_join_their_messages(tmp_path):
    result, outputs, posted = _run(
        tmp_path,
        additions=1500,
        deletions=2000,
        repo_vars={
            "PR_SIZE_LIMIT_ADDITIONS": "1000",
            "PR_SIZE_LIMIT_DELETIONS": "1200",
        },
    )
    assert result.returncode == 0, result.stderr
    assert outputs["skip"] == "true"
    assert outputs["exceeded"] == "PR_SIZE_LIMIT_ADDITIONS PR_SIZE_LIMIT_DELETIONS"
    assert outputs["axes"] == "added deleted"
    assert outputs["total"] == "1500 2000"
    assert outputs["limit"] == "1000 1200"
    assert posted is not None
    assert (
        "1500 added lines exceeds PR_SIZE_LIMIT_ADDITIONS 1000;"
        " 2000 deleted lines exceeds PR_SIZE_LIMIT_DELETIONS 1200"
    ) in posted
    # The bash wording and the one Python definition both modules render
    # through cannot drift apart: the step's parts are the shared function's.
    shared = "; ".join(
        describe_size_breach(*part)
        for part in (
            (1500, ADDITIONS_SIZE_AXIS, ADDITIONS_SIZE_VAR, 1000),
            (2000, DELETIONS_SIZE_AXIS, DELETIONS_SIZE_VAR, 1200),
        )
    )
    assert f"[!] PR too large ({shared})." in posted


# ---------------------------------------------------------------- validation


@pytest.mark.parametrize(
    "name", ["PR_SIZE_LIMIT", "PR_SIZE_LIMIT_ADDITIONS", "PR_SIZE_LIMIT_DELETIONS"]
)
@pytest.mark.parametrize("value", ["many", "3k", "-1", "1 0"])
def test_a_non_integer_limit_fails_the_step(tmp_path, name, value):
    result, outputs, posted = _run(
        tmp_path, additions=1, deletions=1, repo_vars={name: value}
    )
    assert result.returncode != 0
    assert f"::error::Invalid {name}" in result.stdout
    assert "skip" not in outputs
    assert posted is None


# ----------------------------------------------------------------- threading

_AGGREGATE = WORKFLOW.parent / "base-ai-review-aggregate.yml"
_ORCHESTRATOR = WORKFLOW.parent / "base-ai-review-orchestrator.yml"
# prepare job output -> the size-check step output it carries.
_SIZE_OUTPUTS = {
    "size_total": "total",
    "size_limit": "limit",
    "size_additions": "additions",
    "size_deletions": "deletions",
    "size_exceeded": "exceeded",
    "size_axes": "axes",
    "size_warned": "warned",
    "size_warn_axes": "warn_axes",
    "size_warn_total": "warn_total",
    "size_warn_limit": "warn_limit",
}


def test_prepare_exports_every_size_output():
    workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    job_outputs = workflow["jobs"]["prepare"]["outputs"]
    call_outputs = workflow[True]["workflow_call"]["outputs"]
    for name, step_output in _SIZE_OUTPUTS.items():
        assert job_outputs[name] == f"${{{{ steps.size-check.outputs.{step_output} }}}}"
        assert call_outputs[name]["value"] == f"${{{{ jobs.prepare.outputs.{name} }}}}"


def test_aggregate_hands_every_size_input_to_the_script():
    workflow = yaml.safe_load(_AGGREGATE.read_text(encoding="utf-8"))
    inputs = workflow[True]["workflow_call"]["inputs"]
    env = next(
        step["env"]
        for step in next(iter(workflow["jobs"].values()))["steps"]
        if "SIZE_SKIPPED" in step.get("env", {})
    )
    for name in _SIZE_OUTPUTS:
        assert name in inputs, name
        assert env[name.upper()] == f"${{{{ inputs.{name} }}}}", name


def test_orchestrator_passes_every_size_output_to_the_aggregate():
    workflow = yaml.safe_load(_ORCHESTRATOR.read_text(encoding="utf-8"))
    passed = next(
        job["with"]
        for job in workflow["jobs"].values()
        if "size_skipped" in job.get("with", {})
    )
    for name in _SIZE_OUTPUTS:
        assert passed[name] == f"${{{{ needs.prepare.outputs.{name} }}}}", name


# ------------------------------------------------- approaching the limit (AT-2415)


def test_under_the_warn_threshold_warns_nothing(tmp_path):
    result, outputs, posted = _run(
        tmp_path, additions=2000, deletions=399, repo_vars={}
    )
    assert result.returncode == 0, result.stderr
    assert outputs["skip"] == "false"
    assert outputs["warned"] == ""
    assert "::notice::" not in result.stdout
    assert posted is None


def test_at_the_warn_threshold_notices_without_skipping(tmp_path):
    result, outputs, posted = _run(
        tmp_path, additions=2400, deletions=126, repo_vars={}
    )
    assert result.returncode == 0, result.stderr
    assert outputs["skip"] == "false"
    assert outputs["warned"] == "PR_SIZE_LIMIT"
    assert outputs["warn_axes"] == "changed"
    assert outputs["warn_total"] == "2526"
    assert outputs["warn_limit"] == "3000"
    warning = describe_size_warning(2526, COMBINED_SIZE_AXIS, COMBINED_SIZE_VAR, 3000)
    assert warning == (
        "[!] size: 2526/3000 changed lines (84% of PR_SIZE_LIMIT); the review"
        " is skipped above the limit. Consider splitting."
    )
    assert f"::notice::{warning}" in result.stdout
    assert posted is None


def test_exactly_at_the_limit_still_warns_rather_than_skips(tmp_path):
    result, outputs, _ = _run(tmp_path, additions=3000, deletions=0, repo_vars={})
    assert result.returncode == 0, result.stderr
    assert outputs["skip"] == "false"
    assert outputs["warned"] == "PR_SIZE_LIMIT"


def test_the_warn_percent_var_moves_the_threshold(tmp_path):
    result, outputs, _ = _run(
        tmp_path, additions=1600, deletions=0, repo_vars={"PR_SIZE_WARN_PERCENT": "50"}
    )
    assert result.returncode == 0, result.stderr
    assert outputs["warned"] == "PR_SIZE_LIMIT"
    assert "(53% of PR_SIZE_LIMIT)" in result.stdout


def test_per_axis_warning_names_each_axis_var(tmp_path):
    result, outputs, _ = _run(
        tmp_path,
        additions=1700,
        deletions=2500,
        repo_vars={"PR_SIZE_LIMIT_ADDITIONS": "2000"},
    )
    assert result.returncode == 0, result.stderr
    assert outputs["skip"] == "false"
    assert outputs["warned"] == "PR_SIZE_LIMIT_ADDITIONS PR_SIZE_LIMIT"
    assert outputs["warn_axes"] == "added deleted"
    assert outputs["warn_total"] == "1700 2500"
    assert outputs["warn_limit"] == "2000 3000"
    for part in (
        (1700, ADDITIONS_SIZE_AXIS, ADDITIONS_SIZE_VAR, 2000),
        (2500, DELETIONS_SIZE_AXIS, COMBINED_SIZE_VAR, 3000),
    ):
        assert f"::notice::{describe_size_warning(*part)}" in result.stdout


def test_an_exceeded_axis_is_not_also_warned(tmp_path):
    result, outputs, _ = _run(
        tmp_path,
        additions=2500,
        deletions=1700,
        repo_vars={
            "PR_SIZE_LIMIT_ADDITIONS": "2000",
            "PR_SIZE_LIMIT_DELETIONS": "2000",
        },
    )
    assert result.returncode == 0, result.stderr
    assert outputs["skip"] == "true"
    assert outputs["exceeded"] == "PR_SIZE_LIMIT_ADDITIONS"
    assert outputs["warned"] == "PR_SIZE_LIMIT_DELETIONS"


def test_over_the_limit_comment_says_by_how_much_and_how_to_split(tmp_path):
    result, _, posted = _run(tmp_path, additions=7000, deletions=0, repo_vars={})
    assert result.returncode == 0, result.stderr
    overage = describe_size_overage([(7000, COMBINED_SIZE_AXIS, 3000)])
    assert overage == "over by 4000 changed lines; split into at least 3 PRs"
    assert posted == (
        "[!] PR too large (7000 lines changed, limit 3000)."
        " Over by 4000 changed lines; split into at least 3 PRs."
        " Skipping AI review -- the aggregate verdict below explains how to proceed."
    )


def test_both_axes_over_the_limit_name_each_overage(tmp_path):
    result, _, posted = _run(
        tmp_path,
        additions=1500,
        deletions=2500,
        repo_vars={
            "PR_SIZE_LIMIT_ADDITIONS": "1000",
            "PR_SIZE_LIMIT_DELETIONS": "1200",
        },
    )
    assert result.returncode == 0, result.stderr
    overage = describe_size_overage(
        [(1500, ADDITIONS_SIZE_AXIS, 1000), (2500, DELETIONS_SIZE_AXIS, 1200)]
    )
    assert overage == (
        "over by 500 added lines and 1300 deleted lines; split into at least 3 PRs"
    )
    assert posted is not None
    assert f"). {overage[0].upper()}{overage[1:]}. Skipping AI review" in posted


def test_a_zero_limit_names_the_overage_without_a_split_count(tmp_path):
    result, _, posted = _run(
        tmp_path, additions=5, deletions=0, repo_vars={"PR_SIZE_LIMIT": "0"}
    )
    assert result.returncode == 0, result.stderr
    assert (
        describe_size_overage([(5, COMBINED_SIZE_AXIS, 0)]) == "over by 5 changed lines"
    )
    # No breach is no overage, not a crash on max() of nothing.
    assert describe_size_overage([]) == "over by "
    assert posted is not None
    assert "). Over by 5 changed lines. Skipping AI review" in posted


# Bounds derived from the shared constants, so the step's bash literals
# cannot drift from what the local driver enforces.
@pytest.mark.parametrize(
    "value",
    [
        str(SIZE_WARN_PERCENT_MIN - 1),
        str(SIZE_WARN_PERCENT_MAX + 1),
        "many",
        "-5",
        "8 0",
        "1.5",
    ],
)
def test_an_invalid_warn_percent_fails_the_step(tmp_path, value):
    result, outputs, posted = _run(
        tmp_path, additions=1, deletions=1, repo_vars={"PR_SIZE_WARN_PERCENT": value}
    )
    assert result.returncode != 0
    assert "::error::Invalid PR_SIZE_WARN_PERCENT" in result.stdout
    assert (
        f"(must be integer {SIZE_WARN_PERCENT_MIN}-{SIZE_WARN_PERCENT_MAX})"
        in result.stdout
    )
    assert "skip" not in outputs
    assert posted is None


@pytest.mark.parametrize(
    "value", [str(SIZE_WARN_PERCENT_MIN), str(SIZE_WARN_PERCENT_MAX), "080"]
)
def test_a_valid_warn_percent_is_accepted(tmp_path, value):
    result, outputs, _ = _run(
        tmp_path, additions=1, deletions=1, repo_vars={"PR_SIZE_WARN_PERCENT": value}
    )
    assert result.returncode == 0, result.stderr
    assert outputs["skip"] == "false"
