"""Local change planning and the update-set-native write path."""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from snagentic.artifacts.normalization import canonical_json
from snagentic.errors import ConflictError, PolicyDeniedError, ServiceNowError
from snagentic.instance.catalog import Catalog
from snagentic.instance.config import InstanceConfig, InstancePaths
from snagentic.instance.mirror import MirrorRepository, git, worktree_status
from snagentic.instance.records import (
    META_FIELDS,
    META_FILE,
    RECORD_FILE,
    SYS_ID,
    LocalRecord,
    canonical_text,
    iter_record_dirs,
    read_record,
    record_hash,
    split_record,
)
from snagentic.instance.sync import PENDING_FILE, InstanceSync, replace_working_records
from snagentic.instance.tableapi import TableApiClient
from snagentic.instance.updatesets import OPEN_STATE, collision_report, load_update_sets
from snagentic.policy import PolicyEnforcer
from snagentic.review.engine import ReviewTarget
from snagentic.review.gate import WAIVERS_FILE, denial_message
from snagentic.review.scan import (
    change_gate,
    customized_state,
    load_customized,
    report,
    review_changes,
)

CONFLICT_MARKER = re.compile(r"^(<{7}|={7}|>{7})( |$)", re.MULTILINE)
LABEL = re.compile(r"^[A-Za-z0-9_.-]{1,40}$")


def validate_label(label: str) -> str:
    """Update set labels are embedded in names and encoded queries: keep them simple."""

    if not LABEL.fullmatch(label):
        raise ValueError("update set labels may contain only letters, digits, '.', '_' and '-'"
                         " (at most 40 characters)")
    return label


@dataclass(frozen=True)
class PlannedChange:
    operation: str
    table: str
    scope: str
    domain: str
    path: str
    sys_id: str | None
    sys_update_name: str | None
    fields: dict[str, str]
    expected_hash: str | None

    def summary(self) -> dict[str, Any]:
        return {
            "operation": self.operation,
            "table": self.table,
            "scope": self.scope,
            "domain": self.domain,
            "path": self.path,
            "sys_id": self.sys_id,
            "sys_update_name": self.sys_update_name,
            "fields": sorted(self.fields),
            "expected_hash": self.expected_hash,
        }

    def digest_material(self) -> dict[str, Any]:
        return {
            **self.summary(),
            "values": {key: hashlib.sha256(canonical_text(value).encode()).hexdigest()
                       for key, value in sorted(self.fields.items())},
        }


def _location(relative: Path) -> tuple[str, str, str]:
    parts = relative.parts
    if parts and parts[0] == "domains":
        if len(parts) < 5:
            raise ValueError(f"record path is too short: {relative}")
        return parts[1], parts[2], parts[3]
    if len(parts) < 3:
        raise ValueError(f"record path is too short: {relative}")
    return "global", parts[0], parts[1]


def _local_record_dirs(metadata: Path) -> list[Path]:
    """Directories containing ``_meta.yaml`` or, for new records, ``record.yaml``."""

    found = set(iter_record_dirs(metadata))
    if metadata.is_dir():
        for record in metadata.rglob(RECORD_FILE):
            found.add(record.parent)
    return sorted(found)


