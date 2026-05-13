from threading import Lock

from app.models import AgentState


class InMemoryJobStore:
    def __init__(self) -> None:
        self._jobs: dict[str, AgentState] = {}
        self._lock = Lock()

    def save(self, state: AgentState) -> AgentState:
        with self._lock:
            self._jobs[state.job_id] = state.model_copy(deep=True)
            return self._jobs[state.job_id]

    def get(self, job_id: str) -> AgentState | None:
        with self._lock:
            state = self._jobs.get(job_id)
            return state.model_copy(deep=True) if state else None


job_store = InMemoryJobStore()
