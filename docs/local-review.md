# Running the review pipeline off Actions

LENS normally runs as three reusable workflows. A private repository on an
account whose Actions billing is blocked never starts a runner, so the
workflows cannot review it at all. `review_pr_local.py` runs the same
scripts, in the same order, with the same environment, on your machine.

Why the design is what it is -- including the defects that shaped it, and
the one end-to-end run that is the only measurement of this pipeline off
Actions -- is recorded once in
[`docs/tasks/local-review-driver-design-record/design-record.md`](tasks/local-review-driver-design-record/design-record.md).
Code comments cite its sections rather than restating them.

## Usage

```
python3 .github/scripts/review_pr_local.py <owner>/<repo> <pr-number>
```

This repository keeps its review prompts under `examples/prompts/`, not at
the orchestrator's default `.github/prompts/`, so reviewing its own PRs
needs both paths:

```
python3 .github/scripts/review_pr_local.py ignite-corp/ai-dev-pr-review 123 \
  --system-prompt-path examples/prompts/code-review-system.md \
  --checklist-path examples/prompts/code-review-checklist.md
```

Those are the two paths `self-review.yml` passes. The defaults are the
orchestrator's, which is right for a consumer repository and wrong for this
one; the driver checks both paths against the remote **before** it clones,
so a wrong path costs two API calls rather than a full clone.

| Option | Meaning |
|---|---|
| `--run-dir` | Where the clone and the run's files live. Default `$LENS_LOCAL_RUN_ROOT`, else `~/.cache/lens/local-review`; `<owner>-<repo>-pr<n>` is appended to either, so the variable names a root and never the run directory itself. |
| `--config` | `KEY=VALUE` settings file. Default `$LENS_LOCAL_CONFIG` or `~/.config/lens/local-review.env`. Relative paths are resolved before they reach the reviewers. |
| `--system-prompt-path` | Review system prompt **in the target repository**. |
| `--checklist-path` | Review checklist in the target repository. |

## Requirements

| Tool | Notes |
|---|---|
| `gh` | Authenticated. Set `GH_HOST` if you authenticated against an enterprise host. |
| `git`, `jq`, `bash` | |
| `python3` | With `.github/scripts/requirements.txt` installed. |
| `claude` | Logged in (`~/.claude`), or its credential named in `LENS_REVIEWER_ENV_PASSTHROUGH`. |
| `codex` | Logged in (`~/.codex`), or `OPENAI_API_KEY` named in `LENS_REVIEWER_ENV_PASSTHROUGH`. |
| `GOOGLE_AI_API_KEY` | Read by `review_gemini.py`, and forwarded to that reviewer only. |

**The driver sets no credential of its own, and does not hand your whole
environment to the reviewers either.** A reviewer is an LLM CLI reading the
head of a pull request anyone can open; given your environment it also holds
your GitHub token, your cloud keys and every other token you happen to have
exported, none of which a review needs. So each reviewer gets an allowlist:

| Forwarded | Why |
|---|---|
| `PATH` | The shims spawn `claude` / `codex` by name. |
| `HOME` | `claude login` and `codex login` write their credentials under it, as does the default config file. |
| `SHELL` | The `claude` CLI's allowed Bash tools (`cat pr.diff`) need one. |
| `TMPDIR` | If you set it, the default was not usable. |
| `LANG`, `LC_ALL`, `LC_CTYPE` | A diff is decoded by the locale's codec. |
| `HTTP(S)_PROXY`, `NO_PROXY` (both cases) | On a proxied machine, the only route to the model API. |
| `SSL_CERT_FILE`, `SSL_CERT_DIR`, `REQUESTS_CA_BUNDLE`, `NODE_EXTRA_CA_CERTS` | A TLS-inspecting proxy is reached only with its CA bundle. All four name files, not secrets. |
| `LENS_LOCAL_CONFIG` | The shims call `LocalConfig.load()` with no argument. |
| `API_TIMEOUT_MS`, `CLAUDE_STREAM_IDLE_TIMEOUT_MS` | Read by the claude shim; **claude only**. |
| `GOOGLE_AI_API_KEY`, `GEMINI_MAX_OUTPUT_TOKENS` | Read by name in `review_gemini.py`; **gemini only**. |
| Whatever you name in `LENS_REVIEWER_ENV_PASSTHROUGH` | Your decision -- see below. |

**Which credential a reviewer authenticates with is still yours to choose.**
No credential for the `claude` or `codex` CLI is named above, because a
hardcoded list of credential names would decide which choices are supported:
a gateway token, a Bedrock profile or a relocated config directory would go
missing with nothing saying why. Log the CLI in (`HOME` is forwarded), or
name the variable:

