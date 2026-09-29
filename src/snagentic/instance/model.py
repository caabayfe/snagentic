"""Derived, read-only table model: one ``model/tables/<table>.yaml`` per table.

Each file answers "how does this table behave?" in one read: its fields and choices,
inheritance chain, and every business rule, client script, UI policy (with actions),
UI action, ACL (with roles), notification, data policy, event and module that targets
it, with paths to the mirrored records that hold the code. Behaviour inherited from
parent tables is summarized with pointers to the parent's model file.

Schema rows (``sys_db_object``, ``sys_dictionary``, ``sys_choice``,
``sys_security_acl_role``) and customer updates are cached under
``.snagentic/<instance>/model-cache.json`` and refreshed incrementally; behaviour
summaries are derived from the mirrored records and refreshed when a record's
``sys_mod_count`` or ``sys_updated_on`` changes.
"""

from __future__ import annotations

import contextlib
import json
import re
from collections import defaultdict
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

import yaml

from snagentic.instance.catalog import Catalog
from snagentic.instance.records import META_FILE, RECORD_FILE, atomic_write, load_yaml
from snagentic.instance.updatesets import update_name_sys_id

CACHE_FILE = "model-cache.json"
CACHE_VERSION = 1
MODEL_DIRECTORY = "model"
TABLE_NAME = re.compile(r"^[a-z0-9_]{1,80}$")

TABLE_FIELDS = ["sys_id", "name", "label", "super_class", "sys_scope", "is_extendable",
                "sys_updated_on"]
DICTIONARY_FIELDS = ["sys_id", "name", "element", "internal_type", "column_label", "reference",
                     "max_length", "mandatory", "active", "default_value", "read_only",
                     "display", "choice", "dependent", "sys_updated_on"]
CHOICE_FIELDS = ["sys_id", "name", "element", "value", "label", "sequence", "inactive",
                 "language", "sys_updated_on"]
ACL_ROLE_FIELDS = ["sys_id", "sys_security_acl", "sys_user_role.name", "sys_updated_on"]
UPDATE_FIELDS = ["sys_id", "name", "type", "target_name", "action", "sys_updated_on",
                 "sys_updated_by"]


@dataclass(frozen=True)
class Behaviour:
    section: str
    table_field: str
    fields: tuple[str, ...]


BEHAVIOUR: Mapping[str, Behaviour] = {
    "sys_script": Behaviour(
        "business_rules", "collection",
        ("name", "when", "order", "active", "action_insert", "action_update",
         "action_delete", "action_query", "advanced", "condition", "filter_condition",
         "abort_action"),
    ),
    "sys_script_client": Behaviour(
        "client_scripts", "table",
        ("name", "type", "field_name", "active", "ui_type", "global", "view", "isolate_script"),
    ),
    "sys_ui_policy": Behaviour(
        "ui_policies", "table",
        ("short_description", "conditions", "active", "order", "on_load", "reverse_if_false",
         "run_scripts", "global", "view"),
    ),
    "sys_ui_policy_action": Behaviour(
        "ui_policy_actions", "table",
        ("ui_policy", "field", "visible", "mandatory", "disabled", "cleared"),
    ),
    "sys_ui_action": Behaviour(
        "ui_actions", "table",
        ("name", "action_name", "active", "order", "form_button", "form_link",
         "form_context_menu", "list_button", "list_choice", "list_context_menu", "client",
         "condition", "onclick"),
    ),
    "sys_security_acl": Behaviour(
        "acls", "name",
        ("name", "operation", "type", "active", "admin_overrides", "advanced", "condition"),
    ),
    "sysevent_email_action": Behaviour(
        "notifications", "collection",
        ("name", "event_name", "action_insert", "action_update", "condition", "active",
         "generation_type"),
    ),
    "sys_data_policy2": Behaviour(
        "data_policies", "model_table",
        ("short_description", "conditions", "active", "apply_import_set", "enforce_ui"),
    ),
    "sys_dictionary_override": Behaviour(
        "dictionary_overrides", "name",
        ("element", "base_table", "default_value_override", "default_value",
         "mandatory_override", "mandatory", "read_only_override", "read_only",
         "reference_qual_override", "reference_qual"),
    ),
    "sysevent_register": Behaviour(
        "events", "table", ("event_name", "description", "fired_by"),
    ),
    "sysevent_script_action": Behaviour(
        "script_actions", "", ("name", "event_name", "active", "order", "condition_script"),
    ),
    "sys_security_operation": Behaviour("operations", "", ("name",)),
    "sys_app_module": Behaviour(
        "modules", "name", ("title", "application", "link_type", "filter", "view_name", "active"),
    ),
    "sys_transform_map": Behaviour(
        "transform_maps", "target_table", ("name", "source_table", "active",
                                           "run_business_rules"),
    ),
    "contract_sla": Behaviour(
        "slas", "collection", ("name", "duration", "start_condition", "stop_condition", "active"),
    ),
    "sysrule_assignment": Behaviour(
        "assignment_rules", "table", ("name", "condition", "active", "order"),
    ),
}
SECTION_ORDER = (
    "business_rules", "client_scripts", "ui_policies", "ui_actions", "acls", "notifications",
    "data_policies", "dictionary_overrides", "events", "slas", "assignment_rules",
    "transform_maps", "modules",
)


