"""Helpers shared by the test modules.

Most are for the modules that run a tool rather than import a module;
``LiveGhCalled`` is the type the live-`gh` ban in `tests/conftest.py`
raises, kept here so a test can assert on it.

Here rather than in `tests/conftest.py` because `pythonpath` reaches
`.github/scripts` and not `.github/scripts/tests`: importing a name out of
conftest.py works only under pytest's default `prepend` import mode, and
breaks under `--import-mode=importlib` or once a `tests/__init__.py` exists
(both measured). conftest.py is pytest's fixture and hook file, not an
importable helper module. Shared fixtures and gate helpers live here for the
same reason: importing a sibling test module also depends on the import mode.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from collections.abc import Callable
from functools import partial
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from typing import Any

import pytest
import yaml

import aggregate_reviews
import post_inline_comments
from post_inline_comments import normalize_body
from github_pr_support import REVIEWER_NAMES

# Exit codes for a run that happened: clean, and findings. Anything else is
# the tool refusing to run, and its reason is on stderr, not in its output.
RAN = (0, 1)


def tool(name: str) -> str:
    """The path of `name`, or a loud failure naming what installs it."""
    binary = shutil.which(name)
    if binary is None:
        pytest.fail(
            f"{name} is not on PATH; pip install -r .github/scripts/requirements-dev.txt"
        )
    return binary


def ran(name: str, argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
    """Run `name` and fail loudly, with its own stderr, if it refused to run."""
    completed = subprocess.run(
        [tool(name), *argv], capture_output=True, text=True, check=False, **kwargs
    )
    if completed.returncode not in RAN:
        pytest.fail(f"{name} exited {completed.returncode}: {completed.stderr}")
    return completed


class LiveGhCalled(BaseException):
    """Raised by the ``_no_live_gh`` fixture when a test reaches `gh`.

    A ``BaseException``, not an ``Exception``: scripts this suite drives
    catch ``Exception`` around paths that can reach `gh` (review_pr_local.py
    turns a reviewer's raise into a degraded result), and an ``Exception``
    subclass would be absorbed there and leave the test green.
    """


# Pyright's strict unused-function check needs an explicit export marker for
# shared helpers whose fixture-facing names retain the original underscore.
# Keep the complete shared surface here, including the original public helpers.
__all__ = [
    "DRILL",
    "FENCE",
    "LiveGhCalled",
    "RAN",
    "SCRIPT_DIR",
    "SKILL",
    "WORKFLOW",
    "_BELOW_CUTOFF",
    "_BOT",
    "_GIT_ENV",
    "_HEAD",
    "_OLD_HEAD",
    "_PAST_CUTOFF",
    "_PR",
    "_REAL_RUN",
    "_RUN_ID",
    "_TWO_MINORS",
    "_commit",
    "_git",
    "_issue",
    "_render",
    "_run_aggregate",
    "_run_post",
    "_step",
    "_write_review",
    "gate_function",
    "is_allowed_bot",
    "ran",
    "run_folded_gate",
    "tool",
]

SCRIPT_DIR = Path(__file__).resolve().parent
DRILL = SCRIPT_DIR / "gate_headline_drill.sh"
# The one anchor for the plugin SKILL.md path shared by the gate tests.
SKILL = (
    SCRIPT_DIR.parents[1]
    / "plugins"
    / "pr-response-cycle"
    / "skills"
    / "pr-response-cycle"
    / "SKILL.md"
)


# Run the drill's own extract() so every gate test uses the same extraction.
_EXTRACT_FN = re.compile(r"^extract\(\) \{.*?^\}$", re.M | re.S)


def gate_function(name: str) -> str:
    """The function as gate_headline_drill.sh extracts it: the drill's own extract(), run."""
    lifted = _EXTRACT_FN.search(DRILL.read_text(encoding="utf-8"))
    assert lifted is not None, (
        "the drill no longer defines extract() in the shape this lifts"
    )
    result = subprocess.run(
        [
            "bash",
            "-c",
            lifted.group(0) + '\nextract "$1" "$2"',
            "bash",
            str(SKILL),
            name,
        ],
        capture_output=True,
        text=True,
        check=True,
        timeout=60,
    )
    assert result.stdout.startswith(f"{name}() {{\n"), (
        f"the drill's extract() finds no {name} in SKILL.md: {result.stderr}"
    )
    return result.stdout


WORKFLOW = SCRIPT_DIR.parents[0] / "workflows" / "base-ai-review-prepare.yml"


_EXPRESSION = re.compile(r"\$\{\{(.+?)\}\}")


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


_GIT_ENV = {
    **os.environ,
    "GIT_AUTHOR_NAME": "fixture",
    "GIT_AUTHOR_EMAIL": "fixture@example.invalid",
    "GIT_COMMITTER_NAME": "fixture",
    "GIT_COMMITTER_EMAIL": "fixture@example.invalid",
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_NOSYSTEM": "1",
}


def _git(cwd: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=cwd,
        env=_GIT_ENV,
        capture_output=True,
        text=True,
        timeout=60,
        check=True,
    )
    return result.stdout.strip()


def _commit(cwd: Path, message: str, files: dict[str, str]) -> str:
    for name, content in files.items():
        path = cwd / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    _git(cwd, "add", "-A")
    _git(cwd, "commit", "-q", "-m", message)
    return _git(cwd, "rev-parse", "HEAD")


# Three right-side lines in a.py, so up to three in-range findings.
_DIFF = "+++ b/a.py\n@@ -1,3 +1,3 @@\n+line1\n+line2\n+line3\n"


# Completed rounds: the round in progress is one more, past the cutoff of 5.
_PAST_CUTOFF = 6


_BELOW_CUTOFF = 0


_HEAD = "0123456789abcdef0123456789abcdef01234567"


_OLD_HEAD = "fedcba9876543210fedcba9876543210fedcba98"


# What the posting step and the aggregate both run as on Actions.
_BOT = "github-actions[bot]"


_RUN_ID = "local-" + "a" * 32


_REAL_RUN = subprocess.run


def _set_run_identity(monkeypatch: pytest.MonkeyPatch, identity: str) -> None:
    if identity.startswith("actions-"):
        monkeypatch.delenv("INLINE_REVIEW_RUN_ID", raising=False)
        _, run, attempt = identity.split("-")
        monkeypatch.setenv("GITHUB_ACTIONS", "true")
        monkeypatch.setenv("GITHUB_RUN_ID", run)
        monkeypatch.setenv("GITHUB_RUN_ATTEMPT", attempt)
    else:
        monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
        monkeypatch.setenv("INLINE_REVIEW_RUN_ID", identity)


def _issue(line: int, description: str, severity: str = "minor") -> dict[str, Any]:
    return {
        "severity": severity,
        "file": "a.py",
        "line": line,
        "description": description,
        "suggestion": None,
    }


# Distinct wording, so batch-internal dedup keeps both.
_TWO_MINORS = [
    _issue(1, "the loader never closes the handle it opens"),
    _issue(2, "a stale cache key survives the rename"),
]


class _PR:
    """The surfaces of a PR both scripts touch, held as data.

    ``comments`` is what ``gh pr comment`` appended -- the fold comments --
    and ``inline`` what the Reviews API received, one entry per inline
    comment with the commit it was posted against. Both are read back by
    the aggregate's fetchers, stubbed to serve from here.
    """

    def __init__(self) -> None:
        self.comments: list[dict[str, str]] = []
        self.inline: list[dict[str, Any]] = []

    def add_comment(
        self, body: str, login: str = _BOT, author_type: str | None = None
    ) -> None:
        self.comments.append(
            {
                "login": login,
                "body": body,
                "author_type": author_type
                or ("Bot" if login.endswith("[bot]") else "User"),
            }
        )

    def fold_comment_bodies(self) -> list[str]:
        return [
            c["body"]
            for c in self.comments
            if "round-cutoff-" in c["body"] or "inline-fallback-" in c["body"]
        ]


def _write_review(work: Path, name: str, issues: list[dict[str, Any]]) -> Path:
    path = work / f"review-{name}.json"
    review = {
        "summary": f"{name} review",
        "status": "ok",
        "early_exit": False,
        "issues": issues,
    }
    path.write_text(json.dumps(review), encoding="utf-8")
    return path


def _completed_round_count(completed_rounds: int, repo: str, pr: str) -> int:
    return completed_rounds


def _run_post(
    monkeypatch: pytest.MonkeyPatch,
    work: Path,
    pr: _PR,
    name: str,
    *,
    completed_rounds: int,
    head_sha_fails: bool = False,
    inline_fails: bool = False,
    existing_threads: list[dict[str, Any]] | None = None,
    bot_login: str = _BOT,
    fold_post_fails: bool = False,
    dedup_timeout: bool = False,
    run_identity: str = _RUN_ID,
    post_head_sha: str = _HEAD,
    round_counter: Callable[[str, str], int] | None = None,
) -> dict[str, Any]:
    """Run post_inline_comments.main() for one reviewer against ``pr``.

    ``head_sha_fails`` / ``inline_fails`` drive the two routes to
    ``post_fallback``, which puts every finding in one PR comment instead of
    inline threads -- the second way findings leave the thread list.
    """
    posted: dict[str, Any] = {"inline": [], "folded": [], "fallback": []}
    _set_run_identity(monkeypatch, run_identity)
    (work / "pr.diff").write_text(_DIFF, encoding="utf-8")
    monkeypatch.setenv("PR_NUMBER", "179")
    monkeypatch.setenv("GITHUB_REPOSITORY", "o/r")
    monkeypatch.setenv("BOT_LOGIN", bot_login)
    threads = [
        {"path": th["path"], "line": th["line"], "body": normalize_body(th["body"])}
        for th in (existing_threads or [])
    ]

    def fetch_threads(repo: str, pr: str) -> list[dict[str, Any]]:
        return threads

    monkeypatch.setattr(post_inline_comments, "fetch_existing_threads", fetch_threads)
    monkeypatch.setattr(
        post_inline_comments,
        "fetch_round_count",
        round_counter or partial(_completed_round_count, completed_rounds),
    )

    def head_sha(pr_number: str) -> str:
        if head_sha_fails:
            raise subprocess.CalledProcessError(1, ["gh", "pr", "view"])
        return post_head_sha

    monkeypatch.setattr(post_inline_comments, "get_pr_head_sha", head_sha)

    def fake_gh(cmd: list[str], **kwargs: Any) -> SimpleNamespace:
        assert "--arg" not in cmd, "gh api does not accept jq's --arg option"
        if "--input" in cmd and cmd[2].endswith("/reviews"):
            # The real post_inline_review: its payload is the review.
            if inline_fails:
                return SimpleNamespace(returncode=1, stdout="", stderr="boom")
            payload = json.loads(kwargs["input"])
            posted["inline"] = payload["comments"]
            for c in payload["comments"]:
                pr.inline.append(
                    {
                        "login": bot_login,
                        "author_type": "Bot" if bot_login.endswith("[bot]") else "User",
                        "posted_commit": payload["commit_id"],
                        "body": c["body"],
                    }
                )
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if cmd[:2] == ["gh", "api"]:
            if dedup_timeout:
                raise subprocess.TimeoutExpired(cmd, 60)
            # Marker-dedup check in _post_folded_comment, against the fake PR:
            # the marker as the whole first line, as the real jq does.
            assert "--paginate" in cmd
            query = cmd[cmd.index("--jq") + 1]
            # Execute the production query; do not duplicate its matching logic.
            pages = [pr.comments[i : i + 2] for i in range(0, len(pr.comments), 2)]
            counts: list[str] = []
            for page in pages or [[]]:
                payload = [
                    {
                        "user": {"login": c["login"], "type": c["author_type"]},
                        "body": c["body"],
                    }
                    for c in page
                ]
                result = _REAL_RUN(
                    ["jq", "--raw-output", query],
                    input=json.dumps(payload),
                    capture_output=True,
                    text=True,
                )
                assert result.returncode == 0, result.stderr
                counts.append(result.stdout)
            return SimpleNamespace(returncode=0, stdout="".join(counts), stderr="")
        if cmd[:3] == ["gh", "pr", "comment"]:
            if fold_post_fails:
                return SimpleNamespace(returncode=1, stdout="", stderr="post failed")
            pr.add_comment(kwargs["input"], login=bot_login)
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        raise AssertionError(f"unexpected gh call: {cmd}")

    monkeypatch.setattr(subprocess, "run", fake_gh)

    real_summary = post_inline_comments.post_cutoff_summary
    real_fallback = post_inline_comments.post_fallback

    def record_summary(*args: Any, **kwargs: Any) -> None:
        posted["folded"] = args[4]
        real_summary(*args, **kwargs)

    def record_fallback(*args: Any, **kwargs: Any) -> None:
        posted["fallback"] = args[4]
        real_fallback(*args, **kwargs)

    monkeypatch.setattr(post_inline_comments, "post_cutoff_summary", record_summary)
    monkeypatch.setattr(post_inline_comments, "post_fallback", record_fallback)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "post_inline_comments.py",
            "--issues",
            str(work / f"review-{name}.json"),
            "--diff",
            str(work / "pr.diff"),
            "--reviewer",
            name,
        ],
    )
    post_inline_comments.main()
    return posted


