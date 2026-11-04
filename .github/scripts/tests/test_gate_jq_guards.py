"""Every jq read the merge gate assigns is guarded (hyuk-hur/dev-dotfiles#204).

The gate block in the plugin's SKILL.md is pasted whole into shells whose
`set -e` / `pipefail` state it does not control. An unguarded
`X=$(... | jq ...)` leaves `X` empty on a failed read and carries on with
status 0, and downstream an empty value reads as a legitimate absence: no
comments, no reviews, no threads, no verdict. A broken read then looks
exactly like a clean answer, which is the defect this class has had four
times.

A guard sees the status of the substitution's last pipeline stage, which
is jq, unless the shell has `pipefail` on. So the guard proves jq did not
fail, and the network read has to sit in a separate, guarded assignment of
its own -- the split the issue asked for. The block keeps that split today,
and two tests below hold it there: no jq read in it also runs `gh`, and
every `gh` read is itself guarded. Both see `gh` by name only: a network
read through another tool or through a shell function wrapping `gh` is not
detected.

Measured on the dotfiles copy 2026-10-23, 18 of 37 such assignments had no
guard. The block as it reached this repository guards every one, but
nothing pinned that: a new unguarded read would have passed every check.
This scan is the pin. It has to be quote-aware -- jq programs carry
unbalanced parentheses inside single quotes, and two naive scans miscounted
`matched` and `newest` as unguarded for exactly that reason -- so the
scanner gets controls of its own before its count of the real block is
trusted.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

import pytest

from tests_support import SKILL

# The fenced block the gate lives in: SKILL.md carries several pasteable
# blocks, and this one is the one that declares the gate's PR literal.
_GATE_ANCHOR = "\nPR='<pr number>'\n"

# jq-bearing command-substitution assignments the gate block holds today.
# Update it together with the block, never to match a shortfall: a scanner
# that stopped finding assignments would otherwise report zero unguarded.
# The dotfiles measurement's 37 was a different revision of the block, read
# by a different scanner; 38 is this block, read by this one.
EXPECTED_JQ_ASSIGNMENTS = 38

# A declaration builtin returns its own status, 0, whatever the
# substitution did, so no read in its command can ever be guarded.
_DECLARE = re.compile(r"(?:local|export|readonly|declare|typeset)(?=\s)")
# The value is the whole shell word after `=`, in any shape: bare,
# quoted, prefixed, `$(...)` or backticks. It is a read when a command
# substitution in it runs jq.
# `NAME=`, `NAME+=` and `NAME[i]=` alike: each takes its status from the
# value's last substitution.
_ASSIGN = re.compile(r"(?P<name>[A-Za-z_]\w*)(?:\[[^\]]*\])?\+?=")
# This scan covers assignment reads only, the scope of
# hyuk-hur/dev-dotfiles#204. Two shapes next to it are pinned absent: an
# array value `X=( ... )` and a process substitution `< <( ... )`. Both
# leave an empty result with status 0 on a failed jq, neither runs jq in
# the gate block today, and
# `test_the_gate_block_reads_jq_in_no_unscanned_shape` holds it there.
# Other non-assignment reads (`for x in $(jq ...)`, `<<< "$(jq ...)"`, a
# test on jq output) can fail the same way and are not checked here.
_UNSCANNED = re.compile(r"(?:[A-Za-z_]\w*(?:\[[^\]]*\])?\+?=|<)\(")
_WORD_START = " \t\n;(|&!"
_WORD_END = " \t\n;|&<>()"
_COMMAND_END = ";\n&|"


def _tool_word(tool: str) -> re.Pattern[str]:
    """A tool's name as a word: not part of a longer name or a flag."""
    return re.compile(rf"(?<![\w-]){re.escape(tool)}(?![\w-])")


