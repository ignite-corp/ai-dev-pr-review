# Aggregate reviewer selection foundation

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

This is aggregate support only. Current Actions workflows, the local driver
and the runner still dispatch three reviewers and do not expose or propagate
this selection. The local driver explicitly pins the aggregate roster to its
dispatched default three, ignoring ambient `REVIEWERS` until selected dispatch
is wired. Leave `REVIEWERS` unset on the Actions and runner paths until they
dispatch the same selected list. The full-roster PR response gate
continues to require every expected reviewer, independently of approval quorum.
Its unresolved-thread and folded-finding checks remain independent of the
selected roster. Historical disposition labels recognize all supported
reviewers, including a reviewer omitted from the current selection.

This foundation adds no adaptive roster changes or new aggregate state for
carrying prior open findings forward. Workflow integration must carry valid
outstanding findings across any roster change and continue withholding approval
until blocking findings are dispositioned. A roster change is not a disposition
of those findings. The broader integration remains pending.

Compatible scripts must ship through the manual release process before new
workflow jobs or consumers can depend on them: the reusable workflows execute
scripts from their pinned release, not the current source tree. Copilot workflow
execution, credential forwarding and caller adoption belong to later changes.
