r"""Tests for the claude-review composite's ``allowed_bots`` wiring (AT-2272).

A PR opened under the ``ignite-actions-token-app`` App installation token was
rejected by the pinned ``anthropics/claude-code-action`` with "Workflow
initiated by non-human actor" because the actor was not in the composite's
hardcoded ``allowed_bots`` list, permanently blocking merge on any consumer
whose branch-protection quorum needs the Claude review to complete
(``ai-dev-cab#482``).

This module does not import production code -- there is none for this axis,
``allowed_bots`` is pure YAML configuration -- so it instead:

1. Mirrors the comparison the pinned action actually performs
   (``is_allowed_bot`` below), verified against
   ``anthropics/claude-code-action@e5ad3c7725bc2459721893f88879fef9dbcf97b0``
   (the SHA pinned in ``action.yml``), file
   ``src/github/validation/actor.ts``, function ``isAllowedBot``:

   .. code-block:: typescript

      function isAllowedBot(actor: string, allowedBots: string): boolean {
        const trimmed = allowedBots.trim();
        if (trimmed === "*") return true;
        if (!trimmed) return false;

        const allowedList = trimmed
          .split(",")
          .map((bot) =>
            bot
              .trim()
              .toLowerCase()
              .replace(/\[bot\]$/, ""),
          )
          .filter((bot) => bot.length > 0);

        const normalizedActor = actor.toLowerCase().replace(/\[bot\]$/, "");
        return allowedList.includes(normalizedActor);
      }

   Both the list entries and the actor are lowercased and stripped of a
   trailing ``[bot]`` before comparison, so the suffix is cosmetic on either
   side -- an entry can carry it or not, and either form matches an actor
   presented with or without it.

2. Asserts the composite's default ``allowed_bots`` value, parsed with that
   comparison, actually admits ``ignite-actions-token-app`` (the regression
   this ticket fixes) alongside the three bots it already admitted, and
   still rejects an arbitrary bot (no accidental ``'*'``).

3. Asserts ``allowed_bots`` is a composite *input* (consumed via
   ``${{ inputs.allowed_bots }}``), not a hardcoded literal in the `with:`
   block -- otherwise a consumer could not override the list without
   changing the pinned SHA, which was the second half of this fix. An
   override replaces the default wholesale (GitHub Actions inputs don't
   merge), so a consumer adding one bot must pass the complete list.

4. Asserts the caller's (``base-ai-review-single.yml``) fallback default,
   used when ``vars.CLAUDE_ALLOWED_BOTS`` is unset, is byte-for-byte the same
   string as the composite's own default -- so the two copies of "today's
   enumerated bots" cannot silently drift apart the way the wrapper's inline
   reimplementation already can (see ``check_base_wrapper_drift.py``, which
   does not cover ``with:`` inputs).
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[3]
ACTION_YML = REPO_ROOT / ".github" / "actions" / "claude-review" / "action.yml"
SINGLE_YML = REPO_ROOT / ".github" / "workflows" / "base-ai-review-single.yml"
README_MD = REPO_ROOT / "README.md"
README_KO_MD = REPO_ROOT / "README.ko.md"

_BOT_SUFFIX_RE = re.compile(r"\[bot\]$")


def is_allowed_bot(actor: str, allowed_bots: str) -> bool:
    """Port of the pinned action's ``isAllowedBot`` (see module docstring)."""
    trimmed = allowed_bots.strip()
    if trimmed == "*":
        return True
    if not trimmed:
        return False

    allowed_list = [
        _BOT_SUFFIX_RE.sub("", bot.strip().lower())
        for bot in trimmed.split(",")
    ]
    allowed_list = [bot for bot in allowed_list if bot]

    normalized_actor = _BOT_SUFFIX_RE.sub("", actor.lower())
    return normalized_actor in allowed_list


class TestIsAllowedBotPort:
    """Pin the ported comparison's behavior against the format variants that
    make this class of bug easy to get wrong silently (AT-2272, and the
    general gh-bot-author-format-trap: three different formats for the same
    bot depending on how you ask)."""

    ALLOWED = "dependabot[bot],pilot-cd-dispatcher[bot],github-actions[bot]"

    @pytest.mark.parametrize(
        "actor",
        [
            "dependabot[bot]",
            "dependabot",  # suffix optional on the actor side
            "Dependabot[BOT]",  # case-insensitive
        ],
    )
    def test_matches_list_entry_regardless_of_suffix_or_case(self, actor: str) -> None:
        assert is_allowed_bot(actor, self.ALLOWED)

    def test_list_entry_with_surrounding_whitespace_still_matches(self) -> None:
        assert is_allowed_bot("dependabot[bot]", " dependabot[bot] ,github-actions[bot]")

    def test_entry_without_suffix_still_matches_actor_with_suffix(self) -> None:
        # AT-2272's own fix relies on this: an entry with no "[bot]" and an
        # actor with one (or vice versa) normalize to the same string.
        assert is_allowed_bot("ignite-actions-token-app[bot]", "ignite-actions-token-app")
        assert is_allowed_bot("ignite-actions-token-app", "ignite-actions-token-app[bot]")

    def test_unlisted_bot_is_rejected(self) -> None:
        assert not is_allowed_bot("codex[bot]", self.ALLOWED)

    def test_empty_list_rejects_everything(self) -> None:
        assert not is_allowed_bot("dependabot[bot]", "")

    def test_star_admits_anything(self) -> None:
        assert is_allowed_bot("literally-anything[bot]", "*")

    def test_comma_is_the_only_separator(self) -> None:
        # A semicolon-separated value (an easy typo) parses as one entry and
        # matches nothing real -- this is the "wrong form fails silently"
        # trap the ticket warned about.
        assert not is_allowed_bot("dependabot[bot]", "dependabot[bot];github-actions[bot]")


