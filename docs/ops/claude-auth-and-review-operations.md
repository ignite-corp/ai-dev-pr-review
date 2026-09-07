# Claude Auth & Review Operations Runbook

Durable, reusable operating lessons for the AI review fleet. Keep tight.

## Auth model - the SSOT composite

The Claude review step lives in one shared composite action,
`.github/actions/claude-review`, consumed by both `base-ai-review-single.yml`
and the pilot wrapper (via the local checkout path
`./.ai-dev-pr-review/.github/actions/claude-review`). It is the single source of
truth for:

- **OAuth-preferred auth ternary** - the composite passes
  `anthropic_api_key: ${{ inputs.claude_code_oauth_token == '' && inputs.anthropic_api_key || '' }}`.
  When an OAuth token is present, an empty API key is passed (subscription is
  used); when absent, the API key is the fallback.
- **claude-code-action SHA pin** - a full-length commit SHA (currently
  `e5ad3c77...` = v1.0.202), not a floating tag.
- **Model wiring** - `--model ${{ inputs.model }}`.
- **`api_timeout_ms` (default 600000)** - applied to BOTH `API_TIMEOUT_MS` (the
  per-request cap) and `CLAUDE_STREAM_IDLE_TIMEOUT_MS`. The CLI's byte-level
  stream-idle watchdog defaults to **180 s on direct Anthropic API
  connections** and aborts slow billed-API reviews; 600000 matches the caller
  job's 10-minute budget. (AT-1499 auth ternary + composite; AT-1601 timeout.)

## API <-> subscription toggle (per repo)

The org `CLAUDE_CODE_OAUTH_TOKEN` variable has **private** visibility.

- **Put a repo on the billed API**: set an **EMPTY** repo-level
  `CLAUDE_CODE_OAUTH_TOKEN`. Repo-level overrides org -> empty -> the composite
  ternary falls back to `ANTHROPIC_API_KEY`.
- **Restore subscription**: delete the empty repo-level variable so the org
  value applies again.
- **Requirements for the API path to work**: the repo must be in the
  `ANTHROPIC_API_KEY` selected list AND the key must be ENABLED at the
  Anthropic console.
- **CLI precedence trap**: `ANTHROPIC_API_KEY` outranks the OAuth token in the
  CLI. Passing BOTH credentials silently bills the API. That is exactly why the
  composite passes the ternary (one or the other), never both.

## Model selection

- `vars.CLAUDE_MODEL` resolves in the **CALLER** repo and its org
  (reusable-workflow context), NOT the base repo.
- **ignite-corp** sets one org var (visibility all) that covers all its
  consumers. **ignite-pilot-org has no org-level `CLAUDE_MODEL` or
  `CODEX_MODEL`** -> each wrapper consumer needs its own repo-level
  `CLAUDE_MODEL` AND `CODEX_MODEL`, else it falls through to the workflow's
  hard-coded default.
  The defaults, the onboarding commands, and the AT-2202 cost incident are
  under "Op hazard - pilot consumer onboarding must set CODEX_MODEL and
  CLAUDE_MODEL repo vars" below.
- **Avoid `gemini-2.5-flash-lite` for review** - it confabulates (fabricated 59
  "not English" flags on English text). Use `gemini-2.5-pro`.

## Release & pin discipline

- Releases are **manual**: cut `vX.Y.Z` on the merge commit; `move-major-tag`
  floats `v1` to it. Merged base changes stay inert for consumers until release
  + pin bump, so accumulating on main is safe.
