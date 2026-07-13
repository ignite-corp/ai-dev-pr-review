"""Tests for post_inline_comments fuzzy dedup logic.

Covers the scenarios that the previous exact-match dedup missed,
including paraphrases that share token vocabulary, line-distance gating
on the same path, strong-similarity dedup across force-push line shifts,
and backward compatibility when no line is supplied.
"""

from __future__ import annotations

import importlib
import sys
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
