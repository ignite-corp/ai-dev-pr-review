"""The two reviewer shims: what each CLI is given, and what comes back.

Both shims are exercised through their real `run_cli`, never by replacing
it. Replacing it is how the discarded version's
`test_every_failure_path_leaves_a_verdict_file` came to run no spawn and no
composite read at all while reporting green.
"""

from __future__ import annotations

import contextlib
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

SCRIPT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SCRIPT_DIR))

import review_claude_local as claude  # noqa: E402
import review_codex_local as codex  # noqa: E402
from local_reviewer_support import (  # noqa: E402
    DRIVER_ENV_MARKER,
    ERROR_CLI_FAILED,
    ERROR_UNPARSEABLE,
    EXIT_NOT_INSTALLED,
    EXIT_SPAWN_FAILED,
    EXIT_TIMED_OUT,
)


@pytest.fixture
def tree(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "context.md").write_text("guidelines", encoding="utf-8")
    (tmp_path / "pr.diff").write_text("+a\n", encoding="utf-8")
    return tmp_path


def fake_spawn(monkeypatch, module, **outcome):
    """Intercept at subprocess.run, which is where each shim actually spawns."""
    captured = {}

    def fake(argv, **kwargs):
        captured["argv"] = argv
        captured["input"] = kwargs.get("input")
        captured["timeout"] = kwargs.get("timeout")
        captured["env"] = kwargs.get("env")
        if "raises" in outcome:
            raise outcome["raises"]
        return subprocess.CompletedProcess(
            argv, outcome.get("code", 0), outcome.get("stdout", ""), ""
        )

    monkeypatch.setattr(module.subprocess, "run", fake)
    return captured


# ----------------------------------------------------- the timeout pair (2-D)


def test_the_python_bound_follows_the_operators_api_timeout():
    """Derived, so the pair holds at every value and not only the default.

    Pinned at 600 while API_TIMEOUT_MS stayed settable, an operator who
    raised their budget to 900000 was killed by Python 300 seconds before
    the deadline the CLI had been handed, and told "did not finish within
    600s". See design-record 2-D.
    """
    assert claude.cli_timeout_sec({"API_TIMEOUT_MS": "900000"}) == (
        900 + claude._KILL_GRACE_SEC
    )
    assert claude.cli_timeout_sec({"API_TIMEOUT_MS": "600000"}) == (
        600 + claude._KILL_GRACE_SEC
    )


def test_the_bound_always_sits_above_the_cap_the_cli_is_given():
    """Order, not size: a stalled REQUEST aborts before Python kills the CLI.

    The whole run is not bounded by this; see the _KILL_GRACE_SEC comment.
    """
    for milliseconds in ("1000", "600000", "900000"):
        seconds = int(milliseconds) // 1000
        assert claude.cli_timeout_sec({"API_TIMEOUT_MS": milliseconds}) > seconds


@pytest.mark.parametrize("bad", ["", "abc", "0", "-5", "  "])
def test_an_unusable_timeout_falls_back_to_the_composites_own_default(bad, capsys):
    """To the composite's value, never to a number written in the shim."""
    composite = int(claude._action()["inputs"]["api_timeout_ms"]["default"])
    assert claude.cli_timeout_sec({"API_TIMEOUT_MS": bad}) == (
        composite // 1000 + claude._KILL_GRACE_SEC
    )
    assert "not a positive integer" in capsys.readouterr().err


def test_the_composite_is_read_rather_than_restated():
    """Both env values are one `inputs.` reference, resolved to its default."""
    env = claude.cli_env()
    assert set(env) == {"API_TIMEOUT_MS", "CLAUDE_STREAM_IDLE_TIMEOUT_MS"}
    assert all(value.isdigit() for value in env.values())
    assert claude.allowed_tools()


