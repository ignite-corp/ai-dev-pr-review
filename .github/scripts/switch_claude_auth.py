#!/usr/bin/env python3
"""Auto-switch Claude review auth on usage-limit events (AT-1606).

When a Claude review hits the subscription (OAuth) usage limit, switch the
billing path to the billed ANTHROPIC_API_KEY for subsequent reviews, then
automatically revert to the subscription once the limit resets. This is the
auth-axis counterpart of the AT-1512 model-scaling loop; the switched
dimension is the auth path, not the model.

Scope is a single pair of ignite-corp organization variables (no per-repo
list, no pilot fan-out -- pilot has no org vars and keeps its manual
empty-secret toggle):

- CLAUDE_FORCE_API: 'true' forces the billed API path (the caller reads it
  and passes force_api to the composite).
- CLAUDE_FORCE_API_UNTIL: reset epoch (seconds). The cron restores the
  subscription once this epoch has passed, and the review step that reads
  CLAUDE_FORCE_API ignores it once this epoch has passed, without waiting
  for the cron (AT-3439). CLAUDE_FORCE_API=true with no UNTIL still forces
  the API path: that is an operator's manual pin.

Two modes:

- detect-and-switch: parse the claude-code-action execution file (path in
  env EXEC_FILE; a JSON array of Agent SDK messages) for a usage-limit
  failure -- see _detect_limit. On detection, set CLAUDE_FORCE_API=true and
  CLAUDE_FORCE_API_UNTIL=<reset epoch> on the ignite-corp org vars (create
  via POST if absent). An existing future epoch combines as
  min(existing, new), so a window never outlives the earliest reset seen;
  an expired one or one that is not an epoch is replaced (AT-3439). Every outcome is
  logged, including no file and no match.
- restore-if-due: if CLAUDE_FORCE_API_UNTIL exists and has passed, delete
  both CLAUDE_FORCE_API and CLAUDE_FORCE_API_UNTIL (delete = revert to the
  subscription; there is no default value to restore, unlike the model axis).
  UNTIL is re-read before each delete, so a window a review renewed in the
  meantime is not wiped (AT-3439).

Token (a GitHub App installation token minted by the calling step):
- GH_TOKEN: ignite-corp-scoped ops (org variables).

PUBLIC-repo log policy: no repo names are ever emitted. Progress is logged
by index only, matching the ruleset-sync.yml / ruleset-audit.yml convention.

FAIL-SAFE: any API error is logged to stderr and the script exits 0 -- it
must never fail the review job. Set DRY_RUN=true to log intended writes
without calling the API.

Usage: python3 switch_claude_auth.py <detect-and-switch|restore-if-due>
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, TypeGuard

_API_BASE = "https://api.github.com"
_API_TIMEOUT_SEC = 30
_CORP_ORG = "ignite-corp"
_SESSION_RESET_FALLBACK_SEC = 5 * 3600
_FORCE_API_VAR = "CLAUDE_FORCE_API"
_UNTIL_VAR = "CLAUDE_FORCE_API_UNTIL"

# Legacy non-interactive CLI limit signature (anthropics/claude-code#9046).
# Not among the SDK 0.3.316 USAGE_LIMIT_ERROR_PREFIXES, and not seen in any
# CI run read for AT-3408 (2026-08-13..2026-11-10); no real limit failure
# was captured, so this is an observation, not a guarantee. Kept as one
# more alternative: in a JSON execution file it is matched only on error
# text, never on what the reviewer read; a non-JSON file is still scanned
# whole (see _detect_limit).
_LIMIT_EPOCH_RE = re.compile(r"Claude AI usage limit reached\|(\d+)")
_LIMIT_FALLBACK_RE = re.compile(r"usage limit reached", re.IGNORECASE)

# The Agent SDK's USAGE_LIMIT_ERROR_PREFIXES: the messages meaning "a usage
# limit was genuinely reached". Copied from sdk.d.ts of the SDK that the
# pinned claude-code-action installs (its bun.lock pins the version below).
# tests/fixtures/claude-agent-sdk-<version>-usage-limit-prefixes.json holds
# the list and action SHA it was read for, and test_switch_claude_auth.py
# compares this list, both constants and the composite's pin against it, so
# an action bump fails CI until the list is re-read. Non-ASCII is escaped:
# this tree is ASCII-only (test_ascii_only.py).
USAGE_LIMIT_ERROR_PREFIXES: tuple[str, ...] = (
    "You've hit your",
    "You've reached your",
    "You're out of usage credits",
    "Your org is out of usage \u00b7 add funds to continue",
    "Your org is out of usage \u00b7 contact your admin",
    "Your seat type doesn't include usage credits",
    "Your seat type doesn't include usage",
    "Your usage allocation has been disabled by your admin",
    "Your group's usage limit is set to $0",
    "Fable 5 requires usage credits",
    "You're out of extra usage",
    "Your seat type doesn't include extra usage",
)
_PREFIXES_SDK_VERSION = "0.3.316"
_PREFIXES_ACTION_SHA = "d535f5172dec02b204866b393d6b20b9b2aaf98d"

# SDKAssistantMessageError values that a usage limit arrives as. A transient
# 429 is also `rate_limit`, which is why a prefix must match as well.
_LIMIT_ERROR_KINDS = frozenset({"rate_limit", "billing_error"})
# SDKRateLimitInfo reset fields, in the order they are preferred.
_RESET_KEYS = ("resetsAt", "overageResetsAt")
# The SDK types do not state the unit of resetsAt. Any value this large is
# milliseconds: as seconds it would be tens of thousands of years away.
_MS_EPOCH_FLOOR = 10**12
# No Claude limit window is longer than seven days, so a reset further out
# than this is not a real reset time: a read one is not written, and an
# existing UNTIL is warned about.
_MAX_LIMIT_WINDOW_SEC = 7 * 24 * 3600


@dataclass(frozen=True)
class _Detection:
    """Outcome of scanning an execution file.

    `epoch` is the reset epoch when a usage limit was detected, else None.
    `detail` is a log line that names only fixed strings and counts -- never
    execution-file text beyond a matched SDK prefix (PUBLIC log).
    """

    epoch: int | None
    detail: str


def _log(msg: str) -> None:
    print(msg, file=sys.stderr)


def _summary(line: str) -> None:
    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_path:
        try:
            with open(summary_path, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except OSError as e:
            _log(f"Cannot write step summary: {e}")
    _log(line)


def _dry_run() -> bool:
    return os.environ.get("DRY_RUN", "").lower() == "true"


def _api(
    token: str,
    method: str,
    path: str,
    body: dict[str, str] | None = None,
    *,
    label: str,
) -> tuple[int, dict[str, Any]]:
    """Call the GitHub REST API. Returns (status, parsed_json_or_empty).

    `label` is used for logging instead of `path` to keep output free of any
    repo/org identifiers. Returns (0, {}) on transport errors.
    """
    if _dry_run() and method != "GET":
        _log(f"DRY-RUN: would {method} {label}")
        return 200, {}
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(
        f"{_API_BASE}{path}",
        data=data,
        method=method,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=_API_TIMEOUT_SEC) as resp:
            raw = resp.read()
            return resp.status, json.loads(raw) if raw.strip() else {}
    except urllib.error.HTTPError as e:
        if e.code != 404:
            _log(f"API {method} {label} failed: HTTP {e.code}")
        return e.code, {}
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as e:
        _log(f"API {method} {label} failed: {e}")
        return 0, {}


def _get_org_var(token: str, name: str) -> str | None:
    status, data = _api(
        token,
        "GET",
        f"/orgs/{_CORP_ORG}/actions/variables/{name}",
        label=f"org var {name}",
    )
    if status == 200:
        return data.get("value")
    return None


def _set_org_var(token: str, name: str, value: str) -> bool:
    """PATCH the org var; POST (create) if it does not exist yet."""
    body = {"name": name, "value": value}
    status, _ = _api(
        token,
        "PATCH",
        f"/orgs/{_CORP_ORG}/actions/variables/{name}",
        body,
        label=f"org var {name}",
    )
    if status == 404:
        status, _ = _api(
            token,
            "POST",
            f"/orgs/{_CORP_ORG}/actions/variables",
            {**body, "visibility": "all"},
            label=f"org var {name} (create)",
        )
        return status in (201, 204)
    return status in (200, 204)


def _delete_org_var(token: str, name: str) -> bool:
    status, _ = _api(
        token,
        "DELETE",
        f"/orgs/{_CORP_ORG}/actions/variables/{name}",
        label=f"org var {name}",
    )
    return status in (204, 404)


def _legacy_epoch(text: str, now: float) -> int | None:
    """Reset epoch from the legacy CLI signature, or None if absent.

    Primary: `Claude AI usage limit reached|<epoch>` (last match wins),
    when the epoch is a plausible reset (_plausible_reset).
    Fallback: an implausible epoch, or case-insensitive `usage limit
    reached` without one -> conservative session reset of now + 5h
    (premature restore is self-correcting: the next limited run re-triggers
    the switch).
    """
    matches = _LIMIT_EPOCH_RE.findall(text)
    if matches and _plausible_reset(int(matches[-1]), now):
        return int(matches[-1])
    if matches or _LIMIT_FALLBACK_RE.search(text):
        return int(now) + _SESSION_RESET_FALLBACK_SEC
    return None


def _plausible_reset(epoch: float, now: float) -> bool:
    """True when `epoch` lies in (now, now + _MAX_LIMIT_WINDOW_SEC].

    Anything else -- past, further out than any limit window, or NaN /
    Infinity, which json.loads accepts and which fail the comparison -- is
    not written: a far epoch would hold the API path until a nearer reset
    was detected or someone cleared the variable by hand.
    """
    return now < epoch <= now + _MAX_LIMIT_WINDOW_SEC


def _is_dict(obj: object) -> TypeGuard[dict[str, Any]]:
    return isinstance(obj, dict)


def _is_list(obj: object) -> TypeGuard[list[Any]]:
    return isinstance(obj, list)


def _assistant_texts(msg: dict[str, Any]) -> list[str]:
    inner = msg.get("message")
    content = inner.get("content") if _is_dict(inner) else None
    if not _is_list(content):
        return []
    texts: list[str] = []
    for block in content:
        if _is_dict(block) and block.get("type") == "text":
            text = block.get("text")
            if isinstance(text, str):
                texts.append(text)
    return texts


def _usage_prefix(text: str) -> str | None:
    stripped = text.lstrip()
    for prefix in USAGE_LIMIT_ERROR_PREFIXES:
        if stripped.startswith(prefix):
            return prefix
    return None


def _reset_epoch(infos: list[dict[str, Any]], now: float) -> tuple[int, str] | None:
    """(epoch seconds, field name) from the newest rejected rate-limit info
    carrying a plausible reset; None when none does.

    A non-rejected event may describe a different window from the limit
    that was hit, so it is never read. An implausible reset is skipped
    (_plausible_reset).
    """
    rejected = [i for i in infos if i.get("status") == "rejected"]
    for info in reversed(rejected):
        for key in _RESET_KEYS:
            value = info.get(key)
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                continue
            if value >= _MS_EPOCH_FLOOR:
                value = value / 1000
            if not _plausible_reset(value, now):
                continue
            return int(value), key
    return None


def _detect_limit(text: str, now: float) -> _Detection:
    """Scan an execution file for a usage-limit failure (AT-3408).

    The file is the JSON array of Agent SDK messages claude-code-action
    writes. A limit is detected only when BOTH hold:

    - a structured signal: an assistant message whose `error` is in
      _LIMIT_ERROR_KINDS, or a `rate_limit_event` with status "rejected";
    - error text -- an errored result's `result`, or an errored assistant
      message's text -- that starts with an SDK usage-limit prefix.

    Requiring both keeps a dead model pin (`model_not_found`, no prefix)
    and a transient 429 or overload (no prefix, no rejected event) from
    switching. The reset epoch comes from a rejected rate-limit event, else
    now + 5h. The legacy CLI signature is one more alternative, matched on the
    same error text only -- reviewed content routinely contains it. A file
    that is not a JSON message list is scanned whole for the legacy
    signature, as before.
    """
    try:
        messages = json.loads(text)
    except ValueError:
        messages = None
    if not _is_list(messages):
        epoch = _legacy_epoch(text, now)
        if epoch is not None:
            return _Detection(epoch, "legacy usage-limit signature (not JSON)")
        return _Detection(None, "no usage-limit signature (not a JSON message list)")

    msgs = [m for m in messages if _is_dict(m)]
    structured = False
    error_texts: list[str] = []
    infos: list[dict[str, Any]] = []
    for msg in msgs:
        kind = msg.get("type")
        error = msg.get("error")
        if kind == "assistant" and error is not None:
            if isinstance(error, str) and error in _LIMIT_ERROR_KINDS:
                structured = True
            error_texts.extend(_assistant_texts(msg))
        elif kind == "result" and msg.get("is_error") is True:
            result = msg.get("result")
            if isinstance(result, str):
                error_texts.append(result)
        elif kind == "rate_limit_event":
            info = msg.get("rate_limit_info")
            if _is_dict(info):
                infos.append(info)
                if info.get("status") == "rejected":
                    structured = True

    prefix = next((p for t in error_texts if (p := _usage_prefix(t))), None)
    if structured and prefix is not None:
        reset = _reset_epoch(infos, now)
        if reset is not None:
            epoch, source = reset
        else:
            epoch, source = (
                int(now) + _SESSION_RESET_FALLBACK_SEC,
                "now + 5h (no plausible rejected-event reset)",
            )
        return _Detection(
            epoch, f"usage-limit signature: SDK prefix {prefix!r}, reset from {source}"
        )
    for error_text in error_texts:
        epoch = _legacy_epoch(error_text, now)
        if epoch is not None:
            return _Detection(epoch, "legacy usage-limit signature in error text")
    return _Detection(
        None,
        f"no usage-limit signature ({len(msgs)} messages scanned; "
        f"structured limit signal: {'yes' if structured else 'no'}; "
        f"SDK prefix: {'yes' if prefix is not None else 'no'})",
    )


_EPOCH_RE = re.compile(r"[0-9]{1,18}")


def _epoch(value: str | None) -> int | None:
    """The epoch in an org var value, or None when absent or not one.

    Surrounding ASCII whitespace is ignored; what remains must be 1-18
    ASCII digits. This is the same definition as the bash check in 'Resolve
    Claude auth path' (base-ai-review-single.yml), so the review, the
    detection and the restore agree on which values are epochs (AT-3439).
    int() alone would also take a sign, underscores and non-ASCII digits.
    """
    # The characters bash's [:space:] trims on the runner, not str.strip()'s
    # Unicode set.
    stripped = (value or "").strip(" \t\n\v\f\r")
    return int(stripped) if _EPOCH_RE.fullmatch(stripped) else None


def _warn_far_until(existing: int, now: float) -> None:
    """Name an UNTIL that no reset time explains (AT-3408). Under
    min(existing, new) a detected reset replaces it (AT-3439). The parsed
    integer is printed, not the raw value: a line break in the raw value
    would reach the workflow-command stream."""
    if existing - now > _MAX_LIMIT_WINDOW_SEC:
        print(
            f"::warning::{_UNTIL_VAR}={existing} is more than 7 days ahead, "
            "further than any Claude limit window, so it was not set from a "
            "reset time; replaced by the detected reset"
        )


def detect_and_switch(corp_token: str) -> None:
    exec_file = os.environ.get("EXEC_FILE", "")
    if not exec_file or not os.path.isfile(exec_file):
        _log("Claude auth switch: no execution file, nothing to scan")
        return
    try:
        with open(exec_file, encoding="utf-8") as f:
            text = f.read()
    except (OSError, UnicodeDecodeError) as e:
        _log(f"Claude auth switch: cannot read execution file: {type(e).__name__}")
        return

    now = time.time()
    detection = _detect_limit(text, now)
    _log(f"Claude auth switch: {detection.detail}")
    epoch = detection.epoch
    if epoch is None:
        return

    _log(f"Usage-limit signature detected, reset epoch {epoch}")

    # min(existing, new), as codex_review_auth.persist does (AT-3439): an
    # earlier epoch is never pushed out by a later limit event, and a later
    # one -- a hand-set far value included -- gives way to the detected
    # reset. An expired value, or one that is not an epoch, is replaced.
    existing = _epoch(_get_org_var(corp_token, _UNTIL_VAR))
    until = epoch
    if existing is not None and existing > now:
        _warn_far_until(existing, now)
        until = min(existing, epoch)
    if until == existing:
        _log(f"{_UNTIL_VAR} already set to an earlier epoch, keeping it")
    elif not _set_org_var(corp_token, _UNTIL_VAR, str(until)):
        # FORCE without UNTIL is an operator's manual pin, which neither the
        # review nor the restore expires; a failed write must not become one.
        print(
            f"::warning::{_UNTIL_VAR} could not be written, so "
            f"{_FORCE_API_VAR} was left unchanged"
        )
        return

    force_ok = _set_org_var(corp_token, _FORCE_API_VAR, "true")
    _summary(
        f"Claude usage limit detected: {_FORCE_API_VAR}=true "
        f"({'set' if force_ok else 'failed'}), restore at epoch {until}"
    )


def restore_if_due(corp_token: str) -> None:
    original = _get_org_var(corp_token, _UNTIL_VAR)
    if original is None:
        return
    epoch = _epoch(original)
    if epoch is None:
        _log(f"{_UNTIL_VAR} is not an epoch, deleting both vars")
    elif time.time() <= epoch:
        _summary(f"Claude auth restore not due yet (epoch {epoch})")
        return

    # Re-read before each delete, as codex_review_auth.restore does
    # (AT-3439): a review that hit the limit again may have renewed the
    # window since the first read, and deleting it would put the next
    # review back on an exhausted subscription.
    if _get_org_var(corp_token, _UNTIL_VAR) != original:
        _summary("Claude auth restore skipped: the window changed; retry later")
        return
    force_ok = _delete_org_var(corp_token, _FORCE_API_VAR)
    current = _get_org_var(corp_token, _UNTIL_VAR)
    if current != original:
        # The renewing detection may have written its flag before the delete
        # above; a renewed future window needs the flag back, or the review
        # that reads it stays on the exhausted subscription until the next
        # limit hit.
        renewed = _epoch(current)
        if current is None:
            outcome = f"{_UNTIL_VAR} was already removed"
        else:
            outcome = f"{_UNTIL_VAR} kept"
        if renewed is not None and renewed > time.time():
            reforce_ok = _set_org_var(corp_token, _FORCE_API_VAR, "true")
            outcome += f", {_FORCE_API_VAR} re-set {'ok' if reforce_ok else 'failed'}"
            if not reforce_ok:
                print(
                    f"::warning::{_FORCE_API_VAR} could not be re-set for a "
                    f"renewed {_UNTIL_VAR}; reviews use OAuth until the next "
                    "limit is detected"
                )
        _summary(
            f"Claude auth restore stopped: the window changed after "
            f"{_FORCE_API_VAR} was deleted; {outcome}"
        )
        return
    until_ok = _delete_org_var(corp_token, _UNTIL_VAR)
    # A detection that renewed UNTIL just before that delete writes its
    # flag after it, leaving FORCE without UNTIL -- a manual pin nothing
    # expires. Clear it, unless a detection after the delete already wrote
    # a new future UNTIL: then the flag belongs to that live window.
    if _get_org_var(corp_token, _FORCE_API_VAR) is not None:
        renewed = _epoch(_get_org_var(corp_token, _UNTIL_VAR))
        if renewed is None or renewed <= time.time():
            force_ok = _delete_org_var(corp_token, _FORCE_API_VAR)
    _summary(
        f"Claude auth restored to subscription: deleted {_FORCE_API_VAR} "
        f"({'ok' if force_ok else 'failed'}) and {_UNTIL_VAR} "
        f"({'ok' if until_ok else 'failed'})"
    )


def main() -> None:
    if len(sys.argv) != 2 or sys.argv[1] not in (
        "detect-and-switch",
        "restore-if-due",
    ):
        print(
            f"Usage: {sys.argv[0]} <detect-and-switch|restore-if-due>",
            file=sys.stderr,
        )
        sys.exit(1)

    corp_token = os.environ.get("GH_TOKEN", "")
    if not corp_token:
        _log("GH_TOKEN is empty -- skipping auth switch")
        return

    # FAIL-SAFE: never fail the calling job on unexpected errors.
    try:
        if sys.argv[1] == "detect-and-switch":
            detect_and_switch(corp_token)
        else:
            restore_if_due(corp_token)
    except Exception as e:  # noqa: BLE001 -- fail-safe by design
        _log(f"Auth switch failed (ignored): {e}")


if __name__ == "__main__":
    main()
