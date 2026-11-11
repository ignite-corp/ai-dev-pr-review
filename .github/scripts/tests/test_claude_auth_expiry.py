"""The Claude review honours an expired CLAUDE_FORCE_API_UNTIL at start (AT-3439).

The hourly claude-auth-restore cron is not the only way back to OAuth: the
step that resolves force_api treats an UNTIL in the past as no force, so the
first review after the reset uses the subscription. CLAUDE_FORCE_API=true
with no UNTIL still forces the API path, as an operator's manual pin.
"""

from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path
from typing import Any

import pytest
import yaml

_SINGLE = Path(__file__).resolve().parents[2] / "workflows/base-ai-review-single.yml"
_STEP = "Resolve Claude auth path"


def _steps() -> list[dict[str, Any]]:
    return yaml.safe_load(_SINGLE.read_text())["jobs"]["review"]["steps"]


def _step(name: str) -> dict[str, Any]:
    return next(s for s in _steps() if s.get("name") == name)


def _resolve(tmp_path: Path, force: str, until: str) -> str:
    output = tmp_path / "output"
    output.write_text("")
    env = {
        "PATH": os.environ["PATH"],
        "FORCE_API": force,
        "FORCE_API_UNTIL": until,
        "GITHUB_OUTPUT": str(output),
    }
    result = subprocess.run(
        ["bash", "-e", "-c", _step(_STEP)["run"]],
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    lines = [
        ln for ln in output.read_text().splitlines() if ln.startswith("force_api=")
    ]
    assert len(lines) == 1, output.read_text()
    return lines[0].removeprefix("force_api=")


def _now() -> int:
    return int(time.time())


@pytest.mark.parametrize(
    ("force", "until", "expected"),
    [
        ("true", "", "true"),  # manual pin: no expiry
        ("true", "{future}", "true"),
        ("true", "{past}", "false"),  # expired: OAuth without the cron
        ("true", "not-an-int", "false"),  # restore deletes this too
        ("true", "99999999999999999999999", "false"),  # no shell overflow
        ("true", " {future}\n", "true"),  # surrounding whitespace
        ("true", "+{future}", "false"),  # as _epoch: plain digits only
        ("true", "{future_split}", "false"),  # inner whitespace is not stripped
        ("true", "{future}\u00a0", "false"),  # as _epoch: ASCII whitespace only
        ("false", "{future}", "false"),
        ("false", "", "false"),
    ],
)
def test_force_api_resolution(tmp_path: Path, force: str, until: str, expected: str):
    future = str(_now() + 3600)
    until = until.format(
        future=future, past=_now() - 60, future_split=future[:5] + " " + future[5:]
    )
    assert _resolve(tmp_path, force, until) == expected


def test_resolution_reads_both_org_vars():
    step = _step(_STEP)
    assert step["env"]["FORCE_API"] == "${{ vars.CLAUDE_FORCE_API == 'true' }}"
    assert step["env"]["FORCE_API_UNTIL"] == "${{ vars.CLAUDE_FORCE_API_UNTIL }}"
    assert step["if"] == "inputs.reviewer == 'claude'"


def test_claude_review_takes_force_api_from_the_resolution():
    names = [s.get("name") for s in _steps()]
    assert names.index(_STEP) < names.index("Run Claude review")
    step_id = _step(_STEP)["id"]
    forwarded = _step("Run Claude review")["with"]["force_api"]
    assert forwarded == f"${{{{ steps.{step_id}.outputs.force_api == 'true' }}}}"
