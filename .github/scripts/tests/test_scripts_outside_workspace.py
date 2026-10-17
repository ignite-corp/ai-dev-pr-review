"""Steps after a reviewer model run base scripts the model could not write (AT-2941).

A reviewer job checks the scripts out at `.ai-dev-pr-review`, inside the
workspace. The models that run there have write access to it: claude through
its Write tool, codex through `--sandbox workspace-write`. The steps after
the model run those scripts with tokens, including the corp App token in
'Switch Claude auth on usage limit' and GH_TOKEN in 'Post inline comments'.
So every `run:` body executes the copy that 'Copy ai-dev-pr-review scripts
outside the workspace' takes under RUNNER_TEMP before any model starts.

The executed test is the one that matters here. A "model" rewrites the
workspace checkout, and the step that runs next must still run the copy as
it was checked out. The text checks are there so that a new step reading the
workspace path fails here by name.
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path
from typing import Any

import yaml

_SINGLE = Path(__file__).resolve().parents[2] / "workflows" / "base-ai-review-single.yml"
_COPY = "Copy ai-dev-pr-review scripts outside the workspace"
_CHECKOUT = "Checkout ai-dev-pr-review scripts (pinned to release tag)"
# The steps in which a model runs with write access to the workspace.
_MODEL_STEPS = ("Run Claude review", "Run Codex review")
_WORKSPACE_SCRIPTS = re.compile(r"(?<![\w/}])\.ai-dev-pr-review/")
_SHELL_COMMENT = re.compile(r"(?:^|(?<=\s))#.*$", re.MULTILINE)
_RUNNER_BASH = ["bash", "--noprofile", "--norc", "-e", "-c"]


def _steps() -> list[dict[str, Any]]:
    doc = yaml.safe_load(_SINGLE.read_text(encoding="utf-8"))
    return doc["jobs"]["review"]["steps"]


def _index(steps: list[dict[str, Any]], name: str) -> int:
    for i, step in enumerate(steps):
        if step.get("name") == name:
            return i
    raise AssertionError(f"no step named {name!r}")


def test_no_run_body_executes_the_workspace_checkout() -> None:
    offenders = [
        step.get("name")
        for step in _steps()
        if "run" in step
        and step.get("name") != _COPY
        and _WORKSPACE_SCRIPTS.search(_SHELL_COMMENT.sub("", str(step["run"])))
    ]
    assert offenders == [], (
        f"{offenders} run base scripts from .ai-dev-pr-review, which the "
        "reviewer model can write; use ${RUNNER_TEMP}/ai-dev-pr-review"
    )


def test_the_copy_is_taken_after_the_checkout_and_before_any_model() -> None:
    steps = _steps()
    copy = _index(steps, _COPY)
    assert _index(steps, _CHECKOUT) < copy
    for name in _MODEL_STEPS:
        assert copy < _index(steps, name), f"the copy is taken after {name!r}"
    # No gate: a skipped copy would leave every later script path missing,
    # and the guards around them would read that as "old release".
    assert "if" not in steps[copy]


def test_codex_tmpdir_is_pinned_away_from_runner_temp() -> None:
    """The workspace-write sandbox also makes $TMPDIR writable."""
    step = _steps()[_index(_steps(), "Run Codex review")]
    tmpdir = str(step["env"]["TMPDIR"])
    assert tmpdir.startswith("${{ runner.temp }}/") and tmpdir != "${{ runner.temp }}/"
    body = str(step["run"])
    assert body.index('mkdir -p -- "$TMPDIR"') < body.index("codex exec")


def test_a_rewritten_workspace_script_is_not_what_the_net_runs(tmp_path: Path) -> None:
    """Run the copy step, let a "model" rewrite the checkout, then run the net."""
    steps = _steps()
    workdir = tmp_path / "ws"
    runner_temp = tmp_path / "runner-temp"
    marks = tmp_path / "marks"
    for path in (workdir, runner_temp, marks):
        path.mkdir()
    script = workdir / ".ai-dev-pr-review/.github/scripts/promote_legacy_verdict.sh"
    script.parent.mkdir(parents=True)
    script.write_text(f'#!/usr/bin/env bash\n: > "{marks}/trusted"\n', encoding="utf-8")
    env = {"PATH": os.environ["PATH"], "RUNNER_TEMP": str(runner_temp), "REVIEWER": "claude"}

    def run(name: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            _RUNNER_BASH + [str(steps[_index(steps, name)]["run"])],
            cwd=workdir, env=env, capture_output=True, text=True, timeout=30, check=False,
        )

    assert run(_COPY).returncode == 0
    # What a prompt-injected model with workspace write access can do.
    script.write_text(f'#!/usr/bin/env bash\n: > "{marks}/pwned"\n', encoding="utf-8")
    result = run("Clear unowned Claude verdict file")
    assert result.returncode == 0, result.stderr
    assert not (marks / "pwned").exists(), "the net ran the script the model rewrote"
    # Positive control: the net did reach a promote script, the trusted one.
    assert (marks / "trusted").exists(), "the net never reached the promote script"
