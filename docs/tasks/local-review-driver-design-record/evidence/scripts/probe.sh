#!/usr/bin/env bash
# Probe run: default prompt paths, size limit raised out of the way.
WT=/home/hyukhur/Sources/ai-dev-pr-review/.claude/worktrees/agent-af2dbb3734da99b6f
cd "$WT" || exit 9
export GOOGLE_AI_API_KEY="$(sed -n 's/^GOOGLE_AI_API_KEY=//p' "$HOME/.config/lens/credentials.env")"
export PR_SIZE_LIMIT=20000
S=$(date +%s)
{
  timeout --signal=INT 900 python3 -u "$WT/.github/scripts/review_pr_local.py" \
    ignite-corp/ai-dev-pr-review 170 --run-dir /tmp/lens-harvest/run
  echo "DRIVER_EXIT=$?" > /tmp/lens-harvest/probe-exit.txt
} 2>&1 | awk '{printf "%s %s\n", strftime("%H:%M:%S"), $0; fflush()}' | tee /tmp/lens-harvest/probe.log
cat /tmp/lens-harvest/probe-exit.txt
echo "WALL_SECONDS=$(( $(date +%s) - S ))"
