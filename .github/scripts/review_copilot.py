#!/usr/bin/env python3
"""Included-quota Copilot review for the optional selected Actions roster."""

from __future__ import annotations

import asyncio
import logging
import math
import os
import re
import sys
import tempfile
from pathlib import Path
from typing import Literal, Self

from copilot import CopilotClient, RuntimeConnection
from copilot.generated.rpc import AccountGetQuotaRequest, PermissionDecisionReject
from copilot.session_events import AssistantUsageData, SessionEvent
from pydantic import BaseModel, ConfigDict, model_validator

from review_status import write_verdict_json
from reviewer_prompts import build_copilot_prompt, copilot_system_prompt

REVIEW_FILE = "review-copilot.json"
CLI_VERSION = "1.0.105"
REVIEW_TIMEOUT = 300
CONTROL_TIMEOUT = 5


class ReviewIssue(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    severity: Literal["critical", "major", "minor", "suggestion"]
    file: str | None
    line: int | None
    description: str
    suggestion: str | None


class Review(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    summary: str
    status: Literal["ok", "early_exit"]
    early_exit: bool
    issues: list[ReviewIssue]

    @model_validator(mode="after")
    def consistent_status(self) -> Self:
        if (self.status == "early_exit") != self.early_exit:
            raise ValueError("status and early_exit disagree")
        return self


class ReviewFailure(Exception):
    """Only fixed, credential-free reason codes may cross the output boundary."""


def isolated_env(root: Path) -> dict[str, str]:
    directories = ("home", "copilot", "config", "cache", "tmp", "work")
    for name in directories:
        (root / name).mkdir(mode=0o700)
    return {
        "PATH": "/usr/bin:/bin",
        "HOME": str(root / "home"),
        "COPILOT_HOME": str(root / "copilot"),
        "XDG_CONFIG_HOME": str(root / "config"),
        "XDG_CACHE_HOME": str(root / "cache"),
        "TMPDIR": str(root / "tmp"),
        "LANG": "C.UTF-8",
        "COPILOT_AUTO_UPDATE": "false",
    }


async def verify_cli(cli: str, root: Path, env: dict[str, str]) -> None:
    if not Path(cli).is_absolute() or not Path(cli).is_file():
        raise ReviewFailure("cli_path_invalid")
    process = await asyncio.create_subprocess_exec(
        cli,
        "--version",
        cwd=root / "work",
        env=env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        stdout, _ = await asyncio.wait_for(process.communicate(), CONTROL_TIMEOUT)
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()
    match = re.search(
        rb"^GitHub Copilot CLI ([0-9]+\.[0-9]+\.[0-9]+)\.?\r?$",
        stdout,
        re.MULTILINE,
    )
    if process.returncode != 0 or not match or match[1].decode() != CLI_VERSION:
        raise ReviewFailure("cli_version_mismatch")


async def require_included_quota(client: CopilotClient, token: str) -> None:
    auth = await client.get_auth_status()
    if not auth.isAuthenticated:
        raise ReviewFailure("authentication_failed")
    result = await client.rpc.account.get_quota(
        AccountGetQuotaRequest(git_hub_token=token), timeout=CONTROL_TIMEOUT
    )
    # Three codes, because a target check exempts exactly one of them
    # (AT-3487): included_quota_unavailable, and only for a chat quota that
    # is well formed and used up. Overage is checked first, over every
    # bucket, so no other defect can hide it: an explicit no-overage grant
    # is required even while included quota remains, since a concurrent
    # request could consume the last included unit, and an account that
    # permits or records overage is a setting the owner must fix.
    if any(
        snapshot.overage_allowed_with_exhausted_quota is not False
        or snapshot.usage_allowed_with_exhausted_quota is not False
        or snapshot.overage != 0
        for snapshot in result.quota_snapshots.values()
    ):
        raise ReviewFailure("overage_enabled")
    # A missing or inconsistent report shows nothing about exhaustion, so it
    # fails closed without the exemption.
    chat = result.quota_snapshots.get("chat")
    if chat is None:
        raise ReviewFailure("quota_unverifiable")
    # An unlimited plan has no finite entitlement to check. Decide it from the
    # flag alone: a paid account measured 2026-11-16 reported entitlement 0
    # with the flag set, not the -1 the SDK documents. Anything but a literal
    # True, a missing field included, is a finite plan.
    if getattr(chat, "is_unlimited_entitlement", False) is True:
        return
    entitlement = chat.entitlement_requests
    used = chat.used_requests
    remaining = chat.remaining_percentage
    # The SDK's parser already refuses non-numbers and booleans.
    if (
        not all(math.isfinite(value) for value in (entitlement, used, remaining))
        or entitlement <= 0
        or used < 0
        or not 0 <= remaining <= 100
    ):
        raise ReviewFailure("quota_unverifiable")
    used_up = used >= entitlement
    if used_up != (remaining == 0):
        # One counter says exhausted and the other does not.
        raise ReviewFailure("quota_unverifiable")
    if used_up:
        raise ReviewFailure("included_quota_unavailable")


async def run_review(
    prompt: str, token: str, cli: str, root: Path, models: set[str]
) -> Review:
    env = isolated_env(root)
    await verify_cli(cli, root, env)
    client = CopilotClient(
        connection=RuntimeConnection.for_stdio(path=cli),
        mode="empty",
        github_token=token,
        use_logged_in_user=False,
        base_directory=str(root / "copilot"),
        working_directory=str(root / "work"),
        env=env,
        log_level="error",
    )
    try:
        async with asyncio.timeout(REVIEW_TIMEOUT):
            await asyncio.wait_for(client.start(), CONTROL_TIMEOUT)
            await asyncio.wait_for(
                require_included_quota(client, token), CONTROL_TIMEOUT
            )

            def record_usage(event: SessionEvent) -> None:
                data = event.data
                if isinstance(data, AssistantUsageData) and re.fullmatch(
                    r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}", data.model
                ):
                    models.add(data.model)

            session = await client.create_session(
                model="auto",
                available_tools=[],
                tools=[],
                on_permission_request=lambda _request, _invocation: (
                    PermissionDecisionReject()
                ),
                on_event=record_usage,
                working_directory=str(root / "work"),
                config_directory=str(root / "config"),
                enable_config_discovery=False,
                system_message={"mode": "replace", "content": copilot_system_prompt()},
            )
            try:
                return await session.send_and_wait_typed(
                    prompt, Review, timeout=REVIEW_TIMEOUT
                )
            except BaseException:
                # A failed/cancelled wait may leave work running. Abort is
                # best effort and must not replace the original send error.
                try:
                    await asyncio.wait_for(session.abort(), CONTROL_TIMEOUT)
                except Exception:
                    pass
                raise
    finally:
        try:
            await asyncio.wait_for(client.stop(), CONTROL_TIMEOUT)
        except Exception:
            try:
                await asyncio.wait_for(client.force_stop(), CONTROL_TIMEOUT)
            except Exception:
                print("::warning::Copilot runtime cleanup failed", file=sys.stderr)


def main() -> None:
    # The SDK forwards raw runtime stderr through its logging hierarchy.
    # Keep it off the output boundary; only our fixed failure codes leave.
    sdk_logger = logging.getLogger("copilot")
    sdk_logger.handlers = [logging.NullHandler()]
    sdk_logger.propagate = False
    models: set[str] = set()
    payload: dict[str, object]
    interrupted = False
    failure_code = ""
    try:
        token = os.environ.get("COPILOT_GITHUB_TOKEN", "").strip()
        cli = os.environ.get("COPILOT_CLI_PATH", "")
        if not token:
            raise ReviewFailure("credential_missing")
        prompt = build_copilot_prompt(
            Path("context.md").read_text(encoding="utf-8", errors="replace"),
            Path("pr.diff").read_text(encoding="utf-8", errors="replace"),
            os.environ.get("THREAD_COUNT", "0"),
            os.environ.get("EXISTING_COMMENTS", ""),
        )
        with tempfile.TemporaryDirectory(prefix="copilot-review-") as directory:
            review = asyncio.run(
                run_review(prompt, token, cli, Path(directory), models)
            )
        payload = review.model_dump()
    except (Exception, asyncio.CancelledError, KeyboardInterrupt) as exc:
        interrupted = isinstance(exc, (asyncio.CancelledError, KeyboardInterrupt))
        reason = "review_cancelled" if interrupted else "review_failed"
        if isinstance(exc, ReviewFailure):
            reason = failure_code = str(exc)
        payload = {
            "summary": f"Copilot review failed: {reason}",
            "status": "failed",
            "early_exit": False,
            "issues": [],
            "error": reason,
        }
        print(f"::warning::Copilot review failed: {reason}", file=sys.stderr)
    payload["requested_model"] = "auto"
    payload["actual_models"] = sorted(models) if models else ["unknown"]
    write_verdict_json(REVIEW_FILE, payload, indent=2)
    step_output = os.environ.get("GITHUB_OUTPUT", "")
    if failure_code and step_output:
        # The step output, not the verdict file, is what a gate may trust:
        # the file is uploaded and read back. No child of this process can
        # write here either: the CLI and runtime get isolated_env(), which
        # carries no GITHUB_OUTPUT. The workflow allowlists the code before
        # it leaves the job (AT-3487).
        with open(step_output, "a", encoding="utf-8") as output:
            output.write(f"failure_code={failure_code}\n")
    if interrupted:
        raise SystemExit(130)


if __name__ == "__main__":
    main()
