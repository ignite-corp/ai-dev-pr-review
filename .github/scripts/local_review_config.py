#!/usr/bin/env python3
"""Runtime configuration for the local review driver.

In Actions every knob is `${{ vars.NAME || 'default' }}`. Off Actions there
is no `vars` context, so the same names are read from the process
environment, then from a `KEY=VALUE` config file, and the defaults are
parsed back out of the workflow files rather than restated here -- a model
pin copied into a second place is a pin that goes stale silently
(claude-opus-4-8 was retired while nine repositories still named it).

`ALLOW_AUTO_APPROVE` is deliberately not among the names anything asks this
module for. That is all this module does about it -- the value is pinned off
where the aggregate's environment is built, in review_pr_local.aggregate_env,
and `test_auto_approve_is_pinned_off_whatever_the_operator_sets` is what
holds it there. Nothing here would stop a caller resolving it, so this note
says where to look rather than claiming a guarantee this file does not make.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

import yaml

WORKFLOW_DIR = Path(__file__).resolve().parent.parent / "workflows"
WORKFLOW_GLOB = "base-ai-review-*.yml"
ORCHESTRATOR_YML = WORKFLOW_DIR / "base-ai-review-orchestrator.yml"
SYSTEM_PROMPT_INPUT = "code-review-system-prompt-path"
CHECKLIST_INPUT = "code-review-checklist-path"
DEFAULT_CONFIG_PATH = Path.home() / ".config" / "lens" / "local-review.env"
CONFIG_PATH_ENV = "LENS_LOCAL_CONFIG"

# `vars.NAME || 'default'` as the workflows write it. A var used without a
# fallback (`vars.REVIEWER_APP_ID != ''`) has no default to read and is not
# collected.
_VAR_DEFAULT_RE = re.compile(
    r"vars\.(?P<name>[A-Z_][A-Z0-9_]*)\s*\|\|\s*'(?P<default>[^']*)'"
)
_CONFIG_LINE_RE = re.compile(r"^(?P<key>[A-Za-z_][A-Za-z0-9_]*)\s*=\s*(?P<value>.*)$")
# A `#` that opens a trailing comment: whitespace before it, so a `#` inside
# a value (`BOT_LOGIN=a#b`) is left alone.
_TRAILING_COMMENT_RE = re.compile(r"\s+#.*$")


class ConfigError(ValueError):
    """The config file, or the defaults parsed from the workflows, is unusable."""


def workflow_defaults() -> dict[str, str]:
    """Return every `vars.*` default the review workflows declare."""
    defaults: dict[str, str] = {}
    for path in sorted(WORKFLOW_DIR.glob(WORKFLOW_GLOB)):
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as exc:
            raise ConfigError(f"cannot read {path.name}: {exc}") from exc
        for match in _VAR_DEFAULT_RE.finditer(text):
            name = match.group("name")
            value = match.group("default")
            previous = defaults.get(name)
            if previous is not None and previous != value:
                raise ConfigError(
                    f"{name} has conflicting defaults in {WORKFLOW_GLOB}:"
                    f" {previous!r} and {value!r}"
                )
            defaults[name] = value
    if not defaults:
        raise ConfigError(f"no `vars.X || 'default'` found under {WORKFLOW_DIR}")
    return defaults


def _parse_value(value: str) -> str:
    """Strip one layer of quoting, and any comment outside it.

    Quoting and commenting are not alternatives, which is how the first
    version of this read them: a value that was both -- `X="y"  # pinned` --
    matched neither branch cleanly and came out as the literal `"y"`, quotes
    and all, on its way to a CLI that would reject it.

    A quoted value ends at its closing quote; whatever follows is a comment
    and is dropped. An unquoted one runs to a whitespace-preceded `#`, so a
    `#` inside a value (`a#b`) survives, as it does in git config.
    """
    if value[:1] in ("'", '"'):
        closing = value.find(value[0], 1)
        if closing != -1:
            return value[1:closing]
    return _TRAILING_COMMENT_RE.sub("", value).rstrip()


def read_config_file(path: Path) -> dict[str, str]:
    """Parse a `KEY=VALUE` file; missing file means no overrides."""
    if not path.is_file():
        return {}
    values: dict[str, str] = {}
    for number, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        match = _CONFIG_LINE_RE.match(line)
        if not match:
            raise ConfigError(f"{path}:{number}: expected KEY=VALUE, got {raw!r}")
        values[match.group("key")] = _parse_value(match.group("value").strip())
    return values


class LocalConfig:
    """Resolved settings: environment, then config file, then workflow default."""

    def __init__(
        self,
        overrides: dict[str, str],
        defaults: dict[str, str],
        path: Path | None = None,
    ) -> None:
        self._overrides = overrides
        self._defaults = defaults
        # Which file the settings came from, so a driver that was given one on
        # the command line can hand the same file to the reviewer subprocesses
        # instead of letting them re-resolve the default path.
        self.path = path

    @classmethod
    def load(cls, config_path: Path | None = None) -> LocalConfig:
        if config_path is None:
            configured = os.environ.get(CONFIG_PATH_ENV, "")
            config_path = Path(configured) if configured else DEFAULT_CONFIG_PATH
        return cls(read_config_file(config_path), workflow_defaults(), config_path)

    def is_overridden(self, name: str) -> bool:
        """True when the operator set this name, rather than the workflow."""
        return name in os.environ or name in self._overrides

    def get(self, name: str) -> str:
        """Return the effective value; raise when the name has no default.

        Environment values are stripped, as file values already are. A
        trailing newline from `export X=$(...)` is the usual way this goes
        wrong, and it reaches a CLI as part of a model id.
        """
        if name in os.environ:
            return os.environ[name].strip()
        if name in self._overrides:
            return self._overrides[name]
        try:
            return self._defaults[name]
        except KeyError:
            raise ConfigError(
                f"{name} is not set and the review workflows declare no default"
            ) from None

    def get_int(self, name: str) -> int:
        raw = self.get(name)
        try:
            return int(raw)
        except ValueError:
            raise ConfigError(f"Invalid {name}: {raw!r} (must be integer)") from None


def prompt_path_defaults() -> tuple[str, str]:
    """The (system prompt, checklist) paths the orchestrator defaults to.

    Every lookup is reported by name. A bare KeyError here says only that
    some key was absent, in a chain of five, in a file the reader has not
    opened -- and the whole point of reading the value out of the workflow
    is that the workflow is free to change shape underneath us.
    """
    try:
        workflow = yaml.safe_load(ORCHESTRATOR_YML.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise ConfigError(
            f"cannot read {ORCHESTRATOR_YML.name} to find the prompt paths: {exc}"
        ) from exc
    # YAML 1.1 resolves a bare `on:` key to the boolean True, so `["on"]`
    # is the KeyError, not the fix.
    try:
        inputs = workflow[True]["workflow_call"]["inputs"]
    except (KeyError, TypeError) as exc:
        raise ConfigError(
            f"{ORCHESTRATOR_YML.name} has no `on.workflow_call.inputs` section"
            f" to read the prompt paths from ({exc!r})"
        ) from exc
    paths: list[str] = []
    for name in (SYSTEM_PROMPT_INPUT, CHECKLIST_INPUT):
        try:
            value = inputs[name]["default"]
        except (KeyError, TypeError) as exc:
            raise ConfigError(
                f"{ORCHESTRATOR_YML.name} input {name!r} has no default to"
                f" read the prompt path from ({exc!r})"
            ) from exc
        # A present-but-empty `default:` parses as None, which would travel on
        # as a path and fail somewhere that cannot say where it came from.
        if not isinstance(value, str) or not value.strip():
            raise ConfigError(
                f"{ORCHESTRATOR_YML.name} input {name!r} has default"
                f" {value!r}, which is not a path"
            )
        paths.append(value)
    return paths[0], paths[1]
