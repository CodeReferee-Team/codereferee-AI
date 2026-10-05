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
from app.sandbox.docker_runner import _sandbox_result_from_response
from app.workflow import repository_validation as workflow

AI_CORE = pathlib.Path(__file__).resolve().parents[1]
FIXTURES = AI_CORE / "tests" / "fixtures"
GENERATED = AI_CORE.parent / "datasets" / "codereferee" / "generated"

HUMAN_SLICES = {
    "T0": FIXTURES / "agent_golden_cases.json",
    "T0-adv": FIXTURES / "agent_adversarial_cases.json",
    "T1-chaos": FIXTURES / "agent_chaos_cases.json",
}
# 실측 슬라이스. 케이스 본문을 적지 않고 sandbox가 실제로 보낸 파일을 그대로 읽는다.
# 손으로 옮기면 합성 슬라이스가 그랬듯 계약 문서를 베껴 쓰게 된다.
REAL_CHAOS_LABELS = FIXTURES / "agent_chaos_real_labels.json"
REAL_CHAOS_EVIDENCE = FIXTURES / "chaos_actual"
SYNTHETIC_SLICES = {
    "T1-sandbox": GENERATED / "sandbox_failures.jsonl",
    "T1-metrics": GENERATED / "metrics_judge_cases.jsonl",
}

# 생성 데이터 라벨 중 인프라 문제로 재분류할 것들. judge-policy 6절 기준.
INFRA_CATEGORIES = {
    "sandbox_environment_error",
    "rate_limited",
    "network_unreachable",
    "docker_daemon_unavailable",
}
# 사용자 코드 탓인지 환경 탓인지 정할 수 없는 것. 판정 정확도에서 빼고 사람 검수로 보낸다.
# 종료 코드 137(OOMKilled)과 CPU 쿼터는 레포가 무거운 것인지 우리가 건 한도가 낮은 것인지
# 증거만으로 가릴 수 없다. sandbox_memory_limit과 sandbox_nano_cpus가 우리 설정이기 때문이다.
AMBIGUOUS_CATEGORIES = {
    "db_dependency_unavailable",
    "redis_dependency_unavailable",
    "memory_limit_exceeded",
    "cpu_quota_exceeded",
}

# 생성 데이터셋은 우리 reason_category보다 잘게 라벨링했고 같은 뜻에 이름이 둘인 경우도 있다
# (dockerfile_missing과 missing_dockerfile). 이름만 다른 것은 정식 코드로 옮긴다.
# 근거는 각 케이스의 종료 코드다. 추측으로 옮기지 않는다.
CATEGORY_SYNONYMS = {
    # exit 124, timed_out=True
    "sandbox_timeout": "timeout",
    # 의존성 해결 실패
    "npm_install_failed": "dependency_install_failed",
    "package_lock_mismatch": "dependency_install_failed",
    # 테스트가 돌았고 실패했다
    "pytest_failure": "test_failure",
    "gradle_test_failed": "test_failure",
    "maven_test_failed": "test_failure",
    # exit 86 (manifest 없음)
    "dockerfile_missing": "no_manifest_detected",
    "missing_dockerfile": "no_manifest_detected",
    "unsupported_stack": "unsupported_project_stack",
    # exit 87 (러너나 실행 커맨드 없음). 데이터셋 생성 시점에는 89가 없어서 전부 87로 찍혔다.
    "npm_test_missing": "unsupported_project_stack",
    "missing_node_script": "unsupported_project_stack",
    "no_smoke_command": "unsupported_project_stack",
    "no_tests_detected": "unsupported_project_stack",
    "gradle_permission_denied": "unsupported_project_stack",
    "gradle_wrapper_permission_denied": "unsupported_project_stack",
    "maven_permission_denied": "unsupported_project_stack",
    "maven_wrapper_permission_denied": "unsupported_project_stack",
}

