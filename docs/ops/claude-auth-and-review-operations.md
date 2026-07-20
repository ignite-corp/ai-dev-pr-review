# Claude Auth & Review Operations Runbook

Durable, reusable operating lessons for the AI review fleet. Keep tight.

## Auth model - the SSOT composite

The Claude review step lives in one shared composite action,
`.github/actions/claude-review`, consumed by both `base-ai-review-single.yml`
and the pilot wrapper (via the local checkout path
`./.ai-dev-pr-review/.github/actions/claude-review`). It is the single source of
truth for:

- **OAuth-preferred auth ternary** - the composite passes
  `anthropic_api_key: ${{ inputs.claude_code_oauth_token == '' && inputs.anthropic_api_key || '' }}`.
  When an OAuth token is present, an empty API key is passed (subscription is
  used); when absent, the API key is the fallback.
- **claude-code-action SHA pin** - a full-length commit SHA (currently
  `a92e7c70...` = v1.0.159), not a floating tag.
- **Model wiring** - `--model ${{ inputs.model }}`.
- **`api_timeout_ms` (default 600000)** - applied to BOTH `API_TIMEOUT_MS` (the
  per-request cap) and `CLAUDE_STREAM_IDLE_TIMEOUT_MS`. The CLI's byte-level
  stream-idle watchdog defaults to **180 s on direct Anthropic API
  connections** and aborts slow billed-API reviews; 600000 matches the caller
  job's 10-minute budget. (AT-1499 auth ternary + composite; AT-1601 timeout.)

## API <-> subscription toggle (per repo)

The org `CLAUDE_CODE_OAUTH_TOKEN` variable has **private** visibility.

- **Put a repo on the billed API**: set an **EMPTY** repo-level
  `CLAUDE_CODE_OAUTH_TOKEN`. Repo-level overrides org -> empty -> the composite
  ternary falls back to `ANTHROPIC_API_KEY`.
- **Restore subscription**: delete the empty repo-level variable so the org
  value applies again.
- **Requirements for the API path to work**: the repo must be in the
  `ANTHROPIC_API_KEY` selected list AND the key must be ENABLED at the
  Anthropic console.
- **CLI precedence trap**: `ANTHROPIC_API_KEY` outranks the OAuth token in the
  CLI. Passing BOTH credentials silently bills the API. That is exactly why the
  composite passes the ternary (one or the other), never both.

## Model selection

- `vars.CLAUDE_MODEL` resolves in the **CALLER** repo and its org
  (reusable-workflow context), NOT the base repo.
- **ignite-corp** sets one org var (visibility all) that covers all its
  consumers. **ignite-pilot-org has no org-level vars** (plan restriction) ->
  each wrapper consumer needs its own repo-level `CLAUDE_MODEL`, else it falls
  back to the composite default (`claude-sonnet-4-6`).
- **Avoid `gemini-2.5-flash-lite` for review** - it confabulates (fabricated 59
  "not English" flags on English text). Use `gemini-2.5-pro`.

## Release & pin discipline

- Releases are **manual**: cut `vX.Y.Z` on the merge commit; `move-major-tag`
  floats `v1` to it. Merged base changes stay inert for consumers until release
  + pin bump, so accumulating on main is safe.
- base <-> wrapper keep **MAJOR.MINOR in lockstep**; patch versions independent.
- **Pin-bump rule** - `single.yml`, `prepare.yml`, `aggregate.yml` each run
  different scripts from their own pinned checkout. Bump only the pin(s) serving
  the changed script:
  - `threads.jq` -> prepare
  - `aggregate_reviews.py` -> aggregate
  - `post_inline_comments.py` / `review_gemini.py` / `review_prompt.md` /
    composite action -> single
- The pinned tag will not exist until the release is cut, so **cut the release
  immediately after the pin-bump merge**.

## Standalone workflows = SSOT, not drift

Consumer standalone Claude workflows must consume the base composite via a
`@v1` checkout (not a local `anthropics/claude-code-action` copy). The one
documented exception is the **`@claude`-mention comment-trigger flow**: the
composite requires a `prompt` input, so that flow stays inline but must mirror
the fleet SHA pin + auth ternary + timeout env. (AT-1602.)

## Required check

- The merge gate is the pipeline's
  `review / aggregate / Aggregate & Verdict` check.
- Register it via the ruleset's **Suggestions** (source: GitHub Actions), NEVER
  a hand-typed "Any source" context - a hand-typed context sits in permanent
  **Waiting** and never satisfies (AT-1270).
- GitHub occasionally adds default fields to the ruleset API (e.g.
  `dismissal_restriction`) which trips byte-exact audits. Re-sync
  `RULESET_CONFIG` when the audit flags such drift.

## Op hazard - merge then delete, never chained

NEVER chain `gh pr merge && gh api ...delete-branch`. A piped merge
(`gh pr merge ... | head && ...`) masks the merge's non-zero exit, so `&&`
proceeds to delete the head branch of an UNMERGED PR, which auto-CLOSES it.

Correct sequence:
1. Run `gh pr merge --merge` ALONE.
2. Confirm the PR is `MERGED` (check exit / `state==MERGED`).
3. Only then delete the branch (usually already auto-deleted; verify with a 404
   rather than blindly deleting).

Recovery if it happens: restore the ref at `headRefOid`
(`gh api --method POST repos/O/R/git/refs -f ref=refs/heads/<b> -f sha=$HEAD`)
then `gh pr reopen N` - reviews/approvals on that SHA are preserved.
