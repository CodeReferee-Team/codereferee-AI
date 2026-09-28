# CodeReferee Judge Policy

CodeReferee의 Judge Agent는 Repository Preflight, Sandbox Execution, Metrics Snapshot을 바탕으로 검증 결과를 `Pass` 또는 `Fail`로 판단한다.

이 문서는 LLM이 감으로 판단하지 않도록 하는 기본 판정 기준표이다. LitmusChaos와 Prometheus가 연결된 뒤에도 이 기준을 우선 적용하고, 필요한 경우 SLO 기준만 확장한다.

---

## 1. 입력 근거

Judge Agent는 다음 정보를 판단 근거로 사용한다.

```text
preflight_report
execution_result
metrics
validation_plan
```

---

## 2. 기본 판정 순서

Judge는 아래 순서로 판단한다.

```text
1. Preflight 실패 여부
2. Sandbox 실행 여부
3. Sandbox 종료 상태
4. Metrics/SLO 위반 여부
5. 최종 Pass/Fail 결정
```

---

## 3. Fail 기준

아래 조건 중 하나라도 만족하면 기본적으로 `Fail`이다.

| Stage | Condition | Status | Reason Category |
| --- | --- | --- | --- |
| Preflight | `cloneable = false` | Fail | `repository_not_accessible` |
| Preflight | invalid URL/ref/commit | Fail | `invalid_repository_input` |
| Sandbox | sandbox not executed after cloneable repo | Fail | `sandbox_not_executed` |
| Sandbox | `timed_out = true` | Fail | `timeout` |
| Sandbox | `exit_code != 0` | Fail | `sandbox_nonzero_exit` |
| Sandbox | unsupported project stack | Fail | `unsupported_project_stack` |
| Metrics | missing required metrics | Fail | `missing_metrics` |
| Metrics | `p95_latency_ms > 300` | Fail | `latency_slo_violation` |
| Metrics | `error_rate > 0.01` | Fail | `error_rate_slo_violation` |
| Metrics | `cpu_usage_percent > 80` | Fail | `cpu_saturation` |
| Metrics | memory usage ratio `> 0.8` | Fail | `memory_pressure` |
| Metrics | `restart_count > 0` | Fail | `unexpected_restart` |
| Metrics | `availability < 0.995` | Fail | `availability_slo_violation` |
| Metrics | `db_connection_errors > 0` | Fail | `database_connection_errors` |
| Metrics | `redis_connection_errors > 0` | Fail | `redis_connection_errors` |
| Metrics | `request_count = 0` during runtime validation | Fail | `no_traffic_observed` |

---

## 4. Pass 기준

아래 조건을 모두 만족하면 `Pass`로 판단할 수 있다.

```text
preflight.cloneable = true
preflight.executable = true
sandbox.exit_code = 0
sandbox.timed_out = false
required metrics exist
all configured SLO thresholds are satisfied
```

---

## 5. Evidence 규칙

Judge 출력에는 반드시 판단 근거가 포함되어야 한다.

좋은 evidence 예시:

```text
exit_code=0
timed_out=false
p95_latency_ms=120
error_rate=0.0
restart_count=0
```

나쁜 evidence 예시:

```text
Looks good
Probably failed
No issue
```

---

## 6. Chaos 실험 판정 기준 (Chaos v1)

Sandbox v1이 실제 Kubernetes Pod Kill 실험 결과를 보내기 시작하면서 추가한 기준이다. 이 절의 숫자는 모두 출처를 밝히고, 출처가 없는 값은 "우리 관례"라고 표시한다.

### 6.1 세 가지 판정 축

고정 임계값 하나로 판정하지 않는다. Kubernetes는 "Pod Kill 후 몇 초 안에 복구되어야 한다"는 기준을 공표한 적이 없다. 대신 다음 세 축을 쓴다.

