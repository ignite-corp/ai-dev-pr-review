"""The reviewers' line numbers are a contract, and this is what checks it.

Three of four findings in the only real end-to-end run were unusable because
of their coordinates. The four descriptions are preserved under
docs/tasks/local-review-driver-design-record/evidence/reviewer-output/ and
are read here rather than paraphrased, so the token extraction is exercised
on what the models actually wrote.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

SCRIPT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SCRIPT_DIR))

from review_coordinates import (  # noqa: E402
    EVIDENCE_WINDOW,
    MIN_TOKEN_LEN,
    _source_lines,
    check_reviewer_coordinates,
    diff_offset_index,
    evidence_lines,
    # The binding review_coordinates itself uses, so a test that compares
    # the two indexes compares the pair the module actually pairs.
    parse_diff,
    quoted_tokens,
)

EVIDENCE = (
    SCRIPT_DIR.parent.parent
    / "docs"
    / "tasks"
    / "local-review-driver-design-record"
    / "evidence"
    / "reviewer-output"
)
SOURCE = "src/app.py"


def make_tree(tmp_path: Path, source_lines: list[str], diff: str) -> Path:
    (tmp_path / "src").mkdir(parents=True, exist_ok=True)
    (tmp_path / SOURCE).write_text("\n".join(source_lines) + "\n", encoding="utf-8")
    (tmp_path / "pr.diff").write_text(diff, encoding="utf-8")
    return tmp_path


def all_additions_diff(source_lines: list[str]) -> str:
    """A diff that adds the whole file -- the shape with no discrimination.

    Every line is "in the diff" here, so post_inline_comments.py's range
    check passes everything. That is not a contrived case: it is the shape
    of the PR the real run reviewed, and the reason the range check let a
    wrong line through.
    """
    body = "".join(f"+{line}\n" for line in source_lines)
    return (
        f"diff --git a/{SOURCE} b/{SOURCE}\n"
        "new file mode 100644\n"
        "--- /dev/null\n"
        f"+++ b/{SOURCE}\n"
        f"@@ -0,0 +1,{len(source_lines)} @@\n" + body
    )


def write_verdict(tmp_path: Path, reviewer: str, issues: list[dict]) -> Path:
    path = tmp_path / f"review-{reviewer}.json"
    path.write_text(
        json.dumps(
            {"summary": "s", "status": "ok", "early_exit": False, "issues": issues}
        ),
        encoding="utf-8",
    )
    return path


def issue(line, description, path=SOURCE, severity="minor"):
    return {
        "severity": severity,
        "file": path,
        "line": line,
        "description": description,
        "suggestion": None,
    }


# --------------------------------------------------------------- diff index


def test_the_offset_index_maps_added_lines_to_their_file_line():
    source = [f"line_{n}" for n in range(1, 6)]
    index = diff_offset_index(all_additions_diff(source))
    # Offsets 1-5 are the diff's own header lines; the body starts at 6.
    assert index[6] == (SOURCE, 1)
    assert index[10] == (SOURCE, 5)


def test_a_content_line_that_renders_as_a_file_header_opens_no_file():
    """A source line reading `++ b/x` arrives in the diff as `+++ b/x`.

    Baseline (84d7933): _DIFF_FILE_RE was tried on every line, so that
    content line reset the path mid-hunk and every offset after it in the
    file was either unmapped or mapped under the wrong path -- silently
    wrong rescues for any PR touching such a file.
    """
    diff = (
        f"diff --git a/{SOURCE} b/{SOURCE}\n"
        "new file mode 100644\n"
        "--- /dev/null\n"
        f"+++ b/{SOURCE}\n"
        "@@ -0,0 +1,4 @@\n"
        "+line_1\n"
        "+++ b/evil/path\n"
        "+line_3\n"
        "+line_4\n"
    )

    index = diff_offset_index(diff)

    assert index[7] == (SOURCE, 2)
    assert index[9] == (SOURCE, 4)
    assert not [entry for entry in index.values() if entry[0] != SOURCE]


def test_a_content_line_that_renders_as_a_file_header_opens_no_file_either_way():
    """The guard above only closed half of this, and the halves are compared.

    check_reviewer_coordinates builds both indexes from the same text and
    reads them against each other -- `in_range` from parse_diff, the rescue
    it falls back to from diff_offset_index. Baseline (4851510):
    diff_offset_index skipped the in-hunk `+++ b/` and parse_diff took it as
    a header, so parse_diff opened `evil/path`, reset its right-side counter
    and reported {1} for SOURCE where the other index reported four lines --
    the in-range test then failed for lines the rescue had mapped correctly.
    """
    diff = (
        f"diff --git a/{SOURCE} b/{SOURCE}\n"
        "new file mode 100644\n"
        "--- /dev/null\n"
        f"+++ b/{SOURCE}\n"
        "@@ -0,0 +1,4 @@\n"
        "+line_1\n"
        "+++ b/evil/path\n"
        "+line_3\n"
        "+line_4\n"
    )

    valid = parse_diff(diff)

    assert valid[SOURCE] == {1, 2, 3, 4}
    assert "evil/path" not in valid
    assert {line for _, line in diff_offset_index(diff).values()} == valid[SOURCE]


def test_a_bare_empty_context_line_counts_the_same_in_both_indexes():
    """check_reviewer_coordinates compares the two directly.

    `valid_lines = parse_diff(diff_text)` decides in_range and
    `offsets = diff_offset_index(diff_text)` decides what a rescue maps to,
    both from the same text. Baseline (84d7933): diff_offset_index counted a
    bare empty line as a right-side line and parse_diff did not, so a
    pr.diff whose blank context lines lost their leading space -- the usual
    whitespace-stripping in transit -- drifted the two apart by one per
    blank line, and they then disagreed about the same file.
    """
    diff = (
        f"diff --git a/{SOURCE} b/{SOURCE}\n"
        f"--- a/{SOURCE}\n"
        f"+++ b/{SOURCE}\n"
        "@@ -1,3 +1,4 @@\n"
        " first\n"
        "\n"
        "+added\n"
        " last\n"
    )

    offsets = diff_offset_index(diff)
    valid = parse_diff(diff)

    assert valid[SOURCE] == {1, 2, 3, 4}
    assert {line for _, line in offsets.values()} == valid[SOURCE]
    assert offsets[8] == (SOURCE, 4)


def test_the_second_file_in_a_diff_still_opens():
    """The guard above is only sound because `diff --git` closes the hunk;
    without that, every file after the first would be unreachable.

    Asserted on BOTH indexes, because that is the whole of what the two
    docstrings now claim: the git-format marker, and nothing weaker, is what
    reopens the header window. A plain `diff -u` over several files has no
    such marker and is out of scope for both.
    """
    other = "src/other.py"
    diff = (
        f"diff --git a/{SOURCE} b/{SOURCE}\n"
        f"--- a/{SOURCE}\n"
        f"+++ b/{SOURCE}\n"
        "@@ -1,1 +1,1 @@\n"
        "+first\n"
        f"diff --git a/{other} b/{other}\n"
        f"--- a/{other}\n"
        f"+++ b/{other}\n"
        "@@ -1,1 +1,2 @@\n"
        "+second\n"
    )

    index = diff_offset_index(diff)
    valid = parse_diff(diff)

    assert index[5] == (SOURCE, 1)
    assert index[10] == (other, 1)
    assert valid == {SOURCE: {1}, other: {1}}


def test_removed_lines_get_no_file_coordinate():
    diff = (
        f"diff --git a/{SOURCE} b/{SOURCE}\n"
        f"--- a/{SOURCE}\n"
        f"+++ b/{SOURCE}\n"
        "@@ -1,3 +1,2 @@\n"
        " kept\n"
        "-dropped\n"
        "+added\n"
    )
    index = diff_offset_index(diff)
    lines = {offset: mapped[1] for offset, mapped in index.items()}
    assert lines == {5: 1, 7: 2}, lines


# ------------------------------------------------------------ token pulling


@pytest.mark.parametrize(
    "description, expected",
    [
        ("calls `git show` on the base", {"show"}),
        ("`extract_from_log()` is unbounded", {"extract_from_log"}),
        ("no code quoted at all", set()),
        # Below MIN_TOKEN_LEN: these match everywhere and would pass anything.
        ("`if` and `os` and `id`", set()),
        ("`a.b.c_name` dotted", {"c_name"}),
        # Model-authored and unvalidated upstream: `re.findall` raises
        # TypeError on each of these, and the raise escapes the whole
        # review stage. Not a string quotes nothing.
        (None, set()),
        (["a", "list"], set()),
        (42, set()),
        ({"nested": "object"}, set()),
    ],
)
def test_quoted_tokens_keeps_only_identifiers_worth_matching(description, expected):
    assert quoted_tokens(description) == expected


def test_the_minimum_token_length_is_what_lets_the_shortest_real_one_through():
    """`show` carried codex's finding on its own, so the bound is not free."""
    assert MIN_TOKEN_LEN <= len("show")


