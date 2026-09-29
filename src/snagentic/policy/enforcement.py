"""Central policy checks for writes, redaction, and diagnostics."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from snagentic.errors import PolicyDeniedError
from snagentic.policy.models import DiagnosticLimits, EnvironmentKind, WritePolicy


class PolicyEnforcer:
    def __init__(
        self,
        write_policy: WritePolicy | None = None,
        diagnostic_limits: DiagnosticLimits | None = None,
    ) -> None:
        self.write_policy = write_policy or WritePolicy()
        self.diagnostic_limits = diagnostic_limits or DiagnosticLimits()

    def require_write_allowed(self, environment: EnvironmentKind | str) -> None:
        try:
            kind = EnvironmentKind(environment)
        except ValueError as exc:
            raise PolicyDeniedError(f"unknown environment kind: {environment}") from exc
        if kind not in self.write_policy.writable_environments:
            raise PolicyDeniedError(f"writes are denied for {kind.value} environments")

    def exclude_sensitive_fields(self, value: Mapping[str, Any]) -> dict[str, Any]:
        excluded = {field.casefold() for field in self.write_policy.excluded_sensitive_fields}
        return {
            key: self._redact_nested(item)
            for key, item in value.items()
            if key.casefold() not in excluded
        }

    def validate_diagnostic_request(
        self, *, minutes: int, limit: int, payload_bytes: int | None = None
    ) -> None:
        limits = self.diagnostic_limits
        if not limits.min_minutes <= minutes <= limits.max_minutes:
            raise PolicyDeniedError(
                f"diagnostic minutes must be between {limits.min_minutes} "
                f"and {limits.max_minutes}"
            )
        if not 1 <= limit <= limits.max_records:
            raise PolicyDeniedError(
                f"diagnostic limit must be between 1 and {limits.max_records}"
            )
        if payload_bytes is not None and (
            payload_bytes < 0 or payload_bytes > limits.max_payload_bytes
        ):
            raise PolicyDeniedError(
                f"diagnostic payload must not exceed {limits.max_payload_bytes} bytes"
            )

    def _redact_nested(self, value: Any) -> Any:
        if isinstance(value, Mapping):
            return self.exclude_sensitive_fields(value)
        if isinstance(value, list):
            return [self._redact_nested(item) for item in value]
        if isinstance(value, tuple):
            return tuple(self._redact_nested(item) for item in value)
        return value
