"""Fail every read the pr-response-cycle gate block makes, one at a time.

hyuk-hur/dev-dotfiles#204: in a shell with no `set -e` or `pipefail`,
`x=$(... | jq ...)` that fails leaves `x` empty and goes on, so a read the
gate could not make looks like a read that found nothing. Every read in
the block must stop the gate when it fails. The two list reads nested in
the run join's arguments did not until they were split out alongside this
test (#204); every other read already did, and this keeps it that way.

Nothing here parses the shell text. The block is extracted from SKILL.md
by the drill's own awk program and run whole, with `gh` and `jq` replaced
on PATH by one stub that numbers each call, answers `gh` from fixtures and
hands `jq` to the real binary. A passing run of each scenario enumerates
the reads: every call it makes is one. Then, for each k, the run is
repeated twice with call k altered -- once failing, once succeeding with
no output -- and a read is unguarded when either

  * the failure lets the gate pass, or
  * the failure is indistinguishable from the empty answer: same exit
    status, same output, same calls after it. The gate then read the
    failure as the empty result, whatever it did with that afterwards --
    a read whose emptiness some later check happens to refuse is caught
    here too, because the later check is not the read's guard.

A read added later on any path a scenario walks is enumerated by the same
passing run, so an unguarded one turns this red without anyone listing
it. A read on a path no scenario walks is not reached; the scenarios'
path markers below pin which branches each one takes.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from tests_support import DRILL, SKILL

HEAD = "0123456789abcdef0123456789abcdef01234567"
PRODUCER_SHA = "89abcdef0123456789abcdef0123456789abcdef"
PASS_LINE = "(a)(b)(d) clean"

# The drill's own extraction of the gate block (section 5l), lifted and run
# rather than restated, as tests_support.gate_function lifts its extract().
_DRILL_BLOCK_AWK = re.compile(
    r"^awk '(?P<program>/\^```bash\$/.*?)' \"\$SKILL\" > \"\$GATE_BLOCK\"$",
    re.M | re.S,
)

# The reads whose output is only printed for the human half of (c) and
# feeds no decision. Failing one must leave every later call and the exit
# status exactly as the passing run had them; each is matched by its whole
# jq program, and each must still be reached, so an entry cannot outlive
# the read it excuses.
DISPLAY_ONLY = (
    ".[] | {created_at, id, word, approve}",
    '"\\(.surface) \\(.id), state \\(.state), by \\(.author) (\\(.type)), '
    'at \\(.created_at)"',
)

STUB = r"""#!/usr/bin/env bash
# One counted call for test_gate_read_faults.py: numbered, logged with its
# argv, failed or emptied when it is call $FAULT_AT, else answered.
# The number is taken under a lock, and each call records the pipes on its
# stdin and stdout: two stubbed commands joined by a pipe run at once, so
# their order is not one read's order, and the harness refuses that run.
name=${0##*/}
exec 9> "$STUB_RUN/lock"
flock 9
n=$(( $(cat "$STUB_RUN/n" 2> /dev/null || echo 0) + 1 ))
printf '%s\n' "$n" > "$STUB_RUN/n"
printf '%s\0' "$name" "$@" > "$STUB_RUN/call.$n"
exec 9>&-
printf '%s\n%s\n' "$(readlink "/proc/$$/fd/0")" "$(readlink "/proc/$$/fd/1")" \
  > "$STUB_RUN/fds.$n"
if [ "$n" = "${FAULT_AT:-0}" ]; then
  cat > /dev/null
  case $FAULT_MODE in
    fail) [ "$name" = jq ] && exit 5; exit 1 ;;
    empty) exit 0 ;;
  esac
fi
[ "$name" = jq ] && exec "$REAL_JQ" "$@"
args=$*
jqp=''
prev=''
for a in "$@"; do [ "$prev" = --jq ] && jqp=$a; prev=$a; done
while IFS=$'\t' read -r pat file; do
  case $args in
    *"$pat"*)
      [ -n "$jqp" ] && exec "$REAL_JQ" -r "$jqp" < "$STUB_FIXTURES/$file"
      exec cat "$STUB_FIXTURES/$file" ;;
  esac
