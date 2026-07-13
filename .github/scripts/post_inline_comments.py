#!/usr/bin/env python3
"""Post inline PR review comments from a review JSON file.

Parses pr.diff to validate line numbers against the actual diff hunks,
then posts inline comments via the GitHub PR Reviews API.
Falls back to a plain PR comment if the API call fails.

Dedup strategy (fuzzy):
  1. Fetch ALL existing threads (resolved + unresolved)
  2. Normalise each thread's first comment body via _normalize_body
  3. For each new issue, check whether a thread on the same file exists
     whose normalised body has Jaccard token-set similarity
     >= DEDUP_STRONG_JACCARD (duplicate regardless of line distance --
     force-push/rebase can shift lines far beyond any window), or
     >= _JACCARD_THRESHOLD when within DEDUP_LINE_WINDOW lines. This
     survives both force-push line shifts and small wording changes
     (paraphrases, added punctuation, reworded suggestions).
  4. New issues are also checked against the CURRENT batch (same
     _is_duplicate semantics), so one reviewer emitting the same finding
     twice in a round posts once -- at the highest severity seen.

Issues outside the diff range are not posted -- they appear in aggregate summary.

Round-cutoff convergence backstop (RC-5): at review round ROUND_CUTOFF_N
(default 5) or later, when THIS reviewer's findings contain no
critical/major issue, minor/suggestion findings are folded into a single
summary comment instead of individual inline threads. No auto-merge and
no auto-ticket creation -- a human decides merge timing and follow-up.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

from github_pr_support import (
    DIFF_FILE_PREFIX,
    DIFF_FILE_PREFIX_LEN,
    DIFF_SIDE_RIGHT,
    REVIEW_MARKER,
    REVIEWER_NAMES,
    SEVERITY_ICONS,
    GH_TIMEOUT_SEC,
    fetch_paginated_nodes,
    get_pr_head_sha,
    int_env,
)

# Page size per GraphQL request for thread pagination.
THREAD_PAGE_SIZE = 100
_FALLBACK_HEADER = "## [bot] {} Inline Review (fallback)"

# Round-cutoff convergence backstop (RC-5, issue #37). Tunable via the
# ROUND_CUTOFF_N env var; ROUND_CUTOFF_ENABLED=false disables the gate.
DEFAULT_ROUND_CUTOFF_N = 5
_CUTOFF_MARKER = "<!-- round-cutoff-{reviewer}-r{round} -->"
_CUTOFF_HEADER = "## [bot] {} Round Cutoff Summary (R{})"
_CUTOFF_LEAD = (
    "R{} convergence cutoff -- the following minor items are recommended for follow-up:"
)

# A completed round leaves exactly one aggregate verdict post carrying
# REVIEW_MARKER: a PR review when auto-approve is enabled, or an issue
# comment in comment-only mode (the default killswitch state).
_ROUND_COUNT_ENDPOINTS = (
    "repos/{repo}/pulls/{pr}/reviews",
    "repos/{repo}/issues/{pr}/comments",
)

# Fuzzy-dedup thresholds. Two comments on the same path are considered
# duplicates when their normalised token sets overlap by at least
# _JACCARD_THRESHOLD and their right-side line numbers are within
# DEDUP_LINE_WINDOW lines of each other. The Jaccard threshold is
# tunable at runtime via the JACCARD_THRESHOLD environment variable
# (typically 0.5-0.8: lower dedups more aggressively, higher is stricter).
_DEFAULT_JACCARD_THRESHOLD = 0.6
_JACCARD_THRESHOLD = float(
    os.environ.get("JACCARD_THRESHOLD", _DEFAULT_JACCARD_THRESHOLD)
)
DEDUP_LINE_WINDOW = 5
# When similarity is at least this strong, the line-distance check is
# skipped entirely: force-push/rebase shifts line numbers far beyond any
# sensible window (observed delta of 109 lines), so near-identical text on
# the same file is a duplicate no matter how far it moved. Tunable at
# runtime via the DEDUP_STRONG_JACCARD environment variable.
_DEFAULT_STRONG_JACCARD = 0.8
DEDUP_STRONG_JACCARD = float(
    os.environ.get("DEDUP_STRONG_JACCARD", _DEFAULT_STRONG_JACCARD)
)

# Severity precedence for batch-internal dedup (higher rank wins when the
# same finding is emitted at two severities in one round). Order matches
# the review-schema severity enum.
_SEVERITY_RANK: dict[str, int] = {
    sev: rank for rank, sev in enumerate(("suggestion", "minor", "major", "critical"))
}

# Pre-compiled patterns for _normalize_body
_ICON_RE = re.compile("[" + "".join(SEVERITY_ICONS.values()) + "*]")
_BOLD_RE = re.compile(r"\*\*.*?\*\*")
_REVIEWER_RE = re.compile(
    rf"\((?:{'|'.join(re.escape(r) for r in REVIEWER_NAMES)})\)", re.IGNORECASE
)
_BLOCKQUOTE_RE = re.compile(r"^>.*$", re.MULTILINE)
_LEADING_COLON_RE = re.compile(r"^\s*:\s*")


def parse_diff(diff_text: str) -> dict[str, set[int]]:
    """Parse unified diff to extract valid right-side line numbers per file."""
    valid: dict[str, set[int]] = {}
    current_file: str | None = None
    right_line = 0

    for line in diff_text.splitlines():
        if line.startswith("\\"):
            continue  # "\ No newline at end of file"
        if line.startswith(DIFF_FILE_PREFIX):
            # Take only the path; unified diff may append tab+timestamp after it.
            raw_path = line[DIFF_FILE_PREFIX_LEN:]
            current_file = raw_path.split("\t")[0] if raw_path else raw_path
            right_line = 0
            valid.setdefault(current_file, set())
        elif line.startswith("@@ "):
            match = re.search(r"\+(\d+)", line)
            if match:
                right_line = int(match.group(1))
        elif current_file is not None:
            if line.startswith("+") or line.startswith(" "):
                valid[current_file].add(right_line)
                right_line += 1
            elif line.startswith("-"):
                pass  # deleted line, no right-side position
            elif line.startswith("diff --git"):
                current_file = None

    return valid


def _normalize_body(text: str) -> str:
    """Normalize review comment body for exact-match dedup.

    Strips generated prefixes (icons, severity, reviewer tag, leading colon)
    so that thread bodies and raw descriptions produce the same output.
    """
    text = _ICON_RE.sub("", text)
    text = _BOLD_RE.sub("", text)
    text = _REVIEWER_RE.sub("", text)
    text = _BLOCKQUOTE_RE.sub("", text)
    text = _LEADING_COLON_RE.sub("", text)
    return " ".join(re.findall(r"\w+", text.lower()))


_THREADS_QUERY = """
query($owner: String!, $name: String!, $pr: Int!,
      $first: Int!, $after: String) {
  repository(owner: $owner, name: $name) {
    pullRequest(number: $pr) {
      reviewThreads(first: $first, after: $after) {
        pageInfo { hasNextPage endCursor }
        nodes {
          path
          line
          comments(first: 1) { nodes { body } }
        }
      }
    }
  }
}
"""


def _parse_thread_nodes(
    nodes: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Convert raw GraphQL thread nodes into dedup-ready dicts."""
    threads: list[dict[str, Any]] = []
    for node in nodes:
        body = ""
        comments = node.get("comments", {}).get("nodes", [])
        if comments:
            body = comments[0].get("body") or ""
        threads.append(
            {
                "path": node.get("path", ""),
                "line": node.get("line"),
                "body": _normalize_body(body),
            }
        )
    return threads


