# Per-consumer migration checklist

After `ignite-corp/ai-dev-pr-review` is created and tagged `v1.0.0`, open one PR per consumer. Use the matching section below.

## Common preconditions (all consumers)

1. Org or repo secrets present: `OPENAI_API_KEY`, `GOOGLE_AI_API_KEY`, `CLAUDE_CODE_OAUTH_TOKEN`.
2. Repo vars (optional, override defaults from README): `PR_SIZE_LIMIT`, `REVIEW_MODE`, `CRITICAL_THRESHOLD`, `DEPENDABOT_CRITICAL_THRESHOLD`, `MAJOR_CONSENSUS_OVERLAP`, `DEPENDABOT_MAJOR_CONSENSUS_OVERLAP`, `MAJOR_CONSENSUS_MIN`, `CLAUDE_MODEL`, `CODEX_MODEL`, `GEMINI_MODEL`, `BOT_LOGIN`, `ALLOW_AUTO_APPROVE`.
3. `.github/prompts/code-review-system.md` and `.github/prompts/code-review-checklist.md` remain in the consumer repo. Do NOT delete.
4. Branch protection on the consumer repo's main branch still requires the "AI Code Review" check — verify the new thin trigger's job name matches the protected check name, or update the branch protection rule alongside the migration PR.

---

## Consumer: ai-dev-cab

Repo: `ignite-corp/ai-dev-cab`

**Delete:**
- `.github/workflows/ai-review-orchestrator.yml`
- `.github/workflows/ai-review-prepare.yml`
- `.github/workflows/ai-review-single.yml`
- `.github/workflows/ai-review-aggregate.yml`
- `.github/scripts/aggregate_reviews.py`
- `.github/scripts/extract_claude_review.py`
- `.github/scripts/fetch_review_context.py`
- `.github/scripts/github_pr_support.py`
- `.github/scripts/post_inline_comments.py`
- `.github/scripts/review_gemini.py`
- `.github/scripts/verify_action_shas.py`
- `.github/scripts/collect_review_threads.sh`
- `.github/scripts/threads.jq`
- `.github/scripts/review_prompt.md`
- `.github/scripts/requirements.txt`
- `.github/scripts/.python-version`
- `.github/scripts/test_aggregate_reviews.py` (decision: leave or delete — see test policy)
- `.github/scripts/test_verify_action_shas.py` (same)
- `.github/scripts/test_schema_genai_compat.py` (same)
- `.github/schemas/review-schema.json`

**Add:**
- `.github/workflows/ai-review.yml` (copy from `examples/consumer-thin-trigger.yml` variant 1)

**Keep:**
- `.github/prompts/code-review-system.md`
- `.github/prompts/code-review-checklist.md`

**Secrets to verify:** Repo or `ignite-corp` org level — all three present.

**Vars to configure:** Already set; confirm `CLAUDE_MODEL`, `CODEX_MODEL`, `GEMINI_MODEL`, and `ALLOW_AUTO_APPROVE` align with the current values (export from existing repo settings before the migration).

**Test plan:**
1. Land the migration PR.
2. Open a follow-up dry-run PR that changes one trivial file.
3. Verify the new workflow triggers, prepare step uploads `review-context`, all three reviewer jobs run, aggregate posts the verdict comment.
4. Compare verdict comment format against a recent pre-migration PR. Severity icons change from emoji to ASCII (`!`, `+`, `-`, `?`) — confirm this is acceptable for cab.

---

## Consumer: qayak (ai-dev-qa-partner)

Repo: `ignite-corp/ai-dev-qa-partner`

Same delete/add/keep lists as `ai-dev-cab`.

**Secrets to verify:** Same three. The qayak repo currently has its own copy due to AT-1185's multi-PR rollout.

**Vars to configure:** Inspect `ai-dev-qa-partner` for any qayak-specific overrides (e.g., a higher `PR_SIZE_LIMIT` for monorepo-style PRs). Carry those over to the new repo-level vars; they continue to work because the centralized workflows read the same `vars.*` names.

