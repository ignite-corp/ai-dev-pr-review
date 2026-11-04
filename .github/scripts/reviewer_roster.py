"""Trusted reviewer selection, separate from model-authored review payloads."""

from __future__ import annotations

import json
import os
from typing import cast

SUPPORTED_REVIEWERS: tuple[str, ...] = ("claude", "codex", "gemini", "copilot")
DEFAULT_REVIEWERS = SUPPORTED_REVIEWERS[:3]
REVIEWERS_ENV = "REVIEWERS"


def parse_reviewers(raw: str | None) -> tuple[str, ...]:
    """Read an ordered JSON roster; only an absent setting uses the default."""
    if raw is None:
        return DEFAULT_REVIEWERS
    try:
        value: object = json.loads(raw)
    except ValueError:
        raise ValueError("REVIEWERS must be a nonempty JSON array") from None
    if not isinstance(value, list) or not value:
        raise ValueError("REVIEWERS must be a nonempty JSON array")
    selected: list[str] = []
    for name in cast(list[object], value):
        if not isinstance(name, str) or name not in SUPPORTED_REVIEWERS:
            raise ValueError("REVIEWERS contains an unsupported reviewer")
        if name in selected:
            raise ValueError("REVIEWERS contains a duplicate reviewer")
        selected.append(name)
    return tuple(selected)


def configured_reviewers() -> tuple[str, ...]:
    """The operator's environment, never an artifact or a reviewer identity claim."""
    return parse_reviewers(os.environ.get(REVIEWERS_ENV))
