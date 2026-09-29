"""Incremental fetch of ServiceNow metadata into the per-instance mirror branch."""

from __future__ import annotations

import hashlib
import json
import re
import shutil
from collections import Counter, defaultdict
from collections.abc import Callable, Iterator, Mapping
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from snagentic.instance.catalog import Catalog
from snagentic.instance.config import InstanceConfig, InstancePaths, SyncSettings
from snagentic.instance.mirror import MirrorRepository
from snagentic.instance.records import (
    META_FILE,
    SYS_ID,
    load_yaml,
    read_record,
    record_map,
    record_relative_dir,
    write_record,
)
from snagentic.instance.tableapi import TableApiClient
from snagentic.instance.updatesets import fetch_update_sets, update_name_sys_id

INVENTORY_FIELDS = ["sys_id", "sys_class_name", "sys_updated_on", "sys_mod_count"]
TIMESTAMP_FORMAT = "%Y-%m-%d %H:%M:%S"
LIST_PAGE_SIZE = 1000
HEX32 = re.compile(r"(?<![0-9a-f])[0-9a-f]{32}(?![0-9a-f])")


PENDING_FILE = "pending-apply.json"
INDEX_FILE = "mirror-index.json"
INDEX_VERSION = 2
OPERATIONAL_NATURAL_KEYS = {"v_plugin": "id"}
OPERATIONAL_SNAPSHOT_FILES = {"sys_store_app": "operational/sys_store_app.json"}


class _TableDenied(Exception):
    """A class table is not readable (403/404) by the integration user."""


def _index_entry(directory: Path, meta: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "directory": directory,
        "sys_mod_count": str(meta.get("sys_mod_count") or ""),
        "sys_updated_on": str(meta.get("sys_updated_on") or ""),
        "sys_class_name": str(meta.get("sys_class_name") or ""),
    }


def replace_working_records(
    paths: InstancePaths, applied: list[dict[str, Any]]
) -> tuple[list[str], list[str]]:
    """Replace applied working-copy records with their canonical mirrored form."""

    mirror_metadata = paths.mirror_workspace / "metadata"
    mirror_records = record_map(mirror_metadata)
    replaced: list[str] = []
    missing: list[str] = []
    for item in applied:
        local_dir = paths.root / str(item.get("path", ""))
        sys_id = str(item.get("sys_id") or "")
        source = mirror_records.get(sys_id)
        if item.get("operation") == "delete":
            if source is None and local_dir.is_dir() and local_dir.resolve().is_relative_to(
                paths.metadata.resolve()
            ):
                shutil.rmtree(local_dir)
            continue
        if source is None:
            missing.append(sys_id)
            continue
        if local_dir.is_dir() and local_dir.resolve().is_relative_to(paths.metadata.resolve()):
            shutil.rmtree(local_dir)
        target = paths.metadata / source.relative_to(mirror_metadata)
        if target.exists():
            shutil.rmtree(target)
        shutil.copytree(source, target)
        replaced.append(target.relative_to(paths.root).as_posix())
    return replaced, missing


def build_filter(settings: SyncSettings) -> str:
    parts: list[str] = []
    if settings.include_classes:
        parts.append("sys_class_nameIN" + ",".join(settings.include_classes))
    if settings.deny_classes:
        parts.append("sys_class_nameNOT IN" + ",".join(settings.deny_classes))
    scope = scope_filter(settings)
    if scope:
        parts.append(scope)
    return "^".join(parts)


def scope_filter(settings: SyncSettings) -> str:
    return "sys_scope.scopeIN" + ",".join(settings.scopes) if settings.scopes else ""


def _coverage_material(settings: SyncSettings) -> dict[str, Any]:
    material = {
        "include_baseline": settings.include_baseline,
        "baseline_classes": sorted(settings.baseline_classes),
        "baseline_exclude": sorted(settings.baseline_exclude),
        "child_tables": {table: spec.parent_field
                         for table, spec in sorted(settings.child_tables.items())},
        "include_classes": sorted(settings.include_classes),
        "deny_classes": sorted(settings.deny_classes),
        "scopes": sorted(settings.scopes),
        "redact_fields": {key: sorted(value) for key, value in settings.redact_fields.items()},
        "property_value_allowlist": sorted(settings.property_value_allowlist),
    }
    return material


