"""The local Claude reviewer must fail the way the Actions one fails.

AT-1837: when the Claude action died before writing a verdict, no artifact
was uploaded at all, so the aggregate saw an ABSENT reviewer -- which only
lowers the response count -- instead of a FAILED one, and reported the outage
as a benign "early-exit or no-output". The workflow's answer was to always
emit an error verdict. A local driver that quietly writes nothing would
reintroduce exactly that.

So what is pinned here is the verdict file always existing, the status being
"failed" (never a model-emitted one) on every failure path, and the
`--model` / `--allowedTools` values coming from the composite action rather
than from a second copy in this module.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

SCRIPT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SCRIPT_DIR))

import review_claude_local  # noqa: E402
from local_review_config import workflow_defaults  # noqa: E402

ACTION_YML = SCRIPT_DIR.parents[0] / "actions" / "claude-review" / "action.yml"


@pytest.fixture
def workdir(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    return tmp_path


def _verdict(workdir: Path) -> dict:
    return json.loads(
        (workdir / review_claude_local.REVIEW_FILE).read_text(encoding="utf-8")
    )


def _stub_cli(monkeypatch, exit_code: int, stdout: str, writes: dict | None = None):
    def fake_run_cli(prompt, model):
        if writes is not None:
            Path(review_claude_local.REVIEW_FILE).write_text(json.dumps(writes))
        return exit_code, stdout

    monkeypatch.setattr(review_claude_local, "run_cli", fake_run_cli)


def test_allowed_tools_come_from_the_composite_action():
    action = yaml.safe_load(ACTION_YML.read_text(encoding="utf-8"))
    assert (
        review_claude_local.allowed_tools()
        == action["inputs"]["allowed_tools"]["default"]
    )


def _composite() -> dict:
    return yaml.safe_load(ACTION_YML.read_text(encoding="utf-8"))


def _composite_step_env() -> dict:
    action = _composite()
    for step in action["runs"]["steps"]:
        if step.get("id") == "claude-review":
            return step["env"]
    raise AssertionError("the composite has no step with id 'claude-review'")


def test_cli_env_matches_the_composites_step():
    """Env parity for the surface the workflow-step tests cannot see.

    The two timeout keys live on the composite's step, not on a workflow
    step, so test_review_pr_local's per-step env comparison never looks
    here. A key added to that step and not carried locally is a setting
    that silently does nothing on this path.
    """
    assert set(review_claude_local.cli_env()) == set(_composite_step_env())


def test_cli_env_resolves_the_composites_input_defaults():
    """The step's values are `${{ inputs.api_timeout_ms }}`, not literals."""
    default = _composite()["inputs"]["api_timeout_ms"]["default"]
    assert review_claude_local.cli_env() == {
        key: default for key in _composite_step_env()
    }


def _spawn_env(monkeypatch) -> dict[str, str]:
    """Run run_cli against a stubbed spawn and return the env it was given."""
    seen: dict[str, str] = {}

    def fake_run(argv, **kwargs):
        seen.update(kwargs["env"])
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(review_claude_local.subprocess, "run", fake_run)
    review_claude_local.run_cli("prompt", "model")
    return seen


def test_the_timeout_env_reaches_the_cli_when_the_operator_sets_nothing(
    workdir, monkeypatch
):
    """AT-1601: the CLI's own watchdog defaults to 180 s without these.

    Nothing on a developer's machine sets them, so an unbuilt environment
    meant the local CLI kept the ceiling the composite exists to lift.
    """
    for key in review_claude_local.cli_env():
        monkeypatch.delenv(key, raising=False)
    seen = _spawn_env(monkeypatch)
    assert {k: seen[k] for k in review_claude_local.cli_env()} == (
        review_claude_local.cli_env()
    )
    # The operator's own environment still reaches the CLI: it carries the
    # credential the module deliberately does not choose.
    assert "PATH" in seen


