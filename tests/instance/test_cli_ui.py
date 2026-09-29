from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest
from conftest import Harness

import snagentic.cli.instance as instance_cli
from snagentic.cli.main import main
from snagentic.errors import ConfigurationError, PolicyDeniedError
from snagentic.instance.config import InstanceRegistry
from snagentic.instance.ops import promote
from snagentic.instance.ui import UiRunner

REPOSITORY = Path(__file__).resolve().parents[2]


def cli(capsys: pytest.CaptureFixture[str], *args: str) -> dict[str, Any]:
    try:
        main(["--json", "instance", *args])
    except SystemExit as exc:
        out = json.loads(capsys.readouterr().err)
        out["exit"] = exc.code
        return out
    return json.loads(capsys.readouterr().out)


@pytest.fixture
def wired(harness: Harness, monkeypatch: pytest.MonkeyPatch) -> Harness:
    monkeypatch.setattr(instance_cli, "TRANSPORT", harness.fake.transport())
    return harness


def test_cli_fetch_integrate_plan_apply(wired: Harness, capsys: pytest.CaptureFixture[str]) -> None:
    fake = wired.fake
    scope = fake.create_scope("x_acme")
    fake.insert("sys_script_include", {"name": "Util", "sys_scope": scope, "script": "var a;"})
    assert cli(capsys, "list")["result"]["instances"][0]["name"] == "dev"
    fetched = cli(capsys, "fetch")
    assert fetched["ok"] and fetched["result"]["updated"] == 1
    assert cli(capsys, "status")["result"]["integrated"] is False
    assert cli(capsys, "integrate")["ok"]
    status = cli(capsys, "-i", "dev", "status")["result"]
    assert status["integrated"] is True and status["local_changes"] == []

    script = next(wired.paths.metadata.rglob("script.js"))
    script.write_text("var a = 2;\n")
    plan = cli(capsys, "plan")["result"]
    assert [change["operation"] for change in plan["changes"]] == ["update"]
    assert "_objects" not in plan
    denied = cli(capsys, "apply", "--plan-id", plan["plan_id"])
    assert denied["exit"] == 2 and "confirmation" in denied["error"]
    applied = cli(capsys, "apply", "--plan-id", plan["plan_id"], "--confirm")
    assert applied["ok"], applied
    assert cli(capsys, "search", "a = 2")["result"]["results"]
    assert "scopes/x_acme.md" in cli(capsys, "docs")["result"]["pages"]
    assert cli(capsys, "update-sets")["result"]["update_sets"]
    assert "summary" in cli(capsys, "collisions")["result"]
    assert cli(capsys, "ops-list")["result"]["operations"]


def test_cli_rejects_unknown_instance(wired: Harness, capsys: pytest.CaptureFixture[str]) -> None:
    result = cli(capsys, "-i", "nope", "status")
    assert result["exit"] == 2 and "not configured" in result["error"]


def test_cli_denies_writes_to_production_before_connecting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    InstanceRegistry(tmp_path).add("prod", url="https://prod.example.com/", kind="production")
    monkeypatch.delenv("SNAGENTIC_PROD_USERNAME", raising=False)
    for args in (("ops-run", "plugin.activate", "--param", "plugin_id=x", "--confirm"),
                 ("promote", "--confirm"),
                 ("apply", "--plan-id", "0" * 16, "--confirm")):
        result = cli(capsys, "-i", "prod", *args)
        assert result["exit"] == 2 and "denied" in result["error"], args


def test_ui_recipes_fall_back_to_bundled_catalog(harness: Harness) -> None:
    runner = UiRunner(harness.root, harness.paths, harness.config, environ={})
    assert runner.recipes_root == REPOSITORY / "ui" / "recipes"
    assert {recipe["name"] for recipe in runner.list_recipes()} >= {"login-check"}


def test_migrate_imports_legacy_environments(tmp_path: Path) -> None:
    from snagentic.config import load_config

    legacy = load_config(REPOSITORY / "config" / "snagentic.example.yaml")
    registry = InstanceRegistry(tmp_path)
    created = [registry.import_environment(env) for env in legacy.environments.values()]
    assert all(created)
    assert registry.names() == ["dev", "prod", "test"]
    assert registry.load("prod").kind == "production"
    assert registry.import_environment(legacy.environments["dev"]) is None


