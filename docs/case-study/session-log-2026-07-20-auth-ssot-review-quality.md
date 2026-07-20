# Session Log: 2026-07-03..20 — Auth SSOT, review-quality hardening, API-timeout fix

## Basic info
- Dates: 2026-07-03 .. 2026-07-20 (multi-day span, multiple sessions)
- Agent identity: leader coordinating worktree teammates (delegated code changes)
- Related tickets: AT-1497/1498/1499/1501/1502/1511/1525/1526/1527/1528/1530/1601/1602
- Releases: base v1.0.19 -> v1.1.3, pilot wrapper v1.1.0

## Work

### Background / goals
Two threads across the span:
1. **Auth single-source-of-truth (SSOT)** — centralize the Claude review step (OAuth-preferred auth, action SHA pin, model wiring) into one shared composite action so base and the pilot wrapper never drift, then fix the billed-API path that was aborting mid-review.
2. **Review-quality hardening** — reduce reviewer noise (dedup, diff-scope, evidence rules, round cutoff) and align verdict behavior with the auto-approve policy.

### Tasks done
1. **AT-1497** — downgrade `request_changes` to `comment` when auto-approve is disabled, so a non-approving verdict does not hard-block repos that have not opted into App approval.
2. **AT-1498** — make the `_check_criticals` reviewer-set element type explicit (type-safety refactor).
3. **AT-1499** — prefer the OAuth (subscription) token over the API key in the Claude review step via the ternary `secrets.CLAUDE_CODE_OAUTH_TOKEN == '' && secrets.ANTHROPIC_API_KEY || ''`. Fixes the CLI precedence trap where `ANTHROPIC_API_KEY` silently outranks the OAuth token and bills the API.
4. **AT-1501/AT-1502** — extract the review step into the shared composite action `.github/actions/claude-review`; `base-ai-review-single.yml` and the pilot wrapper both consume it via the local checkout path `./.ai-dev-pr-review/.github/actions/claude-review`. SSOT for the ternary, the `anthropics/claude-code-action` SHA pin, and `--model`.
5. **AT-1511** — document the base<->wrapper MINOR-lockstep versioning policy.
6. **AT-1525** — dedup identical findings across force-push line shifts; include outdated threads in the reviewer dedup context.
7. **AT-1526** — diff-citation rule, evidence-rule wording for non-existence claims, severity recalibration.
8. **AT-1527** — dedup same-batch findings keeping highest severity; cap per-thread body at 200 chars on codex and gemini paths; dedup injected threads by path + normalized body.
9. **AT-1528** — add a diff-scope rule beside the evidence rule; allow removal-caused findings within diff scope.
10. **AT-1530** — fold non-critical findings into the summary after the round cutoff.
11. **AT-1601** — raise the claude-code-action API timeout via composite env (`API_TIMEOUT_MS` + `CLAUDE_STREAM_IDLE_TIMEOUT_MS` = 600000). The CLI's byte-level stream-idle watchdog defaults to 180 s on direct Anthropic API connections and was aborting billed-API reviews.
12. **AT-1602** — align consumer standalone Claude workflows to consume the base composite as SSOT (documented `@claude`-mention inline exception).

### Key outputs
- New shared composite: `.github/actions/claude-review/action.yml` (SSOT for auth ternary, SHA pin `a92e7c7...` = claude-code-action v1.0.159, model, timeout env).
- `base-ai-review-single.yml` switched to the composite (pinned scripts checkout at `ref: v1.1.3`).
- Releases cut manually on merge commits: v1.0.19, v1.0.20, v1.0.21, v1.1.0, v1.1.1, v1.1.2, v1.1.3; `v1` floated to the latest via `move-major-tag.yml`.
- Pilot wrapper v1.1.0 (MINOR-lockstep with base v1.1.x).

## Assessment data

### Design changes / clarifications
- **Composite over duplicated step (SSOT)**: the auth ternary + SHA pin + model previously lived in two places (base single.yml and wrapper). Consolidated into one composite so a single edit propagates. Cross-org reusable-*workflow* `uses:` is blocked on GitHub Free (the wrapper's founding rationale), but a local-path composite action after `actions/checkout` works — hence the `./.ai-dev-pr-review/...` path. (Classification: technical constraint.)
- **API-key kept as inert fallback**: user decided NOT to delete the `ANTHROPIC_API_KEY` secret; the ternary makes it inert whenever an OAuth token is present, and the hard on/off lives at the Anthropic console. (Classification: requirement clarification.)

### Rollback — AT-1512 (auto-scaler), reverted
An auto-scaler that would downgrade the review model on usage-limit events and reset back was implemented (PR #60) and immediately reverted (PR #62, revert of #60). Two reasons:
1. **Spec misread as a go signal** — the user's wish-phrasing ("자동으로 내려갔다가 리셋되면 다시 올라오는 방식으로 만들고 싶은데") was feature-spec discussion, treated as approval. The user was still discussing the spec and ordered a full rollback.
2. **Doesn't actually help** — under the Team Standard plan a single weekly bucket governs usage; a per-event sonnet downgrade cannot bypass weekly exhaustion, so the mechanism buys nothing.
Rollback was clean: revert PR merged, ticket returned to To Do, related vars deleted.

### AI errors / incidents (recovered)
1. **branch-delete-on-failed-merge (x2)** — a piped `gh pr merge ... | head && gh api --method DELETE .../refs/heads/<branch>` masked the merge's non-zero exit, so `&&` proceeded to delete the head branch of an UNMERGED PR, auto-closing it. Happened twice in the 2026-07-18..20 span (app-cast #29; the AT-1602 batch). Both recovered by restoring the ref at `headRefOid` (`gh api --method POST .../git/refs`) then `gh pr reopen` — approvals on that SHA are preserved. Fix: run `gh pr merge` alone, confirm `state==MERGED`, then delete.
2. **AT-1512 direction misread** — see rollback above.

### Human intervention
- Direction: release timing ("다른 작업 모두 끝나고 수동으로 릴리스 타이밍 잡으면 어때?" -> manual release cadence, base changes stay inert until release + pin bump), AT-1512 rollback order, auth-secret retention decision.
- Credential/UI-only actions: Anthropic console key enable/disable, org/repo variable and secret configuration.

## Quantitative snapshot
- Tickets landed: 12 (AT-1497/1498/1499/1501/1502/1511/1525/1526/1527/1528/1530/1601); AT-1602 aligns consumers; AT-1512 reverted.
- Releases: base v1.0.19 -> v1.1.3 (7 tags), wrapper v1.1.0; `v1` floated.
- Incidents: 2 branch-delete recoveries, 1 full-feature rollback.
- Static analysis: per-PR CI green (test + non-ASCII guard + actionlint) before each merge.

## Spec <-> Tests <-> Code sync
- Memory updated: `claude-auth-precedence-oauth`, `gh-merge-delete-chain-hazard`, `release-timing-manual`, `implement-only-on-explicit-command`.
- Durable runbook extracted to `docs/ops/claude-auth-and-review-operations.md` (this PR).
