# Reviewer selection

The aggregate script accepts `REVIEWERS` from the trusted operator environment
as a JSON array of distinct reviewer IDs. For example:

```sh
export REVIEWERS='["claude","codex","gemini","copilot"]'
python /trusted/ai-dev-pr-review/.github/scripts/aggregate_reviews.py
```

Supported IDs are `claude`, `codex`, `gemini` and `copilot`. Array order is
preserved as the configured priority. An absent setting defaults to
`["claude","codex","gemini"]`; an explicit empty value, empty list, duplicate,
unsupported ID or other invalid JSON shape fails before any PR lookup or post.
Diagnostics do not print the supplied setting. Review artifacts cannot select
the roster or override the identity assigned by their trusted filename.

The selected list determines the artifact filenames, job-conclusion inputs,
coverage and missing-reviewer reasons. Missing or failed reviewers remain in
the denominator; artifacts from unselected or unknown reviewers do not count.
The response rules remain `ceil(N/2)` for a sufficient review and
`ceil(N/2)+1` for automatic approval, with at least three selected reviewers
required for automatic approval. Findings remain separate from response counts.

The source Actions orchestrator exposes the same selection as its `reviewers`
workflow input, defaulting to `["claude","codex","gemini"]`. Prepare validates
it with the released parser and emits four ordered slots. Parallel mode starts
the selected slots together; sequential mode follows that exact array order,
continuing after failures and stopping on a successful prior early exit.
Cancellation starts no further reviewers. The aggregate receives the trusted
selection and each selected provider's own job conclusion; unselected artifacts
cannot supply responses. Invalid input fails the required aggregate check and
cannot enable approval. There is no availability-based roster reduction.

```yaml
with:
  reviewers: '["copilot","claude","codex","gemini"]'
secrets:
  COPILOT_GITHUB_TOKEN: ${{ secrets.COPILOT_GITHUB_TOKEN }}
  # Forward the existing credentials for the other selected reviewers too.
```

The input adds no approval-count, minimum/maximum-roster, model-selection, or
new execution-mode setting. The existing `REVIEW_MODE` continues to select
parallel or sequential execution. Copilot is optional and requires its explicit
account credential; a roster without Gemini does not require a Google key.

The source local driver reads `REVIEWERS` from the environment, then its existing
settings file, and defaults through the shared parser to the existing three.
It freezes an immutable selection before GitHub access and passes it through
dispatch, coordinates, inline posting and aggregation. Both modes use one
process group at a time to protect the shared tree: `parallel` runs every
selection, while `sequential` stops on a successful early exit. Failed selected
reviewers remain expected; unselected files cannot supply responses. Local
automatic approval remains disabled. This local integration is published from
v1.24.0; see [local review](local-review.md).
As of ai-dev-pr-review-runner `65390ee`, the runner's remote caller
passes this optional input from its trusted `REVIEWERS` Actions variable, and
unset selects the existing three
([runner reviewer selection at `65390ee`](https://github.com/ignite-corp/ai-dev-pr-review-runner/blob/65390ee96a9fce2c59c086e9e3afa630fc600052/README.md#reviewer-selection)).
No runner roster change or consumer rollout is included.
The full-roster PR response gate
continues to require every expected reviewer, independently of approval quorum.
Its unresolved-thread and folded-finding checks remain independent of the
selected roster. Historical disposition labels recognize all supported
reviewers, including a reviewer omitted from the current selection.

This foundation adds no adaptive roster changes or new aggregate state for
carrying prior open findings forward. Existing issue/disposition rules and
withholding for unresolved majors remain in force. A roster change is not a disposition of prior findings. This change
does not add historical finding carryover or replace consumer unresolved-thread
and folded-finding gates.

The workflow interface is published from v1.22.0. The prepare helpers remain
pinned to v1.21.0; single and aggregate use v1.25.0.
No new helper file is assumed. Callers adopt
the input explicitly. No production four-provider service round, account provisioning,
secret or variable change, or automatic activation is claimed by these tests.
