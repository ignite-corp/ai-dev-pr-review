"""Refuse a pyright run that looked at a different set of files than
`[tool.pyright]` selects.

pyright's default exclude is `**/.*`, which skips dot-directories even when
a file is named on the command line, and it then reports "0 errors" over
zero files with exit 0 (AT-2418). pyproject.toml overrides that exclude, and
this script is what makes an override that has stopped working visible: it
counts the source files the include/exclude lists select and refuses a run
whose `filesAnalyzed` is a different number -- or whose selection is empty,
because 0 == 0 is the one equality that must never pass: an `include` with
a one-character typo makes both sides zero (measured on PR #192, round 5,
where the previous in-workflow gate exited 0 on exactly that).

It lives here rather than inline in ci.yml so that pytest, ruff and pyright
-- the three checks ci.yml runs -- can see it. Stdlib only.

What it sees: the `include` roots, and the `exclude` globs in the two shapes
this repository uses -- `**/<glob>`, matched against every suffix of a path
(any depth), and an anchored `<glob>/<glob>/...`, matched from the project
root -- each applied to the path and to each directory above it, which is
how pyright treats a directory pattern. A pattern in any other shape (a
`**` that is not the whole first segment) is refused rather than guessed
at, so an exclude this script cannot read fails loudly instead of being
counted.

What it does not see: sources pyright adds on its own -- a module imported
from outside `include`, or the `ignore` / `strict` lists -- none of which
this repository sets. An absent `exclude` is read as an empty list, which is
what pyright's own fallback then contradicts, loudly.
"""

from __future__ import annotations

import fnmatch
import json
import sys
import tomllib
from collections.abc import Iterable, Sequence
from pathlib import Path, PurePath

ANY_DEPTH = "**/"
PYPROJECT = "pyproject.toml"
# What pyright counts in `filesAnalyzed`: modules and the stubs beside them.
SOURCE_SUFFIXES = ("*.py", "*.pyi")


def _segments_match(parts: Sequence[str], pattern_parts: Sequence[str]) -> bool:
    return len(parts) == len(pattern_parts) and all(
        fnmatch.fnmatchcase(part, pattern)
        for part, pattern in zip(parts, pattern_parts)
    )


def matches(path: PurePath, pattern: str) -> bool:
    """Does `pattern` name `path` itself (not a file beneath it)?"""
    if pattern.startswith(ANY_DEPTH):
        tail = PurePath(pattern[len(ANY_DEPTH) :]).parts
        return any(
            _segments_match(path.parts[start:], tail)
            for start in range(len(path.parts))
        )
    return _segments_match(path.parts, PurePath(pattern).parts)


def check_exclude_patterns(patterns: Iterable[str]) -> None:
    """Refuse a pattern shape this gate cannot read, before any matching.

    Validated as a whole list and up front, not inside the match loop: there,
    whether a malformed pattern was ever seen depended on its position (an
    earlier pattern that matched returned first) and on the tree (a selection
    of no files never called the matcher at all), so the docstring's "fails
    loudly" held of a subset of the cases it claimed.
    """
    for pattern in patterns:
        supported = "**" not in pattern or (
            pattern.startswith(ANY_DEPTH) and "**" not in pattern[len(ANY_DEPTH) :]
        )
        if not supported:
            raise ValueError(
                f"unsupported exclude pattern {pattern!r}: this gate reads"
                " '**/<glob>' and anchored globs only"
            )


def is_excluded(path: PurePath, patterns: Iterable[str]) -> bool:
    """`path`, or a directory above it, is named by one of `patterns`.

    Matching only; `check_exclude_patterns` is what refuses a shape.
    """
    return any(
        matches(candidate, pattern)
        for pattern in patterns
        for candidate in (path, *path.parents)
    )


def expected_files(
    root: Path, include: Iterable[str], exclude: Sequence[str]
) -> list[Path]:
    """The source files `[tool.pyright]` selects, relative to `root`.

    A set, because two `include` roots may overlap -- a nested root counted
    `.github/scripts/tests` twice, 94 files for 59, and pyright reports each
    file once. Stubs count: a single `.pyi` under `.github/scripts` takes
    pyright's `filesAnalyzed` to 60 against 59 .py files (measured), so a
    `*.py`-only count would fail every later run and blame the config.

    Every root is checked on its own, because the total cannot speak for it:
    `[".github/scripts", "exampels"]` selects the same 59 files as the
    surviving root alone (measured), which is the number pyright then reports
    -- the two agree and a whole tree goes unchecked, where `expected == 0`
    sees nothing wrong.
    """
    check_exclude_patterns(exclude)
    selected: set[Path] = set()
    for pattern in include:
        directory = root / pattern
        if not directory.is_dir():
            raise ValueError(
                f"include root {pattern!r} is not a directory: a root that"
                " selects nothing is invisible in the total (AT-2418)"
            )
        found = {
            path.relative_to(root)
            for suffix in SOURCE_SUFFIXES
            for path in directory.rglob(suffix)
            if not is_excluded(path.relative_to(root), exclude)
        }
        if not found:
            raise ValueError(
                f"include root {pattern!r} contributes no source file: either"
                " it is the wrong path or `exclude` swallows all of it"
            )
        selected |= found
    return sorted(selected)


def refusal(files_analyzed: int, expected: int) -> str | None:
    """Why the run is refused, or None when it is not."""
    if expected == 0:
        return (
            "the [tool.pyright] include/exclude selects no .py files at all:"
            " 0 analyzed of 0 expected is the empty-set pass this gate exists"
            " to refuse (AT-2418)"
        )
    if files_analyzed != expected:
        return (
            f"pyright analyzed {files_analyzed} of {expected} selected files:"
            " the include/exclude config is wrong (AT-2418)"
        )
    return None


def main(argv: Sequence[str]) -> int:
    """`argv[1]` is the path of pyright's --outputjson report."""
    if len(argv) != 2:
        print(f"usage: {Path(argv[0]).name} <pyright.json>", file=sys.stderr)
        return 2
    with open(argv[1], encoding="utf-8") as handle:
        report = json.load(handle)
    root = Path.cwd()
    with open(root / PYPROJECT, "rb") as handle:
        config = tomllib.load(handle)["tool"]["pyright"]
    # `exclude` is optional: dropped from pyproject.toml, pyright falls back to
    # its default `**/.*`, analyzes nothing, and that is the AT-2418 defect this
    # script reports -- so it must not die of a KeyError reading its own config.
    # `include` stays subscripted: its absence is a different, equally loud
    # failure.
    try:
        expected = len(
            expected_files(root, config["include"], config.get("exclude", []))
        )
    except ValueError as unreadable:
        # A config this gate cannot read is a refusal with a reason, not a
        # traceback: the run it is asked about is exactly the one nobody can
        # interpret without one.
        print(unreadable, file=sys.stderr)
        return 1
    summary = report["summary"]
    print(
        f"filesAnalyzed={summary['filesAnalyzed']} expected={expected}"
        f" errors={summary['errorCount']}"
    )
    for item in report["generalDiagnostics"]:
        start = item["range"]["start"]
        print(
            f"{item['file']}:{start['line'] + 1}:{start['character'] + 1}"
            f" {item['severity']} {item.get('rule', '')}: {item['message']}"
        )
    why = refusal(summary["filesAnalyzed"], expected)
    if why is None:
        return 0
    print(why, file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
