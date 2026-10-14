"""Tests for verify_action_shas: diff parsing and SHA verification.

Test-module hygiene: **all imports belong at the top of this file per PEP 8**
-- do not add `import foo` statements inside test function bodies. Every prior
reviewer round has flagged in-body imports; keep them here so the next round
does not reopen the same nit.
"""

from __future__ import annotations

import ast
import json
import os
import subprocess
import sys
import textwrap
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from verify_action_shas import (
    _SHA1_HEX_LEN,
    RawPin,
    ResultPin,
    TagCache,
    UnmatchedPin,
    UnverifiedPin,
    VerifiedPin,
    _fetch_tags,
    _parse_pins,
    _render_xml,
    _verify_pin,
    main,
)

# ---------------------------------------------------------------------------
# Test-data constants
# ---------------------------------------------------------------------------

# Real commit SHAs from public GitHub Actions repos -- used across multiple
# test classes, so they live at module level to avoid per-class duplication.
CHECKOUT_SHA = "34e114876b0b11c390a56381ad16ebd13914f8d5"  # actions/checkout v4.3.1
CHECKOUT_V4_3_0_SHA = (
    "08eba0b27e820071cde6df949e0beb9ba4906955"  # actions/checkout v4.3.0
)
SETUP_PYTHON_SHA = (
    "a26af69be951a213d495a4c3e4e4022e16d87065"  # actions/setup-python v5.6.0
)

# Matches the ``timeout=`` the production code hands to ``subprocess.run`` --
# mirrored here so the ``TimeoutExpired`` fixture stays honest if the real
# timeout ever changes.
_SUBPROCESS_TIMEOUT_SEC = 60


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _completed(
    stdout: str = "", returncode: int = 0, stderr: str = ""
) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(
        args=[], returncode=returncode, stdout=stdout, stderr=stderr
    )


def _tags_payload(tags: list[dict[str, Any]]) -> str:
    return json.dumps(tags)


# ---------------------------------------------------------------------------
# _parse_pins
# ---------------------------------------------------------------------------


class TestParsePins:
    def test_single_pin_with_comment(self) -> None:
        diff = f"+      - uses: actions/checkout@{CHECKOUT_SHA}  # v4.3.1\n"
        pins = _parse_pins(diff)
        assert len(pins) == 1
        assert pins[0]["repo"] == "actions/checkout"
        assert pins[0]["sha"] == CHECKOUT_SHA
        assert pins[0]["comment"] == "v4.3.1"

    def test_multiple_pins_deduplicated(self) -> None:
        diff = (
            f"+      - uses: actions/checkout@{CHECKOUT_SHA}  # v4.3.1\n"
            f"+      - uses: actions/setup-python@{SETUP_PYTHON_SHA}  # v5.6.0\n"
            f"+      - uses: actions/checkout@{CHECKOUT_SHA}  # v4.3.1\n"
        )
        pins = _parse_pins(diff)
        assert len(pins) == 2
        repos = [p["repo"] for p in pins]
        assert "actions/checkout" in repos
        assert "actions/setup-python" in repos

    def test_ignores_header_lines(self) -> None:
        diff = (
            "+++ b/.github/workflows/ci.yml\n"
            f"+      - uses: actions/checkout@{CHECKOUT_SHA}\n"
        )
        pins = _parse_pins(diff)
        assert len(pins) == 1

    def test_ignores_removal_lines(self) -> None:
        diff = f"-      - uses: actions/checkout@{CHECKOUT_SHA}\n"
        pins = _parse_pins(diff)
        assert pins == []

    def test_handles_subpath_in_action_ref(self) -> None:
        sha = "aabbccdd" * 5
        diff = f"+      - uses: actions/upload-artifact/foo@{sha}  # v4.0.0\n"
        pins = _parse_pins(diff)
        assert len(pins) == 1
        assert pins[0]["repo"] == "actions/upload-artifact"
        assert pins[0]["sha"] == sha
        assert pins[0]["comment"] == "v4.0.0"

    def test_pin_without_comment(self) -> None:
        diff = f"+      - uses: actions/checkout@{CHECKOUT_SHA}\n"
        pins = _parse_pins(diff)
        assert len(pins) == 1
        assert pins[0]["comment"] is None

    def test_divergent_comments_on_same_sha_are_kept(self) -> None:
        """Same (repo, sha) with different trailing comments (e.g. ``# v4``
        alias and ``# v4.3.1`` specific tag) must produce two distinct pins so
        each comment is independently verified in the rendered output."""
        diff = (
            f"+      - uses: actions/checkout@{CHECKOUT_SHA}  # v4.3.1\n"
            f"+      - uses: actions/checkout@{CHECKOUT_SHA}  # v4\n"
            # Exact duplicate of the first line -- still collapses.
            f"+      - uses: actions/checkout@{CHECKOUT_SHA}  # v4.3.1\n"
        )
        pins = _parse_pins(diff)
        assert len(pins) == 2
        comments = sorted((p["comment"] or "") for p in pins)
        assert comments == ["v4", "v4.3.1"]


# ---------------------------------------------------------------------------
# _fetch_tags (cache behaviour)
# ---------------------------------------------------------------------------


