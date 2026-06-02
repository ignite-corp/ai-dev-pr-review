# AT-1210 Security Audit

Source: `ignite-corp/ai-dev-cab` main branch at commit `84ddf8ccc760027eb8fa94c954c44bd99014e22f` (AT-1185 #210 merge).

## Decision summary

| Decision | Count | Files |
|---|---|---|
| EXTRACT | 11 | All four `ai-review-*.yml`, all helper `*.py` and `*.sh` scripts except review_prompt.md, requirements.txt, .python-version, threads.jq, schemas/review-schema.json |
| MODIFY-THEN-EXTRACT | 6 | All four workflows (renamed, `on: workflow_call`, secret contract, script paths rerouted), `collect_review_threads.sh` (resolve jq path relative to script), `review_prompt.md` (generic — keep) |
| KEEP PRIVATE | 5 | `code-review-system.md`, `code-review-checklist.md`, all three test files |

Net: 16 files published to the public repo; 5 kept private in caller repos.

## Per-file audit

| File | Decision | Reasoning |
|---|---|---|
| `.github/workflows/ai-review-orchestrator.yml` | MODIFY-THEN-EXTRACT | Generic orchestration. Rename to `base-ai-review-orchestrator.yml`; replace `on: pull_request` with `on: workflow_call`; declare explicit `secrets:` block (OPENAI_API_KEY, GOOGLE_AI_API_KEY, CLAUDE_CODE_OAUTH_TOKEN — required:true); replace `secrets: inherit` on internal `uses:` with explicit pass-through (cross-org safe); thread `pr_number`, `code-review-system-prompt-path`, `code-review-checklist-path` inputs through to children. No company strings present. |
| `.github/workflows/ai-review-prepare.yml` | MODIFY-THEN-EXTRACT | Generic prepare step. Rename to `base-ai-review-prepare.yml`; add `workflow_call` inputs for prompt paths; checkout BOTH the caller repo (PR head) AND `ignite-corp/ai-dev-pr-review` at the consumer's pinned ref into `.ai-dev-pr-review/`; reroute script invocations to `.ai-dev-pr-review/.github/scripts/`. Replaced the `> .github/prompts/code-review-system.md` hardcoded paths with `${SYSTEM_PROMPT_PATH}` / `${CHECKLIST_PATH}` env vars sourced from inputs. The `[!]` warning emoji replacement is the only string the consumer might want to override (cosmetic). |
| `.github/workflows/ai-review-single.yml` | MODIFY-THEN-EXTRACT | Generic per-reviewer driver. Rename to `base-ai-review-single.yml`; same dual-checkout pattern; replace `git show "origin/${BASE_REF}:.github/scripts/review_prompt.md"` (which would fetch from the consumer repo — unsafe + wrong source) with a direct `cp` from the trusted `.ai-dev-pr-review/.github/scripts/review_prompt.md`. Em-dashes/arrows replaced with `--` and `->` in echoed prompts. |
| `.github/workflows/ai-review-aggregate.yml` | MODIFY-THEN-EXTRACT | Generic verdict aggregator. Rename to `base-ai-review-aggregate.yml`; same dual-checkout; reroute `python .github/scripts/aggregate_reviews.py` to `.ai-dev-pr-review/.github/scripts/aggregate_reviews.py`. No company strings. |
| `.github/scripts/aggregate_reviews.py` | MODIFY-THEN-EXTRACT | Generic verdict logic. **Behavior change**: `SEVERITY_ICONS` mapping in `github_pr_support.py` changes from emoji to ASCII (`!`, `+`, `-`, `?`) to satisfy the ASCII-only verification rule. Consumer-visible verdict comments now show `! **critical**` instead of `[CRIT] **critical**`. Em-dashes in summary strings replaced with `--`. No company logic. |
| `.github/scripts/extract_claude_review.py` | EXTRACT | Generic Claude execution-log scraper. Em-dashes replaced. |
| `.github/scripts/fetch_review_context.py` | EXTRACT | Generic GraphQL query for resolved/by-design context. Em-dashes/arrows replaced. No company strings. |
| `.github/scripts/github_pr_support.py` | MODIFY-THEN-EXTRACT | Shared helper. `SEVERITY_ICONS` switched from emoji (`U+1F534` etc.) to single ASCII chars so `_ICON_RE` character-class regex in `post_inline_comments.py` continues to compile and the file passes the ASCII grep gate. Other content unchanged. |
| `.github/scripts/post_inline_comments.py` | EXTRACT | Generic GH PR Reviews API client. `_FALLBACK_HEADER` had `[BOT]` (was emoji) but the only other unicode was em-dashes; all replaced. The dedup regex is unchanged because the icons are still single chars. |
| `.github/scripts/review_gemini.py` | EXTRACT | Generic google-genai client. No company strings; em-dashes replaced. |
| `.github/scripts/verify_action_shas.py` | EXTRACT | Generic GitHub Actions SHA verifier. Em-dashes replaced. |
| `.github/scripts/collect_review_threads.sh` | MODIFY-THEN-EXTRACT | One line changed: `jq -f .github/scripts/threads.jq` -> resolve relative to `${BASH_SOURCE[0]}` directory. Otherwise generic. |
| `.github/scripts/threads.jq` | EXTRACT | Filter accepts `gemini-code-assist` and `github-actions` as bot authors. Both are public GitHub identities; no leakage. |
| `.github/scripts/review_prompt.md` | EXTRACT | Audited carefully: contains only the generic three-perspective scaffolding (Code Quality / Security / Spec Compliance), JSON output schema, and `early_exit` rules. No QAyak / Clean Architecture / FastAPI mentions. The "Clean Architecture boundaries, API/DB spec alignment, naming conventions" phrase is a generic engineering bullet, not company-specific. Em-dashes replaced. |
| `.github/scripts/requirements.txt` | EXTRACT | Single line `google-genai>=2.7.0`. |
| `.github/scripts/.python-version` | EXTRACT | `3.14.4`. Public-safe. |
| `.github/schemas/review-schema.json` | EXTRACT | JSON Schema for the per-reviewer output. Generic. Referenced by `review_gemini.py` AND `test_schema_genai_compat.py`. |
| `.github/prompts/code-review-system.md` | KEEP PRIVATE | Heavy QAyak/Clean Architecture/dependency-injector content, internal docs spec paths (`docs/specs/T4_01_API_Spec.md`, etc.), domain naming taboos (`Manager`, `Helper`, `Utils`). Stays in each caller repo via the new `code-review-system-prompt-path` input. |
| `.github/prompts/code-review-checklist.md` | KEEP PRIVATE | References `docs/guides/naming-conventions.md` (caller-repo internal). Same input mechanism keeps it private. |
| `.github/scripts/test_aggregate_reviews.py` | KEEP PRIVATE | Tests reach into the scripts under test from the original `.github/scripts/` path. Caller repos can keep them locally if they bundle the scripts; the public repo will gain its own test layout in a follow-up. Not a security blocker. |
| `.github/scripts/test_verify_action_shas.py` | KEEP PRIVATE | Same. Tests refer to commit SHAs and mock data — generic content but tightly coupled to in-tree imports. |
| `.github/scripts/test_schema_genai_compat.py` | KEEP PRIVATE | Same. Open question: include tests in v1.1.0 once the public repo has CI. |

## What was specifically removed / parameterized

1. **`on: pull_request` triggers** -> replaced with `on: workflow_call` so callers control trigger semantics. The orchestrator's `pull_request: branches: [main, develop]` selection now lives in the consumer thin trigger.
2. **`secrets: inherit`** in orchestrator's child `uses:` -> replaced with explicit `secrets:` mappings (the inner reusable workflows declare their secret contract; the orchestrator forwards from its own `secrets:` block). This is required for cross-org reuse where `secrets: inherit` is silently empty.
3. **`workflow_dispatch` input duplication** -> the orchestrator no longer has its own `workflow_dispatch`; the consumer thin trigger declares it and forwards `pr_number` via `with:`.
4. **Hardcoded prompt paths** (`.github/prompts/code-review-system.md`, `.github/prompts/code-review-checklist.md`) -> replaced with `inputs.code-review-system-prompt-path` and `inputs.code-review-checklist-path`, defaulting to the original locations for back-compat.
5. **`git show "origin/${BASE_REF}:.github/scripts/review_prompt.md"`** (in `ai-review-single.yml`) -> replaced with a direct `cp .ai-dev-pr-review/.github/scripts/review_prompt.md`. The original fetched from the caller's base branch (correct for the in-tree layout) but now must come from the reusable repo itself, which is trusted by construction because the consumer pins it with `@v1.0.0`.
6. **`jq -f .github/scripts/threads.jq`** -> resolved relative to `${BASH_SOURCE[0]}` so the script works when invoked from any cwd.
7. **Severity emoji icons** (red/orange/yellow circles, light bulb) -> replaced with single-char ASCII (`!`, `+`, `-`, `?`) so the file passes `grep -rnP '[^\x00-\x7F]'` and the existing character-class regex in `post_inline_comments._ICON_RE` continues to compile without rewrite.
8. **Em-dashes (`--`), arrows (`->`), warning sign (`[!]`), robot face (`[bot]`)** in comments, docstrings, and echoed prompt strings -> ASCII-substituted by `/tmp/ascii_normalize.py`.

## Residual risks / decisions for user

- Severity icon visual regression: consumer PR verdict comments will look different (`! **critical**` vs `[CRIT] **critical**`). If keeping emoji is preferred, drop the strict ASCII rule for `github_pr_support.py` and add `# noqa` / explicit allow-list. See README "Severity icons" note.
- The Claude prompt is built inline inside `base-ai-review-single.yml` (not imported from `review_prompt.md`). A future task should unify both reviewers on `review_prompt.md` so changes propagate in one place.
- Test files NOT extracted: open question — port to public CI in v1.1.0 vs leave indefinitely.
- README links and the `repository: ignite-corp/ai-dev-pr-review` literal inside each base workflow's nested checkout assume the repo name. If the user picks a different name at creation time, update three locations: orchestrator/prepare/single/aggregate workflow checkout `repository:` line.
