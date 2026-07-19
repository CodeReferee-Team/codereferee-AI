# Generated CodeReferee Dataset Seeds

이 폴더는 CodeReferee 평가/파인튜닝 후보 데이터를 카테고리별로 통합해 보관한다.

## Files

- `preflight_failures.jsonl`: 210 cases
- `sandbox_failures.jsonl`: 210 cases
- `metrics_judge_cases.jsonl`: 220 cases
- `critic_refiner_cases.jsonl`: 220 cases
- `local_sample_repo_specs.jsonl`: 205 specs

Total: 1,065 rows/specs.

## Safety Note

이 데이터는 synthetic seed이므로 바로 fine-tuning에 사용하지 않는다.
파인튜닝 전에는 `human_review_required=true`인 항목을 사람이 검수하고, 검수된 데이터만 `datasets/codereferee/reviewed/`로 이동한다.

## Source Types

- `synthetic_manual_seed`: 처음 수동 설계한 seed case
- `synthetic_generated_seed`: 대량 생성한 synthetic expansion case

두 유형 모두 검수 전에는 최종 학습 데이터로 사용하지 않는다.
