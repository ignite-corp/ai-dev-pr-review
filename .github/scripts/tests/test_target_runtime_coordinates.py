"""Execute target run bodies after the runner re-injects caller GITHUB_*.

Official runner ScriptHandler exports runtime contexts AFTER YAML env. The
stubs below observe actual API arguments and helper child environments, not
merely the presence of an export statement. No network or token is used.
"""

from __future__ import annotations

import os
from pathlib import Path
import re
import subprocess

import pytest
import yaml


WORKFLOWS = Path(__file__).resolve().parents[2] / "workflows"
FILES = ("prepare", "single", "aggregate")
CALLER = "runner-owner/private-runner"
TARGET = "target-owner/reviewed-repo"
SHA = "a" * 40


def target_steps():
    sites = []
    for name in FILES:
        doc = yaml.safe_load((WORKFLOWS / f"base-ai-review-{name}.yml").read_text())
        job = next(iter(doc["jobs"].values()))
        for step in job["steps"]:
            if "GITHUB_REPOSITORY" in step.get("env", {}):
                sites.append((name, job, step))
    assert len(sites) == 10, "Update coverage when target-coordinate sites change"
    return sites


STUBS = r"""
observe() {
  local detail="${2//$'\n'/ }"
  printf '%s\t%s\t%s\n' "$1" "$GITHUB_REPOSITORY" "$detail" >> "$CAPTURE"
}
gh() {
  observe gh "$*"
  if [[ "$*" == *--jq* ]]; then
    printf '%s\n' aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
  else
    printf '%s\n' '{"additions":2,"deletions":1,"base":{"ref":"main"},"head":{"sha":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","ref":"feature"},"user":{"login":"author"},"merged":false,"commits":1,"labels":[]}'
  fi
}
# `ls-tree` is how prepare tells a present regular-file prompt from an
# absent one (AT-2520); every other call stands in for content.
git() {
  observe git "$*"
  if [ "$1" = "ls-tree" ]; then printf '100644\n'; else printf 'prompt\n'; fi
}
bash() { observe bash "$*"; printf 'diff\n' > pr.diff; }
python3() {
  observe python3 "$*"
  if [ "$1" = "-" ]; then
    printf 'author\nfeature\nmain\n'
  elif [[ "$1" == *filter_pr_diff.py ]]; then
    mkdir -p .review-context
    printf '%s\n' '{"policy_skipped":true,"excluded_count":1}' > .review-context/lens-ignore.json
  fi
}
python() { observe python "$*"; }
"""


@pytest.mark.parametrize("cross", [True, False], ids=["cross", "same-repo"])
@pytest.mark.parametrize(
    "name,job,step",
    target_steps(),
    ids=lambda value: value.get("name", "job") if isinstance(value, dict) else value,
)
def test_runtime_target_coordinates(tmp_path, name, job, step, cross):
    target_input = TARGET if cross else ""
    resolved = target_input or CALLER
    scripts = tmp_path / ".ai-dev-pr-review/.github/scripts"
    scripts.mkdir(parents=True)
    for filename in ("extract_pr_diff.sh", "filter_pr_diff.py"):
        (scripts / filename).touch()
    trusted = tmp_path / "runner-temp/ai-dev-pr-review/.github/scripts"
    trusted.mkdir(parents=True)
    (trusted / "post_inline_comments.py").touch()
    (tmp_path / "review-claude.json").write_text("{}")
    capture = tmp_path / "calls.tsv"
    env = os.environ | {
        "CAPTURE": str(capture),
        "GITHUB_OUTPUT": str(tmp_path / "output"),
        "GITHUB_STEP_SUMMARY": str(tmp_path / "summary"),
        "RUNNER_TEMP": str(tmp_path / "runner-temp"),
        "PR_NUMBER": "17",
        "PR_SIZE_LIMIT": "1",  # Exercise the size-skip comment API too.
        "PR_SIZE_WARN_PERCENT": "80",  # The step's `vars.X || '80'` default.
        "PR_AUTHOR": "author",
        "BASE_REF": "",
        "HEAD_REF": "feature",
        "HEAD_SHA": SHA,
        "CROSS_REPO": "true",  # Exercise the explicit API path in both controls.
        "PR_LABELS_JSON": "[]",
        "PR_MERGED": "false",
        "MERGE_COMMIT_SHA": "",
        "PR_COMMITS": "1",
        "LABELS": "",
        "SYSTEM_PROMPT_PATH": ".prompts/system.md",
        "CHECKLIST_PATH": ".prompts/checklist.md",
        "GIT_AUTH_TOKEN": "not-a-token",
        "REVIEWER": "claude",
    }
    # Stage 1: workflow/job env and step env evaluation.
    for key, expression in job.get("env", {}).items():
        assert expression == "${{ inputs.target_repo || github.repository }}"
        env[key] = resolved
    env["GITHUB_REPOSITORY"] = resolved
    # Stage 2: ScriptHandler exports github context AFTER the YAML env.
    env["GITHUB_REPOSITORY"] = CALLER
    # The run body is not rewritten except GitHub's expression evaluation.
    script = re.sub(r"\$\{\{.*?\}\}", SHA, step["run"])
    result = subprocess.run(
        ["bash", "-e", "-o", "pipefail", "-c", STUBS + script],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, (name, step["name"], result.stderr)
    calls = [line.split("\t", 2) for line in capture.read_text().splitlines()]
    assert calls, "The actual run body must reach an API/helper positive control"
    for command, repository, arguments in calls:
        assert repository == resolved, (step["name"], command, repository, resolved)
        if command == "gh":
            if arguments.startswith("api "):
                assert f"repos/{resolved}/pulls/17" in arguments
            else:
                assert f"--repo {resolved}" in arguments
    if step["name"] == "Aggregate and post verdict":
        assert any("aggregate_reviews.py" in args for _, _, args in calls)
    if step["name"] == "Post inline comments":
        assert any(
            str(trusted / "post_inline_comments.py") in args for _, _, args in calls
        )


@pytest.mark.parametrize("missing", ["selector", "adapter"])
def test_missing_runtime_binding_is_detected(tmp_path, missing):
    name, job, step = target_steps()[0]
    job = dict(job)
    step = dict(step)
    if missing == "selector":
        job.pop("env")
    else:
        step["run"] = step["run"].replace(
            'export GITHUB_REPOSITORY="${LENS_TARGET_REPOSITORY:?missing resolved target repository}"\n',
            "",
        )
    with pytest.raises(AssertionError):
        test_runtime_target_coordinates(tmp_path, name, job, step, True)
