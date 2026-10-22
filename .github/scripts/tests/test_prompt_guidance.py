"""prompt_guidance.py: the one list of guidance a reviewer prompt must carry.

AT-2844. Consumer copies of the reviewer prompt had diverged (five copies,
7970-25117 bytes, all different), and the only guard, ``TestPromptGuidance``,
runs in no consumer. consumer-health.yml now reads each consumer's prompts
daily through ``prompt_guidance.py``; these tests hold the matcher itself,
the positive control the workflow reads, and this repo's own reviewer
instructions to the same list.
"""

from __future__ import annotations

import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

import pytest

SCRIPT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SCRIPT_DIR))

import review_gemini  # noqa: E402
from prompt_guidance import (  # noqa: E402
    CONGRUENCE,
    CONGRUENCE_BLOCK,
    CONSUMER_PROMPTS,
    EXIT_COMPLETE,
    EXIT_MISSING,
    EXIT_UNREADABLE,
    PIN,
    GuidanceGroup,
    REVIEWER_INSTRUCTIONS,
    missing_items,
)
from reviewer_prompts import build_claude_prompt  # noqa: E402

REPO_ROOT = SCRIPT_DIR.parent.parent
PROMPT_GUIDANCE = SCRIPT_DIR / "prompt_guidance.py"
# The positive control consumer-health.yml reads before any consumer, and
# the pair self-review.yml hands the orchestrator, in prepare's order.
EXAMPLE_PROMPTS = (
    REPO_ROOT / "examples" / "prompts" / "code-review-system.md",
    REPO_ROOT / "examples" / "prompts" / "code-review-checklist.md",
)


def _all_items(*groups: GuidanceGroup) -> str:
    return "\n".join(needle for g in groups for needle in g.items.values())


def _run_cli(*args: object) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(PROMPT_GUIDANCE), *map(str, args)],
        capture_output=True,
        text=True,
        check=False,
    )


class TestMatcher:
    def test_complete_prompt_has_nothing_missing(self) -> None:
        assert missing_items(_all_items(PIN), CONSUMER_PROMPTS) == []

    @pytest.mark.parametrize("dropped", list(CONGRUENCE.items))
    def test_each_congruence_item_is_flagged_when_absent(self, dropped: str) -> None:
        text = "\n".join(
            needle for item, needle in CONGRUENCE.items.items() if item != dropped
        )
        assert missing_items(text, REVIEWER_INSTRUCTIONS) == [dropped]

    def test_a_prompt_with_no_pin_section_owes_no_pin_item(self) -> None:
        """MIGRATION.md: a prompt with no SHA Pins section needs no change."""
        assert missing_items("# A consumer prompt\n", CONSUMER_PROMPTS) == []

    def test_a_partial_pin_section_is_flagged(self) -> None:
        """The AT-2801 drift: one 404 label copied, the other not."""
        text = '- `error="repo-not-found"` -- suggestion'
        assert missing_items(text, CONSUMER_PROMPTS) == [
            "pin-section",
            "pin-comment-matches-false",
            "pin-absent-comment-matches",
            "pin-repo-not-visible",
        ]

    def test_a_section_kept_with_every_label_reworded_is_flagged(self) -> None:
        """The heading is an item, so the section's presence is what makes
        the group owed -- not whether some label survived a rewrite."""
        text = "## GitHub Actions SHA Pins\n\n- an unverified pin: suggestion\n"
        assert missing_items(text, CONSUMER_PROMPTS) == [
            item for item in PIN.items if item != "pin-section"
        ]

    def test_a_label_outside_the_section_does_not_count(self) -> None:
        """Once the heading is there, the labels are read inside its
        section: a label mentioned elsewhere does not complete it."""
        labels = [n for i, n in PIN.items.items() if i != "pin-section"]
        text = (
            "## GitHub Actions SHA Pins\n\n"
            + "\n".join(f"- {n}" for n in labels[:-1])
            + "\n\n## Another Section\n\n"
            + f"- {labels[-1]}\n"
        )
        assert missing_items(text, CONSUMER_PROMPTS) == ["pin-repo-not-visible"]

    def test_a_complete_section_followed_by_another_passes(self) -> None:
        labels = [n for i, n in PIN.items.items() if i != "pin-section"]
        text = (
            "# Prompt\n\n## GitHub Actions SHA Pins\n\n"
            + "\n".join(f"- {n}" for n in labels)
            + "\n\n## Another Section\n\nText.\n"
        )
        assert missing_items(text, CONSUMER_PROMPTS) == []

    def test_a_respaced_heading_still_opens_the_section(self) -> None:
        """The heading is matched after the same normalization the items
        use, so extra spaces do not drop the section scope."""
        labels = [n for i, n in PIN.items.items() if i != "pin-section"]
        text = (
            "##   GitHub  Actions SHA Pins\n\n"
            + "\n".join(f"- {n}" for n in labels[:-1])
            + "\n\n## Another Section\n\n"
            + f"- {labels[-1]}\n"
        )
        assert missing_items(text, CONSUMER_PROMPTS) == ["pin-repo-not-visible"]

    @pytest.mark.parametrize(
        "heading",
        ["### GitHub Actions SHA Pins", "## GitHub Actions SHA Pins (legacy)"],
        ids=["deeper", "suffixed"],
    )
    def test_a_heading_that_is_not_the_exact_one_owes_nothing(
        self, heading: str
    ) -> None:
        """Only the exact heading opens the section: another level or a
        longer title is no section, so the all-or-none group owes nothing."""
        assert missing_items(f"{heading}\n\nText.\n", CONSUMER_PROMPTS) == []

    @pytest.mark.parametrize(
        "heading",
        ["### GitHub Actions SHA Pins", "## GitHub Actions SHA Pins (legacy)"],
        ids=["deeper", "suffixed"],
    )
    def test_a_heading_that_is_not_the_exact_one_does_not_scope(
        self, heading: str
    ) -> None:
        """Without the exact heading the labels are read in the whole text,
        and the section item is what is missing."""
        labels = [n for i, n in PIN.items.items() if i != "pin-section"]
        text = (
            f"{heading}\n\n"
            + "\n".join(f"- {n}" for n in labels[:-1])
            + "\n\n## Another Section\n\n"
            + f"- {labels[-1]}\n"
        )
        assert missing_items(text, CONSUMER_PROMPTS) == ["pin-section"]

    def test_require_all_makes_an_absent_group_missing(self) -> None:
        """The positive control must not pass by carrying nothing."""
        assert missing_items("", CONSUMER_PROMPTS, require_all=True) == list(PIN.items)

    def test_consumer_prompts_owe_no_congruence_item(self) -> None:
        """One home: the questions travel in the reviewers' own instructions."""
        assert CONGRUENCE not in CONSUMER_PROMPTS

    def test_a_sentence_wrapped_across_lines_still_matches(self) -> None:
        wrapped = _all_items(CONGRUENCE).replace(" ", "\n  ")
        assert missing_items(wrapped, (CONGRUENCE,)) == []

    def test_a_reworded_sentence_does_not_match(self) -> None:
        reworded = _all_items(CONGRUENCE).replace("is the gap stated", "is it said")
        assert missing_items(reworded, (CONGRUENCE,)) == ["congruence-other"]