_JQ = _tool_word("jq")
_CONTINUATION = re.compile(r"(?:[ \t]|\\\n)*")
# A guard stops the gate or the function, or marks the run failed and
# leaves the pass: the shapes the block uses are `exit 1`, `return 1` and
# `rc=1; continue`. An `rc=1` that falls through, or that `break`s out of
# the loop, is not one: the code after it still reads the empty value.
# Matched in the group's text with its quoted strings and comments
# blanked, and only after an unconditional separator, so an `exit 1` that
# is echoed, commented out, or behind `&&` / `||` is not a guard. A group
# holding a compound command is not read at all: an `exit` inside an `if`,
# `case` or loop branch is conditional, and no real guard needs one.
_GUARD_OPEN = re.compile(r"\|\|\s*\{")
_FUNCTION = re.compile(
    r"^[ \t]*(?:function[ \t]+)?[A-Za-z_][\w-]*[ \t]*\(\)[ \t]*\{[ \t]*$", re.M
)
_COMPOUND = re.compile(r"(?:^|[;\n{&|(])\s*(?:if|case|while|until|for|select)\b")
_FAILS = re.compile(
    r"(?:^|[;\n{])\s*"
    r"(?:(?:exit|return)\s+[1-9]\d*\b|rc=[1-9]\d*\s*;\s*continue\b)"
)
_FAILS_TOP = re.compile(
    r"(?:^|[;\n{])\s*(?:exit\s+[1-9]\d*\b|rc=[1-9]\d*\s*;\s*continue\b)"
)
_GH = _tool_word("gh")
# A heredoc body is plain text, which this scanner would read as shell; a
# here-string, `<<<`, is not one.
_HEREDOC = re.compile(r"(?<!<)<<(?!<)-?\s*(?:\\|['\"])?[A-Za-z_]")


@dataclass(frozen=True)
class Assignment:
    name: str
    line: int
    guarded: bool
    value: str


def gate_block(text: str) -> str:
    """The body of the fenced bash block that declares the gate's PR literal."""
    anchor = text.index(_GATE_ANCHOR)
    start = text.rindex("```bash\n", 0, anchor) + len("```bash\n")
    return text[start : text.index("\n```\n", start)]


def _skip_comment(s: str, i: int) -> int:
    end = s.find("\n", i)
    return len(s) if end < 0 else end


def _skip_double(s: str, i: int) -> int:
    """s[i - 1] is an opening double quote; return the index after its close."""
    while s[i] != '"':
        if s[i] == "\\":
            i += 2
        elif s.startswith("$(", i):
            i = _close(s, i + 2) + 1
        else:
            i += 1
    return i + 1


def _close(s: str, i: int) -> int:
    """s[i - 2 : i] is `$(`; return the index of the parenthesis that closes it."""
    depth = 1
    while True:
        c = s[i]
        if c == "\\":
            i += 2
        elif c == "'":
            i = s.index("'", i + 1) + 1
        elif c == '"':
            i = _skip_double(s, i + 1)
        elif c == "#" and s[i - 1] in _WORD_START:
            i = _skip_comment(s, i)
        elif s.startswith("$(", i):
            i = _close(s, i + 2) + 1
        else:
            if c == "(":
                depth += 1
            elif c == ")":
                depth -= 1
                if depth == 0:
                    return i
            i += 1


def _blank(s: str) -> str:
    """s with its quoted strings and comments replaced by spaces, same length."""
    out = list(s)
    i = 0
    while i < len(s):
        c = s[i]
        if c == "\\":
            i += 2
            continue
        if c in "'\"" or (c == "#" and (i == 0 or s[i - 1] in _WORD_START)):
            if c == "'":
                end = s.index("'", i + 1) + 1
            elif c == '"':
                end = _skip_double(s, i + 1)
            else:
                end = _skip_comment(s, i)
            for k in range(i, end):
                out[k] = " "
            i = end
            continue
        i += 1
    return "".join(out)


def _inner(sub: str) -> str:
    """The body of a `$(...)` or backtick substitution."""
    return sub[2:-1] if sub.startswith("$(") else sub[1:-1]


def _last_command(body: str) -> str:
    """The last top-level command of a substitution body: the one whose status it returns."""
    blank = _blank(body)
    depth, cut = 0, 0
    for k, c in enumerate(blank):
        if c in "({":
            depth += 1
        elif c in ")}":
            depth -= 1
        elif (
            depth == 0
            and c in ";\n|&"
            and not (c == "\n" and k and blank[k - 1] == "\\")
            # `2>&1`, `>&2`, `&>`: the `&` of a redirection.
            and not (
                c == "&"
                and (blank[k - 1 : k] in ("<", ">") or blank[k + 1 : k + 2] == ">")
            )
            and body[k + 1 :].strip(" \t\n;|&")
        ):
            cut = k + 1
    return body[cut:]


