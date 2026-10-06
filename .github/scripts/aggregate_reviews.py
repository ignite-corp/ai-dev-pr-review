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
from typing import Any, TypeGuard

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
    REVIEW_MARKER,
    REVIEWER_NAMES,
    SEVERITY_ICONS,
    GH_TIMEOUT_SEC,
    display_path,
    fetch_paginated_nodes,
    int_env,
    is_valid_review as _is_valid_review,
    normalize_bot_login,
    normalize_severity as _normalize_severity,
)

logger = logging.getLogger(__name__)

REVIEWERS: dict[str, str] = {name: f"review-{name}.json" for name in REVIEWER_NAMES}

# Max chars for issue description in verdict reason string.
# Keeps the PR review title concise while showing enough context.
DESC_TRUNCATE_LEN = 80
ERROR_TRUNCATE_LEN = 60
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
BOT_LOGIN = os.environ.get("BOT_LOGIN", "github-actions[bot]")

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
        return err[:ERROR_TRUNCATE_LEN]
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
    """True when every reviewer job exited 0 but produced no review payload.

    Says nothing about why. The reviewer step in base-ai-review-single.yml
    is continue-on-error, so a reviewer whose CLI or action dies on a
    missing or bad credential reports `success` having uploaded nothing --
    the same two facts this reads. Callers must not treat it as a benign
    classification; it only suppresses the sub-quorum CI failure, which is
    pre-existing behaviour on this path.
    """
    conclusions = load_reviewer_conclusions()
    return len(conclusions) == total and all(
        v == "success" for v in conclusions.values()
    )


