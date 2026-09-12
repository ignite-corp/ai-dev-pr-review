#!/usr/bin/env python3
"""Codex code review via the local `codex` CLI.

The prompt and the verdict handling are the Actions path's, step for step:
context.md, then the unresolved-thread block (reviewer_prompts.py, the same
text the Claude reviewer gets), then the trusted review_prompt.md; a direct
write is trusted only when it parses; otherwise the verdict is extracted from
the run log by extract_codex_json.py; otherwise an error verdict is emitted so
the aggregate sees a FAILED reviewer rather than an absent one.

Dropped, because they are runner plumbing rather than review behaviour: the
sysctl writes that enable bubblewrap on an Actions runner and restore it
afterwards, the pinned `npm install -g @openai/codex`, and `codex login
--with-api-key`. The CLI's own `--sandbox workspace-write` is kept -- that is
the review invocation, not the runner's plumbing -- and authentication is
whatever `codex login` already put in the operator's ~/.codex.

Reads context.md and pr.diff from the current directory and writes
review-codex.json.

Env: CODEX_MODEL, THREAD_COUNT, EXISTING_COMMENTS.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import traceback
from enum import Enum
from pathlib import Path

from local_review_config import LocalConfig
from review_status import stamp_model_status
from reviewer_prompts import existing_threads_block

REVIEW_FILE = "review-codex.json"
RUN_LOG = "codex-run.log"
COMBINED_PROMPT = "codex-prompt.md"
TRUSTED_PROMPT = Path(__file__).resolve().parent / "review_prompt.md"
# The base prompt may still instruct the model to write the older names.
LEGACY_VERDICT_FILES = ("verdict-openai.json", "verdict-codex.json")
# The budget the Actions path gives this reviewer: `timeout-minutes: 10` on
# the `Run Codex review` step of base-ai-review-single.yml. Unlike the Claude
# composite there is nothing to pass the CLI here -- codex takes no timeout
# input -- so this is the only place the bound exists locally. Pinned rather
# than parsed, for the reason its sibling gives: a read at import time has no
# verdict file to fail into. test_the_local_timeout_matches_the_workflow_step
# keeps it equal to the step.
_CLI_TIMEOUT_SEC = 600
# Linux caps a SINGLE argv element at MAX_ARG_STRLEN, independently of the
# much larger ARG_MAX total. Measured on this machine: 131071 bytes goes
# through, 131072 raises E2BIG -- while ARG_MAX is 2 MiB. The prompt is one
# argument (context.md + threads + review_prompt.md), and a PR near
# PR_SIZE_LIMIT reaches roughly 244 KB of it, so this is a limit real runs
# meet rather than a theoretical one.
#
# It is detected rather than worked around: `codex exec` takes the prompt as
# an argument here and in base-ai-review-single.yml, and whether it reads one
# from stdin is not something this machine can check -- codex is not
# installed on it. Guessing a flag would be worse than reporting the cause.
# The Actions path has the same ceiling for the same reason.
_MAX_ARG_BYTES = 131072
# Distinct so the two stay distinct all the way to the verdict. Warning about
# them differently and then returning the same value merged them again one
# frame later, which is where review() decides what the operator is told.
_EXIT_NOT_INSTALLED = -1
_EXIT_TIMED_OUT = -2
_EXIT_SPAWN_FAILED = -3
_EXIT_PROMPT_TOO_LARGE = -4
# Last meaningful run-log line, for the fallback verdict's detail field.
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")
_LOG_TAIL_LINES = 20
_LOG_TAIL_CHARS = 200


def build_prompt(thread_count: str, existing_comments: str) -> str:
    context = Path("context.md")
    prompt = f"{context.read_text(encoding='utf-8')}\n" if context.is_file() else ""
    if existing_comments:
        prompt += existing_threads_block(thread_count, existing_comments)
    return prompt + TRUSTED_PROMPT.read_text(encoding="utf-8")


def log_tail(log: str) -> str:
    """The last non-blank log line, stripped of ANSI escapes and capped."""
    for line in reversed(log.splitlines()[-_LOG_TAIL_LINES:]):
        cleaned = _ANSI_RE.sub("", line).rstrip()
        if cleaned.strip():
            return cleaned[:_LOG_TAIL_CHARS]
    return ""


def _exit_reason(exit_code: int) -> str:
    """Say which failure it was, where the verdict can carry it."""
    if exit_code == _EXIT_NOT_INSTALLED:
        return "the CLI is not installed or not on PATH"
    if exit_code == _EXIT_TIMED_OUT:
        return f"the CLI did not finish within {_CLI_TIMEOUT_SEC}s"
    if exit_code == _EXIT_SPAWN_FAILED:
        return "the CLI could not be started"
    if exit_code == _EXIT_PROMPT_TOO_LARGE:
        return (
            f"the prompt exceeds the {_MAX_ARG_BYTES}-byte kernel limit for a"
            " single command-line argument"
        )
    return f"CLI exited {exit_code}"


def error_verdict(summary: str, kind: str, detail: str) -> dict[str, object]:
    return {
        "summary": summary + ("" if not detail else f" -- {detail}"),
        "status": "failed",
        "early_exit": False,
        "issues": [],
        "error": kind,
        "error_detail": detail,
    }


def run_cli(prompt: str, model: str) -> tuple[int, str]:
    """Run the CLI, returning its exit code and the combined run log.

    The environment is inherited rather than built: codex authenticates from
    the operator's own ~/.codex or OPENAI_API_KEY, and this module never logs
    anyone in. The driver adds the settings it owns before this process starts.
    """
    argv = [
        "codex",
        "exec",
        "--sandbox",
        "workspace-write",
        "--model",
        model,
        prompt,
    ]
    encoded = len(prompt.encode("utf-8"))
    if encoded >= _MAX_ARG_BYTES:
        print(
            f"::warning::the codex prompt is {encoded} bytes, at or over the"
            f" {_MAX_ARG_BYTES}-byte kernel limit for one argument; the CLI"
            " cannot be started with it",
            file=sys.stderr,
        )
        return _EXIT_PROMPT_TOO_LARGE, ""
    try:
        result = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=_CLI_TIMEOUT_SEC,
        )
    except OSError as exc:
        if not isinstance(exc, FileNotFoundError):
            print(
                f"::warning::the codex CLI could not be started: {exc}",
                file=sys.stderr,
            )
            return _EXIT_SPAWN_FAILED, ""
        # Distinguished from a timeout below: "not installed" and "ran for ten
        # minutes" are different problems with different fixes, and both used
        # to reach the operator as the same empty output.
        print(
            "::warning::the codex CLI is not installed or not on PATH",
            file=sys.stderr,
        )
        return _EXIT_NOT_INSTALLED, ""
    except subprocess.TimeoutExpired:
        print(
            f"::warning::the codex CLI did not finish within"
            f" {_CLI_TIMEOUT_SEC}s and was killed",
            file=sys.stderr,
        )
        return _EXIT_TIMED_OUT, ""
    return result.returncode, result.stdout + result.stderr


def verdict_file_written() -> bool:
    """Has anything already put a verdict in REVIEW_FILE?

    The one test, asked in one place. The workflow's is `[ -s ]`, non-empty,
    and this module had two answers to the question: the classification
    below tested size, this promotion tested existence alone, so a zero-byte
    file -- the case the classification exists for -- blocked the promotion
    of a perfectly good legacy verdict.
    """
    path = Path(REVIEW_FILE)
    return path.is_file() and bool(path.stat().st_size)


def normalize_verdict_file() -> None:
    """Promote a legacy verdict file to review-codex.json, as the workflow does."""
    if verdict_file_written():
        return
    for candidate in LEGACY_VERDICT_FILES:
        path = Path(candidate)
        if not path.is_file():
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            continue
        if not isinstance(payload, dict):
            continue
        payload.setdefault("early_exit", False)
        Path(REVIEW_FILE).write_text(json.dumps(payload), encoding="utf-8")
        print(f"Normalized {candidate} -> {REVIEW_FILE}")
        return


class DirectWrite(Enum):
    """What the CLI left in REVIEW_FILE -- decided once, here.

    A boolean forced the caller to recompute the absent-versus-malformed half
    of the answer, and it recomputed a weaker one: file-existence, where the
    workflow gates on `if [ -s review-codex.json ]`, i.e. non-empty. A
    zero-byte file (the CLI created it and died before writing, the crash
    the fallback exists for) was then ABSENT here and "malformed" there, so
    the run-log recovery was skipped. One value, one test -- and the test
    itself is verdict_file_written(), which normalize_verdict_file asks too.
    """

    ACCEPTED = "accepted"
    MALFORMED = "malformed"
    ABSENT = "absent"


def accept_direct_write() -> DirectWrite:
    """Classify the CLI's verdict file; stamps its status when it parses."""
    path = Path(REVIEW_FILE)
    if not verdict_file_written():
        return DirectWrite.ABSENT
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return DirectWrite.MALFORMED
    # Valid JSON is not necessarily a verdict: a top-level array parses fine
    # and then has no .get for stamp_model_status, so the module crashed
    # where its own tests say it emits a "failed" verdict.
    if not isinstance(payload, dict):
        return DirectWrite.MALFORMED
    stamp_model_status(payload)
    path.write_text(json.dumps(payload), encoding="utf-8")
    print(f"{REVIEW_FILE} written directly by the CLI")
    return DirectWrite.ACCEPTED


