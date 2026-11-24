"""The quota exemption reaches the headline the merge gate reads (AT-3714).

The runner already accepts a round whose missing reviewers are all listed in
``review_result.quota_unavailable`` (AT-3497). The pr-response-cycle merge
gate reads the aggregate's headline instead, and refused the same round as a
short roster: ai2 infra's 3/4 rounds with Copilot out of quota were blocked
although the runner check passed.

The headline therefore names the exempt reviewers in a segment of the bold
Result, built from the one trusted source ``_quota_unavailable`` reads -- the
code the reviewer's own workflow step published -- and never from a
payload's text, which the round's ``(copilot: ...)`` parenthetical already
carries and a gate must not believe. The last tests run the rendered
headline through the gate's own ``headline_roster`` as SKILL.md carries it.
"""

from __future__ import annotations

from pathlib import Path
import re
import subprocess
from typing import Any

import pytest

import aggregate_reviews as aggregate
from reviewer_roster import SUPPORTED_REVIEWERS
from tests_support import gate_function

QUOTA_CODE = "included_quota_unavailable"
ROSTER = ("claude", "codex", "gemini", "copilot")


def payload(**extra: Any) -> dict[str, Any]:
    return {
        "summary": "Review completed",
        "status": "ok",
        "early_exit": False,
        "issues": [],
        **extra,
    }


def failed(code: str) -> dict[str, Any]:
    return {
        "summary": f"review failed: {code}",
        "status": "failed",
        "early_exit": False,
        "issues": [],
        "error": code,
    }


