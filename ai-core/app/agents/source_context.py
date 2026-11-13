"""Refiner가 고칠 파일의 현재 내용을 모아준다.

왜 필요한가. Refiner는 evidence packet만 보고 unified diff를 써야 하는데, packet에는 로그
발췌까지만 들어 있고 파일 내용이 없었다. 파일을 보지 못한 상태에서 context 줄이 일치하는
diff를 쓰는 것은 구조적으로 불가능하다. 코퍼스 파일럿에서 수율이 0이었던 원인이다
(docs/evaluation-design.md 14.2).

파일은 잘라서 주지 않는다. 잘린 내용으로 쓴 diff는 context가 어긋나 적용되지 않기 때문에,
상한을 넘는 파일은 아예 주지 않고 그 사실만 남긴다.
"""

from __future__ import annotations

import ast
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from pathlib import Path as pathlib_Path

from app.agents.patching import is_test_path

MAX_FILES = 3
# 의존성 실패 로그에는 파일 경로가 없다. pip은 패키지 이름만 말한다. 고칠 파일은 매니페스트다.
MANIFEST_CANDIDATES = (
    "requirements.txt",
    "requirements-dev.txt",
    "pyproject.toml",
    "package.json",
    "Pipfile",
)
# 카오스 실패는 로그에 경로가 없다. 복원력 수정(replicas, probe, resources)은 검증 설정에 모여 있다.
CHAOS_CONFIG_CANDIDATES = (".codereferee/validation.yaml", ".codereferee/validation.yml")
# 의존성 장애 시나리오마다 그 의존성을 부르는 클라이언트 모듈. 장애가 걷힌 뒤 다시 붙지
# 못하는 결함은 이 모듈을 import한 코드에 있다. 로그에는 경로가 찍히지 않는다.
DEPENDENCY_CLIENTS = {
    "dependency_redis_outage": ("redis", "aioredis"),
    "dependency_database_outage": ("psycopg", "psycopg2", "asyncpg", "sqlalchemy", "pymysql"),
}
# 사용자 코드가 아닌 디렉터리. 가상환경을 레포에 커밋한 경우가 있다.
_VENDORED_PARTS = {".git", ".venv", "venv", "node_modules", "site-packages", "__pycache__"}
# 이보다 큰 파일은 주지 않는다. 자르면 적용 불가능한 diff가 나온다.
MAX_FILE_CHARS = 20_000
# sandbox 컨테이너 안의 clone 위치. 로그에 절대 경로로 찍힌다.
_CONTAINER_PREFIX = "/tmp/repository/"

# 실패 지점을 가리키는 표현. 이 줄에 있는 경로를 먼저 본다.
_ERROR_HINTS = ("***", "error", "failed", "syntaxerror", "indentationerror", "e   ", "assert")

_PATH_PATTERNS = (
    re.compile(r'File "([^"]+)", line \d+'),          # 파이썬 트레이스백, compileall
    re.compile(r"Compiling '([^']+)'"),                # compileall
    re.compile(r"([\w./\-]+\.\w{1,4}):\d+"),           # pytest, gcc, eslint
    re.compile(r"FAILED ([\w./\-]+\.\w{1,4})"),        # pytest 요약
    # compileall의 IndentationError는 "(conf.py, line 220)"처럼 파일명만 준다.
    re.compile(r"\(([\w./\-]+\.\w{1,4}), line \d+\)"),
)

# 사용자 레포 파일이 아닌 것.
_EXCLUDED_PREFIXES = (".git/", "site-packages/", "dist-packages/")
_EXCLUDED_MARKERS = ("/site-packages/", "/dist-packages/", "/usr/lib/", "/usr/local/lib/")


def extract_paths(log: str, limit: int = MAX_FILES) -> list[str]:
    """로그에서 레포 상대 경로를 뽑는다. 실패 지점에 가까운 것이 앞에 온다."""
    ranked: list[str] = []
    fallback: list[str] = []
    for line in log.splitlines():
        lowered = line.lower()
        target = ranked if any(hint in lowered for hint in _ERROR_HINTS) else fallback
        for pattern in _PATH_PATTERNS:
            for raw in pattern.findall(line):
                path = _normalise(raw)
                if path and path not in target:
                    target.append(path)

    # 오류 줄에서 찾은 경로가 있으면 그것만 쓴다. compileall은 성공한 파일도 전부 출력하므로
    # 자리를 채우려고 fallback을 섞으면 무관한 파일이 들어간다.
    ordered = ranked or fallback
    return ordered[:limit]


def _normalise(raw: str) -> str | None:
    path = raw.strip()
    if any(marker in path for marker in _EXCLUDED_MARKERS):
        return None
    if path.startswith(_CONTAINER_PREFIX):
        path = path[len(_CONTAINER_PREFIX) :]
    elif path.startswith("/"):
        # 레포 밖의 절대 경로다. 우리가 고칠 파일이 아니다.
        return None
    while path.startswith("./"):
        path = path[2:]
    if not path or path.startswith(_EXCLUDED_PREFIXES) or ".." in Path(path).parts:
        return None
    return path


def strip_caret_lines(log: str) -> str:
    """문법 오류의 열 위치를 가리키는 캐럿 줄을 뺀다. 파일에는 없는 줄이다.

    Refiner가 이것을 파일 내용으로 착각해 편집 앵커에 넣는 것을 두 번 관측했다. 거부 신호로
    다시 물어도 같은 실수를 반복했다. 모델을 설득하는 대신 입력에서 뺀다.
    오류 메시지 줄은 남으므로 정보는 잃지 않는다.
    """
    return "\n".join(line for line in log.splitlines() if not _is_caret_line(line))


