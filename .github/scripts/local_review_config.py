#!/usr/bin/env python3
"""Runtime configuration for the local review driver.

In Actions every knob is `${{ vars.NAME || 'default' }}`. Off Actions there
is no `vars` context, so the same names are read from the process
environment, then from a `KEY=VALUE` config file, and the defaults are
parsed back out of the workflow files rather than restated here -- a model
pin copied into a second place is a pin that goes stale silently
(claude-opus-4-8 was retired while nine repositories still named it).

`ALLOW_AUTO_APPROVE` is deliberately NOT resolved through here: the local
driver has no reviewer App to mint a token for, and a reviewer cannot
approve their own PR, so the driver pins it off and posts a verdict comment.
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


class ConfigError(ValueError):
    """The config file, or the defaults parsed from the workflows, is unusable."""


def workflow_defaults() -> dict[str, str]:
    """Return every `vars.*` default the review workflows declare."""
    defaults: dict[str, str] = {}
    for path in sorted(WORKFLOW_DIR.glob(WORKFLOW_GLOB)):
        for match in _VAR_DEFAULT_RE.finditer(path.read_text(encoding="utf-8")):
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
        value = match.group("value").strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        values[match.group("key")] = value
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
        """Return the effective value; raise when the name has no default."""
        if name in os.environ:
            return os.environ[name]
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
    """The (system prompt, checklist) paths the orchestrator defaults to."""
    workflow = yaml.safe_load(ORCHESTRATOR_YML.read_text(encoding="utf-8"))
    # YAML 1.1 resolves a bare `on:` key to the boolean True, so `["on"]`
    # is the KeyError, not the fix.
    inputs = workflow[True]["workflow_call"]["inputs"]
    return inputs[SYSTEM_PROMPT_INPUT]["default"], inputs[CHECKLIST_INPUT]["default"]
