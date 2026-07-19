# CodeReferee Generated Dataset batch_2026-07-19

이 폴더는 하루 단위로 분리한 generated seed dataset batch이다.

## Counts

- `preflight_failures.jsonl`: 200
- `sandbox_failures.jsonl`: 200
- `metrics_judge_cases.jsonl`: 200
- `critic_refiner_cases.jsonl`: 200
- `local_sample_repo_specs.jsonl`: 200
- Total: 1,000

## Policy

이 batch는 synthetic generated seed이다. 바로 fine-tuning에 사용하지 않고, 사람이 검수한 row만 `datasets/codereferee/reviewed/`로 이동한다.

분포 요약은 `distribution.json`에 저장한다.
