# The boundary between LENS's three execution paths

AT-2564, under AT-2466. Every code claim below was read at commit
`4309344` (`Merge pull request #184 from ignite-corp/task/AT-2555`) on
2026-10-01, and re-checked at `b3555a9` (`Merge pull request #186 from
ignite-corp/task/AT-2539`) for the two **workflow** files that merge
touched — `base-ai-review-orchestrator.yml` and
`base-ai-review-single.yml`. Those two changes are comment-only: no step,
job or key this document cites moved, so every structural claim holds at
both commits. **Where this document cites the prose of a comment in those
two files, the citation is to `b3555a9`**, because that merge wrote it.
That happens in section 4's divergence table and in section 4.1, and each
of those citations carries the commit. That merge also
changed three test files under `.github/scripts/tests/`, which were not
re-read here — section 6 records that.

LENS runs three ways: the Actions pipeline, the local driver, and a
cross-repo path that is designed but not built. Each has documentation or
has none. **The boundary between them had none**, and the cost of that is
already recorded in a ticket: v1.11.0 shipped
`aggregate_reviews.py -> local_reviewer_support -> local_review_config ->
yaml` and broke every consumer's aggregate job at import, because no
document had ever said which direction that dependency may point
(AT-2510).

**Scope.** This document is about the boundary and the contracts the three
paths share. It is not a manual for the local driver and not a restatement
of why the local driver is shaped as it is. Those are
[`docs/local-review.md`](../../local-review.md) and
[`docs/tasks/local-review-driver-design-record/design-record.md`](../local-review-driver-design-record/design-record.md),
and the second of those already declares the division of labour — code
comments cite its sections rather than restating them. This document cites
both for the same reason. Where a reader wants usage, go there; where a
reader wants local design rationale, go there.

Every claim here carries one of the labels below, at the claim —
**measured**, **inferred** and **attributed** alike. Section 6 holds only
the entries that have no claim site in sections 1–5 — what was not read,
what nobody has measured, and attributed reports with no sentence of
their own — under the same names:

| Label | Means |
|---|---|
| **measured** | read or run at the commit named for it |
| **inferred** | reasoned from something measured, not itself observed |
| **attributed** | read from a ticket, or reported by another session, and not re-verified here |
| **unmeasured** | no observation of it exists anywhere, here or elsewhere |
| **not read for this document** | an observation exists, or could be made cheaply, but was not made here |

`unmeasured` and `not read for this document` are different claims and
the document keeps them apart: an unmeasured item needs someone to go and
measure it, a not-read item needs someone to go and look.

**What this document establishes, and what it does not.** The shared
roster, the shared aggregate and the shared verdict contract are evidence
that **the structure is reusable across the three paths** — the same code
produces the same artifact shape wherever it runs. That is not evidence
that all three paths are operationally verified. Three absences decide
that: the serialized local reviewer loop at `4309344` has never been run
end to end; no `actions/checkout` against a foreign repository has been
attempted here; and no byte is known to have been written to a foreign
pull request. **Section 6 carries the label and the narrower measured
claim for each; this paragraph does not restate either.**

Read every "both paths" claim below as a statement about shared code and
its output shape, never as a statement that both have been exercised.

---

## 1. The written reason for the local driver is dead

`docs/local-review.md` opens by justifying the driver this way:

> A private repository on an account whose Actions billing is blocked never
> starts a runner, so the workflows cannot review it at all.

AT-2520's motivation for the cross-repo path is the same condition reached
from the other side.

**That condition has lifted.** On 2026-10-01 `hyuk-hur/dev-dotfiles` ran
six full multi-LLM reviews on Actions: runners assigned, all three
reviewers executing, aggregate succeeding. (**attributed** — measured by
the author of this ticket, not re-run here; `hyuk-hur/dev-dotfiles` is
private, so the run records are not openable by every reader of this
repository.)

Neither feature is thereby pointless. Until this record no document
carried the premise's status at all; `docs/local-review.md` now annotates
its opening and points here rather than carrying a second copy of the
argument. The replacement reasons, stated as what is true now:

**The local driver exists because:**

- It is **stricter than Actions** wherever section 4's table says
  `stricter`; that table is the list, and this bullet does not keep a
  second copy of it. The tickets attached to those rows are AT-2411,
  open, and AT-1837, **Done** in Jira although the behaviour it is cited
  for is still in the workflows. AT-2424 is not one of them: the ordering
  difference it was filed for survives, but the masking does not, so
  section 4 files it as neutral rather than stricter.
- It runs **before a push**, against a head that no remote has yet seen.
- It runs **offline from Actions**: no runner minutes, no queue, no
  dependence on the consumer having wired the workflows at all.
- It reaches a repository the pipeline is not installed in, at the cost of
  being advisory (section 2.3).

**The cross-repo path exists for reach**: one installation in ignite-corp
reviewing pull requests in repositories that will never host the
workflows. Billing was one reason a repository could not host them; it was
never the only one, and it is no longer the live one.

---

## 2. The three paths and what they share

### 2.1 Shared assets

| Asset | Where it is defined | Who uses it |
|---|---|---|
| `REVIEWER_NAMES` | `github_pr_support.py` | the Python: the driver's own tables and the aggregate's `REVIEWERS` map |
| `aggregate_reviews.py` | — | run verbatim by both the Actions aggregate job and the local driver |
| `REVIEW_MARKER` | `github_pr_support.py` | emitted by the aggregate, counted by `post_inline_comments.py` |
| `github_pr_support.py` itself | — | the neutral, stdlib-only module both paths import |