class Reader(Protocol):
    def query(
        self,
        table: str,
        *,
        query: str = "",
        fields: list[str] | None = None,
        limit: int | None = None,
        offset: int = 0,
        allow_missing: bool = False,
    ) -> list[dict[str, Any]] | None: ...


class ModelBuilder:
    def __init__(
        self,
        client: Reader,
        state: Path,
        workspace: Path,
        catalog: Catalog,
        *,
        page_size: int = 500,
        progress: Callable[[str], None] | None = None,
    ) -> None:
        self.client = client
        self.cache_path = state / CACHE_FILE
        self.workspace = workspace
        self.catalog = catalog
        self.page_size = page_size
        self.progress = progress or (lambda _message: None)

    # -- refresh -------------------------------------------------------------------
    def refresh(
        self, *, start: str | None, index: Mapping[str, Mapping[str, Any]]
    ) -> dict[str, int]:
        cache = self._load_cache()
        full = start is None or not cache
        if full:
            cache = {"version": CACHE_VERSION}
        since = None if full else start
        for key, table, fields in (
            ("tables", "sys_db_object", TABLE_FIELDS),
            ("dictionary", "sys_dictionary", DICTIONARY_FIELDS),
            ("choices", "sys_choice", CHOICE_FIELDS),
            ("acl_roles", "sys_security_acl_role", ACL_ROLE_FIELDS),
            ("customer_updates", "sys_update_xml", UPDATE_FIELDS),
        ):
            rows: dict[str, dict[str, str]] = cache.setdefault(key, {})
            count = 0
            for row in self._keyset(table, fields, since):
                rows[str(row["sys_id"])] = {name: str(row.get(name) or "") for name in fields
                                            if name != "sys_id"}
                count += 1
            self.progress(f"model: {table} {count} rows {'loaded' if full else 'changed'}")
        self._refresh_behaviour(cache, index)
        self._save_cache(cache)
        written = self.write(cache, index)
        return {
            "tables": written,
            "fields": len(cache["dictionary"]),
            "behaviour": len(cache["behaviour"]),
            "customer_updates": len(cache["customer_updates"]),
        }

    def _refresh_behaviour(
        self, cache: dict[str, Any], index: Mapping[str, Mapping[str, Any]]
    ) -> None:
        behaviour: dict[str, dict[str, Any]] = cache.setdefault("behaviour", {})
        wanted: dict[str, Mapping[str, Any]] = {}
        for sys_id, entry in index.items():
            if self._behaviour_class(str(entry.get("sys_class_name"))):
                wanted[sys_id] = entry
        for sys_id in set(behaviour) - set(wanted):
            del behaviour[sys_id]
        refreshed = 0
        for sys_id, entry in wanted.items():
            version = f"{entry.get('sys_mod_count')}|{entry.get('sys_updated_on')}"
            cached = behaviour.get(sys_id)
            if cached and cached.get("_version") == version:
                continue
            summary = self._summarize(Path(entry["directory"]), str(entry["sys_class_name"]))
            if summary is None:
                behaviour.pop(sys_id, None)
                continue
            summary["_version"] = version
            behaviour[sys_id] = summary
            refreshed += 1
        self.progress(f"model: {refreshed} behaviour records summarized")

    def _behaviour_class(self, table: str) -> str | None:
        for ancestor in self.catalog.ancestors(table):
            if ancestor in BEHAVIOUR:
                return ancestor
        return None

    def _summarize(self, directory: Path, table: str) -> dict[str, Any] | None:
        kind = self._behaviour_class(table)
        if kind is None or not (directory / RECORD_FILE).is_file():
            return None
        spec = BEHAVIOUR[kind]
        values = load_yaml(directory / RECORD_FILE)
        meta = load_yaml(directory / META_FILE)
        summary: dict[str, Any] = {"kind": kind, "class": table}
        for name in dict.fromkeys((spec.table_field, *spec.fields)):
            if not name:
                continue
            value = str(values.get(name) or "")
            if value:
                summary[name] = value
        try:
            summary["path"] = directory.relative_to(self.workspace).as_posix()
        except ValueError:
            summary["path"] = directory.as_posix()
        files = meta.get("files") if isinstance(meta.get("files"), dict) else {}
        if files:
            summary["files"] = sorted(str(name) for name in files.values())
        summary["scope"] = str(meta.get("scope") or "global")
        summary["update_name"] = str(meta.get("sys_update_name") or "")
        return summary

    # -- rendering -------------------------------------------------------------------
    def write(self, cache: Mapping[str, Any], index: Mapping[str, Mapping[str, Any]]) -> int:
        tables = self.build(cache, index)
        directory = self.workspace / MODEL_DIRECTORY / "tables"
        directory.mkdir(parents=True, exist_ok=True)
        expected: set[str] = set()
        for name, model in tables.items():
            filename = f"{name}.yaml"
            expected.add(filename)
            content = yaml.safe_dump(model, sort_keys=False, allow_unicode=False, width=100)
            path = directory / filename
            if not path.is_file() or path.read_text(encoding="utf-8") != content:
                atomic_write(path, content)
        for path in directory.glob("*.yaml"):
            if path.name not in expected:
                path.unlink()
        customized = self._customer_updates(cache, index)
        atomic_write(
            self.workspace / MODEL_DIRECTORY / "customer-updates.yaml",
            yaml.safe_dump({"customer_updates": customized}, sort_keys=False, width=100),
        )
        atomic_write(self.workspace / MODEL_DIRECTORY / "README.md", MODEL_README)
        return len(tables)

    def build(
        self, cache: Mapping[str, Any], index: Mapping[str, Mapping[str, Any]]
    ) -> dict[str, dict[str, Any]]:
        table_rows: dict[str, dict[str, str]] = cache.get("tables", {})
        names_by_id = {sys_id: row["name"] for sys_id, row in table_rows.items()}
        parents: dict[str, str | None] = {}
        info: dict[str, dict[str, str]] = {}
        for row in table_rows.values():
            name = row.get("name", "")
            if not TABLE_NAME.fullmatch(name):
                continue
            info[name] = row
            parents[name] = names_by_id.get(row.get("super_class", "")) or None
        children: dict[str, list[str]] = defaultdict(list)
        for name, parent in parents.items():
            if parent:
                children[parent].append(name)

        fields: dict[str, dict[str, dict[str, Any]]] = defaultdict(dict)
        for row in cache.get("dictionary", {}).values():
            table, element = row.get("name", ""), row.get("element", "")
            if table in info and element:
                fields[table][element] = _field(row)
        for row in cache.get("choices", {}).values():
            if row.get("language", "en") not in ("", "en") or row.get("inactive") == "true":
                continue
            entry = fields.get(row.get("name", ""), {}).get(row.get("element", ""))
            if entry is not None:
                entry.setdefault("choices", {})[row.get("value", "")] = row.get("label", "")

        customized = {
            str(row.get("name")) for row in cache.get("customer_updates", {}).values()
        }
        behaviour: dict[str, dict[str, list[dict[str, Any]]]] = defaultdict(
            lambda: defaultdict(list)
        )
        summaries: dict[str, dict[str, Any]] = cache.get("behaviour", {})
        operations = {
            sys_id: item.get("name", "")
            for sys_id, item in summaries.items() if item.get("kind") == "sys_security_operation"
        }
        acl_roles: dict[str, list[str]] = defaultdict(list)
        for row in cache.get("acl_roles", {}).values():
            role = row.get("sys_user_role.name", "")
            if role:
                acl_roles[row.get("sys_security_acl", "")].append(role)
        script_actions: dict[str, list[dict[str, Any]]] = defaultdict(list)
        policy_actions: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for sys_id, item in sorted(summaries.items()):
            kind = str(item.get("kind"))
            entry = _entry(sys_id, item, customized)
            if kind == "sysevent_script_action":
                script_actions[str(item.get("event_name", ""))].append(entry)
                continue
            if kind == "sys_ui_policy_action":
                policy_actions[str(item.get("ui_policy", ""))].append(entry)
                continue
            if kind == "sys_security_operation":
                continue
            spec = BEHAVIOUR[kind]
            target = str(item.get(spec.table_field, ""))
            if kind == "sys_security_acl":
                table, _, field = target.partition(".")
                target = table
                if field:
                    entry["field"] = field
                if entry.get("operation") in operations:
                    entry["operation"] = operations[entry["operation"]]
                roles = sorted(set(acl_roles.get(sys_id, [])))
                if roles:
                    entry["roles"] = roles
            if target in info:
                behaviour[target][spec.section].append(entry)
        for sections in behaviour.values():
            for policy in sections.get("ui_policies", []):
                actions = policy_actions.get(policy["sys_id"])
                if actions:
                    policy["actions"] = [
                        {key: value for key, value in action.items() if key != "ui_policy"}
                        for action in actions
                    ]
            for event in sections.get("events", []):
                handlers = script_actions.get(str(event.get("event_name", "")))
                if handlers:
                    event["script_actions"] = handlers
            for entries in sections.values():
                entries.sort(key=_sort_key)

        models: dict[str, dict[str, Any]] = {}
        for name in sorted(info):
            chain = _ancestors(name, parents)
            row = info[name]
            model: dict[str, Any] = {
                "table": name,
                "label": row.get("label", ""),
                "scope": self.catalog.scope_namespace(row.get("sys_scope")),
            }
            if chain:
                model["extends"] = chain
            if children.get(name):
                model["extended_by"] = sorted(children[name])
            model["fields"] = dict(sorted(fields.get(name, {}).items()))
            own = behaviour.get(name, {})
            model["behaviour"] = {
                section: own[section] for section in SECTION_ORDER if own.get(section)
            }
            inherited = {}
            for ancestor in chain:
                counts = {
                    section: len(behaviour[ancestor][section])
                    for section in SECTION_ORDER
                    if behaviour.get(ancestor, {}).get(section)
                }
                own_fields = len(fields.get(ancestor, {}))
                if counts or own_fields:
                    inherited[ancestor] = {
                        "model": f"{ancestor}.yaml",
                        "fields": own_fields,
                        **counts,
                    }
            if inherited:
                model["inherited"] = inherited
            models[name] = model
        return models

    def _customer_updates(
        self, cache: Mapping[str, Any], index: Mapping[str, Mapping[str, Any]]
    ) -> list[dict[str, str]]:
        latest: dict[str, dict[str, str]] = {}
        for row in cache.get("customer_updates", {}).values():
            name = row.get("name", "")
            if name and row.get("sys_updated_on", "") >= latest.get(name, {}).get(
                "sys_updated_on", ""
            ):
                latest[name] = row
        result: list[dict[str, str]] = []
        for name, row in sorted(latest.items()):
            entry = {
                "name": name,
                "type": row.get("type", ""),
                "target": row.get("target_name", ""),
                "action": row.get("action", ""),
                "updated_on": row.get("sys_updated_on", ""),
                "updated_by": row.get("sys_updated_by", ""),
            }
            sys_id = update_name_sys_id(name)
            record = index.get(sys_id or "")
            if record:
                with contextlib.suppress(ValueError):
                    entry["path"] = Path(record["directory"]).relative_to(
                        self.workspace
                    ).as_posix()
            result.append({key: value for key, value in entry.items() if value})
        return result

    # -- io --------------------------------------------------------------------------
    def _keyset(
        self, table: str, fields: list[str], since: str | None
    ) -> Iterator[dict[str, Any]]:
        order = "^ORDERBYsys_updated_on^ORDERBYsys_id"
        cursor: tuple[str, str] | None = None
        while True:
            if cursor is None:
                query = (f"sys_updated_on>={since}" if since else "") + order
                query = query.lstrip("^")
            else:
                stamp, sys_id = cursor
                query = (f"sys_updated_on>{stamp}^NQsys_updated_on={stamp}"
                         f"^sys_id>{sys_id}{order}")
            page = self.client.query(table, query=query, fields=fields, limit=self.page_size,
                                     allow_missing=True) or []
            if not page:
                return
            yield from page
            last = page[-1]
            next_cursor = (str(last.get("sys_updated_on")), str(last.get("sys_id")))
            if next_cursor == cursor:
                return
            cursor = next_cursor

    def _load_cache(self) -> dict[str, Any]:
        if not self.cache_path.is_file():
            return {}
        try:
            raw = json.loads(self.cache_path.read_text(encoding="utf-8"))
        except ValueError:
            return {}
        if not isinstance(raw, dict) or raw.get("version") != CACHE_VERSION:
            return {}
        return raw

    def _save_cache(self, cache: Mapping[str, Any]) -> None:
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.cache_path.with_suffix(".tmp")
        temporary.write_text(json.dumps(cache, sort_keys=True), encoding="utf-8")
        temporary.replace(self.cache_path)


