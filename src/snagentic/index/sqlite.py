"""SQLite metadata and code-text index with optional FTS5 acceleration."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable
from contextlib import closing
from pathlib import Path
from typing import Any

from pydantic import Field

from snagentic.artifacts.normalization import canonical_json
from snagentic.models import NormalizedArtifact, StrictModel


class IndexRecord(StrictModel):
    logical_key: str
    artifact_type: str
    scope: str
    domain_stable_id: str
    natural_key: str
    sys_id: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    content_hash: str


class ArtifactIndex:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.fts5_enabled = False

    def rebuild(self, artifacts: Iterable[NormalizedArtifact]) -> int:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        rows = [self._row(artifact) for artifact in artifacts]
        with closing(self._connect()) as connection, connection:
            connection.execute("DROP TABLE IF EXISTS artifact_fts")
            connection.execute("DROP TABLE IF EXISTS artifacts")
            connection.execute(
                """
                CREATE TABLE artifacts (
                    logical_key TEXT PRIMARY KEY,
                    artifact_type TEXT NOT NULL,
                    scope TEXT NOT NULL,
                    domain_stable_id TEXT NOT NULL,
                    natural_key TEXT NOT NULL,
                    sys_id TEXT,
                    metadata_json TEXT NOT NULL,
                    content_hash TEXT NOT NULL,
                    searchable_text TEXT NOT NULL
                )
                """
            )
            connection.execute(
                "CREATE INDEX artifacts_domain_idx ON artifacts(domain_stable_id)"
            )
            connection.execute(
                "CREATE INDEX artifacts_type_idx ON artifacts(artifact_type)"
            )
            connection.executemany(
                """
                INSERT INTO artifacts (
                    logical_key, artifact_type, scope, domain_stable_id, natural_key,
                    sys_id, metadata_json, content_hash, searchable_text
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                rows,
            )
            self.fts5_enabled = self._create_fts(connection)
        return len(rows)

    def query(
        self,
        *,
        text: str | None = None,
        domain: str | None = None,
        artifact_type: str | None = None,
        limit: int = 100,
    ) -> list[IndexRecord]:
        if not 1 <= limit <= 1000:
            raise ValueError("limit must be between 1 and 1000")
        clauses: list[str] = []
        parameters: list[Any] = []
        join = ""
        normalized_text = text.strip() if text is not None else ""

        with closing(self._connect()) as connection, connection:
            self.fts5_enabled = self._has_fts(connection)
            if normalized_text:
                if self.fts5_enabled:
                    join = " JOIN artifact_fts ON artifact_fts.logical_key = a.logical_key"
                    clauses.append("artifact_fts MATCH ?")
                    parameters.append(_fts_query(normalized_text))
                else:
                    clauses.append("a.searchable_text LIKE ? ESCAPE '\\'")
                    parameters.append(f"%{_escape_like(normalized_text)}%")
            if domain is not None:
                clauses.append("a.domain_stable_id = ?")
                parameters.append(domain)
            if artifact_type is not None:
                clauses.append("a.artifact_type = ?")
                parameters.append(artifact_type)
            where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
            parameters.append(limit)
            try:
                query = f"""
                    SELECT a.logical_key, a.artifact_type, a.scope,
                           a.domain_stable_id, a.natural_key, a.sys_id,
                           a.metadata_json, a.content_hash
                    FROM artifacts AS a
                    {join}
                    {where}
                    ORDER BY a.domain_stable_id, a.artifact_type, a.natural_key
                    LIMIT ?
                    """  # noqa: S608 - interpolated clauses are internal constants
                rows = connection.execute(query, parameters).fetchall()
            except sqlite3.OperationalError as exc:
                if "no such table" in str(exc):
                    return []
                raise
        return [
            IndexRecord(
                logical_key=row["logical_key"],
                artifact_type=row["artifact_type"],
                scope=row["scope"],
                domain_stable_id=row["domain_stable_id"],
                natural_key=row["natural_key"],
                sys_id=row["sys_id"],
                metadata=json.loads(row["metadata_json"]),
                content_hash=row["content_hash"],
            )
            for row in rows
        ]

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        return connection

    @staticmethod
    def _create_fts(connection: sqlite3.Connection) -> bool:
        try:
            connection.execute(
                "CREATE VIRTUAL TABLE artifact_fts USING fts5(logical_key UNINDEXED, text)"
            )
        except sqlite3.OperationalError:
            return False
        connection.execute(
            """
            INSERT INTO artifact_fts(logical_key, text)
            SELECT logical_key, searchable_text FROM artifacts
            """
        )
        return True

    @staticmethod
    def _has_fts(connection: sqlite3.Connection) -> bool:
        row = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'artifact_fts'"
        ).fetchone()
        return row is not None

    @staticmethod
    def _row(artifact: NormalizedArtifact) -> tuple[str | None, ...]:
        identity = artifact.identity
        metadata_json = canonical_json(artifact.metadata)
        content_text = (
            artifact.content
            if isinstance(artifact.content, str)
            else canonical_json(artifact.content)
        )
        searchable_text = "\n".join(
            (
                identity.artifact_type,
                identity.scope,
                identity.domain_stable_id,
                identity.natural_key,
                metadata_json,
                content_text,
            )
        )
        return (
            identity.logical_key,
            identity.artifact_type,
            identity.scope,
            identity.domain_stable_id,
            identity.natural_key,
            identity.sys_id,
            metadata_json,
            artifact.revision.content_hash,
            searchable_text,
        )


def _fts_query(value: str) -> str:
    tokens = value.split()
    return " AND ".join(f'"{token.replace(chr(34), chr(34) * 2)}"' for token in tokens)


def _escape_like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
