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

### The reviewers always run one at a time

Actions runs the three reviewers concurrently in `parallel` mode. **This
driver never does** -- in either mode it runs them one after another.

The reason is the working tree. In Actions each reviewer is a separate job
with its own checkout; here all three share one directory, and two of them can
write to it: Codex runs with `--sandbox workspace-write`, and Claude's
`--allowedTools` includes `Write`. Overlapping them lets one reviewer's writes
land underneath another's read. The cost of serialising is wall-clock time --
a run takes about as long as its three reviewers put together -- and that is
the price of not having three checkouts.

**`REVIEW_MODE` still means what it means**, and the two modes differ only in
what runs, never in how many run at once:

| Mode | Runs | On `early_exit` |
|---|---|---|
| `parallel` (default) | all three | nothing -- the round is not shortened |
| `sequential` | Claude, then Codex, then Gemini | stops the chain |

That distinction is load-bearing. Serialising the *execution* must not
serialise the *semantics*: an operator on the default mode is expecting three
reviews, not however many run before one reviewer asks to stop.

Serialising is not a substitute for deleting the agent configuration, which
happens before each of them. A reviewer that writes a `CLAUDE.md` into the
shared tree simply has it deleted again before the next reviewer starts --
there is nothing to weigh it against, because nothing in that tree is being
preserved.

Two further behaviours differ from the Actions path, both deliberate:

- **Inline comments are posted one reviewer at a time**, after all three have
  finished. In Actions the three jobs post concurrently and none of them sees
  the threads the others are opening; serialising costs nothing here and lets
  each reviewer dedup against what the previous one just posted.
- **A reviewer's exit code is reported.** Actions loses it to
  `continue-on-error`, so a reviewer that died reports `success` with no
  artifact and the aggregate calls the outage "early-exit or no-output". Here
  the exit code is in hand, so a reviewer that failed and wrote nothing is
  reported to the aggregate as `failure`.

## Known: the same ordering problem exists in the workflow

`review_codex_local.py` promotes a legacy verdict file name
(`verdict-openai.json` / `verdict-codex.json` to `review-codex.json`) *before*
it checks for a direct write. `base-ai-review-single.yml` does it the other
way round: its `Normalize review file name` step runs after the `Run Codex
review` step, which has already written an error verdict to
`review-codex.json` when it found no parseable JSON -- so the normalize step
sees the file present and does nothing, and a verdict the model really wrote
under the older name is masked by an error verdict.

**This is a live bug in the Actions path, not just a local difference.** It is
recorded here rather than fixed because the workflow is outside this change's
scope; it wants its own ticket.

## What does not carry over

- The reviewer GitHub App token and the auto-approve path. A comment verdict
  is what a local run can honestly produce.
- The Claude OAuth-versus-API-key precedence and the usage-limit auth switch.
  Both exist to choose between two org-held secrets; the CLI uses the
  credential you are logged in with.
- The bubblewrap sysctl writes around the Codex CLI, and its pinned
  `npm install`. The CLI's own `--sandbox workspace-write` is kept.

## Security

### Agent configuration is deleted from the review tree

The reviewers run with the PR head checked out, which on an Actions runner is
harmless because the runner is ephemeral. On your machine it is not. Measured
on `claude` 2.1.269, in a scratch directory:

| Probe | Result |
|---|---|
| `CLAUDE.md` in the working directory | reached the model |
| `.claude/CLAUDE.md` | reached the model |
| `AGENTS.md` | reached the model |
| `claude.md`, `Claude.md`, `AGENTS.MD` on a **case-sensitive** filesystem | each reached the model -- the CLI matches case-insensitively even where the filesystem does not |
| `--safe-mode`, with `CLAUDE.md` present | **still** reached the model, despite its help text listing `CLAUDE.md` among what it disables |
| a `SessionStart` hook in `.claude/settings.json` | **executed the shell command**, with no prompt, in `-p` mode |
| the same paths removed from the directory | not reached; hook did not run |

So a pull request would otherwise get arbitrary command execution on the
machine reviewing it. Before each reviewer starts, the driver **deletes** the
tree's agent configuration:

- at any depth: `CLAUDE.md`, `AGENTS.md`, `.mcp.json`
- at any depth: `.claude/`, `.codex/`, `.cursor/`
- any directory symlink pointing **outside** the tree (the link, never its
  target -- the CLI would follow it to content this scan cannot see)

A match is decided by **name, never by type**, and matched
**case-insensitively**, both per the measurements above. Deleting rather than
preserving is safe because the review tree is a **git worktree created fresh
for every run** (see below); the change is still in `pr.diff`, so it is
reviewed -- just not obeyed.

Deletion runs **before every reviewer**, not once per round: the reviewers
share one tree and two of them can write to it, so a reviewer that writes a
`CLAUDE.md` would otherwise leave it live for the reviewer after it.

**If a deletion fails, the run aborts** rather than reviewing without the
mitigation.

`--safe-mode` is deliberately **not** passed. The measurement above says it is
not a mitigation here, and passing it would read like one.

### The review tree is a fresh worktree every run

