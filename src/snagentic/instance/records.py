"""Normalized on-disk representation of ServiceNow metadata records.

Each record becomes one directory::

    metadata/[domains/<domain>/]<scope>/<class>/<slug>--<sys_id>/
        _meta.yaml        identity, provenance, and the canonical content hash
        record.yaml       all remaining (non-secret) fields as raw string values
        <field>.<ext>     script/HTML/CSS/XML/JSON fields exploded into real files

Hash contract (version 1): ``sha256(canonical_json({"class": <sys_class_name>,
"fields": {<field>: canonical_text(value)}}))`` over every field in ``record.yaml``
plus every exploded text field. ``canonical_text`` converts CRLF/CR to LF and strips
trailing newlines. Identity and provenance fields (see ``META_FIELDS``) are excluded.
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import tempfile
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

import yaml

from snagentic.artifacts.normalization import canonical_json
from snagentic.instance.catalog import Catalog

HASH_VERSION = 1
META_FILE = "_meta.yaml"
RECORD_FILE = "record.yaml"
# Platform sys_ids are 32 hex characters, but out-of-box records created before that
# convention keep legacy ids such as "sysverb_query", "inbox", "2" or "Default view".
SYS_ID = re.compile(r"^[A-Za-z0-9_](?:[A-Za-z0-9_ ]{0,62}[A-Za-z0-9_])?$")
META_FIELDS = frozenset(
    {
        "sys_id",
        "sys_class_name",
        "sys_mod_count",
        "sys_updated_on",
        "sys_updated_by",
        "sys_created_on",
        "sys_created_by",
        "sys_update_name",
        "sys_scope",
        "sys_domain",
        "sys_domain_path",
        "sys_package",
        "sys_tags",
    }
)
_LOADER: Any = getattr(yaml, "CSafeLoader", yaml.SafeLoader)
_DUMPER: Any = getattr(yaml, "CSafeDumper", yaml.SafeDumper)
NAME_FIELDS = ("sys_name", "name", "api_name", "internal_name", "element", "title", "id")


@dataclass(frozen=True)
class LocalRecord:
    directory: Path
    meta: dict[str, Any]
    values: dict[str, str]

    @property
    def sys_id(self) -> str | None:
        value = self.meta.get("sys_id")
        return value if isinstance(value, str) and SYS_ID.fullmatch(value) else None

    @property
    def table(self) -> str:
        return str(self.meta["sys_class_name"])

    def current_hash(self) -> str:
        return record_hash(self.table, self.values)


def canonical_text(value: Any) -> str:
    text = "" if value is None else str(value)
    return text.replace("\r\n", "\n").replace("\r", "\n").rstrip("\n")


def record_hash(table: str, values: Mapping[str, Any]) -> str:
    material = {
        "class": table,
        "fields": {key: canonical_text(value) for key, value in values.items()},
    }
    return hashlib.sha256(canonical_json(material).encode("utf-8")).hexdigest()


def slug(value: str) -> str:
    lowered = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return lowered[:60].rstrip("-") or "record"


def record_display_name(record: Mapping[str, Any]) -> str:
    for name in NAME_FIELDS:
        value = record.get(name)
        if isinstance(value, str) and value.strip():
            if name == "element" and record.get("name"):
                return f"{record['name']}.{value}"
            return value
    return "record"


def record_relative_dir(record: Mapping[str, Any], catalog: Catalog) -> PurePosixPath:
    sys_id = str(record.get("sys_id") or "")
    table = str(record.get("sys_class_name") or "")
    if not SYS_ID.fullmatch(sys_id):
        raise ValueError(f"record has an invalid sys_id: {sys_id!r}")
    if not re.fullmatch(r"[a-z0-9_$]+", table):
        raise ValueError(f"record has an invalid sys_class_name: {table!r}")
    scope = path_segment(catalog.scope_namespace(str(record.get("sys_scope") or "")))
    domain = path_segment(str(record.get("sys_domain") or "global"))
    leaf = f"{slug(record_display_name(record))}--{sys_id}"
    if domain and domain != "global":
        return PurePosixPath("domains", domain, scope, table, leaf)
    return PurePosixPath(scope, table, leaf)


def path_segment(value: str) -> str:
    """Instance data can carry stray whitespace or symbols; normalize, don't abort."""

    value = value.strip()
    if value and re.fullmatch(r"[A-Za-z0-9_.$-]+", value) and value not in {".", ".."}:
        return value
    return safe_component(slug(value) if value else "global")


def safe_component(value: str) -> str:
    if (
        not value
        or value in {".", ".."}
        or any(ch in value for ch in "/\\\x00")
        or not re.fullmatch(r"[A-Za-z0-9_.$-]+", value)
    ):
        raise ValueError(f"unsafe path component: {value!r}")
    return value