# ------------------------------------------------------------------ rescue


def test_a_diff_offset_is_converted_to_the_file_line_it_named(tmp_path, capsys):
    # The diff carries five header lines, so file line N is offset N + 5.
    # The cited offset has to exceed the file's own length for rescue to be
    # reachable at all -- which is exactly how the two real cases looked, a
    # 2268 against 1687 lines and a 654 against 365.
    source = [f"filler_{n}" for n in range(1, 11)]
    source[7] = "def target_function():"
    work = make_tree(tmp_path, source, all_additions_diff(source))
    write_verdict(tmp_path, "claude", [issue(13, "`target_function` is wrong")])

    counts = check_reviewer_coordinates(work, "claude")

    assert counts["rescued"] == 1
    posted = json.loads((tmp_path / "review-claude.json").read_text())
    assert posted["issues"][0]["line"] == 8
    assert "is a pr.diff offset" in capsys.readouterr().out


def test_rescue_does_not_fire_for_a_line_that_is_already_in_range(tmp_path):
    """The gate is load-bearing, not an optimisation.

    Measured on the preserved run: gemini's wrong-but-in-range 1240 ALSO
    mapped to a valid diff offset, for line 472 of the same file -- which
    was `strip_agent_config`, no closer to the `review_pr` it was
    discussing. Ungated, rescue moves one wrong line to a different wrong
    line and then has its own output to check.
    """
    source = [f"filler_{n}" for n in range(1, 40)]
    source[29] = "def target_function():"
    work = make_tree(tmp_path, source, all_additions_diff(source))
    # Line 8 is in range AND is a valid diff offset (mapping to line 3).
    write_verdict(tmp_path, "gemini", [issue(8, "`target_function` is wrong")])

    counts = check_reviewer_coordinates(work, "gemini")

    assert counts["rescued"] == 0
    assert counts["mislocated"] == 1
    assert json.loads((tmp_path / "review-gemini.json").read_text())["issues"][0][
        "line"
    ] is None


