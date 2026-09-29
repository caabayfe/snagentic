"""Core domain models shared by artifact synchronization components."""

from __future__ import annotations

import json
from datetime import datetime
from enum import StrEnum
from typing import Any, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class StrictModel(BaseModel):
    """Base model for persisted and exchanged snagentic data."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class ArtifactCapability(StrEnum):
    NATIVE_SOURCE_CONTROL = "native_source_control"
    MANAGED_BIDIRECTIONAL = "managed_bidirectional"
    EXPORT_ONLY = "export_only"
    DIAGNOSTIC_ONLY = "diagnostic_only"
    EXCLUDED = "excluded"


class SyncStatus(StrEnum):
    UNTRACKED = "untracked"
    UNCHANGED = "unchanged"
    ADDED = "added"
    MODIFIED = "modified"
    DELETED = "deleted"
    CONFLICTED = "conflicted"


class ChangeKind(StrEnum):
    CREATE = "create"
    UPDATE = "update"
    DELETE = "delete"


class DiagnosticLevel(StrEnum):
    DEBUG = "debug"
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"


class ArtifactIdentity(StrictModel):
    """Logical identity is portable; ``sys_id`` remains instance-specific metadata."""

    artifact_type: str = Field(min_length=1)
    scope: str = Field(min_length=1)
    domain_stable_id: str = Field(default="global", min_length=1)
    natural_key: str = Field(min_length=1)
    sys_id: str | None = None

    @field_validator("artifact_type", "scope", "domain_stable_id", "natural_key")
    @classmethod
    def reject_blank_identity_parts(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("artifact identity components cannot be blank")
        return value

    @property
    def logical_key(self) -> str:
        """Return an unambiguous, stable representation of the portable identity."""

        return json.dumps(
            [
                self.artifact_type,
                self.scope,
                self.domain_stable_id,
                self.natural_key,
            ],
            ensure_ascii=True,
            separators=(",", ":"),
        )


class Provenance(StrictModel):
    source_instance: str = Field(min_length=1)
    source_table: str = Field(min_length=1)
    source_sys_id: str | None = None
    captured_at: datetime
    source_updated_at: datetime | None = None
    correlation_id: str | None = None

    @field_validator("captured_at", "source_updated_at")
    @classmethod
    def require_aware_datetime(cls, value: datetime | None) -> datetime | None:
        if value is not None and value.tzinfo is None:
            raise ValueError("timestamps must include a timezone")
        return value


class ArtifactRevision(StrictModel):
    identity: ArtifactIdentity
    content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    revision: str | None = None
    observed_at: datetime

    @field_validator("observed_at")
    @classmethod
    def require_aware_observed_at(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("observed_at must include a timezone")
        return value


class NormalizedArtifact(StrictModel):
    identity: ArtifactIdentity
    metadata: dict[str, Any] = Field(default_factory=dict)
    content: Any
    revision: ArtifactRevision
    provenance: Provenance

    @model_validator(mode="after")
    def revision_matches_identity(self) -> Self:
        if self.revision.identity.logical_key != self.identity.logical_key:
            raise ValueError("revision identity must match artifact identity")
        return self


class Domain(StrictModel):
    stable_id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    parent_stable_id: str | None = None
    sys_id: str | None = None
    active: bool = True
    metadata: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def reject_self_parent(self) -> Self:
        if self.parent_stable_id == self.stable_id:
            raise ValueError("a domain cannot be its own parent")
        return self


class Tombstone(StrictModel):
    identity: ArtifactIdentity
    deleted_at: datetime
    provenance: Provenance
    reason: str | None = None

    @field_validator("deleted_at")
    @classmethod
    def require_aware_deleted_at(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("deleted_at must include a timezone")
        return value


class Conflict(StrictModel):
    identity: ArtifactIdentity
    reason: str = Field(min_length=1)
    base_revision: ArtifactRevision | None = None
    local_revision: ArtifactRevision | None = None
    remote_revision: ArtifactRevision | None = None
    fields: tuple[str, ...] = ()

    @model_validator(mode="after")
    def revisions_match_identity(self) -> Self:
        for revision in (
            self.base_revision,
            self.local_revision,
            self.remote_revision,
        ):
            if (
                revision is not None
                and revision.identity.logical_key != self.identity.logical_key
            ):
                raise ValueError("conflict revision identity must match conflict identity")
        return self


class ChangeOperation(StrictModel):
    operation: ChangeKind
    identity: ArtifactIdentity
    artifact: NormalizedArtifact | None = None
    expected_revision: ArtifactRevision | None = None

    @model_validator(mode="after")
    def validate_payload(self) -> Self:
        if self.operation in {ChangeKind.CREATE, ChangeKind.UPDATE} and self.artifact is None:
            raise ValueError("create and update operations require an artifact")
        if self.operation == ChangeKind.DELETE and self.artifact is not None:
            raise ValueError("delete operations cannot include an artifact")
        if (
            self.artifact is not None
            and self.artifact.identity.logical_key != self.identity.logical_key
        ):
            raise ValueError("operation artifact identity must match operation identity")
        if (
            self.expected_revision is not None
            and self.expected_revision.identity.logical_key != self.identity.logical_key
        ):
            raise ValueError("expected revision identity must match operation identity")
        return self


class ChangePlan(StrictModel):
    version: int = Field(default=1, ge=1)
    source_environment: str = Field(min_length=1)
    target_environment: str = Field(min_length=1)
    operations: tuple[ChangeOperation, ...] = ()
    conflicts: tuple[Conflict, ...] = ()
    created_at: datetime

    @field_validator("created_at")
    @classmethod
    def require_aware_created_at(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("created_at must include a timezone")
        return value


class DiagnosticRecord(StrictModel):
    timestamp: datetime
    level: DiagnosticLevel
    source: str = Field(min_length=1)
    message: str = Field(min_length=1)
    details: dict[str, Any] = Field(default_factory=dict)
    artifact: ArtifactIdentity | None = None
    correlation_id: str | None = None

    @field_validator("timestamp")
    @classmethod
    def require_aware_timestamp(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("timestamp must include a timezone")
        return value
