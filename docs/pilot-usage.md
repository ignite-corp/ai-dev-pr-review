# AI Code Review — `ignite-pilot-org` usage pattern

How the AI review pipeline is installed and run for `ignite-pilot-org` consumers.
Unlike same-org `ignite-corp` consumers, the pilot org calls through a **wrapper**
and does not support org-level secrets, so everything is configured per repo.

## Architecture (the wrapper reimplements, it does not delegate)

```
consumer repo  .github/workflows/ai-review.yml
  └─ uses: ignite-pilot-org/ai-dev-pr-review-wrapper/.github/workflows/wrapper.yml@v1
       one `review:` job with every prepare/reviewer/aggregate step written out inline
        └─ actions/checkout ignite-corp/ai-dev-pr-review@${{ inputs.upstream_ref }}
             into `.ai-dev-pr-review/` — helper scripts, `review_prompt.md` and the
             shared `.github/actions/claude-review` composite only
```

- The wrapper does **not** call `base-ai-review-orchestrator.yml`, or any other reusable
  workflow from this repo. `wrapper.yml` is a hand-maintained duplicate of the review
  pipeline. Measured at wrapper `c3dd05c` (v1.6.0 plus AT-1979), its 726 non-comment,
  non-blank lines partition as:
  - **603** copied verbatim from base's `prepare` / `single` / `aggregate` / `orchestrator`
    (identical once leading indentation is ignored);
  - **98** scaffolding forced by collapsing base's five jobs into one — per-step
    `steps.size-check.outputs.skip` / `early_exit` guards, the per-reviewer early-exit reads,
    verdict synthesis and reviewer status computation. Base gets all of this for free from
    job-level `needs:` and `result`;
  - **25** genuinely wrapper-specific — the `upstream_ref` input and its checkout, the
    `.wrapper-defaults/.prompts/` fallback, the hardcoded `REVIEW_MODE: sequential`, and
    workflow identity.

  Re-measure against the wrapper's current `main` before quoting these numbers; the totals
  move with every wrapper release.

  Wrapper internals are cited **by step name** below, never by line number. To locate one,
  `grep -n '<step name>' .github/workflows/wrapper.yml` in the wrapper repo. Line numbers
  do not survive: `wrapper.yml:494` was already wrong when this page was last corrected,
  and `wrapper.yml:816` / `:135` (AT-1982 comments 84957 and 85007) and
  `claude-code-action` `action.yml:192` (comment 85002) were all stale within a day of
  being written. A step name survives every edit that does not rename the step.
- What it consumes from base is checked out at `upstream_ref` (default: the floating `v1`
  tag): the `.github/scripts/*` helpers, `.github/scripts/review_prompt.md` (the shared
  reviewer instruction, copied by the `Use trusted Codex prompt from ai-dev-pr-review`
  step), and the `.github/actions/claude-review` composite. Changes to **those** reach
  pilot consumers on the next run with no wrapper edit.
- The per-repo prompts do **not** come from base. `code-review-system.md` and
  `code-review-checklist.md` are read out of the *consumer* repo — base branch first, PR head
  second — and the last-resort fallback is the wrapper's own `.prompts/`, sparse-checked-out
  at the wrapper's own release tag by the `Checkout wrapper canonical default prompts
  (pinned to release tag)` step, so the prompts move in lockstep with the pin, the same
  way the base scripts do. It read `ref: main` until wrapper v1.6.0, which
  left the prompts outside that lockstep (AT-1957, fixed). `examples/prompts/` in this repo
  is a starter template copied once at install time, not a runtime source.
- **A change to `base-ai-review-single.yml` does not reach pilot consumers automatically.**
  The workflow body is duplicated, so it must be hand-ported into `wrapper.yml` and shipped
  as a lockstep wrapper release — see [Tag pinning](../README.md#tag-pinning). **Eight**
  drift incidents have been confirmed this way (AT-1800, AT-1955, AT-1837, AT-1979, and
  AT-2120's missing `ROUND_CUTOFF_N`/`ROUND_CUTOFF_ENABLED` — six MINOR releases and seven
  manual investigations passed before it was noticed; closed by wrapper `82d261d`, v1.7.1 —
  among them). The seventh — the
  wrapper's PR-too-large comment wording, diverged from `base-ai-review-prepare.yml` and
  recorded in AT-1982 comment 85007 — **is closed**: the two `--body "[!] PR too large
  ..."` lines are now identical once indentation is ignored (re-checked 2026-09-02, wrapper
  `c900a95` against base `8266040`). All eight were found by hand while doing something
  else, none by an automated check, at the time each happened. Since AT-2122 an automated
  check covers part of that gap: `.github/workflows/base-wrapper-drift.yml` in **this**
  repo checks out wrapper `main` and runs `.github/scripts/check_base_wrapper_drift.py`
  daily, on every push to base `main`, and on dispatch. It diffs each base review step's
  `env:` key set against the union of its corresponding wrapper step(s)', and the full
  `vars.*`-consumed set of base's four `base-ai-review-*.yml` files against wrapper's
  `wrapper.yml` — exactly the two axes that, applied by hand, caught AT-2120, and the check
  is regression-tested against the real pre-fix wrapper tree (`7de4d6e`) to prove it does.
  The step correspondence and every declared exception live in `.github/drift-check/`;
  each exception carries a mandatory reason, and a base step that sets env keys but has
  no entry there fails the check. It runs in base rather than in the wrapper because base
  is where a change originates and both repos are public, so there is no log-exposure
  asymmetry to route around (the AT-1944 concern does not apply). It is deliberately not
  a required PR check: the lockstep order is base release → wrapper port → wrapper release,
  so nothing inside a base PR can make it green — a red run on `main` between a base merge
  and the wrapper port *is* the signal. It does **not** check `if:`-condition equivalence
  between base's job-level gating and wrapper's step-level gating: the two architectures
  differ legitimately often enough (AT-2092's base fix reverted `!cancelled()` to
  `always()` in one place for reasons that do not transfer from a job to a step) that
  telling a legitimate difference from real drift on that axis is not solved yet. A drift
  on that axis would still only be found by hand.
