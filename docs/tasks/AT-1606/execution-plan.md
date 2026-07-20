# AT-1606 Execution Plan — usage-based auth auto-switch (subscription OAuth → billed API)

Jira: https://ignitecorp.atlassian.net/browse/AT-1606
Relates: AT-1512 (rolled back — model axis), AT-1499/AT-1502 (auth precedence composite + empty-secret toggle), AT-1601 (billed API path verified working)

When a Claude review hits the subscription (OAuth) usage limit, automatically switch **only the repo that hit the limit** to the billed `ANTHROPIC_API_KEY` path for subsequent reviews, then automatically revert to the subscription once the limit resets. This is "AT-1512 done right": AT-1512 downgraded the model (opus → sonnet), which does not bypass the Team-plan single weekly OAuth bucket; switching the **auth** to the billed API is a separate billing path that genuinely bypasses OAuth exhaustion (billed path confirmed working in AT-1601). The detection-script + restore-cron mechanism from AT-1512 is reused; only the switched dimension changes from model to auth.

---

## 0. Investigation findings (origin/main, file:line)

These findings anchor the plan. Every file:line below is on `origin/main`.

### 0-1. The crux — where the var gate is evaluated

**Decision: the gate is evaluated in the CALLER workflow and passed to the composite as a new `force_api` input. It is NOT read inside the composite.**

- The composite `.github/actions/claude-review/action.yml` references **only `inputs.*`** — never `vars.*` or `secrets.*` (confirmed by grep; `action.yml:60-71`). A composite action cannot see the caller's `vars`/`secrets` context; GitHub only exposes those to workflow files, and they reach a composite exclusively as explicit inputs.
- The existing model wiring proves the pattern: the caller `base-ai-review-single.yml:204` reads `vars.CLAUDE_MODEL` and passes it as the composite's `model:` input; the composite consumes `${{ inputs.model }}` (`action.yml:70`). The composite never reads `vars.CLAUDE_MODEL` itself.
- Therefore the team-lead draft "add `vars.CLAUDE_FORCE_API` inside the composite auth expression" is **not implementable as stated** — that variable is invisible inside the composite. AT-1512 §4-1 rejected a composite-internal approach for the same reason.
- **Resolution:** add a `force_api` input to the composite; each caller (`single.yml`, and the pilot `wrapper.yml`) evaluates `vars.CLAUDE_FORCE_API` in its own workflow context and forwards it as `force_api`.

The current auth expression (`action.yml:68`):

```yaml
anthropic_api_key: ${{ inputs.claude_code_oauth_token == '' && inputs.anthropic_api_key || '' }}
```

Target auth expression (adds the `force_api` term):

```yaml
anthropic_api_key: ${{ (inputs.claude_code_oauth_token == '' || inputs.force_api == 'true') && inputs.anthropic_api_key || '' }}
claude_code_oauth_token: ${{ inputs.force_api == 'true' && '' || inputs.claude_code_oauth_token }}
```

The second line is the symmetric half: when `force_api == 'true'`, the OAuth token is blanked so the CLI does not prefer it (per AT-1499, `ANTHROPIC_API_KEY` takes CLI precedence only when OAuth is absent). Passing the API key alone is not sufficient if OAuth is still passed — both must be handled.

### 0-2. Callers of the composite

- Only in-repo caller: `base-ai-review-single.yml:194-205` (`uses: ./.ai-dev-pr-review/.github/actions/claude-review`). `ci.yml`, `orchestrator`, and `aggregate` do not call the composite directly.
- Pilot path (out of this repo): consumer → `ignite-pilot-org/ai-dev-pr-review-wrapper` `wrapper.yml` → base `orchestrator` → `single.yml` → composite (`docs/pilot-usage.md:8-20`). AT-1512 established that the wrapper has its own `Run Claude review` step that `uses` the composite via the local `.ai-dev-pr-review` checkout, so the wrapper needs the same `force_api` input + detection post-step added separately. The pilot org has no org-level variables (`docs/pilot-usage.md`), so pilot `CLAUDE_FORCE_API` lives as a per-repo var.

### 0-3. Detection point

