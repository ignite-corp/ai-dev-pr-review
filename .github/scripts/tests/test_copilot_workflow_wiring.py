"""Execute the selected Actions boundaries without credentials or provider calls."""

from __future__ import annotations

import hashlib
import importlib.util
import io
import itertools
import json
import os
from pathlib import Path
import re
import socket
import shutil
import shlex
import subprocess
import sys
import tarfile
from typing import Any
from unittest.mock import Mock

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[3]
SCRIPTS = ROOT / ".github/scripts"
WORKFLOWS = ROOT / ".github/workflows"
ALL = ("claude", "codex", "gemini", "copilot")
DEFAULT = ALL[:3]
OK = {"summary": "Reviewed", "status": "ok", "early_exit": False, "issues": []}
DIGEST = "aba1f06f27bc0447656647f76e0a4cf58b4b2a341c75ff04a750982b6aeb6bd2"


def workflow(name: str) -> dict[Any, Any]:
    return yaml.safe_load((WORKFLOWS / f"{name}.yml").read_text())


def step(name: str, base: str = "single") -> dict[str, Any]:
    jobs = workflow(f"base-ai-review-{base}")["jobs"]
    return next(s for s in jobs[next(iter(jobs))]["steps"] if s.get("name") == name)


def evaluate(expression: str, values: dict[str, Any], cancelled: bool = False) -> Any:
    """Only the small expression subset actually used by the slot graph."""
    expression = expression.removeprefix("${{").removesuffix("}}")
    expression = expression.replace("!cancelled()", repr(not cancelled))
    expression = re.sub(
        r"(?:needs|vars|inputs|secrets)\.[A-Za-z0-9_.-]+",
        lambda match: repr(values.get(match[0], "")),
        expression,
    )
    expression = expression.replace("&&", " and ").replace("||", " or ")
    assert not re.search(r"needs\.|vars\.|inputs\.|secrets\.|cancelled\(", expression)
    return eval(expression, {"__builtins__": {}}, {})


def shell(
    script: str, directory: Path, env: dict[str, str]
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", "--noprofile", "--norc", "-e", "-o", "pipefail", "-c", script],
        cwd=directory,
        env={"PATH": os.environ["PATH"], **env},
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )


def read_outputs(path: Path) -> dict[str, str]:
    return dict(line.split("=", 1) for line in path.read_text().splitlines())


def prepare_roster(directory: Path, raw: str) -> subprocess.CompletedProcess[str]:
    trusted = directory / ".ai-dev-pr-review/.github/scripts"
    trusted.mkdir(parents=True, exist_ok=True)
    shutil.copy2(SCRIPTS / "reviewer_roster.py", trusted)
    return shell(
        step("Validate ordered reviewer roster", "prepare")["run"],
        directory,
        {"REVIEWERS": raw, "GITHUB_OUTPUT": str(directory / "outputs")},
    )


def slot_values(names: tuple[str, ...], mode: str) -> dict[str, Any]:
    return {
        "vars.REVIEW_MODE": mode,
        "needs.prepare.outputs.skip": "false",
        **{
            f"needs.prepare.outputs.reviewer_{i}": names[i - 1]
            if i <= len(names)
            else ""
            for i in range(1, 5)
        },
    }


@pytest.mark.parametrize("size", range(1, 5))
def test_every_supported_order_dispatches_exactly_its_selected_slots(
    tmp_path: Path, size: int
) -> None:
    jobs = workflow("base-ai-review-orchestrator")["jobs"]
    for names in itertools.permutations(ALL, size):
        result = prepare_roster(tmp_path, json.dumps(names))
        assert result.returncode == 0, result.stderr
        outputs = read_outputs(tmp_path / "outputs")
        assert json.loads(outputs["reviewers"]) == list(names)
        for mode in ("parallel", "sequential"):
            values = slot_values(names, mode)
            for i in range(1, 5):
                assert (
                    outputs[f"reviewer_{i}"]
                    == values[f"needs.prepare.outputs.reviewer_{i}"]
                )
                values[f"needs.review-{i}-s.result"] = "success"
                values[f"needs.review-{i}-s.outputs.early_exit"] = "false"
                for suffix in ("p", "s"):
                    job = jobs[f"review-{i}-{suffix}"]
                    expected = i <= size and suffix == mode[0]
                    assert bool(evaluate(job["if"], values)) is expected
                    if expected:
                        assert evaluate(job["with"]["reviewer"], values) == names[i - 1]


