#!/usr/bin/env python3
"""Base-owned reviewer guidance and consumer prompt availability (AT-3159).

CONGRUENCE_BLOCK and PIN_BLOCK travel in the reviewers' own instructions,
through the consumer's or runner's review pin. Tests hold all three rendered
reviewers to these canonical blocks and execute Claude's inline workflow shell
against its local mirror, preserving pre-tag self-review compatibility.

Consumer prompts contain repository-specific guidance and owe no copy of
these shared rules. The CLI still reads their files and reports unreadable
text. Its --control mode checks the actual base Codex instructions against
REVIEWER_INSTRUCTIONS, through the same remote fetch path in consumer-health.
Matching collapses whitespace but never rewords a rule.
"""

from __future__ import annotations

import argparse
import re
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

EXIT_COMPLETE = 0
EXIT_MISSING = 1
EXIT_UNREADABLE = 2


@dataclass(frozen=True)
class GuidanceGroup:
    items: Mapping[str, str]
    all_or_none: bool
    # The id of an item that is a markdown heading. When the text carries
    # that heading, the group's other items are read only inside its section
    # -- from the heading to the next heading of the same or a higher level.
    section: str | None = None


CONGRUENCE_BLOCK = """CONGRUENCE: Ask three questions of the change. Definition: ~/.claude/AGENTS.md, ### Congruent features.
- Self: does the change do what it says, and are unknowns written as unknown? Typical misses are an unmerged effect written in the present tense, and runtime behaviour asserted without naming the revision it was read from.
- Other: does it serve the actual need rather than the wording, and if the two differ, is the gap stated? A gap the builder resolves alone is placating or superreasonable.
- Context: does it extend the neighbouring pattern rather than add a second way? Where it must differ from its siblings, does it differ visibly with the reason written there?
A difference is not itself a defect; a difference that reads as drift because no reason is given is.
The PR body is not part of your input: judge what the diff itself says. An incongruence is a finding like any other, under the EVIDENCE RULE and DIFF SCOPE above.
"""

CONGRUENCE = GuidanceGroup(
    items={
        "congruence-definition": (
            "Definition: ~/.claude/AGENTS.md, ### Congruent features."
        ),
        "congruence-self": (
            "does the change do what it says, and are unknowns written as unknown?"
        ),
        "congruence-other": (
            "does it serve the actual need rather than the wording, and if the"
            " two differ, is the gap stated?"
        ),
        "congruence-context": (
            "does it extend the neighbouring pattern rather than add a second way?"
        ),
        "congruence-difference": (
            "A difference is not itself a defect; a difference that reads as"
            " drift because no reason is given is."
        ),
    },
    all_or_none=False,
)

PIN = GuidanceGroup(
    # Explicit ids keep health diagnostics tied to the same semantic rule
    # when the canonical policy is reordered or wrapped.
    items={
        "pin-section": "## GitHub Actions SHA Pins",
        "pin-context": "When the diff pins a SHA for a GitHub Action (`uses: owner/repo@<SHA>`), consult the `<verified-action-pins>` section in context.md and apply these rules by status:",
        "pin-verified": '- **`status="verified"`** -- The SHA matched a tag in the upstream repository\'s tag listing. Treat the SHA as definitively valid: do NOT raise a critical/major about SHA invalidity; any such claim would be a false positive.',
        "pin-comment-matches-false": '  - If the same pin carries **`comment-matches="false"`**, the `# vX.Y` version comment in the diff drifts from the resolved upstream tag (given in the `tag="..."` attribute). This IS a legitimate finding -- raise as `minor` and cite the `tag=` value as the correct version comment.',
        "pin-absent-comment-matches": "  - The `comment-matches` attribute appears **only when the diff line carried a `# vX.Y` comment**. If the attribute is absent from the `<pin>` element, the PR chose not to annotate the pin with a version comment -- that is not a finding and MUST NOT be flagged as a comment mismatch.",
        "pin-unmatched": '- **`status="unmatched"`** -- The tag listing was fetched successfully but the SHA did not appear in any tag. This indicates a stale mirror, a force-pushed tag, or a wrong/typo\'d SHA. Raise as `major`: the upstream repo is reachable, so the discrepancy is deterministic, not transient.',
        "pin-repo-not-found": '- **`status="unverified"` with `error="repo-not-found"`** -- The API returned 404 for a repository whose owner is not the owner of the repository under review, or the run\'s own owner could not be determined. GitHub answers 404 both for a repository that does not exist and for a private one this run\'s token may not read, so the 404 does NOT establish that the repository is absent. Where the owners are known to differ, the pin is usually a public action, which makes a misspelled `owner/repo` the likelier of the two. Ask the author to confirm the `owner/repo` spelling; do NOT claim the repository does not exist. Raise only as `suggestion`.',
        "pin-repo-not-visible": '- **`status="unverified"` with `error="repo-not-visible"`** -- The API returned 404 for a repository whose owner is the owner of the repository under review. The same two causes are indistinguishable here, and when the owners match the likelier one is a private repository this run\'s token cannot read. Still ask the author to confirm the `owner/repo` spelling, since a typo or a renamed/deleted action looks identical and would otherwise fail only at runtime; do NOT claim the repository does not exist. Raise only as `suggestion`.',
        "pin-transient": '- **`status="unverified"` with any other `error`** (`rate-limit`, `timeout`, `api-error: ...`, `json-decode-error`, `unknown`) -- The verification itself failed transiently; we could not confirm or deny the pin. Raise only as `suggestion`; do not raise `critical`/`major` since the SHA was not actually checked.',
    },
    all_or_none=False,
    section="pin-section",
)

