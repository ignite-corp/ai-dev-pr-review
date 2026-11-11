"""Every checkout of this repository carries a token (AT-3116).

Inside a reusable workflow, `actions/checkout` with no `token:` uses the
CALLER's `GITHUB_TOKEN`, which can read the caller and nothing else private.
The scripts checkouts in prepare, single and aggregate reach this repository
that way, so they work only while it is public: the day it goes private,
every consumer review fails at that step.

So each one is handed a reviewer-App token minted for this repository alone,
with `contents: read`, and falls back to `github.token` when the key is not
forwarded -- a release cut while the repository is still public then changes
nothing for a consumer that does not pass the key. A checkout of this
repository added later without a token works today and breaks only after the
switch, which is why every one is enumerated here rather than the three
known ones.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
import yaml

_GITHUB = Path(__file__).resolve().parents[2]
_WORKFLOWS = _GITHUB / "workflows"
_ACTIONS = _GITHUB / "actions"
_CONTROL_REPO = "ignite-corp/ai-dev-pr-review"
_MINT_ID = "scripts_token"
_KEY_ID = "scripts_key"
_TOKEN_EXPR = "${{ steps.scripts_token.outputs.token || github.token }}"
_MINT_PIN = "actions/create-github-app-token@bcd2ba49218906704ab6c1aa796996da409d3eb1"
_KEY_GATE = "vars.REVIEWER_APP_ID != '' && steps.scripts_key.outputs.present == 'true'"
_SKIP_GATE = "inputs.skip_reason == ''"
_AGGREGATE_LABEL = "workflows/base-ai-review-aggregate.yml:"


def _step_lists(path: Path) -> list[tuple[str, list[dict[str, Any]]]]:
    doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if path.is_relative_to(_ACTIONS):
        return [("runs", (doc.get("runs") or {}).get("steps") or [])]
    return [
        (job_id, job.get("steps") or [])
        for job_id, job in (doc.get("jobs") or {}).items()
    ]


def _scanned_files() -> list[Path]:
    return sorted(_WORKFLOWS.glob("*.y*ml")) + sorted(_ACTIONS.glob("**/action.y*ml"))


def _control_checkouts() -> list[tuple[str, list[dict[str, Any]], int]]:
    """(label, the job's steps, index of the checkout) per control checkout."""
    found = []
    for path in _scanned_files():
        for owner, steps in _step_lists(path):
            for index, step in enumerate(steps):
                if not str(step.get("uses", "")).startswith("actions/checkout@"):
                    continue
                repository = str((step.get("with") or {}).get("repository", ""))
                # Case-insensitive: GitHub resolves the name either way.
                if repository.lower() == _CONTROL_REPO:
                    label = f"{path.relative_to(_GITHUB)}:{owner}[{index}]"
                    found.append((label, steps, index))
    return found


_CHECKOUTS = _control_checkouts()
_IDS = [label for label, _, _ in _CHECKOUTS]


def _by_id(steps: list[dict[str, Any]], step_id: str) -> tuple[int, dict[str, Any]]:
    for index, step in enumerate(steps):
        if step.get("id") == step_id:
            return index, step
    raise AssertionError(f"no step with id {step_id!r} in this job")


def _clauses(condition: object) -> set[str]:
    return {c.strip() for c in re.split(r"&&", str(condition or "")) if c.strip()}


def test_the_scan_finds_the_scripts_checkouts() -> None:
    # Positive control: a glob or match change that finds nothing would make
    # every assertion below vacuous.
    files = {label.split(":")[0] for label in _IDS}
    assert {
        "workflows/base-ai-review-prepare.yml",
        "workflows/base-ai-review-single.yml",
        "workflows/base-ai-review-aggregate.yml",
    } <= files


@pytest.mark.parametrize("checkout", _CHECKOUTS, ids=_IDS)
def test_control_repo_checkout_carries_a_token(
    checkout: tuple[str, list[dict[str, Any]], int],
) -> None:
    label, steps, index = checkout
    with_ = steps[index].get("with") or {}
    assert with_.get("token"), (
        f"{label} checks out {_CONTROL_REPO} with the caller's github.token, "
        "which cannot read it once it is private; pass the scripts App token"
    )
    assert with_["token"] == _TOKEN_EXPR, f"{label} token is {with_['token']!r}"
    assert with_.get("persist-credentials") is False, f"{label} persists its token"


@pytest.mark.parametrize("checkout", _CHECKOUTS, ids=_IDS)
def test_the_token_is_minted_for_this_repository_alone(
    checkout: tuple[str, list[dict[str, Any]], int],
) -> None:
    label, steps, index = checkout
    mint_at, mint = _by_id(steps, _MINT_ID)
    key_at, _ = _by_id(steps, _KEY_ID)
    assert key_at < mint_at < index, f"{label}: key check, mint, checkout out of order"
    assert mint["uses"] == _MINT_PIN
    inputs = mint["with"]
    assert inputs["owner"] == "ignite-corp"
    assert inputs["repositories"] == "ai-dev-pr-review"
    permissions = {k: v for k, v in inputs.items() if k.startswith("permission-")}
    assert permissions == {"permission-contents": "read"}, (
        f"{label}: the scripts token reads one repository and does nothing else"
    )


@pytest.mark.parametrize("checkout", _CHECKOUTS, ids=_IDS)
def test_the_mint_runs_only_when_the_checkout_does_and_the_key_is_there(
    checkout: tuple[str, list[dict[str, Any]], int],
) -> None:
    # Without the key the mint would fail on `private-key`; skipped, the
    # checkout falls back to github.token, which still reads a public tree.
    label, steps, index = checkout
    _, mint = _by_id(steps, _MINT_ID)
    _, key = _by_id(steps, _KEY_ID)
    checkout_gate = _clauses(steps[index].get("if"))
    assert _clauses(mint.get("if")) == checkout_gate | _clauses(_KEY_GATE), label
    assert _clauses(key.get("if")) == checkout_gate, label
    if label.startswith(_AGGREGATE_LABEL):
        assert checkout_gate == {_SKIP_GATE}


def test_the_scan_finds_the_aggregate_checkout() -> None:
    # The skip_reason branch above keys on the exact file; a rename must
    # fail here, not silently stop checking the gate.
    assert any(label.startswith(_AGGREGATE_LABEL) for label in _IDS)


def test_the_key_check_and_mint_are_the_same_in_every_job() -> None:
    def shape(steps: list[dict[str, Any]], step_id: str) -> dict[str, Any]:
        step = dict(_by_id(steps, step_id)[1])
        step.pop("if", None)  # differs by the checkout's own gate, held above
        return step

    shapes = {
        label: (shape(steps, _KEY_ID), shape(steps, _MINT_ID))
        for label, steps, _ in _CHECKOUTS
    }
    first = next(iter(shapes.values()))
    assert all(s == first for s in shapes.values()), shapes.keys()
    # One bit, not the PEM: only the mint step reads the secret itself.
    assert first[0]["env"] == {
        "REVIEWER_APP_KEY_PRESENT": "${{ secrets.REVIEWER_APP_PRIVATE_KEY != '' }}"
    }
