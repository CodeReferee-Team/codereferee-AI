# CodeReferee Evaluation Dataset

이 폴더는 CodeReferee Agentic AI 검증 워크플로우를 평가하고, 향후 파인튜닝 원천 데이터로 확장하기 위한 seed dataset을 포함한다.

현재 데이터는 실제 LitmusChaos 실행 결과가 아니라, 수동으로 설계한 synthetic SRE failure/evaluation case이다. 따라서 파인튜닝에 바로 사용하기보다는 평가용 seed로 먼저 사용하고, LLM으로 확장한 데이터는 반드시 사람 검수를 거친 뒤 학습 데이터로 변환해야 한다.

## Files

- `preflight_failures.jsonl`: 샌드박스 실행 전 URL/ref 접근 실패 케이스
- `sandbox_failures.jsonl`: 샌드박스 실행 실패 로그 케이스
- `metrics_judge_cases.jsonl`: Prometheus-style metrics 기반 Judge Pass/Fail 케이스
- `critic_refiner_cases.jsonl`: Critic/Refiner 원인 분석 및 개선 제안 케이스
- `local_sample_repo_specs.jsonl`: 나중에 실제 fixture repo로 구현할 수 있는 샘플 레포 명세

## Counts

- Preflight failure cases: 10
- Sandbox failure cases: 10
- Metrics Judge cases: 20
- Critic/Refiner label cases: 20
- Local sample repo specs: 5
- Total cases/specs: 65

## Fine-tuning Policy

각 JSONL row에는 다음 메타데이터가 포함된다.

```json
{
  "source": {
    "type": "synthetic_manual_seed",
    "llm_generated_allowed": true,
    "human_review_required": true,
    "real_execution_observed": false
  }
}
```

의미는 다음과 같다.

- 현재 데이터는 사람이 설계한 synthetic seed이다.
- 나중에 LLM으로 유사 케이스를 확장할 수 있다.
- 단, LLM 생성 데이터는 파인튜닝 전에 반드시 사람이 검수해야 한다.
- LitmusChaos와 Prometheus가 연결된 뒤에는 `real_execution_observed=true`인 실제 실행 데이터를 별도로 추가한다.

## Recommended Next Step

1. 현재 데이터셋으로 Judge/Critic/Refiner 평가 테스트를 만든다.
2. 반복적으로 틀리는 failure type을 확인한다.
3. 부족한 failure type을 LLM으로 초안 생성한다.
4. 사람이 로그/메트릭/정답 라벨을 검수한다.
5. 모델별 fine-tuning 포맷으로 export한다.
