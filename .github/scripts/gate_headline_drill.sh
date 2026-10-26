#!/usr/bin/env bash
# gate-headline-drill - run the pr-response-cycle merge gate's roster check on
# the aggregate headlines measured on this repository, and prove it refuses the
# ones a green job cannot vouch for (#140).
#
# AT-2511: a reviewer step cut at the 10-minute cap leaves the review job
# `success`, so the check rollup is green while one configured reviewer never
# read the diff. The only place on the PR where that shows is the aggregate
# comment's headline - `N/M reviewers`, `N/M LLM responses` - and the gate's
# kind (i) now reads it through headline_roster(). This drill feeds that
# function the headlines as they were measured, verbatim, and requires:
#
#   1. a full roster with Result Approved passes, and prints its fractions
#   2. `Approved with N unreviewed major issue(s)` passes the roster and
#      leaves N in agg_majors, where (c) picks it up - it is not a pass on
#      its own, and it is not refused here either
#   3. a short roster, a zero roster, a missing fraction, and every Result
#      word other than Approved are refused, including a Result the
#      aggregate does not write today
#   4. a full fraction over the wrong denominator is refused: `2/2` and `1/1`
#      block when EXPECTED_REVIEWERS is 3, and an unset or non-numeric
#      EXPECTED_REVIEWERS blocks everything
#   5. (c)'s major_dispositions passes a major with a replied thread at its
#      path and line, or on its path whose first comment is the producer's
#      rendering of the finding -- `+ **major** (<reviewer>): <description>`,
#      the description whole to the end of the line, a line-less thread
#      included -- or a later quoted reply, and blocks one with none of
#      these, one whose only thread is elsewhere in the same file or carries
#      the description as prose, or a body that lists fewer majors than the
#      headline counted; a path containing a colon survives
#   6. select_aggregate reads the aggregate on either surface (#174): the
#      three measured shapes - an issue comment by github-actions[bot], a
#      review by github-actions[bot], a review by ignite-ai-review-approver[bot]
#      - are each found; a comment outside the run's window, a DISMISSED
#      review on the head, a COMMENTED marker review on the head whatever
#      its headline (the producer reviews only with APPROVE and
#      REQUEST_CHANGES), an APPROVED review on a previous head, a marker
#      review by a login outside AGG_AUTHORS and a review with no timestamp
#      are refused; the newest candidate across both surfaces wins. Identity
#      is per verdict, not per repository: a CHANGES_REQUESTED review by
#      github-actions[bot] on an App-shaped PR is selected and refused on
#      its Result word alone, a github-actions[bot] approval on an
#      App-shaped PR is accepted, and the two comment fallbacks - each with
#      an `Intended verdict` note appended, differing only in the reason -
#      are the aggregate present, their headline read by aggregate_headline,
#      which keys on no note. reviews_complete blocks a review list shorter
#      than the PR's count: two non-dismissed candidates on one head pick
#      the newer, and a list that lost it promotes the older approval
#   7. cutoff_findings reads a round's findings off the convergence-cutoff
#      surface, where no thread id exists: the measured `round-cutoff-claude-r7`
#      comment on ignite-corp/ai-dev-pr-review#188 blocks with no thread
#      anywhere in the input and no quoted response, passes against the
#      measured disposition comment that names its marker and quotes each
#      of its items by location, and blocks when the later comment names
#      the marker without quoting, quotes without naming it, quotes an
#      unrelated line or only one of the two items, comes from a Bot, or
#      predates it; dispositions split across two replies answer together,
#      whichever of them names the marker and whichever quotes; a cutoff
#      comment before the window is not this round's; the marker is
#      matched whole, so a reply naming r10 does not answer r1.
#      The cutoff comment is found by its marker line under all three
#      identities the producer runs as - github-actions[bot], the reviewer
#      App, the operator's own login (a User) - while a human comment with
#      no marker, a disposition naming the marker in backticks, and a reply
#      quoting the marker under a > are not cutoff comments (AT-2803).
#      cutoff_window_start, which hands cutoff_findings its window start,
#      reads the earliest review-workflow run on the head off the unfiltered
#      run list, so a cutoff comment posted by a run that then concluded
#      failure, or by the older of two completed/success runs, is inside
#      the window (#194 round 8); the first verdict wins when it is earlier,
#      other workflows' runs do not count, and a run with no createdAt or
#      an unreadable list refuses rather than start the window late; the
#      gate's own call site prints that refusal rather than capturing it
#      (#194 round 11)
#   8. convergence_round refuses a short-roster round as a convergence round
#      - the same fraction headline_roster reads for kind (i) - and refuses
#      a full round whose report does not state whether its findings affect
#      behaviour
#   9. verdict_dispositions, (c)'s one call site, reads every verdict (a)
#      named through one loop that never asks which kind: a kind (ii)
#      approval that lists a major in the aggregate's item shape blocks
#      with no replied thread and no later non-Bot quote, exactly as the
#      aggregate's unreviewed major does, and passes with either; a quote
#      before the verdict or by a Bot does not answer it; the measured
#      hand-verdict shape, prose with no item line, has nothing mechanical
#      to check; both kinds on one head are each checked; an empty or
#      unreadable source list and a source with no timestamp are refused
#      (#194 round 9)
#  10. review_workflow_known checks REVIEW_WF before anything reads it: the
#      shipped placeholder and an empty name are refused outright; a name
#      some run on the head carries passes on the list alone; a name no run
#      carries passes only when the repository's workflow list has it --
#      step 8's "the review did not run on this head" row, left to (a) --
#      and is refused when it does not, an empty run list included; the
#      gate's own call site sits before the name's first reader and stops
#      on the placeholder and on a mistyped name (#194 round 12)
#  11. (b)'s thread read, lifted from the gate body by its text, survives an
#      empty first comment: the row keeps an empty line field rather than
#      aborting the whole read as an unreadable thread list (#194 round 12)
#  12. runner_dispatch tells the three readings of an empty [] row apart
#      where a runner dispatches the round, which neither surface in the
#      target can: no run for this PR in RUNNER_REPO is no dispatch, one
#      queued or in_progress is a WAIT, and one that completed while no
#      check run arrived is a dispatch that died before posting. The rule
#      the sections exercising it follow is step 8's, in SKILL.md, and is
#      not restated here. What a reader of those fixtures needs is the two
#      things they are built around: the candidates are matched on the
#      run's `name` and bounded by the force-push stamp for this head,
#      with the committer date only where there is no such stamp -- which
#      is why every fixture carries a run-name and sits on one side of
#      HEAD_TIME or the other, and why the bound has two paths (AT-3097)
#
# All fourteen core functions are extracted from SKILL.md by name, not copied here:
# the gate block is pasted whole into shells on any machine, so it has to
# carry its own copy, and a second copy in the drill would keep passing after
# the pasted one drifted. Sections 6 to 8 are the controls: a copy of the
# roster function with one fraction's equality cut out must pass a headline
# short on that fraction alone, a copy with the Result check cut out must
# pass a Changes Requested headline that carries full fractions, a copy with
# the denominator check cut out must pass `2/2` against 3, a copy of the
# majors function with its final tally cut out must pass an unanswered major,
# and a copy of the aggregate selector with its commit_id equality cut out
# must pass the previous head's approval, a copy of the thread match with
# its equality widened back to a substring must pass a thread that mentions
# the description in prose, a copy of the cutoff reader with
# its quote requirement cut out must pass a comment that names the marker and
# every location in prose and quotes nothing, one with its non-Bot
# requirement cut out must pass a bot's own reply, one with its location
# binding cut out must pass a reply that names the marker and quotes an
# unrelated line, and one with the marker's trailing guard cut out must
# pass a reply naming r10 against r1, a copy of the window start with its
# earliest-wins tail turned newest-wins must start the window after the
# older run's cutoff comment, a copy of the convergence decision with its roster
# call cut out must pass a 2/3 round, and a copy of the (c) call site with
# its loop filtered back to kind (i) -- round 9's shape -- must pass a kind
# (ii) approval listing an unanswered major, as must a copy with its majors
# call cut out, and a copy of the name check with its repository lookup cut
# out must pass a mistyped name, and -- for the locator, in section 8f -- a
# copy with its head bound cut must report the previous head's round as a
# dispatch that died, a copy reading display_title must accept a run whose
# commit subject merely cites this PR, a copy checking only the right edge
# of the needle must accept another owner's run, a copy of the gate's arm
# that pipes the force-push read must report a failed read as an absent
# event, a copy with the case's default arm deleted must pass over a
# reading it does not know, and a copy with the locator's `elif` cut to a
# plain `if` must consult the runner on a head a round was already found
# for. Without them a refusal above would
# only prove that the function refuses, not that it refuses for the line the
# drill names. Section 9 is the control on the extraction itself.
#
# Touches nothing real: no gh calls, no PR, no comments.

set -uo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
SKILL="$HERE/../../plugins/pr-response-cycle/skills/pr-response-cycle/SKILL.md"
FUNC_NAME=headline_roster
MAJORS_FN=major_dispositions
FINDING_FN=finding_key
THREAD_FN=thread_answers
WINDOW_FN=window_bounds
AGG_FN=select_aggregate
HEADLINE_FN=aggregate_headline
COUNT_FN=reviews_complete
CUTOFF_FN=cutoff_findings
SINCE_FN=cutoff_window_start
CONV_FN=convergence_round
DISP_FN=verdict_dispositions
KNOWN_FN=review_workflow_known
RUNNER_FN=runner_dispatch
pass=0
fail=0
skipped=0

chk() {
    if [ "$2" = "$3" ]; then
        printf '  PASS  %s\n' "$1"
        pass=$((pass + 1))
    else
        printf '  FAIL  %s (got %s, want %s)\n' "$1" "$2" "$3"
        fail=$((fail + 1))
    fi
}

# shellcheck disable=SC2015  # the || arm is the failure of either test, as intended
D=$(mktemp -d "${TMPDIR:-/tmp}/gate-headline-drill.XXXXXX") && [ -n "$D" ] || {
    printf 'gate-headline-drill: mktemp failed - refusing to run without a scratch directory\n' >&2
    exit 1
}
trap 'rm -rf "$D"' EXIT

# A function body as SKILL.md carries it: from its definition line to the
# first line that is a lone closing brace. That is the constraint on the
# functions in SKILL.md: no inner line may be a bare `}` at column one, or
# the extraction stops there and sources a truncated body that may still
# define the function. A second pass of this same program cannot see that:
# it stops at the same brace and yields the same bytes. Section 1 catches
# it before anything is sourced by asserting each function's last two
# lines are its own closing statement and the brace -- each gate function
# added here needs its own tail-line assertion there -- and section 9 strips
# and re-extracts as the control.
extract() { # $1 file, $2 function name
    sed -n "/^$2() {\$/,/^}\$/p" "$1"
}

# pass|block for one headline under the function currently defined, in a
# subshell so a control's mutated copy never replaces the real one. The
# roster is judged against EXPECTED, 3 unless a section says otherwise;
# the literal `unset` leaves EXPECTED_REVIEWERS undefined, as an unfilled
# gate would.
EXPECTED=3
verdict() { # $1 function file, $2 headline
    ( # shellcheck disable=SC1090
      . "$1"
      if [ "$EXPECTED" = unset ]; then unset EXPECTED_REVIEWERS; else EXPECTED_REVIEWERS=$EXPECTED; fi
      "$FUNC_NAME" "$2" > /dev/null 2>&1 && echo pass || echo block )
}

# pass|block for major_dispositions: $2 body, $3 want, $4 thread lines, $5 quotes.
majors() { # $1 function file, then the function's four arguments
    ( # shellcheck disable=SC1090
      . "$1"; "$MAJORS_FN" "$2" "$3" "$4" "$5" > /dev/null 2>&1 && echo pass || echo block )
}

# One thread line in thread_answers' shape: path, lines, replied|unreplied,
# the first comment's first line, joined by the unit separator (0x1f, as
# octal so a POSIX printf reads it) -- not a tab, which `read` treats as
# whitespace and collapses when the lines field is empty.
thr() { printf '%s\037%s\037%s\037%s\n' "$1" "$2" "$3" "$4"; }

# ok|refused for window_bounds on two edges.
window() { # $1 function file, $2 createdAt, $3 updatedAt
    ( # shellcheck disable=SC1090
      . "$1"; "$WINDOW_FN" "$2" "$3" > /dev/null 2>&1 && echo ok || echo refused )
}

# surface:id:state of the aggregate select_aggregate picks, or `none`. The
# comment list and the review list go in as the gate builds them; the
# marker and the author list are the gate's own literals.
MARK='<!-- multi-llm-review -->'
agg() { # $1 function file, $2 comments json, $3 reviews json, $4 head, $5 createdAt, $6 updatedAt
    local out
    # shellcheck disable=SC1090,SC2034  # the function reads AGG_MARKER and AGG_AUTHORS
    out=$( . "$1"; AGG_MARKER=$MARK; AGG_AUTHORS='github-actions[bot] ignite-ai-review-approver[bot]'
           printf '%s\n%s\n' "$2" "$3" | "$AGG_FN" "$4" "$5" "$6" 2> /dev/null )
    if [ -n "$out" ]; then printf '%s' "$out" | jq -r '"\(.surface):\(.id):\(.state)"'; else echo none; fi
}
# One comment or review record in the normalised shape the gate reads
# (issues/N/comments and pulls/N/reviews, mapped as the gate block maps
# them). An empty timestamp becomes null, as jq renders a missing
# submitted_at.
cmt() { # $1 id, $2 created_at, $3 login, $4 user.type, $5 body
    jq -cn --arg id "$1" --arg t "$2" --arg a "$3" --arg ty "$4" --arg b "$5" \
      '{id: ($id | tonumber), created_at: (if $t == "" then null else $t end), author: $a, type: $ty, body: $b}'
}
rev() { # $1 id, $2 submitted_at, $3 login, $4 user.type, $5 state, $6 commit_id, $7 body
    jq -cn --arg id "$1" --arg t "$2" --arg a "$3" --arg ty "$4" --arg st "$5" --arg c "$6" --arg b "$7" \
      '{id: ($id | tonumber), created_at: (if $t == "" then null else $t end), author: $a, type: $ty, state: $st, commit_id: $c, body: $b}'
}
lst() { printf '%s\n' "$@" | jq -cs .; }
# The headline the gate reads off a candidate, through the gate's own function.
headline_of() { # $1 function file, $2 candidate json
    ( # shellcheck disable=SC1090
      . "$1"; printf '%s' "$2" | "$HEADLINE_FN" )
}
# ok|block for reviews_complete on a review list and the PR's count.
complete() { # $1 function file, $2 reviews json, $3 total
    ( # shellcheck disable=SC1090
      . "$1"; printf '%s' "$2" | "$COUNT_FN" "$3" > /dev/null 2>&1 && echo ok || echo block )
}

# ok|block for cutoff_findings on a comment list and a window start.
cutoff() { # $1 function file, $2 comments json, $3 since
    ( # shellcheck disable=SC1090
      . "$1"; printf '%s' "$2" | "$CUTOFF_FN" "$3" > /dev/null 2>&1 && echo ok || echo block )
}

# The window start cutoff_window_start prints for a run list, the review
# workflow's name and the first verdict's timestamp; `refused` when it
# returns non-zero, `none` when it prints nothing.
since_of() { # $1 function file, $2 runs json, $3 workflow name, $4 first verdict
    ( # shellcheck disable=SC1090
      . "$1"; out=$(printf '%s' "$2" | "$SINCE_FN" "$3" "$4" 2> /dev/null) || { echo refused; exit; }
      printf '%s\n' "${out:-none}" )
}

# ok|refused for review_workflow_known on a run list, a name and the
# repository's workflow names, one per line.
known() { # $1 function file, $2 runs json, $3 workflow name, $4 repository names
    ( # shellcheck disable=SC1090
      . "$1"; printf '%s' "$2" | "$KNOWN_FN" "$3" "$4" > /dev/null 2>&1 && echo ok || echo refused )
}

# One run in the shape `gh run list --json` returns; an empty createdAt is
# the null the API leaves on a run it has not timestamped.
run() { # $1 id, $2 workflowName, $3 status, $4 conclusion, $5 createdAt, $6 updatedAt
    jq -cn --arg id "$1" --arg wf "$2" --arg st "$3" --arg c "$4" --arg s "$5" --arg e "$6" \
       '{databaseId: ($id | tonumber), workflowName: $wf, status: $st, conclusion: $c,
         createdAt: (if $s == "" then null else $s end), updatedAt: $e}'
}

# converged|block for convergence_round on a headline and a behaviour
# statement, judged against EXPECTED as the roster is.
converged() { # $1 function file, $2 headline, $3 behaviour statement
    ( # shellcheck disable=SC1090
      . "$1"
      if [ "$EXPECTED" = unset ]; then unset EXPECTED_REVIEWERS; else EXPECTED_REVIEWERS=$EXPECTED; fi
      "$CONV_FN" "$2" "${3:-}" > /dev/null 2>&1 && echo converged || echo block )
}

# pass|block for verdict_dispositions on thread lines, a source list and a
# comment list, output discarded: the (c) print is the human half.
disp() { # $1 function file, $2 thread lines, $3 sources json, $4 comments json
    ( # shellcheck disable=SC1090
      . "$1"; "$DISP_FN" "$2" "$3" "$4" > /dev/null 2>&1 && echo pass || echo block )
}
# One verdict source in the shape (a) builds for (c): kind, label,
# created_at, since, body and want -- a want of `null` is a kind (ii)
# source, which declares no count and is counted off its body. An empty
# timestamp becomes null, as a missing field would.
vsrc() { # $1 kind, $2 label, $3 created_at, $4 since, $5 body, $6 want (a number, or null)
    jq -cn --arg k "$1" --arg l "$2" --arg t "$3" --arg s "$4" --arg b "$5" --argjson w "$6" \
      '{kind: $k, label: $l, created_at: (if $t == "" then null else $t end), since: $s, body: $b, want: $w}'
}

