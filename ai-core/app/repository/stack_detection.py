"""레포의 스택과 실행 커맨드를 파일로 판단한다.

로직은 2026-07-12에 삭제된 `infra/sandbox/service/app.py`에서 가져왔다. 그 코드는 사용자 앱을
`Popen`으로 직접 띄웠고, LitmusChaos가 Kubernetes를 요구해서 Docker 경로 자체가 버려졌다.
버린 것은 실행 모델이고 감지 로직은 그대로 쓸 수 있다.

바꾼 것이 하나 있다. 바인딩 주소를 127.0.0.1에서 0.0.0.0으로 옮겼다. Pod 안에서 루프백에
바인딩하면 Service나 probe가 닿지 못한다. 로컬 프로세스 기준의 값이었다.

실행 커맨드가 없으면 띄울 서비스가 없다는 뜻이다. 라이브러리 레포는 카오스 검증 대상이 아니므로
그 판별에도 쓴다.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

# 스택별 기본 포트. 앱이 환경변수로 바꿀 수 있으므로 커맨드에 명시해 고정한다.
_NODE_PORT = 3000
_PYTHON_PORT = 8000
_JVM_PORT = 8080

# Pod 밖에서 probe하려면 루프백이 아니라 모든 인터페이스에 바인딩해야 한다.
_BIND_HOST = "0.0.0.0"


@dataclass(frozen=True)
class RunTarget:
    command: list[str]
    port: int


@dataclass(frozen=True)
class ProjectProfile:
    stack: str
    build_command: list[str] | None = None
    test_command: list[str] | None = None
    run_target: RunTarget | None = None

    @property
    def is_service(self) -> bool:
        """띄울 수 있는 서비스인가. 아니면 라이브러리다."""
        return self.run_target is not None


# package.json은 서버 스택의 표식이 아니다. Django 레포에도 있고 거기서는 grunt/biome 같은
# 도구 스크립트만 담는다. 서버를 띄우는 스크립트가 있을 때만 node로 본다.
_NODE_SERVER_SCRIPTS = ("start", "serve", "dev")


def detect_stack(repo_dir: Path) -> str:
    scripts = _package_scripts(repo_dir)
    if any(name in scripts for name in _NODE_SERVER_SCRIPTS):
        return "node"
    if any((repo_dir / name).exists() for name in ("pyproject.toml", "setup.py", "requirements.txt")):
        return "python"
    if (repo_dir / "package.json").exists():
        return "node"
    if any((repo_dir / name).exists() for name in ("build.gradle", "build.gradle.kts", "settings.gradle", "gradlew")):
        return "gradle"
    if any((repo_dir / name).exists() for name in ("pom.xml", "mvnw")):
        return "maven"
    return "unknown"


def detect_run_command(repo_dir: Path, stack: str) -> RunTarget | None:
    if stack == "node":
        scripts = _package_scripts(repo_dir)
        for name in ("start", "serve", "dev"):
            if name in scripts:
                command = ["npm", "run", name]
                if name != "start":
                    command += ["--", "--host", _BIND_HOST]
                return RunTarget(command, _NODE_PORT)
        return None

    if stack == "python":
        if (repo_dir / "manage.py").exists():
            return RunTarget(["python", "manage.py", "runserver", f"{_BIND_HOST}:{_PYTHON_PORT}"], _PYTHON_PORT)
        if (repo_dir / "app" / "main.py").exists():
            return RunTarget(_uvicorn("app.main:app"), _PYTHON_PORT)
        if (repo_dir / "main.py").exists():
            return RunTarget(_uvicorn("main:app"), _PYTHON_PORT)
        if (repo_dir / "app.py").exists():
            return RunTarget(
                ["python", "-m", "flask", "--app", "app", "run", "--host", _BIND_HOST, "--port", str(_PYTHON_PORT)],
                _PYTHON_PORT,
            )
        return None

    # Gradle과 Maven은 Spring Boot일 때만 실행 커맨드가 정해진다. 일반 JVM 프로젝트는
    # 무엇을 띄워야 하는지 알 수 없다.
    if stack == "gradle" and _looks_like_spring_boot(repo_dir) and (repo_dir / "gradlew").exists():
        return RunTarget(["sh", "./gradlew", "bootRun", "--no-daemon"], _JVM_PORT)
    if stack == "maven" and _looks_like_spring_boot(repo_dir) and (repo_dir / "mvnw").exists():
        return RunTarget(["sh", "./mvnw", "spring-boot:run"], _JVM_PORT)
    return None


def profile(repo_dir: Path) -> ProjectProfile:
    stack = detect_stack(repo_dir)
    return ProjectProfile(
        stack=stack,
        build_command=_build_command(repo_dir, stack),
        test_command=_test_command(repo_dir, stack),
        run_target=detect_run_command(repo_dir, stack),
    )


def _uvicorn(target: str) -> list[str]:
    return ["python", "-m", "uvicorn", target, "--host", _BIND_HOST, "--port", str(_PYTHON_PORT)]


def _build_command(repo_dir: Path, stack: str) -> list[str] | None:
    if stack == "node":
        return ["npm", "ci"] if (repo_dir / "package-lock.json").exists() else ["npm", "install"]
    if stack == "python" and (repo_dir / "requirements.txt").exists():
        return ["python", "-m", "pip", "install", "-r", "requirements.txt"]
    return None


def _test_command(repo_dir: Path, stack: str) -> list[str] | None:
    if stack == "node":
        return ["npm", "test"] if "test" in _package_scripts(repo_dir) else None
    if stack == "python":
        return ["python", "-m", "pytest", "-q"] if (repo_dir / "tests").is_dir() else None
    if stack == "gradle" and (repo_dir / "gradlew").exists():
        return ["sh", "./gradlew", "test", "--no-daemon"]
    if stack == "maven" and (repo_dir / "mvnw").exists():
        return ["sh", "./mvnw", "-B", "test"]
    return None


def _package_scripts(repo_dir: Path) -> dict[str, str]:
    try:
        package = json.loads(_read_text(repo_dir / "package.json") or "{}")
    except json.JSONDecodeError:
        return {}
    scripts = package.get("scripts", {})
    return scripts if isinstance(scripts, dict) else {}


def _looks_like_spring_boot(repo_dir: Path) -> bool:
    manifests = ("build.gradle", "build.gradle.kts", "settings.gradle", "settings.gradle.kts", "pom.xml")
    content = "\n".join(_read_text(repo_dir / name) for name in manifests)
    return "org.springframework.boot" in content or "spring-boot" in content or "bootRun" in content


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return ""
