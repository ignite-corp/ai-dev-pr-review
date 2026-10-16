"""Tests for the base<->wrapper drift check (AT-2122).

Three layers:

1. Unit behaviour of the extractors, the config loaders and both axes, on
   small synthetic trees.

2. The live correspondence against this repo's own workflow files -- the
   base half of the check, which needs no wrapper checkout and so runs on
   every PR: every base step that sets env keys is mapped, and every base
   step the correspondence names still exists.

3. The regression proof (AT-2104's method, step 3): the check, with the live
   config, run against the real pre-incident tree and the real post-fix tree.

   * Base side: the four ``base-ai-review-*.yml`` at base ``1c57d22`` (the
     v1.7.2 merge; the ROUND_CUTOFF_* wiring has been there since
     ``606c2f4``, v1.1.2), read out of this repository's own history with
     ``git show`` at test time. The commit is fetched by SHA if the checkout
     is shallow, and its absence is a failure, never a skip.
   * Wrapper side, post-fix: ``fixtures/at2120/wrapper-post/wrapper.yml``,
     ``wrapper.yml`` at wrapper ``82d261d`` (``fix(AT-2120): wire
     round-cutoff vars into wrapper inline comment steps``), vendored
     verbatim.
   * Wrapper side, pre-fix: reconstructed by reverse-applying
     ``fixtures/at2120/at-2120-fix.patch`` -- the fix commit itself,
     ``git format-patch -1 82d261d`` -- to the post-fix file, which yields
     ``wrapper.yml`` at ``7de4d6e``, the tree that shipped six MINOR
     releases without ROUND_CUTOFF_*. The reconstruction is checked to
     differ from post-fix in exactly the ROUND_CUTOFF_* lines, so a
     corrupted or wrong patch cannot pass as the incident.

   Why a patch and not a second vendored snapshot: vendoring both wrapper
   trees and base's four files (3864 lines) put the AT-2122 PR over base's
   own PR_SIZE_LIMIT (AT-1975), so no reviewer ran on it. Do not "simplify"
   this back to a vendored pre-fix copy; the patch is also the more faithful
   artefact, being the fix itself. Provenance of both vendored files (commit
   SHA, verification command) is recorded in ``fixtures/at2120/SOURCE``.

   The check must fail on the pre pair naming exactly the AT-2120 keys, on
   both axes, and pass on the post pair. The secondary case the ticket names,
   AT-2117 (``HEAD_SHA`` not passed to aggregation), is present in both
   vendored trees: they predate the wrapper port (wrapper PR #36), and the
   live exceptions.yml stopped excusing it when that port landed. The
   AT-2120 assertions therefore re-declare the HEAD_SHA exception locally
   (``vendored_exceptions``), and the AT-2117 assertion proves the check
   reports it against both trees with the live file as it is.

   Both repositories are public, so ``git show <sha>:.github/workflows/<file>``
   in either confirms every artefact. Because the regression uses the *live*
   config, a base step rename shows up here as StepNotFoundError: move the
   base pin to the commit that renamed it and re-check the four expected
   findings. A base step *added* after the pin cannot be handled that way
   before it merges, so its entry is dropped from the regression config by
   ``POST_PIN_STEPS`` (the HEAD_SHA re-excuse, one level up) and a test
   proves each listed step really is absent at the pin; prune the set when
   the pin moves past the step.
"""

from __future__ import annotations

import ast
import contextlib
import io
import os
import shlex
import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest
from check_base_wrapper_drift import (
    GIT_COMPLAINT_ELLIPSIS,
    GIT_COMPLAINT_QUOTE_LIMIT,
    DriftConfig,
    Exceptions,
    MalformedConfigError,
    OutputCorrespondence,
    StepCorrespondence,
    StepNotFoundError,
    check_env_keys,
    check_vars_consumed,
    check_workflow_outputs,
    _printable,
    describe_input_revision,
    extract_vars_consumed,
    find_step_env_keys,
    load_correspondence,
    load_exceptions,
    run,
    steps_with_env,
    workflow_call_outputs,
)

REPO_ROOT = Path(__file__).resolve().parents[3]
LIVE_WORKFLOWS = REPO_ROOT / ".github" / "workflows"
LIVE_CORRESPONDENCE = REPO_ROOT / ".github" / "drift-check" / "correspondence.yml"
LIVE_EXCEPTIONS = REPO_ROOT / ".github" / "drift-check" / "exceptions.yml"
FIXTURES = Path(__file__).resolve().parent / "fixtures" / "at2120"
WRAPPER_POST = FIXTURES / "wrapper-post"
AT_2120_FIX_PATCH = FIXTURES / "at-2120-fix.patch"
# Base workflow files as of the v1.7.2 merge; see the module docstring.
BASE_PIN = "1c57d2282f1880d471a028adb12e8651b8c33d68"
BASE_FILES = (
    "base-ai-review-prepare.yml",
    "base-ai-review-orchestrator.yml",
    "base-ai-review-single.yml",
    "base-ai-review-aggregate.yml",
)

AT_2120_KEYS = ("ROUND_CUTOFF_ENABLED", "ROUND_CUTOFF_N")


def _write(path: Path, text: str) -> None:
    path.write_text(textwrap.dedent(text), encoding="utf-8")


def _parse(text: str) -> dict:
    import yaml

    return yaml.safe_load(textwrap.dedent(text)) or {}


