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

## 2026-11-13 기존 모델 재실측과 후속 입력 보완

이번 재실측은 Ollama의 `llama3.1:8b`와 `qwen2.5-coder:7b`를 사용했다.
OpenAI 호환 호출, temperature 0, 기존 `json_object` 형식을 사용했고,
CPU 호스트에서 호출 제한은 300초로 설정했다. 과거 PR에는 가중치 digest와 Ollama 버전이
기록되어 있지 않아 현재 태그·양자화·digest를 별도 파일에 보존했다.

| 실제 요청 | 모델 | 실행 결과 |
| --- | --- | --- |
| `a3784f9f-3a37-4d88-94c0-8371b32176ff` | Llama 3.1 8B | 실제 패치 생성·재배포 1회. 모델이 Redis import를 제거해 수정본 업무 baseline 실패 |
| `4310cf04-49cd-43f3-aae2-fb0636262335` | Qwen 2.5 Coder 7B | 실제 패치 재검증 3회. 후속 소스와 컴파일 오류를 전달했지만 모델의 들여쓰기 수정 실패 |
| `2f93e4b2-9bed-49ae-b45b-3701eafbb1b4` | Qwen 2.5 Coder 7B | 별도 재연결 대기값 제어 사례. 모델이 복제본 증가만 제안해 편집 없음 |
| `2d1da9c8-2b96-4b59-b9f9-a7940a1447ff` | Qwen 2.5 Coder 7B | Critic 안내 보완 후 실제 호출 8회·패치 재배포 3회. 1·3회차 Redis Chaos 재실행에서 미복구, 2회차 컴파일 실패 |

마지막 요청은 실제 모델 생성 → 패치 적용 → 그룹 재배포 → Redis 장애 재주입 →
메트릭 수신 → Backend 결과 조회 → PostgreSQL·SQLite 보존 경로를 확인했다.
수리 성공 검사 결과는 `patch_verified=false`다. 제출 원본 판정은 FAILED로 유지됐다.
실측 보고서와 세 회차가 PostgreSQL·SQLite·Backend 조회에서 일치했다.
최대 실행 회차 뒤의 추가 제안은 검증되지 않은 수정안이므로 `refine_rounds`와 구분해 읽는다.

실행 중 확인해 보완한 전달 문제는 다음과 같다.

- 편집 anchor 재요청에서도 선택된 레포 URL을 유지한다.
- 외부 Sandbox의 실패 단계 stdout·stderr를 소스 경로 탐색과 모델 입력에 전달한다.
- LF 고정 clone에 누적 패치를 적용할 때 Windows stdin의 CRLF 변환을 피하도록 UTF-8 바이트를 사용한다.
- Critic 안내 세 줄은 진단 실험에서 사용했고, 최종 Sandbox 연동 범위 정리 때 원래 상태로 되돌렸다.

실제 Git 한글·LF 패치와 실패 로그·소스 첨부 회귀를 포함한 관련 테스트는 실험 당시 121개가
통과했다. Critic 실험 안내 및 정적 테스트를 제거한 최종 코드에서는 120개가 통과했다.
이 목록은 전체 AI 테스트 스위트와 구분한다. 앞서 기록한 Windows SQLite 잠금 문제는 후속이다.
LLM 응답을 대체하지 않았으며, 모든 원본 모델 응답과 실패 실행을 로컬 실측 폴더에 보존했다.

## 원격 모델 설정 확인

원격 AI의 기본 브랜치와 현재 모델 관련 브랜치에서 코드 기본값은
`llm_provider="gemini"`, `llm_model="gemini-flash-latest"`다.
`feat/local-llm-provider`는 환경변수로 Ollama 등을 연결하는 어댑터를 제공한다.
현재 README는 운영 호출을 Groq로 안내하고, infra의 `instances/ai/.env.example`도
Groq 주소와 외부 `LLM_MODEL` 값을 사용한다. 템플릿 모델명은 자리표시자다.
이후 제공된 팀 대화에서 운영이 Groq `openai/gpt-oss-120b`로 결정됐음을 확인했다.
과거 로컬 Llama 시험과 실제 운영 API를 구분한다. 현재 검증 범위는 Sandbox의 멀티레포 패치 계약이다.

