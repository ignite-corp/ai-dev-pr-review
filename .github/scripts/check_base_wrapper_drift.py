#!/usr/bin/env python3
"""Detect base<->wrapper GitHub Actions workflow drift (AT-2122).

`ignite-pilot-org/ai-dev-pr-review-wrapper` does not delegate to this repo's
reusable review workflows; it reimplements them inline in one `wrapper.yml`
job. Every change to base's `base-ai-review-*.yml` files must therefore be
hand-ported, and until this check nothing caught a missed port. AT-2120 is
the eighth confirmed incident of this shape: `ROUND_CUTOFF_N` /
`ROUND_CUTOFF_ENABLED` landed in base at `606c2f4` (v1.1.2) and were absent
from every one of wrapper's 37 subsequent commits, surviving six MINOR
releases and seven manual drift investigations before anyone noticed by
accident.

The two files cannot be compared verbatim: base spreads the review pipeline
across five jobs, wrapper collapses it into one job's worth of steps, with
different indentation, different conditional syntax (`needs:`/`result` vs
`steps.*.outputs`), and deliberately different job/step granularity. Any
line-level diff is pure false positives. This module compares *extracted*
invariants instead -- values that must agree regardless of how the
surrounding YAML is shaped:

1. Env-key set per corresponding step. Base's `Post inline comments` step
   (in `base-ai-review-single.yml`, called once per reviewer via a reusable
   workflow) triplicates into wrapper's `Post Claude/Codex/Gemini inline
   comments` steps. Every env key base's step sets must be set by at least
   one of wrapper's three -- the union, not each step individually, since
   wrapper is free to split responsibilities across the three as long as
   nothing base sets goes unset everywhere. `ROUND_CUTOFF_N` missing from
   all three wrapper steps is exactly the shape this axis catches.

   The correspondence must be complete: every base step that carries an
   `env:` block must appear in it, either mapped to wrapper step(s) or
   declared as having no counterpart with a reason. An unmapped step is a
   failure, not a silent gap -- otherwise a new base step is invisible to
   the check in the same way a new key used to be.

2. `vars.*`-consumed set across the whole review pipeline. Every
   `vars.WHATEVER` token referenced anywhere in base's four
   `base-ai-review-*.yml` files, diffed against every one referenced in
   wrapper's `wrapper.yml`. A name base reads that wrapper never reads is a
   variable a consumer can set that silently does nothing -- runtime-silent,
   not a rendering difference like the README pair AT-2104 covers, so no one
   sees it on a page. The direction that matters is base-has/wrapper-lacks:
   a wrapper-only `vars.*` is not a swallowed base feature and is left as an
   informational note, not a failure.

3. `on.workflow_call.outputs` key set per corresponding workflow file. A
   reusable workflow's outputs are its machine-readable contract with a
   caller -- a merge gate reads them -- and they are neither an env key nor
   a `vars.*` token, so axes 1 and 2 are blind to one. AT-2511 added
   `reviewer_roster` / `reviewers_expected_count` /
   `reviewers_responded_count` to base and the wrapper could have gone on
   lacking them indefinitely with this check green: the same silent gap
   AT-2120 was, one contract surface over.

   Like axis 1 this is a correspondence, not a flat set diff, because
   base's four workflows do not map one-to-one onto wrapper's single file:
   `base-ai-review-prepare.yml` and `base-ai-review-single.yml` declare
   outputs only to hand values to the next *job*, and the single-job
   wrapper has no inter-job hop to declare them on. Those are structural
   exceptions stated once, rather than one per output name. The union of
   the mapped wrapper files' outputs must cover the base file's, the same
   direction and for the same reason as axis 1: a wrapper-only output is
   not a swallowed base feature.

Rejected alternatives:

* Line-level or normalised-text diff of the two files. Rejected for the
  structural reason above: the wrapper is a different shape by design, so
  every diff hunk is a false positive and the real signal drowns.
* Per-step (rather than union) env-key containment. Rejected: the wrapper
  splits one base step into three reviewer-specific ones and may legitimately
  carry a key in only the step that needs it; requiring every wrapper step to
  carry every base key would flag that split as drift.
* A flat base-vs-wrapper union diff of output names, mirroring axis 2.
  Rejected: it would need one exception entry per output name that never
  reaches wrapper -- ten today, nine of them repeating the one fact that
  prepare's and single's outputs are inter-job plumbing the single-job
  wrapper has no place for. The correspondence states that fact once.
* Exact env-key set equality. Rejected: wrapper-only keys (for example
  `CODEX_AUTH_OUTCOME` in its verdict-synthesis step) are scaffolding that
  the single-job shape needs and base does not; only the base-has/wrapper-
  lacks direction is a swallowed feature.

Explicitly OUT OF SCOPE: step/job `if:` condition equivalence. Base and
wrapper legitimately gate the same work differently because one has jobs and
the other has steps -- AT-2092 reverted `!cancelled()` back to `always()` in
one specific base *job* for reasons that do not transfer to a wrapper *step*.
Telling a legitimate job-vs-step gating difference apart from real drift on
this axis is not solved here; it is recorded as a deliberate exclusion, not
an oversight. Also not covered: `with:` inputs to composite actions, which
are a separate channel from `env:` and are left for a later axis.

Escape hatches, both requiring a stated reason:

* Structural: a base step with no wrapper counterpart is listed in the
  correspondence with an empty `wrapper_steps` and a non-empty `reason`;
  a base *file* whose whole `workflow_call` output surface has none is
  listed the same way under `outputs:`, with an empty `wrapper_files`.
* Per key / per variable: an env key, a `vars.*` name, or a
  `workflow_call` output name that deliberately never reaches wrapper is
  listed in the exceptions file with a non-empty `reason`.

An entry with a blank reason is malformed and fails the check on its own,
the same way an unexplained gap does -- a documented exception that excuses
nothing is worse than no exception, because it reads as coverage that is not
there.

Usage:
    check_base_wrapper_drift.py \
        --base-dir DIR --wrapper-dir DIR \
        --correspondence FILE --exceptions FILE

DIR arguments point at the two repos' `.github/workflows` directories.
Exit 0 = no undeclared drift. Exit 1 = drift found or the config itself is
malformed (unresolvable step name, exception with no reason). Findings are
printed to stdout; every string in them is already present in one of the two
public repos' workflow files.

The first two lines of stdout name the revision each input directory was read
at, so an output saved and attached as evidence says for itself which trees it
answered about. That is AT-2557: wrapper PR #44 attached this check's own stdout
as evidence of "exit 0, no drift", and every part of it was genuine -- the real
tool, run the real way, writing its real output -- but it had been produced
against base v1.10.0, three weeks before either axis the PR existed to satisfy
reached base. The same pair at v1.11.2 exits 1 with seven findings. Nothing in
the file said which base it had read, so the only way to catch it was to re-run
four candidate versions and compare bytes.

Every branch of the stamp produces text, including the awkward ones. A stamp
that goes quiet when there is no clean answer rebuilds exactly the hole it
closes: the incident was an output that looked like a good one, and an output
missing its first line looks like a good one too. The same rule governs each
probe the stamp makes -- one that could not run says so, and never borrows the
negative answer it did not establish. `describe_input_revision` lists what each
branch returns; `_tag_phrase` and `_dirt_phrase` carry the measurements behind
their own probes.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from dataclasses import field as dataclass_field
from pathlib import Path
from re import compile as re_compile
from stat import S_ISDIR
from typing import Any, NamedTuple

import yaml

VARS_RE = re_compile(r"vars\.([A-Z_][A-Z0-9_]*)")

# One line of git's own stderr is quoted when a probe exits non-zero, so the
# cap is a log-hygiene bound rather than a semantic one: enough for git's
# fatal messages, short of letting an unexpected flood into Actions output.
GIT_COMPLAINT_QUOTE_LIMIT = 200

# Appended when the cap above actually cuts, so a clipped complaint cannot
# read as git's whole sentence.
GIT_COMPLAINT_ELLIPSIS = "..."

# The stamp runs four of these per input directory, once per run: rev-parse,
# ls-files, tag --points-at, status. The timeout is per call, not per stamp,
# so the worst case is 8 calls for the two directories.
#
# Chosen against the job cap, not independently: `base-wrapper-drift.yml`
# sets `timeout-minutes: 5` (300s) for a job that also pays for two checkouts
# and a pip install. At 20s this stamp's worst case was 160s, over half the
# budget, so a contended runner could push the job into the Actions timeout --
# which kills it and produces no stdout at all, the line-less output this
# stamp exists to prevent. At 5s the worst case is 40s, about an eighth.
#
# Lowered rather than raising the cap, because every probe here degrades
# into an honest sentence when it times out ("tag lookup failed", and so on)
# while the job timeout degrades into nothing. Bounding the stamp converts
# the bad outcome into the designed one. 5s is still some three orders of
# magnitude above what these calls cost on this input -- thirteen files in
# one directory -- so it bounds a pathological stall, not a slow runner.
GIT_TIMEOUT_SECONDS = 5


class StepNotFoundError(LookupError):
    """A correspondence entry names a step that does not exist in the file."""


class MalformedConfigError(ValueError):
    """A correspondence or exceptions entry is missing a required reason."""


class UnreadableYamlError(ValueError):
    """A YAML file could not be parsed, or its top level is not a mapping.

    Raised for workflow and config files alike; run() maps it to a clean
    diagnostic and exit 1. A broken file must never read as an empty one --
    an empty workflow has no steps and no vars, which looks like a pass.
    """


@dataclass(frozen=True)
class StepCorrespondence:
    base_file: str
    base_step: str
    wrapper_file: str
    wrapper_steps: tuple[str, ...]
    # Non-empty exactly when ``wrapper_steps`` is empty: the structural escape
    # hatch for a base step that legitimately has no wrapper counterpart.
    reason: str = ""

    @property
    def key(self) -> tuple[str, str]:
        return (self.base_file, self.base_step)

    @property
    def label(self) -> str:
        return f"{self.base_file}:{self.base_step!r}"

    @property
    def has_no_counterpart(self) -> bool:
        return not self.wrapper_steps


@dataclass(frozen=True)
class OutputCorrespondence:
    """Which wrapper file(s) carry a base file's ``workflow_call`` outputs.

    The axis-3 analogue of ``StepCorrespondence``: ``wrapper_files`` empty
    with a ``reason`` is the structural escape hatch for a base file whose
    whole output surface is inter-job plumbing the wrapper has no place for.
    """

    base_file: str
    wrapper_files: tuple[str, ...]
    reason: str = ""

    @property
    def label(self) -> str:
        return self.base_file

    @property
    def has_no_counterpart(self) -> bool:
        return not self.wrapper_files


@dataclass(frozen=True)
class DriftConfig:
    """The correspondence file: which files to read and how their steps map."""

    base_files: tuple[str, ...]
    wrapper_files: tuple[str, ...]
    steps: tuple[StepCorrespondence, ...]
    outputs: tuple[OutputCorrespondence, ...] = ()


@dataclass(frozen=True)
class Exceptions:
    """The exceptions file, with every reason already validated non-empty.

    ``vars`` maps a ``vars.*`` name to its reason. ``env`` maps
    ``(base_file, base_step)`` to ``{env key: reason}``. ``outputs`` maps
    ``base_file`` to ``{workflow_call output name: reason}``.
    """

    vars: dict[str, str]
    env: dict[tuple[str, str], dict[str, str]]
    outputs: dict[str, dict[str, str]] = dataclass_field(default_factory=dict)

    @classmethod
    def empty(cls) -> Exceptions:
        return cls(vars={}, env={}, outputs={})


class _GitOutcome(NamedTuple):
    """What a git call did, keeping "ran and said no" apart from "never ran".

    The three probes that ask git a yes/no question only need to know whether
    they got an answer, and use ``_git`` below. ``rev-parse`` needs the other
    distinction: a timeout or a missing git binary says nothing at all about
    the directory, while a non-zero exit at least means git looked. Measured
    before that split existed: with ``rev-parse`` timing out, a checkout at a
    tag stamped ``@ not a git checkout (or git unavailable, or no commit
    yet)``.

    ``stderr`` is carried for the same reason, one level finer. A non-zero
    exit does *not* establish what is wrong -- see the ``rev-parse`` branch
    in ``describe_input_revision`` -- so the caller quotes git instead of
    classifying the exit code. Anything this type records, the caller can
    report; nothing it records licenses a diagnosis.
    """

    ran: bool
    """The process completed, whatever it exited with. False = timeout, or
    git could not be executed at all."""
    returncode: int
    """-1 when it never ran."""
    stdout: str
    """Stripped stdout; empty unless ``returncode`` is 0."""
    stderr: str
    """Stripped stderr, whatever the exit status. Empty when it never ran."""

    @property
    def answered(self) -> bool:
        """It ran and exited 0, so ``stdout`` is git's answer."""
        return self.ran and self.returncode == 0

    @property
    def complaint(self) -> str:
        """git's own first line of explanation, for quoting rather than
        paraphrasing. Empty when it said nothing.

        A cut carries ``GIT_COMPLAINT_ELLIPSIS``. Clipping silently would
        print a sentence ending mid-word as though that were all git said --
        the long-path form of ``detected dubious ownership in repository at
        '...'`` is exactly the case that runs past the limit -- and an output
        that reads more complete than it is is this module's whole subject.
        """
        if not self.stderr.strip():
            return ""
        lines = self.stderr.strip().splitlines()
        first = lines[0].strip()
        if len(first) > GIT_COMPLAINT_QUOTE_LIMIT:
            first = first[:GIT_COMPLAINT_QUOTE_LIMIT] + GIT_COMPLAINT_ELLIPSIS
        # Mark the line cut as well as the character cut. Measured on the
        # dubious-ownership case this docstring cites: git writes four lines,
        # of which the three dropped ones carry the remediation
        # (`git config --global --add safe.directory <path>`). Quoting only
        # the first and saying nothing presents a fragment as the whole
        # complaint -- the same elision-without-a-marker the character cut
        # above was fixed for, which is why it is marked the same way.
        if len(lines) > 1:
            first += f" {GIT_COMPLAINT_ELLIPSIS} (+{len(lines) - 1} more line(s))"
        return first


