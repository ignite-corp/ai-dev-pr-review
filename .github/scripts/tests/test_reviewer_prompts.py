"""reviewer_prompts.py must render exactly what base-ai-review-single.yml renders.

The Claude reviewer prompt exists twice on purpose: inline in the workflow's
`Build Claude prompt with existing threads` step, because that step runs from
the PR's YAML against a script checkout pinned to the previous release, and in
reviewer_prompts.py, because the local driver has no Actions step to run.

A copy nobody checks is how the pilot wrapper lost the four rule blocks for
weeks. So this module does not compare wording by eye: it executes the YAML
step's own shell in a temp directory, reads CLAUDE_PROMPT back out of the
$GITHUB_ENV file it writes, and asserts byte equality with the module's
output -- with no threads, with threads, and past the 50-thread /
200-character truncation boundaries where the shell hands the work to jq.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

SCRIPT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SCRIPT_DIR))

from reviewer_prompts import (  # noqa: E402
    MAX_EXISTING_THREADS,
    MAX_THREAD_BODY_CHARS,
    build_claude_prompt,
)

SINGLE_YML = SCRIPT_DIR.parents[0] / "workflows" / "base-ai-review-single.yml"
STEP_NAME = "Build Claude prompt with existing threads"
ENV_KEY = "CLAUDE_PROMPT"
ENV_DELIMITER = "CLAUDE_PROMPT_EOF"


def _step_script() -> str:
    workflow = yaml.safe_load(SINGLE_YML.read_text(encoding="utf-8"))
    for step in workflow["jobs"]["review"]["steps"]:
        if step.get("name") == STEP_NAME:
            return step["run"]
    raise AssertionError(f"{SINGLE_YML.name} has no step named {STEP_NAME!r}")


def _render_via_workflow(tmp_path: Path, thread_count: str, existing: str) -> str:
    """Run the workflow step and return the CLAUDE_PROMPT it exported."""
    github_env = tmp_path / "github_env"
    github_env.touch()
    result = subprocess.run(
        ["bash", "-e", "-o", "pipefail", "-c", _step_script()],
        cwd=tmp_path,
        env={
            "PATH": os.environ["PATH"],
            "RUNNER_TEMP": str(tmp_path),
            "GITHUB_ENV": str(github_env),
            "THREAD_COUNT": thread_count,
            "EXISTING_COMMENTS": existing,
        },
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr

    lines = github_env.read_text(encoding="utf-8").split("\n")
    start = lines.index(f"{ENV_KEY}<<{ENV_DELIMITER}")
    end = lines.index(ENV_DELIMITER, start)
    return "\n".join(lines[start + 1 : end]) + "\n"


def _thread(index: int, body: str = "finding", status: str = "unresolved") -> dict:
    return {
        "path": f"src/module_{index}.py",
        "line": index,
        "status": status,
        "body": body,
    }


pytestmark = pytest.mark.skipif(
    shutil.which("jq") is None or shutil.which("bash") is None,
    reason="the workflow step's own shell needs bash and jq",
)


def test_no_threads_matches_workflow(tmp_path):
    assert _render_via_workflow(tmp_path, "0", "") == build_claude_prompt("0", "")


def test_threads_match_workflow(tmp_path):
    threads = [_thread(1), _thread(2, status="resolved")]
    existing = json.dumps(threads)
    assert _render_via_workflow(tmp_path, "2", existing) == build_claude_prompt(
        "2", existing
    )


def test_long_body_truncation_matches_workflow(tmp_path):
    existing = json.dumps([_thread(1, body="x" * (MAX_THREAD_BODY_CHARS + 50))])
    rendered = build_claude_prompt("1", existing)
    assert _render_via_workflow(tmp_path, "1", existing) == rendered
    assert "x" * MAX_THREAD_BODY_CHARS + "..." in rendered


def test_thread_cap_matches_workflow(tmp_path):
    count = MAX_EXISTING_THREADS + 5
    existing = json.dumps([_thread(i) for i in range(count)])
    rendered = build_claude_prompt(str(count), existing)
    assert _render_via_workflow(tmp_path, str(count), existing) == rendered
    assert f"({count} thread(s) (truncated))" in rendered
    assert f"src/module_{MAX_EXISTING_THREADS}.py" not in rendered


def test_non_ascii_body_matches_workflow(tmp_path):
    """jq passes UTF-8 through; ensure_ascii=False must do the same."""
    existing = json.dumps([_thread(1, body="\u00e9\u00e8 caf\u00e9")])
    assert _render_via_workflow(tmp_path, "1", existing) == build_claude_prompt(
        "1", existing
    )


def test_all_four_rule_blocks_present():
    prompt = build_claude_prompt("0", "")
    for rule in (
        "EVIDENCE RULE:",
        "DIFF SCOPE:",
        "COMPLETENESS RULE:",
        "LINE NUMBERS:",
    ):
        assert rule in prompt
