#!/usr/bin/env python3
"""Claude code review via the local `claude` CLI.

The Actions path runs anthropics/claude-code-action through the
`.github/actions/claude-review` composite; there is no runner here, so this
module drives the CLI directly. Everything the composite decides that is not
about being an Action is kept and READ FROM IT rather than restated: the
prompt (reviewer_prompts.py), `--model`, `--allowedTools`, the step's env,
and the verdict-file fallback.

Not carried over: the OAuth-vs-API-key precedence and the usage-limit auth
switch. Both exist to choose between two secrets held by an org; the CLI
here uses whatever credential the operator is already logged in with.

Credentials are INHERITED, never built. Naming environment variables here
would make this module the thing that decides which credential counts, and
that is the operator's decision (OAuth, API key, a proxy). What is inherited
is bounded, though: the driver hands this process an allowlist rather than
its own environment, so an exported ANTHROPIC_API_KEY reaches the CLI only
when the operator named it (review_pr_local.reviewer_env). A logged-in
`claude` is unaffected -- HOME is forwarded and ~/.claude is where it looks.
That bound is the driver's alone, so run by anything else -- by hand, by a
wrapper, by a Makefile -- this module warns that the CLI is getting the
caller's whole environment instead.

Reads pr.diff and context.md from the current directory (the prompt tells
the CLI to) and writes review-claude.json.

Env: CLAUDE_MODEL, THREAD_COUNT, EXISTING_COMMENTS, API_TIMEOUT_MS.
"""

from __future__ import annotations

import json
import math
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

import yaml

from extract_claude_review import extract_review
from local_review_config import LocalConfig
from local_reviewer_support import (
    ERROR_CLI_FAILED,
    ERROR_UNPARSEABLE,
    EXIT_NOT_INSTALLED,
    EXIT_SPAWN_FAILED,
    EXIT_TIMED_OUT,
    captured_text,
    error_verdict,
    guarded_main,
    spawn_exit_reason,
    warn_unless_driver_spawned,
    write_verdict,
)
from review_status import stamp_model_status
from reviewer_prompts import build_claude_prompt

REVIEW_FILE = "review-claude.json"
RUN_LOG = "claude-run.log"
# The CLI's JSON result, reshaped into the message list extract_claude_review
# reads, so the Actions fallback path is reused rather than reimplemented.
EXEC_FILE = "claude-exec.json"
ACTION_YML = (
    Path(__file__).resolve().parent.parent / "actions" / "claude-review" / "action.yml"
)
# The composite step whose env reaches the CLI process on a runner.
_ACTION_STEP_ID = "claude-review"
_TIMEOUT_ENV = "API_TIMEOUT_MS"
# How long the Python bound sits ABOVE the CLI's own request cap. The order
# is the point, not the size: when ONE request is what stalls -- the measured
# AT-1601 failure mode -- the CLI aborts first and gets to write its own
# verdict, and this kill is only for a CLI that does not come back from its
# own abort. Reversed, Python would kill a CLI that was about to explain
# itself. The ordering stops there, and the comment says so because the code
# cannot: API_TIMEOUT_MS is a PER-REQUEST cap (action.yml says as much), while
# the CLI is an agent loop that reads pr.diff, reads context.md and writes a
# verdict, so several requests can each finish well inside the cap and still
# take the run past cap + this -- Python then kills a healthy CLI and the
# operator gets output_unparseable instead of the CLI's own account. A
# separate whole-run budget is the fix, and it is not invented here: no run
# has been measured for one, and this bound also feeds the driver through
# shim_budget_sec, so a number nobody has measured would move that too.
_KILL_GRACE_SEC = 30
# Only the runner evaluates an Actions expression. The reference has to be
# the WHOLE value, hence `fullmatch`: `${{ inputs.api_timeout_ms }}0` is a
# composed string, and resolving it to the input's default would drop the
# `0`. Stripped first because a YAML folded or block scalar appends a
# newline that is no part of the expression; unstripped it matches nothing
# and the literal `${{ ... }}` is what reaches the CLI's environment.
_INPUT_REF_RE = re.compile(r"\$\{\{\s*inputs\.([A-Za-z0-9_-]+)\s*\}\}")


def _action() -> dict[str, Any]:
    """The composite's parsed YAML.

    Unguarded on purpose. Every failure it can have becomes a
    `reviewer_crashed` verdict with a traceback naming this file, via
    guarded_main. The discarded version had a dedicated exception tuple and
    exit code here, under a comment claiming to list "everything that can
    raise"; a reviewer found AttributeError missing from it. A list that has
    to be complete to be correct is the wrong shape when the composite ships
    in this repository and CI already asserts it parses (record 4-B).
    """
    return yaml.safe_load(ACTION_YML.read_text(encoding="utf-8"))


