"""Artifact definitions and deterministic normalization."""

from snagentic.artifacts.normalization import (
    canonical_json,
    content_hash,
    normalize_line_endings,
    normalize_mapping,
    normalize_nulls,
    normalize_value,
)
from snagentic.artifacts.registry import (
    DEFAULT_ARTIFACT_REGISTRY,
    ArtifactDefinition,
    ArtifactRegistry,
)
from snagentic.models import ArtifactCapability

__all__ = [
    "DEFAULT_ARTIFACT_REGISTRY",
    "ArtifactCapability",
    "ArtifactDefinition",
    "ArtifactRegistry",
    "canonical_json",
    "content_hash",
    "normalize_line_endings",
    "normalize_mapping",
    "normalize_nulls",
    "normalize_value",
]
