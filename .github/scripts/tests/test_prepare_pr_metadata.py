"""Tests for the PR metadata prepare hands to the reviewers (AT-2086).

The `Resolve PR refs` step of base-ai-review-prepare.yml is executed here as
the shell script it is, against a stubbed `gh`, so the values the workflow
would really export are observed rather than asserted about.

Two things are being pinned down.

1. The dispatch path must not report the BASE branch name as the PR's head
   ref. `github.head_ref` is empty without a pull_request payload, and the
   old fallback filled the metadata block with a branch name that was wrong
   rather than absent.

2. The author must arrive in the spelling the rest of the system tests. One
   PR has three incompatible representations -- REST/webhook says
   `dependabot[bot]`, raw GraphQL says `dependabot`, `gh pr view --json
   author` renders `app/dependabot` -- and aggregate_reviews.py compares
   against `dependabot[bot]`. The fixtures below carry two of those shapes
   for the same PR precisely so a test can tell which surface was read.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path
from typing import Any

import pytest
import yaml

SCRIPT_DIR = Path(__file__).resolve().parent.parent
WORKFLOW = (
    SCRIPT_DIR.parents[0] / "workflows" / "base-ai-review-prepare.yml"
)

requires_jq = pytest.mark.skipif(shutil.which("jq") is None, reason="jq not installed")

_EXPRESSION = re.compile(r"\$\{\{(.+?)\}\}")

# The same pull request in two representations. `user.login` and
# `author.login` disagree because one is the API object and the other is a
# display form; a fixture that carried only one shape could not show that a
# fix which reads the wrong surface is broken.
_DEPENDABOT_REST = {
    "base": {"ref": "main"},
    "head": {
        "sha": "4df6c3199ff9b1d8f0f7f2a05e6c8b1d3e5a7c90",
        "ref": "dependabot/pip/urllib3-2.2.2",
        "label": "ignite-corp:dependabot/pip/urllib3-2.2.2",
    },
    "user": {"login": "dependabot[bot]"},
}
_DEPENDABOT_GH_PR_VIEW = {
    "baseRefName": "main",
    "headRefOid": "4df6c3199ff9b1d8f0f7f2a05e6c8b1d3e5a7c90",
    "headRefName": "dependabot/pip/urllib3-2.2.2",
    "author": {"login": "app/dependabot", "is_bot": True},
}

# A fork PR: `head.label` is owner-prefixed, `head.ref` is the bare branch
# name -- the value the pull_request path yields.
_FORK_REST = {
    "base": {"ref": "main"},
    "head": {
        "sha": "5c4c62e7a1b0c9d8e7f6a5b4c3d2e1f0a9b8c7d6",
        "ref": "fix-the-thing",
        "label": "someuser:fix-the-thing",
    },
    "user": {"login": "someuser"},
}


def _steps() -> list[dict[str, Any]]:
    workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    return workflow["jobs"]["prepare"]["steps"]


def _step(name: str) -> dict[str, Any]:
    for step in _steps():
        if step.get("name") == name:
            return step
    raise AssertionError(f"step not found in {WORKFLOW.name}: {name}")


def _render(script: str, context: dict[str, str]) -> str:
    """Substitute ``${{ ... }}`` expressions with the values a run would see.

    Unmapped expressions raise: a test must state every context value it is
    standing in for, so a new expression cannot be silently evaluated as an
    empty string.
    """

    def replace(match: re.Match[str]) -> str:
        expression = match.group(1).strip()
        if expression not in context:
            raise AssertionError(f"unmapped workflow expression: {expression}")
        return context[expression]

    return _EXPRESSION.sub(replace, script)


def _write_gh_stub(tmp_path: Path) -> Path:
    """A `gh` that serves both surfaces and records how it was called."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    stub = bin_dir / "gh"
    stub.write_text(
        textwrap.dedent(
            """\
            #!/bin/sh
            printf '%s\\n' "$*" >> "$GH_CALL_LOG"
            case "$1" in
              api) cat "$REST_FIXTURE" ;;
              pr) cat "$GH_PR_VIEW_FIXTURE" ;;
              *) echo "unexpected gh invocation: $*" >&2; exit 1 ;;
            esac
            """
        ),
        encoding="utf-8",
    )
    stub.chmod(0o755)
    return bin_dir


