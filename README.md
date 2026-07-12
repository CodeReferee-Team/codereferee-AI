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
- 재검증 절차 제안
- 위험도 평가
  
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