# The runner's repository, and the repository it reviews - the two
# runner_dispatch is told apart by, as the gate passes them.
RREPO=ignite-corp/ai-dev-pr-review-runner
TREPO=hyuk-hur/dev-dotfiles
# The head's bound, and the two readings it is taken from. The
# force-push event's createdAt is GitHub's own stamp for the moment the
# SHA became the head, and where there is one it is the whole bound;
# the committer date is the fallback for the heads that have no such
# event, and is the client's clock, which `GIT_COMMITTER_DATE` sets to
# anything. Every run fixture below is placed on one side of HEAD_TIME
# or the other: the run-name carries no SHA, so this is the only thing
# that tells the round dispatched for this head from the one dispatched
# for the last.
HEAD_COMMIT_TIME=2026-10-21T00:44:30Z
HEAD_TIME=2026-10-21T00:45:00Z
# The head the gate's own arm is run against, below.
ARM_HEAD=2917fee0000000000000000000000000000000ff
# Overrides the page cap the gate block declares, for the controls that
# show the walk's loop reads the name rather than a literal and refuses
# a cap it cannot walk with. Empty means "use the block's own value",
# which every other call wants; the literal `unset` leaves
# FPUSH_PAGE_CAP undefined, as a block missing its declaration would.
ARM_CAP=''
# One run of the runner's own review.yml, in the shape ITS API returns -
# unprojected, head_branch and all, so SKILL.md's own --jq runs over an
# API-shaped payload rather than over something already narrowed here.
# `run-name:` sets `name` AND `display_title` to the same string, so the
# fixture does too: measured 2026-10-22 over the runner's last 100
# review runs, the two are identical on every one and neither is ever
# empty. A fixture that put the run-name in `display_title` alone and a
# bare `LENS review` in `name` - a shape the API does not produce - left
# the filter green while it read the wrong field.
rrun() { # $1 id, $2 status, $3 conclusion, $4 created_at, $5 run-name, $6 html_url
    jq -cn --arg id "$1" --arg s "$2" --arg c "$3" --arg t "$4" --arg d "$5" --arg u "$6" \
      '{id: ($id | tonumber), name: $d, head_branch: "task/x", status: $s,
        conclusion: $c, created_at: $t, updated_at: $t, display_title: $d, html_url: $u}'
}
# The other measured shape: a workflow with no `run-name:`, where `name`
# is the workflow's name and `display_title` is the head commit's
# subject line, measured 2026-10-22. A commit subject can carry
# `<owner>/<repo>#<pr>`; a workflow name cannot.
rrun_no_runname() { # $1 id, $2 status, $3 conclusion, $4 created_at, $5 workflow name, $6 commit subject, $7 html_url
    jq -cn --arg id "$1" --arg s "$2" --arg c "$3" --arg t "$4" --arg n "$5" --arg d "$6" --arg u "$7" \
      '{id: ($id | tonumber), name: $n, head_branch: "task/x", status: $s,
        conclusion: $c, created_at: $t, updated_at: $t, display_title: $d, html_url: $u}'
}
rpage() { printf '%s\n' "$@" | jq -cs '{total_count: length, workflow_runs: .}'; }
# One force-push timeline event, in the shape the GraphQL API returns it
# - a timeline node, NOT the {oid, at} the gate's first --jq projects it
# down to. The stub below feeds these through that --jq, so the
# projection path and its field names are exercised and not assumed:
# handing the arm something already projected is how a fixture becomes
# the accomplice of the code it is meant to test. GraphQL carries the
# SHA the push left at the head, which REST's head_ref_force_pushed does
# not - measured on hyuk-hur/dev-dotfiles#197 and #198, `sha: null` on
# every one - and that SHA is what the block's second jq selects on.
fpush() { # $1 afterCommit oid, $2 createdAt
    jq -cn --arg o "$1" --arg a "$2" '{createdAt: $a, afterCommit: {oid: $o}}'
}
# Those nodes inside the envelope the API wraps them in, windowed and
# cursored the way the real connection is. Two properties are modelled,
# both measured on hyuk-hur/dev-dotfiles#194, 2026-10-23:
#
#   * `before:` pages backwards. A cursor is `c<index>`, the index of
#     the first node of the page it came from, and a page is the 100
#     nodes ending there - so page 2's `startCursor` reaches page 3, as
#     it does live (checked against #194 with `last:5`, two pages deep,
#     distinct cursors each time).
#   * `last:` has a ceiling and the server ENFORCES it. `last:100` and
#     `last:250` are accepted; `last:300` and `last:900` are refused
#     with `EXCESSIVE_PAGINATION`. The stub refuses them too, because a
#     stub that accepts any window is a stub on which a widening stays
#     green over one the API rejects. The window it judges is the one
#     the QUERY asks for: the caller's gh stub parses `last:<n>` out of
#     the query text, because the arm carries it as a literal there and
#     sends no `-F k=`, so a limit enforced against the flag alone would
#     be enforced against nothing.
gqlenv() { # $1 newline-separated nodes, oldest first; $2 the window k; $3 'force' to claim older ones regardless; $4 the before-cursor
    local k=${2:-100} forced=${3-} cur=${4-}
    if [ "$k" -gt 250 ]; then
        # shellcheck disable=SC2016  # the backticks are the API's own message
        printf 'EXCESSIVE_PAGINATION: Requesting %s records on the `timelineItems` connection exceeds the `last` limit of 250 records.\n' "$k" >&2
        return 1
    fi
    printf '%s\n' "$1" | command jq -cs --argjson k "$k" \
        --argjson f "$([ "$forced" = force ] && echo true || echo false)" \
        --arg cur "$cur" \
        '(if $cur == "" then length else ($cur | ltrimstr("c") | tonumber) end) as $end
         | ([0, $end - $k] | max) as $start
         | {data:{repository:{pullRequest:{timelineItems:{
             pageInfo:{hasPreviousPage: ($f or ($start > 0)), startCursor: ("c" + ($start|tostring))},
             nodes: .[$start:$end]}}}}}'
}
# And the commit payload the other read projects `.commit.committer.date`
# out of, carrying the author date too, as the API does.
commitpayload() { # $1 committer date
    command jq -cn --arg d "$1" '{sha: "2917fee", commit: {author: {date: "2026-10-21T00:10:00Z"}, committer: {date: $d}}}'
}
# `gh api` as the locator calls it: the URL is recorded, the --jq program
# the function passes is applied to the fixture by real jq - so the
# projection in SKILL.md is exercised, not assumed - and the second page
# is emitted only when --paginate is on the command line, which is what
# lets the pagination assertion go red.
# The locator's exit status is captured rather than piped away, for the
# reason jqfail below captures it: a helper ending in `| tr` throws the
# status out, so every refusal this helper drives would be asserted as
# empty stdout alone, and a guard rewritten as
# `|| { echo ...; return 0; }` satisfies that just as well as a refusal
# does. The status-refusal cases read `error(1)` because of this.
dispatch() { # $1 function file, $2 page 1, $3 page 2, $4 found, $5 the head bound
    ( # shellcheck disable=SC1090,SC2034  # the function reads RUNNER_REPO
      . "$1"; RUNNER_REPO=$RREPO
      local p1=$2 p2=$3
      rm -f "$D/loc-url"
      # shellcheck disable=SC2317  # called by the sourced function, not from here
      gh() {
          local jqp='' paginate=no url=''
          while [ $# -gt 0 ]; do
              case $1 in
                  --jq)       jqp=$2; shift 2 ;;
                  --paginate) paginate=yes; shift ;;
                  api)        shift ;;
                  *)          [ -n "$url" ] || url=$1; shift ;;
              esac
          done
          printf '%s\n' "$url" >> "$D/loc-url"
          # The read's status is the stub's status. Ending on the
          # paginate test instead would swallow a failing --jq, which is
          # exactly what the malformed-envelope cases drive, and the
          # function would see a clean empty read where the real `gh`
          # exits 1.
          local ghrc=0
          printf '%s' "$p1" | jq -r "$jqp" || ghrc=$?
          if [ "$paginate" = yes ] && [ -n "$p2" ]; then
              printf '%s' "$p2" | jq -r "$jqp" || ghrc=$?
          fi
          return "$ghrc"
      }
      local out rc=0
      out=$("$RUNNER_FN" "$TREPO" 194 "${5:-$HEAD_TIME}" "${4-}" 2>/dev/null) || rc=$?
      [ "$rc" -eq 0 ] || { printf 'error(%s)' "$rc"; return; }
      printf '%s' "$out" | tr '\037' '|' )
}
# The gate's own discovery arm, lifted out of SKILL.md and run with the
# functions it calls replaced by tracing stubs: did it consult the
# locator, and what did it print? The guard is an `elif`, so a round on
# either surface must skip the call entirely - that is the half a test
# of the function alone cannot see.
# An EMPTY positional and an OMITTED one mean different things here and
# look identical at the call site: `$6` empty is "a PR with no
# force-push events", `$6` omitted takes the default fixture, and the
# same holds for every knob below it. Count the quote pairs when adding
# a call - one wrong `''` selects a different scenario than the test
# name claims.
arm() { # $1 arm file, $2 round_surface, $3 RUNNER_REPO, $4 tuple, $5 committer date, $6 force-push nodes, $7 which gh read fails, $8 `pipefail` to keep it on, $9 'force' to keep hasPreviousPage true, ${10} which of the arm's own jq calls fails
    # shellcheck disable=SC2034  # the sourced arm reads these, as the gate block does
    ( rm -f "$D/loc-called" "$D/arm-windows"
      HEAD_SHA=$ARM_HEAD
      round_surface=$2; round_status=completed; round_concl=success
      run_start=2026-10-21T01:13:31Z; run_end=2026-10-21T01:18:21Z
      round_url=https://example.invalid/round
      RUNNER_REPO=$3
      local tuple=$4
      local ctime=${5:-$HEAD_COMMIT_TIME}
      # The arm's two reads: `commits/<sha>` for the committer date, and
      # the GraphQL timeline for the force-push events. Both fixtures go
      # in RAW, and the stub applies the --jq the block itself passes -
      # dispatch()'s own rule: with an already-projected fixture the
      # block's first --jq never runs, so a wrong path or field name
      # would read as an absent event, fall back to the committer date,
      # print the caveat, and leave the drill green over a bound nobody
      # had read.
      local pushes=${6-$(fpush "$ARM_HEAD" "$HEAD_TIME")}
      local ghfail=${7-}
      # $9 forces hasPreviousPage true however small the fixture, so the
      # walk can be driven to its cap without 900 fixtures.
      local fpforce=${9-}
      local fpwindow=100
      # The walk's page cap comes from the gate block's own declaration,
      # which sits above the extracted arm: the drill reads it out of
      # the block rather than carrying a second copy of the number, and
      # ARM_CAP overrides it only for the control that shows the loop
      # honours the name rather than a literal.
      if [ "$ARM_CAP" = unset ]; then unset FPUSH_PAGE_CAP; else FPUSH_PAGE_CAP=${ARM_CAP:-$FPUSH_CAP}; fi
      # Which of the arm's OWN jq calls fails, numbered in the order it
      # makes them: 1 head_push_time, 2 fpush_seen, 3 fpush_more,
      # 4 fpush_cursor. The gh reads succeed, so this is the failure the
      # `||` arms exist for and the one no exit status catches without
      # them.
      local jqfail_at=${10-}
      echo 0 > "$D/arm-jqn"
      # shellcheck disable=SC2317  # called by the sourced arm
      jq() { local c; c=$(( $(cat "$D/arm-jqn") + 1 )); echo "$c" > "$D/arm-jqn"
             [ "$c" != "$jqfail_at" ] || return 1
             command jq "$@"; }
      # shellcheck disable=SC2317  # the stubs here are called by the sourced arm, not from this helper
      gh() {
          local jqp='' isgql=no fpcursor='' env='' qtext=''
          while [ $# -gt 0 ]; do
              case $1 in
                  --jq)    jqp=$2; shift 2 ;;
                  # The cursor arrives on `-f`, the string-only form: `-F`
                  # coerces an all-digit value to a JSON number and the
                  # GraphQL variable is declared String. Both flags are
                  # parsed so the control below can send it the wrong way
                  # and the walk can be seen losing its cursor.
                  -f)      case $2 in query=*) qtext=${2#query=} ;; before=*) fpcursor=${2#before=} ;; esac; shift 2 ;;
                  -F)      case $2 in k=*) fpwindow=${2#k=} ;; esac; shift 2 ;;
                  graphql) isgql=yes; shift ;;
                  *)       shift ;;
              esac
          done
          # The window the API would see, taken from the QUERY and not
          # only from `-F k=`: the arm carries it as a literal inside
          # `-f query=...` and sends no `-F k=` at all, so a stub that
          # learned it from the flag alone would measure its own default
          # and the ceiling control would pass for every possible query,
          # `last:300` included. `-F k=` still decides, but only where
          # the query asks for `$k`, which is what the API does too.
          # shellcheck disable=SC2016  # matched literally: the GraphQL variable, not a shell one
          case $qtext in
              *'last:$k'*) : ;;
              *last:*)     fpwindow=${qtext#*last:}; fpwindow=${fpwindow%%[!0-9]*} ;;
          esac
          if [ "$isgql" = yes ]; then
              [ "$ghfail" != graphql ] || return 1
              printf '%s %s\n' "$fpwindow" "${fpcursor:-<none>}" >> "$D/arm-windows"
              env=$(gqlenv "$pushes" "$fpwindow" "$fpforce" "$fpcursor") || return 1
              printf '%s' "$env" | command jq -r "$jqp"
          else
              [ "$ghfail" != commit ] || return 1
              printf '%s' "$(commitpayload "$ctime")" | command jq -r "$jqp"
          fi
      }
      # shellcheck disable=SC2317
      round_usable() { return 0; }
      # shellcheck disable=SC2317
      window_bounds() { printf '%s..%s\n' "$1" "$2"; }
      # shellcheck disable=SC2317
      runner_dispatch() { printf 'called %s\n' "$*" >> "$D/loc-called"; printf '%s' "$tuple"; }
      # The block exits on its error paths, so it is sourced inside a
      # nested subshell: an `exit` there is a status this helper can
      # read, where at this level it would end the helper and look like
      # silence. And `pipefail` is turned OFF in that subshell unless $8
      # asks for it, because the weaker shell is the one the block has
      # to survive: SKILL.md says it is pasted whole into a shell whose
      # options it does not control, and this drill happens to run with
      # `set -uo pipefail`, which would mask a piped read's failure from
      # exactly the control written to catch it.
      local rc=0
      # shellcheck disable=SC1090
      ( [ "${8-}" = pipefail ] || set +o pipefail; . "$1" ) > "$D/arm.out" 2>&1 || rc=$?
      if [ "$rc" -ne 0 ]; then printf 'exit(%s)' "$rc"
      elif [ -e "$D/loc-called" ]; then printf 'consulted'
      else printf 'skipped'; fi )
}
# The locator's local reads, with `gh` succeeding and the function's
# Nth jq call failed instead, counted through a file because each call
# sits in its own command substitution: 1 is `matched`, 2 `unresolved`,
# 3 `earlier`, 4 `bad_status`, 5 `newest`. The status is CAPTURED, not piped away: a
# helper ending in `| tr` throws the function's exit status out and
# leaves the assertions reading empty stdout, which a guard rewritten as
# `|| { echo ...; return 0; }` satisfies just as well as a refusal does.
jqfail() { # $1 function file, $2 which jq call fails
    ( # shellcheck disable=SC1090,SC2034  # the function reads RUNNER_REPO
      . "$1"; RUNNER_REPO=$RREPO
      local want=$2 page out rc=0
      page=$(rpage "$D_DIED")
      echo 0 > "$D/jqn"
      # The stub applies the --jq the FUNCTION passes, as dispatch() and
      # arm() do - a hardcoded projection here would keep these
      # assertions green over a fixture supplying what the code no
      # longer reads. `command jq` so the stub's own call is not one of
      # the calls the counter is numbering.
      # shellcheck disable=SC2317  # both are called by the sourced function
      gh() { local jqp=''
             while [ $# -gt 0 ]; do case $1 in --jq) jqp=$2; shift 2 ;; *) shift ;; esac; done
             printf '%s' "$page" | command jq -r "$jqp"; }
      # shellcheck disable=SC2317
      jq() { local c; c=$(( $(cat "$D/jqn") + 1 )); echo "$c" > "$D/jqn"
             [ "$c" != "$want" ] || return 1
             command jq "$@"; }
      out=$("$RUNNER_FN" "$TREPO" 194 "$HEAD_TIME" '' 2>/dev/null) || rc=$?
      [ "$rc" -eq 0 ] || { printf 'error(%s)' "$rc"; return; }
      printf '%s' "$out" | tr '\037' '|' )
}
# What the locator says on stderr for a given page: its stdout is the
# tuple, so every refusal's text is read here. One helper rather than a
# copy of the stub per needle, for the reason envshape() and badbound()
# were folded into dispatch() - each copy is a place a change to the
# call signature has to land.
locmsg() { # $1 function file, $2 the page the API returns, $3 the needle to count
    ( # shellcheck disable=SC1090,SC2034  # the function reads RUNNER_REPO
      . "$1"; RUNNER_REPO=$RREPO
      local page=$2
      # shellcheck disable=SC2317  # called by the sourced function
      gh() { local j=''
             while [ $# -gt 0 ]; do case $1 in --jq) j=$2; shift 2 ;; *) shift ;; esac; done
             printf '%s' "$page" | command jq -r "$j"; }
      "$RUNNER_FN" "$TREPO" 194 "$HEAD_TIME" '' 2>&1 >/dev/null ) | grep -c "$3"
}

# The locator called with no bound at all. An absent bound is not "no
# bound": with `$since` empty every comparison admits every run, so the
# function degrades into the unbounded shape the head-bound control
# proves dangerous. The one call site shape-checks the value first, so
# this is unreachable from there; the drill sources the function
# standalone, which is the point of its being extractable.
nobound() { # $1 function file
    ( # shellcheck disable=SC1090,SC2034  # the function reads RUNNER_REPO
      . "$1"; RUNNER_REPO=$RREPO
      local p1 out rc=0
      p1=$(rpage "$D_PREV_HEAD")
      # shellcheck disable=SC2317  # called by the sourced function
      gh() { local j=''
             while [ $# -gt 0 ]; do case $1 in --jq) j=$2; shift 2 ;; *) shift ;; esac; done
             printf '%s' "$p1" | command jq -r "$j"; }
      out=$("$RUNNER_FN" "$TREPO" 194 '' '' 2>/dev/null) || rc=$?
      [ "$rc" -eq 0 ] || { printf 'error(%s)' "$rc"; return; }
      printf '%s' "$out" | tr '\037' '|' )
}
# The function's body without its comment lines: the comment names
# `display_title` where it says why that field is not read, so a grep
# for the field over the whole extraction cannot tell the prose from the
# code. `$2` is the file to extract from, defaulting to SKILL.md - the
# controls pass a mutated copy, so an assertion and the control that
# proves it is a reading take the same route into the source rather
# than two.
code_of() { extract "${2:-$SKILL}" "$1" | grep -v '^ *#'; }

# Headlines as measured on hyuk-hur/dev-dotfiles, 2026-10-01 (#168 and the
# aggregate's own format_summary / format_prepare_failure_summary, v1.11.2).
H_NO_ISSUES='**Result: [OK] Approved | posted as comment (auto-approve off) | 3/3 reviewers** -- 3/3 LLM responses -- no issues'
H_MINOR='**Result: [OK] Approved | posted as comment (auto-approve off) | 3/3 reviewers** -- 3/3 LLM responses -- minor/suggestion only'
H_MAJOR='**Result: [!] Approved with 1 unreviewed major issue(s) | posted as comment (auto-approve off) | 3/3 reviewers** -- 3/3 LLM responses -- 1 major issue(s) (no consensus, review recommended)'
# The review-surface headlines, as measured 2026-10-02 on ignite-corp/ai-dev-cab
# #515, ai-dev-qa-partner #1195 and ai-dev-infra-common #876 (#174): with
# ALLOW_AUTO_APPROVE on there is no `posted as comment` segment.
H_REVIEW_NO_ISSUES='**Result: [OK] Approved | 3/3 reviewers** -- 3/3 LLM responses -- no issues'
H_REVIEW_MINOR='**Result: [OK] Approved | 3/3 reviewers** -- 3/3 LLM responses -- minor/suggestion only'
# A cut reviewer is spelled three ways on the headline, by which one was cut
# (LENS, ai-dev-pr-review run 4309344): the fraction is the uniform signal,
# and with a cut it sits inside a parenthetical rather than bare.
H_CUT='**Result: [!] Approved | posted as comment (auto-approve off) | not every reviewer responded (2/3 reviewers)** -- 2/3 LLM responses -- no issues (claude: failed: action_invocation_failed)'
H_CUT_CODEX='**Result: [!] Approved | posted as comment (auto-approve off) | not every reviewer responded (2/3 reviewers)** -- 2/3 LLM responses -- no issues (codex: failed (see logs))'
H_CUT_GEMINI='**Result: [!] Approved | posted as comment (auto-approve off) | not every reviewer responded (2/3 reviewers)** -- 2/3 LLM responses -- no issues (gemini: early-exit or no-output)'
# shellcheck disable=SC2016  # the backticks are the headline's own
H_DID_NOT_RUN='**Result: [X] Review did not run -- the prepare job reported `failure`**'
# The other shapes the same script writes.
H_CUT_SKIPPED='**Result: [!] Approved | comment only: not every reviewer responded (2/3 reviewers)** -- 2/3 LLM responses -- minor/suggestion only (gemini: skipped)'
H_NONE='**Result: [!] Comment Only | not every reviewer responded (0/3 reviewers)** -- 0/3 LLM responses -- all failed, manual review required'
H_QUORUM='**Result: [X] Changes Requested | 1/3 reviewers** -- 1/3 LLM responses -- manual review required'
H_CRITICAL='**Result: [X] Changes Requested | 3/3 reviewers** -- 1 critical issue(s) (claude): unchecked input'
H_TOO_LARGE='**Result: [X] Review skipped -- PR too large** -- 7000 changed lines exceeds the limit of 6000'
H_POLICY='**Result: [OK] Review skipped -- only policy-excluded files changed**'
# Not a shape the aggregate writes today: full fractions under a Result that
# does not approve. The Result check is what refuses it, and nothing else.
H_FULL_NOT_APPROVED='**Result: [X] Changes Requested | 3/3 reviewers** -- 3/3 LLM responses -- 1 critical issue(s) (claude): unchecked input'
H_ZERO='**Result: [OK] Approved | 0/0 reviewers** -- 0/0 LLM responses -- no issues'
# Nor these: the script derives both fractions from one count, so they never
# disagree today. Each isolates one equality, for the controls below.
H_REVIEWERS_SHORT='**Result: [!] Approved | not every reviewer responded (2/3 reviewers)** -- 3/3 LLM responses -- no issues'
H_LLM_SHORT='**Result: [OK] Approved | 3/3 reviewers** -- 2/3 LLM responses -- no issues'

echo "1. all fourteen core functions are in SKILL.md, once each, and load"
FN="$D/fn.sh"
extract_all() { # $1 file
    extract "$1" "$FINDING_FN"; extract "$1" "$FUNC_NAME"; extract "$1" "$MAJORS_FN"; extract "$1" "$THREAD_FN"; extract "$1" "$WINDOW_FN"; extract "$1" "$AGG_FN"; extract "$1" "$HEADLINE_FN"; extract "$1" "$COUNT_FN"; extract "$1" "$CUTOFF_FN"; extract "$1" "$SINCE_FN"; extract "$1" "$CONV_FN"; extract "$1" "$DISP_FN"; extract "$1" "$KNOWN_FN"; extract "$1" "$RUNNER_FN"
}
extract_all "$SKILL" > "$FN"
for f in "$FINDING_FN" "$FUNC_NAME" "$MAJORS_FN" "$THREAD_FN" "$WINDOW_FN" "$AGG_FN" "$HEADLINE_FN" "$COUNT_FN" "$CUTOFF_FN" "$SINCE_FN" "$CONV_FN" "$DISP_FN" "$KNOWN_FN" "$RUNNER_FN"; do
    chk "SKILL.md defines $f exactly once" "$(grep -c "^$f() {\$" "$SKILL")" 1
done
chk "the extraction is non-empty" "$([ -s "$FN" ] && echo yes || echo no)" yes
chk "the extraction holds fourteen closing braces, one per function" "$(grep -c '^}$' "$FN")" 14
# The truncation guard, one assertion per function: each must end at its
# own `return` or tally line followed by the brace -- the shape a truncation
# cannot have. Nothing else here can see one (see extract above).
chk "the key function's last two lines are its jq call and its brace" \
    "$(extract "$SKILL" "$FINDING_FN" | tail -n 2 | tr '\n' '|' | cut -c1-8)" '  jq -nc'
chk "the thread function's last two lines are its return and its brace" \
    "$(extract "$SKILL" "$THREAD_FN" | tail -n 2 | tr '\n' '|')" "  return 1|}|"
chk "the window function's last two lines are its printf and its brace" \
    "$(extract "$SKILL" "$WINDOW_FN" | tail -n 2 | tr '\n' '|' | cut -c1-9)" "  printf "
chk "the roster function's last two lines are its return and its brace" \
    "$(extract "$SKILL" "$FUNC_NAME" | tail -n 2 | tr '\n' '|')" "  return 0|}|"
chk "the aggregate function's last two lines are its newest-wins tail and its brace" \
    "$(extract "$SKILL" "$AGG_FN" | tail -n 2 | tr '\n' '|')" "     | sort_by(.created_at) | last // empty'|}|"
chk "the headline function's last two lines are its jq and its brace" \
    "$(extract "$SKILL" "$HEADLINE_FN" | tail -n 2 | tr '\n' '|' | cut -c1-8)" "  jq -r "
# shellcheck disable=SC2016  # matched literally: the echo as SKILL.md spells it
chk "the count function's last two lines are its echo and its brace" \
    "$(extract "$SKILL" "$COUNT_FN" | tail -n 2 | tr '\n' '|' | cut -c1-24)" '  echo "(a) reviews: $se'
# shellcheck disable=SC2016  # matched literally: the tally as SKILL.md spells it
chk "the majors function's last two lines are its tally and its brace" \
    "$(extract "$SKILL" "$MAJORS_FN" | tail -n 2 | tr '\n' '|')" '  [ "$bad" -eq 0 ]|}|'
# shellcheck disable=SC2016  # matched literally: the return as SKILL.md spells it
chk "the cutoff function's last two lines are its return and its brace" \
    "$(extract "$SKILL" "$CUTOFF_FN" | tail -n 2 | tr '\n' '|')" '  return $rc|}|'
chk "the window-start function's last two lines are its failure tail and its brace" \
    "$(extract "$SKILL" "$SINCE_FN" | tail -n 2 | tr '\n' '|' | cut -c1-17)" '    || { echo "ER'
chk "the convergence function's last two lines are its esac and its brace" \
    "$(extract "$SKILL" "$CONV_FN" | tail -n 2 | tr '\n' '|')" "  esac|}|"
# shellcheck disable=SC2016  # matched literally: the return as SKILL.md spells it
chk "the dispositions function's last two lines are its return and its brace" \
    "$(extract "$SKILL" "$DISP_FN" | tail -n 2 | tr '\n' '|')" '  return $rc|}|'
chk "the name function's last two lines are its did-not-run echo and its brace" \
    "$(extract "$SKILL" "$KNOWN_FN" | tail -n 2 | tr '\n' '|' | cut -c1-14)" '  echo "(a) no'
chk "the locator's last two lines are its closing fi and its brace" \
    "$(extract "$SKILL" "$RUNNER_FN" | tail -n 2 | tr '\n' '|')" "  fi|}|"
# shellcheck disable=SC1090
chk "sourcing it defines the roster function" "$(. "$FN"; type -t "$FUNC_NAME")" function
# shellcheck disable=SC1090
chk "sourcing it defines the majors function" "$(. "$FN"; type -t "$MAJORS_FN")" function
# shellcheck disable=SC1090
chk "sourcing it defines the finding key function" "$(. "$FN"; type -t "$FINDING_FN")" function
# shellcheck disable=SC1090
chk "sourcing it defines the thread function" "$(. "$FN"; type -t "$THREAD_FN")" function
# shellcheck disable=SC1090
chk "sourcing it defines the window function" "$(. "$FN"; type -t "$WINDOW_FN")" function
# shellcheck disable=SC1090
chk "sourcing it defines the aggregate function" "$(. "$FN"; type -t "$AGG_FN")" function
# shellcheck disable=SC1090
chk "sourcing it defines the headline function" "$(. "$FN"; type -t "$HEADLINE_FN")" function
# shellcheck disable=SC1090
chk "sourcing it defines the count function" "$(. "$FN"; type -t "$COUNT_FN")" function
# shellcheck disable=SC1090
chk "sourcing it defines the cutoff function" "$(. "$FN"; type -t "$CUTOFF_FN")" function
# shellcheck disable=SC1090
chk "sourcing it defines the window-start function" "$(. "$FN"; type -t "$SINCE_FN")" function
# shellcheck disable=SC1090
chk "sourcing it defines the convergence function" "$(. "$FN"; type -t "$CONV_FN")" function
# shellcheck disable=SC1090
chk "sourcing it defines the dispositions function" "$(. "$FN"; type -t "$DISP_FN")" function
# shellcheck disable=SC1090
chk "sourcing it defines the name function" "$(. "$FN"; type -t "$KNOWN_FN")" function
# shellcheck disable=SC1090
chk "sourcing it defines the locator" "$(. "$FN"; type -t "$RUNNER_FN")" function
# The gate block is bash; the functions must be clean on their own, outside it.
if command -v shellcheck > /dev/null 2>&1; then
    chk "shellcheck passes on the extracted functions" \
        "$(shellcheck -s bash "$FN" > /dev/null 2>&1 && echo clean || echo dirty)" clean
else
    printf '  SKIP  shellcheck not on PATH - the extracted functions were not linted\n'
    skipped=$((skipped + 1))
fi

echo "2. a full roster with Result Approved passes"
chk "3/3 reviewers, 3/3 LLM responses, no issues" "$(verdict "$FN" "$H_NO_ISSUES")" pass
chk "3/3 reviewers, 3/3 LLM responses, minor/suggestion only" "$(verdict "$FN" "$H_MINOR")" pass
chk "the review-surface headline, no auto-approve segment, no issues" "$(verdict "$FN" "$H_REVIEW_NO_ISSUES")" pass
chk "the review-surface headline, minor/suggestion only" "$(verdict "$FN" "$H_REVIEW_MINOR")" pass
# shellcheck disable=SC1090,SC2034  # the function reads EXPECTED_REVIEWERS
out=$(. "$FN"; EXPECTED_REVIEWERS=3; "$FUNC_NAME" "$H_NO_ISSUES")
chk "it prints the fractions it parsed" \
    "$(printf '%s\n' "$out" | grep -c 'reviewers 3/3, LLM responses 3/3')" 1

echo "3. unreviewed major issues pass the roster and are handed to (c)"
chk "Approved with 1 unreviewed major issue(s), 3/3" "$(verdict "$FN" "$H_MAJOR")" pass
# shellcheck disable=SC1090,SC2034  # the function reads EXPECTED_REVIEWERS
chk "agg_majors carries the count for (c)" "$(. "$FN"; EXPECTED_REVIEWERS=3; "$FUNC_NAME" "$H_MAJOR" > /dev/null; echo "${agg_majors:-unset}")" 1
# shellcheck disable=SC1090,SC2034  # the function reads EXPECTED_REVIEWERS
chk "agg_majors is 0 when the headline names none" "$(. "$FN"; EXPECTED_REVIEWERS=3; "$FUNC_NAME" "$H_NO_ISSUES" > /dev/null; echo "${agg_majors:-unset}")" 0

echo "4. everything a green job cannot vouch for is refused"
chk "2/3 reviewers, claude cut at the step cap (AT-2511)" "$(verdict "$FN" "$H_CUT")" block
chk "2/3 reviewers, codex cut: failed (see logs)" "$(verdict "$FN" "$H_CUT_CODEX")" block
chk "2/3 reviewers, gemini cut: early-exit or no-output" "$(verdict "$FN" "$H_CUT_GEMINI")" block
# shellcheck disable=SC1090,SC2034  # the function reads EXPECTED_REVIEWERS
out=$(. "$FN"; EXPECTED_REVIEWERS=3; "$FUNC_NAME" "$H_CUT")
chk "the fraction is read from inside the parenthetical, not refused for want of one" \
    "$(printf '%s\n' "$out" | grep -c 'reviewers 2/3, LLM responses 2/3')" 1
chk "2/3 reviewers, reviewer skipped" "$(verdict "$FN" "$H_CUT_SKIPPED")" block
chk "0/3 reviewers, Comment Only" "$(verdict "$FN" "$H_NONE")" block
chk "1/3 reviewers, Changes Requested" "$(verdict "$FN" "$H_QUORUM")" block
chk "Changes Requested on a critical, no LLM fraction" "$(verdict "$FN" "$H_CRITICAL")" block
chk "Changes Requested with full fractions (Result word alone refuses)" "$(verdict "$FN" "$H_FULL_NOT_APPROVED")" block
chk "[X] Review did not run" "$(verdict "$FN" "$H_DID_NOT_RUN")" block
chk "[X] Review skipped -- PR too large" "$(verdict "$FN" "$H_TOO_LARGE")" block
chk "[OK] Review skipped -- only policy-excluded files (green, no reviewer ran)" "$(verdict "$FN" "$H_POLICY")" block
chk "0/0 reviewers is not a full roster" "$(verdict "$FN" "$H_ZERO")" block
chk "2/3 reviewers alone refuses" "$(verdict "$FN" "$H_REVIEWERS_SHORT")" block
chk "2/3 LLM responses alone refuses" "$(verdict "$FN" "$H_LLM_SHORT")" block
chk "an empty headline" "$(verdict "$FN" "")" block
chk "a headline with the Result elsewhere than the start" "$(verdict "$FN" "quoting: $H_NO_ISSUES")" block

echo "5. a full fraction over the wrong denominator is refused"
H_TWO='**Result: [OK] Approved | 2/2 reviewers** -- 2/2 LLM responses -- no issues'
H_ONE='**Result: [OK] Approved | 1/1 reviewers** -- 1/1 LLM responses -- no issues'
H_LLM_DENOM='**Result: [OK] Approved | 3/3 reviewers** -- 2/2 LLM responses -- no issues'
chk "2/2 against 3 configured" "$(verdict "$FN" "$H_TWO")" block
chk "1/1 against 3 configured" "$(verdict "$FN" "$H_ONE")" block
chk "3/3 reviewers but 2/2 LLM responses against 3" "$(verdict "$FN" "$H_LLM_DENOM")" block
chk "3/3 against 3 configured" "$(verdict "$FN" "$H_NO_ISSUES")" pass
EXPECTED=2
chk "2/2 against 2 configured passes, so it is the comparison and not the digit" "$(verdict "$FN" "$H_TWO")" pass
EXPECTED='unset'
chk "EXPECTED_REVIEWERS unset refuses a full 3/3" "$(verdict "$FN" "$H_NO_ISSUES")" block
EXPECTED='<n>'
chk "EXPECTED_REVIEWERS left as the placeholder refuses a full 3/3" "$(verdict "$FN" "$H_NO_ISSUES")" block
EXPECTED=0
chk "EXPECTED_REVIEWERS=0 refuses" "$(verdict "$FN" "$H_ZERO")" block
EXPECTED=3

echo "5b. (c): each unreviewed major needs a thread at its path:line or a later quoted reply"
# shellcheck disable=SC2016  # the backticks are the aggregate's own
BODY_ONE='<!-- multi-llm-review -->
## [bot] Multi-LLM Review Summary

**Result: [!] Approved with 1 unreviewed major issue(s) | 3/3 reviewers** -- 3/3 LLM responses -- 1 major issue(s)

### Codex -- 1 issue(s)
- + **major** `claude/skills/pr-response-cycle/SKILL.md:659` -- the check only prints a warning
  > ? Make this condition fail the gate.
- - **minor** `bin/gate-headline-drill:12` -- a minor that is not counted here'
BODY_TWO="$BODY_ONE
- + **major** -- a major with no file at all, keyed by its description"
# shellcheck disable=SC2016  # the backticks are the aggregate's own
BODY_COLON='**Result: [!] Approved with 1 unreviewed major issue(s) | 3/3 reviewers** -- 3/3 LLM responses -- 1 major issue(s)
- + **major** `secrets/host:prod/env.sh:7` -- a path with a colon in it'
# shellcheck disable=SC2016  # the backticks are the aggregate's own
BODY_NOLINE='**Result: [!] Approved with 1 unreviewed major issue(s) | 3/3 reviewers** -- 3/3 LLM responses -- 1 major issue(s)
- + **major** `bin/gate-headline-drill` -- a file-level finding with no line'
P='claude/skills/pr-response-cycle/SKILL.md'
LOC=$(thr "$P" 659 replied 'the check only prints a warning')
LOC_OTHER_LINE=$(thr "$P" 12 replied 'something else entirely, three sections up')
LOC_SILENT=$(thr "$P" 659 unreplied 'the check only prints a warning')
# The producer's first comment on a finding (post_inline_comments.py
# build_comments): `<icon> **<severity>** (<reviewer>): <description>`,
# then, two lines down, the suggestion -- which thread_data does not read.
LOC_NEAR_SAME=$(thr "$P" 662 replied '+ **major** (codex): the check only prints a warning')
LOC_NEAR_OTHER=$(thr "$P" 662 replied '+ **major** (codex): a different finding three lines away about the merge block literal')
# Round 10 of #194 (codex): the match was `*"$desc"*`, so a same-path
# thread whose first comment mentions the description in prose answered it.
LOC_PROSE=$(thr "$P" 662 replied 'this is not about the check only prints a warning in the other branch')
LOC_LONGER=$(thr "$P" 662 replied '+ **major** (codex): the check only prints a warning and returns success')
LOC_MINOR=$(thr "$P" 662 replied '- **minor** (codex): the check only prints a warning')
LOC_OTHER_REVIEWER=$(thr "$P" 662 replied '+ **major** (gemini): the check only prints a warning')
# Round 10 of #194 (codex): a file-level thread has no line, so its lines
# field is empty, and a tab-separated read collapsed it -- `replied` landed
# in the lines field, the body in the reply field, and the reply guard
# skipped the row.
LOC_NOLINE_SAME=$(thr "$P" '' replied '+ **major** (codex): the check only prints a warning')
LOC_NOLINE_OTHER=$(thr "$P" '' replied 'nothing in common')
# shellcheck disable=SC2016  # the backticks are the aggregate's own
QUOTE_PATH='> - + **major** `claude/skills/pr-response-cycle/SKILL.md:659` -- the check only prints a warning
Fixed in the fixup.'
QUOTE_DESC='> a major with no file at all, keyed by its description
By design.'
chk "a major with a replied thread at its path:line" "$(majors "$FN" "$BODY_ONE" 1 "$LOC" '')" pass
chk "an unrelated thread on the same path, different line, does not answer it" "$(majors "$FN" "$BODY_ONE" 1 "$LOC_OTHER_LINE" '')" block
chk "the same-line thread among others passes" "$(majors "$FN" "$BODY_ONE" 1 "$LOC_OTHER_LINE
$LOC" '')" pass
chk "a thread at the exact line but resolved with no reply does not answer it (silent close)" "$(majors "$FN" "$BODY_ONE" 1 "$LOC_SILENT" '')" block
chk "a thread 3 lines away whose first comment is the producer's rendering of the finding answers it" "$(majors "$FN" "$BODY_ONE" 1 "$LOC_NEAR_SAME" '')" pass
chk "a thread 3 lines away about something else does not" "$(majors "$FN" "$BODY_ONE" 1 "$LOC_NEAR_OTHER" '')" block
chk "a same-path thread whose first comment mentions the description in prose does not answer it (#194 round 10)" "$(majors "$FN" "$BODY_ONE" 1 "$LOC_PROSE" '')" block
chk "the description is matched whole to the end of the line: the rendering with words appended does not answer it" "$(majors "$FN" "$BODY_ONE" 1 "$LOC_LONGER" '')" block
chk "the rendering is the major's own: the same description posted as a minor does not answer it" "$(majors "$FN" "$BODY_ONE" 1 "$LOC_MINOR" '')" block
chk "the reviewer is not bound: another reviewer's rendering of the same finding answers it" "$(majors "$FN" "$BODY_ONE" 1 "$LOC_OTHER_REVIEWER" '')" pass
# Two same-path majors sharing a 60-char prefix: one thread carrying one twin's whole
# description answers that twin alone.
# shellcheck disable=SC2016  # the backticks are the aggregate's own
BODY_TWINS='**Result: [!] Approved with 2 unreviewed major issue(s) | 3/3 reviewers** -- 3/3 LLM responses -- 2 major issue(s)
- + **major** `bin/x.sh:10` -- Missing error handling on the retry path when the socket closes early and the
- + **major** `bin/x.sh:40` -- Missing error handling on the retry path when the socket closes early but the'
T_TWIN_A=$(thr bin/x.sh 25 replied '+ **major** (codex): Missing error handling on the retry path when the socket closes early and the')
T_TWIN_B=$(thr bin/x.sh 55 replied '+ **major** (codex): Missing error handling on the retry path when the socket closes early but the')
chk "same-path twins: one thread carrying twin A's whole description leaves twin B blocked" "$(majors "$FN" "$BODY_TWINS" 2 "$T_TWIN_A" '')" block
chk "same-path twins: a thread per twin passes" "$(majors "$FN" "$BODY_TWINS" 2 "$T_TWIN_A
$T_TWIN_B" '')" pass
# shellcheck disable=SC1090
out=$(. "$FN"; "$MAJORS_FN" "$BODY_TWINS" 2 "$T_TWIN_A" '')
chk "same-path twins: the output names twin A as answered and twin B as blocked" \
    "$(printf '%s\n' "$out" | grep -c 'answered by a replied thread.*bin/x.sh:10.*and the$\|BLOCK: major.*bin/x.sh:40')" 2
chk "a thread on the path with no usable line and no matching body does not answer it" "$(majors "$FN" "$BODY_ONE" 1 "$LOC_NOLINE_OTHER" '')" block
chk "a replied thread on the path with no usable line whose first comment is the finding answers it (#194 round 10)" "$(majors "$FN" "$BODY_ONE" 1 "$LOC_NOLINE_SAME" '')" pass
chk "the same thread unreplied does not: the reply flag is read from its own field, not the shifted lines field" "$(majors "$FN" "$BODY_ONE" 1 "$(thr "$P" '' unreplied '+ **major** (codex): the check only prints a warning')" '')" block
chk "a major with a later reply quoting its path:line" "$(majors "$FN" "$BODY_ONE" 1 '' "$QUOTE_PATH")" pass
chk "a major with neither" "$(majors "$FN" "$BODY_ONE" 1 '' '')" block
chk "a thread on a different path does not answer it" "$(majors "$FN" "$BODY_ONE" 1 "$(thr bin/gate-headline-drill 659 replied 'the check only prints a warning')" '')" block
chk "a reply that does not quote (no leading >) does not answer it" "$(majors "$FN" "$BODY_ONE" 1 '' 'Fixed: SKILL.md:659')" block
# shellcheck disable=SC2016  # the backticks are the aggregate's own
chk "a quote of an unrelated finding that merely contains the path as text does not answer it" \
    "$(majors "$FN" "$BODY_ONE" 1 '' '> - + **major** `bin/x.sh:3` -- duplicates logic from claude/skills/pr-response-cycle/SKILL.md:659 instead of sourcing it')" block
chk "a quote carrying the key without its backticks does not answer it" \
    "$(majors "$FN" "$BODY_ONE" 1 '' '> claude/skills/pr-response-cycle/SKILL.md:659 -- the check only prints a warning')" block
# shellcheck disable=SC2016  # the backticks are the aggregate's own
chk "a quote carrying only the backticked key does not answer it (lens re-verdict)" "$(majors "$FN" "$BODY_ONE" 1 '' '> `claude/skills/pr-response-cycle/SKILL.md:659`')" block
# lens C1, verbatim: item 2's prose carries item 1's backticked key; only item 2 is quoted.
# shellcheck disable=SC2016  # the backticks are the aggregate's own
BODY_C1='**Result: [!] Approved with 2 unreviewed major issue(s) | 3/3 reviewers** -- 3/3 LLM responses -- 2 major issue(s)
- + **major** `bin/foo.sh:12` -- unchecked input
- + **major** `bin/bar.sh:3` -- duplicates the logic at `bin/foo.sh:12` instead of sourcing it'
# shellcheck disable=SC2016  # the backticks are the aggregate's own
Q_C1='> - + **major** `bin/bar.sh:3` -- duplicates the logic at `bin/foo.sh:12` instead of sourcing it'
chk "lens C1: quoting item 2 whose prose carries item 1's backticked key leaves item 1 blocked" "$(majors "$FN" "$BODY_C1" 2 '' "$Q_C1")" block
# shellcheck disable=SC2016  # the backticks are the aggregate's own
chk "lens C1: both items quoted whole pass" "$(majors "$FN" "$BODY_C1" 2 '' "$Q_C1
> - + **major** \`bin/foo.sh:12\` -- unchecked input")" pass
# Strict-prefix twins share their location too (PR #194 R23).
# shellcheck disable=SC2016  # the backticks are the aggregate's own
BODY_PREFIX='- + **major** `x.py:3` -- unchecked input
- + **major** `x.py:3` -- unchecked input on the retry path'
# shellcheck disable=SC2016
Q_PREFIX='> - + **major** `x.py:3` -- unchecked input on the retry path'
chk "quoting only the longer same-location major leaves its strict prefix blocked" \
    "$(majors "$FN" "$BODY_PREFIX" 2 '' "$Q_PREFIX")" block
# shellcheck disable=SC2016
chk "both strict-prefix majors quoted exactly pass" \
    "$(majors "$FN" "$BODY_PREFIX" 2 '' "$Q_PREFIX
> - + **major** \`x.py:3\` -- unchecked input")" pass
# lens B1-B4: the quote styles a reader actually produces, all carrying the whole line.
# shellcheck disable=SC2016  # the backticks are the aggregate's own
chk "B1: the line quoted with its bullet" "$(majors "$FN" "$BODY_ONE" 1 '' '> - + **major** `claude/skills/pr-response-cycle/SKILL.md:659` -- the check only prints a warning')" pass
# shellcheck disable=SC2016  # the backticks are the aggregate's own
chk "B2: the bullet stripped" "$(majors "$FN" "$BODY_ONE" 1 '' '> **major** `claude/skills/pr-response-cycle/SKILL.md:659` -- the check only prints a warning')" pass
# shellcheck disable=SC2016  # the backticks are the aggregate's own
chk "B3: re-indented" "$(majors "$FN" "$BODY_ONE" 1 '' '>     - + **major** `claude/skills/pr-response-cycle/SKILL.md:659` -- the check only prints a warning')" pass
# shellcheck disable=SC2016  # the backticks are the aggregate's own
chk "B4: nested under a second >" "$(majors "$FN" "$BODY_ONE" 1 '' '> > - + **major** `claude/skills/pr-response-cycle/SKILL.md:659` -- the check only prints a warning')" pass
chk "a path-less major is NOT answered by a bare quote of its description (lens MAJOR 1)" "$(majors "$FN" "$BODY_TWO" 2 "$LOC" "$QUOTE_DESC")" block
chk "a path-less major is answered by a verbatim quote of its whole rendered line" "$(majors "$FN" "$BODY_TWO" 2 "$LOC" '> - + **major** -- a major with no file at all, keyed by its description
By design.')" pass
# lens's two reproductions, verbatim.
BODY_LENS1='**Result: [!] Approved with 1 unreviewed major issue(s) | 3/3 reviewers** -- 3/3 LLM responses -- 1 major issue(s)
- + **major** -- Missing error handling.'
# shellcheck disable=SC2016  # the backticks are the aggregate's own
chk "lens 1: a quote of a different, path-bearing item whose description repeats the text blocks" \
    "$(majors "$FN" "$BODY_LENS1" 1 '' '> - + **major** `other/file.py:9` -- Missing error handling. in the other module')" block
chk "lens 1: the verbatim full line passes" "$(majors "$FN" "$BODY_LENS1" 1 '' '> - + **major** -- Missing error handling.')" pass
BODY_LENS2='**Result: [!] Approved with 2 unreviewed major issue(s) | 3/3 reviewers** -- 3/3 LLM responses -- 2 major issue(s)
- + **major** -- Missing error handling on the retry path when the socket closes early and the
- + **major** -- Missing error handling on the retry path when the socket closes early but the'
chk "lens 2: twins sharing a 60-char prefix are not both answered by one prefix quote" \
    "$(majors "$FN" "$BODY_LENS2" 2 '' '> Missing error handling on the retry path when the socket closes')" block
chk "lens 2: quoting one twin verbatim leaves the other unanswered" \
    "$(majors "$FN" "$BODY_LENS2" 2 '' '> - + **major** -- Missing error handling on the retry path when the socket closes early and the')" block
chk "lens 2: both twins quoted verbatim pass" \
    "$(majors "$FN" "$BODY_LENS2" 2 '' '> - + **major** -- Missing error handling on the retry path when the socket closes early and the
> - + **major** -- Missing error handling on the retry path when the socket closes early but the')" pass
chk "a path-less major with no quote blocks even when the other is answered" "$(majors "$FN" "$BODY_TWO" 2 "$LOC" '')" block
chk "the headline counts 2 but the body lists 1" "$(majors "$FN" "$BODY_ONE" 2 "$LOC" '')" block
chk "a minor line is not counted as a major" "$(majors "$FN" "$BODY_ONE" 1 "$LOC" '')" pass
chk "a path with a colon: the thread at its real path:line answers it" "$(majors "$FN" "$BODY_COLON" 1 "$(thr secrets/host:prod/env.sh 7 replied x)" '')" pass
chk "a path with a colon: a thread at the truncated path does not" "$(majors "$FN" "$BODY_COLON" 1 "$(thr secrets/host 7 replied x)" '')" block
chk "a path with a colon: a thread at the same path, other line, does not" "$(majors "$FN" "$BODY_COLON" 1 "$(thr secrets/host:prod/env.sh 12 replied x)" '')" block
chk "a line-less major is not answered by a thread on its path about something else" "$(majors "$FN" "$BODY_NOLINE" 1 "$(thr bin/gate-headline-drill 12 replied 'unrelated')" '')" block
chk "a line-less major is answered by a replied thread on its path whose first comment is the producer's rendering of it" "$(majors "$FN" "$BODY_NOLINE" 1 "$(thr bin/gate-headline-drill 12 replied '+ **major** (codex): a file-level finding with no line')" '')" pass
chk "a line-less major is not answered by a thread on its path whose first comment carries its description as prose" "$(majors "$FN" "$BODY_NOLINE" 1 "$(thr bin/gate-headline-drill 12 replied 'a file-level finding with no line, as the reviewer put it')" '')" block
# shellcheck disable=SC2016  # the backticks are the quoted item's own
chk "a line-less major is not answered by a quote of its backticked path alone" "$(majors "$FN" "$BODY_NOLINE" 1 '' '> `bin/gate-headline-drill` -- a file-level finding
Fixed.')" block
# shellcheck disable=SC2016  # the backticks are the quoted item's own
chk "a line-less major is answered by a quote of its whole line" "$(majors "$FN" "$BODY_NOLINE" 1 '' '> - + **major** `bin/gate-headline-drill` -- a file-level finding with no line
Fixed.')" pass
# shellcheck disable=SC1090
out=$(. "$FN"; "$MAJORS_FN" "$BODY_ONE" 1 '' '')
chk "a block names the item" "$(printf '%s\n' "$out" | grep -c 'BLOCK: major with neither.*SKILL.md:659')" 1

echo "5c. the aggregate window needs both edges"
chk "two timestamps are accepted" "$(window "$FN" 2026-10-01T10:50:12Z 2026-10-01T10:58:00Z)" ok
chk "a null updatedAt (jq renders it empty) is refused, not opened" "$(window "$FN" 2026-10-01T10:50:12Z '')" refused
chk "a literal null updatedAt is refused" "$(window "$FN" 2026-10-01T10:50:12Z null)" refused
chk "a missing createdAt is refused" "$(window "$FN" '' 2026-10-01T10:58:00Z)" refused
# The reason the guard exists: a null compares as later than any timestamp.
chk "control: without the guard, jq's string order puts every comment inside a null-ended window" \
    "$(jq -rn '"2099-12-31T00:00:00Z" <= "null"')" true

echo "5d. kind (i)'s aggregate is read on either surface: a comment inside the run's window, a review on the head APPROVED or CHANGES_REQUESTED"
# The three shapes as measured 2026-10-02 (#174): ids, logins, user types,
# states, SHAs and timestamps verbatim; bodies cut to the marker and the
# headline. hyuk-hur/dev-dotfiles #168 posts a comment (ALLOW_AUTO_APPROVE
# off); ai-dev-qa-partner #1195 a review by github-actions[bot] (on, no App
# key); ai-dev-cab #515 and ai-dev-infra-common #876 a review by
# ignite-ai-review-approver[bot] (on, App). On each review-surface PR the
# previous head's review is DISMISSED and the current head's APPROVED, and
# the inline-comment reviews carry no marker.
HEAD_QA=05838374fd19ec120261e9a8ff2db93b22ac2a2a; PREV_QA=0a7a86b3474d2dcc5a2d4d9594922d3c094b81a1
HEAD_CAB=314cf49e65acf9ccf260642d85630159d4a7a31d; PREV_CAB=efd2500fb415c552981b7fc894b3f2c137953428
HEAD_IC=b3befbb19ad3f802d38367b065fefc85d47b2553; PREV_IC=5898287a4879836dcb0ed79125ceb0c22e78daea
NEXT_HEAD=0000000000000000000000000000000000000001
C_DOT=$(cmt 5930195222 2026-10-01T11:15:37Z 'github-actions[bot]' Bot "$MARK"$'\n'"$H_NO_ISSUES")
R_QA_INLINE=$(rev 5389106666 2026-10-02T07:07:00Z 'github-actions[bot]' Bot COMMENTED "$PREV_QA" '')
R_QA_PREV=$(rev 5389112217 2026-10-02T07:07:55Z 'github-actions[bot]' Bot DISMISSED "$PREV_QA" "$MARK"$'\n'"$H_REVIEW_MINOR")
R_QA_HUMAN=$(rev 5389179475 2026-10-02T07:17:55Z hyuk-hur User COMMENTED "$HEAD_QA" '')
R_QA_HEAD=$(rev 5389221717 2026-10-02T07:22:39Z 'github-actions[bot]' Bot APPROVED "$HEAD_QA" "$MARK"$'\n'"$H_REVIEW_NO_ISSUES")
R_CAB_INLINE=$(rev 5384688430 2026-10-01T19:50:55Z 'github-actions[bot]' Bot COMMENTED "$PREV_CAB" '')
R_CAB_PREV=$(rev 5384693316 2026-10-01T19:51:21Z 'ignite-ai-review-approver[bot]' Bot DISMISSED "$PREV_CAB" "$MARK"$'\n'"$H_REVIEW_MINOR")
R_CAB_HUMAN=$(rev 5384724711 2026-10-01T19:54:02Z hyuk-hur User COMMENTED "$PREV_CAB" '')
R_CAB_HEAD=$(rev 5384773396 2026-10-01T19:58:11Z 'ignite-ai-review-approver[bot]' Bot APPROVED "$HEAD_CAB" "$MARK"$'\n'"$H_REVIEW_NO_ISSUES")
R_IC_PREV=$(rev 5390574653 2026-10-02T10:03:53Z 'ignite-ai-review-approver[bot]' Bot DISMISSED "$PREV_IC" "$MARK"$'\n'"$H_REVIEW_NO_ISSUES")
R_IC_HEAD=$(rev 5390620711 2026-10-02T10:09:06Z 'ignite-ai-review-approver[bot]' Bot APPROVED "$HEAD_IC" "$MARK"$'\n'"$H_REVIEW_NO_ISSUES")
QA=$(lst "$R_QA_INLINE" "$R_QA_PREV" "$R_QA_HUMAN" "$R_QA_HEAD")
CAB=$(lst "$R_CAB_INLINE" "$R_CAB_PREV" "$R_CAB_HUMAN" "$R_CAB_HEAD")
IC=$(lst "$R_IC_PREV" "$R_IC_HEAD")
# The run on #168: created 2026-10-01T11:10:52Z, updated 11:17:00Z -- the window the comment sits in.
W_S=2026-10-01T11:10:52Z; W_E=2026-10-01T11:17:00Z
# A window that contains nothing: the review surface must not need one.
E_S=2000-01-01T00:00:00Z; E_E=2000-01-01T00:00:01Z
chk "shape 1: an issue comment by github-actions[bot] inside the run's window (dev-dotfiles #168)" "$(agg "$FN" "$(lst "$C_DOT")" '[]' "$NEXT_HEAD" "$W_S" "$W_E")" comment:5930195222:comment
chk "shape 1: the same comment before the run's createdAt is refused (the window still binds a comment)" "$(agg "$FN" "$(lst "$C_DOT")" '[]' "$NEXT_HEAD" 2026-10-01T11:16:00Z "$W_E")" none
chk "shape 1: the same comment after the run's updatedAt is refused" "$(agg "$FN" "$(lst "$C_DOT")" '[]' "$NEXT_HEAD" "$W_S" 2026-10-01T11:15:00Z)" none
chk "shape 2: a review by github-actions[bot] on the head, APPROVED (ai-dev-qa-partner #1195)" "$(agg "$FN" '[]' "$QA" "$HEAD_QA" "$E_S" "$E_E")" review:5389221717:APPROVED
chk "shape 3: a review by ignite-ai-review-approver[bot] on the head, APPROVED (ai-dev-cab #515)" "$(agg "$FN" '[]' "$CAB" "$HEAD_CAB" "$E_S" "$E_E")" review:5384773396:APPROVED
chk "shape 3: the same on ai-dev-infra-common #876" "$(agg "$FN" '[]' "$IC" "$HEAD_IC" "$E_S" "$E_E")" review:5390620711:APPROVED
chk "a review is bound by commit, not by the window: the window above contains nothing" "$(agg "$FN" '[]' "$IC" "$HEAD_IC" 2026-10-02T10:09:07Z 2026-10-02T10:09:08Z)" review:5390620711:APPROVED
chk "a DISMISSED review on the head is refused: the previous head's round, mid-round on #1195" "$(agg "$FN" '[]' "$QA" "$PREV_QA" "$E_S" "$E_E")" none
chk "a DISMISSED review on the head is refused: the same on #515" "$(agg "$FN" '[]' "$CAB" "$PREV_CAB" "$E_S" "$E_E")" none
chk "an APPROVED review by the approver on a previous head is refused after a push" "$(agg "$FN" '[]' "$CAB" "$NEXT_HEAD" "$E_S" "$E_E")" none
chk "an APPROVED review by github-actions[bot] on a previous head is refused after a push" "$(agg "$FN" '[]' "$QA" "$NEXT_HEAD" "$E_S" "$E_E")" none
R_SPOOF_USER=$(rev 1 2026-10-02T10:09:06Z hyuk-hur User APPROVED "$HEAD_IC" "$MARK"$'\n'"$H_REVIEW_NO_ISSUES")
R_SPOOF_BOT=$(rev 2 2026-10-02T10:09:06Z 'someone-else[bot]' Bot APPROVED "$HEAD_IC" "$MARK"$'\n'"$H_REVIEW_NO_ISSUES")
R_SPOOF_TYPE=$(rev 3 2026-10-02T10:09:06Z 'ignite-ai-review-approver[bot]' User APPROVED "$HEAD_IC" "$MARK"$'\n'"$H_REVIEW_NO_ISSUES")
R_NO_MARK=$(rev 4 2026-10-02T10:09:06Z 'ignite-ai-review-approver[bot]' Bot APPROVED "$HEAD_IC" "$H_REVIEW_NO_ISSUES")
R_NO_TIME=$(rev 5 '' 'ignite-ai-review-approver[bot]' Bot APPROVED "$HEAD_IC" "$MARK"$'\n'"$H_REVIEW_NO_ISSUES")
chk "a marker review on the head by a User login outside AGG_AUTHORS is refused" "$(agg "$FN" '[]' "$(lst "$R_SPOOF_USER")" "$HEAD_IC" "$E_S" "$E_E")" none
chk "a marker review on the head by a Bot login outside AGG_AUTHORS is refused" "$(agg "$FN" '[]' "$(lst "$R_SPOOF_BOT")" "$HEAD_IC" "$E_S" "$E_E")" none
chk "a listed login with user.type User is refused" "$(agg "$FN" '[]' "$(lst "$R_SPOOF_TYPE")" "$HEAD_IC" "$E_S" "$E_E")" none
chk "a review on the head by a listed login without the marker is refused" "$(agg "$FN" '[]' "$(lst "$R_NO_MARK")" "$HEAD_IC" "$E_S" "$E_E")" none
chk "a marker review with no submitted_at is refused" "$(agg "$FN" '[]' "$(lst "$R_NO_TIME")" "$HEAD_IC" "$E_S" "$E_E")" none
chk "the spoofs beside the real review leave the real one selected" "$(agg "$FN" '[]' "$(lst "$R_SPOOF_USER" "$R_SPOOF_BOT" "$R_SPOOF_TYPE" "$R_NO_MARK" "$R_NO_TIME" "$R_IC_HEAD")" "$HEAD_IC" "$E_S" "$E_E")" review:5390620711:APPROVED
# post_verdict reviews only with --approve and --request-changes; its one
# COMMENTED review is post_inline_comments.py's, with an empty body. A
# COMMENTED marker review is no shape the producer posts, so it is refused
# whatever its headline -- an Approved one would otherwise pass as kind (i)
# (codex, round 18 of #194).
R_COMMENTED=$(rev 6 2026-10-02T10:09:06Z 'github-actions[bot]' Bot COMMENTED "$HEAD_IC" "$MARK"$'\n'"$H_QUORUM")
R_COMMENTED_OK=$(rev 7 2026-10-02T10:09:06Z 'ignite-ai-review-approver[bot]' Bot COMMENTED "$HEAD_IC" "$MARK"$'\n'"$H_REVIEW_NO_ISSUES")
chk "a COMMENTED marker review on the head by a listed Bot with an Approved headline is refused" "$(agg "$FN" '[]' "$(lst "$R_COMMENTED_OK")" "$HEAD_IC" "$E_S" "$E_E")" none
chk "a COMMENTED marker review on the head with a Changes Requested headline is refused the same" "$(agg "$FN" '[]' "$(lst "$R_COMMENTED")" "$HEAD_IC" "$E_S" "$E_E")" none
chk "a CHANGES_REQUESTED marker review on the head is still a candidate" "$(agg "$FN" '[]' "$(lst "$(rev 8 2026-10-02T10:09:06Z 'github-actions[bot]' Bot CHANGES_REQUESTED "$HEAD_IC" "$MARK"$'\n'"$H_QUORUM")")" "$HEAD_IC" "$E_S" "$E_E")" review:8:CHANGES_REQUESTED
# Newest across both surfaces: the #168 comment at 11:15:37Z against a review on the head a minute either side.
R_LATER=$(rev 7 2026-10-01T11:16:37Z 'github-actions[bot]' Bot APPROVED "$NEXT_HEAD" "$MARK"$'\n'"$H_REVIEW_NO_ISSUES")
R_EARLIER=$(rev 8 2026-10-01T11:14:37Z 'github-actions[bot]' Bot APPROVED "$NEXT_HEAD" "$MARK"$'\n'"$H_REVIEW_NO_ISSUES")
chk "newest across both surfaces: a review a minute after the comment wins" "$(agg "$FN" "$(lst "$C_DOT")" "$(lst "$R_LATER")" "$NEXT_HEAD" "$W_S" "$W_E")" review:7:APPROVED
chk "newest across both surfaces: a review a minute before the comment loses" "$(agg "$FN" "$(lst "$C_DOT")" "$(lst "$R_EARLIER")" "$NEXT_HEAD" "$W_S" "$W_E")" comment:5930195222:comment
chk "both lists empty: nothing" "$(agg "$FN" '[]' '[]' "$HEAD_IC" "$E_S" "$E_E")" none
# Identity is per verdict, not per repository (LENS, aggregate_reviews.py
# post_verdict at ai-dev-pr-review d554aee): request_changes never uses the
# App token, so on an App-shaped PR a CHANGES_REQUESTED review is
# github-actions[bot]'s; the App mint is continue-on-error, so an approval
# can land as github-actions[bot]'s review there too; a review-API failure
# and a timeout both fall back to an issue comment with an `Intended
# verdict` note appended after the body, differing only in the reason
# (AT-2597). The note-less comment-only control is C_DOT, shape 1 above.
R_RC_GA=$(rev 9 2026-10-02T10:09:06Z 'github-actions[bot]' Bot CHANGES_REQUESTED "$HEAD_IC" "$MARK"$'\n'"$H_CRITICAL")
chk "a CHANGES_REQUESTED review by github-actions[bot] on an App-shaped PR is selected: identity accepted" "$(agg "$FN" '[]' "$(lst "$R_IC_PREV" "$R_RC_GA")" "$HEAD_IC" "$E_S" "$E_E")" review:9:CHANGES_REQUESTED
chk "its headline is then refused by the roster on the Result word" "$(verdict "$FN" "$(headline_of "$FN" "$R_RC_GA")")" block
# shellcheck disable=SC1090,SC2034  # the function reads EXPECTED_REVIEWERS
out=$(. "$FN"; EXPECTED_REVIEWERS=3; "$FUNC_NAME" "$(headline_of "$FN" "$R_RC_GA")")
chk "the refusal names the Result, not the identity" "$(printf '%s\n' "$out" | grep -c 'Result is not Approved')" 1
R_GA_ON_APP=$(rev 10 2026-10-02T10:09:06Z 'github-actions[bot]' Bot APPROVED "$HEAD_IC" "$MARK"$'\n'"$H_REVIEW_NO_ISSUES")
chk "a github-actions[bot] APPROVED review on an App-shaped PR (mint failed) is accepted" "$(agg "$FN" '[]' "$(lst "$R_IC_PREV" "$R_GA_ON_APP")" "$HEAD_IC" "$E_S" "$E_E")" review:10:APPROVED
chk "and its headline passes the roster" "$(verdict "$FN" "$(headline_of "$FN" "$R_GA_ON_APP")")" pass
NOTE='> [!] Intended verdict: **approve** (review API failed, posted as comment)'
C_NOTED=$(cmt 11 2026-10-02T10:09:06Z 'github-actions[bot]' Bot "$MARK"$'\n'"$H_REVIEW_NO_ISSUES"$'\n\n'"$NOTE")
TIMEOUT_NOTE='> [!] Intended verdict: **approve** (review API timed out, posted as comment)'
C_TIMEOUT=$(cmt 12 2026-10-02T10:09:06Z 'github-actions[bot]' Bot "$MARK"$'\n'"$H_REVIEW_NO_ISSUES"$'\n\n'"$TIMEOUT_NOTE")
chk "the review-API-failure fallback comment, note appended, is the aggregate present" "$(agg "$FN" "$(lst "$C_NOTED")" '[]' "$HEAD_IC" 2026-10-02T10:00:00Z 2026-10-02T10:10:00Z)" comment:11:comment
chk "its headline is read with the note present" "$(headline_of "$FN" "$C_NOTED")" "$H_REVIEW_NO_ISSUES"
chk "and passes the roster" "$(verdict "$FN" "$(headline_of "$FN" "$C_NOTED")")" pass
chk "the timeout fallback comment, its note appended, is the aggregate present" "$(agg "$FN" "$(lst "$C_TIMEOUT")" '[]' "$HEAD_IC" 2026-10-02T10:00:00Z 2026-10-02T10:10:00Z)" comment:12:comment
chk "its headline is read with the timeout note present" "$(headline_of "$FN" "$C_TIMEOUT")" "$H_REVIEW_NO_ISSUES"
chk "and passes the roster" "$(verdict "$FN" "$(headline_of "$FN" "$C_TIMEOUT")")" pass
chk "the gate's functions key on no note" "$(grep -c 'Intended verdict\|Auto-approve withheld' "$FN" | sed 's/^/n=/')" "n=0"
C_NOTE_FIRST=$(cmt 13 2026-10-02T10:09:06Z 'github-actions[bot]' Bot "$NOTE"$'\n'"$MARK"$'\n'"$H_REVIEW_NO_ISSUES")
chk "a note ahead of the headline leaves the headline readable" "$(headline_of "$FN" "$C_NOTE_FIRST")" "$H_REVIEW_NO_ISSUES"
WITHHELD='> [!] Auto-approve withheld: not every configured reviewer produced a verdict; posted as comment.'
C_WITHHELD=$(cmt 14 2026-10-02T10:09:06Z 'github-actions[bot]' Bot "$MARK"$'\n'"$H_CUT_SKIPPED"$'\n\n'"$WITHHELD")
chk "the quorum-short comment with its withheld note is the aggregate present" "$(agg "$FN" "$(lst "$C_WITHHELD")" '[]' "$HEAD_IC" 2026-10-02T10:00:00Z 2026-10-02T10:10:00Z)" comment:14:comment
chk "and its 2/3 headline is refused by the roster, not by the note" "$(verdict "$FN" "$(headline_of "$FN" "$C_WITHHELD")")" block
chk "a candidate with no Result line yields an empty headline" "$(headline_of "$FN" "$(cmt 15 2026-10-02T10:09:06Z 'github-actions[bot]' Bot "$MARK"$'\n'"nothing here")")" ""
# shellcheck disable=SC1090,SC2034  # the function reads AGG_MARKER and AGG_AUTHORS
out=$( . "$FN"; AGG_MARKER=$MARK; AGG_AUTHORS='github-actions[bot] ignite-ai-review-approver[bot]'; printf '%s\n%s\n' '[]' "$CAB" | "$AGG_FN" "$HEAD_CAB" "$E_S" "$E_E" )
chk "the candidate names its surface, identity, type, state and id" \
    "$(printf '%s' "$out" | jq -r '"\(.surface) \(.author) \(.type) \(.state) \(.id)"')" "review ignite-ai-review-approver[bot] Bot APPROVED 5384773396"
chk "the candidate's body is the review's, for headline_roster to read" \
    "$(printf '%s' "$out" | jq -r '.body | split("\n") | map(select(startswith("**Result:"))) | .[0]')" "$H_REVIEW_NO_ISSUES"

echo "5e. the review list must be complete: a dropped page can promote an older approval on the same head"
# Why: the producer folds only its own CHANGES_REQUESTED marker reviews
# and never an approval (aggregate_reviews.py _minimize_stale_bot_items at
# d554aee), so a second run on one head leaves the first run's approval
# non-dismissed under its CHANGES_REQUESTED. Newest-wins picks the block;
# a list that lost the newer review picks the approval.
R_RUN1=$(rev 16 2026-10-02T11:00:00Z 'ignite-ai-review-approver[bot]' Bot APPROVED "$HEAD_IC" "$MARK"$'\n'"$H_REVIEW_NO_ISSUES")
R_RUN2=$(rev 17 2026-10-02T11:20:00Z 'github-actions[bot]' Bot CHANGES_REQUESTED "$HEAD_IC" "$MARK"$'\n'"$H_CRITICAL")
chk "two non-dismissed candidates on one head: the newer CHANGES_REQUESTED wins" "$(agg "$FN" '[]' "$(lst "$R_RUN1" "$R_RUN2")" "$HEAD_IC" "$E_S" "$E_E")" review:17:CHANGES_REQUESTED
chk "the same list minus the newer review promotes the older approval -- what a dropped page would do" "$(agg "$FN" '[]' "$(lst "$R_RUN1")" "$HEAD_IC" "$E_S" "$E_E")" review:16:APPROVED
chk "a list equal to the PR's count passes" "$(complete "$FN" "$(lst "$R_RUN1" "$R_RUN2")" 2)" ok
chk "a list one short of the PR's count is refused" "$(complete "$FN" "$(lst "$R_RUN1")" 2)" block
chk "an empty list against a count of 0 passes" "$(complete "$FN" '[]' 0)" ok
chk "an empty list against a count of 1 is refused" "$(complete "$FN" '[]' 1)" block
chk "a count that is not a number (null, the field missing) is refused" "$(complete "$FN" "$(lst "$R_RUN1")" null)" block
chk "an empty count is refused" "$(complete "$FN" "$(lst "$R_RUN1")" '')" block
# shellcheck disable=SC1090
out=$(. "$FN"; printf '%s' "$(lst "$R_RUN1")" | "$COUNT_FN" 2)
chk "the block names both numbers" "$(printf '%s\n' "$out" | grep -c 'read 1 reviews but the PR counts .2.')" 1
# Measured 2026-10-02: GraphQL reviews.totalCount equals the paginated REST length on
# ai-dev-cab #515 (4), ai-dev-qa-partner #1195 (4), ai-dev-infra-common #876 (2), dev-dotfiles #168 (6).
chk "the measured count on #515 against its four reviews passes" "$(complete "$FN" "$CAB" 4)" ok

echo "5f. the cutoff surface: 0 unresolved threads is not 0 findings"
# Measured on ignite-corp/ai-dev-pr-review#188, 2026-10-06: comment
# 6015282943 by github-actions[bot] opens with `<!-- round-cutoff-claude-r7 -->`
# and lists two items that were never threads, each opening with its
# location; comment 6015452260 at 11:39:40Z by hyuk-hur names that marker and
# quotes three items with `>` - the two cutoff items by their locations,
# `.github/scripts/aggregate_reviews.py:61` and
# `.github/scripts/tests/test_aggregate_reviews.py:44`, once each, plus one
# from the R7 verdict body - two Fixed, one By design, rewriting each item's
# text as it quotes it - and the cutoff comment was minimized OUTDATED after
# it. Ids, logins, types, timestamps and item locations verbatim; item text
# cut short. Nothing in this section carries a thread: the whole point is
# that the thread count cannot see these.
# shellcheck disable=SC2016  # the backticks are the cutoff comment's own
CUT_BODY='<!-- round-cutoff-claude-r7 -->
## [bot] claude Round Cutoff Summary (R7)

R7 convergence cutoff -- the following minor items are recommended for follow-up:

- **.github/scripts/aggregate_reviews.py:61** -- - **minor** (claude): This diff deletes the last production caller of `normalize_bot_login`, leaving it dead.

> ? Either delete `normalize_bot_login` from `github_pr_support.py` together with its test class, or give it the same treatment `BOT_LOGIN` got.
- **.github/scripts/tests/test_aggregate_reviews.py:44** -- ? **suggestion** (claude): `+from aggregate_reviews import _STALE_COMMENTS_QUERY, _STALE_REVIEWS_QUERY` is a second `from aggregate_reviews import` statement immediately below the parenthesized one.

> ? Fold the two names into the existing block.'
# shellcheck disable=SC2016  # the backticks are the disposition comment's own
DISP_BODY='Dispositions for the round-R7 items that reached this PR without an inline thread (the convergence cutoff folded them into the `round-cutoff-claude-r7` comment and the R7 verdict body). Head at the time of this comment: `01195c07dce65b5831692cada5895ef8f4100799`.

> **minor** `.github/scripts/aggregate_reviews.py:61` (claude, R7 cutoff comment) -- This diff deletes the last production caller of `normalize_bot_login`, leaving it dead ...

By design: kept inert.

> **suggestion** `.github/scripts/tests/test_aggregate_reviews.py:44` (claude, R7 cutoff comment) -- `from aggregate_reviews import _STALE_COMMENTS_QUERY, _STALE_REVIEWS_QUERY` is a second `from aggregate_reviews import` statement ... Fold the two names into the existing block.

Fixed: both names are in the single parenthesized block.

> **suggestion** `.github/scripts/review_pr_local.py:1814` (claude, R7 verdict body) -- The rewritten docstring now states the resolved value is inert ...

Fixed: the docstring extension.'
# R9 retains the measured marker and timestamp, but its item is synthetic:
# an empty list is no longer a valid producer-shaped positive fixture.
CUT_R9='<!-- round-cutoff-claude-r9 -->
## [bot] claude Round Cutoff Summary (R9)

R9 convergence cutoff -- the following minor items are recommended for follow-up:

- **c.py:9** -- - **minor** (claude): an R9 item'
C_CUTOFF=$(cmt 6015282943 2026-10-06T11:28:32Z 'github-actions[bot]' Bot "$CUT_BODY")
C_DISP=$(cmt 6015452260 2026-10-06T11:39:40Z hyuk-hur User "$DISP_BODY")
C_CUTOFF_R9=$(cmt 6026034008 2026-10-06T21:45:38Z 'github-actions[bot]' Bot "$CUT_R9")
# Every fixture below that is meant to fail on one clause alone satisfies
# the others: names the marker, or quotes both items by location, or both.
C_NAMES_NO_QUOTE=$(cmt 101 2026-10-06T11:40:00Z hyuk-hur User 'handled the round-cutoff-claude-r7 items in the fixup: .github/scripts/aggregate_reviews.py:61 kept inert, .github/scripts/tests/test_aggregate_reviews.py:44 folded')
# shellcheck disable=SC2016  # the backticks are the quoted items' own
C_QUOTE_NO_NAME=$(cmt 102 2026-10-06T11:40:00Z hyuk-hur User '> **minor** `.github/scripts/aggregate_reviews.py:61` -- dead helper
Fixed.
> **suggestion** `.github/scripts/tests/test_aggregate_reviews.py:44` -- second import
Fixed.')
# shellcheck disable=SC2016  # the backticks are the quoted items' own
C_BOT_REPLY=$(cmt 103 2026-10-06T11:40:00Z 'github-actions[bot]' Bot 'round-cutoff-claude-r7:
> **minor** `.github/scripts/aggregate_reviews.py:61` -- dead helper
> **suggestion** `.github/scripts/tests/test_aggregate_reviews.py:44` -- second import
Fixed.')
# The round-5 finding on #194 (codex): the marker named, a `>` line
# present, and neither item quoted.
C_UNRELATED_QUOTE=$(cmt 107 2026-10-06T11:40:00Z hyuk-hur User 'All round-cutoff-claude-r7 items handled.
> something unrelated
Fixed.')
# shellcheck disable=SC2016  # the backticks are the quoted item's own
C_ONE_OF_TWO=$(cmt 108 2026-10-06T11:40:00Z hyuk-hur User 'round-cutoff-claude-r7:
> **minor** `.github/scripts/aggregate_reviews.py:61` -- dead helper
By design.')
# shellcheck disable=SC2016  # the backticks are the quoted item's own
C_THE_OTHER=$(cmt 109 2026-10-06T11:41:00Z hyuk-hur User '> **suggestion** `.github/scripts/tests/test_aggregate_reviews.py:44` -- second import
Fixed.')
# shellcheck disable=SC2016  # the backticks are the quoted items' own
C_LONGER_LINE=$(cmt 110 2026-10-06T11:40:00Z hyuk-hur User 'round-cutoff-claude-r7:
> **minor** `.github/scripts/aggregate_reviews.py:610` -- dead helper
> **suggestion** `.github/scripts/tests/test_aggregate_reviews.py:44` -- second import
Fixed.')
C_EARLY_DISP=$(cmt 104 2026-10-06T11:20:00Z hyuk-hur User "$DISP_BODY")
# The prefix collision (#194 round 7): markers are `round-cutoff-<reviewer>-r<n>`,
# so r1's is a prefix of r10's, and #188 already carries r7 and r9 - a tenth
# round on any PR collides. The producer's cutoff opens at round 5 by
# default, so r1 is not a marker it writes today; the class is the same at
# r5 and r50, and the pair is the one the finding named. The reply quotes
# the r1 item by location and names only r10.
C_CUTOFF_R1=$(cmt 111 2026-10-06T11:28:32Z 'github-actions[bot]' Bot '<!-- round-cutoff-claude-r1 -->
## [bot] claude Round Cutoff Summary (R1)

R1 convergence cutoff -- the following minor items are recommended for follow-up:

- **a.py:3** -- - **minor** (claude): an r1 item')
# shellcheck disable=SC2016  # the backticks are the quoted item's own
C_NAMES_R10=$(cmt 112 2026-10-06T11:40:00Z hyuk-hur User 'round-cutoff-claude-r10:
> **minor** `a.py:3` -- an r1 item
Fixed.')
# The shape a word-boundary guard would refuse and the digit guard must not:
# a Korean particle attached to the marker with no space between - U+C758,
# the possessive, spelled as an escape because this directory is ASCII-only.
# jq's `\b` and `\w` are Unicode-aware, so `r1` plus that particle has no
# boundary after the 1. Not measured on a reply yet - every reply read on
# #176, #188 and #194 names the marker in backticks - but this account's
# replies are Korean as often as English, and a particle takes no space.
PARTICLE=$'\uc758'
# shellcheck disable=SC2016  # the backticks are the quoted item's own
C_NAMES_R1_PARTICLE=$(cmt 113 2026-10-06T11:40:00Z hyuk-hur User "round-cutoff-claude-r1${PARTICLE} items:"'
> **minor** `a.py:3` -- an r1 item
Fixed.')
# The split the other way (#194 round 10, codex): the marker named in one
# reply with nothing quoted, every item quoted in a sibling that does not
# name it. The union the reader promises answers it; until round 10 the
# marker scan asked the one reply naming the marker for a `>` line too.
C_NAMES_ONLY=$(cmt 115 2026-10-06T11:39:00Z hyuk-hur User 'the round-cutoff-claude-r7 items are dispositioned in the next comment')
# The verdict-body surface, for contrast: an aggregate comment is not a cutoff comment.
C_AGG=$(cmt 105 2026-10-06T11:28:00Z 'github-actions[bot]' Bot "$MARK"$'\n'"$H_MINOR")
W_CUT=2026-10-06T11:00:00Z
chk "the measured cutoff comment alone blocks: findings with no thread and no disposition" \
    "$(cutoff "$FN" "$(lst "$C_CUTOFF")" "$W_CUT")" block
chk "the measured disposition comment answers it" \
    "$(cutoff "$FN" "$(lst "$C_CUTOFF" "$C_DISP")" "$W_CUT")" ok
chk "a later comment that names the marker and quotes an unrelated line does not answer it (#194 round 5)" \
    "$(cutoff "$FN" "$(lst "$C_CUTOFF" "$C_UNRELATED_QUOTE")" "$W_CUT")" block
chk "a later comment that quotes one of the two items by location leaves the other unanswered" \
    "$(cutoff "$FN" "$(lst "$C_CUTOFF" "$C_ONE_OF_TWO")" "$W_CUT")" block
chk "dispositions split across two replies, one of them naming the marker, answer it together" \
    "$(cutoff "$FN" "$(lst "$C_CUTOFF" "$C_ONE_OF_TWO" "$C_THE_OTHER")" "$W_CUT")" ok
chk "the split the other way, the marker named in a reply that quotes nothing and both items quoted in a sibling that does not name it, answers it too (#194 round 10)" \
    "$(cutoff "$FN" "$(lst "$C_CUTOFF" "$C_NAMES_ONLY" "$C_QUOTE_NO_NAME")" "$W_CUT")" ok
chk "a location is matched whole: quoting line 610 does not answer line 61" \
    "$(cutoff "$FN" "$(lst "$C_CUTOFF" "$C_LONGER_LINE")" "$W_CUT")" block
chk "a later comment that names the marker and every location in prose but quotes nothing does not answer it" \
    "$(cutoff "$FN" "$(lst "$C_CUTOFF" "$C_NAMES_NO_QUOTE")" "$W_CUT")" block
chk "a later comment that quotes both items but names no marker does not answer it" \
    "$(cutoff "$FN" "$(lst "$C_CUTOFF" "$C_QUOTE_NO_NAME")" "$W_CUT")" block
chk "a Bot's own quoted reply does not answer it" \
    "$(cutoff "$FN" "$(lst "$C_CUTOFF" "$C_BOT_REPLY")" "$W_CUT")" block
chk "a disposition posted before the cutoff comment does not answer it" \
    "$(cutoff "$FN" "$(lst "$C_CUTOFF" "$C_EARLY_DISP")" "$W_CUT")" block
chk "the marker is matched whole: a reply naming round-cutoff-claude-r10 does not answer round-cutoff-claude-r1 (#194 round 7)" \
    "$(cutoff "$FN" "$(lst "$C_CUTOFF_R1" "$C_NAMES_R10")" "$W_CUT")" block
chk "a reply naming the marker with a particle attached, no space, answers it" \
    "$(cutoff "$FN" "$(lst "$C_CUTOFF_R1" "$C_NAMES_R1_PARTICLE")" "$W_CUT")" ok
chk "an aggregate comment in the window is not a cutoff comment" \
    "$(cutoff "$FN" "$(lst "$C_AGG")" "$W_CUT")" ok
chk "no comments at all: nothing on this surface" "$(cutoff "$FN" '[]' "$W_CUT")" ok
chk "a cutoff comment before the window start is a previous round's, not this one's" \
    "$(cutoff "$FN" "$(lst "$C_CUTOFF")" 2026-10-06T12:00:00Z)" ok
chk "a second round's cutoff, undispositioned beside a dispositioned first, blocks" \
    "$(cutoff "$FN" "$(lst "$C_CUTOFF" "$C_DISP" "$C_CUTOFF_R9")" "$W_CUT")" block
chk "both dispositioned passes" \
    "$(cutoff "$FN" "$(lst "$C_CUTOFF" "$C_DISP" "$C_CUTOFF_R9" "$(cmt 106 2026-10-06T22:00:00Z hyuk-hur User '> c.py:9 -- an R9 item
Deferred to #176: round-cutoff-claude-r9')")" "$W_CUT")" ok
# shellcheck disable=SC1090
out=$(. "$FN"; printf '%s' "$(lst "$C_CUTOFF" "$C_DISP" "$C_CUTOFF_R9")" | "$CUTOFF_FN" "$W_CUT")
chk "the block names the undispositioned round and leaves the answered one answered" \
    "$(printf '%s\n' "$out" | grep -c 'BLOCK: round-cutoff-claude-r9\|round-cutoff-claude-r7 (comment 6015282943, 2026-10-06T11:28:32Z): answered')" 2
chk "the block says what the thread count cannot" \
    "$(printf '%s\n' "$out" | grep -c 'zero unresolved threads is not zero findings')" 1
chk "the answered line counts the items it bound" \
    "$(printf '%s\n' "$out" | grep -c 'round-cutoff-claude-r7 (comment 6015282943, 2026-10-06T11:28:32Z): answered.*2 item(s), each quoted by location')" 1
# shellcheck disable=SC1090
out=$(. "$FN"; printf '%s' "$(lst "$C_CUTOFF" "$C_ONE_OF_TWO")" | "$CUTOFF_FN" "$W_CUT")
chk "the block names the item no reply quoted, and only that one" \
    "$(printf '%s\n' "$out" | grep -c 'by location: \.github/scripts/tests/test_aggregate_reviews\.py:44 ')" 1

# The three identities (AT-2803). post_inline_comments.py writes the cutoff
# comment under whichever `gh` the round ran as: `github-actions[bot]` from
# the workflow, `ignite-ai-review-approver[bot]` where the reviewer App's
# token is used, and the operator's own login from review_pr_local.py, whose
# author type is `User`. An author filter on `.type == "Bot"` saw the first
# two and dropped the third, so a locally-driven round printed `no
# round-cutoff comment on this round's surface` and returned 0 over items
# nobody dispositioned -- the window defect of rounds 5 and 8 by another
# path. Reachable, not observed: the cutoff fires only at ROUND_CUTOFF_N
# and fetch_round_count counts Bot-authored verdicts only, so a pure-local
# drive never reaches round 5; a mixed drive does. The body is identical in
# all three rows, so the identity is the only thing that varies.
C_CUT_GA=$(cmt 120 2026-10-06T11:28:32Z 'github-actions[bot]' Bot "$CUT_BODY")
C_CUT_APP=$(cmt 121 2026-10-06T11:28:32Z 'ignite-ai-review-approver[bot]' Bot "$CUT_BODY")
C_CUT_LOCAL=$(cmt 122 2026-10-06T11:28:32Z hyuk-hur User "$CUT_BODY")
chk "a cutoff comment by github-actions[bot] is found" \
    "$(cutoff "$FN" "$(lst "$C_CUT_GA")" "$W_CUT")" block
chk "a cutoff comment by the reviewer App is found" \
    "$(cutoff "$FN" "$(lst "$C_CUT_APP")" "$W_CUT")" block
chk "a cutoff comment by the operator's own gh login is found too (AT-2803)" \
    "$(cutoff "$FN" "$(lst "$C_CUT_LOCAL")" "$W_CUT")" block
chk "the locally-driven cutoff comment, dispositioned, passes: the block above is the findings, not the author" \
    "$(cutoff "$FN" "$(lst "$C_CUT_LOCAL" "$C_DISP")" "$W_CUT")" ok

# What stands in for the author is the producer's signature: the first line
# that is nothing but the marker's HTML comment. These are the shapes that
# must NOT be read as one, now that no author filter refuses them first.
C_HUMAN_PLAIN=$(cmt 123 2026-10-06T11:30:00Z hyuk-hur User 'rebased onto main and pushed the fixup.')
chk "a human comment with no marker is not a cutoff comment" \
    "$(cutoff "$FN" "$(lst "$C_HUMAN_PLAIN")" "$W_CUT")" ok
# The regress: every disposition comment this account writes names the
# marker in backticks, inline. Read as a cutoff comment it would demand a
# disposition of itself, and that disposition would name the marker too --
# without end. C_DISP is the measured one from #188, named-in-backticks and
# nothing more; alone on the surface it must find nothing to block on.
chk "a disposition comment naming the marker in backticks is not a cutoff comment (no regress)" \
    "$(cutoff "$FN" "$(lst "$C_DISP")" "$W_CUT")" ok
# The marker's own text, quoted the way a reply quotes it: the line starts
# with `>`, so it is not the producer's signature.
C_RAW_QUOTED=$(cmt 124 2026-10-06T11:30:00Z hyuk-hur User 'the comment opens with
> <!-- round-cutoff-claude-r7 -->
which is how the gate finds it.')
chk "a reply quoting the raw marker line under a > is not a cutoff comment" \
    "$(cutoff "$FN" "$(lst "$C_RAW_QUOTED")" "$W_CUT")" ok
# A disposition can reproduce the raw marker inside a code fence. Only
# the producer's first line identifies a cutoff comment (PR #194 R22).
C_RAW_FENCED=$(cmt 128 2026-10-06T11:40:00Z hyuk-hur User 'The producer marker is:
```
<!-- round-cutoff-claude-r7 -->
```
'"$DISP_BODY")
chk "a marker reproduced inside a code fence is not a cutoff comment" \
    "$(cutoff "$FN" "$(lst "$C_RAW_FENCED")" "$W_CUT")" ok
chk "a disposition reproducing the raw marker still answers the real cutoff" \
    "$(cutoff "$FN" "$(lst "$C_CUTOFF" "$C_RAW_FENCED")" "$W_CUT")" ok

# The same asymmetry on the answering side. `.type != "Bot"` kept the
# producer's own output from answering it, which held only while the
# producer was always a Bot. Locally it is a `User`, so a later round's
# cutoff comment -- which names the earlier marker and carries `>` lines
# over the items it is re-reporting -- would have answered the round it
# re-reports. The two bodies below differ in one line: the marker.
# shellcheck disable=SC2016  # the backticks are the quoted items' own
R9_OVER_R7='## [bot] claude Round Cutoff Summary (R9)

R9 convergence cutoff -- still open from round-cutoff-claude-r7:

- **c.py:9** -- - **minor** (claude): an R9 item

> **minor** `.github/scripts/aggregate_reviews.py:61` -- dead helper
> **suggestion** `.github/scripts/tests/test_aggregate_reviews.py:44` -- second import'
C_R9_LOCAL=$(cmt 125 2026-10-06T11:35:00Z hyuk-hur User '<!-- round-cutoff-claude-r9 -->
'"$R9_OVER_R7")
C_R9_AS_HUMAN=$(cmt 126 2026-10-06T11:35:00Z hyuk-hur User "$R9_OVER_R7")
# R9 itself is dispositioned, so r9 is not what any verdict below turns on.
C_DISP_R9=$(cmt 127 2026-10-06T11:50:00Z hyuk-hur User 'round-cutoff-claude-r9:
> c.py:9 -- an R9 item
Fixed: handled in the fixup.')
chk "a later locally-posted cutoff comment does not answer the round it re-reports" \
    "$(cutoff "$FN" "$(lst "$C_CUT_LOCAL" "$C_R9_LOCAL" "$C_DISP_R9")" "$W_CUT")" block
# shellcheck disable=SC1090
out=$(. "$FN"; printf '%s' "$(lst "$C_CUT_LOCAL" "$C_R9_LOCAL" "$C_DISP_R9")" | "$CUTOFF_FN" "$W_CUT")
chk "the block names the re-reported round, and the re-reporting one passes" \
    "$(printf '%s\n' "$out" | grep -c 'BLOCK: round-cutoff-claude-r7 (comment 122\|^(c) round-cutoff-claude-r9 (comment 125,.*): answered')" 2
chk "control: the same text without the marker line is a disposition and does answer it" \
    "$(cutoff "$FN" "$(lst "$C_CUT_LOCAL" "$C_R9_AS_HUMAN")" "$W_CUT")" ok

# The window start (#194 round 8, codex). Until then (c)'s window started
# at run_start, the newest completed/success run's createdAt, and two
# shapes the skill documents put the cutoff comment before it: a run that
# posts its cutoff comment and then concludes failure - step 8's "did not
# pass" row - is not in that list, so the start fell to a later kind (ii)
# verdict's; and two completed/success runs on one head start it at the
# newer run's createdAt, after the older run's cutoff comment, which the
# producer does not repost. The cutoff comment is C_CUTOFF, 11:28:32Z; each
# run list is in the shape `gh run list --json` returns, newest first.
CUT_WF='AI Code Review'
R_FAILED=$(run 1 "$CUT_WF" completed failure 2026-10-06T11:28:00Z 2026-10-06T11:35:00Z)
R_OK_OLD=$(run 2 "$CUT_WF" completed success 2026-10-06T11:28:00Z 2026-10-06T11:35:00Z)
R_OK_NEW=$(run 3 "$CUT_WF" completed success 2026-10-06T12:10:00Z 2026-10-06T12:17:00Z)
R_LINT=$(run 4 lint completed success 2026-10-06T11:00:00Z 2026-10-06T11:01:00Z)
V_LATER=2026-10-06T12:05:00Z
C_LATER_VERDICT=$(cmt 114 "$V_LATER" hyuk-hur User 'a kind (ii) approval, no quote')
chk "shape 1: a completed/failure run's createdAt starts the window, not the later kind (ii) verdict" \
    "$(since_of "$FN" "$(lst "$R_FAILED")" "$CUT_WF" "$V_LATER")" 2026-10-06T11:28:00Z
chk "shape 1: the cutoff comment that run posted is inside the window and blocks" \
    "$(cutoff "$FN" "$(lst "$C_CUTOFF" "$C_LATER_VERDICT")" "$(since_of "$FN" "$(lst "$R_FAILED")" "$CUT_WF" "$V_LATER")")" block
chk "shape 2: of two completed/success runs the older starts the window" \
    "$(since_of "$FN" "$(lst "$R_OK_NEW" "$R_OK_OLD")" "$CUT_WF" '')" 2026-10-06T11:28:00Z
chk "shape 2: the older run's cutoff comment is inside the window and blocks" \
    "$(cutoff "$FN" "$(lst "$C_CUTOFF")" "$(since_of "$FN" "$(lst "$R_OK_NEW" "$R_OK_OLD")" "$CUT_WF" '')")" block
chk "the first verdict starts the window when it is the earlier" \
    "$(since_of "$FN" "$(lst "$R_OK_NEW")" "$CUT_WF" 2026-10-06T11:00:00Z)" 2026-10-06T11:00:00Z
chk "another workflow's earlier run does not start the window" \
    "$(since_of "$FN" "$(lst "$R_LINT" "$R_OK_NEW")" "$CUT_WF" '')" 2026-10-06T12:10:00Z
chk "no review-workflow run and no verdict prints nothing, which the gate then refuses as a read that failed" \
    "$(since_of "$FN" "$(lst "$R_LINT")" "$CUT_WF" '')" none
chk "a review-workflow run with a null createdAt is refused, not dropped" \
    "$(since_of "$FN" "$(lst "$(run 5 "$CUT_WF" completed success '' '')" "$R_OK_NEW")" "$CUT_WF" '')" refused
chk "a review-workflow run whose createdAt is not a timestamp is refused, not sorted" \
    "$(since_of "$FN" "$(lst "$(run 6 "$CUT_WF" completed success yesterday '')" "$R_OK_NEW")" "$CUT_WF" '')" refused
chk "an unreadable run list is refused, not read as no run" \
    "$(since_of "$FN" 'not json' "$CUT_WF" '')" refused
# The gate's own call site (#194 round 11, claude). cutoff_window_start
# writes its refusal on stdout, which the gate captures into $since, so a
# bare `|| exit 1` tail stopped the gate with the one line that said why
# dropped. The line is run as SKILL.md spells it, under the extracted
# functions and a run list jq cannot read: it must still stop, and the
# refusal must reach stdout.
SINCE_SITE="since=\$(printf '%s' \"\$runs\" | $SINCE_FN \"\$REVIEW_WF\" \"\$first_verdict\")"
SINCE_CALL=$(grep -F -- "$SINCE_SITE" "$SKILL")
chk "the gate captures the window start at exactly one call site" "$(printf '%s\n' "$SINCE_CALL" | grep -c .)" 1
# shellcheck disable=SC1090,SC2034  # the call site reads runs, REVIEW_WF and first_verdict
out=$( . "$FN"; runs='not json'; REVIEW_WF=$CUT_WF; first_verdict=''; eval "$SINCE_CALL" 2> /dev/null )
rc=$?
chk "that call site stops the gate on a refused window start" "$rc" 1
chk "that call site prints the function's refusal rather than capturing it" \
    "$(printf '%s\n' "$out" | grep -c '^ERROR: could not read the cutoff window start')" 1

echo "5g. a short-roster round is not a convergence round"
chk "a full roster whose findings do not affect behaviour has converged" \
    "$(converged "$FN" "$H_NO_ISSUES" no-behaviour)" converged
chk "the review-surface headline converges the same way" \
    "$(converged "$FN" "$H_REVIEW_NO_ISSUES" no-behaviour)" converged
chk "the same full round, findings that affect behaviour: not converged, fix in round" \
    "$(converged "$FN" "$H_NO_ISSUES" behaviour)" block
chk "2/3 reviewers with no-behaviour is refused: a starved round is not convergence (AT-2511)" \
    "$(converged "$FN" "$H_CUT" no-behaviour)" block
chk "2/3 LLM responses with no-behaviour is refused" "$(converged "$FN" "$H_LLM_SHORT" no-behaviour)" block
chk "0/3 reviewers with no-behaviour is refused" "$(converged "$FN" "$H_NONE" no-behaviour)" block
chk "2/2 against 3 configured with no-behaviour is refused" "$(converged "$FN" "$H_TWO" no-behaviour)" block
chk "a full roster with no behaviour statement at all is refused" "$(converged "$FN" "$H_NO_ISSUES" '')" block
chk "a full roster with an unrecognised statement is refused" "$(converged "$FN" "$H_NO_ISSUES" maybe)" block
chk "an empty headline with no-behaviour is refused" "$(converged "$FN" '' no-behaviour)" block
EXPECTED=2
chk "2/2 against 2 configured converges, so it is the comparison and not the digit" \
    "$(converged "$FN" "$H_TWO" no-behaviour)" converged
EXPECTED=3
# shellcheck disable=SC1090,SC2034  # the function reads EXPECTED_REVIEWERS
out=$(. "$FN"; EXPECTED_REVIEWERS=3; "$CONV_FN" "$H_CUT" no-behaviour)
chk "the refusal names the short roster, not the statement" \
    "$(printf '%s\n' "$out" | grep -c 'cannot be the one that shows nothing new')" 1
# shellcheck disable=SC1090,SC2034  # the function reads EXPECTED_REVIEWERS
out=$(. "$FN"; EXPECTED_REVIEWERS=3; "$CONV_FN" "$H_NO_ISSUES" '')
chk "the refusal on a full round names the missing statement" \
    "$(printf '%s\n' "$out" | grep -c 'must state whether this round.s findings affect behaviour')" 1

echo "5h. (c)'s one call site reads every verdict (a) named, whichever kind: a kind (ii) approval listing a major is checked like the aggregate's"
# Why: until round 9 of #194 major_dispositions ran under `if [ -n
# "$kind_i" ]`, so a local-review approval that listed a major in the
# aggregate's item shape reached the merge block with the item only printed
# (codex). The source list carries both kinds in one shape, and the loop
# asks neither which it is. The header below is the kind (ii) contract's
# bytes -- the em dash and the middle dot are built from octal so this file
# stays ASCII -- though the function reads none of it: a source is
# whatever (a) named, and only its item lines are keyed.
HEAD_R9=a66b72bc020ad638b8cdb5c378841ac99c267eb1
V_AT=2026-10-14T10:00:00Z
EM=$(printf '\342\200\224'); DOT=$(printf '\302\267')
V_HDR="## LENS verdict $EM **APPROVE** $DOT head \`$HEAD_R9\`"
# shellcheck disable=SC2016  # the backticks are the item's own
V_ITEM="$V_HDR

One item left to the operator:
- + **major** \`bin/x.sh:10\` -- unchecked input on the retry path"
V_PROSE="$V_HDR

### two remarks, neither blocking
**[minor 1]** the body's line numbers are off by one"
S_II=$(vsrc ii 'comment 1 by hyuk-hur' "$V_AT" "$V_AT" "$V_ITEM" null)
S_II_PROSE=$(vsrc ii 'comment 1 by hyuk-hur' "$V_AT" "$V_AT" "$V_PROSE" null)
C_VERDICT=$(cmt 1 "$V_AT" hyuk-hur User "$V_ITEM")
C_PROSE=$(cmt 1 "$V_AT" hyuk-hur User "$V_PROSE")
# shellcheck disable=SC2016  # the backticks are the item's own
Q_ITEM='> - + **major** `bin/x.sh:10` -- unchecked input on the retry path
Fixed: bin/x.sh:12'
C_LATER=$(cmt 2 2026-10-14T11:00:00Z hyuk-hur User "$Q_ITEM")
C_EARLIER=$(cmt 2 2026-10-14T09:00:00Z hyuk-hur User "$Q_ITEM")
C_BOT=$(cmt 2 2026-10-14T11:00:00Z 'github-actions[bot]' Bot "$Q_ITEM")
T_ITEM=$(thr bin/x.sh 10 replied 'unchecked input on the retry path')
chk "round 9 reproduction: a kind (ii) approval listing a major, no thread and no quote, blocks" \
    "$(disp "$FN" '' "$(lst "$S_II")" "$(lst "$C_VERDICT")")" block
chk "the item quoted whole in a later non-Bot comment answers it" \
    "$(disp "$FN" '' "$(lst "$S_II")" "$(lst "$C_VERDICT" "$C_LATER")")" pass
chk "a replied thread at the item's path:line answers it" \
    "$(disp "$FN" "$T_ITEM" "$(lst "$S_II")" "$(lst "$C_VERDICT")")" pass
chk "a quote posted before the verdict does not answer it" \
    "$(disp "$FN" '' "$(lst "$S_II")" "$(lst "$C_EARLIER" "$C_VERDICT")")" block
chk "a quote by a Bot after the verdict does not answer it" \
    "$(disp "$FN" '' "$(lst "$S_II")" "$(lst "$C_VERDICT" "$C_BOT")")" block
chk "the measured hand-verdict shape, prose and no item line, has nothing mechanical to check and passes to the human read" \
    "$(disp "$FN" '' "$(lst "$S_II_PROSE")" "$(lst "$C_PROSE")")" pass
# shellcheck disable=SC1090
out=$(. "$FN"; "$DISP_FN" '' "$(lst "$S_II")" "$(lst "$C_VERDICT")")
chk "the block names the verdict kind and its label" \
    "$(printf '%s\n' "$out" | grep -c '^BLOCK (c): a major item of verdict kind (ii) comment 1 by hyuk-hur has no disposition')" 1
chk "a kind (ii) source declares no count, so its one item line is what is counted" \
    "$(printf '%s\n' "$out" | grep -c '^(c) verdict kind (ii) comment 1 by hyuk-hur names 1 major item(s)')" 1
chk "the human read is printed for the kind (ii) verdict, block or not" \
    "$(printf '%s\n' "$out" | grep -c '^(c) READ kind (ii) comment 1 by hyuk-hur')" 1
# shellcheck disable=SC1090
out=$(. "$FN"; "$DISP_FN" '' "$(lst "$S_II_PROSE")" "$(lst "$C_PROSE")")
chk "the prose verdict gets the human read and no mechanical line" \
    "$(printf '%s\n' "$out" | grep -c '^(c) READ kind (ii)\|names [0-9]* major item')" 1
# Kind (i) through the same call site: the aggregate's unreviewed major,
# its headline's count, the same thread and quote routes.
AGG_AT=2026-10-14T08:00:00Z; AGG_SINCE=2026-10-14T07:50:00Z
S_I=$(vsrc i 'aggregate comment 7 by github-actions[bot]' "$AGG_AT" "$AGG_SINCE" "$BODY_ONE" 1)
C_AGG_I=$(cmt 7 "$AGG_AT" 'github-actions[bot]' Bot "$BODY_ONE")
C_Q_I=$(cmt 8 2026-10-14T08:30:00Z hyuk-hur User "$QUOTE_PATH")
chk "kind (i) through the same call site: an unanswered unreviewed major blocks" \
    "$(disp "$FN" '' "$(lst "$S_I")" "$(lst "$C_AGG_I")")" block
chk "kind (i): the item quoted after the aggregate passes" \
    "$(disp "$FN" '' "$(lst "$S_I")" "$(lst "$C_AGG_I" "$C_Q_I")")" pass
chk "kind (i): the headline's count is what is checked -- 2 declared over a body listing 1 blocks" \
    "$(disp "$FN" '' "$(lst "$(vsrc i 'aggregate comment 7 by github-actions[bot]' "$AGG_AT" "$AGG_SINCE" "$BODY_ONE" 2)")" "$(lst "$C_AGG_I" "$C_Q_I")")" block
chk "kind (i): a declared count of 0 is a count, not an absence -- nothing is counted off the body" \
    "$(disp "$FN" '' "$(lst "$(vsrc i 'aggregate comment 7 by github-actions[bot]' "$AGG_AT" "$AGG_SINCE" "$BODY_ONE" 0)")" "$(lst "$C_AGG_I")")" pass
chk "both kinds on one head: kind (i) answered and kind (ii) not blocks -- two reviewers, not duplicates" \
    "$(disp "$FN" '' "$(lst "$S_I" "$S_II")" "$(lst "$C_AGG_I" "$C_Q_I" "$C_VERDICT")")" block
chk "both kinds: kind (ii) answered and kind (i) not blocks" \
    "$(disp "$FN" '' "$(lst "$S_I" "$S_II")" "$(lst "$C_AGG_I" "$C_VERDICT" "$C_LATER")")" block
chk "both kinds, both answered, passes" \
    "$(disp "$FN" '' "$(lst "$S_I" "$S_II")" "$(lst "$C_AGG_I" "$C_Q_I" "$C_VERDICT" "$C_LATER")")" pass
# shellcheck disable=SC1090
out=$(. "$FN"; "$DISP_FN" '' "$(lst "$S_I" "$S_II")" "$(lst "$C_AGG_I" "$C_Q_I" "$C_VERDICT" "$C_LATER")")
chk "both kinds are each read: two READ lines, kind (i) first" \
    "$(printf '%s\n' "$out" | grep -o '^(c) READ kind ([i]*)' | tr '\n' '|')" "(c) READ kind (i)|(c) READ kind (ii)|"
# Refusals: nothing to read is a failed read, and a source the gate cannot
# place is not read with every quote admitted.
chk "an empty source list is refused: (a) exits without a verdict, so an empty list here is a failed read" \
    "$(disp "$FN" '' '[]' "$(lst "$C_VERDICT")")" block
chk "an unreadable source list is refused" "$(disp "$FN" '' 'not json' "$(lst "$C_VERDICT")")" block
chk "a source with no timestamp is refused even with the item quoted later, so the check is a refusal and not a missed quote" \
    "$(disp "$FN" '' "$(lst "$(vsrc ii 'comment 1 by hyuk-hur' '' "$V_AT" "$V_ITEM" null)")" "$(lst "$C_VERDICT" "$C_LATER")")" block
chk "a source with no round start is refused the same way" \
    "$(disp "$FN" '' "$(lst "$(vsrc ii 'comment 1 by hyuk-hur' "$V_AT" '' "$V_ITEM" null)")" "$(lst "$C_VERDICT" "$C_LATER")")" block
# shellcheck disable=SC1090
out=$(. "$FN"; "$DISP_FN" '' "$(lst "$(vsrc ii 'comment 1 by hyuk-hur' '' "$V_AT" "$V_ITEM" null)")" "$(lst "$C_VERDICT" "$C_LATER")")
chk "the refusal says which source it could not place" \
    "$(printf '%s\n' "$out" | grep -c '^ERROR: verdict source comment 1 by hyuk-hur (kind ii) has no timestamp')" 1
# The fields are read in place: a tab-separated read collapsed the empty
# timestamp and shifted the round start and the count into its place, so
# the refusal named the count as what was missing (#194 round 10).
chk "and names the timestamp as the empty value, with the round start and the count still in their own places" \
    "$(printf '%s\n' "$out" | grep -c "no major count .* '', '2026-10-14T10:00:00Z', '1'\$")" 1
chk "an unreadable comment list is refused" "$(disp "$FN" '' "$(lst "$S_II")" 'not json')" block

echo "5i. REVIEW_WF is checked before anything reads it: a name no run carries is not 'no run'"
# #194 round 12 (claude): every reader of REVIEW_WF matches by equality
# and reads a name that matches nothing as "no run" -- the shipped
# placeholder `<review workflow name>` included -- so kind (i) never held
# and, once a local verdict stood, cutoff_window_start started (c)'s window
# at that verdict rather than at the review run: the class rounds 5 and 8
# closed, through the name filter. The function keeps that contract -- it
# takes the name as given -- and review_workflow_known stops the gate
# upstream of it.
chk "cutoff_window_start reads a name no run carries as no run, so the shipped placeholder leaves it the first verdict alone -- the round-12 shape" \
    "$(since_of "$FN" "$(lst "$R_OK_NEW")" '<review workflow name>' "$V_LATER")" "$V_LATER"
NAMES_WITH=$'lint\nAI Code Review'
NAMES_WITHOUT='lint'
chk "the shipped placeholder is refused, whatever the lists say" \
    "$(known "$FN" "$(lst "$R_OK_NEW")" '<review workflow name>' "$NAMES_WITH")" refused
chk "an empty name is refused" "$(known "$FN" "$(lst "$R_OK_NEW")" '' "$NAMES_WITH")" refused
chk "a name some run on this head carries needs nothing else: the list proves it" \
    "$(known "$FN" "$(lst "$R_OK_NEW")" "$CUT_WF" '')" ok
chk "a name no run carries that the repository has is step 8's '[]' row, left to (a): other workflows ran, the review did not" \
    "$(known "$FN" "$(lst "$R_LINT")" "$CUT_WF" "$NAMES_WITH")" ok
chk "an empty run list with a name the repository has is the same row, not a block here" \
    "$(known "$FN" '[]' "$CUT_WF" "$NAMES_WITH")" ok
chk "a name no run carries and the repository lacks is refused: a mistyped name is not a review that did not run" \
    "$(known "$FN" "$(lst "$R_LINT")" "$CUT_WF" "$NAMES_WITHOUT")" refused
chk "a mistyped name is refused although the review workflow's own run is on the head" \
    "$(known "$FN" "$(lst "$R_OK_NEW")" 'AI Code Reveiw' "$NAMES_WITH")" refused
chk "an empty run list with a name the repository lacks is refused" \
    "$(known "$FN" '[]' "$CUT_WF" "$NAMES_WITHOUT")" refused
chk "the repository's names are matched whole: a prefix of the name is not the name" \
    "$(known "$FN" '[]' 'AI Code' "$NAMES_WITH")" refused
chk "an unreadable run list is refused, not read as no run" \
    "$(known "$FN" 'not json' "$CUT_WF" "$NAMES_WITH")" refused
# shellcheck disable=SC1090
out=$(. "$FN"; printf '%s' "$(lst "$R_LINT")" | "$KNOWN_FN" "$CUT_WF" "$NAMES_WITH")
chk "the did-not-run line names the runs the head does have, so the operator reads names, not the count" \
    "$(printf '%s\n' "$out" | grep -c "^(a) no 'AI Code Review' run on this head.*runs here: lint")" 1
# The gate's own call site, as SKILL.md spells it: found once by its text,
# before the first reader of the name, and run under the extracted
# functions with a run list that carries other workflows' runs alone. On
# the shipped placeholder and on a mistyped name it must stop the gate; on
# the right name it must not, since the review not running is (a)'s to
# block. Red on the text before round 12, which had no such call site.
KNOWN_SITE="printf '%s' \"\$runs\" | $KNOWN_FN \"\$REVIEW_WF\" \"\$wf_names\" || exit 1"
KNOWN_CALL=$(grep -F -- "$KNOWN_SITE" "$SKILL")
chk "the gate checks the name at exactly one call site" "$(printf '%s\n' "$KNOWN_CALL" | grep -c .)" 1
known_at=$(grep -nF -- "$KNOWN_SITE" "$SKILL" | head -n 1 | cut -d: -f1)
reader_at=$(grep -n '^run_window=' "$SKILL" | head -n 1 | cut -d: -f1)
chk "that call site precedes run_window, the name's first reader" \
    "$([ "${known_at:-0}" -gt 0 ] && [ "${known_at:-0}" -lt "${reader_at:-0}" ] && echo before || echo not-before)" before
# shellcheck disable=SC1090,SC2034  # the call site reads runs, REVIEW_WF and wf_names
out=$( . "$FN"; runs=$(lst "$R_LINT"); REVIEW_WF='<review workflow name>'; wf_names=$NAMES_WITH; eval "$KNOWN_CALL" 2> /dev/null )
rc=$?
chk "that call site stops the gate on the shipped placeholder" "$rc" 1
chk "and says so on stdout" "$(printf '%s\n' "$out" | grep -c "^ERROR: REVIEW_WF is '<review workflow name>', not a workflow name")" 1
# shellcheck disable=SC1090,SC2034  # the call site reads runs, REVIEW_WF and wf_names
out=$( . "$FN"; runs=$(lst "$R_LINT"); REVIEW_WF='AI Code Reveiw'; wf_names=$NAMES_WITH; eval "$KNOWN_CALL" 2> /dev/null )
rc=$?
chk "that call site stops the gate on a mistyped name" "$rc" 1
chk "and names the repository as what lacks it" "$(printf '%s\n' "$out" | grep -c "^ERROR: REVIEW_WF is 'AI Code Reveiw', which names no workflow in this repository")" 1
# shellcheck disable=SC1090,SC2034  # the call site reads runs, REVIEW_WF and wf_names
out=$( . "$FN"; runs=$(lst "$R_LINT"); REVIEW_WF=$CUT_WF; wf_names=$NAMES_WITH; eval "$KNOWN_CALL" 2> /dev/null )
rc=$?
chk "that call site lets the right name through when the review did not run on this head, which is (a)'s to block" "$rc" 0

echo "5j. (b)'s thread rows survive an empty first comment"
# #194 round 12 (claude): the fourth field of each thread row was
# `split("\n")[0] | gsub(...)`, and jq's split of "" is [], whose [0] is
# null, which gsub refuses -- so one empty first comment aborted the whole
# read as an unreadable thread list. The read lives in the gate body, not
# in a function, so it is lifted by its text, from its assignment line to
# its failure tail, and run under a snapshot with one empty first comment
# and one line-less thread whose first comment is the producer's finding.
# Red on the text before round 12: the lifted read exits 1 there.
thread_from=$(grep -nF "thread_data=\$(printf '%s' \"\$snap_json\" | jq -r" "$SKILL" | head -n 1 | cut -d: -f1)
thread_to=$(grep -nF "an unreadable thread list is not 'no threads'" "$SKILL" | head -n 1 | cut -d: -f1)
chk "the thread read is found once, assignment and failure tail" \
    "$(grep -cF "thread_data=\$(printf '%s' \"\$snap_json\" | jq -r" "$SKILL"),$(grep -cF "an unreadable thread list is not 'no threads'" "$SKILL")" 1,1
THREAD_SITE=$(sed -n "${thread_from:-0},${thread_to:-0}p" "$SKILL")
SNAP_EMPTY='{"headRefOid":"h","reviewThreads":{"totalCount":2,"nodes":[
 {"id":"T1","isResolved":true,"path":"a.py","line":3,"originalLine":3,
  "comments":{"totalCount":1,"nodes":[{"body":"","author":{"login":"github-actions","__typename":"Bot"}}]}},
 {"id":"T2","isResolved":true,"path":"b.py","line":null,"originalLine":null,
  "comments":{"totalCount":2,"nodes":[{"body":"+ **major** (codex): the finding\r\n\r\n> ? fix it","author":{"login":"github-actions","__typename":"Bot"}},
                                       {"body":"Fixed","author":{"login":"hyuk-hur","__typename":"User"}}]}}]}}'
# shellcheck disable=SC2034,SC2154  # the lifted read sets thread_data from snap_json
out=$( snap_json=$SNAP_EMPTY; eval "$THREAD_SITE" 2> /dev/null && printf '%s' "$thread_data" )
rc=$?
chk "an empty first comment does not abort the read" "$rc" 0
chk "both threads come out as rows" "$(printf '%s\n' "$out" | grep -c .)" 2
chk "the empty first comment's row keeps its empty line field, in thread_answers' shape" \
    "$(printf '%s\n' "$out" | head -n 1 | tr '\037' '|')" 'a.py|3|unreplied|'
chk "the line-less thread's row carries the producer's finding line, the suggestion two lines down unread" \
    "$(printf '%s\n' "$out" | sed -n 2p | tr '\037' '|')" 'b.py||replied|+ **major** (codex): the finding'

echo "5k. the runner-side locator: which of three an empty [] row is, for THIS head"
# Measured 2026-10-21 on hyuk-hur/dev-dotfiles#194: run 39523085356 in
# ignite-corp/ai-dev-pr-review-runner failed at `Mint target reporting
# token` with a 404 - before the step that reports into the target - so
# neither surface on that repository carried anything for it and the two
# readings "nothing was dispatched" and "the dispatch died" were one `[]`.
# Its run-name is the shape the filter keys on, verbatim. Every fixture
# is dated against HEAD_TIME, because the name carries no SHA and the
# previous head's round matches the same needle this head's does.
DIED_URL=https://github.com/ignite-corp/ai-dev-pr-review-runner/actions/runs/39523085356
D_DIED=$(rrun 39523085356 completed failure 2026-10-21T00:51:12Z "LENS review $TREPO#194" "$DIED_URL")
D_RUNNING=$(rrun 39530000001 in_progress '' 2026-10-21T02:00:00Z "LENS review $TREPO#194" https://example.invalid/runner/running)
D_QUEUED=$(rrun 39530000002 queued '' 2026-10-21T02:30:00Z "LENS review $TREPO#194" https://example.invalid/runner/queued)
# The previous head's round: completed, and it DID post its check run -
# on the old SHA, which the surfaces here no longer return. On the PR
# number alone it is indistinguishable from a dispatch that died, which
# is the defect the head bound closes.
D_PREV_HEAD=$(rrun 39510000000 completed success 2026-10-20T09:00:00Z "LENS review $TREPO#194" https://example.invalid/runner/prev-head)
# The boundary: a run created at the same instant the head was pushed is
# this head's, not the last one's.
D_AT_HEAD=$(rrun 39515000000 completed failure "$HEAD_TIME" "LENS review $TREPO#194" https://example.invalid/runner/at-head)
# Three near misses the filter must reject: another PR of the same
# repository, a PR whose number merely begins with this one's, and the
# same number on another consumer. The runner reviews every consumer, so
# all three are on the same list this one is.
D_PR195=$(rrun 39520000003 completed success 2026-10-21T03:00:00Z "LENS review $TREPO#195" https://example.invalid/runner/195)
D_PR1940=$(rrun 39520000004 completed success 2026-10-21T03:10:00Z "LENS review $TREPO#1940" https://example.invalid/runner/1940)
D_OTHER_REPO=$(rrun 39520000005 completed success 2026-10-21T03:20:00Z 'LENS review ignite-corp/ai-dev-cab#194' https://example.invalid/runner/cab)
# The LEFT edge. One edge is not a boundary: an owner name ending in
# this one's is a name GitHub allows, and the runner's list carries
# every owner's runs.
D_LEFT=$(rrun 39560000000 completed success 2026-10-21T05:00:00Z "LENS review my-$TREPO#194" https://example.invalid/runner/left)
# A run with no `run-name:`: `name` is the workflow's name and
# `display_title` is the head commit's subject, measured 2026-10-22. A
# session writes commit subjects that cite PRs, so the subject is a
# field an unrelated run can carry the needle in; the workflow name is
# not.
D_SUBJECT_ONLY=$(rrun_no_runname 39540000000 completed success 2026-10-21T04:00:00Z 'AI Code Review' "fix: bind the locator to the head, per $TREPO#194" https://example.invalid/runner/subject)
# Two candidates above the bound, the newer completed and the older
# still running.
D_OLDER_RUNNING=$(rrun 39531000000 in_progress '' 2026-10-21T01:00:00Z "LENS review $TREPO#194" https://example.invalid/runner/older-running)
D_NEWER_DEAD=$(rrun 39532000000 completed failure 2026-10-21T01:30:00Z "LENS review $TREPO#194" https://example.invalid/runner/newer-dead)
# A record with no status at all: `select(.status != "completed")` passes
# a missing value, so without the refusal it becomes `running` and the
# operator waits on nothing.
D_NOSTATUS=$(command jq -cn --arg d "LENS review $TREPO#194" \
  '{id: 39533000000, name: $d, head_branch: "x", conclusion: null, created_at: "2026-10-21T01:00:00Z", updated_at: "x", display_title: $d, html_url: "https://example.invalid/runner/nostatus"}')

# A run too young to have resolved its run-name. Measured 2026-10-26 on
# ignite-corp/ai-dev-pr-review-runner run 40296968898: created
# 09:06:00Z, served at 09:06:02Z with `name` AND `display_title` both
# the bare workflow name `LENS review`, and both carrying
# `LENS review hyuk-hur/dev-dotfiles#194` by 09:06:03Z. The fixture is
# that two-second shape, dated after the bound so it is a candidate.
D_YOUNG=$(rrun 39570000000 queued '' 2026-10-21T01:05:00Z 'LENS review' https://example.invalid/runner/young)
chk "a run whose name has not resolved yet is not read as nothing dispatched" \
    "$(dispatch "$FN" "$(rpage "$D_YOUNG")" '' '')" "error(1)"
chk "and the refusal says it is a run too young to read, not one for another target" \
    "$(locmsg "$FN" "$(rpage "$D_YOUNG")" 'too young to have resolved its run-name')" 1
chk "and it says the wait ends on its own, so the refusal is a pause and not a wall" \
    "$(locmsg "$FN" "$(rpage "$D_YOUNG")" 'no re-dispatch needed')" 1
chk "and it says re-reading is the answer, not re-dispatching" \
    "$(locmsg "$FN" "$(rpage "$D_YOUNG")" 'would cancel it')" 1
chk "it stops the locator beside this head's own dead dispatch too, because it could be a newer one" \
    "$(dispatch "$FN" "$(rpage "$D_DIED" "$D_YOUNG")" '' '')" "error(1)"
# The positive companion, and the reason the rule is keyed on the SHAPE
# rather than on this target's needle: a run for ANOTHER target has
# resolved - it carries a needle, just not ours - so it is a real
# non-match and `none` is still the right answer. Without that
# distinction the rule would refuse on every page the runner serves,
# since the runner reviews every consumer.
chk "another target's resolved run is a plain non-match, not an unresolved name" \
    "$(dispatch "$FN" "$(rpage "$D_OTHER_REPO")" '' '')" "none||||0"
chk "and a whole page of other targets' runs still answers nothing dispatched" \
    "$(dispatch "$FN" "$(rpage "$D_PR195" "$D_PR1940" "$D_OTHER_REPO")" '' '')" "none||||0"
# And the scope: only a run that can still BE the young one it
# describes. A name renders seconds after creation and long before a
# run finishes, so a completed run carrying no needle is a workflow
# that renders no target into its name - a plain non-match, not a
# block no re-read can clear. The harm the rule prevents is unreachable
# through it: a completed run is not a dispatch about to start, so no
# re-dispatch of this head can cancel it.
D_UNRESOLVED_DONE=$(rrun 39572000000 completed success 2026-10-21T01:06:00Z 'LENS review' https://example.invalid/runner/done-bare)
chk "a completed run with no needle in its name is a plain non-match, not a standing block" \
    "$(dispatch "$FN" "$(rpage "$D_UNRESOLVED_DONE")" '' '')" "none||||0"
chk "and it does not stop this head's own dispatch being read" \
    "$(dispatch "$FN" "$(rpage "$D_UNRESOLVED_DONE" "$D_DIED")" '' '')" "died|39523085356|completed|$DIED_URL|0"
chk "while an unfinished one beside it still stops the locator" \
    "$(dispatch "$FN" "$(rpage "$D_UNRESOLVED_DONE" "$D_YOUNG")" '' '')" "error(1)"
chk "and the refusal counts only the unfinished one" \
    "$(locmsg "$FN" "$(rpage "$D_UNRESOLVED_DONE" "$D_YOUNG")" 'has 1 unfinished run(s)')" 1
# And the bound applies to it as it does to everything else: a young run
# that predates this head is not this head's problem.
D_YOUNG_OLD=$(rrun 39571000000 queued '' 2026-10-20T09:00:00Z 'LENS review' https://example.invalid/runner/young-old)
chk "a name that has not resolved, below the bound, does not stop this head's reading" \
    "$(dispatch "$FN" "$(rpage "$D_YOUNG_OLD" "$D_DIED")" '' '')" "died|39523085356|completed|$DIED_URL|0"

chk "no run at all for this PR: nothing was dispatched" \
    "$(dispatch "$FN" "$(rpage)" '' '')" "none||||0"
chk "only other PRs' runs: still nothing was dispatched" \
    "$(dispatch "$FN" "$(rpage "$D_PR195" "$D_PR1940" "$D_OTHER_REPO")" '' '')" "none||||0"
chk "#1940 is not #194: the filter does not stop at the substring" \
    "$(dispatch "$FN" "$(rpage "$D_PR1940")" '' '')" "none||||0"
chk "the same number on another consumer is another run" \
    "$(dispatch "$FN" "$(rpage "$D_OTHER_REPO")" '' '')" "none||||0"
chk "an owner name ending in this one's is a different owner" \
    "$(dispatch "$FN" "$(rpage "$D_LEFT")" '' '')" "none||||0"
chk "and it does not displace this PR's own dispatch" \
    "$(dispatch "$FN" "$(rpage "$D_LEFT" "$D_DIED")" '' '')" "died|39523085356|completed|$DIED_URL|0"
# Its `name` is the workflow's, so it carries no <owner>/<repo>#<n> at
# all - but it is `completed`, so it cannot be a run too young to have
# rendered its name, and the young-run rule leaves it alone. It is a
# plain non-match, which is what a run of a workflow that renders no
# target into its name is.
chk "a run whose COMMIT SUBJECT names this PR is not this PR's dispatch" \
    "$(dispatch "$FN" "$(rpage "$D_SUBJECT_ONLY")" '' '')" "none||||0"
chk "and it does not displace the real one either" \
    "$(dispatch "$FN" "$(rpage "$D_DIED" "$D_SUBJECT_ONLY")" '' '')" "died|39523085356|completed|$DIED_URL|0"
# The field the function READS, not the prose that explains the choice:
# the comment names display_title where it says why it is not read, so
# the needle is the jq that reads one, with the comment lines dropped.
# The positive companion is in 8f, where a copy that does read it makes
# the same grep answer non-zero.
chk "the code reads .name and never .display_title" \
    "$(code_of "$RUNNER_FN" | grep -c 'display_title' | sed 's/^/n=/')" "n=0"
chk "it does read .name" \
    "$([ "$(code_of "$RUNNER_FN" | grep -c '\.name // ""')" -ge 1 ] && echo found || echo missing)" found
chk "the measured dispatch, with no round discovered here: it died before posting" \
    "$(dispatch "$FN" "$(rpage "$D_DIED")" '' '')" "died|39523085356|completed|$DIED_URL|0"
chk "the same run beside a round that WAS discovered: posted, which the gate cannot reach" \
    "$(dispatch "$FN" "$(rpage "$D_DIED")" '' check-run)" "posted|39523085356|completed|$DIED_URL|0"
chk "a dispatch still in flight is a WAIT, carrying its status" \
    "$(dispatch "$FN" "$(rpage "$D_RUNNING")" '' '')" "running|39530000001|in_progress|https://example.invalid/runner/running|0"
chk "a queued dispatch likewise" \
    "$(dispatch "$FN" "$(rpage "$D_QUEUED")" '' '')" "running|39530000002|queued|https://example.invalid/runner/queued|0"
chk "the newest since the head decides, and it is the WAIT over the dead one" \
    "$(dispatch "$FN" "$(rpage "$D_DIED" "$D_RUNNING")" '' '')" "running|39530000001|in_progress|https://example.invalid/runner/running|0"
# Newest-wins alone answers `died` - the only reading that opens a path
# forward - while a round is in flight; concurrency cancellation makes
# that narrow rather than impossible, and no candidate here posted a
# check run or discovery would have found it, so the running one is
# genuinely in flight. An in-flight candidate therefore outranks a newer
# completed one, and within each group the newest still decides.
chk "an older in-flight run outranks a newer completed one: WAIT, not died" \
    "$(dispatch "$FN" "$(rpage "$D_OLDER_RUNNING" "$D_NEWER_DEAD")" '' '')" \
    "running|39531000000|in_progress|https://example.invalid/runner/older-running|0"
chk "and with no in-flight candidate the newest completed one still decides" \
    "$(dispatch "$FN" "$(rpage "$D_DIED" "$D_NEWER_DEAD")" '' '')" \
    "died|39532000000|completed|https://example.invalid/runner/newer-dead|0"
chk "among several in flight, the newest of those decides" \
    "$(dispatch "$FN" "$(rpage "$D_OLDER_RUNNING" "$D_NEWER_DEAD" "$D_RUNNING")" '' '')" \
    "running|39530000001|in_progress|https://example.invalid/runner/running|0"
chk "the near misses do not displace the real one" \
    "$(dispatch "$FN" "$(rpage "$D_DIED" "$D_PR195" "$D_PR1940" "$D_OTHER_REPO")" '' '')" "died|39523085356|completed|$DIED_URL|0"
# The head bound. The previous head's round is completed and posted - on
# the old SHA - so without the bound it is read as a dispatch that died
# and the operator is sent to kind (ii) in the seconds the `none` arm
# exists to wait out.
chk "the previous head's completed round alone: nothing was dispatched for THIS head" \
    "$(dispatch "$FN" "$(rpage "$D_PREV_HEAD")" '' '')" "none||||1"
chk "and it is counted, not dropped in silence" \
    "$(dispatch "$FN" "$(rpage "$D_PREV_HEAD")" '' '' | cut -d'|' -f5)" 1
chk "the previous head's round beside this head's running one: the WAIT" \
    "$(dispatch "$FN" "$(rpage "$D_PREV_HEAD" "$D_RUNNING")" '' '')" "running|39530000001|in_progress|https://example.invalid/runner/running|1"
chk "the previous head's round beside this head's dead one: the dead one, and one earlier" \
    "$(dispatch "$FN" "$(rpage "$D_PREV_HEAD" "$D_DIED")" '' '')" "died|39523085356|completed|$DIED_URL|1"
chk "two earlier rounds are both counted" \
    "$(dispatch "$FN" "$(rpage "$D_PREV_HEAD" "$(rrun 39500000000 completed failure 2026-10-19T09:00:00Z "LENS review $TREPO#194" https://example.invalid/runner/older-still)" "$D_DIED")" '' '')" \
    "died|39523085356|completed|$DIED_URL|2"
chk "a run created at the head's own push time counts as this head's" \
    "$(dispatch "$FN" "$(rpage "$D_AT_HEAD")" '' '')" "died|39515000000|completed|https://example.invalid/runner/at-head|0"
chk "a run with no created_at cannot be shown to be this head's, so it is earlier" \
    "$(dispatch "$FN" "$(rpage "$(rrun 39500000001 completed failure '' "LENS review $TREPO#194" https://example.invalid/runner/no-date)")" '' '')" "none||||1"
# The bound is the head's, not the branch's: a later head pushed after
# this run moves the line past it.
chk "move the head past the measured run and it stops being this head's dispatch" \
    "$(dispatch "$FN" "$(rpage "$D_DIED")" '' '' 2026-10-21T01:30:00Z)" "none||||1"
chk "the endpoint read is the runner's review.yml runs, uncapped" \
    "$(dispatch "$FN" "$(rpage "$D_DIED")" '' '' > /dev/null; cat "$D/loc-url")" \
    "repos/$RREPO/actions/workflows/review.yml/runs?per_page=100"
# The runner reviews every consumer, so this repository's run is far
# down a list a cap would answer `none` from - discovery's own argument,
# on a list nobody here controls the length of.
chk "a dispatch on page 2 is found, because the read paginates" \
    "$(dispatch "$FN" "$(rpage "$D_PR195")" "$(rpage "$D_DIED")" '')" "died|39523085356|completed|$DIED_URL|0"
chk "the locator caps nothing" "$(extract "$SKILL" "$RUNNER_FN" | grep -c -- '--limit' | sed 's/^/n=/')" "n=0"
chk "and it paginates" "$(extract "$SKILL" "$RUNNER_FN" | grep -c -- 'gh api --paginate')" 1
# A failed read is an error, not a clean `none`: a locator that reported
# "nothing was dispatched" on a 500 would send the operator to
# re-dispatch a round that is already running.
# shellcheck disable=SC1090,SC2034,SC2317  # the function reads RUNNER_REPO and calls gh
loc_read=$( . "$FN"; RUNNER_REPO=$RREPO; gh() { return 1; }
            "$RUNNER_FN" "$TREPO" 194 "$HEAD_TIME" '' > /dev/null 2>&1 && echo ok || echo error )
chk "a failing gh is an error, not a reading" "$loc_read" error
chk "an empty bound is refused, not treated as no bound" "$(nobound "$FN")" "error(1)"
# A bound that is present but unusable is refused for the same reason.
# Every comparison in the locator is lexicographic, so `last Tuesday`
# does not refuse anything - it sorts: every `2026-` stamp lands below
# it, so the function answers `none` with every run counted as earlier,
# which the gate reads as "nothing was dispatched for this head".
# The bound is dispatch()'s fifth argument, so no helper of its own:
# the positive companion first, then the malformed bounds.
chk "the same call reads the dispatch on a well-formed bound" \
    "$(dispatch "$FN" "$(rpage "$D_DIED")" '' '' "$HEAD_TIME")" "died|39523085356|completed|$DIED_URL|0"
for badsince in 'last Tuesday' '2026-10-21' '2026-10-21T00:45:00+09:00' 'nonsense'; do
    chk "a bound of '$badsince' is refused, not sorted against" \
        "$(dispatch "$FN" "$(rpage "$D_DIED")" '' '' "$badsince")" "error(1)"
done
chk "and the refusal says the comparisons cannot be made on it" \
    "$( # shellcheck disable=SC1090,SC2034  # the function reads RUNNER_REPO
       ( . "$FN"; RUNNER_REPO=$RREPO
         # shellcheck disable=SC2317  # called by the sourced function
         gh() { printf '%s\n' '{}'; }
         "$RUNNER_FN" "$TREPO" 194 'last Tuesday' '' 2>&1 >/dev/null ) | grep -c 'not an ISO-8601 UTC timestamp')" 1
chk "and the refusal says why" \
    "$( # shellcheck disable=SC1090,SC2034  # the function reads RUNNER_REPO
       ( . "$FN"; RUNNER_REPO=$RREPO
         # shellcheck disable=SC2317  # called by the sourced function
         gh() { printf '%s\n' '{}'; }
         "$RUNNER_FN" "$TREPO" 194 '' '' 2>&1 >/dev/null ) | grep -c 'was given no bound')" 1
# An envelope of the caller's choosing goes through dispatch() as page
# one: its stub already pipes whatever it is given through the
# function's own --jq, so these need no helper of their own.
#
# A successful response whose ENVELOPE is not what the gate expects must
# not read as an empty one. Measured rather than assumed: `gh api --jq
# '.workflow_runs[] | ...'` over a 200 lacking that key exits 1 with
# `cannot iterate over: null`, so the read's own guard fires. The checks
# below pin that: a malformed envelope is an error, and only a
# genuinely empty one is a real `none`.
chk "a well-formed empty response is a real none" \
    "$(dispatch "$FN" '{"total_count":0,"workflow_runs":[]}' '' '')" "none||||0"
chk "an envelope with no workflow_runs is an error, not a none" \
    "$(dispatch "$FN" '{"total_count":0}' '' '')" "error(1)"
chk "nor is one where it is not an array" \
    "$(dispatch "$FN" '{"workflow_runs":"soon"}' '' '')" "error(1)"
chk "and a 200 that is not the API at all is an error too" \
    "$(dispatch "$FN" '{"message":"Not Found","documentation_url":"https://docs.github.com"}' '' '')" "error(1)"
chk "a run with no status at all is refused, not read as in flight" \
    "$(dispatch "$FN" "$(rpage "$D_NOSTATUS")" '' '')" "error(1)"
# An unfamiliar status is NOT an error: a run can legitimately sit in a
# lifecycle state this gate has never seen, and an allow-list turns that
# into a hard block on a normal round. The asymmetry is the argument - a
# value missing from an allow-list makes a normal state an error, one
# missing from a rejection makes a broken record a WAIT, and WAIT blocks.
chk "an empty status is refused too, for the same reason as a missing one" \
    "$(dispatch "$FN" "$(rpage "$(rrun 39535000000 '' '' 2026-10-21T01:00:00Z "LENS review $TREPO#194" https://example.invalid/runner/blank)")" '' '')" "error(1)"
# And the refusal is bounded like `newest`: a statusless run BELOW the
# bound reaches the `earlier` count and nothing else, so it can never be
# read as a round in flight and must not block this head's gate. The
# healthy candidate above the bound still decides, and the statusless
# one is counted.
D_NOSTATUS_OLD=$(command jq -cn --arg d "LENS review $TREPO#194" \
  '{id: 39534000000, name: $d, head_branch: "x", conclusion: null, created_at: "2026-10-20T09:00:00Z", updated_at: "x", display_title: $d, html_url: "https://example.invalid/runner/nostatus-old"}')
chk "a statusless run below the bound does not block the gate on this head" \
    "$(dispatch "$FN" "$(rpage "$D_NOSTATUS_OLD" "$D_DIED")" '' '')" "died|39523085356|completed|$DIED_URL|1"
chk "and it is still counted among the runs that predate this head" \
    "$(dispatch "$FN" "$(rpage "$D_NOSTATUS_OLD" "$D_DIED")" '' '' | cut -d'|' -f5)" 1
chk "while a statusless run above the bound still refuses" \
    "$(dispatch "$FN" "$(rpage "$D_NOSTATUS_OLD" "$D_NOSTATUS")" '' '')" "error(1)"
for unknown in waiting requested pending hibernating; do
    chk "a '$unknown' run is read as in flight, not refused" \
        "$(dispatch "$FN" "$(rpage "$(rrun 39536000000 "$unknown" '' 2026-10-21T01:00:00Z "LENS review $TREPO#194" https://example.invalid/runner/$unknown)")" '' '')" \
        "running|39536000000|$unknown|https://example.invalid/runner/$unknown|0"
done
chk "and completed still ends the round" \
    "$(dispatch "$FN" "$(rpage "$D_DIED")" '' '')" "died|39523085356|completed|$DIED_URL|0"
chk "and the refusal says what was missing, not what was unfamiliar" \
    "$(locmsg "$FN" "$(rpage "$D_NOSTATUS")" 'carrying no status at all')" 1
chk "a mixed page still picks the in-flight one" \
    "$(dispatch "$FN" "$(rpage "$D_DIED" "$D_RUNNING" "$D_QUEUED")" '' '')" \
    "running|39530000002|queued|https://example.invalid/runner/queued|0"
# And the text saying WHICH read failed must survive the caller's
# command substitution, which captures stdout alone. Every other
# `ERROR:` in the block is top level and reaches the terminal; these are
# inside a function whose stdout is the tuple, so they go to stderr.
# shellcheck disable=SC1090,SC2034,SC2317  # the function reads RUNNER_REPO and calls gh
loc_err=$( . "$FN"; RUNNER_REPO=$RREPO; gh() { return 1; }
           out=$("$RUNNER_FN" "$TREPO" 194 "$HEAD_TIME" '' 2>/dev/null)
           [ -z "$out" ] && echo "stdout-clean" || echo "swallowed: $out" )
chk "the failure text is not swallowed into the tuple the caller captures" "$loc_err" stdout-clean
# shellcheck disable=SC1090,SC2034,SC2317  # the function reads RUNNER_REPO and calls gh
loc_msg=$( . "$FN"; RUNNER_REPO=$RREPO; gh() { return 1; }
           "$RUNNER_FN" "$TREPO" 194 "$HEAD_TIME" '' 2>&1 >/dev/null | grep -c "could not list the runs of $RREPO's .github/workflows/review.yml" )
chk "and it does reach stderr, naming the read that failed" "$loc_msg" 1
# The failure is already loud; what it did not say was WHAT it tried to
# read. The filename is fixed in the block while the repository is an
# operator literal, so a runner that names its review workflow otherwise
# 404s here and the block is what has to change.
# shellcheck disable=SC1090,SC2034,SC2317  # the function reads RUNNER_REPO and calls gh
loc_why=$( . "$FN"; RUNNER_REPO=$RREPO; gh() { return 1; }
           "$RUNNER_FN" "$TREPO" 194 "$HEAD_TIME" '' 2>&1 >/dev/null | grep -c 'fixed in this block, not an operator literal' )
chk "and says the workflow filename is not an operator literal" "$loc_why" 1
# The same rule for the reads that never touch the network. An unguarded
# jq falls through with an empty value and returns 0: an empty `newest`
# takes the `none` path, which the gate reports as "nothing was
# dispatched", and an empty `earlier` turns the caller's `-eq 0` into a
# non-numeric test.
chk "the stub itself reads the dispatch when no jq call is failed" \
    "$(jqfail "$FN" 0)" "died|39523085356|completed|$DIED_URL|0"
chk "a failing matched read is an error, not a dispatch" "$(jqfail "$FN" 1)" "error(1)"
chk "a failing unresolved-name read is an error, not a clean none" "$(jqfail "$FN" 2)" "error(1)"
chk "a failing earlier read is an error, not a clean none" "$(jqfail "$FN" 3)" "error(1)"
chk "a failing status validation is an error, not a dispatch" "$(jqfail "$FN" 4)" "error(1)"
chk "a failing newest read is an error, not a clean none" "$(jqfail "$FN" 5)" "error(1)"

echo "5l. the gate's own arm: the locator is read on the [] row and nowhere else"
LOC_ARM="$D/gate-locator-arm.sh"
# shellcheck disable=SC2016  # matched literally: the test as SKILL.md spells it
sed -n '/^if \[ -n "\$round_surface" \]; then$/,/^fi$/p' "$SKILL" > "$LOC_ARM"
# And the fenced block the arm lives in, picked out by the arm itself:
# SKILL.md carries more than one pasteable block, so a grep over the
# whole file counts lines belonging to blocks this arm is not in.
GATE_BLOCK="$D/gate-block.sh"
# shellcheck disable=SC2016  # matched literally: the elif as SKILL.md spells it
awk '/^```bash$/ { inb = 1; buf = ""; next }
     /^```$/     { if (inb && buf ~ /elif \[ -n "\$RUNNER_REPO" \]; then/) printf "%s", buf
                   inb = 0; buf = ""; next }
     inb         { buf = buf $0 "\n" }' "$SKILL" > "$GATE_BLOCK"
# shellcheck disable=SC2016  # matched literally: the elif as SKILL.md spells it
chk "exactly one fenced block carries the locator's arm, and that is the one extracted" \
    "$(grep -c '^elif \[ -n "\$RUNNER_REPO" \]; then$' "$GATE_BLOCK")" 1
chk "and the block holds the locator it calls" "$(grep -c '^runner_dispatch() {$' "$GATE_BLOCK")" 1
# The walk's page cap is declared with the block's other literals, and
# the drill reads the number from there rather than carrying its own
# copy: a second copy is the thing that can drift. Everything below
# that asserts the cap compares against this value.
FPUSH_CAP=$(sed -n 's/^FPUSH_PAGE_CAP=\([0-9][0-9]*\)$/\1/p' "$GATE_BLOCK")
chk "the block declares the page cap as a named literal" \
    "$(printf '%s' "$FPUSH_CAP" | grep -c '^[0-9][0-9]*$')" 1
# shellcheck disable=SC2016  # matched literally: the loop as SKILL.md spells it
chk "and the walk tests against that name, not against a number of its own" \
    "$(grep -c 'while \[ "\$fpush_pages" -lt "\$FPUSH_PAGE_CAP" \]; do' "$LOC_ARM")" 1
chk "the gate's discovery arm was isolated" "$(grep -c 'runner_dispatch OWNER/REPO N' "$LOC_ARM")" 1
# shellcheck disable=SC2016  # matched literally: the elif as SKILL.md spells it
chk "and the locator sits in the elif of the round test" \
    "$(grep -c '^elif \[ -n "\$RUNNER_REPO" \]; then$' "$LOC_ARM")" 1
# Named for what it greps: the committer date is the FALLBACK half of
# the bound, the half the arm marks unauthoritative. Calling it the push
# time here would restate, in a test name, the conflation the arm
# closes. The push-stamp half is named by the check "with the right
# field the push stamp is the bound".
# shellcheck disable=SC2016  # matched literally: the read as SKILL.md spells it
chk "the arm reads the committer date - the fallback half of the bound - on the path where it is the bound" \
    "$(grep -c 'commits/"\$HEAD_SHA" --jq \.commit\.committer\.date' "$LOC_ARM")" 1
T_NONE=$(printf 'none\037\037\037\0370')
T_RUNNING=$(printf 'running\03739530000001\037in_progress\037https://example.invalid/runner/running\0370')
T_DIED=$(printf 'died\03739523085356\037completed\037%s\0370' "$DIED_URL")
T_POSTED=$(printf 'posted\03739523085356\037completed\037%s\0370' "$DIED_URL")
T_NONE_STALE=$(printf 'none\037\037\037\0372')
T_DIED_NOCOUNT=$(printf 'died\03739523085356\037completed\037%s\037' "$DIED_URL")
T_DIED_BADCOUNT=$(printf 'died\03739523085356\037completed\037%s\037many' "$DIED_URL")
chk "a round on either surface: the locator is not read at all" \
    "$(arm "$LOC_ARM" check-run "$RREPO" "$T_POSTED")" skipped
chk "RUNNER_REPO empty: this repository reviews itself, so there is nowhere to look" \
    "$(arm "$LOC_ARM" '' '' "$T_DIED")" skipped
# An empty RUNNER_REPO is a valid value and is byte-identical to having
# forgotten to fill it: both skip the locator and leave the same [] row.
# The gate prints which reading it took and what would change it, so the
# two are told apart by the operator even though the configuration
# cannot tell them apart by itself - the same unset-versus-deliberately-
# empty shape this arm closes in its reads, here on the configuration
# surface.
chk "and the gate says so, rather than leaving the row silent" \
    "$(arm "$LOC_ARM" '' '' "$T_DIED" > /dev/null; grep -c "RUNNER_REPO is empty, taken as 'this repository reviews itself'" "$D/arm.out")" 1
chk "and names what to do if that reading is wrong" \
    "$(arm "$LOC_ARM" '' '' "$T_DIED" > /dev/null; grep -c "fill it from this repository.s AGENTS.md (Review routing rule 5)" "$D/arm.out")" 1
# The line ends with a decision, as its siblings do: what the operator
# is to DO, not only what the gate concluded.
chk "and it ends with the decision, as its siblings do" \
    "$(arm "$LOC_ARM" '' '' "$T_DIED" > /dev/null; grep -c 'Do not merge on a round that is not there' "$D/arm.out")" 1
chk "naming the wait and the path left when it stays empty" \
    "$(arm "$LOC_ARM" '' '' "$T_DIED" > /dev/null
       printf '%s/%s' "$(grep -c 'Wait and re-ask' "$D/arm.out")" "$(grep -c 'local review (kind (ii)) is the path' "$D/arm.out")")" "1/1"
chk "with it filled, that line is not printed" \
    "$(arm "$LOC_ARM" '' "$RREPO" "$T_DIED" > /dev/null; grep -c 'RUNNER_REPO is empty' "$D/arm.out")" 0
chk "and with a round discovered neither branch of the row runs at all" \
    "$(arm "$LOC_ARM" check-run '' "$T_DIED" > /dev/null; grep -c 'RUNNER_REPO is empty' "$D/arm.out")" 0
chk "the [] row with a runner in front of it: the locator is read" \
    "$(arm "$LOC_ARM" '' "$RREPO" "$T_NONE")" consulted
chk "and it is given a time bound, not just the PR number" \
    "$(arm "$LOC_ARM" '' "$RREPO" "$T_NONE" > /dev/null; grep -c "OWNER/REPO N $HEAD_TIME" "$D/loc-called")" 1
# The bound is GitHub's stamp, not the commit's: the force-push event's
# createdAt for this SHA, and the committer date only where there is no
# event, because that one is the client's clock. The rule and the
# measurements behind it are step 8's, in SKILL.md - the one home; the
# fixtures here sit on one side of the bound or the other without
# restating it.
chk "a backdated committer date cannot loosen the bound: with a stamp the gate does not read it" \
    "$(arm "$LOC_ARM" '' "$RREPO" "$T_NONE" 2026-10-01T00:00:00Z > /dev/null; grep -c "OWNER/REPO N $HEAD_TIME" "$D/loc-called")" 1
# A committer date LATER than the stamp does not move the bound either.
# Taking the later of the two would make a future-dated
# GIT_COMMITTER_DATE (or ordinary clock skew) the bound while the label
# still said "the head's push stamp", and would push the bound past an
# in-flight dispatch, so the `none` arm would advise a re-dispatch the
# next line warns cancels a running round. These read the output, not
# only what the locator received.
chk "a future-dated committer date does not move the bound: the stamp is the whole of it" \
    "$(arm "$LOC_ARM" '' "$RREPO" "$T_NONE" 2026-10-25T00:00:00Z > /dev/null; grep -c "OWNER/REPO N $HEAD_TIME" "$D/loc-called")" 1
chk "and the label the output prints is the value the locator got" \
    "$(arm "$LOC_ARM" '' "$RREPO" "$T_NONE" 2026-10-25T00:00:00Z > /dev/null; grep -c "since $HEAD_TIME (the head.s push stamp)" "$D/arm.out")" 1
chk "the future date appears nowhere in what the operator reads" \
    "$(arm "$LOC_ARM" '' "$RREPO" "$T_NONE" 2026-10-25T00:00:00Z > /dev/null; grep -c '2026-10-25' "$D/arm.out")" 0
# Named for what it measures, which is the harm and not a guarantee:
# against a bound skewed into the future an in-flight dispatch DOES fall
# below it, into the earlier count, and the gate then reports `none`.
chk "a skewed bound would push an in-flight dispatch into the earlier count, which is why the stamp is the whole bound" \
    "$(dispatch "$FN" "$(rpage "$D_RUNNING")" '' '' 2026-10-25T00:00:00Z)" "none||||1"
chk "and on the bound the arm actually uses, the same dispatch is the WAIT" \
    "$(dispatch "$FN" "$(rpage "$D_RUNNING")" '' '' "$HEAD_TIME")" "running|39530000001|in_progress|https://example.invalid/runner/running|0"
# The committer label appears only where the committer date is the
# bound, and then it is the value printed beside it.
chk "with no stamp the label and the value are the committer date, together" \
    "$(arm "$LOC_ARM" '' "$RREPO" "$T_NONE" "$HEAD_COMMIT_TIME" '' > /dev/null; grep -c "since $HEAD_COMMIT_TIME (the committer date, not a push stamp" "$D/arm.out")" 1
chk "and the push-stamp label appears nowhere on that path" \
    "$(arm "$LOC_ARM" '' "$RREPO" "$T_NONE" "$HEAD_COMMIT_TIME" '' > /dev/null; grep -c "the head.s push stamp" "$D/arm.out")" 0
chk "a force-push event for another SHA is not this head's push" \
    "$(arm "$LOC_ARM" '' "$RREPO" "$T_NONE" "$HEAD_COMMIT_TIME" "$(fpush 00000000000000000000000000000000000000ff 2026-10-21T06:00:00Z)" > /dev/null; grep -c "OWNER/REPO N $HEAD_COMMIT_TIME" "$D/loc-called")" 1
chk "the newest force-push event for this head decides among several" \
    "$(arm "$LOC_ARM" '' "$RREPO" "$T_NONE" "$HEAD_COMMIT_TIME" "$(fpush "$ARM_HEAD" 2026-10-21T00:45:00Z)
$(fpush "$ARM_HEAD" 2026-10-21T07:00:00Z)" > /dev/null; grep -c 'OWNER/REPO N 2026-10-21T07:00:00Z' "$D/loc-called")" 1
# No force-push event at all - a branch's first push, or an ordinary
# one. The committer date is then the whole bound, and the gate says so
# rather than letting the reader assume a server stamp.
chk "with no force-push event the bound falls back to the committer date" \
    "$(arm "$LOC_ARM" '' "$RREPO" "$T_NONE" "$HEAD_COMMIT_TIME" '' > /dev/null; grep -c "OWNER/REPO N $HEAD_COMMIT_TIME" "$D/loc-called")" 1
chk "and the gate states the limit instead of overclaiming a push time" \
    "$(arm "$LOC_ARM" '' "$RREPO" "$T_NONE" "$HEAD_COMMIT_TIME" '' > /dev/null; grep -c 'the bound below is the committer date, which the commit.s author can set, so it cannot say a completed run belongs to this head' "$D/arm.out")" 1
# Fail closed. `died` is the one reading that opens a path forward
# instead of blocking, so it is the one the bound must be trusted for,
# and WAIT and re-dispatch block either way.
chk "with no server stamp, a completed run is NOT read as a dead dispatch" \
    "$(arm "$LOC_ARM" '' "$RREPO" "$T_DIED" "$HEAD_COMMIT_TIME" '' > /dev/null; grep -c 'cannot say it is THIS head.s dispatch' "$D/arm.out")" 1
chk "and the kind (ii) line is not offered on it" \
    "$(arm "$LOC_ARM" '' "$RREPO" "$T_DIED" "$HEAD_COMMIT_TIME" '' > /dev/null; grep -c 'local review (kind (ii))' "$D/arm.out")" 0
# The committer branch carries its siblings' two protections: the
# re-dispatch warning the `none` and `running` branches print under the
# same bound, and a command for the question the line itself raises -
# which head was this run dispatched for.
chk "the died committer branch warns before re-dispatching, as its siblings do" \
    "$(arm "$LOC_ARM" '' "$RREPO" "$T_DIED" "$HEAD_COMMIT_TIME" '' > /dev/null; grep -c 'a second dispatch cancels one that is running' "$D/arm.out")" 1
chk "and prints the command that answers which head it was for" \
    "$(arm "$LOC_ARM" '' "$RREPO" "$T_DIED" "$HEAD_COMMIT_TIME" '' > /dev/null; grep -c "gh run view 39523085356 --repo $RREPO\$" "$D/arm.out")" 1
chk "while the server branch still prints the log-failed form instead" \
    "$(arm "$LOC_ARM" '' "$RREPO" "$T_DIED" > /dev/null
       printf '%s/%s' "$(grep -c 'log-failed' "$D/arm.out")" "$(grep -c 'a second dispatch cancels one that is running' "$D/arm.out")")" "1/0"
chk "nor the log-failed command on the committer path" \
    "$(arm "$LOC_ARM" '' "$RREPO" "$T_DIED" "$HEAD_COMMIT_TIME" '' > /dev/null; grep -c 'log-failed' "$D/arm.out")" 0
chk "with the server stamp the same tuple IS the dead dispatch" \
    "$(arm "$LOC_ARM" '' "$RREPO" "$T_DIED" > /dev/null; grep -c 'the runner could not produce a verdict' "$D/arm.out")" 1
# WAIT blocks either way, so it survives an unauthoritative bound - but
# on that bound a running round may be the PREVIOUS head's, which posts
# on the old SHA and can never resolve this wait, while the line's own
# warning discourages the re-dispatch that would end it. The arm says
# which it is.
chk "a WAIT is still a WAIT without the server stamp" \
    "$(arm "$LOC_ARM" '' "$RREPO" "$T_RUNNING" "$HEAD_COMMIT_TIME" '' > /dev/null; grep -c 'WAIT, but read which head it was dispatched for' "$D/arm.out")" 1
chk "and it names the run so the operator can read which head it was for" \
    "$(arm "$LOC_ARM" '' "$RREPO" "$T_RUNNING" "$HEAD_COMMIT_TIME" '' > /dev/null; grep -c "gh run view 39530000001 --repo $RREPO\$" "$D/arm.out")" 1
chk "with the stamp it is the plain WAIT, with no such caveat" \
    "$(arm "$LOC_ARM" '' "$RREPO" "$T_RUNNING" > /dev/null
       printf '%s/%s' "$(grep -c 'WAIT for it. Do NOT re-dispatch' "$D/arm.out")" "$(grep -c 'may be the previous head' "$D/arm.out")")" "1/0"
# The `none`, `running` and `died` readings each consult the bound, so
# each is asserted on the server path and on the committer path.
chk "and no-dispatch on the committer path does not assert that nothing was dispatched" \
    "$(arm "$LOC_ARM" '' "$RREPO" "$T_NONE" "$HEAD_COMMIT_TIME" '' > /dev/null
       printf '%s/%s' "$(grep -c 'this is not proof that nothing was dispatched' "$D/arm.out")" "$(grep -c 'nothing was dispatched for it' "$D/arm.out")")" "1/0"
chk "and it warns before re-dispatching, which the server path does not need to" \
    "$(arm "$LOC_ARM" '' "$RREPO" "$T_NONE" "$HEAD_COMMIT_TIME" '' > /dev/null; grep -c 'a second dispatch cancels one that is running' "$D/arm.out")" 1
chk "on the server path it is the plain re-dispatch advice" \
    "$(arm "$LOC_ARM" '' "$RREPO" "$T_NONE" > /dev/null
       printf '%s/%s' "$(grep -c 'nothing was dispatched for it' "$D/arm.out")" "$(grep -c 'not proof that nothing was dispatched' "$D/arm.out")")" "1/0"
# The count is printed where no event matched, and only there: where the
# event was found, the window demonstrably held it.
chk "the no-event line reports how many events were actually examined" \
    "$(arm "$LOC_ARM" '' "$RREPO" "$T_NONE" "$HEAD_COMMIT_TIME" "$(fpush 00000000000000000000000000000000000000ff 2026-10-20T00:00:00Z)" > /dev/null; grep -c 'among the 1 read on OWNER/REPO#N' "$D/arm.out")" 1
# `$6` empty means "a PR with no force-push events", not "use the
# default" - only an omitted argument takes the default - so the server
# path is asked for by passing the fixture explicitly.
chk "and not on the server path, where it would answer nothing" \
    "$(arm "$LOC_ARM" '' "$RREPO" "$T_NONE" "$HEAD_COMMIT_TIME" "$(fpush "$ARM_HEAD" "$HEAD_TIME")" > /dev/null; grep -c 'read on OWNER/REPO#N' "$D/arm.out")" 0
chk "and that really was the server path" \
    "$(arm "$LOC_ARM" '' "$RREPO" "$T_NONE" "$HEAD_COMMIT_TIME" "$(fpush "$ARM_HEAD" "$HEAD_TIME")" > /dev/null; grep -c "since $HEAD_TIME (the head.s push stamp)" "$D/arm.out")" 1
# The force-push match is an equality against the full 40-character oid,
# so an abbreviated or whitespace-carrying HEAD_SHA would match nothing
# and read as an absent event. The block already refuses that where
# HEAD_SHA is assigned, and nothing held that refusal in place until the
# locator came to depend on it.
# shellcheck disable=SC2016  # matched literally: the test as SKILL.md spells it
chk "the block refuses a HEAD_SHA that is not a full 40-character object name" \
    "$(grep -c '^\[ "${#HEAD_SHA}" -eq 40 \] ||' "$GATE_BLOCK")" 1
# The query selection is a text check, because the stub's envelope
# always carries pageInfo and no behavioural case can tell whether the
# QUERY asks for it. Its positive companion is in 8f.
chk "the arm's GraphQL query selects hasPreviousPage and startCursor, which no fixture can supply for it" \
    "$(grep -c 'pageInfo { hasPreviousPage startCursor }' "$LOC_ARM")" 1
chk "the arm's query asks for a window of 100" \
    "$(grep -c 'timelineItems(last:100, before:' "$LOC_ARM")" 1
# The walk pages backwards with a cursor. `last:` pages the FILTERED
# connection, so an event older than the first window is reachable, and
# a gate that reported it as absent was reporting a window, not an
# answer.
ARM_WINDOW_NODES=$( { for i in $(seq 1 100); do fpush "00000000000000000000000000000000000000$(printf '%02d' $((i % 100)))" "2026-10-19T$(printf '%02d' $((i % 24))):00:00Z"; done
                     fpush "$ARM_HEAD" "$HEAD_TIME"; } )
chk "the window fixture really holds 101 events, the newest this head's" \
    "$(printf '%s\n' "$ARM_WINDOW_NODES" | grep -c .)" 101
chk "an event inside the first page is found in one read, with no cursor" \
    "$(arm "$LOC_ARM" '' "$RREPO" "$T_NONE" "$HEAD_COMMIT_TIME" "$ARM_WINDOW_NODES" > /dev/null; tr '\n' ' ' < "$D/arm-windows" | sed 's/ *$//')" "100 <none>"
# Now the head's event is the OLDEST of 101, so the first window of 100
# misses it and the second must find it.
ARM_WINDOW_OLD=$( { fpush "$ARM_HEAD" "$HEAD_TIME"
                    for i in $(seq 1 100); do fpush "00000000000000000000000000000000000000$(printf '%02d' $((i % 100)))" "2026-10-25T$(printf '%02d' $((i % 24))):00:00Z"; done; } )
chk "an event beyond the first page is found by paging, not reported absent" \
    "$(arm "$LOC_ARM" '' "$RREPO" "$T_NONE" "$HEAD_COMMIT_TIME" "$ARM_WINDOW_OLD" > /dev/null; grep -c "since $HEAD_TIME (the head.s push stamp)" "$D/arm.out")" 1
chk "and it took two reads to do it, the second carrying the first's startCursor" \
    "$(arm "$LOC_ARM" '' "$RREPO" "$T_NONE" "$HEAD_COMMIT_TIME" "$ARM_WINDOW_OLD" > /dev/null; tr '\n' ' ' < "$D/arm-windows" | sed 's/ *$//')" "100 <none> 100 c1"
chk "and the window never moves off 100, so no ceiling is ever approached" \
    "$(arm "$LOC_ARM" '' "$RREPO" "$T_NONE" "$HEAD_COMMIT_TIME" "$ARM_WINDOW_OLD" > /dev/null; cut -d' ' -f1 < "$D/arm-windows" | sort -u | tr '\n' ' ' | sed 's/ *$//')" "100"
chk "the stub accepts the ceiling itself" \
    "$(gqlenv "$(fpush "$ARM_HEAD" "$HEAD_TIME")" 250 > /dev/null 2>&1 && echo ok || echo refused)" ok
chk "and refuses a window past it, as the API does" \
    "$(gqlenv "$(fpush "$ARM_HEAD" "$HEAD_TIME")" 300 > /dev/null 2>&1 && echo ok || echo refused)" refused
chk "naming the error the API names" \
    "$(gqlenv "$(fpush "$ARM_HEAD" "$HEAD_TIME")" 300 2>&1 >/dev/null | grep -c 'EXCESSIVE_PAGINATION')" 1
# The cap. A loop in a block pasted into someone else's shell must
# terminate on its own, and reaching the cap without a match is not an
# absent event: the bound falls to the committer date, unauthoritative,
# and the message says how many were read and that older ones remain.
chk "the walk stops at the page cap rather than looping" \
    "$(arm "$LOC_ARM" '' "$RREPO" "$T_NONE" "$HEAD_COMMIT_TIME" "$(fpush 00000000000000000000000000000000000000ff 2026-10-20T00:00:00Z)" '' '' force > /dev/null; grep -c . "$D/arm-windows")" "$FPUSH_CAP"
# And the loop really reads the declared value: driven with a different
# cap it stops at that one instead. Without this the assertion above
# would pass over a loop that had gone back to a literal equal to
# whatever the block happens to declare.
chk "driven with a different cap, the walk stops at the cap it was given" \
    "$(ARM_CAP=3 arm "$LOC_ARM" '' "$RREPO" "$T_NONE" "$HEAD_COMMIT_TIME" "$(fpush 00000000000000000000000000000000000000ff 2026-10-20T00:00:00Z)" '' '' force > /dev/null; grep -c . "$D/arm-windows")" 3
chk "and that is a different number from the block's own" \
    "$([ "$FPUSH_CAP" = 3 ] && echo same || echo differs)" differs
# A cap the loop cannot walk with is refused by name, not walked past.
# `[ 0 -lt "" ]` and `[ 0 -lt x ]` exit non-zero, so the loop would end
# on its first test having read nothing, and the stamp would come back
# empty exactly as it does for a PR with no force-push event: the bound
# falls to the committer date and the gate reports "among the 0 read"
# as though it had looked. Safe direction, silent - the pairing this
# arm exists to refuse.
# `00` and `000` are here because the guard used to match zero as a
# literal and these passed it: all digits, non-empty, not the string
# `0`. They are not an attempt to enumerate zero's spellings - the
# arithmetic test makes enumeration unnecessary - they are what proves
# the arithmetic bites.
for badcap in unset x 0 00 000; do
    chk "a cap of '$badcap' is refused rather than walked past" \
        "$(ARM_CAP=$badcap arm "$LOC_ARM" '' "$RREPO" "$T_NONE")" "exit(1)"
    chk "and the refusal names the cap it got, for '$badcap'" \
        "$(ARM_CAP=$badcap arm "$LOC_ARM" '' "$RREPO" "$T_NONE" > /dev/null; grep -c 'not a positive page count' "$D/arm.out")" 1
    chk "and it does not claim this head has no force-push event, for '$badcap'" \
        "$(ARM_CAP=$badcap arm "$LOC_ARM" '' "$RREPO" "$T_NONE" > /dev/null; grep -c 'no force-push event' "$D/arm.out")" 0
done
chk "while the block's own cap walks and finds the stamp" \
    "$(arm "$LOC_ARM" '' "$RREPO" "$T_NONE" > /dev/null; grep -c "since $HEAD_TIME (the head.s push stamp)" "$D/arm.out")" 1
chk "and at the page cap it falls to the committer date rather than claiming no event" \
    "$(arm "$LOC_ARM" '' "$RREPO" "$T_NONE" "$HEAD_COMMIT_TIME" "$(fpush 00000000000000000000000000000000000000ff 2026-10-20T00:00:00Z)" '' '' force > /dev/null; grep -c "since $HEAD_COMMIT_TIME (the committer date" "$D/arm.out")" 1
chk "the no-event line says whether older events remain beyond what was read" \
    "$(arm "$LOC_ARM" '' "$RREPO" "$T_NONE" "$HEAD_COMMIT_TIME" '' '' '' force > /dev/null; grep -c 'older ones remain: true' "$D/arm.out")" 1
# The needle is the caveat's OWN words: `no force-push event` is proven
# to be a reading by the no-event checks in this section that expect to
# FIND it, and by its positive companion in 8f, which prints it although
# a server stamp exists.
chk "with the event present that caveat is not printed" \
    "$(arm "$LOC_ARM" '' "$RREPO" "$T_NONE" > /dev/null; grep -c 'no force-push event' "$D/arm.out")" 0
# A BROKEN read is not an absent event. As one pipeline inside a command
# substitution the assignment's status would be the last command's, so a
# GraphQL read that failed would arrive as an empty head_push_time, the
# guard would be dead code, the bound would collapse to the committer
# date, and the gate would state as fact that there was no force-push
# event. `set -o pipefail` is no fix: the block is pasted into a shell
# whose options it does not control.
chk "a failing force-push read exits, rather than reading as no event" \
    "$(arm "$LOC_ARM" '' "$RREPO" "$T_NONE" "$HEAD_COMMIT_TIME" "$(fpush "$ARM_HEAD" "$HEAD_TIME")" graphql)" "exit(1)"
chk "and it says so, naming the page, instead of printing the no-event caveat" \
    "$(arm "$LOC_ARM" '' "$RREPO" "$T_NONE" "$HEAD_COMMIT_TIME" "$(fpush "$ARM_HEAD" "$HEAD_TIME")" graphql > /dev/null
       printf '%s/%s' "$(grep -c 'could not read page 1 of the force-push events' "$D/arm.out")" "$(grep -c 'no force-push event' "$D/arm.out")")" "1/0"
# The guard does not name causes it cannot tell apart; it points at gh's
# own message, which is on the terminal because only stdout is captured.
chk "it points at gh's own message instead" \
    "$(grep -c "gh printed why above" "$LOC_ARM")" 1
chk "the locator is never reached on that path" \
    "$(arm "$LOC_ARM" '' "$RREPO" "$T_NONE" "$HEAD_COMMIT_TIME" "$(fpush "$ARM_HEAD" "$HEAD_TIME")" graphql > /dev/null; [ -e "$D/loc-called" ] && echo called || echo not-called)" not-called
# The same rule one level out: the arm's own reads of `fpush_raw` get
# the guards the locator's reads have, because a failed jq there is
# byte-identical to the legitimate absence of a force-push event.
chk "a failing head_push_time read exits, rather than reading as no event" \
    "$(arm "$LOC_ARM" '' "$RREPO" "$T_NONE" '' '' '' '' '' 1)" "exit(1)"
chk "and says an unreadable answer is not an absent event" \
    "$(arm "$LOC_ARM" '' "$RREPO" "$T_NONE" '' '' '' '' '' 1 > /dev/null
       printf '%s/%s' "$(grep -c 'could not read the force-push stamp' "$D/arm.out")" "$(grep -c 'no force-push event' "$D/arm.out")")" "1/0"
chk "a failing fpush_seen read exits too" \
    "$(arm "$LOC_ARM" '' "$RREPO" "$T_NONE" '' '' '' '' '' 2)" "exit(1)"
chk "a failing fpush_more read exits too" \
    "$(arm "$LOC_ARM" '' "$RREPO" "$T_NONE" '' '' '' '' '' 3)" "exit(1)"
chk "and says which read it was" \
    "$(arm "$LOC_ARM" '' "$RREPO" "$T_NONE" '' '' '' '' '' 3 > /dev/null; grep -c 'could not read whether older force-push events remain' "$D/arm.out")" 1
chk "a failing cursor read exits too" \
    "$(arm "$LOC_ARM" '' "$RREPO" "$T_NONE" '' '' '' '' '' 4)" "exit(1)"
chk "and says that a walk which cannot continue is not an exhausted one" \
    "$(arm "$LOC_ARM" '' "$RREPO" "$T_NONE" '' '' '' '' '' 4 > /dev/null; grep -c 'could not read the cursor for the next page' "$D/arm.out")" 1
chk "neither reaches the locator" \
    "$(arm "$LOC_ARM" '' "$RREPO" "$T_NONE" '' '' '' '' '' 1 > /dev/null; [ -e "$D/loc-called" ] && echo called || echo not-called)" not-called
chk "a stamp that is not an ISO-8601 UTC timestamp is refused" \
    "$(arm "$LOC_ARM" '' "$RREPO" "$T_NONE" '' "$(fpush "$ARM_HEAD" 'last Tuesday')")" "exit(1)"
chk "and a committer date that is not one either, on the path that reads it" \
    "$(arm "$LOC_ARM" '' "$RREPO" "$T_NONE" 'not-a-date' '')" "exit(1)"
# And it is read ONLY on that path: with a server stamp the gate never
# asks for it, so a committer date the API cannot return costs nothing.
chk "with a force-push stamp the committer date is not read at all" \
    "$(arm "$LOC_ARM" '' "$RREPO" "$T_NONE" 'not-a-date')" consulted
chk "nor is a failing committer-date read able to block a head whose stamp was read" \
    "$(arm "$LOC_ARM" '' "$RREPO" "$T_NONE" "$HEAD_COMMIT_TIME" "$(fpush "$ARM_HEAD" "$HEAD_TIME")" commit)" consulted
chk "while the well-formed pair still passes" \
    "$(arm "$LOC_ARM" '' "$RREPO" "$T_NONE")" consulted
chk "a failing committer-date read exits on the path that needs it" \
    "$(arm "$LOC_ARM" '' "$RREPO" "$T_NONE" "$HEAD_COMMIT_TIME" '' commit)" "exit(1)"
chk "and says what it could not read" \
    "$(arm "$LOC_ARM" '' "$RREPO" "$T_NONE" "$HEAD_COMMIT_TIME" '' commit > /dev/null; grep -c 'could not read the committer date' "$D/arm.out")" 1
chk "unpiped, the read's own message is what the operator gets" \
    "$(arm "$LOC_ARM" '' "$RREPO" "$T_NONE" "$HEAD_COMMIT_TIME" "$(fpush "$ARM_HEAD" "$HEAD_TIME")" graphql > /dev/null; grep -c 'could not read page 1 of the force-push events' "$D/arm.out")" 1
chk "with the right field the push stamp is the bound" \
    "$(arm "$LOC_ARM" '' "$RREPO" "$T_NONE" > /dev/null; grep -c "OWNER/REPO N $HEAD_TIME" "$D/loc-called")" 1
# The case's default arm: a reading the gate does not know is one more
# loud failure, not a row passed over on the way to the comment count.
chk "an unknown locator reading exits rather than printing nothing" \
    "$(arm "$LOC_ARM" '' "$RREPO" "$(printf 'whatever\037\037\037\0370')")" "exit(1)"
chk "and it names what it was given" \
    "$(arm "$LOC_ARM" '' "$RREPO" "$(printf 'whatever\037\037\037\0370')" > /dev/null; grep -c "runner_dispatch returned 'whatever'" "$D/arm.out")" 1
# An entirely empty tuple is caught before the kind is even read: the
# shape check on the count runs first, so the gate refuses a tuple it
# cannot read rather than reaching the `case` with it.
chk "an empty tuple reaching the gate blocks" "$(arm "$LOC_ARM" '' "$RREPO" '')" "exit(1)"
chk "an empty tuple is refused by shape, before any kind is read" \
    "$(arm "$LOC_ARM" '' "$RREPO" '' > /dev/null; grep -c "reported '<nothing>' earlier runs" "$D/arm.out")" 1
chk "and no reading is named for it, because none was reached" \
    "$(arm "$LOC_ARM" '' "$RREPO" '' > /dev/null; grep -c 'runner_dispatch returned' "$D/arm.out")" 0
# The tuple's fields each mean one thing for every kind, so the WAIT
# message names the run AND its state.
chk "the WAIT line names the run id and the status in their own slots" \
    "$(arm "$LOC_ARM" '' "$RREPO" "$T_RUNNING" > /dev/null; grep -c "run 39530000001 for OWNER/REPO#N, started since .*, is 'in_progress'" "$D/arm.out")" 1
chk "no dispatch: the gate says re-dispatch or wait for the webhook" \
    "$(arm "$LOC_ARM" '' "$RREPO" "$T_NONE" > /dev/null; grep -c 'nothing was dispatched.*AT-3007' "$D/arm.out")" 1
chk "and it names the bound the question is about" \
    "$(arm "$LOC_ARM" '' "$RREPO" "$T_NONE" > /dev/null; grep -c "since $HEAD_TIME (the head.s push stamp)" "$D/arm.out")" 1
# One reading of the output may not contradict another: the value and
# the label come from the same branch.
chk "on the committer path the same line says so instead of calling it a push stamp" \
    "$(arm "$LOC_ARM" '' "$RREPO" "$T_NONE" "$HEAD_COMMIT_TIME" '' > /dev/null; grep -c "since $HEAD_COMMIT_TIME (the committer date, not a push stamp" "$D/arm.out")" 1
# And the died arm carries it too - the block states value-and-label-
# from-the-same-branch as its own invariant, and this was the one place
# it was not applied. Nothing was misleading there, since the sentence
# goes on to say the stamp is missing; it was the convention that was
# uneven, in a file handed to someone else to extend.
chk "the died committer line pairs the bound with its label, as its siblings do" \
    "$(arm "$LOC_ARM" '' "$RREPO" "$T_DIED" "$HEAD_COMMIT_TIME" '' > /dev/null; grep -c "completed after $HEAD_COMMIT_TIME (the committer date, not a push stamp" "$D/arm.out")" 1
chk "and that arm still never calls it a push stamp" \
    "$(arm "$LOC_ARM" '' "$RREPO" "$T_DIED" "$HEAD_COMMIT_TIME" '' > /dev/null; grep -c "the head.s push stamp" "$D/arm.out")" 0
chk "the WAIT line carries the same note" \
    "$(arm "$LOC_ARM" '' "$RREPO" "$T_RUNNING" "$HEAD_COMMIT_TIME" '' > /dev/null; grep -c "started after $HEAD_COMMIT_TIME (the committer date, not a push stamp" "$D/arm.out")" 1
chk "a running dispatch: WAIT, and do not re-dispatch" \
    "$(arm "$LOC_ARM" '' "$RREPO" "$T_RUNNING" > /dev/null; grep -c 'WAIT for it. Do NOT re-dispatch' "$D/arm.out")" 1
chk "a dead dispatch: the runner could not produce a verdict" \
    "$(arm "$LOC_ARM" '' "$RREPO" "$T_DIED" > /dev/null; grep -c 'could not produce a verdict' "$D/arm.out")" 1
chk "and the command that shows why is printed, against the runner's repository" \
    "$(arm "$LOC_ARM" '' "$RREPO" "$T_DIED" > /dev/null; grep -c "gh run view 39523085356 --repo $RREPO --log-failed" "$D/arm.out")" 1
chk "the dead-dispatch line sends the head to kind (ii), not to a merge" \
    "$(arm "$LOC_ARM" '' "$RREPO" "$T_DIED" > /dev/null; grep -c 'local review (kind (ii))' "$D/arm.out")" 1
chk "earlier rounds are reported, so a stale one is visible rather than silently dropped" \
    "$(arm "$LOC_ARM" '' "$RREPO" "$T_NONE_STALE" > /dev/null; grep -c '2 earlier run(s) for this PR predate this head and did not decide' "$D/arm.out")" 1
# The count field gets the shape check every other value in this arm
# has, and the tuple that breaks it is one a failed `earlier` read
# really produces.
chk "an empty earlier count is refused, not fed to -eq" \
    "$(arm "$LOC_ARM" '' "$RREPO" "$T_DIED_NOCOUNT")" "exit(1)"
chk "and the refusal names what it got" \
    "$(arm "$LOC_ARM" '' "$RREPO" "$T_DIED_NOCOUNT" > /dev/null; grep -c "reported '<nothing>' earlier runs" "$D/arm.out")" 1
chk "a non-numeric count is refused the same way" \
    "$(arm "$LOC_ARM" '' "$RREPO" "$T_DIED_BADCOUNT")" "exit(1)"
chk "while a real count still reports" \
    "$(arm "$LOC_ARM" '' "$RREPO" "$T_NONE_STALE" > /dev/null; grep -c '2 earlier run(s)' "$D/arm.out")" 1
chk "and with none to report the line is not printed" \
    "$(arm "$LOC_ARM" '' "$RREPO" "$T_NONE" > /dev/null; grep -c 'predate this head' "$D/arm.out")" 0

echo "6. control: one equality cut out, the headline short on that fraction alone passes"
CTRL_EQ="$D/fn-no-eq.sh"
# The check is a test line and its `|| { ...; return 1; }` continuation.
# Every two-line cut below is `/X/{N;d;}`, the POSIX spelling: `/X/,+1d`
# is a GNU extension, and BSD sed rejects it, so on macOS the control files
# came out empty and the drill failed on "control still loads" -- a FAIL
# with the wrong cause, like the `wc -c` one round 3 replaced.
# shellcheck disable=SC2016  # matched literally: the test as SKILL.md spells it
sed '/\$rv_n -eq \$rv_m/{N;d;}' "$FN" > "$CTRL_EQ"
chk "the control differs from the real function" "$(cmp -s "$FN" "$CTRL_EQ" && echo same || echo differs)" differs
# shellcheck disable=SC1090
chk "control still loads" "$(. "$CTRL_EQ"; type -t "$FUNC_NAME")" function
chk "the unguarded copy passes 2/3 reviewers, so the equality is what refuses" "$(verdict "$CTRL_EQ" "$H_REVIEWERS_SHORT")" pass
chk "the real function still refuses it" "$(verdict "$FN" "$H_REVIEWERS_SHORT")" block
chk "the measured cut still blocks without it: its LLM fraction is short too" "$(verdict "$CTRL_EQ" "$H_CUT")" block
CTRL_LLM="$D/fn-no-llm-eq.sh"
# shellcheck disable=SC2016  # matched literally: the test as SKILL.md spells it
sed '/\$llm_n -eq \$llm_m/{N;d;}' "$FN" > "$CTRL_LLM"
chk "the LLM control differs from the real function" "$(cmp -s "$FN" "$CTRL_LLM" && echo same || echo differs)" differs
# shellcheck disable=SC1090
chk "LLM control still loads" "$(. "$CTRL_LLM"; type -t "$FUNC_NAME")" function
chk "the unguarded copy passes 2/3 LLM responses, so that equality is what refuses" "$(verdict "$CTRL_LLM" "$H_LLM_SHORT")" pass
chk "the real function still refuses it" "$(verdict "$FN" "$H_LLM_SHORT")" block

echo "7. control: the Result check cut out, Changes Requested with full fractions passes"
CTRL_RES="$D/fn-no-result.sh"
# The check is a test line and its `|| { ...; return 1; }` continuation.
# shellcheck disable=SC2016  # matched literally: the test as SKILL.md spells it
sed '/=~ \$result_re/{N;d;}' "$FN" > "$CTRL_RES"
chk "the control differs from the real function" "$(cmp -s "$FN" "$CTRL_RES" && echo same || echo differs)" differs
# shellcheck disable=SC1090
chk "control still loads" "$(. "$CTRL_RES"; type -t "$FUNC_NAME")" function
chk "the unguarded copy passes a non-Approved Result, so the Result check is what refuses" \
    "$(verdict "$CTRL_RES" "$H_FULL_NOT_APPROVED")" pass
chk "the real function still refuses it" "$(verdict "$FN" "$H_FULL_NOT_APPROVED")" block

echo "8. control: the denominator check cut out, 2/2 against 3 passes; the majors tally cut out, an unanswered major passes"
CTRL_DEN="$D/fn-no-denom.sh"
# shellcheck disable=SC2016  # matched literally: the test as SKILL.md spells it
sed '/\$rv_m -eq \$EXPECTED_REVIEWERS/{N;d;}' "$FN" > "$CTRL_DEN"
chk "the control differs from the real function" "$(cmp -s "$FN" "$CTRL_DEN" && echo same || echo differs)" differs
# shellcheck disable=SC1090
chk "control still loads" "$(. "$CTRL_DEN"; type -t "$FUNC_NAME")" function
chk "the unguarded copy passes 2/2 against 3, so the comparison is what refuses" "$(verdict "$CTRL_DEN" "$H_TWO")" pass
chk "the real function still refuses it" "$(verdict "$FN" "$H_TWO")" block
CTRL_TALLY="$D/fn-no-tally.sh"
# shellcheck disable=SC2016  # matched literally: the tally as SKILL.md spells it
sed '/^  \[ "\$bad" -eq 0 \]$/d' "$FN" > "$CTRL_TALLY"
chk "the control differs from the real function" "$(cmp -s "$FN" "$CTRL_TALLY" && echo same || echo differs)" differs
# shellcheck disable=SC1090
chk "control still loads" "$(. "$CTRL_TALLY"; type -t "$MAJORS_FN")" function
chk "the untallied copy passes an unanswered major, so the tally is what refuses" "$(majors "$CTRL_TALLY" "$BODY_ONE" 1 '' '')" pass
chk "the real function still refuses it" "$(majors "$FN" "$BODY_ONE" 1 '' '')" block
CTRL_REPLY="$D/fn-no-reply.sh"
# shellcheck disable=SC2016  # matched literally: the line as SKILL.md spells it
sed '/\[ "\$t_rep" = replied \] || continue/d' "$FN" > "$CTRL_REPLY"
chk "the reply-guard control differs from the real function" "$(cmp -s "$FN" "$CTRL_REPLY" && echo same || echo differs)" differs
chk "without the reply guard a silently resolved thread answers the major, so the guard is what refuses" "$(majors "$CTRL_REPLY" "$BODY_ONE" 1 "$LOC_SILENT" '')" pass
chk "the real function still refuses it" "$(majors "$FN" "$BODY_ONE" 1 "$LOC_SILENT" '')" block
CTRL_NEEDLE="$D/fn-prefix-needle.sh"
# The pre-verdict needle: the bare 60-char description prefix.
# shellcheck disable=SC2016  # matched literally: the line as SKILL.md spells it
sed 's|needle=${line#- + }|needle=${desc:0:60}|' "$FN" > "$CTRL_NEEDLE"
chk "the needle control differs from the real function" "$(cmp -s "$FN" "$CTRL_NEEDLE" && echo same || echo differs)" differs
# shellcheck disable=SC2016  # the backticks are the aggregate's own
chk "the full quoted-item comparison still refuses a truncated description needle" \
    "$(majors "$CTRL_NEEDLE" "$BODY_LENS1" 1 '' '> - + **major** `other/file.py:9` -- Missing error handling. in the other module')" block
CTRL_KEY="$D/fn-key-needle.sh"
# The pre-re-verdict needle for path-bearing items: the backticked key alone.
# shellcheck disable=SC2016  # matched literally: the line as SKILL.md spells it
sed 's|needle=${line#- + }|needle="\\`$key\\`"|' "$FN" > "$CTRL_KEY"
chk "the key-needle control differs from the real function" "$(cmp -s "$FN" "$CTRL_KEY" && echo same || echo differs)" differs
chk "the full quoted-item comparison still refuses a bare-key needle" "$(majors "$CTRL_KEY" "$BODY_C1" 2 '' "$Q_C1")" block
CTRL_QUOTE_PREFIX="$D/fn-quote-prefix.sh"
# shellcheck disable=SC2016  # matched literally: the shell comparison
sed 's|\[ "${BASH_REMATCH\[1\]}" = "$needle" \]|[[ ${BASH_REMATCH[1]} == "$needle"* ]]|' "$FN" > "$CTRL_QUOTE_PREFIX"
chk "the quoted-item prefix control differs from the real function" "$(cmp -s "$FN" "$CTRL_QUOTE_PREFIX" && echo same || echo differs)" differs
chk "a prefix comparison lets one longer quote answer both majors" "$(majors "$CTRL_QUOTE_PREFIX" "$BODY_PREFIX" 2 '' "$Q_PREFIX")" pass
chk "the real full-item comparison still blocks the missing shorter quote" "$(majors "$FN" "$BODY_PREFIX" 2 '' "$Q_PREFIX")" block
# The needle is the line minus its bullet, for both shapes -- read off the
# block message, which prints it; a needle re-assembled from captures could
# agree with the line by luck on one shape and not the other.
# shellcheck disable=SC1090
out=$(. "$FN"; "$MAJORS_FN" "$BODY_C1" 2 '' '')
# shellcheck disable=SC2016  # the backticks are the aggregate's own
chk "path shape: the needle is the rendered line minus '- + '" \
    "$(printf '%s\n' "$out" | grep -cF "quoting '**major** \`bin/foo.sh:12\` -- unchecked input':")" 1
# shellcheck disable=SC1090
out=$(. "$FN"; "$MAJORS_FN" "$BODY_LENS1" 1 '' '')
chk "path-less shape: the needle is the rendered line minus '- + '" \
    "$(printf '%s\n' "$out" | grep -cF "quoting '**major** -- Missing error handling.':")" 1
chk "the needle is derived once, from the line, not per shape" "$(grep -c 'needle=' "$FN")" 1
CTRL_SUBSTR="$D/fn-substring-match.sh"
# The pre-round-10 thread match: the description as a substring of the
# first comment's text, with no shape around it.
# shellcheck disable=SC2016  # matched literally: the test as SKILL.md spells it
sed 's|\[\[ \$t_body =~ \$finding_re \]\] && \[ "\${BASH_REMATCH\[1\]}" = "\$desc" \]|[[ $t_body == *"$desc"* ]]|' "$FN" > "$CTRL_SUBSTR"
chk "the substring control differs from the real function" "$(cmp -s "$FN" "$CTRL_SUBSTR" && echo same || echo differs)" differs
# shellcheck disable=SC1090
chk "substring control still loads" "$(. "$CTRL_SUBSTR"; type -t "$THREAD_FN")" function
chk "with the substring match the same-path thread that mentions the description in prose answers the finding, so the equality after the producer's prefix is what refuses" "$(majors "$CTRL_SUBSTR" "$BODY_ONE" 1 "$LOC_PROSE" '')" pass
chk "the real function still refuses it" "$(majors "$FN" "$BODY_ONE" 1 "$LOC_PROSE" '')" block
chk "the substring copy still answers the producer-shaped thread -- the control widens the match and nothing else" "$(majors "$CTRL_SUBSTR" "$BODY_ONE" 1 "$LOC_NEAR_SAME" '')" pass
# The line-less unrelated thread is refused on its body, not on a reply
# flag read out of the wrong field: with the reply guard cut it still blocks.
chk "without the reply guard the line-less thread with no matching body still blocks, so its body is what refuses it" "$(majors "$CTRL_REPLY" "$BODY_ONE" 1 "$LOC_NOLINE_OTHER" '')" block

echo "8b. control: the aggregate selector with one check cut out passes what that check refuses"
CTRL_COMMIT="$D/fn-no-commit.sh"
# shellcheck disable=SC2016  # matched literally: the test as SKILL.md spells it
sed 's/ and \.commit_id == \$h//' "$FN" > "$CTRL_COMMIT"
chk "the commit control differs from the real function" "$(cmp -s "$FN" "$CTRL_COMMIT" && echo same || echo differs)" differs
# shellcheck disable=SC1090
chk "commit control still loads" "$(. "$CTRL_COMMIT"; type -t "$AGG_FN")" function
chk "without the commit_id equality the previous head's APPROVED review passes after a push, so the equality is what refuses" "$(agg "$CTRL_COMMIT" '[]' "$CAB" "$NEXT_HEAD" "$E_S" "$E_E")" review:5384773396:APPROVED
chk "the real function still refuses it" "$(agg "$FN" '[]' "$CAB" "$NEXT_HEAD" "$E_S" "$E_E")" none
CTRL_STATE="$D/fn-no-state.sh"
# shellcheck disable=SC2016  # matched literally: the test as SKILL.md spells it
sed 's/ and (\.state == "APPROVED" or \.state == "CHANGES_REQUESTED")//' "$FN" > "$CTRL_STATE"
chk "the state control differs from the real function" "$(cmp -s "$FN" "$CTRL_STATE" && echo same || echo differs)" differs
chk "without the state check the dismissed review on the head passes, so that check is what refuses" "$(agg "$CTRL_STATE" '[]' "$CAB" "$PREV_CAB" "$E_S" "$E_E")" review:5384693316:DISMISSED
chk "the real function still refuses it" "$(agg "$FN" '[]' "$CAB" "$PREV_CAB" "$E_S" "$E_E")" none
# Round 18's shape: the filter as it stood, every state but DISMISSED.
CTRL_NOT_DISMISSED="$D/fn-not-dismissed.sh"
# shellcheck disable=SC2016  # matched literally: the test as SKILL.md spells it
sed 's/(\.state == "APPROVED" or \.state == "CHANGES_REQUESTED")/.state != "DISMISSED"/' "$FN" > "$CTRL_NOT_DISMISSED"
chk "the not-DISMISSED control differs from the real function" "$(cmp -s "$FN" "$CTRL_NOT_DISMISSED" && echo same || echo differs)" differs
chk "with every state but DISMISSED admitted the COMMENTED Approved marker review passes, so the allow-list is what refuses it" "$(agg "$CTRL_NOT_DISMISSED" '[]' "$(lst "$R_COMMENTED_OK")" "$HEAD_IC" "$E_S" "$E_E")" review:7:COMMENTED
chk "the real function still refuses it" "$(agg "$FN" '[]' "$(lst "$R_COMMENTED_OK")" "$HEAD_IC" "$E_S" "$E_E")" none
CTRL_AUTHOR="$D/fn-no-author.sh"
# shellcheck disable=SC2016  # matched literally: the test as SKILL.md spells it
sed 's/ and ((\.author as \$x | \$as | index(\$x)) != null)//' "$FN" > "$CTRL_AUTHOR"
chk "the author control differs from the real function" "$(cmp -s "$FN" "$CTRL_AUTHOR" && echo same || echo differs)" differs
chk "without the author check a marker review by another Bot login passes, so the list is what refuses" "$(agg "$CTRL_AUTHOR" '[]' "$(lst "$R_SPOOF_BOT")" "$HEAD_IC" "$E_S" "$E_E")" review:2:APPROVED
chk "the real function still refuses it" "$(agg "$FN" '[]' "$(lst "$R_SPOOF_BOT")" "$HEAD_IC" "$E_S" "$E_E")" none
CTRL_COUNT="$D/fn-no-count.sh"
# The check is a test line and its `|| { ...; return 1; }` continuation.
# shellcheck disable=SC2016  # matched literally: the test as SKILL.md spells it
sed '/\[ "\$seen" -eq "\$total" \]/{N;d;}' "$FN" > "$CTRL_COUNT"
chk "the count control differs from the real function" "$(cmp -s "$FN" "$CTRL_COUNT" && echo same || echo differs)" differs
# shellcheck disable=SC1090
chk "count control still loads" "$(. "$CTRL_COUNT"; type -t "$COUNT_FN")" function
chk "without the comparison a truncated list passes, so the comparison is what refuses" "$(complete "$CTRL_COUNT" "$(lst "$R_RUN1")" 2)" ok
chk "the real function still refuses it" "$(complete "$FN" "$(lst "$R_RUN1")" 2)" block

echo "8c. control: the cutoff reader and the convergence decision, one check cut out each"
CTRL_QUOTE="$D/fn-no-quote.sh"
# The `>` form is one line of the cutoff reader: the select() that keeps
# only quoted lines for the location match. Cutting it cuts the form and
# nothing else; the fixture names the marker and every location, all in
# prose. Until round 10 of #194 the marker scan carried a second `>` test
# on the one reply naming the marker, and this cut took both. The cut is
# scoped to the cutoff reader: verdict_dispositions keeps the same `>`
# form on a continuation line, and cutting that would leave a file that
# does not load. The block closes as `{...;}`: BSD sed rejects a `}`
# that follows a command with no `;` between.
sed "/^${CUTOFF_FN}() {\$/,/^}\$/{/startswith(\">\")/d;}" "$FN" > "$CTRL_QUOTE"
chk "the quote control differs from the real function" "$(cmp -s "$FN" "$CTRL_QUOTE" && echo same || echo differs)" differs
chk "the quote control cut the cutoff reader's one line and nothing else" "$(( $(wc -l < "$FN") - $(wc -l < "$CTRL_QUOTE") ))" 1
# shellcheck disable=SC1090
chk "quote control still loads" "$(. "$CTRL_QUOTE"; type -t "$CUTOFF_FN")" function
chk "without the quote requirement a comment that names the marker and every location in prose passes, so that clause is what refuses" \
    "$(cutoff "$CTRL_QUOTE" "$(lst "$C_CUTOFF" "$C_NAMES_NO_QUOTE")" "$W_CUT")" ok
chk "the real function still refuses it" "$(cutoff "$FN" "$(lst "$C_CUTOFF" "$C_NAMES_NO_QUOTE")" "$W_CUT")" block
CTRL_LOC="$D/fn-no-location.sh"
# The location binding decides on one shell line; without it the marker
# and any `>` line answer the whole comment, which is what round 5 of #194
# found.
# shellcheck disable=SC2016  # matched literally: the test as SKILL.md spells it
sed '/\[ -z "\$missing" \]/d' "$FN" > "$CTRL_LOC"
chk "the location control differs from the real function" "$(cmp -s "$FN" "$CTRL_LOC" && echo same || echo differs)" differs
# shellcheck disable=SC1090
chk "location control still loads" "$(. "$CTRL_LOC"; type -t "$CUTOFF_FN")" function
chk "without the location binding a reply that names the marker and quotes an unrelated line passes, so the binding is what refuses" \
    "$(cutoff "$CTRL_LOC" "$(lst "$C_CUTOFF" "$C_UNRELATED_QUOTE")" "$W_CUT")" ok
chk "the real function still refuses it" "$(cutoff "$FN" "$(lst "$C_CUTOFF" "$C_UNRELATED_QUOTE")" "$W_CUT")" block
CTRL_MARKER="$D/fn-no-marker.sh"
# The marker clause is the one line of the cutoff reader that tests `$m`.
# shellcheck disable=SC2016  # matched literally: the clause as SKILL.md spells it
sed '/(\$m | rx)/d' "$FN" > "$CTRL_MARKER"
chk "the marker control differs from the real function" "$(cmp -s "$FN" "$CTRL_MARKER" && echo same || echo differs)" differs
chk "without the marker clause any quoted comment passes, so naming the cutoff comment is what refuses" \
    "$(cutoff "$CTRL_MARKER" "$(lst "$C_CUTOFF" "$C_QUOTE_NO_NAME")" "$W_CUT")" ok
chk "the real function still refuses it" "$(cutoff "$FN" "$(lst "$C_CUTOFF" "$C_QUOTE_NO_NAME")" "$W_CUT")" block
CTRL_DIGIT="$D/fn-no-digit-guard.sh"
# The marker's trailing guard is one lookahead on that line; without it the
# marker binds as the bare substring round 7 of #194 found.
# shellcheck disable=SC2016  # matched literally: the guard as SKILL.md spells it
sed 's/(\$m | rx) + "(?!\[0-9A-Za-z_-\])"/($m | rx)/' "$FN" > "$CTRL_DIGIT"
chk "the digit-guard control differs from the real function" "$(cmp -s "$FN" "$CTRL_DIGIT" && echo same || echo differs)" differs
# shellcheck disable=SC1090
chk "digit-guard control still loads" "$(. "$CTRL_DIGIT"; type -t "$CUTOFF_FN")" function
chk "without the guard a reply naming r10 answers r1, so the guard is what refuses" \
    "$(cutoff "$CTRL_DIGIT" "$(lst "$C_CUTOFF_R1" "$C_NAMES_R10")" "$W_CUT")" ok
chk "the real function still refuses it" "$(cutoff "$FN" "$(lst "$C_CUTOFF_R1" "$C_NAMES_R10")" "$W_CUT")" block
CTRL_BOT="$D/fn-no-nonbot.sh"
sed '/select(.type != "Bot")/d' "$FN" > "$CTRL_BOT"
chk "the non-Bot control differs from the real function" "$(cmp -s "$FN" "$CTRL_BOT" && echo same || echo differs)" differs
chk "without it the bot's own quoted reply answers its own cutoff, so the author check is what refuses" \
    "$(cutoff "$CTRL_BOT" "$(lst "$C_CUTOFF" "$C_BOT_REPLY")" "$W_CUT")" ok
chk "the real function still refuses it" "$(cutoff "$FN" "$(lst "$C_CUTOFF" "$C_BOT_REPLY")" "$W_CUT")" block
CTRL_SCAN="$D/fn-no-cutoff-scan.sh"
# The cutoff scan's own select: without it every Bot comment in the window
# reads as a cutoff comment, and the aggregate blocks the gate.
sed '/select(.\[2\] != "")/d' "$FN" > "$CTRL_SCAN"
chk "the scan control differs from the real function" "$(cmp -s "$FN" "$CTRL_SCAN" && echo same || echo differs)" differs
chk "without the marker scan the aggregate comment reads as a cutoff and blocks, so the scan is what distinguishes them" \
    "$(cutoff "$CTRL_SCAN" "$(lst "$C_AGG")" "$W_CUT")" block
chk "the real function still passes it" "$(cutoff "$FN" "$(lst "$C_AGG")" "$W_CUT")" ok
CTRL_AUTHOR_SCAN="$D/fn-bot-only-scan.sh"
# The author filter AT-2803 removed, put back in front of the marker scan:
# the operator's own login posts the cutoff comment as a `User` under
# review_pr_local.py, and that filter is what made it go unseen.
# shellcheck disable=SC2016  # matched literally: the scan as SKILL.md spells it
sed '/^'"${CUTOFF_FN}"'() {$/,/^}$/{s/select((\.created_at | type) == "string")/select(.type == "Bot") | &/;}' "$FN" > "$CTRL_AUTHOR_SCAN"
chk "the Bot-only scan control differs from the real function" "$(cmp -s "$FN" "$CTRL_AUTHOR_SCAN" && echo same || echo differs)" differs
# shellcheck disable=SC1090
chk "Bot-only scan control still loads" "$(. "$CTRL_AUTHOR_SCAN"; type -t "$CUTOFF_FN")" function
chk "with the Bot filter restored the operator-posted cutoff comment goes unseen and passes, so that filter is what dropped it" \
    "$(cutoff "$CTRL_AUTHOR_SCAN" "$(lst "$C_CUT_LOCAL")" "$W_CUT")" ok
chk "the real function still blocks it" "$(cutoff "$FN" "$(lst "$C_CUT_LOCAL")" "$W_CUT")" block
CTRL_NEWEST="$D/fn-newest-start.sh"
# The window start's earliest-wins tail is one word; newest-wins is what
# run_start gave (c) until round 8 of #194.
sed 's#| sort | first // empty#| sort | last // empty#' "$FN" > "$CTRL_NEWEST"
chk "the newest-start control differs from the real function" "$(cmp -s "$FN" "$CTRL_NEWEST" && echo same || echo differs)" differs
# shellcheck disable=SC1090
chk "newest-start control still loads" "$(. "$CTRL_NEWEST"; type -t "$SINCE_FN")" function
chk "with newest-wins the window starts after the older run's cutoff comment and the gate opens, so earliest-wins is what blocks" \
    "$(cutoff "$FN" "$(lst "$C_CUTOFF")" "$(since_of "$CTRL_NEWEST" "$(lst "$R_OK_NEW" "$R_OK_OLD")" "$CUT_WF" '')")" ok
chk "the real function still blocks it" \
    "$(cutoff "$FN" "$(lst "$C_CUTOFF")" "$(since_of "$FN" "$(lst "$R_OK_NEW" "$R_OK_OLD")" "$CUT_WF" '')")" block
CTRL_CONV="$D/fn-no-roster-call.sh"
# The call is a line and its `|| { ...; return 1; }` continuation.
sed '/headline_roster "/{N;d;}' "$FN" > "$CTRL_CONV"
chk "the roster-call control differs from the real function" "$(cmp -s "$FN" "$CTRL_CONV" && echo same || echo differs)" differs
# shellcheck disable=SC1090
chk "roster-call control still loads" "$(. "$CTRL_CONV"; type -t "$CONV_FN")" function
# shellcheck disable=SC1090
chk "the control still defines the roster function it no longer calls" "$(. "$CTRL_CONV"; type -t "$FUNC_NAME")" function
chk "without the roster call a 2/3 round converges, so the roster is what refuses it" \
    "$(converged "$CTRL_CONV" "$H_CUT" no-behaviour)" converged
chk "the real function still refuses it" "$(converged "$FN" "$H_CUT" no-behaviour)" block
CTRL_CASE="$D/fn-no-behaviour-case.sh"
sed '/^  case /,/^  esac$/d' "$FN" > "$CTRL_CASE"
chk "the behaviour-case control differs from the real function" "$(cmp -s "$FN" "$CTRL_CASE" && echo same || echo differs)" differs
chk "without the case a full round with no statement converges, so the statement is what refuses" \
    "$(converged "$CTRL_CASE" "$H_NO_ISSUES" '')" converged
chk "the real function still refuses it" "$(converged "$FN" "$H_NO_ISSUES" '')" block

echo "8d. control: the (c) call site with its loop filtered back to kind (i), and with its majors call cut out"
CTRL_KIND="$D/fn-kind-i-only.sh"
# Round 9's shape: the majors check reachable from kind (i) alone -- a
# kind test inserted after the loop reads each source's fields.
chk "the loop's field read appears once in the extracted functions, so the mutation lands there" \
    "$(grep -c 'read -r kind label at since want' "$FN")" 1
# The appended text follows `a\` on its own line, the POSIX form; the
# one-line `a\text` is GNU's, and BSD sed rejects it.
# shellcheck disable=SC2016  # the test is the control's, not the shell's
sed '/read -r kind label at since want/a\
[ "$kind" = i ] || continue' "$FN" > "$CTRL_KIND"
chk "the kind-filter control differs from the real function" "$(cmp -s "$FN" "$CTRL_KIND" && echo same || echo differs)" differs
# shellcheck disable=SC1090
chk "kind-filter control still loads" "$(. "$CTRL_KIND"; type -t "$DISP_FN")" function
chk "with the loop filtered to kind (i) the kind (ii) approval's unanswered major passes, so reading every source is what blocks" \
    "$(disp "$CTRL_KIND" '' "$(lst "$S_II")" "$(lst "$C_VERDICT")")" pass
chk "the real function still blocks it" "$(disp "$FN" '' "$(lst "$S_II")" "$(lst "$C_VERDICT")")" block
chk "the filtered copy still blocks kind (i)'s unanswered major -- the control removes the local path and nothing else" \
    "$(disp "$CTRL_KIND" '' "$(lst "$S_I")" "$(lst "$C_AGG_I")")" block
CTRL_DISP_CALL="$D/fn-no-majors-call.sh"
# The call is a line and its `|| { ...; rc=1; }` continuation.
# shellcheck disable=SC2016  # matched literally: the call as SKILL.md spells it
sed '/major_dispositions "\$body" "\$want"/{N;d;}' "$FN" > "$CTRL_DISP_CALL"
chk "the majors-call control differs from the real function" "$(cmp -s "$FN" "$CTRL_DISP_CALL" && echo same || echo differs)" differs
# shellcheck disable=SC1090
chk "majors-call control still loads" "$(. "$CTRL_DISP_CALL"; type -t "$DISP_FN")" function
# shellcheck disable=SC1090
chk "the control still defines the majors function it no longer calls" "$(. "$CTRL_DISP_CALL"; type -t "$MAJORS_FN")" function
chk "without the majors call the unanswered kind (ii) major passes, so the call is what blocks" \
    "$(disp "$CTRL_DISP_CALL" '' "$(lst "$S_II")" "$(lst "$C_VERDICT")")" pass
chk "the real function still blocks it" "$(disp "$FN" '' "$(lst "$S_II")" "$(lst "$C_VERDICT")")" block

echo "8e. control: the name check with its repository lookup cut out, a mistyped name passes"
CTRL_KNOWN="$D/fn-no-repo-lookup.sh"
# The lookup is a line and its `|| { ...; return 1; }` continuation.
# shellcheck disable=SC2016  # matched literally: the lookup as SKILL.md spells it
chk "the repository lookup appears once in the extracted functions, so the cut lands there" \
    "$(grep -c 'grep -qxF -- "\$wf"' "$FN")" 1
# shellcheck disable=SC2016  # matched literally: the lookup as SKILL.md spells it
sed '/grep -qxF -- "\$wf"/{N;d;}' "$FN" > "$CTRL_KNOWN"
chk "the lookup control differs from the real function" "$(cmp -s "$FN" "$CTRL_KNOWN" && echo same || echo differs)" differs
# shellcheck disable=SC1090
chk "lookup control still loads" "$(. "$CTRL_KNOWN"; type -t "$KNOWN_FN")" function
chk "without the lookup a name the repository lacks passes as the review that did not run, so the lookup is what refuses it" \
    "$(known "$CTRL_KNOWN" "$(lst "$R_LINT")" 'AI Code Reveiw' "$NAMES_WITH")" ok
chk "the real function still refuses it" "$(known "$FN" "$(lst "$R_LINT")" 'AI Code Reveiw' "$NAMES_WITH")" refused
chk "the control still refuses the placeholder -- the cut removes the lookup and nothing else" \
    "$(known "$CTRL_KNOWN" "$(lst "$R_LINT")" '<review workflow name>' "$NAMES_WITH")" refused

echo "8f. control: the locator and the gate's arm, one clause cut out each"
# Every control here mutates the source by a different route than the
# assertion reads it, then shows the assertion go red. A control that
# reads the same way the assertion does is a second copy of the
# assertion, not a test of it: `grep -v P file | grep -c P` is 0 for
# every P and every file, and establishes nothing about the pattern
# beside it.
CTRL_LEFT="$D/fn-locator-right-edge-only.sh"
# The piece AFTER the needle checked, the piece before it not.
# shellcheck disable=SC2016  # matched literally: the clause as SKILL.md spells it
sed 's/(\$p\[\.\]     | test("\[A-Za-z0-9\._-\]\$") | not)/(true)/' "$FN" > "$CTRL_LEFT"
chk "the left-edge control differs from the real function" "$(cmp -s "$FN" "$CTRL_LEFT" && echo same || echo differs)" differs
# shellcheck disable=SC1090
chk "left-edge control still loads" "$(. "$CTRL_LEFT"; type -t "$RUNNER_FN")" function
chk "without the left edge, another owner's run answers as this one's" \
    "$(dispatch "$CTRL_LEFT" "$(rpage "$D_LEFT")" '' '')" "died|39560000000|completed|https://example.invalid/runner/left|0"
chk "and with both edges checked the real runner round is still found" \
    "$(dispatch "$FN" "$(rpage "$D_DIED")" '' '')" "died|39523085356|completed|$DIED_URL|0"
CTRL_TITLE="$D/fn-locator-display-title.sh"
# display_title fetched and matched in place of name. Both halves are
# mutated, because the projection no longer carries display_title at all
# - which is itself part of the fix.
# shellcheck disable=SC2016  # matched literally: the two lines as SKILL.md spells them
sed 's/{id, name, status, created_at, html_url}/{id, display_title, status, created_at, html_url}/;
     s/select(((\.name \/\/ "")/select(((.display_title \/\/ "")/' "$FN" > "$CTRL_TITLE"
chk "the run-name control differs from the real function" "$(cmp -s "$FN" "$CTRL_TITLE" && echo same || echo differs)" differs
# shellcheck disable=SC1090
chk "run-name control still loads" "$(. "$CTRL_TITLE"; type -t "$RUNNER_FN")" function
chk "reading display_title instead, a run whose commit subject cites this PR answers as its dispatch" \
    "$(dispatch "$CTRL_TITLE" "$(rpage "$D_SUBJECT_ONLY")" '' '')" "died|39540000000|completed|https://example.invalid/runner/subject|0"
chk "and the measured runner round is found either way, because run-name sets both" \
    "$(dispatch "$CTRL_TITLE" "$(rpage "$D_DIED")" '' '')" "died|39523085356|completed|$DIED_URL|0"
chk "the same grep finds display_title when the code reads it, so 5k's n=0 is a reading" \
    "$([ "$(code_of "$RUNNER_FN" "$CTRL_TITLE" | grep -c 'display_title')" -gt 0 ] && echo found || echo missing)" found
CTRL_NEWESTONLY="$D/fn-locator-newest-only.sh"
# The in-flight preference removed by a different route than the
# assertions read it: the first alternative of the `//` is emptied, so
# the fall-back branch - newest-wins - is what decides.
# shellcheck disable=SC2016  # matched literally: the line as SKILL.md spells it
sed 's/^     | ((\[ \$since_head\[\] | select(\.status != "completed") \] | last)$/     | ((empty)/' "$FN" > "$CTRL_NEWESTONLY"
chk "the newest-only control differs from the real function" "$(cmp -s "$FN" "$CTRL_NEWESTONLY" && echo same || echo differs)" differs
# shellcheck disable=SC1090
chk "newest-only control still loads" "$(. "$CTRL_NEWESTONLY"; type -t "$RUNNER_FN")" function
chk "newest-only, the in-flight round is passed over and the gate is told the dispatch died" \
    "$(dispatch "$CTRL_NEWESTONLY" "$(rpage "$D_OLDER_RUNNING" "$D_NEWER_DEAD")" '' '')" \
    "died|39532000000|completed|https://example.invalid/runner/newer-dead|0"
CTRL_NOTIME="$D/fn-locator-no-head-bound.sh"
# shellcheck disable=SC2016  # matched literally: the select as SKILL.md spells it
sed 's/select((\.created_at \/\/ "") >= \$s)/select(true)/' "$FN" > "$CTRL_NOTIME"
chk "the head-bound control differs from the real function" "$(cmp -s "$FN" "$CTRL_NOTIME" && echo same || echo differs)" differs
# shellcheck disable=SC1090
chk "head-bound control still loads" "$(. "$CTRL_NOTIME"; type -t "$RUNNER_FN")" function
chk "unbound, the previous head's round comes back as a dispatch that died" \
    "$(dispatch "$CTRL_NOTIME" "$(rpage "$D_PREV_HEAD")" '' '')" "died|39510000000|completed|https://example.invalid/runner/prev-head|1"
chk "bound, the same input says nothing was dispatched for this head" \
    "$(dispatch "$FN" "$(rpage "$D_PREV_HEAD")" '' '')" "none||||1"
chk "unbound, the stale round even outranks this head's running one" \
    "$(dispatch "$CTRL_NOTIME" "$(rpage "$D_PREV_HEAD" "$D_RUNNING")" '' '' 2026-10-21T03:00:00Z)" \
    "running|39530000001|in_progress|https://example.invalid/runner/running|2"
CTRL_NOPAGE="$D/fn-locator-capped.sh"
# shellcheck disable=SC2016  # matched literally: the read as SKILL.md spells it
sed 's/gh api --paginate "repos\/\$RUNNER_REPO/gh api --limit 100 "repos\/$RUNNER_REPO/' "$FN" > "$CTRL_NOPAGE"
chk "the cap control differs from the real function" "$(cmp -s "$FN" "$CTRL_NOPAGE" && echo same || echo differs)" differs
chk "the same grep finds the cap, so 5k's n=0 is a reading and not an empty scan" \
    "$(code_of "$RUNNER_FN" "$CTRL_NOPAGE" | grep -c -- '--limit')" 1
chk "capped, the page-2 dispatch vanishes and the gate is told nothing was dispatched" \
    "$(dispatch "$CTRL_NOPAGE" "$(rpage "$D_PR195")" "$(rpage "$D_DIED")" '')" "none||||0"
CTRL_NOENVGUARD="$D/fn-locator-unguarded-raw.sh"
# shellcheck disable=SC2016  # matched literally: the arm as SKILL.md spells it
sed 's/|| { echo "ERROR: could not list the runs of .*/|| true/' "$FN" > "$CTRL_NOENVGUARD"
chk "the raw-guard control differs from the real function" "$(cmp -s "$FN" "$CTRL_NOENVGUARD" && echo same || echo differs)" differs
chk "without that guard, a broken envelope reads as nothing dispatched" \
    "$(dispatch "$CTRL_NOENVGUARD" '{"total_count":0}' '' '')" "none||||0"
CTRL_NOSTATUSCHECK="$D/fn-locator-no-status-enum.sh"
# shellcheck disable=SC2016  # matched literally: the test as SKILL.md spells it
sed 's/^  \[ "\$bad_status" = 0 \] \\$/  [ -z "" ] \\/' "$FN" > "$CTRL_NOSTATUSCHECK"
chk "the status-enum control differs from the real function" "$(cmp -s "$FN" "$CTRL_NOSTATUSCHECK" && echo same || echo differs)" differs
chk "without it a statusless record becomes an indefinite WAIT" \
    "$(dispatch "$CTRL_NOSTATUSCHECK" "$(rpage "$D_NOSTATUS")" '' '')" "running|39533000000|<none>|https://example.invalid/runner/nostatus|0"
CTRL_UNBOUNDEDSTATUS="$D/fn-locator-unbounded-status.sh"
# shellcheck disable=SC2016  # matched literally: the pass as SKILL.md spells it
sed 's/select((\.created_at \/\/ "") >= \$s) | select((\.status \/\/ "") == "")/select((.status \/\/ "") == "")/' "$FN" > "$CTRL_UNBOUNDEDSTATUS"
chk "the unbounded-status control differs from the real function" "$(cmp -s "$FN" "$CTRL_UNBOUNDEDSTATUS" && echo same || echo differs)" differs
# shellcheck disable=SC1090
chk "unbounded-status control still loads" "$(. "$CTRL_UNBOUNDEDSTATUS"; type -t "$RUNNER_FN")" function
chk "unbounded, a statusless historical run blocks the gate on a head it cannot decide" \
    "$(dispatch "$CTRL_UNBOUNDEDSTATUS" "$(rpage "$D_NOSTATUS_OLD" "$D_DIED")" '' '')" "error(1)"
chk "bounded, the same input reads this head's dead dispatch and counts the other" \
    "$(dispatch "$FN" "$(rpage "$D_NOSTATUS_OLD" "$D_DIED")" '' '')" "died|39523085356|completed|$DIED_URL|1"
chk "and both forms still refuse a statusless candidate above the bound" \
    "$(printf '%s/%s' "$(dispatch "$CTRL_UNBOUNDEDSTATUS" "$(rpage "$D_NOSTATUS")" '' '')" "$(dispatch "$FN" "$(rpage "$D_NOSTATUS")" '' '')")" "error(1)/error(1)"
CTRL_NOYOUNG="$D/fn-locator-no-young-run-rule.sh"
# The young-run rule removed by a different route than the assertion
# reads it: the shape test is emptied so no run is ever counted
# unresolved, which is what the rule's absence amounted to.
# shellcheck disable=SC2016  # matched literally: the select as SKILL.md spells it
sed 's/| select(((\.name \/\/ "") | test("\[A-Za-z0-9\._-\]+\/\[A-Za-z0-9\._-\]+#\[0-9\]+")) | not) \] | length/| select(false) ] | length/' "$FN" > "$CTRL_NOYOUNG"
chk "the young-run control differs from the real function" "$(cmp -s "$FN" "$CTRL_NOYOUNG" && echo same || echo differs)" differs
# shellcheck disable=SC1090
chk "young-run control still loads" "$(. "$CTRL_NOYOUNG"; type -t "$RUNNER_FN")" function
chk "without the rule, a run whose name has not resolved reads as nothing dispatched" \
    "$(dispatch "$CTRL_NOYOUNG" "$(rpage "$D_YOUNG")" '' '')" "none||||0"
# And the refusal's TEXT is read off the copy too, not only its
# behaviour - which is what locmsg()'s function-file parameter is for.
chk "and it says nothing about a name too young, because it never refused" \
    "$(locmsg "$CTRL_NOYOUNG" "$(rpage "$D_YOUNG")" 'too young to have resolved its run-name')" 0
chk "while the real function says exactly that on the same page" \
    "$(locmsg "$FN" "$(rpage "$D_YOUNG")" 'too young to have resolved its run-name')" 1
chk "which is the advice that cancels the round it could not see" \
    "$(arm "$LOC_ARM" '' "$RREPO" "$T_NONE" > /dev/null; grep -c 'Re-dispatch it' "$D/arm.out")" 1
chk "with the rule in place the same page refuses instead" \
    "$(dispatch "$FN" "$(rpage "$D_YOUNG")" '' '')" "error(1)"
chk "and the other-target page is unaffected by either copy, so the rule is not simply refusing everything" \
    "$(printf '%s/%s' "$(dispatch "$CTRL_NOYOUNG" "$(rpage "$D_OTHER_REPO")" '' '')" "$(dispatch "$FN" "$(rpage "$D_OTHER_REPO")" '' '')")" "none||||0/none||||0"
CTRL_NOSCOPE="$D/fn-locator-unscoped-young-run.sh"
# The lifecycle scope removed by a different route than the assertion
# reads it: the status select is emptied, so a completed run with an
# unresolved name is counted as young again.
# shellcheck disable=SC2016  # matched literally: the select as SKILL.md spells it
sed 's/| select((\.status \/\/ "") != "completed")/| select(true)/' "$FN" > "$CTRL_NOSCOPE"
chk "the young-run scope control differs from the real function" "$(cmp -s "$FN" "$CTRL_NOSCOPE" && echo same || echo differs)" differs
# shellcheck disable=SC1090
chk "young-run scope control still loads" "$(. "$CTRL_NOSCOPE"; type -t "$RUNNER_FN")" function
chk "unscoped, a completed run with no needle becomes a block no re-read can clear" \
    "$(dispatch "$CTRL_NOSCOPE" "$(rpage "$D_UNRESOLVED_DONE")" '' '')" "error(1)"
chk "and it blocks this head's own dispatch along with it" \
    "$(dispatch "$CTRL_NOSCOPE" "$(rpage "$D_UNRESOLVED_DONE" "$D_DIED")" '' '')" "error(1)"
chk "scoped, the same pages read normally" \
    "$(printf '%s/%s' "$(dispatch "$FN" "$(rpage "$D_UNRESOLVED_DONE")" '' '')" "$(dispatch "$FN" "$(rpage "$D_UNRESOLVED_DONE" "$D_DIED")" '' '')")" "none||||0/died|39523085356|completed|$DIED_URL|0"
chk "and the young run is still refused by both copies, so the scope did not simply disable the rule" \
    "$(printf '%s/%s' "$(dispatch "$CTRL_NOSCOPE" "$(rpage "$D_YOUNG")" '' '')" "$(dispatch "$FN" "$(rpage "$D_YOUNG")" '' '')")" "error(1)/error(1)"
CTRL_NOSINCE="$D/fn-locator-no-bound-guard.sh"
# Both halves of the bound guard: the emptiness test and the shape case
# are one rule - a bound this function can compare against - and a
# control that removed only the first would now be caught by the second
# and prove nothing about either.
# shellcheck disable=SC2016  # matched literally: the guard as SKILL.md spells it
sed '/runner_dispatch was given no bound for/d; /^  \[ -n "\$since" \] \\$/d; /^  case \$since in$/,/^  esac$/d' "$FN" > "$CTRL_NOSINCE"
chk "the no-bound control differs from the real function" "$(cmp -s "$FN" "$CTRL_NOSINCE" && echo same || echo differs)" differs
# shellcheck disable=SC1090
chk "no-bound control still loads" "$(. "$CTRL_NOSINCE"; type -t "$RUNNER_FN")" function
chk "unguarded, an empty bound admits the previous head's round as this head's dead dispatch" \
    "$(nobound "$CTRL_NOSINCE")" "died|39510000000|completed|https://example.invalid/runner/prev-head|0"
CTRL_UNGUARDED="$D/fn-locator-unguarded-jq.sh"
# The guards removed by a different route than the assertion reads
# them: the `||` arms become `|| true`, which is what their absence
# amounted to.
# shellcheck disable=SC2016  # matched literally: the arms as SKILL.md spells them
sed 's/|| { echo "ERROR: could not count .*/|| true/;
     s/|| { echo "ERROR: could not read the statuses of .*/|| true/;
     s/|| { echo "ERROR: could not read \$RUNNER_REPO.s newest run.*/|| true/' "$FN" > "$CTRL_UNGUARDED"
chk "the unguarded control differs from the real function" "$(cmp -s "$FN" "$CTRL_UNGUARDED" && echo same || echo differs)" differs
# shellcheck disable=SC1090
chk "unguarded control still loads" "$(. "$CTRL_UNGUARDED"; type -t "$RUNNER_FN")" function
chk "unguarded, a failing newest read prints a clean none the gate reads as 'nothing was dispatched'" \
    "$(jqfail "$CTRL_UNGUARDED" 5)" "none||||0"
chk "and a failing earlier read leaves the count field empty, which the gate's -eq 0 cannot test" \
    "$(jqfail "$CTRL_UNGUARDED" 3)" "died|39523085356|completed|$DIED_URL|"
CTRL_RETURN0="$D/fn-locator-guards-return-0.sh"
# A guard that reports and then returns 0. A helper that piped the
# function's status away would see the same empty stdout for this as for
# the real function's refusal; the pair below is what tells them apart,
# and it is visible only because jqfail captures the status.
# shellcheck disable=SC2016  # matched literally: the arms as SKILL.md spells them
sed 's/ >&2; return 1; }/ >\&2; return 0; }/g' "$FN" > "$CTRL_RETURN0"
chk "the return-0 control differs from the real function" "$(cmp -s "$FN" "$CTRL_RETURN0" && echo same || echo differs)" differs
# shellcheck disable=SC1090
chk "return-0 control still loads" "$(. "$CTRL_RETURN0"; type -t "$RUNNER_FN")" function
for n in 1 2 3 4 5; do
    chk "the real function refuses jq call $n with a status" "$(jqfail "$FN" "$n")" "error(1)"
    chk "the return-0 copy ends clean and empty on call $n, which the caller reads as a tuple" \
        "$(jqfail "$CTRL_RETURN0" "$n")" ""
done
# The status refusal is reached through dispatch(), not through jqfail, so
# it needs the return-0 copy driven down that path too: with the status
# captured the refusal reads error(1), and the copy that reports and
# returns 0 reads empty - the pair is what makes the status-refusal
# assertions a reading of the guard rather than of empty stdout.
chk "the statusless fixture refuses with a status through dispatch" \
    "$(dispatch "$FN" "$(rpage "$D_NOSTATUS")" '' '')" "error(1)"
chk "and the return-0 copy ends clean and empty on the same fixture" \
    "$(dispatch "$CTRL_RETURN0" "$(rpage "$D_NOSTATUS")" '' '')" ""
CTRL_NOSHALEN="$D/gate-block-without-sha-length.sh"
# Mutated by a different route than the assertion reads it, not by
# filtering the file with the needle and then counting the needle.
sed 's/-eq 40 \]/-eq 7 ]/' "$GATE_BLOCK" > "$CTRL_NOSHALEN"
chk "the sha-length control differs from the gate block" "$(cmp -s "$GATE_BLOCK" "$CTRL_NOSHALEN" && echo same || echo differs)" differs
# shellcheck disable=SC2016  # matched literally: the test as SKILL.md spells it
chk "and the same grep finds nothing in a copy that accepts an abbreviation" \
    "$(grep -c '^\[ "${#HEAD_SHA}" -eq 40 \] ||' "$CTRL_NOSHALEN")" 0
CTRL_NODECISION="$D/gate-arm-empty-runner-no-decision.sh"
# shellcheck disable=SC2016  # matched literally: the clause as SKILL.md spells it
sed "s/ Wait and re-ask; if it stays empty the workflow did not trigger on this push, and Review routing's local review (kind (ii)) is the path. Do not merge on a round that is not there\.//" "$LOC_ARM" > "$CTRL_NODECISION"
chk "the no-decision control differs from the gate's arm" "$(cmp -s "$LOC_ARM" "$CTRL_NODECISION" && echo same || echo differs)" differs
chk "without it the line explains and instructs nothing" \
    "$(arm "$CTRL_NODECISION" '' '' "$T_DIED" > /dev/null; grep -c 'Do not merge on a round that is not there' "$D/arm.out")" 0
CTRL_SILENTEMPTY="$D/gate-arm-silent-empty-runner.sh"
# shellcheck disable=SC2016  # matched literally: the line as SKILL.md spells it
sed "/RUNNER_REPO is empty, taken as 'this repository reviews itself'/d" "$LOC_ARM" > "$CTRL_SILENTEMPTY"
chk "the silent-empty control differs from the gate's arm" "$(cmp -s "$LOC_ARM" "$CTRL_SILENTEMPTY" && echo same || echo differs)" differs
chk "without the line nothing about RUNNER_REPO is said, and an unfilled literal reads as a configured one" \
    "$(arm "$CTRL_SILENTEMPTY" '' '' "$T_DIED" > /dev/null; grep -c 'RUNNER_REPO' "$D/arm.out")" 0
CTRL_MAXBOUND="$D/gate-arm-max-bound.sh"
# The superseded design in full: the committer date read on the server
# path too - which is where it used to be read - and the bound taken as
# the later of the two, with the label still chosen from the stamp's
# mere presence. The read has to come back with it, because the arm now
# fetches the committer date only on the path where it is the bound.
# shellcheck disable=SC2016  # matched literally: the branch as SKILL.md spells it
sed 's|^    head_time=\$head_push_time$|    head_commit_time=$(gh api repos/OWNER/REPO/commits/"$HEAD_SHA" --jq .commit.committer.date)\n    head_time=$(printf '"'"'%s\\n%s\\n'"'"' "$head_commit_time" "$head_push_time" \| sort \| tail -1)|' "$LOC_ARM" > "$CTRL_MAXBOUND"
# shellcheck disable=SC2016  # matched literally: the read as SKILL.md spells it
chk "the max-bound control really re-reads the committer date on the server path" \
    "$(grep -c 'head_commit_time=$(gh api repos/OWNER/REPO/commits' "$CTRL_MAXBOUND")" 2
chk "the max-bound control differs from the gate's arm" "$(cmp -s "$LOC_ARM" "$CTRL_MAXBOUND" && echo same || echo differs)" differs
chk "taking the later of the two, a skewed committer date becomes the bound" \
    "$(arm "$CTRL_MAXBOUND" '' "$RREPO" "$T_NONE" 2026-10-25T00:00:00Z > /dev/null; grep -c 'OWNER/REPO N 2026-10-25T00:00:00Z' "$D/loc-called")" 1
chk "and the label then contradicts it, which is the defect" \
    "$(arm "$CTRL_MAXBOUND" '' "$RREPO" "$T_NONE" 2026-10-25T00:00:00Z > /dev/null; grep -c 'since 2026-10-25T00:00:00Z (the head.s push stamp)' "$D/arm.out")" 1
CTRL_NONEUNGUARDED="$D/gate-arm-none-unguarded.sh"
# shellcheck disable=SC2016  # matched literally: the branch as SKILL.md spells it
sed 's/^    none)    if \[ "\$head_bound" = server \]; then$/    none)    if true; then/' "$LOC_ARM" > "$CTRL_NONEUNGUARDED"
chk "the none-arm control differs from the gate's arm" "$(cmp -s "$LOC_ARM" "$CTRL_NONEUNGUARDED" && echo same || echo differs)" differs
chk "unguarded, the committer path tells the operator to re-dispatch outright" \
    "$(arm "$CTRL_NONEUNGUARDED" '' "$RREPO" "$T_NONE" "$HEAD_COMMIT_TIME" '' > /dev/null; grep -c 'nothing was dispatched for it. Re-dispatch it' "$D/arm.out")" 1
CTRL_FAILOPEN="$D/gate-arm-died-unguarded.sh"
# shellcheck disable=SC2016  # matched literally: the test as SKILL.md spells it
sed 's/if \[ "\$head_bound" = server \]; then/if true; then/' "$LOC_ARM" > "$CTRL_FAILOPEN"
chk "the fail-closed control differs from the gate's arm" "$(cmp -s "$LOC_ARM" "$CTRL_FAILOPEN" && echo same || echo differs)" differs
chk "unguarded, the committer date alone sends the operator to kind (ii)" \
    "$(arm "$CTRL_FAILOPEN" '' "$RREPO" "$T_DIED" "$HEAD_COMMIT_TIME" '' > /dev/null; grep -c 'local review (kind (ii))' "$D/arm.out")" 1
CTRL_NOPAGEINFO="$D/gate-arm-query-without-pageinfo.sh"
sed 's/pageInfo { hasPreviousPage startCursor }/pageInfo { hasNextPage startCursor }/' "$LOC_ARM" > "$CTRL_NOPAGEINFO"
chk "the no-pageInfo control differs from the gate's arm" "$(cmp -s "$LOC_ARM" "$CTRL_NOPAGEINFO" && echo same || echo differs)" differs
chk "and the same grep finds nothing in a copy whose query selects another field" \
    "$(grep -c 'pageInfo { hasPreviousPage startCursor }' "$CTRL_NOPAGEINFO")" 0
CTRL_WIDEQ="$D/gate-arm-wide-query.sh"
sed 's/timelineItems(last:100, before:/timelineItems(last:300, before:/' "$LOC_ARM" > "$CTRL_WIDEQ"
chk "the wide-query control differs from the gate's arm" "$(cmp -s "$LOC_ARM" "$CTRL_WIDEQ" && echo same || echo differs)" differs
chk "and the same grep finds nothing in a copy that widened it" \
    "$(grep -c 'timelineItems(last:100, before:' "$CTRL_WIDEQ")" 0
# Reached through the arm, not only by calling gqlenv directly: the
# widened query is refused, the arm exits, and the operator is not told
# there was no force-push event.
chk "widened in the query, the read is refused and the arm stops" \
    "$(arm "$CTRL_WIDEQ" '' "$RREPO" "$T_NONE" "$HEAD_COMMIT_TIME" "$(fpush "$ARM_HEAD" "$HEAD_TIME")")" "exit(1)"
chk "and the refusal reaching it is the API's own" \
    "$(arm "$CTRL_WIDEQ" '' "$RREPO" "$T_NONE" "$HEAD_COMMIT_TIME" "$(fpush "$ARM_HEAD" "$HEAD_TIME")" > /dev/null; grep -c 'EXCESSIVE_PAGINATION' "$D/arm.out")" 1
chk "and it does not claim there was no force-push event" \
    "$(arm "$CTRL_WIDEQ" '' "$RREPO" "$T_NONE" "$HEAD_COMMIT_TIME" "$(fpush "$ARM_HEAD" "$HEAD_TIME")" > /dev/null; grep -c 'no force-push event' "$D/arm.out")" 0
chk "while the arm as written reads that same fixture without refusal" \
    "$(arm "$LOC_ARM" '' "$RREPO" "$T_NONE" "$HEAD_COMMIT_TIME" "$(fpush "$ARM_HEAD" "$HEAD_TIME")")" consulted
CTRL_NOWIDEN="$D/gate-arm-single-page.sh"
# shellcheck disable=SC2016  # matched literally: the test as SKILL.md spells it
sed 's/^    \[ "\$fpush_more" = true \] || break$/    break/' "$LOC_ARM" > "$CTRL_NOWIDEN"
chk "the single-page control differs from the gate's arm" "$(cmp -s "$LOC_ARM" "$CTRL_NOWIDEN" && echo same || echo differs)" differs
chk "without paging, the event beyond the first page is reported as no event at all" \
    "$(arm "$CTRL_NOWIDEN" '' "$RREPO" "$T_NONE" "$HEAD_COMMIT_TIME" "$ARM_WINDOW_OLD" > /dev/null; grep -c 'no force-push event' "$D/arm.out")" 1
CTRL_CAVEAT="$D/gate-arm-caveat-always.sh"
# shellcheck disable=SC2016  # matched literally: the test as SKILL.md spells it
sed 's/^  if \[ -n "\$head_push_time" \]; then$/  if false; then/' "$LOC_ARM" > "$CTRL_CAVEAT"
chk "the always-caveat control differs from the gate's arm" "$(cmp -s "$LOC_ARM" "$CTRL_CAVEAT" && echo same || echo differs)" differs
chk "mutated to take the committer branch regardless, the caveat IS printed, so 5l's 0 is a reading" \
    "$(arm "$CTRL_CAVEAT" '' "$RREPO" "$T_NONE" > /dev/null; grep -c 'no force-push event' "$D/arm.out")" 1
CTRL_ARMUNGUARDED="$D/gate-arm-unguarded-jq.sh"
# The arm's guards removed by a different route than the assertions
# read them.
# shellcheck disable=SC2016  # matched literally: the arms as SKILL.md spells them
sed 's/|| { echo "ERROR: could not read the force-push stamp.*/|| true/;
     s/|| { echo "ERROR: could not count the force-push events read.*/|| true/;
     s/|| { echo "ERROR: could not read whether older force-push events remain.*/|| true/;
     s/|| { echo "ERROR: could not read the cursor for the next page.*/|| true/' "$LOC_ARM" > "$CTRL_ARMUNGUARDED"
chk "the unguarded-arm control differs from the gate's arm" "$(cmp -s "$LOC_ARM" "$CTRL_ARMUNGUARDED" && echo same || echo differs)" differs
chk "unguarded, a failed stamp read collapses the bound to the committer date" \
    "$(arm "$CTRL_ARMUNGUARDED" '' "$RREPO" "$T_NONE" "$HEAD_COMMIT_TIME" '' '' '' '' 1 > /dev/null; grep -c "OWNER/REPO N $HEAD_COMMIT_TIME" "$D/loc-called")" 1
chk "and the gate states there was no force-push event, which is false" \
    "$(arm "$CTRL_ARMUNGUARDED" '' "$RREPO" "$T_NONE" "$HEAD_COMMIT_TIME" '' '' '' '' 1 > /dev/null; grep -c 'no force-push event' "$D/arm.out")" 1
# Belt and braces, and the pair says which is which: with the guard gone
# the shape check still stops a failed count read, and only with both
# gone does the empty count show through.
chk "unguarded, a failed count read is still stopped, by the shape check" \
    "$(arm "$CTRL_ARMUNGUARDED" '' "$RREPO" "$T_NONE" "$HEAD_COMMIT_TIME" '' '' '' '' 2 > /dev/null; grep -c "count on OWNER/REPO#N read back as '<empty>', not a number" "$D/arm.out")" 1
chk "unguarded, a failed hasPreviousPage read is caught by its shape check instead" \
    "$(arm "$CTRL_ARMUNGUARDED" '' "$RREPO" "$T_NONE" "$HEAD_COMMIT_TIME" '' '' '' '' 3 > /dev/null; grep -c "hasPreviousPage on OWNER/REPO#N read back as '<empty>', not a boolean" "$D/arm.out")" 1
chk "unguarded, a failed cursor read lets the walk stop silently instead of saying so" \
    "$(arm "$CTRL_ARMUNGUARDED" '' "$RREPO" "$T_NONE" "$HEAD_COMMIT_TIME" '' '' '' '' 4 > /dev/null; grep -c 'could not read the cursor for the next page' "$D/arm.out")" 0
CTRL_NEITHER="$D/gate-arm-count-unchecked.sh"
# shellcheck disable=SC2016  # matched literally: the arms as SKILL.md spells them
sed 's/|| { echo "ERROR: could not count the force-push events read.*/|| true/; /read back as .*, not a number/d' "$LOC_ARM" > "$CTRL_NEITHER"
chk "the neither-check control differs from the gate's arm" "$(cmp -s "$LOC_ARM" "$CTRL_NEITHER" && echo same || echo differs)" differs
chk "with neither, the empty count reaches the running total and the sentence reports nothing read" \
    "$(arm "$CTRL_NEITHER" '' "$RREPO" "$T_NONE" "$HEAD_COMMIT_TIME" '' '' '' '' 2 > /dev/null; grep -c 'among the 0 read on OWNER/REPO#N' "$D/arm.out")" 1
# Shape, not only exit status: jq exits 0 over a payload that simply has
# no `.pageInfo.hasPreviousPage`, so a readable answer that is not the
# expected one may not decide either. The query selection is a text
# check; what READS the field is the --jq path, so the behavioural
# control mutates that instead.
CTRL_BADMORE="$D/gate-arm-pageinfo-path.sh"
# shellcheck disable=SC2016  # matched literally: the path as SKILL.md spells it
sed 's/\.pageInfo\.hasPreviousPage/.pageInfo.hasNextPage/' "$LOC_ARM" > "$CTRL_BADMORE"
chk "the pageInfo-path control differs from the gate's arm" "$(cmp -s "$LOC_ARM" "$CTRL_BADMORE" && echo same || echo differs)" differs
chk "a non-boolean hasPreviousPage is refused, not carried into the sentence" \
    "$(arm "$CTRL_BADMORE" '' "$RREPO" "$T_NONE")" "exit(1)"
chk "and the refusal names what it read" \
    "$(arm "$CTRL_BADMORE" '' "$RREPO" "$T_NONE" > /dev/null; grep -c "hasPreviousPage on OWNER/REPO#N read back as 'null', not a boolean" "$D/arm.out")" 1
CTRL_NOSHAPE="$D/gate-arm-no-shape-checks.sh"
# shellcheck disable=SC2016  # matched literally: the arms as SKILL.md spells them
sed "/read back as .*, not a number/d; /which is not an ISO-8601 UTC timestamp/d" "$LOC_ARM" > "$CTRL_NOSHAPE"
# The same cut for the boolean, over the copy whose --jq reads the wrong
# field, so the unshaped value is one the API could really return.
sed "/not a boolean/d" "$CTRL_BADMORE" > "$D/gate-arm-no-shape-pageinfo.sh"
chk "the no-shape control differs from the gate's arm" "$(cmp -s "$LOC_ARM" "$CTRL_NOSHAPE" && echo same || echo differs)" differs
chk "without them a non-boolean hasPreviousPage reaches the sentence" \
    "$(arm "$D/gate-arm-no-shape-pageinfo.sh" '' "$RREPO" "$T_NONE" "$HEAD_COMMIT_TIME" '' > /dev/null; grep -c 'older ones remain: null' "$D/arm.out")" 1
chk "and a nonsense stamp becomes the bound" \
    "$(arm "$CTRL_NOSHAPE" '' "$RREPO" "$T_NONE" '' "$(fpush "$ARM_HEAD" 'last Tuesday')" > /dev/null; grep -c "OWNER/REPO N last Tuesday" "$D/loc-called")" 1
CTRL_PIPE="$D/gate-arm-piped-read.sh"
# The read made the head of a pipeline, so the `||` tests the LAST
# command's status and not gh's. Rebuilt by appending `| cat` to the
# read rather than by rejoining two statements, because `fpush_raw` is
# read by more than one statement and there is nothing to rejoin it
# into.
# shellcheck disable=SC2016  # matched literally: the line as SKILL.md spells it
sed "s/| @json') \\\\$/| @json' | cat) \\\\/" "$LOC_ARM" > "$CTRL_PIPE"
chk "the pipeline control differs from the gate's arm" "$(cmp -s "$LOC_ARM" "$CTRL_PIPE" && echo same || echo differs)" differs
chk "the control really made the read a pipeline" "$(grep -c "| @json' | cat)" "$CTRL_PIPE")" 1
chk "piped, the failed read's own message is lost" \
    "$(arm "$CTRL_PIPE" '' "$RREPO" "$T_NONE" "$HEAD_COMMIT_TIME" "$(fpush "$ARM_HEAD" "$HEAD_TIME")" graphql > /dev/null; grep -c 'could not read page 1 of the force-push events' "$D/arm.out")" 0
# It still blocks, but by the count's shape check two reads later - belt
# and braces, and the pair says which is doing the work.
chk "and the block that catches it instead is the count's shape check" \
    "$(arm "$CTRL_PIPE" '' "$RREPO" "$T_NONE" "$HEAD_COMMIT_TIME" "$(fpush "$ARM_HEAD" "$HEAD_TIME")" graphql > /dev/null; grep -c "count on OWNER/REPO#N read back as '<empty>', not a number" "$D/arm.out")" 1
# The `$8` knob, exercised rather than declared. The drill turns
# pipefail OFF for the sourced arm because the weaker shell is the one
# the block has to survive - and this pair is why that matters: under
# pipefail the piped read's failure DOES propagate, so the mutation
# CTRL_PIPE makes is invisible, and the control written to catch it
# would be green for the wrong reason. Both shells block, so nothing
# is unsafe either way; what differs is whether the control can see
# the defect.
chk "under pipefail the piped read's own message survives, which is why the drill runs the arm without it" \
    "$(arm "$CTRL_PIPE" '' "$RREPO" "$T_NONE" "$HEAD_COMMIT_TIME" "$(fpush "$ARM_HEAD" "$HEAD_TIME")" graphql pipefail > /dev/null; grep -c 'could not read page 1 of the force-push events' "$D/arm.out")" 1
chk "and without it the same mutation loses that message, which is the defect the control exists to show" \
    "$(arm "$CTRL_PIPE" '' "$RREPO" "$T_NONE" "$HEAD_COMMIT_TIME" "$(fpush "$ARM_HEAD" "$HEAD_TIME")" graphql > /dev/null; grep -c 'could not read page 1 of the force-push events' "$D/arm.out")" 0
chk "the piped form blocks under pipefail too, so the knob changes what is seen and not whether it is safe" \
    "$(arm "$CTRL_PIPE" '' "$RREPO" "$T_NONE" "$HEAD_COMMIT_TIME" "$(fpush "$ARM_HEAD" "$HEAD_TIME")" graphql pipefail)" "exit(1)"
chk "and the arm as written keeps its message under pipefail as well" \
    "$(arm "$LOC_ARM" '' "$RREPO" "$T_NONE" "$HEAD_COMMIT_TIME" "$(fpush "$ARM_HEAD" "$HEAD_TIME")" graphql pipefail > /dev/null; grep -c 'could not read page 1 of the force-push events' "$D/arm.out")" 1
chk "both forms block" \
    "$(printf '%s/%s' "$(arm "$CTRL_PIPE" '' "$RREPO" "$T_NONE" "$HEAD_COMMIT_TIME" "$(fpush "$ARM_HEAD" "$HEAD_TIME")" graphql)" "$(arm "$LOC_ARM" '' "$RREPO" "$T_NONE" "$HEAD_COMMIT_TIME" "$(fpush "$ARM_HEAD" "$HEAD_TIME")" graphql)")" "exit(1)/exit(1)"
CTRL_GQLFIELD="$D/gate-arm-wrong-gql-field.sh"
# The projection is exercised, not assumed: a wrong field name in the
# gate's own --jq must change the answer.
sed 's/\.afterCommit\.oid/.afterCommit.sha/' "$LOC_ARM" > "$CTRL_GQLFIELD"
chk "the projection control differs from the gate's arm" "$(cmp -s "$LOC_ARM" "$CTRL_GQLFIELD" && echo same || echo differs)" differs
chk "a wrong field in the gate's GraphQL projection loses the event, so the stub really runs it" \
    "$(arm "$CTRL_GQLFIELD" '' "$RREPO" "$T_NONE" > /dev/null; grep -c "OWNER/REPO N $HEAD_COMMIT_TIME" "$D/loc-called")" 1
CTRL_NODEFAULT="$D/gate-arm-no-default-case.sh"
# shellcheck disable=SC2016  # matched literally: the arm as SKILL.md spells it
sed "/^    \*)       echo \"ERROR: runner_dispatch returned/d" "$LOC_ARM" > "$CTRL_NODEFAULT"
chk "the default-arm control differs from the gate's arm" "$(cmp -s "$LOC_ARM" "$CTRL_NODEFAULT" && echo same || echo differs)" differs
chk "without it an unknown reading prints nothing and the gate carries on" \
    "$(arm "$CTRL_NODEFAULT" '' "$RREPO" "$(printf 'whatever\037\037\037\0370')")" consulted
CTRL_NOCOUNTCHECK="$D/gate-arm-count-unshaped.sh"
# shellcheck disable=SC2016  # matched literally: the arm as SKILL.md spells it
sed "/earlier runs, which is not a count/d" "$LOC_ARM" > "$CTRL_NOCOUNTCHECK"
chk "the unshaped-count control differs from the gate's arm" "$(cmp -s "$LOC_ARM" "$CTRL_NOCOUNTCHECK" && echo same || echo differs)" differs
chk "without it the empty count reaches -eq, which errors and prints the line anyway" \
    "$(arm "$CTRL_NOCOUNTCHECK" '' "$RREPO" "$T_DIED_NOCOUNT" > /dev/null; grep -c 'earlier run(s) for this PR predate this head' "$D/arm.out")" 1
# The locator's own guard is not in a function - it is the `elif` that
# makes the call reachable only from step 8's [] row. Cut it to a plain
# `if` and the gate consults the runner on a head it already found a
# round for, which is both a wasted read of someone else's repository
# and a `posted` line claiming the block has been edited when it has
# not.
CTRL_GUARD="$D/gate-arm-no-guard.sh"
# shellcheck disable=SC2016  # matched literally: the elif as SKILL.md spells it
sed 's/^elif \[ -n "\$RUNNER_REPO" \]; then$/fi\nif [ -n "$RUNNER_REPO" ]; then/' "$LOC_ARM" > "$CTRL_GUARD"
chk "the guard control differs from the gate's arm" "$(cmp -s "$LOC_ARM" "$CTRL_GUARD" && echo same || echo differs)" differs
# The arm blocks rather than warns: a gate that has announced it cannot
# be trusted may not go on to decide, and every other failure in the
# block exits.
chk "without the guard the locator is consulted though a round was discovered, and the gate stops" \
    "$(arm "$CTRL_GUARD" check-run "$RREPO" "$T_POSTED")" "exit(1)"
chk "and it prints the line that cannot be reached, on a head nothing is wrong with" \
    "$(arm "$CTRL_GUARD" check-run "$RREPO" "$T_POSTED" > /dev/null; grep -c 'the gate block has been edited' "$D/arm.out")" 1
CTRL_POSTEDWARN="$D/gate-arm-posted-warns.sh"
# shellcheck disable=SC2016  # matched literally: the arm as SKILL.md spells it
sed 's/it blocks rather than warns, because a gate that has announced it cannot be trusted may not go on to decide"; exit 1 ;;/read it before trusting anything below" ;;/' "$CTRL_GUARD" > "$CTRL_POSTEDWARN"
chk "the warn-only control differs from the guard control" "$(cmp -s "$CTRL_GUARD" "$CTRL_POSTEDWARN" && echo same || echo differs)" differs
chk "warning only, the gate carries on past a state it just called impossible" \
    "$(arm "$CTRL_POSTEDWARN" check-run "$RREPO" "$T_POSTED")" consulted
chk "with the guard in place it is skipped" \
    "$(arm "$LOC_ARM" check-run "$RREPO" "$T_POSTED")" skipped
chk "and nothing about the runner is printed on that head" \
    "$(arm "$LOC_ARM" check-run "$RREPO" "$T_POSTED" > /dev/null; grep -c "$RREPO" "$D/arm.out")" 0

echo "9. control: the extraction sees nothing where the functions are absent"
STRIPPED="$D/skill-stripped.md"
sed "/^${FINDING_FN}() {\$/,/^}\$/d; /^${FUNC_NAME}() {\$/,/^}\$/d; /^${MAJORS_FN}() {\$/,/^}\$/d; /^${THREAD_FN}() {\$/,/^}\$/d; /^${WINDOW_FN}() {\$/,/^}\$/d; /^${AGG_FN}() {\$/,/^}\$/d; /^${HEADLINE_FN}() {\$/,/^}\$/d; /^${COUNT_FN}() {\$/,/^}\$/d; /^${CUTOFF_FN}() {\$/,/^}\$/d; /^${SINCE_FN}() {\$/,/^}\$/d; /^${CONV_FN}() {\$/,/^}\$/d; /^${DISP_FN}() {\$/,/^}\$/d; /^${KNOWN_FN}() {\$/,/^}\$/d; /^${RUNNER_FN}() {\$/,/^}\$/d" "$SKILL" > "$STRIPPED"
for f in "$FINDING_FN" "$FUNC_NAME" "$MAJORS_FN" "$THREAD_FN" "$WINDOW_FN" "$AGG_FN" "$HEADLINE_FN" "$COUNT_FN" "$CUTOFF_FN" "$SINCE_FN" "$CONV_FN" "$DISP_FN" "$KNOWN_FN" "$RUNNER_FN"; do
    chk "the stripped copy no longer defines $f" "$(grep -c "^$f() {\$" "$STRIPPED")" 0
done
extract_all "$STRIPPED" > "$D/fn-stripped.sh"
chk "extraction from the stripped copy is empty" "$([ -s "$D/fn-stripped.sh" ] && echo yes || echo no)" no

echo
echo "RESULT: $pass passed, $fail failed, $skipped skipped"
# $pass > 0 as well: a drill whose scenarios all stopped running would
# otherwise report a clean sweep of nothing. A skip is counted, not hidden,
# so a run on a machine without shellcheck says so on its last line.
[ "$fail" -eq 0 ] && [ "$pass" -gt 0 ]
