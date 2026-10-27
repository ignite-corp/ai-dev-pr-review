#!/usr/bin/env python3
"""Check a commit message or PR body against the Python source it describes.

During the 2026-10-15 round this check caught more than any other: a retracted
premise still sitting in one commit message, a stale sentence in one PR body, a
missing output case in a third. It then produced a FALSE PASS, and the way it
did is the reason this file exists.

The original matched with ``phrase in source_file_text``. A string the change
had stopped emitting still passed, because it survived five times in a comment
explaining why it had been removed. Comments are part of a file's text and no
part of its behaviour, so a text search cannot tell "this is emitted" from
"this is discussed". Nothing in the checker noticed; what noticed was a
prediction that disagreed with observation. Rewriting the source side on top of
``ast`` -- where comments do not exist -- surfaced five real problems in the
same inputs that had just passed.

WHAT "THE STRINGS A SOURCE FILE CAN EMIT" MEANS HERE
    Every string constant that survives into the module's AST, minus
    docstrings. Nothing else is excluded, and that bias is deliberate.

    Survives into the AST. A comment is not in the AST, nor is code inside a
    comment, nor a literal in a file that does not parse (that is an error, not
    an empty answer). This is the whole correction over text search.

    Including the literal segments of f-strings. ``f"{label}: BLOCKED on {n}"``
    contributes ``": BLOCKED on "``; the interpolations are holes whose values
    are not knowable from the syntax, so they are simply absent. A body that
    quotes the rendered line still matches, because the segments are substrings
    of it -- see the coverage rule below.

    Minus docstrings: the first statement of a module, class or function when
    it is a string constant. A docstring describes the code rather than being a
    value the code computes with, and a body describing behaviour should not be
    required to quote it. (``--help`` does print this module's docstring; that
    is not an output case anyone documents in a commit body.)

    Nothing narrower. The obvious refinement -- keep only the literals that
    reach ``print``/``write``/``raise`` -- is dataflow, and any syntactic
    approximation of it drops real output cases silently: a message assembled
    into a variable, returned, and printed by a caller would vanish. Dropping
    output cases silently is exactly what the completeness dimension exists to
    catch, so a narrowing would reproduce the original bug one level down. The
    cost of over-inclusion is visible instead: a dict key or a regex shows up
    in the completeness list and a human reads one extra line. Use --baseline
    to scope that list to the strings a change actually added.

THE FOUR DIMENSIONS
    Each was added for a specific failure in that round, and the last three
    catch what the first structurally cannot: accuracy only ever examines what
    the body already says.

    accuracy       Every claim the body makes is supported.
    completeness   The body names every string the source can emit (in scope).
    negative       No phrase retracted during the round survives in the body.
    provenance     Our strings and strings measured from a tool stay apart: a
                   claim about what the code emits is not settled by a
                   transcript, and vice versa.

HOW A CLAIM IS SUPPORTED
    A claim about the source is supported when every letter in it traces to a
    literal in the AST: occurrences of the emittable strings are removed from
    the claim, longest first, and the claim is supported when no letter is
    left. ``verdict: BLOCKED on 3 findings`` is supported by the segments
    ``"verdict"``, ``": BLOCKED on "`` and ``" findings"`` -- the residue is
    the interpolated ``3``. A claim quoting a string that lives only in a
    comment keeps its letters and is reported.

    A claim about measured output is supported by plain substring match against
    the transcript, which is literal output text rather than code.

    Both sides normalize runs of whitespace to one space, so a claim or an
    emittable string wrapped across lines still matches.

    A retracted phrase is also matched without regard to case, so the same
    words opening a sentence are still reported. Emittable strings are not:
    the code emits them with exact casing.

    Matching the BODY by substring is not the mistake described above. The
    mistake was searching SOURCE text, where a comment can carry a string the
    code no longer produces. A body is prose with no second layer under it.

BODY CLAIM SYNTAX
    A claim is a marker followed by a backticked span, anywhere in the body:

        the script emits: `verdict: APPROVED`
        the round measured: `ruff: 2 errors fixed`

    Markers are case-sensitive. Prose outside a marker is never read as a
    claim: backticks around file paths, flags and identifiers are ordinary
    prose, and treating them as claims would bury real findings. The price is
    that accuracy only reaches the claims a body marks, which is why the other
    three dimensions carry the weight they do.

KNOWN LIMITATIONS
    - A very short literal (one or two letters) can cover a claim's letters by
      accident, since removal is textual. Pinned by a test so it cannot change
      silently.
    - The negative list is a textual guard: a body that mentions a retracted
      phrase in order to disown it is reported like one that still asserts it.
      Deliberate -- the reviewer should see both.
    - Explicit ``+`` concatenation of two literals is not folded; each operand
      is reported on its own. Adjacent literals are folded by the parser.

Usage:
    check_body_claims.py --body FILE --source FILE [--source FILE ...]
                         [--baseline FILE ...] [--measured FILE]
                         [--retracted FILE]

``--body -`` reads the body from stdin, for ``git log -1 --format=%B | ...``.
Exit 0 when there are no findings, 1 when there are, 2 when an input cannot be
read or parsed.

Public-log safety: unlike the audit scripts beside it, this tool echoes the
content it was handed -- body text and source literals -- so its output is only
as public as its inputs. It is a local development tool; no workflow runs it.
"""

