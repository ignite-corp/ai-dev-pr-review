"""consumer-health.yml ``health`` job: the reviewer-error streak regex must
match every failure kind this repository's reviewers can emit (AT-2552).

The streak check treats any verdict its ``is_error`` filter does not match as
a success, so a kind missing from the regex resets the streak and the dead
credential behind it is never flagged. ``action_invocation_failed`` -- what
base's Claude emitter writes when claude-code-action produced no execution
log -- rendered as ``[ ] not run (action_invocation_failed)`` on
hyuk-hur/dev-dotfiles#194 (comment 6330076622) and the regex did not match
it. The same guard existed for the pilot scan until AT-3036 removed that job.

The kinds are read from their sources, not listed here: every ``ERROR_KIND=``
and ``error: "..."`` in base-ai-review-single.yml, and every ``ERROR_*``
string constant in local_reviewer_support.py, whose verdicts the local driver
posts through the same aggregate. The regex lives inline in the job's jq
program, so the test extracts that program from the YAML and runs it under
jq against bodies rendered by ``aggregate_reviews.format_summary``, with a
neutral summary line so the match must come from the kind itself.
"""

from __future__ import annotations

import ast
import json
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

from aggregate_reviews import (
    _CONCLUSION_REASON,
    _CONCLUSION_REASON_UNKNOWN,
    ERROR_TRUNCATE_LEN,
    REVIEWER_NAMES,
    SEQUENTIAL_EARLY_EXIT_ROSTER_REASON,
    _count_unreviewed_majors,
    _failed_status_detail,
    apply_verdict_rules,
    format_summary,
)

REPO_ROOT = Path(__file__).resolve().parents[3]
CONSUMER_HEALTH = REPO_ROOT / ".github" / "workflows" / "consumer-health.yml"
BASE_SINGLE = REPO_ROOT / ".github" / "workflows" / "base-ai-review-single.yml"
LOCAL_SUPPORT = REPO_ROOT / ".github" / "scripts" / "local_reviewer_support.py"
REVIEW_GEMINI = REPO_ROOT / ".github" / "scripts" / "review_gemini.py"

# Kinds known to be emitted by each source. The positive control, held per
# source: if an extractor stops finding these, it is the extraction that
# broke, and an empty or partial set must not pass as "every kind is matched".
KNOWN_IN_BASE = {
    "action_invocation_failed",
    "cli_invocation_failed",
    "output_unparseable",
}
KNOWN_IN_LOCAL = {"cli_invocation_failed", "output_unparseable", "reviewer_crashed"}

# A missing jq skips on a developer machine. In CI it fails instead: the
# regex is only ever exercised through jq, and a silent skip there is a
# green build over zero coverage.
requires_jq_strict_in_ci = pytest.mark.skipif(
    shutil.which("jq") is None and not os.environ.get("CI"),
    reason="jq not installed",
)


def _health_jq_program() -> str:
    workflow = yaml.safe_load(CONSUMER_HEALTH.read_text(encoding="utf-8"))
    steps = workflow["jobs"]["health"]["steps"]
    script = next(s["run"] for s in steps if s.get("name") == "Consumer health check")
    match = re.search(
        r"streak=\$\(jq -r --arg r \"\$reviewer\" '(.*?)' <<<\"\$prs_json\"",
        script,
        re.DOTALL,
    )
    assert match, "health job jq streak program not found"
    return match.group(1)


# Extractors return the kinds they read plus every emitter site they could
# not read, so collection never fails and an unreadable site is reported by
# the control test below rather than as a module error.


def _base_error_kinds() -> tuple[set[str], list[str]]:
    text = BASE_SINGLE.read_text(encoding="utf-8")
    # Every emitter site must be in a shape the extraction reads: a kind
    # written any other way would drop out of the parametrized test silently.
    # `error: $kind` is the jq envelope fed from ERROR_KIND, checked with it.
    unreadable: list[str] = []
    for site, quoted in (
        (r"\bERROR_KIND=", r'\bERROR_KIND="[a-z0-9_]+"'),
        (r"(?<![\w-])error: (?!\$kind\b)\S", r'(?<![\w-])error: "[a-z0-9_]+"'),
    ):
        if len(re.findall(site, text)) != len(re.findall(quoted, text)):
            unreadable.append(f"an emitter matching {site!r} is not a quoted literal")
    kinds = set(re.findall(r'\bERROR_KIND="([a-z0-9_]+)"', text))
    kinds |= set(re.findall(r'(?<![\w-])error: "([a-z0-9_]+)"', text))
    return kinds, unreadable


