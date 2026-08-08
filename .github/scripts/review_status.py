#!/usr/bin/env python3
"""Shared model-status stamping for reviewer emitters (AT-1799 contract).

Single source of truth for the statuses a reviewer model may emit, used by
extract_claude_review.py, extract_codex_json.py, and review_gemini.py so
the copies cannot drift.
"""

from __future__ import annotations

from typing import Any

# Statuses the reviewer model may emit; "failed" is reserved for
# infrastructure failure paths and is never trusted from model output.
MODEL_STATUSES = ("ok", "early_exit")


def stamp_model_status(obj: dict[str, Any]) -> None:
    """Ensure the payload carries an explicit status (AT-1799 contract).

    Mutates ``obj`` in place: a valid model-emitted status
    ("ok"/"early_exit") is kept; anything else -- including a
    model-emitted "failed", which is reserved for infrastructure and
    never trusted from model output -- is re-derived from the early_exit
    flag (true -> "early_exit", else "ok").
    """
    if obj.get("status") not in MODEL_STATUSES:
        obj["status"] = "early_exit" if obj.get("early_exit") is True else "ok"
