"""Production posting identity and immutable inline evidence (AT-2553)."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest
import yaml

import aggregate_reviews as aggregate
import post_inline_comments as posting
import review_pr_local as driver
import fetch_review_context as context_reader
import github_pr_support as support
from test_round_cutoff_fold_count import (
    _BELOW_CUTOFF,
    _BOT,
    _HEAD,
    _OLD_HEAD,
    _PAST_CUTOFF,
    _PR,
    _RUN_ID,
    _REAL_RUN,
    _TWO_MINORS,
    _issue,
    _run_aggregate,
    _run_post,
    _write_review,
)
from github_pr_support import (
    inline_post_marker,
    inline_run_identity,
    fold_markers_for,
    fold_count_line,
)


def test_production_workflows_use_the_token_owner_not_verdict_override() -> None:
    workflows = Path(__file__).resolve().parents[2] / "workflows"
    expected = "${{ steps.target_token.outputs.app-slug && format('{0}[bot]', steps.target_token.outputs.app-slug) || 'github-actions[bot]' }}"
    single = yaml.safe_load((workflows / "base-ai-review-single.yml").read_text())
    assert isinstance(single, dict)
    steps = single["jobs"]["review"]["steps"]
    step = next(s for s in steps if s.get("name") == "Post inline comments")
    assert step["env"]["BOT_LOGIN"] == expected
    aggregate_workflow = yaml.safe_load(
        (workflows / "base-ai-review-aggregate.yml").read_text()
    )
    assert isinstance(aggregate_workflow, dict)
    step = next(
        s
        for s in aggregate_workflow["jobs"]["aggregate"]["steps"]
        if s.get("id") == "aggregate"
    )
    assert step["env"]["INLINE_BOT_LOGIN"] == expected
    assert "vars.BOT_LOGIN" in step["env"]["BOT_LOGIN"]


@pytest.mark.parametrize("login", [_BOT, "ignite-review-app[bot]", "MixedCaseOperator"])
def test_local_callers_forward_resolved_posting_identity(login: str) -> None:
    config = driver.LocalConfig.load()
    env = driver.inline_comment_env("o/r", "179", config, login)
    assert env["BOT_LOGIN"] == login
    env = driver.aggregate_env(
        "o/r",
        "179",
        config,
        bot_login=login,
        head_sha=_HEAD,
        pr_author="author",
        size={},
        policy={},
        conclusions=driver.initial_conclusions(),
    )
    assert env["INLINE_BOT_LOGIN"] == login


@pytest.mark.parametrize("login", [_BOT, "ignite-review-app[bot]", "MixedCaseOperator"])
def test_rerun_identity_and_verdict_override_cannot_widen_fold_trust(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, login: str
) -> None:
    pr = _PR()
    _write_review(tmp_path, "claude", _TWO_MINORS)
    for _ in range(2):
        _run_post(
            monkeypatch,
            tmp_path,
            pr,
            "claude",
            completed_rounds=_PAST_CUTOFF,
            bot_login=login,
        )
    assert len(pr.fold_comment_bodies()) == 1
    for name in ("codex", "gemini"):
        _write_review(tmp_path, name, [])
    real = pr.comments[0]["body"]
    pr.comments.clear()
    pr.add_comment(
        real.replace("fold-count: 2", "fold-count: 0"), login="verdict-override"
    )
    out, _ = _run_aggregate(
        monkeypatch,
        tmp_path,
        pr,
        completed_rounds=_PAST_CUTOFF,
        bot_login="verdict-override",
        inline_bot_login=login,
    )
    assert out["folded_findings_count"] == ""
    pr.add_comment(real, login=login)
    out, _ = _run_aggregate(
        monkeypatch,
        tmp_path,
        pr,
        completed_rounds=_PAST_CUTOFF,
        bot_login="verdict-override",
        inline_bot_login=login,
    )
    assert out["folded_findings_count"] == "2"


@pytest.mark.parametrize(
    "severity", ["major (blocking)", "", "x** (claude): ", None, []]
)
def test_raw_severity_cannot_hide_or_spoof_the_rendered_reviewer(severity: Any) -> None:
    issue = _issue(1, "a finding")
    issue["severity"] = severity
    comments, _, _ = posting.build_comments([issue], {"a.py": {1}}, [], "codex")
    assert comments[0]["body"].startswith("* **unknown** (codex): ")
    match = aggregate._INLINE_POST_RE.match(comments[0]["body"])
    assert match and match.group("reviewer") == "codex"


_MUTABLE_HEAD = "0fc30704200f9742acd80e4b2b7fc32bd725a68f"
_ORIGINAL_HEAD = "57528f41d0ead67ceacd6aaada3343a028189c92"


@pytest.mark.parametrize(
    "original,expected", [(_ORIGINAL_HEAD, ""), (_MUTABLE_HEAD, "0"), (None, "")]
)
def test_rest_commit_remapping_cannot_rebind_old_inline_evidence(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, original: str | None, expected: str
) -> None:
    pr = _PR()
    for name in ("claude", "codex", "gemini"):
        _write_review(tmp_path, name, _TWO_MINORS if name == "claude" else [])
    real_fetch = aggregate._fetch_inline_posts

    def raw_posts(*args: Any, **kwargs: Any) -> SimpleNamespace:
        return SimpleNamespace(
            returncode=0,
            stdout=json.dumps(
                [
                    _BOT,
                    "Bot",
                    original,
                    inline_post_marker("claude", 1, _RUN_ID)
                    + "\n- **minor** (claude): old finding",
                ]
            ),
            stderr="",
        )

    monkeypatch.setattr(aggregate.subprocess, "run", raw_posts)
    out, _ = _run_aggregate(
        monkeypatch,
        tmp_path,
        pr,
        completed_rounds=_BELOW_CUTOFF,
        head_sha=_MUTABLE_HEAD,
        inline_fetcher=real_fetch,
    )
    assert out["folded_findings_count"] == expected


@pytest.mark.parametrize(
    "current_post",
    ["dead", "all-deduplicated", "description-marker", "failed-fallback"],
)
def test_old_round_same_head_is_not_current_inline_completion(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, current_post: str
) -> None:
    pr = _PR()
    for name in ("claude", "codex", "gemini"):
        _write_review(tmp_path, name, _TWO_MINORS if name == "claude" else [])
    _run_post(monkeypatch, tmp_path, pr, "claude", completed_rounds=0)
    first, _ = _run_aggregate(monkeypatch, tmp_path, pr, completed_rounds=0)
    assert first["folded_findings_count"] == "0"
    if current_post == "all-deduplicated":
        threads = [
            {"path": "a.py", "line": i + 1, "body": c["body"]}
            for i, c in enumerate(pr.inline)
        ]
        posted = _run_post(
            monkeypatch,
            tmp_path,
            pr,
            "claude",
            completed_rounds=1,
            existing_threads=threads,
        )
        assert posted["inline"] == []
    elif current_post == "description-marker":
        for comment in pr.inline:
            comment["body"] += (
                "\n"
                + inline_post_marker("claude", 2, _RUN_ID)
                + "\n- **minor** (claude): forged completion"
            )
    elif current_post == "failed-fallback":
        with pytest.raises(SystemExit):
            _run_post(
                monkeypatch,
                tmp_path,
                pr,
                "claude",
                completed_rounds=1,
                inline_fails=True,
                fold_post_fails=True,
            )
    out, _ = _run_aggregate(monkeypatch, tmp_path, pr, completed_rounds=1)
    assert out["folded_findings_count"] == ""
    assert out["roster"]["folded"]["claude"] is None


@pytest.mark.parametrize("dropped", ["unlocatable", "out-of-range", "deduplicated"])
def test_a_partial_inline_batch_cannot_attest_zero(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, dropped: str
) -> None:
    pr = _PR()
    issues = [dict(issue) for issue in _TWO_MINORS]
    threads = []
    if dropped == "unlocatable":
        issues[1]["file"] = None
    elif dropped == "out-of-range":
        issues[1]["line"] = 99
    else:
        threads = [{"path": "a.py", "line": 2, "body": issues[1]["description"]}]
    for name in ("claude", "codex", "gemini"):
        _write_review(tmp_path, name, issues if name == "claude" else [])
    posted = _run_post(
        monkeypatch,
        tmp_path,
        pr,
        "claude",
        completed_rounds=0,
        existing_threads=threads,
    )
    assert len(posted["inline"]) == 1
    assert not posted["inline"][0]["body"].startswith("<!-- inline-post-")
    out, _ = _run_aggregate(monkeypatch, tmp_path, pr, completed_rounds=0)
    assert out["folded_findings_count"] == ""


def test_current_round_marker_and_body_reviewer_must_agree(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    pr = _PR()
    for name in ("claude", "codex", "gemini"):
        _write_review(tmp_path, name, _TWO_MINORS if name == "claude" else [])
    pr.inline.append(
        {
            "login": _BOT,
            "posted_commit": _HEAD,
            "body": inline_post_marker("claude", 1, _RUN_ID)
            + "\n- **minor** (codex): other reviewer",
        }
    )
    out, _ = _run_aggregate(monkeypatch, tmp_path, pr, completed_rounds=0)
    assert out["folded_findings_count"] == ""


def test_round_marker_does_not_change_thread_dedup() -> None:
    body = "- **minor** (claude): the loader never closes its handle"
    assert posting._normalize_body(
        inline_post_marker("claude", 2, _RUN_ID) + "\n" + body
    ) == posting._normalize_body(body)


def test_fold_dedup_timeout_still_posts_real_findings(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    pr = _PR()
    for name in ("claude", "codex", "gemini"):
        _write_review(tmp_path, name, _TWO_MINORS if name == "claude" else [])
    _run_post(
        monkeypatch,
        tmp_path,
        pr,
        "claude",
        completed_rounds=_PAST_CUTOFF,
        dedup_timeout=True,
    )
    assert len(pr.fold_comment_bodies()) == 1
    out, _ = _run_aggregate(monkeypatch, tmp_path, pr, completed_rounds=_PAST_CUTOFF)
    assert out["folded_findings_count"] == "2"


def test_fold_dedup_timeout_retry_does_not_double_count_findings(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    pr = _PR()
    for name in ("claude", "codex", "gemini"):
        _write_review(tmp_path, name, _TWO_MINORS if name == "claude" else [])
    for _ in range(2):
        _run_post(
            monkeypatch, tmp_path, pr, "claude",
            completed_rounds=_PAST_CUTOFF, dedup_timeout=True,
        )
    assert len(pr.fold_comment_bodies()) == 2
    out, _ = _run_aggregate(monkeypatch, tmp_path, pr, completed_rounds=_PAST_CUTOFF)
    assert out["folded_findings_count"] == "2"


@pytest.mark.parametrize(
    "previous,current",
    [
        ("actions-123-1", "actions-124-1"),
        ("actions-123-1", "actions-123-2"),
        (_RUN_ID, "local-" + "b" * 32),
    ],
)
def test_failed_prior_aggregate_cannot_reuse_ordinal_round_evidence(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, previous: str, current: str
) -> None:
    pr = _PR()
    for name in ("claude", "codex", "gemini"):
        _write_review(tmp_path, name, _TWO_MINORS if name == "claude" else [])
    _run_post(
        monkeypatch, tmp_path, pr, "claude", completed_rounds=0, run_identity=previous
    )
    # The prior run never posted a verdict, so both invocations use r1.
    out, _ = _run_aggregate(
        monkeypatch, tmp_path, pr, completed_rounds=0, run_identity=current
    )
    assert out["folded_findings_count"] == ""
    # A real complete inline batch under the current invocation restores zero.
    _run_post(
        monkeypatch, tmp_path, pr, "claude", completed_rounds=0, run_identity=current
    )
    out, _ = _run_aggregate(
        monkeypatch, tmp_path, pr, completed_rounds=0, run_identity=current
    )
    assert out["folded_findings_count"] == "0"


@pytest.mark.parametrize("identity", ["", "wildcard", "local-*", "actions-0-1"])
def test_missing_or_invalid_invocation_does_not_create_inline_evidence(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, identity: str
) -> None:
    pr = _PR()
    for name in ("claude", "codex", "gemini"):
        _write_review(tmp_path, name, _TWO_MINORS if name == "claude" else [])
    if identity.startswith("actions-"):
        monkeypatch.delenv("INLINE_REVIEW_RUN_ID", raising=False)
        monkeypatch.setenv("GITHUB_ACTIONS", "true")
        monkeypatch.setenv("GITHUB_RUN_ID", "0")
        monkeypatch.setenv("GITHUB_RUN_ATTEMPT", "1")
    else:
        monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
        monkeypatch.setenv("INLINE_REVIEW_RUN_ID", identity)
    assert inline_run_identity() == ""
    if not identity.startswith("actions-"):
        _run_post(
            monkeypatch,
            tmp_path,
            pr,
            "claude",
            completed_rounds=0,
            run_identity=identity,
        )
        out, _ = _run_aggregate(
            monkeypatch, tmp_path, pr, completed_rounds=0, run_identity=identity
        )
        assert out["folded_findings_count"] == ""


def test_local_reused_workdir_gets_fresh_nonce_and_aggregate_only_clears_old_nonce(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(
        driver,
        "pre_review",
        lambda *a: (
            "operator",
            SimpleNamespace(head_sha=_HEAD, pr_author="author"),
            False,
            {},
        ),
    )
    monkeypatch.setattr(driver, "ensure_clone", lambda *a: None)
    monkeypatch.setattr(driver, "prepare", lambda *a: None)
    monkeypatch.setattr(driver, "policy_gate", lambda *a: {"POLICY_SKIPPED": "false"})
    posted_identities = []
    monkeypatch.setattr(
        driver, "run_review_stage", lambda *a: posted_identities.append(a[-1])
    )
    config = driver.LocalConfig.load()
    args = driver.parse_args(["o/r", "179"])
    first = driver.review_pr(
        tmp_path, tmp_path / "clone", tmp_path / "work", config, args
    )
    second = driver.review_pr(
        tmp_path, tmp_path / "clone", tmp_path / "work", config, args
    )
    assert first.inline_run_id != second.inline_run_id
    assert posted_identities == [first.inline_run_id, second.inline_run_id]
    for outcome in (first, second):
        env = driver.aggregate_env(
            "o/r",
            "179",
            config,
            bot_login=outcome.bot_login,
            head_sha=_HEAD,
            pr_author="author",
            size={},
            policy={},
            conclusions=driver.initial_conclusions(),
            inline_run_id=outcome.inline_run_id,
        )
        assert env["INLINE_REVIEW_RUN_ID"] == outcome.inline_run_id
        # The local driver may itself run under Actions; its invocation
        # nonce still isolates repeated calls within the same job attempt.
        with monkeypatch.context() as context:
            context.setenv("GITHUB_ACTIONS", "true")
            context.setenv("GITHUB_RUN_ID", "123")
            context.setenv("GITHUB_RUN_ATTEMPT", "1")
            context.setenv("INLINE_REVIEW_RUN_ID", env["INLINE_REVIEW_RUN_ID"])
            assert inline_run_identity() == outcome.inline_run_id
    monkeypatch.setenv("INLINE_REVIEW_RUN_ID", first.inline_run_id)
    env = driver.aggregate_env(
        "o/r",
        "179",
        config,
        bot_login="operator",
        head_sha=_HEAD,
        pr_author="author",
        size={},
        policy={},
        conclusions=driver.initial_conclusions(),
    )
    assert env["INLINE_REVIEW_RUN_ID"] == ""


def test_cancelled_fold_on_old_head_does_not_suppress_current_head_same_round(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    pr = _PR()
    for n in range(3):
        pr.add_comment(f"unrelated page item {n}")
    for name in ("claude", "codex", "gemini"):
        _write_review(tmp_path, name, _TWO_MINORS if name == "claude" else [])
    _run_post(
        monkeypatch,
        tmp_path,
        pr,
        "claude",
        completed_rounds=_PAST_CUTOFF,
        post_head_sha=_OLD_HEAD,
    )
    # A push cancelled A before its aggregate posted; B retains the ordinal.
    _run_post(
        monkeypatch,
        tmp_path,
        pr,
        "claude",
        completed_rounds=_PAST_CUTOFF,
        post_head_sha=_HEAD,
    )
    assert len(pr.fold_comment_bodies()) == 2
    assert fold_count_line(2, _HEAD) in pr.fold_comment_bodies()[-1]
    # Retry B deduplicates B, not the older same-marker record on A.
    _run_post(
        monkeypatch,
        tmp_path,
        pr,
        "claude",
        completed_rounds=_PAST_CUTOFF,
        post_head_sha=_HEAD,
    )
    assert len(pr.fold_comment_bodies()) == 2
    out, _ = _run_aggregate(monkeypatch, tmp_path, pr, completed_rounds=_PAST_CUTOFF)
    assert out["folded_findings_count"] == "2"


@pytest.mark.parametrize(
    "shape", ["missing-count", "bad-count", "description", "bad-head"]
)
def test_invalid_fold_envelopes_cannot_suppress_real_post(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, shape: str
) -> None:
    pr = _PR()
    marker = fold_markers_for("claude", _PAST_CUTOFF + 1)[0]
    bodies = {
        "missing-count": marker + "\nno count",
        "bad-count": marker + "\n<!-- fold-count: false -->",
        "description": "## prior verdict\n" + marker + "\n" + fold_count_line(0, _HEAD),
        "bad-head": marker + "\n<!-- fold-count: 0 head: not-a-head -->",
    }
    pr.add_comment(bodies[shape])
    _write_review(tmp_path, "claude", _TWO_MINORS)
    _run_post(monkeypatch, tmp_path, pr, "claude", completed_rounds=_PAST_CUTOFF)
    assert len(pr.comments) == 2
    assert fold_count_line(2, _HEAD) in pr.comments[-1]["body"]


def test_a_headless_envelope_still_deduplicates_by_the_reader_contract(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    pr = _PR()
    marker = fold_markers_for("claude", _PAST_CUTOFF + 1)[0]
    pr.add_comment(marker + "\n" + fold_count_line(2))
    _write_review(tmp_path, "claude", _TWO_MINORS)
    _run_post(monkeypatch, tmp_path, pr, "claude", completed_rounds=_PAST_CUTOFF)
    assert len(pr.comments) == 1


@pytest.mark.parametrize("posting_login", [_BOT, "operator", 'quoted"operator\\name'])
def test_production_round_counter_ignores_marker_in_actual_fold_description(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, posting_login: str
) -> None:
    pr = _PR()
    monkeypatch.setenv("BOT_LOGIN", posting_login)
    monkeypatch.setenv("INLINE_REVIEW_RUN_ID", _RUN_ID)
    aggregate_body = support.REVIEW_MARKER + "\n## [bot] Multi-LLM Review Summary"
    reviews = [
        {
            "user": {
                "type": "Bot" if posting_login == _BOT else "User",
                "login": posting_login,
            },
            "body": aggregate_body,
        }
    ] * 3
    for ending in ("\n", "\r\n", "\n"):
        pr.add_comment(
            support.REVIEW_MARKER + ending + "## [bot] Multi-LLM Review Summary",
            login=posting_login,
        )
    pr.add_comment(aggregate_body, login="contributor")
    seen = []

    def real_round_count(repo: str, number: str) -> int:
        def gh_count(cmd: list[str], **kwargs: Any) -> SimpleNamespace:
            assert "--paginate" in cmd
            rows = (
                reviews
                if any("/reviews" in arg for arg in cmd)
                else [
                    {
                        "user": {
                            "type": "Bot" if c["login"] == _BOT else "User",
                            "login": c["login"],
                        },
                        "body": c["body"],
                    }
                    for c in pr.comments
                ]
            )
            chunks = [rows[i : i + 2] for i in range(0, len(rows), 2)]
            result = ""
            for page in chunks:
                query = cmd[cmd.index("--jq") + 1]
                proc = _REAL_RUN(
                    ["jq", "-r", query],
                    input=json.dumps(page),
                    capture_output=True,
                    text=True,
                )
                assert proc.returncode == 0, proc.stderr
                result += proc.stdout
            return SimpleNamespace(returncode=0, stdout=result, stderr="")

        with patch.object(subprocess, "run", gh_count):
            count = support.fetch_round_count(repo, number)
        seen.append(count)
        return count

    issues = [dict(issue) for issue in _TWO_MINORS]
    issues[1]["description"] += " " + support.REVIEW_MARKER
    for name in ("claude", "codex", "gemini"):
        _write_review(tmp_path, name, issues if name == "claude" else [])
    assert real_round_count("o/r", "179") == 6
    _run_post(
        monkeypatch,
        tmp_path,
        pr,
        "claude",
        completed_rounds=6,
        round_counter=real_round_count,
        bot_login=posting_login,
    )
    out, _ = _run_aggregate(
        monkeypatch,
        tmp_path,
        pr,
        completed_rounds=6,
        round_counter=real_round_count,
        bot_login=posting_login,
    )
    assert all(count == 6 for count in seen)
    assert out["folded_findings_count"] == "2"
    pr.add_comment(aggregate_body, login=posting_login)
    _run_post(
        monkeypatch,
        tmp_path,
        pr,
        "claude",
        completed_rounds=7,
        round_counter=real_round_count,
        bot_login=posting_login,
        run_identity="local-" + "b" * 32,
    )
    assert "round-cutoff-claude-r8" in pr.fold_comment_bodies()[-1]
    assert len(pr.fold_comment_bodies()) == 2
    pr.add_comment("> " + aggregate_body)
    pr.add_comment(support.REVIEW_MARKER + " suffix\nnot an aggregate")
    assert real_round_count("o/r", "179") == 7


@pytest.mark.parametrize("nonce", ["", "wildcard", _RUN_ID])
def test_round_counter_only_admits_exact_local_operator_with_valid_nonce(
    monkeypatch: pytest.MonkeyPatch, nonce: str
) -> None:
    monkeypatch.setenv("BOT_LOGIN", "operator")
    monkeypatch.setenv("INLINE_REVIEW_RUN_ID", nonce)
    rows = [
        {
            "user": {"type": kind, "login": login},
            "body": support.REVIEW_MARKER + "\nverdict",
        }
        for kind, login in [
            ("Bot", "app[bot]"),
            ("User", "operator"),
            ("User", "someone-else"),
        ]
    ]

    def gh_count(cmd: list[str], **kwargs: Any) -> SimpleNamespace:
        payload = rows if any("/reviews" in arg for arg in cmd) else []
        result = _REAL_RUN(
            ["jq", "-r", cmd[cmd.index("--jq") + 1]],
            input=json.dumps(payload),
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, result.stderr
        return SimpleNamespace(returncode=0, stdout=result.stdout, stderr="")

    monkeypatch.setattr(subprocess, "run", gh_count)
    assert support.fetch_round_count("o/r", "179") == (2 if nonce == _RUN_ID else 1)


@pytest.mark.parametrize("shape", ["enveloped", "legacy", "malformed"])
def test_prior_context_strips_only_valid_envelope_before_truncation(
    monkeypatch: pytest.MonkeyPatch, shape: str
) -> None:
    issue = "- **minor** (claude): " + "x" * 470 + "TAIL"
    marker = inline_post_marker("claude", 7, _RUN_ID)
    body = (
        issue
        if shape == "legacy"
        else (marker if shape == "enveloped" else marker + " suffix") + "\n" + issue
    )
    node = {
        "isResolved": False,
        "isOutdated": False,
        "comments": {
            "nodes": [
                {
                    "body": body,
                    "author": {"login": "github-actions"},
                    "authorAssociation": "NONE",
                }
            ]
        },
    }
    monkeypatch.setattr(
        context_reader, "fetch_paginated_nodes", lambda **kwargs: [node]
    )
    output = context_reader._format_prior_threads("o/r", "179")
    expected = context_reader._sanitize(issue if shape == "enveloped" else body)
    assert expected in output
    if shape != "malformed":
        assert "TAIL" in output
        assert "inline-post-" not in output


@pytest.mark.parametrize("severity", ["Major", "MINOR", "Suggestion", "CRITICAL"])
def test_known_severity_case_is_normalized_before_safe_prefix(severity: str) -> None:
    comments, _, _ = posting.build_comments(
        [_issue(1, "a finding", severity)], {"a.py": {1}}, [], "claude"
    )
    assert f"**{severity.lower()}** (claude):" in comments[0]["body"]
    assert aggregate._INLINE_POST_RE.match(comments[0]["body"])