def test_example_instance_profile_is_valid() -> None:
    import yaml

    from snagentic.instance.config import InstanceConfig

    raw = yaml.safe_load((REPOSITORY / "config" / "instance.example.yaml").read_text())
    config = InstanceConfig.model_validate(raw)
    assert config.writable and config.ui_credential_names() == (
        "SNAGENTIC_DEV_USERNAME", "SNAGENTIC_DEV_PASSWORD")


def test_promote_completes_agent_sets_with_content_free_manifest(harness: Harness) -> None:
    fake = harness.fake
    own = fake.insert("sys_update_set", {"name": "snagentic: feature-x [x_acme]",
                                         "state": "in progress"})
    fake.insert("sys_update_set", {"name": "snagentic: other [x_acme]", "state": "in progress"})
    fake.insert("sys_update_xml", {"update_set": own, "type": "Script Include",
                                   "action": "INSERT_OR_UPDATE",
                                   "name": "sys_script_include_" + "a" * 32,
                                   "payload": "<xml>secret body</xml>"})
    with pytest.raises(PolicyDeniedError):
        promote(harness.config, harness.client, label="feature-x", confirm=False,
                created_at="2025-01-01T00:00:00+00:00")
    manifest = promote(harness.config, harness.client, label="feature-x", confirm=True,
                       created_at="2025-01-01T00:00:00+00:00")
    assert [item["name"] for item in manifest["update_sets"]] == ["snagentic: feature-x [x_acme]"]
    assert manifest["update_sets"][0]["changes_by_type"] == {"Script Include:INSERT_OR_UPDATE": 1}
    assert "secret body" not in json.dumps(manifest)
    assert fake.records[own]["state"] == "complete"


def test_promotion_paths_are_unique_per_label_at_same_timestamp(harness: Harness) -> None:
    created_at = "2026-09-24T16:55:44+00:00"
    first = instance_cli._promotion_destination(
        harness.paths, "acceptance-stale-winner", created_at
    )
    second = instance_cli._promotion_destination(
        harness.paths, "acceptance-collision-override", created_at
    )
    assert first != second
    assert first.name == "2026-09-24T165544+0000-acceptance-stale-winner.json"


