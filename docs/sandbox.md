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

## 샌드박스 이미지

빌드:

```bash
docker build -t codereferee/sandbox-multi:2 ai-core/sandbox/
```

`:2`는 `eclipse-temurin:17-jdk-noble` 기반이고 Python 3.12.3, setuptools 68.1.2, Node 20, Maven 3.8.7을 담는다.

`:1`(jammy)을 쓰면 안 된다. Python 3.10과 setuptools 59.6을 주는데, 그 setuptools는 PEP 621의 `[project]` 테이블을 읽지 못한다. 그래서 `pip install .`이 이름 없는 `UNKNOWN-0.0.0` 패키지를 만들고 **종료 코드 0으로 끝낸다.** 설치는 되지 않았는데 성공으로 보이고, 뒤이어 pytest가 패키지를 import하지 못해 멀쩡한 레포가 `test_failure`로 나간다.

실측으로 겪었다. `pallets/markupsafe`가 `:1`에서 exit 4로 불합격이었고 `:2`에서 exit 0으로 통과한다.

`:2`에서는 pip 호출에 `--break-system-packages`가 필요하다. noble이 PEP 668로 시스템 Python 설치를 막기 때문이다. 컨테이너는 한 번 쓰고 버리므로 venv를 세우지 않는다. 이 옵션은 pip 23부터 있어서 `:1`에서는 오류가 난다. 즉 이미지와 스크립트는 함께 올라가야 한다.

### 알려진 한계

레포가 요구하는 Python이 3.12보다 높으면 설치가 실패한다. 이때 pip은 `requires a different Python`을 말하는데, 지금은 그 실패가 `test_failure`로 분류된다. 우리 이미지가 낮은 것이지 레포의 결함이 아니므로 분류를 나누는 것이 맞다. 종료 코드를 따로 두는 쪽을 논의해야 한다.

## 레포가 검증 방법을 선언하는 법

`.codereferee/validation.yaml`에 두 줄을 두면 추측하지 않는다.

```yaml
test: pytest -q
testDependencies: requirements/tests.txt
```

`test`가 있으면 스택별 기본 명령 대신 그대로 돌린다. `testDependencies`가 있으면 의존성 단계에서 함께 설치한다.

Kubernetes 샌드박스가 이미 같은 파일로 `deploymentProfile`을 고른다. 1층도 같은 파일을 읽으므로 두 층의 입력이 하나다.

### 왜 필요한가

자동 감지로 알 수 없는 것이 있다. 테스트 전용 의존성이 선언된 자리는 프로젝트마다 다르고(`optional-dependencies`, `dependency-groups`, `requirements/tests.txt`), 멀티모듈 빌드의 순서는 `mvn test` 한 번으로 맞출 수 없고, 브라우저가 필요한 테스트는 제외해야 한다.

선언이 없으면 우리가 명령을 추측한다. 추측이 틀려서 난 실패를 레포 탓으로 돌리면 안 되므로, 구조화 결과에 `verification_declared`를 담아 판정이 그 차이를 알게 한다.

| | 판정 |
| --- | --- |
| 선언했고 그대로 돌렸는데 실패 | 레포 결함으로 본다 |
| 선언하지 않아 추측했고 수집 단계에서 깨짐 | `verification_environment_unsupported` |

### 환경 한계로 분류하는 종료 코드

레포 결함이 아니라 우리가 검증할 수 없었던 경우다.

| 코드 | 뜻 |
| --- | --- |
| 126 | 실행 권한이 없다 |
| 127 | 명령을 찾지 못했다 |
| 137 | 우리가 건 메모리 한도에 걸렸다 |
| 4 | pytest 사용법 오류. 우리가 부르는 방법이 틀렸다 |
| 2 (선언 없을 때) | pytest 수집 오류. 대개 테스트 전용 의존성이다 |

87(러너 없음)은 묶지 않는다. `unsupported_project_stack`이라는 정확한 코드를 이미 갖고 있고, 묶으면 어떤 스택을 받지 못했는지가 사라진다.

### 실측

```
markupsafe     exit 0   declared=false   Pass / all_checks_passed
itsdangerous   exit 2   declared=false   Fail / verification_environment_unsupported
six            exit 89  declared=false   Fail / no_tests_detected
```

`itsdangerous`는 이전에 `test_failure`로 나갔다. `freezegun`이 없어서 수집이 깨진 것이고 레포에는 결함이 없다.
