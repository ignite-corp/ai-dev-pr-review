#!/usr/bin/env python3
"""Measure a PR for the size gate the way its reviewers will see it (AT-3592).

The size gate compared GitHub's whole-PR additions+deletions, so files that
`.github/lens-ignore` keeps out of the reviewers' diff (AT-2206) still
counted: a vendored-tree bump the reviewers would read as 199 lines was
skipped as 10,763, and the only way through was raising a limit every
target of the runner shares. This module subtracts, per file, the lines of
every file the rule file excludes.

A file is left out only when the rules at the PR's BASE commit AND at its
head both exclude it. Base alone would miss a PR that removes or narrows a
rule: the diff filter reads the head's rules, so the reviewers would get
the file the gate never counted. Head alone would let one PR add a rule and
the code it hides, and get past the size gate and the reviewers together.
With both, a rule reduces the count only once it has itself been merged
through review, and only while the PR keeps it. The rule file is never
excluded, as in the diff filter.

Every doubt resolves to the full count, which is the gate's behaviour before
this module: more changed files than the files API lists, a list shorter
than the PR says, per-file counts that do not add up to the PR's totals, or
a file list or rule file that could not be read (a 404 for the rule file is
not a doubt: it means the base has no rules).

Stdlib only: prepare runs it on the runner's system python3, before any
setup-python step.

Env: GITHUB_REPOSITORY, PR_NUMBER. Prints the measure as one JSON object.
"""

from __future__ import annotations

import base64
import binascii
import json
import os
import subprocess
import sys
from dataclasses import dataclass
from typing import Any, Callable, Optional

from filter_pr_diff import DEFAULT_RULE_PATH, matches, parse_rules
from github_pr_support import display_path

# GET /pulls/{n}/files returns at most this many files, however it is paged.
FILES_API_CAP = 3000
_PER_PAGE = 100
_NOT_FOUND = "HTTP 404"

# (argv after `gh`) -> (exit code, stdout, stderr)
GhApi = Callable[[list[str]], tuple[int, str, str]]


@dataclass(frozen=True)
class ChangedFile:
    path: str
    previous_path: Optional[str]
    additions: int
    deletions: int

    @property
    def paths(self) -> list[str]:
        return [p for p in (self.previous_path, self.path) if p]

    @property
    def label(self) -> str:
        """Rendered like the diff filter's labels: `old -> new` for a rename."""
        if self.previous_path and self.previous_path != self.path:
            return f"{display_path(self.previous_path)} -> {display_path(self.path)}"
        return display_path(self.path)


@dataclass(frozen=True)
class SizeMeasure:
    """What the size gate compares, and what it left out and why."""

    additions: int
    deletions: int
    github_additions: int
    github_deletions: int
    excluded_paths: tuple[str, ...]
    # Why lens-ignore was NOT applied although it may have been meant to;
    # "" when it was applied, or when the base has no rule file.
    fallback: str

    @property
    def excluded_lines(self) -> int:
        return (
            self.github_additions
            + self.github_deletions
            - self.additions
            - self.deletions
        )

    def as_json(self) -> dict[str, Any]:
        return {
            "additions": self.additions,
            "deletions": self.deletions,
            "github_additions": self.github_additions,
            "github_deletions": self.github_deletions,
            "excluded_lines": self.excluded_lines,
            "excluded_files": len(self.excluded_paths),
            "excluded_paths": list(self.excluded_paths),
            "fallback": self.fallback,
        }


def full_count(additions: int, deletions: int, fallback: str = "") -> SizeMeasure:
    return SizeMeasure(additions, deletions, additions, deletions, (), fallback)


def measure(
    *,
    additions: int,
    deletions: int,
    changed_files: int,
    files: Optional[list[ChangedFile]],
    base_rules: Optional[str],
    head_rules: Optional[str],
    fallback: str = "",
) -> SizeMeasure:
    """The PR's size with files both commits exclude left out, or the full count."""
    if base_rules is None or head_rules is None or fallback:
        return full_count(additions, deletions, fallback)
    rules = parse_rules(base_rules)
    rules_at_head = parse_rules(head_rules)
    if not rules or not rules_at_head:
        return full_count(additions, deletions)
    if changed_files > FILES_API_CAP:
        return full_count(
            additions,
            deletions,
            f"{changed_files} changed files is more than the {FILES_API_CAP}"
            " the files API lists",
        )
    if files is None:
        return full_count(additions, deletions, "the changed-file list was not read")
    if len(files) != changed_files:
        return full_count(
            additions,
            deletions,
            f"the files API listed {len(files)} of {changed_files} changed files",
        )
    if (
        sum(f.additions for f in files) != additions
        or sum(f.deletions for f in files) != deletions
    ):
        return full_count(
            additions, deletions, "per-file line counts do not add up to the PR's"
        )
    kept_additions, kept_deletions = additions, deletions
    excluded: list[str] = []
    for changed in files:
        if DEFAULT_RULE_PATH in changed.paths:
            continue
        if any(matches(rules, path) for path in changed.paths) and any(
            matches(rules_at_head, path) for path in changed.paths
        ):
            kept_additions -= changed.additions
            kept_deletions -= changed.deletions
            excluded.append(changed.label)
    return SizeMeasure(
        kept_additions, kept_deletions, additions, deletions, tuple(excluded), ""
    )