def test_an_operator_set_timeout_beats_the_composites(workdir, monkeypatch):
    """The composite's value is a default, so it goes under the environment.

    docs/local-review.md states the order as process environment, then
    config file, then the value parsed out of the workflow files; merged the
    other way this discarded a choice the operator had exported, which is
    the direction the first version of this test never exercised -- it
    deleted the keys before asserting, so it passed either way.
    """
    chosen = "900000"
    for key in review_claude_local.cli_env():
        monkeypatch.setenv(key, chosen)
    seen = _spawn_env(monkeypatch)
    assert {k: seen[k] for k in review_claude_local.cli_env()} == {
        key: chosen for key in review_claude_local.cli_env()
    }


@pytest.mark.parametrize(
    "value",
    ["${{ inputs.api_timeout_ms }}0", "${{ env.API_TIMEOUT_MS }}", "${{ vars.X }}"],
)
def test_an_unresolvable_step_env_value_is_reported_not_passed_on(
    workdir, monkeypatch, value
):
    """Only the runner evaluates an expression; this module reads one form.

    Passing the rest through verbatim handed the CLI the literal text
    `${{ ... }}` as a timeout, which is exactly the silent mislabelling
    _EXIT_CONFIG_UNREADABLE was added to replace.
    """
    action = workdir / "action.yml"
    action.write_text(_composite_with_step_env(value), encoding="utf-8")
    monkeypatch.setattr(review_claude_local, "ACTION_YML", action)
    with pytest.raises(review_claude_local.CompositeUnreadable):
        review_claude_local.cli_env()

    def never(*a, **k):
        raise AssertionError("the CLI must not be started")

    monkeypatch.setattr(review_claude_local.subprocess, "run", never)
    assert review_claude_local.run_cli("prompt", "model") == (
        review_claude_local._EXIT_CONFIG_UNREADABLE,
        "",
    )


def test_a_trailing_newline_does_not_pass_as_the_bare_reference(workdir, monkeypatch):
    """`$` matches before a final newline; `fullmatch` is what "exactly" means.

    A YAML folded scalar produces exactly that value, and it was read as the
    reference itself and silently normalised -- the module reporting an env
    the composite would not have set. (The trailing-digit example raised in
    review was never accepted: `$` does not match mid-string.)
    """
    action = workdir / "action.yml"
    action.write_text(
        _composite_with_step_env(">\n          ${{ inputs.api_timeout_ms }}"),
        encoding="utf-8",
    )
    monkeypatch.setattr(review_claude_local, "ACTION_YML", action)
    with pytest.raises(review_claude_local.CompositeUnreadable):
        review_claude_local.cli_env()


