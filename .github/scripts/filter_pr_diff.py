#!/usr/bin/env python3
"""Remove policy-excluded files from pr.diff before any reviewer reads it.

A consumer repository may carry `.github/lens-ignore`: gitignore-syntax
globs, relative to the repository root, naming files whose content must not
reach the reviewers (AT-2206). Every diff entry whose path matches is dropped
from pr.diff -- the reviewers never see those hunks -- and the exclusion is
announced rather than hidden: the paths (never the content) are appended to
context.md for the reviewers and handed to the aggregate through
$GITHUB_OUTPUT, so the verdict comment lists them too.

Without the rule file nothing is touched: pr.diff and context.md keep their
bytes, and the outputs say so (`policy_skipped=false`, `excluded_count=0`).

Rule syntax (a stdlib matcher; `pathspec` is deliberately not a dependency):
  - `#` starts a comment; blank lines are ignored;
  - `*` matches within one path component (never a `/`), `?` matches one
    character, `[...]` is a character class (`[!...]` / `[^...]` negates);
  - `**` matches across directories in the three gitignore positions --
    leading `**/`, trailing `/**`, and `/**/` in the middle. Elsewhere it
    behaves as `*`, as it does in git;
  - a pattern with no `/` (a trailing one aside) matches at any depth; a
    pattern with a `/` anywhere else is anchored to the repository root, and
    a leading `/` anchors explicitly;
  - a trailing `/` matches directories only, i.e. every path under one;
  - a path matched as a directory excludes everything beneath it;
  - `!` negates; the last matching rule wins. Each path is matched on its
    own, so unlike git a file can be re-included under an excluded directory;
  - trailing whitespace is trimmed unless escaped with a backslash, and a
    backslash also escapes a leading `#` or `!`;
  - a line that cannot be compiled (an invalid character class, a pattern
    that is empty once stripped) is skipped with a `::warning::` naming its
    line number, never fatal.

Two things are never negotiable inside the matcher. A rename or copy entry is
excluded when EITHER side matches, so a sensitive file cannot be surfaced by
moving it. And the rule file itself is never excludable: it is read from the
PR head so that a sensitive file added by the same PR can be covered, and the
price of that is that the PR can also edit the rules -- so every change to
them stays in the reviewed diff, whatever the rules say.

Env: PR_DIFF (default `pr.diff`, rewritten in place), LENS_IGNORE_PATH
     (default `.github/lens-ignore`), CONTEXT_MD (default `context.md`),
     POLICY_RESULT (default `.review-context/lens-ignore.json`),
     GITHUB_OUTPUT (appended to when set).
"""

from __future__ import annotations

import json
import os
import re
import sys
import uuid
from dataclasses import dataclass
from pathlib import Path

DEFAULT_DIFF_PATH = "pr.diff"
DEFAULT_RULE_PATH = ".github/lens-ignore"
DEFAULT_CONTEXT_PATH = "context.md"
DEFAULT_RESULT_PATH = ".review-context/lens-ignore.json"

_DIFF_HEADER = "diff --git "
_DEV_NULL = "/dev/null"
_OUTPUT_PATHS_KEY = "excluded_paths"

CONTEXT_SECTION_HEADING = "## Policy-excluded files"


@dataclass(frozen=True)
class Rule:
    pattern: str
    negated: bool
    regex: re.Pattern[str]


class RuleError(ValueError):
    """A rule line that cannot become a matcher."""


def _strip_trailing_whitespace(line: str) -> str:
    """Trim trailing spaces and tabs unless the last one is backslash-escaped."""
    while line and line[-1] in " \t":
        body = line[:-1]
        backslashes = len(body) - len(body.rstrip("\\"))
        if backslashes % 2 == 1:
            break
        line = body
    return line


