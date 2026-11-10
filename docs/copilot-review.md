# Optional Copilot reviewer

The source Actions workflows can select Copilot through the ordered
`reviewers` input, for example `["copilot","claude","codex","gemini"]`.
The default remains `["claude","codex","gemini"]`. Selection alone does not
provision an account: the caller must explicitly forward the optional
`COPILOT_GITHUB_TOKEN` secret. A selected reviewer whose credential or runtime
is unavailable fails and remains in the configured response denominator.
The local driver and external runner continue dispatching their existing three.

This workflow interface is published from v1.22.0; callers adopt it
explicitly. No production four-provider service round has been verified.

Install `.github/scripts/requirements.txt` (Copilot SDK 1.0.28), and provision a
trusted Copilot CLI 1.0.105 executable outside the PR checkout. The adapter
requires `COPILOT_CLI_PATH` to be its absolute path; it checks the reported
version and never downloads a runtime. Do not use a PR-provided executable.
Use the native executable: the runtime receives `PATH=/usr/bin:/bin`, so
launchers that depend on Node or another interpreter from a toolcache or user
installation are not supported by this invocation contract.

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
`requested_model` records `auto`; `actual_models` records sanitized model IDs
from SDK usage events, or `unknown` when the service supplies no such event.

The model is fixed to Auto. Each invocation sends at most one review request,
after checking that the explicit credential is authenticated, its chat quota
has included requests remaining, and every returned quota bucket has no
recorded overage or permission to continue after exhaustion. Missing or
unsuitable quota information fails closed. This preflight supports the tested
request-quota account contract; it does not establish coverage of credit-based
plans. Auto may choose a billable model; Auto itself is no no-charge guarantee.
There is no paid fallback, provider key or adapter retry. The account owner must keep
overages disabled; the preflight cannot override server billing or prevent a
concurrent account settings change. Model selection and quota availability
remain service-controlled.

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