## 제어 검증과 정리 결과

같은 제어 사례의 재연결 대기값을 사람이 86400초에서 1초로 바꿔 실제 Sandbox API·Git·
Docker·kind·Redis 장애를 재실행했다. `observed`, exitCode 0, recovered true,
복구 14.91초를 확인했다. 이 실행은 사람이 작성한 양성 제어이며 LLM 자동 수리 성과에 포함하지 않는다.

실패 사례를 포함한 Sandbox resource ID 26개의 namespace·컨테이너·볼륨·호스트/kind 이미지·
작업 폴더가 남지 않음을 독립 조회했다. 검증을 위해 중지했던 기존 Docker Desktop Litmus
워크로드 10개는 원래 복제본 수와 Ready 상태로 복원했다.
검증용 신규 모델 가중치를 정리했고 원래 모델 목록을 유지했다. C 드라이브 여유는 약 6.38GB였다.

AI 소스 수집의 `shutil.rmtree(ignore_errors=True)`는 Windows의 읽기 전용 Git 파일 때문에
임시 clone을 남겼다. 이번 실측의 16개 폴더는 검증 루트 내부 경로를 확인해 수동 정리했다.
이 정리 경로의 Windows 보강은 후속으로 남긴다.

## 최종 검증 범위 정리

Sandbox 멀티레포 패치 계약을 중심으로 검증을 마무리했다. 모델 출력 품질 개선을 위해
실험했던 Critic 안내 세 줄과 해당 정적 테스트를 제거했고, 최종 관련 회귀는 120개 통과했다.
레포 URL 유지, 실패 단계 로그 전달, Windows 누적 패치 UTF-8 전송 보완은 유지한다.

별도 실제 Sandbox API 실행에서 main과 backend의 정상 패치를 함께 적용했다.
두 commit SHA와 패치 해시가 일치했고 Redis 장애 후 복구, 부하 목표 5명에서 100요청 실패 0건,
동일 요청 재전송의 결과 재사용과 자원 정리를 확인했다. 이 실행은 LLM 호출 없이 수행했다.
기존 실제 Llama 패치 생성·재배포 기록은 자동 수리 성공으로 표시하지 않는다.

운영 Groq 키 사용 중단 요청 이후 외부 LLM 호출을 중단했다. 중단 전 모델 목록 조회 GET 1회가
있었고 생성 호출은 0회였다. 해당 로컬 테스트 요청은 Backend `ERROR`로 종료 처리했다.
키는 소스·문서·환경 파일에 저장하지 않았다.

### 최종 Llama 전체 큐 재검증

`llama3.1:8b`로 최종 코드의 Backend·Redis·Worker·Sandbox·kind·PostgreSQL을 연결했다.
요청 `b0d32d78-f48e-45a1-a177-23b3e6105fe7`은 모델 호출 9회와 backend 패치 재배포
2회를 수행했다. 모델이 클래스 선언을 삭제하거나 잘못된 들여쓰기를 생성해 두 수정본은 build에서
실패했다. 실제 컴파일 로그와 누적 수정 소스는 다음 회차에 전달됐다. 마지막 편집은 backend에
없는 `.codereferee/validation.yaml`을 수정하려 해 가드·재요청 후 거절됐다.

최종 Backend `FAILED`, `patch_verified=false`이며 두 회차와 최종 보고서가 PostgreSQL·SQLite에
동일하게 저장됐다. 원본 메트릭 수신은 `received`였다. 모델 응답 대역은 사용하지 않았다.
이 실행에서 두 레포 순차 자동 수리 성공은 확보하지 못했다. 정상 편집 대역을 이용한 기존
backend→main 전환·누적 성공 실측과 별도로 해석한다.