def coverage_fingerprint(settings: SyncSettings) -> str:
    """Fingerprint metadata coverage that requires an expensive full reconciliation."""

    material = _coverage_material(settings)
    return hashlib.sha256(json.dumps(material, sort_keys=True).encode()).hexdigest()[:16]


def legacy_coverage_fingerprint(
    settings: SyncSettings, operational_tables: list[str] | None = None
) -> str:
    """Accept state written by the short-lived format that included operational tables."""

    material = {
        **_coverage_material(settings),
        "operational_tables": sorted(operational_tables or settings.operational_tables),
    }
    return hashlib.sha256(json.dumps(material, sort_keys=True).encode()).hexdigest()[:16]


def keyset_query(base: str, *, start: str | None, cursor: tuple[str, str] | None) -> str:
    """Stable ordering by (sys_updated_on, sys_id); ``^NQ`` ORs two complete groups."""

    order = "^ORDERBYsys_updated_on^ORDERBYsys_id"
    prefix = f"{base}^" if base else ""
    if cursor is None:
        lower = f"sys_updated_on>={start}" if start else ""
        query = f"{prefix}{lower}".rstrip("^")
        return f"{query}{order}" if query else order.lstrip("^")
    timestamp, sys_id = cursor
    return (
        f"{prefix}sys_updated_on>{timestamp}"
        f"^NQ{prefix}sys_updated_on={timestamp}^sys_id>{sys_id}{order}"
    )


def operational_identity(table: str, row: Mapping[str, Any]) -> str:
    key = OPERATIONAL_NATURAL_KEYS.get(table)
    value = str(row.get(key or "sys_id") or "")
    return hashlib.sha256(f"{table}:{value}".encode()).hexdigest()[:32] if value else ""