def split_record(
    record: Mapping[str, Any],
    catalog: Catalog,
    redact: Mapping[str, Iterable[str]] | None = None,
    property_value_allowlist: Iterable[str] | None = None,
) -> tuple[dict[str, Any], dict[str, str], dict[str, str]]:
    """Return ``(meta, plain_fields, text_fields)`` for a raw Table API record."""

    table = str(record["sys_class_name"])
    secret = catalog.secret_fields(table)
    configured_redact: set[str] = set()
    for ancestor in catalog.ancestors(table):
        configured_redact.update((redact or {}).get(ancestor, ()))
    if (
        table == "sys_properties"
        and str(record.get("name") or "") in set(property_value_allowlist or ())
    ):
        configured_redact.discard("value")
    secret.update(configured_redact)
    text_map = catalog.text_fields(table)
    plain: dict[str, str] = {}
    text: dict[str, str] = {}
    redacted: list[str] = []
    for key, raw in record.items():
        if key in META_FIELDS:
            continue
        if key in secret or key.casefold() in secret:
            redacted.append(key)
            continue
        value = "" if raw is None else str(raw)
        if key in text_map and canonical_text(value):
            text[key] = value
        else:
            plain[key] = value
    values = {**plain, **text}
    meta: dict[str, Any] = {
        key: str(record[key]) for key in sorted(META_FIELDS) if record.get(key) not in (None, "")
    }
    meta["scope"] = catalog.scope_namespace(str(record.get("sys_scope") or ""))
    meta["hash"] = record_hash(table, values)
    meta["hash_version"] = HASH_VERSION
    meta["files"] = {key: f"{key}.{text_map[key]}" for key in sorted(text)}
    if redacted:
        meta["redacted"] = sorted(redacted)
    return meta, plain, text


def write_record(
    directory: Path,
    record: Mapping[str, Any],
    catalog: Catalog,
    redact: Mapping[str, Iterable[str]] | None = None,
    property_value_allowlist: Iterable[str] | None = None,
    *,
    read_only: bool = False,
) -> dict[str, Any]:
    meta, plain, text = split_record(record, catalog, redact, property_value_allowlist)
    if read_only:
        meta["read_only"] = True
    if directory.exists():
        shutil.rmtree(directory)
    directory.mkdir(parents=True)
    write_yaml(directory / META_FILE, meta)
    write_yaml(directory / RECORD_FILE, plain)
    for key, filename in meta["files"].items():
        atomic_write(directory / filename, canonical_text(text[key]) + "\n")
    return meta


def read_record(directory: Path) -> LocalRecord:
    meta = load_yaml(directory / META_FILE) if (directory / META_FILE).is_file() else {}
    record_path = directory / RECORD_FILE
    plain = load_yaml(record_path) if record_path.is_file() else {}
    values = {str(key): "" if value is None else str(value) for key, value in plain.items()}
    raw_files = meta.get("files")
    files: dict[str, Any] = raw_files if isinstance(raw_files, dict) else {}
    known: set[str] = set()
    for key, filename in sorted(files.items()):
        path = directory / str(filename)
        known.add(str(filename))
        if path.is_file():
            values[str(key)] = canonical_text(path.read_text(encoding="utf-8"))
        else:
            values[str(key)] = ""
    for path in sorted(directory.iterdir()):
        if path.name in {META_FILE, RECORD_FILE} or path.name in known or not path.is_file():
            continue
        # A new exploded file added by an agent: "<field>.<ext>".
        field_name = path.name.rsplit(".", 1)[0]
        if re.fullmatch(r"[a-z0-9_]+", field_name):
            values[field_name] = canonical_text(path.read_text(encoding="utf-8"))
    return LocalRecord(directory=directory, meta=meta, values=values)


def iter_record_dirs(root: Path) -> Iterable[Path]:
    if not root.is_dir():
        return
    for directory, child_directories, filenames in os.walk(root):
        child_directories.sort()
        if META_FILE in filenames:
            yield Path(directory)


def record_map(root: Path) -> dict[str, Path]:
    mapping: dict[str, Path] = {}
    for directory in iter_record_dirs(root):
        meta = load_yaml(directory / META_FILE)
        sys_id = meta.get("sys_id")
        if isinstance(sys_id, str):
            mapping[sys_id] = directory
    return mapping


def write_yaml(path: Path, value: Mapping[str, Any]) -> None:
    atomic_write(
        path,
        yaml.dump(dict(value), Dumper=_DUMPER, sort_keys=True, allow_unicode=False, width=100),
    )


def load_yaml(path: Path) -> dict[str, Any]:
    raw = yaml.load(path.read_text(encoding="utf-8"), Loader=_LOADER)  # noqa: S506 - safe loader
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ValueError(f"expected a YAML mapping: {path}")
    return raw


def atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(prefix=".tmp-", dir=path.parent)
    try:
        with os.fdopen(handle, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(content)
        os.replace(temporary, path)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise
