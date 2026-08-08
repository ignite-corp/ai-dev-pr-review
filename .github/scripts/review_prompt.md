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