class ChangePlanner:
    def __init__(self, paths: InstancePaths, config: InstanceConfig) -> None:
        self.paths = paths
        self.config = config
        self.mirror = MirrorRepository(paths, config.mirror_branch)
        self._changed_known = False
        self._changed_cache: list[str] | None = None

    def _changed_files(self) -> list[str] | None:
        """Repository paths under ``metadata/`` that differ from the mirror tip (tracked
        diff plus untracked files), computed once per planner."""

        if self._changed_known:
            return self._changed_cache
        tip = self.mirror.tip()
        result: list[str] | None = None
        if tip is not None:
            prefix = (self.paths.relative_workspace / "metadata").as_posix()
            root = self.paths.root
            diff = git(root, "diff", "--name-only", "-z", "--no-renames", tip, "--", prefix,
                       check=False)
            if not diff.returncode:
                untracked = [
                    path for code, path in worktree_status(root, (prefix,)) if code == "??"
                ]
                result = [p for p in diff.stdout.split("\0") if p] + untracked
        self._changed_cache, self._changed_known = result, True
        return result

    def _changed_record_dirs(self) -> tuple[set[Path], dict[Path, str]] | None:
        """Record folders (relative to ``metadata/``) that differ from the mirror tip, and
        the tip's folder -> sys_id map. ``None`` falls back to a full comparison."""

        tip = self.mirror.tip()
        changed = self._changed_files()
        if tip is None or changed is None:
            return None
        prefix = (self.paths.relative_workspace / "metadata").as_posix()
        listing = git(self.paths.root, "ls-tree", "-r", "--name-only", tip, "--", prefix,
                      check=False)
        if listing.returncode:
            return None
        candidates: set[Path] = set()
        for line in changed:
            if line.startswith(prefix + "/"):
                candidates.add(Path(line[len(prefix) + 1:]).parent)
        base_dirs: dict[Path, str] = {}
        for line in listing.stdout.splitlines():
            if line.endswith("/" + META_FILE):
                relative = Path(line[len(prefix) + 1:]).parent
                sys_id = relative.name.rpartition("--")[2]
                if SYS_ID.fullmatch(sys_id):
                    base_dirs[relative] = sys_id
        return candidates, base_dirs

    def preconditions(self) -> None:
        if self.mirror.tip() is None:
            raise ConflictError("fetch the instance before planning changes")
        if not self.mirror.is_integrated():
            raise ConflictError(
                f"integrate {self.config.mirror_branch} before planning changes "
                "(otherwise remote updates would be reverted)"
            )
        unmerged = git(self.paths.root, "diff", "--name-only", "--diff-filter=U").stdout.split()
        if unmerged:
            raise ConflictError("resolve merge conflicts first: " + ", ".join(unmerged[:10]))
        # The mirror never contains merge markers, so only files that differ from its tip
        # can; scanning just those keeps planning fast on 100k+ record instances.
        changed = self._changed_files()
        if changed is None:
            files = [p for p in self.paths.metadata.rglob("*") if p.is_file()] if (
                self.paths.metadata.is_dir()) else []
        else:
            files = [self.paths.root / line for line in changed]
        markers = [
            path.relative_to(self.paths.root).as_posix()
            for path in files
            if path.is_file() and CONFLICT_MARKER.search(path.read_text(encoding="utf-8",
                                                                          errors="ignore"))
        ]
        if markers:
            raise ConflictError("conflict markers remain in: " + ", ".join(markers[:10]))

    def plan(self, *, check: bool = True) -> dict[str, Any]:
        if check:
            self.preconditions()
        self.mirror.ensure()
        base_root = self.paths.mirror_workspace / "metadata"
        base: dict[str, LocalRecord] = {}
        scoped = self._changed_record_dirs()
        if scoped is None:
            local_dirs = _local_record_dirs(self.paths.metadata)
            for directory in iter_record_dirs(base_root):
                record = read_record(directory)
                if record.sys_id:
                    base[record.sys_id] = record
        else:
            # Only folders that differ from the mirror tip can hold changes; everything
            # else is byte-identical to the base and would produce no change.
            candidates, base_dirs = scoped
            local_dirs = [
                self.paths.metadata / relative for relative in sorted(candidates)
                if (self.paths.metadata / relative / META_FILE).is_file()
                or (self.paths.metadata / relative / RECORD_FILE).is_file()
            ]
            wanted = {base_dirs[relative] for relative in candidates if relative in base_dirs}
            by_id = {sys_id: relative for relative, sys_id in base_dirs.items()}
            for directory in local_dirs:
                sys_id = read_record(directory).sys_id
                if sys_id:
                    wanted.add(sys_id)
            for sys_id in wanted:
                if sys_id in by_id and (base_root / by_id[sys_id]).is_dir():
                    record = read_record(base_root / by_id[sys_id])
                    if record.sys_id:
                        base[record.sys_id] = record
        local_ids: set[str] = set()
        changes: list[PlannedChange] = []
        customized = load_customized(self.paths.workspace)
        review_targets: list[ReviewTarget] = []
        ignored_read_only: set[str] = set()
        read_only_tables = set(self.config.sync.operational_tables)
        for directory in local_dirs:
            relative = directory.relative_to(self.paths.metadata)
            domain, scope, table = _location(relative)
            record = read_record(directory)
            path = directory.relative_to(self.paths.root).as_posix()
            sys_id = record.sys_id
            previous = base.get(sys_id) if sys_id else None
            if (
                table in read_only_tables
                or record.meta.get("read_only") is True
                or (previous is not None and previous.meta.get("read_only") is True)
            ):
                if sys_id and previous is not None:
                    local_ids.add(sys_id)
                if scoped is not None or previous is None or record != previous:
                    ignored_read_only.add(path)
                continue
            if sys_id and sys_id in base:
                local_ids.add(sys_id)
                previous = base[sys_id]
                changed = {
                    key: value
                    for key, value in record.values.items()
                    if canonical_text(previous.values.get(key)) != canonical_text(value)
                }
                self._validate_property_value_change(table, record.values, changed, path)
                if changed:
                    changes.append(
                        PlannedChange("update", previous.table, scope, domain, path, sys_id,
                                      previous.meta.get("sys_update_name"), changed,
                                      str(previous.meta.get("hash")))
                    )
                    review_targets.append(ReviewTarget(
                        path, previous.table, scope, "update", record.values,
                        previous.values, customized_state(previous.meta, customized),
                    ))
            elif sys_id:
                raise ConflictError(
                    f"{path} references sys_id {sys_id} that is not in the mirror; "
                    "remove sys_id from _meta.yaml to create a new record"
                )
            else:
                if not record.values:
                    raise ValueError(f"new record has no fields: {path}")
                self._validate_property_value_change(
                    table, record.values, record.values, path
                )
                changes.append(
                    PlannedChange("create", table, scope, domain, path, None, None,
                                  dict(record.values), None)
                )
                review_targets.append(
                    ReviewTarget(path, table, scope, "create", record.values)
                )
        for sys_id in sorted(set(base) - local_ids):
            previous = base[sys_id]
            relative = previous.directory.relative_to(base_root)
            domain, scope, _ = _location(relative)
            path = (self.paths.relative_workspace / "metadata" / relative).as_posix()
            if previous.table in read_only_tables or previous.meta.get("read_only") is True:
                ignored_read_only.add(path)
                continue
            changes.append(
                PlannedChange("delete", previous.table, scope, domain,
                              path,
                              sys_id, previous.meta.get("sys_update_name"), {},
                              str(previous.meta.get("hash")))
            )
        changes.sort(key=lambda change: (change.operation, change.path))
        # Child rows (_children/) are derived from their owner and never written back.
        ignored = sorted(ignored_read_only | {
            (self.paths.relative_workspace / "metadata" / relative).as_posix()
            for relative in (scoped[0] if scoped else ())
            if relative.name == "_children"
        })
        plan_id = hashlib.sha256(
            canonical_json([change.digest_material() for change in changes]).encode()
        ).hexdigest()[:16]
        review_findings = review_changes(self.paths.workspace, review_targets)
        gate = change_gate(self.paths.workspace, self.paths.relative_workspace.as_posix(),
                           review_findings, plan_id)
        update_sets = load_update_sets(self.paths.mirror_workspace / "update-sets")
        collisions = collision_report(
            update_sets, local_changes=[change.summary() for change in changes]
        )
        return {
            "instance": self.config.name,
            "writable": self.config.writable,
            "plan_id": plan_id,
            "mirror_commit": self.mirror.tip(),
            "changes": [change.summary() for change in changes],
            "collisions": collisions["local_changes_in_open_update_sets"],
            **({"ignored_read_only": ignored} if ignored else {}),
            # ServiceNow best-practice findings and the apply gate; not part of plan_id.
            "review": report(review_findings, reviewed=len(review_targets), limit=100,
                             mode=gate["mode"]),
            "gate": gate,
            "_review_findings": review_findings,
            "_objects": changes,
        }

    def _validate_property_value_change(
        self,
        table: str,
        values: dict[str, str],
        changed: dict[str, str],
        path: str,
    ) -> None:
        if table != "sys_properties" or "value" not in changed:
            return
        name = values.get("name", "")
        if name not in self.config.sync.property_value_allowlist:
            raise PolicyDeniedError(
                f"{path} changes a redacted system property value; add its exact non-sensitive "
                "name to sync.property_value_allowlist before fetching and editing"
            )


