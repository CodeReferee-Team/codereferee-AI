# CodeReferee Sandbox

CodeReferee Sandbox는 GitHub 레포지토리의 실행 가능성과 신뢰성 정보를 수집해 AI Core에 전달하는 실행 모듈이다.

## 역할

- 기본 Docker 실행 경로: GitHub repository clone, branch 또는 commit checkout, stack 감지, build/test/run smoke validation
- 외부 Sandbox 실행 경로: Kubernetes 기반 fixture의 Chaos v1 실험 실행 및 복구 관측
- timeout 및 resource limit 적용
- stdout/stderr/exit_code와 SRE 관측값 수집
- **요청: 실패한 단계를 응답에 담아줄 것** (`failed_step`: `setup` | `clone` | `checkout` | `patch` | `install` | `build` | `test`).
  지금은 AI가 로그 문자열을 뒤져 원인을 추정한다. 이 방식은 실제로 틀렸다 — sandbox 스크립트가 서두에
  `apt-get install`을 출력하기 때문에 모든 실패가 `dependency_install_failed`로 분류됐다
  (docs/evaluation-design.md 14.3). 단계 마커 기준으로 고쳤지만, 로그 형식이 바뀌면 또 깨진다.
  실패한 단계를 sandbox가 직접 알려주면 추정이 사라진다. 외부 sandbox는 우리 스크립트를 쓰지 않으므로
  마커가 없어 특히 필요하다.
- Refiner 패치 재실행: `patch_diff`가 오면 `refiner_patch.diff`로 마운트해 clone/checkout 직후 `git apply`한다. 적용 실패는 검증 실패와 원인이 달라 전용 종료 코드 88로 구분한다. 외부 sandbox HTTP API에는 아직 패치 필드가 없어 `sandbox_patch_unsupported`로 돌려준다 (백엔드 상의 항목).
- Judge Agent에 전달할 실행 결과와 evidence 생성

## 실행 흐름

```text
Repository URL
→ Preflight
→ SANDBOX_BASE_URL 설정 여부 확인
  → 미설정: Docker Sandbox에서 clone/build/test/smoke validation
  → 설정: 외부 Sandbox HTTP API 호출
→ Collect Logs / SRE Evidence
→ Return SandboxResult

## Chaos v1 외부 Sandbox

`SANDBOX_BASE_URL`이 설정되면 AI Core는 `POST /repositories/validate`로 외부 Sandbox를 호출한다.
현재 Chaos v1은 요청에 담긴 사용자 레포지토리를 Kubernetes에 배포하지 않는다. 대신 Sandbox가 관리하는 `fixture-api`에 Pod Kill을 실행하고, 교체 Pod 생성·HTTP 복구·Kubernetes 이벤트를 관측한다.

AI Core는 기존 실행 결과 필드와 함께 다음 Chaos v1 evidence를 보존한다.

- baseline
- availability, error rate, p95 latency, recovery seconds
- chaos observation과 Kubernetes events
- observation source와 probe transport

이 결과는 Judge, Critic, Refiner가 사용할 수 있는 검증 근거다. Chaos v1의 fixture 결과는 사용자 레포지토리 자체의 Kubernetes 실행 결과가 아니라는 점을 구분해야 한다.
```
