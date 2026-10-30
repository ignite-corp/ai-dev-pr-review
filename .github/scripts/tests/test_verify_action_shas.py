"""Tests for verify_action_shas: diff parsing and SHA verification.

Test-module hygiene: **all imports belong at the top of this file per PEP 8**
-- do not add `import foo` statements inside test function bodies. Every prior
reviewer round has flagged in-body imports; keep them here so the next round
does not reopen the same nit.
"""

from __future__ import annotations

import json
import os
import re
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


def _blurb_text(context_md: str) -> str:
    """Return the rendered section WITHOUT its ``<pin .../>`` rows.

    The complement of ``_pin_rows``, and for the same reason: the blurb
    quotes every ``error="..."`` label it governs, so a check run over the
    whole document cannot tell the blurb's description of a label from a
    pin that happens to carry it.
    """
    return "\n".join(line for line in context_md.splitlines() if "<pin " not in line)


def _run_main(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    repo: str,
    sha: str,
    seed: str = "",
) -> str:
    """Run ``main()`` over a one-pin diff and return the written ``context.md``.

    The patched ``subprocess.run`` makes every tag listing 404, which is
    what the 404 guidance tests need; the blurb itself is rendered for any
    pin, so tests that only read the blurb can use it too.

    ``seed`` is what ``context.md`` already holds when ``main()`` runs. It
    defaults to empty for the tests that read one rendered attribute, and
    is NOT what a pipeline run looks like: ``main()`` opens the file in
    append mode and the real one carries PR metadata and prior review
    threads by then. A test whose subject is how much of the file counts as
    the blurb has to say so.
    """
    (tmp_path / "pr.diff").write_text(f"+      - uses: {repo}@{sha}  # v1\n")
    (tmp_path / "context.md").write_text(seed)
    monkeypatch.chdir(tmp_path)
    with patch(
        "verify_action_shas.subprocess.run",
        return_value=_completed(returncode=1, stderr=_GH_404_STDERR),
    ):
        main()
    return (tmp_path / "context.md").read_text()


