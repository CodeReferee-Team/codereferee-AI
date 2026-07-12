from __future__ import annotations

import json
import os
import re
import signal
import shutil
import subprocess
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from fastapi import FastAPI
from pydantic import BaseModel, Field


WORK_ROOT = Path(os.getenv("SANDBOX_WORK_ROOT", "/tmp/codereferee-sandbox"))
COMMAND_TIMEOUT_SECONDS = int(os.getenv("SANDBOX_COMMAND_TIMEOUT_SECONDS", "300"))
REPOSITORY_TIMEOUT_SECONDS = int(os.getenv("SANDBOX_REPOSITORY_TIMEOUT_SECONDS", "300"))
SERVER_START_TIMEOUT_SECONDS = int(os.getenv("SANDBOX_SERVER_START_TIMEOUT_SECONDS", "60"))
MAX_LOG_CHARS = int(os.getenv("SANDBOX_MAX_LOG_CHARS", "12000"))

app = FastAPI(title="CodeReferee Sandbox", version="0.1.0")


@app.on_event("startup")
def cleanup_stale_workdirs() -> None:
    # The fixed sandbox containers are reused, but per-job repository workdirs are disposable.
    shutil.rmtree(WORK_ROOT, ignore_errors=True)
    WORK_ROOT.mkdir(parents=True, exist_ok=True)


class RepositoryValidationRequest(BaseModel):
    repository_url: str | None = Field(default=None, alias="repository_url")
    repositoryUrl: str | None = None
    branch: str | None = None
    commit_sha: str | None = Field(default=None, alias="commit_sha")
    commitSha: str | None = None

    model_config = {"populate_by_name": True}

    @property
    def repo_url(self) -> str:
        return self.repositoryUrl or self.repository_url or ""

    @property
    def requested_commit(self) -> str | None:
        return self.commitSha or self.commit_sha


class RepositoryValidationResponse(BaseModel):
    exitCode: int | None
    exit_code: int | None
    timedOut: bool
    timed_out: bool
    durationMillis: int
    duration_ms: int
    stdout: str = ""
    stderr: str = ""
    detectedStack: str | None = None
    detected_stack: str | None = None
    resolvedCommitSha: str | None = None
    resolved_commit_sha: str | None = None
    sandboxId: str | None = None
    sandbox_id: str | None = None
    serverStarted: bool = False
    server_started: bool = False
    serverUrl: str | None = None
    server_url: str | None = None
    httpStatus: int | None = None
    http_status: int | None = None
    browserLoaded: bool = False
    browser_loaded: bool = False
    pageTitle: str | None = None
    page_title: str | None = None
    runCommand: list[str] | None = None
    run_command: list[str] | None = None


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "sandboxId": _sandbox_id()}


@app.post("/repositories/validate", response_model=RepositoryValidationResponse)
def validate_repository(request: RepositoryValidationRequest) -> RepositoryValidationResponse:
    started = time.monotonic()
    job_id = uuid.uuid4().hex
    workdir = WORK_ROOT / job_id
    repo_dir = workdir / "repository"
    stdout_parts: list[str] = []
    stderr_parts: list[str] = []
    exit_code: int | None = 0
    timed_out = False
    detected_stack: str | None = None
    resolved_commit_sha: str | None = None
    server_smoke: ServerSmokeResult | None = None

    try:
        if not request.repo_url:
            return _response(2, started, "", "repositoryUrl/repository_url is required", False)

        workdir.mkdir(parents=True, exist_ok=True)
        clone_cmd = ["git", "clone", "--depth", "1"]
        if request.branch:
            clone_cmd += ["--branch", request.branch]
        clone_cmd += [request.repo_url, str(repo_dir)]

        result = _run_git_clone_with_retries(clone_cmd, cwd=workdir, repo_dir=repo_dir)
        stdout_parts.append(_section("git clone", result.stdout))
        stderr_parts.append(_section("git clone", result.stderr))
        if result.returncode != 0:
            exit_code = result.returncode
            return _response(exit_code, started, _join(stdout_parts), _join(stderr_parts), timed_out)

        if request.requested_commit:
            result = _run(["git", "checkout", request.requested_commit], cwd=repo_dir)
            stdout_parts.append(_section("git checkout", result.stdout))
            stderr_parts.append(_section("git checkout", result.stderr))
            if result.returncode != 0:
                exit_code = result.returncode
                return _response(exit_code, started, _join(stdout_parts), _join(stderr_parts), timed_out)

        resolved = _run(["git", "rev-parse", "HEAD"], cwd=repo_dir)
        resolved_commit_sha = resolved.stdout.strip() or None
        stdout_parts.append(_section("git rev-parse", resolved.stdout))
        stderr_parts.append(_section("git rev-parse", resolved.stderr))

        detected_stack = _detect_stack(repo_dir)
        stdout_parts.append(f"[CodeReferee] detected_stack={detected_stack}\n")

        exit_code, stack_stdout, stack_stderr, server_smoke = _validate_stack(repo_dir, detected_stack)
        stdout_parts.append(stack_stdout)
        stderr_parts.append(stack_stderr)

    except subprocess.TimeoutExpired as exc:
        exit_code = None
        timed_out = True
        stdout_parts.append(_section("timeout stdout", exc.stdout or ""))
        stderr_parts.append(_section("timeout stderr", exc.stderr or f"Timed out after {exc.timeout}s"))
    except Exception as exc:  # noqa: BLE001 - sandbox must return structured failure instead of crashing.
        exit_code = None
        stderr_parts.append(f"Sandbox internal error: {type(exc).__name__}: {exc}\n")
    finally:
        _kill_processes_using(workdir)
        shutil.rmtree(workdir, ignore_errors=True)

    return _response(
        exit_code,
        started,
        _join(stdout_parts),
        _join(stderr_parts),
        timed_out,
        detected_stack=detected_stack,
        resolved_commit_sha=resolved_commit_sha,
        server_smoke=server_smoke,
    )


