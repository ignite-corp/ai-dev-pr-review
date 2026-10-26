"""Tests for the body/source cross-check (AT-2870).

The deliberately-wrong bodies are the point of this file. Each of the four
dimensions gets one fixture that is wrong in EXACTLY one way: every fixture is
the shared correct body plus or minus a single edit, and the test asserts both
that it produces one finding and that the finding is its own dimension. A
fixture failing for two reasons at once would prove nothing about either, so
the count is asserted, not just the dimension.

``test_a_text_search_would_have_passed_the_accuracy_fixture`` is the
regression this tool exists for: the quoted string IS present in the source
file's text, inside a comment, and the old ``phrase in source_file_text``
checker passed it.

Every document below is synthetic.
"""

from __future__ import annotations

import difflib
import io
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import check_body_claims
import pytest
from check_body_claims import (
    ACCURACY,
    COMPLETENESS,
    NEGATIVE,
    PROVENANCE,
    Claim,
    Finding,
    audit,
    claims,
    emittable_strings,
    load_phrases,
    main,
    scope_to_change,
    supported_by_source,
)

SCRIPT = Path(check_body_claims.__file__)

SOURCE = '''"""Report a review verdict. This docstring is documentation, not output."""

# Dropped in this change: the old "verdict: no findings at all" wording.
LABEL = "verdict"


def report(count):
    """Print the round's verdict. This docstring is documentation too."""
    if count:
        print(f"{LABEL}: BLOCKED on {count} findings")
    else:
        print("verdict: APPROVED")
'''

# The same module before the change that added the clean-round branch.
BASELINE_SOURCE = '''"""Report a review verdict. This docstring is documentation, not output."""

LABEL = "verdict"


def report(count):
    """Print the round's verdict. This docstring is documentation too."""
    if count:
        print(f"{LABEL}: BLOCKED on {count} findings")
'''

BODY = """Report a review verdict (AT-2870).

With findings the script emits: `verdict: BLOCKED on 3 findings`.
With a clean round it emits: `verdict: APPROVED`.

The round measured: `ruff: 2 errors fixed` while this was written.
"""

MEASURED = """$ ruff check .github/scripts
ruff: 2 errors fixed
"""

RETRACTED = """# Phrases withdrawn during the round; none may survive in the body.
every reviewer agreed
"""

# The one edit each fixture makes to BODY.
ACCURACY_EDIT = (
    "\nIt also emits: `verdict: no findings at all` when the round is empty.\n"
)
COMPLETENESS_EDIT = "With a clean round it emits: `verdict: APPROVED`.\n"
NEGATIVE_EDIT = "\nAll three rounds ran and every reviewer agreed on the wording.\n"
PROVENANCE_EDIT = "\nThe gate also emits: `ruff: 2 errors fixed`.\n"


@dataclass(frozen=True)
class Fixture:
    """A body wrong in exactly one dimension, and the body that fixes it."""

    dimension: str
    wrong: str
    fixed: str = BODY


FIXTURES = (
    # Quotes a string that exists in the source only inside a comment.
    Fixture(ACCURACY, BODY + ACCURACY_EDIT),
    # Drops the sentence that was naming one of the emittable strings.
    Fixture(COMPLETENESS, BODY.replace(COMPLETENESS_EDIT, "")),
    # Still carries a premise the round withdrew.
    Fixture(NEGATIVE, BODY + NEGATIVE_EDIT),
    # Credits our code with a line only the tool transcript holds.
    Fixture(PROVENANCE, BODY + PROVENANCE_EDIT),
)


def run(body: str, source: str = SOURCE, baseline: str | None = None) -> list[Finding]:
    emittable = emittable_strings(source)
    before = emittable_strings(baseline) if baseline is not None else []
    return audit(
        body=body,
        emittable=emittable,
        completeness_scope=scope_to_change(emittable, before),
        measured=MEASURED,
        retracted=load_phrases(RETRACTED),
    )


# ---------------------------------------------------------------------------
# The red fixtures: one dimension each
# ---------------------------------------------------------------------------


def test_the_correct_body_is_clean() -> None:
    """The control. Without it a fixture's finding could be background noise."""
    assert run(BODY) == []


@pytest.mark.parametrize("fixture", FIXTURES, ids=[f.dimension for f in FIXTURES])
def test_each_fixture_fails_for_exactly_its_own_dimension(fixture: Fixture) -> None:
    findings = run(fixture.wrong)
    assert [f.dimension for f in findings] == [fixture.dimension], [
        str(f) for f in findings
    ]


