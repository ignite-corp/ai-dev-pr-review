"""Cross-repository review: target threading and posting identity (AT-2520).

The orchestrator takes a `target_repo` input naming the repository whose PR
is reviewed, empty meaning the caller. Three things must hold, and each is
a shape the YAML can lose silently, so each is pinned here by reading the
four `base-ai-review-*.yml` files rather than by running them:

1. Default equivalence. With `target_repo` empty and no target token
   minted, every site this change touched resolves to exactly what it read
   before: `github.repository` for the repository, `github.token` for the
   token. The expressions are rendered with the small `||` evaluator below
   under that state, so the equivalence is checked, not asserted about.

2. Deny-by-default threading. Every job the orchestrator calls passes
   `target_repo`; every site in prepare/single/aggregate that names the
   repository under review or the token that reaches it uses the
   target-first expression. The sites are enumerated from the YAML -- every
   `uses:` job, every `github.repository` mention, every env key or action
   input whose name contains `token` --
   and a new one that does not take the target fails here, unless it is
   listed in the caller-side allowlist with the reason it stays on the
   caller.

3. The public-caller guard. The `Resolve review target` step refuses a
   cross-repository review from a public caller, because the diff and the
   prompts land in the caller's logs and artifacts (measured on run
   38830207533). The step is a shell script, so it is run for real, with a
   stubbed `gh`, under every state that matters: empty target, same target
   in another case, private caller, public caller, visibility unknown from
   the event and resolved (or not) through the API, malformed input,
   missing pr_number, missing App credentials.

The step is duplicated verbatim in all three job files because no helper
checkout exists yet when it runs (it is the first step) and the pinned
scripts checkout would lag a change to it anyway. AT-2538 item 2 names the
hazard of literal duplication; the test that holds the three copies equal
is the answer to it.

Test-module hygiene: all imports belong at the top of this file per PEP 8.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import textwrap
from pathlib import Path
from typing import Any

import pytest
import yaml

WORKFLOWS = Path(__file__).resolve().parents[2] / "workflows"
ORCHESTRATOR = WORKFLOWS / "base-ai-review-orchestrator.yml"
PREPARE = WORKFLOWS / "base-ai-review-prepare.yml"
SINGLE = WORKFLOWS / "base-ai-review-single.yml"
AGGREGATE = WORKFLOWS / "base-ai-review-aggregate.yml"
JOB_FILES = (PREPARE, SINGLE, AGGREGATE)

TARGET_INPUT = "${{ inputs.target_repo }}"
REPO_EXPR = "${{ inputs.target_repo || github.repository }}"
TOKEN_EXPR = "${{ steps.target_token.outputs.token || github.token }}"
REVIEWER_TOKEN_EXPR = (
    "${{ steps.target_token.outputs.token || steps.reviewer_token.outputs.token }}"
)
RESOLVE_STEP = "Resolve review target"
MINT_STEP = "Mint target App token"
TARGET_HEAD_STEP = "Resolve target PR head"
HEAD_SHA_GUARD = "Require head_sha on a cross run"
TARGET_CHECKOUT = "Checkout caller repo (PR head)"
HELPER_CHECKOUT_REPO = "ignite-corp/ai-dev-pr-review"
# The pin every cross-owner mint in this repo uses (consumer-health.yml,
# spike-cross-repo-probe.yml); single.yml's auth-switch mint keeps its own.
APP_TOKEN_PIN = (
    "actions/create-github-app-token@bcd2ba49218906704ab6c1aa796996da409d3eb1"
)
AUTH_SWITCH_PIN = (
    "actions/create-github-app-token@d72941d797fd3113feb6b93fd0dec494b13a2547"
)

# Sites that stay on the CALLER on purpose. Keyed by (file, step name,
# location); a `github.repository` or `GH_TOKEN` anywhere else must use the
# target-first expression. Every entry says why.
CALLER_SIDE: dict[tuple[str, str, str], str] = {
    (
        PREPARE.name,
        RESOLVE_STEP,
        "env:CALLER_REPO",
    ): "the caller, compared against the target",
    (
        SINGLE.name,
        RESOLVE_STEP,
        "env:CALLER_REPO",
    ): "the caller, compared against the target",
    (
        AGGREGATE.name,
        RESOLVE_STEP,
        "env:CALLER_REPO",
    ): "the caller, compared against the target",
    (
        PREPARE.name,
        RESOLVE_STEP,
        "env:TARGET_REPO",
    ): "the raw input, which this step validates before any other site reads it",
    (
        SINGLE.name,
        RESOLVE_STEP,
        "env:TARGET_REPO",
    ): "the raw input, which this step validates before any other site reads it",
    (
        AGGREGATE.name,
        RESOLVE_STEP,
        "env:TARGET_REPO",
    ): "the raw input, which this step validates before any other site reads it",
    (
        PREPARE.name,
        RESOLVE_STEP,
        "env:GH_TOKEN",
    ): "reads the caller's own visibility, before any mint",
    (
        SINGLE.name,
        RESOLVE_STEP,
        "env:GH_TOKEN",
    ): "reads the caller's own visibility, before any mint",
    (
        AGGREGATE.name,
        RESOLVE_STEP,
        "env:GH_TOKEN",
    ): "reads the caller's own visibility, before any mint",
    (PREPARE.name, "Checkout caller default prompts", "with:repository"): (
        "the caller's own default prompts, never the target's (AT-2520)"
    ),
    (PREPARE.name, "Checkout caller default prompts", "with:token"): (
        "the caller's own token reads the caller's own repository (AT-2520)"
    ),
    (SINGLE.name, "Switch Claude auth on usage limit", "env:GH_TOKEN"): (
        "the corp App token that writes an ignite-corp org variable (AT-1606), not the target"
    ),
    (
        AGGREGATE.name,
        "Aggregate and post verdict",
        "env:RUN_URL",
    ): "the run lives in the caller",
    (SINGLE.name, "Run Claude review", "with:github_token"): (
        "claude-code-action's own token: it reads the caller's event with it (its "
        "actor guard) and the review itself reads pr.diff / context.md from disk; "
        "an identity for it on the target is phase 1b with the posting tokens"
    ),
    (SINGLE.name, "Run Claude review", "with:claude_code_oauth_token"): (
        "not a GitHub token: the Anthropic subscription credential, which names no "
        "repository"
    ),
}

# Caller-side token sites whose value is one exact thing, not merely "not
# the target token": each names a credential that is wrong in any other
# spelling.
CALLER_SIDE_VALUE: dict[tuple[str, str, str], str] = {
    (PREPARE.name, "Checkout caller default prompts", "with:repository"): (
        "${{ github.repository }}"
    ),
    (PREPARE.name, "Checkout caller default prompts", "with:token"): "${{ github.token }}",
    (SINGLE.name, "Run Claude review", "with:claude_code_oauth_token"): (
        "${{ secrets.CLAUDE_CODE_OAUTH_TOKEN }}"
    ),
    (SINGLE.name, "Switch Claude auth on usage limit", "env:GH_TOKEN"): (
        "${{ steps.auth_token_corp.outputs.token }}"
    ),
}

_EXPRESSION = re.compile(r"\$\{\{(.+?)\}\}")


def _load(path: Path) -> dict[Any, Any]:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _jobs(path: Path) -> dict[str, dict[str, Any]]:
    return dict(_load(path)["jobs"])


def _only_job(path: Path) -> dict[str, Any]:
    jobs = _jobs(path)
    assert len(jobs) == 1, (
        f"{path.name} is expected to define one job, has {sorted(jobs)}"
    )
    return next(iter(jobs.values()))


def _steps(path: Path) -> list[dict[str, Any]]:
    return [s for s in _only_job(path)["steps"] if isinstance(s, dict)]


def _step(path: Path, name: str) -> dict[str, Any]:
    for step in _steps(path):
        if step.get("name") == name:
            return step
    raise AssertionError(f"step not found in {path.name}: {name}")


def _workflow_call(path: Path) -> dict[str, Any]:
    doc = _load(path)
    trigger = doc.get(True) or doc.get("on")
    return dict(trigger["workflow_call"])


def _evaluate(expression: str, state: dict[str, str]) -> str:
    """Resolve a `${{ a || b || c }}` chain the way the runner does.

    Only the `||` form the touched sites use. Every operand must be in
    ``state``: an operand this test does not know about is a site it has
    not reasoned about, not an empty string.
    """
    match = _EXPRESSION.fullmatch(expression.strip())
    assert match, f"not a single expression: {expression!r}"
    for operand in (part.strip() for part in match.group(1).split("||")):
        assert operand in state, f"operand not modelled: {operand!r} in {expression!r}"
        if state[operand]:
            return state[operand]
    return ""


# ---------------------------------------------------------------------------
# 1. Default equivalence
# ---------------------------------------------------------------------------


class TestDefaultIsTodaysBehaviour:
    """target_repo empty, nothing minted: every site reads what it used to."""

    DEFAULT = {
        "inputs.target_repo": "",
        "github.repository": "ignite-corp/consumer",
        "steps.target_token.outputs.token": "",
        "github.token": "ghs_default",
    }

    def test_repo_expression_is_the_caller(self) -> None:
        assert _evaluate(REPO_EXPR, self.DEFAULT) == "ignite-corp/consumer"

    def test_token_expression_is_the_default_token(self) -> None:
        assert _evaluate(TOKEN_EXPR, self.DEFAULT) == "ghs_default"

    def test_target_set_overrides_both(self) -> None:
        state = dict(
            self.DEFAULT,
            **{
                "inputs.target_repo": "other-org/thing",
                "steps.target_token.outputs.token": "ghs_target",
            },
        )
        assert _evaluate(REPO_EXPR, state) == "other-org/thing"
        assert _evaluate(TOKEN_EXPR, state) == "ghs_target"

    @pytest.mark.parametrize("path", JOB_FILES, ids=lambda p: p.name)
    def test_input_defaults_to_empty(self, path: Path) -> None:
        inputs = _workflow_call(path)["inputs"]
        assert inputs["target_repo"]["required"] is False
        assert inputs["target_repo"]["default"] == ""
        assert inputs["target_repo"]["type"] == "string"

    def test_orchestrator_input_defaults_to_empty(self) -> None:
        inputs = _workflow_call(ORCHESTRATOR)["inputs"]
        assert inputs["target_repo"]["required"] is False
        assert inputs["target_repo"]["default"] == ""

    @pytest.mark.parametrize("path", JOB_FILES, ids=lambda p: p.name)
    def test_mint_is_skipped_unless_cross(self, path: Path) -> None:
        mint = _step(path, MINT_STEP)
        assert mint["if"] == "steps.target.outputs.cross == 'true'"
        assert "continue-on-error" not in mint

    @pytest.mark.parametrize("path", JOB_FILES, ids=lambda p: p.name)
    def test_helper_checkout_is_untouched(self, path: Path) -> None:
        helpers = [
            s
            for s in _steps(path)
            if str(s.get("uses", "")).startswith("actions/checkout")
            and s["with"].get("repository") == HELPER_CHECKOUT_REPO
        ]
        assert len(helpers) == 1, f"{path.name}: expected one helper checkout"
        assert "token" not in helpers[0]["with"]
        assert helpers[0]["with"]["path"] == ".ai-dev-pr-review"
        # Its default token is github.token, and it sits in the same
        # workspace the reviewers walk (round 6).
        assert helpers[0]["with"]["persist-credentials"] is False

    def test_auth_switch_mint_is_untouched(self) -> None:
        mint = _step(SINGLE, "Mint corp App token (auth switch)")
        assert mint["uses"] == AUTH_SWITCH_PIN
        assert mint["with"]["owner"] == "ignite-corp"
        assert "repositories" not in mint["with"]


class TestAggregatePostingIdentity:
    @pytest.mark.parametrize("cross", [False, True], ids=["same-repo", "cross-repo"])
    @pytest.mark.parametrize("optional_token", ["", "ghs_optional"])
    def test_every_posting_path_uses_a_token_for_the_reviewed_repo(
        self, cross: bool, optional_token: str
    ) -> None:
        # The optional token is deliberately populated on cross runs too:
        # a caller-owned approving token must never override the target's.
        state = {
            "steps.target_token.outputs.token": "ghs_target" if cross else "",
            "steps.reviewer_token.outputs.token": optional_token,
            "github.token": "ghs_caller",
        }
        env = _step(AGGREGATE, "Aggregate and post verdict")["env"]
        assert _evaluate(env["GH_TOKEN"], state) == (
            "ghs_target" if cross else "ghs_caller"
        )
        assert _evaluate(env["REVIEWER_TOKEN"], state) == (
            "ghs_target" if cross else optional_token
        )

    def test_optional_caller_mint_does_not_run_on_cross_reviews(self) -> None:
        mint = _step(AGGREGATE, "Mint reviewer App token")
        assert mint["if"] == (
            "${{ steps.target.outputs.cross != 'true' && "
            "vars.REVIEWER_APP_ID != '' && "
            "steps.reviewer_key.outputs.present == 'true' }}"
        )
        assert mint["continue-on-error"] is True

    def test_bot_identity_is_from_the_app_that_minted_the_target_token(self) -> None:
        env = _step(AGGREGATE, "Aggregate and post verdict")["env"]
        assert env["BOT_LOGIN"] == (
            "${{ steps.target_token.outputs.app-slug && "
            "format('{0}[bot]', steps.target_token.outputs.app-slug) || "
            "vars.BOT_LOGIN || 'github-actions[bot]' }}"
        )


# ---------------------------------------------------------------------------
# 2. Deny-by-default threading
# ---------------------------------------------------------------------------


class TestOrchestratorThreading:
    def test_every_called_job_passes_target_repo(self) -> None:
        called = {
            name: job for name, job in _jobs(ORCHESTRATOR).items() if "uses" in job
        }
        assert len(called) == 8, sorted(called)
        for name, job in called.items():
            assert job["with"].get("target_repo") == TARGET_INPUT, (
                f"job {name} does not pass target_repo"
            )

    def test_every_called_job_can_mint_the_target_token(self) -> None:
        # The mint needs the App key in every job that checks the target out,
        # not only the claude jobs the auth switch already forwarded it to.
        for name, job in _jobs(ORCHESTRATOR).items():
            if "uses" not in job:
                continue
            secrets = job.get("secrets") or {}
            assert (
                secrets.get("REVIEWER_APP_PRIVATE_KEY")
                == "${{ secrets.REVIEWER_APP_PRIVATE_KEY }}"
            ), name

    def test_concurrency_group_names_the_target_and_the_pr(self) -> None:
        group = str(_load(ORCHESTRATOR)["concurrency"]["group"])
        assert group == (
            "ai-review-${{ inputs.target_repo || github.repository }}"
            "-${{ github.event.pull_request.number || inputs.pr_number || github.run_id }}"
        )
        assert _load(ORCHESTRATOR)["concurrency"]["cancel-in-progress"] is True

    def test_two_targets_same_pr_number_are_different_groups(self) -> None:
        group = str(_load(ORCHESTRATOR)["concurrency"]["group"])
        base = {
            "github.repository": "ignite-corp/runner",
            "github.event.pull_request.number": "",
            "inputs.pr_number": "12",
            "github.run_id": "1",
        }

        def render(target: str) -> str:
            state = dict(base, **{"inputs.target_repo": target})
            return _EXPRESSION.sub(lambda m: _evaluate(m.group(0), state), group)

        assert render("org-a/x") != render("org-b/y")
        assert render("") == "ai-review-ignite-corp/runner-12"


def _names_a_repository(text: str) -> bool:
    """Either spelling of the repository under review.

    `inputs.target_repo` on its own is the shape the round-4 raw-interpolation
    fix exists to keep out of `run:` text, and the one a `github.repository`-
    only match cannot see (round 6).
    """
    return "github.repository" in text or "inputs.target_repo" in text


def _repository_sites(path: Path) -> list[tuple[str, str, str]]:
    """Every (step, location, value) that names the repository under review."""
    sites: list[tuple[str, str, str]] = []
    for step in _steps(path):
        name = str(step.get("name", ""))
        for key, value in (step.get("env") or {}).items():
            if _names_a_repository(str(value)):
                sites.append((name, f"env:{key}", str(value)))
        for key, value in (step.get("with") or {}).items():
            if _names_a_repository(str(value)):
                sites.append((name, f"with:{key}", str(value)))
        run = str(step.get("run", ""))
        for match in _EXPRESSION.finditer(run):
            if _names_a_repository(match.group(1)):
                sites.append((name, "run", match.group(0)))
    return sites


def _token_sites(path: Path) -> list[tuple[str, str, str]]:
    """Every (step, location, value) that hands a token to something.

    Two shapes, both matched by key name, case-insensitively, on `token`:
    any `env:` key -- `GH_TOKEN`, but also `REVIEWER_TOKEN` or a future
    `TARGET_TOKEN`, which a literal `GH_TOKEN` match let through (round 4)
    -- and any `with:` input of any action -- a checkout's `token`, but also
    a composite's `github_token`, the shape single.yml's Claude review step
    uses. A key the enumeration does not see is a site the deny-by-default
    tests cannot judge, so the match is by name, not by a list of names.
    """
    sites: list[tuple[str, str, str]] = []
    for step in _steps(path):
        name = str(step.get("name", ""))
        for key, value in (step.get("env") or {}).items():
            if "token" in str(key).lower():
                sites.append((name, f"env:{key}", str(value)))
        for key, value in (step.get("with") or {}).items():
            if "token" in str(key).lower():
                sites.append((name, f"with:{key}", str(value)))
    return sites


class TestJobThreading:
    @pytest.mark.parametrize("path", JOB_FILES, ids=lambda p: p.name)
    def test_every_repository_site_follows_the_target(self, path: Path) -> None:
        sites = _repository_sites(path)
        assert sites, (
            f"{path.name}: no repository site found; the enumeration is broken"
        )
        for step, location, value in sites:
            if (path.name, step, location) in CALLER_SIDE:
                # A caller-side site must not quietly acquire the target
                # expression either: that is the drift the allowlist pins.
                assert REPO_EXPR not in value, (
                    f"{path.name}:{step!r} {location} is allowlisted as caller-side "
                    f"but reads the target: {value!r}"
                )
                continue
            assert value == REPO_EXPR, (
                f"{path.name}:{step!r} {location} reads {value!r}, not the target"
            )

    @pytest.mark.parametrize("path", JOB_FILES, ids=lambda p: p.name)
    def test_no_repository_expression_is_interpolated_into_run_text(
        self, path: Path
    ) -> None:
        # `${{ inputs.target_repo }}` is caller-supplied text; interpolated
        # into a `run:` script it is shell source, and the regex guard in
        # `Resolve review target` is an invariant held by a different step.
        # The repository reaches a script through `env:` only.
        run_sites = [
            (step, value)
            for step, location, value in _repository_sites(path)
            if location == "run"
        ]
        assert run_sites == [], (
            f"{path.name}: repository expression interpolated into run text: {run_sites}"
        )

    @pytest.mark.parametrize("path", JOB_FILES, ids=lambda p: p.name)
    def test_every_token_site_follows_the_target(self, path: Path) -> None:
        sites = _token_sites(path)
        assert sites, f"{path.name}: no token site found; the enumeration is broken"
        for step, location, value in sites:
            if (path.name, step, location) in CALLER_SIDE:
                # Closed in both directions, like the repository half: an
                # allowlisted site that drifts ONTO the target token is the
                # drift the list exists to pin -- the auth-switch token
                # writes an ignite-corp org variable and must stay the corp
                # token; the Anthropic credential is not a GitHub token at
                # all (round 5).
                assert TOKEN_EXPR not in value, (
                    f"{path.name}:{step!r} {location} is allowlisted as caller-side "
                    f"but reads the target token: {value!r}"
                )
                expected = CALLER_SIDE_VALUE.get((path.name, step, location))
                if expected is not None:
                    assert value == expected, (
                        f"{path.name}:{step!r} {location} reads {value!r}, not {expected!r}"
                    )
                continue
            # Formal approvals retain the optional same-repo App token as
            # their fallback; cross runs must still prefer the target token.
            expected = (
                REVIEWER_TOKEN_EXPR
                if (path.name, step, location)
                == (AGGREGATE.name, "Aggregate and post verdict", "env:REVIEWER_TOKEN")
                else TOKEN_EXPR
            )
            assert value == expected, (
                f"{path.name}:{step!r} {location} reads {value!r}, not the target token"
            )

    def test_caller_side_allowlist_names_only_real_sites(self) -> None:
        # An allowlist entry for a site that no longer exists is a hole
        # waiting for a step to be renamed into it.
        # Built from the two enumerations only: an entry whose site stopped
        # naming a repository or a token (a literal, say) is dormant, and a
        # dormant entry would silently exempt the step the day the
        # expression came back (round 6).
        live = set()
        for path in JOB_FILES:
            for step, location, _ in _repository_sites(path) + _token_sites(path):
                live.add((path.name, step, location))
        stale = set(CALLER_SIDE) - live
        assert not stale, f"CALLER_SIDE names sites that do not exist: {sorted(stale)}"

    @pytest.mark.parametrize("path", JOB_FILES, ids=lambda p: p.name)
    def test_target_checkout_takes_the_target_and_its_token(self, path: Path) -> None:
        checkout = _step(path, TARGET_CHECKOUT)
        assert checkout["with"]["repository"] == REPO_EXPR
        assert checkout["with"]["token"] == TOKEN_EXPR

    def test_every_extraheader_built_from_a_token_is_masked_before_use(self) -> None:
        # The steps that fetch after the non-persisting checkout build git's
        # Authorization header themselves. Actions masks the raw token but
        # not its base64 form; actions/checkout add-masks the header value it
        # builds, and so must these -- this repository is public, and any
        # later echo, `set -x`, git trace or error printing GIT_CONFIG_VALUE_0
        # would publish a usable credential (round 5, follow-up).
        builders: list[tuple[str, str]] = []
        for path in JOB_FILES:
            for step in _steps(path):
                run = str(step.get("run", ""))
                if "extraheader" in run:
                    builders.append((f"{path.name}:{step.get('name')!r}", run))
        assert len(builders) == 2, [label for label, _ in builders]
        preludes = []
        for label, run in builders:
            # Anchored on the statements themselves, never on a substring a
            # comment can also carry: `"base64" in line` matched the comment
            # above the assignment, so an assignment moved BELOW the add-mask
            # (masking an empty value, leaking the real one) still satisfied
            # the order (round 7).
            code = [
                (i, line.strip())
                for i, line in enumerate(run.splitlines())
                if line.strip() and not line.strip().startswith("#")
            ]

            def only(prefix: str) -> int:
                hits = [i for i, text in code if text.startswith(prefix)]
                assert len(hits) == 1, (
                    f"{label}: expected one line starting {prefix!r}, got {hits}"
                )
                return hits[0]

            encode = only("B64=$(")
            mask = only('echo "::add-mask::$B64"')
            header = only("GIT_CONFIG_VALUE_0=")
            export = only("export GIT_CONFIG_COUNT GIT_CONFIG_KEY_0 GIT_CONFIG_VALUE_0")
            assert encode < mask < header < export, (
                f"{label}: expected encode < mask < header < export, got "
                f"{encode}, {mask}, {header}, {export}"
            )
            lines = run.splitlines()
            # The assignment is the only encoding, and the header is built
            # from the masked variable, not re-encoded.
            assert "base64" in lines[encode]
            assert "${B64}" in lines[header] and "base64" not in lines[header]
            # Nothing reads $B64 between the assignment and the mask.
            assert not any("B64" in text for i, text in code if encode < i < mask), (
                label
            )
            assert "set -x" not in run and "set -o xtrace" not in run, label
            # Statements only, so a comment edit cannot make the two differ or
            # agree spuriously.
            preludes.append([text for i, text in code if encode <= i <= export])
        assert preludes[0] == preludes[1], "the two credential preludes differ"

    @pytest.mark.parametrize("path", JOB_FILES, ids=lambda p: p.name)
    def test_target_token_checkout_does_not_persist_the_credential(
        self, path: Path
    ) -> None:
        # actions/checkout writes the token it was given into the workspace's
        # .git/config unless told not to. On a cross run that token has
        # write access on another organization's repository, and the
        # workspace is what codex (--sandbox workspace-write) and claude walk
        # while reading attacker-authored diff text (round 5, major). Every
        # checkout that takes the target token refuses to persist it; the
        # steps that still fetch (prepare) get the credential through their
        # own env instead. EVERY checkout, not only the target-token ones:
        # the helper clone's default token is the caller's github.token, in
        # the same tree one directory over (round 6).
        checkouts = [
            s
            for s in _steps(path)
            if str(s.get("uses", "")).startswith("actions/checkout")
        ]
        assert checkouts, f"{path.name}: no checkout found; the enumeration is broken"
        for step in checkouts:
            with_ = step.get("with") or {}
            assert with_.get("persist-credentials") is False, (
                f"{path.name}:{step.get('name')!r} persists its credential into .git/config"
            )

    def test_prepare_checkout_ref_is_the_target_head_when_cross(self) -> None:
        # prepare has no `inputs.head_sha`: it is the job that resolves one.
        # Its checkout ref must therefore come from the target when the
        # target is another repository -- `github.event.pull_request.head.sha`
        # and `github.ref` both belong to the CALLER, and a caller's SHA does
        # not exist in the target (round 1, codex + claude major). The only
        # other operand is the resolve step's `fallback_ref`, which carries
        # the caller's event ref when the target IS the caller and nothing
        # on a cross run (round 2).
        ref = str(_step(PREPARE, TARGET_CHECKOUT)["with"]["ref"])
        assert (
            ref
            == "${{ steps.target_head.outputs.sha || steps.target.outputs.fallback_ref }}"
        )
        cross = {
            "steps.target_head.outputs.sha": "feedfacefeedfacefeedfacefeedfacefeedface",
            "steps.target.outputs.fallback_ref": "",
        }
        assert _evaluate(ref, cross) == "feedfacefeedfacefeedfacefeedfacefeedface"
        same = {
            "steps.target_head.outputs.sha": "",
            "steps.target.outputs.fallback_ref": "caller-pr-head",
        }
        assert _evaluate(ref, same) == "caller-pr-head"

    @pytest.mark.parametrize("path", (SINGLE, AGGREGATE), ids=lambda p: p.name)
    def test_reviewer_and_aggregate_checkout_ref_has_no_caller_only_tail(
        self, path: Path
    ) -> None:
        # `inputs.head_sha` is empty when prepare skipped on size or failed,
        # and aggregate runs anyway (always()). The tail must then be the
        # resolve step's `fallback_ref`, which is empty on a cross run, so a
        # caller ref is never handed to a target checkout (round 2); an
        # empty ref is the target's default branch, and the tree is unused
        # on those two paths ('Confirm the tree matches the diff' exits 0
        # on an empty HEAD_SHA).
        ref = str(_step(path, TARGET_CHECKOUT)["with"]["ref"])
        assert ref == "${{ inputs.head_sha || steps.target.outputs.fallback_ref }}"
        assert "github.ref" not in ref and "github.event" not in ref

    def test_single_refuses_a_cross_run_without_head_sha(self) -> None:
        # single.yml resolves no head of its own: the orchestrator passes
        # prepare's. A direct call with a cross target_repo and no head_sha
        # would otherwise check out the target's default branch -- an empty
        # fallback_ref -- and review the wrong tree (round 3, codex major).
        names = [str(s.get("name", "")) for s in _steps(SINGLE)]
        assert (
            names.index(RESOLVE_STEP)
            < names.index(HEAD_SHA_GUARD)
            < names.index(MINT_STEP)
        )
        step = _step(SINGLE, HEAD_SHA_GUARD)
        assert (
            step["if"]
            == "steps.target.outputs.cross == 'true' && inputs.head_sha == ''"
        )
        assert "env" not in step
        result = subprocess.run(
            ["bash", "-euo", "pipefail", "-c", str(step["run"])],
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert result.returncode == 1
        assert "::error::" in result.stdout and "head_sha" in result.stdout

    @pytest.mark.parametrize("path", (PREPARE, AGGREGATE), ids=lambda p: p.name)
    def test_only_single_carries_the_head_sha_guard(self, path: Path) -> None:
        # prepare resolves the target head itself; aggregate reads the tree
        # only when head_sha is set ('Confirm the tree matches the diff'
        # exits 0 without one) and must still run to post the failure
        # verdict, so neither refuses an empty head_sha.
        assert HEAD_SHA_GUARD not in [str(s.get("name", "")) for s in _steps(path)]

    def test_prepare_resolves_the_target_head_between_mint_and_checkout(self) -> None:
        names = [str(s.get("name", "")) for s in _steps(PREPARE)]
        assert (
            names.index(MINT_STEP)
            < names.index(TARGET_HEAD_STEP)
            < names.index(TARGET_CHECKOUT)
        )
        step = _step(PREPARE, TARGET_HEAD_STEP)
        assert step["id"] == "target_head"
        assert step["if"] == "steps.target.outputs.cross == 'true'"
        assert step["env"]["GH_TOKEN"] == TOKEN_EXPR
        assert step["env"]["GITHUB_REPOSITORY"] == REPO_EXPR

    def test_size_check_reads_the_repository_from_env(self) -> None:
        step = _step(PREPARE, "Check PR size")
        assert step["env"]["GITHUB_REPOSITORY"] == REPO_EXPR
        assert step["run"].count('--repo "$GITHUB_REPOSITORY"') == 2

    @pytest.mark.parametrize("path", JOB_FILES, ids=lambda p: p.name)
    def test_resolve_then_mint_precede_the_target_checkout(self, path: Path) -> None:
        names = [str(s.get("name", "")) for s in _steps(path)]
        assert (
            names.index(RESOLVE_STEP)
            < names.index(MINT_STEP)
            < names.index(TARGET_CHECKOUT)
        )

    @pytest.mark.parametrize("path", JOB_FILES, ids=lambda p: p.name)
    def test_mint_is_scoped_to_the_target_and_narrowed(self, path: Path) -> None:
        mint = _step(path, MINT_STEP)
        assert mint["id"] == "target_token"
        assert mint["uses"] == APP_TOKEN_PIN
        assert mint["with"] == {
            "app-id": "${{ vars.REVIEWER_APP_ID }}",
            "private-key": "${{ secrets.REVIEWER_APP_PRIVATE_KEY }}",
            "owner": "${{ steps.target.outputs.owner }}",
            "repositories": "${{ steps.target.outputs.name }}",
            "permission-contents": "read",
            "permission-pull-requests": "write",
        }

    def test_the_three_resolve_steps_are_identical(self) -> None:
        # One script, three copies; a fix in one is a fix in all or it is
        # not a fix (AT-2538, item 2).
        steps = [_step(path, RESOLVE_STEP) for path in JOB_FILES]
        assert all(step == steps[0] for step in steps[1:])
        assert steps[0]["id"] == "target"

    def test_the_three_mint_steps_are_identical(self) -> None:
        steps = [_step(path, MINT_STEP) for path in JOB_FILES]
        assert all(step == steps[0] for step in steps[1:])

    def test_prepare_declares_the_secret_the_mint_reads(self) -> None:
        secrets = _workflow_call(PREPARE)["secrets"]
        assert secrets["REVIEWER_APP_PRIVATE_KEY"]["required"] is False

    def test_refs_step_forces_the_api_path_when_cross(self) -> None:
        refs = _step(PREPARE, "Resolve PR refs")
        assert refs["env"]["CROSS_REPO"] == "${{ steps.target.outputs.cross }}"
        assert (
            'if [ -z "$BASE_REF" ] || [ "$CROSS_REPO" = "true" ]; then' in refs["run"]
        )


# ---------------------------------------------------------------------------
# 3. The public-caller guard, run for real
# ---------------------------------------------------------------------------

requires_bash = pytest.mark.skipif(
    shutil.which("bash") is None, reason="bash not installed"
)

CALLER = "ignite-corp/private-runner"
TARGET = "ignite-pilot-org/max-builder"


def _write_gh_stub(
    tmp_path: Path, visibility_response: str, exit_code: int = 0
) -> Path:
    """A `gh` that answers `gh api repos/<caller> --jq .visibility` and logs."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    stub = bin_dir / "gh"
    stub.write_text(
        textwrap.dedent(
            f"""\
            #!/bin/sh
            printf '%s\\n' "$*" >> "$GH_CALL_LOG"
            printf '%s\\n' {visibility_response!r}
            exit {exit_code}
            """
        ),
        encoding="utf-8",
    )
    stub.chmod(0o755)
    return bin_dir


