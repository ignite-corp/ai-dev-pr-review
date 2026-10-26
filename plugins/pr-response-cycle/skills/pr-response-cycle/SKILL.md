---
name: pr-response-cycle
description: Load this whenever a PR number or URL is in play with any review, thread, or merge action — a PR being taken to merge after CI, bot review comments (gemini-code-assist, claude, codex) needing handling, multiple threads or several in-flight PRs needing consistent cleanup. It applies even when the user never named the skill, because AGENTS.md Non-Negotiable 8 makes the cycle mandatory for PR work, not opt-in. Explicit asks include "process review", "respond to threads", "merge PR", "handle PR feedback", "PR 리뷰 처리", "thread 정리", "봇 코멘트 처리" — and it triggers without an explicit "respond to all threads". The cycle it drives, per the project's 10-step checklist — bulk-classify review threads (Fixed / Deferred / Won't fix / Duplicate / Outdated), post evidence-based replies, manage all three PR timeline item types (review threads + issue comments + review bodies), apply fixup-rebase for review-driven code changes, navigate merge state (CLEAN / BLOCKED / BEHIND / DIRTY), update branches, and merge with the project's own merge method when policy conditions are met.
---

# PR response cycle

Drive a GitHub pull request through the full feedback loop, applying the project's 10-step push checklist and resolve policy.

## Why this skill exists

The PR feedback loop has many small steps that are easy to do wrong: classifying threads consistently, writing the right reply for each resolve condition, fetching ALL threads (not just the first 50), batching GraphQL mutations, distinguishing CLEAN vs BLOCKED vs BEHIND merge states, applying fixup commits in the right order, and respecting project policy on merge strategy and admin gates. Without a clear playbook the agent re-decides these each PR and drifts. This skill encodes the playbook so the agent moves fast and consistently.

## When to use

- User mentions a PR by number/URL with review/merge intent: `"PR #313 리뷰 처리해줘"`, `"merge PR 146 when CI green"`
- Multiple unresolved review threads exist on a PR the user wants to land
- Bot reviewers (gemini-code-assist, claude-review, codex-review) have posted recurring patterns
- After a push, user signals "drive it to merge"
- A session has multiple in-flight PRs needing coordinated bulk cleanup

Skip when: only one trivial thread to address (just reply directly), or user explicitly wants step-by-step manual control.

## Project policy is the source of truth

Before applying any default in this skill, scan the active project's memory directory for relevant overrides. Where memory and this skill conflict, **memory wins**.

Resolve the memory dir from cwd:
```bash
PROJECT_MEMORY="$HOME/.claude/projects/$(pwd | sed 's|/|-|g')/memory"
ls "$PROJECT_MEMORY"/*.md 2>/dev/null
```

If the directory doesn't exist (new project, standalone repo), apply this skill's defaults as written. Surface to user that no project memory was found — they may need to bootstrap one or accept the generic defaults explicitly.

Key memory entries that affect this cycle. **All eight are qa-partner's** — every one exists only under `~/.claude/projects/-*-ai-dev-qa-partner/memory/` (the underscore filenames are the tell; other projects use hyphens). In any other project they are precedent, not authority: read them for the reasoning, then decide from what the current repository actually has — its own project memory, its enforced settings (branch protection, `allow_*_merge`), its own history — in that order, and say which one decided it.
- `process_review_push_checklist.md` — the canonical 10-step ordering (also the spine of this skill)
- `feedback_merge_strategy.md` — merge commit, never `--squash` at merge time (elsewhere, resolve it: **Merge strategy**, Stage 5)
- `feedback_no_admin_merge.md` — `--admin` per-PR explicit only; REST API merge as gh-CLI workaround when APPROVED
- `feedback_no_admin_merge_option.md` — admin merge MUST NOT appear in status reports as a fallback path
- `feedback_amend_over_close.md` — scope change → amend (force-push), never close+new
- `feedback_execute_per_policy.md` — policy already covers it → execute, don't re-ask
- `feedback_no_unjustified_wait.md` — no trailing question; pending task = next action
- `feedback_spawn_freely.md` — safe teammate spawn doesn't need per-PR confirm

AGENTS.md (global) sections that apply:
- Non-Negotiables #1 (no AI attribution), #2 (English only), #5 (PR workflow only), #7 (leader-vs-teammate), #8 (review before merge, this cycle mandatory)
- `## PR Standards` (commits, comment management, resolve policy)
- Phase 9 (push: rebase before push, PR description reflects latest)
- Phase 10 (feedback: `git commit --fixup` + autosquash — canonical form under **Stage 5, step 9**)

## Conventions (apply throughout the cycle)

These rules apply to every artifact this skill produces — commits, PR titles, PR bodies, replies, comments.

**Language**:
- Code artifacts (commits, PR titles, PR bodies, code comments, docs) — **English only**
- Review thread replies and issue comments — Korean OK if the project conversation is Korean (matches reviewer)

