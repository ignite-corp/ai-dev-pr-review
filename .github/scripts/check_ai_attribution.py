#!/usr/bin/env python3
"""Fail a change whose commits or PR body carry AI attribution (AT-3651).

Commit d01d082 (PR #314) reached main with a Claude co-author trailer and a
session-link trailer. The coding harness asks sessions to add them; the
owner's rule forbids them, and nothing checked before merge. This runs in
self-review.yml's `guard` job, a required status check on main.

WHAT IS MATCHED (case-insensitive)
    co-authored-by-ai        a `Co-authored-by:` trailer line whose value
                             names Anthropic, has an anthropic.com address,
                             or is Claude alone, the claude[bot] App, or
                             Claude followed, after a space or hyphen, by a
                             model or product word (Opus, Sonnet, Haiku,
                             Fable, Code, Instant, AI, or a version number).
                             A human co-author, Claude Monet included,
                             passes.
    claude-session-trailer   a `Claude-Session:` trailer line
    claude-session-url       a claude.ai/code/session_ link anywhere
    generated-with-claude-code  "Generated with [Claude Code]" anywhere
    robot-generated-with     the robot emoji followed by "Generated with"

    Trailers are matched at the start of a line, so prose that names them
    mid-sentence passes.

WHAT IS READ
    pull_request, workflow_dispatch  the PR's commits (`pulls/N/commits`,
        which GitHub caps at 250) and its body, both read live from the API.
        The payload body is not used: a re-run replays the original payload,
        so a body fixed after the push would otherwise never pass.
    merge_group, push  the commits of base...head through the compare API.
        No body. Not a trigger of self-review.yml today; handled so that
        adding one does not need a second check.

    --pr N --head-sha SHA  merge-gate mode, run by the pr-response-cycle
        skill's merge gate right before the merge call, so a body edited
        after the last push is still read. Same reads as pull_request; the
        PR's head must be SHA and the commit list must contain it, else the
        head moved and the run exits 2.

    A range longer than the API returns, a push that creates a branch, an
    unsupported event, or a failed API call exits 2: not evaluated is not
    clean.

Output names the rule and the commit (or "PR body"), never the matched text:
the message is untrusted input, and echoing it would let it write workflow
commands.

Exit 0 when clean, 1 when attribution is found, 2 when the check could not
be evaluated.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

_FLAGS = re.IGNORECASE | re.MULTILINE
_CO_AUTHOR = re.compile(r"^[ \t]*co-authored-by:[ \t]*(?P<who>.*)$", _FLAGS)
_AI_CO_AUTHOR = re.compile(
    r"@anthropic\.com\b"
    r"|\banthropic\b"
    r"|^claude(?:\[bot\]|[ \t]*<|[ \t]*$"
    r"|[ \t-]+(?:opus|sonnet|haiku|fable|code|instant|ai|\d)\b)",
    re.IGNORECASE,
)
_RULES: dict[str, re.Pattern[str]] = {
    "claude-session-trailer": re.compile(r"^[ \t]*claude-session:", _FLAGS),
    "claude-session-url": re.compile(r"claude\.ai/code/session_", re.IGNORECASE),
    "generated-with-claude-code": re.compile(
        r"generated with \[claude code\]", re.IGNORECASE
    ),
    "robot-generated-with": re.compile("\U0001f916[ \t]*generated with", re.IGNORECASE),
}
# GitHub's documented ceiling for the PR commits endpoint, which does not page
# past it. Compare pages are joined in _gh_api, so no cap is named there.
API_COMMIT_CAP = 250
_NULL_SHA = re.compile(r"^0+$")
_PR_NUMBER = re.compile(r"^[1-9][0-9]*$")

Gh = Callable[[str], Any]


class GhError(Exception):
    """A `gh api` call failed or returned something unreadable."""


class NotEvaluated(Exception):
    """The input does not let the check reach a verdict."""


def find_attribution(text: str) -> list[str]:
    """Return the sorted names of the rules `text` breaks."""
    found = {name for name, rule in _RULES.items() if rule.search(text)}
    for match in _CO_AUTHOR.finditer(text):
        if _AI_CO_AUTHOR.search(match["who"].strip()):
            found.add("co-authored-by-ai")
    return sorted(found)


def _gh_api(path: str) -> Any:
    done = subprocess.run(
        ["gh", "api", "--paginate", "--slurp", path],
        capture_output=True,
        text=True,
        check=False,
    )
    if done.returncode != 0:
        raise GhError(f"gh api {path} exited {done.returncode}")
    try:
        pages = json.loads(done.stdout)
    except json.JSONDecodeError as exc:
        raise GhError(f"gh api {path} returned non-JSON: {exc}") from exc
    if not isinstance(pages, list):
        raise GhError(f"gh api {path} returned no page list")
    pages = cast(list[Any], pages)
    if len(pages) == 1:
        return pages[0]
    if all(isinstance(page, list) for page in pages):
        return [item for page in pages for item in cast(list[Any], page)]
    # The compare endpoint pages an object whose `commits` list is the part
    # that pages; the rest repeats on every page.
    if all(isinstance(page, dict) and "commits" in page for page in pages):
        objects = cast(list[dict[str, Any]], pages)
        return {
            **objects[0],
            "commits": [c for page in objects for c in page["commits"]],
        }
    raise GhError(f"gh api {path} returned {len(pages)} object pages")


def _messages(commits: Any) -> list[tuple[str, str]]:
    if not isinstance(commits, list):
        raise GhError("commit list is not a list")
    out: list[tuple[str, str]] = []
    for item in cast(list[Any], commits):
        try:
            out.append((str(item["sha"]), str(item["commit"]["message"])))
        except (KeyError, TypeError) as exc:
            raise GhError(f"commit entry without sha/message: {exc}") from exc
    return out


def _pr_sources(
    repo: str, number: str, gh: Gh, head_sha: str | None = None
) -> list[tuple[str, str]]:
    pr = gh(f"repos/{repo}/pulls/{number}")
    commits = _messages(gh(f"repos/{repo}/pulls/{number}/commits?per_page=100"))
    expected = int(pr["commits"])
    if len(commits) < expected:
        raise NotEvaluated(
            f"PR #{number} has {expected} commits but the API returned "
            f"{len(commits)} (it stops at {API_COMMIT_CAP})"
        )
    if head_sha is not None:
        # Both reads must describe the head the caller is about to merge. The
        # list must contain it, not end on it: its order is not promised to
        # be topological, and a commit listed besides the head is still read.
        pr_head = str(pr["head"]["sha"])
        listed = head_sha in {sha for sha, _ in commits}
        if pr_head != head_sha or not listed:
            raise NotEvaluated(
                f"PR #{number} head moved: expected {head_sha}, the PR reads "
                f"{pr_head} and its commit list {'has' if listed else 'lacks'} it"
            )
    sources = [(f"commit {sha[:7]}", message) for sha, message in commits]
    sources.append(("PR body", pr.get("body") or ""))
    return sources


def _range_sources(repo: str, base: str, head: str, gh: Gh) -> list[tuple[str, str]]:
    if not base or not head or _NULL_SHA.match(base):
        raise NotEvaluated(f"no base to compare against (base={base!r})")
    compare = gh(f"repos/{repo}/compare/{base}...{head}?per_page=100")
    commits = _messages(compare["commits"])
    total = int(compare["total_commits"])
    if len(commits) < total:
        raise NotEvaluated(
            f"{base[:7]}...{head[:7]}: the API "
            f"returned {len(commits)} of {total} commits"
        )
    return [(f"commit {sha[:7]}", message) for sha, message in commits]


def collect(
    event_name: str, event: dict[str, Any], repo: str, gh: Gh
) -> list[tuple[str, str]]:
    """Return (where, text) pairs for every text this event's change carries."""
    if event_name == "pull_request":
        return _pr_sources(repo, str(event["pull_request"]["number"]), gh)
    if event_name == "workflow_dispatch":
        number = str(event.get("inputs", {}).get("pr_number", "")).strip()
        if not _PR_NUMBER.match(number):
            raise NotEvaluated("workflow_dispatch pr_number is not a PR number")
        return _pr_sources(repo, number, gh)
    if event_name == "merge_group":
        group = event["merge_group"]
        return _range_sources(repo, group["base_sha"], group["head_sha"], gh)
    if event_name == "push":
        return _range_sources(repo, event["before"], event["after"], gh)
    raise NotEvaluated(f"event {event_name!r} is not one this check reads")


