"""The gemini always() net in base-ai-review-single.yml (AT-2539).

The reviewer run steps are continue-on-error, so a reviewer's own death does
not fail the job; each path has to record its own failure. claude's always()
step synthesises a ``status: "failed"`` verdict when none was written, and
codex's always() step fails the job. gemini recorded its failures only from
inside review_gemini.py's ``try/except``, so a step killed by its
timeout-minutes, or dead before main() reached the handler, wrote nothing
and the job still ended green with no verdict, which the aggregate could
only call "early-exit or no-output" (test_review_gemini.py pins that gap).
On cancellation the job concludes ``cancelled`` rather than green -- which
the aggregate already reports as non-benign -- and ``always()`` still runs
this net and the upload step, so the verdict is recorded there too.

The net mirrors claude's shape rather than codex's because of what the two
shapes do downstream, measured here in ``TestWhatTheAggregateMakesOfIt``: a
``failed`` payload reaches the aggregate, which names the reviewer on the
headline and in the roster and exits 1, while an absent payload only lowers
the response count -- three absent payloads end green.

``TestTheNetIsInTheWorkflow`` reads the YAML: its first test was red at
``d554aee``, where no gemini-gated step carried ``always()``, and the two
inversion tests make the shape wrong and show the same assertions refuse it.
``TestWhatTheNetWrites`` runs the step's own ``run:`` block under bash, so
the verdict the aggregate is fed below is the one the workflow would write.

Test-module hygiene: all imports belong at the top of this file per PEP 8 --
do not add ``import foo`` statements inside test function bodies.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from contextlib import ExitStack
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
import yaml

import review_gemini
from aggregate_reviews import (
    FAILED_DETAIL_PREFIX,
    REVIEWER_NAMES,
    _missing_reason,
    main,
)

_SINGLE = (
    Path(__file__).resolve().parents[2] / "workflows" / "base-ai-review-single.yml"
)
_RUN_STEP = "Run Gemini review"
_NET_STEP = "Emit Gemini error verdict (no verdict file)"
_GEMINI_GATE = "inputs.reviewer == 'gemini'"
# The first step every reviewer path shares after its own net; the net must
# have written the file before anything reads it.
_TAIL_STEP = "Post inline comments"
# The step the net's value rests on: a verdict written after the upload is
# a verdict nobody receives.
_UPLOAD_STEP = "Upload review artifact"
# The script's own constant, so a rename there that the net's bash does not
# follow fails here rather than leaving the net covering a stale name.
_VERDICT_FILE = review_gemini.REVIEW_FILE
_ERROR_KIND = "script_invocation_failed"

# A missing jq skips on a developer machine. In CI it fails instead: the
# class below is the only place the net's bash runs, and a silent skip there
# is a green build over zero coverage.
requires_jq = pytest.mark.skipif(
    shutil.which("jq") is None and not os.environ.get("CI"),
    reason="jq not installed",
)

_OUTCOME = "failure"


def _detail(outcome: str, *, not_object: bool) -> str:
    """The net's DETAIL, built the way the workflow builds it.

    One place for both branches -- the file was absent, or present but not
    JSON -- so the aggregate-facing tests below are fed what the workflow
    writes rather than a payload the pipeline never produces.
    """
    reason = (
        "verdict file present but not a JSON object, replaced"
        if not_object
        else "no verdict file written"
    )
    return f"review_gemini.py outcome={outcome}; {reason}"


def _summary(detail: str) -> str:
    """The net's summary, which carries the detail rather than restating it.

    The lead-in used to say "no verdict file produced" unconditionally, so
    on the not-an-object branch the posted verdict contradicted its own
    detail.
    """
    return f"Gemini review failed: {detail}"


_DETAIL = _detail(_OUTCOME, not_object=False)

# The payload the net writes, field for field -- claude's field set, which
# the gemini net copies.
_NET_VERDICT: dict[str, Any] = {
    "summary": _summary(_DETAIL),
    "status": "failed",
    "early_exit": False,
    "issues": [],
    "error": _ERROR_KIND,
    "error_detail": _DETAIL,
}


def _read_back(output_path: Path) -> dict[str, Any]:
    """Parse $GITHUB_OUTPUT the way the workflow would read it."""
    emitted = dict(
        line.split("=", 1)
        for line in output_path.read_text(encoding="utf-8").splitlines()
    )
    return {**emitted, "roster": json.loads(emitted["reviewer_roster"])}


def _aggregate(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    reviews: dict[str, dict[str, Any] | None] | None,
) -> tuple[int, str, dict[str, Any]]:
    """Run the aggregate with every job green.

    Every REVIEWER_RESULT_* is "success" because that is what a
    continue-on-error reviewer step leaves behind. ``reviews`` replaces the
    loader; ``None`` leaves the real loader reading the current directory.
    Returns the exit code, the posted verdict and the roster written to
    $GITHUB_OUTPUT.
    """
    output_path = tmp_path / "gh-output"
    output_path.unlink(missing_ok=True)
    monkeypatch.setenv("GITHUB_OUTPUT", str(output_path))
    monkeypatch.setenv("PR_NUMBER", "42")
    monkeypatch.setenv("GITHUB_REPOSITORY", "owner/repo")
    for name in REVIEWER_NAMES:
        monkeypatch.setenv(f"REVIEWER_RESULT_{name.upper()}", "success")
    code = 0
    with ExitStack() as stack:
        if reviews is not None:
            stack.enter_context(
                patch("aggregate_reviews.load_reviews", return_value=reviews)
            )
        mock_post = stack.enter_context(patch("aggregate_reviews.post_verdict"))
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
        assert roster["missing"] == {
            n: _missing_reason("success") for n in REVIEWER_NAMES
        }

        with_net = {**nothing, "gemini": dict(_NET_VERDICT)}
        code, verdict, roster = _aggregate(monkeypatch, tmp_path, with_net)
        # The same round with the net's verdict in place of the absence:
        # comment, exit 1, and gemini named with its own detail.
        assert (code, verdict) == (1, "comment")
        assert roster["responded"] == []
        assert roster["missing"]["gemini"] == f"{FAILED_DETAIL_PREFIX}{_ERROR_KIND}"
        assert roster["missing"]["claude"] == _missing_reason("success")
        assert roster["missing"]["codex"] == _missing_reason("success")

    def test_the_failed_verdict_does_not_count_as_a_response(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # Two live reviewers clear the verdict gate; the failed gemini must
        # still be missing, not a third response, so the formal approval is
        # withheld (AT-2124) and the roster names it.
        live = {
            n: {
                "summary": f"{n} review",
                "status": "ok",
                "early_exit": False,
                "issues": [],
            }
            for n in ("claude", "codex")
        }
        reviews: dict[str, dict[str, Any] | None] = {
            **live,
            "gemini": dict(_NET_VERDICT),
        }
        code, verdict, roster = _aggregate(monkeypatch, tmp_path, reviews)
        assert (code, verdict) == (0, "approve")
        assert roster["responded"] == ["claude", "codex"]
        assert roster["missing"] == {"gemini": f"{FAILED_DETAIL_PREFIX}{_ERROR_KIND}"}


def _steps(text: str) -> list[dict[str, Any]]:
    return list(yaml.safe_load(text)["jobs"]["review"]["steps"])


def _live_steps() -> list[dict[str, Any]]:
    return _steps(_SINGLE.read_text(encoding="utf-8"))


def _named(steps: list[dict[str, Any]], name: str) -> dict[str, Any]:
    matches = [step for step in steps if step.get("name") == name]
    assert len(matches) == 1, name
    return matches[0]


def _gemini_gated(steps: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [step for step in steps if _GEMINI_GATE in str(step.get("if", ""))]


def _assert_net_shape(steps: list[dict[str, Any]]) -> None:
    """Every property the net needs to catch a dead step.

    One function, so the inversion tests exercise exactly the assertions
    the live file passes: the net is the one gemini-gated step under
    ``always()``, it is not ``continue-on-error`` (if the failure cannot be
    recorded the job must not report success), it reads the run step's
    outcome by id, and it runs after the run step and before the shared
    tail that reads the file.
    """
    nets = [step for step in _gemini_gated(steps) if "always()" in str(step["if"])]
    assert [step["name"] for step in nets] == [_NET_STEP]
    net = nets[0]
    assert not net.get("continue-on-error", False)
    run = _named(steps, _RUN_STEP)
    assert run.get("continue-on-error") is True
    assert net["env"]["STEP_OUTCOME"] == f"${{{{ steps.{run['id']}.outcome }}}}"
    names = [step.get("name") for step in steps]
    assert names.index(_RUN_STEP) < names.index(_NET_STEP) < names.index(_TAIL_STEP)
    assert names.index(_NET_STEP) < names.index(_UPLOAD_STEP)


class TestTheNetIsInTheWorkflow:
    def test_a_gemini_gated_step_runs_under_always(self) -> None:
        """Red at d554aee: no gemini-gated step carried always()."""
        gated = _gemini_gated(_live_steps())
        assert gated, "the gate string no longer matches any step"
        assert any("always()" in str(step["if"]) for step in gated)

    def test_the_net_has_claudes_shape(self) -> None:
        _assert_net_shape(_live_steps())

    def test_the_shape_check_refuses_a_net_that_lost_always(self) -> None:
        steps = _live_steps()
        _named(steps, _NET_STEP)["if"] = _GEMINI_GATE
        with pytest.raises(AssertionError):
            _assert_net_shape(steps)
        _assert_net_shape(_live_steps())

    def test_the_shape_check_refuses_a_net_made_continue_on_error(self) -> None:
        steps = _live_steps()
        _named(steps, _NET_STEP)["continue-on-error"] = True
        with pytest.raises(AssertionError):
            _assert_net_shape(steps)
        _assert_net_shape(_live_steps())


def _run_net(
    tmp_path: Path, outcome: str, *, existing: str | None = None
) -> subprocess.CompletedProcess[str]:
    """Run the net's ``run:`` block the way the runner does, in ``tmp_path``."""
    script = str(_named(_live_steps(), _NET_STEP)["run"])
    if existing is not None:
        (tmp_path / _VERDICT_FILE).write_text(existing, encoding="utf-8")
    return subprocess.run(
        ["bash", "--noprofile", "--norc", "-eo", "pipefail", "-c", script],
        cwd=tmp_path,
        env={**os.environ, "STEP_OUTCOME": outcome},
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )


