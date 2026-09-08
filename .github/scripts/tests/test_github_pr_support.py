"""Tests for github_pr_support.py's own helpers that have no other home.

`display_path` is already exercised end-to-end from test_filter_pr_diff.py
(it was written for paths first); `format_labels` (AT-2222) is exercised
here since it has no other consumer module to piggyback on.
"""

from __future__ import annotations

from github_pr_support import format_labels


class TestFormatLabels:
    def test_empty_input_is_empty_string_not_omitted(self) -> None:
        assert format_labels([]) == ""

    def test_one_label(self) -> None:
        assert format_labels(["design"]) == "design"

    def test_sorted_lexicographically(self) -> None:
        assert format_labels(["zeta", "alpha", "mid"]) == "alpha, mid, zeta"

    def test_more_than_20_labels_drops_the_overflow_after_sorting(self) -> None:
        names = [f"label-{i:02d}" for i in range(25)]
        result = format_labels(names)
        kept = result.split(", ")
        assert len(kept) == 20
        assert kept == sorted(names)[:20]
        # The dropped tail (label-20..label-24) never appears, silently.
        for dropped in sorted(names)[20:]:
            assert dropped not in result

    def test_name_over_50_chars_is_truncated_to_49_plus_ellipsis(self) -> None:
        long_name = "x" * 60
        result = format_labels([long_name])
        assert result == "x" * 49 + "\u2026"
        assert len(result) == 50

    def test_name_at_exactly_50_chars_is_not_truncated(self) -> None:
        name = "x" * 50
        assert format_labels([name]) == name

    def test_control_chars_and_backtick_are_escaped_via_display_path(self) -> None:
        assert format_labels(["a\nb"]) == "a\\nb"
        assert format_labels(["a`b"]) == "a\u02cbb"
        assert format_labels(["a\x00b"]) == "a\\x00b"

    def test_join_separator_matches_excluded_paths_convention(self) -> None:
        assert format_labels(["one", "two"]) == "one, two"
