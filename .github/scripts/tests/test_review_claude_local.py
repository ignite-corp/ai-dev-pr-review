"""The local Claude reviewer must fail the way the Actions one fails.

AT-1837: when the Claude action died before writing a verdict, no artifact
was uploaded at all, so the aggregate saw an ABSENT reviewer -- which only
lowers the response count -- instead of a FAILED one, and reported the outage
as a benign "early-exit or no-output". The workflow's answer was to always
emit an error verdict. A local driver that quietly writes nothing would
reintroduce exactly that.

So what is pinned here is the verdict file always existing, the status being
"failed" (never a model-emitted one) on every failure path, and the
`--model` / `--allowedTools` values coming from the composite action rather
than from a second copy in this module.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
import yaml

SCRIPT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SCRIPT_DIR))

import review_claude_local  # noqa: E402
from local_review_config import workflow_defaults  # noqa: E402

ACTION_YML = SCRIPT_DIR.parents[0] / "actions" / "claude-review" / "action.yml"


@pytest.fixture
def workdir(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    return tmp_path


def _verdict(workdir: Path) -> dict:
    return json.loads(
        (workdir / review_claude_local.REVIEW_FILE).read_text(encoding="utf-8")
    )


def _stub_cli(monkeypatch, exit_code: int, stdout: str, writes: dict | None = None):
    def fake_run_cli(prompt, model):
        if writes is not None:
            Path(review_claude_local.REVIEW_FILE).write_text(json.dumps(writes))
        return exit_code, stdout

    monkeypatch.setattr(review_claude_local, "run_cli", fake_run_cli)


def test_allowed_tools_come_from_the_composite_action():
    action = yaml.safe_load(ACTION_YML.read_text(encoding="utf-8"))
    assert (
        review_claude_local.allowed_tools()
        == action["inputs"]["allowed_tools"]["default"]
    )


def test_model_is_the_workflow_default_when_unset(workdir, monkeypatch):
    monkeypatch.delenv("CLAUDE_MODEL", raising=False)
    seen: dict[str, str] = {}

    def fake_run_cli(prompt, model):
        seen["model"] = model
        return 0, ""

    monkeypatch.setattr(review_claude_local, "run_cli", fake_run_cli)
    review_claude_local.main()
    assert seen["model"] == workflow_defaults()["CLAUDE_MODEL"]


def test_a_direct_write_is_kept(workdir, monkeypatch):
    payload = {"summary": "ok", "status": "ok", "early_exit": False, "issues": []}
    _stub_cli(monkeypatch, 0, "", writes=payload)
    review_claude_local.main()
    assert _verdict(workdir) == payload


def test_a_malformed_direct_write_becomes_an_error_verdict(workdir, monkeypatch):
    def fake_run_cli(prompt, model):
        Path(review_claude_local.REVIEW_FILE).write_text("not json at all")
        return 0, ""

    monkeypatch.setattr(review_claude_local, "run_cli", fake_run_cli)
    review_claude_local.main()
    verdict = _verdict(workdir)
    assert verdict["status"] == "failed"
    assert verdict["error"] == "output_unparseable"


def test_the_verdict_is_recovered_from_the_cli_output(workdir, monkeypatch):
    review = {
        "summary": "one finding",
        "early_exit": False,
        "issues": [{"severity": "minor", "file": "a.py", "line": 1}],
    }
    _stub_cli(
        monkeypatch, 0, json.dumps({"result": f"here it is:\n{json.dumps(review)}"})
    )
    review_claude_local.main()
    verdict = _verdict(workdir)
    assert verdict["summary"] == "one finding"
    # extract_claude_review stamps the AT-1799 status contract.
    assert verdict["status"] == "ok"


def test_output_without_a_verdict_fails_loudly(workdir, monkeypatch):
    _stub_cli(monkeypatch, 0, json.dumps({"result": "I could not review this."}))
    review_claude_local.main()
    verdict = _verdict(workdir)
    assert verdict["status"] == "failed"
    assert verdict["error"] == "output_unparseable"
    assert verdict["issues"] == []


def test_a_cli_that_never_ran_fails_loudly(workdir, monkeypatch):
    _stub_cli(monkeypatch, -1, "")
    review_claude_local.main()
    verdict = _verdict(workdir)
    assert verdict["status"] == "failed"
    assert verdict["error"] == "cli_invocation_failed"


def test_every_failure_path_leaves_a_verdict_file(workdir, monkeypatch):
    """The aggregate must see a FAILED reviewer, never an absent one."""
    for exit_code, stdout in ((-1, ""), (1, "garbage"), (0, "{}")):
        (workdir / review_claude_local.REVIEW_FILE).unlink(missing_ok=True)
        _stub_cli(monkeypatch, exit_code, stdout)
        review_claude_local.main()
        assert _verdict(workdir)["status"] == "failed"


def test_an_uninstalled_cli_is_reported_not_raised(workdir, monkeypatch):
    def missing(*args, **kwargs):
        raise FileNotFoundError("claude")

    monkeypatch.setattr(review_claude_local.subprocess, "run", missing)
    assert review_claude_local.run_cli("prompt", "model") == (-1, "")


def test_a_direct_write_is_stamped_like_the_codex_shim(workdir, monkeypatch):
    """A direct write skips extract_claude_review, where status is stamped.

    Both shims in this pipeline take a direct write from their CLI; only one
    of them used to apply the AT-1799 contract to it.
    """
    _stub_cli(
        monkeypatch,
        0,
        "",
        writes={"summary": "s", "early_exit": True, "issues": []},
    )
    review_claude_local.main()
    assert _verdict(workdir)["status"] == "early_exit"


def test_a_model_emitted_failed_status_is_not_trusted(workdir, monkeypatch):
    """ "failed" is reserved for infrastructure paths, not model output."""
    _stub_cli(
        monkeypatch,
        0,
        "",
        writes={
            "summary": "s",
            "status": "failed",
            "early_exit": False,
            "issues": [],
        },
    )
    review_claude_local.main()
    assert _verdict(workdir)["status"] == "ok"
