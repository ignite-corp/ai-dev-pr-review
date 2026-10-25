"""Cross-rendering disposition identity, using actual producers and gate code."""

import json
import subprocess
from types import SimpleNamespace

import pytest

import post_inline_comments
import test_post_inline_comments as folded_tests
from aggregate_reviews import _format_issue_line
from test_gate_headline_drill import gate_function


@pytest.fixture
def findings(monkeypatch):
    issues = [
        dict(
            severity="major",
            file="a.py",
            line=1,
            description="Validate credentials before forwarding",
        ),
        dict(
            severity="major",
            file="a.py",
            line=1,
            description="Close the socket after the retry budget expires",
        ),
    ]
    comments, no_location, out_of_range = post_inline_comments.build_comments(
        issues, {"a.py": {1}}, [], "claude"
    )
    assert len(comments) == 2 and no_location == out_of_range == 0
    captured = []

    def transport(cmd, **kwargs):
        if cmd[:2] == ["gh", "api"]:
            return SimpleNamespace(returncode=0, stdout="0", stderr="")
        captured.append(kwargs["input"])
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    with monkeypatch.context() as scoped:
        scoped.setattr(subprocess, "run", transport)
        post_inline_comments.post_fallback("o/r", "7", "claude", comments)
        post_inline_comments.post_cutoff_summary("o/r", "7", "claude", 14, comments)
    return issues, captured


def major(body, quotes):
    functions = "\n".join(
        gate_function(name)
        for name in ["finding_key", "thread_answers", "major_dispositions"]
    )
    return subprocess.run(
        [
            "bash",
            "-c",
            functions
            + '\nset -o pipefail\nquotes=$(cat)\nmajor_dispositions "$1" 1 "" "$quotes"',
            "bash",
            body,
        ],
        input=quotes,
        text=True,
        capture_output=True,
        timeout=30,
    )


@pytest.mark.parametrize("index", [0, 1])
def test_one_verdict_quote_set_answers_both_verdict_and_folded_surfaces(
    findings, index
):
    issues, bodies = findings
    verdict = [_format_issue_line(issue) for issue in issues]
    reply = (
        bodies[index].splitlines()[0]
        + "\n"
        + "\n".join("> " + line for line in verdict)
    )
    result = folded_tests.TestFoldedFindingGate.run_gate(
        bodies[index], "Dispositions: " + reply
    )
    assert result.returncode == 0, result.stdout + result.stderr
    for line in verdict:
        result = major(line, "> " + line)
        assert result.returncode == 0, result.stdout + result.stderr


def test_folded_quote_also_answers_the_same_verdict_item(findings):
    issues, bodies = findings
    first = next(line for line in bodies[0].splitlines() if line.startswith("- **"))
    result = major(_format_issue_line(issues[0]), "> > " + first)
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize(
    "field,value",
    [
        ("severity", "minor"),
        ("file", "different.py"),
        ("line", 10),
        ("description", "Validate credentials"),
        ("description", "Validate credentials before forwarding then log them"),
    ],
)
def test_key_near_misses_do_not_answer_a_major(findings, field, value):
    issues, bodies = findings
    changed = dict(issues[0], **{field: value})
    result = major(_format_issue_line(issues[0]), "> " + _format_issue_line(changed))
    assert result.returncode == 1, result.stdout + result.stderr
    assert _format_issue_line(issues[0]) in result.stdout
    reply = "Dispositions: inline-fallback-claude\n> " + _format_issue_line(changed)
    reply += "\n> " + _format_issue_line(issues[1])
    folded = folded_tests.TestFoldedFindingGate.run_gate(bodies[0], reply)
    assert folded.returncode == 1, folded.stdout + folded.stderr


@pytest.mark.parametrize(
    "description", ["Missing error handling", "**note** Missing error handling"]
)
def test_pathless_verdict_items_keep_full_description(description):
    body = _format_issue_line(dict(severity="major", description=description))
    assert major(body, "> > " + body).returncode == 0
    assert major(body, "> " + body + " more").returncode == 1


def test_key_multiplicity_is_per_source_not_per_surface(findings):
    issues, bodies = findings
    lines = [line for line in bodies[0].splitlines() if line.startswith("- **")]
    # These are two distinct producer rows whose only renderer difference is
    # reviewer decoration. A normalized key cannot consume one quote twice.
    second = lines[0].replace("(claude)", "(gemini)")
    body = bodies[0].replace(lines[1], second)
    quote = "> " + _format_issue_line(issues[0])
    reply = "Dispositions: inline-fallback-claude\n" + quote
    assert folded_tests.TestFoldedFindingGate.run_gate(body, reply).returncode == 1
    result = folded_tests.TestFoldedFindingGate.run_gate(body, reply + "\n" + quote)
    assert result.returncode == 0, result.stdout + result.stderr


def test_source_binding_and_each_item_remain_required(findings):
    issues, bodies = findings
    quoted = "\n".join("> " + _format_issue_line(issue) for issue in issues)
    assert (
        folded_tests.TestFoldedFindingGate.run_gate(bodies[0], quoted).returncode == 1
    )
    reply = "Dispositions: inline-fallback-claude\n> " + _format_issue_line(issues[0])
    result = folded_tests.TestFoldedFindingGate.run_gate(bodies[0], reply)
    assert result.returncode == 1
    missing = next(line for line in bodies[0].splitlines() if "retry budget" in line)
    assert missing in result.stdout


@pytest.mark.parametrize("location", ["a*b.py:1", "folder with space/a:b.py:12"])
def test_shared_key_preserves_complete_locations(location):
    verdict = f"- + **major** `{location}` -- Read the whole description"
    folded = (
        f"- **{location}** -- + **major** (custom-reviewer): Read the whole description"
    )
    function = gate_function("finding_key")
    keys = []
    for line in [verdict, folded]:
        result = subprocess.run(
            ["bash", "-c", function + '\nfinding_key "$1"', "bash", line],
            text=True,
            capture_output=True,
            check=True,
        )
        keys.append(json.loads(result.stdout))
    assert keys[0] == keys[1] == ["major", location, "Read the whole description"]


def test_early_matching_key_with_large_history_survives_pipefail(findings):
    issues, bodies = findings
    first = next(line for line in bodies[0].splitlines() if line.startswith("- **"))
    quotes = (
        "> "
        + first
        + "\n"
        + "\n".join(
            f"> - - **minor** `unrelated.py:{n}` -- Unrelated finding {n}"
            for n in range(4096)
        )
    )
    result = major(_format_issue_line(issues[0]), quotes)
    assert result.returncode == 0, result.stdout + result.stderr