def extract_from_log() -> bool:
    """Recover a verdict the model printed instead of writing.

    Low-reasoning models answer in text rather than using the sandbox tools,
    so the verdict is in the run log; extract_codex_json.py is the same
    script the workflow calls for it.
    """
    extract = subprocess.run(
        [
            sys.executable,
            str(Path(__file__).resolve().parent / "extract_codex_json.py"),
            RUN_LOG,
            REVIEW_FILE,
        ],
    )
    if extract.returncode != 0:
        return False
    print(f"Extracted Codex verdict JSON from {RUN_LOG}")
    return True


def review() -> None:
    config = LocalConfig.load()
    prompt = build_prompt(
        os.environ.get("THREAD_COUNT", "0"),
        os.environ.get("EXISTING_COMMENTS", "").strip(),
    )
    Path(COMBINED_PROMPT).write_text(prompt, encoding="utf-8")

    exit_code, log = run_cli(prompt, config.get("CODEX_MODEL"))
    Path(RUN_LOG).write_text(log, encoding="utf-8")
    # Ahead of the direct-write check rather than after the fallbacks, where
    # the workflow puts it: there, a legacy-named verdict is only promoted if
    # nothing has written review-codex.json first, so an error verdict from
    # the fallback path masks the real one the model wrote.
    normalize_verdict_file()

    written = accept_direct_write()
    if written is DirectWrite.ACCEPTED:
        return

    detail = log_tail(log)
    if written is DirectWrite.MALFORMED:
        print(
            "::warning::Codex wrote malformed verdict JSON -- falling back to"
            " the run log",
            file=sys.stderr,
        )
    # Tried after a malformed direct write as well as after no file at all.
    # Parity does not decide this one: the workflow gates on `[ -s ]` and
    # never validates the JSON, so it has no behaviour here to copy. What
    # decides it is that a complete verdict in the run log is the only
    # usable answer the run produced, and discarding it because a different
    # artefact was truncated is the strictly worse half of the choice. The
    # Claude shim already falls through to its own log recovery this way.
    if exit_code == 0 and extract_from_log():
        return
    if written is DirectWrite.MALFORMED or exit_code == 0:
        print(
            "::warning::Codex emitted output but no parseable verdict JSON found",
            file=sys.stderr,
        )
        summary, kind = (
            "Codex review failed: no parseable verdict JSON in output",
            "output_unparseable",
        )
    else:
        print(
            f"::warning::Codex review failed (exit={exit_code}) -- emitting error"
            " verdict",
            file=sys.stderr,
        )
        summary, kind = (
            f"Codex review failed: {_exit_reason(exit_code)}",
            "cli_invocation_failed",
        )
    Path(REVIEW_FILE).write_text(
        json.dumps(error_verdict(summary, kind, detail), indent=2), encoding="utf-8"
    )


def main() -> None:
    """Run the review; leave a verdict file even if it raises.

    The same guarantee review_claude_local.main() carries, and for the same
    reason: this module's own docstring promises the aggregate a FAILED
    reviewer rather than an absent one, and a raise on any path that the
    handlers below do not enumerate -- an unreadable context.md, a config
    file the operator cannot read -- breaks that promise silently. The
    traceback still reaches the run log the driver captures.
    """
    try:
        review()
    except Exception as exc:  # noqa: BLE001 -- the contract is the catch
        traceback.print_exc()
        print(
            "::warning::the Codex reviewer raised before writing a verdict"
            " -- emitting error verdict (reviewer_crashed)",
            file=sys.stderr,
        )
        Path(REVIEW_FILE).write_text(
            json.dumps(
                error_verdict(
                    "Codex review failed: the reviewer raised before writing a verdict",
                    "reviewer_crashed",
                    f"the local Codex reviewer raised {exc!r};"
                    " see the run log for the traceback",
                ),
                indent=2,
            ),
            encoding="utf-8",
        )


if __name__ == "__main__":
    main()