- The composite's inner step is `continue-on-error: true` (`action.yml:51`) and exposes `execution_file` (`action.yml:40-43`); on a usage-limit event the step exits abnormally but the execution log is still handed to the caller.
- `single.yml` already reads that file at `Extract Claude review from execution log (fallback)` (`single.yml:207-221`, `EXEC_FILE: ${{ steps.claude-review.outputs.execution_file }}`). The detection post-step is added **after** this fallback step, gated `if: inputs.reviewer == 'claude' && always()` — exactly the shape AT-1512 used (see reverted commit `3807e2a`, `base-ai-review-single.yml` post-steps).
- **Aggregate cannot detect**: because the composite swallows the error, the claude job reports success and the execution log is not uploaded as an artifact, so `aggregate` never sees it. Detection must run in single/wrapper.

### 0-4. Reset-epoch parsing (reuse AT-1512)

AT-1512's `scale_claude_model.py` (reverted commit `3807e2a`) `_parse_limit_epoch`:
- Primary regex `Claude AI usage limit reached\|(\d+)` → capture reset epoch (last match wins).
- Fallback: case-insensitive `usage limit reached` with no epoch → conservative session reset `now + 5h`. Premature restore is self-correcting.
- No signature → do not switch.

There is **no subscription usage API** for the Team plan (confirmed this session), so detection is necessarily reactive — no proactive usage-percentage check is possible.

### 0-5. App token mint + Variables:write permission

- Mint pattern reused from `base-ai-review-aggregate.yml:105-113` (`actions/create-github-app-token@d72941d…` v1.12.0, `app-id: ${{ vars.REVIEWER_APP_ID }}`, `private-key: ${{ secrets.REVIEWER_APP_PRIVATE_KEY }}`). AT-1512 minted **two** owner-scoped tokens (`owner: ignite-corp` and `owner: ignite-pilot-org`) because the Reviewer App is installed on both orgs.
- **Variables:write is NOT currently granted.** The Reviewer App (`ignite-ai-review-approver`, app_id 4196135) currently holds `contents:write, metadata:read, pull_requests:write` (`docs/case-study/session-log-2026-07-02-reviewer-app-auto-approve.md:20,27` records only `contents:write`/`pull_requests:write`; no Variables). AT-1512 §5.1 confirms the same "Variables 권한 없음" state. Granting `Variables: Read and write` (repo + org) on both installations is a **manual prerequisite** (Phase 4).
- `secrets.REVIEWER_APP_PRIVATE_KEY` already exists as an ignite-corp org secret (visibility=all); pilot repos have it too.

### 0-6. Consumer list var + public-repo logging

- AT-1512's list var `MODEL_SCALE_REPOS` was deleted on rollback (commit `b4d0644`). A fresh private org var **`AUTH_SWITCH_REPOS`** (JSON array of repo full-names) is created for the cron. `RULESET_CONFIG` enumerates only ignite-corp consumer repos (not pilot), so it is not reusable here.
- Public-repo log policy: this repo is PUBLIC, so pilot repo names must never appear in Actions logs. Reuse the index-only convention — `echo "OK: entry ${i}/${total}"` — from `ruleset-sync.yml:2-12,57-72` and `ruleset-audit.yml:1-9,46-76`. The new script/cron must log by index only.

---

## Phased plan

Each phase lists concrete files. Phases 1-3 are code (delivered as PRs); Phase 4 is manual ops + rollout. Sequencing and which release activates what is in §Sequencing.

### Phase 1 — composite `force_api` gate + release

Goal: the composite can be told to force the billed API path via an input, without breaking the existing OAuth-preferred and empty-secret behaviors.

Files:
- `.github/actions/claude-review/action.yml`
  - Add input `force_api` (`required: false`, `default: 'false'`).
  - Change the auth expression per §0-1 (both the `anthropic_api_key` and `claude_code_oauth_token` lines).
- `.github/workflows/base-ai-review-single.yml`
  - In the `Run Claude review` step (`:194-205`) add `force_api: ${{ vars.CLAUDE_FORCE_API || 'false' }}`.
- Tests: an expression-matrix test (unit or a documented actionlint-verified table) covering:
  - OAuth set + `force_api=false` → OAuth (no API key passed).
  - OAuth set + `force_api=true` → API (API key passed, OAuth blanked).
  - OAuth empty (+ any `force_api`) → API (existing empty-secret path, unchanged).
  - `force_api` unset → OAuth (backward compatible default).

Release: bump the `ref:` literal in `single.yml` (and `aggregate` if co-released) to the new tag, tag it, then move `v1` via `move-major-tag.yml`. This activates the input plumbing. Until any caller sets `CLAUDE_FORCE_API=true`, behavior is identical to today.