def _local_error_kinds() -> tuple[set[str], list[str]]:
    tree = ast.parse(LOCAL_SUPPORT.read_text(encoding="utf-8"))
    kinds: set[str] = set()
    unreadable: list[str] = []
    for node in tree.body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            target, value = node.targets[0], node.value
        elif isinstance(node, ast.AnnAssign):
            target, value = node.target, node.value
        else:
            continue
        if isinstance(target, ast.Name) and target.id.startswith("ERROR_"):
            if isinstance(value, ast.Constant) and isinstance(value.value, str):
                kinds.add(value.value)
            else:
                unreadable.append(f"{target.id} is not a string literal")
    return kinds, unreadable


def _all_kinds() -> list[str]:
    return sorted(_base_error_kinds()[0] | _local_error_kinds()[0])


def _review(summary: str, **extra: Any) -> dict[str, Any]:
    return {
        "summary": summary,
        "status": "ok",
        "early_exit": False,
        "issues": [],
        **extra,
    }


def _failed_review(reviewer: str, kind: str) -> dict[str, Any]:
    # Neutral summary: nothing here may match the regex on its own.
    return _review(f"{reviewer} produced no review", status="failed", error=kind)


def _verdict_body(
    reviews: dict[str, dict[str, Any] | None],
    conclusions: dict[str, str] | None = None,
    *,
    sequential_bypass: bool = False,
) -> str:
    verdict, reason, available = apply_verdict_rules(reviews)
    conclusions = {name: "success" for name in REVIEWER_NAMES} | (conclusions or {})
    # format_summary opens with the review marker the streak program selects on.
    return format_summary(
        reviews,
        verdict,
        reason,
        available,
        conclusions,
        unreviewed_majors=_count_unreviewed_majors(available),
        sequential_bypass=sequential_bypass,
    )


def _healthy() -> dict[str, dict[str, Any] | None]:
    return {name: _review("Looks fine") for name in REVIEWER_NAMES}


def _streak(reviewer: str, bodies: list[str]) -> int:
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
        ["jq", "-r", "--arg", "r", reviewer, _health_jq_program()],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    )
    return int(result.stdout.strip())


def _python_failed_payloads_pair_error_with_failed_status(path: Path) -> list[str]:
    """Every dict literal with an "error" key must also set status "failed"."""
    bad: list[str] = []
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if not isinstance(node, ast.Dict):
            continue
        keys = {
            k.value: v
            for k, v in zip(node.keys, node.values)
            if isinstance(k, ast.Constant) and isinstance(k.value, str)
        }
        if "error" not in keys:
            continue
        status = keys.get("status")
        if not (isinstance(status, ast.Constant) and status.value == "failed"):
            bad.append(f"{path.name}:{node.lineno}")
    return bad


def _jq_failed_payloads_pair_error_with_failed_status(path: Path) -> list[str]:
    """Every jq `error:` field must sit in an object that sets status "failed"."""
    lines = path.read_text(encoding="utf-8").splitlines()
    bad: list[str] = []
    for i, line in enumerate(lines):
        if not re.search(r"(?<![\w-])error: \S", line):
            continue
        j = i
        while j > 0 and "{" not in lines[j]:
            j -= 1
        if not any('status: "failed"' in prior for prior in lines[j:i]):
            bad.append(f"{path.name}:{i + 1}")
    return bad