done < "$STUB_FIXTURES/routes"
printf 'stub gh: no route for %s\n' "$args" >&2
exit 97
"""

# First match wins, so a longer path sits above any path it extends.
ROUTES = (
    ("pr view", "pr.json"),
    ("actions/workflows/review.yml/runs", "runner_runs.json"),
    ("actions/workflows?", "workflows.json"),
    ("actions/runs?head_sha=", "runs.json"),
    ("/check-runs?", "check_runs.json"),
    ("/issues/7/comments", "comments.json"),
    ("repos/o/r/issues/7", "issue.json"),
    ("reviews(first:1)", "review_count.json"),
    ("reviewThreads", "threads.json"),
    ("before=c1", "fpush2.json"),
    ("timelineItems", "fpush1.json"),
    ("/pulls/7/reviews", "reviews.json"),
    ("/contents/", "producer.sh"),
    (f"repos/o/r/commits/{HEAD}", "commit.json"),
)


def _t(minute: int) -> str:
    return f"2026-01-01T10:{minute:02d}:00Z"


def _comment(cid: int, minute: int, login: str, body: str) -> dict[str, Any]:
    kind = "Bot" if login.endswith("[bot]") else "User"
    return {
        "id": cid,
        "created_at": _t(minute),
        "user": {"login": login, "type": kind},
        "body": body,
    }


def _verdict(*items: str) -> str:
    header = f"## LENS verdict \u2014 **Approved** \u00b7 head `{HEAD}`"
    return "\n".join([header, *items])


def _threads(*nodes: dict[str, Any]) -> dict[str, Any]:
    return {
        "data": {
            "repository": {
                "pullRequest": {
                    "headRefOid": HEAD,
                    "reviewThreads": {"totalCount": len(nodes), "nodes": list(nodes)},
                }
            }
        }
    }


def _review_count(n: int) -> dict[str, Any]:
    return {"data": {"repository": {"pullRequest": {"reviews": {"totalCount": n}}}}}


def _workflow_run(rid: int, wid: int, start: int, end: int) -> dict[str, Any]:
    return {
        "id": rid,
        "workflow_id": wid,
        "status": "completed",
        "conclusion": "success",
        "created_at": _t(start),
        "updated_at": _t(end),
        "html_url": f"https://example.invalid/runs/{rid}",
    }


def _check_run(name: str, app: str) -> dict[str, Any]:
    return {
        "name": name,
        "status": "completed",
        "conclusion": "success",
        "started_at": _t(0),
        "completed_at": _t(20),
        "app": {"slug": app},
        "details_url": "https://example.invalid/check",
    }


WORKFLOWS = {
    "workflows": [{"id": 1, "name": "AI Code Review"}, {"id": 2, "name": "CI"}]
}
CI_RUN = _workflow_run(101, 2, 0, 5)
TEST_CHECK = _check_run("test", "github-actions")


def _cutoff_pair() -> list[dict[str, Any]]:
    """A folded round-cutoff comment and the later reply that answers it."""
    item = "- **x.py:9** -- \U0001f7e1 **minor** (claude): the name is unclear"
    return [
        _comment(
            12,
            16,
            "github-actions[bot]",
            f"<!-- round-cutoff-claude-r3 -->\n<!-- fold-count: 1 -->\n{item}",
        ),
        _comment(
            14,
            40,
            "hyuk",
            f"round-cutoff-claude-r3\n> {item}\nWon't fix: below threshold.",
        ),
    ]


def _both_kinds() -> dict[str, Any]:
    """Self-review: a workflow-run aggregate and a local verdict, each with a major."""
    comments = [
        _comment(
            11,
            15,
            "github-actions[bot]",
            "<!-- multi-llm-review -->\n"
            "**Result: [OK] Approved with 1 unreviewed major issue(s)** -- "
            "3/3 reviewers, 3/3 LLM responses\n"
            "- + **major** `a.py:3` -- the read is unguarded",
        ),
        _comment(13, 30, "hyuk", _verdict("- + **major** `b.py:5` -- a second one")),
        _comment(15, 41, "hyuk", "> - + **major** `b.py:5` -- a second one\nFixed."),
        *_cutoff_pair(),
    ]
    thread = {
        "id": "T1",
        "isResolved": True,
        "path": "a.py",
        "line": 3,
        "originalLine": 3,
        "comments": {
            "totalCount": 2,
            "nodes": [
                {
                    "body": "+ **major** (claude): the read is unguarded",
                    "author": {"login": "github-actions", "__typename": "Bot"},
                },
                {"body": "Fixed.", "author": {"login": "hyuk", "__typename": "User"}},
            ],
        },
    }
    return {
        "literals": {},
        "fixtures": {
            "workflows.json": WORKFLOWS,
            "runs.json": {"workflow_runs": [_workflow_run(100, 1, 0, 20), CI_RUN]},
            "check_runs.json": {"check_runs": [TEST_CHECK]},
            "issue.json": {"comments": len(comments)},
            "comments.json": comments,
            "review_count.json": _review_count(0),
            "reviews.json": [],
            "threads.json": _threads(thread),
        },
        "markers": (
            "(a) verdict kind (i)",
            "(a) verdict kind (ii)",
            "(c) major answered by a replied thread on a.py",
            "(c) major answered by a later quoted reply",
            "(c) round-cutoff-claude-r3 (comment 12",
        ),
    }


def _runner_arm() -> dict[str, Any]:
    """No round here, a runner in front: the force-push walk, the committer
    fallback and the dispatch locator, then a local verdict."""
    comments = [_comment(13, 30, "hyuk", _verdict())]
    page = {
        "data": {
            "repository": {
                "pullRequest": {
                    "timelineItems": {
                        "pageInfo": {"hasPreviousPage": True, "startCursor": "c1"},
                        "nodes": [
                            {"createdAt": _t(1), "afterCommit": {"oid": "f" * 40}}
                        ],
                    }
                }
            }
        }
    }
    last = json.loads(json.dumps(page))
    timeline = last["data"]["repository"]["pullRequest"]["timelineItems"]
    timeline["pageInfo"] = {"hasPreviousPage": False, "startCursor": None}
    timeline["nodes"] = []
    return {
        "literals": {"RUNNER_REPO": "o/runner"},
        "fixtures": {
            "workflows.json": WORKFLOWS,
            "runs.json": {"workflow_runs": [CI_RUN]},
            "check_runs.json": {"check_runs": [TEST_CHECK]},
            "fpush1.json": page,
            "fpush2.json": last,
            "commit.json": {"commit": {"committer": {"date": "2026-01-01T09:00:00Z"}}},
            "runner_runs.json": {
                "workflow_runs": [
                    {
                        "id": 300,
                        "name": "review o/r#7",
                        "status": "completed",
                        "created_at": "2026-01-01T09:30:00Z",
                        "html_url": "https://example.invalid/runs/300",
                    }
                ]
            },
            "issue.json": {"comments": len(comments)},
            "comments.json": comments,
            "review_count.json": _review_count(0),
            "reviews.json": [],
            "threads.json": _threads(),
        },
        "markers": (
            "(a) kind (i): no force-push event for",
            "o/runner's run 300 completed after",
            "(a) verdict kind (ii)",
        ),
    }


def _remote_review() -> dict[str, Any]:
    """A runner-reported check run named by a trusted producer, its
    aggregate posted as an App review."""
    review = {
        "id": 9,
        "submitted_at": _t(15),
        "user": {"login": "ignite-ai-review-approver[bot]", "type": "Bot"},
        "state": "APPROVED",
        "commit_id": HEAD,
        "body": "<!-- multi-llm-review -->\n"
        "**Result: [OK] Approved** -- 3/3 reviewers, 3/3 LLM responses",
    }
    return {
        "literals": {
            "REMOTE_REVIEW_SOURCE": f"x/producer@{PRODUCER_SHA}:review.sh",
        },
        "fixtures": {
            "workflows.json": {"workflows": [{"id": 2, "name": "CI"}]},
            "runs.json": {"workflow_runs": [CI_RUN]},
            "check_runs.json": {
                "check_runs": [
                    _check_run("AI Code Review", "ignite-ai-review-approver"),
                    TEST_CHECK,
                ]
            },
            "producer.sh": 'CHECK_NAME="AI Code Review"\n',
            "issue.json": {"comments": 0},
            "comments.json": [],
            "review_count.json": _review_count(1),
            "reviews.json": [review],
            "threads.json": _threads(),
        },
        "markers": (
            "(a) kind (i) aggregate on " + HEAD + ": review 9",
            "(a) verdict kind (i)",
        ),
    }


SCENARIOS: dict[str, Callable[[], dict[str, Any]]] = {
    "both-kinds": _both_kinds,
    "runner-arm": _runner_arm,
    "remote-review": _remote_review,
}

# Every scenario fills these; a scenario's own literals override them.
LITERALS = {
    "OWNER": "o",
    "REPO": "r",
    "PR": "7",
    "REVIEW_WF": "AI Code Review",
    "EXPECTED_REVIEWERS": "3",
}


@dataclass(frozen=True)
class Outcome:
    status: int
    output: str
    calls: tuple[tuple[str, ...], ...]
    # (i, j): call i's stdout is the pipe call j reads, so the two ran at once.
    piped: tuple[tuple[int, int], ...]

    @property
    def passed(self) -> bool:
        return self.status == 0 and PASS_LINE in self.output


def gate_block() -> str:
    """The gate block, extracted by the drill's own awk program."""
    lifted = _DRILL_BLOCK_AWK.search(DRILL.read_text(encoding="utf-8"))
    assert lifted is not None, "the drill no longer extracts the gate block this way"
    result = subprocess.run(
        ["awk", lifted["program"], str(SKILL)],
        capture_output=True,
        text=True,
        check=True,
        timeout=60,
    )
    assert "runner_dispatch() {" in result.stdout, "no gate block extracted"
    return result.stdout


