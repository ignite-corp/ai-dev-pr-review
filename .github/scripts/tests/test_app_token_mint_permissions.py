"""Every App token minted in a workflow names its permissions (AT-3394).

`actions/create-github-app-token` given no `permission-*` input asks for
every permission the installation holds. A permission later added to the
App then reaches that token with no change in this repository, which is how
the reviewer App's new `actions: write` and `actions_variables: write` came to
sit in the token every same-repository consumer run mints for its approval.

Naming one permission is not enough on the v1.12.0 pin: its bundle reads
`INPUT_PERMISSION_*`, while the runner exports a `permission-contents` input
as `INPUT_PERMISSION-CONTENTS`, so every permission input is dropped and the
token is again unnarrowed. So the pin is held to a list of releases read
and confirmed to honour those inputs, and a pin bump has to confirm it again.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

_GITHUB = Path(__file__).resolve().parents[2]
_WORKFLOWS = _GITHUB / "workflows"
_ACTIONS = _GITHUB / "actions"
_ACTION = "actions/create-github-app-token@"
# Releases whose dist/main.cjs reads `INPUT_PERMISSION-*` and so applies the
# permission-* inputs. v3.2.0, read at this SHA for AT-3394; v1.12.0
# (d72941d) reads `INPUT_PERMISSION_*` and is not here.
_PERMISSION_AWARE_PINS = frozenset({"bcd2ba49218906704ab6c1aa796996da409d3eb1"})

# The fleet-administration App's mints, keyed by (file, step id). Out of
# AT-3394's scope, which is the review-pipeline App, whose key reaches every
# consumer run; the fleet App's key reaches only this repository's
# scheduled and dispatched jobs. Listed by step rather than by App so a new
# fleet mint still has to name its permissions.
_UNNARROWED: dict[tuple[str, str], str] = {
    ("consumer-health.yml", "app-token"): "fleet App, out of AT-3394's scope",
    ("ruleset-audit.yml", "app-token"): "fleet App, out of AT-3394's scope",
    ("ruleset-sync.yml", "app-token"): "fleet App, out of AT-3394's scope",
}


def _step_lists(path: Path) -> list[tuple[str, list[dict[str, Any]]]]:
    doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if path.is_relative_to(_ACTIONS):
        # A composite action's steps sit under `runs`, not under a job.
        return [("runs", (doc.get("runs") or {}).get("steps") or [])]
    return [
        (job_id, job.get("steps") or [])
        for job_id, job in (doc.get("jobs") or {}).items()
    ]


def _scanned_files() -> list[Path]:
    return sorted(_WORKFLOWS.glob("*.y*ml")) + sorted(_ACTIONS.glob("**/action.y*ml"))


def _label(path: Path) -> str:
    # Workflows by file name, as the exemptions key them; a composite action
    # by its path, since every one of them is called action.yml.
    return path.name if path.parent == _WORKFLOWS else str(path.relative_to(_GITHUB))


def _mints() -> list[tuple[str, str, dict[str, Any]]]:
    found: list[tuple[str, str, dict[str, Any]]] = []
    for path in _scanned_files():
        for owner, steps in _step_lists(path):
            for index, step in enumerate(steps):
                if str(step.get("uses", "")).startswith(_ACTION):
                    # Positional when there is no id, so two unnamed mints
                    # never share a key or an exemption.
                    key = str(step.get("id") or f"{owner}[{index}]")
                    found.append((_label(path), key, step))
    return found


_MINTS = _mints()


def test_the_scan_finds_the_mints() -> None:
    # Positive control: a glob or key change that finds nothing would make
    # every assertion below vacuous.
    names = {(f, k) for f, k, _ in _MINTS}
    assert ("base-ai-review-aggregate.yml", "reviewer_token") in names
    assert ("base-ai-review-single.yml", "auth_token_corp") in names
    assert ("claude-auth-restore.yml", "token_corp") in names
    assert ("codex-auth-restore.yml", "token_corp") in names


@pytest.mark.parametrize("mint", _MINTS, ids=[f"{f}:{k}" for f, k, _ in _MINTS])
def test_mint_names_its_permissions(mint: tuple[str, str, dict[str, Any]]) -> None:
    file, key, step = mint
    if (file, key) in _UNNARROWED:
        pytest.skip(_UNNARROWED[(file, key)])
    inputs = step.get("with") or {}
    # An empty value is skipped by the action, so it narrows nothing.
    named = {k: v for k, v in inputs.items() if k.startswith("permission-") and v}
    assert named, (
        f"{file}:{key} mints with every permission the installation holds; "
        "add the permission-* inputs its token's callers need"
    )


@pytest.mark.parametrize("mint", _MINTS, ids=[f"{f}:{k}" for f, k, _ in _MINTS])
def test_mint_pin_honours_permission_inputs(
    mint: tuple[str, str, dict[str, Any]],
) -> None:
    file, key, step = mint
    sha = str(step["uses"]).removeprefix(_ACTION)
    assert sha in _PERMISSION_AWARE_PINS, (
        f"{file}:{key} pins {sha}, not a release confirmed to apply "
        "permission-* inputs; read its dist/main.cjs and add it if it does"
    )


def test_no_mint_sits_outside_the_scan() -> None:
    # The structured scan reads workflows and composite actions only. Any
    # other file under .github naming the action is a mint it cannot see.
    scanned = {p.resolve() for p in _scanned_files()}
    outside = [
        str(p.relative_to(_GITHUB))
        for p in sorted(_GITHUB.rglob("*.y*ml"))
        if p.resolve() not in scanned and _ACTION in p.read_text(encoding="utf-8")
    ]
    assert outside == [], f"{outside} use {_ACTION} outside the scanned files"


def test_every_exemption_still_names_a_mint() -> None:
    names = {(f, k) for f, k, _ in _MINTS}
    assert set(_UNNARROWED) <= names, set(_UNNARROWED) - names