- Direct-to-base consumers: `spec-interview` and `factory-process-maker` call the upstream
  orchestrator directly rather than through the wrapper, tracking the floating `@v1` tag.
  This is the normal, designed pattern for a public repo, not a workaround: per AT-1210's
  visibility rules, a **public** reusable workflow can be called cross-org by any caller
  regardless of the caller's own visibility — only private/internal reusable workflows are
  restricted to same-org callers.

## Installation — three components per repo

### 1. Secrets (repo-level, four)

The pilot org does not support org secrets, so set these on **each** repo. `secrets:
inherit` does not work cross-org — the thin trigger maps them explicitly.

| Secret | Required | Used by |
|---|---|---|
| `OPENAI_API_KEY` | yes | Codex reviewer |
| `GOOGLE_AI_API_KEY` | yes | Gemini reviewer |
| `CLAUDE_CODE_OAUTH_TOKEN` | one of these two | Claude reviewer (Pro/Max subscription OAuth) |
| `ANTHROPIC_API_KEY` | one of these two | Claude reviewer (`sk-ant-` API key) |

The Claude reviewer needs at least one of the last two; the `consumer-health` check
reports all four so a misconfigured repo surfaces early.

**Optional — real APPROVED reviews.** `github-actions[bot]` cannot approve PRs, so an
`approve` verdict posts a plain comment by default. To get a real APPROVED review, set a
per-repo var `REVIEWER_APP_ID` and a per-repo secret `REVIEWER_APP_PRIVATE_KEY` from a
dedicated reviewer GitHub App, and map the secret in the thin trigger
(`REVIEWER_APP_PRIVATE_KEY: ${{ secrets.REVIEWER_APP_PRIVATE_KEY }}`). Leaving both unset
keeps the current comment-fallback behavior.

### 2. Thin trigger — `.github/workflows/ai-review.yml`

Calls `wrapper.yml@v1`, maps the four secrets explicitly, and sets `branches:` to the
repo's default branch.

```yaml
name: AI Code Review
on:
  pull_request:
    branches: [main]            # use the repo's default branch
    types: [opened, synchronize]
  workflow_dispatch:
    inputs:
      pr_number: { description: "PR number to review", required: true, type: string }
permissions:
  contents: read
  pull-requests: write
jobs:
  review:
    uses: ignite-pilot-org/ai-dev-pr-review-wrapper/.github/workflows/wrapper.yml@v1
    with:
      pr_number: ${{ inputs.pr_number || '' }}
      code-review-system-prompt-path: .github/prompts/code-review-system.md
      code-review-checklist-path: .github/prompts/code-review-checklist.md
    secrets:
      ANTHROPIC_API_KEY: ${{ secrets.ANTHROPIC_API_KEY }}
      OPENAI_API_KEY: ${{ secrets.OPENAI_API_KEY }}
      GOOGLE_AI_API_KEY: ${{ secrets.GOOGLE_AI_API_KEY }}
      CLAUDE_CODE_OAUTH_TOKEN: ${{ secrets.CLAUDE_CODE_OAUTH_TOKEN }}
```

### 3. Prompts — `.github/prompts/code-review-system.md` + `code-review-checklist.md`

