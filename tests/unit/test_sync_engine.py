from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import yaml

from snagentic.config import ProjectConfig
from snagentic.errors import ConflictError, ServiceNowError
from snagentic.sync.engine import SyncEngine


class FakeClient:
    config = SimpleNamespace(name="dev", kind="development", writable=True)
    fail_domains = False
    apply_count = 0

    def domains(self) -> list[dict[str, Any]]:
        if self.fail_domains:
            raise ServiceNowError("simulated pull failure")
        return [{"stable_id": "customer-a", "name": "Customer A", "parent": "global"}]

    def inventory(self) -> list[dict[str, Any]]:
        return [
            {
                "id": "one",
                "artifact_type": "script_include",
                "stable_key": "hello",
                "domain": "customer-a",
                "scope": "x_example",
            }
        ]

    def export_artifact(self, artifact: dict[str, Any]) -> dict[str, Any]:
        assert artifact["id"] == "one"
        return {
            "id": "one",
            "sys_id": "11111111111111111111111111111111",
            "artifact_type": "script_include",
            "stable_key": "hello",
            "domain": "customer-a",
            "scope": "x_example",
            "scope_sys_id": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
            "sys_mod_count": 3,
            "revision": "2026-09-20 10:00:00:3",
            "hash": "0" * 64,
            "extension": "js",
            "code_field": "script",
            "fields": {"name": "hello", "active": True},
            "content": "function hello() {\r\n  return true;\r\n}\r\n",
        }

    def preflight(self, bundle: dict[str, Any]) -> dict[str, Any]:
        return {"valid": True, "bundle_id": bundle["bundle_id"]}

    def apply_bundle(self, bundle: dict[str, Any]) -> dict[str, Any]:
        self.apply_count += 1
        return {"applied": True, "completed": [{"index": 0}]}


def test_pull_is_deterministic_and_domain_partitioned(tmp_path: Path) -> None:
    config = ProjectConfig.model_validate(
        {
            "environments": {
                "dev": {
                    "name": "dev",
                    "url": "https://dev.service-now.com/",
                    "kind": "development",
                }
            }
        }
    )
    engine = SyncEngine(tmp_path, config, FakeClient())  # type: ignore[arg-type]
    result = engine.pull()
    source = (
        tmp_path
        / "servicenow/domains/customer-a/scopes/x_example/script_include/hello/source.js"
    )
    assert result.artifact_count == 1
    assert source.read_text(encoding="utf-8") == "function hello() {\n  return true;\n}\n"
    assert engine.status() == {"added": [], "deleted": [], "modified": []}

    source.write_text("function hello() {\n  return false;\n}\n", encoding="utf-8")
    plan = engine.create_change_plan()
    assert plan["changes"] == [
        {
            "operation": "update",
            "artifact_type": "script_include",
            "sys_id": "11111111111111111111111111111111",
            "domain": {"sys_id": "customer-a"},
            "application_scope": {
                "sys_id": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
            },
            "expected": {
                "sys_mod_count": 3,
                "revision": "2026-09-20 10:00:00:3",
                "hash": "0" * 64,
            },
            "values": {
                "name": "hello",
                "active": True,
                "script": "function hello() {\n  return false;\n}\n",
            },
        }
    ]

    notes = source.parent / "notes.txt"
    notes.write_text("not a managed artifact file\n", encoding="utf-8")
    with pytest.raises(ConflictError, match="unexpected or missing files"):
        engine.create_change_plan()
    notes.unlink()

    expected_source = source.read_text(encoding="utf-8")
    source.unlink()
    with pytest.raises(ConflictError, match="unexpected or missing files"):
        engine.create_change_plan()
    source.write_text(expected_source, encoding="utf-8")

    metadata_path = source.parent / "metadata.yaml"
    metadata = yaml.safe_load(metadata_path.read_text(encoding="utf-8"))
    metadata["domain"] = "another-domain"
    metadata_path.write_text(yaml.safe_dump(metadata), encoding="utf-8")
    with pytest.raises(ValueError, match="domain/scope path"):
        engine.create_change_plan()


def test_failed_post_apply_pull_blocks_replay_until_reconciled(tmp_path: Path) -> None:
    config = ProjectConfig.model_validate(
        {
            "environments": {
                "dev": {
                    "name": "dev",
                    "url": "https://dev.service-now.com/",
                    "kind": "development",
                }
            }
        }
    )
    client = FakeClient()
    engine = SyncEngine(tmp_path, config, client)  # type: ignore[arg-type]
    engine.pull()
    source = (
        tmp_path
        / "servicenow/domains/customer-a/scopes/x_example/script_include/hello/source.js"
    )
    source.write_text("function hello() {\n  return false;\n}\n", encoding="utf-8")
    client.fail_domains = True
    with pytest.raises(ServiceNowError, match="simulated pull failure"):
        engine.push(approved=True)
    assert client.apply_count == 1
    with pytest.raises(ConflictError, match="successful pull"):
        engine.push(approved=True)
    assert client.apply_count == 1
    client.fail_domains = False
    engine.pull()
    assert not (tmp_path / ".snagentic/reconciliation").exists()
