"""Resolve the repository that owns the demonstrated remediation surface."""
from __future__ import annotations

import re
from typing import Any


def valid_snapshot(value: Any) -> bool:
    return (isinstance(value, dict)
            and isinstance(value.get('url'), str)
            and re.fullmatch(r'https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+(?:\.git)?', value['url']) is not None
            and '..' not in value['url'].split('/')[-2:]
            and isinstance(value.get('commitSha'), str)
            and re.fullmatch(r'[a-f0-9]{40}', value['commitSha']) is not None)


def snapshots(result) -> dict[str, dict]:
    sources = result.source.get('repositories') if result else None
    if not isinstance(sources, dict):
        return {}
    return {name: dict(value) for name, value in sources.items()
            if isinstance(name, str) and re.fullmatch(r'[a-z][a-z0-9-]{0,30}', name)
            and valid_snapshot(value)}


def target_repository(result) -> str | None:
    """Configuration changes belong to main; explicit/code failures use their source."""
    repositories = snapshots(result)
    if 'main' not in repositories:
        return None
    failed = result.sandbox_report.get('failed_repository')
    if failed:
        return failed if failed in repositories else None
    observation = result.chaos_observation or {}
    observations = [observation] + [item.get('chaos_observation', {})
        for item in observation.get('scenarios', []) if isinstance(item, dict)]
    dependency_failed = any(item.get('type') in ('dependency_redis_outage', 'dependency_database_outage')
                            and item.get('recovered') is False for item in observations)
    if dependency_failed:
        target = result.source.get('fault_target', {})
        workload = target.get('deployment') or target.get('service')
        owners = result.source.get('service_repositories', {})
        matches = {owner for service, owner in owners.items()
                   if workload in (service, 'cr-svc-' + service) and owner in repositories}
        return next(iter(matches)) if len(matches) == 1 else None
    return 'main' if 'main' in repositories else None
