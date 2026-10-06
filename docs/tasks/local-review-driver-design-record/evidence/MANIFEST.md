# §5 의 증거 — 무엇이 여기 있고, 무엇이 없고, 없는 것을 어떻게 확인하는가

`design-record.md` §5 는 폐기 전 **한 번의 실제 실행**(2026-09-20, PR 170 대상)에 기댄다.
초안은 그 근거를 `/tmp/lens-harvest/` 하나로만 가리켰다. **`/tmp` 는 재부팅에 지워지므로**, 그대로
두면 §5 의 모든 수 — 가드 41 중 1, 프롬프트 83,579 B, 빌드 sha256, `232 passed` — 가 **독자가
다시 확인할 수 없는 주장**이 된다. 이 문서의 논지가 *"요약을 출력 대신 읽지 마라"* 인데
독자가 그 규칙을 이 문서에 적용할 수 없게 되는 것이라, 내구성 있는 부분집합을 여기 함께 커밋한다.

**전부 넣지는 않는다.** 큰 것은 크기와 sha256 만 남긴다 — 원본이 없어도 *어떤 바이트를 근거로 했는지*는
남고, 누가 같은 실행을 재현하면 대조할 수 있다.

## 들어 있는 것

| 경로 | 무엇 | §5 의 어느 주장 |
|---|---|---|
| `driver-output/run.log` · `run-outer.log` | 성공 실행의 드라이버 stdout/stderr 전문. `run.log` 는 안쪽 `tee` 의 것이고 그 16줄 전부가 `run-outer.log`(`START` + 같은 16줄 + 종료 코드 · 벽시계 · `END`) 안에 그대로 있다 — **같은 출력이 두 번 있는 것이지 두 실행이 아니다** | §5-1(경고 0건) · §5-3(2건 범위 밖) · §5-7(round 13) |
| `driver-output/probe.log` · `probe-exit.txt` | 기본 프롬프트 경로로 돈 첫 시도(종료 1) | §5-9 · §5-1(발동한 가드 1개) |
| `driver-output/run-exit.txt` | `DRIVER_EXIT=0` | §5-0 |
| `driver-output/lens-clone-fingerprint.txt` | `.git/config` 지문(§1-10) | §5-1(평가됐으나 발동 안 함) |
| `reviewer-output/review-{claude,codex,gemini}.json` | 세 판정 파일 원본 — 이름에 대해서는 아래 "판정 파일 이름에 대하여" | §5-3 의 줄 번호 넷 전부 |
| `reviewer-output/claude-run.log` | claude 실행 요약 | §5-0(비용·`duration_api_ms` 378,661 · `duration_ms` 380,819) · §5-5(`modelUsage: claude-sonnet-4-6`) |
| `reviewer-output/{gemini,codex,claude}-review.log` | 리뷰어 로그 | §5-1(세 CLI 모두 판정 직접 씀) |
| `scripts/run.sh` · `probe.sh` | **2026-09-20 에 실제로 돈 명령의 기록** — 비기본 조건 둘이 여기 보인다(`PR_SIZE_LIMIT=20000`, 명시 프롬프트 경로). 재현 레시피가 아니다 — 아래 "실행 스크립트에 대하여" | §5-0 |
| `scripts/watch.sh` · `wait.sh` | 실행 중 관측에 쓴 것 | — |
| `scripts/buftest.sh` · `buftest.log` | **계측 오류의 대조 실험** — 2초 간격 세 줄이 한 시각으로 찍힘 | §5-0(타임스탬프 무효) |

## 파생값은 어떻게 냈는가 — 손으로 다시 내는 방법

