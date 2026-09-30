#!/usr/bin/env bash
# Promote a legacy-named reviewer verdict onto the canonical file (AT-2424).
#
# The base prompt may still instruct the model to write the older names, so
# a real verdict can arrive as verdict-openai.json or verdict-codex.json.
# Two steps of base-ai-review-single.yml need that promotion:
#
#   - 'Run Codex review', BEFORE it writes any error verdict of its own.
#     Promoting after that point is what AT-2424 was filed for: every
#     fallback path writes review-codex.json, and a promotion that skips
#     when the canonical file exists then discards the model's real verdict
#     in favour of our synthesized failure.
#   - 'Normalize review file name', the net for a run step that was
#     cancelled or killed by its timeout before reaching its own promotion.
#
# This file is the ONE definition of what a candidate is, how it is stamped
# and when it may be written: two copies of the loop had already drifted
# apart within a single PR, one of them truncating its target on a
# candidate jq could not read.
#
# PROVENANCE. The caller repo is checked out at the workspace root with no
# `path:`, so every name this script touches is a path the PR under review
# controls: it can commit any of them, as a file, a directory or a symlink.
# `clear` runs immediately before the CLI and removes the candidates AND
# the target, so afterwards the mere existence of one is proof this run
# wrote it. The target is cleared too because the argument applies to it
# verbatim -- a review-codex.json committed by the PR is taken by the run
# step's own direct-write branch, which would let a PR ship its own
# approving verdict on every path, not merely the failing ones.
#
# Nothing here compares timestamps: the CLI runs with
# `--sandbox workspace-write`, so a model induced to touch a file it did
# not create can refresh any mtime, which makes an "is it newer than the
# start of the run" test answerable by the attacker it is meant to stop.
#
# Usage:
#   promote_legacy_verdict.sh clear <target>    remove target + candidates
#   promote_legacy_verdict.sh promote <target>  promote one onto <target>,
#       the target being the CLI's own write from this run
#   promote_legacy_verdict.sh promote --target-is-ours <target>
#       the same, where the run step has already adjudicated the target
#   promote_legacy_verdict.sh stamp <target>    stamp a direct write in place
set -euo pipefail

LEGACY_VERDICT_FILES=(verdict-openai.json verdict-codex.json)

usage() {
  echo "usage: $0 clear <target>" \
    "| promote [--target-is-ours] <target>" \
    "| stamp <target>" >&2
  exit 2
}

