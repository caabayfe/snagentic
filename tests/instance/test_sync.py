from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
import yaml
from conftest import Harness
from pydantic import ValidationError

from snagentic.instance.config import SyncSettings
from snagentic.instance.records import SYS_ID, path_segment, read_record, record_hash
from snagentic.instance.sync import operational_identity


def _script_include(harness: Harness, name: str, script: str, **extra: str) -> str:
    return harness.fake.insert(
        "sys_script_include",
        {"name": name, "api_name": f"global.{name}", "script": script, "active": "true", **extra},
    )


def _record_dirs(root: Path) -> list[Path]:
    return sorted(path.parent for path in root.rglob("_meta.yaml"))


def test_full_fetch_mirrors_customer_metadata_into_mirror_branch(harness: Harness) -> None:
    sys_id = _script_include(harness, "HelloUtil", "var HelloUtil = Class.create();\r\n")
    harness.fake.insert("sys_properties", {"name": "x.secret.prop", "value": "hunter2"})
    harness.fake.insert("sys_documentation", {"name": "incident", "label": "Incident"})
    harness.fake.insert("incident", {"short_description": "business data"})

    result = harness.sync().fetch()

    assert result["mode"] == "full"
    assert result["updated"] == 2
    assert result["mirror_changed"] is True
    files = harness.git("ls-tree", "-r", "--name-only", "servicenow-remote/dev").split()
    include_dir = f"instances/dev/metadata/global/sys_script_include/helloutil--{sys_id}"
    assert f"{include_dir}/script.js" in files
    assert f"{include_dir}/record.yaml" in files
    records = [name for name in files if "/metadata/" in name]
    assert not any("sys_documentation" in name or "incident" in name for name in records)
    script = harness.git("show", f"servicenow-remote/dev:{include_dir}/script.js")
    assert script == "var HelloUtil = Class.create();\n"
    prop = next(name for name in files if "sys_properties" in name and name.endswith("record.yaml"))
    assert "hunter2" not in harness.git("show", f"servicenow-remote/dev:{prop}")
    meta_path = prop.replace("record.yaml", "_meta.yaml")
    meta = yaml.safe_load(harness.git("show", f"servicenow-remote/dev:{meta_path}"))
    assert meta["redacted"] == ["value"]
    # The user's branch and working tree are untouched by fetch.
    assert harness.git("status", "--porcelain") == ""
    assert harness.git("rev-parse", "--abbrev-ref", "HEAD").strip() == "main"


def test_operational_tables_are_mirrored_read_only_and_reconciled(
    harness: Harness,
) -> None:
    plugin = harness.fake.insert(
        "v_plugin",
        {
            "id": "com.example.safe",
            "name": "Safe Example Plugin",
            "active": "inactive",
            "requires": "com.example.base",
            "supports_rollback": "true",
        },
        track=False,
    )
    app = harness.fake.insert(
        "sys_store_app",
        {
            "name": "Safe Example App",
            "scope": "x_example_safe",
            "version": "1.0.0",
            "latest_version": "1.1.0",
            "price_type": "free",
        },
        track=False,
    )
    domain = harness.fake.insert(
        "domain",
        {"name": "Acceptance", "path": "/ACME/", "active": "true"},
        track=False,
    )

    result = harness.sync().fetch()

    assert result["operational"]["records"] == 3
    assert result["operational"]["records_by_table"] == {
        "domain": 1,
        "sys_plugins": 0,
        "sys_store_app": 1,
        "v_plugin": 1,
    }
    assert result["operational"]["complete"] is True
    files = harness.git("ls-tree", "-r", "--name-only", "servicenow-remote/dev").split()
    for table, sys_id in (
        ("v_plugin", operational_identity("v_plugin", harness.fake.records[plugin])),
        ("sys_store_app", operational_identity(
            "sys_store_app", harness.fake.records[app]
        )),
        ("domain", operational_identity("domain", harness.fake.records[domain])),
    ):
        meta_path = next(
            path for path in files
            if f"/{table}/" in path and path.endswith(f"--{sys_id}/_meta.yaml")
        )
        meta = yaml.safe_load(harness.git("show", f"servicenow-remote/dev:{meta_path}"))
        assert meta["read_only"] is True

    harness.fake.update(plugin, {"active": "active"})
    harness.fake.records.pop(app)
    result = harness.sync().fetch()
    assert result["mode"] == "incremental"
    assert result["updated"] == 1
    assert result["deleted"] == 1
    tree = harness.git("ls-tree", "-r", "--name-only", "servicenow-remote/dev")
    assert f"--{app}/record.yaml" not in tree


