# Codex review authentication

Codex prefers `CODEX_ACCESS_TOKEN`, a ChatGPT workspace credential scoped to
Codex. Keep `OPENAI_API_KEY` configured as the billed fallback. API-only
installations continue to work. Token-only installations can review while quota
is available, but cannot retry with the API when it is exhausted.
If another caller's central API window is active, a token-only caller warns and
tries its own access token without changing that shared window.

This implementation uses the existing Codex model and review contract. It does
not combine subscription quotas, rotate identities, or switch models.

## Configuration and availability

1. Create a Codex access token in your eligible workspace. Store it as the
   `CODEX_ACCESS_TOKEN` Actions secret; keep `OPENAI_API_KEY` separately.
2. Grant access to the repository **running the workflow**. For central remote
   reviews this is the private runner, not each reviewed target. Direct callers
   need access to the secret themselves; secrets in the reusable-workflow
   repository are not automatically available to callers. Within an organization,
   one org secret can grant all repositories access or select the actual callers.
3. Forward both secrets explicitly when calling the orchestrator:

   ```yaml
   secrets:
     CODEX_ACCESS_TOKEN: ${{ secrets.CODEX_ACCESS_TOKEN }}
     OPENAI_API_KEY: ${{ secrets.OPENAI_API_KEY }}
   ```

4. For central persistent switching, forward an auth-only key and configure
   the existing `REVIEWER_APP_ID` variable. The same existing App installation
   must read and write Actions organization variables in `ignite-corp`:

   ```yaml
   secrets:
     AUTH_APP_PRIVATE_KEY: ${{ secrets.REVIEWER_APP_PRIVATE_KEY }}
   ```

   This is a reusable-workflow secret alias, not a new App or stored secret.
   Auth prefers `AUTH_APP_PRIVATE_KEY`, falling back to `REVIEWER_APP_PRIVATE_KEY`
   for existing callers. Both Claude and Codex use this choice. The auth token
   is restricted to the `ai-dev-pr-review` repository; its organization-variable
   grant remains organization-wide. The auth step reads state centrally rather
   than assuming caller-scoped `vars` belong to the base organization.

   For same-repository callers that need auth control without App approval,
   pass only `AUTH_APP_PRIVATE_KEY` and omit `REVIEWER_APP_PRIVATE_KEY`. The auth
   alias never reaches prepare or aggregate and cannot supply an aggregate App
   approval token, regardless of `ALLOW_AUTO_APPROVE`. This does not change the
   caller's approval policy or disable posting with its existing `GITHUB_TOKEN`.
   Cross-repository reporting still requires `REVIEWER_APP_PRIVATE_KEY` for the
   target token; the auth alias is not a replacement for that credential.

Feature YAML and its trusted helper must come from a compatible release. The base
repository's self-review executes new workflow YAML against released scripts.
Until a release includes `codex_review_auth.py`, this combination explicitly keeps
the old API-only path. It never executes authentication code from the PR head to
bootstrap the new feature. A private runner also needs its own explicit token
forwarding and a compatible base pin; this base change alone does not update it.
Release/tag/pin changes are separate deployment work.

The pinned CLI is `@openai/codex@0.166.0` (AT-3174); its actual login help
supports `--with-access-token`, as 0.147.0's did. Official
[Codex access-token documentation][access-tokens] describes workspace
eligibility, permissions and expiry. The first-supported `0.138.0-alpha.6`
version was not independently established by this change.

## Quota fallback and recovery

- Start with the access token when present. With no token, use the API key.
- Each CLI attempt has its own ten-minute timeout (plus five seconds to kill
  an unresponsive process). Codex reserves a 21-minute reviewer step and a
  minimum 26-minute job so a late quota error still leaves a full API attempt.
  The orchestrator forwards the caller job timeout to both Codex modes; a larger
  value is preserved. These budgets do not change Claude
  or Gemini timeouts. An attempt timeout reports `error_cause: attempt_timeout`
  (`attempt_timeout -- per-attempt time limit reached`); the overall step timeout
  retains `error_cause: timeout` (`timeout -- step time limit reached`).
- A failed token review with a recognized **terminal CLI quota error** retries
  the same review once using the API key. Previous partial canonical and legacy
  verdicts are removed first. A failed API retry is still an infrastructure
  failure; it cannot pass using its own partial verdict.
- Only after API fallback auth has actually been selected, persist
  `CODEX_FORCE_API=true` and `CODEX_FORCE_API_UNTIL=<UTC epoch>` centrally
  so subsequent reviews use the API until the window ends. `CODEX_FORCE_API`
  is operator-visible state only, never an authentication selection input. The
  expiry is authoritative: an absent/invalid expiry cannot force API authentication forever,
  and a partial flag write cannot lose a valid bounded window. These are
  operational state, not credentials or per-consumer configuration.
  A quota failure without API fallback material leaves no new persistent window
  and reports `error_cause: quota_exhausted` in the failed verdict.