def allowed_tools() -> str:
    """The composite's `--allowedTools` value, read from its own default."""
    return _action()["inputs"]["allowed_tools"]["default"]


def cli_env() -> dict[str, str]:
    """The env the composite puts on the step that runs the CLI.

    It sets API_TIMEOUT_MS and CLAUDE_STREAM_IDLE_TIMEOUT_MS there because
    the CLI's byte-level watchdog defaults to 180000 ms and aborted slow
    reviews well inside the 10-minute budget (AT-1601). Nothing sets them on
    a developer's machine. Read, not restated: both values are
    `${{ inputs.api_timeout_ms }}`, so this resolves the one indirection and
    follows a change to either.
    """
    action = _action()
    for step in action["runs"]["steps"]:
        if step.get("id") != _ACTION_STEP_ID:
            continue
        resolved: dict[str, str] = {}
        for key, value in (step.get("env") or {}).items():
            ref = _INPUT_REF_RE.fullmatch(str(value).strip())
            resolved[key] = str(
                action["inputs"][ref.group(1)]["default"] if ref else value
            )
        return resolved
    raise KeyError(f"{ACTION_YML} has no step with id {_ACTION_STEP_ID!r}")


def cli_timeout_sec(env: dict[str, str]) -> int:
    """The Python bound on the CLI, derived from the cap the CLI is given.

    Pinning 600 while API_TIMEOUT_MS stayed operator-settable meant an
    operator who raised their budget to 900000 was told "did not finish
    within 600s" -- Python killing the CLI 300 seconds before its own
    deadline. The pair held only at the default, the one value nobody has to
    be told about (record 2-D). An unusable value falls back to the
    composite's default, not to a number written here.
    """
    raw = env.get(_TIMEOUT_ENV, "")
    try:
        milliseconds = int(str(raw).strip())
    except ValueError:
        milliseconds = 0
    if milliseconds <= 0:
        print(
            f"::warning::{_TIMEOUT_ENV}={raw!r} is not a positive integer;"
            " using the composite's default",
            file=sys.stderr,
        )
        milliseconds = int(_action()["inputs"]["api_timeout_ms"]["default"])
    return math.ceil(milliseconds / 1000) + _KILL_GRACE_SEC


def cli_environ() -> dict[str, str]:
    """The environment the CLI actually runs with.

    The composite's values go UNDER the inherited environment, not over it.
    They stand in for a runner that is not here to set them, which is
    position 3 of the documented order: process environment, then config
    file, then the value parsed out of the workflow files. Merged the other
    way, an operator who exported API_TIMEOUT_MS had it silently replaced by
    the default cli_env() exists to supply in its absence.
    """
    return {**cli_env(), **os.environ}


def shim_budget_sec() -> int:
    """The longest this shim can take before it stops writing a verdict.

    Asked by the driver, which bounds the shim from outside and must stay
    above this: past its own bound the shim still writes an error verdict
    and keeps the partial transcript, whereas the driver's bound is a
    SIGTERM that leaves neither. There is one budget and this is it, rather
    than a number in the driver that happens to agree at the default.
    """
    return cli_timeout_sec(cli_environ())


def _exit_reason(exit_code: int, timeout_sec: int) -> str:
    return spawn_exit_reason(exit_code, timeout_sec) or f"CLI exited {exit_code}"


def run_cli(prompt: str, model: str) -> tuple[int, str, int]:
    """Run the CLI; return (exit code, raw stdout, the bound that applied)."""
    env = cli_environ()
    timeout_sec = cli_timeout_sec(env)
    argv = [
        "claude",
        "--print",
        "--model",
        model,
        "--allowedTools",
        allowed_tools(),
        "--output-format",
        "json",
    ]
    try:
        result = subprocess.run(
            argv,
            input=prompt,
            capture_output=True,
            text=True,
            timeout=timeout_sec,
            env=env,
        )
    except FileNotFoundError:
        # Kept distinct from a timeout: "not installed" and "ran for ten
        # minutes" are different problems with different fixes, and both
        # used to reach the operator as the same empty output.
        print(
            "::warning::the claude CLI is not installed or not on PATH",
            file=sys.stderr,
        )
        return EXIT_NOT_INSTALLED, "", timeout_sec
    except OSError as exc:
        print(f"::warning::the claude CLI could not be started: {exc}", file=sys.stderr)
        return EXIT_SPAWN_FAILED, "", timeout_sec
    except subprocess.TimeoutExpired as exc:
        print(
            f"::warning::the claude CLI did not finish within {timeout_sec}s"
            " and was killed",
            file=sys.stderr,
        )
        # What it had printed before the kill, not "". The exception carries
        # it, and this return value is what the run log is written from --
        # so discarding it left the operator an empty log at the one moment
        # the transcript is the only evidence of what the review was doing.
        partial = captured_text(exc.stderr)
        if partial:
            print(partial, file=sys.stderr)
        return EXIT_TIMED_OUT, captured_text(exc.stdout), timeout_sec
    if result.stderr:
        print(result.stderr, file=sys.stderr)
    return result.returncode, result.stdout, timeout_sec


