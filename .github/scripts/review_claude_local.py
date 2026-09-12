#!/usr/bin/env python3
"""Claude code review via the local `claude` CLI.

The Actions path runs anthropics/claude-code-action through the
`.github/actions/claude-review` composite; there is no runner here, so this
module drives the CLI directly. Everything the composite decides that is not
about being an Action is kept: the same prompt (reviewer_prompts.py), the same
`--model` and `--allowedTools` values, the same verdict-file fallback, and
the same error-verdict shapes so the aggregate renders a local outage the way
it renders an Actions one.

What does NOT carry over: the OAuth-vs-API-key precedence and the
usage-limit auth switch, both of which exist to choose between two secrets
held by an org. The CLI uses whatever credential the operator is already
logged in with.

Reads pr.diff and context.md from the current directory (the CLI is told to,
in the prompt) and writes review-claude.json.

Env: CLAUDE_MODEL, THREAD_COUNT, EXISTING_COMMENTS.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import traceback
from pathlib import Path

import yaml

from extract_claude_review import extract_review
from local_review_config import LocalConfig
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
# The Actions job caps the reviewer step at 10 minutes (timeout-minutes: 10
# on `Run Claude review`) and the composite raises the CLI's own request and
# stream-idle timeouts to match, at 600000 ms; cli_env() below carries those
# two to the local CLI so the pair holds here as well. Pinned rather than
# derived from the composite: a read at import time has no verdict file to
# fail into. test_the_local_timeout_matches_the_composites keeps them equal.
_CLI_TIMEOUT_SEC = 600
# The composite step whose env reaches the CLI process on a runner.
_ACTION_STEP_ID = "claude-review"
# Matched with fullmatch, and the anchors are gone with the `match` that
# needed them: `$` also matches just before a trailing newline, so
# "${{ inputs.api_timeout_ms }}\n" -- what a YAML folded scalar produces --
# was read as the bare reference and silently normalised. (The example of
# a trailing "0" was never accepted: `$` does not match mid-string.)
_INPUT_REF_RE = re.compile(r"\$\{\{\s*inputs\.([A-Za-z0-9_-]+)\s*\}\}")
# What an unresolved Actions expression still looks like after cli_env has
# done what it can. Only the runner evaluates these; anything still holding
# one is a value this module did not read, not a value it can pass on.
_EXPRESSION_MARKER = "${{"
# Distinct so the two stay distinct all the way to the verdict. Warning about
# them differently and then returning the same value merged them again one
# frame later, which is where review() decides what the operator is told.
_EXIT_NOT_INSTALLED = -1
_EXIT_TIMED_OUT = -2
_EXIT_SPAWN_FAILED = -3
# The composite's YAML, not the CLI. Reading it used to fail as one of the
# other three: an unparseable action.yml said "the CLI could not be started"
# and a missing one said "not installed", pointing the operator at a CLI that
# was never the fault.
_EXIT_CONFIG_UNREADABLE = -4


class CompositeUnreadable(ValueError):
    """The composite's YAML does not carry something this module has to read.

    Distinct from a parse failure: the file is valid YAML, and the value is
    simply not one this module knows how to use -- a step that is no longer
    there, or a step env value that is not a single input reference. Passing
    such a value on unchanged is the outcome _EXIT_CONFIG_UNREADABLE exists
    to replace, so this joins _CONFIG_READ_ERRORS and is reported as that.
    """


# The failures these reads produce, each named rather than summarised: an
# absent or unreadable file (OSError), invalid YAML (YAMLError), a renamed
# or dropped key (KeyError), a document that parses to None or a list so the
# subscript has nothing to index (TypeError), a scalar where a mapping was
# expected so `.get`/`.items` is not there (AttributeError -- `runs.steps`
# as a list of strings, or a step whose `env` is a sequence), and a value
# this module cannot resolve. Not "everything that can raise": that claim
# was here, and AttributeError was missing from under it.
_CONFIG_READ_ERRORS = (
    OSError,
    yaml.YAMLError,
    KeyError,
    TypeError,
    AttributeError,
    CompositeUnreadable,
)


def _action() -> dict:
    return yaml.safe_load(ACTION_YML.read_text(encoding="utf-8"))


def allowed_tools() -> str:
    """The composite's `--allowedTools` value, read from its own default."""
    return _action()["inputs"]["allowed_tools"]["default"]


