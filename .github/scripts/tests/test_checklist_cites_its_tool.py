"""The prompts' function-size rule names the tool that measures its number.

`examples/prompts/code-review-checklist.md` said "No functions exceeding 80
lines (ruff PLR0915)", and the system prompt called the same limit "enforced
by ruff PLR0915". PLR0915 is `too-many-statements`: it counts statements,
default max 50, and does not measure lines at all. On PR #172 it passed a
116-line, 36-statement function while the line limit was cited twice and
resolved twice (AT-2420). The rule now cites the tool for the number it
enforces, keeps the 80-line bound beside it as the reviewer's, and this
module keeps the citation from drifting again: the
number in the prompt is read from the prompt, and the number the tool
enforces is read from the tool, by running it.

Both prompt files are consumer-deployed templates. Nothing here reaches a
consumer's CI; what it pins is that the sentence they are handed is true of
the tool it names.
"""

from __future__ import annotations

import functools
import json
import re
from pathlib import Path

import pytest

from tests_support import ran

ROOT = Path(__file__).resolve().parents[3]
PROMPTS = (
    ROOT / "examples/prompts/code-review-checklist.md",
    ROOT / "examples/prompts/code-review-system.md",
)
CITATION = re.compile(r"functions exceeding (\d+) (\w+) \(ruff PLR0915\)")
FINDING = re.compile(r"Too many statements \((\d+) > (\d+)\)")
# Large enough to trip any threshold a configuration would plausibly set, so
# the number parsed out of the finding is the threshold and not the probe.
PROBE_STATEMENTS = 500


def cited_limits(text: str) -> list[tuple[int, str]]:
    """Every (number, unit) a prompt attaches to the PLR0915 citation."""
    return [(int(n), unit) for n, unit in CITATION.findall(text)]


def mismatches(text: str, enforced: int) -> list[tuple[int, str]]:
    """The citations in `text` that PLR0915 does not measure."""
    return [limit for limit in cited_limits(text) if limit != (enforced, "statements")]


def probe(statements: int) -> str:
    body = "".join(f"    x{i} = {i}\n" for i in range(statements))
    return f"def probe():\n{body}"


def ruff(source: str, *flags: str) -> re.Match[str] | None:
    """Run ruff on `source` as if it were a file under .github/scripts and
    return its PLR0915 finding, parsed, or None when it raised none.

    The repository's own configuration applies unless `--isolated` is passed,
    which is what lets a test tell "selected by this repository" apart from
    "selected on request". JSON output rather than text: a FORCE_COLOR in
    the environment puts escape codes into the text formats.
    """
    completed = ran(
        "ruff",
        [
            "check",
            "--stdin-filename",
            ".github/scripts/probe.py",
            "--output-format",
            "json",
            "--no-cache",
            *flags,
            "-",
        ],
        cwd=ROOT,
        input=source,
    )
    for finding in json.loads(completed.stdout):
        if finding["code"] == "PLR0915":
            return FINDING.search(finding["message"])
    return None


@functools.cache
def enforced_statement_limit() -> int:
    """The threshold PLR0915 applies under this repository's configuration.

    Cached: the configuration and the binary are both fixed for the
    session, and four of this module's tests ask for it.
    """
    found = ruff(probe(PROBE_STATEMENTS))
    assert found, "PLR0915 produced no finding under the repository config"
    return int(found.group(2))


# ------------------------------------------------------- the prompts' number


@pytest.mark.parametrize("prompt", PROMPTS, ids=lambda p: p.name)
def test_every_citation_of_plr0915_states_the_number_it_enforces(
    prompt: Path,
) -> None:
    """Deny by default: a prompt with no citation fails too, because a test
    that passes on an empty match list is the shape this ticket is about."""
    text = prompt.read_text(encoding="utf-8")
    assert cited_limits(text), f"{prompt.name} no longer cites PLR0915"
    assert mismatches(text, enforced_statement_limit()) == []


def test_the_line_the_ticket_quotes_is_refused() -> None:
    """Inversion: the sentence that shipped must fail the check above."""
    enforced = enforced_statement_limit()
    shipped = "- [ ] No functions exceeding 80 lines (ruff PLR0915)"
    assert mismatches(shipped, enforced) == [(80, "lines")]
    # The unit alone is not enough: a statement count the tool does not
    # apply is refused as well.
    wrong_number = f"No functions exceeding {enforced + 10} statements (ruff PLR0915)"
    assert mismatches(wrong_number, enforced) == [(enforced + 10, "statements")]
    # And the accepted form is accepted by the same function.
    assert (
        mismatches(
            f"No functions exceeding {enforced} statements (ruff PLR0915)", enforced
        )
        == []
    )


# ------------------------------------------------------- the tool's number


def test_the_parsed_threshold_is_the_boundary_the_tool_applies() -> None:
    """The number came out of one finding; this checks it is the boundary.

    A function of exactly that many statements passes and one more fails,
    so the prompt's number is the tool's number and not a coincidence of
    the probe size.
    """
    enforced = enforced_statement_limit()
    assert ruff(probe(enforced)) is None
    over = ruff(probe(enforced + 1))
    assert over and over.groups() == (str(enforced + 1), str(enforced))


def test_plr0915_is_selected_by_the_repository_not_only_on_request() -> None:
    """PLR0915 is not in ruff's default rule set. Without the repository's
    `extend-select` the citation would again name a tool that runs nothing.

    `--isolated` drops the repository configuration and is the control: the
    same probe produces no finding there, so a finding under the project
    config is the configuration's doing.
    """
    assert ruff(probe(PROBE_STATEMENTS))
    assert ruff(probe(PROBE_STATEMENTS), "--isolated") is None


def test_a_ruff_that_could_not_run_fails_with_its_own_message() -> None:
    """Exit 2 with empty stdout used to surface as a bare JSONDecodeError,
    which made a configuration mistake look like a defect in this module."""
    with pytest.raises(
        pytest.fail.Exception, match=r"(?s)ruff exited 2: .*nonexistent\.toml"
    ):
        ruff(probe(1), "--config", "/nonexistent.toml")
