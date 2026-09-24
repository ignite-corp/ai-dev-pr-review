#!/usr/bin/env python3
"""Codex code review via the local `codex` CLI.

The prompt and the verdict handling are the Actions path's, step for step:
context.md, then the unresolved-thread block (reviewer_prompts.py, the same
text the Claude reviewer gets), then the trusted review_prompt.md; a direct
write is trusted only when it parses; otherwise the verdict is recovered
from the run log by extract_codex_json.py; otherwise an error verdict, so
the aggregate sees a FAILED reviewer rather than an absent one.

Dropped as runner plumbing rather than review behaviour: the sysctl writes
that enable bubblewrap on a runner, the pinned `npm install -g @openai/codex`,
and `codex login --with-api-key`. The CLI's own `--sandbox workspace-write`
is kept -- that is the review invocation. Authentication is whatever
`codex login` put in the operator's ~/.codex, or an OPENAI_API_KEY the
operator named in $LENS_REVIEWER_ENV_PASSTHROUGH; this module sets neither
and inherits whatever the driver's allowlist let through
(review_pr_local.reviewer_env). Started by anything but the driver there is
no allowlist, so this module warns that the CLI is getting the caller's
whole environment instead.

THE PROMPT GOES OVER STDIN, and that is a measurement rather than a
preference. The Actions path passes it as one argv element, where Linux caps
a single element at MAX_ARG_STRLEN (131072 bytes) independently of the much
larger ARG_MAX; the discarded version inherited that shape, could not check
whether the CLI would take stdin because codex was not installed on the
machine, and so added a size check and an exit code for a limit it could
only report. Measured on codex-cli 0.154.0:

    $ printf 'Reply with exactly: PROBE_STDIN_OK' \\
        | codex exec --sandbox read-only -
    user
    Reply with exactly: PROBE_STDIN_OK
    codex
    PROBE_STDIN_OK

  `codex exec --help`: "[PROMPT] ... If not provided as an argument (or if
  `-` is used), instructions are read from stdin."

So the ceiling is not worked around, it is not reached: the size check, its
exit code, and the prompt's exposure in the process table all go away rather
than being carried across. base-ai-review-single.yml still has the argv
shape and the ceiling with it; that is AT-2411 and deliberately not touched
here.

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
from pathlib import Path

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
from reviewer_prompts import existing_threads_block

SCRIPT_DIR = Path(__file__).resolve().parent
REVIEW_FILE = "review-codex.json"
RUN_LOG = "codex-run.log"
COMBINED_PROMPT = "codex-prompt.md"
TRUSTED_PROMPT = SCRIPT_DIR / "review_prompt.md"
# The base prompt may still instruct the model to write the older names.
LEGACY_VERDICT_FILES = ("verdict-openai.json", "verdict-codex.json")
# The budget the Actions path gives this reviewer: `timeout-minutes: 10` on
# the `Run Codex review` step of base-ai-review-single.yml.
#
# Pinned, where the Claude shim derives its bound from the cap it hands the
# CLI. The asymmetry is real and is stated rather than smoothed over: that
# derivation exists because the composite gives Claude's CLI a request cap
# to be bounded against, and `codex exec` has no timeout option to give
# (checked on 0.154.0). There is nothing here to stay in step with, so this
# is the only place the bound exists locally.
_CLI_TIMEOUT_SEC = 600
# Last meaningful run-log line, for the fallback verdict's detail field.
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")
_LOG_TAIL_LINES = 20
_LOG_TAIL_CHARS = 200


def shim_budget_sec() -> int:
    """The longest this shim can take before it stops writing a verdict.

    TWICE _CLI_TIMEOUT_SEC, because the bound is spent twice in the worst
    case: once on `codex exec`, and again on extract_codex_json.py when the
    model answered in text instead of writing the file. Asked by the driver,
    which bounds this shim from outside and must stay above it -- past its
    own bound this shim still writes an error verdict and keeps the run log,
    whereas the driver's bound is a SIGTERM that leaves neither.
    """
    return 2 * _CLI_TIMEOUT_SEC


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


def run_cli(prompt: str, model: str) -> tuple[int, str]:
    """Run the CLI over stdin, returning its exit code and combined log."""
    argv = [
        "codex",
        "exec",
        "--sandbox",
        "workspace-write",
        "--model",
        model,
        # Reads the prompt from stdin. Measured; see the module docstring.
        "-",
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
        print(
            "::warning::the codex CLI is not installed or not on PATH",
            file=sys.stderr,
        )
        return EXIT_NOT_INSTALLED, ""
    except OSError as exc:
        print(f"::warning::the codex CLI could not be started: {exc}", file=sys.stderr)
        return EXIT_SPAWN_FAILED, ""
    except subprocess.TimeoutExpired as exc:
        print(
            f"::warning::the codex CLI did not finish within"
            f" {_CLI_TIMEOUT_SEC}s and was killed",
            file=sys.stderr,
        )
        # Composed the same way as the clean path below, because this IS
        # the run log: returning "" left log_tail nothing to report and the
        # extractor nothing to read, at the one moment the transcript is
        # the only evidence of what the review was doing.
        return EXIT_TIMED_OUT, captured_text(exc.stdout) + captured_text(exc.stderr)
    return result.returncode, result.stdout + result.stderr


def verdict_file_written() -> bool:
    """Has anything already put a verdict in REVIEW_FILE?

    ONE question, ONE place. The workflow's test is `[ -s ]`, non-empty, and
    the discarded version answered it twice in two ways -- the promotion
    below tested existence alone while the classification tested size -- so
    a zero-byte file, which is exactly the case the classification exists
    for, blocked the promotion of a perfectly good legacy verdict.
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
        except ValueError:
            continue
        if not isinstance(payload, dict):
            continue
        payload.setdefault("early_exit", False)
        Path(REVIEW_FILE).write_text(
            json.dumps(payload, indent=2), encoding="utf-8"
        )
        print(f"Normalized {candidate} -> {REVIEW_FILE}")
        return