class TestFetchTagsCache:
    def test_cache_hit_avoids_second_subprocess_call(self) -> None:
        payload = _tags_payload([{"name": "v1.0.0", "commit": {"sha": "aabbccdd" * 5}}])
        cache: TagCache = {}
        with patch(
            "verify_action_shas.subprocess.run", return_value=_completed(payload)
        ) as run_mock:
            first = _fetch_tags("actions/checkout", cache)
            second = _fetch_tags("actions/checkout", cache)
        assert first == second
        assert run_mock.call_count == 1

    def test_cached_error_is_returned_without_retry(self) -> None:
        cache: TagCache = {}
        with patch(
            "verify_action_shas.subprocess.run",
            return_value=_completed(returncode=1, stderr="rate limit exceeded"),
        ) as run_mock:
            first = _fetch_tags("actions/checkout", cache)
            second = _fetch_tags("actions/checkout", cache)
        assert first == (None, "rate-limit")
        assert second == (None, "rate-limit")
        assert run_mock.call_count == 1

    def test_malformed_pagination_output_surfaces_decode_error(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """If ``gh api --paginate`` emits syntactically broken JSON on a later
        page, callers must not silently consume a truncated tag list. The
        error label is propagated so ``_verify_pin`` routes the pin through
        the ``unverified`` path, and a ``::warning::`` is emitted to
        stderr."""
        # First page decodes; second page is malformed partway through.
        malformed = (
            _tags_payload([{"name": "v1.0.0", "commit": {"sha": "aabbccdd" * 5}}])
            + '[{"name":"v1.0.1","commit":{"sha":"broken'
        )
        cache: TagCache = {}
        with patch(
            "verify_action_shas.subprocess.run",
            return_value=_completed(malformed),
        ):
            tags, error = _fetch_tags("actions/checkout", cache)
        assert tags is None
        assert error == "json-decode-error"
        captured = capsys.readouterr()
        assert "::warning::" in captured.err
        assert "JSON decode failed" in captured.err

    def test_http_404_via_stderr_is_recognised_as_a_404(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """gh api reports HTTP 404 through stderr and exits with returncode=1
        (not 404). The stderr-token parser must still recognise the 404
        rather than falling through to the generic ``api-error``.

        The owner is pinned out-of-org here so this test measures only the
        stderr parse. Which label a recognised 404 earns is the owner's
        business, covered by ``TestFetchTags404Discrimination``.
        """
        monkeypatch.setenv("GITHUB_REPOSITORY", _RUN_REPO)
        cache: TagCache = {}
        with patch(
            "verify_action_shas.subprocess.run",
            return_value=_completed(
                returncode=1, stderr="gh: HTTP 404: Not Found (api.github.com/...)"
            ),
        ):
            tags, error = _fetch_tags("actions/bogus-repo", cache)
        assert tags is None
        assert error == "repo-not-found"

    def test_http_403_via_stderr_maps_to_rate_limit(self) -> None:
        cache: TagCache = {}
        with patch(
            "verify_action_shas.subprocess.run",
            return_value=_completed(
                returncode=1, stderr="HTTP 403: API rate limit exceeded for user"
            ),
        ):
            tags, error = _fetch_tags("actions/checkout", cache)
        assert tags is None
        assert error == "rate-limit"

    def test_generic_api_error_preserves_stderr_excerpt(self) -> None:
        """Any non-recognised failure surfaces the stderr excerpt so the
        reviewer prompt can see the actual failure mode rather than a bare
        ``api-error`` label."""
        cache: TagCache = {}
        with patch(
            "verify_action_shas.subprocess.run",
            return_value=_completed(
                returncode=1, stderr="gh: connection refused by api.github.com"
            ),
        ):
            tags, error = _fetch_tags("actions/checkout", cache)
        assert tags is None
        assert error is not None
        assert error.startswith("api-error:")
        assert "connection refused" in error

    def test_whitespace_only_stderr_does_not_crash(self) -> None:
        """``splitlines()`` on ``"  \\n  "`` returns ``[]`` -- a naive
        ``[0]`` index would raise IndexError. Guard so whitespace-only
        stderr still classifies as a plain ``api-error`` rather than
        crashing the verifier."""
        cache: TagCache = {}
        with patch(
            "verify_action_shas.subprocess.run",
            return_value=_completed(returncode=1, stderr="  \n  "),
        ):
            tags, error = _fetch_tags("actions/checkout", cache)
        assert tags is None
        assert error == "api-error"

    def test_generic_not_found_stderr_is_not_repo_not_found(self) -> None:
        """Bare ``not found`` substrings from unrelated CLI failures
        (e.g. credential store, file lookup) must not be misclassified
        as a missing upstream repo. Only full ``HTTP 404`` tokens map
        to ``repo-not-found``."""
        cache: TagCache = {}
        with patch(
            "verify_action_shas.subprocess.run",
            return_value=_completed(
                returncode=1, stderr="credentials not found in keychain"
            ),
        ):
            tags, error = _fetch_tags("actions/checkout", cache)
        assert tags is None
        assert error is not None
        assert error != "repo-not-found"
        assert error.startswith("api-error:")
        assert "credentials not found" in error


# ---------------------------------------------------------------------------
# _fetch_tags (404 cause discrimination, AT-2801)
# ---------------------------------------------------------------------------


# The exact stderr the live CLI emits for a 404, captured from
# ``gh api repos/<org>/<absent-repo>/tags --paginate`` (exit code 1). The
# same response comes back for a private repo the token may not see: GitHub
# refuses to disclose existence and answers 404, not 403, which is the whole
# reason the two causes cannot be told apart at this layer.
_GH_404_STDERR = "gh: Not Found (HTTP 404)"

_RUN_ORG = "ignite-corp"
_RUN_REPO = f"{_RUN_ORG}/ai-dev-pr-review"
# Private, and in the run's own org when the run is an ``ignite-corp`` one.
# Named here because it is already named throughout this public tree (README,
# MIGRATION.md, consumer-health.yml), and it is a real pin: it is reachable
# as a reusable workflow from inside ignite-corp today.
_IN_ORG_PRIVATE = f"{_RUN_ORG}/ai-dev-infra-common"
# The SAME private repository, seen from a run that is not in its org -- a
# pilot-fleet consumer, say. Nothing about the repository changed; only the
# run did, which is why the out-of-org branch cannot assume the pin is public.
_OUT_OF_ORG_RUN_REPO = "ignite-pilot-org/max-builder"
# Visibly synthetic: the API 404s before any SHA comparison, so the value is
# inert here. AT-2801 records the real measured pins.
_IN_ORG_PRIVATE_SHA = "a" * _SHA1_HEX_LEN
_OUT_OF_ORG_ABSENT = "actions/no-such-action-xyzzy"


def _pin_rows(context_md: str) -> list[str]:
    """Return the ``<pin .../>`` rows of the rendered section.

    The blurb above the rows quotes every ``error="..."`` label it governs,
    so a whole-document substring check cannot tell a pin's own label from
    the prompt's description of it.
    """
    return [line.strip() for line in context_md.splitlines() if "<pin " in line]


def _severity_rule_for(document: str, error_label: str) -> str:
    """Return the one prompt bullet in ``document`` governing a label.

    Both guidance surfaces -- the blurb ``main()`` writes into ``context.md``
    and the consumer template ``examples/prompts/code-review-system.md`` --
    are lists of one-line bullets; the reviewer reads the bullet carrying
    its pin's ``error="..."`` and obeys it. Asking for exactly one keeps a
    test from passing because no bullet matched.
    """
    bullets = [
        line
        for line in document.splitlines()
        if line.startswith("- ") and f'error="{error_label}"' in line
    ]
    assert len(bullets) == 1, (
        f"expected exactly one prompt bullet for error={error_label!r}, "
        f"found {len(bullets)}"
    )
    return bullets[0]


# The premise each 404 label establishes -- no more -- worded so one
# substring fits every surface that states it: the ``main()`` blurb, the
# consumer template, and (for ``repo-not-found``) MIGRATION.md's
# restatement. Each value carries no markdown emphasis and no dashes, so
# the same bytes can appear in a Python string literal and in markdown.
#
# ``_classify_404`` compares owner strings and nothing else. It returns
# ``repo-not-found`` for a differing owner AND whenever the run's own owner
# is unknown (``test_unknown_run_repository_keeps_repo_not_found``,
# ``test_malformed_run_repository_keeps_repo_not_found``), so a surface
# saying only "outside the organisation" claims what the classifier did not
# observe. ``repo-not-visible`` is the same trap mirrored: owner equality
# is not org membership, and the matching owner may be a personal account,
# so "INSIDE the organisation" is equally unsupported.
#
# Keyed per label rather than guarded by one ``if``, because a premise
# check that covers a single label exempts every other bullet from having
# one -- which is how the ``repo-not-visible`` wording passed this helper
# on severity and absence alone. Lookup is by subscript on purpose: a new
# label reaching ``_assert_404_rule_is_honest`` without a premise here
# raises ``KeyError`` instead of silently skipping the check.
_404_PREMISES = {
    "repo-not-found": (
        "a repository whose owner is not the owner of the repository under "
        "review, or the run's own owner could not be determined"
    ),
    "repo-not-visible": (
        "a repository whose owner is the owner of the repository under review"
    ),
}


def _assert_404_rule_is_honest(document: str, label: str) -> None:
    """Assert ``document``'s bullet for ``label`` says only what a 404 supports.

    A 404 separates neither cause, so the bullet may not escalate and may
    not assert absence -- and, per AT-2801's accepted consequence, it has to
    stay actionable: a misspelled or deleted ``owner/repo`` produces this
    same label, and a bullet that only reassures leaves that pin to fail at
    runtime with no finding raised.

    One contract, one home. The rendered blurb and the consumer template
    carry the same guidance in two files, so they are held here by the same
    function: two copies of these assertions drifted apart once already,
    the looser one accepting a bullet worded "escalate beyond
    ``suggestion``" that the stricter one rejected. The strict phrasing is
    the one kept -- ``suggestion`` appearing anywhere in a bullet does not
    show the bullet confines the reviewer to it.

    Every label's bullet is also held to its own premise, from
    ``_404_PREMISES``. Both bullet-carrying surfaces pass through here;
    MIGRATION.md restates the ``repo-not-found`` premise outside a severity
    bullet, so it is held to the same entry by
    ``TestMigrationDocReachesOnboardedConsumers`` instead.
    """
    rule = _severity_rule_for(document, label)
    assert "Raise only as `suggestion`." in rule
    assert "`major`" not in rule
    assert "NOT claim the repository does not exist" in rule
    assert "confirm the `owner/repo` spelling" in rule
    assert _404_PREMISES[label] in rule, (
        f"the {label} bullet must state only the owner relation "
        f"_classify_404 establishes, not org membership: {_404_PREMISES[label]!r}"
    )
    # Positive control: `major` is still reachable in this same document --
    # the `unmatched` bullet carries it -- so "no major" above cannot be an
    # artefact of reading an empty, truncated, or wrong document.
    assert "Raise as `major`" in document


class TestFetchTags404Discrimination:
    """AT-2801: a 404 does not establish that the repository is absent.

    GitHub answers 404 rather than 403 for a private repository the token
    may not see, so ``absent`` and ``present but invisible`` arrive as the
    same status code. Mapping every 404 to ``repo-not-found`` made the
    prompt assert non-existence the classifier could not support, and the
    reviewers obeyed it: a consensus ``major`` on every PR pinning a private
    org repo, which withheld approval. AT-2801 records the measurement.
    """

    def test_in_org_404_is_not_repo_not_found(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A 404 for a repo in the run's own org must not claim absence."""
        monkeypatch.setenv("GITHUB_REPOSITORY", _RUN_REPO)
        cache: TagCache = {}
        with patch(
            "verify_action_shas.subprocess.run",
            return_value=_completed(returncode=1, stderr=_GH_404_STDERR),
        ):
            tags, error = _fetch_tags(_IN_ORG_PRIVATE, cache)
        assert tags is None
        assert error != "repo-not-found"
        assert error == "repo-not-visible"

    def test_in_org_owner_match_ignores_case(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """GitHub owner names are case-insensitive, so the comparison is."""
        monkeypatch.setenv("GITHUB_REPOSITORY", "Ignite-Corp/Ai-Dev-Pr-Review")
        cache: TagCache = {}
        with patch(
            "verify_action_shas.subprocess.run",
            return_value=_completed(returncode=1, stderr=_GH_404_STDERR),
        ):
            _, error = _fetch_tags("IGNITE-CORP/ai-dev-infra-common", cache)
        assert error == "repo-not-visible"

    def test_out_of_org_404_still_repo_not_found(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A 404 for another owner keeps the out-of-org label.

        The label reports the owner relation, not a cause: see
        ``test_out_of_org_label_does_not_discriminate_private_from_absent``
        for the pair that proves it cannot mean "absent".
        """
        monkeypatch.setenv("GITHUB_REPOSITORY", _RUN_REPO)
        cache: TagCache = {}
        with patch(
            "verify_action_shas.subprocess.run",
            return_value=_completed(returncode=1, stderr=_GH_404_STDERR),
        ):
            tags, error = _fetch_tags(_OUT_OF_ORG_ABSENT, cache)
        assert tags is None
        assert error == "repo-not-found"

    def test_out_of_org_label_does_not_discriminate_private_from_absent(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The out-of-org label cannot mean "the repository is absent".

        ``ignite-corp/ai-dev-infra-common`` exists and is private;
        ``actions/no-such-action-xyzzy`` does not exist at all. Measured
        against the live API, their tag-listing 404s are byte-identical down
        to the headers, so both arrive here as the same ``_GH_404_STDERR``
        and both must land on the same label. A reviewer instruction keyed
        on that label therefore cannot assert absence of either one.
        """
        monkeypatch.setenv("GITHUB_REPOSITORY", _OUT_OF_ORG_RUN_REPO)
        cache: TagCache = {}
        with patch(
            "verify_action_shas.subprocess.run",
            return_value=_completed(returncode=1, stderr=_GH_404_STDERR),
        ):
            _, private_error = _fetch_tags(_IN_ORG_PRIVATE, cache)
            _, absent_error = _fetch_tags(_OUT_OF_ORG_ABSENT, cache)
        assert private_error == absent_error == "repo-not-found"

    def test_unknown_run_repository_keeps_repo_not_found(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """With no ``GITHUB_REPOSITORY`` nothing establishes membership.

        Both runners that invoke this script set it (the Actions runner by
        default, ``review_pr_local.py`` through ``script_env``). An ad-hoc
        run without it cannot show the repo is in-org, so the out-of-org
        label stands rather than the in-org one being handed out on a
        guess. Neither is the stricter of the two -- both bullets end in
        ``Raise only as `suggestion`.`` -- so this fallback picks a likelier
        cause, not a severity.
        """
        monkeypatch.delenv("GITHUB_REPOSITORY", raising=False)
        cache: TagCache = {}
        with patch(
            "verify_action_shas.subprocess.run",
            return_value=_completed(returncode=1, stderr=_GH_404_STDERR),
        ):
            _, error = _fetch_tags(_IN_ORG_PRIVATE, cache)
        assert error == "repo-not-found"

    def test_malformed_run_repository_keeps_repo_not_found(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A value with no ``owner/name`` split names no org either."""
        monkeypatch.setenv("GITHUB_REPOSITORY", _RUN_ORG)
        cache: TagCache = {}
        with patch(
            "verify_action_shas.subprocess.run",
            return_value=_completed(returncode=1, stderr=_GH_404_STDERR),
        ):
            _, error = _fetch_tags(_IN_ORG_PRIVATE, cache)
        assert error == "repo-not-found"


class TestRendered404Guidance:
    """The rendered ``context.md`` is what a reviewer actually obeys.

    ``_fetch_tags`` returning a different label is only half the fix: the
    bullet the reviewer reads for that label has to carry a severity the
    classifier can support. These run ``main()`` end to end and read the
    bullet the pin's own label selects.
    """

    def _run_main(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        repo: str,
        sha: str,
    ) -> str:
        (tmp_path / "pr.diff").write_text(f"+      - uses: {repo}@{sha}  # v1\n")
        (tmp_path / "context.md").write_text("")
        monkeypatch.chdir(tmp_path)
        with patch(
            "verify_action_shas.subprocess.run",
            return_value=_completed(returncode=1, stderr=_GH_404_STDERR),
        ):
            main()
        return (tmp_path / "context.md").read_text()

    def test_in_org_pin_does_not_reach_the_major_instruction(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("GITHUB_REPOSITORY", _RUN_REPO)
        content = self._run_main(
            tmp_path, monkeypatch, _IN_ORG_PRIVATE, _IN_ORG_PRIVATE_SHA
        )
        (row,) = _pin_rows(content)
        assert f'repo="{_IN_ORG_PRIVATE}"' in row
        assert 'error="repo-not-found"' not in row
        assert 'error="repo-not-visible"' in row
        _assert_404_rule_is_honest(content, "repo-not-visible")

    def test_out_of_org_pin_does_not_reach_the_major_instruction(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The out-of-org bullet may not escalate either.

        The owner split is not a visibility test -- the run's credential is
        the repo-scoped ``GITHUB_TOKEN``, which cannot read a private
        repository on either side of it -- so a ``major`` here would hand a
        valid private pin the very finding AT-2801 removed, just to a
        consumer whose org differs from the pin's.
        """
        monkeypatch.setenv("GITHUB_REPOSITORY", _RUN_REPO)
        content = self._run_main(
            tmp_path, monkeypatch, _OUT_OF_ORG_ABSENT, CHECKOUT_SHA
        )
        (row,) = _pin_rows(content)
        assert f'repo="{_OUT_OF_ORG_ABSENT}"' in row
        assert 'error="repo-not-found"' in row
        _assert_404_rule_is_honest(content, "repo-not-found")

    def test_unknown_run_owner_bullet_does_not_promise_a_differing_owner(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An ad-hoc run with no ``GITHUB_REPOSITORY`` renders this bullet.

        The pin is the real in-org private repository and the run's owner is
        unknown, so ``_classify_404`` falls back to ``repo-not-found`` with
        nothing observed about the owner relation. A bullet that told the
        reviewer the owner is out-of-org would be asserting what this run
        could not see -- the AT-2801 defect, one scope narrower.
        """
        monkeypatch.delenv("GITHUB_REPOSITORY", raising=False)
        content = self._run_main(
            tmp_path, monkeypatch, _IN_ORG_PRIVATE, _IN_ORG_PRIVATE_SHA
        )
        (row,) = _pin_rows(content)
        assert f'repo="{_IN_ORG_PRIVATE}"' in row
        assert 'error="repo-not-found"' in row
        _assert_404_rule_is_honest(content, "repo-not-found")

    def test_private_pin_seen_from_another_org_is_not_called_absent(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """AT-2801's own case, replayed from a consumer in a different org.

        The pin is the real private ``ignite-corp/ai-dev-infra-common``; only
        the run's owner differs from the in-org test above. It takes the
        out-of-org label, so that label's bullet is what decides whether the
        AT-2801 false ``major`` comes back for cross-org consumers.
        """
        monkeypatch.setenv("GITHUB_REPOSITORY", _OUT_OF_ORG_RUN_REPO)
        content = self._run_main(
            tmp_path, monkeypatch, _IN_ORG_PRIVATE, _IN_ORG_PRIVATE_SHA
        )
        (row,) = _pin_rows(content)
        assert f'repo="{_IN_ORG_PRIVATE}"' in row
        assert 'error="repo-not-found"' in row
        _assert_404_rule_is_honest(content, "repo-not-found")


class TestExamplePromptCovers404Labels:
    """The in-tree consumer template must describe both 404 labels.

    ``examples/prompts/code-review-system.md`` is what a consumer copies
    into its own repo as the reviewer system prompt. A copy that knows only
    ``repo-not-found`` reinstates the AT-2801 false ``major`` on every
    consumer that onboarded from it.
    """

    _EXAMPLE_PROMPT = (
        Path(__file__).resolve().parents[3]
        / "examples"
        / "prompts"
        / "code-review-system.md"
    )

    def test_example_prompt_describes_repo_not_visible(self) -> None:
        text = self._EXAMPLE_PROMPT.read_text(encoding="utf-8")
        # Positive control: the label the template already carried.
        assert 'error="repo-not-found"' in text
        assert 'error="repo-not-visible"' in text, (
            "Consumer prompt template must carry the repo-not-visible rule "
            "the verifier now emits."
        )

    @pytest.mark.parametrize("label", ["repo-not-found", "repo-not-visible"])
    def test_example_prompt_404_rules_are_honest(self, label: str) -> None:
        """Neither 404 bullet may escalate or assert absence, and both must
        stay actionable -- literally the same contract
        ``TestRendered404Guidance`` holds the rendered blurb to, applied by
        the same function to the copy consumers take."""
        text = self._EXAMPLE_PROMPT.read_text(encoding="utf-8")
        _assert_404_rule_is_honest(text, label)


class TestMigrationDocReachesOnboardedConsumers:
    """Updating the template is not enough to reach an onboarded consumer.

    ``MIGRATION.md`` keeps ``.github/prompts/code-review-system.md`` in the
    consumer repo and never deletes it, so a consumer that onboarded before
    AT-2801 holds a prompt whose ``repo-not-found`` bullet still reads "does
    not exist ... Raise as ``major``" -- the AT-2801 defect, on exactly the
    repos this fix targets. A pin bump does not touch that file; only a PR
    in the consumer does. The upgrade step has to be written down where a
    consumer looks, which is ``MIGRATION.md``.
    """

    _MIGRATION = Path(__file__).resolve().parents[3] / "MIGRATION.md"

    def test_migration_tells_consumers_to_recopy_both_404_rules(self) -> None:
        text = self._MIGRATION.read_text(encoding="utf-8")
        # Positive control: the precondition that CREATES the problem -- the
        # prompt is consumer-owned. If this is gone the file was restructured
        # and the assertions below are checking the wrong document.
        assert "Do NOT delete" in text
        assert 'error="repo-not-found"' in text
        assert 'error="repo-not-visible"' in text, (
            "MIGRATION.md must name the new label an onboarded consumer has "
            "to add; the template alone reaches only new onboardings."
        )
        assert "no longer means the repository does not exist" in text, (
            "MIGRATION.md must say repo-not-found changed meaning -- a "
            "consumer re-copying bullets without being told that has no "
            "reason to think the old one was wrong."
        )
        assert _404_PREMISES["repo-not-found"] in text, (
            "MIGRATION.md's restatement is the third copy of the "
            "repo-not-found premise; it must match the other two word for "
            "word or a consumer re-copies a rule the classifier does not "
            "support."
        )


# ---------------------------------------------------------------------------
# Reviewer-prompt guidance
# ---------------------------------------------------------------------------


_PROMPT_PATH = (
    Path(__file__).resolve().parent.parent.parent / "prompts" / "code-review-system.md"
)


@pytest.mark.skipif(
    not _PROMPT_PATH.exists(),
    reason="caller-repo prompt (code-review-system.md) absent in public reusable repo",
)
class TestPromptGuidance:
    """Keep ``code-review-system.md`` and the inline blurb in ``main()`` in
    sync with the status/error labels the verifier actually emits. A
    mismatch between prompt guidance and script output produces systematic
    false positives or false negatives in downstream review.

    Nothing here runs anywhere today, so do not add a label guard to it.
    ``_PROMPT_PATH`` is ``<checkout>/.github/prompts/code-review-system.md``,
    which this repo has never carried and by design will not: those two
    files are read from the CONSUMER repo (README, "Overriding prompts per
    consumer repo"). A consumer does not run this class either -- the suite
    is vendored at ``.ai-dev-pr-review/.github/scripts/tests/``, so the same
    expression resolves inside the vendored copy of this repo rather than
    the consumer's own prompt, and no consumer workflow runs pytest over it.
    AT-2801 added a ``repo-not-visible`` guard here and it was measured
    dead on both counts, so it was removed rather than left to look like
    coverage.

    Where that coverage lives instead: ``TestExamplePromptCovers404Labels``
    holds the in-tree template, ``TestRendered404Guidance`` holds the blurb
    ``main()`` renders, and ``MIGRATION.md`` tells an onboarded consumer to
    re-copy the rules. AT-2844 tracks checking the consumer-side copies
    across the fleet, which no test in this repo can reach; AT-2849 tracks
    this skip, which also hides ``test_inline_blurb_matches_prompt_doc``
    below -- that one reads only ``verify_action_shas.py`` and has no
    business being gated on a caller-repo file at all."""

    _PROMPT_PATH = _PROMPT_PATH

    def test_prompt_distinguishes_comment_matches_false(self) -> None:
        text = self._PROMPT_PATH.read_text()
        assert 'comment-matches="false"' in text, (
            "Prompt must tell reviewers that verified + comment-matches=false "
            "is a legitimate minor finding (not a false positive)."
        )

    def test_prompt_describes_repo_not_found(self) -> None:
        text = self._PROMPT_PATH.read_text()
        assert 'error="repo-not-found"' in text, (
            "Prompt must carry the repo-not-found rule the verifier emits; "
            "copy it from examples/prompts/code-review-system.md."
        )

    def test_inline_blurb_matches_prompt_doc(self) -> None:
        """``main()`` embeds a shortened copy of the prompt guidance into
        ``context.md``. The two must agree on which labels warrant ``major``
        vs ``suggestion`` -- the rendered blurb (after Python string-literal
        escaping is undone) must contain the same tokens as the prompt."""
        # Evaluate the source through ``ast`` so string-literal escapes
        # collapse to their rendered form. We do not actually want to
        # execute ``main()`` here -- just read what it would write.
        # ``.parent`` is this tests/ directory, which holds no production
        # module -- the script sits one level up, in .github/scripts/. The
        # wrong path raised FileNotFoundError rather than failing an
        # assertion, and went unnoticed because the whole class is skipped
        # wherever .github/prompts/code-review-system.md is absent.
        source = (
            Path(__file__).resolve().parent.parent / "verify_action_shas.py"
        ).read_text()
        tree = ast.parse(source)
        # Collect every string constant in the module -- the blurb lives in
        # a concatenated string expression inside ``main()``.
        all_strings = "\n".join(
            node.value
            for node in ast.walk(tree)
            if isinstance(node, ast.Constant) and isinstance(node.value, str)
        )
        assert 'comment-matches="false"' in all_strings, (
            "main()'s inline blurb must reference comment-matches=false"
        )
        assert 'error="repo-not-found"' in all_strings, (
            "main()'s inline blurb must reference error=repo-not-found"
        )

    def test_prompt_clarifies_absent_comment_matches_is_not_a_finding(
        self,
    ) -> None:
        """Commentless pins render with no ``comment-matches`` attribute; the
        prompt must explicitly tell reviewers that the attribute's absence is
        not a mismatch finding. Without this, a reviewer can read 'missing'
        as 'false' and flag every commentless pin."""
        text = self._PROMPT_PATH.read_text()
        assert "absent from the `<pin>` element" in text, (
            "Prompt must clarify that an absent comment-matches attribute "
            "is not a finding."
        )


# ---------------------------------------------------------------------------
# _verify_pin
# ---------------------------------------------------------------------------


class TestVerifyPin:
    def _checkout_tags_payload(self) -> str:
        return _tags_payload([{"name": "v4.3.1", "commit": {"sha": CHECKOUT_SHA}}])

    def test_verified_pin_with_matching_comment(self) -> None:
        pin: RawPin = {
            "repo": "actions/checkout",
            "sha": CHECKOUT_SHA,
            "comment": "v4.3.1",
        }
        cache: TagCache = {}
        with patch(
            "verify_action_shas.subprocess.run",
            return_value=_completed(self._checkout_tags_payload()),
        ):
            result = _verify_pin(pin, cache)

        assert result["status"] == "verified"
        assert result.get("tag") == "v4.3.1"
        assert result.get("comment_matches") == "true"

    def test_verified_pin_with_mismatched_comment(self) -> None:
        pin: RawPin = {
            "repo": "actions/checkout",
            "sha": CHECKOUT_SHA,
            "comment": "v4.0.0",
        }
        cache: TagCache = {}
        with patch(
            "verify_action_shas.subprocess.run",
            return_value=_completed(self._checkout_tags_payload()),
        ):
            result = _verify_pin(pin, cache)

        assert result["status"] == "verified"
        assert result.get("comment_matches") == "false"

    def test_verified_commentless_pin_omits_comment_matches(self) -> None:
        """A pin without a ``# vX.Y`` trailing comment must NOT emit a
        ``comment_matches`` attribute. Previously this returned ``"false"``
        unconditionally, which the reviewer-guidance prompt then read as
        'version comment drifts from tag' -- producing a false positive on
        every commentless pin. The rendered XML must likewise omit both
        ``comment=`` and ``comment-matches=`` attributes."""
        pin: RawPin = {
            "repo": "actions/checkout",
            "sha": CHECKOUT_SHA,
            "comment": None,
        }
        cache: TagCache = {}
        with patch(
            "verify_action_shas.subprocess.run",
            return_value=_completed(self._checkout_tags_payload()),
        ):
            result = _verify_pin(pin, cache)

        assert result["status"] == "verified"
        assert result.get("tag") == "v4.3.1"
        assert "comment_matches" not in result
        # The rendered XML must not carry either field either.
        xml = _render_xml([result])
        assert "comment-matches=" not in xml
        assert "comment=" not in xml

    def test_prefers_comment_matching_tag_among_multiple_matches(self) -> None:
        """If the SHA maps to both a floating alias (v4) and a specific
        version tag (v4.3.1), and the comment pins v4.3.1, prefer v4.3.1."""
        pin: RawPin = {
            "repo": "actions/checkout",
            "sha": CHECKOUT_SHA,
            "comment": "v4.3.1",
        }
        cache: TagCache = {}
        tags_payload = _tags_payload(
            [
                {"name": "v4", "commit": {"sha": CHECKOUT_SHA}},
                {"name": "v4.3.1", "commit": {"sha": CHECKOUT_SHA}},
            ]
        )
        with patch(
            "verify_action_shas.subprocess.run",
            return_value=_completed(tags_payload),
        ):
            result = _verify_pin(pin, cache)

        assert result["status"] == "verified"
        assert result.get("tag") == "v4.3.1"
        assert result.get("comment_matches") == "true"

    def test_api_failure_produces_unverified_entry(self) -> None:
        pin: RawPin = {
            "repo": "actions/checkout",
            "sha": CHECKOUT_SHA,
            "comment": "v4.3.1",
        }
        cache: TagCache = {}
        with patch(
            "verify_action_shas.subprocess.run",
            return_value=_completed(returncode=1, stderr="rate limit exceeded"),
        ):
            result = _verify_pin(pin, cache)

        assert result["status"] == "unverified"
        assert result.get("error") == "rate-limit"

    def test_timeout_produces_unverified_entry(self) -> None:
        pin: RawPin = {
            "repo": "actions/checkout",
            "sha": CHECKOUT_SHA,
            "comment": "v4.3.1",
        }
        cache: TagCache = {}
        with patch(
            "verify_action_shas.subprocess.run",
            side_effect=subprocess.TimeoutExpired([], _SUBPROCESS_TIMEOUT_SEC),
        ):
            result = _verify_pin(pin, cache)

        assert result["status"] == "unverified"
        assert result.get("error") == "timeout"

    def test_sha_not_in_tags_produces_unmatched(self) -> None:
        pin: RawPin = {
            "repo": "actions/checkout",
            "sha": "0000000000000000000000000000000000000000",
            "comment": "v4.3.1",
        }
        cache: TagCache = {}
        with patch(
            "verify_action_shas.subprocess.run",
            return_value=_completed(self._checkout_tags_payload()),
        ):
            result = _verify_pin(pin, cache)

        assert result["status"] == "unmatched"
        assert result.get("error") == "sha-not-found"

    def test_annotated_tag_payload_uses_peeled_commit_sha(self) -> None:
        """The ``/tags`` REST endpoint returns ``commit.sha`` already peeled
        to the underlying commit for both lightweight and annotated tags, so
        no separate peel call is required."""
        tags_payload = _tags_payload(
            [{"name": "v4.3.1", "commit": {"sha": CHECKOUT_SHA}}]
        )

        pin: RawPin = {
            "repo": "actions/checkout",
            "sha": CHECKOUT_SHA,
            "comment": "v4.3.1",
        }
        cache: TagCache = {}
        with patch(
            "verify_action_shas.subprocess.run",
            return_value=_completed(tags_payload),
        ) as run_mock:
            result = _verify_pin(pin, cache)

        assert result["status"] == "verified"
        assert result.get("tag") == "v4.3.1"
        # Exactly one subprocess call -- no separate peel step.
        assert run_mock.call_count == 1

    def test_unverified_pin_has_non_empty_error_even_with_nil_upstream(self) -> None:
        """If ``_fetch_tags`` somehow returns ``(None, None)`` (e.g. a future
        refactor regresses the error-label invariant), ``_verify_pin`` must
        still produce a pin whose ``error`` attribute will render into the
        XML -- a ``None`` error would be silently dropped by the renderer."""
        pin: RawPin = {
            "repo": "actions/checkout",
            "sha": CHECKOUT_SHA,
            "comment": "v4.3.1",
        }
        cache: TagCache = {}
        with patch("verify_action_shas._fetch_tags", return_value=(None, None)):
            result = _verify_pin(pin, cache)

        assert result["status"] == "unverified"
        error = result.get("error")
        assert isinstance(error, str)
        assert error != ""

    def test_unverified_pin_survives_optimised_python(self, tmp_path: Path) -> None:
        """Run ``_verify_pin`` under ``python -OO`` (which strips ``assert``
        statements) and confirm an unreachable-looking ``(None, None)`` return
        from ``_fetch_tags`` still yields a pin with a non-empty ``error``
        attribute. This is the invariant the previous ``assert`` was
        enforcing; the explicit fallback must preserve it when asserts are
        stripped."""
        script = tmp_path / "probe.py"
        # The subprocess script runs in its own interpreter with no access
        # to this module's namespace, so CHECKOUT_SHA is interpolated into
        # the source literal via an f-string (braces in the generated
        # Python source are escaped as ``{{`` / ``}}``).
        script.write_text(
            textwrap.dedent(
                f"""
                import json
                from unittest.mock import patch
                import verify_action_shas as v

                pin = {{
                    "repo": "actions/checkout",
                    "sha": "{CHECKOUT_SHA}",
                    "comment": "v4.3.1",
                }}
                with patch("verify_action_shas._fetch_tags", return_value=(None, None)):
                    result = v._verify_pin(pin, {{}})
                print(json.dumps(result))
                """
            ).lstrip()
        )
        scripts_dir = str(Path(__file__).resolve().parent.parent)
        proc = subprocess.run(
            [sys.executable, "-OO", str(script)],
            capture_output=True,
            text=True,
            env={**os.environ, "PYTHONPATH": scripts_dir},
            check=True,
        )
        result = json.loads(proc.stdout)
        assert result["status"] == "unverified"
        assert isinstance(result["error"], str)
        assert result["error"] != ""


# ---------------------------------------------------------------------------
# _render_xml
# ---------------------------------------------------------------------------


class TestRenderXml:
    def test_verified_pin_renders_all_attributes(self) -> None:
        pins: list[ResultPin] = [
            VerifiedPin(
                repo="actions/checkout",
                sha=CHECKOUT_SHA,
                comment="v4.3.1",
                status="verified",
                tag="v4.3.1",
                comment_matches="true",
            )
        ]
        xml = _render_xml(pins)
        assert "<verified-action-pins>" in xml
        assert 'repo="actions/checkout"' in xml
        assert 'status="verified"' in xml
        assert 'tag="v4.3.1"' in xml
        # Rendered as the kebab-case attribute the prompt expects.
        assert 'comment-matches="true"' in xml
        assert "</verified-action-pins>" in xml

    def test_unverified_pin_includes_error(self) -> None:
        pins: list[ResultPin] = [
            UnverifiedPin(
                repo="actions/checkout",
                sha=CHECKOUT_SHA,
                comment=None,
                status="unverified",
                error="rate-limit",
            )
        ]
        xml = _render_xml(pins)
        assert 'status="unverified"' in xml
        assert 'error="rate-limit"' in xml
        # None values are omitted
        assert "comment=" not in xml

    def test_unmatched_pin_includes_error(self) -> None:
        pins: list[ResultPin] = [
            UnmatchedPin(
                repo="actions/checkout",
                sha="0000000000000000000000000000000000000000",
                comment=None,
                status="unmatched",
                error="sha-not-found",
            )
        ]
        xml = _render_xml(pins)
        assert 'status="unmatched"' in xml
        assert 'error="sha-not-found"' in xml

    def test_attribute_values_are_xml_escaped(self) -> None:
        """Upstream-controlled strings (``repo``, ``tag``, ``error``) must
        not be able to break out of the attribute quoting and inject prompt
        content into ``context.md``. The rendered block must round-trip
        through an XML parser and the raw injection payload must not appear
        unescaped."""
        malicious_repo = 'malicious/"<injected attr="1">'
        malicious_tag = "<script>alert(1)</script>"
        malicious_error = 'err"&<>'

        # Verify tag escaping via a VerifiedPin (tag is only rendered for verified).
        verified_pins: list[ResultPin] = [
            VerifiedPin(
                repo=malicious_repo,
                sha=CHECKOUT_SHA,
                comment="v4.3.1",
                status="verified",
                tag=malicious_tag,
                comment_matches="true",
            )
        ]
        xml_verified = _render_xml(verified_pins)
        assert malicious_repo not in xml_verified
        assert malicious_tag not in xml_verified
        assert "&lt;script&gt;alert(1)&lt;/script&gt;" in xml_verified

        root = ET.fromstring(xml_verified)
        pin_el = root.find("pin")
        assert pin_el is not None
        assert pin_el.get("repo") == malicious_repo
        assert pin_el.get("tag") == malicious_tag

        # Verify error escaping via an UnmatchedPin.
        error_pins: list[ResultPin] = [
            UnmatchedPin(
                repo=malicious_repo,
                sha=CHECKOUT_SHA,
                comment=None,
                status="unmatched",
                error=malicious_error,
            )
        ]
        xml_error = _render_xml(error_pins)
        assert malicious_error not in xml_error
        # Ampersand in the error payload must be entity-escaped.
        assert "err" in xml_error  # sanity: some form of the value is present
        assert "&amp;" in xml_error

        root2 = ET.fromstring(xml_error)
        pin_el2 = root2.find("pin")
        assert pin_el2 is not None
        assert pin_el2.get("repo") == malicious_repo
        assert pin_el2.get("error") == malicious_error


# ---------------------------------------------------------------------------
# main() integration
# ---------------------------------------------------------------------------


class TestMain:
    def test_main_skips_when_diff_missing(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        monkeypatch.chdir(tmp_path)
        main()
        captured = capsys.readouterr()
        assert "pr.diff not found" in captured.err

    def test_main_skips_when_no_pins(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        (tmp_path / "pr.diff").write_text("+just a line with no action pin\n")
        monkeypatch.chdir(tmp_path)
        main()
        captured = capsys.readouterr()
        assert "No pinned action SHAs" in captured.out

    def test_main_appends_section_to_context_md(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        (tmp_path / "pr.diff").write_text(
            f"+      - uses: actions/checkout@{CHECKOUT_SHA}  # v4.3.1\n"
        )
        (tmp_path / "context.md").write_text("existing content")

        tags_payload = _tags_payload(
            [{"name": "v4.3.1", "commit": {"sha": CHECKOUT_SHA}}]
        )

        monkeypatch.chdir(tmp_path)
        with patch(
            "verify_action_shas.subprocess.run",
            return_value=_completed(tags_payload),
        ):
            main()

        content = (tmp_path / "context.md").read_text()
        assert "Verified Action SHA Pins" in content
        assert "<verified-action-pins>" in content
        assert 'status="verified"' in content
        # The section must be appended, not overwritten -- any pre-existing
        # reviewer prompt content in context.md has to survive intact.
        assert "existing content" in content

    def test_main_caches_repeated_repo_calls(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Multiple pins for the same repo must share a single API call."""
        (tmp_path / "pr.diff").write_text(
            f"+      - uses: actions/checkout@{CHECKOUT_SHA}  # v4.3.1\n"
            f"+      - uses: actions/checkout@{CHECKOUT_V4_3_0_SHA}  # v4.3.0\n"
        )
        (tmp_path / "context.md").write_text("")

        tags_payload = _tags_payload(
            [
                {"name": "v4.3.1", "commit": {"sha": CHECKOUT_SHA}},
                {"name": "v4.3.0", "commit": {"sha": CHECKOUT_V4_3_0_SHA}},
            ]
        )

        monkeypatch.chdir(tmp_path)
        with patch(
            "verify_action_shas.subprocess.run",
            return_value=_completed(tags_payload),
        ) as run_mock:
            main()

        assert run_mock.call_count == 1

    def test_main_tolerates_non_utf8_bytes_in_diff(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """A pr.diff with non-UTF-8 bytes must not crash the script.

        Regression guard: a PR on a consumer repo carried ``0xeb`` mid-line
        and the strict utf-8 decode raised ``UnicodeDecodeError`` from
        ``open()``, taking down the whole "Append verified action SHA pins"
        step. With ``errors="replace"`` the script should keep running and
        still extract pins from the surrounding ASCII content.
        """
        diff_bytes = (
            b"diff --git a/x b/x\n"
            b"@@ -1,1 +1,1 @@\n"
            b"-old\n"
            b"+      - uses: actions/checkout@"
            + CHECKOUT_SHA.encode("ascii")
            + b"  # v4.3.1 \xeb\xeb\n"
        )
        (tmp_path / "pr.diff").write_bytes(diff_bytes)
        (tmp_path / "context.md").write_text("")

        tags_payload = _tags_payload(
            [{"name": "v4.3.1", "commit": {"sha": CHECKOUT_SHA}}]
        )

        monkeypatch.chdir(tmp_path)
        with patch(
            "verify_action_shas.subprocess.run",
            return_value=_completed(tags_payload),
        ):
            main()

        captured = capsys.readouterr()
        assert "Verified 1/1 action SHA pins" in captured.out
