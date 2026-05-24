from __future__ import annotations

import re
import subprocess
from urllib.parse import urlparse

from app.models import RepositoryPreflightReport

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
                evidence=["missing executable: git"],
            )
        except subprocess.TimeoutExpired:
            return RepositoryPreflightReport(
                repository_url=normalized_url,
                reason="git ls-remote timed out while checking repository accessibility.",
                evidence=[f"ref={remote_ref}"],
            )

        if result.returncode != 0 or not result.stdout.strip():
            return RepositoryPreflightReport(
                repository_url=normalized_url,
                reason="Repository or requested ref is not reachable.",
                evidence=[result.stderr.strip() or result.stdout.strip() or f"ref={remote_ref}"],
            )

        resolved = _extract_commit(result.stdout) or commit_sha
        return RepositoryPreflightReport(
            repository_url=normalized_url,
            cloneable=True,
            executable=True,
            resolved_commit_sha=resolved,
            detected_stack="unknown until sandbox clone",
            build_command=None,
            test_command="auto-detect in sandbox",
            run_command=None,
            reason="Repository ref is reachable; sandbox will clone and detect executable commands.",
            evidence=[line for line in result.stdout.strip().splitlines()[:3]],
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
