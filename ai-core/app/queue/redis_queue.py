import json
from typing import Any

from app.config import get_settings

try:
    import redis
except ImportError:  # pragma: no cover - exercised only when deps are missing
    redis = None


class RedisTaskQueue:
    def __init__(self) -> None:
        self.settings = get_settings()

    def _client(self):
        if redis is None:
            raise RuntimeError("redis package is not installed. Run: pip install -r requirements.txt")
        return redis.Redis.from_url(self.settings.redis_url, decode_responses=True)

    def enqueue(self, payload: dict[str, Any]) -> int:
        return self._client().rpush(self.settings.redis_workflow_queue, json.dumps(payload))

    def dequeue(self) -> dict[str, Any] | None:
        raw = self._client().lpop(self.settings.redis_workflow_queue)
        return json.loads(raw) if raw else None


redis_task_queue = RedisTaskQueue()