@dataclass
class ServerSmokeResult:
    attempted: bool = False
    server_started: bool = False
    server_url: str | None = None
    http_status: int | None = None
    browser_loaded: bool = False
    page_title: str | None = None
    run_command: list[str] | None = None


def _run_git_clone_with_retries(
    clone_cmd: list[str], *, cwd: Path, repo_dir: Path, attempts: int = 3
) -> subprocess.CompletedProcess[str]:
    stdout_parts: list[str] = []
    stderr_parts: list[str] = []
    last_returncode = 1

    for attempt in range(1, attempts + 1):
        shutil.rmtree(repo_dir, ignore_errors=True)
        result = _run(clone_cmd, cwd=cwd, timeout=REPOSITORY_TIMEOUT_SECONDS)
        last_returncode = result.returncode
        stdout_parts.append(_section(f"git clone attempt {attempt}", result.stdout))
        stderr_parts.append(_section(f"git clone attempt {attempt}", result.stderr))
        if result.returncode == 0:
            return subprocess.CompletedProcess(clone_cmd, 0, _join(stdout_parts), _join(stderr_parts))
        if not _is_retryable_git_clone_failure(result.stderr) or attempt == attempts:
            break
        time.sleep(min(2 * attempt, 5))

    return subprocess.CompletedProcess(clone_cmd, last_returncode, _join(stdout_parts), _join(stderr_parts))


def _is_retryable_git_clone_failure(stderr: str) -> bool:
    retryable_markers = (
        "early EOF",
        "unexpected disconnect",
        "RPC failed",
        "GnuTLS recv error",
        "Connection timed out",
        "Operation timed out",
        "TLS packet",
        "invalid index-pack output",
    )
    return any(marker.lower() in stderr.lower() for marker in retryable_markers)


def _kill_processes_using(workdir: Path) -> None:
    """Best-effort cleanup for tools that return before child daemons fully stop."""
    workdir_text = str(workdir)
    pids: set[int] = set()
    proc_root = Path("/proc")
    if not proc_root.exists():
        return

    for entry in proc_root.iterdir():
        if not entry.name.isdigit():
            continue
        pid = int(entry.name)
        if pid == os.getpid():
            continue
        try:
            cwd = os.readlink(entry / "cwd")
        except OSError:
            cwd = ""
        try:
            cmdline = (entry / "cmdline").read_bytes().replace(b"\x00", b" ").decode("utf-8", errors="replace")
        except OSError:
            cmdline = ""
        if cwd.startswith(workdir_text) or workdir_text in cmdline:
            pids.add(pid)

    for sig in (signal.SIGTERM, signal.SIGKILL):
        for pid in list(pids):
            try:
                os.kill(pid, sig)
            except ProcessLookupError:
                pids.discard(pid)
            except OSError:
                pass
        if pids:
            time.sleep(0.2)