- **Release gate - before moving `v1`, confirm the base `approve_quorum`
  separation (AT-2124) has landed. The gate is not the whole mitigation: the
  base path is already exposed and reaches consumers with no release at all.**
  `approve_quorum` reuses the verdict-gate availability count
  (`aggregate_reviews.py:1127`, against `MIN_REVIEWERS_FOR_VERDICT = 2` at line
  59), so **2 of 3** reviewers satisfies it and auto-approve fires on a
  **degraded reviewer set** - a formal `APPROVED` review from the reviewer App
  on a PR that a configured reviewer never ran against. A green check is
  passive - it says nothing is stopping you. A formal `APPROVED` review is an
  affirmative, attributable claim that the code was reviewed; on that PR it is
  a false attestation.
  - **What the release gate covers.** Pilot consumers track `wrapper.yml@v1`,
    and `v1` floats to release tags rather than to `main`, so merging changes
    nothing for them - they begin running the new behaviour when the float
    moves. Wrapper PR #28 (AT-2114, merged) stops one reviewer's auth failure
    from skipping the other reviewers, so a codex outage on the wrapper path
    leaves **2** reviewers available instead of 1.
  - **What it does not cover - AT-2124 is urgent independently of release
    timing.** Base already runs the three reviewers as independent parallel
    jobs (`review-gemini-p`, `review-codex-p`, `review-claude-p` in
    `base-ai-review-orchestrator.yml`, each `needs: prepare` and nothing else),
    and in sequential mode `review-gemini-s` runs even when codex fails. Either
    way one reviewer dropping out already leaves 2 available, quorum is already
    satisfied, and approval on a degraded set is **current base behaviour** -
    AT-2114 extends an existing base defect to the wrapper path, it does not
    create it. ignite-corp consumers call the orchestrator directly and inherit
    `ALLOW_AUTO_APPROVE=true` from the org unless a repo-level variable
    overrides it, so no release ever has to happen for them to be exposed and a
    release-time gate never reaches them. Do not read "release precondition" as
    "there is time".
  - Not hypothetical. Across the seven ignite-corp consumers, 2026-07-02 ->
    2026-09-02, the reviewer App posted 1150 `APPROVED` reviews, **229 (19.9%)
    of them at `2/3 LLM responses`**, and **102 merged PRs** carry such an
    approval as the last-standing approving review with no human approving
    review at all. Most recent: `ai-dev-infra-common#743`, approved
    2026-08-24T02:37:21Z at `2/3 LLM responses` and merged two minutes later.
  - Pilot exposure at the float when this was written is **six repos**:
    `aws-simple-deploy`, `ig-config-manager`, `mg_wrap`, `wesource`,
    `wesource-be`, `wesource-fe`. Do not trust that list - re-measure it. A
    repo is exposed when it has `ALLOW_AUTO_APPROVE=true` AND `REVIEWER_APP_ID`
    set AND is on `wrapper.yml@v1`.
  - **Repo-level Actions variables read with a plain `repo` scope** - only
    *org*-level variables need `admin:org` - so the list is measurable from an
    ordinary token:

    ```bash
    for r in $(gh api orgs/ignite-pilot-org/repos --paginate --jq '.[].name'); do
      gh api --paginate "repos/ignite-pilot-org/$r/actions/variables" --jq \
        '[.variables[]|select((.name=="ALLOW_AUTO_APPROVE" and .value=="true")
           or .name=="REVIEWER_APP_ID")]|length' 2>/dev/null | grep -qx 2 || continue
      gh api "repos/ignite-pilot-org/$r/contents/.github/workflows/ai-review.yml" \
        --jq .content 2>/dev/null | base64 -d | grep -q 'wrapper.yml@v1' && echo "$r"
    done
    ```

    Runs under a plain `repo` scope; echoes repository names only, never
    variable values.

    Adjust the workflow filename for any consumer not using `ai-review.yml`.
- base <-> wrapper keep **MAJOR.MINOR in lockstep**; patch versions independent.
  The wrapper reimplements the single-review job inline instead of calling base's
  reusable workflows, so a `base-ai-review-single.yml` change must be hand-ported
  into `wrapper.yml` and released with it. Only `.github/scripts/*` (including
  `review_prompt.md`) and the `claude-review` composite ride along on their own -
  the per-repo prompts come from the consumer. See `docs/pilot-usage.md`.
- **Pin-bump rule** - `single.yml`, `prepare.yml`, `aggregate.yml` each run
  different scripts from their own pinned checkout. Bump only the pin(s) serving
  the changed script:
  - `threads.jq` -> prepare
  - `aggregate_reviews.py` -> aggregate
  - `post_inline_comments.py` / `review_gemini.py` / `review_prompt.md` /
    composite action -> single
- The pinned tag will not exist until the release is cut, so **cut the release
  immediately after the pin-bump merge**. Until it exists, `self-review.yml`'s
  `guard` job skips the pin-bump PR's own review (with a `::notice::` naming
  the pin) instead of failing on the missing ref.
- **After publishing the tag, dispatch self-review at the release PR** -
  `gh workflow run self-review.yml -f pr_number=<release PR>`. Script changes
  are exercised by no PR check until they ship in a tag (see the
  `self-review.yml` header), so this single step both restores review coverage
  for the release PR and verifies the new scripts actually run. Confirm the
  checked-out ref in the aggregate job log.

