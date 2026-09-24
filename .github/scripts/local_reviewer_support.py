#!/usr/bin/env python3
"""The verdict shape aggregate_reviews.py reads, written once.

This is NOT a merge of the two reviewer shims. What lives here is the half
of them that is not theirs: the failed-verdict envelope, the failure-kind
vocabulary, and the guarantee that a reviewer process leaves a verdict file
behind even when it raises. All three are aggregate_reviews.py's contract,
and that is one module reading all three reviewers -- so a copy per shim is
a copy of a third party's schema, not of a per-CLI decision.

The record of the discarded attempt is explicit about the cost of the other
arrangement: `error_verdict` went from near-identical to byte-identical
across the two shims, and "the prior round's timeout-handling fixes already
had to be applied twice" as a result.

What deliberately stays in each shim: the argv, how the prompt is delivered,
the timeout, the fallback chain, and the wording of its own exit reasons.
Those differ per CLI, and merging them is what would put a branch on two
contracts in one function.

The driver-spawned marker is here for the same reason as the rest: both
shims make the same promise about it and the driver sets it, so one name in
one place is what keeps the three sides agreeing.
"""

from __future__ import annotations

import json
import os
import sys
import traceback
from collections.abc import Callable
from pathlib import Path
from typing import Any

# The `error` values aggregate_reviews.py distinguishes. Named here so a
# shim cannot invent a fourth spelling that renders as an unknown failure.
ERROR_CLI_FAILED = "cli_invocation_failed"
ERROR_UNPARSEABLE = "output_unparseable"
ERROR_CRASHED = "reviewer_crashed"

# Exit codes for failures that happen instead of the CLI running, rather
# than in it. Negative so they cannot collide with a real CLI exit status.
# Kept distinct all the way to the verdict: warning about them differently
# and then returning one value merged them again a frame later.
EXIT_NOT_INSTALLED = -1
EXIT_TIMED_OUT = -2
EXIT_SPAWN_FAILED = -3

# Set by the driver on a reviewer's environment AFTER its allowlist filter
# has run, and deliberately NOT one of the allowlisted names: a value an
# operator exported is therefore dropped by the filter and replaced by the
# driver's own, so inside a driver-spawned shim this name means the driver
# and nothing else. PRESENCE is the entire signal. Nothing is inferred from
# an absence beyond "the driver did not set this", which is precisely what
# the warning below says -- a shim that read absence as evidence of
# anything more is the check this file is careful not to be.
DRIVER_ENV_MARKER = "LENS_REVIEWER_ENV_FILTERED"


def warn_unless_driver_spawned(cli: str) -> None:
    """Warn when a shim was started by something other than the driver.

    Not a refusal. Re-running a shim by hand inside a run directory is how
    a review is debugged, and that path keeps working. What it does not
    keep is the allowlist, which lives in the driver
    (review_pr_local.reviewer_env, where the entries and their reasons are)
    -- so the warning states the consequence rather than only that
    something is unusual. A second allowlist here would be a second copy of
    that table, free to drift from the one that is enforced.
    """
    if DRIVER_ENV_MARKER in os.environ:
        return
    print(
        f"::warning::started outside the local review driver, so the {cli} CLI"
        " is handed this shell's whole environment -- every token exported"
        " here, not the filtered set review_pr_local.reviewer_env builds"
        f" ({DRIVER_ENV_MARKER} is unset)",
        file=sys.stderr,
    )


def error_verdict(summary: str, kind: str, detail: str) -> dict[str, Any]:
    """The failed-verdict envelope, with the summary the caller's path means.

    `status` is "failed" and is never derived from the model: that value is
    reserved for reviewer infrastructure failures, which is what every
    caller of this function is reporting.
    """
    return {
        "summary": summary + (f" -- {detail}" if detail else ""),
        "status": "failed",
        "early_exit": False,
        "issues": [],
        "error": kind,
        "error_detail": detail,
    }


def write_verdict(path: str, payload: dict[str, Any]) -> None:
    Path(path).write_text(json.dumps(payload, indent=2), encoding="utf-8")


def spawn_exit_reason(exit_code: int, timeout_sec: int) -> str:
    """The operator-facing reason for a negative exit code, or "" if unknown.

    Returns "" rather than guessing for a code it does not own, so a shim
    that adds one of its own is forced to say what it means instead of
    having this function speak for it.
    """
    if exit_code == EXIT_NOT_INSTALLED:
        return "the CLI is not installed or not on PATH"
    if exit_code == EXIT_TIMED_OUT:
        return f"the CLI did not finish within {timeout_sec}s"
    if exit_code == EXIT_SPAWN_FAILED:
        return "the CLI could not be started"
    return ""


def captured_text(captured: str | bytes | None) -> str:
    """One half of what a killed CLI had printed, as text.

    `capture_output=True` leaves it on subprocess.TimeoutExpired, and it
    arrives as BYTES even under `text=True`: _check_timeout raises before
    _communicate decodes (measured on CPython 3.11.9). None means that
    stream buffered nothing. Here rather than in a shim because both shims
    read the same attributes off the same exception type -- what stays per
    shim is how the two halves are composed into that CLI's run log.
    """
    if captured is None:
        return ""
    if isinstance(captured, bytes):
        return captured.decode("utf-8", errors="replace")
    return captured


def guarded_main(review: Callable[[], None], name: str, review_file: str) -> None:
    """Run a reviewer; leave a verdict file behind even if it raises.

    Enumerating the raises is what kept failing in the discarded attempt:
    each fix closed the one raise it was shown -- a PermissionError on
    spawn, then an unparseable action.yml, then a renamed composite input --
    and the next raise site reopened the same hole, with the module exiting
    by traceback, writing nothing, and the aggregate counting an ABSENT
    reviewer where a FAILED one was meant (AT-1837).

    So the property is enforced here instead of re-established per edit.
    The traceback still reaches the run log the driver captures; the
    specific handlers in each shim stay, because they are what makes the
    verdict name the actual fault rather than this one's catch-all text.
    """
    try:
        review()
    except Exception as exc:  # noqa: BLE001 -- the catch IS the contract
        traceback.print_exc()
        print(
            f"::warning::the {name} reviewer raised before writing a verdict"
            f" -- emitting error verdict ({ERROR_CRASHED})",
            file=sys.stderr,
        )
        write_verdict(
            review_file,
            error_verdict(
                f"{name} review failed: the reviewer raised before writing a verdict",
                ERROR_CRASHED,
                f"the local {name} reviewer raised {exc!r};"
                " see the run log for the traceback",
            ),
        )
