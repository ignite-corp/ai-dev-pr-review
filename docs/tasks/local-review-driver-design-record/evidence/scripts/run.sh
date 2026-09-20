#!/usr/bin/env bash
# Full end-to-end run of the local review driver against ignite-corp/ai-dev-pr-review PR 170.
# Only deviations from stock defaults:
#   PR_SIZE_LIMIT raised (PR is 6067 lines; workflow default is 3000 and would size-skip)
#   --system-prompt-path/--checklist-path pointed at examples/prompts, where this repo
#   actually keeps them (self-review.yml passes the same two paths)
WT=/home/hyukhur/Sources/ai-dev-pr-review/.claude/worktrees/agent-af2dbb3734da99b6f
cd "$WT" || exit 9
export GOOGLE_AI_API_KEY="$(sed -n 's/^GOOGLE_AI_API_KEY=//p' "$HOME/.config/lens/credentials.env")"
export PR_SIZE_LIMIT=20000
S=$(date +%s)
echo "START $(date -Is)"
{
  timeout --signal=INT 2400 python3 -u "$WT/.github/scripts/review_pr_local.py" \
    ignite-corp/ai-dev-pr-review 170 \
    --run-dir /tmp/lens-harvest/run \
    --system-prompt-path examples/prompts/code-review-system.md \
    --checklist-path examples/prompts/code-review-checklist.md
  echo "DRIVER_EXIT=$?" > /tmp/lens-harvest/run-exit.txt
} 2>&1 | awk '{printf "%s %s\n", strftime("%H:%M:%S"), $0; fflush()}' | tee /tmp/lens-harvest/run.log
cat /tmp/lens-harvest/run-exit.txt
echo "WALL_SECONDS=$(( $(date +%s) - S ))"
echo "END $(date -Is)"