| 축 | 방법 | 근거 |
| --- | --- | --- |
| 정상 상태 대비 편차 | 실험 전 baseline 구간의 가용성·오류율·지연을 기준으로 실험 구간의 편차를 본다 | [Principles of Chaos Engineering](https://principlesofchaos.org/): 시스템 내부 속성이 아니라 측정 가능한 출력(처리량·오류율·지연 백분위)을 짧은 구간 관찰해 정상 상태의 대리 지표로 삼는다. [AWS FIS stop conditions](https://docs.aws.amazon.com/fis/latest/userguide/stop-conditions.html)도 "정상 상태를 먼저 정의하고 그로부터 임계값을 도출"하는 순서를 명시한다 |
| 에러 버짓 소모량 | 실험으로 발생한 불가용 시간을 월간 에러 버짓과 비교한다 | [SRE Book Availability Table](https://sre.google/sre-book/availability-table/): 99.9% = 월 43.2분. [SRE Workbook 예시 에러 버짓 정책](https://sre.google/workbook/error-budget-policy/): 단일 장애가 월 버짓의 20%를 넘으면 포스트모템 대상, 버짓 소진 시 변경 중단 |
| 기대 복구 상한 | 대상 워크로드 설정에서 계산한다: `terminationGracePeriodSeconds`(기본 30초) + 이미지 pull·기동 + readiness `initialDelaySeconds`(기본 0) + `periodSeconds`(기본 10) × `successThreshold`(기본 1) + `minReadySeconds`(기본 0) | [Pod lifecycle](https://kubernetes.io/docs/concepts/workloads/pods/pod-lifecycle/#pod-termination-flow), [Probes](https://kubernetes.io/docs/concepts/workloads/pods/probes/), [Deployment](https://kubernetes.io/docs/concepts/workloads/controllers/deployment/#min-ready-seconds) |

### 6.2 판정 규칙

우선순위 순으로 적용한다. 앞 규칙이 걸리면 뒤는 보지 않는다.

| 순서 | 조건 | 판정 | Reason Category |
| --- | --- | --- | --- |
| 1 | baseline이나 복구 관측값이 없음 | Error | `chaos_evidence_missing` |
| 2 | 실험이 중단 조건으로 정지됨 | Error | `chaos_experiment_aborted` |
| 3 | `chaos_observation.recovered = false` | Fail | `chaos_not_recovered` |
| 4 | 복구 시간 > 기대 복구 상한 | Fail | `chaos_recovery_exceeds_expected_bound` |
| 5 | 불가용 시간이 월간 에러 버짓의 100% 이상 | Fail | `chaos_error_budget_exhausted` |
| 6 | 불가용 시간이 월간 에러 버짓의 20% 이상 | Pass + 경고 | `chaos_error_budget_significant_burn` |
| 7 | 대상 replica가 1개 | Pass + 경고 | `chaos_single_replica_topology` |
| 8 | 실험 구간 p95가 baseline p95의 N배 초과 (기본 10배, 우리 관례) | Pass + 경고 | `chaos_latency_degraded` |
| 9 | 위에 걸리지 않음 | Pass | `chaos_recovered_within_budget` |

### 6.3 규칙의 근거와 주의점

- **경고는 별도 상태가 아니라 Pass에 붙는 표시다.** 백엔드 최종 상태가 PASSED / FAILED / ERROR 세 가지뿐이라 Warning을 별도 판정으로 만들면 전달할 곳이 없다. 경고는 판정에 영향을 주지 않고 리포트에만 남는다.
- **replica가 1개면 다운타임은 결함이 아니다.** Kubernetes 문서는 desired replica가 1이면 disruption 시 "실질 가용성 100% 상실"이라고 명시하고, 단일 인스턴스 앱은 가끔의 다운타임을 감수하는 것이 정당한 선택지라고 설명한다([Configure PDB](https://kubernetes.io/docs/tasks/run-application/configure-pdb/)). 따라서 구성에 대한 경고로 다루고 Fail로 보지 않는다.
- **오류 0건을 Pass 조건으로 삼지 않는다.** 엔드포인트 제거는 Pod 종료와 동시에 진행되고, 종료 중인 엔드포인트는 EndpointSlice에서 즉시 사라지지 않으며, kube-proxy와 외부 로드밸런서로의 전파 지연에는 문서상 상한이 없다([Pod lifecycle](https://kubernetes.io/docs/concepts/workloads/pods/pod-lifecycle/#pod-termination-flow), [Pods and endpoint termination flow](https://kubernetes.io/docs/tutorials/services/pods-and-endpoint-termination-flow/)). 짧은 오류 구간은 문서화된 정상 동작이다.
- **PDB 유무는 Pod Kill 판정 근거가 아니다.** Kubernetes 문서는 Deployment나 Pod를 삭제하는 행위가 PDB를 우회한다고 명시한다([Disruptions](https://kubernetes.io/docs/concepts/workloads/pods/disruptions/)). 참고 정보로만 기록한다.
- **중단된 실험과 실패한 실험을 구분한다.** AWS FIS는 중단된 실험을 재개할 수 없다고 명시한다. 중단은 안전장치가 작동한 것이지 대상 시스템의 결함 판정이 아니다.
- **지연 배수 10배는 우리 관례다.** Google SRE 문헌은 p95 단일 임계값이 아니라 p50과 p99·p99.9를 함께 보라고 권고한다([Service Level Objectives](https://sre.google/sre-book/service-level-objectives/)). 현재 Sandbox가 p95만 보내므로 임시로 baseline 대비 배수를 쓰고, p99가 들어오면 임계값을 다시 정한다.
- **복구 시간을 DORA 등급으로 표기하지 않는다.** DORA 2025 보고서는 Elite/High/Medium/Low 등급 구분을 없애고 7개 팀 유형으로 대체했다([DORA FAQ](https://dora.dev/faq/)). 등급 대신 필요하면 분포상의 위치로 표현한다.
- **Chaos v1 결과는 fixture 앱의 복원력이다.** 사용자 레포를 Kubernetes에 배포하지 않으므로, 이 판정은 사용자 레포의 SRE 검증 결과가 아니다. 리포트에 `source.fixture` 값을 그대로 노출해 구분한다.

### 6.4 SLO 값은 설정이다

기본 SLO는 목표값이지 자연 법칙이 아니다. SRE Book은 현재 성능을 보고 목표를 정하지 말고, 100%를 목표로 삼지 말라고 권고한다([Embracing Risk](https://sre.google/sre-book/embracing-risk/)). 기본값은 운영자가 바꿀 수 있는 설정으로 두고, 가용성 목표에 따른 월간 허용 불가용 시간은 공표된 표를 따른다.

| 가용성 목표 | 월간 허용 불가용 시간 | 단일 장애 경고선(버짓 20%) |
| --- | --- | --- |
| 99% | 7.2시간 | 약 86분 |
| 99.9% | 43.2분 | 약 8.6분 |
| 99.99% | 4.32분 | 약 52초 |

출처: [SRE Book Availability Table](https://sre.google/sre-book/availability-table/), 20% 기준은 [Example Error Budget Policy](https://sre.google/workbook/error-budget-policy/).

현재 코드의 기본 SLO(`_default_slo`: p95 30000ms, availability 99.9%)와 이 문서 3절의 표(p95 300ms, availability 0.995)가 서로 다르다. 어느 쪽도 출처가 있는 값이 아니므로, 구현 시 하나의 설정으로 통합하고 기본값을 명시적으로 선언한다.

### 6.5 Sandbox에 요청하는 evidence (필드 확정)

`chaos-v1` 응답만으로는 규칙 4와 7을 계산할 수 없다. 아래 필드를 받으면 두 규칙이 동작한다.

**모든 신규 필드는 `chaos_observation` 안에 넣는다.** AI Core는 이 객체를 통째로 보존하므로
파서를 고치지 않아도 값이 유실되지 않는다. 새 최상위 키를 만들면 파서가 버린다.

```json
"chaos_observation": {
  "type": "pod_kill",
  "recovered": true,
  "recovery_seconds": 12,

  "workload": {
    "kind": "Deployment",
    "name": "fixture-api",
    "replicas_desired": 1,
    "replicas_ready_before": 1,
    "min_ready_seconds": 0,
    "termination_grace_period_seconds": 30,
    "readiness_probe": {
      "initial_delay_seconds": 1,
      "period_seconds": 2,
      "success_threshold": 1,
      "failure_threshold": 3,
      "timeout_seconds": 1
    }
  },

  "kill_mode": "graceful",
  "target_pod_uid": "8f3c...",
  "replacement_pod_uid": "b91a...",
  "deletion_requested_at": "2026-09-28T05:00:00Z",
  "replacement_ready_at": "2026-09-28T05:00:12Z",
  "endpoint_ready_at": "2026-09-28T05:00:13Z",

  "aborted": false,
  "abort_reason": null,

  "windows": {
    "baseline_seconds": 30,
    "experiment_seconds": 60,
    "probe_interval_seconds": 1,
    "probe_count_baseline": 30,
    "probe_count_experiment": 60
  },
  "error_rate_definition": {
    "denominator": "application_requests_only",
    "excluded_paths": ["/healthz"]
  }
}
```

#### 필드별 용도와 수집 방법

| 필드 | 쓰는 규칙 | 수집 방법 |
| --- | --- | --- |
| `workload.replicas_desired` | **규칙 7** (단일 replica는 Fail이 아니다) | `kubectl get deploy <name> -o jsonpath='{.spec.replicas}'` |
| `workload.replicas_ready_before` | 규칙 7 보강 (실험 직전 실제 상태) | 같은 객체의 `{.status.readyReplicas}` |
| `workload.readiness_probe.*` | **규칙 4** (기대 복구 상한 계산) | `{.spec.template.spec.containers[0].readinessProbe}` |
| `workload.min_ready_seconds` | 규칙 4 | `{.spec.minReadySeconds}` |
| `workload.termination_grace_period_seconds` | 규칙 4 | `{.spec.template.spec.terminationGracePeriodSeconds}` |
| `kill_mode` | 규칙 4 (`force`면 grace를 더하지 않는다) | pod-delete 실험의 `FORCE` env. true면 `"force"`, 아니면 `"graceful"` |
| `target_pod_uid`, `replacement_pod_uid` | 재시작이 아니라 교체임을 증명 | `kubectl get pod <name> -o jsonpath='{.metadata.uid}'` |
| `deletion_requested_at` | 복구 시간의 시작점 확정 | 삭제 요청 시각. `{.metadata.deletionTimestamp}`로도 확인 가능 |
| `replacement_ready_at` | 복구 시간의 종료점 | `{.status.conditions[?(@.type=="Ready")].lastTransitionTime}` |
| `endpoint_ready_at` | 트래픽 실제 복귀 시점 | `kubectl get endpointslice`의 `endpoints[].conditions.ready` |
| `aborted`, `abort_reason` | **규칙 2** (중단은 결함이 아니다) | 실험 러너의 자체 상태. Litmus면 ChaosEngine `spec.engineState=stop` |
| `windows.*` | 관측 구간이 짧아서 나온 수치인지 구분 | 러너가 쓴 probe 횟수·간격을 그대로 |
| `error_rate_definition.denominator` | 같은 장애가 다른 판정으로 나오는 것 방지 | probe 대상 경로 정의를 그대로 |

`error_rate_definition`은 [SRE Workbook의 요청 기반 SLI 정의](https://sre.google/workbook/implementing-slos/)가 "유효 이벤트" 분모를 명시하도록 요구하기 때문에 받는다. 분모가 흔들리면 같은 장애도 다른 판정이 나온다.

#### 기대 복구 상한 (규칙 4의 계산식)

```
bound = grace + initial_delay + (period × success_threshold) + min_ready + startup_allowance
grace = 0 (kill_mode = "force") | termination_grace_period_seconds (graceful)
```

`startup_allowance`는 스케줄링과 이미지 pull에 드는 시간으로, 클러스터마다 달라 측정이 불가능하다.
설정값(`CHAOS_RECOVERY_STARTUP_ALLOWANCE_SECONDS`, 기본 30초)으로 두고 운영자가 조정한다.
위 필드가 하나라도 없으면 상한을 계산하지 않고 규칙 4를 건너뛴다. 근거 없는 상한으로 Fail을 내지 않는다.

#### LitmusChaos로 전환할 때 추가로 받을 것

Litmus는 아래를 CR에 이미 갖고 있어 새로 측정할 필요가 없다. `chaos_observation.litmus`에 넣어주면
실험 자체가 제대로 돌았는지와 대상 서비스의 결함을 구분할 수 있다.

```json
"litmus": {
  "engine": "fixture-api-chaos",
  "experiment": "pod-delete",
  "verdict": "Pass",
  "fail_step": null,
  "probe_success_percentage": 100,
  "total_chaos_duration_seconds": 30,
  "chaos_interval_seconds": 10,
  "pods_affected_percentage": 100
}
```

출처는 ChaosResult의 `status.experimentStatus`(verdict, failStep)와 `status.probeStatus`, ChaosEngine의
실험 env(`TOTAL_CHAOS_DURATION`, `CHAOS_INTERVAL`, `PODS_AFFECTED_PERC`, `FORCE`)다.
필드 이름은 Litmus 버전에 따라 다를 수 있으니 실제 CR을 덤프해 확인하고 매핑한다.

`verdict`는 판정에 직접 쓰지 않는다. Litmus의 `Fail`은 "실험 수행 실패"와 "대상 서비스 결함"을
구분하지 않기 때문이다. 우리 규칙 2(중단)와 규칙 1(근거 없음)의 판별 자료로만 쓴다.

## 7. 판정 주체 (2026-09-27 확정)

Pass/Fail 판정과 reason_category는 **규칙이 정한다.** LLM은 판정에 관여하지 않는다.

측정 근거(같은 평가셋 34건, docs/evaluation-design.md 12절)

| 지표 | 규칙 | LLM(gemini-3.1-flash-lite) |
| --- | --- | --- |
| 판정 정확도 | 100% | 100% |
| false-pass | 0% | 0% |
| 원인 분류 정확도 | 94.1% | 76.5% |

판정 정확도가 같고 원인 분류는 규칙이 더 정확했다. 게다가 규칙은 결정적이고, 근거를 정책
문서로 추적할 수 있으며, 레포 로그에 심어둔 지시에 흔들리지 않는다. 비용과 지연도 없다.

LLM은 Critic의 원인 서술과 Refiner의 개선 제안에만 쓴다. 규칙이 할 수 없는 일이다.

비교 실험을 다시 돌릴 수 있도록 경로는 남겨 두었다. `JUDGE_USES_LLM=true`,
`PLANNER_USES_LLM=true`로 켤 수 있으며 기본값은 꺼짐이다.

## 8. 현재 한계

현재 기준은 LitmusChaos 실측 데이터가 붙기 전의 기본 정책이다.

향후 추가 예정:

```text
LitmusChaos experiment result
Prometheus query result
container/pod restart metrics
network latency/packet loss metrics
service dependency health metrics
```
