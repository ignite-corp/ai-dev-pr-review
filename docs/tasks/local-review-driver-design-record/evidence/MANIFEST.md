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
| `driver-output/run.log` · `run-outer.log` | 성공 실행의 드라이버 stdout/stderr 전문 | §5-1(경고 0건) · §5-3(2건 범위 밖) · §5-7(round 13) |
| `driver-output/probe.log` · `probe-exit.txt` | 기본 프롬프트 경로로 돈 첫 시도(종료 1) | §5-9 · §5-1(발동한 가드 1개) |
| `driver-output/run-exit.txt` | `DRIVER_EXIT=0` | §5-0 |
| `driver-output/lens-clone-fingerprint.txt` | `.git/config` 지문(§1-10) | §5-1(평가됐으나 발동 안 함) |
| `reviewer-output/review-{claude,codex,gemini}.json` | 세 판정 파일 원본 | §5-3 의 줄 번호 넷 전부 |
| `reviewer-output/claude-run.log` | claude 실행 요약 | §5-0(비용·`duration_ms` 380,819) · §5-5(`modelUsage: claude-sonnet-4-6`) |
| `reviewer-output/{gemini,codex,claude}-review.log` | 리뷰어 로그 | §5-1(세 CLI 모두 판정 직접 씀) |
| `scripts/run.sh` · `probe.sh` | 실행 명령 전문 — **비기본 조건 둘이 여기 보인다**(`PR_SIZE_LIMIT=20000`, 명시 프롬프트 경로) | §5-0 |
| `scripts/watch.sh` · `wait.sh` | 실행 중 관측에 쓴 것 | — |
| `scripts/buftest.sh` · `buftest.log` | **계측 오류의 대조 실험** — 2초 간격 세 줄이 한 시각으로 찍힘 | §5-0(타임스탬프 무효) |
| `scripts/measure.py` | 파생값 **전부**를 다시 계산 | §5-0 · §5-1 · §5-2 · §5-6 |

`scripts/measure.py` 는 인자 없이 돌리면 2026-09-20 에 기록된 값을 찍고, 입력을 주면 **지금 잰 값과
기록값을 나란히** 찍는다. `--driver-output` 만으로도 *발동한 가드 수*는 여기 커밋된 로그에서 바로 나온다:

```
$ python3 scripts/measure.py
  errors_emitted   = 1      <- 프로브의 DriverError
  warnings_emitted = 0      <- 성공 실행에서 24개 경고 경로 중 0개
```

빌드 식별과 가드 개수까지 다시 보려면 그 빌드의 스크립트 디렉터리를 준다:

```
$ python3 scripts/measure.py \
    --driver <그 빌드>/.github/scripts/review_pr_local.py \
    --scripts-dir <그 빌드>/.github/scripts \
    --run-dir <보존된 run 디렉터리>
```

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
  그리고 `pr.diff` 는 그 안에 **없다**(`measure.py` 의 `pr_diff_inside_codex_prompt`).
- §5-6 의 `232` — `nodeids` 에서 읽은 수집 ID 수이고, `codex-run.log` 의 `232 passed in 3.48s` 와 같다.
  **후자의 원본은 커밋하지 않으므로**, 근거로 남는 것은 위 해시와 `measure.py` 가 세는 `nodeids` 쪽이다.
- §5-1 의 `codex-run.log` 안 `::warning::` 63건이 **발동이 아니라 소스 인용**이라는 판정 — 그 파일의
  해시로 동일성만 확인 가능하고, 재확인하려면 같은 실행을 다시 보존해야 한다.

## 이 증거가 증명하지 못하는 것

- **한 번의 실행이다.** 발동하지 않은 가드가 불필요하다는 증명이 아니다(§5-0).
- **실행된 빌드는 #172 의 head 다**(`217da94…`). §1-1 의 worktree, §1-2 의 strip, §1-11 의 직렬화,
  §3-1 의 다섯 방어 **어느 것도 이 실행에 없었다.** 여기 있는 로그로 그 기능들을 평가할 수 없다.
- **타임스탬프는 무효다.** `run.log` · `probe.log` 의 시각은 전부 종료 시각이다 — `buftest.log` 가 그
  대조 실험이다. 단계별 소요는 파일 mtime 으로 읽었고, 그 mtime 은 **완료 시각**이지 시작 시각이 아니다.

## `/tmp/lens-harvest/` 인용에 대하여

문서 본문이 그 경로를 가리키는 자리는 **휘발성 원본**을 뜻한다. 참조가 아니다 —
**재현 가능한 근거는 이 디렉터리와 위 해시 표뿐이다.**