class InstanceSync:
    def __init__(
        self,
        paths: InstancePaths,
        config: InstanceConfig,
        client: TableApiClient,
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        progress: Callable[[str], None] | None = None,
    ) -> None:
        self.paths = paths
        self.config = config
        self.client = client
        self.clock = clock
        self.progress = progress or (lambda _message: None)
        self._pending_index: dict[str, dict[str, Any]] | None = None
        self.mirror = MirrorRepository(paths, config.mirror_branch)

    # -- state -----------------------------------------------------------------
    def load_state(self) -> dict[str, Any]:
        if not self.paths.sync_state.is_file():
            return {}
        raw = json.loads(self.paths.sync_state.read_text(encoding="utf-8"))
        return raw if isinstance(raw, dict) else {}

    def save_state(self, state: dict[str, Any]) -> None:
        self.paths.state.mkdir(parents=True, exist_ok=True)
        self.paths.sync_state.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n")

    def catalog(self, *, refresh: bool) -> Catalog:
        cached = None if refresh else Catalog.load(self.paths.catalog_cache)
        if cached is not None:
            return cached
        catalog = Catalog.discover(self.client)
        catalog.save(self.paths.catalog_cache)
        return catalog

    # -- fetch -----------------------------------------------------------------
    def fetch(self, *, full: bool = False) -> dict[str, Any]:
        restored = self.mirror.ensure()
        state = self.load_state()
        mirror_tip = self.mirror.tip()
        if state.get("mirror") != mirror_tip:
            restored = self.mirror.restore()
        settings = self.config.sync
        fingerprint = coverage_fingerprint(settings)
        coverage_matches = state.get("coverage") in {
            fingerprint,
            legacy_coverage_fingerprint(settings),
            legacy_coverage_fingerprint(
                settings, ["v_plugin", "sys_plugins", "sys_store_app", "sys_domain"]
            ),
        }
        if (
            not restored
            or not state.get("watermark")
            or state.get("mirror") != mirror_tip
            or not coverage_matches
        ):
            full = True
        catalog = self.catalog(refresh=full)
        metadata_root = self.paths.mirror_workspace / "metadata"
        existing = self._load_index(metadata_root, mirror_tip)
        start = None if full else self._start(state["watermark"])
        denied = set(settings.deny_classes)
        mode = "full" if full else "incremental"
        self.progress(f"{mode} fetch; {len(existing)} records mirrored")

        seen: set[str] = set()
        changed: dict[str, list[str]] = defaultdict(list)
        watermark = state.get("watermark")
        written: list[dict[str, Any]] = []
        rejected: list[dict[str, str]] = []
        self.mirror.begin_write()
        written.extend(self._reconcile_duplicate_paths(existing, metadata_root, catalog))

        def admit(row: Mapping[str, Any]) -> str | None:
            """Record a row as present; return its sys_id when it must be (re)written."""

            nonlocal watermark
            sys_id = str(row.get("sys_id") or "")
            table = str(row.get("sys_class_name") or "")
            if not SYS_ID.fullmatch(sys_id) or sys_id in seen or any(
                ancestor in denied for ancestor in catalog.ancestors(table)
            ):
                return None
            seen.add(sys_id)
            updated = str(row.get("sys_updated_on") or "")
            watermark = max(watermark or "", updated) or watermark
            known = existing.get(sys_id)
            if (
                known
                and known["sys_mod_count"] == str(row.get("sys_mod_count") or "")
                and known["sys_updated_on"] == updated
                and known["sys_class_name"] == table
            ):
                return None
            return sys_id

        def store(record: dict[str, Any], table: str, *, read_only: bool = False) -> None:
            if read_only:
                record["sys_class_name"] = table
            else:
                record.setdefault("sys_class_name", table)
            try:
                target = metadata_root / Path(record_relative_dir(record, catalog))
            except ValueError as error:
                rejected.append({"table": table, "sys_id": str(record.get("sys_id")),
                                 "reason": str(error)})
                return
            sys_id = str(record["sys_id"])
            previous = existing.get(sys_id)
            if previous and previous["directory"] != target:
                _remove_tree(previous["directory"])
            meta = write_record(
                target,
                record,
                catalog,
                settings.redact_fields,
                settings.property_value_allowlist,
                read_only=read_only,
            )
            existing[sys_id] = _index_entry(target, meta)
            written.append(meta)

        operational = sorted(
            table for table in set(settings.operational_tables)
            if not set(catalog.ancestors(table)).intersection(denied)
        )
        operational_set = set(operational)
        previous_operational = set(state.get("operational_tables") or operational)
        removed_operational = previous_operational - operational_set
        operational_existing = {
            sys_id for sys_id, entry in existing.items()
            if entry["sys_class_name"] in operational_set
        }
        removed_operational_ids = {
            sys_id for sys_id, entry in existing.items()
            if entry["sys_class_name"] in removed_operational
        }
        operational_counts: dict[str, int] = {}
        operational_denied: list[str] = []
        operational_sources: dict[tuple[str, str], str] = {}
        operational_snapshots: dict[str, dict[str, dict[str, Any]]] = {}
        operational_source_types: dict[str, str] = {}
        bulk = [] if settings.include_baseline else self.baseline_tables(catalog)
        bulk_set = set(bulk)
        denied_tables: list[str] = []
        pool = ThreadPoolExecutor(max_workers=settings.workers)
        try:
            def operational_listing(
                table: str,
            ) -> tuple[str, list[dict[str, Any]] | None]:
                fields = [*INVENTORY_FIELDS]
                natural_key = OPERATIONAL_NATURAL_KEYS.get(table)
                if natural_key:
                    fields.append(natural_key)
                try:
                    rows = list(self._operational_rows(table, fields))
                    operational_source_types[table] = "table_api"
                except _TableDenied:
                    snapshot_rows = self._operational_snapshot(table)
                    if snapshot_rows is None:
                        return table, None
                    rows = snapshot_rows
                    operational_source_types[table] = "application_manager_snapshot"
                    operational_snapshots[table] = {}
                normalized = []
                for row in rows:
                    source_sys_id = str(row.get("sys_id") or "")
                    identity = operational_identity(table, row)
                    if identity:
                        operational_sources[(table, identity)] = (
                            str(row.get(natural_key) or "")
                            if natural_key else source_sys_id
                        )
                        if table in operational_snapshots:
                            operational_snapshots[table][identity] = {
                                **row,
                                "source_sys_id": source_sys_id,
                                "sys_id": identity,
                            }
                        normalized.append({
                            **row,
                            "sys_id": identity,
                            "sys_class_name": table,
                        })
                return table, normalized

            for number, (table, rows) in enumerate(
                pool.map(operational_listing, operational), start=1
            ):
                if rows is None:
                    seen.update(
                        sys_id for sys_id, entry in existing.items()
                        if entry["sys_class_name"] == table
                    )
                    denied_tables.append(table)
                    operational_denied.append(table)
                    self.progress(
                        f"[operational {number}/{len(operational)}] "
                        f"{table}: not readable, skipped"
                    )
                    continue
                operational_counts[table] = len(rows)
                pending = 0
                for row in rows:
                    sys_id = admit(row)
                    if sys_id:
                        changed[table].append(sys_id)
                        pending += 1
                self.progress(
                    f"[operational {number}/{len(operational)}] {table}: "
                    f"{len(rows)} records, {pending} to download"
                )

            if full:
                # List ids per class (cheap), then read full rows only for new or changed
                # records: a coverage change or --full does not re-download the mirror.
                def listing(table: str) -> tuple[str, list[dict[str, Any]] | None]:
                    query = "^".join(part for part in (f"sys_class_name={table}",
                                                       scope_filter(settings)) if part)
                    try:
                        return table, list(self._keyset(
                            table, query, None, INVENTORY_FIELDS,
                            page_size=LIST_PAGE_SIZE, allow_missing=True,
                        ))
                    except _TableDenied:
                        return table, None

                for number, (table, rows) in enumerate(pool.map(listing, bulk), start=1):
                    if rows is None:
                        # Keep what is already mirrored rather than treating it as deleted.
                        seen.update(sys_id for sys_id, entry in existing.items()
                                    if entry["sys_class_name"] == table)
                        denied_tables.append(table)
                        self.progress(f"[{number}/{len(bulk)}] {table}: not readable, skipped")
                        continue
                    pending = 0
                    for row in rows:
                        sys_id = admit(row)
                        if sys_id:
                            changed[str(row.get("sys_class_name") or table)].append(sys_id)
                            pending += 1
                    if rows:
                        self.progress(f"[{number}/{len(bulk)}] {table}: {len(rows)} records, "
                                      f"{pending} to download")
            elif bulk:
                # One delta query over the base table finds changes in every bulk class.
                for row in self._keyset("sys_metadata", scope_filter(settings), start,
                                        INVENTORY_FIELDS):
                    table = str(row.get("sys_class_name") or "")
                    if table in bulk_set:
                        sys_id = admit(row)
                        if sys_id:
                            changed[table].append(sys_id)

            for row in self._inventory(build_filter(settings), start):
                if str(row.get("sys_class_name") or "") in bulk_set | operational_set:
                    # Already read in full above (a customer update of a bulk class).
                    seen.add(str(row.get("sys_id") or ""))
                    continue
                sys_id = admit(row)
                if sys_id:
                    changed[str(row.get("sys_class_name"))].append(sys_id)

            jobs = [
                (table, ids[offset : offset + settings.batch_size])
                for table in sorted(changed)
                for ids in [sorted(changed[table])]
                for offset in range(0, len(ids), settings.batch_size)
            ]
            total = sum(len(chunk) for _, chunk in jobs)
            if total:
                self.progress(f"downloading {total} records")

            def download(job: tuple[str, list[str]]) -> tuple[str, list[dict[str, Any]]]:
                table, chunk = job
                natural_key = OPERATIONAL_NATURAL_KEYS.get(table)
                if table in operational_set:
                    if table in operational_snapshots:
                        return table, [
                            operational_snapshots[table][identity] for identity in chunk
                        ]
                    query_key = natural_key or "sys_id"
                    values = [
                        operational_sources[(table, identity)]
                        for identity in chunk
                    ]
                    rows = self.client.query(
                        table, query=f"{query_key}IN" + ",".join(values), limit=len(values),
                        allow_missing=True,
                    ) or []
                    normalized = []
                    for row in rows:
                        source_sys_id = str(row.get("sys_id") or "")
                        identity = operational_identity(table, row)
                        if identity:
                            normalized.append({
                                **row,
                                "source_sys_id": source_sys_id,
                                "sys_id": identity,
                            })
                    return table, normalized
                result = self.client.query(
                    table, query="sys_idIN" + ",".join(chunk), limit=len(chunk),
                    allow_missing=True,
                )
                return table, result or []

            done = 0
            for number, (table, rows) in enumerate(pool.map(download, jobs), start=1):
                for record in rows:
                    store(record, table, read_only=table in operational_set)
                done += len(jobs[number - 1][1])
                if number % 50 == 0 or number == len(jobs):
                    self.progress(f"downloaded {done}/{total}")
        finally:
            pool.shutdown(wait=True)

        deleted_ids = sorted(
            set(existing) - seen
            if full
            else (
                set(self._deleted_since(start, existing))
                | (operational_existing - seen)
                | removed_operational_ids
            )
        )
        for sys_id in deleted_ids:
            _remove_tree(existing.pop(sys_id)["directory"])

        children_summary: dict[str, Any] | None = None
        if settings.child_tables:
            from snagentic.instance.children import ChildDenied, ChildSync

            def child_keyset(*args: Any, **kwargs: Any) -> Iterator[dict[str, Any]]:
                try:
                    yield from self._keyset(*args, **kwargs)
                except _TableDenied as exc:
                    raise ChildDenied(str(exc)) from exc

            children_summary = ChildSync(
                client=self.client, keyset=child_keyset, specs=settings.child_tables,
                catalog=catalog, state_dir=self.paths.state, redact=settings.redact_fields,
                batch_size=settings.batch_size, progress=self.progress,
            ).refresh(full=full, start=start, index=existing,
                      touched=[str(meta.get("sys_id")) for meta in written])

        model_summary: dict[str, int] | None = None
        if settings.model:
            from snagentic.instance.model import ModelBuilder

            model_summary = ModelBuilder(
                self.client, self.paths.state, self.paths.mirror_workspace, catalog,
                page_size=self.config.page_size, progress=self.progress,
            ).refresh(start=start, index=existing)
        update_set_summary = self.refresh_update_sets()
        self.progress("committing mirror")
        authors = Counter(str(meta.get("sys_updated_by") or "unknown") for meta in written)
        message = self._commit_message(
            full=full,
            watermark=watermark,
            written=len(written),
            deleted=len(deleted_ids),
            authors=authors,
            update_sets=update_set_summary,
        )
        commit = self.mirror.commit(message)
        self._save_index(existing, metadata_root, commit.commit)
        now = self.clock().strftime(TIMESTAMP_FORMAT)
        state.update(
            {
                "watermark": watermark,
                "coverage": fingerprint,
                "operational_tables": operational,
                "mirror": commit.commit,
                "last_fetch": now,
                **({"last_full": now} if full else {}),
            }
        )
        self.save_state(state)
        reconciliation = self.reconcile_pending_apply()
        return {
            "instance": self.config.name,
            "mode": "full" if full else "incremental",
            "updated": len(written),
            "deleted": len(deleted_ids),
            "operational": {
                "tables": operational,
                "records": sum(
                    1 for entry in existing.values()
                    if entry["sys_class_name"] in operational_set
                ),
                "records_by_table": operational_counts,
                "sources": operational_source_types,
                "unreadable_tables": operational_denied,
                "complete": not operational_denied,
                "required_capabilities_complete": all(
                    operational_counts.get(table, 0) > 0
                    for table in ("v_plugin", "sys_store_app", "domain")
                    if table in operational_set
                ),
            },
            "watermark": watermark,
            "mirror_branch": self.config.mirror_branch,
            "mirror_commit": commit.commit,
            "mirror_changed": commit.changed,
            "update_sets": update_set_summary,
            **({"model": model_summary} if model_summary is not None else {}),
            **({"children": children_summary} if children_summary is not None else {}),
            **({"unreadable_tables": denied_tables} if denied_tables else {}),
            **({"rejected_records": rejected} if rejected else {}),
            "authors": dict(sorted(authors.items())),
            **({"pending_apply": reconciliation} if reconciliation else {}),
        }

    def refetch(self, table: str, sys_ids: list[str]) -> list[dict[str, Any]]:
        """Targeted refresh of specific records (used after pushing)."""

        self.mirror.ensure()
        self.mirror.begin_write()
        catalog = self.catalog(refresh=False)
        metadata_root = self.paths.mirror_workspace / "metadata"
        existing = self._load_index(metadata_root, self.mirror.tip())
        rows = self.client.query(table, query="sys_idIN" + ",".join(sys_ids), limit=len(sys_ids))
        found: set[str] = set()
        metas: list[dict[str, Any]] = []
        for record in rows or []:
            record.setdefault("sys_class_name", table)
            sys_id = str(record["sys_id"])
            found.add(sys_id)
            target = metadata_root / Path(record_relative_dir(record, catalog))
            previous = existing.get(sys_id)
            if previous and previous["directory"] != target:
                _remove_tree(previous["directory"])
            meta = write_record(
                target,
                record,
                catalog,
                self.config.sync.redact_fields,
                self.config.sync.property_value_allowlist,
            )
            existing[sys_id] = _index_entry(target, meta)
            metas.append(meta)
        for sys_id in set(sys_ids) - found:
            if sys_id in existing:
                _remove_tree(existing.pop(sys_id)["directory"])
        self._pending_index = existing
        return metas

    def refresh_update_sets(self) -> dict[str, int]:
        settings = self.config.sync
        self.mirror.begin_write()
        return fetch_update_sets(
            self.client,
            self.paths.mirror_workspace / "update-sets",
            window_days=settings.update_set_window_days,
            batch_size=settings.batch_size,
        )

    def reconcile_pending_apply(self) -> dict[str, Any] | None:
        """After a fetch, settle an interrupted apply recorded in ``pending-apply.json``.

        Every change recorded as applied is replaced in the working copy by its canonical
        mirrored form, so records created before the failure are not planned (and
        created) again. The marker is cleared only when every applied record was found.
        """

        marker = self.paths.state / PENDING_FILE
        if not marker.is_file():
            return None
        pending = json.loads(marker.read_text(encoding="utf-8"))
        applied = [item for item in pending.get("applied", []) if isinstance(item, dict)]
        replaced, missing = replace_working_records(self.paths, applied)
        result: dict[str, Any] = {
            "plan_id": pending.get("plan_id"),
            "previous_status": pending.get("status"),
            "reconciled_paths": replaced,
            "missing": missing,
        }
        if pending.get("status") == "apply_in_progress":
            result["warning"] = (
                "the previous apply stopped without recording its last change; review the "
                "next plan for records that may already exist on the instance"
            )
        if missing:
            result["status"] = "reconciliation_required"
            return result
        marker.unlink()
        result["status"] = "reconciled"
        if replaced:
            result["next_step"] = "commit the reconciled paths, then integrate"
        return result

    def commit_refetch(self, message: str) -> str | None:
        commit = self.mirror.commit(message)
        pending = getattr(self, "_pending_index", None)
        if pending is not None:
            self._save_index(pending, self.paths.mirror_workspace / "metadata", commit.commit)
            self._pending_index = None
        state = self.load_state()
        state["mirror"] = commit.commit
        self.save_state(state)
        return commit.commit

    # -- helpers ---------------------------------------------------------------
    def _start(self, watermark: str) -> str:
        parsed = datetime.strptime(watermark, TIMESTAMP_FORMAT)
        overlap = timedelta(seconds=self.config.sync.watermark_overlap_seconds)
        return (parsed - overlap).strftime(TIMESTAMP_FORMAT)

    def _inventory(self, base: str, start: str | None) -> Iterator[dict[str, Any]]:
        if self.config.sync.include_baseline:
            yield from self._keyset("sys_metadata", base, start, INVENTORY_FIELDS)
            return
        # Customer updates: every tracked change writes a sys_update_xml row whose name is
        # the record's sys_update_name, so out-of-box records never appear here.
        names = sorted({
            str(row.get("name") or "")
            for row in self._keyset("sys_update_xml", "", start, ["sys_id", "name",
                                                                   "sys_updated_on"])
        } - {""})
        chunk_size = self.config.sync.batch_size
        for offset in range(0, len(names), chunk_size):
            chunk = ",".join(names[offset : offset + chunk_size])
            query = f"sys_update_nameIN{chunk}" + (f"^{base}" if base else "")
            yield from self.client.iterate(
                "sys_metadata", query=query + "^ORDERBYsys_id", fields=INVENTORY_FIELDS
            )

    def _keyset(
        self,
        table: str,
        base: str,
        start: str | None,
        fields: list[str] | None,
        *,
        page_size: int | None = None,
        allow_missing: bool = False,
    ) -> Iterator[dict[str, Any]]:
        cursor: tuple[str, str] | None = None
        size = page_size or self.config.page_size
        while True:
            query = keyset_query(base, start=start, cursor=cursor)
            result = self.client.query(table, query=query, fields=fields, limit=size,
                                       allow_missing=allow_missing)
            if result is None and allow_missing:
                raise _TableDenied(table)
            page = result or []
            # ServiceNow removes ACL-denied rows after applying the limit, so a short page
            # does not mean the end: only an empty page does.
            if not page:
                return
            yield from page
            last = page[-1]
            next_cursor = (str(last.get("sys_updated_on")), str(last.get("sys_id")))
            if next_cursor == cursor:
                return
            cursor = next_cursor

    def _operational_rows(
        self, table: str, fields: list[str]
    ) -> Iterator[dict[str, Any]]:
        """Complete offset inventory for small operational tables whose view sys_id may
        not be a unique key (notably v_plugin)."""

        offset = 0
        while True:
            page = self.client.query(
                table,
                fields=fields,
                limit=LIST_PAGE_SIZE,
                offset=offset,
                allow_missing=True,
            )
            if page is None:
                raise _TableDenied(table)
            if not page:
                return
            yield from page
            offset += LIST_PAGE_SIZE

    def _operational_snapshot(self, table: str) -> list[dict[str, Any]] | None:
        relative = OPERATIONAL_SNAPSHOT_FILES.get(table)
        if relative is None:
            return None
        path = self.paths.state / relative
        try:
            snapshot = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        records = snapshot.get("records")
        if snapshot.get("complete") is not True or not isinstance(records, list):
            return None
        normalized = []
        for record in records:
            if not isinstance(record, dict):
                return None
            source_sys_id = str(record.get("sys_id") or "")
            if not re.fullmatch(r"[A-Za-z0-9]{32}", source_sys_id):
                return None
            values = {
                "name": str(record.get("name") or ""),
                "scope": str(record.get("scope") or ""),
                "version": str(record.get("version") or ""),
                "vendor": str(record.get("vendor") or ""),
                "short_description": str(record.get("short_description") or ""),
            }
            if not values["name"] or not values["scope"]:
                return None
            normalized.append({
                **values,
                "sys_id": source_sys_id,
                "sys_scope": values["scope"],
                "sys_class_name": table,
                "sys_mod_count": hashlib.sha256(
                    json.dumps(values, sort_keys=True).encode()
                ).hexdigest()[:16],
                "availability": "available",
                "inventory_source": "application_manager",
            })
        return normalized

    def _deleted_since(self, start: str | None, existing: dict[str, dict[str, Any]]) -> list[str]:
        if start is None:
            return []
        candidates: set[str] = set()
        for row in self.client.iterate(
            "sys_metadata_delete", query=f"sys_updated_on>={start}", allow_missing=True
        ):
            own = str(row.get("sys_id") or "")
            for value in row.values():
                candidates.update(
                    token for token in HEX32.findall(str(value)) if token != own
                )
        for row in self.client.iterate(
            "sys_update_xml",
            query=f"action=DELETE^sys_updated_on>={start}",
            fields=["name"],
            allow_missing=True,
        ):
            sys_id = update_name_sys_id(str(row.get("name") or ""))
            if sys_id:
                candidates.add(sys_id)
        candidates &= set(existing)
        if not candidates:
            return []
        still_present: set[str] = set()
        ids = sorted(candidates)
        for offset in range(0, len(ids), 100):
            chunk = ids[offset : offset + 100]
            rows = self.client.query(
                "sys_metadata", query="sys_idIN" + ",".join(chunk), fields=["sys_id"],
                limit=len(chunk),
            ) or []
            still_present.update(str(row.get("sys_id")) for row in rows)
        return sorted(candidates - still_present)

    def baseline_tables(self, catalog: Catalog) -> list[str]:
        """Classes mirrored in full (out-of-box included): the configured behaviour classes
        and every class extending them, restricted by include/deny settings."""

        settings = self.config.sync
        configured = set(settings.baseline_classes)
        every = "*" in configured
        excluded = set(settings.baseline_exclude)
        denied = set(settings.deny_classes)
        operational = set(settings.operational_tables)
        include = set(settings.include_classes)
        tables = []
        for table in catalog.metadata_classes():
            chain = catalog.ancestors(table)
            if table in excluded or table in operational or denied.intersection(chain):
                continue
            if not every and not configured.intersection(chain):
                continue
            if include and table not in include:
                continue
            tables.append(table)
        return sorted(tables)

    def _load_index(self, metadata_root: Path, tip: str | None) -> dict[str, dict[str, Any]]:
        path = self.paths.state / INDEX_FILE
        if tip and path.is_file() and metadata_root.is_dir():
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
            except ValueError:
                raw = {}
            if (
                raw.get("version") == INDEX_VERSION
                and raw.get("tip") == tip
                and isinstance(raw.get("records"), dict)
            ):
                return {
                    sys_id: {
                        **entry,
                        "directory": metadata_root / entry["directory"],
                        "duplicate_directories": [
                            metadata_root / directory
                            for directory in entry.get("duplicate_directories", [])
                        ],
                    }
                    for sys_id, entry in raw["records"].items()
                }
        return self._mirror_index(metadata_root)

    def _save_index(
        self, index: dict[str, dict[str, Any]], metadata_root: Path, tip: str | None
    ) -> None:
        if not tip:
            return
        records = {
            sys_id: {
                **{key: value for key, value in entry.items() if key != "directory"},
                "directory": Path(entry["directory"]).relative_to(metadata_root).as_posix(),
                "duplicate_directories": [
                    Path(directory).relative_to(metadata_root).as_posix()
                    for directory in entry.get("duplicate_directories", [])
                ],
            }
            for sys_id, entry in sorted(index.items())
        }
        self.paths.state.mkdir(parents=True, exist_ok=True)
        temporary = self.paths.state / (INDEX_FILE + ".tmp")
        temporary.write_text(
            json.dumps({"version": INDEX_VERSION, "tip": tip, "records": records}),
            encoding="utf-8",
        )
        temporary.replace(self.paths.state / INDEX_FILE)

    @staticmethod
    def _mirror_index(metadata_root: Path) -> dict[str, dict[str, Any]]:
        index: dict[str, dict[str, Any]] = {}
        if not metadata_root.is_dir():
            return index
        for meta_path in metadata_root.rglob(META_FILE):
            meta = load_yaml(meta_path)
            sys_id = meta.get("sys_id")
            if isinstance(sys_id, str):
                if sys_id in index:
                    index[sys_id].setdefault("duplicate_directories", []).append(
                        meta_path.parent
                    )
                else:
                    index[sys_id] = _index_entry(meta_path.parent, meta)
        return index

    def _reconcile_duplicate_paths(
        self,
        index: dict[str, dict[str, Any]],
        metadata_root: Path,
        catalog: Catalog,
    ) -> list[dict[str, Any]]:
        """Collapse stale paths for one record after a scope sys_id becomes resolvable."""

        written: list[dict[str, Any]] = []
        for sys_id, entry in index.items():
            duplicates = entry.pop("duplicate_directories", [])
            if not duplicates:
                continue
            directories = [entry["directory"], *duplicates]
            record = read_record(directories[0])
            raw = {**record.meta, **record.values}
            target = metadata_root / Path(record_relative_dir(raw, catalog))
            for directory in directories:
                if directory != target:
                    _remove_tree(directory)
            meta = write_record(
                target,
                raw,
                catalog,
                self.config.sync.redact_fields,
                self.config.sync.property_value_allowlist,
                read_only=record.meta.get("read_only") is True,
            )
            index[sys_id] = _index_entry(target, meta)
            written.append(meta)
        return written

    def _commit_message(
        self,
        *,
        full: bool,
        watermark: str | None,
        written: int,
        deleted: int,
        authors: Counter[str],
        update_sets: dict[str, int],
    ) -> str:
        lines = [
            f"snagentic fetch {self.config.name}: {written} updated, {deleted} deleted",
            "",
            f"Instance: {self.config.name} ({self.config.kind})",
            f"Mode: {'full' if full else 'incremental'}",
            f"Watermark: {watermark or 'none'}",
            f"Update sets: {update_sets['update_sets']} ({update_sets['open']} open, "
            f"{update_sets['changes']} changes)",
        ]
        if authors:
            lines.append("Authors: " + ", ".join(f"{name} ({count})"
                                                 for name, count in authors.most_common(20)))
        return "\n".join(lines) + "\n"


def _remove_tree(directory: Path) -> None:
    import shutil

    if directory.exists():
        shutil.rmtree(directory)
    parent = directory.parent
    while parent.name and parent.name != "metadata" and parent.exists() and not any(
        parent.iterdir()
    ):
        parent.rmdir()
        parent = parent.parent
