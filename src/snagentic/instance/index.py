"""Per-instance SQLite index: records, full-text search, and a dependency graph."""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Iterable, Iterator
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from snagentic.instance.records import LocalRecord, iter_record_dirs, read_record

GLIDE_TABLE = re.compile(
    r"\bnew\s+(?:global\.)?Glide(?:Record|RecordSecure|Aggregate|QueryCondition)\s*\(\s*['\"]"
    r"([a-z0-9_$]+)['\"]"
)
EVENT_QUEUE = re.compile(r"\bgs\.eventQueue(?:Scheduled)?\s*\(\s*['\"]([A-Za-z0-9_.\-]+)['\"]")
IDENTIFIER = re.compile(r"\b(?:new\s+)?(?:[a-z_][a-z0-9_]*\.)?([A-Z][A-Za-z0-9_]{2,})\b")
PROPERTY_GET = re.compile(r"\bgs\.getProperty\s*\(\s*['\"]([A-Za-z0-9_.\-]+)['\"]")
TABLE_FIELDS = {
    "sys_script": "collection",
    "sys_script_client": "table",
    "sys_ui_policy": "table",
    "sys_ui_action": "table",
    "sys_data_policy2": "model_table",
    "sys_dictionary": "name",
    "sys_choice": "name",
    "sysevent_register": "table",
}


@dataclass(frozen=True)
class Reference:
    source: str
    kind: str
    target: str


def record_name(record: LocalRecord) -> str:
    values = record.values
    if record.table == "sys_dictionary" and values.get("element"):
        return f"{values.get('name')}.{values['element']}"
    for key in ("api_name", "name", "sys_name", "internal_name", "title"):
        value = values.get(key) or record.meta.get(key)
        if value:
            return str(value)
    return str(record.sys_id)


def extract_references(
    record: LocalRecord, script_include_names: set[str]
) -> Iterator[Reference]:
    sys_id = str(record.sys_id)
    table_field = TABLE_FIELDS.get(record.table)
    if table_field and record.values.get(table_field):
        yield Reference(sys_id, "table", record.values[table_field])
    if record.table == "sys_security_acl" and record.values.get("name"):
        yield Reference(sys_id, "table", record.values["name"].split(".", 1)[0])
    if record.table == "sys_dictionary" and record.values.get("reference"):
        yield Reference(sys_id, "references_table", record.values["reference"])
    if record.table == "sys_db_object" and record.values.get("super_class"):
        yield Reference(sys_id, "extends", record.values["super_class"])
    own = record_name(record).split(".")[-1]
    files = record.meta.get("files") if isinstance(record.meta.get("files"), dict) else {}
    for field in files or {}:
        text = record.values.get(field, "")
        for table in sorted(set(GLIDE_TABLE.findall(text))):
            yield Reference(sys_id, "queries_table", table)
        for event in sorted(set(EVENT_QUEUE.findall(text))):
            yield Reference(sys_id, "fires_event", event)
        for prop in sorted(set(PROPERTY_GET.findall(text))):
            yield Reference(sys_id, "reads_property", prop)
        for name in sorted(set(IDENTIFIER.findall(text)) & script_include_names - {own}):
            yield Reference(sys_id, "calls_script_include", name)


