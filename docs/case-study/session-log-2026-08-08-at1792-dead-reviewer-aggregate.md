# Session Log: 2026-08-08 — Dead-reviewer aggregate fix, self-review dogfooding, credential probe

## Basic info
- Date: 2026-08-08 (single day, leader + worktree teammates)
- Agent identity: leader coordinating worktree teammates (delegated code changes)
- Related tickets: AT-1792 (dead-reviewer aggregate fix), AT-1796 (credential probe)
- Release: base v1.2.0

## Work

### Background / goals
The aggregate step counted a reviewer as "participated" even when its CLI died and the
payload was a fallback error stub — a dead codex produced a fake "3/3 no issues" quorum.
Plan (AT-1792 ticket): honest reviewer counting (error keys on codex fallback payloads,
benign-skip narrowed to artifact-absence), codex CLI pin with arg fix, unit tests,
consumer-health error-streak detection, an auto-approve quorum gate, and a release with
pin bumps.

### Tasks done
1. **PR #88 (AT-1792, 5 commits)** — all planned items. codex pinned at
   `@openai/codex@0.147.0`; `--full-auto` removed after live-reproducing the outage error
   against the pinned CLI. pytest 190 passed / 4 skipped (+10 new tests).
2. **PR #89 — self-review (dogfooding) workflow** — this repo's own PRs now run the
   orchestrator via local path; repo var `ALLOW_AUTO_APPROVE=false` blocks bot
   self-approval. Its first run live-reproduced the AT-1792 bug (fake 3/3 with a dead
   codex) on the pre-fix pipeline — attached as evidence on #88.
3. **PR #90 (AT-1796) — weekly credential probe** — Friday 00:00 UTC (= 09:00 KST).
   curl reads headers from stdin (`-H @-`) so secrets never appear in argv or URLs.
   Classification: flag on 401/403 (Google also 400), warn on 5xx/timeout/429.
   GitHub-issue alerting is idempotent (open/update one issue) and auto-close is gated on
   all_ok — warns are not treated as confirmed recovery. `CLAUDE_CODE_OAUTH_TOKEN` is
   deliberately not probed (no documented validation endpoint).
4. **PR #91 (release v1.2.0)** — bumped three self-checkout pins: `single.yml:71`
   v1.1.5 -> v1.2.0; `aggregate.yml:57` and `prepare.yml:54` both stale at v1.1.2 ->
   v1.2.0. Release published; `move-major-tag` floated `v1` to merge commit `6b00160`.

### Key outputs
- Honest quorum: fallback error payloads carry error keys; benign-skip only on artifact
  absence; auto-approve gated on a real quorum.
- Self-review workflow on this repo (dogfooding loop that caught the very bug it shipped
  alongside).
- Weekly credential probe with issue-based alerting.
- Release v1.2.0, `v1` floated to `6b00160`.

## Assessment data

### Incidents / discoveries during execution
1. **12-day invalid org key surfaced by removing a misdiagnosis** — deleting the
   hardcoded "quota/auth error" label immediately exposed the real current failure: org
   secret `OPENAI_API_KEY` had been replaced on 2026-07-27 with an invalid value and gone
   undetected for 12 days. Replacement procedure: candidate key #1 was rejected by a
   pre-store curl check (401 — never stored); key #2 validated 200 and was stored with
   `--visibility selected --repos` preserved (20 repos). Codex then performed its first
   real review since 2026-07-18 ("3/3 no issues").
2. **Stacked verification ladder** — pre-fix pipeline -> fake 3/3 (bug reproduced);
   post-fix pipeline with dead codex -> honest 2/3 with the real cause quoted;
   post-key-fix -> true 3/3. All three states observed live on this repo's own PRs via
   the new self-review workflow.
3. **Retroactive consumer-health flags — working as designed** — the first post-merge
   consumer-health run flagged all 5 consumers ("codex error verdict in 9-10 consecutive
   recent PRs"): the 20-day coverage loss made visible. Each consumer's flag resets on
   its first PR with a working codex review.
4. **Review rounds** — #88: 2 rounds (3 Fixed + 1 By design); #90: 3 rounds (5 Fixed +
   3 By design/Duplicate). All closures carried evidence-based reply comments.

### Deviations from plan
- **Probe cadence**: daily (ticket draft) -> weekly -> Friday 09:00 KST. User/leader
  decisions, documented in the ticket. (Classification: requirement change.)
- **`prepare.yml:54` pin** discovered during release prep — the plan only knew of two
  pins; caught by grep. (Classification: investigation gap in the plan, fixed in
  execution.)
- **AT-1796 not in the original AT-1792 plan** — spawned as recurrence prevention after
  the key incident. (Classification: requirement change / scope addition.)
- **Ticket proposal 3 (explicit reviewer status contract)** deferred to a future ticket
  (not created yet). (Classification: scope deferral.)

### Human intervention
- Direction: probe cadence decisions (daily -> weekly -> Friday 09:00 KST), release
  timing, key-replacement approval.
- Credential actions: supplying the replacement `OPENAI_API_KEY` candidates.

## Quantitative snapshot
- PRs merged: 4 (#88 fix, #89 self-review, #90 probe, #91 release).
- Release: v1.2.0; `v1` floated to `6b00160`; 3 checkout pins bumped.
- Tests: pytest 190 passed / 4 skipped (+10 new) on #88.
- Review threads closed with evidence: #88 3 Fixed + 1 By design; #90 5 Fixed + 3 By
  design/Duplicate.
- Outage window made visible: codex dead 2026-07-18 .. 2026-08-08 (~20 days of coverage
  loss; invalid key for the last 12).

## Spec <-> Tests <-> Code sync
- Ticket plan vs implementation compared above; deviations recorded in the ticket.
- Deferred item tracked: reviewer status contract (future ticket, not yet created).
- Follow-ups: AT-1797 — replace the temporary `OPENAI_API_KEY` (week of 2026-08-10);
  verify pilot wrapper MINOR-lockstep alignment to v1.2.0; consumer-health health job
  stays red until consumers accumulate clean-codex PRs (expected, self-clearing).