def _group_commands(s: str, i: int) -> str:
    """s[i - 1] is a group's `{`; its own text up to the matching `}`.

    Quotes, comments, nested `{ ... }` groups and `( ... )` subshells are
    blanked: a nested group can sit behind `&&` or `||`, and an exit in a
    subshell ends only the subshell, so neither is read as the outer
    group's own.
    """
    out: list[str] = []
    depth = 1
    while True:
        c = s[i]
        if c == "\\":
            out.append("  ")
            i += 2
        elif c == "'":
            end = s.index("'", i + 1) + 1
            out.append(" " * (end - i))
            i = end
        elif c == '"':
            end = _skip_double(s, i + 1)
            out.append(" " * (end - i))
            i = end
        elif c == "#" and s[i - 1] in _WORD_START + "{":
            end = _skip_comment(s, i)
            out.append(" " * (end - i))
            i = end
        elif s.startswith("${", i):
            end = s.index("}", i) + 1
            out.append(" " * (end - i))
            i = end
        elif s.startswith("$(", i):
            end = _close(s, i + 2) + 1
            out.append(" " * (end - i))
            i = end
        elif c == "(":
            end = _close(s, i + 1) + 1
            out.append(" " * (end - i))
            i = end
        else:
            if c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    return "".join(out)
            out.append(c if depth == 1 and c != "}" else " ")
            i += 1


def _guarded(rest: str, in_function: bool) -> bool:
    """rest opens with a `|| { ... }` group that runs a failing exit, return or rc.

    `return` counts only inside a function: at the top level of a pasted
    block it is an error bash reports and then carries on past.
    """
    opened = _GUARD_OPEN.match(rest)
    if opened is None:
        return False
    group = "{" + _group_commands(rest, opened.end())
    fails = _FAILS if in_function else _FAILS_TOP
    return _COMPOUND.search(group) is None and fails.search(group) is not None


def _function_spans(block: str) -> list[tuple[int, int]]:
    """Each function body in block, from its `name() {` line to its lone `}`.

    The span search relies on each function closing on a `}` at column
    one; `test_every_gate_function_closes_on_a_column_one_brace` holds the
    gate block to that shape.
    """
    spans: list[tuple[int, int]] = []
    for m in _FUNCTION.finditer(block):
        close = re.compile(r"^\}$", re.M).search(block, m.end())
        spans.append((m.start(), close.end() if close else len(block)))
    return spans


def _word_end(s: str, i: int, found: list[str] | None = None) -> int:
    """The index just past the shell word starting at s[i].

    Each command substitution the word carries, outside single quotes, is
    appended to `found` in order.
    """
    while i < len(s) and s[i] not in _WORD_END:
        if s[i] == "\\":
            i += 2
        elif s[i] == "'":
            i = s.index("'", i + 1) + 1
        elif s[i] == '"':
            i += 1
            while s[i] != '"':
                if s[i] == "\\":
                    i += 2
                elif s.startswith("$(", i) or s[i] == "`":
                    i = _substitution_end(s, i, found)
                else:
                    i += 1
            i += 1
        elif s.startswith("$(", i) or s[i] == "`":
            i = _substitution_end(s, i, found)
        elif s.startswith("${", i):
            i = s.index("}", i) + 1
        else:
            i += 1
    return i


def _substitution_end(s: str, i: int, found: list[str] | None) -> int:
    """s[i] opens `$(` or a backtick; record its body and return the index past it."""
    end = _close(s, i + 2) + 1 if s[i] == "$" else s.index("`", i + 1) + 1
    if found is not None:
        found.append(s[i:end])
    return end


def _invokes(command: str, tool: str) -> bool:
    """command runs tool as its command word, not only names it in an argument.

    A `!` before it inverts the status the guard would see, so a negated
    command is not one.
    """
    return (
        re.match(
            rf"\s*(?:[A-Za-z_]\w*=\S*\s+)*(?:\S*/)?{re.escape(tool)}(?![\w-])",
            _blank(command),
        )
        is not None
    )


