import shlex
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

    def run_repository(self, repository_url: str, branch: str | None = None, commit_sha: str | None = None) -> SandboxResult:
        """Clone and smoke-test an existing repository inside an isolated Docker sandbox."""
        started_at = time.monotonic()
        with tempfile.TemporaryDirectory(prefix="codereferee-repo-") as tmp:
            workdir = Path(tmp)
            script_path = workdir / "validate_repository.sh"
            script_path.write_text(_repository_validation_script(repository_url, branch, commit_sha), encoding="utf-8")

            try:
                client = docker.from_env()
                container = client.containers.run(
                    image=self.settings.sandbox_image,
                    command=["sh", "/workspace/validate_repository.sh"],
                    detach=True,
                    network_disabled=False,
                    mem_limit=self.settings.sandbox_memory_limit,
                    nano_cpus=self.settings.sandbox_nano_cpus,
                    pids_limit=128,
                    read_only=False,
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
                    stderr=f"Docker repository sandbox error: {exc}",
                    duration_ms=_duration_ms(started_at),
                )


def _repository_validation_script(repository_url: str, branch: str | None, commit_sha: str | None) -> str:
    url = shlex.quote(repository_url)
    branch_clause = f"--branch {shlex.quote(branch)}" if branch else ""
    checkout = f"git checkout {shlex.quote(commit_sha)}" if commit_sha else "true"
    return f"""#!/bin/sh
set -eu

echo "[CodeReferee] installing sandbox clone tools"
apt-get update >/dev/null
apt-get install -y --no-install-recommends git ca-certificates >/dev/null
rm -rf /var/lib/apt/lists/*

echo "[CodeReferee] cloning repository"
git clone --depth 1 {branch_clause} {url} /tmp/repository
cd /tmp/repository
{checkout}

echo "[CodeReferee] resolving commit"
git rev-parse HEAD

echo "[CodeReferee] detecting project stack"
if [ -f pyproject.toml ] || [ -f setup.py ] || [ -f requirements.txt ]; then
  echo "detected_stack=python"
  if [ -f requirements.txt ]; then
    python -m pip install --disable-pip-version-check -r requirements.txt >/dev/null
  fi
  python -m compileall .
  if [ -d tests ]; then
    python -m pip install --disable-pip-version-check pytest >/dev/null
    python -m pytest -q
  fi
elif [ -f build.gradle ] || [ -f settings.gradle ] || [ -f gradlew ]; then
  echo "detected_stack=gradle"
  if [ -x ./gradlew ]; then ./gradlew test --no-daemon; else echo "No Gradle wrapper in sandbox image"; exit 87; fi
elif [ -f pom.xml ] || [ -f mvnw ]; then
  echo "detected_stack=maven"
  if [ -x ./mvnw ]; then ./mvnw test; else echo "No Maven wrapper in sandbox image"; exit 87; fi
elif [ -f package.json ]; then
  echo "detected_stack=node"
  echo "Node execution is not enabled in the current Python sandbox image"
  exit 87
else
  echo "No supported project manifest found"
  exit 86
fi

echo "[CodeReferee] repository smoke validation completed"
"""


def _duration_ms(started_at: float) -> int:
    return int((time.monotonic() - started_at) * 1000)


sandbox_runner = SandboxRunner()
