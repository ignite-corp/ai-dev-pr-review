"""Positive control for the suite-wide live-`gh` ban in conftest.py.

The control lives in its own module, not in test_aggregate_reviews.py where
the fixture used to sit, so it also checks reach: before the fixture moved
to conftest.py this module was outside it and every assertion below failed.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from tests_support import LiveGhCalled


def test_no_live_gh_fixture_is_armed() -> None:
    """Positive control for ``_no_live_gh``.

    Without this, a green suite is indistinguishable from a fixture that
    silently stopped matching -- a rename of the binary, a change to the
    argv shape, or an autouse registration that no longer applies.
    """
    with pytest.raises(LiveGhCalled, match="live GitHub API"):
        subprocess.run(["gh", "pr", "view", "1"])
    # A string command is the same call in the other spelling, and the
    # naive normalisation made the whole string argv[0], so basename()
    # compared "gh pr view 1" against "gh" and let it through.
    with pytest.raises(LiveGhCalled, match="live GitHub API"):
        subprocess.run("gh pr view 1")
    # An absolute path is still gh.
    with pytest.raises(LiveGhCalled, match="live GitHub API"):
        subprocess.run(["/usr/bin/gh", "pr", "view", "1"])
    # ...and it must not break the non-gh shell-outs other tests rely on,
    # in either spelling.
    done = subprocess.run(["echo", "ok"], capture_output=True, text=True)
    assert done.stdout.strip() == "ok"
    done = subprocess.run("echo ok", shell=True, capture_output=True, text=True)
    assert done.stdout.strip() == "ok"


@pytest.mark.parametrize(
    "cmd",
    [[b"gh", b"pr", b"view", b"1"], b"gh pr view 1"],
    ids=["argv", "string"],
)
def test_no_live_gh_catches_bytes(
    cmd: list[bytes] | bytes, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``subprocess`` accepts bytes in both spellings, and so must the guard.

    ``str(b"gh")`` is ``"b'gh'"``, which no basename comparison matches. A
    fake `gh` sits first on PATH so a regression touches a marker file
    rather than the live API.
    """
    marker = tmp_path / "reached"
    fake = tmp_path / "gh"
    fake.write_text(f"#!/bin/sh\ntouch {marker}\n")
    fake.chmod(0o755)
    monkeypatch.setenv("PATH", f"{tmp_path}{os.pathsep}{os.environ['PATH']}")
    with pytest.raises(LiveGhCalled, match="live GitHub API"):
        subprocess.run(cmd)
    assert not marker.exists()


def test_no_live_gh_is_not_swallowed_by_except_exception() -> None:
    """A caller's ``except Exception`` must not absorb the guard.

    Scripts the suite drives -- review_pr_local.py among them -- catch
    ``Exception`` around paths that can reach `gh` and turn it into a
    degraded result. A guard that raised an ``Exception`` subclass would
    fire there and the test would still pass.
    """

    def swallowing_caller() -> str:
        try:
            subprocess.run(["gh", "pr", "view", "1"])
        except Exception:  # noqa: BLE001 -- the shape under test
            return "swallowed"
        return "ran"

    with pytest.raises(LiveGhCalled, match="live GitHub API") as raised:
        swallowing_caller()
    assert not isinstance(raised.value, Exception)
