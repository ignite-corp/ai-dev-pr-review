"""review_run_health.py: a runner review outage flags, a verdict does not.

The fixtures replay the shapes measured on the runner's review.yml around
AT-3353: on 2026-11-09 every run from 09:24Z failed in "Start target check"
at actions/setup-python, while the failures before it were reviews whose
verdict blocked the run (aggregate at its verdict step, finish at its
completion step) and the odd start that found its PR no longer open.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

import review_run_health as h

NOW = datetime(2026, 11, 10, 0, 0, tzinfo=UTC)
SETUP_PYTHON = "Run actions/setup-python@a309ff8b426b58ec0e2a45f0f869d46889d02405"
START_JOB = "Start target check"
RESOLVE_STEP = "Resolve PR head and create this attempt's check"
AGGREGATE_JOB = "review / aggregate / Aggregate & Verdict"


def _run(run_id: int, minutes_ago: int, conclusion: str) -> dict[str, object]:
    created = NOW - timedelta(minutes=minutes_ago)
    return {
        "id": run_id,
        "created_at": created.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "status": "completed",
        "conclusion": conclusion,
    }


# The annotation aggregate_reviews.py writes for a blocked verdict it posted,
# as the runner's pinned release renders it.
POSTED_BLOCK = (
    "BLOCK_MERGE_ON_VERDICT is on and the verdict does not pass (request_changes)"
    " -- failing the check so it blocks the merge; see the verdict comment"
)
UNPOSTED_BLOCK = (
    "BLOCK_MERGE_ON_VERDICT is on and the verdict does not pass (request_changes)"
    " -- failing the check so it blocks the merge; the verdict was not posted"
    " to the PR; see this run's log"
)


def _job_id(run_id: int, name: str) -> int:
    return run_id * 10 + (1 if name == AGGREGATE_JOB else 2)


def _job(run_id: int, name: str, step: str | None) -> dict[str, object]:
    steps: list[dict[str, object]] = [{"name": "Set up job", "conclusion": "success"}]
    if step is not None:
        steps.append({"name": step, "conclusion": "failure"})
    return {
        "run_id": run_id,
        "id": _job_id(run_id, name),
        "name": name,
        "conclusion": "failure",
        "steps": steps,
    }


def _annotation(run_id: int, *messages: str) -> dict[str, object]:
    return {"job_id": _job_id(run_id, AGGREGATE_JOB), "annotations": list(messages)}


def _outage_jobs(run_id: int) -> list[dict[str, object]]:
    return [_job(run_id, START_JOB, SETUP_PYTHON)]


def _verdict_jobs(run_id: int, *annotations: str) -> list[dict[str, object]]:
    """The aggregate failed at its verdict step and finish reported it."""
    return [
        _job(run_id, AGGREGATE_JOB, h.VERDICT_STEP),
        _job(run_id, h.FINISH_JOB, h.FINISH_STEP),
        _annotation(run_id, *(annotations or (POSTED_BLOCK,))),
    ]


def _judge(
    runs: list[dict[str, object]], jobs: list[dict[str, object]]
) -> h.Assessment:
    return h.assess(h.sample_runs(runs, NOW), h.failed_jobs_by_run(jobs))


def _nov_9_streak() -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    """Successes and verdict failures, then setup-python fails every run."""
    runs: list[dict[str, object]] = []
    jobs: list[dict[str, object]] = []
    # Older: a healthy mix with verdict-only failures (newest-first ids).
    for n in range(10):
        run_id = 100 + n
        if n % 3 == 0:
            runs.append(_run(run_id, 600 + n * 10, "failure"))
            jobs += _verdict_jobs(run_id)
        else:
            runs.append(_run(run_id, 600 + n * 10, "success"))
    # Newer: the outage, with the superseded attempts cancelled.
    for n in range(14):
        run_id = 200 + n
        runs.append(_run(run_id, 10 + n * 30, "failure"))
        jobs += _outage_jobs(run_id)
    runs.append(_run(300, 15, "cancelled"))
    return runs, jobs


def test_a_nov_9_setup_python_streak_flags_and_names_the_step() -> None:
    runs, jobs = _nov_9_streak()
    result = _judge(runs, jobs)
    assert result.flagged
    assert result.infra_streak == 14
    assert result.top_failure == f"{START_JOB} / {SETUP_PYTHON}"
    assert f"most common: {START_JOB} / {SETUP_PYTHON} (14 run(s))" in (
        result.describe()
    )


def test_the_streak_alone_flags_before_the_ratio_can() -> None:
    """Five outage runs atop a long healthy history: the ratio is low."""
    runs = [_run(n, 100 + n, "success") for n in range(20, 40)]
    jobs: list[dict[str, object]] = []
    for n in range(h.INFRA_STREAK_THRESHOLD):
        runs.append(_run(n, n, "failure"))
        jobs += _outage_jobs(n)
    result = _judge(runs, jobs)
    assert result.ratio < h.INFRA_RATIO_THRESHOLD
    assert result.flagged


def test_an_intermittent_outage_flags_on_the_ratio() -> None:
    """Two of every three runs fail as infrastructure: the streak never
    reaches the threshold, but the share of the sample does."""
    runs: list[dict[str, object]] = []
    jobs: list[dict[str, object]] = []
    for n in range(h.SAMPLE_SIZE):
        if n % 3:
            runs.append(_run(n, n, "failure"))
            jobs += _outage_jobs(n)
        else:
            runs.append(_run(n, n, "success"))
    result = _judge(runs, jobs)
    assert result.infra_streak < h.INFRA_STREAK_THRESHOLD
    assert result.ratio >= h.INFRA_RATIO_THRESHOLD
    assert result.flagged


def test_a_sample_under_the_ratio_threshold_does_not_flag() -> None:
    """Every other run failing is half the sample, under the threshold."""
    runs: list[dict[str, object]] = []
    jobs: list[dict[str, object]] = []
    for n in range(h.SAMPLE_SIZE):
        if n % 2:
            runs.append(_run(n, n, "failure"))
            jobs += _outage_jobs(n)
        else:
            runs.append(_run(n, n, "success"))
    result = _judge(runs, jobs)
    assert result.ratio < h.INFRA_RATIO_THRESHOLD
    assert not result.flagged


def test_a_healthy_window_does_not_flag() -> None:
    """Successes, a few verdict failures and one PR closed before start."""
    runs: list[dict[str, object]] = []
    jobs: list[dict[str, object]] = []
    for n in range(h.SAMPLE_SIZE):
        if n in (3, 9, 15):
            runs.append(_run(n, n * 20, "failure"))
            jobs += _verdict_jobs(n)
        elif n == 6:
            runs.append(_run(n, n * 20, "failure"))
            jobs.append(_job(n, START_JOB, RESOLVE_STEP))
        else:
            runs.append(_run(n, n * 20, "success"))
    result = _judge(runs, jobs)
    assert not result.flagged
    assert result.infra_failures == 1
    assert result.verdict_failures == 3


def test_verdict_only_failures_are_not_an_outage() -> None:
    """BLOCK_MERGE_ON_VERDICT fails every run: the reviews still worked."""
    runs = [_run(n, n, "failure") for n in range(h.SAMPLE_SIZE)]
    jobs = [j for n in range(h.SAMPLE_SIZE) for j in _verdict_jobs(n)]
    result = _judge(runs, jobs)
    assert not result.flagged
    assert result.infra_failures == 0
    assert result.verdict_failures == h.SAMPLE_SIZE
    assert result.top_failure is None


def test_a_verdict_job_beside_an_infrastructure_failure_is_infrastructure() -> None:
    """The aggregate failing while finish dies at setup-python is the outage."""
    jobs = [
        h.FailedJob(AGGREGATE_JOB, (h.VERDICT_STEP,), verdict_posted=True),
        h.FailedJob(h.FINISH_JOB, (SETUP_PYTHON,)),
    ]
    assert not h.is_verdict_only(jobs)
    assert h.failure_label(jobs) == f"{h.FINISH_JOB} / {SETUP_PYTHON}"


def test_a_verdict_step_that_failed_to_post_is_an_outage() -> None:
    """The verdict step also posts: a posting failure exits 1 from the same
    step with no block annotation, and a block it could not post says so.
    Both are outages; a sample of them must flag (round 2, codex)."""
    runs = [_run(n, n, "failure") for n in range(h.SAMPLE_SIZE)]
    jobs: list[dict[str, object]] = []
    for n in range(h.SAMPLE_SIZE):
        if n % 2:
            jobs += _verdict_jobs(n, UNPOSTED_BLOCK)
        else:
            jobs += _verdict_jobs(n, "Process completed with exit code 1.")
    result = _judge(runs, jobs)
    assert result.flagged
    assert result.verdict_failures == 0
    assert result.top_failure == f"{AGGREGATE_JOB} / {h.VERDICT_STEP}"


def test_a_verdict_step_without_annotation_data_is_not_judged() -> None:
    """Annotations never fetched are not 'no annotation'."""
    jobs = _verdict_jobs(1)[:2]
    with pytest.raises(h.InputError, match="no annotation data"):
        h.failed_jobs_by_run(jobs)


def test_annotate_names_only_failed_verdict_step_jobs() -> None:
    jobs = _verdict_jobs(1) + _outage_jobs(2) + [{"run_id": 3}]
    jobs.append(_job(4, AGGREGATE_JOB, "Mint reviewer App token"))
    assert h.verdict_job_ids(jobs) == [_job_id(1, AGGREGATE_JOB)]


def test_the_annotation_strings_are_the_aggregate_s_own() -> None:
    """Tie the matched text to the code that writes it."""
    source = (Path(h.__file__).parent / "aggregate_reviews.py").read_text(
        encoding="utf-8"
    )
    assert 'BLOCK_MERGE_ON_VERDICT is on and the"' in source
    assert '" verdict does not pass (' in source
    assert '"see the verdict comment"' in source
    assert h.VERDICT_ANNOTATION.endswith("verdict does not pass")
    assert h.VERDICT_POSTED == "see the verdict comment"


def test_an_aggregate_failing_outside_its_verdict_step_is_infrastructure() -> None:
    jobs = [h.FailedJob(AGGREGATE_JOB, ("Mint reviewer App token",))]
    assert not h.is_verdict_only(jobs)


def test_a_finish_report_alone_is_not_a_verdict() -> None:
    """Finish failing with no failed aggregate is the lifecycle breaking."""
    assert not h.is_verdict_only([h.FailedJob(h.FINISH_JOB, (h.FINISH_STEP,))])


def test_a_run_that_started_no_job_is_infrastructure() -> None:
    runs = [_run(n, n, "startup_failure") for n in range(h.INFRA_STREAK_THRESHOLD)]
    # The jobs listing for each run is fetched and empty.
    failed = {n: [] for n in range(h.INFRA_STREAK_THRESHOLD)}
    result = h.assess(h.sample_runs(runs, NOW), failed)
    assert result.flagged
    assert result.top_failure == h.NO_FAILED_JOB


def test_cancelled_skipped_and_old_runs_are_not_sampled() -> None:
    runs = [
        _run(1, 1, "cancelled"),
        _run(2, 2, "skipped"),
        _run(3, 3, "success"),
        _run(4, h.LOOKBACK_HOURS * 60 + 1, "failure"),
        {**_run(5, 4, "failure"), "status": "in_progress"},
        # After the judging time: a replay at --now must not see the future.
        _run(6, -5, "failure"),
    ]
    assert [r.run_id for r in h.sample_runs(runs, NOW)] == [3]


def test_the_sample_is_capped_to_the_newest_runs() -> None:
    runs = [_run(n, n, "success") for n in range(h.SAMPLE_SIZE + 5)]
    sampled = h.sample_runs(runs, NOW)
    assert len(sampled) == h.SAMPLE_SIZE
    assert sampled[0].run_id == 0


def test_a_small_sample_is_not_judged_on_the_ratio() -> None:
    """Two failures of three runs is not an outage call yet."""
    runs = [_run(1, 1, "success"), _run(2, 2, "failure"), _run(3, 3, "failure")]
    jobs = _outage_jobs(2) + _outage_jobs(3)
    result = _judge(runs, jobs)
    assert result.ratio >= h.INFRA_RATIO_THRESHOLD
    assert not result.flagged


def test_a_failed_run_without_job_data_is_not_judged() -> None:
    runs = [_run(1, 1, "failure")]
    with pytest.raises(h.InputError, match="no job data"):
        h.assess(h.sample_runs(runs, NOW), {})


def test_no_runs_is_healthy_and_says_so() -> None:
    result = h.assess([], {})
    assert not result.flagged
    assert result.describe() == (
        f"no completed review runs in the last {h.LOOKBACK_HOURS}h -- nothing to judge"
    )


def _write_jsonl(path: Path, records: list[dict[str, object]]) -> str:
    path.write_text("".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")
    return str(path)


def test_cli_flags_the_streak_and_never_prints_run_ids(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    runs, jobs = _nov_9_streak()
    runs_file = _write_jsonl(tmp_path / "runs.jsonl", runs)
    jobs_file = _write_jsonl(tmp_path / "jobs.jsonl", jobs)
    now = NOW.strftime("%Y-%m-%dT%H:%M:%SZ")
    code = h.main(["judge", "--runs", runs_file, "--jobs", jobs_file, "--now", now])
    out = capsys.readouterr().out
    assert code == 1
    assert "14/20 recent review runs failed as infrastructure" in out
    for record in runs:
        assert str(record["id"]) not in out


def test_cli_select_prints_only_sampled_failed_runs(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    runs = [_run(1, 1, "success"), _run(2, 2, "failure"), _run(3, 3, "cancelled")]
    runs_file = _write_jsonl(tmp_path / "runs.jsonl", runs)
    now = NOW.strftime("%Y-%m-%dT%H:%M:%SZ")
    assert h.main(["select", "--runs", runs_file, "--now", now]) == 0
    assert capsys.readouterr().out.split() == ["2"]


def test_cli_since_prints_the_lookback_start(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert h.main(["since", "--now", "2026-11-10T09:00:00+09:00"]) == 0
    assert capsys.readouterr().out.strip() == "2026-11-09T00:00:00Z"


def test_cli_accepts_the_array_an_empty_listing_returns(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    runs_file = tmp_path / "runs.json"
    runs_file.write_text("[]\n", encoding="utf-8")
    jobs_file = tmp_path / "jobs.json"
    jobs_file.write_text("", encoding="utf-8")
    code = h.main(["judge", "--runs", str(runs_file), "--jobs", str(jobs_file)])
    assert code == 0
    assert "no completed review runs" in capsys.readouterr().out


@pytest.mark.parametrize(
    "runs_text",
    ["{not json", '"a string"\n', '{"id": "1"}\n', '{"id": 1}\n'],
    ids=["malformed", "not-an-object", "string-id", "no-created-at"],
)
def test_cli_unusable_input_is_not_judged(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], runs_text: str
) -> None:
    runs_file = tmp_path / "runs.jsonl"
    runs_file.write_text(runs_text, encoding="utf-8")
    jobs_file = _write_jsonl(tmp_path / "jobs.jsonl", [])
    code = h.main(["judge", "--runs", str(runs_file), "--jobs", jobs_file])
    assert code == 2
    assert "not judged" in capsys.readouterr().out


def test_cli_startup_failures_with_fetched_empty_jobs_flag(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The workflow writes a bare run_id record per fetched run; a run with
    no jobs is then an outage, not a run whose jobs were never read."""
    ids = range(h.INFRA_STREAK_THRESHOLD)
    runs_file = _write_jsonl(
        tmp_path / "runs.jsonl", [_run(n, n, "startup_failure") for n in ids]
    )
    jobs_file = _write_jsonl(tmp_path / "jobs.jsonl", [{"run_id": n} for n in ids])
    now = NOW.strftime("%Y-%m-%dT%H:%M:%SZ")
    code = h.main(["judge", "--runs", runs_file, "--jobs", jobs_file, "--now", now])
    assert code == 1
    assert f"most common: {h.NO_FAILED_JOB}" in capsys.readouterr().out


def test_cli_missing_job_data_is_not_judged(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    runs_file = _write_jsonl(tmp_path / "runs.jsonl", [_run(1, 1, "failure")])
    jobs_file = _write_jsonl(tmp_path / "jobs.jsonl", [])
    now = NOW.strftime("%Y-%m-%dT%H:%M:%SZ")
    code = h.main(["judge", "--runs", runs_file, "--jobs", jobs_file, "--now", now])
    assert code == 2
    assert "not judged" in capsys.readouterr().out