def _artifacts_entirely_absent(
    reviews: Mapping[str, dict[str, Any] | None],
) -> bool:
    """True when no reviewer wrote any artifact at all.

    Error-bearing fallback payloads (e.g. the codex CLI-failure verdict)
    are artifacts: their presence marks an infrastructure failure the
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
            if _artifacts_entirely_absent(reviews) and _all_reviewer_jobs_succeeded(
                total
            ):
                return (
                    "approve",
                    f"0/{total} LLM responses -- all early-exit or no-output,"
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
        f"**Result: [X] Review did not run -- the prepare job reported"
        f" `{result}`**"
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
        "1. **`PR_SIZE_LIMIT` is not an integer.** Fix the repository variable"
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


def _size_skip_details() -> tuple[str, str] | None:
    """(total, limit) when prepare skipped the review for size, else None.

    prepare gates the reviewer jobs on PR_SIZE_LIMIT but cannot skip this job:
    its name is a required status context, and a job that never runs reports
    nothing, so the check sits Pending with no failure to explain it. The
    aggregate therefore runs on the skip path and renders it (AT-1975).
    """
    if os.environ.get("SIZE_SKIPPED", "false").strip().lower() != "true":
        return None
    return (
        os.environ.get("SIZE_TOTAL", "").strip() or "unknown",
        os.environ.get("SIZE_LIMIT", "").strip() or "unknown",
    )


def format_size_skip_summary(total: str, limit: str) -> str:
    """Verdict body for a review skipped because the PR is too large.

    Names the measurement and both remedies: the original defect was not that
    large PRs are blocked -- they already were -- but that nothing said so or
    said what to do about it.
    """
    headline = (
        f"**Result: [X] Review skipped -- PR too large** --"
        f" {total} changed lines exceeds the limit of {limit}"
    )
    why_red = (
        "No reviewer ran, so this check reports a failure rather than a"
        " verdict. That is deliberate: a required check that is never"
        " reported stays Pending forever with nothing to act on."
    )
    remedy_split = (
        f"1. **Split this PR** so each part is at or under {limit} changed lines."
    )
    remedy_limit = (
        "2. **Raise the limit for this repository** by setting the"
        f" `PR_SIZE_LIMIT` repository variable above {total}"
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
    listed = [f"- `{display_path(path)}`" for path in paths] or ["- (paths not reported)"]
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
        return err[:ERROR_TRUNCATE_LEN]
    return "reviewer reported status=failed"


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
# nothing uploaded -- is exactly what a credential outage produces
# (base-ai-review-orchestrator.yml, the note above review-codex-s: the
# review step is continue-on-error, so a reviewer whose CLI or action dies
# on a missing or bad credential reports `success` with no verdict
# artifact), and the aggregate is given nothing that separates the two: it
# reads job conclusions and artifacts, and both are identical in the two
# cases. Nor is there a trivial-diff round hiding behind it to protect --
# every "nothing to review" state is decided before the reviewers run and
# is reported elsewhere: prepare fails the round on an empty diff
# (extract_pr_diff.sh, AT-2201), a size skip exits 1, and a policy skip
# takes its own branch in main() with POLICY_SKIP_ROSTER_REASON.
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
            # across the orchestrator and the wrapper's lockstep -- so the
            # wider meaning is documented in the README reason table
            # instead of narrowed here (AT-2511).
            reasons[name] = SEQUENTIAL_EARLY_EXIT_ROSTER_REASON
        else:
            reasons[name] = _missing_reason(conclusion)
    return reasons


# Conclusions that say nothing worth putting on the headline: a reviewer the
# workflow deliberately excluded, or one whose job reported nothing at all.
_QUIET_CONCLUSIONS = frozenset({"", "skipped"})


def write_reviewer_roster(
    available: dict[str, dict[str, Any]],
    missing_reasons: dict[str, str],
) -> None:
    """Publish who reviewed, and who did not, to ``$GITHUB_OUTPUT`` (AT-2511).

    Everything here is already known -- it reaches the reader as a prose
    sentence on the summary comment -- but a merge gate cannot read prose.
    All a gate sees is the job's conclusion, and that is `success` whether
    three reviewers produced a verdict or one did: on PR #174 the job ended
    green in 10m26s with claude cut at the step timeout, and the only trace
    was a parenthesis in the comment body.

    This changes no conclusion and no exit status. Whether a 2/3 round
    should go red is a fleet policy question -- 17 pilot consumers plus the
    wrapper's lockstep -- and the point of emitting the roster is to let
    that decision be made somewhere it can be made, not to pre-empt it here.

    ``responded`` is derived from ``available``, the same dict the coverage
    figure counts, rather than recomputed: a roster that could disagree
    with the headline would reproduce the defect it exists to report.
    """
    output_path = os.environ.get("GITHUB_OUTPUT", "")
    if not output_path:
        return
    responded = [name for name in REVIEWERS if name in available]
    roster = {
        "expected": list(REVIEWERS),
        "responded": responded,
        "missing": missing_reasons,
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
    sequential_bypass: bool = False,
) -> str:
    """Build the headline as three independent axes (AT-2240).

    The three questions a reader needs answered can each vary independently,
    so a single fused label collapses distinct situations onto the same
    text: what the rules decided (verdict), whether that was actually
    posted as a formal review or only as a comment (posting), and how many
    of the configured reviewers produced a verdict (coverage). Callers pass
    the same ``approve_quorum`` used for ``post_verdict`` so the headline
    can never describe a different outcome than the one actually posted.

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

    n_major = sum(
        1
        for review in available.values()
        for issue in review.get("issues", [])
        if issue.get("severity") == "major"
    )
    approve_has_majors = verdict == "approve" and n_major > 0
    approve_quorum_short = verdict == "approve" and not approve_quorum

    if verdict == "approve":
        label = "Approved"
        if approve_has_majors:
            label += f" with {n_major} unreviewed major issue(s)"
        icon = "[!]" if approve_has_majors or approve_quorum_short else "[OK]"
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
        f"not every reviewer responded ({coverage})" if approve_quorum_short else coverage
    )

    # The posting axis: only rendered when it diverges from the plain
    # coverage figure, so a clean, fully-posted verdict states just the
    # verdict and the coverage.
    if comment_only and verdict in ("approve", "request_changes"):
        label += f" | posted as comment (auto-approve off) | {coverage_segment}"
    elif approve_quorum_short:
        label += f" | comment only: {coverage_segment}"
    else:
        label += f" | {coverage_segment}"

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
            header = f"### {name.title()} -- [ ] not run ({_failed_status_detail(review)})"
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
        nodes { id author { login } isMinimized body }
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
        nodes { id author { login } state body }
      }
    }
  }
}
"""

_STALE_PAGE_SIZE = 50


def _minimize_stale_bot_items(pr_number: str, repo: str) -> None:
    """Minimize previous bot comments and dismiss stale reviews."""
    if not repo:
        return
    parts = repo.split("/", 1)
    if len(parts) != 2:
        print(f"Invalid GITHUB_REPOSITORY format: {repo}", file=sys.stderr)
        return
    owner, name = parts
    # The queries read author.login over GraphQL, which drops the "[bot]"
    # suffix the BOT_LOGIN default carries; normalize both sides so the
    # spelling of either never decides whether a prior round is folded.
    bot = normalize_bot_login(BOT_LOGIN)

    for node in fetch_paginated_nodes(
        _STALE_COMMENTS_QUERY,
        "comments",
        owner,
        name,
        pr_number,
        page_size=_STALE_PAGE_SIZE,
    ):
        if (
            normalize_bot_login((node.get("author") or {}).get("login") or "") == bot
            and not node.get("isMinimized")
            and REVIEW_MARKER in node.get("body", "")
        ):
            _run_gql_mutation(_MINIMIZE_QUERY, node["id"], "minimize")

    for node in fetch_paginated_nodes(
        _STALE_REVIEWS_QUERY,
        "reviews",
        owner,
        name,
        pr_number,
        page_size=_STALE_PAGE_SIZE,
    ):
        if (
            normalize_bot_login((node.get("author") or {}).get("login") or "") == bot
            and node.get("state") == "CHANGES_REQUESTED"
            and REVIEW_MARKER in node.get("body", "")
        ):
            _run_gql_mutation(_DISMISS_QUERY, node["id"], "dismiss")
            _run_gql_mutation(_MINIMIZE_QUERY, node["id"], "minimize")


def post_verdict(
    comment: str,
    verdict: str,
    *,
    comment_only: bool,
    approve_quorum: bool = True,
) -> None:
    pr_number = os.environ.get("PR_NUMBER", "")
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    if not pr_number or not pr_number.isdigit():
        print(f"PR_NUMBER missing or invalid: {pr_number!r}", file=sys.stderr)
        sys.exit(1)

    _minimize_stale_bot_items(pr_number, repo)

    # Never send a formal APPROVED review unless every configured reviewer
    # produced a verdict: an approval would assert coverage that never
    # happened. The verdict (and thus the merge-gate exit code) is
    # unchanged; only the posted event is downgraded to a comment.
    if verdict == "approve" and not approve_quorum:
        print(
            "::notice::Auto-approve withheld -- not every configured reviewer"
            " produced a verdict; posting comment instead of approval.",
            file=sys.stderr,
        )
        note = (
            "\n\n> [!] Auto-approve withheld: not every configured reviewer"
            " produced a verdict; posted as comment."
        )
        if not _post_comment(pr_number, repo, comment + note):
            print("Failed to post comment", file=sys.stderr)
            sys.exit(1)
        return

    # Downgrade formal review verdicts to comment when killswitch is off.
    if verdict in ("approve", "request_changes") and comment_only:
        if not _post_comment(pr_number, repo, comment):
            print("Failed to post comment", file=sys.stderr)
            sys.exit(1)
        return

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
            print("Post review timed out, falling back to comment", file=sys.stderr)
            _post_comment(pr_number, repo, comment)
            return
        if result.returncode != 0:
            print(
                f"Post review failed: {result.stderr}, falling back to comment",
                file=sys.stderr,
            )
            fallback_note = (
                f"\n\n> [!] Intended verdict: **{verdict}**"
                " (review API failed, posted as comment)"
            )
            if not _post_comment(pr_number, repo, comment + fallback_note):
                print("Both review and fallback comment failed", file=sys.stderr)
                sys.exit(1)
    else:
        if not _post_comment(pr_number, repo, comment):
            print("Failed to post comment", file=sys.stderr)
            sys.exit(1)


def main() -> None:
    # First, ahead of every other path: a run whose head has been superseded
    # posts nothing. The job still runs -- it has to, its name is the required
    # status context -- and it still fails, so a superseded run can never be
    # mistaken for one that reviewed the current commit.
    if _head_is_stale():
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
        comment = format_prepare_failure_summary(
            prepare_failure, os.environ.get("RUN_URL", "").strip()
        )
        # PR_NUMBER does not come from prepare -- it is the event payload on
        # pull_request and the caller's input on workflow_dispatch -- so it
        # survives a prepare failure. If it is somehow absent, post_verdict
        # exits 1 with the reason on stderr: the check is still created and
        # still red, explained only in the runner log. Worse than a comment,
        # far better than a context that never appears.
        post_verdict(comment, "request_changes", comment_only=_is_comment_only())
        print(f"Final verdict: request_changes -- prepare reported {prepare_failure}")
        # No review happened at all; passing would newly permit unreviewed
        # merges, which is the opposite of what this check exists for.
        sys.exit(1)

    size_skip = _size_skip_details()
    if size_skip is not None:
        total, limit = size_skip
        comment = format_size_skip_summary(total, limit)
        post_verdict(
            comment,
            "request_changes",
            comment_only=_is_comment_only(),
        )
        print(
            f"Final verdict: request_changes -- review skipped, {total} changed"
            f" lines exceeds PR_SIZE_LIMIT {limit}"
        )
        # Blocking is the pre-existing policy (an unreported required check
        # already made these PRs unmergeable); this only makes it visible.
        sys.exit(1)

    policy_skip = _policy_skip_details()
    if policy_skip is not None:
        comment = format_policy_skip_summary(policy_skip)
        # A plain comment, never an approval: nothing was reviewed, and an
        # APPROVED review would attest to a review that did not happen. The
        # verdict "comment" takes post_verdict's comment-only branch on every
        # ALLOW_AUTO_APPROVE setting.
        post_verdict(comment, "comment", comment_only=_is_comment_only())
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

    review_mode = os.environ.get("REVIEW_MODE", _REVIEW_MODE_PARALLEL)
    sequential_bypass = review_mode == _REVIEW_MODE_SEQUENTIAL and _has_early_exit(
        available
    )
    # Every job exited 0 AND no reviewer wrote any artifact. Error-bearing
    # fallback payloads disqualify the bypass -- provider failures must fail
    # CI. It suppresses the sub-quorum exit below and nothing else: it is
    # NOT a benign classification, and does not reach the roster. The same
    # signature is what a credential outage produces
    # (SEQUENTIAL_EARLY_EXIT_ROSTER_REASON's comment), and the aggregate is
    # given nothing that separates the two, so the reasons say
    # "early-exit or no-output" and a gate reading the reason table blocks
    # on it even though this run stays green. Keeping the run green is the
    # pre-existing behaviour on this path -- _check_insufficient already
    # returns "approve" here -- and turning it red is a fleet decision
    # across the pilot consumers and the wrapper's lockstep, not one to
    # make as a side effect of naming the reason honestly.
    no_artifact_bypass = (
        _artifacts_entirely_absent(reviews)
        and _all_reviewer_jobs_succeeded(len(REVIEWERS))
        and verdict == "approve"
    )
    # Computed before the emit, not just before the exit check below, so the
    # roster and the exit code read the same flags. A round sequential_bypass
    # lets through ends green sub-quorum by design, and the reason string is
    # the only place that can say so.
    write_reviewer_roster(
        available,
        _missing_reviewer_reasons(
            reviews,
            available,
            conclusions,
            sequential_bypass=sequential_bypass,
        ),
    )
    comment = format_summary(
        reviews,
        verdict,
        reason,
        available,
        conclusions,
        comment_only=comment_only,
        approve_quorum=approve_quorum,
        sequential_bypass=sequential_bypass,
    )
    post_verdict(
        comment,
        verdict,
        comment_only=comment_only,
        approve_quorum=approve_quorum,
    )
    print(f"Final verdict: {verdict} -- {reason}")

    if (
        len(available) < MIN_REVIEWERS_FOR_VERDICT
        and not sequential_bypass
        and not no_artifact_bypass
    ):
        print("ERROR: Insufficient LLM responses -- failing CI", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
