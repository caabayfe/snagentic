"""Deterministic, offline promotion scaffolding from validated change plans."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_SOURCE_BUNDLE_PATTERN = re.compile(r"^[0-9a-f]{32}$")
_GIT_COMMIT_PATTERN = re.compile(r"^[0-9a-fA-F]{7,64}$")
_UTC_TIMESTAMP_PATTERN = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|\+00:00)$"
)
_OPERATIONS = frozenset({"create", "update", "delete"})
_MECHANISMS = frozenset({"application", "update_set", "servicenow_cicd"})
_TARGET_KINDS = frozenset({"test", "production"})
_CONTENT_KEYS = frozenset({"body", "code", "payload", "value", "values"})
_CONTENT_PARTS = ("comment", "content", "journal", "script", "source", "worknote")
_SECRET_PARTS = (
    "apikey",
    "authorization",
    "clientsecret",
    "credential",
    "password",
    "passwd",
    "privatekey",
    "secret",
    "token",
)
_EXECUTION_DIRECTIVE_KEYS = frozenset(
    {
        "apply",
        "applychanges",
        "applydirectly",
        "applyenabled",
        "applynow",
        "approve",
        "approvechanges",
        "approved",
        "allowdirectwrite",
        "deploy",
        "deploychanges",
        "deploydirectly",
        "deployenabled",
        "deploymentapproved",
        "deploynow",
        "direct",
        "directwrite",
        "directwriteenabled",
        "execute",
        "executechanges",
        "executeenabled",
        "executenow",
        "execution",
        "write",
        "writechanges",
        "writeenabled",
        "writenow",
        "writedirectly",
    }
)
_MECHANISM_KEYS = frozenset(
    {
        "deploymentmechanism",
        "deploymentmethod",
        "deploymentmode",
        "deploymechanism",
        "deploymethod",
        "deploymode",
        "mechanism",
        "promotionmechanism",
        "promotionmethod",
        "promotionmode",
        "writemechanism",
        "writemethod",
        "writemode",
    }
)
_DIRECT_WRITE_VALUES = frozenset(
    {
        "api",
        "apiwrite",
        "apply",
        "deploy",
        "direct",
        "directapi",
        "directdeploy",
        "directdeployment",
        "directservicenow",
        "directwrite",
        "push",
        "restapi",
        "servicenowdirect",
    }
)


def canonical_json(value: Any) -> str:
    """Return the canonical JSON representation used for every promotion digest."""

    try:
        return json.dumps(
            value,
            ensure_ascii=True,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        )
    except (TypeError, ValueError) as exc:
        raise ValueError("change plan must contain only canonical JSON values") from exc


def _digest(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _normalized_key(value: object) -> str:
    return "".join(character for character in str(value).casefold() if character.isalnum())


def _is_excluded_key(key: object) -> bool:
    normalized = _normalized_key(key)
    return (
        normalized in _CONTENT_KEYS
        or any(part in normalized for part in _CONTENT_PARTS)
        or any(part in normalized for part in _SECRET_PARTS)
    )


def sanitize_change_plan(value: Any) -> Any:
    """Recursively copy JSON data while removing content and secret-like fields."""

    if isinstance(value, Mapping):
        return {
            str(key): sanitize_change_plan(item)
            for key, item in value.items()
            if not _is_excluded_key(key)
        }
    if isinstance(value, (list, tuple)):
        return [sanitize_change_plan(item) for item in value]
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    raise ValueError("change plan must contain only JSON-compatible values")


@dataclass(frozen=True, slots=True)
class PromotionOperation:
    """Content-free immutable operation metadata."""

    operation: str
    artifact_type: str
    sys_id: str | None
    domain_sys_id: str
    application_scope_sys_id: str
    expected_hash: str | None
    artifact_hash: str | None
    operation_hash: str

    def as_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "operation": self.operation,
            "artifact_type": self.artifact_type,
            "domain": {"sys_id": self.domain_sys_id},
            "application_scope": {"sys_id": self.application_scope_sys_id},
            "operation_hash": self.operation_hash,
        }
        if self.sys_id is not None:
            result["sys_id"] = self.sys_id
        if self.expected_hash is not None:
            result["expected_hash"] = self.expected_hash
        if self.artifact_hash is not None:
            result["artifact_hash"] = self.artifact_hash
        return result


@dataclass(frozen=True, slots=True)
class ValidatedChangePlan:
    """Validated source provenance and safely reduced immutable operations."""

    source_bundle_id: str
    source_changes_sha256: str
    operations: tuple[PromotionOperation, ...]


@dataclass(frozen=True, slots=True)
class PromotionManifest:
    """Immutable promotion manifest."""

    promotion_bundle_id: str
    source_bundle_id: str
    source_changes_sha256: str
    created_at: str
    source_kind: str
    target_kind: str
    deployment_mechanism: str
    operations: tuple[PromotionOperation, ...]
    git_commit: str | None = None

    def as_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "schema_version": 1,
            "kind": "snagentic_promotion_manifest",
            "promotion_bundle_id": self.promotion_bundle_id,
            "source_plan": {
                "bundle_id": self.source_bundle_id,
                "changes_sha256": self.source_changes_sha256,
            },
            "created_at": self.created_at,
            "source_kind": self.source_kind,
            "target_kind": self.target_kind,
            "deployment_mechanism": self.deployment_mechanism,
            "operations": [operation.as_dict() for operation in self.operations],
        }
        if self.git_commit is not None:
            result["git_commit"] = self.git_commit
        return result


@dataclass(frozen=True, slots=True)
class PromotionReport:
    """Immutable PR-oriented summary of a promotion manifest."""

    promotion_bundle_id: str
    source_bundle_id: str
    source_changes_sha256: str
    created_at: str
    source_kind: str
    target_kind: str
    deployment_mechanism: str
    operations: tuple[PromotionOperation, ...]
    git_commit: str | None = None

    def as_dict(self) -> dict[str, Any]:
        counts = {
            operation: sum(item.operation == operation for item in self.operations)
            for operation in ("create", "update", "delete")
        }
        result: dict[str, Any] = {
            "schema_version": 1,
            "kind": "snagentic_change_report",
            "promotion_bundle_id": self.promotion_bundle_id,
            "source_plan": {
                "bundle_id": self.source_bundle_id,
                "changes_sha256": self.source_changes_sha256,
            },
            "created_at": self.created_at,
            "source_kind": self.source_kind,
            "target_kind": self.target_kind,
            "deployment_mechanism": self.deployment_mechanism,
            "summary": {"total": len(self.operations), **counts},
            "operations": [operation.as_dict() for operation in self.operations],
        }
        if self.git_commit is not None:
            result["git_commit"] = self.git_commit
        return result


def load_change_plan(path: Path) -> ValidatedChangePlan:
    """Load and validate a change plan without loading project configuration."""

    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ValueError(f"cannot read change plan: {path}") from exc
    except json.JSONDecodeError as exc:
        raise ValueError(f"change plan is not valid JSON: {path}") from exc
    return validate_change_plan(value)


def validate_change_plan(value: Any) -> ValidatedChangePlan:
    """Validate source provenance, hashes, identities, and content-derived hashes."""

    if not isinstance(value, Mapping):
        raise ValueError("change plan must be a JSON object")
    _reject_direct_write(value)
    bundle_id = _required_text(value, "bundle_id")
    if _SOURCE_BUNDLE_PATTERN.fullmatch(bundle_id) is None:
        raise ValueError("change plan bundle_id must be a 32-character lowercase hex digest")
    changes = value.get("changes")
    if not isinstance(changes, list):
        raise ValueError("change plan changes must be a list")
    expected_bundle_id = _digest(changes)[:32]
    if bundle_id != expected_bundle_id:
        raise ValueError(
            "change plan bundle_id does not match the canonical source changes"
        )
    operations = tuple(sorted((_validate_change(item) for item in changes), key=_sort_key))
    return ValidatedChangePlan(
        source_bundle_id=bundle_id,
        source_changes_sha256=_digest(changes),
        operations=operations,
    )


def build_promotion_manifest(
    plan: ValidatedChangePlan | Mapping[str, Any],
    *,
    source_kind: str,
    target_kind: str,
    mechanism: str,
    created_at: str,
    git_commit: str | None = None,
) -> PromotionManifest:
    """Build an immutable manifest whose ID is derived from its canonical material."""

    validated = plan if isinstance(plan, ValidatedChangePlan) else validate_change_plan(plan)
    normalized = _normalize_metadata(
        source_kind=source_kind,
        target_kind=target_kind,
        mechanism=mechanism,
        created_at=created_at,
        git_commit=git_commit,
    )
    material = _manifest_material(
        validated,
        source_kind=normalized["source_kind"],
        target_kind=normalized["target_kind"],
        deployment_mechanism=normalized["deployment_mechanism"],
        created_at=normalized["created_at"],
        git_commit=normalized.get("git_commit"),
    )
    return PromotionManifest(
        promotion_bundle_id=_digest(material),
        source_bundle_id=validated.source_bundle_id,
        source_changes_sha256=validated.source_changes_sha256,
        operations=validated.operations,
        source_kind=normalized["source_kind"],
        target_kind=normalized["target_kind"],
        deployment_mechanism=normalized["deployment_mechanism"],
        created_at=normalized["created_at"],
        git_commit=normalized.get("git_commit"),
    )


def build_change_report(
    plan: ValidatedChangePlan | Mapping[str, Any],
    *,
    source_kind: str,
    target_kind: str,
    mechanism: str,
    created_at: str,
    git_commit: str | None = None,
) -> PromotionReport:
    """Build an immutable, content-free report tied to the promotion bundle."""

    manifest = build_promotion_manifest(
        plan,
        source_kind=source_kind,
        target_kind=target_kind,
        mechanism=mechanism,
        created_at=created_at,
        git_commit=git_commit,
    )
    return PromotionReport(
        promotion_bundle_id=manifest.promotion_bundle_id,
        source_bundle_id=manifest.source_bundle_id,
        source_changes_sha256=manifest.source_changes_sha256,
        created_at=manifest.created_at,
        git_commit=manifest.git_commit,
        source_kind=manifest.source_kind,
        target_kind=manifest.target_kind,
        deployment_mechanism=manifest.deployment_mechanism,
        operations=manifest.operations,
    )


def _validate_change(value: Any) -> PromotionOperation:
    if not isinstance(value, Mapping):
        raise ValueError("each change plan operation must be an object")
    operation = _required_text(value, "operation")
    if operation not in _OPERATIONS:
        raise ValueError(f"unsupported change operation: {operation}")
    artifact_type = _required_text(value, "artifact_type")
    domain_sys_id = _nested_identifier(value, "domain")
    scope_sys_id = _nested_identifier(value, "application_scope")
    sys_id = None
    expected_hash = None
    artifact_hash = None
    if operation in {"update", "delete"}:
        sys_id = _required_text(value, "sys_id")
        expected = value.get("expected")
        if not isinstance(expected, Mapping):
            raise ValueError(f"{operation} operation expected must be an object")
        expected_hash = _required_sha256(expected, "hash")
    if operation in {"create", "update"}:
        values = value.get("values")
        if not isinstance(values, Mapping) or not values:
            raise ValueError(f"{operation} operation values must be a non-empty object")
        artifact_hash = _digest(values)
        declared_hash = value.get("artifact_hash")
        if declared_hash is not None:
            if not isinstance(declared_hash, str) or _SHA256_PATTERN.fullmatch(
                declared_hash
            ) is None:
                raise ValueError("artifact_hash must be a lowercase SHA-256 digest")
            if declared_hash != artifact_hash:
                raise ValueError("artifact_hash does not match canonical operation values")
    material: dict[str, Any] = {
        "operation": operation,
        "artifact_type": artifact_type,
        "domain": {"sys_id": domain_sys_id},
        "application_scope": {"sys_id": scope_sys_id},
    }
    if sys_id is not None:
        material["sys_id"] = sys_id
    if expected_hash is not None:
        material["expected_hash"] = expected_hash
    if artifact_hash is not None:
        material["artifact_hash"] = artifact_hash
    return PromotionOperation(
        operation=operation,
        artifact_type=artifact_type,
        sys_id=sys_id,
        domain_sys_id=domain_sys_id,
        application_scope_sys_id=scope_sys_id,
        expected_hash=expected_hash,
        artifact_hash=artifact_hash,
        operation_hash=_digest(material),
    )


def _manifest_material(
    plan: ValidatedChangePlan,
    *,
    source_kind: str,
    target_kind: str,
    deployment_mechanism: str,
    created_at: str,
    git_commit: str | None,
) -> dict[str, Any]:
    material: dict[str, Any] = {
        "schema_version": 1,
        "source_plan": {
            "bundle_id": plan.source_bundle_id,
            "changes_sha256": plan.source_changes_sha256,
        },
        "created_at": created_at,
        "source_kind": source_kind,
        "target_kind": target_kind,
        "deployment_mechanism": deployment_mechanism,
        "operations": [operation.as_dict() for operation in plan.operations],
    }
    if git_commit is not None:
        material["git_commit"] = git_commit
    return material


def _normalize_metadata(
    *,
    source_kind: str,
    target_kind: str,
    mechanism: str,
    created_at: str,
    git_commit: str | None,
) -> dict[str, str]:
    if source_kind != "development":
        raise ValueError("promotion source kind must be exactly 'development'")
    if target_kind not in _TARGET_KINDS:
        raise ValueError("promotion target kind must be exactly 'test' or 'production'")
    if target_kind == source_kind:
        raise ValueError("promotion source and target kinds must differ")
    if mechanism not in _MECHANISMS:
        raise ValueError(
            "deployment mechanism must be application, update_set, or servicenow_cicd"
        )
    result = {
        "source_kind": source_kind,
        "target_kind": target_kind,
        "deployment_mechanism": mechanism,
        "created_at": _normalize_created_at(created_at),
    }
    normalized_git_commit = _normalize_git_commit(git_commit)
    if normalized_git_commit is not None:
        result["git_commit"] = normalized_git_commit
    return result


def _normalize_created_at(value: str) -> str:
    if not isinstance(value, str) or _UTC_TIMESTAMP_PATTERN.fullmatch(value) is None:
        raise ValueError("created_at must be an RFC3339 UTC timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("created_at must be an RFC3339 UTC timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() != UTC.utcoffset(parsed):
        raise ValueError("created_at must use the UTC timezone")
    return parsed.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _normalize_git_commit(value: str | None) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or _GIT_COMMIT_PATTERN.fullmatch(value) is None:
        raise ValueError("git_commit must be a 7 to 64 character hexadecimal object ID")
    return value.lower()


def _required_text(value: Mapping[str, Any], key: str) -> str:
    candidate = value.get(key)
    if not isinstance(candidate, str) or not candidate.strip():
        raise ValueError(f"change plan field {key!r} must be a non-empty string")
    if candidate != candidate.strip():
        raise ValueError(f"change plan field {key!r} must not have surrounding whitespace")
    return candidate


def _required_sha256(value: Mapping[str, Any], key: str) -> str:
    candidate = _required_text(value, key)
    if _SHA256_PATTERN.fullmatch(candidate) is None:
        raise ValueError(f"change plan field {key!r} must be a lowercase SHA-256 digest")
    return candidate


def _nested_identifier(value: Mapping[str, Any], key: str) -> str:
    nested = value.get(key)
    if not isinstance(nested, Mapping):
        raise ValueError(f"change plan field {key!r} must be an object")
    return _required_text(nested, "sys_id")


def _sort_key(operation: PromotionOperation) -> str:
    return canonical_json(operation.as_dict())


def _reject_direct_write(value: Any) -> None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            normalized = _normalized_key(key)
            if normalized in _EXECUTION_DIRECTIVE_KEYS:
                raise ValueError("direct-write/apply/deploy directives are not supported")
            if normalized in _MECHANISM_KEYS and (
                isinstance(item, str)
                and _normalized_key(item) in _DIRECT_WRITE_VALUES
            ):
                raise ValueError("direct-write deployment mechanisms are not supported")
            _reject_direct_write(item)
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        for item in value:
            _reject_direct_write(item)
