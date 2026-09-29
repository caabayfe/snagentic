"""Platform operations through the supported ServiceNow CI/CD REST API (``sn_cicd``).

API first: Playwright UI recipes are only for operations with no API equivalent.
All state-changing operations are limited to development instances and need
explicit confirmation.
"""

from __future__ import annotations

import re
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from snagentic.errors import PolicyDeniedError, ServiceNowError
from snagentic.instance.config import InstanceConfig
from snagentic.instance.tableapi import TableApiClient
from snagentic.instance.updatesets import OPEN_STATE
from snagentic.policy import PolicyEnforcer

SAFE_VALUE = re.compile(r"^[A-Za-z0-9_.:\- ]{1,200}$")
TERMINAL_STATES = {"2": "successful", "3": "failed", "4": "canceled"}


@dataclass(frozen=True)
class Operation:
    method: str
    path: str
    path_params: tuple[str, ...] = ()
    query_params: tuple[str, ...] = ()
    required: tuple[str, ...] = ()
    mutating: bool = True
    description: str = ""


OPERATIONS: Mapping[str, Operation] = {
    "plugin.activate": Operation("POST", "plugin/{plugin_id}/activate", ("plugin_id",),
                                 description="Activate a plugin by ID"),
    "plugin.rollback": Operation("POST", "plugin/{plugin_id}/rollback", ("plugin_id",),
                                 description="Roll back a plugin activation"),
    "app.install": Operation("POST", "app_repo/install", (),
                             ("sys_id", "scope", "version", "auto_upgrade_base_app"),
                             description="Install an application from the app repository"),
    "app.rollback": Operation("POST", "app_repo/rollback", (), ("sys_id", "scope", "version"),
                              description="Roll back an application install"),
    "update_set.create": Operation("POST", "update_set/create", (),
                                   ("update_set_name", "scope", "sys_id", "description"),
                                   ("update_set_name",), description="Create a local update set"),
    "update_set.retrieve": Operation(
        "POST", "update_set/retrieve", (),
        ("update_set_id", "update_source_id", "update_source_instance_id",
         "auto_preview", "cleanup_retrieved"),
        ("update_set_id",), description="Retrieve a completed update set from a source"),
    "update_set.preview": Operation("POST", "update_set/preview/{remote_update_set_id}",
                                    ("remote_update_set_id",),
                                    description="Preview a retrieved update set"),
    "update_set.commit": Operation("POST", "update_set/commit/{remote_update_set_id}",
                                   ("remote_update_set_id",), ("force_commit",),
                                   description="Commit a previewed update set"),
    "update_set.back_out": Operation("POST", "update_set/back_out", (),
                                     ("update_set_id", "rollback_installs"), ("update_set_id",),
                                     description="Back out a committed update set"),
    "atf.run": Operation("POST", "testsuite/run", (),
                         ("test_suite_sys_id", "test_suite_name", "browser_name", "os_name"),
                         description="Run an ATF test suite"),
    "scan.full": Operation("POST", "instance_scan/full_scan", description="Full instance scan"),
    "scan.suite": Operation("POST", "instance_scan/suite_scan/{suite_sys_id}",
                            ("suite_sys_id",), description="Run a scan suite"),
    "progress": Operation("GET", "progress/{progress_id}", ("progress_id",), mutating=False,
                          description="Read the progress of an asynchronous CI/CD operation"),
}