def test_operational_tables_are_enabled_by_default() -> None:
    assert SyncSettings().operational_tables == [
        "v_plugin",
        "sys_plugins",
        "sys_store_app",
        "domain",
    ]


def test_unreadable_operational_table_preserves_existing_records(harness: Harness) -> None:
    plugin = harness.fake.insert(
        "v_plugin",
        {"id": "com.example.safe", "name": "Safe Example Plugin", "active": "inactive"},
        track=False,
    )
    harness.sync().fetch()
    identity = operational_identity("v_plugin", harness.fake.records[plugin])
    harness.fake.denied_tables.add("v_plugin")

    result = harness.sync().fetch()

    assert result["operational"]["complete"] is False
    assert result["operational"]["unreadable_tables"] == ["v_plugin"]
    tree = harness.git("ls-tree", "-r", "--name-only", "servicenow-remote/dev")
    assert f"--{identity}/record.yaml" in tree


def test_unreadable_store_app_uses_complete_application_manager_snapshot(
    harness: Harness,
) -> None:
    harness.fake.denied_tables.add("sys_store_app")
    snapshot = harness.paths.state / "operational/sys_store_app.json"
    snapshot.parent.mkdir(parents=True)
    snapshot.write_text(json.dumps({
        "source": "application_manager",
        "complete": True,
        "records": [{
            "sys_id": "1" * 31 + "q",
            "scope": "x_acme_app",
            "name": "Acme App",
            "version": "1.2.3",
            "vendor": "Acme",
            "short_description": "Safe application metadata",
        }],
    }))

    result = harness.sync().fetch()

    assert result["operational"]["records_by_table"]["sys_store_app"] == 1
    assert result["operational"]["sources"]["sys_store_app"] == (
        "application_manager_snapshot"
    )
    assert "sys_store_app" not in result["operational"]["unreadable_tables"]
    identity = operational_identity("sys_store_app", {"sys_id": "1" * 31 + "q"})
    record_path = next(
        path
        for path in harness.git(
            "ls-tree", "-r", "--name-only", "servicenow-remote/dev"
        ).split()
        if "/sys_store_app/" in path and path.endswith(f"--{identity}/record.yaml")
    )
    record = harness.git("show", f"servicenow-remote/dev:{record_path}")
    assert "inventory_source: application_manager" in record
    assert "version: 1.2.3" in record


def test_changing_operational_tables_does_not_force_full_metadata_fetch(
    harness: Harness,
) -> None:
    plugin = harness.fake.insert(
        "v_plugin",
        {"id": "com.example.safe", "name": "Safe Example Plugin", "active": "inactive"},
        track=False,
    )
    app = harness.fake.insert(
        "sys_store_app",
        {"name": "Safe Example App", "scope": "x_example_safe", "version": "1.0.0"},
        track=False,
    )
    harness.sync().fetch()
    identity = operational_identity("v_plugin", harness.fake.records[plugin])
    app_identity = operational_identity("sys_store_app", harness.fake.records[app])
    assert harness.sync().fetch()["mode"] == "incremental"
    harness.config.sync.operational_tables = ["v_plugin"]

    result = harness.sync().fetch()

    assert result["mode"] == "incremental"
    assert result["deleted"] == 1
    tree = harness.git("ls-tree", "-r", "--name-only", "servicenow-remote/dev")
    assert f"--{identity}/record.yaml" in tree
    assert f"--{app_identity}/record.yaml" not in tree