from __future__ import annotations

import argparse
import ast
import re
import sys
from dataclasses import dataclass
from pathlib import Path

ACCURACY = "accuracy"
COMPLETENESS = "completeness"
NEGATIVE = "negative"
PROVENANCE = "provenance"

EMITS = "emits"
MEASURED = "measured"

_CLAIM_RE = re.compile(rf"\b(?P<kind>{EMITS}|{MEASURED}):\s*`(?P<text>[^`]+)`")
_WHITESPACE = re.compile(r"\s+")
_DEFINES_DOCSTRING = (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)


def normalize(text: str) -> str:
    """Collapse runs of whitespace to one space so wrapped text still matches."""
    return _WHITESPACE.sub(" ", text).strip()


@dataclass(frozen=True)
class Claim:
    """A marked quotation from the body, with what the body attributes it to."""

    kind: str
    text: str


@dataclass(frozen=True)
class Finding:
    dimension: str
    detail: str

    def __str__(self) -> str:
        return f"{self.dimension}: {self.detail}"


def _docstring_nodes(tree: ast.AST) -> set[ast.Constant]:
    nodes: set[ast.Constant] = set()
    for node in ast.walk(tree):
        if not isinstance(node, _DEFINES_DOCSTRING):
            continue
        body = node.body
        if not body or not isinstance(body[0], ast.Expr):
            continue
        value = body[0].value
        if isinstance(value, ast.Constant) and isinstance(value.value, str):
            nodes.add(value)
    return nodes


def emittable_strings(source: str, filename: str = "<source>") -> list[str]:
    """Return the sorted, de-duplicated strings the source can emit.

    See the module docstring for what that means; raises SyntaxError when the
    source does not parse, because an unparsable file has no answer rather than
    an empty one.
    """
    tree = ast.parse(source, filename=filename)
    docstrings = _docstring_nodes(tree)
    found = {
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and node not in docstrings
    }
    return sorted(s for s in found if normalize(s))


def claims(body: str) -> list[Claim]:
    """Return the marked claims the body makes, in the order they appear."""
    found: list[Claim] = []
    for match in _CLAIM_RE.finditer(body):
        text = normalize(match.group("text"))
        if text:
            found.append(Claim(kind=match.group("kind"), text=text))
    return found


def load_phrases(text: str) -> list[str]:
    """Read a phrase list: one per line, ``#`` comments and blanks skipped."""
    phrases: list[str] = []
    for line in text.splitlines():
        stripped = normalize(line)
        if stripped and not stripped.startswith("#"):
            phrases.append(stripped)
    return phrases


def residue(claim: str, emittable: list[str]) -> str:
    """Remove every emittable string from the claim, longest first."""
    text = normalize(claim)
    for literal in sorted(
        (normalize(s) for s in emittable), key=lambda s: (-len(s), s)
    ):
        if literal:
            text = text.replace(literal, " ")
    return text


