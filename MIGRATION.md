# Per-consumer migration checklist

After `ignite-corp/ai-dev-pr-review` is created and tagged `v1.0.0`, open one PR per consumer. Use the matching section below.

## Common preconditions (all consumers)

1. Org or repo secrets present: `OPENAI_API_KEY`, `GOOGLE_AI_API_KEY`, `CLAUDE_CODE_OAUTH_TOKEN`.
2. Repo vars (optional -- every one has a working default, so a consumer can migrate without setting any). These are read inside the reusable workflows, so they appear neither in the thin trigger's `with:` block nor anywhere else in the consumer's tree. Commonly set: `PR_SIZE_LIMIT`, `REVIEW_MODE`, `ALLOW_AUTO_APPROVE`. For the complete set with defaults and effects see [Runtime configuration via `vars.*`](README.md#runtime-configuration-via-vars) -- that table is the single list and is deliberately not copied here. Set them under `Settings -> Secrets and variables -> Actions -> Variables`.
3. `.github/prompts/code-review-system.md` and `.github/prompts/code-review-checklist.md` remain in the consumer repo. Do NOT delete.
4. Branch protection on the consumer repo's main branch still requires the "AI Code Review" check — verify the new thin trigger's job name matches the protected check name, or update the branch protection rule alongside the migration PR.
5. The thin trigger's `pull_request` carries no `branches:` filter, so a review round runs on PRs to every base branch, not only `main` -- more model spend and more PR comments than a base-filtered trigger. This is deliberate: a base filter leaves the required check absent on a PR based on a topic branch (AT-2412). To cut volume, exclude paths with [`.github/lens-ignore`](README.md#excluding-paths-from-review-githublens-ignore). It skips a round only when every changed file is excluded: the reviewers do not run and the check still reports success as `skipped (policy)`. On any other PR it only shrinks the diff they read, and all reviewers still run. Do not use a workflow-level `paths:` / `paths-ignore:` filter instead: a filtered-out PR gets no check at all, which brings the absent check back.

---

## Upgrading an already-onboarded consumer

Bumping the reusable-workflow pin does **not** update a consumer's prompts. Precondition 3 above keeps `.github/prompts/code-review-system.md` and `.github/prompts/code-review-checklist.md` in the consumer repo, and the per-consumer **Keep** lists repeat it: those two files are consumer-owned and are only ever changed by a PR in the consumer. `examples/prompts/code-review-system.md` in this repo is the template for a *new* onboarding, so a change to it reaches nobody already onboarded. When a release changes what the reviewer is told, the consumer needs its own PR re-copying the changed rules out of the template.

A consumer whose prompt still predates a rule keeps obeying the old one, and the reviewer then has two contradictory instructions for the same input: the blurb this repo's `verify_action_shas.py` renders into `context.md` travels with the pin and is current, while the consumer's prompt is whatever was last copied.

### Action SHA pin 404 rules (AT-2801)

Re-copy **both** of these bullets from the `## GitHub Actions SHA Pins` section of [`examples/prompts/code-review-system.md`](examples/prompts/code-review-system.md) into the consumer's `.github/prompts/code-review-system.md`, replacing the single `repo-not-found` bullet that is there now:

- `status="unverified"` with `error="repo-not-found"`
- `status="unverified"` with `error="repo-not-visible"` (new label)

**`error="repo-not-found"` no longer means the repository does not exist.** It now means only that the API answered 404 for a repository whose owner is not the owner of the repository under review, or the run's own owner could not be determined. GitHub answers 404 both for an absent repository and for a private one the run's token may not read, so the label asserts nothing about existence. Neither 404 bullet earns a `major` any more: both raise `suggestion` and ask the author to confirm the `owner/repo` spelling. A prompt left on the old bullet — "the upstream action repository does not exist … Raise as `major`" — produces a consensus `major` on every PR that pins a private repository, which is the AT-2801 defect itself.

A consumer whose prompt carries no `## GitHub Actions SHA Pins` section at all needs no change: it never had the old rule, and the rendered `context.md` blurb is its only guidance either way.

### Prompt files must be non-empty regular files (AT-2520)

**Behaviour change, no consumer action unless a prompt is broken.** `prepare` now reads `code-review-system-prompt-path` and `code-review-checklist-path` from git objects, on every source the run reads, and accepts only a non-empty regular file (git mode `100644` / `100755`):

- an **empty or whitespace-only** prompt used to produce a review with an empty prompt section; it now fails `prepare` with `Empty review prompt`;
- a **symlink** at the prompt path used to be read through (on the base branch, the link target's *path* became the prompt; on the PR head, the linked file's contents did); it now fails `prepare` with `Unreadable review prompt` (git mode `120000`);
- a **directory** at the prompt path used to put a tree listing into the prompt; it now fails `prepare` the same way (git mode `040000`).

This rule does not change what happens to a missing prompt; where a missing prompt is read from is covered by the next entry (AT-3032). The local driver (`review_pr_local.py`) applies the same rule. To check a consumer before bumping the pin, run `git ls-tree origin/<base> -- .github/prompts/` and confirm both files are listed as `100644 blob` and are not empty.

### Cross-repository runs never read prompts from the PR head (AT-3032)

**Behaviour change for cross-repository targets only.** A cross-repository run (`target_repo` names another repository, as the LENS runner does) runs under the caller's secrets, so a PR-head copy of a prompt would let the PR author write the reviewer prompt. On such a run the order is now the base branch, then the caller's `default_prompts_path`; the PR head is never read.

- A cross-repository target whose prompts exist **only on its PR head** (a first-time onboarding PR) now fails `prepare` with `Missing review prompt`, unless the caller passes `default_prompts_path`. The LENS runner does, so its targets are reviewed against the runner's `default-prompts/` until the target's own prompts reach its base branch.
- Same-repository runs are unchanged: a prompt missing from the base branch is still taken from the PR head with an `Onboarding prompt fallback` warning, and the step fails when neither side has it. The local driver (`review_pr_local.py`) keeps that same-repository order.

No consumer action is needed. A consumer reviewed by the runner that wants its own prompts used merges them to its base branch.

### Finding the release that changed a rule

This change lowers the severity the prompt prescribes for a finding class, which the [version policy](README.md#tag-pinning) places in a MINOR (`v1.x.0`): it is consumer-visible behaviour, so **not** a patch, and it only ever relaxes, so it is not one of the breaking changes that would make it a MAJOR. Per the wrapper lockstep in that same section, the pilot wrapper cuts a matching MINOR. Release notes for such a tag must say which prompt rules changed and name this section, so a consumer reviewing a Dependabot pin bump can see that the bump alone is not the whole upgrade.

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
