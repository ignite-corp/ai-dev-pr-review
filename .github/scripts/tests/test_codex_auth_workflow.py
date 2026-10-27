"""Run the actual workflow shell with deterministic, credential-free CLI attempts."""

from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

from aggregate_reviews import (
    _emit_partial_observability,
    _failed_status_detail,
    _payload_failure_reason,
    format_summary,
)
from review_status import stamp_model_status

ROOT = Path(__file__).resolve().parents[3]
SCRIPTS = ROOT / ".github/scripts"
SINGLE = ROOT / ".github/workflows/base-ai-review-single.yml"
_CODEX_JOB_MINUTES = 26
VERDICTS = ("review-codex.json", "verdict-codex.json", "verdict-openai.json")


def _steps() -> list[dict[str, Any]]:
    return yaml.safe_load(SINGLE.read_text())["jobs"]["review"]["steps"]


def _step(name: str) -> dict[str, Any]:
    return next(s for s in _steps() if s.get("name") == name)


def _fixture(
    tmp_path: Path, scenario: str, fallback: bool = True, helper: bool = True
) -> dict[str, str]:
    temp = tmp_path / "runner-temp"
    trusted = temp / "ai-dev-pr-review/.github/scripts"
    trusted.mkdir(parents=True)
    for filename in [
        "promote_legacy_verdict.sh",
        "extract_codex_json.py",
        "github_pr_support.py",
        "review_status.py",
    ]:
        shutil.copy2(SCRIPTS / filename, trusted / filename)
    if helper:
        shutil.copy2(SCRIPTS / "codex_review_auth.py", trusted / "codex_review_auth.py")
    (temp / "review_prompt.md").write_text("Review this diff.")
    home = tmp_path / "home"
    auth = home / ".codex"
    auth.mkdir(parents=True)
    (auth / "review-auth.json").write_text('{"mode":"access-token"}')
    (auth / "auth.json").write_text('{"kind":"token"}')
    if fallback:
        (auth / "review-api-auth.json").write_text('{"kind":"api"}')
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    cli = bin_dir / "codex"
    cli.write_text(
        "#!/usr/bin/env python3\n"
        "import json, os, sys, time\n"
        "from pathlib import Path\n"
        "assert not any('TOKEN' in k or 'KEY' in k or 'SECRET' in k for k in os.environ)\n"
        "assert os.environ['TZ'] == 'UTC'\n"
        "assert 'cli_auth_credentials_store=\"file\"' in sys.argv\n"
        "assert sys.stdin.read() == 'Review this diff.'\n"
        "count = Path('attempt-count')\n"
        "n = int(count.read_text()) + 1 if count.exists() else 1\n"
        "count.write_text(str(n))\n"
        "names = ['review-codex.json', 'verdict-codex.json', 'verdict-openai.json']\n"
        "assert not any(Path(p).exists() for p in names), names\n"
        "scenario = os.environ['SCENARIO']\n"
        "verdict = {'summary': 'api' if n == 2 else 'token', 'status': 'ok', 'early_exit': False, 'issues': []}\n"
        "quota = {'type':'turn.failed','error':{'message':\"You've hit your usage limit. Try again later.\"}}\n"
        "if n == 1 and not scenario.startswith('events-') and scenario not in ['success', 'stdout-verdict', 'stdout-with-brace', 'legacy', 'attempt-timeout', 'attempt-killed']:\n"
        " for name in names: Path(name).write_text(json.dumps(verdict))\n"
        " if scenario == 'retry-agent-leak': print(json.dumps({'type':'item.completed','item':{'type':'agent_message','text':json.dumps(verdict)}}))\n"
        " if scenario == 'stderr-lookalike':\n"
        "  print(json.dumps(quota), file=sys.stderr)\n"
        "  print(json.dumps({'type':'turn.failed','error':{'message':'connection failed'}}))\n"
        " else: print(json.dumps(quota))\n"
        " sys.exit(1)\n"
        "if scenario in ['attempt-timeout', 'api-attempt-timeout']:\n"
        " Path(names[0]).write_text(json.dumps(verdict))\n"
        " time.sleep(5)\n"
        "if scenario == 'attempt-killed':\n"
        " Path(names[0]).write_text(json.dumps(verdict))\n"
        " sys.exit(137)\n"
        "if scenario == 'api-fails':\n"
        " Path(names[0]).write_text(json.dumps(verdict))\n"
        " print(json.dumps({'type':'turn.failed','error':{'message':'API unavailable'}}))\n"
        " sys.exit(1)\n"
        "if scenario.startswith('events-') or scenario == 'retry-agent-leak':\n"
        " def event(kind, text): print(json.dumps({'type':'item.completed','item':{'type':kind,'text':text,'aggregated_output':text}}))\n"
        " if scenario == 'events-braces': event('agent_message','An unmatched brace: {')\n"
        " if scenario == 'events-tool-only': event('command_execution',json.dumps(verdict))\n"
        " elif scenario == 'events-reasoning-only': event('reasoning',json.dumps(verdict))\n"
        " elif scenario != 'retry-agent-leak': event('agent_message',json.dumps(verdict))\n"
        " if scenario == 'events-last-valid': event('agent_message',json.dumps({**verdict,'summary':'last'}))\n"
        " event('agent_message','All done.')\n"
        " Path(sys.argv[sys.argv.index('--output-last-message')+1]).write_text(json.dumps({**verdict,'summary':'final'}) if scenario == 'events-final-primary' else 'All done.')\n"
        " print(json.dumps({'type':'turn.failed','error':{'message':'connection failed'}} if scenario == 'events-failure' else {'type':'turn.completed','usage':{}}))\n"
        " sys.exit(1 if scenario == 'events-failure' else 0)\n"
        "if scenario in ['stdout-verdict', 'stdout-with-brace']:\n"
        " Path(sys.argv[sys.argv.index('--output-last-message')+1]).write_text(json.dumps(verdict))\n"
        " if scenario == 'stdout-with-brace': print(json.dumps({'type':'item.completed','item':{'type':'agent_message','text':'An unmatched brace: {'}}))\n"
        "else: Path(names[0]).write_text(json.dumps(verdict))\n"
        "print(json.dumps({'type':'turn.completed','usage':{}}))\n"
    )
    cli.chmod(0o755)
    return {
        "PATH": str(bin_dir) + os.pathsep + os.environ["PATH"],
        "HOME": str(home),
        "RUNNER_TEMP": str(temp),
        "TZ": "UTC",
        "TMPDIR": str(temp / "codex-tmp"),
        "CODEX_MODEL": "test-model",
        "GITHUB_OUTPUT": str(tmp_path / "outputs"),
        "THREAD_COUNT": "0",
        "EXISTING_COMMENTS": "",
        "SCENARIO": scenario,
    }


