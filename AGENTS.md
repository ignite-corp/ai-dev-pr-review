# AGENTS.md

## Fleet Management Doctrine (AT-1270)

This repo owns reviewer infrastructure AND consumer fleet management for the tracked consumers.

### Rulesets are code
GitHub Rulesets for the tracked consumers are stored in the PRIVATE org secret `RULESET_CONFIG` (scoped to this repo), shape `{"<repo>": {"id": <int>, "ruleset": {<body>}}, ...}`. A secret (not a variable) is used so Actions masks the value wherever a workflow would otherwise echo it. `ruleset-sync.yml` / `ruleset-audit.yml` read the secret and report progress by index only.

- **Never edit rulesets via the GitHub UI.** The UI has a footgun: required status checks added as "Any source" (integration_id=null) silently never match check_runs, leaving PRs permanently BLOCKED. See AT-1270.
- To change a ruleset: edit the `RULESET_CONFIG` org secret, then run `ruleset-sync.yml` manually (workflow_dispatch). A secret change fires no event, so the sync is not automatic.
- **The secret is write-only.** Unlike the old org variable it cannot be read back (`gh api ... --jq .value` returns metadata, not the value), so every edit is a full-value rewrite starting from the operator-held canonical copy of the JSON.
- Write it back from stdin: `gh secret set RULESET_CONFIG --org <org> --visibility selected --repos <this repo> < config.json`. Re-pass `--visibility` / `--repos` every time or the scoping resets, and pass the value explicitly - `gh secret set` does not read a same-named env var.
- No canonical copy at hand? One consumer's stored body can be rebuilt from its live ruleset with `gh api repos/<org>/<repo>/rulesets/<id> | jq -S '{name,target,enforcement,conditions,rules,bypass_actors}'` - the exact projection `ruleset-audit.yml` compares. The consumer list itself is NOT recoverable this way, so keep a copy of it outside the secret.
- Drift detection runs nightly via `ruleset-audit.yml`. Live vs stored divergence fails the workflow (only generic schema key paths are logged in the public repo; investigate privately).
- New tracked repo: add a `<repo>: {id, ruleset}` entry to `RULESET_CONFIG` (full-value rewrite, as above). No workflow code change needed.

### What the public tree exposes (AT-1956)
This repo is PUBLIC. Ruleset IDs, ruleset bodies, and the tracked-consumer list must never live in the tree or be printed to Actions logs -- that part holds today. Consumer repo names are a different matter: `MIGRATION.md`, `AUDIT.md`, `examples/consumer-thin-trigger.yml`, and both READMEs name consumers, and have since `80d73cb` (2026-06-02), so the names are already permanent in public history. They stay. These are per-consumer onboarding documents, a tree scrub would leave history untouched, and rewriting history would break the SHA pins consumers hold. The required check context (`AI Code Review`) is public by design too, since consumers must match it. The protection that actually holds is the mapping, not the names: this tree does not reveal which repos are ruleset-tracked, nor any ruleset ID or rule body. Do not restore a broader claim than the tree keeps -- a doctrine that promises more coverage than it has is exactly what let AT-1944 run on a false assurance.

### Daily consumer health
`consumer-health.yml` runs daily (00:00 UTC) checking the tracked consumers:
- recent ai-review.yml run conclusions
- reusable workflow pin freshness: a SHA pin is resolved back to its release
  tag (`.github/scripts/pin_freshness.py`), and a version or SHA pin must name
  the latest release or the one MINOR before it. A floating `v1` is resolved to
  the commit it points at and held to the latest release exactly: nobody bumps
  it by hand, so any lag means the tag move did not run (AT-2088; rationale in
  the module docstring). A commit that matches no release is flagged too. The
  base repo's own entry is skipped here -- self-review.yml calls the
  orchestrator by local path, so it has no pin.
- org/repo-level secret accessibility, Actions and Dependabot (Dependabot is
  reported, not flagged)
- Dependabot PR status
- reviewer prompt availability and base guidance (AT-3159): each consumer's
  `.github/prompts/` pair (this repo: `examples/prompts/`) and the runner's
  `default-prompts/` remain repository-specific and are read together.
  Consumers owe no copied SHA Pins or congruence rules: both live in the
  actual base reviewer instructions and travel through the consumer's review
  pin or the runner's review pin, checked for freshness. A source merge alone
  does not publish or deploy them; MINOR publication and compatible pin
  adoption precede stale-copy removal (MIGRATION.md). Freshness tolerance alone does not prove
  adoption of the particular release.
  Declared prompt paths must be plain relative paths; expressions, `..`, and
  queries are not requested and have their own notice. Absent prompts flag;
  failed fetches are not evaluated. The positive control fetches this commit's
  actual Codex base instructions through the same fetch/matcher path and
  requires the full shared rules in `.github/scripts/prompt_guidance.py`.
  Missing rules fail the run; an unreadable control skips only prompt checks.
  Tests hold all three rendered reviewers and the executed Claude shell/local
  mirror to the exact canonical blocks and reject omitted rules. Results go
  to the summary by entry index; runner flags raise their own error. Runner
  targets outside ignite-corp remain outside the fleet App's reach.