`REVIEWER_NAMES` is `("claude", "codex", "gemini")`. **It governs the
Python and nothing else.** `review_pr_local.py` imports it and **raises at
module import** — a `raise`, not an `assert`, because `python -O` drops
asserts — if its own `SEQUENTIAL_ORDER` or `REVIEWER_SCRIPTS` disagrees
with it, and again if its artifact-name tuples disagree by set equality.
`base-ai-review-orchestrator.yml` declares its reviewer jobs separately
and by hand — a parallel and a sequential job per reviewer — and consumes
the constant nowhere. **The roster is therefore defined in both places
and kept in step by convention**: adding a name to `REVIEWER_NAMES` adds
no Actions job, and no guard derives or compares the job set against the
constant.
(measured)

**The roster is fixed; the range executed is not.** No configuration
selects a subset of reviewers, and `REVIEW_MODE` chooses ordering and
gating rather than membership. But `run_reviewers` leaves its loop early
when `check_shared_inputs` finds that `pr.diff` or
`context.md` changed under the round, in which case the current reviewer
and every reviewer after it is not run; and, in sequential mode only, when
a reviewer that did not fail requests an early exit. "The roster cannot be
narrowed" and "all three always run" are different claims, and only the
first is true. (measured)

The reviewer layer itself does not couple to the repository under review:
the reviewers read the filesystem rather than GitHub, and their
credentials are org-level secrets with no coupling to the target. That is
why the three-reviewer shape is not where cross-repo difficulty lies.
(measured)

**The orchestrator's lack of `github.repository` references is not
evidence of neutrality.** Its concurrency key is one place the absence is
itself the defect — section 5.5. Read a zero count as "nothing to edit
here", never as "already repo-agnostic".

### 2.2 The verdict contract — a conflation to stop repeating

**The pipeline's contract**, emitted by `aggregate_reviews.py` and
therefore **byte-identical on the local and Actions paths**, because the
local driver runs the same script:

```
<!-- multi-llm-review -->                  <- REVIEW_MARKER
## [bot] Multi-LLM Review Summary
**Result: <icon> <label>** -- <reason>
```

Four builders in that file emit this shape: prepare-failure, size-skip,
policy-skip, and the normal verdict. The policy-skip variant inserts
`POLICY_SKIP_MARKER` as its second line, as the documented hook for a
consumer gate that must not merge on a skipped review. **The head SHA
travels in the `HEAD_SHA` environment variable and never in the comment
body.** (measured)

**The hand-review convention** is different and belongs to neither path:

```
## LENS 판정 — **<word>** · head <sha>
```

A human session posts this to a peer repository's pull request when the
pipeline produced no verdict there. **It appears nowhere in this
repository** — grepping the whole tree at `4309344` for `LENS 판정`
returns zero hits. It is not produced, parsed, or mentioned by any script
here. (measured)

The distinction is load-bearing: anything keyed on the pipeline's
contract — round counting, stale-comment folding, a consumer merge gate —
sees local and Actions verdicts as the same shape, and sees a hand-written
verdict not at all. A change to the hand-review wording therefore reaches
no code here, and a change to `REVIEW_MARKER` or the summary heading
reaches both pipeline paths at once.

### 2.3 The merge gate, and why a local verdict is advisory

The merge gate is the aggregate **job name** registered as a required
status context: `review / aggregate / Aggregate & Verdict`, per
`docs/ops/claude-auth-and-review-operations.md`, "Required check".
(measured)

A local run starts no job, so it produces no result for that context.
**Two conditions decide whether that blocks a merge**, and the ruleset
itself was not read for this document: the context has to be required on
the branch in question, and there must be no successful result already
recorded for the same head — a context satisfied by an earlier Actions run
on that head stays satisfied, and a local run neither adds to it nor
disturbs it. Where both conditions hold, the context has nothing to report
and the merge waits. This is mechanical, not a policy choice, and it is
the cleanest statement of what a local verdict is: advisory. Three further
mechanisms hold the same line, and none of them is the one above:

1. `review_pr_local.py` writes `ALLOW_AUTO_APPROVE: "false"` into the
   aggregate's environment **after** splatting `os.environ`, so an
   exported or config-file value is overwritten. The aggregate's
   comment-only branch then routes both `approve` and `request_changes`
   to a comment. (measured)
2. That pin is enforced deny-by-default in
   `.github/scripts/tests/test_review_pr_local.py`:
   one test sets `ALLOW_AUTO_APPROVE=true` in the environment and asserts
   the built environment still reads `"false"`; a second walks the
   module's AST and requires every env-building function mentioning the
   name to carry the pin, so a newly added builder that forgets it fails
   the suite. (measured)
3. Nothing in `.github/scripts/` writes a check run or a commit status:
   grepping for `check-runs`, `/statuses/` and `checks:write` across the
   scripts and the workflows returns zero hits. Turning that context green
   from a local run would require new code and a token scope that do not
   exist here, so nothing can do it by accident. (measured)

---

## 3. The boundary, its guards, and what they do not cover

### 3.1 The direction, as it is now

```
aggregate_reviews.py ─────────► github_pr_support.py        (stdlib only)
                                      ▲
local_reviewer_support.py ────────────┘
local_reviewer_support.py ────► local_review_config.py ────► yaml
review_pr_local.py, review_claude_local.py, review_codex_local.py ─► both
```