def accept_direct_write() -> bool | None:
    """True when the CLI's verdict file is usable, False when it is not.

    None means there was nothing to judge, and the question of whether
    anything is there is asked through verdict_file_written() -- the same
    call normalize_verdict_file makes, so the two cannot answer differently.
    """
    path = Path(REVIEW_FILE)
    if not verdict_file_written():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except ValueError:
        return False
    if not isinstance(payload, dict):
        return False
    stamp_model_status(payload)
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(f"{REVIEW_FILE} written directly by the CLI")
    return True


def extract_from_log() -> bool:
    """Recover a verdict the model printed instead of writing.

    Low-reasoning models answer in text rather than using the sandbox tools,
    so the verdict is in the run log; extract_codex_json.py is the same
    script the workflow calls for it.
    """
    argv = [
        sys.executable,
        str(SCRIPT_DIR / "extract_codex_json.py"),
        RUN_LOG,
        REVIEW_FILE,
    ]
    # Classified here, like every other spawn in this shim. Left to raise, it
    # reached guarded_main, which writes ERROR_CRASHED -- "the reviewer raised
    # before writing a verdict" -- naming a fault that did not happen: the
    # reviewer had already run, and it was the recovery step that failed.
    # Returning False instead falls through to the ERROR_UNPARSEABLE verdict
    # below, whose detail is the log tail the extractor could not read.
    try:
        extract = subprocess.run(argv, timeout=_CLI_TIMEOUT_SEC)
    except subprocess.TimeoutExpired:
        print(
            f"::warning::extract_codex_json.py did not finish within"
            f" {_CLI_TIMEOUT_SEC}s and was killed",
            file=sys.stderr,
        )
        return False
    except OSError as exc:
        print(
            f"::warning::extract_codex_json.py could not be started: {exc}",
            file=sys.stderr,
        )
        return False
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
    # the workflow puts it: there, a legacy-named verdict is promoted only
    # if nothing has written review-codex.json first, so an error verdict
    # from the fallback path masks the real one the model wrote (AT-2424).
    normalize_verdict_file()

    written = accept_direct_write()
    if written is True:
        return
    if written is False:
        print(
            "::warning::Codex wrote malformed verdict JSON -- falling back to"
            " the run log",
            file=sys.stderr,
        )
    # Tried after a malformed direct write as well as after no file at all,
    # and after a non-zero exit as well as after a clean one. Parity does
    # not decide this: the workflow gates on `[ -s ]` and never validates
    # the JSON, so it has no behaviour here to copy. What decides it is that
    # a complete verdict in the run log is the only usable answer the run
    # produced, and discarding it because a different artefact was truncated
    # is the strictly worse half of the choice. An exit status is a
    # different artefact too -- it says nothing about what the log holds --
    # so it is not a second condition here. The extractor's own exit code
    # is the evidence: it requires summary, early_exit and issues in one
    # parsed object, and fails when the log has no such thing. An empty log
    # is not a third condition sneaking back in -- there is nothing to read,
    # and a CLI that printed nothing is one that never ran.
    if log and extract_from_log():
        return

    # The log tail when there is a log, and the exit reason when there is
    # not: a CLI that never started leaves nothing to tail, and an empty
    # error_detail made the two shims disagree about the one field the
    # aggregate reads for a reason.
    #
    # `reason` is computed HERE, above both the summary and the detail, and
    # not inside the branch that used to own it. spawn_exit_reason answers
    # "" for a code it does not own -- deliberately, so a shim adding one
    # has to say what it means -- so `log_tail(log) or spawn_exit_reason(..)`
    # was still "" on a real path: the CLI ran, printed nothing, and exited
    # with an ordinary non-zero status. That wrote the empty error_detail
    # this comment forbids, while the Claude shim on the same run wrote
    # "claude CLI exited 3; no output produced" -- the two shims disagreeing
    # about the one field the aggregate reads for a reason, which is the
    # defect, not the emptiness on its own.
    #
    # The fallback is the Claude shim's sentence and not a bare `reason`:
    # error_verdict appends the detail to the summary, and the summary is
    # already built from `reason`, so handing both the same string produced
    # "Codex review failed: CLI exited 3 -- CLI exited 3". The reached-only-
    # when-log_tail-is-empty condition is exactly what "no output produced"
    # states, so the wording is a fact about this path and not a guess.
    reason = spawn_exit_reason(exit_code, _CLI_TIMEOUT_SEC) or f"CLI exited {exit_code}"
    detail = log_tail(log) or f"codex {reason}; no output produced"
    if written is False or exit_code == 0:
        print(
            "::warning::Codex emitted output but no parseable verdict JSON found",
            file=sys.stderr,
        )
        summary = "Codex review failed: no parseable verdict JSON in output"
        kind = ERROR_UNPARSEABLE
    else:
        print(
            f"::warning::Codex review failed ({reason}) -- emitting error verdict",
            file=sys.stderr,
        )
        summary = f"Codex review failed: {reason}"
        kind = ERROR_CLI_FAILED
    write_verdict(REVIEW_FILE, error_verdict(summary, kind, detail))


if __name__ == "__main__":
    # Before anything else this process does: whoever started it has
    # already decided what environment the CLI will get.
    warn_unless_driver_spawned("codex")
    guarded_main(review, "Codex", REVIEW_FILE)
