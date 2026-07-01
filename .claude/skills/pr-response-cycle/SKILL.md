---
name: pr-response-cycle
description: Drive a GitHub pull request through the full review-fix-merge cycle following the project's established 10-step checklist — bulk-classify review threads (Fixed / Deferred / Won't fix / Duplicate / Outdated), post evidence-based replies, manage all three PR timeline item types (review threads + issue comments + review bodies), apply fixup-rebase for review-driven code changes, navigate merge state (CLEAN / BLOCKED / BEHIND / DIRTY), update branches, and merge with merge commit (never squash) when policy conditions are met. Use this skill whenever the user asks to "process review", "respond to threads", "merge PR", "handle PR feedback", "PR 리뷰 처리", "thread 정리", "봇 코멘트 처리", or mentions a PR number/URL with any review/merge action — even without explicit "respond to all threads". Trigger strongly when multiple review threads need consistent handling, when bot reviewers (gemini-code-assist, claude, codex) post repeated patterns across PRs, when the user wants to push a PR to merge after CI completion, or when a session has multiple in-flight PRs needing coordinated cleanup.
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

Key memory entries that affect this cycle:
- `process_review_push_checklist.md` — the canonical 10-step ordering (also the spine of this skill)
- `feedback_merge_strategy.md` — merge commit, NEVER `--squash` at merge time
- `feedback_no_admin_merge.md` — `--admin` per-PR explicit only; REST API merge as gh-CLI workaround when APPROVED
- `feedback_no_admin_merge_option.md` — admin merge MUST NOT appear in status reports as a fallback path
- `feedback_amend_over_close.md` — scope change → amend (force-push), never close+new
- `feedback_execute_per_policy.md` — policy already covers it → execute, don't re-ask
- `feedback_no_unjustified_wait.md` — no trailing question; pending task = next action
- `feedback_spawn_freely.md` — safe teammate spawn doesn't need per-PR confirm

AGENTS.md (global) sections that apply:
- Non-Negotiables #1 (no AI attribution), #2 (English only), #5 (PR workflow only), #7 (leader-vs-teammate)
- `## PR Standards` (commits, comment management, resolve policy)
- Phase 9 (push: rebase before push, PR description reflects latest)
- Phase 10 (feedback: `git commit --fixup` + `git rebase --autosquash`)

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
type(JIRA-TICKET): brief imperative description
```
- Types: `feat`, `fix`, `refactor`, `test`, `docs`, `perf`, `chore`
- Imperative mood, lowercase after colon, under 72 chars, no trailing period
- The fixup-and-autosquash workflow preserves the original commit's message, so getting the format right on first commit matters

**Branch naming** (Phase 2):
```
task/<JIRA-TICKET>          # e.g., task/QAYAK-346
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

Closes <JIRA-TICKET>
```

The closing keyword (`Closes JIRA-TICKET` or `Fixes #N`) lets GitHub/Jira auto-transition the linked ticket on merge — include it in the body, not just the commit message.

When PR scope changes mid-flight (per `feedback_amend_over_close.md`), update the body to reflect the latest state — Phase 9: "Ensure PR description reflects the latest changes". Don't leave a stale Summary describing the original scope.

**Autosquash vs squash merge** (commonly confused):
- `git rebase -i --autosquash <base>` (LOCAL, post-fixup) — **REQUIRED** by checklist step 9
- `gh pr merge --squash` (REMOTE, at merge time) — **FORBIDDEN** by `feedback_merge_strategy.md`
- They're different operations. Local autosquash collapses fixup commits into their target before push. Squash merge collapses ALL commits into one at merge — that's the prohibited one.

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
8.  Check running workflow — surface to user if any
9.  Rebase (GIT_SEQUENCE_EDITOR=true git rebase -i --autosquash <base>)
10. Push (alone, last step)
```

**Critical ordering rule — cleanup BEFORE push within each round**:

1. Make all code edits locally (steps 1-2: fix + fixup commit)
2. Do thread/issue/review-body cleanup (steps 3-7) — reply, resolve, minimize, dismiss
3. THEN rebase --autosquash (step 9) and push (step 10)

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
| 5. Finalize + navigate + merge | 8, 9, 10, merge gates | Workflow check, rebase, push, merge |

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
- **`headRefOid`** is the PR HEAD SHA at snapshot time. Re-check this after Stage 4 — if it changed, a concurrent push happened and the cleanup may be stale; restart from Stage 1.
- **`baseRefName`** is the actual base branch — don't assume `main`. Use this everywhere later commands say `origin/main` (e.g., `gh pr update-branch`, `git rebase --autosquash`).

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
| **Deferred** | `Deferred to TICKET-XXX` (ticket MUST exist + verified before posting — **NEVER "will defer later" / "follow-up will be filed" without an actual ticket key**) | Valid issue but out of scope for this PR — only after a real Jira ticket exists |
| **Won't fix** | `By design: <rationale>` with reasoning | Intentional decision — explain why |
| **Duplicate** | `Handled in thread #N` with thread reference | Same issue raised in another thread |
| **Outdated** | (Minimize via `minimizeComment`, no reply needed) | Superseded by newer review round |