def test_default_remains_three_and_invalid_rosters_cannot_dispatch(
    tmp_path: Path,
) -> None:
    orchestrator = workflow("base-ai-review-orchestrator")
    declared = orchestrator[True]["workflow_call"]["inputs"]["reviewers"]["default"]
    assert json.loads(declared) == list(DEFAULT)
    for raw in (
        "",
        "[]",
        "null",
        '["copilot","copilot"]',
        '["unknown"]',
        "$(touch owned)",
        '["copilot\n::error::injected"]',
    ):
        result = prepare_roster(tmp_path, raw)
        assert result.returncode != 0
        assert raw not in result.stdout or not raw
        assert "Invalid reviewers" in result.stdout
        assert not (tmp_path / "owned").exists()
    for job in orchestrator["jobs"].values():
        if job.get("uses", "").endswith("base-ai-review-single.yml"):
            assert not evaluate(job["if"], {})
            assert not evaluate(job["if"], {"vars.REVIEW_MODE": "sequential"})
    assert orchestrator["jobs"]["aggregate"]["if"] == "always()"
    assert (
        workflow("base-ai-review-aggregate")["jobs"]["aggregate"]["name"]
        == "Aggregate & Verdict"
    )


def test_all_prior_slot_states_gate_the_whole_ordered_chain() -> None:
    jobs = workflow("base-ai-review-orchestrator")["jobs"]
    states = (
        ("success", "false"),
        ("success", "true"),
        ("failure", "true"),
        ("skipped", ""),
        ("cancelled", ""),
    )
    names = ("copilot", "gemini", "claude", "codex")
    for index in range(1, 5):
        job = jobs[f"review-{index}-s"]
        expected_needs = ["prepare", *(f"review-{j}-s" for j in range(1, index))]
        actual_needs = job["needs"]
        assert (
            [actual_needs] if isinstance(actual_needs, str) else actual_needs
        ) == expected_needs
        for prior in itertools.product(states, repeat=index - 1):
            values = slot_values(names, "sequential")
            for j, (conclusion, early) in enumerate(prior, 1):
                values[f"needs.review-{j}-s.result"] = conclusion
                values[f"needs.review-{j}-s.outputs.early_exit"] = early
            assert bool(evaluate(job["if"], values)) is not any(
                conclusion != "failure" and early == "true"
                for conclusion, early in prior
            )
            assert not evaluate(job["if"], values, cancelled=True)
            assert not evaluate(
                job["if"], {**values, "needs.prepare.outputs.skip": "true"}
            )
            assert not evaluate(job["if"], {**values, "needs.prepare.outputs.skip": ""})


@pytest.mark.parametrize(
    "requested",
    ["", '["copilot","claude","codex","gemini"]', '["unknown"]'],
)
def test_manual_self_review_forwards_selection_without_changing_the_default(
    requested: str,
) -> None:
    caller = workflow("self-review")
    default = caller[True]["workflow_dispatch"]["inputs"]["reviewers"]["default"]
    assert json.loads(default) == list(DEFAULT)
    job = caller["jobs"]["review"]
    selected = evaluate(job["with"]["reviewers"], {"inputs.reviewers": requested})
    assert selected == (requested or default)
    # Only this named account credential is forwarded; selection cannot choose
    # a different secret, and the default PR event still dispatches three.
    assert job["secrets"]["COPILOT_GITHUB_TOKEN"] == (
        "${{ secrets.COPILOT_GITHUB_TOKEN }}"
    )