class FakeProcess:
    def __init__(self, stdout: str, returncode: int = 0) -> None:
        self.calls: list[dict[str, Any]] = []
        self.stdout = stdout
        self.returncode = returncode

    def __call__(self, command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        self.calls.append({"command": command, **kwargs})
        return subprocess.CompletedProcess(command, self.returncode, self.stdout, "boom")


def _ui_repo(harness: Harness) -> Path:
    target = harness.root / "ui"
    import shutil

    shutil.copytree(REPOSITORY / "ui" / "recipes", target / "recipes")
    # Mark the workspace runner as installed so it wins over the checkout's own ui/.
    (target / "node_modules" / "playwright").mkdir(parents=True)
    return target


def test_ui_runner_policy_and_request(harness: Harness) -> None:
    _ui_repo(harness)
    environ = {"PATH": "/usr/bin", "SNAGENTIC_NODE": "node", "SNAGENTIC_DEV_USERNAME": "agent",
               "SNAGENTIC_DEV_PASSWORD": "pw-value", "UNRELATED_SECRET": "x"}
    process = FakeProcess(json.dumps({"ok": True, "result": {"activated": True}}))
    runner = UiRunner(harness.root, harness.paths, harness.config, runner=process,
                      environ=environ)
    names = {recipe["name"] for recipe in runner.list_recipes()}
    assert names == {
        "activate-plugin",
        "export-app-inventory",
        "incident-task-create",
        "install-app",
        "login-check",
        "upload-update-set",
    }

    dry = runner.run("activate-plugin", {"plugin_id": "com.snc.x"}, dry_run=True)
    assert dry["status"] == "dry_run" and not process.calls
    with pytest.raises(PolicyDeniedError):
        runner.run("activate-plugin", {"plugin_id": "com.snc.x"})
    result = runner.run("activate-plugin", {"plugin_id": "com.snc.x"}, confirm=True)
    assert result["status"] == "completed"
    call = process.calls[0]
    assert call["command"] == ["node", str(harness.root / "ui" / "src" / "cli.mjs")]
    assert "pw-value" not in " ".join(call["command"])
    assert "UNRELATED_SECRET" not in call["env"]
    assert not {"SNAGENTIC_DEV_USERNAME", "SNAGENTIC_DEV_PASSWORD"} & set(call["env"])
    request = json.loads(call["input"])
    assert request["repository_root"] == str(harness.root)
    assert request["credentials"] == {"username": "agent", "password": "pw-value"}
    persisted = next(harness.paths.ui_evidence.rglob("request.json")).read_text()
    assert "pw-value" not in persisted and "credentials" not in persisted
    assert '"password_env": "SNAGENTIC_DEV_PASSWORD"' in persisted

    prod = UiRunner(harness.root, harness.paths,
                    harness.config.model_copy(update={"kind": "production"}),
                    runner=process, environ=environ)
    with pytest.raises(PolicyDeniedError):
        prod.run("activate-plugin", {"plugin_id": "com.snc.x"}, dry_run=True)
    prod.run("login-check", {})


def test_ui_runner_saves_complete_application_inventory(harness: Harness) -> None:
    _ui_repo(harness)
    environ = {
        "PATH": "/usr/bin",
        "SNAGENTIC_NODE": "node",
        "SNAGENTIC_DEV_USERNAME": "agent",
        "SNAGENTIC_DEV_PASSWORD": "pw-value",
    }
    inventory = {
        "complete": True,
        "displayed_count": 2,
        "duplicate_count": 1,
        "records": [{
            "sys_id": "1" * 31 + "q",
            "scope": "x_acme_app",
            "name": "Acme App",
            "version": "1.2.3",
            "vendor": "Acme",
            "short_description": "Safe application metadata",
        }],
    }
    runner = UiRunner(
        harness.root,
        harness.paths,
        harness.config,
        runner=FakeProcess(json.dumps({"ok": True, "result": inventory})),
        environ=environ,
    )

    runner.run("export-app-inventory", {})

    saved = json.loads(
        (harness.paths.state / "operational/sys_store_app.json").read_text()
    )
    assert saved["source"] == "application_manager"
    assert saved["complete"] is True
    assert saved["displayed_count"] == 2
    assert saved["duplicate_count"] == 1
    assert saved["records"] == inventory["records"]


def test_ui_runner_file_parameters_stay_in_repository(harness: Harness) -> None:
    _ui_repo(harness)
    (harness.root / "exports").mkdir()
    (harness.root / "exports" / "set.xml").write_text("<unload/>")
    runner = UiRunner(harness.root, harness.paths, harness.config, runner=FakeProcess("{}"),
                      environ={})
    plan = runner.run("upload-update-set", {"file": "exports/set.xml"}, dry_run=True)
    assert plan["parameters"]["file"] == "exports/set.xml"
    for bad in ("../outside.xml", ".git/config", "instances/dev/instance.yaml"):
        with pytest.raises(ValueError):
            runner.run("upload-update-set", {"file": bad}, dry_run=True)
    with pytest.raises(ConfigurationError, match="missing UI credential"):
        runner.run("upload-update-set", {"file": "exports/set.xml"}, confirm=True)


def test_ui_runner_reads_login_from_the_credential_store(
    harness: Harness, isolated_credential_store: Any,
) -> None:
    from snagentic import credentials

    _ui_repo(harness)
    url = str(harness.config.url)
    credentials.store_secret(url, "SNAGENTIC_DEV_USERNAME", "keychain-user")
    credentials.store_secret(url, "SNAGENTIC_DEV_PASSWORD", "keychain-pw")
    config = harness.config.model_copy(
        update={"auth": harness.config.auth.model_copy(update={"store": "keychain"})}
    )
    process = FakeProcess(json.dumps({"ok": True, "result": {}}))
    environ = {"PATH": "/usr/bin", "SNAGENTIC_NODE": "/opt/node/bin/node",
               "SNAGENTIC_DEV_PASSWORD": "stale-env-value"}
    runner = UiRunner(harness.root, harness.paths, config, runner=process, environ=environ)
    runner.run("login-check", {})
    call = process.calls[0]
    assert json.loads(call["input"])["credentials"] == {
        "username": "keychain-user", "password": "keychain-pw"}
    assert "keychain-pw" not in json.dumps(call["env"]) + " ".join(call["command"])


def test_ui_runner_docker_mode_keeps_credentials_off_the_command_line(harness: Harness) -> None:
    _ui_repo(harness)
    environ = {"PATH": "/usr/bin", "SNAGENTIC_UI_RUNNER": "docker",
               "SNAGENTIC_DEV_USERNAME": "agent", "SNAGENTIC_DEV_PASSWORD": "pw-value"}
    process = FakeProcess(json.dumps({"ok": True, "result": {}}))
    runner = UiRunner(harness.root, harness.paths, harness.config, runner=process,
                      environ=environ)
    runner.run("login-check", {})
    call = process.calls[0]
    assert call["command"][1:] == ["compose", "run", "--rm", "-T", "ui"]
    assert not {"SNAGENTIC_DEV_USERNAME", "SNAGENTIC_DEV_PASSWORD"} & set(call["env"])
    request = json.loads(call["input"])
    assert request["repository_root"] == "/workspace"
    assert request["credentials"]["password"] == "pw-value"


def test_ui_runner_rejects_unknown_modes_and_missing_node(
    harness: Harness, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from snagentic import resources
    from snagentic.instance import ui

    _ui_repo(harness)
    environ = {"PATH": "/nonexistent", "SNAGENTIC_DEV_USERNAME": "agent",
               "SNAGENTIC_DEV_PASSWORD": "pw-value"}
    for mode, message in (("podman", "must be 'node' or 'docker'"),
                          ("node", "no Node.js runtime")):
        runner = UiRunner(harness.root, harness.paths, harness.config,
                          runner=FakeProcess("{}"),
                          environ={**environ, "SNAGENTIC_UI_RUNNER": mode})
        with pytest.raises(ConfigurationError, match=message):
            runner.run("login-check", {})
    with pytest.raises(ConfigurationError, match="simple executable name"):
        ui.node_executable({"SNAGENTIC_NODE": "node --inspect"})
    bundled = harness.root / "bundle" / "runtime" / "node"
    bundled.parent.mkdir(parents=True)
    bundled.write_text("")
    monkeypatch.setattr(resources, "bundled_node", lambda: bundled)
    assert ui.node_executable({"PATH": "/nonexistent"}) == str(bundled)


def test_frozen_ui_runs_use_the_managed_browser_directory(
    harness: Harness, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import sys

    from snagentic import resources

    _ui_repo(harness)
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    environ = {"PATH": "/usr/bin", "HOME": str(harness.root / "home"),
               "LOCALAPPDATA": str(harness.root / "home"), "SNAGENTIC_NODE": "node"}
    process = FakeProcess(json.dumps({"ok": True, "result": {}}))
    runner = UiRunner(harness.root, harness.paths, harness.config, runner=process,
                      environ=environ, resolver=lambda name: "value")
    runner.run("login-check", {})
    assert process.calls[0]["env"]["PLAYWRIGHT_BROWSERS_PATH"] == str(
        resources.playwright_browsers_dir(environ))


def test_ui_app_dir_prefers_a_runner_with_playwright(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from snagentic import resources
    from snagentic.instance.ui import ui_app_dir

    workspace = tmp_path / "work"
    (workspace / "ui" / "recipes").mkdir(parents=True)
    bundled = tmp_path / "bundle" / "ui"
    (bundled / "recipes").mkdir(parents=True)
    monkeypatch.setattr(resources, "asset_dir", lambda name: bundled)
    environ = {"HOME": str(tmp_path / "home"), "LOCALAPPDATA": str(tmp_path / "home")}

    assert ui_app_dir(workspace, environ) == workspace / "ui"
    (bundled / "node_modules" / "playwright").mkdir(parents=True)
    assert ui_app_dir(workspace, environ) == bundled
    (workspace / "ui" / "node_modules" / "playwright").mkdir(parents=True)
    assert ui_app_dir(workspace, environ) == workspace / "ui"
    assert ui_app_dir(tmp_path / "empty", environ) == bundled
