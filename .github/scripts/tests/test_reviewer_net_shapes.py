"""The three reviewer nets record a dead step the same way (AT-2603).

Each reviewer step in base-ai-review-single.yml is continue-on-error, so
the path's always() net is what records a reviewer that wrote no verdict.
Before AT-2603 the three nets differed:

* claude's judged presence by ``[ -s ]`` alone, so a file cut mid-write
  passed as a verdict and the aggregate then dropped it as malformed --
  the absence the net exists to replace;
* gemini's wrote ``script_invocation_failed`` with no cause, so a step cut
  by its timeout read the same as any other death;
* codex's failed the job instead of writing a verdict, so a cut turned the
  job red and left the roster only "failed (see logs)".

These tests run each net's own ``run:`` block under bash, then feed what it
wrote to the aggregate's renderers.

Test-module hygiene: all imports belong at the top of this file per PEP 8 --
do not add ``import foo`` statements inside test function bodies.
"""

from __future__ import annotations

import difflib
import json
import os
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
import yaml

from aggregate_reviews import (
    ERROR_CAUSE_TEXT,
    FAILED_DETAIL_PREFIX,
    _failed_status_detail,
    _partial_short_message,
    _payload_failure_reason,
)
from aggregate_reviews import main as aggregate_main

_SINGLE = (
    Path(__file__).resolve().parents[2] / "workflows" / "base-ai-review-single.yml"
)
_CLAUDE_NET = "Emit Claude error verdict (no verdict file)"
_GEMINI_NET = "Emit Gemini error verdict (no verdict file)"
_CODEX_NET = "Verify Codex verdict file"
_GEMINI_RUN = "Run Gemini review"
_CODEX_RUN = "Run Codex review"
_GEMINI_KIND = "script_invocation_failed"
_CODEX_KIND = "cli_invocation_failed"
_TIMEOUT = "timeout"
_TRUNCATED = '{"status": "o'
_NOT_OBJECT_SUFFIX = "verdict file present but not a JSON object, replaced"
# Well past any cap the nets are given, and well inside it.
_LONG_AGO_SECONDS = 3600
_JUST_NOW_SECONDS = 5

# The nets' bash is only exercised here and in the sibling net modules; a
# missing jq skips on a developer machine and fails in CI.
requires_jq = pytest.mark.skipif(
    shutil.which("jq") is None and not os.environ.get("CI"),
    reason="jq not installed",
)


def _date_reads_file_mtime() -> bool:
    probe = subprocess.run(
        ["date", "-r", __file__, "+%s"], capture_output=True, check=False
    )
    return probe.returncode == 0


# The timeout cases read the start marker's mtime with `date -r FILE`, which
# BSD date does not take; same rule as jq: skip off CI, fail in CI.
requires_gnu_date = pytest.mark.skipif(
    not _date_reads_file_mtime() and not os.environ.get("CI"),
    reason="date -r FILE is GNU-only",
)


def _step(name: str) -> dict[str, Any]:
    steps = yaml.safe_load(_SINGLE.read_text(encoding="utf-8"))["jobs"]["review"][
        "steps"
    ]
    matches = [step for step in steps if step.get("name") == name]
    assert len(matches) == 1, name
    return matches[0]


def _run(
    tmp_path: Path, name: str, env: dict[str, str]
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            "bash",
            "--noprofile",
            "--norc",
            "-e",
            "-o",
            "pipefail",
            "-c",
            _step(name)["run"],
        ],
        cwd=tmp_path,
        env={"PATH": os.environ["PATH"], **env},
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )


def _marker(tmp_path: Path, age_seconds: int) -> Path:
    """A start marker whose mtime is ``age_seconds`` in the past."""
    marker = tmp_path / "runner-temp" / "start"
    marker.parent.mkdir(exist_ok=True)
    marker.touch()
    then = time.time() - age_seconds
    os.utime(marker, (then, then))
    return marker


