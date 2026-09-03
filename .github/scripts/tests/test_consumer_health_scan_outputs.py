"""consumer-health.yml pilot-credential-scan: a check that did not run must
never read as a confirmed recovery (AT-2121).

The scan's "Close pilot credential alert issue on recovery" step fires on
``all_ok == '1'``. Review round 3 of PR #135 found that the per-reviewer
streak computation masked any jq failure as streak 0 (``|| echo 0``): a
malformed payload made every reviewer look healthy, ``scan_errors`` stayed 0,
``all_ok`` became 1, and an open alert would have been auto-closed -- the
opposite of the step's own rule that a fetch failure is a check NOT
performed.

This test runs the tail of the scan step (the consumer loop through the
``$GITHUB_OUTPUT`` write) under bash, with ``gh`` replaced by a stub that
returns a canned GraphQL payload or fails, and asserts on the outputs the
downstream steps gate on.
"""

from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

from aggregate_reviews import REVIEWER_NAMES, apply_verdict_rules, format_summary
from github_pr_support import REVIEW_MARKER

REPO_ROOT = Path(__file__).resolve().parents[3]
CONSUMER_HEALTH = REPO_ROOT / ".github" / "workflows" / "consumer-health.yml"
SCAN_JOB = "pilot-credential-scan"
SCAN_STEP_ID = "scan"
LOOP_START = "flagged_lines=()"
CONSUMER = "repo-x"
STREAK_THRESHOLD = 5

requires_jq = pytest.mark.skipif(shutil.which("jq") is None, reason="jq not installed")


def _scan_step_tail(workflow: Path) -> str:
    doc = yaml.safe_load(workflow.read_text(encoding="utf-8"))
    steps = doc["jobs"][SCAN_JOB]["steps"]
    run = next(s["run"] for s in steps if s.get("id") == SCAN_STEP_ID)
    start = run.index(LOOP_START)
    return run[start:]


def _verdict_body(failed: str | None) -> str:
    reviews: dict[str, dict[str, Any] | None] = {
        name: {
            "summary": "Looks fine",
            "status": "ok",
            "early_exit": False,
            "issues": [],
        }
        for name in REVIEWER_NAMES
    }
    if failed is not None:
        reviews[failed] = {
            "summary": f"{failed} produced no review",
            "status": "failed",
            "early_exit": False,
            "issues": [],
            "error": "cli_invocation_failed",
        }
    verdict, reason, available = apply_verdict_rules(reviews)
    conclusions = {name: "success" for name in REVIEWER_NAMES}
    return (
        REVIEW_MARKER
        + "\n"
        + format_summary(reviews, verdict, reason, available, conclusions)
    )


def _graphql_payload(bodies: list[str]) -> dict[str, Any]:
    return {
        "data": {
            "repository": {
                "pullRequests": {
                    "nodes": [
                        {
                            "reviews": {"nodes": []},
                            "comments": {
                                "nodes": [
                                    {"body": b, "createdAt": "2026-01-01T00:00:00Z"}
                                ]
                            },
                        }
                        for b in bodies
                    ]
                }
            }
        }
    }


def run_scan_tail(
    workflow: Path, tmp_path: Path, *, gh_stdout: str, gh_exit: int = 0
) -> tuple[dict[str, str], str]:
    """Run the scan step's consumer loop with a stubbed ``gh``.

    Returns (GITHUB_OUTPUT key/values, combined stdout+stderr).
    """
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    payload_file = tmp_path / "gh_stdout"
    payload_file.write_text(gh_stdout, encoding="utf-8")
    gh_stub = bin_dir / "gh"
    gh_stub.write_text(
        f'#!/bin/sh\ncat "{payload_file}"\nexit {gh_exit}\n', encoding="utf-8"
    )
    gh_stub.chmod(gh_stub.stat().st_mode | stat.S_IXUSR)
    output_file = tmp_path / "github_output"
    output_file.touch()
    script = "\n".join(
        [
            "set -u",
            f"CONSECUTIVE_ERROR_THRESHOLD={STREAK_THRESHOLD}",
            "RECENT_PR_COUNT=10",
            "TIMELINE_ITEM_COUNT=10",
            f"WRAPPER_CONSUMERS=({CONSUMER})",
            _scan_step_tail(workflow),
        ]
    )
    env = dict(os.environ)
    env["PATH"] = f"{bin_dir}{os.pathsep}{env['PATH']}"
    env["GITHUB_OUTPUT"] = str(output_file)
    result = subprocess.run(
        ["bash", "-c", script],
        capture_output=True,
        text=True,
        timeout=60,
        env=env,
        check=True,
    )
    outputs: dict[str, str] = {}
    for line in output_file.read_text(encoding="utf-8").splitlines():
        if "=" in line and not line.startswith(("summary", "SCAN_EOF", "- ")):
            key, value = line.split("=", 1)
            outputs[key] = value
    return outputs, result.stdout + result.stderr


@requires_jq
class TestPilotScanOutputs:
    def test_malformed_payload_is_a_scan_error_not_a_recovery(
        self, tmp_path: Path
    ) -> None:
        outputs, log = run_scan_tail(
            CONSUMER_HEALTH, tmp_path, gh_stdout="this is not json"
        )
        assert outputs["flagged"] == "0"
        assert outputs["all_ok"] == "0"
        assert "recovery not confirmed" in log
        assert f"[WARN] {CONSUMER}: streak check for reviewer" in log
        # Log names the consumer and reviewer, never payload content.
        assert "this is not json" not in log

    def test_fetch_failure_is_a_scan_error(self, tmp_path: Path) -> None:
        outputs, log = run_scan_tail(CONSUMER_HEALTH, tmp_path, gh_stdout="", gh_exit=1)
        assert outputs["all_ok"] == "0"
        assert f"[WARN] {CONSUMER}: could not fetch" in log

    def test_genuine_streak_zero_confirms_recovery(self, tmp_path: Path) -> None:
        payload = _graphql_payload([_verdict_body(None)] * 3)
        outputs, log = run_scan_tail(
            CONSUMER_HEALTH, tmp_path, gh_stdout=json.dumps(payload)
        )
        assert outputs["flagged"] == "0"
        assert outputs["all_ok"] == "1"
        assert "[WARN]" not in log

    def test_sustained_streak_flags_and_blocks_recovery(self, tmp_path: Path) -> None:
        payload = _graphql_payload([_verdict_body("codex")] * STREAK_THRESHOLD)
        outputs, log = run_scan_tail(
            CONSUMER_HEALTH, tmp_path, gh_stdout=json.dumps(payload)
        )
        assert outputs["flagged"] == "1"
        assert outputs["all_ok"] == "0"
        assert f"[FLAG] {CONSUMER}: reviewer codex" in log