def _run_resolve(
    tmp_path: Path,
    *,
    target: str,
    caller: str = CALLER,
    caller_visibility: str,
    pr_number: str = "160",
    app_id: str = "4196135",
    app_key: str = "-----BEGIN RSA PRIVATE KEY-----",
    gh_visibility: str = "private",
    gh_exit: int = 0,
    event_head_sha: str = "",
    event_ref: str = "refs/heads/caller-branch",
) -> tuple[int, dict[str, str], list[str], str]:
    """Run the real step script; return (exit, outputs, gh calls, stdout)."""
    step = _step(PREPARE, RESOLVE_STEP)
    context = {
        "inputs.target_repo": target,
        "github.repository": caller,
        "github.event.repository.visibility": caller_visibility,
        "github.event.pull_request.head.sha": event_head_sha,
        "github.ref": event_ref,
        "inputs.pr_number": pr_number,
        "github.token": "ghs_caller",
        "vars.REVIEWER_APP_ID": app_id,
        "secrets.REVIEWER_APP_PRIVATE_KEY != ''": "true" if app_key else "false",
    }

    def render(text: str) -> str:
        def replace(match: re.Match[str]) -> str:
            expression = match.group(1).strip()
            assert expression in context, f"unmapped workflow expression: {expression}"
            return context[expression]

        return _EXPRESSION.sub(replace, text)

    github_output = tmp_path / "github_output"
    github_output.touch()
    call_log = tmp_path / "gh-calls.log"
    call_log.touch()
    env = {
        "PATH": f"{_write_gh_stub(tmp_path, gh_visibility, gh_exit)}{os.pathsep}{os.environ['PATH']}",
        "GITHUB_OUTPUT": str(github_output),
        "GH_CALL_LOG": str(call_log),
    }
    for key, value in step["env"].items():
        env[key] = render(str(value))
    result = subprocess.run(
        ["bash", "-euo", "pipefail", "-c", render(step["run"])],
        env=env,
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=60,
    )
    outputs: dict[str, str] = {}
    for line in github_output.read_text(encoding="utf-8").splitlines():
        if line:
            key, _, value = line.partition("=")
            outputs[key] = value
    calls = [line for line in call_log.read_text(encoding="utf-8").splitlines() if line]
    return result.returncode, outputs, calls, result.stdout + result.stderr