초안은 이 자리에 `scripts/measure.py` 를 두고 *"파생값 전부를 다시 계산한다"* 고 적었다. **AT-2425 로
지웠다.** 그 스크립트의 기록값 표 `EXPECTED` 는 `guards_that_fired: 1` 을 선언했는데 그 키를 계산하는
코드가 없었고, 계산되는 키(`warnings_emitted` · `errors_emitted`)는 `EXPECTED` 에 없어서, `main()` 이
둘을 `(not recorded)` 로 짝지어 플래그 없이 찍었다 — **기록값을 아무 수로 바꿔도 보고서가 깨끗했다.**
범위를 정확히 적으면: `EXPECTED` 의 16개 키 중 11개는 문서화된 인자(`--driver` · `--scripts-dir` ·
`--run-dir`)를 다 주면 실제로 비교된다. 어긋난 것은 선언만 된 다섯(`guards_that_fired` · `max_arg_strlen` ·
`wall_seconds` · `driver_sha256_pr170_head_ce8c324` · `driver_sha256_pr172_head_e9592d8`)과 계산만 되는 넷
(`warnings_emitted` · `errors_emitted` · `serialises_reviewers` · `pytest_files`)이다. 그런데 run 디렉터리가
사라진 지금 돌릴 수 있는 경로는 `--driver-output` 뿐이고, 그 경로가 계산하는 키는 정확히 그 선언 안 된 둘이라
**그 쌍에 대한 비교가 한 번도 실행되지 않은 것이고**, §4-A 가 서술하는 모양(0 을 돌려주고 그 0 이 통과로
읽힌다)을 §5 를 증명하려고 만든 도구가 저질렀다. 고치는 대신 지운 이유는 **비교할 원본이 다시 생기지 않기
때문이다**: 프롬프트 구성과 `232` 를 재는 쪽은 보존된 run 디렉터리를 입력으로 받는데 그 디렉터리는
`/tmp` 와 함께 사라졌고, 남은 쪽(발동한 가드 수 · 가드 개수 · 빌드 sha256)은 아래처럼 커밋된 파일과
한 줄 명령으로 끝난다. 입력이 영영 안 생기는 비교 도구는 거짓 안심이다.

