"""Functional (behaviour) documentation per table, rendered from the derived table model.

For each documented table the page explains what the platform does, in execution order:
queries, form load, field changes, submit, server-side insert/update/delete processing,
available actions, access control, notifications, SLAs and flows. Every entry links to the
mirrored record, and script effects (fields set, events fired, script includes called,
tables queried, aborts) are extracted statically from the mirrored code.
"""

from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Callable, Iterable, Mapping
from pathlib import Path
from typing import Any

import yaml

_LOADER: Any = getattr(yaml, "CSafeLoader", yaml.SafeLoader)

SERVER_SETS = re.compile(
    r"current\.(?!setValue|update|insert|deleteRecord|setAbortAction|addQuery|get\b|"
    r"getValue|isValid|operation|changes|setWorkflow)([a-z_][a-z0-9_]*)\s*=(?!=)"
    r"|current\.setValue\(\s*['\"]([a-z0-9_]+)"
)
EVENTS = re.compile(r"gs\.eventQueue(?:Scheduled)?\(\s*['\"]([\w.\-]+)")
SCRIPT_INCLUDES = re.compile(r"new\s+(?:global\.)?([A-Z][A-Za-z0-9_]+)\s*\(")
IGNORED_CLASSES = frozenset({"GlideRecord", "GlideRecordSecure", "GlideAggregate", "GlideDateTime",
                             "GlideDuration", "GlideAjax", "Date", "Array", "Object", "RegExp",
                             "Error", "GlideSysAttachment", "GlideFilter", "GlideDate",
                             "GlideTime", "GlideElement", "JSON", "Packages", "String"})
QUERIES = re.compile(r"new\s+Glide(?:Record|RecordSecure|Aggregate)\(\s*['\"]([a-z0-9_]+)")
MESSAGES = re.compile(r"gs\.add(Error|Info)Message\(")
CLIENT_EFFECTS = re.compile(
    r"g_form\.(setValue|setMandatory|setVisible|setDisplay|setReadOnly|setDisabled|"
    r"clearValue|addOption|removeOption|showFieldMsg|addErrorMessage|addInfoMessage)"
    r"\(\s*['\"]?([a-z0-9_]*)"
)
AJAX = re.compile(r"new\s+GlideAjax\(\s*['\"]([\w.]+)")
LEADING_COMMENT = re.compile(r"^\s*(?:/\*+(.*?)\*/|((?:\s*//[^\n]*\n)+))", re.DOTALL)
OPERATORS = (
    ("NOT IN", "not in"), ("NOTLIKE", "does not contain"), ("NOT LIKE", "does not contain"),
    ("ISNOTEMPTY", "is not empty"), ("ISEMPTY", "is empty"), ("STARTSWITH", "starts with"),
    ("ENDSWITH", "ends with"), ("VALCHANGES", "changes"), ("CHANGESFROM", "changes from"),
    ("CHANGESTO", "changes to"), ("LIKE", "contains"), (">=", ">="), ("<=", "<="),
    ("!=", "!="), ("IN", "in"), (">", ">"), ("<", "<"), ("=", "="),
)


def readable_query(query: str) -> str:
    """Render an encoded query (``priority=1^ORstate=2^EQ``) as plain text."""

    if not query:
        return ""
    groups: list[str] = []
    for group in query.replace("^EQ", "").split("^NQ"):
        terms: list[str] = []
        for index, term in enumerate(group.split("^")):
            if not term:
                continue
            joiner = " OR " if term.startswith("OR") and index else " AND "
            term = term[2:] if term.startswith("OR") and index else term
            if term.startswith("ORDERBY"):
                continue
            for token, text in OPERATORS:
                field, sep, value = term.partition(token)
                if sep and re.fullmatch(r"[a-z0-9_.]+", field):
                    rendered = f"{field} {text}" + (f" {value}" if value else "")
                    break
            else:
                rendered = term
            terms.append((joiner if terms else "") + rendered)
        if terms:
            groups.append("".join(terms))
    return " — or — ".join(groups)