@pytest.mark.parametrize("fixture", FIXTURES, ids=[f.dimension for f in FIXTURES])
def test_each_fixture_is_clean_once_fixed(fixture: Fixture) -> None:
    assert run(fixture.fixed) == []


@pytest.mark.parametrize("fixture", FIXTURES, ids=[f.dimension for f in FIXTURES])
def test_each_fixture_differs_from_the_control_by_one_edit(fixture: Fixture) -> None:
    """A fixture that rewrote the body wholesale would isolate nothing."""
    opcodes = difflib.SequenceMatcher(
        None, fixture.fixed.split("\n"), fixture.wrong.split("\n")
    ).get_opcodes()
    changed = [op for op in opcodes if op[0] != "equal"]
    assert len(changed) == 1, changed


def test_a_text_search_would_have_passed_the_accuracy_fixture() -> None:
    """The false pass this tool was rewritten to stop (AT-2870)."""
    phrase = "verdict: no findings at all"
    assert phrase in SOURCE, "the fixture no longer reproduces the comment case"
    assert phrase not in emittable_strings(SOURCE)
    assert [f.dimension for f in run(BODY + ACCURACY_EDIT)] == [ACCURACY]


# ---------------------------------------------------------------------------
# emittable_strings
# ---------------------------------------------------------------------------


def test_a_string_only_a_comment_holds_is_not_emittable() -> None:
    source = '# print("removed wording")\nprint("kept wording")\n'
    assert emittable_strings(source) == ["kept wording"]


def test_docstrings_are_not_emittable() -> None:
    source = '"""Module doc."""\n\n\nclass C:\n    """Class doc."""\n\n    def m(self):\n        """Method doc."""\n        return "value"\n'
    assert emittable_strings(source) == ["value"]


def test_a_string_statement_that_is_not_the_first_one_is_emittable() -> None:
    """Only the leading constant is documentation; a later one is a value."""
    source = '"""Module doc."""\nx = 1\n"not a docstring"\n'
    assert emittable_strings(source) == ["not a docstring"]


def test_f_string_literal_segments_are_emittable_and_holes_are_not() -> None:
    source = 'print(f"{label}: BLOCKED on {count} findings")\n'
    assert emittable_strings(source) == [" findings", ": BLOCKED on "]


def test_output_is_sorted_and_deduplicated() -> None:
    source = 'print("b")\nprint("a")\nprint("b")\n'
    assert emittable_strings(source) == ["a", "b"]


def test_whitespace_only_literals_are_dropped() -> None:
    """A separator carries no claim, so demanding the body name it is noise."""
    assert emittable_strings('print("\\n")\nprint("")\nprint("x")\n') == ["x"]


def test_adjacent_literals_are_folded_by_the_parser_but_a_plus_is_not() -> None:
    assert emittable_strings('print("one " "two")\n') == ["one two"]
    assert emittable_strings('print("one " + "two")\n') == ["one ", "two"]


def test_an_unparsable_source_raises_rather_than_reporting_nothing() -> None:
    with pytest.raises(SyntaxError):
        emittable_strings("def broken(:\n")


# ---------------------------------------------------------------------------
# claims and support
# ---------------------------------------------------------------------------


def test_only_marked_spans_are_read_as_claims() -> None:
    body = "Run `check_body_claims.py --body -`; it emits: `verdict: APPROVED`."
    assert claims(body) == [Claim(kind="emits", text="verdict: APPROVED")]


def test_a_marker_inside_a_longer_word_is_not_a_marker() -> None:
    assert claims("the module reemits: `verdict: APPROVED`") == []


def test_both_markers_are_the_module_constants() -> None:
    """The pattern is built from them, so the two spellings cannot drift."""
    emits, measured = check_body_claims.EMITS, check_body_claims.MEASURED
    body = f"it {emits}: `verdict: APPROVED`; the round {measured}: `x`"
    assert [c.kind for c in claims(body)] == [emits, measured]


def test_a_claim_wrapped_across_lines_still_matches() -> None:
    body = "it emits: `verdict: BLOCKED\non 3 findings`"
    assert claims(body) == [Claim(kind="emits", text="verdict: BLOCKED on 3 findings")]
    assert (
        run(
            BODY.replace(
                "`verdict: BLOCKED on 3 findings`", "`verdict:\nBLOCKED on 3 findings`"
            )
        )
        == []
    )