Measured by reading the import block of each file at `4309344`:
`aggregate_reviews.py` imports the standard library plus exactly one
non-stdlib block, `from github_pr_support import ...`; `github_pr_support.py`
imports the standard library only; `local_reviewer_support.py` re-exports
`is_valid_review`, `normalize_severity` and `usable_verdict` from
`github_pr_support` so the shims and the aggregate are provably the same
function objects, and holds the chain's only import of
`local_review_config`; `local_review_config.py` holds the only `import
yaml` reachable from any of them.

**The rule the boundary encodes:** code on the Actions path that runs
**without a `pip install`** — the aggregate, and every script in
`STDLIB_ONLY_SCRIPTS` — may not reach local-driver code, because those
jobs go straight from `setup-python` to `python <script>`. Anything they
import must be importable with the standard library alone. The rule does
not reach the gemini job, which installs
`.github/scripts/requirements.txt` first; that is what
`INSTALLED_DEPS_SCRIPTS` exists to mark.

### 3.2 The guard, and what it establishes

`.github/scripts/tests/test_actions_path_imports.py`, added for AT-2510,
holds these tests:

- **`test_actions_path_script_imports_without_third_party_packages`** runs
  each Actions-path script in a **subprocess** with a `sys.meta_path`
  finder that raises `ModuleNotFoundError` for anything rooted at
  `THIRD_PARTY_ROOTS`, after purging already-loaded matching modules. An
  in-process import test would prove nothing, because the test runner has
  the packages installed. **What it establishes is exactly this: each
  listed module can be imported with `yaml` and `google` blocked.** It
  does not establish that the execution path is stdlib-only — see
  section 3.4.
- **`test_every_script_the_base_workflows_run_is_classified`** extracts
  the script names the `base-ai-review-*.yml` workflows invoke and fails
  **in both directions**: a referenced script in neither
  `STDLIB_ONLY_SCRIPTS` nor `INSTALLED_DEPS_SCRIPTS` fails as
  unclassified, and a classified name no longer referenced fails as
  stale.

(both measured; both pass at `4309344`)

### 3.3 First residual — the dependency axis does not deny by default

`THIRD_PARTY_ROOTS` is a literal `frozenset({"yaml", "google"})`.
`.github/scripts/requirements.txt` at `4309344` holds exactly
`google-genai` and `pyyaml`, so the set is complete **with respect to the
two declared packages**, today, and for that reason only. It is not
complete with respect to what the gemini job installs: a declared package
brings its transitive dependencies in as importable top-level roots too,
and the frozenset names none of them.

**Add a third dependency and the guard stays green while no longer
covering it.** The script-name axis denies by default — a new script in a
base workflow cannot slip through unclassified. The dependency axis does
not: a new package is simply not blocked, and the import that reaches it
passes.

Deriving the set from `.github/scripts/requirements.txt` would track the
declared packages automatically, but it would not make the axis deny by
default either, for the same transitive reason: the set that matches what
the job can import is the **resolved** environment after install, not the
requirements file. Tracked as a remaining item on AT-2510; this document
changes no code and proposes none.

(The literal and `.github/scripts/requirements.txt` are **measured**; that
a third dependency would escape the guard is **inferred**, since observing
it would mean adding one; what the install actually makes importable is
**unmeasured**. Section 6 carries the unmeasured one.)

### 3.4 Second residual — the guard covers import, not execution

The test's subprocess body ends at `import <module>`. Nothing in the
script is then called. The meta-path finder would catch a lazy import
inside a function body — that is what a finder buys over a stub — but a
function nobody invokes never runs, so its import is never attempted and
never caught.

**The guarantee is "each listed module imports cleanly with `yaml` and
`google` blocked", not "the Actions path never reaches a third-party
package at runtime".** A `import yaml` inside a rarely-taken branch of
`aggregate_reviews.py` would pass this test and fail on a consumer's
runner, which is the exact failure class AT-2510 records.

Closing it means exercising the real entry points — invoking each script
as the workflow invokes it — in an environment where the packages are
genuinely absent, rather than importing it. (measured for the test body;
**inferred** for the uncaught case, which would require writing such an
import to observe.) This is a second and separate limit from 3.3; neither
subsumes the other.

### 3.5 Boundaries the code does not currently separate

Recorded as-is. Each is a distinction the system does not draw today, not
a proposal; the target shape and the order of any transition belong to the
cross-repo epic, not here.

**1. The dominant coupling is the assumption that the executing
repository is the repository under review.** It is stronger than anything
in the review model itself, and it merges concepts that the code nowhere
separates:

- *execution location* — which repository's Actions, or which operator's
  machine, is running;
- *review target* — repository, pull request number, and the reviewed head
  SHA;
- *the tool's own code and version* — today the `ref:` pin on the scripts
  checkout, which is already independent and is the one concept that is
  separated;
- *permission* — **reading the target**, **publishing results to it**,
  and **changing operational settings on it** are different authorities,
  and one `GH_TOKEN` carries them together.

Environment variables can carry each of these, and several already do.
What is missing is that **which repository and which action a credential
is for is nowhere visible as a contract** — a token arrives ambient and
the receiving code cannot state what it is entitled to do. (**Measured**:
the paths that write to a pull request shell out to `gh`, which reads an
ambient `GH_TOKEN`; the one of them that takes a token — the approve path
— takes `REVIEWER_TOKEN` as an environment value rather than as a
declared scope. `switch_claude_auth.py` sits outside that quantifier
entirely: it writes org variables, not pull requests, through its own
REST client. The decomposition above is **attributed** to AT-2564's
ticket body, not derived from the code.)

