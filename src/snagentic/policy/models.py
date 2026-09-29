"""Policy configuration models."""

from __future__ import annotations

from enum import StrEnum

from pydantic import Field, model_validator

from snagentic.models import StrictModel


class EnvironmentKind(StrEnum):
    DEVELOPMENT = "development"
    TEST = "test"
    PRODUCTION = "production"


DEFAULT_SENSITIVE_FIELDS = frozenset(
    {
        "api_key",
        "client_secret",
        "comments",
        "credential",
        "journal",
        "key",
        "password",
        "password2",
        "private_key",
        "secret",
        "token",
        "value",
        "work_notes",
    }
)


class WritePolicy(StrictModel):
    writable_environments: frozenset[EnvironmentKind] = frozenset(
        {EnvironmentKind.DEVELOPMENT}
    )
    excluded_sensitive_fields: frozenset[str] = DEFAULT_SENSITIVE_FIELDS


class DiagnosticLimits(StrictModel):
    min_minutes: int = Field(default=1, ge=1)
    max_minutes: int = Field(default=1440, ge=1)
    max_records: int = Field(default=5000, ge=1)
    max_payload_bytes: int = Field(default=10 * 1024 * 1024, ge=1)

    @model_validator(mode="after")
    def validate_range(self) -> DiagnosticLimits:
        if self.min_minutes > self.max_minutes:
            raise ValueError("min_minutes cannot exceed max_minutes")
        return self