def test_a_claim_is_supported_when_every_letter_traces_to_a_literal() -> None:
    emittable = emittable_strings(SOURCE)
    assert supported_by_source("verdict: BLOCKED on 3 findings", emittable)
    assert not supported_by_source("verdict: SKIPPED by policy", emittable)


def test_known_limitation_short_literals_can_cover_a_claim_by_accident() -> None:
    # KNOWN LIMITATION, pinned so it cannot change silently: removal is
    # textual, so one- and two-letter literals cover letters they never
    # produced together. A claim with no letters at all is covered by nothing.
    assert supported_by_source("ab", emittable_strings('print("a")\nprint("b")\n'))
    assert supported_by_source("3", [])


# ---------------------------------------------------------------------------
# provenance, both directions
# ---------------------------------------------------------------------------


def test_a_measured_claim_the_source_itself_emits_is_a_provenance_finding() -> None:
    findings = run(BODY + "\nThe log measured: `verdict: APPROVED`.\n")
    assert [f.dimension for f in findings] == [PROVENANCE]


def test_a_measured_claim_supported_by_nothing_is_an_accuracy_finding() -> None:
    findings = run(BODY + "\nThe log measured: `pyright: 4 errors`.\n")
    assert [f.dimension for f in findings] == [ACCURACY]


def test_without_a_transcript_an_unsupported_emits_claim_is_accuracy() -> None:
    """Provenance needs the other side's evidence; absent it, nothing is split."""
    findings = audit(
        body=BODY + PROVENANCE_EDIT,
        emittable=emittable_strings(SOURCE),
        completeness_scope=[],
        measured="",
    )
    assert [f.dimension for f in findings] == [ACCURACY, ACCURACY]


# ---------------------------------------------------------------------------
# completeness scope
# ---------------------------------------------------------------------------


def test_a_baseline_scopes_completeness_to_what_the_change_added() -> None:
    body = "The change adds a clean-round branch to the report helper."
    scoped = run(body, baseline=BASELINE_SOURCE)
    assert [str(f) for f in scoped] == [
        'completeness: the body never names the emittable string "verdict: APPROVED"'
    ]
    # Without a baseline the same body also owes the strings it did not touch.
    assert len(run(body)) == len(emittable_strings(SOURCE)) == 4


def test_a_multi_line_literal_is_reported_on_one_line() -> None:
    """Output is one finding per line, so the message carries the normalized form."""
    source = 'x = 1\nprint("""first line\n    second line""")\n'
    findings = run("The body names nothing.", source=source)
    assert [str(f) for f in findings] == [
        "completeness: the body never names the emittable string "
        '"first line second line"'
    ]


def test_a_baseline_differing_only_in_whitespace_scopes_the_string_out(
    tmp_path: Path,
) -> None:
    """Completeness compares normalized strings, so the scoping must too."""
    body = "The body names nothing."
    source = 'print("verdict:  APPROVED")\n'
    baseline = 'print("verdict: APPROVED")\n'
    argv = [
        "--body",
        _write(tmp_path, "body.md", body),
        "--source",
        _write(tmp_path, "source.py", source),
        "--baseline",
        _write(tmp_path, "baseline.py", baseline),
    ]
    assert main(argv) == 0
    assert run(body, source=source, baseline=baseline) == []


def test_completeness_is_satisfied_by_a_rendered_line_covering_the_segments() -> None:
    """A body quoting the rendered f-string names its segments as substrings."""
    source = 'print(f"{label}: BLOCKED on {count} findings")\n'
    assert run("it reads verdict: BLOCKED on 3 findings", source=source) == []


# ---------------------------------------------------------------------------
# the negative list
# ---------------------------------------------------------------------------


def test_the_phrase_list_skips_comments_and_blank_lines() -> None:
    assert load_phrases("# a comment\n\n  every reviewer agreed  \n") == [
        "every reviewer agreed"
    ]


def test_a_retracted_phrase_is_matched_in_normalized_form() -> None:
    """audit() is public; a caller may hand it phrases load_phrases never saw."""
    findings = audit(
        body=BODY + NEGATIVE_EDIT,
        emittable=emittable_strings(SOURCE),
        completeness_scope=[],
        measured=MEASURED,
        retracted=["every  reviewer\nagreed"],
    )
    assert [str(f) for f in findings] == [
        'negative: the body still carries the retracted phrase "every reviewer agreed"'
    ]


