# CodeReferee Agents

CodeReferee의 Agent 모듈은 GitHub 레포지토리 검증 결과를 바탕으로 실행 가능성, 신뢰성, 장애 원인, 개선 방향을 판단하는 역할을 한다.

이 프로젝트는 코드를 새로 생성하는 AI가 아니라, 기존 레포지토리를 검증하고 분석하는 Agentic AI 구조를 목표로 한다.

---

## 1. Agent 구조 개요

현재 Agent 흐름은 다음과 같다.

```text
Repository URL
→ Preflight
→ Sandbox Execution
→ Judge Agent
→ Critic Agent
→ Refiner Agent
→ Validation Report
```

## 2. 주요 Agent

### Planner Agent

Planner Agent는 레포지토리 검증 계획을 세운다.

- 검증 목적 설정
- 검증 범위 정의
- 필요한 메트릭 정의
- 중단 조건 설정

### Judge Agent

Judge Agent는 Preflight, Sandbox 실행 결과, Metrics를 바탕으로 검증 성공 여부를 판단한다.

- 레포지토리 실행 가능 여부 판단
- Sandbox 결과 분석
- Metrics 기반 Pass/Fail 판단

### Critic Agent

Critic Agent는 Judge Agent가 Fail로 판단한 경우, 실패 원인과 신뢰성 문제를 분석한다.

- 실패 원인 분석
- 로그와 메트릭 기반 근거 추출
- 개선 방향 제안

### Refiner Agent

Refiner Agent는 Critic Agent의 분석 결과를 바탕으로 수정 방향을 제안한다.

- 개선 요약 작성
- 수정 가이드 제안
- 실제 unified diff 제안 (`patch_diff`, 만들 근거가 없으면 `null`)
- 재검증 절차 제안
- 위험도 평가

패치는 실행 전에 두 단계로 거른다. `inspect_diff`가 1MB 상한·보호 경로(`.github/`, `.git/`, CI 설정)·레포 밖 경로를 막고, `check_applies`가 얕게 clone한 레포에 `git apply --check`를 돌린다. 둘을 통과한 패치만 sandbox에서 적용해 재실행한다.

재실행이 여전히 실패하면 그 실행 결과를 다시 판정해 다음 패치를 만들고, 새 패치를 누적 diff 뒤에 이어 붙여 다시 돌린다. 멈추는 조건은 네 가지다 — 재실행 통과, 라운드 상한(`MAX_SELF_HEALING_RETRIES`, 기본 3), 누적 diff 1MB 초과, 더 만들 패치가 없음. 라운드마다 `REFINING` progress를 `round`/`max_rounds`와 함께 보내고, 결과는 `metrics.patch_rounds`(라운드별)와 `metrics.patch_rerun`(마지막)에 남는다.

재판정은 복사한 state에서 돌린다. 제출된 레포에 대한 판정이 최종 산출물이라 덮어쓰면 안 된다. 재실행이 통과해도 판정은 바뀌지 않는다 — 제출된 레포는 여전히 실패했고, 누적 diff는 "이 변경이면 고쳐진다"는 증거다.

## 3. 관련 파일

### ai-core/app/agents/llm.py

LLM 호출을 담당한다.

### ai-core/app/agents/prompts.py

각 Agent가 사용할 프롬프트를 정의한다.

### ai-core/app/agents/nodes.py

각 Agent의 실행 노드를 정의한다.

### ai-core/app/workflow/repository_validation.py

전체 Agent workflow를 연결한다.

### ai-core/app/models.py

Agent 간에 공유되는 데이터 모델을 정의한다.
