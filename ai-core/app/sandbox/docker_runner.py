import json
import shlex
import tempfile
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

import docker
from docker.errors import DockerException

from app.config import get_settings
from app.models import SandboxResult


class SandboxRunner:
    def __init__(self) -> None:
        self.settings = get_settings()

    def run_repository(self, repository_url: str, branch: str | None = None, commit_sha: str | None = None) -> SandboxResult:
        """Clone and smoke-test an existing repository.

        When SANDBOX_BASE_URL is configured, delegate to the external sandbox HTTP service.
        Otherwise, fall back to the local Docker SDK sandbox.
        """
        if self.settings.sandbox_base_url:
            return self._run_repository_via_http(repository_url, branch, commit_sha)
        return self._run_repository_via_local_docker(repository_url, branch, commit_sha)

    def _run_repository_via_http(
        self, repository_url: str, branch: str | None = None, commit_sha: str | None = None
    ) -> SandboxResult:
        started_at = time.monotonic()
        endpoint = _join_url(self.settings.sandbox_base_url or "", self.settings.sandbox_repository_path)
        payload = {
            # Server-facing schema.
            "repositoryUrl": repository_url,
            "branch": branch,
            "commitSha": commit_sha,
            # Snake-case aliases for sandbox implementations that follow the AI Core API style.
            "repository_url": repository_url,
            "commit_sha": commit_sha,
        }
        request = Request(
            endpoint,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urlopen(request, timeout=self.settings.sandbox_http_timeout_seconds) as response:
                body = response.read().decode("utf-8", errors="replace")
                return _sandbox_result_from_response(body, started_at)
        except HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace") if exc.fp else ""
            return SandboxResult(
                exit_code=None,
                stderr=f"Sandbox HTTP error {exc.code} from {endpoint}: {body or exc.reason}",
                duration_ms=_duration_ms(started_at),
            )
        except URLError as exc:
            return SandboxResult(
                exit_code=None,
                stderr=f"Sandbox connection error from {endpoint}: {exc.reason}",
                duration_ms=_duration_ms(started_at),
            )
        except TimeoutError:
            return SandboxResult(
                exit_code=None,
                stderr=f"Sandbox HTTP request timed out after {self.settings.sandbox_http_timeout_seconds}s: {endpoint}",
                timed_out=True,
                duration_ms=_duration_ms(started_at),
            )

    def _run_repository_via_local_docker(
        self, repository_url: str, branch: str | None = None, commit_sha: str | None = None
    ) -> SandboxResult:
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


def _sandbox_result_from_response(body: str, started_at: float) -> SandboxResult:
    try:
        data = json.loads(body) if body else {}
    except json.JSONDecodeError:
        return SandboxResult(exit_code=0, stdout=body, duration_ms=_duration_ms(started_at))

    exit_code = data.get("exit_code", data.get("exitCode"))
    timed_out = bool(data.get("timed_out", data.get("timedOut", False)))
    duration_ms = int(data.get("duration_ms", data.get("durationMillis", _duration_ms(started_at))) or 0)
    stdout = str(data.get("stdout", data.get("log", "")) or "")
    stderr = str(data.get("stderr", data.get("errorMessage", "")) or "")
    server_started = bool(data.get("server_started", data.get("serverStarted", False)))
    server_url = data.get("server_url", data.get("serverUrl"))
    http_status = data.get("http_status", data.get("httpStatus"))
    browser_loaded = bool(data.get("browser_loaded", data.get("browserLoaded", False)))
    page_title = data.get("page_title", data.get("pageTitle"))
    run_command = data.get("run_command", data.get("runCommand"))
    service_check_attempted = _explicit_bool(data, "service_check_attempted", "serviceCheckAttempted")
    browser_check_attempted = _explicit_bool(data, "browser_check_attempted", "browserCheckAttempted")

    if "isExecutable" in data and exit_code is None:
        exit_code = 0 if data.get("isExecutable") else 1
    if "executable" in data and exit_code is None:
        exit_code = 0 if data.get("executable") else 1

    if exit_code is None and not stderr:
        exit_code = 0

    return SandboxResult(
        exit_code=exit_code,
        stdout=stdout if exit_code == 0 else stdout,
        stderr=stderr,
        timed_out=timed_out,
        duration_ms=duration_ms,
        server_started=server_started,
        server_url=str(server_url) if server_url else None,
        http_status=int(http_status) if http_status is not None else None,
        browser_loaded=browser_loaded,
        page_title=str(page_title) if page_title else None,
        run_command=run_command if isinstance(run_command, list) else None,
        service_check_attempted=(
            service_check_attempted
            if service_check_attempted is not None
            else _infer_service_check_attempted(server_started, server_url, http_status, run_command)
        ),
        browser_check_attempted=(
            browser_check_attempted
            if browser_check_attempted is not None
            else _infer_browser_check_attempted(browser_loaded, page_title, http_status, server_started, server_url, run_command)
        ),
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


def _join_url(base_url: str, path: str) -> str:
    return f"{base_url.rstrip('/')}/{path.lstrip('/')}"


def _explicit_bool(data: dict, snake_key: str, camel_key: str) -> bool | None:
    if snake_key in data:
        return bool(data[snake_key])
    if camel_key in data:
        return bool(data[camel_key])
    return None


def _infer_service_check_attempted(
    server_started: bool, server_url: object, http_status: object, run_command: object
) -> bool:
    return bool(server_started or server_url or http_status is not None or run_command)


def _infer_browser_check_attempted(
    browser_loaded: bool,
    page_title: object,
    http_status: object,
    server_started: bool,
    server_url: object,
    run_command: object,
) -> bool:
    return bool(
        browser_loaded
        or page_title
        or (http_status is not None and _infer_service_check_attempted(server_started, server_url, http_status, run_command))
    )


def _duration_ms(started_at: float) -> int:
    return int((time.monotonic() - started_at) * 1000)


sandbox_runner = SandboxRunner()
