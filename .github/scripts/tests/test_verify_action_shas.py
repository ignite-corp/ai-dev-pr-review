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

    def test_http_404_via_stderr_maps_to_repo_not_found(self) -> None:
        """gh api reports HTTP 404 through stderr and exits with returncode=1
        (not 404). The stderr-token parser must still classify this correctly
        as ``repo-not-found`` rather than the generic ``api-error``."""
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
    false positives or false negatives in downstream review."""

    _PROMPT_PATH = _PROMPT_PATH

    def test_prompt_distinguishes_comment_matches_false(self) -> None:
        text = self._PROMPT_PATH.read_text()
        assert 'comment-matches="false"' in text, (
            "Prompt must tell reviewers that verified + comment-matches=false "
            "is a legitimate minor finding (not a false positive)."
        )

    def test_prompt_treats_repo_not_found_as_deterministic(self) -> None:
        text = self._PROMPT_PATH.read_text()
        assert 'error="repo-not-found"' in text, (
            "Prompt must distinguish repo-not-found (deterministic, major) "
            "from transient unverified errors (suggestion)."
        )

    def test_inline_blurb_matches_prompt_doc(self) -> None:
        """``main()`` embeds a shortened copy of the prompt guidance into
        ``context.md``. The two must agree on which labels warrant ``major``
        vs ``suggestion`` -- the rendered blurb (after Python string-literal
        escaping is undone) must contain the same tokens as the prompt."""
        # Evaluate the source through ``ast`` so string-literal escapes
        # collapse to their rendered form. We do not actually want to
        # execute ``main()`` here -- just read what it would write.
        source = (Path(__file__).resolve().parent / "verify_action_shas.py").read_text()
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