def jq_assignments(
    block: str, tool: str = "jq", in_function: bool | None = None
) -> list[Assignment]:
    """Every `X=<word>` at a word start whose value runs tool in a substitution.

    The tool is jq unless named: the same scan reads the `gh` reads. A read
    is guarded only when tool is the command word of the last command of
    its last substitution, which is the status the guard sees.

    A word start, not strictly command position: an argument such as
    `-f body=$(jq ...)` is counted too, which errs toward flagging.
    Quoting is honoured at every level: a parenthesis, a `#` or an `X=$(`
    inside a single- or double-quoted string is text, not shell, except
    the body of a `$(...)` inside double quotes, which is shell and is
    scanned as such. No read in
    a `local`, `export`, `readonly`, `declare` or `typeset` command is
    guarded, because the builtin returns 0 whatever the substitution did.
    `in_function` is for the recursion into a substitution body, which
    inherits it; at the top it is read off the block's function bodies.
    """
    found: list[Assignment] = []
    spans = _function_spans(block) if in_function is None else []

    def fn_at(k: int) -> bool:
        if in_function is not None:
            return in_function
        return any(a <= k < b for a, b in spans)

    # Set by a declaration builtin, cleared at the end of its command: every
    # assignment it carries is masked, not only the first.
    masked = False
    i = 0
    while i < len(block):
        c = block[i]
        if c == "\\":
            i += 2
            continue
        if c == "'":
            i = block.index("'", i + 1) + 1
            continue
        if c == "`":
            # A backtick substitution's body is shell, read like `$(...)`.
            end = block.index("`", i + 1)
            found += _shifted(block, i + 1, end, tool, fn_at(i))
            i = end + 1
            continue
        if c == '"':
            i = _double_quoted_reads(block, i + 1, tool, found, fn_at(i))
            continue
        if c in _COMMAND_END:
            masked = False
        at_word = i == 0 or block[i - 1] in _WORD_START
        if c == "#" and at_word:
            i = _skip_comment(block, i)
            continue
        builtin = _DECLARE.match(block, i) if at_word else None
        if builtin is not None:
            masked = True
            i = builtin.end()
            continue
        m = _ASSIGN.match(block, i) if at_word else None
        if m is None:
            i += 1
            continue
        reads, i = _read_at(block, m, tool, masked=masked, in_function=fn_at(i))
        found += reads
    return found


def _read_at(
    block: str, m: re.Match[str], tool: str, *, masked: bool, in_function: bool
) -> tuple[list[Assignment], int]:
    """The reads one `X=<word>` assignment carries, and the index past its word.

    A read nested in the value's substitutions is a read of its own. The
    assignment itself is a read when a substitution in its value runs
    tool, and it is guarded only when nothing masks the status the guard
    sees: its status is its last substitution's, and that is the status
    of that substitution's last command.
    """
    reads: list[Assignment] = []
    substitutions: list[str] = []
    after = _word_end(block, m.end(), substitutions)
    at = m.end()
    for sub in substitutions:
        at = block.index(sub, at)
        start = at + (2 if sub.startswith("$(") else 1)
        reads += _shifted(block, start, at + len(sub) - 1, tool, in_function)
        at += len(sub)
    if any(_tool_word(tool).search(sub) for sub in substitutions):
        rest = _CONTINUATION.sub("", block[after:], count=1)
        reads.append(
            Assignment(
                name=m["name"],
                line=block.count("\n", 0, m.start()) + 1,
                guarded=not masked
                and _invokes(_last_command(_inner(substitutions[-1])), tool)
                and _guarded(rest, in_function),
                value=block[m.end() : after],
            )
        )
    return reads, after


def _double_quoted_reads(
    block: str, i: int, tool: str, found: list[Assignment], in_function: bool
) -> int:
    """block[i - 1] opens a double-quoted string: add the reads in its
    substitutions to found, which are shell, and return the index past it."""
    while block[i] != '"':
        if block[i] == "\\":
            i += 2
        elif block.startswith("$(", i):
            end = _close(block, i + 2)
            found += _shifted(block, i + 2, end, tool, in_function)
            i = end + 1
        elif block[i] == "`":
            end = block.index("`", i + 1)
            found += _shifted(block, i + 1, end, tool, in_function)
            i = end + 1
        else:
            i += 1
    return i + 1


def _shifted(
    block: str, start: int, end: int, tool: str, in_function: bool
) -> list[Assignment]:
    """The reads in block[start:end], with their lines counted from block's start."""
    offset = block.count("\n", 0, start)
    return [
        Assignment(a.name, a.line + offset, a.guarded, a.value)
        for a in jq_assignments(block[start:end], tool, in_function)
    ]


def _unguarded(block: str) -> list[str]:
    return [
        f"{a.name} (block line {a.line})"
        for a in jq_assignments(block)
        if not a.guarded
    ]


@pytest.fixture(scope="module")
def block() -> str:
    """The real gate block, read once."""
    return gate_block(SKILL.read_text(encoding="utf-8"))


# --- the scanner's own controls ------------------------------------------


