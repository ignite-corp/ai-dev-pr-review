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

from local_review_config import WORKFLOW_DIR  # noqa: E402
from reviewer_prompts import (  # noqa: E402
    MAX_EXISTING_THREADS,
    MAX_THREAD_BODY_CHARS,
    build_claude_prompt,
)

SINGLE_YML = WORKFLOW_DIR / "base-ai-review-single.yml"
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


def _thread(
    index: int, body: str = "finding", status: str = "unresolved"
) -> dict[str, object]:
    return {
        "path": f"src/module_{index}.py",
        "line": index,
        "status": status,
        "body": body,
    }


# Running the YAML step's own shell needs bash and jq. Missing them locally is
# a reason to skip; missing them in CI is a reason to fail.
#
# This module is the only thing standing between the two copies of the Claude
# prompt, and its own docstring says why that matters -- the pilot wrapper lost
# all four rule blocks for weeks because nobody was checking. A module-wide
# skip would retire that check silently and report the run green, so on CI,
# where both tools are guaranteed, their absence is an error instead.
_MISSING_TOOLS = [tool for tool in ("bash", "jq") if shutil.which(tool) is None]
_ON_CI = bool(os.environ.get("CI") or os.environ.get("GITHUB_ACTIONS"))

if _MISSING_TOOLS and _ON_CI:
    raise RuntimeError(
        f"{', '.join(_MISSING_TOOLS)} missing on CI: the prompt-parity check"
        " cannot run, and skipping it would report a green run with the two"
        " copies of the Claude prompt unchecked"
    )

pytestmark = pytest.mark.skipif(
    bool(_MISSING_TOOLS),
    reason=f"needs {', '.join(_MISSING_TOOLS)} to run the workflow step's own shell",
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


def test_an_empty_array_renders_the_same_on_both_sides(tmp_path):
    """`"[]"` is truthy in Python and non-empty to `[ -n ]` in shell.

    Neither path emits it -- the driver's load_threads and the workflow's
    thread-loading step both collapse an empty list to an empty string -- so
    it is not reachable in practice. It is pinned anyway because the obvious
    "fix" (treating "[]" as no threads on the Python side) would make this
    module disagree with the workflow it exists to mirror, which is the one
    defect this file is here to prevent.
    """
    assert _render_via_workflow(tmp_path, "0", "[]") == build_claude_prompt("0", "[]")


def test_exactly_at_the_thread_cap_is_not_truncated(tmp_path):
    """The docstring claims the boundaries; these two pin both sides of them."""
    existing = json.dumps([_thread(i) for i in range(MAX_EXISTING_THREADS)])
    rendered = build_claude_prompt(str(MAX_EXISTING_THREADS), existing)
    assert (
        _render_via_workflow(tmp_path, str(MAX_EXISTING_THREADS), existing) == rendered
    )
    assert "(truncated)" not in rendered
    assert f"src/module_{MAX_EXISTING_THREADS - 1}.py" in rendered


def test_a_body_exactly_at_the_cap_is_not_truncated(tmp_path):
    body = "y" * MAX_THREAD_BODY_CHARS
    existing = json.dumps([_thread(1, body=body)])
    rendered = build_claude_prompt("1", existing)
    assert _render_via_workflow(tmp_path, "1", existing) == rendered
    assert body + "..." not in rendered


def test_one_thread_past_the_cap_is_truncated(tmp_path):
    """The other side of the thread boundary.

    One render per test: _render_via_workflow appends to one $GITHUB_ENV file
    and reads back the first block it finds, so two renders sharing a tmp_path
    silently compare the first one twice.
    """
    count = MAX_EXISTING_THREADS + 1
    existing = json.dumps([_thread(i) for i in range(count)])
    rendered = build_claude_prompt(str(count), existing)
    assert _render_via_workflow(tmp_path, str(count), existing) == rendered
    assert "(truncated)" in rendered


def test_one_character_past_the_body_cap_is_truncated(tmp_path):
    existing = json.dumps([_thread(1, body="z" * (MAX_THREAD_BODY_CHARS + 1))])
    rendered = build_claude_prompt("1", existing)
    assert _render_via_workflow(tmp_path, "1", existing) == rendered
    assert "z" * MAX_THREAD_BODY_CHARS + "..." in rendered


def test_unparseable_thread_data_costs_the_context_not_the_run(capsys):
    """Losing dedup context is what a failed collection already costs.

    The input's source is `.review-context/unresolved-threads.json`, which a
    pull request could reach until the checkout/cleanup order was fixed. The
    reason this is safe is not that the door is now shut -- it is that a
    reviewer without prior threads is exactly the state a failed
    collect_review_threads.sh leaves, and that is allowed to happen.
    """
    assert build_claude_prompt("3", "{not json") == build_claude_prompt("0", "")
    assert "no prior threads" in capsys.readouterr().err


def test_thread_data_that_is_not_a_list_costs_the_context_not_the_run(capsys):
    assert build_claude_prompt("3", '{"a": 1}') == build_claude_prompt("0", "")
    assert "not a list" in capsys.readouterr().err


def test_valid_thread_data_is_untouched_by_the_guard(tmp_path):
    """The guard must not move the byte-parity the module exists for."""
    existing = json.dumps([_thread(1), _thread(2)])
    assert _render_via_workflow(tmp_path, "2", existing) == build_claude_prompt(
        "2", existing
    )
