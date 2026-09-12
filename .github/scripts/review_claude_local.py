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
import subprocess
import sys
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
# Generous: the Actions job caps the reviewer step at 10 minutes and the
# composite raises the CLI's own request timeout to match.
_CLI_TIMEOUT_SEC = 600


def allowed_tools() -> str:
    """The composite's `--allowedTools` value, read from its own default."""
    action = yaml.safe_load(ACTION_YML.read_text(encoding="utf-8"))
    return action["inputs"]["allowed_tools"]["default"]


def error_verdict(kind: str, detail: str) -> dict[str, object]:
    return {
        "summary": f"Claude review failed: no verdict file produced -- {detail}",
        "status": "failed",
        "early_exit": False,
        "issues": [],
        "error": kind,
        "error_detail": detail,
    }


def run_cli(prompt: str, model: str) -> tuple[int, str]:
    """Run the CLI, returning its exit code and raw stdout."""
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
            timeout=_CLI_TIMEOUT_SEC,
        )
    except FileNotFoundError:
        return -1, ""
    except subprocess.TimeoutExpired:
        return -1, ""
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


def main() -> None:
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
            # A direct write skips extract_claude_review, which is where the
            # AT-1799 status contract is otherwise stamped -- so stamp it here,
            # exactly as the Codex shim does for its own direct write. A
            # model-emitted "failed" is not trusted from either of them.
            stamp_model_status(payload)
            review_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
            print(f"{REVIEW_FILE} written directly by the CLI")
            return
        except json.JSONDecodeError:
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
    if stdout or malformed_write:
        kind = "output_unparseable"
        detail = f"claude CLI exited {exit_code}; output held no verdict JSON"
    else:
        kind = "cli_invocation_failed"
        detail = f"claude CLI exited {exit_code}; no output produced"
    print(
        f"::warning::Claude review produced no verdict file -- emitting error"
        f" verdict ({kind})",
        file=sys.stderr,
    )
    review_path.write_text(
        json.dumps(error_verdict(kind, detail), indent=2), encoding="utf-8"
    )


if __name__ == "__main__":
    main()