@requires_bash
class TestResolveStep:
    def test_empty_target_is_the_caller_and_touches_nothing(
        self, tmp_path: Path
    ) -> None:
        # Today's consumers: no API call, no refusal path, outputs that the
        # target-first expressions never read because the input is empty.
        code, outputs, calls, _ = _run_resolve(
            tmp_path, target="", caller_visibility="public", app_id="", app_key=""
        )
        assert code == 0
        assert outputs == {
            "owner": "ignite-corp",
            "name": "private-runner",
            "cross": "false",
            "fallback_ref": "refs/heads/caller-branch",
        }
        assert calls == []

    def test_fallback_ref_is_the_event_head_then_the_ref_when_not_cross(
        self, tmp_path: Path
    ) -> None:
        # The same `head.sha || github.ref` chain the checkouts carried
        # before target_repo existed, now computed here so that a cross run
        # can withhold it.
        (tmp_path / "a").mkdir()
        (tmp_path / "b").mkdir()
        _, with_sha, _, _ = _run_resolve(
            tmp_path / "a",
            target="",
            caller_visibility="",
            app_id="",
            app_key="",
            event_head_sha="caller-pr-head",
        )
        assert with_sha["fallback_ref"] == "caller-pr-head"
        _, without_sha, _, _ = _run_resolve(
            tmp_path / "b", target="", caller_visibility="", app_id="", app_key=""
        )
        assert without_sha["fallback_ref"] == "refs/heads/caller-branch"

    def test_fallback_ref_is_withheld_on_a_cross_run(self, tmp_path: Path) -> None:
        # A pull_request event in the caller carries a head SHA; it names the
        # caller's commit and must never reach a target checkout (round 2).
        code, outputs, _, _ = _run_resolve(
            tmp_path,
            target=TARGET,
            caller_visibility="private",
            event_head_sha="caller-pr-head",
        )
        assert code == 0
        assert outputs["fallback_ref"] == ""

    def test_default_path_never_validates_the_caller_name(self, tmp_path: Path) -> None:
        # The owner/name regex is a guard on caller-supplied INPUT. The
        # caller's own name is GitHub's, not ours to re-validate: a login the
        # hand-written owner pattern does not model (an EMU login with `_`,
        # say) must not break every default run of that consumer (round 8).
        code, outputs, calls, log = _run_resolve(
            tmp_path,
            target="",
            caller="own_er/name",
            caller_visibility="public",
            app_id="",
            app_key="",
        )
        assert code == 0, log
        assert outputs["cross"] == "false"
        assert outputs["owner"] == "own_er" and outputs["name"] == "name"
        assert calls == []

    def test_cross_path_still_refuses_a_malformed_owner(self, tmp_path: Path) -> None:
        # The same spelling as INPUT is still refused; only the default is
        # exempt from the regex.
        code, outputs, _, log = _run_resolve(
            tmp_path, target="own_er/name", caller_visibility="private"
        )
        assert code == 1
        assert "must be owner/name" in log
        assert outputs == {}

    def test_target_that_is_the_caller_in_another_case_is_not_cross(
        self, tmp_path: Path
    ) -> None:
        # GitHub repository names are case-insensitive (AT-2538, item 1).
        code, outputs, calls, _ = _run_resolve(
            tmp_path,
            target=CALLER.upper(),
            caller_visibility="public",
            app_id="",
            app_key="",
        )
        assert code == 0
        assert outputs["cross"] == "false"
        assert calls == []

    def test_private_caller_resolves_the_target(self, tmp_path: Path) -> None:
        code, outputs, calls, _ = _run_resolve(
            tmp_path, target=TARGET, caller_visibility="private"
        )
        assert code == 0
        assert outputs == {
            "owner": "ignite-pilot-org",
            "name": "max-builder",
            "cross": "true",
            "fallback_ref": "",
        }
        # The event payload answered; the API was not asked.
        assert calls == []

    def test_public_caller_is_refused(self, tmp_path: Path) -> None:
        code, outputs, _, log = _run_resolve(
            tmp_path, target=TARGET, caller_visibility="public"
        )
        assert code == 1
        assert "::error::" in log and "not private" in log and "AT-2520" in log
        assert outputs == {}

    def test_internal_caller_is_refused(self, tmp_path: Path) -> None:
        # GitHub reports `private: true` for an internal repository too; the
        # guard decides on `visibility`, and internal is every member of the
        # enterprise -- a wider audience than the target's collaborators
        # (round 3).
        code, outputs, _, log = _run_resolve(
            tmp_path, target=TARGET, caller_visibility="internal"
        )
        assert code == 1
        assert "visibility is internal, not private" in log
        assert outputs == {}

    def test_api_says_internal_is_refused(self, tmp_path: Path) -> None:
        code, _, calls, log = _run_resolve(
            tmp_path, target=TARGET, caller_visibility="", gh_visibility="internal"
        )
        assert code == 1
        assert "visibility is internal, not private" in log
        assert calls == [f"api repos/{CALLER} --jq .visibility"]

    def test_unknown_visibility_is_resolved_through_the_api(
        self, tmp_path: Path
    ) -> None:
        code, outputs, calls, _ = _run_resolve(
            tmp_path, target=TARGET, caller_visibility="", gh_visibility="private"
        )
        assert code == 0
        assert outputs["cross"] == "true"
        assert calls == [f"api repos/{CALLER} --jq .visibility"]

    def test_api_says_public_is_refused(self, tmp_path: Path) -> None:
        code, _, calls, log = _run_resolve(
            tmp_path, target=TARGET, caller_visibility="", gh_visibility="public"
        )
        assert code == 1
        assert "visibility is public, not private" in log
        assert calls == [f"api repos/{CALLER} --jq .visibility"]

    def test_api_failure_is_refused_not_assumed_private(self, tmp_path: Path) -> None:
        # A zero needs a positive control: an unanswerable question is not a
        # "yes, private".
        code, _, _, log = _run_resolve(
            tmp_path, target=TARGET, caller_visibility="", gh_visibility="", gh_exit=1
        )
        assert code == 1
        assert "visibility is unknown, not private" in log

    @pytest.mark.parametrize(
        "bad",
        [
            "max-builder",
            "a/b/c",
            "ignite pilot/x",
            "org/name\n::error::x",
            # Dot segments: syntactically `owner/name`, but a path traversal
            # once concatenated into `repos/<target>/pulls/<n>` (round 2).
            "../..",
            "owner/..",
            "owner/.",
            "owner/...",
            "./name",
            # An owner is alphanumerics and inner hyphens only; `.`/`_` and a
            # leading or trailing hyphen are not an owner on GitHub.
            "own.er/name",
            "own_er/name",
            "-owner/name",
            "owner-/name",
        ],
    )
    def test_malformed_target_is_refused(self, tmp_path: Path, bad: str) -> None:
        code, outputs, calls, log = _run_resolve(
            tmp_path, target=bad, caller_visibility="private"
        )
        assert code == 1
        assert "must be owner/name" in log
        assert outputs == {}
        assert calls == []

    def test_malformed_target_is_not_echoed_as_a_second_log_line(
        self, tmp_path: Path
    ) -> None:
        _, _, _, log = _run_resolve(
            tmp_path, target="org/name\n::error::injected", caller_visibility="private"
        )
        assert "::error::injected" not in log
        assert log.count("::error::") == 1

    def test_cross_without_pr_number_is_refused(self, tmp_path: Path) -> None:
        code, _, calls, log = _run_resolve(
            tmp_path, target=TARGET, caller_visibility="private", pr_number=""
        )
        assert code == 1
        assert "pr_number is empty" in log
        assert calls == []

    @pytest.mark.parametrize("app_id,app_key", [("", "key"), ("4196135", "")])
    def test_cross_without_app_credentials_is_refused(
        self, tmp_path: Path, app_id: str, app_key: str
    ) -> None:
        code, outputs, _, log = _run_resolve(
            tmp_path,
            target=TARGET,
            caller_visibility="private",
            app_id=app_id,
            app_key=app_key,
        )
        assert code == 1
        assert "REVIEWER_APP_ID / REVIEWER_APP_PRIVATE_KEY" in log
        assert outputs == {}

    def test_refusals_precede_the_credential_check(self, tmp_path: Path) -> None:
        # Visibility is judged before credentials: a public caller with no
        # App key is told about the leak, not about the key.
        _, _, _, log = _run_resolve(
            tmp_path, target=TARGET, caller_visibility="public", app_id="", app_key=""
        )
        assert "not private" in log
        assert "REVIEWER_APP_ID" not in log