## Standalone workflows = SSOT, not drift

Consumer standalone Claude workflows must consume the base composite via a
`@v1` checkout (not a local `anthropics/claude-code-action` copy). The one
documented exception is the **`@claude`-mention comment-trigger flow**: the
composite requires a `prompt` input, so that flow stays inline but must mirror
the fleet SHA pin + auth ternary + timeout env. (AT-1602.)

## Required check

- The merge gate is the pipeline's
  `review / aggregate / Aggregate & Verdict` check.
- Register it via the ruleset's **Suggestions** (source: GitHub Actions), NEVER
  a hand-typed "Any source" context - a hand-typed context sits in permanent
  **Waiting** and never satisfies (AT-1270).
- GitHub occasionally adds default fields to the ruleset API (e.g.
  `dismissal_restriction`) which trips byte-exact audits. Re-sync
  `RULESET_CONFIG` when the audit flags such drift.

## GHA expression pitfall - `A && '' || B` cannot yield empty

The `${{ X && A || B }}` ternary idiom **breaks whenever the value you want to
select (A) is falsy in GHA** (empty string, `'0'`, `'false'`, `0`). GHA
evaluates left-to-right: when `A` is falsy, `X && A` is falsy, so `|| B` falls
through to the right operand and you get `B` instead of `A`.

Real defect (AT-1606, v1.1.4 -> v1.1.5): to blank the OAuth token when forcing
the billed API, the composite used

```yaml
claude_code_oauth_token: ${{ inputs.force_api == 'true' && '' || inputs.claude_code_oauth_token }}
```

This could NEVER blank the token. With `force_api == 'true'`, the expression is
`true && '' || oauth` -> `'' || oauth` -> `oauth`, because the empty string is
falsy so `||` falls through. So `force_api=true` returned the non-empty OAuth
token. A runtime dry-run confirmed the inner `claude-code-action` received
`claude_code_oauth_token: "***"` (present), not empty.

Fix - **invert the condition so the empty/falsy result lands on the `|| ''`
side**:

```yaml
claude_code_oauth_token: ${{ inputs.force_api != 'true' && inputs.claude_code_oauth_token || '' }}
```

Truth table: `force_api == 'true'` -> `false && oauth` = false -> `false || ''`
= `''` (blanked); otherwise the OAuth token passes through.

- To select an empty/falsy value on a condition, restructure so the falsy
  result is the `|| ''` default (invert the condition), or drop the idiom.
- The auth line `${{ ... && api || '' }}` is safe ONLY because `api` is
  non-empty when selected - the same bug would bite if that operand could ever
  be empty.
- **Meta-lesson**: workflow expression changes that gate credentials/behavior
  must be verified by a **runtime dry-run**, not by reading the truth table - a
  hand-derived table missed this defect (the author "verified" it on paper).
  Dry-run method: set the gate var briefly, dispatch a real review, grep the
  INNER action's resolved inputs in the job log for the credential value
  (masked `"***"` = present, `""` = blank), then delete the var.

## Op hazard - release-op hygiene

Complements the merge-then-delete hazard below.

- Pin-bump / release-prep edits are **code changes** - delegate them to a
  teammate / worktree; don't edit them from the coordinating context.
- Commit messages must carry **NO AI attribution** - no `Co-Authored-By`, no
  `Generated with`. If a tooling default injects one, `git commit --amend` to
  strip it before the PR is reviewed.

## Op hazard - merge then delete, never chained

NEVER chain `gh pr merge && gh api ...delete-branch`. A piped merge
(`gh pr merge ... | head && ...`) masks the merge's non-zero exit, so `&&`
proceeds to delete the head branch of an UNMERGED PR, which auto-CLOSES it.

Correct sequence:
1. Run `gh pr merge --merge` ALONE.
2. Confirm the PR is `MERGED` (check exit / `state==MERGED`).
3. Only then delete the branch (usually already auto-deleted; verify with a 404
   rather than blindly deleting).

Recovery if it happens: restore the ref at `headRefOid`
(`gh api --method POST repos/O/R/git/refs -f ref=refs/heads/<b> -f sha=$HEAD`)
then `gh pr reopen N` - reviews/approvals on that SHA are preserved.

## Op hazard - probe repositories are one-way

