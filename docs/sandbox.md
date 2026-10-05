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

## 통합 테스트 기록 (2026-10-05)

서버 API로만 쏘고 서버 응답만 읽었다. `Backend → Redis → AI → Sandbox → Redis → Backend` 전 구간이다. 이미지는 `:2`, LLM은 끄고 규칙만 돌렸다.

| 케이스 | 레포 | 판정 | 사유 코드 | exit |
| --- | --- | --- | --- | --- |
| python 통과 | `pallets/markupsafe` | PASSED | `all_checks_passed` | 0 |
| node 통과 | `sindresorhus/slugify` | PASSED | `all_checks_passed` | 0 |
| 테스트 없음 | `benjaminp/six` | FAILED | `no_tests_detected` | 89 |
| 테스트 전용 의존성 | `pallets/itsdangerous` | FAILED | `test_failure` | 2 |
| 브라우저 테스트 | `debug-js/debug` | FAILED | `test_failure` | 1 |
| maven 멀티모듈 | `google/gson` | FAILED | `test_failure` | 1 |
| gradle Android | `square/okio` | FAILED | `test_failure` | 1 |
| 잘못된 브랜치 | `debug-js/debug` + `main` | FAILED | `ref_not_found` | — |
| PR 링크 | `markupsafe/pull/1` | FAILED | `invalid_repository_input` | — |
| 없는 레포 | `no-such-repo-zzz` | FAILED | `repository_not_found` | — |

스택 감지는 python·node·maven·gradle 네 가지가 모두 맞았다. prepare, clone, patch, detect, dependencies 다섯 단계는 모든 케이스에서 통과했고 갈리는 곳은 smoke뿐이다.

### 환경 한계를 레포 결함으로 보고한다

위 표에서 `test_failure`로 나간 네 건 중 **레포에 결함이 있는 것은 하나도 없다.** 전부 우리 샌드박스가 그 레포를 검증할 수 없는 경우다.

| 레포 | 실제 원인 | 보고 |
| --- | --- | --- |
| `itsdangerous` | 테스트 전용 의존성(`freezegun`). 선언 위치가 프로젝트마다 다르다 | `test_failure` |
| `debug` | karma와 browserify. 이미지에 브라우저가 없다 | `test_failure` |
| `gson` | JPMS 테스트 모듈. `mvn test` 단독 호출로는 멀티모듈 순서를 맞출 수 없다 | `test_failure` |
| `okio` | `:okio-assetfilesystem` 모듈. Android SDK가 없다 | `test_failure` |

같은 뿌리의 사례가 더 있다.

- `markupsafe`는 이미지의 setuptools가 낡아 불합격이었다. 이 문서 위쪽에 적었다
- Python 3.12보다 높은 버전을 요구하는 레포는 설치가 실패하고 `test_failure`가 된다

**"우리가 검증할 수 없었다"와 "레포에 결함이 있다"가 한 사유 코드로 뭉개진다.** 심사 서비스에서 이것은 사용자가 멀쩡한 코드를 불합격으로 받는다는 뜻이다.

이미지에 브라우저와 Android SDK와 모든 Python 버전을 넣는 것은 답이 아니다. 끝이 없고 넣어도 다음 스택에서 같은 일이 생긴다.

### 분리 방향

두 가지를 함께 정해야 한다.

**사유 코드를 나눈다.** 환경 미지원 전용 코드를 두고 그 경우 판정을 Fail이 아니라 Error로 보낸다. `chaos_evidence_missing`을 Error로 둔 것과 같은 원리다. 우리 쪽 문제이므로 사용자의 불합격이 아니고 집계에서도 우리 커버리지 격차로 모인다.

판별 가능한 신호가 이미 있다.

```
pytest exit 2   수집 중단. 대개 테스트 전용 의존성
pytest exit 4   사용법 오류. 우리 호출이 틀렸다
pytest exit 5   수집된 테스트 0건
pip  "requires a different Python"
npm  "Cannot find module" 중 브라우저 런타임 계열
```

**레포가 테스트 명령을 선언하게 한다.** 자동 감지로는 테스트 전용 의존성과 멀티모듈 순서를 알 수 없다. 프로필이나 `.codereferee/validation.yaml`이 그 자리다. 이것은 Kubernetes 샌드박스가 이미 쓰는 방식이고 1층도 같은 선언을 읽으면 두 층의 입력이 하나가 된다.

### 재검증 루프

`itsdangerous`를 LLM을 켜고 돌렸을 때 루프가 2라운드 돌았다. 가드 통과, 샌드박스 재실행, 재판정, 중단 조건이 모두 동작했고 `patch_verified=false`로 "패치가 고치지 못했다"를 정확히 보고했다.

그때 모델이 테스트 파일을 고치려 했다. 원인은 모델 성능이 아니라 우리가 준 입력이었다. 수집 오류일 때 매니페스트를 함께 주지 않아 모델이 가진 파일이 테스트 파일 하나뿐이었다. 입력을 고치고 테스트 경로를 가드로 막았다.
