"""The gemini always() net in base-ai-review-single.yml (AT-2539).

The reviewer run steps are continue-on-error, so a reviewer's own death does
not fail the job; each path has to record its own failure. claude's always()
step synthesises a ``status: "failed"`` verdict when none was written, and
codex's always() step fails the job. gemini recorded its failures only from
inside review_gemini.py's ``try/except``, so a step killed by its
timeout-minutes, cancelled, or dead before main() reached the handler left
a green job with no verdict, which the aggregate could only call "early-exit
or no-output" (test_review_gemini.py pins that gap).

The net mirrors claude's shape rather than codex's because of what the two
shapes do downstream, measured here in ``TestWhatTheAggregateMakesOfIt``: a
``failed`` payload reaches the aggregate, which names the reviewer on the
headline and in the roster and exits 1, while an absent payload only lowers
the response count -- three absent payloads end green.

Test-module hygiene: all imports belong at the top of this file per PEP 8 --
do not add ``import foo`` statements inside test function bodies.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from aggregate_reviews import FAILED_DETAIL_PREFIX, REVIEWER_NAMES, _missing_reason, main

# The field set claude's net writes; the gemini net copies it.
_NET_VERDICT: dict[str, Any] = {
    "summary": "Gemini review failed: no verdict file produced -- outcome=failure",
    "status": "failed",
    "early_exit": False,
    "issues": [],
    "error": "script_invocation_failed",
    "error_detail": "review_gemini.py outcome=failure; no verdict file written",
}


def _read_back(output_path: Path) -> dict[str, Any]:
    """Parse $GITHUB_OUTPUT the way the workflow would read it."""
    emitted = dict(
        line.split("=", 1) for line in output_path.read_text(encoding="utf-8").splitlines()
    )
    return {**emitted, "roster": json.loads(emitted["reviewer_roster"])}


def _aggregate(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    reviews: dict[str, dict[str, Any] | None],
) -> tuple[int, str, dict[str, Any]]:
    """Run the aggregate over ``reviews`` with every job green.

    Every REVIEWER_RESULT_* is "success" because that is what a
    continue-on-error reviewer step leaves behind. Returns the exit code,
    the posted verdict and the roster written to $GITHUB_OUTPUT.
    """
    output_path = tmp_path / "gh-output"
    output_path.unlink(missing_ok=True)
    monkeypatch.setenv("GITHUB_OUTPUT", str(output_path))
    monkeypatch.setenv("PR_NUMBER", "42")
    monkeypatch.setenv("GITHUB_REPOSITORY", "owner/repo")
    for name in REVIEWER_NAMES:
        monkeypatch.setenv(f"REVIEWER_RESULT_{name.upper()}", "success")
    code = 0
    with (
        patch("aggregate_reviews.load_reviews", return_value=reviews),
        patch("aggregate_reviews.post_verdict") as mock_post,
    ):
        try:
            main()
        except SystemExit as exc:
            code = int(exc.code or 0)
    return code, mock_post.call_args[0][1], _read_back(output_path)["roster"]


class TestWhatTheAggregateMakesOfIt:
    """The ticket's measurement: one failed verdict ends red, three absent end green."""

    def test_one_failed_gemini_verdict_with_two_absent_ends_red(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        nothing: dict[str, dict[str, Any] | None] = {n: None for n in REVIEWER_NAMES}
        code, verdict, roster = _aggregate(monkeypatch, tmp_path, nothing)
        # Three absent payloads with every job green: approve, exit 0. This
        # is the round a dead gemini step used to produce when the other two
        # reviewers were also absent, and nothing in it names gemini.
        assert (code, verdict) == (0, "approve")
        assert roster["missing"] == {n: _missing_reason("success") for n in REVIEWER_NAMES}

        with_net = {**nothing, "gemini": dict(_NET_VERDICT)}
        code, verdict, roster = _aggregate(monkeypatch, tmp_path, with_net)
        # The same round with the net's verdict in place of the absence:
        # comment, exit 1, and gemini named with its own detail.
        assert (code, verdict) == (1, "comment")
        assert roster["responded"] == []
        assert roster["missing"]["gemini"] == f"{FAILED_DETAIL_PREFIX}script_invocation_failed"
        assert roster["missing"]["claude"] == _missing_reason("success")
        assert roster["missing"]["codex"] == _missing_reason("success")

    def test_the_failed_verdict_does_not_count_as_a_response(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # Two live reviewers clear the verdict gate; the failed gemini must
        # still be missing, not a third response, so the formal approval is
        # withheld (AT-2124) and the roster names it.
        live = {
            n: {"summary": f"{n} review", "status": "ok", "early_exit": False, "issues": []}
            for n in ("claude", "codex")
        }
        reviews: dict[str, dict[str, Any] | None] = {**live, "gemini": dict(_NET_VERDICT)}
        code, verdict, roster = _aggregate(monkeypatch, tmp_path, reviews)
        assert (code, verdict) == (0, "approve")
        assert roster["responded"] == ["claude", "codex"]
        assert roster["missing"] == {"gemini": f"{FAILED_DETAIL_PREFIX}script_invocation_failed"}
