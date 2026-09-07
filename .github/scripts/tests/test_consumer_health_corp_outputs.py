"""consumer-health.yml corp ``health`` job: a reviewer-error streak check
that did not run must never read as a healthy consumer (AT-2164).

Step 4 of the job ("Consecutive reviewer error verdicts") wrapped its verdict
fetch in ``|| echo '{}'`` and its jq streak computation in ``|| echo 0``, so
a ``gh`` failure (network, permissions, rate limit) or a malformed payload
was absorbed as streak 0 and the consumer was logged as ``OK`` -- the same
defect PR #135 (AT-2121) fixed in the pilot scan of the same file, which the
corp job did not share because it has no ``scan_errors`` / ``all_ok`` gate.

This test runs the job's consumer loop (from ``any_flag=0`` through the final
exit) under bash with ``gh`` replaced by a stub that answers the GraphQL
verdict query with a canned payload or fails, and asserts on the log lines
and exit code the run is judged by. The consumer under test is the base repo
itself, whose entry skips the workflow-file and pin lookups, so the stub only
needs to answer the verdict query, the run listing and the Dependabot PR
listing.
"""

from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest
import yaml

from test_consumer_health_scan_outputs import (
    STREAK_THRESHOLD,
    _graphql_payload,
    _verdict_body,
)

REPO_ROOT = Path(__file__).resolve().parents[3]
CONSUMER_HEALTH = REPO_ROOT / ".github" / "workflows" / "consumer-health.yml"
HEALTH_JOB = "health"
HEALTH_STEP_NAME = "Consumer health check"
LOOP_START = "any_flag=0"
BASE_REPO = "ai-dev-pr-review"
NOT_EVALUATED_NOTICE = "::notice::recovery not confirmed -- 1 consumer(s) could not be fetched or evaluated"

requires_jq = pytest.mark.skipif(shutil.which("jq") is None, reason="jq not installed")


def _health_step_tail(workflow: Path) -> str:
    doc = yaml.safe_load(workflow.read_text(encoding="utf-8"))
    steps = doc["jobs"][HEALTH_JOB]["steps"]
    run = next(s["run"] for s in steps if s.get("name") == HEALTH_STEP_NAME)
    start = run.index(LOOP_START)
    return run[start:]


def run_health_tail(
    workflow: Path, tmp_path: Path, *, gh_stdout: str, gh_exit: int = 0
) -> tuple[int, str]:
    """Run the health step's consumer loop with a stubbed ``gh``.

    Only ``gh api graphql`` (the verdict fetch) returns ``gh_stdout`` with
    ``gh_exit``; every other ``gh`` call answers an empty JSON list so the
    run-conclusion and Dependabot checks see nothing to flag.

    Returns (exit code, combined stdout+stderr).
    """
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    payload_file = tmp_path / "gh_stdout"
    payload_file.write_text(gh_stdout, encoding="utf-8")
    gh_stub = bin_dir / "gh"
    gh_stub.write_text(
        "\n".join(
            [
                "#!/bin/sh",
                'if [ "$1" = "api" ] && [ "$2" = "graphql" ]; then',
                f'  cat "{payload_file}"',
                f"  exit {gh_exit}",
                "fi",
                "echo '[]'",
                "exit 0",
                "",
            ]
        ),
        encoding="utf-8",
    )
    gh_stub.chmod(gh_stub.stat().st_mode | stat.S_IXUSR)
    script = "\n".join(
        [
            # The step declares no `shell:`, so Actions runs it as
            # `bash -e {0}`; `set -u` is the step's own first line.
            "set -eu",
            f"CONSUMERS=({BASE_REPO})",
            f"BASE_REPO={BASE_REPO}",
            "REQUIRED_SECRETS=()",
            "STALE_BEFORE=2026-06-09T00:00:00Z",
            f"CONSECUTIVE_ERROR_THRESHOLD={STREAK_THRESHOLD}",
            "RECENT_PR_COUNT=10",
            "TIMELINE_ITEM_COUNT=10",
            _health_step_tail(workflow),
        ]
    )
    env = dict(os.environ)
    env["PATH"] = f"{bin_dir}{os.pathsep}{env['PATH']}"
    result = subprocess.run(
        ["bash", "-c", script],
        capture_output=True,
        text=True,
        timeout=60,
        env=env,
        check=False,
    )
    return result.returncode, result.stdout + result.stderr


