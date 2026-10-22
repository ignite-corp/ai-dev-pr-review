#!/usr/bin/env python3
"""The guidance a reviewer prompt must carry, and the check for it (AT-2844).

One list, read by three things: the tests that hold this repo's own reviewer
instructions to it, the tests that hold the consumer template
(examples/prompts/) to it, and consumer-health.yml, which reads each tracked
consumer's prompts, and the runner's default-prompts/, every day and reports
what they lack. A consumer's copy is the consumer's file (MIGRATION.md,
"Upgrading an already-onboarded consumer"), so no test in this repo reaches
it; this module is how the daily check and the tests agree on what "carries
the guidance" means without a second hand-kept list.

Two groups, with two different homes:

- ``congruence`` lives in the reviewers' own instructions only, which are
  base-owned and travel with the pin, so they reach every consumer and every
  runner target without a copy. ``CONGRUENCE_BLOCK`` is the text, and the
  base carries it four times: the inline shell in base-ai-review-single.yml
  and its mirror reviewer_prompts.py (Claude), review_prompt.md (Codex) and
  review_gemini.py (Gemini). test_reviewer_prompts.py holds the shell
  byte-equal to the mirror, and test_prompt_guidance.py holds the three
  rendered prompts byte-equal to this constant, so an edit changes all four.
  The definition's home is ~/.claude/AGENTS.md, ``### Congruent features``
  (the definition owner's choice); the block points there and does not copy
  it. The pointer is for the person reading a finding -- the model reviewing
  in CI cannot open it, and the questions inline are what it applies.
  Consumer prompts owe none of it,
  and the daily check reads a consumer's pin freshness, not its prompt, for
  whether the questions reach it.
- ``pin`` is the consumer prompt's, all-or-none. These are the labels
  verify_action_shas.py emits into context.md, which a prompt's
  ``## GitHub Actions SHA Pins`` section keys on, and that heading itself;
  when the heading is there the labels are read inside its section only.
  MIGRATION.md says a prompt
  with no such section needs no change, because the rendered blurb is then
  its only guidance; a section that carries some of the labels but not all
  is the AT-2801 drift, a reviewer told about one 404 label and not the
  other.

Matching is exact text after collapsing whitespace, so a prompt may wrap a
sentence at any column; it may not reword it.
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
            "does it extend the neighbouring pattern rather than add a second"
            " way?"
        ),
        "congruence-difference": (
            "A difference is not itself a defect; a difference that reads as"
            " drift because no reason is given is."
        ),
    },
    all_or_none=False,
)

PIN = GuidanceGroup(
    items={
        # The section heading counts as an item, so a section kept with
        # every label reworded is a partial section, not an absent one.
        "pin-section": "## GitHub Actions SHA Pins",
        "pin-comment-matches-false": 'comment-matches="false"',
        "pin-absent-comment-matches": "absent from the `<pin>` element",
        "pin-repo-not-found": 'error="repo-not-found"',
        "pin-repo-not-visible": 'error="repo-not-visible"',
    },
    all_or_none=True,
    section="pin-section",
)

# This repo's own reviewer instructions (the Claude prompt, review_prompt.md,
# review_gemini.py). Pin guidance is not theirs to carry: the blurb
# verify_action_shas.py renders into context.md travels with it.
REVIEWER_INSTRUCTIONS: tuple[GuidanceGroup, ...] = (CONGRUENCE,)

# A consumer's code-review-system.md and code-review-checklist.md, read
# together the way prepare concatenates them into context.md.
CONSUMER_PROMPTS: tuple[GuidanceGroup, ...] = (PIN,)


def _normalize(text: str) -> str:
    return " ".join(text.split())


def _scope(text: str, group: GuidanceGroup) -> str:
    """The part of ``text`` the group is read in: its section when the text
    carries the section heading, else all of it."""
    if group.section is None:
        return text
    heading = group.items[group.section]
    level = len(heading) - len(heading.lstrip("#"))
    lines = text.splitlines()
    for start, line in enumerate(lines):
        if line.strip().startswith(heading):
            end = next(
                (
                    i
                    for i in range(start + 1, len(lines))
                    if re.match(rf"#{{1,{level}}}\s", lines[i].strip())
                ),
                len(lines),
            )
            return "\n".join(lines[start:end])
    return text


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
        absent = [
            item
            for item, needle in group.items.items()
            if _normalize(needle) not in haystack
        ]
        if group.all_or_none and not require_all and len(absent) == len(group.items):
            continue
        missing.extend(absent)
    return missing


def main(argv: Sequence[str]) -> int:
    """Check prompt files against ``CONSUMER_PROMPTS``.

    usage: prompt_guidance.py [--control] FILE [FILE ...]

    The files are read together, as prepare concatenates the system prompt
    and the checklist into context.md. ``--control`` requires every group,
    all-or-none ones included, for the positive control. Prints each missing
    item id on its own line. Exit 0 when nothing is missing, 1 when something
    is, 2 on a usage error (argparse's own exit) or when a file cannot be read
    as UTF-8 text.
    """
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
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
        "\n".join(texts), CONSUMER_PROMPTS, require_all=args.control
    )
    for item in missing:
        print(item)
    return EXIT_MISSING if missing else EXIT_COMPLETE


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
