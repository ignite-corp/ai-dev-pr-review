# AGENTS.md

## Fleet Management Doctrine (AT-1270)

This repo owns reviewer infrastructure AND consumer fleet management for the tracked consumers.

### Rulesets are code
GitHub Rulesets for the tracked consumers are stored in the PRIVATE org variable `RULESET_CONFIG` (scoped to this repo), shape `{"<repo>": {"id": <int>, "ruleset": {<body>}}, ...}`. This repo is PUBLIC, so consumer repo names, ruleset IDs, and ruleset bodies must never live in the tree or be printed to Actions logs. `ruleset-sync.yml` / `ruleset-audit.yml` read the variable and report progress by index only.

- **Never edit rulesets via the GitHub UI.** The UI has a footgun: required status checks added as "Any source" (integration_id=null) silently never match check_runs, leaving PRs permanently BLOCKED. See AT-1270.
- To change a ruleset: edit the `RULESET_CONFIG` org variable, then run `ruleset-sync.yml` manually (workflow_dispatch). A variable change fires no event, so the sync is not automatic.
- Drift detection runs nightly via `ruleset-audit.yml`. Live vs stored divergence fails the workflow (details are not logged in the public repo; investigate privately).
- New tracked repo: add a `<repo>: {id, ruleset}` entry to `RULESET_CONFIG`. No workflow code change needed.

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
