"""AT-2843: a major already dispositioned on a resolved thread stops counting.

The aggregate re-decides every round from that round's payloads, so a major
a human answered with Won't fix -- and a reviewer then raised again -- kept
the formal approval withheld forever (AT-2823). The aggregate now reads the
PR's review threads; a re-raised major that matches a resolved thread the
posting step opened as a major, and whose newest human reply opens with a
Resolve Policy disposition, is excluded from the withhold count, from the
headline's N and from the body's ``- + **major**`` lines, and is listed in
a section of its own.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from aggregate_reviews import (
    _DISPOSITION_PREFIXES,
    _approval_withhold,
    _count_unreviewed_majors,
    _dispositioned_majors,
    _fetch_disposition_threads,
    _parse_disposition_threads,
    _repository_writer,
    format_summary,
    main,
    REVIEWER_NAMES,
)
from github_pr_support import GH_TIMEOUT_SEC, inline_post_marker
from post_inline_comments import DEDUP_STRONG_JACCARD, is_duplicate, normalize_body
from tests_support import gate_function

_REVIEWER = next(iter(REVIEWER_NAMES))
_DESC = "Close the socket after the retry budget expires"
_BOT = {"login": "github-actions", "__typename": "Bot"}
_HUMAN = {"login": "hyuk-hur", "__typename": "User"}
_MAJOR_LINE = "- + **major**"


def _posted(severity: str = "major", description: str = _DESC) -> str:
    icon = {"major": "+", "minor": "-"}[severity]
    return (
        inline_post_marker(_REVIEWER, 1, "actions-1-1")
        + f"\n{icon} **{severity}** ({_REVIEWER}): {description}\n\n> ? do it"
    )


def _thread(
    *,
    resolved: bool = True,
    first_author: dict[str, str] = _BOT,
    first_body: str | None = None,
    replies: list[tuple[Any, ...]] | None = None,
    path: str = "a.py",
    line: int | None = 37,
    original_line: int | None = None,
) -> dict[str, Any]:
    """A raw GraphQL reviewThreads node in the shape the aggregate queries.

    A reply is (author, body) or (author, body, authorAssociation); the
    association defaults to MEMBER.
    """
    first = {
        "author": first_author,
        "authorAssociation": "NONE",
        "body": first_body or _posted(),
    }
    tail = [first] + [
        {
            "author": reply[0],
            "body": reply[1],
            "authorAssociation": reply[2] if len(reply) > 2 else "MEMBER",
        }
        for reply in (
            replies
            if replies is not None
            else [(_HUMAN, "Won't fix: below threshold. Intentional.")]
        )
    ]
    return {
        "isResolved": resolved,
        "path": path,
        "line": line,
        "originalLine": line if original_line is None else original_line,
        "head": {"nodes": [first]},
        "tail": {"nodes": tail},
    }


def _issue(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "severity": "major",
        "file": "a.py",
        # Re-raised after a small rebase shift: within the posting step's
        # line window of the thread at 37, so it dedups rather than reposts.
        "line": 40,
        "description": _DESC,
        "suggestion": None,
        "reviewer": _REVIEWER,
    }
    base.update(overrides)
    return base


def _available(*issues: dict[str, Any]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {
        n: {"summary": f"{n} review", "status": "ok", "issues": []}
        for n in REVIEWER_NAMES
    }
    out[_REVIEWER]["issues"] = list(issues)
    return out


def _dispositioned(
    available: dict[str, dict[str, Any]], *threads: dict[str, Any]
) -> dict[tuple[str, int], str]:
    return _dispositioned_majors(available, _parse_disposition_threads(list(threads)))


class TestWhatCountsAsDispositioned:
    def test_re_raised_dispositioned_major_is_not_counted(self) -> None:
        available = _available(_issue())
        dispositioned = _dispositioned(available, _thread())
        assert dispositioned == {(_REVIEWER, 0): "Won't fix by `hyuk-hur`"}
        assert _count_unreviewed_majors(available, dispositioned) == 0

    def test_undispositioned_major_is_still_counted(self) -> None:
        # Positive control: no thread at all.
        available = _available(_issue())
        assert _count_unreviewed_majors(available, _dispositioned(available)) == 1

    @pytest.mark.parametrize(
        "thread",
        [
            pytest.param(_thread(resolved=False), id="thread-not-resolved"),
            pytest.param(
                _thread(replies=[(_HUMAN, "Thanks, will look later")]),
                id="reply-carries-no-disposition",
            ),
            pytest.param(_thread(replies=[]), id="silent-resolve"),
            pytest.param(
                _thread(replies=[(_BOT, "Won't fix: below threshold.")]),
                id="reply-only-from-a-bot",
            ),
            pytest.param(
                _thread(
                    replies=[
                        (_HUMAN, "Won't fix: below threshold."),
                        (_HUMAN, "Reopening: this does break the retry path."),
                    ]
                ),
                id="newest-human-reply-withdraws-it",
            ),
            pytest.param(_thread(first_author=_HUMAN), id="thread-not-posted-by-bot"),
            pytest.param(
                _thread(first_body=_posted(severity="minor")),
                id="thread-was-a-minor",
            ),
            pytest.param(_thread(path="b.py"), id="other-file"),
            pytest.param(
                _thread(
                    first_body=_posted(
                        description="Validate credentials before forwarding them"
                    )
                ),
                id="other-finding-same-file",
            ),
            pytest.param(
                _thread(replies=[(_HUMAN, "Won't fix: by me.", "CONTRIBUTOR")]),
                id="reply-from-a-non-collaborator",
            ),
            pytest.param(
                _thread(replies=[(_HUMAN, "Won't fix: by me.", "NONE")]),
                id="reply-from-no-association",
            ),
            pytest.param(
                _thread(replies=[(None, "Won't fix: deleted account.")]),
                id="reply-from-a-deleted-account",
            ),
            pytest.param(
                # A re-raise after "Fixed:" is evidence the fix did not hold.
                _thread(replies=[(_HUMAN, "Fixed: closed in a.py:41")]),
                id="fixed-is-not-a-standing-disposition",
            ),
            pytest.param(
                # A Duplicate close points at a thread this does not read.
                _thread(replies=[(_HUMAN, "Handled in thread #3")]),
                id="duplicate-close-asserts-nothing-itself",
            ),
        ],
    )
    def test_major_stays_counted_without_a_narrow_disposition(
        self, thread: dict[str, Any]
    ) -> None:
        available = _available(_issue())
        dispositioned = _dispositioned(available, thread)
        assert dispositioned == {}
        assert _count_unreviewed_majors(available, dispositioned) == 1

    @pytest.mark.parametrize(
        "reply,prefix",
        [
            ("By design: the socket is pooled", "By design:"),
            ("Deferred to AT-1 (exists)", "Deferred to"),
            ("Won't fix: below threshold.", "Won't fix"),
        ],
    )
    def test_every_standing_disposition_disposes(self, reply: str, prefix: str) -> None:
        available = _available(_issue())
        assert _dispositioned(available, _thread(replies=[(_HUMAN, reply)])) == {
            (_REVIEWER, 0): f"{prefix.rstrip(':')} by `{_HUMAN['login']}`"
        }

    @pytest.mark.parametrize("association", ["OWNER", "MEMBER", "COLLABORATOR"])
    def test_a_repository_member_reply_disposes(self, association: str) -> None:
        available = _available(_issue())
        thread = _thread(replies=[(_HUMAN, "By design: pooled.", association)])
        assert _dispositioned(available, thread) == {
            (_REVIEWER, 0): f"By design by `{_HUMAN['login']}`"
        }

    def test_withhold_note_names_exactly_the_prefixes_matched(self) -> None:
        reason = _approval_withhold("approve", True, 1)
        assert reason is not None
        assert f"({', '.join(_DISPOSITION_PREFIXES)})" in reason.reason

    def test_a_re_raise_the_posting_step_would_repost_is_not_matched(
        self,
    ) -> None:
        # Out of the line window (40 vs 10) and, measured here rather than
        # asserted in prose, below the strong threshold: the thread body is
        # normalized whole and carries the producer's `> ? do it` line, so
        # the posting step opens a new thread, which is where it is answered.
        thread = _thread(line=10)
        parsed = _parse_disposition_threads([thread])
        issue_tokens = set(normalize_body(_DESC).split())
        thread_tokens = set(parsed[0]["body"].split())
        jaccard = len(issue_tokens & thread_tokens) / len(issue_tokens | thread_tokens)
        assert jaccard < DEDUP_STRONG_JACCARD
        assert not is_duplicate("a.py", _DESC, parsed, line=40)
        assert _dispositioned(_available(_issue()), thread) == {}

    def test_outdated_thread_scores_as_the_posting_step_does(self) -> None:
        # GitHub nulls `line` on an outdated thread; the posting step then
        # skips the window and dedups at the 0.6 threshold (0.70 here), so
        # it opens no new thread -- this must find the disposition there.
        available = _available(_issue())
        thread = _thread(line=None, original_line=10)
        assert _dispositioned(available, thread) == {
            (_REVIEWER, 0): "Won't fix by `hyuk-hur`"
        }

    def test_pathless_major_has_no_thread_channel(self) -> None:
        available = _available(_issue(file=None, line=None))
        assert _dispositioned(available, _thread()) == {}


class TestVerifiedWriterDispositions:
    @pytest.mark.parametrize(
        "permission,role_name", [("admin", "admin"), ("write", "maintain")]
    )
    def test_masked_writer_disposes_a_real_major(
        self, permission: str, role_name: str
    ) -> None:
        thread = _thread(replies=[(_HUMAN, "By design: pooled.", "CONTRIBUTOR")])
        result = subprocess.CompletedProcess(
            [],
            0,
            json.dumps(
                {
                    "permission": permission,
                    "role_name": role_name,
                    "user": {"login": _HUMAN["login"].upper()},
                }
            ),
        )
        with patch("aggregate_reviews.subprocess.run", return_value=result) as run:
            parsed = _parse_disposition_threads([thread], repo="owner/repo")
        assert _dispositioned_majors(_available(_issue()), parsed) == {
            (_REVIEWER, 0): "By design by `hyuk-hur`"
        }
        run.assert_called_once_with(
            ["gh", "api", "repos/owner/repo/collaborators/hyuk-hur/permission"],
            capture_output=True,
            text=True,
            timeout=GH_TIMEOUT_SEC,
        )

    @pytest.mark.parametrize(
        "response",
        [
            {"permission": "read", "role_name": "admin"},
            {"permission": "none"},
            {"permission": "maintain"},
            {"permission": "push"},
            {"permission": "custom-writer"},
            {"permission": None},
            {"permission": ["admin"]},
            {"role_name": "admin"},
            {"permission": "admin", "user": {"login": "someone-else"}},
            {"permission": "admin", "user": {}},
            {"permission": "admin", "user": None},
            {"permission": "admin", "user": {"login": 123}},
            [],
            None,
        ],
    )
    def test_unverified_access_withholds(self, response: Any) -> None:
        if isinstance(response, dict):
            response = {"user": {"login": _HUMAN["login"]}, **response}
        thread = _thread(replies=[(_HUMAN, "Won't fix: intentional.", "CONTRIBUTOR")])
        result = subprocess.CompletedProcess([], 0, json.dumps(response))
        with patch("aggregate_reviews.subprocess.run", return_value=result):
            parsed = _parse_disposition_threads([thread], repo="owner/repo")
        assert (
            _count_unreviewed_majors(
                _available(_issue()),
                _dispositioned_majors(_available(_issue()), parsed),
            )
            == 1
        )

    @pytest.mark.parametrize("failure", ["403", "404", "429", "timeout", "os", "json"])
    def test_lookup_failure_withholds(self, failure: str) -> None:
        result = subprocess.CompletedProcess([], 1, "", f"HTTP {failure}")
        error = None
        if failure == "timeout":
            error = subprocess.TimeoutExpired("gh", GH_TIMEOUT_SEC)
        elif failure == "os":
            error = OSError("gh unavailable")
        elif failure == "json":
            result = subprocess.CompletedProcess([], 0, "not JSON")
        with patch(
            "aggregate_reviews.subprocess.run", return_value=result, side_effect=error
        ):
            assert not _repository_writer("owner/repo", _HUMAN["login"])

    @pytest.mark.parametrize(
        "repo,login",
        [
            ("owner", "writer"),
            ("owner/repo/other", "writer"),
            ("owner/..", "writer"),
            ("owner/.", "writer"),
            ("owner/repo?query", "writer"),
            ("owner/repo", "../writer"),
            ("owner/repo", "writer?query"),
            ("owner/repo", "--hostname=other"),
            ("owner/repo", "$(command)"),
            ("owner/repo", ""),
        ],
    )
    def test_invalid_endpoint_components_are_never_requested(
        self, repo: str, login: str
    ) -> None:
        with patch("aggregate_reviews.subprocess.run") as run:
            assert not _repository_writer(repo, login)
        run.assert_not_called()

    @pytest.mark.parametrize("association", ["OWNER", "MEMBER", "COLLABORATOR"])
    def test_existing_associations_need_no_permission_lookup(
        self, association: str
    ) -> None:
        thread = _thread(replies=[(_HUMAN, "By design: pooled.", association)])
        with patch("aggregate_reviews.subprocess.run") as run:
            assert _parse_disposition_threads([thread], repo="owner/repo")
        run.assert_not_called()

    @pytest.mark.parametrize(
        "thread",
        [
            _thread(resolved=False),
            _thread(first_author=_HUMAN),
            _thread(first_body=_posted(severity="minor")),
            _thread(replies=[]),
            _thread(replies=[(_BOT, "By design: pooled.")]),
            _thread(replies=[(None, "By design: deleted.")]),
            _thread(replies=[(_HUMAN, "Fixed: pooled.")]),
            _thread(
                replies=[
                    (_HUMAN, "By design: pooled."),
                    (_HUMAN, "Reopening: still broken."),
                ]
            ),
        ],
    )
    def test_other_thread_gates_run_before_permission_lookup(
        self, thread: dict[str, Any]
    ) -> None:
        for reply in thread["tail"]["nodes"]:
            reply["authorAssociation"] = "CONTRIBUTOR"
        with patch("aggregate_reviews.subprocess.run") as run:
            assert _parse_disposition_threads([thread], repo="owner/repo") == []
        run.assert_not_called()

    @pytest.mark.parametrize("allowed", [True, False])
    def test_cache_is_scoped_to_repository_and_login(self, allowed: bool) -> None:
        cache: dict[tuple[str, str], bool] = {}
        thread = _thread(replies=[(_HUMAN, "By design: pooled.", "CONTRIBUTOR")])
        other = _thread(
            replies=[({"login": "other", "__typename": "User"}, "By design:", "NONE")]
        )
        with patch(
            "aggregate_reviews._repository_writer", return_value=allowed
        ) as check:
            for repo in ("owner/repo", "owner/other"):
                parsed = _parse_disposition_threads(
                    [thread, thread, other], repo=repo, permission_cache=cache
                )
                assert len(parsed) == (3 if allowed else 0)
        assert check.call_count == 4
        assert {call.args for call in check.call_args_list} == {
            (repo, login)
            for repo in ("owner/repo", "owner/other")
            for login in (_HUMAN["login"], "other")
        }

    def test_fetch_caches_across_pages_but_not_across_aggregates(self) -> None:
        thread = _thread(replies=[(_HUMAN, "By design: pooled.", "CONTRIBUTOR")])

        def pages(*args: Any, **kwargs: Any) -> list[dict[str, Any]]:
            transform = kwargs["transform"]
            return transform([thread]) + transform([thread])

        with (
            patch("aggregate_reviews.fetch_paginated_nodes", side_effect=pages),
            patch(
                "aggregate_reviews._repository_writer", side_effect=[True, False]
            ) as check,
        ):
            assert len(_fetch_disposition_threads("owner/repo", "42")) == 2
            assert _fetch_disposition_threads("owner/repo", "42") == []
        assert check.call_count == 2


class TestOneCountEverywhere:
    """Headline N == body ``- + **major**`` lines == withhold count."""

    def _summary(self) -> tuple[str, int]:
        available = _available(
            _issue(suggestion="Pool the socket"),
            _issue(file="b.py", line=3, description="Validate input lengths first"),
        )
        dispositioned = _dispositioned(available, _thread())
        n = _count_unreviewed_majors(available, dispositioned)
        body = format_summary(
            available,
            "approve",
            "3/3 LLM responses -- 2 major issue(s) (no consensus, review recommended)",
            available,
            {name: "success" for name in REVIEWER_NAMES},
            comment_only=True,
            approve_quorum=True,
            unreviewed_majors=n,
            dispositioned=dispositioned,
        )
        return body, n

    def test_counts_agree(self) -> None:
        body, n = self._summary()
        assert n == 1
        assert "Approved with 1 unreviewed major issue(s)" in body
        assert [ln for ln in body.splitlines() if ln.startswith(_MAJOR_LINE)] == [
            "- + **major** `b.py:3` -- Validate input lengths first"
        ]

    def test_dispositioned_major_is_listed_separately(self) -> None:
        body, _ = self._summary()
        section = body.split("### Dispositioned in an earlier round", 1)
        assert len(section) == 2, body
        # The note sits between the item and its suggestion: after the
        # `> ?` line it would render as part of that quote.
        assert (
            f"- = **major** `a.py:40` -- {_DESC}\n"
            f"  {_REVIEWER}: thread answered Won't fix by `hyuk-hur`, and resolved\n"
            "  > ? Pool the socket"
        ) in section[1]

    def test_body_passes_the_merge_gate(self) -> None:
        # The pr-response-cycle gate (c), run as the drill extracts it:
        # headline_roster reads N, major_dispositions counts the body's
        # major lines against it and needs each answered. The one
        # undispositioned major is answered by a quote; the dispositioned
        # one must not be a line the gate counts, or n != want blocks.
        body, _ = self._summary()
        headline = next(ln for ln in body.splitlines() if ln.startswith("**Result:"))
        functions = "\n".join(
            gate_function(name)
            for name in (
                "headline_roster",
                "finding_key",
                "thread_answers",
                "major_dispositions",
            )
        )
        quote = "> **major** `b.py:3` -- Validate input lengths first"
        result = subprocess.run(
            [
                "bash",
                "-c",
                functions
                + '\nset -o pipefail\nheadline_roster "$1" || exit 7'
                + '\nmajor_dispositions "$2" "$agg_majors" "" "$3"',
                "bash",
                headline,
                body,
                quote,
            ],
            env={
                "EXPECTED_REVIEWERS": str(len(REVIEWER_NAMES)),
                "PATH": "/usr/bin:/bin",
            },
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert result.returncode == 0, result.stdout + result.stderr


class TestMainReadsDispositions:
    def test_main_hands_the_dispositioned_count_to_both_readers(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("PR_NUMBER", "42")
        monkeypatch.setenv("GITHUB_REPOSITORY", "owner/repo")
        for name in REVIEWER_NAMES:
            monkeypatch.setenv(f"REVIEWER_RESULT_{name.upper()}", "success")
        available = _available(
            _issue(),
            _issue(file="b.py", line=3, description="Validate input lengths first"),
        )
        with (
            patch("aggregate_reviews.load_reviews", return_value=available),
            patch(
                "aggregate_reviews.format_summary", return_value="body"
            ) as mock_summary,
            patch("aggregate_reviews.post_verdict") as mock_post,
            patch("aggregate_reviews.fetch_round_count", return_value=0),
            patch("aggregate_reviews._fetch_fold_comments", return_value=[]),
            patch("aggregate_reviews._fetch_inline_posts", return_value=[]),
            patch(
                "aggregate_reviews._fetch_disposition_threads",
                return_value=_parse_disposition_threads([_thread()]),
            ) as mock_threads,
        ):
            main()

        mock_threads.assert_called_once_with("owner/repo", "42")
        assert mock_post.call_args.kwargs["unreviewed_majors"] == 1
        assert mock_summary.call_args.kwargs["unreviewed_majors"] == 1
        assert mock_summary.call_args.kwargs["dispositioned"] == {
            (_REVIEWER, 0): "Won't fix by `hyuk-hur`"
        }
        # The rules count only the live major; the reason names the other.
        reason = mock_summary.call_args.args[2]
        assert "-- 1 major issue(s) (no consensus" in reason
        assert reason.endswith(
            "-- plus 1 major issue(s) dispositioned in an earlier round"
        )

    def _verdict(
        self, monkeypatch: pytest.MonkeyPatch, threads: list[dict[str, Any]]
    ) -> str:
        """main()'s posted verdict when two reviewers raise the same major."""
        monkeypatch.setenv("PR_NUMBER", "42")
        monkeypatch.setenv("GITHUB_REPOSITORY", "owner/repo")
        for name in REVIEWER_NAMES:
            monkeypatch.setenv(f"REVIEWER_RESULT_{name.upper()}", "success")
        reviews = _available(_issue())
        other = [n for n in REVIEWER_NAMES if n != _REVIEWER][0]
        reviews[other]["issues"] = [_issue(reviewer=other)]
        with (
            patch("aggregate_reviews.load_reviews", return_value=reviews),
            patch("aggregate_reviews.format_summary", return_value="body") as summary,
            patch("aggregate_reviews.post_verdict") as mock_post,
            patch("aggregate_reviews.fetch_round_count", return_value=0),
            patch("aggregate_reviews._fetch_fold_comments", return_value=[]),
            patch("aggregate_reviews._fetch_inline_posts", return_value=[]),
            patch(
                "aggregate_reviews._fetch_disposition_threads",
                return_value=_parse_disposition_threads(threads),
            ),
        ):
            main()
        self.reason = summary.call_args.args[2]
        return mock_post.call_args.args[1]

    def test_consensus_on_a_dispositioned_major_does_not_request_changes(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Leader's extension of the 2026-10-27 decision: a dispositioned
        # major counts nowhere, the consensus rule included.
        assert self._verdict(monkeypatch, [_thread()]) == "approve"
        # Nothing standing, two majors set aside: the reason says both.
        headline = self.reason
        assert "-- no issues standing -- plus 2 major issue(s)" in headline
        assert "-- no issues --" not in headline

    def test_consensus_on_an_undispositioned_major_still_requests_changes(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        assert self._verdict(monkeypatch, [_thread(resolved=False)]) == (
            "request_changes"
        )

    def test_round_without_majors_reads_no_threads(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("PR_NUMBER", "42")
        monkeypatch.setenv("GITHUB_REPOSITORY", "owner/repo")
        for name in REVIEWER_NAMES:
            monkeypatch.setenv(f"REVIEWER_RESULT_{name.upper()}", "success")
        with (
            patch("aggregate_reviews.load_reviews", return_value=_available()),
            patch("aggregate_reviews.format_summary", return_value="body"),
            patch("aggregate_reviews.post_verdict"),
            patch("aggregate_reviews._fetch_disposition_threads") as mock_threads,
        ):
            main()
        mock_threads.assert_not_called()


def test_aggregate_reads_the_posting_steps_jaccard_threshold() -> None:
    # The matcher's threshold binds from the environment at import; the
    # posting job sets it from vars, so the aggregate job must too, or a
    # consumer that tightens it leaves the aggregate matching looser.
    workflows = Path(__file__).resolve().parents[2] / "workflows"
    line = "JACCARD_THRESHOLD: ${{ vars.JACCARD_THRESHOLD || '0.6' }}"
    for name in ("base-ai-review-single.yml", "base-ai-review-aggregate.yml"):
        assert line in (workflows / name).read_text(encoding="utf-8"), name


@pytest.mark.parametrize("name", ["JACCARD_THRESHOLD", "DEDUP_STRONG_JACCARD"])
def test_a_malformed_threshold_does_not_stop_the_aggregate_importing(
    name: str,
) -> None:
    # The aggregate now imports the posting step's matcher, which reads
    # these at import: a consumer var of "0,6" must fall back with a
    # warning, not raise before main() can post any verdict.
    scripts = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        [sys.executable, "-c", "import aggregate_reviews"],
        cwd=scripts,
        env={**os.environ, name: "0,6"},
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    assert f"Invalid {name}='0,6'" in result.stderr
