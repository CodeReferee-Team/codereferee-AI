"""Refiner가 고칠 파일의 현재 내용을 모아준다.

왜 필요한가. Refiner는 evidence packet만 보고 unified diff를 써야 하는데, packet에는 로그
발췌까지만 들어 있고 파일 내용이 없었다. 파일을 보지 못한 상태에서 context 줄이 일치하는
diff를 쓰는 것은 구조적으로 불가능하다. 코퍼스 파일럿에서 수율이 0이었던 원인이다
(docs/evaluation-design.md 14.2).

파일은 잘라서 주지 않는다. 잘린 내용으로 쓴 diff는 context가 어긋나 적용되지 않기 때문에,
상한을 넘는 파일은 아예 주지 않고 그 사실만 남긴다.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from pathlib import Path as pathlib_Path

MAX_FILES = 3
# 의존성 실패 로그에는 파일 경로가 없다. pip은 패키지 이름만 말한다. 고칠 파일은 매니페스트다.
MANIFEST_CANDIDATES = (
    "requirements.txt",
    "requirements-dev.txt",
    "pyproject.toml",
    "package.json",
    "Pipfile",
)
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


def read_files(repo_path: Path, paths: list[str]) -> dict[str, str]:
    """레포에서 파일 내용을 읽는다. 없거나 상한을 넘으면 건너뛴다."""
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
    return collected


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
        return read_files(repo, paths)
    except (subprocess.TimeoutExpired, OSError):
        return {}
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
