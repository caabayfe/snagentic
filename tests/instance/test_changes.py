from __future__ import annotations

import shutil
import subprocess

import pytest
import yaml
from conftest import Harness

from snagentic.errors import ConflictError, PolicyDeniedError
from snagentic.instance.changes import ChangePlanner, UpdateSetWriter
from snagentic.instance.config import InstanceRegistry
from snagentic.instance.mirror import MirrorRepository
from snagentic.instance.sync import operational_identity


def _setup(harness: Harness) -> str:
    sys_id = harness.fake.insert(
        "sys_script_include",
        {"name": "Greeter", "script": "function greet() { return 'hi'; }", "active": "true"},
    )
    harness.sync().fetch()
    MirrorRepository(harness.paths, harness.config.mirror_branch).integrate(message="integrate")
    return sys_id


def _script_path(harness: Harness, sys_id: str):
    return harness.paths.metadata / "global" / "sys_script_include" / f"greeter--{sys_id}" / (
        "script.js"
    )


def test_integrate_brings_remote_into_working_branch(harness: Harness) -> None:
    sys_id = _setup(harness)
    assert _script_path(harness, sys_id).read_text() == "function greet() { return 'hi'; }\n"
    assert harness.git("status", "--porcelain") == ""
    plan = ChangePlanner(harness.paths, harness.config).plan()
    assert plan["changes"] == []


def test_plan_ignores_operational_table_edits_and_deletes(harness: Harness) -> None:
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
    MirrorRepository(harness.paths, harness.config.mirror_branch).integrate(message="integrate")
    plugin_identity = operational_identity("v_plugin", harness.fake.records[plugin])
    app_identity = operational_identity("sys_store_app", harness.fake.records[app])

    plugin_dir = next(
        path.parent for path in harness.paths.metadata.rglob("_meta.yaml")
        if path.parent.name.endswith(plugin_identity)
    )
    app_dir = next(
        path.parent for path in harness.paths.metadata.rglob("_meta.yaml")
        if path.parent.name.endswith(app_identity)
    )
    plugin_record = yaml.safe_load((plugin_dir / "record.yaml").read_text())
    plugin_record["active"] = "active"
    (plugin_dir / "record.yaml").write_text(yaml.safe_dump(plugin_record, sort_keys=True))
    shutil.rmtree(app_dir)

    plan = ChangePlanner(harness.paths, harness.config).plan()

    assert plan["changes"] == []
    assert sorted(plan["ignored_read_only"]) == sorted([
        plugin_dir.relative_to(harness.root).as_posix(),
        app_dir.relative_to(harness.root).as_posix(),
    ])


def test_integrate_refuses_dirty_workspace(harness: Harness) -> None:
    sys_id = _setup(harness)
    _script_path(harness, sys_id).write_text("dirty\n")
    harness.fake.update(sys_id, {"script": "remote change"}, by="alice")
    harness.sync().fetch()
    with pytest.raises(ConflictError, match="commit or stash"):
        MirrorRepository(harness.paths, harness.config.mirror_branch).integrate(message="m")