def _char_class(pattern: str, start: int) -> tuple[str, int] | None:
    """Translate the `[...]` class opening at `start`; None if it never closes."""
    i = start + 1
    negate = i < len(pattern) and pattern[i] in "!^"
    if negate:
        i += 1
    # A `]` right after the opening (or the negation) is a literal member.
    members_start = i
    while i < len(pattern) and (pattern[i] != "]" or i == members_start):
        if pattern[i] == "\\" and i + 1 < len(pattern):
            i += 1
        i += 1
    if i >= len(pattern):
        return None
    members = pattern[members_start:i]
    # Guard every regex-significant member except the range dash and the
    # backslash escapes the author wrote.
    escaped = re.sub(r"(?<!\\)([\[\]^])", r"\\\1", members)
    return "[" + ("^" if negate else "") + escaped + "]", i + 1


def _glob_to_regex(pattern: str, *, anchored: bool, dir_only: bool) -> str:
    out: list[str] = []
    i = 0
    n = len(pattern)
    while i < n:
        c = pattern[i]
        if c == "*":
            if pattern.startswith("**/", i) and (i == 0 or pattern[i - 1] == "/"):
                out.append("(?:.*/)?")
                i += 3
                continue
            if pattern.startswith("**", i) and i + 2 == n and i > 0 and pattern[i - 1] == "/":
                out.append(".*")
                i += 2
                continue
            # Any other run of asterisks is a single `*`, as in git.
            while i < n and pattern[i] == "*":
                i += 1
            out.append("[^/]*")
            continue
        if c == "?":
            out.append("[^/]")
        elif c == "[":
            translated = _char_class(pattern, i)
            if translated is None:
                out.append(re.escape(c))
            else:
                cls, i = translated
                out.append(cls)
                continue
        elif c == "\\" and i + 1 < n:
            i += 1
            out.append(re.escape(pattern[i]))
        else:
            out.append(re.escape(c))
        i += 1
    body = "".join(out)
    prefix = "^" if anchored else "^(?:.*/)?"
    # A directory-only rule matches only what lies beneath the directory; any
    # other rule matches the path itself or, as a directory, everything under it.
    suffix = "/.+$" if dir_only else "(?:/.*)?$"
    return prefix + body + suffix


def parse_rule(line: str) -> Rule | None:
    """One rule from one line; None for a blank or comment line.

    Raises RuleError for a line that cannot be compiled.
    """
    line = _strip_trailing_whitespace(line.rstrip("\r\n"))
    if not line or line.startswith("#"):
        return None
    negated = line.startswith("!")
    if negated:
        line = line[1:]
    if line.startswith(("\\#", "\\!")):
        line = line[1:]
    dir_only = line.endswith("/") and not line.endswith("\\/")
    if dir_only:
        line = line[:-1]
    anchored = "/" in line
    line = line.lstrip("/")
    if not line:
        raise RuleError("pattern is empty once stripped")
    try:
        regex = re.compile(_glob_to_regex(line, anchored=anchored, dir_only=dir_only))
    except re.error as exc:
        raise RuleError(f"invalid pattern: {exc}") from exc
    return Rule(pattern=line, negated=negated, regex=regex)


def parse_rules(text: str) -> list[Rule]:
    """Every usable rule in a rule file, in order; unusable lines warn and skip."""
    rules: list[Rule] = []
    for number, line in enumerate(text.splitlines(), start=1):
        try:
            rule = parse_rule(line)
        except RuleError as exc:
            print(
                f"::warning title=lens-ignore::line {number} skipped -- {exc}: {line!r}",
                file=sys.stderr,
            )
            continue
        if rule is not None:
            rules.append(rule)
    return rules


def matches(rules: list[Rule], path: str) -> bool:
    """Whether `path` (repository-relative, `/`-separated) is excluded."""
    excluded = False
    for rule in rules:
        if rule.regex.match(path):
            excluded = not rule.negated
    return excluded


# ---------------------------------------------------------------------------
# Diff entries
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DiffEntry:
    """One file's slice of a unified diff: its `diff --git` header to the next."""

    text: str
    old_path: str | None
    new_path: str | None

    @property
    def paths(self) -> list[str]:
        return [p for p in (self.old_path, self.new_path) if p]

    @property
    def label(self) -> str:
        if self.old_path and self.new_path and self.old_path != self.new_path:
            return f"{self.old_path} -> {self.new_path}"
        return self.new_path or self.old_path or "?"


def _clean(path: str) -> str:
    """Path text safe to print and post: raw non-UTF-8 bytes become U+FFFD."""
    return path.encode("utf-8", "surrogateescape").decode("utf-8", "replace")