def _run(
    tmp_path: Path, env: dict[str, str], script: str | None = None
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            "bash",
            "--noprofile",
            "--norc",
            "-e",
            "-o",
            "pipefail",
            "-c",
            script if script is not None else _step("Run Codex review")["run"],
        ],
        cwd=tmp_path,
        env=env,
        text=True,
        capture_output=True,
        check=False,
        timeout=15,
    )


def test_same_run_quota_retry_discards_all_partial_verdicts(tmp_path: Path) -> None:
    env = _fixture(tmp_path, "quota")
    result = _run(tmp_path, env)
    assert result.returncode == 0, result.stderr
    assert (tmp_path / "attempt-count").read_text() == "2"
    verdict = json.loads((tmp_path / VERDICTS[0]).read_text())
    assert verdict["status"] == "ok" and verdict["summary"] == "api"
    for name in VERDICTS[1:]:
        assert not (tmp_path / name).exists()
    for name in [
        "codex-run.log",
        "codex-events.jsonl",
        "codex-stderr.log",
        "codex-final.txt",
        "codex-token-quota.log",
    ]:
        assert stat.S_IMODE((Path(env["RUNNER_TEMP"]) / name).stat().st_mode) == 0o600
    assert (Path(env["HOME"]) / ".codex/review-quota.json").is_file()