def _timed_env(
    tmp_path: Path, name: str, outcome: str, age_seconds: int | None
) -> dict[str, str]:
    """The env a gemini or codex net reads, with the cap the YAML gives it."""
    env = {
        "STEP_OUTCOME": outcome,
        "STEP_CAP_MINUTES": str(_step(name)["env"]["STEP_CAP_MINUTES"]),
        "START_MARKER": str(tmp_path / "runner-temp" / "absent"),
    }
    if age_seconds is not None:
        env["START_MARKER"] = str(_marker(tmp_path, age_seconds))
    return env


def _verdict(tmp_path: Path, reviewer: str) -> dict[str, Any]:
    return json.loads(
        (tmp_path / f"review-{reviewer}.json").read_text(encoding="utf-8")
    )


def _claude_env(tmp_path: Path) -> dict[str, str]:
    """The env claude's net reads: no execution log, inner action failed."""
    return {
        "EXEC_FILE": "",
        "STEP_OUTCOME": "success",
        "INNER_OUTCOME": "failure",
        "ACTOR_CHECK_FILE": str(tmp_path / "no-check"),
        "STEP_CAP_MINUTES": "10",
    }


@requires_jq
class TestClaudeNetTreatsATruncatedFileAsMissing:
    def _run_claude(self, tmp_path: Path) -> subprocess.CompletedProcess[str]:
        return _run(tmp_path, _CLAUDE_NET, _claude_env(tmp_path))

    def test_truncated_claude_file_is_replaced_by_a_failed_verdict(
        self, tmp_path: Path
    ) -> None:
        # Red before AT-2603: `[ -s ]` read this as a verdict present.
        (tmp_path / "review-claude.json").write_text(_TRUNCATED, encoding="utf-8")
        result = self._run_claude(tmp_path)
        assert result.returncode == 0, result.stderr
        verdict = _verdict(tmp_path, "claude")
        assert verdict["status"] == "failed"
        assert verdict["error"] == "action_invocation_failed"
        assert verdict["error_detail"].endswith(_NOT_OBJECT_SUFFIX)

    def test_valid_claude_verdict_is_left_alone(self, tmp_path: Path) -> None:
        written = json.dumps({"summary": "fine", "status": "ok", "issues": []})
        (tmp_path / "review-claude.json").write_text(written, encoding="utf-8")
        result = self._run_claude(tmp_path)
        assert result.returncode == 0, result.stderr
        assert (tmp_path / "review-claude.json").read_text(encoding="utf-8") == written

    def test_missing_claude_file_does_not_claim_a_replacement(
        self, tmp_path: Path
    ) -> None:
        result = self._run_claude(tmp_path)
        assert result.returncode == 0, result.stderr
        assert _NOT_OBJECT_SUFFIX not in _verdict(tmp_path, "claude")["error_detail"]


