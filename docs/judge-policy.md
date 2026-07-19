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

## 6. 현재 한계

현재 기준은 LitmusChaos 실측 데이터가 붙기 전의 기본 정책이다.

향후 추가 예정:

```text
LitmusChaos experiment result
Prometheus query result
container/pod restart metrics
network latency/packet loss metrics
service dependency health metrics
```