Critical rules from AGENTS.md `## Resolve Policy`:
- Every close needs a reply — silent resolves are prohibited
- "Acknowledged" alone is never a valid close reason
- Deferring requires the target ticket to actually exist
- Bot duplicates (same issue across review rounds) → resolve as **Duplicate** referencing the canonical thread

For **Deferred** disposition: the target ticket **MUST exist as a real Jira / GitHub issue with a concrete key** before posting the reply. Phrases like "will defer to a future ticket", "follow-up will be filed", "deferred to next sprint", or "별 ticket 으로 처리" without a key are **NOT valid Deferred replies** — they ship a promise nobody will track. Two-step procedure:

**Step 1 — Create the ticket first (if it doesn't exist)**
If the ticket doesn't exist yet, create it via Atlassian MCP (`mcp__claude_ai_Atlassian__createJiraIssue`) or `gh issue create` BEFORE posting the thread reply. Capture the new key.

**Step 2 — Verify the key exists, then reply**
```bash
# Jira
mcp__claude_ai_Atlassian__getJiraIssue cloudId=<id> issueIdOrKey=<TICKET>
# or: gh api "https://<site>.atlassian.net/rest/api/3/issue/<TICKET>" 2>/dev/null

# GitHub issue
gh api repos/O/R/issues/<N> --jq .number 2>/dev/null
```

If either lookup fails — the ticket key is invalid or doesn't exist — **pick a different disposition** (Fixed / Won't fix / Duplicate). Never post a Deferred reply pointing at a phantom key. A deferred-to-nothing reply ships a lie that gets merged.

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
3. **Leader integrates** the teammate's branch back into the PR branch (still local, no push). For the default flow (teammate didn't push), use **Cross-worktree fixup integration** below — no fetch needed since worktrees share `$GIT_COMMON_DIR/objects/`. If the teammate explicitly pushed their branch (non-default path), use the fetch form:
   ```bash
   git fetch origin <teammate-branch>:<teammate-branch>
   git checkout <pr-branch>
   git merge --ff-only <teammate-branch>   # if teammate worked on a sibling branch
   # OR if teammate worked directly on the PR branch:
   git pull --rebase origin <pr-branch>
   ```
4. **Defer rebase + push to Stage 5** — checklist steps 9 (rebase --autosquash) and 10 (push) run AFTER Stage 4 cleanup. Stage 3 ends with the fixup commit local, no remote update.

**Cross-worktree fixup integration** (common when the teammate worked on a `worktree-agent-<id>` branch, not on the PR branch directly):

If the teammate replies with a fixup commit SHA on their own worktree branch (not on `task/<TICKET>`), the leader integrates via cherry-pick into a worktree that owns the PR branch:

```bash
# 1. The PR branch may already be held in another worktree (a leftover from
#    an earlier teammate, or the original spawn). Find it:
git worktree list | grep '<pr-branch>'

# 2. If a worktree owns the branch, cd there. Otherwise pick any worktree
#    and use `--ignore-other-worktrees` to override the dual-checkout lock
#    (the flag overrides the safety lock; each worktree still keeps its own
#    HEAD at `$GIT_COMMON_DIR/worktrees/<id>/HEAD`):
git checkout --ignore-other-worktrees <pr-branch>

# 3. Cherry-pick the teammate's fixup commit. No `git fetch` needed —
#    Stage 3 says the teammate does NOT push, and all worktrees share
#    `$GIT_COMMON_DIR/objects/`, so the fixup SHA is already reachable
#    locally via the agent's worktree.
git cherry-pick <fixup-sha>
#    If cherry-pick reports `error: could not apply <sha>...`, it hit a merge
#    conflict during patch application. Resolve the conflict markers in the
#    affected files, then `git add <conflicted-files>` followed by
#    `git cherry-pick --continue`. To abort entirely: `git cherry-pick --abort`.
#
#    A separate, distinct pre-flight error message `error: Your local changes
#    to the following files would be overwritten by merge` indicates the
#    current worktree has uncommitted changes that would be overwritten — that
#    one is fixed by `git stash` first, then retry. Each worktree has its own
#    isolated index, so uncommitted edits in another worktree cannot cause a
#    cherry-pick conflict in the current worktree.
#
#    If cherry-pick says "The previous cherry-pick is now empty, possibly
#    due to conflict resolution" with a hint to use `git cherry-pick --skip`,
#    the fixup's diff is already on this branch (already applied in a prior
#    cherry-pick, or both branches independently arrived at the same change)
#    — `git cherry-pick --skip` to drop the empty commit and proceed.