### Phase 2 — detection post-step (single.yml + wrapper + standalone paths)

Goal: on a usage-limit event, set `CLAUDE_FORCE_API=true` + `CLAUDE_FORCE_API_UNTIL=<epoch>` on the current run's repo var.

Files:
- `.github/scripts/switch_claude_auth.py` (new) — two modes, shape modeled on AT-1512 `scale_claude_model.py`:
  - `detect-and-switch`: read `EXEC_FILE`; `_parse_limit_epoch`; on detection, PATCH the **current repo's** `CLAUDE_FORCE_API=true` and `CLAUDE_FORCE_API_UNTIL=<epoch>` repo vars. Scope is per-repo (unlike AT-1512's org+all-pilots fan-out). Fail-safe: any error → log to stderr, exit 0. Index-only logging; `DRY_RUN` support.
  - `restore-if-due`: used by Phase 3 cron.
- `.github/scripts/tests/test_switch_claude_auth.py` (new) — parser cases (a)-(d) per AT-1606 §6.1.
- `.github/workflows/base-ai-review-single.yml`
  - Add `REVIEWER_APP_PRIVATE_KEY` (optional) to the `secrets:` block.
  - After the fallback extract step (`:207-221`), add the AT-1512-shaped post-steps: check-key-present → mint App token (gated `vars.REVIEWER_APP_ID != ''` + key present) → run `switch_claude_auth.py detect-and-switch` with `EXEC_FILE`, `GH_TOKEN` (owner-scoped for the current repo's org), `continue-on-error: true`, `if: inputs.reviewer == 'claude' && always()`.
- `.github/workflows/base-ai-review-orchestrator.yml`
  - Forward `REVIEWER_APP_PRIVATE_KEY` to the `review-claude-p` and `review-claude-s` jobs (AT-1512 added exactly these two lines; currently forwarded only to `aggregate`).
- Pilot `wrapper.yml` (separate repo, out of this PR set) — the same `force_api` input (Phase 1) + detection post-step. Tracked as a wrapper-side change; the script is shared automatically via the wrapper's pinned `.ai-dev-pr-review` checkout.
- Standalone path: there is no in-repo standalone Claude step beyond `single.yml` (§0-2). "Standalone" reduces to `single.yml`; no separate wiring needed.

Note on repo-var scope: for the current repo, the mint owner is the repo's org (ignite-corp for base consumers, ignite-pilot-org for pilot). Because `single.yml` runs inside each consumer's own org context, minting a single owner-scoped token for `github.repository`'s owner is sufficient for the per-repo PATCH. (Contrast Phase 3, which fans out across both orgs and mints two tokens.)

### Phase 3 — revert cron + list var

Goal: hourly, restore any repo whose `CLAUDE_FORCE_API_UNTIL` epoch has passed back to the subscription.

Files:
- `.github/workflows/claude-auth-restore.yml` (new) — hourly cron, shape from AT-1512 `claude-model-restore.yml` (reverted `3807e2a`): `on.schedule` hourly + `workflow_dispatch`; mint corp + pilot App tokens; run `switch_claude_auth.py restore-if-due` with `AUTH_SWITCH_REPOS`.
- `switch_claude_auth.py` `restore-if-due`: for each repo in `AUTH_SWITCH_REPOS`, GET `CLAUDE_FORCE_API_UNTIL`; if present and `now > epoch`, DELETE both `CLAUDE_FORCE_API` and `CLAUDE_FORCE_API_UNTIL` (delete = revert to subscription; no default value to restore, unlike the model axis). Index-only logging.
- Manual prereq (Phase 4): create private org var `AUTH_SWITCH_REPOS`.

### Phase 4 — App Variables:write grant (manual) + rollout

Manual (USER):
1. Grant the Reviewer App `Variables: Read and write` (Repository permissions) + `Variables (organization): Read and write` (Organization permissions); approve on both installations — ignite-corp (inst 143863091) and ignite-pilot-org (inst 143863152).
2. Create private org var `AUTH_SWITCH_REPOS` (ignite-corp, visibility selected/private) = JSON array of managed repo full-names.
3. Ensure each target repo is in the `ANTHROPIC_API_KEY` selected-secret list and the key is ENABLED at the Anthropic console (currently may be toggled off — AT-1499/1502).

Rollout: land Phase 1-3 PRs and release; complete the manual grants; observe a real limit event flipping a target repo to the API path and the cron reverting it after reset.

---

## Sequencing (which release activates what)

1. **Release A (Phase 1):** composite `force_api` input + `single.yml` forwards `vars.CLAUDE_FORCE_API`. Safe no-op until a `CLAUDE_FORCE_API=true` var exists anywhere. Ship first so the gate exists before anything writes to it.
2. **Release B (Phase 2 + 3):** detection post-step + `switch_claude_auth.py` + `orchestrator` secret forward + cron. Detection writes `CLAUDE_FORCE_API` only after the App has Variables:write; before the grant, the mint/PATCH fail-safes to a no-op (the review still runs on OAuth). The cron similarly no-ops until the grant + `AUTH_SWITCH_REPOS` exist.
3. **Manual (Phase 4):** grant Variables:write + create `AUTH_SWITCH_REPOS` + verify `ANTHROPIC_API_KEY` enabled. This is the switch that makes the loop live end-to-end.
4. **Wrapper release:** add `force_api` + detection post-step to the pilot `wrapper.yml`, released from its own repo pinned to the new upstream tag.

Rationale for A-before-B: the gate input must exist and be released before any automation can meaningfully set the var (a var read by a caller that has not shipped Phase 1 would do nothing). Keeping the manual grant last means Phases 1-3 can merge and release while inert, minimizing the window where a half-configured system could misbehave.

---

## Test strategy

- Unit (`.github/scripts/tests/test_switch_claude_auth.py`): `_parse_limit_epoch` cases — `…reached|<epoch>` → epoch; epoch-less variant → `now+5h`; no-limit log → None; multi-message log → last match wins. Follow `test_scale_claude_model.py` / `test_extract_codex_json.py` style.
- Composite auth-expression matrix (Phase 1 §): four combinations of OAuth-present × `force_api`.
- `actionlint` on all changed/new workflows.
- Index-only logging check: assert no pilot repo name is echoed by the new script/cron (only `entry i/total`), matching `ruleset-sync.yml` / `ruleset-audit.yml`.
- `DRY_RUN=true` mode logs intended PATCH/DELETE targets (by index) without calling the API.
- Live observation on a real limit event: target repo var flips to API + `_UNTIL` set; after epoch, cron deletes both vars.

---

## Rollback plan

- **Code:** revert the Phase-1/2/3 merge commits (AT-1512 rollback precedent — clean revert of the feat merge). Re-release with the `ref:` literal pointed back to the prior tag; move `v1`.
- **Vars:** delete any lingering `CLAUDE_FORCE_API` / `CLAUDE_FORCE_API_UNTIL` repo vars and the `AUTH_SWITCH_REPOS` org var. A one-shot `restore-if-due` run (or manual deletion) reverts all switched repos to subscription.
- **Permission:** the Variables:write grant can be left in place or revoked; with the code reverted it is inert.
- The design is self-correcting: if the cron is disabled mid-flight, switched repos stay on the billed API (reviews still succeed, just billed) until the var is removed — no review outage.

---

## 채택하지 않은 대안

- **var-only gate inside the composite** (team-lead draft, literal reading): put `vars.CLAUDE_FORCE_API` directly into the composite auth expression. **Rejected — not implementable.** A composite action cannot read caller `vars` (§0-1); the variable is invisible inside `action.yml`. The only way the composite can act on it is via an input, which is exactly the adopted design (caller evaluates the var, forwards `force_api`).
- **secret-only toggle (no var, no automation):** keep only the existing empty-`CLAUDE_CODE_OAUTH_TOKEN` manual toggle. **Rejected for the automation goal** — a secret cannot be written by the detection post-step without secrets:write (higher privilege and no per-repo automation ergonomics), and secrets cannot be read in `if:` conditions. The empty-secret path is nonetheless **kept** as the manual operator escape hatch (hybrid design, AT-1606 §3.1); the var gate is additive.
- **global-scope switch (flip the org var / all repos at once):** on detection, set `CLAUDE_FORCE_API` org-wide like AT-1512 flipped `CLAUDE_MODEL` everywhere. **Rejected** — over-switches repos that have not individually hit the limit, moving their billing to the API unnecessarily; and pilot org has no org var, so a global org flip would not reach pilots anyway. Per-repo lazy switching bills only the repos that actually exhausted their share and self-limits blast radius. (The team-lead design point #3 already fixes this to per-repo; recorded here for completeness.)
