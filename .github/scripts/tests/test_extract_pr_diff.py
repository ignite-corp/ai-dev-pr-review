"""Tests for extract_pr_diff.sh, the prepare step's diff strategy (AT-2201).

The script is run as the shell it is, inside a real git repository built for
each test, so the diffs are observed rather than asserted about. A bare
"origin" stands in for GitHub, a second repository stands in for the merge
that GitHub performs, and a clone of origin is the runner checkout the script
runs in. `gh` is a stub that serves a fixture diff and records its calls.

Three things are pinned down.

1. The open-PR path is byte-identical to `git diff origin/BASE...HEAD` and
   never calls `gh`.
2. A merged PR is diffed from what landed -- the merge commit against its
   first parent -- with `gh pr diff` as the fallback when the merge commit is
   unknown, unreachable, or (a rebase merge) does not carry the whole PR.
3. An empty pr.diff fails the step and names the strategy that produced it.
"""

from __future__ import annotations

import os
import subprocess
import textwrap
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
import yaml

SCRIPT_DIR = Path(__file__).resolve().parent.parent
SCRIPT = SCRIPT_DIR / "extract_pr_diff.sh"
WORKFLOW = SCRIPT_DIR.parents[0] / "workflows" / "base-ai-review-prepare.yml"

PR_NUMBER = "35"
REPOSITORY = "ignite-corp/ai-dev-pr-review"
# The fixture PR has two commits unless a test builds it with one; this is
# the `commits` count the API reports and the refs step passes on.
PR_COMMITS = "2"
GH_PR_DIFF_FIXTURE = textwrap.dedent(
    """\
    diff --git a/served-by-gh.txt b/served-by-gh.txt
    new file mode 100644
    --- /dev/null
    +++ b/served-by-gh.txt
    @@ -0,0 +1 @@
    +served by gh pr diff
    """
)

_GIT_ENV = {
    **os.environ,
    "GIT_AUTHOR_NAME": "fixture",
    "GIT_AUTHOR_EMAIL": "fixture@example.invalid",
    "GIT_COMMITTER_NAME": "fixture",
    "GIT_COMMITTER_EMAIL": "fixture@example.invalid",
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_NOSYSTEM": "1",
}


def _git(cwd: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=cwd,
        env=_GIT_ENV,
        capture_output=True,
        text=True,
        timeout=60,
        check=True,
    )
    return result.stdout.strip()


def _commit(cwd: Path, message: str, files: dict[str, str]) -> str:
    for name, content in files.items():
        cwd.joinpath(name).write_text(content, encoding="utf-8")
    _git(cwd, "add", "-A")
    _git(cwd, "commit", "-q", "-m", message)
    return _git(cwd, "rev-parse", "HEAD")


@dataclass
class Repos:
    """origin: the bare remote. github: where merges happen. work: the runner."""

    origin: Path
    github: Path
    work: Path
    base_sha: str
    head_sha: str

    def merge_with_merge_commit(self) -> str:
        _git(self.github, "checkout", "-q", "main")
        _git(self.github, "merge", "-q", "--no-ff", "-m", "Merge pull request #35", "feature")
        _git(self.github, "push", "-q", "origin", "main")
        return _git(self.github, "rev-parse", "HEAD")

    def merge_with_squash(self) -> str:
        _git(self.github, "checkout", "-q", "main")
        _git(self.github, "merge", "-q", "--squash", "feature")
        _git(self.github, "commit", "-q", "-m", "squashed (#35)")
        _git(self.github, "push", "-q", "origin", "main")
        return _git(self.github, "rev-parse", "HEAD")

    def merge_with_rebase(self) -> str:
        # GitHub's rebase merge: the PR commits are replayed onto main one by
        # one and merge_commit_sha names the LAST of them.
        _git(self.github, "checkout", "-q", "feature")
        _git(self.github, "rebase", "-q", "main")
        _git(self.github, "checkout", "-q", "main")
        _git(self.github, "merge", "-q", "--ff-only", "feature")
        _git(self.github, "push", "-q", "origin", "main")
        return _git(self.github, "rev-parse", "HEAD")

    def advance_main(self) -> str:
        """An unrelated commit on main after the PR branched, before the merge."""
        _git(self.github, "checkout", "-q", "main")
        sha = _commit(self.github, "unrelated main change", {"other.txt": "other\n"})
        _git(self.github, "push", "-q", "origin", "main")
        return sha

    def sync_work(self) -> None:
        """Bring the runner clone up to date, as a fetch-depth 0 checkout of main is."""
        _git(self.work, "fetch", "-q", "origin")