- Prefer the provider reset time printed by the CLI. Its minute precision is
  rounded up, and the CLI runs in UTC. If no reliable future reset time is
  available, try the access token again after **five hours**. This is a probe
  interval, not a claim that the workspace quota resets every five hours.
- `codex-auth-restore.yml` removes expired state hourly and on manual dispatch.
  Reviewers also ignore expired state immediately, so a delayed cron does not
  prolong the API window. A still-exhausted token can trigger another fallback.

On repeated events, persistence keeps the earlier of the existing unexpired expiry
and the new expiry. GitHub's organization
variable API has no compare-and-swap: simultaneous writers are best effort rather
than an atomic transaction. Both partial writes are recoverable and all windows
remain bounded; a restore re-reads state before deletion and reports a concurrent
change rather than claiming success. The restore workflow serializes its runs.

Without a control-plane App token, immediate retry can still work, but persistence
and automatic cleanup cannot be claimed; warnings identify this degraded state.
Control-plane read/write errors do not turn a failed review into success.

Expired/revoked tokens, permission errors, generic 429s, transport errors, denied
models, invalid output and API-key quota failures do not trigger this paid
switch. The selector never silently changes from token to API just because token
login failed.

## Evidence and credential boundaries

Quota evidence is read from stdout-only `codex exec --json` terminal `turn.failed`
events plus the process's nonzero exit. Model text and tool output live in JSON
string fields of `item.*` events and are not recursively interpreted. Stderr and
ordinary review text are not quota evidence.

Verdict extraction reads the CLI's final response first. If that contains no
verdict, only completed assistant-message text from this attempt's JSONL stream
is decoded, newest valid message first and each message separately. Tool output,
reasoning and raw event JSON are never scanned for verdicts. A failed CLI attempt
cannot recover a verdict this way, and an API retry cannot use its prior token
attempt's messages.

Codex source `rust-v0.147.0` (`be6e8eac`) maps HTTP 429 with provider
`usage_limit_reached` to `UsageLimitReachedError` in
`codex-rs/codex-api/src/api_bridge.rs`. Its canonical workspace and named-limit
messages come from `codex-rs/protocol/src/error.rs`. The JSONL processor in
`codex-rs/exec/src/event_processor_with_jsonl_output.rs` wraps the rendered message
in `turn.failed` but drops the typed code and raw reset fields. The detector
therefore recognizes those specific workspace/named-limit renderings, not a
general substring such as “quota”. Unrecognized future CLI wording fails closed.
`rust-v0.166.0` keeps that mapping and the JSONL wrapping, but renders the
messages with a typographic apostrophe (`You’ve`, U+2019) where 0.147.0 printed
an ASCII one, so the detector accepts both. Each rendering has a fixture captured
from the real CLI offline
(`.github/scripts/tests/fixtures/codex-<version>-quota.jsonl`).

The trusted login step imports selected credentials over stdin and prepares any
API fallback before the model starts. Auth files and attempt metadata are
owner-only under `~/.codex`, denied by the existing review filesystem profile.
Neither credential nor the control-plane token is passed into the model process
environment. Existing permissions, trusted script checkout, stdin prompt,
owner-only logs and verdict provenance guards remain in place. The Codex artifact
includes both the final attempt log and any token-quota attempt log; symlinked
artifact paths are removed before upload.

Provider health checks keep probing the API key independently. A Codex access
token is not sent to a Platform API-key probe. For a token-only installation the
probe explicitly reports that token validity is unverified; repeated actual
review failures remain visible to error-streak monitoring. Intentionally unprobed
Codex tokens are counted separately from transient API-key probe failures. An
unrelated API-key alert can close when all applicable API-key probes recover,
while the closing comment keeps the Codex token's unverified status explicit.
The per-consumer health scan still reports token-only authentication as not
evaluated rather than clean OK.

Unit and workflow simulation tests are not evidence of a live token review or
real quota exhaustion. Deployment verification must record actual token-backed
inference separately. Exercise quota transitions with controlled fixtures rather
than deliberately exhausting the user's workspace quota.

## Rollback

Remove the optional token configuration and clear only `CODEX_FORCE_API` and
`CODEX_FORCE_API_UNTIL` in the central control plane. Verify the API-only path.
Keep `OPENAI_API_KEY` and Claude's independent auth variables. If workflow code or
pins need reverting, use the normal reviewed deployment process.

[access-tokens]: https://learn.chatgpt.com/docs/enterprise/access-tokens
