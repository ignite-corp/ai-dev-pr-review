"""The local Codex reviewer must build the Actions prompt and fail the same way.

The prompt is the part that has to be exact: context.md, then the
unresolved-thread block, then the trusted review_prompt.md that ships in this
repository (a PR in the reviewed repository cannot reach it). The thread block
is not rebuilt here -- it is the same function the Claude reviewer uses, which
is the same text base-ai-review-single.yml writes in shell for both.

The verdict handling mirrors the workflow's: a direct write is trusted only
when it parses, a legacy file name is promoted, a text answer is recovered by
extract_codex_json.py, and every remaining path still leaves a "failed"
verdict so the aggregate counts a FAILED reviewer rather than an absent one.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
import yaml

SCRIPT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SCRIPT_DIR))

import review_codex_local  # noqa: E402
from local_review_config import workflow_defaults  # noqa: E402
from reviewer_prompts import existing_threads_block  # noqa: E402


@pytest.fixture
def workdir(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    return tmp_path


def _verdict(workdir: Path) -> dict:
    return json.loads(
        (workdir / review_codex_local.REVIEW_FILE).read_text(encoding="utf-8")
    )


def _stub_cli(monkeypatch, exit_code: int, log: str, writes=None):
    def fake_run_cli(prompt, model):
        if writes is not None:
            Path(review_codex_local.REVIEW_FILE).write_text(writes)
        return exit_code, log

    monkeypatch.setattr(review_codex_local, "run_cli", fake_run_cli)


# --- prompt -----------------------------------------------------------------


def test_prompt_is_context_then_threads_then_the_trusted_prompt(workdir):
    Path("context.md").write_text("CONTEXT BODY\n", encoding="utf-8")
    threads = json.dumps([{"path": "a.py", "status": "unresolved", "body": "x"}])
    prompt = review_codex_local.build_prompt("1", threads)

    trusted = review_codex_local.TRUSTED_PROMPT.read_text(encoding="utf-8")
    assert prompt.startswith("CONTEXT BODY\n")
    assert prompt.endswith(trusted)
    assert existing_threads_block("1", threads) in prompt
    assert prompt.index("Existing review threads") < prompt.index(trusted)


def test_prompt_without_threads_omits_the_block(workdir):
    Path("context.md").write_text("CONTEXT BODY\n", encoding="utf-8")
    prompt = review_codex_local.build_prompt("0", "")
    assert "Existing review threads" not in prompt


def test_prompt_survives_a_missing_context(workdir):
    prompt = review_codex_local.build_prompt("0", "")
    assert prompt == review_codex_local.TRUSTED_PROMPT.read_text(encoding="utf-8")


def test_model_is_the_workflow_default_when_unset(workdir, monkeypatch):
    monkeypatch.delenv("CODEX_MODEL", raising=False)
    seen: dict[str, str] = {}

    def fake_run_cli(prompt, model):
        seen["model"] = model
        return 0, ""

    monkeypatch.setattr(review_codex_local, "run_cli", fake_run_cli)
    review_codex_local.main()
    assert seen["model"] == workflow_defaults()["CODEX_MODEL"]


# --- verdict handling -------------------------------------------------------


def test_a_direct_write_is_kept_and_stamped(workdir, monkeypatch):
    _stub_cli(
        monkeypatch,
        0,
        "",
        writes=json.dumps({"summary": "s", "early_exit": True, "issues": []}),
    )
    review_codex_local.main()
    # stamp_model_status re-derives a missing status from early_exit.
    assert _verdict(workdir)["status"] == "early_exit"


def test_a_model_emitted_failed_status_is_not_trusted(workdir, monkeypatch):
    """ "failed" is reserved for infrastructure paths (AT-1799)."""
    _stub_cli(
        monkeypatch,
        0,
        "",
        writes=json.dumps(
            {"summary": "s", "status": "failed", "early_exit": False, "issues": []}
        ),
    )
    review_codex_local.main()
    assert _verdict(workdir)["status"] == "ok"


def test_a_legacy_verdict_name_is_promoted(workdir, monkeypatch):
    def fake_run_cli(prompt, model):
        Path("verdict-openai.json").write_text(
            json.dumps({"summary": "s", "issues": []})
        )
        return 0, ""

    monkeypatch.setattr(review_codex_local, "run_cli", fake_run_cli)
    review_codex_local.main()
    verdict = _verdict(workdir)
    assert verdict["summary"] == "s"
    assert verdict["early_exit"] is False


def test_a_text_answer_is_recovered_from_the_run_log(workdir, monkeypatch):
    review = {"summary": "found one", "early_exit": False, "issues": []}
    _stub_cli(monkeypatch, 0, f"thinking...\n{json.dumps(review)}\ndone\n")
    review_codex_local.main()
    assert _verdict(workdir)["summary"] == "found one"


def test_output_without_a_verdict_fails_loudly(workdir, monkeypatch):
    _stub_cli(monkeypatch, 0, "I had trouble reading the diff.\n")
    review_codex_local.main()
    verdict = _verdict(workdir)
    assert verdict["status"] == "failed"
    assert verdict["error"] == "output_unparseable"


def test_a_nonzero_exit_fails_loudly_with_the_last_log_line(workdir, monkeypatch):
    _stub_cli(monkeypatch, 3, "starting\n\x1b[31mfatal: no credentials\x1b[0m\n")
    review_codex_local.main()
    verdict = _verdict(workdir)
    assert verdict["status"] == "failed"
    assert verdict["error"] == "cli_invocation_failed"
    assert verdict["error_detail"] == "fatal: no credentials"
    assert "exited 3" in verdict["summary"]


def test_an_empty_verdict_file_falls_through_to_run_log_recovery(workdir, monkeypatch):
    """A zero-byte file is not a direct write -- it is the crash case.

    The CLI creating review-codex.json and dying before writing a byte is
    exactly what the run-log fallback exists for, and the run log here holds
    a complete verdict. Calling it "malformed" threw that verdict away.
    """
    review = {"summary": "found one", "early_exit": False, "issues": []}
    _stub_cli(monkeypatch, 0, f"thinking...\n{json.dumps(review)}\ndone\n", writes="")
    review_codex_local.main()
    assert _verdict(workdir)["summary"] == "found one"


def test_the_direct_write_is_classified_once_not_twice(workdir):
    """Absent and malformed are different answers, so they are one value.

    While this was a bool, the caller had to re-derive "was there a file at
    all", and derived it from existence alone where the helper tests
    existence and size -- the disagreement that discarded the verdict above.
    """
    absent = review_codex_local.accept_direct_write()
    Path(review_codex_local.REVIEW_FILE).write_text("")
    empty = review_codex_local.accept_direct_write()
    Path(review_codex_local.REVIEW_FILE).write_text("{not json")
    malformed = review_codex_local.accept_direct_write()
    Path(review_codex_local.REVIEW_FILE).write_text(
        json.dumps({"summary": "s", "early_exit": False, "issues": []})
    )
    accepted = review_codex_local.accept_direct_write()
    assert (absent, empty) == (
        review_codex_local.DirectWrite.ABSENT,
        review_codex_local.DirectWrite.ABSENT,
    )
    assert malformed is review_codex_local.DirectWrite.MALFORMED
    assert accepted is review_codex_local.DirectWrite.ACCEPTED


SINGLE_YML = SCRIPT_DIR.parents[0] / "workflows" / "base-ai-review-single.yml"


def test_a_legacy_verdict_is_promoted_past_a_zero_byte_file(workdir, monkeypatch):
    """The promotion asked the same question with a weaker test.

    `is_file()` here against existence-and-size there: a zero-byte
    review-codex.json -- the very case the classification exists for --
    blocked the promotion of a good legacy verdict, which was then lost.
    """

    def fake_run_cli(prompt, model):
        Path(review_codex_local.REVIEW_FILE).write_text("")
        Path("verdict-openai.json").write_text(
            json.dumps({"summary": "legacy verdict kept", "issues": []})
        )
        return 0, "nothing useful here\n"

    monkeypatch.setattr(review_codex_local, "run_cli", fake_run_cli)
    review_codex_local.main()
    assert _verdict(workdir)["summary"] == "legacy verdict kept"


def test_one_test_decides_whether_a_verdict_has_been_written(workdir):
    """Both callers ask verdict_file_written(), so neither can disagree."""
    assert not review_codex_local.verdict_file_written()
    Path(review_codex_local.REVIEW_FILE).write_text("")
    assert not review_codex_local.verdict_file_written()
    Path(review_codex_local.REVIEW_FILE).write_text("{}")
    assert review_codex_local.verdict_file_written()


def test_a_malformed_write_still_falls_back_to_the_run_log(workdir, monkeypatch):
    """The sibling shim recovers here; discarding the log is the worse half.

    Parity cannot settle it -- the workflow never validates the JSON, so it
    has no behaviour for this case -- but a complete verdict sitting in the
    run log is the only usable answer the run produced.
    """
    review = {"summary": "found one", "early_exit": False, "issues": []}
    _stub_cli(
        monkeypatch,
        0,
        f"thinking...\n{json.dumps(review)}\ndone\n",
        writes="{truncated",
    )
    review_codex_local.main()
    assert _verdict(workdir)["summary"] == "found one"


def test_the_workflow_gates_the_direct_write_on_a_non_empty_file():
    """The fact the classification above mirrors, in the workflow's own words."""
    assert "if [ -s review-codex.json ]; then" in SINGLE_YML.read_text(encoding="utf-8")