@requires_jq
class TestGeminiNetNamesATimeout:
    @requires_gnu_date
    def test_gemini_timeout_yields_error_cause_timeout(self, tmp_path: Path) -> None:
        # Red before AT-2603: the net wrote no error_cause at all.
        env = _timed_env(tmp_path, _GEMINI_NET, "failure", _LONG_AGO_SECONDS)
        result = _run(tmp_path, _GEMINI_NET, env)
        assert result.returncode == 0, result.stderr
        verdict = _verdict(tmp_path, "gemini")
        assert verdict["error"] == _GEMINI_KIND
        assert verdict["error_cause"] == _TIMEOUT

    @requires_gnu_date
    def test_aggregate_renders_the_gemini_timeout(self, tmp_path: Path) -> None:
        env = _timed_env(tmp_path, _GEMINI_NET, "failure", _LONG_AGO_SECONDS)
        assert _run(tmp_path, _GEMINI_NET, env).returncode == 0
        verdict = _verdict(tmp_path, "gemini")
        rendered = f"{_GEMINI_KIND}: {ERROR_CAUSE_TEXT[_TIMEOUT]}"
        assert _failed_status_detail(verdict) == rendered
        assert _payload_failure_reason(verdict) == FAILED_DETAIL_PREFIX + rendered

    @pytest.mark.parametrize(
        ("outcome", "age"),
        [
            ("failure", _JUST_NOW_SECONDS),
            ("failure", None),
            ("cancelled", _LONG_AGO_SECONDS),
            ("success", _LONG_AGO_SECONDS),
        ],
        ids=["quick-failure", "no-marker", "cancelled", "success"],
    )
    def test_gemini_non_timeout_failure_does_not_claim_timeout(
        self, tmp_path: Path, outcome: str, age: int | None
    ) -> None:
        env = _timed_env(tmp_path, _GEMINI_NET, outcome, age)
        result = _run(tmp_path, _GEMINI_NET, env)
        assert result.returncode == 0, result.stderr
        verdict = _verdict(tmp_path, "gemini")
        assert "error_cause" not in verdict
        assert _failed_status_detail(verdict) == _GEMINI_KIND


@requires_jq
class TestCodexNetWritesAFailedVerdict:
    def test_codex_missing_verdict_yields_cli_invocation_failed_without_failing(
        self, tmp_path: Path
    ) -> None:
        # Red before AT-2603: the step exited 1 and wrote nothing.
        env = _timed_env(tmp_path, _CODEX_NET, "failure", _JUST_NOW_SECONDS)
        result = _run(tmp_path, _CODEX_NET, {**env, "EXIT_CODE": "1"})
        assert result.returncode == 0, result.stderr
        verdict = _verdict(tmp_path, "codex")
        assert verdict["status"] == "failed"
        assert verdict["early_exit"] is False
        assert verdict["issues"] == []
        assert verdict["error"] == _CODEX_KIND
        assert "error_cause" not in verdict
        assert "::warning::" in result.stdout

    @requires_gnu_date
    def test_codex_timeout_yields_error_cause_timeout(self, tmp_path: Path) -> None:
        # A step cut by its cap writes no exit_code output.
        env = _timed_env(tmp_path, _CODEX_NET, "failure", _LONG_AGO_SECONDS)
        result = _run(tmp_path, _CODEX_NET, env)
        assert result.returncode == 0, result.stderr
        verdict = _verdict(tmp_path, "codex")
        assert verdict["error_cause"] == _TIMEOUT
        rendered = f"{_CODEX_KIND}: {ERROR_CAUSE_TEXT[_TIMEOUT]}"
        assert _payload_failure_reason(verdict) == FAILED_DETAIL_PREFIX + rendered

    @pytest.mark.parametrize(
        ("outcome", "age"),
        [
            ("failure", None),
            ("cancelled", _LONG_AGO_SECONDS),
            ("success", _LONG_AGO_SECONDS),
        ],
        ids=["no-marker", "cancelled", "success"],
    )
    def test_codex_non_timeout_failure_does_not_claim_timeout(
        self, tmp_path: Path, outcome: str, age: int | None
    ) -> None:
        env = _timed_env(tmp_path, _CODEX_NET, outcome, age)
        result = _run(tmp_path, _CODEX_NET, env)
        assert result.returncode == 0, result.stderr
        verdict = _verdict(tmp_path, "codex")
        assert "error_cause" not in verdict
        assert _failed_status_detail(verdict) == _CODEX_KIND

    def test_truncated_codex_file_is_replaced_by_a_failed_verdict(
        self, tmp_path: Path
    ) -> None:
        (tmp_path / "review-codex.json").write_text(_TRUNCATED, encoding="utf-8")
        env = _timed_env(tmp_path, _CODEX_NET, "failure", _JUST_NOW_SECONDS)
        result = _run(tmp_path, _CODEX_NET, env)
        assert result.returncode == 0, result.stderr
        verdict = _verdict(tmp_path, "codex")
        assert verdict["error"] == _CODEX_KIND
        assert verdict["error_detail"].endswith(_NOT_OBJECT_SUFFIX)

    def test_valid_codex_verdict_is_left_alone(self, tmp_path: Path) -> None:
        written = json.dumps({"summary": "fine", "status": "ok", "issues": []})
        (tmp_path / "review-codex.json").write_text(written, encoding="utf-8")
        env = _timed_env(tmp_path, _CODEX_NET, "success", _JUST_NOW_SECONDS)
        result = _run(tmp_path, _CODEX_NET, env)
        assert result.returncode == 0, result.stderr
        assert (tmp_path / "review-codex.json").read_text(encoding="utf-8") == written

    def test_codex_net_does_not_write_through_a_link(self, tmp_path: Path) -> None:
        outside = tmp_path / "outside.json"
        outside.write_text("keep", encoding="utf-8")
        (tmp_path / "review-codex.json").symlink_to(outside)
        env = _timed_env(tmp_path, _CODEX_NET, "failure", _JUST_NOW_SECONDS)
        result = _run(tmp_path, _CODEX_NET, env)
        assert result.returncode == 0, result.stderr
        assert outside.read_text(encoding="utf-8") == "keep"
        assert not (tmp_path / "review-codex.json").is_symlink()
        assert _verdict(tmp_path, "codex")["error"] == _CODEX_KIND