def _git_run(path: Path, *args: str) -> _GitOutcome:
    """Run a read-only git command inside ``path`` and report how it went.

    ``errors="replace"`` because the decode happens inside ``subprocess.run``
    under ``text=True``, and git does not promise UTF-8: a path or a ref with
    undecodable bytes in it makes a strict decode raise ``UnicodeDecodeError``
    -- a ``ValueError``, so it escaped the handler below, escaped
    ``describe_input_revision``, and reached the stamp's ``print`` in ``run``,
    which sits before ``run``'s own ``try``. Measured: the stamp produced a
    traceback and no line at all, against a docstring promising that every
    branch produces text. ``ValueError`` is caught as well, so a residual
    decode or argument error still lands on the honest ``ran=False`` wording
    rather than taking the line away.
    """
    try:
        result = subprocess.run(
            ("git", "-C", str(path), *args),
            capture_output=True,
            text=True,
            errors="replace",
            timeout=GIT_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, ValueError, subprocess.SubprocessError):
        return _GitOutcome(ran=False, returncode=-1, stdout="", stderr="")
    return _GitOutcome(
        ran=True,
        returncode=result.returncode,
        stdout=result.stdout.strip() if result.returncode == 0 else "",
        stderr=result.stderr.strip(),
    )


def _git(path: Path, *args: str) -> str | None:
    """Run a read-only git command inside ``path``; ``None`` when it cannot.

    ``None`` covers every way the answer is unavailable -- git not
    installed, ``path`` not in a checkout, a command that exits non-zero or
    times out -- because these callers treat them alike: they have no answer
    to report and must say so, rather than report an empty one. Where the
    difference matters, call ``_git_run`` and read the outcome.
    """
    outcome = _git_run(path, *args)
    return outcome.stdout if outcome.answered else None


