#!/usr/bin/env bash
# Paginate PR reviewThreads via GraphQL and write unresolved threads
# to a JSON artifact so single reviewers avoid re-raising prior findings.
set -euo pipefail

OWNER="${GITHUB_REPOSITORY%%/*}"
REPO="${GITHUB_REPOSITORY##*/}"

QUERY='query($owner: String!, $repo: String!, $pr: Int!, $cursor: String) {
  repository(owner: $owner, name: $repo) {
    pullRequest(number: $pr) {
      reviewThreads(first: 100, after: $cursor) {
        totalCount
        pageInfo { hasNextPage endCursor }
        nodes {
          isResolved
          isOutdated
          path
          line
          comments(first: 1) {
            nodes {
              body
              author { login }
            }
          }
        }
      }
    }
  }
}'

ALL_THREADS="[]"
HAS_NEXT=true
CURSOR="null"

while [ "$HAS_NEXT" = "true" ]; do
  if [ "$CURSOR" = "null" ]; then
    RESULT=$(gh api graphql -f query="$QUERY" \
      -f owner="$OWNER" -f repo="$REPO" -F pr="$PR_NUMBER")
  else
    RESULT=$(gh api graphql -f query="$QUERY" \
      -f owner="$OWNER" -f repo="$REPO" -F pr="$PR_NUMBER" \
      -f cursor="$CURSOR")
  fi

  PAGE=$(echo "$RESULT" | jq '.data.repository.pullRequest.reviewThreads.nodes')
  ALL_THREADS=$(echo "$ALL_THREADS $PAGE" | jq -s 'add')

  HAS_NEXT=$(echo "$RESULT" | jq -r '.data.repository.pullRequest.reviewThreads.pageInfo.hasNextPage')
  CURSOR=$(echo "$RESULT" | jq -r '.data.repository.pullRequest.reviewThreads.pageInfo.endCursor')
done

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
UNRESOLVED=$(echo "$ALL_THREADS" | jq -f "${SCRIPT_DIR}/threads.jq")

mkdir -p .review-context
echo "$UNRESOLVED" > .review-context/unresolved-threads.json
THREAD_COUNT=$(echo "$UNRESOLVED" | jq 'length')
echo "Collected ${THREAD_COUNT} unresolved review thread(s)"