**No AI attribution** (Non-Negotiable #1):
- Never add `Co-Authored-By: Claude`, `Generated with`, or any AI mention in commits, PR bodies, replies, or any artifact
- This applies to every reply, every commit, every issue comment

**Commit format** (AGENTS.md PR Standards):
```
type(TICKET): brief imperative description
```
- Types: `feat`, `fix`, `refactor`, `test`, `docs`, `perf`, `chore`
- Imperative mood, lowercase after colon, under 72 chars, no trailing period
- The fixup-and-autosquash workflow preserves the original commit's message, so getting the format right on first commit matters

**Branch naming** (Phase 2):
```
task/<TICKET>               # e.g., task/QAYAK-346, task/42
task/<short-description>    # when no ticket
```

**PR title** = same format as the commit (`type(TICKET): description`).

**PR body** template:
```markdown
## Summary
- <1-3 bullets explaining the change>

## Test plan
- [ ] <verification step 1>
- [ ] <verification step 2>

Closes AT-1234      # Jira: the bare key
Fixes #37           # GitHub issues: the "#" is required
```

Use the line that matches the repository's tracker and delete the other. The two are not interchangeable: on a GitHub-issue repository `Closes 37` links nothing while still reading as if it had, so the issue stays open after the merge that closed it. Whichever form applies, it belongs in the body — not only in the commit message.

When PR scope changes mid-flight (per `feedback_amend_over_close.md`), update the body to reflect the latest state — Phase 9: "Ensure PR description reflects the latest changes". Don't leave a stale Summary describing the original scope.

**Autosquash vs squash merge** (commonly confused):
- Local autosquash (post-fixup) — **REQUIRED** by checklist step 9; canonical form under **Stage 5, step 9**
- `gh pr merge --squash` (REMOTE, at merge time) — **not this skill's call**: forbidden where the project says so (qa-partner does, via `feedback_merge_strategy.md`), allowed where it doesn't. Resolve it per **Merge strategy**, Stage 5
- They're different operations. Local autosquash collapses fixup commits into their target before push. Squash merge collapses ALL commits into one at merge — that's the one a merge-strategy decision is about.

## The canonical 10-step ordering

Per `process_review_push_checklist.md`, every push (after review feedback) follows this order. The ordering matters because each push triggers new bot comments — interleaving cleanup with code fixes guarantees missed items.

```
1.  Code fix + lint
2.  Fixup commit (git commit --fixup=<target SHA>)
3.  Thread resolve — every unresolved thread gets reply + resolve
4.  By-design quote comment — for items in aggregate summary without inline thread
5.  By-design user confirm — minor+ by-design needs user OK
6.  Stale minimize/dismiss — minimize prior aggregate, dismiss ALL prior reviews
7.  Issue-comment full sweep — confirm 0 un-minimized bot comments
8.  Review round completed on the head SHA — canonical form under Stage 5, step 8
9.  Rebase — autosquash per Stage 5, step 9 (exact form + post-check)
10. Push (alone, last step)
```

**These are labels, not an order, and 8 is the one that differs**: the numbering comes from `process_review_push_checklist.md` and is kept so the two documents can be read against each other, but the gate at 8 **runs after 9 and 10**, never before. Its subject is the SHA that ends up on the remote, and 9 and 10 are what decide that SHA — checking before them asks about a commit the push is about to replace, which is the whole failure this gate exists to stop. Executed order within a round: **1-7, then 9, 10, then 8, then the merge gate (Stage 5, Standard merge), then merge.**

**Critical ordering rule — cleanup BEFORE push within each round**:

1. Make all code edits locally (steps 1-2: fix + fixup commit)
2. Do thread/issue/review-body cleanup (steps 3-7) — reply, resolve, minimize, dismiss
3. THEN autosquash (step 9) and push (step 10)

Why this order:
- Each push triggers a new bot review round on the NEW HEAD. If you push before cleanup, the OLD round's threads get auto-dismissed/superseded chaotically with the NEW round's threads.
- Cleaning up before push means the existing round closes cleanly; the next push starts a fresh round.
- Bot replies referencing "Fixed: <description>" don't need the commit to be on remote yet — the description-based reply works before push.

The AGENTS.md guideline "Do not interleave code fixes with comment cleanup" applies to MULTI-iteration scenarios: don't cleanup between code commits within one round; do all code edits first, then one cleanup pass, then push. Within one round the order is still: code → cleanup → push.

### 5-stage ↔ 10-step mapping

| Stage | Steps | Action |
|-------|-------|--------|
| 1. Snapshot | (pre-checklist gather) | Read state |
| 2. Classify | (pre-checklist decision) | Decide dispositions |
| 3. Code change via teammate | 1, 2 | Fix + fixup LOCAL only — no push yet |
| 4. Apply all dispositions | 3, 4, 5, 6, 7 | Reply, resolve, minimize, dismiss BEFORE push |
| 5. Finalize + navigate + merge | 8, 9, 10, merge gates | Rebase, push, head-SHA review gate, merge |

The order matters: Stage 3 must NOT push. Stage 4 cleans up the existing round's comments. Stage 5 then rebases + pushes (which triggers next bot round) and proceeds to merge gates.

## The five-stage execution flow

### Stage 1 — Snapshot PR state

If the user pasted a PR URL, parse it: `gh pr view <URL>` works directly with full URLs.

Get all decision-relevant data upfront. Parallel-friendly:

```bash
gh pr view N --repo OWNER/REPO \
  --json mergeStateStatus,reviewDecision,statusCheckRollup,reviews \
  --jq '{merge:.mergeStateStatus, review:.reviewDecision,
         checks:[.statusCheckRollup[] | "\(.name):\(.status):\(.conclusion)"],
         reviewers:[.reviews[] | "\(.author.login):\(.state)"]}'
```

For threads, paginate properly. Default `last:50` misses items on large PRs. Always check `totalCount` first:

```bash
gh api graphql -f query='query($o:String!,$r:String!,$n:Int!) {
  repository(owner:$o,name:$r) { pullRequest(number:$n) {
    headRefOid
    baseRefName
    reviewThreads(last:50) {
      totalCount
      nodes { id isResolved isOutdated path
              comments(first:1) { nodes { author { login authorAssociation } body } } }
    }
  } }
}' -F o=OWNER -F r=REPO -F n=N
```

Capture `headRefOid` and `baseRefName`:
- **`headRefOid`** is the PR HEAD SHA at snapshot time. Re-check this after Stage 4 — if it changed, a concurrent push happened and the cleanup may be stale; restart from Stage 1. This snapshot value is never the one the merge gate uses: **Stage 5, step 8** re-reads the head for itself, immediately before merging.
- **`baseRefName`** is the actual base branch — don't assume `main`. Use this everywhere later commands say `origin/main` (e.g., `gh pr update-branch`, the step 9 autosquash rebase).

If `totalCount > 50`, use cursor pagination (`before:`/`startCursor`) — don't silently miss items.

**Author classification** (for thread-handling decisions):
- Bot reviewers — automated, skill can auto-classify and respond
- Human reviewers — **never auto-resolve a human-authored thread**. Their threads need user-driven response or escalation. Treat as blocking until user addresses.

**Robust bot detection** (ordered checks, fall through on miss):
1. `authorAssociation == "BOT"` — most reliable signal
2. `author.login` ends with `[bot]` — secondary signal (e.g., `dependabot[bot]`)
3. Known list match — `gemini-code-assist`, `claude`, `codex`, `github-actions` (these don't end in `[bot]` but are bots)
4. If none match → treat as human

Don't rely on the hardcoded list alone — it ages as bots are added/renamed. The first two signals catch most current and future bots.

Also snapshot the other two timeline item types (per AGENTS.md PR Standards):
- **Issue comments** — `gh api --paginate repos/O/R/issues/N/comments`
- **Review bodies** — `gh api --paginate repos/O/R/pulls/N/reviews` (or `gh pr view --json reviews` which already returns all)

REST API pagination is mandatory: GitHub's default page size is 30, max 100. **Always use `--paginate`** with `gh api` for comments and reviews, otherwise PRs with 30+ items will silently miss content. The `gh pr view --json` family already returns everything; the issue is only with raw `gh api repos/...` calls. Same principle as the GraphQL `totalCount` check earlier — never trust default page-1.

All three need management. The handling pattern parallels:

| Item type | "Resolve" equivalent | "Reply" equivalent |
|-----------|---------------------|---------------------|
| Review thread | `resolveReviewThread` | `addPullRequestReviewThreadReply` (inline) |
| Issue comment | `minimizeComment` (`classifier: OUTDATED`) | New issue comment that quotes each item with `>` and responds inline |
| Review body | Dismiss the review (`PUT reviews/<id>/dismissals`) — **all prior reviews on a modified PR**, both APPROVED and CHANGES_REQUESTED | (No reply mechanism — express disagreement via thread or new issue comment) |

The issue-comment quoted-response pattern triggers specifically when: the bot's aggregate summary issue comment lists items that **don't have corresponding inline review threads**. For items that DO have inline threads, reply on the thread instead.

### Stage 2 — Classify each unresolved item

**First filter**: handle `isOutdated=true` threads as Outdated (minimize, no reply) without reaching the classification table. GitHub flags a thread `isOutdated` when the commented line is no longer in the diff — the issue auto-resolved by code change. No human reasoning needed.

**Second filter**: human-authored threads (non-bot) — surface to user, do NOT auto-classify. Exit the auto-loop and ask for direction.

**Third filter (the table below)**: bot-authored, not-outdated threads. Read each item's first comment and pick the resolve condition. The mapping is project policy:

| Condition | Reply pattern | When |
|-----------|--------------|------|
| **Fixed** | `Fixed: <what changed>` + file:line reference | Code change addresses the issue |
| **Deferred** | `Deferred to TICKET-XXX` (ticket MUST exist + verified before posting — **NEVER "will defer later" / "follow-up will be filed" without an actual ticket key**) | Valid issue but out of scope for this PR — only after a real ticket exists |
| **Won't fix** | `By design: <rationale>` with reasoning — or `Won't fix: below threshold` for an item *Fix in round, or close below threshold* puts under the line | Intentional decision — explain why; a below-threshold close is pre-authorized and skips the human gate |
| **Duplicate** | `Handled in thread #N` with thread reference | Same issue raised in another thread |
| **Outdated** | (Minimize via `minimizeComment`, no reply needed) | Superseded by newer review round |

Critical rules from AGENTS.md `## Resolve Policy`:
- Every close needs a reply — silent resolves are prohibited
- "Acknowledged" alone is never a valid close reason
- Deferring requires the target ticket to actually exist
- Bot duplicates (same issue across review rounds) → resolve as **Duplicate** referencing the canonical thread

### Fix in round, or close below threshold — which items may leave this PR

A review lane can chase its own tail, and the table above cannot see it happening. Measured on `ignite-corp/ai-dev-infra-max#1`: the reviewer approved every round while each fix push produced one or two new test-hardening items, and after five rounds the lane had not converged — because the queue was fixing every item, and the surface of "properties a hand-written assertion test could have" is unbounded. Five parallel lanes ran rounds 5..11 that week and three of them independently reported the same shape without being asked to look for it: **the later rounds' findings were against the code the previous round's fixes had added.** One PR grew from config-plus-wording to config plus two test modules plus a conftest.

So every item a round raises gets one question before it reaches the disposition table:

- **Fix in round** — critical, major, and any minor that changes what the code or the guard will catch. Correctness and security included, always.
- **Close below threshold** — items that only change how a test is written, and every *other* minor: a nit, a wording, naming or `작성 방식` item, anything the line above does not already send back into the round. Reply `Won't fix: below threshold` and resolve by the table above. **No ticket.** A finding worth a ticket is worth its own summary naming the defect, so there are no `PR #N follow-up`, `PR #N RN 잔여` or `리뷰 suggestion 묶음` tickets.

**The boundary is not test-vs-code.** That is the obvious wording and it is the wrong one. The same week's *test-level* items included a harness defect where two inversions of equal length shared a `.pyc`, so a failure was attributed to the previous case and one guard was not actually proven for several rounds; a coverage gate that passed with nothing analysed (`filesAnalyzed=0 expected=0`, exit 0), rebuilding the very defect its own ticket existed to close; and a policy table whose tests all fabricated the second file it declared facts about. A rule that defers "test-hardening" defers those three — each of which changes what is caught, and two of which had already let a real defect through. Ask this instead, in these words:

> does the item change what the code or the guard will catch, or only how the test is written?

- **Changes what is caught — fix in round**: "add a case for X" where X is a reachable failure the guard misses; a test whose assertion never exercised the path it names; a gate that passes on an empty input set; a fixture shared where the cases must not share one.
- **Only how the test is written — close below threshold**: "rename the fixture", "assert the whole string instead of a prefix", "extract the helper", "add `-> None`".

**Two conditions, pre-authorized** (user decision, 2026-10-16, superseding 2026-10-08). An item that meets **both** is closed `Won't fix: below threshold` without asking:

1. the item does not change what the code or the guard catches;
2. the item is, in substance, a minor or a suggestion — the only two severities this carve-out admits. Critical, major, correctness and security never close below threshold, whatever label the reviewer put on them, and neither does a severity this line does not name: anything not admitted here is refused by default and reaches the gate below.

Those two replace the per-item question for that one shape of item and nothing else. **Everything outside them keeps the human gate below** — every `Deferred`, every other `Won't fix`, every intentional hold or skip.

**Security needs a judge, not a label.** Reviewers do file security findings as `minor` — an internal-visibility guard arrived that way on `ai-dev-pr-review#202`, 2026-10-16 — so the severity the reviewer typed does not settle condition 2. An item whose security relevance is ambiguous is **not** below threshold: it goes to the human gate, where the skill already sends every security judgment.

**Why the batch ticket went away.** Until 2026-10-16 this section batched those items into one follow-up ticket per PR and closed each `Deferred to <KEY>`. Measured that day, the owner's To Do held 181 assigned tickets, about 40 of them that batch shape — `PR #N 후속`, `RN 잔여`, `suggestion 묶음` — and the recursion the old rule claimed would terminate did not: each batch ticket got its own PR, that PR got its own rounds, and each round batched again. A below-threshold item that matters is raised again by the next round on that file; that is the reviewer's job, and it costs nothing to carry. A ticket is for a defect worth its own summary.

### Human gate on punt decisions (Deferred / Won't fix / intentional hold) — MANDATORY

The agent must NOT unilaterally decide to punt a valid finding down the road. **Deferred, Won't fix, and any "나중에 / later / 의도적 보류 / intentional skip" disposition is a human-judgment decision, not an agent decision** — surface it and get explicit human sign-off on the *decision to punt itself* BEFORE posting the reply and resolving. Verifying a ticket exists is necessary but NOT sufficient: the human must agree the finding *should* be punted rather than fixed now.

Why this rule exists: a deferred/held finding is invisible after merge — it lives only in a ticket nobody re-opens. The AT-1839 class (a real `validation-canary` failure recorded as "의도적 보류 — 비핵심" during a DR drill, then silently red for ~4 weeks and hiding 4 downstream stacks) is exactly this failure mode: an agent-or-human punt that no human deliberately re-affirmed. The recurrence cost dwarfs the confirm cost.

One carve-out, and only one: the below-threshold close in *Fix in round, or close below threshold* is pre-authorized (user decision, 2026-10-16), so an item meeting both of its conditions is closed `Won't fix: below threshold` without coming here. The conditions are stated there and nowhere else. Every other `Won't fix`, and every `Deferred` without exception, reaches this gate.

Concretely, before Deferred or Won't fix on anything the two conditions above do not put below the threshold (and ALWAYS for correctness/security findings, including any item whose security relevance is ambiguous):
1. **State the punt as a decision, not a fact**: "이 finding은 지금 고치지 않고 <TICKET>으로 미루려 합니다 — 사유: <why out-of-scope>. 진행할까요?" — present it via `AskUserQuestion` (or an explicit inline ask), never as an already-decided line in a status report.
2. **Get the human's answer** on the punt itself. If they say fix-now, re-classify to Fixed (dispatch the change). Only on explicit agreement do you post the Deferred/Won't-fix reply.
3. **Record who decided and when** in the reply and the ticket, so the punt is auditable ("Deferred to AT-XXXX per <user> 2026-MM-DD").

The existing step-5 "By-design user confirm" (minor+ intentional choices need user OK) is this same gate for Won't fix; this rule generalizes it to **Deferred and every hold/skip**, and makes the confirmation about the *decision*, not just the ticket's existence. Trivial cosmetic Outdated-class items and pure Duplicates are exempt (they punt nothing — the issue is already handled or gone).

For **Deferred** disposition: the target ticket **MUST exist in the repository's tracker with a concrete key** before posting the reply. Phrases like "will defer to a future ticket", "follow-up will be filed", "deferred to next sprint", or "별 ticket 으로 처리" without a key are **NOT valid Deferred replies** — they ship a promise nobody will track. Two-step procedure:

**Step 1 — Create the ticket first (if it doesn't exist)**
If the ticket doesn't exist yet, create it in whichever tracker the repository's `AGENTS.md` names - `gh issue create`, or the Atlassian MCP where that is the tracker - BEFORE posting the thread reply. Capture the new key.

**Step 2 — Verify the key exists, then reply**
Verify the key against whichever tracker the repository names:

```bash
# a tracker reached through the Atlassian MCP
mcp__claude_ai_Atlassian__getJiraIssue cloudId=<id> issueIdOrKey=<TICKET>
# or: gh api "https://<site>.atlassian.net/rest/api/3/issue/<TICKET>" 2>/dev/null

# GitHub issue. Compared, not merely run: /issues/<N> also serves pull
# requests, so the bare lookup accepts the PR you are working on as its own
# deferral target. Filtering the PR out empties the output but leaves the exit
# status at 0, so a caller reading only `$?` still cannot tell the two apart.
# Measured against this repository: #48 is a PR and #37 is an issue.
#
#   --jq 'select(...)|.number'   #48 -> out="" rc=0   #37 -> out=37 rc=0
#   compared to <N>              #48 -> rejected      #37 -> accepted
#
[ "$(gh api repos/O/R/issues/<N> --jq 'select(.pull_request==null)|.number' 2>/dev/null)" = "<N>" ]
```

If a lookup fails or the comparison does not hold — the key is invalid, absent, or names a pull request rather than an issue — **pick a different disposition** (Fixed / Won't fix / Duplicate). Never post a Deferred reply pointing at a phantom key. A deferred-to-nothing reply ships a lie that gets merged.

Acceptable Deferred reply format:
```
Deferred to QAYAK-XXX (created 2026-MM-DD): <out-of-scope reason>
```
The "created YYYY-MM-DD" annotation makes it explicit the ticket is real and recent — auditors don't have to verify themselves.

For **bot factual errors** (e.g., "X is not a valid enum value" when it actually is), Won't fix is the correct disposition, but the reply MUST include verification evidence — schema dump, API output, doc URL. Bots cite (sometimes stale) docs; the human reviewer needs proof your verification was real.

For **DRY / refactor suggestions** at the threshold:
- 2 sites: Won't fix — abstraction below threshold adds template indirection without benefit
- 4+ sites: Fixed via follow-up commit

For **minor+ by-design** items (per `process_review_push_checklist.md` step 5): user confirm BEFORE marking Won't fix. Don't auto-disposition critical/major intentional choices without user OK.

### Stage 3 — Code changes via teammate (checklist steps 1, 2, 9)

Per AGENTS.md Non-Negotiable #7, leader must NOT edit code in the main worktree. Per `feedback_spawn_freely.md`, safe code-fix spawns don't need per-PR user confirm — just go.

For Fixed-class items requiring code change:

1. **Spawn coder teammate** with `isolation: "worktree"`. Pass:
   - The specific thread IDs and required changes
   - Branch name: existing PR branch (`task/<TICKET>`)
   - Commit format requirement: `type(TICKET): description` (Phase 2)
   - Fixup target SHA if amending an existing commit
2. **Teammate creates fixup commit LOCALLY** (do NOT push yet):
   ```
   git commit --fixup=<target SHA>
   # NO push here — Stage 4 cleanup must finish first
   ```
3. **Leader integrates** the teammate's fixup into the PR branch (still local, no push). Two paths, told apart by what the teammate reports it did — the SHA of its fixup and whether it pushed — not by what the leader expects:
   - **The fixup is local only** (step 2's rule, so the usual case): there is no remote ref to fetch. A worktree-isolated teammate shares this repository's object store, so its commit is reachable by SHA from the PR branch's checkout:
     ```bash
     git worktree list                  # the teammate's worktree and the branch it committed on
     git checkout <pr-branch>           # refused while another worktree holds the branch — below
     git merge --ff-only <fixup-sha>    # the teammate branched from the PR head
     # OR, when the PR branch has moved since the teammate branched from it:
     git cherry-pick <fixup-sha>        # a conflict or an empty pick stops here — below
     ```
   - **The teammate pushed a sibling branch** (it was told to, or it works from a separate clone): the remote ref exists, so fetch it:
     ```bash
     git fetch origin <teammate-branch>:<teammate-branch>
     git checkout <pr-branch>
     git merge --ff-only <teammate-branch>
     ```
   A `fixup!` subject survives both paths, so Stage 5, step 9 squashes it the same. A teammate that pushed to the PR branch itself has already started the round this stage exists to hold back — that is a step-2 breach to report, not a third path.

   Where the local path stops, and what to do there. The multi-worktree case is the expected one — `git worktree list` is the first line because of it — and the checkout is where it shows:
   - `error: 'task/X' is already used by worktree at /path/Y` — the PR branch is checked out in another worktree: an earlier teammate's, or the original spawn's. Either integrate from that worktree (`cd /path/Y`, then the merge or cherry-pick line), or override the dual-checkout lock from here with `git checkout --ignore-other-worktrees <pr-branch>` — every worktree keeps its own HEAD, so the flag bypasses the lock and nothing else. Round 2 of ignite-corp/ai-dev-pr-review#194 stopped on exactly this refusal, after the recipe that named the case had been removed.
   - `error: could not apply <sha>...` from `git cherry-pick` — a conflict applying the fixup. Resolve the markers, `git add <files>`, then `git cherry-pick --continue`; `git cherry-pick --abort` backs out entirely.
   - `The previous cherry-pick is now empty, possibly due to conflict resolution` — the fixup's diff is already on this branch (picked once before, or both branches made the same change): `git cherry-pick --skip` drops the empty commit.
4. **Defer rebase + push to Stage 5** — checklist steps 9 (autosquash) and 10 (push) run AFTER Stage 4 cleanup. Stage 3 ends with the fixup commit local, no remote update.

Trivial exceptions where leader edits directly: reply posts to review threads (text only), ticket comments, PR descriptions. NOT code/config/doc files.

For **PR scope changes** (rename, restructure, new requirements): per `feedback_amend_over_close.md`, **amend the existing PR** (force-push history rewrite). NEVER close + open new PR. The existing review threads (resolved + unresolved) and the continuity of the ticket link are why. Update the PR body too (Phase 9).

### Stage 4 — Apply all dispositions (checklist steps 3, 4, 5, 6, 7)

Execute mutations in parallel when items are independent. After all code work is done LOCALLY (Stage 3 end-state), but BEFORE pushing:

```bash
# Reply to a thread
gh api graphql -f query='mutation($id:ID!,$body:String!) {
  addPullRequestReviewThreadReply(input:{pullRequestReviewThreadId:$id,body:$body}) {
    comment { url }
  }
}' -F id="$THREAD_ID" -F body="$REPLY"

# Resolve a thread
gh api graphql -f query='mutation($id:ID!) {
  resolveReviewThread(input:{threadId:$id}) { thread { isResolved } }
}' -F id="$THREAD_ID"

# Minimize an outdated comment (within a thread, or a stale issue comment)
gh api graphql -f query='mutation($id:ID!) {
  minimizeComment(input:{subjectId:$id,classifier:OUTDATED}) {
    minimizedComment { isMinimized }
  }
}' -F id="$COMMENT_ID"

# Issue comment: post quoted-response as a NEW issue comment, then minimize the original
gh api repos/OWNER/REPO/issues/N/comments \
  --method POST -f body="$QUOTED_RESPONSE"

# Stale review body: dismiss ALL prior reviews when code changes occur
# Applies to BOTH APPROVED and CHANGES_REQUESTED — any modification resets the review state
# --paginate is critical: PRs with 30+ reviews would otherwise miss items past page 1
for REVIEW_ID in $(gh api --paginate repos/OWNER/REPO/pulls/N/reviews --jq '.[] | select(.state=="APPROVED" or .state=="CHANGES_REQUESTED") | .id'); do
  gh api --method PUT repos/OWNER/REPO/pulls/N/reviews/$REVIEW_ID/dismissals \
    -f message="Dismissed: superseded by round N+1 (code changes since this review)"
done

# PR body update (when scope changed) — fetch-modify-write to preserve metadata
# Naive `gh pr edit --body` overwrites the entire body, wiping any auto-added
# closing keywords, project links, or labeler-applied metadata.
CURRENT_BODY=$(gh pr view N --repo OWNER/REPO --json body --jq .body)
NEW_BODY=$(echo "$CURRENT_BODY" | <sed/awk in-place edit>)
gh pr edit N --repo OWNER/REPO --body "$NEW_BODY"

# Re-request reviewers after a fixup push (bot reviewers usually auto-trigger
# on push, but human reviewers don't get notified unless re-requested)
gh api --method POST repos/OWNER/REPO/pulls/N/requested_reviewers \
  -f reviewers='["<human-reviewer-login>"]'
```

**Review body dismiss rule (strict)**: when code changes are made on the PR (any fixup commit, any new push affecting reviewable code), ALL prior review bodies must be dismissed — both APPROVED and CHANGES_REQUESTED. The new HEAD has not been re-reviewed, so prior approvals are stale by definition. The review workflow's own aggregate is a review body on a consumer with `ALLOW_AUTO_APPROVE` on (step 8's shape table), and it is what the merge gate's kind (i) reads there — on the new head, in state `APPROVED` or `CHANGES_REQUESTED` — the only two the producer reviews with. This loop never reaches it when run where this stage places it, before the push: that aggregate is posted by the round the push starts, so it does not exist yet, and the reviews the loop finds are the previous head's — and this loop is what dismisses them. The producer folds only its own `CHANGES_REQUESTED` marker reviews (`aggregate_reviews.py`, `_minimize_stale_bot_items`, at ai-dev-pr-review d554aee) and never an approval; on the three PRs step 8's shape table cites, every dismissed approval was dismissed by this account — `review_dismissed` timeline events read 2026-10-02: ai-dev-cab #515 at 19:54:06Z, ai-dev-qa-partner #1195 at 07:17:40Z, ai-dev-infra-common #876 at 10:05:02Z, all `hyuk-hur`, none carrying the producer's `Superseded by new review` message. On a consumer that does not run this loop the previous head's approval stays `APPROVED`, and the gate's `commit_id` equality is the only thing that refuses it. Re-running the loop after the push would dismiss the new head's aggregate; the gate then refuses kind (i) and falls to kind (ii) — closed, not open — so restart from Stage 1 rather than re-running this loop on a head whose round has posted.

**Issue comment quoted-response format** (per `process_review_push_checklist.md` step 4):
```
> 봇이 지적한 항목 1: <quote>
Won't fix — <rationale + evidence>

> 봇이 지적한 항목 2: <quote>
Fixed in <commit-sha>: <what changed>

> 봇이 지적한 항목 3: <quote>
Deferred to TICKET-XXX: <out-of-scope reason>
```

**Colliding folded locations**: if cutoff/fallback items share a location,
quote each whole finding line on its own, with no label or other prefix inside
the quote. A supported verdict rendering and folded rendering with the same
severity, location and whole recoverable description line share one key, so
either quotation can answer both surfaces. The source marker/time binding is
still required. Repeat a quote once per matching item within a single folded
source, even when reviewers differ; quoting one item cannot answer two rows.
Custom or ambiguous raw renderer fields retain the original exact-line route;
unique locations also retain the legacy rewritten-location route. This does
not reconstruct arbitrary multiline descriptions. Explain each disposition
separately on the following line. Name the exact source marker, including
its round when present; this example answers the round-14 fallback:

```text
Dispositions for `inline-fallback-claude-r14`:

> - **a.py:1** -- - **minor** (claude): Validate authentication before forwarding credentials
Fixed: validate authentication first.

> - **a.py:1** -- - **minor** (claude): Close the socket after the retry budget expires
Fixed: close the socket on exhaustion.
```

**Invalid folded source comments**: a reply or minimization cannot repair an
empty, unparseable, or count-mismatched source comment. Inspect that comment
and ask its author or an authorized maintainer to repair the producer output
at the source, preserving every real finding; then rerun the gate. Do not
discard findings or change a count merely to obtain a pass. Until the source
is repaired, the merge remains blocked. Minimized comments are still read.

**Two failure modes**:
1. **GraphQL parse errors on reply text**: avoid parens-with-colons inside body when shell-quoting. Pass body via `-F body=...` (variable substitution), never inline in the query string. If you see `Expected COLON, actual: IDENTIFIER`, simplify the body's punctuation.
2. **Race against new review rounds**: if you reply while a new bot review fires, your replies may land on now-outdated threads. Per AGENTS.md PR Comment Management: finish all code work and push BEFORE the cleanup pass. Don't interleave.

**Idempotency**: this stage is safe to re-run. Already-resolved threads return cleanly from `resolveReviewThread`. Already-minimized comments stay minimized. Already-dismissed reviews ignore re-dismissal. If the cycle is interrupted (network, rate limit), restart from Stage 1 — re-snapshot and continue.

**Concurrent-push check** (post-Stage-4): re-fetch `headRefOid` and compare to the value from Stage 1.
```bash
NEW_HEAD=$(gh api graphql -f query='query { repository(owner:"O",name:"R") { pullRequest(number:N) { headRefOid } } }' --jq '.data.repository.pullRequest.headRefOid')
[ "$NEW_HEAD" != "$STAGE1_HEAD" ] && echo "HEAD changed during cleanup — restart from Stage 1"
```
If the HEAD changed, your replies likely landed on stale threads (new bot review may have superseded them). Restart from Stage 1 — don't proceed to Stage 5.

### Stage 5 — Finalize + navigate merge state + merge (checklist steps 8, 9, 10 + merge gates)

**Step 9 — local autosquash IS MANDATORY before push** — this block is the recipe's only home; every other mention, in this skill and in `claude/AGENTS.md` Phase 10, points here (use `baseRefName` from Stage 1). The rebase and its post-check are one sequence — paste the whole block; `pre`, captured before the rebase, is what the check after it compares against. The squash targets `$mb`, the branch's own merge-base with the base, so nothing inside this block moves the base:
```bash
[ ! -d "$(git rev-parse --git-path rebase-merge)" ] && [ ! -d "$(git rev-parse --git-path rebase-apply)" ] \
  || { echo "ERROR: a rebase is already in progress — finish or abort it before autosquashing"; exit 1; }
mb=$(git merge-base origin/<baseRefName> HEAD) \
  || { echo "ERROR: cannot find the merge-base with origin/<baseRefName> — is it fetched?"; exit 1; }
pre=$(git rev-parse HEAD)
# Both editors stubbed: GIT_SEQUENCE_EDITOR for the todo list, GIT_EDITOR for the
# commit message a squash! opens. The default combined message is the target's
# plus the squash! body, which is what --squash= asked for, and the tree check
# below proves the squash rewrote no content -- do not drop a stub to review it.
rc=0; GIT_SEQUENCE_EDITOR=true GIT_EDITOR=true git rebase -i --autosquash "$mb" || rc=$?
if [ "$rc" -ne 0 ]; then
  if [ -d "$(git rev-parse --git-path rebase-merge)" ] || [ -d "$(git rev-parse --git-path rebase-apply)" ]; then
    orig=$(cat "$(git rev-parse --git-path rebase-merge)/orig-head" 2>/dev/null \
        || cat "$(git rev-parse --git-path rebase-apply)/orig-head" 2>/dev/null)
    [ -n "$orig" ] \
      || { echo "ERROR: rebase state on disk has no readable orig-head — inspect git status and clean up by hand"; exit 1; }
    [ "$orig" = "$pre" ] \
      || { echo "ERROR: a rebase not started by this block is in progress (orig-head ≠ $pre) — not touching it; finish or abort it by hand"; exit 1; }
    git rebase --abort \
      || { echo "ERROR: autosquash failed AND git rebase --abort failed — rebase state is still on disk; inspect git status and clean up by hand before anything else"; exit 1; }
    echo "ERROR: autosquash stopped mid-replay — the fixup cannot be replayed cleanly in place. Aborted; re-target it at the most recent commit in the branch that touches those hunks, do not resolve by hand, do not push"
  else
    echo "ERROR: autosquash did not start (exit $rc) — read git's output above: unfetched base ref, invalid ref, or a failing hook; nothing to abort, do not push"
  fi
  exit 1
fi
[ ! -d "$(git rev-parse --git-path rebase-merge)" ] && [ ! -d "$(git rev-parse --git-path rebase-apply)" ] \
  || { echo "ERROR: a rebase is still in progress — HEAD is a partially rebuilt branch, not a squashed one"; exit 1; }
git diff --quiet "$pre" HEAD \
  || { echo "ERROR: the tree changed across the autosquash — a squash rewrites history, never content"; exit 1; }
subjects="$(git log --format=%s origin/<baseRefName>..HEAD)" \
  || { echo "ERROR: could not read origin/<baseRefName>..HEAD — is the base ref fetched?"; exit 1; }
if printf '%s\n' "$subjects" | grep -qE '^(fixup|squash|amend)!'; then
  echo "ERROR: fixup/squash/amend commits not squashed — run autosquash first"; exit 1
fi
echo "OK — clean history, $(printf '%s\n' "$subjects" | grep -c .) commit(s) checked"
```

Precondition, not something the block can verify: no other session rebases this worktree while the block runs (AGENTS.md Multi-Agent Safety: one worktree per teammate). git keeps no owner on rebase state, so the `orig-head` comparison is the strongest signal available; a second rebase started at the same HEAD inside the window is indistinguishable, and the rule, not the block, is what excludes it.

Then, if `origin/<baseRefName>` is ahead of `$mb`, `git rebase origin/<baseRefName>` — an ordinary rebase whose conflicts are ordinary conflicts, outside the squash check; re-run the whole block afterwards; it captures a fresh `$mb` against the updated base. `$mb` is the squash upstream only; the subject check reads `origin/<baseRefName>..HEAD` because that is what reaches the remote — the two coincide when there is one merge-base and differ with several, where only the origin range answers the question.

`GIT_SEQUENCE_EDITOR=true` is what makes `-i` non-interactive: it stubs out the todo-list editor so the rebase never opens one. `GIT_EDITOR=true` stubs the other editor the rebase can open: a `squash!` commit — `git commit --squash=<sha>`, one of the three prefixes the check enumerates — makes the rebase combine the two messages and open the commit-message editor, which `GIT_SEQUENCE_EDITOR` does not reach. Measured on round 6 of ignite-corp/ai-dev-pr-review#194 (2026-10-14, git 2.43, a branch holding one `squash!`, `GIT_EDITOR` unset, stdin not a terminal): the rebase launched the default editor, the editor refused the non-terminal input, `git commit` failed with *There was a problem with the editor*, and the rebase stopped at `Could not apply … squash! …` with `rebase-merge` on disk — which the block's failure branch then reports as a replay conflict it is not; an editor that waits instead of refusing hangs the shell at that point. With both stubs the same branch rebased to a clean history, the target carrying the combined message, tree unchanged. The shell the block was first run in had `GIT_EDITOR=true` in its environment already, which is why the defect did not show there and will not show in any shell that pre-sets it. Keep all three — `-i` and the two stubs — none is noise, and without the stubs the `-i` would wait on an editor that never opens. Version dependence: git 2.44+ honours `--autosquash` without `-i`, but 2.43 (what Ubuntu noble ships) exits 0 and squashes nothing.

This collapses the autosquash-prefixed commits into their target commits. Without it, pushing leaves those literal titles in remote history — visible in PR commit list, ugly in `git log`, and hard for reviewers to follow. The autosquash is non-optional, and because it can fail silently the check that follows the rebase in the block is part of the recipe rather than an extra — it is version-independent, so it runs after every autosquash, before push.

Eleven properties, all load-bearing, because a check for a silent failure must not have one of its own: a rebase already in progress before the block starts is loud and exits non-zero; rebase state found after a failure is aborted only when its `orig-head` is the HEAD this block captured as `pre`, so a rebase another session started — before the block, or in the window after its precondition — is left untouched, loud, and exits non-zero, and state with no readable `orig-head` at all is reported as exactly that rather than as someone else's; a merge-base that cannot be found — base ref unfetched or misspelt — is loud and exits non-zero before anything runs; a rebase that stopped mid-replay is aborted, loud, and exits non-zero; an abort that itself fails is loud, exits non-zero, and says the state is still on disk rather than claiming a clean abort; a rebase that never started — unfetched base ref, invalid ref, failing hook — is loud, exits non-zero with git's own output above it, and aborts nothing; a rebase directory still present is loud and exits non-zero; a tree that differs from `pre` is loud and exits non-zero; a range that could not be read is loud and exits non-zero; an autosquash-prefixed subject is loud and exits non-zero; only a range actually read and actually clean prints OK. An empty range — a branch with no commits ahead of base — is legitimate, and reports `0 commit(s) checked`, which a failed read never reaches.

The exit-status guard and the two assertions ahead of the grep exist because the grep alone passed on a rebase that had stopped: on 2026-09-07, on hyukhur3680 (the factory-system-builder session, ignite-pilot-org/factory-system-builder PR #18), an autosquash halted on a conflict, the fixups not yet replayed sat outside `<base>..HEAD` — HEAD was the partially rebuilt branch — so the old check reported a clean history and 2 of 5 commits were force-pushed. A conflict here means the fixup cannot be replayed cleanly in place — abort, then re-target it at the most recent commit in the branch that touches those hunks: the later commit when the original target was right but a later one overlaps it, the right commit when the target was simply wrong. Resolving by hand inside the autosquash authors content in a step that must change none. The tree check is about the squash alone: the rebase targets `$mb`, the branch's own merge-base, so a base that moved since the last sync cannot change the tree inside this block — updating onto it is the separate, ordinary rebase above, deliberately outside the check, because inside it a moved base would fail a good squash.

The alternation above is the only place the prefixes are enumerated; everything else defers to it. There are three, not two: `git commit --fixup=<sha>` writes `fixup!`, `git commit --squash=<sha>` writes `squash!`, and `git commit --fixup=amend:<sha>` — with `--fixup=reword:<sha>` as its shorthand — writes `amend!`. `--autosquash` collapses all three, so all three reach the remote unsquashed under the same silent no-op, and a check that matches only the first two reports clean history with an `amend!` commit sitting in the range.

Two details in the shape are deliberate. Reading into `subjects` first makes the range read a step that can fail: piping `git log` straight into `grep -q` swallows the failure, since `grep` then finds no match, exits 1, and an `|| echo "OK"` tail reports clean history having inspected nothing. And the match is on `--format=%s` rather than `--oneline`: a `log.decorate` setting inserts `(HEAD -> branch)` between the SHA and the subject, so an anchored pattern skips the very commit it exists to find.

**Step 10 — push** (single push at end of round, force-with-lease for amended history):
```bash
git push --force-with-lease origin <branch>
```
Always `--force-with-lease`, never `--force`. Lease guards against silently overwriting concurrent updates.

**Step 8 — a verdict on the exact head SHA** — read both review surfaces: workflow runs in the target and remote App check runs posted into it. Kind (i) selects the newest identity-matched round first, requires it to be completed, then requires its aggregate to approve with every configured reviewer; kind (ii) is a local-review verdict naming this SHA when no usable remote verdict exists. The aggregate's Result, roster and dispositions decide, not the check conclusion alone.

The question is not *"is anything running?"* but **"has a review round completed against the commit I am about to merge?"** The difference is when each can be answered wrongly. A branch-scoped `gh run list --branch <branch> --status in_progress` returns the same empty list whether the branch is genuinely idle or the push's run has simply not registered yet — and a run registers seconds after the push. An empty answer inside that window reads as *quiet* when it means *not visible yet*, and the gate opens on a head nothing has looked at. A question that names the SHA has no such window: a commit with no run has no run, whenever you ask it.

```bash
HEAD_SHA=$(gh pr view N --repo OWNER/REPO --json headRefOid --jq .headRefOid)
gh api --paginate "repos/OWNER/REPO/actions/workflows?per_page=100" \
  --jq '.workflows[] | {id, name} | @json'
gh api --paginate "repos/OWNER/REPO/actions/runs?head_sha=$HEAD_SHA&per_page=100" \
  --jq '.workflow_runs[] | {id, workflow_id, status, conclusion, created_at, updated_at, html_url} | @json'
gh api --paginate "repos/OWNER/REPO/commits/$HEAD_SHA/check-runs?filter=all&per_page=100" \
  --jq '.check_runs[] | {name, status, conclusion, started_at, completed_at, app: .app.slug, details_url} | @json'
```

**Know the review name and distinct reviewer count before reading output.** Read `REVIEW_WF` from the target's actual review configuration: `AI Code Review (self)` for this repository, or the configured remote check name for a runner target. A remote-only target need not carry a local review workflow. The gate verifies the name against target workflows and identity-matched check runs. Read `EXPECTED_REVIEWERS` from the producing orchestrator, including the runner's selected orchestrator: Claude, Codex and Gemini are three reviewers, not six parallel/sequential jobs. The aggregate's own denominator is not configuration evidence.

These reads are SHA-scoped and paginated to exhaustion. Do not substitute a branch, sweep a capped list, or filter by status before selecting the newest round. A workflow run's `name` is its run-name, so the gate joins `workflow_id` against the workflow registry. Remote check candidates match both `REVIEW_WF` and `CHECK_APPS`, excluding ordinary Actions job checks and other Apps. Latest start wins across surfaces before status/conclusion checks; a queued round cannot hide behind an older completion. Completed success or failure proceeds to the aggregate's Result, roster and dispositions: the runner can conclude failure after posting an unreviewed-major verdict. Other completed conclusions supply no usable verdict. The cutoff window independently includes the earliest identity-matched start regardless of status/conclusion, and the first local verdict.

The check-run endpoint also carries ordinary job-level checks under the `github-actions` App. Those are not remote review rounds, even if a job copies the review name. Only the configured review name from an allowed reporting App is a remote-round candidate; a workflow run remains identified through its workflow ID/name join.

For a remote-only target with no check yet, set `REMOTE_REVIEW_SOURCE` to an
operator-selected trusted producer `owner/repo@<full 40-character SHA>:path`,
never a file from the target PR head. The gate reads that immutable file
through the authenticated contents API. The supported contract is exactly one
unindented `CHECK_NAME = "<name>"` assignment whose value is a JSON string;
dynamic Python, single-quoted literals and ambiguous assignments are not
evaluated or accepted. The decoded nonempty single-line name must match the
independently filled `REVIEW_WF`. Bad references, unreadable sources and name
mismatches block. With no reference, the existing name guard is unchanged.
This supplies only name evidence, not a review or a synthetic timestamp: no
matching round still requires a real local-review verdict. `CHECK_APPS`
continues to determine which check runs enter the history. Read that history
with `filter=all`, since GitHub's default `latest` can hide an older round.

The trusted producer must post its aggregate before completing the target
check: `completed_at` closes the aggregate's window. Do not add an arbitrary
grace period to compensate for an unverified producer ordering.

**Where a runner dispatches the round, an empty result has a third reading, and it is on neither surface here.** A dispatch that fails before posting a check run leaves nothing in the target at all — no run, no check run, no `details_url` to follow — so "nothing was dispatched", "the dispatch is still running" and "the dispatch died" collapse into one indistinguishable `[]`. Measured 2026-10-21 on `hyuk-hur/dev-dotfiles#194`: run `39523085356` in `ignite-corp/ai-dev-pr-review-runner` failed at `Mint target reporting token` with a 404, before the step that reports into the target, and both surfaces there stayed empty for it. `RUNNER_REPO` is the gate's name for the repository the round is dispatched from, read from the target's own `AGENTS.md` **on the base branch, never from the PR head's copy** (*Review routing* rule 5) — pinned away from the head for the reason `REMOTE_REVIEW_SOURCE` is, since a value naming a repository the PR author controls turns a run they can name and complete into the `died` reading, the one reading that opens kind (ii) — and left empty where the repository reviews itself — empty is the skip, since there is then no elsewhere for a dispatch to have died in. With it filled, `runner_dispatch` lists the runner's own `review.yml` runs, paginated to exhaustion like every other read here, and keeps the ones whose run-name carries `<owner>/<repo>#<pr>` — the runner titles its runs that way, measured `LENS review hyuk-hur/dev-dotfiles#194`. The newest by `created_at` decides, except that a run still in flight outranks a newer completed one: `died` is the only reading that opens a path forward, and a round still running is one the gate must wait for.

**The run-name is the run's `name`, not its `display_title`.** `run-name:` sets both to the same string — measured 2026-10-22 over the runner's last 100 `review.yml` runs, the two identical on every one and neither ever empty — so the choice shows only in the fallbacks, and that is what decides it. Where no `run-name:` is set, `name` is the workflow's name while `display_title` is the head commit's subject line (measured the same day on a workflow that sets none: `AI Code Review` against `fix(#140): require every reviewer in the aggregate headline for kind (i)`). A commit subject can carry `<owner>/<repo>#<pr>`, because sessions write them that way; a workflow name cannot, so `display_title` lets a run that is not a review answer as one. The needle is matched on **both** edges: the character after it must not be a digit, or `#194` answers for `#1940`, and the character before it must not be one an owner name can contain, or `my-hyuk-hur/dev-dotfiles#194` answers for `hyuk-hur/dev-dotfiles#194` — and the runner reviews every consumer, so other owners' runs are on the same list this one is. One edge is not a boundary.

**The candidates are bound to this head by time, because the run-name carries no SHA.** Every round the PR has ever had matches the same needle, so on the number alone the newest match after a push is the *previous* head's round — completed, and with its check run on the old SHA, which the surfaces here no longer return for the new one. That is the exact shape of a dispatch that died, announced in the seconds the no-dispatch reading exists to wait out. The bound is the moment this SHA became the head, and it must not be a number the commit's author can choose: `.commit.committer.date` is the client's clock, git refreshes it on an amend or a rebase, and `GIT_COMMITTER_DATE` sets it to anything. GitHub's own stamp is the force-push timeline event's `createdAt`, read through GraphQL because REST's `head_ref_force_pushed` carries no SHA — measured on `hyuk-hur/dev-dotfiles#197` and `#198`, `sha: null` on every one — while `commit.pushedDate` is null, GitHub having stopped populating it. Where the event exists it *is* the bound, and the committer date is not consulted: the event is the moment this SHA became the head, so it already precedes every run dispatched for this head, and preferring a later committer date would prefer a client-set value over the server's, which is the one thing the bound exists to prevent. Only a run created at or after the bound can be this head's dispatch, and one created exactly at it counts. **The limit is printed, and it decides nothing**: a force-push event exists for a force-push and for nothing else, so a branch's first push and an ordinary one leave the bound resting on the committer date alone. The gate marks that bound unauthoritative and the `died` arm refuses to classify on it — a completed run after an unauthoritative bound is reported as a run the gate cannot attribute to this head, and the operator waits or re-dispatches rather than falling to kind (ii). The committer date can still produce a WAIT or a re-dispatch, both of which block; it is never what calls a run this head's dead dispatch.

**The timeline connection is walked with a cursor, not with a bigger window.** `last:` has a server-enforced ceiling, measured 2026-10-23 on `hyuk-hur/dev-dotfiles#194`: `last:100` and `last:250` are accepted, `last:300` and `last:900` are refused outright with `EXCESSIVE_PAGINATION`, *"Requesting 300 records on the `timelineItems` connection exceeds the `last` limit of 250 records"*. A read that widens its window therefore fails the first time it needs to, and *"250 works, so up to 250 is fine"* is the inference that produces exactly that. The read asks for `last:100` with `before:` set to the previous page's `startCursor` — verified the same day on `#194`, two pages deep with `last:5`, distinct cursors each time — and the number never moves, not because 100 is a limit but because the cursor makes the window size irrelevant, and an irrelevant number should be small and fixed. The page count is capped, because a loop in a block pasted into someone else's shell must terminate on its own; reaching the cap without a match is not an absent event, so the bound falls to the committer date, unauthoritative, with the number of events read and whether older ones remain both printed. Do not read `totalCount` on this connection: it ignores `itemTypes` and returns the whole timeline's size — measured the same day, `#194` reports 110 against the 20 force-push events the filtered connection holds.

**And the run record names no target head at all**, so the bound is the only thing tying a run to this SHA. Measured 2026-10-23 on `ignite-corp/ai-dev-pr-review-runner`: every `review.yml` run carries the runner's own `head_sha` and `head_branch` — identical across runs for different consumers and different PRs — with `head_commit` the runner's commit, `pull_requests` empty, and the target appearing only in the run-name. Matching the run's `head_sha` against `HEAD_SHA` is therefore not available: it would match nothing and report every head as having no dispatch. What is left open is a run created after this head's bound but dispatched for an earlier head — a webhook delivered late (AT-3007), or a re-dispatch naming a stale head; a round dispatched for the previous head in the ordinary way is created before this head's push and lands in the `earlier` count, which is why that count is printed. The residual cost is bounded by where it lands: such a run read as `running` is a WAIT, which blocks, and read as `died` it sends the operator to `gh run view` on that very run, where the PR head it reviewed is on the screen. Earlier matching runs are reported rather than dropped in silence, because a round the gate must not read is still something the operator wants to know is there.

Every branch of the result is a decision, and one of them is a block:

| Result | Meaning | Action |
|--------|---------|--------|
| `[]` | **No review has run on what you are about to merge.** This is not "quiet" | **BLOCK**, and with `RUNNER_REPO` filled the block says which of three it is, from the runner-side locator above, each read only over runs created since the head bound step 8 defines: **no dispatch** — re-dispatch it, or wait for the webhook (AT-3007); **a dispatch still running** — WAIT, and do not re-dispatch, which cancels it; **a dispatch that died before posting** — where a server-side push stamp bounds this head, the runner could not produce a verdict, so read `gh run view <id> --repo $RUNNER_REPO --log-failed` and fall to kind (ii); with no such stamp the gate reports the run as one it cannot attribute to this head, and blocks. Without `RUNNER_REPO` it is the undiscriminated block: wait and re-ask, and if it stays empty for minutes the workflow did not trigger on this push — find out why. Do not merge on a run that is not there. Where the workflow cannot run at all — Actions blocked on the account, workflow disabled — *Review routing* requires a local review, and its verdict comment on this SHA is what the gate's kind (ii) reads |
| entries present, but **none** has the review workflow's `workflowName` | other CI ran on this head; the review did not | **BLOCK — identical to `[]`.** A green lint or build run is not a review. Read the names, not the count |
| the review workflow's entry has `status` other than `completed` | the round is in flight on this SHA | **WAIT**, re-asking until it is `completed` |
| the selected round is `completed` with `conclusion: success` | a round ran; green does not establish a complete roster (AT-2511) | Read its aggregate's Result, roster and dispositions before accepting kind (i) |
| the selected round is `completed` with `conclusion: failure` and posted a verdict | the runner may withhold approval while still publishing a result | Read the aggregate exactly as above; without a readable approving full-roster aggregate, kind (i) cannot hold |
| the review workflow's entry is `completed` with `conclusion: failure` but **posted no verdict** — the review job never started (0 steps, runner unassigned, GitHub's *"The job was not started because recent account payments have failed"* annotation), or every reviewer was skipped (`Review skipped -- PR too large`) | **no verdict — not a rejection.** The check is red because a required check that never reports stays pending forever, not because the change was judged | **BLOCK on kind (i), and not a negative verdict**: *Review routing* requires a local review, whose comment on this SHA is the gate's kind (ii). Do not read the red check as the reviewer's answer, and do not count it as running for (d) — its status is `completed`. The two observed shapes are under the gate block below |
| the review workflow's entry is `completed` with **any other** `conclusion` — `skipped`, `neutral`, `action_required`, `stale` | the round produced no usable result. A conditional `if:` that evaluated false reports `skipped`, and a skipped review is an absent review | **BLOCK.** Find out why it did not run before merging. Read this row as the default: only `success` or `failure` proceeds to the required approving, full-roster aggregate and disposition checks; every other completed conclusion lands here |

The aggregate the success row sends the gate to read lands on one of two surfaces under one of two identities, chosen by the consumer's configuration of `base-ai-review-aggregate.yml` — the `ALLOW_AUTO_APPROVE` repository variable and the `REVIEWER_APP_ID` / `REVIEWER_APP_PRIVATE_KEY` App credentials. Three shapes, measured 2026-10-02 on each consumer's newest merged PR (#174):

| configuration | surface | identity | bound to the run by | measured on |
|---|---|---|---|---|
| `ALLOW_AUTO_APPROVE` off | issue comment; the headline carries, for example, `posted as comment (auto-approve off)` — that segment is appended to an approve or request-changes verdict only, not to `Comment Only` | `github-actions[bot]` | time — created inside the run's `createdAt`..`updatedAt` | hyuk-hur/dev-dotfiles #168, ignite-corp/ai-dev-pr-review |
| on, no App key | review, state `APPROVED`; the previous head's review is `DISMISSED` | `github-actions[bot]` | commit — `commit_id` is the head, state `APPROVED` or `CHANGES_REQUESTED` | ignite-corp/ai-dev-qa-partner #1195 |
| on, with the App | review, state `APPROVED` / `DISMISSED` | `ignite-ai-review-approver[bot]` | commit — the same | ignite-corp/ai-dev-infra-common #876, ignite-corp/ai-dev-cab #515 |

Both identities have `user.type` `Bot`; the gate's `AGG_AUTHORS` lists exactly those two, and a marker body by any other login is not an aggregate on either surface. The rows are the approval's shape, and identity is per verdict, not per repository (LENS, `aggregate_reviews.py` `post_verdict`, 2026-10-02): a `Changes Requested` review is posted with the default token, so it is `github-actions[bot]`'s on the App repositories too; a `Comment Only` verdict, and the fallback after a review-API failure or a timeout, is an issue comment whatever the killswitch says — the two fallbacks each carry a `> [!] Intended verdict: **<v>** (<reason>, posted as comment)` note after the body, the reason `review API failed` or `review API timed out`, while a `Comment Only` post carries none (AT-2597); and the App mint step is `continue-on-error`, so the `github-actions[bot]` review shape is reachable on an App repository whenever minting fails. The gate therefore applies both logins to both surfaces, reads the marker and the headline, and keys on no note; it never derives an identity from the repository. Why the review surface binds on `commit_id` rather than on the window is in the kind (i) paragraph after the gate block.

**This answer has no memory.** It is about one SHA and stops being an answer the moment the head is a different SHA, so it cannot be carried from earlier in the cycle: re-read `HEAD_SHA` and re-run the query *immediately* before the merge call. The Standard merge block below therefore *contains* this query, and the REST fallback runs that block before its own call — a comment telling the reader to re-check is not a re-check. Then pin the SHA into the merge call itself with `--match-head-commit "$HEAD_SHA"` (Standard merge, below) or REST `-f sha=`, so a head that moves in the last seconds is refused by GitHub rather than by your timing. The pin closes the window after a passing check; it does not establish that a check happened, so it replaces neither the query nor the reading of it. Carrying a stale answer forward is the same defect on a different clock.

Why the block is absolute here rather than advisory: **there is no post-merge recovery.** The review workflow computes `git diff origin/<base>...HEAD`; once the PR is merged that merge-base *is* the head, so the diff is empty and the round reviews nothing — including a round re-triggered by hand afterwards. The only moment the review can happen is before the merge.

A bot *reviewer* that declines to re-review is a different thing from a review *run* that has not completed — see the force-push entry in **Anti-patterns**. A verdict is required — the completed run, or, where it produced none, the local-review comment; any particular bot's review event is not.

After steps 9, 10, 8, re-snapshot. Act on state:

| State | Diagnosis | Action |
|-------|-----------|--------|
| `CLEAN + APPROVED` | Ready — but `APPROVED` says nothing about *this* head: where `dismiss_stale_reviews_on_push` is false the approval is **carried** from the old head, not re-earned (ai-dev-cab#488, 2026-09-11: `reviewDecision: APPROVED` on the update-branch head before its round had run). `reviewDecision` is never evidence a round ran on this SHA; only a verdict on the SHA — Standard merge, condition (a) — is | Merge gate, then merge (see below) |
| `CLEAN + empty` (no APPROVED review) | Normal in a repo whose branch protection does not require an approving review. But `CLEAN` is a statement about **mergeability only** — where there is no ruleset it is not evidence that any review ran, least of all on this head, because there was no server-side gate for it to satisfy. The merging session is the only thing that waits for the round, and after the merge the round can no longer be obtained. So **condition (a)'s verdict-on-the-head-SHA check is this row's precondition, not a nicety**: a `CLEAN` PR whose head has no verdict is a block | Merge gate, then merge (see below) — every condition of it, on the current head |
| `BEHIND` | Branch behind base | `gh pr update-branch N` then re-snapshot. **Update-branch is a push**: it produces a new head (cab#488: `a5df4fa` → `1ed509d`) and triggers a round on it, so the whole merge gate restarts on the new SHA — everything verified a minute earlier described a commit that no longer exists |
| `BLOCKED + APPROVED + 0 unresolved` | Required check failing/pending | Identify check, fix root cause; if check passes but `gh pr merge` still refuses, see REST API fallback below |
| `BLOCKED + REVIEW_REQUIRED` (or `reviewDecision: empty`) | **First, verify with raw data — `mergeStateStatus` / `reviewDecision` can be stale cache.** Run `gh api repos/.../pulls/N/reviews --paginate --jq '.[] | select(.state=="APPROVED") \| {commit_id, user: .user.login}'`. If at least one APPROVED's `commit_id == headRefOid`, the ruleset is actually satisfied — try REST API merge directly (see fallback below). Bot reviewers (`github-actions[bot]`, `claude[bot]`, etc.) count toward `required_approving_review_count` unless CODEOWNERS or `required_reviewers` explicitly excludes them. ONLY if zero fresh APPROVED on current HEAD: wait for reviewer. **Do NOT propose admin merge.** Per `feedback_no_admin_merge_option.md`: admin merge MUST NOT appear in status reports as a path/fallback |
| `UNSTABLE` | Non-required check failed | Often mergeable; verify per-project. For flaky CI: `gh run rerun <run-id> --failed` — only retry transient failures (network, runner provisioning), not legitimate test failures |
| `DIRTY` | Merge conflict | Rebase or merge base; resolve conflicts |

#### Convergence — the round that ends the lane

Converged is a property of a round's findings, not of a round number. **When a round's findings are all against code the previous round added and none of them affects behaviour, the lane has converged**: stop, close the remainder below threshold per *Fix in round, or close below threshold* (Stage 2), merge. A count cap is a backstop and never the test: a cap stops a lane that is still finding real things at round 3, and lets the tail-chasing one run to 7. The five parallel lanes run over rounds 5..11 in 2026-10-06..08 are why the test is by kind — three of them reported the tail-chasing shape unprompted, and no round number separated them from the two that did not.

Two things have to hold of a round before it can be the one that ends the lane, and both are default-deny.

**It must be a full round.** A short-roster round reads as "no new findings" if nothing says otherwise, so a starved round looks like convergence — and the reviewer has been returning 2/3 intermittently on shared capacity, so starved rounds are the common case, not the exotic one. The fraction that decides it is the same one `headline_roster` already reads for kind (i): `N/M reviewers` and `N/M LLM responses` against `EXPECTED_REVIEWERS`. A round short on either is not a round, so it cannot be the round that shows nothing new — the gate refuses it as a verdict, and the lane must refuse it as convergence for the same reason.

**The round report must state, beside the roster, whether the round's findings affect behaviour.** That statement is what made the tail-chasing shape visible in the first place, and nothing else enforces the paragraph above: without it, "these findings are all cosmetic" is an impression rather than a claim someone wrote down and can be held to. A report that does not say is not a report that said no.

`convergence_round` is that decision. It needs `headline_roster` and `EXPECTED_REVIEWERS`, so define those two from the gate block below first — the `EXPECTED_REVIEWERS=` line and the `headline_roster` function, not the whole block: its top-level `exit 1` tails close an interactive shell on the first refusal:

```bash
convergence_round() {
  local h=$1 effect=${2:-}
  headline_roster "$h" \
    || { echo "convergence: refused — this round's roster is short, zero or missing, so it is not a round, and a round that is not a round cannot be the one that shows nothing new"; return 1; }
  case $effect in
    no-behaviour)
      echo "convergence: full roster, and the round report states these findings do not affect behaviour — converged: close the remainder below threshold and merge"; return 0 ;;
    behaviour)
      echo "convergence: full roster, and the round report states these findings affect behaviour — not converged: fix them in this round"; return 1 ;;
    *)
      echo "convergence: refused — the round report must state whether this round's findings affect behaviour, 'behaviour' or 'no-behaviour', and it says '$effect'"; return 1 ;;
  esac
}
```

A refusal here is not a merge block: it says this round is not the one that ends the lane, so the lane runs another round — Stage 2 for the items it raised, fixed in round or closed below threshold by the boundary question. `.github/scripts/gate_headline_drill.sh` in `ignite-corp/ai-dev-pr-review` (deployed as `bin/gate-headline-drill` from `hyuk-hur/dev-dotfiles`) extracts this function from this file by name and runs the measured headlines through it, as it does the gate's own.

#### Standard merge

The merge call runs only when, **on the same head SHA, read immediately before the call**: (a) at least one verdict exists on that SHA — a usable completed workflow/check round whose approving aggregate — a comment or a review, step 8's shape table — reports every configured reviewer, kind (i), or a local-review verdict comment opening with the repository's verdict header for that SHA whose newest instance approves, kind (ii); (b) that head's threads are all resolved; (c) every item each verdict raised has a disposition; and (d) no check on that SHA is still running. Each is a block on its own; a failed one means no merge — back to Stage 2 for the new items (reply + resolve, or a fixup, which pushes a new head, starts a new round, and brings you back to this gate). This block is the gate's only home; paste it whole. (a), (b) and (d) decide for themselves and exit non-zero, loud, on anything but a pass — the step 9 shape, because a query that does not block is not a gate. (c) is a read of the verdicts (a) named, so the merge call is a separate block after it — except the major items a verdict lists in the aggregate's item shape, the aggregate's unreviewed majors and a local verdict's items in that same shape, which the block checks itself on every verdict (a) named and exits non-zero on; what (c) can no longer do is read an empty list as a pass, because (a) refuses to reach it without a verdict.

**Where (c) reads a round's findings: on the verdict comment's body, on the aggregate's body, and on every first-line `round-cutoff-*` comment in the current-head review window and every first-line `inline-fallback-*` comment the PR carries, regardless of round — never on the thread count.** (b) returning zero unresolved threads is a statement about threads, and not about findings: from the convergence cutoff onward a round folds its remaining items into a cutoff comment, and those items carry no thread id at all, so a PR that has finished its reply pass reads 0 unresolved while carrying findings nobody has dispositioned. Observed on four PRs over 2026-10-06..08; the clearest had 11 threads and 0 unresolved, and then a round produced three findings, none of them a thread. When several folded items share a location, quote each item's whole rendered first line separately. A folded item is dispositioned instead by a quoted-response comment, Stage 4's issue-comment pattern, which names the cutoff comment it answers. The worked example to read rather than a description of one: `ignite-corp/ai-dev-pr-review#188`, comment `6015452260` at 2026-10-06T11:39:40Z — three items quoted with their round and their source (`(claude, R7 cutoff comment)` twice, `(claude, R7 verdict body)` once), two `Fixed:` with file:line, one `By design:`, and the cutoff comment it names now minimized `OUTDATED`. `cutoff_findings` in the block below is that read, and it blocks.

What counts as a verdict is the repository's `AGENTS.md`'s to declare, not this skill's: the review workflow's name, which step 8 already requires, and, for a local review, the header its verdict comment opens with. The two kinds are bound differently, and the block below does both. Kind (i)'s aggregate is bound to an identity — `github-actions[bot]` or `ignite-ai-review-approver[bot]`, user type `Bot`, the workflow's own token or its App's — because those are identities no session's account can post under, so they tell the workflow's comment or review from everyone else's. Kind (ii)'s verdict cannot be: every session posts as one account, so an author list there — `Bot`, a `[bot]` suffix, `github-actions`, a login — would be a list of whom to trust rather than of what was said, and the gate carries none for it. With identity unavailable, the content check is the only thing that separates a verdict from a request that quotes one, so kind (ii) is a contract of **position and form, default-deny**: the comment's first non-empty line, whole, is `## LENS 판정 — **<word>** · head `<40-char SHA>`` — `LENS verdict` is accepted in the same slot — with the full head SHA, a prefix being no name. The captured word is the verdict, and it approves only when it is exactly `승인` or, case-insensitively, `APPROVE` / `APPROVED`. Any other word — `차단 1건`, `승인 보류`, `승인 아님`, `CHANGES REQUESTED` — is a verdict that does not approve, and it blocks. A comment whose first line is any other shape is not a verdict, whatever its body says: a review request, a quotation of a verdict, a gate record never count — and must never be posted in the header shape, because the shape is the whole of what the gate reads. On one head the newest verdict decides: an approval followed by a non-approval blocks. This is the contract a local reviewer posts to; the defaults below are LENS's actual header, and a repository that declares its own overrides both regexes. When both kinds exist on the same head, both are dispositioned: two reviewers, not duplicates. What a kind (ii) verdict leaves to the operator it lists in the aggregate's item shape, `- + **major** `<path>[:<line>]` -- <description>`, and the gate checks those the way it checks the aggregate's own: `verdict_dispositions` below runs `major_dispositions` on every verdict (a) named, whichever kind, so an approval that lists an item nobody answered blocks on either path. An item in any other shape is the human read (c) prints, and the verdict word is the reviewer's own disposition of it — a hand verdict has no `Approved with N` headline, so nothing but its item lines can count its majors.

```bash
# Nothing here is carried forward: every condition is read on the head that
# exists NOW, and the merge call names that head.
HEAD_SHA=$(gh pr view N --repo OWNER/REPO --json headRefOid --jq .headRefOid) \
  || { echo "ERROR: could not read the PR head"; exit 1; }
[ "${#HEAD_SHA}" -eq 40 ] || { echo "ERROR: need the full 40-char head SHA, got '$HEAD_SHA'"; exit 1; }

# What counts as a verdict is the repository's AGENTS.md's to declare (Review
# routing, rule 5): the review workflow's name — step 8's literal — and, for a
# local review, the header its verdict comment opens with. The two regexes
# below are the defaults where it declares none. VERDICT_RE must match the
# comment's FIRST non-empty line, whole, with <HEAD_SHA> substituted; the word
# it captures is the verdict, and it approves only if APPROVE_WORDS matches
# that word whole. A first line in any other shape is not a verdict, whatever
# the body says — default-deny, no negative list.
REVIEW_WF='<review workflow name>'
EXPECTED_REVIEWERS='<n>'
AGG_MARKER='<!-- multi-llm-review -->'
AGG_AUTHORS='github-actions[bot] ignite-ai-review-approver[bot]'
CHECK_APPS='ignite-ai-review-approver'
# The repository the round is dispatched from, where a runner reviews this
# one - the repository's own AGENTS.md says which (Review routing rule 5).
# Read it from the BASE branch's AGENTS.md, never from the PR head's, for
# the reason the trusted-producer literal below is also pinned away from
# the head: the head's copy is the PR author's to write, and a value
# naming a repository they control turns a run they can name and complete
# into this gate's `died` reading - the one reading that opens kind (ii)
# and sends the operator past the review round the gate exists to require.
# Empty means this repository reviews itself, and runner_dispatch below is
# never called: there is no elsewhere for a dispatch to have died in.
RUNNER_REPO=''
# Optional trusted producer owner/repo@full40SHA:path, never a PR-head file.
# Its single literal CHECK_NAME supplies name evidence when no check exists.
# The producer must post its aggregate before completing the target check;
# completed_at closes the aggregate window, with no speculative grace period.
REMOTE_REVIEW_SOURCE=''
# How many pages of force-push events the gate's walk reads before it
# stops. A loop in a block pasted into someone else's shell must
# terminate on its own, and reaching this cap without a match is not an
# absent event: the bound falls to the committer date, unauthoritative,
# and the message says how many events were read and that older ones
# remain. The window size the walk asks for is NOT here - it is a
# literal inside the GraphQL query text, where the cursor makes it
# irrelevant and single quotes keep it unexpanded; step 8 says why.
FPUSH_PAGE_CAP=10
VERDICT_RE='^##\s*LENS (?:판정|verdict)\s*—\s*\*\*(?<word>[^*]+)\*\*\s*·\s*head\s*`?<HEAD_SHA>`?\s*$'
APPROVE_WORDS='^(?:승인|(?i:approved?))$'

# A remote review reports an App check run instead of a workflow run here.
# Select the newest identity-matched start before judging its status/result.
# Six unit-separated fields preserve missing values; IFS whitespace would
# collapse empty fields and shift later values into the wrong slots.
discover_round() {
  # shellcheck disable=SC2016  # the $names are jq's, bound with --arg
  jq -rs --arg wf "${REVIEW_WF:-}" --arg apps "${CHECK_APPS:-}" \
    '($apps | split(" ")) as $as
     | ( [.[0][] | select(.workflowName == $wf)
         | {surface: "run", status: (.status // ""), conclusion: (.conclusion // ""), start: (.createdAt // ""), end: (.updatedAt // ""), url: (.url // "")}]
     + [.[1][] | select(.name == $wf and ((.app as $x | $as | index($x)) != null))
         | {surface: "check-run", status: (.status // ""), conclusion: (.conclusion // ""), start: (.started_at // ""), end: (.completed_at // ""), url: (.details_url // "")}] )
     | sort_by([(.start == ""), .start]) | last // empty
     | [.surface, .status, .conclusion, .start, .end, .url] | join("\u001f")'
}

round_usable() {
  local status=$1 concl=$2 surface=$3 start=${4-} url=${5-}
  [ "$status" = completed ] \
    || { echo "(a) kind (i): the newest round on this head is a $surface still '${status:-<none>}' ($url) — step 8's WAIT row: re-ask until it completes. An older completed round on this head is not this head's verdict"; return 1; }
  [ -n "$start" ] \
    || { echo "(a) kind (i) refused: the newest round on this head is a $surface with no start edge, so it cannot be placed against the other rounds on this head or bound to an aggregate — and an older round that can be placed is not this head's round. Per Review routing a local review is required now"; return 1; }
  case $concl in
    success) return 0 ;;
    failure)
      echo "(a) the round's conclusion is 'failure' — read, not trusted: it is the verdict's and not the round's, and the aggregate decides (step 8)"
      return 0 ;;
    *)
      echo "(a) kind (i) refused: '${REVIEW_WF:-}' on this head ended '${concl:-<none>}' as a $surface — step 8's any-other-conclusion row: the round produced no usable result, so nothing inside its window is this head's verdict, however approving it reads. Per Review routing a local review is required now, and (a) blocks below unless one stands"
      return 1 ;;
  esac
}

# Step 8's `[]` row, where both surfaces here are empty, read against the
# repository the round is dispatched from. A dispatch that failed before
# posting a check run leaves nothing on this repository at all - no run,
# no check run, and no details_url to follow - so "nothing was dispatched"
# and "the dispatch died" are the same reading here, and the `[]` row
# without this is a block rather than a discrimination. The run is in
# RUNNER_REPO, under its own `review.yml`, and its run-name carries the
# target: measured 2026-10-21, `LENS review hyuk-hur/dev-dotfiles#194`,
# which is why the target is passed in rather than guessed. Paginated to
# exhaustion for discovery's own reason - the runner reviews every
# consumer, so this repository's run is far down a list a cap would
# answer `none` from. The cost is bounded and measured rather than
# assumed: `review.yml` on the runner held 190 runs on 2026-10-23, two
# pages, and GitHub's 1000-item pagination ceiling is not in sight. It
# matters because the `earlier` count is complete only below that
# ceiling - above it the count would quietly stop being true - so the
# number and its date are recorded here for whoever reads it next.
#
# **The PR number does not identify a head.** The run-name carries no
# SHA, so every round this PR has ever had matches the same needle, and
# the newest match after a push is the PREVIOUS head's round - completed,
# and with its check run on the old SHA, which the surfaces here no
# longer return. Read on the number alone that is a `died`, so the gate
# would announce that the runner could not produce a verdict and send the
# operator to kind (ii) during exactly the seconds the `none` arm exists
# to wait out. $3 is the bound against that, and this function neither
# computes it nor judges it: only a run created at or after $3 can be
# this head's dispatch, the rest are counted and reported but never
# decisive, and what $3 is made of - and what the caller must refuse to
# decide when its server half is missing - is step 8's, above. Read it
# there; it is not restated here, because the copy of it that used to be
# is what sent a reader of this function alone to the very value the
# bound exists not to trust.
#
# $4 is what discovery found on this head, empty by construction at the
# gate's one call site: a completed run beside a round that WAS
# discovered is `posted`, which the gate cannot reach and the drill
# exercises directly. The conclusion is fetched and deliberately not
# branched on: a completed run that posted no check run is a dead
# dispatch whatever it concluded, and the operator reads the reason from
# the run itself, which is why `died` prints `gh run view` - so the
# conclusion is not projected out of the API here either, since a field
# fetched and never read is one a later edit is invited to decide on.
# Newest by created_at among the candidates above the bound, except
# that an in-flight one outranks a newer completed one - `died` is the
# only reading that opens a path forward, so a round still running wins.
# Prints one unit-separated tuple, by discover_round's rule, the same
# fields for every kind and each field meaning the same thing in each:
# `<kind> <id> <status> <url> <n>`, where the kind is `none`, `running`,
# `died` or `posted`, `n` counts the runs that predate this head, and
# `none` leaves the three middle fields empty.
# .github/scripts/gate_headline_drill.sh extracts this function from this
# file by name, as it does the other gate functions.
runner_dispatch() {
  local target=$1 pr=$2 since=$3 found=${4-} raw matched unresolved earlier bad_status newest id status url
  # An absent bound is not "no bound". With `$since` empty the head
  # filter admits every run, including one with no created_at, while
  # the `earlier` count admits none - so the function degrades into
  # exactly the unbounded shape the drill's head-bound control exists
  # to prove dangerous: the previous head's completed round reported as
  # this head's `died`, with nothing counted as having predated the
  # head. The one call site shape-checks the value first, so this is
  # unreachable from there; the drill sources this function standalone,
  # which is the point of being extractable, and an absent bound
  # reading as no bound is the conflation the function exists to end.
  [ -n "$since" ] \
    || { echo "ERROR: runner_dispatch was given no bound for $target#$pr - with none every run matches, which is the unbounded reading this function exists to refuse" >&2; return 1; }
  # And the same argument reaches a bound that is present but unusable.
  # Every comparison below is lexicographic, so a bound that is not an
  # ISO-8601 UTC timestamp does not refuse anything - it sorts: `last
  # Tuesday` puts every `2026-` stamp below it, so the function answers
  # `none` with every run in the `earlier` count, which reads as "nothing
  # was dispatched for this head". The one call site validates the format
  # before calling, so this is unreachable from there, exactly as the
  # empty case is; the drill sources this function standalone, which is
  # what both guards are for.
  case $since in
    [0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9]Z ) : ;;
    * ) echo "ERROR: runner_dispatch was given '$since' as the bound for $target#$pr, which is not an ISO-8601 UTC timestamp - the comparisons here are lexicographic and cannot be made on it" >&2; return 1 ;;
  esac
  # The envelope needs no check of its own, measured rather than
  # assumed: `gh api --jq '.workflow_runs[] | ...'` over a 200 whose
  # body lacks that key exits **1** with `cannot iterate over: null`,
  # with and without `--paginate`, so the read's own guard fires.
  # The same holds for a non-array value and for an error document
  # served as 200. The one shape that yields no bytes and exit 0 is
  # `{"workflow_runs": []}`, which is a real empty answer and is read
  # as one. The drill pins all four.
  raw=$(gh api --paginate "repos/$RUNNER_REPO/actions/workflows/review.yml/runs?per_page=100" \
    --jq '.workflow_runs[] | {id, name, status, created_at, html_url} | @json') \
    || { echo "ERROR: could not list the runs of $RUNNER_REPO's .github/workflows/review.yml - that filename is fixed in this block, not an operator literal like RUNNER_REPO, so a runner whose review workflow is named otherwise 404s here and the block itself is what has to change" >&2; return 1; }
  # Every round this PR has had, oldest first. Two clauses of the
  # match rule have to be here, because a reader holding this block
  # alone cannot read the filter without them:
  #
  #   * The candidate is identified by the run's `name`, never by
  #     `display_title`.
  #   * The needle `<owner>/<repo>#<pr>` is bounded on **both** edges,
  #     so neither a longer number nor a longer owner answers for this
  #     PR.
  #
  # Why those two and not others - what each field falls back to, which
  # fallback can carry the needle, and the measurements behind both -
  # is step 8's, above, and is not repeated here. The head bound is
  # applied below and not in this match, so the runs it excludes can
  # still be counted.
  # shellcheck disable=SC2016  # the $names are jq's, bound with --arg
  matched=$(printf '%s\n' "$raw" | jq -cs --arg t "$target#$pr" \
    '[ .[] | select(((.name // "") | split($t)) as $p
                    | ($p | length) as $n
                    | $n > 1
                    and any(range(0; $n - 1);
                            ($p[.]     | test("[A-Za-z0-9._-]$") | not)
                        and ($p[. + 1] | test("^[0-9]")          | not))) ]
     | sort_by(.created_at // "")') \
    || { echo "ERROR: could not read $RUNNER_REPO's review runs for $target#$pr" >&2; return 1; }
  # A run too young to have resolved its name is not evidence of
  # absence. Measured 2026-10-26 on `ignite-corp/ai-dev-pr-review-runner`
  # run 40296968898, by polling the run list every 3s and re-reading
  # each new run at +1s, +2s, +5s and +10s: created 09:06:00Z, served
  # at 09:06:02Z with `name` AND `display_title` both the bare workflow
  # name, and by 09:06:03Z both carrying the resolved `run-name:`. So
  # for the first seconds of a run's life the
  # needle is absent from every field on this endpoint, and no choice of
  # field avoids it.
  #
  # Keyed on the SHAPE and not on this target's needle: a run whose name
  # carries some other `<owner>/<repo>#<n>` has resolved and is a real
  # non-match, correctly excluded; a run whose name carries no such
  # shape at all has not resolved yet, and is not a non-match. The rule
  # needs no second literal naming the runner's own workflow, and it
  # covers any unresolved rendering rather than only the bare-name one.
  #
  # The refusal is not confined to the `none` reading, though that is
  # where the harm was found: `died` is the reading that opens a path
  # forward, and an unresolved run beside a completed one could be this
  # head's dispatch just started, which would make the answer WAIT. So
  # any unresolved candidate at or after the bound stops the locator and
  # asks for a re-read, which blocks either way.
  #
  # It IS confined to runs that can still be the young one it describes.
  # A name resolves seconds after creation and long before a run
  # finishes, so a `completed` run whose name carries no needle is not
  # too young - it is a run of a workflow that renders no target into
  # its name, which is a plain non-match. Refusing on it would be a
  # standing block that no re-read can clear, under a message telling
  # the operator to wait for something that has already happened, and
  # it would do that for every consumer of the runner at once. The harm
  # this rule exists to stop is unreachable through a completed run:
  # that run is not a dispatch about to start, so no re-dispatch of
  # this head can cancel it.
  #
  # What it costs to get this wrong, which is why it refuses rather than
  # guessing: the `none` arm tells the operator to re-dispatch, and the
  # runner's own `review.yml` carries `concurrency: cancel-in-progress:
  # true`, so that re-dispatch cancels the round that was already
  # running. A hand-dispatch followed at once by a gate read is an
  # ordinary sequence, not a contrived one.
  #
  # This refusal is a pause, not a wall: the name resolves on its own
  # within a few seconds of the run being created, so re-reading ends
  # it without anyone doing anything. It is the one refusal in this arm
  # that clears itself. Measured 2026-10-26 over six runs: every read
  # at 2.2s of age or younger saw the bare name, every read at 3.2s or
  # older saw the resolved one, and the three caught bare resolved
  # 2.6s, 3.2s and 3.6s after creation.
  # shellcheck disable=SC2016  # the $names are jq's, bound with --arg
  unresolved=$(printf '%s\n' "$raw" | jq -s --arg s "$since" \
    '[ .[] | select((.created_at // "") >= $s)
           | select((.status // "") != "completed")
           | select(((.name // "") | test("[A-Za-z0-9._-]+/[A-Za-z0-9._-]+#[0-9]+")) | not) ] | length') \
    || { echo "ERROR: could not check $RUNNER_REPO's runs for $target#$pr for names that have not resolved yet" >&2; return 1; }
  [ "$unresolved" = 0 ] \
    || { echo "ERROR: $RUNNER_REPO has $unresolved unfinished run(s) created since this head whose name carries no <owner>/<repo>#<n> at all, which is a run too young to have resolved its run-name rather than a run for another target - re-read in a few seconds and it will have resolved on its own, no re-dispatch needed. Answering now could report no dispatch for a round that has just started, and the re-dispatch that advice invites would cancel it" >&2; return 1; }
  # Every read is guarded, the local jq passes as well as the one
  # network call, since only `raw` touches the network at all.
  # An unguarded jq here falls through with an empty value and returns
  # 0: an empty `newest` takes the `none` path, which the gate
  # reports as "nothing was dispatched, re-dispatch it", and an empty
  # `earlier` turns the caller's `-eq 0` into a non-numeric test. That
  # is a broken read wearing an absence's clothes, which is the one
  # conflation this function exists to end; it may not be reintroduced
  # inside it.
  #
  # A run with no created_at cannot be shown to be this head's, so it
  # counts as earlier: default-deny, the rule discovery's startless
  # candidate follows.
  # shellcheck disable=SC2016  # the $names are jq's, bound with --arg
  earlier=$(printf '%s' "$matched" | jq --arg s "$since" \
    '[ .[] | select((.created_at // "") < $s) ] | length') \
    || { echo "ERROR: could not count $RUNNER_REPO's runs for $target#$pr that predate this head" >&2; return 1; }
  # A status is refused for being ABSENT, never for being unfamiliar.
  # The defect was that `select(.status != "completed")` passed a
  # missing value, so a malformed record became `running` and the
  # operator waited on nothing. The fix is **not** an allow-list of
  # valid states: a run can legitimately sit in a lifecycle state this
  # gate has never seen, and a list of the valid ones turns that into a
  # hard error on a perfectly normal round.
  #
  # The asymmetry is what decides the direction. A value missing from
  # an allow-list makes a NORMAL state an error; a value missing from a
  # rejection makes a BROKEN record a WAIT - and WAIT blocks. The
  # second failure is the safe one, so the code enumerates what is
  # invalid, which is one thing: nothing there at all.
  #
  # Measured 2026-10-23 over 200 runs, 100 of the runner's `review.yml`
  # and 100 of a consumer's own: 199 `completed`, 1 `in_progress`, no
  # third value. That is what was seen and not what exists, and nothing
  # below decides on it.
  #
  # The refusal carries the same bound as `newest`, and for the reason
  # the refusal exists: only a candidate at or after `$since` can be
  # read as `running`, so only there does a missing status turn into a
  # wait on nothing. A statusless run below the bound reaches the
  # `earlier` count and nothing else, and refusing on it would block
  # this head's gate on a historical run that decides nothing - the
  # safe direction, but a block the design does not need.
  # shellcheck disable=SC2016  # the $names are jq's, bound with --arg
  bad_status=$(printf '%s' "$matched" | jq -r --arg s "$since" \
    '[ .[] | select((.created_at // "") >= $s) | select((.status // "") == "") ] | length') \
    || { echo "ERROR: could not read the statuses of $RUNNER_REPO's runs for $target#$pr" >&2; return 1; }
  [ "$bad_status" = 0 ] \
    || { echo "ERROR: $RUNNER_REPO returned $bad_status run(s) for $target#$pr since this head carrying no status at all - a record the gate cannot read is not a round in flight, and it will not be waited on as one" >&2; return 1; }
  # Among the candidates above the bound, an in-flight one outranks a
  # newer completed one. Newest-wins alone would report `died` - the
  # only reading that opens a path forward - while a round is still
  # running, which concurrency cancellation makes narrow rather than
  # impossible. And no candidate here posted a check run, or discovery
  # would have found it, so a run still running is genuinely in flight
  # and WAIT is the answer. Within each group the newest still decides.
  # shellcheck disable=SC2016  # the $names are jq's, bound with --arg
  newest=$(printf '%s' "$matched" | jq -r --arg s "$since" \
    '[ .[] | select((.created_at // "") >= $s) ] as $since_head
     | (([ $since_head[] | select(.status != "completed") ] | last)
        // ($since_head | last)) // empty
     | [((.id // "") | tostring), (.status // ""), (.html_url // "")] | join("\u001f")') \
    || { echo "ERROR: could not read $RUNNER_REPO's newest run for $target#$pr since this head" >&2; return 1; }
  # Every kind emits those fields in that order, so no slot ever
  # changes meaning: a tuple whose second field was the run id for
  # one kind and the status for another reads correctly only while the
  # caller remembers which, and the next edit to the WAIT arm is the one
  # that forgets. The id and the status are both carried for all of
  # them, so the WAIT message can name the run as well as its state.
  [ -n "$newest" ] || { printf 'none\x1f\x1f\x1f\x1f%s\n' "$earlier"; return 0; }
  IFS=$'\x1f' read -r id status url <<<"$newest"
  if [ "$status" != completed ]; then
    printf 'running\x1f%s\x1f%s\x1f%s\x1f%s\n' "$id" "${status:-<none>}" "$url" "$earlier"
  elif [ -n "$found" ]; then
    printf 'posted\x1f%s\x1f%s\x1f%s\x1f%s\n' "$id" "$status" "$url" "$earlier"
  else
    printf 'died\x1f%s\x1f%s\x1f%s\x1f%s\n' "$id" "$status" "$url" "$earlier"
  fi
}

# Read only an operator-selected immutable producer file, never execute it.
remote_review_name() {
  local reference=$1 repository revision path raw name
  if ! [[ $reference =~ ^([A-Za-z0-9-]+/[A-Za-z0-9._-]+)@([0-9a-f]{40}):([A-Za-z0-9._/-]+)$ ]]; then
    echo "ERROR: remote review source must be owner/repo@full40SHA:path"; return 1
  fi
  repository="${BASH_REMATCH[1]}"; revision="${BASH_REMATCH[2]}"; path="${BASH_REMATCH[3]}"
  if [[ $repository == */. || $repository == */.. || $path == /* || $path == */ || $path == *//* || "/$path/" == */./* || "/$path/" == */../* ]]; then
    echo "ERROR: remote review source contains an invalid repository or path"; return 1
  fi
  raw=$(gh api -H 'Accept: application/vnd.github.raw+json' "repos/$repository/contents/$path?ref=$revision") \
    || { echo "ERROR: could not read the trusted remote review source"; return 1; }
  name=$(printf '%s' "$raw" | jq -Rrs '
    [split("\n")[] | select(test("^CHECK_NAME[ \\t]*="))]
    | if length != 1 then error("need exactly one CHECK_NAME assignment")
      else .[0] | capture("^CHECK_NAME[ \\t]*=[ \\t]*(?<literal>.*?)[ \\t]*$").literal | fromjson
      | if type == "string" and test("\\S") and (explode | all(. >= 32 and . != 127))
        then . else error("CHECK_NAME must be a nonempty single-line string") end end') \
    || { echo "ERROR: trusted source must declare one literal CHECK_NAME JSON string"; return 1; }
  printf '%s\n' "$name"
}

# Kind (i)'s roster. The aggregate's headline is the only place on the PR
# where the configured reviewers are counted (AT-2511): a reviewer cut at the
# step cap leaves the job success, and only here does the cut show. Prints
# the headline's fractions; returns 0 only when the Result word is Approved
# and every configured reviewer AND every LLM response is present. Any other
# Result word — Changes Requested, Comment Only, Review did not run, Review
# skipped — and any short, zero or missing fraction returns 1. `Approved with
# N unreviewed major issue(s)` is not refused here: N is left in agg_majors
# for (c), where each of them needs a disposition. Both denominators must
# equal EXPECTED_REVIEWERS, the count step 8 has the operator read from the
# review workflow: `2/2` is full only against a roster of two, and the
# headline cannot say how many were configured.
# .github/scripts/gate_headline_drill.sh extracts this function from this
# file by name and runs the measured headlines through it — edit it here, and
# the drill follows.

headline_roster() {
  local h=$1 rv_n rv_m llm_n llm_m
  [[ ${EXPECTED_REVIEWERS:-} =~ ^[1-9][0-9]*$ ]] \
    || { echo "(a) kind (i) refused: EXPECTED_REVIEWERS is '${EXPECTED_REVIEWERS:-}', not a positive count — fill it from the review workflow (step 8) before the roster can be judged"; return 1; }
  local result_re='^\*\*Result: \[(OK|!)\] Approved( with ([0-9]+) unreviewed major issue\(s\))?( \|[^*]*)?\*\* -- '
  local rev_re='([0-9]+)/([0-9]+) reviewers'
  local llm_re='([0-9]+)/([0-9]+) LLM responses'
  [[ $h =~ $result_re ]] \
    || { echo "(a) kind (i) refused: the aggregate's Result is not Approved — not a verdict that approves: $h"; return 1; }
  agg_majors=${BASH_REMATCH[3]:-0}
  [[ $h =~ $rev_re ]] \
    || { echo "(a) kind (i) refused: no 'N/M reviewers' fraction in the aggregate headline — no roster, no verdict: $h"; return 1; }
  rv_n=${BASH_REMATCH[1]}; rv_m=${BASH_REMATCH[2]}
  [[ $h =~ $llm_re ]] \
    || { echo "(a) kind (i) refused: no 'N/M LLM responses' fraction in the aggregate headline — no roster, no verdict: $h"; return 1; }
  llm_n=${BASH_REMATCH[1]}; llm_m=${BASH_REMATCH[2]}
  echo "(a) kind (i) roster: reviewers $rv_n/$rv_m, LLM responses $llm_n/$llm_m, unreviewed major issues $agg_majors"
  [[ $rv_m -eq $EXPECTED_REVIEWERS && $llm_m -eq $EXPECTED_REVIEWERS ]] \
    || { echo "(a) kind (i) refused: denominators $rv_m reviewers / $llm_m LLM responses, but $EXPECTED_REVIEWERS reviewers are configured — the round was not the configured round"; return 1; }
  [[ $rv_n -eq $rv_m ]] \
    || { echo "(a) kind (i) refused: $rv_n/$rv_m reviewers — a configured reviewer did not report (AT-2511); the job is green, the round is not complete"; return 1; }
  [[ $llm_n -eq $llm_m ]] \
    || { echo "(a) kind (i) refused: $llm_n/$llm_m LLM responses — a reviewer ran and produced no response; the job is green, the round is not complete"; return 1; }
  return 0
}

# Kind (i)'s comment-surface window. A comment aggregate is bound to the
# run by time, so both bounds must exist: a run whose updatedAt is null —
# still being written, or a field the API left out — has no closing edge,
# and a window with no closing edge admits every later comment on the PR.
# Prints the two bounds; returns 1, loud, when either is not a timestamp.
# Extracted by name by the drill, like the functions below.
window_bounds() {
  local s=$1 e=$2 ts_re='^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$'
  [[ $s =~ $ts_re ]] || { echo "(a) kind (i) refused: the run's createdAt is '$s', not a timestamp — no window"; return 1; }
  [[ $e =~ $ts_re ]] || { echo "(a) kind (i) refused: the run's updatedAt is '$e', not a timestamp — the window has no closing edge, so it admits nothing (AT-2511)"; return 1; }
  printf '%s %s\n' "$s" "$e"
}

# Kind (i)'s aggregate, on either surface the workflow posts it to. The
# consumer's configuration chooses surface and identity (step 8's shape
# table): ALLOW_AUTO_APPROVE off posts an issue comment as
# github-actions[bot]; on, an approval as a review by github-actions[bot];
# on with an App key, by ignite-ai-review-approver[bot]. That is the
# approval alone: a request_changes review is always github-actions[bot]'s,
# and a comment verdict or the fallback after a review-API failure or
# timeout is an issue comment on any configuration — so the author list
# applies to both surfaces and nothing here maps a repository to an
# identity. On both surfaces the author is one of AGG_AUTHORS with
# user.type Bot and the body carries AGG_MARKER. A comment names no SHA, so it is bound to the run by time:
# created inside the run's createdAt..updatedAt. A review names its head,
# so it is bound by commit: commit_id equal to the head, and in a state
# the producer posts it in -- APPROVED or CHANGES_REQUESTED, the two events
# post_verdict passes `gh pr review`; every other outcome of a round is an
# issue comment, and the producer's only COMMENTED review is the inline
# one, whose body is empty. A COMMENTED marker review is therefore no
# aggregate, whatever its headline says. The two clauses do different work. The commit equality is what refuses
# the previous head's approval: the producer folds only its own
# CHANGES_REQUESTED marker reviews and never an approval, and on every
# measured PR the previous head's approval was dismissed by this account's
# Stage 4 loop, not by the workflow — a consumer that does not run that
# loop keeps it APPROVED. The state clause, beyond refusing COMMENTED, is
# what refuses a review on THIS head that has been folded (DISMISSED): the operator's approval after a Stage 4
# re-run on a posted head, or the producer's own superseded
# CHANGES_REQUESTED. A candidate with no timestamp is refused: it could not be
# ordered against the others, and (c) reads what came after it. Reads two
# JSON documents on stdin, the comment list then the review list, both in
# the normalised shape the gate builds; prints the newest candidate across
# both surfaces with its surface named, or nothing. Extracted by name by
# the drill, like the functions around it.
select_aggregate() {
  local head=$1 s=$2 e=$3
  # shellcheck disable=SC2016  # the $names are jq's, bound with --arg
  jq -cs --arg m "$AGG_MARKER" --arg a "$AGG_AUTHORS" --arg h "$head" --arg s "$s" --arg e "$e" \
    '($a | split(" ")) as $as
     | def ours: .type == "Bot" and ((.author as $x | $as | index($x)) != null)
                 and ((.created_at | type) == "string") and (.body | contains($m));
     ( [.[0][] | select(ours and .created_at >= $s and .created_at <= $e) | . + {surface: "comment", state: "comment"}]
     + [.[1][] | select(ours and .commit_id == $h and (.state == "APPROVED" or .state == "CHANGES_REQUESTED")) | . + {surface: "review"}] )
     | sort_by(.created_at) | last // empty'
}

# The review list must be complete, the way the comment list must: the
# producer spares approvals when it folds, so two runs on one head can
# leave an APPROVED marker review under a newer CHANGES_REQUESTED one, both
# non-dismissed on HEAD_SHA, and newest-wins picks the block — unless a
# dropped page lost the newer one, which promotes the approval. Reads the
# review list on stdin and the PR's own review count; prints the two, or
# blocks, loud, when they differ or the count is not a number. Extracted
# by name by the drill.
reviews_complete() {
  local total=$1 seen
  seen=$(jq length) || { echo "BLOCK (a): could not count the reviews read"; return 1; }
  [ "$seen" -eq "$total" ] 2> /dev/null \
    || { echo "BLOCK (a): read $seen reviews but the PR counts '$total' — a page was missed or a review landed mid-read; re-run"; return 1; }
  echo "(a) reviews: $seen read, $total on the PR"
}

# The aggregate's headline: the first `**Result:` line of its body, on
# either surface. What the producer appends after the body is not read —
# `> [!] Intended verdict: **<v>** (review API failed|timed out, posted as
# comment)` on a review-API failure or timeout (AT-2597, producer side),
# `> [!] Auto-approve withheld …` on a short quorum — and the gate keys
# on no note: a comment-only post carries none, so a marker-and-headline
# comment with no note is the aggregate, present.
# Reads the candidate on stdin; prints the headline, or nothing.
# Extracted by name by the drill.
aggregate_headline() {
  jq -r '.body | gsub("\r"; "") | split("\n") | map(select(startswith("**Result:"))) | .[0] // ""'
}

# Does a review thread answer a major? Threads arrive one per line as
# path, the thread's line(s) comma-joined (its current line and its original
# one), `replied` or `unreplied`, and the first comment's first line — the
# (b) query's shape — with the fields separated by the unit separator
# (0x1f), not a tab: a tab is IFS whitespace, so `read` collapses a run of
# them and an empty field vanishes. A file-level thread has no line, and
# its row read as path, `replied` in the lines field and the body in the
# reply field, so the reply guard skipped it, and the description route —
# the only one a line-less thread has — was unreachable for exactly the
# threads it exists for (#194 round 10). A thread answers only if its path
# is the item's AND it carries a reply from a non-Bot account — a thread
# resolved with no reply is a silent close, which Resolve Policy forbids, so
# the gate does not count it — AND either its line equals the finding's
# line or its first comment IS the producer's rendering of the finding: the
# line post_inline_comments.py's build_comments writes first,
# `<icon> **<severity>** (<reviewer>): <description>`, with a major's own
# icon and severity and the description whole to the end of the line; the
# suggestion the producer appends sits two lines down and is not read. The
# description is compared by equality once the prefix has matched, not
# searched for — a substring of free text is not a binding: until round 10
# of #194 the test was `*"$desc"*`, so a same-path thread whose first
# comment mentioned the description in passing answered the finding
# (codex), the class round 5 closed for the cutoff response and round 7 for
# the marker. The second match is content-bound, not distance-bound: the
# producer posts no thread when the line is outside the diff or when a
# similar thread sits within five lines (post_inline_comments.py), so an
# answered major can be re-listed a few lines from the thread that answered
# it, and a distance rule would admit an unrelated thread at the same
# distance.
thread_answers() {
  local threads=$1 path=$2 lineno=$3 desc=$4 t_path t_lines t_rep t_body
  local finding_re='^\+ \*\*major\*\* \([^)]+\): (.*)$'
  while IFS=$'\x1f' read -r t_path t_lines t_rep t_body; do
    [ "$t_path" = "$path" ] || continue
    [ "$t_rep" = replied ] || continue
    if [ -n "$lineno" ] && [[ ",$t_lines," == *",$lineno,"* ]]; then return 0; fi
    if [ -n "$desc" ] && [[ $t_body =~ $finding_re ]] && [ "${BASH_REMATCH[1]}" = "$desc" ]; then return 0; fi
  done <<<"$threads"
  return 1
}

# One optional equivalence key for the supported verdict/folded renderings:
# severity, location and the complete recoverable description line. Internal
# whitespace and description suffixes are significant; trailing whitespace
# follows the existing folded-quote contract. Discovery stays independent of
# this strict decoder, so unsupported raw fields retain their legacy routes.
# --jq exports this same definition for the folded reader, not a second copy.
finding_key() {
  # One decoder for both consumers; unsupported raw renderer fields retain
  # the existing matching routes instead of disappearing from discovery.
  # shellcheck disable=SC2016  # jq owns the captured field variables.
  local program='def finding_key:
    gsub("\r"; "")
    | sub("^[ \\t]*(?:>[ \\t]*)+"; "")
    | sub("^[ \\t]+"; "")
    | sub("^(?:[-*+!?][ \\t]+){0,2}"; "")
    | sub("[ \\t]+$"; "")
    | ([capture("^\\*\\*(?<location>.+?:[0-9]+)\\*\\* -- \\S+ \\*\\*(?<severity>critical|major|minor|suggestion)\\*\\* \\([^()\\r\\n]*\\): (?<description>.+)$")] | .[0] // null) as $fold
    | if $fold != null then [$fold.severity, $fold.location, $fold.description]
      else ([capture("^\\*\\*(?<severity>critical|major|minor|suggestion)\\*\\*(?: \\x60(?<location>[^\\x60]+)\\x60)? -- (?<description>.+)$")] | .[0] // null) as $verdict
      | if $verdict != null then [$verdict.severity, ($verdict.location // ""), $verdict.description]
        else null end end;';
  if [ "$1" = --jq ]; then printf '%s\n' "$program"; return 0; fi
  jq -nc --arg line "$1" "$program"' $line | finding_key | select(. != null)'
}

# (c)'s mechanical half for kind (i). The aggregate lists each major as
# `- + **major** `<path>[:<line>]` -- <description>` (the path is optional:
# `file: null` is a legal payload and renders without the path part). An
# item is answered when thread_answers finds a replied thread for it —
# resolved, since (b) already demanded zero unresolved — or when a later
# comment from someone other than the workflow quotes it with `>`: the quote
# must carry the item's whole rendered text from `**major**` to the end of
# its description line, verbatim, or a supported folded rendering with the
# same finding_key. Both share one decoder; a pathless item has an empty
# location in its key and no folded producer counterpart.
# Nothing shorter is the item's own: a description prefix is shared by
# near-identical findings and repeated by other findings' prose, and a
# backticked `path:line` is a token reviewers routinely write inside OTHER
# items' descriptions when they refer to a location, so quoting item 2 whose
# prose mentions item 1's key would answer item 1. Starting the needle at
# `**major**` rather than at the bullet lets a quote survive the bullet
# being stripped, re-indented, or nested under a second `>`. An item with
# no path has only the quote route: there is no position for a thread to be
# at. The key splits into path and line only when what follows
# the last colon is a number — a path may itself contain colons. Anything
# else is a block, and so is a body that lists fewer majors than its
# headline counted: an item the gate cannot find is an item nobody
# answered. Arguments: the aggregate body, the headline's count, the thread
# lines in thread_answers' shape, the `>` lines from later non-workflow
# comments one per line. Extracted by name by the drill, like
# headline_roster.
major_dispositions() {
  local body=$1 want=$2 threads=$3 quotes=$4 line key path lineno desc needle n=0 bad=0 key_program qkeys item_key
  # shellcheck disable=SC2016  # the backticks are the aggregate's own
  local item_re='^- \+ \*\*major\*\* `([^`]+)` -- (.*)$'
  local nopath_re='^- \+ \*\*major\*\* -- (.+)$'
  local loc_re='^(.*):([0-9]+)$'
  local quote quote_re='^[[:space:]>+-]*(\*\*major\*\*.*)$'
  key_program=$(finding_key --jq) || { echo "(c) ERROR: could not read the finding-key decoder"; return 1; }
  qkeys=$(printf '%s\n' "$quotes" | jq -Rrc "$key_program"' finding_key | select(. != null)') \
    || { echo "(c) ERROR: could not normalize the quoted finding lines"; return 1; }
  while IFS= read -r line; do
    [[ $line == '- + **major**'* ]] || continue
    n=$((n + 1))
    if [[ $line =~ $item_re ]]; then
      key=${BASH_REMATCH[1]}; desc=${BASH_REMATCH[2]}
      if [[ $key =~ $loc_re ]]; then path=${BASH_REMATCH[1]}; lineno=${BASH_REMATCH[2]}; else path=$key; lineno=""; fi
    elif [[ $line =~ $nopath_re ]]; then
      key=""; desc=${BASH_REMATCH[1]}; path=""; lineno=""
    else
      echo "(c) BLOCK: a major item in a shape the gate cannot key — answer it, then re-run: $line"; bad=$((bad + 1)); continue
    fi
    # The needle is the line itself minus its bullet — one derivation for
    # both shapes, so neither re-assembles the separators from captures and
    # the two cannot drift apart.
    needle=${line#- + }
    item_key=$(finding_key "$line") \
      || { echo "(c) ERROR: could not normalize the major item: $line"; return 1; }
    if [ -n "$path" ] && thread_answers "$threads" "$path" "$lineno" "$desc"; then
      echo "(c) major answered by a replied thread on $path: $line"
    elif [ -n "$item_key" ] && printf '%s\n' "$qkeys" | grep -xF -- "$item_key" > /dev/null; then
      echo "(c) major answered by a later quoted reply with the same finding key: $line"
    elif (
      while IFS= read -r quote; do
        [[ $quote =~ $quote_re ]] && [ "${BASH_REMATCH[1]}" = "$needle" ] && exit 0
      done <<<"$quotes"
      exit 1
    ); then
      echo "(c) major answered by a later quoted reply: $line"
    else
      echo "(c) BLOCK: major with neither a replied thread on its path at its line or opening with the producer's rendering of it, nor a later reply quoting '$needle': $line"; bad=$((bad + 1))
    fi
  done <<<"$body"
  [ "$n" -eq "$want" ] \
    || { echo "(c) BLOCK: the headline names $want unreviewed major issue(s) but the body lists $n — cannot show each was answered"; return 1; }
  [ "$bad" -eq 0 ]
}

# (c)'s one call site, for every verdict (a) named. Reads the source list
# (a) built — one record per verdict kind this head has: kind, a label
# naming its surface and id, created_at, since (where its round's comments
# start: the run's createdAt for kind (i), the first verdict's timestamp
# for kind (ii)), body, and want, the major count the verdict declares —
# null where nothing declares one, and the body's own item lines are then
# counted: kind (i)'s headline counts, a kind (ii) verdict has no headline,
# so for it the tally inside major_dispositions cannot catch a short body
# and the per-item checks are the whole of the mechanical half. For every
# source, whichever kind: when it names majors, major_dispositions reads
# its body against the thread lines and the `>` lines of every later
# non-Bot comment, and a block is a block; then the source and every
# comment from its round's start are printed for the human half of (c).
# Nothing here asks which kind a source is. Until round 9 of #194 the
# majors check sat under `if [ -n "$kind_i" ]`, so a kind (ii) approval
# listing a major reached the merge block with the item only printed
# (codex) — the fourth check the local path did not reach, after the
# window's flag (round 5), its run list (round 8) and the cutoff reader's
# author filter (AT-2803); one loop over a list (a) builds is what a fifth
# instance would have to be added to on purpose. Every read carries the
# failure tail: an unreadable list is not an empty one, and an empty
# source list here is a read that failed, since (a) exits without a
# verdict. Arguments: the thread lines in thread_answers' shape, the source
# list, the comment list in the shape the gate builds. Extracted by name by
# the drill, like the functions above.
verdict_dispositions() {
  local threads=$1 sources=$2 comments=$3 n rows src fields kind label at since want body quotes rc=0
  local ts_re='^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$'
  n=$(printf '%s' "$sources" | jq length) \
    || { echo "ERROR: could not count the verdict sources — an unreadable list is not 'no verdict'"; return 1; }
  [ "$n" -gt 0 ] \
    || { echo "ERROR: no verdict source reached (c) although (a) passed — nothing to read is not nothing to answer"; return 1; }
  rows=$(printf '%s' "$sources" | jq -c '.[]') \
    || { echo "ERROR: could not read the verdict sources one by one"; return 1; }
  while IFS= read -r src; do
    # Joined by the unit separator, as thread_answers' rows are: a null
    # timestamp is an empty field, and a tab-separated read would collapse
    # it and shift the fields after it, so the refusal below would name the
    # wrong value as missing.
    fields=$(printf '%s' "$src" | jq -r \
      '[.kind, .label, .created_at, .since,
        ((.want // (.body | gsub("\r"; "") | split("\n") | map(select(startswith("- + **major**"))) | length)) | tostring)]
       | join("\u001f")') \
      || { echo "ERROR: could not read a verdict source's fields"; return 1; }
    IFS=$'\x1f' read -r kind label at since want <<<"$fields"
    # An empty or malformed timestamp would admit every quote on the PR as
    # later than the verdict, and an empty round start would print nothing
    # as 'no comments'; a source the gate cannot place is refused, not read.
    [[ $at =~ $ts_re && $since =~ $ts_re && $want =~ ^[0-9]+$ ]] \
      || { echo "ERROR: verdict source $label (kind $kind) has no timestamp, no round start or no major count — '$at', '$since', '$want'"; return 1; }
    body=$(printf '%s' "$src" | jq -r '.body | gsub("\r"; "")') \
      || { echo "ERROR: could not read the body of verdict source $label"; return 1; }
    quotes=$(printf '%s' "$comments" | jq -r --arg t "$at" \
      '.[] | select(.created_at > $t and .type != "Bot") | .body | gsub("\r"; "") | split("\n")[] | select(startswith(">"))') \
      || { echo "ERROR: could not read the quoted lines after verdict source $label — unreadable is not 'nothing quoted'"; return 1; }
    if [ "$want" -gt 0 ]; then
      echo "(c) verdict kind ($kind) $label names $want major item(s) — an approval is not a pass on those; each must already be answered:"
      major_dispositions "$body" "$want" "$threads" "$quotes" \
        || { echo "BLOCK (c): a major item of verdict kind ($kind) $label has no disposition — back to Stage 2"; rc=1; }
    fi
    echo "(c) READ kind ($kind) $label — the verdict, then the comments since its round started at $since; every item without an inline thread needs a quoted response (Stage 4's issue-comment pattern) in a comment after it, or it is a block:"
    printf '%s\n' "$body"
    printf '%s' "$comments" | jq --arg t "$since" '[.[] | select(.created_at >= $t)]' \
      || { echo "ERROR: could not print the comments since $since — an unprinted list is not 'no comments'"; return 1; }
  done <<<"$rows"
  return $rc
}

# (c)'s cutoff window start. Reads the run list on stdin — every run on
# HEAD_SHA as (a) fetched it, every workflow and every conclusion — and
# takes the review workflow's name and the first local-review verdict's
# timestamp, empty when the head has none. Prints the earliest of every
# review-workflow run's createdAt and that timestamp; prints nothing when
# the head has neither, which is a result; returns 1, loud, when the list
# cannot be read or a review-workflow run's createdAt is not a timestamp —
# a run dropped for want of one could be the earliest, and a window that
# starts after it is the defect (c) records.
# The invariant this holds: the window includes every round-cutoff-*
# comment on this head, whatever any run's conclusion and however many
# runs there are. The producer posts a cutoff comment from inside a
# review-workflow run on this head, so every such comment is created at or
# after the earliest such run's createdAt; the conclusion that run reaches
# later and the runs that follow it do not move when it started. The list
# is read with no filter but the workflow's name, so no refusal in (a) —
# roster, conclusion, aggregate — can narrow it: rounds 5 and 8 of #194
# each found the start read off a value something else selected (the
# roster flag; the success-filtered run list), and this reads it off none.
# What remains is the list's completeness, which the limit check beside
# its fetch holds, and the name itself, which review_workflow_known holds
# before anything reads it: a name no run carries leaves $starts empty,
# and this reads an empty $starts as "no run on this head" — the result
# when the name is right, and the first verdict alone when it is not.
# Extracted by name by the drill, like the functions above.
cutoff_window_start() {
  local wf=$1 first_verdict=$2
  jq -r --arg wf "$wf" --arg v "$first_verdict" \
    '[ .[] | select(.workflowName == $wf) | .createdAt ] as $starts
     | if any($starts[]; type != "string" or (test("^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$") | not))
       then error("a \($wf) run on this head has no createdAt timestamp") else . end
     | ($starts + [ $v | select(. != "") ]) | sort | first // empty' \
    || { echo "ERROR: could not read the cutoff window start from the run list — an unreadable list, or a '$wf' run whose createdAt is not a timestamp, is not 'no run'"; return 1; }
}

# REVIEW_WF, before anything reads it. Every reader below — run_window's
# filter, cutoff_window_start's, the (a) lines that name it — matches the
# name by equality, and each reads a name that matches nothing as "no run":
# the placeholder this block ships with, `<review workflow name>`, matches
# nothing, and so does a name copied from the wrong repository, and until
# round 12 of #194 nothing checked either (claude). Kind (i) then never
# holds, which blocks only until a local verdict is posted, and then (c)'s
# cutoff window starts at that verdict rather than at the review run's
# createdAt — the class rounds 5 and 8 closed, through a different
# selector, the name filter. Takes the name and the repository's workflow
# names one per line — the list step 8 has the operator read the name
# from — and reads the run list on stdin. An empty name or one still in
# angle brackets is refused, as headline_roster refuses EXPECTED_REVIEWERS'
# placeholder. A name some run on this head carries needs nothing else:
# the list proves it. A name no run carries is checked against the
# repository: a name the repository lacks cannot be the review workflow's
# and is refused; a name it has with no run on this head is step 8's first
# two rows — `[]`, and "entries present, none the review workflow's", the
# same state — printed here and left to (a), which blocks without a kind
# (ii) verdict and passes with one, Review routing's path where the
# workflow cannot run. That state is not refused here because the run list
# cannot tell a review workflow that did not trigger from one that is
# disabled, and the window start is right either way: no run on this head
# posted a cutoff comment, so the first verdict is the earliest thing on
# the surface. Extracted by name by the drill, like the functions above.
review_workflow_known() {
  local wf=$1 names=$2 runs on_head seen
  if [ -z "$wf" ] || [[ $wf == '<'*'>' ]]; then
    echo "ERROR: REVIEW_WF is '$wf', not a workflow name — fill it from the review workflow (step 8) before the gate can place the review round"; return 1
  fi
  runs=$(cat) || { echo "ERROR: could not read the run list for '$wf' runs"; return 1; }
  on_head=$(printf '%s' "$runs" | jq --arg wf "$wf" '[ .[] | select(.workflowName == $wf) ] | length') \
    || { echo "ERROR: could not read the run list for '$wf' runs — an unreadable list is not 'no run'"; return 1; }
  if [ "$on_head" -gt 0 ]; then echo "(a) '$wf': $on_head run(s) on this head"; return 0; fi
  printf '%s\n' "$names" | grep -qxF -- "$wf" \
    || { echo "ERROR: REVIEW_WF is '$wf', which names no workflow in this repository and no run on this head — a name the repository lacks cannot be the review workflow's; fill it from gh workflow list (step 8)"; return 1; }
  seen=$(printf '%s' "$runs" | jq -r '[ .[] | .workflowName ] | unique | join(", ")') \
    || { echo "ERROR: could not read the workflow names on this head"; return 1; }
  echo "(a) no '$wf' run on this head — step 8's no-review-run rows: the review did not run on what you are about to merge (runs here: ${seen:-none}); kind (i) cannot hold, and (a) blocks below unless a local verdict stands (Review routing)"
}

# (c)'s other surface: the items a convergence cutoff folded out of the
# threads. Measured on ignite-corp/ai-dev-pr-review#188 — comment
# 6015282943 by github-actions[bot], first line `<!-- round-cutoff-claude-r7
# -->`, listing two items that were never threads, on a PR whose reply pass
# was finished. The format is the producer's — in ignite-corp/ai-dev-pr-review
# `.github/scripts/post_inline_comments.py`, `_CUTOFF_MARKER` defines it and
# `post_cutoff_summary` emits it as the comment's first line, through
# `_post_folded_comment`, which writes the marker it is handed on line one —
# so a suffix appended there falls outside the anchored regex below and this
# read misses the comment and passes. So (b)'s zero unresolved is not zero
# findings, and this is the read that says so.
# A cutoff comment is answered by later comments from non-Bot accounts,
# none of them a cutoff comment itself, that, between them, **name the
# cutoff comment's own marker** and quote every item it lists, each
# identified by its location — #188's comment 6015452260 answering
# 6015282943 is the shape. Naming the marker binds the
# response to this surface rather than to the threads. The location binds
# the response to the item: the producer renders each item as
# `- **<path>:<line>** -- <severity> (<reviewer>): <text>`, so every item
# opens with a location, and the measured response names both of #188's
# (`.github/scripts/aggregate_reviews.py:61` and
# `.github/scripts/tests/test_aggregate_reviews.py:44`, once each, on its
# `>` lines) while rewriting the item text as it quotes it — which is why a
# verbatim needle like major_dispositions' would refuse that response and a
# `path:line` needle does not. Until round 5 of #194 the binding was the
# marker plus any `>` line, so a reply that named the marker and quoted
# something unrelated answered the whole comment (codex). The location must
# sit on a `>` line and match whole — `x.py:6` is not `x.py:61` — and the
# union across replies is what counts, not one reply, because dispositions
# split across comments legitimately; at least one of them must still name
# the marker, and that is the whole of what the marker scan asks of a
# reply. Until round 10 of #194 it also asked that same reply for a `>`
# line, round 5's remnant, so a split with the marker in one reply and the
# quotes in a sibling blocked against the union this comment promised
# (codex). The `>` requirement is Stage 4's quoting form and not a second
# binding: the location check below carries it, refusing a location
# mentioned in prose, nothing more. What this asserts is only that each
# item was addressed by name; whether the disposition is adequate stays the
# human read in (c), which is why the gate prints these. Both convergence
# cutoff and inline-API fallback comments carry findings without threads.
# Main's fallback marker has no round suffix. Pending PR #191 at
# 49e7c61029c7ad1fb3de94f13eb5cf1e6f025602 adds a round-scoped form;
# it is accepted here for forward compatibility, not emitted by main yet.
# Each location must be quoted, and when several items share one location,
# each item's whole rendered first line must be quoted separately. A list
# with no recognized items, malformed declared metadata, or a declared count
# differing from recognized items blocks. Unescaped legacy bodies do not
# make every arbitrary malformed mixed item distinguishable from prose.
# Reads the comment list on stdin, in the shape the gate builds.
# Arguments: the window start, as cutoff_window_start prints it — the
# earliest review-workflow run's createdAt on the head or the first verdict's
# timestamp, whichever is earlier, so no round's cutoff is missed. Prints
# one line per cutoff comment at or after it and returns 1 for any with no
# such response. All inline-fallback history remains in scope: the legacy
# producer deduplicates that marker across heads, and a reviewer name can
# end in -rN, so a suffix alone cannot establish round scope. Even newer
# round-suffixed fallbacks are retained conservatively until dispositioned;
# count/head metadata is not used as authentication or permission to skip one.
# The marker scan's own
# `select` is the only thing that decides what is a cutoff comment, so a
# comment with no marker — the aggregate's own, every other bot comment in the
# window — never reaches the loop; a second guard inside the loop would make
# the scan unfalsifiable, which is what the drill's control found. The scan
# is author-blind, and must be: the same producer runs under three
# identities — `github-actions[bot]`, the reviewer App, and the operator's
# own `gh` login via `review_pr_local.py`, whose author type is `User` —
# so an author filter on `.type == "Bot"` dropped a locally-driven round's
# cutoff comment, printed `no round-cutoff comment on this round's
# surface` and let (c) pass over findings nobody dispositioned (AT-2803;
# reachable, not yet observed, because the cutoff only fires at
# ROUND_CUTOFF_N and `fetch_round_count` counts Bot verdicts only — a
# mixed drive reaches it). `cutoff_marker` recognizes the producer format,
# not an authenticated identity: anyone can post that first-line marker.
# Such a comment can conservatively add a blocker, never grant approval.
# No personal account allowlist applies to responses: another non-Bot
# reviewer may answer, and adequacy of each disposition stays the human read.
# The first-line shape also keeps the regress out: a response names the marker
# inline in backticks, never as a line of its own, so it stays an answer
# and is never mistaken for the thing it answers. The reply scans exclude
# it by the same shape rather than by author, for the same reason: under
# the local driver a producer comment is a `User` too, and
# `.type != "Bot"` alone would let the producer's next cutoff comment
# answer its own earlier one. Extracted by name by the drill, like the
# functions above.
cutoff_findings() {
  local since=$1 comments markers rc=0 id at marker items n_items missing rx key_program
  # jq's regex-escape for the two literals the reply is bound to below, the
  # marker and each item's location, and the producer's own signature: a
  # first line that is nothing but the marker's HTML comment. One
  # definition each, prepended to all three programs.
  # shellcheck disable=SC2016  # jq's own text, not shell
  rx='def rx: gsub("(?<c>[.^$*+?()\\[\\]{}|\\\\])"; "\\" + .c);
      def cutoff_marker: ((((.body // "") | gsub("\r"; "") | split("\n") | (.[0] // "")
        | [capture("^<!--\\s*(?<m>(?:round-cutoff|inline-fallback)-\\S+?)\\s*-->$") | .m]) | .[0]) // "");'
  key_program=$(finding_key --jq) || { echo "(c) BLOCK: could not read the finding-key decoder"; return 1; }
  rx="$key_program $rx"
  comments=$(cat) || { echo "(c) BLOCK: could not read the comments for the cutoff surface"; return 1; }
  markers=$(printf '%s' "$comments" | jq -r --arg s "$since" "$rx"'
     [ .[]
       | select((.created_at | type) == "string")
       | select(.created_at >= $s or (cutoff_marker | startswith("inline-fallback-")))
       | [ (.id | tostring), .created_at, cutoff_marker ]
       | select(.[2] != "")
     ] | .[] | @tsv') \
    || { echo "(c) BLOCK: could not read the cutoff comments"; return 1; }
  [ -n "$markers" ] || { echo "(c) no round-cutoff or inline-fallback comment on this round's surface"; return 0; }
  while IFS=$'\t' read -r id at marker; do
    # A marker must not be followed by another ASCII marker character:
    # r1 is not r10, and a legacy fallback is not its round-scoped successor.
    # Keep Korean particles attached without a space compatible; jq's
    # Unicode-aware word boundary would incorrectly refuse those responses.
    if ! printf '%s' "$comments" | jq -e --arg t "$at" --arg m "$marker" "$rx"'
       [ .[]
         | select(.type != "Bot")
         | select(cutoff_marker == "")
         | select((.created_at | type) == "string" and .created_at > $t)
         | select((.body // "") | test("(?<![0-9A-Za-z_-])" + ($m | rx) + "(?![0-9A-Za-z_-])"))
       ] | length > 0' > /dev/null; then
      echo "(c) BLOCK: $marker (comment $id, $at) carries findings that have no thread id, and no later non-Bot comment names it — zero unresolved threads is not zero findings"
      rc=1; continue
    fi
    # Unique locations retain the measured rewritten-quote contract above.
    # Colliding locations need exact item lines or equivalent normalized keys,
    # with matching quote multiplicity within this source, never one for all.
    items=$(printf '%s' "$comments" | jq -r --arg t "$at" --arg i "$id" "$rx"'
       ([ .[] | select((.id | tostring) == $i) ] | first | (.body // "") | gsub("\r"; "") | split("\n")) as $lines
       # jq capture emits no value for a nonmatching line: map drops
       # headers and separators without raising an error.
       | ($lines | map(capture("^- \\*\\*(?<loc>.+?:[0-9]+)\\*\\* -- (?<text>\\S+ \\*\\*.*)$"))) as $findings
       # Item bodies contain unescaped multiline prose and suggestions, so
       # an ordinary bold bullet is not evidence of an extra finding.
       # Only the icon and opening severity delimiter are invariant: raw
       # severity/reviewer fields can contain delimiters or newlines.
       # Prose carrying that producer-shaped opening prefix remains ambiguous
       # without a producer encoding change.
       | ($lines[1] // "") as $envelope
       # Pending PR #191 at 49e7c610 adds optional count/head metadata;
       # main does not emit it yet. Only count consistency is checked here:
       # the head is not authentication or SHA proof. The caller supplies
       # the review window independently.
       | (if ($envelope | startswith("<!-- fold-count:")) then
            ($envelope | [capture("^<!-- fold-count: (?<n>[0-9]+)(?: head: [0-9a-f]{40})? -->$")]
              | .[0] // {})
          else null end) as $metadata
       | if $metadata == {} then "!\tmalformed fold-count envelope"
         elif ($findings | length) == 0 then "!\tempty or unparseable folded finding list"
         elif $metadata != null and ($metadata.n | tonumber) != ($findings | length) then
           "!\tdeclared fold-count does not match rendered findings"
         else
         ([ .[]
            | select(.type != "Bot")
            | select(cutoff_marker == "")
            | select((.created_at | type) == "string" and .created_at > $t)
            | (.body // "") | gsub("\r"; "") | split("\n")[]
            | select(startswith(">"))
          ]) as $quotes
       | ($quotes | map(finding_key) | map(select(. != null))) as $quote_keys
       | ($findings | map(. + {key: (("- **" + .loc + "** -- " + .text) | finding_key)})) as $keyed
       | [ $keyed[] | . as $finding
           | ([ $findings[] | select(.loc == $finding.loc) ] | length) as $same_location
           | (("**" + $finding.loc + "** -- " + $finding.text) | sub("\\s+$"; "")) as $rendered
           | (if $same_location > 1 then
               ([ $quotes[] | sub("^>+\\s*"; "") | ltrimstr("- ") | sub("\\s+$"; "")
                  | select(. == $rendered) ] | length)
               < ([ $findings[] | select((("**" + .loc + "** -- " + .text) | sub("\\s+$"; "")) == $rendered) ] | length)
             else
               ($quotes | join("\n") | test("(?<![\\w./-])" + ($finding.loc | rx) + "(?![0-9])") | not)
             end) as $legacy_missing
           | select($legacy_missing and
               (if $finding.key != null and $finding.key[1] == $finding.loc then
                  ([ $quote_keys[] | select(. == $finding.key) ] | length)
                  < ([ $keyed[] | select(.key == $finding.key) ] | length)
                else true end))
           | .loc + (if $same_location > 1 then " (whole item line required: \($same_location) items share this location)" else "" end)
             + " ; quote: - " + $rendered ]
       | "\($findings | length)\t\(join(", "))" end') \
      || { echo "(c) BLOCK: could not read the items of $marker (comment $id)"; rc=1; continue; }
    IFS=$'\t' read -r n_items missing <<<"$items"
    [ "$n_items" != "!" ] || { echo "(c) BLOCK: $marker (comment $id): invalid folded findings: $missing -- no reply or minimization clears this; ask the author or an authorized maintainer to repair source comment $id, preserving real findings, then rerun the gate"; rc=1; continue; }
    [ -z "$missing" ] || { echo "(c) BLOCK: $marker (comment $id, $at): a later non-Bot comment names it, but no later non-Bot comment quotes these item(s) by location: $missing — an item not quoted by location is an item nobody answered"; rc=1; continue; }
    echo "(c) $marker (comment $id, $at): answered by a later quoted response naming it; $n_items item(s), each quoted by location in a later non-Bot comment"
  done <<<"$markers"
  return $rc
}

# (a) at least one verdict on this SHA. Kind (i): the newest identity-matched
#     round, completed with a usable conclusion (step 8), AND
#     the aggregate that run posted — a comment or a review, step 8's shape
#     table — with every configured reviewer on its headline. Kind (ii): the NEWEST local-review verdict comment on
#     this SHA, and it must approve — an older approval under a newer
#     non-approval is a block. Zero of either is a block: no verdict is not
#     "nothing to answer".
workflow_names_raw=$(gh api --paginate "repos/OWNER/REPO/actions/workflows?per_page=100" \
  --jq '.workflows[] | {id, name} | @json') \
  || { echo "ERROR: could not list the repository's workflows — without them a run cannot be matched to REVIEW_WF"; exit 1; }
wf_runs=$(gh api --paginate "repos/OWNER/REPO/actions/runs?head_sha=$HEAD_SHA&per_page=100" \
  --jq '.workflow_runs[] | {id, workflow_id, status, conclusion, created_at, updated_at, html_url} | @json') \
  || { echo "ERROR: could not list the runs on $HEAD_SHA"; exit 1; }
workflow_runs=$(jq -n --argjson w "$(printf '%s\n' "$workflow_names_raw" | jq -s .)" \
             --argjson r "$(printf '%s\n' "$wf_runs" | jq -s .)" \
  '($w | map({(.id|tostring): .name}) | add // {}) as $m
   | [ $r[] | {databaseId: .id, workflowName: ($m[(.workflow_id|tostring)] // ""), status, conclusion,
               createdAt: .created_at, updatedAt: .updated_at, url: .html_url} ]') \
  || { echo "ERROR: could not join the runs to their workflow names on $HEAD_SHA"; exit 1; }
# surface 2 — a check run a review dispatched elsewhere reported into this repository
raw_k=$(gh api --paginate "repos/OWNER/REPO/commits/$HEAD_SHA/check-runs?filter=all&per_page=100" \
  --jq '.check_runs[] | {name, status, conclusion, started_at, completed_at, app: .app.slug, details_url} | @json') \
  || { echo "ERROR: could not list the check runs on $HEAD_SHA"; exit 1; }
checks=$(printf '%s\n' "$raw_k" | jq -s .) \
  || { echo "ERROR: could not read the check-run list"; exit 1; }
runs=$(printf '%s\n%s\n' "$workflow_runs" "$checks" | jq -cs --arg wf "$REVIEW_WF" --arg apps "$CHECK_APPS" '
  ($apps | split(" ")) as $as
  | .[0] + [.[1][] | select(.name == $wf and ((.app as $x | $as | index($x)) != null))
      | {workflowName: .name, createdAt: .started_at, status, conclusion}] ') \
  || { echo "ERROR: could not normalize the review surfaces for the cutoff window"; exit 1; }
wf_names=$(printf '%s\n' "$workflow_names_raw" | jq -rs 'map(.name) | join("\n")') \
  || { echo "ERROR: could not read the repository workflow names"; exit 1; }
if [ -n "$REMOTE_REVIEW_SOURCE" ]; then
  remote_name=$(remote_review_name "$REMOTE_REVIEW_SOURCE") \
    || { printf '%s\n' "$remote_name"; exit 1; }
  [ "$remote_name" = "$REVIEW_WF" ] \
    || { echo "ERROR: REVIEW_WF does not match the trusted producer CHECK_NAME"; exit 1; }
  wf_names=$(printf '%s\n%s\n' "$wf_names" "$remote_name")
fi
printf '%s' "$runs" | review_workflow_known "$REVIEW_WF" "$wf_names" || exit 1
run_window=$(printf '%s\n%s\n' "$workflow_runs" "$checks" | discover_round) \
  || { echo "ERROR: could not read the review round"; exit 1; }
IFS=$'\x1f' read -r round_surface round_status round_concl run_start run_end round_url <<<"$run_window"
kind_i=""
if [ -n "$round_surface" ]; then
  if round_usable "$round_status" "$round_concl" "$round_surface" "$run_start" "$round_url"; then
    if w=$(window_bounds "$run_start" "$run_end"); then kind_i=yes; else echo "$w"; fi
  fi
elif [ -n "$RUNNER_REPO" ]; then
  # Step 8's `[]` row with a runner in front of it: nothing here says
  # whether the dispatch never happened, is still running, or died before
  # it could post. The locator is read only here — a round on either
  # surface answers the question already — and only with RUNNER_REPO
  # filled, since a repository that reviews itself has no elsewhere.
  # A run older than this head belongs to a head that is no longer this
  # one, and the locator must not read it as this head's dispatch — the
  # previous head's round completed and posted its check run on the OLD
  # SHA, so on the PR number alone it reads as a dispatch that died.
  # The bound is the moment this SHA became the head. Two clauses of
  # that rule have to be here, because a reader holding this block
  # alone in a pasted shell cannot read the arm correctly without
  # them:
  #
  #   * The bound is the force-push event's server-side `createdAt`
  #     for THIS SHA, and the committer date only where there is no
  #     such event.
  #   * On the committer date the bound is **unauthoritative** and the
  #     `died` arm does not classify on it — so WAIT and re-dispatch
  #     stay open and kind (ii) does not.
  #
  # Everything else is step 8's, above, and is not repeated here: why
  # the committer date cannot be trusted, what else was looked for and
  # found missing, how the connection is walked, and every measurement
  # behind all of it. Those are what a reader needs to understand the
  # design, not to act on this row — and a reader who wants them can
  # open step 8, where they are written once.
  # The committer date is read in the `else` below, where it is the
  # bound, and not here. Reading it unconditionally made a hard block
  # out of a value the server path never consults: a rate-limited or
  # malformed committer-date read stopped the gate on a head whose
  # force-push stamp had been read successfully, which is a block
  # attributable to a read that decides nothing. Both paths still get a
  # shape check - the server path's is the force-push `case` inside the
  # walk, the committer path's is beside its read.
  #
  # The read and the filter are two statements on purpose. As one
  # pipeline inside a command substitution the assignment's status is
  # the LAST command's — `jq -r`, which prints nothing for an absent
  # node and exits 0 — so a GraphQL read that failed, for whatever
  # reason (the gate does not guess; it points at gh's own message),
  # would arrive here as an empty `head_push_time`, the guard below
  # would be dead code, the bound would collapse to the committer date,
  # and the caveat would be printed saying there was no force-push
  # event when there was one and the read broke. That is the bound
  # loosening to a value the commit's author can set, which is the hole
  # this bound exists to close, and it is this repository's recurring
  # shape: a broken read indistinguishable from an absence.
  # `set -o pipefail` is not available to fix it — this block is pasted
  # whole into a shell whose options it does not control, and nothing
  # in it sets any. So `gh` gets a statement of its own, as
  # `runner_dispatch`'s `raw` read already does, and its exit status is
  # the one tested.
  #
  # The force-push events, newest first, walked back a page at a time
  # with `before:` and the previous page's `startCursor` until this
  # head's is found or the connection is exhausted. Why the window is
  # a fixed `last:100` rather than a widening one, why `totalCount` is
  # not read, and the measurements behind both are step 8's, above.
  #
  # The page count is capped at FPUSH_PAGE_CAP, declared with the other
  # literals above so the number has one home; the rationale is there
  # too. Reaching the cap without a match is not an absent event: the
  # bound falls to the committer date, unauthoritative, and the message
  # says how many events were read and that older ones remain. That
  # fall-back is the safe floor this paging sits in front of, not a
  # thing it replaces.
  # The cap is read before the walk, as headline_roster reads
  # EXPECTED_REVIEWERS before judging a roster, and for the same kind of
  # reason: `[ 0 -lt "" ]` and `[ 0 -lt x ]` both exit non-zero, so an
  # unset or non-numeric cap ends the loop on its first test. Measured
  # 2026-10-26: zero pages read, loop status 0, no diagnostic - the
  # stamp then reads empty exactly as a PR with no force-push event
  # does, the bound falls to the committer date, and the gate prints
  # "among the 0 read" as though it had looked. That is the safe
  # direction and a silent one, which is the pairing this whole arm
  # exists to refuse, so the cap is refused by name instead.
  # Two tests, and the second is arithmetic on purpose. Matching zero as
  # a literal catches the spelling `0` and nothing else: `00` is
  # non-empty, is all digits, and is not the string `0`, so it passed -
  # and `[ 0 -lt 00 ]` is false, so the walk read nothing and the arm
  # fell to the committer date reporting "among the 0 read", which is
  # exactly the silent pairing this guard exists to refuse. A glob can
  # only ever enumerate spellings; `-gt 0` decides the value, so there
  # is nothing left to enumerate.
  case ${FPUSH_PAGE_CAP:-} in
    '' | *[!0-9]* ) echo "ERROR: FPUSH_PAGE_CAP is '${FPUSH_PAGE_CAP:-<unset>}', not a positive page count - the walk below would read nothing and fall to the committer date as though this head had never been force-pushed, which is a reading it must not reach without looking"; exit 1 ;;
  esac
  [ "$FPUSH_PAGE_CAP" -gt 0 ] \
    || { echo "ERROR: FPUSH_PAGE_CAP is '$FPUSH_PAGE_CAP', not a positive page count - the walk below would read nothing and fall to the committer date as though this head had never been force-pushed, which is a reading it must not reach without looking"; exit 1; }
  fpush_cursor=''
  fpush_pages=0
  fpush_seen=0
  fpush_total_seen=0
  fpush_more=false
  head_push_time=''
  while [ "$fpush_pages" -lt "$FPUSH_PAGE_CAP" ]; do
    fpush_pages=$((fpush_pages + 1))
    # `before` is omitted on the first page: a declared-but-unsupplied
    # GraphQL variable is null, which is what "start at the newest"
    # means, where an empty string would be an invalid cursor.
    #
    # It is passed with `-f`, not `-F`: `-F` applies gh's type coercion,
    # which sends a value matching ^-?[0-9]+$ as a JSON number, and
    # `$before` is declared `String`. Real timelineItems cursors are
    # base64 and effectively never all digits, so this fails closed
    # rather than silently - but it would exit on "could not read page
    # N" for a reason the operator cannot act on.
    #
    # The same rule decides the other two on the call below: `$o` and
    # `$r` are declared `String!`, GitHub allows an all-digit owner or
    # repository name, and an operator substituting one would get the
    # same unactionable exit. They take `-f`. `-F n=N` stays `-F`,
    # because `$n:Int!` needs exactly the coercion the others must not
    # have. The three other GraphQL calls in this block predate this
    # row and are left as they are.
    fpush_before=()
    [ -z "$fpush_cursor" ] || fpush_before=(-f "before=$fpush_cursor")
    fpush_raw=$(gh api graphql -f query='query($o:String!,$r:String!,$n:Int!,$before:String) {
  repository(owner:$o,name:$r) { pullRequest(number:$n) {
    timelineItems(last:100, before:$before, itemTypes:[HEAD_REF_FORCE_PUSHED_EVENT]) {
      pageInfo { hasPreviousPage startCursor }
      nodes { ... on HeadRefForcePushedEvent { createdAt afterCommit { oid } } } }
  } }
}' -f o=OWNER -f r=REPO -F n=N ${fpush_before[@]+"${fpush_before[@]}"} \
      --jq '.data.repository.pullRequest.timelineItems
            | {more: .pageInfo.hasPreviousPage, cursor: (.pageInfo.startCursor // ""),
               nodes: [.nodes[] | {oid: (.afterCommit.oid // ""), at: (.createdAt // "")}]} | @json') \
      || { echo "ERROR: could not read page $fpush_pages of the force-push events on OWNER/REPO#N — gh printed why above; the bound is not falling back to the committer date on a read that failed"; exit 1; }
    # Guarded like every other read here, and for the reason this whole
    # arm exists: a failed jq leaves `head_push_time` empty, which is
    # byte-identical to the legitimate absence of an event —
    # `head_bound=committer`, the bound collapsing to the value
    # `GIT_COMMITTER_DATE` can set, and the gate printing that there was
    # no force-push event as a statement of fact. The block sets no
    # shell options, so nothing catches it for them.
    head_push_time=$(printf '%s\n' "$fpush_raw" | jq -r --arg s "$HEAD_SHA" \
      '[.nodes[] | select(.oid == $s) | .at] | max // ""') \
      || { echo "ERROR: could not read the force-push stamp for $HEAD_SHA out of the timeline OWNER/REPO#N returned — an unreadable answer is not an absent event, and the bound does not fall back on one"; exit 1; }
    fpush_seen=$(printf '%s\n' "$fpush_raw" | jq -r '.nodes | length') \
      || { echo "ERROR: could not count the force-push events read on OWNER/REPO#N — see above: an unreadable answer is not an absent event"; exit 1; }
    fpush_more=$(printf '%s\n' "$fpush_raw" | jq -r '.more') \
      || { echo "ERROR: could not read whether older force-push events remain on OWNER/REPO#N — without it the gate cannot tell an exhausted connection from a truncated one"; exit 1; }
    fpush_cursor=$(printf '%s\n' "$fpush_raw" | jq -r '.cursor') \
      || { echo "ERROR: could not read the cursor for the next page of force-push events on OWNER/REPO#N — without it this walk cannot continue, and a truncated walk is not an absent event"; exit 1; }
    # Shape, not only exit status: jq exits 0 over a payload that has
    # no `.nodes` and no `.pageInfo`, so only a well-formed answer is
    # allowed to decide, and an empty stamp counts as the no-event
    # case only when it came out of one.
    case $fpush_seen in
      '' | *[!0-9]* ) echo "ERROR: the force-push event count on OWNER/REPO#N read back as '${fpush_seen:-<empty>}', not a number, so the timeline read did not return what this gate expects"; exit 1 ;;
    esac
    case $fpush_more in
      true | false ) : ;;
      * ) echo "ERROR: the timeline's hasPreviousPage on OWNER/REPO#N read back as '${fpush_more:-<empty>}', not a boolean, so the gate cannot tell an exhausted connection from a truncated one"; exit 1 ;;
    esac
    case $head_push_time in
      '' | [0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9]Z ) : ;;
      * ) echo "ERROR: the force-push stamp for $HEAD_SHA read back as '$head_push_time', which is not an ISO-8601 UTC timestamp, and the gate will not bound a head on a value it cannot read"; exit 1 ;;
    esac
    fpush_total_seen=$((fpush_total_seen + fpush_seen))
    [ -z "$head_push_time" ] || break
    [ "$fpush_more" = true ] || break
    [ -n "$fpush_cursor" ] \
      || { echo "ERROR: OWNER/REPO#N reports older force-push events but returned no cursor to reach them — a walk that cannot continue is not an exhausted one"; exit 1; }
  done
  # The server stamp, where there is one, is the whole bound — not the
  # later of it and the committer date. It is the moment this SHA
  # became the head, so it already precedes every run dispatched for
  # this head, and taking a later committer date buys nothing: what it
  # buys is a path for a client-set value to beat the server stamp,
  # which is the one thing this bound exists to prevent. Two things
  # follow from a `sort | tail -1` here. A committer date later than
  # the stamp — a future-dated `GIT_COMMITTER_DATE`, or ordinary clock
  # skew — would make `head_time` the committer date while the label
  # below still said "the head's push stamp", so the lines would print
  # a sentence the code itself contradicts. And it would push the
  # bound into the future, dropping an in-flight dispatch below it, so
  # the `none` arm would tell the operator to re-dispatch a round the
  # next line warns re-dispatching cancels. Choosing the value and the
  # label from the same test is what makes them agree by construction
  # rather than by maintenance.
  if [ -n "$head_push_time" ]; then
    head_bound=server
    head_time=$head_push_time
    head_time_note="the head's push stamp"
  else
    # Read here, where it is the bound. `.commit.committer.date` comes
    # back normalised to UTC even for a commit made on a machine that is
    # not, so the pattern below requires the `Z`, and the lexicographic
    # comparison in `runner_dispatch` is valid because of it.
    head_commit_time=$(gh api repos/OWNER/REPO/commits/"$HEAD_SHA" --jq .commit.committer.date) \
      || { echo "ERROR: could not read the committer date of $HEAD_SHA, and with no force-push stamp for this head there is nothing else to bound a runner-side run with"; exit 1; }
    case $head_commit_time in
      [0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9]Z ) : ;;
      * ) echo "ERROR: $HEAD_SHA's committer date read back as '${head_commit_time:-<empty>}', which is not an ISO-8601 UTC timestamp, so the fallback half of the bound is unreadable and there is no bound"; exit 1 ;;
    esac
    head_bound=committer
    head_time=$head_commit_time
    head_time_note="the committer date, not a push stamp — see the caveat above"
    echo "(a) kind (i): no force-push event for $HEAD_SHA among the $fpush_total_seen read on OWNER/REPO#N over $fpush_pages page(s) (older ones remain: $fpush_more) — the bound below is the committer date, which the commit's author can set, so it cannot say a completed run belongs to this head rather than the last"
  fi
  disp=$(runner_dispatch OWNER/REPO N "$head_time" "$round_surface") \
    || { echo "ERROR: could not locate the dispatch in $RUNNER_REPO"; exit 1; }
  IFS=$'\x1f' read -r disp_kind disp_id disp_status disp_url disp_earlier <<<"$disp"
  # The tuple is checked for shape BEFORE anything is decided from it.
  # Below the `case` this refusal still exits, but only after the
  # operator has read "a local review (kind (ii)) is the path on this
  # head" — advice drawn from a tuple the gate has just said it cannot
  # read. `-eq` on a non-number is a shell error followed by the line
  # printing anyway with a hole in it, and the drill builds exactly
  # that tuple for the unguarded controls.
  case $disp_earlier in
    '' | *[!0-9]* ) echo "ERROR: runner_dispatch reported '${disp_earlier:-<nothing>}' earlier runs, which is not a count, so the tuple is not the one this gate reads"; exit 1 ;;
  esac
  case $disp_kind in
    # The `none`, `running` and `died` readings each consult the
    # bound, because each gives advice that depends on it.
    # "Re-dispatch it" is the one here, and on an unauthoritative
    # bound a round for this head could have been dispatched before
    # the committer date and so fall outside the window — in which
    # case re-dispatching cancels a round that is running, which is
    # what the `running` reading warns against.
    none)    if [ "$head_bound" = server ]; then
               echo "(a) kind (i): no round on either surface here, and $RUNNER_REPO has no review run for OWNER/REPO#N since $head_time ($head_time_note) — nothing was dispatched for it. Re-dispatch it, or wait for the webhook (AT-3007). Do not merge on a round that is not there"
             else
               echo "(a) kind (i): no round on either surface here, and $RUNNER_REPO has no review run for OWNER/REPO#N since $head_time ($head_time_note) — but with no server-side push stamp that window may start after a round this head's dispatch already began, so this is not proof that nothing was dispatched. Wait for the webhook (AT-3007); before re-dispatching, read $RUNNER_REPO's recent runs for this PR, because a second dispatch cancels one that is running. Do not merge on a round that is not there"
             fi ;;
    # WAIT blocks either way, so an unauthoritative bound may produce
    # it — but on that bound a round still running may be the PREVIOUS
    # head's, which will post its check run on the old SHA and so can
    # never resolve this wait, while the line's own warning discourages
    # the re-dispatch that would end it. So the arm says which it is.
    running) if [ "$head_bound" = server ]; then
               echo "(a) kind (i): no round here yet, and $RUNNER_REPO's run $disp_id for OWNER/REPO#N, started since $head_time ($head_time_note), is '$disp_status' ($disp_url) — WAIT for it. Do NOT re-dispatch: a second dispatch cancels this one"
             else
               echo "(a) kind (i): no round here yet, and $RUNNER_REPO's run $disp_id for OWNER/REPO#N is '$disp_status' ($disp_url), started after $head_time ($head_time_note) — WAIT, but read which head it was dispatched for before waiting long: with no server-side push stamp this may be the previous head's round, which posts on the old SHA and will never complete this wait. Do NOT re-dispatch blind: a second dispatch cancels a round that is genuinely this head's."
               echo "    gh run view $disp_id --repo $RUNNER_REPO"
             fi ;;
    # The one reading that opens a path forward rather than blocking, so
    # it is the one the bound has to be trusted for. Without the server
    # stamp the gate cannot tell this head's dead dispatch from the last
    # head's finished round, and it says so instead of guessing: WAIT
    # and re-dispatch both block, and blocking is the safe side.
    died)    if [ "$head_bound" = server ]; then
               echo "(a) kind (i): no round on either surface here, and $RUNNER_REPO's newest run for OWNER/REPO#N since $head_time ($head_time_note) completed without posting one ($disp_url) — the runner could not produce a verdict. Read why, then per Review routing a local review (kind (ii)) is the path on this head:"
               echo "    gh run view $disp_id --repo $RUNNER_REPO --log-failed"
             else
               echo "(a) kind (i): $RUNNER_REPO's run $disp_id completed after $head_time ($head_time_note) ($disp_url), but with no server-side push stamp for $HEAD_SHA the gate cannot say it is THIS head's dispatch rather than the previous head's round — it is not read as a dead dispatch, and kind (ii) is not opened on it. WAIT; before re-dispatching, read $RUNNER_REPO's recent runs for this PR, because a second dispatch cancels one that is running — and on this bound a round genuinely in flight for this head can have been created before it, which puts it in the earlier count rather than among the candidates:"
               echo "    gh run view $disp_id --repo $RUNNER_REPO"
             fi ;;
    posted)  echo "ERROR: $RUNNER_REPO's run $disp_id ($disp_url) completed beside a round discovery did find — this line cannot be reached from here, so the gate block has been edited; it blocks rather than warns, because a gate that has announced it cannot be trusted may not go on to decide"; exit 1 ;;
    # Every other failure in this block exits loudly; a reading the gate
    # does not know is one more, not a row to pass over in silence on
    # the way to the comment count.
    *)       echo "ERROR: runner_dispatch returned '${disp_kind:-<nothing>}', which this gate does not know how to read — it will not guess on a head nothing has reviewed"; exit 1 ;;
  esac
  [ "$disp_earlier" -eq 0 ] \
    || echo "    $disp_earlier earlier run(s) for this PR predate this head and did not decide"
else
  # An empty RUNNER_REPO is a valid value — "this repository reviews
  # itself", so there is no elsewhere for a dispatch to have died in —
  # and it is byte-identical to the state of having forgotten to fill
  # it. Both skip the locator and leave the same `[]` row, which is the
  # conflation this whole arm exists to end, here on the configuration
  # surface rather than in a read. So the gate prints which reading it
  # took and what would change it; no new placeholder convention is
  # invented for it, because the literal is filled from this
  # repository's AGENTS.md exactly as REVIEW_WF and EXPECTED_REVIEWERS
  # are (Review routing rule 5).
  echo "(a) kind (i): no round on either surface here, and RUNNER_REPO is empty, taken as 'this repository reviews itself', so there is nowhere else to look. Wait and re-ask; if it stays empty the workflow did not trigger on this push, and Review routing's local review (kind (ii)) is the path. Do not merge on a round that is not there. And if a runner does review this repository, that empty value is an unfilled literal rather than a fact: fill it from this repository's AGENTS.md (Review routing rule 5), as REVIEW_WF and EXPECTED_REVIEWERS are filled, and read this row again"
fi
c_total=$(gh api repos/OWNER/REPO/issues/N --jq .comments) \
  || { echo "ERROR: could not read the comment count"; exit 1; }
raw=$(gh api --paginate repos/OWNER/REPO/issues/N/comments \
  --jq '.[] | {id, created_at, author: .user.login, type: .user.type, body: (.body // "")} | @json') \
  || { echo "ERROR: could not read the comments"; exit 1; }
comments=$(printf '%s\n' "$raw" | jq -s .) \
  || { echo "ERROR: could not assemble the comment list"; exit 1; }
c_seen=$(printf '%s' "$comments" | jq length) \
  || { echo "ERROR: could not count the comments read"; exit 1; }
[ "$c_seen" -eq "$c_total" ] \
  || { echo "BLOCK (a): read $c_seen comments but the PR counts $c_total — a page was missed or a comment landed mid-read; re-run"; exit 1; }
# Reviews, kind (i)'s other surface, in the same shape with the review's
# submitted_at as its created_at plus its state and commit_id, and the same
# completeness check: the PR's review count comes from GraphQL, where the
# comment count came from the issue object, and a short list is a block.
r_total=$(gh api graphql -f query='query($o:String!,$r:String!,$n:Int!) {
  repository(owner:$o,name:$r) { pullRequest(number:$n) { reviews(first:1) { totalCount } } }
}' -F o=OWNER -F r=REPO -F n=N --jq '.data.repository.pullRequest.reviews.totalCount') \
  || { echo "ERROR: could not read the review count"; exit 1; }
raw_r=$(gh api --paginate repos/OWNER/REPO/pulls/N/reviews \
  --jq '.[] | {id, created_at: .submitted_at, author: .user.login, type: .user.type, state, commit_id, body: (.body // "")} | @json') \
  || { echo "ERROR: could not read the reviews"; exit 1; }
reviews=$(printf '%s\n' "$raw_r" | jq -s .) \
  || { echo "ERROR: could not assemble the review list"; exit 1; }
printf '%s' "$reviews" | reviews_complete "$r_total" || exit 1
verdict_re=${VERDICT_RE//<HEAD_SHA>/$HEAD_SHA}
verdicts=$(printf '%s' "$comments" | jq -c --arg re "$verdict_re" --arg aw "$APPROVE_WORDS" \
  '[.[] | ((.body | split("\n") | map(select(test("\\S"))) | .[0]) // "") as $first
        | select($first | test($re))
        | ($first | capture($re) | .word | gsub("^\\s+|\\s+$"; "")) as $word
        | {id, created_at, author, body, first: $first, word: $word, approve: ($word | test($aw))}]
   | sort_by(.created_at)') \
  || { echo "ERROR: could not read the local-review verdicts on $HEAD_SHA — jq rejected VERDICT_RE or APPROVE_WORDS (an invalid regex, or one with no 'word' group); an unreadable verdict list is not 'no non-approval found'"; exit 1; }
n_verdicts=$(printf '%s' "$verdicts" | jq length) \
  || { echo "ERROR: could not count the local-review verdicts on $HEAD_SHA"; exit 1; }
newest_ok=$(printf '%s' "$verdicts" | jq -r 'if length == 0 then "none" else (last | .approve | tostring) end') \
  || { echo "ERROR: could not read the newest local-review verdict on $HEAD_SHA"; exit 1; }
[ "$n_verdicts" -eq 0 ] || { echo "(a) local-review verdicts on $HEAD_SHA, oldest first — the newest decides:"; \
  printf '%s' "$verdicts" | jq -c '.[] | {created_at, id, word, approve}'; }
[ "$newest_ok" != "false" ] \
  || { echo "BLOCK (a): the newest local-review verdict on $HEAD_SHA does not approve — back to Stage 2"; exit 1; }
# Kind (i) is the run AND its roster. The workflow posts the aggregate with
# its own token or its App's, as one of AGG_AUTHORS with user.type Bot —
# identities no session's account can post under, which is why an author
# check is right here and wrong for kind (ii) — on one of two surfaces by
# the consumer's configuration (step 8's shape table), and select_aggregate
# reads both. A comment names no SHA, so it is bound to the run by time:
# created between the run's createdAt and its updatedAt, from inside the
# run — a late aggregate from the previous head's round lands after this
# run's updatedAt and is outside it. A review names its head, so it is
# bound by commit: commit_id equal to HEAD_SHA, state APPROVED or
# CHANGES_REQUESTED (the only events the producer reviews with) — the
# equality refuses the previous head's review whatever its state, since
# nothing but this account's Stage 4 loop folds a previous head's approval
# (the producer folds only its own CHANGES_REQUESTED), and the state
# clause refuses a folded review on this head and a COMMENTED one. --commit already scoped the run to HEAD_SHA, so a second run on the same
# head is a second candidate, and the newest across both surfaces decides.
# A marker comment or review from any other author is not an aggregate,
# whatever it says. No aggregate on either surface, no headline, or a
# headline whose roster is short is no kind (i) verdict — kind_i is
# cleared, and the decision below falls to kind (ii), which Review routing
# requires in exactly that case.
if [ -n "$kind_i" ]; then
  agg=$(printf '%s\n%s\n' "$comments" "$reviews" | select_aggregate "$HEAD_SHA" "$run_start" "$run_end") \
    || { echo "ERROR: could not read the aggregate candidates on $HEAD_SHA — an unreadable list is not 'no aggregate'"; exit 1; }
  if [ -z "$agg" ]; then
    echo "(a) kind (i) refused: '$REVIEW_WF' completed/$round_concl as $round_surface on $HEAD_SHA (created $run_start, updated $run_end) posted no aggregate the gate can read: no comment by one of $AGG_AUTHORS (type Bot) carrying '$AGG_MARKER' inside that window, and no review by one of them carrying it on $HEAD_SHA in state APPROVED or CHANGES_REQUESTED — a green job with no aggregate is not a verdict (AT-2511); marker comments and reviews by anyone else were ignored"
    kind_i=""
  else
    headline=$(printf '%s' "$agg" | aggregate_headline) \
      || { echo "ERROR: could not read the aggregate's headline — an unreadable body is not 'no headline'"; exit 1; }
    echo "(a) kind (i) aggregate on $HEAD_SHA: $(printf '%s' "$agg" | jq -r '"\(.surface) \(.id), state \(.state), by \(.author) (\(.type)), at \(.created_at)"')"
    if [ -z "$headline" ]; then
      echo "(a) kind (i) refused: that aggregate carries no '**Result:' headline — no roster, no verdict"
      kind_i=""
    else
      echo "    $headline"
      headline_roster "$headline" || kind_i=""
    fi
  fi
fi
if [ -z "$kind_i" ] && [ "$newest_ok" != "true" ]; then
  echo "BLOCK (a): no verdict on $HEAD_SHA — no usable '$REVIEW_WF' workflow/check round whose aggregate reports every configured reviewer (read the kind (i) lines above and step 8's table against the runs below) and none of $c_total comments opens with the verdict header for this SHA. Per Review routing a local review is required now, delivered as such a comment:"
  printf '%s\n' "$runs"; exit 1
fi
[ -z "$kind_i" ] || echo "(a) verdict kind (i): '$REVIEW_WF' completed/$round_concl as $round_surface on $HEAD_SHA, run created $run_start, every configured reviewer on its aggregate"
[ "$newest_ok" != "true" ] || echo "(a) verdict kind (ii): the newest local-review verdict on $HEAD_SHA approves"
# The first verdict's timestamp, read once: kind (ii)'s round starts there
# for (c)'s print, and so does the cutoff window when it is the earlier
# bound. Empty when there is no verdict, which is a result and not a failure.
first_verdict=$(printf '%s' "$verdicts" | jq -r 'if length == 0 then empty else .[0].created_at end') \
  || { echo "ERROR: could not read the first local-review verdict's timestamp"; exit 1; }
# The verdict sources (c) reads, in the shape verdict_dispositions takes:
# one record per kind this head has. Kind (i) is the aggregate, with its
# headline's major count and the run's createdAt as its round start; kind
# (ii) is the newest verdict comment, with no declared count — it has no
# headline, so verdict_dispositions counts its item lines off its body —
# and the first verdict's timestamp as its round start. Both are appended
# when both kinds hold: two reviewers, not duplicates. (a) has exited
# above unless at least one is appended, so (c) never reads an empty list.
sources='[]'
if [ -n "$kind_i" ]; then
  sources=$(printf '%s\n%s\n' "$sources" "$agg" | jq -cs --arg s "$run_start" --argjson w "${agg_majors:-0}" \
    '.[0] + [ .[1] | {kind: "i", label: "aggregate \(.surface) \(.id) by \(.author)", created_at, since: $s, body, want: $w} ]') \
    || { echo "ERROR: could not build the kind (i) verdict source — an unreadable aggregate is not 'nothing to answer'"; exit 1; }
fi
if [ "$newest_ok" = "true" ]; then
  sources=$(printf '%s\n%s\n' "$sources" "$verdicts" | jq -cs --arg s "$first_verdict" \
    '.[0] + [ .[1] | last | {kind: "ii", label: "comment \(.id) by \(.author)", created_at, since: $s, body, want: null} ]') \
    || { echo "ERROR: could not build the kind (ii) verdict source — an unreadable verdict is not 'nothing to answer'"; exit 1; }
fi

# (b) 0 unresolved threads — a fresh snapshot, not Stage 4's. Any id listed
#     is a block, whatever its severity label; a count past the window is a
#     block too — cursor-paginate as in Stage 1 before deciding.
snap_json=$(gh api graphql -f query='query($o:String!,$r:String!,$n:Int!) {
  repository(owner:$o,name:$r) { pullRequest(number:$n) {
    headRefOid
    reviewThreads(last:50) { totalCount nodes { id isResolved path line originalLine
      comments(first:100) { totalCount nodes { body author { login __typename } } } } }
  } }
}' -F o=OWNER -F r=REPO -F n=N --jq '.data.repository.pullRequest') \
  || { echo "ERROR: could not read the threads"; exit 1; }
snap=$(printf '%s' "$snap_json" | jq -r \
  '"\(.headRefOid) \(.reviewThreads.totalCount) \([.reviewThreads.nodes[] | select(.isResolved|not) | .id] | join(" "))"') \
  || { echo "ERROR: could not read the head, thread count and unresolved ids from the snapshot"; exit 1; }
# Every thread, resolved or not, one per line in thread_answers' shape:
# path, its line and original line comma-joined, `replied` when any comment
# after the first is by a non-Bot account, and the first comment's first
# line after stripping a valid inline posting envelope — the producer's
# finding line; its suggestion follows a blank line
# and is not read — the fields joined by the unit separator, which `read`
# does not collapse the way it collapses tabs, so a thread with no line
# keeps its empty field. An empty first comment keeps its field too: jq's
# split of "" is [], whose first element is null, and a null fed to gsub
# aborts the whole read as an unreadable thread list (#194 round 12,
# claude) — the `// ""` the sibling reads already carry. (c) matches the
# aggregate's major items against it after this condition has required
# that none is unresolved.
thread_data=$(printf '%s' "$snap_json" | jq -r \
  '.reviewThreads.nodes[]
   | [ .path,
       ([.line, .originalLine] | map(select(. != null)) | unique | map(tostring) | join(",")),
       (if ([.comments.nodes[1:][] | select(.author.__typename != "Bot")] | length) > 0 then "replied" else "unreplied" end),
       ((.comments.nodes[0].body // "")
        | sub("^<!-- inline-post-(claude|codex|gemini)-r[1-9][0-9]* run:(actions-[1-9][0-9]*-[1-9][0-9]*|local-[0-9a-f]{32}) -->\\r?\\n"; "")
        | gsub("\r"; "") | split("\n") | (.[0] // "") | gsub("\u001f"; "")) ]
   | join("\u001f")') \
  || { echo "ERROR: could not read the threads in thread_answers' shape — an unreadable thread list is not 'no threads'"; exit 1; }
read -r thr_head thr_total unresolved <<<"$snap"
[ "$thr_head" = "$HEAD_SHA" ] || { echo "BLOCK (b): head moved to $thr_head during the gate — restart it"; exit 1; }
[ "$thr_total" -le 50 ] || { echo "BLOCK (b): $thr_total threads exceed this query's window of 50 — paginate before deciding"; exit 1; }
# (c)'s thread_answers reads each thread's replies off this same snapshot,
# so a reply past the comment page is not a missing reply, it is an unread
# one: a thread whose count exceeds what the page returned blocks here and
# says so, rather than reading as `unreplied` below and reaching
# major_dispositions as "not answered" — the page was 5 and the scan was
# all of it until round 5 of #194 (codex), so a sixth-comment reply was a
# false block with the wrong reason, and a gate that blocks answered work
# gets overridden by hand.
short=$(printf '%s' "$snap_json" | jq -r \
  '.reviewThreads.nodes[] | select(.comments.totalCount > (.comments.nodes | length))
   | "\(.id): read \(.comments.nodes | length) of \(.comments.totalCount) comments"') \
  || { echo "ERROR: could not compare each thread's comment count with what was read"; exit 1; }
[ -z "$short" ] || { echo "BLOCK (b): could not read all comments on these threads — a reply the gate did not read is not a missing reply; paginate their comments before deciding whether they were answered:"; printf '%s\n' "$short" | sed 's/^/  /'; exit 1; }
[ -z "$unresolved" ] || { echo "BLOCK (b): unresolved threads on $HEAD_SHA — back to Stage 2:"; printf '  %s\n' $unresolved; exit 1; }

# (d) 0 checks still running on this SHA — not only the review workflow.
#     Read on two surfaces, whichever sees it first: the SHA's check runs,
#     and the run list (a) already fetched — every workflow on the SHA, not
#     only REVIEW_WF — since a run and the check runs its jobs register are
#     not one record (step 8's "not visible yet" window). Either surface
#     naming anything not completed refuses.
running=$(gh api --paginate "repos/OWNER/REPO/commits/$HEAD_SHA/check-runs?filter=all&per_page=100" \
  --jq '.check_runs[] | select(.status!="completed") | "\(.name):\(.status)"') \
  || { echo "ERROR: could not list check-runs on $HEAD_SHA"; exit 1; }
pending=$(printf '%s' "$runs" \
  | jq -r '.[] | select(.status!="completed") | "\(.workflowName) (run \(.databaseId)):\(.status)"') \
  || { echo "ERROR: could not read which runs on $HEAD_SHA are still in flight — an unreadable run list is not 'nothing pending'"; exit 1; }
still=$(printf '%s\n%s\n' "$running" "$pending" | sed '/^$/d') \
  || { echo "ERROR: could not join the two (d) surfaces"; exit 1; }
[ -z "$still" ] || { echo "BLOCK (d): checks still running on $HEAD_SHA:"; printf '%s\n' "$still" | sed 's/^/  /'; exit 1; }

# (c) the items each verdict (a) named raised — read on those verdicts, never
#     on an author-filtered list, and on every one of them, never on one
#     kind: verdict_dispositions walks the source list (a) built. Where a
#     verdict names majors in the aggregate's item shape — kind (i)'s
#     headline count, or the item lines a kind (ii) verdict carries — each
#     is checked mechanically by major_dispositions: a replied thread at
#     the item's line or carrying its description, or a later non-workflow
#     comment quoting it, else exit 1. Then each verdict and every comment
#     from its round's start are printed for the rest: a review-surface
#     aggregate is not among the comments, so its body is printed on its
#     own first; a kind (ii) verdict's inline threads are (b)'s. A verdict
#     with nothing to answer passes; an item without a disposition is a block.
verdict_dispositions "$thread_data" "$sources" "$comments" || exit 1
# (c) on the cutoff surface, for whichever kinds this head has. The window
# starts at the earliest of workflow runs and allowed check runs on this
# head and the first verdict's timestamp — cutoff_window_start, above — so
# a cutoff comment from any round on this head is inside it. Not run_start:
# that is the newest identity-matched round's start, selected for kind
# (i)'s aggregate, and a start read off a selected value misses the cutoff
# comment whenever the selection and the comment's run differ. Three
# instances, one class (#194 rounds 5 and 8, codex); each ends the same way:
#   round 5  11:28Z the review run posts <!-- round-cutoff-claude-rN --> and
#           an aggregate with a 2/3 roster; headline_roster refuses, and the
#           refusal clears the one variable that also held 11:28Z, a flag
#           and a timestamp in one. 12:05Z a local reviewer posts a kind
#           (ii) approval, and since = 12:05Z
#   round 8  the run posts its cutoff comment and then concludes failure —
#           step 8's "did not pass" row — so the success-filtered list is
#           empty, run_start with it, and since is again the later kind
#           (ii) verdict's; or two completed/success runs on one head put
#           run_start at the newer one's createdAt, after the older run's
#           cutoff comment, which the producer does not repost — an
#           existing marker skips the post
# In each, the cutoff comment is filtered out as a previous round's,
# cutoff_findings reports "no round-cutoff comment on this round's
# surface", and the gate opens on the findings this read exists to block.
# Round 5 gave the timestamp its own variable, which closed the flag
# instance and left the selection; cutoff_window_start reads the start
# off no selection at all, and the drill's 5f holds both round-8 shapes.
# The function's refusal is on stdout, where this capture takes it: a bare
# `|| exit 1` here stopped the gate with the one line that said why dropped
# (#194 round 11), so the tail prints what it captured before it exits.
since=$(printf '%s' "$runs" | cutoff_window_start "$REVIEW_WF" "$first_verdict") || { printf '%s\n' "$since"; exit 1; }
# (a) has already exited unless kind (i) holds, which needs a review-workflow
# run on this head, or kind (ii) does, which set first_verdict — so an empty
# window start here is a read that failed, never a result, and it would read
# every cutoff comment the PR has ever carried as this round's.
[ -n "$since" ] || { echo "ERROR: no window start for the cutoff surface although (a) passed — no '$REVIEW_WF' run on $HEAD_SHA, first verdict '$first_verdict'"; exit 1; }
printf '%s' "$comments" | cutoff_findings "$since" \
  || { echo "BLOCK (c): a cutoff or inline-fallback comment's findings on $HEAD_SHA have no disposition — back to Stage 2"; exit 1; }
echo "(a)(b)(d) clean — read (c), then start the merge block with:"
echo "  HEAD_SHA=$HEAD_SHA"
```

Only after you have read (c) and every item is answered. The merge block starts from the value the gate printed, as a literal — not from shell state, which does not survive between tool calls in the sessions that run this skill, and not from a re-read, which would pin whatever head exists now. The literal carries the SHA the gate actually read; if the head moved in between, `--match-head-commit` refuses server-side.

```bash
HEAD_SHA=<paste the 40-char value the gate printed>
[ "${#HEAD_SHA}" -eq 40 ] || { echo "ERROR: HEAD_SHA is not the 40-char value the gate printed"; exit 1; }
# <resolved method> is one of merge / squash / rebase — resolve it with Merge strategy below
gh pr merge N --repo OWNER/REPO --<resolved method> --match-head-commit "$HEAD_SHA"
```

Why (b) and (c) are read here and not carried from Stage 4: **the push after Stage 4's cleanup is itself what triggers the next round, and until 2026-09-11 nothing after that push re-read the threads.** Measured that day over every PR this account merged since 2026-09-04 — 144 PRs, GraphQL `reviewThreads` — 3 were merged with an open bot thread, all with 0 replies: ai-dev-cab#481 (thread 04:49Z, merged 05:08Z), ai-dev-qa-partner#1112 (thread 22:37Z, merged 23:01Z), ai-dev-pr-review#147 (merged 10:43Z before its round finished; the thread arrived 11:15Z). A fourth, ai-dev-infra-common#801, was handed over as "0 unresolved threads" from a query made before the final round posted — same mechanism, second machine, caught before the merge — the thread was answered at 00:04Z and it merged at 00:12Z. Severity does not exempt: a `suggestion` gets a reply and a resolve like anything else; an open thread at merge time is a silent close (Resolve Policy).

Why (a) reads two verdict kinds and (c) reads only what (a) named, and why neither passes on an empty list (#117, filed 2026-09-20): until this change (c) selected comments by author identity — `Bot`, a `[bot]` suffix, `github-actions` — and an empty selection with 30 or fewer comments printed *no bot aggregate comment* and continued. It could not tell nothing-to-answer from nothing-asked. Measured: ignite-pilot-org/factory-system-builder #63 (gate run 2026-09-18) had 3 comments, all LENS-session verdicts posted as `hyuk-hur` — two not-approved, one APPROVE — and the filter matched 0, so the merge rested on a hand check the gate never saw; hyuk-hur/dev-dotfiles #116 (2026-09-19T13:41Z) and #130 (2026-09-29) each had exactly one comment, the LENS hand-review verdict naming the head, the gate printed *no bot aggregate comment among 1 comments*, and the operator substituted an ad-hoc APPROVE-comment check for (a) each time — once missing the verdict outright because it was worded `승인`, not `APPROVE`. LENS observed the same shape on dgt-research #126 and #123 and on ai-dev-pr-review #119. The organisation's own aggregate already behaves the way this gate now does: on ai-dev-pr-review #172 every reviewer was skipped and it reported *Review skipped -- PR too large* as `conclusion: failure` rather than as a pass. Kind (ii) is not an interim to be restored later: it is *Review routing*'s standing path whenever the workflow produces no verdict, and it stops being exercised by itself the moment the workflow produces one again — nothing here needs putting back afterwards.

Why kind (ii) is a header contract and not a marker search (lens, 2026-09-29, on this change's first cut): that cut dropped the identity filter and matched an approval word anywhere in a body that contained the SHA, minus three enumerated negative spellings. Measured on ignite-pilot-org/max-builder #80, head `7b3081be`: three comments named the SHA and all three counted as approvals — the author's own review request (`## 리뷰 재요청 — LENS 세션 · head …`, whose body asks the reviewer to *name the head SHA in the APPROVE*), the verdict itself (`## LENS 판정 — **승인** · head …`), and the author's own gate record, which quotes the verdict. With identity gone, a body-wide marker lets the author satisfy (a) in the author's own words. And the negative list caught only the spellings it enumerated: lens's real rejection vocabulary — `**차단 1건** — 고치면 승인합니다`, `승인하지 않습니다`, `승인 보류` — passed as approvals, the same closed-enumeration-with-default-allow defect that #130's review found in the human-gate list. Identity was removed on purpose, so the content check has to do what identity used to: separate a verdict from a request, a quotation and a record. Position and form do that and a word search cannot — a request never opens with the verdict header, and a word the header does not capture as an approval is not one, whether or not this file has heard of it. The cost is that a verdict outside the form does not count: hyuk-hur/dev-dotfiles #116's `## APPROVE — `e835d…`` is one, and a reviewer who posts that shape reposts in the contract's. The same cost falls on a rejection, and there it is easier to miss (lens, 2026-09-29, non-blocking, on #131's verdict): a rejection posted outside the header form — the older `## LENS 손 리뷰 — head …` with `차단 1건` in the body — is not a verdict, so it is indistinguishable from the absence of a review, and the gate reports *no verdict, local review required* rather than *the reviewer rejected*. Both block; the operator-facing message differs, and a PR that was reviewed and rejected reads as one that was never reviewed — which is why the reviewer reposts the rejection in the contract's form too.

Two limits, stated rather than papered over. Under one shared GitHub account the gate cannot prove the local reviewer is not the author — every comment is `hyuk-hur`. It requires the claim in machine-readable form, a comment opening with the verdict header for this SHA, and verifies nothing beyond that; that the reviewer is a different session is *Review routing* rule 4, a rule, not a check — and the same rule is what forbids the author from posting in the header shape. And a gate that counts comments must paginate: the comment read uses `gh api --paginate` and compares what it fetched with the count the PR itself reports, so a short page or a comment landing mid-read is a block, not a shorter list — the old block asked `-le 30` only on its empty branch, which is asking after deciding.

Why kind (i) reads the aggregate's roster and not the run's conclusion (#140; AT-2511, measured by LENS on ai-dev-pr-review #174, 2026-09-29): a reviewer step that hits the 10-minute cap leaves the job `success` — the aggregate records `action_invocation_failed` for that reviewer and reaches quorum on the others — so the check rollup is green while one configured reviewer never read the diff, and the cut correlates with size (everything at or above 6,629 changed lines was cut, everything at or below 6,074 completed), so the larger the PR the likelier a green run reviewed less of it. LENS will not lower the conclusion — that would change merge behaviour for every consumer at once, a fleet decision — and exports a roster instead (AT-2565, producer side); but `reviewer_roster` and `reviewers_responded_count` are `workflow_call` outputs, reachable by a calling workflow and by nothing `gh run list` or the check rollup returns, so the headline's `N/M reviewers` and `N/M LLM responses` fractions are the only rendering of that roster a consumer can read on the PR, and the gate reads them: both must be `M/M` and the Result word `Approved`. The fraction is the uniform signal and nothing else is: a cut reviewer is spelled three ways depending on which one was cut — claude `failed: action_invocation_failed` with job conclusion `success`, codex `failed (see logs)` with conclusion `failure`, gemini `early-exit or no-output` with conclusion `success`, because `continue-on-error` rewrites a step killed at `timeout-minutes` to `success` — so the gate keys on neither a `failed` prefix in the reason text nor any job or step conclusion. The fraction's placement moves too: with a cut it sits inside a parenthetical, `not every reviewer responded (2/3 reviewers)`, and with a full roster it is bare, `3/3 reviewers`; the regex is unanchored so both placements match, and the drill holds both. Three things the first cut of this check left open, found by the remote round on #170 (codex, 2026-10-01). The denominator: `N/N` is full only against the roster the headline was written for, so the gate compares both denominators with `EXPECTED_REVIEWERS`, read from the review workflow as step 8 says, and refuses an unfilled or non-numeric placeholder the way an unfilled `REVIEW_WF` already fails. The author: the marker is a public string and the window is minutes wide, so a comment is an aggregate only when its author is `github-actions[bot]` with `user.type` `Bot` — the identity the workflow's own token posts under and no session's account can; #174 widened the list to the App's login, below — which is not the identity list the kind (ii) contract refuses: that list named humans sharing one account, this names an account no human holds. The majors: `Approved with N unreviewed major issue(s)` used to reach the merge block on a printed warning, so now `major_dispositions` parses the body's major items and exits non-zero unless each has a thread at its path and line or a later non-workflow comment quoting it, and a body listing fewer majors than its headline counts blocks too. Round 2 on the same PR (codex, 2026-10-01) tightened two of its reads: a thread anywhere in the item's file used to count, so an unrelated resolved thread on the same path answered a finding it never touched — the match is now path and line, with `originalLine` accepted for a thread the diff has since moved, and an item with no line has only the quote route; and the path was cut at its first colon, so a path containing one was truncated — the key splits only where a number follows the last colon. Round 3 (gemini, 2026-10-02) tightened the quote route the same way: a bare substring match let a quote of some other finding whose text contained the path answer this one, so a path-bearing item now matches only its key in backticks, as the aggregate renders it, and the plain-prefix match is kept for path-less items alone. The hand verdict on the head after that (LENS, 2026-10-02) showed the kept half was the weaker one: `file: null` is a legal payload, so a path-less major is real, and its 60-character prefix is repeated by another finding's prose and shared by near-identical twins — both reproduced with the gate's own function — so a path-less item now matches only its whole rendered line, verbatim. The re-verdict on that head (LENS, 2026-10-02) found the same defect alive on the other branch — a backticked `path:line` is a token reviewers write inside other items' descriptions, measured on this PR's own aggregate, so quoting item 2 whose prose mentioned item 1's key answered item 1 — and named the shape: three rounds in a row the fix moved the defect from one branch of a two-way split to the other. The split is gone: every item's needle is its whole rendered line from `**major**` to the end of its description, `**major** \`path:line\` -- description` or `**major** -- description`, and the thread match likewise reads the whole description rather than a 60-character prefix, which would have let one thread answer two same-path twins. The approval that followed (LENS, 2026-10-02, non-blocking) named the last trace of the shape: two `needle=` assignments re-assembling the line from captures are still two things to keep in sync, so the needle is now derived once from the matched line itself, `${line#- + }`, after whichever regex matched. The same verdict widened the thread match where the producer itself leaves a gap: `post_inline_comments.py` posts no thread for a line outside the diff and none within five lines of a similar thread, so an answered major can be re-listed a few lines from the thread that answered it; a thread now answers on path plus either the line or its first comment being the producer's rendering of the finding, `+ **major** (<reviewer>): <description>` with the description whole to the end of the line, content-bound rather than distance-bound — round 10 of #194 (codex) found that match written as a substring of the first comment's text, so a same-path thread mentioning the description in prose answered it, and the same round found the thread rows read with a tab IFS, which collapses the empty line field of a file-level thread and shifts its reply flag into the line field, so the description route never reached the line-less threads it exists for; the rows are now unit-separated and the match is an equality after the producer's prefix. And two more things it named: a null `updatedAt` on the run used to open the aggregate window rather than close it, since a null compares as later than any timestamp, so `window_bounds` now refuses either missing edge; and a thread resolved with no reply counted as a disposition, which Resolve Policy forbids, so `thread_answers` requires a reply from a non-Bot account. The Result words the aggregate writes are `Approved`, `Approved with N unreviewed major issue(s)`, `Changes Requested`, `Comment Only`, `Review did not run -- …`, `Review skipped -- PR too large` and `Review skipped -- only policy-excluded files changed`; only the first two approve, and the second is not a pass on its own — its N items go to (c). The binding of comment to run is by time, because the aggregate body names no SHA: this run's aggregate is the newest marker comment created between the run's `createdAt` and `updatedAt`, which excludes a previous head's round posting late. That is one of three shapes (#174, measured 2026-10-02 on each consumer's newest merged PR; step 8's shape table): with `ALLOW_AUTO_APPROVE` on, the aggregate is a review, not a comment — by `github-actions[bot]` without an App key, by `ignite-ai-review-approver[bot]` with one — and until this change the gate reported *no comment by github-actions[bot] carrying the marker* on ignite-corp/ai-dev-qa-partner #1195, ai-dev-infra-common #876 and ai-dev-cab #515, although on each the round had completed and its aggregate was on the PR. The review surface binds on `commit_id`, not on the window, because a review names its head where a comment cannot, and the equality carries the whole of the previous-head refusal: on every measured PR the previous head's review is `DISMISSED` and the current head's `APPROVED`, but it was not the workflow that folded it — the producer dismisses only its own `CHANGES_REQUESTED` marker reviews (`_minimize_stale_bot_items`, ai-dev-pr-review d554aee) and never an approval, and the `review_dismissed` timeline events on ai-dev-cab #515, ai-dev-qa-partner #1195 and ai-dev-infra-common #876, read 2026-10-02, are all this account's, the Stage 4 loop, none with the producer's `Superseded by new review` message (LENS, on #175's first head, which had written the mechanism backwards in three places). A consumer that does not run that loop keeps the previous head's approval `APPROVED`, so `DISMISSED` cannot stand in for "previous head", and the drill's control that cuts the equality is what shows it is load-bearing. The state clause is for this head: the gate reads an `APPROVED` or `CHANGES_REQUESTED` review whose `commit_id` is `HEAD_SHA` — the two events `post_verdict` passes `gh pr review`, so a `COMMENTED` marker review, which the producer never posts (its only `COMMENTED` review is the inline one, with an empty body), is no aggregate whatever its headline says (codex, round 18 of #194) — — not the newest review on `HEAD_SHA` of any state, which after a Stage 4 re-run on a posted head would be a dismissed approval. The time window stays on the comment surface alone, where it is the only binding there is. And the review list gets the comment list's completeness check, against GraphQL `reviews.totalCount` (equal to the paginated REST length on all four measured PRs): the producer spares approvals when it folds, so two runs on one head can leave an approval under a newer `CHANGES_REQUESTED`, both non-dismissed on the head, and a dropped page that lost the newer one would promote the approval — the first cut had exempted the review read on the claim that a missed review could only lose a candidate, which is the opposite of what newest-wins exists for (LENS, same verdict). Both surfaces are read by one function, `select_aggregate`, and the newest candidate across them decides; `headline_roster` and `major_dispositions` read its body without knowing which surface it came from. Stage 4's dismiss-all-prior-reviews loop runs before the push that starts the round, so the aggregate's review on the new head does not exist when it runs; a re-run after the round has posted dismisses it, and the gate then falls to kind (ii) rather than reading a dismissed approval. The author list is the one the comment surface already carried, widened by the App's login, and it is applied to both surfaces because identity is per verdict and not per repository: on the App repositories a `Changes Requested` review is still `github-actions[bot]`'s, a `Comment Only` verdict and the review-API-failure and timeout fallbacks are issue comments on every configuration — both with an `Intended verdict` note appended after the headline, differing only in the reason, while a comment-only post carries none, so the gate keys on no note (AT-2597, producer side) — and a failed App mint leaves the approval posted as `github-actions[bot]`'s review. Both are `Bot`, and `.github/scripts/gate_headline_drill.sh` holds the three measured shapes, a `DISMISSED` review on the head, an `APPROVED` review on the previous head, and a marker review by a login outside the list, with a control showing the `commit_id` equality is what refuses the previous head's approval. The cost, stated: a sequential-mode round that early-exits by design reports `1/3 reviewers` and is refused here too — this repository's workflow runs its reviewers in parallel, and a repository that does not falls to kind (ii). Measured on this repository 2026-10-01 on #168, the first remote round after the billing block lifted: `**Result: [OK] Approved | posted as comment (auto-approve off) | 3/3 reviewers** -- 3/3 LLM responses -- minor/suggestion only`; the shapes `.github/scripts/gate_headline_drill.sh` holds are that one, its `no issues` and `1 major issue(s)` siblings, a `2/3 reviewers` cut and `[X] Review did not run`.

Why (d) is its own condition: on 2026-09-11 the cab session had two PRs with `APPROVED` and the review round completed/success while `lint (3.14)` was still `IN_PROGRESS` — a poll that waits only on the review workflow merges past a running check.

(d) counts what is still running, and a review job that never started is not running: its status is `completed`, its conclusion `failure` or `skipped`, its step count 0. Two observed shapes. On every hyuk-hur PR through 2026-09-29 the `AI Code Review` run ended in four seconds with `Verify reviewer keys: completed/failure, steps=0` and `review: completed/skipped`, annotated *"The job was not started because recent account payments have failed or your spending limit needs to be increased"* — #128's head `659345c`, #130's head `4b0195e`. On ignite-corp/ai-dev-pr-review #172, head `2af348c` (run 35479552503, 2026-09-20), all six reviewer *jobs* were skipped past `PR_SIZE_LIMIT` and the aggregate reported `Review skipped -- PR too large` as `failure`. Six is jobs, not reviewers: the orchestrator declares a parallel-mode and a sequential-mode job per reviewer, `review-{claude,codex,gemini}-{p,s}`, and `vars.REVIEW_MODE` selects one set, so three of the six are skipped on every round — on #172's final head `e9592d8` the three `-p` jobs completed and the three `-s` jobs were `skipped` with 0 steps — and the roster, step 8's `EXPECTED_REVIEWERS`, is the three reviewers, not the six jobs. Neither produced a verdict: neither satisfies (a)'s kind (i), neither blocks (d), and neither is a rejection — reading one as the reviewer's negative answer blocks the repository for as long as the reviewer cannot be summoned, because the reviewer that could not run is counted as the reviewer that said no. A completed `failure` that did post a verdict is a rejection and blocks as before; the difference is whether anything was said, and the aggregate comment — kind (i)'s items — is where it would have been said.

The gate has two entry paths, and both restart it from the top: a push (fixup or otherwise), and `gh pr update-branch` — see the `BEHIND` row; each moves the head and triggers a round on the new SHA. Nothing read on the old SHA survives either.

`--match-head-commit` takes the **full 40-char SHA**. Given a prefix it refuses with `Head branch was modified` — that reads like a race but is the guard rejecting an abbreviation, and the length check at the top of the block exists so it is never mistaken for one.

`--match-head-commit` is what closes the trailing edge. Without it there is a window between the last check and the merge call in which the head can move, and the merge takes whatever is there; with it GitHub compares server-side and refuses, so a moved head costs a restart instead of an unreviewed commit on the base branch. Never drop it because the check "just passed" — the flag exists for the interval after the check.

**Merge strategy — resolve it, don't assume it.** `--merge` is this skill's default, and it preserves individual commit history. It is a default, not a fact about every repository. Repository settings bound every answer, the default included — settings decide what a repository may still do, and no tier below them may name a method they rule out — so they are read first. Stop at the first tier that answers:

1. **Repository settings** — enforced by GitHub, not opinion:
   ```bash
   gh api repos/OWNER/REPO -q '{squash: .allow_squash_merge, rebase: .allow_rebase_merge, merge: .allow_merge_commit}'
   ```
   Each `false` rules a method out, for every tier below and for the default. **Exactly one `true` left answers on its own; two or more and the settings do not answer — they bound: tiers 2 and 3 choose among the survivors only.** A non-zero exit rules nothing out and answers nothing: every method survives, and the check is named as unresolved in the report.
2. **Project memory**, where it names a survivor — qa-partner's `feedback_merge_strategy.md` forbids `--squash` at merge time. That is qa-partner's rule, and it binds there. A memory that mandates a method tier 1 ruled out does not answer — `gh pr merge` would refuse that method server-side — so name the memory as stale in the report and go on to tier 3.
3. **The repository's own history** — what the repo actually does outranks what this file defaults to. **It ranks only the survivors: history shows what a repo did, settings decide what it may still do.**
   ```bash
   git log --oneline --merges origin/<baseRefName> -5
   ```
   Merge commits listed → the repo merges; empty → it squashes **or** rebases, and this command cannot tell which. Take the surviving method the evidence points at; if it points at two survivors, or at none, ask. A non-zero exit prints nothing too — inconclusive, not a signal.

If no tier answers, use `--merge` where tier 1 left it standing and say that the default was used; where tier 1 ruled it out and nothing answered, ask. A tier that exited non-zero is a tier that did not answer, so the fallback is the same — **the report is not: name that check as unresolved alongside the default.**

#### REST API merge fallback (only when ALL guards pass)

Per `feedback_no_admin_merge.md` Rule 2: when `gh pr merge` refuses with `the base branch policy prohibits the merge` AND ALL these guards hold:
- **Standard merge (a) passed on the current head** — a verdict, kind (i) or kind (ii), on the SHA about to be merged
- PR has at least one APPROVED review (any reviewer, including bots with `authorAssociation: NONE`)
- All required checks pass
- 0 unresolved threads
- Branch up-to-date with base

Then REST API direct merge is acceptable:

```bash
# Same gate, same place — the fallback is a merge, so it is gated like one:
# the Standard merge gate block runs first, and this block takes the place
# of the merge block, starting from the literal the gate printed.
HEAD_SHA=<paste the 40-char value the gate printed>
[ "${#HEAD_SHA}" -eq 40 ] || { echo "ERROR: HEAD_SHA is not the 40-char value the gate printed"; exit 1; }
gh api --method PUT repos/OWNER/REPO/pulls/N/merge \
  -f merge_method=<resolved method> \
  -f sha="$HEAD_SHA"
```

`merge_method` is the method resolved by **Merge strategy** above, not a literal `merge`; the REST values `merge` / `squash` / `rebase` are the same three words as `--merge` / `--squash` / `--rebase`, so nothing needs translating. `commit_title` and `commit_message` are optional, and the block omits them on purpose: the endpoint documents them as *Title for the automatic commit message* and *Extra detail to append to automatic commit message*, so when they are omitted GitHub writes the automatic message the repository's own settings define — for this repository `merge_commit_title: MERGE_MESSAGE` with `merge_commit_message: PR_TITLE` (read from `gh api repos/OWNER/REPO`, 2026-10-14), which is the `Merge pull request #N from OWNER/BRANCH` subject with the PR title as body that every merge commit on `main` carries, and `squash_merge_commit_title: COMMIT_OR_PR_TITLE` with `squash_merge_commit_message: COMMIT_MESSAGES` for a squash — the same message `gh pr merge` writes when given no `--subject` or `--body`. Pass the two fields only to override that message; `rebase` ignores both.

`-f sha=` is the REST equivalent of `--match-head-commit`, and it is required for the same reason: the fallback path is a merge like any other, so it carries the same pinned head. A `409` back from it means the head moved — restart from step 8; do not retry without the pin.

This is NOT admin bypass — it's working around `gh pr merge`'s overly conservative pre-check + GitHub's stale `mergeStateStatus` cache. GitHub server-side asynchronously recomputes `mergeStateStatus` and `reviewDecision`, so they can lag the actual ruleset evaluation by several minutes after a state-changing event (push, review, check). The guards above prove the ruleset is actually satisfied. If REST returns 200 OK + a merge SHA, the ruleset was satisfied all along (CLI cache was stale). If REST returns 405/422, the response body's `message` field gives the real reason — that's the natural blocker, act on it.

Common stale-cache signals that should prompt the raw-data check above:
- `reviewDecision: empty` but `gh api repos/.../pulls/N/reviews --paginate` shows APPROVED on current HEAD
- `mergeStateStatus: BLOCKED` but all required checks (`gh api repos/.../commits/<sha>/check-runs`) pass and 0 unresolved threads
- `mergeStateStatus: UNKNOWN` immediately after a push (GitHub mid-recompute)

Before declaring a "policy change" / "inconsistency" / "bot APPROVED not counted" hypothesis: ALWAYS verify with raw REST + ruleset data first. The CLI display values are not the source of truth.

If ANY guard fails (especially CHANGES_REQUESTED or unresolved threads): STOP. Don't merge via either path.

#### `--admin` flag is gated

`gh pr merge --admin` only when user explicitly commands it for THAT specific PR (`"admin merge PR #161"`). A standing "go ahead" or "merge it" does NOT authorize `--admin`. Don't propose `--admin` as a path/fallback at all.

### Confirm post-merge

```bash
gh pr view N --repo OWNER/REPO --json state,mergedAt,mergedBy --jq '{state,mergedAt,mergedBy:.mergedBy.login}'
```

If a round finishes **after** the merge — the ai-dev-pr-review#147 shape: merged 10:43Z, thread 11:15Z — its threads still get a reply and a resolve on the merged PR, per the Resolve Policy; the merge itself is a gate violation and is recorded as one, in the reply, naming the merged SHA and the run that was still in flight, so the next audit finds it rather than an unexplained open thread.

Branch deletion and the ticket transition are out of this skill's scope — handle separately.

## Multi-PR coordination

When several PRs are in flight, batch by stage rather than fully serializing:
1. Snapshot all PRs in parallel
2. For each PR with unresolved items: classify in parallel
3. Apply replies+resolves+dismisses across all items of all PRs in parallel
4. Navigate merge state per PR
5. Merge ready PRs in parallel

This is faster and surfaces cross-PR patterns (same bot false claim across multiple PRs) so you write the evidence-based reply once and reuse it.

## Repeated bot patterns

If the same bot posts the same wrong claim across multiple PRs, prepare ONE evidence-based reply template, then apply it uniformly. Don't re-research the same fact per PR — verify once, reuse the verification.

Real example (`IMMUTABLE_WITH_EXCLUSION` enum, 2026-05-08 session, 4 false claims across 3 PRs):

```
By design. Verified against the live CloudFormation registry today.

Schema accepts 4 enum values: `MUTABLE`, `IMMUTABLE`, `MUTABLE_WITH_EXCLUSION`, `IMMUTABLE_WITH_EXCLUSION`.

Command: `aws cloudformation describe-type --type RESOURCE --type-name AWS::ECR::Repository`

The value was also applied via the live API to all 6 ECR repos in our account today. Bot reference appears stale relative to the 2024 ECR exclusion-filter release.
```

Pattern: assertion + verification command + applied-evidence + likely-cause-of-bot-error.

## Anti-patterns (catch these before they happen)

- **AI attribution** — never write `Co-Authored-By: Claude`, `Generated with`, or any AI mention in any artifact. Non-Negotiable #1.
- **Korean in commits/PR titles/PR bodies** — Non-Negotiable #2: code artifacts are English only. Thread replies/issue comments can be Korean.
- **Silent resolve** — close without reply. Every close needs a reply, no exceptions.
- **Admin merge in status report** — `feedback_no_admin_merge_option.md`: never frame admin merge as a path. Status report = "review thread X 해결 + 인간 review 대기".
- **Trailing question** — "should I merge?" when CLEAN + APPROVED + policy says yes. Per `feedback_no_unjustified_wait.md`: just merge.
- **Picking the remote merge method without checking** — resolve it per **Merge strategy** (Stage 5) instead of assuming: qa-partner's `feedback_merge_strategy.md` forbids `--squash`, a repo with `allow_squash_merge=false` forbids it, and a base branch with no merge commits in its history is not a repo that merges. Local autosquash (**Stage 5, step 9**) is a different operation — always fine, and required.
- **Close + new PR for scope change** — `feedback_amend_over_close.md`: amend the existing PR via force-push.
- **Forgetting to update PR body after scope change** — Phase 9: PR description must reflect the latest changes.
- **`--force` instead of `--force-with-lease`** — race-unsafe; always use `--force-with-lease`.
- **An autosquash that silently does nothing** — dropping `-i` or the `GIT_SEQUENCE_EDITOR` stub makes the rebase a no-op on git 2.43 while still exiting 0, so the session believes it squashed and pushes autosquash-prefixed titles anyway. Use the **Stage 5, step 9** form verbatim and run its post-check — that check is what catches this.
- **Pushing autosquash-prefixed commits without squashing** — `git commit --fixup` and `--squash`, in any of their forms, create commits whose titles carry an autosquash prefix; **Stage 5, step 9** enumerates them. Autosquash MUST run before push to collapse them into their targets. Pushing those literal titles pollutes remote history and forces a follow-up cleanup commit.
- **`gh api` without `--paginate`** for comments/reviews — default 30 per page silently truncates. Always `--paginate` on any `repos/.../comments` or `pulls/.../reviews` fetch.
- **Auto-merge enable** (`gh pr merge --auto`) — bypasses the explicit "review is complete" gate. Default is poll-then-merge after CLEAN+APPROVED, not pre-arm. Auto-merge prematurely commits intent to merge before review outcome is known.
- **Treating human-authored threads as auto-classifiable** — bots get auto-handled; human reviewer threads exit the loop to the user.
- **Ignoring `isOutdated`** — outdated threads should bypass classification and go straight to minimize.
- **Assuming base branch is `main`** — read `baseRefName` from the PR; some PRs target staging or feature branches.
- **Skipping the concurrent-push check** — if HEAD changed during Stage 4, your replies landed on stale state. Re-snapshot.
- **Sequential GraphQL mutations** when batch parallelism works — wastes minutes per PR.
- **Re-fetching threads after each mutation** — fetch once, classify all, then act.
- **Editing code as leader** — Non-Negotiable #7: spawn coder teammate with `isolation: "worktree"`.
- **Asking before spawning a teammate for review-fix** — `feedback_spawn_freely.md`: safe spawns don't need per-PR confirm.
- **Interleaving code fix and comment cleanup** — AGENTS.md PR Comment Management: finish all code + push, THEN one final cleanup pass.
- **Bot disagreement without evidence** — always cite schema/API/doc verification.
- **Asking "policy or X?"** — `feedback_execute_per_policy.md`: if policy already covers it, execute the policy answer without consulting.
- **Merging on a head no review ran against** — `gh run list --branch <branch> --status in_progress` names the branch, and the branch is the thing that moved; it answers "nothing running" during the seconds between a push and its run registering. PR #86 (issue #92) merged 12 seconds after the round for its new head started; the run then diffed the merged branch against itself, reviewed an empty diff, and an 11-line delta reached `main` with no review at all. Ask **Stage 5, step 8**'s SHA-named question instead, re-ask it immediately before the merge call, and pin the SHA into the call.
- **Counting threads before the final round** — "0 unresolved" from Stage 4's snapshot, or from any query made before the round on the merged SHA posted, is a count of the previous round. ai-dev-cab#481, ai-dev-qa-partner#1112 and ai-dev-pr-review#147 (2026-09-11 audit) were merged with an open bot thread that way. The count that opens the gate is the one taken in **Stage 5, Standard merge**, condition (b), on the head named in the merge call.
- **Infinite wait for bot re-review after force-push** — some bot reviewers (observed: `gemini-code-assist`) do NOT re-review on force-push; they trigger only on initial PR open. Waiting past ~2-3 min for a *review event* from one of those is waiting for something that will not arrive: proceed with the merge state's normal action (see Stage 5 table). **This licenses skipping a reviewer's review event; it never licenses skipping a review run.** Stage 5, step 8 asks about the workflow run on the head SHA, and `[]` there is a block that no amount of elapsed time converts into a pass — the two were conflated once already, and that is issue #92.

## Compatibility

- `gh` CLI authenticated for the target repo
- `jq` on `PATH` — the Standard merge gate filters the paginated comment list with it, outside `gh`
- `bash`, not `sh` or `zsh` — the Standard merge gate uses `[[ … =~ … ]]` with `BASH_REMATCH`, here-strings and `${var#…}`, and the drill lints the extracted functions with `shellcheck -s bash`. Measured 2026-10-15 with `[[ "x3/3" =~ ([0-9]+)/([0-9]+) ]]`: bash 5.2 captures `3` and `3`; zsh 5.9 reports a match with `BASH_REMATCH` empty, so `headline_roster`'s `rv_n`/`rv_m` come out empty and it refuses every roster, and under `setopt BASH_REMATCH` zsh fills index 1 with the whole match `3/3` and index 2 with the first group, so the option is not a fix; dash does not parse `[[` at all. On a machine whose login shell is zsh, run the block in a `bash` session
- GraphQL access (default with `gh` auth)
- For multi-PR / bulk operations, parallel `gh api` calls work; throttle at >100 mutations/min
- Project memory directory accessible at `~/.claude/projects/<project-encoded-cwd>/memory/`
- AGENTS.md global at `~/.claude/AGENTS.md`
