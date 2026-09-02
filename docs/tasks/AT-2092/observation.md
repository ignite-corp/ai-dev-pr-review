# AT-2092 observation: a superseded run must not post a verdict

Scratch PR used to observe the head-SHA discriminator on the base path.

The completion criterion for AT-2092 is a measurement, not a code read: push
twice in quick succession so the first run is superseded, then confirm that
exactly one verdict comment exists on the PR and that it belongs to the live
head.

## What this exercises

`base-ai-review-orchestrator.yml` keeps `if: always()` on the aggregate job on
purpose -- the job's name is a required status context, so a job that does not
run leaves the check Pending forever. The posting is stopped inside
`aggregate_reviews.py` instead: `_head_is_stale()` compares the head the run
reviewed against the PR's current head and short-circuits before posting once a
newer run owns the comment slot.

## Step 1

First commit. The second commit follows once this run is in progress.