**2. Failure state and review verdict are not separated.** The aggregate
reaches the `approve` verdict when every payload is absent and every
reviewer job reported success — it infers "review complete" from "process
succeeded" because, as its own comment states, it is given nothing that
separates the two. **What follows from that label is split, and only half
of it is withheld.** Measured at `4309344`: `_has_full_reviewer_coverage`
is false with no payloads, so `post_verdict` withholds the formal
APPROVED review and posts a comment carrying "Auto-approve withheld".
The other half is not withheld — `no_artifact_bypass` suppresses the
sub-quorum `sys.exit(1)`, so the job exits 0, and the code comment beside
the withholding says exactly this: *"The verdict (and thus the merge-gate
exit code) is unchanged; only the posted event is downgraded to a
comment."* Since the merge gate is that job's status context
(section 2.3), **absence of every result still satisfies the merge gate,
and must not.** The formal approval is the part the design already
refuses; the green check is the part that still follows from nothing.
That is AT-2569, and section 7 carries it — this is the one place the
record says current behaviour is wrong rather than merely unseparated, so
it owes the reader a ticket rather than a verdict.
A common result format would have to distinguish states that today
collapse into payload-present or payload-absent: a completed review and
its verdict; an explicit policy skip; an execution failure or
cancellation; and missing or malformed output. Related and
recorded with it: **building the error envelope is spread across each
reviewer's implementation** rather than owned by one execution wrapper —
`guarded_main` for the local claude and codex shims, a hand-written
handler in `review_gemini.py`, and on Actions per-reviewer workflow steps
that differ in kind from each other (section 4.1). (measured)

**3. Publishing responsibility is distributed, and staleness has fewer
states in code than in principle.** Each reviewer job posts its own inline
comments and the aggregate posts the verdict, so no single component owns
what reaches the pull request. Locally those posts are serialized and a
later reviewer's comments dedup against an earlier one's; on Actions the
three race and none sees the others. Separately, `_head_is_stale` returns
a boolean. **Current, superseded and unknown are distinct states; the
boolean collapses unknown into current**, so a head that could not be
read is indistinguishable from one confirmed unchanged. A retry that
wanted to be idempotent would need a key naming target repository, pull
request, head SHA and reviewer; nothing in either path carries one.
(measured)

---

## 4. Deliberate divergence

These are differences the design chose. `docs/local-review.md`,
"Differences from the Actions path, on purpose", is their home and carries
the reason for each; the boundary-relevant fact is which direction each
difference points.

**Where local is stricter than Actions:**

| Difference | Direction |
|---|---|
| Reviewers run one at a time | stricter: three reviewers share one tree locally and two of them can write to it, where Actions gives each job its own checkout |
| A reviewer's exit code reaches the aggregate | stricter: each Actions reviewer's run step is `continue-on-error`, so a credential death there is reported as success. The code comments at `b3555a9` cite AT-1837 for this; that ticket is Done in Jira while the behaviour remains, so the citation is provenance and not an open-defect reference. What each reviewer has *behind* that step differs — section 4.1 |
| Inline comments posted after all reviewers finish | stricter: each reviewer's comments dedup against the previous one's, where in Actions the three race and none sees the others |
| The codex prompt goes over stdin | stricter: the workflow passes it as one argv element and so meets `MAX_ARG_STRLEN` (AT-2411, open) |

**Serialization is not isolation.** `run_reviewers` runs one reviewer's
process group at a time precisely because the three share one worktree and
two of them can write to it. Ordering reviewers that can modify a shared
tree reduces collisions; it does not isolate them — a reviewer still sees
whatever the previous one left, which is why `strip_agent_config` runs per
reviewer rather than per round, why `clear_reviewer_slot` and
`disown_unauthored_verdicts` exist, and why `check_shared_inputs` can stop
the round mid-loop. A per-reviewer checkout or worktree is the direction
in which that constraint relaxes; it is recorded here as the shape of the
limit, not as work. (measured)

Three further differences are not strictness in either direction.
`BOT_LOGIN` defaults to the operator's `gh` login locally, which is what
makes AT-2427 bite (section 7). No App token is minted locally, which is
section 2.3. And **legacy verdict names are promoted before the
fallbacks** locally, where the workflow promotes elsewhere in its
sequence: at both `4309344` and `b3555a9` the codex **Normalize review
file name** step promotes only on the run step's marker and with
`--target-is-ours`, so a promotion cannot re-derive an error verdict into
a passing one. That gating is why the ordering no longer decides anything
and the difference is neutral rather than stricter; AT-2424 (section 7)
is the history.

**One place local is weaker, and it is structural:** the local path is
advisory. See section 2.3.

