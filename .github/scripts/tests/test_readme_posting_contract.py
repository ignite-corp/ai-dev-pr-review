"""No README example reads PR comments to gate a merge (AT-2597).

A comment never carries a merge decision: the ruleset's required check and
required approval do. A gate built on a comment can be spoofed by anyone
who can comment, and binds to no head. These tests read the whole of both
READMEs and fail when any code example in them reads PR comments, keys on
the verdict marker, or reads review state or a check conclusion without
binding it to the PR head.

Not caught:
- Anything outside a fenced block. Fences are found with the parity
  guard's own FENCE pattern, so backtick and tilde fences count, indented
  or not; a four-space indented code block does not.
- Prose. Only the withdrawn round-1 sentence is refused, by
  test_contract_section_does_not_call_the_comment_a_gate; other wording
  that recommends a comment gate passes.
- Head binding is checked by presence: a block that names one of
  HEAD_BINDINGS passes, whether or not it compares it to the review's
  commit or the check's head.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from github_pr_support import REVIEW_MARKER
from test_readme_translation_parity import FENCE

REPO_ROOT = Path(__file__).resolve().parents[3]
READMES = ("README.md", "README.ko.md")
SECTIONS = {
    "README.md": "## Verdict posting contract",
    # Escaped: test_ascii_only keeps .github ASCII.
    "README.ko.md": "## \ud310\uc815 \uac8c\uc2dc \uacc4\uc57d",
}

# Ways a snippet reads PR comments: GraphQL connections and REST endpoints.
COMMENT_READS = (
    re.compile(r"\bcomments\s*\("),
    re.compile(r"issues/[^\s/]+/comments"),
    re.compile(r"\bgh\s+pr\s+view\b[^\n]*--json[^\n]*\bcomments\b"),
)
# Ways a snippet reads a verdict-bearing state: reviews or check runs.
STATE_READS = (
    re.compile(r"\breviews\s*\("),
    re.compile(r"\blatestOpinionatedReviews\b"),
    re.compile(r"pulls/[^\s/]+/reviews"),
    re.compile(r"check-runs|checkSuites|statusCheckRollup|\bgh\s+pr\s+checks\b"),
)
HEAD_BINDINGS = ("headRefOid", "HEAD_SHA", "head.sha", "head_sha")


def _section(text: str, heading: str) -> str:
    start = text.index(heading + "\n")
    end = text.find("\n## ", start + len(heading))
    return text[start : end if end != -1 else len(text)]


def _fenced_blocks(section: str) -> list[str]:
    """The bodies of the fenced blocks in a section, an unclosed one included."""
    blocks: list[str] = []
    current: list[str] | None = None
    for line in section.splitlines():
        if FENCE.match(line):
            if current is None:
                current = []
            else:
                blocks.append("\n".join(current))
                current = None
        elif current is not None:
            current.append(line)
    if current is not None:
        blocks.append("\n".join(current))
    return blocks


def comment_gate_problems(section: str) -> list[str]:
    """What in this text reads a gate off a comment."""
    problems: list[str] = []
    for block in _fenced_blocks(section):
        problems += [
            f"reads comments: {p.pattern}" for p in COMMENT_READS if p.search(block)
        ]
        if REVIEW_MARKER in block:
            problems.append("keys on the verdict marker")
        if any(p.search(block) for p in STATE_READS) and not any(
            binding in block for binding in HEAD_BINDINGS
        ):
            problems.append("reads review or check state without binding the head")
    return problems


@pytest.fixture(params=READMES, ids=str)
def readme(request: pytest.FixtureRequest) -> str:
    return (REPO_ROOT / request.param).read_text(encoding="utf-8")


@pytest.fixture(params=sorted(SECTIONS), ids=str)
def section(request: pytest.FixtureRequest) -> str:
    text = (REPO_ROOT / request.param).read_text(encoding="utf-8")
    return _section(text, SECTIONS[request.param])


def test_readme_prescribes_no_comment_gate(readme: str) -> None:
    assert comment_gate_problems(readme) == []


def test_contract_section_does_not_call_the_comment_a_gate(section: str) -> None:
    # The round-1 rule this section withdrew, in either language.
    assert "Gate on the body" not in section
    assert "\ubcf8\ubb38\uc73c\ub85c \uac8c\uc774\ud2b8" not in section


# Negative controls: the checker must see each shape it exists to refuse,
# or a clean pass above means nothing.

ROUND_ONE_SNIPPET = """## Verdict posting contract

```bash
gh api graphql -f query='
query { repository(owner: "o", name: "r") { pullRequest(number: 1) {
  comments(last: 100) { nodes { body isMinimized } }
  reviews(last: 100) { nodes { body state commit { oid } } }
} } }' --jq 'map(select(.body | startswith("<!-- multi-llm-review -->")))'
```
"""


def test_checker_refuses_the_withdrawn_marker_gate() -> None:
    problems = comment_gate_problems(ROUND_ONE_SNIPPET)
    assert any(p.startswith("reads comments") for p in problems)
    assert "keys on the verdict marker" in problems
    assert "reads review or check state without binding the head" in problems


@pytest.mark.parametrize(
    "snippet",
    [
        "gh api repos/o/r/issues/1/comments --jq '.[].body'",
        "gh pr view 1 --json comments --jq '.comments'",
    ],
)
def test_checker_refuses_rest_comment_reads(snippet: str) -> None:
    assert comment_gate_problems(f"```bash\n{snippet}\n```\n")


@pytest.mark.parametrize(
    ("opening", "closing"),
    [("~~~bash", "~~~"), ("   ```bash", "   ```"), ("  ~~~", "  ~~~")],
    ids=["tilde", "indented-backtick", "indented-tilde"],
)
def test_checker_sees_every_fence_the_parity_guard_sees(
    opening: str, closing: str
) -> None:
    snippet = "gh api repos/o/r/issues/1/comments --jq '.[].body'"
    assert comment_gate_problems(f"{opening}\n{snippet}\n{closing}\n")


def test_checker_accepts_head_bound_review_state() -> None:
    snippet = (
        "```bash\n"
        'gh api graphql -f query=\'query { repository(owner: "o", name: "r") {\n'
        "  pullRequest(number: 1) { headRefOid\n"
        "    reviews(last: 100) { nodes { state commit { oid } } } } } }' \\\n"
        "  --jq '.data.repository.pullRequest as $p\n"
        "    | [$p.reviews.nodes[] | select(.commit.oid == $p.headRefOid)]'\n"
        "```\n"
    )
    assert comment_gate_problems(snippet) == []