def _unquote(path: str) -> str:
    """Undo git's C-style quoting of a path with special characters.

    Quoted paths are ASCII with octal escapes for every other byte, so the
    escapes are decoded to bytes first and only then read as UTF-8.
    """
    if len(path) >= 2 and path[0] == '"' and path[-1] == '"':
        try:
            raw = path[1:-1].encode("latin-1", "surrogateescape")
            return raw.decode("unicode_escape").encode("latin-1").decode("utf-8", "replace")
        except (UnicodeDecodeError, UnicodeEncodeError):
            return _clean(path[1:-1])
    return _clean(path)


def _strip_prefix(path: str, prefix: str) -> str | None:
    path = _unquote(path)
    if path == _DEV_NULL:
        return None
    return path[len(prefix):] if path.startswith(prefix) else path


def _paths_from_header(header: str) -> tuple[str | None, str | None]:
    """Old and new path from a `diff --git a/OLD b/NEW` line.

    The line is unparseable in general when a path contains ` b/`, so the
    `---`/`+++` and `rename`/`copy` lines are preferred when an entry has
    them; this is the fallback for entries that do not (mode-only, binary).
    """
    rest = header[len(_DIFF_HEADER):].rstrip("\r\n")
    if rest.startswith('"'):
        parts = re.findall(r'"(?:[^"\\]|\\.)*"|\S+', rest)
        if len(parts) == 2:
            return _strip_prefix(parts[0], "a/"), _strip_prefix(parts[1], "b/")
    # Unchanged name: the two halves are equal, which pins the split point
    # even when the path itself contains ` b/`.
    if rest.startswith("a/"):
        for i in range(2, len(rest)):
            if rest.startswith(" b/", i) and rest[2:i] == rest[i + 3:]:
                return _clean(rest[2:i]), _clean(rest[2:i])
    split = rest.find(" b/")
    if split == -1:
        return None, None
    return _strip_prefix(rest[:split], "a/"), _strip_prefix(rest[split + 1:], "b/")


def _entry_paths(lines: list[str]) -> tuple[str | None, str | None]:
    old_path, new_path = _paths_from_header(lines[0])
    for line in lines[1:]:
        if line.startswith("@@"):
            break
        body = line.rstrip("\r\n")
        if body.startswith("--- "):
            old_path = _strip_prefix(body[4:], "a/")
        elif body.startswith("+++ "):
            new_path = _strip_prefix(body[4:], "b/")
        elif body.startswith(("rename from ", "copy from ")):
            old_path = _unquote(body.split(" from ", 1)[1])
        elif body.startswith(("rename to ", "copy to ")):
            new_path = _unquote(body.split(" to ", 1)[1])
    return old_path, new_path


def split_diff(text: str) -> tuple[str, list[DiffEntry]]:
    """(preamble, entries). Joining preamble + every entry's text is `text`."""
    lines = text.splitlines(keepends=True)
    starts = [i for i, line in enumerate(lines) if line.startswith(_DIFF_HEADER)]
    if not starts:
        return text, []
    preamble = "".join(lines[: starts[0]])
    entries: list[DiffEntry] = []
    for index, start in enumerate(starts):
        end = starts[index + 1] if index + 1 < len(starts) else len(lines)
        chunk = lines[start:end]
        old_path, new_path = _entry_paths(chunk)
        entries.append(DiffEntry("".join(chunk), old_path, new_path))
    return preamble, entries


def filter_diff(
    text: str, rules: list[Rule], *, protected: str = DEFAULT_RULE_PATH
) -> tuple[str, list[str]]:
    """(kept diff, excluded labels). Kept entries are byte-identical.

    `protected` is the rule file's own path: an entry for it is kept whatever
    the rules say, with a warning, so a policy change is always reviewed.
    """
    preamble, entries = split_diff(text)
    kept: list[str] = [preamble]
    excluded: list[str] = []
    for entry in entries:
        if any(matches(rules, path) for path in entry.paths):
            if protected in entry.paths:
                print(
                    f"::warning title=lens-ignore::{protected} matches its own"
                    " rules; it is never excluded, so the policy change stays"
                    " in the reviewed diff",
                    file=sys.stderr,
                )
            else:
                excluded.append(entry.label)
                continue
        kept.append(entry.text)
    return "".join(kept), excluded


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------