def test_integrate_reports_success_when_git_fails_after_landing_the_merge(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """git merge can advance HEAD and still exit non-zero (e.g. a post-merge checkout
    step tripping over stray untracked content). integrate() must not report failure
    once the mirror tip is actually reachable from HEAD."""
    sys_id = harness.fake.insert(
        "sys_script_include",
        {"name": "Greeter", "script": "function greet() { return 'hi'; }", "active": "true"},
    )
    harness.sync().fetch()
    repo = MirrorRepository(harness.paths, harness.config.mirror_branch)

    from snagentic.instance import mirror as mirror_module

    real_git = mirror_module.git
    calls: list[tuple[str, ...]] = []

    def flaky_git(root, *args, **kwargs):  # type: ignore[no-untyped-def]
        result = real_git(root, *args, **kwargs)
        calls.append(args)
        if args and args[0] == "merge":
            return subprocess.CompletedProcess(
                args=result.args,
                returncode=1,
                stdout=result.stdout,
                stderr="fatal: stash failed",
            )
        return result

    monkeypatch.setattr(mirror_module, "git", flaky_git)

    result = repo.integrate(message="m")

    assert result["status"] == "merged"
    assert result["warning"] == "fatal: stash failed"
    assert _script_path(harness, sys_id).read_text() == "function greet() { return 'hi'; }\n"
    assert any(args and args[0] == "merge" for args in calls)


def test_conflicting_remote_and_local_edits_produce_git_markers(harness: Harness) -> None:
    sys_id = _setup(harness)
    _script_path(harness, sys_id).write_text("function greet() { return 'local'; }\n")
    harness.git("commit", "-qam", "local edit")
    harness.fake.update(sys_id, {"script": "function greet() { return 'remote'; }"}, by="alice")
    harness.sync().fetch()
    result = MirrorRepository(harness.paths, harness.config.mirror_branch).integrate(message="m")
    assert result["status"] == "conflicts"
    content = _script_path(harness, sys_id).read_text()
    assert "<<<<<<<" in content and "'remote'" in content and "'local'" in content
    with pytest.raises(ConflictError):
        ChangePlanner(harness.paths, harness.config).plan()
    harness.git("add", "-A")
    harness.git("commit", "-qm", "committed with markers")
    with pytest.raises(ConflictError, match="conflict markers"):
        ChangePlanner(harness.paths, harness.config).plan()


def test_plan_requires_integration(harness: Harness) -> None:
    sys_id = _setup(harness)
    harness.fake.update(sys_id, {"script": "newer"}, by="alice")
    harness.sync().fetch()
    with pytest.raises(ConflictError, match="integrate"):
        ChangePlanner(harness.paths, harness.config).plan()


def test_apply_update_lands_in_agent_update_set(harness: Harness) -> None:
    sys_id = _setup(harness)
    _script_path(harness, sys_id).write_text("function greet() { return 'hello'; }\n")
    plan = ChangePlanner(harness.paths, harness.config).plan()
    assert [change["operation"] for change in plan["changes"]] == ["update"]
    assert plan["changes"][0]["fields"] == ["script"]

    result = UpdateSetWriter(harness.paths, harness.config, harness.client).apply(
        plan_id=plan["plan_id"], confirm=True
    )

    assert result["status"] == "applied"
    assert result["not_captured"] == []
    remote = harness.fake.records[sys_id]
    assert remote["script"] == "function greet() { return 'hello'; }"
    update_set = result["update_sets"]["global"]
    assert update_set["name"] == "snagentic: main [global]"
    captured = [
        row for row in harness.fake.records.values()
        if row["sys_class_name"] == "sys_update_xml" and row["update_set"] == update_set["sys_id"]
    ]
    assert [row["name"] for row in captured] == [f"sys_script_include_{sys_id}"]
    # Preference restored (created then removed).
    assert not any(
        row["sys_class_name"] == "sys_user_preference" for row in harness.fake.records.values()
    )
    harness.git("add", "-A")
    harness.git("commit", "-qm", "agent change")
    merged = MirrorRepository(harness.paths, harness.config.mirror_branch).integrate(message="m")
    assert merged["status"] == "merged"
    assert ChangePlanner(harness.paths, harness.config).plan()["changes"] == []


def test_allowlisted_property_value_can_be_planned_and_applied(harness: Harness) -> None:
    name = "glide.incident.close.code"
    harness.config = harness.config.model_copy(update={
        "sync": harness.config.sync.model_copy(update={"property_value_allowlist": [name]})
    })
    sys_id = harness.fake.insert("sys_properties", {"name": name, "value": "7"})
    harness.sync().fetch()
    MirrorRepository(harness.paths, harness.config.mirror_branch).integrate(message="integrate")
    directory = next((harness.paths.metadata / "global" / "sys_properties").iterdir())
    record = yaml.safe_load((directory / "record.yaml").read_text())
    record["value"] = "6"
    (directory / "record.yaml").write_text(yaml.safe_dump(record, sort_keys=True))

    plan = ChangePlanner(harness.paths, harness.config).plan()
    assert plan["changes"][0]["fields"] == ["value"]
    UpdateSetWriter(harness.paths, harness.config, harness.client).apply(
        plan_id=plan["plan_id"], confirm=True
    )
    assert harness.fake.records[sys_id]["value"] == "6"


def test_redacted_property_value_change_is_denied(harness: Harness) -> None:
    harness.fake.insert("sys_properties", {"name": "glide.incident.close.code", "value": "7"})
    harness.sync().fetch()
    MirrorRepository(harness.paths, harness.config.mirror_branch).integrate(message="integrate")
    directory = next((harness.paths.metadata / "global" / "sys_properties").iterdir())
    record = yaml.safe_load((directory / "record.yaml").read_text())
    record["value"] = "6"
    (directory / "record.yaml").write_text(yaml.safe_dump(record, sort_keys=True))

    with pytest.raises(PolicyDeniedError, match="property_value_allowlist"):
        ChangePlanner(harness.paths, harness.config).plan()


def test_apply_create_and_delete(harness: Harness) -> None:
    sys_id = _setup(harness)
    shutil.rmtree(_script_path(harness, sys_id).parent)
    new_dir = harness.paths.metadata / "global" / "sys_script_include" / "new-helper"
    new_dir.mkdir(parents=True)
    (new_dir / "record.yaml").write_text(yaml.safe_dump({"name": "NewHelper", "active": "true"}))
    (new_dir / "script.js").write_text("var NewHelper = {};\n")
    plan = ChangePlanner(harness.paths, harness.config).plan()
    assert sorted(change["operation"] for change in plan["changes"]) == ["create", "delete"]

    result = UpdateSetWriter(harness.paths, harness.config, harness.client).apply(
        plan_id=plan["plan_id"], confirm=True
    )

    assert sys_id not in harness.fake.records
    created = next(item for item in result["applied"] if item["operation"] == "create")
    assert harness.fake.records[created["sys_id"]]["script"] == "var NewHelper = {};"
    assert not new_dir.exists()
    canonical = harness.paths.metadata / "global" / "sys_script_include" / (
        f"newhelper--{created['sys_id']}"
    )
    assert (canonical / "_meta.yaml").is_file()
    harness.git("add", "-A")
    harness.git("commit", "-qm", "agent change")
    MirrorRepository(harness.paths, harness.config.mirror_branch).integrate(message="m")
    assert ChangePlanner(harness.paths, harness.config).plan()["changes"] == []


def test_apply_detects_remote_drift(harness: Harness) -> None:
    sys_id = _setup(harness)
    _script_path(harness, sys_id).write_text("local\n")
    plan = ChangePlanner(harness.paths, harness.config).plan()
    harness.fake.update(sys_id, {"script": "sneaky"}, by="alice")
    with pytest.raises(ConflictError, match="changed remotely"):
        UpdateSetWriter(harness.paths, harness.config, harness.client).apply(
            plan_id=plan["plan_id"], confirm=True
        )
    assert harness.fake.records[sys_id]["script"] == "sneaky"
    assert (harness.paths.state / "pending-apply.json").is_file()


def test_apply_blocks_collisions_with_other_open_update_sets(harness: Harness) -> None:
    sys_id = harness.fake.insert("sys_script_include", {"name": "Greeter", "script": "a"})
    harness.fake.open_update_set("alice work", by="alice")
    harness.fake.update(sys_id, {"script": "alice"}, by="alice")
    harness.sync().fetch()
    MirrorRepository(harness.paths, harness.config.mirror_branch).integrate(message="m")
    _script_path(harness, sys_id).write_text("mine\n")
    plan = ChangePlanner(harness.paths, harness.config).plan()
    assert plan["collisions"][0]["holders"][0]["user"] == "alice"
    with pytest.raises(ConflictError, match="other open update sets"):
        UpdateSetWriter(harness.paths, harness.config, harness.client).apply(
            plan_id=plan["plan_id"], confirm=True
        )


def test_apply_requires_confirmation_matching_plan_and_dev(harness: Harness) -> None:
    sys_id = _setup(harness)
    _script_path(harness, sys_id).write_text("x\n")
    plan = ChangePlanner(harness.paths, harness.config).plan()
    writer = UpdateSetWriter(harness.paths, harness.config, harness.client)
    with pytest.raises(PolicyDeniedError):
        writer.apply(plan_id=plan["plan_id"], confirm=False)
    with pytest.raises(ConflictError, match="plan changed"):
        writer.apply(plan_id="0" * 16, confirm=True)
    prod = harness.config.model_copy(update={"kind": "production"})
    with pytest.raises(PolicyDeniedError):
        UpdateSetWriter(harness.paths, prod, harness.client).apply(
            plan_id=plan["plan_id"], confirm=True
        )


def test_registry_rejects_ambiguous_default(harness: Harness) -> None:
    registry = InstanceRegistry(harness.root)
    registry.add("prod", url="https://prod.example.service-now.com/", kind="production")
    with pytest.raises(Exception, match="pass --instance"):
        registry.load(None)
    assert registry.load("prod").auth.username_env == "SNAGENTIC_PROD_USERNAME"


def test_failed_fetch_does_not_poison_the_plan_base(harness: Harness, monkeypatch) -> None:
    sys_id = _setup(harness)
    harness.fake.update(sys_id, {"script": "function greet() { return 'remote'; }"}, by="alice")
    sync = harness.sync()

    def boom(*_args, **_kwargs):
        raise RuntimeError("network down")

    monkeypatch.setattr(type(sync), "refresh_update_sets", boom)
    with pytest.raises(RuntimeError):
        sync.fetch()
    monkeypatch.undo()
    # The half-written mirror tree must be discarded: no spurious reverts or deletes.
    assert ChangePlanner(harness.paths, harness.config).plan()["changes"] == []


def test_partial_apply_is_reconciled_by_fetch(harness: Harness, monkeypatch) -> None:
    _setup(harness)
    base = harness.paths.metadata / "global" / "sys_script_include"
    for name in ("first", "second"):
        folder = base / f"new-{name}"
        folder.mkdir(parents=True)
        (folder / "record.yaml").write_text(yaml.safe_dump({"name": f"New{name}"}))
        (folder / "script.js").write_text(f"var New{name} = {{}};\n")
    plan = ChangePlanner(harness.paths, harness.config).plan()
    assert len(plan["changes"]) == 2

    original = UpdateSetWriter._apply_one
    calls = {"n": 0}

    def flaky(self, *args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("second insert failed")
        return original(self, *args, **kwargs)

    monkeypatch.setattr(UpdateSetWriter, "_apply_one", flaky)
    with pytest.raises(RuntimeError):
        UpdateSetWriter(harness.paths, harness.config, harness.client).apply(
            plan_id=plan["plan_id"], confirm=True
        )
    monkeypatch.undo()
    created = [r for r in harness.fake.records.values() if r.get("name", "").startswith("New")]
    assert len(created) == 1

    harness.sync().fetch()
    assert not (harness.paths.state / "pending-apply.json").exists()
    harness.git("add", "-A")
    harness.git("commit", "-qm", "reconciled")
    MirrorRepository(harness.paths, harness.config.mirror_branch).integrate(message="m")
    remaining = ChangePlanner(harness.paths, harness.config).plan()["changes"]
    assert [change["operation"] for change in remaining] == ["create"]


def test_branch_with_slash_yields_valid_label(harness: Harness) -> None:
    from snagentic.instance.changes import _branch_label, validate_label

    harness.git("checkout", "-qb", "feature/foo")
    label = _branch_label(harness.paths.root)
    assert label == "feature-foo"
    assert validate_label(label) == label
    with pytest.raises(ValueError):
        validate_label("feature/foo")


def test_scoped_plan_matches_full_scan(harness: Harness, monkeypatch) -> None:
    keep = harness.fake.insert("sys_script_include", {"name": "Keep", "script": "var k;"})
    move = harness.fake.insert("sys_script_include", {"name": "Mover", "script": "var m;"})
    sys_id = _setup(harness)
    root = harness.paths.metadata / "global" / "sys_script_include"
    _script_path(harness, sys_id).write_text("function greet() { return 'changed'; }\n")
    harness.git("commit", "-qam", "committed local edit")
    shutil.rmtree(root / f"keep--{keep}")
    (root / f"mover--{move}").rename(root / f"renamed--{move}")
    new_dir = root / "fresh"
    new_dir.mkdir()
    (new_dir / "record.yaml").write_text(yaml.safe_dump({"name": "Fresh"}))

    planner = ChangePlanner(harness.paths, harness.config)
    assert planner._changed_record_dirs() is not None
    scoped = planner.plan()["changes"]
    monkeypatch.setattr(ChangePlanner, "_changed_record_dirs", lambda self: None)
    full = ChangePlanner(harness.paths, harness.config).plan()["changes"]

    def summary(changes):
        return sorted((c["operation"], c.get("sys_id") or "", c["path"]) for c in changes)

    assert summary(scoped) == summary(full)
    assert sorted(c["operation"] for c in scoped) == ["create", "delete", "update"]


def test_apply_moves_updates_captured_in_default(harness: Harness) -> None:
    sys_id = _setup(harness)
    other = harness.fake.insert("sys_script_include", {"name": "Other", "script": "x"}, by="alice")
    harness.fake.ignore_preferences = True
    _script_path(harness, sys_id).write_text("function greet() { return 'hello'; }\n")
    plan = ChangePlanner(harness.paths, harness.config).plan()

    result = UpdateSetWriter(harness.paths, harness.config, harness.client).apply(
        plan_id=plan["plan_id"], confirm=True
    )

    name = f"sys_script_include_{sys_id}"
    assert result["not_captured"] == []
    assert result["moved_to_update_set"] == [name]
    rows = {
        row["name"]: row["update_set"]
        for row in harness.fake.records.values()
        if row["sys_class_name"] == "sys_update_xml"
    }
    assert rows[name] == result["update_sets"]["global"]["sys_id"]
    assert rows[f"sys_script_include_{other}"] == harness.fake.default_update_set
