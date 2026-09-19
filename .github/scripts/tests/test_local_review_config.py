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
    ORCHESTRATOR_YML,
    WORKFLOW_DIR,
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
    defaults = workflow_defaults()  # once: it globs and parses every workflow
    for name in CONSUMED:
        assert defaults[name] != ""


def test_model_defaults_are_read_not_invented(monkeypatch):
    """Change the workflow's pin and the driver follows it, with no edit here.

    The environment is cleared first because `_config().get` reads it before
    anything else (see test_environment_beats_config_file): an operator with
    CLAUDE_MODEL exported would otherwise have this test look for their value
    in the workflow and fail -- or, worse, have it pass for the wrong reason
    because their value happened to equal the workflow's default.
    """
    for name in ("CLAUDE_MODEL", "CODEX_MODEL", "GEMINI_MODEL"):
        monkeypatch.delenv(name, raising=False)
    config = _config()
    single = (WORKFLOW_DIR / "base-ai-review-single.yml").read_text(encoding="utf-8")
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
    orchestrator = ORCHESTRATOR_YML.read_text(encoding="utf-8")
    assert f'default: "{system_prompt}"' in orchestrator
    assert f'default: "{checklist}"' in orchestrator


def test_a_trailing_comment_is_not_part_of_the_value(tmp_path):
    """`CLAUDE_MODEL=x  # pinned` must pin x, not "x  # pinned".

    A value that carries its own comment reaches the CLI as a model id and
    fails there, a long way from the line that caused it.
    """
    path = tmp_path / "local-review.env"
    path.write_text(
        "\n".join(
            [
                "CLAUDE_MODEL=claude-opus-5  # pinned by ops",
                "REVIEW_MODE=sequential\t# tab before the comment",
                'BOT_LOGIN="quoted # stays"',
                "JACCARD_THRESHOLD=0.6#not-a-comment",
            ]
        ),
        encoding="utf-8",
    )
    assert read_config_file(path) == {
        "CLAUDE_MODEL": "claude-opus-5",
        "REVIEW_MODE": "sequential",
        # Quoted values keep a literal '#'.
        "BOT_LOGIN": "quoted # stays",
        # No whitespace before it, so it is part of the value, as in git config.
        "JACCARD_THRESHOLD": "0.6#not-a-comment",
    }


def test_a_reshaped_orchestrator_says_what_was_not_found(tmp_path, monkeypatch):
    """A bare KeyError names no key, in a file the reader has not opened."""
    reshaped = tmp_path / "base-ai-review-orchestrator.yml"
    reshaped.write_text("on:\n  workflow_call:\n    inputs: {}\n", encoding="utf-8")
    monkeypatch.setattr("local_review_config.ORCHESTRATOR_YML", reshaped, raising=True)
    with pytest.raises(ConfigError, match="code-review-system-prompt-path"):
        prompt_path_defaults()


def test_an_orchestrator_without_workflow_call_says_so(tmp_path, monkeypatch):
    reshaped = tmp_path / "base-ai-review-orchestrator.yml"
    reshaped.write_text("on:\n  push:\n    branches: [main]\n", encoding="utf-8")
    monkeypatch.setattr("local_review_config.ORCHESTRATOR_YML", reshaped, raising=True)
    with pytest.raises(ConfigError, match="workflow_call"):
        prompt_path_defaults()


def test_a_value_that_is_both_quoted_and_commented(tmp_path):
    """Quoting and commenting are not alternatives.

    Read as exclusive branches, `X="y"  # pinned` matched neither cleanly and
    came out as the literal `"y"` -- quotes and all -- on its way to a CLI
    that would reject it.
    """
    path = tmp_path / "local-review.env"
    path.write_text(
        "\n".join(
            [
                'BOT_LOGIN="quoted-login"  # pinned by ops',
                "REVIEW_MODE='sequential'  # single quotes too",
                'CLAUDE_MODEL="keeps # inside"',
                "GEMINI_MODEL=plain  # unquoted",
            ]
        ),
        encoding="utf-8",
    )
    assert read_config_file(path) == {
        "BOT_LOGIN": "quoted-login",
        "REVIEW_MODE": "sequential",
        "CLAUDE_MODEL": "keeps # inside",
        "GEMINI_MODEL": "plain",
    }


def test_an_unterminated_quote_is_left_alone(tmp_path):
    """Better a value that looks wrong than one silently truncated."""
    path = tmp_path / "local-review.env"
    path.write_text('CLAUDE_MODEL="unterminated\n', encoding="utf-8")
    assert read_config_file(path) == {"CLAUDE_MODEL": '"unterminated'}


def test_an_empty_default_is_refused(tmp_path, monkeypatch):
    """A present-but-empty `default:` parses as None, not as a path."""
    reshaped = tmp_path / "base-ai-review-orchestrator.yml"
    reshaped.write_text(
        "on:\n"
        "  workflow_call:\n"
        "    inputs:\n"
        "      code-review-system-prompt-path:\n"
        "        default:\n"
        "      code-review-checklist-path:\n"
        '        default: "c.md"\n',
        encoding="utf-8",
    )
    monkeypatch.setattr("local_review_config.ORCHESTRATOR_YML", reshaped, raising=True)
    with pytest.raises(ConfigError, match="which is not a path"):
        prompt_path_defaults()


def test_an_environment_value_is_stripped(monkeypatch):
    """`export X=$(...)` is the usual way a trailing newline reaches a CLI."""
    monkeypatch.setenv("CLAUDE_MODEL", "some-model\n")
    assert _config().get("CLAUDE_MODEL") == "some-model"


def test_unreadable_orchestrator_yaml_is_a_config_error(tmp_path, monkeypatch):
    bad = tmp_path / "base-ai-review-orchestrator.yml"
    bad.write_text("on: [unclosed\n", encoding="utf-8")
    monkeypatch.setattr("local_review_config.ORCHESTRATOR_YML", bad, raising=True)
    with pytest.raises(ConfigError, match="cannot read"):
        prompt_path_defaults()


def test_load_resolves_the_file_and_records_it(tmp_path, monkeypatch):
    """LocalConfig.load was the one part the suite never exercised."""
    monkeypatch.delenv("CLAUDE_MODEL", raising=False)
    path = tmp_path / "local-review.env"
    path.write_text("CLAUDE_MODEL=from-the-file\n", encoding="utf-8")

    explicit = LocalConfig.load(path)
    assert explicit.path == path
    assert explicit.get("CLAUDE_MODEL") == "from-the-file"

    monkeypatch.setenv("LENS_LOCAL_CONFIG", str(path))
    from_env = LocalConfig.load()
    assert from_env.path == path
    assert from_env.get("CLAUDE_MODEL") == "from-the-file"


def test_load_without_a_file_falls_back_to_the_workflow_defaults(tmp_path, monkeypatch):
    monkeypatch.delenv("CLAUDE_MODEL", raising=False)
    monkeypatch.setenv("LENS_LOCAL_CONFIG", str(tmp_path / "absent.env"))
    assert LocalConfig.load().get("CLAUDE_MODEL") == workflow_defaults()["CLAUDE_MODEL"]
