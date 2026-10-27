"""The Codex sandbox gets its own permission profile (AT-3054).

'Restrict the Codex sandbox' writes the CLI's config before the model runs:
the sandboxed shell must not inherit credentials, and must not read the
CLI's home or the runner's directory. The executed tests run that step's
body under a parent named like the runner's worker, as on a real runner,
and read back the config it wrote.
"""

from __future__ import annotations

import fnmatch
import os
import shutil
import subprocess
import tomllib
from pathlib import Path
from typing import Any

import pytest
import yaml

_SINGLE = (
    Path(__file__).resolve().parents[2] / "workflows" / "base-ai-review-single.yml"
)
_RESTRICT = "Restrict the Codex sandbox"
_AUTH = "Authenticate Codex CLI with preferred credential"
_RUN = "Run Codex review"
_WORKER = "Runner.Worker"


def _steps() -> list[dict[str, Any]]:
    doc = yaml.safe_load(_SINGLE.read_text(encoding="utf-8"))
    return doc["jobs"]["review"]["steps"]


def _index(steps: list[dict[str, Any]], name: str) -> int:
    for i, step in enumerate(steps):
        if step.get("name") == name:
            return i
    raise AssertionError(f"no step named {name!r}")


def _step(name: str) -> dict[str, Any]:
    steps = _steps()
    return steps[_index(steps, name)]


def _exec_call() -> str:
    """The `codex exec` invocation in the run step, up to its `| tee`."""
    body = str(_step(_RUN)["run"])
    call = body[body.index("codex exec") :]
    return call[: call.index("| tee")]


def _run_restrict(
    tmp_path: Path, parent_name: str
) -> tuple[subprocess.CompletedProcess[str], Path, Path]:
    """Run the step body as a child of a shell copied to <root>/bin/<parent_name>."""
    body = str(_step(_RESTRICT)["run"])
    shell = shutil.which("dash") or shutil.which("sh")
    if shell is None:
        pytest.skip("no POSIX shell to stand in for the runner's worker")
    root = tmp_path / "runner"
    (root / "bin").mkdir(parents=True)
    parent = root / "bin" / parent_name
    shutil.copy2(shell, parent)
    home = tmp_path / "home"
    (home / ".codex").mkdir(parents=True)
    env = {
        "PATH": os.environ["PATH"],
        "HOME": str(home),
        "RUNNER_TEMP": str(tmp_path / "temp"),
    }
    # The trailing `exit` keeps the parent from exec-ing into bash, so the
    # parent stays the body's $PPID.
    proc = subprocess.run(
        [str(parent), "-c", 'bash --noprofile --norc -e -c "$0"; exit $?', body],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    return proc, home / ".codex", root


def test_the_step_runs_after_login_and_before_the_model() -> None:
    steps = _steps()
    restrict = _index(steps, _RESTRICT)
    assert _index(steps, _AUTH) < restrict < _index(steps, _RUN)
    step = steps[restrict]
    assert step.get("if") == "inputs.reviewer == 'codex'"
    # A failure here must stop the job, not let the model run unrestricted.
    assert not step.get("continue-on-error")


def test_codex_exec_does_not_pass_a_sandbox_flag() -> None:
    """`--sandbox` replaces the configured profile, measured on 0.147.0."""
    exec_call = _exec_call()
    assert "--sandbox" not in exec_call and " -s " not in exec_call


def test_codex_exec_pins_the_profile_the_step_writes(tmp_path: Path) -> None:
    """Without the config, `codex exec` 0.147.0 falls back to read-only with
    the full environment; naming the profile makes it refuse to start."""
    exec_call = _exec_call()
    proc, codex_home, _ = _run_restrict(tmp_path, _WORKER)
    assert proc.returncode == 0, proc.stderr
    profile = tomllib.loads((codex_home / "config.toml").read_text(encoding="utf-8"))[
        "default_permissions"
    ]
    assert f"-c 'default_permissions=\"{profile}\"'" in exec_call


def test_the_run_step_hands_no_credential_to_the_model_process(tmp_path: Path) -> None:
    """The CLI authenticates from what the login step stored, so no key needs
    to sit in the environment of the process the sandbox is spawned from."""
    proc, codex_home, _ = _run_restrict(tmp_path, _WORKER)
    assert proc.returncode == 0, proc.stderr
    excludes = tomllib.loads((codex_home / "config.toml").read_text(encoding="utf-8"))[
        "shell_environment_policy"
    ]["exclude"]
    names = list(_step(_RUN).get("env", {}))
    assert [n for n in names if any(fnmatch.fnmatchcase(n, p) for p in excludes)] == []


def test_the_config_selects_a_profile_that_extends_workspace_write(
    tmp_path: Path,
) -> None:
    proc, codex_home, _ = _run_restrict(tmp_path, _WORKER)
    assert proc.returncode == 0, proc.stderr
    config = tomllib.loads((codex_home / "config.toml").read_text(encoding="utf-8"))
    profile = config["default_permissions"]
    assert config["permissions"][profile]["extends"] == ":workspace"


@pytest.mark.parametrize(
    "name",
    ["OPENAI_API_KEY", "CODEX_API_KEY", "GH_TOKEN", "GITHUB_TOKEN", "SOME_SECRET"],
)
def test_the_sandboxed_shell_does_not_inherit_credentials(
    tmp_path: Path, name: str
) -> None:
    proc, codex_home, _ = _run_restrict(tmp_path, _WORKER)
    assert proc.returncode == 0, proc.stderr
    policy = tomllib.loads((codex_home / "config.toml").read_text(encoding="utf-8"))[
        "shell_environment_policy"
    ]
    assert policy["inherit"] in ("core", "none")
    assert any(fnmatch.fnmatchcase(name, pattern) for pattern in policy["exclude"])
    assert name not in policy.get("set", {})
    assert "include_only" not in policy


def test_the_cli_home_and_the_runner_files_are_unreadable(tmp_path: Path) -> None:
    proc, codex_home, runner = _run_restrict(tmp_path, _WORKER)
    assert proc.returncode == 0, proc.stderr
    config = tomllib.loads((codex_home / "config.toml").read_text(encoding="utf-8"))
    filesystem = config["permissions"][config["default_permissions"]]["filesystem"]
    assert filesystem.get(str(codex_home)) == "none"
    assert filesystem.get(str(runner)) == "none"
    assert filesystem.get(str(tmp_path / "temp" / "_runner_file_commands")) == "none"
    # Nothing else is narrowed or widened by this profile.
    assert len(filesystem) == 3


def test_the_step_refuses_when_the_runner_directory_is_unknown(tmp_path: Path) -> None:
    proc, codex_home, _ = _run_restrict(tmp_path, "not-the-worker")
    assert proc.returncode != 0
    assert not (codex_home / "config.toml").exists()
