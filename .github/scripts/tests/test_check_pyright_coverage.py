"""The pyright coverage gate refuses the empty set, and reads globs as globs.

Three gates in a row on PR #192 passed something they should not have: a
hardcoded root that a wrong `include` could not disturb, then a reduction
of every exclude to a directory name, then a count derived from the same
config pyright read -- which made a one-character `include` typo produce
`filesAnalyzed=0 expected=0` and exit 0, the AT-2418 defect rebuilt inside
the gate meant to end it. None of the three could be tested, because each
was Python inside a YAML `run:` block. This module is what the extraction
buys: each case below was measured against the in-workflow gate first.
"""

from __future__ import annotations

import json
import tomllib
from pathlib import Path, PurePath

import pytest

import check_pyright_coverage as gate

ROOT = Path(__file__).resolve().parents[3]
EXCLUDE = ["**/__pycache__", "**/node_modules", "**/.venv", "**/.pytest_cache"]


def tree(root: Path, *files: str) -> Path:
    for name in files:
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("x = 1\n", encoding="utf-8")
    return root


# ------------------------------------------------------------ the empty set


def test_nothing_selected_is_refused_even_when_pyright_agrees() -> None:
    """0 == 0 is the equality the previous gate accepted."""
    assert gate.refusal(0, 0) is not None


def test_a_full_run_passes() -> None:
    assert gate.refusal(57, 57) is None


@pytest.mark.parametrize("analyzed", [0, 56, 58])
def test_a_different_count_is_refused(analyzed: int) -> None:
    """Fewer is a skipped file; more is a file pulled in from outside."""
    assert gate.refusal(analyzed, 57) is not None


# ------------------------------------------------------------- the matcher


def test_an_any_depth_file_glob_excludes_only_what_it_names(tmp_path: Path) -> None:
    """`**/generated_*.py` -- the control the directory-name reduction failed."""
    tree(tmp_path, ".github/scripts/generated_probe.py", ".github/scripts/real.py")
    selected = gate.expected_files(
        tmp_path, [".github/scripts"], [*EXCLUDE, "**/generated_*.py"]
    )
    assert selected == [Path(".github/scripts/real.py")]


def test_a_directory_pattern_excludes_everything_beneath_it(tmp_path: Path) -> None:
    """`**/.venv` names the directory; the files under it go with it."""
    tree(
        tmp_path,
        ".github/scripts/.venv/lib/python3.14/site-packages/dep.py",
        ".github/scripts/real.py",
    )
    assert gate.expected_files(tmp_path, [".github/scripts"], EXCLUDE) == [
        Path(".github/scripts/real.py")
    ]


def test_an_anchored_pattern_matches_from_the_root_only(tmp_path: Path) -> None:
    tree(
        tmp_path,
        ".github/scripts/kept.py",
        ".github/scripts/vendor/dep.py",
        "other/.github/scripts/vendor/dep.py",
    )
    exclude = [*EXCLUDE, ".github/scripts/vendor"]
    # The pattern names one path from the root: the first vendor/ is dropped,
    # the identically-named one under other/ is not.
    assert gate.expected_files(tmp_path, [".github/scripts", "other"], exclude) == [
        Path(".github/scripts/kept.py"),
        Path("other/.github/scripts/vendor/dep.py"),
    ]


def test_a_second_include_root_is_counted(tmp_path: Path) -> None:
    """The control the hardcoded root failed."""
    tree(tmp_path, ".github/scripts/a.py", "examples/b.py")
    assert gate.expected_files(tmp_path, [".github/scripts", "examples"], EXCLUDE) == [
        Path(".github/scripts/a.py"),
        Path("examples/b.py"),
    ]


def test_a_nested_include_root_counts_each_file_once(tmp_path: Path) -> None:
    """Measured on the real tree: nested roots gave 94 files for 59, 35 of
    them duplicates, and pyright counts each file once."""
    tree(tmp_path, ".github/scripts/a.py", ".github/scripts/tests/test_a.py")
    roots = [".github/scripts", ".github/scripts/tests"]
    selected = gate.expected_files(tmp_path, roots, EXCLUDE)
    assert selected == [
        Path(".github/scripts/a.py"),
        Path(".github/scripts/tests/test_a.py"),
    ]
    assert len(selected) == len(set(selected))


