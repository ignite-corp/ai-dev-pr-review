"""The release procedure's review step has one home (AT-2542).

A release PR bumps the script pins to a tag that is only created after the
merge, so `self-review.yml`'s `guard` skips its review and the PR merges on
`test` + `guard` alone. The user decided on 2026-10-27 that this stays so, and
that the review comes after tagging: dispatching self-review at the release PR
is a required step of the release procedure, not an optional follow-up.

That decision lives in the "Release & pin discipline" section of the ops doc.
These tests hold the section to the three things the decision needs -- which
PRs qualify, the dispatch command, and what to do with a finding -- and keep
the command from growing a second copy in any other maintained document,
workflow or script.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
OPS_DOC = ROOT / "docs/ops/claude-auth-and-review-operations.md"
SECTION = "## Release & pin discipline"
DISPATCH = 'gh workflow run self-review.yml --ref main -f pr_number="$RELEASE_PR"'
# The scan matches the command without its flags, so a second copy that
# spells the flags differently is still found.
DISPATCH_NEEDLE = DISPATCH.split(" --", 1)[0]
# Dated narratives and per-ticket records quote the procedure as it was; they
# are history, not homes.
HISTORY_DIRS = ("docs/case-study/", "docs/tasks/")
MAINTAINED_DIRS = (".github", "docs", "examples", "plugins")
TEXT_SUFFIXES = {".md", ".yml", ".yaml", ".py", ".sh"}


def release_section() -> str:
    text = OPS_DOC.read_text(encoding="utf-8")
    assert SECTION in text, f"{OPS_DOC.relative_to(ROOT)} has no {SECTION!r} section"
    start = text.index(SECTION)
    end = text.find("\n## ", start + len(SECTION))
    return text[start : end if end != -1 else len(text)]


def normalized(text: str) -> str:
    """Collapse the line wrapping so a phrase can be matched across lines."""
    return re.sub(r"\s+", " ", text)


def test_release_section_makes_the_post_tag_dispatch_required() -> None:
    section = normalized(release_section())
    assert DISPATCH in section
    assert "required step of the release, not an optional follow-up" in section


def test_release_section_states_which_prs_merge_without_a_verdict() -> None:
    section = normalized(release_section())
    assert "An eligible release PR merges on `test` + `guard` alone." in section
    assert (
        "A PR qualifies only when its diff is limited to the `ref:` pins of the"
        " script checkouts and the version notes"
    ) in section


def test_release_section_sends_post_tag_findings_to_a_follow_up_pr() -> None:
    section = normalized(release_section())
    assert "fix it in a follow-up PR and ship it in the next release" in section
    assert "Never re-tag: consumers pin release SHAs" in section


def maintained_files() -> list[Path]:
    """Root documents plus the maintained directories, minus history.

    Walked from named directories rather than from the root so a checkout's
    local `.claude/worktrees/*` copies of this tree are never counted.
    """
    found = [p for p in ROOT.glob("*") if p.suffix in TEXT_SUFFIXES]
    for top in MAINTAINED_DIRS:
        # A renamed or removed directory must fail here, not narrow the scan.
        assert (ROOT / top).is_dir(), top
        found += [p for p in (ROOT / top).rglob("*") if p.suffix in TEXT_SUFFIXES]
    return [
        p
        for p in found
        if p.is_file()
        and p.resolve() != Path(__file__).resolve()
        and not p.relative_to(ROOT).as_posix().startswith(HISTORY_DIRS)
    ]


def test_dispatch_command_has_one_home() -> None:
    homes = [
        path.relative_to(ROOT).as_posix()
        for path in maintained_files()
        if DISPATCH_NEEDLE in path.read_text(encoding="utf-8")
    ]
    assert homes == [OPS_DOC.relative_to(ROOT).as_posix()]
