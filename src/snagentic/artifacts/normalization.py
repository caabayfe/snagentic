"""Deterministic normalization for hashing and filesystem persistence."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from datetime import UTC, date, datetime
from enum import Enum
from pathlib import Path
from typing import Any

from pydantic import BaseModel


def normalize_line_endings(value: str, *, trailing_newline: bool = True) -> str:
    normalized = value.replace("\r\n", "\n").replace("\r", "\n")
    if not trailing_newline:
        return normalized
    return normalized.rstrip("\n") + "\n"


def normalize_nulls(value: Any, *, omit_mapping_nulls: bool = False) -> Any:
    """Normalize nested nulls while preserving list positions and meaningful empty strings."""

    if isinstance(value, Mapping):
        return {
            str(key): normalize_nulls(item, omit_mapping_nulls=omit_mapping_nulls)
            for key, item in value.items()
            if not (omit_mapping_nulls and item is None)
        }
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [normalize_nulls(item, omit_mapping_nulls=omit_mapping_nulls) for item in value]
    return None if value is None else value


def normalize_mapping(
    value: Mapping[str, Any], *, omit_nulls: bool = False
) -> dict[str, Any]:
    normalized = normalize_nulls(value, omit_mapping_nulls=omit_nulls)
    if not isinstance(normalized, dict):
        raise TypeError("mapping normalization produced a non-mapping value")
    return {
        key: normalize_value(item, omit_nulls=omit_nulls)
        for key, item in sorted(normalized.items())
    }


def normalize_value(value: Any, *, omit_nulls: bool = False) -> Any:
    if isinstance(value, BaseModel):
        value = value.model_dump(mode="json")
    if isinstance(value, Mapping):
        return normalize_mapping(value, omit_nulls=omit_nulls)
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [normalize_value(item, omit_nulls=omit_nulls) for item in value]
    if isinstance(value, str):
        return value.replace("\r\n", "\n").replace("\r", "\n")
    if isinstance(value, datetime):
        if value.tzinfo is None:
            raise ValueError("datetime values must include a timezone")
        return value.astimezone(UTC).isoformat().replace("+00:00", "Z")
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Path):
        return value.as_posix()
    if isinstance(value, Enum):
        return normalize_value(value.value, omit_nulls=omit_nulls)
    if value is None or isinstance(value, (bool, int, float)):
        return value
    raise TypeError(f"unsupported value for deterministic normalization: {type(value).__name__}")


def canonical_json(value: Any, *, omit_nulls: bool = False) -> str:
    return json.dumps(
        normalize_value(value, omit_nulls=omit_nulls),
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
        allow_nan=False,
    )


def content_hash(value: Any, *, omit_nulls: bool = False) -> str:
    encoded = canonical_json(value, omit_nulls=omit_nulls).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()