def test_every_emitter_pairs_its_error_with_failed_status() -> None:
    # The regex matches the `[ ] not run (...)` header, which aggregate_reviews
    # renders only for status "failed". An emitter that wrote a failure kind
    # with any other status would render `(partial: <kind>)` and drop out of
    # the streak, so the pairing is the invariant the regex depends on.
    bad = _jq_failed_payloads_pair_error_with_failed_status(BASE_SINGLE)
    for path in (LOCAL_SUPPORT, REVIEW_GEMINI):
        bad += _python_failed_payloads_pair_error_with_failed_status(path)
    assert not bad, f"error emitted without status failed at {bad}"
    # Positive control: the scan saw the emitters it is meant to check.
    jq_sites = sum(
        1
        for line in BASE_SINGLE.read_text(encoding="utf-8").splitlines()
        if re.search(r"(?<![\w-])error: \S", line)
    )
    assert jq_sites >= 6


def test_kind_extraction_finds_the_known_kinds() -> None:
    # Positive control for the parametrized test below: it can only be as
    # complete as the set it iterates.
    base, base_unreadable = _base_error_kinds()
    local, local_unreadable = _local_error_kinds()
    assert not base_unreadable + local_unreadable
    assert KNOWN_IN_BASE <= base
    assert KNOWN_IN_LOCAL <= local