def cli_env() -> dict[str, str]:
    """The env the composite puts on the step that runs the CLI.

    It sets API_TIMEOUT_MS and CLAUDE_STREAM_IDLE_TIMEOUT_MS there because
    the CLI's byte-level stream watchdog defaults to 180000 ms on direct
    Anthropic API connections and aborted slow reviews well inside the
    10-minute budget (AT-1601). Nothing sets them on a developer's machine,
    so a local run kept the smaller ceiling the composite exists to lift
    while the comment on _CLI_TIMEOUT_SEC claimed the opposite.

    Read, not restated: the step's two values are both
    `${{ inputs.api_timeout_ms }}`, so this resolves that one indirection to
    the input's own default and follows a change to either. Whatever keys
    the step grows, the CLI gets -- test_cli_env_matches_the_composites_step
    compares the key sets.

    Only the runner can evaluate an Actions expression, and the one form
    above is all this resolves. A value in any other form -- a reference
    interpolated into a longer string, or one naming env/vars/steps -- is
    reported as unreadable rather than handed to the CLI as literal
    `${{ ... }}` text, which is the silent pass-through this module's own
    _EXIT_CONFIG_UNREADABLE was added to stop.
    """
    action = _action()
    for step in action["runs"]["steps"]:
        if step.get("id") != _ACTION_STEP_ID:
            continue
        resolved: dict[str, str] = {}
        for key, value in (step.get("env") or {}).items():
            ref = _INPUT_REF_RE.fullmatch(str(value))
            resolved_value = action["inputs"][ref.group(1)]["default"] if ref else value
            # `default:` with nothing after it parses to None, and str(None)
            # is "None" -- a value with no `${{` in it, which would have been
            # handed to the CLI as API_TIMEOUT_MS=None. A missing `default`
            # key raises KeyError above; a present and empty one was silently
            # usable, which is the pass-through this class exists to stop.
            if not isinstance(resolved_value, (str, int, float)):
                raise CompositeUnreadable(
                    f"{ACTION_YML}: step {_ACTION_STEP_ID!r} resolves {key} to"
                    f" {resolved_value!r}, which is not a value"
                )
            settled = str(resolved_value)
            if _EXPRESSION_MARKER in settled:
                raise CompositeUnreadable(
                    f"{ACTION_YML}: step {_ACTION_STEP_ID!r} sets {key} to"
                    f" {value!r}, which this module cannot resolve -- it reads"
                    " a value that is exactly one inputs.<name> reference,"
                    " and only the runner evaluates anything else"
                )
            resolved[key] = settled
        return resolved
    raise CompositeUnreadable(f"{ACTION_YML} has no step with id {_ACTION_STEP_ID!r}")


def _exit_reason(exit_code: int) -> str:
    """Say which failure it was, where the verdict can carry it."""
    if exit_code == _EXIT_NOT_INSTALLED:
        return "the CLI is not installed or not on PATH"
    if exit_code == _EXIT_TIMED_OUT:
        return f"the CLI did not finish within {_CLI_TIMEOUT_SEC}s"
    if exit_code == _EXIT_SPAWN_FAILED:
        return "the CLI could not be started"
    if exit_code == _EXIT_CONFIG_UNREADABLE:
        return "the composite action's CLI settings could not be read"
    return f"CLI exited {exit_code}"