@pytest.mark.parametrize(
    "snippet",
    [
        "x=$(printf '%s' \"$j\" | jq -r 'select(.a == \")\")') || { echo e; exit 1; }",
        "x=$(jq -r '(.a | (.b' <<< \"$j\") \\\n  || { echo e; exit 1; }",
        'f() {\n  x=$(jq -r .a <<< "$j") || { echo e; return 1; }\n}',
        "items=$(jq -r '.[]' <<< \"$j\") || { echo e; rc=1; continue; }",
        'x=$(jq -r .a <<< "$j" 2>&1) || { echo e; exit 1; }',
        'x=$(printf \'%s\' "$(jq -r .a <<< "$j")" | jq -r .b) || { echo e; exit 1; }',
        'x=$(jq -r .a <<< "$j") || { echo "${PR} } failed"; exit 1; }',
        'x="$(jq -r .a <<< "$j")" || { echo e; exit 1; }',
        'x="${PR}: $(jq -r .a <<< "$j")" || { echo e; exit 1; }',
        'x=`jq -r .a <<< "$j"` || { echo e; exit 1; }',
    ],
    ids=[
        "paren-in-quotes",
        "unbalanced-paren-continued",
        "return-inside-a-function",
        "rc-continue",
        "redirection-ampersand",
        "nested",
        "braces-in-guard-text",
        "quoted-value",
        "quoted-value-with-prefix",
        "backticks",
    ],
)
def test_scanner_reads_a_guarded_read_as_guarded(snippet: str) -> None:
    """Parentheses inside a jq program, line continuations and nesting do not cut the read short."""
    found = jq_assignments(snippet + "\n")
    assert [a.guarded for a in found] == [True], found


@pytest.mark.parametrize(
    "snippet",
    [
        'x=$(jq -r \'.a)\' <<< "$j")\n[ -n "$x" ] || { echo e; exit 1; }',
        'x=$(jq -r .a <<< "$j") || true',
        'x=$(jq -r .a <<< "$j") || { echo e; }',
        'f() {\n  local x=$(jq -r .a <<< "$j") || { echo e; return 1; }\n}',
        'f() {\n  local -r x=$(jq -r .a <<< "$j") || { echo e; return 1; }\n}',
        'f() {\n  local a=1 x=$(jq -r .a <<< "$j") || { echo e; return 1; }\n}',
        'x="$(jq -r .a <<< "$j")"',
        'x="p $(jq -r .a <<< "$j")"',
        'x=pre$(jq -r .a <<< "$j")',
        'x=`jq -r .a <<< "$j"`',
        'x=$(jq -r .a <<< "$j") || { [ -n "$x" ] && exit 1; }',
        'x=$(jq -r .a <<< "$j") || { echo e; false || exit 1; }',
        'x=$(jq -r .a <<< "$j") || { echo e; rc=1; }',
        'x=$(jq -r .a <<< "$j") || { echo e; rc=1; break; }',
        'x=$(jq -r .a <<< "$j") || { true && { exit 1; }; }',
        'x=$(jq -r .a <<< "$j") || { true || { exit 1; }; }',
        'x=$(jq -r .a <<< "$j") || { echo e; (echo e; exit 1); }',
        'x=$(jq -r .a <<< "$j" | head -1) || { echo e; exit 1; }',
        'x=$(jq .a <<< "$j"; true) || { echo e; exit 1; }',
        "x=$(jq . <<< \"$j\" | sed 's/jq/jq/') || { echo e; exit 1; }",
        'x=$(! jq . <<< "$j") || { echo e; exit 1; }',
        'x=$(jq -r .a <<< "$j") || { echo e; return 1; }',
        'x+=$(jq -r .a <<< "$j")',
        'a[0]=$(jq -r .a <<< "$j")',
        'x=$(jq -r .a <<< "$j") || { if false; then\n  exit 1\nfi; }',
        'x=$(jq -r .a <<< "$j") || { case $j in a) exit 1 ;; esac; }',
        'x="$(jq -r .a <<< "$j")$(printf ok)" || { echo e; exit 1; }',
        'export x=$(jq -r .a <<< "$j") || { echo e; exit 1; }',
        'y=1; x=$(jq length <<< "$j")',
        'x=$(jq -r .a <<< "$j") || { echo "will not exit 1"; }',
        "f() {\n  x=$(jq -r .a <<< \"$j\") || { echo 'return 1'; }\n}",
        'x=$(jq -r .a <<< "$j") || { echo e\n  # exit 1\n}',
    ],
    ids=[
        "guard-on-next-command",
        "or-true",
        "guard-that-continues",
        "local",
        "local-with-option",
        "local-second-name",
        "quoted-value",
        "quoted-value-with-prefix",
        "unquoted-value-with-prefix",
        "backticks",
        "exit-behind-and",
        "exit-behind-or",
        "rc-that-falls-through",
        "rc-that-breaks",
        "exit-in-nested-group-behind-and",
        "exit-in-nested-group-behind-or",
        "exit-in-subshell",
        "jq-piped-into-another-stage",
        "jq-followed-by-a-command",
        "jq-named-only-in-the-last-stage-argument",
        "negated-jq",
        "return-outside-a-function",
        "append-assignment",
        "subscript-assignment",
        "exit-inside-if",
        "exit-inside-case",
        "masked-by-a-later-substitution",
        "export",
        "after-semicolon",
        "exit-only-in-double-quotes",
        "return-only-in-single-quotes",
        "exit-only-in-a-comment",
    ],
)
def test_scanner_reads_an_unguarded_read_as_unguarded(snippet: str) -> None:
    """A read whose failure the next statement cannot see is flagged."""
    found = jq_assignments(snippet + "\n")
    assert [a.guarded for a in found] == [False], found


