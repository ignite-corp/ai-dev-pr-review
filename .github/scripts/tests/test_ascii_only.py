"""Public repo invariant: all CI-related files must be ASCII-only.

Korean/emoji content belongs in caller repos' prompt files, not in the
reusable workflow surface area.
"""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3] / ".github"
NON_ASCII = re.compile(r"[^\x00-\x7F]")


def test_no_non_ascii_in_github_dir():
    bad = []
    for path in ROOT.rglob("*"):
        if not path.is_file():
            continue
        try:
            content = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue  # binary file, skip
        if NON_ASCII.search(content):
            bad.append(str(path.relative_to(ROOT.parent)))
    assert not bad, f"Non-ASCII found in: {bad}"