**One asymmetry a consumer gate can trip on:** the driver's own size-skip
comment carries a `lens:skipped` marker; `aggregate_reviews.py`'s size-skip
verdict does not. A gate keyed on "`lens:skipped` present means skipped"
sees it in one place and not the other. Known, deliberate, unfixed —
aligning the aggregate would change behaviour for every consumer.
(measured; recorded in `docs/local-review.md`, "One asymmetry worth
knowing")

### 4.1 The three reviewers do not fail alike on Actions

This belongs in a boundary document because it is the clearest case where
"a reviewer does X" is false and only a per-step statement is true.
The steps below are measured at both `4309344` and `b3555a9`. The note
above **Run Claude review** in `base-ai-review-single.yml` is the code's
own home for this breakdown and names AT-2539 — that note was **added by
`b3555a9`** and is cited at that commit, not at `4309344`, where neither
workflow mentions AT-2539 at all:

| Reviewer | Net on failure |
|---|---|
| claude | the step **Emit Claude error verdict (no verdict file)** runs under `always()`, is deliberately **not** `continue-on-error`, and writes a `status: "failed"` verdict file when none exists |
| codex | the step **Verify Codex verdict file** runs under `always()` and `exit 1`s when `review-codex.json` is absent or empty |
| gemini | neither. Its run step is `continue-on-error` with no `always()` net, so a step death leaves a green job and no artifact |

AT-2539's scope is gemini's step, not "the reviewer step". At `b3555a9`
the orchestrator's note above `review-codex-s` covers what
`continue-on-error` costs the chaining guard and points at the
single-reviewer note for the per-reviewer mechanisms, so neither file
restates the other; that division is also `b3555a9`'s work.
The aggregate's green-with-nothing bypass additionally requires
`_artifacts_entirely_absent` — *every* payload missing — together with
every reviewer job reporting `success`, so gemini alone failing does not
reach it. (measured)

Locally the corresponding guarantee is uniform rather than per-reviewer:
`guarded_main` in `local_reviewer_support.py` catches `Exception`, writes
the failed-verdict envelope and does not re-raise, for both shims;
`review_gemini.py` writes the same shape from its own handler around the
model call. A verdict file therefore exists on every local exit path those
cover, which is why the AT-2539 signature is effectively unreachable
locally. (measured for the code; **inferred** for "unreachable", which
would need a run to observe.)

---

## 5. Cross-repo: what exists, and what does not

### 5.1 What exists

`.github/workflows/spike-cross-repo-probe.yml` — `workflow_dispatch` only,
gated on a three-entry `ALLOWED_TARGETS` array **inside the file**, which
its header justifies: an input or a repository variable would be
changeable by whoever dispatches the run, and this repository is public
while the App can read private repositories in two organisations.

The spike mints an App token scoped to the target owner and repository,
reads the installation, reads a pull request, reads a diff, and compares
against the default token as a control. Its negative-claim discipline is
deliberate: only HTTP 404 counts as a measured negative; `000`, 5xx, 401,
403 and 429 each print "not established" rather than a verdict. (measured)

**The cross-owner mint the design needs is already parameterised — in the
spike, not in the pipeline.** (measured)

### 5.2 What does not exist

**No byte has ever been written to a foreign pull request.** The spike's
Probe 4 is a branch over the installation's *declared* permission set and
labels itself so in its own output — "inferred from the installation scope
and permission set, never tested by posting" — and its step header says
"No POST is made anywhere here." `pull_requests=write` in a declared
permission set states what GitHub would allow. It is not a write.

**What is measured here is the probe's own behaviour**: this workflow
makes no POST, and says so in its step header and in Probe 4's output.
"No byte has ever been written to a foreign pull request" is the broader
claim, and it is **unmeasured** — establishing it would need an audit of
every writer, not a reading of one spike. Section 6 files it under
unmeasured; this paragraph is the measured half.

**The conversion surface is about an order of magnitude larger than
AT-2520 records, and of a different kind.** The count, from
`grep -ohE 'github\.repository|github\.token'` over the four
`base-ai-review-*.yml` workflows: **23** occurrences — 15 in
`-prepare.yml`, 4 in `-single.yml`, 4 in `-aggregate.yml`, 0 in
`-orchestrator.yml`; 13 `github.repository` and 10 `github.token`. To
those add a `token:` input on each of the three target checkouts, where
none exists today. AT-2520 describes two sites.

**A string is not an edit site.** The count bounds the search; it does not
enumerate the changes. At least one occurrence must stay as it is:
`RUN_URL` in the aggregate's verdict step builds a link to the run that
executed, and that link should keep naming the **executing** repository
even when the review target is elsewhere. Any conversion has to decide
each occurrence against which of section 3.5's concepts it names, and
some will resolve to "leave alone".

**The durable finding is the shape**: distinct kinds of change, not one
repeated. Numbered so later text can name them:

1. `repository:` on the three target checkouts — the only kind the ticket
   describes.
2. `token:` **added** to those same three checkouts. Absent entirely
   today, and therefore invisible to a survey that looks for hardcodes:
   a survey cannot see a line that is not there.
3. `GITHUB_REPOSITORY` step environments plus two inline `--repo`
   arguments.
4. `GH_TOKEN` step environments swapped from `github.token` to an App
   token, plus the `github_token:` input to the Claude composite, plus an
   `owner:` on the aggregate's App-token mint, plus lifting the
   `verdict == "approve"` gate in `aggregate_reviews.py`.

Kinds 2 and 4 decide whether anything reaches the target at all.
(measured)

**Most writes take no token at all; they inherit one.** Every
pull-request write listed below shells out to `gh`, which reads an
ambient `GH_TOKEN` from the process environment. Each write, and what it
uses — the list is the claim, and no arithmetic over it is needed to size
the conversion:

| Write | Token today | What conversion costs |
|---|---|---|
| `gh pr review --approve` | `REVIEWER_TOKEN` when one is present | a value, already plumbed |
| `gh pr review --request-changes` | the ambient token | **a gate, not a parameter.** It is the same `subprocess.run` call, which receives `env=review_env` either way; only the swap into `review_env` is gated on `verdict == "approve"` |
| verdict comment | the ambient token | plumbing: no token handling in the function |
| GraphQL minimize | the ambient token | plumbing |
| GraphQL dismiss | the ambient token | plumbing |
| inline comments | the ambient token | plumbing: grepping `post_inline_comments.py` for `GH_TOKEN`, `Authorization` and `REVIEWER_TOKEN` returns nothing |

**The load-bearing point is that most writes inherit an ambient token
rather than taking one**, so converting them adds a parameter where none
exists — which is not what a survey of wrong values would find. AT-2520
records none of this. **request-changes** is the site kind 4 and this
table share: it is a gate lift rather than a parameter. The approve path
appears in this table only — it is already plumbed, so it is not a
conversion site. (measured)

**One GitHub caller in the pipeline is not `gh` and not a pull-request
write.** `switch_claude_auth.py` reaches `https://api.github.com` directly
through `urllib.request`, and what it writes is **org Actions variables on
`ignite-corp`**, hardcoded as `_CORP_ORG`. It touches no pull request in
any repository, so it is outside that table and outside the conversion
inventory. It belongs here anyway, because it is the clearest instance of
**changing operational settings** — one of the authorities section 3.5
bundles into its *permission* concept, and a different one from reading a
target or publishing to it. It is also the one the pipeline already keeps
pinned to its own org rather than to the repository under review. (measured)

**The App-token mint that writes to a pull request is the one a
cross-repo run would have to re-scope.** The aggregate's **Mint reviewer
App token** step passes no `owner:`, so `create-github-app-token`
defaults to the owner of the repository the workflow runs in.
`base-ai-review-aggregate.yml` is `workflow_call`, and its checkout step
names that repository the caller's — "Checkout caller repo (PR head)" —
so today that default is the **consumer's** owner, which is also the
owner of the repository under review. **Today it is therefore correct,
not mis-scoped**; it becomes wrong only when the executing repository
stops being the review target, which is the coupling of section 3.5 item
1. The mint that *does* carry
`owner: ignite-corp`, in `base-ai-review-single.yml`'s **Mint corp App
token (auth switch)** step, feeds only the **Switch Claude auth on usage
limit** step and `switch_claude_auth.py`. **That token writes nothing to
any pull request in any repository.** AT-2520's one auth item names the
inert token and omits the only App token that reaches a pull request.
(measured)

**The spike measured a different mechanism than the one that would ship.**
`extract_pr_diff.sh` takes the REST path only on its merged-PR fallback;
the open-PR path, which is the normal case, runs `git diff` against the
cloned tree. The spike's diff read was a `curl` to `api.github.com` with a
Bearer header. It establishes REST diff readability with an App
installation token; it does not establish that `actions/checkout` can
clone that repository with that token over git-over-HTTPS. Two greps
over the spike, each for its own conclusion. `grep -c actions/checkout`
returns **0**, so the spike declares no checkout action. `grep -nw git`
returns **one** hit, and it is prose inside a comment — a line about
`diff --git` headers being a change count — so stripping comments first
(`sed 's/#.*//' | grep -cw git`) returns **0**: no `run:` step shells out
to git under another name. The second grep is reported with its hit
rather than as a clean zero, because the zero is only true of the
comment-stripped file and a reader running the plain command would see
the hit and not the reason.

A checkout probe does not exist. (measured; the consequence for the
promotion plan is **inferred**.)

### 5.3 Two things cross-repo gets for free

- **The AT-2038 tree guard needs no edit.** The **Confirm the tree matches
  the diff** step compares `HEAD_SHA` against `git rev-parse HEAD` of the
  workspace root. It names no repository — it is positional, not nominal —
  and it runs before the scripts checkout, so the subdirectory clone can
  never be what it reads. Point the root checkout at a target and the
  guard follows. Under a *partial* conversion, where `HEAD_SHA` is the
  target's but the checkout is still the caller's, it mismatches and
  exits 1: **fail-closed**. (measured)
- **The head SHA need not be transported by the trigger.** When no event
  payload supplies one, `base-ai-review-prepare.yml` resolves the base
  ref, head SHA, author, merge state and labels from `gh api
  repos/.../pulls/<n>`. That is the existing, exercised dispatch path, so
  a trigger carrying only target repository and PR number is sufficient.
  (measured)

The two checkouts do not collide: the scripts checkout already carries
`path: .ai-dev-pr-review`, so two `repository:` values coexist in every
run today. (measured)

### 5.4 The guards disagree on failure direction

AT-2038's tree guard fails **closed** (above). The AT-2092 staleness
re-read in `aggregate_reviews.py` fails **open**: when the live head cannot
be read it returns "not stale" and the run posts anyway. Its docstring
argues that choice for rate limits, network failures and deleted pull
requests — a redundant verdict is visible and superseded, a dropped one is
the defect the file exists to prevent. **A credential that can never read
the target is a case that reasoning does not cover**, and under a
scheduled cross-repo trigger, where two polls can straddle a force-push,
it becomes reachable. (measured for both mechanisms and for the docstring;
**inferred** for "does not cover" and for reachability — no double-post was
observed.)

### 5.5 The concurrency key names no repository

`base-ai-review-orchestrator.yml`:

```yaml
concurrency:
  group: ai-review-${{ github.event.pull_request.number || inputs.pr_number || github.run_id }}
  cancel-in-progress: true
```

The key is built from a pull request **number** and nothing else. Within
one repository that is correct and is what makes a re-push cancel the
round it supersedes. Across repositories it is not: a review of repository
A's PR #12 and a review of repository B's PR #12 land in the same
concurrency group, and `cancel-in-progress: true` means **one cancels the
other**. (The key and the `cancel-in-progress` setting are **measured**;
the collision itself is **inferred** — no cross-repo run has been made, so
it has not been observed.)

This is the clearest case of the coupling in section 3.5 item 1 — the key
identifies a review by target PR number while silently relying on
execution location to disambiguate it, and cross-repo removes that
disambiguation. It is also why "the orchestrator holds zero
`github.repository` references" must not be read as neutrality: here the
absence **is** the defect, and a survey of hardcodes cannot see a missing
identifier any more than it can see the absent `token:` inputs of
section 5.2.

**No ticket names the concurrency key.** It belongs to the cross-repo
promotion set (AT-2520 / AT-2538 / AT-2560) by subject, and none of the
three records it; it is also not among the occurrences that command
counts, because what has to change here is an identifier that is absent
rather than one that is wrong. Recorded so the gap is visible rather than
inferred from section 7's table.

---

## 6. The evidence ledger

The epic this ticket sits under exists because a merged design record
cited evidence that had never run and facts that had gone stale. This
section is part of the deliverable rather than an appendix to it, and it
holds only what has no claim site in sections 1–5: observations not
made, observations nobody has, and attributed reports with no sentence
of their own. The **measured**, **inferred** and **attributed** marks
live where each claim is made and are not listed again here. Earlier
revisions did list them, and in PR
#187's last two rounds five of eleven findings were that list
mis-describing what sections 3–5 stated correctly — a wrong bucket, a
scope widened in restatement, a test named by position (**attributed**
to AT-2568's body, which classified those rounds; the threads were not
re-read here, and these rounds post-date `4309344`) — because a
ledger derived from the prose is a second copy of the prose, and the
copy is the one that drifts (AT-2568).

### Not read for this document

- **The branch ruleset.** Section 2.3 names the required context from
  `docs/ops/claude-auth-and-review-operations.md` and states the two
  conditions under which a missing result blocks a merge; which branches
  actually require it was not checked here.
- **The three test files `b3555a9` changed.**
  `test_aggregate_reviewer_roster.py`, `test_aggregate_reviews.py` and
  `test_orchestrator_sequential_gates.py`, all under
  `.github/scripts/tests/`, were changed by that merge and not re-read
  here. The first is the aggregate-side counterpart to section 2.1's
  roster claim, and section 2.3 and section 3.2 lean on two other test
  files, so this is coverage the document does not have rather than
  coverage it chose against.
- **Nothing beyond `4309344` about the two roster definitions.** They
  agree at that commit — the orchestrator's six reviewer jobs are one
  parallel and one sequential per name in `REVIEWER_NAMES`, matched by
  reading both (section 2.1). **No test compares them**, so nothing
  enforces that they keep agreeing, and none was written here.

### Unmeasured — the largest first

1. **No byte has ever been written to a foreign pull request.** What is
   measured is narrower: *this* spike makes no POST, by its own step
   header and Probe 4's self-labelling. The historical claim covers every
   writer and has had no audit, so it is unmeasured, and every cross-repo
   write claim rests on the probe's one inference.
2. **No `actions/checkout` against a foreign repository has been
   attempted here.** The spike contains no `actions/checkout` at all
   (measured, section 5.2), and no such probe is recorded in AT-2520,
   AT-2538 or AT-2560. That bounds the negative to this repository and
   those tickets rather than asserting it of every attempt anywhere.
3. **The serialized local reviewer path at `4309344` has never been run
   end to end.** The one preserved end-to-end local run was of an ancestor
   build that ran the three reviewers concurrently via a
   `ThreadPoolExecutor`; at `4309344` there is no `ThreadPoolExecutor`
   under `.github/scripts/` — the driver and the reviewer scripts — and
   `run_reviewers` runs them one at a time. The name does still occur at
   that commit in `docs/tasks/local-review-driver-design-record/`, in the
   record's prose and in its evidence script, so the grep is only a
   negative over the code path and not over the tree. (The grep is
   measured; the preserved run is **attributed** to the local design
   record's section 5-0.) This is the honest caveat on every "it works
   locally" statement.
4. **The App installation-token lifetime against a round's duration** —
   one hour against three reviewer jobs plus an aggregate — is reasoned,
   not observed.
5. **Behaviour against a target where the App is installed** was not
   exercised by the one recorded spike execution, which used an
   uninstalled target.
6. **What the gemini job's install actually makes importable.** Section
   3.3's transitive-roots point is a property of the resolved environment;
   no install was run for this document.
7. **That AT-2569's bypass is reachable at all.** It needs every reviewer
   payload absent *and* every reviewer job reporting success. Section
   4.1's claude and codex nets each break that conjunction under a
   credential outage, and no path that satisfies it has been constructed
   or observed here. The code is measured (section 3.5 item 2); the
   reachability is not, which is why section 7's row states the defect
   and leaves the status to this entry.

### Attributed, not re-verified here

- The spike result for `ignite-pilot-org/max-builder` — PR read 200, diff
  read, `pull_requests=write` — which appears in **AT-2560's body only**
  and in no AT-2520 comment. The provenance asymmetry is flagged
  deliberately: this is the most-cited positive cross-repo result and it
  has one source.
- The `claude` 2.1.269 hook execution that motivates agent-config
  stripping, recorded in `docs/local-review.md`, "Security".

---

## 7. Known defects, with their tickets

This document changes no code. Each item below is somebody else's ticket,
and is listed because a boundary document that omitted them would read as
if the boundary were clean.

| Ticket | Defect | Which boundary it crosses |
|---|---|---|
| **AT-2427** | A verdict is identified by **who wrote it**, not by `REVIEW_MARKER`. `fetch_round_count` filters on `.user.type == "Bot"`, so a local verdict never raises the round counter and the convergence cutoff never fires; `BOT_LOGIN` folds one author's history, so on a pull request carrying both Actions and local rounds neither value is right. Both close together by keying on the marker. | Local verdicts are invisible to Actions-path bookkeeping. Open; reproduces at `4309344`. |
| **AT-2510** | The import inversion is **gone** and the guard exists and passes; what remains is the two residuals of sections 3.3 and 3.4 — the dependency axis does not deny by default, and the guard covers import rather than execution. The ticket is still open, which makes a merged boundary violation look live when it is not. | The boundary itself. |
| **AT-2539** | gemini's run step has no `always()` net, where claude's and codex's steps do (section 4.1). | Actions path only; the local path's `guarded_main` closes the same hole. |
| **AT-2569** | `no_artifact_bypass` lets the aggregate job exit 0 with every reviewer payload absent, so the merge-gate context of section 2.3 is satisfied by nothing. The formal APPROVED review is withheld by the coverage check; the green status is not. Reachable only when every reviewer step dies without writing — section 6 carries the evidence status. Not via a credential outage: claude's `always()` net writes a failed verdict file and codex's `always()` step exits 1, and either one breaks the every-payload-absent-and-every-job-success conjunction, even though gemini records nothing (section 4.1). | **The merge gate, on both paths**: the aggregate is the same script locally, though a local run starts no job and so reaches no context (section 2.3). |
| **AT-2411** | The Actions path passes the codex prompt as one argv element. | Divergence; local uses stdin. |
| **AT-2424** | Filed when the Actions path promoted legacy verdict names after the fallbacks, so an error verdict masked the real one. At both `4309344` and `b3555a9` the codex promotion is gated on the run step's marker and carries `--target-is-ours`, so that masking no longer occurs; the ticket is still open in Jira against a behaviour whose workflow-side fix has shipped. | Divergence; local promotes before the fallbacks (section 4). |
| **AT-2520 / AT-2538 / AT-2560** | Cross-repo promotion. Section 5 records which sites the tickets name as important and why the important ones are elsewhere; the appendix records where their line citations have moved. | The unbuilt path. |
| **AT-2425** | The local design record's evidence script did not verify what it claimed to: in the only path runnable today its two computed keys are absent from its recorded-value table, so it compared nothing and reported clean. The ticket carries seven items. | That record's self-verification, not this document's. The change that rewrote section 6 addresses items 1–4 by deleting the script (the comparison, the double count, the substring serialisation check, the unreachable no-argument branch) and item 6 by correcting two figures, and records in that record's `evidence/MANIFEST.md`, for each derived number, which source and which command produced it — naming the three whose source went with `/tmp` (prompt composition, the `EXISTING_COMMENTS=` size, `232`), which survive as recorded values against a hash. Items 5 (the machine-bound run scripts) and 7 (the `review-*.json` basenames) are deliberately kept, with the measured reason in that MANIFEST. The ticket's status is Jira's. |
| **AT-2428** | Three factual errors in the merged local design record, including a cited commit that is no longer an ancestor, so re-checking it today reads as the document being wrong. | Same. All three corrected in the same change; the ticket's status is Jira's. |

**AT-2425 (items 1–4 and 6) and AT-2428 were addressed against
`docs/tasks/local-review-driver-design-record/design-record.md` in the
same change as this section's rewrite.** This document does not
supersede that record and does not inherit its evidence: every claim
here was read at `4309344` or carries an **attributed** mark at its
claim site; section 6 lists only the attributed reports that have no
sentence of their own.

---

## Appendix — citations that had moved by `4309344`

Recorded so the next reader does not re-derive them, and as the reason this
document cites files and symbols rather than line ranges.

| Source | Claim | At `4309344` |
|---|---|---|
| AT-2520 | `base-ai-review-aggregate.yml:89` holds a hardcoded `repository:` | That line is now `permissions:`; the checkout's `repository:` moved down the file. The substance holds. |
| AT-2520 | `aggregate_reviews.py:499` and `:1076` read the target from the environment | Both line numbers are stale. The reads exist; the substance holds. |
| AT-2520 | The hardcoding is two checkout lines | Three checkouts carry it, and `base-ai-review-prepare.yml`'s — the one that resolves the pull request and produces `pr.diff` — is the one the ticket omits. |
| AT-2520 | The AT-2038 guard must be changed to use the target's head SHA | It must not; it is positional (section 5.3). |
| AT-2520 | The two checkouts would collide in a cross-repo run | They do not; `path: .ai-dev-pr-review` already separates them. |

Line ranges in this repository have gone stale inside a single day. A
pointer a reader will follow later names a file and a symbol or a step.