def test_an_empty_default_is_not_a_value(workdir, monkeypatch):
    """`default:` with nothing after it parses to None, and str(None) is "None".

    It carries no `${{`, so the expression check passed it, and the CLI
    would have been started with API_TIMEOUT_MS=None. A missing `default`
    key raises KeyError; a present and empty one was silently usable.
    """
    action = workdir / "action.yml"
    action.write_text(
        "inputs:\n"
        "  allowed_tools:\n    default: 'W'\n"
        "  api_timeout_ms:\n    default:\n"
        "runs:\n  steps:\n"
        f"    - id: {review_claude_local._ACTION_STEP_ID}\n"
        "      env:\n        API_TIMEOUT_MS: ${{ inputs.api_timeout_ms }}\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(review_claude_local, "ACTION_YML", action)
    with pytest.raises(review_claude_local.CompositeUnreadable):
        review_claude_local.cli_env()


def test_the_real_composite_resolves_completely():
    """The guard above must not be firing on the composite we actually ship."""
    for value in review_claude_local.cli_env().values():
        assert review_claude_local._EXPRESSION_MARKER not in value


def test_the_local_timeout_matches_the_composites():
    """_CLI_TIMEOUT_SEC is pinned, so something has to notice a change."""
    default = int(_composite()["inputs"]["api_timeout_ms"]["default"])
    assert review_claude_local._CLI_TIMEOUT_SEC * 1000 == default


def test_model_is_the_workflow_default_when_unset(workdir, monkeypatch):
    monkeypatch.delenv("CLAUDE_MODEL", raising=False)
    seen: dict[str, str] = {}

    def fake_run_cli(prompt, model):
        seen["model"] = model
        return 0, ""

    monkeypatch.setattr(review_claude_local, "run_cli", fake_run_cli)
    review_claude_local.main()
    assert seen["model"] == workflow_defaults()["CLAUDE_MODEL"]


def test_a_direct_write_is_kept(workdir, monkeypatch):
    payload = {"summary": "ok", "status": "ok", "early_exit": False, "issues": []}
    _stub_cli(monkeypatch, 0, "", writes=payload)
    review_claude_local.main()
    assert _verdict(workdir) == payload


def test_a_malformed_direct_write_becomes_an_error_verdict(workdir, monkeypatch):
    def fake_run_cli(prompt, model):
        Path(review_claude_local.REVIEW_FILE).write_text("not json at all")
        return 0, ""

    monkeypatch.setattr(review_claude_local, "run_cli", fake_run_cli)
    review_claude_local.main()
    verdict = _verdict(workdir)
    assert verdict["status"] == "failed"
    assert verdict["error"] == "output_unparseable"


def test_the_verdict_is_recovered_from_the_cli_output(workdir, monkeypatch):
    review = {
        "summary": "one finding",
        "early_exit": False,
        "issues": [{"severity": "minor", "file": "a.py", "line": 1}],
    }
    _stub_cli(
        monkeypatch, 0, json.dumps({"result": f"here it is:\n{json.dumps(review)}"})
    )
    review_claude_local.main()
    verdict = _verdict(workdir)
    assert verdict["summary"] == "one finding"
    # extract_claude_review stamps the AT-1799 status contract.
    assert verdict["status"] == "ok"


def test_output_without_a_verdict_fails_loudly(workdir, monkeypatch):
    _stub_cli(monkeypatch, 0, json.dumps({"result": "I could not review this."}))
    review_claude_local.main()
    verdict = _verdict(workdir)
    assert verdict["status"] == "failed"
    assert verdict["error"] == "output_unparseable"
    assert verdict["issues"] == []


def test_a_cli_that_never_ran_fails_loudly(workdir, monkeypatch):
    _stub_cli(monkeypatch, -1, "")
    review_claude_local.main()
    verdict = _verdict(workdir)
    assert verdict["status"] == "failed"
    assert verdict["error"] == "cli_invocation_failed"


def test_every_cli_output_shape_leaves_a_verdict_file(workdir, monkeypatch):
    """The aggregate must see a FAILED reviewer, never an absent one.

    Renamed from "every failure path": these three are shapes of CLI output
    and nothing more. Stubbing run_cli means nothing inside it ever runs, so
    every raise found in there so far -- a PermissionError spawn, an
    unparseable action.yml, a renamed composite input -- passed straight
    through this test while the module exited by traceback and wrote no
    verdict. The paths this name claimed are covered below.
    """
    for exit_code, stdout in ((-1, ""), (1, "garbage"), (0, "{}")):
        (workdir / review_claude_local.REVIEW_FILE).unlink(missing_ok=True)
        _stub_cli(monkeypatch, exit_code, stdout)
        review_claude_local.main()
        assert _verdict(workdir)["status"] == "failed"


def _spawn_raises(exc):
    def apply(workdir, monkeypatch):
        def raising(*a, **k):
            raise exc

        monkeypatch.setattr(review_claude_local.subprocess, "run", raising)

    return apply


def _composite_is(text: str | None):
    """Break the composite's YAML; None removes the file entirely."""

    def apply(workdir, monkeypatch):
        action = workdir / "action.yml"
        if text is not None:
            action.write_text(text, encoding="utf-8")
        monkeypatch.setattr(review_claude_local, "ACTION_YML", action)

    return apply


def _composite_with_step_env(value: str) -> str:
    """A composite whose CLI step sets a value cli_env cannot resolve."""
    return (
        "inputs:\n"
        "  allowed_tools:\n    default: 'Write'\n"
        "  api_timeout_ms:\n    default: '600000'\n"
        "runs:\n  steps:\n"
        f"    - id: {review_claude_local._ACTION_STEP_ID}\n"
        f"      env:\n        API_TIMEOUT_MS: {value}\n"
    )


def _something_else_raises(workdir, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("a raise no handler enumerates")

    monkeypatch.setattr(review_claude_local, "build_claude_prompt", boom)


@pytest.mark.parametrize(
    "break_it",
    [
        pytest.param(_spawn_raises(FileNotFoundError("claude")), id="cli-missing"),
        pytest.param(
            _spawn_raises(PermissionError("not executable")), id="spawn-denied"
        ),
        pytest.param(
            _spawn_raises(subprocess.TimeoutExpired("claude", 600)), id="cli-hangs"
        ),
        pytest.param(
            _composite_is("inputs:\n  allowedTools:\n    default: 'Write'\n"),
            id="composite-input-renamed",
        ),
        pytest.param(_composite_is("# comment only\n"), id="composite-parses-to-none"),
        pytest.param(_composite_is("inputs: [1, 2]\n"), id="composite-inputs-a-list"),
        pytest.param(_composite_is(None), id="composite-absent"),
        pytest.param(_composite_is("inputs: {\n"), id="composite-not-yaml"),
        pytest.param(
            _composite_is(
                "inputs:\n  allowed_tools:\n    default: 'W'\nruns:\n  steps: [a, b]\n"
            ),
            id="composite-steps-are-scalars",
        ),
        pytest.param(
            _composite_is(
                "inputs:\n  allowed_tools:\n    default: 'W'\nruns:\n  steps:\n"
                "    - id: claude-review\n      env:\n        - API_TIMEOUT_MS=1\n"
            ),
            id="composite-step-env-is-a-sequence",
        ),
        pytest.param(
            _composite_is(_composite_with_step_env("${{ inputs.api_timeout_ms }}0")),
            id="step-env-interpolated",
        ),
        pytest.param(
            _composite_is(_composite_with_step_env("${{ env.API_TIMEOUT_MS }}")),
            id="step-env-other-context",
        ),
        pytest.param(_something_else_raises, id="a-raise-no-handler-names"),
    ],
)
def test_a_failure_before_the_cli_answers_still_leaves_a_verdict(
    workdir, monkeypatch, break_it
):
    """The AT-1837 contract, driven through the real run_cli.

    The last case is the point: it is not a failure mode anyone enumerated,
    and the module still owes the aggregate a verdict for it. Each fix here
    so far closed the one raise it was shown and the next raise reopened the
    hole, so main() now catches what escapes review() as well.

    The spawn is stubbed first, and each case may replace that stub: a case
    whose fault is in the composite must not reach a `claude` that happens
    to be installed on the machine running the tests.
    """
    monkeypatch.setattr(
        review_claude_local.subprocess,
        "run",
        lambda argv, **kwargs: subprocess.CompletedProcess(argv, 0, "", ""),
    )
    break_it(workdir, monkeypatch)
    review_claude_local.main()
    verdict = _verdict(workdir)
    assert verdict["status"] == "failed"
    assert verdict["issues"] == []
    assert verdict["error_detail"]


def test_the_summary_names_which_failure_it_was(workdir, monkeypatch):
    """One fixed string told the operator the CLI wrote nothing on a path
    where it wrote garbage, and on a path where it never ran at all -- the
    merge the four exit codes above exist to prevent, one frame later."""

    def wrote_garbage(prompt, model):
        Path(review_claude_local.REVIEW_FILE).write_text("{truncated")
        return 0, ""

    monkeypatch.setattr(review_claude_local, "run_cli", wrote_garbage)
    review_claude_local.main()
    wrote = _verdict(workdir)["summary"]

    (workdir / review_claude_local.REVIEW_FILE).unlink()
    _stub_cli(monkeypatch, -1, "")
    review_claude_local.main()
    never_ran = _verdict(workdir)["summary"]

    assert "no verdict file produced" not in wrote
    assert "no verdict file produced" in never_ran
    assert wrote != never_ran


def test_a_raise_no_handler_names_is_reported_as_a_crash(workdir, monkeypatch):
    _something_else_raises(workdir, monkeypatch)
    review_claude_local.main()
    verdict = _verdict(workdir)
    assert verdict["error"] == "reviewer_crashed"
    assert "RuntimeError" in verdict["error_detail"]


def test_an_unreadable_composite_is_not_reported_as_a_missing_cli(workdir, monkeypatch):
    """The operator is told which thing broke, not which thing ran last.

    A YAML fault used to return _EXIT_SPAWN_FAILED ("the CLI could not be
    started") and a missing action.yml used to return _EXIT_NOT_INSTALLED --
    the merging of distinct failures the constants above exist to prevent.
    """
    _composite_is(None)(workdir, monkeypatch)
    review_claude_local.main()
    detail = _verdict(workdir)["error_detail"]
    assert "composite action's CLI settings" in detail
    assert "not installed" not in detail
    assert "could not be started" not in detail


def test_an_uninstalled_cli_is_reported_not_raised(workdir, monkeypatch):
    def missing(*args, **kwargs):
        raise FileNotFoundError("claude")

    monkeypatch.setattr(review_claude_local.subprocess, "run", missing)
    assert review_claude_local.run_cli("prompt", "model") == (-1, "")


def test_a_direct_write_is_stamped_like_the_codex_shim(workdir, monkeypatch):
    """A direct write skips extract_claude_review, where status is stamped.

    Both shims in this pipeline take a direct write from their CLI; only one
    of them used to apply the AT-1799 contract to it.
    """
    _stub_cli(
        monkeypatch,
        0,
        "",
        writes={"summary": "s", "early_exit": True, "issues": []},
    )
    review_claude_local.main()
    assert _verdict(workdir)["status"] == "early_exit"


def test_a_model_emitted_failed_status_is_not_trusted(workdir, monkeypatch):
    """ "failed" is reserved for infrastructure paths, not model output."""
    _stub_cli(
        monkeypatch,
        0,
        "",
        writes={
            "summary": "s",
            "status": "failed",
            "early_exit": False,
            "issues": [],
        },
    )
    review_claude_local.main()
    assert _verdict(workdir)["status"] == "ok"


def test_valid_json_that_is_not_an_object_fails_loudly(workdir, monkeypatch):
    """Same defect as the Codex shim: valid JSON that is not a verdict."""
    _stub_cli(monkeypatch, 0, "some output", writes=["not", "an", "object"])
    review_claude_local.main()
    verdict = _verdict(workdir)
    assert verdict["status"] == "failed"
    assert verdict["error"] == "output_unparseable"


def test_a_missing_cli_and_a_timeout_stay_distinct(workdir, monkeypatch):
    """Warning differently and returning the same value merges them again."""
    assert (
        review_claude_local._EXIT_NOT_INSTALLED != review_claude_local._EXIT_TIMED_OUT
    )

    def missing(*a, **k):
        raise FileNotFoundError("claude")

    monkeypatch.setattr(review_claude_local.subprocess, "run", missing)
    review_claude_local.main()
    assert "not installed" in _verdict(workdir)["error_detail"]

    (workdir / review_claude_local.REVIEW_FILE).unlink()

    def hanging(*a, **k):
        raise review_claude_local.subprocess.TimeoutExpired("claude", 600)

    monkeypatch.setattr(review_claude_local.subprocess, "run", hanging)
    review_claude_local.main()
    assert "did not finish" in _verdict(workdir)["error_detail"]


def test_a_cli_that_cannot_be_started_is_not_an_escape(workdir, monkeypatch):
    """PermissionError used to leave run_cli as itself, with no verdict."""

    def denied(*a, **k):
        raise PermissionError("not executable")

    monkeypatch.setattr(review_claude_local.subprocess, "run", denied)
    review_claude_local.main()
    assert _verdict(workdir)["status"] == "failed"