@requires_jq
class TestCorpHealthOutputs:
    def test_fetch_failure_is_not_evaluated_not_ok(self, tmp_path: Path) -> None:
        code, log = run_health_tail(CONSUMER_HEALTH, tmp_path, gh_stdout="", gh_exit=1)
        assert code == 0
        assert "[WARN] entry 1/1: could not fetch recent PR data" in log
        assert "Consumer 1/1: not evaluated" in log
        assert "Consumer 1/1: OK" not in log
        assert NOT_EVALUATED_NOTICE in log

    @pytest.mark.parametrize(
        "gh_stdout",
        ["this is not json", "{}", '{"data": {"repository": null}}'],
        ids=["not-json", "empty-object", "missing-pr-list"],
    )
    def test_unexpected_shape_is_not_evaluated_not_ok(
        self, tmp_path: Path, gh_stdout: str
    ) -> None:
        # Valid JSON without the PR list used to fall through the jq
        # program's optional traversal as an empty list, i.e. streak 0.
        code, log = run_health_tail(CONSUMER_HEALTH, tmp_path, gh_stdout=gh_stdout)
        assert code == 0
        assert "[WARN] entry 1/1: recent PR data has an unexpected shape" in log
        assert "Consumer 1/1: not evaluated" in log
        assert "Consumer 1/1: OK" not in log
        assert NOT_EVALUATED_NOTICE in log
        # Log names the entry index only, never payload content.
        assert "this is not json" not in log

    def test_jq_failure_is_not_evaluated_not_ok(self, tmp_path: Path) -> None:
        # Well-shaped list whose verdict body is not a string: the streak
        # program itself fails (jq exit 5) rather than the shape check.
        payload = _graphql_payload([])
        payload["data"]["repository"]["pullRequests"]["nodes"].append(
            {
                "reviews": {"nodes": []},
                "comments": {"nodes": [{"body": 123, "createdAt": "2026-01-01T00:00:00Z"}]},
            }
        )
        code, log = run_health_tail(
            CONSUMER_HEALTH, tmp_path, gh_stdout=json.dumps(payload)
        )
        assert code == 0
        assert "[WARN] entry 1/1: streak check for reviewer" in log
        assert "Consumer 1/1: not evaluated" in log
        assert "Consumer 1/1: OK" not in log
        assert NOT_EVALUATED_NOTICE in log
        # One increment per consumer, not per reviewer.
        assert log.count("[WARN] entry 1/1: streak check for reviewer") == 1

    def test_no_recent_prs_is_ok(self, tmp_path: Path) -> None:
        # An empty PR list is a valid shape with nothing to evaluate.
        code, log = run_health_tail(
            CONSUMER_HEALTH, tmp_path, gh_stdout=json.dumps(_graphql_payload([]))
        )
        assert code == 0
        assert "Consumer 1/1: OK" in log
        assert "[WARN]" not in log

    def test_healthy_payload_is_ok_with_no_warning(self, tmp_path: Path) -> None:
        payload = _graphql_payload([_verdict_body(None)] * 3)
        code, log = run_health_tail(
            CONSUMER_HEALTH, tmp_path, gh_stdout=json.dumps(payload)
        )
        assert code == 0
        assert "Consumer 1/1: OK" in log
        assert "[WARN]" not in log
        assert "[FLAG]" not in log
        assert "recovery not confirmed" not in log

    def test_sustained_streak_still_flags_and_fails(self, tmp_path: Path) -> None:
        payload = _graphql_payload([_verdict_body("codex")] * STREAK_THRESHOLD)
        code, log = run_health_tail(
            CONSUMER_HEALTH, tmp_path, gh_stdout=json.dumps(payload)
        )
        assert code == 1
        assert "[FLAG] entry 1/1: reviewer codex error verdict" in log
        assert "Consumer 1/1: 1 flag(s)" in log
        assert "::error::One or more consumers flagged" in log
        assert "recovery not confirmed" not in log
