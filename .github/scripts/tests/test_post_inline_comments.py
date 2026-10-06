"""Tests for post_inline_comments fuzzy dedup logic.

Covers the scenarios that the previous exact-match dedup missed,
including paraphrases that share token vocabulary, line-distance gating
on the same path, strong-similarity dedup across force-push line shifts,
and backward compatibility when no line is supplied.

Also covers the round-cutoff convergence backstop (RC-5): round counting,
the cutoff decision, and the fold into a single summary comment.
"""

from __future__ import annotations

import importlib
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import post_inline_comments
from post_inline_comments import (
    _is_duplicate,
    _normalize_body,
    build_comments,
    fetch_round_count,
    post_cutoff_summary,
    round_cutoff_round,
)

# The consumer of the round-cutoff comment: cutoff_findings in the plugin's
# SKILL.md, the function the merge gate pastes into a shell. Its path has one
# anchor, in the drill's test, and so does the way it is carved out of the
# file -- gate_function runs the drill's own extract() -- so the two cannot
# disagree about where it is or about what it is.
from test_gate_headline_drill import SKILL, gate_function


def _cutoff_findings(
    run: Any, function_file: Path, body: str
) -> subprocess.CompletedProcess[str]:
    """Feed one Bot comment carrying ``body`` to the real cutoff_findings, nothing answering it."""
    comments = json.dumps(
        [{"id": 1, "type": "Bot", "created_at": "2026-10-13T00:00:00Z", "body": body}]
    )
    return run(
        [
            "bash",
            "-c",
            '. "$1"; printf "%s" "$2" | cutoff_findings ""',
            "bash",
            str(function_file),
            comments,
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )


def _thread(path: str, body: str, line: int | None = None) -> dict[str, Any]:
    """Build a thread dict matching fetch_existing_threads output."""
    return {"path": path, "line": line, "body": _normalize_body(body)}


def _issue(
    file: str, line: int, description: str, severity: str = "minor"
) -> dict[str, Any]:
    """Build an issue dict matching the review JSON schema."""
    return {
        "file": file,
        "line": line,
        "description": description,
        "severity": severity,
    }


class TestIsDuplicate:
    def test_exact_match_dedups(self) -> None:
        existing = [_thread("a.py", "Missing input validation on user_id", line=10)]
        assert _is_duplicate(
            "a.py",
            "Missing input validation on user_id",
            existing,
            line=10,
        )

    def test_reworded_dedups_at_low_threshold(self) -> None:
        # Two phrasings of the same concern share most tokens.
        existing = [
            _thread(
                "a.py",
                "Input validation missing for user_id parameter",
                line=10,
            )
        ]
        assert _is_duplicate(
            "a.py",
            "Missing input validation on user_id",
            existing,
            line=10,
            threshold=0.5,
        )

    def test_different_paths_no_dedup(self) -> None:
        existing = [_thread("a.py", "Missing input validation on user_id", line=10)]
        assert not _is_duplicate(
            "b.py",
            "Missing input validation on user_id",
            existing,
            line=10,
        )

    def test_identical_text_distant_lines_dedups(self) -> None:
        # Force-push/rebase shifts lines far beyond the window; identical
        # text (Jaccard 1.0 >= strong threshold) must dedup regardless.
        existing = [_thread("a.py", "Missing input validation on user_id", line=10)]
        assert _is_duplicate(
            "a.py",
            "Missing input validation on user_id",
            existing,
            line=16,  # delta 6, just past the +/-5 window
        )

    def test_identical_text_large_line_shift_dedups(self) -> None:
        # Real-world case: the same finding moved from line 271 to 162
        # (delta 109) after a force-push and was reposted 8 times.
        existing = [_thread("tf/sg.tf", "Egress open to 0.0.0.0/0", line=271)]
        assert _is_duplicate(
            "tf/sg.tf",
            "Egress open to 0.0.0.0/0",
            existing,
            line=162,
        )

    def test_moderate_similarity_distant_lines_no_dedup(self) -> None:
        # Jaccard 4/6 = 0.67 sits in the moderate band (0.6-0.8), which
        # still requires the line window -- distant lines must NOT dedup.
        existing = [_thread("a.py", "Missing input validation on user_id", line=10)]
        assert not _is_duplicate(
            "a.py",
            "Missing input validation user_id parameter",
            existing,
            line=100,
        )

    def test_moderate_similarity_within_window_dedups(self) -> None:
        # Same moderate-band pair (Jaccard 0.67) within +/-5 lines dedups.
        existing = [_thread("a.py", "Missing input validation on user_id", line=10)]
        assert _is_duplicate(
            "a.py",
            "Missing input validation user_id parameter",
            existing,
            line=12,
        )

    def test_identical_text_different_file_no_dedup(self) -> None:
        # The strong-similarity path skips the line window but never the
        # path check.
        existing = [_thread("a.py", "Missing input validation on user_id", line=10)]
        assert not _is_duplicate(
            "b.py",
            "Missing input validation on user_id",
            existing,
            line=200,
        )

    def test_empty_tokens_no_dedup(self) -> None:
        existing = [_thread("a.py", "Missing input validation on user_id", line=10)]
        # Body that normalises to no tokens must not dedup.
        assert not _is_duplicate("a.py", "   ", existing, line=10)

    def test_resolved_thread_still_dedups(self) -> None:
        # Threads are passed in with no isResolved field at this layer --
        # presence in `existing_threads` is enough to suppress the new post.
        # This guards against regressions where the caller starts filtering
        # by status before passing in.
        existing = [_thread("a.py", "Missing input validation on user_id", line=10)]
        assert _is_duplicate(
            "a.py",
            "Missing input validation on user_id",
            existing,
            line=10,
        )

    def test_no_line_param_backward_compat(self) -> None:
        # Old callers that don't pass `line` still get content-based dedup.
        existing = [_thread("a.py", "Missing input validation on user_id", line=10)]
        assert _is_duplicate(
            "a.py",
            "Missing input validation on user_id",
            existing,
        )

    def test_none_description_does_not_raise(self) -> None:
        # An issue payload with description=None must not crash dedup.
        existing = [_thread("a.py", "Missing input validation on user_id", line=10)]
        assert not _is_duplicate("a.py", None, existing, line=10)

    def test_none_thread_body_does_not_raise(self) -> None:
        # A thread fetched with body=None (explicit null) must not crash dedup.
        existing = [{"path": "a.py", "line": 10, "body": None}]
        assert not _is_duplicate(
            "a.py",
            "Missing input validation on user_id",
            existing,
            line=10,
        )


class TestBuildCommentsBatchDedup:
    """Batch-internal dedup (RC-4): one reviewer emitting the same finding
    twice in a single round must post once, at the highest severity."""

    VALID_LINES = {"a.py": set(range(1, 100)), "b.py": set(range(1, 100))}

    def test_same_finding_two_severities_keeps_higher_when_higher_first(self) -> None:
        issues = [
            _issue("a.py", 10, "Missing input validation on user_id", "major"),
            _issue("a.py", 12, "Missing input validation on user_id", "minor"),
        ]
        comments, no_location, out_of_range = build_comments(
            issues, self.VALID_LINES, [], "claude"
        )
        assert len(comments) == 1
        assert "**major**" in comments[0]["body"]
        assert comments[0]["line"] == 10
        assert (no_location, out_of_range) == (0, 0)

    def test_same_finding_two_severities_replaces_with_higher(self) -> None:
        issues = [
            _issue("a.py", 10, "Missing input validation on user_id", "minor"),
            _issue("a.py", 12, "Missing input validation on user_id", "critical"),
        ]
        comments, _, _ = build_comments(issues, self.VALID_LINES, [], "claude")
        assert len(comments) == 1
        assert "**critical**" in comments[0]["body"]
        assert comments[0]["line"] == 12

    def test_equal_severity_duplicate_keeps_first(self) -> None:
        issues = [
            _issue("a.py", 10, "Missing input validation on user_id", "minor"),
            _issue("a.py", 12, "Missing input validation on user_id", "minor"),
        ]
        comments, _, _ = build_comments(issues, self.VALID_LINES, [], "claude")
        assert len(comments) == 1
        assert comments[0]["line"] == 10

    def test_distinct_findings_both_posted(self) -> None:
        issues = [
            _issue("a.py", 10, "Missing input validation on user_id", "major"),
            _issue("a.py", 50, "Hardcoded credentials in config loader", "major"),
        ]
        comments, _, _ = build_comments(issues, self.VALID_LINES, [], "claude")
        assert len(comments) == 2

    def test_same_finding_different_files_both_posted(self) -> None:
        issues = [
            _issue("a.py", 10, "Missing input validation on user_id", "major"),
            _issue("b.py", 10, "Missing input validation on user_id", "major"),
        ]
        comments, _, _ = build_comments(issues, self.VALID_LINES, [], "claude")
        assert len(comments) == 2

    def test_existing_thread_dedup_still_applies(self) -> None:
        existing = [_thread("a.py", "Missing input validation on user_id", line=10)]
        issues = [
            _issue("a.py", 10, "Missing input validation on user_id", "critical"),
        ]
        comments, _, _ = build_comments(issues, self.VALID_LINES, existing, "claude")
        assert comments == []


def test_jaccard_threshold_env_override(monkeypatch: Any) -> None:
    """Setting JACCARD_THRESHOLD via env should make dedup stricter or looser."""
    # Force a fresh import with the env set
    monkeypatch.setenv("JACCARD_THRESHOLD", "0.95")
    sys.modules.pop("post_inline_comments", None)
    mod = importlib.import_module("post_inline_comments")
    # The module-level _JACCARD_THRESHOLD should reflect the env value
    assert getattr(mod, "_JACCARD_THRESHOLD", None) == 0.95


def test_strong_jaccard_env_override(monkeypatch: Any) -> None:
    """Setting DEDUP_STRONG_JACCARD via env should tune the strong threshold."""
    # Force a fresh import with the env set
    monkeypatch.setenv("DEDUP_STRONG_JACCARD", "0.9")
    sys.modules.pop("post_inline_comments", None)
    mod = importlib.import_module("post_inline_comments")
    # The module-level DEDUP_STRONG_JACCARD should reflect the env value
    assert getattr(mod, "DEDUP_STRONG_JACCARD", None) == 0.9


def _cutoff_issue(**overrides: Any) -> dict[str, Any]:
    """Create a minimal issue payload as found in review-<name>.json."""
    base: dict[str, Any] = {
        "severity": "minor",
        "file": "a.py",
        "line": 2,
        "description": "minor finding",
        "suggestion": None,
    }
    base.update(overrides)
    return base


class TestRoundCutoffRound:
    """Decision logic: enabled flag, severity gate, round threshold."""

    def _patch_round(self, monkeypatch: Any, completed: int) -> None:
        monkeypatch.setattr(
            post_inline_comments, "fetch_round_count", lambda repo, pr: completed
        )

    def test_below_cutoff_returns_none(self, monkeypatch: Any) -> None:
        # 3 completed rounds -> current round 4 < default 5: normal posting.
        self._patch_round(monkeypatch, 3)
        assert round_cutoff_round("o/r", "1", [_cutoff_issue()]) is None

    def test_minor_only_at_cutoff_returns_round(self, monkeypatch: Any) -> None:
        # 4 completed rounds -> current round 5 >= default 5: cutoff.
        self._patch_round(monkeypatch, 4)
        issues = [_cutoff_issue(), _cutoff_issue(severity="suggestion", file="b.py")]
        assert round_cutoff_round("o/r", "1", issues) == 5

    def test_major_present_returns_none(self, monkeypatch: Any) -> None:
        self._patch_round(monkeypatch, 9)
        issues = [_cutoff_issue(), _cutoff_issue(severity="major")]
        assert round_cutoff_round("o/r", "1", issues) is None

    def test_critical_present_returns_none(self, monkeypatch: Any) -> None:
        self._patch_round(monkeypatch, 9)
        assert (
            round_cutoff_round("o/r", "1", [_cutoff_issue(severity="critical")]) is None
        )

    def test_unknown_severity_fails_open(self, monkeypatch: Any) -> None:
        # Aliases like "high" are normalized only in the aggregate stage;
        # here anything outside minor/suggestion must block the cutoff.
        self._patch_round(monkeypatch, 9)
        assert round_cutoff_round("o/r", "1", [_cutoff_issue(severity="high")]) is None

    def test_env_override_lowers_cutoff(self, monkeypatch: Any) -> None:
        monkeypatch.setenv("ROUND_CUTOFF_N", "2")
        self._patch_round(monkeypatch, 1)  # current round 2
        assert round_cutoff_round("o/r", "1", [_cutoff_issue()]) == 2

    def test_env_override_raises_cutoff(self, monkeypatch: Any) -> None:
        monkeypatch.setenv("ROUND_CUTOFF_N", "10")
        self._patch_round(monkeypatch, 8)  # current round 9 < 10
        assert round_cutoff_round("o/r", "1", [_cutoff_issue()]) is None

    def test_invalid_env_uses_default(self, monkeypatch: Any) -> None:
        monkeypatch.setenv("ROUND_CUTOFF_N", "not-a-number")
        self._patch_round(monkeypatch, 4)  # current round 5 >= default 5
        assert round_cutoff_round("o/r", "1", [_cutoff_issue()]) == 5

    def test_disabled_via_env(self, monkeypatch: Any) -> None:
        monkeypatch.setenv("ROUND_CUTOFF_ENABLED", "false")
        self._patch_round(monkeypatch, 99)
        assert round_cutoff_round("o/r", "1", [_cutoff_issue()]) is None


class TestFetchRoundCount:
    def test_sums_pages_across_endpoints(self, monkeypatch: Any) -> None:
        # --paginate emits one jq count per page; both endpoints contribute.
        outputs = iter(["2\n1\n", "3\n"])
        calls: list[list[str]] = []

        def fake_run(cmd: list[str], **kwargs: Any) -> SimpleNamespace:
            calls.append(cmd)
            return SimpleNamespace(returncode=0, stdout=next(outputs), stderr="")

        monkeypatch.setattr(subprocess, "run", fake_run)
        assert fetch_round_count("o/r", "7") == 6
        assert len(calls) == 2
        assert "repos/o/r/pulls/7/reviews" in calls[0]
        assert "repos/o/r/issues/7/comments" in calls[1]

    def test_filters_on_bot_and_marker(self, monkeypatch: Any) -> None:
        captured: list[str] = []

        def fake_run(cmd: list[str], **kwargs: Any) -> SimpleNamespace:
            captured.append(cmd[cmd.index("--jq") + 1])
            return SimpleNamespace(returncode=0, stdout="0\n", stderr="")

        monkeypatch.setattr(subprocess, "run", fake_run)
        fetch_round_count("o/r", "7")
        for jq_filter in captured:
            assert '"Bot"' in jq_filter
            assert "<!-- multi-llm-review -->" in jq_filter

    def test_api_failure_fails_open_to_zero(self, monkeypatch: Any) -> None:
        def fake_run(cmd: list[str], **kwargs: Any) -> SimpleNamespace:
            return SimpleNamespace(returncode=1, stdout="", stderr="boom")

        monkeypatch.setattr(subprocess, "run", fake_run)
        assert fetch_round_count("o/r", "7") == 0

    def test_timeout_fails_open_to_zero(self, monkeypatch: Any) -> None:
        def fake_run(cmd: list[str], **kwargs: Any) -> SimpleNamespace:
            raise subprocess.TimeoutExpired(cmd=cmd, timeout=1)

        monkeypatch.setattr(subprocess, "run", fake_run)
        assert fetch_round_count("o/r", "7") == 0


class TestPostCutoffSummary:
    _COMMENTS = [
        {"path": "a.py", "line": 3, "side": "RIGHT", "body": "- **minor**: x"},
        {"path": "b.py", "line": 9, "side": "RIGHT", "body": "? **suggestion**: y"},
    ]

    def test_posts_single_comment_with_template(self, monkeypatch: Any) -> None:
        recorded: dict[str, Any] = {}

        def fake_run(cmd: list[str], **kwargs: Any) -> SimpleNamespace:
            if cmd[:2] == ["gh", "api"]:
                # Marker-dedup check: no existing summary comment.
                return SimpleNamespace(returncode=0, stdout="0", stderr="")
            recorded["cmd"] = cmd
            recorded["body"] = kwargs.get("input")
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        monkeypatch.setattr(subprocess, "run", fake_run)
        post_cutoff_summary("o/r", "7", "claude", 6, self._COMMENTS)
        assert recorded["cmd"][:3] == ["gh", "pr", "comment"]
        body = recorded["body"]
        assert "<!-- round-cutoff-claude-r6 -->" in body
        assert "R6 convergence cutoff" in body
        assert "recommended for follow-up" in body
        assert "a.py:3" in body
        assert "b.py:9" in body

    def test_marker_line_is_what_the_skill_consumer_anchors_on(
        self, monkeypatch: Any, tmp_path: Path
    ) -> None:
        """The emitted marker line is found by cutoff_findings in SKILL.md, and only whole.

        The consumer captures the marker as a whole line. The body the producer
        emits must block there (nothing answers it); the control appends a suffix
        to that one line and the same consumer then reports no cutoff comment and
        passes -- the silent pass _CUTOFF_MARKER's comment warns about.
        """
        assert shutil.which("jq"), "jq must be on PATH: cutoff_findings is a jq program"
        real_run = subprocess.run
        # Extracted before subprocess.run is patched below: gate_function
        # runs the drill's own extract() through it.
        function_file = tmp_path / "cutoff_findings.sh"
        function_file.write_text(
            gate_function("finding_key") + "\n" + gate_function("cutoff_findings"),
            encoding="utf-8",
        )
        recorded: dict[str, Any] = {}

        def fake_run(cmd: list[str], **kwargs: Any) -> SimpleNamespace:
            if cmd[:2] == ["gh", "api"]:
                return SimpleNamespace(returncode=0, stdout="0", stderr="")
            recorded["body"] = kwargs.get("input")
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        monkeypatch.setattr(subprocess, "run", fake_run)
        post_cutoff_summary("o/r", "7", "claude", 6, self._COMMENTS)
        body = recorded["body"]

        real = _cutoff_findings(real_run, function_file, body)
        found = re.match(r"\(c\) BLOCK: (\S+) \(comment 1, ", real.stdout)
        assert found is not None, real.stdout + real.stderr
        assert real.returncode == 1
        marker_line, rest = body.split("\n", 1)
        assert found.group(1) in marker_line

        control = _cutoff_findings(
            real_run, function_file, marker_line + " (2 items)\n" + rest
        )
        assert control.returncode == 0, control.stdout + control.stderr
        assert (
            control.stdout.strip()
            == "(c) no round-cutoff or inline-fallback comment on this round's surface"
        )

    def test_existing_marker_skips_post(self, monkeypatch: Any) -> None:
        posted: list[list[str]] = []

        def fake_run(cmd: list[str], **kwargs: Any) -> SimpleNamespace:
            if cmd[:2] == ["gh", "api"]:
                return SimpleNamespace(
                    returncode=0,
                    stdout=json.dumps(
                        [
                            "github-actions[bot]",
                            "Bot",
                            "<!-- round-cutoff-claude-r6 -->\n<!-- fold-count: 2 -->",
                        ]
                    ),
                    stderr="",
                )
            posted.append(cmd)
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        monkeypatch.setattr(subprocess, "run", fake_run)
        post_cutoff_summary("o/r", "7", "claude", 6, self._COMMENTS)
        assert posted == []


class TestMainRoundCutoff:
    """End-to-end main() behavior around the cutoff gate."""

    _DIFF = "+++ b/a.py\n@@ -1,3 +1,3 @@\n line1\n+line2\n line3\n"

    def _write_inputs(self, tmp_path: Any, issues: list[dict[str, Any]]) -> None:
        review = {"summary": "s", "early_exit": False, "issues": issues}
        (tmp_path / "review-claude.json").write_text(json.dumps(review))
        (tmp_path / "pr.diff").write_text(self._DIFF)

    def _run_main(self, monkeypatch: Any, tmp_path: Any) -> None:
        monkeypatch.setenv("PR_NUMBER", "7")
        monkeypatch.setenv("GITHUB_REPOSITORY", "o/r")
        monkeypatch.setattr(
            post_inline_comments, "fetch_existing_threads", lambda repo, pr: []
        )
        # The cutoff route looks the head up for its fold comment; never a
        # real `gh pr view` from a test.
        monkeypatch.setattr(post_inline_comments, "get_pr_head_sha", lambda pr: "abc")
        monkeypatch.setattr(
            sys,
            "argv",
            [
                "post_inline_comments.py",
                "--issues",
                str(tmp_path / "review-claude.json"),
                "--diff",
                str(tmp_path / "pr.diff"),
                "--reviewer",
                "claude",
            ],
        )
        post_inline_comments.main()

    def test_cutoff_suppresses_inline_and_folds(
        self, monkeypatch: Any, tmp_path: Any
    ) -> None:
        self._write_inputs(tmp_path, [_cutoff_issue()])
        monkeypatch.setattr(
            post_inline_comments, "fetch_round_count", lambda repo, pr: 5
        )
        folded: dict[str, Any] = {}

        def record_summary(
            repo: str,
            pr: str,
            reviewer: str,
            round_number: int,
            comments: list[dict[str, Any]],
            head: str | None = None,
        ) -> None:
            folded.update(round_number=round_number, comments=comments)

        def fail_inline(*args: Any, **kwargs: Any) -> bool:
            raise AssertionError("inline review must not be posted under cutoff")

        monkeypatch.setattr(post_inline_comments, "post_cutoff_summary", record_summary)
        monkeypatch.setattr(post_inline_comments, "post_inline_review", fail_inline)
        self._run_main(monkeypatch, tmp_path)
        assert folded["round_number"] == 6
        assert len(folded["comments"]) == 1
        assert folded["comments"][0]["path"] == "a.py"

    def test_below_cutoff_posts_inline_normally(
        self, monkeypatch: Any, tmp_path: Any
    ) -> None:
        self._write_inputs(tmp_path, [_cutoff_issue()])
        monkeypatch.setattr(
            post_inline_comments, "fetch_round_count", lambda repo, pr: 0
        )
        posted: dict[str, Any] = {}

        def record_inline(
            repo: str,
            pr: str,
            sha: str,
            reviewer: str,
            comments: list[dict[str, Any]],
        ) -> bool:
            posted.update(comments=comments)
            return True

        def fail_summary(*args: Any, **kwargs: Any) -> None:
            raise AssertionError("cutoff summary must not be posted below cutoff")

        monkeypatch.setattr(post_inline_comments, "get_pr_head_sha", lambda pr: "abc")
        monkeypatch.setattr(post_inline_comments, "post_inline_review", record_inline)
        monkeypatch.setattr(post_inline_comments, "post_cutoff_summary", fail_summary)
        self._run_main(monkeypatch, tmp_path)
        assert len(posted["comments"]) == 1

    def test_major_at_cutoff_posts_inline_normally(
        self, monkeypatch: Any, tmp_path: Any
    ) -> None:
        # Distinct description so batch-internal dedup keeps both findings.
        self._write_inputs(
            tmp_path,
            [
                _cutoff_issue(),
                _cutoff_issue(
                    severity="major",
                    line=3,
                    description="hardcoded credentials in loader",
                ),
            ],
        )
        monkeypatch.setattr(
            post_inline_comments, "fetch_round_count", lambda repo, pr: 9
        )
        posted: dict[str, Any] = {}

        def record_inline(
            repo: str,
            pr: str,
            sha: str,
            reviewer: str,
            comments: list[dict[str, Any]],
        ) -> bool:
            posted.update(comments=comments)
            return True

        def fail_summary(*args: Any, **kwargs: Any) -> None:
            raise AssertionError("cutoff must not apply when a major is present")

        monkeypatch.setattr(post_inline_comments, "get_pr_head_sha", lambda pr: "abc")
        monkeypatch.setattr(post_inline_comments, "post_inline_review", record_inline)
        monkeypatch.setattr(post_inline_comments, "post_cutoff_summary", fail_summary)
        self._run_main(monkeypatch, tmp_path)
        assert len(posted["comments"]) == 2


class TestFoldedFindingGate:
    @pytest.fixture
    def folded(self, monkeypatch: Any, request: Any) -> tuple[str, str]:
        """Run both real producers, replacing only GitHub transport."""
        captured: list[str] = []

        def transport(cmd: list[str], **kwargs: Any) -> SimpleNamespace:
            if cmd[:2] == ["gh", "api"]:
                return SimpleNamespace(returncode=0, stdout="0", stderr="")
            captured.append(kwargs["input"])
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        issues = [_issue("a.py", 1, "first"), _issue("b.py", 2, "second")]
        param = getattr(request, "param", False)
        if param == "separator":
            issues[0]["description"] = "first\n- **details** -- explanatory text"
            issues[0]["suggestion"] = "first line\n- **bold** -- explanatory text"
        elif param == "location-prose":
            issues[0]["description"] = (
                "first\n- **some/file.py:12** -- explanatory text"
            )
            issues[0]["suggestion"] = "first line\n- **a.py:1** -- explanatory text"
        elif param == "collision-template":
            issues = [
                _issue(
                    "a.py", 1, "Validate authentication before forwarding credentials"
                ),
                _issue("a.py", 1, "Close the socket after the retry budget expires"),
            ]
        elif param == "identical-first-lines":
            issues = [
                _issue(
                    "a.py", 1, "\nValidate authentication before forwarding credentials"
                ),
                _issue("a.py", 1, "\nClose the socket after the retry budget expires"),
            ]
        elif param == "unknown":
            issues[0]["severity"] = "future level"
        elif param == "empty-severity":
            issues[0]["severity"] = ""
        elif param == "asterisk-severity":
            issues[0]["severity"] = "mi*nor"
        elif param == "newline-severity":
            issues[0]["severity"] = "mi\nnor"
        elif param == "asterisk-path":
            issues[0]["file"] = "a*b.py"
        elif param == "whitespace":
            issues[0]["description"] = "\nfirst continuation"
            issues[1]["description"] = "second  with internal spaces   "
        elif param:
            issues[0]["description"] += "\n- **details** ordinary continuation"
            issues[0]["suggestion"] = "first line\n- **bold** second line"
        reviewer = (
            "claude)extension"
            if param == "parenthesized-reviewer"
            else "custom-r7"
            if param == "round-looking-reviewer"
            else "claude"
        )
        valid_lines = {"a.py": {1}, "b.py": {2}}
        if param == "asterisk-path":
            valid_lines = post_inline_comments.parse_diff(
                "diff --git a/a*b.py b/a*b.py\n--- a/a*b.py\n+++ b/a*b.py\n"
                "@@ -0,0 +1 @@\n+first\n"
                "diff --git a/b.py b/b.py\n--- a/b.py\n+++ b/b.py\n"
                "@@ -0,0 +2 @@\n+second\n"
            )
            assert valid_lines == {"a*b.py": {1}, "b.py": {2}}
        comments, no_location, out_of_range = build_comments(
            issues, valid_lines, [], reviewer
        )
        assert len(comments) == 2 and no_location == out_of_range == 0
        with monkeypatch.context() as scoped:
            scoped.setattr(subprocess, "run", transport)
            post_inline_comments.post_fallback("o/r", "7", reviewer, 14, comments)
            post_cutoff_summary("o/r", "7", reviewer, 14, comments)
        return captured[0], captured[1]

    @staticmethod
    def run_gate(
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

    @pytest.mark.parametrize("index", [0, 1])
    def test_each_real_producer_requires_every_item(
        self, folded: tuple[str, str], index: int
    ) -> None:
        body = folded[index]
        marker = body.splitlines()[0]
        for reply, expected in [
            ("", 1),
            (marker + "\n> a.py:1\nFixed: first", 1),
            (marker + "\n> a.py:1\nFixed: first\n> b.py:2\nFixed: second", 0),
        ]:
            # A response names the marker inline, never in the producer's first-line form.
            result = self.run_gate(body, "Dispositions: " + reply if reply else "")
            assert result.returncode == expected, result.stdout + result.stderr

    @pytest.mark.parametrize(
        "envelope",
        ["<!-- fold-count: 2 -->", "<!-- fold-count: 2 head: " + "a" * 40 + " -->"],
    )
    def test_round_scoped_fallback_with_count_is_found(
        self, folded: tuple[str, str], envelope: str
    ) -> None:
        _, rest = folded[0].split("\n", 1)
        body = "<!-- inline-fallback-claude-r14 -->\n" + envelope + "\n" + rest
        assert self.run_gate(body).returncode == 1
        result = self.run_gate(
            body,
            "inline-fallback-claude-r14\n> a.py:1\nFixed: first\n> b.py:2\nFixed: second",
        )
        assert result.returncode == 0, result.stdout + result.stderr

    @pytest.mark.parametrize(
        "envelope",
        [
            "<!-- fold-count: 3 -->",
            "<!-- fold-count: bad -->",
            "<!-- fold-count: 0 -->",
        ],
    )
    def test_bad_counts_do_not_pass_with_all_locations_quoted(
        self, folded: tuple[str, str], envelope: str
    ) -> None:
        first, rest = folded[0].split("\n", 1)
        result = self.run_gate(
            first + "\n" + envelope + "\n" + rest,
            "inline-fallback-claude-r14\n> a.py:1\n> b.py:2\nFixed: both",
        )
        assert result.returncode == 1, result.stdout + result.stderr
        reason = (
            "malformed fold-count envelope"
            if "bad" in envelope
            else "declared fold-count does not match rendered findings"
        )
        assert "invalid folded findings: " + reason in result.stdout
        assert result.stderr == ""

    def test_same_location_findings_need_separate_whole_quotes(
        self, folded: tuple[str, str]
    ) -> None:
        body = folded[0].replace("b.py:2", "a.py:1")
        first, second = [line for line in body.splitlines() if line.startswith("- **")]
        reply = "inline-fallback-claude-r14\n> " + first + "\nFixed: first"
        partial = self.run_gate(body, reply)
        assert partial.returncode == 1
        assert (
            "a.py:1 (whole item line required: 2 items share this location)"
            in partial.stdout
        )
        result = self.run_gate(body, reply + "\n> " + second + "\nFixed: second")
        assert result.returncode == 0, result.stdout + result.stderr

    @pytest.mark.parametrize(
        "body",
        [
            "<!-- inline-fallback-claude -->\nheader only",
            "<!-- round-cutoff-claude-r14 -->\n- **a.py:1** invalid",
        ],
    )
    def test_empty_or_malformed_items_fail_closed(self, body: str) -> None:
        result = self.run_gate(
            body,
            "inline-fallback-claude round-cutoff-claude-r14\n> a.py:1\nFixed: item",
        )
        assert result.returncode == 1, result.stdout + result.stderr
        assert (
            "invalid folded findings: empty or unparseable folded finding list"
            in result.stdout
        )
        assert result.stderr == ""

    def test_crlf_and_marker_quotations(self, folded: tuple[str, str]) -> None:
        crlf_body = folded[0].replace("\n", "\r\n")
        absent = self.run_gate(crlf_body)
        assert absent.returncode == 1
        assert "carries findings that have no thread id" in absent.stdout
        result = self.run_gate(
            crlf_body, "inline-fallback-claude-r14\n> a.py:1\n> b.py:2\nFixed: both"
        )
        assert result.returncode == 0, result.stdout + result.stderr
        for body in [
            "Description\n" + folded[0],
            "> " + folded[0],
            folded[0].replace(" -->", " --> suffix", 1),
        ]:
            result = self.run_gate(body)
            assert result.returncode == 0, result.stdout + result.stderr

    @pytest.mark.parametrize("index", [0, 1])
    @pytest.mark.parametrize(
        "author,kind",
        [
            ("github-actions[bot]", "Bot"),
            ("ignite-ai-review-approver[bot]", "Bot"),
            ("local-operator", "User"),
        ],
    )
    def test_all_producer_identities_and_another_reviewer_can_answer(
        self,
        folded: tuple[str, str],
        index: int,
        author: str,
        kind: str,
    ) -> None:
        body = folded[index]
        assert (
            self.run_gate(body, source_author=author, source_type=kind).returncode == 1
        )
        reply = (
            "Dispositions for "
            + body.splitlines()[0]
            + "\n> a.py:1\nFixed: first\n> b.py:2\nFixed: second"
        )
        result = self.run_gate(
            body,
            reply,
            source_author=author,
            source_type=kind,
            reply_author="another-reviewer",
        )
        assert result.returncode == 0, result.stdout + result.stderr
        result = self.run_gate(
            body,
            reply,
            source_author=author,
            source_type=kind,
            reply_author="review-helper[bot]",
            reply_type="Bot",
        )
        assert result.returncode == 1, result.stdout + result.stderr

    @pytest.mark.parametrize(
        "marker", ["inline-fallback-claude-r15", "round-cutoff-claude"]
    )
    def test_local_producer_repost_cannot_answer_earlier_fallback(
        self, folded: tuple[str, str], marker: str
    ) -> None:
        reply = (
            "<!-- " + marker + " -->\n"
            "inline-fallback-claude-r14\n> a.py:1\nFixed: first\n> b.py:2\nFixed: second"
        )
        result = self.run_gate(
            folded[0], reply, source_type="User", source_author="local-operator"
        )
        assert result.returncode == 1, result.stdout + result.stderr
        assert "BLOCK: inline-fallback-claude-r14 (comment 1," in result.stdout

    @pytest.mark.parametrize("folded", [True], indirect=True)
    @pytest.mark.parametrize("index", [0, 1])
    def test_multiline_description_and_suggestion_are_not_extra_findings(
        self,
        folded: tuple[str, str],
        index: int,
    ) -> None:
        body = folded[index]
        assert "\n- **details** ordinary continuation" in body
        assert "\n- **bold** second line" in body
        marker = body.splitlines()[0]
        partial = "Dispositions: " + marker + "\n> a.py:1\nFixed: first"
        assert self.run_gate(body, partial).returncode == 1
        result = self.run_gate(body, partial + "\n> b.py:2\nFixed: second")
        assert result.returncode == 0, result.stdout + result.stderr

    def test_legacy_cutoff_marker_is_found_for_a_local_producer(
        self, folded: tuple[str, str]
    ) -> None:
        body = folded[1].replace("round-cutoff-claude-r14", "round-cutoff-claude")
        assert (
            self.run_gate(
                body, source_type="User", source_author="local-reviewer"
            ).returncode
            == 1
        )
        result = self.run_gate(
            body, "round-cutoff-claude\n> a.py:1\n> b.py:2\nFixed: both"
        )
        assert result.returncode == 0, result.stdout + result.stderr

    @pytest.mark.parametrize(
        "marker", ["inline-fallback-claude-r14", "prefix-inline-fallback-claude"]
    )
    def test_another_marker_does_not_answer_legacy_marker(
        self, folded: tuple[str, str], marker: str
    ) -> None:
        legacy = folded[0].replace(
            "inline-fallback-claude-r14", "inline-fallback-claude"
        )
        result = self.run_gate(legacy, marker + "\n> a.py:1\n> b.py:2\nFixed: both")
        assert result.returncode == 1, result.stdout + result.stderr

    @pytest.mark.parametrize("folded", ["separator"], indirect=True)
    @pytest.mark.parametrize("index", [0, 1])
    @pytest.mark.parametrize("with_count", [False, True])
    def test_bold_separator_prose_is_not_a_location(
        self,
        folded: tuple[str, str],
        index: int,
        with_count: bool,
    ) -> None:
        body = folded[index]
        assert "\n- **details** -- explanatory text" in body
        assert "\n- **bold** -- explanatory text" in body
        marker, rest = body.split("\n", 1)
        if with_count:
            body = marker + "\n<!-- fold-count: 2 head: " + "a" * 40 + " -->\n" + rest
        reply = "Dispositions: " + marker + "\n> a.py:1\nFixed: first"
        assert self.run_gate(body, reply).returncode == 1
        result = self.run_gate(body, reply + "\n> b.py:2\nFixed: second")
        assert result.returncode == 0, result.stdout + result.stderr

    @pytest.mark.parametrize("path", ["dir with space/a.py", "scheme:component/a.py"])
    def test_location_paths_preserve_spaces_and_colons(
        self, folded: tuple[str, str], path: str
    ) -> None:
        body = folded[0].replace("a.py", path)
        reply = "inline-fallback-claude-r14\n> " + path + ":1\n> b.py:2\nFixed: both"
        result = self.run_gate(body, reply)
        assert result.returncode == 0, result.stdout + result.stderr

    def test_missing_jq_fails_before_a_negative_case_can_pass(
        self, monkeypatch: Any
    ) -> None:
        monkeypatch.setattr(shutil, "which", lambda name: None)
        with pytest.raises(AssertionError, match="jq must be on PATH"):
            self.run_gate("<!-- inline-fallback-claude -->\n- **a.py:1** -- item")

    @pytest.mark.parametrize("folded", ["whitespace"], indirect=True)
    def test_collision_quotes_can_drop_only_trailing_whitespace(
        self, folded: tuple[str, str]
    ) -> None:
        body = folded[0].replace("b.py:2", "a.py:1")
        first, second = [line for line in body.splitlines() if line.startswith("- **")]
        assert first.endswith(": ") and second.endswith("   ")
        partial = "inline-fallback-claude-r14\n> " + first.rstrip() + "\nFixed: first"
        assert self.run_gate(body, partial).returncode == 1
        wrong = (
            partial + "\n> " + second.rstrip().replace("second  with", "second with")
        )
        assert self.run_gate(body, wrong).returncode == 1
        result = self.run_gate(
            body, partial + "\n> " + second.rstrip() + "\nFixed: second"
        )
        assert result.returncode == 0, result.stdout + result.stderr

    def test_jq_runtime_error_is_not_a_policy_diagnostic(
        self, folded: tuple[str, str], monkeypatch: Any
    ) -> None:
        extracted = gate_function("cutoff_findings")
        original_gate_function = gate_function
        # Fail only the items query, after marker discovery and reply matching.
        shim = 'jq() { if [[ " $* " == *" --arg i "* ]]; then return 17; fi; command jq "$@"; }'
        monkeypatch.setattr(
            sys.modules[__name__],
            "gate_function",
            lambda name: extracted + "\n" + shim
            if name == "cutoff_findings"
            else original_gate_function(name),
        )
        result = self.run_gate(
            folded[0], "inline-fallback-claude-r14\n> a.py:1\n> b.py:2\nFixed: both"
        )
        assert result.returncode == 1, result.stdout + result.stderr
        assert "could not read the items" in result.stdout
        assert "invalid folded findings" not in result.stdout

    @pytest.mark.parametrize("folded", ["location-prose"], indirect=True)
    @pytest.mark.parametrize("index", [0, 1])
    @pytest.mark.parametrize("with_count", [False, True])
    def test_path_like_description_and_suggestion_are_not_findings(
        self, folded: tuple[str, str], index: int, with_count: bool
    ) -> None:
        body = folded[index]
        assert "\n- **some/file.py:12** -- explanatory text" in body
        assert "\n- **a.py:1** -- explanatory text" in body
        marker, rest = body.split("\n", 1)
        if with_count:
            body = marker + "\n<!-- fold-count: 2 head: " + "a" * 40 + " -->\n" + rest
        reply = "Dispositions: " + marker + "\n> a.py:1\nFixed: first"
        assert self.run_gate(body, reply).returncode == 1
        result = self.run_gate(body, reply + "\n> b.py:2\nFixed: second")
        assert result.returncode == 0, result.stdout + result.stderr

    @pytest.mark.parametrize("folded", ["collision-template"], indirect=True)
    def test_documented_collision_reply_answers_the_real_producer(
        self, folded: tuple[str, str]
    ) -> None:
        section = SKILL.read_text().split("**Colliding folded locations**", 1)[1]
        reply = section.split("```text\n", 1)[1].split("\n```", 1)[0]
        quotes = [line for line in reply.splitlines() if line.startswith("> ")]
        assert len(quotes) == 2
        assert self.run_gate(folded[0], reply.replace(quotes[1], "", 1)).returncode == 1
        result = self.run_gate(folded[0], reply)
        assert result.returncode == 0, result.stdout + result.stderr

    @pytest.mark.parametrize("folded", ["unknown", "empty-severity"], indirect=True)
    def test_prefix_does_not_whitelist_severity(self, folded: tuple[str, str]) -> None:
        assert self.run_gate(folded[0]).returncode == 1
        missing = self.run_gate(
            folded[0], "inline-fallback-claude-r14\n> b.py:2\nFixed: second"
        )
        assert missing.returncode == 1, missing.stdout + missing.stderr
        assert "a.py:1" in missing.stdout
        result = self.run_gate(
            folded[0], "inline-fallback-claude-r14\n> a.py:1\n> b.py:2\nFixed: both"
        )
        assert result.returncode == 0, result.stdout + result.stderr

    @pytest.mark.parametrize("folded", ["asterisk-path"], indirect=True)
    @pytest.mark.parametrize("index", [0, 1])
    @pytest.mark.parametrize("counted", [False, True])
    def test_real_asterisk_path_cannot_be_silently_dropped(
        self, folded: tuple[str, str], index: int, counted: bool
    ) -> None:
        body = folded[index]
        marker, rest = body.split("\n", 1)
        if counted:
            body = marker + "\n<!-- fold-count: 2 -->\n" + rest
        partial = "Dispositions: " + marker + "\n> b.py:2\nFixed: second"
        missing = self.run_gate(body, partial)
        assert missing.returncode == 1, missing.stdout + missing.stderr
        assert "a*b.py:1" in missing.stdout
        result = self.run_gate(body, partial + "\n> a*b.py:1\nFixed: first")
        assert result.returncode == 0, result.stdout + result.stderr

    @pytest.mark.parametrize(
        "folded",
        ["asterisk-severity", "newline-severity", "parenthesized-reviewer"],
        indirect=True,
    )
    def test_raw_producer_fields_do_not_silently_drop_findings(
        self, folded: tuple[str, str]
    ) -> None:
        body = folded[0]
        marker = body.splitlines()[0]
        partial = "Dispositions: " + marker + "\n> b.py:2\nFixed: second"
        missing = self.run_gate(body, partial)
        assert missing.returncode == 1, missing.stdout + missing.stderr
        assert "a.py:1" in missing.stdout
        result = self.run_gate(body, partial + "\n> a.py:1\nFixed: first")
        assert result.returncode == 0, result.stdout + result.stderr

    @pytest.mark.parametrize("folded", ["identical-first-lines"], indirect=True)
    def test_identical_first_lines_require_one_quote_per_finding(
        self, folded: tuple[str, str]
    ) -> None:
        body = folded[0]
        first, second = [line for line in body.splitlines() if line.startswith("- **")]
        assert first == second
        one = (
            "inline-fallback-claude-r14\n> "
            + first.rstrip()
            + "\nFixed: authentication"
        )
        assert self.run_gate(body, one).returncode == 1
        result = self.run_gate(body, one + "\n> " + second.rstrip() + "\nFixed: socket")
        assert result.returncode == 0, result.stdout + result.stderr

    @pytest.mark.parametrize("folded", [False, "round-looking-reviewer"], indirect=True)
    def test_legacy_fallback_remains_visible_after_the_head_window_moves(
        self, folded: tuple[str, str]
    ) -> None:
        body = folded[0].replace("-r14 -->", " -->", 1)
        old = "2026-10-21T00:00:00Z"
        assert self.run_gate(body, source_at=old).returncode == 1
        reply = (
            "Dispositions: "
            + body.splitlines()[0]
            + "\n> a.py:1\n> b.py:2\nFixed: both"
        )
        result = self.run_gate(body, reply, source_at=old)
        assert result.returncode == 0, result.stdout + result.stderr

    def test_round_scoped_fallback_history_is_also_retained(
        self, folded: tuple[str, str]
    ) -> None:
        _, rest = folded[0].split("\n", 1)
        body = (
            "<!-- inline-fallback-claude-r14 -->\n<!-- fold-count: 2 head: "
            + "a" * 40
            + " -->\n"
            + rest
        )
        old = "2026-10-21T00:00:00Z"
        assert self.run_gate(body, source_at=old).returncode == 1
        result = self.run_gate(
            body,
            "inline-fallback-claude-r14\n> a.py:1\n> b.py:2\nFixed: both",
            source_at=old,
        )
        assert result.returncode == 0, result.stdout + result.stderr

    def test_old_cutoff_stays_outside_the_current_head_window(
        self, folded: tuple[str, str]
    ) -> None:
        result = self.run_gate(folded[1], source_at="2026-10-21T00:00:00Z")
        assert result.returncode == 0, result.stdout + result.stderr
        assert "no round-cutoff or inline-fallback" in result.stdout

    def test_old_invalid_fallback_remains_a_policy_block(
        self, folded: tuple[str, str]
    ) -> None:
        marker, rest = folded[0].split("\n", 1)
        body = marker + "\n<!-- fold-count: 3 -->\n" + rest
        result = self.run_gate(
            body,
            "inline-fallback-claude-r14\n> a.py:1\n> b.py:2\nFixed: both",
            source_at="2026-10-21T00:00:00Z",
        )
        assert result.returncode == 1
        assert "declared fold-count does not match rendered findings" in result.stdout
