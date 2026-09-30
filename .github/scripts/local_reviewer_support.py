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
import re
import signal
import sys
import traceback
from collections.abc import Callable
from pathlib import Path
from typing import Any

# Re-exported for the two shims: the payload shape is aggregate_reviews.py's
# contract and now lives in the neutral module, so a shim importing it from
# here still gets the aggregate's own functions and not a second copy
# (AT-2510). It cannot live here -- this module is the LOCAL driver's, and
# the Actions-path aggregate must not be made to import it.
from github_pr_support import (  # noqa: F401
    is_valid_review,
    normalize_severity,
    usable_verdict,
)
from local_review_config import LocalConfig

# The `error` values aggregate_reviews.py distinguishes. Named here so a
# shim cannot invent a fourth spelling that renders as an unknown failure.
ERROR_CLI_FAILED = "cli_invocation_failed"
ERROR_UNPARSEABLE = "output_unparseable"
ERROR_CRASHED = "reviewer_crashed"

# Exit codes for failures that happen instead of the CLI running, rather
# than in it. Kept distinct all the way to the verdict: warning about them
# differently and then returning one value merged them again a frame later.
#
# BELOW `SIGNAL_EXIT_FLOOR`, and that is the whole of what keeps them from
# colliding. "Negative" was the earlier claim and it was false:
# CompletedProcess.returncode is `-N` for a child killed by signal N, so
# -1, -2 and -3 were SIGHUP, SIGINT and SIGQUIT, and a CLI that died on a
# SIGHUP was reported to the operator as "the CLI is not installed or not
# on PATH" -- a diagnosis of a fault that did not occur, in the one field
# the aggregate renders as the reason. The driver's own kill path uses
# SIGTERM and SIGKILL, but the direct run documented in docs/local-review.md
# and any supervisor signalling the CLI reach these. A signal number cannot
# exceed SIGRTMAX, so nothing below that floor is a returncode any child
# can produce; test_sentinels_cannot_be_a_signal_death is what holds it.
SIGNAL_EXIT_FLOOR = -signal.NSIG
EXIT_NOT_INSTALLED = SIGNAL_EXIT_FLOOR - 1
EXIT_TIMED_OUT = SIGNAL_EXIT_FLOOR - 2
EXIT_SPAWN_FAILED = SIGNAL_EXIT_FLOOR - 3

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
    """Note that a shim was started by something other than the driver.

    INFORMATIONAL, and only that. It used to be the whole of what stood
    between a directly-launched shim and an unbounded child environment,
    which a reviewer named for what it was: a warning is a diagnostic, not
    a boundary, and the boundary is now `reviewer_cli_env` -- applied in
    the shim, so it holds however the shim was started.

    Kept because the two paths are still not identical: the driver resolves
    `--config` and passes it down, and it alone prints the withheld names.
    Re-running a shim by hand inside a run directory is how a review is
    debugged, and saying which of the two is running is worth a line.
    """
    if DRIVER_ENV_MARKER in os.environ:
        return
    print(
        f"::warning::started outside the local review driver, so the {cli} CLI"
        " gets this shell's allowlisted names rather than the driver's"
        " filtered set -- same allowlist, but --config is not passed down"
        f" and the withheld names are not printed ({DRIVER_ENV_MARKER} is"
        " unset)",
        file=sys.stderr,
    )


# The operator's own additions, resolved like every other setting: process
# environment first, then the config file. Comma-separated names.
PASSTHROUGH_SETTING = "LENS_REVIEWER_ENV_PASSTHROUGH"
# What every reviewer subprocess inherits from the operator's environment.
# An ALLOWLIST, because the reviewer is an LLM CLI reading the head of a
# pull request anyone can open: handed the whole environment it also holds
# the operator's GitHub token, cloud keys and every other service token that
# happens to be exported, none of which any part of a review needs.
#
# Each entry is here for a reason that can be stated, and the reasons are
# two: the process must be able to start and reach the model API, or this
# repository's own reviewer code reads the name. NO CREDENTIAL FOR THE
# REVIEWER'S OWN CLI IS NAMED HERE -- see review_pr_local.reviewer_env for
# why that is the operator's decision and how they make it.
#
# HERE rather than in the driver because the driver is not the only thing
# that spawns a CLI. Both shims run standalone -- by hand, by a wrapper,
# by a Makefile -- and while the table lived in the driver that path had no
# allowlist at all, only a warning that it had none. One table, applied at
# every spawn.
#
# Both tables are annotated because neither is an enumeration: they are
# LISTS OF NAMES, and a name is compared against os.environ's keys, never
# passed where one of these spellings is required. Left to inference the
# tuple became `tuple[Literal['PATH'], ...]`, which made the operator's own
# `tuple[str, ...]` an argument error, and the dict became
# `dict[str, Unknown]` -- unchecked rather than over-checked, the worse of
# the two, since a value of the wrong shape there would pass silently.
REVIEWER_ENV_ALLOWLIST: tuple[str, ...] = (
    "PATH",  # the shims spawn `claude` / `codex` by name
    "HOME",  # `claude login` / `codex login` write under it; so does --config
    "SHELL",  # the claude CLI's allowed Bash tools (`cat pr.diff`) need one
    "TMPDIR",  # an operator who set it did so because the default is unusable
    "LANG",  # a diff is decoded by the locale's codec; a non-ASCII path
    "LC_ALL",  # otherwise fails to decode in a reviewer that should read it
    "LC_CTYPE",
    "HTTP_PROXY",  # on a proxied machine these are the only route to the
    "HTTPS_PROXY",  # model API; lowercase too, since libcurl and requests
    "NO_PROXY",  # read the lowercase spelling and Node reads the upper
    "http_proxy",
    "https_proxy",
    "no_proxy",
    "SSL_CERT_FILE",  # a TLS-inspecting proxy is reached only with its CA
    "SSL_CERT_DIR",  # bundle; all four name FILES, not secrets
    "REQUESTS_CA_BUNDLE",
    "NODE_EXTRA_CA_CERTS",
    "LENS_LOCAL_CONFIG",  # the shims call LocalConfig.load() with no argument
    # The operator's own list of names, forwarded so the SHIM can resolve
    # the same passthrough the driver did. Withheld, a shim under the driver
    # re-derived a SHORTER allowlist than the one it was handed and dropped
    # the very credential the operator had named -- the filter being too
    # narrow, which breaks a working review as surely as too wide leaks one.
    # A list of names, never a value.
    PASSTHROUGH_SETTING,
)
# Per reviewer, because a name only one reviewer reads has no business in
# another's environment -- GOOGLE_AI_API_KEY in the `claude` CLI's process
# is the finding this allowlist exists for, in miniature. Read with `.get`
# and an empty default, so a reviewer added to REVIEWER_NAMES and missed
# here inherits nothing extra rather than everything.
REVIEWER_ENV_EXTRA: dict[str, tuple[str, ...]] = {
    # Both read by review_claude_local.cli_environ; an operator who raised
    # API_TIMEOUT_MS must keep it, or the Python bound derived from it kills
    # the CLI before its own deadline (record 2-D).
    "claude": ("API_TIMEOUT_MS", "CLAUDE_STREAM_IDLE_TIMEOUT_MS"),
    "codex": (),
    # Both read by name in review_gemini.py -- that module already decides
    # gemini authenticates with this key, so forwarding it decides nothing.
    "gemini": ("GOOGLE_AI_API_KEY", "GEMINI_MAX_OUTPUT_TOKENS"),
}
ENV_NAME_RE = re.compile(r"\A[A-Za-z_][A-Za-z0-9_]*\Z")


