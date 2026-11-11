"""Execute operator-selected local dispatch and its credential/provenance boundary."""

from __future__ import annotations

import argparse
import importlib.util
import sys
import json
from pathlib import Path
from unittest.mock import Mock

import pytest

import review_pr_local as driver
from local_review_config import ConfigError, LocalConfig, workflow_defaults
from local_reviewer_support import reviewer_env_allowlist
from reviewer_roster import DEFAULT_REVIEWERS, SUPPORTED_REVIEWERS

FOUR = ("copilot", "gemini", "claude", "codex")
OK = {"summary": "Reviewed", "status": "ok", "early_exit": False, "issues": []}


def config(**values: str) -> LocalConfig:
    return LocalConfig(values, workflow_defaults())


def aggregate_env(
    cfg: LocalConfig, selected: tuple[str, ...], conclusions: dict[str, str]
) -> dict[str, str]:
    return driver.aggregate_env(
        "owner/repo",
        "1",
        cfg,
        bot_login="operator",
        head_sha="a" * 40,
        pr_author="author",
        size=driver.initial_size(),
        policy=driver.initial_policy(),
        conclusions=conclusions,
        reviewers=selected,
    )


def stand_ins(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    early: bool = False,
    failed: str = "",
) -> tuple[Path, Path]:
    scripts = tmp_path / "trusted"
    scripts.mkdir()
    work = tmp_path / "run" / "review"
    work.mkdir(parents=True)
    native = tmp_path / "native-copilot"
    native.write_text("#!/bin/sh\nexit 0\n")
    native.chmod(0o700)
    monkeypatch.setenv("COPILOT_CLI_PATH", str(native))
    for name in SUPPORTED_REVIEWERS:
        payload = {**OK, "early_exit": early and name == FOUR[0]}
        if name == failed:
            payload.update(status="failed", error="credential_missing")
        script = scripts / driver.REVIEWER_SCRIPTS[name]
        script.write_text(
            "import json,os,pathlib\n"
            "with pathlib.Path('order').open('a') as f: "
            f"f.write({name!r}+'\\n')\n"
            f"pathlib.Path('review-{name}.json').write_text(json.dumps({payload!r}))\n"
        )
    monkeypatch.setattr(driver, "SCRIPT_DIR", scripts)
    monkeypatch.setattr(driver, "load_threads", lambda work: ("0", ""))
    return scripts, work


def test_config_selection_precedence_and_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("REVIEWERS", raising=False)
    assert config().reviewers() == DEFAULT_REVIEWERS
    cfg = config(REVIEWERS=json.dumps(FOUR))
    assert cfg.reviewers() == FOUR
    monkeypatch.setenv("REVIEWERS", '["codex","copilot"]')
    assert cfg.reviewers() == ("codex", "copilot")


@pytest.mark.parametrize(
    "raw", ["", "[]", "null", '["claude","claude"]', '["unknown"]', "$(touch private)"]
)
def test_invalid_roster_fails_before_pr_lookup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    raw: str,
) -> None:
    monkeypatch.setenv("REVIEWERS", raw)
    lookup = Mock(side_effect=AssertionError("PR must not be fetched"))
    monkeypatch.setattr(driver, "pre_review", lookup)
    with pytest.raises(ConfigError, match="Invalid REVIEWERS") as raised:
        driver.main(["owner/repo", "1", "--config", str(tmp_path / "absent")])
    assert raw not in str(raised.value) or not raw
    lookup.assert_not_called()


@pytest.mark.parametrize(
    "mode,early,expected",
    [
        ("parallel", False, FOUR),
        ("parallel", True, FOUR),
        ("sequential", False, FOUR),
        ("sequential", True, FOUR[:1]),
    ],
)
def test_selected_order_and_mode_preserve_roster(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
    early: bool,
    expected: tuple[str, ...],
) -> None:
    _, work = stand_ins(tmp_path, monkeypatch, early=early)
    cfg = config(REVIEW_MODE=mode)
    conclusions = driver.initial_conclusions(FOUR)
    assert driver.run_reviewers(work, cfg, conclusions, FOUR)
    assert (work / "order").read_text().splitlines() == list(expected)
    assert {name for name, result in conclusions.items() if result == "success"} == set(
        expected
    )
    env = aggregate_env(cfg, FOUR, conclusions)
    assert json.loads(env["REVIEWERS"]) == list(FOUR)
    assert env["ALLOW_AUTO_APPROVE"] == "false"


def test_failed_selected_reviewer_stays_in_real_aggregate_denominator(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, work = stand_ins(tmp_path, monkeypatch, failed="copilot")
    cfg = config(REVIEW_MODE="sequential")
    conclusions = driver.initial_conclusions(FOUR)
    assert driver.run_reviewers(work, cfg, conclusions, FOUR)
    assert (work / "order").read_text().splitlines() == list(FOUR)
    env = aggregate_env(cfg, FOUR, conclusions)
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("GITHUB_OUTPUT", str(tmp_path / "outputs"))
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", "")
    monkeypatch.chdir(work)
    spec = importlib.util.spec_from_file_location(
        "_local_four_aggregate", Path(driver.__file__).with_name("aggregate_reviews.py")
    )
    assert spec is not None and spec.loader is not None
    aggregate = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, aggregate)
    spec.loader.exec_module(aggregate)
    monkeypatch.setattr(aggregate, "_head_is_stale", lambda: False)
    monkeypatch.setattr(aggregate, "fetch_round_count", lambda *args: 0)
    for name in (
        "_fetch_fold_comments",
        "_fetch_inline_posts",
        "_fetch_disposition_threads",
    ):
        monkeypatch.setattr(aggregate, name, lambda *args: [])
    post = Mock(return_value=True)
    monkeypatch.setattr(aggregate, "post_verdict", post)
    aggregate.main()
    outputs = dict(
        line.split("=", 1) for line in (tmp_path / "outputs").read_text().splitlines()
    )
    roster = json.loads(outputs["reviewer_roster"])
    assert roster["expected"] == list(FOUR)
    assert roster["responded"] == list(FOUR[1:])
    assert "copilot" in roster["missing"]
    assert outputs["reviewers_expected_count"] == "4"
    post.assert_called_once()


@pytest.mark.parametrize("selected", [DEFAULT_REVIEWERS, ("copilot",)])
def test_unselected_artifacts_are_cleared_even_when_planted_after_prepare(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    selected: tuple[str, ...],
) -> None:
    scripts, work = stand_ins(tmp_path, monkeypatch)
    unselected = next(name for name in SUPPORTED_REVIEWERS if name not in selected)
    first = scripts / driver.REVIEWER_SCRIPTS[selected[0]]
    first.write_text(
        first.read_text()
        + f"pathlib.Path('review-{unselected}.json').write_text({json.dumps(OK)!r})\n"
    )
    cfg = config(REVIEW_MODE="parallel")
    conclusions = driver.initial_conclusions(selected)
    assert driver.run_reviewers(work, cfg, conclusions, selected)
    assert not (work / f"review-{unselected}.json").exists()
    assert set(conclusions) == set(selected)


@pytest.mark.parametrize(
    "kind", ["inside", "outside-symlink", "inside-symlink", "relative", "missing"]
)
def test_pr_cli_is_never_executed_even_for_version(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    kind: str,
) -> None:
    work = tmp_path / "run" / "review"
    work.mkdir(parents=True)
    native = work / "copilot"
    native.write_text("#!/bin/sh\ntouch executed\n")
    native.chmod(0o700)
    chosen = str(native)
    if kind == "outside-symlink":
        link = tmp_path / "operator-link"
        link.symlink_to(native)
        chosen = str(link)
    elif kind == "inside-symlink":
        trusted = tmp_path / "operator-cli"
        trusted.write_text("#!/bin/sh\ntouch executed\n")
        trusted.chmod(0o700)
        link = work / "operator-link"
        link.symlink_to(trusted)
        chosen = str(link)
    elif kind == "relative":
        chosen = "copilot"
    elif kind == "missing":
        chosen = ""
    spawned = Mock(side_effect=AssertionError("untrusted CLI must never start"))
    monkeypatch.setattr(driver.subprocess, "Popen", spawned)
    assert (
        driver.run_reviewer("copilot", work, {"COPILOT_CLI_PATH": chosen}) == "success"
    )
    verdict = json.loads((work / "review-copilot.json").read_text())
    assert verdict["status"] == "failed" and verdict["error"] == "cli_path_invalid"
    assert chosen not in verdict["summary"] or not chosen
    spawned.assert_not_called()
    assert not (work / "executed").exists()


