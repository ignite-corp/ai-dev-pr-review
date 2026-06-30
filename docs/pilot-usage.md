# AI Code Review — `ignite-pilot-org` usage pattern

How the AI review pipeline is installed and run for `ignite-pilot-org` consumers.
Unlike same-org `ignite-corp` consumers, the pilot org calls through a **wrapper**
and does not support org-level secrets, so everything is configured per repo.

## Architecture (wrapper indirection)

```
consumer repo  .github/workflows/ai-review.yml
  └─ uses: ignite-pilot-org/ai-dev-pr-review-wrapper/.github/workflows/wrapper.yml@v1
        └─ uses: ignite-corp/ai-dev-pr-review/.github/workflows/base-ai-review-orchestrator.yml@v1
```

- Pilot consumers do **not** call the upstream `ignite-corp` orchestrator directly —
  they go through `ignite-pilot-org/ai-dev-pr-review-wrapper`, which forwards inputs and
  secrets to the upstream orchestrator.
- Exceptions: `spec-interview` and `factory-process-maker` historically pin the upstream
  orchestrator directly (`@v1.0.5`). Migrating them to the wrapper is recommended for
  consistency.

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
ones: `REVIEW_MODE` (`parallel` default | `sequential`), `GEMINI_MODEL`, `CLAUDE_MODEL`,
`CODEX_MODEL`, `PR_SIZE_LIMIT`, `CRITICAL_THRESHOLD`, `ALLOW_AUTO_APPROVE`. Full list:
[Runtime configuration via `vars.*`](../README.md#runtime-configuration-via-vars).

## Actions allowlist (org setting)

The org/repo Actions policy must permit the wrapper + orchestrator reusable workflows and
the underlying actions: `anthropics/claude-code-action`, `actions/checkout`,
`actions/setup-python`, `actions/download-artifact`, `actions/upload-artifact`.

## Install status (snapshot 2026-06-24)

- **Installed (13):** daon-manufacturing, peaknow, bnk-mes-prod-plan, Ignite-pilot-plugins,
  IGTdesignsystem, spec-interview, factory-process-maker, bnk-mes, ig-notification,
  ig-member, ig-ai-report, admin-tools-plugins, PS-Simulation1
- **Not installed (6):** aws-simple-deploy, wesource, wesource-fe, wesource-be,
  ig-config-manager, mg_wrap

To re-survey, list `ignite-pilot-org` repos and check each for `.github/workflows/ai-review.yml`.

## Bulk install script (for not-installed repos)

Sets the four secrets, adds the thin trigger, and copies the standard prompts, opening one
PR per repo. Export the secret values first (never hardcode); trim the `REPOS` list to the
repos that should actually run review (skip deploy/wrapper-only repos).

```bash
#!/usr/bin/env bash
set -euo pipefail
ORG=ignite-pilot-org
BR=task/install-ai-review
REPOS=( aws-simple-deploy wesource wesource-fe wesource-be )   # edit as needed

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