| 값 | 어디서 | 어떻게 |
|---|---|---|
| 발동한 가드 — 경고 **0** · 오류 **1** (§5-1) | `driver-output/run-outer.log` · `probe.log` | `grep -c '::warning::'` → 0 · 0, `grep -c '::error::'` → 0 · 1. `run.log` 는 `run-outer.log` 의 부분집합이므로 **더하지 않는다** — `run.log` 에 마커가 있었다면 한 번 발동한 경고를 둘로 셌을 것이다. 이 실행에서는 세 로그 모두 경고 0건이라 디렉터리 전체를 훑어도 합계가 우연히 같았고, 둘로 세는 것은 두 로그에 경고 한 줄을 심은 대조 실행으로 확인했다(PR #189) |
| 가드 개수 **41** = 24 + 17 (§5-1) | 돌아간 빌드 `e9592d8` 의 세 모듈 — `git show e9592d8:.github/scripts/review_pr_local.py` 등 (`origin/task/local-review-driver-core` 에 있다 — **이 행과 다음 행의 재확인 경로는 그 브랜치 ref 하나에 걸려 있다.** 브랜치가 지워지면 객체가 어느 ref 에서도 안 닿아 `9cee8e6` 과 같은 상태가 되고, 두 행의 값은 `232` 행처럼 **기록값으로만** 남는다) | 세 모듈에 `grep -c '::warning::'` → 9 · 7 · 8 = **24**, 같은 셋에 `grep -c 'raise DriverError('` → 17 · 0 · 0 = **17** (2026-10-06 재측정). 원래 센 방법은 AST 로, `print(...)` 호출의 문자열 상수에 `::warning::` 이 든 것과 `raise DriverError(...)` 문을 센 것이다 — 주석·docstring 속 문자열을 빼기 위해서인데, **이 빌드에서는 그런 문자열이 없고 한 줄에 마커가 둘인 자리도 없어서 줄을 세는 grep 과 값이 같다.** 다른 빌드에서는 갈릴 수 있다 |
| 돌아간 드라이버의 sha256 `217da94…` (§5-0) | 같은 파일 | `git show e9592d8:.github/scripts/review_pr_local.py \| sha256sum` → `217da9413de294a523ef345f13d52c51c0c7d26f6cc0ca6e60236c8ab52252a7`. #170 head `ce8c324` 의 것은 `89a5a158f041258587547ecdc58d725de8c92ad987c5ed31fa8e93170f261719` (2026-10-06 재측정, 2026-09-20 기록과 일치) |
| 스레드 **45** (§5-2) | `driver-output/run.log` | `Collected 45 unresolved review thread(s)` 줄 |
| 벽시계 **399초** (§5-0) | `driver-output/run-outer.log` | `WALL_SECONDS=399` 줄 |
| 프롬프트 구성 83,579 = 63,694 + 19,885, `pr.diff` 불포함 (§5-2) | 아래 해시 표 | 크기는 `stat -c%s`. **불포함은 크기로 결판난다** — `pr.diff` 가 257,081 B 인데 `codex-prompt.md` 가 83,579 B 이므로 diff 전문이 그 안에 있을 수 없다. 2026-09-20 에 돌린 검사는 그보다 약한 것이었다(`pr.diff` 의 **앞 400자**가 프롬프트 안에 문자열로 없음) — 그것만으로는 "일부도 안 들어갔다"까지 안 되고, 크기 논증으로 되는 것은 **전문 불포함**까지다. 원본이 없으므로 지금은 해시 동일성 외에 재확인할 길이 없다 |
| `"EXISTING_COMMENTS="` 항목 **28,189 bytes** (§5-2) | 사라진 `/tmp` run 디렉터리의 `repo/.review-context/unresolved-threads.json` (아래 해시 표의 29,793 B 파일) | `len("EXISTING_COMMENTS=")` + 스레드 **전부(45개)** 를 `json.dumps(…, separators=(",",":"), ensure_ascii=False)` 로 직렬화한 바이트 수 — 슬라이스 상한은 `threads[:51]` 이었으나 수집된 스레드가 45개라 잘린 것이 없다. **원본이 없어 재확인 불가** — `232` 행과 같고, 남는 것은 해시다. 지운 `measure.py` 가 이 수의 유일한 기록된 도출이었으므로 슬라이스 범위와 인코딩을 여기 옮겨 둔다 |
| `232` (§5-6) | 커밋되지 않은 `nodeids` · `codex-run.log` | 아래 해시 표만 남는다 |

## 들어 있지 않은 것 — 크기와 해시로 남긴다

부피가 크거나(수백 KB) 내용의 대부분이 프롬프트 에코라 커밋하지 않는다.
**아래 값은 `stat -c%s` 와 `sha256sum` 으로 2026-09-20 에 잰 것이다.**

| 파일(보존된 run 디렉터리 기준) | bytes | sha256 |
|---|---|---|
| `repo/pr.diff` | 257,081 | `a254c82d5ea8f87ae814321c9b8cf2a97b8ca7e904386d5421fb4902abd7c9a4` |
| `repo/codex-run.log` | 539,846 | `dea53b4a03c8fb977437ff65b377f13fd966dd1e9689cf0b5306574758eb3a98` |
| `repo/codex-prompt.md` | 83,579 | `5f1bb2130f81ebdf65038a82bc260a7fdf8b57a2701ddb20cfd656454eaf2e09` |
| `repo/context.md` | 63,694 | `66a9b918644750307b44962f711277c2fb6f35794de282fc01097d20fe8b6c75` |
| `repo/.review-context/unresolved-threads.json` | 29,793 | `394e22c45d4668095ac22e4df11bfa99d012b502057d05e5347dcad7fd39cdcb` |
| `repo/.pytest_cache/v/cache/nodeids` | 25,620 | `61ddce402931039917451c277116280e674ccea11133ec42976cf20dda4d5739` |

잰 명령:

```
stat -c%s <path>
sha256sum <path>
```

**이 표가 받치는 주장들:**

- §5-2 의 프롬프트 구성 — `codex-prompt.md` 83,579 = `context.md` 63,694 + 19,885,
  그리고 `pr.diff` **전문**은 그 안에 없다 — 257,081 B 가 83,579 B 안에 안 들어간다(위 "파생값" 표).
- §5-6 의 `232` — `nodeids` 에서 읽은 수집 ID 수이고, `codex-run.log` 의 `232 passed in 3.48s` 와 같다.
  **둘 다 커밋하지 않으므로**, 근거로 남는 것은 위 해시뿐이다 — 재확인하려면 같은 실행을 다시 보존해야 한다.
- §5-1 의 `codex-run.log` 안 `::warning::` 63건이 **발동이 아니라 소스 인용**이라는 판정 — 그 파일의
  해시로 동일성만 확인 가능하고, 재확인하려면 같은 실행을 다시 보존해야 한다.

## 실행 스크립트에 대하여

`scripts/run.sh` 와 `probe.sh` 는 **그날 그 기계에서 돈 명령 그대로**다. 실행한 worktree 경로
(`/home/hyukhur/.../agent-af2dbb3734da99b6f`)와 `/tmp/lens-harvest` 가 박혀 있어 다른 기계에서는 `cd` 가
9 로 끝난다. 재현 명령으로 읽지 말고, **어떤 플래그와 환경으로 돌았는지의 기록**으로 읽을 것 — 그 두 비기본
조건이 §5-0 의 발견이다. `run.sh` 의 `tee` 파이프라인에 `pipefail` 이 없는 것도 그대로 둔다: 드라이버의
종료 코드는 파이프 안의 중괄호 블록이 `run-exit.txt` 에 따로 쓰므로(`DRIVER_EXIT=0`) 파이프에 가려지지
않고, 가려지는 것은 스크립트 자신의 종료 상태뿐이다. 초안의 `run.sh` 주석이 PR 을 6067줄이라 적었던 것은
`run.log` 의 `6109` 에 맞춰 고쳤다 — 주석이지 돌아간 명령이 아니다.

## 판정 파일 이름에 대하여

`reviewer-output/review-{claude,codex,gemini}.json` 은 §1-8 이 **아티팩트 충돌 벡터**로 이름한 바로 그
basename 이다. 일부러 그대로 둔다. 세 파일은 리뷰어가 쓴 바이트 그대로이고
`.github/scripts/tests/test_review_coordinates.py` 가 이 디렉터리를 픽스처로 읽는다 — 이름을 바꾸면 증거의
성질이 아니라 그 테스트가 바뀐다. 충돌은 재측정했다(2026-10-06, `d554aee`): 드라이버의 `tracked_artifact_names`
는 체크아웃 루트에서 `git ls-files -z -- <RUN_ARTIFACTS 이름들>` 을 돌리고, 그 pathspec 은 **루트 기준
경로**지 basename 매칭이 아니다. 이 저장소 루트에서 `git ls-files -- review-claude.json review-codex.json
review-gemini.json` 은 빈 출력이고, `'**/review-claude.json'` 으로 바꿔야 이 디렉터리의 파일이 잡힌다. 그러므로
이 셋은 이 저장소를 대상으로 한 어느 실행에도 경고를 내지 않는다. 드라이버가 basename 매칭으로 바뀌면 이
문단이 먼저 틀리므로, 그때 이 셋을 옮길 것.

## 이 증거가 증명하지 못하는 것

- **한 번의 실행이다.** 발동하지 않은 가드가 불필요하다는 증명이 아니다(§5-0).
- **실행된 빌드는 #172 의 head 다**(`217da94…`). §1-1 의 worktree, §1-2 의 strip, §1-11 의 직렬화,
  §3-1 의 다섯 방어 **어느 것도 이 실행에 없었다.** 여기 있는 로그로 그 기능들을 평가할 수 없다.
- **타임스탬프는 무효다.** `run.log` · `probe.log` 의 시각은 전부 종료 시각이다 — `buftest.log` 가 그
  대조 실험이다. 단계별 소요는 파일 mtime 으로 읽었고, 그 mtime 은 **완료 시각**이지 시작 시각이 아니다.

## `/tmp/lens-harvest/` 인용에 대하여

문서 본문이 그 경로를 가리키는 자리는 **휘발성 원본**을 뜻한다. 참조가 아니다 —
**재현 가능한 근거는 이 디렉터리와 위 해시 표뿐이다.**