@pytest.mark.parametrize(
    ("net", "run"), [(_GEMINI_NET, _GEMINI_RUN), (_CODEX_NET, _CODEX_RUN)]
)
def test_net_cap_matches_its_run_step_timeout(net: str, run: str) -> None:
    # An expression cannot read another step's timeout-minutes, so the net
    # carries a literal; a cap that drifts from the step's would misjudge
    # every timeout.
    assert int(_step(net)["env"]["STEP_CAP_MINUTES"]) == int(
        _step(run)["timeout-minutes"]
    )


@pytest.mark.parametrize("net", [_GEMINI_NET, _CODEX_NET])
def test_net_start_marker_is_the_one_written_before_the_run_step(net: str) -> None:
    reviewer = "gemini" if net == _GEMINI_NET else "codex"
    assert (
        _step(net)["env"]["START_MARKER"] == f"${{{{ runner.temp }}}}/{reviewer}-start"
    )


@pytest.mark.parametrize("net", [_GEMINI_NET, _CODEX_NET])
def test_net_reads_its_run_steps_outcome_and_is_not_advisory(net: str) -> None:
    run = _step(_GEMINI_RUN if net == _GEMINI_NET else _CODEX_RUN)
    step = _step(net)
    assert step["env"]["STEP_OUTCOME"] == f"${{{{ steps.{run['id']}.outcome }}}}"
    assert "always()" in str(step["if"])
    assert not step.get("continue-on-error", False)


@pytest.mark.parametrize(
    ("kind", "cause"),
    [
        (_GEMINI_KIND, "rejected"),
        (_GEMINI_KIND, "action_error"),
        (_CODEX_KIND, "rejected"),
        ("output_unparseable", _TIMEOUT),
    ],
)
def test_cause_the_kinds_net_never_emits_is_not_rendered(kind: str, cause: str) -> None:
    review = {"status": "failed", "error": kind, "error_cause": cause}
    assert _failed_status_detail(review) == kind


