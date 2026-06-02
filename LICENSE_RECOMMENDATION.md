# LICENSE recommendation for `ignite-corp/ai-dev-pr-review`

## TL;DR — recommend Apache License 2.0.

## Options compared

| Aspect | MIT | Apache 2.0 | Proprietary (LicenseRef-Ignite) |
|---|---|---|---|
| Permissiveness | Maximum | High | None — public-visibility-only |
| Patent grant from contributors | None implicit | Explicit grant + termination on patent litigation | N/A |
| Attribution requirement | Yes (short notice) | Yes (NOTICE file + copyright header) | N/A |
| Modification disclosure | Not required | Not required, but changed files must be marked | N/A |
| Suitability for CI tooling | Common (jq, prettier) | Common (terraform, kubernetes, github actions ecosystem) | Common only for internal-use |
| Compatibility with downstream GPL | Yes | Yes (one-way: Apache code into GPLv3 OK) | No |
| What happens if a contributor's employer sues over a patent | No defense | Implicit patent grant becomes a defense; contributor's grant terminates against the litigator | N/A |
| What signal it sends to `ignite-pilot-org` consumers | "Use freely, no obligations beyond attribution" | "Use freely; we explicitly grant patent rights; if you sue over patents we revoke yours" | "Look but don't copy" — conflicts with the cross-org goal |

## Why Apache 2.0 for this repo specifically

1. **Patent posture for CI infrastructure**. CI tooling sits in the build path for every PR; downstream users want assurance that the code can't be retroactively encumbered. Apache 2.0's explicit patent grant + termination clause is the standard mitigation. MIT silently relies on whatever patent rights US copyright law implicitly grants, which is materially weaker.

2. **Industry norm**. The reusable-workflows ecosystem (`actions/*`, `anthropics/claude-code-action`, `actions/setup-python`, `actions/upload-artifact`) is Apache 2.0 or MIT. Apache 2.0 keeps `ai-dev-pr-review` license-compatible with everything it composes and everything that might compose with it.

3. **NOTICE file is a lightweight obligation**. The only practical cost vs MIT is maintaining a `NOTICE` file in the repo. That's a one-time setup.

4. **Cross-org reuse goal**. AT-1210 explicitly targets `ignite-pilot-org` (a separate GitHub org) plus potentially external open-source use. A proprietary license blocks this entirely. MIT and Apache 2.0 both unblock; Apache 2.0 gives the cleaner posture for the patent question.

5. **Internal precedent**. AGENTS.md "Non-Negotiable #5" enforces PR workflow over direct main pushes — operational hygiene the workflows here help enforce. There is no business sensitivity in CI orchestration code that argues against open-sourcing it under Apache 2.0.

## Why not MIT

- MIT works fine functionally and is shorter. The deciding factor is the patent grant: this code interacts with multiple third-party AI SDKs (Anthropic, OpenAI, Google) and verifies GitHub Actions SHAs. Any of those areas could attract patent claims. Apache 2.0 is +20 lines of text for explicit downside protection.

## Why not proprietary

- Defeats the cross-org goal. A `LicenseRef-Ignite` proprietary license technically works for "public visibility" (so cross-org `workflow_call` can resolve the file) but legally prevents `ignite-pilot-org` (separate legal entity) from using it without a written agreement. Same problem applies to any external community use.

## Action items if Apache 2.0 is approved

1. Add `LICENSE` (Apache 2.0 standard text — `https://www.apache.org/licenses/LICENSE-2.0.txt`).
2. Add `NOTICE`:
   ```
   ai-dev-pr-review
   Copyright 2026 Ignite Corp.
   ```
3. Add SPDX header to each script (optional but recommended):
   ```python
   # SPDX-License-Identifier: Apache-2.0
   ```
4. Add `license: Apache-2.0` to any future `pyproject.toml`.

## Open question for user

If your legal counsel prefers MIT (simpler, no NOTICE obligation, no patent termination clause that could surprise a contributor), it is a fine fallback. The recommendation defaults to Apache 2.0 strictly on patent posture; if Ignite has standardized on MIT for prior public repos, follow that precedent for consistency.
