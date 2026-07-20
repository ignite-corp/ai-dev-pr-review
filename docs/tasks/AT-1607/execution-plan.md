# AT-1607 Execution Plan — Claude OAuth token sequential rotation (N≤4 separate subscriptions)

Jira: https://ignitecorp.atlassian.net/browse/AT-1607
Depends on (is blocked by): AT-1606 (OAuth→API auto-switch — the final fallback layer this feature composes with)
Relates: AT-1499/AT-1502 (auth precedence composite + empty-secret toggle), AT-1512 (rolled back — reused detect/cron pattern + same-team-bucket trap), AT-1601 (billed API path verified)

When a Claude review hits the usage limit on the **current** OAuth token, rotate to the **next** OAuth token (from a separate subscription with its own weekly bucket); only when **all** managed OAuth tokens are exhausted does it fall through to the billed `ANTHROPIC_API_KEY` path — and that final fallback **is AT-1606**, not re-implemented here. Each token's reset epoch (`|<epoch>` from the limit error) is tracked so an exhausted token is skipped until its reset, then rotated back in. This is a **token-selection layer that sits above** AT-1606's OAuth→API boolean switch.

Two-stage precedence per review run:

```
ladder (pick OAuth token 1..N)  →  AT-1606 force_api gate (all exhausted → billed API)
```

---

## 0. Investigation findings (origin/main, file:line)

Every file:line below is on `origin/main`. AT-1606's own findings (`docs/tasks/AT-1606/execution-plan.md`) are the foundation; this plan cites only what AT-1607 adds or depends on.

### 0-1. The crux — dynamic token selection is NOT expressible in GHA; use a static ladder in the caller

**Decision: adopt option (a) — N discrete secrets + an org var index + a static ternary ladder evaluated in the CALLER. The composite is unchanged.**

- **Dynamic secret indexing is unsupported.** GitHub Actions expressions cannot index the `secrets`/`vars` context by a computed key. `secrets[format('CLAUDE_CODE_OAUTH_TOKEN_%d', idx)]` is **not** a valid dynamic lookup: the GitHub contexts docs describe only static property-dereference (`github.sha`) and literal index (`github['sha']`) access, and state that dereferencing a nonexistent property yields the empty string — there is no runtime name computation for `secrets`. So an index variable cannot select a secret at runtime by name.
- **The static ladder is the accepted workaround.** GHA's `&&`/`||` are short-circuit operators that **return the operand value** (documented in the expressions reference), so a chained ternary selects one of a fixed set of **literal** secret references. Each rung names a literal secret, decided at parse time — no dynamic indexing. Because N is capped at 4, the ladder is bounded.

The current caller step passes the single OAuth secret directly (`base-ai-review-single.yml:203`):

```yaml
claude_code_oauth_token: ${{ secrets.CLAUDE_CODE_OAUTH_TOKEN }}
```