@pytest.mark.parametrize(
    "snippet",
    [
        "# x=$(jq .a)\n",
        "echo 'x=$(jq .a)'\n",
        'echo "not x=$(echo jq)"\n',
        "x=$(gh pr view 1 --json body) || { echo e; exit 1; }\n",
    ],
    ids=["comment", "single-quoted", "double-quoted", "no-jq"],
)
def test_scanner_ignores_what_is_not_a_jq_assignment(snippet: str) -> None:
    """Text that only looks like an assignment, and a read with no jq in it, are not counted."""
    assert jq_assignments(snippet) == []


# --- the gate block -----------------------------------------------------


def test_gate_block_holds_the_expected_reads(block: str) -> None:
    """The scan finds the block's jq reads by the pinned count, including the two naive scans missed."""
    found = jq_assignments(block)
    assert len(found) == EXPECTED_JQ_ASSIGNMENTS, [a.name for a in found]
    names = {a.name for a in found}
    assert {"matched", "newest", "reviews", "thread_data"} <= names, names


def test_every_jq_read_in_the_gate_block_is_guarded(block: str) -> None:
    """No jq-bearing assignment in the gate block falls through on a failed read."""
    assert _unguarded(block) == []


def test_a_guard_cut_from_the_real_block_is_caught(block: str) -> None:
    """Removing one real guard turns the scan red, naming that read: the pin is load-bearing."""
    guard = '|| { echo "ERROR: could not assemble the review list"; exit 1; }'
    assert block.count(guard) == 1, "the control's target guard moved; pick another"
    assert _unguarded(block.replace(guard, "")) == [
        f"reviews (block line {next(a.line for a in jq_assignments(block) if a.name == 'reviews')})"
    ]


def test_a_quoted_unguarded_read_added_to_the_real_block_is_caught(block: str) -> None:
    """A new `X="$(... jq ...)"` with no guard raises the count and is named."""
    grown = block + '\nnew_read="$(printf \'%s\' "$comments" | jq length)"\n'
    assert len(jq_assignments(grown)) == EXPECTED_JQ_ASSIGNMENTS + 1
    assert [u.split(" ")[0] for u in _unguarded(grown)] == ["new_read"]


def test_no_jq_read_in_the_gate_block_also_runs_gh(block: str) -> None:
    """The guard sees jq's status only, so the network read stays in an assignment of its own."""
    found = jq_assignments(block)
    assert [a.name for a in found if _GH.search(a.value)] == []
    # Control: the same check sees a read that pipes gh into jq.
    piped = jq_assignments("x=$(gh api y | jq .a) || { echo e; exit 1; }\n")
    assert [a.name for a in piped if _GH.search(a.value)] == ["x"]


def test_the_gate_block_holds_no_heredoc(block: str) -> None:
    """The scanner reads every line as shell; a heredoc's text would not be."""
    assert _HEREDOC.search(block) is None
    # Controls: a heredoc is seen, a here-string is not one.
    assert _HEREDOC.search("cat <<'EOF'\nit's\nEOF\n") is not None
    assert _HEREDOC.search("cat <<\\EOF\nit's\nEOF\n") is not None
    assert _HEREDOC.search('read -r a <<<"$b"\n') is None


