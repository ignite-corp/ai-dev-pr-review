# ai-dev-pr-review

[English](README.md) | [한국어](README.ko.md)

다중 LLM 풀 리퀘스트 리뷰(Claude + Codex + Gemini, 병렬 또는 순차)를 위한 재사용 가능한 GitHub Actions 워크플로우입니다. 인라인 코멘트 게시, 이전 라운드 대비 중복 제거, 규칙 기반 통합 판정을 제공합니다.

> **Code Factory 코드네임: LENS.** 이 레포는 Code Factory 백서 §4.1 ③ PR 시스템 모듈의 실제 구현체입니다. 여러 LLM 리뷰어가 같은 코드를 서로 다른 *렌즈*(리뷰 차원)로 보고, 취합기가 이를 하나의 판정으로 결합합니다.

## 이 레포가 제공하는 것

`.github/workflows/` 아래 4개의 `workflow_call` 워크플로우:

| 워크플로우 | 목적 |
|---|---|
| `base-ai-review-orchestrator.yml` | 최상위 진입점. prepare + 리뷰어 + aggregate를 생성. 소비자(consumer) thin 트리거가 이것을 호출. |
| `base-ai-review-prepare.yml` | PR 크기 측정, diff + 컨텍스트 추출, 이전 리뷰 스레드와 검증된 action SHA 핀 수집, `review-context` 아티팩트로 업로드. |
| `base-ai-review-single.yml` | 리뷰어 1개(Claude / Codex / Gemini) 실행, `review-<reviewer>.json` 작성, 인라인 코멘트 게시. |
| `base-ai-review-aggregate.yml` | 모든 리뷰어 출력을 로드, 심각도 규칙 적용, PR에 통합 판정 게시. |

`.github/scripts/` 아래 헬퍼 (Python 3.14, 그리고 bash 1개 + jq 1개):
`aggregate_reviews.py`, `extract_claude_review.py`, `extract_codex_json.py`, `fetch_review_context.py`, `github_pr_support.py`, `post_inline_comments.py`, `review_gemini.py`, `verify_action_shas.py`, `collect_review_threads.sh`, `threads.jq`, `review_prompt.md`, `requirements.txt`, `.python-version`.

`.github/schemas/review-schema.json` 스키마 (리뷰어별 출력 계약).

## 소비자 레포에 필요한 시크릿

