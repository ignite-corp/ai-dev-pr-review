"""Exercise the whole merge gate, replacing only GitHub transport."""

import json
import os
import shlex
import subprocess
import sys

import pytest
from test_gate_headline_drill import SKILL, gate_function

HEAD = "a" * 40
WF = "AI Code Review"
TRUSTED_SOURCE = "trusted/runner@" + "b" * 40 + ":.github/scripts/check_lifecycle.py"
T0, T1, T2, T3, T4 = [f"2026-10-22T00:0{i}:00Z" for i in range(5)]


def data():
    mode = os.environ["AUDIT_MODE"]
    marker = os.environ["AUDIT_MARKER"]
    source = f"<!-- {marker} -->\nheader\n- **a.py:1** -- - **minor** (claude): first"
    comments = [
        dict(id=1, created_at=T1, author="github-actions[bot]", type="Bot", body=source)
    ]
    comments.append(
        dict(
            id=2,
            created_at=T3,
            author="github-actions[bot]",
            type="Bot",
            body="<!-- multi-llm-review -->\n**Result: [OK] Approved | 3/3 reviewers** -- 3/3 LLM responses -- no issues",
        )
    )
    if os.environ["AUDIT_ANSWER"] == "yes":
        comments.append(
            dict(
                id=3,
                created_at=T4,
                author="another-reviewer",
                type="User",
                body=f"Dispositions for `{marker}`:\n> a.py:1\nFixed: first",
            )
        )
    runs = (
        [
            dict(
                id=i,
                workflow_id=10,
                status="completed",
                conclusion="success" if i == 12 else "failure",
                created_at=t,
                updated_at=T4,
                html_url="https://example.invalid/run",
            )
            for i, t in [(11, T0), (12, T2)]
        ]
        if mode in ["self", "both"]
        else []
    )
    checks = (
        [
            dict(
                name=WF,
                status="completed",
                conclusion="success" if t == T2 else "failure",
                started_at=t,
                completed_at=T4 if t == T2 else T1,
                app="ignite-ai-review-approver",
                details_url="https://example.invalid/check",
            )
            for t in [T0, T2]
        ]
        if mode in ["remote", "both"]
        else []
    )
    variant = os.environ.get("AUDIT_VARIANT", "")
    if variant:
        checks[0]["conclusion"] = "success"
    if variant in ["other-app", "actions-job"]:
        for row in checks:
            row["app"] = (
                "github-actions" if variant == "actions-job" else "untrusted-app"
            )
    elif variant == "running":
        checks[-1]["status"] = "in_progress"
        checks[-1]["completed_at"] = None
    elif variant == "missing-start":
        checks[0]["started_at"] = None
    elif variant in ["neutral", "failure"]:
        checks[-1]["conclusion"] = variant
    elif variant == "other-pending":
        checks.append(dict(name="lint", status="queued", app="github-actions"))
    elif variant == "no-check":
        checks = []
    if os.environ.get("AUDIT_LOCAL") == "yes":
        comments.append(
            dict(
                id=99,
                created_at=T4,
                author="independent-reviewer",
                type="User",
                body=f"## LENS verdict \u2014 **APPROVED** \u00b7 head `{HEAD}`",
            )
        )
    return comments, runs, checks


def transport(args):
    comments, runs, checks = data()
    joined = " ".join(args)
    if args[:2] == ["pr", "view"]:
        print(HEAD)
    elif args[:2] == ["run", "list"]:
        print(
            json.dumps(
                [
                    dict(
                        databaseId=row["id"],
                        workflowName=WF,
                        status=row["status"],
                        conclusion=row["conclusion"],
                        createdAt=row["created_at"],
                        updatedAt=row["updated_at"],
                    )
                    for row in runs
                ]
            )
        )
    elif "actions/workflows" in joined:
        if os.environ["AUDIT_MODE"] in ["self", "both"]:
            print(
                WF
                if ".workflows[].name" in joined
                else json.dumps(dict(id=10, name=WF))
            )
    elif "actions/runs?" in joined:
        for row in runs:
            print(json.dumps(row))
    elif "check-runs" in joined:
        if "filter=all" not in joined:
            checks = list({(row["name"], row["app"]): row for row in checks}.values())
        if "select" in joined:
            for row in checks:
                if row["status"] != "completed":
                    print(f"{row['name']}:{row['status']}")
        else:
            if os.environ.get("AUDIT_VARIANT") == "bad-json":
                print("{not json")
                return
            for row in checks:
                print(json.dumps(row))
    elif "repos/trusted/runner/contents/" in joined:
        config = os.environ.get("AUDIT_CONFIG", 'CHECK_NAME = "AI Code Review"')
        if config == "FETCH_ERROR":
            raise SystemExit(1)
        print(config)
    elif "/issues/7/comments" in joined:
        for row in comments:
            print(json.dumps(row))
    elif "/issues/7" in joined:
        print(len(comments))
    elif "/pulls/7/reviews" in joined:
        pass
    elif args[:2] == ["api", "graphql"]:
        if "reviewThreads" in joined:
            print(
                json.dumps(
                    dict(headRefOid=HEAD, reviewThreads=dict(totalCount=0, nodes=[]))
                )
            )
        else:
            print(0)
    else:
        raise AssertionError(f"unexpected gh transport call: {args}")


