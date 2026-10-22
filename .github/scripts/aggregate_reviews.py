#!/usr/bin/env python3
"""Aggregate multi-LLM review results and post rule-based verdict.

Reads review-claude.json, review-codex.json, review-gemini.json,
applies severity-based rules, and posts a consolidated PR review.

Inline comments are posted by each reviewer job -- this script
handles only the summary verdict.

Reviewer payload contract (AT-1799):
    Every reviewer payload carries a first-class ``status`` field with one
    of three values:
      - "ok"          -- review completed normally; counts toward verdict
      - "early_exit"  -- reviewer stopped early on a fundamental flaw;
                         counts and drives the sequential early-exit bypass
      - "failed"      -- reviewer infrastructure failed; NEVER counted as
                         a performed review
    Parsing is fail-closed: a missing ``status`` field, or a
    present-but-unknown value, is normalized to "failed" (with a
    ::warning) so a payload that violates the contract can never
    masquerade as a healthy review.

    ``status`` is the single source of truth and is never inferred from
    other keys such as ``error`` or ``early_exit``. The inference shim for
    emitters pinned to pre-contract releases was removed once every
    emitter (base + wrapper) shipped ``status`` (AT-1954; contract
    introduced 2026-08-08).
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import sys
from collections import Counter
from collections.abc import Mapping
from pathlib import Path
from typing import Any, NamedTuple, TypeGuard

# The payload-shape pair is written once, in github_pr_support, so the two
# reviewer shims can gate their direct-write path on the SAME test this
# module applies -- see usable_verdict there. Imported under the names this
# module already used, because the shape is still this module's contract.
#
# It sits in github_pr_support and NOT in local_reviewer_support: this
# script is the Actions-path aggregate, run straight after setup-python
# with no `pip install`, so importing the local driver's support module
# put it behind that module's PyYAML dependency and broke every consumer
# (AT-2510).
from github_pr_support import (
    COMBINED_SIZE_AXIS,
    COMBINED_SIZE_VAR,
    REVIEW_MARKER,
    REVIEWER_NAMES,
    SEVERITY_ICONS,
    GH_TIMEOUT_SEC,
    describe_size_breach,
    describe_size_overage,
    describe_size_warning,
    display_path,
    fetch_paginated_nodes,
    fetch_round_count,
    trusted_post_author,
    fold_envelope,
    fold_markers_for,
    int_env,
    inline_post_envelope,
    inline_run_identity,
    is_valid_review as _is_valid_review,
    normalize_severity as _normalize_severity,
)

logger = logging.getLogger(__name__)

REVIEWERS: dict[str, str] = {name: f"review-{name}.json" for name in REVIEWER_NAMES}

# Max chars for issue description in verdict reason string.
# Keeps the PR review title concise while showing enough context.
DESC_TRUNCATE_LEN = 80
ERROR_TRUNCATE_LEN = 60
# Why the claude path wrote neither a verdict nor an execution log, as
# classified by its error net in base-ai-review-single.yml (AT-2276). The
# payload keeps ``error: action_invocation_failed`` for every regex keyed on
# it and carries the class in ``error_cause``; only a value in this closed
# set is rendered, so a payload cannot put arbitrary text after the error.
ERROR_CAUSE_KIND = "action_invocation_failed"
ERROR_CAUSE_TEXT: dict[str, str] = {
    "rejected": "rejected -- actor not in allowed_bots",
    "timeout": "timeout -- step time limit reached",
    "action_error": "action_error -- action failed before producing a log",
}
MIN_REVIEWERS_FOR_VERDICT = 2
_DISMISS_MESSAGE = "Superseded by new review"
_REVIEW_MODE_SEQUENTIAL = "sequential"
_REVIEW_MODE_PARALLEL = "parallel"


def _is_comment_only() -> bool:
    return os.environ.get("ALLOW_AUTO_APPROVE", "false").lower() != "true"


_PR_AUTHOR = os.environ.get("PR_AUTHOR", "")


def _resolve_thresholds(
    is_dependabot: bool,
) -> tuple[int, float]:
    """Return (CRITICAL_THRESHOLD, MAJOR_CONSENSUS_OVERLAP) based on PR author.

    Dependabot PRs use higher thresholds to tolerate single LLM false-positives;
    human-authored PRs retain strict defaults (1 critical blocks immediately).
    """
    if is_dependabot:
        return (
            int_env("DEPENDABOT_CRITICAL_THRESHOLD", 2),
            float(os.environ.get("DEPENDABOT_MAJOR_CONSENSUS_OVERLAP", "0.5")),
        )
    return (
        int_env("CRITICAL_THRESHOLD", 1),
        float(os.environ.get("MAJOR_CONSENSUS_OVERLAP", "0.3")),
    )


CRITICAL_THRESHOLD, MAJOR_CONSENSUS_OVERLAP = _resolve_thresholds(
    _PR_AUTHOR == "dependabot[bot]"
)

MAJOR_CONSENSUS_MIN = int_env("MAJOR_CONSENSUS_MIN", 2)
# The identity the aggregate's own verdict comments are posted under.
# NOTHING IN THIS MODULE READS IT TODAY: the stale fold used to, and since
# AT-2599 it keys on the review marker instead, because tidying needs no
# identity. It is kept, rather than deleted, for the reads that do need one
# -- a read that must decide whether to trust a verdict, not merely fold it,
# cannot use the marker, which anyone can quote. The local driver resolves
# this name from the default the aggregate step declares, so the two stay in
# step. Delete it if no such read lands.
BOT_LOGIN = os.environ.get("BOT_LOGIN", "github-actions[bot]")
# Fold evidence trusts only the actual inline/fold posting author (AT-2553),
# which can differ from the configurable aggregate-verdict identity.
INLINE_BOT_LOGIN = os.environ.get("INLINE_BOT_LOGIN", "github-actions[bot]")

# Map job conclusion -> human-readable missing-verdict reason.
# "skipped" means the job was conditionally excluded (e.g. wrong REVIEW_MODE).
_CONCLUSION_REASON: dict[str, str] = {
    "success": "early-exit or no-output",
    "failure": "failed (see logs)",
    "cancelled": "cancelled",
    "skipped": "skipped",
}
_CONCLUSION_REASON_UNKNOWN = "no verdict (unknown)"

STATUS_OK = "ok"
STATUS_EARLY_EXIT = "early_exit"
STATUS_FAILED = "failed"
_VALID_STATUSES = frozenset({STATUS_OK, STATUS_EARLY_EXIT, STATUS_FAILED})


def _normalize_status(name: str, review: dict[str, Any]) -> str:
    """Normalize and stamp ``status`` on the payload, returning it.

    A missing ``status`` field and a present-but-unknown value are both
    fail-closed to "failed" so a payload that violates the contract can
    never count as a performed review. Status is never inferred from
    ``error`` or ``early_exit``. Idempotent: downstream logic reads the
    stamped field as the single source of truth.
    """
    raw = review.get("status")
    if isinstance(raw, str) and raw in _VALID_STATUSES:
        return raw
    if "status" in review:
        print(
            f"::warning title={name.title()} unknown status::"
            f"status={raw!r} not in {sorted(_VALID_STATUSES)};"
            " treating as failed (fail-closed)",
            file=sys.stderr,
        )
    else:
        print(
            f"::warning title={name.title()} missing status::"
            f"status absent, expected one of {sorted(_VALID_STATUSES)};"
            " treating as failed (fail-closed)",
            file=sys.stderr,
        )
    review["status"] = STATUS_FAILED
    return STATUS_FAILED


def load_reviewer_conclusions() -> dict[str, str]:
    """Read per-reviewer job conclusions from env vars set by the workflow.

    Returns a dict mapping reviewer name -> conclusion string.
    Missing or empty env vars produce an empty string.
    """
    return {
        name: os.environ.get(f"REVIEWER_RESULT_{name.upper()}", "")
        for name in REVIEWER_NAMES
    }


def _missing_reason(conclusion: str) -> str:
    """Convert a job conclusion into a display reason for a missing verdict."""
    return _CONCLUSION_REASON.get(conclusion, _CONCLUSION_REASON_UNKNOWN)


def _get_available(
    reviews: Mapping[str, dict[str, Any] | None],
) -> dict[str, dict[str, Any]]:
    """Filter reviews to only those with valid responses.

    Reads the normalized ``status``: anything except "failed" counts.
    A partial-failure review that still reports status "ok" (an 'error'
    alongside 'issues') stays included so its issues contribute to verdict
    calculation; payloads whose status is "failed" -- including those that
    fail closed for a missing or unknown status -- are excluded entirely.
    """
    return {
        k: v
        for k, v in reviews.items()
        if v is not None
        and "summary" in v  # must have summary key (rejects empty {})
        and _normalize_status(k, v) != STATUS_FAILED
    }


def load_reviews() -> dict[str, dict[str, Any] | None]:
    reviews: dict[str, dict[str, Any] | None] = {}
    for name, filename in REVIEWERS.items():
        path = Path(filename)
        if path.exists():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                _normalize_severity(data)
                if _is_valid_review(data):
                    _normalize_status(name, data)
                    reviews[name] = data
                else:
                    print(f"Malformed review payload: {name}", file=sys.stderr)
                    reviews[name] = None
            except (json.JSONDecodeError, OSError):  # fmt: skip
                reviews[name] = None
        else:
            reviews[name] = None
    return reviews


def _has_early_exit(available: dict[str, dict[str, Any]]) -> bool:
    """Check if any reviewer's normalized status is early_exit."""
    return any(
        _normalize_status(k, v) == STATUS_EARLY_EXIT for k, v in available.items()
    )


def _has_full_reviewer_coverage(available: dict[str, dict[str, Any]]) -> bool:
    """True only when every configured reviewer produced a verdict (AT-2124).

    Deliberately independent of MIN_REVIEWERS_FOR_VERDICT. That threshold
    governs the verdict and the merge-gate exit code, where one reviewer's
    outage must not block every PR in the fleet. A green check is passive --
    it says only that nothing is stopping you. A formal APPROVED review is an
    affirmative, attributable claim that the code was reviewed, so it may be
    posted only when the full configured reviewer set actually ran; on a PR a
    configured reviewer never ran against, it is a false attestation.

    The failure directions are not symmetric: requiring the full set degrades
    to comment-only and a human approves, while reusing the availability
    count degrades toward silently approving more. A gate must fail toward
    passing less.

    The configured set is REVIEWERS (claude, codex, gemini) -- fixed for
    every run in both REVIEW_MODE=parallel and =sequential; neither mode
    selects a subset. "Produced a verdict" means the payload survived
    ``_get_available``, i.e. normalized status "ok" or "early_exit". An early
    exit is a judgement the reviewer reached after reading the diff, so that
    reviewer ran; status "failed", missing and malformed payloads did not.
    """
    return set(available) == set(REVIEWERS)


_PARTIAL_SUMMARY_PREFIX = "partial:"


def _is_partial(review: dict[str, Any] | None) -> bool:
    """Return True if the reviewer hit a partial failure.

    Partial means: missing payload, an ``error`` field, a normalized
    ``status`` of "failed", or a summary that starts with ``partial:``
    (case-insensitive). These signals are emitted
    by ``review_gemini.py`` / ``review_codex.py`` / ``review_claude.py``
    when the underlying API call raised or returned a truncated response.
    """
    if review is None:
        return True
    if review.get("error"):
        return True
    if review.get("status") == STATUS_FAILED:
        return True
    summary = review.get("summary", "")
    return isinstance(summary, str) and summary.strip().lower().startswith(
        _PARTIAL_SUMMARY_PREFIX
    )


def _partial_short_message(
    name: str,
    review: dict[str, Any] | None,
    conclusion: str,
) -> str:
    """Build a short human-readable description for a partial reviewer."""
    if review is None:
        if conclusion:
            return _missing_reason(conclusion)
        return "no payload"
    err = review.get("error")
    if isinstance(err, str) and err:
        return _error_with_cause(err, review)
    summary = review.get("summary", "")
    if isinstance(summary, str) and summary:
        return summary[:ERROR_TRUNCATE_LEN]
    return "partial output"


