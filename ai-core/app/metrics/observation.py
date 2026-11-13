"""샌드박스 응답에 실려 오는 자원 시계열로 cpu/memory 요약값을 채운다.

샌드박스는 Prometheus Agent가 remote_write한 샘플을 실험이 끝난 뒤 range 질의로 다시
읽어 `source.metrics_observation`에 컨테이너별 `cpu_cores`(코어 사용량 rate)와
`memory_working_set_bytes`를 1초 간격으로 싣는다. 요약값(`cpu_usage_percent`,
`memory_usage_mb`)은 null로 보낸다. 여기서 그 시계열을 장애 구간으로 잘라 최대치를 낸다.

단위는 prometheus.py의 PromQL과 같다. CPU는 코어 합 × 100(1코어 = 100%),
메모리는 working set 합 / 1e6(MB). 같은 시각의 파드 값은 더한다.

장애 구간은 chaos_observation의 started_at부터 recovered_at까지다. 복구하지 못했으면
관측 끝까지 본다. 구간을 모르면 채우지 않는다. 기동 직후 스파이크까지 섞인 전체 최대로
메우면 운영자 임계값이 걸렸을 때 근거 없는 Fail이 난다.
"""

from __future__ import annotations

import re
from datetime import datetime

_UNITS = {"": 1, "Ki": 2**10, "Mi": 2**20, "Gi": 2**30, "Ti": 2**40,
          "k": 10**3, "M": 10**6, "G": 10**9, "T": 10**12}


def fill_resource_metrics(metrics: dict[str, object], metrics_observation: object,
                          chaos_observation: dict | None) -> None:
    """metrics를 제자리에서 채운다. 이미 값이 있으면 건드리지 않는다. 예외를 던지지 않는다."""
    if not isinstance(metrics_observation, dict):
        return
    limit = _memory_limit_mb(metrics_observation.get("pod_bindings"))
    if limit is not None and metrics.get("memory_limit_mb") is None:
        metrics["memory_limit_mb"] = limit

    windows = _fault_windows(chaos_observation or {}, _as_float(metrics_observation.get("ended_at")))
    if not windows:
        return
    if metrics.get("cpu_usage_percent") is None:
        peak = _peak(metrics_observation.get("cpu_cores"), windows)
        if peak is not None:
            metrics["cpu_usage_percent"] = round(peak * 100, 4)
    if metrics.get("memory_usage_mb") is None:
        peak = _peak(metrics_observation.get("memory_working_set_bytes"), windows)
        if peak is not None:
            metrics["memory_usage_mb"] = round(peak / 1_000_000, 6)


def _fault_windows(chaos: dict, observation_end: float | None) -> list[tuple[float, float]]:
    """(시작, 끝) epoch 초 목록. 묶음 실행이면 시나리오마다 하나씩."""
    observations = [s.get("chaos_observation") for s in chaos.get("scenarios") or [] if isinstance(s, dict)]
    if not observations:
        observations = [chaos]
    windows = []
    for obs in observations:
        if not isinstance(obs, dict):
            continue
        start = _epoch(obs.get("started_at"))
        end = _epoch(obs.get("recovered_at")) or observation_end
        if start is not None and end is not None and end > start:
            windows.append((start, end))
    return windows


def _peak(series: object, windows: list[tuple[float, float]]) -> float | None:
    if not isinstance(series, list):
        return None
    totals: dict[float, float] = {}
    for entry in series:
        values = entry.get("values") if isinstance(entry, dict) else None
        for point in values if isinstance(values, list) else []:
            if not isinstance(point, list) or len(point) != 2:
                continue
            t, v = _as_float(point[0]), _as_float(point[1])
            if t is None or v is None:
                continue
            if any(start <= t <= end for start, end in windows):
                totals[t] = totals.get(t, 0.0) + v
    return max(totals.values()) if totals else None


def _memory_limit_mb(bindings: object) -> float | None:
    """모든 파드가 같은 메모리 한도를 가질 때만 그 값(MB). 다르거나 없으면 None."""
    if not isinstance(bindings, list) or not bindings:
        return None
    limits = set()
    for b in bindings:
        raw = ((b.get("resources") or {}).get("limits") or {}).get("memory") if isinstance(b, dict) else None
        parsed = _quantity_bytes(raw)
        if parsed is None:
            return None
        limits.add(parsed)
    return limits.pop() / 1_000_000 if len(limits) == 1 else None


def _quantity_bytes(value: object) -> float | None:
    match = re.fullmatch(r"(\d+(?:\.\d+)?)(Ki|Mi|Gi|Ti|k|M|G|T)?", str(value)) if value is not None else None
    if not match:
        return None
    return float(match.group(1)) * _UNITS[match.group(2) or ""]


def _epoch(value: object) -> float | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _as_float(value: object) -> float | None:
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return f if f == f and f not in (float("inf"), float("-inf")) else None
