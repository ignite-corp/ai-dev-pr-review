#!/usr/bin/env python3
"""Make the reviewers' line numbers a checked contract instead of a hope.

One real end-to-end run produced four findings from three reviewers. THREE
of the four were unusable because of their line numbers, and nothing in the
driver was watching for it:

  claude  review_pr_local.py:2268     the file has 1687 lines -- a pr.diff offset
  claude  review_codex_local.py:654   the file has 365 lines  -- a pr.diff offset
  gemini  review_pr_local.py:1240     in range, but the wrong line
  codex   review_pr_local.py:742      correct, and the only one posted

The first two were dropped by post_inline_comments.py's range check, which
printed one line of stdout about it. The third PASSED that check: the PR
added whole files, so every line was "in the diff" and the range check had
zero power to discriminate. The reviewer prompt already tells every model to
report the file's own line number; two of three did not.

So two mechanisms, for the two ways it went wrong.

RESCUE. Both dropped findings were right about the code and right about the
position -- in pr.diff's coordinate system. That system is a total function
of pr.diff, so the conversion is exact rather than a guess.

EVIDENCE. The prompt's EVIDENCE RULE already makes a model quote the code it
is accusing, so the cited line has to carry some of what the description
quotes.

ORDER: RESCUE FIRST, AND ONLY WHEN THE CITED LINE IS OUT OF RANGE.
The two mechanisms disagree, so the order is a decision and not a detail.
Rescue runs first because after it the issue is making a claim in file
coordinates, and that is the claim worth checking; checking the raw number
would test a claim in a coordinate system the reviewer was not using.

Rescue is gated on the cited line being out of range, and the gate is
load-bearing rather than an optimisation. Measured on the four preserved
findings: gemini's wrong-but-in-range 1240 ALSO maps to a valid pr.diff
offset, for line 472 of the same file -- which is `strip_agent_config`,
no closer to the `review_pr` it was talking about than 1240 was. An
ungated rescue would have moved one wrong line to a different wrong line
and then had its own output to check.

WHAT THIS DOES NOT DO, stated because the range check's silence about its
own blind spot is what cost three findings:
  - It cannot check an issue whose description quotes no code. Those are
    counted as unquoted, not called checked.
  - It reads `description` only, never `suggestion`: a suggestion quotes the
    code that SHOULD be there, which by construction is not at the cited
    line. codex's finding quoted `git cat-file` in its suggestion and would
    have been scored against a file that has never contained it.
  - A quote that is merely nearby is not told from one that is exact.
  - Rescue needs pr.diff. Without it, only the evidence half runs.
  - It cannot tell a finding about a REMOVED line from a wrong citation.
    The prompt's own Diff Scope Rule has a model report such a finding at
    the nearest remaining line and quote the `-` line as its evidence, and
    that quote is by construction absent from the head file. So the finding
    is counted mislocated, loses its line, and carries MISLOCATED_NOTE
    saying the citation does not carry the code it quotes -- true of the
    file, and unfair to a reviewer who was right. The FINDING survives;
    only its inline position is lost.

Matching the `-` lines of pr.diff was the alternative, and was not taken. A
removed line has no right-side number, so a hit on one cannot be held to
EVIDENCE_WINDOW the way every other hit is, and an unbounded match would
suppress the check across a whole file -- the ungated hazard this module
already refused once for rescue, and the exact shape of the one miss the
window is measured against: gemini's wrong-but-in-range 1240 was caught at
distance 260, which a file-wide match could not have caught. Pinning a `-`
hit to the hunk's right-side position at that point would keep the
proximity, but it widens a threshold measured on a four-finding corpus that
contains no deletion finding to widen it against. Written down rather than
guessed at.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from post_inline_comments import parse_diff

# How far from the cited line the quoted evidence may sit.
#
# Measured, not chosen. Distance from the cited line (after rescue) to the
# nearest line carrying the quoted evidence, over all four findings of the
# preserved run -- the three sound ones and the one that was wrong:
#
#   claude  review_pr_local.py:2268 -> 1500   distance 0     (def review_pr)
#   claude  review_codex_local.py:654 -> 257  distance 0     (the unbounded call)
#   codex   review_pr_local.py:742            distance 6     (`git show`, line 736)
#   gemini  review_pr_local.py:1240           distance 260   (WRONG line)
#
# Any value in [6, 259] separates the sound findings from the unsound one.
# 20 sits an order of magnitude below the observed miss, with room above the
# largest sound distance for a finding that cites the head of a block and
# quotes something a few lines into it.
EVIDENCE_WINDOW = 20
# Short tokens ("if", "os", "id") match everywhere and would make every
# issue pass. Four characters is the shortest that still names things in
# this codebase ("work", "runs", "show" -- and "show" is the single token
# that carried codex's finding, so this bound is not free).
MIN_TOKEN_LEN = 4
# Said in the description of a finding whose line was dropped, so the
# operator can tell "the reviewer gave no location" from "the reviewer gave
# one and it did not match". Without it this outcome is indistinguishable
# from the accident it exists to correct: a real finding reaching only the
# aggregate is exactly what happened to claude's two. {line} is therefore
# the number the REVIEWER wrote, never the one a rescue computed from it:
# attributing the driver's arithmetic to the reviewer is the one thing that
# would make this sentence unable to answer the question it is asked.
MISLOCATED_NOTE = (
    "[driver: the reviewer cited {path}:{line}, which does not carry the code"
    " this finding quotes, so it is reported here rather than inline]"
)

_BACKTICK_RE = re.compile(r"`([^`\n]+)`")
_WORD_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_DIFF_FILE_RE = re.compile(r"^\+\+\+ b/(.*)$")
_HUNK_RE = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)")


def diff_offset_index(diff_text: str) -> dict[int, tuple[str, int]]:
    """Map each 1-based pr.diff line offset to the (path, line) it renders.

    Only added and context lines are mapped: those are the ones that exist
    on the right side and so have a file line number at all. A model reading
    pr.diff and reporting its own position lands on one of them.

    The file header is read only BEFORE a hunk opens. Inside one, a source
    line reading `++ b/x` is rendered as `+++ b/x` and matched the header
    pattern, which reset the path and left every later offset in that file
    mapped to nothing or to the wrong number.

    SOUND FOR GIT-FORMAT DIFFS ONLY, which is what this reads: pr.diff is
    produced by extract_pr_diff.sh, and there every file opens with
    `diff --git`, the one line that reopens the header window. Plain
    `diff -u` over several files puts the second file's `--- `/`+++ ` pair
    straight after the previous hunk body, with no `diff --git` between, and
    on that input the second file's lines are attributed to the first.
    Widening the window to `--- ` would reintroduce the mirror image of the
    bug above -- a deleted source line reading `-- a/x` is rendered
    `--- a/x` -- so the limit is recorded rather than traded for another
    one. post_inline_comments.parse_diff carries the same guard and the same
    limit; the two indexes are compared directly, so they move together.
    """
    index: dict[int, tuple[str, int]] = {}
    path: str | None = None
    right = 0
    for offset, line in enumerate(diff_text.splitlines(), 1):
        if line.startswith("\\"):
            continue
        if line.startswith("diff --git"):
            path = None
            right = 0
            continue
        header = _DIFF_FILE_RE.match(line) if right == 0 else None
        if header:
            path = header.group(1).split("\t")[0]
            continue
        hunk = _HUNK_RE.match(line)
        if hunk:
            right = int(hunk.group(1))
            continue
        if path is None or right == 0:
            continue
        if line.startswith(("+", " ")) or line == "":
            index[offset] = (path, right)
            right += 1
    return index


def quoted_tokens(description: Any) -> set[str]:
    """Identifier-ish words the description quotes in backticks.

    Whole spans are not required to appear verbatim: a model writes
    `git show` for a list that reads ["git", "show", ...], and
    `extract_from_log()` for a def without the parentheses. Words are what
    survive that, so words are what is looked for.

    THE TYPE IS NOT GUARANTEED, so it is checked here rather than assumed:
    `description` is model-authored and both shims accept the CLI's verdict
    file on `isinstance(payload, dict)` alone, with no per-issue validation.
    `re.findall` raises TypeError on a list or a number, and that raise
    escaped check_reviewer_coordinates into the driver's review stage, whose
    `except Exception` skips post_inline_comments -- so ONE malformed
    description cost EVERY reviewer its inline comments. The other
    model-authored fields on this path are already checked (_issue_location
    requires a str `file`, _source_lines refuses an escaping path); this one
    was not. Anything that is not a string quotes nothing, which is the
    answer an empty description already got.
    """
    if not isinstance(description, str):
        return set()
    tokens: set[str] = set()
    for span in _BACKTICK_RE.findall(description):
        for word in _WORD_RE.findall(span):
            if len(word) >= MIN_TOKEN_LEN:
                tokens.add(word)
    return tokens


def evidence_lines(lines: list[str], tokens: set[str]) -> list[int]:
    """1-based line numbers carrying any of the tokens."""
    return [
        number
        for number, text in enumerate(lines, 1)
        if tokens & set(_WORD_RE.findall(text))
    ]


def _source_lines(work: Path, relative: str) -> list[str] | None:
    """The file's lines, or None when it is not a readable file inside `work`.

    `relative` is the `file` field of a model-authored verdict, and a model's
    output is reachable by whoever wrote the diff it read. Unchecked,
    `work / relative` IS `/etc/passwd` for an absolute value and walks out of
    the checkout for a `../` one, which turns the evidence check -- a report
    of whether a quoted token sits near a cited line -- into a read oracle
    over the machine. Judged on the RESOLVED path, so a symlink inside the
    tree cannot point out of it either. The caller treats None as unquoted.

    ValueError is caught beside OSError because `resolve()` does not answer
    every malformed path with an OSError: the os.lstat underneath raises
    ValueError("embedded null byte") for a path carrying a NUL, and
    UnicodeEncodeError -- a ValueError subclass -- for a lone surrogate.
    Both are legal JSON and both reach here, since no shim validates a
    verdict per issue. The handler is what widened rather than a
    `"\\x00" in relative` guard beside is_absolute(), because that closes
    one spelling of the class and leaves the rest. Escaping cost far more
    than the check: it left check_reviewer_coordinates through
    check_coordinates and run_review_stage into review_pr's
    `except Exception`, which reports a "review" stage failure and skips
    post_inline_comments -- so ONE malformed path in ONE reviewer's verdict
    took EVERY reviewer's inline comments with it.
    """
    try:
        if Path(relative).is_absolute():
            return None
        candidate = (work / relative).resolve()
        if not candidate.is_relative_to(work.resolve()):
            return None
        if not candidate.is_file():
            return None
        return candidate.read_text(encoding="utf-8", errors="replace").splitlines()
    except (OSError, ValueError):
        return None


def _issue_location(issue: dict[str, Any]) -> tuple[str, int] | None:
    """The (path, line) an issue claims, or None when it claims none.

    OverflowError is the third variant of the hole `quoted_tokens` and
    `_source_lines` each closed on their own field, and it is on this one:
    Python's json accepts the bare token `Infinity`, both shims take the
    CLI's verdict file on `isinstance(payload, dict)` alone, and
    `json.dumps` writes that float straight back out -- so `{"line":
    Infinity}` survives accept_direct_write into the file read here, where
    `int(inf)` raises an OverflowError the handler did not name. The blast
    radius is _source_lines': the raise leaves this module and costs every
    reviewer its inline comments. An issue whose line cannot be an integer
    claims no location, which is what None already says.
    """
    path = issue.get("file")
    if not isinstance(path, str) or not path:
        return None
    try:
        return path, int(issue["line"])
    except (KeyError, TypeError, ValueError, OverflowError):
        return None


def check_reviewer_coordinates(
    work: Path, reviewer: str, diff_name: str = "pr.diff"
) -> dict[str, int]:
    """Rescue and screen one reviewer's line numbers, in place.

    Returns a count per outcome. The verdict file is rewritten only when
    something changed, so a reviewer whose coordinates were all sound is
    left byte for byte as it wrote it.
    """
    counts = {"rescued": 0, "mislocated": 0, "unquoted": 0, "checked": 0}
    verdict_path = work / f"review-{reviewer}.json"
    try:
        verdict = json.loads(verdict_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        # A reviewer that wrote no readable verdict has no coordinates to
        # check, and repeating that here would say what the shim and the
        # aggregate both already say.
        return counts
    if not isinstance(verdict, dict) or not isinstance(verdict.get("issues"), list):
        return counts

    try:
        diff_text = (work / diff_name).read_text(encoding="utf-8", errors="replace")
    except OSError:
        diff_text = ""
    valid_lines = parse_diff(diff_text)
    offsets = diff_offset_index(diff_text)

    changed = False
    for issue in verdict["issues"]:
        if not isinstance(issue, dict):
            continue
        located = _issue_location(issue)
        if located is None:
            continue
        path, line = located

        # Out of range AND an offset into this file is the one case rescue
        # answers; anything else keeps the number as written, because
        # inventing one would be worse. ONLY THE RESCUE IS SKIPPED -- the
        # evidence check still runs on the line as cited. Skipping the issue
        # outright is how an unrescuable citation went uncounted, unchecked,
        # and unmentioned by check_coordinates.
        in_range = line in valid_lines.get(path, set())
        mapped = None if in_range else offsets.get(line)
        if mapped is not None and mapped[0] == path:
            line = mapped[1]
            print(
                f"  {reviewer}: {path}:{located[1]} is a pr.diff offset;"
                f" reading it as {path}:{line}"
            )
            issue["line"] = line
            counts["rescued"] += 1
            changed = True

        tokens = quoted_tokens(issue.get("description"))
        lines = _source_lines(work, path)
        if not tokens or lines is None:
            counts["unquoted"] += 1
            continue
        counts["checked"] += 1
        hits = evidence_lines(lines, tokens)
        if any(abs(hit - line) <= EVIDENCE_WINDOW for hit in hits):
            continue
        nearest = min(hits, key=lambda hit: abs(hit - line)) if hits else None
        print(
            f"  {reviewer}: {path}:{line} does not carry the code the finding"
            " quotes"
            + (f" (nearest match: line {nearest})" if nearest else "")
            + "; dropping the line, keeping the finding"
        )
        issue["line"] = None
        note = MISLOCATED_NOTE.format(path=path, line=located[1])
        issue["description"] = f"{issue.get('description', '')} {note}".strip()
        counts["mislocated"] += 1
        changed = True

    if changed:
        verdict_path.write_text(json.dumps(verdict, indent=2), encoding="utf-8")
    return counts
