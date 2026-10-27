"""Weekly API-key alert recovery is independent of intentionally unprobed tokens."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

WORKFLOW = Path(__file__).resolve().parents[2] / "workflows/consumer-health.yml"


def _step(name: str) -> dict[str, Any]:
    steps = yaml.safe_load(WORKFLOW.read_text())["jobs"]["credential-probe"]["steps"]
    return next(s for s in steps if s["name"] == name)


def _setup(tmp_path: Path) -> dict[str, str]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "curl").write_text(
        "#!/usr/bin/env python3\n"
        "import json,os,sys\n"
        "from pathlib import Path\n"
        "assert not any('test-key' in a or 'test-token' in a for a in sys.argv)\n"
        "assert 'CODEX_ACCESS_TOKEN' not in os.environ\n"
        "assert 'test-token' not in sys.stdin.read()\n"
        "status=json.loads(Path(os.environ['PROBE_STATE']).read_text())\n"
        "provider='google' if 'googleapis' in sys.argv[-1] else ('openai' if 'api.openai.com' in sys.argv[-1] else 'anthropic')\n"
        "with Path(os.environ['REQUESTS']).open('a') as f: f.write(provider+'\\n')\n"
        "print(status[provider],end='')\n"
    )
    (bin_dir / "gh").write_text(
        "#!/usr/bin/env python3\n"
        "import json,os,sys\n"
        "from pathlib import Path\n"
        "p=Path(os.environ['ISSUE_STATE'])\n"
        "state=json.loads(p.read_text()) if p.exists() else {'open':False}\n"
        "action=sys.argv[2]\n"
        "if action=='list': print('42' if state['open'] else '')\n"
        "elif action=='create': state['open']=True\n"
        "elif action=='close':\n"
        " state['open']=False\n"
        " state['closed_comment']=sys.argv[sys.argv.index('--comment')+1]\n"
        "p.write_text(json.dumps(state))\n"
    )
    for path in bin_dir.iterdir():
        path.chmod(0o755)
    return {
        "PATH": str(bin_dir) + os.pathsep + os.environ["PATH"],
        "OPENAI_API_KEY": "",
        "CODEX_TOKEN_PRESENT": "true",
        "REQUESTS": str(tmp_path / "requests"),
        "GOOGLE_AI_API_KEY": "test-key-google",
        "ANTHROPIC_API_KEY": "test-key-anthropic",
        "GITHUB_OUTPUT": str(tmp_path / "output"),
        "PROBE_STATE": str(tmp_path / "probes.json"),
        "ISSUE_STATE": str(tmp_path / "issue.json"),
        "GITHUB_SERVER_URL": "https://github.com",
        "GITHUB_REPOSITORY": "example/review",
        "GITHUB_RUN_ID": "1",
    }


def _run(name: str, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        ["bash", "-e", "-c", _step(name)["run"]],
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return result


def _cycle(env: dict[str, str], google: str) -> tuple[dict[str, str], str]:
    Path(env["PROBE_STATE"]).write_text(
        json.dumps({"google": google, "anthropic": "200", "openai": "200"})
    )
    Path(env["GITHUB_OUTPUT"]).write_text("")
    result = _run("Probe provider credentials", env)
    probe_env = _step("Probe provider credentials")["env"]
    assert "CODEX_ACCESS_TOKEN" not in probe_env
    assert probe_env["CODEX_TOKEN_PRESENT"] == "${{ secrets.CODEX_ACCESS_TOKEN != '' }}"
    output = {}
    for line in Path(env["GITHUB_OUTPUT"]).read_text().splitlines():
        if line.startswith(("flagged=", "all_ok=", "warn_count=", "unprobed_count=")):
            key, value = line.split("=", 1)
            output[key] = value
    if output["flagged"] == "1":
        _run(
            "Open or update credential alert issue",
            {**env, "SUMMARY": "GOOGLE_AI_API_KEY: HTTP 400"},
        )
    assert (
        _step("Close credential alert issue on recovery")["if"]
        == "steps.probe.outputs.all_ok == '1'"
    )
    assert (
        _step("Close credential alert issue on recovery")["env"]["UNPROBED_COUNT"]
        == "${{ steps.probe.outputs.unprobed_count }}"
    )
    if output["all_ok"] == "1":
        _run(
            "Close credential alert issue on recovery",
            {**env, "UNPROBED_COUNT": output.get("unprobed_count", "0")},
        )
    return output, result.stdout


def test_google_key_recovery_closes_its_alert_without_claiming_token_validation(
    tmp_path: Path,
) -> None:
    env = _setup(tmp_path)
    failed, _ = _cycle(env, "400")
    assert failed["flagged"] == "1" and failed["all_ok"] == "0"
    assert json.loads(Path(env["ISSUE_STATE"]).read_text())["open"]
    recovered, log = _cycle(env, "200")
    assert recovered["all_ok"] == "1"
    assert recovered["warn_count"] == "0" and recovered["unprobed_count"] == "1"
    assert "token validity is not established" in log
    assert log.count("::notice::CODEX_ACCESS_TOKEN") == 1
    assert "::warning::Codex access token" not in log
    state = json.loads(Path(env["ISSUE_STATE"]).read_text())
    assert not state["open"]
    assert "Applicable API-key probes returned HTTP 200" in state["closed_comment"]
    assert "Codex access token was not validated" in state["closed_comment"]
    assert "All credential probes returned HTTP 200" not in state["closed_comment"]
    assert "test-token" not in log + state["closed_comment"]


@pytest.mark.parametrize(
    ("token_present", "api_key"),
    [("true", ""), ("false", "test-key-api"), ("true", "test-key-api")],
)
@pytest.mark.parametrize("status", ["429", "500", "000"])
def test_transient_api_probe_unknown_keeps_the_alert_open(
    tmp_path: Path, status: str, token_present: str, api_key: str
) -> None:
    env = _setup(tmp_path)
    env.update(CODEX_TOKEN_PRESENT=token_present, OPENAI_API_KEY=api_key)
    _cycle(env, "400")
    output, log = _cycle(env, status)
    assert output["flagged"] == "0" and output["all_ok"] == "0"
    assert output["warn_count"] == "1"
    assert output["unprobed_count"] == ("1" if token_present == "true" else "0")
    assert "recovery not confirmed" in log
    assert json.loads(Path(env["ISSUE_STATE"]).read_text())["open"]


@pytest.mark.parametrize(
    ("token_present", "api_key", "flagged", "all_ok"),
    [
        ("true", "", "0", "1"),
        ("false", "test-key-api", "0", "1"),
        ("true", "test-key-api", "0", "1"),
        ("false", "", "1", "0"),
    ],
)
def test_probe_uses_boolean_token_presence_without_exporting_or_validating_token(
    tmp_path: Path,
    token_present: str,
    api_key: str,
    flagged: str,
    all_ok: str,
) -> None:
    env = _setup(tmp_path)
    env.update(CODEX_TOKEN_PRESENT=token_present, OPENAI_API_KEY=api_key)
    Path(env["PROBE_STATE"]).write_text(
        json.dumps({"google": "200", "anthropic": "200", "openai": "200"})
    )
    result = _run("Probe provider credentials", env)
    probe_env = _step("Probe provider credentials")["env"]
    assert "CODEX_ACCESS_TOKEN" not in probe_env
    assert probe_env["CODEX_TOKEN_PRESENT"] == "${{ secrets.CODEX_ACCESS_TOKEN != '' }}"
    output = Path(env["GITHUB_OUTPUT"]).read_text()
    assert f"flagged={flagged}\n" in output
    assert f"all_ok={all_ok}\n" in output
    assert "warn_count=0\n" in output
    assert f"unprobed_count={int(token_present == 'true')}\n" in output
    requests = Path(env["REQUESTS"]).read_text().splitlines()
    assert requests == (["openai"] if api_key else []) + ["google", "anthropic"]
    if token_present == "true":
        assert "token validity is not established" in result.stdout
        assert ("API fallback is unavailable" in result.stdout) == (not api_key)
    else:
        assert "CODEX_ACCESS_TOKEN intentionally unprobed" not in result.stdout
