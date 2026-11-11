"""AT-3428: a major with no thread is dispositioned by a quoted reply.

A major the posting step folds into an inline-fallback or round-cutoff
comment has no review thread, and neither has a major with no file or no
line, so the thread route (AT-2843) could never clear it and the approval
was withheld for good. The pr-response-cycle skill already answers such an
item with a quoted-response issue comment (Stage 4; worked example
ignite-corp/ai-dev-pr-review#188, comment 6015452260). These cases hold
the aggregate to that contract, under the thread route's trust rules: the
author, the source binding, the quote, the prefix, the newest reply, and a
read that fails.
"""

from __future__ import annotations

import json
import os
import subprocess
from types import SimpleNamespace
from typing import Any

import pytest

import aggregate_reviews as aggregate
import post_inline_comments as posting
from aggregate_reviews import (
    REVIEW_MARKER,
    _dispositioned_majors,
    _fetch_quoted_dispositions,
    _format_issue_line,
    _parse_holding_threads,
    _parse_quoted_dispositions,
    _QuotedDisposition,
    format_summary,
)
from github_pr_support import inline_post_marker
from tests_support import run_folded_gate

_REAL_RUN = subprocess.run
_BOT = "github-actions[bot]"
_REVIEWER = "claude"
_DESC = (
    "The stale-approval rule is missing from item 7, so a post-approval"
    " rebase can merge content the human never approved"
)
_OTHER = "Close the socket after the retry budget expires on every path"
_MARKER = f"inline-fallback-{_REVIEWER}-r6"
_FOLD_AT = "2026-11-11T07:45:01Z"
_VERDICT_AT = "2026-11-11T07:45:23Z"


def _issue(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "severity": "major",
        "file": "claude/AGENTS.md",
        "line": 386,
        "description": _DESC,
        "suggestion": "Restore the rule.",
    }
    base.update(overrides)
    return base


