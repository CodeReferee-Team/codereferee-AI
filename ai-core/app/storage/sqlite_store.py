from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.config import get_settings
from app.models import AgentState


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class SQLitePatchStore:
    def __init__(self, db_path: str | Path | None = None) -> None:
        self.db_path = Path(db_path or get_settings().sqlite_patch_db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        # API와 worker가 다른 프로세스에서 동시에 접근하므로 WAL로 읽기-쓰기 충돌을 줄인다.
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    def _init_schema(self) -> None:
        schema_path = Path(__file__).with_name("schema.sql")
        with self._connect() as conn:
            conn.executescript(schema_path.read_text(encoding="utf-8"))

    def save_validation_run(self, state: AgentState) -> int:
        judge_status = str(state.judge_report.get("status")) if state.judge_report else None
        reason_category = _reason_category(state)
        with self._connect() as conn:
            cursor = conn.execute(
                """
                INSERT INTO validation_runs (
                  job_id, repository_url, branch, commit_sha,
                  status, judge_status, reason_category, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    state.job_id,
                    state.repository_url,
                    state.branch,
                    state.resolved_commit_sha or state.requested_commit_sha,
                    str(state.status),
                    judge_status,
                    reason_category,
                    _utc_now(),
                ),
            )
            return int(cursor.lastrowid)

    def save_patch_suggestion(self, *, run_id: int, state: AgentState) -> int:
        critic = state.critic_feedback or {}
        refiner = state.refiner_report or {}
        patch_guidance = refiner.get("patch_guidance", [])
        if isinstance(patch_guidance, list):
            patch_summary = "\n".join(str(item) for item in patch_guidance)
        else:
            patch_summary = str(patch_guidance)
        with self._connect() as conn:
            cursor = conn.execute(
                """
                INSERT INTO patch_suggestions (
                  run_id, target_file, issue, root_cause,
                  patch_summary, patch_diff, risk, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    None,
                    _string_or_none(critic.get("issue")),
                    _string_or_none(critic.get("root_cause")),
                    patch_summary,
                    None,
                    _string_or_none(refiner.get("risk")),
                    _utc_now(),
                ),
            )
            return int(cursor.lastrowid)

    def save_rerun_result(
        self,
        *,
        patch_id: int,
        before_judge_status: str | None,
        after_judge_status: str | None,
        before_error_rate: float | None,
        after_error_rate: float | None,
        before_p95_latency_ms: float | None,
        after_p95_latency_ms: float | None,
        improved: bool,
    ) -> int:
        with self._connect() as conn:
            cursor = conn.execute(
                """
                INSERT INTO rerun_results (
                  patch_id, before_judge_status, after_judge_status,
                  before_error_rate, after_error_rate,
                  before_p95_latency_ms, after_p95_latency_ms,
                  improved, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    patch_id,
                    before_judge_status,
                    after_judge_status,
                    before_error_rate,
                    after_error_rate,
                    before_p95_latency_ms,
                    after_p95_latency_ms,
                    1 if improved else 0,
                    _utc_now(),
                ),
            )
            return int(cursor.lastrowid)


class SQLiteJobStore(SQLitePatchStore):
    """검증 job 상태 저장소.

    API 프로세스와 worker 프로세스가 분리돼 있어 메모리 dict로는 서로의 job을 볼 수 없다.
    같은 SQLite 파일을 공유해 worker가 처리한 job도 GET /jobs/{job_id}로 조회된다.
    """

    def save(self, state: AgentState) -> AgentState:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO validation_jobs (job_id, request_id, status, updated_at, state_json)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(job_id) DO UPDATE SET
                    request_id = excluded.request_id,
                    status = excluded.status,
                    updated_at = excluded.updated_at,
                    state_json = excluded.state_json
                """,
                (state.job_id, state.request_id, str(state.status), _utc_now(), state.model_dump_json()),
            )
        return state

    def get(self, job_id: str) -> AgentState | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT state_json FROM validation_jobs WHERE job_id = ?", (job_id,)
            ).fetchone()
        return AgentState.model_validate_json(row["state_json"]) if row else None


job_store = SQLiteJobStore()


def record_validation_artifacts(state: AgentState) -> None:
    store = SQLitePatchStore()
    run_id = store.save_validation_run(state)
    if state.critic_feedback or state.refiner_report:
        store.save_patch_suggestion(run_id=run_id, state=state)


def _reason_category(state: AgentState) -> str | None:
    for source in (state.judge_report, state.critic_feedback):
        value = source.get("reason_category") if source else None
        if value:
            return str(value)
    return None


def _string_or_none(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value)
    return text if text.strip() else None