def error_verdict(summary: str, kind: str, detail: str) -> dict[str, object]:
    """The failed verdict, with the summary the caller's path actually means.

    Fixed text here said "no verdict file produced" on the path where the
    CLI did produce one and it did not parse, and again for a crash before
    the CLI ran -- merging, in the operator-facing line, three failures the
    exit codes above are split apart to keep distinct. Parameterised like
    the Codex shim's, which had it this way already.
    """
    return {
        "summary": summary + ("" if not detail else f" -- {detail}"),
        "status": "failed",
        "early_exit": False,
        "issues": [],
        "error": kind,
        "error_detail": detail,
    }


def run_cli(prompt: str, model: str) -> tuple[int, str]:
    """Run the CLI, returning its exit code and raw stdout.

    Credentials are inherited rather than built: the CLI authenticates from
    whatever the operator is already logged in with, and listing those here
    would be this module deciding which of them count. The driver adds the
    settings it owns (the model, the thread context) to that environment
    before this process starts. On top of it go the composite's own two
    timeout keys, which no runner is here to set (cli_env).
    """
    # Its own guard, and its own exit code: reading the composite's YAML is
    # not starting the CLI, and sharing an exception clause with the spawn
    # merged two different faults into one message for the operator. The
    # clause here is wide because the fault can be in the file, the parser or
    # either subscript (_CONFIG_READ_ERRORS); the spawn's clauses stay narrow.
    try:
        tools = allowed_tools()
        # The composite's values go UNDER the inherited environment, not over
        # it. They stand in for a runner that is not here to set them, which
        # is position 3 of the order docs/local-review.md states -- process
        # environment, then config file, then the value parsed out of the
        # workflow files. Merged the other way, an operator who exported
        # API_TIMEOUT_MS had it silently replaced by the default this
        # function exists to supply in its absence.
        env = {**cli_env(), **os.environ}
    except _CONFIG_READ_ERRORS as exc:
        print(
            f"::warning::cannot read the composite action's CLI settings"
            f" (allowed_tools, step env) from {ACTION_YML}: {exc!r}",
            file=sys.stderr,
        )
        return _EXIT_CONFIG_UNREADABLE, ""
    argv = [
        "claude",
        "--print",
        "--model",
        model,
        "--allowedTools",
        tools,
        "--output-format",
        "json",
    ]
    try:
        result = subprocess.run(
            argv,
            input=prompt,
            capture_output=True,
            text=True,
            timeout=_CLI_TIMEOUT_SEC,
            env=env,
        )
    except OSError as exc:
        if not isinstance(exc, FileNotFoundError):
            print(
                f"::warning::the claude CLI could not be started: {exc}",
                file=sys.stderr,
            )
            return _EXIT_SPAWN_FAILED, ""
        # Distinguished from a timeout below: "not installed" and "ran for ten
        # minutes" are different problems with different fixes, and both used
        # to reach the operator as the same empty output.
        print(
            "::warning::the claude CLI is not installed or not on PATH",
            file=sys.stderr,
        )
        return _EXIT_NOT_INSTALLED, ""
    except subprocess.TimeoutExpired:
        print(
            f"::warning::the claude CLI did not finish within"
            f" {_CLI_TIMEOUT_SEC}s and was killed",
            file=sys.stderr,
        )
        return _EXIT_TIMED_OUT, ""
    if result.stderr:
        print(result.stderr, file=sys.stderr)
    return result.returncode, result.stdout


def write_exec_file(stdout: str) -> bool:
    """Reshape the CLI's JSON output into an execution log; False if unusable."""
    try:
        payload = json.loads(stdout)
    except json.JSONDecodeError:
        return False
    text = payload.get("result") if isinstance(payload, dict) else None
    if not isinstance(text, str):
        return False
    Path(EXEC_FILE).write_text(
        json.dumps([{"content": [{"text": text}]}]), encoding="utf-8"
    )
    return True


