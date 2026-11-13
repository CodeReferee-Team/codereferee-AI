# 멀티레포 순차 수정 연결

외부 Kubernetes Sandbox의 version 3 결과에 있는 `source.repositories`와
`source.service_repositories`를 이용해 수정 소스의 소유 레포를 정한다.
모델·Judge 임계값은 변경하지 않는다. Refiner 프롬프트에는 전달된 파일의 alias·SHA와 경로 기준을 설명한다.

## 순서

1. 처음 실행한 모든 레포의 전체 commit SHA를 보관한다.
2. 명시적 실패 레포 또는 미복구 의존성 장애의 대상 서비스 매핑으로 소스 레포를 찾는다.
3. 복제본 등 그룹 설정 수정은 기준 레포를 대상으로 한다. 근거가 모호한 의존성 소유자는 임의 선택하지 않는다.
4. 선택한 SHA를 checkout하고 그 레포의 이전 누적 패치를 적용한 파일을 Refiner에 제공한다.
5. Refiner의 기존 편집·가드 경로를 거친 패치에 코드가 alias·기준 SHA를 붙인다.
6. 기준 레포 패치는 `patchDiff`, 추가 레포 패치는 `repositoryPatches`로 전달한다.
7. 수정하지 않은 레포도 `repositoryCommits`로 고정하고 동일 검사 조건으로 그룹을 재검증한다.
8. 여전히 실패하면 새 근거에 따라 다른 레포로 전환할 수 있다. 이전 레포 패치는 유지한다.

횟수는 **레포별이 아니라 기존 전체 루프의 `max_self_healing_retries`**를 공유한다(기본 3회).
한 회차에서 새로 수정하는 레포는 하나다. 여러 레포의 협조적 동시 수정은 별도 과제다.
원본 커밋의 판정과 실행 결과는 기존처럼 복원하고, 패치 검증 여부와 라운드는 별도로 제공한다.
검증 성공은 모든 공개 레포의 자동 수리나 LLM 출력 품질을 보장하지 않는다.

## 안전 및 호환성

- 잘못된 SHA·미선언 alias·소유자가 불명확한 경우 실행 전에 멈춘다.
- 전체 패치 크기를 레포 합산으로 검사한다. 기존 경로·CI·테스트 약화 검사도 유지한다.
- 단일 레포 요청에는 추가 필드를 보내지 않는다.
- 추가 레포 패치는 외부 Sandbox에서만 지원한다. 로컬 Docker로 조용히 대체하지 않는다.
- `patch_repository`와 `patch_base_commit_sha`는 기존 Refiner 보고서의 확장 필드다.
- 레포를 바꾸는 것은 관측 근거 기반의 수정 대상 전환이다. 복합 장애의 근본 원인을 확정했다는 의미는 아니다.

## 검증 구분

외부 Sandbox가 `sandboxReport.steps`에 실패 단계의 stdout·stderr를 담은 경우,
그 로그를 후속 소스 파일 탐색과 Critic·Refiner 입력에 전달한다.
최상위 stderr가 일반적인 실패 요약이어도 실제 컴파일 오류와 파일 경로를 보존한다.
LF로 고정한 clone에 이전 패치를 복원할 때는 UTF-8 바이트로 Git stdin에 전달한다.
Windows의 텍스트 stdin 줄바꿈 변환으로 같은 패치가 적용되지 않는 문제를 방지한다.

검사 묶음의 모델 입력에는 실제 실행한 하위 시나리오의 종류와 복구 관측을 함께 전달한다.
묶음 이름만으로 의존성 장애를 식별할 수 없으므로, 수집된 관측의 요약을 사용한다.

라우팅·누적·SHA 고정은 단위/그래프 테스트와 실제 Git·Sandbox 실행으로 확인한다.
고정된 편집 응답을 사용하는 실측은 LLM 생성 성공으로 집계하지 않는다.
외부 API를 호출한 수리 사례와 전체 Backend 큐·저장 검증은 각각 별도 근거로 남긴다.

### 2026-11-13 실측

로컬 Git fixture의 실제 원본 Redis 미복구를 Judge Fail로 확인했다.
고정된 편집 응답을 기존 Refiner 가드에 넣어 backend 코드를 수정하고 그룹을 재배포했다.
Redis는 복구했지만 목표 5명에서 요청 100회 중 59회가 실패해 Judge는 Fail을 유지했다.
소스 문맥이 main의 설정으로 전환되고 backend 패치를 보존한 2회차 배포에서 목표 5명 100회 실패 0회를 확인했다.
Judge Pass와 patch_verified true를 기록하면서 제출 원본은 Fail로 유지했다.
고부하 200명은 201/1,633회 실패였으므로 모든 부하 통과로 해석하지 않는다.