def script_effects(script: str, *, client: bool = False) -> list[str]:
    effects: list[str] = []
    comment = LEADING_COMMENT.match(script)
    if comment:
        text = " ".join(
            line.strip(" */").strip() for line in (comment.group(1) or comment.group(2)).split("\n")
        ).strip()
        text = re.sub(r"\s+", " ", text.replace("//", " ")).strip()
        if text:
            effects.append("notes: " + (text[:200] + "…" if len(text) > 200 else text))
    if client:
        by_kind: dict[str, set[str]] = defaultdict(set)
        for kind, field in CLIENT_EFFECTS.findall(script):
            by_kind[kind].add(field or "(message)")
        for kind in sorted(by_kind):
            effects.append(f"{kind}: " + ", ".join(f"`{f}`" for f in sorted(by_kind[kind])))
        ajax = sorted(set(AJAX.findall(script)))
        if ajax:
            effects.append("calls server (GlideAjax): " + ", ".join(f"`{a}`" for a in ajax))
        return effects
    fields = sorted({a or b for a, b in SERVER_SETS.findall(script)})
    if fields:
        effects.append("sets " + ", ".join(f"`{f}`" for f in fields))
    events = sorted(set(EVENTS.findall(script)))
    if events:
        effects.append("fires events " + ", ".join(f"`{e}`" for e in events))
    includes = sorted(set(SCRIPT_INCLUDES.findall(script)) - IGNORED_CLASSES)
    if includes:
        effects.append("uses " + ", ".join(f"`{i}`" for i in includes))
    tables = sorted(set(QUERIES.findall(script)))
    if tables:
        effects.append("reads/writes " + ", ".join(f"`{t}`" for t in tables))
    if "setAbortAction(true)" in script.replace(" ", ""):
        effects.append("**can abort the operation**")
    messages = sorted(set(MESSAGES.findall(script)))
    if messages:
        effects.append("shows " + " and ".join(m.lower() for m in messages) + " messages")
    return effects


