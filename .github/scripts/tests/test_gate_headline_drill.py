"""Run gate_headline_drill.sh against the plugin's SKILL.md (AT-2648).

The core pr-response-cycle gate has fourteen shell functions carried inside
plugins/pr-response-cycle/skills/pr-response-cycle/SKILL.md, pasted whole
into a shell on whatever machine runs the gate. The drill extracts them from
that file by name and feeds them the aggregate headlines, review lists and
cutoff comments as they were measured on real PRs, with inversion controls
that cut one clause at a time to show each refusal is the clause's doing.

This wrapper pins the drill's last line, not only its exit status. The drill
exits 0 on any run with no failure and at least one pass, and counts a
missing shellcheck as a skip rather than a failure; a scenario section that
stopped running, or a lint that silently did not happen, would both leave
the exit status green. The check count is pinned to the number the drill
holds today so a drop in coverage is a failing test, not a quieter log.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

SCRIPT_DIR = Path(__file__).resolve().parent.parent
DRILL = SCRIPT_DIR / "gate_headline_drill.sh"
# The one anchor for the plugin SKILL.md path; test_post_inline_comments imports it.
SKILL = (
    SCRIPT_DIR.parents[1]
    / "plugins"
    / "pr-response-cycle"
    / "skills"
    / "pr-response-cycle"
    / "SKILL.md"
)

# The gate functions the drill extracts from SKILL.md, read off the drill's
# own name assignments -- FUNC_NAME= and its *_FN= siblings -- rather than
# transcribed, so a function added to the drill reaches the tests below.
_DRILL_FUNCTION_NAMES = re.compile(r"^(?:FUNC_NAME|[A-Z_]+_FN)=(\w+)$", re.M)
GATE_FUNCTIONS = tuple(_DRILL_FUNCTION_NAMES.findall(DRILL.read_text(encoding="utf-8")))

# How many the drill names today. Update it together with the drill, as
# EXPECTED_CHECKS is: a regex that matched nothing would otherwise hand the
# parametrized test an empty tuple, which pytest skips rather than fails.
EXPECTED_GATE_FUNCTIONS = 14

# Checks the drill runs when every scenario section executes and shellcheck
# is on PATH. Update it together with the drill, never to match a shortfall.
EXPECTED_CHECKS = 746

RESULT_LINE = re.compile(r"^RESULT: (\d+) passed, (\d+) failed, (\d+) skipped$", re.M)

# The drill's extract(), lifted from the drill by name: its definition line
# to the first lone closing brace, the shape it extracts the gate functions
# in. It is run as bash runs it, not transcribed, so there is one encoding
# of how a gate function is carved out of SKILL.md and this is not it.
_EXTRACT_FN = re.compile(r"^extract\(\) \{.*?^\}$", re.M | re.S)


def gate_function(name: str) -> str:
    """The function as gate_headline_drill.sh extracts it: the drill's own extract(), run."""
    lifted = _EXTRACT_FN.search(DRILL.read_text(encoding="utf-8"))
    assert lifted is not None, (
        "the drill no longer defines extract() in the shape this lifts"
    )
    result = subprocess.run(
        [
            "bash",
            "-c",
            lifted.group(0) + '\nextract "$1" "$2"',
            "bash",
            str(SKILL),
            name,
        ],
        capture_output=True,
        text=True,
        check=True,
        timeout=60,
    )
    assert result.stdout.startswith(f"{name}() {{\n"), (
        f"the drill's extract() finds no {name} in SKILL.md: {result.stderr}"
    )
    return result.stdout


def test_drill_anchors_on_the_plugin_skill() -> None:
    """The extraction path in the drill names the plugin copy, not a user-level one."""
    text = DRILL.read_text(encoding="utf-8")
    anchor = re.search(r'^SKILL="\$HERE/(?P<rel>[^"]+)"$', text, re.M)
    assert anchor is not None, "the drill no longer sets SKILL from $HERE"
    assert (SCRIPT_DIR / anchor["rel"]).resolve() == SKILL


def test_gate_functions_are_read_off_the_drill() -> None:
    """The derivation found the drill's whole list: the pinned count, one name per assignment shape."""
    assert len(GATE_FUNCTIONS) == EXPECTED_GATE_FUNCTIONS, GATE_FUNCTIONS
    assert len(set(GATE_FUNCTIONS)) == len(GATE_FUNCTIONS), GATE_FUNCTIONS
    # Positive controls: the FUNC_NAME= assignment and one *_FN= assignment.
    assert "headline_roster" in GATE_FUNCTIONS
    assert "review_workflow_known" in GATE_FUNCTIONS


def test_skill_defines_each_gate_function_once() -> None:
    """Every function the drill extracts is defined exactly once, in the shape it extracts."""
    text = SKILL.read_text(encoding="utf-8")
    counts = {
        fn: len(re.findall(rf"^{fn}\(\) \{{$", text, re.M)) for fn in GATE_FUNCTIONS
    }
    assert counts == {fn: 1 for fn in GATE_FUNCTIONS}


@pytest.mark.parametrize("name", GATE_FUNCTIONS)
def test_gate_function_is_one_whole_function(name: str) -> None:
    """The drill's extract() yields each function from its definition line to a lone brace.

    This cannot see a truncation: the sed range stops at the first lone
    brace, so what it yields ends in exactly one whether or not that brace
    was the function's own. The truncation guard is the drill's section 1,
    one tail-line assertion per function, which the drill run below counts.
    What this does see is a function the range never closes: sed then
    prints to the end of SKILL.md, which is prose, not a brace.
    """
    text = gate_function(name)
    assert text.endswith("\n}\n"), text[-80:]


def test_drill_passes_every_check() -> None:
    """EXPECTED_CHECKS passed, 0 failed, 0 skipped -- the count, not only the exit status."""
    result = subprocess.run(
        ["bash", str(DRILL)],
        capture_output=True,
        text=True,
        timeout=600,
    )
    tail = "\n".join(result.stdout.splitlines()[-40:])
    assert result.returncode == 0, (
        f"drill exited {result.returncode}\n{tail}\n{result.stderr}"
    )
    failures = [
        line for line in result.stdout.splitlines() if line.startswith("  FAIL")
    ]
    assert not failures, "\n".join(failures)
    match = RESULT_LINE.search(result.stdout)
    assert match is not None, f"no RESULT line\n{tail}"
    passed, failed, skipped = (int(n) for n in match.groups())
    assert shutil.which("shellcheck"), (
        "shellcheck must be on PATH: the drill counts its absence as a skip, not a failure"
    )
    assert (passed, failed, skipped) == (EXPECTED_CHECKS, 0, 0), match.group(0)