def gate(path):
    text = (
        path.read_text()
        .split("#### Standard merge", 1)[1]
        .split("```bash\n", 1)[1]
        .split("\n```", 1)[0]
    )
    for before, after in [
        ("OWNER/REPO", "o/r"),
        ("-F o=OWNER", "-F o=o"),
        ("-F r=REPO", "-F r=r"),
        ("gh pr view N", "gh pr view 7"),
        ("/issues/N", "/issues/7"),
        ("/pulls/N", "/pulls/7"),
        ("-F n=N", "-F n=7"),
        ("REVIEW_WF='<review workflow name>'", f"REVIEW_WF='{WF}'"),
        ("EXPECTED_REVIEWERS='<n>'", "EXPECTED_REVIEWERS='3'"),
    ]:
        text = text.replace(before, after)
    return f'gh() {{ "{sys.executable}" "{__file__}" --gh "$@"; }}\n' + text


def run_gate(
    mode,
    marker,
    answered,
    variant="",
    *,
    reference="",
    local=False,
    config='CHECK_NAME = "AI Code Review"',
    review_name=WF,
):
    env = dict(
        os.environ,
        AUDIT_MODE=mode,
        AUDIT_MARKER=marker,
        AUDIT_ANSWER=answered,
        AUDIT_VARIANT=variant,
        AUDIT_LOCAL="yes" if local else "no",
        AUDIT_CONFIG=config,
    )
    script = gate(SKILL).replace(
        "REMOTE_REVIEW_SOURCE=''", "REMOTE_REVIEW_SOURCE=" + shlex.quote(reference)
    )
    script = script.replace(
        "REVIEW_WF=" + shlex.quote(WF), "REVIEW_WF=" + shlex.quote(review_name)
    )
    return subprocess.run(
        ["bash", "-c", script], env=env, text=True, capture_output=True, timeout=30
    )


@pytest.mark.parametrize("mode", ["self", "remote", "both"])
@pytest.mark.parametrize("marker", ["round-cutoff-claude-r1", "inline-fallback-claude"])
@pytest.mark.parametrize("answered", ["no", "yes"])
def test_whole_gate_retains_old_round_findings(mode, marker, answered):
    result = run_gate(mode, marker, answered)
    if answered == "no":
        assert result.returncode == 1, result.stdout + result.stderr
        assert "BLOCK (c)" in result.stdout and marker in result.stdout
    else:
        assert result.returncode == 0, result.stdout + result.stderr
        assert "(a)(b)(d) clean" in result.stdout


@pytest.mark.parametrize(
    "variant,reason",
    [
        ("other-app", "names no workflow"),
        ("actions-job", "names no workflow"),
        ("running", "still"),
        ("missing-start", "no start edge"),
        ("neutral", "produced no usable result"),
        ("other-pending", "BLOCK (d)"),
    ],
)
def test_newest_remote_round_is_not_hidden_by_older_success(variant, reason):
    result = run_gate("remote", "round-cutoff-claude-r1", "yes", variant)
    assert result.returncode == 1, result.stdout + result.stderr
    assert reason in result.stdout


