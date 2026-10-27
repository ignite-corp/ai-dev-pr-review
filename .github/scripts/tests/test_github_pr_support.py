"""Tests for github_pr_support.py's own helpers that have no other home.

`display_path` is already exercised end-to-end from test_filter_pr_diff.py
(it was written for paths first); `format_labels` (AT-2222) is exercised
here since it has no other consumer module to piggyback on.
"""

from __future__ import annotations

import pytest

from github_pr_support import array_field, format_labels, object_field


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


@pytest.mark.parametrize("value", [None, {}, ["a"], "login", 1])
def test_object_field_reads_anything_but_an_object_as_empty(value: object) -> None:
    """AT-2604: the helper is typed dict[str, Any], so it checks that it is one.

    A truthy non-object (a list, a string) used to come back as itself, and
    the caller's `.get` was an AttributeError pyright could no longer see.
    """
    assert object_field({"author": value}, "author") == {}


def test_object_field_returns_an_object_unchanged() -> None:
    author = {"login": "octocat"}
    assert object_field({"author": author}, "author") is author


@pytest.mark.parametrize("value", [None, [], {"a": 1}, "nodes", 1])
def test_array_field_reads_anything_but_an_array_as_empty(value: object) -> None:
    """AT-2604: `x.get("nodes") or []` annotated as a list asserted the shape;
    a truthy non-array reached `head[0]` / `for` unchecked."""
    assert array_field({"nodes": value}, "nodes") == []


def test_array_field_returns_an_array_unchanged() -> None:
    nodes = [{"id": 1}]
    assert array_field({"nodes": nodes}, "nodes") is nodes
