#!/usr/bin/env python3
"""Regenerate the derived numbers section 5 of design-record.md cites.

Every figure in section 5 that is not copied straight out of a log in
``evidence/`` comes from here. Run it against a checkout of the two PR heads
and a preserved run directory and it prints the same values, or says which
input it could not find.

    python3 measure.py --driver <path to review_pr_local.py as it ran> \
                       --scripts-dir <.github/scripts of that build> \
                       --run-dir <preserved run directory>

Nothing here touches the network. With no arguments it prints the expected
values recorded on 2026-09-20 so a reader can diff against their own run.
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import pathlib
import re
import sys

# Values measured on 2026-09-20 against the run preserved at /tmp/lens-harvest
# (ephemeral; see MANIFEST.md). Kept here so the numbers in the document have a
# machine-readable counterpart even when the run directory is gone.
EXPECTED = {
    "driver_sha256_that_ran": "217da9413de294a523ef345f13d52c51c0c7d26f6cc0ca6e60236c8ab52252a7",
    "driver_sha256_pr172_head_e9592d8": "217da9413de294a523ef345f13d52c51c0c7d26f6cc0ca6e60236c8ab52252a7",
    "driver_sha256_pr170_head_ce8c324": "89a5a158f041258587547ecdc58d725de8c92ad987c5ed31fa8e93170f261719",
    "warning_emit_sites_total": 24,
    "raise_drivererror_sites_total": 17,
    "guard_sites_total": 41,
    "guards_that_fired": 1,
    "codex_prompt_bytes": 83579,
    "context_md_bytes": 63694,
    "pr_diff_bytes": 257081,
    "pr_diff_inside_codex_prompt": False,
    "existing_comments_entry_bytes": 28189,
    "max_arg_strlen": 131072,
    "threads": 45,
    "pytest_nodeids": 232,
    "wall_seconds": 399,
}

MODULES = ("review_pr_local.py", "review_claude_local.py", "review_codex_local.py")


def sha256(path: pathlib.Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def guard_counts(scripts_dir: pathlib.Path) -> dict[str, int]:
    """Count real emit sites, not string occurrences.

    A plain grep for '::warning::' also matches the string where it appears in
    a comment or docstring; this walks the AST so only print() calls and
    `raise DriverError(...)` statements are counted. On the build that ran,
    the two agree (no occurrence sits outside a print), and that agreement is
    itself part of what the document claims.
    """
    warn = raise_de = 0
    for name in MODULES:
        path = scripts_dir / name
        if not path.is_file():
            print(f"missing: {path}", file=sys.stderr)
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "print":
                blob = ""
                for sub in ast.walk(node):
                    if isinstance(sub, ast.Constant) and isinstance(sub.value, str):
                        blob += sub.value
                if "::warning::" in blob:
                    warn += 1
            if isinstance(node, ast.Raise):
                exc = node.exc
                if isinstance(exc, ast.Call) and isinstance(exc.func, ast.Name) and exc.func.id == "DriverError":
                    raise_de += 1
    return {"warning_emit_sites_total": warn, "raise_drivererror_sites_total": raise_de,
            "guard_sites_total": warn + raise_de}


def serialises(driver: pathlib.Path) -> bool:
    """True when the build runs reviewers one at a time in every mode.

    The distinction the document got wrong once: a ThreadPoolExecutor in the
    non-sequential path means `parallel` really is concurrent.
    """
    src = driver.read_text(encoding="utf-8")
    return "ThreadPoolExecutor" not in src


def prompt_composition(run_dir: pathlib.Path) -> dict[str, object]:
    repo = run_dir / "repo"
    prompt = (repo / "codex-prompt.md")
    ctx = (repo / "context.md")
    diff = (repo / "pr.diff")
    out: dict[str, object] = {}
    if prompt.is_file():
        out["codex_prompt_bytes"] = prompt.stat().st_size
    if ctx.is_file():
        out["context_md_bytes"] = ctx.stat().st_size
    if diff.is_file():
        out["pr_diff_bytes"] = diff.stat().st_size
    if prompt.is_file() and diff.is_file():
        head = diff.read_text(encoding="utf-8", errors="replace")[:400]
        out["pr_diff_inside_codex_prompt"] = head in prompt.read_text(encoding="utf-8", errors="replace")
    threads_file = repo / ".review-context" / "unresolved-threads.json"
    if threads_file.is_file():
        threads = json.loads(threads_file.read_text(encoding="utf-8"))
        out["threads"] = len(threads)
        value = json.dumps(threads[:51], separators=(",", ":"), ensure_ascii=False).encode()
        out["existing_comments_entry_bytes"] = len(b"EXISTING_COMMENTS=") + len(value)
    nodeids = repo / ".pytest_cache" / "v" / "cache" / "nodeids"
    if nodeids.is_file():
        ids = json.loads(nodeids.read_text(encoding="utf-8"))
        out["pytest_nodeids"] = len(ids)
        out["pytest_files"] = sorted({i.split("::")[0] for i in ids})
    return out


def fired_guards(driver_output: pathlib.Path) -> dict[str, int]:
    """How many guard paths actually emitted, across both invocations."""
    warn = err = 0
    for log in sorted(driver_output.glob("*.log")):
        text = log.read_text(encoding="utf-8", errors="replace")
        warn += len(re.findall(r"::warning::", text))
        err += len(re.findall(r"::error::", text))
    return {"warnings_emitted": warn, "errors_emitted": err}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--driver", type=pathlib.Path)
    ap.add_argument("--scripts-dir", type=pathlib.Path)
    ap.add_argument("--run-dir", type=pathlib.Path)
    ap.add_argument("--driver-output", type=pathlib.Path,
                    default=pathlib.Path(__file__).resolve().parent.parent / "driver-output")
    args = ap.parse_args()

    got: dict[str, object] = {}
    if args.driver and args.driver.is_file():
        got["driver_sha256_that_ran"] = sha256(args.driver)
        got["serialises_reviewers"] = serialises(args.driver)
    if args.scripts_dir and args.scripts_dir.is_dir():
        got.update(guard_counts(args.scripts_dir))
    if args.run_dir and args.run_dir.is_dir():
        got.update(prompt_composition(args.run_dir))
    if args.driver_output.is_dir():
        got.update(fired_guards(args.driver_output))

    if not got:
        print("No inputs given. Values recorded on 2026-09-20:")
        for k, v in EXPECTED.items():
            print(f"  {k} = {v}")
        return 0

    print("measured now vs recorded 2026-09-20:")
    for key in sorted(got):
        now = got[key]
        then = EXPECTED.get(key, "(not recorded)")
        flag = "" if then == "(not recorded)" or now == then else "   <-- DIFFERS"
        print(f"  {key}\n      now      = {now}\n      recorded = {then}{flag}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