def _run_aggregate(
    monkeypatch: pytest.MonkeyPatch,
    work: Path,
    pr: _PR,
    *,
    completed_rounds: int,
    head_sha: str = _HEAD,
    bot_login: str = _BOT,
    inline_bot_login: str | None = None,
    fold_fetcher: Callable[..., list[dict[str, Any]]] | None = None,
    inline_fetcher: Callable[..., list[dict[str, Any]]] | None = None,
    run_identity: str = _RUN_ID,
    round_counter: Callable[[str, str], int] | None = None,
) -> tuple[dict[str, Any], str]:
    """Run aggregate_reviews.main() over the verdict files, reading ``pr``.

    Returns what it wrote to $GITHUB_OUTPUT (the roster parsed) and the
    headline line of the verdict it would have posted.
    """
    output_path = work / "gh-output"
    _set_run_identity(monkeypatch, run_identity)
    monkeypatch.chdir(work)
    monkeypatch.setenv("GITHUB_OUTPUT", str(output_path))
    monkeypatch.setenv("PR_NUMBER", "179")
    monkeypatch.setenv("GITHUB_REPOSITORY", "o/r")
    monkeypatch.setenv("HEAD_SHA", head_sha)
    for name in REVIEWER_NAMES:
        monkeypatch.setenv(f"REVIEWER_RESULT_{name.upper()}", "success")

    def count_rounds(repo: str, pr_number: str) -> int:
        return completed_rounds

    def fetch_folded(repo: str, pr_number: str) -> list[dict[str, str]]:
        return list(pr.comments)

    def fetch_inline(repo: str, pr_number: str) -> list[dict[str, Any]]:
        return list(pr.inline)

    def fetch_dispositions(repo: str, pr_number: str) -> list[dict[str, Any]]:
        return []

    monkeypatch.setattr(aggregate_reviews, "BOT_LOGIN", bot_login)
    monkeypatch.setattr(
        aggregate_reviews, "INLINE_BOT_LOGIN", inline_bot_login or bot_login
    )
    monkeypatch.setattr(
        aggregate_reviews,
        "fetch_round_count",
        round_counter or count_rounds,
    )
    monkeypatch.setattr(
        aggregate_reviews,
        "_fetch_fold_comments",
        fold_fetcher or fetch_folded,
    )
    monkeypatch.setattr(
        aggregate_reviews,
        "_fetch_inline_posts",
        inline_fetcher or fetch_inline,
    )
    # No earlier thread disposes of anything here (AT-2843).
    monkeypatch.setattr(
        aggregate_reviews, "_fetch_disposition_threads", fetch_dispositions
    )
    # _head_is_stale compares HEAD_SHA with the live head; the fake PR's is HEAD_SHA.
    monkeypatch.setattr(aggregate_reviews, "_head_is_stale", lambda: False)
    with patch("aggregate_reviews.post_verdict") as post_verdict:
        aggregate_reviews.main()
    comment = post_verdict.call_args.args[0]
    headline = next(
        line for line in comment.splitlines() if line.startswith("**Result:")
    )
    emitted = dict(
        line.split("=", 1)
        for line in output_path.read_text(encoding="utf-8").splitlines()
    )
    return {**emitted, "roster": json.loads(emitted["reviewer_roster"])}, headline