def test_a_line_that_is_no_offset_is_not_rescued_but_is_still_screened(tmp_path):
    """Rescue declines it; the evidence half still gets to speak.

    Skipping the rescue is not skipping the issue. Before this was fixed the
    two were the same `continue`, so an out-of-range citation that happened
    not to be a diff offset was neither rescued, checked, nor counted, and
    check_coordinates printed nothing about it at all.
    """
    source = [f"filler_{n}" for n in range(1, 10)]
    work = make_tree(tmp_path, source, all_additions_diff(source))
    write_verdict(tmp_path, "codex", [issue(900, "`filler_1` somewhere", path=SOURCE)])

    counts = check_reviewer_coordinates(work, "codex")

    assert counts == {"rescued": 0, "mislocated": 1, "unquoted": 0, "checked": 1}
    assert json.loads((tmp_path / "review-codex.json").read_text())["issues"][0][
        "line"
    ] is None


def test_a_rescued_line_that_then_fails_the_check_names_the_reviewers_number(
    tmp_path,
):
    """The note exists to tell "no location given" from "one that missed".

    It can only do that if the number it reports is the reviewer's. Baseline
    (84d7933): the rescue reassigned `line` before the note was formatted,
    so a rescued-then-mislocated finding read "the reviewer cited
    src/app.py:38" -- a number the driver computed, attributed to the
    reviewer, in the one sentence whose whole job is to say what the
    reviewer wrote.
    """
    source = [f"filler_{n}" for n in range(1, 41)]
    source[0] = "def target_function():"
    work = make_tree(tmp_path, source, all_additions_diff(source))
    # 43 is past the file's 40 lines and is a valid diff offset, so it is
    # rescued to line 38 -- which is 37 lines from the quoted code.
    write_verdict(tmp_path, "claude", [issue(43, "`target_function` is wrong")])

    counts = check_reviewer_coordinates(work, "claude")

    assert counts["rescued"] == 1
    assert counts["mislocated"] == 1
    described = json.loads((tmp_path / "review-claude.json").read_text())["issues"][0]
    assert f"cited {SOURCE}:43" in described["description"]
    assert f"cited {SOURCE}:38" not in described["description"]


