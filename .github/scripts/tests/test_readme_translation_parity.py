"""Public repo invariant: README.md and README.ko.md must not drift apart.

AT-2031 added a 39-line ``## Required consumer-repo settings`` section to
README.md only. It shipped in v1.6.0 English-only -- the one section whose
absence kills a consumer at workflow startup -- and nothing reported it. A
person found it days later while doing unrelated work (AT-2091).

Translation cannot be checked by comparing strings: README.ko.md is
deliberately different prose. Two invariants survive translation instead.

1. Section shape. The ordered sequence of heading levels (outside fenced
   code) must be identical in both files. Heading *text* is translated, so
   only the shape is comparable; a section added to one side alone changes
   it.

2. Identifier presence. Tokens that no translator rewrites -- variable and
   secret names, API field names, workflow and script filenames -- must
   appear in both files or in neither. Presence, not count.

   Exact occurrence counts were rejected as the invariant. On the tree as
   it stands they flag ``settings.json`` (15 vs 13) and
   ``managed-settings.json`` (8 vs 7): both files document the same facts,
   the English prose just repeats the filename in sentences the Korean
   phrases without it. Presence parity has zero violations on the same tree
   and still catches AT-2091, where the Korean side had the identifiers zero
   times.

Escape hatch: put ``<!-- translation-parity: ignore-section <reason> -->`` on
the line before a heading to drop that heading and its body from both checks,
for a section that deliberately exists on one side only. The reason is
mandatory and lives inside the comment, so it never renders into the page.

A marker inside a fenced code block is an example, not an instruction: it
neither exempts a section nor counts as malformed. Documenting the hatch in
this repository must not silently switch it on.

A marker with no reason does not exempt anything: it leaves the section under
both checks *and* fails ``test_ignore_markers_carry_a_reason`` at its own
file and line. Silently honouring it would be the exact defect this module
exists to catch, one level up -- a documented rule that nothing enforces,
failing in the direction that looks like success. Ignoring it quietly instead
would keep coverage but let a marker someone believes is load-bearing sit
unnoticed in a section that happens to be shape- and identifier-neutral
today. Failing loudly keeps the coverage and names the defect. Nothing in the
tree needs the hatch today.

Not caught: a section present in both files but hollow in one. Matching
headings and matching identifier presence say the structure and the
untranslatable vocabulary agree; they say nothing about whether the prose
under a heading actually explains the same thing. This is accepted -- the
alternative is comparing translated prose, which is what makes this hard.
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Iterator
from difflib import SequenceMatcher
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
ENGLISH = REPO_ROOT / "README.md"
KOREAN = REPO_ROOT / "README.ko.md"

FENCE = re.compile(r"^\s*(?:```|~~~)")
HEADING = re.compile(r"^(#{1,6})\s+(\S.*)$")
# The escape hatch, and the looser pattern that recognises an attempt at it.
IGNORE_MARKER = re.compile(
    r"^\s*<!--\s*translation-parity:\s*ignore-section\s+(?P<reason>\S.*?)\s*-->\s*$"
)
IGNORE_MARKER_ATTEMPT = re.compile(r"^\s*<!--\s*translation-parity:\s*ignore-section\b")
IGNORE_MARKER_EXAMPLE = "<!-- translation-parity: ignore-section <reason> -->"

# Tokens as markdown writes them: identifiers, dotted paths, filenames.
TOKEN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
# An identifier is untranslatable if it is snake_case/SCREAMING_SNAKE ...
SNAKE = re.compile(r"^[A-Za-z][A-Za-z0-9]*(?:_[A-Za-z0-9]+)+$")
# ... or a filename with an extension this repo actually ships.
FILENAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*\.(?:yml|yaml|json|md|py|sh)$")


def _read(path: Path) -> str:
    """Read a README as UTF-8, never as the locale-preferred encoding.

    ``Path.read_text()`` with no encoding follows the platform locale. On a
    runner with ``C``/``POSIX`` or any non-UTF-8 locale it raises
    ``UnicodeDecodeError`` on README.ko.md -- this check would die on the very
    file it exists to read, and an error is not a finding: a check that cannot
    open the file looks exactly like a check with nothing to report.
    """
    return path.read_text(encoding="utf-8")


def _scan(text: str) -> Iterator[tuple[int, str, bool]]:
    """Yield (line number, line, is inside a fenced code block).

    Every consumer of fence state goes through here. Three separate loops
    used to re-derive it, which is why fence-awareness kept being fixed in
    one branch at a time. Fence delimiters themselves report as inside the
    fence: they are never headings and never markers.
    """
    in_fence = False
    for lineno, line in enumerate(text.splitlines(), start=1):
        if FENCE.match(line):
            in_fence = not in_fence
            yield lineno, line, True
            continue
        yield lineno, line, in_fence


def _strip_ignored_sections(text: str) -> str:
    """Drop sections marked as deliberately one-sided.

    Only a marker carrying a reason, and sitting outside a fenced block,
    exempts anything. A reasonless marker is inert here and is reported by
    ``malformed_markers`` instead.
    """
    kept: list[str] = []
    skip_above: int | None = None
    marked = False
    for _, line, in_fence in _scan(text):
        heading = None if in_fence else HEADING.match(line)
        if heading:
            level = len(heading.group(1))
            if marked:
                skip_above, marked = level, False
                continue
            if skip_above is not None and level > skip_above:
                continue
            skip_above = None
        elif not in_fence and IGNORE_MARKER.match(line):
            marked = True
            continue
        if skip_above is None:
            kept.append(line)
    return "\n".join(kept)


def malformed_markers(text: str) -> list[str]:
    """Lines reaching for the escape hatch without giving a reason.

    Fenced lines are exempt for the same reason they are not honoured as
    markers: inside a code block the text is an example, not an instruction.
    """
    bad: list[str] = []
    for lineno, line, in_fence in _scan(text):
        if in_fence:
            continue
        if IGNORE_MARKER_ATTEMPT.match(line) and not IGNORE_MARKER.match(line):
            bad.append(f"  line {lineno}: {line.strip()}")
    return bad


def headings(text: str) -> list[tuple[int, str]]:
    """(level, title) for every heading outside a fenced code block."""
    found: list[tuple[int, str]] = []
    for _, line, in_fence in _scan(_strip_ignored_sections(text)):
        if in_fence:
            continue
        match = HEADING.match(line)
        if match:
            found.append((len(match.group(1)), match.group(2).strip()))
    return found


def identifier_counts(text: str) -> Counter[str]:
    """Occurrences of every untranslatable identifier in the document.

    Fenced code is deliberately included: ``default_workflow_permissions``,
    the identifier at the centre of AT-2091, appears only inside a ``gh api``
    example.
    """
    counts: Counter[str] = Counter()
    for token in TOKEN.findall(_strip_ignored_sections(text)):
        token = token.strip("._-")
        if SNAKE.match(token) or FILENAME.match(token):
            counts[token] += 1
    return counts


def shape_diff(english: list[tuple[int, str]], korean: list[tuple[int, str]]) -> list[str]:
    """Human-readable report of where the two heading sequences diverge."""
    matcher = SequenceMatcher(a=[level for level, _ in english], b=[level for level, _ in korean])
    report: list[str] = []
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            continue
        for level, title in english[i1:i2]:
            report.append(f"  README.md only: {'#' * level} {title}")
        for level, title in korean[j1:j2]:
            report.append(f"  README.ko.md only: {'#' * level} {title}")
    return report


def presence_diff(english: Counter[str], korean: Counter[str]) -> list[str]:
    report: list[str] = []
    for name in sorted(set(english) | set(korean)):
        if bool(english[name]) != bool(korean[name]):
            report.append(f"  {name}: README.md x{english[name]}, README.ko.md x{korean[name]}")
    return report


# ---------------------------------------------------------------------------
# The invariant, against the real files
# ---------------------------------------------------------------------------


def test_both_readmes_exist() -> None:
    """A rename must break this test loudly, not make it vacuous."""
    assert ENGLISH.is_file(), f"missing {ENGLISH}"
    assert KOREAN.is_file(), f"missing {KOREAN}"


def test_ignore_markers_carry_a_reason() -> None:
    for path in (ENGLISH, KOREAN):
        bad = malformed_markers(_read(path))
        assert not bad, (
            f"{path.name} reaches for the parity escape hatch without a reason:\n"
            + "\n".join(bad)
            + f"\nUse {IGNORE_MARKER_EXAMPLE}"
        )


def test_section_shape_matches() -> None:
    diff = shape_diff(headings(_read(ENGLISH)), headings(_read(KOREAN)))
    assert not diff, "README.md and README.ko.md have different sections:\n" + "\n".join(diff)


def test_untranslatable_identifiers_appear_in_both() -> None:
    diff = presence_diff(identifier_counts(_read(ENGLISH)), identifier_counts(_read(KOREAN)))
    assert not diff, (
        "identifiers documented on one side only:\n"
        + "\n".join(diff)
        + "\nTranslate the missing text, or mark the section with "
        + IGNORE_MARKER_EXAMPLE
    )


# ---------------------------------------------------------------------------
# The checks themselves, against synthetic documents
# ---------------------------------------------------------------------------

_EN = """# Title

