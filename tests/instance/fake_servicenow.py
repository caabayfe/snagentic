"""In-memory ServiceNow Table API fake served through ``httpx.MockTransport``.

Supports the encoded-query subset snagentic emits: ``=``, ``!=``, ``>``, ``>=``,
``<``, ``<=``, ``IN``, ``NOT IN``, ``^OR``, ``^NQ``, ``ORDERBY`` and one level of
dot-walking. ``javascript:`` values match everything. Writes to tracked
(``sys_metadata``) tables are captured into the user's current update set.
"""

from __future__ import annotations

import copy
import json
import re
import uuid
from typing import Any

import httpx

OPERATORS = ("STARTSWITH", "NOT IN", ">=", "<=", "!=", "IN", ">", "<", "=")


class FakeServiceNow:
    def __init__(self) -> None:
        self.parents: dict[str, str | None] = {
            "sys_metadata": None,
            "sys_script_include": "sys_metadata",
            "sys_script": "sys_metadata",
            "sys_properties": "sys_metadata",
            "sys_db_object": "sys_metadata",
            "sys_dictionary": "sys_metadata",
            "sys_documentation": "sys_metadata",
            "sys_ws_definition": "sys_metadata",
            "sys_ws_operation": "sys_metadata",
            "sys_hub_flow": "sys_metadata",
            "sys_choice": "sys_metadata",
            "sys_security_acl": "sys_metadata",
            "sys_security_acl_role": None,
            "sys_security_operation": "sys_metadata",
            "sys_user_role": "sys_metadata",
            "sys_script_client": "sys_metadata",
            "sys_ui_policy": "sys_metadata",
            "sys_ui_policy_action": "sys_metadata",
            "sysevent_register": "sys_metadata",
            "sysevent_script_action": "sys_metadata",
            "task": None,
            "sys_scope": None,
            "sys_update_set": None,
            "sys_update_xml": None,
            "sys_metadata_delete": None,
            "sys_user": None,
            "sys_user_preference": None,
            "v_plugin": None,
            "sys_plugins": "sys_metadata",
            "sys_store_app": "sys_metadata",
            "domain": None,
            "incident": "task",
            "wf_workflow": "sys_metadata",
            "wf_workflow_version": None,
            "wf_activity": None,
            "sys_hub_action_instance_v2": None,
            "scan_check": None,
            "scan_result": None,
            "scan_finding": None,
        }
        self.records: dict[str, dict[str, Any]] = {}
        self.clock = 0
        self.requests: list[tuple[str, str, dict[str, str]]] = []
        self.progress_statuses: list[str] = ["2"]
        # Instance Scan: target sys_id -> scan_check sys_ids that report a finding on it.
        self.scan_findings: dict[str, list[str]] = {}
        self.cicd_bodies: list[tuple[str, Any]] = []
        # sys_ids hidden by "ACLs": dropped after the limit, as the real Table API does.
        self.hidden: set[str] = set()
        self.denied_tables: set[str] = set()
        self.user_id = self.insert(
            "sys_user", {"user_name": "integration", "name": "Integration"}, track=False
        )
        self.scope_global = "global"
        self.records["global"] = {
            "sys_id": "global", "sys_class_name": "sys_scope", "scope": "global",
        }
        for table, parent in self.parents.items():
            if table not in {"sys_user"}:
                self.insert(
                    "sys_db_object",
                    {"name": table, "super_class": "", "_parent_name": parent or ""},
                    track=False,
                    by="maint",
                )
        by_name = {
            row["name"]: row["sys_id"]
            for row in self.records.values()
            if row["sys_class_name"] == "sys_db_object"
        }
        for row in list(self.records.values()):
            if row["sys_class_name"] == "sys_db_object":
                row["super_class"] = by_name.get(row.pop("_parent_name"), "")
        # Tracked changes made without a chosen update set land in Default.
        self.default_update_set = self.insert(
            "sys_update_set",
            {"name": "Default", "state": "in progress", "application": "global",
             "is_default": "true"},
            track=False,
            by="maint",
        )

    # -- data helpers ------------------------------------------------------------
    def now(self) -> str:
        self.clock += 1
        minutes, seconds = divmod(self.clock, 60)
        hours, minutes = divmod(minutes, 60)
        return f"2026-09-20 {10 + hours:02d}:{minutes:02d}:{seconds:02d}"

    def insert(
        self,
        table: str,
        values: dict[str, Any],
        *,
        track: bool = True,
        by: str = "developer",
        sys_id: str | None = None,
    ) -> str:
        sys_id = sys_id or values.get("sys_id") or uuid.uuid4().hex
        timestamp = self.now()
        record = {
            "sys_id": sys_id,
            "sys_class_name": table,
            "sys_created_on": timestamp,
            "sys_updated_on": timestamp,
            "sys_created_by": by,
            "sys_updated_by": by,
            "sys_mod_count": "0",
        }
        if self.is_metadata(table):
            record.setdefault("sys_scope", "global")
            record["sys_update_name"] = f"{table}_{sys_id}"
        record.update({key: str(value) for key, value in values.items() if key != "sys_id"})
        self.records[sys_id] = record
        if track and self.is_metadata(table):
            self.capture(record, "INSERT_OR_UPDATE", by)
        return sys_id

    def update(self, sys_id: str, values: dict[str, Any], *, by: str = "developer") -> None:
        record = self.records[sys_id]
        record.update({key: str(value) for key, value in values.items()})
        record["sys_mod_count"] = str(int(record["sys_mod_count"]) + 1)
        record["sys_updated_on"] = self.now()
        record["sys_updated_by"] = by
        if self.is_metadata(record["sys_class_name"]):
            self.capture(record, "INSERT_OR_UPDATE", by)

    def delete(self, sys_id: str, *, by: str = "developer") -> None:
        record = self.records.pop(sys_id)
        if self.is_metadata(record["sys_class_name"]):
            self.capture(record, "DELETE", by)
            self.insert(
                "sys_metadata_delete",
                {"sys_update_name": record["sys_update_name"], "name": record.get("name", "")},
                track=False,
                by=by,
            )

    def create_scope(self, namespace: str) -> str:
        return self.insert("sys_scope", {"scope": namespace, "name": namespace}, track=False)

    def open_update_set(self, name: str, *, by: str = "developer", scope: str = "global") -> str:
        sys_id = self.insert(
            "sys_update_set",
            {"name": name, "state": "in progress", "application": scope},
            track=False,
            by=by,
        )
        self.set_current(by, sys_id, scope)
        return sys_id

    def set_current(self, user: str, update_set: str, scope: str = "global") -> None:
        self._current[(user, scope)] = update_set

    @property
    def _current(self) -> dict[tuple[str, str], str]:
        if not hasattr(self, "_current_sets"):
            self._current_sets: dict[tuple[str, str], str] = {}
        return self._current_sets

    def capture(self, record: dict[str, Any], action: str, by: str) -> None:
        scope = record.get("sys_scope", "global")
        update_set = self._current.get((by, scope))
        if by == "integration" and not getattr(self, "ignore_preferences", False):
            preference = self._integration_preference(scope)
            update_set = preference or update_set
        update_set = update_set or getattr(self, "default_update_set", None)
        if not update_set:
            return
        name = record["sys_update_name"]
        for existing in self.records.values():
            if (
                existing["sys_class_name"] == "sys_update_xml"
                and existing.get("update_set") == update_set
                and existing.get("name") == name
            ):
                existing.update({"action": action, "sys_updated_on": self.now(),
                                 "sys_updated_by": by})
                return
        self.insert(
            "sys_update_xml",
            {
                "name": name,
                "type": record["sys_class_name"],
                "target_name": record.get("name", ""),
                "action": action,
                "update_set": update_set,
                "application": scope,
            },
            track=False,
            by=by,
        )

    def _integration_preference(self, scope: str) -> str | None:
        wanted = "sys_update_set" if scope == "global" else f"updateSetForScope{scope}"
        for record in self.records.values():
            if (
                record["sys_class_name"] == "sys_user_preference"
                and record.get("user") == self.user_id
                and record.get("name") == wanted
            ):
                return str(record.get("value"))
        return None

    def is_metadata(self, table: str) -> bool:
        current: str | None = table
        while current:
            if current == "sys_metadata":
                return True
            current = self.parents.get(current)
        return False

    def in_table(self, record: dict[str, Any], table: str) -> bool:
        current: str | None = record["sys_class_name"]
        while current:
            if current == table:
                return True
            current = self.parents.get(current)
        return False

    # -- query engine ------------------------------------------------------------
    def select(self, table: str, query: str) -> list[dict[str, Any]]:
        groups = query.split("^NQ") if query else [""]
        order: list[str] = []
        clauses: list[list[list[str]]] = []
        for group in groups:
            terms = [term for term in group.split("^") if term]
            conjunction: list[list[str]] = []
            for term in terms:
                if term.startswith("ORDERBY"):
                    order.append(term[len("ORDERBY"):])
                elif term.startswith("OR") and conjunction:
                    conjunction[-1].append(term[2:])
                else:
                    conjunction.append([term])
            clauses.append(conjunction)
        rows = [
            record
            for record in self.records.values()
            if self.in_table(record, table)
            and any(
                all(any(self.match(record, term) for term in alternatives)
                    for alternatives in conjunction)
                for conjunction in clauses
            )
        ]
        for field in reversed(order or ["sys_id"]):
            rows.sort(key=lambda row, f=field: str(row.get(f, "")))
        return rows

    def match(self, record: dict[str, Any], term: str) -> bool:
        for operator in OPERATORS:
            match = re.fullmatch(rf"([A-Za-z0-9_.]+){re.escape(operator)}(.*)", term)
            if match:
                field, value = match.groups()
                break
        else:
            raise ValueError(f"unsupported query term: {term}")
        if value.startswith("javascript:"):
            if "getUserID" in value:
                value = self.user_id
            else:
                return True
        actual = self.resolve(record, field)
        if operator == "=":
            return actual == value
        if operator == "!=":
            return actual != value
        if operator == "STARTSWITH":
            return actual.startswith(value)
        if operator == "IN":
            return actual in value.split(",")
        if operator == "NOT IN":
            return actual not in value.split(",")
        if operator == ">":
            return actual > value
        if operator == ">=":
            return actual >= value
        if operator == "<":
            return actual < value
        return actual <= value

    def resolve(self, record: dict[str, Any], field: str) -> str:
        if "." in field:
            reference, sub = field.split(".", 1)
            target = self.records.get(str(record.get(reference, "")))
            return str(target.get(sub, "")) if target else ""
        return str(record.get(field, ""))

    # -- HTTP ------------------------------------------------------------------
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    def handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        params = dict(request.url.params)
        self.requests.append((request.method, path, params))
        match = re.fullmatch(r"/api/now/table/([A-Za-z0-9_$]+)(?:/([0-9a-f]{32}))?", path)
        if match:
            table, sys_id = match.groups()
            return self.table_request(request, table, sys_id, params)
        match = re.fullmatch(r"/api/sn_cicd/(.+)", path)
        if match:
            if match.group(1).startswith("progress/"):
                status = (self.progress_statuses.pop(0) if len(self.progress_statuses) > 1
                          else self.progress_statuses[0])
                return httpx.Response(200, json={"result": {
                    "status": status, "status_message": "done" if status == "2" else "working",
                }})
            body = json.loads(request.content) if request.content else None
            self.cicd_bodies.append((match.group(1), body))
            progress_id = "p1"
            if match.group(1).startswith("instance_scan/"):
                progress_id = self.run_scan(params, body)
            return httpx.Response(
                200, json={"result": {"links": {"progress": {"id": progress_id}},
                                      "status": "0", "status_label": "Pending",
                                      "path": match.group(1)}}
            )
        return httpx.Response(404, json={"error": {"message": "not found"}})

    def run_scan(self, params: dict[str, str], body: Any) -> str:
        progress_id = f"{len(self.cicd_bodies):032x}"
        result = self.insert("scan_result", {"progress_id": progress_id, "state": "complete",
                                             "number": f"SR{len(self.cicd_bodies):05d}"},
                             track=False)
        targets = [params["target_sys_id"]] if "target_sys_id" in params else [
            row["name"].rsplit("_", 1)[-1]
            for update_set in (body or {}).get("update_set_sys_ids", [])
            for row in self.select("sys_update_xml", f"update_set={update_set}")
        ]
        for target in targets:
            record = self.records.get(target, {})
            for check in self.scan_findings.get(target, []):
                self.insert("scan_finding", {
                    "result": result, "check": check, "source": target,
                    "source_table": record.get("sys_class_name", ""),
                    "finding_details": "found by fake scan", "count": "1", "muted": "false",
                }, track=False)
        return progress_id

    def table_request(
        self, request: httpx.Request, table: str, sys_id: str | None, params: dict[str, str]
    ) -> httpx.Response:
        if table not in self.parents:
            return httpx.Response(400, json={"error": {"message": "Invalid table"}})
        if table in self.denied_tables:
            return httpx.Response(403, json={"error": {"message": "Access denied"}})
        if request.method == "GET":
            rows = self.select(table, params.get("sysparm_query", ""))
            offset = int(params.get("sysparm_offset", 0))
            limit = int(params.get("sysparm_limit", 10000))
            rows = [row for row in rows[offset : offset + limit]
                    if row["sys_id"] not in self.hidden]
            fields = params.get("sysparm_fields")
            result = [
                {key: self.resolve(row, key) if "." in key else row.get(key, "")
                 for key in fields.split(",")} if fields
                else {key: value for key, value in row.items() if not key.startswith("_")}
                for row in rows
            ]
            return httpx.Response(200, json={"result": copy.deepcopy(result)})
        body = json.loads(request.content or b"{}")
        if request.method == "POST":
            new_id = self.insert(table, body, by="integration")
            return httpx.Response(201, json={"result": copy.deepcopy(self.records[new_id])})
        if sys_id is None or sys_id not in self.records:
            return httpx.Response(404, json={"error": {"message": "No Record found"}})
        if request.method == "PATCH":
            self.update(sys_id, body, by="integration")
            return httpx.Response(200, json={"result": copy.deepcopy(self.records[sys_id])})
        if request.method == "DELETE":
            self.delete(sys_id, by="integration")
            return httpx.Response(204)
        return httpx.Response(405)