def _is_caret_line(line: str) -> bool:
    stripped = line.strip()
    return bool(stripped) and set(stripped) == {"^"}


def read_files(
    repo_path: Path, paths: list[str], dependency_clients: tuple[str, ...] = ()
) -> dict[str, str]:
    """레포에서 파일 내용을 읽는다. 없거나 상한을 넘으면 건너뛴다.

    dependency_clients가 있으면 그 모듈을 import하는 레포 코드도 붙인다.
    """
    collected: dict[str, str] = {}
    root = repo_path.resolve()
    for path in paths:
        candidate = (root / path).resolve()
        if not candidate.is_relative_to(root) or not candidate.is_file():
            # compileall은 파일명만 주기도 한다("conf.py, line 220"). 레포에서 찾아본다.
            candidate = _resolve_basename(root, path)
            if candidate is None:
                continue
            path = str(candidate.relative_to(root))
        try:
            content = candidate.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        if len(content) > MAX_FILE_CHARS:
            continue
        collected[path] = content
    return _with_dependency_users(root, _with_tested_modules(root, collected), dependency_clients)


def _with_dependency_users(
    root: Path, collected: dict[str, str], clients: tuple[str, ...]
) -> dict[str, str]:
    """의존성 클라이언트를 import하는 레포 모듈을 붙인다. 테스트와 가상환경은 뺀다."""
    if not clients:
        return collected
    for file in sorted(root.rglob("*.py")):
        if len(collected) >= MAX_FILES:
            break
        relative = file.relative_to(root)
        path = relative.as_posix()
        if path in collected or _VENDORED_PARTS.intersection(relative.parts) or is_test_path(path):
            continue
        try:
            body = file.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        if len(body) > MAX_FILE_CHARS:
            continue
        if any(module.split(".")[0] in clients for module in _imported_modules(body)):
            collected[path] = body
    return collected


def _with_tested_modules(root: Path, collected: dict[str, str]) -> dict[str, str]:
    """실패한 테스트가 부르는 레포 모듈을 붙인다.

    assert가 테스트 안에서 실패하면 로그에는 테스트 파일만 찍힌다. 고칠 곳은 테스트가 부르는
    모듈인데, 테스트 파일만 주면 Refiner는 그것을 보지 못하고 테스트 편집은 가드가 막는다.
    레포 안에 실제로 있는 모듈만 붙인다. 표준 라이브러리와 서드파티는 파일이 없어 걸러진다.
    """
    for path, content in list(collected.items()):
        if not path.endswith(".py") or not is_test_path(path):
            continue
        for module in _imported_modules(content):
            if len(collected) >= MAX_FILES:
                return collected
            for candidate in (f"{module.replace('.', '/')}.py", f"{module.replace('.', '/')}/__init__.py"):
                file = root / candidate
                if candidate in collected or not file.is_file():
                    continue
                try:
                    body = file.read_text(encoding="utf-8")
                except (OSError, UnicodeDecodeError):
                    break
                if len(body) <= MAX_FILE_CHARS and body.strip():
                    collected[candidate] = body
                break
    return collected


def _imported_modules(content: str) -> list[str]:
    try:
        tree = ast.parse(content)
    except SyntaxError:
        return []
    modules: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module and not node.level:
            modules.append(node.module)
        elif isinstance(node, ast.Import):
            modules.extend(alias.name for alias in node.names)
    return list(dict.fromkeys(modules))


def _resolve_basename(root: pathlib_Path, path: str) -> pathlib_Path | None:
    """파일명만 주어진 경우 레포에서 찾는다. 같은 이름이 여러 개면 어느 것인지 알 수 없으므로 포기한다."""
    name = Path(path).name
    if name != path:
        return None
    matches = [p for p in root.rglob(name) if p.is_file() and ".git" not in p.parts]
    return matches[0] if len(matches) == 1 else None


def collect(
    repository_url: str,
    paths: list[str],
    *,
    branch: str | None = None,
    applied_patch: str | None = None,
    clone_timeout_seconds: int = 60,
    dependency_clients: tuple[str, ...] = (),
) -> dict[str, str]:
    """레포를 얕게 clone해 파일을 읽는다. 작업 디렉터리는 항상 지운다.

    applied_patch가 있으면 먼저 적용한다. Refiner가 만들 다음 패치는 그 위에 올라가므로
    적용 후의 내용을 봐야 context가 맞는다.
    """
    if not paths:
        return {}
    workdir = tempfile.mkdtemp(prefix="codereferee-source-")
    try:
        clone = subprocess.run(
            ["git", "clone", "--quiet", "--depth", "1", *(["--branch", branch] if branch else []),
             repository_url, workdir],
            capture_output=True,
            text=True,
            timeout=clone_timeout_seconds,
        )
        if clone.returncode != 0:
            return {}
        repo = Path(workdir)
        if applied_patch:
            applied = subprocess.run(
                ["git", "apply", "--whitespace=nowarn", "-"],
                cwd=repo,
                input=applied_patch,
                text=True,
                capture_output=True,
                timeout=60,
            )
            if applied.returncode != 0:
                # 적용 전 내용을 주면 Refiner가 어긋난 context로 패치를 쓴다. 아무것도 주지 않는다.
                return {}
        return read_files(repo, paths, dependency_clients)
    except (subprocess.TimeoutExpired, OSError):
        return {}
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
