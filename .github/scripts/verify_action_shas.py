#!/usr/bin/env python3
"""Verify GitHub Actions SHA pins in pr.diff and append results to context.md.

Reads pr.diff from the current directory, parses added lines that reference
pinned GitHub Actions SHAs, verifies each pin against the upstream tag API,
and appends a <verified-action-pins> XML section to context.md.

Pin statuses:
  - ``verified``   -- SHA matches a tag in the upstream ``/repos/{repo}/tags``
                     listing.
  - ``unmatched``  -- Tag listing fetched successfully but the SHA did not match
                     any tag (stale mirror, force-pushed tag, or wrong SHA).
  - ``unverified`` -- Tag listing could not be fetched (network/timeout/rate
                     limit/404). Treat with caution but do not block on it.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from typing import Any, Literal, TypedDict, cast
from xml.sax.saxutils import quoteattr

from github_pr_support import GH_TIMEOUT_SEC

# SHA-1 hex digest length -- GitHub Actions pins use the full commit SHA.
_SHA1_HEX_LEN = 40

# Matches: uses: owner/repo[/subpath]@<40-hex-sha>  # optional-comment
_USES_RE = re.compile(
    rf"uses:\s*(?P<repo>[\w.-]+/[\w.-]+)(?:/[^@]+)?@(?P<sha>[0-9a-f]{{{_SHA1_HEX_LEN}}})"
    r"(?:\s*#\s*(?P<comment>v?[\w.-]+))?"
)

_TAGS_PATH_TMPL = "repos/{repo}/tags"

# Maximum number of stderr characters carried into the ``api-error: ...``
# pin attribute. Large enough to preserve the gist of a gh/curl failure
# message, short enough to avoid bloating ``context.md`` when the CLI dumps
# a stack trace.
_STDERR_EXCERPT_MAX_LEN = 200


class RawPin(TypedDict):
    """A parsed action pin before verification."""

    repo: str
    sha: str
    comment: str | None


class VerifiedPin(TypedDict):
    repo: str
    sha: str
    comment: str | None
    status: Literal["verified"]
    tag: str
    comment_matches: str


class _VerifiedPinNoComment(TypedDict):
    """Verified pin for a commentless diff line (no comment_matches field)."""

    repo: str
    sha: str
    comment: str | None
    status: Literal["verified"]
    tag: str


class UnverifiedPin(TypedDict):
    repo: str
    sha: str
    comment: str | None
    status: Literal["unverified"]
    error: str


class UnmatchedPin(TypedDict):
    repo: str
    sha: str
    comment: str | None
    status: Literal["unmatched"]
    error: str


# ResultPin covers all post-verification states.
ResultPin = VerifiedPin | _VerifiedPinNoComment | UnverifiedPin | UnmatchedPin

# Pin is the public alias used by external callers (tests import it).
Pin = RawPin | ResultPin

# Memoised ``_fetch_tags`` results keyed by repo. The value pair is
# ``(tags, None)`` on success and ``(None, error_label)`` on failure.
TagCache = dict[str, tuple[list[dict[str, Any]] | None, str | None]]


def _parse_pins(diff_text: str) -> list[RawPin]:
    """Return unique (repo, sha, comment) tuples from added diff lines.

    Skips `+++` header lines and `-` removal lines.
    Deduplicates by the full ``(repo, sha, comment)`` triple so that divergent
    version comments on the same SHA (e.g. ``# v4`` and ``# v4.3.1``) each
    produce a distinct ``<pin>`` row in the rendered output. Exact duplicate
    lines within the diff still collapse to a single entry.
    """
    seen: set[tuple[str, str, str | None]] = set()
    pins: list[RawPin] = []
    for line in diff_text.splitlines():
        if not line.startswith("+") or line.startswith("+++"):
            continue
        m = _USES_RE.search(line)
        if not m:
            continue
        repo = m.group("repo")
        sha = m.group("sha")
        comment = m.group("comment")
        key = (repo, sha, comment)
        if key in seen:
            continue
        seen.add(key)
        pins.append({"repo": repo, "sha": sha, "comment": comment})
    return pins


def _decode_paginated_json(
    raw: str,
) -> tuple[list[dict[str, Any]], str | None]:
    """Parse --paginate output that may concatenate multiple JSON arrays.

    Returns ``(items, None)`` on full success. If a ``JSONDecodeError`` is
    encountered mid-stream (e.g. a later page was truncated or corrupted),
    returns ``(items_so_far, "json-decode-error")`` and emits a
    ``::warning::`` to stderr so the GitHub Actions UI surfaces the
    diagnostic. The caller is then responsible for treating the partial
    result as untrustworthy rather than silently consuming it.
    """
    items: list[dict[str, Any]] = []
    decoder = json.JSONDecoder()
    pos = 0
    while pos < len(raw):
        try:
            obj, end = decoder.raw_decode(raw, pos)
            if isinstance(obj, list):
                items.extend(cast("list[dict[str, Any]]", obj))
            pos = end
            while pos < len(raw) and raw[pos] in " \t\n\r":
                pos += 1
        except json.JSONDecodeError as exc:
            print(
                f"::warning::JSON decode failed in paginated tag output "
                f"at offset {pos}: {exc}",
                file=sys.stderr,
            )
            return items, "json-decode-error"
    return items, None


def _fetch_tags(
    repo: str, cache: TagCache
) -> tuple[list[dict[str, Any]] | None, str | None]:
    """Fetch all tags for repo via the ``/repos/{repo}/tags`` REST endpoint.

    Each element in the returned list has the shape
    ``{"name": str, "commit": {"sha": str, ...}}``. The ``commit.sha`` is the
    peeled commit SHA for both lightweight and annotated tags, so no separate
    peeling call is required.

    Results are memoised in ``cache`` so that multiple pins referencing the
    same repo (e.g. several ``actions/checkout`` entries at different SHAs)
    share a single API call.

    Returns (tags, None) on success, (None, error_label) on failure.
    """
    if repo in cache:
        return cache[repo]
    try:
        result = subprocess.run(
            ["gh", "api", _TAGS_PATH_TMPL.format(repo=repo), "--paginate"],
            capture_output=True,
            text=True,
            timeout=GH_TIMEOUT_SEC,
        )
    except subprocess.TimeoutExpired:
        outcome: tuple[list[dict[str, Any]] | None, str | None] = (None, "timeout")
        cache[repo] = outcome
        return outcome
    if result.returncode != 0:
        # ``gh api`` reports HTTP failures through stderr with a non-zero
        # CLI exit code (typically 1 for any error). We cannot rely on
        # ``returncode`` matching the HTTP status -- the process exit code
        # is not the HTTP status -- so we inspect stderr for known markers.
        stderr_lc = result.stderr.lower()
        if "rate limit" in stderr_lc or "http 403" in stderr_lc:
            outcome = (None, "rate-limit")
        # Only match the full ``http 404`` token -- bare "not found" is too
        # broad (e.g. "credentials not found in keychain",
        # "file not found") and would misclassify unrelated CLI errors as a
        # missing repo.
        elif "http 404" in stderr_lc:
            outcome = (None, "repo-not-found")
        else:
            # Preserve a short excerpt of stderr so ``<verified-action-pins>``
            # surfaces the actual failure mode to the reviewer prompt.
            # ``splitlines()`` can return ``[]`` for a whitespace-only stderr
            # (e.g. ``"  \n  "``) -- guard against ``IndexError`` there.
            stderr_lines = result.stderr.strip().splitlines() if result.stderr else []
            excerpt = stderr_lines[0] if stderr_lines else ""
            outcome = (
                None,
                f"api-error: {excerpt[:_STDERR_EXCERPT_MAX_LEN]}"
                if excerpt
                else "api-error",
            )
        cache[repo] = outcome
        return outcome
    items, decode_error = _decode_paginated_json(result.stdout.strip())
    if decode_error is not None:
        # A malformed page mid-stream leaves us with an incomplete tag list
        # -- route through the ``unverified`` path so the reviewer prompt
        # knows the verification could not be trusted, rather than silently
        # falling into ``unmatched`` territory.
        outcome = (None, decode_error)
    else:
        outcome = (items, None)
    cache[repo] = outcome
    return outcome


def _verify_pin(pin: RawPin, cache: TagCache) -> ResultPin:
    """Verify a single pin against the upstream tag listing.

    Returns a typed result pin with status/tag/comment_matches/error.
    Prefers the comment-matching tag when multiple tags share the same SHA
    (e.g. a floating 'v4' alias and a specific 'v4.3.1' tag).
    """
    repo = pin["repo"]
    sha = pin["sha"]
    comment = pin["comment"]

    tags, error = _fetch_tags(repo, cache)
    if tags is None:
        # ``_fetch_tags`` always returns a non-None error label alongside a
        # None tags list, but we fall back explicitly here rather than rely on
        # ``assert`` -- the ``-O`` / ``-OO`` optimisation flags strip asserts
        # and would otherwise produce a pin with ``error=None`` that
        # ``_render_xml`` silently drops.
        return UnverifiedPin(
            repo=repo,
            sha=sha,
            comment=comment,
            status="unverified",
            error=error if error is not None else "unknown",
        )

    matching_tags: list[str] = []
    for tag in tags:
        commit = tag.get("commit", {})
        if commit.get("sha") == sha:
            matching_tags.append(tag.get("name", ""))

    if not matching_tags:
        return UnmatchedPin(
            repo=repo,
            sha=sha,
            comment=comment,
            status="unmatched",
            error="sha-not-found",
        )

    # Prefer the tag that matches the comment; fall back to the first match.
    matched_tag = next(
        (t for t in matching_tags if comment and t == comment), matching_tags[0]
    )
    # Only emit ``comment_matches`` when the diff actually carried a
    # ``# vX.Y`` comment. A commentless pin has no mismatch to flag, so
    # omitting the field keeps downstream prompt guidance from producing a
    # false-positive "comment drift" finding on every commentless pin.
    if comment is not None:
        return VerifiedPin(
            repo=repo,
            sha=sha,
            comment=comment,
            status="verified",
            tag=matched_tag,
            comment_matches=str(matched_tag == comment).lower(),
        )
    return _VerifiedPinNoComment(
        repo=repo, sha=sha, comment=comment, status="verified", tag=matched_tag
    )


def _pin_attrs(p: ResultPin) -> list[str]:
    """Return the XML attribute list for a single result pin.

    Every value is passed through :func:`xml.sax.saxutils.quoteattr` so that
    upstream-controlled strings cannot inject content into the rendered XML.
    """
    # Core fields present on every variant.
    attrs = [
        f"repo={quoteattr(p['repo'])}",
        f"sha={quoteattr(p['sha'])}",
    ]
    if p["comment"] is not None:
        attrs.append(f"comment={quoteattr(p['comment'])}")
    attrs.append(f"status={quoteattr(p['status'])}")

    # Discriminated on status -- each branch carries exactly the right fields.
    if p["status"] == "verified":
        attrs.append(f"tag={quoteattr(p['tag'])}")
        # comment_matches is present only when the original pin had a comment.
        if "comment_matches" in p:
            attrs.append(f"comment-matches={quoteattr(p['comment_matches'])}")
    elif p["status"] in ("unverified", "unmatched"):
        attrs.append(f"error={quoteattr(p['error'])}")

    return attrs


def _render_xml(pins: list[ResultPin]) -> str:
    """Render result pins as XML attributes inside <verified-action-pins>.

    Every attribute value is passed through :func:`xml.sax.saxutils.quoteattr`
    so that upstream-controlled strings (``repo``, ``tag``, ``error``) cannot
    inject quotes, angle brackets, or ampersands into the rendered XML and
    thereby into the downstream reviewer prompt in ``context.md``.
    """
    lines = ["<verified-action-pins>"]
    for p in pins:
        lines.append(f"  <pin {' '.join(_pin_attrs(p))}/>")
    lines.append("</verified-action-pins>")
    return "\n".join(lines)


def main() -> None:
    try:
        # ``errors="replace"`` keeps the script tolerant of PR diffs that
        # carry non-UTF-8 bytes (e.g. mixed-encoding text added to source).
        # SHA verification is the job here -- not policing diff encoding --
        # and replacing bad bytes preserves byte/line positions for
        # downstream regex matching.
        with open("pr.diff", encoding="utf-8", errors="replace") as f:
            diff_text = f.read()
    except FileNotFoundError:
        print("pr.diff not found -- skipping SHA verification", file=sys.stderr)
        return

    pins = _parse_pins(diff_text)
    if not pins:
        print("No pinned action SHAs found in diff")
        return

    cache: TagCache = {}
    results: list[ResultPin] = [_verify_pin(p, cache) for p in pins]

    verified_count = sum(1 for r in results if r["status"] == "verified")
    print(f"Verified {verified_count}/{len(results)} action SHA pins")

    section = (
        "## Verified Action SHA Pins\n\n"
        "The PR pins SHAs for the following GitHub Actions. Each pin was verified "
        "against the upstream repository's tag listing at review-prepare time. "
        "Apply these rules by status:\n\n"
        '- `status="verified"` -- the SHA matched a tag. Do NOT flag the SHA as '
        'invalid. If `comment-matches="false"`, the `# vX.Y` version comment '
        "drifts from the resolved tag (given in the `tag` attribute) -- raise as "
        "`minor` citing the correct tag. If `comment-matches` is absent, the "
        "PR pin has no `# vX.Y` comment at all -- that is not a finding.\n"
        '- `status="unmatched"` -- the tag listing was fetched but the SHA did '
        "not appear in any tag. Raise as `major` (likely stale mirror, "
        "force-pushed tag, or typo).\n"
        '- `status="unverified"` with `error="repo-not-found"` -- the upstream '
        "action repository does not exist; the pin will never resolve. Raise as "
        "`major`.\n"
        '- `status="unverified"` with any other error (e.g. `rate-limit`, '
        "`timeout`, `api-error: ...`, `json-decode-error`, `unknown`) -- the "
        "verification itself failed transiently. Raise only as `suggestion`.\n\n"
        + _render_xml(results)
    )

    with open("context.md", "a", encoding="utf-8") as f:
        f.write(f"\n\n---\n\n{section}")


if __name__ == "__main__":
    main()