class FunctionalDocs:
    def __init__(
        self,
        workspace: Path,
        root: Path,
        narrative: Callable[[str, Mapping[str, str]], str],
    ) -> None:
        self.workspace = workspace
        self.root = root
        self.models = workspace / "model" / "tables"
        self.narrative = narrative
        self._cache: dict[str, dict[str, Any] | None] = {}

    # -- selection ---------------------------------------------------------------
    def tables(self, configured: Iterable[str]) -> list[str]:
        selected = {name for name in configured if self._model(name)}
        if self.models.is_dir():
            for path in self.models.glob("*.yaml"):
                model = self._model(path.stem)
                if model and any(
                    entry.get("customized")
                    for entries in (model.get("behaviour") or {}).values() for entry in entries
                ):
                    selected.add(path.stem)
        return sorted(selected)

    def _model(self, table: str) -> dict[str, Any] | None:
        if table not in self._cache:
            path = self.models / f"{table}.yaml"
            self._cache[table] = (
                yaml.load(path.read_text(encoding="utf-8"), Loader=_LOADER)  # noqa: S506
                if path.is_file() else None
            )
        return self._cache[table]

    def _script(self, entry: Mapping[str, Any], name: str) -> str:
        path = self.workspace / str(entry.get("path", "")) / name
        return path.read_text(encoding="utf-8") if path.is_file() else ""

    def _link(self, entry: Mapping[str, Any]) -> str:
        target = str(entry.get("path", ""))
        return f"**{_label(entry)}** (`{target}/record.yaml`)"

    # -- rendering ---------------------------------------------------------------
    def render(self, table: str, existing: Mapping[str, str]) -> str:
        model = self._model(table) or {}
        chain = [table, *model.get("extends", [])]
        sections: dict[str, list[tuple[str, dict[str, Any]]]] = defaultdict(list)
        for owner in chain:
            owner_model = self._model(owner) or {}
            for section, entries in (owner_model.get("behaviour") or {}).items():
                for entry in entries:
                    sections[section].append((owner, entry))
        out = [f"# {model.get('label') or table} (`{table}`)\n"]
        facts = [f"Scope: `{model.get('scope', 'global')}`"]
        if model.get("extends"):
            facts.append("extends " + " → ".join(f"`{t}`" for t in model["extends"]))
        facts.append(f"{len(model.get('fields') or {})} own fields")
        out.append(" · ".join(facts) + f". Model: `model/tables/{table}.yaml`.\n")
        out.append("Entries marked ★ are customer changes; others are out of box. Rules "
                   "inherited from a parent table are labelled with that table.\n")
        out += ["## Functional description\n",
                self.narrative(f"table-{table}-purpose", existing)]
        out += self._summary(sections)
        out += self._server(sections, table)
        out += self._client(sections, table)
        out += self._actions(sections, table)
        out += self._access(sections, table)
        out += self._notifications(sections, table)
        out += self._other(sections, table)
        return "\n".join(out) + "\n"

    def _summary(self, sections: Mapping[str, list[tuple[str, dict[str, Any]]]]) -> list[str]:
        rows = []
        for section, items in sorted(sections.items()):
            active = [e for _, e in items if e.get("active", "true") != "false"]
            custom = sum(1 for e in active if e.get("customized"))
            rows.append(f"| {section.replace('_', ' ')} | {len(active)} | {custom} |")
        if not rows:
            return ["_No behaviour is attached to this table._\n"]
        return ["## At a glance\n", "| Behaviour | Active | Customized |", "|---|---|---|",
                *rows, ""]

    def _owner(self, owner: str, table: str) -> str:
        return "" if owner == table else f" _(from `{owner}`)_"

    def _server(self, sections: Mapping[str, list[tuple[str, dict[str, Any]]]],
                table: str) -> list[str]:
        rules = [(o, e) for o, e in sections.get("business_rules", [])
                 if e.get("active", "true") != "false"]
        if not rules:
            return []
        out = ["## Server-side processing (business rules)\n",
               "Rules run in `order` within each phase: _before_ rules can change the record "
               "or abort, _after_ rules react to the saved record, _async_ rules run later in "
               "the background, _display_ rules prepare data for the form.\n"]
        phases = [("Query", "action_query", ("before",)),
                  ("Form display", None, ("display",)),
                  ("Insert", "action_insert", ("before", "after", "async", "async_always")),
                  ("Update", "action_update", ("before", "after", "async", "async_always")),
                  ("Delete", "action_delete", ("before", "after", "async", "async_always"))]
        for title, flag, whens in phases:
            lines: list[str] = []
            for when in whens:
                matched = [
                    (o, e) for o, e in rules
                    if e.get("when") == when and (flag is None or e.get(flag) == "true")
                ]
                if title == "Form display":
                    matched = [(o, e) for o, e in rules
                               if e.get("when") in ("display", "before_display")]
                for owner, entry in sorted(matched, key=lambda i: _order(i[1])):
                    lines.append(self._rule_line(owner, entry, table, when))
                if title == "Form display":
                    break
            if lines:
                out += [f"### {title}\n", *lines, ""]
        return out

    def _rule_line(self, owner: str, entry: Mapping[str, Any], table: str, when: str) -> str:
        parts = [f"- **{when}** · order {entry.get('order', '100')} · "
                 f"{self._link(entry)}{' ★' if entry.get('customized') else ''}"
                 f"{self._owner(owner, table)}"]
        condition = readable_query(str(entry.get("filter_condition", "")))
        if condition:
            parts.append(f"  - when: {condition}")
        if entry.get("condition"):
            parts.append(f"  - condition script: `{_short(str(entry['condition']))}`")
        if entry.get("abort_action") == "true":
            parts.append("  - **aborts the operation** when the condition matches")
        if entry.get("advanced") == "true" or "script.js" in (entry.get("files") or []):
            for effect in script_effects(self._script(entry, "script.js")):
                parts.append(f"  - {effect}")
        return "\n".join(parts)

    def _client(self, sections: Mapping[str, list[tuple[str, dict[str, Any]]]],
                table: str) -> list[str]:
        scripts = [(o, e) for o, e in sections.get("client_scripts", [])
                   if e.get("active", "true") != "false"]
        policies = [(o, e) for o, e in sections.get("ui_policies", [])
                    if e.get("active", "true") != "false"]
        if not scripts and not policies:
            return []
        out = ["## Form behaviour (browser)\n"]
        by_type: dict[str, list[tuple[str, dict[str, Any]]]] = defaultdict(list)
        for owner, entry in scripts:
            by_type[str(entry.get("type", "onLoad"))].append((owner, entry))
        for kind in ("onLoad", "onChange", "onSubmit", "onCellEdit"):
            items = by_type.pop(kind, [])
            if not items:
                continue
            out.append(f"### {kind} client scripts\n")
            for owner, entry in sorted(items, key=lambda i: (str(i[1].get("field_name", "")),
                                                             _label(i[1]))):
                field = f" on `{entry['field_name']}`" if entry.get("field_name") else ""
                out.append(f"- {self._link(entry)}{field}"
                           f"{' ★' if entry.get('customized') else ''}{self._owner(owner, table)}")
                for effect in script_effects(self._script(entry, "script.js"), client=True):
                    out.append(f"  - {effect}")
            out.append("")
        if policies:
            out.append("### UI policies\n")
            for owner, entry in sorted(policies, key=lambda i: _order(i[1])):
                condition = readable_query(str(entry.get("conditions", ""))) or "always"
                out.append(f"- {self._link(entry)}{' ★' if entry.get('customized') else ''}"
                           f"{self._owner(owner, table)} — when {condition}")
                for action in entry.get("actions", []):
                    flags = [name for name in ("mandatory", "visible", "disabled", "cleared")
                             if action.get(name) not in (None, "", "ignore")]
                    rendered = ", ".join(f"{name}={action[name]}" for name in flags)
                    out.append(f"  - `{action.get('field', '?')}`: {rendered or 'no change'}")
            out.append("")
        return out

    def _actions(self, sections: Mapping[str, list[tuple[str, dict[str, Any]]]],
                 table: str) -> list[str]:
        actions = [(o, e) for o, e in sections.get("ui_actions", [])
                   if e.get("active", "true") != "false"]
        if not actions:
            return []
        out = ["## Actions (buttons, links and menus)\n",
               "| Action | Where | Condition | Customized |", "|---|---|---|---|"]
        for owner, entry in sorted(actions, key=lambda i: (_label(i[1]), i[0])):
            where = [label for key, label in (
                ("form_button", "form button"), ("form_link", "form link"),
                ("form_context_menu", "form menu"), ("list_button", "list button"),
                ("list_choice", "list choice"), ("list_context_menu", "list menu"),
            ) if entry.get(key) == "true"]
            out.append(f"| {self._link(entry)}{self._owner(owner, table)} | "
                       f"{', '.join(where) or '—'} | `{_short(str(entry.get('condition', '')))}` "
                       f"| {'★' if entry.get('customized') else ''} |")
        return [*out, ""]

    def _access(self, sections: Mapping[str, list[tuple[str, dict[str, Any]]]],
                table: str) -> list[str]:
        acls = [(o, e) for o, e in sections.get("acls", []) if e.get("active", "true") != "false"]
        if not acls:
            return []
        out = ["## Access control\n",
               "| Operation | Field | Roles | Condition/script | ACL |", "|---|---|---|---|---|"]
        for owner, entry in sorted(acls, key=lambda i: (str(i[1].get("operation", "")),
                                                        str(i[1].get("field", "")))):
            logic = []
            if entry.get("condition"):
                logic.append(readable_query(str(entry["condition"])))
            if entry.get("advanced") == "true":
                logic.append("script")
            out.append(
                f"| {entry.get('operation', '')} | {entry.get('field', '*') or '*'} | "
                f"{', '.join(entry.get('roles', [])) or '—'} | {'; '.join(logic) or '—'} | "
                f"{self._link(entry)}{' ★' if entry.get('customized') else ''}"
                f"{self._owner(owner, table)} |"
            )
        return [*out, ""]

    def _notifications(self, sections: Mapping[str, list[tuple[str, dict[str, Any]]]],
                       table: str) -> list[str]:
        notes = [(o, e) for o, e in sections.get("notifications", [])
                 if e.get("active", "true") != "false"]
        events = sections.get("events", [])
        if not notes and not events:
            return []
        out = ["## Notifications and events\n"]
        for owner, entry in sorted(notes, key=lambda i: _label(i[1])):
            actions = "/".join(
                key.split("_")[1]
                for key in ("action_insert", "action_update")
                if entry.get(key) == "true"
            )
            trigger = (
                f"on event `{entry['event_name']}`"
                if entry.get("event_name")
                else f"on {actions}" if actions else ""
            )
            condition = readable_query(str(entry.get("condition", ""))).strip()
            details = " ".join(
                part
                for part in (
                    trigger,
                    f"when {condition}" if condition else "",
                )
                if part
            )
            out.append(
                f"- Notification {self._link(entry)}"
                f"{' ' + details if details else ''}"
                f"{' ★' if entry.get('customized') else ''}{self._owner(owner, table)}"
            )
        for owner, entry in sorted(events, key=lambda i: _label(i[1])):
            handlers = ", ".join(self._link(h) for h in entry.get("script_actions", []))
            out.append(f"- Event `{entry.get('event_name', '')}`{self._owner(owner, table)}"
                       f"{' → script actions ' + handlers if handlers else ''}")
        return [*out, ""]

    def _other(self, sections: Mapping[str, list[tuple[str, dict[str, Any]]]],
               table: str) -> list[str]:
        out: list[str] = []
        titles = {"slas": "Service levels (SLAs)", "assignment_rules": "Assignment rules",
                  "data_policies": "Data policies", "dictionary_overrides":
                  "Dictionary overrides", "transform_maps": "Import transform maps",
                  "modules": "Navigation modules"}
        for section, title in titles.items():
            items = [(o, e) for o, e in sections.get(section, [])
                     if e.get("active", "true") != "false"]
            if not items:
                continue
            out.append(f"## {title}\n")
            for owner, entry in sorted(items, key=lambda i: _label(i[1])):
                detail = []
                for key in ("start_condition", "stop_condition", "condition", "conditions"):
                    if entry.get(key):
                        detail.append(f"{key.replace('_', ' ')}: "
                                      f"{readable_query(str(entry[key]))}")
                for key in ("duration", "element", "source_table"):
                    if entry.get(key):
                        detail.append(f"{key.replace('_', ' ')}: `{entry[key]}`")
                out.append(f"- {self._link(entry)}{' ★' if entry.get('customized') else ''}"
                           f"{self._owner(owner, table)}"
                           + (" — " + "; ".join(detail) if detail else ""))
            out.append("")
        return out


def _label(entry: Mapping[str, Any]) -> str:
    for key in ("name", "short_description", "title", "event_name", "element"):
        if entry.get(key):
            return str(entry[key])
    return str(entry.get("sys_id", "record"))


def _order(entry: Mapping[str, Any]) -> tuple[float, str]:
    try:
        return float(entry.get("order") or 100), _label(entry)
    except ValueError:
        return 100.0, _label(entry)


def _short(value: str, limit: int = 120) -> str:
    value = re.sub(r"\s+", " ", value).strip().replace("|", "\\|").replace("`", "'")
    return value if len(value) <= limit else value[: limit - 1] + "…"