def fetch_existing_threads(
    repo: str,
    pr_number: str,
) -> list[dict[str, Any]]:
    """Fetch all review threads with cursor pagination for dedup."""
    parts = repo.split("/", 1)
    if len(parts) != 2:
        print(f"Invalid GITHUB_REPOSITORY format: {repo}", file=sys.stderr)
        return []
    owner, name = parts
    return fetch_paginated_nodes(
        query=_THREADS_QUERY,
        field="reviewThreads",
        owner=owner,
        name=name,
        pr_number=pr_number,
        page_size=THREAD_PAGE_SIZE,
        transform=_parse_thread_nodes,
    )


def _is_duplicate(
    file_path: str,
    description: str | None,
    existing_threads: list[dict[str, Any]],
    line: int | None = None,
    threshold: float = _JACCARD_THRESHOLD,
    line_window: int = DEDUP_LINE_WINDOW,
    strong_threshold: float = DEDUP_STRONG_JACCARD,
) -> bool:
    """Check if a fuzzy duplicate exists.

    A thread on the same file is considered a duplicate when either:

    * its normalised body has Jaccard token-set similarity
      >= ``strong_threshold`` with the new description -- line distance
      is ignored because force-push/rebase moves lines far beyond any
      window while the finding text stays the same; or
    * its line is within ``line_window`` of ``line`` (when both lines
      are known) and the similarity is >= ``threshold``.

    ``line=None`` skips the line-distance check, preserving backward
    compatibility for callers that lack a right-side line number.
    """
    normalized = _normalize_body(description or "")
    tokens = set(normalized.split())
    if not tokens:
        return False
    for thread in existing_threads:
        if thread["path"] != file_path:
            continue
        other_tokens = set((thread.get("body") or "").split())
        if not other_tokens:
            continue
        union = tokens | other_tokens
        if not union:
            continue
        jaccard = len(tokens & other_tokens) / len(union)
        if jaccard >= strong_threshold:
            return True
        other_line = thread.get("line")
        if line is not None and other_line is not None:
            if abs(line - other_line) > line_window:
                continue
        if jaccard >= threshold:
            return True
    return False


