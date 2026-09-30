# CodeReferee Sandbox

CodeReferee Sandbox는 GitHub 레포지토리의 실행 가능성과 신뢰성 정보를 수집해 AI Core에 전달하는 실행 모듈이다.

## 역할

- 기본 Docker 실행 경로: GitHub repository clone, branch 또는 commit checkout, stack 감지, build/test/run smoke validation
- 외부 Sandbox 실행 경로: Kubernetes 기반 fixture의 Chaos v1 실험 실행 및 복구 관측
- timeout 및 resource limit 적용
- stdout/stderr/exit_code와 SRE 관측값 수집
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


## 종료 코드 계약

스크립트가 쓰는 종료 코드다. 사용자 레포의 결함과 "검증할 것이 없음"을 구분하기 위해 값을 나눠 둔다.

| 코드 | 의미 |
| --- | --- |
| 0 | 검증 통과 |
| 86 | 지원하는 manifest가 없다 |
| 87 | 스택은 알았지만 sandbox에 러너가 없다 |
| 88 | 패치 파일이 sandbox에 없다 |
| **89** | **검증할 테스트가 없다** |

### 89를 둔 이유

이전에는 테스트가 없는 레포가 통과로 나왔다. Python은 `compileall` 뒤에 `tests` 디렉터리가 없으면
그대로 끝냈고, Node는 `npm run test --if-present`가 test 스크립트가 없을 때 아무것도 하지 않고
성공으로 끝냈다. 안정성을 검증하는 서비스가 "검증할 것이 없었다"를 "합격"으로 내보내면
사용자가 근거 없는 확신을 얻는다.

컴파일이 되는 것과 동작이 검증된 것은 다르다. 89는 그 구분을 남긴다.

판정은 FAILED다. 백엔드 최종 상태가 PASSED / FAILED / ERROR 세 가지뿐이고, 89는 우리 인프라
문제가 아니라 대상 레포의 상태이므로 ERROR가 아니다. 판정 이유에는 "테스트가 없어 안정성을
확인할 수 없었다"가 그대로 들어간다.

Gradle과 Maven은 테스트가 0건이어도 `test` 태스크가 성공한다. 이를 구분하려면 테스트 리포트를
읽어야 하므로 아직 다루지 않는다.
