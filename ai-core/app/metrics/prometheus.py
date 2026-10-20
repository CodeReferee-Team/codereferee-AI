"""외부 Prometheus에서 자원 지표(cpu/memory)를 읽어 채운다.

샌드박스는 Prometheus Agent로 cAdvisor/Node Exporter를 1초 간격 수집해 바깥 장수
Prometheus로 remote_write만 한다(질의 API 없음). 모든 샘플에는 external label
`codereferee_request_id`가 붙는다. 그래서 cpu/memory는 pod UID가 아니라 request_id로
장애 구간에서 뽑는다 — 샌드박스가 요청마다 사라져도, pod가 교체돼도 질의가 안정적이다.

판정에 쓸 PromQL과 임계값은 Judge(여기)의 책임이다(샌드박스 metrics-collection 문서가
명시). availability·error_rate·p95는 HTTP probe로 이미 들어오므로 여기서 다루지 않는다.

prometheus_url이 없으면(연동 전) 아무것도 하지 않는다. 어떤 질의 실패도 파이프라인을
막지 않는다 — 채우지 못하면 cpu/memory는 null로 남고, 판정은 없는 지표를 쓰지 않는다.
"""

from __future__ import annotations

import json
import logging
import urllib.parse
import urllib.request
from datetime import datetime

from app.config import Settings

logger = logging.getLogger(__name__)

# 수집되는 cAdvisor 메트릭(샌드박스 keep 목록 기준). 이 이름에 의존한다.
_CPU_SECONDS = "container_cpu_usage_seconds_total"
_MEM_WORKING_SET = "container_memory_working_set_bytes"


def enrich_resource_metrics(metrics: dict[str, object], request_id: str | None,
                            chaos_observation: dict | None, settings: Settings) -> None:
    """cpu_usage_percent / memory_usage_mb가 비어 있으면 Prometheus에서 채운다.

    metrics를 제자리에서 수정한다. 전제가 안 맞으면(설정 없음·request_id 없음·창 없음·
    이미 값 있음) 조용히 넘어간다. best-effort — 예외를 밖으로 던지지 않는다.
    """
    if not settings.prometheus_url or not request_id:
        return
    window = _window_seconds(chaos_observation)
    if window is None:
        return  # 장애 구간을 모르면 어느 구간의 자원을 봐야 할지 알 수 없다.

    try:
        if _is_missing(metrics.get("cpu_usage_percent")):
            cpu = _peak_cpu_percent(request_id, window, settings)
            if cpu is not None:
                metrics["cpu_usage_percent"] = cpu
        if _is_missing(metrics.get("memory_usage_mb")):
            mem = _peak_memory_mb(request_id, window, settings)
            if mem is not None:
                metrics["memory_usage_mb"] = mem
    except Exception as e:  # noqa: BLE001 - 발송처럼 판정 저장을 막으면 안 된다
        logger.warning("Prometheus 자원 지표 조회 실패 (request_id=%s): %s", request_id, e)


def _peak_cpu_percent(request_id: str, window: int, settings: Settings) -> float | None:
    """장애 구간의 CPU 사용률 최대치(%). 코어 사용량 rate를 퍼센트로 환산한 피크."""
    rate = settings.prometheus_rate_window_seconds
    promql = (
        f'max_over_time('
        f'sum(rate({_CPU_SECONDS}{{codereferee_request_id="{request_id}"}}[{rate}s]))'
        f'[{window}s:1s]) * 100'
    )
    return _query_scalar(promql, settings)


def _peak_memory_mb(request_id: str, window: int, settings: Settings) -> float | None:
    """장애 구간의 메모리 working set 최대치(MB)."""
    promql = (
        f'max_over_time('
        f'sum({_MEM_WORKING_SET}{{codereferee_request_id="{request_id}"}})'
        f'[{window}s:1s]) / 1000000'
    )
    return _query_scalar(promql, settings)


def _query_scalar(promql: str, settings: Settings) -> float | None:
    """instant query를 던져 스칼라/단일 벡터 값을 float로 돌려준다. 없으면 None."""
    base = settings.prometheus_url.rstrip("/")
    url = f"{base}/api/v1/query?" + urllib.parse.urlencode({"query": promql})
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=settings.prometheus_timeout_seconds) as resp:
        payload = json.loads(resp.read().decode("utf-8"))
    if payload.get("status") != "success":
        return None
    data = payload.get("data", {})
    result = data.get("result")
    rtype = data.get("resultType")
    if rtype == "scalar" and isinstance(result, list) and len(result) == 2:
        return _as_float(result[1])
    if rtype == "vector" and result:
        value = result[0].get("value")
        if isinstance(value, list) and len(value) == 2:
            return _as_float(value[1])
    return None


def _window_seconds(chaos_observation: dict | None) -> int | None:
    """장애 관측 구간 길이(초). started_at~recovered_at. 못 구하면 None."""
    if not chaos_observation:
        return None
    start = _parse_time(chaos_observation.get("started_at"))
    end = _parse_time(chaos_observation.get("recovered_at"))
    if start is None or end is None:
        return None
    seconds = int((end - start).total_seconds())
    return seconds if seconds > 0 else None


def _parse_time(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _is_missing(value: object) -> bool:
    return value is None


def _as_float(value: object) -> float | None:
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    # Prometheus는 NaN/Inf를 문자열로 준다. 판정에 넣지 않는다.
    return f if f == f and f not in (float("inf"), float("-inf")) else None