# 4. Defer Autosquash + push to Stage 5 (Do NOT run these until Stage 4
#    cleanup is complete):
# GIT_SEQUENCE_EDITOR=true git rebase -i --autosquash origin/<baseRefName>
# git push --force-with-lease origin <pr-branch>
```

This is necessary because:
- `Agent(isolation: "worktree")` creates a fresh worktree on a `worktree-agent-<id>` branch by default, NOT the PR branch.
- When the teammate runs `git commit --fixup=<sha>`, the fixup lands on the agent's worktree branch, not the PR branch.
- All worktrees in the same repo share `$GIT_COMMON_DIR/objects/`, so the leader can cherry-pick the teammate's local-only commit without any remote round-trip.

If the teammate explicitly checked out the PR branch (e.g., via `--ignore-other-worktrees`), the fixup is already on the right branch — skip cherry-pick, just rebase + push.

**Worktree race troubleshooting**:
- `error: 'task/X' is already used by worktree at /path/Y` — another worktree holds the branch. Either `cd` to that worktree and work there, or use `--ignore-other-worktrees` to override (the worktrees still keep separate HEADs; the override just bypasses the dual-checkout safety lock).
- Cherry-pick reports empty commit / suggests `--skip` — the fixup diff is already on this branch. Run `git cherry-pick --skip` to drop the empty commit and proceed to autosquash.

Trivial exceptions where leader edits directly: reply posts to review threads (text only), Jira comments, PR descriptions. NOT code/config/doc files.

For **PR scope changes** (rename, restructure, new requirements): per `feedback_amend_over_close.md`, **amend the existing PR** (force-push history rewrite). NEVER close + open new PR. The existing review threads (resolved + unresolved) and Jira link continuity are why. Update the PR body too (Phase 9).

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

**Review body dismiss rule (strict)**: when code changes are made on the PR (any fixup commit, any new push affecting reviewable code), ALL prior review bodies must be dismissed — both APPROVED and CHANGES_REQUESTED. The new HEAD has not been re-reviewed, so prior approvals are stale by definition.

**Issue comment quoted-response format** (per `process_review_push_checklist.md` step 4):
```
> 봇이 지적한 항목 1: <quote>
Won't fix — <rationale + evidence>

> 봇이 지적한 항목 2: <quote>
Fixed in <commit-sha>: <what changed>

> 봇이 지적한 항목 3: <quote>
Deferred to TICKET-XXX: <out-of-scope reason>
```

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

**Step 9 — local rebase --autosquash IS MANDATORY before push** (use `baseRefName` from Stage 1):
```bash
GIT_SEQUENCE_EDITOR=true git rebase -i --autosquash origin/<baseRefName>
```

This collapses `fixup! <original>` commits into their target commits. Without this step, pushing leaves literal `fixup! <message>` commit titles in remote history — visible in PR commit list, ugly in `git log`, and hard for reviewers to follow. The autosquash is non-optional; verify before push:
```bash
git log --oneline origin/<baseRefName>..HEAD | grep -i '^[a-f0-9]* fixup!' \
  && echo "ERROR: fixup commits not squashed — run autosquash first" \
  || echo "OK — clean history"