# Every target is a plain file name in the step's working directory. The
# check is not about today's two call sites, which pass literals: `clear`
# removes its argument with rm -rf, and this script exists to be the one
# owner of that operation, so the one place it is performed should not be
# able to take an argument it cannot bound. Measured before it was added:
# `clear ../outside.txt` deleted a file outside the working directory.
require_verdict_name() {
  case ${1:-} in
    "" | */* | .*)
      echo "::error::refusing to operate on '${1:-}':" \
        "expected a plain file name in the working directory" >&2
      exit 2
      ;;
  esac
}

# Is this file a verdict the aggregate can read? The top-level half of
# github_pr_support.is_valid_review. The per-issue half stays with the
# aggregate deliberately: a jq copy would put the severity vocabulary in a
# second place, and shelling out to Python for it would give this step an
# import dependency its job does not install -- the break v1.11.0 shipped
# into the aggregate job (AT-2510). What this has to catch is a payload
# that is not a verdict at all displacing the honest error verdict its
# caller would write instead, and the stamping alone does not: `null` and
# `{"foo": 1}` both come out of it as objects.
IS_VERDICT='
  type == "object"
  and (.summary | type) == "string"
  and (.early_exit | type) == "boolean"
  and (.issues | type) == "array"
'

is_verdict() {
  jq -e "$IS_VERDICT" "$1" > /dev/null 2>&1
}

# Back-fill early_exit and stamp status the way review_status.stamp_model_status
# does: a model-emitted "ok"/"early_exit" is kept -- including "ok" beside an
# early_exit of true, which that function keeps too -- and anything else,
# including a model-emitted "failed", reserved for infrastructure paths and
# never trusted from model output, is re-derived from the early_exit flag.
# Asked of the stamped result rather than the raw candidate, because the
# legacy schema carries neither field and back-filling them is the point.
# Without the stamp the aggregate fails closed on the missing status
# (AT-1954) and discards the very verdict this promotion exists to save.
STAMP='
  .early_exit = (.early_exit // false)
  | .status = (
      if (.status == "ok" or .status == "early_exit") then .status
      elif .early_exit == true then "early_exit"
      else "ok"
      end
    )
'

clear_files() {
  local target=$1 name
  for name in "$target" "${LEGACY_VERDICT_FILES[@]}"; do
    # -L as well as -e, so a dangling symlink is reported and not swept in
    # silence. -rf, not -f: a directory at one of these names makes a
    # plain rm fail, which under set -e aborts this script and under the
    # runner's bash -e takes the whole step down -- "commit a directory"
    # would become "no codex review, ever", a denial the PR under review
    # gets to choose.
    if [ -e "$name" ] || [ -L "$name" ]; then
      echo "::notice::Removed $name, which the PR checked out;" \
        "only a verdict this run writes can stand"
    fi
    rm -rf "$name"
  done
}

# Anything at a verdict path that is not a regular file can hold no
# verdict and cannot be replaced by one: `mv` moves the new file INSIDE a
# directory sitting there -- or inside the one a symlink names -- and
# reports success, so the promotion notice named a promotion that had not
# happened. The caller's own error-verdict redirect would then fail on the
# same path and take the step down. Removed the way clear_files removes
# it, and for the same reason; a symlink to a regular file is left alone,
# because mv replaces the link rather than writing through it.
drop_if_not_regular() {
  local path=$1
  if { [ -e "$path" ] || [ -L "$path" ]; } && [ ! -f "$path" ]; then
    echo "::notice::Removed $path, which is not a regular file"
    rm -rf "$path"
  fi
}

# Stamp a verdict the model wrote to the canonical name itself. The
# direct-write path's half of the same decision, and here rather than in
# the workflow because a second copy of the rule is how the two diverged:
# the inline filter did not back-fill early_exit, so a direct write in the
# legacy schema came out with a status but no boolean, failed
# is_valid_review in the aggregate, and the review was reported as nothing
# (`Codex -- [ ] N/A`). Returns non-zero when the file cannot be read as a
# verdict, which is the caller's signal to write its own error verdict --
# including for a parseable non-object, where the inline filter instead
# errored and killed the step under the runner's bash -e.
stamp() {
  local target=$1 tmp
  drop_if_not_regular "$target"
  [ -f "$target" ] || return 1
  if ! tmp=$(mktemp "${RUNNER_TEMP:-/tmp}/promote-verdict.XXXXXX" 2>/dev/null); then
    echo "::warning::no temp file available; not stamping $target"
    return 1
  fi
  if jq "$STAMP" "$target" > "$tmp" 2>/dev/null && is_verdict "$tmp"; then
    mv "$tmp" "$target"
    return 0
  fi
  rm -f "$tmp"
  return 1
}

# Does the target already hold this run's answer? The answer depends on
# WHO WROTE IT, which the caller knows and the file cannot be trusted to
# say, so the caller declares it and no payload can select its own
# treatment.
#
#   model -- 'Run Codex review' calling during the run. Anything at the
#     target is the CLI's own write from moments ago, so it is stamped:
#     model semantics, where a model-emitted "failed" is re-derived
#     (AT-1799). Stamping is also what decides whether it IS a verdict,
#     since the legacy schema carries no early_exit until the stamp adds
#     one -- asking is_verdict of the raw file instead held the target to
#     a stricter standard than any candidate, and a legacy-named file
#     then overwrote a canonical one written in that schema.
#
#   ours -- the net calling after the run step finished. That step
#     already adjudicated this file: it stamped the CLI's write, or it
#     wrote an infrastructure verdict of its own. Re-deriving THAT status
#     turns our own "failed" into "ok" and re-admits a reviewer that
#     never ran, so the shape is asked without rewriting anything.
#
# The fall-through in `ours` cannot reach an infrastructure verdict: every
# one this workflow writes carries a summary string, early_exit false and
# an issues array, so is_verdict returns above. What reaches it is a
# target that is NOT yet a verdict -- a raw legacy-schema write left by a
# run step killed between the CLI and its own promotion -- and model
# semantics are right for exactly that.
promote() {
  local target=$1 origin=$2 candidate tmp
  if [ "$origin" = ours ] && is_verdict "$target"; then
    return 0
  fi
  if stamp "$target"; then
    return 0
  fi
  for candidate in "${LEGACY_VERDICT_FILES[@]}"; do
    [ -f "$candidate" ] || continue
    # A STEP-OWNED temp file, because "${target}.tmp" was a path in the
    # PR's checkout like every other name here: committed as a symlink it
    # redirected this write to any runner-writable path it named, and
    # committed as a directory it failed the cleanup rm and killed the
    # step before any fallback could write a verdict.
    if ! tmp=$(mktemp "${RUNNER_TEMP:-/tmp}/promote-verdict.XXXXXX" 2>/dev/null); then
      echo "::warning::no temp file available; not promoting $candidate"
      return 0
    fi
    if jq "$STAMP" "$candidate" > "$tmp" 2>/dev/null && is_verdict "$tmp"; then
      # Renames over the target, so a symlink sitting there is replaced
      # rather than written through.
      mv "$tmp" "$target"
      echo "::notice::Promoted $candidate -> $target"
      return 0
    fi
    rm -f "$tmp"
  done
  return 0
}

case "${1:-}" in
  clear)
    [ $# -eq 2 ] || usage
    require_verdict_name "$2"
    clear_files "$2"
    ;;
  promote)
    # `--target-is-ours` is the caller saying the run step already
    # adjudicated the target. See promote() for why that cannot be read
    # off the file instead.
    if [ "${2:-}" = "--target-is-ours" ]; then
      [ $# -eq 3 ] || usage
      require_verdict_name "$3"
      promote "$3" ours
    else
      [ $# -eq 2 ] || usage
      require_verdict_name "$2"
      promote "$2" model
    fi
    ;;
  stamp)
    [ $# -eq 2 ] || usage
    require_verdict_name "$2"
    stamp "$2"
    ;;
  *)
    usage
    ;;
esac