def write_exec_file(stdout: str) -> bool:
    """Reshape the CLI's JSON output into an execution log; False if unusable."""
    try:
        payload = json.loads(stdout)
    except ValueError:
        return False
    text = payload.get("result") if isinstance(payload, dict) else None
    if not isinstance(text, str):
        return False
    Path(EXEC_FILE).write_text(
        json.dumps([{"content": [{"text": text}]}]), encoding="utf-8"
    )
    return True


def accept_direct_write() -> bool | None:
    """True when the CLI's own verdict file is usable, False when it is not.

    None means there was nothing to judge. The three answers are kept apart
    because the caller's message differs for each, and collapsing the last
    two is how "the CLI produced nothing" came to be printed on a path where
    it had produced a file that did not parse.
    """
    path = Path(REVIEW_FILE)
    if not (path.is_file() and path.stat().st_size):
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except ValueError:
        return False
    # Valid JSON is not necessarily a verdict: a top-level array parses and
    # then has no .get for stamp_model_status.
    if not isinstance(payload, dict):
        return False
    # A direct write skips extract_claude_review, which is where the AT-1799
    # status contract is otherwise stamped. A model-emitted "failed" is not
    # trusted from either reviewer.
    stamp_model_status(payload)
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(f"{REVIEW_FILE} written directly by the CLI")
    return True


def review() -> None:
    config = LocalConfig.load()
    prompt = build_claude_prompt(
        os.environ.get("THREAD_COUNT", "0"),
        os.environ.get("EXISTING_COMMENTS", "").strip(),
    )
    exit_code, stdout, timeout_sec = run_cli(prompt, config.get("CLAUDE_MODEL"))
    Path(RUN_LOG).write_text(stdout, encoding="utf-8")

    written = accept_direct_write()
    if written is True:
        return
    if written is False:
        print(
            f"::warning::{REVIEW_FILE} is not a usable verdict -- falling back"
            " to the execution log",
            file=sys.stderr,
        )
    if write_exec_file(stdout) and extract_review(EXEC_FILE):
        return

    reason = _exit_reason(exit_code, timeout_sec)
    if written is False:
        kind = ERROR_UNPARSEABLE
        summary = "Claude review failed: the verdict file the CLI wrote is not usable"
        detail = f"claude {reason}; its verdict file is not a JSON object"
    elif exit_code in (EXIT_NOT_INSTALLED, EXIT_TIMED_OUT, EXIT_SPAWN_FAILED):
        # Ahead of the output test, because the CLI never ran to completion:
        # whatever it printed first is a transcript, not a verdict it failed
        # to format. Keeping the partial transcript instead of "" moved this
        # case onto `elif stdout:`, which reported a killed CLI as
        # output_unparseable -- and review_codex_local classifies the same
        # run as cli_invocation_failed however much log it kept, so the two
        # shims were spelling one failure two ways.
        kind = ERROR_CLI_FAILED
        summary = f"Claude review failed: {reason}"
        detail = f"claude {reason}" + (
            "; output held no verdict JSON" if stdout else "; no output produced"
        )
    elif stdout:
        kind = ERROR_UNPARSEABLE
        summary = "Claude review failed: no verdict file produced"
        detail = f"claude {reason}; output held no verdict JSON"
    else:
        kind = ERROR_CLI_FAILED
        summary = "Claude review failed: no verdict file produced"
        detail = f"claude {reason}; no output produced"
    print(f"::warning::{summary} -- emitting error verdict ({kind})", file=sys.stderr)
    write_verdict(REVIEW_FILE, error_verdict(summary, kind, detail))


if __name__ == "__main__":
    # Before anything else this process does: whoever started it has
    # already decided what environment the CLI will get.
    warn_unless_driver_spawned("claude")
    guarded_main(review, "Claude", REVIEW_FILE)