def _tag_phrase(path: Path, head: str) -> str:
    """How ``head`` is tagged, for the parenthesis in the stamp.

    Only an exact tag is reported, never ``git describe``'s nearest-tag
    form: ``v1.11.2-3-gabc1234`` reads as v1.11.2 at a glance, and a
    near-miss revision passing for the right one is AT-2557's defect.

    A failed lookup is kept apart from a confirmed absence. ``_git`` answers
    ``None`` for git missing, a non-zero exit and a timeout alike, so testing
    the result for falsity would report "no tag at this commit" about a
    commit nothing had looked at -- and here the tag may well exist.
    """
    tags = _git(path, "tag", "--points-at", head)
    if tags is None:
        return "tag lookup failed, so this commit's tags are unknown"
    if tags:
        # Plural when there is more than one. A release commit routinely
        # carries both its version tag and a moving one, and `tag v1.11.2,
        # latest` invites reading the second name as something other than a
        # tag -- a label narrower than its data, which is the same shape as
        # an output narrower than its evidence.
        names = tags.split()
        return f"{'tag' if len(names) == 1 else 'tags'} {', '.join(names)}"
    return "no tag at this commit"


def _dirt_phrase(path: Path) -> str:
    """What under ``path`` the named commit does not account for.

    Appended to the stamp, empty when the tree is clean. The SHA alone
    would be a half-truth whenever it is not: the files the check read are
    then not the files that commit holds.

    `-uall` rather than the default `--untracked-files=normal`, which
    collapses an untracked directory into a single `?? dir/` entry: fifty
    unpacked artefact files then report as `1 uncommitted path(s)` and
    understate how far the tree read has departed from the named commit.

    `--ignored` because without it the count is blind in exactly the case
    the `ls-files` probe does not cover. That probe catches a directory with
    *nothing* tracked in it; a directory holding tracked workflow files
    *and* an ignored artefact passes it, and plain `status` says nothing
    about the ignored half -- so the stamp would print a SHA and a tag for a
    tree it had only partly looked at.

    An earlier round rejected `--ignored` as noisy on the strength of 7
    routine `__pycache__` / `.pytest_cache` paths, but that was measured
    against `.github/scripts`. The inputs this check is given are
    `.github/workflows` directories, and re-measured there `--ignored -uall`
    reports 0 paths against 61 for `.github/scripts` -- nothing writes build
    artefacts next to workflow YAML. The positive control for that zero:
    planting one `__pycache__` under `.github/workflows` takes it to 1, so
    the zero is a clean directory and not a silent failure. The noise
    objection was about a directory this code never reads.

    What the count covers, stated so the residue is not left to inference:
    modified, staged, untracked and ignored paths under `path`, one line per
    file. What it does not cover is anything outside `path`, which is
    deliberate -- only what the check reads can change its answer -- and a
    tracked file overwritten with content identical to the commit, which git
    cannot distinguish from an untouched one and which does not change what
    the check read either.
    """
    status = _git(path, "status", "--porcelain", "--ignored", "-uall", "--", ".")
    if status is None:
        return " + uncommitted-change check failed, so the SHA is unconfirmed"
    if status:
        count = len(status.splitlines())
        return f" + {count} uncommitted path(s) under it, so the SHA is not the whole input"
    return ""


