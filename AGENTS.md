# AGENTS.md

## Fleet Management Doctrine (AT-1270)

This repo owns reviewer infrastructure AND consumer fleet management for the tracked consumers.

### Rulesets are code
GitHub Rulesets for the 5 tracked consumers are managed in `rulesets/` of this repo, one JSON per consumer.

- **Never edit rulesets via the GitHub UI.** The UI has a footgun: required status checks added as "Any source" (integration_id=null) silently never match check_runs, leaving PRs permanently BLOCKED. See AT-1270.
- To change a ruleset: edit `rulesets/<repo>.json` via PR. On merge, `ruleset-sync.yml` PUTs to live ruleset via API.
- Drift detection runs nightly via `ruleset-audit.yml`. Live vs JSON divergence fails the workflow.
- New tracked repo: add `rulesets/<name>.json` + entries in the static slug-to-ID maps in both `ruleset-sync.yml` and `ruleset-audit.yml`.

### Daily consumer health
`consumer-health.yml` runs daily (00:00 UTC) checking the 5 tracked consumers:
- recent ai-review.yml run conclusions
- reusable workflow pin freshness
- org/repo-level secret accessibility
- Dependabot PR status

Authentication: GitHub App `ignite-actions-token-app` (app_id=1582952).
- Org secrets `IGNITE_ACTIONS_TOKEN_APP_ID` / `IGNITE_ACTIONS_TOKEN_APP_PRIVATE_KEY` already accessible from this repo.
- App permissions: `administration: write` (rulesets), `actions: read` (workflow runs), `contents: read`, `metadata: read`.
- Workflows mint short-lived tokens via `actions/create-github-app-token@v3`. No PAT to manage / rotate.