def review() -> None:
    config = LocalConfig.load()
    prompt = build_claude_prompt(
        os.environ.get("THREAD_COUNT", "0"),
        os.environ.get("EXISTING_COMMENTS", "").strip(),
    )
    exit_code, stdout = run_cli(prompt, config.get("CLAUDE_MODEL"))
    Path(RUN_LOG).write_text(stdout, encoding="utf-8")

    # The CLI was told to write the verdict itself; trust it only when it
    # parses, exactly as the codex path does with its direct write.
    review_path = Path(REVIEW_FILE)
    malformed_write = False
    if review_path.is_file() and review_path.stat().st_size:
        try:
            payload = json.loads(review_path.read_text(encoding="utf-8"))
            # Valid JSON is not necessarily a verdict: a top-level array
            # parses fine and then has no .get for stamp_model_status, so
            # this crashed where the tests say it emits a "failed" verdict.
            if not isinstance(payload, dict):
                raise ValueError("verdict JSON is not an object")
            # A direct write skips extract_claude_review, which is where the
            # AT-1799 status contract is otherwise stamped -- so stamp it here,
            # exactly as the Codex shim does for its own direct write. A
            # model-emitted "failed" is not trusted from either of them.
            stamp_model_status(payload)
            review_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
            print(f"{REVIEW_FILE} written directly by the CLI")
            return
        except (json.JSONDecodeError, ValueError):
            malformed_write = True
            print(
                f"::warning::{REVIEW_FILE} is not parseable JSON -- falling back"
                " to the execution log",
                file=sys.stderr,
            )

    if write_exec_file(stdout) and extract_review(EXEC_FILE):
        return

    # The workflow splits these on whether an execution log exists at all:
    # something to parse that held no verdict, versus nothing to parse.
    if malformed_write:
        kind = "output_unparseable"
        summary = "Claude review failed: the verdict file the CLI wrote is not usable"
        detail = f"claude {_exit_reason(exit_code)}; its verdict file is not JSON"
    elif stdout:
        kind = "output_unparseable"
        summary = "Claude review failed: no verdict file produced"
        detail = f"claude {_exit_reason(exit_code)}; output held no verdict JSON"
    else:
        kind = "cli_invocation_failed"
        summary = "Claude review failed: no verdict file produced"
        detail = f"claude {_exit_reason(exit_code)}; no output produced"
    print(f"::warning::{summary} -- emitting error verdict ({kind})", file=sys.stderr)
    review_path.write_text(
        json.dumps(error_verdict(summary, kind, detail), indent=2), encoding="utf-8"
    )


def main() -> None:
    """Run the review; leave a verdict file even if it raises.

    Each fix for this module so far has closed the one raise it was shown --
    a PermissionError on spawn, then an unparseable action.yml, then a
    renamed composite input -- and the next raise reopened the same hole: the
    module exits by traceback, writes nothing, and the aggregate counts an
    ABSENT reviewer instead of a FAILED one (AT-1837). Enumerating the raises
    is what keeps failing, so the property is enforced here instead: an
    Exception escaping `review()` becomes a verdict, and the traceback still
    reaches the run log the driver captures. The specific handlers above stay, because
    they are what makes the verdict name the actual fault rather than this
    one's catch-all text.
    """
    try:
        review()
    except Exception as exc:  # noqa: BLE001 -- the contract is the catch
        traceback.print_exc()
        print(
            "::warning::the Claude reviewer raised before writing a verdict"
            " -- emitting error verdict (reviewer_crashed)",
            file=sys.stderr,
        )
        Path(REVIEW_FILE).write_text(
            json.dumps(
                error_verdict(
                    "Claude review failed: the reviewer raised before writing"
                    " a verdict",
                    "reviewer_crashed",
                    f"the local Claude reviewer raised {exc!r};"
                    " see the run log for the traceback",
                ),
                indent=2,
            ),
            encoding="utf-8",
        )


if __name__ == "__main__":
    main()
