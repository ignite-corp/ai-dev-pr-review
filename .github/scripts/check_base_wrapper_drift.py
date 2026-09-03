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

Rejected alternatives:

* Line-level or normalised-text diff of the two files. Rejected for the
  structural reason above: the wrapper is a different shape by design, so
  every diff hunk is a false positive and the real signal drowns.
* Per-step (rather than union) env-key containment. Rejected: the wrapper
  splits one base step into three reviewer-specific ones and may legitimately
  carry a key in only the step that needs it; requiring every wrapper step to
  carry every base key would flag that split as drift.
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
  correspondence with an empty `wrapper_steps` and a non-empty `reason`.
* Per key / per variable: an env key, or a `vars.*` name, that deliberately
  never reaches wrapper is listed in the exceptions file with a non-empty
  `reason`.

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
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from re import compile as re_compile
from typing import Any

import yaml

VARS_RE = re_compile(r"vars\.([A-Z_][A-Z0-9_]*)")


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
class DriftConfig:
    """The correspondence file: which files to read and how their steps map."""

    base_files: tuple[str, ...]
    wrapper_files: tuple[str, ...]
    steps: tuple[StepCorrespondence, ...]


@dataclass(frozen=True)
class Exceptions:
    """The exceptions file, with every reason already validated non-empty.

    ``vars`` maps a ``vars.*`` name to its reason. ``env`` maps
    ``(base_file, base_step)`` to ``{env key: reason}``.
    """

    vars: dict[str, str]
    env: dict[tuple[str, str], dict[str, str]]

    @classmethod
    def empty(cls) -> Exceptions:
        return cls(vars={}, env={})


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
    )


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
    return Exceptions(vars=vars_exceptions, env=env_exceptions)


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
    base_docs = {name: _load_yaml(base_dir / name) for name in config.base_files}
    for entry in config.steps:
        if entry.base_file not in base_docs:
            base_docs[entry.base_file] = _load_yaml(base_dir / entry.base_file)

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

    findings = env_findings + vars_findings
    if env_notes:
        print("acknowledged env exceptions:")
        print("\n".join(env_notes))
    if vars_notes:
        print("acknowledged vars exceptions:")
        print("\n".join(vars_notes))
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