@requires_jq
@pytest.mark.parametrize(
    ("net", "reviewer"),
    [(_CLAUDE_NET, "claude"), (_GEMINI_NET, "gemini"), (_CODEX_NET, "codex")],
)
def test_directory_at_verdict_path_is_replaced_by_a_failed_verdict(
    tmp_path: Path, net: str, reviewer: str
) -> None:
    # A directory passes `[ -s ]` and fails the object check, so the net
    # falls through to its write; a bare redirect onto a directory fails
    # under set -e and takes the job down instead of recording the death.
    held = tmp_path / f"review-{reviewer}.json"
    held.mkdir()
    (held / "inside").write_text("x", encoding="utf-8")
    env = (
        _claude_env(tmp_path)
        if net == _CLAUDE_NET
        else _timed_env(tmp_path, net, "failure", _JUST_NOW_SECONDS)
    )
    result = _run(tmp_path, net, env)
    assert result.returncode == 0, result.stderr
    verdict = _verdict(tmp_path, reviewer)
    assert verdict["status"] == "failed"
    assert verdict["error_detail"].endswith(_NOT_OBJECT_SUFFIX)


# The timeout judgment each net carries, from the cap arithmetic to the
# line that names the cause. Three copies in one file, held identical here
# so a change to one that is not made to the others fails.
_TIMEOUT_BLOCK_START = 'case "${STEP_CAP_MINUTES:-}" in'
_TIMEOUT_BLOCK_END = 'ERROR_CAUSE="timeout"'


def _timeout_block(run: str) -> str:
    lines = [line.strip() for line in run.splitlines()]
    starts = [i for i, line in enumerate(lines) if line == _TIMEOUT_BLOCK_START]
    ends = [i for i, line in enumerate(lines) if line == _TIMEOUT_BLOCK_END]
    assert len(starts) == 1 and len(ends) == 1, (starts, ends)
    assert starts[0] < ends[0]
    return "\n".join(lines[starts[0] : ends[0] + 1])


def _assert_timeout_blocks_identical(runs: dict[str, str]) -> None:
    blocks = {name: _timeout_block(run) for name, run in runs.items()}
    assert len(set(blocks.values())) == 1, blocks


def _net_runs() -> dict[str, str]:
    return {
        net: str(_step(net)["run"]) for net in (_CLAUDE_NET, _GEMINI_NET, _CODEX_NET)
    }


def test_timeout_judgment_is_identical_in_all_three_nets() -> None:
    _assert_timeout_blocks_identical(_net_runs())


def _net_body(run: str, reviewer: str) -> str:
    """Gemini's or codex's net from its link guard to its self-check.

    The reviewer's name is the one thing allowed to differ, so it is
    normalised away; the kind and the outcome detail are set before the span.
    """
    lines = run.splitlines()
    start = f"if [ -L review-{reviewer}.json ]; then"
    end = f'jq -e -s "$OBJECT_FILTER" review-{reviewer}.json >/dev/null'
    starts = [i for i, line in enumerate(lines) if line == start]
    ends = [i for i, line in enumerate(lines) if line == end]
    assert len(starts) == 1 and len(ends) == 1, (reviewer, starts, ends)
    assert starts[0] < ends[0]
    body = "\n".join(lines[starts[0] : ends[0] + 1])
    return body.replace(reviewer.capitalize(), "<Name>").replace(reviewer, "<name>")


def _assert_net_bodies_identical(gemini_run: str, codex_run: str) -> None:
    gemini = _net_body(gemini_run, "gemini")
    codex = _net_body(codex_run, "codex")
    assert gemini == codex, "\n".join(
        difflib.unified_diff(
            gemini.splitlines(), codex.splitlines(), "gemini", "codex", lineterm=""
        )
    )


def test_gemini_and_codex_net_bodies_are_identical() -> None:
    runs = _net_runs()
    _assert_net_bodies_identical(runs[_GEMINI_NET], runs[_CODEX_NET])


def test_net_body_identity_check_refuses_an_edited_copy() -> None:
    runs = _net_runs()
    edited = runs[_GEMINI_NET].replace(
        "rm -f -- review-gemini.json", "rm -- review-gemini.json", 1
    )
    assert edited != runs[_GEMINI_NET]
    with pytest.raises(AssertionError):
        _assert_net_bodies_identical(edited, runs[_CODEX_NET])


