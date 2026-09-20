# 로컬 리뷰 드라이버 — 설계 기록

버려지는 것은 `review_pr_local.py` · `review_claude_local.py` · `review_codex_local.py` 와 그 테스트입니다.
남는 것은 그 코드가 열한 라운드에 걸쳐 도달한 **설계**이고, 오늘 그 설계는 닫히게 될 리뷰 스레드와
답글에만 흩어져 있습니다. 이 문서가 그것을 한자리에 모읍니다.

읽은 범위(전부, 표본 아님):

| PR | 이슈 코멘트 | 리뷰(본문) | 리뷰 스레드 | 스레드 내 코멘트 |
|---|---|---|---|---|
| [#172](https://github.com/ignite-corp/ai-dev-pr-review/pull/172) (open, 폐기 예정) | 24/24 (17건 minimized) | 48/48 (**전부 본문 없음**) | 36/36 (전부 resolved, 18건 outdated) | 72/72 |
| [#170](https://github.com/ignite-corp/ai-dev-pr-review/pull/170) (open, 폐기 예정) | 22/22 (11건 minimized) | 59/59 (**전부 본문 없음**) | 45/45 (전부 resolved, 39건 outdated) | 90/90 |
| [#171](https://github.com/ignite-corp/ai-dev-pr-review/pull/171) (**merged**, 남음) | 6/6 (4건 minimized) | 25/25 (**전부 본문 없음**) | 20/20 (전부 resolved, 10건 outdated) | 40/40 |

**두 번째 종류의 자료가 하나 더 있습니다.** 폐기 전에 드라이버를 **실제로 한 번 돌렸고**(PR 170 에 대고,
판정까지 게시됨), 그 실행이 §1~§4 와 성질이 달라 **§5 로 따로 섰습니다.** 열한 라운드가 코드를 읽어
찾은 것과, 코드가 돌 때 실제로 일어난 것은 **겹치지 않았습니다** — 그 사실이 §6 의 무게를 바꿉니다.
원자료는 `/tmp/lens-harvest/` 이고, §5 의 숫자는 요약을 옮긴 것이 아니라 거기서 **다시 측정**했습니다.

리뷰어 지적은 영어이므로 **영어 그대로 인용**합니다. 증거에는 **측정됨**(누군가 실제로 돌려 출력을
붙임) / **논증됨**(추론만)을 매 항목에 표시합니다. 이 구분이 이 문서의 요점입니다 — 이 프로젝트는
**결과는 맞고 기전은 틀린 결론**에 반복해서 데였고, 그 사례가 이 두 PR 안에만 네 건 있습니다(§4-F).

**같은 일이 이 문서를 만드는 동안에도 났습니다. 전부 고치고, 고쳤다는 사실을 각 자리에 남깁니다:**

| | 문서 자신이 틀린 것 | 부류 | 기록된 곳 |
|---|---|---|---|
| 1 | 열지도 않은 출처(Jira)에 대해 **티켓 부재를 단언** — 다섯 중 셋이 틀림 | §4-A | §3-2 |
| 2 | 전달받은 가드 개수 **23/20** 을 다시 세니 **24/17** | §4-F | §5-1 |
| 3 | 라운드 카운터를 **"영원히 1"** 로 적었는데 실측 **13**(참인 명제는 "로컬은 0에서 안 오른다") | §4-F | §5-7 |
| 4 | **§4-A 자신의 수**가 본문 일곱 · 표 아홉 | §4-A | §4-A |
| 5 | §4-B 의 "열셋 중 **아홉**" — 실제로는 열셋 전부가 그 부류 | §4-A | §6-3 |
| 6 | 작동하는 **적대적 지시문을 무력화 없이** 실음 — 같은 파일이 다른 자리에선 무력화하고 있었음 | §3-4 / §1-5 | §1-7 |
| 7 | §6 의 권고가 §3-6 의 **여덟 중 다섯만** 이름함 — 재작성이 둘을 조용히 상속 | §4-A | §6-9 |

**4·5·7 은 #173 의 리뷰어가 찾았고, 6 도 그렇습니다.** 1~3 은 §5 를 준비하며 스스로 찾았습니다.
일곱 건 모두 이 문서가 **서술하는 부류**에 속합니다 — 그래서 지우지 않습니다.

---

## 1. 결정된 것

### 1-1. 리뷰 트리는 매 실행 새로 만드는 일회용 `git worktree` — 운영자 파일을 옮겼다 되돌리지 않는다

**결정.** 체크아웃 옆에 클론을 두고, 리뷰는 **매 실행 새로 만드는 worktree** 안에서 돈다.
PR 의 에이전트 설정(`CLAUDE.md` · `AGENTS.md` · `.mcp.json` · `.claude/` · `.codex/` · `.cursor/`)은
**리뷰어마다** 그냥 **삭제**한다(`strip_agent_config`). 옮겨 두었다 되돌리는 장부(manifest)는 없다.

**이유.** 처음 설계는 설정을 보관 디렉터리로 **옮겼다가 되돌리는** 것이었고, 그 장부 하나가
**세 라운드 연속 데이터 손실 major** 를 냈다:

1. 존재 확인이 `source.exists()` 였는데 그것이 **링크를 따라가** 상대 심링크를 건너뛰었고, 뒤따르는
   `rmtree` 가 **운영자의 심링크를 영구 삭제**했다 — [#170 T0 답글](https://github.com/ignite-corp/ai-dev-pr-review/pull/170#discussion_r3996172476)
2. 장부 기록이 `rename` **뒤**에 있어, 그 사이에서 죽으면 파일이 보관 디렉터리에 있는데 장부엔 없고,
   복원이 건너뛰며 정리가 영구 삭제 — [#170 T12](https://github.com/ignite-corp/ai-dev-pr-review/pull/170#discussion_r4001916797) / [답글](https://github.com/ignite-corp/ai-dev-pr-review/pull/170#discussion_r4002036881)
3. 선행 기록으로 바꾸자 **그 기록 자체가 비원자적**이었다 — `write_text` 는 쓰기 전에 파일을 0바이트로
   자르고, 그 창에서 죽으면 다음 실행이 `json.loads("")` 로 매번 죽는다 —
   [#170 T26](https://github.com/ignite-corp/ai-dev-pr-review/pull/170#discussion_r4042248264) / [T25](https://github.com/ignite-corp/ai-dev-pr-review/pull/170#discussion_r4042210071)

지적된 창을 닫을 때마다 다음 창이 열렸고, 네 번째로 **리뷰어가 되돌리기 경로의 부모 심링크를 바꿔
치는** 탈출이 나왔다([#170 T24](https://github.com/ignite-corp/ai-dev-pr-review/pull/170#discussion_r4042210067),
문자열 봉쇄로 닫혔다고 답했다가 [T34 에서 재제기](https://github.com/ignite-corp/ai-dev-pr-review/pull/170#discussion_r4042367225)
— "the fix in this diff is `_contained` … the thread's scenario was a filesystem one … and that is unchanged here").

그 장부 전체가 지키려던 것은 **운영자가 손으로 리뷰 트리에 둔 파일 하나뿐**이었다. 매 실행 새로 만드는
worktree 에는 그런 파일이 있을 수 없으므로 **지킬 것이 없고 장부가 필요 없다.**
`_write_manifest` · `_read_manifest` · `_manifest_path` · `restore_agent_config` ·
`quarantine_agent_config` · `_contained` 와 보관 디렉터리 상수가 전부 삭제됐고,
**테스트 서른셋도 적응시키지 않고 함께 삭제**했다 — "없어진 기계를 시험하는 테스트를 적응시키면 그
기계가 코드로 다시 스며듭니다"([#170 T29 답글](https://github.com/ignite-corp/ai-dev-pr-review/pull/170#discussion_r4054070751)).

**teardown 이 곧 크래시 복구다.** `remove_review_worktree` 가 `git worktree add` **앞에서 무조건**
돌므로, 크래시한 실행이 남긴 것은 다음 실행이 그냥 버린다. 별도 복구 단계가 없으므로
"복구 단계가 예외 경계 밖에 있다"는 부류의 지적([#170 T29](https://github.com/ignite-corp/ai-dev-pr-review/pull/170#discussion_r4042348012),
[T33](https://github.com/ignite-corp/ai-dev-pr-review/pull/170#discussion_r4042367224),
[T28](https://github.com/ignite-corp/ai-dev-pr-review/pull/170#discussion_r4042248272))도 **사라진다.**

**증거.**
- **측정됨** — 재작성 **뒤에** 실제 worktree 와 실제 `claude` CLI 로 위협을 다시 돌렸다: 훅 미실행,
  `CLAUDE.md` 모델 미도달, 소스 생존([#170 T29 답글](https://github.com/ignite-corp/ai-dev-pr-review/pull/170#discussion_r4054070751)).
- **측정됨** — 손실 사례 1~3 각각 BEFORE/AFTER 출력이 답글에 붙어 있다.
- **논증됨** — "worktree 에는 운영자 파일이 있을 수 없다"는 것 자체는 구조 논증이다.

**설계 결정 하나가 코드에 박혀 있다.** 새 코드는 *"목적은 장부보다 오래 남는다. '이제 worktree 를 쓰니
이것은 불필요하다'고 결론 내리는 독자는 측정된(이론이 아닌) 위협을 되살린다"* 를 주석으로 남긴다
([#170 T36 답글](https://github.com/ignite-corp/ai-dev-pr-review/pull/170#discussion_r4054071178)).

---

### 1-2. 위협 자체가 측정됐다 — 그리고 주장은 측정된 만큼만

**결정.** 에이전트 설정을 지우는 이유는 문서 문제가 아니라 **실행**이다.

**증거 — 측정됨.** `claude` 2.1.269 에, 추측 불가능한 codeword 로([#170 본문](https://github.com/ignite-corp/ai-dev-pr-review/pull/170)):

| 프로브 | 결과 |
|---|---|
| `CLAUDE.md` · `.claude/CLAUDE.md` · `AGENTS.md` | 각각 모델에 도달 |
| `--safe-mode` + `CLAUDE.md` | **여전히** 도달 — help 텍스트가 `CLAUDE.md` 를 끈다고 적고 있는데도 |
| `.claude/settings.json` 의 `SessionStart` 커맨드 훅 | **셸 명령이 실행됨**, `-p` 모드, 프롬프트 없이 |
| `claude.md` · `Claude.md` · `AGENTS.MD` | 대소문자 **구분하는** 파일시스템에서도 각각 도달 |

세 번째 행이 "문서로 안내할 문제가 아니다"의 근거이고, 네 번째 행이 **목록을 철자별로 늘리는 대신
대소문자 폴딩으로 비교**하는 근거다([#170 T19](https://github.com/ignite-corp/ai-dev-pr-review/pull/170#discussion_r4002071258) /
[답글](https://github.com/ignite-corp/ai-dev-pr-review/pull/170#discussion_r4042178271) — 지적은 "the default
filesystem on macOS is case-insensitive" 를 근거로 들었으나, 실측 결과 **구분하는 파일시스템에서도
CLI 가 대소문자를 안 가리고 읽었다.** 결론은 같고 기전은 달랐다).
`--safe-mode` 는 **일부러 넘기지 않으며**, 그 측정을 결정 옆에 적어 두어 나중에 "정리"로 되돌아가지 않게 했다.

**주장의 한계도 결정됐다 — 논증됨/미측정.**
- 음성 대조군(같은 적대 파일 + 보호 해제)은 **한 번의 시행에서 평범한 리뷰를 냈다.** 그래서
  **"이것이 없으면 리뷰가 탈취된다"고 주장하지 않는다.** 확립된 것은 더 좁고 충분하다 —
  파일이 읽히고, 출력에 영향을 주며, 그 안의 훅이 명령을 실행한다([#170 본문](https://github.com/ignite-corp/ai-dev-pr-review/pull/170)).
- `.codex/` · `.cursor/` 는 이 기계에 `codex` 가 없어 **프로브하지 못했고**, 보수적으로 넣었다고 목록
  주석이 출처별로 표시한다([#170 T21 답글](https://github.com/ignite-corp/ai-dev-pr-review/pull/170#discussion_r4042178821)).
- **목록의 규칙**: *측정된 것* / *플래그 문구에서 온 것*(`.mcp.json`) / *재 보지 못한 것* 으로 **출처를
  표시하지 않은 이름은 넣지 않는다.** 그래서 `CLAUDE.local.md` 추가 요청은 거절됐다 — 프로브 없이
  더하는 것 자체가 주장이기 때문(같은 답글). **§3 의 알려진 간극이기도 하다.**
- `GEMINI.md` 는 **필요 없다는 것이 확인됐다**: `review_gemini.py` 는 `context.md` · `pr.diff` ·
  `__file__` 기준 `review-schema.json` 셋만 열며 **트리에서 설정을 읽는 표면이 없다.** 에이전트 CLI 가
  아니라 API 클라이언트다. **없다와 빠뜨렸다를 독자가 구분할 수 없었던 것**이 지적의 요점이었고,
  그 사실을 목록 주석에 같은 형식으로 적었다([#170 T41](https://github.com/ignite-corp/ai-dev-pr-review/pull/170#discussion_r4054183768) /
  [답글](https://github.com/ignite-corp/ai-dev-pr-review/pull/170#discussion_r4054214951)). — **측정됨(코드 독해)**

---

### 1-3. 한 번의 실행은 **판정을 집계에 닿게 하거나, 왜 못 했는지를 말한다.** traceback 만 남기는 일은 없다

**결정.** 불변식을 문장이 아니라 **구조와 테스트**로 세운다.
`EXCEPTION_BOUNDARY = ("review_pr", "aggregate")` 가 *실행의 실패가 빠져나가면 안 되는 함수*를
이름하고, 그 두 함수는 `Exception` 을 잡는다. 과잉 포획의 대가는 **치르되 감추지 않는다** —
전체 traceback 이 stderr 로 나가고 종료 코드는 실패로 남는다. `BaseException` 은 **일부러 안 잡는다**
(Ctrl-C 는 실행을 멈춘다, 테스트로 고정).

**왜 문장으로는 안 됐나 — 이 결함이 계속 돌아왔기 때문.** 매번 **보여진 `raise` 하나**를 닫았고
다음 `raise` 가 다시 열었다. **세는 단위는 PR 별이다**: #170 의 마지막 라운드가 자기 안에서 **여덟 번째**로
세고([T44 답글](https://github.com/ignite-corp/ai-dev-pr-review/pull/170#discussion_r4055430927);
네 번째는 [T37 답글](https://github.com/ignite-corp/ai-dev-pr-review/pull/170#discussion_r4054145745)),
#172 는 R6 시점에 이미 **여섯·일곱 번째**라고 적는다
([R6 응답](https://github.com/ignite-corp/ai-dev-pr-review/pull/172#issuecomment-5746306100)) — 그 뒤로도
같은 부류가 더 나왔으므로 일곱은 하한이다. 아래 표는 두 PR 에 걸쳐 **관측된 서로 다른 문**을 모은 것이고,
어느 한 PR 의 카운트와 일대일로 대응하지 않는다:

| # | 문 | 출처 |
|---|---|---|
| 1 | prepare 실패가 `aggregate()` 를 안 부르고 최상위 핸들러로 나감 | [#172 T0](https://github.com/ignite-corp/ai-dev-pr-review/pull/172#discussion_r4001905853) |
| 2 | 그 `try` 가 너무 넓어 **리뷰 실패까지 prepare 실패로** 보고, head 를 지움 | [#170 T18](https://github.com/ignite-corp/ai-dev-pr-review/pull/170#discussion_r4002071251) |
| 3 | 넣은 타임아웃이 `TimeoutExpired` 를 내는데 그건 `DriverError` 가 아님 | [#170 T32](https://github.com/ignite-corp/ai-dev-pr-review/pull/170#discussion_r4042367221) |
| 4 | `clean_artifacts` 의 `OSError`(심링크 `rmtree`) | [#170 T37](https://github.com/ignite-corp/ai-dev-pr-review/pull/170#discussion_r4054097589) |
| 5 | `aggregate()` 스폰의 `OSError` (핸들러는 `TimeoutExpired` 만) | [#170 R9 컷오프](https://github.com/ignite-corp/ai-dev-pr-review/pull/170#issuecomment-5744404135) |
| 6 | `policy_gate` 가 prepare 블록 안에 있어 **head 가 있는데도 없다고** 보고 | [#170 T38](https://github.com/ignite-corp/ai-dev-pr-review/pull/170#discussion_r4054183760) |
| 7 | `resolve_refs` · `size_gate` · **`resolve_bot_login`** · `config.get_int` · `run_dir.mkdir` 이 경계 **밖** | [#170 T44](https://github.com/ignite-corp/ai-dev-pr-review/pull/170#discussion_r4054328644) |
| 8 | 예외 경계가 예외를 잡으면서 **끝난 리뷰어의 결론을 떨어뜨림** | [#170 R9 컷오프](https://github.com/ignite-corp/ai-dev-pr-review/pull/170#issuecomment-5744404135) |

**구조로 선 것 넷:**

1. **원천에서 변환.** `run()` 이 비정상 종료 · `OSError` · `SubprocessError` 를, `gh_json` 이 비-JSON 과
   객체 아닌 출력을 `DriverError` 로 바꾼다. 핸들러를 넓히는 대신 헬퍼를 고쳐 **모든 호출자와, 경계가
   서기 전에 도는 둘까지** 덮는다([#170 T43 답글](https://github.com/ignite-corp/ai-dev-pr-review/pull/170#discussion_r4054318844)).
2. **결론 딕셔너리는 호출자 소유.** `run_reviewers(work, config, conclusions)` 가 채운다. 끝난 리뷰어는
   그 뒤에 무슨 일이 나든 보고된다([#170 R9 응답](https://github.com/ignite-corp/ai-dev-pr-review/pull/170#issuecomment-5744443546)).
3. **빈 `head_sha` 는 오직 "prepare 가 헤드를 정하지 못했다"만 뜻한다.** 가르는 기준을 "얼마나
   진행됐나"에서 "헤드가 정해졌나"로 다시 썼다 — 앞의 틀이 6번 회귀를 허용했다
   ([#170 T38 답글](https://github.com/ignite-corp/ai-dev-pr-review/pull/170#discussion_r4054214377)).
4. **AST 로 강제.** `test_the_exception_boundary_absorbs_everything`(이름된 함수가 `Exception` 보다
   좁게 잡으면 실패) + `test_main_has_nothing_unguarded_between_the_config_and_the_aggregate`
   (§1-4 의 거부 기본값).

**증거.**
- **측정됨** — 1~8 각각 BEFORE/AFTER 재현 출력이 답글에 있다. 7번은 특히:
  `resolve_bot_login` 이 맨 `subprocess.run` 으로 `gh` 를 띄워 `FileNotFoundError` 가
  `DriverError` 도 `ConfigError` 도 아니었고, 실제 traceback 이 붙어 있다.
- **측정됨** — `test_the_stage_list...` 를 거부 기본값으로 바꾸기 전/후에 *막으려던 바로 그 경우*로 돌려
  `1 passed` → `1 failed` 를 확인했다([#170 R10 응답](https://github.com/ignite-corp/ai-dev-pr-review/pull/170#issuecomment-5744547755)).

**여기서 성격이 뒤집힌 순간 하나는 기록할 값이 있다.** 8번에서 예외 경계가 예외를 잡으면서 진행분을
떨어뜨렸고, 그 결과 **"판정이 안 남는다"가 "판정이 거짓을 말한다"로 바뀌었다** — 집계가 그것을
**Approved** 라고 불렀다. 앞은 눈에 보이게 깨지고 뒤는 조용히 틀린다
([#170 R9 응답](https://github.com/ignite-corp/ai-dev-pr-review/pull/170#issuecomment-5744443546)).

---

### 1-4. 열거는 **거부 기본값**이다 — 아는 이름을 허용하지 않는다

**결정.** 열거가 필요한 자리는 `알려진 것 - 허용 목록` 이 아니라 `관측된 것 - 허용된 것 - 이유와 함께
제외된 것` 으로 계산하고, **남으면 실패**한다.

**사례 셋:**

- **단계 목록.** `test_the_stage_list_matches_the_stages_the_code_has` 가 `called & {여섯 이름} -
  FAILABLE_STAGES` 였다. 새 단계는 `called` 에 있고 **두 목록 어디에도 없으므로** 교집합에서 빠져
  통과한다 — **막으려고 만든 바로 그 회귀**(`policy_gate` 가 아무도 열거하지 않은 넷째 단계였다)에
  눈이 없었다. `called - FAILABLE_STAGES - NON_STAGE_CALLS` 로 뒤집었다.
  [#170 R10 컷오프](https://github.com/ignite-corp/ai-dev-pr-review/pull/170#issuecomment-5744507195) /
  [답변](https://github.com/ignite-corp/ai-dev-pr-review/pull/170#issuecomment-5744547755)
  — **측정됨**: 목록에 없는 `brand_new_stage` 를 넣고 돌려 `1 passed`, 뒤집은 뒤 `1 failed`.
- **경계 밖 호출.** `test_main_has_nothing_unguarded_between_the_config_and_the_aggregate` 가
  `main()` 을 `ast.unparse` 로 걸어 계산한다. `node.func.id` 스캔은 `run_dir.mkdir` · `config.get_int`
  같은 **속성 호출을 정확히 놓치므로** 그 방식을 쓰지 않았다
  ([#170 T44 답글](https://github.com/ignite-corp/ai-dev-pr-review/pull/170#discussion_r4055430927)) — **측정됨**(무는지 확인).
- **리뷰어 표.** `SEQUENTIAL_ORDER` · `REVIEWER_SCRIPTS` 가 `REVIEWER_NAMES` 와 어긋나면
  **import 자체를 거부**한다. `assert` 가 아니라 `raise RuntimeError` — `python -O` 가 assert 를
  버리는데 이건 거부 기본값 검사라 버려지면 안 된다
  ([#172 R7 응답](https://github.com/ignite-corp/ai-dev-pr-review/pull/172#issuecomment-5746491861))
  — **측정됨**: 넷째 리뷰어를 넣어 재현, 전에는 `'fourth': 'skipped'` 로 조용히 통과.

**일반화(기록된 대로):** *"허용 목록과 거부 기본값이 한 토큰 차이인데 성질 전체가 다르다. 테스트가
검사 가능한 열거의 모양을 하고 있어서 맞아 보였다."*

같은 이유로, `.git/config` 의 실행 가능 키를 **열거해 unset 하자**는 제안은 **거절됐다** — 그것은
허용 목록의 거울상이고, 이 PR 이 스스로 *"열거는 끝이 없다"* 고 적었다면 답은 더 긴 열거가 아니다
([#172 T31 답글](https://github.com/ignite-corp/ai-dev-pr-review/pull/172#discussion_r4056671234)).

---

### 1-5. 한 질문에는 하나의 검사 — 판정 파일이 쓰였는지를 두 방식으로 묻지 않는다

**결정.** "무언가 판정 파일을 이미 썼는가"는 `verdict_file_written()` **한 곳**에서만 답한다.
`normalize_verdict_file`(레거시 `verdict-openai.json` 승격)과 `accept_direct_write`(직접 쓰기 수용)가
같은 함수를 부른다. `DirectWrite` docstring 은 규칙을 **다시 적지 않고 그 함수를 가리킨다** —
규칙을 두 번 적은 것이 애초에 **세 번째 자리**를 못 보게 한 원인이다.

**이유.** 두 검사가 갈려 있었다: `normalize_verdict_file` 은 `is_file()`, `accept_direct_write` 는
`is_file() and stat().st_size`. 0바이트 파일(= CLI 가 파일만 만들고 죽은, 폴백이 존재하는 바로 그 경우)에서
좋은 레거시 판정이 조용히 버려졌다.
[#172 R10 컷오프](https://github.com/ignite-corp/ai-dev-pr-review/pull/172#issuecomment-5747947711) /
[답변](https://github.com/ignite-corp/ai-dev-pr-review/pull/172#issuecomment-5748944503)

**증거 — 측정됨.** BEFORE: `status: failed / output_unparseable` ↔ AFTER: `Normalized
verdict-openai.json -> review-codex.json … status: ok`. 그리고 **크기 검사가 아니라 tri-state 를
택한 근거가 기록돼 있다**: *"크기 검사는 두 검사를 오늘 일치시키고, tri-state 는 두 번째 의견이 존재할
가능성 자체를 없앤다 — 호출자가 사실을 다시 계산하지 않으니 다르게 계산할 수도 없다"*
([#172 R6 응답](https://github.com/ignite-corp/ai-dev-pr-review/pull/172#issuecomment-5746306100)).

**같은 부류의 자매 사례 — 측정됨.** `isinstance(payload, dict)` 가드가 필요한 자리는 리뷰어가 짚은
**두 shim 이 아니라 셋**이었다. `normalize_verdict_file` 에도 같은 것이 있었고, 지적된 자리만 고쳤으면
하나가 남았다([#172 T10 답글](https://github.com/ignite-corp/ai-dev-pr-review/pull/172#discussion_r4042331742)).

---

### 1-6. 리뷰어 CLI 는 **자기 세션으로 인증한다** — 드라이버는 어떤 API 키도 설정하지 않는다

**이 항목은 문서와 코드가 어긋났고, 틀린 쪽은 문서였다.**

**사실(코드 기준).**
- `review_claude_local.py` 의 CLI 스폰은 `env=` 로 자격을 만들지 않고 **주변 환경을 그대로 물려받는다.**
  그것이 누락이 아니라 **선택**임을 docstring 이 적는다 — 여기서 환경 변수를 열거하면 *이 모듈이 어느
  자격이 유효한지를 정하는 셈*이 되고, 그건 드라이버가 할 판단이 아니다(운영자가 OAuth 인지 API 키인지,
  프록시가 있는지)([#172 T16 답글](https://github.com/ignite-corp/ai-dev-pr-review/pull/172#discussion_r4043394629)).
- `review_codex_local.py` 는 **`~/.codex` 또는 `OPENAI_API_KEY`** 로 인증한다(모듈 주석).
- `review_gemini.py` 는 `GOOGLE_AI_API_KEY` 를 쓴다.
- 드라이버는 **셋 중 아무것도 설정하지 않는다.** `os.environ` 을 그대로 넘기므로 운영자가 export 한
  키가 그대로 리뷰어에 도달한다.

**문서가 적고 있던 것.** `docs/local-review.md` 의 요구사항 표가 `GOOGLE_AI_API_KEY` 를
*"the only API key the driver reads from the environment"* 라고 적었고, `codex` 행은 `codex login` /
`~/.codex` 만 언급했다. 지적:

> An operator reading only the doc would not know an exported `OPENAI_API_KEY` reaches the Codex CLI.

[#172 T35](https://github.com/ignite-corp/ai-dev-pr-review/pull/172#discussion_r4056633045) /
[답글](https://github.com/ignite-corp/ai-dev-pr-review/pull/172#discussion_r4056671422)

**증거 — 측정됨(문서 문자열 전후 확인) + 코드 독해.**
```
BEFORE  'only API key the driver reads' 있음: True    OPENAI_API_KEY 언급: False
AFTER   'only API key the driver reads' 있음: False   OPENAI_API_KEY 언급: True
```

**이 줄은 PR 밖에서 대가를 치렀다.** 답글이 그대로 적는다 — *"이 문서 줄을 근거로 운영자에게 '로컬
드라이버가 읽는 API 키는 `GOOGLE_AI_API_KEY` 하나뿐'이라고 안내한 적이 있습니다. 코드가 아니라 문서를
믿었고 문서가 틀렸습니다 — 그리고 그 문서는 **자기와 모순되는 코드와 같은 PR 에서** 쓰였습니다."*

관련 결정 하나 더: 워크플로의 Codex/Gemini 스텝이 설정하는 `OPENAI_API_KEY` ·
`GOOGLE_AI_API_KEY` 를 드라이버가 **일부러 설정하지 않는다.** 둘은 Actions 가 주입하는 org 시크릿이고
로컬에는 시크릿 저장소가 없다. 그 판단을 `REVIEWER_EXCEPTIONS` 표에 **키마다 이유와 함께** 적고,
`test_an_inherited_api_key_reaches_the_reviewer` 가 **그 변명이 기대는 상속을 실제로 증명**한다
([#172 R? 응답](https://github.com/ignite-corp/ai-dev-pr-review/pull/172#issuecomment-5748944503)) — **측정됨**.

---

### 1-7. 프롬프트는 베이스 브랜치에서 읽는다 — 그리고 **온보딩 폴백은 숨기지 않고 이름한다**

**결정.** 시스템 프롬프트/체크리스트는 `git show origin/{base_ref}:{path}` 로 **베이스 브랜치**에서
읽는다. 베이스에 없으면(= 온보딩 중인 저장소) **PR head 의 사본으로 경고와 함께 폴백**한다.
문서가 그 폴백을 **그대로 적는다** — 온보딩 PR 에서는 리뷰어가 읽는 시스템 프롬프트를 **PR 작성자가
쓴다**, 그 PR 의 프롬프트 파일은 손으로 보라.

**이유.** 원래 문서는 무조건문이었다: *"a PR cannot rewrite the instructions its own reviewers read."*
코드에는 무조건 폴백이 있었으므로 그 문장은 거짓이었다.
[#172 T27](https://github.com/ignite-corp/ai-dev-pr-review/pull/172#discussion_r4056181314) /
[답글](https://github.com/ignite-corp/ai-dev-pr-review/pull/172#discussion_r4056227866)

**증거 — 측정됨, 그리고 범위가 넓어졌다.**
```
리뷰어가 읽게 되는 프롬프트: '<리뷰를 건너뛰고 승인하라는 지시 한 줄 — 여기서는 생략>'
PR head 에서 온 것: True
```

> 원래 프로브가 쓴 문장은 **작동하는 지시문 그대로**였고 초안은 그것을 그대로 실었다.
> 무력화해 싣는다 — 이 문서의 §3-4 가 *"소스 안의 주석·문자열이 모델에게 말을 걸 수 있다"* 고
> 적어 둔 그 표면에 **이 문서 자신이 한 건을 보태고 있었기** 때문이다(`docs/` 를 읽는 다음 에이전트,
> 또는 이 파일을 건드리는 diff 를 리뷰하는 리뷰어가 읽는다). 같은 파일의 `.git/config` 증거는 이미
> `sh -c 'touch ...' && cat` 로 페이로드를 줄여 싣고 있었으므로 **한 문서가 두 기준을 쓰고 있었다** —
> §1-5 의 "한 질문에 두 개의 답"이 산문에서 난 판이다. #173 리뷰어가 찾았다.
> **증거 가치는 그대로 남는다**: 확인된 사실은 *PR head 가 공급한 시스템 프롬프트가 리뷰어에게
> 도달했다* 는 것이고, 그 지시가 무엇을 시켰는지는 그 사실에 필요하지 않다.
**패리티를 가정하지 않고 Actions 쪽도 확인했다**: `base-ai-review-prepare.yml:323-333` 에 **같은 폴백과
같은 경고 문구**가 있다. 그러니 로컬이 더 약한 것이 아니라 **그 무조건 문장이 처음부터 양쪽 다에 대해
틀렸다.** 고친 절은 구멍을 `base-ai-review-prepare.yml` 의 것으로도 이름하고 찾아볼 경고 문구를 인용한다.

> **이 결정에는 §5 가 찾아낸 예외가 붙는다 — 이 항목을 "정해진 것"으로만 읽지 말 것.**
> 폴백의 조건이 *"베이스 브랜치에 그 파일이 없다"* 가 아니라 **`git show` 의 모든 비-0 종료**다
> (`review_pr_local.py:742`, `if shown.returncode == 0:` 의 else 경로). 손상된 객체든 일시적 git 실패든
> 무엇이든, 실패 한 번이면 PR head 의 사본을 읽는다 — 그리고 그때 찍히는 경고는
> *"Base branch '{base_ref}' has no {path}"* 라고 **코드가 확인하지 않은 원인을 단언한다**(§4-B 의 모양).
> **실제 실행에서 codex 리뷰어가 이것을 major 로 찾아냈고, 열한 라운드의 Actions 리뷰는 못 찾았다** —
> [PR 170 인라인 4056951799](https://github.com/ignite-corp/ai-dev-pr-review/pull/170#discussion_r4056951799).
> 즉 §1-7 의 보증은 *"베이스에 파일이 있으면 성립"* 이 아니라 *"베이스에 파일이 있고 `git show` 가
> 성공하면 성립"* 이다. — **측정됨**(§5-4)

---

### 1-8. 산출물 정리는 **체크아웃 뒤**에 — PR 이 자기 판정을 공급할 수 없다

**결정.** `ensure_clone → checkout_head → clean_artifacts → extract_diff`.
그리고 PR 이 그런 이름의 파일을 들고 왔다는 **사실 자체를 경고로 남긴다**(`tracked_artifact_names`,
`git ls-files`).

**이유.** 순서가 `clean → checkout` 이면 `git checkout --force` 가 정리 **뒤에** PR 의
`review-claude.json` 을 트리에 복원하고, shim 이 그것을 자기 출력으로 신뢰한다.

**증거 — 측정됨, 실제 `review-claude.json` 을 커밋한 진짜 로컬 저장소로:**
```
BEFORE  리뷰어 시작 시 review-claude.json 존재: True
          summary: Looks great to me! No issues found.   early_exit: True
        has_early_exit -> True (체인을 끊음)
        집계가 "수행된 리뷰"로 읽음: ok
AFTER   존재: False / has_early_exit -> False / 집계 상태: failed
```
[#172 T26 답글](https://github.com/ignite-corp/ai-dev-pr-review/pull/172#discussion_r4056227665)

**같은 구멍에 문이 둘이었다 — 측정됨.** PR 이 `.review-context/unresolved-threads.json` 을 커밋하면
`collect_review_threads.sh` 실패 시(그 실패는 허용된다) 그것이 이전 리뷰 맥락으로 읽힌다
([#172 T11 답글](https://github.com/ignite-corp/ai-dev-pr-review/pull/172#discussion_r4042331470)).

**구조적 해법은 불가능한 것으로 확인됐다 — 논증됨(코드 독해).** 산출물을 작업 트리 **밖**에 두려면
재사용 스크립트를 고쳐야 한다: `review_gemini.py` 가 cwd 기준으로 쓰고, Claude 프롬프트가 현재
디렉터리 이름을 부르며, 리뷰어가 소스를 읽으려면 cwd 가 체크아웃이어야 한다. **그 재사용이 이 드라이버의
전제다.** 그래서 순서로 닫되 **여기서 순서가 충분한 이유**(두 단계 사이에 아무것도 안 돈다)를 적었다.

**거절한 대안 — 논증됨.** "PR head 가 `RUN_ARTIFACTS` 이름을 추적하면 `DriverError` 로 거절"은
받지 않았다. `RUN_ARTIFACTS` 에 `context.md` 가 있고 저장소가 그 이름을 커밋하는 것은 정당하다.
거절은 **악의 없는 PR 의 리뷰를 악의적인 PR 만큼 쉽게 막는다.**

---

### 1-9. 클론 신원은 **호스트까지** 대조한다

**결정.** 재사용되는 클론의 `origin` 을 읽어 `(host, owner/name)` **둘 다** 맞아야 받아들인다.
호스트 기대값은 `$GH_HOST` 또는 `github.com`.

**경로가 기록할 값이 있다 — 세 판이었다.**
1. 처음엔 검사가 아예 없었다: `--run-dir` 재사용 시 origin 미확인
   ([#172 T9](https://github.com/ignite-corp/ai-dev-pr-review/pull/172#discussion_r4042210460)).
2. 넣은 판이 **경로의 마지막 두 세그먼트만** 비교해서
   `https://evil.example.com/ignite-corp/ai-dev-pr-review.git` 이 통과했다
   ([#170 T31](https://github.com/ignite-corp/ai-dev-pr-review/pull/170#discussion_r4042348030)).
   답글이 그대로 적는다 — *"앞 라운드에 이 검증을 더한 것도, 그것을 승인한 것도 저희였습니다.
   'origin을 읽어 대조한다'는 문장이 참이었고 충분하지 않았습니다."*
3. 고친 판을 #172 로 역이식할 때 **베끼기 전에 그것이 무엇을 비교하는지 먼저 확인했다**
   ([#172 T25 답글](https://github.com/ignite-corp/ai-dev-pr-review/pull/172#discussion_r4056227749)).

**증거 — 측정됨.** https · `.git` 없는 https · scp 꼴 · `ssh://` · `:port` · user:pw · 적대 호스트 ·
같은 호스트 다른 이름으로 매개변수화한 표와 BEFORE/AFTER 출력이 두 PR 답글 양쪽에 있다.

---

### 1-10. 훅은 **매 실행** 무장 해제하고, 재사용 클론의 `.git/config` 는 **지문**으로 본다 — 그리고 그 지문이 증명하는 것을 정확히 적는다

**결정.**
- `disarm_hooks()` 가 `core.hooksPath=/dev/null` 설정 + `.git/hooks` 삭제를 **매 실행**,
  fetch·checkout **앞에서** 한다(클론 시점만이 아니라 — 설정이 되돌려졌을 수 있으므로).
- 재사용 판단은 `.git/config` 의 **sha256 지문**(`run_dir/.lens-clone`, `.git` **안이 아니라 옆**)으로
  한다. 드라이버가 클론 설정을 끝낸 뒤 쓰고 다음 실행 시작에 다시 읽는다. 다르면 **재클론**.

**증명하는 것(그대로 docstring 에):** *"이 드라이버의 이전 실행이 썼고, `.git/config` 가 그 실행이 남긴
것과 바이트 단위로 같다."* `.git` 의 나머지(objects · refs · `info/` · alternates)와 작업 트리에 대해서는
**아무것도 증명하지 않는다.** *"아무도 이 디렉터리를 안 건드렸다"고 적지 않았다 — 검사할 수 없으니까.*

**왜 맨 마커가 아니라 지문인가 — 논증됨.** 설정은 드라이버가 클론을 만든 **뒤에** 심기므로
"우리가 만들었다"는 표식은 그대로 남는다.

**증거 — 측정됨.**
```
BEFORE  planted config key executed: True
        filter.lens.smudge still set: sh -c 'touch ...' && cat
AFTER   planted config key executed: False   (unset)
```
드라이버 **자신의 `checkout_head`** 중에, 운영자 권한으로, `credential.helper` 가 이미 붙은 클론에서
실행됐다. 재클론 비용도 쟀다(손 안 댄 실행은 `reused`, 변조된 경우만 `re-cloned`).
[#172 T31 답글](https://github.com/ignite-corp/ai-dev-pr-review/pull/172#discussion_r4056671234)

훅 실행 자체도 별도로 측정됐다 — 일회용 origin 에 `refs/pull/7/head` 를 만들고 `post-checkout` 을
심어: `PLANTED HOOK RAN as hyukhur` → AFTER `hook fired: False`
([#172 R6 응답](https://github.com/ignite-corp/ai-dev-pr-review/pull/172#issuecomment-5746306100)).

**지적의 기전 하나는 측정 결과 성립하지 않았다 — §4-F 참조.** 리뷰어가 `url.<base>.insteadOf` 로
`git remote get-url origin` 을 속일 수 있다고 적었으나, git 2.43 에서 재작성은 URL 이 **해석될 때**
적용되므로 `remote get-url` 이 **공격자의 URL 을 보고**하고 신원 검사가 잡는다. *"이걸 재지 않았으면
멀쩡히 작동하는 방어를 못 믿고 다시 만들 뻔했습니다."* 결론(설정 키가 전송을 돌릴 수 있다)은
`http.proxy` · `http.<url>.proxy` · `core.gitProxy` 로 **여전히 성립**한다.

---

### 1-11. 리뷰어는 어느 모드에서도 **한 번에 하나**만 돈다 — 실행은 직렬화, 의미는 아니다

**결정(사용자 결정, 2026-09-12).** 셋이 트리 하나를 공유하고 둘이 그 트리에 **쓸 수 있다**
(Codex `--sandbox workspace-write`, Claude `--allowedTools` 에 `Write`). Actions 의 `parallel` 은 잡마다
자기 체크아웃이라 등가가 아니다. 그래서 **실행을 직렬화**한다. 대가는 벽시계 시간.

**의미는 직렬화하지 않는다:**

| 모드 | 도는 것 | `early_exit` |
|---|---|---|
| `parallel`(기본) | 셋 다 | 라운드를 줄이지 않음 |
| `sequential` | Claude → Codex → Gemini | 체인을 끊음(AT-2125) |

근거: 기본 모드로 돌린 운영자는 **리뷰 셋**을 기대하지, 하나가 중단을 요청하기 전까지 도는 만큼을
기대하지 않는다.
[#170 T3](https://github.com/ignite-corp/ai-dev-pr-review/pull/170#discussion_r3996038241) /
[답글](https://github.com/ignite-corp/ai-dev-pr-review/pull/170#discussion_r3996187733)

**증거 — 측정됨, 그리고 첫 테스트는 아무것도 검사하지 않았다.** 동시성 테스트가 처음엔 in-flight 창이
관측 불가능하게 좁아 무의미했다. **일부러 다시 동시로 만든 빌드**에 돌려 3개 중 1개만 실패하는 것을 보고
알았고, 창 안에 실제 대기를 넣어 다시 재니 동시 빌드 2 실패 / 직렬 빌드 4 통과가 됐다. 그 대기가 왜
있는지를 헬퍼 docstring 에 적었다.

**직렬화가 다른 전제를 무너뜨린 자리도 기록됐다 — 측정됨.** 스레드 풀일 때는 셋이 동시에 시작해
창이 좁았는데, 직렬화가 "1이 끝나고 2가 시작"을 **보장된 순서**로 만들었다. 그래서 격리(지금은 strip)가
**리뷰어마다** 돌아야 한다: BEFORE `codex: 'PLANTED BY REVIEWER 1'` / `gemini: 동일` / `ESCAPED: True`
→ AFTER 둘 다 `None`
([#170 T11 답글](https://github.com/ignite-corp/ai-dev-pr-review/pull/170#discussion_r4002036719)).

**그리고 그 불변식은 shim 이 아니라 프로세스 그룹에 걸린다 — 측정됨.** 리뷰어 타임아웃이
`subprocess.run` 이면 **shim 만 죽고 그 아래 CLI 는 고아로 살아남아** 다음 리뷰어가 읽는 트리에 쓴다:
```
BEFORE  3초 뒤: CLAUDE.md exists = True   content: 'orphan write'   ORPHAN SURVIVED
AFTER   3초 뒤: CLAUDE.md exists = False
```
`Popen(start_new_session=True)` + `wait(timeout)` + **SIGTERM 먼저, 그다음 무조건 SIGKILL**
(shim 이 SIGTERM 에 곱게 죽는 것은 그 아래 CLI 에 대해 아무것도 말하지 않는다). 유예가 동작하는 것도
확인했다(`review-claude.json` 에 `{"verdict": "timed out"}`).
총칭 한정사도 함께 좁혔다: `Never two at once, in either mode.` →
**`One reviewer's process group at a time, in either mode.`**, 잔여(스스로 `setsid` 하는 후손)를 명시.
[#170 R11 응답](https://github.com/ignite-corp/ai-dev-pr-review/pull/170#issuecomment-5746468375)

---

### 1-12. Actions 경로와 **일부러 다른** 것들 — 각각 이유와 함께

[#172 본문](https://github.com/ignite-corp/ai-dev-pr-review/pull/172)에 표로 있고, 리뷰에서 각각
확인됐다.

| 차이 | 근거 | 증거 |
|---|---|---|
| 리뷰어 직렬 실행 | §1-11 | 측정됨 |
| `BOT_LOGIN` 기본값이 **인증된 `gh` 로그인**(`github-actions[bot]` 아님) | 로컬에서 그 작성자는 운영자이고, 워크플로 기본값이면 **실행마다 판정 코멘트가 쌓인다**(AT-2208) | 논증됨 |
| 인라인 코멘트를 **리뷰어 전원 종료 후 직렬 게시** | 그래야 각 리뷰어가 앞 리뷰어가 단 것에 대고 중복을 거른다. Actions 에서는 셋이 경쟁해 아무도 서로를 못 본다 | 논증됨 |
| **리뷰어 종료 코드가 집계에 닿는다** | Actions 는 `continue-on-error` 로 그것을 잃어, 자격 장애가 성공으로 보고되고 집계는 "early-exit or no-output"이라 말한다(AT-1837) | 논증됨 |
| 레거시 판정 이름 승격이 **워크플로보다 먼저** | 워크플로의 `Normalize review file name` 이 run 단계가 오류 판정을 쓴 **뒤에** 돌아 진짜 판정을 가린다 | 논증됨(§3 의 알려진 간극) |
| 크기 스킵 코멘트에 마커를 단다 | 마커 없는 코멘트는 접기 패스(`REVIEW_MARKER in node["body"]`, `aggregate_reviews.py:1100`)에 안 보여 **재실행마다 쌓인다** | 측정됨(접기 기전 확인) |

`ALLOW_AUTO_APPROVE` 는 **꺼진 채로 고정**되고, 운영자가 `true` 로 export 해도 테스트가 그것을 붙든다
([#172 본문](https://github.com/ignite-corp/ai-dev-pr-review/pull/172)). 다만 그 보장이 어디에 사는지는
#171 리뷰에서 문제가 됐다 — §2-H.

---

### 1-13. (#171, 머지됨 — 남는 설계) 한 벌짜리 프롬프트·설정, 그리고 **합칠 수 없는 사본은 CI 가 같음을 증명한다**

- **프롬프트는 모듈이 권위**이고 YAML 의 사본은 **바이트 대조**로 묶인다. 테스트가
  `base-ai-review-single.yml` 의 `run:` 블록을 꺼내 bash·jq 로 실행하고 `$GITHUB_ENV` 에서
  `CLAUDE_PROMPT` 를 읽어 바이트 비교한다. **합치지 않는 이유는 릴리스 핀**이다 —
  `self-review.yml` 이 PR 의 YAML 을 **마지막 릴리스 태그에 핀된 스크립트 체크아웃**에 대고 돌리므로,
  PR 에만 있는 모듈을 부르는 스텝은 다음 릴리스까지 **빈 Claude 프롬프트**를 만든다
  ([#171 본문](https://github.com/ignite-corp/ai-dev-pr-review/pull/171)). — **논증됨**
- **모델 id 는 두 번 적지 않는다.** 워크플로의 `vars.X || 'default'` 를 실행 시점에 파싱하고,
  파싱된 값이 YAML 에 **여전히 리터럴로 있는지** 테스트가 단언한다. 근거: `claude-opus-4-8` 이
  2026-09-11 에 은퇴하며 핀된 아홉 저장소를 조용히 깼다. — **논증됨(사건 기반)**
- **드리프트 가드는 조용히 사라질 수 없다.** `jq`/`bash` 부재가 모듈 전체를 skip 시키고 있었다.
  CI 에서는 skip 이 아니라 **실패**한다:
  `RuntimeError: bash, jq missing on CI: ... would report a green run with the two copies of the
  Claude prompt unchecked`. **양방향 확인 — 측정됨**
  ([#171 T1](https://github.com/ignite-corp/ai-dev-pr-review/pull/171#discussion_r3996208334) /
  [답글](https://github.com/ignite-corp/ai-dev-pr-review/pull/171#discussion_r4002035577)).
  *"건너뛴 테스트는 통과가 아닙니다."*
- **파이썬 쪽만 고치면 대조가 깨진다.** `thread_count` 검증도, `"[]"` 분기도 **거절**됐다 —
  한쪽에만 넣으면 이 모듈이 존재하는 이유인 패리티가 깨진다. 대신 **그 경우를 바이트 대조 테스트에 넣어**
  일치가 논증이 아니라 CI 로 강제되게 했다
  ([#171 T4](https://github.com/ignite-corp/ai-dev-pr-review/pull/171#discussion_r3996208339),
  [T5](https://github.com/ignite-corp/ai-dev-pr-review/pull/171#discussion_r3996208343) /
  [답글](https://github.com/ignite-corp/ai-dev-pr-review/pull/171#discussion_r4002036010) — **측정됨**:
  `load_threads` 가 빈 배열에서 `('0','')` 를 내므로 `"[]"` 는 도달하지 않고, 도달해도 두 사본이 같다).
- **설정 파서는 운영자가 틀리는 자리를 이름한다.** 줄 끝 주석 분리(따옴표 값은 리터럴 `#` 유지,
  `0.6#x` 는 그대로 — git config 규칙), 따옴표+주석 조합(전에는 `"our-bot"` 이 따옴표째 값이 됐다),
  환경값도 파일값처럼 strip(`export X=$(...)` 가 끝의 줄바꿈을 모델 id 에 싣는다), 워크플로가 모양을
  바꿨을 때 **무엇을 못 찾았는지 말하는** `ConfigError`. 각각 **측정됨**
  ([#171 T6](https://github.com/ignite-corp/ai-dev-pr-review/pull/171#discussion_r3996208344),
  [T8](https://github.com/ignite-corp/ai-dev-pr-review/pull/171#discussion_r4002059822),
  [T17](https://github.com/ignite-corp/ai-dev-pr-review/pull/171#discussion_r4042360920),
  [T14](https://github.com/ignite-corp/ai-dev-pr-review/pull/171#discussion_r4042226195)).
- **`collect_review_threads.sh` 가 잘못 쓴 파일은 크래시가 아니라 "스레드 없음"으로 떨어진다.**
  근거의 모양이 기록돼 있다 — *"'PR 이 더는 그 파일을 공급할 수 없다'로 닫을 수도 있었지만 그건 근거의
  모양이 틀립니다. 닿지 않는다가 아니라 **닿아도 안전하다**여야 합니다."* 그리고 그 단계는 **아예 실패해도
  되는** 단계인데 *잘못 쓴 파일에 더 치명적인 것은 앞뒤가 안 맞는다*
  ([#171 T13](https://github.com/ignite-corp/ai-dev-pr-review/pull/171#discussion_r4042226193) /
  [답글](https://github.com/ignite-corp/ai-dev-pr-review/pull/171#discussion_r4042331042)). — **논증됨**

---

## 2. 끝내 정해지지 않은 것

재작성이 **물려받지 말고 의식적으로 정해야 할** 것들. 각각 질문 / 선택지 / 택한 것 / 근거의 종류.

### 2-A. 두 shim 을 합칠 것인가

- **질문.** `review_claude_local.py` 와 `review_codex_local.py` 의 닮은 부분(`error_verdict`,
  종료 코드 상수 셋, `_exit_reason`, `main()` 의 catch-all, spawn 트리아지)을 공용 모듈로 뺄 것인가.
- **선택지.** (a) `local_reviewer_support.py` 로 추출 (b) 그대로 두고 갈릴 때 합친다.
- **택한 것.** (b) — *"두 shim 이 계약이 다른 두 CLI 를 감쌉니다. 지금 닮아 보이는 부분을 합치면 두 CLI 가
  갈릴 때마다 공용 코드에 분기가 생기고, 그 분기가 곧 두 계약이 섞이는 자리가 됩니다."*
  ([#172 T6 답글](https://github.com/ignite-corp/ai-dev-pr-review/pull/172#discussion_r4042333271))
- **근거의 종류.** **논증됨.** 그리고 **반증이 나왔다.** 라운드 컷오프(R12)에서 같은 리뷰어가 재제기:

  > The two shims duplicate more verbatim code than when this was last raised, not less. … `error_verdict`
  > went from near-identical to byte-identical … three new duplications were added on top … Each copy is a
  > place a future fix to the shared contract can be applied to one shim only — **which is what the prior
  > round's timeout-handling fixes already had to be applied twice for.**

  [#172 R12 컷오프](https://github.com/ignite-corp/ai-dev-pr-review/pull/172#issuecomment-5749114728)
- **상태.** **미해결.** 저자의 예측("갈릴 때 드러난다")과 기록("두 번 고쳐야 했다")이 정반대다.
  §4-C 의 실제 비용이 이 결정에 달려 있다.

### 2-B. 리뷰어가 격리/삭제 대상 경로를 **다시 만들면** 어떻게 할 것인가

- **질문.** 트리 쪽 점유자와 보관본 중 누가 이기는가.
- **선택지.** (a) 큰 소리로 실패하고 보관 디렉터리를 이름한다 (b) 보관본이 이기고 점유자는 경고와 함께 버린다.
- **경과.** 문서가 처음부터 (a)를 적었고, 리뷰어가 코드가 파일 모양에서 **조용히 덮어쓴다**고 지적
  ([#170 T13](https://github.com/ignite-corp/ai-dev-pr-review/pull/170#discussion_r4001916799)).
  (a)로 고쳤다. 그런데 격리가 **리뷰어마다** 돌게 된 뒤로는 무언가를 쓰는 **첫 리뷰어**에서 걸려
  라운드 전체가 죽었다. 그래서 **(b)로 되돌렸다** — 점유자는 언제나 리뷰어가 스크래치 클론에 쓴 것이고,
  보관본은 체크아웃 자신의 것이므로.
- **근거의 종류.** **논증됨** — *"흘린 쓰기 하나로 라운드를 죽이는 것은 위험한 적 없는 것을 지키려
  쓸 수 있는 것을 부수는 일."*
- **상태.** **문제 자체가 사라졌다**(worktree 재작성으로 되돌릴 것이 없음). 하지만 재작성이 다시
  "지우고 리뷰어가 다시 만들면?"을 만나면 **같은 질문이 그대로 돌아온다.** 그때 참고할 것은 이
  왕복 전체다: [T13](https://github.com/ignite-corp/ai-dev-pr-review/pull/170#discussion_r4001916799) →
  [T17](https://github.com/ignite-corp/ai-dev-pr-review/pull/170#discussion_r4002054821) →
  [T20](https://github.com/ignite-corp/ai-dev-pr-review/pull/170#discussion_r4002071262).

### 2-C. Codex 프롬프트를 argv 로 넘길 것인가 stdin 으로 넘길 것인가

- **질문.** Claude shim 은 stdin(`input=prompt`), Codex shim 은 argv 한 요소. 후자는 리눅스의
  `MAX_ARG_STRLEN` 에 걸린다.
- **측정됨.**
  ```
  ARG_MAX: 2097152
  131000 bytes in one argv element: OK
  131072 bytes: OSError [Errno 7] Argument list too long
  ```
  `PR_SIZE_LIMIT` 근처의 프롬프트는 약 244 KB — **한도를 넘지 않은 큰 PR 이 여기서 막힌다.**
  (별개 측정: 환경 변수도 같은 항목당 한도에 걸린다 — `largest value that execs: 131053`,
  `smallest that raises E2BIG: 131054`, `KEY=VALUE` 전체 기준 `131071/131072`.)
- **택한 것.** **stdin 으로 바꾸지 않았다.** `codex exec` 가 프롬프트를 stdin 에서 읽는지 이 기계에서
  확인할 수 없고(codex 미설치), **플래그를 추측하지 않기로 했다.** 대신 **스폰 전에 크기를 검사해 있는
  그대로 보고**한다(`_EXIT_PROMPT_TOO_LARGE`).
- **상태.** **미해결 — 답은 CLI 가 설치된 기계에서만 나온다.**
  [#172 T22 답글](https://github.com/ignite-corp/ai-dev-pr-review/pull/172#discussion_r4056227749)
  부수 효과: 프롬프트가 프로세스 테이블에 노출된다(리뷰어 지적, 미해결).

### 2-D. CLI 프로세스 상한을 **고정할 것인가 운영자 환경을 따라갈 것인가**

- **경과.** R6 에서 `env = {**os.environ, **cli_env()}` 로 써서 **컴포짓 기본값이 운영자 export 를 짓눌렀다.**
  R7 에서 **뒤집었다**(`{**cli_env(), **os.environ}`) — 컴포짓 값은 **러너가 없는 자리를 대신하는
  기본값**이고, 그건 문서가 적은 우선순위의 3번이다. **측정됨**
  ([#172 R7 응답](https://github.com/ignite-corp/ai-dev-pr-review/pull/172#issuecomment-5746491861)).
- **그런데 그 결과 새 불일치가 남았다.** `_CLI_TIMEOUT_SEC = 600` 은 고정인데 `API_TIMEOUT_MS` 는
  운영자가 900000 으로 올릴 수 있다 → **CLI 에는 900초를 말하고 파이썬이 600초에 죽이며, 운영자는
  자기가 올린 예산에 대해 "did not finish within 600s"를 받는다.** 주석이 약속하는 쌍은 기본값에서만
  성립한다. R12 컷오프에서 **minor 로 남았다.**
  [#172 R12 컷오프](https://github.com/ignite-corp/ai-dev-pr-review/pull/172#issuecomment-5749114728)
- **상태.** **미해결.** 방향(운영자가 이긴다)은 정해졌고, 그 방향의 **귀결이 정해지지 않았다.**

### 2-E. 같은 설정의 두 전파 방향

- `LENS_LOCAL_CONFIG`(드라이버 → 리뷰어)는 **상속된 값을 덮는다** — 부모에서 `--config` 가 이미
  그 변수를 이겼고 자식은 같은 파일을 읽어야 하므로.
- `API_TIMEOUT_MS` 등 컴포짓 값은 **양보한다** — 없는 러너를 대신하므로.
- **택한 것.** 두 방향을 **명시적으로 정하고 양쪽 다 테스트**했다
  ([#172 R7 응답](https://github.com/ignite-corp/ai-dev-pr-review/pull/172#issuecomment-5746491861)). — **측정됨**
- **남은 것.** 상대 경로 `--config` 는 드라이버(원래 cwd)와 리뷰어(`cwd=work`)가 **다르게 해석한다.**
  codex R8 컷오프가 짚었고 해결 기록이 없다:
  [#170 R8 컷오프](https://github.com/ignite-corp/ai-dev-pr-review/pull/170#issuecomment-5744239092).
  **미해결.**

### 2-F. `ConfigError` 는 판정으로 게시하는가

- **질문.** "한 실행은 판정을 낸다"는 불변식이 설정 파싱 실패에도 적용되는가.
- **택한 것.** **아니다.** `::error::` + 종료 코드 1 로 보고하고 판정은 게시하지 않는다 —
  **집계가 해석에 실패한 바로 그 설정을 필요로 하기 때문.** 렌더할 것이 없고 보고할 리뷰도 없다.
  되지 말아야 할 것은 traceback 이었고 그것이 고쳐졌다.
  [#172 T18 답글](https://github.com/ignite-corp/ai-dev-pr-review/pull/172#discussion_r4054146369) — **논증됨**
- **상태.** 근거는 튼튼하지만 **불변식에 구멍이 하나 열려 있는 상태**이고, 그 구멍의 경계가 라운드마다
  움직였다(`config.get_int("PR_SIZE_LIMIT")` 는 집계가 읽지 않는 설정이라 예외 조항 **밖**으로 옮겨졌다 —
  [#170 T44 답글](https://github.com/ignite-corp/ai-dev-pr-review/pull/170#discussion_r4055430927)).
  재작성은 이 경계를 **처음부터** 정해야 한다.

### 2-G. 크기 스킵 코멘트의 `lens:skipped` 마커 — 컨슈머 게이트에 비대칭이 생긴다

- 지시대로 크기 스킵 코멘트에 `<!-- lens:skipped reason=size-limit ... -->` 를 붙였다(그래야 접힌다).
- **그런데 집계 자신의 크기 스킵 판정은 일부러 그 마커를 달지 않는다**(그 경로는 실패를 보고하므로
  게이트가 어차피 막힌다). *"`lens:skipped` 가 있으면 스킵으로 간주"* 로 키를 잡은 컨슈머는 이제 크기
  스킵에서도 그것을 본다.
- **저자가 스스로 밝히고 되돌릴 의사를 적었으나 답을 받지 못했다** — *"다음 라운드가 찾게 두지 않고
  지금 밝힙니다 — 이 비대칭이 곤란하면 되돌리겠습니다."*
  [#172 T28 답글](https://github.com/ignite-corp/ai-dev-pr-review/pull/172#discussion_r4056227911)
- **상태. 미해결.** 재작성이 명시적으로 정해야 한다.

### 2-H. `ALLOW_AUTO_APPROVE` 를 **구조로** 막을 것인가 문서로 적을 것인가

- **지적(#171).** `local_review_config` docstring 이 *"`ALLOW_AUTO_APPROVE` is deliberately NOT resolved
  through here … the driver pins it off"* 라고 적지만, `workflow_defaults()` 는 제외 목록 없이 모든
  `vars.X || '...'` 를 긁으므로 그 이름도 평범한 설정으로 들어오고 `get()` 의 env-우선 규칙이 그대로
  적용된다. *"The 'pinned off' guarantee lives only in a driver that does not exist yet, in a module whose
  whole premise is that a value in a second place goes stale silently."*
- **제안.** `NOT_LOCALLY_RESOLVED = frozenset({"ALLOW_AUTO_APPROVE"})` 로 **구조적 제외**.
- **택한 것.** 제외를 넣지 않고 **docstring 이 실제로 강제하는 자리(`aggregate_env`)와 그 테스트를
  가리키게** 고쳤다.
  [#171 T11](https://github.com/ignite-corp/ai-dev-pr-review/pull/171#discussion_r4042226187) /
  [답글](https://github.com/ignite-corp/ai-dev-pr-review/pull/171#discussion_r4042330683) — **논증됨**
- **상태.** 보장은 여전히 **드라이버 쪽에** 있고, 그 드라이버가 지금 폐기된다.
  **재작성이 이 보장을 다시 만들어야 하며, 어디에 둘지는 정해지지 않았다.**

### 2-I. 기대 호스트를 `GH_HOST` 로 추정할 것인가 `gh` 에게 물을 것인가

- **지적.** `gh auth login --hostname` 으로 엔터프라이즈에 인증한 운영자는 `GH_HOST` 없이도 거기서
  클론할 수 있고, 그러면 **드라이버가 자기 클론을 공격으로 거절**한다.
- **택한 것.** `gh help environment` 를 인용해 **By design** — *"GH_HOST: specify the GitHub hostname for
  commands where a hostname has not been provided…"* 맨 `owner/name` 은 호스트를 주지 않으므로 이 검사와
  `gh repo clone` 이 같은 값을 같은 기본값으로 읽는다. `gh auth status` 파싱은 **표시용 표면**이라
  거절했다(`gh pr view --json author` 가 `app/dependabot` 이라고 말하는 것과 같은 부류).
  [#170 T42](https://github.com/ignite-corp/ai-dev-pr-review/pull/170#discussion_r4054183770) /
  [답글](https://github.com/ignite-corp/ai-dev-pr-review/pull/170#discussion_r4054215076)
- **근거의 종류.** **논증됨(문서 독해).** **엔터프라이즈 호스트에서 실측된 적 없다.**

### 2-J. 워크플로 **기본값**을 읽는 것과 CI 가 **실제로 쓰는 값**을 읽는 것

- **질문.** 드라이버가 모델·설정을 `vars.X || '기본값'` 의 **기본값**에서 푸는데, CI 는 org/repo
  `vars` 가 그 기본값을 **덮은 값**으로 돈다. 로컬 리뷰가 CI 리뷰와 다른 모델로 도는 것을 허용할 것인가,
  맞출 것인가, 아니면 **최소한 말할 것인가**.
- **현재 동작.** 셋 다 아니다. 드라이버는 `gh` 로 저장소를 읽으면서도 **`vars` 는 보지 않고**, 어느
  출력에도 어떤 모델을 썼는지 나오지 않는다.
- **§5-5 에서 측정된 결과** — 세 리뷰어 중 **둘이 조용히 갈린다**:

  | 리뷰어 | 드라이버가 읽는 워크플로 기본값 | org `vars` (CI 가 실제로 쓰는 값) |
  |---|---|---|
  | claude | `claude-sonnet-4-6` | **`claude-opus-5`** |
  | codex | `gpt-5.6-luna` | `gpt-5.6-luna`(같음) |
  | gemini | `gemini-2.5-pro` | **`gemini-3.5-flash-lite`** |

- **이것이 §1-13 의 "모델 id 는 두 번 적지 않는다" 결정에 붙는 단서다.** 그 결정은 **드리프트를
  막는 데는 옳고**(핀이 바뀌면 따라온다), *"로컬이 CI 와 같은 리뷰를 한다"* 는 보증은 **주지 않는다.**
  문서 어디에도 그 구분이 없었다.
- **상태. 미해결.** 설정 해석 우선순위(§2-D · §2-E)와 같은 결정이고, 재작성이 정해야 한다.
  최소선은 **어떤 모델로 돌았는지 출력에 적는 것**이다 — 지금은 `claude-run.log` 를 열어야만 알 수 있다.

### 2-K. 타입 체크 · 함수 길이 — 규칙이 있는데 도구가 없다

§3 의 간극이기도 하지만 **결정이 미뤄진 것**이므로 여기에도 적는다. 미뤄진 것은 **넷**이고,
티켓은 **둘**이 덮는다 — 이 절만 읽고도 매핑이 보이도록 적는다:

| # | 미뤄진 결정 | 티켓 | 상태 |
|---|---|---|---|
| 1 | `pyrightconfig.json` 을 만들어 `.github/scripts` 를 포함할 것인가 | [AT-2418](https://ignitecorp.atlassian.net/browse/AT-2418) | 해야 할 일 |
| 2 | 그렇게 하면 드러나는 기존 16~18건을 어떻게 할 것인가 | [AT-2418](https://ignitecorp.atlassian.net/browse/AT-2418)(1번과 **같은 티켓**) | 해야 할 일 |
| 3 | 80줄 한도를 어떤 도구로 강제할 것인가(또는 체크리스트에서 뺄 것인가) | [AT-2420](https://ignitecorp.atlassian.net/browse/AT-2420) | 해야 할 일 |
| 4 | `RUF059`(unused unpacked variable)를 켤 것인가 | **없음** | — |

**1·2 가 한 티켓인 것은 의도한 것이다** — 설정을 켜는 변경과 그것이 드러내는 기존 위반을 처리하는
일이 분리되면 전자만 머지되고 CI 가 빨개진다. 그래서 *항목 넷, 티켓 둘, 티켓 없는 것 하나*다.

**티켓이 있다는 것은 결정이 났다는 뜻이 아니다** — 넷 중 티켓이 있는 셋도 전부 `해야 할 일` 상태이고,
*무엇을 강제할지*는 여전히 정해지지 않았다. 재작성이 이 게이트 위에 서려면 그 결정이 먼저다.

---

## 3. 알려진 간극

### 3-1. #172 본문의 "Merge constraint" 다섯 — **검증했다. 표는 정확하다.**

#170 에 있고 #172 에 없는 방어 다섯. 본문 표와
[#172 T33 답글의 감사 표](https://github.com/ignite-corp/ai-dev-pr-review/pull/172#discussion_r4056671337)가
내용·순서까지 일치한다.

| | 함수 | #170 에 있고 #172 에 없는 것 | #172 단독 머지 시 결과 |
|---|---|---|---|
| 1 | `run_reviewer` | `_REVIEWER_TIMEOUT_SEC` + `Popen(start_new_session=True)` + 프로세스 그룹 SIGTERM→SIGKILL | **리뷰어 스폰에 타임아웃이 아예 없음** — 멈춘 CLI 가 드라이버를 무한히 잡는다 |
| 2 | `aggregate` | 스폰 주위 try/except | 판정 게시자의 실패가 traceback 으로 탈출 |
| 3 | `append_prior_context` | 관용을 `OSError`/`TimeoutExpired` 까지 | 주석이 지키겠다고 한 리뷰를 `bash` 부재나 멈춘 `gh` 가 죽인다 |
| 4 | `post_inline_comments` | 같은 가드 | 인라인 게시 스폰 실패가 집계 전에 끝낸다 |
| 5 | `run_reviewers` / `aggregate_env` | 결론 딕셔너리 호출자 소유, `prepare_result` 하드코딩 안 함 | 중간 raise 가 끝난 리뷰어를 전부 잃고, 집계에 **항상 "성공"**이라 말한다 |

**1번은 공격자 없이 오늘 사용자가 맞는다.** 나머지는 다른 단계의 실패를 필요로 한다.
다섯 다 **#170 이 통째로 다시 쓰는 함수 안**에 있어, 가져오려면 `ReviewOutcome` ·
`report_stage_failure` · `kill_reviewer_group` 을 끌어오거나 **다르게 구현**해야 하는데 후자가 바로
§4-C 를 만든 방식이다. 넷은 저자의 두 축 감사가, **다섯 번째는 리뷰어가** 찾았다.
**두 축 모두 `review_pr_local.py` 안에서만 비교하므로, #170 이 다른 파일에 더한 방어는 어느 쪽도 못 본다.**

### 3-2. 검증 도구가 실제로는 돌지 않았다

- **pyright 는 이 저장소에서 한 번도 실제로 돈 적이 없다.**
  ```
  $ pyright --outputjson .github/scripts/review_pr_local.py
  filesAnalyzed = 0
  errorCount    = 0
  ```
  기본 exclude `**/.*` 때문에 dot-디렉터리로 내려가지 않고, **파일을 이름으로 직접 지정해도** 제외하며,
  그러면서 종료 코드 0 과 "0 errors" 를 돌려준다. `pyrightconfig.json` 없음, `ci.yml` 에 타입 체크 스텝 없음.
  **#172 · #171 · #170 에서 주장된 pyright 결과는 전부 공허했고, 공개로 취소됐다.**
  [#172 정정](https://github.com/ignite-corp/ai-dev-pr-review/pull/172#issuecomment-5746459387) ·
  [#170 취소](https://github.com/ignite-corp/ai-dev-pr-review/pull/170#issuecomment-5746468375)
  실측(`.github/` 밖 복사본): **16~18 errors, 전부 테스트 하니스**(`**kwargs` 전달의
  `reportArgumentType` 16, `reportOperatorIssue` 1, `Args` vs `Namespace` 1), **드라이버 본체 0, 델타 0.**
  **고치지 않았다. 게이트를 세우는 변경(설정 + 16~18건 처리)은 별건으로 남았고,
  [AT-2418](https://ignitecorp.atlassian.net/browse/AT-2418) 로 등록돼 있다**(2026-09-20 05:28 생성).
- **80줄 함수 한도를 강제하는 것이 CI 에 없다.** 체크리스트
  `examples/prompts/code-review-checklist.md:15` 가 *"No functions exceeding 80 lines (ruff PLR0915)"*
  라고 적지만, **PLR0915 는 `too-many-statements` 이고 기본 상한 50 문장**이다. 116줄 · 36문장짜리
  `main()` 에서 `All checks passed!` 가 나왔다. **규칙 문서가 자기 괄호 안의 도구가 측정하지 않는 수를
  앞에 적고 있다.** 이 PR 에서 그 한도가 두 번 지적되고 두 번 resolved 로 닫히는 동안 아무 도구도
  실패하지 않았다. **체크리스트는 건드리지 않았고,
  [AT-2420](https://ignitecorp.atlassian.net/browse/AT-2420) 으로 등록돼 있다**(2026-09-20 09:27 생성 —
  이 항목을 찾아낸 라운드 응답이 09:26 이므로 **거의 즉시** 티켓이 났다).
  [#172 응답](https://github.com/ignite-corp/ai-dev-pr-review/pull/172#issuecomment-5748944503)
- **`RUF059`**(unused unpacked variable)는 현재 ruff 버전에서 preview 게이트라 **켜지 않았다** —
  `ruff --select F841` 은 튜플 언패킹에 눈이 없어 여섯 건을 전후 모두 통과시켰다.
  `pyproject`/`ruff.toml` 변경은 범위 밖. **별건이고, 이 셋 중 유일하게 티켓이 없다**
  (2026-09-20 기준 `lens` 라벨 전수 확인).
  [#170 R11 응답](https://github.com/ignite-corp/ai-dev-pr-review/pull/170#issuecomment-5746468375)

> **이 절의 티켓 상태는 2026-09-20 에 Jira 를 직접 확인해 채웠다.** 이 문서의 초안은 세 항목 모두
> **"티켓 없음"** 이라고 적었는데, 그 근거는 PR 기록뿐이었다 — 초안을 쓴 세션은 브리프상 PR 만 읽었고
> Jira 를 보지 않았다. 셋 중 **둘은 이미 티켓이 있었다.**
> **한 저장소에서의 부재가 다른 저장소에서의 부재를 증명하지 않는다**는 것이 이 문서 §4-A 가 말하는
> 바로 그 모양이고, 이 문서 자신이 한 번 그렇게 틀렸다는 사실을 지우지 않고 남긴다.

### 3-3. Actions 경로의 실버그 둘 — 이 PR 들이 발견했고 고치지 않았다

- **`Normalize review file name` 순서.** `base-ai-review-single.yml` 에서 그 스텝이 run 스텝이 이미
  오류 판정을 `review-codex.json` 에 쓴 **뒤에** 돌아, 모델이 진짜로 `verdict-openai.json` 으로 쓴
  판정을 가린다. 로컬 드라이버는 순서를 바꿨고 **워크플로는 건드리지 않았다.**
  리뷰어가 **머지 전에 티켓을 만들고 ID 를 인용하라**고 요구했으나(주변 주석들은 전부 AT-#### 를 단다),
  답은 *"By design: 의도한 기록"* 이었다 — **리뷰 당시에는 티켓이 없었고, 2026-09-20 에
  [AT-2424](https://ignitecorp.atlassian.net/browse/AT-2424) 가 만들어졌다**(priority High).
  **리뷰어가 옳았고, 그 요구가 넉 달이 아니라 엿새 만에 이행된 것은 이 문서를 만드는 과정 덕분이다** —
  지적 자체는 [2026-09-14](https://github.com/ignite-corp/ai-dev-pr-review/pull/170#discussion_r4002071271)에
  들어왔고 그때 닫혔다. 티켓 생성 시 `base-ai-review-single.yml` 을 직접 확인했으며 기전이 이 기록과
  같다: `Normalize` 의 가드가 `if [ ! -f "$EXPECTED" ]` 인데 `Run Codex review` 가 실패 경로에서 이미
  오류 판정을 `review-codex.json` 에 써 놓아 **승격이 통째로 건너뛰어진다.**
  [#170 T23](https://github.com/ignite-corp/ai-dev-pr-review/pull/170#discussion_r4002071271) /
  [답글](https://github.com/ignite-corp/ai-dev-pr-review/pull/170#discussion_r4042179274)
- **`base-ai-review-single.yml` 이 프롬프트를 같은 방식으로 넘기고 같은 `MAX_ARG_STRLEN` 상한을 갖는다.**
  운영 중인 Actions 경로의 실버그이고, **이 PR 에서 워크플로는 건드리지 않았다.**
  *"별건으로 남길 것"* 이 **실제로 지켜졌다** —
  [AT-2411](https://ignitecorp.atlassian.net/browse/AT-2411)(2026-09-19 등록,
  지적과 같은 날). 두 항목 중 이쪽만 약속이 이행돼 있었다.
  [#172 T22 답글](https://github.com/ignite-corp/ai-dev-pr-review/pull/172#discussion_r4056227749)

### 3-4. 측정되지 않은 채 남은 위협 표면

- **음성 대조군이 확립되지 않았다.** 보호를 해제한 같은 적대 파일이 **한 번의 시행에서 평범한 리뷰를
  냈다.** *"No claim is made that without this the review gets hijacked."*([#170 본문](https://github.com/ignite-corp/ai-dev-pr-review/pull/170))
- **`codex` 는 이 기계에서 프로브하지 못했다** — `.codex/` 항목은 측정이 아니라 보수적 추정.
  `.cursor/` 도 마찬가지.
- **`CLAUDE.local.md` 는 프로브되지 않았고 목록에 없다.** 관례적으로 gitignore 되지만 PR 이 일부러
  커밋할 수 있다. 목록의 규칙(출처 표시 없는 이름은 넣지 않는다)에 따라 **의도적으로 비어 있다.**
  [#170 T21](https://github.com/ignite-corp/ai-dev-pr-review/pull/170#discussion_r4002071265)
- **`.git/config` 지문이 증명하지 않는 것**: `.git` 의 나머지(objects · refs · `info/` · alternates),
  작업 트리, 그리고 마커가 `run_dir` 에 있다는 사실이 *"아무것도 `run_dir` 에 닿을 수 없다"* 를
  뜻하지 않는다는 것. **명시적으로 적혀 있다.**
- **프로세스 그룹 킬을 스스로 `setsid` 로 빠져나가는 후손**은 닿지 않는다. 잔여로 명시.
- **CLI 는 운영자로, 운영자의 자격으로 돈다.** 리뷰 대상 코드를 실행하지는 않지만 **CLI 가 할 수 있는
  일을 CLI 가 하는 것을 막는 것은 없다.** 목록은 CLI 를 올릴 때 **재프로브**를 요구한다 — 프로브는
  파일 셋과 codeword 하나.
- **트리의 소스는 여전히 읽힌다** — 소스 안의 주석·문자열이 모델에게 말을 걸 수 있다.

### 3-5. 실행되지 않은 검증

[#172 본문](https://github.com/ignite-corp/ai-dev-pr-review/pull/172) "Deliberately not done":
게시까지 포함한 전체 실행 없음 / Codex·Gemini 를 진짜 모델에 대고 돌리지 않음(codex 미설치, 키 없음 —
실패 경로만) / `gh repo clone` over SSH 미검증(빌드 기계의 `gh` 는 HTTPS).

### 3-6. §2 의 미해결 항목들이 그대로 간극이다

**여덟이다.** 2-A(shim 중복, R12 에 minor 로 남음) · 2-C(codex 프롬프트 전달 방식) ·
2-D(운영자가 올린 타임아웃과 고정 프로세스 상한의 불일치, R12 에 minor 로 남음) ·
2-E(상대 경로 `--config`) · 2-G(마커 비대칭) · 2-H(`ALLOW_AUTO_APPROVE` 보장의 집) ·
2-I(엔터프라이즈 호스트 미실측) · **2-J(로컬이 CI 와 다른 모델로 돈다 — §5 가 측정한 뒤 추가)**.
2-K(타입 체크 · 함수 길이)는 티켓이 있어 §3-2 에서 따로 다룬다.
**여덟 다 §6 의 권고 9 가 이름한다** — 그 둘의 수가 어긋나면 재작성이 조용히 물려받는다.
추가로 **R12 컷오프에 남은 채 머지 시도된 것들**:
`REPO_RE.match` 의 끝 앵커 없음, `_action() -> dict` 맨 주석,
`RUN_ARTIFACTS` 가 shim 의 상수 이름을 다시 적음(드리프트 시 **이전 실행의 레거시 판정이 이번 실행의
답으로 승격**된다 — `RUN_ARTIFACTS` 주석이 막겠다고 적은 바로 그 실패),
`run()` 을 우회하는 subprocess 호출 넷.
[codex R12](https://github.com/ignite-corp/ai-dev-pr-review/pull/172#issuecomment-5749091741) ·
[claude R12](https://github.com/ignite-corp/ai-dev-pr-review/pull/172#issuecomment-5749114728) ·
[최종 집계](https://github.com/ignite-corp/ai-dev-pr-review/pull/172#issuecomment-5749116609)

---

## 4. 이 코드가 반복해서 만든 결함의 **모양**

### 4-A. **0 을 돌려주고, 그 0 이 통과로 읽힌다** — 검사가 돌았다와 그 검사가 이 경우를 볼 수 있다는 다른 사실

죽지 않고, 예외도 없고, 초록이다. 확인된 인스턴스 **아홉**:

| # | 사례 | 출처 |
|---|---|---|
| 1 | `pyright --outputjson` → `filesAnalyzed = 0`, `0 errors`, 종료 코드 0 | [#172](https://github.com/ignite-corp/ai-dev-pr-review/pull/172#issuecomment-5746459387) |
| 2 | `test_every_failure_path_leaves_a_verdict_file` 이 `run_cli` 를 **통째로 갈아끼워** 스폰도 컴포짓 YAML 읽기도 한 번도 실행하지 않음 — 실제로는 **CLI 출력 모양 셋** | [#172 R6](https://github.com/ignite-corp/ai-dev-pr-review/pull/172#issuecomment-5746306100) |
| 3 | env 테스트가 **단언 전에 키를 지워** 방향 역전을 못 봄 → **뒤집힌 채 초록** | [#172 R7](https://github.com/ignite-corp/ai-dev-pr-review/pull/172#issuecomment-5746491861) |
| 4 | 허용 목록 AST 테스트가 **막으려던 바로 그 경우**에 통과(§1-4) | [#170 R10](https://github.com/ignite-corp/ai-dev-pr-review/pull/170#issuecomment-5744547755) |
| 5 | `test_a_reviewer_that_hangs_after_writing_still_counts` 가 `subprocess.run` 을 패치했는데 경계가 `Popen.wait` 로 **옮겨간 뒤** 아무것도 가로채지 않음 → 빈 환경으로 **진짜 shim 을 띄우고도** 통과(자기가 써 둔 파일을 단언했으므로) | [#170 R11](https://github.com/ignite-corp/ai-dev-pr-review/pull/170#issuecomment-5746468375) |
| 6 | `step-env-*` 케이스가 스폰을 스텁하지 않아, `claude` 가 **설치돼 있는 기계**에서 진짜 CLI 를 호출하고 **단언과 무관한 이유로** 통과 | [#172 R7](https://github.com/ignite-corp/ai-dev-pr-review/pull/172#issuecomment-5746491861) |
| 7 | 첫 프롬프트 경계 테스트가 한 `tmp_path` 에서 두 번 렌더해 헬퍼가 **첫 `$GITHUB_ENV` 블록을 두 번 읽고 자기와 비교** | [#171 T9 답글](https://github.com/ignite-corp/ai-dev-pr-review/pull/171#discussion_r4042177796) |
| 8 | 동시성 테스트의 in-flight 창이 **관측 불가능하게 좁아** 아무것도 안 잡음(§1-11) | [#170 T3 답글](https://github.com/ignite-corp/ai-dev-pr-review/pull/170#discussion_r3996187733) |
| 9 | `test_ci_detection_reads_the_value_not_its_presence` 가 **모듈의 데이터와 식을 인라인으로 다시 선언**해 자기 사본을 검사 — 모듈을 presence-testing 으로 되돌려도 초록 | [#171 R5 컷오프](https://github.com/ignite-corp/ai-dev-pr-review/pull/171#issuecomment-5744081472) |

여기에 **도구 쪽 사례 둘**이 더 붙는다: PLR0915 가 116줄 함수를 통과(§3-2), `ruff F841` 이 튜플
언패킹에 눈 없음(§3-2). 그리고 기록에 따르면 **같은 부류가 이 저장소 밖에서도 났다** — "게이트가 봇
코멘트 0건을 통과로" 읽은 사례가 같은 날 함께 열거돼 있다
([#172](https://github.com/ignite-corp/ai-dev-pr-review/pull/172#issuecomment-5746459387)).

이 PR 스스로가 이 부류를 내내 지적해 놓고 **자기 검증 항목에는 같은 검사를 하지 않았다** —
*"리뷰어가 준 숫자는 따져 물으면서 제 팀이 준 `0` 은 그대로 실었습니다."*

**한 줄 요약(기록된 대로):** *출력의 요약을 출력 대신 읽는 것.* `pyright --outputjson` 에 일어났을 때와
집계 코멘트를 `head -20` 으로 자른 것(§4-F)이 다르지 않다.

> **이 절의 수가 초안에서 틀렸다.** 본문이 *"확인된 인스턴스 **일곱**"* 이라고 적는데 표는 **아홉 줄**
> 이었다. 표 아래의 *"도구 쪽 사례 둘"* 은 표 밖이라고 명시돼 있으므로 그 차이를 메우지도 못한다.
> **이 절이 서술하는 바로 그 부류다** — 아무도 다시 세지 않은 수가 통과로 읽혔다. 문서 자신이,
> 그 문장을 쓰는 절에서. #173 의 리뷰어가 표를 다시 세어 찾았고([claude minor](https://github.com/ignite-corp/ai-dev-pr-review/pull/173)),
> 같은 리뷰가 §4-B(13) · §4-C(5) · §4-D(7) · §4-F(4) 는 맞다고 확인해 **고립된 결함**임도 함께 보였다.
> 수는 고쳤고 이 문단은 남긴다 — 지우면 §4-A 의 가장 좋은 증거가 사라진다.
> (그 뒤 §1~§6 의 모든 열거를 다시 셌다. §5 는 이 리뷰 뒤에 추가돼 아무도 세지 않은 절이었다.)

### 4-B. **검사할 수 없는 총칭 한정사** — 주석 속의 "every / never / all"은 아직 안 쓴 테스트다

확인된 인스턴스:

| 총칭 | 무엇이 거짓이었나 | 출처 |
|---|---|---|
| `_CONFIG_READ_ERRORS` 위의 *"Everything reading the composite's YAML can raise"* | `AttributeError` 가 같은 읽기에서 닿는데 튜플에 없었다. **저자가 전날 "검사할 수 없는 총칭은 아직 안 쓴 테스트"라고 적어 놓고 두 라운드 만에 다시 썼다** | [#172](https://github.com/ignite-corp/ai-dev-pr-review/pull/172#issuecomment-5748944503) |
| `DirectWrite` docstring 의 *"One value, one test, no second opinion"* | 한 번도 보지 않은 **세 번째 자리**에 대해 거짓(§1-5) | 같은 곳 |
| `build_context` 주석의 *"Every value below is … display_path …"* | `labels` 가 그 함수를 직접 거치지 않는다(실제로는 `format_labels` 가 이름마다 적용 — 결론은 안전, 문장은 부정확) | [#170 T5](https://github.com/ignite-corp/ai-dev-pr-review/pull/170#discussion_r3996038250), [#172 T12](https://github.com/ignite-corp/ai-dev-pr-review/pull/172#discussion_r4042226744) |
| `run_reviewers` 의 *"Never two at once, in either mode."* | 고아 CLI 가 shim 보다 오래 산다 → **`One reviewer's process group at a time`** 으로 좁힘 | [#170 R11](https://github.com/ignite-corp/ai-dev-pr-review/pull/170#issuecomment-5746468375) |
| `EXCEPTION_BOUNDARY` 주석의 *"never leaves a traceback as its only output"* | `resolve_bot_login` 등 다섯이 경계 밖 | [#170 T44](https://github.com/ignite-corp/ai-dev-pr-review/pull/170#discussion_r4054328644) |
| `report_stage_failure` docstring 의 *"still exits non-zero … verdict posted first, reason shown after"* | 종료 코드가 그렇지 않았고, **순서도 지어낸 것**이었다(코드는 traceback 을 먼저 찍는다) | [#170 T39](https://github.com/ignite-corp/ai-dev-pr-review/pull/170#discussion_r4054183762) |
| `_write_manifest` docstring 의 **power-loss 내구성** | `os.replace` 의 디렉터리 메타데이터가 fsync 되지 않음 | [#170 T36](https://github.com/ignite-corp/ai-dev-pr-review/pull/170#discussion_r4042367233) |
| `docs` 의 *"a PR cannot rewrite the instructions its own reviewers read"* | 온보딩 폴백(§1-7). **Actions 쪽에 대해서도 처음부터 거짓** | [#172 T27](https://github.com/ignite-corp/ai-dev-pr-review/pull/172#discussion_r4056181314) |
| `docs` 의 *"the only API key the driver reads from the environment"* | §1-6. **PR 밖에서 운영자 안내를 틀리게 만들었다** | [#172 T35](https://github.com/ignite-corp/ai-dev-pr-review/pull/172#discussion_r4056633045) |
| `docs` 의 *"the restore refuses to merge or overwrite it"* | 코드는 경고 후 버리고 보관본을 복원. **같은 문서가 뒤에서 자기와도 어긋났다** | [#170 T17](https://github.com/ignite-corp/ai-dev-pr-review/pull/170#discussion_r4002054821), [T20](https://github.com/ignite-corp/ai-dev-pr-review/pull/170#discussion_r4002071262) |
| `local_review_config` docstring 의 *"`ALLOW_AUTO_APPROVE` … the driver pins it off"* | 이 모듈은 강제하지 않는다(§2-H) | [#171 T11](https://github.com/ignite-corp/ai-dev-pr-review/pull/171#discussion_r4042226187) |
| `agent_config_targets` 의 *"리뷰어 CLI 가 읽으면 안 되는 모든 경로"* | CLI 가 무엇을 읽는지 알 수 없다 → *"에이전트 설정 목록에 맞는 경로"* 로 좁힘 | [#170 R10 감사](https://github.com/ignite-corp/ai-dev-pr-review/pull/170#issuecomment-5744547755) |
| `RUN_ARTIFACTS` 주석의 *"a stale verdict from the previous run is never read as this run's output"* | shim 의 상수와 이름이 갈리면 정확히 그 일이 난다(R12, 미해결) | [#172 R12](https://github.com/ignite-corp/ai-dev-pr-review/pull/172#issuecomment-5749114728) |

**#170 R10 에서 총칭 감사가 한 번 돌았다 — 이것이 이 부류에 대한 유일한 체계적 대응이다.**
새 모듈 다섯에서 "every/never/all/모든/언제나"를 **열 개** 뽑아 네 칸으로 분류했다:
이미 검사됨 5 / **주장을 검사로** 2 / **문장을 코드에 맞춤** 1 / **코드를 문장에 맞춤** 1.
*"감사의 값은 세 번째·네 번째 칸에 있었다. 계속 틀리던 것들은 어떤 테스트도 실패시킬 수 없는
문장들이었고, 그래서 쓸모 있는 규칙은 '주장을 적게 써라'가 아니라 이것이다 — **주석 속의 총칭
한정사는 아직 안 쓴 테스트다. 쓸 수 없으면 그 한정사가 틀린 것이다.**"*
[#170 R10 응답](https://github.com/ignite-corp/ai-dev-pr-review/pull/170#issuecomment-5744547755)

**그리고 리뷰어가 이 저장소의 문장으로 이 저장소의 코드를 기소한 것이 세 번이다** —
`DirectWrite` 의 "One value, one test", `_CONFIG_READ_ERRORS` 의 "Everything … can raise",
그리고 run-log 복구의 *"discarding it … is the strictly worse half of the choice"*(그 논거가 종료 코드에
의존하지 않는데 코드는 의존했다)
([#172 T32 답글](https://github.com/ignite-corp/ai-dev-pr-review/pull/172#discussion_r4056671289)).

### 4-C. **스택 위쪽 PR 에서 고치고 아래쪽에 없다** — 아래쪽이 먼저 머지된다. **다섯 번.**

| # | 수정 | 상태 |
|---|---|---|
| 1 | `clean_artifacts` 순서(정리 → 체크아웃) | #170 에만. **#172 에서 "Fixed" 로 닫힌 적 있음** |
| 2 | `clone_origin` 호스트 대조 | #170 에만. **#172 에서 "Fixed" 로 닫힌 적 있음** |
| 3 | `run()` 의 타임아웃 → `DriverError` 변환 | #170 에만 |
| 4 | 심링크 `rmtree` 크래시 | #170 에만. **#172 에서 "Fixed" 로 닫힌 적 있음** |
| 5 | 리뷰어 타임아웃 + 프로세스 그룹 킬 | #170 에만 — **리뷰어가 찾았다**(저자의 두 축은 못 봄) |

**1번의 기전이 특히 기록할 값이 있다 — 측정됨.** #170 에서 이 수정은 한 줄 순서 바꾸기가 아니었다.
커밋 `8c87444` 가 재사용 체크아웃을 일회용 worktree 로 바꿨고 **올바른 순서는 그 재구조화에서 떨어져
나온 부산물**이었다. 그래서 **역이식할 한 줄이 존재하지 않았고, 역이식할 것이 있다는 사실 자체를
아무도 눈치채지 못했다.** 이력 전수 확인:

```
9cee8e6  [#172, 현재]     823: clean_artifacts -> 824: checkout_head      (틀림)
8c87444  [#170, 현재]    1345: create_review_worktree -> 1346: clean_artifacts  (맞음)
40900bd  [#172, R6 스쿼시] 727: clean_artifacts -> 728: checkout_head      (틀림)
d0b8ea5  [#170, 이전]    1167: create_review_worktree -> 1168: clean_artifacts  (맞음)
72f7ef4  [#172, 최초]     647: clean_artifacts -> 648: checkout_head      (틀림)
```
[#172 T26 답글](https://github.com/ignite-corp/ai-dev-pr-review/pull/172#discussion_r4056227665)

**셋은 앞선 라운드에 #172 에서 "Fixed" 로 공개 답변됐다.** 그 답은 **작업이 이루어진 브랜치에 대해서는
참이었고, 지적이 제기된 브랜치에 대해서는 거짓**이었다. 스택은 이 실패를 싸게 만들고, 리뷰어가 같은
줄을 재제기하기 전까지 보이지 않게 만들며, **아래쪽이 먼저 머지되므로 `main` 이 그 구멍을 먼저 받는다.**

찾아낸 방법이 기록돼 있다: *"care 가 아니라 method — 이력에서 파일의 모든 판을 열거하고, 앞선 축이
실제 사례를 놓친 것이 증명된 뒤에 고른 축으로 두 브랜치를 비교."* 두 축은 (1) 함수별 예외 핸들러,
(2) 같은 이름 함수의 **본문 차이**(첫 축이 분기 순서를 못 보기 때문 — codex 의 심링크 major 가 그것을
증명했다). **두 축 모두 `review_pr_local.py` 안에서만 본다**(§3-1).

### 4-D. **한 부류의 한 인스턴스만 고치고 형제를 남긴다**

| 사례 | 지적된 자리 | 실제 자리 |
|---|---|---|
| `isinstance(payload, dict)` 가드 | 두 shim | **셋** — `normalize_verdict_file` 도 배열에서 `setdefault` 로 깨졌다 |
| 타임아웃 vs 미설치 구분 | claude shim | **codex shim 은 그대로** → 다음 라운드 major. 나란히 놓고 훑으니 **둘이 더** 나왔다(codex 타임아웃, 두 shim 의 spawn 실패, `allowed_tools()` 위치) |
| `re.match` 끝 앵커 | `_INPUT_REF_RE` | R12 에 `REPO_RE.match` 가 **같은 모양으로 남았다**(미해결) |
| 가드 없는 `json.loads` | `existing_threads_block` | 드라이버의 `load_threads` 에도 있었고 거기 `len()` 은 dict 까지 조용히 받았다 |
| `TimeoutExpired` 가 `DriverError` 가 아님 | `clone_origin` (주석이 이미 그 결함을 적고 있었다) | **모든 호출이 지나가는 `run()` 은 그 주석이 묘사하는 모양 그대로** |
| 맨 `subprocess.run` | (지적되지 않음) | R12 에 **넷**: `tracked_artifact_names` · `_prompt_text` · `append_prior_context` · `resolve_bot_login` |
| 정적 분석 경고 묶음 | — | **묶음의 하나를 확인하고 나머지를 같이 넘김 — 두 번** (`result` possibly-unbound, `_populate` is not accessed) |

출처: [#172 T10](https://github.com/ignite-corp/ai-dev-pr-review/pull/172#discussion_r4042331742) ·
[T20](https://github.com/ignite-corp/ai-dev-pr-review/pull/172#discussion_r4054145834) ·
[T33](https://github.com/ignite-corp/ai-dev-pr-review/pull/172#discussion_r4056671337) ·
[R7](https://github.com/ignite-corp/ai-dev-pr-review/pull/172#issuecomment-5746491861) ·
[#170 T35](https://github.com/ignite-corp/ai-dev-pr-review/pull/170#discussion_r4042367226) ·
[#170 T40](https://github.com/ignite-corp/ai-dev-pr-review/pull/170#discussion_r4054183764) ·
[R12](https://github.com/ignite-corp/ai-dev-pr-review/pull/172#issuecomment-5749114728)

### 4-E. **같은 결함이 다른 문으로 돌아온다** — 보여진 `raise` 를 닫으면 다음 `raise` 가 연다

§1-3 의 여덟(#170) · 일곱(#172). 별도 부류로 적는 이유는 **대응이 달라야 했기 때문**이다 —
점 수정이 반복해서 실패한 뒤에야 구조(경계 + AST 검사 + 원천 변환)로 갔다.
*"매번 나타난 자리에서 닫았고, 다음 `raise` 자리가 다시 열었습니다. '모든 경로가 집계에 닿는다'를
편집할 때마다 손으로 다시 세우고 있었고, 기록이 그게 유지되지 않는다고 말합니다."*

**하위 모양 하나:** 이 PR 이 **자기 수정으로 자기 회귀를 만든 것**이 여러 번이다 —
넣은 타임아웃이 두 라운드 전의 major 를 되살렸고("영원히 멈춘다"를 "판정 없이 죽는다"로 바꿈,
[#170 T32](https://github.com/ignite-corp/ai-dev-pr-review/pull/170#discussion_r4042367221)),
넣은 예외 경계가 진행분을 떨어뜨렸으며(§1-3), R7 지적 다섯 중 **둘이 R6 에서 만든 회귀**였고
하나는 *"그 규칙을 세우려고 만든 함수가 그 규칙을 자기에게 적용하지 않은 것"* 이었다
([#172 R7](https://github.com/ignite-corp/ai-dev-pr-review/pull/172#issuecomment-5746491861)).
*"못 읽는 것을 못 읽었다고 말하려고 만든 장치가 자기 경로를 안 덮는"* 것이 한 PR 에서 두 번.

### 4-F. **결과는 맞고 기전은 틀린 지적** — 그리고 **받아쓰지 않고 잰 것**이 방어를 구했다

**이 부류는 리뷰어 쪽 결함이고, 대응이 이 프로젝트의 자산이다.** 확인된 넷:

| 지적 | 결과 | 기전 | 처분 |
|---|---|---|---|
| `url.<base>.insteadOf` 가 `git remote get-url origin` 을 속인다 | **결론 성립**(설정 키가 전송을 돌린다 — `http.proxy` 등) | **틀림** — git 2.43 에서 `remote get-url` 이 **재작성된 URL 을 보고**해 신원 검사가 잡는다 | 결론 수용, 문장 거절. *"이걸 재지 않았으면 멀쩡히 작동하는 방어를 못 믿고 다시 만들 뻔했습니다"* |
| `run()` 이 타임아웃 없이 `subprocess.run` 을 시작한다 | **결과 실재**(멈춘 fetch 가 운영자에게 나쁘게 끝난다) | **틀림** — `run()` 에는 **처음부터** `timeout=_GIT_TIMEOUT_SEC` 이 있었다(실측: 1초에 `TimeoutExpired`). 실제 결함은 그것이 `DriverError` 가 아니라 traceback 으로 끝나는 것 | 결과 수용, 원인 수정 안 함 — *"없는 것을 고칠 수는 없으니까"* |
| `${{ inputs.x }}0` 가 앵커 없는 `match()` 를 통과한다 | **결론 성립**(앵커링 필요) | **예가 틀림** — `}}0` 은 이미 거절됐다(`$` 는 문자열 중간에서 매치하지 않는다). **진짜 구멍은 `}}\n`** — `$` 가 끝 개행 직전에 매치하고 YAML folded scalar 가 그런 값을 routine 하게 만든다 | 수정하되 **"틀린 이유로 옳은 수정"이라고 적어 둠** |
| `AST` 로 기본값 집을 세라(`ast.Dict` 에서 키 찾기) | **방향 성립** | **구현이 틀림** — 제안 안의 예 `dict(POLICY_SKIPPED=...)` 가 `ast.Dict` 가 아니라 `ast.Call` 이라 안 걸리고, 키만 보면 `policy_gate`·`size_gate` 에서 **과다 매칭**한다 | 키+상수값으로, `{}`·`dict()`·`dict.fromkeys()` 전부 받게. 그리고 **각 기본값을 소유 함수에 묶음** |

출처: [#172 T31](https://github.com/ignite-corp/ai-dev-pr-review/pull/172#discussion_r4056671234) ·
[#170 T43](https://github.com/ignite-corp/ai-dev-pr-review/pull/170#discussion_r4054318844) ·
[#172 T24](https://github.com/ignite-corp/ai-dev-pr-review/pull/172#discussion_r4056227817) ·
[#170 R11](https://github.com/ignite-corp/ai-dev-pr-review/pull/170#issuecomment-5746468375)

**반대 방향도 있다 — 저자 쪽 기전 오독.** 라운드를 팀에 전달하며 **집계 코멘트를 `head -20` 으로 잘라
읽었고 codex 섹션이 21줄부터였다.** 헤드라인의 `Major issue consensus` 를 읽고도 *무엇과 무엇 사이의
합의인가*를 묻지 않았다 — **두 리뷰어가 같은 파일에서 서로 다른 major 를 냈다**는 뜻이었는데 하나로 읽었다
([#172 T30 답글](https://github.com/ignite-corp/ai-dev-pr-review/pull/172#discussion_r4056671178)).
§4-A 와 같은 모양이다.

### 4-G. 그 밖에 반복된 작은 모양들

- **소스를 텍스트로 읽는 단언.** `"ThreadPoolExecutor" not in source` 는 **주석과 산문에도 걸리고
  동작은 보지 않는다** — 그 파일에 왜 없는지 설명하는 문장만 써도 실패한다. 지웠다
  ([#170 T22](https://github.com/ignite-corp/ai-dev-pr-review/pull/170#discussion_r4002071267)).
  **그런데 같은 PR 이 몇백 줄 뒤에서 `source.count(...)` 로 같은 기법을 다시 썼다**
  ([#170 R11 컷오프](https://github.com/ignite-corp/ai-dev-pr-review/pull/170#issuecomment-5744606999)) —
  양방향 실패가 **측정됨**(주석 추가로 초록이 깨지고, 다르게 적은 진짜 두 번째 집은 통과).
- **사람이 읽는 자리와 기계가 읽는 자리가 다른 말을 한다.** stderr 에는 *"the claude CLI could not be
  started: {exc}"* 를 정확히 찍고 종료 코드는 **미설치**를 돌려준다. 리뷰어가 이 부류를 이름해 줬다 —
  *"정확히 경고하고 부정확하게 코드를 돌려준다"*
  ([#172 T23 답글](https://github.com/ignite-corp/ai-dev-pr-review/pull/172#discussion_r4054146201)).
  같은 자리에서 `action.yml` **부재**의 `FileNotFoundError` 가 *"the CLI is not installed"* 로 보고돼
  운영자를 **애초에 원인이 아닌 CLI 설치로** 보냈다.
- **테스트 이름이 본문이 낼 수 없는 총칭을 약속한다.** 두 shim 의
  `test_every_failure_path_leaves_a_verdict_file` 은 `test_every_cli_output_shape_...` 로 개명됐고,
  약속하던 범위는 **진짜 `run_cli` 를 쓰는** 아홉-케이스 테스트로 새로 썼다(커밋된 코드에서 **9 중 4 실패**).
- **거부해야 할 열거를 허용으로 쓴다** — §1-4. 별개 부류가 아니라 4-A 의 특수형이지만,
  **한 토큰 차이**라는 점에서 따로 적을 값이 있다.
- **PR 이 두 번 크기 한도를 넘어 리뷰가 스킵됐다**(3291·3196·3007줄 / 한도 3000). 매번 자기 도구가
  자기를 막았다: [#170](https://github.com/ignite-corp/ai-dev-pr-review/pull/170#issuecomment-5645871551) ·
  [#172](https://github.com/ignite-corp/ai-dev-pr-review/pull/172#issuecomment-5746497121) ·
  [#170 재발](https://github.com/ignite-corp/ai-dev-pr-review/pull/170#issuecomment-5746478032).
  마지막 라운드가 **삭제가 있는 첫 라운드**였고(+192, 직전 둘은 +324·+344), 그 감소는 `main()` 추출이
  줄을 **옮겼기** 때문이지 줄인 것이 아니다.

---

## 5. 한 번의 실제 실행이 보여 준 것

폐기 전에 드라이버를 **실제로 한 번 돌렸다.** §1~§4 와 **성질이 다른 자료**이므로 그 안에 섞지 않고
따로 세운다 — 앞의 넷은 *리뷰어가 코드를 읽고 찾은 것*이고, 이 절은 *코드가 돌 때 실제로 일어난 것*이다.
원자료는 `/tmp/lens-harvest/`(드라이버 로그 전문 · 보존된 run 디렉터리 · 실행 스크립트)에 있고,
아래 숫자는 전부 **그 원자료에서 직접 다시 측정**했다.

### 5-0. 무엇을 돌렸는가, 그리고 이 절이 증명하지 못하는 것

| | |
|---|---|
| 대상 | `ignite-corp/ai-dev-pr-review` PR **170** (열한 라운드를 거친 그 PR) |
| 결과 | 드라이버 종료 **0**, 벽시계 **399초**, 리뷰어 3/3 성공, 최종 판정 `approve` |
| 실행 순서 | gemini(21:31) → codex(21:35) → claude(21:37) — 로그 mtime 기준, **한 번에 하나**(§1-11 과 일치) |
| 비용(claude 만) | `total_cost_usd` **1.27419**, `duration_api_ms` 380661 |

**먼저 이 절의 가장 중요한 단서.** 돌아간 드라이버는 **#172 의 head 와 바이트 동일**하다:

```
run 에 쓰인 review_pr_local.py   sha256 217da9413de294a523ef345f13d52c51c0c7d26f6cc0ca6e60236c8ab52252a7
#172 head e9592d8 의 같은 파일   sha256 217da9413de294a523ef345f13d52c51c0c7d26f6cc0ca6e60236c8ab52252a7   <- 일치
#170 head ce8c324 의 같은 파일   sha256 89a5a158f041258587547ecdc58d725de8c92ad987c5ed31fa8e93170f261719
```

`strip_agent_config` · `create_review_worktree` · `quarantine_agent_config` 가 **전부 0회 등장**한다.
그러므로 **이 실행은 §1-1(일회용 worktree)과 §1-2(에이전트 설정 삭제)를 시험하지 않았다** — 리뷰어들은
저장소의 실제 `.claude/` · `AGENTS.md` 가 **그대로 있는 클론 안에서** 돌았다. §3-1 의 다섯 방어도 전부
없는 판이다. **스택의 아래쪽 PR 이 실제로 무엇을 의미하는지가 실행으로 한 번 더 확인된 셈이다**(§4-C).

**실행 조건이 기본값이 아니었다. 그 두 가지가 필요했다는 사실 자체가 발견이다:**

1. `PR_SIZE_LIMIT=20000` 을 export 했다. PR 이 6109줄이고 워크플로 기본값은 3000 이라, 그러지 않으면
   **크기 스킵으로 리뷰가 아예 안 돈다.** 이 PR 은 자기 도구에 세 번 막힌 적이 있다(§4-G).
2. `--system-prompt-path` · `--checklist-path` 를 **명시로 넘겨야 했다**(§5-9).

**이 절이 증명하지 못하는 것 — 한 번의 실행은 한 번의 실행이다.**
발동하지 않은 가드가 **불필요하다는 증명이 아니다.** 한 번의 정상 경로가 밟지 않았다는 사실일 뿐이고,
그 가드들이 겨냥하는 상황(CLI 부재·행·malformed 출력·중도 크래시)은 이 실행에 없었다. 아래 회계는
*"이 코드는 쓸데없다"* 가 아니라 *"이 코드의 어느 쪽이 실제 사용으로 접근되는지"* 에 대한 **한 점의 관측**이다.

**계측 오류 하나를 먼저 적는다.** 로그의 타임스탬프는 **전부 쓸모없다.** 드라이버 출력을
`awk '{printf strftime(...)}'` 로 찍었는데, `fflush()` 를 불렀음에도 **mawk 가 입력 쪽을 블록 버퍼링**해서
모든 줄이 종료 시각 하나로 찍혔다. 따로 실험해 확인했다:

```
2초 간격으로 flush 하며 낸 세 줄:
  21:41:07 line 0 1789908061.394278
  21:41:07 line 1 1789908063.3943787      <- 2초 뒤에 나온 줄인데 같은 시각
  21:41:07 line 2 1789908065.3946183
```

그래서 단계별 소요는 **로그가 아니라 파일 mtime** 으로 읽었다. **측정 도구가 틀릴 수 있다는 것이
이 문서의 논지 중 하나이고**(§4-A), 이번엔 측정하는 쪽이 그랬다.

### 5-1. 가드 회계 — 41개 중 **1개**가 발동했다

새 세 모듈의 실패 대응 지점을 AST 로 전수 셌다(주석·docstring 안의 문자열은 0건, 전부 실제 방출 지점):

| 모듈 | `print(::warning::)` | `raise DriverError(...)` | 줄 수 |
|---|---|---|---|
| `review_pr_local.py` | 9 | 17 | 1124 |
| `review_claude_local.py` | 7 | 0 | 397 |
| `review_codex_local.py` | 8 | 0 | 369 |
| **합계** | **24** | **17** | **1890** |

**두 번의 기동을 합쳐 발동한 것은 정확히 1개다:**

- 프로브 실행 — `_prompt_text` 의 `DriverError` 1개(§5-9). 종료 코드 1.
- 성공 실행 — `::warning::` **0개**, `DriverError` **0개**. `run.log` · `run-outer.log` · 세 리뷰어 로그
  어디에도 `::warning::` 이 없다. (`codex-run.log` 에 63건이 보이지만 전부 `{name}` · `{exc}` 가
  **치환되지 않은 채**인 문자열 — codex 가 리뷰 대상 **소스를 인용한 것**이지 발동이 아니다.)

**값을 한 가드가 어느 쪽인지가 이 회계의 요점이다.** 발동한 1개와, 발동하지는 않았지만 이번 실행이
**실제로 평가한** 것들(클론 지문 대조 · 추적 산출물 스캔 · `lens-ignore` 확인 · codex 프롬프트 크기 검사)은
**전부 "운영자 환경이 틀린 것"** 에 대한 가드다. 반면 **"CLI 가 오작동하는 것"** 에 대한 가드 —
부피로 새 코드의 큰 절반을 차지하는 모든 폴백 사슬 · malformed 판정 경로 · 타임아웃 · 종료 코드 구분
(`_EXIT_NOT_INSTALLED` · `_EXIT_TIMED_OUT` · `_EXIT_SPAWN_FAILED` · `_EXIT_CONFIG_UNREADABLE` ·
`_EXIT_PROMPT_TOO_LARGE`) — 은 **어느 방향에서도 접근되지 않았다.** 세 CLI 모두 정상 종료했고,
셋 다 판정 파일을 **직접 썼다**(`review-{name}.json written directly by the CLI`), 즉 run-log 복구
경로도 레거시 이름 승격도 밟히지 않았다. — **측정됨**

> 세어 보기 전에 전달받은 수치는 *"`::warning::` 23개 · `DriverError` 20개"* 였다. **다시 세어
> 24 / 17 이 나왔고, 이 문서는 다시 센 쪽을 싣는다.** 요약을 출력 대신 읽지 않는 것이 §4-A 이고,
> 그 규칙은 전달받은 요약에도 적용된다.

### 5-2. 닿지 않았지만 **가까웠던** 둘 — 발동 안 함과 멀리 있음은 다르다

추측처럼 보이던 두 상한의 실제 여유를 쟀다:

```
codex-prompt.md                  83,579 bytes  =  63.8% of MAX_ARG_STRLEN(131,072)
"EXISTING_COMMENTS=" + 값 전체    28,189 bytes  =  21.5% of 131,072   (스레드 45개)
```

argv 쪽은 **한도의 3분의 2 를 쓰고 있다.** 이 PR 은 6109줄이고 `PR_SIZE_LIMIT` 기본값은 3000 이므로,
**한도를 지킨 더 큰 PR** 이나 스레드가 더 쌓인 PR 이면 닿는다 — §2-C 에서 *"한도를 넘지 않은 큰 PR 이
여기서 막힌다"* 고 적은 것의 실측 뒷받침이다. 두 가드는 **발동하지 않았지만 추측이 아니었다.** — **측정됨**

### 5-3. **정작 일어난 실패는 가드 목록 어디에도 없다** — 줄 번호

세 리뷰어가 낸 4건 중, PR 에 **인라인으로 닿은 것은 1건**이다. 나머지가 어떻게 됐는지가 이 절의 핵심이다.

| 리뷰어 | 지목한 위치 | 실제 | 결말 |
|---|---|---|---|
| claude #1 | `review_pr_local.py:2268` | 파일은 **1687줄** — EOF 밖 | 범위 검사가 버림 |
| claude #2 | `review_codex_local.py:654` | 파일은 **365줄** — EOF 밖 | 범위 검사가 버림 |
| gemini | `review_pr_local.py:1240` | 범위 **안**, 그러나 틀린 줄 | 통과 → R13 컷오프로 접힘 |
| codex | `review_pr_local.py:742` | **정확** | **인라인 게시됨** |

**claude 의 두 줄 번호는 `pr.diff` 오프셋이다. 2건 중 2건.** 확인했다:

```
pr.diff:2268  ->  +def review_pr(                 <- claude 가 말한 바로 그 함수
pr.diff:654   ->  +    extract = subprocess.run(   <- claude 가 말한 바로 그 호출
```

즉 **claude 의 발견 자체는 옳았고 위치도 옳았다 — diff 좌표계에서.** 파일 좌표계로 옮기는 일이
아무 데서도 일어나지 않았다. 범위 검사가 둘 다 잡아냈고(`claude: 2 issue(s) outside diff`),
`claude: no inline comments to post` 로 끝났다.

**gemini 는 반대쪽으로 실패했다.** `review_pr` 이 1240–1418 줄이라고 적었는데, 실제 `review_pr` 은
**1500–1632**(133줄)이고 1240 은 `aggregate_env` 의 딕셔너리 한가운데다. 그런데 **이 PR 은 파일이
통째로 추가라 모든 줄이 "diff 안"** 이므로 범위 검사가 통과시켰다. **범위 검사는 줄이 diff 안에 있는지만
보지, 그 줄이 말한 내용과 맞는지는 보지 않는다.**

세 리뷰어 중 **둘이 같은 진짜 결함**(`review_pr` 이 80줄 한도를 넘는다)을 독립적으로 찾았는데,
하나는 좌표계가 달라 버려지고 하나는 틀린 줄에 달려 접혔다. **claude 의 길이 추정(≈130줄)이
실제(133줄)에 맞고, gemini 의 추정(179줄)이 틀렸다** — 버려진 쪽이 더 정확했다.

**4000줄 어디에도 이 부류를 지켜보는 코드가 없다.** 가드는 *"판정이 안 나온다"* · *"CLI 가 죽는다"* ·
*"설정이 틀렸다"* 를 겨냥해 쓰였고, 실제로 일어난 것은 **판정이 정상적으로 나왔는데 그 안의 좌표가
쓸 수 없는 것**이었다. — **측정됨**

> **§6 에 주는 결론:** 우리는 **상상한 실패 모드에 코드를 썼고, 실제 실패 모드는 아무도 상상하지
> 않았다.** 41개 중 1개라는 회계가 그 문장을 받친다.

한 가지 정확히 해 둘 것: 버려진 것은 **인라인 게시**이지 발견 전체가 아니다. 집계 판정 코멘트는
claude 의 두 항목을 `:2268` · `:654` 라는 **쓸 수 없는 줄 번호 그대로** 본문에 싣는다
([판정 5749842804](https://github.com/ignite-corp/ai-dev-pr-review/pull/170#issuecomment-5749842804)).
사람은 읽을 수 있고, 기계는 못 쓴다.

### 5-4. codex 가 Actions 열한 라운드가 못 찾은 진짜 결함을 찾았다

`_prompt_text` 의 베이스 브랜치 폴백이 **`git show` 의 모든 비-0 종료**에서 PR head 를 읽는다.
일시적 git 실패 하나면 **PR 작성자가 자기 리뷰어의 시스템 프롬프트를 공급한다.** §1-7 에 단서로 붙였다.
[인라인 4056951799](https://github.com/ignite-corp/ai-dev-pr-review/pull/170#discussion_r4056951799) —
`review_pr_local.py:742`, **파일 좌표계로 정확**했고, 그래서 **유일하게 게시된 항목**이다. — **측정됨**

### 5-5. 모델이 조용히 갈린다 — **셋 중 둘**

드라이버는 워크플로의 `vars.X || '기본값'` 에서 **기본값**을 파싱하고 org/repo `vars` 는 보지 않는다.

```
claude-run.log 의 modelUsage:  claude-sonnet-4-6   <- 실제로 돈 모델
base-ai-review-single.yml:257  vars.CLAUDE_MODEL || 'claude-sonnet-4-6'
org variable CLAUDE_MODEL   =  claude-opus-5       <- CI 가 실제로 쓰는 값
org variable GEMINI_MODEL   =  gemini-3.5-flash-lite  (워크플로 기본값은 gemini-2.5-pro)
org variable CODEX_MODEL    =  gpt-5.6-luna        (기본값과 같음 — 유일하게 안 갈림)
```

**출력 어디에도 그 사실이 없다.** claude 의 모델은 `claude-run.log` 를 열어야 알 수 있고, gemini 는
자기 로그에 모델을 찍지 않아 **이 실행에서 어떤 모델이었는지 관측되지 않았다**(기본값 해석 경로로
`gemini-2.5-pro` 일 것이라는 것은 **논증됨**, 관측은 아니다). §2-J 에 미결 질문으로 세웠다.
— claude 쪽 **측정됨**, gemini 쪽 **논증됨**

### 5-6. 리뷰어 CLI 가 **이 기계에서 PR 의 테스트를 수집·실행**했다

보존된 run 디렉터리에 codex 가 남긴 것:

```
.pytest_cache/v/cache/nodeids   232개 수집 ID, 파일 셋 전부 이 PR 이 추가한 것:
   .github/scripts/tests/test_review_claude_local.py
   .github/scripts/tests/test_review_codex_local.py
   .github/scripts/tests/test_review_pr_local.py
.ruff_cache/0.14.11/…           실제 캐시 엔트리 2개
```

**수집만이 아니라 실행이다.** 캐시만으로는 `--collect-only` 와 구분되지 않아 codex 의 실행 기록을
확인했고, 거기에 결과가 남아 있다:

```
codex-run.log:  232 passed in 3.48s
                ruff check .github/scripts/review_claude_local.py \
                           .github/scripts/review_codex_local.py \
                           .github/scripts/review_pr_local.py ...
```

**232 는 캐시된 수집 ID 수와 정확히 같다.** 즉 PR 작성자가 쓴 테스트 232개가 운영자의 기계에서
실제로 실행돼 전부 통과했다.

둘 다 **자기 `.gitignore` 에 `*` 를 담고 있어** `git status` 에 나타나지 않고, **`RUN_ARTIFACTS` 에
없어서 다음 실행의 `clean_artifacts` 가 지우지 않는다.**

이것은 §3-4 가 *"CLI 는 운영자로, 운영자의 자격으로 돈다 — 리뷰 대상 코드를 실행하지는 않지만 CLI 가
할 수 있는 일을 막는 것은 없다"* 고 **미측정으로 적어 둔 항목의 측정된 판본**이다. 같은 절이
*"`.codex/` 는 프로브하지 못했고 보수적 추정"* 이라고 적은 바로 그 자리이기도 하다.

> **이 항목의 지위를 정확히 적는다 — 완화책이 있는데도 뚫린 것이 아니다.**
> 이 실행은 §5-0 이 보인 대로 **#172 의 헤드**, 즉 §1-1 의 일회용 worktree 도 §1-2 의
> `strip_agent_config` 도 **없는 빌드**였다. 그러므로 여기서 확인된 것은
> *"완화책이 있는 드라이버에서 codex 가 테스트를 실행했다"* 가 **아니라**
> *"완화책이 없는 빌드에서 실행됐다"* 이다. 둘은 전혀 다른 주장이고, 이 구분 없이 인용되면
> 완화책이 무력하다는 **틀린 결론**이 나온다.
>
> **그리고 완화책이 이 위협에 실제로 듣는지는 여전히 미측정이다.** worktree 가 매 실행 새로
> 만들어지므로 캐시가 다음 실행까지 남지 않을 것이라는 것은 **논증**이고, 관측이 아니다 —
> 그리고 `strip_agent_config` 는 에이전트 **설정**을 지우지 `.pytest_cache` 를 지우지 않으므로
> *그 실행 안에서* 테스트가 도는 것 자체는 어느 완화책도 막지 않는다. **재작성이 측정해야 할
> 목록에 올린다**(§6-12 의 "먼저 한 번 돌려 보라"가 이 경우를 포함한다).
> — **측정됨**(캐시의 존재·내용·`232 passed`) / **미측정**(완화책의 효과)

### 5-7. 라운드 카운터는 로컬 실행을 **세지 않는다**

로그는 `round cutoff active (round 13)` 을 찍었다 — **1 이 아니다.** 기전을 확인했다.
`post_inline_comments.fetch_round_count` 는 `.user.type == "Bot"` **이면서** `REVIEW_MARKER` 를 담은
코멘트만 센다. PR 170 에는 Actions 가 만든 그런 코멘트가 이미 12개(+ reviews 엔드포인트 1) 있어서 13 이 됐다.

**로컬 실행의 판정은 세어지지 않는다.** 이번 실행이 남긴 판정 코멘트는:

```
5749842804  user=hyuk-hur  type=User  REVIEW_MARKER 있음  -> 카운트에 안 잡힘
```

즉 **Actions 이력이 있는 PR 에서는 카운터가 그 이력만큼 읽히고, 드라이버가 존재하는 이유인
저장소(Actions 가 닿지 않는 곳)에서는 아무리 돌려도 0 에 머문다** — 수렴 컷오프가 영원히 발동하지
않는다. 설계 결함이고 §6 행이다. — **측정됨**

> 전달받은 서술은 *"로컬에서 영원히 1"* 이었다. 관측값은 **13** 이고, 참인 명제는 *"로컬 실행은
> 카운터를 올리지 않는다"* 다. 결과는 맞고 기전과 수치가 달랐다 — §4-F 의 모양이 이 절을
> 전달하는 과정에서 한 번 더 났다.

### 5-8. `BOT_LOGIN` 이 운영자라 **아무것도 접히지 않았다**

`BOT_LOGIN` 은 인증된 `gh` 로그인(`hyuk-hur`)으로 해석됐다(§1-12 의 의도된 기본값). 결과를 쟀다:

```
실행 전  이슈 코멘트 22개 중 minimized 11
실행 후  이슈 코멘트 24개 중 minimized 11      <- 기존 코멘트의 접힘 상태 변화 0건
새로 추가  5749842519(R13 컷오프) · 5749842804(판정)  둘 다 펼쳐진 채
```

이전 라운드들의 판정 코멘트는 전부 `github-actions[bot]` 이 썼으므로 `hyuk-hur` 로 맞추는 패스에
걸리지 않는다. **§1-12 가 이 기본값을 고른 이유가 "워크플로 기본값이면 실행마다 판정이 쌓인다"
였는데, Actions 이력이 있는 PR 에서는 이 기본값으로도 쌓였다.** 순수 로컬 PR 에서는 의도대로
동작할 것이다(두 번째 실행이 첫 번째를 접는다) — 그러나 **혼합 이력에서는 어느 쪽 값도 맞지 않는다.**

덧붙여 두 코멘트 모두 `## [bot] …` 라는 제목을 달고 **사람 계정에서** 나간다. — **측정됨**

### 5-9. 기본 프롬프트 경로가 이 저장소에 없다 — 전체 클론 **뒤에** 실패한다

첫 시도(기본 경로)는 이렇게 끝났다:

```
Cloning into '/tmp/lens-harvest/run/repo'...
 * [new ref]  refs/pull/170/head -> origin/pr/170
pr.diff: 6109 lines via open PR: git diff origin/task/local-review-driver-core...ce8c324
::error::neither origin/task/local-review-driver-core nor the PR head carries
         .github/prompts/code-review-system.md; pass --system-prompt-path /
         --checklist-path if this repository keeps its review prompts elsewhere
DRIVER_EXIT=1
```

**오류 메시지는 고칠 방법을 정확히 이름한다** — 그 점은 이 모듈이 내내 지향한 것이고 지켜졌다.
문제는 **순서**다: 클론 · fetch · 체크아웃 · diff 추출을 모두 끝낸 **뒤에야** 실패한다. 이 저장소는
프롬프트를 `examples/prompts/` 에 두고 `self-review.yml` 도 그 두 경로를 넘긴다 — 즉 **드라이버의
기본값이 자기 저장소에 대해 틀려 있다.** 인자로만 확인할 수 있는 조건을 네트워크 작업 뒤로 미룬
것이고, §6 의 "싼 검사를 먼저" 항목이다. — **측정됨**

---

## 6. 재작성이 다르게 해야 할 것

§1~5 에서 직접 따라 나오는 것만. 의견이 아니라 기록의 귀결이다.
**1~10 은 열한 라운드의 리뷰 기록에서, 11~15 는 한 번의 실제 실행(§5)에서 나온다.**

> §5 가 이 목록의 무게를 바꾼다. 리뷰 기록만 보면 이 코드의 문제는 *"실패 경로가 충분히 촘촘하지
> 않다"* 로 읽힌다 — 열한 라운드가 거의 전부 그것을 다뤘다. 실행을 한 번 해 보면 **실패 대응 지점
> 41개 중 1개가 발동했고, 정작 일어난 실패는 그 41개 어디에도 없었다.** 촘촘함이 부족했던 것이
> 아니라 **겨냥한 곳이 달랐다.**

1. **스택으로 나누지 말 것.** §4-C 가 **다섯 번** 났다 — 다섯 다 위쪽에서 고쳐져 아래쪽에 없었고,
   그중 **셋**은 아래쪽 PR 에서 "Fixed" 로 공개 답변까지 됐으며(그 답은 작업한 브랜치에 대해서만 참),
   **넷**은 저자의 두 축 감사가 찾고 **다섯 번째는 리뷰어가** 찾았다. 나눠야 한다면 **아래쪽부터 고치고 위로 리베이스**하거나,
   **두 브랜치의 같은 파일을 기계로 비교하는 검사를 CI 에 둘 것** — 그리고 그 비교가
   `review_pr_local.py` 안으로 제한되지 않게 할 것(§3-1).
   **게이트 쪽 사정이 이 권고를 더 무겁게 만든다**: 토픽 브랜치를 base 로 하는 PR 에서는 필수 체크
   `test` 도([AT-2412](https://ignitecorp.atlassian.net/browse/AT-2412)) self-review 도
   ([AT-2413](https://ignitecorp.atlassian.net/browse/AT-2413)) 돌지 않아 **스택 PR 이 리뷰 없이
   CLEAN 으로 보이고**, 머지된 head 브랜치가 안 지워져 base 자동 재타깃도 안 된다
   ([AT-2414](https://ignitecorp.atlassian.net/browse/AT-2414)). 셋 다 미해결이므로, 지금 스택을 쓰면
   §4-C 를 **사람이 손으로만** 잡아야 한다.
2. **불변식은 문장이 아니라 거부 기본값 검사로 쓸 것.** "한 실행은 판정을 낸다"는 라운드마다 손으로
   다시 세워졌다. `EXCEPTION_BOUNDARY` + `main()` 의 AST 검사 + `run()`/`gh_json` 의 **원천 변환**
   셋을 처음부터 넣고, **예외 조항은 이유와 함께 목록에 적을 것**.
3. **총칭 한정사를 쓸 때는 그 자리에서 테스트를 쓸 것.** 못 쓰겠으면 문장을 좁힐 것.
   §4-B 의 **열셋은 전부** 주석·docstring·문서가 코드보다 많이 약속한 것이고, 그중 하나는 운영자
   안내를 틀리게 했다(§1-6). *(초안은 "열셋 중 아홉"이라고 적었다 — 표를 다시 세어 고쳤다.)*
   #170 R10 의 **총칭 감사**(열거 → 네 칸 분류)를 **라운드마다가 아니라 설계 시점에** 한 번 돌릴 것.
4. **"검사가 돌았다"와 "그 검사가 이 경우를 볼 수 있다"를 항상 따로 확인할 것.** 새 테스트는
   **기준선에 대고 돌려 실제로 실패하는지**를 보고 나서 믿을 것 — 이 PR 들이 그렇게 해서 9건을 찾았다.
   `pyright`·`PLR0915`·`F841` 은 **이 저장소에서 약속한 것을 측정하지 않는다.** 재작성 전에
   `pyrightconfig.json` + CI 스텝을 세우고(AT-2418), 80줄 한도는 강제하는 도구를 붙이거나
   체크리스트에서 뺄 것(AT-2420). **둘 다 티켓은 있고 결정은 없다**(§2-K).
5. **한 질문에는 하나의 검사.** 판정 파일 존재는 `verdict_file_written()` 하나, 리뷰어 집합은
   `REVIEWER_NAMES` 하나, 산출물 이름은 shim 의 상수를 **import** 할 것(R12 미해결 항목).
   **규칙을 두 번 적는 것이 세 번째 자리를 못 보게 한다.**
6. **결함을 고칠 때 형제를 같은 목록으로 훑을 것.** §4-D 의 일곱 사례 전부 "지적된 하나 + 안 지적된
   n". 두 shim 을 나란히 놓는 것만으로 셋이 더 나왔다.
7. **worktree 는 유지하고, 장부는 만들지 말 것.** §1-1. 그리고 *"목적은 장부보다 오래 남는다"*
   주석을 옮길 것 — 그것이 없으면 다음 사람이 strip 을 불필요하다고 지운다.
8. **측정하지 않은 것은 측정하지 않았다고 목록에 적을 것.** strip 목록의 출처 표시(측정됨 / 플래그
   문구 / 미프로브) 규칙은 **그대로 가져갈 가치가 있다.** `CLAUDE.local.md` · `.codex` · `.cursor` ·
   `codex` CLI 전반은 **여전히 미측정**이고, CLI 를 올릴 때 재프로브가 필요하다.
9. **§3-6 의 여덟을 설계 단계에서 결정할 것 — 다섯이 아니라 여덟이다.**
   shim 중복(§2-A) · codex 프롬프트 전달(§2-C) · 타임아웃 쌍(§2-D) · **상대 경로 `--config`(§2-E)** ·
   마커 비대칭(§2-G) · `ALLOW_AUTO_APPROVE` 의 집(§2-H) · **엔터프라이즈 호스트(§2-I)** ·
   모델 갈림(§2-J). 전부 라운드 컷오프에 미해결로 남았거나 실측되지 않았고, 물려받으면 같은 라운드가
   반복된다.
   *(초안은 여기 다섯만 적어 2-E 와 2-I 를 빠뜨렸다. 이 목록이 재작성의 체크리스트로 쓰이므로
   빠진 항목은 조용히 상속된다 — #173 리뷰어가 §3-6 과 이 줄의 수를 대조해 찾았다. §2-J 는 §5 이후
   추가돼 양쪽 모두에 없던 것이고, 이번 재계수에서 함께 넣었다.)*
10. **Actions 경로의 실버그 둘(§3-3)은 재작성과 독립으로 고칠 것** —
    [AT-2424](https://ignitecorp.atlassian.net/browse/AT-2424)(High) ·
    [AT-2411](https://ignitecorp.atlassian.net/browse/AT-2411). 둘 다 **로컬 드라이버가 아니라
    운영 중인 Actions 경로**의 결함이므로, 드라이버를 다시 쓰든 안 쓰든 남는다.
    그리고 **발견을 산문 각주로 남기지 말 것**: 둘 중 티켓이 먼저 난 쪽은 약속이 지켜졌고, 나머지 하나는
    리뷰어의 명시적 요구에도 엿새를 산문으로 있다가 **이 문서를 만드는 과정에서야** 티켓이 났다.
    "의도한 기록"이 가리키는 곳은 **닫히는 PR 이 아니라 티켓**이어야 한다.

---

**아래 다섯은 §5 — 한 번의 실행 — 에서 나온다. 리뷰 기록만으로는 어느 것도 보이지 않았다.**

11. **좌표계를 계약으로 만들 것.** 이 실행에서 **네 건 중 세 건이 줄 번호 때문에 못 쓰게 됐다**(§5-3):
    두 건은 `pr.diff` 오프셋이었고(범위 검사가 버림), 한 건은 범위 안의 **틀린 줄**이었다(통과함).
    41개의 가드 중 이 부류를 보는 것은 **하나도 없었다.** 재작성은 리뷰어 출력의 `line` 이 *어느
    좌표계인지*를 계약으로 정하고, **diff 좌표 → 파일 좌표 변환을 드라이버가 수행**하거나 리뷰어에게
    파일 좌표를 강제할 것. 그리고 **범위 검사가 통과시킨 것이 옳다는 뜻이 아니다** — 전부 추가인
    파일에서는 모든 줄이 범위 안이므로 그 검사의 변별력이 0 이다. 최소한 *"지목한 줄이 인용한 코드와
    일치하는가"* 를 볼 것.
12. **상상한 실패가 아니라 관측된 실패에 코드를 쓸 것.** 가드 41개 중 **운영자 환경**에 대한 것만
    접근됐고, **CLI 오작동**에 대한 것 — 부피의 큰 절반 — 은 어느 방향에서도 닿지 않았다(§5-1).
    그 코드를 지우라는 뜻이 아니다(한 번의 실행은 한 번의 실행이고, §5-0 이 그 한계를 적는다).
    **먼저 한 번 돌려 보고 나서 어디를 촘촘하게 할지 정하라는 뜻이다** — 열한 라운드가 그 순서를
    반대로 했다.
13. **싼 검사를 네트워크 작업보다 먼저 할 것.** 기본 프롬프트 경로가 이 저장소에 대해 틀렸고,
    그 사실이 **전체 클론·fetch·체크아웃·diff 추출을 끝낸 뒤에** 드러난다(§5-9). 인자만으로 판정할 수
    있는 조건은 클론 앞에 둘 것. 그리고 **기본값이 자기 저장소에서 맞는지 테스트로 고정할 것** —
    `self-review.yml` 이 넘기는 두 경로가 이미 정답을 들고 있었다.
14. **실행 정체를 출력에 적을 것.** 어떤 모델로 돌았는지(§5-5 — 셋 중 둘이 CI 와 다르다), 어떤
    드라이버 판인지(§5-0), 라운드 카운터가 무엇을 셌는지(§5-7)가 **전부 출력에 없다.** 세 가지 다
    나중에 파일을 뒤져서야 알았고, 모델은 org `vars` 를 따로 읽지 않는 한 **알 수도 없다.**
    판정 코멘트에 한 줄이면 된다.
15. **로컬 실행을 세지 않는 카운터를 고칠 것.** `fetch_round_count` 가 `.user.type == "Bot"` 으로
    거르므로 **로컬 판정은 영원히 세어지지 않는다**(§5-7). Actions 가 닿지 않는 저장소 — 이 드라이버가
    존재하는 이유 그 자체 — 에서는 수렴 컷오프가 **절대 발동하지 않는다.** 같은 이유로 `BOT_LOGIN`
    기본값은 혼합 이력 PR 에서 아무것도 접지 못했다(§5-8). 둘 다 *"누가 썼는가"* 로 판정을 식별하는
    데서 오므로, **마커로 식별하고 작성자는 보지 않는 쪽**이 한 번에 닫는다.
