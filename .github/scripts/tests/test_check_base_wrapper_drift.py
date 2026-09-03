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
   trees and is proven the same way with its exception lifted.

   Both repositories are public, so ``git show <sha>:.github/workflows/<file>``
   in either confirms every artefact. Because the regression uses the *live*
   config, a base step rename shows up here as StepNotFoundError: move the
   base pin to the commit that renamed it and re-check the four expected
   findings.
"""

from __future__ import annotations

import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest
from check_base_wrapper_drift import (
    DriftConfig,
    Exceptions,
    MalformedConfigError,
    StepCorrespondence,
    StepNotFoundError,
    check_env_keys,
    check_vars_consumed,
    extract_vars_consumed,
    find_step_env_keys,
    load_correspondence,
    load_exceptions,
    run,
    steps_with_env,
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


def _load_yaml(path: Path) -> dict:
    import yaml

    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


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
    assert extract_vars_consumed(text) == {"REVIEW_MODE", "JACCARD_THRESHOLD"}


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


def test_run_exits_one_on_a_malformed_exceptions_file(tmp_path: Path) -> None:
    assert _run_dirs(tmp_path, with_round_cutoff=True, exceptions_text="vars:\n  SOMETHING: {}\nenv: {}\n") == 1


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


def test_regression_fixture_is_the_at_2120_fix_commit() -> None:
    """The patch must be the fix itself, not a hand-written lookalike."""
    header = AT_2120_FIX_PATCH.read_text(encoding="utf-8").splitlines()[:5]
    assert header[0].startswith("From 82d261d08ea1449d8db8f70756b894fd51eecc38 ")
    assert any("fix(AT-2120): wire round-cutoff vars into wrapper inline" in line for line in header)
    # The provenance record beside the fixtures must name the same commit.
    assert "82d261d08ea1449d8db8f70756b894fd51eecc38" in (FIXTURES / "SOURCE").read_text(encoding="utf-8")


def test_regression_at_2120_pre_fix_tree_fails_on_exactly_the_incident(
    base_tree: Path, wrapper_pre: Path, live_config, live_exceptions
) -> None:
    env_findings, _ = check_env_keys(base_tree, wrapper_pre, live_config, live_exceptions)
    vars_findings, _, _ = check_vars_consumed(base_tree, wrapper_pre, live_config, live_exceptions)
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


def test_regression_at_2120_post_fix_tree_passes(base_tree: Path, live_config, live_exceptions) -> None:
    env_findings, _ = check_env_keys(base_tree, WRAPPER_POST, live_config, live_exceptions)
    vars_findings, _, _ = check_vars_consumed(base_tree, WRAPPER_POST, live_config, live_exceptions)
    assert env_findings == []
    assert vars_findings == []


def test_regression_at_2120_end_to_end_exit_codes(base_tree: Path, wrapper_pre: Path) -> None:
    assert run(base_tree, wrapper_pre, LIVE_CORRESPONDENCE, LIVE_EXCEPTIONS) == 1
    assert run(base_tree, WRAPPER_POST, LIVE_CORRESPONDENCE, LIVE_EXCEPTIONS) == 0


@pytest.mark.parametrize("tree", ["pre", "post"])
def test_regression_at_2117_head_sha_is_caught_once_its_exception_is_lifted(
    tree: str, base_tree: Path, wrapper_pre: Path, live_config, live_exceptions
) -> None:
    """The secondary case AT-2122 names: HEAD_SHA not passed to aggregation.
    It is open in both trees and only silent because exceptions.yml declares
    it as tracked drift; without that entry the check reports it."""
    aggregate = ("base-ai-review-aggregate.yml", "Aggregate and post verdict")
    assert "HEAD_SHA" in live_exceptions.env[aggregate], "exceptions.yml no longer lists HEAD_SHA; AT-2117 closed?"
    lifted = Exceptions(
        vars=live_exceptions.vars,
        env={
            **live_exceptions.env,
            aggregate: {k: v for k, v in live_exceptions.env[aggregate].items() if k != "HEAD_SHA"},
        },
    )
    wrapper_dir = wrapper_pre if tree == "pre" else WRAPPER_POST
    findings, _ = check_env_keys(base_tree, wrapper_dir, live_config, lifted)
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
