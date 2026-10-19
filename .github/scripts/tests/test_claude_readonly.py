"""The reviewer can read code but only a host step can write its verdict."""

from pathlib import Path
import json
import os
import shlex
import subprocess

import yaml


def test_claude_tools_and_configuration_sources_are_restricted() -> None:
    action = Path(__file__).resolve().parents[2] / "actions/claude-review/action.yml"
    doc = yaml.safe_load(action.read_text())
    step = next(s for s in doc["runs"]["steps"] if s.get("id") == "claude-review")
    args = shlex.split(step["with"]["claude_args"])
    assert args[args.index("--tools") + 1] == "Read,Glob,Grep"
    assert args[args.index("--setting-sources") + 1] == "user"
    assert "--strict-mcp-config" in args
    assert "--disable-slash-commands" in args
    assert "--json-schema" in args
    assert doc["inputs"]["allowed_tools"]["default"] == "Read,Glob,Grep"
    assert step["with"]["settings"] == '{"disableAllHooks":true}'
    assert (
        doc["outputs"]["structured_output"]["value"]
        == "${{ steps.claude-review.outputs.structured_output }}"
    )


def test_schema_step_preserves_json_through_cli_argument_quoting(tmp_path) -> None:
    action = Path(__file__).resolve().parents[2] / "actions/claude-review/action.yml"
    doc = yaml.safe_load(action.read_text())
    step = next(s for s in doc["runs"]["steps"] if s.get("id") == "review-schema")
    output = tmp_path / "output"
    subprocess.run(
        ["bash", "-e", "-c", step["run"]],
        check=True,
        env={
            **os.environ,
            "ACTION_PATH": str(action.parent),
            "GITHUB_OUTPUT": str(output),
        },
    )
    encoded = output.read_text().removeprefix("schema=").strip()
    (schema_text,) = shlex.split(encoded)
    schema = json.loads(schema_text)
    assert schema["properties"]["issues"]["items"]["properties"]["file"]["type"] == [
        "string",
        "null",
    ]
    # Apostrophes in canonical descriptions cross the same parsing boundary.
    assert "'ok'" in schema["properties"]["status"]["description"]