The `health` job of `consumer-health.yml` authenticates as the fleet-admin App
below, not as the review-pipeline App.

### Authentication: two Apps, not one (AT-2537)
Two GitHub Apps are live in this repo and they are not interchangeable. Choose
by what the job has to do, not by whichever credential name you saw first: a
job that administers the fleet and a job that speaks on a pull request need
different Apps, and swapping one for the other fails for reasons that look
like something else.

**Fleet administration -- `ignite-actions-token-app` (app_id=1582952).**
- Used by `ruleset-sync.yml`, `ruleset-audit.yml`, and the `health` job of
  `consumer-health.yml`.
- Credentials: org secrets `IGNITE_ACTIONS_TOKEN_APP_ID` /
  `IGNITE_ACTIONS_TOKEN_APP_PRIVATE_KEY` (visibility=selected, with this repo
  on the selected list).
- Installed on **ignite-corp only**: measured 2026-10-16,
  `gh api orgs/ignite-pilot-org/installations` does not list it, so no token
  minted from it reaches a pilot consumer, with any permission. That is an
  observation and negatives go stale silently -- re-read that call before
  building on the absence.
- Permissions this doctrine relies on, mapped against GitHub's
  permissions-required-for-github-apps reference rather than guessed from the
  endpoint name: `administration: write` (PUT a ruleset -- `ruleset-sync.yml`
  only), `metadata: read` (GET a ruleset in `ruleset-audit.yml`, and this
  repo's tag list for pin freshness -- both are Metadata endpoints, not
  Administration or Contents), `contents: read` (consumer workflow files, read
  by both `ruleset-audit.yml` and the health check, and the consumer and
  runner review prompts the health check matches), `actions: read` (consumer
  run conclusions), `secrets: read` / `organization_secrets: read` /
  `dependabot_secrets: read` / `organization_dependabot_secrets: read`
  (secret accessibility, repository- and org-level for each scope; the
  org-level Dependabot grant was added 2026-10-27 under AT-2888 -- re-read
  `gh api orgs/ignite-corp/installations` before building on it -- and a
  health run that gets HTTP 403 there names the permission in a notice
  rather than reading the secret as absent),
  `pull_requests: read` (Dependabot PR status, via a REST call; the
  reviewer-error streak uses GraphQL, which the reference does not map, so this
  entry rests on the REST call alone). The live installation
  grant is broader than this list; if a decision turns on whether some
  permission is present, read `gh api orgs/ignite-corp/installations` instead
  of this file.

**Review pipeline -- `ignite-ai-review-approver` (app_id=4196135).**
- Used by `base-ai-review-aggregate.yml` for PR reviews, and by the Claude
  and Codex auth-control steps in `base-ai-review-single.yml` plus the auth
  restore workflows for ignite-corp auth org variables.
- Org var `REVIEWER_APP_ID` (visibility=all) identifies the App; org secret
  `REVIEWER_APP_PRIVATE_KEY` (visibility=selected, including this repo) supplies
  its key. The orchestrator accepts an
  optional `AUTH_APP_PRIVATE_KEY` alias for auth control, preferring it over
  the reviewer key and forwarding it only to Claude/Codex jobs in both modes.
  Prepare and aggregate receive only the reviewer key, preserving target
  reporting and approval behavior. A same-repository caller can map its
  existing key to the auth alias alone without enabling App approval.
- Auth-control installation tokens are scoped to the `ai-dev-pr-review`
  repository, while the organization-variable permission remains org-wide.
  Raw keys are action inputs, not reviewer model environment variables.
- Installed on **both ignite-corp and ignite-pilot-org**. This bullet gives the
  permissions the cross-org question turns on, not the full grant: it holds
  `pull_requests: write` on each, so it is the only one of the two that can act
  cross-org, and `organization_actions_variables: write`, which is what
  `switch_claude_auth.py` uses to set and delete the ignite-corp
  `CLAUDE_FORCE_API` and `CLAUDE_FORCE_API_UNTIL` org vars. Measured
  2026-10-16, neither installation grants any `actions` permission --
  `organization_actions_variables: write` is a different key. Treat that as
  an observation, not a
  design guarantee: a negative goes stale silently, so re-read
  `gh api orgs/ignite-corp/installations` and
  `gh api orgs/ignite-pilot-org/installations` before building on the absence.

Both mint short-lived tokens via `actions/create-github-app-token` (pinned per
workflow). No PAT to manage / rotate.
