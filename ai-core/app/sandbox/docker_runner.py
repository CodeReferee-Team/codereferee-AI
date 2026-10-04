import json
import shlex
import tempfile
import time
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

import docker
from docker.errors import DockerException

from app.config import get_settings
from app.models import SandboxResult


class SandboxRunner:
    def __init__(self) -> None:
        self.settings = get_settings()

    def run_repository(
        self,
        repository_url: str,
        branch: str | None = None,
        commit_sha: str | None = None,
        patch_diff: str | None = None,
        chaos_mode: str | None = None,
        deployment_profile: str | None = None,
        request_id: str | None = None,
    ) -> SandboxResult:
        """Clone and smoke-test an existing repository.

        When SANDBOX_BASE_URL is configured, delegate to the external sandbox HTTP service.
        Otherwise, fall back to the local Docker SDK sandbox.

        patch_diff가 있으면 clone 직후 적용한다. 재검증 라운드에서 Refiner가 만든
        누적 diff를 넣는 경로이며, 사용자 레포에는 절대 push하지 않는다.
        """
        if self.settings.sandbox_base_url:
            return self._run_repository_via_http(
                repository_url, branch, commit_sha, patch_diff, chaos_mode, deployment_profile, request_id
            )
        if chaos_mode or deployment_profile:
            # 로컬 Docker 경로는 쿠버네티스가 없어 장애를 주입할 수 없다. 그냥 빌드 검증을
            # 돌려 통과로 내보내면, 안정성 검사를 요청한 사용자가 실험이 돌지 않았다는 것을
            # 모른 채 합격을 받는다.
            return SandboxResult(
                exit_code=None,
                stderr="Chaos execution requires the Kubernetes sandbox. Set SANDBOX_BASE_URL.",
                infra_error="chaos_requires_remote_sandbox",
            )
        return self._run_repository_via_local_docker(repository_url, branch, commit_sha, patch_diff)

    def _run_repository_via_http(
        self,
        repository_url: str,
        branch: str | None = None,
        commit_sha: str | None = None,
        patch_diff: str | None = None,
        chaos_mode: str | None = None,
        deployment_profile: str | None = None,
        request_id: str | None = None,
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
        if patch_diff:
            payload["patchDiff"] = patch_diff
            payload["patch_diff"] = patch_diff
        if chaos_mode:
            payload["chaosMode"] = chaos_mode
            payload["chaos_mode"] = chaos_mode
        if deployment_profile:
            # Sandbox는 프로필을 받으면 requestId로 namespace 이름을 만든다. 없으면 422다.
            payload["deploymentProfile"] = deployment_profile
            payload["deployment_profile"] = deployment_profile
        if request_id:
            payload["requestId"] = request_id
            payload["request_id"] = request_id
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
                infra_error="sandbox_http_error",
                duration_ms=_duration_ms(started_at),
            )
        except URLError as exc:
            return SandboxResult(
                exit_code=None,
                stderr=f"Sandbox connection error from {endpoint}: {exc.reason}",
                infra_error="sandbox_unreachable",
                duration_ms=_duration_ms(started_at),
            )
        except TimeoutError:
            return SandboxResult(
                exit_code=None,
                stderr=f"Sandbox HTTP request timed out after {self.settings.sandbox_http_timeout_seconds}s: {endpoint}",
                infra_error="sandbox_request_timeout",
                timed_out=True,
                duration_ms=_duration_ms(started_at),
            )

    def _run_repository_via_local_docker(
        self,
        repository_url: str,
        branch: str | None = None,
        commit_sha: str | None = None,
        patch_diff: str | None = None,
    ) -> SandboxResult:
        started_at = time.monotonic()
        with tempfile.TemporaryDirectory(prefix="codereferee-repo-") as tmp:
            workdir = Path(tmp)
            patch_path = ""
            if patch_diff:
                # 컨테이너에는 /workspace가 read-only로 마운트되므로 여기서 미리 써둔다.
                (workdir / PATCH_FILENAME).write_text(patch_diff, encoding="utf-8")
                patch_path = PATCH_CONTAINER_PATH
            script_path = workdir / "validate_repository.sh"
            script_path.write_text(
                _repository_validation_script(repository_url, branch, commit_sha, patch_path), encoding="utf-8"
            )

            try:
                client = docker.from_env()
                container = client.containers.run(
                    image=self.settings.sandbox_image,
                    command=["sh", "/workspace/validate_repository.sh"],
                    detach=True,
                    network_disabled=False,
                    mem_limit=self.settings.sandbox_memory_limit,
                    nano_cpus=self.settings.sandbox_nano_cpus,
                    pids_limit=self.settings.sandbox_pids_limit,
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
                report, clean_logs = _extract_sandbox_report(logs)
                return SandboxResult(
                    exit_code=exit_code,
                    stdout=clean_logs if exit_code == 0 else "",
                    stderr="" if exit_code == 0 else clean_logs,
                    timed_out=timed_out,
                    duration_ms=_duration_ms(started_at),
                    sandbox_report=report,
                )
            except DockerException as exc:
                return SandboxResult(
                    exit_code=None,
                    stderr=f"Docker repository sandbox error: {exc}",
                    infra_error="docker_daemon_unreachable",
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
    observation_status = data.get("observation_status", data.get("observationStatus"))
    schema_version = data.get("schema_version", data.get("schemaVersion"))
    probe_transport = data.get("probe_transport", data.get("probeTransport"))

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
        schema_version=str(schema_version) if schema_version else None,
        probe_transport=str(probe_transport) if probe_transport else None,
        baseline=_json_object(data.get("baseline")),
        metrics=_json_object(data.get("metrics")),
        chaos_observation=_json_object(data.get("chaos_observation", data.get("chaosObservation"))),
        source=_json_object(data.get("source")),
        sandbox_report=_json_object(data.get("sandbox_report", data.get("sandboxReport"))),
        observation_status=str(observation_status) if observation_status else None,
        infra_error=_infra_error_from_observation(observation_status),
    )


# 스크립트 본문은 f-string이 아니다. 셸의 중괄호를 그대로 쓰기 위함이며,
# 저장소 URL 등 동적 값은 앞쪽 헤더에서 셸 변수로만 주입한다.
_VALIDATION_BODY = """
STACK="unknown"
OUTCOME="error"
FAILED_STEP="none"
EXIT_CODE=1
STEPS=""

now_ms() { date +%s%3N 2>/dev/null || echo 0; }

# trap으로 걸어두면 중간에 exit해도 구조화 결과가 반드시 마지막 줄에 남는다.
emit_result() {
  printf '\n[CodeReferee:RESULT] {"schema_version":"sandbox-result.v1","detected_stack":"%s","outcome":"%s","failed_step":"%s","exit_code":%s,"steps":[%s]}\n' \
    "$STACK" "$OUTCOME" "$FAILED_STEP" "$EXIT_CODE" "$STEPS"
}
trap emit_result EXIT

run_step() {
  _name=$1
  shift
  _start=$(now_ms)
  "$@"
  _rc=$?
  _dur=$(( $(now_ms) - _start ))
  if [ -n "$STEPS" ]; then STEPS="$STEPS,"; fi
  STEPS="$STEPS$(printf '{"name":"%s","exit_code":%s,"duration_ms":%s}' "$_name" "$_rc" "$_dur")"
  if [ "$_rc" -ne 0 ]; then
    FAILED_STEP="$_name"
    EXIT_CODE="$_rc"
    OUTCOME="failure"
    exit "$_rc"
  fi
}

prepare_sandbox() {
  command -v git >/dev/null 2>&1 && return 0
  # DEBIAN_FRONTEND 없이 apt를 돌리면 debconf 경고가 stderr를 채워
  # 실제 실패 원인이 evidence에 묻힌다.
  export DEBIAN_FRONTEND=noninteractive
  apt-get update >/dev/null || return $?
  apt-get install -y --no-install-recommends git ca-certificates >/dev/null || return $?
  rm -rf /var/lib/apt/lists/*
}

clone_repository() {
  git clone --depth 1 $BRANCH_CLAUSE "$REPO_URL" /tmp/repository || return $?
  cd /tmp/repository || return $?
  [ -n "$COMMIT_SHA" ] || return 0
  # --depth 1 클론에는 해당 커밋이 없을 수 있어 먼저 받아둔다.
  git fetch --depth 1 origin "$COMMIT_SHA" >/dev/null 2>&1 || true
  git checkout "$COMMIT_SHA"
}

apply_patch() {
  [ -n "$PATCH_FILE" ] || return 0
  [ -f "$PATCH_FILE" ] || { echo "Patch file not found: $PATCH_FILE"; return 88; }
  # 적용 불가능한 diff는 빌드 전에 걸러낸다. 검증 시간의 대부분이 smoke 단계라
  # 여기서 막으면 가장 비싼 구간을 통째로 건너뛴다.
  git apply --check "$PATCH_FILE" || return $?
  git apply "$PATCH_FILE"
  echo "[CodeReferee] patch applied"
}

detect_stack() {
  if [ -f pyproject.toml ] || [ -f setup.py ] || [ -f requirements.txt ]; then STACK="python"
  elif [ -f build.gradle ] || [ -f settings.gradle ] || [ -f gradlew ]; then STACK="gradle"
  elif [ -f pom.xml ] || [ -f mvnw ]; then STACK="maven"
  elif [ -f package.json ]; then STACK="node"
  else
    echo "No supported project manifest found"
    return 86
  fi
  echo "detected_stack=$STACK"
}

install_dependencies() {
  case "$STACK" in
    python)
      [ -f requirements.txt ] || return 0
      python -m pip install --disable-pip-version-check -r requirements.txt >/dev/null
      ;;
    node)
      command -v npm >/dev/null 2>&1 || { echo "Node toolchain is not available in the sandbox image"; return 87; }
      if [ -f package-lock.json ]; then npm ci; else npm install; fi
      ;;
    *)
      # gradle/maven은 의존성 해결이 test 단계에 포함된다.
      return 0
      ;;
  esac
}

run_smoke_test() {
  case "$STACK" in
    python)
      python -m compileall -q . || return $?
      [ -d tests ] || return 0
      python -m pip install --disable-pip-version-check pytest >/dev/null || return $?
      python -m pytest -q
      ;;
    gradle)
      [ -x ./gradlew ] || { echo "Repository has no Gradle wrapper (./gradlew)"; return 87; }
      ./gradlew test --no-daemon
      ;;
    maven)
      if [ -x ./mvnw ]; then ./mvnw -B test
      elif command -v mvn >/dev/null 2>&1; then mvn -B test
      else echo "No Maven wrapper and no system mvn"; return 87; fi
      ;;
    node)
      npm run test --if-present
      ;;
  esac
}

echo "[CodeReferee] preparing sandbox"
run_step prepare prepare_sandbox
echo "[CodeReferee] cloning repository"
run_step clone clone_repository
echo "[CodeReferee] applying patch if present"
run_step patch apply_patch
echo "[CodeReferee] detecting project stack"
run_step detect detect_stack
echo "[CodeReferee] installing dependencies"
run_step dependencies install_dependencies
echo "[CodeReferee] running smoke validation"
run_step smoke run_smoke_test

OUTCOME="success"
FAILED_STEP="none"
EXIT_CODE=0
echo "[CodeReferee] repository smoke validation completed"
exit 0
"""


PATCH_FILENAME = "repository.patch"
PATCH_CONTAINER_PATH = f"/workspace/{PATCH_FILENAME}"


def _repository_validation_script(
    repository_url: str,
    branch: str | None,
    commit_sha: str | None,
    patch_file: str = "",
) -> str:
    branch_clause = f"--branch {branch}" if branch else ""
    header = (
        "#!/bin/sh\n"
        # set -e는 쓰지 않는다. 각 단계의 exit code를 기록해야 하므로
        # run_step이 직접 반환값을 받아 처리한다.
        "set -u\n"
        f"REPO_URL={shlex.quote(repository_url)}\n"
        f"BRANCH_CLAUSE={shlex.quote(branch_clause)}\n"
        f"COMMIT_SHA={shlex.quote(commit_sha or '')}\n"
        f"PATCH_FILE={shlex.quote(patch_file)}\n"
    )
    return header + _VALIDATION_BODY


RESULT_SENTINEL = "[CodeReferee:RESULT] "


def _extract_sandbox_report(logs: str) -> tuple[dict[str, Any], str]:
    """구조화 결과 줄을 분리해 돌려주고, 로그 본문에서는 제거한다.

    Judge는 이 구조화 결과로 판정하고 로그 전문은 보지 않는 것이 계약이다.
    """
    report: dict[str, Any] = {}
    kept: list[str] = []
    for line in logs.splitlines():
        if line.startswith(RESULT_SENTINEL):
            try:
                parsed = json.loads(line[len(RESULT_SENTINEL) :])
            except json.JSONDecodeError:
                kept.append(line)
                continue
            if isinstance(parsed, dict):
                report = parsed
        else:
            kept.append(line)
    return report, "\n".join(kept).strip()


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


OBSERVED = "observed"
INFRASTRUCTURE_ERROR = "infrastructure_error"


def _infra_error_from_observation(observation_status: object) -> str | None:
    """Sandbox가 "관측 불가"라고 밝히면 기존 infra_error 흐름으로 넘긴다.

    2026-09 Sandbox 팀과의 합의:
    - observed             → exitCode로 판정 (0=복구 성공, 1=복구 실패). timedOut이 함께 와도 FAILED다.
    - infrastructure_error → exitCode는 null이고 판정 자체가 불가능하다. ERROR로 간다.

    timedOut만으로 판단하지 않는 것이 핵심이다. 같은 timedOut이라도
    observed면 복구 관측 시간 초과(코드 결함), infrastructure_error면 Sandbox 실행 실패다.
    """
    if observation_status == INFRASTRUCTURE_ERROR:
        return "sandbox_observation_unavailable"
    return None


def _json_object(value: object) -> dict[str, object]:
    return value if isinstance(value, dict) else {}


def _duration_ms(started_at: float) -> int:
    return int((time.monotonic() - started_at) * 1000)


sandbox_runner = SandboxRunner()
