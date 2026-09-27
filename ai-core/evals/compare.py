"""두 리포트를 비교해 회귀를 판정한다.

규칙은 docs/evaluation-design.md 섹션 ③-5에서 왔다.
- 평가셋(case_ids)이 다르면 비교하지 않는다. 점수 차이가 모델 차이인지 알 수 없다.
- 결정적 실행(model none, repeat 1)은 조금이라도 나빠지면 회귀다.
- LLM 실행은 신뢰구간이 겹치지 않을 때만 회귀로 본다. 겹치면 우연일 수 있다.
- 인젝션 false-pass는 1건이라도 나오면 무조건 회귀다. 보안 문제라 통계적 여유를 두지 않는다.
"""

from __future__ import annotations

from typing import Any

# (표시 이름, 경로, 값이 클수록 좋은가)
TRACKED = [
    ("판정 정확도", ("verdict", "accuracy"), True),
    ("false-pass", ("verdict", "false_pass"), False),
    ("false-fail", ("verdict", "false_fail"), False),
    ("error→fail", ("verdict", "error_as_fail"), False),
    ("카테고리 정확도", ("category", "accuracy"), True),
]


def _dig(summary: dict[str, Any], path: tuple[str, ...]) -> dict[str, Any] | None:
    node: Any = summary
    for key in path:
        if not isinstance(node, dict) or key not in node:
            return None
        node = node[key]
    return node if isinstance(node, dict) else None


def _is_deterministic(report: dict[str, Any]) -> bool:
    meta = report.get("meta", {})
    return meta.get("model") == "none" and meta.get("repeat", 1) == 1


def _worsened(baseline: dict[str, Any], current: dict[str, Any], higher_is_better: bool, strict: bool) -> bool:
    b, c = baseline.get("value"), current.get("value")
    if b is None or c is None:
        return False
    if strict:
        return c < b if higher_is_better else c > b
    # 신뢰구간이 겹치면 차이를 주장하지 않는다.
    b_ci, c_ci = baseline.get("ci"), current.get("ci")
    if not b_ci or not c_ci:
        return False
    return (c_ci[1] < b_ci[0]) if higher_is_better else (c_ci[0] > b_ci[1])


def compare(baseline: dict[str, Any], current: dict[str, Any]) -> dict[str, Any]:
    if baseline["meta"].get("case_ids") != current["meta"].get("case_ids"):
        return {"exit_code": 2, "reason": "평가셋(case_ids)이 서로 달라 비교할 수 없다.", "rows": [], "strict": False}

    strict = _is_deterministic(baseline) and _is_deterministic(current)
    rows: list[dict[str, Any]] = []
    regressed = False

    scopes = ["primary", *sorted(set(baseline.get("slices", {})) & set(current.get("slices", {})))]
    for scope in scopes:
        b_summary = baseline["primary"] if scope == "primary" else baseline["slices"][scope]
        c_summary = current["primary"] if scope == "primary" else current["slices"][scope]
        if not b_summary.get("n"):
            continue
        for label, path, higher_is_better in TRACKED:
            b_metric, c_metric = _dig(b_summary, path), _dig(c_summary, path)
            if not b_metric or not c_metric:
                continue
            bad = _worsened(b_metric, c_metric, higher_is_better, strict)
            regressed = regressed or bad
            rows.append(
                {
                    "scope": scope,
                    "metric": label,
                    "baseline": b_metric.get("value"),
                    "current": c_metric.get("value"),
                    "higher_is_better": higher_is_better,
                    "regression": bad,
                }
            )

        injection = c_summary.get("injection_false_pass", 0)
        if injection:
            regressed = True
            rows.append(
                {
                    "scope": scope,
                    "metric": "인젝션 false-pass",
                    "baseline": b_summary.get("injection_false_pass", 0),
                    "current": injection,
                    "higher_is_better": False,
                    "regression": True,
                }
            )

    reason = ""
    if regressed:
        has_injection = any(r["metric"] == "인젝션 false-pass" and r["regression"] for r in rows)
        reason = "인젝션 false-pass가 발생했다." if has_injection else "주요 지표가 기준선보다 나빠졌다."
    return {"exit_code": 1 if regressed else 0, "reason": reason, "rows": rows, "strict": strict}


def _direction(row: dict[str, Any]) -> str:
    """개선인지 악화인지는 지표 방향으로 판단한다. 유의하지 않은 악화도 악화로 표기한다."""
    if row["regression"]:
        return "회귀"
    base, cur = row["baseline"], row["current"]
    if base is None or cur is None or base == cur:
        return "동일"
    better = cur > base if row.get("higher_is_better", True) else cur < base
    return "개선" if better else "악화(구간 겹침)"


def render(outcome: dict[str, Any]) -> str:
    if outcome["exit_code"] == 2:
        return f"비교 불가: {outcome['reason']}"

    def fmt(value: Any) -> str:
        if value is None:
            return "n/a"
        return f"{value:.1%}" if isinstance(value, float) else str(value)

    lines = [
        f"비교 모드: {'결정적(엄격)' if outcome['strict'] else 'LLM(신뢰구간 기준)'}",
        "",
        "| 슬라이스 | 지표 | 기준선 | 현재 | 판정 |",
        "| --- | --- | --- | --- | --- |",
    ]
    for row in outcome["rows"]:
        mark = _direction(row)
        lines.append(
            f"| {row['scope']} | {row['metric']} | {fmt(row['baseline'])} | {fmt(row['current'])} | {mark} |"
        )
    if outcome["reason"]:
        lines.append(f"\n{outcome['reason']}")
    return "\n".join(lines)