def _printable(text: str) -> str:
    """``text`` rendered so ``print`` cannot raise on this stdout.

    The stamp quotes git's stderr, which is decoded with ``errors="replace"``
    and so can carry U+FFFD; a path can carry any non-ASCII besides. If
    stdout cannot encode that -- an ASCII-only locale, which CI images do
    have -- ``print`` raises ``UnicodeEncodeError``, and the stamp's print
    sits before ``run``'s own ``try``, so the line would vanish exactly as
    it did for the decode and the stat. Measured: U+FFFD through an
    ``ascii``/``strict`` writer raises; through ``backslashreplace`` it does
    not. Conditional on the live encoding rather than unconditional, so a
    UTF-8 runner still prints a non-ASCII path as itself.
    """
    encoding = sys.stdout.encoding or "utf-8"
    return text.encode(encoding, "backslashreplace").decode(encoding, "replace")


def describe_input_revision(path: Path) -> str:
    """One line naming the revision ``path``'s contents were read at (AT-2557).

    The incident this exists for, and the rule every branch below follows,
    are in the module docstring. What each branch returns:

    * Could not be inspected -> says that, naming the OS error. A stat can
      fail without answering: with an unreadable parent directory
      ``Path.exists()`` raises rather than returning False, because EACCES
      is not in ``pathlib``'s ignored-errno set, and the stamp then
      produced a traceback and no line at all.
    * No such directory -> says that. ``git -C <missing> rev-parse HEAD``
      exits non-zero exactly as a real directory without a checkout does,
      and this is how a mistyped ``--base-dir`` presents, so it is the most
      reachable way to be told something confident about a directory nobody
      found.
    * Exists but is not a directory -> says that, separately. A third
      fact: ``--base-dir`` aimed at a single workflow file is neither a
      missing path nor an input tree, and calling it missing is false
      about the filesystem.
    * A git that ran and named no revision -> git's own first line of
      stderr, quoted. Not "not a git checkout": a non-zero ``rev-parse``
      does not establish that, since git exits 128 for ``detected dubious
      ownership`` (the safe.directory check, which fires whenever a
      checkout is read by a different UID than created it) and for a
      ``.git`` it cannot parse, both of which are checkouts. Quoting lets
      the reader tell those apart and keeps the stamp from diagnosing
      what it only observed. An unpacked tarball still reads plainly,
      because git's own words for it are ``fatal: not a git repository``.
    * A resolvable HEAD but nothing tracked under ``path`` -> says that,
      and names no SHA. ``git rev-parse`` walks up out of the directory it
      is given, so an artefact unpacked into a checkout answers with that
      checkout's HEAD; printing it would credit a commit for files it
      never contained, which is this ticket's own defect one layer down.
    * A resolvable HEAD -> the full SHA, plus ``tag <name>`` for every tag
      pointing at exactly that commit, or ``no tag at this commit``. Why
      only an exact tag, and why a failed lookup reads differently from a
      confirmed absence: ``_tag_phrase``.
    * Uncommitted, untracked or ignored files under ``path`` -> the count,
      appended, one per file rather than one per untracked directory. Which
      paths that covers, which it does not, and the measurements behind
      ``-uall`` and ``--ignored``: ``_dirt_phrase``.
    * A probe that could not run -> said as a failure, never as its negative
      answer. A failed ``ls-files`` is not "nothing is tracked here", a
      failed ``tag --points-at`` is not "no tag at this commit", and a
      ``rev-parse`` that timed out is not "not a git checkout"; each would
      be a positive claim about the tree resting on no evidence, which is
      what this stamp exists to stop an output from doing. All four probes
      are held to this, ``rev-parse`` included -- it is the one that decides
      whether the other three run at all, so it is the worst place to guess.
    """
    # Absolute, because the relative path the workflow passes
    # (``.github/workflows``) says nothing once the output is read away from
    # the directory it ran in -- which is the whole situation this addresses.
    #
    # Guarded, because resolution is itself a filesystem operation that can
    # fail: measured, a two-link symlink loop makes `resolve()` raise
    # `RuntimeError: Symlink loop from '...'` (not an `OSError`, so both are
    # caught), and since this runs before the stat below and the stamp prints
    # before `run`'s own `try`, the result was a traceback and no revision
    # line -- the same invariant the stat guard protects, one call earlier.
    # The unresolved path is reported, because it is the only name for this
    # input that exists when resolution is what failed.
    try:
        path = path.resolve()
    except (OSError, RuntimeError, ValueError) as exc:
        reason = getattr(exc, "strerror", None) or str(exc) or type(exc).__name__
        return f"{path} @ could not be inspected: {reason}"

    # One guarded stat, not `exists()` then `is_dir()`: those two booleans
    # cannot say "I could not look", and they answer four distinct facts with
    # two. The docstring's first four bullets carry the cases and the
    # measurements behind them; this is their one home.
    try:
        info = path.stat()
    except (FileNotFoundError, NotADirectoryError):
        return f"{path} @ no such directory, so there is nothing here to describe"
    except OSError as exc:
        reason = exc.strerror or type(exc).__name__
        return f"{path} @ could not be inspected: {reason}"
    if not S_ISDIR(info.st_mode):
        return f"{path} @ exists but is not a directory, so it is not an input tree"

    # The fourth probe, held to the same rule as the three below: a git that
    # never ran has said nothing about this directory, and no claim about it
    # may be built on that silence.
    resolved = _git_run(path, "rev-parse", "HEAD")
    if not resolved.ran:
        return f"{path} @ HEAD lookup failed, so this directory's revision is unknown"

    # No stdout means git declined to name a revision -- a non-zero exit
    # (stdout is blanked for those) or the pathological exit-0-with-nothing.
    # What it does NOT mean is "there is no repository here". git exits 128
    # for `detected dubious ownership in repository`, the safe.directory
    # check, which fires whenever a checkout is read by a different UID than
    # created it -- routine for an artefact unpacked by another job or a step
    # in a container -- and for a `.git` it cannot parse. Measured: under
    # either, a checkout at tag v1.11.2 stamped `not a git checkout (or no
    # commit yet)`, a confident claim about a directory that was one, in the
    # single branch that suppresses the SHA, the tag and the dirt count
    # together. So this quotes git instead of classifying its exit code: the
    # reader gets `fatal: not a git repository ...` or `fatal: detected
    # dubious ownership ...` and can tell those apart, and the stamp asserts
    # nothing it did not establish.
    if not resolved.stdout:
        if resolved.complaint:
            return f"{path} @ no revision resolved here; git said: {resolved.complaint}"
        return f"{path} @ no revision resolved here, and git gave no reason (exit {resolved.returncode})"
    head = resolved.stdout

    # `git status` is silent about ignored paths, so a directory with nothing
    # tracked in it -- an artefact unpacked into `build/`, `tmp/` or anything
    # else a .gitignore covers -- reports clean while `rev-parse` has already
    # walked up and answered with the surrounding repository's HEAD. Asking
    # what git actually tracks here is what closes that. The dirt probe's
    # `--ignored` narrows this case but does not replace this one: it appends
    # a count while the line still reads `@ <sha> (tag v1.11.2)`, leaving on
    # display the near-miss revision this check exists to refuse. The noise
    # measurement that flag once rested on is recounted in one place only,
    # at the dirt probe below; it is not re-argued here.
    #
    # A failed probe is not an empty one. `_git` returns None for git missing,
    # a non-zero exit and a timeout alike, and reading that as "nothing is
    # tracked here" would print a positive claim about the tree that was never
    # established -- and suppress a SHA `rev-parse` had already resolved. That
    # is this ticket's own defect, an output reading better than its evidence,
    # so each probe below keeps its failure apart from its negative answer the
    # way the dirt check already does.
    tracked = _git(path, "ls-files", "--", ".")
    if tracked is None:
        return f"{path} @ {head} (tracked-content check failed, so the SHA is unconfirmed)"
    if not tracked:
        return f"{path} @ no tracked content here, so the nearest checkout's HEAD does not describe it"

    return f"{path} @ {head} ({_tag_phrase(path, head)}){_dirt_phrase(path)}"