def context_section(paths: list[str], rule_path: str) -> str:
    lines = [
        "",
        "---",
        "",
        CONTEXT_SECTION_HEADING,
        "",
        f"{len(paths)} file(s) excluded by policy ({rule_path}). Their hunks"
        " were removed from pr.diff before review. Do not open, quote, or"
        " infer the content of these paths; treat this diff as partial:",
        "",
        *(f"- `{path}`" for path in paths),
        "",
    ]
    return "\n".join(lines)


def _write_github_output(policy_skipped: bool, paths: list[str]) -> None:
    output_path = os.environ.get("GITHUB_OUTPUT", "")
    if not output_path:
        return
    delimiter = f"lens_ignore_{uuid.uuid4().hex}"
    with open(output_path, "a", encoding="utf-8", errors="replace") as fh:
        fh.write(f"policy_skipped={'true' if policy_skipped else 'false'}\n")
        fh.write(f"excluded_count={len(paths)}\n")
        fh.write(f"{_OUTPUT_PATHS_KEY}<<{delimiter}\n")
        for path in paths:
            fh.write(f"{path}\n")
        fh.write(f"{delimiter}\n")


def _write_result(
    result_path: str,
    *,
    rule_path: str | None,
    policy_skipped: bool,
    paths: list[str],
    kept_count: int,
) -> None:
    Path(result_path).parent.mkdir(parents=True, exist_ok=True)
    Path(result_path).write_text(
        json.dumps(
            {
                "rule_file": rule_path,
                "policy_skipped": policy_skipped,
                "excluded_count": len(paths),
                "excluded_paths": paths,
                "kept_count": kept_count,
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )


def main() -> None:
    diff_path = os.environ.get("PR_DIFF", DEFAULT_DIFF_PATH)
    rule_path = os.environ.get("LENS_IGNORE_PATH", DEFAULT_RULE_PATH)
    context_path = os.environ.get("CONTEXT_MD", DEFAULT_CONTEXT_PATH)
    result_path = os.environ.get("POLICY_RESULT", DEFAULT_RESULT_PATH)

    if not Path(rule_path).is_file():
        print(f"{rule_path} not present; pr.diff left as extracted")
        _write_github_output(False, [])
        _write_result(
            result_path, rule_path=None, policy_skipped=False, paths=[], kept_count=-1
        )
        return

    try:
        # surrogateescape round-trips bytes that are not UTF-8, so a kept
        # entry is written back exactly as it was read.
        diff_text = Path(diff_path).read_text(encoding="utf-8", errors="surrogateescape")
    except OSError as exc:
        print(f"::error::cannot read {diff_path}: {exc}", file=sys.stderr)
        sys.exit(1)

    rules = parse_rules(Path(rule_path).read_text(encoding="utf-8", errors="replace"))
    kept_text, excluded = filter_diff(diff_text, rules, protected=rule_path)
    total = len(split_diff(diff_text)[1])
    kept_count = total - len(excluded)
    policy_skipped = total > 0 and kept_count == 0

    if excluded:
        Path(diff_path).write_text(kept_text, encoding="utf-8", errors="surrogateescape")
        with open(context_path, "a", encoding="utf-8", errors="replace") as fh:
            fh.write(context_section(excluded, rule_path))
        print(
            f"{len(excluded)} file(s) excluded by policy ({rule_path}),"
            f" {kept_count} kept:"
        )
        for path in excluded:
            print(f"  - {path}")
    else:
        print(f"{rule_path} present; no diff entry matched ({total} kept)")

    _write_github_output(policy_skipped, excluded)
    _write_result(
        result_path,
        rule_path=rule_path,
        policy_skipped=policy_skipped,
        paths=excluded,
        kept_count=kept_count,
    )
    if policy_skipped:
        print(
            f"::notice title=lens-ignore::every changed file is policy-excluded"
            f" ({len(excluded)}); the review is skipped"
        )


if __name__ == "__main__":
    main()