def _run_target_head(
    tmp_path: Path, *, pr_number: str, api_response: str, gh_exit: int = 0
) -> tuple[int, dict[str, str], list[str], str]:
    """Run the real `Resolve target PR head` script against a stubbed `gh`."""
    step = _step(PREPARE, TARGET_HEAD_STEP)
    context = {
        "steps.target_token.outputs.token || github.token": "ghs_target",
        "inputs.target_repo || github.repository": TARGET,
        "inputs.pr_number": pr_number,
    }

    def render(text: str) -> str:
        def replace(match: re.Match[str]) -> str:
            expression = match.group(1).strip()
            assert expression in context, f"unmapped workflow expression: {expression}"
            return context[expression]

        return _EXPRESSION.sub(replace, text)

    github_output = tmp_path / "github_output"
    github_output.touch()
    call_log = tmp_path / "gh-calls.log"
    call_log.touch()
    env = {
        "PATH": f"{_write_gh_stub(tmp_path, api_response, gh_exit)}{os.pathsep}{os.environ['PATH']}",
        "GITHUB_OUTPUT": str(github_output),
        "GH_CALL_LOG": str(call_log),
    }
    for key, value in _only_job(PREPARE).get("env", {}).items():
        env[key] = render(str(value))
    for key, value in step["env"].items():
        env[key] = render(str(value))
    result = subprocess.run(
        ["bash", "-euo", "pipefail", "-c", render(step["run"])],
        env=env,
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=60,
    )
    outputs: dict[str, str] = {}
    for line in github_output.read_text(encoding="utf-8").splitlines():
        if line:
            key, _, value = line.partition("=")
            outputs[key] = value
    calls = [line for line in call_log.read_text(encoding="utf-8").splitlines() if line]
    return result.returncode, outputs, calls, result.stdout + result.stderr