# 우리 분류로 표현할 수 없는 라벨. 데이터셋이 더 잘게 나눈 것이고, 규칙은 exit code와 구조화된
# 결과만 보므로 이 구분을 만들 수 없다(judge-policy 8절). 정식 코드로 억지로 옮기면 정답을
# 우리가 정하는 셈이 되므로 그대로 두고 틀린 것으로 센다. 카테고리 정확도의 상한이 이것이다.
UNMAPPED_CATEGORIES = {
    "syntax_error",
    "pip_compile_error",
    "entrypoint_crash",
    "entrypoint_import_error",
    "port_bind_failure",
    "permission_denied_runtime",
    "missing_env",
    "missing_environment_config",
    "missing_settings_gradle",
    "postgres_unavailable",
    "redis_unavailable",
}


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
        if body := self.raw_state.get("sandbox_response"):
            # 운영과 같은 HTTP 파서를 통과시킨다. 필드 이름이 어긋나면 여기서 드러난다.
            state.execution_result = _sandbox_result_from_response(body, 0.0)
        elif ex := self.raw_state.get("execution_result"):
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


def _real_chaos_cases() -> list[EvalCase]:
    url = "https://github.com/phdcoco/QuickByte_Demo.git"
    labels = json.loads(REAL_CHAOS_LABELS.read_text(encoding="utf-8"))
    built: list[EvalCase] = []
    for entry in labels:
        path = REAL_CHAOS_EVIDENCE / f"{entry['id']}.json"
        if not path.is_file():
            raise FileNotFoundError(f"실측 증거 파일이 없다: {path}")
        built.append(
            EvalCase(
                id=entry["id"],
                slice="T1-chaos-real",
                expected=dict(entry["label"]),
                raw_state={
                    "repository_url": url,
                    "preflight_report": {
                        "repository_url": url,
                        "cloneable": True,
                        "executable": True,
                        "reason": "reachable",
                    },
                    "sandbox_response": path.read_text(encoding="utf-8"),
                },
                expected_warnings=entry.get("expected_warnings", []),
                note=entry.get("note"),
            )
        )
    return built


def _sandbox_failure_case(row: dict[str, Any]) -> EvalCase:
    category = row["expected_failure_type"]
    execution = dict(row["execution_result"])
    if category in INFRA_CATEGORIES:
        # 운영 경로는 이때 infra_error를 채운다(docker_runner의 docker_daemon_unreachable).
        # 데이터셋은 그 필드가 생기기 전에 만들어져 비어 있다. 판정이 운영에서 받는 입력을
        # 평가에서도 받아야 한다. 아니면 만들 수 없는 답을 요구하게 된다.
        execution.setdefault("infra_error", category)
    return EvalCase(
        id=row["case_id"],
        slice="T1-sandbox",
        expected={
            "verdict": "Error" if category in INFRA_CATEGORIES else row["expected_judge_status"],
            "stage": "sandbox",
            "category": CATEGORY_SYNONYMS.get(category, category),
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
            "execution_result": execution,
        },
        ambiguous=category in AMBIGUOUS_CATEGORIES,
        note=f"category_unmapped={category}" if category in UNMAPPED_CATEGORIES else None,
    )


SLO_FIELD_MAP = {
    "p95_latency_ms_max": "p95_latency_ms_max",
    "error_rate_max": "error_rate_max",
    "availability_min": "availability_percent_min",
    "cpu_usage_percent_max": "cpu_usage_percent_max",
    "memory_usage_ratio_max": "memory_usage_ratio_max",
    "restart_count_max": "restart_count_max",
    "db_connection_errors_max": "db_connection_errors_max",
    "redis_connection_errors_max": "redis_connection_errors_max",
    "request_count_min": "request_count_min",
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
        if name == "T1-chaos-real":
            loaded.extend(_real_chaos_cases())
        elif name in HUMAN_SLICES:
            loaded.extend(_human_cases(name))
        elif name in SYNTHETIC_SLICES:
            loaded.extend(_synthetic_cases(name, per_category, seed))
        else:
            raise ValueError(f"알 수 없는 슬라이스: {name}. 가능한 값: {available()}")
    return loaded


def available() -> list[str]:
    return sorted(HUMAN_SLICES) + ["T1-chaos-real"] + sorted(SYNTHETIC_SLICES)