def _severity_rule_for(document: str, error_label: str) -> str:
    """Return the one prompt bullet in ``document`` governing a label.

    Both guidance surfaces -- the blurb ``main()`` writes into ``context.md``
    and the base instructions ``review_prompt.md`` --
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
# base instructions. Each value carries no markdown emphasis and no dashes, so
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

    One contract, one home. The rendered blurb and the base instructions
    carry the same guidance in two files, so they are held here by the same
    function: two copies of these assertions drifted apart once already,
    the looser one accepting a bullet worded "escalate beyond
    ``suggestion``" that the stricter one rejected. The strict phrasing is
    the one kept -- ``suggestion`` appearing anywhere in a bullet does not
    show the bullet confines the reviewer to it.

    Every label's bullet is also held to its own premise, from
    ``_404_PREMISES``. Both bullet-carrying surfaces pass through here.
    Migration documentation describes release adoption and copy removal
    without restating the policy.
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
        """GitHub owner names are case-insensitive, so the comparison is.

        Both identities are re-cased from the module constants rather than
        respelled as literals. A literal pair stays self-consistent when
        ``_RUN_ORG`` changes, so the test would keep passing while no longer
        testing an in-org pair at all -- silently, because the two literals
        agree with each other rather than with the module. (AT-2873)

        Deriving the inputs from the target brings its own silent failure,
        so the property this test needs is asserted directly: the two owner
        halves must DIFFER in spelling yet match case-insensitively. Only
        then is ``_classify_404``'s ``.lower()`` doing any work.

        Asserting that each transform changed its own constant is not the
        same property and does not imply it -- measured: make the two
        transforms converge (``.title()`` on both) and each value still
        differs from its own constant, so a per-constant check passes,
        while both owners become ``Ignite-Corp`` and the comparison under
        test stops being case-insensitive at all.
        """
        run_repo = _RUN_REPO.title()
        in_org_private = _IN_ORG_PRIVATE.upper()
        run_owner = run_repo.split("/", 1)[0]
        pin_owner = in_org_private.split("/", 1)[0]
        assert run_owner != pin_owner, (
            "the two owners are spelled identically, so this no longer "
            "exercises a case-insensitive comparison; pick transforms "
            "that differ from each other"
        )
        assert run_owner.lower() == pin_owner.lower(), (
            "the two owners must be the same owner differing only in case, "
            "or this is not an in-org pair"
        )
        monkeypatch.setenv("GITHUB_REPOSITORY", run_repo)
        cache: TagCache = {}
        with patch(
            "verify_action_shas.subprocess.run",
            return_value=_completed(returncode=1, stderr=_GH_404_STDERR),
        ):
            _, error = _fetch_tags(in_org_private, cache)
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
        ``actions/no-such-action-xyzzy`` does not exist at all. Both come
        back as HTTP 404 with the same body, and all this layer sees is
        ``gh``'s stderr token -- so both arrive here as the same
        ``_GH_404_STDERR`` and both must land on the same label.
        (Per-request headers differ, but carry no signal about which cause
        it was, and ``_fetch_tags`` never reads them.) A reviewer
        instruction keyed on that label therefore cannot assert absence of
        either one.
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

    def test_in_org_pin_does_not_reach_the_major_instruction(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("GITHUB_REPOSITORY", _RUN_REPO)
        content = _run_main(tmp_path, monkeypatch, _IN_ORG_PRIVATE, _IN_ORG_PRIVATE_SHA)
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
        content = _run_main(tmp_path, monkeypatch, _OUT_OF_ORG_ABSENT, CHECKOUT_SHA)
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
        content = _run_main(tmp_path, monkeypatch, _IN_ORG_PRIVATE, _IN_ORG_PRIVATE_SHA)
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
        content = _run_main(tmp_path, monkeypatch, _IN_ORG_PRIVATE, _IN_ORG_PRIVATE_SHA)
        (row,) = _pin_rows(content)
        assert f'repo="{_IN_ORG_PRIVATE}"' in row
        assert 'error="repo-not-found"' in row
        _assert_404_rule_is_honest(content, "repo-not-found")


# Actual base Codex instructions, held byte-equal to all three reviewers
# by test_prompt_guidance.py. No consumer copy is required (AT-3159).
_BASE_PROMPT_PATH = Path(__file__).resolve().parent.parent / "review_prompt.md"


class TestBasePromptCovers404Labels:
    """Both 404 rules in the base instructions remain honest and actionable."""

    _BASE_PROMPT = _BASE_PROMPT_PATH

    def test_base_prompt_describes_repo_not_visible(self) -> None:
        text = self._BASE_PROMPT.read_text(encoding="utf-8")
        # Positive control: the label the template already carried.
        assert 'error="repo-not-found"' in text
        assert 'error="repo-not-visible"' in text, (
            "Base reviewer instructions must carry the repo-not-visible rule "
            "the verifier now emits."
        )

    @pytest.mark.parametrize("label", ["repo-not-found", "repo-not-visible"])
    def test_base_prompt_404_rules_are_honest(self, label: str) -> None:
        """Neither 404 bullet may escalate or assert absence, and both must
        stay actionable -- literally the same contract
        ``TestRendered404Guidance`` holds the rendered blurb to, applied by
        the same function to the instructions consumers receive."""
        text = self._BASE_PROMPT.read_text(encoding="utf-8")
        _assert_404_rule_is_honest(text, label)


# A label as the reviewer reads it: a backticked ``name="value"`` token.
# Matched by shape, never by an allow-list of attribute names -- a list that
# does not reject by default admits a brand-new attribute on one surface
# without noticing it never reached the other, which is the hole AT-2880
# opened.
#
# The name follows XML's own attribute-name shape, because a narrower class
# is that same allow-list one level down: ``[A-Za-z][A-Za-z0-9-]*`` matched
# neither ``error_detail="x"`` nor ``error.detail="x"``, both legal, so
# documenting one on a single surface left both label sets unchanged.
# ``[^\W\d]`` opens on a letter or ``_`` (Unicode letters included, as
# ``\w`` is Unicode here) and never on a digit, which XML forbids; the rest
# adds digits, ``.``, ``:`` and ``-``. A name opening with ``:`` is legal
# and matched by neither -- it is reserved for namespace use and no surface
# here writes one.
_LABEL_TOKEN = re.compile(r'`([^\W\d][\w.:-]*)="([^"`]*)"`')

_PIN_GUIDANCE_HEADING = "## GitHub Actions SHA Pins"

# The same two anchors on the rendered side: the heading ``main()`` writes
# and the element that ends its prose.
_RENDERED_PIN_HEADING = "## Verified Action SHA Pins"
_RENDERED_PIN_ELEMENTS = "<verified-action-pins>"


def _guidance_labels(document: str) -> set[str]:
    """Return every ``name="value"`` token ``document`` quotes to the reviewer.

    Placeholder values (``tag="..."``) are kept, not filtered. Dropping them
    from both sides hid the AT-2880 drift class itself at its least visible
    point: an attribute documented on one surface alone with ``...`` for its
    value -- the shape guidance prose reaches for first -- left that side's
    set, the other side never had it, and the two sets compared equal.

    So what is guaranteed is the whole token, name and value together. A
    surface writing ``severity="..."`` where the other writes nothing, or
    writes ``severity="major"``, is reported as a one-sided token either
    way. That couples the two surfaces on how they spell a placeholder,
    which is the point: they are maintained as one description, and a
    comparison that forgave the spelling would have to forgive the absence
    too.
    """
    return {f'{name}="{value}"' for name, value in _LABEL_TOKEN.findall(document)}


def _rendered_pin_blurb(context_md: str) -> str:
    """Return the prose of the pin section ``main()`` appended.

    The blurb-side counterpart of ``_base_prompt_pin_section``, scoped
    for the same reason: that side is narrowed to one heading so a
    ``severity="..."`` example under another is not compared as a pin
    label, and a blurb side read from all of ``context.md`` would put the
    two sides' label sets on unequal scopes.

    Measured, not assumed. ``main()`` opens ``context.md`` in append mode,
    so a pipeline run reaches it with PR metadata and prior review threads
    already in the file; seeding one ``severity="critical"`` token in an
    earlier section put that token into the blurb's label set, and the
    comparison then turned on what the rest of the pipeline happened to
    quote. The equality looked symmetric only because every test seeded the
    file empty.

    ``rindex`` for the heading because ``main()`` appends -- the section
    under test is the last one -- and both lookups raise rather than fall
    back, the choice ``_base_prompt_pin_section`` makes: a renamed
    heading or a renamed element must fail loudly instead of yielding an
    empty blurb that compares empty-to-empty and passes. The slice stops at
    the ``<verified-action-pins>`` element, so the pin rows are excluded
    here the way ``_blurb_text`` excludes them, and for the same reason.
    """
    start = context_md.rindex(_RENDERED_PIN_HEADING) + len(_RENDERED_PIN_HEADING)
    end = context_md.index(_RENDERED_PIN_ELEMENTS, start)
    return context_md[start:end]


def _base_prompt_pin_section() -> str:
    """Return the base instructions' pin-guidance section, heading excluded.

    Scoped to the one section because the rest of the instructions govern other
    things: a ``severity="..."`` example written under another heading is not
    a pin label and must not be compared as one.

    The named Output format heading closes this policy in review_prompt.md.
    ``str.index`` on purpose -- a renamed or deleted heading raises here
    instead of yielding an empty section, which would make the label sets
    below compare empty-to-empty and pass.
    """
    text = _BASE_PROMPT_PATH.read_text(encoding="utf-8")
    start = text.index(_PIN_GUIDANCE_HEADING) + len(_PIN_GUIDANCE_HEADING)
    end = text.index("\n## Output format\n", start)
    return text[start:end]


class TestGuidanceSurfacesShareOneLabelSet:
    """The blurb ``main()`` writes and the base instructions must name the
    same labels -- not merely each carry the labels somebody listed.

    AT-2880: every check above holds ONE surface to ONE token it should
    contain. Each side therefore stays green while the other falls behind,
    because nothing reads both and compares. Add ``error="rate-limited"`` to
    the renderer and the reviewer template never learns the word, and no
    test here notices; drop a label from the template and the blurb keeps
    promising guidance the reviewer no longer has.

    So this is set equality, in both directions, and the message names the
    side a token is missing from. A list of expected labels would be the
    same defect one level up: it would have to be edited by the person
    adding the label, which is exactly the person who just forgot.

    Held against the actual base Codex instructions. All three reviewer
    renderers carry that same policy byte for byte (test_prompt_guidance.py).
    """

    def test_blurb_and_base_prompt_name_the_same_labels(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("GITHUB_REPOSITORY", _RUN_REPO)
        content = _run_main(tmp_path, monkeypatch, _IN_ORG_PRIVATE, _IN_ORG_PRIVATE_SHA)
        blurb_labels = _guidance_labels(_rendered_pin_blurb(content))

        # Positive control on the extractor itself: were it to match
        # nothing, the comparison below would be `set() == set()` and would
        # pass over any two documents at all. These two tokens are the ones
        # AT-2880 names as having to agree.
        assert {'comment-matches="false"', 'error="repo-not-found"'} <= blurb_labels, (
            "the label extractor did not find both tokens AT-2880 names in "
            f"the rendered blurb; it found {sorted(blurb_labels)}"
        )

        prompt_labels = _guidance_labels(_base_prompt_pin_section())
        assert blurb_labels == prompt_labels, (
            "the blurb main() renders and base review_prompt.md "
            "must name the same labels -- "
            f"only in the blurb: {sorted(blurb_labels - prompt_labels)}; "
            f"only in the prompt: {sorted(prompt_labels - blurb_labels)}"
        )

    def test_placeholder_valued_attribute_on_one_surface_is_not_hidden(self) -> None:
        """An attribute written ``name="..."`` on one surface alone differs.

        The AT-2880 drift class in its least visible form. While the
        extractor dropped every ``...`` value, a brand-new attribute
        documented with a placeholder -- the shape guidance prose reaches
        for first -- left the one side's set, the other side never had it,
        and the comparison above passed over two documents that disagree.
        Held on the extractor rather than on the two real documents,
        because the defect is that the token never reaches a set at all.
        """
        one_sided = _guidance_labels('- `severity="..."` carries the finding.')
        assert one_sided == {'severity="..."'}, (
            "a placeholder-valued attribute must reach the label set; it "
            f"yielded {sorted(one_sided)}"
        )
        assert one_sided != _guidance_labels("- the other surface says nothing."), (
            "a surface documenting an attribute the other never mentions "
            "must not compare equal to it"
        )

    @pytest.mark.parametrize("name", ["error_detail", "error.detail", "error:detail"])
    def test_legal_attribute_names_outside_letters_and_hyphens_are_seen(
        self, name: str
    ) -> None:
        """``_``, ``.`` and ``:`` are legal in an XML attribute name.

        A name class of letters, digits and hyphens is an allow-list one
        level down: a name carrying any other legal character matched
        NEITHER surface, so adding it to one alone left both label sets
        unchanged and the equality above green -- the same silence the
        shape match exists to prevent.
        """
        one_sided = _guidance_labels(f'- `{name}="x"` explains the failure.')
        assert one_sided == {f'{name}="x"'}, (
            f"an attribute named {name!r} must reach the label set; it "
            f"yielded {sorted(one_sided)}"
        )
        assert one_sided != _guidance_labels("- the other surface says nothing."), (
            f"a surface documenting {name!r} must not compare equal to one "
            "that never mentions it"
        )

    def test_output_examples_cannot_contaminate_the_pin_label_set(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        text = _BASE_PROMPT_PATH.read_text(encoding="utf-8")
        expected = _guidance_labels(_base_prompt_pin_section())
        output = "\n## Output format\n"
        assert output in text
        changed = text.replace(output, output + '\nExample: `severity="major"`.\n')
        path = tmp_path / "review_prompt.md"
        path.write_text(changed, encoding="utf-8")
        monkeypatch.setitem(globals(), "_BASE_PROMPT_PATH", path)
        assert 'severity="major"' in _guidance_labels(changed)
        assert _guidance_labels(_base_prompt_pin_section()) == expected
        path.write_text(
            changed.replace(output, "\n## Renamed output\n"), encoding="utf-8"
        )
        with pytest.raises(ValueError):
            _base_prompt_pin_section()

    def test_earlier_context_md_sections_stay_out_of_the_blurb_side(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The blurb is the section ``main()`` wrote, not the whole file.

        The prompt side is narrowed to one heading on purpose, so a blurb
        side read from all of ``context.md`` compares two unequal scopes:
        every ``name="value"`` token quoted anywhere earlier in the file
        enters the pin label set as though the blurb had named it, and the
        equality then turns on what the rest of the pipeline wrote. The
        other tests here seed ``context.md`` empty, which is the single
        case where that cannot show.
        """
        monkeypatch.setenv("GITHUB_REPOSITORY", _RUN_REPO)
        content = _run_main(
            tmp_path,
            monkeypatch,
            _IN_ORG_PRIVATE,
            _IN_ORG_PRIVATE_SHA,
            seed=(
                "## PR Metadata\n\nauthor: dependabot[bot] -- R2 pre-release "
                'is a `severity="critical"` veto.\n'
            ),
        )
        blurb_labels = _guidance_labels(_rendered_pin_blurb(content))
        assert 'severity="critical"' not in blurb_labels, (
            "a token from a section main() did not write must not enter "
            f"the blurb's label set; it holds {sorted(blurb_labels)}"
        )
        prompt_labels = _guidance_labels(_base_prompt_pin_section())
        assert blurb_labels == prompt_labels, (
            "with both sides scoped to their own pin section the labels "
            "must agree whatever context.md already carried -- "
            f"only in the blurb: {sorted(blurb_labels - prompt_labels)}; "
            f"only in the prompt: {sorted(prompt_labels - blurb_labels)}"
        )