def _load_yaml(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


_README_ROW_RE = re.compile(
    r"^\| `CLAUDE_ALLOWED_BOTS` \| `(?P<default>[^`]+)` \|", re.MULTILINE
)


def _readme_default(path: Path) -> str:
    """The default literal from the `CLAUDE_ALLOWED_BOTS` vars-table row."""
    match = _README_ROW_RE.search(path.read_text(encoding="utf-8"))
    assert match, f"no CLAUDE_ALLOWED_BOTS row found in {path}"
    return match.group("default")


class TestCompositeDefault:
    def test_allowed_bots_is_declared_as_an_input(self) -> None:
        doc = _load_yaml(ACTION_YML)
        assert "allowed_bots" in doc["inputs"], (
            "allowed_bots must be a composite input, not a literal baked "
            "into the `with:` block, so a consumer can override it without "
            "a new SHA pin (AT-2272)"
        )

    def test_step_consumes_the_input_not_a_literal(self) -> None:
        doc = _load_yaml(ACTION_YML)
        step = doc["runs"]["steps"][0]
        assert step["with"]["allowed_bots"] == "${{ inputs.allowed_bots }}"

    def test_default_admits_the_regressed_actor(self) -> None:
        doc = _load_yaml(ACTION_YML)
        default = doc["inputs"]["allowed_bots"]["default"]
        assert is_allowed_bot("ignite-actions-token-app", default), (
            "the AT-2272 regression: ignite-actions-token-app must be "
            "admitted by the composite's default allowed_bots"
        )
        # Ensure the fix is additive, not a swap-in that dropped an existing
        # entry.
        for existing in ("dependabot[bot]", "pilot-cd-dispatcher[bot]", "github-actions[bot]"):
            assert is_allowed_bot(existing, default), f"{existing} must remain admitted"

    def test_default_does_not_admit_everything(self) -> None:
        doc = _load_yaml(ACTION_YML)
        default = doc["inputs"]["allowed_bots"]["default"]
        assert default.strip() != "*"
        assert not is_allowed_bot("some-other-bot[bot]", default)


class TestCallerParity:
    """The caller's fallback default must not drift from the composite's own
    default -- two copies of the same list is exactly the shape that goes
    stale (see AGENTS.md "One rule, one home")."""

    def test_single_yml_fallback_matches_composite_default(self) -> None:
        action_doc = _load_yaml(ACTION_YML)
        composite_default = action_doc["inputs"]["allowed_bots"]["default"]

        single_doc = _load_yaml(SINGLE_YML)
        steps = single_doc["jobs"]["review"]["steps"]
        claude_step = next(s for s in steps if s.get("id") == "claude-review")
        expression = claude_step["with"]["allowed_bots"]

        match = re.fullmatch(
            r"\$\{\{ vars\.CLAUDE_ALLOWED_BOTS \|\| '(?P<default>.*)' \}\}",
            expression,
        )
        assert match, f"unexpected allowed_bots expression shape: {expression!r}"
        assert match.group("default") == composite_default


class TestReadmeParity:
    """The `CLAUDE_ALLOWED_BOTS` vars-table row in each README documents the
    default as a literal string -- a fourth copy alongside the composite
    default and the caller's fallback (TestCallerParity above), and one
    check_base_wrapper_drift.py cannot see because it only compares the
    workflow YAML files, not README.md/README.ko.md. Un-guarded, this is
    exactly the shape AGENTS.md "One rule, one home" warns about: a doc row
    that quietly stops matching the code it describes."""

    def test_readme_default_matches_composite_default(self) -> None:
        action_doc = _load_yaml(ACTION_YML)
        composite_default = action_doc["inputs"]["allowed_bots"]["default"]
        assert _readme_default(README_MD) == composite_default, (
            "README.md's CLAUDE_ALLOWED_BOTS row has drifted from "
            "action.yml's allowed_bots default"
        )

    def test_readme_ko_default_matches_composite_default(self) -> None:
        action_doc = _load_yaml(ACTION_YML)
        composite_default = action_doc["inputs"]["allowed_bots"]["default"]
        assert _readme_default(README_KO_MD) == composite_default, (
            "README.ko.md's CLAUDE_ALLOWED_BOTS row has drifted from "
            "action.yml's allowed_bots default"
        )
