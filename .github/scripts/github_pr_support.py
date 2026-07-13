"""Review context: shared constants and utilities for multi-LLM review scripts."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from collections.abc import Callable
from typing import Any

REVIEWER_NAMES: tuple[str, ...] = ("claude", "codex", "gemini")
GH_TIMEOUT_SEC = 60
_DEFAULT_PAGE_SIZE = 50

# Marker embedded in every aggregate verdict post (PR review or comment).
# Shared so post_inline_comments can count completed review rounds by
# looking for the exact same string the aggregate script emits.
REVIEW_MARKER = "<!-- multi-llm-review -->"


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
