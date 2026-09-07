#!/usr/bin/env bash
# Write pr.diff, the unified diff the reviewers read, for the PR under review.
#
# An open PR is diffed exactly as before: `git diff origin/BASE...HEAD`, the
# head against its merge-base with the base branch. Once the PR is merged the
# base branch contains the head, the merge-base IS the head, and that diff is
# empty -- so a merged PR re-reviewed through workflow_dispatch reviewed
# nothing and the run reported success (AT-2201).
#
# A merged PR is therefore diffed from what actually landed: the merge commit
# against its first parent, when that is known to be the whole PR. It is
# known structurally, never inferred from line counts (net totals collide):
#   - a merge commit has two parents, and its first-parent diff is by
#     definition everything the merge brought onto the base;
#   - a squash or rebase merge lands single-parent commits, and
#     merge_commit_sha names the last one. For a one-commit PR that commit is
#     the whole PR either way; for a longer PR a rebase left the earlier
#     commits behind it and nothing local tells a squash from a rebase, so
#     `gh pr diff` -- GitHub's own record of the PR -- is used instead,
# as it is when the merge commit is unknown or cannot be fetched.
#
# An empty pr.diff never passes, whichever strategy produced it: the step
# fails naming the strategy, and the aggregate renders a failed prepare as an
# explicit verdict (AT-2087) instead of three reviewers approving nothing.
#
# Env: BASE_REF, HEAD_SHA, PR_NUMBER, GITHUB_REPOSITORY, GH_TOKEN,
#      PR_MERGED ("true" once merged), MERGE_COMMIT_SHA (empty unless merged),
#      PR_COMMITS (number of commits on the PR, as the API reports it).
set -euo pipefail

OUT="pr.diff"
PR_MERGED="${PR_MERGED:-false}"
MERGE_COMMIT_SHA="${MERGE_COMMIT_SHA:-}"
PR_COMMITS="${PR_COMMITS:-}"

# The merge commit is usable when it is (or can be) fetched and has a parent.
merge_commit_usable() {
  git cat-file -e "${MERGE_COMMIT_SHA}^{commit}" 2>/dev/null \
    || git fetch --no-tags origin "$MERGE_COMMIT_SHA" \
    || return 1
  git rev-parse --verify --quiet "${MERGE_COMMIT_SHA}^1" >/dev/null
}

# Number of parents of a commit.
parent_count() {
  git rev-list --parents -n 1 "$1" | awk '{ print NF - 1 }'
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
  elif [ "$(parent_count "$MERGE_COMMIT_SHA")" -ge 2 ] || [ "$PR_COMMITS" = "1" ]; then
    STRATEGY="merged PR: git diff ${MERGE_COMMIT_SHA}^1 ${MERGE_COMMIT_SHA}"
    git diff "${MERGE_COMMIT_SHA}^1" "$MERGE_COMMIT_SHA" > "$OUT"
  else
    echo "::warning::merge commit ${MERGE_COMMIT_SHA} has one parent and PR #${PR_NUMBER} has ${PR_COMMITS:-an unknown number of} commits; a squash cannot be told from a rebase, falling back to gh pr diff"
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