@pytest.mark.parametrize(
    "location,link_direction",
    [
        ("clone", "none"),
        ("artifacts", "none"),
        (".", "none"),
        ("clone", "outside-in"),
        ("artifacts", "outside-in"),
        ("clone", "inside-out"),
        ("artifacts", "inside-out"),
    ],
)
def test_run_root_cli_is_never_executed_by_real_adapter(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    location: str,
    link_direction: str,
) -> None:
    run_dir = tmp_path / "run"
    work = run_dir / "review"
    work.mkdir(parents=True)
    (work / "context.md").write_text("Review the diff")
    (work / "pr.diff").write_text("")
    directory = run_dir / location
    directory.mkdir(exist_ok=True)
    marker = tmp_path / "executed"
    native = directory / "copilot"
    if link_direction == "inside-out":
        native = tmp_path / "operator-cli"
    native.write_text(
        f"#!{sys.executable}\n"
        "import pathlib\n"
        f"pathlib.Path({str(marker)!r}).touch()\n"
        "print('GitHub Copilot CLI 0.0.0')\n"
    )
    native.chmod(0o700)
    chosen = native
    if link_direction != "none":
        chosen = (
            tmp_path / "operator-link"
            if link_direction == "outside-in"
            else directory / "operator-link"
        )
        chosen.symlink_to(native)
    monkeypatch.setenv("COPILOT_CLI_PATH", str(chosen))
    monkeypatch.setenv("COPILOT_GITHUB_TOKEN", "test-account-token")
    assert (
        driver.run_reviewer(
            "copilot", work, driver.reviewer_env("copilot", config(), "0", "")
        )
        == "success"
    )
    assert not marker.exists()
    verdict = json.loads((work / "review-copilot.json").read_text())
    assert verdict["status"] == "failed" and verdict["error"] == "cli_path_invalid"