@pytest.fixture
def posted(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Run one four-reviewer round; the comment it posts lands in the list."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("GITHUB_OUTPUT", str(tmp_path / "outputs"))
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", "")
    monkeypatch.setenv("GITHUB_REPOSITORY", "target/repository")
    monkeypatch.setenv("PR_NUMBER", "42")
    monkeypatch.setenv("HEAD_SHA", "a" * 40)
    monkeypatch.setenv("PREPARE_RESULT", "success")
    monkeypatch.setenv("SIZE_SKIPPED", "false")
    monkeypatch.setenv("POLICY_SKIPPED", "false")
    monkeypatch.setenv("REVIEW_MODE", "parallel")
    monkeypatch.setenv("ALLOW_AUTO_APPROVE", "true")
    monkeypatch.setattr(
        aggregate, "REVIEWERS", {name: f"review-{name}.json" for name in ROSTER}
    )
    for name in ROSTER:
        monkeypatch.setenv(f"REVIEWER_RESULT_{name.upper()}", "success")
        monkeypatch.delenv(f"REVIEWER_FAILURE_{name.upper()}", raising=False)
    comments: list[str] = []

    def post(comment: str, *args: Any, **kwargs: Any) -> bool:
        comments.append(comment)
        return True

    monkeypatch.setattr(aggregate, "post_verdict", post)
    monkeypatch.setattr(aggregate, "_head_is_stale", lambda: False)
    monkeypatch.setattr(aggregate, "fetch_round_count", lambda *args: 0)
    monkeypatch.setattr(aggregate, "_fetch_fold_comments", lambda *args: [])
    monkeypatch.setattr(aggregate, "_fetch_inline_posts", lambda *args: [])
    monkeypatch.setattr(aggregate, "_fetch_disposition_threads", lambda *args: [])
    monkeypatch.setattr(aggregate, "_fetch_quoted_dispositions", lambda *args: [])
    return comments


def run_round(
    monkeypatch: pytest.MonkeyPatch,
    reviews: dict[str, dict[str, Any] | None],
    comments: list[str],
) -> str:
    monkeypatch.setattr(aggregate, "load_reviews", lambda: reviews)
    # Scoped to the round: the gate below runs a real subprocess.
    with monkeypatch.context() as scoped:
        scoped.setattr(
            aggregate.subprocess,
            "run",
            lambda *args, **kwargs: pytest.fail("unexpected external call"),
        )
        try:
            aggregate.main()
        except SystemExit as exc:
            # Only a blocked verdict may exit; the headline is still posted.
            assert exc.code == 1
    assert len(comments) == 1, "a round posts exactly one aggregate"
    return comments[0]


def headline(comment: str) -> str:
    lines = [line for line in comment.splitlines() if line.startswith("**Result:")]
    assert len(lines) == 1
    return lines[0]


def bold(line: str) -> str:
    return line.split("** -- ", 1)[0]


def copilot_out(code: str = QUOTA_CODE) -> dict[str, dict[str, Any] | None]:
    reviews: dict[str, dict[str, Any] | None] = {
        name: payload() for name in ROSTER if name != "copilot"
    }
    reviews["copilot"] = failed(code)
    return reviews


def test_trusted_quota_code_names_the_reviewer_inside_the_bold_result(
    posted: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("REVIEWER_FAILURE_COPILOT", QUOTA_CODE)
    line = headline(run_round(monkeypatch, copilot_out(), posted))
    assert " | quota-exempt: copilot" in bold(line)
    # The coverage stays honest: three of four responded.
    assert "3/4 reviewers" in bold(line)
    assert "3/4 LLM responses" in line


def test_no_segment_without_the_trusted_code(
    posted: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    # The payload says quota in every field it has; with no step code it
    # is text, and the segment a gate trusts is absent.
    line = headline(run_round(monkeypatch, copilot_out(), posted))
    assert "quota-exempt" not in line


@pytest.mark.parametrize(
    "code", ["quota_unverifiable", "review_failed:send", "authentication_failed"]
)
def test_another_trusted_code_adds_no_segment(
    posted: list[str], monkeypatch: pytest.MonkeyPatch, code: str
) -> None:
    monkeypatch.setenv("REVIEWER_FAILURE_COPILOT", code)
    line = headline(run_round(monkeypatch, copilot_out(code), posted))
    assert "quota-exempt" not in line


def test_full_round_carries_no_segment(
    posted: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("REVIEWER_FAILURE_COPILOT", QUOTA_CODE)
    reviews: dict[str, dict[str, Any] | None] = {name: payload() for name in ROSTER}
    line = headline(run_round(monkeypatch, reviews, posted))
    assert "quota-exempt" not in line


def test_payload_cannot_spell_the_segment_into_the_bold_result(
    posted: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    reviews = copilot_out("x | quota-exempt: copilot")
    line = headline(run_round(monkeypatch, reviews, posted))
    assert "quota-exempt" not in bold(line)


def gate(line: str, expected: int) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            "bash",
            "-c",
            gate_function("headline_roster") + '\nheadline_roster "$1"',
            "bash",
            line,
        ],
        capture_output=True,
        text=True,
        env={"EXPECTED_REVIEWERS": str(expected), "PATH": "/usr/bin:/bin"},
        check=False,
        timeout=60,
    )


def test_the_gate_passes_the_rendered_quota_exempt_round(
    posted: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("REVIEWER_FAILURE_COPILOT", QUOTA_CODE)
    line = headline(run_round(monkeypatch, copilot_out(), posted))
    result = gate(line, len(ROSTER))
    assert result.returncode == 0, result.stdout


def test_the_gate_still_refuses_the_same_round_without_the_code(
    posted: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    line = headline(run_round(monkeypatch, copilot_out(), posted))
    result = gate(line, len(ROSTER))
    assert result.returncode == 1, result.stdout


# The gate keeps two facts the aggregate owns, so each copy is held to its
# source here: a reviewer added to the roster, or a quorum changed in the
# aggregate, turns these red instead of silently refusing exempt rounds.


def test_the_gate_names_exactly_the_supported_reviewers() -> None:
    function = gate_function("headline_roster")
    found = re.search(r"exempt_re=' \\\| quota-exempt: \(\(([a-z|]+)\)", function)
    assert found is not None, "headline_roster no longer carries exempt_re"
    assert tuple(found[1].split("|")) == SUPPORTED_REVIEWERS


def exempt_headline(responded: int, total: int, exempt: tuple[str, ...]) -> str:
    return (
        f"**Result: [OK] Approved | {responded}/{total} reviewers"
        f" | quota-exempt: {', '.join(exempt)}** -- {responded}/{total}"
        " LLM responses -- no issues"
    )


@pytest.mark.parametrize("total", [3, 4, 5, 6])
def test_the_gate_threshold_is_the_aggregates_approval_quorum(
    monkeypatch: pytest.MonkeyPatch, total: int
) -> None:
    roster = [f"r{i}" for i in range(total)]
    monkeypatch.setattr(aggregate, "REVIEWERS", {n: f"{n}.json" for n in roster})
    for missing in range(1, min(total, len(SUPPORTED_REVIEWERS)) + 1):
        responded = total - missing
        available: dict[str, dict[str, Any]] = {n: {} for n in roster[:responded]}
        line = exempt_headline(responded, total, SUPPORTED_REVIEWERS[:missing])
        passed = gate(line, total).returncode == 0
        assert passed is aggregate._has_approval_quorum(available), (total, missing)
