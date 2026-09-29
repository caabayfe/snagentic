"""Resolve authored documentation references against a mirrored instance."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from snagentic.instance.docsource import (
    AuthoredDocument,
    EvidenceReference,
    ProcessDocument,
)
from snagentic.instance.index import record_name
from snagentic.instance.records import META_FILE, LocalRecord, read_record

EVIDENCE_TABLES = {
    "flow": "sys_hub_flow",
    "catalog_item": "sc_cat_item",
    "role": "sys_user_role",
    "event": "sysevent_register",
    "property": "sys_properties",
    "script_include": "sys_script_include",
}
_LOADER: Any = getattr(yaml, "CSafeLoader", yaml.SafeLoader)


@dataclass(frozen=True)
class ResolvedEvidence:
    kind: str
    target: str
    label: str
    path: str | None
    fingerprint: str | None
    status: str
    message: str | None = None

    def as_dict(self) -> dict[str, str | None]:
        return {
            "kind": self.kind,
            "target": self.target,
            "label": self.label,
            "path": self.path,
            "fingerprint": self.fingerprint,
            "status": self.status,
            "message": self.message,
        }


class DocumentationEvidence:
    def __init__(self, workspace: Path, root: Path, records: list[LocalRecord]) -> None:
        self.workspace = workspace
        self.root = root
        self.records = records
        self.by_table: dict[str, list[LocalRecord]] = {}
        for record in records:
            self.by_table.setdefault(record.table, []).append(record)

    def resolve_document(self, document: AuthoredDocument) -> list[ResolvedEvidence]:
        references = list(document.evidence)
        if isinstance(document, ProcessDocument):
            references += [
                reference for step in document.steps for reference in step.evidence
            ]
        return [self.resolve(reference) for reference in references]

    def customized_tables(self) -> list[str]:
        model_root = self.workspace / "model" / "tables"
        tables: list[str] = []
        if not model_root.is_dir():
            return tables
        for path in sorted(model_root.glob("*.yaml")):
            data = yaml.load(  # noqa: S506 - _LOADER is CSafeLoader or SafeLoader
                path.read_text(encoding="utf-8"), Loader=_LOADER
            ) or {}
            if not isinstance(data, dict):
                continue
            if any(
                entry.get("customized")
                for entries in (data.get("behaviour") or {}).values()
                for entry in entries
                if isinstance(entry, dict)
            ):
                tables.append(path.stem)
        return tables

    def resolve(self, reference: EvidenceReference) -> ResolvedEvidence:
        if reference.kind == "table":
            return self._resolve_table(reference)
        if reference.kind == "artifact":
            return self._resolve_artifact(reference)
        table = EVIDENCE_TABLES[reference.kind]
        matches = [
            record
            for record in self.by_table.get(table, [])
            if reference.target in {record_name(record), record.sys_id}
        ]
        if not matches:
            return self._missing(reference, f"no {table} record matches {reference.target!r}")
        if len(matches) > 1:
            paths = ", ".join(
                sorted(
                    record.directory.relative_to(self.workspace).as_posix()
                    for record in matches
                )
            )
            return ResolvedEvidence(
                kind=reference.kind,
                target=reference.target,
                label=reference.label or reference.target,
                path=None,
                fingerprint=None,
                status="ambiguous",
                message=f"multiple {table} records match: {paths}",
            )
        return self._record(reference, matches[0])

    def _resolve_table(self, reference: EvidenceReference) -> ResolvedEvidence:
        path = self.workspace / "model" / "tables" / f"{reference.target}.yaml"
        if not path.is_file():
            return self._missing(reference, f"table model does not exist: {reference.target}")
        return ResolvedEvidence(
            kind=reference.kind,
            target=reference.target,
            label=reference.label or reference.target,
            path=path.relative_to(self.workspace).as_posix(),
            fingerprint=_file_hash(path),
            status="resolved",
        )

    def _resolve_artifact(self, reference: EvidenceReference) -> ResolvedEvidence:
        candidate = (self.workspace / reference.target).resolve()
        try:
            candidate.relative_to(self.workspace.resolve())
        except ValueError:
            return self._missing(reference, "artifact path escapes the instance workspace")
        if not candidate.exists():
            return self._missing(reference, f"artifact path does not exist: {reference.target}")
        record_dir = candidate if candidate.is_dir() else candidate.parent
        matches = [record for record in self.records if record.directory.resolve() == record_dir]
        record = matches[0] if len(matches) == 1 else None
        if record is None and (record_dir / META_FILE).is_file():
            loaded = read_record(record_dir)
            if loaded.sys_id:
                record = loaded
        if candidate.is_file():
            return ResolvedEvidence(
                kind=reference.kind,
                target=reference.target,
                label=reference.label or reference.target,
                path=candidate.relative_to(self.workspace).as_posix(),
                fingerprint=record.current_hash() if record else _file_hash(candidate),
                status="resolved",
            )
        if record is not None:
            return self._record(reference, record)
        return self._missing(reference, f"artifact path does not exist: {reference.target}")

    def _record(
        self, reference: EvidenceReference, record: LocalRecord
    ) -> ResolvedEvidence:
        return ResolvedEvidence(
            kind=reference.kind,
            target=reference.target,
            label=reference.label or record_name(record),
            path=(record.directory / "record.yaml").relative_to(self.workspace).as_posix(),
            fingerprint=record.current_hash(),
            status="resolved",
        )

    def _missing(self, reference: EvidenceReference, message: str) -> ResolvedEvidence:
        return ResolvedEvidence(
            kind=reference.kind,
            target=reference.target,
            label=reference.label or reference.target,
            path=None,
            fingerprint=None,
            status="missing",
            message=message,
        )


def _file_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()
