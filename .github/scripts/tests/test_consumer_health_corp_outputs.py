"""consumer-health.yml corp ``health`` job: a reviewer-error streak check
that did not run must never read as a healthy consumer (AT-2164).

Step 4 of the job ("Consecutive reviewer error verdicts") wrapped its verdict
fetch in ``|| echo '{}'`` and its jq streak computation in ``|| echo 0``, so
a ``gh`` failure (network, permissions, rate limit) or a malformed payload
was absorbed as streak 0 and the consumer was logged as ``OK`` -- the same
defect PR #135 (AT-2121) fixed in the pilot scan that used to sit in the same
file (removed in AT-3036), which the corp job did not share because it has no
``scan_errors`` / ``all_ok`` gate.

This test runs the job's consumer loop (from ``any_flag=0`` through the final
exit) under bash with ``gh`` replaced by a stub that answers the GraphQL
verdict query with a canned payload or fails, and asserts on the log lines
and exit code the run is judged by. By default the consumer under test is the
base repo itself, whose entry skips the workflow-file and pin lookups; the
stub also serves ``repos/<slug>/contents/<path>`` from a fixture tree (the
prompts, the runner's review workflow, a consumer's review workflow), and
``run_health_tail(consumers=...)`` runs a non-base entry, as
TestPromptGuidanceCheck does (AT-2844).
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

from aggregate_reviews import (
    REVIEWER_NAMES,
    _count_unreviewed_majors,
    apply_verdict_rules,
    format_summary,
)
from github_pr_support import REVIEW_MARKER

REPO_ROOT = Path(__file__).resolve().parents[3]
CONSUMER_HEALTH = REPO_ROOT / ".github" / "workflows" / "consumer-health.yml"
HEALTH_JOB = "health"
HEALTH_STEP_NAME = "Consumer health check"
LOOP_START = "any_flag=0"
BASE_REPO = "ai-dev-pr-review"
NOT_EVALUATED_NOTICE = "::notice::recovery not confirmed -- 1 consumer(s) could not be fetched or evaluated"
STREAK_THRESHOLD = 5

requires_jq = pytest.mark.skipif(shutil.which("jq") is None, reason="jq not installed")


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
        + format_summary(
            reviews,
            verdict,
            reason,
            available,
            conclusions,
            unreviewed_majors=_count_unreviewed_majors(available),
        )
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


def _health_step_tail(workflow: Path) -> str:
    doc = yaml.safe_load(workflow.read_text(encoding="utf-8"))
    steps = doc["jobs"][HEALTH_JOB]["steps"]
    run = next(s["run"] for s in steps if s.get("name") == HEALTH_STEP_NAME)
    start = run.index(LOOP_START)
    return run[start:]


# Prompt fixtures the stubbed ``gh api .../contents/...`` serves. A request
# carrying ``?ref=`` (the positive control, read at the run's own commit) is
# served from REF_ROOT, any other from HEAD_ROOT, so a test can break the
# base entry's prompt without breaking the control that reads the same path.
REF_ROOT = "ref"
HEAD_ROOT = "head"
EXAMPLE_PROMPT_DIR = "examples/prompts"
RUNNER_PROMPT_DIR = "default-prompts"
PROMPT_FILES = ("code-review-system.md", "code-review-checklist.md")
RUNNER_WORKFLOW = ".github/workflows/review.yml"
LATEST_RELEASE = "v1.15.1"
# A fixture value meaning "the fetch fails with something other than 404".
FETCH_FAILS = object()
# A fixture value meaning "the first fetch succeeds, every later one fails
# with something other than 404" -- a transient failure after discovery.
FAILS_AFTER_FIRST = object()
RUN_SHA = "0" * 40
PROMPT_GUIDANCE = REPO_ROOT / ".github" / "scripts" / "prompt_guidance.py"


def _runner_workflow(ref: str) -> str:
    return (
        "jobs:\n  review:\n"
        "    uses: ignite-corp/ai-dev-pr-review/.github/workflows/"
        f"base-ai-review-orchestrator.yml@{ref}\n"
    )


def _default_prompt_fixtures() -> dict[str, str]:
    fixtures: dict[str, str] = {}
    for name in PROMPT_FILES:
        text = (REPO_ROOT / EXAMPLE_PROMPT_DIR / name).read_text(encoding="utf-8")
        fixtures[f"{REF_ROOT}/{EXAMPLE_PROMPT_DIR}/{name}"] = text
        fixtures[f"{HEAD_ROOT}/{EXAMPLE_PROMPT_DIR}/{name}"] = text
        fixtures[f"{HEAD_ROOT}/{RUNNER_PROMPT_DIR}/{name}"] = text
    fixtures[f"{HEAD_ROOT}/{RUNNER_WORKFLOW}"] = _runner_workflow(LATEST_RELEASE)
    return fixtures


def _crashing_matcher(tmp_path: Path, crash_if: str) -> Path:
    """A stand-in for prompt_guidance.py that raises -- CPython then exits 1,
    the matcher's own "items missing" code, printing nothing -- when the
    Python expression ``crash_if`` holds, and runs the real module otherwise."""
    stub = tmp_path / "crashing_prompt_guidance.py"
    stub.write_text(
        "import runpy, sys\n"
        "from pathlib import Path\n"
        f"if {crash_if}:\n"
        "    raise AssertionError('matcher crashed')\n"
        f"sys.argv[0] = {str(PROMPT_GUIDANCE)!r}\n"
        f"runpy.run_path({str(PROMPT_GUIDANCE)!r}, run_name='__main__')\n",
        encoding="utf-8",
    )
    return stub


def run_health_tail(
    workflow: Path,
    tmp_path: Path,
    *,
    gh_stdout: str,
    gh_exit: int = 0,
    prompts: dict[str, object] | None = None,
    consumers: tuple[str, ...] = (BASE_REPO,),
    prompt_guidance: Path = PROMPT_GUIDANCE,
) -> tuple[int, str]:
    """Run the health step's consumer loop with a stubbed ``gh``.

    Only ``gh api graphql`` (the verdict fetch) returns ``gh_stdout`` with
    ``gh_exit``. ``gh api repos/<owner>/<repo>/contents/<path>`` answers from
    the prompt fixtures: by default every prompt is this repo's own
    examples/prompts, so the reviewer prompt check finds nothing to flag.
    ``prompts`` overrides fixtures by key (``<root>/<path>``): a string
    replaces the content, ``None`` makes it a 404, ``FETCH_FAILS`` makes the
    fetch fail otherwise, and ``FAILS_AFTER_FIRST`` serves the default
    review workflow body once and then fails like ``FETCH_FAILS``. Every other ``gh`` call answers an empty JSON list
    so the run-conclusion and Dependabot checks see nothing to flag.
    ``prompt_guidance`` replaces the matcher script the step runs.

    Returns (exit code, combined stdout+stderr). The job summary the step
    writes is left at ``tmp_path / "summary.md"``.
    """
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fixture_dir = tmp_path / "fixtures"
    fixtures: dict[str, object] = {**_default_prompt_fixtures(), **(prompts or {})}
    for key, value in fixtures.items():
        target = fixture_dir / key
        target.parent.mkdir(parents=True, exist_ok=True)
        if value is FETCH_FAILS:
            target.with_name(target.name + ".fail").touch()
        elif value is FAILS_AFTER_FIRST:
            target.write_text(_runner_workflow(LATEST_RELEASE), encoding="utf-8")
            target.with_name(target.name + ".once").touch()
        elif isinstance(value, str):
            target.write_text(value, encoding="utf-8")
    release_tags = tmp_path / "release_tags"
    release_tags.write_text(
        "\n".join(
            json.dumps({"name": tag, "sha": f"{n:040d}"})
            for n, tag in enumerate(
                ["v1.13.0", "v1.14.0", "v1.15.0", LATEST_RELEASE], 1
            )
        ),
        encoding="utf-8",
    )
    payload_file = tmp_path / "gh_stdout"
    payload_file.write_text(gh_stdout, encoding="utf-8")
    gh_stub = bin_dir / "gh"
    gh_stub.write_text(
        "\n".join(
            [
                "#!/bin/bash",
                'if [ "$1" = "api" ] && [ "$2" = "graphql" ]; then',
                f'  cat "{payload_file}"',
                f"  exit {gh_exit}",
                "fi",
                'if [ "$1" = "api" ] && [[ "$2" == repos/*/*/contents/* ]]; then',
                '  rel="${2#repos/*/*/contents/}"',
                f'  root="{fixture_dir}/{HEAD_ROOT}"',
                f'  [[ "$rel" == *"?ref="* ]] && root="{fixture_dir}/{REF_ROOT}"',
                '  f="${root}/${rel%%\\?*}"',
                '  if [ -e "${f}.fail" ]; then echo "gh: Bad Gateway (HTTP 502)" >&2; exit 1; fi',
                '  if [ -e "${f}.once" ]; then mv "${f}.once" "${f}.fail"; fi',
                '  if [ -f "$f" ]; then printf \'{"content":"%s"}\\n\' "$(base64 -w0 < "$f")"; exit 0; fi',
                '  echo "gh: Not Found (HTTP 404)" >&2',
                "  exit 1",
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
            f"CONSUMERS=({' '.join(consumers)})",
            f"BASE_REPO={BASE_REPO}",
            "REQUIRED_SECRETS=()",
            "STALE_BEFORE=2026-06-09T00:00:00Z",
            f"CONSECUTIVE_ERROR_THRESHOLD={STREAK_THRESHOLD}",
            "RECENT_PR_COUNT=10",
            "TIMELINE_ITEM_COUNT=10",
            f"PROMPT_GUIDANCE={prompt_guidance}",
            "CONSUMER_PROMPT_DIR=.github/prompts",
            f"BASE_PROMPT_DIR={EXAMPLE_PROMPT_DIR}",
            "RUNNER_REPO=ignite-corp/ai-dev-pr-review-runner",
            f"RUNNER_PROMPT_DIR={RUNNER_PROMPT_DIR}",
            f"RUNNER_WORKFLOW={RUNNER_WORKFLOW}",
            f"PIN_FRESHNESS={REPO_ROOT / '.github' / 'scripts' / 'pin_freshness.py'}",
            f"RELEASE_TAGS={release_tags}",
            _health_step_tail(workflow),
        ]
    )
    env = dict(os.environ)
    env["PATH"] = f"{bin_dir}{os.pathsep}{env['PATH']}"
    env["GITHUB_REPOSITORY"] = f"ignite-corp/{BASE_REPO}"
    env["GITHUB_SHA"] = RUN_SHA
    env["GITHUB_STEP_SUMMARY"] = str(tmp_path / "summary.md")
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
                "comments": {
                    "nodes": [{"body": 123, "createdAt": "2026-01-01T00:00:00Z"}]
                },
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


def _healthy_verdicts() -> str:
    return json.dumps(_graphql_payload([_verdict_body(None)] * 3))


_BASE_SYSTEM = f"{HEAD_ROOT}/{EXAMPLE_PROMPT_DIR}/code-review-system.md"
_CONTROL_SYSTEM = f"{REF_ROOT}/{EXAMPLE_PROMPT_DIR}/code-review-system.md"
_RUNNER_SYSTEM = f"{HEAD_ROOT}/{RUNNER_PROMPT_DIR}/code-review-system.md"
_RUNNER_WF = f"{HEAD_ROOT}/{RUNNER_WORKFLOW}"
# The AT-2801 drift: a pin section that knows one 404 label and not the rest.
_PARTIAL_PINS = '# System\n\n- `error="repo-not-found"` -- suggestion\n'
_NO_PIN_SECTION = "# System\n\nReview the diff.\n"
# A prompt line the crashing matcher stub raises on.
_CRASH_MARKER = "MATCHER-CRASH-MARKER"
_PARTIAL_IDS = "pin-section pin-comment-matches-false pin-absent-comment-matches pin-repo-not-visible"


@requires_jq
class TestPromptGuidanceCheck:
    """Check 6, the runner default prompts and the runner pin (AT-2844).

    The consumer under test is the base repo, whose prompts sit in
    examples/prompts -- the same path the positive control reads, at the
    run's own commit, from a separate fixture root.
    """

    def test_complete_prompts_are_ok_and_summarised_by_index(
        self, tmp_path: Path
    ) -> None:
        code, log = run_health_tail(
            CONSUMER_HEALTH, tmp_path, gh_stdout=_healthy_verdicts()
        )
        assert code == 0
        assert "prompt guidance positive control: complete" in log
        assert "Consumer 1/1: OK" in log
        assert "runner pin freshness:" in log
        summary = (tmp_path / "summary.md").read_text(encoding="utf-8")
        # A consumer row names its entry by index only (public summary).
        assert "| 1/1 | complete |" in summary.splitlines()
        assert (
            "| ignite-corp/ai-dev-pr-review-runner `default-prompts/` | complete |"
            in summary
        )
        assert (
            f"| ignite-corp/ai-dev-pr-review-runner `{RUNNER_WORKFLOW}` pin | pin fresh |"
            in summary
        )

    def test_a_prompt_with_no_pin_section_is_complete(self, tmp_path: Path) -> None:
        """MIGRATION.md: a prompt with no SHA Pins section owes nothing."""
        code, log = run_health_tail(
            CONSUMER_HEALTH,
            tmp_path,
            gh_stdout=_healthy_verdicts(),
            prompts={_BASE_SYSTEM: _NO_PIN_SECTION},
        )
        assert code == 0
        assert "Consumer 1/1: OK" in log

    def test_missing_items_flag_and_fail(self, tmp_path: Path) -> None:
        code, log = run_health_tail(
            CONSUMER_HEALTH,
            tmp_path,
            gh_stdout=_healthy_verdicts(),
            prompts={_BASE_SYSTEM: _PARTIAL_PINS},
        )
        assert code == 1
        assert f"[FLAG] entry 1/1: reviewer prompt lacks: {_PARTIAL_IDS}" in log
        assert "Consumer 1/1: 1 flag(s)" in log
        assert "::error::One or more consumers flagged" in log
        summary = (tmp_path / "summary.md").read_text(encoding="utf-8")
        assert f"| 1/1 | FLAG: lacks {_PARTIAL_IDS} |" in summary

    def test_absent_prompt_flags_and_fails(self, tmp_path: Path) -> None:
        code, log = run_health_tail(
            CONSUMER_HEALTH,
            tmp_path,
            gh_stdout=_healthy_verdicts(),
            prompts={_BASE_SYSTEM: None},
        )
        assert code == 1
        assert (
            "[FLAG] entry 1/1: no reviewer prompt at examples/prompts/code-review-system.md"
            in log
        )

    def test_an_absent_checklist_names_the_checklist(self, tmp_path: Path) -> None:
        code, log = run_health_tail(
            CONSUMER_HEALTH,
            tmp_path,
            gh_stdout=_healthy_verdicts(),
            prompts={
                f"{HEAD_ROOT}/{EXAMPLE_PROMPT_DIR}/code-review-checklist.md": None
            },
        )
        assert code == 1
        assert (
            "[FLAG] entry 1/1: no reviewer prompt at examples/prompts/code-review-checklist.md"
            in log
        )

    def test_an_unreadable_control_skips_check_6_and_runs_the_rest(
        self, tmp_path: Path
    ) -> None:
        """A transient fetch error proves nothing about the matcher path, so
        it must not take the pin, secret and streak checks down with it."""
        code, log = run_health_tail(
            CONSUMER_HEALTH,
            tmp_path,
            gh_stdout=_healthy_verdicts(),
            prompts={_CONTROL_SYSTEM: FETCH_FAILS},
        )
        assert code == 0
        assert "::notice::prompt guidance positive control could not be read" in log
        assert (
            "Consumer 1/1: OK (prompt guidance not evaluated: positive control not read)"
            in log
        )
        assert "prompt guidance: skipped (positive control not read)" in log
        assert "::error::" not in log

    def test_fetch_failure_is_not_evaluated_not_ok(self, tmp_path: Path) -> None:
        code, log = run_health_tail(
            CONSUMER_HEALTH,
            tmp_path,
            gh_stdout=_healthy_verdicts(),
            prompts={_BASE_SYSTEM: FETCH_FAILS},
        )
        assert code == 0
        assert "[WARN] entry 1/1: could not read the reviewer prompt" in log
        assert "Consumer 1/1: not evaluated" in log
        assert "Consumer 1/1: OK" not in log
        assert NOT_EVALUATED_NOTICE in log

    @pytest.mark.parametrize(
        "control",
        [_NO_PIN_SECTION, _PARTIAL_PINS, None],
        ids=["no-pin-section", "partial", "absent"],
    )
    def test_a_failed_positive_control_fails_before_any_consumer(
        self, tmp_path: Path, control: object
    ) -> None:
        """A zero needs a positive control: an empty fetch must not read
        clean, and the all-or-none group must not let the control pass by
        carrying none of it."""
        code, log = run_health_tail(
            CONSUMER_HEALTH,
            tmp_path,
            gh_stdout=_healthy_verdicts(),
            prompts={_CONTROL_SYSTEM: control},
        )
        assert code == 1
        assert "::error::prompt guidance positive control failed" in log
        assert "Consumer 1/1" not in log

    def test_runner_default_prompts_missing_items_flag_and_fail(
        self, tmp_path: Path
    ) -> None:
        code, log = run_health_tail(
            CONSUMER_HEALTH,
            tmp_path,
            gh_stdout=_healthy_verdicts(),
            prompts={_RUNNER_SYSTEM: _PARTIAL_PINS},
        )
        assert code == 1
        assert "Consumer 1/1: OK" in log
        assert f"[FLAG] runner default prompts lack: {_PARTIAL_IDS}" in log
        # The runner is not a consumer, and the error says which flagged.
        assert "::error::Runner default prompts or review pin flagged" in log
        assert "::error::One or more consumers flagged" not in log

    def test_runner_fetch_failure_is_a_notice_not_a_consumer_error(
        self, tmp_path: Path
    ) -> None:
        code, log = run_health_tail(
            CONSUMER_HEALTH,
            tmp_path,
            gh_stdout=_healthy_verdicts(),
            prompts={_RUNNER_SYSTEM: FETCH_FAILS},
        )
        assert code == 0
        assert "[WARN] could not read the runner default prompts" in log
        assert (
            "::notice::runner default prompts or review pin could not be fetched" in log
        )
        assert "recovery not confirmed" not in log

    def test_a_stale_runner_pin_flags_and_fails(self, tmp_path: Path) -> None:
        """The questions reach a runner target only through the runner's pin."""
        code, log = run_health_tail(
            CONSUMER_HEALTH,
            tmp_path,
            gh_stdout=_healthy_verdicts(),
            prompts={_RUNNER_WF: _runner_workflow("v1.13.0")},
        )
        assert code == 1
        assert "Consumer 1/1: OK" in log
        assert "[FLAG] runner .github/workflows/review.yml:" in log
        assert "::error::Runner default prompts or review pin flagged" in log

    def test_an_unreadable_runner_workflow_is_a_notice(self, tmp_path: Path) -> None:
        code, log = run_health_tail(
            CONSUMER_HEALTH,
            tmp_path,
            gh_stdout=_healthy_verdicts(),
            prompts={_RUNNER_WF: FETCH_FAILS},
        )
        assert code == 0
        assert "[WARN] could not read the runner's .github/workflows/review.yml" in log
        assert (
            "::notice::runner default prompts or review pin could not be fetched" in log
        )

    def test_an_absent_runner_workflow_flags(self, tmp_path: Path) -> None:
        """Check 2 flags a consumer with no review workflow; so does this."""
        code, log = run_health_tail(
            CONSUMER_HEALTH,
            tmp_path,
            gh_stdout=_healthy_verdicts(),
            prompts={_RUNNER_WF: None},
        )
        assert code == 1
        assert "[FLAG] runner has no .github/workflows/review.yml" in log

    @staticmethod
    def _consumer_workflow(system: str, checklist: str) -> str:
        return (
            _runner_workflow(LATEST_RELEASE)
            + f"    with:\n      code-review-system-prompt-path: {system}\n"
            + f"      code-review-checklist-path: {checklist}\n"
        )

    def test_a_custom_prompt_path_is_read_where_the_consumer_points(
        self, tmp_path: Path
    ) -> None:
        text = (REPO_ROOT / EXAMPLE_PROMPT_DIR / "code-review-system.md").read_text(
            encoding="utf-8"
        )
        code, log = run_health_tail(
            CONSUMER_HEALTH,
            tmp_path,
            gh_stdout=_healthy_verdicts(),
            consumers=("other-consumer",),
            prompts={
                f"{HEAD_ROOT}/.github/workflows/review-ai.yml": self._consumer_workflow(
                    "docs/review/system.md", "docs/review/checklist.md"
                ),
                f"{HEAD_ROOT}/docs/review/system.md": text,
                f"{HEAD_ROOT}/docs/review/checklist.md": "# Checklist\n",
            },
        )
        assert code == 0, log
        assert "prompt guidance: complete" in log
        assert "Consumer 1/1: OK" in log

    @pytest.mark.parametrize(
        "declared",
        [
            "${{ vars.PROMPT }}",
            "../../orgs/x/actions/secrets",
            "/etc/passwd",
            "a?ref=b",
        ],
        ids=["expression", "dot-dot", "absolute", "query"],
    )
    def test_a_path_that_is_not_a_plain_repo_path_is_not_fetched(
        self, tmp_path: Path, declared: str
    ) -> None:
        """The path comes from a consumer's file and goes into a URL the
        fleet App requests, so only a plain relative path is used. The
        condition never clears on its own, so it has its own notice rather
        than the transient 'recovery not confirmed' one."""
        code, log = run_health_tail(
            CONSUMER_HEALTH,
            tmp_path,
            gh_stdout=_healthy_verdicts(),
            consumers=("other-consumer",),
            prompts={
                f"{HEAD_ROOT}/.github/workflows/review-ai.yml": self._consumer_workflow(
                    declared, ".github/prompts/code-review-checklist.md"
                ),
            },
        )
        assert code == 0, log
        assert (
            "prompt guidance: not evaluated (declared prompt path is not a plain repository path)"
            in log
        )
        assert (
            "::notice::1 consumer(s) declare a prompt path this check does not read"
            in log
        )
        assert "recovery not confirmed" not in log
        assert declared not in log

    @pytest.mark.parametrize(
        "directory",
        ["docs/secret-product", ".github/prompts/secret-product"],
        ids=["elsewhere", "under-the-default-directory"],
    )
    def test_an_absent_declared_path_is_not_printed(
        self, tmp_path: Path, directory: str
    ) -> None:
        """A declared path can name a product; the public log keys an entry
        by index, so only a default path -- the exact default file, not
        anything under its directory -- is printed."""
        code, log = run_health_tail(
            CONSUMER_HEALTH,
            tmp_path,
            gh_stdout=_healthy_verdicts(),
            consumers=("other-consumer",),
            prompts={
                f"{HEAD_ROOT}/.github/workflows/review-ai.yml": self._consumer_workflow(
                    f"{directory}/system.md", f"{directory}/checklist.md"
                ),
            },
        )
        assert code == 1
        assert "[FLAG] entry 1/1: no reviewer prompt at its declared prompt path" in log
        assert "secret-product" not in log
        assert "secret-product" not in (tmp_path / "summary.md").read_text(
            encoding="utf-8"
        )

    @pytest.mark.parametrize(
        "workflow",
        [None, ""],
        ids=["no-workflow-file", "empty-workflow-body"],
    )
    def test_an_unread_review_workflow_leaves_check_6_not_evaluated(
        self, tmp_path: Path, workflow: str | None
    ) -> None:
        """Check 6 takes the prompt paths from the workflow body check 2
        read. When check 2 read none, the defaults are a guess about this
        consumer, so an absent default prompt is not something it found.
        A consumer with no review workflow is a standing condition check 2
        already flags, not a fetch to retry, so it is no scan error: the
        "recovery not confirmed" notice would otherwise never clear."""
        code, log = run_health_tail(
            CONSUMER_HEALTH,
            tmp_path,
            gh_stdout=_healthy_verdicts(),
            consumers=("other-consumer",),
            prompts={f"{HEAD_ROOT}/.github/workflows/review-ai.yml": workflow},
        )
        assert code == 1, log
        assert "[FLAG] entry 1/1: no review workflow found" in log
        assert "no reviewer prompt at" not in log
        assert (
            "[WARN] entry 1/1: prompt guidance not evaluated (no review workflow found)"
            in log
        )
        assert "recovery not confirmed" not in log
        summary = (tmp_path / "summary.md").read_text(encoding="utf-8")
        assert "| 1/1 | not evaluated (no review workflow found) |" in summary

    def test_a_review_workflow_fetch_failure_leaves_check_6_not_evaluated(
        self, tmp_path: Path
    ) -> None:
        """A transient failure on check 2's fetch is a check not performed,
        so it counts as a scan error and keeps the notice up."""
        code, log = run_health_tail(
            CONSUMER_HEALTH,
            tmp_path,
            gh_stdout=_healthy_verdicts(),
            consumers=("other-consumer",),
            prompts={f"{HEAD_ROOT}/.github/workflows/review-ai.yml": FAILS_AFTER_FIRST},
        )
        assert "workflow=review-ai.yml" in log
        assert "no reviewer prompt at" not in log
        assert (
            "[WARN] entry 1/1: prompt guidance not evaluated (review workflow could not be read)"
            in log
        )
        assert NOT_EVALUATED_NOTICE in log, log
        summary = (tmp_path / "summary.md").read_text(encoding="utf-8")
        assert "| 1/1 | not evaluated (review workflow could not be read) |" in summary

    def test_a_matcher_crash_is_not_evaluated_not_a_flag(self, tmp_path: Path) -> None:
        """CPython exits 1 on an unhandled exception, the code the matcher
        uses for "items missing"; a crash with nothing printed must not
        read as a verdict that lacks an empty list."""
        stub = _crashing_matcher(
            tmp_path,
            "any(\n"
            f"    {_CRASH_MARKER!r} in Path(a).read_text(encoding='utf-8')\n"
            "    for a in sys.argv[1:]\n"
            "    if not a.startswith('--')\n"
            ")",
        )
        code, log = run_health_tail(
            CONSUMER_HEALTH,
            tmp_path,
            gh_stdout=_healthy_verdicts(),
            prompts={_BASE_SYSTEM: f"# System\n\n{_CRASH_MARKER}\n"},
            prompt_guidance=stub,
        )
        assert code == 0, log
        assert "prompt guidance positive control: complete" in log
        assert "AssertionError: matcher crashed" in log
        assert "reviewer prompt lacks" not in log
        assert "[WARN] entry 1/1: could not read the reviewer prompt" in log
        assert "Consumer 1/1: not evaluated" in log
        assert NOT_EVALUATED_NOTICE in log

    def test_a_matcher_crash_on_the_control_fails_the_run(self, tmp_path: Path) -> None:
        """Unlike a control that could not be fetched, a matcher that
        crashes on the control proves the matcher path broken; read as a
        notice, a matcher that always crashed would stop check 6 silently."""
        stub = _crashing_matcher(tmp_path, "'--control' in sys.argv[1:]")
        code, log = run_health_tail(
            CONSUMER_HEALTH,
            tmp_path,
            gh_stdout=_healthy_verdicts(),
            prompt_guidance=stub,
        )
        assert code == 1, log
        assert "AssertionError: matcher crashed" in log
        assert "::error::prompt guidance positive control failed" in log
        assert "positive control could not be read" not in log
        assert "Consumer 1/1" not in log

    def test_an_empty_prompt_is_not_read_as_complete(self, tmp_path: Path) -> None:
        """An empty pair owes no all-or-none group, so reading it as a
        verdict would be a false clean; prepare fails on it too."""
        code, log = run_health_tail(
            CONSUMER_HEALTH,
            tmp_path,
            gh_stdout=_healthy_verdicts(),
            prompts={_BASE_SYSTEM: ""},
        )
        assert code == 0
        assert "[WARN] entry 1/1: could not read the reviewer prompt" in log
        assert "Consumer 1/1: not evaluated" in log


def test_every_expression_opener_in_the_workflow_is_closed() -> None:
    """GitHub parses ``${{`` anywhere in the file, run scripts and shell
    quoting included, and an unclosed one fails the whole workflow at load
    -- every job, not only the step. The harness above runs the step's shell
    directly and cannot see that, so this reads the raw text (#229 round 3:
    a shell pattern matching a literal expression opener did it). The whole
    file is read as one string, so an expression wrapped across lines counts
    as closed."""
    text = CONSUMER_HEALTH.read_text(encoding="utf-8")
    index = text.find("${{")
    while index != -1:
        close = text.find("}}", index + 3)
        line = text.count("\n", 0, index) + 1
        assert close != -1, f"consumer-health.yml:{line}: '${{{{' never closed"
        index = text.find("${{", close + 2)