@pytest.mark.parametrize(
    ("scenario", "fallback", "attempts"),
    [("api-fails", True, "2"), ("quota", False, "1")],
)
def test_unavailable_or_failed_fallback_cannot_pass_with_partial_verdict(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    scenario: str,
    fallback: bool,
    attempts: str,
) -> None:
    result = _run(tmp_path, _fixture(tmp_path, scenario, fallback))
    assert result.returncode == 0, result.stderr
    assert (tmp_path / "attempt-count").read_text() == attempts
    verdict = json.loads((tmp_path / VERDICTS[0]).read_text())
    assert verdict["status"] == "failed"
    assert verdict["error"] == "cli_invocation_failed"
    if not fallback:
        assert verdict["error_cause"] == "quota_exhausted"
        assert not (tmp_path / "home/.codex/review-quota.json").exists()
        rendered = "cli_invocation_failed: quota_exhausted -- token quota exhausted and API fallback unavailable"
        summary = format_summary(
            {"codex": verdict},
            "approve",
            "missing reviewer",
            {},
            {"codex": "success"},
            unreviewed_majors=0,
        )
        assert f"codex: {rendered}" in summary
        assert _payload_failure_reason(verdict) == f"failed: {rendered}"
        table = tmp_path / "job-summary.md"
        monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(table))
        _emit_partial_observability({"codex": verdict}, {"codex": "success"})
        assert f"| Codex | {rendered} |" in table.read_text()
        assert f"### Codex -- [ ] not run ({rendered})" in summary
        # Model-authored output cannot retain this infrastructure cause.
        model_verdict = dict(verdict)
        stamp_model_status(model_verdict)
        assert _failed_status_detail(model_verdict) == "cli_invocation_failed"
        for kind in ["action_invocation_failed", "script_invocation_failed"]:
            assert _failed_status_detail({**verdict, "error": kind}) == kind
    else:
        assert "error_cause" not in verdict


def test_stderr_lookalike_does_not_retry(tmp_path: Path) -> None:
    result = _run(tmp_path, _fixture(tmp_path, "stderr-lookalike"))
    assert result.returncode == 0, result.stderr
    assert (tmp_path / "attempt-count").read_text() == "1"
    assert not (tmp_path / "home/.codex/review-quota.json").exists()


@pytest.mark.parametrize(
    ("scenario", "helper"),
    [("stdout-verdict", True), ("stdout-with-brace", True), ("legacy", False)],
)
def test_stdout_verdict_and_old_release_compatibility(
    tmp_path: Path, scenario: str, helper: bool
) -> None:
    result = _run(tmp_path, _fixture(tmp_path, scenario, helper=helper))
    assert result.returncode == 0, result.stderr
    assert (tmp_path / "attempt-count").read_text() == "1"
    assert json.loads((tmp_path / VERDICTS[0]).read_text())["status"] == "ok"


def test_both_reviewer_modes_forward_token_and_control_plane_key() -> None:
    doc = yaml.safe_load(
        (ROOT / ".github/workflows/base-ai-review-orchestrator.yml").read_text()
    )
    assert not doc[True]["workflow_call"]["secrets"]["CODEX_ACCESS_TOKEN"]["required"]
    for name in ["review-codex-p", "review-codex-s"]:
        secrets = doc["jobs"][name]["secrets"]
        assert secrets["CODEX_ACCESS_TOKEN"] == "${{ secrets.CODEX_ACCESS_TOKEN }}"
        assert (
            secrets["REVIEWER_APP_PRIVATE_KEY"]
            == "${{ secrets.REVIEWER_APP_PRIVATE_KEY }}"
        )
    run_env = _step("Run Codex review")["env"]
    assert not any(
        word in key for key in run_env for word in ["KEY", "TOKEN", "SECRET"]
    )


