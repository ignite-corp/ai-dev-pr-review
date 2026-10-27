# AI Code Review — `ignite-pilot-org` usage pattern

`ignite-pilot-org` repos are reviewed by the runner,
[`ignite-corp/ai-dev-pr-review-runner`](https://github.com/ignite-corp/ai-dev-pr-review-runner#automatic-dispatch).
It calls this repo's orchestrator from `ignite-corp` with `target_repo` set, and the
reviewer App's webhook dispatches it automatically on each pull request of an enrolled repo
(on the runner's allowlist, with the reviewer App installed; AT-3007). A pilot
repo carries no review workflow and no repo-level review secrets.

How to enroll a repo, and how targets, prompts and manual dispatch work, is the runner's
README to say; this page does not restate it. For the general cross-org options, see
[Cross-org usage](../README.md#cross-org-usage) in this repo.

The pilot wrapper `ignite-pilot-org/ai-dev-pr-review-wrapper` is retired and archived
(AT-3036; archive observed 2026-10-27). Every non-archived pilot repo that called it, plus
the two that called this repo's orchestrator directly (`factory-process-maker` and
`spec-interview`), deleted its `ai-review.yml` under that ticket; the ticket's 2026-10-27
status comment records the sweep. Archived repos were not swept; they run no Actions.
This repo keeps no release lockstep with the wrapper. This page used to describe the
wrapper's architecture, installation and drift handling; read this file on `main` before
commit `75ca1ed` (#215) for that text.
