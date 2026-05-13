import tempfile
import time
from pathlib import Path

import docker
from docker.errors import DockerException

from app.config import get_settings
from app.models import SandboxResult


class SandboxRunner:
    def __init__(self) -> None:
        self.settings = get_settings()

    def run_python(self, code: str) -> SandboxResult:
        started_at = time.monotonic()
        with tempfile.TemporaryDirectory(prefix="codereferee-") as tmp:
            workdir = Path(tmp)
            script_path = workdir / "submission.py"
            script_path.write_text(code, encoding="utf-8")

            try:
                client = docker.from_env()
                container = client.containers.run(
                    image=self.settings.sandbox_image,
                    command=["python", "/workspace/submission.py"],
                    detach=True,
                    network_disabled=False,
                    mem_limit=self.settings.sandbox_memory_limit,
                    nano_cpus=self.settings.sandbox_nano_cpus,
                    pids_limit=64,
                    read_only=True,
                    volumes={str(workdir): {"bind": "/workspace", "mode": "ro"}},
                    working_dir="/workspace",
                )
                try:
                    wait_result = container.wait(timeout=self.settings.sandbox_timeout_seconds)
                    exit_code = int(wait_result.get("StatusCode", 1))
                    timed_out = False
                except Exception:
                    container.kill()
                    exit_code = None
                    timed_out = True

                logs = container.logs(stdout=True, stderr=True).decode("utf-8", errors="replace")
                container.remove(force=True)
                return SandboxResult(
                    exit_code=exit_code,
                    stdout=logs if exit_code == 0 else "",
                    stderr="" if exit_code == 0 else logs,
                    timed_out=timed_out,
                    duration_ms=_duration_ms(started_at),
                )
            except DockerException as exc:
                return SandboxResult(
                    exit_code=None,
                    stderr=f"Docker sandbox error: {exc}",
                    duration_ms=_duration_ms(started_at),
                )


def _duration_ms(started_at: float) -> int:
    return int((time.monotonic() - started_at) * 1000)


sandbox_runner = SandboxRunner()
