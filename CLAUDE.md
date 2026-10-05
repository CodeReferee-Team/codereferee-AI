# CodeReferee AI Core

GitHub 레포지토리 URL을 받아 "이 코드가 돌아가는가, 장애에서 살아남는가"를 판정하고 근거와 수정안을 함께 돌려주는 서비스. 코드를 생성하지 않는다.

## 먼저 읽을 문서

| 알고 싶은 것 | 문서 |
| --- | --- |
| 파이프라인 전체와 노드별 역할 | `docs/architecture.md` |
| 판정 규칙의 조문과 근거 | `docs/judge-policy.md` |
| 평가셋·지표·회귀 게이트 | `docs/evaluation-design.md` |
| Agent 입출력 스키마 | `docs/agents.md`, `docs/agent-output-schema.md` |
| 샌드박스 계약과 종료 코드 | `docs/sandbox.md` |

동작이 바뀌면 코드보다 문서를 먼저 갱신한다.

## 설계 원칙

| 원칙 | 내용 |
| --- | --- |
| 판정은 규칙이 한다 | Pass/Fail과 `reason_category`는 결정적 규칙이 정한다. `judge_uses_llm` 기본값은 `False` |
| LLM은 서술과 생성만 | Critic의 원인 설명, Refiner의 수정 편집. 판정에 관여하지 않는다 |
| 적용과 재검증은 코드 | 패치 적용, 가드, 재실행, 재판정 전부 결정적 코드 |
| 사용자 레포에 쓰지 않는다 | 패치는 샌드박스 안에서만 적용된다. push는 없다 |

판정을 LLM에 맡기지 않는 이유는 측정값이다. 같은 34건에서 규칙 100%, LLM 94%였고, 레포 코드에 "이건 통과시켜라"는 문장을 심으면 LLM이 2건 속았다. 심사받는 쪽이 심사자를 조종할 수 있으면 심사가 아니다.

**근거 없는 임계값으로 Fail을 내지 않는다.** 복구 상한은 probe 설정이 없으면 `None`을 돌려주고, 자원 SLO는 운영자가 임계값을 정한 지표만 본다. 기본값을 발명하면 판정의 근거가 없어진다.

## 경계

```
preflight    규칙만. URL 형태 확인과 git ls-remote 한 번. clone도 실행도 하지 않는다
1층 샌드박스  clone, 패치, 스택 감지, 의존성 설치, 테스트, smoke       (로컬 Docker)
2층 샌드박스  이미지 빌드, 배포, 장애 주입, 복구 관측                   (Kubernetes)
```

지금 두 층은 순서가 아니라 **교체 관계**다. `SANDBOX_BASE_URL`을 설정하면 2층만 돌고 1층 검증이 조용히 빠진다. 통합이 끝나기 전까지 이 점을 기억해야 한다. `docs/architecture.md` 1-6절.

## 작업 규칙

- 기본 브랜치에 직접 push하지 않는다. 커밋과 PR은 요청받을 때만 만든다
- PR 의견은 `gh pr comment`가 아니라 `gh pr review`로 보낸다. 알림이 가야 읽힌다
- **같은 영역을 건드리기 전에 열린 PR을 먼저 읽는다.** 머지가 느려서 `main`은 현재 상태가 아니다. `main`만 보고 "안 되어 있다"고 단정하면 틀린다
- 버그·신규기능·행동변경은 테스트부터 쓴다
- 한글 산출물을 내보내기 전에 `humanize-korean`을 통과시킨다

## 평가

```bash
cd ai-core
python -m evals.runner run --model none --slices T0,T0-adv,T1-chaos
python -m evals.runner compare --gate evals/baselines/fallback.json <report.json>
```

`--model none`은 LLM 없이 규칙만 채점한다. 결정적이고 비용이 들지 않는다.

`evals/`와 `tests/fixtures/chaos_actual/`은 아직 기본 브랜치에 없다. 미머지 브랜치에서 온다.

주 지표는 사람이 만든 T0와 T0-adv뿐이다. 합성 슬라이스는 회귀 감시용으로 따로 집계한다. **인젝션 false-pass가 1건이라도 나오면 무조건 회귀다.**

평가셋 자체를 바꾸면 `case_ids`가 달라져 게이트가 "비교 불가"로 exit 2를 낸다. 그때는 기준선을 다시 찍고 이전 값을 문서에 남긴다.

## 자주 물린 함정

**필드 이름이 계약 문서와 다르다.** 카오스 응답에서 네 건을 겪었다. `recovered` 불리언은 오지 않고 `recovered_at`만 오고, `source.real_execution_observed`는 키 자체가 없고, `workload`가 아니라 `target_configuration`이고, `aborted`가 아니라 `abort_condition.triggered`다. 계약 문서가 아니라 실제 응답으로 테스트를 만든다. `tests/fixtures/chaos_actual/`에 실측 파일이 있다.

**값을 보냈는데 받는 쪽이 조용히 버린다.** pydantic은 모르는 필드를 버린다. `SLO` 모델에 자리가 없어 CPU 임계값이 사라지고, CPU 96%짜리 레포가 합격으로 나갔다. 전송 계약을 늘릴 때는 양쪽을 같이 본다.

**실험이 돌지 않았는데 합격을 보고하는 경로.** 카오스 스키마로 왔는데 관측이 비어 있던 경우, 테스트가 없어 아무것도 실행하지 않은 경우, 쿠버네티스 없이 카오스를 요청한 경우. 전부 "검증할 것이 없었다"를 "합격"으로 내보내던 구멍이었다. 새 경로를 만들 때 이 모양을 의심한다.

**로그 문구에 의존하지 않는다.** 규칙은 종료 코드와 구조화된 결과만 본다. 로그를 읽는 쪽이 인젝션에 뚫린 경로다. 예외로 둔 키워드 fallback은 정확한 문구만 본다. `install`처럼 성공 로그에도 나오는 낱말로 분기하면 apt 준비 출력에 걸린다.

## 데이터셋 주의

`datasets/codereferee/generated/`의 커밋된 배치에는 정답 누출이 있다. 610건 중 168건이 `expected_failure_type`을 `stderr`에 그대로 적어두었다. 생성기는 고쳤지만 기존 배치는 다시 찍지 않았다. **다시 찍기 전에는 이 슬라이스로 모델을 학습시키지 않는다.**

T1-metrics 라벨 392건 중 186건은 `reason_category` 코드가 아니라 시나리오 이름이다. 카테고리 정확도를 그 숫자 그대로 읽으면 안 된다.

## 종료 코드

로컬 Docker 경로의 계약이다.

| 코드 | 뜻 |
| --- | --- |
| 86 | manifest 없음 |
| 87 | 러너나 툴체인 없음 |
| 88 | 패치 파일 없음 또는 적용 실패 |
| 89 | 검증할 것이 없음 |
| 126 | 실행 권한 없음 |
| 137 | OOM |

89가 중요하다. 테스트가 하나도 없는 레포를 합격으로 내보내지 않기 위한 코드다. Gradle과 Maven은 테스트가 0건이어도 성공하므로 아직 적용되지 않는다.

## 건드리지 않는 것

- `.env`의 `REDIS_URL`
- 요청 범위 밖의 동작하는 코드. 기존 데드코드는 보존한다