@requires_jq_strict_in_ci
class TestHealthErrorMarkers:
    @pytest.mark.parametrize("kind", _all_kinds())
    @pytest.mark.parametrize("reviewer", sorted(REVIEWER_NAMES))
    def test_failed_kind_counts_toward_streak(self, reviewer: str, kind: str) -> None:
        reviews = _healthy()
        reviews[reviewer] = _failed_review(reviewer, kind)
        body = _verdict_body(reviews)
        assert f"[ ] not run ({kind[:ERROR_TRUNCATE_LEN]})" in body
        assert _streak(reviewer, [body]) == 1

    @pytest.mark.parametrize("reviewer", sorted(REVIEWER_NAMES))
    def test_failed_job_with_no_payload_counts_toward_streak(
        self, reviewer: str
    ) -> None:
        # A reviewer job that failed before writing any verdict renders as
        # `[ ] N/A -- failed (see logs)` -- codex's own death fails its job
        # by design (AT-2539), so this is a real dead-reviewer shape.
        reviews = _healthy()
        reviews[reviewer] = None
        body = _verdict_body(reviews, {reviewer: "failure"})
        assert f"[ ] N/A -- {_CONCLUSION_REASON['failure']}" in body
        assert _streak(reviewer, [body]) == 1

    @pytest.mark.parametrize("reviewer", sorted(REVIEWER_NAMES))
    def test_failed_status_with_no_error_counts_toward_streak(
        self, reviewer: str
    ) -> None:
        # A payload whose status is missing or unknown is normalized to
        # "failed" without an `error`, and renders the fallback detail.
        reviews = _healthy()
        failed = _review(f"{reviewer} produced no review")
        del failed["status"]
        reviews[reviewer] = failed
        body = _verdict_body(reviews)
        assert f"[ ] not run ({_failed_status_detail({})})" in body
        assert _streak(reviewer, [body]) == 1

    def test_unmapped_conclusion_with_no_payload_counts_toward_streak(self) -> None:
        # A conclusion outside the mapped four renders `no verdict (unknown)`:
        # nothing was produced and nothing explains why, so it fails closed.
        reviews = _healthy()
        reviews["codex"] = None
        body = _verdict_body(reviews, {"codex": "timed_out"})
        assert f"[ ] N/A -- {_CONCLUSION_REASON_UNKNOWN}" in body
        assert _streak("codex", [body]) == 1

    def test_bare_na_header_counts_toward_streak(self) -> None:
        # With no job conclusion, the header carries no reason at all and the
        # error kind is printed on the next line; main caught it there.
        reviews = _healthy()
        reviews["claude"] = {"status": "failed", "error": "cli_invocation_failed"}
        body = _verdict_body(reviews, {"claude": ""})
        assert "### Claude -- [ ] N/A\n_cli_invocation_failed_" in body
        assert _streak("claude", [body]) == 1

    def test_bare_na_header_without_error_line_is_not_an_error(self) -> None:
        # A caller that passes no job conclusions renders every absent
        # reviewer as a bare `[ ] N/A`; without an error line beneath it there
        # is nothing saying the reviewer failed, and main did not count it.
        reviews = _healthy()
        reviews["codex"] = None
        body = _verdict_body(reviews, {"codex": ""})
        assert "### Codex -- [ ] N/A\n" in body
        assert _streak("codex", [body]) == 0

    def test_success_with_no_payload_counts_toward_streak(self) -> None:
        # `early-exit or no-output`: the job ended green but wrote no verdict.
        # The reviewer prompt requires a verdict file even for an early exit,
        # so a missing one is an infrastructure failure, never a benign skip
        # (aggregate_reviews.py, the comment above BENIGN_ROSTER_REASONS).
        reviews = _healthy()
        reviews["codex"] = None
        body = _verdict_body(reviews, {"codex": "success"})
        assert f"[ ] N/A -- {_CONCLUSION_REASON['success']}" in body
        assert _streak("codex", [body]) == 1

    @pytest.mark.parametrize("conclusion", ["skipped", "cancelled"])
    def test_absent_payload_for_other_conclusions_is_not_an_error(
        self, conclusion: str
    ) -> None:
        # skipped and a cancelled (superseded) run are not dead credentials;
        # counting them would flag healthy consumers.
        reviews = _healthy()
        reviews["codex"] = None
        body = _verdict_body(reviews, {"codex": conclusion})
        assert _streak("codex", [body]) == 0

    @pytest.mark.parametrize(
        "summary",
        [
            "The integration job failed (see logs) before this change.",
            "Renames action_invocation_failed and reviewer_crashed handling.",
            "Explains when a reviewer reported status=failed.",
            "The review failed: the diff was truncated, so this is partial.",
            "Review failed: is how the old summary prefix read.",
            "Notes that output unparseable used to be a summary marker.",
            "Quotes the rendered header [ ] not run (action_invocation_failed).",
            "Quotes the rendered header [ ] N/A -- failed (see logs).",
            "Retried after the provider was unavailable (quota/auth error) earlier.",
        ],
    )
    def test_healthy_summary_quoting_a_marker_is_not_an_error(
        self, summary: str
    ) -> None:
        # The window includes the reviewer's own summary line, so a marker
        # phrase there must not count: only the rendered header shapes do.
        reviews = _healthy()
        reviews["claude"] = _review(summary)
        assert _streak("claude", [_verdict_body(reviews)]) == 0

    def test_sequential_early_exit_is_not_an_error(self) -> None:
        # The real benign path: no payload, job skipped, sequential bypass on.
        reviews = _healthy()
        reviews["gemini"] = None
        body = _verdict_body(reviews, {"gemini": "skipped"}, sequential_bypass=True)
        assert f"[ ] N/A -- {SEQUENTIAL_EARLY_EXIT_ROSTER_REASON}" in body
        assert _streak("gemini", [body]) == 0

    def test_early_exit_payload_is_not_an_error(self) -> None:
        reviews = _healthy()
        reviews["codex"] = _review(
            "Nothing to review", status="early_exit", early_exit=True
        )
        assert _streak("codex", [_verdict_body(reviews)]) == 0

    def test_gemini_free_form_error_counts_via_its_header(self) -> None:
        # review_gemini.py has no fixed kind: it stores the exception text as
        # `error`, which the failed header renders whatever it says.
        exc_text = (
            "400 INVALID_ARGUMENT. API key not valid. Please pass a valid API key."
        )
        reviews = _healthy()
        reviews["gemini"] = _failed_review("gemini", exc_text)
        body = _verdict_body(reviews)
        assert f"[ ] not run ({exc_text[:ERROR_TRUNCATE_LEN]})" in body
        assert _streak("gemini", [body]) == 1

    def test_healthy_verdict_ends_streak(self) -> None:
        healthy = _verdict_body(_healthy())
        assert _streak("codex", [healthy]) == 0
        reviews = _healthy()
        reviews["claude"] = _failed_review("claude", "cli_invocation_failed")
        dead_claude = _verdict_body(reviews)
        # A failing reviewer must not count for another reviewer's section.
        assert _streak("codex", [dead_claude]) == 0
        # Newest first: two failures then a success is a streak of 2.
        assert _streak("claude", [dead_claude, dead_claude, healthy]) == 2
