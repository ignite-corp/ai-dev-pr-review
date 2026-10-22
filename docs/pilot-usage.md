# AI Code Review — `ignite-pilot-org` usage pattern

The pilot wrapper `ignite-pilot-org/ai-dev-pr-review-wrapper` is retired (AT-3036),
and `ignite-pilot-org` repos are no longer reviewed through it. The runner,
[`ignite-corp/ai-dev-pr-review-runner`](https://github.com/ignite-corp/ai-dev-pr-review-runner#automatic-dispatch),
reviews them instead. It calls this repo's orchestrator from `ignite-corp` with
`target_repo` set, and the reviewer App's webhook dispatches it automatically (AT-3007).

A pilot repo needs no review workflow and no repo-level secrets. To be reviewed, it needs
two things: an entry in the runner's allowlist (`ignite-pilot-org/*` covers the whole org)
and the reviewer App installed. For targets, prompts and manual dispatch, see the runner's
README. For the general cross-org options, see [Cross-org usage](../README.md#cross-org-usage)
in this repo.

The wrapper repository will be archived, not deleted, so its history stays readable. This
page used to describe the wrapper's architecture, installation and drift handling. Read
this file at a commit before AT-3036 for that text.
