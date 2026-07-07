#!/usr/bin/env python3
"""Auto-scale the Claude review model on usage-limit events (AT-1512).

Two modes, shared by the base repo and the pilot wrapper (which runs this
script from its pinned `.ai-dev-pr-review` upstream checkout):

- detect-and-downgrade: scan the claude-code-action execution log (path in
  env EXEC_FILE) for the usage-limit signature. On detection, set
  CLAUDE_MODEL to the sonnet fallback on the ignite-corp org var and on
  every pilot repo var, and record the reset epoch in the ignite-corp org
  var CLAUDE_MODEL_RESTORE_AT (first value wins; an existing epoch is kept).
- restore-if-due: if CLAUDE_MODEL_RESTORE_AT exists and has passed, set
  CLAUDE_MODEL everywhere back to vars.CLAUDE_MODEL_DEFAULT and delete
  CLAUDE_MODEL_RESTORE_AT.

Tokens (GitHub App installation tokens, minted by the calling step):
- GH_TOKEN_CORP: ignite-corp-scoped ops (org variables).
- GH_TOKEN_PILOT: ignite-pilot-org repo variable ops.

Pilot repo list: env MODEL_SCALE_REPOS (JSON array of full names) if set,
otherwise read from the private ignite-corp org var MODEL_SCALE_REPOS.

PUBLIC-repo log policy: pilot repo names must never appear in output.
Progress is logged by index only (`entry i/total`), matching the
ruleset-sync.yml / ruleset-audit.yml convention.

FAIL-SAFE: any API error is logged to stderr and the script exits 0 -- it
must never fail the review job. Set DRY_RUN=true to log intended writes
(index-only) without calling the API.

Usage: python3 scale_claude_model.py <detect-and-downgrade|restore-if-due>
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
_SONNET_MODEL = "claude-sonnet-4-6"
_FALLBACK_DEFAULT_MODEL = "claude-opus-4-8"
_SESSION_RESET_FALLBACK_SEC = 5 * 3600
_MODEL_VAR = "CLAUDE_MODEL"
_RESTORE_AT_VAR = "CLAUDE_MODEL_RESTORE_AT"
_DEFAULT_MODEL_VAR = "CLAUDE_MODEL_DEFAULT"
_REPO_LIST_VAR = "MODEL_SCALE_REPOS"

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

    `label` is used for logging instead of `path`, which may contain pilot
    repo names that must not appear in public logs. Returns (0, {}) on
    transport errors.
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


def _set_repo_var(token: str, repo: str, name: str, value: str, label: str) -> bool:
    """PATCH the repo var; POST (create) if it does not exist yet."""
    body = {"name": name, "value": value}
    status, _ = _api(
        token,
        "PATCH",
        f"/repos/{repo}/actions/variables/{name}",
        body,
        label=label,
    )
    if status == 404:
        status, _ = _api(
            token,
            "POST",
            f"/repos/{repo}/actions/variables",
            body,
            label=f"{label} (create)",
        )
        return status in (201, 204)
    return status in (200, 204)


def _pilot_repos(corp_token: str) -> list[str]:
    """Pilot repo full names from env, else from the private org var."""
    raw = os.environ.get(_REPO_LIST_VAR, "").strip()
    if not raw:
        raw = _get_org_var(corp_token, _REPO_LIST_VAR) or ""
    if not raw:
        _log(f"{_REPO_LIST_VAR} unavailable -- skipping pilot repo updates")
        return []
    try:
        repos = json.loads(raw)
    except json.JSONDecodeError as e:
        _log(f"{_REPO_LIST_VAR} is not valid JSON: {e}")
        return []
    if not isinstance(repos, list) or not all(isinstance(r, str) for r in repos):
        _log(f"{_REPO_LIST_VAR} must be a JSON array of strings")
        return []
    return repos


def _set_model_everywhere(corp_token: str, pilot_token: str, model: str) -> tuple[int, int]:
    """Set CLAUDE_MODEL on the corp org var and every pilot repo var.

    Always writes the target value to every target (idempotent PATCH) so
    value drift is corrected too -- never skip on "already set". Returns
    (ok_count, total_count) over all targets.
    """
    ok = 0
    if _set_org_var(corp_token, _MODEL_VAR, model):
        ok += 1
    if not pilot_token:
        _log("GH_TOKEN_PILOT is empty -- skipping pilot repo updates")
        return ok, 1
    repos = _pilot_repos(corp_token)
    total = 1 + len(repos)
    for i, repo in enumerate(repos, start=1):
        label = f"pilot repo var {_MODEL_VAR} entry {i}/{len(repos)}"
        if _set_repo_var(pilot_token, repo, _MODEL_VAR, model, label):
            ok += 1
            _log(f"OK: entry {i}/{len(repos)}")
    return ok, total


def _parse_limit_epoch(text: str, now: float) -> int | None:
    """Reset epoch from the usage-limit signature, or None if absent.

    Primary: `Claude AI usage limit reached|<epoch>` (last match wins).
    Fallback: case-insensitive `usage limit reached` without an epoch ->
    conservative session reset of now + 5h (premature restore is
    self-correcting: the next limited run re-triggers the downgrade).
    """
    matches = _LIMIT_EPOCH_RE.findall(text)
    if matches:
        return int(matches[-1])
    if _LIMIT_FALLBACK_RE.search(text):
        return int(now) + _SESSION_RESET_FALLBACK_SEC
    return None


def detect_and_downgrade(corp_token: str, pilot_token: str) -> None:
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
    ok, total = _set_model_everywhere(corp_token, pilot_token, _SONNET_MODEL)

    # First value wins: keep an existing epoch so repeated limit events
    # inside the same window do not push the restore further out.
    existing = _get_org_var(corp_token, _RESTORE_AT_VAR)
    if existing:
        _log(f"{_RESTORE_AT_VAR} already set, keeping existing epoch")
    else:
        _set_org_var(corp_token, _RESTORE_AT_VAR, str(epoch))
    _summary(
        f"Claude usage limit detected: downgraded {_MODEL_VAR} to "
        f"{_SONNET_MODEL} on {ok}/{total} targets, restore at epoch "
        f"{existing or epoch}"
    )


def restore_if_due(corp_token: str, pilot_token: str) -> None:
    restore_at = _get_org_var(corp_token, _RESTORE_AT_VAR)
    if restore_at is None:
        return
    try:
        epoch = int(restore_at)
    except ValueError:
        _log(f"{_RESTORE_AT_VAR} is not an integer, deleting it")
        _delete_org_var(corp_token, _RESTORE_AT_VAR)
        return
    now = time.time()
    if now <= epoch:
        _summary(f"Claude model restore not due yet (epoch {epoch})")
        return

    default_model = (
        _get_org_var(corp_token, _DEFAULT_MODEL_VAR) or _FALLBACK_DEFAULT_MODEL
    )
    ok, total = _set_model_everywhere(corp_token, pilot_token, default_model)
    _delete_org_var(corp_token, _RESTORE_AT_VAR)
    _summary(
        f"Claude model restored: set {_MODEL_VAR} to {default_model} on "
        f"{ok}/{total} targets, cleared {_RESTORE_AT_VAR}"
    )


def main() -> None:
    if len(sys.argv) != 2 or sys.argv[1] not in (
        "detect-and-downgrade",
        "restore-if-due",
    ):
        print(
            f"Usage: {sys.argv[0]} <detect-and-downgrade|restore-if-due>",
            file=sys.stderr,
        )
        sys.exit(1)

    corp_token = os.environ.get("GH_TOKEN_CORP", "")
    pilot_token = os.environ.get("GH_TOKEN_PILOT", "")
    if not corp_token:
        _log("GH_TOKEN_CORP is empty -- skipping model scaling")
        return

    # FAIL-SAFE: never fail the calling job on unexpected errors.
    try:
        if sys.argv[1] == "detect-and-downgrade":
            detect_and_downgrade(corp_token, pilot_token)
        else:
            restore_if_due(corp_token, pilot_token)
    except Exception as e:  # noqa: BLE001 -- fail-safe by design
        _log(f"Model scaling failed (ignored): {e}")


if __name__ == "__main__":
    main()
