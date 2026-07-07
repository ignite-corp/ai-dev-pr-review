# ai-dev-pr-review

[English](README.md) | [한국어](README.ko.md)

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
`aggregate_reviews.py`, `extract_claude_review.py`, `extract_codex_json.py`, `fetch_review_context.py`, `github_pr_support.py`, `post_inline_comments.py`, `review_gemini.py`, `verify_action_shas.py`, `collect_review_threads.sh`, `threads.jq`, `review_prompt.md`, `requirements.txt`, `.python-version`.

Schema under `.github/schemas/review-schema.json` (the per-reviewer output contract).

## Required consumer-repo secrets

`OPENAI_API_KEY` and `GOOGLE_AI_API_KEY` are required. The Claude reviewer needs at least one of `CLAUDE_CODE_OAUTH_TOKEN` or `ANTHROPIC_API_KEY` (see [Claude reviewer auth](#claude-reviewer-auth-oauth-token-vs-api-key) below). Pass them explicitly — `secrets: inherit` does NOT work cross-org, same-org or not.

| Secret | Required | Used by | Notes |
|---|---|---|---|
| `OPENAI_API_KEY` | yes | Codex reviewer | Org or repo secret. Codex CLI logs in via stdin. |
| `GOOGLE_AI_API_KEY` | yes | Gemini reviewer | Org or repo secret. |
| `CLAUDE_CODE_OAUTH_TOKEN` | one of these two | Claude reviewer | Claude Pro/Max subscription OAuth token (`claude setup-token`). |
| `ANTHROPIC_API_KEY` | one of these two | Claude reviewer | Standard `sk-ant-` API key. |
| `REVIEWER_APP_PRIVATE_KEY` | no | Aggregate approve | Private key of a dedicated reviewer GitHub App. Paired with the `REVIEWER_APP_ID` var, it enables a real APPROVED review on `approve` verdicts. Without it (or the var), `approve` posts a plain comment — the current default behavior. |

### Claude reviewer auth: OAuth token vs API key

The Claude reviewer runs through `anthropics/claude-code-action`, which accepts two credential types and requires **at least one** of them (or workload identity):

| | `CLAUDE_CODE_OAUTH_TOKEN` | `ANTHROPIC_API_KEY` |
|---|---|---|
| What it is | OAuth token for a Claude Pro/Max **subscription** (`claude setup-token`) | Standard **API key** (`sk-ant-...`) |
| Billing | Against the subscription | Against your Anthropic API account (usage-based) |
| Generate via | `claude setup-token` locally | Anthropic Console |

**Precedence when both are set:** the action documents the two as **mutually exclusive** and does NOT define which wins — both are exported to the Claude process and the runtime resolves one, so the outcome is not contractual. Provide the single credential you want to authenticate and bill against; do not rely on a particular one taking priority. This repo's workflow wires both inputs through, so a consumer supplies only the secret for the method it uses. (The `consumer-health` check reports all four so a misconfigured repo surfaces early — that is a health signal, not a hard requirement to set both Claude secrets.)

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
    uses: ignite-corp/ai-dev-pr-review/.github/workflows/base-ai-review-orchestrator.yml@v1
    with:
      pr_number: ${{ inputs.pr_number || '' }}
      code-review-system-prompt-path: .github/prompts/code-review-system.md
      code-review-checklist-path: .github/prompts/code-review-checklist.md
    secrets:
      OPENAI_API_KEY: ${{ secrets.OPENAI_API_KEY }}
      GOOGLE_AI_API_KEY: ${{ secrets.GOOGLE_AI_API_KEY }}
      CLAUDE_CODE_OAUTH_TOKEN: ${{ secrets.CLAUDE_CODE_OAUTH_TOKEN }}
      ANTHROPIC_API_KEY: ${{ secrets.ANTHROPIC_API_KEY }}
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

### Option 2 — Specific version pin (`@v1.0.15`)

```yaml
uses: ignite-corp/ai-dev-pr-review/.github/workflows/base-ai-review-orchestrator.yml@v1.0.15
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
uses: ignite-corp/ai-dev-pr-review/.github/workflows/base-ai-review-orchestrator.yml@v1.0.15
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
| `CLAUDE_MODEL` | `claude-sonnet-4-6` | Model passed to `anthropics/claude-code-action` via `claude_args --model`. |
| `CODEX_MODEL` | `gpt-5.5` | Model passed to `codex exec --model`. |
| `GEMINI_MODEL` | `gemini-2.5-pro` | Model passed to the `google-genai` client. |
| `BOT_LOGIN` | `github-actions[bot]` | Author login used for minimizing prior bot comments and dismissing stale reviews. |
| `JACCARD_THRESHOLD` | `0.6` | Token-set Jaccard similarity threshold for dedup. Lower values dedup more aggressively (more strings collapse to same issue), higher values are stricter. Tune `0.5`-`0.8` for behavior trade-off. |
| `ALLOW_AUTO_APPROVE` | `false` | Killswitch gating **all** formal review events. When `false`, both "approve" and "request_changes" verdicts are posted as plain comments (no `gh pr review --approve` or `--request-changes` submitted). Flip to `true` to enable real `gh pr review --approve` and `--request-changes` (Changes Requested) events. |
| `REVIEWER_APP_ID` | _(unset)_ | App ID of a dedicated reviewer GitHub App. When set (and the `REVIEWER_APP_PRIVATE_KEY` secret is configured), the aggregate mints an App installation token and submits a real APPROVED review on `approve` verdicts. `github-actions[bot]` cannot approve PRs, so without this the `approve` verdict falls back to a plain comment — the current default behavior. Optional and fully backward compatible. |

## Concurrency and re-push behavior

The orchestrator sets `concurrency: { group: ai-review-<pr-number>, cancel-in-progress: true }`, so each PR has at most one active review run at a time. The group key is the PR number (`inputs.pr_number` for `workflow_dispatch`, falling back to `github.run_id`).

- **New push during a review:** every push fires `pull_request: synchronize`, which starts a new run and cancels the in-progress run for the same PR. The new run restarts from `prepare` against the latest diff. Runs do not accumulate — the PR converges to a single active run.
- **Different PRs:** different group keys, so they run independently and never cancel each other.
- **`sequential` trade-off:** because reviewers run one after another (`Claude -> Codex -> Gemini`), a run takes longer wall-clock than `parallel`, so a re-push is more likely to land mid-run. Cancellation discards already-completed stages (e.g. a finished Claude review) and the new run re-runs the chain from the start. `parallel` wastes less work on rapid successive pushes.
- **Manual `workflow_dispatch`:** pass `pr_number` so the group key matches the PR. Without it the key falls back to `github.run_id`, which is unique per run, so concurrent manual runs are not de-duplicated.

## Cross-org usage

GitHub does NOT propagate `secrets: inherit` across organizations. For `ignite-pilot-org` (or any other org) consumers:

1. Org admin: configure `OPENAI_API_KEY`, `GOOGLE_AI_API_KEY`, `CLAUDE_CODE_OAUTH_TOKEN`, `ANTHROPIC_API_KEY` as org-level secrets and grant the consumer repos access.
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

Dependabot for `github-actions` covers reusable workflow refs (e.g. `uses: ignite-corp/ai-dev-pr-review/.github/workflows/base-ai-review-orchestrator.yml@v1.0.15`) in addition to plain action refs. Adjust `interval` to `daily` for faster pickup or `monthly` for less PR noise.

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

The per-repo system prompt and checklist live in the consumer repo, NOT here. The reusable workflow reads them via `code-review-system-prompt-path` / `code-review-checklist-path` and concatenates them into the `context.md` that every reviewer (Claude / Codex / Gemini) reads as its shared guideline. To customize:

1. Copy the starter templates from this repo's `examples/prompts/` into the consumer's `.github/prompts/`:
   - `code-review-system.md` — baseline **review disposition** (pair each finding with a concrete `suggestion`; no opposition-for-opposition nitpicks; still raise material flaws of any severity even when the fix is non-trivial) plus inline `<...>` placeholders (project identity, architecture layers) to fill in.
   - `code-review-checklist.md` — baseline code-quality / security / spec-compliance checklist.
2. Edit them with repo-specific rules (architecture conventions, naming taboos, security expectations). Tune the review disposition here — all three reviewers read these files as their shared guideline.
3. Reference them from the thin trigger only if you use a non-default path:
   ```yaml
   with:
     code-review-system-prompt-path: my/custom/path/system.md
   ```
4. Commit the prompts to the consumer's BASE branch. The `prepare` workflow always reads from the base branch, never the PR head, to prevent prompt injection — so changes take effect on the next PR after they merge.

### system prompt vs checklist — how to author each

The two files play different roles in the concatenated `context.md`:

| | `code-review-system.md` | `code-review-checklist.md` |
|---|---|---|
| Role | **How to judge** — persona, policy, severity rubric | **What to check** — enumerated pass/fail items |
| Form | Prose + tables | `- [ ]` bullets |
| Holds | Review disposition, the three perspectives, severity meanings, output contract, SHA-pin / dedup / Dependabot rules, repo architecture & security expectations | Concrete, binary checks under Code Quality / Security / Spec Compliance |

- **system.md** — write *how the reviewer should think and decide*. Keep the org-standard sections; customize only the two `<...>` lines (project identity, architecture layers) plus repo-specific architecture / naming / security expectations. Severity semantics and the review disposition belong here.
- **checklist.md** — write short, scannable, **binary** items ("no function over 80 lines", "parameterized queries only", "JWT validation present"). No judgment or philosophy — that lives in system.md. Reference the disposition with one line rather than restating it.
- **Don't duplicate.** Policy / disposition / severity → system.md only. Enumerated checks → checklist.md only. Restating the same rule in both invites drift and contradiction.

## Severity icons

The aggregate verdict comment and inline reviewer comments use single-character ASCII severity indicators:

| Severity | Icon |
|---|---|
| critical | `!` |
| major | `+` |
| minor | `-` |
| suggestion | `?` |

This is a deliberate ASCII-only choice for the public repo. Consumers that want richer icons (emoji) can fork or open a PR to make `SEVERITY_ICONS` configurable.

## PR response skill (Claude Code)

The reviewer posts findings; a developer still has to work each PR through the review → fix → merge cycle. The canonical `pr-response-cycle` Claude Code skill lives here at [`.claude/skills/pr-response-cycle/`](.claude/skills/pr-response-cycle/SKILL.md). It drives a PR through the project's 10-step checklist: bulk-classify review threads (Fixed / Deferred / Won't fix / Duplicate / Outdated), post evidence-based replies, manage all three timeline item types (threads + issue comments + review bodies), apply fixup-rebase for review-driven changes, navigate merge state (CLEAN / BLOCKED / BEHIND / DIRTY), and merge with a merge commit (never squash) when policy allows.

This repo doubles as a Claude Code **plugin marketplace**, so consumer repos can install the skill by reference and auto-follow updates without any push into the consumer repo.

**Option A - install from the marketplace (recommended, auto-follows updates)**

In Claude Code:

```
/plugin marketplace add ignite-corp/ai-dev-pr-review
/plugin install pr-response-cycle@ai-dev-pr-review
```

Third-party marketplace auto-update is OFF by default. Enable it once via `/plugin` -> Marketplaces -> toggle auto-update for `ai-dev-pr-review`. After that, updates arrive at each Claude Code startup - no push, no write access into your repo, and no per-consumer targeting. Because the plugin ships without a pinned `version`, every commit here becomes a new version (SHA-based auto-follow, the closest analog to a workflow `@v1`).

**Option A2 - zero-config activation via committed settings (no `/plugin` commands)**

To auto-enable the skill for *everyone* working in a consumer repo with no per-user setup, commit [`examples/consumer-claude-settings.json`](examples/consumer-claude-settings.json) into that repo as `.claude/settings.json`:

```json
{
  "extraKnownMarketplaces": {
    "ai-dev-pr-review": { "source": { "source": "github", "repo": "ignite-corp/ai-dev-pr-review" } }
  },
  "enabledPlugins": { "pr-response-cycle@ai-dev-pr-review": true }
}
```

Result: on first open of the repo, the user gets a single folder-trust prompt. After they accept, Claude Code auto-adds the `ai-dev-pr-review` marketplace, installs and enables the `pr-response-cycle` plugin, and auto-follows upstream updates from this repo - with no `/plugin marketplace add` / `/plugin install` commands and no skill files copied into the consumer repo (the skill is referenced from the marketplace, not vendored). The skill then invokes as `/pr-response-cycle`.

If the repo already has a `.claude/settings.json`, merge these two keys into it rather than replacing the file, preserving any existing settings.

**Caveat:** the one folder-trust prompt on first open is unavoidable - it is part of Claude Code's workspace-trust model and fires before committed settings are applied. It cannot be pre-approved or suppressed through any managed setting: Claude Code has no setting to persist folder trust, and only fully non-interactive `-p` mode skips the prompt. So one trust click per repo per machine is the irreducible minimum for interactive use.

### Org-wide auto-update (admins)

Options A and A2 still leave third-party marketplace auto-update OFF by default, so each user has to enable it once (see the per-user toggle in Option A). An org admin can make auto-update automatic for the whole fleet - with no per-user toggle - and force-enable the plugin org-wide through **enterprise-managed settings** (the org-deployed `managed-settings.json`). The complete block:

```json
{
  "extraKnownMarketplaces": {
    "ai-dev-pr-review": {
      "source": { "source": "github", "repo": "ignite-corp/ai-dev-pr-review" },
      "autoUpdate": true
    }
  },
  "enabledPlugins": { "pr-response-cycle@ai-dev-pr-review": true },
  "strictKnownMarketplaces": [
    { "source": "github", "repo": "ignite-corp/ai-dev-pr-review" }
  ]
}
```

What each key does:

- `autoUpdate: true` makes upstream updates arrive automatically for the whole fleet with no per-user toggle. It is only honored on an `extraKnownMarketplaces.<name>` entry in managed settings; it is **silently ignored** in a project-scoped `.claude/settings.json` (the Option A2 file), so do NOT add it there.
- `enabledPlugins` force-enables the plugin org-wide. It does NOT auto-install: the first install still happens on folder-trust via the committed project `.claude/settings.json` (Option A2), so this key does not by itself eliminate that step.
- `strictKnownMarketplaces` is an allowlist of the marketplaces users may add, restricting plugin sources to the entries listed here.

The folder-trust prompt cannot be pre-approved or suppressed by any of these keys - trust pre-approval is not a supported managed setting (see the Option A2 caveat above).

**Deployment methods:**

- **Server-managed (recommended)** - push the block from the claude.ai admin console so it deploys to the fleet without touching each machine's filesystem. Requires Claude Code Teams v2.1.38+ or Enterprise v2.1.30+.
- **File-based** - write `managed-settings.json` to the OS-specific system path below.
- **MDM** - deliver the file via device management (macOS configuration profile / plist, or Windows registry under HKLM). Anthropic publishes no ready-made MDM profile, so you author the payload yourself.

Deploy `managed-settings.json` to the OS-specific system path (confirmed from Claude Code docs -> Settings -> managed settings):

| OS | Path |
|---|---|
| macOS | `/Library/Application Support/ClaudeCode/managed-settings.json` |
| Linux / WSL | `/etc/claude-code/managed-settings.json` |
| Windows | `C:\Program Files\ClaudeCode\managed-settings.json` |

**Residual manual steps that cannot be eliminated:**

1. One folder-trust prompt per repo per machine (interactive use); no managed setting can pre-approve it.
2. First plugin install is handled by the committed project `.claude/settings.json` (Option A2) on folder-trust, not by managed settings.
3. Each user still authenticates to the org.

**Security note:** trusting a folder auto-loads that repo's settings, hooks, MCP servers, and skills - a code-execution surface. Keep the trust prompt as the human gate, and layer `strictKnownMarketplaces` (allowlisted sources) plus `permissions.deny` to constrain what a trusted repo can do.

Tracked for deployment in AT-1476.

**Option B - manual copy (fallback)**

Copy the skill folder to one of:

```bash
# per-repo (available to everyone working in that repo)
cp -RL .claude/skills/pr-response-cycle <consumer-repo>/.claude/skills/

# or per-developer (available everywhere for you)
cp -RL .claude/skills/pr-response-cycle ~/.claude/skills/
```

The canonical copy lives under `plugins/pr-response-cycle/skills/pr-response-cycle/`; `.claude/skills/pr-response-cycle` is a symlink to it, so `-L` (follow symlinks) resolves the real files when copying.

Then invoke `/pr-response-cycle` in Claude Code, or just say "process the review" / "PR 리뷰 처리" with a PR number. Project policy in the repo's `~/.claude/projects/<cwd>/memory/` overrides the skill's defaults where they conflict.

## Contributing

Open issues and PRs against this repo. CI / tests for the public repo are out of scope for v1.0.0; see `CONTRIBUTING.md` (TBD) once it exists.

## License

See `LICENSE` (decision pending — review `LICENSE_RECOMMENDATION.md`).
