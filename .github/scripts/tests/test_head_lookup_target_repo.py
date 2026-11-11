"""AT-3428: the posting step resolves the PR head in the target repository.

In the runner, gh's default repository is the runner's own checkout, so a
bare `gh pr view N` looked up the runner's PR N and failed. Every round on
a runner target then took the head-less inline fallback: on
hyuk-hur/dev-dotfiles#254 rounds r1-r6 are all `inline-fallback-claude-*`
comments, and the claude job log for run 42981508444 reads "failed to get
PR head SHA".
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

import github_pr_support
import post_inline_comments as posting

_TARGET = "hyuk-hur/dev-dotfiles"
_HEAD = "7532323d5764b2692a8446db682910c38f9c616e"


def _runner_gh(calls: list[list[str]]):
    """gh as the runner sees it: `pr view` finds the PR only via --repo."""

    def run(cmd: list[str], **kwargs: Any) -> SimpleNamespace:
        calls.append(cmd)
        if cmd[:3] == ["gh", "pr", "view"]:
            if "--repo" in cmd and cmd[cmd.index("--repo") + 1] == _TARGET:
                return SimpleNamespace(returncode=0, stdout=_HEAD + "\n", stderr="")
            if kwargs.get("check"):
                raise posting.subprocess.CalledProcessError(1, cmd)
            return SimpleNamespace(returncode=1, stdout="", stderr="no PR")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    return run


def test_head_lookup_names_the_target_repository(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The step's own checkout is the runner; the target differs from it.
    monkeypatch.setenv("GITHUB_REPOSITORY", "ignite-corp/ai-dev-pr-review-runner")
    calls: list[list[str]] = []
    monkeypatch.setattr(github_pr_support.subprocess, "run", _runner_gh(calls))
    assert github_pr_support.get_pr_head_sha("254", _TARGET) == _HEAD
    assert calls[0][calls[0].index("--repo") + 1] == _TARGET


def test_head_lookup_refuses_to_guess_the_repository() -> None:
    # A caller that names no repository would get the runner's PR N.
    with pytest.raises(ValueError):
        github_pr_support.get_pr_head_sha("254", "")


def test_runner_round_posts_inline_not_the_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[list[str]] = []
    monkeypatch.setattr(github_pr_support.subprocess, "run", _runner_gh(calls))
    monkeypatch.setattr(posting.subprocess, "run", _runner_gh(calls))
    comment = {
        "path": "a.py",
        "line": 1,
        "side": "RIGHT",
        "body": "+ **major** (claude): x",
    }
    posting.post_findings(_TARGET, "254", "claude", [comment], 1, None)
    assert not any(cmd[:3] == ["gh", "pr", "comment"] for cmd in calls)
    review = next(cmd for cmd in calls if cmd[:2] == ["gh", "api"])
    assert review[2] == f"repos/{_TARGET}/pulls/254/reviews"
