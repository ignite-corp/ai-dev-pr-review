# ai-dev-pr-review

[English](README.md) | [한국어](README.ko.md)

다중 LLM 풀 리퀘스트 리뷰(Claude + Codex + Gemini, 병렬 또는 순차)를 위한 재사용 가능한 GitHub Actions 워크플로우입니다. 인라인 코멘트 게시, 이전 라운드 대비 중복 제거, 규칙 기반 통합 판정을 제공합니다.

## 이 레포가 제공하는 것

`.github/workflows/` 아래 4개의 `workflow_call` 워크플로우:

| 워크플로우 | 목적 |
|---|---|
| `base-ai-review-orchestrator.yml` | 최상위 진입점. prepare + 리뷰어 + aggregate를 생성. 소비자(consumer) thin 트리거가 이것을 호출. |
| `base-ai-review-prepare.yml` | PR 크기 측정, diff + 컨텍스트 추출, 이전 리뷰 스레드와 검증된 action SHA 핀 수집, `review-context` 아티팩트로 업로드. |
| `base-ai-review-single.yml` | 리뷰어 1개(Claude / Codex / Gemini) 실행, `review-<reviewer>.json` 작성, 인라인 코멘트 게시. |
| `base-ai-review-aggregate.yml` | 모든 리뷰어 출력을 로드, 심각도 규칙 적용, PR에 통합 판정 게시. |

`.github/scripts/` 아래 헬퍼 (Python 3.14, 그리고 bash 1개 + jq 1개):
`aggregate_reviews.py`, `extract_claude_review.py`, `fetch_review_context.py`, `github_pr_support.py`, `post_inline_comments.py`, `review_gemini.py`, `verify_action_shas.py`, `collect_review_threads.sh`, `threads.jq`, `review_prompt.md`, `requirements.txt`, `.python-version`.

`.github/schemas/review-schema.json` 스키마 (리뷰어별 출력 계약).

## 소비자 레포에 필요한 시크릿

소비자 thin 트리거에서 세 개를 모두 명시적으로 전달하세요. `secrets: inherit`는 조직 간(cross-org)에서 동작하지 않으므로 — 동일 조직 여부와 무관하게 모든 소비자에서 아래의 명시적 형태를 사용하세요.

| 시크릿 | 사용처 | 비고 |
|---|---|---|
| `OPENAI_API_KEY` | Codex 리뷰어 | 조직 또는 레포 시크릿. Codex CLI가 stdin으로 로그인. |
| `GOOGLE_AI_API_KEY` | Gemini 리뷰어 | 조직 또는 레포 시크릿. |
| `CLAUDE_CODE_OAUTH_TOKEN` | Claude 리뷰어 | `anthropics/claude-code-action`의 OAuth 토큰. |

## 최소 소비자 thin 트리거

소비자 레포에 `.github/workflows/ai-review.yml`로 추가하세요:

```yaml
name: AI Code Review
on:
  pull_request:
    branches: [main]
  workflow_dispatch:
    inputs:
      pr_number:
        description: "PR number to review"
        required: true
        type: string

jobs:
  review:
    uses: ignite-corp/ai-dev-pr-review/.github/workflows/base-ai-review-orchestrator.yml@v1.0.5
    with:
      pr_number: ${{ inputs.pr_number || '' }}
      code-review-system-prompt-path: .github/prompts/code-review-system.md
      code-review-checklist-path: .github/prompts/code-review-checklist.md
    secrets:
      OPENAI_API_KEY: ${{ secrets.OPENAI_API_KEY }}
      GOOGLE_AI_API_KEY: ${{ secrets.GOOGLE_AI_API_KEY }}
      CLAUDE_CODE_OAUTH_TOKEN: ${{ secrets.CLAUDE_CODE_OAUTH_TOKEN }}
```

조직 간 변형을 포함한 전체 파일은 `examples/consumer-thin-trigger.yml`를 참고하세요.

## 핀(pinning) 전략

소비자는 재사용 워크플로우 ref를 두 가지 방식으로 핀할 수 있습니다. 두 방식 모두 지원되며 동시에 태깅됩니다.

### 옵션 1 — 메이저 플로팅 태그 (`@v1`) — 기본 권장

```yaml
uses: ignite-corp/ai-dev-pr-review/.github/workflows/base-ai-review-orchestrator.yml@v1
```