`<run-dir>/clone` is a long-lived clone of the target repository;
`<run-dir>/review` is a `git worktree` cut from it, **removed and recreated on
every run**. It sits beside the clone in the cache directory, never inside the
repository under review -- a worktree inside an inspected tree is picked up by
that project's own globs.

This is what lets the deletion above be a deletion. An earlier design *moved*
the configuration aside and put it back, to protect untracked files an operator
might have left in a long-lived tree; that required a manifest, an atomic
write, crash recovery, occupant handling and path containment, and each round
of review found another hole in it. A tree made seconds ago holds nothing of
the operator's, so there is nothing to preserve and none of that machinery is
needed.

Two consequences worth stating:

- **A crash is harmless.** A worktree left behind by a killed run has no value;
  the next run removes it, and `git worktree prune` clears the registration.
  That is precisely why the recovery machinery could be deleted rather than
  fixed.
- **Freshness is a requirement, not an aspiration.** Reuse would reintroduce
  the possibility of operator files in the tree, and with it everything above.
  The teardown is unconditional and runs before the worktree is created.

The tree is **left in place after a run**, so `pr.diff`, `context.md`, the
verdicts and each reviewer's log can be inspected; the next run's unconditional
teardown is what guarantees it is never reused.

### Hooks are disarmed in the clone, every run

The review tree is fresh, but `<run-dir>/clone` is not: it is reused whenever
its `.git` exists, and the worktree cut from it shares its config and its hook
directory. The reviewer CLIs run in that worktree with write access
(`codex exec --sandbox workspace-write` may write anywhere in the workspace)
and the artifact cleanup deliberately leaves `.git` alone -- so what one run
writes there is still there for the next one, where the driver's own
`git fetch` and `git worktree add` would run a planted `post-checkout` as you,
with the clone's credential helper already configured. The driver therefore
sets `core.hooksPath` to `/dev/null` and deletes the clone's `.git/hooks` at
the start of every run, before it fetches or creates a worktree; every run
rather than at clone time, because a run that can plant a hook can also unset
the config that ignores it. Hooks are not the only thing a rewritten
`.git/config` can get git to execute, so after reviewing something you have
reason to distrust, delete the run directory
(`rm -rf ~/.cache/lens/local-review/<owner>-<repo>-pr<n>`) instead of reusing
it.

### The reviewed code cannot supply its own review

Run artifacts live in the work tree, which is also the pull request's tree, so
the two namespaces collide. A PR that commits a `review-claude.json` had it
written into the tree by the checkout, and `aggregate_reviews.py` reads
whichever `review-<name>.json` it finds — the reviewed code handing in its own
verdict. A PR committing `.review-context/unresolved-threads.json` is the same
door: that file is read as prior review context whenever
`collect_review_threads.sh` fails, and it is allowed to fail.

The worktree is therefore created **before** the cleanup, never after, so the
PR's copies are removed before any reviewer or the aggregate can see them. The
changes are still in `pr.diff`, so they are reviewed — just not obeyed, the
same shape as deleting the agent configuration.

Keeping artifacts outside the work tree would close this structurally rather
than by ordering. It is not available without changing the reused scripts:
`review_gemini.py` writes `review-gemini.json` relative to its working
directory, the Claude prompt names `review-claude.json` in the current
directory, and the reviewers need that directory to *be* the checkout so they
can read source. Reusing those unchanged is the point of the driver. Ordering
is sufficient here because nothing runs between creating the worktree and
cleaning it.

### Residual risk

The mitigation closes the configuration surface, not every surface:

- **The tree's source is still read** -- that is what reviewing is. A comment
  or string in a source file can still try to address the model. The
  diff-scope and evidence rules in the reviewer prompts are the defence, and
  they are the same ones the Actions path relies on.
- **The list depends on what the CLIs read**, which is a moving target. A CLI
  version that adds a configuration file not named above would not be covered.
  Re-probe when you bump `claude` or `codex`; the probes are three files and a
  codeword.
- **Codex was never probed** -- it is not installed on the machine this was
  built on. Its entries above are conservative guesses about a reader that
  could not be verified, not measurements.
- **The reviewer CLI still runs as you**, with your credentials and network
  access. The driver does not sandbox it. It does not execute the reviewed
  code, but nothing stops a CLI from doing what a CLI can do.

### Carried over from the Actions path

The prompt-injection defences of the Actions path are kept, with the same
limits they have there: review prompts are read from the target repository's
BASE branch rather than the PR head, so a PR that edits them does not change
what its own reviewers read; the PR metadata (author, branch names, labels)
goes into `context.md` inside a fenced block that declares itself untrusted,
with every value passed through `display_path` so it cannot break out of the
fence; and `.github/lens-ignore` is applied before any reviewer sees the diff.

The base-branch rule has one hole, and it is the same one
`base-ai-review-prepare.yml` has: when the base branch does not carry the
prompt file at all, both fall back to the PR head's copy with a warning
(`Base branch '<base>' has no <path>; using PR head`) so that a repository
can onboard with the PR that adds its prompts. **On that first PR, the
system prompt the reviewers read is written by the PR author.** Read that
warning when it appears: on any later PR it means the prompt file was
deleted from the base branch, not that onboarding is in progress. Passing
`--system-prompt-path` / `--checklist-path` to a file that does exist on the
base branch avoids the fallback entirely.