def _field(row: Mapping[str, str]) -> dict[str, Any]:
    entry: dict[str, Any] = {"type": row.get("internal_type", "")}
    for key, name in (("column_label", "label"), ("reference", "reference"),
                      ("max_length", "max_length"), ("default_value", "default")):
        value = row.get(key, "")
        if value:
            entry[name] = value
    for flag in ("mandatory", "read_only", "display"):
        if row.get(flag) == "true":
            entry[flag] = True
    if row.get("active") == "false":
        entry["active"] = False
    return entry


def _entry(sys_id: str, item: Mapping[str, Any], customized: set[str]) -> dict[str, Any]:
    entry: dict[str, Any] = {"sys_id": sys_id}
    for key, value in item.items():
        if key in {"kind", "class", "_version", "update_name", "scope"}:
            continue
        entry[key] = value
    if item.get("scope") and item.get("scope") != "global":
        entry["scope"] = item["scope"]
    if item.get("update_name") in customized:
        entry["customized"] = True
    return entry


def _sort_key(entry: Mapping[str, Any]) -> tuple[int, str]:
    order = str(entry.get("order") or "0")
    try:
        number = int(float(order))
    except ValueError:
        number = 0
    return number, str(entry.get("name") or entry.get("short_description") or entry["sys_id"])


def _ancestors(name: str, parents: Mapping[str, str | None]) -> list[str]:
    chain: list[str] = []
    current = parents.get(name)
    while current and current not in chain and current != name:
        chain.append(current)
        current = parents.get(current)
    return chain


MODEL_README = """# Table model (generated)

Generated by `snagentic instance fetch`; do not edit. Each `tables/<table>.yaml` lists:

- `extends` / `extended_by`: the inheritance chain. Fields and behaviour of parent tables
  also apply; `inherited` counts them and points at the parent's model file.
- `fields`: the table's own dictionary entries (type, label, reference, choices).
- `behaviour`: business rules, client scripts, UI policies (with actions), UI actions,
  ACLs (with roles; `field` for field ACLs), notifications, data policies, dictionary
  overrides, events (with script actions), SLAs, assignment rules, transform maps and
  modules that target the table. `path` points at the mirrored record under
  `metadata/`; `files` lists its exploded code files. `customized: true` marks records
  with customer updates (see `customer-updates.yaml`).

Edit behaviour through the records under `metadata/`, never here.
"""
