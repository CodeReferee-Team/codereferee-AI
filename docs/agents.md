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

### 역할 요약 (2026-09-30 갱신)

| 단계 | 누가 | 하는 일 | 판정 권한 | 수정 권한 |
| --- | --- | --- | --- | --- |
| Planner | 규칙 | 검증 계획 | 없음 | 없음 |
| **Judge** | **규칙** | Pass/Fail + `reason_category` | **있음** | 없음 |
| 위치 특정 | 규칙 | 실패 로그에서 고칠 파일 추출 | 없음 | 없음 |
| **Critic** | LLM | 실패 원인 분석 (자연어) | 없음 | 없음 |
| **Refiner** | LLM | 수정안 생성 (어느 줄 → 무엇으로) | 없음 | **있음** |
| 적용·재검증 | 규칙 | 편집 적용 → 게이트 → sandbox 재실행 | 없음 | 집행만 |

판정과 수정을 다른 주체가 맡는다. 판정은 규칙이 하고(8절 judge-policy), 수정안은 모델이 만들고,
적용과 재검증은 다시 코드가 한다. 모델이 "고쳤다"고 선언할 수 없다는 뜻이다.

### Planner Agent

Planner Agent는 레포지토리 검증 계획을 세운다.

- 검증 목적 설정
- 검증 범위 정의
- 필요한 메트릭 정의
- 중단 조건 설정

기본값은 규칙 기반이다. `PLANNER_USES_LLM=true`로 LLM 경로를 켤 수 있다.

### Judge Agent

Judge는 Preflight, Sandbox 실행 결과, Metrics를 바탕으로 Pass/Fail과 `reason_category`를 정한다.
**규칙이 판정한다.** LLM은 판정에 관여하지 않는다. 근거는 docs/judge-policy.md 8절이다 —
같은 평가셋에서 규칙이 판정 100%·카테고리 94.1%였고, LLM은 94.1%·58.8%였으며 레포 로그에 심어둔
지시에 2건 속았다.

- 레포지토리 실행 가능 여부 판단
- 구조화된 sandbox 결과(exit code, `failed_step`) 분석
- Metrics 기반 Pass/Fail 판단

### Critic Agent

Critic Agent는 Judge가 Fail로 판단한 경우 실패 원인을 분석한다.

- 실패 원인 분석 (자연어)
- 로그와 메트릭 기반 근거 추출
- 권장 조치 제시

**고칠 파일을 찾는 일은 Critic이 하지 않는다.** 실패 로그에서 파일 경로를 뽑는 것은
`app/agents/source_context.py`가 결정적으로 한다. 로그가 파일명을 말해주는데 모델에게 다시 묻는
것은 틀릴 여지만 만든다.

Critic의 기여는 측정했다(docs/evaluation-design.md 14.11). Critic을 끄면 수율이 75%에서 62.5%로
내려갔다. 8건 중 1건 차이이므로 통계적으로 확정할 수는 없지만, 갈린 케이스에서 Critic이 없을 때
모델이 엉뚱한 파일(`pyproject.toml`)을 겨냥했다. 원인 서술이 수정 대상을 좁히는 데 쓰인다.

### Refiner Agent

Refiner Agent는 Critic의 분석과 Judge의 판정을 바탕으로 **실행 가능한 수정안**을 만든다.

- 개선 요약 작성
- **편집 목록 생성**: 어느 파일의 어느 줄을 무엇으로 바꿀지
- 재검증 절차 제안
- 위험도 평가

Refiner는 diff를 쓰지 않고 파일 전문도 쓰지 않는다. 바꿀 줄(`find`)과 바꿀 내용(`replace`)만
지목하고, diff 조립은 `app/agents/patching.py`가 한다. 근거는 docs/evaluation-design.md 14.5와
14.7이다 — 8B 모델은 diff 형식(context 줄, hunk 헤더)을 맞추지 못했고, 파일 전문을 요구하면
뒤를 잘라먹어 멀쩡한 코드 31줄이 지워졌다.

`find`는 파일에 정확히 한 번 나타나야 한다. 없으면 지어낸 것이고, 여럿이면 어디인지 알 수 없다.
둘 다 거부한다. 지목하지 않은 줄은 바뀔 수 없다.

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