class TestMigrationDocReachesOnboardedConsumers:
    """Base-source preparation does not deploy a release or remove stale copies."""

    _MIGRATION = Path(__file__).resolve().parents[3] / "MIGRATION.md"

    def test_migration_sequences_release_before_copy_removal(self) -> None:
        text = self._MIGRATION.read_text(encoding="utf-8")
        start = text.index("### Shared GitHub Actions SHA Pins guidance (AT-3159)\n")
        end = text.index("\n### ", start + 1)
        section = text[start:end]
        assert "MINOR release" in section
        assert (
            "Update each consumer's review pin to that release before removing"
            in section
        )
        assert "source merge alone" in section
        assert "does not publish or deploy" in section
        assert (
            "The runner needs the same sequence: update its review pin to the compatible release, "
            "then remove the shared section from `default-prompts/`."
        ) in section

    @pytest.mark.parametrize("placement", ["removed", "moved"])
    def test_runner_rollout_must_stay_in_the_shared_policy_section(
        self, placement: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        text = self._MIGRATION.read_text(encoding="utf-8")
        path = tmp_path / "MIGRATION.md"
        path.write_text(text, encoding="utf-8")
        monkeypatch.setattr(self, "_MIGRATION", path)
        self.test_migration_sequences_release_before_copy_removal()
        (paragraph,) = [
            p
            for p in text.split("\n\n")
            if p.startswith("The runner needs the same sequence:")
        ]
        changed = text.replace(paragraph + "\n\n", "")
        if placement == "moved":
            changed += "\n\n" + paragraph + "\n"
        # The previous whole-document substring guard misses both mutations.
        assert all(
            phrase in changed
            for phrase in (
                "Do NOT delete",
                "AT-3159",
                "MINOR release",
                "before removing",
                "source merge alone",
                "does not publish or deploy",
                "runner",
            )
        )
        path.write_text(changed, encoding="utf-8")
        with pytest.raises(AssertionError):
            self.test_migration_sequences_release_before_copy_removal()


# ---------------------------------------------------------------------------
# Reviewer-prompt guidance
# ---------------------------------------------------------------------------


_PROMPT_PATH = (
    Path(__file__).resolve().parent.parent.parent / "prompts" / "code-review-system.md"
)


class TestInlineBlurbLabels:
    """Labels in the blurb ``main()`` writes that nothing else covers.

    Deliberately NOT gated on ``_PROMPT_PATH``: this runs ``main()`` and
    reads what it emitted. It sat inside ``TestPromptGuidance`` until
    AT-2849 and so had never executed anywhere -- the class is skipped here
    (the caller-repo prompt is absent by design) and is not run in a
    consumer either. An "add an assertion to the blurb guard" finding
    therefore landed in dead code and looked addressed.

    Nothing here compares the blurb with the caller-repo prompt doc, and
    -- measured, not assumed -- no test in this repo does: the test left
    in ``TestPromptGuidance`` reads ``_PROMPT_PATH`` alone and never reads
    what ``main()`` emits. The blurb IS compared against the base
    instructions, which a test here can read, by
    ``TestGuidanceSurfacesShareOneLabelSet`` above (AT-2880).
    """

    def test_inline_blurb_names_comment_matches_false(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The ``status="verified"`` bullet must name ``comment-matches="false"``.

        Without it a reviewer has no rule for a verified pin whose
        ``# vX.Y`` comment drifted, and the drift goes unreported.

        Anchored to the bullet, not to the document. The token's meaning
        is the status it hangs off: the same words under ``unmatched``, or
        up in the intro prose, attach the rule to the wrong pins while a
        whole-document substring check stays green. The one-element
        unpack is the same protection ``_severity_rule_for`` gives the
        ``error="..."`` labels: exactly one ``status="verified"`` bullet,
        so splitting the verified guidance across two fails too.

        What it does NOT catch, measured: the sentence staying here AND
        being copied into another status's bullet. This holds the
        verified bullet to carrying the rule; it does not make the rule
        exclusive to it.

        Only this one token is asserted here. The 404 labels are already
        held against the same rendered text, and more strictly, by
        ``TestRendered404Guidance``: ``_assert_404_rule_is_honest`` runs
        ``_severity_rule_for``, which requires exactly one bullet carrying
        the label and then checks what that bullet says. Repeating them
        here would be strictly weaker than coverage that already runs.

        Asserted against what ``main()`` emits, by running it, rather than
        against the shape of its source: reading the source coupled the
        test to how the blurb happens to be built, so a refactor that
        changed no output could fail it while a label present anywhere
        else in the source kept it green. (AT-2849)
        """
        monkeypatch.setenv("GITHUB_REPOSITORY", _RUN_REPO)
        content = _run_main(tmp_path, monkeypatch, _IN_ORG_PRIVATE, _IN_ORG_PRIVATE_SHA)
        (verified_bullet,) = [
            line
            for line in _blurb_text(content).splitlines()
            if line.startswith('- `status="verified"`')
        ]
        assert 'comment-matches="false"' in verified_bullet, (
            'the `status="verified"` bullet must tell the reviewer what to '
            "do when comment-matches=false"
        )


@pytest.mark.skipif(
    not _PROMPT_PATH.exists(),
    reason="caller-repo prompt (code-review-system.md) absent in public reusable repo",
)
class TestPromptGuidance:
    """Optional caller-repo prompt availability; no shared policy copy is owed."""

    def test_prompt_files_are_readable_and_nonempty(self) -> None:
        assert _PROMPT_PATH.read_text(encoding="utf-8").strip()
        checklist = _PROMPT_PATH.with_name("code-review-checklist.md")
        assert checklist.read_text(encoding="utf-8").strip()


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