def _emit_partial_observability(
    reviews: Mapping[str, dict[str, Any] | None],
    conclusions: dict[str, str],
) -> list[str]:
    """Emit GHA warnings + Job Summary rows per partial-failed reviewer.

    Returns the list of partial reviewer names so the caller can use the
    count when deciding whether to downgrade the verdict.
    """
    partial_names: list[str] = []
    summary_rows: list[str] = []
    for name in REVIEWER_NAMES:
        review = reviews.get(name)
        if not _is_partial(review):
            continue
        partial_names.append(name)
        msg = _partial_short_message(name, review, conclusions.get(name, ""))
        print(
            f"::warning title={name.title()} partial-fail::{msg}",
            file=sys.stderr,
        )
        summary_rows.append(f"| {name.title()} | {msg} |")

    step_summary_path = os.environ.get("GITHUB_STEP_SUMMARY", "")
    if summary_rows and step_summary_path:
        try:
            with open(step_summary_path, "a", encoding="utf-8") as fh:
                fh.write("\n### Multi-LLM partial-fail reviewers\n\n")
                fh.write("| Reviewer | Reason |\n")
                fh.write("| --- | --- |\n")
                fh.write("\n".join(summary_rows))
                fh.write("\n")
        except OSError as e:
            print(
                f"::warning::Failed to write GITHUB_STEP_SUMMARY: {e}",
                file=sys.stderr,
            )
    return partial_names


def _all_reviewer_jobs_succeeded(total: int) -> bool:
    """True when every reviewer job reported `success`.

    Says nothing about why no payload arrived. The reviewer step in
    base-ai-review-single.yml is continue-on-error, so a reviewer whose CLI
    or action dies on a missing or bad credential reports `success` having
    uploaded nothing. Callers must not treat it as a benign classification;
    it only selects the indeterminate reason, and the round still fails CI
    (AT-2569).
    """
    conclusions = load_reviewer_conclusions()
    return len(conclusions) == total and all(
        v == "success" for v in conclusions.values()
    )


def _no_valid_payloads(
    reviews: Mapping[str, dict[str, Any] | None],
) -> bool:
    """True when no reviewer has a valid payload.

    ``reviews`` comes from load_reviews, which maps a missing file, an
    unparseable one and a schema-invalid one alike to None, so this cannot
    tell "nothing was written" from "something unusable was written".
    Error-bearing fallback payloads (e.g. the codex CLI-failure verdict) are
    valid payloads: their presence marks an infrastructure failure the
    reviewer was able to report, which is strictly more than this predicate
    can tell its callers.
    """
    return all(v is None for v in reviews.values())


def _check_insufficient(
    available: dict[str, dict[str, Any]],
    reviews: Mapping[str, dict[str, Any] | None],
    total: int,
) -> tuple[str, str] | None:
    if len(available) < MIN_REVIEWERS_FOR_VERDICT:
        # Only bypass minimum-reviewer check in sequential mode where
        # early_exit intentionally skips subsequent reviewers.
        review_mode = os.environ.get("REVIEW_MODE", _REVIEW_MODE_PARALLEL)
        if review_mode == _REVIEW_MODE_SEQUENTIAL and _has_early_exit(available):
            return None
        if len(available) == 0:
            if _no_valid_payloads(reviews) and _all_reviewer_jobs_succeeded(total):
                return (
                    "comment",
                    f"0/{total} LLM responses -- no reviewer output,"
                    " cause indeterminate",
                )
            return (
                "comment",
                f"0/{total} LLM responses -- all failed, manual review required",
            )
        n = len(available)
        return "request_changes", f"{n}/{total} LLM responses -- manual review required"
    return None


def _check_criticals(all_issues: list[dict[str, Any]]) -> tuple[str, str] | None:
    criticals = [i for i in all_issues if i.get("severity") == "critical"]
    if len(criticals) >= CRITICAL_THRESHOLD:
        reviewers = sorted({str(r) for i in criticals if (r := i.get("reviewer"))})
        desc = criticals[0].get("description", "")[:DESC_TRUNCATE_LEN]
        return (
            "request_changes",
            f"{len(criticals)} critical issue(s) ({', '.join(reviewers)}): {desc}",
        )
    return None


def _normalize_desc(text: str) -> set[str]:
    """Extract lowercase words from a description for consensus matching."""
    return set(re.findall(r"\w+", text.lower()))


def _check_major_consensus(all_issues: list[dict[str, Any]]) -> tuple[str, str] | None:
    """Check if 2+ reviewers flagged similar major issues on the same file.

    Consensus requires same file + overlapping description words (>30%).
    Major issues without a file path fall through to a comment verdict.
    """
    majors = [i for i in all_issues if i.get("severity") == "major"]
    if not majors:
        return None
    # Group by file, then check if different reviewers raised similar issues
    file_issues: dict[str, list[dict[str, Any]]] = {}
    for issue in majors:
        file_path = issue.get("file")
        if not file_path:
            continue
        if not issue.get("reviewer"):
            continue
        file_issues.setdefault(file_path, []).append(issue)
    consensus_files: list[str] = []
    for fp, issues in file_issues.items():
        reviewers_with_consensus: set[str] = set()
        for i, a in enumerate(issues):
            for b in issues[i + 1 :]:
                if a["reviewer"] == b["reviewer"]:
                    continue
                words_a = _normalize_desc(a.get("description", ""))
                words_b = _normalize_desc(b.get("description", ""))
                if not words_a or not words_b:
                    continue
                overlap = len(words_a & words_b) / min(len(words_a), len(words_b))
                if overlap > MAJOR_CONSENSUS_OVERLAP:
                    reviewers_with_consensus.add(a["reviewer"])
                    reviewers_with_consensus.add(b["reviewer"])
        if len(reviewers_with_consensus) >= MAJOR_CONSENSUS_MIN:
            consensus_files.append(fp)
    if consensus_files:
        files_str = ", ".join(consensus_files)
        return "request_changes", f"Major issue consensus ({files_str})"
    return None


