#!/usr/bin/env python3
"""Aggregate multi-LLM review results and post rule-based verdict.

Reads review-claude.json, review-codex.json, review-gemini.json,
applies severity-based rules, and posts a consolidated PR review.

Inline comments are posted by each reviewer job -- this script
handles only the summary verdict.
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import sys
from collections import Counter
from pathlib import Path
from typing import Any

from github_pr_support import (
    REVIEWER_NAMES,
    SEVERITY_ICONS,
    GH_TIMEOUT_SEC,
    fetch_paginated_nodes,
)

logger = logging.getLogger(__name__)

REVIEWERS: dict[str, str] = {name: f"review-{name}.json" for name in REVIEWER_NAMES}

# Max chars for issue description in verdict reason string.
# Keeps the PR review title concise while showing enough context.
DESC_TRUNCATE_LEN = 80
ERROR_TRUNCATE_LEN = 60
MIN_REVIEWERS_FOR_VERDICT = 2
_REVIEW_MARKER = "<!-- multi-llm-review -->"
_DISMISS_MESSAGE = "Superseded by new review"
_REVIEW_MODE_SEQUENTIAL = "sequential"
_REVIEW_MODE_PARALLEL = "parallel"


def _int_env(name: str, default: int) -> int:
    raw = os.environ.get(name, str(default))
    try:
        return int(raw)
    except ValueError:
        print(f"Invalid {name}={raw!r}, using default {default}", file=sys.stderr)
        return default


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
            _int_env("DEPENDABOT_CRITICAL_THRESHOLD", 2),
            float(os.environ.get("DEPENDABOT_MAJOR_CONSENSUS_OVERLAP", "0.5")),
        )
    return (
        _int_env("CRITICAL_THRESHOLD", 1),
        float(os.environ.get("MAJOR_CONSENSUS_OVERLAP", "0.3")),
    )


CRITICAL_THRESHOLD, MAJOR_CONSENSUS_OVERLAP = _resolve_thresholds(
    _PR_AUTHOR == "dependabot[bot]"
)

MAJOR_CONSENSUS_MIN = _int_env("MAJOR_CONSENSUS_MIN", 2)
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
    reviews: dict[str, dict[str, Any] | None],
) -> dict[str, dict[str, Any]]:
    """Filter reviews to only those with valid responses.

    Partial-failure reviews (both 'error' and 'issues' present) are
    included so their issues still contribute to verdict calculation.
    Reviews with errors but no issues are excluded entirely.
    """
    return {
        k: v
        for k, v in reviews.items()
        if v is not None
        and "summary" in v  # must have summary key (rejects empty {})
        # Allow reviews with non-fatal errors if they still produced issues
        and not (v.get("error") and not v.get("issues"))
    }


_SEVERITY_ALIASES: dict[str, str] = {
    "high": "major",
    "medium": "minor",
    "low": "suggestion",
    "info": "suggestion",
    "warning": "minor",
    "note": "suggestion",
    "error": "major",
}


def _normalize_severity(data: Any) -> None:
    """Normalize non-standard severity values in-place before validation.

    Only maps explicitly supported aliases. Unknown values are left
    untouched so ``_is_valid_review`` rejects the payload.
    """
    if not isinstance(data, dict) or not isinstance(data.get("issues"), list):
        return
    for issue in data["issues"]:
        if not isinstance(issue, dict):
            continue
        sev = issue.get("severity")
        if not isinstance(sev, str):
            continue
        lowered = sev.lower()
        if lowered in SEVERITY_ICONS:
            issue["severity"] = lowered
        elif lowered in _SEVERITY_ALIASES:
            issue["severity"] = _SEVERITY_ALIASES[lowered]
        else:
            logger.warning("Unknown severity %r, leaving as-is", sev)


def _is_valid_review(data: Any) -> bool:
    """Check that a review payload has the required shape."""
    if not isinstance(data, dict):
        return False
    if not isinstance(data.get("summary"), str):
        return False
    if not isinstance(data.get("early_exit"), bool):
        return False
    if not isinstance(data.get("issues"), list):
        return False
    for issue in data["issues"]:
        if not isinstance(issue, dict):
            return False
        if issue.get("severity") not in SEVERITY_ICONS:
            return False
        if not isinstance(issue.get("description"), str):
            return False
        for key in ("file", "line", "suggestion"):
            if key not in issue:
                return False
        if issue["file"] is not None and not isinstance(issue["file"], str):
            return False
        if issue["line"] is not None and not isinstance(issue["line"], int):
            return False
        if issue["suggestion"] is not None and not isinstance(issue["suggestion"], str):
            return False
    return True


def load_reviews() -> dict[str, dict[str, Any] | None]:
    reviews: dict[str, dict[str, Any] | None] = {}
    for name, filename in REVIEWERS.items():
        path = Path(filename)
        if path.exists():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                _normalize_severity(data)
                if _is_valid_review(data):
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
    """Check if any reviewer triggered early_exit."""
    return any(v.get("early_exit") is True for v in available.values())


_PARTIAL_SUMMARY_PREFIX = "partial:"


def _is_partial(review: dict[str, Any] | None) -> bool:
    """Return True if the reviewer hit a partial failure.

    Partial means: missing payload, an ``error`` field, or a summary that
    starts with ``partial:`` (case-insensitive). These signals are emitted
    by ``review_gemini.py`` / ``review_codex.py`` / ``review_claude.py``
    when the underlying API call raised or returned a truncated response.
    """
    if review is None:
        return True
    if review.get("error"):
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
    reviews: dict[str, dict[str, Any] | None],
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

    Indicates a benign trivial-diff early-exit, not an infrastructure failure.
    """
    conclusions = load_reviewer_conclusions()
    return len(conclusions) == total and all(
        v == "success" for v in conclusions.values()
    )