# ------------------------------------------- the path the reviewer wrote


@pytest.mark.parametrize("escape", ["/etc/passwd", "../outside.txt", "link"])
def test_a_path_that_leaves_the_tree_is_never_read(tmp_path, escape):
    """`file` is model-authored, and a model's output is attacker-reachable.

    Measured before this was refused: `_source_lines(work, "/etc/passwd")`
    returned the file's lines, and `"../<name>"` returned a file beside the
    tree. A symlink inside the tree reaches out the same way, which is why
    the check is on the RESOLVED path and not on the spelling.
    """
    outside = tmp_path / "outside.txt"
    outside.write_text("secret_token_value\n", encoding="utf-8")
    work = tmp_path / "work"
    work.mkdir()
    (work / "link").symlink_to(outside)

    assert _source_lines(work, escape) is None


@pytest.mark.parametrize(
    "malformed",
    [
        pytest.param("src/\x00app.py", id="nul-byte"),
        pytest.param("src/\ud800app.py", id="lone-surrogate"),
    ],
)
def test_a_path_the_os_cannot_look_up_is_unquoted_and_not_fatal(tmp_path, malformed):
    """The containment guard held the contract for one class of bad path.

    Baseline (facb578): the guard was `except OSError`, and `resolve()` does
    not answer every malformed path with an OSError -- the os.lstat under it
    raises ValueError("embedded null byte") for a NUL and UnicodeEncodeError,
    a ValueError subclass, for a lone surrogate. Both are legal JSON and no
    shim validates a verdict per issue, so both arrive here from a model.
    The contract is the one the escaping paths above are held to: an
    unusable `file` is unquoted, never a raise, because a raise out of this
    module costs EVERY reviewer its inline comments.
    """
    work = tmp_path / "work"
    work.mkdir()

    assert _source_lines(work, malformed) is None


def test_a_line_that_cannot_be_an_integer_is_no_location_at_all(tmp_path):
    """The third variant of the hole, on the field beside `file`.

    Python's json accepts the bare token `Infinity`, both shims take the
    CLI's verdict on `isinstance(payload, dict)` alone, and `json.dumps`
    writes the float straight back -- so it survives accept_direct_write
    into the file read here, where `int(inf)` raised an OverflowError that
    `except (KeyError, TypeError, ValueError)` did not name.
    """
    work = tmp_path / "work"
    work.mkdir()
    (work / "review-claude.json").write_text(
        '{"issues": [{"file": "' + SOURCE + '", "line": Infinity,'
        ' "description": "`target_function` is wrong"}]}',
        encoding="utf-8",
    )

    counts = check_reviewer_coordinates(work, "claude")

    assert counts == {"rescued": 0, "mislocated": 0, "unquoted": 0, "checked": 0}


def test_an_escaping_path_reaches_the_caller_as_unquoted(tmp_path):
    """End to end: a `checked` count would mean the outside file was read.

    The evidence check reports whether the quoted token appears near the
    cited line, so reading an arbitrary path turns a prompt-injected
    verdict into a read oracle over the machine. None is what the caller
    already treats as "nothing to judge".
    """
    (tmp_path / "outside.txt").write_text("secret_token_value\n", encoding="utf-8")
    work = tmp_path / "work"
    work.mkdir()

    write_verdict(
        work, "codex", [issue(1, "`secret_token_value` leaks", path="../outside.txt")]
    )
    counts = check_reviewer_coordinates(work, "codex")

    assert counts == {"rescued": 0, "mislocated": 0, "unquoted": 1, "checked": 0}


# ---------------------------------------------------------- evidence check


def test_a_line_that_carries_the_quoted_code_passes_untouched(tmp_path):
    source = [f"filler_{n}" for n in range(1, 20)]
    source[9] = "    result = git_show(ref)"
    work = make_tree(tmp_path, source, all_additions_diff(source))
    before = write_verdict(
        tmp_path, "codex", [issue(10, "`git_show` is unguarded")]
    ).read_bytes()

    counts = check_reviewer_coordinates(work, "codex")

    assert counts["mislocated"] == 0 and counts["checked"] == 1
    # Byte-for-byte: a verdict with sound coordinates is not rewritten.
    assert (tmp_path / "review-codex.json").read_bytes() == before