class TestCli:
    def test_complete_prompt_exits_zero_and_prints_nothing(
        self, tmp_path: Path
    ) -> None:
        prompt = tmp_path / "system.md"
        prompt.write_text(_all_items(PIN), encoding="utf-8")
        result = _run_cli(prompt)
        assert (result.returncode, result.stdout) == (EXIT_COMPLETE, "")

    def test_missing_items_exit_one_and_print_one_id_per_line(
        self, tmp_path: Path
    ) -> None:
        prompt = tmp_path / "system.md"
        prompt.write_text('- `error="repo-not-found"`\n', encoding="utf-8")
        result = _run_cli(prompt)
        assert result.returncode == EXIT_MISSING
        assert result.stdout.splitlines() == [
            "pin-section",
            "pin-comment-matches-false",
            "pin-absent-comment-matches",
            "pin-repo-not-visible",
        ]

    def test_control_flag_requires_every_group(self, tmp_path: Path) -> None:
        prompt = tmp_path / "system.md"
        prompt.write_text("# Nothing here\n", encoding="utf-8")
        assert _run_cli(prompt).returncode == EXIT_COMPLETE
        result = _run_cli("--control", prompt)
        assert result.returncode == EXIT_MISSING
        assert result.stdout.splitlines() == list(PIN.items)

    def test_items_split_across_the_two_files_count_while_no_heading_intervenes(
        self, tmp_path: Path
    ) -> None:
        """prepare concatenates system + checklist; so does the check. The
        section runs on into the checklist only until a heading of its level
        or higher: this checklist opens with none."""
        system = tmp_path / "system.md"
        checklist = tmp_path / "checklist.md"
        needles = list(PIN.items.values())
        system.write_text("\n".join(needles[:2]), encoding="utf-8")
        checklist.write_text("\n".join(needles[2:]), encoding="utf-8")
        assert _run_cli("--control", system, checklist).returncode == EXIT_COMPLETE

    def test_a_heading_opening_the_checklist_ends_the_system_files_section(
        self, tmp_path: Path
    ) -> None:
        """The real checklist opens with a ``#`` heading, which ends a
        ``##`` section the system prompt left open, so labels below it are
        outside the section -- where a reviewer reading context.md finds
        them too."""
        first_line = EXAMPLE_PROMPTS[1].read_text(encoding="utf-8").splitlines()[0]
        assert first_line.startswith("# ")
        system = tmp_path / "system.md"
        checklist = tmp_path / "checklist.md"
        needles = list(PIN.items.values())
        system.write_text("\n".join(needles[:2]), encoding="utf-8")
        checklist.write_text("\n".join([first_line, *needles[2:]]), encoding="utf-8")
        result = _run_cli(system, checklist)
        assert result.returncode == EXIT_MISSING
        assert result.stdout.splitlines() == list(PIN.items)[2:]

    def test_unreadable_file_is_not_a_verdict(self, tmp_path: Path) -> None:
        result = _run_cli(tmp_path / "absent.md")
        assert result.returncode == EXIT_UNREADABLE
        assert result.stdout == ""

    def test_no_file_is_a_usage_error(self) -> None:
        assert _run_cli().returncode == EXIT_UNREADABLE
        assert _run_cli("--control").returncode == EXIT_UNREADABLE

    def test_an_unknown_option_is_a_usage_error_not_a_filename(
        self, tmp_path: Path
    ) -> None:
        prompt = tmp_path / "system.md"
        prompt.write_text(_all_items(PIN), encoding="utf-8")
        result = _run_cli("--controll", prompt)
        assert result.returncode == EXIT_UNREADABLE
        assert (
            "unrecognized arguments" in result.stderr or "--controll" in result.stderr
        )

    def test_the_flag_may_follow_the_files(self, tmp_path: Path) -> None:
        prompt = tmp_path / "system.md"
        prompt.write_text("# Nothing here\n", encoding="utf-8")
        assert _run_cli(prompt, "--control").returncode == EXIT_MISSING