def _make_repos(tmp_path: Path, *, single_commit: bool = False) -> Repos:
    """A PR of two commits (`new.py`, then a README line), or of one holding both."""
    origin = tmp_path / "origin.git"
    _git(tmp_path, "init", "-q", "--bare", str(origin))
    # GitHub serves any reachable commit by SHA; a local remote does not
    # unless told to, and the script's fetch-by-SHA path depends on it.
    _git(origin, "config", "uploadpack.allowReachableSHA1InWant", "true")

    github = tmp_path / "github"
    _git(tmp_path, "init", "-q", "-b", "main", str(github))
    base_sha = _commit(github, "base", {"README.md": "hello\n"})
    _git(github, "remote", "add", "origin", str(origin))
    _git(github, "push", "-q", "origin", "main")

    _git(github, "checkout", "-q", "-b", "feature")
    new_py = {"new.py": "print('one')\nprint('two')\n"}
    readme = {"README.md": "hello\nnew.py\n"}
    if single_commit:
        head_sha = _commit(github, "feat: add new.py", {**new_py, **readme})
    else:
        _commit(github, "feat: add new.py", new_py)
        head_sha = _commit(github, "feat: mention new.py", readme)
    _git(github, "push", "-q", "origin", "feature")

    work = tmp_path / "work"
    _git(tmp_path, "clone", "-q", str(origin), str(work))
    _git(work, "fetch", "-q", "origin", head_sha)
    _git(work, "checkout", "-q", "--detach", head_sha)
    return Repos(origin=origin, github=github, work=work, base_sha=base_sha, head_sha=head_sha)


@pytest.fixture
def repos(tmp_path: Path) -> Repos:
    return _make_repos(tmp_path)


def _write_gh_stub(tmp_path: Path) -> Path:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    stub = bin_dir / "gh"
    stub.write_text(
        textwrap.dedent(
            """\
            #!/bin/sh
            printf '%s\\n' "$*" >> "$GH_CALL_LOG"
            case "$1 $2" in
              "pr diff") cat "$GH_PR_DIFF_FIXTURE" ;;
              *) echo "unexpected gh invocation: $*" >&2; exit 1 ;;
            esac
            """
        ),
        encoding="utf-8",
    )
    stub.chmod(0o755)
    return bin_dir


@dataclass
class Run:
    returncode: int
    stdout: str
    stderr: str
    diff: str
    gh_calls: list[str]