def test_timeout_identity_check_refuses_an_edited_copy() -> None:
    runs = _net_runs()
    edited = runs[_CODEX_NET].replace('"$CAP_SECONDS" -gt 0', '"$CAP_SECONDS" -gt 1', 1)
    assert edited != runs[_CODEX_NET]
    with pytest.raises(AssertionError):
        _assert_timeout_blocks_identical({**runs, _CODEX_NET: edited})


# A cause renders only on a payload the nets wrote. The signal is
# `status: "failed"`: every model-output path re-derives a model-emitted
# status to ok/early_exit before the file is final (stamp_model_status,
# promote_legacy_verdict.sh), so a model can carry a net's kind and cause but
# not its status. A payload whose status was missing or unknown is
# fail-closed to "failed" by the aggregate, so its cause is dropped there.
_CAUSE_TEXTS = tuple(ERROR_CAUSE_TEXT.values())
_FORGED = [
    ("codex", _CODEX_KIND, _TIMEOUT),
    ("gemini", _GEMINI_KIND, _TIMEOUT),
    ("claude", "action_invocation_failed", _TIMEOUT),
    ("claude", "action_invocation_failed", "rejected"),
]


def _model_payload(kind: str, cause: str, status: str | None) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "summary": "looks fine",
        "early_exit": False,
        "issues": [],
        "error": kind,
        "error_cause": cause,
    }
    if status is not None:
        payload["status"] = status
    return payload


def _aggregate_outputs(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    payloads: dict[str, dict[str, Any]],
) -> str:
    """Run the real aggregate over payload files; return everything it emitted."""
    work = tmp_path / "aggregate"
    work.mkdir()
    for name, payload in payloads.items():
        (work / f"review-{name}.json").write_text(json.dumps(payload), "utf-8")
    monkeypatch.chdir(work)
    monkeypatch.setenv("GITHUB_OUTPUT", str(work / "gh-output"))
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(work / "step-summary"))
    monkeypatch.setenv("PR_NUMBER", "42")
    monkeypatch.setenv("GITHUB_REPOSITORY", "owner/repo")
    for name in ("claude", "codex", "gemini"):
        monkeypatch.setenv(f"REVIEWER_RESULT_{name.upper()}", "success")
    with patch("aggregate_reviews.post_verdict") as posted:
        try:
            aggregate_main()
        except SystemExit:
            pass
    captured = capsys.readouterr()
    emitted = [captured.out, captured.err, str(posted.call_args)]
    for name in ("gh-output", "step-summary"):
        path = work / name
        if path.exists():
            emitted.append(path.read_text(encoding="utf-8"))
    return "\n".join(emitted)


@pytest.mark.parametrize(("reviewer", "kind", "cause"), _FORGED)
def test_model_payload_with_a_net_kind_does_not_render_its_cause(
    reviewer: str, kind: str, cause: str
) -> None:
    payload = _model_payload(kind, cause, "ok")
    assert _partial_short_message(reviewer, payload, "success") == kind
    assert _failed_status_detail(payload) == kind


@pytest.mark.parametrize(("reviewer", "kind", "cause"), _FORGED)
@pytest.mark.parametrize(
    "status", ["ok", None, "bogus"], ids=["ok", "missing", "unknown"]
)
def test_model_payload_cause_reaches_no_aggregate_output(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    reviewer: str,
    kind: str,
    cause: str,
    status: str | None,
) -> None:
    payload = _model_payload(kind, cause, status)
    emitted = _aggregate_outputs(monkeypatch, capsys, tmp_path, {reviewer: payload})
    assert kind in emitted
    assert not [text for text in _CAUSE_TEXTS if text in emitted]


