"""평가셋을 하나의 EvalCase 형태로 정규화한다.

슬라이스를 섞지 않는다. 사람이 만든 T0/T0-adv/T1-chaos가 주 지표이고,
템플릿으로 생성한 T1-*은 회귀 감시용으로 따로 집계한다(docs/evaluation-design.md 3절).
"""

from __future__ import annotations

import json
import pathlib
import random
from dataclasses import dataclass, field
from typing import Any

from app.models import SLO, AgentState, RepositoryPreflightReport, SandboxResult
from app.workflow import repository_validation as workflow

AI_CORE = pathlib.Path(__file__).resolve().parents[1]
FIXTURES = AI_CORE / "tests" / "fixtures"
GENERATED = AI_CORE.parent / "datasets" / "codereferee" / "generated"

HUMAN_SLICES = {
    "T0": FIXTURES / "agent_golden_cases.json",
    "T0-adv": FIXTURES / "agent_adversarial_cases.json",
    "T1-chaos": FIXTURES / "agent_chaos_cases.json",
}
SYNTHETIC_SLICES = {
    "T1-sandbox": GENERATED / "sandbox_failures.jsonl",
    "T1-metrics": GENERATED / "metrics_judge_cases.jsonl",
}

# 생성 데이터 라벨 중 인프라 문제로 재분류할 것들. judge-policy 6절 기준.
INFRA_CATEGORIES = {"sandbox_environment_error", "rate_limited", "network_unreachable"}
# 사용자 코드 탓인지 환경 탓인지 정할 수 없는 것. 판정 정확도에서 빼고 사람 검수로 보낸다.
AMBIGUOUS_CATEGORIES = {"db_dependency_unavailable", "redis_dependency_unavailable"}


@dataclass
class EvalCase:
    id: str
    slice: str
    expected: dict[str, Any]
    raw_state: dict[str, Any]
    expected_warnings: list[str] = field(default_factory=list)
    group: str | None = None
    injection: bool = False
    ambiguous: bool = False
    note: str | None = None
    slo: dict[str, Any] | None = None

    def build_state(self) -> AgentState:
        state = AgentState(job_id=self.id, repository_url=self.raw_state["repository_url"])
        if pf := self.raw_state.get("preflight_report"):
            state.preflight_report = RepositoryPreflightReport(**pf)
        if ex := self.raw_state.get("execution_result"):
            state.execution_result = SandboxResult(**ex)
        # 워크플로와 같은 순서로 지표를 채워야 Judge가 운영과 동일한 입력을 본다.
        state.metrics = workflow._metrics_from_execution(state)
        state.sre_metrics = workflow._sre_metrics_from_execution(state)
        if self.slo:
            # 케이스가 자기 SLO를 들고 오면 그것으로 판정해야 한다.
            # 기본 SLO로 재면 데이터셋이 의도한 위반이 전혀 잡히지 않는다.
            state.sre_metrics.slo = SLO(**self.slo)
        return state


def _human_cases(slice_name: str) -> list[EvalCase]:
    raw = json.loads(HUMAN_SLICES[slice_name].read_text(encoding="utf-8"))
    return [
        EvalCase(
            id=c["id"],
            slice=slice_name,
            expected=dict(c["label"]),
            raw_state=c["state"],
            expected_warnings=c.get("expected_warnings", []),
            group=c.get("group"),
            injection=bool(c.get("injection")),
            note=c.get("needs_sandbox_field"),
        )
        for c in raw
    ]


def _sandbox_failure_case(row: dict[str, Any]) -> EvalCase:
    category = row["expected_failure_type"]
    return EvalCase(
        id=row["case_id"],
        slice="T1-sandbox",
        expected={
            "verdict": "Error" if category in INFRA_CATEGORIES else row["expected_judge_status"],
            "stage": "sandbox",
            "category": category,
        },
        raw_state={
            "repository_url": "https://github.com/example/generated.git",
            "preflight_report": {
                "repository_url": "https://github.com/example/generated.git",
                "cloneable": row["preflight_report"]["cloneable"],
                "executable": row["preflight_report"]["executable"],
                "detected_stack": row["preflight_report"].get("detected_stack"),
                "reason": "reachable",
            },
            "execution_result": row["execution_result"],
        },
        ambiguous=category in AMBIGUOUS_CATEGORIES,
    )


SLO_FIELD_MAP = {
    "p95_latency_ms_max": "p95_latency_ms_max",
    "error_rate_max": "error_rate_max",
    "availability_min": "availability_percent_min",
}


def _slo_from_row(row: dict[str, Any]) -> dict[str, Any]:
    """데이터셋의 slo 블록을 SLO 모델 필드로 옮긴다. availability는 비율이라 percent로 환산한다."""
    raw = row.get("slo") or {}
    slo: dict[str, Any] = {}
    for src, dst in SLO_FIELD_MAP.items():
        if (value := raw.get(src)) is None:
            continue
        slo[dst] = value * 100 if src == "availability_min" else value
    return slo


def _metrics_judge_case(row: dict[str, Any]) -> EvalCase:
    sandbox = row["sandbox"]
    return EvalCase(
        id=row["case_id"],
        slice="T1-metrics",
        expected={
            "verdict": row["expected_judge_status"],
            "stage": "metrics",
            "category": row["expected_reason_category"],
        },
        raw_state={
            "repository_url": "https://github.com/example/generated.git",
            "preflight_report": {
                "repository_url": "https://github.com/example/generated.git",
                "cloneable": True,
                "executable": True,
                "reason": "reachable",
            },
            "execution_result": {
                "exit_code": sandbox.get("exit_code"),
                "timed_out": sandbox.get("timed_out", False),
                "duration_ms": 1000,
                "metrics": row["metrics"],
            },
        },
        slo=_slo_from_row(row),
    )


def _synthetic_cases(slice_name: str, per_category: int, seed: int) -> list[EvalCase]:
    path = SYNTHETIC_SLICES[slice_name]
    builder = _sandbox_failure_case if slice_name == "T1-sandbox" else _metrics_judge_case
    cases = [builder(json.loads(line)) for line in path.read_text(encoding="utf-8").splitlines() if line]

    # 카테고리별 층화 추출. 전체에서 무작위로 뽑으면 흔한 카테고리가 결과를 지배한다.
    buckets: dict[str, list[EvalCase]] = {}
    for case in cases:
        buckets.setdefault(case.expected["category"], []).append(case)

    rng = random.Random(seed)
    picked: list[EvalCase] = []
    for category in sorted(buckets):
        bucket = sorted(buckets[category], key=lambda c: c.id)
        picked.extend(bucket if len(bucket) <= per_category else rng.sample(bucket, per_category))
    return sorted(picked, key=lambda c: c.id)


def load(slices: list[str], *, per_category: int = 3, seed: int = 7) -> list[EvalCase]:
    """슬라이스 이름 목록을 받아 EvalCase 목록을 돌려준다."""
    loaded: list[EvalCase] = []
    for name in slices:
        if name in HUMAN_SLICES:
            loaded.extend(_human_cases(name))
        elif name in SYNTHETIC_SLICES:
            loaded.extend(_synthetic_cases(name, per_category, seed))
        else:
            raise ValueError(f"알 수 없는 슬라이스: {name}. 가능한 값: {available()}")
    return loaded


def available() -> list[str]:
    return sorted(HUMAN_SLICES) + sorted(SYNTHETIC_SLICES)
