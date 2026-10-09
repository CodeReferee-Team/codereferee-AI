"""LLM 서술 품질 채점 (Critic/Refiner/Planner).

판정·원인분류는 규칙 Judge가 정하므로 모델을 바꿔도 안 변한다. 모델 차이는 Critic/Refiner가
쓰는 서술에서만 드러난다 — 개념 커버리지, 환각 evidence, generic 진단, grounding. 그 채점은
tests/agent_quality.py가 이미 구현해 뒀으니(docs/evaluation-design.md 3.2 "채점 함수 재사용")
그대로 import해 쓴다. 여기서는 eval 러너가 고른 모델 출력에 그 채점을 적용할 뿐이다.

각 채점은 try/except로 감싼다. 모델이 깨진 JSON을 내 fallback을 타면 report 스키마가
어긋날 수 있는데, 그걸 eval 전체를 죽이는 대신 schema_ok=False로 기록한다(§3.2 스키마 통과율).
"""
from __future__ import annotations

from typing import Any

from app.models import AgentState
from tests.agent_quality import _score_critic, _score_planner, _score_refiner


def _safe(scorer) -> dict[str, Any]:
    try:
        result = scorer()
        result["schema_ok"] = True
        return result
    except Exception as exc:  # noqa: BLE001 - 깨진 출력도 지표다, 죽이지 않는다
        return {"passed": False, "failures": [f"schema_or_scoring_error: {exc}"], "schema_ok": False}


def score_narrative(
    annotations: dict[str, Any], state: AgentState, packet_text: str
) -> dict[str, Any] | None:
    """적용 가능한 에이전트만 채점한다. 리포트가 비어 있으면(그 단계가 안 돈 경우) 건너뛴다."""
    out: dict[str, Any] = {}
    if annotations.get("planner_grounding") and state.validation_plan:
        out["planner"] = _safe(lambda: _score_planner(annotations, state, packet_text))
    if state.critic_feedback:
        out["critic"] = _safe(lambda: _score_critic(annotations, state, packet_text))
    if state.refiner_report:
        out["refiner"] = _safe(lambda: _score_refiner(annotations, state))
    return out or None