def fill(block: str, literals: dict[str, str]) -> str:
    """Set each literal on its own declaration line, which must exist once."""
    for name, value in literals.items():
        block, n = re.subn(
            rf"^{name}='[^']*'$", lambda _m: f"{name}='{value}'", block, flags=re.M
        )
        assert n == 1, f"{name} is declared {n} times in the gate block"
    return block


class Harness:
    def __init__(self, root: Path) -> None:
        self.root = root
        bindir = root / "bin"
        bindir.mkdir()
        stub = bindir / "stub"
        stub.write_text(STUB, encoding="utf-8")
        stub.chmod(0o755)
        for name in ("gh", "jq"):
            (bindir / name).symlink_to(stub)
        real_jq = shutil.which("jq")
        assert real_jq is not None, "jq must be on PATH"
        self.env = {
            "PATH": f"{bindir}{os.pathsep}{os.environ['PATH']}",
            "REAL_JQ": real_jq,
            "LC_ALL": "C.UTF-8",
        }

    def scenario(self, name: str, block: str) -> tuple[Path, Path]:
        spec = SCENARIOS[name]()
        where = self.root / name
        fixtures = where / "fixtures"
        fixtures.mkdir(parents=True)
        for file, content in {
            "pr.json": {"headRefOid": HEAD},
            **spec["fixtures"],
        }.items():
            text = content if isinstance(content, str) else json.dumps(content)
            (fixtures / file).write_text(text, encoding="utf-8")
        (fixtures / "routes").write_text(
            "".join(f"{pat}\t{file}\n" for pat, file in ROUTES), encoding="utf-8"
        )
        script = where / "gate.sh"
        script.write_text(
            fill(block, {**LITERALS, **spec["literals"]}), encoding="utf-8"
        )
        return script, fixtures

    def run(
        self, script: Path, fixtures: Path, fault: int = 0, mode: str = "fail"
    ) -> Outcome:
        rundir = Path(
            tempfile.mkdtemp(prefix=f"run-{fault}-{mode}-", dir=script.parent)
        )
        result = subprocess.run(
            ["bash", str(script)],
            capture_output=True,
            text=True,
            stdin=subprocess.DEVNULL,
            timeout=120,
            env={
                **self.env,
                "STUB_RUN": str(rundir),
                "STUB_FIXTURES": str(fixtures),
                "FAULT_AT": str(fault),
                "FAULT_MODE": mode,
            },
            check=False,
        )
        count = int((rundir / "n").read_text()) if (rundir / "n").exists() else 0
        calls = tuple(
            tuple((rundir / f"call.{i}").read_text(encoding="utf-8").split("\0")[:-1])
            for i in range(1, count + 1)
        )
        fds = [
            (rundir / f"fds.{i}").read_text(encoding="utf-8").split("\n")[:2]
            for i in range(1, count + 1)
        ]
        piped = tuple(
            (i + 1, j + 1)
            for i, (_, out) in enumerate(fds)
            for j, (inp, _) in enumerate(fds)
            if i != j and out.startswith("pipe:") and out == inp
        )
        return Outcome(result.returncode, result.stdout + result.stderr, calls, piped)


