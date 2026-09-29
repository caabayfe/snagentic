"""Update set mirroring, collision detection, and team activity views."""

from __future__ import annotations

import re
import shutil
from collections import defaultdict
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any, Protocol

from snagentic.instance.records import load_yaml, safe_component, slug, write_yaml

UPDATE_SET_FIELDS = [
    "sys_id",
    "name",
    "state",
    "application",
    "parent",
    "base_update_set",
    "is_default",
    "sys_created_by",
    "sys_created_on",
    "sys_updated_by",
    "sys_updated_on",
    "description",
]
UPDATE_FIELDS = [
    "sys_id",
    "name",
    "type",
    "target_name",
    "action",
    "update_set",
    "application",
    "sys_created_by",
    "sys_updated_by",
    "sys_updated_on",
]
OPEN_STATE = "in progress"
SYS_ID_SUFFIX = re.compile(r"([0-9a-f]{32})$")


class Reader(Protocol):
    def iterate(
        self,
        table: str,
        *,
        query: str = "",
        fields: list[str] | None = None,
        allow_missing: bool = False,
    ) -> Iterable[dict[str, Any]]: ...


def fetch_update_sets(
    reader: Reader, destination: Path, *, window_days: int, batch_size: int = 100
) -> dict[str, int]:
    """Replace ``destination`` with open and recently changed update sets."""

    query = f"state={OPEN_STATE}"
    if window_days > 0:
        query += f"^ORsys_updated_on>=javascript:gs.daysAgoStart({int(window_days)})"
    sets = list(
        reader.iterate(
            "sys_update_set", query=query + "^ORDERBYsys_id", fields=UPDATE_SET_FIELDS
        )
    )
    entries: dict[str, list[dict[str, Any]]] = defaultdict(list)
    ids = [str(item["sys_id"]) for item in sets]
    for start in range(0, len(ids), batch_size):
        chunk = ",".join(ids[start : start + batch_size])
        for entry in reader.iterate(
            "sys_update_xml",
            query=f"update_setIN{chunk}^ORDERBYname^ORDERBYsys_id",
            fields=UPDATE_FIELDS,
        ):
            entries[str(entry.get("update_set"))].append(
                {key: str(entry.get(key) or "") for key in UPDATE_FIELDS if key != "update_set"}
            )
    if destination.exists():
        shutil.rmtree(destination)
    for item in sets:
        state = safe_component(slug(str(item.get("state") or "unknown")))
        leaf = f"{slug(str(item.get('name') or 'update-set'))}--{item['sys_id']}"
        folder = destination / state / leaf
        write_yaml(
            folder / "update-set.yaml",
            {key: str(item.get(key) or "") for key in UPDATE_SET_FIELDS},
        )
        changes = sorted(
            entries.get(str(item["sys_id"]), []), key=lambda row: (row["name"], row["sys_id"])
        )
        write_yaml(folder / "changes.yaml", {"count": len(changes), "changes": changes})
    return {
        "update_sets": len(sets),
        "open": sum(1 for item in sets if item.get("state") == OPEN_STATE),
        "changes": sum(len(value) for value in entries.values()),
    }


def load_update_sets(directory: Path) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    if not directory.is_dir():
        return result
    for header in sorted(directory.rglob("update-set.yaml")):
        data = load_yaml(header)
        changes_file = header.parent / "changes.yaml"
        changes = load_yaml(changes_file).get("changes", []) if changes_file.is_file() else []
        data["changes"] = changes
        data["path"] = header.parent.as_posix()
        result.append(data)
    return result


def update_name_sys_id(update_name: str) -> str | None:
    match = SYS_ID_SUFFIX.search(update_name)
    return match.group(1) if match else None


