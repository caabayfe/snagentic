from __future__ import annotations

import copy
import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
import yaml

import snagentic.cli.main as cli_main
from snagentic.config import EnvironmentConfig, ProjectConfig
from snagentic.errors import ServiceNowError


class FixtureCompanionBackend:
    def __init__(self, fixture: dict[str, Any]) -> None:
        self.fixture = copy.deepcopy(fixture)
        self.artifacts = copy.deepcopy(fixture["artifacts"])
        self.fail_apply = False
        self.apply_count = 0
        self.preflight_bundles: list[dict[str, Any]] = []
        self.applied_bundles: list[dict[str, Any]] = []
        self.diagnostic_queries: list[dict[str, Any]] = []

    def client(self, environment: EnvironmentConfig) -> FixtureServiceNowClient:
        return FixtureServiceNowClient(environment, self)

    def duplicate_first_artifact_path(self) -> None:
        duplicate = copy.deepcopy(self.artifacts[0])
        duplicate["inventory"]["sys_id"] = "99999999999999999999999999999999"
        duplicate["export"]["sys_id"] = "99999999999999999999999999999999"
        self.artifacts.append(duplicate)


class FixtureServiceNowClient:
    def __init__(
        self,
        environment: EnvironmentConfig,
        backend: FixtureCompanionBackend,
    ) -> None:
        self.config = environment
        self.backend = backend

    def __enter__(self) -> FixtureServiceNowClient:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def capabilities(self) -> dict[str, Any]:
        return copy.deepcopy(self.backend.fixture["capabilities"])

    def domains(self) -> list[dict[str, Any]]:
        return copy.deepcopy(self.backend.fixture["domains"])

    def contexts(self) -> list[dict[str, Any]]:
        return copy.deepcopy(self.backend.fixture["contexts"])

    def inventory(self) -> list[dict[str, Any]]:
        return copy.deepcopy([artifact["inventory"] for artifact in self.backend.artifacts])

    def export_artifact(self, inventory_item: dict[str, Any]) -> dict[str, Any]:
        sys_id = inventory_item["sys_id"]
        for artifact in self.backend.artifacts:
            if artifact["inventory"]["sys_id"] == sys_id:
                return copy.deepcopy(artifact["export"])
        raise AssertionError(f"fixture export not found for {sys_id}")

    def preflight(self, bundle: dict[str, Any]) -> dict[str, Any]:
        self.backend.preflight_bundles.append(copy.deepcopy(bundle))
        result = copy.deepcopy(self.backend.fixture["preflight"])
        result["bundle_id"] = bundle["bundle_id"]
        return result

    def apply_bundle(self, bundle: dict[str, Any]) -> dict[str, Any]:
        self.backend.apply_count += 1
        self.backend.applied_bundles.append(copy.deepcopy(bundle))
        if self.backend.fail_apply:
            failure = self.backend.fixture["partial_failure"]
            raise ServiceNowError(
                failure["message"],
                status_code=failure["status_code"],
                code=failure["code"],
                details=copy.deepcopy(failure["details"]),
            )

        completed = []
        for index, change in enumerate(bundle["changes"]):
            if change["operation"] != "update":
                raise AssertionError("the offline fixture only applies update operations")
            artifact = self._artifact(change["sys_id"])
            exported = artifact["export"]
            values = copy.deepcopy(change["values"])
            code_field = exported["code_field"]
            exported["content"] = values.pop(code_field)
            exported["fields"] = values
            exported["sys_mod_count"] += 1
            exported["revision"] = (
                f"2026-09-20 12:00:00:{exported['sys_mod_count']}"
            )
            exported["hash"] = hashlib.sha256(
                exported["content"].encode("utf-8")
            ).hexdigest()
            artifact["inventory"].update(
                {
                    "sys_mod_count": exported["sys_mod_count"],
                    "revision": exported["revision"],
                    "hash": exported["hash"],
                }
            )
            completed.append(
                {
                    "index": index,
                    "operation": "update",
                    "artifact_type": exported["artifact_type"],
                    "sys_id": exported["sys_id"],
                    "domain": {"sys_id": exported["domain"]},
                    "application_scope": {"sys_id": exported["scope_sys_id"]},
                    "revision": exported["revision"],
                    "hash": exported["hash"],
                    "tombstoned": False,
                }
            )
        return {
            "bundle_id": bundle["bundle_id"],
            "applied": True,
            "completed": completed,
        }

    def diagnostics(self, query: dict[str, Any]) -> dict[str, Any]:
        self.backend.diagnostic_queries.append(copy.deepcopy(query))
        return copy.deepcopy(self.backend.fixture["diagnostics"])

    def _artifact(self, sys_id: str) -> dict[str, Any]:
        for artifact in self.backend.artifacts:
            if artifact["export"]["sys_id"] == sys_id:
                return artifact
        raise AssertionError(f"fixture artifact not found for {sys_id}")


@dataclass
class OfflineCli:
    root: Path
    config_path: Path
    config: ProjectConfig
    backend: FixtureCompanionBackend

    def invoke(
        self,
        command: str,
        *arguments: str,
        environment: str = "dev",
    ) -> Any:
        args = cli_main.build_parser().parse_args(
            [
                "--config",
                str(self.config_path),
                "--environment",
                environment,
                command,
                *arguments,
            ]
        )
        return cli_main.run(args)

    def companion(self, environment: str = "dev") -> FixtureServiceNowClient:
        return self.backend.client(self.config.environment(environment))


@pytest.fixture
def offline_cli(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> OfflineCli:
    fixture_path = Path(__file__).parent / "fixtures" / "companion.json"
    fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
    backend = FixtureCompanionBackend(fixture)
    config_path = tmp_path / "snagentic.yaml"
    config_data = {
        "default_environment": "dev",
        "workspace": "servicenow",
        "state_directory": ".snagentic",
        "environments": {
            name: {
                "name": name,
                "url": f"https://{name}.service-now.example/",
                "kind": kind,
            }
            for name, kind in (
                ("dev", "development"),
                ("test", "test"),
                ("prod", "production"),
            )
        },
    }
    config_path.write_text(yaml.safe_dump(config_data), encoding="utf-8")
    config = ProjectConfig.model_validate(config_data)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli_main, "ServiceNowClient", backend.client)
    return OfflineCli(tmp_path, config_path, config, backend)