def test_a_wrong_line_in_an_all_additions_file_is_caught(tmp_path, capsys):
    """The case the range check cannot see, and the reason this module exists."""
    source = [f"filler_{n}" for n in range(1, 120)]
    source[99] = "def review_pr(run_dir):"
    work = make_tree(tmp_path, source, all_additions_diff(source))
    write_verdict(tmp_path, "gemini", [issue(10, "`review_pr` is too long")])

    counts = check_reviewer_coordinates(work, "gemini")

    assert counts["mislocated"] == 1
    payload = json.loads((tmp_path / "review-gemini.json").read_text())
    assert payload["issues"][0]["line"] is None
    # The cost of dropping the line is stated where the operator reads the
    # finding, so "the reviewer gave no location" is distinguishable from
    # "the reviewer gave one and it did not match".
    assert "does not carry the code" in payload["issues"][0]["description"]
    assert f"{SOURCE}:10" in payload["issues"][0]["description"]
    assert "nearest match: line 100" in capsys.readouterr().out


def test_a_finding_that_quotes_nothing_is_counted_not_judged(tmp_path):
    source = [f"filler_{n}" for n in range(1, 10)]
    work = make_tree(tmp_path, source, all_additions_diff(source))
    write_verdict(tmp_path, "claude", [issue(3, "this code is unclear")])

    counts = check_reviewer_coordinates(work, "claude")

    assert counts == {"rescued": 0, "mislocated": 0, "unquoted": 1, "checked": 0}
    assert json.loads((tmp_path / "review-claude.json").read_text())["issues"][0][
        "line"
    ] == 3


def test_a_finding_about_a_removed_line_is_the_documented_blind_spot(tmp_path):
    """A deletion finding loses its line, and the docstring has to say so.

    The prompt's Diff Scope Rule has a model report a finding about removed
    code at the nearest remaining line and quote the `-` line as evidence.
    That quote is by construction not in the head file, so the evidence
    check calls the citation mislocated although the reviewer was right.

    Not fixed by matching the `-` lines: a removed line has no right-side
    number to hold a hit to EVIDENCE_WINDOW with, and a file-wide match
    could not have caught gemini's wrong-but-in-range 1240 at distance 260
    -- the one miss the window is measured against. So the blind spot is
    written into the list that exists to be complete, and this test is what
    keeps the list honest about it.
    """
    source = ["def refresh():", "    return build()"]
    diff = (
        f"diff --git a/{SOURCE} b/{SOURCE}\n"
        f"--- a/{SOURCE}\n"
        f"+++ b/{SOURCE}\n"
        "@@ -1,3 +1,2 @@\n"
        " def refresh():\n"
        "-    stale_cache.invalidate()\n"
        "     return build()\n"
    )
    work = make_tree(tmp_path, source, diff)
    write_verdict(
        tmp_path,
        "claude",
        [issue(2, "the removed `stale_cache.invalidate()` was the only thing"
                  " keeping this from returning a stale build")],
    )

    counts = check_reviewer_coordinates(work, "claude")

    # The behaviour: line 2 is in range, so nothing is rescued; the quote is
    # gone from the file, so the citation is called mislocated.
    assert counts == {"rescued": 0, "mislocated": 1, "unquoted": 0, "checked": 1}
    described = json.loads((tmp_path / "review-claude.json").read_text())["issues"][0]
    assert described["line"] is None
    # The finding itself survives; only its inline position is lost.
    assert "stale_cache.invalidate()" in described["description"]

    # The list is stated to be complete, so this case belongs in it.
    module_doc = sys.modules[check_reviewer_coordinates.__module__].__doc__ or ""
    assert "REMOVED line" in module_doc.split("WHAT THIS DOES NOT DO", 1)[1]


def test_the_window_admits_evidence_a_few_lines_off():
    source = [f"filler_{n}" for n in range(1, 40)]
    source[9] = "    subprocess.run(argv)"
    hits = evidence_lines(source, {"subprocess"})
    assert hits == [10]
    assert any(abs(hit - (10 + EVIDENCE_WINDOW)) <= EVIDENCE_WINDOW for hit in hits)
    assert not any(abs(hit - (10 + EVIDENCE_WINDOW + 1)) <= EVIDENCE_WINDOW
                   for hit in hits)


# --------------------------------------------------- degradation, not death


@pytest.mark.parametrize(
    "content", ["", "not json", "[]", '{"issues": "not a list"}']
)
def test_an_unusable_verdict_file_is_left_for_the_aggregate_to_report(
    tmp_path, content
):
    work = make_tree(tmp_path, ["a"], all_additions_diff(["a"]))
    (tmp_path / "review-codex.json").write_text(content, encoding="utf-8")
    assert check_reviewer_coordinates(work, "codex") == {
        "rescued": 0, "mislocated": 0, "unquoted": 0, "checked": 0
    }