관측 후 fixture 두 레포의 branch HEAD를 이동시켜도 최초 SHA를 유지했다.
실측 패치와 회차를 기존 Redis 결과 이벤트로 직렬화하고 SQLite 저장·조회에서 보존됨도 확인했다.
이것은 실제 LLM 생성 성공이나 새 Backend/Redis/PostgreSQL 전체 E2E가 아니다.
처음 편집 대역의 인자 오류를 바로잡은 뒤 저장된 실제 원본 관측에서 이어 실행한 사실도 구분한다.

신규 라우팅 테스트 19개 통과. 관련 Windows 회귀 162개 중 158개 통과·SQLite 파일 잠금 오류 4개다.
같은 4개는 분리된 변경 전 HEAD 5d28d40에서도 재현했다. 저장소 코드는 이 PR에서 변경하지 않는다.
Sandbox의 `docs/multi-repository-patch-validation.md`에 실제 거절 3건·자원 8건 독립 감사와 최초 실패를 기록했다.
Linux/Mac/AWS 및 공개 멀티레포의 추가 실측, 레포별 패치 UI는 후속이다.

### 실제 로컬 LLM과 Backend E2E 추가 검증

기존 설치된 Ollama `exaone3.5:2.4b`로 실제 Critic·Refiner를 호출했다.
로컬 Git fixture 두 레포를 사용하며, 모델 응답이나 편집을 대체하지 않았다.
공개 GitHub 프로젝트를 대상으로 한 실측은 추가로 필요하다.

첫 실행에서 모델이 없는 코드 줄을 수정하려 해 기존 anchor 검사로 거절됐다.
검사 묶음의 모델 입력이 하위 Redis 장애 종류를 누락하는 점을 보완하고 다시 실행했다.
두 번째 요청 `e75064d5-999b-4c0f-ab43-d7755fbe3da5`에서는 실제 Redis 장애·미복구 관측,
메트릭 수신(`received`), 로컬 모델 응답 3회, Backend FAILED 저장까지 확인했다.
PostgreSQL과 SQLite의 최종 보고서 및 빈 `refine_rounds` 일치도 확인했다.

모델이 `patch_guidance`에 문자열 대신 편집 객체를 넣었고, 기존 스키마 보정 재요청도 실패했다.
따라서 `patch_verified=false`, 재배포 회차 0회다. 실제 LLM의 수리 성공은 미검증이다.
실측에 사용한 모델은 이번 검증에서 선택한 기존 로컬 모델이며 운영 모델 확정 사항은 아니다.
Judge 정책·모델 가중치·학습 설정은 변경하지 않았다.

별도 Backend 수정은 선택 필드 `refine_rounds`를 aiReports에 보존한다.
원본 Fail과 수정안 검증 결과를 구분하는 기존 계약을 유지한다.
앞서 실측한 backend→main 2회차 결과를 새 요청의 Redis 결과 큐에 재생하고,
Backend 조회·PostgreSQL 및 Backend 재기동 뒤 두 회차와 전체 패치 보고서 보존을 확인했다.
이 재생은 저장 계약 검증이며, LLM이나 Chaos를 새로 실행한 사례에 합산하지 않는다.
추가 회귀는 라우팅·Refiner 관련 56개 통과이며, 신규 라우팅 테스트는 총 21개다.
원본 요청·응답과 실패 로그는 로컬 `tmp/multirepo-patch-20261113/llm-e2e*`에 보존한다.

### 기존 AI 검증 기록과 이번 실패의 범위

원격 PR #83과 `docs/evaluation-design.md` 6절에는 Ollama `llama3.1:8b`의
실제 편집 생성 8/8, 패치 재실행 성공 6/8 기록이 있다.
PR #108은 `qwen2.5-coder` 7B로 동일 Chaos 실패 입력에서 복제본 수정 편집을
3회 생성한 기록을 남겼다. 해당 PR은 당시 전체 Chaos Pass의 제약도 함께 명시했다.
현재 README는 운영 LLM 호출을 Groq로 안내한다.

이번 `exaone3.5:2.4b`는 검증 과정에서 설치된 모델을 선택한 것이다.
팀의 기존 모델과 같은 설정으로 재현한 시험이 아니며, 복합 시나리오·멀티레포 fixture도 다르다.
따라서 이번 실패를 기존 MVP 전체의 고장이나 기존 LLM 실측 부재로 일반화하지 않는다.
원본 요청을 고정 SHA의 Git 파일과 대조해 backend의 app.py와 Redis 장애 정보가
실제 Refiner에 일치하게 전달됐음을 확인했다. 응답에는 edits가 비어 있고,
patch_guidance에 편집 객체가 들어 있었다.
구조화 출력은 검토한 보완 후보이며 현재 구현된 옵션은 아니다.