def test_a_stub_counts_because_pyright_counts_it(tmp_path: Path) -> None:
    """Measured: one `.pyi` under .github/scripts takes pyright's
    filesAnalyzed to 60 against 59 .py files, so a .py-only count fails a
    correct config."""
    tree(tmp_path, ".github/scripts/a.py")
    (tmp_path / ".github/scripts/a.pyi").write_text(
        "def f() -> int: ...\n", encoding="utf-8"
    )
    assert gate.expected_files(tmp_path, [".github/scripts"], EXCLUDE) == [
        Path(".github/scripts/a.py"),
        Path(".github/scripts/a.pyi"),
    ]


def test_a_pattern_shape_the_gate_cannot_read_is_refused_not_guessed() -> None:
    with pytest.raises(ValueError, match="unsupported exclude pattern"):
        gate.check_exclude_patterns(["a/**/b"])


def test_a_bad_pattern_is_refused_whatever_its_position(tmp_path: Path) -> None:
    """Measured on the lazy form: a path under `__pycache__` matched the first
    pattern and returned before the malformed second was ever read."""
    tree(tmp_path, ".github/scripts/a.py", ".github/scripts/__pycache__/a.pyc")
    with pytest.raises(ValueError, match="unsupported exclude pattern 'a/\\*\\*/b'"):
        gate.expected_files(tmp_path, [".github/scripts"], ["**/__pycache__", "a/**/b"])
    assert not gate.is_excluded(PurePath("x/y.py"), ["**/__pycache__"])


def test_a_bad_pattern_is_refused_although_the_tree_selects_nothing(
    tmp_path: Path,
) -> None:
    """Measured on the lazy form: an empty selection never called the matcher,
    so the refusal reported was the generic empty-set message."""
    tree(tmp_path, "elsewhere/a.py")
    (tmp_path / ".github/scripts").mkdir(parents=True)
    with pytest.raises(ValueError, match="unsupported exclude pattern"):
        gate.expected_files(tmp_path, [".github/scripts"], ["a/**/b"])


def test_an_include_root_that_is_not_a_directory_is_refused(tmp_path: Path) -> None:
    """The typo the total cannot show: `exampels` beside a real root selects
    the surviving root's count, which is what pyright reports (measured)."""
    tree(tmp_path, ".github/scripts/a.py", "examples/b.py")
    both = gate.expected_files(tmp_path, [".github/scripts", "examples"], EXCLUDE)
    with pytest.raises(ValueError, match="include root 'exampels' is not a directory"):
        gate.expected_files(tmp_path, [".github/scripts", "exampels"], EXCLUDE)
    # The control: without the check, the typo's count is the survivor's.
    assert (
        len(gate.expected_files(tmp_path, [".github/scripts"], EXCLUDE))
        == len(both) - 1
    )


def test_an_include_root_that_contributes_nothing_is_refused(tmp_path: Path) -> None:
    """A root that exists and is entirely excluded is the same blind spot."""
    tree(tmp_path, ".github/scripts/a.py", "vendor/__pycache__/dep.py")
    with pytest.raises(
        ValueError, match="include root 'vendor' contributes no source file"
    ):
        gate.expected_files(tmp_path, [".github/scripts", "vendor"], EXCLUDE)


def test_the_matcher_names_the_path_itself_not_its_children() -> None:
    """`is_excluded` walks the parents; `matches` alone must not."""
    assert gate.matches(PurePath(".github/scripts/.venv"), "**/.venv")
    assert not gate.matches(PurePath(".github/scripts/.venv/x.py"), "**/.venv")
    assert gate.is_excluded(PurePath(".github/scripts/.venv/x.py"), ["**/.venv"])


# ---------------------------------------------------------- the real tree


