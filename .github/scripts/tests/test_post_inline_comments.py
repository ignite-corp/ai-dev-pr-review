"""Tests for post_inline_comments fuzzy dedup logic.

Covers the seven scenarios that the previous exact-match dedup missed,
including paraphrases that share token vocabulary, line-distance gating
on the same path, and backward compatibility when no line is supplied.
"""

from __future__ import annotations

from typing import Any

from post_inline_comments import _is_duplicate, _normalize_body


def _thread(path: str, body: str, line: int | None = None) -> dict[str, Any]:
    """Build a thread dict matching fetch_existing_threads output."""
    return {"path": path, "line": line, "body": _normalize_body(body)}


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

    def test_distant_lines_no_dedup(self) -> None:
        existing = [_thread("a.py", "Missing input validation on user_id", line=10)]
        # Line 100 is well outside the default window of 5.
        assert not _is_duplicate(
            "a.py",
            "Missing input validation on user_id",
            existing,
            line=100,
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
