#!/usr/bin/env python3
"""Shared model-status stamping and verdict writing for reviewer emitters.

Single source of truth for the statuses a reviewer model may emit (AT-1799
contract), and for the one link-safe verdict writer (AT-2941), used by
extract_claude_review.py, extract_codex_json.py, review_gemini.py and
local_reviewer_support.py so the copies cannot drift.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
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


# Read once at import, before any thread exists: os.umask can only be read
# by setting it, which would briefly unmask files another thread creates.
_UMASK = os.umask(0)
os.umask(_UMASK)


def replace_file_text(path: str | os.PathLike[str], text: str) -> None:
    """Write ``text`` to ``path`` without following a symlink at ``path``.

    The reviewer workspace is model-writable (the codex CLI runs with
    ``--sandbox workspace-write``), so a verdict path may be a symlink the
    model planted pointing outside the workspace -- the scripts snapshot
    under RUNNER_TEMP, or any runner-writable file. ``open(path, "w")``
    would follow it and the host would write through the link; instead a
    sibling temp file is written and ``os.replace``'d onto the path, which
    replaces a symlink at the path rather than writing through it. The
    guarantee covers the final path component only: a symlinked parent
    directory is followed, so callers pass a name in a trusted directory
    (the CI verdict names sit at the workspace root). This is
    the host-side mirror of promote_legacy_verdict.sh's temp-file + mv, so
    the two writers share one rule (AT-2941).
    """
    target = Path(path)
    fd, tmp = tempfile.mkstemp(dir=target.parent, prefix=f"{target.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        # mkstemp creates 0600; restore the umask-derived mode the replaced
        # writers produced (0o666 & ~umask).
        os.chmod(tmp, 0o666 & ~_UMASK)
        os.replace(tmp, target)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def write_verdict_json(path: str | os.PathLike[str], obj: Any, **dumps_kwargs: Any) -> None:
    """Serialize ``obj`` to JSON and install it at ``path`` link-safely."""
    replace_file_text(path, json.dumps(obj, **dumps_kwargs))