def describe(call: tuple[str, ...]) -> str:
    text = " ".join(call)
    return text if len(text) <= 160 else text[:157] + "..."


def is_display_only(call: tuple[str, ...]) -> bool:
    return call[0] == "jq" and any(program in call for program in DISPLAY_ONLY)


@dataclass(frozen=True)
class Sweep:
    baseline: Outcome
    unguarded: list[str]
    display_seen: set[str]


def sweep(harness: Harness, script: Path, fixtures: Path) -> Sweep:
    """Fail, then empty, each call of a passing run; name every unguarded one."""
    baseline = harness.run(script, fixtures)
    assert baseline.passed, f"the scenario does not pass as built:\n{baseline.output}"
    assert not baseline.piped, (
        "stubbed calls piped into each other run at once, so call k is not one "
        f"read: {[(describe(baseline.calls[i - 1]), j) for i, j in baseline.piped]}"
    )
    assert harness.run(script, fixtures, fault=0, mode="again").calls == (
        baseline.calls
    ), "two passing runs made different calls; the k-th call is not one read"
    count = len(baseline.calls)
    jobs = [(k, mode) for k in range(1, count + 1) for mode in ("fail", "empty")]
    with ThreadPoolExecutor(max_workers=os.cpu_count() or 4) as pool:
        done = list(pool.map(lambda job: harness.run(script, fixtures, *job), jobs))
    outcomes = {job: outcome for job, outcome in zip(jobs, done, strict=True)}
    unguarded: list[str] = []
    display_seen: set[str] = set()
    for k in range(1, count + 1):
        call = baseline.calls[k - 1]
        failed, emptied = outcomes[(k, "fail")], outcomes[(k, "empty")]
        for outcome in (failed, emptied):
            assert outcome.calls[:k] == baseline.calls[:k], (
                f"call {k} is not the same read on every run: {describe(call)}"
            )
        if is_display_only(call):
            display_seen.update(p for p in DISPLAY_ONLY if p in call)
            if (failed.status, failed.calls) != (baseline.status, baseline.calls):
                unguarded.append(
                    f"{k} display-only read changed the gate: {describe(call)}"
                )
            continue
        if failed.passed:
            unguarded.append(f"{k} failed, gate passed: {describe(call)}")
        elif (failed.status, failed.output, failed.calls[k:]) == (
            emptied.status,
            emptied.output,
            emptied.calls[k:],
        ):
            unguarded.append(f"{k} failed, read as empty: {describe(call)}")
    return Sweep(baseline, unguarded, display_seen)