def test_a_folded_composite_value_resolves_instead_of_leaking_the_expression(
    tmp_path, monkeypatch
):
    """`API_TIMEOUT_MS: >` in the composite appends a trailing newline.

    Unresolved, the literal `${{ ... }}` is what reaches the environment the
    CLI runs with, and the two keys then fail differently: API_TIMEOUT_MS
    degrades visibly -- cli_timeout_sec fails the int(), warns, falls back to
    the composite default -- while CLAUDE_STREAM_IDLE_TIMEOUT_MS is validated
    nowhere, so the CLI silently keeps its own 180000 ms watchdog and aborts
    the slow review cli_env exists to keep alive (AT-1601).
    """
    folded = tmp_path / "action.yml"
    folded.write_text(
        claude.ACTION_YML.read_text(encoding="utf-8").replace(
            "${{ inputs.api_timeout_ms }}",
            ">\n          ${{ inputs.api_timeout_ms }}",
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(claude, "ACTION_YML", folded)
    monkeypatch.delenv("API_TIMEOUT_MS", raising=False)
    monkeypatch.delenv("CLAUDE_STREAM_IDLE_TIMEOUT_MS", raising=False)

    default = claude._action()["inputs"]["api_timeout_ms"]["default"]
    assert default.isdigit()
    environ = claude.cli_environ()
    assert environ["API_TIMEOUT_MS"] == default
    assert environ["CLAUDE_STREAM_IDLE_TIMEOUT_MS"] == default


def test_a_value_that_is_more_than_a_reference_is_left_to_the_runner(
    tmp_path, monkeypatch
):
    """Only a whole-value reference is resolvable; the rest is exported as
    written rather than silently losing the part around it."""
    composed = tmp_path / "action.yml"
    composed.write_text(
        claude.ACTION_YML.read_text(encoding="utf-8").replace(
            "CLAUDE_STREAM_IDLE_TIMEOUT_MS: ${{ inputs.api_timeout_ms }}",
            "CLAUDE_STREAM_IDLE_TIMEOUT_MS: ${{ inputs.api_timeout_ms }}0",
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(claude, "ACTION_YML", composed)
    assert (
        claude.cli_env()["CLAUDE_STREAM_IDLE_TIMEOUT_MS"]
        == "${{ inputs.api_timeout_ms }}0"
    )


def test_an_operator_export_beats_the_composite_default(tree, monkeypatch):
    """Position 3 of the documented order: the composite stands in for an
    absent runner, so it must not overwrite a value the operator set."""
    monkeypatch.setenv("API_TIMEOUT_MS", "123000")
    captured = fake_spawn(monkeypatch, claude, stdout="{}")
    claude.run_cli("prompt", "model")
    assert captured["env"]["API_TIMEOUT_MS"] == "123000"
    assert captured["timeout"] == 123 + claude._KILL_GRACE_SEC


def test_each_shim_reports_the_budget_it_actually_spends(tree, monkeypatch):
    """The driver waits on this number, so it has to be the real worst case.

    A second opinion written in the driver is what the outer bound used to
    be, and it was wrong at every API_TIMEOUT_MS above the default.
    """
    monkeypatch.setenv("API_TIMEOUT_MS", "123000")
    captured = fake_spawn(monkeypatch, claude, stdout="{}")
    claude.run_cli("prompt", "model")
    assert claude.shim_budget_sec() == captured["timeout"]

    captured = fake_spawn(monkeypatch, codex, stdout="")
    codex.run_cli("prompt", "model")
    # Spent TWICE in the worst case: once on `codex exec` above, and again
    # on extract_codex_json.py when the model answered in text.
    assert captured["timeout"] == codex._CLI_TIMEOUT_SEC
    assert codex.shim_budget_sec() == 2 * captured["timeout"]


# -------------------------------------------- codex prompt delivery (2-C)


def test_the_codex_prompt_travels_on_stdin_and_not_in_argv(tree, monkeypatch):
    """Measured on codex-cli 0.154.0; see design-record 2-C.

    With the prompt in argv it met MAX_ARG_STRLEN (131072 bytes for one
    element) and was visible in the process table. `-` tells the CLI to read
    stdin, so neither is true any more -- and the size check and exit code
    the discarded version needed for the ceiling are gone rather than
    ported.
    """
    captured = fake_spawn(monkeypatch, codex, code=0)
    prompt = "P" * 200_000
    codex.run_cli(prompt, "some-model")

    assert captured["argv"][-1] == "-"
    assert captured["input"] == prompt
    assert not any(len(str(part)) > 1000 for part in captured["argv"])


def test_the_codex_argv_keeps_the_sandbox_the_review_invocation_uses(
    tree, monkeypatch
):
    captured = fake_spawn(monkeypatch, codex, code=0)
    codex.run_cli("p", "m")
    assert "--sandbox" in captured["argv"]
    assert "workspace-write" in captured["argv"]
    assert captured["argv"][captured["argv"].index("--model") + 1] == "m"


def test_no_argv_size_ceiling_survives_in_the_codex_shim():
    """The ceiling is not worked around -- it is not reached.

    Attributes, not a text scan of the source. The first version of this
    asserted `"MAX_ARG" not in source` and failed on the docstring that
    explains why the constant is gone -- the same defect the record names:
    an assertion that reads source as text also matches prose, and a file
    can be failed by a comment describing why it is correct.
    """
    for gone in ("_MAX_ARG_BYTES", "_EXIT_PROMPT_TOO_LARGE"):
        assert not hasattr(codex, gone), gone
    assert not any(
        isinstance(value, int) and value == 131072
        for value in vars(codex).values()
    )


# ------------------------------------------------ spawn failures stay apart


@pytest.mark.parametrize("module", [claude, codex], ids=["claude", "codex"])
@pytest.mark.parametrize(
    "raised, expected",
    [
        (FileNotFoundError("no such file"), EXIT_NOT_INSTALLED),
        (PermissionError("denied"), EXIT_SPAWN_FAILED),
        (subprocess.TimeoutExpired("cli", 1), EXIT_TIMED_OUT),
    ],
)
def test_each_spawn_failure_gets_its_own_exit_code(
    tree, monkeypatch, module, raised, expected
):
    """"Not installed" and "ran for ten minutes" are different problems with
    different fixes, and both used to arrive as the same empty output."""
    fake_spawn(monkeypatch, module, raises=raised)
    assert module.run_cli("p", "m")[0] == expected


@pytest.mark.parametrize(
    "module, cli", [(claude, "claude"), (codex, "codex")], ids=["claude", "codex"]
)
@pytest.mark.parametrize("signum", [signal.SIGHUP, signal.SIGINT, signal.SIGQUIT])
def test_a_cli_killed_by_a_signal_is_not_called_uninstalled(
    tree, monkeypatch, module, cli, signum
):
    """Both shims feed the raw returncode back into the sentinel vocabulary.

    CompletedProcess.returncode is `-N` for a child killed by signal N, and
    the sentinels sat on -1, -2 and -3. Baseline (688cd0a), through the real
    run_cli and a real CLI dying on SIGHUP: review-claude.json came back
    reading "Claude review failed: the CLI is not installed or not on PATH"
    -- the operator told to fix a PATH for a CLI that had just run. Driven
    through a stand-in that kills itself rather than a stubbed returncode,
    because the value under test is the one the OS produces.
    """
    binder = tree / "bin"
    binder.mkdir(exist_ok=True)
    stand_in = binder / cli
    stand_in.write_text(
        f"#!/bin/sh\ncat > /dev/null\nkill -{int(signum)} $$\n", encoding="utf-8"
    )
    stand_in.chmod(0o755)
    monkeypatch.setenv("PATH", f"{binder}{os.pathsep}{os.environ['PATH']}")

    module.review()

    payload = json.loads(Path(module.REVIEW_FILE).read_text())
    spoken = payload["summary"] + payload["error_detail"]
    assert "not installed" not in spoken
    assert "could not be started" not in spoken
    assert "did not finish within" not in spoken
    assert str(-int(signum)) in spoken


# -------------------------------------------------- what the CLI wrote back


@pytest.mark.parametrize("module", [claude, codex], ids=["claude", "codex"])
@pytest.mark.parametrize(
    "content, expected",
    [
        (None, None),
        ("", None),
        ("not json", False),
        ("[1, 2]", False),
        # A JSON OBJECT that is not a verdict. `isinstance(payload, dict)`
        # was the whole gate, so each of these was accepted, review()
        # returned on it, and the log-extraction fallback never ran.
        ('{"note": "half a write"}', False),
        ('{"summary": "s", "issues": []}', False),
        ('{"summary": "s", "early_exit": false}', False),
        ('{"summary": 1, "early_exit": false, "issues": []}', False),
        (
            '{"summary": "s", "early_exit": false, "issues":'
            ' [{"severity": "nonsense", "description": "d", "file": null,'
            ' "line": null, "suggestion": null}]}',
            False,
        ),
        ('{"summary": "s", "early_exit": false, "issues": []}', True),
        # Accepted because the AGGREGATE accepts it: load_reviews normalizes
        # severity before it validates, so a shim that only validated would
        # refuse a review over the spelling `High` and report it unparseable.
        (
            '{"summary": "s", "early_exit": false, "issues":'
            ' [{"severity": "High", "description": "d", "file": null,'
            ' "line": null, "suggestion": null}]}',
            True,
        ),
    ],
)
def test_a_direct_write_is_classified_once(tree, module, content, expected):
    """Three answers, because the caller's message differs for each.

    A boolean made the caller recompute the absent-versus-malformed half,
    and it recomputed a weaker one: a zero-byte file -- the CLI created it
    and died, the case the fallback exists for -- was ABSENT on one side and
    malformed on the other, so the recovery was skipped.
    """
    if content is not None:
        Path(module.REVIEW_FILE).write_text(content, encoding="utf-8")
    assert module.accept_direct_write() is expected


@pytest.mark.parametrize("module", [claude, codex], ids=["claude", "codex"])
def test_an_accepted_verdict_gets_its_status_stamped(tree, module):
    """A direct write skips the extractor, where AT-1799 is otherwise
    stamped -- and a model-emitted "failed" is trusted from neither."""
    Path(module.REVIEW_FILE).write_text(
        '{"summary": "reviewed", "status": "failed",'
        ' "early_exit": false, "issues": []}',
        encoding="utf-8",
    )
    assert module.accept_direct_write() is True
    assert json.loads(Path(module.REVIEW_FILE).read_text())["status"] == "ok"


# ---------------------------------------- one question, one check (codex)


def test_the_legacy_promotion_and_the_classification_ask_the_same_question(tree):
    """A zero-byte verdict file is "nothing written" to BOTH.

    They disagreed once: the promotion tested existence, the classification
    tested size, so a zero-byte file blocked the promotion of a perfectly
    good legacy verdict. See design-record 1-5.
    """
    Path(codex.REVIEW_FILE).write_text("", encoding="utf-8")
    Path("verdict-openai.json").write_text(
        '{"summary": "real", "issues": []}', encoding="utf-8"
    )

    assert codex.verdict_file_written() is False
    codex.normalize_verdict_file()

    promoted = json.loads(Path(codex.REVIEW_FILE).read_text())
    assert promoted["summary"] == "real"
    assert promoted["early_exit"] is False


@pytest.mark.parametrize("module", [claude, codex], ids=["claude", "codex"])
def test_every_verdict_file_a_shim_writes_is_readable_by_a_human(tree, module):
    """One indentation, whatever path produced the file.

    write_verdict and the Claude shim use indent=2; baseline (84d7933): the
    Codex shim wrote a successful verdict with no indentation at all, so the
    one file an operator opens when a Codex review looks wrong was the one
    file on a single line -- while every error verdict beside it was
    indented.
    """
    Path(module.REVIEW_FILE).write_text(
        '{"summary": "s", "early_exit": false, "issues": []}', encoding="utf-8"
    )

    assert module.accept_direct_write() is True

    assert '\n  "summary"' in Path(module.REVIEW_FILE).read_text(encoding="utf-8")


def test_a_promoted_legacy_verdict_is_indented_too(tree):
    """The other json.dumps on the same path, with the same reason."""
    Path("verdict-openai.json").write_text(
        '{"summary": "real", "issues": []}', encoding="utf-8"
    )

    codex.normalize_verdict_file()

    assert '\n  "summary"' in Path(codex.REVIEW_FILE).read_text(encoding="utf-8")


def test_a_real_verdict_is_not_replaced_by_a_legacy_one(tree):
    Path(codex.REVIEW_FILE).write_text('{"summary": "this run"}', encoding="utf-8")
    Path("verdict-codex.json").write_text('{"summary": "older"}', encoding="utf-8")
    codex.normalize_verdict_file()
    assert json.loads(Path(codex.REVIEW_FILE).read_text())["summary"] == "this run"


@pytest.mark.parametrize("junk", ["not json", "[]", '"a string"'])
def test_an_unusable_legacy_file_is_not_promoted(tree, junk):
    Path("verdict-openai.json").write_text(junk, encoding="utf-8")
    codex.normalize_verdict_file()
    assert not Path(codex.REVIEW_FILE).exists()


# ------------------------------------------------- end to end, real run_cli


@pytest.mark.parametrize("module", [claude, codex], ids=["claude", "codex"])
def test_a_reviewer_whose_cli_is_missing_still_writes_a_failed_verdict(
    tree, monkeypatch, module
):
    """Through the real run_cli, so the spawn and the composite read both
    happen -- the two things a stubbed run_cli never exercised."""
    monkeypatch.setattr(module.subprocess, "run", _raise_missing)
    module.review()
    payload = json.loads(Path(module.REVIEW_FILE).read_text())
    assert payload["status"] == "failed"
    assert "not installed" in payload["error_detail"]


def _raise_missing(argv, **kwargs):
    raise FileNotFoundError(argv[0])


def test_claude_recovers_a_verdict_the_model_printed(tree, monkeypatch):
    """The CLI answered in text instead of writing the file."""
    verdict = {"summary": "s", "status": "ok", "early_exit": False, "issues": []}
    fake_spawn(
        monkeypatch, claude, stdout=json.dumps({"result": json.dumps(verdict)})
    )
    claude.review()
    assert json.loads(Path(claude.REVIEW_FILE).read_text())["summary"] == "s"


def _what_the_aggregate_reads(name: str):
    """The payload aggregate_reviews would count, read the way it reads it.

    Not the file restated: load_reviews applies its own shape test, so a
    file that merely exists is not a verdict to it. Going through it is
    what makes the assertion about the verdict the run yields.
    """
    import aggregate_reviews

    return aggregate_reviews.load_reviews()[name]


_HALF_WRITTEN = '{"note": "the CLI began a verdict and stopped"}'
_RECOVERABLE = {
    "summary": "one real finding",
    "early_exit": False,
    "issues": [
        {
            "severity": "major",
            "description": "a real bug",
            "file": "a.py",
            "line": 1,
            "suggestion": "fix it",
        }
    ],
}


def test_claude_recovers_a_verdict_a_half_written_file_used_to_suppress(
    tree, monkeypatch, capsys
):
    """A JSON object is not a verdict, and accepting one skipped the recovery.

    review() RETURNS on an accepted direct write, so the weaker gate did not
    merely mislabel the file -- it cost the run the valid verdict sitting in
    the CLI's own output. Measured on 688cd0a: the shim printed
    "review-claude.json written directly by the CLI", the file on disk was
    {"note": ..., "status": "ok"}, and aggregate_reviews.load_reviews()
    answered `None` for claude with "Malformed review payload: claude".
    """

    def fake(argv, **kwargs):
        Path(claude.REVIEW_FILE).write_text(_HALF_WRITTEN, encoding="utf-8")
        return subprocess.CompletedProcess(
            argv, 0, json.dumps({"result": json.dumps(_RECOVERABLE)}), ""
        )

    monkeypatch.setattr(claude.subprocess, "run", fake)

    claude.review()

    assert "not a usable verdict" in capsys.readouterr().err
    assert _what_the_aggregate_reads("claude") == {**_RECOVERABLE, "status": "ok"}


def test_codex_recovers_a_verdict_a_half_written_file_used_to_suppress(
    tree, monkeypatch
):
    """The same gate, the same cost, in the other shim."""
    payload = json.dumps({**_RECOVERABLE, "status": "ok"})
    _stand_in_codex(tree, monkeypatch, payload, 0)
    # The stand-in cannot write the file itself, so the half-written verdict
    # is placed the way the CLI would have left it: before the shim judges.
    Path(codex.REVIEW_FILE).write_text(_HALF_WRITTEN, encoding="utf-8")

    codex.review()

    assert _what_the_aggregate_reads("codex") == {**_RECOVERABLE, "status": "ok"}


def test_the_unusable_verdict_detail_names_the_test_that_was_applied(
    tree, monkeypatch, capsys
):
    """A JSON OBJECT is not the gate any more, so it cannot be the message.

    Baseline (e912c89): with `{"summary": "s", "issues": []}` in the verdict
    file -- an object, missing early_exit -- the operator got "its verdict
    file is not a JSON object", naming a test the code had stopped applying
    when the gate became usable_verdict. The summary and the warning above
    were both updated at the time; this one was not.
    """
    Path(claude.REVIEW_FILE).write_text(
        json.dumps({"summary": "s", "issues": []}), encoding="utf-8"
    )
    monkeypatch.setattr(
        claude.subprocess,
        "run",
        lambda argv, **kw: subprocess.CompletedProcess(argv, 0, "", ""),
    )

    claude.review()

    detail = json.loads(Path(claude.REVIEW_FILE).read_text())["error_detail"]
    assert "not a JSON object" not in detail
    assert "verdict shape" in detail


def test_both_shims_gate_on_the_aggregates_own_test(tree, monkeypatch):
    """One validation, shared -- not two that agree on the day they are written.

    The aggregate already owned the shape; a second copy per shim is a copy
    of a third party's schema, free to drift from the one that decides.
    """
    import aggregate_reviews

    from local_reviewer_support import is_valid_review, normalize_severity

    assert aggregate_reviews._is_valid_review is is_valid_review
    assert aggregate_reviews._normalize_severity is normalize_severity
    assert claude.usable_verdict is codex.usable_verdict


# ------------------------------------- what a killed CLI already printed


@pytest.fixture(scope="module")
def real_timeout():
    """A TimeoutExpired as subprocess actually raises one.

    A hand-built one carries None or str; a real one carries BYTES even
    under `text=True`, because _check_timeout raises before _communicate
    decodes (measured on CPython 3.11.9). The shims have to survive that,
    so the tests use the real article rather than a convenient stand-in.
    """
    script = (
        "import sys, time; print('PARTIAL OUTPUT'); sys.stdout.flush();"
        " time.sleep(30)"
    )
    try:
        subprocess.run(
            [sys.executable, "-c", script],
            capture_output=True,
            text=True,
            timeout=1,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        assert isinstance(exc.stdout, bytes), type(exc.stdout)
        return exc
    raise AssertionError("the probe did not time out")


@pytest.mark.parametrize("module", [claude, codex], ids=["claude", "codex"])
def test_each_shim_keeps_what_its_cli_printed_before_the_timeout(
    tree, monkeypatch, module, real_timeout
):
    """Both shims used to return "" here and throw the transcript away.

    It is on the exception, `capture_output=True` having put it there, and
    it is discarded exactly when an operator most needs it: the run log is
    written from this return value, so a timed-out review left an empty
    one and log_tail had nothing to report.
    """
    fake_spawn(monkeypatch, module, raises=real_timeout)

    returned = module.run_cli("prompt", "model")

    assert returned[0] == EXIT_TIMED_OUT
    assert "PARTIAL OUTPUT" in returned[1]


def test_a_timed_out_claude_review_leaves_the_transcript_in_the_run_log(
    tree, monkeypatch, real_timeout
):
    """The operator-visible half: the log is what the run leaves behind."""
    fake_spawn(monkeypatch, claude, raises=real_timeout)

    claude.review()

    assert "PARTIAL OUTPUT" in Path(claude.RUN_LOG).read_text(encoding="utf-8")
    payload = json.loads(Path(claude.REVIEW_FILE).read_text())
    assert "did not finish within" in payload["error_detail"]
    # Keeping the transcript changes the detail text, not the failure kind.
    assert payload["error"] == ERROR_CLI_FAILED


@pytest.mark.parametrize("module", [claude, codex], ids=["claude", "codex"])
def test_both_shims_call_a_timed_out_cli_the_same_failure(
    tree, monkeypatch, module, real_timeout
):
    """The kept transcript changes the detail text, not the failure kind.

    local_reviewer_support names the three `error` values so a shim cannot
    invent a fourth spelling, and two shims spelling one failure two ways
    is the same defect one step out. Baseline (4851510): keeping what the
    CLI printed made the Claude shim's `elif stdout:` fire, so a CLI killed
    at its bound after printing anything reached the aggregate as
    output_unparseable -- `assert 'output_unparseable' == 'cli_invocation_failed'`
    -- while the Codex shim left the identical run on cli_invocation_failed
    however much log it had kept.
    """
    real_run = subprocess.run

    def fake(argv, **kwargs):
        if Path(argv[0]).name in ("claude", "codex"):
            raise real_timeout
        return real_run(argv, **kwargs)

    monkeypatch.setattr(module.subprocess, "run", fake)

    module.review()

    payload = json.loads(Path(module.REVIEW_FILE).read_text())
    assert payload["error"] == ERROR_CLI_FAILED
    assert "did not finish within" in payload["summary"] + payload["error_detail"]
    assert "PARTIAL OUTPUT" in Path(module.RUN_LOG).read_text(encoding="utf-8")


def _stand_in_cli(tmp_path, monkeypatch, name, stdout: str, code: int) -> None:
    """Put a `name` on PATH that prints `stdout` and exits `code`.

    Not fake_spawn: that one replaces the module's whole `subprocess.run`,
    which is also how extract_from_log starts the extractor, so a stubbed
    spawn would answer for both and prove nothing about either.
    """
    binder = tmp_path / "bin"
    binder.mkdir(exist_ok=True)
    (binder / f"{name}-payload.txt").write_text(stdout, encoding="utf-8")
    stand_in = binder / name
    stand_in.write_text(
        f'#!/bin/sh\ncat > /dev/null\ncat "$(dirname "$0")/{name}-payload.txt"\n'
        f"exit {code}\n",
        encoding="utf-8",
    )
    stand_in.chmod(0o755)
    monkeypatch.setenv("PATH", f"{binder}{os.pathsep}{os.environ['PATH']}")


def _stand_in_codex(tmp_path, monkeypatch, stdout: str, code: int) -> None:
    _stand_in_cli(tmp_path, monkeypatch, "codex", stdout, code)


def test_codex_keeps_a_complete_verdict_from_a_run_that_exited_non_zero(
    tree, monkeypatch, capsys
):
    """A non-zero exit is not evidence about what the log holds.

    The comment above this fallback already settles the principle: a
    complete verdict in the run log is the only usable answer the run
    produced, and discarding it because a different artefact went wrong is
    the strictly worse half of the choice. The exit code was a second
    condition doing exactly that. Measured on the baseline, this verdict
    came back as `Codex review failed: CLI exited 3 -- {"summary": "s", ...}`
    with status "failed" and no issues.
    """
    verdict = {"summary": "s", "status": "ok", "early_exit": False, "issues": []}
    _stand_in_codex(tree, monkeypatch, json.dumps(verdict), 3)

    codex.review()

    payload = json.loads(Path(codex.REVIEW_FILE).read_text())
    assert payload["summary"] == "s"
    assert "error" not in payload
    assert "Extracted Codex verdict JSON" in capsys.readouterr().out


def test_codex_still_fails_when_a_non_zero_run_left_no_verdict(tree, monkeypatch):
    """Dropping the gate does not make a broken run look successful: the
    extractor's own exit code decides, and it finds nothing here."""
    _stand_in_codex(tree, monkeypatch, "codex: connection reset", 3)

    codex.review()

    payload = json.loads(Path(codex.REVIEW_FILE).read_text())
    assert payload["status"] == "failed"
    assert payload["error"] == "cli_invocation_failed"
    assert payload["error_detail"] == "codex: connection reset"


@pytest.mark.parametrize("module", [claude, codex], ids=["claude", "codex"])
def test_both_shims_give_a_silent_non_zero_cli_the_same_reason(
    tree, monkeypatch, module
):
    """The one field the aggregate reads for a reason, on the quiet path.

    A CLI that ran, printed nothing and exited with an ordinary non-zero
    status leaves no log to tail, and spawn_exit_reason answers "" for a
    code it does not own -- by design, so a shim adding a code of its own
    has to say what it means. Baseline (facb578): the Codex shim's
    `log_tail(log) or spawn_exit_reason(..)` was therefore "" here and the
    verdict carried `error_detail: ""`, the emptiness the comment above
    that line forbids, while the Claude shim wrote
    "claude CLI exited 3; no output produced" for the identical run. The
    defect is the DISAGREEMENT, so both shims are asserted together and on
    one sentence, not merely on non-emptiness.
    """
    name = "claude" if module is claude else "codex"
    _stand_in_cli(tree, monkeypatch, name, "", 3)

    module.review()

    payload = json.loads(Path(module.REVIEW_FILE).read_text())
    assert payload["error"] == ERROR_CLI_FAILED
    assert payload["error_detail"] == f"{name} CLI exited 3; no output produced"
    # error_verdict appends the detail to the summary, so a fallback that
    # reused the bare `reason` read "Codex review failed: CLI exited 3 --
    # CLI exited 3". The detail names the CLI, which is what keeps the two
    # halves from being the same sentence.
    assert " -- CLI exited 3" not in payload["summary"]


@pytest.mark.parametrize("module", [claude, codex], ids=["claude", "codex"])
def test_both_shims_call_a_noisy_non_zero_cli_the_same_failure(
    tree, monkeypatch, module
):
    """A CLI that printed a diagnostic and then failed is still a failed run.

    The exit status is the CLI's own statement about whether the invocation
    failed; what it happened to print is evidence about what there was to
    parse, which is a different question. Classifying on the second is how
    the two shims came apart here: the Codex shim read the status and wrote
    cli_invocation_failed, while the Claude shim's `elif stdout:` saw a
    non-empty stdout and wrote output_unparseable for the identical run.
    """
    name = "claude" if module is claude else "codex"
    _stand_in_cli(tree, monkeypatch, name, f"{name}: connection reset", 3)

    module.review()

    payload = json.loads(Path(module.REVIEW_FILE).read_text())
    assert payload["error"] == ERROR_CLI_FAILED
    # The detail wording is each shim's own -- the Codex shim tails the log,
    # the Claude shim names the exit -- but neither may leave it empty: it is
    # the one field the aggregate reads for a reason.
    assert payload["error_detail"]


@pytest.mark.parametrize("module", [claude, codex], ids=["claude", "codex"])
def test_both_shims_call_a_silent_clean_exit_the_same_failure(
    tree, monkeypatch, module
):
    """The mirror case: exit 0, nothing printed, so nothing to parse.

    The invocation did not fail -- the CLI said so itself -- and the run
    still produced no verdict, which is what output_unparseable names. The
    Claude shim's `elif stdout:` fell through to its `else` on the empty
    stdout and called a clean exit a failed invocation, while the Codex
    shim read the status and wrote output_unparseable for the same run.
    """
    name = "claude" if module is claude else "codex"
    _stand_in_cli(tree, monkeypatch, name, "", 0)

    module.review()

    payload = json.loads(Path(module.REVIEW_FILE).read_text())
    assert payload["error"] == ERROR_UNPARSEABLE
    assert payload["error_detail"]


def test_codex_names_a_hung_extractor_rather_than_a_reviewer_that_raised(
    tree, monkeypatch, capsys
):
    """Unhandled, this reached guarded_main, which wrote ERROR_CRASHED and
    "the reviewer raised before writing a verdict" -- a fault that did not
    happen. The reviewer ran; the recovery step is what timed out."""
    _stand_in_codex(tree, monkeypatch, "codex: thinking out loud", 0)
    real_run = subprocess.run
    expired = subprocess.TimeoutExpired("extract_codex_json.py", codex._CLI_TIMEOUT_SEC)

    def fake(argv, **kwargs):
        if Path(argv[-1]).name == codex.REVIEW_FILE:
            raise expired
        return real_run(argv, **kwargs)

    monkeypatch.setattr(codex.subprocess, "run", fake)

    codex.review()

    payload = json.loads(Path(codex.REVIEW_FILE).read_text())
    assert payload["error"] == ERROR_UNPARSEABLE
    assert payload["error_detail"] == "codex: thinking out loud"
    assert "extract_codex_json.py did not finish within" in capsys.readouterr().err


def test_the_shims_never_set_a_credential(tree):
    """Credentials are inherited: naming variables here would make the shim
    decide which credential counts, and that is the operator's call."""
    for name in ("review_claude_local.py", "review_codex_local.py"):
        source = (SCRIPT_DIR / name).read_text(encoding="utf-8")
        for key in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN"):
            assert f'"{key}"' not in source and f"'{key}'" not in source


# ----------------------------------------- what the kill grace actually buys


def _comment_above(source: str, name: str) -> str:
    """The `#` block immediately above a module-level assignment."""
    lines = source.splitlines()
    index = next(i for i, text in enumerate(lines) if text.startswith(f"{name} ="))
    found = []
    while index and lines[index - 1].startswith("#"):
        index -= 1
        found.insert(0, lines[index])
    return "\n".join(found)


@pytest.mark.parametrize("module", [claude, codex], ids=["claude", "codex"])
def test_a_shim_killed_by_sigterm_leaves_no_verdict_behind(tmp_path, module):
    """The measurement the kill-grace comment has to agree with.

    A stand-in CLI that hangs, a real SIGTERM at the shim, and the work
    tree inspected afterwards. Both shims die at the default disposition,
    so guarded_main's `except Exception` never runs and nothing is written
    during the grace. The reviewer is still reported: run_reviewer calls a
    reviewer that left no verdict a failure.
    """
    name = "claude" if module is claude else "codex"
    binder = tmp_path / "bin"
    binder.mkdir()
    stand_in = binder / name
    stand_in.write_text("#!/bin/sh\ntouch cli-started\nsleep 15\n", encoding="utf-8")
    stand_in.chmod(0o755)
    env = {
        **os.environ,
        "PATH": f"{binder}{os.pathsep}{os.environ['PATH']}",
        "PYTHONPATH": str(SCRIPT_DIR),
    }
    with (tmp_path / f"{name}.log").open("w", encoding="utf-8") as log:
        process = subprocess.Popen(
            [sys.executable, str(SCRIPT_DIR / f"review_{name}_local.py")],
            cwd=tmp_path,
            env=env,
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    try:
        deadline = time.monotonic() + 60
        while not (tmp_path / "cli-started").is_file():
            assert time.monotonic() < deadline, "the stand-in CLI never started"
            time.sleep(0.05)
        process.send_signal(signal.SIGTERM)
        assert process.wait(timeout=60) == -signal.SIGTERM
    finally:
        with contextlib.suppress(OSError):
            os.killpg(process.pid, signal.SIGKILL)

    assert not (tmp_path / module.REVIEW_FILE).exists()


def test_the_kill_grace_promises_only_what_a_signal_free_shim_can_do():
    """The comment said the shims write their error verdict "from the
    handler", and neither shim has a handler to write it from. A comment
    promising an operator-visible verdict that no code path can produce is
    the defect; the missing handler is not.

    Module attributes, not a text scan, for the reason
    test_no_argv_size_ceiling_survives_in_the_codex_shim gives: source read
    as text also matches prose, and both shims carry docstrings that may
    one day explain why no handler is installed. Nothing reaches
    signal.signal without the module or one of its names being bound here.
    """
    for module in (claude, codex):
        for name, value in vars(module).items():
            assert value is not signal, f"{module.__name__}.{name}"
            assert getattr(value, "__module__", None) != "signal", (
                f"{module.__name__}.{name}"
            )
    driver = (SCRIPT_DIR / "review_pr_local.py").read_text(encoding="utf-8")
    comment = _comment_above(driver, "_REVIEWER_KILL_GRACE_SEC")
    assert "Neither shim installs a signal handler" in comment


# ------------------------------- run by something other than the driver


def _run_shim_as_its_own_process(tmp_path, name, env_extra: dict[str, str]) -> str:
    """Start a shim the way a second caller would, and return its stderr.

    A real process, because the check being exercised is in `__main__` and
    the fault being reproduced is a shim STARTED by something other than
    the driver. The stand-in CLI reads its prompt and prints nothing, so
    the review fails and writes an error verdict -- which is the point:
    the environment bound is not on the success path only.

    The stand-in also dumps its OWN environment to `cli-env.txt`, because
    what the CLI receives is the thing under test and the shim's stderr is
    not evidence about it.
    """
    binder = tmp_path / "bin"
    binder.mkdir(exist_ok=True)
    stand_in = binder / name
    stand_in.write_text(
        '#!/bin/sh\ncat > /dev/null\nenv > "$PWD/cli-env.txt"\n', encoding="utf-8"
    )
    stand_in.chmod(0o755)
    (tmp_path / "context.md").write_text("guidelines", encoding="utf-8")
    (tmp_path / "pr.diff").write_text("+a\n", encoding="utf-8")
    env = {
        key: value
        for key, value in os.environ.items()
        if key != DRIVER_ENV_MARKER
    }
    env["PATH"] = f"{binder}{os.pathsep}{os.environ['PATH']}"
    env["PYTHONPATH"] = str(SCRIPT_DIR)
    env.update(env_extra)
    done = subprocess.run(
        [sys.executable, str(SCRIPT_DIR / f"review_{name}_local.py")],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert (tmp_path / f"review-{name}.json").is_file()
    return done.stderr


@pytest.mark.parametrize("name", ["claude", "codex"])
def test_a_shim_run_outside_the_driver_still_bounds_its_cli(tmp_path, name):
    """A directly-launched shim leaked every exported credential.

    Measured on e912c89 with a stand-in CLI that printed its own
    environment: GITHUB_TOKEN and AWS_SECRET_ACCESS_KEY both arrived. The
    Claude shim merged `os.environ` OVER its allowlisted base
    (`{**cli_env(), **os.environ}`) and the Codex shim passed no `env=` at
    all, so the only allowlist was the driver's and the driver was not in
    the picture -- the shim merely WARNED that it was not, which is a
    diagnostic and not a boundary. The bound is now applied in the shim,
    so it holds however the shim was started.

    A bound and NOT a refusal: re-running a shim by hand inside a run
    directory is how a review is debugged, and the verdict file this run
    still writes is asserted by the helper.
    """
    err = _run_shim_as_its_own_process(tmp_path, name, {"MY_CLOUD_SECRET": "hunter2"})
    seen = dict(
        line.split("=", 1)
        for line in (tmp_path / "cli-env.txt").read_text(encoding="utf-8").splitlines()
        if "=" in line
    )
    assert "MY_CLOUD_SECRET" not in seen
    # The complement, and it is half the test: an allowlist that forwards
    # nothing stops no leak because it stops the review. PATH is what finds
    # the CLI at all and HOME is where a logged-in one keeps its credential.
    assert seen["PATH"].startswith(str(tmp_path / "bin"))
    assert seen["HOME"] == os.environ["HOME"]
    assert "::warning::started outside the local review driver" in err
    assert f"the {name} CLI" in err


@pytest.mark.parametrize("name", ["claude", "codex"])
def test_a_shim_forwards_the_credential_the_operator_named(tmp_path, name):
    """Too NARROW breaks a working review as surely as too wide leaks one.

    $LENS_REVIEWER_ENV_PASSTHROUGH is how an operator says which credential
    their CLI authenticates with -- the shims name none themselves. The
    setting's own name is in the allowlist for this reason: withheld, a
    shim re-deriving the allowlist would drop the very variable the
    operator had asked for, and under the driver it would drop one the
    driver had already forwarded.
    """
    _run_shim_as_its_own_process(
        tmp_path,
        name,
        {
            "LENS_REVIEWER_ENV_PASSTHROUGH": "MY_MODEL_KEY",
            "MY_MODEL_KEY": "chosen-by-the-operator",
            "MY_CLOUD_SECRET": "hunter2",
        },
    )
    seen = (tmp_path / "cli-env.txt").read_text(encoding="utf-8")
    assert "MY_MODEL_KEY=chosen-by-the-operator" in seen
    assert "hunter2" not in seen


@pytest.mark.parametrize("name", ["claude", "codex"])
def test_the_marker_the_driver_sets_is_what_silences_the_warning(tmp_path, name):
    """PRESENCE is the signal, and nothing else is read from it.

    The driver assigns this name AFTER its filter and it is in no
    allowlist, so a shim that has it was started by the driver; the driver
    side of that pair is asserted in test_review_pr_local.py. Nothing is
    inferred from its absence beyond "the driver did not set it".
    """
    err = _run_shim_as_its_own_process(tmp_path, name, {DRIVER_ENV_MARKER: "1"})
    assert "started outside the local review driver" not in err
