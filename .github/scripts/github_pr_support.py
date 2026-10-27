"""Review context: shared constants and utilities for multi-LLM review scripts."""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import sys
from collections.abc import Callable, Iterable
from typing import Any

logger = logging.getLogger(__name__)

REVIEWER_NAMES: tuple[str, ...] = ("claude", "codex", "gemini")
GH_TIMEOUT_SEC = 60
_DEFAULT_PAGE_SIZE = 50

# Marker embedded in every aggregate verdict post (PR review or comment).
# Shared so post_inline_comments can count completed review rounds by
# looking for the exact same string the aggregate script emits.
REVIEW_MARKER = "<!-- multi-llm-review -->"

# The PR size gate's vocabulary (AT-3052), one definition for the aggregate
# and the local driver. base-ai-review-prepare.yml's `Check PR size` spells
# the same words in bash; test_prepare_size_check pins the two together.
COMBINED_SIZE_VAR = "PR_SIZE_LIMIT"
ADDITIONS_SIZE_VAR = "PR_SIZE_LIMIT_ADDITIONS"
DELETIONS_SIZE_VAR = "PR_SIZE_LIMIT_DELETIONS"
COMBINED_SIZE_AXIS = "changed"
ADDITIONS_SIZE_AXIS = "added"
DELETIONS_SIZE_AXIS = "deleted"


def describe_size_breach(value: object, axis: str, var: str, limit: object) -> str:
    """`{value} {axis} lines exceeds {VAR} {limit}` -- one exceeded size limit."""
    return f"{value} {axis} lines exceeds {var} {limit}"


# Approaching the limit (AT-2415): a check at or over this percentage of its
# limit, and not over the limit, is warned about. Same bash/Python pinning as
# above.
SIZE_WARN_PERCENT_VAR = "PR_SIZE_WARN_PERCENT"
# The workflow's `vars.PR_SIZE_WARN_PERCENT || '80'`; test_local_review_config
# pins the two together.
SIZE_WARN_PERCENT_DEFAULT = 80
SIZE_WARN_PERCENT_MIN = 1
SIZE_WARN_PERCENT_MAX = 99


def size_warn_reached(value: int, limit: int, percent: int) -> bool:
    """True when `value` is within the limit but at or over `percent` of it.

    A zero limit is never warned: nothing is under it to approach it.
    """
    return 0 < limit and value <= limit and value * 100 >= limit * percent


def describe_size_warning(value: int, axis: str, var: str, limit: int) -> str:
    """The one-line notice for a check approaching its limit."""
    return (
        f"[!] size: {value}/{limit} {axis} lines ({value * 100 // limit}% of {var});"
        " the review is skipped above the limit. Consider splitting."
    )


