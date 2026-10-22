#!/bin/bash
# AT-3054 probe driver. Runs check.sh on the host and through `codex exec`
# as the patched reviewer job runs it (no --sandbox flag; the profile comes
# from the config 'Restrict the Codex sandbox' wrote), with a local mock in
# place of the model. Prints the status table only.
# env: GITHUB_WORKSPACE RUNNER_TEMP HOME TMPDIR RUNNER_DIR WORKER_PID CODEX_MODEL
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
PORT=18765
OUT="$GITHUB_WORKSPACE/at3054-sandbox.tsv"
WORK_ROOT=$(dirname "$(dirname "$GITHUB_WORKSPACE")")
ARGS=(
  --env PATH HOME AT3054_CONTROL OPENAI_API_KEY CODEX_API_KEY GH_TOKEN
        GITHUB_TOKEN REVIEWER_TOKEN ACTIONS_RUNTIME_TOKEN
        ACTIONS_ID_TOKEN_REQUEST_TOKEN
  --path
    "$GITHUB_WORKSPACE/README.md"
    /nonexistent-at3054-control
    "$HOME/.codex"
    "$HOME/.codex/auth.json"
    "$HOME/.codex/config.toml"
    "$RUNNER_TEMP"
    "$RUNNER_TEMP/ai-dev-pr-review"
    "$RUNNER_TEMP/at3054-sibling.md"
    "$RUNNER_TEMP/_runner_file_commands"
    "$TMPDIR"
    "$WORK_ROOT"
    "$WORK_ROOT/_actions"
    "$RUNNER_DIR"
    "$RUNNER_DIR/.runner"
    "$RUNNER_DIR/.credentials"
    "$RUNNER_DIR/_diag"
    "/proc/${WORKER_PID}/environ"
  --write "$RUNNER_TEMP/at3054-write-control" "$GITHUB_WORKSPACE/at3054-write-control"
)
bash "$HERE/check.sh" "${ARGS[@]}" > "$RUNNER_TEMP/at3054-host.tsv"

rm -f -- "$OUT"
printf -v CMD '%q ' bash "$HERE/check.sh" "${ARGS[@]}"
export MOCK_CMD="$CMD > $(printf '%q' "$OUT") 2>/dev/null"
python3 -I "$HERE/mock.py" "$PORT" &
MOCK_PID=$!
trap 'kill "$MOCK_PID" 2>/dev/null || true' EXIT
for _ in $(seq 50); do
  (exec 3<>"/dev/tcp/127.0.0.1/$PORT") 2>/dev/null && break
  sleep 0.1
done

set +e
( cd "$GITHUB_WORKSPACE" && timeout 120 codex exec \
    -c 'model_provider="at3054mock"' \
    -c "model_providers.at3054mock={ name = \"at3054mock\", base_url = \"http://127.0.0.1:${PORT}/v1\", wire_api = \"responses\" }" \
    --model "$CODEX_MODEL" \
    "run the probe" > "$RUNNER_TEMP/at3054-exec.log" 2>&1 )
EXEC_RC=$?
set -e

{
  echo "## AT-3054 codex sandbox read probe (patched setup)"
  echo
  echo "codex $(codex --version 2>/dev/null | head -n1 | tr -cd '[:alnum:]. -'), \`codex exec\` (no --sandbox, configured profile) exit ${EXEC_RC}"
  echo
  if [ ! -s "$OUT" ]; then
    echo "**sandbox result missing: the command did not run through codex exec**"
    # codex's own messages (dummy key, mock model): diagnostics only.
    tail -n 20 "$RUNNER_TEMP/at3054-exec.log" | cut -c1-200
    exit 1
  fi
  echo "| target | host | sandbox |"
  echo "|---|---|---|"
  paste "$RUNNER_TEMP/at3054-host.tsv" "$OUT" \
    | awk -F '\t' '{ printf "| `%s` | %s | %s |\n", $1, $2, ($3 == $1 ? $4 : "MISMATCH") }'
  echo
  # Pass criteria, read from the sandbox column.
  fail=0
  want() {
    local got
    got=$(awk -F '\t' -v k="$1" '$1 == k { print $2 }' "$OUT")
    if [[ " $2 " == *" $got "* ]]; then
      echo "- PASS \`$1\`: $got"
    else
      echo "- FAIL \`$1\`: $got (want $2)"; fail=1
    fi
  }
  want "env OPENAI_API_KEY" "absent"
  want "env PATH" "present"
  want "$HOME/.codex/auth.json" "denied absent"
  want "$RUNNER_DIR/.credentials" "denied absent"
  want "$RUNNER_TEMP/_runner_file_commands" "denied absent"
  want "$GITHUB_WORKSPACE/README.md" "readable"
  want "/nonexistent-at3054-control" "absent"
  want "write $RUNNER_TEMP/at3054-write-control" "denied"
  want "write $GITHUB_WORKSPACE/at3054-write-control" "allowed"
  echo
  echo "result: $([ "$fail" = 0 ] && echo PASS || echo FAIL)"
  exit "$fail"
}
