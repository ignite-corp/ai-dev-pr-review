"""A run cancelled from outside posts no verdict for the head it still owns (AT-3410).

A reviewer job's always() step 'Record cancel cause' in
base-ai-review-single.yml writes ``cancel_cause`` into the reviewer's verdict
file when the job is being cancelled: ``external_cancel`` when the job's
elapsed time is short of its cap, ``timeout`` otherwise. The aggregate posts
nothing, with reason ``head_superseded``, only when a reviewer recorded
``external_cancel``, the head is unchanged, and no reviewer produced a usable
payload. Everything else posts as before.

The shapes below are measured runs:

* 43020520145 -- job timeout, 60 s cap, landed at 72 s;
* 43020532010 -- ``gh run cancel``, 480 s cap, landed at 115 s;
* 43020544005 -- concurrency supersede, 480 s cap, landed at 117 s;
* 42937609117 -- dev-dotfiles#252, all three reviewers cancelled early by a
  same-head dispatch, nets wrote failed verdicts, "0/3 all failed" posted;
* 33615176056 -- all three reviewers cancelled, two had already uploaded real
  reviews: the partial verdict is a real one and still posts.

In all three probes ``job.status`` read ``cancelled`` and only elapsed time
told them apart, which is what the step's arithmetic is tested against.

Test-module hygiene: all imports belong at the top of this file per PEP 8 --
do not add ``import foo`` statements inside test function bodies.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

import pytest
import yaml

import aggregate_reviews as aggregate

_SINGLE = (
    Path(__file__).resolve().parents[2] / "workflows" / "base-ai-review-single.yml"
)
_CAUSE_STEP = "Record cancel cause"
_START_STEP = "Record job start"
_UPLOAD_STEP = "Upload review artifact"
_HEAD = "a" * 40
_NEWER = "b" * 40
_FIELD = "cancel_cause"
_EXTERNAL = "external_cancel"
_TIMEOUT = "timeout"

requires_jq = pytest.mark.skipif(
    shutil.which("jq") is None and not os.environ.get("CI"),
    reason="jq not installed",
)


def _failed(reviewer: str, cause: str | None) -> dict[str, Any]:
    """The failed verdict a reviewer net writes, as the cause step leaves it."""
    verdict: dict[str, Any] = {
        "summary": f"{reviewer} review failed: no verdict file produced",
        "status": "failed",
        "early_exit": False,
        "issues": [],
        "error": "action_invocation_failed",
    }
    if cause is not None:
        verdict[_FIELD] = cause
    return verdict


def _ok(cause: str | None) -> dict[str, Any]:
    verdict: dict[str, Any] = {
        "summary": "Review completed",
        "status": "ok",
        "early_exit": False,
        "issues": [],
    }
    if cause is not None:
        verdict[_FIELD] = cause
    return verdict


@pytest.fixture
def round_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """An aggregate CWD whose reviewer files are real, read by load_reviews."""
    monkeypatch.chdir(tmp_path)
    output = tmp_path / "outputs"
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", "")
    monkeypatch.setenv("GITHUB_REPOSITORY", "target/repository")
    monkeypatch.setenv("PR_NUMBER", "42")
    monkeypatch.setenv("HEAD_SHA", _HEAD)
    monkeypatch.setenv("PREPARE_RESULT", "success")
    monkeypatch.setenv("SIZE_SKIPPED", "false")
    monkeypatch.setenv("POLICY_SKIPPED", "false")
    monkeypatch.setenv("REVIEW_MODE", "parallel")
    monkeypatch.setenv("ALLOW_AUTO_APPROVE", "false")
    for name in aggregate.REVIEWERS:
        # Every probe and incident run reported its reviewers `cancelled`.
        monkeypatch.setenv(f"REVIEWER_RESULT_{name.upper()}", "cancelled")
    monkeypatch.setattr(aggregate, "fetch_round_count", lambda *args: 0)
    monkeypatch.setattr(aggregate, "_fetch_fold_comments", lambda *args: [])
    monkeypatch.setattr(aggregate, "_fetch_inline_posts", lambda *args: [])
    monkeypatch.setattr(aggregate, "_fetch_disposition_threads", lambda *args: [])
    monkeypatch.setattr(aggregate, "_fetch_quoted_dispositions", lambda *args: [])
    monkeypatch.setattr(
        aggregate.subprocess,
        "run",
        lambda *args, **kwargs: pytest.fail("unexpected external call"),
    )
    return tmp_path


def _write(round_dir: Path, verdicts: dict[str, dict[str, Any] | str]) -> None:
    for name, verdict in verdicts.items():
        text = verdict if isinstance(verdict, str) else json.dumps(verdict)
        (round_dir / f"review-{name}.json").write_text(text, encoding="utf-8")


def _drive(
    monkeypatch: pytest.MonkeyPatch, live_head: str | None
) -> tuple[list[tuple[Any, ...]], int | str | None, dict[str, Any] | None]:
    """Run main(); return the post_verdict calls, the exit code, the result."""
    posts: list[tuple[Any, ...]] = []

    def post(*args: Any, **kwargs: Any) -> bool:
        posts.append(args)
        return True

    monkeypatch.setattr(aggregate, "post_verdict", post)
    monkeypatch.setattr(aggregate, "_current_pr_head", lambda *args: live_head)
    code: int | str | None = 0
    try:
        aggregate.main()
    except SystemExit as exc:
        code = exc.code
    result = None
    output = Path(os.environ["GITHUB_OUTPUT"])
    if output.exists():
        for line in output.read_text(encoding="utf-8").splitlines():
            if line.startswith("review_result="):
                result = json.loads(line.split("=", 1)[1])
    return posts, code, result


def _all(cause: str | None) -> dict[str, dict[str, Any] | str]:
    return {name: _failed(name, cause) for name in aggregate.REVIEWERS}


class TestTheProbeShapes:
    """The three measured shapes, each reviewer leaving a net's failed verdict."""

    def test_a_job_timeout_still_posts(
        self, round_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # 43020520145: the cap fired, which is a real outcome of this round.
        _write(round_dir, _all(_TIMEOUT))
        posts, _, _ = _drive(monkeypatch, _HEAD)
        assert len(posts) == 1

    @pytest.mark.parametrize(
        "run_id", ["43020532010-run-cancel", "43020544005-concurrency-supersede"]
    )
    def test_an_external_cancel_with_no_payload_posts_nothing(
        self, round_dir: Path, monkeypatch: pytest.MonkeyPatch, run_id: str
    ) -> None:
        _write(round_dir, _all(_EXTERNAL))
        posts, code, result = _drive(monkeypatch, _HEAD)
        assert posts == []
        # Not green: nothing reviewed this head, as on the stale-head path.
        assert code == 1
        assert result is not None
        assert result["reason"] == "head_superseded"
        assert result["posted"] is False


class TestTheIncidentShapes:
    def test_42937609117_all_cancelled_early_posts_nothing(
        self, round_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The defect: "0/3 all failed" for reviewers that were cancelled."""
        _write(round_dir, _all(_EXTERNAL))
        posts, code, _ = _drive(monkeypatch, _HEAD)
        assert posts == []
        assert code == 1

    def test_33615176056_real_reviews_before_the_cancel_still_post(
        self, round_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Two reviewers finished before the cancel: that partial verdict is real."""
        names = list(aggregate.REVIEWERS)
        verdicts: dict[str, dict[str, Any] | str] = {
            names[0]: _ok(_EXTERNAL),
            names[1]: _ok(_EXTERNAL),
        }
        verdicts.update({name: _failed(name, _EXTERNAL) for name in names[2:]})
        _write(round_dir, verdicts)
        posts, _, _ = _drive(monkeypatch, _HEAD)
        assert len(posts) == 1


class TestEverythingElsePostsAsBefore:
    def test_an_artifact_without_the_field_posts(
        self, round_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A reviewer pinned to a release before AT-3410 writes no cause."""
        _write(round_dir, _all(None))
        posts, _, _ = _drive(monkeypatch, _HEAD)
        assert len(posts) == 1

    @pytest.mark.parametrize(
        "value",
        ["External_Cancel", "external_cancel ", "", 1, True, None, ["external_cancel"]],
    )
    def test_an_unrecognised_cause_posts(
        self, round_dir: Path, monkeypatch: pytest.MonkeyPatch, value: Any
    ) -> None:
        verdicts = _all(None)
        for verdict in verdicts.values():
            assert isinstance(verdict, dict)
            verdict[_FIELD] = value
        _write(round_dir, verdicts)
        posts, _, _ = _drive(monkeypatch, _HEAD)
        assert len(posts) == 1

    def test_an_unreadable_artifact_carries_no_cause(
        self, round_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        names = list(aggregate.REVIEWERS)
        verdicts: dict[str, dict[str, Any] | str] = {
            name: '{"cancel_cause": "external_cancel", "summary": ' for name in names
        }
        _write(round_dir, verdicts)
        posts, _, _ = _drive(monkeypatch, _HEAD)
        assert len(posts) == 1

    def test_an_undeterminable_head_posts(
        self, round_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Suppression needs the head confirmed unchanged, not merely unrefuted."""
        _write(round_dir, _all(_EXTERNAL))
        posts, _, _ = _drive(monkeypatch, None)
        assert len(posts) == 1

    def test_no_reviewed_head_posts(
        self, round_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _write(round_dir, _all(_EXTERNAL))
        monkeypatch.delenv("HEAD_SHA")
        posts, _, _ = _drive(monkeypatch, _HEAD)
        assert len(posts) == 1

    def test_one_external_cancel_among_timeouts_is_enough(
        self, round_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A cancel reaches the reviewers at different points in their caps."""
        names = list(aggregate.REVIEWERS)
        verdicts: dict[str, dict[str, Any] | str] = {
            name: _failed(name, _TIMEOUT) for name in names
        }
        verdicts[names[0]] = _failed(names[0], _EXTERNAL)
        _write(round_dir, verdicts)
        posts, _, _ = _drive(monkeypatch, _HEAD)
        assert posts == []

    def test_a_superseded_head_keeps_its_own_path(
        self, round_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """AT-2092's path is unchanged: it never reads the reviewer files."""
        _write(round_dir, _all(None))
        monkeypatch.setattr(
            aggregate,
            "load_reviews",
            lambda: pytest.fail("a superseded head read the reviewer files"),
        )
        posts, code, result = _drive(monkeypatch, _NEWER)
        assert posts == []
        assert code == 1
        assert result is not None
        assert result["reason"] == "head_superseded"


# --- The reviewer side: the step's own bash ---------------------------------


def _steps() -> list[dict[str, Any]]:
    return yaml.safe_load(_SINGLE.read_text(encoding="utf-8"))["jobs"]["review"][
        "steps"
    ]


def _step(name: str) -> dict[str, Any]:
    matches = [step for step in _steps() if step.get("name") == name]
    assert len(matches) == 1, name
    return matches[0]


def _run_cause(
    tmp_path: Path,
    *,
    job_status: str,
    elapsed: int | None,
    cap_minutes: str = "8",
    reviewer: str = "claude",
) -> subprocess.CompletedProcess[str]:
    env = {
        "PATH": os.environ["PATH"],
        "REVIEWER": reviewer,
        "JOB_STATUS": job_status,
        "JOB_CAP_MINUTES": cap_minutes,
        "STARTED": "" if elapsed is None else str(int(time.time()) - elapsed),
        # The margin the YAML gives the step, not a copy of it.
        "MARGIN_SECONDS": str(_step(_CAUSE_STEP)["env"]["MARGIN_SECONDS"]),
    }
    return subprocess.run(
        ["bash", "--noprofile", "--norc", "-e", "-o", "pipefail", "-c"]
        + [_step(_CAUSE_STEP)["run"]],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )


def _read(tmp_path: Path, reviewer: str = "claude") -> dict[str, Any]:
    return json.loads(
        (tmp_path / f"review-{reviewer}.json").read_text(encoding="utf-8")
    )


@requires_jq
class TestTheCauseStep:
    @pytest.mark.parametrize(
        ("cap_minutes", "elapsed", "expected"),
        [
            # 43020520145: 60 s cap, the step ran 72 s in.
            ("1", 72, _TIMEOUT),
            # 43020532010 and 43020544005: 480 s cap, 115 s and 117 s in.
            ("8", 115, _EXTERNAL),
            ("8", 117, _EXTERNAL),
            # Inside the margin before the cap reads as the cap.
            ("8", 8 * 60 - 30, _TIMEOUT),
            ("8", 8 * 60 - 60, _TIMEOUT),
            ("25", 25 * 60 - 1, _TIMEOUT),
            # A few seconds inside, so a clock tick during the test cannot cross.
            ("15", 15 * 60 - 65, _EXTERNAL),
            # Below two minutes the margin is half the cap.
            ("1", 30, _TIMEOUT),
            ("1", 25, _EXTERNAL),
        ],
        ids=[
            "probe-timeout",
            "probe-run-cancel",
            "probe-supersede",
            "near-cap",
            "margin-boundary",
            "near-default-cap",
            "15m",
            "short-cap-boundary",
            "short-cap-early",
        ],
    )
    def test_a_cancelled_job_is_classified_by_elapsed_time(
        self, tmp_path: Path, cap_minutes: str, elapsed: int, expected: str
    ) -> None:
        _write(tmp_path, {"claude": _failed("claude", None)})
        result = _run_cause(
            tmp_path, job_status="cancelled", elapsed=elapsed, cap_minutes=cap_minutes
        )
        assert result.returncode == 0, result.stderr
        assert _read(tmp_path)[_FIELD] == expected

    @pytest.mark.parametrize(
        ("started", "cap"),
        [(None, "8"), (115, ""), (115, "x"), (115, "0")],
        ids=["no-start", "no-cap", "bad-cap", "zero-cap"],
    )
    def test_a_cancel_it_cannot_time_reads_as_timeout(
        self, tmp_path: Path, started: int | None, cap: str
    ) -> None:
        """Unknown falls toward posting, the direction that loses nothing."""
        _write(tmp_path, {"claude": _failed("claude", None)})
        result = _run_cause(
            tmp_path, job_status="cancelled", elapsed=started, cap_minutes=cap
        )
        assert result.returncode == 0, result.stderr
        assert _read(tmp_path)[_FIELD] == _TIMEOUT

    @pytest.mark.parametrize("status", ["success", "failure"])
    def test_a_job_not_being_cancelled_strips_a_model_written_cause(
        self, tmp_path: Path, status: str
    ) -> None:
        """A reviewer's own output cannot forge the field the aggregate reads."""
        _write(tmp_path, {"claude": _ok(_EXTERNAL)})
        result = _run_cause(tmp_path, job_status=status, elapsed=10)
        assert result.returncode == 0, result.stderr
        assert _FIELD not in _read(tmp_path)
        assert _read(tmp_path)["summary"] == "Review completed"

    def test_a_cancelled_job_overwrites_a_model_written_cause(
        self, tmp_path: Path
    ) -> None:
        _write(tmp_path, {"claude": _ok(_EXTERNAL)})
        result = _run_cause(
            tmp_path, job_status="cancelled", elapsed=8 * 60, cap_minutes="8"
        )
        assert result.returncode == 0, result.stderr
        assert _read(tmp_path)[_FIELD] == _TIMEOUT

    def test_a_job_not_being_cancelled_leaves_the_file_byte_for_byte(
        self, tmp_path: Path
    ) -> None:
        text = '{"summary":"fine","status":"ok","issues":[]}'
        _write(tmp_path, {"claude": text})
        result = _run_cause(tmp_path, job_status="success", elapsed=10)
        assert result.returncode == 0, result.stderr
        assert (tmp_path / "review-claude.json").read_text(encoding="utf-8") == text

    @pytest.mark.parametrize("status", ["success", "cancelled"])
    def test_no_verdict_file_stays_no_verdict_file(
        self, tmp_path: Path, status: str
    ) -> None:
        result = _run_cause(tmp_path, job_status=status, elapsed=10)
        assert result.returncode == 0, result.stderr
        assert not (tmp_path / "review-claude.json").exists()

    @pytest.mark.parametrize("status", ["success", "cancelled"])
    def test_a_file_that_is_not_one_object_is_not_shipped(
        self, tmp_path: Path, status: str
    ) -> None:
        """The nets leave one object; anything else is a payload no step vouches for."""
        _write(tmp_path, {"claude": '{"cancel_cause":"external_cancel"}{}'})
        result = _run_cause(tmp_path, job_status=status, elapsed=10)
        assert result.returncode == 0, result.stderr
        assert not (tmp_path / "review-claude.json").exists()

    def test_a_link_at_the_verdict_path_is_not_followed(self, tmp_path: Path) -> None:
        outside = tmp_path / "outside.json"
        outside.write_text(json.dumps(_failed("claude", None)), encoding="utf-8")
        work = tmp_path / "work"
        work.mkdir()
        (work / "review-claude.json").symlink_to(outside)
        result = _run_cause(work, job_status="cancelled", elapsed=10)
        assert result.returncode == 0, result.stderr
        assert _FIELD not in json.loads(outside.read_text(encoding="utf-8"))
        assert not (work / "review-claude.json").exists()


class TestTheWorkflowShape:
    def test_the_start_is_the_first_step_and_a_step_output(self) -> None:
        """A step output cannot be rewritten by a later step, models included."""
        first = _steps()[0]
        assert first["name"] == _START_STEP
        assert "GITHUB_OUTPUT" in first["run"]
        assert _step(_CAUSE_STEP)["env"]["STARTED"] == (
            f"${{{{ steps.{first['id']}.outputs.epoch }}}}"
        )

    def test_the_cause_step_runs_always_and_reads_the_job_status(self) -> None:
        step = _step(_CAUSE_STEP)
        assert step["if"] == "always()"
        assert step["env"]["JOB_STATUS"] == "${{ job.status }}"
        assert "continue-on-error" not in step
        assert _FIELD in step["run"]
        assert _EXTERNAL in step["run"]

    def test_the_cap_it_measures_against_is_the_job_cap(self) -> None:
        job = yaml.safe_load(_SINGLE.read_text(encoding="utf-8"))["jobs"]["review"]
        assert _step(_CAUSE_STEP)["env"]["JOB_CAP_MINUTES"] == job["timeout-minutes"]

    def test_the_cause_step_is_the_last_writer_before_the_upload(self) -> None:
        """Every net, and every other always() step, has written by then."""
        names = [step.get("name") for step in _steps()]
        cause = names.index(_CAUSE_STEP)
        assert names[cause + 1] == _UPLOAD_STEP
        assert names[-1] == _UPLOAD_STEP