def _validate_stack(repo_dir: Path, detected_stack: str) -> tuple[int | None, str, str, ServerSmokeResult | None]:
    stdout_parts: list[str] = []
    stderr_parts: list[str] = []

    if detected_stack == "node":
        install_cmd = _node_install_command(repo_dir)
        result = _run(install_cmd, cwd=repo_dir)
        stdout_parts.append(_section("node install", result.stdout))
        stderr_parts.append(_section("node install", result.stderr))
        if result.returncode != 0:
            return result.returncode, _join(stdout_parts), _join(stderr_parts), None

        package = _read_text(repo_dir / "package.json")
        if '"build"' in package:
            result = _run(["npm", "run", "build", "--", "--watch=false"], cwd=repo_dir)
            stdout_parts.append(_section("npm run build", result.stdout))
            stderr_parts.append(_section("npm run build", result.stderr))
            if result.returncode != 0:
                return result.returncode, _join(stdout_parts), _join(stderr_parts), None

        server_smoke, smoke_stdout, smoke_stderr = _server_smoke(repo_dir, detected_stack)
        stdout_parts.append(smoke_stdout)
        stderr_parts.append(smoke_stderr)
        if server_smoke.attempted and not server_smoke.browser_loaded:
            return 88, _join(stdout_parts), _join(stderr_parts), server_smoke

        for script_name in ("test",):
            if f'"{script_name}"' not in package:
                stdout_parts.append(f"[CodeReferee] npm script '{script_name}' not found; skipped\n")
                continue
            result = _run(["npm", "run", script_name, "--", "--watch=false"], cwd=repo_dir)
            stdout_parts.append(_section(f"npm run {script_name}", result.stdout))
            stderr_parts.append(_section(f"npm run {script_name}", result.stderr))
            if result.returncode != 0:
                return result.returncode, _join(stdout_parts), _join(stderr_parts), server_smoke

        extension_dir = _find_chrome_extension_dir(repo_dir)
        if extension_dir:
            result = _run(
                [
                    "timeout",
                    "15",
                    "chromium",
                    "--headless=new",
                    "--disable-gpu",
                    "--no-sandbox",
                    f"--load-extension={extension_dir}",
                    "about:blank",
                ],
                cwd=repo_dir,
                timeout=25,
            )
            stdout_parts.append(_section("chromium extension smoke", result.stdout))
            stderr_parts.append(_section("chromium extension smoke", result.stderr))
            # GNU timeout returns 124 when the browser stayed alive until the smoke window ended.
            if result.returncode not in (0, 124):
                return result.returncode, _join(stdout_parts), _join(stderr_parts), server_smoke

        return 0, _join(stdout_parts), _join(stderr_parts), server_smoke

    if detected_stack == "python":
        if (repo_dir / "requirements.txt").exists():
            result = _run(["python", "-m", "pip", "install", "--disable-pip-version-check", "-r", "requirements.txt"], cwd=repo_dir)
            stdout_parts.append(_section("pip install", result.stdout))
            stderr_parts.append(_section("pip install", result.stderr))
            if result.returncode != 0:
                return result.returncode, _join(stdout_parts), _join(stderr_parts), None
        result = _run(["python", "-m", "compileall", "."], cwd=repo_dir)
        stdout_parts.append(_section("python compileall", result.stdout))
        stderr_parts.append(_section("python compileall", result.stderr))
        if result.returncode != 0:
            return result.returncode, _join(stdout_parts), _join(stderr_parts), None
        server_smoke, smoke_stdout, smoke_stderr = _server_smoke(repo_dir, detected_stack)
        stdout_parts.append(smoke_stdout)
        stderr_parts.append(smoke_stderr)
        if server_smoke.attempted and not server_smoke.browser_loaded:
            return 88, _join(stdout_parts), _join(stderr_parts), server_smoke
        return 0, _join(stdout_parts), _join(stderr_parts), server_smoke

    if detected_stack == "php":
        return _validate_php(repo_dir)

    if detected_stack == "gradle":
        gradlew = repo_dir / "gradlew"
        if not gradlew.exists():
            return 87, "", "Gradle wrapper not found\n", None
        result = _run(["sh", "./gradlew", "test", "--no-daemon"], cwd=repo_dir)
        stdout_parts.append(_section("gradle test", result.stdout))
        stderr_parts.append(_section("gradle test", result.stderr))
        if result.returncode != 0:
            return result.returncode, _join(stdout_parts), _join(stderr_parts), None
        server_smoke, smoke_stdout, smoke_stderr = _server_smoke(repo_dir, detected_stack)
        stdout_parts.append(smoke_stdout)
        stderr_parts.append(smoke_stderr)
        if server_smoke.attempted and not server_smoke.browser_loaded:
            return 88, _join(stdout_parts), _join(stderr_parts), server_smoke
        return 0, _join(stdout_parts), _join(stderr_parts), server_smoke

    if detected_stack == "maven":
        mvnw = repo_dir / "mvnw"
        if not mvnw.exists():
            return 87, "", "Maven wrapper not found\n", None
        result = _run(["sh", "./mvnw", "test"], cwd=repo_dir)
        stdout_parts.append(_section("maven test", result.stdout))
        stderr_parts.append(_section("maven test", result.stderr))
        if result.returncode != 0:
            return result.returncode, _join(stdout_parts), _join(stderr_parts), None
        server_smoke, smoke_stdout, smoke_stderr = _server_smoke(repo_dir, detected_stack)
        stdout_parts.append(smoke_stdout)
        stderr_parts.append(smoke_stderr)
        if server_smoke.attempted and not server_smoke.browser_loaded:
            return 88, _join(stdout_parts), _join(stderr_parts), server_smoke
        return 0, _join(stdout_parts), _join(stderr_parts), server_smoke

    return 86, "", "No supported project manifest found\n", None