def collision_report(
    update_sets: Iterable[Mapping[str, Any]],
    *,
    local_changes: Iterable[Mapping[str, Any]] = (),
    agent_update_set: str | None = None,
) -> dict[str, Any]:
    """Find records touched by more than one open update set, and local edits that
    touch records held in someone else's open update set.

    Default update sets are the platform's catch-all for changes made without choosing
    a set; they do not mean anyone holds the record, so they are reported separately."""

    holders: dict[str, list[dict[str, str]]] = defaultdict(list)
    unmanaged: set[str] = set()
    for update_set in update_sets:
        if update_set.get("state") != OPEN_STATE:
            continue
        if is_default_update_set(update_set):
            unmanaged.update(str(change.get("name")) for change in update_set.get("changes", []))
            continue
        for change in update_set.get("changes", []):
            holders[str(change.get("name"))].append(
                {
                    "update_set": str(update_set.get("sys_id")),
                    "update_set_name": str(update_set.get("name")),
                    "user": str(change.get("sys_updated_by") or change.get("sys_created_by")),
                    "updated_on": str(change.get("sys_updated_on")),
                    "action": str(change.get("action")),
                    "target": str(change.get("target_name")),
                    "type": str(change.get("type")),
                }
            )
    between_sets: list[dict[str, Any]] = [
        {"record": name, "holders": sorted(items, key=_updated_on)}
        for name, items in sorted(holders.items())
        if len({item["update_set"] for item in items}) > 1
    ]
    local: list[dict[str, Any]] = []
    for change in local_changes:
        update_name = str(change.get("sys_update_name") or "")
        if not update_name:
            continue
        others = [
            item for item in holders.get(update_name, []) if item["update_set"] != agent_update_set
        ]
        if others:
            local.append(
                {
                    "record": update_name,
                    "path": change.get("path"),
                    "operation": change.get("operation"),
                    "holders": others,
                }
            )
    return {
        "between_update_sets": between_sets,
        "local_changes_in_open_update_sets": local,
        "summary": {
            "open_records": len(holders),
            "collisions": len(between_sets),
            "local_collisions": len(local),
            "default_update_set_records": len(unmanaged),
        },
    }


def is_default_update_set(update_set: Mapping[str, Any]) -> bool:
    return str(update_set.get("is_default") or "").lower() == "true"


def _updated_on(item: Mapping[str, str]) -> str:
    return item["updated_on"]


def activity_view(update_sets: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    by_user: dict[str, dict[str, Any]] = {}
    by_application: dict[str, dict[str, Any]] = {}
    sets_view: list[dict[str, Any]] = []
    for update_set in update_sets:
        changes = list(update_set.get("changes", []))
        last = max((str(change.get("sys_updated_on")) for change in changes), default="")
        sets_view.append(
            {
                "sys_id": update_set.get("sys_id"),
                "name": update_set.get("name"),
                "state": update_set.get("state"),
                "owner": update_set.get("sys_created_by"),
                "application": update_set.get("application"),
                "changes": len(changes),
                "last_change": last or update_set.get("sys_updated_on"),
            }
        )
        for change in changes:
            user = str(change.get("sys_updated_by") or change.get("sys_created_by") or "unknown")
            entry = by_user.setdefault(user, {"changes": 0, "update_sets": set(), "last": ""})
            entry["changes"] += 1
            entry["update_sets"].add(str(update_set.get("name")))
            entry["last"] = max(entry["last"], str(change.get("sys_updated_on")))
            application = str(change.get("application") or update_set.get("application") or "")
            app_entry = by_application.setdefault(application, {"changes": 0, "users": set()})
            app_entry["changes"] += 1
            app_entry["users"].add(user)
    return {
        "update_sets": sorted(sets_view, key=lambda item: str(item["last_change"]), reverse=True),
        "users": {
            user: {
                "changes": data["changes"],
                "update_sets": sorted(data["update_sets"]),
                "last_change": data["last"],
            }
            for user, data in sorted(by_user.items())
        },
        "applications": {
            app: {"changes": data["changes"], "users": sorted(data["users"])}
            for app, data in sorted(by_application.items())
        },
    }