`v1` 태그는 최신 `v1.x.y` 릴리스를 자동으로 추적합니다. 이 레포가 `v1.0.8`, `v1.0.9` 등을 발행하면, `move-major-tag.yml` 워크플로우가 `v1` 태그를 새 커밋으로 강제 이동시킵니다. 모든 `@v1` 소비자는 다음 `ai-review` 런에서 변경을 받아옵니다 — 소비자별 PR이 필요 없습니다.

**사용 시점**: 이 업스트림을 신뢰하고, 릴리스별 관리 작업 없이 수정사항(예: AT-1264의 codex stdout 폴백)을 받고 싶을 때.

**비고**: 호환성 깨지는 변경은 새 `v2` 태그와 함께 `v2`로 배포됩니다. `@v1` 소비자는 v2로 자동 승급되지 **않습니다** — 명시적인 호출자(caller) 업데이트가 필요합니다. 따라서 `@v1`은 v1 메이저 라인 내에서 안전합니다.

### 옵션 2 — 특정 버전 핀 (`@v1.0.7`)

```yaml
uses: ignite-corp/ai-dev-pr-review/.github/workflows/base-ai-review-orchestrator.yml@v1.0.7
```

특정 불변(immutable) 커밋에 핀합니다. 각 신규 릴리스는 Dependabot bump PR로 나타납니다(`package-ecosystem: github-actions` 활성화 시).

**사용 시점**: 릴리스별 명시적 리뷰/승인(감사 추적)을 원할 때, 어떤 이유로든 신규 마이너 채택을 미루고 싶을 때, 또는 불변 공급망 ref가 요구되는 규제 환경일 때.

### 비교

| 항목 | `@v1` (가변 메이저) | `@v1.0.X` (특정) |
|---|---|---|
| 신규 릴리스 채택 | 자동, 다음 런 | Dependabot PR 통한 수동 |
| 릴리스별 PR 오버헤드 | 없음 | 소비자당 릴리스당 1 PR |
| 감사 추적 | 거침(메이저 라인) | 릴리스별 명시적 |
| 호환성 깨짐 안전성 | v1.x.x에 고정(v2로 자동 점프 안 함) | 정확히 고정 |
| 강제 푸시 윈도우 | 있음(릴리스 발행과 다음 호출자 런 사이) | 없음 |

### 둘 사이 전환

소비자를 특정 핀에서 플로팅으로 전환하려면:

```yaml
# Before
uses: ignite-corp/ai-dev-pr-review/.github/workflows/base-ai-review-orchestrator.yml@v1.0.7
# After
uses: ignite-corp/ai-dev-pr-review/.github/workflows/base-ai-review-orchestrator.yml@v1
```

그 반대도 마찬가지입니다. 두 ref 모두 항상 해석됩니다.

## 입력 계약 레퍼런스

모든 워크플로우는 `workflow_call` 입력을 받습니다:

| 입력 | 타입 | 필수 | 기본값 | 비고 |
|---|---|---|---|---|
| `pr_number` | string | false | `""` | `workflow_dispatch`로 트리거될 때만 필수. `pull_request`에서는 orchestrator가 `github.event.pull_request.number`를 읽음. |
| `code-review-system-prompt-path` | string | false | `.github/prompts/code-review-system.md` | 레포별 시스템 프롬프트의 **소비자 레포 내부** 경로. prepare 워크플로우가 프롬프트 인젝션 방지를 위해 PR head가 아닌 PR의 base 브랜치에서 `git show`로 읽음. |
| `code-review-checklist-path` | string | false | `.github/prompts/code-review-checklist.md` | 동일한 경로 의미. |

`single`과 `aggregate` 워크플로우는 추가로 리뷰어 라우팅 입력(`reviewer`, `claude_result`, `codex_result`, `gemini_result`)을 받지만, 소비자가 직접 호출하지 않습니다 — orchestrator가 연결합니다.

## `vars.*`를 통한 런타임 구성

코드 변경 없이 동작을 조정합니다. 레포 또는 조직의 `Settings -> Secrets and variables -> Actions -> Variables`에서 설정하세요.