```

**Step 10 — push** (single push at end of round, force-with-lease for amended history):
```bash
git push --force-with-lease origin <branch>
```
Always `--force-with-lease`, never `--force`. Lease guards against silently overwriting concurrent updates.

**Step 8 — running workflow check** (before merge gate):
```bash
gh run list --repo OWNER/REPO --branch <branch> --status in_progress --limit 5
```
If any are running, surface to user and wait — don't merge while CI is mid-flight. Also wait for the post-push bot review round to complete (which becomes the NEXT round's input if it adds threads).

After steps 9, 10, 8, re-snapshot. Act on state:

| State | Diagnosis | Action |
|-------|-----------|--------|
| `CLEAN + APPROVED` | Ready | Merge (see below) |
| `BEHIND` | Branch behind base | `gh pr update-branch N` then re-snapshot |
| `BLOCKED + APPROVED + 0 unresolved` | Required check failing/pending | Identify check, fix root cause; if check passes but `gh pr merge` still refuses, see REST API fallback below |
| `BLOCKED + REVIEW_REQUIRED` (or `reviewDecision: empty`) | **First, verify with raw data — `mergeStateStatus` / `reviewDecision` can be stale cache.** Run `gh api repos/.../pulls/N/reviews --paginate --jq '.[] | select(.state=="APPROVED") \| {commit_id, user: .user.login}'`. If at least one APPROVED's `commit_id == headRefOid`, the ruleset is actually satisfied — try REST API merge directly (see fallback below). Bot reviewers (`github-actions[bot]`, `claude[bot]`, etc.) count toward `required_approving_review_count` unless CODEOWNERS or `required_reviewers` explicitly excludes them. ONLY if zero fresh APPROVED on current HEAD: wait for reviewer. **Do NOT propose admin merge.** Per `feedback_no_admin_merge_option.md`: admin merge MUST NOT appear in status reports as a path/fallback |
| `UNSTABLE` | Non-required check failed | Often mergeable; verify per-project. For flaky CI: `gh run rerun <run-id> --failed` — only retry transient failures (network, runner provisioning), not legitimate test failures |
| `DIRTY` | Merge conflict | Rebase or merge base; resolve conflicts |

#### Standard merge

```bash
gh pr merge N --repo OWNER/REPO --merge
```

**`--merge` (merge commit), NEVER `--squash` or `--rebase`** — per `feedback_merge_strategy.md`. Individual commit history must be preserved.

#### REST API merge fallback (only when ALL guards pass)

Per `feedback_no_admin_merge.md` Rule 2: when `gh pr merge` refuses with `the base branch policy prohibits the merge` AND ALL these guards hold:
- PR has at least one APPROVED review (any reviewer, including bots with `authorAssociation: NONE`)
- All required checks pass
- 0 unresolved threads
- Branch up-to-date with base

Then REST API direct merge is acceptable:

```bash
gh api --method PUT repos/OWNER/REPO/pulls/N/merge \
  -f merge_method=merge \
  -f commit_title="Merge pull request #N from BRANCH" \
  -f commit_message="<short summary>"
```

This is NOT admin bypass — it's working around `gh pr merge`'s overly conservative pre-check + GitHub's stale `mergeStateStatus` cache. GitHub server-side asynchronously recomputes `mergeStateStatus` and `reviewDecision`, so they can lag the actual ruleset evaluation by several minutes after a state-changing event (push, review, check). The 4 guards above prove the ruleset is actually satisfied. If REST returns 200 OK + a merge SHA, the ruleset was satisfied all along (CLI cache was stale). If REST returns 405/422, the response body's `message` field gives the real reason — that's the natural blocker, act on it.

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

Branch deletion + Jira transition are out of this skill's scope — handle separately.

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

The value was also applied via the live API to all ECR repos in our account today. Bot reference appears stale relative to the 2024 ECR exclusion-filter release.
```

Pattern: assertion + verification command + applied-evidence + likely-cause-of-bot-error.

## Anti-patterns (catch these before they happen)

- **AI attribution** — never write `Co-Authored-By: Claude`, `Generated with`, or any AI mention in any artifact. Non-Negotiable #1.
- **Korean in commits/PR titles/PR bodies** — Non-Negotiable #2: code artifacts are English only. Thread replies/issue comments can be Korean.
- **Silent resolve** — close without reply. Every close needs a reply, no exceptions.
- **Admin merge in status report** — `feedback_no_admin_merge_option.md`: never frame admin merge as a path. Status report = "review thread X 해결 + 인간 review 대기".
- **Trailing question** — "should I merge?" when CLEAN + APPROVED + policy says yes. Per `feedback_no_unjustified_wait.md`: just merge.
- **`--squash` at merge time** — `feedback_merge_strategy.md` explicitly forbids. Always `--merge` for the remote merge. Local `git rebase --autosquash` is fine and required (different operation).
- **Close + new PR for scope change** — `feedback_amend_over_close.md`: amend the existing PR via force-push.
- **Forgetting to update PR body after scope change** — Phase 9: PR description must reflect the latest changes.
- **`--force` instead of `--force-with-lease`** — race-unsafe; always use `--force-with-lease`.
- **Pushing `fixup!` commits without autosquash** — `git commit --fixup=<sha>` creates a commit with `fixup! <original message>` as the title. Autosquash MUST run before push to collapse them into the target commit. Pushing literal `fixup!` commits pollutes remote history and forces a follow-up cleanup commit.
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
- **Merging during in-flight CI** — checklist step 8: check `gh run list ... --status in_progress` first.

## Compatibility

- `gh` CLI authenticated for the target repo
- GraphQL access (default with `gh` auth)
- For multi-PR / bulk operations, parallel `gh api` calls work; throttle at >100 mutations/min
- Project memory directory accessible at `~/.claude/projects/<project-encoded-cwd>/memory/`
- AGENTS.md global at `~/.claude/AGENTS.md`