def test_codex_budget_fits_two_bounded_attempts_and_finalization() -> None:
    job = yaml.safe_load(SINGLE.read_text())["jobs"]["review"]
    step = _step("Run Codex review")
    attempt_seconds = 10 * 60
    kill_grace_seconds = 5
    assert int(step["timeout-minutes"]) * 60 > 2 * (
        attempt_seconds + kill_grace_seconds
    )
    assert "timeout --signal=TERM --kill-after=5s 10m codex exec" in step["run"]
    assert (
        job["timeout-minutes"]
        == "${{ (inputs.reviewer == 'codex' && inputs.review-job-timeout-minutes < %d) && %d || inputs.review-job-timeout-minutes }}"
        % (_CODEX_JOB_MINUTES, _CODEX_JOB_MINUTES)
    )
    guard = _step("Validate timeout inputs")
    assert guard["env"]["JOB_MINUTES"] == job["timeout-minutes"]
    assert guard["env"]["CODEX_STEP_MINUTES"] == step["timeout-minutes"]
    assert (
        _step("Verify Codex verdict file")["env"]["STEP_CAP_MINUTES"]
        == step["timeout-minutes"]
    )
    assert (
        _CODEX_JOB_MINUTES
        >= int(step["timeout-minutes"]) + guard["env"]["MIN_HEADROOM_MINUTES"]
    )


