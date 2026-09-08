"""Review context: shared constants and utilities for multi-LLM review scripts."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from collections.abc import Callable, Iterable
from typing import Any

REVIEWER_NAMES: tuple[str, ...] = ("claude", "codex", "gemini")
GH_TIMEOUT_SEC = 60
_DEFAULT_PAGE_SIZE = 50

# Marker embedded in every aggregate verdict post (PR review or comment).
# Shared so post_inline_comments can count completed review rounds by
# looking for the exact same string the aggregate script emits.
REVIEW_MARKER = "<!-- multi-llm-review -->"

_APP_LOGIN_PREFIX = "app/"
_BOT_LOGIN_SUFFIX = "[bot]"


def normalize_bot_login(login: str) -> str:
    """Reduce every GitHub spelling of an app's login to the bare app slug.

    The same GitHub App is rendered three ways depending on which surface
    reports it: REST ``.user.login`` says ``github-actions[bot]``, GraphQL
    ``author.login`` says ``github-actions`` (no suffix), and ``gh pr view
    --json author`` says ``app/github-actions``. ``BOT_LOGIN`` defaults to
    the REST spelling while the stale-item pass reads authors over GraphQL,
    so a verbatim compare of the two never matched and no prior-round
    aggregate item was ever minimized under the default (AT-2208).

    Both sides of a comparison go through this function rather than the
    default changing to the GraphQL spelling: a consumer that already sets
    ``BOT_LOGIN`` in the REST form keeps working, and a comparison written
    against any of the three surfaces stays correct if the surface changes.

    Case is left alone. GitHub logins are case-insensitive, but every
    surface above reports the canonical casing, and a lowercase step here
    would mean explaining why one comparison folds case when nothing else
    in these scripts does.

    A human login has neither affix and passes through unchanged.
    """
    if login.startswith(_APP_LOGIN_PREFIX):
        login = login[len(_APP_LOGIN_PREFIX) :]
    if login.endswith(_BOT_LOGIN_SUFFIX):
        login = login[: -len(_BOT_LOGIN_SUFFIX)]
    return login


def int_env(name: str, default: int) -> int:
    """Read an integer env var, falling back to default on missing/invalid."""
    raw = os.environ.get(name, str(default))
    try:
        return int(raw)
    except ValueError:
        print(f"Invalid {name}={raw!r}, using default {default}", file=sys.stderr)
        return default


SEVERITY_ICONS: dict[str, str] = {
    "critical": "!",
    "major": "+",
    "minor": "-",
    "suggestion": "?",
}

# Stand-in for a backtick in a displayed path: U+02CB MODIFIER LETTER GRAVE
# ACCENT looks like one but is not one to markdown, so the code span around
# the path cannot be closed from inside it. Written as an escape because the
# .github tree is ASCII-only.
_BACKTICK_LOOKALIKE = "\u02cb"
_NAMED_ESCAPES = {"\n": "\\n", "\r": "\\r", "\t": "\\t"}
# Python's str.splitlines() -- which the aggregate uses to read the path list
# back -- also breaks on U+2028 and U+2029, so they are line breaks here too.
_LINE_SEPARATORS = "\u2028\u2029"


def display_path(path: str) -> str:
    """A single-line, code-span-safe rendering of a repository path.

    Paths reach the reviewers' context.md, $GITHUB_OUTPUT (one per line), a
    JSON result and PR comments as `path` inside a markdown code span; a git
    C-quoted name can decode to anything, including a newline or a backtick,
    and would otherwise break out of all four. Every C0 and C1 control
    character, DEL and the Unicode line separators become a visible escape
    (`\\n`, `\\r`, `\\t`, `\\xNN`, `\\u2028`), and a backtick becomes a
    lookalike. The result is a label, deliberately lossy: matching and every
    other decision use the raw path, never this.
    """
    out: list[str] = []
    for ch in path:
        code = ord(ch)
        if ch in _NAMED_ESCAPES:
            out.append(_NAMED_ESCAPES[ch])
        elif code < 0x20 or 0x7F <= code <= 0x9F:
            out.append(f"\\x{code:02x}")
        elif ch in _LINE_SEPARATORS:
            out.append(f"\\u{code:04x}")
        elif ch == "`":
            out.append(_BACKTICK_LOOKALIKE)
        else:
            out.append(ch)
    return "".join(out)


_MAX_LABELS = 20
_MAX_LABEL_NAME_LEN = 50


def format_labels(names: Iterable[str]) -> str:
    """Render a PR's label names for the `## PR Metadata` block (AT-2222).

    Two caps, both defense-in-depth against a maliciously large label set or
    an oversized label name reaching the reviewers' prompt context: at most
    20 labels survive, and any individual name longer than 50 characters is
    truncated to 49 characters plus an ellipsis. GitHub's UI already enforces
    both limits in practice -- a PR cannot carry more than 20 labels through
    the picker, and a label name cannot exceed 50 characters -- so this only
    guards against that enforcement being bypassed or changing.

    Names are sorted first (plain lexicographic order), then capped, so the
    kept set is deterministic rather than dependent on API ordering; the
    dropped overflow is silent, with no "+N more" marker. Each surviving name
    is rendered through `display_path` -- despite the name it is a general
    single-line, code-span-safe string renderer, and reusing it keeps one
    escaping implementation for every string this workflow prints into a
    markdown code span (repo convention: one rule, one home).
    """
    rendered: list[str] = []
    for name in sorted(names)[:_MAX_LABELS]:
        if len(name) > _MAX_LABEL_NAME_LEN:
            name = name[: _MAX_LABEL_NAME_LEN - 1] + "\u2026"
        rendered.append(display_path(name))
    return ", ".join(rendered)


DIFF_FILE_PREFIX = "+++ b/"
DIFF_FILE_PREFIX_LEN = len(DIFF_FILE_PREFIX)
DIFF_SIDE_RIGHT = "RIGHT"


_MAX_FETCH_PAGES = 20


def fetch_paginated_nodes(
    query: str,
    field: str,
    owner: str,
    name: str,
    pr_number: str,
    page_size: int = _DEFAULT_PAGE_SIZE,
    transform: Callable[[list[dict[str, Any]]], list[dict[str, Any]]] | None = None,
) -> list[dict[str, Any]]:
    """Fetch all nodes from a paginated GraphQL query.

    Args:
        query: GraphQL query with $owner, $name, $pr, $first, $after variables.
        field: The pullRequest sub-field to extract (e.g., "reviewThreads").
        owner: Repository owner.
        name: Repository name.
        pr_number: PR number (must be numeric string).
        page_size: Number of items per page.
        transform: Optional function to transform raw nodes before appending.
    """
    if not pr_number.isdigit():
        print(f"Invalid pr_number for GraphQL: {pr_number}", file=sys.stderr)
        return []
    nodes: list[dict[str, Any]] = []
    cursor = ""
    for page_num in range(_MAX_FETCH_PAGES):
        cmd = [
            "gh", "api", "graphql",
            "-f", f"query={query}",
            "-f", f"owner={owner}",
            "-f", f"name={name}",
            # -F (--field) auto-converts integers to JSON number type
            "-F", f"first={page_size}",
            "-F", f"pr={pr_number}",
        ]
        if cursor:
            cmd += ["-f", f"after={cursor}"]
        try:
            result = subprocess.run(
                cmd, capture_output=True, text=True, timeout=GH_TIMEOUT_SEC
            )
        except subprocess.TimeoutExpired:
            print(f"Warning: gh CLI timed out for {field}", file=sys.stderr)
            break
        if result.returncode != 0:
            break
        try:
            data = json.loads(result.stdout)
            section = (
                (data.get("data") or {})
                .get("repository", {})
                .get("pullRequest", {})
                .get(field, {})
            )
            raw = section.get("nodes", [])
            nodes.extend(transform(raw) if transform else raw)
            page = section.get("pageInfo", {})
            if page.get("hasNextPage") and page.get("endCursor"):
                cursor = page["endCursor"]
            else:
                break
        except (json.JSONDecodeError, KeyError):
            break
    else:
        print(
            f"Warning: reached max pages ({_MAX_FETCH_PAGES}) for {field}, "
            "results may be truncated",
            file=sys.stderr,
        )
    return nodes


def get_pr_head_sha(pr_number: str) -> str:
    """Get the HEAD commit SHA of the PR."""
    if not pr_number.isdigit():
        raise ValueError(f"Invalid pr_number: {pr_number!r}")
    result = subprocess.run(
        ["gh", "pr", "view", pr_number, "--json", "headRefOid", "-q", ".headRefOid"],
        capture_output=True,
        timeout=GH_TIMEOUT_SEC,
        text=True,
        check=True,
    )
    return result.stdout.strip()