def test_legacy_operational_coverage_fingerprint_does_not_force_full_fetch(
    harness: Harness,
) -> None:
    harness.fake.insert(
        "v_plugin",
        {"id": "com.example.safe", "name": "Safe Example Plugin", "active": "inactive"},
        track=False,
    )
    harness.sync().fetch()
    state = json.loads(harness.paths.sync_state.read_text())
    from snagentic.instance.sync import coverage_fingerprint, legacy_coverage_fingerprint

    state["coverage"] = legacy_coverage_fingerprint(
        harness.config.sync,
        ["v_plugin", "sys_plugins", "sys_store_app", "sys_domain"],
    )
    harness.paths.sync_state.write_text(json.dumps(state))
    sync = harness.sync()
    assert state["coverage"] in {
        coverage_fingerprint(harness.config.sync),
        legacy_coverage_fingerprint(harness.config.sync),
        legacy_coverage_fingerprint(
            harness.config.sync,
            ["v_plugin", "sys_plugins", "sys_store_app", "sys_domain"],
        ),
    }

    assert sync.fetch()["mode"] == "incremental"


def test_allowlisted_property_value_is_versioned(harness: Harness) -> None:
    name = "glide.incident.close.code"
    harness.config = harness.config.model_copy(update={
        "sync": harness.config.sync.model_copy(update={"property_value_allowlist": [name]})
    })
    harness.fake.insert("sys_properties", {"name": name, "value": "7"})

    harness.sync().fetch()

    files = harness.git("ls-tree", "-r", "--name-only", "servicenow-remote/dev").split()
    record = next(
        item for item in files
        if "/sys_properties/" in item and item.endswith("/record.yaml")
    )
    mirrored = yaml.safe_load(harness.git("show", f"servicenow-remote/dev:{record}"))
    assert mirrored["name"] == name
    assert mirrored["value"] == "7"
    meta = yaml.safe_load(
        harness.git("show", f"servicenow-remote/dev:{record.replace('record.yaml', '_meta.yaml')}")
    )
    assert "redacted" not in meta


@pytest.mark.parametrize("name", [
    "*",
    "x.company.api_key",
    "x.company.oauth.token",
    "x.company.password",
])
def test_property_value_allowlist_rejects_unsafe_names(name: str) -> None:
    with pytest.raises(ValidationError):
        SyncSettings(property_value_allowlist=[name])


def test_record_round_trip_preserves_hash(harness: Harness) -> None:
    _script_include(harness, "Util", "line1\r\nline2\r\n\r\n", description="multi\nline")
    harness.sync().fetch()
    (directory,) = [
        path for path in _record_dirs(harness.paths.mirror_workspace)
        if "sys_script_include" in path.as_posix()
    ]
    record = read_record(directory)
    assert record.current_hash() == record.meta["hash"]
    assert record.values["script"] == "line1\nline2"


def test_duplicate_scope_paths_are_reconciled_to_catalog_namespace(
    harness: Harness,
) -> None:
    scope_id = harness.fake.create_scope("x_acme")
    sys_id = harness.fake.insert(
        "sys_script_include",
        {"name": "ScopedUtil", "api_name": "x_acme.ScopedUtil", "sys_scope": scope_id},
    )
    sync = harness.sync()
    sync.fetch()
    metadata = harness.paths.mirror_workspace / "metadata"
    canonical = next(path.parent for path in metadata.rglob(f"*--{sys_id}/_meta.yaml"))
    duplicate = metadata / scope_id / "sys_script_include" / canonical.name
    duplicate.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(canonical, duplicate)
    index = sync._mirror_index(metadata)

    reconciled = sync._reconcile_duplicate_paths(index, metadata, sync.catalog(refresh=False))

    assert len(reconciled) == 1
    assert canonical.is_dir()
    assert not duplicate.exists()
    assert index[sys_id]["directory"] == canonical


def test_hash_contract_vectors() -> None:
    vectors = json.loads(
        (Path(__file__).parents[1] / "fixtures" / "hash-vectors.json").read_text()
    )
    for vector in vectors["vectors"]:
        assert record_hash(vector["class"], vector["fields"]) == vector["hash"], vector["name"]


