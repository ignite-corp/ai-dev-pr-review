# Optional Copilot reviewer

The source Actions workflows can select Copilot through the ordered
`reviewers` input, for example `["copilot","claude","codex","gemini"]`.
The default remains `["claude","codex","gemini"]`. Selection alone does not
provision an account: the caller must explicitly forward the optional
`COPILOT_GITHUB_TOKEN` secret. A selected reviewer whose credential or runtime
is unavailable fails and remains in the configured response denominator.
The source local driver accepts the same ordered roster through `REVIEWERS`
in its process environment or existing settings file. Its default remains
the existing three. As of ai-dev-pr-review-runner `65390ee`, the
runner's remote caller passes this input from its trusted `REVIEWERS` Actions
variable; unset selects the existing three
([runner reviewer selection at `65390ee`](https://github.com/ignite-corp/ai-dev-pr-review-runner/blob/65390ee96a9fce2c59c086e9e3afa630fc600052/README.md#reviewer-selection)).

This workflow interface is published from v1.22.0; callers adopt it
explicitly. No production four-provider service round has been verified.
The local integration is published from v1.24.0.

Install `.github/scripts/requirements.txt` (Copilot SDK 1.0.28), and provision a
trusted Copilot CLI 1.0.105 executable outside the PR checkout. The adapter
requires `COPILOT_CLI_PATH` to be its absolute path; it checks the reported
version and never downloads a runtime. Do not use a PR-provided executable.
Use the native executable: the runtime receives `PATH=/usr/bin:/bin`, so
launchers that depend on Node or another interpreter from a toolcache or user
installation are not supported by this invocation contract.

For a local pipeline run, export both `COPILOT_GITHUB_TOKEN` and
`COPILOT_CLI_PATH`, then select Copilot with
`REVIEWERS='["copilot","claude","codex","gemini"]'` before invoking
`review_pr_local.py` as described in [local review](local-review.md).
The driver reads neither Copilot value from the settings file and never
discovers login files or provisions credentials. Both names are reserved from
other reviewers even when named in general passthrough. Before any executable
or version probe starts, the driver rejects relative paths, paths anywhere in
the per-PR run directory (including its clone, review worktree, and artifacts),
and outside symlinks resolving into that directory. The adapter is launched
from trusted scripts with isolated Python imports.

In a directory containing prepared `context.md` and `pr.diff`, run the trusted
adapter with `COPILOT_GITHUB_TOKEN` supplied through the environment:

```sh
export COPILOT_CLI_PATH=/trusted/path/to/copilot
python /trusted/ai-dev-pr-review/.github/scripts/review_copilot.py
```

Optional `EXISTING_COMMENTS` and `THREAD_COUNT` supply the existing thread
excerpts used by the other reviewers. Output is `review-copilot.json` in that
directory, using the existing review fields. Infrastructure failures produce
`status: failed`, without exception text or authentication metadata.
A failure that is none of the adapter's own codes names the stage it happened
in (AT-3500): `review_failed:start` (CLI probe and runtime start),
`review_failed:quota` (authentication and quota calls), `review_failed:session`,
`review_failed:send`, or `review_failed:timeout` for a timeout at any stage or
of the whole review. None of them is exempted. The job log adds the failing
exception's class name in parentheses, only when it is a plain identifier,
otherwise `unknown`; its message is never printed. Before AT-3500 every such
failure was a bare `review_failed`, so a 2026-11-16 flip in which every run
failed within seconds could not say which stage broke. For the SDK's
`JsonRpcError` the log also shows its numeric JSON-RPC code, for example
`review_failed:send (JsonRpcError code=-32603)`. The code is printed only when
it is an `int` in the 32-bit range, otherwise as `code=unknown`. The error's
message and data are never printed. Like the class name, the code stays out
of the verdict file and the step output.
`requested_model` records `auto`; `actual_models` records sanitized model IDs
from SDK usage events, or `unknown` when the service supplies no such event.

The model is fixed to Auto. Each invocation sends at most one review request,
after checking that the explicit credential is authenticated and that its chat
quota either has included requests remaining or allows overage. A chat quota
that reports `is_unlimited_entitlement` as true is accepted without the
included-request checks, since an unlimited plan has no finite entitlement.
The decision reads the flag, not the number: a paid account measured
2026-11-16 with CLI 1.0.105 / SDK 1.0.28 reported entitlement `0` with the
flag set, not the SDK-documented `-1`. Overage is not refused (AT-3525,
owner's decision of 2026-11-17): an account whose chat quota allows overage
proceeds; overage recorded earlier does not by itself let an exhausted quota
proceed. That the service then bills such a request instead of refusing it is
the meaning the SDK gives the flags (`overage_allowed_with_exhausted_quota`:
"Whether additional usage is allowed when quota is exhausted", SDK 1.0.28
`copilot/generated/rpc.py`) and the owner's expectation; no billed request has
been measured yet. Until AT-3525 such an account failed with
`overage_enabled`; that code is retired. A finite chat quota whose included
requests are used up, with `used_requests` at or above `entitlement_requests`
and `remaining_percentage` at `0`, fails with `included_quota_unavailable`
only when it allows no overage (`overage_allowed_with_exhausted_quota` and
`usage_allowed_with_exhausted_quota` both `false`), because with overage the
request is expected to be billed rather than refused. If either flag is
missing or not a boolean on a used-up quota, it fails closed with
`quota_unverifiable`. A missing chat quota, or one that is reported
inconsistently (a non-finite, negative or out-of-range value, an entitlement
of `0` on a finite plan, or the two counters disagreeing about exhaustion),
fails closed with `quota_unverifiable` whatever its overage settings, and a
missing unlimited flag counts as a finite plan.
The adapter publishes its failure code as a step output, and only
`included_quota_unavailable` lists Copilot in the aggregate's
`review_result.quota_unavailable` (AT-3487). `quota_unverifiable` is not
exempted, since a report that cannot be read shows nothing about whether the
quota ran out. This preflight supports the tested request-quota account
contract; it does not establish coverage of credit-based plans. Auto may
choose a billable model; Auto itself is no no-charge guarantee. There is no
paid fallback, provider key or adapter retry. Overage billing is the account
owner's accepted cost; the preflight cannot cap it or prevent a concurrent
account settings change. Model selection and quota availability remain
service-controlled.

The runtime receives isolated directories and a minimal environment, with no
ambient login or inherited provider credentials. Filesystem tools, custom tools
and configuration discovery are disabled; all review data is supplied in memory.
The review has a five-minute limit; control and teardown operations have
five-second limits. The official native Linux x64 archive's checksum and
isolated `--version` execution were verified without credentials. SDK behavior and workflow failure
paths have credential-free tests; account authentication, service quota, and
review quality still need an explicitly authorized end-to-end service check.

Actions installs the native Linux x64 executable outside the PR checkout from
[the official v1.0.105 release](https://github.com/github/copilot-cli/releases/tag/v1.0.105).
Its pinned SHA-256, confirmed against the release API digest and the official
[SHA256SUMS.txt](https://github.com/github/copilot-cli/releases/download/v1.0.105/SHA256SUMS.txt),
is `aba1f06f27bc0447656647f76e0a4cf58b4b2a341c75ff04a750982b6aeb6bd2`.
The workflow verifies that digest before extracting or running the executable;
the adapter independently checks the version in its isolated runtime environment.
Dependency installation uses isolated Python outside the PR directory. Only the
Copilot review step receives the account credential; installation receives none.

The workflow uploads `review-copilot.json` through the common artifact path and
posts findings through the same released inline-comment entrypoint as the other
reviewers. If setup failed or the run was skipped, interrupted, or timed out,
its always-running failure step replaces any PR-supplied verdict file, directory,
or symlink with a fixed failure result. Completed adapter failures retain their
sanitized reason codes. Parallel execution starts every selected reviewer;
sequential execution follows the configured order and stops after an earlier
reviewer's successful early exit. An early exit never lowers the formal
approval response threshold. See [reviewer selection](reviewer-roster.md).
