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

The local driver explicitly pins the aggregate roster to its dispatched default
three, ignoring ambient `REVIEWERS`. The external runner's existing caller has
not adopted this optional input. No runner or consumer rollout is included.
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

The published v1.21.0 helpers already support this source workflow wiring;
the reusable workflows keep that fixed helper release pin. New helper files
from the source PR are not assumed to exist in that released checkout. The new
workflow interface itself still requires a subsequent release and explicit
caller adoption. No production four-provider service round, account provisioning,
secret or variable change, or automatic activation is claimed by these tests.
