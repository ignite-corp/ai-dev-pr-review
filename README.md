# ai-dev-pr-review

Reusable GitHub Actions workflows for multi-LLM pull request review (Claude + Codex + Gemini, parallel or sequential), with inline comment posting, deduplication against prior rounds, and a rule-based aggregate verdict.

## What this repo provides

Four `workflow_call` workflows under `.github/workflows/`:

| Workflow | Purpose |
|---|---|
| `base-ai-review-orchestrator.yml` | Top-level entry point. Spawns prepare + reviewers + aggregate. Consumer thin triggers call this. |
| `base-ai-review-prepare.yml` | Sizes the PR, extracts the diff + context, fetches prior review threads and verified action SHA pins, uploads as `review-context` artifact. |
| `base-ai-review-single.yml` | Runs one reviewer (Claude / Codex / Gemini), writes `review-<reviewer>.json`, posts inline comments. |
| `base-ai-review-aggregate.yml` | Loads all reviewer outputs, applies severity rules, posts the consolidated verdict on the PR. |

Helpers under `.github/scripts/` (Python 3.14, plus one bash + one jq):
`aggregate_reviews.py`, `extract_claude_review.py`, `fetch_review_context.py`, `github_pr_support.py`, `post_inline_comments.py`, `review_gemini.py`, `verify_action_shas.py`, `collect_review_threads.sh`, `threads.jq`, `review_prompt.md`, `requirements.txt`, `.python-version`.

Schema under `.github/schemas/review-schema.json` (the per-reviewer output contract).

## Required consumer-repo secrets

Pass all three explicitly in the consumer thin trigger. `secrets: inherit` does NOT work cross-org — use the explicit form below in every consumer, same-org or not.

| Secret | Used by | Notes |
|---|---|---|
| `OPENAI_API_KEY` | Codex reviewer | Org or repo secret. Codex CLI logs in via stdin. |
| `GOOGLE_AI_API_KEY` | Gemini reviewer | Org or repo secret. |
| `CLAUDE_CODE_OAUTH_TOKEN` | Claude reviewer | OAuth token from `anthropics/claude-code-action`. |

## Minimal consumer thin trigger

Drop this into the consumer repo as `.github/workflows/ai-review.yml`:

```yaml
name: AI Code Review
on:
  pull_request:
    branches: [main]
  workflow_dispatch:
    inputs:
      pr_number:
        description: "PR number to review"
        required: true
        type: string

jobs:
  review:
    uses: ignite-corp/ai-dev-pr-review/.github/workflows/base-ai-review-orchestrator.yml@v1.0.5
    with:
      pr_number: ${{ inputs.pr_number || '' }}
      code-review-system-prompt-path: .github/prompts/code-review-system.md
      code-review-checklist-path: .github/prompts/code-review-checklist.md
    secrets:
      OPENAI_API_KEY: ${{ secrets.OPENAI_API_KEY }}
      GOOGLE_AI_API_KEY: ${{ secrets.GOOGLE_AI_API_KEY }}
      CLAUDE_CODE_OAUTH_TOKEN: ${{ secrets.CLAUDE_CODE_OAUTH_TOKEN }}
```

See `examples/consumer-thin-trigger.yml` for the full file with cross-org variant.

## Pinning strategy

Consumers can pin the reusable workflow ref in two ways. Each is supported and tagged simultaneously.

### Option 1 — Major floating tag (`@v1`) — default recommendation

```yaml
uses: ignite-corp/ai-dev-pr-review/.github/workflows/base-ai-review-orchestrator.yml@v1
```

The `v1` tag automatically tracks the latest `v1.x.y` release. When this repo publishes `v1.0.8`, `v1.0.9`, etc., the `v1` tag is force-moved to the new commit by the `move-major-tag.yml` workflow. All `@v1` consumers pick up the change on their next `ai-review` run — no per-consumer PR.

