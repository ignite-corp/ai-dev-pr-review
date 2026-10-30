"""Shared policy travels in actual base reviewer instructions (AT-3159)."""

from __future__ import annotations

import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

import pytest

import review_claude_local
import review_codex_local
import review_gemini
from prompt_guidance import (
    CONGRUENCE,
    CONGRUENCE_BLOCK,
    CONSUMER_PROMPTS,
    EXIT_COMPLETE,
    EXIT_MISSING,
    EXIT_UNREADABLE,
    PIN,
    PIN_BLOCK,
    REVIEWER_INSTRUCTIONS,
    missing_items,
)
from reviewer_prompts import build_claude_prompt

SCRIPT_DIR = Path(__file__).resolve().parent.parent
REPO_ROOT = SCRIPT_DIR.parent.parent
PROMPT_GUIDANCE = SCRIPT_DIR / "prompt_guidance.py"
EXAMPLE_PROMPTS = (
    REPO_ROOT / "examples/prompts/code-review-system.md",
    REPO_ROOT / "examples/prompts/code-review-checklist.md",
)


def _run_cli(*args: object) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(PROMPT_GUIDANCE), *map(str, args)],
        capture_output=True,
        text=True,
        check=False,
    )


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


class TestReviewerInstructions:
    @_REVIEWER_PROMPTS
    def test_every_renderer_carries_exact_canonical_blocks(
        self, render: Callable[[], str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Claude's actual workflow shell is executed against this mirror by
        # test_reviewer_prompts.py; local reviewers use these same entrypoints.
        monkeypatch.delenv("EXISTING_COMMENTS", raising=False)
        text = render()
        assert missing_items(text, REVIEWER_INSTRUCTIONS) == []
        assert CONGRUENCE_BLOCK in text
        assert PIN_BLOCK in text

    @pytest.mark.parametrize("dropped", list(PIN.items))
    @_REVIEWER_PROMPTS
    def test_omitting_any_policy_rule_fails_the_actual_control(
        self, render: Callable[[], str], dropped: str, tmp_path: Path
    ) -> None:
        # Check the real rendered instructions, not a synthetic set of labels.
        text = render()
        rule = PIN.items[dropped]
        assert rule in text
        prompt = tmp_path / "base.md"
        prompt.write_text(text.replace(rule, ""), encoding="utf-8")
        result = _run_cli("--control", prompt)
        assert result.returncode == EXIT_MISSING
        assert dropped in result.stdout.splitlines()

    @pytest.mark.parametrize("dropped", list(CONGRUENCE.items))
    def test_missing_congruence_is_still_detected(self, dropped: str) -> None:
        text = _codex_prompt().replace(CONGRUENCE.items[dropped], "")
        assert missing_items(text, REVIEWER_INSTRUCTIONS) == [dropped]

    def test_retaining_labels_but_changing_severity_fails(self) -> None:
        text = _codex_prompt().replace("Raise as `major`:", "Raise as `critical`:")
        assert missing_items(text, REVIEWER_INSTRUCTIONS) == ["pin-unmatched"]

    def test_wrapping_does_not_change_the_rule(self) -> None:
        text = "\n".join(
            line if line.startswith("## ") else line.replace(" ", "\n  ")
            for line in _codex_prompt().splitlines()
        )
        assert missing_items(text, REVIEWER_INSTRUCTIONS) == []

    def test_labels_outside_pin_section_do_not_complete_it(self) -> None:
        text = _codex_prompt().replace(PIN.items["pin-repo-not-visible"], "")
        text += "\n## Other\n" + PIN.items["pin-repo-not-visible"]
        assert missing_items(text, REVIEWER_INSTRUCTIONS) == ["pin-repo-not-visible"]


class TestCli:
    def test_actual_health_positive_control_is_complete(self) -> None:
        result = _run_cli("--control", SCRIPT_DIR / "review_prompt.md")
        assert (result.returncode, result.stdout) == (EXIT_COMPLETE, "")

    def test_empty_control_never_passes(self, tmp_path: Path) -> None:
        prompt = tmp_path / "base.md"
        prompt.write_text("", encoding="utf-8")
        result = _run_cli(prompt, "--control")
        assert result.returncode == EXIT_MISSING
        assert set(result.stdout.splitlines()) == {
            *CONGRUENCE.items,
            *PIN.items,
        }

    @pytest.mark.parametrize(
        "text", ["# Repo guidance", PIN_BLOCK, 'error="repo-not-found"']
    )
    def test_consumer_copies_owe_no_shared_guidance(
        self, text: str, tmp_path: Path
    ) -> None:
        prompt = tmp_path / "repo.md"
        prompt.write_text(text, encoding="utf-8")
        assert missing_items(text, CONSUMER_PROMPTS) == []
        result = _run_cli(prompt)
        assert (result.returncode, result.stdout) == (EXIT_COMPLETE, "")

    def test_pair_is_read_even_when_no_shared_rule_is_owed(
        self, tmp_path: Path
    ) -> None:
        prompt = tmp_path / "repo.md"
        prompt.write_text("# Repo guidance", encoding="utf-8")
        assert _run_cli(prompt, tmp_path / "absent.md").returncode == EXIT_UNREADABLE

    def test_unreadable_text_is_not_a_verdict(self, tmp_path: Path) -> None:
        prompt = tmp_path / "repo.md"
        prompt.write_bytes(b"\xff")
        result = _run_cli(prompt)
        assert (result.returncode, result.stdout) == (EXIT_UNREADABLE, "")

    def test_usage_errors_remain_errors(self) -> None:
        assert _run_cli().returncode == EXIT_UNREADABLE
        assert _run_cli("--control").returncode == EXIT_UNREADABLE
        result = _run_cli("--controll", SCRIPT_DIR / "review_prompt.md")
        assert result.returncode == EXIT_UNREADABLE
        assert "unrecognized arguments: --controll" in result.stderr
        assert "cannot read prompt file" not in result.stderr

    def test_usage_guard_rejects_a_mutant_that_reads_options_as_files(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self.test_usage_errors_remain_errors()
        source = PROMPT_GUIDANCE.read_text(encoding="utf-8")
        original = "args = parser.parse_args(argv)"
        assert source.count(original) == 1
        mutant = tmp_path / "prompt_guidance.py"
        mutant.write_text(
            source.replace(original, 'args = parser.parse_args(["--", *argv])'),
            encoding="utf-8",
        )
        monkeypatch.setitem(globals(), "PROMPT_GUIDANCE", mutant)
        result = _run_cli("--controll", SCRIPT_DIR / "review_prompt.md")
        assert result.returncode == EXIT_UNREADABLE
        assert "cannot read prompt file: FileNotFoundError" in result.stderr
        with pytest.raises(AssertionError):
            self.test_usage_errors_remain_errors()


@pytest.mark.parametrize("reviewer", ["claude", "codex"])
def test_local_shims_receive_shared_policy(
    reviewer: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "context.md").write_text("# Repository guidance\n", encoding="utf-8")
    text = (
        review_claude_local.build_claude_prompt("0", "")
        if reviewer == "claude"
        else review_codex_local.build_prompt("0", "")
    )
    assert PIN_BLOCK in text
    assert CONGRUENCE_BLOCK in text
    assert missing_items(text, REVIEWER_INSTRUCTIONS) == []


def test_examples_carry_no_shared_policy_copy() -> None:
    text = "\n".join(p.read_text(encoding="utf-8") for p in EXAMPLE_PROMPTS)
    assert "CONGRUENCE" not in text.upper()
    assert "## GitHub Actions SHA Pins" not in text
    assert CONGRUENCE not in CONSUMER_PROMPTS
    assert PIN not in CONSUMER_PROMPTS


@pytest.mark.parametrize(
    "missing", ["code-review-system.md", "code-review-checklist.md"]
)
def test_missing_required_example_cannot_pass_as_policy_free(
    missing: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = tuple(tmp_path / path.name for path in EXAMPLE_PROMPTS)
    for path in paths:
        path.write_text("# Repository-specific guidance\n", encoding="utf-8")
    monkeypatch.setitem(globals(), "EXAMPLE_PROMPTS", paths)
    test_examples_carry_no_shared_policy_copy()
    (tmp_path / missing).unlink()
    with pytest.raises(FileNotFoundError):
        test_examples_carry_no_shared_policy_copy()


@pytest.mark.parametrize(
    ("item", "marker"),
    [
        ("pin-section", "## GitHub Actions SHA Pins"),
        ("pin-context", "When the diff pins a SHA"),
        ("pin-verified", 'status="verified"'),
        ("pin-comment-matches-false", 'comment-matches="false"'),
        ("pin-absent-comment-matches", "If the attribute is absent"),
        ("pin-unmatched", 'status="unmatched"'),
        ("pin-repo-not-found", 'error="repo-not-found"'),
        ("pin-repo-not-visible", 'error="repo-not-visible"'),
        ("pin-transient", "with any other `error`"),
    ],
)
def test_semantic_rule_omission_reports_its_stable_id(
    item: str, marker: str, tmp_path: Path
) -> None:
    text = _codex_prompt()
    (rule,) = [line for line in text.splitlines() if marker in line]
    prompt = tmp_path / "base.md"
    prompt.write_text(text.replace(rule, ""), encoding="utf-8")
    result = _run_cli("--control", prompt)
    assert result.returncode == EXIT_MISSING
    assert result.stdout.splitlines() == [item]
