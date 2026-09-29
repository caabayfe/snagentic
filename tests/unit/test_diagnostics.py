import json
from pathlib import Path
from typing import Any

import pytest

from snagentic.diagnostics import DiagnosticService
from snagentic.errors import PolicyDeniedError


class FakeClient:
    def diagnostics(self, query: dict[str, Any]) -> dict[str, Any]:
        return {"query": query, "records": []}


def test_collect_writes_local_snapshot(tmp_path: Path) -> None:
    service = DiagnosticService(tmp_path, FakeClient())  # type: ignore[arg-type]
    path = service.collect(minutes=10, limit=20, domain="customer-a")
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["query"]["domain"] == "customer-a"


def test_collect_enforces_bounds(tmp_path: Path) -> None:
    service = DiagnosticService(tmp_path, FakeClient())  # type: ignore[arg-type]
    with pytest.raises(PolicyDeniedError, match="minutes"):
        service.collect(minutes=0)