Copy the org-standard starter templates from `examples/prompts/` in this repo and fill
the two `<...>` placeholders (project identity, architecture layers). The `prepare`
workflow reads them from the **base branch** (prompt-injection safety), so they take
effect on the next PR after they merge. See [Overriding prompts per consumer
repo](../README.md#overriding-prompts-per-consumer-repo) for how `system.md` and
`checklist.md` differ and how to author each.

## Runtime configuration (`vars.*`, optional)

Set under repo or org `Settings → Secrets and variables → Actions → Variables`. Common
ones: `GEMINI_MODEL`, `CLAUDE_MODEL`, `CODEX_MODEL`, `PR_SIZE_LIMIT`,
`CRITICAL_THRESHOLD`, `ALLOW_AUTO_APPROVE`, `REVIEWER_APP_ID` (see Secrets above). Full
list: [Runtime configuration via `vars.*`](../README.md#runtime-configuration-via-vars).

### `REVIEW_MODE` does nothing on the wrapper path

**Do not set `REVIEW_MODE` for a wrapper consumer. It has no effect, and nothing will
tell you so.** The README documents it because base consumers have it: the orchestrator
gates its reviewer jobs on `vars.REVIEW_MODE`, fanning them out or chaining them. The
wrapper has no jobs to fan out — it is a single `review:` job that runs the three
reviewers as consecutive steps, so the mode is not a choice it is able to make. It hands
the aggregator a literal `REVIEW_MODE: sequential` (step `Aggregate and post verdict`)
and never reads the variable at all.

`ignite-pilot-org` has an org-level `REVIEW_MODE=parallel` set today. It is dead. Runs
go on reviewing sequentially and report `success`, so the setting looks accepted; the
failure is entirely silent. `REVIEW_MODE` is the *only* such variable: grepping
`vars.[A-Z_]*` out of `wrapper.yml` and differencing it against the README's `vars.*`
table leaves `REVIEW_MODE` and nothing else unread. Every other variable the README
documents does work here. That README-table difference could not see AT-2120's
`ROUND_CUTOFF_*` (missing from the table and the wrapper alike); since AT-2122 the
`vars.*` difference is taken against base's workflow files instead, by the drift check
described under [Architecture](#architecture-the-wrapper-reimplements-it-does-not-delegate),
and `REVIEW_MODE` is its one declared `vars.*` exception (`.github/drift-check/exceptions.yml`).

## Actions allowlist (org setting)

The org/repo Actions policy must permit the wrapper + orchestrator reusable workflows and
the underlying actions:

- `anthropics/claude-code-action`
- `actions/checkout`
- `actions/setup-python`
- `actions/download-artifact` (base/orchestrator path)
- `actions/upload-artifact`
- `actions/create-github-app-token` — mints the reviewer App token when `REVIEWER_APP_ID`
  is set. A blanket `actions/*@*` pattern already covers it; it is named here because a
  policy written action-by-action will not.
- `oven-sh/setup-bun` — **we never call it.** `anthropics/claude-code-action` uses it
  internally (its `action.yml`, `Setup Bun` step), and a nested action is checked against
  the allowlist under its own name, so allowing the parent is not enough. Leaving it out
  does not fail the run: the Claude step dies with `action_invocation_failed`, the
  aggregate writes an error verdict for Claude, and the run still finishes `success`.
  That is what happened to the AT-1982 experiment run on `spec-interview` — a 2-of-3
  review read as a healthy 3-reviewer run, and it nearly became the evidence for retiring
  the wrapper.

## Install status (snapshot 2026-09-02)

All 32 non-archived `ignite-pilot-org` repos surveyed; 19 run review.

- **Wrapper consumers (17)**, all on the floating `wrapper.yml@v1`: ig-notification,
  ig-member, mg_wrap, aws-simple-deploy, bnk-mes, ig-ai-report, daon-manufacturing,
  ig-config-manager, admin-tools-plugins, peaknow, PS-Simulation1, IGTdesignsystem,
  Ignite-pilot-plugins, bnk-mes-prod-plan, wesource-fe, wesource-be, wesource
- **Base-direct consumers (2)**, tracking the floating `@v1` tag: spec-interview,
  factory-process-maker — the direct-to-base bullet above
- **No review workflow (12):** factory-system-builder, ig-movie-editor,
  IGTdesignsystem_VOC, ig-movie-editor-app, max-kakao-gateway, Ignite-pilot-compass,
  factory-process-simulator, fsb-repository-publisher-bootstrap,
  fsb-github-app-smoke-20260824-083722, komt-dev, factory-notok, hanbit-cnc

The 32nd repo is `ai-dev-pr-review-wrapper` itself, which is not a consumer.

To re-survey: `gh api orgs/ignite-pilot-org/repos --paginate --jq '.[] | select(.archived==false) | .name'`,
then read `.github/workflows/ai-review.yml` in each and classify by its `uses:` line.
Cheap and exact — re-run it rather than trusting the date above.

## Bulk install script (for not-installed repos)

Sets the four secrets, adds the thin trigger, and copies the standard prompts, opening one
PR per repo. Export the secret values first (never hardcode); fill the `REPOS` list from
the **No review workflow** list above, keeping only the repos that should actually run
review (skip deploy-only and scratch repos). The list below is an example, not a
worklist — re-run the survey before using it, or you will open install PRs against repos
that already run the wrapper.

```bash
#!/usr/bin/env bash
set -euo pipefail
ORG=ignite-pilot-org
BR=task/install-ai-review
REPOS=( komt-dev hanbit-cnc )   # example only -- refill from a fresh survey

# OPENAI + GOOGLE are required; Claude needs at least ONE of OAUTH / ANTHROPIC.
: "${OPENAI_API_KEY:?export}" "${GOOGLE_AI_API_KEY:?export}"
[ -n "${CLAUDE_CODE_OAUTH_TOKEN:-}" ] || [ -n "${ANTHROPIC_API_KEY:-}" ] || {
  echo "export CLAUDE_CODE_OAUTH_TOKEN and/or ANTHROPIC_API_KEY" >&2; exit 1; }

b64() { base64 | tr -d '\n'; }   # single-line base64 for the contents API

SYS=$(gh api repos/ignite-corp/ai-dev-pr-review/contents/examples/prompts/code-review-system.md --jq .content | base64 -d)
CHK=$(gh api repos/ignite-corp/ai-dev-pr-review/contents/examples/prompts/code-review-checklist.md --jq .content | base64 -d)

for r in "${REPOS[@]}"; do
  R="$ORG/$r"; def=$(gh api "repos/$R" --jq .default_branch)
  echo "=== $R (base=$def) ==="

  for n in OPENAI_API_KEY GOOGLE_AI_API_KEY CLAUDE_CODE_OAUTH_TOKEN ANTHROPIC_API_KEY; do
    [ -n "${!n:-}" ] || { echo "  - skip $n (unset)"; continue; }
    gh secret set "$n" --repo "$R" --body "${!n}" && echo "  ✓ secret $n"
  done

  WF=$(cat <<YAML
name: AI Code Review
on:
  pull_request:
    branches: [$def]
    types: [opened, synchronize]
  workflow_dispatch:
    inputs:
      pr_number: { description: "PR number to review", required: true, type: string }
permissions:
  contents: read
  pull-requests: write
jobs:
  review:
    uses: ignite-pilot-org/ai-dev-pr-review-wrapper/.github/workflows/wrapper.yml@v1
    with:
      pr_number: \${{ inputs.pr_number || '' }}
      code-review-system-prompt-path: .github/prompts/code-review-system.md
      code-review-checklist-path: .github/prompts/code-review-checklist.md
    secrets:
      ANTHROPIC_API_KEY: \${{ secrets.ANTHROPIC_API_KEY }}
      OPENAI_API_KEY: \${{ secrets.OPENAI_API_KEY }}
      GOOGLE_AI_API_KEY: \${{ secrets.GOOGLE_AI_API_KEY }}
      CLAUDE_CODE_OAUTH_TOKEN: \${{ secrets.CLAUDE_CODE_OAUTH_TOKEN }}
YAML
)

  head=$(gh api "repos/$R/git/ref/heads/$def" --jq .object.sha)
  gh api --method POST "repos/$R/git/refs" -f ref="refs/heads/$BR" -f sha="$head" >/dev/null 2>&1 || true
  put() { gh api --method PUT "repos/$R/contents/$1" -f message="$2" \
            -f content="$(printf '%s' "$3" | b64)" -f branch="$BR" >/dev/null && echo "  ✓ $1"; }
  put ".github/workflows/ai-review.yml"          "ci: install AI code review (wrapper)" "$WF"
  put ".github/prompts/code-review-system.md"    "docs: add code review system prompt"  "$SYS"
  put ".github/prompts/code-review-checklist.md" "docs: add code review checklist"       "$CHK"
  gh pr create --repo "$R" --base "$def" --head "$BR" \
    --title "ci: install AI code review" \
    --body "Wrapper thin-trigger + org-standard prompts + repo secrets. Fill the <...> placeholders (project identity, architecture layers) in code-review-system.md before merge." \
    | sed 's/^/  PR: /'
done
```

> The installed `code-review-system.md` ships with `<PROJECT_NAME>` / `<ARCHITECTURE_LAYERS>`
> placeholders — fill them in the install PR before merge.
