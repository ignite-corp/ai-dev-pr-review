"""Execute the release guard against all three actual checkout declarations."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[3]
WORKFLOW_DIR = ROOT / ".github/workflows"
WORKFLOWS = (
    "base-ai-review-prepare.yml",
    "base-ai-review-single.yml",
    "base-ai-review-aggregate.yml",
)
PINS = ("v1.17.0", "v1.18.0", "v1.18.1")


def _checkout_ref(text: str) -> str:
    document = yaml.safe_load(text)
    (ref,) = [
        step["with"]["ref"]
        for job in document["jobs"].values()
        for step in job.get("steps", [])
        if step.get("with", {}).get("repository") == "ignite-corp/ai-dev-pr-review"
    ]
    assert isinstance(ref, str) and ref
    return ref


def _run_guard(
    tmp_path: Path,
    *,
    missing_tag: int | None = None,
    malformed: int | None = None,
    absent_file: bool = False,
    probe_failure: int | None = None,
) -> tuple[subprocess.CompletedProcess[str], str, list[str]]:
    files = tmp_path / ".github/workflows"
    files.mkdir(parents=True)
    for index, (name, pin) in enumerate(zip(WORKFLOWS, PINS, strict=True)):
        if absent_file and index == malformed:
            continue
        text = (WORKFLOW_DIR / name).read_text(encoding="utf-8")
        declared = f"ref: {_checkout_ref(text)}"
        assert text.count(declared) == 1
        text = text.replace(declared, f"ref: {pin}")
        if index == malformed:
            text = text.replace(f"ref: {pin}", "ref:")
        (files / name).write_text(text, encoding="utf-8")
    calls = tmp_path / "calls"
    calls.touch()
    git = tmp_path / "git"
    git.write_text(
        f"#!{sys.executable}\n"
        "import pathlib, sys\n"
        f"with pathlib.Path({str(calls)!r}).open('a') as stream:\n"
        "    stream.write(sys.argv[-1] + '\\n')\n"
        f"available = {json.dumps([p for i, p in enumerate(PINS) if i != missing_tag])}\n"
        f"failure = {probe_failure!r}\n"
        "sys.exit(failure if failure is not None else (2 if sys.argv[-1] not in available else 0))\n",
        encoding="utf-8",
    )
    git.chmod(0o700)
    output = tmp_path / "output"
    output.touch()
    workflow = yaml.safe_load((WORKFLOW_DIR / "self-review.yml").read_text())
    script = next(
        step["run"]
        for step in workflow["jobs"]["guard"]["steps"]
        if step.get("id") == "pin"
    )
    result = subprocess.run(
        ["bash", "-e", "-c", script],
        cwd=tmp_path,
        env={
            **os.environ,
            "PATH": f"{tmp_path}{os.pathsep}{os.environ['PATH']}",
            "GITHUB_OUTPUT": str(output),
        },
        text=True,
        capture_output=True,
        check=False,
    )
    return result, output.read_text(), calls.read_text().splitlines()


def test_all_actual_checkout_refs_exist_and_run_review(tmp_path: Path) -> None:
    result, output, calls = _run_guard(tmp_path)
    assert result.returncode == 0, result.stderr
    assert "all script checkout tags exist; running self-review" in result.stdout
    assert output == ""
    assert calls == list(PINS)


@pytest.mark.parametrize("missing", range(len(WORKFLOWS)), ids=WORKFLOWS)
def test_any_missing_checkout_tag_skips_with_its_pin_and_workflow(
    tmp_path: Path, missing: int
) -> None:
    result, output, calls = _run_guard(tmp_path, missing_tag=missing)
    assert result.returncode == 0, result.stderr
    assert "skip_reason=release pin bump:" in output
    assert (
        f"pinned tag {PINS[missing]} in {WORKFLOWS[missing]} does not exist yet"
        in output
    )
    assert "dispatch this workflow once the tag exists" in output
    assert "running self-review" not in result.stdout
    assert calls == list(PINS[: missing + 1])


@pytest.mark.parametrize("malformed", range(len(WORKFLOWS)), ids=WORKFLOWS)
def test_all_declarations_are_validated_before_any_missing_tag_skip(
    tmp_path: Path, malformed: int
) -> None:
    result, output, calls = _run_guard(tmp_path, missing_tag=0, malformed=malformed)
    assert result.returncode == 1
    assert (
        f"::error::cannot parse the script checkout pin from {WORKFLOWS[malformed]}"
        in result.stdout
    )
    assert output == ""
    assert calls == []


def test_missing_workflow_fails_instead_of_skipping_review(tmp_path: Path) -> None:
    result, output, calls = _run_guard(tmp_path, malformed=2, absent_file=True)
    assert result.returncode == 1
    assert (
        f"::error::cannot parse the script checkout pin from {WORKFLOWS[2]}"
        in result.stdout
    )
    assert output == ""
    assert calls == []


@pytest.mark.parametrize("exit_status", [128, 1])
def test_tag_probe_operational_failure_fails_without_skip_reason(
    tmp_path: Path, exit_status: int
) -> None:
    result, output, calls = _run_guard(tmp_path, probe_failure=exit_status)
    assert result.returncode == 1
    assert output == ""
    assert (
        f"::error::tag probe for {PINS[0]} in {WORKFLOWS[0]} failed (git exit {exit_status})"
        in result.stdout
    )
    assert "self-review skipped" not in result.stdout
    assert "running self-review" not in result.stdout
    assert calls == [PINS[0]]


def test_fixture_accepts_a_single_checkout_bumped_to_the_next_release(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    for name in (*WORKFLOWS, "self-review.yml"):
        shutil.copy(WORKFLOW_DIR / name, source / name)
    single = source / WORKFLOWS[1]
    text = single.read_text(encoding="utf-8")
    declared = f"ref: {_checkout_ref(text)}"
    assert text.count(declared) == 1
    single.write_text(text.replace(declared, "ref: v1.19.0"), encoding="utf-8")
    assert _checkout_ref(single.read_text(encoding="utf-8")) == "v1.19.0"
    monkeypatch.setitem(globals(), "WORKFLOW_DIR", source)
    result, output, calls = _run_guard(tmp_path / "run", missing_tag=1)
    assert result.returncode == 0, result.stderr
    assert f"pinned tag {PINS[1]} in {WORKFLOWS[1]} does not exist yet" in output
    assert calls == list(PINS[:2])
