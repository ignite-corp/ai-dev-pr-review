"""Only a verdict file THIS run produced may stand as a verdict (AT-2614).

The caller repo is checked out at the workspace root with no `path:`, in
the reviewer job and in the aggregate job alike, so `review-<name>.json`
is a path the PR under review can commit. Where nothing clears it, the
committed file is read as that reviewer's verdict: in the reviewer job by
the `always()` net and the uploader, in the aggregate job whenever the
artifact is simply absent -- which needs no dead step at all, because a
claude early_exit skips both downstream jobs by design.

The assertions here are ORDER assertions, not presence assertions. A clear
step that exists but runs after the reviewer, or after the upload, closes
nothing; the hole reopens on a reordering that a presence check would pass.

`test_parser_finds_the_codex_provenance_steps` is the positive control: the
codex path already carries all three parts on today's tree, so a parser
that reports zero for it is broken and every other result in this module is
meaningless.
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

# The roster is imported, never copied: `aggregate_reviews.REVIEWERS` is
# built from this same constant and is what reads `review-<name>.json` out
# of the CWD. A local tuple here would make the module blind to the drift it
# exists to catch -- a fourth reviewer whose committed verdict file no clear
# step removes.
from github_pr_support import REVIEWER_NAMES

_WORKFLOWS = Path(__file__).resolve().parents[2] / "workflows"
_SINGLE = _WORKFLOWS / "base-ai-review-single.yml"
_AGGREGATE = _WORKFLOWS / "base-ai-review-aggregate.yml"

POST_INLINE = "Post inline comments"
UPLOAD = "Upload review artifact"
# The third reviewer-generic reader. Its `jq -r '.early_exit // false'` is
# the one whose answer leaves this job: a PR-committed file saying
# `early_exit: true` makes the orchestrator skip the downstream jobs, which
# suppresses the review outright rather than merely misreporting it.
EARLY_EXIT = "Extract early exit flag"

# `for <var> in <names>; do`, the aggregate clear's hand-written roster.
_LOOP = re.compile(r"\bfor\s+(\w+)\s+in\s+([\w\s]+?);?\s*do\b")
# `inputs.reviewer == '<name>'`, the gate that says which reviewer a step runs for.
_GATE = re.compile(r"inputs\.reviewer\s*==\s*'(\w+)'")
# What a step must bind REVIEWER to for `${REVIEWER}` in its body to mean
# the dispatched reviewer. The expression the workflow actually writes.
_REVIEWER_BINDING = "${{ inputs.reviewer }}"
# `$REVIEWER` as a WHOLE variable name. A plain `"$REVIEWER" in text` also
# matches `$REVIEWER_APP_PRIVATE_KEY`, which this module's first spelling of
# the binding check did: it reported two steps that bind a secret, not the
# reviewer, as missing a binding they never needed -- and the substitution
# built on the same prefix would have rewritten that secret's name into
# `claudeAPP_PRIVATE_KEY`.
_REVIEWER_VAR = re.compile(r"\$\{REVIEWER\}|\$REVIEWER(?![A-Za-z0-9_])")
# A shell comment: `#` at the start of a word, to the end of the line. Every
# predicate reads the body with these removed, because a body that names a
# command only in a comment does not run it.
_SHELL_COMMENT = re.compile(r"(?:^|(?<=\s))#.*$", re.MULTILINE)


def _run_step_name(reviewer: str) -> str:
    """The reviewer step each provenance clear has to sit in front of.

    Derived rather than tabulated: a dict keyed by reviewer is a second copy
    of the roster, and a copy that nobody extends is the drift this module
    is about. A reviewer whose run step is named otherwise fails in `_index`
    with that name, which is the honest report.
    """
    return f"Run {reviewer.capitalize()} review"


# Reviewers with no `Emit <Name> error verdict` step on this tree, and why.
# Recorded and asserted rather than skipped over: a skip reads as "this
# reviewer's ordering was checked" on a green run. The reason is asserted
# too, not only the key, and is interpolated into the message of the
# control that guards the absence -- a value no assertion reads and no
# failure prints is a comment wearing a dict.
#
# AN ENTRY MUST BE DELETED WHEN THAT REVIEWER'S STEP LANDS. This said the
# opposite -- "nothing here needs editing when a step lands" -- and it was
# wrong in a way that costs exactly what the module protects. Once `emit`
# is non-empty the key is unreachable, so it stops being a record and
# becomes a standing permission: a later rename that changes the step's
# name prefix empties `emit` again, the absence branch finds the stale
# reason waiting, and the ordering assertion for that reviewer is dropped
# on a green run. gemini's entry was in that state the moment AT-2539
# merged. Only a prefix change does it -- `_emit_steps` matches on
# `Emit <Name> error verdict`, so a parenthetical suffix change does not
# empty the lookup -- which is narrower than it sounds, not safer: it is
# the renames nobody is thinking about that arrive unannounced.
NO_ERROR_VERDICT_STEP = {
    "codex": "codex has no error-verdict step by design: it fails its job instead",
}


def _steps(path: Path, job: str) -> list[dict[str, Any]]:
    workflow = yaml.safe_load(path.read_text(encoding="utf-8"))
    return list(workflow["jobs"][job]["steps"])


def _single_steps() -> list[dict[str, Any]]:
    return _steps(_SINGLE, "review")


def _aggregate_steps() -> list[dict[str, Any]]:
    return _steps(_AGGREGATE, "aggregate")


def _reviewer_gates(step: dict[str, Any]) -> set[str]:
    """The reviewer names this step's `if:` restricts it to, if any.

    Empty means the step runs for every reviewer -- `always()` with no
    reviewer clause -- not that it runs for none.
    """
    return set(_GATE.findall(str(step.get("if", ""))))


def _body(step: dict[str, Any]) -> str:
    """The step's `run:` text with shell comments removed.

    Every predicate reads this and not the raw text: a marker, a removal or a
    loop named only in a comment executes nothing.
    """
    return _SHELL_COMMENT.sub("", str(step.get("run", "")))


def _run_text(step: dict[str, Any], reviewer: str) -> str:
    """The step's shell body, with the reviewer placeholders resolved.

    Two spellings stand in for a reviewer name and both have to be read as
    one. `${REVIEWER}` is a shell variable the runner fills from the step's
    own `env:` block; the aggregate's clear instead loops a shell variable
    over the three names. Resolving both here is what lets one set of
    assertions read every path.

    THE BINDING IS READ, NOT THE GATE. This substituted whenever the `if:`
    named the reviewer, which made the `env: { REVIEWER: ... }` block
    invisible to every assertion built on this function: deleting all four
    env blocks left the whole repository's suite green -- 1388 passed --
    while at runtime `set -euo pipefail` hits `REVIEWER: unbound variable`,
    the step exits 1, and on the two clears that carry no `always()` the
    job dies there with the PR's committed file still on disk and the
    reviewer never run. An assertion about a string the step would not
    execute is not an assertion about the workflow. The repo already does
    this correctly one step over, in
    `test_claude_review_timeout.py`'s check that the guard's env is
    `${{ inputs.reviewer }}`.

    A body that uses the placeholder with no such binding raises rather
    than silently resolving: a missing premise must not read as a passing
    assertion, which is the failure this docstring records.

    Comments are stripped first: a marker or a removal named only in a
    comment executes nothing, and a substring search that read comments let
    a net with its marker check deleted still pass as a net.
    """
    text = _body(step)
    if _REVIEWER_VAR.search(text):
        binding = (step.get("env") or {}).get("REVIEWER")
        if binding != _REVIEWER_BINDING:
            raise AssertionError(
                f"step {step.get('name')!r} uses $REVIEWER in its body but does not bind "
                f"it to {_REVIEWER_BINDING!r} (env.REVIEWER is {binding!r}); under "
                "`set -euo pipefail` the step aborts on an unbound variable, so any "
                "assertion about this body would describe a command that never runs"
            )
        gates = _reviewer_gates(step)
        # Gated to someone else: the step never runs for this reviewer, so
        # the placeholder is deliberately left unresolved and the callers'
        # predicates answer False -- substituting here would report the
        # claude clear as clearing gemini's file.
        if not gates or reviewer in gates:
            text = _REVIEWER_VAR.sub(reviewer, text)
    loop = _LOOP.search(text)
    if loop and reviewer in loop.group(2).split():
        var = loop.group(1)
        text = text.replace("${" + var + "}", reviewer).replace("$" + var, reviewer)
    return text


def _loop_roster(step: dict[str, Any]) -> set[str] | None:
    """The reviewer names a `for ... in ...; do` in this step iterates."""
    loop = _LOOP.search(_body(step))
    return set(loop.group(2).split()) if loop else None


def _clears(step: dict[str, Any], reviewer: str) -> bool:
    """Does this step remove `review-<reviewer>.json`?

    Two spellings count, the two the repo uses: the one owner of the
    removal, `promote_legacy_verdict.sh clear <target>`, and the `rm -rf`
    fallback for the window where that script is not in the pinned
    checkout. `rm -f` does not count: a directory committed at the name
    fails a plain rm and under the runner's bash -e takes the step down.
    """
    text = _run_text(step, reviewer)
    target = re.escape(f"review-{reviewer}.json")
    clear = rf"""\bclear\b[^\n]*['"]?{target}['"]?"""
    remove = rf"""\brm\s+(-[a-zA-Z]*\s+)*-[a-zA-Z]*r[a-zA-Z]*f[a-zA-Z]*\b[^\n]*{target}"""
    return bool(re.search(clear, text) or re.search(remove, text))


def _writes_marker(step: dict[str, Any], reviewer: str) -> bool:
    """Does this step CREATE the `<reviewer>-start` marker?

    The redirect itself, `: > "${RUNNER_TEMP}/<reviewer>-start"`, and not
    the marker's name plus a colon somewhere in the body: that looser test
    matched a colon inside a comment, so it classified the read-only
    `Normalize review file name` as a writer and was a slower spelling of
    `_reads_marker`, which makes the ordering assertion that rests on it
    claim a guarantee nothing checked.
    """
    pattern = rf""":\s*>\s*"?\$\{{RUNNER_TEMP\}}/{re.escape(reviewer)}-start"""
    return bool(re.search(pattern, _run_text(step, reviewer)))


def _reads_marker(step: dict[str, Any], reviewer: str) -> bool:
    return f"{reviewer}-start" in _run_text(step, reviewer)


def _index(steps: list[dict[str, Any]], name: str) -> int:
    for i, step in enumerate(steps):
        if step.get("name") == name:
            return i
    raise AssertionError(f"no step named {name!r}")


def _is_always(step: dict[str, Any]) -> bool:
    return "always()" in str(step.get("if", ""))


def _assert_not_advisory(step: dict[str, Any], role: str) -> None:
    """A removal step may not be `continue-on-error`.

    `continue-on-error: true` turns a failed removal into a passing job,
    which is the same outcome as having no step at all -- the protection
    becomes advisory with nothing saying so. Asserted rather than assumed
    because it is one word, added anywhere in this file, with no other test
    in the repository standing between it and a green run.
    """
    assert not step.get("continue-on-error", False), (
        f"the {role} {str(step.get('name'))!r} is continue-on-error, so a failed "
        "removal passes the job and the PR-committed file survives unreported"
    )


def _emit_steps(steps: list[dict[str, Any]], reviewer: str) -> list[int]:
    prefix = f"Emit {reviewer.capitalize()} error verdict"
    return [i for i, step in enumerate(steps) if str(step.get("name", "")).startswith(prefix)]


def _verify_steps(steps: list[dict[str, Any]], reviewer: str) -> list[int]:
    """The reviewer's `Verify <Name> verdict file` steps.

    codex alone has one, and on that path it is the EARLIEST reader: it sits
    ahead of all three reviewer-generic readers, so a reordering that slipped
    the net past it would be caught by nothing else. `Normalize review file
    name`'s own comment names it -- a PR-committed file is what "'Verify
    Codex verdict file' would otherwise report as a verdict present" -- so
    the reader the net is justified by was the reader not asserted. Empty is
    allowed, the way `_emit_steps` and `_log_fallback_steps` are.
    """
    prefix = f"Verify {reviewer.capitalize()} verdict file"
    return [i for i, step in enumerate(steps) if str(step.get("name", "")).startswith(prefix)]


def _log_fallback_steps(steps: list[dict[str, Any]], reviewer: str) -> list[int]:
    """The reviewer's `Extract <Name> review from execution log` steps.

    Collected by name prefix and allowed to come back empty, the way
    `_emit_steps` is: only claude has a log fallback on this tree, and an
    index tabulated per reviewer would be a second roster for a step two of
    the three do not have.
    """
    prefix = f"Extract {reviewer.capitalize()} review from execution log"
    return [i for i, step in enumerate(steps) if str(step.get("name", "")).startswith(prefix)]


def test_every_recorded_absence_is_still_an_absence() -> None:
    """No `NO_ERROR_VERDICT_STEP` key may name a reviewer that has the step.

    The gate for the hazard described above that dict, rather than a note
    asking the next person to remember: an entry whose reviewer has since
    gained the step is unreachable, and an unreachable entry is a standing
    permission for a prefix-changing rename to drop that reviewer's
    ordering assertion on a green run. gemini's entry was in exactly that
    state from AT-2539's merge until it was removed, and nothing failed.
    """
    steps = _single_steps()
    live = {r: _emit_steps(steps, r) for r in NO_ERROR_VERDICT_STEP}
    assert not any(live.values()), (
        "NO_ERROR_VERDICT_STEP records an absence that is no longer one -- delete "
        f"the entry now that the step exists: {({r: i for r, i in live.items() if i})}"
    )
    # Positive control: every key coming back empty is also what a lookup
    # that can find nothing at all would produce, which would make the
    # assertion above pass for the wrong reason.
    assert any(_emit_steps(steps, r) for r in REVIEWER_NAMES), (
        "the error-verdict lookup finds no step for any reviewer, so the absences "
        "above are unverified rather than confirmed"
    )


def test_parser_finds_the_codex_provenance_steps() -> None:
    """Positive control: codex already has all three parts on this tree."""
    steps = _single_steps()
    run_index = _index(steps, _run_step_name("codex"))
    assert _clears(steps[run_index], "codex"), "parser does not see the codex pre-run clear"
    assert _writes_marker(steps[run_index], "codex"), "parser does not see codex-start"

    net_index = _index(steps, "Normalize review file name")
    assert net_index > run_index
    assert _is_always(steps[net_index])
    assert _clears(steps[net_index], "codex"), "parser does not see the codex net's clear"
    assert _reads_marker(steps[net_index], "codex"), "parser does not see the net's marker read"
    # And the negative half of the same control: the net only reads the
    # marker, so a `_writes_marker` that answers True here is matching
    # something other than the redirect and cannot separate the two roles.
    assert not _writes_marker(steps[net_index], "codex"), (
        "parser reports the read-only net as a writer of codex-start"
    )


def _commented_out(run: str, pattern: str) -> str:
    """`run` with every executable line matching `pattern` turned into a comment."""
    lines = []
    for line in run.splitlines():
        body = line.lstrip()
        if re.search(pattern, line) and not body.startswith("#"):
            line = line[: len(line) - len(body)] + "# " + body
        lines.append(line)
    return "\n".join(lines) + "\n"


@pytest.mark.parametrize("reviewer", REVIEWER_NAMES)
def test_a_commented_out_net_is_not_read_as_a_net(reviewer: str) -> None:
    """The predicates read commands, not prose about them.

    A body that names `<reviewer>-start` or `clear review-<name>.json` only in
    a comment executes neither, yet a substring search over the whole `run:`
    text reported both. Removing the net's marker check while leaving a
    comment that mentions it then left every ordering assertion built on
    these predicates green. The copy here comments out the executable lines
    and keeps them as text, so the marker name is still in the body.
    """
    steps = _single_steps()
    step = steps[_index(steps, _NET_STEPS[reviewer])]
    assert _reads_marker(step, reviewer) and _clears(step, reviewer), (
        f"precondition: the live {reviewer} net must read as a net"
    )
    no_read = {**step, "run": _commented_out(str(step["run"]), r"-start\b")}
    assert "-start" in no_read["run"], "the copy must still mention the marker"
    assert not _reads_marker(no_read, reviewer), (
        f"a {reviewer} net whose marker check exists only as a comment still "
        "reads as checking the marker"
    )
    no_clear = {
        **step,
        "run": _commented_out(str(step["run"]), r"\bclear\b|\brm\s+-rf\b"),
    }
    assert not _clears(no_clear, reviewer), (
        f"a {reviewer} net whose removal exists only as a comment still reads "
        f"as clearing review-{reviewer}.json"
    )


@pytest.mark.parametrize("reviewer", REVIEWER_NAMES)
def test_verdict_file_is_cleared_before_the_reviewer_runs(reviewer: str) -> None:
    """A file the PR committed cannot survive into the reviewer's own run.

    `<=` and not `<`: codex clears inside its run step, immediately before
    the CLI. What the assertion rejects is a clear that happens after.
    """
    steps = _single_steps()
    run_index = _index(steps, _run_step_name(reviewer))
    cleared = [
        i
        for i, step in enumerate(steps)
        if _clears(step, reviewer) and _writes_marker(step, reviewer)
    ]
    assert cleared, f"no step clears review-{reviewer}.json and marks {reviewer}-start"
    if min(cleared) != run_index:
        _assert_not_advisory(steps[min(cleared)], "pre-run clear")
    else:
        # codex clears inside `Run Codex review`, and the reviewer run steps
        # are `continue-on-error` by design -- a reviewer's own death must
        # not fail the job, which is what lets each path record its own
        # failure (AT-2539's premise). Asserting otherwise here would demand
        # the opposite of that design. What stands in for it is the net,
        # which is a dedicated step and is held to the rule below.
        assert steps[run_index].get("continue-on-error") is True, (
            f"{_run_step_name(reviewer)!r} clears in-step but is not "
            "continue-on-error, so the reason this exemption exists is absent"
        )
        # Positive control: this branch must not be the only one taken, or
        # the rule above is never exercised by anyone.
        dedicated = [
            r
            for r in REVIEWER_NAMES
            if min(
                i
                for i, step in enumerate(steps)
                if _clears(step, r) and _writes_marker(step, r)
            )
            != _index(steps, _run_step_name(r))
        ]
        assert dedicated, (
            "every reviewer clears inside its run step, so the "
            "continue-on-error rule for dedicated clears is never checked"
        )
    assert min(cleared) <= run_index, (
        f"review-{reviewer}.json is cleared only after {_run_step_name(reviewer)!r}"
    )


@pytest.mark.parametrize("reviewer", REVIEWER_NAMES)
def test_a_net_clears_the_verdict_file_when_the_marker_is_absent(reviewer: str) -> None:
    """A reviewer step that never started leaves nothing of the PR's behind.

    The net has to run before every step that would otherwise read the
    committed file as this run's answer: the inline poster, the uploader
    whose artifact the aggregate trusts, the early-exit reader whose flag
    skips the downstream jobs, and the claude log fallback, whose
    `if [ -f "review-claude.json" ]; then ... exit 0` opening makes a file
    left standing suppress the honest extraction rather than be replaced by
    it. Three of the four exist for every reviewer; the log fallback is
    claude's alone and is the only one allowed to be absent.
    """
    steps = _single_steps()
    run_index = _index(steps, _run_step_name(reviewer))
    nets = [
        i
        for i, step in enumerate(steps)
        if i > run_index
        and _is_always(step)
        and _reads_marker(step, reviewer)
        and _clears(step, reviewer)
    ]
    assert nets, (
        f"no always() step after {_run_step_name(reviewer)!r} clears review-{reviewer}.json "
        f"on a missing {reviewer}-start marker"
    )
    _assert_not_advisory(steps[min(nets)], "net")
    # The three reviewer-generic readers. Each is looked up through
    # `_index`, which raises on a name it cannot find rather than returning
    # an empty result -- these steps exist for every reviewer, so a lookup
    # that finds nothing is a failure here and never a fall-through. Named
    # one per line, and not bounded by each other: ordering is not
    # transitive across a reordering that moves a single step, so a reader
    # sitting between two asserted ones is not thereby asserted.
    assert min(nets) < _index(steps, POST_INLINE)
    assert min(nets) < _index(steps, UPLOAD)
    assert min(nets) < _index(steps, EARLY_EXIT), (
        f"the {reviewer} net runs after {EARLY_EXIT!r}, which would read the PR's "
        "committed early_exit flag as this run's and skip the downstream jobs"
    )
    verify = _verify_steps(steps, reviewer)
    if verify:
        assert min(nets) < min(verify), (
            f"the {reviewer} net runs after 'Verify {reviewer.capitalize()} verdict file', "
            "the earliest reader on this path, which would report a file the PR committed "
            "as this run's verdict present and pass the job on it"
        )
    else:
        # Positive control for the fall-through, as for the log fallback: an
        # empty list from a lookup that can find nothing would read the same.
        assert _verify_steps(steps, "codex"), (
            "the verify lookup finds no step for any reviewer, so an empty "
            f"list proves nothing about {reviewer}"
        )
    fallbacks = _log_fallback_steps(steps, reviewer)
    if fallbacks:
        assert min(nets) < min(fallbacks), (
            f"the {reviewer} net runs after 'Extract {reviewer.capitalize()} review from "
            "execution log', whose presence check would read a file the PR committed as "
            "this run's verdict and skip the extraction from the execution log"
        )
    else:
        # Positive control for the fall-through: an empty list from a lookup
        # that can find nothing at all would read exactly the same.
        assert _log_fallback_steps(steps, "claude"), (
            "the log-fallback lookup finds no step for any reviewer, so an "
            f"empty list proves nothing about {reviewer}"
        )


@pytest.mark.parametrize("reviewer", REVIEWER_NAMES)
def test_the_emitted_error_verdict_comes_after_the_net(reviewer: str) -> None:
    """A net that runs after the error-verdict step would delete the verdict.

    claude and gemini both have such a step; codex alone does not, because
    it fails its job instead. Only codex therefore reaches the absence
    branch, where the reason is asserted against `NO_ERROR_VERDICT_STEP`
    rather than skipped over, and the net is still required to exist. The
    other two run the ordering assertion itself -- gemini's since AT-2539
    merged, which is also when its `NO_ERROR_VERDICT_STEP` entry had to go
    (see the note above that dict).
    """
    steps = _single_steps()
    emit = _emit_steps(steps, reviewer)
    run_index = _index(steps, _run_step_name(reviewer))
    nets = [
        i
        for i, step in enumerate(steps)
        if i > run_index and _is_always(step) and _reads_marker(step, reviewer)
    ]
    assert nets, f"no {reviewer} net to order against"
    if not emit:
        reason = NO_ERROR_VERDICT_STEP.get(reviewer, "")
        # The reason itself and not just the key: a key whose reason is
        # empty records nothing, and the whole point of a mapping here
        # rather than a set is that the reason reaches a reader.
        assert reason, (
            f"{reviewer} has no 'Emit {reviewer.capitalize()} error verdict' step and no "
            "recorded reason for not having one; reasons are recorded for "
            f"{sorted(NO_ERROR_VERDICT_STEP)}"
        )
        # Positive control for that absence: an empty list from a lookup
        # that can find nothing at all would read exactly the same. The
        # reason rides along, because this is where it is wanted -- the
        # message says the absence could not be verified, and the record
        # says what the absence was expected to be.
        assert _emit_steps(steps, "claude"), (
            "the error-verdict lookup finds no step for any reviewer, so an empty list "
            f"proves nothing about {reviewer}, whose step is recorded missing because "
            f"{reason}"
        )
        return
    assert min(nets) < min(emit), (
        f"the {reviewer} net runs after its error-verdict step, which would "
        "clear the verdict that step just wrote"
    )


def test_aggregate_clears_every_verdict_file_before_the_first_download() -> None:
    """The aggregate's own checkout is an injection point of its own.

    It checks out the PR head at the workspace root and then downloads the
    artifacts into that same directory, each `continue-on-error`. An absent
    artifact therefore leaves the PR's committed file in place for
    `load_reviews()`, with no dead step anywhere in the chain.

    The clear's roster is written out by hand in the shell loop -- the step
    runs before `setup-python`, so the workflow cannot import the constant
    at runtime -- and is held to `REVIEWER_NAMES` here instead. A name added
    to that constant reaches `load_reviews()` and the loop alike; a name
    added to it and not the loop is AT-2614 reopened for that reviewer.
    """
    steps = _aggregate_steps()
    checkout = _index(steps, "Checkout caller repo (PR head)")
    downloads = [
        i for i, step in enumerate(steps) if "download-artifact" in str(step.get("uses", ""))
    ]
    assert downloads, "positive control: the aggregate downloads no artifacts at all"

    clears = [
        i
        for i, step in enumerate(steps)
        if any(_clears(step, reviewer) for reviewer in REVIEWER_NAMES)
    ]
    assert clears, "no aggregate step clears any review-<name>.json file"
    clear = min(clears)
    # The last of the removal steps to get this: the net and the pre-run
    # clear were held to it while this one, which has no always() step
    # behind it at all, was not. One word here makes the removal advisory
    # and the PR's committed file reaches load_reviews() on a green suite.
    _assert_not_advisory(steps[clear], "aggregate clear")
    assert _loop_roster(steps[clear]) == set(REVIEWER_NAMES), (
        "the aggregate clear's hand-written roster has drifted from "
        "github_pr_support.REVIEWER_NAMES, which is what aggregate_reviews."
        "REVIEWERS reads review-<name>.json out of the CWD for"
    )
    assert all(_clears(steps[clear], reviewer) for reviewer in REVIEWER_NAMES), (
        "the aggregate's clear step does not remove every review-<name>.json"
    )
    assert checkout < clear < min(downloads), (
        "the aggregate's clear must sit between the caller checkout that "
        "creates the files and the first artifact download"
    )


# The nets whose only job is the removal, by step name. The assertions below
# read their `run:` bodies out of the live workflow and execute them.
_NET_STEPS = {
    "claude": "Clear unowned Claude verdict file",
    "gemini": "Clear unowned Gemini verdict file",
    "codex": "Normalize review file name",
}
_FAILING_PROMOTE = "#!/usr/bin/env bash\nexit 1\n"
# How the runner executes a `run:` block when the step names no `shell:`,
# which none of these do: `bash -e {0}`. Without `-e` here a command that
# fails ahead of the removal is skipped over and the removal still runs, so
# a body that aborts on the runner reads as one that clears -- and the
# codex net carries no `set` line of its own to supply the flag.
_RUNNER_BASH = ["bash", "--noprofile", "--norc", "-e", "-c"]
# The four files a step writes its outputs, env, state and summary to. In a
# workflow run they are real paths belonging to the LIVE step, and
# `env={**os.environ, ...}` inherits them, so a body that gains an
# `echo ... >> "$GITHUB_OUTPUT"` line would write into the CI step wrapping
# this test instead of the sandbox. The step bodies here are pinned by NAME,
# not by content, so that line can arrive without touching this file.
_GITHUB_SINKS = ("GITHUB_OUTPUT", "GITHUB_ENV", "GITHUB_STATE", "GITHUB_STEP_SUMMARY")


def _sandboxed_env(tmp_path: Path, **extra: str) -> dict[str, str]:
    """`os.environ` with every GitHub write sink redirected under `tmp_path`."""
    sinks = tmp_path / "gh"
    sinks.mkdir(exist_ok=True)
    env = {**os.environ}
    for name in _GITHUB_SINKS:
        target = sinks / name.lower()
        target.touch()
        env[name] = str(target)
    env.update(extra)
    return env


def _run_net_body(
    tmp_path: Path, reviewer: str, *, promote: str | None, marker: bool
) -> subprocess.CompletedProcess[str]:
    """Execute a net's own `run:` block the way the runner does.

    Executed and not pattern-matched: the defect this guards is that the
    step ABORTS instead of removing, which no assertion about the command
    text can see. Only running it and looking at the filesystem can.
    """
    steps = _single_steps()
    step = steps[_index(steps, _NET_STEPS[reviewer])]
    runner_temp = tmp_path / "runner-temp"
    runner_temp.mkdir()
    if marker:
        (runner_temp / f"{reviewer}-start").touch()
    if promote is not None:
        # The scripts copy under RUNNER_TEMP, which is what the net runs
        # (AT-2941), not the workspace checkout.
        script = runner_temp / "ai-dev-pr-review/.github/scripts/promote_legacy_verdict.sh"
        script.parent.mkdir(parents=True)
        script.write_text(promote, encoding="utf-8")
        script.chmod(0o755)
    (tmp_path / f"review-{reviewer}.json").write_text(
        '{"summary":"the PR wrote this","status":"approved","issues":[]}', encoding="utf-8"
    )
    return subprocess.run(
        _RUNNER_BASH + [str(step["run"])],
        cwd=tmp_path,
        env=_sandboxed_env(tmp_path, RUNNER_TEMP=str(runner_temp), REVIEWER=reviewer),
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )


def test_every_reviewer_has_a_net_step() -> None:
    """`_NET_STEPS` is written by hand; the roster it must cover is not.

    The executing tests below are parametrized over `REVIEWER_NAMES`, so a
    reviewer missing here fails them with a KeyError; this names the gap
    instead, and also catches a name left behind after a reviewer is
    removed, which a parametrization never visits.
    """
    assert set(_NET_STEPS) == set(REVIEWER_NAMES), (
        f"_NET_STEPS covers {sorted(_NET_STEPS)} but REVIEWER_NAMES is "
        f"{sorted(REVIEWER_NAMES)}: a reviewer without an entry has a net nothing "
        "here executes"
    )


@pytest.mark.parametrize("reviewer", REVIEWER_NAMES)
def test_the_net_removes_the_file_even_when_the_script_fails(
    reviewer: str, tmp_path: Path
) -> None:
    """A promote script that EXISTS but exits non-zero must not save the file.

    `set -e` plus a bare `bash "$PROMOTE" clear ...` aborts the step, and an
    aborted net leaves the PR-committed file exactly where a net that was
    never written would have: read downstream as that reviewer's verdict.
    The evidence is the file's absence after execution, not the text of the
    command.
    """
    result = _run_net_body(tmp_path, reviewer, promote=_FAILING_PROMOTE, marker=False)
    assert not (tmp_path / f"review-{reviewer}.json").exists(), (
        f"the {reviewer} net left the PR's review-{reviewer}.json in place when "
        f"promote_legacy_verdict.sh exited non-zero (rc={result.returncode}, "
        f"stderr={result.stderr.strip()!r})"
    )


@pytest.mark.parametrize("reviewer", REVIEWER_NAMES)
def test_the_net_leaves_this_runs_file_alone(reviewer: str, tmp_path: Path) -> None:
    """Positive control for the test above.

    Without it, "the file is gone" would also be produced by a net that
    removes unconditionally -- which would delete the reviewer's own honest
    verdict. With the marker present the file is this run's and must
    survive, so the two together pin removal to exactly the unowned case.

    The return code is checked FIRST, because a surviving file is also what
    a body that aborted before reaching any removal produces -- an unbound
    variable, a syntax error, a failed `[ -f ]` chain. Without this the
    control could pass for exactly the reason it exists to exclude, and its
    paired test above only catches a net that aborts ALWAYS, never one that
    aborts on the marker-present path alone, which is this path.
    """
    result = _run_net_body(tmp_path, reviewer, promote=None, marker=True)
    assert result.returncode == 0, (
        f"the {reviewer} net exited {result.returncode} "
        f"(stderr={result.stderr.strip()!r}), so the surviving file proves nothing"
    )
    assert (tmp_path / f"review-{reviewer}.json").exists(), (
        f"the {reviewer} net removed review-{reviewer}.json although the "
        f"{reviewer}-start marker said this run had written it"
    )


_AGGREGATE_CLEAR = "Clear verdict files the PR checked out"
_PROMOTE_SCRIPT = Path(__file__).resolve().parents[1] / "promote_legacy_verdict.sh"


def _bash_array(text: str, name: str) -> tuple[str, ...]:
    """The words of the bash array assignment `name=(...)` in `text`."""
    found = re.search(rf"(?<![\w-]){re.escape(name)}=\((.*?)\)", text)
    if found is None:
        raise AssertionError(f"no `{name}=(...)` array to read")
    return tuple(found.group(1).split())


def _legacy_verdict_files() -> tuple[str, ...]:
    """The script's own LEGACY_VERDICT_FILES, read rather than copied.

    `clear` removes the target AND these, which is what using the script
    buys over a bare `rm review-<name>.json`. That difference is the only
    thing separating a correct implementation from three unconditional
    removals, so it is what the positive control below asserts -- and a copy
    kept here would agree with a stale YAML copy while the script moved on.
    """
    return _bash_array(
        _PROMOTE_SCRIPT.read_text(encoding="utf-8"), "LEGACY_VERDICT_FILES"
    )


# Fails the FIRST call only, then succeeds. `set -e` plus a bare call aborts
# the loop on the first name, so the remaining two are never swept -- which
# is the actual content of "the loop aborts at the first failure".
_PROMOTE_FAILING_ONCE = """#!/usr/bin/env bash
if [ ! -e .promote-called ]; then
  : > .promote-called
  exit 1
fi
shift || true
rm -rf -- "$@"
"""


def _run_aggregate_clear(
    tmp_path: Path, *, promote: str | None, planted: tuple[str, ...]
) -> subprocess.CompletedProcess[str]:
    """Execute the aggregate clear's own `run:` block the way the runner does."""
    steps = _aggregate_steps()
    step = steps[_index(steps, _AGGREGATE_CLEAR)]
    if promote is not None:
        script = tmp_path / ".ai-dev-pr-review/.github/scripts/promote_legacy_verdict.sh"
        script.parent.mkdir(parents=True)
        script.write_text(promote, encoding="utf-8")
        script.chmod(0o755)
    for name in planted:
        (tmp_path / name).write_text(
            '{"summary":"the PR wrote this","status":"approved","issues":[]}',
            encoding="utf-8",
        )
    return subprocess.run(
        _RUNNER_BASH + [str(step["run"])],
        cwd=tmp_path,
        env=_sandboxed_env(tmp_path),
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )


def _planted_verdicts() -> tuple[str, ...]:
    """One committed verdict file per reviewer, derived from the roster."""
    return tuple(f"review-{reviewer}.json" for reviewer in REVIEWER_NAMES)


def test_the_aggregate_clear_removes_every_file_when_the_script_fails(
    tmp_path: Path,
) -> None:
    """The aggregate clear is the only removal site with no net behind it.

    The reviewer job's nets are each other's and their own backstop; this
    step is alone -- `aggregate_reviews.py` reads straight out of this CWD
    and no `always()` step follows. A script that exists but exits non-zero
    aborts it under `set -euo pipefail`, and every planted file survives
    into `load_reviews()`.
    """
    planted = _planted_verdicts()
    assert len(planted) >= 3, f"fixture too small to separate the cases: {planted}"
    result = _run_aggregate_clear(tmp_path, promote=_FAILING_PROMOTE, planted=planted)
    survivors = [name for name in planted if (tmp_path / name).exists()]
    assert not survivors, (
        f"the aggregate clear left {survivors} in place when "
        f"promote_legacy_verdict.sh exited non-zero (rc={result.returncode}, "
        f"stderr={result.stderr.strip()!r})"
    )


def test_the_aggregate_clear_does_not_stop_at_the_first_failure(tmp_path: Path) -> None:
    """A failure on the first name must not leave the other two standing.

    Derived from the roster rather than counting to three, and asserted on
    the names AFTER the first: with `set -e` and a bare call the loop dies
    on name one, so a test that only checked name one would pass on the
    defect.
    """
    planted = _planted_verdicts()
    result = _run_aggregate_clear(
        tmp_path, promote=_PROMOTE_FAILING_ONCE, planted=planted
    )
    later = [name for name in planted[1:] if (tmp_path / name).exists()]
    assert not later, (
        f"the aggregate clear aborted on the first name and left {later} behind "
        f"(rc={result.returncode}, stderr={result.stderr.strip()!r})"
    )


def test_the_aggregate_clear_sweeps_the_legacy_names_too(tmp_path: Path) -> None:
    """Positive control: the normal path, with the REAL script.

    Every planted `review-<name>.json` is gone here as well, so file absence
    alone cannot tell a correct implementation from three unconditional
    `rm -rf`s. What separates them is the legacy candidates: `clear` removes
    `verdict-openai.json` and `verdict-codex.json` as well, and a bare rm of
    the three canonical names leaves them for the codex promotion to pick up
    as this run's verdict. This also pins the step's success: the body must
    exit 0 on the path where nothing fails.
    """
    real = _PROMOTE_SCRIPT.read_text(encoding="utf-8")
    planted = _planted_verdicts() + _legacy_verdict_files()
    result = _run_aggregate_clear(tmp_path, promote=real, planted=planted)
    assert result.returncode == 0, (
        f"the aggregate clear exited {result.returncode} on the normal path "
        f"(stderr={result.stderr.strip()!r})"
    )
    survivors = [name for name in planted if (tmp_path / name).exists()]
    assert not survivors, (
        f"the aggregate clear left {survivors} behind on the normal path; the "
        "legacy candidates are what the script removes and a bare rm does not"
    )


@pytest.mark.parametrize(
    "promote", [None, _FAILING_PROMOTE], ids=["script-missing", "script-fails"]
)
def test_the_aggregate_fallbacks_sweep_the_legacy_names_too(
    tmp_path: Path, promote: str | None
) -> None:
    """Both fallbacks remove what `clear` removes, not only the canonical name.

    The script-missing branch is a real path in self-review, where this YAML
    comes from the PR and the scripts from the release tag. Each fallback
    that removes less than `clear` makes what the aggregate's CWD holds
    afterwards depend on which path ran.
    """
    planted = _planted_verdicts() + _legacy_verdict_files()
    result = _run_aggregate_clear(tmp_path, promote=promote, planted=planted)
    survivors = [name for name in planted if (tmp_path / name).exists()]
    assert not survivors, (
        f"the aggregate clear's fallback left {survivors} behind "
        f"(rc={result.returncode}, stderr={result.stderr.strip()!r})"
    )


def test_the_aggregate_fallback_list_is_the_scripts_list() -> None:
    """The YAML's `legacy=(...)` is a copy; the script's array is the source.

    The fallbacks exist for when the script is not there to be read, so the
    YAML cannot source it and has to carry the names itself. A name added
    to the script only would then be swept on the script path and left on
    both fallback paths -- and every runtime test here would still pass,
    because they plant the same list they check.
    """
    steps = _aggregate_steps()
    step = steps[_index(steps, _AGGREGATE_CLEAR)]
    assert _bash_array(_body(step), "legacy") == _legacy_verdict_files(), (
        "the aggregate clear's legacy=(...) differs from "
        "promote_legacy_verdict.sh's LEGACY_VERDICT_FILES"
    )


def test_the_harness_runs_bodies_the_way_the_runner_does(tmp_path: Path) -> None:
    """A failing command ahead of the removal must abort, as `bash -e` does.

    Without `-e` the harness steps over it and the removal still runs, so a
    net that dies on the runner before it removes anything passes every
    executing test here.
    """
    result = subprocess.run(
        [*_RUNNER_BASH, "false; : > reached"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode != 0 and not (tmp_path / "reached").exists(), (
        "the harness ran past a failing command, which the runner's bash -e "
        "would not"
    )


def test_the_loop_roster_ignores_a_commented_loop() -> None:
    """`_loop_roster` reads commands, like every other predicate.

    A comment naming the full roster ahead of a loop that has drifted must
    not stand in for the loop.
    """
    step = {
        "run": (
            "# for reviewer in claude gemini codex; do\n"
            "for reviewer in claude gemini; do\n"
            '  rm -rf -- "review-${reviewer}.json"\n'
            "done\n"
        )
    }
    assert _loop_roster(step) == {"claude", "gemini"}


# --- Symlinked workspace paths host steps read or write (AT-2941) ---
# The PR head is checked out at the workspace root and the codex CLI runs
# with --sandbox workspace-write, so a workspace path a host step later
# reads, writes, or uploads can be a symlink the PR committed or the model
# planted, pointing at the scripts snapshot or any runner-writable file.
# These guard the two host steps that would otherwise follow such a link:
# the context download (write-through) and the codex log upload (reads the
# link's target and ships it as an artifact).


def test_context_paths_are_cleared_before_the_download() -> None:
    steps = _single_steps()
    copy = _index(steps, "Copy ai-dev-pr-review scripts outside the workspace")
    clear = _index(steps, "Clear context paths the PR may have symlinked")
    download = _index(steps, "Download review context")
    # The clear sits between the snapshot copy and the download: it must
    # run before the download (so no link is followed) and the names it
    # removes are exactly the ones the download repopulates.
    assert copy < clear < download, (copy, clear, download)
    _assert_not_advisory(steps[clear], "context clear")
    body = _body(steps[clear])
    for name in ("pr.diff", "context.md", ".review-context"):
        assert name in body, f"{name} is not cleared before the context download"


def test_symlinked_codex_artifacts_are_dropped_before_upload() -> None:
    steps = _single_steps()
    drop = _index(steps, "Drop symlinked codex artifacts before upload")
    upload = _index(steps, "Upload Codex logs")
    assert drop < upload, (drop, upload)
    # always(), so a run step killed by its timeout does not leave a link
    # standing for the upload to follow.
    assert _is_always(steps[drop]), "the drop step must run on always()"
    _assert_not_advisory(steps[drop], "codex artifact drop")
    body = _body(steps[drop])
    for name in ("review-codex.json", "verdict-codex.json", "verdict-openai.json"):
        assert name in body, f"{name} is not dropped before the codex upload"
