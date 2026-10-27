"""The "see the note above X" pointers in the reviewer workflows (AT-2567).

PR #186 (AT-2539) gave the continue-on-error rationale one home and replaced
its copies with pointers:

* the 'Run Gemini review' and 'Run Codex review' steps point at the note
  above 'Run Claude review' (the hub note);
* the hub note points at the orchestrator's caveat above ``review-codex-s``;
* that caveat points back at the hub note.

Those four are checked against the notes' content. Every other step pointer
in base-ai-review-single.yml is held to the weaker bar of naming a step that
exists and has a note above it -- one of them had already drifted to a step
name that no longer existed when this module was written.

The step and job names were already looked up by other tests, but nothing
tied a pointer to the note it names: moving the note, or renaming its step or
job, left every pointer dangling with the suite green.

Each pointer test below reads the pointer out of the comment block directly
above its own step or job and resolves the name it carries to the comment
block above that target, and the targets must carry the notes' content.
Two further tests do no pointer resolution: one pins the hub note's
rationale where it lives, the other keeps the all-pointers scan from passing
vacuously. The pointer text is parsed rather than restated, so a rename has
to land on both ends -- and the assertions anchor on the comment blocks the
pointers live in, never on neighbouring context lines (the AT-2566 lesson).

Test-module hygiene: all imports belong at the top of this file per PEP 8 --
do not add ``import foo`` statements inside test function bodies.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

_WORKFLOWS = Path(__file__).resolve().parents[2] / "workflows"
_SINGLE = _WORKFLOWS / "base-ai-review-single.yml"
_ORCHESTRATOR = _WORKFLOWS / "base-ai-review-orchestrator.yml"

# "the note above 'Run Claude review'" names a step; "the note above
# review-codex-s" names a job.
_STEP_POINTER = re.compile(r"the note above '([^']+)'")
_JOB_POINTER = re.compile(r"the note above ([A-Za-z0-9_-]+)")


def _comment_above(path: Path, anchor: re.Pattern[str]) -> str:
    """The comment block directly above the one line `anchor` matches.

    Returned flattened, because comment lines are rewrapped freely and a
    pointer often breaks across two of them. Empty when nothing but blank
    space sits above the line.
    """
    lines = path.read_text(encoding="utf-8").splitlines()
    hits = [i for i, line in enumerate(lines) if anchor.fullmatch(line)]
    assert len(hits) == 1, (
        f"{anchor.pattern!r} matched {len(hits)} lines in {path.name}"
    )
    block: list[str] = []
    for line in reversed(lines[: hits[0]]):
        stripped = line.strip()
        if not stripped.startswith("#"):
            break
        block.append(stripped.lstrip("#").strip())
    return " ".join(reversed(block))


def _above_step(path: Path, step: str) -> str:
    return _comment_above(path, re.compile(r"\s*- name: " + re.escape(step)))


def _above_job(path: Path, job: str) -> str:
    # Two spaces exactly: job keys sit directly under `jobs:`, so this does
    # not also match a nested key of the same name.
    return _comment_above(path, re.compile(r"  " + re.escape(job) + r":"))


def _one_pointer(pattern: re.Pattern[str], block: str) -> str:
    """The name the block's single pointer carries.

    Exactly one, the way `_comment_above` holds its anchor to one line: a
    block is flattened from every contiguous comment line, so a second
    pointer earlier in it would otherwise be validated in place of this one.
    """
    names = pattern.findall(block)
    assert len(names) == 1, f"{len(names)} pointers in: {block}"
    return names[0]


def _hub_note() -> str:
    # Non-empty, or a detached note would compare equal to an unresolved one.
    note = _above_step(_SINGLE, "Run Claude review")
    assert note, "no comment block directly above 'Run Claude review'"
    return note


def _orchestrator_caveat() -> str:
    # Found through the hub note's pointer, not by a restated job name.
    return _above_job(_ORCHESTRATOR, _one_pointer(_JOB_POINTER, _hub_note()))


class TestHubNote:
    """The note the other three pointers lean on, pinned where it lives."""

    def test_hub_note_states_the_rationale_the_reviewer_steps_point_to(
        self,
    ) -> None:
        note = _hub_note()
        assert "continue-on-error here and on the gemini/codex reviewer steps" in note

    def test_hub_note_points_at_the_orchestrator_caveat(self) -> None:
        note = _hub_note()
        assert "base-ai-review-orchestrator.yml, the note above " in note
        # test_orchestrator_sequential_gates.py pins this string to the file;
        # here it proves the pointer landed on the block that carries it.
        assert "nobody reads it as protection" in _orchestrator_caveat()


class TestReviewerStepsPointAtTheHubNote:
    @pytest.mark.parametrize("step", ["Run Gemini review", "Run Codex review"])
    def test_pointer_resolves_to_the_hub_note(self, step: str) -> None:
        block = _above_step(_SINGLE, step)
        assert block.startswith("Why continue-on-error and its cost:"), block
        target = _one_pointer(_STEP_POINTER, block)
        assert _above_step(_SINGLE, target) == _hub_note()


class TestOrchestratorCaveatPointsBack:
    def test_caveat_pointer_resolves_to_the_hub_note(self) -> None:
        caveat = _orchestrator_caveat()
        target = _one_pointer(_STEP_POINTER, caveat)
        assert f"the note above '{target}' in base-ai-review-single.yml" in caveat
        assert _above_step(_SINGLE, target) == _hub_note()


def _single_step_pointers() -> list[str]:
    """Every step name a `the note above '...'` in single.yml's comments carries.

    Comments only, the discipline `_comment_above` keeps: the phrase in a run
    script or an embedded prompt is not a pointer. Each contiguous comment
    block is flattened on its own, since a pointer wraps within one.
    """
    blocks: list[list[str]] = [[]]
    for line in _SINGLE.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            blocks[-1].append(stripped.lstrip("#").strip())
        elif blocks[-1]:
            blocks.append([])
    names = {name for b in blocks for name in _STEP_POINTER.findall(" ".join(b))}
    return sorted(names)


class TestEveryStepPointerResolves:
    def test_the_scan_finds_the_pointers(self) -> None:
        """A scan that found nothing would pass the test below vacuously."""
        assert "Run Claude review" in _single_step_pointers()

    @pytest.mark.parametrize("step", _single_step_pointers())
    def test_pointer_names_a_step_with_a_note_above_it(self, step: str) -> None:
        assert _above_step(_SINGLE, step), step
