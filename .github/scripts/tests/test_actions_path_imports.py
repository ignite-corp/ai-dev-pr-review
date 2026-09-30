"""The Actions path must import with nothing but the standard library.

Only the gemini job runs `pip install -r requirements.txt`. Every other
job that runs a Python script goes straight from `setup-python` to
`python <script>`, so anything those scripts reach -- directly or through
a chain of local imports -- has to be in the stdlib or it is a
ModuleNotFoundError on the runner.

Nothing tested that before AT-2510. The whole suite, every reviewer and
the release all run with requirements.txt installed, so an import that
only the runner would fail on is invisible to them: v1.11.0 shipped
`aggregate_reviews -> local_reviewer_support -> local_review_config ->
yaml` and broke every consumer's aggregate job at import.

A unit test that merely imports the module proves nothing here, which is
why each case runs in a subprocess with the third-party roots made
unimportable.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1]
WORKFLOWS = SCRIPTS.parent / "workflows"

# Top-level module names requirements.txt provides. Blocked wholesale
# rather than named per script: the point is what the runner has, and the
# runner has neither of these unless a job installed them.
THIRD_PARTY_ROOTS = frozenset({"yaml", "google"})

# Scripts a base workflow runs with no `pip install` ahead of them.
STDLIB_ONLY_SCRIPTS = (
    "aggregate_reviews",
    "extract_claude_review",
    "extract_codex_json",
    "fetch_review_context",
    "filter_pr_diff",
    "post_inline_comments",
    "switch_claude_auth",
    "verify_action_shas",
)

# Scripts a base workflow runs only after installing requirements.txt.
INSTALLED_DEPS_SCRIPTS = ("review_gemini",)

_SCRIPT_REF = re.compile(r"\.github/scripts/(?P<name>[A-Za-z0-9_]+)\.py")

# Installed as a meta-path finder so the block covers a lazy import too,
# not only the ones resolved while the module body runs.
_BLOCK_AND_IMPORT = """
import sys

_BLOCKED = {blocked!r}


class _Blocker:
    def find_spec(self, fullname, path=None, target=None):
        root = fullname.split(".")[0]
        if root in _BLOCKED:
            raise ModuleNotFoundError("No module named " + repr(root), name=fullname)
        return None


sys.meta_path.insert(0, _Blocker())
for _name in list(sys.modules):
    if _name.split(".")[0] in _BLOCKED:
        del sys.modules[_name]

import {module}
"""


@pytest.mark.parametrize("module", STDLIB_ONLY_SCRIPTS)
def test_actions_path_script_imports_without_third_party_packages(module: str) -> None:
    code = _BLOCK_AND_IMPORT.format(blocked=set(THIRD_PARTY_ROOTS), module=module)
    proc = subprocess.run(
        [sys.executable, "-c", code],
        cwd=SCRIPTS,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert proc.returncode == 0, (
        f"{module}.py cannot be imported without "
        f"{sorted(THIRD_PARTY_ROOTS)}, but the job that runs it installs "
        f"nothing:\n{proc.stderr}"
    )


def test_every_script_the_base_workflows_run_is_classified() -> None:
    """Deny by default: a new script has to be put in one list or the other."""
    referenced: set[str] = set()
    for path in sorted(WORKFLOWS.glob("base-ai-review-*.yml")):
        text = path.read_text(encoding="utf-8")
        referenced.update(match.group("name") for match in _SCRIPT_REF.finditer(text))

    classified: set[str] = set(STDLIB_ONLY_SCRIPTS) | set(INSTALLED_DEPS_SCRIPTS)
    unclassified = sorted(referenced - classified)
    assert not unclassified, (
        "base workflows run these scripts but no list here says whether "
        f"their job installs requirements.txt: {unclassified}"
    )

    stale = sorted(classified - referenced)
    assert not stale, f"no base workflow runs these any more: {stale}"
