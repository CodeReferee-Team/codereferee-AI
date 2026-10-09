# CodeReferee fixture 매트릭스

손수 만든 테스트 레포 세트. 각 fixture = 샌박이 clone→배포→검증할 수 있는 작은 레포 하나.
**결함 종류 × 배포 shape** 두 축으로 커버리지를 넓혀, 데모 범위와 eval(T2a 로컬 fixture)을 함께 채운다.

검증 기준은 두 가지만 자동으로 확정한다. 나머지(HTTP 500, chaos 판정, 롤아웃 실패 같은 런타임
결과)는 full-stack 실행이 필요해 여기서 검증하지 않고 각 `LABEL.json`의 `unverified_runtime`에
명시해 둔다. 과장하지 않기 위해서다.

- **배포 플랜**: 실제 샌박 코드 `execution_plan.resolve_plan`이 선언한 shape로 해석되는가
- **결함 존재**: `LABEL.json`의 `defect_probe`가 가리키는 결함이 코드에 실제로 있는가(단위)

```
<sbx venv>/bin/python verify_fixtures.py   # 전체 재검증
```

## 매트릭스

| fixture | yaml | 배포 shape | plan source | 결함 | 기대 판정 | 자가치유 대상 |
|---|---|---|---|---|---|---|
| healthy-fastapi | 無 | stack_detection | stack_detection | 없음(baseline) | Pass | — |
| http500-bug-fastapi | 無 | stack_detection | stack_detection | 코드버그(KeyError→500) | Fail | app.py 코드 패치 |
| single-replica | 有 | validation.yaml | repository_configuration | 단일 replica 토폴로지 | Pass+WARNING | validation.yaml `replicas 1→2` |
| crashloop-dockerfile | 無 | Dockerfile | dockerfile | 부팅 크래시(미선언 env) | Fail | app.py env 가드 |
| compose-healthy | 無 | compose | compose | 없음(baseline) | Pass | — |
| node-healthy | 無 | stack_detection(node) | stack_detection | 없음(baseline) | Pass | — |

## 런타임 실검증 결과 (2026-10-09, full stack)

repo `CodeReferee-Team/codereferee-chaos-demo`에 브랜치로 올려 실제 제출(chaos_mode=litmus_container_kill).
master = single-replica/느린기동(READY_DELAY=35s) 케이스.

| fixture | 기대 판정 | 실측 판정 | 자가치유 | 실제 근거 |
|---|---|---|---|---|
| master(single-replica) | Fail→치유→Pass | ✅ Pass (iter=1) | **발동** replicas 1→2 | chaos_recovered_within_budget |
| healthy-fastapi | Pass | ✅ Pass | 불필요 | chaos_recovered_within_budget |
| http500-bug | Fail | ✅ Fail | ⚠️ 미발동(iter=0) | sandbox_nonzero_exit (/ 500→readiness 실패→타임아웃) |
| crashloop | Fail | ✅ Fail | 미발동(iter=0) | sandbox_nonzero_exit (CrashLoopBackOff→타임아웃) |

**판정 4/4 기대 일치.** 단 자가치유는 master(replicas)만 실동작.

**surfaced 능력 갭:**
1. 코드버그(http500)가 health 경로에서 터지면 "기동 타임아웃"으로 가려짐 → Critic 오진 → 코드픽스 heal 미발동. 현재 자가치유 = 사실상 replicas 토폴로지 1종.
2. http500·crashloop 둘 다 동일 근거(sandbox_nonzero_exit)로 수렴 — Critic이 서로 다른 실패를 "기동 실패"로 뭉뚱그림. = 설계문서의 "Critic 분류기 개선" 1순위 과제를 실측으로 재확인.

## 설계 원칙

1. **yaml은 탈출구, 전제 아님.** 4개 중 3개가 config 0줄(stack/Dockerfile 추론)로 배포됨.
   `single-replica`만 yaml 필요 — replicas는 "레포가 선언한 토폴로지"라 고칠 파일이 있어야 하기 때문.
   (근거 증명: `scratchpad/prove_no_yaml.py`)
2. **간판은 코드버그 픽스**(`http500-bug-fastapi`). config-free + 범용 + 깊이. `replicas++`보다 강함.
3. **네거티브/경고 케이스 포함**(`crashloop`, `single-replica`) — Judge가 틀린 걸 틀렸다 하는지 본다.

## 현재 커버리지 갭 (다음 배치 후보)

- `slow-startup`(복구 상한 초과), `bad-healthpath`(yaml override 필요 사례), `build-fail`(preflight)
- `oom`: 샌박 evidence에 mem 없음 → 현재 미지원(계측 갭). 넣으면 "탐지 못 함"이 라벨 정답
- 스택 다양화: java 복제(node는 node-healthy로 커버)
- 단, build-fail·bad-healthpath·oom은 대부분 'deploy 실패 → 자가치유 못 함' 벽에 걸림.
  샌박 pod 진단(docs/sandbox-pod-diagnostics.md)이 와야 코드·env 자가치유로 열림 → 그 전까진 eval 측정셋·갭 노출 가치만.