@pytest.mark.parametrize(
    ("reviewer", "caller_minutes", "expected"),
    [
        ("codex", 15, 26),
        ("codex", 26, 26),
        ("codex", 45, 45),
        ("claude", 15, 15),
        ("claude", 45, 45),
        ("gemini", 15, 15),
        ("gemini", 45, 45),
    ],
)
def test_workflow_job_budget_preserves_larger_caller_values(
    reviewer: str,
    caller_minutes: int,
    expected: int,
) -> None:
    job = yaml.safe_load(SINGLE.read_text())["jobs"]["review"]
    guard = _step("Validate timeout inputs")
    # These expressions use only comparisons and short-circuit boolean
    # operators, whose operand-returning behavior matches Python here.
    values = []
    for expression in [job["timeout-minutes"], guard["env"]["JOB_MINUTES"]]:
        rendered = expression.removeprefix("${{").removesuffix("}}")
        rendered = rendered.replace("inputs.reviewer", repr(reviewer))
        rendered = rendered.replace(
            "inputs.review-job-timeout-minutes", str(caller_minutes)
        )
        rendered = rendered.replace("&&", "and").replace("||", "or")
        values.append(eval(rendered, {"__builtins__": {}}, {}))
    assert values == [expected, expected]
    env = {key: str(value) for key, value in guard["env"].items()}
    env.update(REVIEWER=reviewer, JOB_MINUTES=str(values[1]), CLAUDE_STEP_MINUTES="10")
    result = subprocess.run(
        ["bash", "-e", "-c", guard["run"]],
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize("job_name", ["review-codex-p", "review-codex-s"])
@pytest.mark.parametrize(("caller_minutes", "expected"), [(15, 26), (45, 45)])
def test_orchestrator_forwards_codex_budget_to_single_workflow(
    job_name: str,
    caller_minutes: int,
    expected: int,
) -> None:
    doc = yaml.safe_load(
        (ROOT / ".github/workflows/base-ai-review-orchestrator.yml").read_text()
    )
    upstream = doc["jobs"][job_name]["with"]
    assert (
        upstream["review-job-timeout-minutes"]
        == "${{ inputs.review-job-timeout-minutes }}"
    )
    single = yaml.safe_load(SINGLE.read_text())["jobs"]["review"]
    expression = single["timeout-minutes"].removeprefix("${{").removesuffix("}}")
    expression = expression.replace("inputs.reviewer", repr(upstream["reviewer"]))
    expression = expression.replace(
        "inputs.review-job-timeout-minutes", str(caller_minutes)
    )
    expression = expression.replace("&&", "and").replace("||", "or")
    assert eval(expression, {"__builtins__": {}}, {}) == expected
    for name in ["review-gemini-p", "review-gemini-s"]:
        assert "review-job-timeout-minutes" not in doc["jobs"][name]["with"]


@pytest.mark.parametrize(
    ("scenario", "attempts"), [("attempt-timeout", "1"), ("api-attempt-timeout", "2")]
)
def test_per_attempt_timeout_finishes_with_an_honest_failed_verdict(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    scenario: str,
    attempts: str,
) -> None:
    script = _step("Run Codex review")["run"]
    shortened = script.replace("10m codex exec", "0.5s codex exec")
    assert shortened != script
    result = _run(tmp_path, _fixture(tmp_path, scenario), shortened)
    assert result.returncode == 0, result.stderr
    assert (tmp_path / "attempt-count").read_text() == attempts
    verdict = json.loads((tmp_path / VERDICTS[0]).read_text())
    assert verdict["status"] == "failed"
    assert verdict["error_cause"] == "attempt_timeout"
    rendered = (
        "cli_invocation_failed: attempt_timeout -- per-attempt time limit reached"
    )
    assert _payload_failure_reason(verdict) == f"failed: {rendered}"
    summary = format_summary(
        {"codex": verdict},
        "approve",
        "missing reviewer",
        {},
        {"codex": "success"},
        unreviewed_majors=0,
    )
    assert f"codex: {rendered}" in summary
    assert f"### Codex -- [ ] not run ({rendered})" in summary
    table = tmp_path / "job-summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(table))
    _emit_partial_observability({"codex": verdict}, {"codex": "success"})
    assert f"| Codex | {rendered} |" in table.read_text()
    for kind in ["action_invocation_failed", "script_invocation_failed"]:
        assert _failed_status_detail({**verdict, "error": kind}) == kind
    model_verdict = dict(verdict)
    stamp_model_status(model_verdict)
    assert _failed_status_detail(model_verdict) == "cli_invocation_failed"


def test_killed_attempt_does_not_claim_timeout_without_evidence(tmp_path: Path) -> None:
    result = _run(tmp_path, _fixture(tmp_path, "attempt-killed"))
    assert result.returncode == 0, result.stderr
    assert (tmp_path / "attempt-count").read_text() == "1"
    verdict = json.loads((tmp_path / VERDICTS[0]).read_text())
    assert verdict["status"] == "failed"
    assert "error_cause" not in verdict


def test_quota_attempt_log_is_uploaded_and_symlinks_are_removed(tmp_path: Path) -> None:
    env = _fixture(tmp_path, "quota")
    archive = Path(env["RUNNER_TEMP"]) / "codex-token-quota.log"
    archive.write_text("old log")
    archive.chmod(0o644)
    result = _run(tmp_path, env)
    assert result.returncode == 0, result.stderr
    assert stat.S_IMODE(archive.stat().st_mode) == 0o600
    assert (
        "${{ runner.temp }}/codex-token-quota.log"
        in _step("Upload Codex logs")["with"]["path"]
    )
    archive.unlink()
    outside = tmp_path / "outside"
    outside.write_text("not an artifact")
    archive.symlink_to(outside)
    final_log = Path(env["RUNNER_TEMP"]) / "codex-run.log"
    final_log.unlink()
    final_log.symlink_to(outside)
    drop = subprocess.run(
        [
            "bash",
            "-e",
            "-c",
            _step("Drop symlinked codex artifacts before upload")["run"],
        ],
        cwd=tmp_path,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )
    assert drop.returncode == 0, drop.stderr
    assert not archive.exists()
    assert not final_log.exists()
    assert outside.read_text() == "not an artifact"


def test_legacy_bootstrap_login_has_explicit_file_store_and_empty_store_error(
    tmp_path: Path,
) -> None:
    env = _fixture(tmp_path, "legacy", helper=False)
    env["OPENAI_API_KEY"] = "test-api-key"
    env["CODEX_ACCESS_TOKEN"] = "test-token"
    env["GH_TOKEN"] = "test-control-token"
    cli = tmp_path / "bin/codex"
    cli.write_text(
        "#!/usr/bin/env python3\n"
        "import os, sys\n"
        "from pathlib import Path\n"
        "assert 'cli_auth_credentials_store=\"file\"' in sys.argv\n"
        "assert '--with-api-key' in sys.argv\n"
        "assert 'CODEX_ACCESS_TOKEN' not in os.environ and 'GH_TOKEN' not in os.environ\n"
        "assert sys.stdin.read().strip() == 'test-api-key'\n"
        "(Path.home()/'.codex/auth.json').write_text('')\n"
    )
    result = subprocess.run(
        [
            "bash",
            "-e",
            "-c",
            _step("Authenticate Codex CLI with preferred credential")["run"],
        ],
        cwd=tmp_path,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 1
    assert (
        "::error::Codex API login did not produce a nonempty auth.json" in result.stdout
    )


@pytest.mark.parametrize(("metadata", "exit_code"), [("{", 2), ("[]", 1), ("{}", 2)])
def test_classifier_failure_clears_partial_verdicts_before_failure_net(
    tmp_path: Path,
    metadata: str,
    exit_code: int,
) -> None:
    env = _fixture(tmp_path, "quota")
    (Path(env["HOME"]) / ".codex/review-auth.json").write_text(metadata)
    result = _run(tmp_path, env)
    assert result.returncode == exit_code
    assert "::error::Codex quota classifier failed" in result.stdout
    assert not any((tmp_path / name).exists() for name in VERDICTS)
    env.update(
        {
            "REVIEWER": "codex",
            "RUN_OUTCOME": "failure",
            "STEP_OUTCOME": "failure",
            "STEP_CAP_MINUTES": str(_step("Run Codex review")["timeout-minutes"]),
            "START_MARKER": str(Path(env["RUNNER_TEMP"]) / "codex-start"),
            "END_MARKER": str(Path(env["RUNNER_TEMP"]) / "codex-end"),
        }
    )
    for name in [
        "Drop symlinked codex artifacts before upload",
        "Normalize review file name",
        "Verify Codex verdict file",
    ]:
        net = subprocess.run(
            ["bash", "-e", "-c", _step(name)["run"]],
            cwd=tmp_path,
            env=env,
            text=True,
            capture_output=True,
            check=False,
        )
        assert net.returncode == 0, net.stderr
    verdict = json.loads((tmp_path / VERDICTS[0]).read_text())
    assert verdict["status"] == "failed"
    assert verdict["error"] == "cli_invocation_failed"


def test_normal_no_quota_path_does_not_retry_with_api(tmp_path: Path) -> None:
    result = _run(tmp_path, _fixture(tmp_path, "success"))
    assert result.returncode == 0, result.stderr
    assert (tmp_path / "attempt-count").read_text() == "1"
    assert json.loads((tmp_path / VERDICTS[0]).read_text())["summary"] == "token"


@pytest.mark.parametrize(
    ("scenario", "summary", "error"),
    [
        ("events-nonfinal", "token", None),
        ("events-braces", "token", None),
        ("events-last-valid", "last", None),
        ("events-final-primary", "final", None),
        ("events-tool-only", None, "output_unparseable"),
        ("events-reasoning-only", None, "output_unparseable"),
        ("events-failure", None, "cli_invocation_failed"),
        ("retry-agent-leak", None, "output_unparseable"),
    ],
)
def test_only_completed_agent_messages_can_recover_a_nonfinal_verdict(
    tmp_path: Path,
    scenario: str,
    summary: str | None,
    error: str | None,
) -> None:
    result = _run(tmp_path, _fixture(tmp_path, scenario))
    assert result.returncode == 0, result.stderr
    verdict = json.loads((tmp_path / VERDICTS[0]).read_text())
    if summary is not None:
        assert verdict["status"] == "ok" and verdict["summary"] == summary
    else:
        assert verdict["status"] == "failed" and verdict["error"] == error
    assert (tmp_path / "attempt-count").read_text() == (
        "2" if scenario == "retry-agent-leak" else "1"
    )
