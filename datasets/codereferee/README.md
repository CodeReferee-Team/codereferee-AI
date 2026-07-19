# CodeReferee Dataset

이 폴더는 CodeReferee Agentic AI 검증 워크플로우를 평가하고, 향후 파인튜닝 원천 데이터로 확장하기 위한 dataset을 포함한다.

현재 데이터는 실제 LitmusChaos 실행 결과가 아니라 synthetic/manual/generated SRE failure/evaluation case이다. 따라서 바로 파인튜닝에 사용하지 않고, 사람이 검수한 데이터만 `reviewed/`로 이동해 학습 후보로 사용한다.

## Structure

```text
datasets/codereferee/
├── README.md
├── generated/
│   ├── README.md
│   ├── preflight_failures.jsonl
│   ├── sandbox_failures.jsonl
│   ├── metrics_judge_cases.jsonl
│   ├── critic_refiner_cases.jsonl
│   └── local_sample_repo_specs.jsonl
└── reviewed/
    └── .gitkeep
```

## Generated Counts

- `generated/preflight_failures.jsonl`: 210개
- `generated/sandbox_failures.jsonl`: 210개
- `generated/metrics_judge_cases.jsonl`: 220개
- `generated/critic_refiner_cases.jsonl`: 220개
- `generated/local_sample_repo_specs.jsonl`: 205개
- Total: 1,065개

## Fine-tuning Policy

각 row에는 다음 메타데이터가 포함된다.

```json
{
  "source": {
    "llm_generated_allowed": true,
    "human_review_required": true,
    "real_execution_observed": false
  }
}
```

의미는 다음과 같다.

- 현재 데이터는 synthetic seed이다.
- LLM으로 유사 케이스를 확장할 수 있다.
- LLM 생성/합성 데이터는 파인튜닝 전에 반드시 사람이 검수해야 한다.
- LitmusChaos와 Prometheus가 연결된 뒤에는 `real_execution_observed=true`인 실제 실행 데이터를 별도로 추가한다.

## Recommended Workflow

1. `generated/` 데이터로 Judge/Critic/Refiner 평가 테스트를 만든다.
2. 반복적으로 틀리는 failure type을 확인한다.
3. 부족한 failure type을 LLM으로 초안 생성한다.
4. 사람이 로그/메트릭/정답 라벨을 검수한다.
5. 검수 완료 데이터만 `reviewed/`로 이동한다.
6. 모델별 fine-tuning 포맷으로 export한다.
