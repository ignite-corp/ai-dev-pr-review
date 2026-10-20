"""Where prepare takes the review prompts from (AT-2520).

The `Extract diff and context` step of base-ai-review-prepare.yml is run here
as the shell script it is, against real local git repositories, and the
context.md it writes is read back.

The order is: the target's base branch (a PR cannot rewrite its own review
instructions), then the PR head (first-time onboarding, before the files reach
the base), then -- only when the caller passed `default_prompts_path` -- the
caller's own copy under that directory. Base ships no default of its own:
without the input a target with no prompts fails prepare, as it always has.

Only ABSENCE falls through to the default. A prompt that is present but empty,
or is not a regular file, fails the step: replacing a broken prompt the target
owns with a default would hide the breakage behind a review that looks normal.
Every source is read from git objects as a regular file, so a symlink can
never turn a prompt path into a read of a runner-local file.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
import yaml
from test_prepare_pr_metadata import (
    _GIT_ENV,
    SCRIPT_DIR,
    WORKFLOW,
    _commit,
    _git,
    _render,
    _step,
)

SYSTEM = ".github/prompts/code-review-system.md"
CHECKLIST = ".github/prompts/code-review-checklist.md"

# What a caller such as the LENS runner passes as default_prompts_path.
DEFAULTS_DIR = "default-prompts"
CALLER_SYSTEM = f"{DEFAULTS_DIR}/code-review-system.md"
CALLER_CHECKLIST = f"{DEFAULTS_DIR}/code-review-checklist.md"
CALLER_FILES = {
    CALLER_SYSTEM: "CALLER SYSTEM\n",
    CALLER_CHECKLIST: "CALLER CHECKLIST\n",
}


def _link(root: Path, links: dict[str, Path]) -> None:
    for name, target in links.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.symlink_to(target)


def _make_repo(
    tmp_path: Path,
    *,
    base_files: dict[str, str],
    head_files: dict[str, str],
    base_links: dict[str, Path] | None = None,
    head_links: dict[str, Path] | None = None,
    caller_files: dict[str, str] | None = None,
    caller_links: dict[str, Path] | None = None,
) -> tuple[Path, str]:
    """`origin/main` carrying base_files, a work tree one commit ahead with head_files.

    The `*_links` maps commit a symlink at each path, pointing at the target.
    `caller_files` / `caller_links` build the caller checkout that 'Checkout
    caller default prompts' would have made; neither given means no checkout,
    which is the step's state when the input is unset.
    """
    origin = tmp_path / "origin.git"
    _git(tmp_path, "init", "-q", "--bare", str(origin))

    seed = tmp_path / "seed"
    _git(tmp_path, "init", "-q", "-b", "main", str(seed))
    _link(seed, base_links or {})
    _commit(seed, "base", {"README.md": "base\n", **base_files})
    _git(seed, "remote", "add", "origin", str(origin))
    _git(seed, "push", "-q", "origin", "main")

    work = tmp_path / "work"
    _git(tmp_path, "clone", "-q", str(origin), str(work))
    _git(work, "checkout", "-q", "-b", "main", "origin/main")
    _link(work, head_links or {})
    head_sha = _commit(work, "pr change", {"changed.txt": "hi\n", **head_files})

    # The metadata block imports display_path from the pinned checkout.
    pinned = work / ".ai-dev-pr-review" / ".github" / "scripts"
    pinned.mkdir(parents=True)
    shutil.copy(SCRIPT_DIR / "github_pr_support.py", pinned / "github_pr_support.py")

    if caller_files is not None or caller_links is not None:
        caller = work / ".ai-dev-pr-review-caller"
        _git(work, "init", "-q", "-b", "main", str(caller))
        _link(caller, caller_links or {})
        _commit(caller, "caller", {"README.md": "caller\n", **(caller_files or {})})
    return work, head_sha


# The expression 'Extract diff and context' hands the script: the input on a
# cross-repository run, nothing on a same-repository one. TestWiring pins the
# workflow to this exact text, so the evaluation below is the one GitHub does.
DEFAULT_EXPR = (
    "steps.target.outputs.cross == 'true' && inputs.default_prompts_path || ''"
)


def _run(
    work: Path, head_sha: str, default_prompts_path: str = "", *, cross: bool = True
) -> subprocess.CompletedProcess[str]:
    step = _step("Extract diff and context")
    context = {
        "steps.target_token.outputs.token || github.token": "gh-token",
        "inputs.pr_number || github.event.pull_request.number": "56",
        "inputs.target_repo || github.repository": "ignite-pilot-org/peaknow",
        "steps.refs.outputs.base_ref": "main",
        "steps.refs.outputs.head_sha": head_sha,
        "steps.refs.outputs.pr_author": "someuser",
        "steps.refs.outputs.head_ref": "task/x",
        "steps.refs.outputs.pr_merged": "false",
        "steps.refs.outputs.merge_commit_sha": "",
        "steps.refs.outputs.pr_commits": "1",
        "steps.refs.outputs.labels": "",
        "inputs.code-review-system-prompt-path": SYSTEM,
        "inputs.code-review-checklist-path": CHECKLIST,
        "inputs.default_prompts_path": default_prompts_path,
        DEFAULT_EXPR: default_prompts_path if cross else "",
    }
    script = _render(step["run"], context)
    env = dict(_GIT_ENV)
    job = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))["jobs"]["prepare"]
    for key, value in job.get("env", {}).items():
        env[key] = _render(str(value), context)
    for key, value in step["env"].items():
        env[key] = _render(str(value), context)
    # `bash -e`: what the runner uses for a `run:` with no `shell:` set,
    # which is this step -- no pipefail, no nounset.
    return subprocess.run(
        ["bash", "-e", "-c", script],
        env=env,
        cwd=work,
        capture_output=True,
        text=True,
        timeout=60,
    )


def _context(work: Path) -> str:
    path = work / "context.md"
    return path.read_text(encoding="utf-8") if path.exists() else ""


def _sources(stdout: str) -> list[str]:
    """The one annotation per prompt file that names where it came from."""
    prefixes = (
        "::notice title=Review prompt source",
        "::warning title=Default review prompt",
    )
    return [line for line in stdout.splitlines() if line.startswith(prefixes)]


class TestTargetWithoutPromptsAndNoDefault:
    def test_fails_as_before_when_the_input_is_unset(self, tmp_path: Path) -> None:
        work, head = _make_repo(tmp_path, base_files={}, head_files={})
        result = _run(work, head)
        assert result.returncode != 0
        assert "::error" in result.stdout and SYSTEM in result.stdout
        assert "default_prompts_path" in result.stdout

    def test_base_ships_no_default_of_its_own(self, tmp_path: Path) -> None:
        # A copy of the old canonical location in the pinned checkout must
        # not be picked up: the default is the caller's to supply.
        work, head = _make_repo(tmp_path, base_files={}, head_files={})
        pinned = work / ".ai-dev-pr-review" / "examples" / "prompts"
        pinned.mkdir(parents=True)
        (pinned / "code-review-system.md").write_text(
            "BASE DEFAULT\n", encoding="utf-8"
        )
        (pinned / "code-review-checklist.md").write_text(
            "BASE DEFAULT\n", encoding="utf-8"
        )
        result = _run(work, head)
        assert result.returncode != 0
        assert "BASE DEFAULT" not in _context(work)


class TestCallerDefault:
    def test_reads_the_caller_copy(self, tmp_path: Path) -> None:
        work, head = _make_repo(
            tmp_path, base_files={}, head_files={}, caller_files=CALLER_FILES
        )
        result = _run(work, head, DEFAULTS_DIR)
        assert result.returncode == 0, result.stdout + result.stderr
        assert "CALLER SYSTEM\n\n\n---\n\nCALLER CHECKLIST\n" in _context(work)

    def test_names_the_caller_copy_once_per_prompt_file(self, tmp_path: Path) -> None:
        work, head = _make_repo(
            tmp_path, base_files={}, head_files={}, caller_files=CALLER_FILES
        )
        result = _run(work, head, f"{DEFAULTS_DIR}/")
        assert result.returncode == 0, result.stdout + result.stderr
        notices = _sources(result.stdout)
        assert len(notices) == 2
        # A default is a deliberate caller choice but still the weakest
        # source, and a misspelled prompt path lands here too: warning level.
        assert all(
            n.startswith("::warning title=Default review prompt") for n in notices
        )
        assert SYSTEM in notices[0] and f"caller default {CALLER_SYSTEM}" in notices[0]
        assert (
            CHECKLIST in notices[1]
            and f"caller default {CALLER_CHECKLIST}" in notices[1]
        )

    def test_one_missing_file_defaults_alone(self, tmp_path: Path) -> None:
        work, head = _make_repo(
            tmp_path,
            base_files={SYSTEM: "TARGET SYSTEM\n"},
            head_files={},
            caller_files=CALLER_FILES,
        )
        result = _run(work, head, DEFAULTS_DIR)
        assert result.returncode == 0, result.stdout + result.stderr
        context_md = _context(work)
        assert "TARGET SYSTEM\n\n\n---\n\nCALLER CHECKLIST\n" in context_md
        assert "CALLER SYSTEM" not in context_md
        notices = _sources(result.stdout)
        assert notices[0].startswith("::notice") and "base branch" in notices[0]
        assert notices[1].startswith("::warning")
        assert f"caller default {CALLER_CHECKLIST}" in notices[1]

    def test_target_prompts_win_over_the_caller_copy(self, tmp_path: Path) -> None:
        work, head = _make_repo(
            tmp_path,
            base_files={SYSTEM: "BASE SYSTEM\n", CHECKLIST: "BASE CHECKLIST\n"},
            head_files={},
            caller_files=CALLER_FILES,
        )
        result = _run(work, head, DEFAULTS_DIR)
        assert result.returncode == 0, result.stdout + result.stderr
        assert "CALLER" not in _context(work)

    def test_caller_without_the_file_fails(self, tmp_path: Path) -> None:
        work, head = _make_repo(
            tmp_path,
            base_files={},
            head_files={},
            caller_files={CALLER_SYSTEM: "CALLER SYSTEM\n"},
        )
        result = _run(work, head, DEFAULTS_DIR)
        assert result.returncode != 0
        assert "::error" in result.stdout and CALLER_CHECKLIST in result.stdout

    @pytest.mark.parametrize("content", ["", "\n  \n"])
    def test_empty_caller_copy_fails(self, tmp_path: Path, content: str) -> None:
        work, head = _make_repo(
            tmp_path,
            base_files={},
            head_files={},
            caller_files={CALLER_SYSTEM: content, CALLER_CHECKLIST: "C\n"},
        )
        result = _run(work, head, DEFAULTS_DIR)
        assert result.returncode != 0
        assert "::error title=Empty review prompt" in result.stdout

    def test_symlinked_caller_copy_fails(self, tmp_path: Path) -> None:
        outside = tmp_path / "outside"
        outside.mkdir()
        secret = outside / "secret.txt"
        secret.write_text("PLANTED-SECRET\n", encoding="utf-8")
        work, head = _make_repo(
            tmp_path,
            base_files={},
            head_files={},
            caller_files={CALLER_CHECKLIST: "C\n"},
            caller_links={CALLER_SYSTEM: secret},
        )
        result = _run(work, head, DEFAULTS_DIR)
        assert result.returncode != 0
        assert "::error" in result.stdout and "120000" in result.stdout
        context_md = _context(work)
        assert "PLANTED-SECRET" not in context_md
        assert str(secret) not in context_md


class TestSameRepoRunIgnoresTheInput:
    """On a same-repository run the caller IS the target, at the PR's commit.

    Reading defaults there would let the PR author write the review prompt,
    which the base-branch read exists to prevent; the input is honoured only
    when target_repo names another repository.
    """

    def test_same_repo_does_not_read_pr_controlled_defaults(
        self, tmp_path: Path
    ) -> None:
        pr_authored = {
            CALLER_SYSTEM: "PR-AUTHORED SYSTEM\n",
            CALLER_CHECKLIST: "PR-AUTHORED CHECKLIST\n",
        }
        work, head = _make_repo(
            tmp_path, base_files={}, head_files={}, caller_files=pr_authored
        )
        result = _run(work, head, DEFAULTS_DIR, cross=False)
        assert result.returncode != 0
        assert "PR-AUTHORED" not in _context(work)
        assert "no default_prompts_path applies" in result.stdout

    def test_same_repo_with_its_own_prompts_is_unaffected(self, tmp_path: Path) -> None:
        work, head = _make_repo(
            tmp_path,
            base_files={SYSTEM: "BASE SYSTEM\n", CHECKLIST: "BASE CHECKLIST\n"},
            head_files={},
            caller_files=CALLER_FILES,
        )
        result = _run(work, head, DEFAULTS_DIR, cross=False)
        assert result.returncode == 0, result.stdout + result.stderr
        assert "BASE SYSTEM\n\n\n---\n\nBASE CHECKLIST\n" in _context(work)

    def test_cross_repo_still_reads_the_caller_copy(self, tmp_path: Path) -> None:
        work, head = _make_repo(
            tmp_path, base_files={}, head_files={}, caller_files=CALLER_FILES
        )
        result = _run(work, head, DEFAULTS_DIR, cross=True)
        assert result.returncode == 0, result.stdout + result.stderr
        assert "CALLER SYSTEM" in _context(work)


class TestEmptyPromptHint:
    def test_no_fallback_promise_without_a_default(self, tmp_path: Path) -> None:
        work, head = _make_repo(
            tmp_path, base_files={SYSTEM: "", CHECKLIST: "C\n"}, head_files={}
        )
        result = _run(work, head)
        assert "::error title=Empty review prompt" in result.stdout
        assert "falls back" not in result.stdout

    def test_names_the_fallback_when_a_default_is_configured(
        self, tmp_path: Path
    ) -> None:
        work, head = _make_repo(
            tmp_path,
            base_files={SYSTEM: "", CHECKLIST: "C\n"},
            head_files={},
            caller_files=CALLER_FILES,
        )
        result = _run(work, head, DEFAULTS_DIR)
        assert "::error title=Empty review prompt" in result.stdout
        assert f"falls back to the caller default {DEFAULTS_DIR}" in result.stdout

    def test_an_empty_caller_default_is_not_told_to_delete_itself(
        self, tmp_path: Path
    ) -> None:
        # The caller default is the last source: deleting it fails the step
        # with "the caller has no ...", so "or delete it" would be circular.
        work, head = _make_repo(
            tmp_path,
            base_files={},
            head_files={},
            caller_files={CALLER_SYSTEM: "", CALLER_CHECKLIST: "C\n"},
        )
        result = _run(work, head, DEFAULTS_DIR)
        assert "::error title=Empty review prompt" in result.stdout
        assert "delete it" not in result.stdout
        assert "the caller default is the last source" in result.stdout


class TestTargetPromptsKeepPrecedence:
    def test_base_branch_copy_wins_over_the_head(self, tmp_path: Path) -> None:
        work, head = _make_repo(
            tmp_path,
            base_files={SYSTEM: "BASE SYSTEM\n", CHECKLIST: "BASE CHECKLIST\n"},
            head_files={SYSTEM: "HEAD SYSTEM\n", CHECKLIST: "HEAD CHECKLIST\n"},
        )
        result = _run(work, head)
        assert result.returncode == 0, result.stderr
        context_md = _context(work)
        assert "BASE SYSTEM\n\n\n---\n\nBASE CHECKLIST\n" in context_md
        assert "HEAD" not in context_md

    def test_onboarding_head_copy_is_used_with_its_warning(
        self, tmp_path: Path
    ) -> None:
        work, head = _make_repo(
            tmp_path,
            base_files={},
            head_files={SYSTEM: "HEAD SYSTEM\n", CHECKLIST: "HEAD CHECKLIST\n"},
        )
        result = _run(work, head)
        assert result.returncode == 0, result.stderr
        assert "HEAD SYSTEM\n\n\n---\n\nHEAD CHECKLIST\n" in _context(work)
        assert result.stdout.count("Onboarding prompt fallback") == 2


class TestPresentButBrokenFailsClosed:
    @pytest.mark.parametrize("content", ["", "\n  \n"])
    def test_empty_prompt_on_the_base_branch_fails(
        self, tmp_path: Path, content: str
    ) -> None:
        work, head = _make_repo(
            tmp_path,
            base_files={SYSTEM: content, CHECKLIST: "C\n"},
            head_files={},
            caller_files=CALLER_FILES,
        )
        result = _run(work, head, DEFAULTS_DIR)
        assert result.returncode != 0
        assert "::error" in result.stdout and SYSTEM in result.stdout
        assert "CALLER" not in _context(work)

    def test_empty_prompt_on_the_head_fails(self, tmp_path: Path) -> None:
        work, head = _make_repo(
            tmp_path, base_files={}, head_files={SYSTEM: "S\n", CHECKLIST: ""}
        )
        result = _run(work, head)
        assert result.returncode != 0
        assert "::error" in result.stdout and CHECKLIST in result.stdout

    def test_directory_at_the_prompt_path_fails(self, tmp_path: Path) -> None:
        # `git show <ref>:<dir>` exits 0 and prints the tree listing, which
        # the old code wrote into context.md as the system prompt.
        work, head = _make_repo(
            tmp_path,
            base_files={f"{SYSTEM}/nested.md": "x\n", CHECKLIST: "C\n"},
            head_files={},
        )
        result = _run(work, head)
        assert result.returncode != 0
        assert "::error" in result.stdout and SYSTEM in result.stdout


class TestSymlinkedPromptNeverReadsOutsideTheTree:
    """A prompt path must not become a way to read a runner-local file.

    git stores a symlink as a blob of mode 120000 whose content is the link
    target, so a blob-only check admits it on the base branch; and a `cat`
    of the PR head's working tree follows it to whatever it names.
    """

    @staticmethod
    def _planted(tmp_path: Path) -> Path:
        outside = tmp_path / "outside"
        outside.mkdir()
        secret = outside / "secret.txt"
        secret.write_text("PLANTED-SECRET\n", encoding="utf-8")
        return secret

    def test_symlink_on_the_base_branch_fails(self, tmp_path: Path) -> None:
        secret = self._planted(tmp_path)
        work, head = _make_repo(
            tmp_path,
            base_files={CHECKLIST: "C\n"},
            head_files={},
            base_links={SYSTEM: secret},
        )
        result = _run(work, head)
        assert result.returncode != 0
        assert "::error" in result.stdout and SYSTEM in result.stdout
        context_md = _context(work)
        assert str(secret) not in context_md
        assert "PLANTED-SECRET" not in context_md

    def test_symlink_on_the_head_fails(self, tmp_path: Path) -> None:
        secret = self._planted(tmp_path)
        work, head = _make_repo(
            tmp_path,
            base_files={},
            head_files={CHECKLIST: "C\n"},
            head_links={SYSTEM: secret},
        )
        result = _run(work, head)
        assert result.returncode != 0
        assert "::error" in result.stdout and SYSTEM in result.stdout
        assert "PLANTED-SECRET" not in _context(work)

    def test_symlinked_directory_on_the_head_is_not_followed(
        self, tmp_path: Path
    ) -> None:
        # The link sits on a parent directory, so the prompt path itself is
        # not a link and a `[ -L "$path" ]` check alone would not see it.
        outside = tmp_path / "outside-dir"
        outside.mkdir()
        for name in ("code-review-system.md", "code-review-checklist.md"):
            (outside / name).write_text("PLANTED-SECRET\n", encoding="utf-8")
        work, head = _make_repo(
            tmp_path,
            base_files={},
            head_files={},
            head_links={".github/prompts": outside},
        )
        _run(work, head)
        assert "PLANTED-SECRET" not in _context(work)


class TestWiring:
    def test_orchestrator_forwards_the_input_to_prepare(self) -> None:
        orchestrator = yaml.safe_load(
            (WORKFLOW.parent / "base-ai-review-orchestrator.yml").read_text(
                encoding="utf-8"
            )
        )
        call = orchestrator[True]["workflow_call"]["inputs"]["default_prompts_path"]
        assert call["required"] is False and call["default"] == ""
        assert (
            orchestrator["jobs"]["prepare"]["with"]["default_prompts_path"]
            == "${{ inputs.default_prompts_path }}"
        )

    def test_extract_step_receives_the_input_only_on_a_cross_run(self) -> None:
        env = _step("Extract diff and context")["env"]
        assert env["DEFAULT_PROMPTS_PATH"] == "${{ " + DEFAULT_EXPR + " }}"

    def test_same_repo_run_says_the_input_was_ignored(self) -> None:
        step = _step("Ignore default prompts on a same-repository run")
        assert step["if"] == (
            "inputs.default_prompts_path != '' && steps.target.outputs.cross != 'true'"
        )
        assert "::notice" in step["run"]

    def test_caller_checkout_reads_the_caller_at_its_own_commit(self) -> None:
        step = _step("Checkout caller default prompts")
        assert step["if"] == (
            "inputs.default_prompts_path != '' && steps.target.outputs.cross == 'true'"
        )
        assert step["with"]["repository"] == "${{ github.repository }}"
        assert step["with"]["ref"] == "${{ github.sha }}"
        assert step["with"]["path"] == ".ai-dev-pr-review-caller"
        assert step["with"]["sparse-checkout"] == "${{ inputs.default_prompts_path }}"
        assert step["with"]["persist-credentials"] is False
