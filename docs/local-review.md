# Local review driver

`.github/scripts/review_pr_local.py` runs the LENS pipeline on your machine
against a pull request in **any** repository, without GitHub Actions.

It exists because the Actions path cannot reach every repository. A private
repository on an account whose Actions billing is blocked never starts a
runner, so the reusable workflows cannot review it at all -- the workflow sits
`disabled_manually` and the review never happens. The driver runs the same
scripts, in the same order, with the same environment, from a shell.

## Usage

```bash
python3 .github/scripts/review_pr_local.py <owner/repo> <pr-number>
```

The target repository is an argument, not the current directory -- the driver
clones it into its own run directory. Reviewing a PR in the repository you
happen to be sitting in is the same command as any other.

For a `review-pr` command, link it onto your `PATH`:

```bash
ln -s "$PWD/.github/scripts/review_pr_local.py" ~/.local/bin/review-pr
review-pr hyuk-hur/dgt-research 42
```

Options:

| Flag | Default | Purpose |
|---|---|---|
| `--run-dir` | `$LENS_LOCAL_RUN_ROOT` or `~/.cache/lens/local-review/<owner>-<repo>-pr<n>` | Where the clone and the run's files live |
| `--config` | `$LENS_LOCAL_CONFIG` or `~/.config/lens/local-review.env` | Settings file |
| `--system-prompt-path` | `.github/prompts/code-review-system.md` | Review system prompt, read from the target repo's BASE branch |
| `--checklist-path` | `.github/prompts/code-review-checklist.md` | Review checklist, same |

The exit status is `aggregate_reviews.py`'s: non-zero when fewer than two
reviewers produced a verdict, as in CI.

## What it needs

| Tool | Used for | Notes |
|---|---|---|
| `gh`, authenticated | every GitHub call, and the clone's credential helper | `gh auth login` once; no PAT to manage |
| `git` | the scratch clone, the diff | |
| `jq` | `collect_review_threads.sh` | |
| `python3` + `.github/scripts/requirements.txt` | the reused scripts | `google-genai` is what the Gemini reviewer imports |
| `claude` | the Claude reviewer | whatever credential `claude` is already logged in with |
| `codex` | the Codex reviewer | `codex login` beforehand; the driver never logs you in, so it cannot clobber your `~/.codex/auth.json` |
| `GOOGLE_AI_API_KEY` | the Gemini reviewer | the only API key the driver reads from the environment |

A reviewer whose CLI is missing does not abort the run: it emits a `failed`
verdict, the aggregate names it, and the other two still report.

## Configuration

Everything the workflows read as `${{ vars.NAME || 'default' }}` is read here
under the same name, in this order:

1. the process environment,
2. the config file (`KEY=VALUE`, `#` comments),
3. the default, **parsed out of `.github/workflows/base-ai-review-*.yml`** at
   run time.

Nothing is restated in the driver's own code -- a model pin copied into a
second place goes stale silently, which is how `claude-opus-4-8` kept being
named by nine repositories after it was retired. Change the workflow's
fallback and the driver follows it.

```
# ~/.config/lens/local-review.env
REVIEW_MODE=sequential
CLAUDE_MODEL=claude-sonnet-4-6
PR_SIZE_LIMIT=5000
```

Two settings behave differently from Actions, deliberately:

- **`ALLOW_AUTO_APPROVE` is pinned off** and cannot be turned on. Nobody
  approves their own PR, and there is no reviewer GitHub App to mint a token
  for, so the verdict is always posted as a comment.
- **`BOT_LOGIN` defaults to your own `gh` login**, not `github-actions[bot]`.
  It names the author whose prior verdict comments the next run folds; here
  that author is you. Set it explicitly to override.

## What runs, in order

| Stage | Reused unchanged | Local shim |
|---|---|---|
| Prepare | `extract_pr_diff.sh`, `filter_pr_diff.py`, `fetch_review_context.py`, `verify_action_shas.py`, `collect_review_threads.sh` | clone/fetch, size gate, ref resolution, `context.md` assembly |
| Review | `review_gemini.py`, `extract_claude_review.py`, `extract_codex_json.py`, `review_status.py`, `review_prompt.md` | `review_claude_local.py`, `review_codex_local.py`, `reviewer_prompts.py` |
| Post | `post_inline_comments.py` | -- |
| Aggregate | `aggregate_reviews.py` | -- |

Between-job artifact upload and download becomes plain files in the run
directory. Every file a run writes is removed at the start of the next one, so
a stale verdict is never read as the current run's output.

`REVIEW_MODE` works as it does in CI: `parallel` (the default) runs the three
reviewers concurrently, `sequential` runs Claude, then Codex, then Gemini, and
stops when one returns `early_exit`.

Two behaviours differ from the Actions path, both deliberate:

- **Inline comments are posted one reviewer at a time**, after all three have
  finished. In Actions the three jobs post concurrently and none of them sees
  the threads the others are opening; serialising costs nothing here and lets
  each reviewer dedup against what the previous one just posted.
- **A reviewer's exit code is reported.** Actions loses it to
  `continue-on-error`, so a reviewer that died reports `success` with no
  artifact and the aggregate calls the outage "early-exit or no-output". Here
  the exit code is in hand, so a reviewer that failed and wrote nothing is
  reported to the aggregate as `failure`.

## What does not carry over

- The reviewer GitHub App token and the auto-approve path. A comment verdict
  is what a local run can honestly produce.
- The Claude OAuth-versus-API-key precedence and the usage-limit auth switch.
  Both exist to choose between two org-held secrets; the CLI uses the
  credential you are logged in with.
- The bubblewrap sysctl writes around the Codex CLI, and its pinned
  `npm install`. The CLI's own `--sandbox workspace-write` is kept.

## Security

The driver checks out the PR head and runs the `claude` CLI inside that tree.
Anything in the reviewed repository that a CLI honours -- `CLAUDE.md`,
`.claude/settings.json`, hooks -- is therefore in scope, and `-p` skips the
workspace trust prompt. On an Actions runner that tree is ephemeral; on your
machine it is not. **Review repositories you trust**, or run the driver in a
container.

The prompt-injection defences of the Actions path are all kept: review
prompts are read from the target repository's BASE branch rather than the PR
head, so a PR cannot rewrite the instructions its own reviewers read; the PR
metadata (author, branch names, labels) goes into `context.md` inside a fenced
block that declares itself untrusted, with every value passed through
`display_path` so it cannot break out of the fence; and `.github/lens-ignore`
is applied before any reviewer sees the diff.