def _load_yaml(path: Path) -> dict:
    return _parse(path.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# find_step_env_keys / steps_with_env / extract_vars_consumed
# ---------------------------------------------------------------------------


def test_find_step_env_keys_reads_the_named_step() -> None:
    doc = {
        "jobs": {
            "review": {
                "steps": [
                    {"name": "Checkout", "uses": "actions/checkout@x"},
                    {"name": "Post inline comments", "env": {"GH_TOKEN": "x", "PR_NUMBER": "x"}},
                ]
            }
        }
    }
    assert find_step_env_keys(doc, "Post inline comments") == {"GH_TOKEN", "PR_NUMBER"}


def test_find_step_env_keys_missing_step_raises() -> None:
    doc = {"jobs": {"review": {"steps": [{"name": "Checkout"}]}}}
    with pytest.raises(StepNotFoundError):
        find_step_env_keys(doc, "Post inline comments")


def test_find_step_env_keys_step_with_no_env_block_is_empty_set() -> None:
    doc = {"jobs": {"review": {"steps": [{"name": "Checkout", "uses": "actions/checkout@x"}]}}}
    assert find_step_env_keys(doc, "Checkout") == set()


def test_steps_with_env_lists_only_env_bearing_steps_and_flags_unnamed() -> None:
    doc = {
        "jobs": {
            "a": {"steps": [{"name": "Checkout"}, {"name": "Run", "env": {"X": "1"}}]},
            "b": {"steps": [{"env": {"Y": "1"}}, {"name": "Empty env", "env": {}}]},
        }
    }
    assert steps_with_env(doc) == ["Run", None]


def test_extract_vars_consumed_collects_distinct_names() -> None:
    text = "if: ${{ vars.REVIEW_MODE == 'sequential' }}\nenv:\n  X: ${{ vars.JACCARD_THRESHOLD || '0.6' }}\n"
    assert extract_vars_consumed(_parse(text)) == {"REVIEW_MODE", "JACCARD_THRESHOLD"}


def test_extract_vars_consumed_walks_nested_lists_and_mappings() -> None:
    doc = {"jobs": {"a": {"steps": [{"run": "echo ${{ vars.ONE }}", "with": {"x": "${{ vars.TWO }}"}}]}}}
    assert extract_vars_consumed(doc) == {"ONE", "TWO"}


def test_extract_vars_consumed_ignores_a_variable_named_only_in_a_comment() -> None:
    """A prose mention is documentation, not consumption; counting it would
    turn the check red for a variable no job reads."""
    text = "# vars.COMMENT_ONLY controls nothing here\nenv:\n  X: ${{ vars.REAL }}  # not vars.TRAILING either\n"
    assert extract_vars_consumed(_parse(text)) == {"REAL"}


# ---------------------------------------------------------------------------
# check_env_keys: the AT-2120 shape, reproduced synthetically
# ---------------------------------------------------------------------------

_BASE_SINGLE_YML = """\
    jobs:
      review:
        steps:
          - name: Post inline comments
            env:
              GH_TOKEN: x
              PR_NUMBER: x
              REVIEWER: x
              JACCARD_THRESHOLD: x
              ROUND_CUTOFF_N: x
              ROUND_CUTOFF_ENABLED: x
    """


def _wrapper_yml(*, with_round_cutoff: bool) -> str:
    keys = ["GH_TOKEN", "PR_NUMBER", "REVIEWER", "JACCARD_THRESHOLD"]
    if with_round_cutoff:
        keys += ["ROUND_CUTOFF_N", "ROUND_CUTOFF_ENABLED"]
    lines = ["jobs:", "  review:", "    steps:"]
    for name in ("Post Claude inline comments", "Post Codex inline comments", "Post Gemini inline comments"):
        lines.append(f"      - name: {name}")
        lines.append("        env:")
        lines.extend(f"          {key}: x" for key in keys)
    return "\n".join(lines) + "\n"


_POST_INLINE = StepCorrespondence(
    base_file="base-ai-review-single.yml",
    base_step="Post inline comments",
    wrapper_file="wrapper.yml",
    wrapper_steps=(
        "Post Claude inline comments",
        "Post Codex inline comments",
        "Post Gemini inline comments",
    ),
)


def _config(*steps: StepCorrespondence, base_files=("base-ai-review-single.yml",)) -> DriftConfig:
    return DriftConfig(base_files=tuple(base_files), wrapper_files=("wrapper.yml",), steps=tuple(steps))


@pytest.fixture
def repo_dirs(tmp_path: Path) -> tuple[Path, Path]:
    base_dir = tmp_path / "base"
    wrapper_dir = tmp_path / "wrapper"
    base_dir.mkdir()
    wrapper_dir.mkdir()
    _write(base_dir / "base-ai-review-single.yml", _BASE_SINGLE_YML)
    return base_dir, wrapper_dir


def test_env_check_passes_when_wrapper_union_covers_base(repo_dirs) -> None:
    base_dir, wrapper_dir = repo_dirs
    (wrapper_dir / "wrapper.yml").write_text(_wrapper_yml(with_round_cutoff=True), encoding="utf-8")
    findings, notes = check_env_keys(base_dir, wrapper_dir, _config(_POST_INLINE), Exceptions.empty())
    assert findings == []
    assert notes == []


def test_env_check_catches_the_at_2120_shape(repo_dirs) -> None:
    """ROUND_CUTOFF_* wired in base, absent from all three wrapper steps."""
    base_dir, wrapper_dir = repo_dirs
    (wrapper_dir / "wrapper.yml").write_text(_wrapper_yml(with_round_cutoff=False), encoding="utf-8")
    findings, notes = check_env_keys(base_dir, wrapper_dir, _config(_POST_INLINE), Exceptions.empty())
    assert [key for key in AT_2120_KEYS if any(key in f for f in findings)] == list(AT_2120_KEYS)
    assert notes == []


def test_env_check_key_present_in_only_one_wrapper_step_still_passes(repo_dirs) -> None:
    """The invariant is a union-subset check: some wrapper step in the
    correspondence must carry each base key, not every wrapper step."""
    base_dir, wrapper_dir = repo_dirs
    _write(
        wrapper_dir / "wrapper.yml",
        """\
        jobs:
          review:
            steps:
              - name: Post Claude inline comments
                env:
                  GH_TOKEN: x
                  PR_NUMBER: x
                  REVIEWER: x
                  JACCARD_THRESHOLD: x
                  ROUND_CUTOFF_N: x
                  ROUND_CUTOFF_ENABLED: x
              - name: Post Codex inline comments
                env:
                  GH_TOKEN: x
              - name: Post Gemini inline comments
                env:
                  GH_TOKEN: x
        """,
    )
    findings, _ = check_env_keys(base_dir, wrapper_dir, _config(_POST_INLINE), Exceptions.empty())
    assert findings == []


def test_env_check_wrapper_only_key_is_not_a_finding(repo_dirs) -> None:
    base_dir, wrapper_dir = repo_dirs
    _write(
        wrapper_dir / "wrapper.yml",
        _wrapper_yml(with_round_cutoff=True) + "          WRAPPER_ONLY_SCAFFOLDING: x\n",
    )
    findings, _ = check_env_keys(base_dir, wrapper_dir, _config(_POST_INLINE), Exceptions.empty())
    assert findings == []


def test_env_check_excepted_key_is_noted_not_failed(repo_dirs) -> None:
    base_dir, wrapper_dir = repo_dirs
    (wrapper_dir / "wrapper.yml").write_text(_wrapper_yml(with_round_cutoff=False), encoding="utf-8")
    exceptions = Exceptions(
        vars={},
        env={
            _POST_INLINE.key: {
                "ROUND_CUTOFF_N": "deliberately excluded in this test",
                "ROUND_CUTOFF_ENABLED": "deliberately excluded in this test",
            }
        },
    )
    findings, notes = check_env_keys(base_dir, wrapper_dir, _config(_POST_INLINE), exceptions)
    assert findings == []
    assert len(notes) == 2


def test_env_check_stale_base_step_name_raises(repo_dirs) -> None:
    base_dir, wrapper_dir = repo_dirs
    (wrapper_dir / "wrapper.yml").write_text(_wrapper_yml(with_round_cutoff=True), encoding="utf-8")
    bad = StepCorrespondence(
        base_file="base-ai-review-single.yml",
        base_step="Post inline comments (renamed)",
        wrapper_file="wrapper.yml",
        wrapper_steps=("Post Claude inline comments",),
    )
    with pytest.raises(StepNotFoundError):
        check_env_keys(base_dir, wrapper_dir, _config(bad), Exceptions.empty())


def test_env_check_stale_wrapper_step_name_raises(repo_dirs) -> None:
    base_dir, wrapper_dir = repo_dirs
    (wrapper_dir / "wrapper.yml").write_text(_wrapper_yml(with_round_cutoff=True), encoding="utf-8")
    bad = StepCorrespondence(
        base_file="base-ai-review-single.yml",
        base_step="Post inline comments",
        wrapper_file="wrapper.yml",
        wrapper_steps=("Post Claude inline comments (renamed)",),
    )
    with pytest.raises(StepNotFoundError):
        check_env_keys(base_dir, wrapper_dir, _config(bad), Exceptions.empty())


def test_env_check_unmapped_base_step_is_a_finding(repo_dirs) -> None:
    """A base step that sets env keys and appears in no correspondence entry
    is the AT-2120 shape one level up: a whole step, not a key, that nothing
    would ever compare."""
    base_dir, wrapper_dir = repo_dirs
    _write(
        base_dir / "base-ai-review-single.yml",
        _BASE_SINGLE_YML.rstrip(" ") + "          - name: Brand new base step\n            env:\n              NEW_KEY: x\n",
    )
    (wrapper_dir / "wrapper.yml").write_text(_wrapper_yml(with_round_cutoff=True), encoding="utf-8")
    findings, _ = check_env_keys(base_dir, wrapper_dir, _config(_POST_INLINE), Exceptions.empty())
    assert findings == [
        "unmapped base step: base-ai-review-single.yml:'Brand new base step' sets env keys "
        "but has no correspondence entry (map it to wrapper step(s), or declare it as "
        "having no counterpart with a reason)"
    ]


def test_env_check_structural_exception_is_noted_not_failed(repo_dirs) -> None:
    base_dir, wrapper_dir = repo_dirs
    _write(
        base_dir / "base-ai-review-single.yml",
        _BASE_SINGLE_YML.rstrip(" ") + "          - name: Confirm the tree matches the diff\n            env:\n              HEAD_SHA: x\n",
    )
    (wrapper_dir / "wrapper.yml").write_text(_wrapper_yml(with_round_cutoff=True), encoding="utf-8")
    structural = StepCorrespondence(
        base_file="base-ai-review-single.yml",
        base_step="Confirm the tree matches the diff",
        wrapper_file="wrapper.yml",
        wrapper_steps=(),
        reason="single job, tree checked out once",
    )
    findings, notes = check_env_keys(
        base_dir, wrapper_dir, _config(_POST_INLINE, structural), Exceptions.empty()
    )
    assert findings == []
    assert notes == [
        "  step base-ai-review-single.yml:'Confirm the tree matches the diff' has no wrapper "
        "counterpart: single job, tree checked out once"
    ]


def test_env_check_structural_exception_still_requires_the_base_step_to_exist(repo_dirs) -> None:
    """A reason for a step that no longer exists is a stale config, not a pass."""
    base_dir, wrapper_dir = repo_dirs
    (wrapper_dir / "wrapper.yml").write_text(_wrapper_yml(with_round_cutoff=True), encoding="utf-8")
    gone = StepCorrespondence(
        base_file="base-ai-review-single.yml",
        base_step="Removed long ago",
        wrapper_file="wrapper.yml",
        wrapper_steps=(),
        reason="was structural once",
    )
    with pytest.raises(StepNotFoundError):
        check_env_keys(base_dir, wrapper_dir, _config(_POST_INLINE, gone), Exceptions.empty())


# ---------------------------------------------------------------------------
# check_vars_consumed: the REVIEW_MODE shape (a live, documented exception)
# ---------------------------------------------------------------------------


def _vars_dirs(tmp_path: Path, base_text: str, wrapper_text: str) -> tuple[Path, Path]:
    base_dir, wrapper_dir = tmp_path / "base", tmp_path / "wrapper"
    base_dir.mkdir()
    wrapper_dir.mkdir()
    _write(base_dir / "orchestrator.yml", base_text)
    _write(wrapper_dir / "wrapper.yml", wrapper_text)
    return base_dir, wrapper_dir


_VARS_CONFIG = DriftConfig(base_files=("orchestrator.yml",), wrapper_files=("wrapper.yml",), steps=())


def test_vars_check_catches_a_base_only_variable(tmp_path: Path) -> None:
    base_dir, wrapper_dir = _vars_dirs(
        tmp_path,
        "if: vars.REVIEW_MODE == 'sequential' && vars.JACCARD_THRESHOLD\n",
        "env:\n  X: ${{ vars.JACCARD_THRESHOLD }}\n",
    )
    findings, notes, info = check_vars_consumed(base_dir, wrapper_dir, _VARS_CONFIG, Exceptions.empty())
    assert any("REVIEW_MODE" in f for f in findings)
    assert not any("JACCARD_THRESHOLD" in f for f in findings)
    assert notes == []
    assert info == []


def test_vars_check_excepted_variable_is_noted(tmp_path: Path) -> None:
    base_dir, wrapper_dir = _vars_dirs(tmp_path, "if: vars.REVIEW_MODE == 'sequential'\n", "env:\n  X: y\n")
    exceptions = Exceptions(vars={"REVIEW_MODE": "single job, nothing to fan out; see AT-2105"}, env={})
    findings, notes, info = check_vars_consumed(base_dir, wrapper_dir, _VARS_CONFIG, exceptions)
    assert findings == []
    assert notes == ["  vars.REVIEW_MODE: single job, nothing to fan out; see AT-2105"]


def test_vars_check_wrapper_only_variable_is_informational(tmp_path: Path) -> None:
    base_dir, wrapper_dir = _vars_dirs(tmp_path, "env:\n  X: y\n", "env:\n  Y: ${{ vars.WRAPPER_ONLY_THING }}\n")
    findings, notes, info = check_vars_consumed(base_dir, wrapper_dir, _VARS_CONFIG, Exceptions.empty())
    assert findings == []
    assert notes == []
    assert info == ["  vars.WRAPPER_ONLY_THING (wrapper-only, not a drift)"]


# ---------------------------------------------------------------------------
# check_workflow_outputs: the AT-2511 shape -- a workflow_call output base
# declares and the wrapper never does, which neither axis above can see
# ---------------------------------------------------------------------------

_ROSTER_KEYS = ("reviewer_roster", "reviewers_expected_count", "reviewers_responded_count")


def _outputs_yml(*names: str) -> str:
    """A reusable workflow declaring ``names`` under on.workflow_call.outputs."""
    lines = ["on:", "  workflow_call:"]
    if names:
        lines.append("    outputs:")
        for name in names:
            lines.append(f"      {name}:")
            lines.append(f"        value: ${{{{ jobs.j.outputs.{name} }}}}")
    return "\n".join(lines) + "\njobs:\n  j:\n    steps: []\n"


def _outputs_dirs(tmp_path: Path, base_text: str, wrapper_text: str) -> tuple[Path, Path]:
    base_dir, wrapper_dir = tmp_path / "base", tmp_path / "wrapper"
    base_dir.mkdir()
    wrapper_dir.mkdir()
    (base_dir / "base-ai-review-orchestrator.yml").write_text(base_text, encoding="utf-8")
    (wrapper_dir / "wrapper.yml").write_text(wrapper_text, encoding="utf-8")
    return base_dir, wrapper_dir


def _outputs_config(*entries: OutputCorrespondence) -> DriftConfig:
    return DriftConfig(
        base_files=("base-ai-review-orchestrator.yml",),
        wrapper_files=("wrapper.yml",),
        steps=(),
        outputs=entries,
    )


_MAPPED_ORCHESTRATOR = OutputCorrespondence(
    base_file="base-ai-review-orchestrator.yml", wrapper_files=("wrapper.yml",)
)


def test_workflow_call_outputs_reads_the_unquoted_on_key() -> None:
    """`on:` is a YAML 1.1 boolean, so the parsed key is True, not "on".
    Reading only the string spelling would report every workflow as
    declaring nothing -- indistinguishable from a pass on this axis."""
    doc = _parse(_outputs_yml(*_ROSTER_KEYS))
    assert True in doc and "on" not in doc
    assert workflow_call_outputs(doc) == set(_ROSTER_KEYS)


def test_workflow_call_outputs_reads_the_quoted_on_key() -> None:
    assert workflow_call_outputs(_parse('"on":\n  workflow_call:\n    outputs:\n      a: {value: x}\n')) == {"a"}


@pytest.mark.parametrize(
    "text",
    [
        "on:\n  pull_request:\n    branches: [main]\n",  # not callable at all
        "on:\n  workflow_call:\n    inputs:\n      x: {type: string}\n",  # callable, no outputs
        "jobs:\n  j:\n    steps: []\n",  # no trigger block
    ],
)
def test_workflow_call_outputs_is_empty_when_there_is_no_contract(text: str) -> None:
    assert workflow_call_outputs(_parse(text)) == set()


def test_outputs_check_catches_the_at_2511_shape(tmp_path: Path) -> None:
    """The finding the reviewer asked for: base declares the roster, the
    wrapper declares nothing, and axes 1 and 2 stay silent because a
    workflow output is neither an env key nor a vars.* token."""
    base_dir, wrapper_dir = _outputs_dirs(tmp_path, _outputs_yml(*_ROSTER_KEYS), _outputs_yml())
    findings, notes = check_workflow_outputs(
        base_dir, wrapper_dir, _outputs_config(_MAPPED_ORCHESTRATOR), Exceptions.empty()
    )
    assert findings == [
        f"outputs drift: base base-ai-review-orchestrator.yml declares workflow_call output "
        f"{key!r}, which none of wrapper's ['wrapper.yml'] declare "
        "(a caller's gate reads nothing on the wrapper path)"
        for key in sorted(_ROSTER_KEYS)
    ]
    assert notes == []


def test_outputs_check_passes_once_the_wrapper_declares_them(tmp_path: Path) -> None:
    base_dir, wrapper_dir = _outputs_dirs(
        tmp_path, _outputs_yml(*_ROSTER_KEYS), _outputs_yml(*_ROSTER_KEYS)
    )
    findings, notes = check_workflow_outputs(
        base_dir, wrapper_dir, _outputs_config(_MAPPED_ORCHESTRATOR), Exceptions.empty()
    )
    assert findings == []
    assert notes == []


def test_outputs_check_partial_port_still_names_the_missing_one(tmp_path: Path) -> None:
    base_dir, wrapper_dir = _outputs_dirs(
        tmp_path, _outputs_yml(*_ROSTER_KEYS), _outputs_yml("reviewer_roster")
    )
    findings, _ = check_workflow_outputs(
        base_dir, wrapper_dir, _outputs_config(_MAPPED_ORCHESTRATOR), Exceptions.empty()
    )
    assert len(findings) == 2
    assert not any("'reviewer_roster'" in f for f in findings)


def test_outputs_check_wrapper_only_output_is_not_a_finding(tmp_path: Path) -> None:
    """Same direction as the other two axes: a wrapper-only output is
    scaffolding its shape needs, not a swallowed base feature."""
    base_dir, wrapper_dir = _outputs_dirs(
        tmp_path, _outputs_yml("reviewer_roster"), _outputs_yml("reviewer_roster", "wrapper_only")
    )
    findings, notes = check_workflow_outputs(
        base_dir, wrapper_dir, _outputs_config(_MAPPED_ORCHESTRATOR), Exceptions.empty()
    )
    assert findings == []
    assert notes == []


def test_outputs_check_excepted_key_is_noted_not_failed(tmp_path: Path) -> None:
    base_dir, wrapper_dir = _outputs_dirs(tmp_path, _outputs_yml("reviewer_roster"), _outputs_yml())
    exceptions = Exceptions(
        vars={},
        env={},
        outputs={"base-ai-review-orchestrator.yml": {"reviewer_roster": "tracked in AT-9999"}},
    )
    findings, notes = check_workflow_outputs(
        base_dir, wrapper_dir, _outputs_config(_MAPPED_ORCHESTRATOR), exceptions
    )
    assert findings == []
    assert notes == ["  outputs base-ai-review-orchestrator.yml key reviewer_roster: tracked in AT-9999"]


def test_outputs_check_structural_exception_is_noted_not_failed(tmp_path: Path) -> None:
    """prepare.yml's shape: outputs that exist only for the next job, which
    the single-job wrapper has no surface to declare."""
    base_dir, wrapper_dir = _outputs_dirs(tmp_path, _outputs_yml("head_sha", "skip"), _outputs_yml())
    entry = OutputCorrespondence(
        base_file="base-ai-review-orchestrator.yml", wrapper_files=(), reason="inter-job plumbing"
    )
    findings, notes = check_workflow_outputs(
        base_dir, wrapper_dir, _outputs_config(entry), Exceptions.empty()
    )
    assert findings == []
    assert notes == [
        "  outputs base-ai-review-orchestrator.yml has no wrapper counterpart: inter-job plumbing"
    ]


def test_outputs_check_unmapped_base_file_is_a_finding(tmp_path: Path) -> None:
    """Completeness, the same rule axis 1 applies to a new env-bearing step:
    a base workflow that becomes callable with outputs must not be invisible
    here the way a new output name used to be."""
    base_dir, wrapper_dir = _outputs_dirs(tmp_path, _outputs_yml("reviewer_roster"), _outputs_yml())
    findings, notes = check_workflow_outputs(
        base_dir, wrapper_dir, _outputs_config(), Exceptions.empty()
    )
    assert findings == [
        "unmapped base outputs: base-ai-review-orchestrator.yml declares "
        "on.workflow_call.outputs but has no entry under the correspondence's "
        "'outputs' (map it to wrapper file(s), or declare it as having no "
        "counterpart with a reason)"
    ]
    assert notes == []


def test_outputs_check_base_file_declaring_nothing_needs_no_entry(tmp_path: Path) -> None:
    base_dir, wrapper_dir = _outputs_dirs(tmp_path, _outputs_yml(), _outputs_yml())
    findings, notes = check_workflow_outputs(
        base_dir, wrapper_dir, _outputs_config(), Exceptions.empty()
    )
    assert findings == []
    assert notes == []


def test_outputs_check_stale_base_file_name_raises(tmp_path: Path) -> None:
    """A config naming a file that no longer exists must fail like a stale
    step name, not compare against nothing."""
    base_dir, wrapper_dir = _outputs_dirs(tmp_path, _outputs_yml(), _outputs_yml())
    entry = OutputCorrespondence(base_file="gone.yml", wrapper_files=("wrapper.yml",))
    with pytest.raises(FileNotFoundError):
        check_workflow_outputs(base_dir, wrapper_dir, _outputs_config(entry), Exceptions.empty())


# ---------------------------------------------------------------------------
# load_correspondence / load_exceptions: every escape hatch carries a reason
# ---------------------------------------------------------------------------


def test_load_correspondence_reads_mapped_and_structural_entries(tmp_path: Path) -> None:
    path = tmp_path / "correspondence.yml"
    _write(
        path,
        """\
        base_files: [a.yml, b.yml]
        wrapper_files: [wrapper.yml]
        steps:
          - base_file: a.yml
            base_step: One
            wrapper_file: wrapper.yml
            wrapper_steps: [One, One again]
          - base_file: b.yml
            base_step: Two
            wrapper_file: wrapper.yml
            wrapper_steps: []
            reason: no counterpart on purpose
        """,
    )
    config = load_correspondence(path)
    assert config.base_files == ("a.yml", "b.yml")
    assert config.wrapper_files == ("wrapper.yml",)
    assert config.steps[0].wrapper_steps == ("One", "One again")
    assert config.steps[0].reason == ""
    assert config.steps[1].has_no_counterpart
    assert config.steps[1].reason == "no counterpart on purpose"


@pytest.mark.parametrize("field", ["base_file", "base_step", "wrapper_file"])
def test_load_correspondence_rejects_an_entry_missing_a_required_field(tmp_path: Path, field: str) -> None:
    """A missing required key is a malformed config, reported through the
    same MalformedConfigError path as a missing reason -- not a KeyError."""
    entry = {"base_file": "a.yml", "base_step": "One", "wrapper_file": "wrapper.yml", "wrapper_steps": ["One"]}
    del entry[field]
    path = tmp_path / "correspondence.yml"
    lines = ["steps:", "  - " + "\n    ".join(f"{k}: {v}" for k, v in entry.items())]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    with pytest.raises(MalformedConfigError, match=f"entry 0.*missing '{field}'"):
        load_correspondence(path)


def test_load_correspondence_rejects_no_counterpart_without_reason(tmp_path: Path) -> None:
    path = tmp_path / "correspondence.yml"
    _write(
        path,
        """\
        steps:
          - base_file: b.yml
            base_step: Two
            wrapper_file: wrapper.yml
            wrapper_steps: []
        """,
    )
    with pytest.raises(MalformedConfigError):
        load_correspondence(path)


def test_load_correspondence_rejects_blank_reason(tmp_path: Path) -> None:
    path = tmp_path / "correspondence.yml"
    _write(
        path,
        """\
        steps:
          - base_file: b.yml
            base_step: Two
            wrapper_file: wrapper.yml
            wrapper_steps: []
            reason: '   '
        """,
    )
    with pytest.raises(MalformedConfigError):
        load_correspondence(path)


def test_load_correspondence_rejects_reason_on_a_mapped_step(tmp_path: Path) -> None:
    """A reason means 'nothing to compare against'; on a mapped step it would
    read as excusing keys the check still compares."""
    path = tmp_path / "correspondence.yml"
    _write(
        path,
        """\
        steps:
          - base_file: a.yml
            base_step: One
            wrapper_file: wrapper.yml
            wrapper_steps: [One]
            reason: but also this
        """,
    )
    with pytest.raises(MalformedConfigError):
        load_correspondence(path)


def test_load_exceptions_requires_a_reason(tmp_path: Path) -> None:
    path = tmp_path / "exceptions.yml"
    _write(path, "vars:\n  REVIEW_MODE: {}\n")
    with pytest.raises(MalformedConfigError):
        load_exceptions(path)


def test_load_exceptions_blank_reason_also_rejected(tmp_path: Path) -> None:
    path = tmp_path / "exceptions.yml"
    _write(path, "vars:\n  REVIEW_MODE:\n    reason: '   '\n")
    with pytest.raises(MalformedConfigError):
        load_exceptions(path)


def test_load_exceptions_env_entry_requires_a_reason(tmp_path: Path) -> None:
    path = tmp_path / "exceptions.yml"
    _write(path, "env:\n  a.yml:\n    One:\n      KEY: {}\n")
    with pytest.raises(MalformedConfigError):
        load_exceptions(path)


def test_load_correspondence_reads_mapped_and_structural_output_entries(tmp_path: Path) -> None:
    path = tmp_path / "correspondence.yml"
    _write(
        path,
        """\
        base_files: [a.yml, b.yml]
        wrapper_files: [wrapper.yml]
        steps: []
        outputs:
          - base_file: a.yml
            wrapper_files: [wrapper.yml]
          - base_file: b.yml
            wrapper_files: []
            reason: inter-job plumbing only
        """,
    )
    config = load_correspondence(path)
    assert config.outputs == (
        OutputCorrespondence(base_file="a.yml", wrapper_files=("wrapper.yml",)),
        OutputCorrespondence(base_file="b.yml", wrapper_files=(), reason="inter-job plumbing only"),
    )


def test_load_correspondence_outputs_default_to_empty_when_absent(tmp_path: Path) -> None:
    path = tmp_path / "correspondence.yml"
    _write(path, "base_files: [a.yml]\nwrapper_files: [wrapper.yml]\nsteps: []\n")
    assert load_correspondence(path).outputs == ()


def test_load_correspondence_rejects_an_outputs_entry_missing_base_file(tmp_path: Path) -> None:
    path = tmp_path / "correspondence.yml"
    _write(path, "outputs:\n  - wrapper_files: [wrapper.yml]\n")
    with pytest.raises(MalformedConfigError, match="missing 'base_file'"):
        load_correspondence(path)


def test_load_correspondence_rejects_an_unmapped_outputs_entry_without_reason(tmp_path: Path) -> None:
    path = tmp_path / "correspondence.yml"
    _write(path, "outputs:\n  - base_file: a.yml\n    wrapper_files: []\n")
    with pytest.raises(MalformedConfigError, match="no wrapper_files and no reason"):
        load_correspondence(path)


def test_load_correspondence_rejects_a_blank_outputs_reason(tmp_path: Path) -> None:
    path = tmp_path / "correspondence.yml"
    _write(path, "outputs:\n  - base_file: a.yml\n    wrapper_files: []\n    reason: '   '\n")
    with pytest.raises(MalformedConfigError, match="no wrapper_files and no reason"):
        load_correspondence(path)


def test_load_correspondence_rejects_a_reason_on_a_mapped_outputs_entry(tmp_path: Path) -> None:
    path = tmp_path / "correspondence.yml"
    _write(path, "outputs:\n  - base_file: a.yml\n    wrapper_files: [wrapper.yml]\n    reason: why\n")
    with pytest.raises(MalformedConfigError, match="maps wrapper files and also carries"):
        load_correspondence(path)


def test_load_exceptions_outputs_entry_requires_a_reason(tmp_path: Path) -> None:
    path = tmp_path / "exceptions.yml"
    _write(path, "outputs:\n  a.yml:\n    reviewer_roster: {}\n")
    with pytest.raises(MalformedConfigError, match="outputs a.yml reviewer_roster has no reason"):
        load_exceptions(path)


def test_load_exceptions_missing_file_is_empty_not_an_error(tmp_path: Path) -> None:
    assert load_exceptions(tmp_path / "does-not-exist.yml") == Exceptions.empty()


def test_load_exceptions_reads_well_formed_entries(tmp_path: Path) -> None:
    path = tmp_path / "exceptions.yml"
    _write(
        path,
        """\
        vars:
          REVIEW_MODE:
            reason: see AT-2105
        env:
          a.yml:
            One:
              KEY:
                reason: hardcoded on the wrapper side
        """,
    )
    assert load_exceptions(path) == Exceptions(
        vars={"REVIEW_MODE": "see AT-2105"},
        env={("a.yml", "One"): {"KEY": "hardcoded on the wrapper side"}},
    )


# ---------------------------------------------------------------------------
# run(): end-to-end exit codes on a synthetic pair
# ---------------------------------------------------------------------------


def _write_correspondence(path: Path) -> None:
    _write(
        path,
        """\
        base_files: [base-ai-review-single.yml]
        wrapper_files: [wrapper.yml]
        steps:
          - base_file: base-ai-review-single.yml
            base_step: Post inline comments
            wrapper_file: wrapper.yml
            wrapper_steps:
              - Post Claude inline comments
              - Post Codex inline comments
              - Post Gemini inline comments
        """,
    )


def _run_dirs(tmp_path: Path, *, with_round_cutoff: bool, exceptions_text: str = "vars: {}\nenv: {}\n") -> int:
    base_dir, wrapper_dir = tmp_path / "base", tmp_path / "wrapper"
    base_dir.mkdir()
    wrapper_dir.mkdir()
    _write(base_dir / "base-ai-review-single.yml", _BASE_SINGLE_YML)
    (wrapper_dir / "wrapper.yml").write_text(_wrapper_yml(with_round_cutoff=with_round_cutoff), encoding="utf-8")
    correspondence = tmp_path / "correspondence.yml"
    _write_correspondence(correspondence)
    exceptions = tmp_path / "exceptions.yml"
    exceptions.write_text(exceptions_text, encoding="utf-8")
    return run(base_dir, wrapper_dir, correspondence, exceptions)


def test_run_exits_zero_on_a_matched_pair(tmp_path: Path) -> None:
    assert _run_dirs(tmp_path, with_round_cutoff=True) == 0


def test_run_exits_one_on_the_at_2120_shape(tmp_path: Path) -> None:
    assert _run_dirs(tmp_path, with_round_cutoff=False) == 1


@pytest.mark.parametrize("missing", ["base", "wrapper"])
def test_run_exits_one_when_a_configured_file_is_missing(tmp_path: Path, missing: str, capsys) -> None:
    """A renamed or removed workflow file must fail the check like a renamed
    step does -- a file compared against nothing is neither a skip nor a pass."""
    base_dir, wrapper_dir = tmp_path / "base", tmp_path / "wrapper"
    base_dir.mkdir()
    wrapper_dir.mkdir()
    if missing != "base":
        _write(base_dir / "base-ai-review-single.yml", _BASE_SINGLE_YML)
    if missing != "wrapper":
        (wrapper_dir / "wrapper.yml").write_text(_wrapper_yml(with_round_cutoff=True), encoding="utf-8")
    correspondence = tmp_path / "correspondence.yml"
    _write_correspondence(correspondence)
    exceptions = tmp_path / "exceptions.yml"
    exceptions.write_text("vars: {}\nenv: {}\n", encoding="utf-8")
    assert run(base_dir, wrapper_dir, correspondence, exceptions) == 1
    out = capsys.readouterr().out
    assert "names a file that does not exist" in out
    assert "stale against the current workflow files" in out


def _run_with_wrapper_text(tmp_path: Path, wrapper_text: str) -> int:
    base_dir, wrapper_dir = tmp_path / "base", tmp_path / "wrapper"
    base_dir.mkdir()
    wrapper_dir.mkdir()
    _write(base_dir / "base-ai-review-single.yml", _BASE_SINGLE_YML)
    (wrapper_dir / "wrapper.yml").write_text(wrapper_text, encoding="utf-8")
    correspondence = tmp_path / "correspondence.yml"
    _write_correspondence(correspondence)
    exceptions = tmp_path / "exceptions.yml"
    exceptions.write_text("vars: {}\nenv: {}\n", encoding="utf-8")
    return run(base_dir, wrapper_dir, correspondence, exceptions)


def test_run_exits_one_on_an_unparsable_workflow_file(tmp_path: Path, capsys) -> None:
    """The wrapper file comes from another repository's main; a transient
    syntax error there must fail the check cleanly, not traceback."""
    assert _run_with_wrapper_text(tmp_path, "jobs:\n  review:\n    steps: [unclosed\n") == 1
    out = capsys.readouterr().out
    assert "cannot be read as a workflow" in out
    assert "not valid YAML" in out
    assert "stale against the current workflow files" in out


def test_run_exits_one_on_a_non_mapping_workflow_file(tmp_path: Path, capsys) -> None:
    """A file whose top level is a list parses fine and has no jobs, no
    steps and no vars -- it must not pass as 'nothing to compare'."""
    assert _run_with_wrapper_text(tmp_path, "- Post Claude inline comments\n- Post Codex inline comments\n") == 1
    out = capsys.readouterr().out
    assert "cannot be read as a workflow" in out
    assert "top level is list, not a mapping" in out


def test_run_exits_one_on_an_unparsable_config_file(tmp_path: Path, capsys) -> None:
    base_dir, wrapper_dir = tmp_path / "base", tmp_path / "wrapper"
    base_dir.mkdir()
    wrapper_dir.mkdir()
    _write(base_dir / "base-ai-review-single.yml", _BASE_SINGLE_YML)
    (wrapper_dir / "wrapper.yml").write_text(_wrapper_yml(with_round_cutoff=True), encoding="utf-8")
    correspondence = tmp_path / "correspondence.yml"
    correspondence.write_text("steps: [unclosed\n", encoding="utf-8")
    exceptions = tmp_path / "exceptions.yml"
    exceptions.write_text("vars: {}\nenv: {}\n", encoding="utf-8")
    assert run(base_dir, wrapper_dir, correspondence, exceptions) == 1
    assert "malformed drift-check config" in capsys.readouterr().out


def _run_outputs_dirs(tmp_path: Path, *, wrapper_declares: bool) -> tuple[int, str]:
    base_dir, wrapper_dir = _outputs_dirs(
        tmp_path,
        _outputs_yml(*_ROSTER_KEYS),
        _outputs_yml(*_ROSTER_KEYS) if wrapper_declares else _outputs_yml(),
    )
    correspondence = tmp_path / "correspondence.yml"
    _write(
        correspondence,
        """\
        base_files: [base-ai-review-orchestrator.yml]
        wrapper_files: [wrapper.yml]
        steps: []
        outputs:
          - base_file: base-ai-review-orchestrator.yml
            wrapper_files: [wrapper.yml]
        """,
    )
    exceptions = tmp_path / "exceptions.yml"
    exceptions.write_text("vars: {}\nenv: {}\noutputs: {}\n", encoding="utf-8")
    return run(base_dir, wrapper_dir, correspondence, exceptions), str(correspondence)


def test_run_exits_one_on_an_unported_workflow_output(tmp_path: Path, capsys) -> None:
    code, _ = _run_outputs_dirs(tmp_path, wrapper_declares=False)
    assert code == 1
    out = capsys.readouterr().out
    assert "DRIFT FOUND:" in out
    assert "outputs drift" in out


def test_run_exits_zero_once_the_output_is_ported(tmp_path: Path) -> None:
    code, _ = _run_outputs_dirs(tmp_path, wrapper_declares=True)
    assert code == 0


def test_run_exits_one_on_a_malformed_outputs_exception(tmp_path: Path) -> None:
    assert _run_dirs(tmp_path, with_round_cutoff=True, exceptions_text="outputs:\n  a.yml:\n    x: {}\n") == 1


def test_run_exits_one_on_a_malformed_exceptions_file(tmp_path: Path) -> None:
    assert _run_dirs(tmp_path, with_round_cutoff=True, exceptions_text="vars:\n  SOMETHING: {}\nenv: {}\n") == 1


# ---------------------------------------------------------------------------
# The input-revision stamp (AT-2557)
#
# Wrapper PR #44 attached this check's own output as evidence of "exit 0, no
# drift". The output was genuine -- the real tool, the real findings -- but it
# had been produced against base v1.10.0, three weeks before the two axes the
# PR existed to satisfy (vars.CLAUDE_ALLOWED_BOTS, AT-2272; workflow_call
# outputs, AT-2511) reached base at all. Re-run at v1.11.2 the same pair gives
# exit 1 with seven findings. Nothing in the attached file said which base it
# had read, so catching it cost a four-version bisect.
#
# Every test below is on that one property: the output names its own inputs,
# in every case, including the cases where the honest answer is awkward.
# ---------------------------------------------------------------------------

_GIT_ENV = {
    **os.environ,
    # The host's git config must not reach these fixtures: a global
    # commit.gpgsign or a template hook would make the commit fail, and
    # init.defaultBranch would change nothing the tests read but is pinned
    # anyway so the fixture is the same everywhere.
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_SYSTEM": os.devnull,
    "GIT_AUTHOR_NAME": "drift test",
    "GIT_AUTHOR_EMAIL": "drift-test@example.invalid",
    "GIT_COMMITTER_NAME": "drift test",
    "GIT_COMMITTER_EMAIL": "drift-test@example.invalid",
}


@pytest.fixture
def git_search_ceiling(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Insulate the git calls *inside the code under test* from the host.

    ``describe_input_revision`` runs ``git -C <dir>``, and git walks up out of
    ``<dir>`` looking for a checkout. Whether it finds one above ``tmp_path``
    is a property of the host, not of the code: some CI images and container
    setups put TMPDIR inside the workspace checkout, and the tests that assert
    `not a git checkout` then fail against the surrounding repository's HEAD --
    a red suite for a reason that has nothing to do with the stamp. Measured:
    with TMPDIR inside a checkout and no ceiling, the first line reads
    `@ <workspace sha> (no tag at this commit) + 1 uncommitted path(s)`.

    ``_GIT_ENV`` pins the config files but is a snapshot taken at import and
    is passed only to the fixture-building calls; the git calls inside the
    code under test inherit the ambient environment instead, which is what
    ``monkeypatch`` reaches. So the config files are pinned here too, or the
    insulation stops at the fixtures and leaves the assertions exposed to the
    host for a second reason.

    Historical, and no longer reproducible against this code: a host-global
    `core.excludesFile` naming `*.yml` once turned
    `test_stamp_reports_an_untracked_file_in_the_input` and
    `test_stamp_counts_every_file_in_an_untracked_directory` red, the
    fifty-file case stamping with no dirt at all. That was measured before
    the dirt probe took `--ignored`. It now reports ignored paths as `!!`
    entries, `-uall` expands them one per file, and the counts hold -- so
    the same config leaves both tests green. Re-measured with the pins
    removed: `core.excludesFile` naming `*.yml`, the same naming `*.yml` and
    `*.swp`, `status.showUntrackedFiles = no` (overridden by the explicit
    `-uall`) and `core.quotePath = true` all leave both green. The control
    for that negative is the same `*.yml` config against the probe with
    `--ignored` taken out, which still turns both red, and `git config
    --get core.excludesFile` returning the planted path -- so the sweep
    reached git and found nothing, rather than never reaching it.

    The pins stay because four variables leaving the assertions alone is not
    a proof that none can, and the insulation costs two lines; what is
    withdrawn is the claim that a specific one currently bites.
    """
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path))
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_SYSTEM", os.devnull)


def _fixture_git(path: Path, *args: str) -> str:
    result = subprocess.run(
        ("git", "-C", str(path), *args),
        capture_output=True,
        text=True,
        check=True,
        env=_GIT_ENV,
    )
    return result.stdout.strip()


def _break_git_on_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    fails: str = "",
    fails_message: str = "forced git failure",
    hangs: str = "",
    garbles: str = "",
) -> None:
    """Put a passthrough ``git`` on PATH that makes one subcommand really fail.

    The failure branches are about what ``_git`` returns when the subprocess
    itself goes wrong, so the subprocess is made to go wrong: the shim exits
    128 for ``fails``, sleeps past the timeout for ``hangs``, writes
    undecodable bytes on stderr for ``garbles``, and execs the real git for
    everything else. Patching ``_git``'s return instead would assert against
    the stub and leave the real failure path unproven -- and for ``garbles``
    it would miss the point entirely, since the decode happens inside
    ``subprocess.run`` and that is the thing under test.

    Install it *after* the fixture repository is built, or the fixture's own
    git calls hit the shim too.
    """
    real = shutil.which("git")
    assert real is not None, "these tests need a real git on PATH"
    # exist_ok, so a test can re-plant the shim with a different message;
    # the file is overwritten and the PATH entry is simply prepended again.
    bindir = tmp_path / "shim-bin"
    bindir.mkdir(exist_ok=True)
    # Every interpolation that becomes shell source is shlex.quote'd, and
    # a test enumerates them so the word "every" is checked rather than
    # asserted. Unquoted, a
    # message carrying `$`, a backtick, a backslash or a double quote -- the
    # shape of a realistic git complaint, `... at "$HOME/x"` -- is expanded
    # or terminates the word, and the shim then emits something other than
    # what the test planted while the assertion still passes against the
    # wrong string. Measured: `fatal: ... at "$HOME/x"` came out as
    # `fatal: ... at /home/SENTINEL/x`, quotes eaten and $HOME expanded. A
    # guard that can quietly check something other than what it names is
    # worse than no guard. `printf %s\n` rather than `echo` for the same
    # reason: dash's echo interprets backslash escapes.
    guards: list[str] = []
    if fails:
        guards.append(
            f"  if [ \"$a\" = {shlex.quote(fails)} ]; then"
            f" printf '%s\\n' {shlex.quote(fails_message)} >&2; exit 128; fi"
        )
    if hangs:
        # `exec`, so the sleep *replaces* the shell and the kill that ends the
        # timeout lands on the process actually holding the inherited stdout
        # and stderr pipes. As a child of /bin/sh it survives instead: measured
        # on this tree, 3 runs out of 3 left an orphaned `sleep` reparented to
        # init, against 0 out of 3 with `exec`. It costs no wall time here --
        # both forms measure 1.01s against a 1s timeout, because CPython's
        # POSIX branch calls process.wait() on the direct child rather than
        # communicate(); it is communicate()-after-kill, the Windows branch,
        # that would block on the orphan's pipes. Leaking a process per run is
        # reason enough, and `exec` also keeps that branch honest.
        guards.append(f"  if [ \"$a\" = {shlex.quote(hangs)} ]; then exec sleep 5; fi")
    if garbles:
        # A lone 0x80 is not valid UTF-8, so a strict decode of this stderr
        # raises UnicodeDecodeError inside subprocess.run -- which is where
        # the decode happens under `text=True`.
        guards.append(
            f"  if [ \"$a\" = {shlex.quote(garbles)} ];"
            f" then printf 'fatal: \\200\\200 bad\\n' >&2; exit 128; fi"
        )
    shim = bindir / "git"
    shim.write_text(
        "#!/bin/sh\nfor a in \"$@\"; do\n" + "\n".join([*guards, "  :"]) + f'\ndone\nexec {shlex.quote(real)} "$@"\n',
        encoding="utf-8",
    )
    shim.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")


def _committed_base(tmp_path: Path, *, tag: str = "") -> tuple[Path, str]:
    """The prologue every stamp test shares: a committed one-file input dir.

    Returns the directory and its HEAD, so a test that needs more -- a
    `.gitignore` before the commit, an extra file after it -- builds on the
    returned path rather than restating the four lines.
    """
    base = tmp_path / "base"
    base.mkdir()
    _write(base / "base-ai-review-single.yml", _BASE_SINGLE_YML)
    return base, _commit_as_repo(base, tag=tag)


def _commit_as_repo(path: Path, *, tag: str = "") -> str:
    """Turn ``path`` into a git checkout with its contents committed."""
    subprocess.run(
        ("git", "init", "-q", "--initial-branch=main", str(path)),
        check=True,
        capture_output=True,
        env=_GIT_ENV,
    )
    _fixture_git(path, "add", "-A")
    _fixture_git(path, "commit", "-q", "-m", "fixture")
    if tag:
        _fixture_git(path, "tag", tag)
    return _fixture_git(path, "rev-parse", "HEAD")


def _stamped_run(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    *,
    with_round_cutoff: bool = True,
    base_tag: str = "",
    base_is_repo: bool = True,
    dirty: bool = False,
) -> tuple[int, list[str]]:
    """Run the check on a synthetic pair and return (exit code, output lines)."""
    base_dir, wrapper_dir = tmp_path / "base", tmp_path / "wrapper"
    base_dir.mkdir()
    wrapper_dir.mkdir()
    _write(base_dir / "base-ai-review-single.yml", _BASE_SINGLE_YML)
    (wrapper_dir / "wrapper.yml").write_text(_wrapper_yml(with_round_cutoff=with_round_cutoff), encoding="utf-8")
    if base_is_repo:
        _commit_as_repo(base_dir, tag=base_tag)
    if dirty:
        (base_dir / "base-ai-review-single.yml").write_text(
            textwrap.dedent(_BASE_SINGLE_YML) + "# edited after the commit\n", encoding="utf-8"
        )
    correspondence = tmp_path / "correspondence.yml"
    _write_correspondence(correspondence)
    exceptions = tmp_path / "exceptions.yml"
    exceptions.write_text("vars: {}\nenv: {}\n", encoding="utf-8")
    code = run(base_dir, wrapper_dir, correspondence, exceptions)
    return code, capsys.readouterr().out.splitlines()


@pytest.mark.usefixtures("git_search_ceiling")
def test_run_stamps_the_base_revision_on_the_first_line(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """The negative test for AT-2557, and the whole ticket in one assertion.

    The positive control is the last line: `no undeclared base<->wrapper
    drift found` is what this pair printed before the stamp existed and must
    still print, so a green first line cannot be an artefact of the check
    having stopped producing output at all.
    """
    code, lines = _stamped_run(tmp_path, capsys, base_tag="v9.9.9")
    head = _fixture_git(tmp_path / "base", "rev-parse", "HEAD")

    assert lines[0].startswith("base input revision:")
    assert head in lines[0]
    assert "tag v9.9.9" in lines[0]

    assert code == 0
    assert lines[-1] == "no undeclared base<->wrapper drift found"


@pytest.mark.usefixtures("git_search_ceiling")
def test_run_stamps_the_wrapper_revision_too(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """The check reads two trees, and either going stale produces the same
    hollow pass; stamping only the one this incident happened to involve
    would leave the other half unreadable."""
    _, lines = _stamped_run(tmp_path, capsys)
    assert lines[1].startswith("wrapper input revision:")


@pytest.mark.usefixtures("git_search_ceiling")
def test_stamp_names_a_tag_that_points_at_the_commit(tmp_path: Path) -> None:
    base, head = _committed_base(tmp_path, tag="v1.11.2")

    stamp = describe_input_revision(base)
    assert head in stamp
    assert "tag v1.11.2" in stamp
    assert "uncommitted" not in stamp


@pytest.mark.usefixtures("git_search_ceiling")
def test_stamp_says_no_tag_rather_than_guessing_a_nearby_one(tmp_path: Path) -> None:
    """A detached commit with no tag on it prints the SHA and says so.

    Deliberately not `git describe`'s nearest-tag form: `v1.11.2-3-gabc1234`
    reads as v1.11.2 at a glance, and a near-miss revision passing for the
    right one is the defect this stamp exists to close.
    """
    base, head = _committed_base(tmp_path, tag="v1.11.2")
    (base / "base-ai-review-single.yml").write_text(textwrap.dedent(_BASE_SINGLE_YML) + "# later\n", encoding="utf-8")
    _fixture_git(base, "commit", "-q", "-am", "a commit after the tag")
    later = _fixture_git(base, "rev-parse", "HEAD")

    stamp = describe_input_revision(base)
    assert later in stamp
    assert head not in stamp
    assert "no tag at this commit" in stamp
    assert "v1.11.2" not in stamp


@pytest.mark.usefixtures("git_search_ceiling")
def test_stamp_reports_an_uncommitted_change_in_the_input(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """A dirty tree makes the SHA a half-truth: the file the check read is
    not the file that SHA names. Saying only the SHA would be the same
    hollow pass one layer down."""
    _, lines = _stamped_run(tmp_path, capsys, base_tag="v9.9.9", dirty=True)
    assert "uncommitted" in lines[0]
    # Still names the commit it departs from -- the stamp narrows the search,
    # it does not refuse to answer.
    assert _fixture_git(tmp_path / "base", "rev-parse", "HEAD") in lines[0]


@pytest.mark.usefixtures("git_search_ceiling")
def test_stamp_reports_an_untracked_file_in_the_input(tmp_path: Path) -> None:
    """Untracked counts as dirty: a workflow file added but not committed is
    read by the check and is in no commit, so the SHA does not describe the
    input. It covers only what `git status` reports, which is why the two
    tests below exist: an ignored path is invisible to it."""
    base, _ = _committed_base(tmp_path)
    _write(base / "base-ai-review-extra.yml", "jobs: {}\n")

    assert "uncommitted" in describe_input_revision(base)


@pytest.mark.usefixtures("git_search_ceiling")
def test_stamp_refuses_to_credit_a_checkout_for_an_ignored_artefact_directory(tmp_path: Path) -> None:
    """An artefact unpacked into an ignored location names no revision.

    `git status` says nothing about ignored paths, so a directory under
    `build/` reads as clean while `rev-parse` has already walked up and
    answered with the surrounding repository's HEAD -- which would stamp that
    commit's SHA and tag onto files it never contained. That is AT-2557's own
    defect one layer down, and it is the case the untracked-file test above
    does not reach.

    The positive control is the tracked directory in the same checkout: it
    must still get the SHA and the tag, so a green assertion here cannot come
    from the stamp having stopped naming revisions at all.
    """
    checkout = tmp_path / "checkout"
    tracked = checkout / ".github" / "workflows"
    tracked.mkdir(parents=True)
    _write(tracked / "base-ai-review-single.yml", _BASE_SINGLE_YML)
    (checkout / ".gitignore").write_text("build/\n", encoding="utf-8")
    head = _commit_as_repo(checkout, tag="v1.11.2")

    artefact = checkout / "build" / "unpacked-base"
    artefact.mkdir(parents=True)
    _write(artefact / "base-ai-review-single.yml", _BASE_SINGLE_YML)

    stamp = describe_input_revision(artefact)
    assert "no tracked content here" in stamp
    assert head not in stamp
    assert "v1.11.2" not in stamp

    control = describe_input_revision(tracked)
    assert head in control
    assert "tag v1.11.2" in control


@pytest.mark.usefixtures("git_search_ceiling")
def test_stamp_counts_an_ignored_artefact_beside_tracked_workflow_files(tmp_path: Path) -> None:
    """The case `--ignored` exists to close, and the one `ls-files` cannot.

    The tracked-content probe catches a directory with *nothing* tracked in
    it. A directory holding real tracked workflow files *and* an unpacked
    artefact under an ignored path sails through it, and plain `git status`
    is silent about the ignored half -- so the stamp printed a SHA and a tag
    for a tree it had only partly looked at. That is this ticket's defect in
    the configuration that is actually common: a checkout, with something
    extra dropped into it.

    The positive control is the clean-directory assertion first: the same
    fixture with nothing planted stamps no dirt at all, so the count below
    is the artefact and not a fixture that was dirty to begin with.

    Several files, not one. With a single planted file a per-file count and
    a per-directory `!! unpacked/` count are both 1, so the assertion could
    not tell them apart and passed either way -- measured: at one planted
    file `--ignored -uall` and `--ignored` alone both report 1, and they
    diverge only from two upwards. Per-file expansion *inside an ignored
    directory* rests on `--ignored` and `-uall` together, which the
    fifty-file test cannot show because its artefact is merely untracked.
    Were that interaction to change, a fifty-file artefact under an ignored
    path would report `1 uncommitted path(s)` -- the exact understatement
    the dirt probe's own comment says `-uall` prevents.
    """
    base = tmp_path / "base"
    base.mkdir()
    _write(base / "base-ai-review-single.yml", _BASE_SINGLE_YML)
    (base / ".gitignore").write_text("unpacked/\n", encoding="utf-8")
    head = _commit_as_repo(base, tag="v1.11.2")

    # Control: tracked content, nothing extra, no dirt.
    clean = describe_input_revision(base)
    assert "uncommitted" not in clean
    assert head in clean and "tag v1.11.2" in clean

    unpacked = base / "unpacked"
    unpacked.mkdir()
    planted = [unpacked / f"base-ai-review-{index:02d}.yml" for index in range(4)]
    for artefact in planted:
        _write(artefact, "jobs: {}\n")

    stamp = describe_input_revision(base)
    # Derived from the plant rather than written out, so the two cannot
    # drift apart if someone changes how many files the fixture lays down.
    assert f"{len(planted)} uncommitted path(s)" in stamp
    # Still names the commit; the stamp narrows the search rather than
    # refusing to answer. And it is not the no-tracked-content line: this
    # directory does track files, which is precisely why `ls-files` passes.
    assert head in stamp
    assert "no tracked content here" not in stamp


@pytest.mark.usefixtures("git_search_ceiling")
def test_stamp_counts_routine_ignored_files_too_which_is_the_accepted_cost(tmp_path: Path) -> None:
    """Counting ignored paths also counts `__pycache__` and swapfiles.

    This test used to assert the opposite, and the reversal is deliberate.
    An earlier round rejected `--ignored` because it reported routine build
    noise as input dirt, on the strength of 7 such paths under this repo's
    `.github/scripts`. But `.github/scripts` is not an input to this check:
    the directories it is given are `.github/workflows`, and re-measured
    there, `--porcelain --ignored -uall` reports 0 paths against 61 for
    `.github/scripts` -- nothing writes build artefacts next to workflow
    YAML. The control for that zero is that planting one `__pycache__`
    under `.github/workflows` takes it to 1, so the zero is a clean
    directory rather than a command that silently reported nothing.

    So the noise this test once guarded against does not occur on the real
    inputs, while the case the previous test covers does. The over-reporting
    kept here is the price, and it errs toward saying the tree was not fully
    accounted for -- the safe direction for a stamp whose whole purpose is
    to stop an output reading better than its evidence.
    """
    base = tmp_path / "base"
    base.mkdir()
    _write(base / "base-ai-review-single.yml", _BASE_SINGLE_YML)
    (base / ".gitignore").write_text("__pycache__/\n*.swp\n", encoding="utf-8")
    head = _commit_as_repo(base, tag="v1.11.2")
    (base / "__pycache__").mkdir()
    (base / "__pycache__" / "helper.cpython-313.pyc").write_bytes(b"\x00")
    (base / ".base-ai-review-single.yml.swp").write_bytes(b"\x00")

    stamp = describe_input_revision(base)
    assert "2 uncommitted path(s)" in stamp
    assert head in stamp
    assert "tag v1.11.2" in stamp


@pytest.mark.usefixtures("git_search_ceiling")
def test_stamp_counts_every_file_in_an_untracked_directory(tmp_path: Path) -> None:
    """Fifty unpacked files are fifty, not one `?? unpacked/` entry.

    `--untracked-files=normal`, git's default, collapses an untracked
    directory into a single porcelain line, so an artefact directory of fifty
    files reported `1 uncommitted path(s)` and understated how far the tree
    the check read had departed from the commit the same line names. The
    number is there to say how much of the input the SHA does not cover, so
    it has to be a count of what was read.

    The control for the flag not having simply made everything dirty is the
    clean-directory assertion in the ignored-artefact test above: the same
    `--ignored -uall` reports no dirt there until something is planted.
    """
    base, head = _committed_base(tmp_path, tag="v1.11.2")
    unpacked = base / "unpacked"
    unpacked.mkdir()
    for index in range(50):
        _write(unpacked / f"base-ai-review-{index:02d}.yml", "jobs: {}\n")

    stamp = describe_input_revision(base)
    assert "50 uncommitted path(s)" in stamp
    assert head in stamp


@pytest.mark.usefixtures("git_search_ceiling")
def test_stamp_says_the_tracked_content_probe_failed_rather_than_that_nothing_is_tracked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed `ls-files` is not an empty one.

    `_git` answers None for git missing, a non-zero exit and a timeout alike,
    so testing the probe's result for falsity read every one of them as "no
    tracked content here, so the nearest checkout's HEAD does not describe
    it" -- a positive claim about a tree nothing had looked at, which also
    discarded a SHA `rev-parse` had already resolved. An output reading
    better than the evidence behind it is the defect AT-2557 exists to stop,
    so it may not be the one this fix introduces.
    """
    base, head = _committed_base(tmp_path, tag="v1.11.2")

    # Positive control: content really is tracked here, so "no tracked
    # content" is false of this directory however the probe ends up going.
    assert "no tracked content" not in describe_input_revision(base)

    _break_git_on_path(tmp_path, monkeypatch, fails="ls-files")
    stamp = describe_input_revision(base)

    assert "tracked-content check failed" in stamp
    assert "no tracked content here" not in stamp
    assert head in stamp


@pytest.mark.usefixtures("git_search_ceiling")
def test_stamp_says_the_tracked_content_probe_failed_when_it_times_out(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The timeout branch of the same conflation, which is the likelier one:
    a non-zero exit needs a broken checkout, while a stalled git needs only a
    slow or contended filesystem on a CI runner."""
    base, head = _committed_base(tmp_path, tag="v1.11.2")

    monkeypatch.setattr("check_base_wrapper_drift.GIT_TIMEOUT_SECONDS", 1)
    _break_git_on_path(tmp_path, monkeypatch, hangs="ls-files")
    stamp = describe_input_revision(base)

    assert "tracked-content check failed" in stamp
    assert "no tracked content here" not in stamp
    assert head in stamp


@pytest.mark.usefixtures("git_search_ceiling")
def test_stamp_says_the_tag_lookup_failed_rather_than_that_there_is_no_tag(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The same conflation one line down: `no tag at this commit` asserted the
    absence of a tag that was never successfully queried. Here the tag exists,
    so the old wording was not merely unsupported but wrong."""
    base, head = _committed_base(tmp_path, tag="v1.11.2")

    # Positive control: the tag is on this very commit.
    assert "tag v1.11.2" in describe_input_revision(base)

    _break_git_on_path(tmp_path, monkeypatch, fails="--points-at")
    stamp = describe_input_revision(base)

    assert "tag lookup failed" in stamp
    assert "no tag at this commit" not in stamp
    assert head in stamp


@pytest.mark.usefixtures("git_search_ceiling")
def test_stamp_says_the_tag_lookup_failed_when_it_times_out(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The timeout half of the tag probe, which was the one gap in the grid.

    The three other probes each had both failure modes tested; this one had
    only the non-zero exit, while a docstring two tests down claimed all of
    them were covered on both. The claim was wider than the suite, in a file
    whose subject is claims wider than their evidence, so the sibling is
    added rather than the claim narrowed -- the timeout is as reachable here
    as anywhere, since `tag --points-at` scans the whole refs namespace.
    """
    base, head = _committed_base(tmp_path, tag="v1.11.2")

    # Positive control: the tag really is on this commit, so "no tag" would
    # be false of it whatever the probe does.
    assert "tag v1.11.2" in describe_input_revision(base)

    monkeypatch.setattr("check_base_wrapper_drift.GIT_TIMEOUT_SECONDS", 1)
    _break_git_on_path(tmp_path, monkeypatch, hangs="--points-at")
    stamp = describe_input_revision(base)

    assert "tag lookup failed" in stamp
    assert "no tag at this commit" not in stamp
    assert head in stamp


@pytest.mark.usefixtures("git_search_ceiling")
def test_stamp_says_the_dirt_check_failed_rather_than_reporting_a_clean_tree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The third probe's failure branch, which had only the docstring behind it.

    `status is None` was the one branch of the three with no test: collapsing
    it into the clean branch left the whole suite green while the stamp
    printed a bare `@ <sha> (tag v1.11.2)` for a tree nobody had managed to
    inspect. Measured against that mutant on a genuinely dirty fixture, the
    honest line reads `+ 1 uncommitted path(s) ...` and the mutant's reads
    `@ <sha> (tag v1.11.2)` -- an overstatement of exactly the kind this
    ticket exists to stop, hidden by the asymmetry in the tests rather than
    by anything in the code.
    """
    base, head = _committed_base(tmp_path, tag="v1.11.2")
    # Genuinely dirty, so "clean" is false of this tree however the probe goes
    # -- the positive control for the assertion below.
    (base / "base-ai-review-single.yml").write_text(
        textwrap.dedent(_BASE_SINGLE_YML) + "# edited after the commit\n", encoding="utf-8"
    )
    assert "1 uncommitted path(s)" in describe_input_revision(base)

    _break_git_on_path(tmp_path, monkeypatch, fails="status")
    stamp = describe_input_revision(base)

    assert "uncommitted-change check failed" in stamp
    assert "uncommitted path(s)" not in stamp
    assert head in stamp


@pytest.mark.usefixtures("git_search_ceiling")
def test_stamp_says_the_dirt_check_failed_when_it_times_out(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The timeout variant. `status` is the likeliest of the four to stall:
    it is the only one that has to stat the whole working tree.

    This docstring used to claim all three probes were covered identically
    on both failure modes, which was false -- `tag --points-at` had only the
    non-zero exit. The grid is now complete for all four, and the way to
    check that is to grep the suite for `fails=` and `hangs=` and see each
    probe named twice, rather than to trust this sentence."""
    base, head = _committed_base(tmp_path, tag="v1.11.2")
    (base / "base-ai-review-single.yml").write_text(
        textwrap.dedent(_BASE_SINGLE_YML) + "# edited after the commit\n", encoding="utf-8"
    )

    monkeypatch.setattr("check_base_wrapper_drift.GIT_TIMEOUT_SECONDS", 1)
    _break_git_on_path(tmp_path, monkeypatch, hangs="status")
    stamp = describe_input_revision(base)

    assert "uncommitted-change check failed" in stamp
    assert "uncommitted path(s)" not in stamp
    assert head in stamp


def test_every_shell_interpolation_in_the_shim_is_quoted() -> None:
    """The helper's comment says "every", so this is what makes that true.

    It said "every" while `shutil.which("git")` went in raw on the `exec`
    line -- a universal claim wider than the code, in the file whose subject
    is that mismatch. Prose cannot hold the claim as the helper grows, so it
    is enumerated here instead: every formatted value inside
    `_break_git_on_path` must pass through `shlex.quote`.

    Denies by default. A new interpolation is a failure until it is quoted
    or explicitly added to the waiver below, rather than passing unnoticed
    because nobody re-read the comment.
    """
    source = Path(__file__).read_text(encoding="utf-8")
    fn = next(
        node
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.FunctionDef) and node.name == "_break_git_on_path"
    )
    # Only the expressions that become shell *source*: the guard lines and
    # the script written to disk. The PATH value handed to monkeypatch.setenv
    # is also an f-string in this function, and quoting it would be wrong --
    # it is an environment value, never parsed by a shell. Scoping the check
    # to the claim keeps it from flagging something it does not mean.
    shell_text = [
        node
        for node in ast.walk(fn)
        if isinstance(node, ast.Call)
        and ast.unparse(node.func) in {"guards.append", "shim.write_text"}
    ]
    formatted = [n for call in shell_text for n in ast.walk(call) if isinstance(n, ast.FormattedValue)]
    unquoted = [
        ast.unparse(n.value) for n in formatted if not ast.unparse(n.value).startswith("shlex.quote(")
    ]

    assert not unquoted, f"unquoted shell interpolation(s): {unquoted}"
    # Control: the walk really found interpolations, so an empty `unquoted`
    # means they were all quoted and not that none were seen.
    assert len(formatted) >= 5, f"expected the shim's interpolations, saw {len(formatted)}"


def test_printable_survives_an_encoding_that_cannot_hold_the_text() -> None:
    """The stamp's own `print` is on the pre-try path too.

    Found by enumerating that path rather than by review: `errors="replace"`
    puts U+FFFD into the quoted complaint, and an ASCII-only stdout -- which
    CI images do have -- then makes `print` raise UnicodeEncodeError before
    `run`'s `try`, losing the line the same way the decode and the stat did.
    Measured: U+FFFD through an `ascii`/`strict` writer raises; through
    `backslashreplace` it does not.

    The control is the UTF-8 case: a non-ASCII path must still print as
    itself, or the fix would have cost legibility everywhere to protect one
    locale.
    """
    text = "base input revision: /x @ git said: fatal: \ufffd\ufffd bad"

    def writer(encoding: str) -> io.TextIOWrapper:
        return io.TextIOWrapper(io.BytesIO(), encoding=encoding, errors="strict")

    # The unprotected text really does raise on an ASCII stdout: without this
    # the rest of the test would pass against a problem that is not there.
    with pytest.raises(UnicodeEncodeError):
        print(text, file=writer("ascii"))

    ascii_out = writer("ascii")
    with contextlib.redirect_stdout(ascii_out):
        safe = _printable(text)
    print(safe, file=ascii_out)  # must not raise
    assert "bad" in safe
    assert "\ufffd" not in safe

    # Control: a UTF-8 stdout leaves the text exactly as it was, so the
    # escaping is paid for only where it is needed.
    utf8_out = writer("utf-8")
    with contextlib.redirect_stdout(utf8_out):
        assert _printable(text) == text


@pytest.mark.usefixtures("git_search_ceiling")
def test_the_shim_emits_exactly_the_message_it_was_given(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The guard must plant what it says it planted.

    `_break_git_on_path` interpolates into `/bin/sh`. Unquoted, a message
    carrying a `$` or a double quote -- the shape of a real git complaint,
    `... at "$HOME/x"` -- is expanded or terminates the word. Measured
    before the fix, with HOME set to a sentinel: the shim emitted
    `fatal: ... at /home/SENTINEL/x`, quotes eaten and the variable
    expanded, while an assertion naming the planted text would have been
    checking a different string and could still pass.

    So this asserts byte-for-byte equality rather than containment: a
    substring check is exactly what would not notice the substitution.
    """
    base, _ = _committed_base(tmp_path)
    tricky = 'fatal: dubious ownership at "$HOME/x" `id` \\n end'
    monkeypatch.setenv("HOME", "/home/SENTINEL")

    _break_git_on_path(tmp_path, monkeypatch, fails="rev-parse", fails_message=tricky)
    stamp = describe_input_revision(base)

    quoted = stamp.split("git said: ", 1)[1]
    assert quoted == tricky, f"shim emitted {quoted!r}, not the planted {tricky!r}"
    # The sentinel must not appear: its presence would mean $HOME expanded.
    assert "SENTINEL" not in stamp


@pytest.mark.usefixtures("git_search_ceiling")
def test_stamp_marks_a_complaint_whose_later_lines_were_dropped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A multi-line complaint quoted at one line must say so.

    The character cut carries a marker; the line cut did not. On the
    dubious-ownership case the module cites, git writes four lines and the
    three dropped ones carry the remediation
    (`git config --global --add safe.directory <path>`), so the stamp
    presented a fragment as the whole complaint -- the same
    elision-without-a-marker, one axis over.

    The control is the single-line complaint asserted first: a complaint
    with nothing dropped must carry no line marker, or the marker would be
    noise rather than information.
    """
    base, _ = _committed_base(tmp_path)

    _break_git_on_path(tmp_path, monkeypatch, fails="rev-parse", fails_message="fatal: just one line")
    single = describe_input_revision(base)
    assert "more line(s)" not in single

    # Re-plant rather than monkeypatch.undo(): undo would also roll back the
    # fixture's GIT_CONFIG_* and ceiling pins and expose this to the host.
    _break_git_on_path(
        tmp_path, monkeypatch, fails="rev-parse", fails_message="fatal: first\nsecond\nthird"
    )
    stamp = describe_input_revision(base)

    assert "fatal: first" in stamp
    assert "(+2 more line(s))" in stamp
    assert GIT_COMPLAINT_ELLIPSIS in stamp


@pytest.mark.usefixtures("git_search_ceiling")
def test_stamp_says_it_could_not_inspect_when_the_path_cannot_be_resolved(tmp_path: Path) -> None:
    """`resolve()` is a filesystem call too, and it runs before the stat.

    A symlink loop makes `Path.resolve()` raise `RuntimeError: Symlink loop
    from '...'` -- not an `OSError`, so catching only that would have missed
    it. Measured before the fix: a traceback and no revision line, the same
    invariant the stat guard protects, one call earlier in the same
    function, and reachable from a malformed `--base-dir`.

    The stamp names the *unresolved* path, because when resolution is what
    failed there is no resolved name to report. The control is the first
    assertion: an ordinary directory still resolves and stamps normally.
    """
    base, head = _committed_base(tmp_path, tag="v1.11.2")
    assert head in describe_input_revision(base)

    looping = tmp_path / "loop-a"
    other = tmp_path / "loop-b"
    looping.symlink_to(other)
    other.symlink_to(looping)

    stamp = describe_input_revision(looping)
    assert "could not be inspected" in stamp
    assert str(looping) in stamp
    assert "no such directory" not in stamp


@pytest.mark.skipif(os.getuid() == 0, reason="root bypasses the permission check this exercises")
@pytest.mark.usefixtures("git_search_ceiling")
def test_stamp_says_it_could_not_inspect_rather_than_that_nothing_is_there(tmp_path: Path) -> None:
    """A stat that could not be performed is not a stat that said "absent".

    The rule the other probes follow -- a probe that could not run says so,
    never its negative answer -- did not reach the `exists()`/`is_dir()`
    pair, which cannot express "I could not look". With the parent directory
    chmod 000, `Path.exists()` does not quietly return False: EACCES is not
    in `pathlib`'s ignored-errno set (ENOENT, EBADF, ENOTDIR, ELOOP), so the
    PermissionError propagated out of `describe_input_revision` and reached
    the stamp's `print` in `run`, which sits before `run`'s own `try`.
    Measured before the fix: a traceback and no stamp line at all, in the
    one check that runs before any git probe.

    Two controls, because this test asserts about a directory nobody can
    read: the target is confirmed present before the parent is locked, and
    the "no such directory" wording is asserted absent -- the failure must
    not be folded into the missing case it sits next to.
    """
    parent = tmp_path / "locked"
    target = parent / "workflows"
    target.mkdir(parents=True)
    _write(target / "base-ai-review-single.yml", _BASE_SINGLE_YML)
    assert target.is_dir(), "control: the directory is really there before locking"

    parent.chmod(0o000)
    try:
        stamp = describe_input_revision(target)
    finally:
        # Restore before pytest's own tmp_path cleanup walks it.
        parent.chmod(0o755)

    assert "could not be inspected" in stamp
    assert "no such directory" not in stamp
    assert str(target) in stamp


@pytest.mark.usefixtures("git_search_ceiling")
def test_stamp_marks_a_git_complaint_it_had_to_cut(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A clipped complaint must not read as git's whole sentence.

    `complaint` caps the quoted line, and cutting silently prints a sentence
    ending mid-word as though that were all git said -- an output reading
    more complete than it is, which is this module's subject. The real case
    is `detected dubious ownership in repository at '<long path>'`, which
    runs past the cap on any deeply nested checkout.

    The control is the short-complaint test elsewhere in this file: an
    uncut complaint must carry no marker, or the marker would say nothing.
    """
    base, _ = _committed_base(tmp_path)
    long_message = "fatal: " + ("x" * (GIT_COMPLAINT_QUOTE_LIMIT * 2))

    _break_git_on_path(tmp_path, monkeypatch, fails="rev-parse", fails_message=long_message)
    stamp = describe_input_revision(base)

    assert stamp.endswith(GIT_COMPLAINT_ELLIPSIS)
    # The quoted part is capped, and the marker is extra rather than eating
    # into the quota -- so the reader sees the cap's worth of git's words.
    quoted = stamp.split("git said: ", 1)[1]
    assert len(quoted) == GIT_COMPLAINT_QUOTE_LIMIT + len(GIT_COMPLAINT_ELLIPSIS)


@pytest.mark.usefixtures("git_search_ceiling")
def test_stamp_pluralises_when_two_tags_point_at_the_commit(tmp_path: Path) -> None:
    """`tag v1.11.2, latest` invites reading the second name as not-a-tag.

    A release commit routinely carries its version tag and a moving one, so
    the plural case is ordinary rather than exotic -- and it was unexercised:
    the existing tag test plants exactly one, which cannot tell the singular
    wording from a correct plural one.

    The control is the single-tag assertion first: "tag" must stay singular
    where there is one, or this would be a rename rather than a plural.
    """
    base, _ = _committed_base(tmp_path, tag="v1.11.2")
    single = describe_input_revision(base)
    assert "(tag v1.11.2)" in single

    _fixture_git(base, "tag", "latest")

    stamp = describe_input_revision(base)
    assert "tags " in stamp
    assert "v1.11.2" in stamp and "latest" in stamp
    assert "(tag v" not in stamp


@pytest.mark.usefixtures("git_search_ceiling")
def test_stamp_survives_undecodable_bytes_from_git(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Undecodable stderr must not take the stamp line away.

    `_git_run` runs git under `text=True`, so the decode happens inside
    `subprocess.run`. git does not promise UTF-8 -- a path or a ref can
    carry arbitrary bytes -- and a strict decode then raises
    `UnicodeDecodeError`, which is a `ValueError` and so escaped a handler
    catching only `(OSError, subprocess.SubprocessError)`. It escaped
    `describe_input_revision` too, and reached the stamp's `print` in `run`,
    which sits *before* `run`'s own `try`. Measured before the fix: a
    traceback and no stamp line at all, against this function's own promise
    that every branch produces text -- the exact shape AT-2557 exists to
    stop, since an output missing its first line looks like a good one.

    The control is the clean stamp taken first: the same fixture produces a
    real revision line, so a line appearing below is the fix and not a
    fixture that was degenerate to begin with.
    """
    base, head = _committed_base(tmp_path, tag="v1.11.2")

    control = describe_input_revision(base)
    assert head in control and "tag v1.11.2" in control

    _break_git_on_path(tmp_path, monkeypatch, garbles="rev-parse")
    stamp = describe_input_revision(base)

    # A line, not an exception -- and one that still names the directory.
    assert stamp
    assert str(base) in stamp
    # git's complaint comes through with the undecodable bytes replaced,
    # rather than being dropped or taking the line with it.
    assert "no revision resolved here" in stamp
    assert "bad" in stamp


@pytest.mark.usefixtures("git_search_ceiling")
def test_stamp_says_no_such_directory_rather_than_not_a_git_checkout(tmp_path: Path) -> None:
    """A path that is not there is not a path without a checkout.

    `git -C <missing> rev-parse HEAD` exits 128 with empty stdout, exactly
    as it does for a real directory that no checkout covers, so both landed
    on "not a git checkout (or no commit yet)". `Path.resolve()` does not
    raise for a missing path and the stamp prints before `run`'s own error
    handling, so nothing else caught it. Measured before the fix, the two
    stamps were identical after the path itself -- which makes a mistyped
    `--base-dir` read as a successfully-inspected directory that happens to
    hold no checkout, and that is the likeliest way this ticket's defect
    actually reaches someone.

    The control is the second stamp: a directory that *does* exist with no
    checkout in it must still get its own distinct line, or this fix would
    have traded one wrong sentence for another. That line now quotes git
    rather than classifying the exit code, so the control looks for git's
    own words.
    """
    missing = tmp_path / "typo-in-base-dir" / "workflows"
    assert not missing.exists()

    stamp = describe_input_revision(missing)
    assert "no such directory" in stamp
    assert "git said" not in stamp

    present_without_checkout = tmp_path / "unpacked-tarball"
    present_without_checkout.mkdir()
    _write(present_without_checkout / "base-ai-review-single.yml", _BASE_SINGLE_YML)

    control = describe_input_revision(present_without_checkout)
    assert "no revision resolved here" in control
    assert "not a git repository" in control
    assert "no such directory" not in control


@pytest.mark.usefixtures("git_search_ceiling")
def test_stamp_says_not_a_directory_separately_from_missing(tmp_path: Path) -> None:
    """A path that exists and is not a directory is a third fact.

    `is_dir()` is false both for an absent path and for a file, a symlink to
    a file or a device node, so `--base-dir` aimed at a single workflow file
    -- an easy slip, since the surrounding tooling takes both -- was stamped
    "no such directory", which is simply false about the filesystem.
    Measured before the split: a path with `exists()` True and `is_file()`
    True stamped `@ no such directory, so there is nothing here to
    describe`.

    The control is the first assertion: the genuinely absent path must keep
    its own line, so the split is a split and not a rename.
    """
    base, _ = _committed_base(tmp_path, tag="v1.11.2")

    absent = tmp_path / "not-there"
    assert "no such directory" in describe_input_revision(absent)

    a_file = base / "base-ai-review-single.yml"
    assert a_file.is_file()

    stamp = describe_input_revision(a_file)
    assert "exists but is not a directory" in stamp
    assert "no such directory" not in stamp


@pytest.mark.usefixtures("git_search_ceiling")
def test_stamp_says_the_head_lookup_failed_rather_than_that_this_is_no_checkout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The fourth probe, and the one that decides whether the others run.

    `rev-parse` was the last site still reporting a failure as its negative
    answer: `if not head` collapsed a timeout into "not a git checkout",
    which is a claim about the directory that a git which never ran cannot
    support. Measured before the fix, with `rev-parse` timing out against a
    checkout tagged v1.11.2, the stamp read
    `@ not a git checkout (or git unavailable, or no commit yet)` -- the
    worst of the four to guess at, because it is the one branch that
    suppresses the SHA, the tag and the dirt count all at once.
    """
    base, head = _committed_base(tmp_path, tag="v1.11.2")

    # Positive control: this directory really is a checkout at a tag, so
    # "not a git checkout" is false of it however the probe ends up going.
    control = describe_input_revision(base)
    assert head in control and "tag v1.11.2" in control

    monkeypatch.setattr("check_base_wrapper_drift.GIT_TIMEOUT_SECONDS", 1)
    _break_git_on_path(tmp_path, monkeypatch, hangs="rev-parse")
    stamp = describe_input_revision(base)

    assert "HEAD lookup failed" in stamp
    assert "not a git checkout" not in stamp


@pytest.mark.usefixtures("git_search_ceiling")
def test_stamp_quotes_git_when_rev_parse_ran_and_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The other half of the split, and it quotes rather than classifies.

    This test used to assert "not a git checkout" here, on the premise that a
    non-zero `rev-parse` has answered that there is no HEAD. The premise was
    too strong: git exits 128 for `detected dubious ownership` and for a
    `.git` it cannot parse, both of which are checkouts, so the exit code
    does not carry the diagnosis the sentence made. The branch now reports
    git's own first line and claims nothing -- which still distinguishes the
    cases, because git's words differ even where its exit code does not.

    The shim's complaint stands in for whatever git would have said; the
    stamp must carry it through rather than replace it with a conclusion.
    """
    base, _ = _committed_base(tmp_path)

    _break_git_on_path(tmp_path, monkeypatch, fails="rev-parse")
    stamp = describe_input_revision(base)

    assert "no revision resolved here" in stamp
    assert "forced git failure" in stamp
    assert "not a git checkout" not in stamp
    assert "HEAD lookup failed" not in stamp


@pytest.mark.usefixtures("git_search_ceiling")
def test_stamp_says_so_when_the_input_is_not_a_git_checkout(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """An unpacked tarball or an artefact directory has no revision to name.

    It must say that in words. A stamp that prints nothing here rebuilds the
    hole it exists to close: the reader sees a line-less output and has no
    way to tell "no revision" from "revision not asked for".

    The words are git's own since the branch stopped classifying exit codes,
    and for this case git's are `fatal: not a git repository` -- so the
    plainest reading of the commonest finding survived the change to
    quoting, which is the thing worth asserting here.
    """
    _, lines = _stamped_run(tmp_path, capsys, base_is_repo=False)
    assert lines[0].startswith("base input revision:")
    assert "no revision resolved here" in lines[0]
    assert "not a git repository" in lines[0]


@pytest.mark.usefixtures("git_search_ceiling")
def test_stamp_is_printed_even_when_the_check_fails(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """The failing output gets attached as evidence too, and a reviewer
    rebutting a finding needs the input revision at least as much."""
    code, lines = _stamped_run(tmp_path, capsys, with_round_cutoff=False)
    assert code == 1
    assert lines[0].startswith("base input revision:")
    assert "DRIFT FOUND:" in lines


@pytest.mark.usefixtures("git_search_ceiling")
def test_stamp_is_printed_before_a_malformed_config_aborts(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    base_dir, wrapper_dir = tmp_path / "base", tmp_path / "wrapper"
    base_dir.mkdir()
    wrapper_dir.mkdir()
    correspondence = tmp_path / "correspondence.yml"
    correspondence.write_text("steps: [unclosed\n", encoding="utf-8")
    exceptions = tmp_path / "exceptions.yml"
    exceptions.write_text("vars: {}\nenv: {}\n", encoding="utf-8")

    assert run(base_dir, wrapper_dir, correspondence, exceptions) == 1
    lines = capsys.readouterr().out.splitlines()
    assert lines[0].startswith("base input revision:")
    assert "malformed drift-check config" in lines[2]


# ---------------------------------------------------------------------------
# The live correspondence against this repo's own workflow files (no wrapper
# needed, so this runs on every PR): a base change that adds an env-bearing
# step, or renames one, fails here before it can go unmapped on main.
# ---------------------------------------------------------------------------


def test_live_correspondence_and_exceptions_are_well_formed() -> None:
    config = load_correspondence(LIVE_CORRESPONDENCE)
    load_exceptions(LIVE_EXCEPTIONS)
    assert config.base_files
    assert config.wrapper_files
    assert config.steps
    assert config.outputs


def test_live_correspondence_maps_every_env_bearing_base_step() -> None:
    config = load_correspondence(LIVE_CORRESPONDENCE)
    mapped = {entry.key for entry in config.steps}
    unmapped = [
        f"{base_file}:{name!r}"
        for base_file in config.base_files
        for name in steps_with_env(_load_yaml(LIVE_WORKFLOWS / base_file))
        if name is None or (base_file, name) not in mapped
    ]
    assert unmapped == [], "add a correspondence entry (or a no-counterpart reason) for each"


def test_live_correspondence_names_only_existing_base_steps() -> None:
    config = load_correspondence(LIVE_CORRESPONDENCE)
    for entry in config.steps:
        find_step_env_keys(_load_yaml(LIVE_WORKFLOWS / entry.base_file), entry.base_step)


def test_live_exceptions_name_only_mapped_steps() -> None:
    """An env exception for a step the correspondence does not map would
    never be consulted -- it would look like coverage and excuse nothing."""
    config = load_correspondence(LIVE_CORRESPONDENCE)
    mapped = {entry.key for entry in config.steps if not entry.has_no_counterpart}
    assert set(load_exceptions(LIVE_EXCEPTIONS).env) <= mapped


def test_live_correspondence_maps_every_output_bearing_base_file() -> None:
    """Axis 3's completeness against this repo's own tree: a base workflow
    that declares workflow_call outputs and is not in the correspondence
    would be excluded from the comparison silently."""
    config = load_correspondence(LIVE_CORRESPONDENCE)
    mapped = {entry.base_file for entry in config.outputs}
    unmapped = [
        base_file
        for base_file in config.base_files
        if workflow_call_outputs(_load_yaml(LIVE_WORKFLOWS / base_file)) and base_file not in mapped
    ]
    assert unmapped == [], "add an 'outputs' correspondence entry (or a no-counterpart reason) for each"


def test_live_outputs_correspondence_names_only_existing_base_files() -> None:
    config = load_correspondence(LIVE_CORRESPONDENCE)
    for entry in config.outputs:
        assert (LIVE_WORKFLOWS / entry.base_file).exists(), entry.base_file


def test_live_outputs_exceptions_name_only_mapped_files() -> None:
    """Same rule as the env exceptions: an entry for a file the
    correspondence does not map would never be consulted."""
    config = load_correspondence(LIVE_CORRESPONDENCE)
    mapped = {entry.base_file for entry in config.outputs if not entry.has_no_counterpart}
    assert set(load_exceptions(LIVE_EXCEPTIONS).outputs) <= mapped


def test_live_roster_outputs_are_on_the_axis(tmp_path: Path) -> None:
    """The AT-2511 outputs must actually be compared, not merely declared.
    Run the live config's axis 3 against a wrapper that declares nothing --
    the shape the real wrapper is in -- and require every roster name to be
    reported. Written against the live workflow files so that removing the
    orchestrator's re-export, or dropping its correspondence entry, is what
    fails here."""
    wrapper_dir = tmp_path / "wrapper"
    wrapper_dir.mkdir()
    (wrapper_dir / "wrapper.yml").write_text(
        "on:\n  workflow_call:\n    inputs:\n      pr_number: {type: string}\njobs:\n  review:\n    steps: []\n",
        encoding="utf-8",
    )
    findings, _ = check_workflow_outputs(
        LIVE_WORKFLOWS, wrapper_dir, load_correspondence(LIVE_CORRESPONDENCE), load_exceptions(LIVE_EXCEPTIONS)
    )
    for key in _ROSTER_KEYS:
        assert any(f"base-ai-review-orchestrator.yml declares workflow_call output {key!r}" in f for f in findings), (
            f"{key} is declared on the orchestrator but axis 3 does not compare it"
        )


# ---------------------------------------------------------------------------
# Regression proof against the real AT-2120 trees (see module docstring)
# ---------------------------------------------------------------------------


def _git(*args: str, cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=False)


@pytest.fixture(scope="module")
def base_tree(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Base's workflow files at BASE_PIN, materialised from this repo's history.

    A shallow checkout (actions/checkout defaults to depth 1) does not carry
    the commit; fetching it by SHA is what GitHub allows for any reachable
    commit. If it still cannot be read the test fails: a regression proof
    that skips is indistinguishable from one that passes.
    """
    probe = _git("cat-file", "-e", f"{BASE_PIN}^{{commit}}", cwd=REPO_ROOT)
    if probe.returncode != 0:
        fetched = _git("fetch", "--depth=1", "origin", BASE_PIN, cwd=REPO_ROOT)
        if fetched.returncode != 0:
            pytest.fail(f"base pin {BASE_PIN} is not in this checkout and could not be fetched: {fetched.stderr}")
    out = tmp_path_factory.mktemp("base")
    for name in BASE_FILES:
        shown = _git("show", f"{BASE_PIN}:.github/workflows/{name}", cwd=REPO_ROOT)
        if shown.returncode != 0:
            pytest.fail(f"git show {BASE_PIN}:.github/workflows/{name} failed: {shown.stderr}")
        (out / name).write_text(shown.stdout, encoding="utf-8")
    return out


@pytest.fixture(scope="module")
def wrapper_pre(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """wrapper.yml at 7de4d6e: the post-fix file with the fix reverse-applied.

    The patch carries the fix's own path (.github/workflows/wrapper.yml), so
    the copy is laid out the same way and `git apply -R` is run from the
    root of that layout; git apply works as a plain patch tool outside a
    repository. The result is then checked against the post-fix file line by
    line: dropping every ROUND_CUTOFF_* line from post must give pre exactly,
    so a patch that touched anything else (or nothing) fails here, not in
    the assertions that depend on it.
    """
    root = tmp_path_factory.mktemp("wrapper-pre")
    workflows = root / ".github" / "workflows"
    workflows.mkdir(parents=True)
    shutil.copyfile(WRAPPER_POST / "wrapper.yml", workflows / "wrapper.yml")
    applied = _git("apply", "-R", str(AT_2120_FIX_PATCH), cwd=root)
    # A non-clean apply (fuzz, offset, rejected hunk) is a failure of the
    # proof, never a skip: pytest reports every dependent test as an error.
    assert applied.returncode == 0, applied.stderr
    post_lines = (WRAPPER_POST / "wrapper.yml").read_text(encoding="utf-8").splitlines()
    pre_lines = (workflows / "wrapper.yml").read_text(encoding="utf-8").splitlines()
    removed = [line for line in post_lines if "ROUND_CUTOFF" in line]
    assert [line for line in post_lines if "ROUND_CUTOFF" not in line] == pre_lines
    assert sorted(line.split(":")[0].strip() for line in removed) == sorted(AT_2120_KEYS * 3)
    assert not any("ROUND_CUTOFF" in line for line in pre_lines)
    return workflows


@pytest.fixture(scope="module")
def live_config() -> DriftConfig:
    return load_correspondence(LIVE_CORRESPONDENCE)


@pytest.fixture(scope="module")
def live_exceptions() -> Exceptions:
    return load_exceptions(LIVE_EXCEPTIONS)


AGGREGATE_STEP = ("base-ai-review-aggregate.yml", "Aggregate and post verdict")
VENDORED_HEAD_SHA_REASON = (
    "Test-only. The vendored wrapper snapshot (82d261d) predates the AT-2117 "
    "port, so HEAD_SHA is re-excused here to keep the AT-2120 proof about "
    "AT-2120; the live exceptions.yml no longer carries this entry."
)


@pytest.fixture(scope="module")
def vendored_exceptions(live_exceptions: Exceptions) -> Exceptions:
    """The live exceptions plus the HEAD_SHA entry the vendored wrapper needs."""
    return Exceptions(
        vars=live_exceptions.vars,
        env={
            **live_exceptions.env,
            AGGREGATE_STEP: {**live_exceptions.env.get(AGGREGATE_STEP, {}), "HEAD_SHA": VENDORED_HEAD_SHA_REASON},
        },
    )


@pytest.fixture(scope="module")
def vendored_exceptions_file(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """``vendored_exceptions`` as a file, for the end-to-end ``run`` proof."""
    import yaml

    data = _load_yaml(LIVE_EXCEPTIONS)
    data.setdefault("env", {}).setdefault(AGGREGATE_STEP[0], {}).setdefault(AGGREGATE_STEP[1], {})["HEAD_SHA"] = {
        "reason": VENDORED_HEAD_SHA_REASON
    }
    path = tmp_path_factory.mktemp("exceptions") / "exceptions.yml"
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    return path


# Base steps added after BASE_PIN. Neither the pinned base files nor the
# vendored wrapper carry them, so their live correspondence entries would
# raise StepNotFoundError on both sides of the proof. They are dropped from
# the regression config only; the live-correspondence tests above still hold
# every entry against the current tree.
POST_PIN_STEPS = frozenset(
    {
        ("base-ai-review-prepare.yml", "Filter policy-excluded files"),  # AT-2206
        ("base-ai-review-single.yml", "Validate timeout inputs"),  # AT-2876
        ("base-ai-review-single.yml", "Emit Gemini error verdict (no verdict file)"),  # AT-2539
        ("base-ai-review-prepare.yml", "Resolve review target"),  # AT-2520
        ("base-ai-review-prepare.yml", "Resolve target PR head"),  # AT-2520
        ("base-ai-review-single.yml", "Resolve review target"),  # AT-2520
        ("base-ai-review-aggregate.yml", "Resolve review target"),  # AT-2520
    }
)


@pytest.fixture(scope="module")
def vendored_config(live_config: DriftConfig) -> DriftConfig:
    """The live correspondence minus the steps the vendored trees predate."""
    return DriftConfig(
        base_files=live_config.base_files,
        wrapper_files=live_config.wrapper_files,
        steps=tuple(entry for entry in live_config.steps if entry.key not in POST_PIN_STEPS),
    )


@pytest.fixture(scope="module")
def vendored_correspondence_file(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """``vendored_config`` as a file, for the end-to-end ``run`` proof."""
    import yaml

    data = _load_yaml(LIVE_CORRESPONDENCE)
    data["steps"] = [
        step for step in data["steps"] if (step["base_file"], step["base_step"]) not in POST_PIN_STEPS
    ]
    path = tmp_path_factory.mktemp("correspondence") / "correspondence.yml"
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    return path


def test_post_pin_steps_are_live_entries_absent_at_the_pin(base_tree: Path, live_config) -> None:
    """The set must stay honest in both directions: every listed step is a
    real live entry, and none exists at the pin -- once the pin moves past a
    step, dropping it would silently narrow the proof."""
    live_keys = {entry.key for entry in live_config.steps}
    for base_file, step in POST_PIN_STEPS:
        assert (base_file, step) in live_keys, f"{step!r} is not in the live correspondence"
        with pytest.raises(StepNotFoundError):
            find_step_env_keys(_load_yaml(base_tree / base_file), step)


def test_regression_fixture_is_the_at_2120_fix_commit() -> None:
    """The patch must be the fix itself, not a hand-written lookalike."""
    header = AT_2120_FIX_PATCH.read_text(encoding="utf-8").splitlines()[:5]
    assert header[0].startswith("From 82d261d08ea1449d8db8f70756b894fd51eecc38 ")
    assert any("fix(AT-2120): wire round-cutoff vars into wrapper inline" in line for line in header)
    # The provenance record beside the fixtures must name the same commit.
    assert "82d261d08ea1449d8db8f70756b894fd51eecc38" in (FIXTURES / "SOURCE").read_text(encoding="utf-8")


def test_regression_at_2120_pre_fix_tree_fails_on_exactly_the_incident(
    base_tree: Path, wrapper_pre: Path, vendored_config, vendored_exceptions
) -> None:
    env_findings, _ = check_env_keys(base_tree, wrapper_pre, vendored_config, vendored_exceptions)
    vars_findings, _, _ = check_vars_consumed(base_tree, wrapper_pre, vendored_config, vendored_exceptions)
    assert env_findings == [
        "env drift: base step base-ai-review-single.yml:'Post inline comments' sets "
        f"{key!r}, which none of wrapper's ['Post Claude inline comments', "
        "'Post Codex inline comments', 'Post Gemini inline comments'] (wrapper.yml) set"
        for key in AT_2120_KEYS
    ]
    assert vars_findings == [
        f"vars drift: base consumes vars.{key}, wrapper never reads it "
        "(a consumer setting it has no effect and no error)"
        for key in AT_2120_KEYS
    ]


def test_regression_at_2120_post_fix_tree_passes(base_tree: Path, vendored_config, vendored_exceptions) -> None:
    env_findings, _ = check_env_keys(base_tree, WRAPPER_POST, vendored_config, vendored_exceptions)
    vars_findings, _, _ = check_vars_consumed(base_tree, WRAPPER_POST, vendored_config, vendored_exceptions)
    assert env_findings == []
    assert vars_findings == []


def test_regression_at_2120_end_to_end_exit_codes(
    base_tree: Path, wrapper_pre: Path, vendored_correspondence_file: Path, vendored_exceptions_file: Path
) -> None:
    assert run(base_tree, wrapper_pre, vendored_correspondence_file, vendored_exceptions_file) == 1
    assert run(base_tree, WRAPPER_POST, vendored_correspondence_file, vendored_exceptions_file) == 0


@pytest.mark.parametrize("tree", ["pre", "post"])
def test_regression_at_2117_head_sha_is_caught_against_the_vendored_wrapper(
    tree: str, base_tree: Path, wrapper_pre: Path, vendored_config, live_exceptions
) -> None:
    """The secondary case AT-2122 names: HEAD_SHA not passed to aggregation.
    Both vendored trees predate the wrapper port (wrapper PR #36), and the
    live exceptions.yml stopped excusing it with that port, so the check
    must report it against either tree as the file stands."""
    assert "HEAD_SHA" not in live_exceptions.env.get(AGGREGATE_STEP, {}), (
        "exceptions.yml lists HEAD_SHA again; the AT-2117 port is in the wrapper, so this is a regression"
    )
    wrapper_dir = wrapper_pre if tree == "pre" else WRAPPER_POST
    findings, _ = check_env_keys(base_tree, wrapper_dir, vendored_config, live_exceptions)
    expected = (
        "env drift: base step base-ai-review-aggregate.yml:'Aggregate and post verdict' sets "
        "'HEAD_SHA', which none of wrapper's ['Aggregate and post verdict'] (wrapper.yml) set"
    )
    assert expected in findings
    if tree == "post":
        # With AT-2120 fixed, AT-2117 is the only thing left to report.
        assert findings == [expected]


def test_regression_exceptions_do_not_excuse_the_at_2120_keys(live_exceptions) -> None:
    """The escape hatch must never be widened to swallow the incident it was
    built to catch. Reading the file is cheaper than waiting for the pre-fix
    regression to go green for the wrong reason."""
    for key in AT_2120_KEYS:
        assert key not in live_exceptions.vars
        for reasons in live_exceptions.env.values():
            assert key not in reasons