def _run_refs_step(
    tmp_path: Path,
    *,
    context: dict[str, str],
    rest: dict[str, Any] | None = None,
    gh_pr_view: dict[str, Any] | None = None,
) -> tuple[dict[str, str], list[str]]:
    """Run the `Resolve PR refs` script; return (step outputs, gh calls)."""
    step = _step("Resolve PR refs")
    script = _render(step["run"], context)

    rest_fixture = tmp_path / "rest.json"
    rest_fixture.write_text(json.dumps(rest or _DEPENDABOT_REST), encoding="utf-8")
    view_fixture = tmp_path / "gh-pr-view.json"
    view_fixture.write_text(
        json.dumps(gh_pr_view or _DEPENDABOT_GH_PR_VIEW), encoding="utf-8"
    )
    call_log = tmp_path / "gh-calls.log"
    call_log.touch()
    github_output = tmp_path / "github_output"
    github_output.touch()

    env = {
        "PATH": f"{_write_gh_stub(tmp_path)}{os.pathsep}{os.environ['PATH']}",
        "GITHUB_OUTPUT": str(github_output),
        "GH_CALL_LOG": str(call_log),
        "REST_FIXTURE": str(rest_fixture),
        "GH_PR_VIEW_FIXTURE": str(view_fixture),
    }
    for key, value in step["env"].items():
        env[key] = _render(str(value), context)

    result = subprocess.run(
        ["bash", "-euo", "pipefail", "-c", script],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr

    outputs: dict[str, str] = {}
    for line in github_output.read_text(encoding="utf-8").splitlines():
        if line:
            key, _, value = line.partition("=")
            outputs[key] = value
    calls = [line for line in call_log.read_text(encoding="utf-8").splitlines() if line]
    return outputs, calls


def _dispatch_context(pr_number: str = "35") -> dict[str, str]:
    """workflow_dispatch: no pull_request payload, so every ref is empty."""
    return {
        "github.token": "gh-token",
        "github.base_ref": "",
        "github.head_ref": "",
        "github.event.pull_request.user.login": "",
        "github.event.pull_request.head.sha": "",
        "inputs.pr_number || github.event.pull_request.number": pr_number,
        "github.repository": "ignite-corp/ai-dev-pr-review",
    }


def _pull_request_context(
    *,
    base_ref: str = "main",
    head_ref: str = "dependabot/pip/urllib3-2.2.2",
    author: str = "dependabot[bot]",
    head_sha: str = "4df6c3199ff9b1d8f0f7f2a05e6c8b1d3e5a7c90",
) -> dict[str, str]:
    return {
        "github.token": "gh-token",
        "github.base_ref": base_ref,
        "github.head_ref": head_ref,
        "github.event.pull_request.user.login": author,
        "github.event.pull_request.head.sha": head_sha,
        "inputs.pr_number || github.event.pull_request.number": "35",
        "github.repository": "ignite-corp/ai-dev-pr-review",
    }


@requires_jq
class TestDispatchPath:
    """A dispatched run must describe the PR, not the ref it was dispatched on."""

    def test_head_ref_is_the_pr_head_not_the_base_branch(self, tmp_path: Path) -> None:
        outputs, _ = _run_refs_step(tmp_path, context=_dispatch_context())
        assert outputs["head_ref"] == "dependabot/pip/urllib3-2.2.2"
        assert outputs["head_ref"] != outputs["base_ref"]

    def test_author_is_the_webhook_spelling_not_the_display_form(
        self, tmp_path: Path
    ) -> None:
        # `app/dependabot` is what `gh pr view --json author` would have
        # returned for this same PR; it fails aggregate_reviews.py's equality
        # test exactly as silently as the empty string it replaced.
        outputs, calls = _run_refs_step(tmp_path, context=_dispatch_context())
        assert outputs["pr_author"] == "dependabot[bot]"
        assert outputs["pr_author"] != _DEPENDABOT_GH_PR_VIEW["author"]["login"]
        assert calls == ["api repos/ignite-corp/ai-dev-pr-review/pulls/35"]

    def test_fork_head_ref_is_bare_not_owner_prefixed(self, tmp_path: Path) -> None:
        # `.head.label` would read `someuser:fix-the-thing` -- a new false
        # head ref in place of the old one.
        outputs, _ = _run_refs_step(
            tmp_path, context=_dispatch_context(), rest=_FORK_REST
        )
        assert outputs["head_ref"] == "fix-the-thing"
        assert ":" not in outputs["head_ref"]
        assert outputs["pr_author"] == "someuser"

    def test_base_ref_and_head_sha_still_resolved(self, tmp_path: Path) -> None:
        outputs, _ = _run_refs_step(tmp_path, context=_dispatch_context())
        assert outputs["base_ref"] == "main"
        assert outputs["head_sha"] == _DEPENDABOT_REST["head"]["sha"]

    def test_unresolvable_fields_are_empty_not_a_wrong_value(
        self, tmp_path: Path
    ) -> None:
        rest = {
            "base": {"ref": "main"},
            "head": {"sha": "deadbeef", "ref": None},
            "user": None,
        }
        outputs, _ = _run_refs_step(tmp_path, context=_dispatch_context(), rest=rest)
        assert outputs["head_ref"] == ""
        assert outputs["pr_author"] == ""
        # Neither the literal "null" nor the base branch name.
        assert outputs["head_ref"] != "null"
        assert outputs["head_ref"] != outputs["base_ref"]


@requires_jq
class TestPullRequestPath:
    """The event-driven path must be byte-identical to its pre-fix behaviour."""

    def test_values_come_from_the_event_payload(self, tmp_path: Path) -> None:
        context = _pull_request_context()
        outputs, _ = _run_refs_step(tmp_path, context=context)
        assert outputs["base_ref"] == "main"
        assert outputs["head_ref"] == "dependabot/pip/urllib3-2.2.2"
        assert outputs["pr_author"] == "dependabot[bot]"
        assert outputs["head_sha"] == "4df6c3199ff9b1d8f0f7f2a05e6c8b1d3e5a7c90"

    def test_no_api_request_is_made(self, tmp_path: Path) -> None:
        # The rate-limit surface of the pull_request path is unchanged: the
        # resolution branch is not entered at all when github.base_ref is set.
        _, calls = _run_refs_step(tmp_path, context=_pull_request_context())
        assert calls == []

    def test_human_author_and_head_ref_pass_through(self, tmp_path: Path) -> None:
        context = _pull_request_context(
            head_ref="task/AT-2086", author="hyuk-hur", base_ref="main"
        )
        outputs, _ = _run_refs_step(tmp_path, context=context)
        assert outputs["head_ref"] == "task/AT-2086"
        assert outputs["pr_author"] == "hyuk-hur"


class TestMetadataWiring:
    """The reviewers' metadata block must read the resolved values."""

    def _metadata_env(self) -> dict[str, str]:
        return _step("Extract diff and context")["env"]

    def test_author_and_head_ref_come_from_the_refs_step(self) -> None:
        env = self._metadata_env()
        assert env["PR_AUTHOR"] == "${{ steps.refs.outputs.pr_author }}"
        assert env["HEAD_REF"] == "${{ steps.refs.outputs.head_ref }}"

    def test_head_ref_no_longer_falls_back_to_the_base_ref(self) -> None:
        # The defect verbatim: `${{ github.head_ref || steps.refs.outputs.base_ref }}`.
        assert "base_ref" not in self._metadata_env()["HEAD_REF"]

    def test_author_is_exposed_as_a_job_output(self) -> None:
        # The aggregate's PR_AUTHOR consumes this; without it a dispatched
        # dependabot PR is scored against the human thresholds.
        workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
        assert (
            workflow["jobs"]["prepare"]["outputs"]["pr_author"]
            == "${{ steps.refs.outputs.pr_author }}"
        )


def _verdict_for_author(author: str) -> str:
    """Verdict for two reviewers and ONE critical issue, at the given author.

    Run in a subprocess because aggregate_reviews resolves its thresholds at
    import time. Nothing is patched: the threshold selection is visible only
    through the verdict it produces, which is the point -- at the human
    threshold of 1 the single critical blocks, at the dependabot threshold of
    2 it does not.
    """
    program = textwrap.dedent(
        """\
        import json, sys
        from aggregate_reviews import REVIEWER_NAMES, apply_verdict_rules

        def review(name, issues):
            return {
                "summary": name,
                "status": "ok",
                "early_exit": False,
                "issues": [
                    {
                        "severity": s,
                        "file": "foo.py",
                        "line": 1,
                        "description": "test issue",
                        "suggestion": None,
                        "reviewer": name,
                    }
                    for s in issues
                ],
            }

        names = list(REVIEWER_NAMES)
        reviews = {n: None for n in REVIEWER_NAMES}
        reviews[names[0]] = review(names[0], ["critical"])
        reviews[names[1]] = review(names[1], [])
        verdict, _, _ = apply_verdict_rules(reviews)
        sys.stdout.write(verdict)
        """
    )
    result = subprocess.run(
        [sys.executable, "-c", program],
        env={
            "PATH": os.environ["PATH"],
            "PYTHONPATH": str(SCRIPT_DIR),
            "PR_AUTHOR": author,
        },
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


@requires_jq
class TestResolvedAuthorSelectsThresholds:
    """The value the workflow exports must reach the dependabot thresholds.

    Asserting the author string alone would not show this: the previous
    attempt at this fix did exactly that and shipped a value that failed the
    comparison. Here the same critical issue is scored twice and the
    threshold choice is read off the outcome.
    """

    def test_resolved_dispatch_author_relaxes_the_critical_threshold(
        self, tmp_path: Path
    ) -> None:
        outputs, _ = _run_refs_step(tmp_path, context=_dispatch_context())
        assert _verdict_for_author(outputs["pr_author"]) == "approve"

    def test_display_form_author_would_block_at_the_human_threshold(self) -> None:
        # Why the source matters: `gh pr view`'s spelling scores a dependabot
        # PR as a human one, which is what the empty string did before.
        assert (
            _verdict_for_author(_DEPENDABOT_GH_PR_VIEW["author"]["login"])
            == "request_changes"
        )

    def test_empty_author_blocks_at_the_human_threshold(self) -> None:
        # The size-skip and prepare-failure paths leave the output empty.
        # Human thresholds there are the strict direction, and deliberate.
        assert _verdict_for_author("") == "request_changes"

    def test_human_author_blocks_at_the_human_threshold(self, tmp_path: Path) -> None:
        outputs, _ = _run_refs_step(
            tmp_path, context=_pull_request_context(author="hyuk-hur")
        )
        assert _verdict_for_author(outputs["pr_author"]) == "request_changes"
