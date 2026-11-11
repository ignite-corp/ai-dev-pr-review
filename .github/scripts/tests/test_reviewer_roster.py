"""Trusted roster configuration and selected-identity aggregate behavior."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import subprocess
import sys
from unittest.mock import Mock

import pytest

from github_pr_support import (
    REVIEWER_NAMES,
    inline_post_envelope,
    inline_post_marker,
    trusted_post_author,
)
from post_inline_comments import normalize_body
from reviewer_roster import DEFAULT_REVIEWERS, SUPPORTED_REVIEWERS, parse_reviewers

SCRIPT_DIR = Path(__file__).parents[1]
ALL = ["claude", "codex", "gemini", "copilot"]
OK = {"summary": "Reviewed", "status": "ok", "early_exit": False, "issues": []}


def test_default_alias_and_supported_registry_are_separate():
    assert parse_reviewers(None) == DEFAULT_REVIEWERS == REVIEWER_NAMES
    assert DEFAULT_REVIEWERS == ("claude", "codex", "gemini")
    assert SUPPORTED_REVIEWERS == tuple(ALL)


def test_configured_priority_is_preserved_exactly():
    assert parse_reviewers('["copilot","claude","gemini"]') == (
        "copilot",
        "claude",
        "gemini",
    )


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "not-json",
        "[]",
        "null",
        "true",
        "1",
        '"claude"',
        "{}",
        '["claude","claude"]',
        '["CLAUDE"]',
        '["claude",null]',
        '["claude",1]',
        '["claude",{}]',
        '["claude",["codex"]]',
        '["claude","copilot","outsider"]',
    ],
)
def test_invalid_explicit_selection_never_falls_back(raw):
    with pytest.raises(ValueError, match="REVIEWERS"):
        parse_reviewers(raw)


@pytest.fixture
def selected_round(monkeypatch, tmp_path):
    """Execute a fresh aggregate import so its real configuration boundary runs."""
    monkeypatch.chdir(tmp_path)
    for name, value in {
        "GITHUB_OUTPUT": str(tmp_path / "outputs"),
        "GITHUB_STEP_SUMMARY": "",
        "GITHUB_REPOSITORY": "owner/repo",
        "PR_NUMBER": "42",
        "HEAD_SHA": "a" * 40,
        "PREPARE_RESULT": "success",
        "SIZE_SKIPPED": "false",
        "POLICY_SKIPPED": "false",
        "REVIEW_MODE": "parallel",
        "ALLOW_AUTO_APPROVE": "true",
        "BLOCK_MERGE_ON_VERDICT": "false",
    }.items():
        monkeypatch.setenv(name, value)

    def configure(names, responded):
        monkeypatch.setenv("REVIEWERS", json.dumps(names))
        spec = importlib.util.spec_from_file_location(
            "_selected_aggregate_test", SCRIPT_DIR / "aggregate_reviews.py"
        )
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        monkeypatch.setitem(sys.modules, spec.name, module)
        spec.loader.exec_module(module)
        monkeypatch.setattr(module, "_head_is_stale", lambda: False)
        monkeypatch.setattr(module, "fetch_round_count", lambda *args: 0)
        for method in (
            "_fetch_fold_comments",
            "_fetch_inline_posts",
            "_fetch_disposition_threads",
            "_fetch_quoted_dispositions",
        ):
            monkeypatch.setattr(module, method, lambda *args: [])
        post = Mock(return_value=True)
        monkeypatch.setattr(module, "post_verdict", post)
        for name in names:
            monkeypatch.setenv(f"REVIEWER_RESULT_{name.upper()}", "success")
            if name in responded:
                Path(f"review-{name}.json").write_text(json.dumps(OK))
        return module, post

    return configure


def run_aggregate(module):
    try:
        module.main()
    except SystemExit as exc:
        return exc.code
    return 0


def outputs():
    return dict(line.split("=", 1) for line in Path("outputs").read_text().splitlines())


@pytest.mark.parametrize("responded", range(5))
def test_four_selected_use_two_for_review_and_three_for_approval(
    selected_round, responded
):
    module, post = selected_round(ALL, ALL[:responded])
    assert list(module.REVIEWERS) == ALL
    assert module._minimum_reviewers_for_verdict() == 2
    assert run_aggregate(module) == int(responded < 2)
    assert post.call_args.kwargs["approve_quorum"] is (responded >= 3)
    assert f"{responded}/4 reviewers" in post.call_args.args[0]
    emitted = outputs()
    roster = json.loads(emitted["reviewer_roster"])
    assert roster["expected"] == ALL
    assert roster["responded"] == ALL[:responded]
    assert emitted["reviewers_expected_count"] == "4"


@pytest.mark.parametrize("failed", [False, True])
def test_missing_or_failed_copilot_stays_in_denominator(
    selected_round, monkeypatch, failed
):
    module, post = selected_round(ALL, ALL[:3])
    monkeypatch.setenv("REVIEWER_RESULT_COPILOT", "failure")
    if failed:
        Path("review-copilot.json").write_text(
            json.dumps(OK | {"status": "failed", "error": "review_failed"})
        )
    assert run_aggregate(module) == 0
    assert "3/4 reviewers" in post.call_args.args[0]
    roster = json.loads(outputs()["reviewer_roster"])
    assert roster["responded"] == ALL[:3]
    assert set(roster["missing"]) == {"copilot"}
    assert "failed" in roster["missing"]["copilot"]
    assert module.load_reviewer_conclusions()["copilot"] == "failure"


def test_unselected_and_unknown_artifacts_and_identity_claims_do_not_count(
    selected_round,
):
    module, post = selected_round(ALL[:3], ALL[:2])
    for name in ("copilot", "outsider"):
        Path(f"review-{name}.json").write_text(json.dumps(OK | {"reviewer": "gemini"}))
    assert run_aggregate(module) == 0
    assert post.call_args.kwargs["approve_quorum"] is False
    assert "2/3 reviewers" in post.call_args.args[0]
    assert set(module.load_reviews()) == set(ALL[:3])


def test_artifact_and_roster_order_follows_configured_priority(selected_round):
    names = ["copilot", "claude", "gemini"]
    module, _ = selected_round(names, ["copilot", "gemini"])
    assert list(module.load_reviews()) == names
    assert run_aggregate(module) == 0
    roster = json.loads(outputs()["reviewer_roster"])
    assert roster["expected"] == names
    assert roster["responded"] == ["copilot", "gemini"]


@pytest.mark.parametrize(
    "raw", ['["SECRET_unknown"]', '"SECRET_invalid"', "SECRET_bad_json"]
)
def test_invalid_config_exits_before_external_calls_or_outputs(tmp_path, raw):
    gh = tmp_path / "gh"
    gh.write_text("#!/bin/sh\nprintf called > external-call\n")
    gh.chmod(0o700)
    result = subprocess.run(
        [sys.executable, str(SCRIPT_DIR / "aggregate_reviews.py")],
        cwd=tmp_path,
        env={
            "PATH": str(tmp_path),
            "REVIEWERS": raw,
            "GITHUB_OUTPUT": str(tmp_path / "outputs"),
            "GITHUB_REPOSITORY": "owner/repo",
            "PR_NUMBER": "42",
        },
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert result.returncode != 0
    assert "REVIEWERS" in result.stderr
    assert "SECRET" not in result.stderr + result.stdout
    assert not (tmp_path / "external-call").exists()
    assert not (tmp_path / "outputs").exists()


def test_copilot_marker_is_recognized_without_widening_author_trust(monkeypatch):
    run = "actions-123-1"
    marker = inline_post_marker("copilot", 1, run)
    assert inline_post_envelope(marker + "\nbody") == ("copilot", 1, run, "body")
    with pytest.raises(ValueError):
        inline_post_marker("outsider", 1, run)
    assert normalize_body("+ **major** (copilot): defect") == normalize_body(
        "+ **major** (claude): defect"
    )
    assert normalize_body("+ **major** (outsider): defect") != normalize_body(
        "+ **major** (claude): defect"
    )
    monkeypatch.setenv("GITHUB_RUN_ID", "123")
    monkeypatch.setenv("GITHUB_RUN_ATTEMPT", "1")
    assert trusted_post_author(
        {"author_type": "Bot", "login": "github-actions[bot]"}, "github-actions[bot]"
    )
    assert not trusted_post_author(
        {"author_type": "Bot", "login": "outsider[bot]"}, "github-actions[bot]"
    )
    assert not trusted_post_author(
        {"author_type": "User", "login": "github-actions"}, "github-actions[bot]"
    )


def test_historical_major_labels_include_unselected_supported_reviewers(selected_round):
    module, _ = selected_round(ALL[:3], ALL[:3])
    assert "copilot" not in module.REVIEWERS
    assert module._POSTED_MAJOR_RE.match("+ **major** (copilot): previous finding")
    assert not module._POSTED_MAJOR_RE.match("+ **major** (outsider): fake finding")
    first = {
        "author": {"login": "github-actions", "__typename": "Bot"},
        "body": inline_post_marker("copilot", 1, "actions-123-1")
        + "\n+ **major** (copilot): Close socket after timeout",
    }
    thread = {
        "isResolved": True,
        "path": "a.py",
        "line": 10,
        "head": {"nodes": [first]},
        "tail": {
            "nodes": [
                first,
                {
                    "author": {"login": "maintainer", "__typename": "User"},
                    "authorAssociation": "MEMBER",
                    "body": "By design: bounded elsewhere",
                },
            ]
        },
    }
    parsed = module._parse_disposition_threads([thread])
    assert len(parsed) == 1
    available = {
        "claude": OK
        | {
            "issues": [
                {
                    "severity": "major",
                    "file": "a.py",
                    "line": 10,
                    "description": "Close socket after timeout",
                    "suggestion": None,
                }
            ]
        }
    }
    assert module._dispositioned_majors(available, parsed) == {
        ("claude", 0): "By design by `maintainer`"
    }