@pytest.fixture(scope="module")
def block() -> str:
    return gate_block()


@pytest.mark.parametrize("name", list(SCENARIOS))
def test_every_read_stops_the_gate_when_it_fails(
    name: str, block: str, tmp_path: Path
) -> None:
    harness = Harness(tmp_path)
    script, fixtures = harness.scenario(name, block)
    result = sweep(harness, script, fixtures)
    for marker in SCENARIOS[name]()["markers"]:
        assert marker in result.baseline.output, (
            f"{name} no longer walks the path it is for: {marker!r} missing"
        )
    if name == "both-kinds":
        assert result.display_seen == set(DISPLAY_ONLY), "a display-only entry is stale"
    assert not result.unguarded, "unguarded reads:\n" + "\n".join(result.unguarded)


# The anchor the controls insert beside, and the guard one of them cuts.
UNRESOLVED_CHECK = '[ -z "$unresolved" ] || { echo "BLOCK (b): unresolved threads'
C_SEEN_GUARDED = (
    "c_seen=$(printf '%s' \"$comments\" | jq length) \\\n"
    '  || { echo "ERROR: could not count the comments read"; exit 1; }\n'
)
INSERTED_PROGRAM = "[.reviewThreads.nodes[] | select(.isResolved | not)] | length"


def _control(block: str, tmp_path: Path, old: str, new: str) -> Sweep:
    assert block.count(old) == 1, (
        f"the control's anchor is not in the block once: {old!r}"
    )
    harness = Harness(tmp_path)
    script, fixtures = harness.scenario("both-kinds", block.replace(old, new))
    return sweep(harness, script, fixtures)


