"""The Python version has one home, `.github/scripts/.python-version`.

ci.yml provisions the interpreter from it. ruff and pyright cannot read it:
each takes its own copy in pyproject.toml, and neither can be dropped. Left
unset, ruff lints for every version and formats for 3.10 (measured on ruff
0.14.11: `linter.unresolved_target_version = none`,
`formatter.unresolved_target_version = 3.10`), and pyright assumes whatever
interpreter it finds -- 3.11 on one machine, 3.14 in CI -- so the same tree
would be checked against different rules on different machines. ruff would
also read `[project] requires-python`, but pyright does not (measured: it
still assumed the local interpreter), and this repository is not a package.
So the copies stay, and this module holds them to the source: three literals
that have to move together, with nothing failing when one is left behind,
are the drift class AT-2420 is about.

Two reads. The config read says the copies are spelled right; the tool read
says each tool actually resolved that version, which is what the copy is
for.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

from tests_support import ran

ROOT = Path(__file__).resolve().parents[3]
SOURCE = ROOT / ".github/scripts/.python-version"
PYPROJECT = ROOT / "pyproject.toml"
# What `pyright --verbose` prints for the version it checks against.
PYRIGHT_VERSION = re.compile(r"Python version: (\S+)")


def source_version() -> str:
    return SOURCE.read_text(encoding="utf-8").strip()


def expected_copies(version: str) -> dict[str, str]:
    """What each tool's copy must say for `version`: ruff spells 3.14 `py314`."""
    return {
        "tool.ruff.target-version": "py" + version.replace(".", ""),
        "tool.pyright.pythonVersion": version,
    }


def actual_copies(pyproject_text: str) -> dict[str, str]:
    data = tomllib.loads(pyproject_text)
    return {
        "tool.ruff.target-version": data["tool"]["ruff"]["target-version"],
        "tool.pyright.pythonVersion": data["tool"]["pyright"]["pythonVersion"],
    }


def drifted(pyproject_text: str, version: str) -> dict[str, str]:
    """The copies in `pyproject_text` that do not say `version`."""
    want = expected_copies(version)
    return {k: v for k, v in actual_copies(pyproject_text).items() if v != want[k]}


# -------------------------------------------------------------- the copies


def test_every_copy_says_what_the_source_says() -> None:
    assert drifted(PYPROJECT.read_text(encoding="utf-8"), source_version()) == {}


def test_a_copy_left_behind_is_named() -> None:
    """Inversion: move one copy, and that copy alone is reported."""
    text = PYPROJECT.read_text(encoding="utf-8")
    version = source_version()
    stale_ruff = text.replace(
        f'target-version = "py{version.replace(".", "")}"', 'target-version = "py39"'
    )
    assert drifted(stale_ruff, version) == {"tool.ruff.target-version": "py39"}
    stale_pyright = text.replace(
        f'pythonVersion = "{version}"', 'pythonVersion = "3.9"'
    )
    assert drifted(stale_pyright, version) == {"tool.pyright.pythonVersion": "3.9"}
    # And moving the source reports both copies, which is the bump that
    # forgot pyproject.toml.
    assert set(drifted(text, "3.99")) == set(expected_copies("3.99"))


# --------------------------------------------------------------- the tools


def test_ruff_resolves_the_source_version() -> None:
    """Both of ruff's resolved targets, read from the tool, not the file."""
    version = source_version()
    settings = ran(
        "ruff",
        ["check", "--show-settings", str(SOURCE.parent / "review_pr_local.py")],
        cwd=ROOT,
    ).stdout
    for key in (
        "linter.unresolved_target_version",
        "formatter.unresolved_target_version",
    ):
        assert f"{key} = {version}\n" in settings, key


def test_pyright_checks_against_the_source_version() -> None:
    """Exact, because an unset pythonVersion prints the local interpreter's
    full version (`3.11.9.final.0` where this was written), not `3.14`.

    One file is named so pyright resolves this repository's config and
    prints its banner without re-analyzing the whole tree -- the CI gate
    already does that once.
    """
    completed = ran(
        "pyright", ["--verbose", str(SOURCE.parent / "review_pr_local.py")], cwd=ROOT
    )
    found = PYRIGHT_VERSION.search(completed.stdout)
    assert found, "pyright --verbose did not print the Python version it assumed"
    assert found.group(1) == source_version()
