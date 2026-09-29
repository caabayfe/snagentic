"""Child rows that belong to a mirrored record but are not ``sys_metadata`` themselves.

Flow Designer logic (``sys_hub_*_instance_v2``), form and list layouts
(``sys_ui_element``), legacy workflow activities and variable values live in plain tables
whose rows are captured in the parent's update XML. They are written, read-only, next to
the owning record::

    metadata/<scope>/sys_hub_flow/<name>--<sys_id>/_children/sys_hub_action_instance_v2.yaml

Compressed (gzip + base64) values, such as flow step inputs, are decoded to readable JSON.
Owners are resolved through ``parent_field``: either a mirrored record or a row of a child
table processed earlier (``wf_activity`` -> ``wf_workflow_version`` -> ``wf_workflow``).
"""

from __future__ import annotations

import base64
import binascii
import gzip
import json
import shutil
from collections import defaultdict
from collections.abc import Callable, Iterable, Iterator, Mapping
from pathlib import Path
from typing import Any

import yaml

from snagentic.instance.catalog import Catalog
from snagentic.instance.config import ChildTable
from snagentic.instance.records import SYS_ID, atomic_write

CHILDREN_DIRECTORY = "_children"
OWNERS_FILE = "children-owners.json"
DROPPED_FIELDS = frozenset(
    {"sys_created_by", "sys_created_on", "sys_mod_count", "sys_tags", "sys_domain_path"}
)
_DUMPER: Any = getattr(yaml, "CSafeDumper", yaml.SafeDumper)

Keyset = Callable[..., Iterator[dict[str, Any]]]


class ChildDenied(Exception):
    """Raised by the keyset callable when a child table is not readable."""


def decode_value(value: str) -> Any:
    """Decode ServiceNow ``compressed`` values (gzip + base64), parsing JSON when possible."""

    if not value.startswith("H4sI"):
        return value
    try:
        text = gzip.decompress(base64.b64decode(value, validate=True)).decode("utf-8")
    except (binascii.Error, OSError, EOFError, UnicodeDecodeError, ValueError):
        return value
    try:
        return json.loads(text)
    except ValueError:
        return text


