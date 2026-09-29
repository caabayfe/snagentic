"""Bounded local storage for redacted diagnostic snapshots."""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from snagentic.client import ServiceNowClient
from snagentic.policy import PolicyEnforcer


class DiagnosticService:
    def __init__(self, directory: Path, client: ServiceNowClient) -> None:
        self.directory = directory
        self.client = client
        self.policy = PolicyEnforcer()

    def collect(
        self,
        *,
        minutes: int = 60,
        limit: int = 500,
        domain: str | None = None,
    ) -> Path:
        self.policy.validate_diagnostic_request(minutes=minutes, limit=limit)
        query: dict[str, Any] = {"minutes": minutes, "limit": limit}
        if domain:
            query["domain"] = domain
        payload = self.client.diagnostics(query)
        self.directory.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        destination = self.directory / f"diagnostics-{timestamp}.json"
        temporary = destination.with_suffix(".tmp")
        temporary.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        os.replace(temporary, destination)
        return destination

    def expire(self, *, ttl_hours: int = 168) -> int:
        if ttl_hours < 1:
            raise ValueError("ttl_hours must be positive")
        cutoff = datetime.now(UTC) - timedelta(hours=ttl_hours)
        removed = 0
        if not self.directory.exists():
            return removed
        for path in self.directory.glob("diagnostics-*.json"):
            modified = datetime.fromtimestamp(path.stat().st_mtime, UTC)
            if modified < cutoff:
                path.unlink()
                removed += 1
        return removed
