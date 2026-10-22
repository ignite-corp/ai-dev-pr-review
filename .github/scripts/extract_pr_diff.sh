#!/usr/bin/env bash
# Write pr.diff, the unified diff the reviewers read, for the PR under review.
#
# An open PR is diffed exactly as before: `git diff origin/BASE...HEAD`, the
# head against its merge-base with the base branch. Once the PR is merged the
# base branch contains the head, the merge-base IS the head, and that diff is
# empty -- so a merged PR re-reviewed through workflow_dispatch reviewed
# nothing and the run reported success (AT-2201).
#
# A merged PR is diffed the same way, against the base as it stood when the
# PR landed: `git diff MERGE^1...HEAD`. MERGE^1 is on the base the merge,
# squash or rebase was applied onto -- for a rebase, the previously replayed
# commit, a copy that is not in HEAD's history -- so its merge-base with HEAD
# is where the PR branched (or last took the base in) and the diff is the
# PR's own change, ending at HEAD. Merge, squash and rebase are handled
# alike: nothing here needs to tell them apart.
#
# Ending at HEAD is the point (AT-2271). Every reviewer job checks out HEAD
# and asserts it (AT-2038). A diff taken from the landed commit instead
# (`MERGE^1 MERGE`) ends at the landed tree, which differs from HEAD wherever
# the merge resolved a conflict or the base had moved in a file the PR also
# touched -- the reviewers then read lines, and line numbers, that the tree
# they open does not hold. What a merge itself introduced is not part of the
# PR and is not reviewed here.
#
# `gh pr diff` -- GitHub's own record of the PR, a diff ending at its head --
# is the fallback when the merge commit is unknown, cannot be fetched, shares
# no history with HEAD in this clone, cannot be compared against it at all,
# is itself in HEAD's history, or yields an empty diff against it. A merge
# commit in HEAD's history is a fast-forward landing: the PR's own commits
# are on the base, so MERGE^1 may be one of them and the three-dot diff
# would drop the earlier ones; nothing local says where the PR starts.
#
# An empty pr.diff never passes, whichever strategy produced it: the step
# fails naming the strategy, and the aggregate renders a failed prepare as an
# explicit verdict (AT-2087) instead of three reviewers approving nothing.
#
# Env: BASE_REF, HEAD_SHA, PR_NUMBER, GITHUB_REPOSITORY, GH_TOKEN,
#      PR_MERGED ("true" once merged), MERGE_COMMIT_SHA (empty unless merged).
set -euo pipefail

OUT="pr.diff"
PR_MERGED="${PR_MERGED:-false}"
MERGE_COMMIT_SHA="${MERGE_COMMIT_SHA:-}"

# The merge commit is usable when it is (or can be) fetched and has a parent.
merge_commit_usable() {
  git cat-file -e "${MERGE_COMMIT_SHA}^{commit}" 2>/dev/null \
    || git fetch --no-tags origin "$MERGE_COMMIT_SHA" \
    || return 1
  git rev-parse --verify --quiet "${MERGE_COMMIT_SHA}^1" >/dev/null
}

if [ "$PR_MERGED" != "true" ]; then
  STRATEGY="open PR: git diff origin/${BASE_REF}...${HEAD_SHA}"
  git diff "origin/${BASE_REF}...${HEAD_SHA}" > "$OUT"
else
  STRATEGY=""
  if [ -z "$MERGE_COMMIT_SHA" ]; then
    echo "::warning::PR #${PR_NUMBER} is merged but has no merge commit; falling back to gh pr diff"
  elif ! merge_commit_usable; then
    echo "::warning::merge commit ${MERGE_COMMIT_SHA} of PR #${PR_NUMBER} is unreachable or has no parent; falling back to gh pr diff"
  else
    # merge-base exits 1 for unrelated histories and 128 for anything else
    # (an unresolvable HEAD_SHA, say). The warning is the operator's only
    # account of why the local strategy was abandoned, so it says which.
    rc=0
    git merge-base "${MERGE_COMMIT_SHA}^1" "$HEAD_SHA" >/dev/null 2>&1 || rc=$?
    if [ "$rc" -eq 1 ]; then
      echo "::warning::${MERGE_COMMIT_SHA}^1 and head ${HEAD_SHA} of PR #${PR_NUMBER} share no history in this clone; falling back to gh pr diff"
    elif [ "$rc" -ne 0 ]; then
      echo "::warning::merge-base of ${MERGE_COMMIT_SHA}^1 and head ${HEAD_SHA} of PR #${PR_NUMBER} could not be computed (git exited ${rc}); falling back to gh pr diff"
    else
      # --is-ancestor: 0 yes, 1 no, anything else an error, which falls
      # back like the merge-base error above rather than aborting the step.
      git merge-base --is-ancestor "$MERGE_COMMIT_SHA" "$HEAD_SHA" >/dev/null 2>&1 || rc=$?
      if [ "$rc" -eq 0 ]; then
        echo "::warning::merge commit ${MERGE_COMMIT_SHA} is in the history of head ${HEAD_SHA} (a fast-forward landing), so where PR #${PR_NUMBER} starts cannot be told locally; falling back to gh pr diff"
      elif [ "$rc" -ne 1 ]; then
        echo "::warning::ancestry of merge commit ${MERGE_COMMIT_SHA} and head ${HEAD_SHA} of PR #${PR_NUMBER} could not be determined (git exited ${rc}); falling back to gh pr diff"
      else
        STRATEGY="merged PR: git diff ${MERGE_COMMIT_SHA}^1...${HEAD_SHA}"
        git diff "${MERGE_COMMIT_SHA}^1...${HEAD_SHA}" > "$OUT"
        # Empty when HEAD's tree matches MERGE^1's -- for instance the head
        # reached the base by another route before this commit. GitHub's
        # record can still serve the PR, so fall back rather than fail here.
        if [ ! -s "$OUT" ]; then
          echo "::warning::git diff ${MERGE_COMMIT_SHA}^1...${HEAD_SHA} is empty for PR #${PR_NUMBER} (the head's tree matches its merge-base with ${MERGE_COMMIT_SHA}^1); falling back to gh pr diff"
          STRATEGY=""
        fi
      fi
    fi
  fi
  if [ -z "$STRATEGY" ]; then
    STRATEGY="merged PR: gh pr diff ${PR_NUMBER}"
    gh pr diff "$PR_NUMBER" --repo "$GITHUB_REPOSITORY" > "$OUT"
  fi
fi

if [ ! -s "$OUT" ]; then
  echo "::error title=Empty diff::pr.diff is empty after '${STRATEGY}' (PR #${PR_NUMBER}, merged=${PR_MERGED}); nothing to review, refusing to report success"
  exit 1
fi
echo "pr.diff: $(wc -l < "$OUT") lines via ${STRATEGY}"
