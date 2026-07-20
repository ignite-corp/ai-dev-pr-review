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
  subscription once this epoch has passed.

Two modes:

- detect-and-switch: scan the claude-code-action execution log (path in env
  EXEC_FILE) for the usage-limit signature. On detection, set
  CLAUDE_FORCE_API=true and CLAUDE_FORCE_API_UNTIL=<reset epoch> on the
  ignite-corp org vars (create via POST if absent). The epoch is kept-first:
  an existing earlier epoch is never overwritten.
- restore-if-due: if CLAUDE_FORCE_API_UNTIL exists and has passed, delete
  both CLAUDE_FORCE_API and CLAUDE_FORCE_API_UNTIL (delete = revert to the
  subscription; there is no default value to restore, unlike the model axis).

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

_API_BASE = "https://api.github.com"
_API_TIMEOUT_SEC = 30
_CORP_ORG = "ignite-corp"
_SESSION_RESET_FALLBACK_SEC = 5 * 3600
_FORCE_API_VAR = "CLAUDE_FORCE_API"
_UNTIL_VAR = "CLAUDE_FORCE_API_UNTIL"

# Non-interactive CLI limit signature (anthropics/claude-code#9046).
_LIMIT_EPOCH_RE = re.compile(r"Claude AI usage limit reached\|(\d+)")
_LIMIT_FALLBACK_RE = re.compile(r"usage limit reached", re.IGNORECASE)


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
) -> tuple[int, dict]:
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


def _parse_limit_epoch(text: str, now: float) -> int | None:
    """Reset epoch from the usage-limit signature, or None if absent.

    Primary: `Claude AI usage limit reached|<epoch>` (last match wins).
    Fallback: case-insensitive `usage limit reached` without an epoch ->
    conservative session reset of now + 5h (premature restore is
    self-correcting: the next limited run re-triggers the switch).
    """
    matches = _LIMIT_EPOCH_RE.findall(text)
    if matches:
        return int(matches[-1])
    if _LIMIT_FALLBACK_RE.search(text):
        return int(now) + _SESSION_RESET_FALLBACK_SEC
    return None


def detect_and_switch(corp_token: str) -> None:
    exec_file = os.environ.get("EXEC_FILE", "")
    if not exec_file or not os.path.isfile(exec_file):
        return
    try:
        with open(exec_file, encoding="utf-8") as f:
            text = f.read()
    except OSError as e:
        _log(f"Cannot read execution file: {e}")
        return

    epoch = _parse_limit_epoch(text, time.time())
    if epoch is None:
        return

    _log(f"Usage-limit signature detected, reset epoch {epoch}")

    # Keep-first on the epoch: an existing earlier epoch is never pushed out
    # by a later limit event inside the same window.
    existing = _get_org_var(corp_token, _UNTIL_VAR)
    if existing:
        _log(f"{_UNTIL_VAR} already set, keeping existing epoch")
    else:
        _set_org_var(corp_token, _UNTIL_VAR, str(epoch))

    force_ok = _set_org_var(corp_token, _FORCE_API_VAR, "true")
    _summary(
        f"Claude usage limit detected: {_FORCE_API_VAR}=true "
        f"({'set' if force_ok else 'failed'}), restore at epoch "
        f"{existing or epoch}"
    )


def restore_if_due(corp_token: str) -> None:
    until = _get_org_var(corp_token, _UNTIL_VAR)
    if until is None:
        return
    try:
        epoch = int(until)
    except ValueError:
        _log(f"{_UNTIL_VAR} is not an integer, deleting both vars")
        _delete_org_var(corp_token, _FORCE_API_VAR)
        _delete_org_var(corp_token, _UNTIL_VAR)
        return
    now = time.time()
    if now <= epoch:
        _summary(f"Claude auth restore not due yet (epoch {epoch})")
        return

    force_ok = _delete_org_var(corp_token, _FORCE_API_VAR)
    until_ok = _delete_org_var(corp_token, _UNTIL_VAR)
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