def _number(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return float("inf")


def clean_row(
    row: Mapping[str, Any], table: str, catalog: Catalog, redact: Mapping[str, Iterable[str]]
) -> dict[str, Any]:
    secret = catalog.secret_fields(table) | set(redact.get(table, ()))
    result: dict[str, Any] = {}
    for key, raw in sorted(row.items()):
        if key in DROPPED_FIELDS:
            continue
        value = "" if raw is None else str(raw)
        if key in secret or key.casefold() in secret:
            result[key] = "[redacted]" if value else ""
            continue
        result[key] = decode_value(value)
    return result


def _sort_key(row: Mapping[str, Any]) -> tuple[float, float, str]:
    return (_number(row.get("order")), _number(row.get("position")), str(row.get("sys_id")))


class ChildSync:
    def __init__(
        self,
        *,
        client: Any,
        keyset: Keyset,
        specs: Mapping[str, ChildTable],
        catalog: Catalog,
        state_dir: Path,
        redact: Mapping[str, Iterable[str]],
        batch_size: int,
        progress: Callable[[str], None],
    ) -> None:
        self.client = client
        self.keyset = keyset
        self.specs = dict(specs)
        self.catalog = catalog
        self.state_dir = state_dir
        self.redact = redact
        self.batch_size = batch_size
        self.progress = progress

    # -- public ----------------------------------------------------------------
    def refresh(
        self,
        *,
        full: bool,
        start: str | None,
        index: Mapping[str, Mapping[str, Any]],
        touched: Iterable[str],
    ) -> dict[str, Any]:
        """Rewrite ``_children`` files for affected owners.

        ``touched`` are records written by this fetch (their folders were replaced).
        Incremental refresh also follows child rows changed since ``start``; child rows
        deleted without a change to the owner are removed by the next full fetch.
        """

        previous = self._load_owners()
        denied: list[str] = []
        if full:
            rows, owners = self._collect_all(index, denied)
            affected = set(rows) | set(previous.values())
        else:
            affected = {sys_id for sys_id in touched if sys_id in index}
            affected |= self._changed_owners(start, index, previous, denied)
            kept = {child: owner for child, owner in previous.items() if owner not in affected}
            rows, fresh = self._collect_for(affected, index, denied)
            owners = {**kept, **fresh}
        written = self._write(affected, rows, index, set(denied))
        if denied:
            # Keep ownership of rows we could not read so their files are not dropped.
            owners.update({c: o for c, o in previous.items() if c not in owners})
        self._save_owners(owners)
        summary: dict[str, Any] = {
            "owners": written,
            "rows": sum(len(items) for per in rows.values() for items in per.values()),
        }
        if denied:
            summary["unreadable_tables"] = sorted(set(denied))
        return summary

    # -- collection ------------------------------------------------------------
    def _resolve(
        self, parent: str, index: Mapping[str, Any], owners: Mapping[str, str]
    ) -> str | None:
        if parent in index:
            return parent
        return owners.get(parent)

    def _collect_all(
        self, index: Mapping[str, Any], denied: list[str]
    ) -> tuple[dict[str, dict[str, list[dict[str, Any]]]], dict[str, str]]:
        rows: dict[str, dict[str, list[dict[str, Any]]]] = defaultdict(lambda: defaultdict(list))
        owners: dict[str, str] = {}
        for table, spec in self.specs.items():
            count = 0
            try:
                for row in self.keyset(table, "", None, None, allow_missing=True):
                    count += self._admit(table, spec, row, index, owners, rows)
            except ChildDenied:
                denied.append(table)
                self.progress(f"children {table}: not readable, skipped")
                continue
            self.progress(f"children {table}: {count} rows")
        return rows, owners

    def _collect_for(
        self, affected: set[str], index: Mapping[str, Any], denied: list[str]
    ) -> tuple[dict[str, dict[str, list[dict[str, Any]]]], dict[str, str]]:
        rows: dict[str, dict[str, list[dict[str, Any]]]] = defaultdict(lambda: defaultdict(list))
        owners: dict[str, str] = {}
        if not affected:
            return rows, owners
        for table, spec in self.specs.items():
            parents = sorted(affected | set(owners))
            try:
                for offset in range(0, len(parents), self.batch_size):
                    chunk = ",".join(parents[offset : offset + self.batch_size])
                    for row in self.keyset(table, f"{spec.parent_field}IN{chunk}", None, None,
                                           allow_missing=True):
                        self._admit(table, spec, row, index, owners, rows, affected)
            except ChildDenied:
                denied.append(table)
        return rows, owners

    def _admit(
        self,
        table: str,
        spec: ChildTable,
        row: Mapping[str, Any],
        index: Mapping[str, Any],
        owners: dict[str, str],
        rows: dict[str, dict[str, list[dict[str, Any]]]],
        restrict: set[str] | None = None,
    ) -> int:
        sys_id = str(row.get("sys_id") or "")
        owner = self._resolve(str(row.get(spec.parent_field) or ""), index, owners)
        if not SYS_ID.fullmatch(sys_id) or not owner or (restrict and owner not in restrict):
            return 0
        if sys_id in owners:
            return 0
        owners[sys_id] = owner
        rows[owner][table].append(clean_row(row, table, self.catalog, self.redact))
        return 1

    def _changed_owners(
        self,
        start: str | None,
        index: Mapping[str, Any],
        previous: Mapping[str, str],
        denied: list[str],
    ) -> set[str]:
        affected: set[str] = set()
        if start is None:
            return affected
        for table, spec in self.specs.items():
            try:
                for row in self.keyset(table, "", start, ["sys_id", "sys_updated_on",
                                                         spec.parent_field],
                                       allow_missing=True):
                    sys_id = str(row.get("sys_id") or "")
                    if sys_id in previous:
                        affected.add(previous[sys_id])
                    owner = self._resolve(str(row.get(spec.parent_field) or ""), index, previous)
                    if owner:
                        affected.add(owner)
            except ChildDenied:
                denied.append(table)
        return affected

    # -- output ----------------------------------------------------------------
    def _write(
        self,
        affected: Iterable[str],
        rows: Mapping[str, Mapping[str, list[dict[str, Any]]]],
        index: Mapping[str, Mapping[str, Any]],
        denied: set[str],
    ) -> int:
        written = 0
        for owner in sorted(affected):
            entry = index.get(owner)
            if not entry:
                continue
            directory = Path(entry["directory"])
            if not directory.is_dir():
                continue
            target = directory / CHILDREN_DIRECTORY
            per_table = rows.get(owner, {})
            for table in self.specs:
                if table in denied:
                    continue
                path = target / f"{table}.yaml"
                items = per_table.get(table)
                if items:
                    document = {
                        "table": table,
                        "parent_field": self.specs[table].parent_field,
                        "read_only": True,
                        "rows": sorted(items, key=_sort_key),
                    }
                    atomic_write(path, yaml.dump(document, Dumper=_DUMPER, sort_keys=False,
                                                 allow_unicode=False, width=100))
                elif path.exists():
                    path.unlink()
            if target.is_dir() and not any(target.iterdir()):
                shutil.rmtree(target)
            if per_table:
                written += 1
        return written

    def _load_owners(self) -> dict[str, str]:
        path = self.state_dir / OWNERS_FILE
        if not path.is_file():
            return {}
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except ValueError:
            return {}
        return {str(k): str(v) for k, v in raw.items()} if isinstance(raw, dict) else {}

    def _save_owners(self, owners: Mapping[str, str]) -> None:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        (self.state_dir / OWNERS_FILE).write_text(
            json.dumps(dict(sorted(owners.items())), separators=(",", ":")) + "\n"
        )