# ---------------------------------------------------------------------------
# Reading from GitHub
# ---------------------------------------------------------------------------


def gh_api(args: list[str]) -> tuple[int, str, str]:
    result = subprocess.run(["gh", *args], capture_output=True, text=True, check=False)
    return result.returncode, result.stdout, result.stderr


def _read_rules(
    gh: GhApi, repo: str, sha: str, which: str
) -> tuple[Optional[str], str]:
    """(rule text or None when `sha` has none, fallback reason)."""
    code, out, err = gh(
        [
            "api",
            "-X",
            "GET",
            f"repos/{repo}/contents/{DEFAULT_RULE_PATH}",
            "-f",
            f"ref={sha}",
        ]
    )
    if code != 0:
        if _NOT_FOUND in err:
            return None, ""
        return None, f"{DEFAULT_RULE_PATH} at the {which} could not be read"
    try:
        payload = json.loads(out)
        text = base64.b64decode(payload["content"]).decode("utf-8", errors="replace")
    except (ValueError, KeyError, TypeError, binascii.Error):
        return None, f"{DEFAULT_RULE_PATH} at the {which} could not be decoded"
    return text, ""


def _read_files(gh: GhApi, repo: str, pr_number: str) -> Optional[list[ChangedFile]]:
    code, out, _ = gh(
        [
            "api",
            "--paginate",
            f"repos/{repo}/pulls/{pr_number}/files?per_page={_PER_PAGE}",
            "--jq",
            ".[] | {filename, previous_filename, additions, deletions}",
        ]
    )
    if code != 0:
        return None
    files: list[ChangedFile] = []
    try:
        for line in out.splitlines():
            if not line.strip():
                continue
            entry = json.loads(line)
            files.append(
                ChangedFile(
                    path=str(entry["filename"]),
                    previous_path=entry.get("previous_filename") or None,
                    additions=int(entry["additions"]),
                    deletions=int(entry["deletions"]),
                )
            )
    except (ValueError, KeyError, TypeError):
        return None
    return files


def measure_pr(repo: str, pr_number: str, gh: GhApi = gh_api) -> SizeMeasure:
    """Measure PR `pr_number`; a failure to read the PR itself raises."""
    code, out, err = gh(["api", f"repos/{repo}/pulls/{pr_number}"])
    if code != 0:
        raise RuntimeError(f"cannot read {repo}#{pr_number}: {err.strip()}")
    pr = json.loads(out)
    additions = int(pr["additions"])
    deletions = int(pr["deletions"])
    changed_files = int(pr["changed_files"])
    rules, fallback = _read_rules(gh, repo, str(pr["base"]["sha"]), "base")
    head_rules: Optional[str] = None
    if rules is not None and not fallback:
        head_rules, fallback = _read_rules(gh, repo, str(pr["head"]["sha"]), "head")
    files: Optional[list[ChangedFile]] = None
    if (
        rules is not None
        and head_rules is not None
        and not fallback
        and changed_files <= FILES_API_CAP
    ):
        files = _read_files(gh, repo, pr_number)
    return measure(
        additions=additions,
        deletions=deletions,
        changed_files=changed_files,
        files=files,
        base_rules=rules,
        head_rules=head_rules,
        fallback=fallback,
    )


def main() -> None:
    repo = os.environ["GITHUB_REPOSITORY"]
    pr_number = os.environ["PR_NUMBER"]
    try:
        measured = measure_pr(repo, pr_number, gh_api)
    except (RuntimeError, ValueError, KeyError, TypeError) as exc:
        print(f"::error::size measure failed: {exc}", file=sys.stderr)
        sys.exit(1)
    if measured.fallback:
        print(
            f"::warning title=lens-ignore::size counted in full -- {measured.fallback}",
            file=sys.stderr,
        )
    print(json.dumps(measured.as_json()))


if __name__ == "__main__":
    main()