def _load_yaml(path: Path) -> dict[str, Any]:
    """Parse ``path`` as a YAML mapping; an empty document is an empty mapping.

    Raises UnreadableYamlError on a syntax error or a non-mapping top level
    (a list, a scalar) instead of returning ``{}``: the wrapper file comes
    from another repository's main, so a transient broken state is possible,
    and it must fail the check rather than pass it as "nothing to compare".
    """
    try:
        doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise UnreadableYamlError(f"{path}: not valid YAML ({exc})") from exc
    if doc is None:
        return {}
    if not isinstance(doc, dict):
        raise UnreadableYamlError(f"{path}: top level is {type(doc).__name__}, not a mapping")
    return doc


def _iter_steps(doc: dict[str, Any]) -> Iterator[dict[str, Any]]:
    """Yield every step mapping across every job in a workflow document."""
    jobs = doc.get("jobs")
    if not isinstance(jobs, dict):
        return
    for job in jobs.values():
        if not isinstance(job, dict):
            continue
        for step in job.get("steps") or []:
            if isinstance(step, dict):
                yield step


def _env_keys(step: dict[str, Any]) -> set[str]:
    env = step.get("env")
    return set(env.keys()) if isinstance(env, dict) else set()


def find_step_env_keys(doc: dict[str, Any], step_name: str) -> set[str]:
    """Env-block key names of the step named ``step_name``.

    GitHub Actions requires step names to be unique within one job, not
    across jobs in a file; every file this check reads defines each of its
    correspondence step names exactly once, so first-match is exact, not a
    shortcut. A name that matches nothing is a broken config, not an empty
    result -- callers must not read "no keys" as "step reads nothing."
    """
    for step in _iter_steps(doc):
        if step.get("name") == step_name:
            return _env_keys(step)
    raise StepNotFoundError(step_name)