def test_the_local_timeout_matches_the_workflow_step():
    """A bare 600 tied to nothing was the sibling shim's defect too.

    codex takes no timeout of its own, so this constant is the whole bound
    off a runner; on one, `timeout-minutes` on the step is. Nothing would
    have noticed them diverging.
    """
    workflow = yaml.safe_load(SINGLE_YML.read_text(encoding="utf-8"))
    for step in workflow["jobs"]["review"]["steps"]:
        if step.get("name") == "Run Codex review":
            assert review_codex_local._CLI_TIMEOUT_SEC == step["timeout-minutes"] * 60
            return
    raise AssertionError("base-ai-review-single.yml has no 'Run Codex review' step")


def test_a_malformed_direct_write_fails_loudly(workdir, monkeypatch):
    _stub_cli(monkeypatch, 0, "some output", writes="{not json")
    review_codex_local.main()
    verdict = _verdict(workdir)
    assert verdict["status"] == "failed"
    assert verdict["error"] == "output_unparseable"


def test_every_cli_output_shape_leaves_a_verdict_file(workdir, monkeypatch):
    """Renamed: these are three shapes of CLI output, not three failure paths.

    Stubbing run_cli means nothing inside it, or before it, ever runs -- so
    a raise there left no verdict and this test passed anyway. The class is
    covered below.
    """
    for exit_code, log in ((-1, ""), (1, "boom"), (0, "no json here")):
        (workdir / review_codex_local.REVIEW_FILE).unlink(missing_ok=True)
        _stub_cli(monkeypatch, exit_code, log)
        review_codex_local.main()
        assert _verdict(workdir)["status"] == "failed"


