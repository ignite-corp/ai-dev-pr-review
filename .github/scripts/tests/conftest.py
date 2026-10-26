"""Suite-wide fixtures for the scripts tests.

Holds the live-`gh` ban. It started life module-local in
test_aggregate_reviews.py, where its own docstring claimed "every main()
path in the aggregate is driven from this file" -- which was false:
test_aggregate_reviewer_roster.py imports the same ``main`` and drives it
at nine call sites, none of them covered. A deliberate unpatched driver
there reached `gh api graphql` on the live API, so the guard that
announced itself as a choke point was describing a reach it did not
have. Moving it here is what makes the claim true rather than narrowing
the claim to fit.

Its positive control is test_no_live_gh.py.
"""

from __future__ import annotations

import os
import shlex
import subprocess
from typing import Any

import pytest

from tests_support import LiveGhCalled


@pytest.fixture(autouse=True)
def _no_live_gh(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail any test in this directory that calls ``subprocess.run`` on ``gh``.

    Several modules drive the aggregate's ``main()``, and every such path
    can reach a `gh` call. A test that forgets to patch
    ``subprocess.run`` does not fail -- it quietly hits the live GitHub
    API, which is how ``test_main_hands_both_readers_the_same_count`` came
    to run `gh pr view 42 --json labels --repo owner/repo` against the
    real API on every CI run, and to error outright on a machine with no
    `gh` on PATH.

    A sweep someone remembers to run is not a check, so the ban lives here
    rather than in a script. Tests that patch ``subprocess.run`` in the
    module under test (``aggregate_reviews.subprocess.run`` and the like)
    themselves override this for their own duration, which is the
    intended way to exercise a `gh` path. Non-`gh` shell-outs are delegated
    untouched -- several tests in the suite legitimately drive `jq`, `bash`
    and `python3`.

    What it covers, exactly, because a guard whose docstring outruns its
    reach is the same defect one level up: calls through
    ``subprocess.run``, in either spelling -- an argv sequence or a single
    command string, which is split rather than compared whole -- and
    ``gh`` named by bare name or by absolute path, as the first word of the
    command, given as ``str``, ``bytes`` or a path object.
    ``subprocess.check_output`` routes through ``run`` and so is covered
    too.

    It does NOT cover:

    - ``subprocess.call`` and ``subprocess.check_call``, which start
      ``Popen`` directly rather than going through ``run``;
    - a command string where `gh` is not the first word -- a pipeline
      (``"echo x | gh ..."``), an environment prefix
      (``"GH_TOKEN=x gh ..."``) or a compound (``"cd d && gh ..."``);
    - a module that holds its own reference to ``run`` taken before this
      fixture applies;
    - a subprocess that shells out to `gh` from inside a script this suite
      launches;
    - ``subprocess.Popen`` used directly. This one is reachable today:
      ``run_reviewer`` in review_pr_local.py calls ``subprocess.Popen``
      directly to launch a reviewer script, and test_review_pr_local.py and
      test_review_local_shims.py drive it. That call starts Python, not
      `gh`, so nothing escapes through it now, but a `gh` call moved onto
      ``Popen`` anywhere in the scripts would pass this guard unseen.
    """
    real = subprocess.run

    def text(part: Any) -> str:
        # subprocess takes bytes and path objects as well as str, and
        # str(b"gh") is "b'gh'", which no basename comparison matches.
        if isinstance(part, (str, bytes, os.PathLike)):
            return os.fsdecode(part)
        return str(part)

    def guard(cmd: Any, *args: Any, **kwargs: Any) -> Any:
        if isinstance(cmd, (list, tuple)):
            argv = [text(c) for c in cmd]
        else:
            # A string command is the same call in the other spelling.
            # Comparing it whole made basename() see "gh pr view ..." and
            # let it past; splitting is what makes the two spellings one
            # check.
            try:
                argv = shlex.split(text(cmd))
            except ValueError:
                argv = [text(cmd)]
        if argv and os.path.basename(argv[0]) == "gh":
            raise LiveGhCalled(
                "test reached the live GitHub API: "
                + " ".join(argv)
                + " -- patch subprocess.run in the module under test"
            )
        return real(cmd, *args, **kwargs)

    monkeypatch.setattr(subprocess, "run", guard)