@pytest.mark.parametrize("mode", ["parallel", "sequential"])
def test_provider_conclusions_follow_slot_identity_and_never_unselected_jobs(
    mode: str,
) -> None:
    jobs = workflow("base-ai-review-orchestrator")["jobs"]
    for size in range(1, 5):
        for names in itertools.permutations(ALL, size):
            values = slot_values(names, mode)
            for i in range(1, 5):
                for suffix in ("p", "s"):
                    values[f"needs.review-{i}-{suffix}.result"] = f"{suffix}-slot-{i}"
            for provider in ALL:
                actual = evaluate(
                    jobs["aggregate"]["with"][f"{provider}_result"], values
                )
                expected = (
                    f"{mode[0]}-slot-{names.index(provider) + 1}"
                    if provider in names
                    else ""
                )
                assert actual == expected
                assert (
                    evaluate(jobs["aggregate"]["with"][f"{provider}_result"], {}) == ""
                )


@pytest.mark.parametrize(
    "names",
    [
        DEFAULT,
        ("copilot",),
        ("gemini", "copilot"),
        ("copilot", "gemini", "codex", "claude"),
    ],
)
@pytest.mark.parametrize("mode", ["parallel", "sequential"])
def test_actual_roster_outputs_artifacts_and_aggregate_denominator_agree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, names: tuple[str, ...], mode: str
) -> None:
    assert prepare_roster(tmp_path, json.dumps(names)).returncode == 0
    outputs = read_outputs(tmp_path / "outputs")
    selected = json.loads(outputs["reviewers"])
    values = slot_values(names, mode)
    for i in range(1, 5):
        for suffix in ("p", "s"):
            values[f"needs.review-{i}-{suffix}.result"] = (
                "failure" if i == len(names) else "success"
            )
    # The actual aggregate clear step removes every precommitted provider file,
    # including Copilot when absent from this particular selected roster.
    for provider in ALL:
        (tmp_path / f"review-{provider}.json").write_text(json.dumps(OK))
    result = shell(
        step("Clear verdict files the PR checked out", "aggregate")["run"], tmp_path, {}
    )
    assert result.returncode == 0, result.stderr
    assert not list(tmp_path.glob("review-*.json"))
    for provider in selected[:-1]:
        (tmp_path / f"review-{provider}.json").write_text(json.dumps(OK))
    # A failed artifact has the same fixed denominator as an absent artifact.
    failed = {**OK, "status": "failed", "error": "invocation_incomplete"}
    (tmp_path / f"review-{selected[-1]}.json").write_text(json.dumps(failed))
    monkeypatch.chdir(tmp_path)
    aggregate_job = workflow("base-ai-review-orchestrator")["jobs"]["aggregate"]
    for key, value in {
        "REVIEWERS": outputs["reviewers"],
        "PR_NUMBER": "42",
        "GITHUB_REPOSITORY": "owner/repo",
        "HEAD_SHA": "a" * 40,
        "PREPARE_RESULT": "success",
        "SIZE_SKIPPED": "false",
        "POLICY_SKIPPED": "false",
        "REVIEW_MODE": mode,
        "ALLOW_AUTO_APPROVE": "true",
        "BLOCK_MERGE_ON_VERDICT": "false",
        "GITHUB_OUTPUT": str(tmp_path / "aggregate-outputs"),
        "GITHUB_STEP_SUMMARY": "",
    }.items():
        monkeypatch.setenv(key, value)
    for provider in ALL:
        monkeypatch.setenv(
            f"REVIEWER_RESULT_{provider.upper()}",
            evaluate(aggregate_job["with"][f"{provider}_result"], values),
        )
    spec = importlib.util.spec_from_file_location(
        "_workflow_selected_aggregate", SCRIPTS / "aggregate_reviews.py"
    )
    assert spec is not None and spec.loader is not None
    aggregate = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, aggregate)
    spec.loader.exec_module(aggregate)
    monkeypatch.setattr(aggregate, "_head_is_stale", lambda: False)
    monkeypatch.setattr(aggregate, "fetch_round_count", lambda *args: 0)
    for method in (
        "_fetch_fold_comments",
        "_fetch_inline_posts",
        "_fetch_disposition_threads",
    ):
        monkeypatch.setattr(aggregate, method, lambda *args: [])
    post = Mock(return_value=True)
    monkeypatch.setattr(aggregate, "post_verdict", post)
    try:
        aggregate.main()
    except SystemExit as exc:
        assert exc.code == int(len(names) - 1 < (len(names) + 1) // 2)
    emitted = read_outputs(tmp_path / "aggregate-outputs")
    roster = json.loads(emitted["reviewer_roster"])
    assert roster["expected"] == selected
    assert roster["responded"] == selected[:-1]
    assert selected[-1] in roster["missing"]
    assert emitted["reviewers_expected_count"] == str(len(selected))
    assert post.call_args.kwargs["approve_quorum"] is (
        len(names) >= 3 and len(names) - 1 >= (len(names) + 1) // 2 + 1
    )


@pytest.fixture
def copilot_job(tmp_path: Path) -> dict[str, str]:
    trusted = tmp_path / "runner/ai-dev-pr-review/.github/scripts"
    trusted.parent.mkdir(parents=True)
    shutil.copytree(
        SCRIPTS, trusted, ignore=shutil.ignore_patterns("__pycache__", "tests", ".venv")
    )
    tools = tmp_path / "tools"
    tools.mkdir()
    for name in ("python", "python3"):
        launcher = tools / name
        launcher.write_text(f'#!/bin/sh\nexec "{sys.executable}" "$@"\n')
        launcher.chmod(0o755)
    return {
        "PATH": str(tools) + os.pathsep + os.environ["PATH"],
        "RUNNER_TEMP": str(tmp_path / "runner"),
        "STEP_OUTCOME": "success",
    }


@pytest.mark.parametrize("outcome", ["failure", "cancelled", "skipped", "success"])
@pytest.mark.parametrize(
    "shape", ["ok", "array", "malformed", "directory", "symlink", "absent"]
)
def test_failure_net_rejects_forged_or_unowned_results(
    tmp_path: Path, copilot_job: dict[str, str], outcome: str, shape: str
) -> None:
    verdict = tmp_path / "review-copilot.json"
    target = tmp_path / "outside.json"
    target.write_text("must not change")
    if shape == "directory":
        verdict.mkdir()
    elif shape == "symlink":
        verdict.symlink_to(target)
    elif shape != "absent":
        verdict.write_text(
            json.dumps(OK) if shape == "ok" else "[]" if shape == "array" else "{"
        )
    result = shell(
        step("Verify Copilot verdict file")["run"],
        tmp_path,
        {**copilot_job, "STEP_OUTCOME": outcome},
    )
    assert result.returncode == 0, result.stderr
    payload = json.loads(verdict.read_text())
    assert payload["status"] == (
        "ok" if outcome == "success" and shape == "ok" else "failed"
    )
    assert target.read_text() == "must not change"
    assert not verdict.is_symlink()
    assert not list(tmp_path.glob("copilot-verdict.*"))


def test_missing_account_credential_runs_real_adapter_and_normalizes_failure(
    tmp_path: Path, copilot_job: dict[str, str]
) -> None:
    (tmp_path / "review-copilot.json").write_text(json.dumps(OK))
    # These modules would execute if Python put the PR directory on sys.path.
    for name in ("json.py", "pip.py", "review_status.py", "copilot.py"):
        (tmp_path / name).write_text("raise RuntimeError('PR module executed')")
    run = step("Run Copilot review")
    assert run["env"]["COPILOT_GITHUB_TOKEN"] == "${{ secrets.COPILOT_GITHUB_TOKEN }}"
    assert not any(
        key in run["env"]
        for key in ("GH_TOKEN", "GITHUB_TOKEN", "OPENAI_API_KEY", "ANTHROPIC_API_KEY")
    )
    result = shell(run["run"], tmp_path, copilot_job)
    assert result.returncode == 0, result.stderr
    payload = json.loads((tmp_path / "review-copilot.json").read_text())
    assert payload["status"] == "failed" and payload["error"] == "credential_missing"
    assert "PR module executed" not in result.stderr
    assert (
        shell(
            step("Verify Copilot verdict file")["run"], tmp_path, copilot_job
        ).returncode
        == 0
    )
    assert json.loads((tmp_path / "review-copilot.json").read_text()) == payload


@pytest.mark.parametrize("early_exit", [False, True])
def test_completed_adapter_result_reaches_extract_upload_and_inline_entrypoint(
    tmp_path: Path, copilot_job: dict[str, str], early_exit: bool
) -> None:
    trusted = Path(copilot_job["RUNNER_TEMP"]) / "ai-dev-pr-review/.github/scripts"
    # Provider service substitute only: execute the real workflow wrapper,
    # then the real posting/extraction scripts on its adapter-owned result.
    payload = {
        **OK,
        "early_exit": early_exit,
        "status": "early_exit" if early_exit else "ok",
    }
    (trusted / "review_copilot.py").write_text(
        "from review_status import write_verdict_json\n"
        + f"write_verdict_json('review-copilot.json', {payload!r})\n"
    )
    assert (
        shell(step("Run Copilot review")["run"], tmp_path, copilot_job).returncode == 0
    )
    assert (
        shell(
            step("Verify Copilot verdict file")["run"], tmp_path, copilot_job
        ).returncode
        == 0
    )
    env = {
        **copilot_job,
        "REVIEWER": "copilot",
        "GITHUB_OUTPUT": str(tmp_path / "extracted"),
        "PR_NUMBER": "42",
        "LENS_TARGET_REPOSITORY": "owner/repo",
    }
    extracted = shell(step("Extract early exit flag")["run"], tmp_path, env)
    assert extracted.returncode == 0, extracted.stderr
    assert read_outputs(tmp_path / "extracted")["early_exit"] == str(early_exit).lower()
    posted = shell(step("Post inline comments")["run"], tmp_path, env)
    assert posted.returncode == 0, posted.stderr
    assert "copilot: no issues to post" in posted.stdout
    artifact = step("Upload review artifact")["with"]
    assert artifact["name"] == "review-${{ inputs.reviewer }}"
    assert artifact["path"] == "review-${{ inputs.reviewer }}.json"


@pytest.mark.parametrize("valid_checksum", [False, True])
def test_installer_checks_digest_before_extracting_or_executing(
    tmp_path: Path, copilot_job: dict[str, str], valid_checksum: bool
) -> None:
    archive = tmp_path / "native.tar.gz"
    with tarfile.open(archive, "w:gz") as bundle:
        executable = b"#!/bin/sh\nprintf 'fake native executable\\n'\n"
        info = tarfile.TarInfo("copilot")
        info.size = len(executable)
        bundle.addfile(info, io.BytesIO(executable))
    fake_curl = tmp_path / "tools/curl"
    fake_curl.write_text(
        '#!/bin/sh\nfor arg; do output="$arg"; done\ncp -- "$TEST_ASSET" "$output"\n'
    )
    fake_curl.chmod(0o755)
    script = step("Install trusted Copilot CLI")["run"]
    assert f"{DIGEST}  " in script
    assert (
        "https://github.com/github/copilot-cli/releases/download/v1.0.105/copilot-linux-x64.tar.gz"
        in script
    )
    if valid_checksum:
        script = script.replace(
            DIGEST, hashlib.sha256(archive.read_bytes()).hexdigest()
        )
    result = shell(
        script,
        Path(copilot_job["RUNNER_TEMP"]),
        {**copilot_job, "TEST_ASSET": str(archive)},
    )
    installed = Path(copilot_job["RUNNER_TEMP"]) / "lens-copilot-cli/copilot"
    assert (result.returncode == 0) is valid_checksum
    assert installed.exists() is valid_checksum
    if valid_checksum:
        assert installed.stat().st_mode & 0o777 == 0o700
    else:
        # Model run is skipped after this failed setup; the always() net
        # must replace even a plausible PR-provided success artifact.
        (tmp_path / "review-copilot.json").write_text(json.dumps(OK))
        net = shell(
            step("Verify Copilot verdict file")["run"],
            tmp_path,
            {**copilot_job, "STEP_OUTCOME": "skipped"},
        )
        assert net.returncode == 0
        assert (
            json.loads((tmp_path / "review-copilot.json").read_text())["status"]
            == "failed"
        )


def test_stalled_native_download_leaves_time_to_replace_a_pr_verdict(
    tmp_path: Path, copilot_job: dict[str, str]
) -> None:
    install = step("Install trusted Copilot CLI")
    assert install["timeout-minutes"] == 3
    script = install["run"]
    assert "--connect-timeout 15 --max-time 120" in script
    assert install["timeout-minutes"] * 60 > 120
    (tmp_path / "review-copilot.json").write_text(json.dumps(OK))
    # A listening TCP socket accepts the handshake but sends no HTTP response.
    # Use real curl with shorter test deadlines; no external service is called.
    with socket.socket() as stalled:
        stalled.bind(("127.0.0.1", 0))
        stalled.listen()
        local_url = f"http://127.0.0.1:{stalled.getsockname()[1]}/native.tar.gz"
        script = script.replace(
            "https://github.com/github/copilot-cli/releases/download/v1.0.105/copilot-linux-x64.tar.gz",
            local_url,
        ).replace("--proto '=https'", "--proto '=http'")
        script = script.replace(
            "--connect-timeout 15 --max-time 120",
            "--connect-timeout 0.1 --max-time 0.2",
        )
        result = shell(script, Path(copilot_job["RUNNER_TEMP"]), copilot_job)
    assert result.returncode == 28, result.stderr
    assert not (Path(copilot_job["RUNNER_TEMP"]) / "lens-copilot-cli/copilot").exists()
    net = shell(
        step("Verify Copilot verdict file")["run"],
        tmp_path,
        {**copilot_job, "STEP_OUTCOME": "skipped"},
    )
    assert net.returncode == 0, net.stderr
    verdict = json.loads((tmp_path / "review-copilot.json").read_text())
    assert verdict["status"] == "failed"
    assert verdict["error"] == "invocation_incomplete"


def test_dependency_install_does_not_import_pr_modules_or_use_pr_directory() -> None:
    install = step("Install dependencies (Gemini / Copilot)")
    assert install["working-directory"] == "${{ runner.temp }}"
    assert install["run"].startswith("python -I -m pip --isolated install ")
    for name in ("base-ai-review-single", "base-ai-review-orchestrator"):
        secrets = workflow(name)[True]["workflow_call"]["secrets"]
        assert secrets["COPILOT_GITHUB_TOKEN"]["required"] is False
        assert secrets["GOOGLE_AI_API_KEY"]["required"] is False


def test_interrupted_adapter_cannot_leave_a_usable_verdict(
    tmp_path: Path, copilot_job: dict[str, str]
) -> None:
    trusted = Path(copilot_job["RUNNER_TEMP"]) / "ai-dev-pr-review/.github/scripts"
    (trusted / "review_copilot.py").write_text(
        "import time\nfrom review_status import write_verdict_json\n"
        + f"write_verdict_json('review-copilot.json', {OK!r})\n"
        + "time.sleep(10)\n"
    )
    # The deadline kills the same process group as a runner step timeout,
    # after the adapter has written an otherwise plausible success result.
    result = shell(
        "timeout --signal=TERM --kill-after=1s 0.5s bash -e -c "
        + shlex.quote(step("Run Copilot review")["run"]),
        tmp_path,
        copilot_job,
    )
    assert result.returncode == 124
    assert json.loads((tmp_path / "review-copilot.json").read_text())["status"] == "ok"
    normalized = shell(
        step("Verify Copilot verdict file")["run"],
        tmp_path,
        {**copilot_job, "STEP_OUTCOME": "failure"},
    )
    assert normalized.returncode == 0, normalized.stderr
    assert (
        json.loads((tmp_path / "review-copilot.json").read_text())["status"] == "failed"
    )


def test_copilot_findings_reach_the_actual_inline_api_entrypoint(
    tmp_path: Path, copilot_job: dict[str, str]
) -> None:
    issues = [
        {
            "severity": "major",
            "file": "a.py",
            "line": 1,
            "description": "A concrete defect",
            "suggestion": None,
        }
    ]
    (tmp_path / "review-copilot.json").write_text(json.dumps({**OK, "issues": issues}))
    (tmp_path / "pr.diff").write_text(
        "diff --git a/a.py b/a.py\n--- a/a.py\n+++ b/a.py\n@@ -0,0 +1 @@\n+value = 1\n"
    )
    fake_gh = tmp_path / "tools/gh"
    fake_gh.write_text(
        f"#!{sys.executable}\n"
        "import json, os, sys\nfrom pathlib import Path\n"
        "args = sys.argv[1:]\n"
        "if 'graphql' in args:\n"
        " print(json.dumps({'data': {'repository': {'pullRequest': {'reviewThreads': {'nodes': [], 'pageInfo': {'hasNextPage': False}}}}}}))\n"
        "elif '--paginate' in args: print('0')\n"
        "elif args[:2] == ['pr', 'view']: print('a' * 40)\n"
        "elif '--input' in args:\n"
        " Path(os.environ['POST_CAPTURE']).write_text(sys.stdin.read())\n"
        " print('{}')\n"
        "else: raise SystemExit('unexpected fake request')\n"
    )
    fake_gh.chmod(0o755)
    capture = tmp_path / "posted.json"
    env = {
        **copilot_job,
        "REVIEWER": "copilot",
        "PR_NUMBER": "42",
        "LENS_TARGET_REPOSITORY": "owner/repo",
        "POST_CAPTURE": str(capture),
        "ROUND_CUTOFF_ENABLED": "false",
    }
    result = shell(step("Post inline comments")["run"], tmp_path, env)
    assert result.returncode == 0, result.stderr
    assert "copilot: posted 1 inline comment(s)" in result.stdout
    payload = json.loads(capture.read_text())
    assert payload["event"] == "COMMENT" and payload["commit_id"] == "a" * 40
    assert payload["comments"][0]["path"] == "a.py"
    assert "(copilot)" in payload["comments"][0]["body"]


def test_slot_auth_control_and_existing_timeouts_follow_the_actual_provider() -> None:
    jobs = workflow("base-ai-review-orchestrator")["jobs"]
    for names in itertools.permutations(ALL):
        values = {
            **slot_values(names, "parallel"),
            "inputs.review-job-timeout-minutes": 25,
            "secrets.AUTH_APP_PRIVATE_KEY": "test-auth-control-key",
        }
        for i, provider in enumerate(names, 1):
            for mode in ("p", "s"):
                job = jobs[f"review-{i}-{mode}"]
                control = provider in ("claude", "codex")
                assert evaluate(job["secrets"]["AUTH_APP_PRIVATE_KEY"], values) == (
                    "test-auth-control-key" if control else ""
                )
                assert evaluate(job["with"]["review-job-timeout-minutes"], values) == (
                    25 if control else 15
                )


def test_dependency_command_uses_isolation_and_the_runner_directory(
    tmp_path: Path, copilot_job: dict[str, str]
) -> None:
    launcher = tmp_path / "tools/python"
    capture = tmp_path / "dependency-invocation"
    launcher.write_text(
        '#!/bin/sh\npwd > "$INSTALL_CAPTURE"\nprintf "%s\\n" "$@" >> "$INSTALL_CAPTURE"\n'
    )
    launcher.chmod(0o755)
    result = shell(
        step("Install dependencies (Gemini / Copilot)")["run"],
        Path(copilot_job["RUNNER_TEMP"]),
        {**copilot_job, "INSTALL_CAPTURE": str(capture)},
    )
    assert result.returncode == 0, result.stderr
    recorded = capture.read_text().splitlines()
    assert recorded == [
        copilot_job["RUNNER_TEMP"],
        "-I",
        "-m",
        "pip",
        "--isolated",
        "install",
        "-r",
        str(
            Path(copilot_job["RUNNER_TEMP"])
            / "ai-dev-pr-review/.github/scripts/requirements.txt"
        ),
    ]
