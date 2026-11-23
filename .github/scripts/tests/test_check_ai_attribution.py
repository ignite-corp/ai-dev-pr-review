"""check_ai_attribution.py fails a PR whose commits or body carry AI attribution.

AT-3651: commit d01d082 (PR #314, AT-3592) reached main with a Claude
co-author trailer and a session-link trailer. The patterns here are the ones
that commit carried plus the PR-body footer the same harness asks for; human
co-author trailers must keep passing.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

import check_ai_attribution as cka

ROBOT = "\U0001f916"

# The message of d01d082 as it reached main, trailers verbatim apart from the
# session id.
D01D082 = (
    "feat(AT-3592): leave lens-ignore files out of the size count\n"
    "\n"
    "Files marked lens-ignore no longer count toward the size gate.\n"
    "\n"
    "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>\n"
    "Claude-Session: https://claude.ai/code/session_01ABCDEF\n"
)
CLEAN = (
    "feat(AT-3592): leave lens-ignore files out of the size count\n"
    "\n"
    "Files marked lens-ignore no longer count toward the size gate.\n"
)


class TestFindAttribution:
    def test_d01d082_message_is_caught_by_every_rule_it_breaks(self) -> None:
        assert cka.find_attribution(D01D082) == [
            "claude-session-trailer",
            "claude-session-url",
            "co-authored-by-ai",
        ]

    def test_clean_message_passes(self) -> None:
        assert cka.find_attribution(CLEAN) == []

    @pytest.mark.parametrize(
        "line",
        [
            "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>",
            "co-authored-by: claude sonnet 4 <noreply@anthropic.com>",
            "CO-AUTHORED-BY: Claude <noreply@anthropic.com>",
            "Co-authored-by: Claude Code <claude@example.com>",
            "Co-authored-by: Claude 3.5 Sonnet <bot@example.com>",
            "Co-authored-by: Claude",
            "Co-authored-by: Someone <someone@anthropic.com>",
            "Co-authored-by: Anthropic Bot <bot@example.com>",
            "  Co-Authored-By: Claude Haiku <noreply@anthropic.com>",
            "Co-authored-by: claude[bot] <1+claude[bot]@users.noreply.github.com>",
            "Co-authored-by: Claude AI <ai@example.com>",
            "Co-authored-by: Claude-Opus-4 <x@example.com>",
        ],
    )
    def test_ai_co_author_trailers_are_caught(self, line: str) -> None:
        assert cka.find_attribution(f"subject\n\n{line}\n") == ["co-authored-by-ai"]

    @pytest.mark.parametrize(
        "line",
        [
            "Co-authored-by: Hyuk Hur <hyuk@example.com>",
            "Co-authored-by: Claude Monet <claude.monet@example.fr>",
            "Co-authored-by: Claudia Schiffer <cs@example.com>",
            "Co-authored-by: Claude-Michel Martin <cm@example.fr>",
            "Co-authored-by: Claude Aiello <ca@example.com>",
            "Co-authored-by: Jane <jane@anthropicresearch.example>",
            "Co-authored-by: dependabot[bot] <support@github.com>",
        ],
    )
    def test_human_co_author_trailers_pass(self, line: str) -> None:
        assert cka.find_attribution(f"subject\n\n{line}\n") == []

    def test_session_trailer_is_caught_case_insensitively(self) -> None:
        assert cka.find_attribution("x\n\nclaude-session: abc\n") == [
            "claude-session-trailer"
        ]

    def test_session_url_is_caught_anywhere(self) -> None:
        assert cka.find_attribution(
            "see https://CLAUDE.ai/code/session_xyz for the log"
        ) == ["claude-session-url"]

    def test_generated_with_footer_is_caught(self) -> None:
        body = (
            f"## Summary\n\nstuff\n\n{ROBOT} Generated with "
            "[Claude Code](https://claude.com/claude-code)\n"
        )
        assert cka.find_attribution(body) == [
            "generated-with-claude-code",
            "robot-generated-with",
        ]

    def test_robot_footer_without_claude_code_is_caught(self) -> None:
        assert cka.find_attribution(f"{ROBOT} generated with something") == [
            "robot-generated-with"
        ]

    def test_prose_naming_the_trailers_mid_line_passes(self) -> None:
        # A PR body or commit that discusses the rule without carrying it.
        text = (
            "This blocks the `Co-Authored-By` and `Claude-Session` trailers,\n"
            "and the Claude Code footer. Claude reviewed nothing here.\n"
        )
        assert cka.find_attribution(text) == []

    def test_mid_line_session_trailer_word_is_not_a_trailer(self) -> None:
        assert cka.find_attribution("the Claude-Session: trailer is banned") == []


def _event(tmp_path: Path, payload: dict[str, Any]) -> Path:
    path = tmp_path / "event.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


class FakeGh:
    """Stands in for `gh api`, keyed on the API path."""

    def __init__(self, responses: dict[str, Any]) -> None:
        self.responses = responses
        self.calls: list[str] = []

    def __call__(self, path: str) -> Any:
        self.calls.append(path)
        if path not in self.responses:
            raise cka.GhError(f"unexpected path {path}")
        return self.responses[path]


def _commit(sha: str, message: str) -> dict[str, Any]:
    return {"sha": sha, "commit": {"message": message}}


def _run(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    event_name: str,
    payload: dict[str, Any],
    responses: dict[str, Any],
) -> tuple[int, str, FakeGh]:
    gh = FakeGh(responses)
    status = cka.main(
        [
            "--event-name",
            event_name,
            "--event-path",
            str(_event(tmp_path, payload)),
            "--repo",
            "o/r",
        ],
        gh=gh,
    )
    return status, capsys.readouterr().out, gh


PR_PATH = "repos/o/r/pulls/7"
COMMITS_PATH = "repos/o/r/pulls/7/commits?per_page=100"


class TestMain:
    def test_pull_request_with_d01d082_commit_fails(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        status, out, _ = _run(
            tmp_path,
            capsys,
            "pull_request",
            {"pull_request": {"number": 7}},
            {
                PR_PATH: {"body": "clean body", "commits": 2},
                COMMITS_PATH: [
                    _commit("a" * 40, CLEAN),
                    _commit("d01d082" + "0" * 33, D01D082),
                ],
            },
        )
        assert status == 1
        assert "::error::commit d01d082: co-authored-by-ai" in out
        assert "::error::commit d01d082: claude-session-trailer" in out
        assert "aaaaaaa" not in out
        # Only the rule and the commit are printed, never the message text.
        assert "noreply@anthropic.com" not in out

    def test_pull_request_all_clean_passes(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        status, out, gh = _run(
            tmp_path,
            capsys,
            "pull_request",
            {"pull_request": {"number": 7, "body": "stale payload body"}},
            {
                PR_PATH: {"body": "clean body", "commits": 1},
                COMMITS_PATH: [_commit("a" * 40, CLEAN)],
            },
        )
        assert status == 0, out
        assert "no AI attribution in 1 commit(s) and the PR body" in out
        assert gh.calls == [PR_PATH, COMMITS_PATH]

    def test_body_is_read_live_not_from_the_payload(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # A re-run replays the original payload, so a body fixed after the
        # push must be read from the API or the check can never go green.
        status, out, _ = _run(
            tmp_path,
            capsys,
            "pull_request",
            {"pull_request": {"number": 7, "body": f"{ROBOT} Generated with x"}},
            {
                PR_PATH: {"body": "fixed", "commits": 1},
                COMMITS_PATH: [_commit("a" * 40, CLEAN)],
            },
        )
        assert status == 0, out

    def test_pull_request_body_footer_fails(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        status, out, _ = _run(
            tmp_path,
            capsys,
            "pull_request",
            {"pull_request": {"number": 7}},
            {
                PR_PATH: {
                    "body": f"text\n\n{ROBOT} Generated with [Claude Code](u)",
                    "commits": 1,
                },
                COMMITS_PATH: [_commit("a" * 40, CLEAN)],
            },
        )
        assert status == 1
        assert "::error::PR body: generated-with-claude-code" in out
        assert "::error::PR body: robot-generated-with" in out

    def test_null_body_is_empty(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        status, _, _ = _run(
            tmp_path,
            capsys,
            "pull_request",
            {"pull_request": {"number": 7}},
            {
                PR_PATH: {"body": None, "commits": 1},
                COMMITS_PATH: [_commit("a" * 40, CLEAN)],
            },
        )
        assert status == 0

    def test_commit_list_shorter_than_the_pr_count_cannot_be_evaluated(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # The PR commits endpoint stops at 250; a longer PR is not "clean".
        status, out, _ = _run(
            tmp_path,
            capsys,
            "pull_request",
            {"pull_request": {"number": 7}},
            {
                PR_PATH: {"body": "", "commits": 251},
                COMMITS_PATH: [_commit("a" * 40, CLEAN)] * 250,
            },
        )
        assert status == 2
        assert "251" in out and "250" in out

    def test_workflow_dispatch_reads_the_input_pr(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        status, _, gh = _run(
            tmp_path,
            capsys,
            "workflow_dispatch",
            {"inputs": {"pr_number": "7"}},
            {
                PR_PATH: {"body": "", "commits": 1},
                COMMITS_PATH: [_commit("d01d082" + "0" * 33, D01D082)],
            },
        )
        assert status == 1
        assert gh.calls == [PR_PATH, COMMITS_PATH]

    @pytest.mark.parametrize("value", ["", "7; rm -rf /", "../7", "-1"])
    def test_workflow_dispatch_rejects_a_non_numeric_pr(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str], value: str
    ) -> None:
        status, _, gh = _run(
            tmp_path,
            capsys,
            "workflow_dispatch",
            {"inputs": {"pr_number": value}},
            {},
        )
        assert status == 2
        assert gh.calls == []

    def test_merge_group_reads_the_base_to_head_range(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        base, head = "b" * 40, "c" * 40
        path = f"repos/o/r/compare/{base}...{head}?per_page=100"
        status, out, gh = _run(
            tmp_path,
            capsys,
            "merge_group",
            {"merge_group": {"base_sha": base, "head_sha": head}},
            {
                path: {
                    "total_commits": 2,
                    "commits": [
                        _commit("a" * 40, CLEAN),
                        _commit("d01d082" + "0" * 33, D01D082),
                    ],
                }
            },
        )
        assert status == 1
        assert "::error::commit d01d082: co-authored-by-ai" in out
        assert gh.calls == [path]

    def test_push_reads_before_to_after(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        base, head = "b" * 40, "c" * 40
        path = f"repos/o/r/compare/{base}...{head}?per_page=100"
        status, out, _ = _run(
            tmp_path,
            capsys,
            "push",
            {"before": base, "after": head},
            {path: {"total_commits": 1, "commits": [_commit("a" * 40, CLEAN)]}},
        )
        assert status == 0, out
        assert "no AI attribution in 1 commit(s)" in out

    def test_push_creating_a_branch_cannot_be_evaluated(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        status, _, gh = _run(
            tmp_path,
            capsys,
            "push",
            {"before": "0" * 40, "after": "c" * 40},
            {},
        )
        assert status == 2
        assert gh.calls == []

    def test_compare_truncated_range_cannot_be_evaluated(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        base, head = "b" * 40, "c" * 40
        path = f"repos/o/r/compare/{base}...{head}?per_page=100"
        status, out, _ = _run(
            tmp_path,
            capsys,
            "push",
            {"before": base, "after": head},
            {path: {"total_commits": 300, "commits": [_commit("a" * 40, CLEAN)]}},
        )
        assert status == 2
        # Compare pages are joined, so the shortfall is not the 250 cap.
        assert "returned 1 of 300 commits" in out
        assert "250" not in out

    def test_unsupported_event_fails_closed(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        status, out, _ = _run(tmp_path, capsys, "schedule", {}, {})
        assert status == 2
        assert "schedule" in out

    def test_api_failure_cannot_be_evaluated(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        status, out, _ = _run(
            tmp_path, capsys, "pull_request", {"pull_request": {"number": 7}}, {}
        )
        assert status == 2
        assert "::error::" in out


HEAD = "d" * 40


class TestMergeGateMode:
    """`--pr N --head-sha SHA`: the pr-response-cycle merge gate's call."""

    def _run(
        self, capsys: pytest.CaptureFixture[str], pr: dict[str, Any], commits: Any
    ) -> tuple[int, str, FakeGh]:
        gh = FakeGh({PR_PATH: pr, COMMITS_PATH: commits})
        status = cka.main(["--repo", "o/r", "--pr", "7", "--head-sha", HEAD], gh=gh)
        return status, capsys.readouterr().out, gh

    def test_clean_pr_at_the_named_head_passes(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        status, out, gh = self._run(
            capsys,
            {"body": "", "commits": 1, "head": {"sha": HEAD}},
            [_commit(HEAD, CLEAN)],
        )
        assert status == 0, out
        assert "no AI attribution in 1 commit(s) and the PR body" in out
        assert gh.calls == [PR_PATH, COMMITS_PATH]

    def test_attribution_at_the_named_head_fails(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        status, out, _ = self._run(
            capsys,
            {"body": "", "commits": 1, "head": {"sha": HEAD}},
            [_commit(HEAD, D01D082)],
        )
        assert status == 1
        assert "co-authored-by-ai" in out

    def test_pr_head_other_than_the_named_one_is_not_evaluated(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        status, out, _ = self._run(
            capsys,
            {"body": "", "commits": 1, "head": {"sha": "e" * 40}},
            [_commit("e" * 40, CLEAN)],
        )
        assert status == 2
        assert "moved" in out

    def test_head_listed_before_an_older_commit_passes(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # The commits endpoint is not promised to end on the head (a rebase
        # can keep older dates); the head is required to be listed, not last.
        status, out, _ = self._run(
            capsys,
            {"body": "", "commits": 2, "head": {"sha": HEAD}},
            [_commit(HEAD, CLEAN), _commit("a" * 40, CLEAN)],
        )
        assert status == 0, out

    def test_commit_list_without_the_head_is_not_evaluated(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # The head moved between the two reads: the PR said HEAD, the
        # commit list was read off a later push.
        status, out, _ = self._run(
            capsys,
            {"body": "", "commits": 1, "head": {"sha": HEAD}},
            [_commit("e" * 40, CLEAN)],
        )
        assert status == 2
        assert "moved" in out

    @pytest.mark.parametrize("bad", ["", "abc", "7; x", "0"])
    def test_bad_pr_number_is_not_evaluated(
        self, capsys: pytest.CaptureFixture[str], bad: str
    ) -> None:
        gh = FakeGh({})
        status = cka.main(["--repo", "o/r", "--pr", bad, "--head-sha", HEAD], gh=gh)
        assert status == 2
        assert gh.calls == []

    def test_head_sha_without_pr_is_not_evaluated(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        gh = FakeGh({})
        assert cka.main(["--repo", "o/r", "--head-sha", HEAD], gh=gh) == 2

    def test_truncated_list_reports_the_cap_not_a_moved_head(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # Past 250 commits the list ends before the head; the cause is the
        # cap, and saying "head moved" sends the operator after a push.
        status, out, _ = self._run(
            capsys,
            {"body": "", "commits": 251, "head": {"sha": HEAD}},
            [_commit("a" * 40, CLEAN)] * 250,
        )
        assert status == 2
        assert "251" in out and "250" in out
        assert "moved" not in out

    @pytest.mark.parametrize("pr", [[], "x", None])
    def test_malformed_response_is_not_evaluated_not_found(
        self, capsys: pytest.CaptureFixture[str], pr: Any
    ) -> None:
        # Exit 1 means attribution found, and the merge gate says so; a
        # crash on an unexpected shape must not borrow that meaning.
        status, out, _ = self._run(capsys, pr, [_commit(HEAD, CLEAN)])
        assert status == 2
        assert "not evaluated" in out

    def test_unexpected_exception_is_not_evaluated_not_found(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        def boom(path: str) -> Any:
            raise AttributeError("unexpected shape")

        status = cka.main(["--repo", "o/r", "--pr", "7", "--head-sha", HEAD], gh=boom)
        assert status == 2
        assert "not evaluated: AttributeError" in capsys.readouterr().out


class TestGuardWiring:
    """The check runs in self-review.yml's `guard`, the required check."""

    def test_guard_runs_the_script_with_the_event(self) -> None:
        import yaml

        root = Path(__file__).resolve().parents[3]
        workflow = yaml.safe_load(
            (root / ".github/workflows/self-review.yml").read_text(encoding="utf-8")
        )
        guard = workflow["jobs"]["guard"]
        step = next(s for s in guard["steps"] if s.get("id") == "ai-attribution")
        assert "check_ai_attribution.py" in step["run"]
        assert step["env"]["GH_TOKEN"] == "${{ github.token }}"
        assert guard["permissions"]["pull-requests"] == "read"


class TestGhApiPages:
    """`gh api --paginate --slurp` returns one element per page."""

    def _patch(self, monkeypatch: pytest.MonkeyPatch, pages: list[Any]) -> None:
        class Done:
            returncode = 0
            stdout = json.dumps(pages)

        monkeypatch.setattr(cka.subprocess, "run", lambda *a, **k: Done())

    def test_list_pages_are_concatenated(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._patch(monkeypatch, [[1, 2], [3]])
        assert cka._gh_api("p") == [1, 2, 3]

    def test_compare_object_pages_join_their_commits(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A compare range longer than one page comes back as one object per
        # page; the commits are joined, not refused as an API error.
        first = {"total_commits": 150, "commits": [_commit("a" * 40, CLEAN)] * 100}
        second = {"total_commits": 150, "commits": [_commit("b" * 40, CLEAN)] * 50}
        self._patch(monkeypatch, [first, second])
        merged = cka._gh_api("p")
        assert merged["total_commits"] == 150
        assert len(merged["commits"]) == 150

    def test_other_object_pages_are_refused(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._patch(monkeypatch, [{"a": 1}, {"a": 2}])
        with pytest.raises(cka.GhError):
            cka._gh_api("p")