def test_control_an_inserted_unguarded_read_is_named(
    block: str, tmp_path: Path
) -> None:
    """A new read with no guard of its own, beside (b)'s thread check: its
    failure is an empty count, the count reads as zero, and the gate passes."""
    inserted = (
        f"open_n=$(printf '%s' \"$snap_json\" | jq '{INSERTED_PROGRAM}')\n"
        '[ "${open_n:-0}" -eq 0 ] || { echo "BLOCK (b): $open_n open"; exit 1; }\n'
    )
    result = _control(block, tmp_path, UNRESOLVED_CHECK, inserted + UNRESOLVED_CHECK)
    assert len(result.unguarded) == 1, result.unguarded
    assert "failed, gate passed" in result.unguarded[0]
    assert INSERTED_PROGRAM in result.unguarded[0]


def test_control_a_guard_cut_from_a_read_is_named(block: str, tmp_path: Path) -> None:
    """The comment count with its guard cut: the empty count then fails the
    comparison after it, so the gate still blocks -- with a reason about a
    count it never read. Stopping is not enough; the failure must not be
    indistinguishable from the empty answer."""
    cut = "c_seen=$(printf '%s' \"$comments\" | jq length)\n"
    result = _control(block, tmp_path, C_SEEN_GUARDED, cut)
    assert len(result.unguarded) == 1, result.unguarded
    assert "failed, read as empty: jq length" in result.unguarded[0]


def test_control_two_stubbed_calls_in_one_pipeline_are_seen(
    block: str, tmp_path: Path
) -> None:
    """A gh read piped straight into jq: both stubs run at once, so their
    numbers do not name one read each, and the harness must say so."""
    piped = (
        f"c_again=$(gh api repos/o/r/issues/7 --jq . | jq .comments)\n"
        f"{UNRESOLVED_CHECK}"
    )
    assert block.count(UNRESOLVED_CHECK) == 1
    harness = Harness(tmp_path)
    script, fixtures = harness.scenario(
        "both-kinds", block.replace(UNRESOLVED_CHECK, piped)
    )
    outcome = harness.run(script, fixtures)
    assert outcome.passed, outcome.output
    assert len(outcome.piped) == 1, outcome.piped
    writer, reader = outcome.piped[0]
    assert outcome.calls[writer - 1][:2] == ("gh", "api")
    assert outcome.calls[reader - 1] == ("jq", ".comments")
    with pytest.raises(AssertionError, match="piped into each other"):
        sweep(harness, script, fixtures)