def build_comments(
    issues: list[dict[str, Any]],
    valid_lines: dict[str, set[int]],
    existing_threads: list[dict[str, Any]],
    reviewer: str,
) -> tuple[list[dict[str, Any]], int, int]:
    """Filter issues and build comment payloads.

    Besides deduping against existing threads, each issue is checked
    against the comments already accepted in this batch (same
    ``_is_duplicate`` semantics): a batch-internal duplicate replaces the
    earlier comment when its severity is higher and is skipped otherwise.

    Returns (comments, no_location, out_of_range).
    """
    comments: list[dict[str, Any]] = []
    # Dedup-shaped mirror of `comments` (path/line/normalised body plus
    # severity) so batch entries can feed _is_duplicate directly.
    batch_entries: list[dict[str, Any]] = []
    no_location = 0
    out_of_range = 0

    for issue in issues:
        file_path = issue.get("file")
        raw_line = issue.get("line")
        if not file_path or raw_line is None:
            no_location += 1
            continue
        try:
            line_num = int(raw_line)
        except (ValueError, TypeError):
            no_location += 1
            continue
        if file_path not in valid_lines or line_num not in valid_lines[file_path]:
            out_of_range += 1
            continue
        desc = issue.get("description", "")
        if _is_duplicate(file_path, desc, existing_threads, line=line_num):
            print(f"{reviewer}: skip {file_path}:{line_num} (similar thread exists)")
            continue

        sev = issue.get("severity", "suggestion")
        dup_idx = next(
            (
                i
                for i, entry in enumerate(batch_entries)
                if _is_duplicate(file_path, desc, [entry], line=line_num)
            ),
            None,
        )
        if dup_idx is not None and _SEVERITY_RANK.get(sev, 0) <= _SEVERITY_RANK.get(
            batch_entries[dup_idx]["severity"], 0
        ):
            print(f"{reviewer}: skip {file_path}:{line_num} (duplicate in batch)")
            continue

        icon = SEVERITY_ICONS.get(sev, "*")
        body = f"{icon} **{sev}** ({reviewer}): {desc}"
        if issue.get("suggestion"):
            body += f"\n\n> {SEVERITY_ICONS['suggestion']} {issue['suggestion']}"
        comment = {
            "path": file_path,
            "line": line_num,
            "side": DIFF_SIDE_RIGHT,
            "body": body,
        }
        entry = {
            "path": file_path,
            "line": line_num,
            "body": _normalize_body(desc or ""),
            "severity": sev,
        }
        if dup_idx is not None:
            dup = comments[dup_idx]
            print(
                f"{reviewer}: replace {dup['path']}:{dup['line']} "
                f"(higher-severity duplicate in batch)"
            )
            comments[dup_idx] = comment
            batch_entries[dup_idx] = entry
        else:
            comments.append(comment)
            batch_entries.append(entry)

    return comments, no_location, out_of_range