def main(argv: list[str] | None = None, gh: Gh = _gh_api) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument("--event-name", default=os.environ.get("GITHUB_EVENT_NAME"))
    parser.add_argument("--event-path", default=os.environ.get("GITHUB_EVENT_PATH"))
    parser.add_argument("--repo", default=os.environ.get("GITHUB_REPOSITORY"))
    parser.add_argument("--pr", help="merge-gate mode: the PR number, with --head-sha")
    parser.add_argument("--head-sha", help="merge-gate mode: the head being merged")
    args = parser.parse_args(argv)

    try:
        if args.pr is not None or args.head_sha is not None:
            if not (args.repo and args.head_sha and _PR_NUMBER.match(args.pr or "")):
                raise NotEvaluated("merge-gate mode needs --repo, --pr and --head-sha")
            sources = _pr_sources(args.repo, args.pr, gh, args.head_sha)
        else:
            if not (args.event_name and args.event_path and args.repo):
                raise NotEvaluated("event name, event path and repository are required")
            event = json.loads(Path(args.event_path).read_text(encoding="utf-8"))
            sources = collect(args.event_name, event, args.repo, gh)
    except (NotEvaluated, GhError, OSError, ValueError, KeyError, TypeError) as exc:
        print(f"::error::AI attribution check not evaluated: {exc}")
        return 2
    except Exception as exc:  # noqa: BLE001 -- exit 1 must only mean "found"
        print(
            f"::error::AI attribution check not evaluated: {type(exc).__name__}: {exc}"
        )
        return 2

    findings = [
        (where, rule) for where, text in sources for rule in find_attribution(text)
    ]
    for where, rule in findings:
        print(f"::error::{where}: {rule}")
    if findings:
        print(
            "AI attribution found (AT-3651). Remove the trailers with an "
            "interactive rebase or `git commit --amend`, edit the PR body, "
            "then push or re-run this job."
        )
        return 1
    commits = sum(1 for where, _ in sources if where.startswith("commit "))
    has_body = any(where == "PR body" for where, _ in sources)
    print(
        f"no AI attribution in {commits} commit(s)"
        + (" and the PR body" if has_body else "")
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