def test_incremental_fetch_updates_and_deletes(harness: Harness) -> None:
    keep = _script_include(harness, "Keep", "a")
    drop = _script_include(harness, "Drop", "b")
    sync = harness.sync()
    sync.fetch()
    harness.fake.update(keep, {"script": "a2"}, by="alice")
    harness.fake.delete(drop, by="bob")
    added = _script_include(harness, "Added", "c")

    result = harness.sync().fetch()

    assert result["mode"] == "incremental"
    assert result["updated"] == 2
    assert result["deleted"] == 1
    tree = harness.git("ls-tree", "-r", "--name-only", "servicenow-remote/dev")
    assert keep in tree and added in tree and drop not in tree
    log = harness.git("log", "--format=%an|%s", "servicenow-remote/dev")
    assert log.splitlines()[0] == "snagentic mirror|snagentic fetch dev: 2 updated, 1 deleted"
    body = harness.git("log", "-1", "--format=%b", "servicenow-remote/dev")
    assert "alice (1)" in body


def test_fetch_without_changes_does_not_commit(harness: Harness) -> None:
    _script_include(harness, "Stable", "x")
    harness.sync().fetch()
    before = harness.git("rev-parse", "servicenow-remote/dev")
    result = harness.sync().fetch()
    assert result["mirror_changed"] is False
    assert harness.git("rev-parse", "servicenow-remote/dev") == before


def test_keyset_paging_handles_identical_timestamps(harness: Harness) -> None:
    harness.config.page_size = 2
    ids = [_script_include(harness, f"Bulk{i}", str(i)) for i in range(5)]
    for sys_id in ids:
        harness.fake.records[sys_id]["sys_updated_on"] = "2026-09-20 09:00:00"
    result = harness.sync().fetch()
    assert result["updated"] == 5


def test_full_fetch_detects_deletes_without_tombstones(harness: Harness) -> None:
    gone = _script_include(harness, "Gone", "x")
    harness.sync().fetch()
    harness.fake.records.pop(gone)
    result = harness.sync().fetch(full=True)
    assert result["deleted"] == 1


def test_scoped_and_domain_records_use_scope_and_domain_paths(harness: Harness) -> None:
    scope = harness.fake.create_scope("x_acme_app")
    domain = "d" * 32
    sys_id = _script_include(harness, "Scoped", "s", sys_scope=scope, sys_domain=domain)
    harness.sync().fetch()
    tree = harness.git("ls-tree", "-r", "--name-only", "servicenow-remote/dev")
    assert (
        f"instances/dev/metadata/domains/{domain}/x_acme_app/sys_script_include/scoped--{sys_id}/"
        in tree
    )


def test_update_sets_are_mirrored(harness: Harness) -> None:
    harness.fake.open_update_set("STRY001 alice", by="alice")
    _script_include(harness, "Tracked", "t", )
    harness.fake.update(next(iter(
        sid for sid, rec in harness.fake.records.items() if rec.get("name") == "Tracked"
    )), {"script": "t2"}, by="alice")
    result = harness.sync().fetch()
    # The insert by "developer" (no chosen set) lands in Default, the update in alice's set.
    assert result["update_sets"] == {"update_sets": 2, "open": 2, "changes": 2}
    tree = harness.git("ls-tree", "-r", "--name-only", "servicenow-remote/dev")
    assert "update-sets/in-progress/stry001-alice--" in tree


def test_non_behaviour_classes_mirror_customer_updates_only(harness: Harness) -> None:
    baseline = harness.fake.insert(
        "sys_dictionary", {"name": "incident", "element": "oob_field"}, track=False,
        by="myla.jordan",
    )
    customer = harness.fake.insert("sys_dictionary", {"name": "incident", "element": "u_field"})
    harness.sync().fetch()
    files = harness.git("ls-tree", "-r", "--name-only", "servicenow-remote/dev")
    assert customer in files and baseline not in files

    harness.fake.update(baseline, {"max_length": "80"}, by="admin")
    harness.sync().fetch()
    files = harness.git("ls-tree", "-r", "--name-only", "servicenow-remote/dev")
    assert baseline in files


def test_behaviour_classes_are_mirrored_in_full(harness: Harness) -> None:
    oob = harness.fake.insert(
        "sys_script", {"name": "Close tasks", "collection": "task", "script": "x();"},
        track=False, by="glide.maint",
    )
    result = harness.sync().fetch()
    files = harness.git("ls-tree", "-r", "--name-only", "servicenow-remote/dev")
    assert f"instances/dev/metadata/global/sys_script/close-tasks--{oob}/script.js" in files
    assert result["updated"] == 1

    # Incremental: unchanged records are not rewritten, changed ones are.
    assert harness.sync().fetch()["updated"] == 0
    harness.fake.update(oob, {"script": "y();"}, by="glide.maint")
    assert harness.sync().fetch()["updated"] == 1