| 변수 | 기본값 | 영향 |
|---|---|---|
| `PR_SIZE_LIMIT` | `3000` | 추가+삭제 라인이 이 값을 초과하면 리뷰 건너뜀. PR에 코멘트하고 `skip=true` 반환. |
| `REVIEW_MODE` | `parallel` | `parallel`(기본)은 세 리뷰어를 동시 실행. `sequential`은 Claude -> Codex -> Gemini 순서로 실행하고 `early_exit` 시 중단. |
| `CRITICAL_THRESHOLD` | `1` | 사람 PR: `request_changes`를 유발하는 critical 이슈 수. |
| `DEPENDABOT_CRITICAL_THRESHOLD` | `2` | Dependabot PR 전용: 동일 게이트, 상향. |
| `MAJOR_CONSENSUS_OVERLAP` | `0.3` | 두 리뷰어의 major 발견이 같은 이슈로 간주되는 단어 중복 비율(0-1). |
| `DEPENDABOT_MAJOR_CONSENSUS_OVERLAP` | `0.5` | Dependabot 전용. |
| `MAJOR_CONSENSUS_MIN` | `2` | major 이슈에 대해 `request_changes`를 유발하는 합의에 필요한 리뷰어 수. |
| `CLAUDE_MODEL` | `claude-sonnet-4-6` | `claude_args --model`을 통해 `anthropics/claude-code-action`에 전달되는 모델. |
| `CODEX_MODEL` | `gpt-5.5` | `codex exec --model`에 전달되는 모델. |
| `GEMINI_MODEL` | `gemini-2.5-pro` | `google-genai` 클라이언트에 전달되는 모델. |
| `BOT_LOGIN` | `github-actions[bot]` | 이전 봇 코멘트 최소화 및 오래된 리뷰 해제에 사용되는 작성자 로그인. |
| `JACCARD_THRESHOLD` | `0.6` | 중복 제거용 토큰셋 Jaccard 유사도 임계값. 낮을수록 더 공격적으로 중복 제거(더 많은 문자열이 같은 이슈로 합쳐짐), 높을수록 엄격. 동작 트레이드오프는 `0.5`-`0.8`로 조정. |
| `ALLOW_AUTO_APPROVE` | `false` | 킬스위치. `false`일 때 "approve" 판정은 일반 코멘트로 게시됨(실제 승인 미제출). `true`로 전환하면 실제 `gh pr review --approve` 활성화. |

## 동시성(Concurrency)과 재푸시(re-push) 동작

orchestrator는 `concurrency: { group: ai-review-<pr-number>, cancel-in-progress: true }`를 설정하므로, 각 PR은 한 번에 최대 1개의 활성 리뷰 런만 갖습니다. 그룹 키는 PR 번호입니다(`workflow_dispatch`에서는 `inputs.pr_number`, 없으면 `github.run_id`로 폴백).

- **리뷰 도중 새 push:** 모든 push는 `pull_request: synchronize`를 발생시켜 새 런을 시작하고 같은 PR의 진행 중 런을 취소합니다. 새 런은 최신 diff로 `prepare`부터 재시작합니다. 런은 누적되지 않으며 — PR은 단일 활성 런으로 수렴합니다.
- **서로 다른 PR:** 그룹 키가 다르므로 독립적으로 실행되고 서로 취소하지 않습니다.
- **`sequential` 트레이드오프:** 리뷰어가 차례대로 실행되므로(`Claude -> Codex -> Gemini`) 런이 `parallel`보다 실제 소요 시간이 길고, 따라서 재푸시가 런 도중에 끼어들 확률이 높습니다. 취소되면 이미 완료된 단계(예: 끝난 Claude 리뷰)까지 버려지고 새 런이 체인을 처음부터 다시 실행합니다. `parallel`은 빠른 연속 푸시에서 낭비되는 작업이 더 적습니다.
- **수동 `workflow_dispatch`:** 그룹 키가 PR과 일치하도록 `pr_number`를 전달하세요. 없으면 키가 런마다 고유한 `github.run_id`로 폴백되어, 동시 수동 런이 중복 제거되지 않습니다.

## 조직 간(cross-org) 사용

GitHub은 조직 간에 `secrets: inherit`를 전파하지 **않습니다**. `ignite-pilot-org`(또는 다른 조직) 소비자의 경우:

1. 조직 관리자: `OPENAI_API_KEY`, `GOOGLE_AI_API_KEY`, `CLAUDE_CODE_OAUTH_TOKEN`을 조직 수준 시크릿으로 구성하고 소비자 레포에 접근 권한 부여.
2. 소비자 thin 트리거: 위에 표시된 명시적 `secrets:` 매핑 사용. `secrets: inherit`를 사용하지 **마세요**.
3. 조직 관리자: Actions allowlist가 `anthropics/claude-code-action`, `actions/checkout`, `actions/setup-python`, `actions/download-artifact`, `actions/upload-artifact`를 허용하는지 확인. 재사용 워크플로우 자체는 `oven-sh/setup-bun`을 가져오지 않지만 기반 `anthropics/claude-code-action`이 가져올 수 있으니, 소비자 추가 전 해당 action의 요구사항을 확인하세요.