class InstanceIndex:
    def __init__(self, path: Path) -> None:
        self.path = path

    def _connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        return connection

    def rebuild(self, metadata_root: Path, workspace_root: Path) -> dict[str, int]:
        records = [read_record(directory) for directory in iter_record_dirs(metadata_root)]
        records = [record for record in records if record.sys_id]
        include_names = {
            record_name(record).split(".")[-1]
            for record in records
            if record.table == "sys_script_include"
        }
        with closing(self._connect()) as connection, connection:
            connection.executescript(
                """
                DROP TABLE IF EXISTS records;
                DROP TABLE IF EXISTS refs;
                DROP TABLE IF EXISTS records_fts;
                CREATE TABLE records (
                    record_key TEXT PRIMARY KEY, sys_id TEXT NOT NULL, class TEXT NOT NULL,
                    scope TEXT NOT NULL, domain TEXT NOT NULL, name TEXT NOT NULL,
                    path TEXT NOT NULL,
                    updated_on TEXT, updated_by TEXT, active TEXT
                );
                CREATE INDEX records_sys_id ON records(sys_id);
                CREATE INDEX records_class ON records(class);
                CREATE INDEX records_name ON records(name);
                CREATE TABLE refs (source TEXT NOT NULL, kind TEXT NOT NULL, target TEXT NOT NULL);
                CREATE INDEX refs_target ON refs(target);
                CREATE INDEX refs_source ON refs(source);
                """
            )
            fts = True
            try:
                connection.execute(
                    "CREATE VIRTUAL TABLE records_fts USING fts5(record_key UNINDEXED, name, body)"
                )
            except sqlite3.OperationalError:
                fts = False
                connection.execute(
                    "CREATE TABLE records_fts (record_key TEXT, name TEXT, body TEXT)"
                )
            reference_count = 0
            for record in records:
                relative = record.directory.relative_to(workspace_root).as_posix()
                parts = record.directory.relative_to(metadata_root).parts
                domain = parts[1] if parts[0] == "domains" else "global"
                connection.execute(
                    "INSERT INTO records VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (
                        relative, record.sys_id, record.table,
                        str(record.meta.get("scope", "global")), domain,
                        record_name(record), relative,
                        record.meta.get("sys_updated_on"), record.meta.get("sys_updated_by"),
                        record.values.get("active"),
                    ),
                )
                connection.execute(
                    "INSERT INTO records_fts VALUES (?,?,?)",
                    (relative, record_name(record), "\n".join(record.values.values())),
                )
                references = list(extract_references(record, include_names))
                connection.executemany(
                    "INSERT INTO refs VALUES (?,?,?)",
                    [(relative, ref.kind, ref.target) for ref in references],
                )
                reference_count += len(references)
        return {"records": len(records), "references": reference_count, "fts5": int(fts)}

    def search(
        self, text: str, *, table: str | None = None, limit: int = 50
    ) -> list[dict[str, Any]]:
        with closing(self._connect()) as connection, connection:
            params: list[Any] = [f"%{text}%", f"%{text}%"]
            sql = (
                "SELECT r.* FROM records r JOIN records_fts f ON f.record_key = r.record_key "
                "WHERE (f.name LIKE ? OR f.body LIKE ?)"
            )
            if table:
                sql += " AND r.class = ?"
                params.append(table)
            sql += " ORDER BY r.class, r.name LIMIT ?"
            params.append(limit)
            return [dict(row) for row in connection.execute(sql, params)]

    def references(self, target: str) -> list[dict[str, Any]]:
        """Records that reference ``target`` (a table, script include, event, or property)."""

        with closing(self._connect()) as connection, connection:
            rows = connection.execute(
                "SELECT refs.kind, r.* FROM refs JOIN records r ON r.record_key = refs.source "
                "WHERE refs.target = ? ORDER BY r.class, r.name",
                (target,),
            )
            return [dict(row) for row in rows]

    def dependencies(self, sys_id: str) -> list[dict[str, Any]]:
        with closing(self._connect()) as connection, connection:
            rows = connection.execute(
                "SELECT kind, target FROM refs WHERE source IN "
                "(SELECT record_key FROM records WHERE sys_id = ?) ORDER BY kind, target",
                (sys_id,),
            )
            return [dict(row) for row in rows]

    def records(self, *, table: str | None = None) -> list[dict[str, Any]]:
        with closing(self._connect()) as connection, connection:
            if table:
                rows = connection.execute(
                    "SELECT * FROM records WHERE class = ? ORDER BY name", (table,)
                )
            else:
                rows = connection.execute("SELECT * FROM records ORDER BY class, name")
            return [dict(row) for row in rows]

    def all_references(self) -> Iterable[dict[str, Any]]:
        with closing(self._connect()) as connection, connection:
            return [dict(row) for row in connection.execute("SELECT * FROM refs")]
