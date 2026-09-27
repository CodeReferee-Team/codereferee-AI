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

### 6.5 Sandbox에 추가로 요청할 evidence

현재 `chaos-v1` 응답만으로는 위 규칙 4, 7을 계산할 수 없다. 다음 필드가 필요하다.

- 대상 워크로드의 `replicas`
- readiness probe 설정(`initialDelaySeconds`, `periodSeconds`, `successThreshold`)과 `minReadySeconds`, `terminationGracePeriodSeconds`
- kill 방식(graceful 삭제인지 `--force --grace-period=0`인지)
- 삭제 요청, 교체 Pod 생성, Ready 도달, 엔드포인트 등록 시각
- 이전 Pod와 새 Pod의 UID(재시작이 아니라 교체임을 증명)
- baseline 관측 구간의 길이와 실험 구간의 길이
- 중단 조건 발동 여부
- 오류율 분모에 무엇을 포함했는지(헬스체크 포함 여부)

마지막 항목은 [SRE Workbook의 요청 기반 SLI 정의](https://sre.google/workbook/implementing-slos/)가 "유효 이벤트" 분모를 명시하도록 요구하기 때문이다. 분모가 흔들리면 같은 장애도 다른 판정이 나온다.

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