def fetch_round_count(repo: str, pr_number: str) -> int:
    """Count completed review rounds on the PR.

    Every completed round ends with exactly one bot-authored aggregate
    verdict post carrying ``REVIEW_MARKER`` (see _ROUND_COUNT_ENDPOINTS).
    Counting marker posts rather than raw bot reviews avoids overcounting:
    each single reviewer job also submits a COMMENT review per round for
    its inline comment batch.

    Fails open: a gh error yields a partial (possibly zero) count so the
    cutoff never suppresses findings because of an API failure.
    """
    jq_filter = (
        'map(select((.user.type // "") == "Bot"'
        f' and ((.body // "") | contains("{REVIEW_MARKER}")))) | length'
    )
    total = 0
    for template in _ROUND_COUNT_ENDPOINTS:
        endpoint = template.format(repo=repo, pr=pr_number)
        try:
            result = subprocess.run(
                ["gh", "api", "--paginate", endpoint, "--jq", jq_filter],
                capture_output=True,
                text=True,
                timeout=GH_TIMEOUT_SEC,
            )
        except subprocess.TimeoutExpired:
            print(f"Warning: round count timed out for {endpoint}", file=sys.stderr)
            continue
        if result.returncode != 0:
            print(
                f"Warning: round count failed for {endpoint}: {result.stderr.strip()}",
                file=sys.stderr,
            )
            continue
        # --paginate emits one jq result per page; sum them.
        for token in result.stdout.split():
            try:
                total += int(token)
            except ValueError:
                print(
                    f"Warning: unexpected round count output: {token!r}",
                    file=sys.stderr,
                )
    return total


def round_cutoff_round(
    repo: str,
    pr_number: str,
    issues: list[dict[str, Any]],
) -> int | None:
    """Return the current round number when the convergence cutoff applies.

    The cutoff applies when it is enabled, every finding in this
    reviewer's payload is non-blocking (minor/suggestion -- unknown
    severities count as blocking so unexpected payloads fail open to
    normal posting), and the current round number (completed rounds + 1)
    has reached ``ROUND_CUTOFF_N``.
    """
    enabled = os.environ.get("ROUND_CUTOFF_ENABLED", "true").strip().lower()
    if enabled == "false":
        return None
    for issue in issues:
        rank = _SEVERITY_RANK.get(str(issue.get("severity", "")).lower())
        if rank is None or rank >= _SEVERITY_RANK["major"]:
            return None
    cutoff_n = int_env("ROUND_CUTOFF_N", DEFAULT_ROUND_CUTOFF_N)
    round_number = fetch_round_count(repo, pr_number) + 1
    if round_number >= cutoff_n:
        return round_number
    return None


def post_inline_review(
    repo: str,
    pr_number: str,
    commit_sha: str,
    reviewer: str,
    comments: list[dict[str, Any]],
) -> bool:
    """Post inline comments via Reviews API. Returns True on success."""
    payload = json.dumps(
        {
            "commit_id": commit_sha,
            "event": "COMMENT",
            "body": "",
            "comments": comments,
        }
    )
    try:
        result = subprocess.run(
            ["gh", "api", f"repos/{repo}/pulls/{pr_number}/reviews", "--input", "-"],
            input=payload,
            capture_output=True,
            text=True,
            timeout=GH_TIMEOUT_SEC,
        )
    except subprocess.TimeoutExpired:
        print(f"{reviewer}: inline API timed out", file=sys.stderr)
        return False
    if result.returncode == 0:
        print(f"{reviewer}: posted {len(comments)} inline comment(s)")
        return True
    print(f"{reviewer}: inline API failed ({result.stderr.strip()})", file=sys.stderr)
    return False


def _post_folded_comment(
    repo: str,
    pr_number: str,
    reviewer: str,
    marker: str,
    header_lines: list[str],
    comments: list[dict[str, Any]],
    label: str,
) -> None:
    """Post all comment payloads as a single PR comment.

    Uses a marker comment to prevent duplicate posts on reruns.
    """
    check = subprocess.run(
        [
            "gh",
            "api",
            f"repos/{repo}/issues/{pr_number}/comments",
            "--jq",
            "[.[] | select(.body | contains($marker))] | length",
            "--arg",
            "marker",
            marker,
        ],
        capture_output=True,
        text=True,
        timeout=GH_TIMEOUT_SEC,
    )
    if check.returncode == 0 and check.stdout.strip() not in ("", "0"):
        print(f"{reviewer}: {label} comment already exists, skipping")
        return

    lines = [marker, *header_lines, ""]
    for c in comments:
        lines.append(f"- **{c.get('path')}:{c.get('line')}** -- {c.get('body', '')}")
    body = "\n".join(lines)
    try:
        result = subprocess.run(
            [
                "gh",
                "pr",
                "comment",
                pr_number,
                "--repo",
                repo,
                "--body-file",
                "-",
            ],
            input=body,
            capture_output=True,
            text=True,
            timeout=GH_TIMEOUT_SEC,
        )
    except subprocess.TimeoutExpired:
        print(f"{reviewer}: {label} comment timed out", file=sys.stderr)
        sys.exit(1)
    if result.returncode != 0:
        print(f"{reviewer}: {label} comment failed: {result.stderr}", file=sys.stderr)
        sys.exit(1)


