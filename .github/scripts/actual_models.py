#!/usr/bin/env python3
"""Record the model a reviewer actually ran on, in its verdict (AT-3591).

The configured model (``vars.CLAUDE_MODEL || '<default>'`` and its siblings)
says what was asked for. ``actual_models`` says what answered, and is read
only from a source the reviewer model cannot write:

* Claude: the ``modelUsage`` keys of the CLI's own ``result`` message in the
  execution file. Assistant content in the same file is model text and is
  never read for this.
* Codex: the ``threads.model`` column of the CLI's state database
  (``$CODEX_HOME/state_<n>.sqlite``), for the thread the ``thread.started``
  event names. The database sits under the Codex home the sandbox profile
  denies to the model (AT-3054). The non-JSON header is not used: ``codex
  exec --json`` prints none (measured on 0.166.0), and without ``--json`` it
  shares a stream with the transcript, where model text could imitate it.
* Gemini: ``GenerateContentResponse.model_version``, set by review_gemini.py.
* Copilot: the adapter's ``AssistantUsageData`` events, set by
  review_copilot.py.

Anything that cannot be read is ``["unknown"]``. The verdict file is
model-writable for Claude and Codex, so ``stamp`` always overwrites the field
rather than trusting a value already there.

Usage:
  actual_models.py claude <verdict> <execution_file>
  actual_models.py codex <verdict> <events_jsonl> <codex_home>
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import sys
from contextlib import closing
from pathlib import Path
from typing import Any

from github_pr_support import is_json_array, is_json_object
from review_status import write_verdict_json

FIELD = "actual_models"
UNKNOWN = "unknown"
# One shape for every reviewer's model ID; the verdict comment renders these
# inside backticks, so nothing that could close one gets through.
MODEL_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}")
_STATE_DB_RE = re.compile(r"state_([0-9]+)\.sqlite")
_USAGE = (
    "usage: actual_models.py claude <verdict> <execution_file>\n"
    "       actual_models.py codex <verdict> <events_jsonl> <codex_home>"
)


def sanitize(values: object) -> list[str]:
    """Sorted unique model IDs from ``values``; ``["unknown"]`` if none."""
    if not is_json_array(values):
        return [UNKNOWN]
    models = {v for v in values if isinstance(v, str) and MODEL_ID_RE.fullmatch(v)}
    return sorted(models) or [UNKNOWN]


def claude_models(exec_path: str | os.PathLike[str]) -> list[str]:
    """The ``modelUsage`` keys of the last ``result`` message."""
    try:
        data = json.loads(Path(exec_path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return [UNKNOWN]
    entries = data if is_json_array(data) else [data]
    for entry in reversed(entries):
        if is_json_object(entry) and entry.get("type") == "result":
            usage = entry.get("modelUsage")
            return sanitize(list(usage) if is_json_object(usage) else None)
    return [UNKNOWN]


def _thread_ids(events_path: str | os.PathLike[str]) -> list[str]:
    """Thread IDs from the CLI's ``thread.started`` events, one JSON per line."""
    try:
        text = Path(events_path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    ids: list[str] = []
    for line in text.splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if is_json_object(event) and event.get("type") == "thread.started":
            thread_id = event.get("thread_id")
            if isinstance(thread_id, str):
                ids.append(thread_id)
    return ids


def _state_db(codex_home: Path) -> Path | None:
    """The newest ``state_<n>.sqlite``: the CLI bumps <n> on a schema change."""
    try:
        found = [
            (int(m.group(1)), path)
            for path in codex_home.iterdir()
            if (m := _STATE_DB_RE.fullmatch(path.name))
        ]
    except OSError:
        return None
    return max(found)[1] if found else None


def codex_models(
    events_path: str | os.PathLike[str], codex_home: str | os.PathLike[str]
) -> list[str]:
    """``threads.model`` for this run's threads, from the CLI's state DB."""
    ids = _thread_ids(events_path)
    db = _state_db(Path(codex_home))
    if not ids or db is None:
        return [UNKNOWN]
    marks = ",".join("?" * len(ids))
    try:
        uri = f"{db.resolve().as_uri()}?mode=ro"
        with closing(sqlite3.connect(uri, uri=True)) as conn:
            rows = conn.execute(
                f"SELECT model FROM threads WHERE id IN ({marks})", ids
            ).fetchall()
    except sqlite3.Error:
        return [UNKNOWN]
    return sanitize([row[0] for row in rows])


def stamp(verdict_path: str | os.PathLike[str], models: list[str]) -> None:
    """Overwrite the verdict's ``actual_models``; leave a non-verdict alone.

    A symlink at the verdict path is not followed (AT-2941): 'Record cancel
    cause' removes it before the upload.
    """
    path = Path(verdict_path)
    if path.is_symlink() or not path.is_file():
        return
    try:
        verdict: Any = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return
    if not is_json_object(verdict):
        return
    verdict[FIELD] = sanitize(models)
    write_verdict_json(path, verdict, indent=2, ensure_ascii=False)


def main(argv: list[str]) -> int:
    if len(argv) == 3 and argv[0] == "claude":
        models = claude_models(argv[2])
    elif len(argv) == 4 and argv[0] == "codex":
        models = codex_models(argv[2], argv[3])
    else:
        print(_USAGE, file=sys.stderr)
        return 2
    stamp(argv[1], models)
    print(f"{argv[0]} actual_models: {', '.join(models)}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