@requires_bash
class TestResolveTargetHeadStep:
    HEAD = "feedfacefeedfacefeedfacefeedfacefeedface"

    def test_reads_the_target_pr_head_with_the_target_coordinate(
        self, tmp_path: Path
    ) -> None:
        code, outputs, calls, _ = _run_target_head(
            tmp_path, pr_number="160", api_response=self.HEAD
        )
        assert code == 0
        assert outputs == {"sha": self.HEAD}
        assert calls == [f'api repos/{TARGET}/pulls/160 --jq .head.sha // ""']

    @pytest.mark.parametrize("bad", ["", "abc", "1/../2", "160\n::error::x"])
    def test_a_pr_number_that_is_not_digits_never_reaches_the_url(
        self, tmp_path: Path, bad: str
    ) -> None:
        code, outputs, calls, log = _run_target_head(
            tmp_path, pr_number=bad, api_response=self.HEAD
        )
        assert code == 1
        assert "Invalid PR_NUMBER" in log
        assert calls == []
        assert outputs == {}

    @pytest.mark.parametrize("response", ["", "null", "not-a-sha", "FEEDFACE"])
    def test_an_unresolvable_head_refuses_rather_than_checking_out_a_guess(
        self, tmp_path: Path, response: str
    ) -> None:
        code, outputs, _, log = _run_target_head(
            tmp_path, pr_number="160", api_response=response
        )
        assert code == 1
        assert "could not resolve the head" in log
        assert outputs == {}

    def test_an_api_failure_refuses(self, tmp_path: Path) -> None:
        # `gh` exiting non-zero (404: App not installed on the target, or no
        # such PR there) must reach the annotation, not abort the step under
        # `set -e` before it (round 4).
        code, outputs, _, log = _run_target_head(
            tmp_path, pr_number="160", api_response="", gh_exit=1
        )
        assert code == 1
        assert "could not resolve the head" in log
        assert outputs == {}