@pytest.mark.parametrize(("reviewer", "kind", "cause"), _FORGED)
def test_net_written_cause_still_reaches_the_aggregate_output(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    reviewer: str,
    kind: str,
    cause: str,
) -> None:
    # Positive control for the test above: the same payload with the
    # status only a net writes renders its cause on the roster and the
    # partial message alike.
    payload = _model_payload(kind, cause, "failed")
    emitted = _aggregate_outputs(monkeypatch, capsys, tmp_path, {reviewer: payload})
    rendered = f"{kind}: {ERROR_CAUSE_TEXT[cause]}"
    assert _partial_short_message(reviewer, payload, "success") == rendered
    assert FAILED_DETAIL_PREFIX + rendered in emitted


# Codex's run step can be cut after the model wrote review-codex.json and
# before the step stamped it. 'Normalize review file name' then decides
# whose that file is: only a run step that finished has adjudicated it, so
# anything else is promoted with model semantics, which re-derive a
# model-written "failed".
_NORMALIZE_STEP = "Normalize review file name"
_REPO_ROOT = Path(__file__).resolve().parents[3]


def _codex_workdir(tmp_path: Path, payload: dict[str, Any] | None) -> Path:
    work = tmp_path / "codex-work"
    runner_temp = work / "runner-temp"
    runner_temp.mkdir(parents=True)
    (runner_temp / "ai-dev-pr-review").symlink_to(_REPO_ROOT)
    marker = runner_temp / "codex-start"
    marker.touch()
    then = time.time() - _LONG_AGO_SECONDS
    os.utime(marker, (then, then))
    if payload is not None:
        (work / "review-codex.json").write_text(json.dumps(payload), "utf-8")
    return work


def _normalize_then_net(work: Path, run_outcome: str) -> dict[str, Any]:
    """Run 'Normalize review file name' then codex's net, as the job does."""
    runner_temp = work / "runner-temp"
    normalized = _run(
        work,
        _NORMALIZE_STEP,
        {
            "RUNNER_TEMP": str(runner_temp),
            "REVIEWER": "codex",
            "RUN_OUTCOME": run_outcome,
        },
    )
    assert normalized.returncode == 0, normalized.stderr
    netted = _run(
        work,
        _CODEX_NET,
        {
            "STEP_OUTCOME": run_outcome,
            "STEP_CAP_MINUTES": str(_step(_CODEX_NET)["env"]["STEP_CAP_MINUTES"]),
            "START_MARKER": str(runner_temp / "codex-start"),
        },
    )
    assert netted.returncode == 0, netted.stderr
    return _verdict(work, "codex")


def test_normalize_reads_the_run_steps_outcome() -> None:
    run = _step(_CODEX_RUN)
    env = _step(_NORMALIZE_STEP)["env"]
    assert env["RUN_OUTCOME"] == f"${{{{ steps.{run['id']}.outcome }}}}"


@requires_jq
@pytest.mark.parametrize("run_outcome", ["failure", "cancelled"])
def test_model_failed_payload_left_by_a_cut_codex_step_renders_no_cause(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    run_outcome: str,
) -> None:
    # Red before AT-2603 round 6: `promote --target-is-ours` kept the
    # model's "failed", the net passed the valid object through, and the
    # aggregate rendered the model's chosen cause.
    forged = {
        "summary": "looks fine",
        "status": "failed",
        "early_exit": False,
        "issues": [],
        "error": _CODEX_KIND,
        "error_cause": _TIMEOUT,
    }
    verdict = _normalize_then_net(_codex_workdir(tmp_path, forged), run_outcome)
    assert verdict["status"] != "failed"
    emitted = _aggregate_outputs(monkeypatch, capsys, tmp_path, {"codex": verdict})
    assert not [text for text in _CAUSE_TEXTS if text in emitted]


@requires_jq
def test_run_steps_own_failed_verdict_survives_normalize(tmp_path: Path) -> None:
    # What the run step writes when the CLI exits non-zero; the step then
    # finishes, so its "failed" is ours and must not be re-derived.
    own = {
        "summary": "Codex review failed: CLI exited 1",
        "status": "failed",
        "early_exit": False,
        "issues": [],
        "error": _CODEX_KIND,
        "error_detail": "",
    }
    verdict = _normalize_then_net(_codex_workdir(tmp_path, own), "success")
    assert verdict == own


