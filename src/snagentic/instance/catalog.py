"""Discovery of the ``sys_metadata`` class hierarchy, scopes, and field types."""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

TEXT_TYPE_EXTENSIONS: Mapping[str, str] = {
    "script": "js",
    "script_plain": "js",
    "script_server": "js",
    "script_client": "js",
    "html": "html",
    "html_script": "html",
    "html_template": "html",
    "translated_html": "html",
    "css": "css",
    "xml": "xml",
    "json": "json",
}
SECRET_TYPES = frozenset({"password", "password2", "encrypted_text"})
SECRET_FIELD_NAMES = frozenset(
    {
        "api_key",
        "client_secret",
        "credential",
        "password",
        "password2",
        "private_key",
        "secret",
        "token",
        "access_token",
        "refresh_token",
    }
)

# Fallback text-field map, merged with dictionary data. Keeps well-known artifacts
# exploded into readable files even when sys_dictionary is not readable.
KNOWN_TEXT_FIELDS: Mapping[str, Mapping[str, str]] = {
    "sys_script_include": {"script": "js"},
    "sys_script": {"script": "js"},
    "sys_script_client": {"script": "js"},
    "catalog_script_client": {"script": "js"},
    "sys_ui_script": {"script": "js"},
    "sys_ui_action": {"script": "js", "client_script_v2": "js"},
    "sys_ui_policy": {"script_true": "js", "script_false": "js"},
    "sys_ui_page": {"html": "html", "client_script": "js", "processing_script": "js"},
    "sys_ui_macro": {"xml": "xml"},
    "sys_security_acl": {"script": "js"},
    "sys_ws_operation": {"operation_script": "js"},
    "sys_script_fix": {"script": "js"},
    "sysauto_script": {"script": "js"},
    "sysevent_script_action": {"script": "js"},
    "sys_transform_script": {"script": "js"},
    "sys_transform_map": {"script": "js"},
    "sys_processor": {"script": "js"},
    "sp_widget": {
        "template": "html",
        "css": "css",
        "client_script": "js",
        "script": "js",
        "link": "js",
    },
    "sp_angular_provider": {"script": "js"},
}


class Reader(Protocol):
    def iterate(
        self,
        table: str,
        *,
        query: str = "",
        fields: list[str] | None = None,
        allow_missing: bool = False,
    ) -> Iterable[dict[str, Any]]: ...


@dataclass
class Catalog:
    parents: dict[str, str | None]
    scopes: dict[str, str]
    typed_fields: dict[str, dict[str, str]] = field(default_factory=dict)

    @classmethod
    def discover(cls, reader: Reader) -> Catalog:
        tables = list(
            reader.iterate("sys_db_object", fields=["sys_id", "name", "super_class"])
        )
        by_id = {str(row.get("sys_id")): str(row.get("name")) for row in tables}
        parents: dict[str, str | None] = {}
        for row in tables:
            super_id = str(row.get("super_class") or "")
            parents[str(row.get("name"))] = by_id.get(super_id) if super_id else None
        scopes = {
            str(row.get("sys_id")): str(row.get("scope") or "global")
            for row in reader.iterate("sys_scope", fields=["sys_id", "scope"])
        }
        scopes.setdefault("global", "global")
        interesting = ",".join(sorted({*TEXT_TYPE_EXTENSIONS, *SECRET_TYPES}))
        typed: dict[str, dict[str, str]] = {}
        for row in reader.iterate(
            "sys_dictionary",
            query=f"internal_typeIN{interesting}",
            fields=["name", "element", "internal_type"],
            allow_missing=True,
        ):
            table = str(row.get("name") or "")
            element = str(row.get("element") or "")
            internal_type = str(row.get("internal_type") or "")
            if table and element:
                typed.setdefault(table, {})[element] = internal_type
        return cls(parents=parents, scopes=scopes, typed_fields=typed)

    def ancestors(self, table: str) -> list[str]:
        chain: list[str] = []
        current: str | None = table
        while current and current not in chain:
            chain.append(current)
            current = self.parents.get(current)
        return chain

    def is_metadata(self, table: str) -> bool:
        return "sys_metadata" in self.ancestors(table)

    def metadata_classes(self) -> list[str]:
        return sorted(name for name in self.parents if self.is_metadata(name))

    def text_fields(self, table: str) -> dict[str, str]:
        result: dict[str, str] = {}
        for ancestor in reversed(self.ancestors(table)):
            for element, internal_type in self.typed_fields.get(ancestor, {}).items():
                extension = TEXT_TYPE_EXTENSIONS.get(internal_type)
                if extension:
                    result[element] = extension
            result.update(KNOWN_TEXT_FIELDS.get(ancestor, {}))
        return result

    def secret_fields(self, table: str) -> set[str]:
        result = set(SECRET_FIELD_NAMES)
        for ancestor in self.ancestors(table):
            for element, internal_type in self.typed_fields.get(ancestor, {}).items():
                if internal_type in SECRET_TYPES:
                    result.add(element)
        return result

    def scope_namespace(self, scope_sys_id: str | None) -> str:
        if not scope_sys_id:
            return "global"
        return self.scopes.get(scope_sys_id, scope_sys_id)

    def scope_sys_id(self, namespace: str) -> str | None:
        for sys_id, value in self.scopes.items():
            if value == namespace and sys_id != "global":
                return sys_id
        return "global" if namespace == "global" else None

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "parents": self.parents,
                    "scopes": self.scopes,
                    "typed_fields": self.typed_fields,
                },
                sort_keys=True,
            ),
            encoding="utf-8",
        )

    @classmethod
    def load(cls, path: Path) -> Catalog | None:
        if not path.is_file():
            return None
        raw = json.loads(path.read_text(encoding="utf-8"))
        return cls(
            parents=dict(raw["parents"]),
            scopes=dict(raw["scopes"]),
            typed_fields={key: dict(value) for key, value in raw["typed_fields"].items()},
        )
