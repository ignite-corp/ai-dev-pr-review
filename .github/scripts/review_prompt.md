Read `context.md` (review guidelines) and `pr.diff` (unified diff) in the current directory.

Review the diff from three perspectives:
1. Code Quality -- architecture layers, naming, type hints, magic numbers, function/class size, dead code
2. Security -- OWASP Top 10 (injection, broken auth, hardcoded secrets, insecure config, input validation)
3. Spec Compliance -- Clean Architecture boundaries, API/DB spec alignment, naming conventions

IMPORTANT: Focus on NEW issues only. If the context includes previously
resolved review threads, check their responses before re-raising the same
issue -- only re-raise if the current code has materially changed.

EVIDENCE RULE: Raise a finding ONLY if you can point to the exact line(s) in
THIS diff that exhibit it. Any existence or correctness claim (e.g., "X does
not exist", "Y is undefined") MUST quote the diff line(s) where the reference is used. If a
claim depends on runtime, library, or environment facts you are not certain
of, downgrade it to "suggestion" or omit it.

DIFF SCOPE: Anchor every finding to a line this diff adds or changes (prefixed
with `+`). If a defect is caused by a removal (e.g., deleted validation, check,
or error handling), report it at the nearest affected remaining line and quote
the removed (`-`) line as evidence. Context lines (space-prefixed) that this PR
does not touch are out of scope -- do NOT raise issues about them.

COMPLETENESS RULE: Do not claim that a list, table, enum, or other multi-part
structure is complete, or state its count, based on this diff alone -- rows,
items, or members outside the visible hunk are not something you can see. If
such a claim is tempting, mark it as unverifiable from the provided context
instead of raising it as a finding.

LINE NUMBERS: The `line` you report MUST be the target file's OWN line number
(the right-hand/new-file line -- the number you would see in `git blame` or
when opening the file directly), NOT the line's position within the `pr.diff`
document you are reading. Unified diff hunk headers look like
`@@ -a,b +c,d @@`; the new-file line number starts at `c` for the first line
after that header and increments by one for each `+` or context (space-
prefixed) line in the hunk -- it does NOT increment for `-` lines. When a PR
touches multiple files, each file's line numbering restarts independently of
where that file's diff appears in the combined document.

CONGRUENCE: Ask three questions of the change. Definition: ~/.claude/AGENTS.md, ### Congruent features.
- Self: does the change do what it says, and are unknowns written as unknown? Typical misses are an unmerged effect written in the present tense, and runtime behaviour asserted without naming the revision it was read from.
- Other: does it serve the actual need rather than the wording, and if the two differ, is the gap stated? A gap the builder resolves alone is placating or superreasonable.
- Context: does it extend the neighbouring pattern rather than add a second way? Where it must differ from its siblings, does it differ visibly with the reason written there?
A difference is not itself a defect; a difference that reads as drift because no reason is given is.
The PR body is not part of your input: judge what the diff itself says. An incongruence is a finding like any other, under the EVIDENCE RULE and DIFF SCOPE above.

## GitHub Actions SHA Pins

When the diff pins a SHA for a GitHub Action (`uses: owner/repo@<SHA>`), consult the `<verified-action-pins>` section in context.md and apply these rules by status:

- **`status="verified"`** -- The SHA matched a tag in the upstream repository's tag listing. Treat the SHA as definitively valid: do NOT raise a critical/major about SHA invalidity; any such claim would be a false positive.
  - If the same pin carries **`comment-matches="false"`**, the `# vX.Y` version comment in the diff drifts from the resolved upstream tag (given in the `tag="..."` attribute). This IS a legitimate finding -- raise as `minor` and cite the `tag=` value as the correct version comment.
  - The `comment-matches` attribute appears **only when the diff line carried a `# vX.Y` comment**. If the attribute is absent from the `<pin>` element, the PR chose not to annotate the pin with a version comment -- that is not a finding and MUST NOT be flagged as a comment mismatch.
- **`status="unmatched"`** -- The tag listing was fetched successfully but the SHA did not appear in any tag. This indicates a stale mirror, a force-pushed tag, or a wrong/typo'd SHA. Raise as `major`: the upstream repo is reachable, so the discrepancy is deterministic, not transient.
- **`status="unverified"` with `error="repo-not-found"`** -- The API returned 404 for a repository whose owner is not the owner of the repository under review, or the run's own owner could not be determined. GitHub answers 404 both for a repository that does not exist and for a private one this run's token may not read, so the 404 does NOT establish that the repository is absent. Where the owners are known to differ, the pin is usually a public action, which makes a misspelled `owner/repo` the likelier of the two. Ask the author to confirm the `owner/repo` spelling; do NOT claim the repository does not exist. Raise only as `suggestion`.
- **`status="unverified"` with `error="repo-not-visible"`** -- The API returned 404 for a repository whose owner is the owner of the repository under review. The same two causes are indistinguishable here, and when the owners match the likelier one is a private repository this run's token cannot read. Still ask the author to confirm the `owner/repo` spelling, since a typo or a renamed/deleted action looks identical and would otherwise fail only at runtime; do NOT claim the repository does not exist. Raise only as `suggestion`.
- **`status="unverified"` with any other `error`** (`rate-limit`, `timeout`, `api-error: ...`, `json-decode-error`, `unknown`) -- The verification itself failed transiently; we could not confirm or deny the pin. Raise only as `suggestion`; do not raise `critical`/`major` since the SHA was not actually checked.

## Output format

Write ONLY the following JSON to `review-codex.json` (no other output, no markdown fences):
{
  "summary": "<1-2 sentence summary>",
  "status": "ok" | "early_exit",
  "early_exit": <bool>,
  "issues": [
    {
      "severity": "critical" | "major" | "minor" | "suggestion",
      "file": "<file path or null>",
      "line": <integer or null>,
      "description": "<concise description>",
      "suggestion": "<fix suggestion or null>"
    }
  ]
}

early_exit rules:
- true ONLY for fundamental flaws that make further review pointless (e.g., entire design must be scrapped)
- false for normal critical/major issues that other reviewers should still evaluate
- false for documented/acknowledged technical constraints

status rules:
- "ok" when the review completed normally
- "early_exit" when early_exit is true
- never emit "failed" -- it is reserved for reviewer infrastructure failures

If no issues found, write {"summary": "No issues found.", "status": "ok", "early_exit": false, "issues": []}.

Do NOT post any PR comments or reviews. Only write review-codex.json.
