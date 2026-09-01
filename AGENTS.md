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
This repo is PUBLIC. Ruleset IDs, ruleset bodies, and the tracked-consumer list must never live in the tree or be printed to Actions logs -- that part holds today. Consumer repo names are a different matter: `MIGRATION.md`, `AUDIT.md`, `docs/pilot-usage.md`, `examples/consumer-thin-trigger.yml`, and both READMEs name consumers, and have since `80d73cb` (2026-06-02), so the names are already permanent in public history. They stay. These are per-consumer onboarding documents, a tree scrub would leave history untouched, and rewriting history would break the SHA pins consumers hold. The required check context (`AI Code Review`) is public by design too, since consumers must match it. The protection that actually holds is the mapping, not the names: this tree does not reveal which repos are ruleset-tracked, nor any ruleset ID or rule body. Do not restore a broader claim than the tree keeps -- a doctrine that promises more coverage than it has is exactly what let AT-1944 run on a false assurance.

### Daily consumer health
`consumer-health.yml` runs daily (00:00 UTC) checking the tracked consumers:
- recent ai-review.yml run conclusions
- reusable workflow pin freshness: a SHA pin is resolved back to its release
  tag (`.github/scripts/pin_freshness.py`), and every pin form must name the
  latest release or the one MINOR before it. A commit that matches no release
  is flagged too. The base repo's own entry is skipped here -- self-review.yml
  calls the orchestrator by local path, so it has no pin.
- org/repo-level secret accessibility
- Dependabot PR status

Authentication: GitHub App `ignite-actions-token-app` (app_id=1582952).
- Org secrets `IGNITE_ACTIONS_TOKEN_APP_ID` / `IGNITE_ACTIONS_TOKEN_APP_PRIVATE_KEY` already accessible from this repo.
- App permissions: `administration: write` (rulesets), `actions: read` (workflow runs), `contents: read`, `metadata: read`.
- Workflows mint short-lived tokens via `actions/create-github-app-token@v3`. No PAT to manage / rotate.
