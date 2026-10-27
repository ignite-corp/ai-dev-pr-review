#!/usr/bin/env python3
"""Prefer workspace Codex auth, retry quota failures once, and restore it (AT-3117).

Credentials and retry metadata live inside ~/.codex, which the review permissions
profile denies to model tools. Only prepare/persist/restore receive secrets; retry
runs with stored auth and stdout-only JSONL events captured by the trusted host.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

from github_pr_support import is_json_object

# Operator-visible state only; selection uses the authoritative expiry, unlike Claude.
FORCE = "CODEX_FORCE_API"
UNTIL = "CODEX_FORCE_API_UNTIL"
PROBE_SECONDS = 5 * 3600
NO_RETRY = 4
NO_API_FALLBACK = 3
_VARIABLES_URL = "https://api.github.com/orgs/ignite-corp/actions/variables"
_API_TIMEOUT_SEC = 30

# Codex rust-v0.147.0, be6e8eac, protocol/src/error.rs: UsageLimitReachedError.
# exec/src/event_processor_with_jsonl_output.rs drops the typed error/reset
# fields but emits its canonical message inside a terminal turn.failed event.
# Agent/tool content is escaped inside item.* events, never parsed recursively.
_WORKSPACE_LIMITS = {
    "Your workspace is out of credits. Add credits to continue.",
    "Your workspace is out of credits. Ask your workspace owner to refill in order to continue.",
    "You hit your spend cap set in your workspace. Increase your spend cap to continue.",
    "You hit your spend cap set by the owner of your workspace. Ask an owner to increase your spend cap to continue.",
}
_QUOTA_MESSAGE = re.compile(
    r"(?:You've hit your usage limit\."
    r"(?: To get more access now, send a request to your admin)?|"
    r"You've hit your usage limit for [^\r\n]+\. Switch to another model now,)"
    r" (?:or try|Try) again (?:later|at (?P<reset>[^\r\n]+))\."
)


class ControlPlaneError(Exception):
    """The control plane could not be read or updated; absence is different."""


class Variables(Protocol):
    def get(self, name: str) -> str | None: ...
    def set(self, name: str, value: str) -> None: ...
    def delete(self, name: str) -> None: ...


class OrgVariables:
    def __init__(self, token: str):
        self.token = token

    def request(
        self, method: str, name: str = "", body: dict[str, str] | None = None
    ) -> dict[str, Any] | None:
        request = urllib.request.Request(
            _VARIABLES_URL + ("/" + name if name else ""),
            data=json.dumps(body).encode() if body is not None else None,
            method=method,
            headers={
                "Authorization": "Bearer " + self.token,
                "Accept": "application/vnd.github+json",
                "Content-Type": "application/json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=_API_TIMEOUT_SEC) as response:
                raw = response.read()
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as error:
            if error.code == 404:
                return None
            raise ControlPlaneError(f"{method} {name}: HTTP {error.code}") from None
        except (OSError, ValueError) as error:
            raise ControlPlaneError(
                f"{method} {name}: {type(error).__name__}"
            ) from None

    def get(self, name: str) -> str | None:
        response = self.request("GET", name)
        return response.get("value") if response is not None else None

    def set(self, name: str, value: str) -> None:
        body = {"name": name, "value": value}
        if self.request("PATCH", name, body) is None:
            if self.request("POST", body={**body, "visibility": "all"}) is None:
                raise ControlPlaneError(f"Cannot create {name}")

    def delete(self, name: str) -> None:
        self.request("DELETE", name)


def _epoch(value: str | None) -> int | None:
    try:
        return int(value) if value else None
    except ValueError:
        return None


def api_override(store: Variables, now: float) -> bool:
    # The expiry is authoritative. A partial flag write cannot strand API auth
    # forever, and an expiry committed before its flag still takes effect.
    until = _epoch(store.get(UNTIL))
    return until is not None and until > now


def persist(store: Variables, until: int, now: float) -> None:
    existing = _epoch(store.get(UNTIL))
    if existing is not None and existing > now:
        until = min(existing, until)
    if until <= now:
        raise ControlPlaneError("Refusing an expired quota switch")
    store.set(UNTIL, str(until))
    store.set(FORCE, "true")
    print(f"Codex API fallback persisted; token retry after UTC epoch {until}")


def restore(store: Variables, now: float) -> None:
    original = store.get(UNTIL)
    until = _epoch(original)
    if until is not None and until > now:
        print(f"Codex token restoration not due; UTC epoch {until}")
        return
    if original is None and store.get(FORCE) is None:
        return
    # Re-read before deleting: a review may have renewed an expired window.
    if store.get(UNTIL) != original:
        raise ControlPlaneError("Quota state changed during restoration; retry later")
    store.delete(FORCE)
    if store.get(UNTIL) != original:
        raise ControlPlaneError("Quota state changed during restoration; retry later")
    store.delete(UNTIL)
    print("Codex access-token preference restored")


def _reset_epoch(text: str | None, now: float) -> int:
    if text:
        # exec is run with TZ=UTC. The CLI prints only minutes; round up to
        # avoid retrying before the provider's actual seconds have elapsed.
        cleaned = re.sub(r"(?<=\d)(?:st|nd|rd|th)(?=,)", "", text)
        formats = ["%b %d, %Y %I:%M %p", "%I:%M %p"]
        for fmt in formats:
            try:
                date = datetime.strptime(cleaned, fmt).replace(tzinfo=UTC)
                if fmt == "%I:%M %p":
                    today = datetime.fromtimestamp(now, UTC)
                    date = date.replace(
                        year=today.year, month=today.month, day=today.day
                    )
                candidate = int(date.timestamp()) + 60
                if candidate > now:
                    return candidate
            except ValueError:
                continue
    return int(now) + PROBE_SECONDS


def quota_until(events: Path, exit_code: int, now: float) -> int | None:
    if exit_code == 0:
        return None
    terminal = None
    try:
        for line in events.read_text(encoding="utf-8").splitlines():
            event = json.loads(line)
            if not is_json_object(event):
                return None
            if event.get("type") in {"turn.failed", "turn.completed"}:
                terminal = event
    except (OSError, ValueError):
        return None
    if not terminal or terminal.get("type") != "turn.failed":
        return None
    error = terminal.get("error")
    message = error.get("message") if is_json_object(error) else None
    if not isinstance(message, str):
        return None
    if message in _WORKSPACE_LIMITS:
        return int(now) + PROBE_SECONDS
    match = _QUOTA_MESSAGE.fullmatch(message)
    return _reset_epoch(match["reset"], now) if match else None


def _write_private(path: Path, data: bytes) -> None:
    # Paths are inside the denied auth directory, not the PR workspace.
    temporary = path.with_suffix(".new")
    temporary.unlink(missing_ok=True)
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(data)
    os.replace(temporary, path)


def _login(credential: str, mode: str, destination: Path) -> bytes:
    destination.mkdir(mode=0o700, parents=True, exist_ok=True)
    auth_file = destination / "auth.json"
    auth_file.unlink(missing_ok=True)
    # Neither credential nor the control-plane token is inherited by the CLI.
    env = {
        key: value
        for key, value in os.environ.items()
        if not any(word in key.upper() for word in ("KEY", "TOKEN", "SECRET"))
        and key
        not in {"OPENAI_IDENTITY_TOKEN_FILE", "OPENAI_WORKLOAD_IDENTITY_PROVIDER"}
    }
    env["CODEX_HOME"] = str(destination)
    result = subprocess.run(
        ["codex", "-c", 'cli_auth_credentials_store="file"', "login", "--with-" + mode],
        input=credential,
        text=True,
        env=env,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0 or not auth_file.is_file():
        # Never repeat login stdout/stderr: a provider could reflect a token.
        raise ValueError(f"Codex {mode} login failed; exit={result.returncode}")
    data = auth_file.read_bytes()
    if not data:
        raise ValueError("Codex login produced an empty auth store")
    auth_file.chmod(0o600)
    return data


def prepare(home: Path, store: Variables | None, now: float) -> None:
    home.mkdir(mode=0o700, parents=True, exist_ok=True)
    # Even a failed login must not persist an older run's quota marker.
    (home / "review-quota.json").unlink(missing_ok=True)
    (home / "review-auth.json").unlink(missing_ok=True)
    token = os.environ.get("CODEX_ACCESS_TOKEN", "").strip()
    key = os.environ.get("OPENAI_API_KEY", "").strip()
    forced = False
    if token and store:
        try:
            forced = api_override(store, now)
        except ControlPlaneError as error:
            print(
                f"::warning::Codex auth state unavailable: {error}; trying preferred auth"
            )
    elif token:
        print("::warning::Codex central auth state unavailable: no control-plane token")
    if forced and token and not key:
        print(
            "::warning::Codex API fallback is unavailable for this caller; trying its access token"
        )
        forced = False
    mode = "api-key" if forced or not token else "access-token"
    if not (key if mode == "api-key" else token):
        raise ValueError(f"Codex selected {mode} credential is missing")
    fallback = home / "review-api-auth.json"
    fallback.unlink(missing_ok=True)
    if mode == "access-token" and key:
        try:
            data = _login(key, "api-key", home / "review-api-login")
            _write_private(fallback, data)
        except (ValueError, OSError):
            print("::warning::Codex API fallback credential preparation failed")
    selected = key if mode == "api-key" else token
    data = _login(selected, mode, home)
    _write_private(home / "auth.json", data)
    _write_private(home / "review-auth.json", json.dumps({"mode": mode}).encode())
    print(f"Codex selected auth: {mode}")


def retry(home: Path, events: Path, exit_code: int, now: float) -> int:
    meta = json.loads((home / "review-auth.json").read_text())
    if meta.get("mode") == "api-key":
        return NO_RETRY
    if meta.get("mode") != "access-token":
        raise ValueError("Codex review auth metadata has an unknown mode")
    until = quota_until(events, exit_code, now)
    if until is None:
        return NO_RETRY
    fallback = home / "review-api-auth.json"
    if not fallback.is_file():
        print("::warning::Codex quota exhausted but API fallback is unavailable")
        return NO_API_FALLBACK
    _write_private(home / "auth.json", fallback.read_bytes())
    _write_private(home / "review-auth.json", b'{"mode":"api-key"}')
    _write_private(home / "review-quota.json", json.dumps({"until": until}).encode())
    print(
        f"Codex workspace quota exhausted; retrying once with API; UTC retry epoch {until}"
    )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=["prepare", "retry", "persist", "restore"])
    parser.add_argument("--events", type=Path)
    parser.add_argument("--exit-code", type=int, default=0)
    args = parser.parse_args()
    home = Path.home() / ".codex"
    token = os.environ.get("GH_TOKEN", "")
    store = OrgVariables(token) if token else None
    now = time.time()
    try:
        if args.mode == "prepare":
            prepare(home, store, now)
        elif args.mode == "retry":
            if args.events is None:
                parser.error("retry requires --events")
            return retry(home, args.events, args.exit_code, now)
        elif args.mode == "persist":
            marker = home / "review-quota.json"
            if not marker.is_file():
                return 0
            if not store:
                raise ControlPlaneError(
                    "Cannot persist Codex fallback: no control-plane token"
                )
            persist(store, int(json.loads(marker.read_text())["until"]), now)
        else:
            if not store:
                raise ControlPlaneError(
                    "Cannot restore Codex auth: no control-plane token"
                )
            restore(store, now)
    except (ControlPlaneError, OSError, ValueError) as error:
        annotation = "error" if args.mode == "prepare" else "warning"
        print(f"::{annotation}::{error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