def test_a_retracted_phrase_is_caught_at_the_start_of_a_sentence() -> None:
    body = BODY + "\nEvery reviewer agreed on the wording.\n"
    assert [str(f) for f in run(body)] == [
        'negative: the body still carries the retracted phrase "every reviewer agreed"'
    ]


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------


def _write(tmp_path: Path, name: str, text: str) -> str:
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return str(path)


def _argv(tmp_path: Path, body: str) -> list[str]:
    return [
        "--body",
        _write(tmp_path, "body.md", body),
        "--source",
        _write(tmp_path, "source.py", SOURCE),
        "--measured",
        _write(tmp_path, "measured.txt", MEASURED),
        "--retracted",
        _write(tmp_path, "retracted.txt", RETRACTED),
    ]


def test_main_exits_zero_and_prints_nothing_for_a_clean_body(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(_argv(tmp_path, BODY)) == 0
    assert capsys.readouterr().out == ""


def test_main_exits_one_and_prints_one_line_per_finding(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(_argv(tmp_path, BODY + ACCURACY_EDIT)) == 1
    out = capsys.readouterr().out.splitlines()
    assert len(out) == 1
    assert out[0].startswith(f"{ACCURACY}: ")
    assert "verdict: no findings at all" in out[0]


def test_main_reads_the_body_from_stdin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("sys.stdin", io.StringIO(BODY))
    argv = _argv(tmp_path, BODY)
    argv[argv.index("--body") + 1] = "-"
    assert main(argv) == 0


def test_main_rejects_a_source_that_does_not_parse(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    argv = _argv(tmp_path, BODY)
    argv[argv.index("--source") + 1] = _write(tmp_path, "broken.py", "def broken(:\n")
    assert main(argv) == 2
    assert "does not parse" in capsys.readouterr().err


def test_main_rejects_a_missing_input(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    argv = _argv(tmp_path, BODY)
    argv[argv.index("--body") + 1] = str(tmp_path / "absent.md")
    assert main(argv) == 2
    assert "cannot read input" in capsys.readouterr().err


def test_help_prints_the_docstring_with_its_headings_intact(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The docstring advertises --help as the way to read it, so the headings
    have to survive the formatter rather than collapse into one paragraph."""
    with pytest.raises(SystemExit) as exit_info:
        main(["--help"])
    assert exit_info.value.code == 0
    lines = capsys.readouterr().out.splitlines()
    assert "THE FOUR DIMENSIONS" in lines
    assert "BODY CLAIM SYNTAX" in lines


def test_main_rejects_an_input_that_is_not_utf8(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A decode failure is a ValueError, and the contract promises exit 2."""
    argv = _argv(tmp_path, BODY)
    undecodable = tmp_path / "undecodable.md"
    undecodable.write_bytes(b"\xff\xfe not utf-8\n")
    argv[argv.index("--body") + 1] = str(undecodable)
    assert main(argv) == 2
    assert "cannot read input" in capsys.readouterr().err


def test_main_rejects_a_source_containing_a_null_byte(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    argv = _argv(tmp_path, BODY)
    argv[argv.index("--source") + 1] = _write(tmp_path, "nul.py", 'print("x")\n\x00')
    assert main(argv) == 2
    assert "does not parse" in capsys.readouterr().err


def test_main_rejects_a_parse_that_raises_a_value_error(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Which exception ast.parse picks for a null byte varies by interpreter;
    on those that pick ValueError the test above would otherwise go uncaught."""

    def refuse(*args: object, **kwargs: object) -> object:
        raise ValueError("source code string cannot contain null bytes")

    monkeypatch.setattr(check_body_claims.ast, "parse", refuse)
    assert main(_argv(tmp_path, BODY)) == 2
    assert "does not parse" in capsys.readouterr().err


def test_the_script_runs_as_a_command(tmp_path: Path) -> None:
    proc = subprocess.run(
        [sys.executable, str(SCRIPT)] + _argv(tmp_path, BODY + NEGATIVE_EDIT),
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert proc.returncode == 1
    assert proc.stdout == (
        'negative: the body still carries the retracted phrase "every reviewer agreed"\n'
    )