Target caller expression (the ladder; index selects the rung, default rung = token 1 = today's secret):

```yaml
claude_code_oauth_token: >-
  ${{ vars.CLAUDE_OAUTH_INDEX == '4' && secrets.CLAUDE_CODE_OAUTH_TOKEN_4
   || vars.CLAUDE_OAUTH_INDEX == '3' && secrets.CLAUDE_CODE_OAUTH_TOKEN_3
   || vars.CLAUDE_OAUTH_INDEX == '2' && secrets.CLAUDE_CODE_OAUTH_TOKEN_2
   || secrets.CLAUDE_CODE_OAUTH_TOKEN }}
```

- **Ladder ordering rationale:** highest index first so the first matching rung wins; the final bare `|| secrets.CLAUDE_CODE_OAUTH_TOKEN` is the default rung — it fires when `CLAUDE_OAUTH_INDEX` is unset, `'1'`, or any non-`'2'..'4'` value. An unset var resolves to `''` (GHA), so every state except explicit `'2'/'3'/'4'` yields token 1 — **today's behavior**.
- **Empty-rung caveat (intentional):** if `CLAUDE_OAUTH_INDEX == '2'` but `CLAUDE_CODE_OAUTH_TOKEN_2` is not set, `secrets.CLAUDE_CODE_OAUTH_TOKEN_2` resolves to `''`. `'2'-cond && '' || ...` — the `&&` yields `''` (falsy), so evaluation falls through to the next rung, ultimately to token 1. Net effect: pointing the index at a non-existent token gracefully degrades to token 1 rather than passing an empty OAuth token (which would otherwise trigger AT-1606's empty-secret API path). This is the desired safety behavior and must be asserted in tests.

### 0-1a. Composite is unchanged — the ladder is entirely caller-side

- The composite `.github/actions/claude-review/action.yml` consumes exactly one OAuth input, `inputs.claude_code_oauth_token` (`action.yml:16-19,69`). The ladder resolves to a single token string in the caller (`single.yml`) and is passed to that one input. **No composite change is required for rotation** — a strict improvement over AT-1606, which had to add a `force_api` input.
- The ladder (token selection) and AT-1606's `force_api` gate (OAuth→API) are **orthogonal and compose cleanly**: the ladder decides *which OAuth token* to pass; AT-1606's composite expression decides *whether to blank OAuth and use the API key*. When `force_api == 'true'`, whichever token the ladder selected is blanked by AT-1606's `claude_code_oauth_token: ${{ inputs.force_api == 'true' && '' || inputs.claude_code_oauth_token }}` line. So Phase-1 of this feature touches only `single.yml` (and later the wrapper), never `action.yml`.

### 0-1b. INVARIANT — feature-off = single-token = today (hard constraint)

**With zero rotation config the behavior MUST be byte-for-byte identical to today (single OAuth token).**

- No `CLAUDE_CODE_OAUTH_TOKEN_2..4` secret, no `CLAUDE_OAUTH_INDEX` var → the ladder's default rung `secrets.CLAUDE_CODE_OAUTH_TOKEN` fires → same token passed to the composite as today (`single.yml:203`).
- Nothing declares the index var; the ladder is a pure comparison that collapses unset/empty/invalid to the default rung. No `env:` block, no workflow-level `vars:` declaration, no secret pre-creation. A consumer that never opts in, never grants Variables:write, and never runs the detection post-step sees today's behavior.
- This mirrors AT-1606 §3.7 / §0-1a and is a Done-criterion with a dedicated test.

### 0-2. Callers of the composite (same as AT-1606 §0-2)

- Only in-repo caller: `base-ai-review-single.yml:194-205` (`uses: ./.ai-dev-pr-review/.github/actions/claude-review`). `ci.yml`, `orchestrator`, `aggregate` do not call the composite directly.
- Pilot path (separate repo): consumer → `ignite-pilot-org/ai-dev-pr-review-wrapper` `wrapper.yml` → base orchestrator → single → composite (`docs/pilot-usage.md:8-20`). The wrapper has its own `Run Claude review` step that `uses` the composite via the local `.ai-dev-pr-review` checkout, so the wrapper needs the same ladder added separately. The pilot org supports neither org vars nor org secrets (`docs/pilot-usage.md:3-4,26-33`), so pilot `CLAUDE_OAUTH_INDEX` and the extra token secrets live per-repo.

### 0-3. Detection point + reset-epoch parsing (reuse AT-1606 / AT-1512)

- The composite's inner step is `continue-on-error: true` (`action.yml:51`) and exposes `execution_file` (`action.yml:40-43`); `single.yml` reads that file at `Extract Claude review from execution log (fallback)` (`single.yml:207-221`, `EXEC_FILE: ${{ steps.claude-review.outputs.execution_file }}`). AT-1606 adds a `switch_claude_auth.py detect-and-switch` post-step after that fallback; **AT-1607 extends that same post-step** (or runs alongside it) with the rotation decision.
- Reset-epoch parse is AT-1606's `_parse_limit_epoch` (from AT-1512 `scale_claude_model.py`, reverted commit `3807e2a:.github/scripts/scale_claude_model.py`): primary regex `Claude AI usage limit reached\|(\d+)` (last match wins); fallback `usage limit reached` with no epoch → `now + 5h`; no signature → no switch. **Reused, not re-implemented.**
- **Aggregate cannot detect** (AT-1606 §0-3): the composite swallows the error, so the claude job reports success and the execution log is not an aggregate artifact. Detection must run in single/wrapper.

### 0-4. App token mint + Variables:write (shared prerequisite with AT-1606, still ungranted)

- Mint pattern: `base-ai-review-aggregate.yml:105-113` (`actions/create-github-app-token@d72941d797fd3113feb6b93fd0dec494b13a2547` v1.12.0, `app-id: ${{ vars.REVIEWER_APP_ID }}`, `private-key: ${{ secrets.REVIEWER_APP_PRIVATE_KEY }}`). `secrets.REVIEWER_APP_PRIVATE_KEY` already exists (ignite-corp org secret, visibility=all); the orchestrator forwards it to the claude jobs as of AT-1606 (`base-ai-review-orchestrator.yml:29,79-104` — the `REVIEWER_APP_PRIVATE_KEY` forward AT-1606 adds).
- **Variables:write is NOT granted** — the Reviewer App holds only `contents:write, metadata:read, pull_requests:write`. Granting `Variables: Read and write` (repo + org) on both installations is the SAME manual prerequisite as AT-1606 §5.1 — do it once, both features use it. This feature adds no new permission beyond AT-1606's.

### 0-5. Public-repo logging + list var (reuse AT-1606)

- This repo is PUBLIC. Token names and pilot repo names must NEVER appear in Actions logs. Reuse the index-only convention (`echo "OK: entry ${i}/${total}"`) from `ruleset-sync.yml` / `ruleset-audit.yml` and AT-1606's script. The rotation logic logs the **index number** by design anyway, but must never echo a secret value or pilot repo full-name.
- The cron's managed-repo list reuses AT-1606's private org var `AUTH_SWITCH_REPOS` (JSON array of repo full-names). No new list var.

---

## Var / secret schema (additive over AT-1606)

| name | scope | value | created / deleted by | absent means |
|---|---|---|---|---|
| `CLAUDE_CODE_OAUTH_TOKEN` | org secret (existing) | token 1 (subscription A) | manual (exists) | — (required baseline) |
| `CLAUDE_CODE_OAUTH_TOKEN_2..4` | org secret (new, opt-in) | tokens 2..N (separate subscriptions) | manual (`claude setup-token` per subscription) | ladder degrades to token 1 (feature-off) |
| `CLAUDE_OAUTH_INDEX` | org var (repo var for pilot) | `'1'`..`'4'` | detection post-step / cron | ladder default rung = token 1 (today) |
| `CLAUDE_OAUTH_UNTIL_<i>` | org var (repo var for pilot), i∈2..N (and 1) | reset epoch (string) | detection post-step / cron | token i is available |
| `CLAUDE_FORCE_API` / `_UNTIL` | AT-1606 vars | `'true'` / epoch | this feature sets when ALL exhausted; AT-1606 cron deletes | AT-1606 OAuth path |
| `AUTH_SWITCH_REPOS` | private org var (AT-1606) | repo full-name JSON array | manual (AT-1606 prereq) | cron has no targets |

Notes:
- `CLAUDE_OAUTH_UNTIL_1` is tracked too, so token 1's exhaustion/reset participates in the "lowest-index non-exhausted" selection symmetrically.
- Pilot repos use repo-scoped vars for `CLAUDE_OAUTH_INDEX`/`CLAUDE_OAUTH_UNTIL_<i>` and repo-scoped secrets for the extra tokens (pilot org has neither org vars nor org secrets).

---

## Phased plan

Phases are ordered by dependency. **Phase 0 is a hard gate: AT-1606 must be shipped first.**

### Phase 0 — depends on AT-1606 shipped

- AT-1606 (Release A + B) must be merged and released: the composite `force_api` input, the detection post-step + `switch_claude_auth.py`, the orchestrator `REVIEWER_APP_PRIVATE_KEY` forward, and the `claude-auth-restore.yml` cron. This feature composes with all of them; do not start Phase 1 until AT-1606's `switch_claude_auth.py` and cron exist on `origin/main`.
- The shared manual prerequisite (App Variables:write, `AUTH_SWITCH_REPOS`) is AT-1606's Phase 4 — reused, not re-done.

### Phase 1 — caller ladder (single.yml) + composite unchanged + release

Goal: the caller can select OAuth token 1..N via `CLAUDE_OAUTH_INDEX`, defaulting to token 1.

Files:
- `.github/workflows/base-ai-review-single.yml`
  - Replace the `claude_code_oauth_token:` value in the `Run Claude review` step (`:203`) with the §0-1 static ladder. No other change to that step.
- `.github/actions/claude-review/action.yml` — **no change** (§0-1a).
- Tests: a ladder-matrix table (unit or documented actionlint-verified expression matrix):
  - `CLAUDE_OAUTH_INDEX` unset → token 1 (default rung; feature-off invariant, §0-1b, required Done-criterion).
  - `'1'` → token 1; `'2'` → token 2; `'3'` → token 3; `'4'` → token 4.
  - non-valid value (`'x'`, `'True'`, `'0'`) → token 1.
  - index `'2'` but `CLAUDE_CODE_OAUTH_TOKEN_2` unset → falls through to token 1 (empty-rung caveat, §0-1; asserts we never pass an empty OAuth token via a misconfigured index).

Release: bump the `ref:` pin in `single.yml:69` (`.ai-dev-pr-review` checkout) to the new tag, tag it, move `v1` via `move-major-tag.yml`. Behavior-identical to today until an index var + extra token secret exist (§0-1b). Pilot picks it up via its `@v1` floating pin, but the wrapper's own claude step needs the ladder too (Phase 2 wrapper item).

### Phase 2 — detection rotation logic (switch_claude_auth.py extension + single.yml)

Goal: on a usage-limit event, advance `CLAUDE_OAUTH_INDEX` to the next non-exhausted token; if none remain, hand off to AT-1606's API path.

Files:
- `.github/scripts/switch_claude_auth.py` (AT-1606's script) — extend `detect-and-switch` (or add a `rotate-and-switch` mode invoked before the AT-1606 force_api set):
  - On limit detection with epoch `E`:
    1. Read current `CLAUDE_OAUTH_INDEX` (default `1`). Record `CLAUDE_OAUTH_UNTIL_<current>=E` (the just-exhausted token).
    2. Compute the next index: the smallest `j` in `current+1..N` whose `CLAUDE_OAUTH_UNTIL_<j>` is absent or already-passed. Cap N by counting which `CLAUDE_CODE_OAUTH_TOKEN_<j>` secrets are configured — but a script cannot read secret existence; instead treat N as `min(4, configured-max)` supplied via env `CLAUDE_OAUTH_TOKEN_COUNT` (an org var, default `1`), so the loop never selects an index without a secret.
    3. If such `j` exists → PATCH `CLAUDE_OAUTH_INDEX=j`. Do NOT set `CLAUDE_FORCE_API`.
    4. If no `j` (all of `current+1..N` exhausted or `N==current`) → set AT-1606's `CLAUDE_FORCE_API=true` and `CLAUDE_FORCE_API_UNTIL=<earliest token reset epoch>` (compose — reuse AT-1606's setter). This is the all-exhausted→API handoff.
  - Fail-safe exit 0, index-only logging, `DRY_RUN`, per AT-1606 conventions. Scope is per-repo (mint owner = the run's org), same as AT-1606's `detect-and-switch`.
- `.github/scripts/tests/test_switch_claude_auth.py` (AT-1606's test file) — add rotation cases:
  - next-index from 1 with token 2 available → 2; token 2 exhausted-until-future → skip to 3; all of 2..N exhausted → returns "handoff to API" and asserts `CLAUDE_FORCE_API` set.
  - `CLAUDE_OAUTH_TOKEN_COUNT=1` (feature-off) → any limit → straight to AT-1606 force_api (single-token behavior: no rotation, immediate API handoff exactly as AT-1606 alone would do).
  - reset-aware: an until epoch already passed is treated as available.
- `.github/workflows/base-ai-review-single.yml` — the detection post-step is AT-1606's; AT-1607 only changes which script mode/args it invokes (pass `CLAUDE_OAUTH_TOKEN_COUNT` env). No new step.
- Pilot `wrapper.yml` (separate repo) — add the Phase-1 ladder to the wrapper's claude step; the script is shared via the wrapper's pinned `.ai-dev-pr-review` checkout, so no wrapper-side script change.

### Phase 3 — cron extension (claude-auth-restore.yml)

Goal: hourly, return reset tokens to rotation and prefer the lowest-index available token.

Files:
- `.github/workflows/claude-auth-restore.yml` (AT-1606's cron) — extend `switch_claude_auth.py restore-if-due` per repo in `AUTH_SWITCH_REPOS`:
  1. For each `i` in `1..N`: if `CLAUDE_OAUTH_UNTIL_<i>` exists and `now > epoch` → DELETE it (token i rejoins rotation).
  2. Set `CLAUDE_OAUTH_INDEX` to the **lowest** `i` in `1..N` with no live `CLAUDE_OAUTH_UNTIL_<i>` (prefer the cheapest/first subscription). If token 1 is available, index returns to 1.
  3. AT-1606 rule preserved: if `CLAUDE_FORCE_API_UNTIL` passed → DELETE `CLAUDE_FORCE_API`/`_UNTIL`; but if step 2 found any available OAuth token, prefer returning to OAuth (index reset) over staying on API even before the force-api epoch, since a token became usable again.
- Reuse `AUTH_SWITCH_REPOS`; no new list var. Index-only logging.

### Phase 4 — operator setup (manual) + rollout

Manual (USER):
1. AT-1606's App Variables:write grant + `AUTH_SWITCH_REPOS` (shared prerequisite; done once for both features).
2. On **each separate subscription** run `claude setup-token`; create org secrets `CLAUDE_CODE_OAUTH_TOKEN_2` (and `_3`/`_4` if scaling) with visibility=selected for the managed repos. Start with token 2 (N=2).
3. Create/ set org var `CLAUDE_OAUTH_TOKEN_COUNT` = the number of configured tokens (e.g. `'2'`). Absent/`'1'` = feature-off.
4. Ensure `ANTHROPIC_API_KEY` is in the selected-secret list and ENABLED (AT-1606 prereq — the final fallback).

Rollout: land Phase 1-3 PRs and release; complete operator setup; observe token 1 exhaustion → index→2 → token 2 exhaustion → `CLAUDE_FORCE_API` set (API) → after resets, cron returns index→1.

---

## Sequencing vs AT-1606

1. **AT-1606 ships first (Phase 0 gate).** Its `force_api` composite input, `switch_claude_auth.py`, orchestrator secret forward, and `claude-auth-restore.yml` cron are the substrate this feature extends. Building AT-1607 before them means editing scripts/workflows that don't exist yet.
2. **AT-1607 Release (Phase 1-3):** caller ladder (composite untouched) + `switch_claude_auth.py` rotation extension + cron extension. Safe no-op until `CLAUDE_OAUTH_TOKEN_COUNT`/index/extra-token secrets exist (§0-1b), so it can merge and release while inert.
3. **Manual (Phase 4):** separate-subscription tokens + `CLAUDE_OAUTH_TOKEN_COUNT`. This activates rotation. The AT-1606 Variables:write grant is shared.
4. **Wrapper release:** add the ladder to the pilot `wrapper.yml` claude step, released from its repo pinned to the new upstream tag; the script rides along via the pinned checkout.

Rationale: AT-1606 is the last-resort layer AT-1607's "all exhausted" branch delegates to. Composing (rather than duplicating) means AT-1606 must exist first, and AT-1607's inert-until-configured property lets both ship independently of the manual activation.

---

## Test strategy

- **Ladder matrix (Phase 1):** the six §Phase-1 cases, **including the feature-off invariant** (index unset → token 1, byte-for-byte today) and the empty-rung caveat (index points at an unset token → degrade to token 1, never pass an empty OAuth token).
- **Rotation unit (`test_switch_claude_auth.py`):** next-index computation (skip exhausted, respect `CLAUDE_OAUTH_TOKEN_COUNT` cap), reset-aware skip/return, and the **all-exhausted → AT-1606 handoff** (asserts `CLAUDE_FORCE_API=true` + `_UNTIL` set only when no OAuth token remains).
- **Feature-off unit:** `CLAUDE_OAUTH_TOKEN_COUNT` absent/`'1'` → a limit event goes straight to AT-1606's force_api with no rotation (single-token behavior preserved).
- **Cron unit:** reset an until epoch → token rejoins; index reset to lowest available; prefer OAuth return when a token frees up.
- `actionlint` on changed workflows (validates the ladder expression parses).
- **Index-only logging check:** assert no token value and no pilot repo full-name is echoed — only `entry i/total` and the numeric index.
- `DRY_RUN=true` logs intended index/var writes (index-only) without calling the API.
- **Live observation:** token 1 exhausts → index→2 → token 2 exhausts → `CLAUDE_FORCE_API` set → after resets, cron returns index→1 and clears force-api.

---

## Rollback plan

- **Code:** revert the AT-1607 Phase-1/2/3 merge commits (leaves AT-1606 intact — this feature is strictly additive). Re-release with `ref:` pointed back to the prior tag; move `v1`. The ladder's default rung means reverting `single.yml` restores the plain single-token pass.
- **Vars/secrets:** set `CLAUDE_OAUTH_TOKEN_COUNT=1` (or delete it) and delete `CLAUDE_OAUTH_INDEX` / `CLAUDE_OAUTH_UNTIL_<i>`; the ladder falls back to token 1. Extra token secrets can stay (unused) or be deleted. AT-1606's force_api handling is untouched.
- **Permission:** the shared Variables:write grant stays (AT-1606 needs it); inert for AT-1607 once its vars are removed.
- Self-correcting: if the cron is disabled mid-flight, a repo stays on its current index (reviews still run on that token) or on the billed API (AT-1606) until vars are cleared — no review outage.

---

## 채택하지 않은 대안

- **Dynamic index into secrets** (`secrets[format('CLAUDE_CODE_OAUTH_TOKEN_%d', vars.CLAUDE_OAUTH_INDEX)]`): the "obvious" design. **Rejected — not valid GHA** (§0-1). The `secrets`/`vars` contexts admit only static property/literal-index access; there is no runtime name computation, and a nonexistent property yields `''`. The static ternary ladder is the standard bounded workaround (N≤4).
- **Single delimited secret split in a script** (option (b): one `CLAUDE_OAUTH_TOKENS` secret = `tok1|tok2|...`, a script picks the active one). **Rejected.** The chosen token must reach claude-code-action as a **workflow-layer `with:` input**; a `run:` step cannot inject a secret value back into a *later* step's `with:` safely — writing a secret to `$GITHUB_OUTPUT`/`$GITHUB_ENV` for later `with:` consumption defeats secret masking and risks leaking into public logs, and the action reads `claude_code_oauth_token` from the workflow expression context, not from a runtime-computed env. The caller-side ladder keeps every token as a first-class masked secret referenced by literal name.
- **Same-team seats as the extra tokens** (add teammates' seats from the SAME subscription). **Rejected / warned.** Same-team seats share a single weekly OAuth bucket, so rotating among them gives zero extra capacity — exactly the AT-1512 sonnet trap. Rotation ONLY helps when each token is a **separate subscription** with its own bucket (§1). The spec warns operators explicitly; if separate subscriptions are unavailable, this feature is a no-op and AT-1606 (OAuth→API) alone is the right layer.
- **Proactive rotation via a usage API.** **Rejected — no such API.** The Team plan exposes no subscription usage-percentage endpoint (confirmed in AT-1606/AT-1512), so rotation is necessarily reactive on the limit error, like AT-1606. Cost: one failed attempt per token at first exhaustion (self-correcting).
- **Composite-internal token selection** (put the ladder inside `action.yml`). **Rejected — same reason as AT-1606 §4-1:** a composite cannot read the caller's `vars`/`secrets`. The ladder must live in the caller; the composite stays single-input.