# Render the canonical policy from its one home, preserving the paragraph
# spacing and nested bullet indentation delivered by every reviewer.
PIN_BLOCK = (
    PIN.items["pin-section"]
    + "\n\n"
    + PIN.items["pin-context"]
    + "\n\n"
    + "\n".join(
        rule
        for item, rule in PIN.items.items()
        if item not in ("pin-section", "pin-context")
    )
    + "\n"
)

REVIEWER_INSTRUCTIONS: tuple[GuidanceGroup, ...] = (CONGRUENCE, PIN)

# Consumer/runner files are still read; shared guidance arrives via their pin.
CONSUMER_PROMPTS: tuple[GuidanceGroup, ...] = ()


def _normalize(text: str) -> str:
    return " ".join(text.split())


_HEADING = re.compile(r"(#+)\s+(.*)")


def _heading_start(lines: Sequence[str], heading: str) -> int | None:
    """Index of the line that is ``heading`` exactly: the same number of
    hashes, and the same text after the normalization the items use."""
    want = _HEADING.fullmatch(heading)
    assert want is not None, heading
    for index, line in enumerate(lines):
        got = _HEADING.fullmatch(line.strip())
        if (
            got is not None
            and got.group(1) == want.group(1)
            and _normalize(got.group(2)) == _normalize(want.group(2))
        ):
            return index
    return None


def _scope(text: str, group: GuidanceGroup) -> str:
    """The part of ``text`` the group is read in: its section when the text
    carries the section heading, else all of it."""
    if group.section is None:
        return text
    heading = group.items[group.section]
    level = len(heading) - len(heading.lstrip("#"))
    lines = text.splitlines()
    start = _heading_start(lines, heading)
    if start is None:
        return text
    end = next(
        (
            i
            for i in range(start + 1, len(lines))
            if re.match(rf"#{{1,{level}}}\s", lines[i].strip())
        ),
        len(lines),
    )
    return "\n".join(lines[start:end])


def missing_items(
    text: str, groups: Sequence[GuidanceGroup], *, require_all: bool = False
) -> list[str]:
    """Return the id of every required item ``text`` does not carry.

    ``require_all`` treats an all-or-none group as required, for the positive
    control: a control that passed by carrying none of a group would pass on
    an empty fetch too.
    """
    missing: list[str] = []
    for group in groups:
        haystack = _normalize(_scope(text, group))
        # The heading is present only as a heading line, by the same test
        # that scopes the section, not as a substring of a deeper or longer
        # heading.
        absent = [
            item
            for item, needle in group.items.items()
            if (
                _heading_start(text.splitlines(), needle) is None
                if item == group.section
                else _normalize(needle) not in haystack
            )
        ]
        if group.all_or_none and not require_all and len(absent) == len(group.items):
            continue
        missing.extend(absent)
    return missing


def main(argv: Sequence[str]) -> int:
    """Read consumer prompt files, or check base instructions with --control.

    usage: prompt_guidance.py [--control] FILE [FILE ...]

    The files are read together, as prepare concatenates the system prompt
    and the checklist into context.md. ``--control`` requires every group
    in REVIEWER_INSTRUCTIONS, for the positive control. Prints each missing
    item id on its own line. Exit 0 when nothing is missing, 1 when something
    is, 2 on a usage error (argparse's own exit) or when a file cannot be read
    as UTF-8 text.
    """
    parser = argparse.ArgumentParser(description=(__doc__ or "").partition("\n")[0])
    parser.add_argument(
        "--control", action="store_true", help="require every group (positive control)"
    )
    parser.add_argument("files", nargs="+", help="prompt files, read together")
    args = parser.parse_args(argv)
    texts: list[str] = []
    for name in args.files:
        try:
            texts.append(Path(name).read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError) as exc:
            print(f"cannot read prompt file: {type(exc).__name__}", file=sys.stderr)
            return EXIT_UNREADABLE
    missing = missing_items(
        "\n".join(texts),
        REVIEWER_INSTRUCTIONS if args.control else CONSUMER_PROMPTS,
        require_all=args.control,
    )
    for item in missing:
        print(item)
    return EXIT_MISSING if missing else EXIT_COMPLETE


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
