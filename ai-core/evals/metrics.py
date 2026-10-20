"""평가 지표 계산. 표준 라이브러리만 쓴다.

모든 비율에는 Wilson 95% 신뢰구간을 붙인다. 사람이 만든 평가셋이 40건 남짓이라
구간 없이 비교하면 1건 차이(2~5%p)를 개선으로 착각하기 쉽다.
"""

from __future__ import annotations

import collections
import math
from typing import Any, Iterable, Sequence

Z = 1.959963984540054  # 95% 양측


def proportion(successes: int, total: int) -> tuple[float | None, tuple[float, float] | None]:
    """비율과 Wilson 신뢰구간. 분모가 0이면 (None, None)."""
    if total <= 0:
        return None, None
    p = successes / total
    denom = 1 + Z**2 / total
    center = (p + Z**2 / (2 * total)) / denom
    margin = Z * math.sqrt(p * (1 - p) / total + Z**2 / (4 * total**2)) / denom
    return p, (max(0.0, center - margin), min(1.0, center + margin))


def metric(successes: int, total: int) -> dict[str, Any]:
    value, interval = proportion(successes, total)
    return {"value": value, "ci": list(interval) if interval else None, "n": total}


def verdict_summary(rows: Iterable[dict[str, Any]]) -> dict[str, Any]:
    """판정 지표. rows는 {expected, actual, ambiguous?} 형태."""
    rows = list(rows)
    scored = [r for r in rows if not r.get("ambiguous")]

    non_pass = [r for r in scored if r["expected"] != "Pass"]
    pass_labelled = [r for r in scored if r["expected"] == "Pass"]
    error_labelled = [r for r in scored if r["expected"] == "Error"]

    confusion: dict[str, dict[str, int]] = collections.defaultdict(lambda: collections.defaultdict(int))
    for r in rows:
        confusion[r["expected"]][r["actual"]] += 1

    return {
        # 1순위 지표. 고장인데 Pass를 준 비율.
        "false_pass": metric(sum(1 for r in non_pass if r["actual"] == "Pass"), len(non_pass)),
        "error_as_fail": metric(sum(1 for r in error_labelled if r["actual"] == "Fail"), len(error_labelled)),
        "false_fail": metric(sum(1 for r in pass_labelled if r["actual"] != "Pass"), len(pass_labelled)),
        "accuracy": metric(sum(1 for r in scored if r["actual"] == r["expected"]), len(scored)),
        "confusion": {k: dict(v) for k, v in confusion.items()},
        "ambiguous_excluded": len(rows) - len(scored),
    }


def category_summary(rows: Iterable[dict[str, Any]]) -> dict[str, Any]:
    """원인 분류 지표. macro-F1은 드문 카테고리도 같은 비중으로 센다."""
    rows = [r for r in rows if r.get("expected")]
    if not rows:
        return {"accuracy": metric(0, 0), "macro_f1": None, "top_confusions": []}

    labels = {r["expected"] for r in rows} | {r["actual"] for r in rows if r.get("actual")}
    f1s = []
    for label in labels:
        tp = sum(1 for r in rows if r["expected"] == label and r["actual"] == label)
        fp = sum(1 for r in rows if r["expected"] != label and r["actual"] == label)
        fn = sum(1 for r in rows if r["expected"] == label and r["actual"] != label)
        if tp + fn == 0:  # 정답에 등장하지 않는 라벨은 macro 평균에서 뺀다
            continue
        precision = tp / (tp + fp) if tp + fp else 0.0
        recall = tp / (tp + fn)
        f1s.append(2 * precision * recall / (precision + recall) if precision + recall else 0.0)

    mistakes = collections.Counter(
        (r["expected"], r["actual"]) for r in rows if r["actual"] != r["expected"]
    )
    return {
        "accuracy": metric(sum(1 for r in rows if r["actual"] == r["expected"]), len(rows)),
        "macro_f1": sum(f1s) / len(f1s) if f1s else None,
        "top_confusions": [
            {"expected": e, "actual": a, "count": n} for (e, a), n in mistakes.most_common(5)
        ],
    }


def warning_summary(rows: Iterable[dict[str, Any]]) -> dict[str, Any]:
    """경고 정확도. 경고는 판정에 영향을 주지 않아 verdict 지표로는 드러나지 않는다."""
    rows = [r for r in rows if r.get("expected") is not None]
    hit = sum(1 for r in rows if set(r["expected"]) <= set(r.get("actual") or []))
    return metric(hit, len(rows))


def narrative_summary(score_dicts: Sequence[dict[str, Any]]) -> dict[str, Any] | None:
    """서술 채점 집계. score_dicts는 run["scores"] 모음(각 {critic:{...}, refiner:{...}, planner?}).

    모델 교체 비교의 핵심 지표다 — 판정과 달리 모델에 따라 움직인다. 에이전트별 개념/grounding
    통과율과 스키마 통과율(깨진 JSON 비율)을 낸다.
    """
    agents = ("planner", "critic", "refiner")
    out: dict[str, Any] = {}
    for agent in agents:
        scored = [s[agent] for s in score_dicts if agent in s]
        if not scored:
            continue
        passed = sum(1 for s in scored if s.get("passed"))
        schema_ok = sum(1 for s in scored if s.get("schema_ok"))
        out[agent] = {
            "pass_rate": metric(passed, len(scored)),
            "schema_pass": metric(schema_ok, len(scored)),
            "n": len(scored),
        }
    return out or None


def majority(verdicts: Sequence[str]) -> str:
    return collections.Counter(verdicts).most_common(1)[0][0]


def agreement(verdicts: Sequence[str]) -> float:
    """반복 실행에서 최빈 판정과 일치한 비율."""
    if not verdicts:
        return 0.0
    return collections.Counter(verdicts).most_common(1)[0][1] / len(verdicts)


def group_consistency(groups: dict[str, list[str]]) -> dict[str, Any]:
    """메타모픽 그룹별 판정 일치 여부. 같은 사건의 변형은 같은 판정이어야 한다."""
    graded = {g: v for g, v in groups.items() if len(v) > 1}
    return metric(sum(1 for v in graded.values() if len(set(v)) == 1), len(graded))


def percentiles(values: Sequence[float]) -> dict[str, float | None]:
    if not values:
        return {"p50": None, "p95": None}
    ordered = sorted(values)

    def pick(q: float) -> float:
        idx = min(len(ordered) - 1, max(0, math.ceil(q * len(ordered)) - 1))
        return ordered[idx]

    return {"p50": pick(0.5), "p95": pick(0.95)}