def describe_size_overage(breaches: list[tuple[int, str, int]]) -> str:
    """`over by N {axis} lines[ and ...]; split into at least K PRs`.

    `breaches` is (value, axis, limit) per exceeded check. K is the largest
    ceil(value / limit) across them, the fewest parts that bring every axis
    under its limit if lines split evenly; it is left out when a limit is 0,
    which no number of parts can satisfy.
    """
    overage = " and ".join(
        f"{value - limit} {axis} lines" for value, axis, limit in breaches
    )
    text = f"over by {overage}"
    if breaches and all(limit > 0 for _, _, limit in breaches):
        parts = max(-(-value // limit) for value, _, limit in breaches)
        text += f"; split into at least {parts} PRs"
    return text


# The fold comments: when post_inline_comments puts a reviewer's findings in
# ONE PR comment instead of inline threads -- the round-cutoff summary, or the
# inline fallback when no inline post could be made -- the comment opens with
# one of these markers, bound to the reviewer and the round, and carries the
# count below. The aggregate reads the count back from the comment, by author
# and marker, because the comment is the one carrier the PR cannot author
# (AT-2553): the verdict file is the reviewer's own JSON, written over PR
# content, and anything recorded there can be planted there.
#
# Not named for the round cutoff: that is one of the two routes, not the
# category. The round in the marker is what keeps a rerun from posting twice
# and an earlier round's comment from being read as this one's.
FOLD_COMMENT_MARKERS: tuple[str, ...] = (
    "<!-- round-cutoff-{reviewer}-r{round} -->",
    "<!-- inline-fallback-{reviewer}-r{round} -->",
)
_INLINE_POST_MARKER = re.compile(
    rf"<!-- inline-post-(?P<reviewer>{'|'.join(REVIEWER_NAMES)})-r(?P<round>[1-9][0-9]*) "
    r"run:(?P<run>actions-[1-9][0-9]*-[1-9][0-9]*|local-[0-9a-f]{32}) -->"
)


def inline_post_envelope(body: str) -> tuple[str, int, str, str] | None:
    """Read the trusted first-line posting marker, never description text."""
    first, separator, rest = body.partition("\n")
    match = _INLINE_POST_MARKER.fullmatch(first.rstrip("\r"))
    if not separator or match is None:
        return None
    try:
        round_number = int(match.group("round"))
    except ValueError:
        return None
    return match.group("reviewer"), round_number, match.group("run"), rest


def inline_post_marker(reviewer: str, round_number: int, run_identity: str) -> str:
    marker = f"<!-- inline-post-{reviewer}-r{round_number} run:{run_identity} -->"
    if _INLINE_POST_MARKER.fullmatch(marker) is None:
        raise ValueError("invalid inline posting identity")
    return marker


def inline_run_identity() -> str:
    """Trusted invocation identity, never a completed-verdict ordinal."""
    local = os.environ.get("INLINE_REVIEW_RUN_ID", "")
    if re.fullmatch(r"local-[0-9a-f]{32}", local):
        return local
    if os.environ.get("GITHUB_ACTIONS") == "true":
        run = os.environ.get("GITHUB_RUN_ID", "")
        attempt = os.environ.get("GITHUB_RUN_ATTEMPT", "")
        if re.fullmatch(r"[1-9][0-9]*", run) and re.fullmatch(r"[1-9][0-9]*", attempt):
            return f"actions-{run}-{attempt}"
        return ""
    return ""


# One line in the comment body, machine-readable. The head is carried when
# the posting step knows it, so the aggregate can refuse a comment left by a
# run on a superseded head; a comment without one binds on the round alone.
_FOLD_COUNT_RE = re.compile(
    r"<!-- fold-count: (?P<count>\d+)(?: head: (?P<head>[0-9a-f]{7,40}))? -->"
)


def fold_count_line(count: int, head: str | None = None) -> str:
    """The line a fold comment carries so the aggregate can read the count."""
    suffix = f" head: {head}" if head else ""
    return f"<!-- fold-count: {count}{suffix} -->"


def fold_envelope(body: str) -> tuple[str, int, str | None] | None:
    """The (marker, count, head) a fold comment's envelope carries, or None.

    An envelope, not a search: the marker is the whole first line and the
    count line the whole second, which is the shape ``_post_folded_comment``
    writes and one no reviewer-authored text can occupy -- every
    bot-authored comment on a PR quotes LLM-authored descriptions verbatim
    (the verdict lists every finding, a fold comment every folded one), so
    a marker or count found anywhere else in a body may have been put
    there through a description (AT-2553, round 10). A comment with the
    marker but no readable count line is one the aggregate cannot use: it
    reads as no count rather than as zero.
    """
    lines = body.split("\n", 2)
    if len(lines) < 2:
        return None
    match = _FOLD_COUNT_RE.fullmatch(lines[1].rstrip("\r"))
    if match is None:
        return None
    return lines[0].rstrip("\r"), int(match.group("count")), match.group("head")


def parse_fold_count(body: str) -> tuple[int, str | None] | None:
    """The (count, head) a fold comment's envelope carries, or None."""
    envelope = fold_envelope(body)
    if envelope is None:
        return None
    return envelope[1], envelope[2]


def fold_markers_for(reviewer: str, round_number: int) -> tuple[str, ...]:
    """The markers a fold comment by ``reviewer`` for ``round_number`` opens with."""
    return tuple(
        marker.format(reviewer=reviewer, round=round_number)
        for marker in FOLD_COMMENT_MARKERS
    )


# A completed round leaves exactly one aggregate verdict post carrying
# REVIEW_MARKER: a PR review when auto-approve is enabled, or an issue
# comment in comment-only mode (the default killswitch state). Both sides
# of the fold contract count rounds the same way, with this one function.
_ROUND_COUNT_ENDPOINTS = (
    "repos/{repo}/pulls/{pr}/reviews",
    "repos/{repo}/issues/{pr}/comments",
)


def fetch_round_count(repo: str, pr_number: str) -> int:
    """Count completed review rounds on the PR.

    Every completed round ends with exactly one aggregate verdict whose
    whole first line is REVIEW_MARKER (CRLF accepted), on either endpoint.
    Keep the Bot policy on Actions. A trusted local invocation additionally
    counts only its exact resolved User operator, never arbitrary Users.
    Counting marker posts rather than raw bot reviews avoids overcounting:
    each single reviewer job also submits a COMMENT review per round for
    its inline comment batch.

    Fails open: a gh error yields a partial (possibly zero) count so the
    cutoff never suppresses findings because of an API failure. On the
    aggregate side the same partial count makes a fold comment's round
    fail to match, which reads as unknown -- closed, there.
    """
    author_filter = '(.user.type // "") == "Bot"'
    operator = os.environ.get("BOT_LOGIN", "")
    if inline_run_identity().startswith("local-") and operator:
        author_filter += f' or ((.user.type // "") == "User" and .user.login == {json.dumps(operator)})'
    jq_filter = (
        f"map(select(({author_filter})"
        f' and (((.body // "") | split("\\n")[0] | rtrimstr("\\r")) == "{REVIEW_MARKER}"))) | length'
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


_APP_LOGIN_PREFIX = "app/"
_BOT_LOGIN_SUFFIX = "[bot]"


def normalize_bot_login(login: str) -> str:
    """Reduce every GitHub spelling of an app's login to the bare app slug.

    fetch_review_context.py calls it to trust the threads BOT_LOGIN opened
    (AT-2520): the threads come from GraphQL, BOT_LOGIN usually arrives in
    the REST spelling. The aggregate's fold count (AT-2553) trusts
    INLINE_BOT_LOGIN; posting-side fold dedup trusts its actual BOT_LOGIN.
    The stale-item fold used to call it too; since AT-2599 it keys on the review marker
    instead of the author.

    The same GitHub App is rendered three ways depending on which surface
    reports it: REST ``.user.login`` says ``github-actions[bot]``, GraphQL
    ``author.login`` says ``github-actions`` (no suffix), and ``gh pr view
    --json author`` says ``app/github-actions``. ``BOT_LOGIN`` defaults to
    the REST spelling while the stale-item pass read authors over GraphQL,
    so a verbatim compare of the two never matched and no prior-round
    aggregate item was ever minimized under the default (AT-2208).

    Both sides of a comparison go through this function rather than the
    default changing to the GraphQL spelling: a consumer that already sets
    ``BOT_LOGIN`` in the REST form keeps working, and a comparison written
    against any of the three surfaces stays correct if the surface changes.

    Case is left alone. GitHub logins are case-insensitive, but every
    surface above reports the canonical casing, and a lowercase step here
    would mean explaining why one comparison folds case when nothing else
    in these scripts does.

    A human login has neither affix and passes through unchanged.
    """
    if login.startswith(_APP_LOGIN_PREFIX):
        login = login[len(_APP_LOGIN_PREFIX) :]
    if login.endswith(_BOT_LOGIN_SUFFIX):
        login = login[: -len(_BOT_LOGIN_SUFFIX)]
    return login


def trusted_post_author(post: dict[str, Any], expected: str) -> bool:
    """Trust Bots, or the local driver's exact raw expected posting operator.

    Expected is writer BOT_LOGIN or aggregate INLINE_BOT_LOGIN, never the
    configurable aggregate verdict author.
    A User account sharing an App's bare slug is not that App.
    """
    if post.get("author_type") == "Bot":
        return normalize_bot_login(post["login"]) == normalize_bot_login(expected)
    return bool(
        post.get("author_type") == "User"
        and inline_run_identity().startswith("local-")
        and post["login"] == expected
    )


def int_env(name: str, default: int) -> int:
    """Read an integer env var, falling back to default on missing/invalid."""
    raw = os.environ.get(name, str(default))
    try:
        return int(raw)
    except ValueError:
        print(f"Invalid {name}={raw!r}, using default {default}", file=sys.stderr)
        return default


def float_env(name: str, default: float) -> float:
    """Read a float env var, falling back to default on missing/invalid."""
    raw = os.environ.get(name, str(default))
    try:
        return float(raw)
    except ValueError:
        print(f"Invalid {name}={raw!r}, using default {default}", file=sys.stderr)
        return default


SEVERITY_ICONS: dict[str, str] = {
    "critical": "!",
    "major": "+",
    "minor": "-",
    "suggestion": "?",
}

# The payload-shape pair, and the gate built from them, live beside
# SEVERITY_ICONS because that is what they read. The shape is still
# aggregate_reviews.py's contract; this module is only the neutral place
# both sides of it can reach. They were in local_reviewer_support until
# AT-2510: that put the Actions-path aggregate on an import of the LOCAL
# driver's support module, and when that module gained a PyYAML
# dependency every consumer's aggregate job died at import (v1.11.0).
_SEVERITY_ALIASES: dict[str, str] = {
    "high": "major",
    "medium": "minor",
    "low": "suggestion",
    "info": "suggestion",
    "warning": "minor",
    "note": "suggestion",
    "error": "major",
}


def normalize_severity(data: Any) -> None:
    """Map reviewer severity spellings onto the canonical set, in place.

    Unknown values are left alone so ``is_valid_review`` rejects the
    payload rather than this silently inventing a severity for it.
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


def is_valid_review(data: Any) -> bool:
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


def usable_verdict(payload: Any) -> bool:
    """Would aggregate_reviews.load_reviews read this payload as a verdict?

    THE AGGREGATE'S OWN SEQUENCE, not a second opinion about it: normalize
    the severities, then apply the shape test, because that is the order
    load_reviews uses and a shim answering only the second half would
    refuse a payload the aggregate accepts -- an "output_unparseable" for a
    review whose only fault was spelling `High`.

    Both local reviewer shims gate their direct-write path on this one
    call, and `isinstance(payload, dict)` -- the gate it replaced --
    accepted any JSON object at all, so a CLI that half-wrote its verdict
    file suppressed the log-extraction fallback that would have recovered
    the real one and the aggregate then saw a malformed review.
    """
    normalize_severity(payload)
    return is_valid_review(payload)


# Stand-in for a backtick in a displayed path: U+02CB MODIFIER LETTER GRAVE
# ACCENT looks like one but is not one to markdown, so the code span around
# the path cannot be closed from inside it. Written as an escape because the
# .github tree is ASCII-only.
_BACKTICK_LOOKALIKE = "\u02cb"
_NAMED_ESCAPES = {"\n": "\\n", "\r": "\\r", "\t": "\\t"}
# Python's str.splitlines() -- which the aggregate uses to read the path list
# back -- also breaks on U+2028 and U+2029, so they are line breaks here too.
_LINE_SEPARATORS = "\u2028\u2029"


def display_path(path: str) -> str:
    """A single-line, code-span-safe rendering of a repository path.

    Paths reach the reviewers' context.md, $GITHUB_OUTPUT (one per line), a
    JSON result and PR comments as `path` inside a markdown code span; a git
    C-quoted name can decode to anything, including a newline or a backtick,
    and would otherwise break out of all four. Every C0 and C1 control
    character, DEL and the Unicode line separators become a visible escape
    (`\\n`, `\\r`, `\\t`, `\\xNN`, `\\u2028`), and a backtick becomes a
    lookalike. The result is a label, deliberately lossy: matching and every
    other decision use the raw path, never this.
    """
    out: list[str] = []
    for ch in path:
        code = ord(ch)
        if ch in _NAMED_ESCAPES:
            out.append(_NAMED_ESCAPES[ch])
        elif code < 0x20 or 0x7F <= code <= 0x9F:
            out.append(f"\\x{code:02x}")
        elif ch in _LINE_SEPARATORS:
            out.append(f"\\u{code:04x}")
        elif ch == "`":
            out.append(_BACKTICK_LOOKALIKE)
        else:
            out.append(ch)
    return "".join(out)


_MAX_LABELS = 20
_MAX_LABEL_NAME_LEN = 50


def format_labels(names: Iterable[str]) -> str:
    """Render a PR's label names for the `## PR Metadata` block (AT-2222).

    Two caps, both defense-in-depth against a maliciously large label set or
    an oversized label name reaching the reviewers' prompt context: at most
    20 labels survive, and any individual name longer than 50 characters is
    truncated to 49 characters plus an ellipsis. GitHub's UI already enforces
    both limits in practice -- a PR cannot carry more than 20 labels through
    the picker, and a label name cannot exceed 50 characters -- so this only
    guards against that enforcement being bypassed or changing.

    Names are sorted first (plain lexicographic order), then capped, so the
    kept set is deterministic rather than dependent on API ordering; the
    dropped overflow is silent, with no "+N more" marker. Each surviving name
    is rendered through `display_path` -- despite the name it is a general
    single-line, code-span-safe string renderer, and reusing it keeps one
    escaping implementation for every string this workflow prints into a
    markdown code span (repo convention: one rule, one home).
    """
    rendered: list[str] = []
    for name in sorted(names)[:_MAX_LABELS]:
        if len(name) > _MAX_LABEL_NAME_LEN:
            name = name[: _MAX_LABEL_NAME_LEN - 1] + "\u2026"
        rendered.append(display_path(name))
    return ", ".join(rendered)


DIFF_FILE_PREFIX = "+++ b/"
DIFF_FILE_PREFIX_LEN = len(DIFF_FILE_PREFIX)
DIFF_SIDE_RIGHT = "RIGHT"


_MAX_FETCH_PAGES = 20


def fetch_paginated_nodes(
    query: str,
    field: str,
    owner: str,
    name: str,
    pr_number: str,
    page_size: int = _DEFAULT_PAGE_SIZE,
    transform: Callable[[list[dict[str, Any]]], list[dict[str, Any]]] | None = None,
) -> list[dict[str, Any]]:
    """Fetch all nodes from a paginated GraphQL query.

    Args:
        query: GraphQL query with $owner, $name, $pr, $first, $after variables.
        field: The pullRequest sub-field to extract (e.g., "reviewThreads").
        owner: Repository owner.
        name: Repository name.
        pr_number: PR number (must be numeric string).
        page_size: Number of items per page.
        transform: Optional function to transform raw nodes before appending.
    """
    if not pr_number.isdigit():
        print(f"Invalid pr_number for GraphQL: {pr_number}", file=sys.stderr)
        return []
    nodes: list[dict[str, Any]] = []
    cursor = ""
    for page_num in range(_MAX_FETCH_PAGES):
        cmd = [
            "gh",
            "api",
            "graphql",
            "-f",
            f"query={query}",
            "-f",
            f"owner={owner}",
            "-f",
            f"name={name}",
            # -F (--field) auto-converts integers to JSON number type
            "-F",
            f"first={page_size}",
            "-F",
            f"pr={pr_number}",
        ]
        if cursor:
            cmd += ["-f", f"after={cursor}"]
        try:
            result = subprocess.run(
                cmd, capture_output=True, text=True, timeout=GH_TIMEOUT_SEC
            )
        except subprocess.TimeoutExpired:
            print(f"Warning: gh CLI timed out for {field}", file=sys.stderr)
            break
        if result.returncode != 0:
            break
        try:
            data = json.loads(result.stdout)
            section = (
                (data.get("data") or {})
                .get("repository", {})
                .get("pullRequest", {})
                .get(field, {})
            )
            raw = section.get("nodes", [])
            nodes.extend(transform(raw) if transform else raw)
            page = section.get("pageInfo", {})
            if page.get("hasNextPage") and page.get("endCursor"):
                cursor = page["endCursor"]
            else:
                break
        except (json.JSONDecodeError, KeyError):
            break
    else:
        print(
            f"Warning: reached max pages ({_MAX_FETCH_PAGES}) for {field}, "
            "results may be truncated",
            file=sys.stderr,
        )
    return nodes


def get_pr_head_sha(pr_number: str) -> str:
    """Get the HEAD commit SHA of the PR."""
    if not pr_number.isdigit():
        raise ValueError(f"Invalid pr_number: {pr_number!r}")
    result = subprocess.run(
        ["gh", "pr", "view", pr_number, "--json", "headRefOid", "-q", ".headRefOid"],
        capture_output=True,
        timeout=GH_TIMEOUT_SEC,
        text=True,
        check=True,
    )
    return result.stdout.strip()