def passthrough_names(config: LocalConfig) -> tuple[str, ...]:
    """The extra variable names the operator asked to forward.

    Malformed entries are dropped WITH A WARNING rather than passed on: a
    name with a space or a `=` in it can never match a variable, so
    forwarding it silently would leave an operator reading their own
    setting back and still not getting the value.
    """
    # `get` alone would raise: it ends at the workflow YAML, and this
    # setting describes running OFF Actions, so no `vars.NAME || 'default'`
    # declares it. `is_overridden` is the existing answer to "did the
    # operator set this, rather than the workflow", and asking it first
    # keeps the one precedence order -- environment, then config file --
    # instead of a second one written here.
    if not config.is_overridden(PASSTHROUGH_SETTING):
        return ()
    names: list[str] = []
    for token in config.get(PASSTHROUGH_SETTING).split(","):
        candidate = token.strip()
        if not candidate:
            continue
        if not ENV_NAME_RE.match(candidate):
            print(
                f"::warning::{PASSTHROUGH_SETTING} entry {candidate!r} is not"
                " an environment variable name; ignoring it",
                file=sys.stderr,
            )
            continue
        names.append(candidate)
    return tuple(names)


def reviewer_env_allowlist(name: str, config: LocalConfig) -> set[str]:
    """Every environment name reviewer `name` may inherit.

    ONE answer, asked twice: by the driver, building the environment it
    hands the shim, and by the shim, building the environment it hands the
    CLI. The two must agree or the second undoes the first -- wider and the
    shim reopens the hole the driver closed, narrower and it drops a
    credential the driver was asked to forward.
    """
    allowed = set(REVIEWER_ENV_ALLOWLIST)
    allowed.update(REVIEWER_ENV_EXTRA.get(name, ()))
    allowed.update(passthrough_names(config))
    return allowed


def reviewer_cli_env(name: str, base: dict[str, str] | None = None) -> dict[str, str]:
    """The environment a shim hands its CLI: `base`, under the allowlist.

    THE BOUNDARY, and it is here rather than in the driver because the
    driver is not always in the picture. Run standalone the shims passed
    `os.environ` straight through -- the Claude shim explicitly, as
    `{**cli_env(), **os.environ}`, and the Codex shim by passing no `env=`
    at all -- so every exported GitHub, cloud and service token reached an
    LLM CLI and everything it spawns. Under the driver it was already
    filtered, so this re-filter is a no-op there and the fix costs the
    supported path nothing.

    `base` is what the shim needs UNDER the operator's environment: the
    Claude shim's composite defaults, which stand in for a runner that is
    not here to set them. It is not filtered -- it is the shim's own,
    already-bounded values, not anything inherited -- and the operator's
    allowlisted names still win over it, which is the documented order.
    """
    allowed = reviewer_env_allowlist(name, LocalConfig.load())
    env = dict(base or {})
    env.update({k: v for k, v in os.environ.items() if k in allowed})
    return env


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


def exit_reason(exit_code: int, timeout_sec: int) -> str:
    """spawn_exit_reason, with the wording both shims fall back to.

    HERE and not once per shim: the fallback is a CROSS-SHIM contract, not a
    default. The aggregate reads one `error_detail` field whoever wrote it,
    and the defect this wording settled was the two shims disagreeing about
    it on the same run -- so a second copy that drifts reopens exactly the
    thing the first one closed. spawn_exit_reason stays separate because it
    answers "" for a code it does not own, which is what forces a shim
    adding one to say what it means.
    """
    return spawn_exit_reason(exit_code, timeout_sec) or f"CLI exited {exit_code}"


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