def test_the_repository_selection_is_not_empty_and_skips_caches() -> None:
    with open(ROOT / "pyproject.toml", "rb") as handle:
        config = tomllib.load(handle)["tool"]["pyright"]
    selected = gate.expected_files(ROOT, config["include"], config["exclude"])
    assert selected, "the repository's own [tool.pyright] selects nothing"
    assert all("__pycache__" not in path.parts for path in selected)


def test_ci_runs_this_gate_and_not_an_inline_copy() -> None:
    """The point of the extraction: the gate CI runs is the one tested here.

    The earlier form excused the step's name with a `.replace()` that could
    not matter -- `must analyze every file` holds no `filesAnalyzed` -- so it
    protected nothing and read as if an exemption were in force. These two
    assert the property the module claims: no heredoc Python in the workflow,
    and no reading of pyright's report there.
    """
    workflow = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
    assert "check_pyright_coverage.py" in workflow
    assert "python3 - " not in workflow
    assert "filesAnalyzed" not in workflow


# --------------------------------------------------------- end to end


def report(files_analyzed: int) -> dict[str, object]:
    return {
        "summary": {"filesAnalyzed": files_analyzed, "errorCount": 0},
        "generalDiagnostics": [],
    }


def run_main(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, include: str, analyzed: int
) -> int:
    tree(tmp_path, ".github/scripts/a.py")
    (tmp_path / "pyproject.toml").write_text(
        f'[tool.pyright]\ninclude = ["{include}"]\nexclude = ["**/__pycache__"]\n',
        encoding="utf-8",
    )
    (tmp_path / "pyright.json").write_text(
        json.dumps(report(analyzed)), encoding="utf-8"
    )
    monkeypatch.chdir(tmp_path)
    return gate.main(["check_pyright_coverage.py", "pyright.json"])


def test_main_refuses_a_root_that_does_not_exist(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The measured typo, end to end: a refusal with a reason, not a
    traceback -- the run it is asked about is the one nobody can interpret
    without one."""
    assert run_main(tmp_path, monkeypatch, ".github/script", 0) == 1
    assert "include root '.github/script' is not a directory" in capsys.readouterr().err


def test_main_refuses_a_selection_that_is_empty_for_another_reason(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """`expected == 0` still has to hold: `include` may be absent entirely."""
    tree(tmp_path, ".github/scripts/a.py")
    (tmp_path / "pyproject.toml").write_text(
        '[tool.pyright]\ninclude = []\nexclude = ["**/__pycache__"]\n', encoding="utf-8"
    )
    (tmp_path / "pyright.json").write_text(json.dumps(report(0)), encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    assert gate.main(["check_pyright_coverage.py", "pyright.json"]) == 1
    assert "selects no .py files" in capsys.readouterr().err


def test_main_refuses_misuse_with_its_usage(capsys: pytest.CaptureFixture[str]) -> None:
    """No stdin branch: the only caller passes a path, so a missing argument
    is a usage error rather than a silent wait on an empty stdin."""
    assert gate.main(["check_pyright_coverage.py"]) == 2
    assert "usage: check_pyright_coverage.py <pyright.json>" in capsys.readouterr().err


def test_main_passes_a_matching_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert run_main(tmp_path, monkeypatch, ".github/scripts", 1) == 0


def test_main_reports_a_missing_exclude_instead_of_a_keyerror(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Measured before the fix: pyright analyzed 0 with no exclude -- the
    AT-2418 defect -- and the gate died with `KeyError: 'exclude'` and an
    empty stdout, so the one case it exists for produced no diagnosis."""
    tree(tmp_path, ".github/scripts/a.py")
    (tmp_path / "pyproject.toml").write_text(
        '[tool.pyright]\ninclude = [".github/scripts"]\n', encoding="utf-8"
    )
    (tmp_path / "pyright.json").write_text(json.dumps(report(0)), encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    assert gate.main(["check_pyright_coverage.py", "pyright.json"]) == 1
    captured = capsys.readouterr()
    assert "filesAnalyzed=0 expected=1" in captured.out
    assert "analyzed 0 of 1" in captured.err


def test_main_refuses_a_short_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    assert run_main(tmp_path, monkeypatch, ".github/scripts", 0) == 1
    assert "analyzed 0 of 1" in capsys.readouterr().err