```sh
export LENS_REVIEWER_ENV_PASSTHROUGH=ANTHROPIC_API_KEY,CLAUDE_CODE_OAUTH_TOKEN
```

It is a comma-separated list of variable **names**, settable in the config
file too, and it applies to every reviewer.

**Every run prints what it withheld**, by name, per reviewer:

```
  claude: 61 of 74 environment variables withheld (AWS_SECRET_ACCESS_KEY, GH_TOKEN, ...)
```

Names only -- never values. If a reviewer suddenly cannot authenticate or
cannot reach the network, that line is where to look first: find the
variable in it and add it to `LENS_REVIEWER_ENV_PASSTHROUGH`.

## Settings

Every knob is `${{ vars.NAME || 'default' }}` in the workflows. Off Actions
the same names are read from, in order: the process environment, the config
file, then the default **parsed back out of the workflow YAML**. Nothing
restates a default in Python -- a model pin copied into a second place is a
pin that goes stale silently.

`LENS_REVIEWER_ENV_PASSTHROUGH` is the one setting the workflows do not
declare -- there is no Actions run for it to describe -- so it is read from
the environment and the config file only. See Requirements for what it does.

`ALLOW_AUTO_APPROVE` is pinned to `false` whatever you set. There is no App
token to mint here and you cannot approve your own PR, so the verdict is
posted as a comment.

### The models may differ from CI

The driver resolves models from the **workflow default**, and your
organisation's `vars` may override that default in CI. In the one preserved
run, two of three reviewers silently differed:

| Reviewer | Workflow default (what you get locally) | Org variable (what CI used) |
|---|---|---|
| claude | `claude-sonnet-4-6` | `claude-opus-5` |
| codex | `gpt-5.6-luna` | `gpt-5.6-luna` (same) |
| gemini | `gemini-2.5-pro` | `gemini-3.5-flash-lite` |

The run now prints what it is using and where the value came from:

```
Reviewers: claude=claude-sonnet-4-6 (workflow default)  codex=gpt-5.6-luna (workflow default)  gemini=gemini-2.5-pro (workflow default)
```

The driver deliberately does **not** read `vars` to remove the difference.
That needs repo-admin or `admin:org`, which you will not have when reviewing
someone else's PR; this driver exists for repositories Actions cannot reach,
where `vars` describe a run that never happens; and resolving from them
would assert "local matches CI", a larger claim than "local follows the
workflow default". Export `CLAUDE_MODEL` / `CODEX_MODEL` / `GEMINI_MODEL` if
you want to match a particular CI run.

## Line numbers

The reviewer prompt tells every model to report the target file's own line
number. In the one real run, two of three did not: two findings carried
`pr.diff` offsets and a third pointed at an unrelated line that the range
check passed, because the PR added whole files and every line was therefore
"in the diff".

So the driver screens coordinates before anything is posted:

- A line that is not a valid right-side line for its file, but **is** a
  `pr.diff` offset belonging to that file, is converted and the conversion
  is printed.
- Every located finding is checked against the code its description quotes.
  One that cites a line carrying none of it keeps its description and loses
  its line -- it reaches the verdict, not an inline comment in the wrong
  place -- and the description says why:

  > `[driver: the reviewer cited <path>:<line>, which does not carry the code this finding quotes, so it is reported here rather than inline]`

A finding whose description quotes no code at all is counted as unquotable
rather than called checked.

## Security

The reviewer CLIs run **as you, with your credentials**, on a checkout of
the pull request's head.

What the driver does about that:

- **Agent configuration is deleted** from the review tree before each
  reviewer: `CLAUDE.md`, `AGENTS.md`, `.mcp.json`, `.claude/`, `.codex/`,
  `.cursor/`, matched case-insensitively, nested as well as at the root.
  This is measured, not precautionary: on `claude` 2.1.269 a `SessionStart`
  hook in a committed `.claude/settings.json` executed a shell command with
  no prompt, and `--safe-mode` did **not** stop a `CLAUDE.md` reaching the
  model despite its help text saying it does.
- **A reviewer's environment is an allowlist**, not your environment: see
  Requirements for the entries and the reason for each. What is withheld is
  printed by name on every run.
- **The review tree is a fresh `git worktree` every run**, thrown away and
  recreated. Nothing of yours is in it, so nothing has to be restored.
- **Hooks are disarmed on every run**, and the cached clone is re-cloned
  unless its `.git/config` is byte-for-byte what the driver last wrote.
- **The clone's identity is checked on host *and* `owner/name`.**
- **Run artifacts are removed after the checkout**, so a PR that commits a
  `review-claude.json` cannot supply its own verdict.

### What is NOT guarded, and why

Per the record's rule that an unmeasured claim is marked as one:

| Not covered | Why |
|---|---|
| `.codex/` and `.cursor/` are **unprobed** | Included conservatively. The CLIs that read them were not probed for what they load. Re-probe before trusting the entry. |
| `CLAUDE.local.md` is **not stripped** | Deliberately absent: adding a name without a probe is itself a claim. Conventionally gitignored, but a PR can commit one. |
| Whether the strip actually blunts a real attack | The negative control -- the same hostile files with the protection off -- produced an ordinary review in its one trial. The established claim is narrower: the files are read, they affect output, and a hook in them executes. |
| A descendant that calls `setsid` for itself | The reviewer timeout kills a process **group**. Anything that leaves the group is out of reach. |
| Which variables a CLI **needs** | The allowlist is argued from this repository's own code and from what a process needs to start and reach the network. The CLIs were not instrumented for what they read, so an entry may be missing. That is why the withheld list is printed and `LENS_REVIEWER_ENV_PASSTHROUGH` exists. |
| Running a reviewer shim **directly** | The allowlist is the driver's, so a shim started by hand -- or by a wrapper, a Makefile, CI -- hands its CLI the caller's whole environment. Not refused, because that is a debugging path: the shim warns on startup unless the driver's marker is present, and the driver assigns that marker **after** its filter, so an exported copy never survives into a reviewer. An operator can still silence their own direct run by setting it -- the warning is about the environment they are handing over, not a lock on it. |
| What the child can still reach through `HOME` | The allowlist bounds the environment, not the filesystem. The CLIs run as you, so `~/.aws`, `~/.ssh` and the rest are still readable -- the same standing limit as everything below. |
| Source comments and strings in the tree | Still read by the reviewers. A comment can address the model. |
| Everything a reviewer CLI could read | Unknowable. The strip list is the measured list, applied -- not a claim of completeness. |
| `codex exec -` on other CLI versions | Stdin delivery was measured on codex-cli 0.154.0. The driver does not detect a version that behaves differently. |
| An enterprise `GH_HOST` | The host check is argued from `gh help environment`, never measured against a real enterprise host. If it rejects your own clone, export `GH_HOST`. |
| The composite `action.yml` being unreadable | No dedicated exit code. It ships in this repository and CI asserts it parses, so at runtime this is an edited working tree; it becomes a `reviewer_crashed` verdict with a traceback naming the file. |

### Two known limits this PR does not fix

Both come from identifying a verdict by **who wrote it** rather than by its
marker, and both live in code shared with every consumer's Actions runs
(`post_inline_comments.py`, `aggregate_reviews.py`), so changing them is a
change to the Actions path, not to this driver.

1. **The round counter does not count local runs.** `fetch_round_count`
   filters on `.user.type == "Bot"`, and a local verdict is authored by you.
   In a repository Actions cannot reach -- the reason this driver exists --
   the counter stays at zero and the convergence cutoff never fires.
2. **`BOT_LOGIN` cannot fold both histories.** On a PR with both Actions and
   local rounds, prior Actions verdicts were written by
   `github-actions[bot]` and local ones by you; one login folds one set or
   the other. Measured: a local run on such a PR minimized none of the 11
   already-minimized comments and left both of its own expanded.

Keying on `REVIEW_MARKER` and ignoring the author would close both at once.

### One asymmetry worth knowing

The driver's size-skip comment carries
`<!-- lens:skipped reason=size-limit ... -->`, because without a marker the
stale-item pass cannot fold it and every re-run leaves another copy standing
(AT-2208). `aggregate_reviews.py`'s **own** size-skip verdict does not carry
that prefix. A consumer gate keyed on "`lens:skipped` present means skipped"
therefore sees it in one place and not the other. The driver keeps its
marker; aligning the aggregate would change behaviour for every consumer and
is not done here.

## Differences from the Actions path, on purpose

| Difference | Why |
|---|---|
| Reviewers run one at a time | Actions gives each of its three jobs its own checkout. Here they share one tree and two of them can write to it. The price is wall-clock time. |
| `BOT_LOGIN` defaults to your `gh` login | The verdict's author here is you, so a previous local verdict is folded rather than stacked (AT-2208). |
| Inline comments are posted after all reviewers finish | Each reviewer's comments can then dedup against the previous one's. In Actions the three race and none sees the others. |
| A reviewer's exit code reaches the aggregate | Actions loses it to `continue-on-error`, so a credential failure is reported as success (AT-1837). |
| Legacy verdict names are promoted **before** the fallbacks | The workflow promotes after, so an error verdict masks the real one (AT-2424). |
| The Codex prompt goes over stdin | The workflow passes it as one argv element, which meets `MAX_ARG_STRLEN`. The workflow side is AT-2411. |
| No App token, no auto-approve | You cannot approve your own PR. |