def _unscanned_jq(block: str) -> list[str]:
    """Each array value or process substitution in block whose text runs jq."""
    found: list[str] = []
    # Matched on the block with quotes and comments blanked, so a `<(` in
    # a jq program or a string is not taken for shell.
    for m in _UNSCANNED.finditer(_blank(block)):
        body = block[m.end() : _close(block, m.end())]
        if _JQ.search(body):
            found.append(m.group(0) + body + ")")
    return found


def test_the_gate_block_reads_jq_in_no_unscanned_shape(block: str) -> None:
    """No jq read hides in an array value or a process substitution, which the scan does not model."""
    assert _unscanned_jq(block) == []
    # Controls: both shapes are seen when they run jq; an array of plain
    # words, the shape the block does carry, is not one.
    assert len(_unscanned_jq("x=($(jq -r '.[]' <<< \"$j\"))\n")) == 1
    assert len(_unscanned_jq("mapfile -t x < <(jq -r '.[]' <<< \"$j\")\n")) == 1
    assert len(_unscanned_jq("x+=($(jq -r '.[]' <<< \"$j\"))\n")) == 1
    assert _unscanned_jq('a=(-f "before=$c")\n') == []
    assert _unscanned_jq("x=$(jq 'select(.n<(.m))' <<< \"$j\") || { exit 1; }\n") == []


def test_an_assignment_inside_a_double_quoted_substitution_is_scanned() -> None:
    """The body of `"$(...)"` is shell: a read there is counted and judged like any other."""
    found = jq_assignments('echo "$(x=$(jq -r .a <<< "$j"); echo "$x")"\n')
    assert [(a.name, a.guarded) for a in found] == [("x", False)]
    found = jq_assignments('echo "$(x=$(jq -r .a <<< "$j") || { echo e; exit 1; })"\n')
    assert [(a.name, a.guarded) for a in found] == [("x", True)]


def test_a_read_nested_in_a_substitution_is_counted() -> None:
    """`x=$(y=$(jq ...); echo "$y")`: the inner read is a read, and the outer one is masked by echo."""
    found = jq_assignments(
        'x=$(y=$(jq -r .a <<< "$j"); echo "$y") || { echo e; exit 1; }\n'
    )
    assert sorted((a.name, a.guarded) for a in found) == [("x", False), ("y", False)]


def test_a_read_inside_backticks_is_scanned() -> None:
    """A backtick body is shell, at top level and inside double quotes."""
    for snippet in ('`x=$(jq -r .a <<< "$j")`\n', 'echo "`x=$(jq -r .a <<< "$j")`"\n'):
        assert [(a.name, a.guarded) for a in jq_assignments(snippet)] == [("x", False)]


def test_every_gh_read_in_the_gate_block_is_guarded(block: str) -> None:
    """The network read split out of each jq read carries a guard of its own.

    Without one, a failed `gh` leaves its variable empty, and the jq read of
    it that follows may succeed on empty input: its own guard never fires.
    """
    found = jq_assignments(block, "gh")
    assert {"HEAD_SHA"} <= {a.name for a in found}, [a.name for a in found]
    assert [f"{a.name} (block line {a.line})" for a in found if not a.guarded] == []
    # Controls: the same scan reads a gh read with and without its guard.
    assert [
        a.guarded for a in jq_assignments("x=$(gh api y) || { exit 1; }\n", "gh")
    ] == [True]
    assert [a.guarded for a in jq_assignments("x=$(gh api y)\n", "gh")] == [False]
    assert [
        a.guarded for a in jq_assignments("x=$(! gh api y) || { exit 1; }\n", "gh")
    ] == [False]


def test_every_gate_function_closes_on_a_column_one_brace(block: str) -> None:
    """Each function's lone `}` comes before the next function opens, as `_function_spans` assumes."""
    opens = [m.start() for m in _FUNCTION.finditer(block)]
    assert len(opens) >= 10, opens
    for k, start in enumerate(opens):
        limit = opens[k + 1] if k + 1 < len(opens) else len(block)
        assert re.compile(r"^\}$", re.M).search(block, start, limit), block[
            start : start + 80
        ]
    # Control: an indented closer is not one, so the check sees it.
    assert re.compile(r"^\}$", re.M).search("f() {\n  x\n  }\n") is None