def _run_script(
    repos: Repos,
    tmp_path: Path,
    *,
    merged: bool,
    merge_commit_sha: str = "",
    pr_commits: str | None = PR_COMMITS,
    gh_diff: str = GH_PR_DIFF_FIXTURE,
) -> Run:
    fixture = tmp_path / "gh-pr-diff.txt"
    fixture.write_text(gh_diff, encoding="utf-8")
    call_log = tmp_path / "gh-calls.log"
    call_log.touch()
    env = {
        **_GIT_ENV,
        "PATH": f"{_write_gh_stub(tmp_path)}{os.pathsep}{os.environ['PATH']}",
        "GH_CALL_LOG": str(call_log),
        "GH_PR_DIFF_FIXTURE": str(fixture),
        "GH_TOKEN": "gh-token",
        "PR_NUMBER": PR_NUMBER,
        "GITHUB_REPOSITORY": REPOSITORY,
        "BASE_REF": "main",
        "HEAD_SHA": repos.head_sha,
        "PR_MERGED": "true" if merged else "false",
        "MERGE_COMMIT_SHA": merge_commit_sha,
    }
    if pr_commits is not None:
        env["PR_COMMITS"] = pr_commits
    result = subprocess.run(
        ["bash", str(SCRIPT)],
        cwd=repos.work,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    diff_path = repos.work / "pr.diff"
    diff = diff_path.read_text(encoding="utf-8") if diff_path.exists() else ""
    calls = [line for line in call_log.read_text(encoding="utf-8").splitlines() if line]
    return Run(result.returncode, result.stdout, result.stderr, diff, calls)


def _has_commit(cwd: Path, sha: str) -> bool:
    result = subprocess.run(
        ["git", "cat-file", "-e", f"{sha}^{{commit}}"],
        cwd=cwd,
        env=_GIT_ENV,
        capture_output=True,
        timeout=60,
    )
    return result.returncode == 0


def _three_dot_diff(repos: Repos) -> str:
    return _git(repos.work, "diff", f"origin/main...{repos.head_sha}")


def _first_parent_diff(repos: Repos, sha: str) -> str:
    return _git(repos.work, "diff", f"{sha}^1", sha)


class TestOpenPr:
    """The path every pull_request event takes must not change at all."""

    def test_diff_is_the_three_dot_diff_verbatim(self, repos: Repos, tmp_path: Path) -> None:
        run = _run_script(repos, tmp_path, merged=False)
        assert run.returncode == 0, run.stderr
        assert run.diff.strip() == _three_dot_diff(repos)
        assert "+++ b/new.py" in run.diff

    def test_no_gh_call_is_made(self, repos: Repos, tmp_path: Path) -> None:
        run = _run_script(repos, tmp_path, merged=False)
        assert run.gh_calls == []

    def test_base_moving_on_does_not_leak_into_the_diff(
        self, repos: Repos, tmp_path: Path
    ) -> None:
        # Three-dot semantics: the merge-base, not the base tip. An unrelated
        # commit on main must not appear as if the PR made it.
        repos.advance_main()
        repos.sync_work()
        run = _run_script(repos, tmp_path, merged=False)
        assert run.returncode == 0, run.stderr
        assert "other.txt" not in run.diff
        assert run.diff.strip() == _three_dot_diff(repos)


class TestMergedPrWithMergeCommit:
    """A merged PR is diffed from its merge commit against the first parent."""

    def test_three_dot_diff_is_empty_once_merged(self, repos: Repos, tmp_path: Path) -> None:
        # The defect itself: after the merge the merge-base is the head, so
        # the diff the old step computed has nothing in it.
        repos.merge_with_merge_commit()
        repos.sync_work()
        assert _three_dot_diff(repos) == ""

    def test_merge_commit_yields_the_pr_diff(self, repos: Repos, tmp_path: Path) -> None:
        repos.advance_main()
        merge_sha = repos.merge_with_merge_commit()
        repos.sync_work()
        run = _run_script(repos, tmp_path, merged=True, merge_commit_sha=merge_sha)
        assert run.returncode == 0, run.stderr
        assert run.diff.strip() == _first_parent_diff(repos, merge_sha)
        assert "+++ b/new.py" in run.diff
        # First parent, not the base at branch time: main's own commit is
        # already in the first parent and must not be reported as PR content.
        assert "other.txt" not in run.diff
        assert run.gh_calls == []

    def test_single_commit_pr_squashed_yields_the_pr_diff(self, tmp_path: Path) -> None:
        # One commit squashed is that commit; nothing can have been left behind.
        repos = _make_repos(tmp_path, single_commit=True)
        squash_sha = repos.merge_with_squash()
        repos.sync_work()
        run = _run_script(
            repos, tmp_path, merged=True, merge_commit_sha=squash_sha, pr_commits="1"
        )
        assert run.returncode == 0, run.stderr
        assert run.diff.strip() == _first_parent_diff(repos, squash_sha)
        assert "+++ b/new.py" in run.diff
        assert run.gh_calls == []

    def test_single_commit_pr_rebased_yields_the_pr_diff(self, tmp_path: Path) -> None:
        repos = _make_repos(tmp_path, single_commit=True)
        repos.advance_main()
        last_sha = repos.merge_with_rebase()
        repos.sync_work()
        run = _run_script(
            repos, tmp_path, merged=True, merge_commit_sha=last_sha, pr_commits="1"
        )
        assert run.returncode == 0, run.stderr
        assert run.diff.strip() == _first_parent_diff(repos, last_sha)
        assert "+++ b/new.py" in run.diff
        assert "other.txt" not in run.diff
        assert run.gh_calls == []

    def test_merge_commit_needs_no_commit_count(self, repos: Repos, tmp_path: Path) -> None:
        # Two parents settle it structurally; PR_COMMITS is not consulted.
        merge_sha = repos.merge_with_merge_commit()
        repos.sync_work()
        run = _run_script(
            repos, tmp_path, merged=True, merge_commit_sha=merge_sha, pr_commits=None
        )
        assert run.returncode == 0, run.stderr
        assert "::warning::" not in run.stdout
        assert run.diff.strip() == _first_parent_diff(repos, merge_sha)
        assert run.gh_calls == []

    def test_merge_commit_absent_locally_is_fetched(self, repos: Repos, tmp_path: Path) -> None:
        # The runner clone was made before the merge; the commit exists only
        # on origin, as it does when the checkout ref predates the merge.
        merge_sha = repos.merge_with_merge_commit()
        assert not _has_commit(repos.work, merge_sha)
        run = _run_script(repos, tmp_path, merged=True, merge_commit_sha=merge_sha)
        assert _has_commit(repos.work, merge_sha)
        assert run.returncode == 0, run.stderr
        assert "+++ b/new.py" in run.diff
        assert run.gh_calls == []

    def test_strategy_is_logged(self, repos: Repos, tmp_path: Path) -> None:
        merge_sha = repos.merge_with_merge_commit()
        repos.sync_work()
        run = _run_script(repos, tmp_path, merged=True, merge_commit_sha=merge_sha)
        assert f"git diff {merge_sha}^1 {merge_sha}" in run.stdout



class TestMergedPrFallsBackToGhPrDiff:
    """Whenever the merge commit cannot stand for the PR, GitHub's diff does."""

    def test_no_merge_commit(self, repos: Repos, tmp_path: Path) -> None:
        repos.merge_with_merge_commit()
        repos.sync_work()
        run = _run_script(repos, tmp_path, merged=True, merge_commit_sha="")
        assert run.returncode == 0, run.stderr
        assert run.diff == GH_PR_DIFF_FIXTURE
        assert run.gh_calls == [f"pr diff {PR_NUMBER} --repo {REPOSITORY}"]
        assert "::warning::" in run.stdout and "no merge commit" in run.stdout

    def test_unreachable_merge_commit(self, repos: Repos, tmp_path: Path) -> None:
        repos.merge_with_merge_commit()
        repos.sync_work()
        run = _run_script(
            repos, tmp_path, merged=True, merge_commit_sha="0" * 40
        )
        assert run.returncode == 0, run.stderr
        assert run.diff == GH_PR_DIFF_FIXTURE
        assert run.gh_calls == [f"pr diff {PR_NUMBER} --repo {REPOSITORY}"]
        assert "unreachable" in run.stdout

    def test_rebase_merge_last_commit_does_not_carry_the_whole_pr(
        self, repos: Repos, tmp_path: Path
    ) -> None:
        # merge_commit_sha names the last replayed commit; its first-parent
        # diff is one of the PR's two commits. The script cannot see that
        # from the commit alone -- which is why a single-parent landing of a
        # multi-commit PR is never trusted.
        repos.advance_main()
        last_sha = repos.merge_with_rebase()
        repos.sync_work()
        assert "+++ b/new.py" not in _first_parent_diff(repos, last_sha)
        run = _run_script(repos, tmp_path, merged=True, merge_commit_sha=last_sha)
        assert run.returncode == 0, run.stderr
        assert run.diff == GH_PR_DIFF_FIXTURE
        assert run.gh_calls == [f"pr diff {PR_NUMBER} --repo {REPOSITORY}"]
        assert "a squash cannot be told from a rebase" in run.stdout

    def test_multi_commit_pr_squashed_is_indistinguishable_from_a_rebase(
        self, repos: Repos, tmp_path: Path
    ) -> None:
        # The squash commit does carry the whole PR here, but a rebase of the
        # same PR would leave an identical-looking single-parent commit at
        # merge_commit_sha, so the canonical diff is used for both.
        squash_sha = repos.merge_with_squash()
        repos.sync_work()
        assert "+++ b/new.py" in _first_parent_diff(repos, squash_sha)
        run = _run_script(repos, tmp_path, merged=True, merge_commit_sha=squash_sha)
        assert run.returncode == 0, run.stderr
        assert run.diff == GH_PR_DIFF_FIXTURE
        assert run.gh_calls == [f"pr diff {PR_NUMBER} --repo {REPOSITORY}"]

    def test_unknown_commit_count_on_a_single_parent_landing(
        self, repos: Repos, tmp_path: Path
    ) -> None:
        squash_sha = repos.merge_with_squash()
        repos.sync_work()
        run = _run_script(
            repos, tmp_path, merged=True, merge_commit_sha=squash_sha, pr_commits=None
        )
        assert run.returncode == 0, run.stderr
        assert run.diff == GH_PR_DIFF_FIXTURE
        assert run.gh_calls == [f"pr diff {PR_NUMBER} --repo {REPOSITORY}"]
        assert "an unknown number of commits" in run.stdout


class TestEmptyDiffFailsLoudly:
    """Nothing to review is a failure with a reason, never a green run."""

    def test_open_pr_with_no_changes(self, repos: Repos, tmp_path: Path) -> None:
        # The head IS the base: nothing to diff. Also the exact shape of the
        # original defect on the old step, when a merged head was diffed
        # three-dot against a base that already contained it.
        repos.merge_with_merge_commit()
        repos.sync_work()
        run = _run_script(repos, tmp_path, merged=False)
        assert run.returncode == 1
        assert "::error" in run.stdout
        assert "pr.diff is empty" in run.stdout
        assert f"open PR: git diff origin/main...{repos.head_sha}" in run.stdout
        assert "merged=false" in run.stdout

    def test_merged_pr_whose_fallback_is_empty(self, repos: Repos, tmp_path: Path) -> None:
        repos.merge_with_merge_commit()
        repos.sync_work()
        run = _run_script(repos, tmp_path, merged=True, merge_commit_sha="", gh_diff="")
        assert run.returncode == 1
        assert "::error" in run.stdout
        assert f"merged PR: gh pr diff {PR_NUMBER}" in run.stdout
        assert "merged=true" in run.stdout

    def test_empty_diff_leaves_no_usable_pr_diff(self, repos: Repos, tmp_path: Path) -> None:
        repos.merge_with_merge_commit()
        repos.sync_work()
        run = _run_script(repos, tmp_path, merged=False)
        assert run.diff == ""


def _steps() -> list[dict[str, Any]]:
    workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    return workflow["jobs"]["prepare"]["steps"]


def _step(name: str) -> dict[str, Any]:
    for step in _steps():
        if step.get("name") == name:
            return step
    raise AssertionError(f"step not found in {WORKFLOW.name}: {name}")


class TestWorkflowWiring:
    """prepare must hand the script every value it reads, from the refs step."""

    def test_extract_step_runs_the_script(self) -> None:
        run = _step("Extract diff and context")["run"]
        assert 'SCRIPT=".ai-dev-pr-review/.github/scripts/extract_pr_diff.sh"' in run
        assert 'bash "$SCRIPT"' in run

    def test_extract_step_falls_back_to_the_legacy_diff_only_without_the_script(
        self,
    ) -> None:
        # self-review runs the PR's YAML against release-pinned scripts, so
        # the legacy diff survives for a pin that predates the script -- and
        # for nothing else: it is the one `git diff` in the step, it sits in
        # the else branch of the existence check, and it is announced.
        run = _step("Extract diff and context")["run"]
        legacy = 'git diff "origin/${BASE_REF}...${HEAD_SHA}" > pr.diff'
        # One write of pr.diff outside the script: the legacy line, which the
        # error text quotes but does not repeat.
        assert run.count("> pr.diff") == 1
        assert run.count(legacy) == 1
        guard = run.index('if [ -f "$SCRIPT" ]')
        assert guard < run.index("else") < run.index(legacy) < run.index("fi\n")
        assert run.index("::warning::extract_pr_diff.sh not in pinned checkout") < run.index(legacy)

    def test_legacy_fallback_still_fails_on_an_empty_diff(self) -> None:
        # The script cannot reconstruct a merged PR from an old pin, but the
        # ticket's other guarantee needs no script: an empty diff is a failed
        # step, not three reviewers approving nothing.
        run = _step("Extract diff and context")["run"]
        legacy = 'git diff "origin/${BASE_REF}...${HEAD_SHA}" > pr.diff'
        empty_check = run.index("if [ ! -s pr.diff ]")
        assert run.index(legacy) < empty_check < run.index("fi\n")
        assert run.index("::error title=Empty diff::") > empty_check
        assert run.index("exit 1", empty_check) < run.index("fi\n", empty_check)

    def test_extract_step_env_covers_the_script_inputs(self) -> None:
        env = _step("Extract diff and context")["env"]
        assert env["PR_MERGED"] == "${{ steps.refs.outputs.pr_merged }}"
        assert env["MERGE_COMMIT_SHA"] == "${{ steps.refs.outputs.merge_commit_sha }}"
        assert env["PR_COMMITS"] == "${{ steps.refs.outputs.pr_commits }}"
        assert env["BASE_REF"] == "${{ steps.refs.outputs.base_ref }}"
        assert env["HEAD_SHA"] == "${{ steps.refs.outputs.head_sha }}"
        assert env["GH_TOKEN"] == "${{ github.token }}"
        assert env["GITHUB_REPOSITORY"] == "${{ github.repository }}"
        assert env["PR_NUMBER"] == "${{ inputs.pr_number || github.event.pull_request.number }}"

    def test_refs_step_exports_the_merge_state(self) -> None:
        run = _step("Resolve PR refs")["run"]
        assert "pr_merged=${PR_MERGED}" in run
        assert "merge_commit_sha=${MERGE_COMMIT_SHA}" in run
        assert "pr_commits=${PR_COMMITS}" in run