@pytest.mark.parametrize(
    "run_start,check_start,expected",
    [
        (T2, T1, "run"),
        (T1, T2, "check-run"),
        (T2, T2, "check-run"),
    ],
)
def test_selection_uses_latest_start_across_surfaces(run_start, check_start, expected):
    run = dict(
        workflowName=WF,
        status="completed",
        conclusion="success",
        createdAt=run_start,
        updatedAt=T4,
    )
    check = dict(
        name=WF,
        app="ignite-ai-review-approver",
        status="completed",
        conclusion="success",
        started_at=check_start,
        completed_at=T4,
    )
    result = subprocess.run(
        [
            "bash",
            "-c",
            gate_function("discover_round")
            + f'\nREVIEW_WF="{WF}"; CHECK_APPS=ignite-ai-review-approver; discover_round',
        ],
        input=json.dumps([run]) + "\n" + json.dumps([check]),
        text=True,
        capture_output=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.split("\x1f")[0] == expected


@pytest.mark.parametrize("answered", ["no", "yes"])
def test_failed_remote_check_is_judged_by_its_aggregate(answered):
    result = run_gate("remote", "round-cutoff-claude-r1", answered, "failure")
    assert result.returncode == (1 if answered == "no" else 0), (
        result.stdout + result.stderr
    )
    assert ("BLOCK (c)" if answered == "no" else "(a)(b)(d) clean") in result.stdout


@pytest.mark.parametrize("missing", ["completed_at", "details_url", "status"])
def test_discovery_preserves_empty_fields(missing):
    check = dict(
        name=WF,
        app="ignite-ai-review-approver",
        status="completed",
        conclusion="success",
        started_at=T0,
        completed_at=T4,
        details_url="https://example.invalid/check",
    )
    check[missing] = None
    result = subprocess.run(
        [
            "bash",
            "-c",
            gate_function("discover_round")
            + f'\nREVIEW_WF="{WF}"; CHECK_APPS=ignite-ai-review-approver; discover_round',
        ],
        input="[]\n" + json.dumps([check]),
        text=True,
        capture_output=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
    fields = result.stdout.rstrip("\n").split("\x1f")
    assert fields == [
        "check-run",
        check["status"] or "",
        "success",
        T0,
        check["completed_at"] or "",
        check["details_url"] or "",
    ]


@pytest.mark.parametrize("variant", ["no-check", "other-app"])
@pytest.mark.parametrize("local", [False, True])
def test_trusted_name_evidence_preserves_no_round_local_fallback(variant, local):
    result = run_gate(
        "remote",
        "inline-fallback-claude",
        "yes",
        variant,
        reference=TRUSTED_SOURCE,
        local=local,
    )
    assert result.returncode == (0 if local else 1), result.stdout + result.stderr
    assert ("(a)(b)(d) clean" if local else "BLOCK (a)") in result.stdout


def test_local_verdict_without_remote_name_evidence_still_blocks():
    result = run_gate("remote", "inline-fallback-claude", "yes", "no-check", local=True)
    assert result.returncode == 1 and "names no workflow" in result.stdout


def test_independent_workflow_typo_cannot_shorten_the_cutoff_window():
    result = run_gate(
        "remote",
        "round-cutoff-claude-r1",
        "yes",
        "no-check",
        reference=TRUSTED_SOURCE,
        local=True,
        review_name="AI Code Reveiw",
    )
    assert result.returncode == 1 and "does not match" in result.stdout


@pytest.mark.parametrize(
    "config",
    [
        "FETCH_ERROR",
        "OTHER_NAME = 1",
        'CHECK_NAME = "AI Code Reveiw"',
        'CHECK_NAME = "AI Code Review"\nCHECK_NAME = "AI Code Review"',
        "CHECK_NAME = get_name()",
        "CHECK_NAME = 'AI Code Review'",
        'CHECK_NAME = ""',
        'CHECK_NAME = "AI\\nCode Review"',
    ],
)
def test_unreadable_unsupported_or_mismatched_source_cannot_supply_name(config):
    result = run_gate(
        "remote",
        "inline-fallback-claude",
        "yes",
        "no-check",
        reference=TRUSTED_SOURCE,
        local=True,
        config=config,
    )
    assert result.returncode == 1, result.stdout + result.stderr
    assert "ERROR:" in result.stdout and "(a)(b)(d) clean" not in result.stdout


@pytest.mark.parametrize(
    "reference",
    [
        "trusted/runner@main:.github/scripts/check_lifecycle.py",
        "trusted/runner@" + "b" * 40 + ":../outside.py",
        "trusted/runner@" + "b" * 40 + ":/absolute.py",
        "trusted/runner@" + "b" * 40 + ":.github//check.py",
        "trusted/..@" + "b" * 40 + ":check.py",
        "trusted/runner@" + "b" * 40 + ":check.py?ref=main",
    ],
)
def test_remote_source_reference_cannot_escape_immutable_contents_endpoint(reference):
    result = run_gate(
        "remote",
        "inline-fallback-claude",
        "yes",
        "no-check",
        reference=reference,
        local=True,
    )
    assert result.returncode == 1 and "ERROR:" in result.stdout


def test_complete_check_history_keeps_earlier_same_name_cutoff_visible():
    result = run_gate("remote", "round-cutoff-claude-r1", "no")
    assert result.returncode == 1 and "BLOCK (c)" in result.stdout


def test_unreadable_check_rows_stop_at_their_own_read():
    result = run_gate("remote", "round-cutoff-claude-r1", "yes", "bad-json")
    assert (
        result.returncode == 1 and "could not read the check-run list" in result.stdout
    )


@pytest.mark.parametrize(
    "name", ["discover_round", "round_usable", "remote_review_name"]
)
def test_new_remote_helpers_extract_parse_and_lint_as_actual_skill_functions(name):
    function = gate_function(name)
    assert function.startswith(name + "() {\n") and function.endswith("\n}\n")
    subprocess.run(["bash", "-n"], input=function, text=True, check=True)
    subprocess.run(
        ["shellcheck", "-S", "warning", "-s", "bash", "-"],
        input=function,
        text=True,
        check=True,
    )


if __name__ == "__main__":
    assert sys.argv[1] == "--gh", "only GitHub transport may invoke this file"
    transport(sys.argv[2:])