def _validate_php(repo_dir: Path) -> tuple[int | None, str, str, ServerSmokeResult | None]:
    stdout_parts: list[str] = []
    stderr_parts: list[str] = []

    if (repo_dir / "composer.json").exists():
        result = _run(["composer", "install", "--no-interaction", "--no-progress", "--prefer-dist"], cwd=repo_dir)
        stdout_parts.append(_section("composer install", result.stdout))
        stderr_parts.append(_section("composer install", result.stderr))
        if result.returncode != 0:
            return result.returncode, _join(stdout_parts), _join(stderr_parts), None

    php_files = [path for path in repo_dir.rglob("*.php") if "vendor" not in path.parts][:200]
    if not php_files:
        return 86, _join(stdout_parts), _join(stderr_parts) + "No PHP files found\n", None

    for php_file in php_files:
        result = _run(["php", "-l", str(php_file)], cwd=repo_dir)
        stdout_parts.append(_section(f"php lint {php_file.relative_to(repo_dir)}", result.stdout))
        stderr_parts.append(_section(f"php lint {php_file.relative_to(repo_dir)}", result.stderr))
        if result.returncode != 0:
            return result.returncode, _join(stdout_parts), _join(stderr_parts), None

    server_smoke, smoke_stdout, smoke_stderr = _server_smoke(repo_dir, "php")
    stdout_parts.append(smoke_stdout)
    stderr_parts.append(smoke_stderr)
    if server_smoke.attempted and not server_smoke.browser_loaded:
        return 88, _join(stdout_parts), _join(stderr_parts), server_smoke

    return 0, _join(stdout_parts), _join(stderr_parts), server_smoke


