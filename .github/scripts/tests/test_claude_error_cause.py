"""Why the Claude reviewer ended without a verdict (AT-2276).

When the claude path writes no verdict and no execution log, the net in
``base-ai-review-single.yml`` emits ``error: action_invocation_failed``.
That one string covered causes an operator fixes in different places:

* **rejected** -- the pinned claude-code-action refuses a non-human actor
  that is not in ``allowed_bots`` ("Workflow initiated by non-human actor:
  <name> (type: Bot) ... Add bot to allowed_bots list"), before any model
  call (AT-1837, AT-2272). The fix is the allow-list.
* **timeout** -- the step cap fired after the model was reached
  (``##[error]The action has timed out.``; run 38819148513, job
  116717264926, PR #194). The fix is the cap or the PR's size.
* **action_error** -- the action failed some other way before it produced
  an execution log (token setup, write-permission check, CLI install,
  environment validation).

What the record shows, measured on that timeout run, is why the step's own
outcome cannot tell these apart: the inner action step ended
``outcome=failure;conclusion=success`` and the caller's ``Run Claude
review`` step reported ``success``, exactly as a rejection does. Both are
``continue-on-error``. So the classes rest on two facts recorded by the
composite before the action runs -- the pre-check's verdict on the actor,
and the time the action step began -- plus the inner step's outcome, which
the composite now exposes.

``error`` keeps the bare string so every regex keyed on it still matches;
the class rides in ``error_cause``. A payload without a recognised cause
renders exactly as before.

Test-module hygiene: all imports belong at the top of this file per PEP 8 --
do not add ``import foo`` statements inside test function bodies.
"""

from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import time
from pathlib import Path
from typing import Any

import pytest
import yaml

from aggregate_reviews import (
    ERROR_CAUSE_TEXT,
    FAILED_DETAIL_PREFIX,
    _failed_status_detail,
    _partial_short_message,
    _payload_failure_reason,
    format_summary,
)
from test_claude_review_allowed_bots import is_allowed_bot

_REPO = Path(__file__).resolve().parents[3]
_ACTION = _REPO / ".github" / "actions" / "claude-review" / "action.yml"
_SINGLE = _REPO / ".github" / "workflows" / "base-ai-review-single.yml"

_CHECK_STEP_ID = "actor-check"
_INNER_STEP_ID = "claude-review"
_NET_STEP = "Emit Claude error verdict (no verdict file)"
_RUN_STEP = "Run Claude review"
_MARKER_NAME = "claude-review-actor-check"
_DEFAULT_BOTS = (
    "dependabot[bot],pilot-cd-dispatcher[bot],github-actions[bot],"
    "ignite-actions-token-app[bot]"
)
_KIND = "action_invocation_failed"
# The step cap every consumer runs with (claude-timeout-minutes default).
_CAP_MINUTES = 10
# The measured timeout: the inner action step ran 608548 ms against a
# 600 s cap, and the net ran about 611 s after the action step began.
_MEASURED_TIMEOUT_ELAPSED = 611

requires_jq = pytest.mark.skipif(
    shutil.which("jq") is None and not os.environ.get("CI"),
    reason="jq not installed",
)


def _composite() -> dict[str, Any]:
    return yaml.safe_load(_ACTION.read_text(encoding="utf-8"))


def _composite_step(step_id: str) -> dict[str, Any]:
    matches = [s for s in _composite()["runs"]["steps"] if s.get("id") == step_id]
    assert len(matches) == 1, step_id
    return matches[0]


def _single_step(name: str) -> dict[str, Any]:
    steps = yaml.safe_load(_SINGLE.read_text(encoding="utf-8"))["jobs"]["review"][
        "steps"
    ]
    matches = [s for s in steps if s.get("name") == name]
    assert len(matches) == 1, name
    return matches[0]


# --- the pre-check -------------------------------------------------------

