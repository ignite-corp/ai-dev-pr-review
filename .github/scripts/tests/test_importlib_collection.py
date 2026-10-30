"""Shared test helpers must be importable without pytest prepending tests/."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


def test_complete_suite_collects_with_importlib() -> None:
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "--collect-only",
            "-p",
            "no:cacheprovider",
            "--import-mode=importlib",
            "-q",
        ],
        cwd=Path(__file__).resolve().parents[3],
        env=dict(os.environ, PYTEST_ADDOPTS=""),
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert any("::" in line for line in result.stdout.splitlines()), result.stdout