**Test plan:**
1. Land the migration PR.
2. Open a follow-up dry-run PR.
3. Verify the workflow runs and posts the verdict. Cross-check that the qayak `.github/prompts/code-review-system.md` is still consulted (it should be — the path-based input defaults to it).

---

## Consumer: t2a (ai-tf-t2a)

Repo: `ignite-corp/ai-tf-t2a`

Same delete/add/keep lists as `ai-dev-cab`.

**Secrets to verify:** Same three.

**Vars to configure:** Same set. t2a is the Terraform repo — confirm whether the existing per-repo system prompt encodes Terraform-specific rules (HCL, module conventions); if so, no change needed because the prompt stays in the consumer.

**Test plan:**
1. Land the migration PR.
2. Open a follow-up dry-run PR that touches one `.tf` file.
3. Verify Codex's Terraform-awareness still triggers via the consumer's own system prompt.

---

## Consumer: infra-common (ai-dev-infra-common)

Repo: `ignite-corp/ai-dev-infra-common`

Same delete/add/keep lists as `ai-dev-cab`. Note: this repo is the architectural precedent for reusable workflows in the org (per AT-1210's "Reference" section). It still goes through the same migration even though it lives in the same org as the new public repo — keeping it consistent with other consumers simplifies the upgrade story.

**Secrets to verify:** Same three.

**Vars to configure:** Inspect for any per-repo overrides; infra-common may set a tighter `CRITICAL_THRESHOLD` to gate infra changes harder. Carry forward.

**Test plan:**
1. Land the migration PR.
2. Open a follow-up dry-run PR that changes a sample CloudFormation template.
3. Verify the verdict posts correctly.

---

## Consumer: generic new repo (e.g., ignite-pilot-org/<repo>)

Repo: any cross-org consumer that does NOT currently have the workflows.

**Prerequisites:**
1. Org admin: configure `OPENAI_API_KEY`, `GOOGLE_AI_API_KEY`, `CLAUDE_CODE_OAUTH_TOKEN` as org-level secrets in `ignite-pilot-org` (or the target org). Grant the consumer repo access.
2. Org admin: Settings -> Actions -> General -> Allow `ignite-corp/ai-dev-pr-review` in the actions allowlist. Permit the underlying actions listed in `examples/consumer-thin-trigger.yml` Variant 2.
3. Org admin: confirm the repo allows the `pull-requests: write` permission for `GITHUB_TOKEN` so the workflow can post review comments.

**Delete:** Nothing (greenfield).

**Add:**
- `.github/workflows/ai-review.yml` (copy from `examples/consumer-thin-trigger.yml` variant 1 — the YAML is identical for cross-org, only the prep work differs)
- `.github/prompts/code-review-system.md` — write a repo-specific system prompt (or use a minimal generic stub for the first pass)
- `.github/prompts/code-review-checklist.md` — same

**Vars to configure:** Set the default set (`CLAUDE_MODEL`, etc.) under repo or org variables. Defaults baked into the reusable workflow are sensible — only override what's specific.

**Test plan:**
1. Land the workflow PR + prompts PR (can be combined).
2. Open a dry-run PR.
3. Watch the workflow run. Common cross-org failures to watch for:
   - "secrets unavailable" -> org secret not granted to this repo
   - "anthropics/claude-code-action denied by allowlist" -> add it to the org Actions allowlist
   - "permission denied for pull-requests write" -> adjust `permissions:` in the thin trigger or in repo settings
4. Verify the verdict comment posts.

---

## Rollback plan (any consumer)

If the migration PR misbehaves in production:

1. Revert the migration PR. This re-introduces the local `ai-review-*.yml` + scripts.
2. The reverted state is functionally identical to the pre-migration state (no behavior change inside the consumer's prompts or vars), so no further cleanup is needed.
3. File a follow-up bug against `ignite-corp/ai-dev-pr-review` describing the failure mode. Address there, cut a `v1.0.x` patch, then re-attempt the migration.