def post_fallback(
    repo: str, pr_number: str, reviewer: str, comments: list[dict[str, Any]]
) -> None:
    """Fallback: post all issues as a single PR comment."""
    _post_folded_comment(
        repo,
        pr_number,
        reviewer,
        f"<!-- inline-fallback-{reviewer} -->",
        [_FALLBACK_HEADER.format(reviewer)],
        comments,
        "fallback",
    )


def post_cutoff_summary(
    repo: str,
    pr_number: str,
    reviewer: str,
    round_number: int,
    comments: list[dict[str, Any]],
) -> None:
    """Fold suppressed non-blocking findings into one summary comment.

    The round-scoped marker prevents duplicate summaries on reruns while
    still allowing one summary per later round.
    """
    _post_folded_comment(
        repo,
        pr_number,
        reviewer,
        _CUTOFF_MARKER.format(reviewer=reviewer, round=round_number),
        [
            _CUTOFF_HEADER.format(reviewer, round_number),
            "",
            _CUTOFF_LEAD.format(round_number),
        ],
        comments,
        "cutoff summary",
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--issues", required=True, help="Path to review JSON file")
    parser.add_argument("--diff", required=True, help="Path to pr.diff file")
    parser.add_argument("--reviewer", required=True, help="Reviewer name")
    args = parser.parse_args()

    pr_number = os.environ.get("PR_NUMBER", "")
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    if not pr_number or not repo:
        print("PR_NUMBER or GITHUB_REPOSITORY not set", file=sys.stderr)
        sys.exit(1)
    if not pr_number.isdigit():
        print(f"Invalid PR_NUMBER: {pr_number}", file=sys.stderr)
        sys.exit(1)

    try:
        with open(args.issues, encoding="utf-8") as f:
            review = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError) as e:
        print(f"Cannot load {args.issues}: {e}", file=sys.stderr)
        sys.exit(1)

    issues = review.get("issues", [])
    if not issues:
        print(f"{args.reviewer}: no issues to post")
        return

    diff_path = Path(args.diff)
    if not diff_path.exists():
        print(f"Warning: diff file not found: {args.diff}", file=sys.stderr)
    # ``errors="replace"`` keeps validation alive when the PR diff carries
    # non-UTF-8 bytes; we only need hunk headers, which are ASCII.
    diff_text = (
        diff_path.read_text(encoding="utf-8", errors="replace")
        if diff_path.exists()
        else ""
    )
    valid_lines = parse_diff(diff_text)
    existing_threads = fetch_existing_threads(repo, pr_number)

    comments, no_location, out_of_range = build_comments(
        issues, valid_lines, existing_threads, args.reviewer
    )

    if no_location:
        print(f"{args.reviewer}: {no_location} issue(s) without file/line")
    if out_of_range:
        print(f"{args.reviewer}: {out_of_range} issue(s) outside diff")

    if not comments:
        print(f"{args.reviewer}: no inline comments to post")
        return

    cutoff_round = round_cutoff_round(repo, pr_number, issues)
    if cutoff_round is not None:
        print(
            f"{args.reviewer}: round cutoff active (round {cutoff_round}) --"
            f" folding {len(comments)} finding(s) into a summary comment"
        )
        post_cutoff_summary(repo, pr_number, args.reviewer, cutoff_round, comments)
        return

    try:
        commit_sha = get_pr_head_sha(pr_number)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as e:
        print(f"{args.reviewer}: failed to get PR head SHA: {e}", file=sys.stderr)
        post_fallback(repo, pr_number, args.reviewer, comments)
        return
    if not post_inline_review(repo, pr_number, commit_sha, args.reviewer, comments):
        post_fallback(repo, pr_number, args.reviewer, comments)


if __name__ == "__main__":
    main()