def steps_with_env(doc: dict[str, Any]) -> list[str | None]:
    """Names of every step that carries a non-empty ``env:`` block.

    ``None`` stands for a step that has an env block but no ``name``: it
    cannot be mapped by name, so the completeness check reports it.
    """
    return [step.get("name") for step in _iter_steps(doc) if _env_keys(step)]


def workflow_call_outputs(doc: dict[Any, Any]) -> set[str]:
    """Key names declared under ``on.workflow_call.outputs``.

    Annotated ``dict[Any, Any]`` rather than ``dict[str, Any]`` because the
    key this reads is genuinely not a string; see below.

    ``on:`` is a YAML 1.1 boolean, so ``yaml.safe_load`` gives the trigger
    block the key ``True``, not the string ``"on"``. Both spellings are
    read: a file quoted as ``"on":`` parses to the string, and reading only
    one of them would silently report every workflow as declaring nothing,
    which on this axis looks exactly like a pass.

    A workflow that is not callable, or that declares no outputs, has an
    empty set -- there is no contract to port.
    """
    trigger: Any = None
    for key in (True, "on"):
        candidate = doc.get(key)
        if isinstance(candidate, dict):
            trigger = candidate
            break
    if trigger is None:
        return set()
    call = trigger.get("workflow_call")
    if not isinstance(call, dict):
        return set()
    outputs = call.get("outputs")
    return set(outputs.keys()) if isinstance(outputs, dict) else set()


def extract_vars_consumed(node: Any) -> set[str]:
    """Every distinct ``vars.NAME`` token inside the string values of a parsed
    workflow document.

    The walk is over the YAML tree, not the file text: a ``vars.NAME`` in a
    comment is documentation, not consumption, and counting it would let a
    prose mention in base turn the check red for a variable no job reads.
    """
    if isinstance(node, str):
        return set(VARS_RE.findall(node))
    if isinstance(node, dict):
        found: set[str] = set()
        for value in node.values():
            found |= extract_vars_consumed(value)
        return found
    if isinstance(node, list):
        found = set()
        for item in node:
            found |= extract_vars_consumed(item)
        return found
    return set()


def _reason(entry: Any) -> str:
    """The stripped ``reason`` of a mapping entry, or "" when absent."""
    if not isinstance(entry, dict):
        return ""
    return str(entry.get("reason") or "").strip()


def load_correspondence(path: Path) -> DriftConfig:
    """Parse the correspondence file.

    Raises MalformedConfigError when a step with no wrapper counterpart
    carries no reason, or when a mapped step carries one (a reason only
    means something for a step that has nothing to be compared against).
    """
    doc = _load_yaml(path)
    steps: list[StepCorrespondence] = []
    for index, raw in enumerate(doc.get("steps") or []):
        if not isinstance(raw, dict):
            raise MalformedConfigError(f"correspondence entry {index} is not a mapping")
        for field in ("base_file", "base_step", "wrapper_file"):
            if not str(raw.get(field) or "").strip():
                where = f"entry {index}"
                if field != "base_step" and str(raw.get("base_step") or "").strip():
                    where += f" ({raw['base_step']!r})"
                raise MalformedConfigError(f"correspondence {where} is missing {field!r}")
        entry = StepCorrespondence(
            base_file=str(raw["base_file"]),
            base_step=str(raw["base_step"]),
            wrapper_file=str(raw["wrapper_file"]),
            wrapper_steps=tuple(str(name) for name in raw.get("wrapper_steps") or []),
            reason=_reason(raw),
        )
        if entry.has_no_counterpart and not entry.reason:
            raise MalformedConfigError(
                f"correspondence {entry.label} has no wrapper_steps and no reason"
            )
        if not entry.has_no_counterpart and entry.reason:
            raise MalformedConfigError(
                f"correspondence {entry.label} maps wrapper steps and also carries a "
                "reason; a reason is only for a step with no counterpart"
            )
        steps.append(entry)
    return DriftConfig(
        base_files=tuple(str(name) for name in doc.get("base_files") or []),
        wrapper_files=tuple(str(name) for name in doc.get("wrapper_files") or []),
        steps=tuple(steps),
        outputs=_load_output_correspondence(doc),
    )


def _load_output_correspondence(doc: dict[str, Any]) -> tuple[OutputCorrespondence, ...]:
    """Parse the ``outputs:`` section, with axis 1's reason rules.

    A file with no wrapper counterpart needs a reason; one that maps
    wrapper files must not carry a reason, because there is nothing for it
    to excuse.
    """
    entries: list[OutputCorrespondence] = []
    for index, raw in enumerate(doc.get("outputs") or []):
        if not isinstance(raw, dict):
            raise MalformedConfigError(f"outputs correspondence entry {index} is not a mapping")
        if not str(raw.get("base_file") or "").strip():
            raise MalformedConfigError(
                f"outputs correspondence entry {index} is missing 'base_file'"
            )
        entry = OutputCorrespondence(
            base_file=str(raw["base_file"]),
            wrapper_files=tuple(str(name) for name in raw.get("wrapper_files") or []),
            reason=_reason(raw),
        )
        if entry.has_no_counterpart and not entry.reason:
            raise MalformedConfigError(
                f"outputs correspondence {entry.label} has no wrapper_files and no reason"
            )
        if not entry.has_no_counterpart and entry.reason:
            raise MalformedConfigError(
                f"outputs correspondence {entry.label} maps wrapper files and also carries "
                "a reason; a reason is only for a file with no counterpart"
            )
        entries.append(entry)
    return tuple(entries)


