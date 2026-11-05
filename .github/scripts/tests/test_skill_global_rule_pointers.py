"""The skill points at global rules instead of restating them (AT-3203).

Two rules the pr-response-cycle skill used to carry in its own words now
have one home in the global AGENTS.md: which language an artifact is
written in (``### Writing language``, Non-Negotiable #2, v3.18.0) and
whether spawning a teammate needs confirmation (``### Agent structure``,
item 4, v3.17.0). The skill's old copies said "English only" for PR bodies,
which the global rule no longer says, and cited ``feedback_spawn_freely.md``,
a memory file that exists in one devcontainer and nowhere else. These
tests keep the copies out and the pointers in, and keep the skill's count
of the memory entries it lists equal to the list.
"""

from __future__ import annotations

import re

from tests_support import SKILL

_COUNT_WORDS = {
    w: n
    for n, w in enumerate(
        "one two three four five six seven eight nine ten eleven twelve".split(), 1
    )
}
_LANGUAGE = "`### Writing language`"
_SPAWN = "`### Agent structure`, item 4"


def _line(text: str, prefix: str) -> str:
    """The one line of text that starts with prefix."""
    lines = [ln for ln in text.splitlines() if ln.startswith(prefix)]
    assert len(lines) == 1, (prefix, lines)
    return lines[0]


def _skill() -> str:
    return SKILL.read_text(encoding="utf-8")


def test_skill_does_not_restate_the_writing_language_rule() -> None:
    """No "English only" copy; the language guidance names the global section."""
    text = _skill()
    assert "English only" not in text
    # Each place the old copy stood now carries the pointer.
    assert _LANGUAGE in _line(text, "**Language**:")
    assert _LANGUAGE in _line(text, "- Non-Negotiables #1 (no AI attribution)")
    assert _LANGUAGE in _line(text, "- **Choosing an artifact's language")


def test_skill_cites_agent_structure_not_a_memory_file_for_spawning() -> None:
    """The spawning rule is the global section, not a file only one machine holds."""
    text = _skill()
    assert "feedback_spawn_freely" not in text
    # Each place the old citation stood now carries the pointer.
    assert _SPAWN in _line(
        text, "Per AGENTS.md Non-Negotiable #7, leader must NOT edit"
    )
    assert _SPAWN in _line(text, "- **Asking about a spawn the rule already settles**")


def test_memory_entry_count_matches_the_list() -> None:
    """The line "All <n> are qa-partner's" counts the entries listed under it."""
    text = _skill()
    head = re.search(r"\*\*All (\w+) are qa-partner's\*\*", text)
    assert head is not None
    start = text.index("\n", head.end()) + 1
    entries = re.findall(
        r"^- `[a-z_]+\.md` \u2014 ", text[start : text.index("\n\n", start)], re.M
    )
    word = head.group(1).lower()
    assert word in _COUNT_WORDS, f"unknown count word {word!r}; extend _COUNT_WORDS"
    assert _COUNT_WORDS[word] == len(entries), (word, entries)