def supported_by_source(claim: str, emittable: list[str]) -> bool:
    """True when every letter in the claim traces to a literal in the AST."""
    return not any(ch.isalpha() for ch in residue(claim, emittable))


def supported_by_measurement(claim: str, measured: str) -> bool:
    return normalize(claim) in normalize(measured)


def scope_to_change(emittable: list[str], baseline: list[str]) -> list[str]:
    """Keep the emittable strings the baseline does not already emit."""
    before = {normalize(s) for s in baseline}
    return [s for s in emittable if normalize(s) not in before]


def _judge_claim(claim: Claim, emittable: list[str], measured: str) -> Finding | None:
    from_source = supported_by_source(claim.text, emittable)
    from_tool = supported_by_measurement(claim.text, measured)
    if claim.kind == EMITS:
        if from_source:
            return None
        if from_tool:
            return Finding(
                PROVENANCE,
                f'emits claim "{claim.text}" is supported only by the measured '
                "output, not by any literal in the source",
            )
        return Finding(
            ACCURACY,
            f'emits claim "{claim.text}" quotes text no literal in the source '
            "can produce",
        )
    # claims() yields no other kind, so the rest is the MEASURED branch.
    if from_tool:
        return None
    if from_source:
        return Finding(
            PROVENANCE,
            f'measured claim "{claim.text}" is credited to a tool, but the '
            "source emits it",
        )
    return Finding(
        ACCURACY,
        f'measured claim "{claim.text}" does not appear in the measured output',
    )


def audit(
    *,
    body: str,
    emittable: list[str],
    completeness_scope: list[str],
    measured: str = "",
    retracted: list[str] | None = None,
) -> list[Finding]:
    """Return the findings for one body, sorted by dimension then detail."""
    findings: list[Finding] = []
    for claim in claims(body):
        finding = _judge_claim(claim, emittable, measured)
        if finding is not None:
            findings.append(finding)

    normalized_body = normalize(body)
    folded_body = normalized_body.casefold()
    for candidate in map(normalize, completeness_scope):
        if candidate not in normalized_body:
            findings.append(
                Finding(
                    COMPLETENESS,
                    f'the body never names the emittable string "{candidate}"',
                )
            )
    for phrase in map(normalize, retracted or []):
        if phrase.casefold() in folded_body:
            findings.append(
                Finding(
                    NEGATIVE,
                    f'the body still carries the retracted phrase "{phrase}"',
                )
            )
    return sorted(findings, key=lambda f: (f.dimension, f.detail))


def _read(path: str) -> str:
    if path == "-":
        return sys.stdin.read()
    return Path(path).read_text(encoding="utf-8")


def _pooled_strings(paths: list[str]) -> list[str]:
    pooled: set[str] = set()
    for path in paths:
        pooled.update(emittable_strings(_read(path), filename=path))
    return sorted(pooled)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--body", required=True, help="body file, or - for stdin")
    parser.add_argument(
        "--source",
        required=True,
        action="append",
        default=[],
        help="a source file the body describes; repeatable",
    )
    parser.add_argument(
        "--baseline",
        action="append",
        default=[],
        help="the same source before the change; scopes completeness to what "
        "the change added. Repeatable; omit to check the whole file",
    )
    parser.add_argument("--measured", help="transcript of tool output from the round")
    parser.add_argument("--retracted", help="phrases withdrawn during the round")
    args = parser.parse_args(argv)

    try:
        body = _read(args.body)
        emittable = _pooled_strings(args.source)
        baseline = _pooled_strings(args.baseline)
        measured = _read(args.measured) if args.measured else ""
        retracted = load_phrases(_read(args.retracted)) if args.retracted else []
    except (OSError, UnicodeDecodeError) as exc:
        print(f"cannot read input: {exc}", file=sys.stderr)
        return 2
    except (SyntaxError, ValueError) as exc:
        print(f"source does not parse: {exc}", file=sys.stderr)
        return 2

    findings = audit(
        body=body,
        emittable=emittable,
        completeness_scope=scope_to_change(emittable, baseline),
        measured=measured,
        retracted=retracted,
    )
    for finding in findings:
        print(finding)
    return 1 if findings else 0


if __name__ == "__main__":
    sys.exit(main())
