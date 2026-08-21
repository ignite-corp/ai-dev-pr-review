"""Tests for ruleset_drift_paths: key-path drift reporting for ruleset audits.

Test-module hygiene: all imports belong at the top of this file per PEP 8 --
do not add `import foo` statements inside test function bodies.

Every document below is synthetic. The audit runs in a public repo, so the
safety property under test is that only schema KEYS ever reach the output.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

# Add parent dir to path for import
SCRIPT_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(SCRIPT_DIR))

from ruleset_drift_paths import drifted_paths, main


def _ruleset(*, enforcement: str = "active", extra_param: dict | None = None) -> dict:
    """A minimal ruleset-shaped document with synthetic values only."""
    params: dict = {"required_approving_review_count": 1}
    if extra_param:
        params.update(extra_param)
    return {
        "name": "ruleset-one",
        "target": "branch",
        "enforcement": enforcement,
        "rules": [
            {"type": "pull_request", "parameters": params},
            {
                "type": "required_status_checks",
                "parameters": {"required_status_checks": [{"context": "check-one", "integration_id": 1}]},
            },
        ],
        "bypass_actors": [{"actor_id": 1, "bypass_mode": "always"}],
    }


# ---------------------------------------------------------------------------
# drifted_paths
# ---------------------------------------------------------------------------


def test_identical_documents_report_no_paths() -> None:
    assert drifted_paths(_ruleset(), _ruleset()) == []


def test_changed_scalar_reports_its_path() -> None:
    assert drifted_paths(_ruleset(), _ruleset(enforcement="evaluate")) == ["enforcement"]


def test_key_present_on_one_side_only_is_reported() -> None:
    stored = {"name": "ruleset-one", "target": "branch"}
    live = {"name": "ruleset-one"}
    assert drifted_paths(stored, live) == ["target"]
    assert drifted_paths(live, stored) == ["target"]


def test_nested_change_inside_array_element_uses_normalized_path() -> None:
    # The real-world case: a new rule parameter backfilled into every ruleset.
    stored = _ruleset()
    live = _ruleset(extra_param={"require_extra_approval": False})
    assert drifted_paths(stored, live) == ["rules[].parameters.require_extra_approval"]


def test_added_array_element_reports_element_paths() -> None:
    stored = _ruleset()
    live = json.loads(json.dumps(stored))
    live["bypass_actors"].append({"actor_id": 2, "bypass_mode": "pull_request"})
    assert drifted_paths(stored, live) == [
        "bypass_actors[].actor_id",
        "bypass_actors[].bypass_mode",
    ]


def test_array_reordering_is_not_drift() -> None:
    stored = _ruleset()
    live = json.loads(json.dumps(stored))
    live["rules"].reverse()
    assert drifted_paths(stored, live) == []


def test_known_limitation_correlated_swap_between_elements_is_invisible() -> None:
    # KNOWN LIMITATION, pinned so it cannot regress silently: values are bucketed
    # per normalized path, so two array elements swapping a field leave both
    # multisets unchanged and no path is reported. Here two required checks trade
    # their integration_id -- a real AT-1270-class change. The audit does not rely
    # on this function to detect drift (its own diff does that), which is why the
    # caller must not report an empty result as a benign reordering.
    stored = {
        "rules": [
            {"context": "check-one", "integration_id": 1},
            {"context": "check-two", "integration_id": 2},
        ]
    }
    live = {
        "rules": [
            {"context": "check-one", "integration_id": 2},
            {"context": "check-two", "integration_id": 1},
        ]
    }
    assert drifted_paths(stored, live) == []


def test_output_is_sorted_and_deduplicated() -> None:
    stored = {"b": 1, "a": 1, "items": [{"x": 1}, {"x": 2}]}
    live = {"b": 2, "a": 2, "items": [{"x": 3}, {"x": 4}]}
    assert drifted_paths(stored, live) == ["a", "b", "items[].x"]


def test_no_document_value_ever_appears_in_the_output() -> None:
    # Hard safety requirement: paths carry schema keys, never ruleset content.
    stored = {
        "name": "stored-name-value",
        "rules": [{"type": "stored-type-value", "parameters": {"context": "stored-context-value"}}],
    }
    live = {
        "name": "live-name-value",
        "rules": [{"type": "live-type-value", "parameters": {"context": "live-context-value"}}],
        "enforcement": "live-enforcement-value",
    }
    output = "\n".join(drifted_paths(stored, live))
    assert "-value" not in output
    assert output == "enforcement\nname\nrules[].parameters.context\nrules[].type"


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------


def test_main_prints_paths_for_two_json_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    stored_path = tmp_path / "stored.json"
    live_path = tmp_path / "live.json"
    stored_path.write_text(json.dumps(_ruleset()), encoding="utf-8")
    live_path.write_text(json.dumps(_ruleset(enforcement="disabled")), encoding="utf-8")

    monkeypatch.setattr("sys.argv", ["ruleset_drift_paths.py", str(stored_path), str(live_path)])
    assert main() == 0
    assert capsys.readouterr().out == "enforcement\n"


def test_main_rejects_wrong_argument_count(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("sys.argv", ["ruleset_drift_paths.py"])
    assert main() == 2