def _check_insufficient(
    available: dict[str, dict[str, Any]],
    total: int,
) -> tuple[str, str] | None:
    if len(available) < MIN_REVIEWERS_FOR_VERDICT:
        # Only bypass minimum-reviewer check in sequential mode where
        # early_exit intentionally skips subsequent reviewers.
        review_mode = os.environ.get("REVIEW_MODE", _REVIEW_MODE_PARALLEL)
        if review_mode == _REVIEW_MODE_SEQUENTIAL and _has_early_exit(available):
            return None
        if len(available) == 0:
            if _all_reviewer_jobs_succeeded(total):
                return (
                    "approve",
                    f"0/{total} LLM responses -- all early-exit (benign skip)",
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


def apply_verdict_rules(
    reviews: dict[str, dict[str, Any] | None],
) -> tuple[str, str, dict[str, dict[str, Any]]]:
    """Apply severity-based verdict rules. Returns (verdict, reason, available)."""
    available = _get_available(reviews)
    total = len(REVIEWERS)

    result = _check_insufficient(available, total)
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


def format_summary(
    reviews: dict[str, dict[str, Any] | None],
    verdict: str,
    reason: str,
    available: dict[str, dict[str, Any]],
    conclusions: dict[str, str] | None = None,
    *,
    comment_only: bool = False,
) -> str:
    if verdict == "approve" and comment_only:
        icon, label = "[OK]", "Aggregate verdict (comment only -- auto-approve disabled)"
    elif verdict == "approve":
        icon, label = "[OK]", "Approved"
    elif verdict == "request_changes" and comment_only:
        icon, label = "[!]", "Changes recommended (comment only -- auto-approve disabled)"
    elif verdict == "request_changes":
        icon, label = "[X]", "Changes Requested"
    else:
        icon, label = "[!]", "Comment Only"

    # Append per-reviewer reason annotations for any missing verdicts.
    missing_notes: list[str] = []
    if conclusions:
        for name in REVIEWERS:
            if reviews.get(name) is None or "summary" not in (reviews.get(name) or {}):
                conclusion = conclusions.get(name, "")
                if conclusion and conclusion != "skipped":
                    missing_notes.append(f"{name}: {_missing_reason(conclusion)}")
    reason_suffix = f" ({', '.join(missing_notes)})" if missing_notes else ""

    lines = [
        _REVIEW_MARKER,
        "## [bot] Multi-LLM Review Summary",
        "",
        f"**Result: {icon} {label}** -- {reason}{reason_suffix}",
    ]
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
            na_label = (
                f"[ ] N/A -- {_missing_reason(conclusion)}" if conclusion else "[ ] N/A"
            )
            lines += ["", f"### {name.title()} -- {na_label}"]
            if err_msg:
                lines.append(f"_{err_msg}_")
            lines.append("")
            continue

        issues = review.get("issues", [])
        summary = review.get("summary", "")
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

    for node in fetch_paginated_nodes(
        _STALE_COMMENTS_QUERY,
        "comments",
        owner,
        name,
        pr_number,
        page_size=_STALE_PAGE_SIZE,
    ):
        if (
            node.get("author", {}).get("login") == BOT_LOGIN
            and not node.get("isMinimized")
            and _REVIEW_MARKER in node.get("body", "")
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
            node.get("author", {}).get("login") == BOT_LOGIN
            and node.get("state") == "CHANGES_REQUESTED"
            and _REVIEW_MARKER in node.get("body", "")
        ):
            _run_gql_mutation(_DISMISS_QUERY, node["id"], "dismiss")
            _run_gql_mutation(_MINIMIZE_QUERY, node["id"], "minimize")


def post_verdict(comment: str, verdict: str, *, comment_only: bool) -> None:
    pr_number = os.environ.get("PR_NUMBER", "")
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    if not pr_number or not pr_number.isdigit():
        print(f"PR_NUMBER missing or invalid: {pr_number!r}", file=sys.stderr)
        sys.exit(1)

    _minimize_stale_bot_items(pr_number, repo)

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
    comment = format_summary(
        reviews, verdict, reason, available, conclusions, comment_only=comment_only
    )
    post_verdict(comment, verdict, comment_only=comment_only)
    print(f"Final verdict: {verdict} -- {reason}")

    review_mode = os.environ.get("REVIEW_MODE", _REVIEW_MODE_PARALLEL)
    sequential_bypass = review_mode == _REVIEW_MODE_SEQUENTIAL and _has_early_exit(
        available
    )
    # Parallel mode: all jobs succeeded but produced no payload (benign trivial-diff
    # early-exit). The verdict was already posted as "approve" -- do not fail CI.
    parallel_benign_bypass = (
        _all_reviewer_jobs_succeeded(len(REVIEWERS)) and verdict == "approve"
    )
    if (
        len(available) < MIN_REVIEWERS_FOR_VERDICT
        and not sequential_bypass
        and not parallel_benign_bypass
    ):
        print("ERROR: Insufficient LLM responses -- failing CI", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