## Setup

Set `default_workflow_permissions` to write.

## Usage

Call `base-ai-review-single.yml`.
"""

_KO = """# Title

## Seteop

`default_workflow_permissions` reul write ro.

## Sayong

`base-ai-review-single.yml` ho-chul.
"""


def _marker(reason: str) -> str:
    return f"<!-- translation-parity: ignore-section {reason} -->"


BARE_MARKER = "<!-- translation-parity: ignore-section -->"


def test_translated_pair_passes_both_checks() -> None:
    assert shape_diff(headings(_EN), headings(_KO)) == []
    assert presence_diff(identifier_counts(_EN), identifier_counts(_KO)) == []


def test_section_added_on_one_side_is_reported() -> None:
    drifted = _EN + "\n## Required consumer-repo settings\n\nRun `gh api`.\n"
    diff = shape_diff(headings(drifted), headings(_KO))
    assert any("Required consumer-repo settings" in line for line in diff), diff


def test_identifier_documented_on_one_side_only_is_reported() -> None:
    drifted = _EN.replace("`default_workflow_permissions`", "nothing")
    diff = presence_diff(identifier_counts(drifted), identifier_counts(_KO))
    assert diff == ["  default_workflow_permissions: README.md x0, README.ko.md x1"]


def test_repeated_identifier_is_not_a_count_mismatch() -> None:
    """Prose density differs between languages; presence is the invariant."""
    wordy = _EN + "\nAgain: `base-ai-review-single.yml`, `base-ai-review-single.yml`.\n"
    assert presence_diff(identifier_counts(wordy), identifier_counts(_KO)) == []


def test_heading_inside_a_code_fence_is_not_a_section() -> None:
    fenced = _EN + "\n```bash\n# Not a heading\necho hi\n```\n"
    assert shape_diff(headings(fenced), headings(_KO)) == []


def test_marked_section_is_excluded_from_both_checks() -> None:
    en_only = (
        _EN
        + "\n" + _marker("English-only appendix, no Korean equivalent planned") + "\n"
        + "## Appendix\n\nSee `internal_only_flag` and `notes.md`.\n"
    )
    assert shape_diff(headings(en_only), headings(_KO)) == []
    assert presence_diff(identifier_counts(en_only), identifier_counts(_KO)) == []


def test_marked_section_ends_at_the_next_sibling_heading() -> None:
    en_only = (
        _EN
        + "\n" + _marker("English-only appendix") + "\n"
        + "## Appendix\n\n### Detail\n\nUses `internal_only_flag`.\n"
        + "## Tail\n\nUses `base-ai-review-single.yml`.\n"
    )
    ko = _KO + "\n## Kkori\n\n`base-ai-review-single.yml` sayong.\n"
    assert shape_diff(headings(en_only), headings(ko)) == []
    assert presence_diff(identifier_counts(en_only), identifier_counts(ko)) == []


def test_marked_section_survives_a_comment_line_in_fenced_code() -> None:
    en_only = (
        _EN
        + "\n" + _marker("English-only appendix") + "\n"
        + "## Appendix\n\n```bash\n# Looks like a heading, is not one\ngh api\n```\n"
        + "Uses `internal_only_flag`.\n"
    )
    assert shape_diff(headings(en_only), headings(_KO)) == []
    assert presence_diff(identifier_counts(en_only), identifier_counts(_KO)) == []


def test_bare_marker_does_not_exempt_a_section() -> None:
    """A reasonless marker must leave the section under both checks."""
    en_only = (
        _EN
        + f"\n{BARE_MARKER}\n"
        + "## Appendix\n\nSee `internal_only_flag`.\n"
    )
    assert any("Appendix" in line for line in shape_diff(headings(en_only), headings(_KO)))
    assert presence_diff(identifier_counts(en_only), identifier_counts(_KO)) == [
        "  internal_only_flag: README.md x1, README.ko.md x0"
    ]


def test_bare_marker_is_reported_at_its_line() -> None:
    assert malformed_markers(f"# Title\n\n{BARE_MARKER}\n\n## Appendix\n") == [
        f"  line 3: {BARE_MARKER}"
    ]


def test_marker_with_a_reason_is_not_malformed() -> None:
    reason = _marker("English-only appendix, no Korean equivalent planned")
    assert malformed_markers(f"# Title\n\n{reason}\n\n## Appendix\n") == []


def test_marker_reason_stays_inside_the_comment() -> None:
    """A reason placed after the comment close would render into the page."""
    outside = "<!-- translation-parity: ignore-section --> English-only appendix"
    assert malformed_markers(outside) == [f"  line 1: {outside}"]


def test_marker_inside_a_code_fence_is_not_honoured() -> None:
    """Documenting the hatch must not switch it on."""
    documented = (
        _EN
        + "\nHow to exempt a section:\n\n```markdown\n"
        + _marker("English-only appendix")
        + "\n```\n\n## Appendix\n\nSee `internal_only_flag`.\n"
    )
    assert any("Appendix" in line for line in shape_diff(headings(documented), headings(_KO)))
    assert presence_diff(identifier_counts(documented), identifier_counts(_KO)) == [
        "  internal_only_flag: README.md x1, README.ko.md x0"
    ]


def test_bare_marker_inside_a_code_fence_is_not_malformed() -> None:
    """The mirror of the case above: an example must not be a violation."""
    assert malformed_markers(f"# Title\n\n```markdown\n{BARE_MARKER}\n```\n") == []