def load_exceptions(path: Path) -> Exceptions:
    """Parse the exceptions file; a missing file means no exceptions.

    Raises MalformedConfigError for any entry whose reason is missing or
    blank -- an exception that excuses nothing must not silently pass as one
    that does.
    """
    if not path.exists():
        return Exceptions.empty()
    doc = _load_yaml(path)
    vars_exceptions: dict[str, str] = {}
    for name, entry in (doc.get("vars") or {}).items():
        reason = _reason(entry)
        if not reason:
            raise MalformedConfigError(f"vars.{name} has no reason")
        vars_exceptions[str(name)] = reason
    env_exceptions: dict[tuple[str, str], dict[str, str]] = {}
    for base_file, by_step in (doc.get("env") or {}).items():
        for base_step, by_key in (by_step or {}).items():
            reasons: dict[str, str] = {}
            for key, entry in (by_key or {}).items():
                reason = _reason(entry)
                if not reason:
                    raise MalformedConfigError(f"env {base_file}:{base_step!r} {key} has no reason")
                reasons[str(key)] = reason
            env_exceptions[(str(base_file), str(base_step))] = reasons
    output_exceptions: dict[str, dict[str, str]] = {}
    for base_file, by_key in (doc.get("outputs") or {}).items():
        reasons = {}
        for key, entry in (by_key or {}).items():
            reason = _reason(entry)
            if not reason:
                raise MalformedConfigError(f"outputs {base_file} {key} has no reason")
            reasons[str(key)] = reason
        output_exceptions[str(base_file)] = reasons
    return Exceptions(vars=vars_exceptions, env=env_exceptions, outputs=output_exceptions)


def _base_docs(
    base_dir: Path,
    config: DriftConfig,
    extra: Iterable[str],
) -> dict[str, dict[str, Any]]:
    """Parse every base workflow an axis compares, keyed by file name.

    Seeded from ``config.base_files`` -- the completeness sweep each axis
    runs needs all of them -- and extended with any file a correspondence
    entry names that the config does not list, so a stale or narrower
    ``base_files`` cannot make an entry unreadable. Shared because both
    axes need exactly this set and had written it out separately.
    """
    docs = {name: _load_yaml(base_dir / name) for name in config.base_files}
    for name in extra:
        if name not in docs:
            docs[name] = _load_yaml(base_dir / name)
    return docs


def check_env_keys(
    base_dir: Path,
    wrapper_dir: Path,
    config: DriftConfig,
    exceptions: Exceptions,
) -> tuple[list[str], list[str]]:
    """Returns (findings, acknowledged-exception notes).

    Raises StepNotFoundError when the correspondence names a step that no
    longer exists on either side: a stale config is a failure, not a pass.
    """
    findings: list[str] = []
    notes: list[str] = []
    base_docs = _base_docs(base_dir, config, (e.base_file for e in config.steps))

    mapped = {entry.key for entry in config.steps}
    for base_file in config.base_files:
        for name in steps_with_env(base_docs[base_file]):
            if name is None:
                findings.append(
                    f"unmapped base step: {base_file} has an unnamed step with an env "
                    "block; give it a name and add it to the correspondence"
                )
            elif (base_file, name) not in mapped:
                findings.append(
                    f"unmapped base step: {base_file}:{name!r} sets env keys but has no "
                    "correspondence entry (map it to wrapper step(s), or declare it as "
                    "having no counterpart with a reason)"
                )

    wrapper_docs: dict[str, dict[str, Any]] = {}
    for entry in config.steps:
        base_keys = find_step_env_keys(base_docs[entry.base_file], entry.base_step)
        if entry.has_no_counterpart:
            notes.append(f"  step {entry.label} has no wrapper counterpart: {entry.reason}")
            continue
        if entry.wrapper_file not in wrapper_docs:
            wrapper_docs[entry.wrapper_file] = _load_yaml(wrapper_dir / entry.wrapper_file)
        wrapper_doc = wrapper_docs[entry.wrapper_file]
        wrapper_union: set[str] = set()
        for wrapper_step in entry.wrapper_steps:
            wrapper_union |= find_step_env_keys(wrapper_doc, wrapper_step)
        allowed = exceptions.env.get(entry.key, {})
        for key in sorted(base_keys - wrapper_union):
            if key in allowed:
                notes.append(f"  env {entry.label} key {key}: {allowed[key]}")
            else:
                findings.append(
                    f"env drift: base step {entry.label} sets {key!r}, which none of "
                    f"wrapper's {list(entry.wrapper_steps)} ({entry.wrapper_file}) set"
                )
    return findings, notes