def _server_smoke(repo_dir: Path, detected_stack: str) -> tuple[ServerSmokeResult, str, str]:
    command, port = _detect_run_command(repo_dir, detected_stack)
    smoke = ServerSmokeResult(attempted=command is not None, run_command=command)
    stdout_parts: list[str] = []
    stderr_parts: list[str] = []

    if not command:
        stdout_parts.append("[CodeReferee] server smoke: no supported run command detected; skipped\n")
        return smoke, _join(stdout_parts), _join(stderr_parts)

    env = {
        **os.environ,
        "CI": "true",
        "HOST": "127.0.0.1",
        "PORT": str(port),
        "SERVER_PORT": str(port),
        "SPRING_PROFILES_ACTIVE": os.getenv("SPRING_PROFILES_ACTIVE", "test"),
        "PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD": "1",
    }
    process: subprocess.Popen[str] | None = None
    try:
        process = subprocess.Popen(
            command,
            cwd=str(repo_dir),
            env=env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
        url = f"http://127.0.0.1:{port}/"
        smoke.server_url = url
        stdout_parts.append(f"[CodeReferee] server smoke command: {' '.join(command)}\n")
        stdout_parts.append(f"[CodeReferee] server smoke url: {url}\n")

        status = _wait_for_http(url, SERVER_START_TIMEOUT_SECONDS)
        if status is None:
            stderr_parts.append(f"[CodeReferee] server smoke failed: no HTTP response within {SERVER_START_TIMEOUT_SECONDS}s\n")
        else:
            smoke.server_started = True
            smoke.http_status = status
            stdout_parts.append(f"[CodeReferee] server smoke http_status={status}\n")

            browser = _run_browser_probe(url, cwd=repo_dir)
            stdout_parts.append(_section("browser probe", browser.stdout))
            stderr_parts.append(_section("browser probe", browser.stderr))
            smoke.browser_loaded = browser.returncode == 0
            smoke.page_title = _extract_title(browser.stdout)
            stdout_parts.append(f"[CodeReferee] browser_loaded={smoke.browser_loaded}\n")
            if smoke.page_title:
                stdout_parts.append(f"[CodeReferee] page_title={smoke.page_title}\n")
    except Exception as exc:  # noqa: BLE001 - keep sandbox response structured.
        stderr_parts.append(f"[CodeReferee] server smoke internal error: {type(exc).__name__}: {exc}\n")
    finally:
        if process is not None:
            _terminate_process_tree(process)
            process_stdout, process_stderr = _read_process_output(process)
            stdout_parts.append(_section("server stdout", process_stdout))
            stderr_parts.append(_section("server stderr", process_stderr))

    return smoke, _join(stdout_parts), _join(stderr_parts)


def _detect_run_command(repo_dir: Path, detected_stack: str) -> tuple[list[str] | None, int]:
    if detected_stack == "node":
        scripts = _package_scripts(repo_dir)
        if "start" in scripts:
            return ["npm", "run", "start"], 3000
        if "serve" in scripts:
            return ["npm", "run", "serve", "--", "--host", "127.0.0.1"], 3000
        if "dev" in scripts:
            return ["npm", "run", "dev", "--", "--host", "127.0.0.1"], 3000
        return None, 3000

    if detected_stack == "php" and (repo_dir / "index.php").exists():
        return ["php", "-S", "127.0.0.1:8080", "-t", str(repo_dir)], 8080

    if detected_stack == "gradle" and _looks_like_spring_boot(repo_dir) and (repo_dir / "gradlew").exists():
        return ["sh", "./gradlew", "bootRun", "--no-daemon"], 8080

    if detected_stack == "maven" and _looks_like_spring_boot(repo_dir) and (repo_dir / "mvnw").exists():
        return ["sh", "./mvnw", "spring-boot:run"], 8080

    if detected_stack == "python":
        if (repo_dir / "manage.py").exists():
            return ["python", "manage.py", "runserver", "127.0.0.1:8000"], 8000
        if (repo_dir / "app" / "main.py").exists():
            return ["python", "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", "8000"], 8000
        if (repo_dir / "main.py").exists():
            return ["python", "-m", "uvicorn", "main:app", "--host", "127.0.0.1", "--port", "8000"], 8000
        if (repo_dir / "app.py").exists():
            return ["python", "-m", "flask", "--app", "app", "run", "--host", "127.0.0.1", "--port", "8000"], 8000

    return None, 0


def _wait_for_http(url: str, timeout_seconds: int) -> int | None:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        try:
            request = Request(url, headers={"User-Agent": "CodeReferee-Sandbox/0.1"})
            with urlopen(request, timeout=2) as response:
                return int(response.status)
        except HTTPError as exc:
            return int(exc.code)
        except (OSError, URLError, TimeoutError):
            time.sleep(1)
    return None


def _run_browser_probe(url: str, *, cwd: Path) -> subprocess.CompletedProcess[str]:
    return _run(
        [
            "chromium",
            "--headless=new",
            "--disable-gpu",
            "--no-sandbox",
            "--dump-dom",
            url,
        ],
        cwd=cwd,
        timeout=30,
    )


def _terminate_process_tree(process: subprocess.Popen[str]) -> None:
    if process.poll() is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except OSError:
        process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except OSError:
            process.kill()
        process.wait(timeout=5)


def _read_process_output(process: subprocess.Popen[str]) -> tuple[str, str]:
    try:
        stdout, stderr = process.communicate(timeout=1)
    except subprocess.TimeoutExpired:
        return "", ""
    return stdout or "", stderr or ""


def _package_scripts(repo_dir: Path) -> dict[str, str]:
    try:
        package = json.loads(_read_text(repo_dir / "package.json") or "{}")
    except json.JSONDecodeError:
        return {}
    scripts = package.get("scripts", {})
    return scripts if isinstance(scripts, dict) else {}


def _looks_like_spring_boot(repo_dir: Path) -> bool:
    manifests = [
        repo_dir / "build.gradle",
        repo_dir / "build.gradle.kts",
        repo_dir / "settings.gradle",
        repo_dir / "settings.gradle.kts",
        repo_dir / "pom.xml",
    ]
    content = "\n".join(_read_text(path) for path in manifests)
    return "org.springframework.boot" in content or "spring-boot" in content or "bootRun" in content


def _extract_title(html: str) -> str | None:
    match = re.search(r"<title[^>]*>(.*?)</title>", html, flags=re.IGNORECASE | re.DOTALL)
    if not match:
        return None
    return re.sub(r"\s+", " ", match.group(1)).strip() or None


def _detect_stack(repo_dir: Path) -> str:
    if (repo_dir / "package.json").exists():
        return "node"
    if any((repo_dir / name).exists() for name in ("pyproject.toml", "setup.py", "requirements.txt")):
        return "python"
    if (repo_dir / "composer.json").exists() or (repo_dir / "index.php").exists() or any(repo_dir.rglob("*.php")):
        return "php"
    if any((repo_dir / name).exists() for name in ("build.gradle", "settings.gradle", "gradlew")):
        return "gradle"
    if any((repo_dir / name).exists() for name in ("pom.xml", "mvnw")):
        return "maven"
    return "unknown"


def _node_install_command(repo_dir: Path) -> list[str]:
    if (repo_dir / "pnpm-lock.yaml").exists():
        return ["sh", "-lc", "npx --yes pnpm install --frozen-lockfile --store-dir /cache/pnpm-store"]
    if (repo_dir / "yarn.lock").exists():
        return ["sh", "-lc", "npx --yes yarn install --frozen-lockfile --prefer-offline"]
    if (repo_dir / "package-lock.json").exists():
        return ["npm", "ci", "--prefer-offline"]
    return ["npm", "install", "--prefer-offline"]


def _find_chrome_extension_dir(repo_dir: Path) -> Path | None:
    candidates = [repo_dir / "manifest.json", repo_dir / "extension" / "manifest.json", repo_dir / "public" / "manifest.json"]
    for manifest in candidates:
        if manifest.exists() and '"manifest_version"' in _read_text(manifest):
            return manifest.parent
    return None


def _run(cmd: list[str], *, cwd: Path, timeout: int | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        cmd,
        cwd=str(cwd),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout or COMMAND_TIMEOUT_SECONDS,
        env={**os.environ, "CI": "true", "PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD": "1"},
        check=False,
    )


def _response(
    exit_code: int | None,
    started: float,
    stdout: str,
    stderr: str,
    timed_out: bool,
    *,
    detected_stack: str | None = None,
    resolved_commit_sha: str | None = None,
    server_smoke: ServerSmokeResult | None = None,
) -> RepositoryValidationResponse:
    duration_ms = int((time.monotonic() - started) * 1000)
    sandbox_id = _sandbox_id()
    smoke = server_smoke or ServerSmokeResult()
    return RepositoryValidationResponse(
        exitCode=exit_code,
        exit_code=exit_code,
        timedOut=timed_out,
        timed_out=timed_out,
        durationMillis=duration_ms,
        duration_ms=duration_ms,
        stdout=_truncate(stdout),
        stderr=_truncate(stderr),
        detectedStack=detected_stack,
        detected_stack=detected_stack,
        resolvedCommitSha=resolved_commit_sha,
        resolved_commit_sha=resolved_commit_sha,
        sandboxId=sandbox_id,
        sandbox_id=sandbox_id,
        serverStarted=smoke.server_started,
        server_started=smoke.server_started,
        serverUrl=smoke.server_url,
        server_url=smoke.server_url,
        httpStatus=smoke.http_status,
        http_status=smoke.http_status,
        browserLoaded=smoke.browser_loaded,
        browser_loaded=smoke.browser_loaded,
        pageTitle=smoke.page_title,
        page_title=smoke.page_title,
        runCommand=smoke.run_command,
        run_command=smoke.run_command,
    )


def _sandbox_id() -> str:
    return os.getenv("HOSTNAME", "codereferee-sandbox")


def _section(name: str, content: Any) -> str:
    text = content.decode("utf-8", errors="replace") if isinstance(content, bytes) else str(content or "")
    return f"\n--- {name} ---\n{text}\n"


def _join(parts: list[str]) -> str:
    return "".join(parts)


def _truncate(text: str) -> str:
    if len(text) <= MAX_LOG_CHARS:
        return text
    head_chars = MAX_LOG_CHARS // 2
    tail_chars = MAX_LOG_CHARS - head_chars
    return text[:head_chars] + "\n...[truncated by CodeReferee sandbox]...\n" + text[-tail_chars:]


def _read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace") if path.exists() else ""
