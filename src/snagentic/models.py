"""Core domain models shared by kept snagentic components."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class StrictModel(BaseModel):
    """Base model for persisted and exchanged snagentic data."""

    model_config = ConfigDict(extra="forbid", frozen=True)