@requires_jq
class TestWhatTheNetWrites:
    def test_a_dead_step_gets_a_failed_verdict_naming_its_outcome(
        self, tmp_path: Path
    ) -> None:
        result = _run_net(tmp_path, _OUTCOME)
        assert result.returncode == 0, result.stderr
        verdict = json.loads((tmp_path / _VERDICT_FILE).read_text(encoding="utf-8"))
        # Values, not just key names: the payload below is what the
        # aggregate-facing tests are fed, so a drift between it and what the
        # net writes has to fail here rather than hide in a key-set compare.
        assert verdict == _NET_VERDICT
        assert "::warning::" in result.stdout

    def test_a_verdict_the_script_wrote_is_left_alone(self, tmp_path: Path) -> None:
        written = json.dumps(
            {"summary": "fine", "status": "ok", "early_exit": False, "issues": []}
        )
        result = _run_net(tmp_path, "success", existing=written)
        assert result.returncode == 0, result.stderr
        assert (tmp_path / _VERDICT_FILE).read_text(encoding="utf-8") == written
        assert "::notice::" in result.stdout

    def test_an_empty_file_counts_as_no_verdict(self, tmp_path: Path) -> None:
        result = _run_net(tmp_path, "failure", existing="")
        assert result.returncode == 0, result.stderr
        verdict = json.loads((tmp_path / _VERDICT_FILE).read_text(encoding="utf-8"))
        assert verdict["status"] == "failed"

    def test_a_truncated_file_counts_as_no_verdict(self, tmp_path: Path) -> None:
        # A step killed mid-write leaves a non-empty file that is not JSON.
        # `-s` alone accepts it, and the aggregate then drops it as
        # malformed -- the same absence the net exists to replace.
        result = _run_net(tmp_path, _OUTCOME, existing='{"status": "o')
        assert result.returncode == 0, result.stderr
        verdict = json.loads((tmp_path / _VERDICT_FILE).read_text(encoding="utf-8"))
        assert verdict["status"] == "failed"
        assert verdict["error"] == _ERROR_KIND
        # Same kind, but neither the detail nor the summary may claim
        # nothing was written -- the summary carries the detail, so the two
        # cannot disagree.
        not_object = _detail(_OUTCOME, not_object=True)
        assert verdict["error_detail"] == not_object
        assert verdict["error_detail"] != _DETAIL
        assert verdict["summary"] == _summary(not_object)
        assert "no verdict file produced" not in verdict["summary"]

    @pytest.mark.parametrize(
        "existing",
        ["  \n", "42", 'null {"a":1}'],
        ids=["whitespace", "scalar", "stream"],
    )
    def test_a_non_object_file_counts_as_no_verdict(
        self, tmp_path: Path, existing: str
    ) -> None:
        # `jq empty` exits 0 on the first two: zero values is not an error
        # to it, and a bare scalar parses. `jq -e` without --slurp judges
        # only the last value of a stream, so the third passed it. All
        # three are malformed to the aggregate's single-document load.
        result = _run_net(tmp_path, _OUTCOME, existing=existing)
        assert result.returncode == 0, result.stderr
        verdict = json.loads((tmp_path / _VERDICT_FILE).read_text(encoding="utf-8"))
        assert verdict["status"] == "failed"
        assert verdict["error_detail"] == _detail(_OUTCOME, not_object=True)

    def test_the_nets_verdict_turns_a_dead_gemini_round_red(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """End to end: the file the net wrote, read by the real loader, through main()."""
        result = _run_net(tmp_path, _OUTCOME)
        assert result.returncode == 0, result.stderr
        monkeypatch.chdir(tmp_path)
        code, verdict, roster = _aggregate(monkeypatch, tmp_path, None)
        assert (code, verdict) == (1, "comment")
        assert roster["responded"] == []
        assert roster["missing"]["gemini"] == f"{FAILED_DETAIL_PREFIX}{_ERROR_KIND}"