**Use when**: you trust this upstream and want fixes (like AT-1264's codex stdout fallback) without per-release admin work.

**Note**: breaking changes ship under `v2`, with a new `v2` tag. `@v1` consumers are NOT auto-bumped to v2 — that requires an explicit caller update. So `@v1` is safe within the v1 major line.

### Option 2 — Specific version pin (`@v1.0.7`)

```yaml
uses: ignite-corp/ai-dev-pr-review/.github/workflows/base-ai-review-orchestrator.yml@v1.0.7
```

Pins to a specific immutable commit. Each new release surfaces as a Dependabot bump PR (when `package-ecosystem: github-actions` is enabled).

**Use when**: you want explicit per-release review/approval (audit trail), want to defer adopting a new minor for any reason, or are in a regulated environment that requires immutable supply-chain refs.

### Comparison

| Aspect | `@v1` (mutable major) | `@v1.0.X` (specific) |
|---|---|---|
| New release adoption | Automatic, next run | Manual via Dependabot PR |
| Per-release PR overhead | None | 1 PR per consumer per release |
| Audit trail | Coarser (major-line) | Per-release explicit |
| Breaking-change safety | Pinned to v1.x.x (won't auto-jump to v2) | Pinned exactly |
| Force-push window | Yes (between release publish and next caller run) | None |

### Switching between the two

To switch a consumer from specific to floating:

```yaml
# Before
uses: ignite-corp/ai-dev-pr-review/.github/workflows/base-ai-review-orchestrator.yml@v1.0.7
# After
uses: ignite-corp/ai-dev-pr-review/.github/workflows/base-ai-review-orchestrator.yml@v1
```

And vice versa. Both refs always resolve.

## Input contract reference

All workflows accept `workflow_call` inputs:

| Input | Type | Required | Default | Notes |
|---|---|---|---|---|
| `pr_number` | string | false | `""` | Required only when triggered via `workflow_dispatch`. On `pull_request`, the orchestrator reads `github.event.pull_request.number`. |
| `code-review-system-prompt-path` | string | false | `.github/prompts/code-review-system.md` | Path INSIDE THE CONSUMER REPO to the per-repo system prompt. The prepare workflow `git show`s this from the PR's base branch (not from PR head) to prevent prompt injection. |
| `code-review-checklist-path` | string | false | `.github/prompts/code-review-checklist.md` | Same path semantics. |

The `single` and `aggregate` workflows additionally accept reviewer-routing inputs (`reviewer`, `claude_result`, `codex_result`, `gemini_result`) but consumers do not invoke them directly — the orchestrator wires them.

## Runtime configuration via `vars.*`

These tune behavior without code changes. Set them under repository or organization `Settings -> Secrets and variables -> Actions -> Variables`.

| Var | Default | Affects |
|---|---|---|
| `PR_SIZE_LIMIT` | `3000` | Skip review when added+deleted lines exceed this. Comments on the PR and returns `skip=true`. |
| `REVIEW_MODE` | `parallel` | `parallel` (default) runs all three reviewers concurrently. `sequential` runs Claude -> Codex -> Gemini and stops on `early_exit`. |
| `CRITICAL_THRESHOLD` | `1` | Human PRs: number of critical issues that triggers `request_changes`. |
| `DEPENDABOT_CRITICAL_THRESHOLD` | `2` | Dependabot PRs only: same gate, raised. |
| `MAJOR_CONSENSUS_OVERLAP` | `0.3` | Word-overlap ratio (0-1) at which two reviewers' major findings count as the same issue. |
| `DEPENDABOT_MAJOR_CONSENSUS_OVERLAP` | `0.5` | Dependabot-only. |
| `MAJOR_CONSENSUS_MIN` | `2` | Number of reviewers required for consensus to trigger `request_changes` on a major issue. |
| `CLAUDE_MODEL` | `claude-opus-4-7` | Model passed to `anthropics/claude-code-action`. |
| `CODEX_MODEL` | `gpt-5.5` | Model passed to `codex exec --model`. |
| `GEMINI_MODEL` | `gemini-2.5-pro` | Model passed to the `google-genai` client. |
| `BOT_LOGIN` | `github-actions[bot]` | Author login used for minimizing prior bot comments and dismissing stale reviews. |
| `JACCARD_THRESHOLD` | `0.6` | Token-set Jaccard similarity threshold for dedup. Lower values dedup more aggressively (more strings collapse to same issue), higher values are stricter. Tune `0.5`-`0.8` for behavior trade-off. |
| `ALLOW_AUTO_APPROVE` | `false` | Killswitch. When `false`, "approve" verdicts are posted as plain comments (no actual approval submitted). Flip to `true` to enable real `gh pr review --approve`. |

## Concurrency and re-push behavior

The orchestrator sets `concurrency: { group: ai-review-<pr-number>, cancel-in-progress: true }`, so each PR has at most one active review run at a time. The group key is the PR number (`inputs.pr_number` for `workflow_dispatch`, falling back to `github.run_id`).

- **New push during a review:** every push fires `pull_request: synchronize`, which starts a new run and cancels the in-progress run for the same PR. The new run restarts from `prepare` against the latest diff. Runs do not accumulate — the PR converges to a single active run.
- **Different PRs:** different group keys, so they run independently and never cancel each other.
- **`sequential` trade-off:** because reviewers run one after another (`Claude -> Codex -> Gemini`), a run takes longer wall-clock than `parallel`, so a re-push is more likely to land mid-run. Cancellation discards already-completed stages (e.g. a finished Claude review) and the new run re-runs the chain from the start. `parallel` wastes less work on rapid successive pushes.
- **Manual `workflow_dispatch`:** pass `pr_number` so the group key matches the PR. Without it the key falls back to `github.run_id`, which is unique per run, so concurrent manual runs are not de-duplicated.

## Cross-org usage

GitHub does NOT propagate `secrets: inherit` across organizations. For `ignite-pilot-org` (or any other org) consumers:

1. Org admin: configure `OPENAI_API_KEY`, `GOOGLE_AI_API_KEY`, `CLAUDE_CODE_OAUTH_TOKEN` as org-level secrets and grant the consumer repos access.
2. Consumer thin trigger: use the explicit `secrets:` mapping shown above. Do NOT use `secrets: inherit`.
3. Org admin: ensure the Actions allowlist permits `anthropics/claude-code-action`, `actions/checkout`, `actions/setup-python`, `actions/download-artifact`, `actions/upload-artifact`. The reusable workflow itself does not pull `oven-sh/setup-bun`, but the underlying `anthropics/claude-code-action` may; check that action's requirements before adding the consumer.

## Receiving release updates (Dependabot)

When this repo publishes a new tag, each consumer's own Dependabot opens a 1-line bump PR on the consumer side. No central propagation infrastructure or cross-org token required.

### Per-consumer setup (one-time)

Add `.github/dependabot.yml` in each consumer repo:

````yaml
version: 2
updates:
  - package-ecosystem: github-actions
    directory: "/"
    schedule:
      interval: weekly
    open-pull-requests-limit: 5
````

Dependabot for `github-actions` covers reusable workflow refs (e.g. `uses: ignite-corp/ai-dev-pr-review/.github/workflows/base-ai-review-orchestrator.yml@v1.0.5`) in addition to plain action refs. Adjust `interval` to `daily` for faster pickup or `monthly` for less PR noise.

### Trade-offs vs. central propagation

- **Pros**: zero shared secrets, dependabot[bot] uses each repo's least-privilege token, no admin overhead per release
- **Cons**: not instant — bumps appear within the configured `interval`, not the moment the tag is pushed

The previous push-based workflow (PR #6) was reverted; consumers now self-pull via Dependabot.

## Tag pinning

Consumers MUST pin to an immutable tag, e.g. `@v1.0.0`. Do NOT use `@main` in production triggers — a force-push or experimental commit on `main` would propagate to every consumer immediately.

Recommended upgrade flow:

1. Watch for new releases via GitHub Releases / Dependabot.
2. Open a PR in the consumer that bumps the pin: `@v1.0.0` -> `@v1.1.0`.
3. The new pin runs against the PR itself, which gives you a real-world test of the upgrade.
4. Merge once verdict is clean.

Version policy:

- Patch (`v1.0.x`): bug fixes, no behavior change for consumers.
- Minor (`v1.x.0`): new optional inputs, additive reviewer features.
- Major (`v2.0.0`): input contract changes, breaking script signatures, severity threshold defaults shift.

## Overriding prompts per consumer repo

The per-repo system prompt and checklist live in the consumer repo, NOT here. The reusable workflow reads them via `code-review-system-prompt-path` / `code-review-checklist-path`. To customize:

1. In the consumer repo, create / edit `.github/prompts/code-review-system.md` with repo-specific rules (architecture conventions, naming taboos, security expectations).
2. Reference it from the thin trigger if you use a non-default path:
   ```yaml
   with:
     code-review-system-prompt-path: my/custom/path/system.md
   ```
3. Commit the prompt to the consumer's BASE branch. The `prepare` workflow always reads from the base branch, never the PR head, to prevent prompt injection.

## Severity icons

The aggregate verdict comment and inline reviewer comments use single-character ASCII severity indicators:

| Severity | Icon |
|---|---|
| critical | `!` |
| major | `+` |
| minor | `-` |
| suggestion | `?` |

This is a deliberate ASCII-only choice for the public repo. Consumers that want richer icons (emoji) can fork or open a PR to make `SEVERITY_ICONS` configurable.

## Contributing

Open issues and PRs against this repo. CI / tests for the public repo are out of scope for v1.0.0; see `CONTRIBUTING.md` (TBD) once it exists.

## License

See `LICENSE` (decision pending — review `LICENSE_RECOMMENDATION.md`).
