# CodeReferee Agent Output Schema

CodeReferee의 Agent들은 백엔드 저장, 평가 자동화, 향후 파인튜닝을 위해 정해진 JSON 구조로 응답해야 한다.

이 문서는 Planner, Judge, Critic, Refiner Agent의 출력 스키마를 정의한다.

---

## 1. 공통 규칙

모든 Agent 출력은 다음 규칙을 따른다.

```text
JSON object만 반환
Markdown fence 금지
필수 필드 누락 금지
근거는 evidence 배열로 제공
불확실한 내용은 추측처럼 표현하지 말고 근거 부족으로 표시
```

---

## 2. Planner Output

Planner Agent는 레포지토리 검증 계획을 반환한다.

```json
{
  "objective": "Validate repository reliability",
  "validation_scope": ["cloneability", "build", "test", "runtime"],
  "chaos_scenarios": ["network_delay", "cpu_stress"],
  "metrics_required": ["exit_code", "duration_ms", "error_rate"],
  "stop_conditions": ["uncloneable repository", "sandbox timeout"]
}
```

필수 필드:

```text
objective
validation_scope
chaos_scenarios
metrics_required
stop_conditions
```

---

## 3. Judge Output

Judge Agent는 검증 결과를 `Pass` 또는 `Fail`로 판단한다.

```json
{
  "status": "Fail",
  "reason": "Sandbox execution timed out before validation completed.",
  "evidence": [
    "timed_out=true",
    "duration_ms=60000"
  ]
}
```

필수 필드:

```text
status: "Pass" 또는 "Fail"
reason: 판단 이유
evidence: 판단 근거 배열
```

---

## 4. Critic Output

Critic Agent는 실패 원인과 reliability gap을 분석한다.

```json
{
  "issue": "Repository failed sandbox reliability validation.",
  "root_cause": "Redis dependency failure is not handled gracefully.",
  "evidence": [
    "Connection refused: redis:6379",
    "error_rate=0.35"
  ],
  "recommended_action": "Add timeout, retry, fallback behavior, and health checks."
}
```

필수 필드:

```text
issue
root_cause
evidence
recommended_action
```

---

## 5. Refiner Output

Refiner Agent는 기존 레포지토리를 개선하기 위한 수정 가이드를 제안한다.

```json
{
  "summary": "Redis dependency failure is not handled safely.",
  "patch_guidance": [
    "Configure Redis command timeout.",
    "Add retry with exponential backoff.",
    "Return degraded response when Redis is unavailable."
  ],
  "verification_steps": [
    "Run validation with Redis unavailable.",
    "Confirm controlled failure response.",
    "Confirm error rate stays within threshold."
  ],
  "risk": "medium"
}
```

필수 필드:

```text
summary
patch_guidance
verification_steps
risk: "low" | "medium" | "high"
```

---

## 6. 파인튜닝 데이터 변환 기준

향후 fine-tuning 포맷으로 변환할 때는 다음 형태를 기준으로 한다.

```json
{
  "case_id": "JUDGE-001",
  "agent": "judge",
  "input": {
    "preflight_report": {},
    "execution_result": {},
    "metrics": {}
  },
  "expected_output": {
    "status": "Fail",
    "reason": "...",
    "evidence": []
  },
  "source": {
    "human_review_required": true,
    "real_execution_observed": false
  }
}
```

검수되지 않은 synthetic/generated 데이터는 최종 학습 데이터로 사용하지 않는다.