def test_real_adapter_missing_token_cannot_import_pr_python(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A trusted existing executable satisfies the driver guard. The adapter
    # reports the missing account token before making any native/service call.
    (tmp_path / "copilot.py").write_text("raise RuntimeError('PR imported')\n")
    monkeypatch.delenv("COPILOT_GITHUB_TOKEN", raising=False)
    monkeypatch.setenv("COPILOT_CLI_PATH", "/usr/bin/true")
    env = driver.reviewer_env("copilot", config(), "0", "")
    assert driver.run_reviewer("copilot", tmp_path, env) == "success"
    verdict = json.loads((tmp_path / "review-copilot.json").read_text())
    assert verdict["status"] == "failed" and verdict["error"] == "credential_missing"
    assert "PR imported" not in (tmp_path / "copilot-review.log").read_text()


def test_copilot_names_are_reserved_even_under_general_passthrough(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("COPILOT_GITHUB_TOKEN", "test-account-token")
    monkeypatch.setenv("COPILOT_CLI_PATH", "/operator/copilot")
    monkeypatch.setenv("OPENAI_API_KEY", "operator-existing-passthrough")
    cfg = config(
        LENS_REVIEWER_ENV_PASSTHROUGH="COPILOT_GITHUB_TOKEN,COPILOT_CLI_PATH,OPENAI_API_KEY"
    )
    for name in DEFAULT_REVIEWERS:
        allowed = reviewer_env_allowlist(name, cfg)
        env = driver.reviewer_env(name, cfg, "0", "")
        assert (
            "COPILOT_GITHUB_TOKEN" not in allowed and "COPILOT_CLI_PATH" not in allowed
        )
        assert "COPILOT_GITHUB_TOKEN" not in env and "COPILOT_CLI_PATH" not in env
        assert env["OPENAI_API_KEY"] == "operator-existing-passthrough"
    env = driver.reviewer_env("copilot", cfg, "0", "")
    assert env["COPILOT_GITHUB_TOKEN"] == "test-account-token"
    assert env["COPILOT_CLI_PATH"] == "/operator/copilot"
    assert "COPILOT_MODEL" not in env


@pytest.mark.parametrize("failure", ["pre-review", "size", "prepare", "review"])
def test_selected_roster_survives_every_driver_failure_boundary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    cfg = config()
    run_dir = tmp_path / "run"
    work = run_dir / "review"
    refs = driver.Refs("main", "a" * 40, "head", "author", "false", "", "", 1, 0)
    # The shape of Refs is real; the failure points below avoid all API calls.
    monkeypatch.setattr(
        driver,
        "pre_review",
        lambda *args: ("operator", refs, failure == "size", driver.initial_size()),
    )
    monkeypatch.setattr(driver, "ensure_clone", lambda *args: None)
    monkeypatch.setattr(driver, "prepare", lambda *args: None)
    monkeypatch.setattr(driver, "policy_gate", lambda *args: driver.initial_policy())
    monkeypatch.setattr(driver, "run_review_stage", lambda *args: None)
    if failure in ("pre-review", "prepare", "review"):
        name = {
            "pre-review": "pre_review",
            "prepare": "prepare",
            "review": "run_review_stage",
        }[failure]
        monkeypatch.setattr(
            driver, name, Mock(side_effect=RuntimeError("test failure"))
        )
    outcome = driver.review_pr(
        run_dir,
        run_dir / "clone",
        work,
        cfg,
        argparse.Namespace(repo="o/r", pr_number="1"),
        FOUR,
    )
    assert outcome.reviewers == FOUR
    assert tuple(outcome.conclusions) == FOUR
    env = aggregate_env(cfg, outcome.reviewers, outcome.conclusions)
    assert json.loads(env["REVIEWERS"]) == list(FOUR)


def test_file_selection_and_process_override_share_existing_config_precedence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "settings.env"
    path.write_text('REVIEWERS=["gemini","copilot","codex"]\n')
    monkeypatch.delenv("REVIEWERS", raising=False)
    cfg = LocalConfig.load(path)
    assert cfg.reviewers() == ("gemini", "copilot", "codex")
    monkeypatch.setenv("REVIEWERS", json.dumps(FOUR))
    assert cfg.reviewers() == FOUR


def test_main_freezes_selection_before_any_pr_work(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("REVIEWERS", json.dumps(FOUR))

    def fail_preparation(*args: object) -> None:
        monkeypatch.setenv("REVIEWERS", "not-json-anymore")
        raise RuntimeError("offline pre-review failure")

    monkeypatch.setattr(driver, "pre_review", fail_preparation)
    posted = Mock(return_value=1)
    monkeypatch.setattr(driver, "aggregate", posted)
    assert (
        driver.main(
            [
                "owner/repo",
                "1",
                "--config",
                str(tmp_path / "absent"),
                "--run-dir",
                str(tmp_path),
            ]
        )
        == 1
    )
    env = posted.call_args.args[1]
    assert json.loads(env["REVIEWERS"]) == list(FOUR)
    for name in FOUR:
        assert env[f"REVIEWER_RESULT_{name.upper()}"] == "skipped"


def test_account_credential_and_cli_path_are_not_read_from_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("COPILOT_GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("COPILOT_CLI_PATH", raising=False)
    cfg = config(
        COPILOT_GITHUB_TOKEN="not-a-config-secret", COPILOT_CLI_PATH="/not/from/config"
    )
    env = driver.reviewer_env("copilot", cfg, "0", "")
    assert "COPILOT_GITHUB_TOKEN" not in env
    assert "COPILOT_CLI_PATH" not in env


def test_native_probe_receives_no_account_or_other_provider_credentials(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    work = tmp_path / "run" / "review"
    work.mkdir(parents=True)
    (work / "context.md").write_text("Review the diff")
    (work / "pr.diff").write_text("")
    captured = tmp_path / "native-env.json"
    native = tmp_path / "operator-cli"
    native.write_text(
        f"#!{sys.executable}\n"
        "import json,os,pathlib,sys\n"
        f"pathlib.Path({str(captured)!r}).write_text(json.dumps(dict(os.environ)))\n"
        "assert sys.argv[1:] == ['--version']\n"
        "print('GitHub Copilot CLI 0.0.0')\n"
    )
    native.chmod(0o700)
    monkeypatch.setenv("COPILOT_CLI_PATH", str(native))
    monkeypatch.setenv("COPILOT_GITHUB_TOKEN", "test-account-token")
    monkeypatch.setenv("OPENAI_API_KEY", "test-provider-key")
    cfg = config(LENS_REVIEWER_ENV_PASSTHROUGH="OPENAI_API_KEY")
    assert (
        driver.run_reviewer(
            "copilot", work, driver.reviewer_env("copilot", cfg, "0", "")
        )
        == "success"
    )
    inherited = json.loads(captured.read_text())
    assert "COPILOT_GITHUB_TOKEN" not in inherited
    assert "OPENAI_API_KEY" not in inherited
    assert "COPILOT_CLI_PATH" not in inherited
    assert inherited["HOME"] != str(Path.home())
    verdict = json.loads((work / "review-copilot.json").read_text())
    assert verdict["error"] == "cli_version_mismatch"
    assert "test-account-token" not in (work / "copilot-review.log").read_text()


def test_missing_native_cli_preserves_selection_and_continues_dispatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, work = stand_ins(tmp_path, monkeypatch)
    monkeypatch.delenv("COPILOT_CLI_PATH", raising=False)
    conclusions = driver.initial_conclusions(FOUR)
    cfg = config(REVIEW_MODE="sequential")
    assert driver.run_reviewers(work, cfg, conclusions, FOUR)
    assert (work / "order").read_text().splitlines() == list(FOUR[1:])
    verdict = json.loads((work / "review-copilot.json").read_text())
    assert verdict["error"] == "cli_path_invalid"
    assert tuple(conclusions) == FOUR
    assert json.loads(aggregate_env(cfg, FOUR, conclusions)["REVIEWERS"]) == list(FOUR)


def test_copilot_artifacts_from_pr_are_removed_before_dispatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    work = tmp_path / "review"
    work.mkdir()
    (work / "review-copilot.json").write_text(json.dumps(OK))
    (work / "copilot-review.log").write_text("PR supplied log")
    monkeypatch.setattr(driver, "tracked_artifact_names", lambda work: [])
    driver.clean_artifacts(work)
    assert not (work / "review-copilot.json").exists()
    assert not (work / "copilot-review.log").exists()
