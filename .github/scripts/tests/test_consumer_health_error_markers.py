"""consumer-health.yml pilot-credential-scan: the error-marker regex must
match every failure kind the reviewed pipeline actually emits (AT-2121).

The scan treats any verdict its ``is_error`` filter does not match as a
success, so a kind missing from the regex resets the streak and the dead
credential behind it is never flagged. Review of PR #135 found exactly that:
``action_invocation_failed`` was named in the job's header comment and absent
from the regex, caught only because the Claude emitter's summary happens to
start with "Claude review failed:".

The regex lives inline in the workflow's jq program, so this test extracts
that program from the YAML and runs it under jq against verdict bodies
rendered by ``aggregate_reviews.format_summary`` -- the real renderer, in
both the current ``[ ] not run (<kind>)`` header shape (AT-2123) and the
older ``[!] (partial: <kind>)`` one still present on pilot PRs inside the
scan window. The summary line is deliberately neutral so the match must come
from the kind itself, not from "review failed:" wording.

Kinds come from two places. Base's own emitter is read from the tree.
Pilot consumers run the wrapper's inline copy of that emitter
(ignite-pilot-org/ai-dev-pr-review-wrapper, ``.github/workflows/wrapper.yml``),
which adds two kinds base never emits; the wrapper is not in this tree, so
those are listed here with their source.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

from aggregate_reviews import REVIEWER_NAMES, apply_verdict_rules, format_summary
from github_pr_support import REVIEW_MARKER

REPO_ROOT = Path(__file__).resolve().parents[3]
CONSUMER_HEALTH = REPO_ROOT / ".github" / "workflows" / "consumer-health.yml"
BASE_SINGLE = REPO_ROOT / ".github" / "workflows" / "base-ai-review-single.yml"

# Emitted by wrapper.yml's "Synthesize missing reviewer verdicts" step
# (synthesize_failed_verdict / synthesize_not_run_verdict). The second is the
# shape a dead OPENAI_API_KEY takes on the wrapper: `codex login
# --with-api-key` fails, the Codex run step is skipped, and the verdict never
# reaches cli_invocation_failed.
WRAPPER_ONLY_ERROR_KINDS = (
    "reviewer_produced_no_verdict",
    "reviewer_not_run: authentication failed",
)

requires_jq = pytest.mark.skipif(shutil.which("jq") is None, reason="jq not installed")


def _pilot_scan_jq_program() -> str:
    text = CONSUMER_HEALTH.read_text(encoding="utf-8")
    job = text.split("\n  pilot-credential-scan:\n", 1)[1].split("\n  health:\n", 1)[0]
    match = re.search(
        r"jq -r --arg r \"\$reviewer\" '(.*?)' <<<\"\$prs_json\"", job, re.DOTALL
    )
    assert match, "pilot-credential-scan jq streak program not found"
    return match.group(1)


def _base_error_kinds() -> set[str]:
    text = BASE_SINGLE.read_text(encoding="utf-8")
    kinds = set(re.findall(r'ERROR_KIND="([a-z_]+)"', text))
    kinds |= set(re.findall(r'error: "([a-z_]+)"', text))
    assert kinds, "no error kinds found in base-ai-review-single.yml"
    return kinds


def _failed_review(reviewer: str, kind: str) -> dict[str, Any]:
    # Neutral summary: nothing here may match the regex on its own.
    return {
        "summary": f"{reviewer} produced no review",
        "status": "failed",
        "early_exit": False,
        "issues": [],
        "error": kind,
    }


def _healthy_review() -> dict[str, Any]:
    return {"summary": "Looks fine", "status": "ok", "early_exit": False, "issues": []}


def _verdict_body(reviews: dict[str, dict[str, Any] | None]) -> str:
    verdict, reason, available = apply_verdict_rules(reviews)
    conclusions = {name: "success" for name in REVIEWER_NAMES}
    return (
        REVIEW_MARKER
        + "\n"
        + format_summary(reviews, verdict, reason, available, conclusions)
    )


def _legacy_partial_body(reviewer: str, kind: str) -> str:
    # Pre-AT-2123 section header, as rendered on pilot PRs before v1.7.2.
    return "\n".join(
        [
            REVIEW_MARKER,
            "**Result: [OK] Approved** -- 2/3 LLM responses",
            "",
            "---",
            "",
            f"### {reviewer.title()} -- 0 issue(s) [!] (partial: {kind[:60]})",
            f"{reviewer} produced no review",
            "",
        ]
    )


def _streak(program: str, reviewer: str, bodies: list[str]) -> int:
    payload = {
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
    result = subprocess.run(
        ["jq", "-r", "--arg", "r", reviewer, program],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    )
    return int(result.stdout.strip())


def _all_kinds() -> list[str]:
    return sorted(_base_error_kinds()) + list(WRAPPER_ONLY_ERROR_KINDS)


@requires_jq
class TestPilotScanErrorMarkers:
    def test_base_emitter_kinds_are_the_known_set(self) -> None:
        # If base grows a kind, WRAPPER_ONLY_ERROR_KINDS and the header
        # comment in consumer-health.yml need a look, not just the regex.
        assert _base_error_kinds() == {
            "action_invocation_failed",
            "cli_invocation_failed",
            "output_unparseable",
        }

    @pytest.mark.parametrize("kind", _all_kinds())
    @pytest.mark.parametrize("reviewer", sorted(REVIEWER_NAMES))
    def test_failed_kind_counts_toward_streak(self, reviewer: str, kind: str) -> None:
        reviews: dict[str, dict[str, Any] | None] = {
            name: _healthy_review() for name in REVIEWER_NAMES
        }
        reviews[reviewer] = _failed_review(reviewer, kind)
        body = _verdict_body(reviews)
        assert f"[ ] not run ({kind[:60]})" in body
        assert _streak(_pilot_scan_jq_program(), reviewer, [body]) == 1

    @pytest.mark.parametrize("kind", _all_kinds())
    def test_legacy_partial_header_counts_toward_streak(self, kind: str) -> None:
        body = _legacy_partial_body("codex", kind)
        assert _streak(_pilot_scan_jq_program(), "codex", [body]) == 1

    def test_healthy_verdict_ends_streak(self) -> None:
        program = _pilot_scan_jq_program()
        reviews: dict[str, dict[str, Any] | None] = {
            name: _healthy_review() for name in REVIEWER_NAMES
        }
        healthy = _verdict_body(reviews)
        assert _streak(program, "codex", [healthy]) == 0
        # A failing reviewer must not count for another reviewer's section.
        reviews["claude"] = _failed_review("claude", "action_invocation_failed")
        dead_claude = _verdict_body(reviews)
        assert _streak(program, "codex", [dead_claude]) == 0
        # Newest first: two failures then a success is a streak of 2.
        assert _streak(program, "claude", [dead_claude, dead_claude, healthy]) == 2