def _available(*issues: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {_REVIEWER: {"summary": "s", "status": "ok", "issues": list(issues)}}


def _fold_body(*issues: dict[str, Any], round_number: int = 6) -> str:
    """The producer's own fallback comment for ``issues``."""
    valid: dict[str, set[int]] = {}
    for issue in issues:
        valid.setdefault(issue["file"], set()).add(issue["line"])
    comments, _, _ = posting.build_comments(list(issues), valid, [], _REVIEWER)
    captured: list[str] = []

    def transport(cmd: list[str], **kwargs: Any) -> SimpleNamespace:
        if cmd[:2] == ["gh", "api"]:
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        captured.append(kwargs["input"])
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    original = posting.subprocess.run
    posting.subprocess.run = transport  # type: ignore[assignment]
    try:
        posting.post_fallback(
            "o/r", "7", _REVIEWER, round_number, comments, head="a" * 40
        )
    finally:
        posting.subprocess.run = original
    return captured[0]


def _fold_line(body: str) -> str:
    return next(line for line in body.split("\n") if line.startswith("- **"))


def _comment(
    body: str,
    *,
    at: str,
    login: str = "hyuk-hur",
    author_type: str = "User",
    association: str = "MEMBER",
) -> dict[str, Any]:
    return {
        "login": login,
        "author_type": author_type,
        "association": association,
        "created_at": at,
        "body": body,
    }


def _fold(issue: dict[str, Any] | None = None, **overrides: Any) -> dict[str, Any]:
    fold = _comment(
        _fold_body(issue or _issue()),
        at=_FOLD_AT,
        login=_BOT,
        author_type="Bot",
        association="NONE",
    )
    fold.update(overrides)
    return fold


def _reply(
    quote: str,
    answer: str = "Deferred to #256 (created 2026-11-11): out of scope.",
    *,
    marker: str = _MARKER,
    at: str = "2026-11-11T07:46:30Z",
    **kwargs: Any,
) -> dict[str, Any]:
    head = f"Dispositions for `{marker}`:\n\n" if marker else "Dispositions:\n\n"
    return _comment(f"{head}> {quote}\n{answer}\n", at=at, **kwargs)


def _verdict(*issues: dict[str, Any], login: str = _BOT) -> dict[str, Any]:
    body = "\n".join(
        [REVIEW_MARKER, "## [bot] Multi-LLM Review Summary", ""]
        + [_format_issue_line(i) for i in issues]
    )
    return _comment(
        body, at=_VERDICT_AT, login=login, author_type="Bot", association="NONE"
    )


def _decide(
    comments: list[dict[str, Any]],
    *issues: dict[str, Any],
    verdicts: list[dict[str, Any]] | None = None,
) -> dict[tuple[str, int], str]:
    quoted = _parse_quoted_dispositions(
        comments, verdicts if verdicts is not None else comments, repo="o/r"
    )
    return _dispositioned_majors(_available(*(issues or (_issue(),))), [], quoted)


@pytest.fixture(autouse=True)
def _identities(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(aggregate, "INLINE_BOT_LOGIN", _BOT)
    monkeypatch.setattr(aggregate, "BOT_LOGIN", _BOT)
    monkeypatch.delenv("INLINE_REVIEW_RUN_ID", raising=False)
    # A writer lookup must be stubbed by the case that needs one.
    monkeypatch.setattr(aggregate, "_repository_writer", lambda *a: False)


class TestFoldedMajor:
    def test_trusted_quote_reply_naming_the_marker_dispositions_it(self) -> None:
        fold = _fold()
        out = _decide([fold, _reply(_fold_line(fold["body"]))])
        assert out == {(_REVIEWER, 0): "Deferred to by `hyuk-hur`"}
        assert isinstance(out[(_REVIEWER, 0)], _QuotedDisposition)

    @pytest.mark.parametrize(
        "answer",
        [
            "By design: the rule moved to #256.",
            "Won't fix -- below threshold.",
            "Won't fix: below threshold",
        ],
    )
    def test_every_standing_prefix_disposes(self, answer: str) -> None:
        fold = _fold()
        assert _decide([fold, _reply(_fold_line(fold["body"]), answer)])

    def test_blank_line_between_quote_and_answer_is_the_188_shape(self) -> None:
        fold = _fold()
        reply = _comment(
            f"Answers `{_MARKER}`.\n\n> {_fold_line(fold['body'])}\n\n"
            "By design: kept.\n",
            at="2026-11-11T07:46:30Z",
        )
        assert _decide([fold, reply])

    def test_quote_in_the_verdict_rendering_also_answers_the_fold(self) -> None:
        # The skill: either rendering can answer both surfaces.
        fold = _fold()
        quote = _format_issue_line({**_issue(), "suggestion": None})
        assert _decide([fold, _reply(quote)])

    def test_reply_passes_the_skills_own_cutoff_gate(self) -> None:
        # The same comment the aggregate accepts answers the skill's gate.
        fold = _fold()
        reply = _reply(_fold_line(fold["body"]))
        assert _decide([fold, reply])
        result = run_folded_gate(fold["body"], reply["body"])
        assert result.returncode == 0, result.stdout + result.stderr


class TestTrust:
    @pytest.mark.parametrize("association", ["OWNER", "MEMBER", "COLLABORATOR"])
    def test_trusted_associations(self, association: str) -> None:
        fold = _fold()
        reply = _reply(_fold_line(fold["body"]), association=association)
        assert _decide([fold, reply])

    @pytest.mark.parametrize("association", ["CONTRIBUTOR", "NONE", ""])
    def test_untrusted_author_does_not_dispose(self, association: str) -> None:
        fold = _fold()
        reply = _reply(_fold_line(fold["body"]), association=association)
        assert _decide([fold, reply]) == {}

    def test_verified_writer_with_hidden_association_disposes(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls: list[tuple[str, str]] = []

        def writer(repo: str, login: str) -> bool:
            calls.append((repo, login))
            return True

        monkeypatch.setattr(aggregate, "_repository_writer", writer)
        fold = _fold()
        reply = _reply(_fold_line(fold["body"]), association="NONE")
        assert _decide([fold, reply])
        assert calls == [("o/r", "hyuk-hur")]

    @pytest.mark.parametrize("author_type", ["Bot", "Organization", ""])
    def test_non_person_reply_does_not_dispose(self, author_type: str) -> None:
        fold = _fold()
        reply = _reply(_fold_line(fold["body"]), author_type=author_type)
        assert _decide([fold, reply]) == {}

    def test_fold_comment_not_by_the_posting_identity_binds_nothing(self) -> None:
        # Anyone can post a marker line; only INLINE_BOT_LOGIN's is a source.
        planted = _fold(login="hyuk-hur", author_type="User", association="MEMBER")
        assert _decide([planted, _reply(_fold_line(planted["body"]))]) == {}

    @pytest.mark.parametrize("surface", ["fold", "verdict"])
    def test_a_producer_comment_by_a_person_is_not_a_reply(
        self, monkeypatch: pytest.MonkeyPatch, surface: str
    ) -> None:
        # Under the local driver the producer's comments are the operator's
        # own (User, OWNER). A reviewer's suggestion is rendered after `> `
        # and may carry newlines, so a crafted one can lay out a quote of an
        # earlier fold item followed by a prefixed line. That is LLM text,
        # not a person's answer.
        monkeypatch.setattr(aggregate, "INLINE_BOT_LOGIN", "hyuk-hur")
        monkeypatch.setattr(aggregate, "BOT_LOGIN", "hyuk-hur")
        monkeypatch.setenv("INLINE_REVIEW_RUN_ID", "local-" + "b" * 32)
        source = _fold(login="hyuk-hur", author_type="User", association="OWNER")
        crafted = _issue(
            line=390,
            description=_OTHER,
            suggestion=f"see {_MARKER}\n> {_fold_line(source['body'])}\nBy design: x",
        )
        if surface == "fold":
            body = _fold_body(crafted, round_number=7)
        else:
            body = "\n".join([REVIEW_MARKER, "", _format_issue_line(crafted)])
        later = _comment(
            body, at="2026-11-11T08:00:00Z", login="hyuk-hur", association="OWNER"
        )
        # Control: the same text from a person's ordinary comment disposes.
        ordinary = {**later, "body": "note\n" + body}
        assert _decide([source, ordinary])
        assert _decide([source, later]) == {}


class TestBinding:
    def test_reply_that_does_not_name_the_marker_does_not_dispose(self) -> None:
        fold = _fold()
        assert _decide([fold, _reply(_fold_line(fold["body"]), marker="")]) == {}

    def test_a_longer_marker_is_not_this_one(self) -> None:
        fold = _fold()
        reply = _reply(_fold_line(fold["body"]), marker=_MARKER + "0")
        assert _decide([fold, reply]) == {}

    def test_reply_older_than_the_fold_comment_does_not_dispose(self) -> None:
        fold = _fold()
        reply = _reply(_fold_line(fold["body"]), at="2026-11-11T07:00:00Z")
        assert _decide([fold, reply]) == {}

    def test_quote_of_an_item_the_named_fold_does_not_list(self) -> None:
        fold = _fold(_issue(description=_OTHER))
        reply = _reply(_fold_line(_fold_body(_issue())))
        assert _decide([fold, reply]) == {}

    def test_quote_mismatch_does_not_dispose(self) -> None:
        fold = _fold(_issue(description=_OTHER))
        reply = _reply(_fold_line(fold["body"]))
        # The fold and the quote are _OTHER; this round's major is _DESC.
        assert _decide([fold, reply], _issue()) == {}

    def test_quote_on_another_path_does_not_dispose(self) -> None:
        fold = _fold(_issue(file="b.md"))
        reply = _reply(_fold_line(fold["body"]))
        assert _decide([fold, reply], _issue()) == {}

    def test_quote_of_a_minor_does_not_dispose_a_major(self) -> None:
        fold = _fold(_issue(severity="minor"))
        reply = _reply(_fold_line(fold["body"]))
        assert _decide([fold, reply]) == {}

    def test_elided_rewrite_is_below_the_strong_threshold(self) -> None:
        # dev-dotfiles#254's reply rewrote the quote with ellipses; the
        # strong threshold refuses a rewrite that drops this much.
        fold = _fold()
        quote = (
            "**claude/AGENTS.md:386** -- + **major** (claude): The stale-approval"
            " rule is missing ..."
        )
        assert _decide([fold, _reply(quote)]) == {}


class TestPrefix:
    @pytest.mark.parametrize(
        "answer",
        ["Fixed: restored.", "Acknowledged.", "Thanks", "Handled in thread #3", ""],
    )
    def test_answer_without_a_standing_prefix_does_not_dispose(
        self, answer: str
    ) -> None:
        fold = _fold()
        assert _decide([fold, _reply(_fold_line(fold["body"]), answer)]) == {}

    @pytest.mark.parametrize("indent", ["", "  "], ids=["flush", "indented"])
    def test_an_answer_belongs_to_its_own_quote_only(self, indent: str) -> None:
        # Round 4 (codex): A quoted, then B quoted and answered. A has no
        # answer of its own, and B's must not reach back to it.
        a, b = _issue(), _issue(line=390, description=_OTHER)
        fold = _comment(
            _fold_body(a, b),
            at=_FOLD_AT,
            login=_BOT,
            author_type="Bot",
            association="NONE",
        )
        line_a, line_b = (
            line for line in fold["body"].split("\n") if line.startswith("- **")
        )
        reply = _comment(
            f"`{_MARKER}`:\n\n{indent}> {line_a}\n\n{indent}> {line_b}\n"
            "By design: only B.\n",
            at="2026-11-11T07:46:30Z",
        )
        assert _decide([fold, reply], a, b) == {
            (_REVIEWER, 1): "By design by `hyuk-hur`"
        }

    def test_a_later_reply_with_an_indented_quote_withdraws(self) -> None:
        # Round 4 (claude): an indented quote is a quote, not the answer to
        # the quote above it.
        fold = _fold()
        line = _fold_line(fold["body"])
        later = _comment(
            f"`{_MARKER}`:\n\n  > {line}\nReopened.\n", at="2026-11-11T08:00:00Z"
        )
        assert _decide([fold, _reply(line), later]) == {}

    def test_prefix_elsewhere_in_the_comment_does_not_count(self) -> None:
        fold = _fold()
        reply = _comment(
            f"By design: see below, `{_MARKER}`.\n\n> {_fold_line(fold['body'])}\n"
            "Will look later.\n",
            at="2026-11-11T07:46:30Z",
        )
        assert _decide([fold, reply]) == {}


class TestNewestWins:
    def test_later_reply_without_prefix_withdraws(self) -> None:
        fold = _fold()
        line = _fold_line(fold["body"])
        first = _reply(line)
        later = _reply(line, "Reopened: fix it here.", at="2026-11-11T08:00:00Z")
        assert _decide([fold, first, later]) == {}

    def test_later_untrusted_reply_withdraws(self) -> None:
        fold = _fold()
        line = _fold_line(fold["body"])
        later = _reply(
            line, "By design: x", at="2026-11-11T08:00:00Z", association="NONE"
        )
        assert _decide([fold, _reply(line), later]) == {}

    def test_later_trusted_reply_restores(self) -> None:
        fold = _fold()
        line = _fold_line(fold["body"])
        withdrawn = _reply(line, "Reopened.", at="2026-11-11T08:00:00Z")
        again = _reply(line, "Won't fix: settled", at="2026-11-11T09:00:00Z")
        assert _decide([fold, _reply(line), withdrawn, again]) == {
            (_REVIEWER, 0): "Won't fix by `hyuk-hur`"
        }

    def test_a_reply_about_another_major_does_not_withdraw(self) -> None:
        fold = _comment(
            _fold_body(_issue(), _issue(line=390, description=_OTHER)),
            at=_FOLD_AT,
            login=_BOT,
            author_type="Bot",
            association="NONE",
        )
        lines = [line for line in fold["body"].split("\n") if line.startswith("- **")]
        out = _decide(
            [
                fold,
                _reply(lines[0]),
                _reply(lines[1], "Reopened.", at="2026-11-11T08:00:00Z"),
            ],
            _issue(),
            _issue(line=390, description=_OTHER),
        )
        assert out == {(_REVIEWER, 0): "Deferred to by `hyuk-hur`"}


class TestLinelessMajor:
    @pytest.mark.parametrize(
        "issue",
        [_issue(file=None, line=None), _issue(line=None)],
        ids=["no-file", "no-line"],
    )
    def test_quote_of_the_verdict_line_disposes(self, issue: dict[str, Any]) -> None:
        verdict = _verdict(issue)
        quote = _format_issue_line({**issue, "suggestion": None})
        reply = _reply(quote, "By design: intended.", marker="")
        assert _decide([verdict, reply], issue) == {
            (_REVIEWER, 0): "By design by `hyuk-hur`"
        }

    def test_reply_older_than_the_verdict_does_not_dispose(self) -> None:
        issue = _issue(file=None, line=None)
        quote = _format_issue_line({**issue, "suggestion": None})
        reply = _reply(quote, "By design: x", marker="", at="2026-11-11T07:00:00Z")
        assert _decide([_verdict(issue), reply], issue) == {}

    def test_verdict_by_another_identity_binds_nothing(self) -> None:
        issue = _issue(file=None, line=None)
        quote = _format_issue_line({**issue, "suggestion": None})
        reply = _reply(quote, "By design: x", marker="")
        planted = _verdict(issue, login="someone-else[bot]")
        assert _decide([planted, reply], issue) == {}

    def test_verdict_route_does_not_reach_a_major_with_a_line(self) -> None:
        # A major with a line has a thread or a fold comment, and the
        # verdict quote names no marker.
        issue = _issue()
        quote = _format_issue_line({**issue, "suggestion": None})
        reply = _reply(quote, "By design: x", marker="")
        assert _decide([_verdict(issue), reply], issue) == {}

    def test_verdict_bound_reply_does_not_reach_the_finding_re_raised_with_a_line(
        self,
    ) -> None:
        # Once the finding has a line it has a thread or a fold comment, and
        # the reply that names neither does not answer it.
        lineless = _issue(line=None)
        quote = _format_issue_line({**lineless, "suggestion": None})
        reply = _reply(quote, "By design: x", marker="")
        assert _decide([_verdict(lineless), reply], lineless)
        assert _decide([_verdict(lineless), reply], _issue()) == {}

    def test_fold_bound_reply_reaches_the_finding_re_raised_without_a_line(
        self,
    ) -> None:
        fold = _fold()
        assert _decide([fold, _reply(_fold_line(fold["body"]))], _issue(line=None))

    def test_file_less_major_is_not_answered_by_a_located_quote(self) -> None:
        issue = _issue(file=None, line=None)
        fold = _fold()
        assert (
            _decide([fold, _verdict(issue), _reply(_fold_line(fold["body"]))], issue)
            == {}
        )


class TestRead:
    def _transport(self, monkeypatch: pytest.MonkeyPatch, rows: dict[str, Any]):
        calls: list[str] = []

        def run(cmd: list[str], **kwargs: Any) -> Any:
            endpoint = cmd[3]
            calls.append(endpoint)
            kind = "/".join(endpoint.split("/")[3::2])  # issues/comments ...
            payload = rows.get(kind, [])
            if isinstance(payload, SimpleNamespace):
                return payload
            return _REAL_RUN(
                # gh's --jq prints strings raw, as jq -r does.
                ["jq", "-r", cmd[cmd.index("--jq") + 1]],
                input=json.dumps(payload),
                text=True,
                capture_output=True,
                env=os.environ,
            )

        monkeypatch.setattr(aggregate.subprocess, "run", run)
        return calls

    @staticmethod
    def _rest(comment: dict[str, Any], *, review: bool = False) -> dict[str, Any]:
        return {
            "user": {"login": comment["login"], "type": comment["author_type"]},
            "author_association": comment["association"],
            ("submitted_at" if review else "created_at"): comment["created_at"],
            "body": comment["body"],
        }

    def test_reads_both_listings_through_the_real_jq_filter(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fold = _fold()
        reply = _reply(_fold_line(fold["body"]))
        calls = self._transport(
            monkeypatch,
            {
                "issues/comments": [self._rest(fold), self._rest(reply)],
                "pulls/reviews": [],
            },
        )
        entries = _fetch_quoted_dispositions("o/r", "254")
        assert calls == [
            "repos/o/r/issues/254/comments",
            "repos/o/r/pulls/254/reviews",
            "repos/o/r/pulls/254/comments",
        ]
        assert _dispositioned_majors(_available(_issue()), [], entries)

    def test_lineless_major_binds_to_a_verdict_review(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        issue = _issue(file=None, line=None)
        verdict = _verdict(issue)
        reply = _reply(
            _format_issue_line({**issue, "suggestion": None}), "By design: x", marker=""
        )
        self._transport(
            monkeypatch,
            {
                "issues/comments": [self._rest(reply)],
                "pulls/reviews": [self._rest(verdict, review=True)],
            },
        )
        entries = _fetch_quoted_dispositions("o/r", "254")
        assert _dispositioned_majors(_available(issue), [], entries)

    @pytest.mark.parametrize(
        "failing", ["issues/comments", "pulls/reviews", "pulls/comments"]
    )
    def test_a_failed_listing_withholds(
        self, monkeypatch: pytest.MonkeyPatch, failing: str
    ) -> None:
        fold = _fold()
        reply = _reply(_fold_line(fold["body"]))
        rows: dict[str, Any] = {
            "issues/comments": [self._rest(fold), self._rest(reply)],
            "pulls/reviews": [],
        }
        rows[failing] = SimpleNamespace(returncode=1, stdout="", stderr="HTTP 502")
        self._transport(monkeypatch, rows)
        assert _fetch_quoted_dispositions("o/r", "254") == []

    def test_a_pending_review_does_not_void_the_read(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # The local driver's token lists the operator's own pending review,
        # which has no submitted_at. It is no verdict, and it must not
        # refuse every quoted disposition on the PR.
        fold = _fold()
        reply = _reply(_fold_line(fold["body"]))
        pending = {
            "user": {"login": "hyuk-hur", "type": "User"},
            "author_association": "MEMBER",
            "state": "PENDING",
            "submitted_at": None,
            "body": "",
        }
        self._transport(
            monkeypatch,
            {
                "issues/comments": [self._rest(fold), self._rest(reply)],
                "pulls/reviews": [pending],
            },
        )
        entries = _fetch_quoted_dispositions("o/r", "254")
        assert _dispositioned_majors(_available(_issue()), [], entries)
        assert "malformed" not in capsys.readouterr().err

    def test_a_refused_read_says_which_listing_and_why(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        def run(cmd: list[str], **kwargs: Any) -> Any:
            return SimpleNamespace(returncode=0, stdout="not json\n", stderr="")

        monkeypatch.setattr(aggregate.subprocess, "run", run)
        assert _fetch_quoted_dispositions("o/r", "254") == []
        err = capsys.readouterr().err
        assert "issue comment listing returned a malformed row" in err
        assert "o/r" not in err

    def test_a_timed_out_listing_withholds(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def run(cmd: list[str], **kwargs: Any) -> Any:
            raise subprocess.TimeoutExpired(cmd, 1)

        monkeypatch.setattr(aggregate.subprocess, "run", run)
        assert _fetch_quoted_dispositions("o/r", "254") == []

    @pytest.mark.parametrize(
        "bad",
        [
            "not json",
            json.dumps(["hyuk-hur", "User", "MEMBER", "2026-11-11T09:00:00Z"]),
            json.dumps(["hyuk-hur", "User", "MEMBER", "", "x"]),
            json.dumps(["hyuk-hur", "User", "MEMBER", None, "x"]),
            json.dumps({"login": "hyuk-hur"}),
        ],
    )
    def test_a_malformed_row_withholds(
        self, monkeypatch: pytest.MonkeyPatch, bad: str
    ) -> None:
        # A row the reader cannot use could be the newer reply that
        # withdraws, so it voids the whole read rather than being skipped.
        fold = _fold()
        reply = _reply(_fold_line(fold["body"]))
        rows = [
            json.dumps(
                [
                    c["login"],
                    c["author_type"],
                    c["association"],
                    c["created_at"],
                    c["body"],
                ]
            )
            for c in (fold, reply)
        ]

        def listing(extra: list[str]):
            def run(cmd: list[str], **kwargs: Any) -> Any:
                out = rows + extra if "/issues/" in cmd[3] else []
                return SimpleNamespace(returncode=0, stdout="\n".join(out), stderr="")

            return run

        monkeypatch.setattr(aggregate.subprocess, "run", listing([]))
        assert _fetch_quoted_dispositions("o/r", "254")
        monkeypatch.setattr(aggregate.subprocess, "run", listing([bad]))
        assert _fetch_quoted_dispositions("o/r", "254") == []

    @pytest.mark.parametrize(
        "repo,pr", [("o/r", "25 4"), ("o/r/x", "254"), ("-o/r", "254"), ("", "254")]
    )
    def test_unusable_target_reads_nothing(
        self, monkeypatch: pytest.MonkeyPatch, repo: str, pr: str
    ) -> None:
        def run(cmd: list[str], **kwargs: Any) -> Any:
            raise AssertionError("no gh call for an unusable target")

        monkeypatch.setattr(aggregate.subprocess, "run", run)
        assert _fetch_quoted_dispositions(repo, pr) == []


def _opener(
    issue: dict[str, Any], *, login: str = _BOT, author_type: str = "Bot"
) -> dict[str, Any]:
    """A thread's opening comment as the posting step writes it."""
    icon = {"major": "+", "minor": "-"}[issue["severity"]]
    return {
        "login": login,
        "author_type": author_type,
        "line": str(issue["line"]),
        "path": issue["file"],
        "body": inline_post_marker(_REVIEWER, 7, "actions-1-1")
        + f"\n{icon} **{issue['severity']}** ({_REVIEWER}): {issue['description']}",
    }


class TestThreadHoldsTheMajor:
    """A newer thread for the major outranks an older quoted reply (round 2)."""

    def _decide(self, *openers: dict[str, Any]) -> dict[tuple[str, int], str]:
        fold = _fold()
        quoted = _parse_quoted_dispositions(
            [fold, _reply(_fold_line(fold["body"]))],
            [],
        ) + _parse_holding_threads(list(openers))
        return _dispositioned_majors(_available(_issue()), [], quoted)

    def test_control_without_a_thread_the_quoted_reply_disposes(self) -> None:
        assert self._decide()

    def test_a_posting_step_thread_for_the_major_holds_it(self) -> None:
        # Folded in r6 and deferred by a quoted reply, then posted inline in
        # r7: that thread is open (or reopened), and it decides.
        assert self._decide(_opener(_issue())) == {}

    def test_a_thread_someone_else_opened_does_not_hold_it(self) -> None:
        planted = _opener(_issue(), login="hyuk-hur", author_type="User")
        assert self._decide(planted)

    def test_a_thread_for_a_minor_does_not_hold_it(self) -> None:
        assert self._decide(_opener(_issue(severity="minor")))

    def test_a_thread_for_another_finding_does_not_hold_it(self) -> None:
        assert self._decide(_opener(_issue(description=_OTHER)))

    def test_the_holding_read_uses_the_real_jq_filter(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fold = _fold()
        reply = _reply(_fold_line(fold["body"]))
        opener = _opener(_issue())
        rest = {
            "user": {"login": opener["login"], "type": opener["author_type"]},
            "in_reply_to_id": None,
            "line": 386,
            "path": opener["path"],
            "body": opener["body"],
        }
        follow_up = {**rest, "in_reply_to_id": 1, "body": "Reopened."}

        def run(cmd: list[str], **kwargs: Any) -> Any:
            endpoint = cmd[3]
            if endpoint.endswith("/issues/254/comments"):
                payload = [TestRead._rest(c) for c in (fold, reply)]
            elif endpoint.endswith("/pulls/254/comments"):
                payload = [rest, follow_up]
            else:
                payload = []
            return _REAL_RUN(
                ["jq", "-r", cmd[cmd.index("--jq") + 1]],
                input=json.dumps(payload),
                text=True,
                capture_output=True,
                env=os.environ,
            )

        monkeypatch.setattr(aggregate.subprocess, "run", run)
        entries = _fetch_quoted_dispositions("o/r", "254")
        assert [e for e in entries if e.get("holds")] == [
            {
                "holds": True,
                "path": "claude/AGENTS.md",
                "line": 386,
                "body": aggregate.normalize_body(opener["body"]),
            }
        ]
        assert _dispositioned_majors(_available(_issue()), [], entries) == {}


class TestReadDispositionsWiring:
    def test_quoted_route_reaches_the_count_and_the_body(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fold = _fold()
        reply = _reply(_fold_line(fold["body"]))
        monkeypatch.setattr(aggregate, "_fetch_disposition_threads", lambda *a: [])
        monkeypatch.setattr(
            aggregate,
            "_fetch_quoted_dispositions",
            lambda *a: _parse_quoted_dispositions([fold, reply], [fold, reply]),
        )
        available = _available(_issue())
        dispositioned = aggregate._read_dispositions(available, "o/r", "254")
        assert aggregate._count_unreviewed_majors(available, dispositioned) == 0
        body = format_summary(
            available,
            "approve",
            "r",
            available,
            dispositioned=dispositioned,
            approve_quorum=True,
            unreviewed_majors=0,
        )
        assert "- + **major**" not in body
        assert (
            f"{_REVIEWER}: answered Deferred to by `hyuk-hur` in a quoted reply" in body
        )