class TestPositiveControl:
    """The prompt consumer-health.yml reads first to prove its path works.

    A daily check that reports "nothing missing" because every fetch
    returned nothing is indistinguishable from a clean fleet; the workflow
    therefore fails unless this pair, read through the same fetch and the
    same matcher with every group required, comes back complete.
    """

    def test_example_prompts_carry_every_item(self) -> None:
        result = _run_cli("--control", *EXAMPLE_PROMPTS)
        assert result.stdout == ""
        assert result.returncode == EXIT_COMPLETE


def _claude_prompt() -> str:
    return build_claude_prompt("0", "")


def _codex_prompt() -> str:
    return (SCRIPT_DIR / "review_prompt.md").read_text(encoding="utf-8")


def _gemini_prompt() -> str:
    return review_gemini.build_user_prompt("dummy diff")


_REVIEWER_PROMPTS = pytest.mark.parametrize(
    "render",
    [_claude_prompt, _codex_prompt, _gemini_prompt],
    ids=["claude", "codex", "gemini"],
)


class TestReviewerPromptsCarryCongruence:
    """Every reviewer's own instructions carry the congruence questions.

    These three are the questions' only home in the fleet: they are
    base-owned, travel with a consumer's pin and the runner's, and so reach
    every review without a consumer or runner copy. The Claude prompt is
    read from reviewer_prompts.py, whose output test_reviewer_prompts.py
    holds byte-equal to the inline shell in base-ai-review-single.yml, so
    that copy is covered too.
    """

    @_REVIEWER_PROMPTS
    def test_prompt_carries_every_congruence_item(
        self, render: Callable[[], str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("EXISTING_COMMENTS", raising=False)
        assert missing_items(render(), REVIEWER_INSTRUCTIONS) == []

    @_REVIEWER_PROMPTS
    def test_prompt_carries_the_one_block_unchanged(
        self, render: Callable[[], str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """One text, byte for byte: a block edited in one copy fails here.
        The base carries it four times -- the inline shell in
        base-ai-review-single.yml, reviewer_prompts.py, review_prompt.md and
        review_gemini.py -- because the Claude prompt is built in the
        workflow's own shell, which cannot read a file added in the same PR
        (reviewer_prompts.py docstring). This test holds the three rendered
        prompts to CONGRUENCE_BLOCK, and test_reviewer_prompts.py holds the
        shell byte-equal to reviewer_prompts.py, so the four stay one text."""
        monkeypatch.delenv("EXISTING_COMMENTS", raising=False)
        assert CONGRUENCE_BLOCK in render()


def test_example_prompts_carry_no_congruence_copy() -> None:
    """A consumer copies its prompts from examples/; a copy there is a
    second home that the next wording change would leave behind."""
    text = "\n".join(p.read_text(encoding="utf-8") for p in EXAMPLE_PROMPTS)
    assert "CONGRUENCE" not in text.upper()
    assert all(needle not in text for needle in CONGRUENCE.items.values())
