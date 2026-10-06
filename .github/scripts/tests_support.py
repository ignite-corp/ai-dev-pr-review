"""Helpers for the test modules that run a tool rather than import a module.

Here rather than in `tests/conftest.py` because `pythonpath` reaches
`.github/scripts` and not `.github/scripts/tests`: importing a name out of
conftest.py works only under pytest's default `prepend` import mode, and
breaks under `--import-mode=importlib` or once a `tests/__init__.py` exists
(both measured). conftest.py is pytest's fixture and hook file, not an
importable helper module.
"""

from __future__ import annotations

import shutil
import subprocess
from typing import Any

import pytest

# Exit codes for a run that happened: clean, and findings. Anything else is
# the tool refusing to run, and its reason is on stderr, not in its output.
RAN = (0, 1)


def tool(name: str) -> str:
    """The path of `name`, or a loud failure naming what installs it."""
    binary = shutil.which(name)
    if binary is None:
        pytest.fail(
            f"{name} is not on PATH; pip install -r .github/scripts/requirements-dev.txt"
        )
    return binary


def ran(name: str, argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
    """Run `name` and fail loudly, with its own stderr, if it refused to run."""
    completed = subprocess.run(
        [tool(name), *argv], capture_output=True, text=True, check=False, **kwargs
    )
    if completed.returncode not in RAN:
        pytest.fail(f"{name} exited {completed.returncode}: {completed.stderr}")
    return completed