`OPENAI_API_KEY`와 `GOOGLE_AI_API_KEY`는 필수입니다. Claude 리뷰어는 `CLAUDE_CODE_OAUTH_TOKEN` 또는 `ANTHROPIC_API_KEY` 중 **하나 이상**이 필요합니다(아래 [Claude 리뷰어 인증](#claude-리뷰어-인증-oauth-토큰-vs-api-키) 참고). `secrets: inherit`는 조직 간(cross-org)에서 동작하지 않으므로 — 동일 조직 여부와 무관하게 명시적으로 전달하세요.

| 시크릿 | 필수 | 사용처 | 비고 |
|---|---|---|---|
| `OPENAI_API_KEY` | 예 | Codex 리뷰어 | 조직 또는 레포 시크릿. Codex CLI가 stdin으로 로그인. |
| `GOOGLE_AI_API_KEY` | 예 | Gemini 리뷰어 | 조직 또는 레포 시크릿. |
| `CLAUDE_CODE_OAUTH_TOKEN` | 둘 중 하나 | Claude 리뷰어 | Claude Pro/Max 구독 OAuth 토큰(`claude setup-token`). |
| `ANTHROPIC_API_KEY` | 둘 중 하나 | Claude 리뷰어 | 일반 `sk-ant-` API 키. |
| `REVIEWER_APP_PRIVATE_KEY` | 아니오 | 취합 승인 | 전용 리뷰어 GitHub App의 프라이빗 키. `REVIEWER_APP_ID` 변수와 함께 설정하면 `approve` 판정 시 실제 APPROVED 리뷰가 게시됨. 미설정 시(또는 변수 미설정 시) `approve`는 일반 코멘트로 게시됨 — 현재 기본 동작. |

### Claude 리뷰어 인증: OAuth 토큰 vs API 키

Claude 리뷰어는 `anthropics/claude-code-action`을 통해 실행되며, 이 액션은 두 가지 자격증명을 받고 그중 **최소 하나**(또는 workload identity)를 요구합니다:

| | `CLAUDE_CODE_OAUTH_TOKEN` | `ANTHROPIC_API_KEY` |
|---|---|---|
| 정체 | Claude Pro/Max **구독**용 OAuth 토큰(`claude setup-token`) | 일반 **API 키**(`sk-ant-...`) |
| 과금 | 구독에 청구 | Anthropic API 계정에 청구(사용량 기반) |
| 발급 | 로컬에서 `claude setup-token` | Anthropic Console |

**둘 다 설정된 경우의 우선순위:** CLI는 `ANTHROPIC_API_KEY`에 더 높은 우선순위를 부여하므로, 둘 다 전달하면 OAuth 토큰이 있어도 API에 과금됩니다. 이를 피하기 위해 이 레포의 워크플로우는 이제 `CLAUDE_CODE_OAUTH_TOKEN`이 설정된 경우 **OAuth 토큰만** 전달하여 구독이 사용되도록 하며, `ANTHROPIC_API_KEY`는 OAuth 토큰이 없을 때만 연결되는 폴백입니다(docs: code.claude.com/docs/en/authentication). 인증·과금하려는 **하나만** 제공하세요. (`consumer-health` 체크는 네 개를 모두 점검해 잘못 구성된 레포를 조기에 드러내지만, 이는 헬스 신호일 뿐 Claude 시크릿 두 개를 모두 설정해야 한다는 하드 요구사항은 아닙니다.)

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
    uses: ignite-corp/ai-dev-pr-review/.github/workflows/base-ai-review-orchestrator.yml@v1
    with:
      pr_number: ${{ inputs.pr_number || '' }}
      code-review-system-prompt-path: .github/prompts/code-review-system.md
      code-review-checklist-path: .github/prompts/code-review-checklist.md
    secrets:
      OPENAI_API_KEY: ${{ secrets.OPENAI_API_KEY }}
      GOOGLE_AI_API_KEY: ${{ secrets.GOOGLE_AI_API_KEY }}
      CLAUDE_CODE_OAUTH_TOKEN: ${{ secrets.CLAUDE_CODE_OAUTH_TOKEN }}
      ANTHROPIC_API_KEY: ${{ secrets.ANTHROPIC_API_KEY }}
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

### 옵션 2 — 특정 버전 핀 (`@v1.0.15`)

```yaml
uses: ignite-corp/ai-dev-pr-review/.github/workflows/base-ai-review-orchestrator.yml@v1.0.15
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
uses: ignite-corp/ai-dev-pr-review/.github/workflows/base-ai-review-orchestrator.yml@v1.0.15
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
| `ALLOW_AUTO_APPROVE` | `false` | **모든** 정식 리뷰 이벤트를 제어하는 킬스위치. `false`일 때 "approve"와 "request_changes" 판정 모두 일반 코멘트로 게시됨(`gh pr review --approve` 및 `--request-changes` 미제출). `true`로 전환하면 실제 `gh pr review --approve` 및 `--request-changes`(Changes Requested) 이벤트 활성화. |
| `REVIEWER_APP_ID` | _(미설정)_ | 전용 리뷰어 GitHub App의 App ID. 설정 시(그리고 `REVIEWER_APP_PRIVATE_KEY` 시크릿 구성 시) 취합 단계가 App 설치 토큰을 발급해 `approve` 판정에 실제 APPROVED 리뷰를 제출함. `github-actions[bot]`은 PR을 승인할 수 없으므로, 미설정 시 `approve` 판정은 일반 코멘트로 폴백됨 — 현재 기본 동작. 선택 사항이며 완전한 하위 호환. |

## 동시성(Concurrency)과 재푸시(re-push) 동작

orchestrator는 `concurrency: { group: ai-review-<pr-number>, cancel-in-progress: true }`를 설정하므로, 각 PR은 한 번에 최대 1개의 활성 리뷰 런만 갖습니다. 그룹 키는 PR 번호입니다(`workflow_dispatch`에서는 `inputs.pr_number`, 없으면 `github.run_id`로 폴백).

- **리뷰 도중 새 push:** 모든 push는 `pull_request: synchronize`를 발생시켜 새 런을 시작하고 같은 PR의 진행 중 런을 취소합니다. 새 런은 최신 diff로 `prepare`부터 재시작합니다. 런은 누적되지 않으며 — PR은 단일 활성 런으로 수렴합니다.
- **서로 다른 PR:** 그룹 키가 다르므로 독립적으로 실행되고 서로 취소하지 않습니다.
- **`sequential` 트레이드오프:** 리뷰어가 차례대로 실행되므로(`Claude -> Codex -> Gemini`) 런이 `parallel`보다 실제 소요 시간이 길고, 따라서 재푸시가 런 도중에 끼어들 확률이 높습니다. 취소되면 이미 완료된 단계(예: 끝난 Claude 리뷰)까지 버려지고 새 런이 체인을 처음부터 다시 실행합니다. `parallel`은 빠른 연속 푸시에서 낭비되는 작업이 더 적습니다.
- **수동 `workflow_dispatch`:** 그룹 키가 PR과 일치하도록 `pr_number`를 전달하세요. 없으면 키가 런마다 고유한 `github.run_id`로 폴백되어, 동시 수동 런이 중복 제거되지 않습니다.

## 조직 간(cross-org) 사용

GitHub은 조직 간에 `secrets: inherit`를 전파하지 **않습니다**. `ignite-pilot-org`(또는 다른 조직) 소비자의 경우:

1. 조직 관리자: `OPENAI_API_KEY`, `GOOGLE_AI_API_KEY`, `CLAUDE_CODE_OAUTH_TOKEN`, `ANTHROPIC_API_KEY`를 조직 수준 시크릿으로 구성하고 소비자 레포에 접근 권한 부여.
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

`github-actions`용 Dependabot은 일반 action ref뿐 아니라 재사용 워크플로우 ref(예: `uses: ignite-corp/ai-dev-pr-review/.github/workflows/base-ai-review-orchestrator.yml@v1.0.15`)도 다룹니다. 더 빠른 채택은 `interval`을 `daily`로, PR 노이즈를 줄이려면 `monthly`로 조정하세요.

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

래퍼 버전 정렬: 이 레포와 파일럿 래퍼 `ignite-pilot-org/ai-dev-pr-review-wrapper`는 릴리스 버전의 MAJOR.MINOR를 lockstep으로 유지하며, PATCH는 레포별로 독립적으로 올립니다. 현재 페어링: base `v1.2.0` ↔ wrapper `v1.2.0`. 한쪽 레포가 MINOR(또는 MAJOR)를 올리면, 다른 쪽도 매칭되는 정렬(alignment) 릴리스를 냅니다 — 변경 사항이 없으면 내용이 동일한 릴리스입니다. 두 레포 모두 릴리스 발행 시 `move-major-tag.yml`로 `v1` 메이저 태그를 자동 이동하므로, `@v1` 소비자는 별도 조치가 필요 없습니다.

## 소비자 레포별 프롬프트 재정의

레포별 시스템 프롬프트와 체크리스트는 여기가 아니라 소비자 레포에 있습니다. 재사용 워크플로우가 `code-review-system-prompt-path` / `code-review-checklist-path`로 읽어 `context.md`로 합치고, 세 리뷰어(Claude / Codex / Gemini)가 이를 공통 지침으로 읽습니다. 커스터마이즈하려면:

1. 이 레포의 `examples/prompts/` 스타터 템플릿을 소비자의 `.github/prompts/`로 복사:
   - `code-review-system.md` — 기본 **리뷰 성향(disposition)**(이슈마다 구체적 `suggestion` 동반, 반대를 위한 반대 금지, 수정이 어려워도 모든 심각도의 실질적 결함은 보고) + 채워 넣을 인라인 `<...>` 플레이스홀더(프로젝트 정체성, 아키텍처 레이어).
   - `code-review-checklist.md` — 기본 코드 품질 / 보안 / 스펙 준수 체크리스트.
2. 레포별 규칙(아키텍처 관례, 네이밍 금기, 보안 기대사항)으로 편집. 리뷰 성향도 여기서 조정 — 세 리뷰어 모두 이 파일들을 공통 지침으로 읽습니다.
3. 기본값이 아닌 경로를 쓸 때만 thin 트리거에서 참조:
   ```yaml
   with:
     code-review-system-prompt-path: my/custom/path/system.md
   ```
4. 프롬프트를 소비자의 **BASE** 브랜치에 커밋. `prepare` 워크플로우는 프롬프트 인젝션 방지를 위해 항상 PR head가 아닌 base 브랜치에서 읽으므로 — 변경은 머지 후 다음 PR부터 적용됩니다.

### system 프롬프트 vs checklist — 각각 어떻게 작성하나

두 파일은 이어붙인 `context.md` 안에서 역할이 다릅니다:

| | `code-review-system.md` | `code-review-checklist.md` |
|---|---|---|
| 역할 | **어떻게 판단하나** — 페르소나·정책·심각도 기준 | **무엇을 점검하나** — 통과/실패 항목 나열 |
| 형식 | 산문 + 표 | `- [ ]` 항목 |
| 담는 것 | 리뷰 disposition, 3관점, 심각도 의미, 출력 계약, SHA-pin / 중복회피 / Dependabot 규칙, repo 아키텍처·보안 기대치 | 코드품질 / 보안 / 스펙준수의 구체적·이진 점검 항목 |

- **system.md** — *리뷰어가 어떻게 사고·결정할지*를 작성. org 표준 섹션은 유지하고, `<...>` 2줄(프로젝트 정체성, 아키텍처 레이어) + repo별 아키텍처/네이밍/보안 기대치만 커스터마이즈. 심각도 의미와 disposition은 여기에.
- **checklist.md** — 짧고 스캔 가능한 **이진** 항목만("함수 80줄 초과 금지", "파라미터라이즈드 쿼리만", "JWT 검증 존재"). 판단/철학은 넣지 말 것 — 그건 system.md 소관. disposition은 재서술하지 말고 한 줄로 참조.
- **중복 금지.** 정책 / disposition / 심각도 → system.md에만. 열거 점검 → checklist.md에만. 같은 규칙을 양쪽에 쓰면 어긋나며 모순이 생깁니다.

## 심각도 아이콘

통합 판정 코멘트와 인라인 리뷰어 코멘트는 단일 문자 ASCII 심각도 표시를 사용합니다:

| 심각도 | 아이콘 |
|---|---|
| critical | `!` |
| major | `+` |
| minor | `-` |
| suggestion | `?` |

이는 공개 레포를 위한 의도적인 ASCII 전용 선택입니다. 더 풍부한 아이콘(이모지)을 원하는 소비자는 포크하거나 `SEVERITY_ICONS`를 구성 가능하게 만드는 PR을 열 수 있습니다.

## PR 응대 스킬 (Claude Code)

리뷰어는 findings를 게시하지만, 실제 PR을 리뷰→수정→머지 사이클로 몰아가는 건 개발자 몫입니다. 정본 `pr-response-cycle` Claude Code 스킬이 여기 [`.claude/skills/pr-response-cycle/`](.claude/skills/pr-response-cycle/SKILL.md)에 있습니다. 10단계 체크리스트로 PR을 구동합니다: 리뷰 스레드 일괄 분류(Fixed / Deferred / Won't fix / Duplicate / Outdated), 증거 기반 답글, 타임라인 3종(스레드 + 이슈 코멘트 + 리뷰 바디) 관리, fixup-rebase, 머지 상태(CLEAN / BLOCKED / BEHIND / DIRTY) 판단, 정책 허용 시 merge commit(절대 squash 아님)으로 머지.

**리뷰어 설정 repo에서 쓰려면** 스킬 폴더를 아래 중 한 곳에 복사하세요:

```bash
# repo 단위 (그 repo 작업자 전원 사용 가능)
cp -R .claude/skills/pr-response-cycle <consumer-repo>/.claude/skills/

# 또는 개발자 단위 (본인은 모든 곳에서 사용)
cp -R .claude/skills/pr-response-cycle ~/.claude/skills/
```

이후 Claude Code에서 `/pr-response-cycle`을 호출하거나, PR 번호와 함께 "PR 리뷰 처리" / "process the review"라고 말하면 됩니다. repo의 `~/.claude/projects/<cwd>/memory/` 프로젝트 정책이 스킬 기본값과 충돌하면 정책이 우선합니다. 이 사본을 source of truth로 유지하고, 변경 시 다시 복사하세요.

### 조직 전체 자동 업데이트 (관리자용)

수동 복사 대신, 이 레포는 Claude Code **플러그인 마켓플레이스**를 겸하므로 소비자 레포에 커밋된 `.claude/settings.json`([`examples/consumer-claude-settings.json`](examples/consumer-claude-settings.json))으로 스킬을 참조 설치할 수 있습니다. 다만 서드파티 마켓플레이스 auto-update는 기본 OFF라 사용자마다 1회 토글이 필요합니다. 조직 관리자는 **enterprise-managed settings**(조직 배포 `managed-settings.json`)로 이 토글을 없애고, fleet 전체에 auto-update + 플러그인 강제 enable을 적용할 수 있습니다.

배포 정본 파일은 [`examples/managed-settings.json`](examples/managed-settings.json)입니다 — MDM/구성관리 스크립트에서 복붙 대신 이 파일을 직접 가져다 쓰세요. 전체 블록:

```json
{
  "extraKnownMarketplaces": {
    "ai-dev-pr-review": {
      "source": { "source": "github", "repo": "ignite-corp/ai-dev-pr-review" },
      "autoUpdate": true
    }
  },
  "enabledPlugins": { "pr-response-cycle@ai-dev-pr-review": true },
  "strictKnownMarketplaces": [
    { "source": "github", "repo": "ignite-corp/ai-dev-pr-review" }
  ]
}
```

각 키의 역할:

- `autoUpdate: true` — fleet 전체에 상류 업데이트를 사용자 토글 없이 자동 반영. managed settings의 `extraKnownMarketplaces.<name>` 항목에서만 유효하며, 프로젝트 스코프 `.claude/settings.json`에서는 **조용히 무시**되므로 거기에 넣지 마세요.
- `enabledPlugins` — 조직 전체 강제 enable. 자동 설치는 아님: 최초 설치는 소비자 레포에 커밋된 project `.claude/settings.json`이 folder-trust 시점에 처리합니다.
- `strictKnownMarketplaces` — 사용자가 추가할 수 있는 마켓플레이스 허용목록. **배포 전에 조직에서 이미 쓰는 마켓플레이스를 전수 조사해 목록에 모두 추가하세요** — 목록에 없는 마켓플레이스는 배포 즉시 전 사용자에게 추가 불가가 됩니다.

**배포 방식:**

- **Server-managed (권장)** — claude.ai 관리자 콘솔에서 블록을 푸시하면 각 머신 파일시스템을 건드리지 않고 fleet에 배포됩니다. Claude Code Teams v2.1.38+ 또는 Enterprise v2.1.30+ 필요.
- **파일 기반** — 아래 OS별 시스템 경로에 `managed-settings.json` 작성.
- **MDM** — 장치 관리로 파일 전달(macOS 구성 프로파일/plist, Windows HKLM 레지스트리). Anthropic이 완성된 MDM 프로파일을 제공하지 않으므로 payload는 직접 작성해야 합니다.

| OS | 경로 |
|---|---|
| macOS | `/Library/Application Support/ClaudeCode/managed-settings.json` |
| Linux / WSL | `/etc/claude-code/managed-settings.json` |
| Windows | `C:\Program Files\ClaudeCode\managed-settings.json` |

**이걸로도 제거할 수 없는 잔여 수동 스텝:**

1. repo/머신당 folder-trust 프롬프트 1회(인터랙티브 사용 시) — 어떤 managed setting으로도 사전승인 불가(`-p` 비인터랙티브 모드만 스킵).
2. 최초 플러그인 설치는 커밋된 project `.claude/settings.json`이 folder-trust 시 처리 — managed settings가 하지 않음.
3. 각 사용자의 조직 인증(로그인).

**보안 노트:** 폴더를 신뢰하면 그 repo의 settings/hooks/MCP 서버/skills가 자동 로드·실행됩니다(코드 실행 표면). 신뢰 프롬프트를 사람 게이트로 유지하고, `strictKnownMarketplaces`(출처 허용목록)와 `permissions.deny`를 계층 방어로 두세요.

배포 추적: AT-1476.

## 기여

이 레포에 이슈와 PR을 여세요. 공개 레포의 CI / 테스트는 v1.0.0 범위 밖입니다. `CONTRIBUTING.md`(예정)가 생기면 참고하세요.

## 라이선스

`LICENSE` 참고(결정 대기 중 — `LICENSE_RECOMMENDATION.md` 검토).
