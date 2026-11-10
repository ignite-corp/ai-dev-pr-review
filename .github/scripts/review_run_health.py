#!/usr/bin/env python3
"""Judge whether the runner's review workflow is failing as infrastructure.

consumer-health.yml checked the runner's prompts and its review pin, never
whether its reviews ran at all. On 2026-11-09 ``ubuntu-latest`` moved to an
image that the runner's pinned Python had no build for, and every review
failed in "Start target check" at ``actions/setup-python`` -- 62 runs in a row
-- for most of a day, until a person happened to open a failed run (AT-3353).
The daily check reported green the whole time.

What counts
    Only completed runs from the last ``LOOKBACK_HOURS``, newest first, capped
    at ``SAMPLE_SIZE``. A cancelled, skipped or stale run did not try to
    review anything (the runner cancels a superseded attempt for the same PR
    by design), so it is excluded rather than counted as either outcome.

Verdict versus outage
    ``BLOCK_MERGE_ON_VERDICT`` makes a run fail when the review completed and
    its verdict did not pass. That is the review working, not an outage. A
    failed run is a verdict failure when the aggregate job failed at its
    verdict step, that step left the annotation aggregate_reviews.py writes
    for a blocked verdict it posted, and the only other failure, if any, is
    the finish job at the step that completes the target check -- which exits
    non-zero on a failure verdict by design. The annotation is what tells a
    blocked verdict from the same step failing to post one: both exit 1 from
    one step, and only the deliberate block writes it, naming whether the
    verdict reached the PR. A verdict that was not posted is an outage too.
    Every other failed run is an infrastructure failure, including one with
    no failed job at all (``startup_failure``). Matching is by job and step
    name and annotation text, so a rename or rewording turns verdict failures
    into infrastructure failures: it fails loud, never quiet.

    No completed run in the window is not a flag: the runner idles (on
    2026-11-08 the 24h window held 2 runs, on 2026-11-02 3), so an empty
    window would flag every quiet weekend. It is reported as nothing to
    judge, never as healthy.

Thresholds
    Flag when the newest ``INFRA_STREAK_THRESHOLD`` evaluated runs all failed
    as infrastructure, or when at least ``MIN_RUNS_FOR_RATIO`` runs were
    evaluated and ``INFRA_RATIO_THRESHOLD`` or more of them did. The streak
    catches a hard outage as soon as a handful of reviews hit it; the ratio
    catches an intermittent one that successes keep interrupting. See the
    constants for the measured values behind each number.

Public-log safety: the input carries run conclusions and the runner's job
and step names, which are fixed strings in the runner's and this repo's
workflow files. Run titles name the target repository, so they are never
read; run ids are read only to join jobs to runs and are never printed.
A future job or step name built from a ``${{ }}`` expression over the
dispatch inputs would print a target here, and so would a ``run:`` step with
no ``name:``, which GitHub labels with its command's first line; keep every
runner and aggregate job and step name explicit and static. Annotation text
is matched, never printed.

Consumer check 1 (AT-3393)
    consumer-health.yml's per-consumer check 1 flags 2 or more failures in
    a consumer's last 3 review runs. It counted a blocked verdict as a
    failure, so a PR whose rounds drew majors flagged its repository as if
    reviews were down. It now asks this module the same verdict-versus-
    outage question over its own listing: ``--as-listed`` takes the runs as
    given, with no lookback window or sample cap, and ``count`` prints the
    infrastructure and verdict-only failure counts; the threshold stays in
    the workflow. A consumer calls the base orchestrator through ``uses:``,
    so its aggregate job carries the same caller prefix as the runner's
    (``review / aggregate / Aggregate & Verdict``, measured on every tracked
    consumer's failed runs 2026-11-10) and has no finish job.

Usage:
    review_run_health.py since [--now ISO]
    review_run_health.py select --runs RUNS [--now ISO | --as-listed]
    review_run_health.py annotate --jobs JOBS
    review_run_health.py judge --runs RUNS --jobs JOBS [--now ISO]
    review_run_health.py count --runs RUNS --jobs JOBS

RUNS is JSON lines of ``{"id", "created_at", "status", "conclusion"}`` (a
JSON array is accepted too); JOBS is JSON lines of
``{"run_id", "id", "name", "conclusion", "steps": [{"name", "conclusion"}]}``,
plus one bare ``{"run_id"}`` per run whose jobs were fetched (a run that
started no job lists none, and without that record it would read as never
fetched), plus one ``{"job_id", "annotations": [message, ...]}`` per job
``annotate`` named.

``since`` prints the start of the lookback window, for the caller's
``created>=`` filter, so the window has one home. ``select`` prints the ids
of the failed runs inside the sample (with ``--as-listed``, inside the
whole listing), one per line, so the caller fetches jobs for those runs
only. ``annotate`` prints the ids of the failed jobs whose annotations
decide verdict versus outage. ``judge`` prints a one-line verdict; exit 0 = healthy, 1 = flag, 2 = input unusable (the check
was not performed -- never read as healthy). ``count`` judges every completed
run in the listing and prints ``<infrastructure> <verdict-only>`` failure
counts; exit 0, or 2 as ``judge`` does.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TypeGuard, cast

# A day, so a daily cron sees every run since the previous one.
LOOKBACK_HOURS = 24
# The newest runs judged. Small enough that a recovered runner reads healthy
# within a few hours of the fix rather than carrying the outage for a whole
# day; large enough that the ratio means something.
SAMPLE_SIZE = 20
# Measured by replaying this module hourly over the runner's last 1000 review
# runs (the listing API's cap: 2026-10-29..2026-11-10), with the AT-3353
# outage (62 consecutive setup-python failures from 2026-11-09T09:24Z) set
# apart. Outside it the longest newest-first streak of infrastructure
# failures was 4, all at "Resolve PR head", the start step that refuses a PR
# no longer open; the outage first flagged at 10:00Z with a streak of 10, and
# the 00:00Z cron the next day would have seen 20 of 20.
INFRA_STREAK_THRESHOLD = 5
# Share of the sample that flags. Outside the outage the worst full sample
# (20 runs) was 45% and the worst 10-run sample 50% (2026-11-02T01:00Z, with
# the aggregate's unposted verdicts counted as infrastructure), so 0.5 flagged
# once in the replay and 0.6 never did. A smaller sample is not judged on the
# ratio: at 2026-11-09T00:00Z only 6 runs were in the window and 4 of them
# (67%) had failed at that same start step, hours before the outage began.
INFRA_RATIO_THRESHOLD = 0.6
MIN_RUNS_FOR_RATIO = 10

FAILED_CONCLUSIONS = frozenset({"failure", "timed_out", "startup_failure"})
EXCLUDED_CONCLUSIONS = frozenset({"cancelled", "skipped", "stale"})
JOB_FAILED_CONCLUSIONS = frozenset({"failure", "timed_out"})

# The aggregate job runs inside the reusable orchestrator, so the runner
# reports it under a caller prefix ("review / aggregate / ..."); match the
# suffix this repo's base-ai-review-aggregate.yml names.
VERDICT_JOB_SUFFIX = "Aggregate & Verdict"
VERDICT_STEP = "Aggregate and post verdict"
# aggregate_reviews.py _exit_if_verdict_blocks_merge writes this annotation
# on a deliberate block, and only there; the second phrase is its wording
# when the verdict reached the PR. Both are in v1.19.2, the runner's pin.
VERDICT_ANNOTATION = "BLOCK_MERGE_ON_VERDICT is on and the verdict does not pass"
VERDICT_POSTED = "see the verdict comment"
# The runner's finish job exits 1 after recording a failure verdict on the
# target check (check_lifecycle.py finish).
FINISH_JOB = "Complete this attempt's target check"
FINISH_STEP = "Complete only the check created by start"

NO_FAILED_JOB = "no failed job (run did not start)"

Record = Mapping[str, object]


class InputError(ValueError):
    """The input cannot be judged; the check was not performed."""


@dataclass(frozen=True)
class Run:
    run_id: int
    created_at: datetime
    failed: bool


@dataclass(frozen=True)
class FailedJob:
    name: str
    steps: tuple[str, ...]
    # True only when the job's annotations show a blocked verdict was posted.
    verdict_posted: bool = False


@dataclass(frozen=True)
class Assessment:
    evaluated: int
    infra_failures: int
    verdict_failures: int
    infra_streak: int
    top_failure: str | None
    top_failure_count: int

    @property
    def ratio(self) -> float:
        return self.infra_failures / self.evaluated if self.evaluated else 0.0

    @property
    def flagged(self) -> bool:
        if self.infra_streak >= INFRA_STREAK_THRESHOLD:
            return True
        return (
            self.evaluated >= MIN_RUNS_FOR_RATIO and self.ratio >= INFRA_RATIO_THRESHOLD
        )

    def describe(self) -> str:
        if self.evaluated == 0:
            return (
                f"no completed review runs in the last {LOOKBACK_HOURS}h"
                " -- nothing to judge"
            )
        text = (
            f"{self.infra_failures}/{self.evaluated} recent review runs failed as "
            f"infrastructure ({self.ratio:.0%}), newest streak {self.infra_streak}, "
            f"{self.verdict_failures} verdict-only failure(s) not counted"
        )
        if self.top_failure is not None:
            text += (
                f"; most common: {self.top_failure} ({self.top_failure_count} run(s))"
            )
        return text


def _is_record(item: object) -> TypeGuard[Record]:
    return isinstance(item, dict)


def load_records(text: str) -> list[Record]:
    """Read a JSON array or JSON lines into a list of objects."""
    stripped = text.strip()
    if not stripped:
        return []
    items: list[object]
    try:
        if stripped.startswith("["):
            loaded: object = json.loads(stripped)
            if not isinstance(loaded, list):
                raise InputError("expected a JSON array")
            items = cast("list[object]", loaded)
        else:
            items = [json.loads(line) for line in stripped.splitlines() if line]
    except json.JSONDecodeError as exc:
        raise InputError(f"malformed JSON: {exc.msg}") from exc
    records: list[Record] = []
    for item in items:
        if not _is_record(item):
            raise InputError("expected JSON objects")
        records.append(item)
    return records


def _parse_time(value: object) -> datetime:
    if not isinstance(value, str):
        raise InputError("run has no created_at")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise InputError("run has an unreadable created_at") from exc
    if parsed.tzinfo is None:
        raise InputError("run created_at has no timezone")
    return parsed


def _completed_run(record: Record) -> Run | None:
    """The run as judged, or None when it did not try to review anything."""
    run_id = record.get("id")
    if not isinstance(run_id, int) or isinstance(run_id, bool):
        raise InputError("run has no integer id")
    created = _parse_time(record.get("created_at"))
    if record.get("status") != "completed":
        return None
    conclusion = record.get("conclusion")
    if not isinstance(conclusion, str) or conclusion in EXCLUDED_CONCLUSIONS:
        return None
    return Run(run_id, created, conclusion in FAILED_CONCLUSIONS)


def listed_runs(records: Iterable[Record]) -> list[Run]:
    """Return every judged run in the listing, newest first.

    No window and no cap: the caller already chose which runs to look at.
    """
    runs = [run for run in map(_completed_run, records) if run is not None]
    runs.sort(key=lambda run: run.created_at, reverse=True)
    return runs


def sample_runs(records: Iterable[Record], now: datetime) -> list[Run]:
    """Return the judged runs, newest first."""
    cutoff = now - timedelta(hours=LOOKBACK_HOURS)
    runs = [run for run in listed_runs(records) if cutoff <= run.created_at <= now]
    return runs[:SAMPLE_SIZE]


def _str_field(record: Record, key: str) -> str:
    value = record.get(key)
    if not isinstance(value, str):
        raise InputError(f"job has no {key}")
    return value


def _is_verdict_step_failure(name: str, steps: Sequence[str]) -> bool:
    return name.endswith(VERDICT_JOB_SUFFIX) and tuple(steps) == (VERDICT_STEP,)


def _int_field(record: Record, key: str) -> int:
    value = record.get(key)
    if not isinstance(value, int) or isinstance(value, bool):
        raise InputError(f"record has no integer {key}")
    return value


def _failed_steps(record: Record) -> tuple[str, ...]:
    raw_steps = record.get("steps")
    steps: list[str] = []
    if isinstance(raw_steps, list):
        for step in cast("list[object]", raw_steps):
            if not _is_record(step):
                raise InputError("job step is not an object")
            if step.get("conclusion") in JOB_FAILED_CONCLUSIONS:
                steps.append(_str_field(step, "name"))
    return tuple(steps)


def _annotations(records: Iterable[Record]) -> dict[int, list[str]]:
    found: dict[int, list[str]] = {}
    for record in records:
        if "job_id" not in record:
            continue
        raw = record.get("annotations")
        if not isinstance(raw, list):
            raise InputError("annotation record has no list")
        messages = [m for m in cast("list[object]", raw) if isinstance(m, str)]
        found[_int_field(record, "job_id")] = messages
    return found


def verdict_job_ids(records: Iterable[Record]) -> list[int]:
    """The failed jobs whose annotations decide verdict versus outage."""
    ids: list[int] = []
    for record in records:
        if "job_id" in record or "name" not in record:
            continue
        if record.get("conclusion") not in JOB_FAILED_CONCLUSIONS:
            continue
        if _is_verdict_step_failure(_str_field(record, "name"), _failed_steps(record)):
            ids.append(_int_field(record, "id"))
    return ids


def failed_jobs_by_run(records: Iterable[Record]) -> dict[int, list[FailedJob]]:
    """Group the failed jobs, and their failed steps, by run id."""
    items = list(records)
    annotations = _annotations(items)
    grouped: dict[int, list[FailedJob]] = {}
    for record in items:
        if "job_id" in record:
            continue
        run_id = _int_field(record, "run_id")
        grouped.setdefault(run_id, [])
        if (
            "name" not in record
            or record.get("conclusion") not in JOB_FAILED_CONCLUSIONS
        ):
            continue
        name = _str_field(record, "name")
        steps = _failed_steps(record)
        posted = False
        if _is_verdict_step_failure(name, steps):
            job_id = _int_field(record, "id")
            if job_id not in annotations:
                raise InputError("a verdict job has no annotation data")
            posted = any(
                VERDICT_ANNOTATION in m and VERDICT_POSTED in m
                for m in annotations[job_id]
            )
        grouped[run_id].append(FailedJob(name, steps, posted))
    return grouped


def _is_verdict_job(job: FailedJob) -> bool:
    return job.verdict_posted and _is_verdict_step_failure(job.name, job.steps)


def _is_finish_report(job: FailedJob) -> bool:
    return job.name == FINISH_JOB and job.steps == (FINISH_STEP,)


def is_verdict_only(jobs: Sequence[FailedJob]) -> bool:
    """True when the review completed and only its verdict failed the run."""
    if not any(_is_verdict_job(job) for job in jobs):
        return False
    return all(_is_verdict_job(job) or _is_finish_report(job) for job in jobs)


def failure_label(jobs: Sequence[FailedJob]) -> str:
    """Name the first failed job and step, the place an operator looks first.

    A verdict job or finish report failing beside an infrastructure failure
    is its consequence, not its cause, so it is named only when nothing else
    failed.
    """
    if not jobs:
        return NO_FAILED_JOB
    causes = [j for j in jobs if not (_is_verdict_job(j) or _is_finish_report(j))]
    job = (causes or list(jobs))[0]
    if job.steps:
        return f"{job.name} / {job.steps[0]}"
    return job.name


def assess(
    runs: Sequence[Run], failed_jobs: Mapping[int, Sequence[FailedJob]]
) -> Assessment:
    """Classify each sampled run and measure the infrastructure failures.

    Every failed run must have an entry in ``failed_jobs`` (possibly empty,
    for a run that never started a job); a missing entry means its jobs were
    never fetched, and guessing either way would be a verdict on nothing.
    """
    infra: list[bool] = []
    labels: Counter[str] = Counter()
    verdict_failures = 0
    for run in runs:
        if not run.failed:
            infra.append(False)
            continue
        if run.run_id not in failed_jobs:
            raise InputError("a failed run has no job data")
        jobs = failed_jobs[run.run_id]
        if is_verdict_only(jobs):
            verdict_failures += 1
            infra.append(False)
            continue
        infra.append(True)
        labels[failure_label(jobs)] += 1
    streak = next((i for i, failed in enumerate(infra) if not failed), len(infra))
    top = labels.most_common(1)
    return Assessment(
        evaluated=len(runs),
        infra_failures=sum(infra),
        verdict_failures=verdict_failures,
        infra_streak=streak,
        top_failure=top[0][0] if top else None,
        top_failure_count=top[0][1] if top else 0,
    )


def _read(path: str) -> str:
    if path == "-":
        return sys.stdin.read()
    return Path(path).read_text(encoding="utf-8")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Judge whether the runner's review runs are failing."
    )
    parser.add_argument(
        "mode", choices=("since", "select", "annotate", "judge", "count")
    )
    parser.add_argument("--runs")
    parser.add_argument("--jobs")
    when = parser.add_mutually_exclusive_group()
    when.add_argument("--now", help="ISO 8601 time to judge at (default: now)")
    when.add_argument(
        "--as-listed",
        action="store_true",
        help="select from every run listed, with no window or cap",
    )
    args = parser.parse_args(argv)
    # A flag a mode would ignore is a caller's mistake; refuse it loudly.
    if args.as_listed and args.mode != "select":
        parser.error("--as-listed applies to select only (count is always as listed)")
    if args.now is not None and args.mode in ("annotate", "count"):
        parser.error(f"--now does not apply to {args.mode}")
    mode: str = args.mode
    runs_path: str | None = args.runs
    jobs_path: str | None = args.jobs
    now_text: str | None = args.now
    as_listed: bool = args.as_listed
    try:
        now = _parse_time(now_text) if now_text else datetime.now(UTC)
        if mode == "since":
            cutoff = now.astimezone(UTC) - timedelta(hours=LOOKBACK_HOURS)
            print(cutoff.strftime("%Y-%m-%dT%H:%M:%SZ"))
            return 0
        if mode == "annotate":
            if jobs_path is None:
                raise InputError("annotate needs --jobs")
            for job_id in verdict_job_ids(load_records(_read(jobs_path))):
                print(job_id)
            return 0
        if runs_path is None:
            raise InputError(f"{mode} needs --runs")
        records = load_records(_read(runs_path))
        runs = (
            listed_runs(records)
            if as_listed or mode == "count"
            else sample_runs(records, now)
        )
        if mode == "select":
            for run in runs:
                if run.failed:
                    print(run.run_id)
            return 0
        if jobs_path is None:
            raise InputError(f"{mode} needs --jobs")
        result = assess(runs, failed_jobs_by_run(load_records(_read(jobs_path))))
    except (InputError, OSError) as exc:
        print(f"review run health not judged: {exc}")
        return 2
    if mode == "count":
        print(f"{result.infra_failures} {result.verdict_failures}")
        return 0
    print(result.describe())
    return 1 if result.flagged else 0


if __name__ == "__main__":
    sys.exit(main())
