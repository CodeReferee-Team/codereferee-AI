from __future__ import annotations

import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from urllib.parse import urlparse

from app.config import get_settings
from app.models import RepositoryPreflightReport
from app.repository import stack_detection

_GITHUB_HOSTS = {"github.com", "www.github.com"}
_COMMIT_RE = re.compile(r"^[0-9a-f]{40}\s+HEAD$", re.MULTILINE)


class RepositoryPreflightRunner:
    def run(self, repository_url: str, branch: str | None = None, commit_sha: str | None = None) -> RepositoryPreflightReport:
        normalized_url = _normalize_github_url(repository_url)
        if normalized_url is None:
            return RepositoryPreflightReport(
                repository_url=repository_url,
                reason="Only public GitHub HTTPS repository URLs are supported in the current MVP.",
                evidence=["Expected URL shape: https://github.com/{owner}/{repo}"],
            )

        remote_ref = branch or commit_sha or "HEAD"
        try:
            result = subprocess.run(
                ["git", "ls-remote", normalized_url, remote_ref],
                check=False,
                text=True,
                capture_output=True,
                timeout=15,
            )
        except FileNotFoundError:
            return RepositoryPreflightReport(
                repository_url=normalized_url,
                reason="git is not installed on the AI core host, so repository intake cannot be verified.",
                infra_error="git_not_installed",
                evidence=["missing executable: git"],
            )
        except subprocess.TimeoutExpired:
            return RepositoryPreflightReport(
                repository_url=normalized_url,
                reason="git ls-remote timed out while checking repository accessibility.",
                infra_error="preflight_timeout",
                evidence=[f"ref={remote_ref}"],
            )

        if result.returncode != 0 or not result.stdout.strip():
            return RepositoryPreflightReport(
                repository_url=normalized_url,
                reason="Repository or requested ref is not reachable.",
                evidence=[result.stderr.strip() or result.stdout.strip() or f"ref={remote_ref}"],
            )

        resolved = _extract_commit(result.stdout) or commit_sha
        evidence = [line for line in result.stdout.strip().splitlines()[:3]]
        profile = _detect_profile(normalized_url, branch)
        if profile is None:
            # ls-remote로 접근성은 이미 증명됐다. 감지에 실패했다고 검증을 막지 않는다.
            # sandbox가 clone 후에 다시 판단한다.
            return RepositoryPreflightReport(
                repository_url=normalized_url,
                cloneable=True,
                executable=True,
                resolved_commit_sha=resolved,
                detected_stack="unknown until sandbox clone",
                test_command="auto-detect in sandbox",
                reason="Repository ref is reachable; stack detection was skipped and the sandbox will detect it.",
                evidence=evidence,
            )

        run_target = profile.run_target
        return RepositoryPreflightReport(
            repository_url=normalized_url,
            cloneable=True,
            executable=profile.stack != "unknown",
            resolved_commit_sha=resolved,
            detected_stack=profile.stack,
            build_command=" ".join(profile.build_command) if profile.build_command else None,
            test_command=" ".join(profile.test_command) if profile.test_command else None,
            run_command=" ".join(run_target.command) if run_target else None,
            reason=(
                f"Repository ref is reachable; detected {profile.stack}"
                + (f" service on port {run_target.port}." if run_target else " with no runnable service.")
            ),
            evidence=evidence + [f"is_service={profile.is_service}"],
        )


def _normalize_github_url(repository_url: str) -> str | None:
    parsed = urlparse(repository_url)
    if parsed.scheme != "https" or parsed.netloc.lower() not in _GITHUB_HOSTS:
        return None
    parts = [part for part in parsed.path.strip("/").split("/") if part]
    if len(parts) < 2:
        return None
    owner, repo = parts[0], parts[1].removesuffix(".git")
    if not owner or not repo:
        return None
    return f"https://github.com/{owner}/{repo}.git"


def _extract_commit(ls_remote_output: str) -> str | None:
    first_hash = ls_remote_output.split()[0] if ls_remote_output.split() else None
    return first_hash if first_hash and re.fullmatch(r"[0-9a-f]{40}", first_hash) else None


repository_preflight_runner = RepositoryPreflightRunner()


def _detect_profile(repository_url: str, branch: str | None) -> stack_detection.ProjectProfile | None:
    """얕게 clone해 스택과 커맨드를 판단한다. 실패하면 None을 돌려 판정을 막지 않는다.

    sandbox도 clone 후에 스택을 판단하지만, 실행 커맨드는 그보다 먼저 필요하다. Kubernetes에
    띄우려면 Deployment를 만들 때 커맨드와 포트가 있어야 하고, 실행할 서비스가 없는 레포
    (라이브러리)는 카오스 검증 대상이 아니라는 판별도 여기서 나온다.
    """
    workdir = tempfile.mkdtemp(prefix="codereferee-preflight-")
    try:
        done = subprocess.run(
            ["git", "clone", "--quiet", "--depth", "1", *(["--branch", branch] if branch else []),
             repository_url, workdir],
            capture_output=True,
            text=True,
            timeout=get_settings().repository_clone_timeout_seconds,
        )
        if done.returncode != 0:
            return None
        return stack_detection.profile(Path(workdir))
    except (subprocess.TimeoutExpired, OSError):
        return None
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