## 릴리스 업데이트 수신 (Dependabot)

이 레포가 새 태그를 발행하면, 각 소비자의 Dependabot이 소비자 측에서 1줄 bump PR을 엽니다. 중앙 전파 인프라나 조직 간 토큰이 필요 없습니다.

### 소비자별 설정 (1회성)

각 소비자 레포에 `.github/dependabot.yml` 추가:

````yaml
version: 2
updates:
  - package-ecosystem: github-actions
    directory: "/"
    schedule:
      interval: weekly
    open-pull-requests-limit: 5
````

`github-actions`용 Dependabot은 일반 action ref뿐 아니라 재사용 워크플로우 ref(예: `uses: ignite-corp/ai-dev-pr-review/.github/workflows/base-ai-review-orchestrator.yml@v1.0.5`)도 다룹니다. 더 빠른 채택은 `interval`을 `daily`로, PR 노이즈를 줄이려면 `monthly`로 조정하세요.

### 중앙 전파 대비 트레이드오프

- **장점**: 공유 시크릿 없음, dependabot[bot]이 각 레포의 최소 권한 토큰 사용, 릴리스별 관리 오버헤드 없음
- **단점**: 즉시가 아님 — bump는 태그 푸시 순간이 아니라 구성된 `interval` 내에 나타남

이전의 push 기반 워크플로우(PR #6)는 되돌려졌고, 소비자는 이제 Dependabot으로 직접 가져옵니다.

## 태그 핀(Tag pinning)

소비자는 불변 태그(예: `@v1.0.0`)에 핀해야 **합니다**. 프로덕션 트리거에서 `@main`을 사용하지 **마세요** — `main`에 대한 강제 푸시나 실험적 커밋이 모든 소비자에게 즉시 전파됩니다.

권장 업그레이드 흐름:

1. GitHub Releases / Dependabot으로 신규 릴리스 주시.
2. 소비자에서 핀을 올리는 PR 열기: `@v1.0.0` -> `@v1.1.0`.
3. 새 핀이 그 PR 자체에 대해 실행되어, 업그레이드의 실제 테스트가 됨.
4. 판정이 깨끗하면 머지.

버전 정책:

- 패치(`v1.0.x`): 버그 수정, 소비자 동작 변화 없음.
- 마이너(`v1.x.0`): 새 선택적 입력, 추가적 리뷰어 기능.
- 메이저(`v2.0.0`): 입력 계약 변경, 스크립트 시그니처 깨짐, 심각도 임계 기본값 변경.

## 소비자 레포별 프롬프트 재정의

레포별 시스템 프롬프트와 체크리스트는 여기가 아니라 소비자 레포에 있습니다. 재사용 워크플로우가 `code-review-system-prompt-path` / `code-review-checklist-path`로 읽습니다. 커스터마이즈하려면:

1. 소비자 레포에서 `.github/prompts/code-review-system.md`를 레포별 규칙(아키텍처 관례, 네이밍 금기, 보안 기대사항)으로 생성/편집.
2. 기본값이 아닌 경로를 쓴다면 thin 트리거에서 참조:
   ```yaml
   with:
     code-review-system-prompt-path: my/custom/path/system.md
   ```
3. 프롬프트를 소비자의 **BASE** 브랜치에 커밋. `prepare` 워크플로우는 프롬프트 인젝션 방지를 위해 항상 PR head가 아닌 base 브랜치에서 읽습니다.

## 심각도 아이콘

통합 판정 코멘트와 인라인 리뷰어 코멘트는 단일 문자 ASCII 심각도 표시를 사용합니다:

| 심각도 | 아이콘 |
|---|---|
| critical | `!` |
| major | `+` |
| minor | `-` |
| suggestion | `?` |

이는 공개 레포를 위한 의도적인 ASCII 전용 선택입니다. 더 풍부한 아이콘(이모지)을 원하는 소비자는 포크하거나 `SEVERITY_ICONS`를 구성 가능하게 만드는 PR을 열 수 있습니다.

## 기여

이 레포에 이슈와 PR을 여세요. 공개 레포의 CI / 테스트는 v1.0.0 범위 밖입니다. `CONTRIBUTING.md`(예정)가 생기면 참고하세요.

## 라이선스

`LICENSE` 참고(결정 대기 중 — `LICENSE_RECOMMENDATION.md` 검토).