_FAKE_CURL = """#!/usr/bin/env bash
# Stands in for the GitHub Users API: writes FAKE_BODY to the -o path and
# prints FAKE_CODE as curl's -w '%{http_code}'. Records the URL, its whole
# argv, and what it read as a --config file on stdin; sleeps FAKE_SLEEP.
printf '%s\\n' "$*" >> "$FAKE_ARGV_LOG"
out=""
url=""
while [ $# -gt 0 ]; do
  case "$1" in
    -o) out="$2"; shift 2 ;;
    --config) [ "$2" = "-" ] && cat >> "$FAKE_STDIN_LOG"; shift 2 ;;
    -w|-H|--max-time) shift 2 ;;
    -*) shift ;;
    *) url="$1"; shift ;;
  esac
done
sleep "${FAKE_SLEEP:-0}"
printf '%s\\n' "$url" >> "$FAKE_URL_LOG"
[ -n "$out" ] && printf '%s' "$FAKE_BODY" > "$out"
printf '%s' "$FAKE_CODE"
exit "${FAKE_EXIT:-0}"
"""


_TOKEN = "ghs_not-a-real-token-0123456789"


def _run_check(
    tmp_path: Path,
    *,
    actor: str,
    allowed: str = _DEFAULT_BOTS,
    code: str = "200",
    actor_type: str | None = "Bot",
    curl_exit: int = 0,
    curl_sleep: int = 0,
    broken: tuple[str, ...] = (),
) -> tuple[subprocess.CompletedProcess[str], dict[str, str]]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    stubs = {"curl": _FAKE_CURL, **{name: "#!/bin/sh\nexit 1\n" for name in broken}}
    for name, text in stubs.items():
        stub = bin_dir / name
        stub.write_text(text, encoding="utf-8")
        stub.chmod(stub.stat().st_mode | stat.S_IEXEC)
    runner_temp = tmp_path / "runner-temp"
    runner_temp.mkdir(exist_ok=True)
    body = json.dumps({"login": actor, "type": actor_type}) if actor_type else "{}"
    env = {
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "RUNNER_TEMP": str(runner_temp),
        "GITHUB_OUTPUT": str(tmp_path / "gh-output"),
        "FAKE_BODY": body,
        "FAKE_CODE": code,
        "FAKE_EXIT": str(curl_exit),
        "FAKE_URL_LOG": str(tmp_path / "urls"),
        "FAKE_ARGV_LOG": str(tmp_path / "argv"),
        "FAKE_STDIN_LOG": str(tmp_path / "stdin"),
        "FAKE_SLEEP": str(curl_sleep),
        "GH_TOKEN": _TOKEN,
        "ACTOR": actor,
        "ALLOWED_BOTS": allowed,
        "API_URL": "https://api.example.test",
    }
    step = _composite_step(_CHECK_STEP_ID)
    result = subprocess.run(
        ["bash", "--noprofile", "--norc", "-e", "-o", "pipefail", "-c", step["run"]],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    marker = runner_temp / _MARKER_NAME
    record: dict[str, str] = {}
    if marker.exists():
        record = dict(
            line.split("=", 1)
            for line in marker.read_text(encoding="utf-8").splitlines()
            if "=" in line
        )
    return result, record


class TestThePreCheckIsWiredBeforeTheAction:
    def test_it_runs_immediately_before_the_action_and_cannot_fail_it(self) -> None:
        ids = [s.get("id") for s in _composite()["runs"]["steps"]]
        assert ids.index(_CHECK_STEP_ID) == ids.index(_INNER_STEP_ID) - 1
        assert _composite_step(_CHECK_STEP_ID).get("continue-on-error") is True

    def test_it_reads_the_same_actor_and_list_the_action_reads(self) -> None:
        env = _composite_step(_CHECK_STEP_ID)["env"]
        # The action's actor is github.context.actor (src/github/context.ts),
        # i.e. GITHUB_ACTOR; its list is the allowed_bots input.
        assert env["ACTOR"] == "${{ github.actor }}"
        assert env["ALLOWED_BOTS"] == "${{ inputs.allowed_bots }}"
        inner = _composite_step(_INNER_STEP_ID)
        assert inner["with"]["allowed_bots"] == "${{ inputs.allowed_bots }}"

    def test_the_composite_exposes_the_inner_steps_outcome(self) -> None:
        outputs = _composite()["outputs"]
        assert outputs["inner_outcome"]["value"] == (
            f"${{{{ steps.{_INNER_STEP_ID}.outcome }}}}"
        )


@requires_jq
class TestWhatThePreCheckDecides:
    """The pre-check must agree with actor.ts checkHumanActor at the pin."""

    def test_measured_rejection_a_bot_outside_the_list_is_rejected(
        self, tmp_path: Path
    ) -> None:
        result, record = _run_check(tmp_path, actor="some-app[bot]")
        assert result.returncode == 0, result.stderr
        assert record["result"] == "rejected"
        assert record["started"].isdigit()

    def test_a_listed_bot_is_admitted(self, tmp_path: Path) -> None:
        _, record = _run_check(tmp_path, actor="dependabot[bot]")
        assert record["result"] == "admitted"

    def test_a_user_is_admitted_whatever_the_list_says(self, tmp_path: Path) -> None:
        # actor.ts consults allowed_bots only for non-User accounts.
        _, record = _run_check(tmp_path, actor="octocat", actor_type="User", allowed="")
        assert record["result"] == "admitted"

    def test_an_unresolvable_actor_is_judged_by_the_list(self, tmp_path: Path) -> None:
        # A 404 from the Users API is an App with no backing user
        # (actor.ts's catch branch): rejected unless listed.
        _, record = _run_check(tmp_path, actor="Copilot", code="404", actor_type=None)
        assert record["result"] == "rejected"
        _, record = _run_check(
            tmp_path, actor="Copilot", code="404", actor_type=None, allowed="copilot"
        )
        assert record["result"] == "admitted"

    def test_the_actor_is_url_encoded(self, tmp_path: Path) -> None:
        _run_check(tmp_path, actor="dependabot[bot]")
        url = (tmp_path / "urls").read_text(encoding="utf-8").splitlines()[-1]
        assert url == "https://api.example.test/users/dependabot%5Bbot%5D"

    @pytest.mark.parametrize(
        "broken", [("mktemp",), ("jq",), ("date",)], ids=["mktemp", "jq", "date"]
    )
    def test_a_broken_helper_still_records_when_the_action_began(
        self, tmp_path: Path, broken: tuple[str, ...]
    ) -> None:
        # Without started= a real timeout is reported as action_error, so
        # no failure ahead of the action may cost the marker.
        result, record = _run_check(tmp_path, actor="some-app[bot]", broken=broken)
        assert result.returncode == 0, result.stderr
        if broken == ("date",):
            # A broken clock costs the timestamp, not the decision.
            assert (record["result"], record["started"]) == ("rejected", "")
        else:
            assert record["result"] == "unresolved"
            assert record["started"].isdigit()

    def test_the_start_is_taken_after_the_api_call(self, tmp_path: Path) -> None:
        # The pre-check's own latency must not count against the step cap.
        before = int(time.time())
        _, record = _run_check(tmp_path, actor="dependabot[bot]", curl_sleep=2)
        assert int(record["started"]) >= before + 2

    def test_the_token_is_not_on_curls_command_line(self, tmp_path: Path) -> None:
        _run_check(tmp_path, actor="dependabot[bot]")
        assert _TOKEN not in (tmp_path / "argv").read_text(encoding="utf-8")
        assert _TOKEN in (tmp_path / "stdin").read_text(encoding="utf-8")
        assert "--max-time" in (tmp_path / "argv").read_text(encoding="utf-8")

    @pytest.mark.parametrize("code,curl_exit", [("500", 0), ("000", 6)])
    def test_an_api_failure_decides_nothing(
        self, tmp_path: Path, code: str, curl_exit: int
    ) -> None:
        result, record = _run_check(
            tmp_path, actor="some-app[bot]", code=code, curl_exit=curl_exit
        )
        assert result.returncode == 0, result.stderr
        assert record["result"] == "unresolved"

    @pytest.mark.parametrize(
        "actor,allowed",
        [
            ("dependabot[bot]", _DEFAULT_BOTS),
            ("Dependabot[BOT]", " dependabot , other "),
            ("dependabot", "dependabot[bot]"),
            ("renovate[bot]", _DEFAULT_BOTS),
            ("renovate[bot]", "*"),
            ("renovate[bot]", " * "),
            ("renovate[bot]", ""),
            ("renovate[bot]", ",,"),
            ("x[bot][bot]", "x[bot]"),
            ("renovate[bot]", "dependabot[bot],\nrenovate[bot]"),
            # The suffix is stripped as the literal "[bot]", never as a
            # bracket expression: under a glob "abct" would lose its "t".
            ("ignite-actions-token-app", "ignite-actions-token-app[bot]"),
            ("ignite-actions-token-app[bot]", "ignite-actions-token-app"),
            ("abct", "abc"),
            ("abc", "abct"),
            ("renovate[bot]", "dependabot[bot]\n,renovate[bot]\n"),
        ],
    )
    def test_the_list_match_agrees_with_the_pinned_action(
        self, tmp_path: Path, actor: str, allowed: str
    ) -> None:
        _, record = _run_check(tmp_path, actor=actor, allowed=allowed)
        expected = "admitted" if is_allowed_bot(actor, allowed) else "rejected"
        assert record["result"] == expected


# --- the net -------------------------------------------------------------


def _run_net(
    tmp_path: Path,
    *,
    step_outcome: str = "success",
    inner_outcome: str = "",
    check: str | None = None,
    elapsed: int = 5,
    exec_file: str = "",
    cap_minutes: int = _CAP_MINUTES,
) -> dict[str, Any]:
    marker = tmp_path / _MARKER_NAME
    if check is not None:
        marker.write_text(
            f"result={check}\nstarted={int(time.time()) - elapsed}\n", encoding="utf-8"
        )
    step = _single_step(_NET_STEP)
    env = {
        "PATH": os.environ["PATH"],
        "EXEC_FILE": exec_file,
        "STEP_OUTCOME": step_outcome,
        "INNER_OUTCOME": inner_outcome,
        "ACTOR_CHECK_FILE": str(marker),
        "STEP_CAP_MINUTES": str(cap_minutes),
    }
    result = subprocess.run(
        ["bash", "--noprofile", "--norc", "-e", "-o", "pipefail", "-c", step["run"]],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return json.loads((tmp_path / "review-claude.json").read_text(encoding="utf-8"))


class TestTheNetIsWired:
    def test_the_net_reads_what_the_classes_rest_on(self) -> None:
        env = _single_step(_NET_STEP)["env"]
        run_id = _single_step(_RUN_STEP)["id"]
        assert env["INNER_OUTCOME"] == f"${{{{ steps.{run_id}.outputs.inner_outcome }}}}"
        assert env["ACTOR_CHECK_FILE"] == f"${{{{ runner.temp }}}}/{_MARKER_NAME}"
        assert env["STEP_CAP_MINUTES"] == "${{ inputs.claude-timeout-minutes }}"
        # The pre-check writes to RUNNER_TEMP under the same name.
        assert f'"${{RUNNER_TEMP}}/{_MARKER_NAME}"' in _composite_step(_CHECK_STEP_ID)[
            "run"
        ]


@requires_jq
class TestWhatTheNetClassifies:
    def test_measured_rejection(self, tmp_path: Path) -> None:
        # AT-1837 / AT-2272 shape: the action refuses the bot within
        # seconds, the composite completes, the caller's step is success.
        verdict = _run_net(
            tmp_path, inner_outcome="failure", check="rejected", elapsed=5
        )
        assert verdict["error"] == _KIND
        assert verdict["error_cause"] == "rejected"
        assert verdict["status"] == "failed"

    def test_measured_timeout(self, tmp_path: Path) -> None:
        # Run 38819148513: STEP_OUTCOME success, inner failure, no log,
        # 611 s after the action step began against a 600 s cap.
        verdict = _run_net(
            tmp_path,
            inner_outcome="failure",
            check="admitted",
            elapsed=_MEASURED_TIMEOUT_ELAPSED,
        )
        assert verdict["error"] == _KIND
        assert verdict["error_cause"] == "timeout"

    def test_the_shortest_cap_can_still_time_out(self, tmp_path: Path) -> None:
        # claude-timeout-minutes=1 is valid; a fixed minute of slack left it
        # no threshold at all.
        late, early = tmp_path / "late", tmp_path / "early"
        late.mkdir()
        early.mkdir()
        verdict = _run_net(
            late, inner_outcome="failure", check="admitted", elapsed=61, cap_minutes=1
        )
        assert verdict["error_cause"] == "timeout"
        verdict = _run_net(
            early, inner_outcome="failure", check="admitted", elapsed=5, cap_minutes=1
        )
        assert verdict["error_cause"] == "action_error"

    def test_a_timeout_outranks_a_rejection_the_action_did_not_make(
        self, tmp_path: Path
    ) -> None:
        # Ten minutes in, the action plainly did not refuse the actor; the
        # pre-check disagreed with it, and the elapsed time is the fact.
        verdict = _run_net(
            tmp_path,
            inner_outcome="failure",
            check="rejected",
            elapsed=_MEASURED_TIMEOUT_ELAPSED,
        )
        assert verdict["error_cause"] == "timeout"

    def test_an_early_failure_of_an_admitted_actor_is_an_action_error(
        self, tmp_path: Path
    ) -> None:
        verdict = _run_net(tmp_path, inner_outcome="failure", check="admitted")
        assert verdict["error_cause"] == "action_error"

    @pytest.mark.parametrize(
        "kwargs",
        [
            {},
            {"inner_outcome": "success", "check": "admitted"},
            {"step_outcome": "cancelled", "check": "admitted"},
            {"inner_outcome": "", "check": "rejected"},
        ],
        ids=["nothing-recorded", "inner-success", "cancelled", "no-inner-outcome"],
    )
    def test_unknown_stays_the_bare_string(
        self, tmp_path: Path, kwargs: dict[str, Any]
    ) -> None:
        verdict = _run_net(tmp_path, **kwargs)
        assert verdict["error"] == _KIND
        assert "error_cause" not in verdict

    def test_the_detail_names_the_inner_outcome(self, tmp_path: Path) -> None:
        # The detail used to label the caller's outcome "claude-code-action
        # outcome=success" on a run where the action had failed.
        verdict = _run_net(tmp_path, inner_outcome="failure", check="rejected")
        assert "claude-code-action outcome=failure" in verdict["error_detail"]
        assert "step outcome=success" in verdict["error_detail"]

    def test_an_unparseable_log_is_not_classified(self, tmp_path: Path) -> None:
        exec_file = tmp_path / "execution.json"
        exec_file.write_text("[]", encoding="utf-8")
        verdict = _run_net(
            tmp_path,
            inner_outcome="failure",
            check="rejected",
            exec_file=str(exec_file),
        )
        assert verdict["error"] == "output_unparseable"
        assert "error_cause" not in verdict
        assert "claude-code-action outcome=failure" in verdict["error_detail"]
        assert "step outcome=success" in verdict["error_detail"]

    @pytest.mark.parametrize("cap", ["", "x", "1.5", "a b"])
    def test_a_malformed_cap_still_writes_the_verdict(
        self, tmp_path: Path, cap: str
    ) -> None:
        # The net runs under always(), including after 'Validate timeout
        # inputs' refused the value; it must still record the failure.
        step = _single_step(_NET_STEP)
        result = subprocess.run(
            ["bash", "--noprofile", "--norc", "-e", "-o", "pipefail", "-c", step["run"]],
            cwd=tmp_path,
            env={
                "PATH": os.environ["PATH"],
                "EXEC_FILE": "",
                "STEP_OUTCOME": "success",
                "INNER_OUTCOME": "failure",
                "ACTOR_CHECK_FILE": str(tmp_path / "absent"),
                "STEP_CAP_MINUTES": cap,
            },
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        assert result.returncode == 0, result.stderr
        verdict = json.loads((tmp_path / "review-claude.json").read_text(encoding="utf-8"))
        assert verdict["error_cause"] == "action_error"


# --- what the aggregate shows ---------------------------------------------


def _failed(cause: str | None) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "summary": "Claude review failed: no verdict file produced -- x",
        "status": "failed",
        "early_exit": False,
        "issues": [],
        "error": _KIND,
    }
    if cause is not None:
        payload["error_cause"] = cause
    return payload


_RENDERED = {
    "rejected": f"{_KIND}: rejected -- actor not in allowed_bots",
    "timeout": f"{_KIND}: timeout -- step time limit reached",
    "action_error": f"{_KIND}: action_error -- action failed before producing a log",
}


class TestWhatTheAggregateShows:
    @pytest.mark.parametrize("cause", sorted(_RENDERED))
    def test_each_class_is_named(self, cause: str) -> None:
        review = _failed(cause)
        assert _failed_status_detail(review) == _RENDERED[cause]
        assert _payload_failure_reason(review) == FAILED_DETAIL_PREFIX + _RENDERED[cause]
        # The check's annotations and job summary.
        assert _partial_short_message("claude", review, "success") == _RENDERED[cause]

    @pytest.mark.parametrize("cause", [None, "", "made-up", 7])
    def test_no_recognised_cause_renders_the_bare_string(self, cause: Any) -> None:
        # The cause is shown only from the closed set: a reviewer-authored
        # payload cannot put arbitrary text there.
        review = _failed(cause)
        assert _failed_status_detail(review) == _KIND
        assert _partial_short_message("claude", review, "success") == _KIND

    @pytest.mark.parametrize(
        "error", ["script_invocation_failed", "output_unparseable", "cli_invocation_failed"]
    )
    def test_a_cause_is_shown_only_on_the_kind_that_carries_it(self, error: str) -> None:
        # The classes describe the claude net's action_invocation_failed;
        # attached to any other error -- a gemini or codex payload, which an
        # LLM may author -- they would name the wrong cause.
        review = {**_failed("rejected"), "error": error}
        assert _failed_status_detail(review) == error
        assert _partial_short_message("gemini", review, "success") == error

    def test_the_comment_carries_it_on_the_headline_and_the_section(self) -> None:
        reviews: dict[str, dict[str, Any] | None] = {
            "claude": _failed("rejected"),
            "codex": {"summary": "ok", "status": "ok", "early_exit": False, "issues": []},
            "gemini": {
                "summary": "ok",
                "status": "ok",
                "early_exit": False,
                "issues": [],
            },
        }
        summary = format_summary(
            reviews,
            "approve",
            "2/3",
            ["codex", "gemini"],
            {name: "success" for name in reviews},
            unreviewed_majors=0,
        )
        assert f"claude: {_RENDERED['rejected']}" in summary
        assert f"### Claude -- [ ] not run ({_RENDERED['rejected']})" in summary


@pytest.mark.parametrize("readme", ["README.md", "README.ko.md"])
def test_both_readmes_spell_every_cause_as_rendered(readme: str) -> None:
    text = (_REPO / readme).read_text(encoding="utf-8")
    for cause, rendered in ERROR_CAUSE_TEXT.items():
        assert f"`{rendered}`" in text, (readme, cause)