_BOT_SUFFIX_RE = re.compile(r"\[bot\]$")


def is_allowed_bot(actor: str, allowed_bots: str) -> bool:
    """Port of the pinned action's ``isAllowedBot`` (see module docstring)."""
    trimmed = allowed_bots.strip()
    if trimmed == "*":
        return True
    if not trimmed:
        return False

    allowed_list = [
        _BOT_SUFFIX_RE.sub("", bot.strip().lower()) for bot in trimmed.split(",")
    ]
    allowed_list = [bot for bot in allowed_list if bot]

    normalized_actor = _BOT_SUFFIX_RE.sub("", actor.lower())
    return normalized_actor in allowed_list


FENCE = re.compile(r"^\s*(?:```|~~~)")


def run_folded_gate(
    body: str,
    reply: str = "",
    *,
    source_author: str = "github-actions[bot]",
    source_type: str = "Bot",
    reply_author: str = "hyuk-hur",
    reply_type: str = "User",
    source_at: str = "2026-10-22T00:00:00Z",
) -> subprocess.CompletedProcess[str]:
    assert shutil.which("jq"), "jq must be on PATH: cutoff_findings is a jq program"
    comments = [
        {
            "id": 1,
            "author": source_author,
            "type": source_type,
            "created_at": source_at,
            "body": body,
        }
    ]
    if reply:
        comments.append(
            {
                "id": 2,
                "author": reply_author,
                "type": reply_type,
                "created_at": "2026-10-22T00:01:00Z",
                "body": reply,
            }
        )
    return subprocess.run(
        [
            "bash",
            "-c",
            gate_function("finding_key")
            + "\n"
            + gate_function("cutoff_findings")
            + '\ncutoff_findings "2026-10-22T00:00:00Z"',
        ],
        input=json.dumps(comments),
        text=True,
        capture_output=True,
        timeout=30,
    )