@requires_jq
@requires_gnu_date
def test_net_written_timeout_after_normalize_renders_its_cause(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    verdict = _normalize_then_net(_codex_workdir(tmp_path, None), "failure")
    assert verdict["status"] == "failed"
    assert verdict["error_cause"] == _TIMEOUT
    emitted = _aggregate_outputs(monkeypatch, capsys, tmp_path, {"codex": verdict})
    assert f"{_CODEX_KIND}: {ERROR_CAUSE_TEXT[_TIMEOUT]}" in emitted


# Codex's net runs after the log upload, the sysctl restore and Normalize,
# so "now" at the net is later than the run step's end. The step's end is
# recorded by 'Drop symlinked codex artifacts before upload', the always()
# step right after it, and the net measures against that.
_DROP_STEP = "Drop symlinked codex artifacts before upload"
_CODEX_CAP_SECONDS = int(_step(_CODEX_NET)["env"]["STEP_CAP_MINUTES"]) * 60
_RUN_ENDED_AT_SECONDS = _CODEX_CAP_SECONDS - 90
_SLOW_UPLOAD_SECONDS = 50


def _end_marker(tmp_path: Path, age_seconds: int) -> Path:
    marker = tmp_path / "runner-temp" / "end"
    marker.parent.mkdir(exist_ok=True)
    marker.touch()
    then = time.time() - age_seconds
    os.utime(marker, (then, then))
    return marker


def test_drop_step_records_the_codex_run_steps_end() -> None:
    drop = _step(_DROP_STEP)
    assert "always()" in str(drop["if"])
    assert ': > "${RUNNER_TEMP}/codex-end"' in str(drop["run"])
    assert _step(_CODEX_NET)["env"]["END_MARKER"] == "${{ runner.temp }}/codex-end"
    steps = yaml.safe_load(_SINGLE.read_text(encoding="utf-8"))["jobs"]["review"][
        "steps"
    ]
    names = [step.get("name") for step in steps]
    assert names.index(_DROP_STEP) == names.index(_CODEX_RUN) + 1, (
        "the end marker must be written by the step right after the run step; "
        "any later position charges the steps in between to the run step"
    )


@requires_jq
@requires_gnu_date
def test_codex_failure_under_the_cap_then_slow_upload_is_not_a_timeout(
    tmp_path: Path,
) -> None:
    # Red before AT-2603 round 7: the net measured to its own "now", so a
    # failure 90s before the cap plus a 50s upload crosses the net
    # threshold (60s before the cap) if upload latency is mischarged.
    started = _RUN_ENDED_AT_SECONDS + _SLOW_UPLOAD_SECONDS
    env = _timed_env(tmp_path, _CODEX_NET, "failure", started)
    env["END_MARKER"] = str(_end_marker(tmp_path, _SLOW_UPLOAD_SECONDS))
    result = _run(tmp_path, _CODEX_NET, env)
    assert result.returncode == 0, result.stderr
    assert "error_cause" not in _verdict(tmp_path, "codex")


@requires_jq
@requires_gnu_date
def test_codex_cut_at_the_cap_then_slow_upload_is_still_a_timeout(
    tmp_path: Path,
) -> None:
    started = _CODEX_CAP_SECONDS + _SLOW_UPLOAD_SECONDS
    env = _timed_env(tmp_path, _CODEX_NET, "failure", started)
    env["END_MARKER"] = str(_end_marker(tmp_path, _SLOW_UPLOAD_SECONDS))
    result = _run(tmp_path, _CODEX_NET, env)
    assert result.returncode == 0, result.stderr
    assert _verdict(tmp_path, "codex")["error_cause"] == _TIMEOUT
