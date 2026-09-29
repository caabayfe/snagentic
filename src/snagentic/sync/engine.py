"""Atomic pull and three-state synchronization orchestration."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
from collections.abc import Iterable, Mapping
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

from snagentic.artifacts.normalization import content_hash
from snagentic.artifacts.registry import DEFAULT_ARTIFACT_REGISTRY
from snagentic.client import ServiceNowClient
from snagentic.config import ProjectConfig
from snagentic.domains import DomainPathMapper
from snagentic.errors import ConflictError, ServiceNowError
from snagentic.index import ArtifactIndex
from snagentic.models import (
    ArtifactIdentity,
    ArtifactRevision,
    NormalizedArtifact,
    Provenance,
)
from snagentic.policy import PolicyEnforcer


@dataclass(frozen=True)
class PullResult:
    artifact_count: int
    domain_count: int
    cursor: str | None

    def as_dict(self) -> dict[str, Any]:
        return {
            "artifact_count": self.artifact_count,
            "domain_count": self.domain_count,
            "cursor": self.cursor,
        }


class SyncEngine:
    def __init__(self, root: Path, config: ProjectConfig, client: ServiceNowClient) -> None:
        self.root = root.resolve()
        self.config = config
        self.client = client
        self.workspace = (self.root / config.workspace).resolve()
        self.state = (self.root / config.state_directory).resolve()
        if not self.workspace.is_relative_to(self.root):
            raise ValueError("workspace must stay inside the repository root")
        if not self.state.is_relative_to(self.root):
            raise ValueError("state_directory must stay inside the repository root")
        if self.workspace.is_relative_to(self.state) or self.state.is_relative_to(
            self.workspace
        ):
            raise ValueError("workspace and state_directory must not contain each other")
        self.paths = DomainPathMapper(self.workspace)
        self.policy = PolicyEnforcer()

    def inventory(self) -> list[dict[str, Any]]:
        return list(self.client.inventory())

    def pull(self) -> PullResult:
        domains = self.client.domains()
        inventory = list(self.client.inventory())
        indexed_artifacts: list[NormalizedArtifact] = []
        self.state.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="snagentic-", dir=self.state) as temporary:
            staging = Path(temporary) / "workspace"
            staging.mkdir()
            self._write_yaml(staging / "manifest.yaml", self._manifest(domains, inventory))
            for domain in domains:
                stable_id = _required_slug(domain, "stable_id")
                self._write_yaml(staging / "domains" / stable_id / "domain.yaml", domain)
            for item in inventory:
                artifact = self.client.export_artifact(item)
                target = self._artifact_path(staging, artifact)
                if target.exists():
                    raise ValueError(
                        f"multiple ServiceNow artifacts resolved to the same path: {target}"
                    )
                self._write_artifact(target, artifact)
                indexed_artifacts.append(self._normalized_artifact(artifact))
            self._replace_workspace(staging)
        baseline = self.state / "baselines" / "latest"
        if baseline.exists():
            shutil.rmtree(baseline)
        baseline.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(self.workspace, baseline)
        ArtifactIndex(self.state / "state.sqlite").rebuild(indexed_artifacts)
        self._write_json(self.state / "sync-state.json", {"cursor": None})
        self._clear_reconciliation_markers()
        return PullResult(len(inventory), len(domains), None)

    def status(self) -> dict[str, list[str]]:
        baseline = self.state / "baselines" / "latest"
        current = self.workspace
        baseline_files = _file_hashes(baseline)
        current_files = _file_hashes(current)
        return {
            "added": sorted(current_files.keys() - baseline_files.keys()),
            "deleted": sorted(baseline_files.keys() - current_files.keys()),
            "modified": sorted(
                path
                for path in current_files.keys() & baseline_files.keys()
                if current_files[path] != baseline_files[path]
            ),
        }

    def create_change_plan(self) -> dict[str, Any]:
        baseline_root = self.state / "baselines" / "latest"
        baseline_artifacts = _artifact_directories(baseline_root)
        current_artifacts = _artifact_directories(self.workspace)
        roots: list[tuple[str, str, Path]] = []
        for relative in sorted(current_artifacts.keys() - baseline_artifacts.keys()):
            roots.append(("create", relative, current_artifacts[relative]))
        for relative in sorted(baseline_artifacts.keys() - current_artifacts.keys()):
            roots.append(("delete", relative, baseline_artifacts[relative]))
        for relative in sorted(current_artifacts.keys() & baseline_artifacts.keys()):
            if _file_hashes(current_artifacts[relative]) != _file_hashes(
                baseline_artifacts[relative]
            ):
                roots.append(("update", relative, current_artifacts[relative]))
        changes = [
            self._change_from_directory(operation, path)
            for operation, _, path in roots
        ]
        encoded = json.dumps(changes, sort_keys=True, separators=(",", ":")).encode()
        return {
            "bundle_id": hashlib.sha256(encoded).hexdigest()[:32],
            "changes": changes,
        }

    def push(self, *, approved: bool) -> dict[str, Any]:
        self.policy.require_write_allowed(self.client.config.kind)
        self._require_no_pending_reconciliation()
        plan = self.create_change_plan()
        if not plan["changes"]:
            return {"status": "no_changes", "bundle_id": plan["bundle_id"]}
        if not approved:
            raise ConflictError("push requires explicit approval")
        preflight = self.client.preflight(plan)
        if preflight.get("valid") is not True:
            raise ConflictError("ServiceNow preflight rejected the change plan")
        self._write_reconciliation_marker(
            plan,
            status="apply_in_progress",
        )
        try:
            result = self.client.apply_bundle(plan)
        except ServiceNowError as exc:
            self._record_reconciliation_required(plan, exc)
            raise
        self._write_reconciliation_marker(
            plan,
            status="apply_succeeded_pull_required",
            details=_safe_apply_summary(result),
        )
        self.pull()
        return result

    def _change_from_directory(self, operation: str, directory: Path) -> dict[str, Any]:
        metadata_path = directory / "metadata.yaml"
        metadata = yaml.safe_load(metadata_path.read_text(encoding="utf-8"))
        if not isinstance(metadata, dict):
            raise ValueError(f"artifact metadata must be a mapping: {metadata_path}")
        source_root = (
            self.state / "baselines" / "latest"
            if directory.is_relative_to(self.state / "baselines" / "latest")
            else self.workspace
        )
        identity = ArtifactIdentity(
            artifact_type=_required_text(metadata, "artifact_type"),
            scope=_required_text(metadata, "scope"),
            domain_stable_id=(
                "global"
                if metadata.get("domain") in (None, "", "global")
                else _required_text(metadata, "domain")
            ),
            natural_key=_required_text(metadata, "stable_key"),
            sys_id=metadata.get("sys_id") if isinstance(metadata.get("sys_id"), str) else None,
        )
        expected_directory = DomainPathMapper(source_root).artifact_directory(identity)
        if directory.resolve() != expected_directory:
            raise ValueError(
                f"artifact metadata does not match its domain/scope path: {directory}"
            )
        artifact_type = _required_text(metadata, "artifact_type")
        if not DEFAULT_ARTIFACT_REGISTRY.supports_write(artifact_type):
            raise ConflictError(
                f"artifact type is not managed bidirectionally: {artifact_type}"
            )
        change_operation = operation
        domain = metadata.get("domain")
        domain_id = "global" if domain in (None, "", "global") else str(domain)
        change: dict[str, Any] = {
            "operation": change_operation,
            "artifact_type": artifact_type,
            "domain": {"sys_id": domain_id},
            "application_scope": {
                "sys_id": _required_text(metadata, "scope_sys_id")
            },
        }
        if change_operation != "create":
            change["sys_id"] = _required_text(metadata, "sys_id")
            change["expected"] = {
                "sys_mod_count": _required_nonnegative_int(metadata, "sys_mod_count"),
                "revision": _required_text(metadata, "revision"),
                "hash": _required_sha256(metadata, "hash"),
            }
        if change_operation != "delete":
            change["values"] = self._artifact_values(directory, metadata)
        return change

    def _artifact_values(
        self, directory: Path, metadata: Mapping[str, Any]
    ) -> dict[str, Any]:
        record_path = directory / "record.yaml"
        if record_path.exists():
            _require_artifact_files(directory, {"metadata.yaml", "record.yaml"})
            values = yaml.safe_load(record_path.read_text(encoding="utf-8"))
            if not isinstance(values, dict) or not values:
                raise ValueError(f"artifact record must be a non-empty mapping: {record_path}")
            return self.policy.exclude_sensitive_fields(values)
        extension = _required_slug(metadata, "extension")
        source_path = directory / f"source.{extension}"
        _require_artifact_files(
            directory,
            {"metadata.yaml", source_path.name},
        )
        code_field = _required_text(metadata, "code_field")
        fields = metadata.get("fields", {})
        if not isinstance(fields, Mapping):
            raise ValueError(f"artifact fields must be a mapping: {directory}")
        values = self.policy.exclude_sensitive_fields(fields)
        values[code_field] = source_path.read_text(encoding="utf-8")
        return values

    def _record_reconciliation_required(
        self, plan: Mapping[str, Any], error: ServiceNowError
    ) -> None:
        self._write_reconciliation_marker(
            plan,
            status="reconciliation_required",
            error_code=error.code,
            status_code=error.status_code,
            details=error.details,
        )

    def _write_reconciliation_marker(
        self,
        plan: Mapping[str, Any],
        *,
        status: str,
        error_code: str | None = None,
        status_code: int | None = None,
        details: Any = None,
    ) -> None:
        destination = self.state / "reconciliation" / f"{plan['bundle_id']}.json"
        self._write_json(
            destination,
            {
                "bundle_id": plan["bundle_id"],
                "status": status,
                "error_code": error_code,
                "status_code": status_code,
                "details": details if details is not None else [],
            },
        )

    def _require_no_pending_reconciliation(self) -> None:
        directory = self.state / "reconciliation"
        pending = sorted(directory.glob("*.json")) if directory.exists() else []
        if pending:
            names = ", ".join(path.stem for path in pending[:5])
            raise ConflictError(
                "push is blocked until a successful pull reconciles pending bundle(s): "
                f"{names}"
            )

    def _clear_reconciliation_markers(self) -> None:
        directory = self.state / "reconciliation"
        if not directory.exists():
            return
        for marker in directory.glob("*.json"):
            marker.unlink()
        with suppress(OSError):
            directory.rmdir()

    def _artifact_path(self, staging: Path, artifact: Mapping[str, Any]) -> Path:
        domain = artifact.get("domain")
        identity = ArtifactIdentity(
            artifact_type=_required_slug(artifact, "artifact_type"),
            scope=_required_slug(artifact, "scope"),
            domain_stable_id="global"
            if domain in (None, "", "global")
            else _required_slug(artifact, "domain"),
            natural_key=_required_slug(artifact, "stable_key"),
            sys_id=artifact.get("sys_id") if isinstance(artifact.get("sys_id"), str) else None,
        )
        return DomainPathMapper(staging).artifact_directory(identity)

    def _write_artifact(self, target: Path, artifact: Mapping[str, Any]) -> None:
        content = artifact.get("content")
        metadata = self.policy.exclude_sensitive_fields(
            {key: value for key, value in artifact.items() if key != "content"}
        )
        self._write_yaml(target / "metadata.yaml", metadata)
        if isinstance(content, str):
            extension = _required_slug(artifact, "extension")
            _atomic_write(target / f"source.{extension}", _normalize_text(content))
        elif isinstance(content, Mapping):
            self._write_yaml(
                target / "record.yaml",
                self.policy.exclude_sensitive_fields(content),
            )
        else:
            raise ValueError("artifact content must be text or an object")

    def _normalized_artifact(self, artifact: Mapping[str, Any]) -> NormalizedArtifact:
        now = datetime.now(UTC)
        domain = artifact.get("domain")
        identity = ArtifactIdentity(
            artifact_type=_required_text(artifact, "artifact_type"),
            scope=_required_text(artifact, "scope"),
            domain_stable_id="global"
            if domain in (None, "", "global")
            else _required_text(artifact, "domain"),
            natural_key=_required_text(artifact, "stable_key"),
            sys_id=artifact.get("sys_id") if isinstance(artifact.get("sys_id"), str) else None,
        )
        content = artifact.get("content")
        safe_content = (
            self.policy.exclude_sensitive_fields(content)
            if isinstance(content, Mapping)
            else content
        )
        if not isinstance(safe_content, (str, Mapping)):
            raise ValueError("artifact content must be text or an object")
        revision = ArtifactRevision(
            identity=identity,
            content_hash=content_hash(safe_content),
            revision=str(artifact.get("revision") or artifact.get("sys_mod_count") or "")
            or None,
            observed_at=now,
        )
        return NormalizedArtifact(
            identity=identity,
            metadata=self.policy.exclude_sensitive_fields(
                {key: value for key, value in artifact.items() if key != "content"}
            ),
            content=safe_content,
            revision=revision,
            provenance=Provenance(
                source_instance=self.client.config.name,
                source_table=str(artifact.get("table") or identity.artifact_type),
                source_sys_id=identity.sys_id,
                captured_at=now,
                correlation_id=(
                    str(artifact["correlation_id"])
                    if artifact.get("correlation_id") is not None
                    else None
                ),
            ),
        )

    def _replace_workspace(self, staging: Path) -> None:
        self.workspace.parent.mkdir(parents=True, exist_ok=True)
        backup = self.workspace.with_name(f"{self.workspace.name}.previous")
        if backup.exists():
            shutil.rmtree(backup)
        if self.workspace.exists():
            os.replace(self.workspace, backup)
        try:
            os.replace(staging, self.workspace)
        except BaseException:
            if backup.exists() and not self.workspace.exists():
                os.replace(backup, self.workspace)
            raise
        if backup.exists():
            shutil.rmtree(backup)

    @staticmethod
    def _manifest(
        domains: Iterable[Mapping[str, Any]], inventory: Iterable[Mapping[str, Any]]
    ) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "domains": list(domains),
            "artifacts": sorted(
                (dict(item) for item in inventory),
                key=lambda item: (
                    str(item.get("domain", "")),
                    str(item.get("artifact_type", "")),
                    str(item.get("stable_key", "")),
                ),
            ),
        }

    @staticmethod
    def _write_yaml(path: Path, value: Mapping[str, Any]) -> None:
        _atomic_write(path, yaml.safe_dump(dict(value), sort_keys=True, allow_unicode=False))

    @staticmethod
    def _write_json(path: Path, value: Mapping[str, Any]) -> None:
        _atomic_write(path, json.dumps(value, indent=2, sort_keys=True) + "\n")


def _required_text(value: Mapping[str, Any], key: str) -> str:
    candidate = value.get(key)
    if not isinstance(candidate, str) or not candidate:
        raise ValueError(f"artifact field {key!r} must be a non-empty string")
    return candidate


def _required_slug(value: Mapping[str, Any], key: str) -> str:
    candidate = _required_text(value, key)
    if candidate in {".", ".."} or "/" in candidate or "\\" in candidate:
        raise ValueError(f"artifact field {key!r} is not a safe path component")
    return candidate


def _required_nonnegative_int(value: Mapping[str, Any], key: str) -> int:
    candidate = value.get(key)
    if isinstance(candidate, bool) or not isinstance(candidate, (int, str)):
        raise ValueError(f"artifact field {key!r} must be a non-negative integer")
    try:
        result = int(candidate)
    except (TypeError, ValueError) as exc:
        raise ValueError(
            f"artifact field {key!r} must be a non-negative integer"
        ) from exc
    if result < 0:
        raise ValueError(f"artifact field {key!r} must be a non-negative integer")
    return result


def _required_sha256(value: Mapping[str, Any], key: str) -> str:
    candidate = _required_text(value, key).lower()
    if len(candidate) != 64 or any(character not in "0123456789abcdef" for character in candidate):
        raise ValueError(f"artifact field {key!r} must be a SHA-256 hash")
    return candidate


def _normalize_text(value: str) -> str:
    return value.replace("\r\n", "\n").replace("\r", "\n").rstrip() + "\n"


def _atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(content, encoding="utf-8", newline="\n")
    os.replace(temporary, path)


def _file_hashes(root: Path) -> dict[str, str]:
    if not root.exists():
        return {}
    result: dict[str, str] = {}
    for path in sorted(candidate for candidate in root.rglob("*") if candidate.is_file()):
        result[path.relative_to(root).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
    return result


def _find_artifact_root(root: Path, relative: Path) -> Path | None:
    candidate = (root / relative).resolve()
    resolved_root = root.resolve()
    if not candidate.is_relative_to(resolved_root):
        raise ValueError("changed path escapes the workspace")
    current = candidate if candidate.is_dir() else candidate.parent
    while current != resolved_root:
        if (current / "metadata.yaml").exists():
            return current
        current = current.parent
    return None


def _artifact_directories(root: Path) -> dict[str, Path]:
    if not root.exists():
        return {}
    return {
        path.parent.relative_to(root).as_posix(): path.parent
        for path in root.rglob("metadata.yaml")
    }


def _require_artifact_files(directory: Path, expected: set[str]) -> None:
    actual = {
        path.relative_to(directory).as_posix()
        for path in directory.rglob("*")
        if path.is_file()
    }
    unexpected = sorted(actual - expected)
    missing = sorted(expected - actual)
    if unexpected or missing:
        raise ConflictError(
            f"artifact directory has unexpected or missing files: {directory}; "
            f"unexpected={unexpected}, missing={missing}"
        )


def _safe_apply_summary(result: Mapping[str, Any]) -> dict[str, Any]:
    completed = result.get("completed")
    safe_completed = (
        [
            {
                key: item[key]
                for key in (
                    "index",
                    "operation",
                    "artifact_type",
                    "sys_id",
                    "domain",
                    "application_scope",
                    "revision",
                    "hash",
                    "tombstoned",
                )
                if key in item
            }
            for item in completed
            if isinstance(item, Mapping)
        ]
        if isinstance(completed, list)
        else []
    )
    return {
        "applied": result.get("applied") is True,
        "completed": safe_completed,
    }
