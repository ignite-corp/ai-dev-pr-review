"""The verdict shape belongs to the aggregate, so it is written once.

The condition on extracting it was that the check be shown failing on a
baseline first. `test_neither_shim_builds_a_failed_verdict_itself` is that
check, and the baseline it fails on is the discarded pair of shims, where
`error_verdict` had become byte-identical in both files.
"""

from __future__ import annotations

import ast
import json
import signal
import sys
from pathlib import Path

import pytest

SCRIPT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SCRIPT_DIR))

from local_reviewer_support import (  # noqa: E402
    ERROR_CRASHED,
    EXIT_NOT_INSTALLED,
    EXIT_SPAWN_FAILED,
    EXIT_TIMED_OUT,
    error_verdict,
    exit_reason,
    guarded_main,
    spawn_exit_reason,
)

SHIMS = ("review_claude_local.py", "review_codex_local.py")


def test_the_envelope_carries_every_key_the_aggregate_validates():
    """aggregate_reviews rejects a payload missing any of these."""
    verdict = error_verdict("summary", "kind", "detail")
    assert set(verdict) == {
        "summary", "status", "early_exit", "issues", "error", "error_detail"
    }
    assert verdict["status"] == "failed"
    assert verdict["early_exit"] is False
    assert verdict["issues"] == []


def test_a_detail_is_appended_and_an_empty_one_is_not():
    assert error_verdict("s", "k", "d")["summary"] == "s -- d"
    assert error_verdict("s", "k", "")["summary"] == "s"


@pytest.mark.parametrize(
    "code, fragment",
    [
        (EXIT_NOT_INSTALLED, "not installed"),
        (EXIT_TIMED_OUT, "within 90s"),
        (EXIT_SPAWN_FAILED, "could not be started"),
    ],
)
def test_each_spawn_failure_keeps_its_own_words(code, fragment):
    assert fragment in spawn_exit_reason(code, 90)


def test_a_code_this_module_does_not_own_gets_no_invented_reason():
    """Empty, so a shim adding a code of its own must say what it means
    rather than have this function speak for it."""
    assert spawn_exit_reason(3, 90) == ""
    assert spawn_exit_reason(-99999, 90) == ""


def test_sentinels_cannot_be_a_signal_death():
    """The comment's claim, enforced instead of asserted.

    It used to read "Negative so they cannot collide with a real CLI exit
    status", and nothing held it: CompletedProcess.returncode is `-N` for a
    child killed by signal N, so -1, -2 and -3 WERE SIGHUP, SIGINT and
    SIGQUIT. Baseline (688cd0a): spawn_exit_reason(-signal.SIGHUP, 90)
    returned "the CLI is not installed or not on PATH" and that is what the
    aggregate rendered as the reason a review had failed.

    Enumerated over every returncode a signal death can produce rather than
    over the three spellings that were wrong, because the next signal added
    to the platform is the one a three-case check would not see.
    """
    sentinels = (EXIT_NOT_INSTALLED, EXIT_TIMED_OUT, EXIT_SPAWN_FAILED)
    assert len(set(sentinels)) == len(sentinels)
    for signum in range(1, signal.NSIG):
        assert -signum not in sentinels, signal.Signals(signum).name
        # And the vocabulary itself refuses to speak for one.
        assert spawn_exit_reason(-signum, 90) == "", signal.Signals(signum).name


def test_the_fallback_wording_has_one_home_both_shims_read():
    """The aggregate reads one error_detail whoever wrote it.

    The wording was computed identically in both shims, so the agreement
    was a coincidence a one-line edit could end. Here it is the function,
    and the shims are checked for the copy rather than for the call: a
    second `or f"CLI exited ..."` anywhere is the drift itself.
    """
    assert exit_reason(3, 90) == "CLI exited 3"
    assert exit_reason(EXIT_TIMED_OUT, 90) == spawn_exit_reason(EXIT_TIMED_OUT, 90)
    for shim in SHIMS:
        source = (SCRIPT_DIR / shim).read_text(encoding="utf-8")
        assert "CLI exited {exit_code}" not in source, shim
        assert "exit_reason(" in source, shim


def test_a_raising_reviewer_still_leaves_a_verdict(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)

    def explode():
        raise RuntimeError("boom")

    guarded_main(explode, "Codex", "review-codex.json")

    payload = json.loads((tmp_path / "review-codex.json").read_text())
    assert payload["status"] == "failed"
    assert payload["error"] == ERROR_CRASHED
    assert "boom" in payload["error_detail"]
    # The traceback still reaches the log the driver captures.
    assert "RuntimeError" in capsys.readouterr().err


def test_a_reviewer_that_succeeds_is_not_overwritten(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    target = tmp_path / "review-claude.json"

    guarded_main(lambda: target.write_text('{"status": "ok"}'), "Claude", target.name)

    assert json.loads(target.read_text()) == {"status": "ok"}


def test_keyboard_interrupt_still_stops_the_reviewer(tmp_path, monkeypatch):
    """BaseException is deliberately not caught."""
    monkeypatch.chdir(tmp_path)

    def interrupted():
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        guarded_main(interrupted, "Claude", "review-claude.json")
    assert not (tmp_path / "review-claude.json").exists()


def failed_verdict_builders(source: str) -> list[str]:
    """Functions that construct a dict literal with status "failed"."""
    found = []
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef):
            continue
        for inner in ast.walk(node):
            if not isinstance(inner, ast.Dict):
                continue
            for key, value in zip(inner.keys, inner.values):
                if (
                    isinstance(key, ast.Constant)
                    and key.value == "status"
                    and isinstance(value, ast.Constant)
                    and value.value == "failed"
                ):
                    found.append(node.name)
    return found


@pytest.mark.parametrize("shim", SHIMS)
def test_neither_shim_builds_a_failed_verdict_itself(shim):
    """The aggregate's schema has ONE home.

    Shown failing on the baseline before it was believed: against the
    discarded shims on origin/task/local-review-driver this reports
    `error_verdict` for both files, which is the duplication a reviewer
    measured growing rather than shrinking -- and the reason the previous
    round's timeout fix had to be applied twice.
    """
    source = (SCRIPT_DIR / shim).read_text(encoding="utf-8")
    assert failed_verdict_builders(source) == []


def test_the_baseline_detector_actually_detects():
    """A check nobody has seen fail is not a check."""
    baseline = '''
def error_verdict(summary, kind, detail):
    return {"summary": summary, "status": "failed", "issues": []}
'''
    assert failed_verdict_builders(baseline) == ["error_verdict"]


@pytest.mark.parametrize("shim", SHIMS)
def test_every_shim_routes_its_entry_point_through_the_guard(shim):
    """The guarantee is structural, not a habit each shim re-establishes."""
    source = (SCRIPT_DIR / shim).read_text(encoding="utf-8")
    assert "guarded_main(" in source
    assert "if __name__ ==" in source
