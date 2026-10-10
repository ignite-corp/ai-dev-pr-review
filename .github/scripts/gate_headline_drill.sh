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
#      App-shaped PR is accepted, and the two comment fallbacks - with the
#      `Intended verdict` note appended, and the bare timeout one - are
#      the aggregate present, their headline read by aggregate_headline,
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
#
# All twelve functions are extracted from SKILL.md by name, not copied here:
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
# out must pass a mistyped name. Without them a refusal above would
# only prove that the function refuses, not that it refuses for the line the
# drill names. Section 9 is the control on the extraction itself.
#
# Touches nothing real: no gh calls, no PR, no comments.

set -uo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
SKILL="$HERE/../../plugins/pr-response-cycle/skills/pr-response-cycle/SKILL.md"
FUNC_NAME=headline_roster
MAJORS_FN=major_dispositions
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

echo "1. all twelve functions are in SKILL.md, once each, and load"
FN="$D/fn.sh"
extract_all() { # $1 file
    extract "$1" "$FUNC_NAME"; extract "$1" "$MAJORS_FN"; extract "$1" "$THREAD_FN"; extract "$1" "$WINDOW_FN"; extract "$1" "$AGG_FN"; extract "$1" "$HEADLINE_FN"; extract "$1" "$COUNT_FN"; extract "$1" "$CUTOFF_FN"; extract "$1" "$SINCE_FN"; extract "$1" "$CONV_FN"; extract "$1" "$DISP_FN"; extract "$1" "$KNOWN_FN"
}
extract_all "$SKILL" > "$FN"
for f in "$FUNC_NAME" "$MAJORS_FN" "$THREAD_FN" "$WINDOW_FN" "$AGG_FN" "$HEADLINE_FN" "$COUNT_FN" "$CUTOFF_FN" "$SINCE_FN" "$CONV_FN" "$DISP_FN" "$KNOWN_FN"; do
    chk "SKILL.md defines $f exactly once" "$(grep -c "^$f() {\$" "$SKILL")" 1
done
chk "the extraction is non-empty" "$([ -s "$FN" ] && echo yes || echo no)" yes
chk "the extraction holds twelve closing braces, one per function" "$(grep -c '^}$' "$FN")" 12
# The truncation guard, one assertion per function: each must end at its
# own `return` or tally line followed by the brace -- the shape a truncation
# cannot have. Nothing else here can see one (see extract above).
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
# shellcheck disable=SC1090
chk "sourcing it defines the roster function" "$(. "$FN"; type -t "$FUNC_NAME")" function
# shellcheck disable=SC1090
chk "sourcing it defines the majors function" "$(. "$FN"; type -t "$MAJORS_FN")" function
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
# falls back to an issue comment with an `Intended verdict` note appended
# after the body, and a timeout to the bare body, byte-identical to a
# comment-only post (AT-2597).
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
C_BARE=$(cmt 12 2026-10-02T10:09:06Z 'github-actions[bot]' Bot "$MARK"$'\n'"$H_REVIEW_NO_ISSUES")
chk "the review-API-failure fallback comment, note appended, is the aggregate present" "$(agg "$FN" "$(lst "$C_NOTED")" '[]' "$HEAD_IC" 2026-10-02T10:00:00Z 2026-10-02T10:10:00Z)" comment:11:comment
chk "its headline is read with the note present" "$(headline_of "$FN" "$C_NOTED")" "$H_REVIEW_NO_ISSUES"
chk "and passes the roster" "$(verdict "$FN" "$(headline_of "$FN" "$C_NOTED")")" pass
chk "the timeout fallback comment, no note, is the aggregate present" "$(agg "$FN" "$(lst "$C_BARE")" '[]' "$HEAD_IC" 2026-10-02T10:00:00Z 2026-10-02T10:10:00Z)" comment:12:comment
chk "its headline is read without any note" "$(headline_of "$FN" "$C_BARE")" "$H_REVIEW_NO_ISSUES"
chk "and passes the roster" "$(verdict "$FN" "$(headline_of "$FN" "$C_BARE")")" pass
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
CUT_R9='<!-- round-cutoff-claude-r9 -->
## [bot] claude Round Cutoff Summary (R9)

R9 convergence cutoff -- the following minor items are recommended for follow-up:'
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
    "$(cutoff "$FN" "$(lst "$C_CUTOFF" "$C_DISP" "$C_CUTOFF_R9" "$(cmt 106 2026-10-06T22:00:00Z hyuk-hur User '> an R9 item
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

> **minor** `.github/scripts/aggregate_reviews.py:61` -- dead helper
> **suggestion** `.github/scripts/tests/test_aggregate_reviews.py:44` -- second import'
C_R9_LOCAL=$(cmt 125 2026-10-06T11:35:00Z hyuk-hur User '<!-- round-cutoff-claude-r9 -->
'"$R9_OVER_R7")
C_R9_AS_HUMAN=$(cmt 126 2026-10-06T11:35:00Z hyuk-hur User "$R9_OVER_R7")
# R9 itself is dispositioned, so r9 is not what any verdict below turns on:
# it carries no `- **loc** --` item line, and naming its marker answers it.
C_DISP_R9=$(cmt 127 2026-10-06T11:50:00Z hyuk-hur User 'round-cutoff-claude-r9 handled in the fixup.')
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
sed 's/(\$m | rx) + "(?!\[0-9\])"/($m | rx)/' "$FN" > "$CTRL_DIGIT"
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
sed '/^'"${CUTOFF_FN}"'() {$/,/^}$/{s/select((\.created_at | type) == "string" and \.created_at >= \$s)/select(.type == "Bot") | &/;}' "$FN" > "$CTRL_AUTHOR_SCAN"
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

echo "9. control: the extraction sees nothing where the functions are absent"
STRIPPED="$D/skill-stripped.md"
sed "/^${FUNC_NAME}() {\$/,/^}\$/d; /^${MAJORS_FN}() {\$/,/^}\$/d; /^${THREAD_FN}() {\$/,/^}\$/d; /^${WINDOW_FN}() {\$/,/^}\$/d; /^${AGG_FN}() {\$/,/^}\$/d; /^${HEADLINE_FN}() {\$/,/^}\$/d; /^${COUNT_FN}() {\$/,/^}\$/d; /^${CUTOFF_FN}() {\$/,/^}\$/d; /^${SINCE_FN}() {\$/,/^}\$/d; /^${CONV_FN}() {\$/,/^}\$/d; /^${DISP_FN}() {\$/,/^}\$/d; /^${KNOWN_FN}() {\$/,/^}\$/d" "$SKILL" > "$STRIPPED"
for f in "$FUNC_NAME" "$MAJORS_FN" "$THREAD_FN" "$WINDOW_FN" "$AGG_FN" "$HEADLINE_FN" "$COUNT_FN" "$CUTOFF_FN" "$SINCE_FN" "$CONV_FN" "$DISP_FN" "$KNOWN_FN"; do
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