def check_workflow_outputs(
    base_dir: Path,
    wrapper_dir: Path,
    config: DriftConfig,
    exceptions: Exceptions,
) -> tuple[list[str], list[str]]:
    """Returns (findings, acknowledged-exception notes) for axis 3.

    Completeness is enforced the way axis 1 enforces it: a base file that
    declares ``workflow_call`` outputs and has no correspondence entry is a
    finding, so a newly callable base workflow cannot be invisible here in
    the same way a new output name used to be.
    """
    findings: list[str] = []
    notes: list[str] = []
    base_docs = _base_docs(base_dir, config, (e.base_file for e in config.outputs))

    mapped = {entry.base_file for entry in config.outputs}
    for base_file in config.base_files:
        if workflow_call_outputs(base_docs[base_file]) and base_file not in mapped:
            findings.append(
                f"unmapped base outputs: {base_file} declares on.workflow_call.outputs "
                "but has no entry under the correspondence's 'outputs' (map it to wrapper "
                "file(s), or declare it as having no counterpart with a reason)"
            )

    wrapper_docs: dict[str, dict[str, Any]] = {}
    for entry in config.outputs:
        base_keys = workflow_call_outputs(base_docs[entry.base_file])
        if entry.has_no_counterpart:
            notes.append(f"  outputs {entry.label} has no wrapper counterpart: {entry.reason}")
            continue
        wrapper_union: set[str] = set()
        for name in entry.wrapper_files:
            if name not in wrapper_docs:
                wrapper_docs[name] = _load_yaml(wrapper_dir / name)
            wrapper_union |= workflow_call_outputs(wrapper_docs[name])
        allowed = exceptions.outputs.get(entry.base_file, {})
        for key in sorted(base_keys - wrapper_union):
            if key in allowed:
                notes.append(f"  outputs {entry.label} key {key}: {allowed[key]}")
            else:
                findings.append(
                    f"outputs drift: base {entry.label} declares workflow_call output "
                    f"{key!r}, which none of wrapper's {list(entry.wrapper_files)} declare "
                    "(a caller's gate reads nothing on the wrapper path)"
                )
    return findings, notes


def check_vars_consumed(
    base_dir: Path,
    wrapper_dir: Path,
    config: DriftConfig,
    exceptions: Exceptions,
) -> tuple[list[str], list[str], list[str]]:
    """Returns (findings, acknowledged-exception notes, wrapper-only info notes)."""
    base_vars: set[str] = set()
    for name in config.base_files:
        base_vars |= extract_vars_consumed(_load_yaml(base_dir / name))
    wrapper_vars: set[str] = set()
    for name in config.wrapper_files:
        wrapper_vars |= extract_vars_consumed(_load_yaml(wrapper_dir / name))

    findings: list[str] = []
    notes: list[str] = []
    for name in sorted(base_vars - wrapper_vars):
        if name in exceptions.vars:
            notes.append(f"  vars.{name}: {exceptions.vars[name]}")
        else:
            findings.append(
                f"vars drift: base consumes vars.{name}, wrapper never reads it "
                "(a consumer setting it has no effect and no error)"
            )

    info = [f"  vars.{name} (wrapper-only, not a drift)" for name in sorted(wrapper_vars - base_vars)]
    return findings, notes, info


def run(base_dir: Path, wrapper_dir: Path, correspondence_path: Path, exceptions_path: Path) -> int:
    # Before anything that can abort: this output gets saved and attached as
    # evidence, and a failing run's output is attached as often as a passing
    # one. See describe_input_revision for why it is stamped at all.
    print(_printable(f"base input revision: {describe_input_revision(base_dir)}"))
    print(_printable(f"wrapper input revision: {describe_input_revision(wrapper_dir)}"))

    try:
        config = load_correspondence(correspondence_path)
        exceptions = load_exceptions(exceptions_path)
    except (MalformedConfigError, UnreadableYamlError) as exc:
        print(f"malformed drift-check config: {exc}")
        return 1

    try:
        env_findings, env_notes = check_env_keys(base_dir, wrapper_dir, config, exceptions)
        vars_findings, vars_notes, vars_info = check_vars_consumed(
            base_dir, wrapper_dir, config, exceptions
        )
        output_findings, output_notes = check_workflow_outputs(
            base_dir, wrapper_dir, config, exceptions
        )
    except StepNotFoundError as exc:
        print(
            f"correspondence config names a step that does not exist: {exc}\n"
            f"({correspondence_path} is stale against the current workflow files)"
        )
        return 1
    except FileNotFoundError as exc:
        # A renamed or removed workflow file is the same defect as a renamed
        # step: the config no longer describes the trees. It must fail the
        # same way -- a missing file compared against nothing is not a pass.
        print(
            f"correspondence config names a file that does not exist: {exc.filename}\n"
            f"({correspondence_path} is stale against the current workflow files)"
        )
        return 1
    except UnreadableYamlError as exc:
        # Same failure class as a missing file: a workflow that cannot be read
        # as a mapping has no steps and no vars to compare, which must not
        # look like "nothing drifted".
        print(
            f"correspondence config names a file that cannot be read as a workflow: {exc}\n"
            f"({correspondence_path} is stale against the current workflow files, "
            "or the file is broken)"
        )
        return 1

    findings = env_findings + vars_findings + output_findings
    if env_notes:
        print("acknowledged env exceptions:")
        print("\n".join(env_notes))
    if vars_notes:
        print("acknowledged vars exceptions:")
        print("\n".join(vars_notes))
    if output_notes:
        print("acknowledged workflow_call output exceptions:")
        print("\n".join(output_notes))
    if vars_info:
        print("informational (wrapper-only vars.*, not checked):")
        print("\n".join(vars_info))
    if findings:
        print("DRIFT FOUND:")
        for line in findings:
            print(f"  {line}")
        return 1
    print("no undeclared base<->wrapper drift found")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--base-dir", required=True, type=Path)
    parser.add_argument("--wrapper-dir", required=True, type=Path)
    parser.add_argument("--correspondence", required=True, type=Path)
    parser.add_argument("--exceptions", required=True, type=Path)
    args = parser.parse_args(argv)
    return run(args.base_dir, args.wrapper_dir, args.correspondence, args.exceptions)


if __name__ == "__main__":
    sys.exit(main())
