"""Tests for threads.jq (review-thread filter used by collect_review_threads.sh).

Runs jq against a fixture of raw GraphQL reviewThreads nodes and checks
that outdated threads are kept (dropping them hid prior findings from the
reviewer's dedup context, causing re-raises), that unresolved threads sort
first (so the downstream 50-item prompt cap never crowds them out), and
that the bot-author filter still applies.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

THREADS_JQ = Path(__file__).resolve().parent.parent / "threads.jq"

requires_jq = pytest.mark.skipif(shutil.which("jq") is None, reason="jq not installed")


def _node(
    author: str,
    path: str,
    line: int | None,
    body: str,
    *,
    resolved: bool,
    outdated: bool,
) -> dict[str, Any]:
    """Build a raw GraphQL reviewThread node as collect_review_threads.sh sees it."""
    return {
        "isResolved": resolved,
        "isOutdated": outdated,
        "path": path,
        "line": line,
        "comments": {"nodes": [{"body": body, "author": {"login": author}}]},
    }


def _run_threads_jq(nodes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result = subprocess.run(
        ["jq", "-f", str(THREADS_JQ)],
        input=json.dumps(nodes),
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    )
    return json.loads(result.stdout)


@requires_jq
class TestThreadsJq:
    def test_outdated_threads_are_included(self) -> None:
        # After a push most prior threads become outdated; they must stay
        # in the dedup list so the reviewer can see what it already raised.
        nodes = [
            _node("claude", "a.py", 10, "current finding", resolved=False, outdated=False),
            _node("claude", "a.py", 20, "outdated unresolved", resolved=False, outdated=True),
            _node("codex", "b.py", 30, "outdated resolved", resolved=True, outdated=True),
            _node("gemini-code-assist", "c.py", 40, "resolved finding", resolved=True, outdated=False),
        ]
        output = _run_threads_jq(nodes)
        assert len(output) == 4
        assert {item["body"] for item in output} == {
            "current finding",
            "outdated unresolved",
            "outdated resolved",
            "resolved finding",
        }

    def test_unresolved_threads_sort_first(self) -> None:
        # Unresolved items must precede resolved ones so the downstream
        # 50-item cap trims resolved noise, never open findings.
        nodes = [
            _node("claude", "a.py", 10, "resolved one", resolved=True, outdated=True),
            _node("claude", "a.py", 20, "open one", resolved=False, outdated=True),
            _node("codex", "b.py", 30, "resolved two", resolved=True, outdated=False),
            _node("codex", "b.py", 40, "open two", resolved=False, outdated=False),
        ]
        output = _run_threads_jq(nodes)
        statuses = [item["status"] for item in output]
        assert statuses == ["unresolved", "unresolved", "resolved", "resolved"]

    def test_non_bot_authors_are_excluded(self) -> None:
        nodes = [
            _node("claude", "a.py", 10, "bot finding", resolved=False, outdated=True),
            _node("some-human", "a.py", 20, "human comment", resolved=False, outdated=False),
        ]
        output = _run_threads_jq(nodes)
        assert [item["author"] for item in output] == ["claude"]

    def test_output_shape_and_status_mapping(self) -> None:
        nodes = [
            _node("claude", "a.py", 10, "open finding", resolved=False, outdated=True),
        ]
        output = _run_threads_jq(nodes)
        assert output == [
            {
                "author": "claude",
                "path": "a.py",
                "line": 10,
                "status": "unresolved",
                "body": "open finding",
            }
        ]

    def test_long_body_is_truncated(self) -> None:
        nodes = [
            _node("claude", "a.py", 10, "x" * 600, resolved=False, outdated=False),
        ]
        output = _run_threads_jq(nodes)
        assert output[0]["body"] == "x" * 500 + "...(truncated)"