class PlatformOperations:
    def __init__(
        self,
        config: InstanceConfig,
        client: TableApiClient,
        *,
        policy: PolicyEnforcer | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.config = config
        self.client = client
        self.policy = policy or PolicyEnforcer()
        self.sleep = sleep

    @staticmethod
    def catalog() -> list[dict[str, Any]]:
        return [
            {"operation": name, "description": op.description, "mutating": op.mutating,
             "parameters": [*op.path_params, *op.query_params], "required": [
                 *op.path_params, *op.required]}
            for name, op in sorted(OPERATIONS.items())
        ]

    def run(
        self,
        name: str,
        parameters: Mapping[str, str],
        *,
        confirm: bool,
        wait: bool = True,
        timeout_seconds: float = 900,
    ) -> dict[str, Any]:
        operation = OPERATIONS.get(name)
        if operation is None:
            raise ValueError(f"unknown operation: {name}")
        allowed = set(operation.path_params) | set(operation.query_params)
        unknown = set(parameters) - allowed
        if unknown:
            raise ValueError(f"unsupported parameters for {name}: {', '.join(sorted(unknown))}")
        missing = [key for key in (*operation.path_params, *operation.required)
                   if not parameters.get(key)]
        if missing:
            raise ValueError(f"missing parameters for {name}: {', '.join(missing)}")
        for key, value in parameters.items():
            if not SAFE_VALUE.fullmatch(str(value)):
                raise ValueError(f"unsafe value for {key}")
        if operation.mutating:
            self.policy.require_write_allowed(self.config.kind)
            if not confirm:
                raise PolicyDeniedError(f"{name} requires explicit confirmation")
        path = "api/sn_cicd/" + operation.path.format(
            **{key: parameters[key] for key in operation.path_params}
        )
        query = {key: parameters[key] for key in operation.query_params if key in parameters}
        payload = self.client.call(operation.method, path, params=query, retries=0) or {}
        result = payload.get("result") if isinstance(payload.get("result"), dict) else payload
        if not isinstance(result, dict):
            raise ServiceNowError(f"CI/CD operation {name} returned a non-object result")
        progress_id = _progress_id(result)
        if not wait or progress_id is None or name == "progress":
            return {"operation": name, "result": result, "progress_id": progress_id}
        return {"operation": name, "progress_id": progress_id,
                "result": self.wait(progress_id, timeout_seconds=timeout_seconds)}

    def wait(self, progress_id: str, *, timeout_seconds: float = 900) -> dict[str, Any]:
        deadline = time.monotonic() + timeout_seconds
        delay = 2.0
        while True:
            payload = self.client.call("GET", f"api/sn_cicd/progress/{progress_id}") or {}
            result = payload.get("result") if isinstance(payload.get("result"), dict) else {}
            if not isinstance(result, dict):
                raise ServiceNowError(
                    f"CI/CD progress {progress_id} returned a non-object result"
                )
            status = str(result.get("status", ""))
            if status in TERMINAL_STATES:
                outcome = {**result, "outcome": TERMINAL_STATES[status]}
                if status != "2":
                    raise ServiceNowError(
                        f"CI/CD operation {progress_id} {TERMINAL_STATES[status]}: "
                        f"{str(result.get('status_message') or result.get('error') or '')[:300]}"
                    )
                return outcome
            if time.monotonic() > deadline:
                raise ServiceNowError(f"timed out waiting for CI/CD operation {progress_id}")
            self.sleep(delay)
            delay = min(delay * 1.5, 30.0)


def _progress_id(result: Mapping[str, Any]) -> str | None:
    links = result.get("links")
    if isinstance(links, Mapping):
        progress = links.get("progress")
        if isinstance(progress, Mapping) and progress.get("id"):
            return str(progress["id"])
    return None


def complete_update_set(
    config: InstanceConfig,
    client: TableApiClient,
    update_set_id: str,
    *,
    confirm: bool,
    policy: PolicyEnforcer | None = None,
) -> dict[str, Any]:
    """Mark an agent update set complete so it can be promoted with standard tooling."""

    (policy or PolicyEnforcer()).require_write_allowed(config.kind)
    if not confirm:
        raise PolicyDeniedError("completing an update set requires explicit confirmation")
    current = client.get("sys_update_set", update_set_id)
    if current is None:
        raise ServiceNowError(f"update set not found: {update_set_id}")
    if not str(current.get("name", "")).startswith("snagentic: "):
        raise PolicyDeniedError("only snagentic-owned update sets can be completed by the tool")
    updated = client.update("sys_update_set", update_set_id, {"state": "complete"})
    return {"sys_id": update_set_id, "name": updated.get("name"), "state": updated.get("state")}


def promote(
    config: InstanceConfig,
    client: TableApiClient,
    *,
    label: str,
    confirm: bool,
    created_at: str,
    git_commit: str | None = None,
    policy: PolicyEnforcer | None = None,
) -> dict[str, Any]:
    """Complete the agent update sets for ``label`` and return a content-free manifest.

    The manifest lists update sets and change counts only (no payloads or field
    values). Moving the completed sets to test or production stays with the
    supported ServiceNow deployment process; snagentic never writes there.
    """

    from snagentic.instance.changes import validate_label

    validate_label(label)
    prefix = f"snagentic: {label} ["
    rows = client.query(
        "sys_update_set",
        query=f"nameSTARTSWITH{prefix}^state={OPEN_STATE}",
        fields=["sys_id", "name", "application"],
    ) or []
    rows = [row for row in rows if str(row.get("name", "")).startswith(prefix)]
    if not rows:
        raise ServiceNowError(f"no open snagentic update sets for {label!r}")
    update_sets: list[dict[str, Any]] = []
    for row in sorted(rows, key=lambda item: str(item.get("name"))):
        entries = list(client.iterate(
            "sys_update_xml", query=f"update_set={row['sys_id']}^ORDERBYname",
            fields=["type", "action", "name"],
        ))
        by_type: dict[str, int] = {}
        for entry in entries:
            key = f"{entry.get('type') or 'unknown'}:{entry.get('action') or 'unknown'}"
            by_type[key] = by_type.get(key, 0) + 1
        completed = complete_update_set(config, client, str(row["sys_id"]), confirm=confirm,
                                        policy=policy)
        update_sets.append({
            "sys_id": str(row["sys_id"]),
            "name": str(row.get("name")),
            "application": str(row.get("application") or ""),
            "state": completed["state"],
            "changes": len(entries),
            "changes_by_type": dict(sorted(by_type.items())),
            "records": sorted({str(entry.get("name")) for entry in entries}),
        })
    return {
        "manifest_version": 1,
        "source_instance": config.name,
        "source_kind": config.kind,
        "label": label,
        "created_at": created_at,
        "git_commit": git_commit,
        "mechanism": "update_set",
        "update_sets": update_sets,
        "next_steps": [
            "Retrieve the completed update sets on the target instance "
            "(update source or sn_cicd update_set/retrieve run from the target pipeline).",
            "Preview, resolve problems, and commit using the supported deployment process.",
        ],
    }
