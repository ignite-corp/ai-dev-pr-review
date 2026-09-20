#!/usr/bin/env bash
# Emit one line whenever a reviewer artifact appears or the PR gains a comment.
# Exits when the driver process is gone.
D=/tmp/lens-harvest/run/repo
seen=""
pr_before=$(wc -l < /tmp/lens-harvest/before-issue-comments.txt)
rc_before=$(wc -l < /tmp/lens-harvest/before-review-comments.txt)
while true; do
  for f in review-claude.json review-codex.json review-gemini.json claude-exec.json verdict-codex.json verdict-openai.json; do
    if [ -s "$D/$f" ] && [[ "$seen" != *"|$f|"* ]]; then
      echo "ARTIFACT $f $(stat -c%s "$D/$f") bytes"
      seen="$seen|$f|"
    fi
  done
  if ! pgrep -f "review_pr_local.py ignite-corp" > /dev/null 2>&1; then
    echo "DRIVER PROCESS GONE"
    n=$(gh api repos/ignite-corp/ai-dev-pr-review/issues/170/comments --paginate --jq '.[].id' 2>/dev/null | wc -l)
    m=$(gh api repos/ignite-corp/ai-dev-pr-review/pulls/170/comments --paginate --jq '.[].id' 2>/dev/null | wc -l)
    echo "PR170 issue-comments $pr_before -> $n ; review-comments $rc_before -> $m"
    break
  fi
  # safety: shout if the PR gains an implausible number of inline comments
  m=$(gh api repos/ignite-corp/ai-dev-pr-review/pulls/170/comments --paginate --jq '.[].id' 2>/dev/null | wc -l)
  if [ "$m" -gt $((rc_before + 40)) ]; then
    echo "ALERT inline comments jumped $rc_before -> $m"
  fi
  sleep 30
done