def _current_pr_head(pr_number: str, repo: str) -> str | None:
    """The PR's head SHA as GitHub reports it now, or None if unobtainable.

    None is "could not determine", never "not stale" -- the caller decides
    what to do with that, and the two must not be confused here.
    """
    try:
        result = subprocess.run(
            ["gh", "api", f"repos/{repo}/pulls/{pr_number}", "--jq", ".head.sha"],
            capture_output=True,
            text=True,
            timeout=GH_TIMEOUT_SEC,
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        print(f"::warning::Head lookup failed: {exc}", file=sys.stderr)
        return None
    if result.returncode != 0:
        print(
            f"::warning::Head lookup failed: {result.stderr.strip()}", file=sys.stderr
        )
        return None
    return result.stdout.strip() or None


def _head_is_stale() -> bool:
    """True when a newer commit has superseded the head this run reviewed.

    This is the discriminator, and it is not cancellation. The reviewer job
    conclusions cannot tell "the run was cancelled" from "one reviewer died":
    a cancel lands wherever the reviewers happen to be, so it produces every
    mixture from three cancelled to none, and a dead reviewer produces one of
    those same mixtures. Run 33615176056 shows how little the conclusions say
    -- all three reviewers `cancelled`, and two of them had already uploaded
    real reviews. No threshold over those values is correct in both
    directions, so none is taken.

    What actually matters is who owns the comment slot. If the head has moved,
    a newer run is reviewing the current commit and will post; this run's
    verdict is about a commit nobody is looking at any more, and posting it
    races the live run for the same slot (AT-2092). If the head has not moved,
    this run is still the one that owes the PR a verdict -- whether it was
    cancelled by hand, lost a reviewer, or hit a timeout is indistinguishable
    and, here, irrelevant. It posts the honest partial verdict naming who
    died, which is the AT-1837 behavior, in either review mode.

    Two cases are deliberately not stale:

    * no HEAD_SHA -- prepare never resolved one, so it either failed
      (AT-2087) or skipped for size (AT-1975). Both owe the PR a verdict and
      there is nothing to compare anyway.
    * the head could not be determined -- rate limit, network, a PR that no
      longer exists. Failing that way posts a verdict that may be redundant;
      failing the other way drops one silently. Redundancy is visible and the
      live run's later verdict supersedes it; a dropped verdict is the defect
      this whole file exists to prevent.
    """
    reviewed = os.environ.get("HEAD_SHA", "").strip()
    if not reviewed:
        return False
    pr_number = os.environ.get("PR_NUMBER", "").strip()
    repo = os.environ.get("GITHUB_REPOSITORY", "").strip()
    if not pr_number or not repo:
        return False
    current = _current_pr_head(pr_number, repo)
    if current is None:
        return False
    return current != reviewed


def _prepare_failure_result() -> str | None:
    """prepare's result when it did not succeed, else None.

    The orchestrator gates the reviewer jobs on prepare but cannot gate this
    job on it: its name is a required status context, and a job that never
    runs reports nothing, so the check sits Pending with no failure to explain
    it -- the AT-1975 defect reached through a different door (AT-2087).

    Unset is treated as success so that a caller pinned to a tag that predates
    this input keeps the old path.
    """
    result = os.environ.get("PREPARE_RESULT", "").strip().lower()
    if result in ("", "success"):
        return None
    return result


def format_prepare_failure_summary(result: str, run_url: str) -> str:
    """Verdict body for a run where prepare did not succeed.

    prepare dies before publishing anything -- no head_sha, no size numbers, no
    reviewer artifacts -- so the body cannot name a measurement the way the
    size-skip body does. It names the job to open instead: the reason is in
    that job's log, and pointing at it is the difference between a check that
    blocks and a check that blocks actionably.
    """
    headline = (
        f"**Result: [X] Review did not run -- the prepare job reported `{result}`**"
    )
    why_red = (
        "No reviewer ran, so this check reports a failure rather than a"
        " verdict. That is deliberate: a required check that is never"
        " reported stays Pending forever with nothing to act on."
    )
    where = (
        "The reason is in the `review / prepare / Prepare Review Context` job"
        " of this run"
    )
    where += f": {run_url}" if run_url else "."
    causes = [
        "Known causes:",
        "",
        "1. **`PR_SIZE_LIMIT` (or `PR_SIZE_LIMIT_ADDITIONS` /"
        " `PR_SIZE_LIMIT_DELETIONS`) is not an integer, or `PR_SIZE_WARN_PERCENT`"
        " is not an integer from 1 to 99.** Fix the repository variable"
        " (Settings -> Secrets and variables -> Actions -> Variables) or remove"
        " it to fall back to the default.",
        "2. **The PR head could not be resolved**, or the checked-out tree did"
        " not match the commit the diff is about. The log names the two SHAs.",
        "3. **Checkout, GitHub API, or runner failure.** Usually transient --"
        " re-run the workflow.",
        "4. **The diff is empty.** The PR changes nothing against its base,"
        " or a merged PR's changes could not be reconstructed from its merge"
        " commit. The log names the diff strategy that came up empty.",
    ]
    lines = [
        REVIEW_MARKER,
        "## [bot] Multi-LLM Review Summary",
        "",
        headline,
        "",
        "---",
        "",
        why_red,
        "",
        where,
        "",
        *causes,
        "",
        "This check turns green once prepare succeeds and the reviewers report.",
    ]
    return "\n".join(lines)


class SizeBreach(NamedTuple):
    """One size limit the PR exceeded, as prepare measured it (AT-3052).

    `axis` is `changed` on the combined gate, `added` or `deleted` on the
    per-axis one; `var` is the variable the axis was held to, which is
    PR_SIZE_LIMIT for an axis whose own variable is unset.
    """

    value: str
    axis: str
    var: str
    limit: str

    def describe(self) -> str:
        return describe_size_breach(self.value, self.axis, self.var, self.limit)


def _size_skip_details() -> list[SizeBreach] | None:
    """Each exceeded size limit when prepare skipped for size, else None.

    prepare gates the reviewer jobs on the size limits but cannot skip this
    job: its name is a required status context, and a job that never runs
    reports nothing, so the check sits Pending with no failure to explain it.
    The aggregate therefore runs on the skip path and renders it (AT-1975).

    SIZE_EXCEEDED / SIZE_AXES / SIZE_TOTAL / SIZE_LIMIT are space-separated
    and aligned by position. An empty SIZE_EXCEEDED is a prepare from before
    AT-3052, which only ever had the combined gate.
    """
    if os.environ.get("SIZE_SKIPPED", "false").strip().lower() != "true":
        return None
    exceeded = os.environ.get("SIZE_EXCEEDED", "").split() or [COMBINED_SIZE_VAR]
    axes = os.environ.get("SIZE_AXES", "").split()
    totals = os.environ.get("SIZE_TOTAL", "").split()
    limits = os.environ.get("SIZE_LIMIT", "").split()

    def at(values: list[str], index: int, fallback: str) -> str:
        return values[index] if index < len(values) else fallback

    return [
        SizeBreach(
            value=at(totals, index, "unknown"),
            axis=at(axes, index, COMBINED_SIZE_AXIS),
            var=var,
            limit=at(limits, index, "unknown"),
        )
        for index, var in enumerate(exceeded)
    ]


def _size_warnings() -> list[str]:
    """One warning line per size check prepare found near its limit (AT-2415).

    SIZE_WARNED / SIZE_WARN_AXES / SIZE_WARN_TOTAL / SIZE_WARN_LIMIT are
    aligned by position like the SIZE_EXCEEDED lists. An entry whose numbers
    are missing or unparseable is dropped: a warning is advice, and a
    percentage made up from "unknown" would be worse than none.

    Rendered on the reviewed path only. On the size-skip verdict the review
    is already blocked and its remedy already says how to split, so a
    sibling axis near its own limit is left to prepare's `::notice::`.
    """
    warned = os.environ.get("SIZE_WARNED", "").split()
    axes = os.environ.get("SIZE_WARN_AXES", "").split()
    totals = os.environ.get("SIZE_WARN_TOTAL", "").split()
    limits = os.environ.get("SIZE_WARN_LIMIT", "").split()
    lines: list[str] = []
    for var, axis, total, limit in zip(warned, axes, totals, limits):
        if total.isdigit() and limit.isdigit() and int(limit) > 0:
            lines.append(describe_size_warning(int(total), axis, var, int(limit)))
    return lines


def _size_overage_sentence(breaches: list[SizeBreach]) -> str:
    """` Over by ...; split into at least K PRs.`, or "" when not computable."""
    if not breaches or not all(
        b.value.isdigit() and b.limit.isdigit() for b in breaches
    ):
        return ""
    overage = describe_size_overage(
        [(int(b.value), b.axis, int(b.limit)) for b in breaches]
    )
    return f" {overage[0].upper()}{overage[1:]}."


def format_size_skip_reason(breaches: list[SizeBreach]) -> str:
    """`{value} {axis} lines exceeds {VAR} {limit}`, joined with `; `."""
    return "; ".join(breach.describe() for breach in breaches)


def _is_combined_breach(breaches: list[SizeBreach]) -> bool:
    return (
        len(breaches) == 1
        and breaches[0].axis == COMBINED_SIZE_AXIS
        and breaches[0].var == COMBINED_SIZE_VAR
    )


def format_size_skip_summary(breaches: list[SizeBreach]) -> str:
    """Verdict body for a review skipped because the PR is too large.

    Names the measurement and both remedies: the original defect was not that
    large PRs are blocked -- they already were -- but that nothing said so or
    said what to do about it. On the combined gate the headline keeps its
    pre-AT-3052 wording, which consumers' gates and the drill pin.
    """
    if _is_combined_breach(breaches):
        only = breaches[0]
        measurement = f"{only.value} changed lines exceeds the limit of {only.limit}"
    else:
        measurement = format_size_skip_reason(breaches)
    headline = f"**Result: [X] Review skipped -- PR too large** -- {measurement}"
    why_red = (
        "No reviewer ran, so this check reports a failure rather than a"
        " verdict. That is deliberate: a required check that is never"
        " reported stays Pending forever with nothing to act on."
    )
    each_part = " and ".join(f"{b.limit} {b.axis} lines" for b in breaches)
    remedy_split = (
        f"1. **Split this PR** so each part is at or under {each_part}."
        f"{_size_overage_sentence(breaches)}"
    )
    raise_vars = " and ".join(
        f"the `{b.var}` repository variable above {b.value}" for b in breaches
    )
    remedy_limit = (
        f"2. **Raise the limit for this repository** by setting {raise_vars}"
        " (Settings -> Secrets and variables -> Actions -> Variables),"
        " then re-run this workflow."
    )
    tradeoff = (
        "Raising the limit makes the reviewers read a larger diff, which costs"
        " more and reviews less closely -- splitting is preferred where it is"
        " possible."
    )
    lines = [
        REVIEW_MARKER,
        "## [bot] Multi-LLM Review Summary",
        "",
        headline,
        "",
        "---",
        "",
        why_red,
        "",
        "To proceed, either:",
        "",
        remedy_split,
        remedy_limit,
        "",
        tradeoff,
    ]
    return "\n".join(lines)


LENS_IGNORE_PATH = ".github/lens-ignore"
# Machine-readable marker for the policy-skip verdict, in the same family as
# REVIEW_MARKER. A consumer gate that must not merge on a skipped review keys
# on it: conclusion == success AND the latest LENS comment lacks this marker.
# It cannot key on the conclusion alone, because that path reports success by
# decision (see main), and a job conclusion cannot be neutral.
POLICY_SKIP_MARKER = "<!-- lens:skipped reason=policy-excluded-only files={n} -->"
# Roster reason on the policy-skip path. Spelled apart from the plain
# "skipped" of a REVIEW_MODE-excluded job: that one means the round ran
# without this reviewer, this one means no round ran at all, and a gate
# reading a zero roster on a green run has only the reason to tell them apart.
POLICY_SKIP_ROSTER_REASON = "skipped (policy)"


def _excluded_paths() -> list[str]:
    """Paths prepare removed from the diff by policy, one per input line."""
    raw = os.environ.get("EXCLUDED_PATHS", "")
    return [line.strip() for line in raw.splitlines() if line.strip()]


def _excluded_count() -> int:
    """EXCLUDED_COUNT as an int; the path list's length when it is unusable."""
    raw = os.environ.get("EXCLUDED_COUNT", "").strip()
    if raw.isdigit():
        return int(raw)
    return len(_excluded_paths())


def _policy_skip_details() -> list[str] | None:
    """The excluded paths when prepare skipped the review by policy, else None.

    Like the size skip, prepare gates the reviewer jobs but cannot skip this
    job, so the aggregate renders the skip. Unlike the size skip it is not a
    failure: nothing in the PR was review material, by the consumer's own
    rule, and the check reports success (AT-2206).
    """
    if os.environ.get("POLICY_SKIPPED", "false").strip().lower() != "true":
        return None
    return _excluded_paths()


def format_policy_skip_summary(paths: list[str]) -> str:
    """Verdict body for a review skipped because every file was policy-excluded.

    Names the rule file and the paths (never their content) so the skip is
    auditable on the PR, and carries POLICY_SKIP_MARKER for consumer gates.
    """
    count = len(paths)
    headline = "**Result: [OK] Review skipped -- only policy-excluded files changed**"
    why = (
        f"No reviewer ran: every changed file in this PR matched"
        f" `{LENS_IGNORE_PATH}`, so its hunks were removed from the diff before"
        " review and nothing was left to review."
    )
    # prepare already sanitizes; rendered through display_path again so a
    # path list from any other producer cannot close the code span either.
    listed = [f"- `{display_path(path)}`" for path in paths] or [
        "- (paths not reported)"
    ]
    outcome = (
        "This check reports success by policy: the excluded content is not"
        " review material, and this is not a review of it. A gate that must"
        " not merge on a skipped review can key on the"
        " `<!-- lens:skipped ... -->` marker in this comment."
    )
    lines = [
        REVIEW_MARKER,
        POLICY_SKIP_MARKER.format(n=count),
        "## [bot] Multi-LLM Review Summary",
        "",
        headline,
        "",
        "---",
        "",
        why,
        "",
        f"{count} file(s) excluded by policy ({LENS_IGNORE_PATH}):",
        "",
        *listed,
        "",
        outcome,
    ]
    return "\n".join(lines)


def apply_verdict_rules(
    reviews: Mapping[str, dict[str, Any] | None],
) -> tuple[str, str, dict[str, dict[str, Any]]]:
    """Apply severity-based verdict rules. Returns (verdict, reason, available)."""
    available = _get_available(reviews)
    total = len(REVIEWERS)

    result = _check_insufficient(available, reviews, total)
    if result:
        return (*result, available)

    all_issues: list[dict[str, Any]] = []
    for name, review in available.items():
        for issue in review.get("issues", []):
            all_issues.append({**issue, "reviewer": name})

    if not all_issues:
        n = len(available)
        return "approve", f"{n}/{total} LLM responses -- no issues", available

    result = _check_criticals(all_issues)
    if result:
        return (*result, available)

    result = _check_major_consensus(all_issues)
    if result:
        return (*result, available)

    severity_counts = Counter(
        i["severity"] for i in all_issues if i.get("severity") in SEVERITY_ICONS
    )
    if severity_counts.get("major", 0) > 0:
        n_major = severity_counts["major"]
        reason = (
            f"{len(available)}/{total} LLM responses -- "
            f"{n_major} major issue(s) (no consensus, review recommended)"
        )
        return "approve", reason, available

    reason = f"{len(available)}/{total} LLM responses -- minor/suggestion only"
    return "approve", reason, available


def _count_unreviewed_majors(available: dict[str, dict[str, Any]]) -> int:
    """Count major issues in this round that no disposition has closed.

    "Unreviewed" is the headline's own word for them: the aggregate knows
    only that a reviewer raised a major and that the round ended with it
    still standing -- it cannot see a reply, a fix or a Won't fix, so every
    major it can see is one nobody has dispositioned here yet.

    One computation, two readers: main() calls this once and passes the
    value to both ``format_summary``, which renders it, and
    ``post_verdict``, which gates the formal approval on it -- so the
    comment can never say "2 unreviewed major issue(s)" over a posted
    APPROVED review (AT-2823). Neither reader recounts; the value is a
    parameter on both, the way ``approve_quorum`` is.
    """
    return sum(
        1
        for review in available.values()
        for issue in review.get("issues", [])
        if issue.get("severity") == "major"
    )


def _format_issue_line(issue: dict[str, Any]) -> str:
    """Format a single issue as a markdown list item."""
    sev = issue.get("severity", "suggestion")
    icon = SEVERITY_ICONS.get(sev, "*")
    file_part = ""
    if issue.get("file"):
        file_part = f" `{issue['file']}"
        if issue.get("line"):
            file_part += f":{issue['line']}"
        file_part += "`"
    desc = issue.get("description", "")
    line = f"- {icon} **{sev}**{file_part} -- {desc}"
    if issue.get("suggestion"):
        line += f"\n  > ? {issue['suggestion']}"
    return line


def _failed_status_detail(review: dict[str, Any]) -> str:
    """Human-readable reason for a payload whose normalized status is "failed".

    Used both on the headline and in a reviewer's own section: a normalized
    status of "failed" means the reviewer's infrastructure broke and the
    payload never counts as a performed review (AT-1799), regardless of
    whether an ``error`` string happens to be attached.
    """
    err = review.get("error")
    if isinstance(err, str) and err:
        return _error_with_cause(err, review)
    return "reviewer reported status=failed"


def _error_with_cause(err: str, review: dict[str, Any]) -> str:
    """The truncated ``error`` string, followed by its class when one is known.

    Only on the error kind the classes describe: the claude net's
    ``action_invocation_failed``. On any other kind -- another reviewer's
    payload, which an LLM may author -- they would name the wrong cause.
    """
    cause = review.get("error_cause")
    text = (
        ERROR_CAUSE_TEXT.get(cause)
        if isinstance(cause, str) and err == ERROR_CAUSE_KIND
        else None
    )
    head = err[:ERROR_TRUNCATE_LEN]
    return f"{head}: {text}" if text else head


def _has_payload(review: dict[str, Any] | None) -> TypeGuard[dict[str, Any]]:
    """True when a reviewer wrote a payload carrying a summary.

    A ``TypeGuard`` rather than a plain ``bool`` so callers can pass the
    narrowed value straight on: with a plain ``bool`` the type checker
    still sees ``dict | None`` inside the branch, and the ``or {}``
    fallbacks that used to satisfy it read as defensive code guarding a
    case this predicate has already excluded.
    """
    return review is not None and "summary" in review


# Roster reason for the one sub-quorum green path the aggregate can
# positively recognise, in the same family as POLICY_SKIP_ROSTER_REASON and
# coined for the same reason: a gate reading `responded < expected` on a run
# that ended green has only the reason to tell a benign round from a
# degraded one. It is spelled apart from the conclusion-derived reason it
# displaces, because a reviewer a sequential early exit gated off reads as
# the same bare "skipped" as one the REVIEW_MODE condition excluded from a
# round that did run (AT-2511). What makes it safe to coin is the payload it
# rests on: some reviewer wrote an artifact whose `early_exit` is true, so
# the round is known to have run and to have stopped on purpose.
#
# There is deliberately no counterpart for the parallel round where no
# reviewer wrote any artifact at all. That signature -- every job green,
# nothing uploaded -- has no known producer short of every net dying with
# its step: each reviewer path records its own death, claude's and
# gemini's as a `failed` verdict and codex's by failing the job
# (base-ai-review-single.yml, the note above 'Run Claude review'; AT-2539
# closed gemini's gap), and the nets are ordinary steps that can die with
# the job. What is left is that, or a job this pipeline's reviewer steps
# did not produce, and the aggregate is given nothing that separates
# either from a caller's deliberate exclusion: it reads job conclusions
# and artifacts, and both say only that nothing was written -- it cannot
# tell from here. Nor is there a trivial-diff round hiding behind it to
# protect -- every "nothing to review" state is decided before the
# reviewers run and is reported elsewhere: prepare fails the round on an
# empty diff (extract_pr_diff.sh, AT-2201), a size skip exits 1, and a
# policy skip takes its own branch in main() with POLICY_SKIP_ROSTER_REASON.
# base-ai-review-single.yml says the same thing from the reviewer's side --
# "the prompt requires a verdict file even for early_exit, so a missing
# file here is always an infrastructure failure, never a benign skip".
# Those reviewers therefore keep the conclusion-derived
# "early-exit or no-output", which is honest about not knowing.
SEQUENTIAL_EARLY_EXIT_ROSTER_REASON = "skipped (sequential early exit)"
# Every reason that means "this round was fine without them". Exported so a
# reader has one place to find the partition the output descriptions and
# README promise, rather than two constants to collect by hand.
BENIGN_ROSTER_REASONS = frozenset(
    {
        POLICY_SKIP_ROSTER_REASON,
        SEQUENTIAL_EARLY_EXIT_ROSTER_REASON,
    }
)
# The one reason in the roster that is not chosen from a closed set here:
# a failed reviewer's own ``error`` text, which is LLM-authored over PR
# content this project's prompt treats as untrusted (context.md R6). A
# gate decides on whole-string equality against BENIGN_ROSTER_REASONS, so
# without a namespace of its own that text can spell a benign reason and
# a broken reviewer presents itself as a skipped one. Prefixing is
# unconditional -- a conditional escape would have to re-derive the
# collision every time the benign set grows.
FAILED_DETAIL_PREFIX = "failed: "


def _payload_failure_reason(review: dict[str, Any]) -> str:
    """Roster reason for a reviewer whose own payload reported failure.

    The single place payload-derived text enters ``missing``: everything
    else there is a module constant or a ``_missing_reason`` lookup over a
    closed set of job conclusions. Keeping it to one function is what lets
    the namespacing be a property of the roster rather than of one branch.
    """
    return f"{FAILED_DETAIL_PREFIX}{_failed_status_detail(review)}"


def _missing_reviewer_reasons(
    reviews: Mapping[str, dict[str, Any] | None],
    available: dict[str, dict[str, Any]],
    conclusions: dict[str, str] | None,
    *,
    sequential_bypass: bool = False,
) -> dict[str, str]:
    """Reason per configured reviewer that produced no usable verdict.

    Keyed on absence from ``available`` -- the same set the coverage figure
    counts -- so the prose headline and the machine-readable roster can
    never name different reviewers as missing (AT-2511). Ordered by
    ``REVIEWERS`` so both renderings list them the same way.

    The reason strings are the ones already in use: a job conclusion for a
    reviewer that wrote no payload, the payload's own detail -- namespaced
    by ``_payload_failure_reason`` -- for one whose normalized status is
    "failed". The two are conflated in places (AT-2276) and that is fixed
    there, not by a second spelling introduced here.

    The exception is the sequential early-exit round, which the caller
    identifies because it is not visible from a reviewer's conclusion
    alone: the same "skipped" means gated off by an early exit or excluded
    by REVIEW_MODE. It gets a reason of its own so ``missing`` partitions
    into benign and did-not-run -- the distinction a gate cannot make from
    the count, which is honestly 1/3 on that path.

    A parallel round where no reviewer wrote an artifact gets no such
    exception, and deliberately: see SEQUENTIAL_EARLY_EXIT_ROSTER_REASON's
    comment. Nothing the aggregate is given separates that round from a
    credential outage, so those reviewers keep the conclusion-derived
    "early-exit or no-output" -- which names the ambiguity rather than
    resolving it in the direction that merges.
    """
    reasons: dict[str, str] = {}
    for name in REVIEWERS:
        if name in available:
            continue
        review = reviews.get(name)
        if _has_payload(review):
            reasons[name] = _payload_failure_reason(review)
            continue
        conclusion = (conclusions or {}).get(name, "")
        # The benign reason is narrowed to the conclusion its path actually
        # produces, so a reviewer that failed on such a round is still
        # reported as failed. Redundant against today's flag -- and
        # deliberately so: the naming stays correct here rather than
        # depending on how the caller happens to define it.
        if sequential_bypass and conclusion == "skipped":
            # Keyed on the conclusion alone, so on a sequential early-exit
            # round every reviewer reporting "skipped" reads as gated off
            # by that exit -- including one a caller excluded for its own
            # reasons. Base's orchestrator dispatches all three in
            # sequential mode and falls the parallel-mode conclusion
            # through to its sequential twin, so there the two cannot
            # differ; a caller that reimplements the chain can make them.
            # Telling them apart needs a dispatched-reviewer set the
            # aggregate is not given, which is a new consumer-facing input
            # on the orchestrator -- so the
            # wider meaning is documented in the README reason table
            # instead of narrowed here (AT-2511).
            reasons[name] = SEQUENTIAL_EARLY_EXIT_ROSTER_REASON
        else:
            reasons[name] = _missing_reason(conclusion)
    return reasons


# Conclusions that say nothing worth putting on the headline: a reviewer the
# workflow deliberately excluded, or one whose job reported nothing at all.
_QUIET_CONCLUSIONS = frozenset({"", "skipped"})


_FOLD_COMMENTS_QUERY = """
query($owner: String!, $name: String!, $pr: Int!,
      $first: Int!, $after: String) {
  repository(owner: $owner, name: $name) {
    pullRequest(number: $pr) {
      comments(first: $first, after: $after) {
        pageInfo { hasNextPage endCursor }
        nodes { author { login __typename } body }
      }
    }
  }
}
"""
_FOLD_PAGE_SIZE = 50
# The first line of every inline comment the posting step writes:
# `{icon} **{severity}** ({reviewer}): {description}`. Anchored at the start
# of the body, where the LLM-authored description cannot reach, so a
# description that mentions another reviewer by name cannot pass as that
# reviewer's post.
_INLINE_POST_RE = re.compile(
    rf"^\S+ \*\*(?:{'|'.join(SEVERITY_ICONS)}|unknown)\*\* "
    rf"\((?P<reviewer>{'|'.join(REVIEWERS)})\):"
)


def _fetch_fold_comments(repo: str, pr_number: str) -> list[dict[str, Any]]:
    """Every issue comment with its login, author type and body; [] on failure."""
    parts = repo.split("/", 1)
    if len(parts) != 2:
        return []
    owner, name = parts
    return [
        {
            "login": (node.get("author") or {}).get("login") or "",
            "author_type": (node.get("author") or {}).get("__typename") or "",
            "body": node.get("body") or "",
        }
        for node in fetch_paginated_nodes(
            _FOLD_COMMENTS_QUERY,
            "comments",
            owner,
            name,
            pr_number,
            page_size=_FOLD_PAGE_SIZE,
        )
    ]


def _fetch_inline_posts(repo: str, pr_number: str) -> list[dict[str, Any]]:
    """Every inline comment with its login, type, original posting head and body.

    Read over REST for immutable ``original_commit_id``. GitHub can remap
    ``commit_id`` to a later head without another post; that mutable value
    must never establish evidence that this run posted. An absent original
    supplies no evidence. [] on any failure.
    """
    try:
        result = subprocess.run(
            [
                "gh",
                "api",
                "--paginate",
                f"repos/{repo}/pulls/{pr_number}/comments",
                "--jq",
                ".[] | [.user.login, .user.type, .original_commit_id, .body] | @json",
            ],
            capture_output=True,
            text=True,
            timeout=GH_TIMEOUT_SEC,
        )
    except subprocess.TimeoutExpired:
        print("Warning: inline comment listing timed out", file=sys.stderr)
        return []
    if result.returncode != 0:
        print(
            f"Warning: inline comment listing failed: {result.stderr.strip()}",
            file=sys.stderr,
        )
        return []
    posts: list[dict[str, Any]] = []
    for line in result.stdout.splitlines():
        try:
            login, author_type, original, body = json.loads(line)
        except (ValueError, TypeError):
            continue
        posts.append(
            {
                "login": login or "",
                "author_type": author_type or "",
                # GitHub remaps commit_id when a later push moves a diff;
                # only original_commit_id identifies the actual posting head.
                "posted_commit": original or "",
                "body": body or "",
            }
        )
    return posts


def _fold_comment_count(
    comments: list[dict[str, Any]], reviewer: str, round_number: int, head_sha: str
) -> int | None:
    """The count the reviewer's fold comments for this round carry, or None.

    ``comments`` are already INLINE_BOT_LOGIN's. Read as envelopes
    (``fold_envelope``): the marker is the whole first line, the count the
    whole second, and nothing else in the body is searched -- a marker or
    count quoted in a description reaches bot-authored comments verbatim,
    and a body-wide match let a previous verdict comment, or another
    reviewer's fold comment, stand in for this reviewer's (round 10). A
    comment that names a head other than this run's is a superseded run's
    and is skipped; one with the marker but no readable count line is
    skipped too, never read as zero.
    """
    counts: list[int] = []
    markers = fold_markers_for(reviewer, round_number)
    for comment in comments:
        envelope = fold_envelope(comment["body"])
        if envelope is None:
            continue
        marker, count, head = envelope
        if marker not in markers:
            continue
        if head and head_sha and head != head_sha:
            continue
        counts.append(count)
    # A timed-out dedup lookup can post the same reviewer/round fold twice.
    return max(counts) if counts else None


def _reviewers_posted_inline(
    repo: str, pr_number: str, head_sha: str, bot: str, round_number: int
) -> set[str]:
    """Reviewers whose complete batch posted on this invocation and head.

    Require an anchored posting envelope for this reviewer, round and run
    identity as well as the immutable original head and posting author.
    Missing invocation identity supplies no evidence.
    """
    posted: set[str] = set()
    run_identity = inline_run_identity()
    if not head_sha or not run_identity:
        return posted
    for post in _fetch_inline_posts(repo, pr_number):
        if not trusted_post_author(post, bot):
            continue
        if head_sha != post["posted_commit"]:
            continue
        envelope = inline_post_envelope(post["body"])
        if (
            envelope is None
            or envelope[1] != round_number
            or envelope[2] != run_identity
        ):
            continue
        match = _INLINE_POST_RE.match(envelope[3])
        if match and match.group("reviewer") == envelope[0]:
            posted.add(match.group("reviewer"))
    return posted


def _folded_findings(
    available: dict[str, dict[str, Any]],
    repo: str,
    pr_number: str,
    head_sha: str,
) -> dict[str, int | None]:
    """Per responded reviewer, how many findings its posting step folded.

    Read from the PR, never from the verdict file (AT-2553): a fold comment
    -- the round-cutoff summary or the inline fallback -- is written by the
    posting step under ``INLINE_BOT_LOGIN``, opens with a marker bound to the
    reviewer and the round, and carries ``fold_count_line``. The verdict
    file is the reviewer's own JSON, written over PR content, so a count
    recorded there can be planted there; ``INLINE_BOT_LOGIN`` is the identity
    the PR cannot post under -- the workflow's token on Actions, the
    operator's login on the local driver, which sets it -- distinct from
    the configurable aggregate-verdict ``BOT_LOGIN``. It is the
    trust boundary here. The round is this run's own ``fetch_round_count``,
    the same function the posting step used, so an earlier round's comment
    does not match; a comment that names a head is refused when it is not
    this one, so a superseded run's comment does not either.

    A zero is derived only where the PR cannot have authored it: the
    payload lists no findings, so there was nothing to fold; or this
    reviewer's complete inline batch carries this invocation's anchored
    reviewer/round/run envelope, and was originally posted on this head
    under ``INLINE_BOT_LOGIN``. Otherwise ``None`` -- unknown. A reviewer
    that reported findings and left neither a fold comment nor inline
    comments on this head is one whose posting step died before posting,
    or posted nothing because every finding was deduplicated against an
    earlier round's thread or dropped as unlocatable; partially accepted
    batches carry no zero-evidence envelope either. The aggregate cannot
    tell these apart and does not guess -- the all-deduplicated round reads
    as unknown by decision (2026-10-16), not as a zero nothing on this head
    proves. The documented gate blocks on unknown and lets a label through
    it (README, "Output contract reference").

    Keyed on ``available``, the dict the roster's ``responded`` is derived
    from, so a reviewer the roster lists as missing contributes nothing.
    """
    folded: dict[str, int | None] = {}
    with_findings = [
        n for n in REVIEWERS if n in available and available[n].get("issues")
    ]
    for name in REVIEWERS:
        if name in available and name not in with_findings:
            folded[name] = 0
    if not with_findings:
        return folded
    bot = INLINE_BOT_LOGIN
    round_number = fetch_round_count(repo, pr_number) + 1
    comments = [
        c for c in _fetch_fold_comments(repo, pr_number) if trusted_post_author(c, bot)
    ]
    # Fetched only when a fold comment does not settle every reviewer: the
    # common round posts inline, and this is one more paginated read.
    inline: set[str] | None = None
    for name in with_findings:
        from_comment = _fold_comment_count(comments, name, round_number, head_sha)
        if from_comment is not None:
            folded[name] = from_comment
            continue
        # No fold comment for this reviewer and round. That has two
        # producers, and the rule separating them is where the evidence
        # comes from: the batch went to threads, so the step is DONE and
        # the count is a zero the PR could not have authored -- provable
        # only by this reviewer's inline comments on this head under
        # INLINE_BOT_LOGIN; or the step DIED (or posted nothing: every finding
        # deduplicated against an earlier round, or dropped as
        # unlocatable), which leaves nothing on the PR at all. A zero needs
        # the evidence; its absence is unknown, never a zero.
        if inline is None:
            inline = _reviewers_posted_inline(
                repo, pr_number, head_sha, bot, round_number
            )
        folded[name] = 0 if name in inline else None
    return folded


def _folded_findings_total(folded: dict[str, int | None]) -> int | None:
    """The sum a gate reads, or ``None`` when any part of it is unknown."""
    if any(count is None for count in folded.values()):
        return None
    return sum(count for count in folded.values() if count is not None)


def _folded_segment(
    folded: dict[str, int | None], available: dict[str, dict[str, Any]]
) -> str:
    """Headline rendering of ``_folded_findings``; empty when there is nothing to say.

    An unknown says what the aggregate looked at and did not find: the
    reviewer reported N findings and this head carries neither a fold
    comment nor inline comments of its own. A derived zero needs no
    segment -- a plain headline is the statement, and the roster shows the
    0 -- so the two producers of "no fold comment" never read alike.

    Names no route: either comment -- the round-cutoff summary or the inline
    fallback -- is on the PR under the reviewer's name, and a reader sent to
    look for a "Round Cutoff Summary" on a round the fallback folded would
    be looking for a comment that does not exist.

    Names the reviewers that folded, and never a number of comments. The
    fold is per reviewer, one comment each, so a sum over three reviewers
    described as "one comment" sends a reader to find a third of it and
    stop.
    """
    parts: list[str] = []
    by_reviewer = {name: n for name, n in folded.items() if n}
    known = sum(by_reviewer.values())
    if known:
        who = ", ".join(f"{name} {n}" for name, n in by_reviewer.items())
        parts.append(f"{known} finding(s) folded into comments ({who})")
    unknown = [
        f"{name}: {len(available.get(name, {}).get('issues') or [])} finding(s)"
        " reported, none on this head as threads or a fold comment"
        for name, count in folded.items()
        if count is None
    ]
    if unknown:
        parts.append(f"fold count unknown ({'; '.join(unknown)})")
    return "".join(f" | {part}" for part in parts)


def write_reviewer_roster(
    available: dict[str, dict[str, Any]],
    missing_reasons: dict[str, str],
    folded: dict[str, int | None] | None = None,
) -> None:
    """Publish who reviewed, and who did not, to ``$GITHUB_OUTPUT`` (AT-2511).

    Everything here is already known -- it reaches the reader as a prose
    sentence on the summary comment -- but a merge gate cannot read prose.
    All a gate sees is the job's conclusion, and that is `success` whether
    three reviewers produced a verdict or one did: on PR #174 the job ended
    green in 10m26s with claude cut at the step timeout, and the only trace
    was a parenthesis in the comment body.

    This changes no conclusion and no exit status. Whether a 2/3 round
    should go red is a fleet policy question -- every consumer of the
    orchestrator -- and the point of emitting the roster is to let
    that decision be made somewhere it can be made, not to pre-empt it here.

    ``responded`` is derived from ``available``, the same dict the coverage
    figure counts, rather than recomputed: a roster that could disagree
    with the headline would reproduce the defect it exists to report.

    ``folded`` and ``folded_findings_count`` are the same fact for the fold
    (AT-2553): a finding put in one comment rather than an inline thread --
    by the round cutoff, or by the inline fallback -- is not a thread, so a
    gate counting unresolved threads reads a round with folded findings as a
    round with none.

    A zero says those two routes folded nothing. It does NOT say every
    finding reached a thread: a finding with no file or line, or one outside
    the diff, is dropped by the posting step and lives only in the verdict
    comment's body, and no count here covers it. That is pre-existing
    behaviour, unchanged by AT-2553 -- counting those too would make a
    common round block on the documented gate, which is a fleet decision
    rather than this ticket's. The count is empty, not 0, when any
    responded reviewer's count is unknown (``_folded_findings``) -- the
    same reading the README's emptiness guard already gives an absent
    roster. ``folded`` is computed once by the caller and shared with the
    headline, so the two cannot disagree.
    """
    output_path = os.environ.get("GITHUB_OUTPUT", "")
    if not output_path:
        return
    responded = [name for name in REVIEWERS if name in available]
    folded = folded if folded is not None else {}
    folded_total = _folded_findings_total(folded)
    roster = {
        "expected": list(REVIEWERS),
        "responded": responded,
        "missing": missing_reasons,
        "folded": folded,
    }
    # One line, no heredoc delimiter: reviewer-supplied reason text can carry
    # newlines, and json.dumps escapes them, so the value cannot break out of
    # the line-oriented $GITHUB_OUTPUT format or forge a second key.
    payload = json.dumps(roster, separators=(",", ":"))
    # Degrades to a ::warning the way _emit_partial_observability does. An
    # unwritable or full $GITHUB_OUTPUT must not abort main before
    # post_verdict runs: the PR would get a red required check and no
    # comment saying why, and an annotation would have taken down the
    # verdict it exists only to annotate -- which is also what the
    # docstring above promises.
    try:
        with open(output_path, "a", encoding="utf-8", errors="replace") as fh:
            fh.write(f"reviewer_roster={payload}\n")
            fh.write(f"reviewers_expected_count={len(REVIEWERS)}\n")
            fh.write(f"reviewers_responded_count={len(responded)}\n")
            fh.write(
                "folded_findings_count="
                f"{'' if folded_total is None else folded_total}\n"
            )
    except OSError as e:
        print(f"::warning::Failed to write GITHUB_OUTPUT: {e}", file=sys.stderr)


# 58 statements on the day PLR0915 was switched on; splitting it belongs
# with a change to this module, not with the one that turned the check on
# (AT-2420).
def format_summary(  # noqa: PLR0915
    reviews: Mapping[str, dict[str, Any] | None],
    verdict: str,
    reason: str,
    available: dict[str, dict[str, Any]],
    conclusions: dict[str, str] | None = None,
    *,
    comment_only: bool = False,
    approve_quorum: bool = True,
    unreviewed_majors: int,
    sequential_bypass: bool = False,
    folded: dict[str, int | None] | None = None,
) -> str:
    """Build the headline as three independent axes (AT-2240).

    The three questions a reader needs answered can each vary independently,
    so a single fused label collapses distinct situations onto the same
    text: what the rules decided (verdict), whether that was actually
    posted as a formal review or only as a comment (posting), and how many
    of the configured reviewers produced a verdict (coverage). Callers pass
    the same ``approve_quorum`` used for ``post_verdict`` so the headline
    can never describe a different outcome than the one actually posted.

    ``unreviewed_majors`` is a parameter for that same reason, not a count
    taken off ``available`` here (AT-2823): recomputing it would agree with
    ``post_verdict`` only while both happened to be handed the same
    mapping, and a caller passing a different one would reintroduce exactly
    the headline/posted-event disagreement this withhold exists to close.

    It is required rather than defaulted, on both readers. The gate is
    ``unreviewed_majors > 0``, so 0 is the permissive value: a default
    would make a caller that forgot the keyword indistinguishable from a
    clean round, and the silent outcome of that confusion is a formal
    APPROVED review posted over majors nobody counted -- the thing
    AT-2823 exists to prevent. ``approve_quorum`` has the same shape and
    keeps its default: that one predates this change, and widening it is
    not this PR's to do.

    ``sequential_bypass`` is passed for the same reason and forwarded to
    ``_missing_reviewer_reasons``: sharing the helper only makes the prose
    and the roster one rendering of one computation if both are told which
    round this is. Without it a sequential early-exit round says
    "skipped" on the comment about the same reviewers the roster is at
    that moment reporting as gated off by that exit (AT-2511).
    """
    total = len(REVIEWERS)
    n_available = len(available)
    coverage = f"{n_available}/{total} reviewers"

    approve_has_majors = verdict == "approve" and unreviewed_majors > 0
    approve_quorum_short = verdict == "approve" and not approve_quorum
    # The two states that withhold the formal approval, read off the same
    # flags post_verdict is handed (AT-2823, AT-2511).
    approve_withheld = approve_has_majors or approve_quorum_short

    if verdict == "approve":
        label = "Approved"
        if approve_has_majors:
            label += f" with {unreviewed_majors} unreviewed major issue(s)"
        icon = "[!]" if approve_withheld else "[OK]"
    elif verdict == "request_changes":
        label = "Changes Requested"
        icon = "[!]" if comment_only else "[X]"
    else:
        label = "Comment Only"
        icon = "[!]"

    # The coverage segment names the quorum shortfall whenever one exists,
    # independent of comment_only -- otherwise a PR that is both
    # comment_only and quorum-short would report the auto-approve killswitch
    # but never mention that a reviewer didn't respond (AT-2240 follow-up).
    coverage_segment = (
        f"not every reviewer responded ({coverage})"
        if approve_quorum_short
        else coverage
    )

    # The posting axis: only rendered when it diverges from the plain
    # coverage figure, so a clean, fully-posted verdict states just the
    # verdict and the coverage.
    if comment_only and verdict in ("approve", "request_changes"):
        label += f" | posted as comment (auto-approve off) | {coverage_segment}"
    elif approve_quorum_short:
        # The colon introduces a reason, and here the coverage segment is
        # one: "comment only: not every reviewer responded (2/3
        # reviewers)" (AT-2511).
        label += f" | comment only: {coverage_segment}"
    elif approve_withheld:
        # Majors with a full roster. The coverage figure is not the reason
        # -- the majors are, and the label already names them -- so this
        # keeps the plain axis separator. Under a colon it rendered
        # "comment only: 3/3 reviewers", which reads as though a complete
        # roster were the cause of its own downgrade.
        label += f" | comment only | {coverage_segment}"
    else:
        label += f" | {coverage_segment}"

    # The fold axis (AT-2553): findings a reviewer put in one comment -- a
    # round-cutoff summary, or the inline fallback -- are not threads, so a
    # reader counting unresolved threads sees none of them. The verdict word
    # stays; the headline stops being plain. One source with the roster --
    # the map the caller computed once, rendered.
    label += _folded_segment(folded or {}, available)

    # Append per-reviewer reason annotations for any missing verdicts, and for
    # reviewers that did produce a payload but reported status "failed" -- an
    # infrastructure failure has to be named on the headline too, not only in
    # that reviewer's section further down (AT-1837).
    #
    # Shared with the machine-readable roster (AT-2511): the headline shows a
    # subset of the same reasons -- a reviewer that wrote no payload and was
    # either skipped on purpose or reported no conclusion at all is not news
    # here, but it is still absent, so the roster names it.
    #
    # One source, two renderings: FAILED_DETAIL_PREFIX namespaces the roster
    # value a gate compares as a whole string, and nothing compares this
    # prose, so it comes off at the rendering edge rather than being
    # re-derived here. Left on, the detail's own wording doubles it --
    # "claude: failed: reviewer reported status=failed".
    missing_reasons = _missing_reviewer_reasons(
        reviews,
        available,
        conclusions,
        sequential_bypass=sequential_bypass,
    )
    missing_notes = [
        f"{name}: {reason.removeprefix(FAILED_DETAIL_PREFIX)}"
        for name, reason in missing_reasons.items()
        if _has_payload(reviews.get(name))
        or (conclusions or {}).get(name, "") not in _QUIET_CONCLUSIONS
    ]
    reason_suffix = f" ({', '.join(missing_notes)})" if missing_notes else ""

    lines = [
        REVIEW_MARKER,
        "## [bot] Multi-LLM Review Summary",
        "",
        f"**Result: {icon} {label}** -- {reason}{reason_suffix}",
    ]
    # A PR approaching its size limit is warned under the headline, which
    # stays as it is: the next round over the limit reviews nothing (AT-2415).
    for warning in _size_warnings():
        lines.append(f"\n> {warning}")
    # A partial diff is announced on the verdict, paths only (AT-2206).
    excluded_count = _excluded_count()
    if excluded_count > 0:
        listed = ", ".join(f"`{display_path(path)}`" for path in _excluded_paths())
        lines.append(
            f"\n> [i] {excluded_count} file(s) excluded by policy"
            f" ({LENS_IGNORE_PATH}): {listed or 'paths not reported'}"
        )
    review_mode = os.environ.get("REVIEW_MODE", _REVIEW_MODE_PARALLEL)
    if (
        review_mode == _REVIEW_MODE_SEQUENTIAL
        and len(available) < MIN_REVIEWERS_FOR_VERDICT
        and _has_early_exit(available)
    ):
        lines.append(
            f"\n> [!] Early exit: verdict derived from"
            f" {len(available)}/{len(REVIEWERS)} reviewer(s)."
        )
    lines += [
        "",
        "---",
    ]

    for name in REVIEWERS:
        review = reviews.get(name)
        if review is None or "summary" not in review:
            err_msg = (review or {}).get("error", "")
            conclusion = (conclusions or {}).get(name, "")
            # The same reason the headline and the roster carry, not a
            # third derivation of it: on a benign round the conclusion-
            # derived spelling would contradict both, two paragraphs apart
            # in one comment.
            na_label = (
                f"[ ] N/A -- {missing_reasons[name]}" if conclusion else "[ ] N/A"
            )
            lines += ["", f"### {name.title()} -- {na_label}"]
            if err_msg:
                lines.append(f"_{err_msg}_")
            lines.append("")
            continue

        issues = review.get("issues", [])
        summary = review.get("summary", "")
        status = _normalize_status(name, review)
        # A normalized status of "failed" means the reviewer never produced a
        # performed review (AT-1799) -- this takes priority over the generic
        # `error` check below, even when both are present, because "N issue(s)"
        # implies looking and a failed reviewer never looked (AT-2123).
        if status == STATUS_FAILED:
            header = (
                f"### {name.title()} -- [ ] not run ({_failed_status_detail(review)})"
            )
        else:
            header = f"### {name.title()} -- {len(issues)} issue(s)"
            if review.get("error"):
                header += f" [!] (partial: {review['error'][:ERROR_TRUNCATE_LEN]})"
        lines += ["", header, summary]
        for issue in issues:
            lines.append(_format_issue_line(issue))

    return "\n".join(lines)


def _post_comment(pr_number: str, repo: str, body: str) -> bool:
    cmd = ["gh", "pr", "comment", pr_number, "--body-file", "-"]
    if repo:
        cmd += ["--repo", repo]
    try:
        result = subprocess.run(
            cmd, input=body, capture_output=True, text=True, timeout=GH_TIMEOUT_SEC
        )
    except subprocess.TimeoutExpired:
        print("Post comment timed out", file=sys.stderr)
        return False
    if result.returncode != 0:
        print(f"Post comment failed: {result.stderr}", file=sys.stderr)
        return False
    return True


def _annotation_value(text: str) -> str:
    """Escape a value so a workflow command keeps it on its one line."""
    stripped = text.strip()
    return stripped.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")


_REASON_REVIEW_FAILED = "review API failed"
_REASON_REVIEW_TIMED_OUT = "review API timed out"


def _intended_verdict_note(verdict: str, reason: str) -> str:
    """The note a review fallback appends, so it reads apart from a comment."""
    return f"\n\n> [!] Intended verdict: **{verdict}** ({reason}, posted as comment)"


_MINIMIZE_QUERY = """
mutation($id: ID!) {
  minimizeComment(input: {subjectId: $id, classifier: OUTDATED}) {
    minimizedComment { isMinimized }
  }
}
"""

_DISMISS_QUERY = f"""
mutation($id: ID!) {{
  dismissPullRequestReview(input: {{
    pullRequestReviewId: $id,
    message: "{_DISMISS_MESSAGE}"
  }}) {{ pullRequestReview {{ state }} }}
}}
"""


def _run_gql_mutation(query: str, node_id: str, label: str) -> None:
    """Run a GraphQL mutation with a single ID parameter."""
    try:
        subprocess.run(
            ["gh", "api", "graphql", "-f", f"query={query}", "-F", f"id={node_id}"],
            capture_output=True,
            text=True,
            check=True,
            timeout=GH_TIMEOUT_SEC,
        )
    except subprocess.CalledProcessError as e:
        print(f"::warning::GQL {label} failed: {e.stderr.strip()}", file=sys.stderr)
    except subprocess.TimeoutExpired:
        print(f"::warning::GQL {label} timed out", file=sys.stderr)


_STALE_COMMENTS_QUERY = """
query($owner: String!, $name: String!, $pr: Int!,
      $first: Int!, $after: String) {
  repository(owner: $owner, name: $name) {
    pullRequest(number: $pr) {
      comments(first: $first, after: $after) {
        pageInfo { hasNextPage endCursor }
        nodes { id isMinimized body }
      }
    }
  }
}
"""

_STALE_REVIEWS_QUERY = """
query($owner: String!, $name: String!, $pr: Int!,
      $first: Int!, $after: String) {
  repository(owner: $owner, name: $name) {
    pullRequest(number: $pr) {
      reviews(first: $first, after: $after) {
        pageInfo { hasNextPage endCursor }
        nodes { id state body commit { oid } }
      }
    }
  }
}
"""

_STALE_PAGE_SIZE = 50

# dismissPullRequestReview "dismisses an approved or rejected pull request
# review" (GitHub GraphQL reference) and errors on every other state, which
# _run_gql_mutation would surface as a warning every round for as long as the
# review exists. These two are also the only states a ruleset counts, so a
# marker review in any other state has nothing to dismiss.
_DISMISSABLE_STATES = frozenset({"APPROVED", "CHANGES_REQUESTED"})


def _is_prior_round(body: str | None) -> bool:
    """Was this body posted by a round of this pipeline?

    Every verdict this pipeline posts opens with REVIEW_MARKER on line 1 --
    the verdict itself (format_summary), the prepare-failure, size-skip and
    policy-skip comments, and the local driver's two skip comments -- so the
    marker's position is as reliable as its presence and discriminates what
    containment cannot: GitHub's "Quote reply" copies the raw markdown of
    the quoted body, HTML comments included, so a human who quote-replies a
    verdict and approves in the same review would have that approval
    dismissed under a containment test. The rule is that the marker sits at
    column 0 once blank lines are skipped: blank lines render nothing, so
    they are tolerated; any leading space is refused, because no producer
    in this pipeline writes one -- each puts REVIEW_MARKER as element 0 of
    a join -- so a body that opens with " <!-- multi-llm-review -->" is not
    a verdict of ours, and four spaces would besides make it an indented
    code block, someone documenting the marker. A quoted line starts with
    "> ".
    """
    return (body or "").lstrip("\r\n").startswith(REVIEW_MARKER)


def _fold_prior_round_items(pr_number: str, repo: str, *, head_sha: str) -> list[str]:
    """Minimize prior-round verdict comments and dismiss prior-round reviews.

    Runs right before the new verdict is posted, so a review of a superseded
    head -- an approval included -- never outlives the round that replaces
    it. The ruleset otherwise keeps counting a head-A approval on head B
    wherever dismiss_stale_reviews_on_push is off (AT-2599).

    An APPROVED review of head_sha itself is the one item left standing,
    and its ids are returned for _dismiss_replaced_approvals to fold once
    this round has posted a review of its own. The caller does not yet
    know whether it will: the approve-quorum withhold, the comment_only
    downgrade and the fallback after a failed `gh pr review` all post a
    comment and no review, and an approval's permitting force lives only
    in the review, so dismissing it ahead of those turns a PR that was
    green on this head into one blocked by a transient failure of the
    re-run.

    The AT-2823 withhold also posts a comment and no review, and is the
    one such path that is NOT carved out: an undispositioned major is a
    finding about this head rather than a flake of the re-run, so
    post_verdict dismisses the standing approval itself once its comment
    is up (see _Withhold.transient). Left standing it would keep the PR
    green on precisely the same-head re-run the withhold note asks for,
    with the comment saying the approval was withheld. The carve-out is
    for rounds that found nothing wrong and could not say so.

    A CHANGES_REQUESTED of this head goes up front all the same:
    the required check's exit code carries its block for as long as the
    verdict stands, so losing the review loses nothing the gate needs.

    When head_sha is empty, prepare never resolved one (AT-2087, AT-1975).
    Both of those paths post a verdict and, with the killswitch off, no
    review -- so folding a same-head approval up front there is exactly
    the outcome above. The head GitHub reports now stands in: _head_is_stale
    let this run post because nothing could show it superseded, so the
    live head is the head this round is about. Only when that lookup fails
    too is there nothing to compare, and then every approval goes up front:
    the invariant (a superseded approval never outlives the round) needs no
    evidence, the carve-out needs positive evidence of the head, and the
    two failure modes are not symmetric -- an approval of this head folded
    here is visible, since this round's verdict names why it ran, and the
    next round that posts a review replaces it, while a superseded approval
    left standing is silent and stays until a review happens to land. Each
    branch says so in the run log (AT-1975: block visibly). A review that
    names no commit cannot be vouched for either and is folded the same way.

    An item is prior-round when its body opens with REVIEW_MARKER (see
    _is_prior_round); who wrote it is not read. The marker is this
    pipeline's own HTML comment, and the
    verdicts that carry it are posted under whichever identity a path has
    -- github-actions[bot], the reviewer App for an approval, the operator's
    own login on the local driver (review_pr_local.py) -- so any author
    filter folds one path's history and leaves another's standing. Folding
    is housekeeping, not trust: a consumer's merge gate decides whether to
    believe a verdict and needs identity for that; this pass decides what
    to tidy and does not. The worst case, some other account quoting the
    marker in a review of its own, costs that review a dismissal.
    """
    if not repo:
        return []
    parts = repo.split("/", 1)
    if len(parts) != 2:
        print(f"Invalid GITHUB_REPOSITORY format: {repo}", file=sys.stderr)
        return []
    owner, name = parts

    for node in fetch_paginated_nodes(
        _STALE_COMMENTS_QUERY,
        "comments",
        owner,
        name,
        pr_number,
        page_size=_STALE_PAGE_SIZE,
    ):
        if not node.get("isMinimized") and _is_prior_round(node.get("body")):
            _run_gql_mutation(_MINIMIZE_QUERY, node["id"], "minimize")

    head = head_sha.strip()
    if not head:
        head = _current_pr_head(pr_number, repo) or ""
        if head:
            print(
                "::notice::HEAD_SHA unset; prior-round approvals compared"
                f" against {head}, the PR head GitHub reports now",
                file=sys.stderr,
            )
        else:
            print(
                "::notice::HEAD_SHA unset and the PR head lookup failed -- no"
                " head to compare, so every prior-round approval is dismissed"
                " up front",
                file=sys.stderr,
            )
    standing: list[str] = []
    for node in fetch_paginated_nodes(
        _STALE_REVIEWS_QUERY,
        "reviews",
        owner,
        name,
        pr_number,
        page_size=_STALE_PAGE_SIZE,
    ):
        if node.get("state") not in _DISMISSABLE_STATES or not _is_prior_round(
            node.get("body")
        ):
            continue
        reviewed = (node.get("commit") or {}).get("oid")
        if node["state"] == "APPROVED" and head and reviewed == head:
            standing.append(node["id"])
            continue
        _run_gql_mutation(_DISMISS_QUERY, node["id"], "dismiss")
        _run_gql_mutation(_MINIMIZE_QUERY, node["id"], "minimize")
    return standing


def _dismiss_replaced_approvals(review_ids: list[str]) -> None:
    """Fold the same-head approvals _fold_prior_round_items left standing.

    Called once this round's own verdict is on the PR -- either a review
    that `gh pr review` returned 0 for, or, on AT-2823's majors withhold,
    the comment that deliberately replaces one. Those are the two callers
    and they differ in kind: the review path replaces the approval, so
    something still vouches for the head; the withhold path removes it
    and puts nothing back, which is the point, because a major was found
    on this head and the PR must not stay green on a prior round's say-so.
    What both guarantee is that the page already explains itself before
    the old approval goes.

    The other comment-and-no-review paths -- the approve-quorum withhold,
    the comment_only downgrade, the fallback after a failed review -- do
    NOT call this: see _fold_prior_round_items for why a transient round
    must leave a same-head approval alone.

    The ids come from the fold's own read of the page, not a fresh one: a
    re-read now would find this round's review, which opens with the
    marker and is of this head too.
    """
    for review_id in review_ids:
        _run_gql_mutation(_DISMISS_QUERY, review_id, "dismiss")
        _run_gql_mutation(_MINIMIZE_QUERY, review_id, "minimize")


class _Withhold(NamedTuple):
    """Why an approve verdict is not posted, and whether that is a flake.

    ``transient`` is what _fold_prior_round_items needs to know. Its
    carve-out leaves an APPROVED review of this same head standing
    because re-blocking a PR that was green on this head would be a
    regression caused by a transient failure of the re-run -- a reviewer
    that did not answer, the killswitch, an API error. An undispositioned
    major is not one of those: it is a finding about this very head, so
    the approval it contradicts has to go, or the withhold does nothing
    at all on the same-head re-run its own note asks the reader to run.

    The two fields answer different questions and must not be coupled:
    the reason is "what do I tell the reader", the flag is "is this head
    vouched for". They come apart when both withholds apply at once. A
    short roster is reported first, because the missing reviewer may yet
    raise more and that is the more useful thing to say -- but a major
    was still found on this head, so the round is NOT transient and the
    standing approval goes. Putting the flag on the branch that chose the
    prose is what made the combined case transient, and it reopened
    AT-2823 behind a second, unrelated fault.

    The flag travels with the reason rather than being re-derived at the
    call site, because the function that decides *why* is the one that
    knows *which kind*.
    """

    reason: str
    transient: bool


def _approval_withhold(
    verdict: str, approve_quorum: bool, unreviewed_majors: int
) -> _Withhold | None:
    """Why this approve verdict must not be posted as a formal approval.

    Both states mean the same thing -- this round is not finished -- so both
    take one branch and produce one note, rather than a second mechanism
    beside the first. A short roster was the original (AT-2511); majors
    nobody dispositioned were posted green until AT-2823, which is the
    asymmetry this returns a single reason to close. Coverage is named
    first: with both true, the missing reviewer may yet raise more.

    The majors reason carries how it clears, because the reader it
    reaches is the one it blocks. There is no key to hold: the aggregate
    re-decides every round from that round's payloads, so the approval
    posts as soon as a round ends with no major standing. What the
    aggregate cannot do is see a disposition -- a reply, a fix or a
    Won't fix on a thread reaches the reviewers (prepare hands them the
    unresolved threads as context) but never this function, so the reason
    says a reply may stop a reviewer raising it again rather than
    promising that a reply clears the withhold. AT-2843 is the channel
    that would make a resolved thread clear its own major.

    The human path is named too: a human approving the PR satisfies a
    required-approving-review rule on its own, independently of anything
    the App does or does not post.
    """
    if verdict != "approve":
        return None
    if not approve_quorum:
        # Coverage is named first, but the two questions are separate: the
        # reason answers "what do I tell the reader", the flag answers "is
        # this head vouched for". A short roster alone is transient -- the
        # missing reviewer may answer next run and nothing has been found
        # wrong -- while majors alongside it still mean a finding on this
        # head, so the flag reads the majors even when the prose does not.
        return _Withhold(
            "not every configured reviewer produced a verdict",
            unreviewed_majors == 0,
        )
    if unreviewed_majors > 0:
        return _Withhold(
            f"{unreviewed_majors} major issue(s) raised this round are not"
            " dispositioned. The next round decides again: push a fix, or"
            " reply on the thread so the reviewers see it, then re-run the"
            " review (push a commit, or re-run the workflow) -- the approval"
            " posts once a round ends with no major raised. A human approving"
            " this PR satisfies a required-review rule on its own",
            False,
        )
    return None


def _post_withheld_verdict(
    pr_number: str,
    repo: str,
    comment: str,
    withhold: _Withhold,
    standing: list[str],
) -> None:
    """Post a withheld approve verdict as a comment, and fold if it must.

    Split out of post_verdict, which the withhold grew past the 80-line
    guideline; the block was already self-contained.
    """
    # The outcome leads and the reason follows, rather than the reason
    # leading and the outcome trailing behind a semicolon: the majors
    # reason is several sentences now, and a trailing "; posted as
    # comment" read as a clause of whichever sentence happened to end it.
    print(
        "::notice::Auto-approve withheld, posting comment instead of"
        f" approval -- {withhold.reason}.",
        file=sys.stderr,
    )
    note = f"\n\n> [!] Auto-approve withheld, posted as comment: {withhold.reason}."
    if not _post_comment(pr_number, repo, comment + note):
        print("Failed to post comment", file=sys.stderr)
        sys.exit(1)
    # A non-transient withhold retires the standing same-head approval
    # that _fold_prior_round_items deliberately left alone. Its carve-out
    # is for rounds that post no review because something flaked; a major
    # found on this head is not that, and leaving the approval up would
    # keep the PR green on exactly the same-head re-run the note above
    # tells the reader to make.
    #
    # The position is load-bearing, not incidental: _post_comment exits 1
    # above on failure, so reaching this line means the explanation is
    # already on the PR. Dismissing before it would let a failed comment
    # pull the approval and leave nothing saying why -- a silently
    # blocked PR, which is the failure AT-1975 ("block visibly") exists
    # to prevent.
    if not withhold.transient:
        _dismiss_replaced_approvals(standing)


def post_verdict(
    comment: str,
    verdict: str,
    *,
    comment_only: bool,
    unreviewed_majors: int,
    approve_quorum: bool = True,
) -> bool:
    """Post the verdict; return whether a review or fallback comment landed."""
    pr_number = os.environ.get("PR_NUMBER", "")
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    if not pr_number or not pr_number.isdigit():
        print(f"PR_NUMBER missing or invalid: {pr_number!r}", file=sys.stderr)
        sys.exit(1)

    standing = _fold_prior_round_items(
        pr_number, repo, head_sha=os.environ.get("HEAD_SHA", "")
    )

    # Never send a formal APPROVED review for a round that is not finished:
    # an approval asserts coverage that never happened, or fills the
    # ruleset's required approval over issues still awaiting a disposition.
    # The verdict (and thus the merge-gate exit code) is unchanged; only the
    # posted event is downgraded to a comment, and the next round approves
    # once the reason is gone.
    #
    # Computed unconditionally. comment_only gates the *prose* below and
    # nothing else: the headline already says "posted as comment
    # (auto-approve off)", so a withhold note would be a second reason for
    # one downgrade -- but the fold is not an explanation, it is the
    # retirement of an approval this round's finding contradicts, and it
    # has to happen on every path that found something.
    withhold = _approval_withhold(verdict, approve_quorum, unreviewed_majors)

    if withhold is not None and not comment_only:
        _post_withheld_verdict(pr_number, repo, comment, withhold, standing)
        return True

    # Downgrade formal review verdicts to comment when killswitch is off.
    if verdict in ("approve", "request_changes") and comment_only:
        if not _post_comment(pr_number, repo, comment):
            print("Failed to post comment", file=sys.stderr)
            sys.exit(1)
        # The same fold the withheld path does, for the same reason and in
        # the same position -- after the comment lands. The killswitch
        # suppresses the second explanation, not the consequence: a round
        # that found a major on this head must not leave a prior APPROVED
        # of this head standing to satisfy the required-review rule.
        if withhold is not None and not withhold.transient:
            _dismiss_replaced_approvals(standing)
        return True

    if verdict in ("approve", "request_changes"):
        base_args = ["gh", "pr", "review", pr_number, "--body-file", "-"]
        if repo:
            base_args += ["--repo", repo]
        flag = "--approve" if verdict == "approve" else "--request-changes"
        # github-actions[bot] is forbidden from approving PRs. When a dedicated
        # reviewer App token is provided, use it ONLY for the approve call so a
        # real APPROVED review is posted. All other gh calls keep the default
        # GH_TOKEN. Missing/empty token falls through to the existing behavior.
        review_env = os.environ.copy()
        reviewer_token = os.environ.get("REVIEWER_TOKEN", "").strip()
        if verdict == "approve" and reviewer_token:
            review_env["GH_TOKEN"] = reviewer_token
        elif verdict == "approve":
            # The mint step is skipped or continue-on-error, so an absent
            # token is otherwise invisible in the run (AT-2597). A notice,
            # not a warning: auto-approve with no App is a supported setup;
            # the warning belongs to the fallback below, if it happens.
            print(
                "::notice::No reviewer App token; approve will be attempted"
                " under GH_TOKEN; if the API rejects it, the verdict is posted"
                f" as a comment marked '{_REASON_REVIEW_FAILED}'",
                file=sys.stderr,
            )
        try:
            result = subprocess.run(
                base_args + [flag],
                input=comment,
                capture_output=True,
                text=True,
                timeout=GH_TIMEOUT_SEC,
                env=review_env,
            )
        except subprocess.TimeoutExpired:
            print(
                "::warning::Post review timed out, falling back to comment",
                file=sys.stderr,
            )
            timeout_note = _intended_verdict_note(verdict, _REASON_REVIEW_TIMED_OUT)
            # Returning False, not exiting, is deliberate: review_result
            # records it as posted=false (AT-2520), pinned by
            # test_aggregate_review_result.py
            # test_timed_out_review_returns_the_actual_comment_fallback_outcome.
            return _post_comment(pr_number, repo, comment + timeout_note)
        if result.returncode != 0:
            print(
                "::warning::Post review failed, falling back to comment: "
                + _annotation_value(result.stderr),
                file=sys.stderr,
            )
            fallback_note = _intended_verdict_note(verdict, _REASON_REVIEW_FAILED)
            if not _post_comment(pr_number, repo, comment + fallback_note):
                print("Both review and fallback comment failed", file=sys.stderr)
                sys.exit(1)
            return True
        _dismiss_replaced_approvals(standing)
    else:
        if not _post_comment(pr_number, repo, comment):
            print("Failed to post comment", file=sys.stderr)
            sys.exit(1)
    return True


def _write_review_result(result: dict[str, Any]) -> None:
    """Export outcome facts without changing the existing aggregate exit policy."""
    output_path = os.environ.get("GITHUB_OUTPUT", "")
    if not output_path:
        return
    try:
        with open(output_path, "a", encoding="utf-8", errors="replace") as output:
            output.write(f"review_result={json.dumps(result, separators=(',', ':'))}\n")
    except OSError as error:
        # A missing result is unknown, never success, to the runner gate.
        # Still leave the existing verdict and exit behavior intact.
        print(f"::warning::Failed to write review_result: {error}", file=sys.stderr)


def _post_recorded_verdict(
    result: dict[str, Any],
    comment: str,
    verdict: str,
    *,
    comment_only: bool,
    unreviewed_majors: int,
    approve_quorum: bool = True,
) -> None:
    result["verdict"] = verdict
    result["unreviewed_majors"] = unreviewed_majors
    posted = post_verdict(
        comment,
        verdict,
        comment_only=comment_only,
        unreviewed_majors=unreviewed_majors,
        approve_quorum=approve_quorum,
    )
    # A successful API call with an arbitrary body is not a verdict the
    # merge gate can identify. All normal formatters lead with this marker.
    result["posted"] = posted is True and comment.startswith(REVIEW_MARKER)


# 53 statements, none of them this branch's: the gate PLR0915 is being
# switched on here catches a function AT-2553 and AT-3023 grew. Splitting
# it belongs with a change to this module, as for format_summary above
# (AT-2420).
def _aggregate(result: dict[str, Any]) -> None:  # noqa: PLR0915
    # First, ahead of every other path: a run whose head has been superseded
    # posts nothing. The job still runs -- it has to, its name is the required
    # status context -- and it still fails, so a superseded run can never be
    # mistaken for one that reviewed the current commit.
    if _head_is_stale():
        result["reason"] = "head_superseded"
        print(
            "::notice title=Aggregate::the head this run reviewed has been"
            " superseded -- no verdict posted",
            file=sys.stderr,
        )
        print(
            "Final verdict: none -- head superseded; the run reviewing the"
            " current commit posts the verdict for this PR"
        )
        # Not exit 0: nothing reviewed the current head, and a green required
        # check would say something had. This check reports on the superseded
        # commit, which is no longer the one the ruleset evaluates.
        #
        # This is the one path that blocks without saying so on the PR, and it
        # is an exception to AT-1975's "block visibly", not an instance of it.
        # AT-1975 posts a comment because nothing else would ever explain the
        # block. Here the live run posts one seconds later, and any comment
        # from this run is the AT-2092 race itself -- the explanation would be
        # the defect.
        sys.exit(1)

    # Checked before the size gate: when prepare fails, its size outputs are
    # empty, so the size path cannot recognise this run at all. The two are
    # mutually exclusive in practice -- every step after the size check is
    # gated on skip == 'false', so a skipping prepare succeeds.
    prepare_failure = _prepare_failure_result()
    if prepare_failure is not None:
        result["reason"] = "prepare_failed"
        comment = format_prepare_failure_summary(
            prepare_failure, os.environ.get("RUN_URL", "").strip()
        )
        # PR_NUMBER does not come from prepare -- it is the event payload on
        # pull_request and the caller's input on workflow_dispatch -- so it
        # survives a prepare failure. If it is somehow absent, post_verdict
        # exits 1 with the reason on stderr: the check is still created and
        # still red, explained only in the runner log. Worse than a comment,
        # far better than a context that never appears.
        # Zero majors is the fact here, not a placeholder: prepare failed,
        # so no reviewer ran and nothing was raised. The same holds for the
        # size-skip and policy-skip exits below.
        _post_recorded_verdict(
            result,
            comment,
            "request_changes",
            comment_only=_is_comment_only(),
            unreviewed_majors=0,
        )
        print(f"Final verdict: request_changes -- prepare reported {prepare_failure}")
        # No review happened at all; passing would newly permit unreviewed
        # merges, which is the opposite of what this check exists for.
        sys.exit(1)

    size_skip = _size_skip_details()
    if size_skip is not None:
        result["reason"] = "size_skipped"
        comment = format_size_skip_summary(size_skip)
        _post_recorded_verdict(
            result,
            comment,
            "request_changes",
            comment_only=_is_comment_only(),
            unreviewed_majors=0,
        )
        print(
            "Final verdict: request_changes -- review skipped,"
            f" {format_size_skip_reason(size_skip)}"
        )
        # Blocking is the pre-existing policy (an unreported required check
        # already made these PRs unmergeable); this only makes it visible.
        sys.exit(1)

    policy_skip = _policy_skip_details()
    if policy_skip is not None:
        result["reason"] = "policy_skipped"
        result["policy_skipped"] = True
        comment = format_policy_skip_summary(policy_skip)
        # A plain comment, never an approval: nothing was reviewed, and an
        # APPROVED review would attest to a review that did not happen. The
        # verdict "comment" takes post_verdict's comment-only branch on every
        # ALLOW_AUTO_APPROVE setting.
        _post_recorded_verdict(
            result,
            comment,
            "comment",
            comment_only=_is_comment_only(),
            unreviewed_majors=0,
        )
        print(
            f"Final verdict: none -- review skipped, {len(policy_skip)}"
            " policy-excluded file(s) only"
        )
        # The only path that ends green having reached no reviewer, so the
        # only one where a gate reads these counts -- and an absent output is
        # an empty string, not a zero, to whatever comparison it feeds. The
        # three paths above exit 1 and deliberately emit nothing: a red run
        # already blocks on its conclusion, and a roster for a round that
        # never ran would assert a coverage figure about this PR that no
        # reviewer was ever asked to produce. On a superseded head that
        # assertion would also be racing the live run's true one.
        write_reviewer_roster(
            {}, {name: POLICY_SKIP_ROSTER_REASON for name in REVIEWERS}
        )
        # Success by decision (AT-2206): the consumer's own rule says the
        # content is not review material, and a failure would block merge on
        # a repository with no ruleset to override it. A neutral conclusion
        # is not available to a job (`exit 78` was removed in 2019), and a
        # separate neutral check run would need an App token consumers may
        # lack -- so a gate that must not merge on this keys on the
        # POLICY_SKIP_MARKER in the comment instead.
        sys.exit(0)

    reviews = load_reviews()
    conclusions = load_reviewer_conclusions()
    partial_names = _emit_partial_observability(reviews, conclusions)
    verdict, reason, available = apply_verdict_rules(reviews)

    # Downgrade CHANGES_REQUESTED -> COMMENTED when at most one reviewer
    # produced a non-partial response. A single survivor is not enough
    # signal to block; defer to manual review.
    total = len(REVIEWERS)
    successful_count = total - len(partial_names)
    if verdict == "request_changes" and successful_count <= 1:
        print(
            "::notice::Aggregate downgraded -- only"
            f" {successful_count}/{total} reviewers responded; manual review"
            " recommended.",
            file=sys.stderr,
        )
        verdict = "comment"
        reason = (
            f"Downgraded from CHANGES_REQUESTED -- only {successful_count}/{total}"
            " reviewers responded (manual review recommended)"
        )

    comment_only = _is_comment_only()
    # Computed once so the headline and the posted event can never disagree
    # about whether every configured reviewer produced a verdict (AT-2240).
    approve_quorum = _has_full_reviewer_coverage(available)
    # The same contract for the other thing that withholds an approval: one
    # count, passed to the headline and to the posted event (AT-2823).
    unreviewed_majors = _count_unreviewed_majors(available)
    result.update(
        reason="reviewed" if not partial_names else "partial_responses",
        responded=[name for name in REVIEWERS if name in available],
        partial=partial_names,
    )

    review_mode = os.environ.get("REVIEW_MODE", _REVIEW_MODE_PARALLEL)
    # Computed before the emit, not just before the exit check below, so the
    # roster and the exit code read the same flag. A round sequential_bypass
    # lets through ends green sub-quorum by design, and the reason string is
    # the only place that can say so.
    sequential_bypass = review_mode == _REVIEW_MODE_SEQUENTIAL and _has_early_exit(
        available
    )
    # Read from the PR once (AT-2553), then rendered twice -- the roster a
    # gate reads and the headline a human reads -- so they cannot disagree.
    folded = _folded_findings(
        available,
        os.environ.get("GITHUB_REPOSITORY", ""),
        os.environ.get("PR_NUMBER", ""),
        os.environ.get("HEAD_SHA", "").strip(),
    )
    write_reviewer_roster(
        available,
        _missing_reviewer_reasons(
            reviews,
            available,
            conclusions,
            sequential_bypass=sequential_bypass,
        ),
        folded,
    )
    comment = format_summary(
        reviews,
        verdict,
        reason,
        available,
        conclusions,
        comment_only=comment_only,
        approve_quorum=approve_quorum,
        unreviewed_majors=unreviewed_majors,
        sequential_bypass=sequential_bypass,
        folded=folded,
    )
    _post_recorded_verdict(
        result,
        comment,
        verdict,
        comment_only=comment_only,
        approve_quorum=approve_quorum,
        unreviewed_majors=unreviewed_majors,
    )
    print(f"Final verdict: {verdict} -- {reason}")

    if len(available) < MIN_REVIEWERS_FOR_VERDICT and not sequential_bypass:
        print("ERROR: Insufficient LLM responses -- failing CI", file=sys.stderr)
        sys.exit(1)


def main() -> None:
    # Defaults describe an interrupted/failed aggregate, not an approving
    # round. Never copy model summaries, issues or provider errors here.
    result: dict[str, Any] = {
        "version": 1,
        "target_repo": os.environ.get("GITHUB_REPOSITORY", ""),
        "pr_number": os.environ.get("PR_NUMBER", ""),
        "head_sha": os.environ.get("HEAD_SHA", ""),
        "verdict": "none",
        "reason": "aggregate_failed",
        "expected": list(REVIEWERS),
        "responded": [],
        "partial": [],
        "unreviewed_majors": 0,
        "policy_skipped": False,
        "posted": False,
    }
    try:
        _aggregate(result)
    finally:
        _write_review_result(result)


if __name__ == "__main__":
    main()