def test_a_description_that_is_not_a_string_is_unquoted_not_a_raise(tmp_path):
    """The degradation contract holds for the ONE field that was unchecked.

    Baseline (f61fad1): `quoted_tokens(issue.get("description", ""))` hands
    a list straight to `re.findall`, which raises TypeError out of this
    function -- and this function's contract is that an unusable verdict
    returns zero counts rather than raising.
    """
    source = [f"filler_{n}" for n in range(1, 10)]
    work = make_tree(tmp_path, source, all_additions_diff(source))
    write_verdict(tmp_path, "claude", [issue(3, ["not", "a", "string"])])

    counts = check_reviewer_coordinates(work, "claude")

    assert counts == {"rescued": 0, "mislocated": 0, "unquoted": 1, "checked": 0}


def test_a_missing_diff_still_lets_the_evidence_half_run(tmp_path):
    source = [f"filler_{n}" for n in range(1, 40)]
    source[29] = "def target_function():"
    (tmp_path / "src").mkdir()
    (tmp_path / SOURCE).write_text("\n".join(source) + "\n", encoding="utf-8")
    write_verdict(tmp_path, "claude", [issue(5, "`target_function` is wrong")])

    # No pr.diff: nothing is in range, so rescue cannot fire either way --
    # but a cited line that does not carry its evidence is still caught.
    # `target_function` is at line 30 and the citation is line 5, well
    # outside EVIDENCE_WINDOW, so this is the mislocated case.
    counts = check_reviewer_coordinates(tmp_path, "claude")
    assert counts["checked"] == 1 and counts["mislocated"] == 1
    assert json.loads((tmp_path / "review-claude.json").read_text())["issues"][0][
        "line"
    ] is None


# ------------------------------------------- the four findings, as written


def load_real_issues() -> list[tuple[str, dict]]:
    """The preserved findings, or [] when a file of them has gone missing.

    Tolerant because this runs at COLLECTION time, under the parametrize
    below. Raising there fails the whole module, taking with it
    test_the_preserved_run_still_has_its_four_findings -- the test written
    to notice exactly this. Missing corpus is that test's finding to report,
    so it must survive to report it.
    """
    found = []
    for reviewer in ("claude", "codex", "gemini"):
        path = EVIDENCE / f"review-{reviewer}.json"
        if not path.is_file():
            return []
        for entry in json.loads(path.read_text(encoding="utf-8"))["issues"]:
            found.append((reviewer, entry))
    return found


def test_the_preserved_run_still_has_its_four_findings():
    assert len(load_real_issues()) == 4


@pytest.mark.parametrize("reviewer, entry", load_real_issues())
def test_every_real_finding_quotes_something_this_module_can_match(reviewer, entry):
    """No real finding is unquotable, so none of the four is merely skipped.

    This is the check that would have failed silently: if the token
    extraction found nothing in these descriptions, every issue would be
    counted "unquoted" and pass without being judged at all.
    """
    assert quoted_tokens(entry["description"]), (reviewer, entry["line"])


def test_geminis_finding_quotes_the_function_it_was_wrong_about():
    """The single token that separates a right finding from a wrong line.

    gemini reported `review_pr` at line 1240 of a 1687-line file. That line
    was mid-dictionary in `aggregate_env`; `review_pr` began at 1500, 260
    lines away. One quoted identifier is all the evidence check needs.
    """
    gemini = json.loads((EVIDENCE / "review-gemini.json").read_text())
    assert quoted_tokens(gemini["issues"][0]["description"]) == {"review_pr"}
    assert gemini["issues"][0]["line"] == 1240


def test_claudes_two_findings_named_a_file_they_overshot():
    """Both were pr.diff offsets, and both exceeded their file's length.

    Preserved: review_pr_local.py had 1687 lines and review_codex_local.py
    had 365, against citations of 2268 and 654. Rescue is what recovers
    them; without it the range check drops both and prints one line.
    """
    claude = json.loads((EVIDENCE / "review-claude.json").read_text())
    cited = {entry["file"].split("/")[-1]: entry["line"] for entry in claude["issues"]}
    assert cited == {"review_pr_local.py": 2268, "review_codex_local.py": 654}
