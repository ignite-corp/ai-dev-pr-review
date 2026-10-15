"""The formatter's reach is configuration, and this holds it (AT-2508).

CI runs ``ruff format --check .`` from the repository root against
``ruff.toml``, so the walk reaches everything under the root that no line
there excludes. Three places under the root are not this working tree's
code, and ``ruff.toml`` carves each one out:

* ``docs/tasks/local-review-driver-design-record/evidence/`` -- a preserved
  run, cited by the record it sits in; reformatting it would make the text
  differ from the artifact the record describes.
* ``.ai-dev-pr-review/`` -- the pinned release of this same repository that
  this repository's own review workflows check out beside the tree.
* ``.claude/worktrees/`` -- other branches of this repository, checked out
  by Claude Code.

Nothing asserted that the carve-outs still applied: a config edit could drop
one and every test would stay green, because the suite never ran the
formatter. These cases do, as a subprocess, with the ruff the suite shares
with CI (``requirements-dev.txt`` pins it; it is installed before pytest in
``ci.yml``). Ruff absent is a failure, never a skip.

Two kinds of case. The mirror cases copy the committed ``ruff.toml`` into a
throwaway tree that carries an unformatted file at every carved-out path
and one outside them, so the direction is exact in both senses: every
carve-out is honoured and nothing else is. The in-tree cases run the same
config against the real evidence directory, which a mirror cannot see move.

One more case runs no formatter: ``ruff.toml``'s ``target-version`` is a
copy of ``.github/scripts/.python-version``, the interpreter CI runs under,
and the case holds the copy equal to its source.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
RUFF_TOML = REPO_ROOT / "ruff.toml"
PYTHON_VERSION_FILE = REPO_ROOT / ".github" / "scripts" / ".python-version"

EVIDENCE = Path("docs/tasks/local-review-driver-design-record/evidence")
VENDORED_CHECKOUT = Path(".ai-dev-pr-review")
CLAUDE_WORKTREES = Path(".claude/worktrees")

# One unformatted file per carve-out, at a depth a real one would have.
CARVED_OUT_FILES = (
    EVIDENCE / "scripts" / "measure.py",
    VENDORED_CHECKOUT / ".github" / "scripts" / "vendored.py",
    CLAUDE_WORKTREES / "agent-x" / ".github" / "scripts" / "other_branch.py",
)
OUTSIDE_FILE = Path(".github/scripts/outside.py")

# `ruff format` rewrites this to `x = [1, 2, 3]`, so a file holding it is
# reported wherever the walk reaches.
UNFORMATTED = "x = [1,2,3]\n"
WOULD_REFORMAT = "Would reformat: "
# What ruff prints when every named path is excluded (force-exclude = true).
NOTHING_FOUND = "No Python files found under the given path(s)"


def _format_check(*paths: str, cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "ruff", "format", "--check", *paths],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )


def _reported(result: subprocess.CompletedProcess[str]) -> set[str]:
    return {
        line.removeprefix(WOULD_REFORMAT)
        for line in result.stdout.splitlines()
        if line.startswith(WOULD_REFORMAT)
    }


@pytest.fixture
def mirror(tmp_path: Path) -> Path:
    """The committed config over a tree with an unformatted file everywhere."""
    shutil.copy(RUFF_TOML, tmp_path / "ruff.toml")
    for relative in (*CARVED_OUT_FILES, OUTSIDE_FILE):
        target = tmp_path / relative
        target.parent.mkdir(parents=True)
        target.write_text(UNFORMATTED, encoding="utf-8")
    return tmp_path


def test_walk_reaches_exactly_the_files_outside_the_carve_outs(
    mirror: Path,
) -> None:
    result = _format_check(".", cwd=mirror)
    assert result.returncode == 1, result.stderr
    assert _reported(result) == {OUTSIDE_FILE.as_posix()}


@pytest.mark.parametrize("carved_out", CARVED_OUT_FILES, ids=lambda p: p.parts[0])
def test_carve_out_holds_when_the_path_is_named_directly(
    mirror: Path, carved_out: Path
) -> None:
    # force-exclude: a contributor's `ruff format <that path>` must not
    # rewrite it either.
    result = _format_check(carved_out.as_posix(), cwd=mirror)
    assert result.returncode == 0, result.stdout
    assert NOTHING_FOUND in result.stderr


def test_evidence_record_is_excluded_in_the_real_tree() -> None:
    # The mirror cannot see the record move; this can. The script the
    # record cites must be where the carve-out says it is ...
    assert (REPO_ROOT / EVIDENCE / "scripts" / "measure.py").is_file()
    # ... and the formatter must not find it there.
    result = _format_check(EVIDENCE.as_posix(), cwd=REPO_ROOT)
    assert result.returncode == 0, result.stdout
    assert NOTHING_FOUND in result.stderr


def test_this_module_is_not_carved_out_of_the_real_tree() -> None:
    # The opposite direction in the real tree: a test file is found and
    # checked, so a carve-out widened to swallow the suite is seen.
    this_file = Path(__file__).resolve().relative_to(REPO_ROOT)
    result = _format_check(this_file.as_posix(), cwd=REPO_ROOT)
    assert result.returncode == 0, result.stdout
    assert NOTHING_FOUND not in result.stderr


def test_target_version_is_the_interpreter_ci_runs_under() -> None:
    # ci.yml sets up Python from .python-version; ruff.toml carries the same
    # number as `pyXY`. A bump that reaches one file and not the other has
    # the formatter checking against a target the tree does not ship on.
    pinned = PYTHON_VERSION_FILE.read_text(encoding="utf-8").strip()
    major, minor = pinned.split(".")[:2]
    config = tomllib.loads(RUFF_TOML.read_text(encoding="utf-8"))
    assert config["target-version"] == f"py{major}{minor}"
