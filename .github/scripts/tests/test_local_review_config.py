"""The local driver's settings must be the workflows' settings.

Off Actions there is no `vars` context, so every knob is re-resolved from the
environment and a config file. The defaults are not restated in Python: they
are parsed back out of base-ai-review-*.yml, because a model pin copied into
a second place is a pin that goes stale silently -- claude-opus-4-8 was
retired while nine repositories still named it, and every one failed quietly.

So these tests check the parse, the precedence order, and that the names the
driver actually asks for are names the workflows declare a default for.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

SCRIPT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SCRIPT_DIR))

from local_review_config import (  # noqa: E402
    ConfigError,
    LocalConfig,
    prompt_path_defaults,
    read_config_file,
    workflow_defaults,
)

# Names the driver and the reviewer shims resolve through LocalConfig.
CONSUMED = (
    "PR_SIZE_LIMIT",
    "REVIEW_MODE",
    "CLAUDE_MODEL",
    "CODEX_MODEL",
    "GEMINI_MODEL",
    "BOT_LOGIN",
    "CRITICAL_THRESHOLD",
    "DEPENDABOT_CRITICAL_THRESHOLD",
    "MAJOR_CONSENSUS_OVERLAP",
    "DEPENDABOT_MAJOR_CONSENSUS_OVERLAP",
    "MAJOR_CONSENSUS_MIN",
    "JACCARD_THRESHOLD",
    "ROUND_CUTOFF_N",
    "ROUND_CUTOFF_ENABLED",
)


def _config(overrides: dict[str, str] | None = None) -> LocalConfig:
    return LocalConfig(overrides or {}, workflow_defaults())


def test_every_consumed_name_has_a_workflow_default():
    defaults = workflow_defaults()
    missing = [name for name in CONSUMED if name not in defaults]
    assert not missing, f"no `vars.X || 'default'` in the workflows for: {missing}"


def test_defaults_are_not_empty():
    """An empty default would silently mean "unset" at the reviewer."""
    for name in CONSUMED:
        assert workflow_defaults()[name] != ""


def test_model_defaults_are_read_not_invented(monkeypatch):
    """Change the workflow's pin and the driver follows it, with no edit here."""
    config = _config()
    single = (
        SCRIPT_DIR.parents[0] / "workflows" / "base-ai-review-single.yml"
    ).read_text(encoding="utf-8")
    assert f"vars.CLAUDE_MODEL || '{config.get('CLAUDE_MODEL')}'" in single
    assert f"vars.CODEX_MODEL || '{config.get('CODEX_MODEL')}'" in single
    assert f"vars.GEMINI_MODEL || '{config.get('GEMINI_MODEL')}'" in single


def test_environment_beats_config_file(monkeypatch):
    monkeypatch.setenv("CLAUDE_MODEL", "from-env")
    assert _config({"CLAUDE_MODEL": "from-file"}).get("CLAUDE_MODEL") == "from-env"


def test_config_file_beats_workflow_default(monkeypatch):
    monkeypatch.delenv("CLAUDE_MODEL", raising=False)
    assert _config({"CLAUDE_MODEL": "from-file"}).get("CLAUDE_MODEL") == "from-file"


def test_workflow_default_is_the_floor(monkeypatch):
    monkeypatch.delenv("REVIEW_MODE", raising=False)
    assert _config().get("REVIEW_MODE") == workflow_defaults()["REVIEW_MODE"]


def test_unknown_name_is_an_error_not_an_empty_string(monkeypatch):
    monkeypatch.delenv("NO_SUCH_SETTING", raising=False)
    with pytest.raises(ConfigError, match="NO_SUCH_SETTING"):
        _config().get("NO_SUCH_SETTING")


def test_is_overridden_distinguishes_operator_from_workflow(monkeypatch):
    monkeypatch.delenv("BOT_LOGIN", raising=False)
    assert not _config().is_overridden("BOT_LOGIN")
    assert _config({"BOT_LOGIN": "someone"}).is_overridden("BOT_LOGIN")
    monkeypatch.setenv("BOT_LOGIN", "someone-else")
    assert _config().is_overridden("BOT_LOGIN")


def test_get_int_rejects_a_non_integer(monkeypatch):
    monkeypatch.setenv("PR_SIZE_LIMIT", "many")
    with pytest.raises(ConfigError, match="PR_SIZE_LIMIT"):
        _config().get_int("PR_SIZE_LIMIT")


def test_config_file_parsing(tmp_path):
    path = tmp_path / "local-review.env"
    path.write_text(
        "\n".join(
            [
                "# a comment",
                "",
                "CLAUDE_MODEL=some-model",
                'BOT_LOGIN="quoted-login"',
                "REVIEW_MODE = sequential",
            ]
        ),
        encoding="utf-8",
    )
    assert read_config_file(path) == {
        "CLAUDE_MODEL": "some-model",
        "BOT_LOGIN": "quoted-login",
        "REVIEW_MODE": "sequential",
    }


def test_missing_config_file_is_not_an_error(tmp_path):
    assert read_config_file(tmp_path / "absent.env") == {}


def test_malformed_config_line_names_its_line_number(tmp_path):
    path = tmp_path / "local-review.env"
    path.write_text("CLAUDE_MODEL=ok\nnot a setting\n", encoding="utf-8")
    with pytest.raises(ConfigError, match=":2:"):
        read_config_file(path)


def test_prompt_paths_come_from_the_orchestrator_inputs():
    system_prompt, checklist = prompt_path_defaults()
    orchestrator = (
        SCRIPT_DIR.parents[0] / "workflows" / "base-ai-review-orchestrator.yml"
    ).read_text(encoding="utf-8")
    assert f'default: "{system_prompt}"' in orchestrator
    assert f'default: "{checklist}"' in orchestrator
