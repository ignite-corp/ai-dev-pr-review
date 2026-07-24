#!/usr/bin/env python3
"""Detect required status checks that can never run on a pull request.

Reads a directory of GitHub Actions workflow YAML files (``sys.argv[1]``) and a
newline-separated list of required status-check contexts on stdin. Prints the
number of contexts that are a *footgun*: not reachable on any ``pull_request``
event yet positively present as a job/workflow name in a push-only workflow.
Such a check is a permanent merge gate -- it is required but never reports on a
PR, so every PR stays blocked.

Detection is conservative to avoid false positives that would break the audit:
a context is only counted when it is BOTH (a) not reachable on a PR AND (b)
found in a push-only workflow. Contexts that match no local workflow (external
apps, checks reported by other means) are never flagged.

The output is a single integer; the caller reports it by index only, so no
context strings are printed here either -- consumer config stays private.
"""

from __future__ import annotations

import glob
import os
import sys

import yaml

# PR events under which a workflow's checks report on a pull request. A
# ``workflow_call`` reusable is treated as PR-reachable too: it is almost always
# invoked by a PR-triggered caller, and assuming otherwise risks false footguns.
_PR_EVENTS = {"pull_request", "pull_request_target"}
_CALLABLE = _PR_EVENTS | {"workflow_call"}


def _event_set(on: object) -> set[str]:
    """Normalise a workflow ``on:`` trigger into a set of event names."""
    if isinstance(on, str):
        return {on}
    if isinstance(on, list):
        return {e for e in on if isinstance(e, str)}
    if isinstance(on, dict):
        return {str(k) for k in on}
    return set()


def _names(doc: dict) -> set[str]:
    """Collect the status-check context names a workflow can produce.

    A job reports under its ``name:`` if set, else its job id. The workflow
    ``name:`` is included to match single-segment or composed contexts.
    """
    out: set[str] = set()
    wf_name = doc.get("name")
    if isinstance(wf_name, str):
        out.add(wf_name.strip())
    jobs = doc.get("jobs")
    if isinstance(jobs, dict):
        for job_id, body in jobs.items():
            out.add(str(job_id).strip())
            if isinstance(body, dict):
                job_name = body.get("name")
                if isinstance(job_name, str):
                    out.add(job_name.strip())
    return out


def _classify(wf_dir: str) -> tuple[set[str], set[str]]:
    """Return (pr_reachable_names, push_only_names) across all workflow files."""
    pr_names: set[str] = set()
    push_names: set[str] = set()
    for path in sorted(glob.glob(os.path.join(wf_dir, "*.yml")) + glob.glob(os.path.join(wf_dir, "*.yaml"))):
        try:
            with open(path, encoding="utf-8") as fh:
                doc = yaml.safe_load(fh)
        except (OSError, yaml.YAMLError):
            continue
        if not isinstance(doc, dict):
            continue
        # PyYAML parses the bare key ``on`` as the boolean True (YAML 1.1).
        events = _event_set(doc.get(True, doc.get("on")))
        names = _names(doc)
        if events & _CALLABLE:
            pr_names |= names
        else:
            push_names |= names
    return pr_names, push_names


def count_unreachable(wf_dir: str, contexts: list[str]) -> int:
    pr_names, push_names = _classify(wf_dir)
    unreachable = 0
    for ctx in contexts:
        terminal = ctx.split(" / ")[-1].strip()
        if ctx in pr_names or terminal in pr_names:
            continue
        if ctx in push_names or terminal in push_names:
            unreachable += 1
    return unreachable


def main() -> int:
    if len(sys.argv) != 2:
        print("usage: pr_reachable_checks.py <workflow-dir>", file=sys.stderr)
        return 2
    contexts = [line.strip() for line in sys.stdin if line.strip()]
    print(count_unreachable(sys.argv[1], contexts))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