class UpdateSetWriter:
    """Apply a change plan into an agent-owned update set via the Table API.

    For every scope touched by the plan, an ``in progress`` update set named
    ``snagentic: <label> [<scope>]`` is created or reused, and made current for the
    integration user through ``sys_user_preference`` (restored afterwards). Each
    write is guarded by an optimistic check of the record's canonical hash.
    """

    def __init__(
        self,
        paths: InstancePaths,
        config: InstanceConfig,
        client: TableApiClient,
        *,
        policy: PolicyEnforcer | None = None,
    ) -> None:
        self.paths = paths
        self.config = config
        self.client = client
        self.policy = policy or PolicyEnforcer()
        self.sync = InstanceSync(paths, config, client)

    def apply(
        self,
        *,
        plan_id: str,
        confirm: bool,
        label: str | None = None,
        allow_collisions: bool = False,
    ) -> dict[str, Any]:
        self.policy.require_write_allowed(self.config.kind)
        if not self.config.writable:
            raise PolicyDeniedError(f"writes are denied for {self.config.kind} instances")
        pending = self.paths.state / PENDING_FILE
        if pending.is_file():
            raise ConflictError(
                "a previous apply did not finish cleanly; run fetch + integrate to reconcile "
                f"({pending})"
            )
        planner = ChangePlanner(self.paths, self.config)
        plan = planner.plan()
        if plan["plan_id"] != plan_id:
            raise ConflictError("the plan changed since it was reviewed; review the new plan")
        changes: list[PlannedChange] = plan["_objects"]
        if not changes:
            return {"status": "no_changes", "plan_id": plan_id}
        gate = plan["gate"]
        if gate["enforced"] and not gate["passed"]:
            raise PolicyDeniedError(denial_message(
                gate, f"{self.paths.relative_workspace.as_posix()}/{WAIVERS_FILE}"
            ))
        if not confirm:
            raise PolicyDeniedError("apply requires explicit confirmation")
        if plan["collisions"] and not allow_collisions:
            raise ConflictError(
                "planned changes touch records held in other open update sets: "
                + ", ".join(item["record"] for item in plan["collisions"][:10])
            )
        catalog = self.sync.catalog(refresh=False)
        label = validate_label(label or _branch_label(self.paths.root))
        by_scope: dict[str, list[PlannedChange]] = defaultdict(list)
        for change in changes:
            by_scope[change.scope].append(change)

        user_id, user_name = self._current_user()
        applied: list[dict[str, Any]] = []
        scope_of: dict[str, str] = {}
        since: dict[str, str] = {}
        update_sets: dict[str, dict[str, str]] = {}
        self._write_pending({"plan_id": plan_id, "status": "apply_in_progress"})
        try:
            for scope, scoped_changes in sorted(by_scope.items()):
                scope_id = catalog.scope_sys_id(scope)
                if scope_id is None:
                    raise ConflictError(f"unknown application scope: {scope}")
                update_set = self._agent_update_set(label, scope, scope_id)
                update_sets[scope] = update_set
                preference = "sys_update_set" if scope_id == "global" else (
                    f"updateSetForScope{scope_id}"
                )
                previous = self._set_preference(user_id, preference, update_set["sys_id"])
                since[scope] = previous.get("written_at", "") if previous else ""
                try:
                    for change in scoped_changes:
                        item = self._apply_one(change, catalog, scope_id)
                        scope_of[item["sys_id"]] = scope
                        applied.append(item)
                        self._write_pending({"plan_id": plan_id, "status": "apply_in_progress",
                                             "applied": applied})
                finally:
                    self._restore_preference(user_id, preference, previous)
        except Exception as exc:
            self._write_pending(
                {"plan_id": plan_id, "status": "reconciliation_required",
                 "applied": applied, "error": str(exc)}
            )
            raise
        captured = self._verify_capture(update_sets, applied)
        moved: list[str] = []
        if captured:
            moved = self._adopt_uncaptured(
                update_sets, applied, scope_of, since, user_name, set(captured)
            )
            captured = self._verify_capture(update_sets, applied)
        by_table: dict[str, list[str]] = defaultdict(list)
        for item in applied:
            by_table[item["table"]].append(item["sys_id"])
        for table, ids in sorted(by_table.items()):
            self.sync.refetch(table, ids)
        self.sync.refresh_update_sets()
        mirror_commit = self.sync.commit_refetch(
            f"snagentic apply {self.config.name}: {len(applied)} change(s) from plan {plan_id}\n\n"
            + "\n".join(f"Update set [{scope}]: {item['name']} ({item['sys_id']})"
                        for scope, item in sorted(update_sets.items()))
            + "\n"
        )
        self._refresh_working_copy(applied)
        (self.paths.state / PENDING_FILE).unlink(missing_ok=True)
        return {
            "status": "applied",
            "plan_id": plan_id,
            "applied": applied,
            "update_sets": update_sets,
            "not_captured": captured,
            **({"moved_to_update_set": moved} if moved else {}),
            "mirror_commit": mirror_commit,
            "next_steps": [
                "commit your working changes",
                f"snagentic instance -i {self.config.name} integrate",
            ],
        }

    # -- internals -------------------------------------------------------------
    def _refresh_working_copy(self, applied: list[dict[str, Any]]) -> None:
        """Replace touched working-copy records with their canonical remote form so the
        next integrate merges identical content and new records are not planned twice."""

        replace_working_records(self.paths, applied)

    def _apply_one(self, change: PlannedChange, catalog: Catalog, scope_id: str) -> dict[str, Any]:
        if change.operation == "create":
            sys_id = uuid.uuid4().hex
            values = {key: canonical_text(value) for key, value in change.fields.items()
                      if key not in META_FIELDS}
            values.update({"sys_id": sys_id, "sys_scope": scope_id})
            if change.domain != "global":
                values["sys_domain"] = change.domain
            result = self.client.insert(change.table, values)
            return {"operation": "create", "table": change.table,
                    "sys_id": str(result.get("sys_id") or sys_id), "path": change.path}
        if change.sys_id is None:
            raise ConflictError(f"{change.path} has no sys_id; rebuild the change plan")
        current = self.client.get(change.table, change.sys_id)
        if current is None:
            raise ConflictError(f"{change.path} was deleted remotely; fetch and integrate")
        current.setdefault("sys_class_name", change.table)
        _, plain, text = split_record(
            current,
            catalog,
            self.config.sync.redact_fields,
            self.config.sync.property_value_allowlist,
        )
        remote_hash = record_hash(change.table, {**plain, **text})
        if remote_hash != change.expected_hash:
            raise ConflictError(f"{change.path} changed remotely since the last fetch")
        if change.operation == "delete":
            self.client.delete(change.table, change.sys_id)
        else:
            values = {key: canonical_text(value) for key, value in change.fields.items()
                      if key not in META_FIELDS}
            self.client.update(change.table, change.sys_id, values)
        return {"operation": change.operation, "table": change.table,
                "sys_id": change.sys_id, "path": change.path,
                "sys_update_name": change.sys_update_name or f"{change.table}_{change.sys_id}"}

    def _current_user(self) -> tuple[str, str]:
        rows = self.client.query(
            "sys_user",
            query="sys_id=javascript:gs.getUserID()",
            fields=["sys_id", "user_name"],
            limit=1,
        )
        if not rows:
            raise ServiceNowError("could not resolve the integration user")
        return str(rows[0]["sys_id"]), str(rows[0].get("user_name") or "")

    def _agent_update_set(self, label: str, scope: str, scope_id: str) -> dict[str, str]:
        name = f"snagentic: {label} [{scope}]"[:80]
        rows = self.client.query(
            "sys_update_set",
            query=f"name={name}^state={OPEN_STATE}^application={scope_id}",
            fields=["sys_id", "name"],
            limit=1,
        ) or []
        if rows:
            return {"sys_id": str(rows[0]["sys_id"]), "name": name, "created": "false"}
        created = self.client.insert(
            "sys_update_set",
            {
                "name": name,
                "application": scope_id,
                "state": OPEN_STATE,
                "description": "Changes applied by snagentic from a reviewed git change plan.",
            },
        )
        return {"sys_id": str(created["sys_id"]), "name": name, "created": "true"}

    def _set_preference(self, user_id: str, name: str, value: str) -> dict[str, str] | None:
        rows = self.client.query(
            "sys_user_preference",
            query=f"user={user_id}^name={name}",
            fields=["sys_id", "value"],
            limit=1,
        ) or []
        if rows:
            previous = {"sys_id": str(rows[0]["sys_id"]), "value": str(rows[0].get("value", ""))}
            written = self.client.update("sys_user_preference", previous["sys_id"],
                                         {"value": value})
            previous["written_at"] = str((written or {}).get("sys_updated_on") or "")
            return previous
        created = self.client.insert(
            "sys_user_preference", {"user": user_id, "name": name, "value": value}
        )
        return {"sys_id": str(created["sys_id"]), "value": "", "created": "true",
                "written_at": str(created.get("sys_updated_on") or "")}

    def _restore_preference(
        self, user_id: str, name: str, previous: dict[str, str] | None
    ) -> None:
        if previous is None:
            return
        if previous.get("created") == "true":
            self.client.delete("sys_user_preference", previous["sys_id"])
        else:
            self.client.update("sys_user_preference", previous["sys_id"],
                               {"value": previous["value"]})

    def _verify_capture(
        self, update_sets: dict[str, dict[str, str]], applied: list[dict[str, Any]]
    ) -> list[str]:
        ids = ",".join(item["sys_id"] for item in update_sets.values())
        rows = self.client.query(
            "sys_update_xml", query=f"update_setIN{ids}", fields=["name"], limit=10_000
        ) or []
        names = {str(row.get("name")) for row in rows}
        missing: list[str] = []
        for item in applied:
            expected = item.get("sys_update_name") or f"{item['table']}_{item['sys_id']}"
            if expected not in names:
                missing.append(str(expected))
        return missing

    def _adopt_uncaptured(
        self,
        update_sets: dict[str, dict[str, str]],
        applied: list[dict[str, Any]],
        scope_of: dict[str, str],
        since: dict[str, str],
        user_name: str,
        missing: set[str],
    ) -> list[str]:
        """Some releases ignore the current-update-set preference for REST sessions and
        capture into Default. Move only the rows this apply just produced: same update
        name, created by the integration user, written at or after the apply started, in
        another in-progress set. Moving a customer update between sets is the same
        operation as the platform's "move to update set"."""

        if not user_name:
            return []
        moved: list[str] = []
        for item in applied:
            name = str(item.get("sys_update_name") or f"{item['table']}_{item['sys_id']}")
            scope = scope_of.get(str(item["sys_id"]))
            if name not in missing or scope is None or not since.get(scope):
                continue
            target = update_sets[scope]["sys_id"]
            rows = self.client.query(
                "sys_update_xml",
                query=(
                    f"name={name}^sys_updated_by={user_name}^update_set!={target}"
                    f"^update_set.state={OPEN_STATE}^sys_updated_on>={since[scope]}"
                ),
                fields=["sys_id", "update_set"],
                limit=10,
            ) or []
            for row in rows:
                self.client.update("sys_update_xml", str(row["sys_id"]), {"update_set": target})
                moved.append(name)
        return moved

    def _write_pending(self, value: dict[str, Any]) -> None:
        self.paths.state.mkdir(parents=True, exist_ok=True)
        (self.paths.state / PENDING_FILE).write_text(json.dumps(value, indent=2, sort_keys=True))


def _branch_label(root: Path) -> str:
    branch = git(root, "rev-parse", "--abbrev-ref", "HEAD", check=False).stdout.strip()
    return re.sub(r"[^A-Za-z0-9_.-]", "-", branch or "work").strip("-.")[:40] or "work"