The session tokens have no `delete_repo` scope, so `gh repo delete` returns
403. A repository a session creates, no session can remove: creation is
irreversible from where it was done, and leftovers accumulate in
`ignite-pilot-org` not because nobody would clean them but because nobody with
a token can. Two `fsb-*` scratch repos from the 2026-08-24 onboarding round
(AT-2113) are still there for that reason, and a `*-repro-*` probe repo for an
AT-2125 measurement hit the same wall on 2026-09-02. (AT-2127.)

**Convention: never create a repository for a probe.** Probe against things
that already have an exit path:

- an existing repository - a throwaway branch on an existing scratch repo, or
  a branch plus PR that can be closed and deleted afterwards (the AT-2092
  observation finished at branch/PR level);
- a manual dispatch instead of a synthetic event - the thin trigger in the
  consumer (`ai-review.yml`; `self-review.yml` here) owns `workflow_dispatch`
  and forwards `pr_number` to the reusable orchestrator or `wrapper.yml`, which
  are `workflow_call` only (the wrapper also takes `upstream_ref`), so a review
  can be run against an existing PR at a chosen ref without creating anything
  (README, "Input contract reference");
- the runtime dry-run under "GHA expression pitfall" above: set a gate
  variable briefly, dispatch a real review, read the resolved inputs from the
  job log, delete the variable.

**The general lesson: verify the exit path BEFORE the action whenever the
safety argument rests on it.** The approved plan for the AT-2125 probe read
"the throwaway keys can stay throwaway; the repo is deleted at the end". The
second clause was false, and the whole safety argument stood on it. Only one
repo was stranded, and none holding a live provider key set, because the
implementer ran the delete probe before minting any credential - tested the
plan's exit condition first and found it closed. Create-then-delete would have
left a repository with a key set in it permanently. The order decided the
outcome.

Options not taken (AT-2127):

- Acquire `delete_repo` scope - rejected: it deletes any repository in the
  org, an org-wide destructive scope on a shared token for a cleanup
  convenience.
- A periodic cleanup pass over `*-repro-*` / `*-smoke-*` names - not adopted:
  with the convention above nothing new accumulates, and deleting anything
  would need the scope above anyway.

The two existing `fsb-*` repos are not deleted on name or age alone. They
belong to another operator's onboarding work; a human confirms they are dead
before anything is removed.

## Op hazard - pilot consumer onboarding must set CODEX_MODEL and CLAUDE_MODEL repo vars

`ignite-pilot-org` has no org-level `CODEX_MODEL` or `CLAUDE_MODEL`: its org
variable list holds only `PR_SIZE_LIMIT` and `REVIEW_MODE` (measured
2026-09-07; what the org lacks outright is org-level secrets, not variables).
Reading that list needs `admin:org`, so a 403 there is not evidence a var is
absent. A pilot consumer missing a model variable therefore inherits nothing -
it falls through to the hard-coded default in the workflow:
`vars.CODEX_MODEL || 'gpt-5.6-luna'` and `vars.CLAUDE_MODEL || 'claude-sonnet-4-6'`
in both base and wrapper v1.8.0 (the Claude value is also the `model` input
default of the `claude-review` composite). The fallthrough is silent - `vars.X || default` logs nothing - and
the default is whatever the workflow author pinned at release time, not
necessarily the model the fleet is meant to run on.

Incident (AT-2202, 2026-09-07): five pilot consumers had no `CODEX_MODEL` set
and fell through to the default of the time, `gpt-5.5`, roughly 20x the OpenAI
spend per run of the intended model.

Onboarding step, per repo, before the first review runs:

```bash
gh variable set CODEX_MODEL --repo ignite-pilot-org/<repo> --body <model>
gh variable set CLAUDE_MODEL --repo ignite-pilot-org/<repo> --body <model>
```

Verify:

```bash
gh api --paginate --slurp "repos/ignite-pilot-org/<repo>/actions/variables" \
  | jq '{total: .[0].total_count, names: [.[].variables[].name]}'
```

`names` must contain both variables, and its length must equal `total`.
`--paginate` matters: the endpoint pages at 10 by default and caps `per_page`
at 30, so a plain call on a repo with more variables than that hides a missing
one behind a truncated list. `--slurp` folds the pages into one array so the
count comes from the same response as the names.
Where `vars.*` resolve (caller repo and its org, never the base repo) is under
"Model selection" above.
