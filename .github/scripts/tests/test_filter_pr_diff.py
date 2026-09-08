"""Tests for filter_pr_diff.py, the policy exclusion of the prepare step (AT-2206).

Three layers are pinned down.

1. The matcher: every gitignore semantic the module docstring promises, as
   pure-function cases, including the ones that are easy to get subtly wrong
   (anchoring by an inner slash, directory-only rules, `**` in its three
   positions, negation with last-match-wins, escaped trailing whitespace,
   malformed lines that warn and skip).
2. The diff filter: every entry shape git emits -- modify, new, deleted,
   rename (both sides), copy, binary, binary patch, mode-only, no trailing
   newline -- is dropped whole or kept byte-identical, and the rule file is
   never excludable.
3. The step: `Filter policy-excluded files` in base-ai-review-prepare.yml is
   executed as the shell it is, against a stubbed `gh`, so the pinned-checkout
   guard, the skip comment and the outputs are observed rather than asserted
   about; plus the wiring through orchestrator and aggregate.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path
from typing import Any

import pytest
import yaml

SCRIPT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SCRIPT_DIR))

import filter_pr_diff  # noqa: E402
from filter_pr_diff import (  # noqa: E402
    CONTEXT_SECTION_HEADING,
    DEFAULT_RULE_PATH,
    filter_diff,
    matches,
    parse_rules,
    split_diff,
)

SCRIPT = SCRIPT_DIR / "filter_pr_diff.py"
WORKFLOWS = SCRIPT_DIR.parents[0] / "workflows"
PREPARE = WORKFLOWS / "base-ai-review-prepare.yml"
ORCHESTRATOR = WORKFLOWS / "base-ai-review-orchestrator.yml"
AGGREGATE = WORKFLOWS / "base-ai-review-aggregate.yml"
CORRESPONDENCE = SCRIPT_DIR.parents[0] / "drift-check" / "correspondence.yml"

STEP_NAME = "Filter policy-excluded files"
POLICY_GATE = "steps.policy.outputs.policy_skipped != 'true'"

requires_jq = pytest.mark.skipif(shutil.which("jq") is None, reason="jq not installed")


def _rules(text: str) -> list[filter_pr_diff.Rule]:
    return parse_rules(textwrap.dedent(text))


# ---------------------------------------------------------------------------
# 1. Matcher
# ---------------------------------------------------------------------------


class TestMatcherSemantics:
    @pytest.mark.parametrize(
        ("pattern", "path", "expected"),
        [
            # No slash: any depth; a match as a directory covers what is under it.
            ("*.lock", "poetry.lock", True),
            ("*.lock", "sub/dir/poetry.lock", True),
            ("*.lock", "sub/poetry.lockx", False),
            ("*.lock", "a.lock/inside.txt", True),
            ("secrets", "secrets", True),
            ("secrets", "deep/secrets/key.pem", True),
            # `*` never crosses a slash; `?` is exactly one non-slash char.
            ("a*b", "a/b", False),
            ("file?.txt", "file1.txt", True),
            ("file?.txt", "file/.txt", False),
            ("file?.txt", "file.txt", False),
            # Leading slash anchors to the root.
            ("/secrets.txt", "secrets.txt", True),
            ("/secrets.txt", "sub/secrets.txt", False),
            # An inner slash anchors too, like gitignore.
            ("build/*", "build/a", True),
            ("build/*", "build/a/b", True),
            ("build/*", "x/build/a", False),
            # Trailing slash: directory only, everything beneath it.
            ("docs/", "docs/a.md", True),
            ("docs/", "docs/sub/a.md", True),
            ("docs/", "docs", False),
            ("docs/", "x/docs/a.md", True),
            # `**` in its three positions.
            ("**/vendor", "vendor", True),
            ("**/vendor", "a/b/vendor/x.js", True),
            ("gen/**", "gen/x", True),
            ("gen/**", "gen/x/y", True),
            ("gen/**", "gen", False),
            ("a/**/b", "a/b", True),
            ("a/**/b", "a/x/y/b", True),
            ("a/**/b", "a/x/yb", False),
            # `**` anywhere else is a plain `*`.
            ("x.**.y", "x.foo.y", True),
            ("x.**.y", "x.foo/bar.y", False),
            # Character classes, with and without negation and ranges.
            ("*.[ch]", "x.c", True),
            ("*.[ch]", "x.o", False),
            ("*.[!c]", "x.o", True),
            ("*.[!c]", "x.c", False),
            ("a[x-z]b", "ayb", True),
            ("a[x-z]b", "aab", False),
            # Regex metacharacters in a pattern are literal.
            ("a.b", "aXb", False),
            ("a+b", "a+b", True),
            ("(x)", "(x)", True),
        ],
    )
    def test_pattern(self, pattern: str, path: str, expected: bool) -> None:
        assert matches(parse_rules(pattern + "\n"), path) is expected

    def test_comments_and_blank_lines_are_ignored(self) -> None:
        rules = _rules(
            """\
            # a comment

            *.md
            """
        )
        assert [r.pattern for r in rules] == ["*.md"]

    def test_escaped_hash_and_bang_are_literal(self) -> None:
        rules = _rules("\\#literal\n\\!bang\n")
        assert matches(rules, "#literal") is True
        assert matches(rules, "!bang") is True
        assert all(not r.negated for r in rules)

    def test_trailing_whitespace_is_trimmed_unless_escaped(self) -> None:
        assert matches(parse_rules("trail  \n"), "trail") is True
        assert matches(parse_rules("trail  \n"), "trail ") is False
        assert matches(parse_rules("trail\\ \n"), "trail ") is True
        assert matches(parse_rules("trail\\ \n"), "trail") is False

    def test_negation_last_match_wins(self) -> None:
        rules = _rules(
            """\
            *.md
            !README.md
            /docs/**
            !docs/keep.md
            """
        )
        assert matches(rules, "x.md") is True
        assert matches(rules, "README.md") is False
        assert matches(rules, "sub/README.md") is False
        assert matches(rules, "docs/x.txt") is True
        assert matches(rules, "docs/keep.md") is False

    def test_negation_before_the_rule_does_not_win(self) -> None:
        rules = _rules("!README.md\n*.md\n")
        assert matches(rules, "README.md") is True

    def test_no_rules_match_nothing(self) -> None:
        assert matches([], "anything") is False

    def test_malformed_lines_warn_with_their_number_and_are_skipped(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        rules = _rules(
            """\
            [z-a]x
            /
            !
            ok.txt
            """
        )
        assert [r.pattern for r in rules] == ["ok.txt"]
        err = capsys.readouterr().err
        assert "::warning title=lens-ignore::line 1 skipped" in err
        assert "::warning title=lens-ignore::line 2 skipped" in err
        assert "::warning title=lens-ignore::line 3 skipped" in err
        assert "line 4" not in err

    def test_unclosed_class_is_a_literal_bracket(self) -> None:
        assert matches(parse_rules("a[b\n"), "a[b") is True
        assert matches(parse_rules("a[b\n"), "ab") is False


# ---------------------------------------------------------------------------
# 2. Diff filter
# ---------------------------------------------------------------------------

MODIFY = textwrap.dedent(
    """\
    diff --git a/src/app.py b/src/app.py
    index 1111111..2222222 100644
    --- a/src/app.py
    +++ b/src/app.py
    @@ -1,2 +1,2 @@
    -old
    +new
     keep
    """
)
NEW_FILE = textwrap.dedent(
    """\
    diff --git a/secrets/key.pem b/secrets/key.pem
    new file mode 100644
    index 0000000..3333333
    --- /dev/null
    +++ b/secrets/key.pem
    @@ -0,0 +1 @@
    +PRIVATE
    """
)
DELETED = textwrap.dedent(
    """\
    diff --git a/old.txt b/old.txt
    deleted file mode 100644
    index 4444444..0000000
    --- a/old.txt
    +++ /dev/null
    @@ -1 +0,0 @@
    -gone
    """
)
RENAME = textwrap.dedent(
    """\
    diff --git a/config/prod.env b/config/live.env
    similarity index 90%
    rename from config/prod.env
    rename to config/live.env
    index 5555555..6666666 100644
    --- a/config/prod.env
    +++ b/config/live.env
    @@ -1 +1 @@
    -A=1
    +A=2
    """
)
PURE_RENAME = textwrap.dedent(
    """\
    diff --git a/a.txt b/moved/b.txt
    similarity index 100%
    rename from a.txt
    rename to moved/b.txt
    """
)
COPY = textwrap.dedent(
    """\
    diff --git a/tmpl.txt b/copies/copy.txt
    similarity index 100%
    copy from tmpl.txt
    copy to copies/copy.txt
    """
)
BINARY = textwrap.dedent(
    """\
    diff --git a/img/logo.png b/img/logo.png
    index 7777777..8888888 100644
    Binary files a/img/logo.png and b/img/logo.png differ
    """
)
BINARY_PATCH = textwrap.dedent(
    """\
    diff --git a/blob.bin b/blob.bin
    new file mode 100644
    index 0000000..9999999
    GIT binary patch
    literal 4
    LcmZQzU|;|M00aO4

    literal 0
    HcmV?d00001

    """
)
MODE_ONLY = textwrap.dedent(
    """\
    diff --git a/run.sh b/run.sh
    old mode 100644
    new mode 100755
    """
)
NO_NEWLINE = textwrap.dedent(
    """\
    diff --git a/notes.txt b/notes.txt
    index aaaaaaa..bbbbbbb 100644
    --- a/notes.txt
    +++ b/notes.txt
    @@ -1 +1 @@
    -end
    \\ No newline at end of file
    +end!
    \\ No newline at end of file
    """
)
LENS_IGNORE_ENTRY = textwrap.dedent(
    """\
    diff --git a/.github/lens-ignore b/.github/lens-ignore
    index ccccccc..ddddddd 100644
    --- a/.github/lens-ignore
    +++ b/.github/lens-ignore
    @@ -1 +1,2 @@
     *.pem
    +*.py
    """
)
ALL_ENTRIES = [
    MODIFY,
    NEW_FILE,
    DELETED,
    RENAME,
    PURE_RENAME,
    COPY,
    BINARY,
    BINARY_PATCH,
    MODE_ONLY,
    NO_NEWLINE,
]


class TestSplitDiff:
    def test_every_entry_shape_is_one_entry_with_its_paths(self) -> None:
        _, entries = split_diff("".join(ALL_ENTRIES))
        assert [(e.old_path, e.new_path) for e in entries] == [
            ("src/app.py", "src/app.py"),
            (None, "secrets/key.pem"),
            ("old.txt", None),
            ("config/prod.env", "config/live.env"),
            ("a.txt", "moved/b.txt"),
            ("tmpl.txt", "copies/copy.txt"),
            ("img/logo.png", "img/logo.png"),
            ("blob.bin", "blob.bin"),
            ("run.sh", "run.sh"),
            ("notes.txt", "notes.txt"),
        ]

    def test_entries_reassemble_to_the_input(self) -> None:
        text = "".join(ALL_ENTRIES)
        preamble, entries = split_diff(text)
        assert preamble + "".join(e.text for e in entries) == text

    def test_preamble_before_the_first_header_is_kept(self) -> None:
        preamble, entries = split_diff("some preamble\n" + MODIFY)
        assert preamble == "some preamble\n"
        assert len(entries) == 1

    def test_header_with_a_space_in_the_path(self) -> None:
        text = "diff --git a/my dir/file b.txt b/my dir/file b.txt\nold mode 100644\nnew mode 100755\n"
        _, entries = split_diff(text)
        assert entries[0].paths == ["my dir/file b.txt", "my dir/file b.txt"]

    def test_quoted_header_path_is_unquoted(self) -> None:
        text = 'diff --git "a/caf\\303\\251.txt" "b/caf\\303\\251.txt"\nold mode 100644\nnew mode 100755\n'
        _, entries = split_diff(text)
        assert entries[0].new_path == "caf\u00e9.txt"

    def test_no_entries_in_text_without_headers(self) -> None:
        assert split_diff("") == ("", [])
        assert split_diff("not a diff\n") == ("not a diff\n", [])


class TestFilterDiff:
    @pytest.mark.parametrize(
        ("entry", "rule"),
        [
            (MODIFY, "src/"),
            (NEW_FILE, "*.pem"),
            (DELETED, "old.txt"),
            (BINARY, "*.png"),
            (BINARY_PATCH, "*.bin"),
            (MODE_ONLY, "run.sh"),
            (NO_NEWLINE, "notes.txt"),
            (PURE_RENAME, "moved/"),
            (COPY, "copies/"),
        ],
        ids=[
            "modify",
            "new-file",
            "deleted",
            "binary",
            "binary-patch",
            "mode-only",
            "no-newline",
            "pure-rename",
            "copy",
        ],
    )
    def test_each_entry_shape_is_dropped_whole(self, entry: str, rule: str) -> None:
        before = MODIFY.replace("src/app.py", "before/x.py")
        after = MODIFY.replace("src/app.py", "after/x.py")
        kept, excluded = filter_diff(before + entry + after, parse_rules(rule + "\n"))
        assert len(excluded) == 1
        # Only the excluded entry is gone; its neighbours are intact.
        assert kept == before + after

    def test_kept_entries_are_byte_identical(self) -> None:
        text = "".join(ALL_ENTRIES)
        kept, excluded = filter_diff(text, parse_rules("*.pem\n*.png\n"))
        assert excluded == ["secrets/key.pem", "img/logo.png"]
        assert kept == "".join(e for e in ALL_ENTRIES if e not in (NEW_FILE, BINARY))

    def test_no_match_returns_the_input_unchanged(self) -> None:
        text = "".join(ALL_ENTRIES)
        assert filter_diff(text, parse_rules("nothing-here\n")) == (text, [])

    @pytest.mark.parametrize("rule", ["config/prod.env", "config/live.env"])
    def test_rename_is_excluded_when_either_side_matches(self, rule: str) -> None:
        kept, excluded = filter_diff(MODIFY + RENAME, parse_rules(rule + "\n"))
        assert kept == MODIFY
        assert excluded == ["config/prod.env -> config/live.env"]

    @pytest.mark.parametrize("rule", ["tmpl.txt", "copies/copy.txt"])
    def test_copy_is_excluded_when_either_side_matches(self, rule: str) -> None:
        kept, excluded = filter_diff(COPY + MODIFY, parse_rules(rule + "\n"))
        assert kept == MODIFY
        assert excluded == ["tmpl.txt -> copies/copy.txt"]

    def test_interleaved_entries_keep_their_order(self) -> None:
        text = MODIFY + NEW_FILE + DELETED + BINARY + NO_NEWLINE
        kept, excluded = filter_diff(text, parse_rules("*.pem\n*.png\n"))
        assert kept == MODIFY + DELETED + NO_NEWLINE
        assert excluded == ["secrets/key.pem", "img/logo.png"]

    def test_all_entries_excluded_leaves_nothing(self) -> None:
        kept, excluded = filter_diff("".join(ALL_ENTRIES), parse_rules("*\n"))
        assert kept == ""
        assert len(excluded) == len(ALL_ENTRIES)

    def test_negation_reinstates_an_entry(self) -> None:
        kept, excluded = filter_diff(MODIFY + NEW_FILE, parse_rules("*\n!secrets/key.pem\n"))
        assert kept == NEW_FILE
        assert excluded == ["src/app.py"]

    @pytest.mark.parametrize("rule", ["*", ".github/**", "/.github/lens-ignore", "lens-ignore"])
    def test_the_rule_file_is_never_excluded(
        self, rule: str, capsys: pytest.CaptureFixture[str]
    ) -> None:
        kept, excluded = filter_diff(LENS_IGNORE_ENTRY + MODIFY, parse_rules(rule + "\n"))
        assert kept.startswith(LENS_IGNORE_ENTRY)
        assert ".github/lens-ignore" not in excluded
        assert ".github/lens-ignore matches its own rules" in capsys.readouterr().err

    def test_a_renamed_rule_file_is_never_excluded_either(self) -> None:
        renamed = PURE_RENAME.replace("a.txt", ".github/lens-ignore")
        kept, excluded = filter_diff(renamed, parse_rules("*\n"))
        assert kept == renamed
        assert excluded == []

    def test_protected_path_follows_the_configured_rule_file(self) -> None:
        kept, excluded = filter_diff(MODIFY, parse_rules("*\n"), protected="src/app.py")
        assert kept == MODIFY
        assert excluded == []


# ---------------------------------------------------------------------------
# 2b. main(): files, outputs, context.md
# ---------------------------------------------------------------------------


def _workdir(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    diff: str,
    rules: str | None,
) -> Path:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "pr.diff").write_text(diff, encoding="utf-8")
    (tmp_path / "context.md").write_text("## PR Metadata\n", encoding="utf-8")
    if rules is not None:
        (tmp_path / ".github").mkdir()
        (tmp_path / ".github" / "lens-ignore").write_text(rules, encoding="utf-8")
    output = tmp_path / "github_output"
    output.touch()
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))
    for name in ("PR_DIFF", "LENS_IGNORE_PATH", "CONTEXT_MD", "POLICY_RESULT"):
        monkeypatch.delenv(name, raising=False)
    return tmp_path


def _outputs(root: Path) -> dict[str, str]:
    """Parse $GITHUB_OUTPUT, including the heredoc form of multiline values."""
    lines = (root / "github_output").read_text(encoding="utf-8").splitlines()
    parsed: dict[str, str] = {}
    i = 0
    while i < len(lines):
        line = lines[i]
        if "<<" in line:
            key, delimiter = line.split("<<", 1)
            end = lines.index(delimiter, i + 1)
            parsed[key] = "\n".join(lines[i + 1 : end])
            i = end + 1
            continue
        key, _, value = line.partition("=")
        parsed[key] = value
        i += 1
    return parsed


def _result(root: Path) -> dict[str, Any]:
    return dict(json.loads((root / ".review-context" / "lens-ignore.json").read_text()))


class TestMain:
    def test_absent_rule_file_touches_nothing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        root = _workdir(tmp_path, monkeypatch, diff="".join(ALL_ENTRIES), rules=None)
        before = (root / "pr.diff").stat().st_mtime_ns
        filter_pr_diff.main()
        assert (root / "pr.diff").read_text(encoding="utf-8") == "".join(ALL_ENTRIES)
        assert (root / "pr.diff").stat().st_mtime_ns == before
        assert (root / "context.md").read_text(encoding="utf-8") == "## PR Metadata\n"
        assert _outputs(root) == {
            "policy_skipped": "false",
            "excluded_count": "0",
            "excluded_paths": "",
        }
        assert _result(root)["rule_file"] is None

    def test_rules_without_a_match_leave_the_diff_alone(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        root = _workdir(tmp_path, monkeypatch, diff=MODIFY + NEW_FILE, rules="*.nomatch\n")
        filter_pr_diff.main()
        assert (root / "pr.diff").read_text(encoding="utf-8") == MODIFY + NEW_FILE
        assert (root / "context.md").read_text(encoding="utf-8") == "## PR Metadata\n"
        assert _outputs(root)["policy_skipped"] == "false"
        assert _outputs(root)["excluded_count"] == "0"
        assert _result(root) == {
            "rule_file": DEFAULT_RULE_PATH,
            "policy_skipped": False,
            "excluded_count": 0,
            "excluded_paths": [],
            "kept_count": 2,
        }

    def test_partial_exclusion_rewrites_the_diff_and_reports(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        root = _workdir(
            tmp_path, monkeypatch, diff=MODIFY + NEW_FILE + RENAME, rules="*.pem\nconfig/\n"
        )
        filter_pr_diff.main()
        assert (root / "pr.diff").read_text(encoding="utf-8") == MODIFY
        outputs = _outputs(root)
        assert outputs["policy_skipped"] == "false"
        assert outputs["excluded_count"] == "2"
        assert outputs["excluded_paths"] == "secrets/key.pem\nconfig/prod.env -> config/live.env"
        assert _result(root)["excluded_paths"] == [
            "secrets/key.pem",
            "config/prod.env -> config/live.env",
        ]
        assert _result(root)["kept_count"] == 1

    def test_context_section_names_paths_only(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        root = _workdir(tmp_path, monkeypatch, diff=MODIFY + NEW_FILE, rules="*.pem\n")
        filter_pr_diff.main()
        context = (root / "context.md").read_text(encoding="utf-8")
        assert context.startswith("## PR Metadata\n")
        assert CONTEXT_SECTION_HEADING in context
        assert "1 file(s) excluded by policy (.github/lens-ignore)" in context
        assert "Do not open, quote, or infer the content of these paths" in context
        assert "- `secrets/key.pem`" in context
        assert "PRIVATE" not in context

    def test_all_excluded_sets_policy_skipped(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        root = _workdir(tmp_path, monkeypatch, diff=NEW_FILE + BINARY, rules="*.pem\n*.png\n")
        filter_pr_diff.main()
        outputs = _outputs(root)
        assert outputs["policy_skipped"] == "true"
        assert outputs["excluded_count"] == "2"
        assert _result(root)["policy_skipped"] is True
        assert _result(root)["kept_count"] == 0

    def test_the_rule_file_change_is_kept_even_under_a_catch_all(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        root = _workdir(tmp_path, monkeypatch, diff=LENS_IGNORE_ENTRY + NEW_FILE, rules="*\n")
        filter_pr_diff.main()
        assert (root / "pr.diff").read_text(encoding="utf-8") == LENS_IGNORE_ENTRY
        assert _outputs(root)["policy_skipped"] == "false"
        assert _outputs(root)["excluded_paths"] == "secrets/key.pem"

    def test_env_overrides_every_path(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _workdir(tmp_path, monkeypatch, diff="", rules=None)
        (tmp_path / "other.diff").write_text(NEW_FILE, encoding="utf-8")
        (tmp_path / "rules.txt").write_text("*.pem\n", encoding="utf-8")
        (tmp_path / "ctx.md").write_text("", encoding="utf-8")
        monkeypatch.setenv("PR_DIFF", "other.diff")
        monkeypatch.setenv("LENS_IGNORE_PATH", "rules.txt")
        monkeypatch.setenv("CONTEXT_MD", "ctx.md")
        monkeypatch.setenv("POLICY_RESULT", "out/result.json")
        filter_pr_diff.main()
        assert (tmp_path / "other.diff").read_text(encoding="utf-8") == ""
        assert "(rules.txt)" in (tmp_path / "ctx.md").read_text(encoding="utf-8")
        assert json.loads((tmp_path / "out" / "result.json").read_text())["rule_file"] == "rules.txt"

    def test_missing_diff_fails(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        root = _workdir(tmp_path, monkeypatch, diff="", rules="*.pem\n")
        (root / "pr.diff").unlink()
        with pytest.raises(SystemExit) as excinfo:
            filter_pr_diff.main()
        assert excinfo.value.code == 1

    def test_non_utf8_bytes_in_a_kept_entry_survive(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        raw = MODIFY.encode("utf-8").replace(b"+new\n", b"+n\xffw\n")
        root = _workdir(tmp_path, monkeypatch, diff="", rules="*.pem\n")
        (root / "pr.diff").write_bytes(raw + NEW_FILE.encode("utf-8"))
        filter_pr_diff.main()
        assert (root / "pr.diff").read_bytes() == raw


# ---------------------------------------------------------------------------
# 3. The step and its wiring
# ---------------------------------------------------------------------------


def _steps(path: Path) -> list[dict[str, Any]]:
    workflow = yaml.safe_load(path.read_text(encoding="utf-8"))
    job = next(iter(workflow["jobs"].values()))
    return list(job["steps"])


def _step(name: str) -> dict[str, Any]:
    for step in _steps(PREPARE):
        if step.get("name") == name:
            return step
    raise AssertionError(f"step not found in {PREPARE.name}: {name}")


def _prepare_workflow() -> dict[str, Any]:
    return dict(yaml.safe_load(PREPARE.read_text(encoding="utf-8")))


class TestPrepareStep:
    def test_step_runs_after_extract_and_before_setup_python(self) -> None:
        names = [s.get("name") or s.get("uses", "") for s in _steps(PREPARE)]
        extract = names.index("Extract diff and context")
        policy = names.index(STEP_NAME)
        setup = next(i for i, n in enumerate(names) if n.startswith("actions/setup-python"))
        assert extract < policy < setup

    def test_step_is_gated_on_the_size_check_only(self) -> None:
        step = _step(STEP_NAME)
        assert step["id"] == "policy"
        assert step["if"] == "steps.size-check.outputs.skip == 'false'"

    def test_step_env_covers_the_skip_comment(self) -> None:
        env = _step(STEP_NAME)["env"]
        assert env["GH_TOKEN"] == "${{ github.token }}"
        assert env["PR_NUMBER"] == "${{ inputs.pr_number || github.event.pull_request.number }}"
        assert env["GITHUB_REPOSITORY"] == "${{ github.repository }}"

    def test_guard_mirrors_the_extract_step(self) -> None:
        run = _step(STEP_NAME)["run"]
        assert 'SCRIPT=".ai-dev-pr-review/.github/scripts/filter_pr_diff.py"' in run
        guard = run.index('if [ ! -f "$SCRIPT" ]')
        rules = run.index('if [ -f "$RULES" ]', guard)
        error = run.index("::error title=Pinned checkout predates lens-ignore::", rules)
        assert run.index("exit 1", error) < run.index("\n  fi\n", error)
        assert run.index('python3 "$SCRIPT"') > run.index("exit 0", guard)

    def test_every_later_step_is_gated_on_the_policy_skip(self) -> None:
        steps = _steps(PREPARE)
        after = steps[[s.get("name") for s in steps].index(STEP_NAME) + 1 :]
        assert after, "no steps follow the policy step -- selector is stale"
        for step in after:
            condition = str(step.get("if", ""))
            assert "steps.size-check.outputs.skip == 'false'" in condition, step
            assert POLICY_GATE in condition, step

    def test_no_earlier_step_is_gated_on_the_policy_skip(self) -> None:
        steps = _steps(PREPARE)
        before = steps[: [s.get("name") for s in steps].index(STEP_NAME)]
        for step in before:
            assert "steps.policy" not in str(step.get("if", "")), step

    def test_job_outputs_combine_the_two_skips(self) -> None:
        outputs = _prepare_workflow()["jobs"]["prepare"]["outputs"]
        assert outputs["skip"] == (
            "${{ steps.size-check.outputs.skip == 'true'"
            " || steps.policy.outputs.policy_skipped == 'true' }}"
        )
        assert outputs["size_skipped"] == "${{ steps.size-check.outputs.skip }}"
        assert outputs["policy_skipped"] == "${{ steps.policy.outputs.policy_skipped }}"
        assert outputs["excluded_count"] == "${{ steps.policy.outputs.excluded_count }}"
        assert outputs["excluded_paths"] == "${{ steps.policy.outputs.excluded_paths }}"

    def test_workflow_call_exposes_every_output(self) -> None:
        exposed = _prepare_workflow()[True]["workflow_call"]["outputs"]
        for key in ("skip", "size_skipped", "policy_skipped", "excluded_count", "excluded_paths"):
            assert exposed[key]["value"] == f"${{{{ jobs.prepare.outputs.{key} }}}}", key
            assert exposed[key]["description"]


def _write_gh_stub(root: Path) -> Path:
    bin_dir = root / "bin"
    bin_dir.mkdir()
    stub = bin_dir / "gh"
    stub.write_text(
        textwrap.dedent(
            """\
            #!/bin/sh
            printf '%s\\n' "$*" >> "$GH_CALL_LOG"
            """
        ),
        encoding="utf-8",
    )
    stub.chmod(0o755)
    return bin_dir


def _run_step(
    tmp_path: Path,
    *,
    pinned_script: bool,
    rules: str | None,
    diff: str = MODIFY + NEW_FILE,
) -> tuple[subprocess.CompletedProcess[str], dict[str, str], list[str]]:
    work = tmp_path / "work"
    work.mkdir()
    (work / "pr.diff").write_text(diff, encoding="utf-8")
    (work / "context.md").write_text("", encoding="utf-8")
    if rules is not None:
        (work / ".github").mkdir()
        (work / ".github" / "lens-ignore").write_text(rules, encoding="utf-8")
    if pinned_script:
        target = work / ".ai-dev-pr-review" / ".github" / "scripts"
        target.mkdir(parents=True)
        shutil.copy(SCRIPT, target / SCRIPT.name)
    output = tmp_path / "github_output"
    output.touch()
    call_log = tmp_path / "gh-calls.log"
    call_log.touch()
    env = {
        "PATH": f"{_write_gh_stub(tmp_path)}{os.pathsep}{os.environ['PATH']}",
        "GH_CALL_LOG": str(call_log),
        "GITHUB_OUTPUT": str(output),
        "GH_TOKEN": "gh-token",
        "PR_NUMBER": "35",
        "GITHUB_REPOSITORY": "ignite-corp/ai-dev-pr-review",
    }
    result = subprocess.run(
        ["bash", "-e", "-c", _step(STEP_NAME)["run"]],
        cwd=work,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    outputs = _outputs(tmp_path)
    calls = [line for line in call_log.read_text(encoding="utf-8").splitlines() if line]
    return result, outputs, calls


@requires_jq
class TestPrepareStepExecution:
    """The step as it runs: guard, script, comment, outputs."""

    def test_pin_without_the_script_fails_when_rules_exist(self, tmp_path: Path) -> None:
        result, outputs, calls = _run_step(tmp_path, pinned_script=False, rules="*.pem\n")
        assert result.returncode == 1
        assert "::error title=Pinned checkout predates lens-ignore::" in result.stdout
        assert outputs == {}
        assert calls == []

    def test_pin_without_the_script_reports_no_skip_without_rules(self, tmp_path: Path) -> None:
        result, outputs, calls = _run_step(tmp_path, pinned_script=False, rules=None)
        assert result.returncode == 0, result.stderr
        assert "::warning::filter_pr_diff.py not in pinned checkout" in result.stdout
        assert outputs == {"policy_skipped": "false", "excluded_count": "0", "excluded_paths": ""}
        assert calls == []
        assert (tmp_path / "work" / "pr.diff").read_text(encoding="utf-8") == MODIFY + NEW_FILE

    def test_partial_exclusion_posts_no_comment(self, tmp_path: Path) -> None:
        result, outputs, calls = _run_step(tmp_path, pinned_script=True, rules="*.pem\n")
        assert result.returncode == 0, result.stderr
        assert outputs["policy_skipped"] == "false"
        assert outputs["excluded_paths"] == "secrets/key.pem"
        assert calls == []
        assert (tmp_path / "work" / "pr.diff").read_text(encoding="utf-8") == MODIFY

    def test_all_excluded_posts_the_skip_comment(self, tmp_path: Path) -> None:
        result, outputs, calls = _run_step(tmp_path, pinned_script=True, rules="*\n")
        assert result.returncode == 0, result.stderr
        assert outputs["policy_skipped"] == "true"
        assert outputs["excluded_count"] == "2"
        assert calls == [
            "pr comment 35 --repo ignite-corp/ai-dev-pr-review --body [i] Only"
            " policy-excluded files changed (2 file(s) matched .github/lens-ignore)."
            " Skipping AI review -- the aggregate verdict below lists the paths."
        ]

    def test_no_rules_with_the_script_is_a_no_op(self, tmp_path: Path) -> None:
        result, outputs, calls = _run_step(tmp_path, pinned_script=True, rules=None)
        assert result.returncode == 0, result.stderr
        assert outputs["policy_skipped"] == "false"
        assert calls == []
        assert (tmp_path / "work" / "pr.diff").read_text(encoding="utf-8") == MODIFY + NEW_FILE


class TestPipelineWiring:
    def test_orchestrator_hands_the_aggregate_the_policy_outputs(self) -> None:
        jobs = yaml.safe_load(ORCHESTRATOR.read_text(encoding="utf-8"))["jobs"]
        supplied = dict(jobs["aggregate"]["with"])
        # The size input must be the size flag, not the combined skip: a
        # policy skip would otherwise render as a PR-too-large failure.
        assert supplied["size_skipped"] == "${{ needs.prepare.outputs.size_skipped }}"
        for key in ("policy_skipped", "excluded_count", "excluded_paths"):
            assert supplied[key] == f"${{{{ needs.prepare.outputs.{key} }}}}", key

    def test_reviewer_jobs_still_gate_on_the_combined_skip(self) -> None:
        jobs = yaml.safe_load(ORCHESTRATOR.read_text(encoding="utf-8"))["jobs"]
        reviewers = [n for n in jobs if n.startswith("review-")]
        assert reviewers
        for name in reviewers:
            assert "needs.prepare.outputs.skip == 'false'" in str(jobs[name]["if"]), name

    def test_aggregate_accepts_and_forwards_the_policy_inputs(self) -> None:
        workflow = yaml.safe_load(AGGREGATE.read_text(encoding="utf-8"))
        inputs = workflow[True]["workflow_call"]["inputs"]
        assert inputs["policy_skipped"]["default"] == "false"
        assert inputs["excluded_count"]["default"] == ""
        assert inputs["excluded_paths"]["default"] == ""
        step = next(s for s in _steps(AGGREGATE) if s.get("name") == "Aggregate and post verdict")
        assert step["env"]["POLICY_SKIPPED"] == "${{ inputs.policy_skipped }}"
        assert step["env"]["EXCLUDED_COUNT"] == "${{ inputs.excluded_count }}"
        assert step["env"]["EXCLUDED_PATHS"] == "${{ inputs.excluded_paths }}"

    def test_drift_correspondence_maps_the_step_to_a_same_named_wrapper_step(self) -> None:
        entries = yaml.safe_load(CORRESPONDENCE.read_text(encoding="utf-8"))["steps"]
        entry = next(e for e in entries if e["base_step"] == STEP_NAME)
        assert entry["base_file"] == PREPARE.name
        assert entry["wrapper_steps"] == [STEP_NAME]