def test_changing_coverage_forces_a_full_fetch(harness: Harness) -> None:
    harness.fake.insert("sys_script", {"name": "Rule", "collection": "task"}, track=False)
    harness.sync().fetch()
    assert harness.sync().fetch()["mode"] == "incremental"
    harness.config.sync.baseline_classes = []
    result = harness.sync().fetch()
    assert result["mode"] == "full" and result["deleted"] == 1


def test_paging_continues_past_pages_shortened_by_acls(harness: Harness) -> None:
    harness.config.page_size = 2
    harness.config.sync.include_baseline = True
    ids = [_script_include(harness, f"Paged{index}", "p") for index in range(6)]
    harness.fake.hidden.add(ids[1])
    harness.sync().fetch()
    files = harness.git("ls-tree", "-r", "--name-only", "servicenow-remote/dev")
    assert all(sys_id in files for sys_id in ids[2:])


def _compressed(value: object) -> str:
    import base64
    import gzip
    import json

    return base64.b64encode(gzip.compress(json.dumps(value).encode())).decode()


def test_every_metadata_class_and_child_rows_are_mirrored(harness: Harness) -> None:
    harness.config.sync.baseline_exclude = []
    fake = harness.fake
    flow = fake.insert("sys_hub_flow", {"name": "Route P1"}, track=False)
    fake.insert("sys_hub_action_instance_v2",
                {"flow": flow, "order": "2", "values": _compressed({"table": "incident"})},
                track=False)
    workflow = fake.insert("wf_workflow", {"name": "Approval"}, track=False)
    version = fake.insert("wf_workflow_version", {"workflow": workflow, "name": "v1"}, track=False)
    activity = fake.insert("wf_activity", {"workflow_version": version, "name": "Approve"},
                           track=False)
    result = harness.sync().fetch()
    assert result["children"]["owners"] == 2
    root = harness.paths.mirror_workspace / "metadata" / "global"
    assert list((root / "sys_db_object").iterdir())  # "*" includes table definitions

    flow_dir = next((root / "sys_hub_flow").iterdir())
    steps = yaml.safe_load((flow_dir / "_children" / "sys_hub_action_instance_v2.yaml")
                           .read_text())
    assert steps["read_only"] and steps["rows"][0]["values"] == {"table": "incident"}
    wf_children = next((root / "wf_workflow").iterdir()) / "_children"
    assert (wf_children / "wf_workflow_version.yaml").is_file()
    assert "Approve" in (wf_children / "wf_activity.yaml").read_text()

    # Incremental: a changed nested child row refreshes its owner's files.
    fake.update(activity, {"name": "Approve by manager"}, by="alice")
    harness.sync().fetch()
    assert "Approve by manager" in (wf_children / "wf_activity.yaml").read_text()

    # Excluding a class keeps it out of the full mirror.
    harness.config.sync.baseline_exclude = ["sys_db_object"]
    harness.sync().fetch()
    assert not (root / "sys_db_object").exists()


def test_path_segments_tolerate_dirty_instance_values() -> None:
    assert path_segment(" 469106e153231010df5dddeeff7b12a") == "469106e153231010df5dddeeff7b12a"
    assert path_segment("x_acme app") == "x-acme-app"
    assert path_segment("") == "global"
    assert path_segment("..") == "record"


def test_legacy_sys_ids_are_mirrored(harness: Harness) -> None:
    harness.fake.insert("sys_script_include", {"name": "Legacy", "script": "var a;"},
                        sys_id="sysverb_query", track=False)
    harness.sync().fetch()
    folder = harness.paths.mirror_workspace / "metadata" / "global" / "sys_script_include" / (
        "legacy--sysverb_query"
    )
    assert read_record(folder).sys_id == "sysverb_query"
    assert SYS_ID.fullmatch("Default view")
    for bad in ("a^b", "a,b", "../x", "a-b", "x" * 65, "", " a", "a "):
        assert not SYS_ID.fullmatch(bad)