def test_a_raise_no_handler_names_still_leaves_a_verdict(workdir, monkeypatch):
    """The AT-1837 contract this module's docstring states, enforced once.

    build_prompt reads two files and neither read is guarded; the point is
    not that particular raise, but that the module owes a verdict for any of
    them, including the ones nobody has thought of yet.
    """

    def boom(*a, **k):
        raise RuntimeError("a raise no handler enumerates")

    monkeypatch.setattr(review_codex_local, "build_prompt", boom)
    review_codex_local.main()
    verdict = _verdict(workdir)
    assert verdict["status"] == "failed"
    assert verdict["error"] == "reviewer_crashed"
    assert "RuntimeError" in verdict["error_detail"]


def test_an_uninstalled_cli_is_reported_not_raised(workdir, monkeypatch):
    def missing(*args, **kwargs):
        raise FileNotFoundError("codex")

    monkeypatch.setattr(review_codex_local.subprocess, "run", missing)
    assert review_codex_local.run_cli("prompt", "model") == (-1, "")


# --- log tail ---------------------------------------------------------------


def test_log_tail_strips_ansi_and_blank_lines():
    assert (
        review_codex_local.log_tail("a\n\x1b[1mlast line\x1b[0m\n\n\n") == "last line"
    )


def test_log_tail_is_capped():
    tail = review_codex_local.log_tail("x" * 500)
    assert len(tail) == review_codex_local._LOG_TAIL_CHARS


def test_log_tail_of_nothing_is_empty():
    assert review_codex_local.log_tail("") == ""


def test_valid_json_that_is_not_an_object_fails_loudly(workdir, monkeypatch):
    """A top-level array parses fine and then has no .get to stamp.

    The module crashed with AttributeError where every other malformed-output
    path here emits a "failed" verdict -- the tests claimed one behaviour and
    the code did another.
    """
    _stub_cli(monkeypatch, 0, "some output", writes=json.dumps(["not", "an", "object"]))
    review_codex_local.main()
    verdict = _verdict(workdir)
    assert verdict["status"] == "failed"
    assert verdict["error"] == "output_unparseable"


def test_a_legacy_file_that_is_not_an_object_is_not_promoted(workdir, monkeypatch):
    def fake_run_cli(prompt, model):
        Path("verdict-openai.json").write_text(json.dumps([1, 2, 3]))
        return 0, "output"

    monkeypatch.setattr(review_codex_local, "run_cli", fake_run_cli)
    review_codex_local.main()
    assert _verdict(workdir)["status"] == "failed"
