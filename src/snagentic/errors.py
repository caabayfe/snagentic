"""Domain-specific errors surfaced by the CLI and extension."""

from __future__ import annotations

from typing import Any


class SnagenticError(Exception):
    """Base error for expected snagentic failures."""


class ConfigurationError(SnagenticError):
    """Configuration is missing or invalid."""


class ServiceNowError(SnagenticError):
    """A ServiceNow API request failed."""

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        code: str | None = None,
        details: list[dict[str, Any]] | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.details = details or []


class PolicyDeniedError(SnagenticError):
    """A requested operation is denied by policy."""


class ConflictError(SnagenticError):
    """Local and remote changes cannot be safely reconciled."""
