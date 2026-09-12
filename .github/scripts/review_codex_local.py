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
_CLI_TIMEOUT_SEC = 600
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
    """Run the CLI, returning its exit code and the combined run log."""
    argv = [
        "codex",
        "exec",
        "--sandbox",
        "workspace-write",
        "--model",
        model,
        prompt,
    ]
    try:
        result = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=_CLI_TIMEOUT_SEC,
        )
    except FileNotFoundError:
        return -1, ""
    except subprocess.TimeoutExpired:
        return -1, ""
    return result.returncode, result.stdout + result.stderr


def normalize_verdict_file() -> None:
    """Promote a legacy verdict file to review-codex.json, as the workflow does."""
    if Path(REVIEW_FILE).is_file():
        return
    for candidate in LEGACY_VERDICT_FILES:
        path = Path(candidate)
        if not path.is_file():
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            continue
        payload.setdefault("early_exit", False)
        Path(REVIEW_FILE).write_text(json.dumps(payload), encoding="utf-8")
        print(f"Normalized {candidate} -> {REVIEW_FILE}")
        return


def accept_direct_write() -> bool:
    """True when the CLI wrote a verdict file that parses; stamps its status."""
    path = Path(REVIEW_FILE)
    if not path.is_file() or not path.stat().st_size:
        return False
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return False
    stamp_model_status(payload)
    path.write_text(json.dumps(payload), encoding="utf-8")
    print(f"{REVIEW_FILE} written directly by the CLI")
    return True


def main() -> None:
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

    if accept_direct_write():
        return

    detail = log_tail(log)
    if Path(REVIEW_FILE).is_file():
        print(
            "::warning::Codex wrote malformed verdict JSON -- replacing with"
            " error verdict",
            file=sys.stderr,
        )
    elif exit_code == 0:
        # Low-reasoning models answer in text instead of using the sandbox
        # tools; the verdict is then in the run log.
        extract = subprocess.run(
            [
                sys.executable,
                str(Path(__file__).resolve().parent / "extract_codex_json.py"),
                RUN_LOG,
                REVIEW_FILE,
            ],
        )
        if extract.returncode == 0:
            print(f"Extracted Codex verdict JSON from {RUN_LOG}")
            return
        print(
            "::warning::Codex emitted output but no parseable verdict JSON found",
            file=sys.stderr,
        )
    else:
        print(
            f"::warning::Codex review failed (exit={exit_code}) -- emitting error"
            " verdict",
            file=sys.stderr,
        )
        Path(REVIEW_FILE).write_text(
            json.dumps(
                error_verdict(
                    f"Codex review failed: CLI exited {exit_code}",
                    "cli_invocation_failed",
                    detail,
                ),
                indent=2,
            ),
            encoding="utf-8",
        )
        return

    Path(REVIEW_FILE).write_text(
        json.dumps(
            error_verdict(
                "Codex review failed: no parseable verdict JSON in output",
                "output_unparseable",
                detail,
            ),
            indent=2,
        ),
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()
