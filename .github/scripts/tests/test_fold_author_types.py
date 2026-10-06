"""Posting identity must distinguish human accounts from GitHub Apps."""

import json
import os
import subprocess
from types import SimpleNamespace

import pytest

import aggregate_reviews as aggregate
import post_inline_comments as posting
from github_pr_support import fold_count_line, inline_post_marker, inline_run_identity

_REAL_RUN = subprocess.run
_HEAD = "a" * 40
_NONCE = "local-" + "b" * 32


@pytest.mark.parametrize("surface", ["fold", "inline", "writer"])
@pytest.mark.parametrize(
    "kind,login,expected_login,nonce,accepted",
    [
        ("Bot", "review-app", "review-app[bot]", "", True),
        ("User", "review-app", "review-app[bot]", "", False),
        ("User", "review-app", "review-app[bot]", _NONCE, False),
        ("User", "Operator", "Operator", _NONCE, True),
        ("User", "operator", "Operator", _NONCE, False),
        ("User", "other", "Operator", _NONCE, False),
        ("User", "Operator", "Operator", "invalid", False),
        ("User", "Operator", "Operator", "", False),
        (None, "review-app", "review-app[bot]", "", False),
    ],
)
def test_actual_transports_bind_type_and_exact_local_operator(
    monkeypatch, surface, kind, login, expected_login, nonce, accepted
):
    monkeypatch.setenv("INLINE_REVIEW_RUN_ID", nonce)
    monkeypatch.setenv("BOT_LOGIN", expected_login)
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.setenv("GITHUB_RUN_ID", "123")
    monkeypatch.setenv("GITHUB_RUN_ATTEMPT", "1")
    monkeypatch.setattr(aggregate, "INLINE_BOT_LOGIN", expected_login)
    monkeypatch.setattr(aggregate, "fetch_round_count", lambda *args: 0)
    inline = (
        inline_post_marker("claude", 1, inline_run_identity())
        + "\n- **minor** (claude): finding"
    )
    fold = "<!-- round-cutoff-claude-r1 -->\n" + fold_count_line(2, _HEAD)

    posted = []

    def transport(cmd, **kwargs):
        if cmd[:3] == ["gh", "pr", "comment"]:
            posted.append(kwargs["input"])
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if cmd[:3] == ["gh", "api", "graphql"]:
            query = next(arg for arg in cmd if arg.startswith("query="))
            author = {"login": login}
            if "__typename" in query:
                author["__typename"] = kind
            nodes = [{"author": author, "body": fold}] if surface == "fold" else []
            payload = {
                "data": {
                    "repository": {
                        "pullRequest": {
                            "comments": {
                                "nodes": nodes,
                                "pageInfo": {"hasNextPage": False, "endCursor": None},
                            }
                        }
                    }
                }
            }
            return SimpleNamespace(returncode=0, stdout=json.dumps(payload), stderr="")
        rows = (
            [
                {
                    "user": {"login": login, "type": kind},
                    "commit_id": _HEAD,
                    "original_commit_id": _HEAD,
                    "body": fold if surface == "writer" else inline,
                }
            ]
            if surface in {"inline", "writer"}
            else []
        )
        return _REAL_RUN(
            ["jq", "-r", cmd[cmd.index("--jq") + 1]],
            input=json.dumps(rows),
            text=True,
            capture_output=True,
            env=os.environ,
        )

    monkeypatch.setattr(subprocess, "run", transport)
    if surface == "writer":
        posting.post_cutoff_summary(
            "o/r",
            "7",
            "claude",
            1,
            [{"path": "a.py", "line": 1, "body": "finding"}],
            _HEAD,
        )
        assert len(posted) == (0 if accepted else 1)
        return
    result = aggregate._folded_findings({"claude": {"issues": [{}]}}, "o/r", "7", _HEAD)
    assert result["claude"] == ((2 if surface == "fold" else 0) if accepted else None)
