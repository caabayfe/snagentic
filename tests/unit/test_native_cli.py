from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

import snagentic.cli.native as native
from snagentic import __version__, credentials, resources
from snagentic.cli.main import main
from snagentic.instance.config import InstanceRegistry

REPOSITORY = Path(__file__).resolve().parents[2]


def run(capsys: pytest.CaptureFixture[str], *args: str) -> dict[str, Any]:
    try:
        main(["--json", *args])
    except SystemExit as exc:
        out = json.loads(capsys.readouterr().err)
        out["exit"] = exc.code
        return out
    return json.loads(capsys.readouterr().out)


@pytest.fixture
def workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.chdir(tmp_path)
    for name in ("SNAGENTIC_DEV_USERNAME", "SNAGENTIC_DEV_PASSWORD"):
        monkeypatch.delenv(name, raising=False)
    InstanceRegistry(tmp_path).add("dev", url="https://dev.example.service-now.com/",
                                   kind="development", store="keychain")
    return tmp_path


def test_version_flag(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        main(["--version"])
    assert exc.value.code == 0
    assert capsys.readouterr().out.strip() == f"snagentic {__version__}"


def test_auth_login_status_logout_never_print_values(
    workspace: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch,
    isolated_credential_store: credentials.MemoryBackend,
) -> None:
    prompts: list[tuple[str, bool]] = []

    def answer(prompt: str, secret: bool) -> str:
        prompts.append((prompt, secret))
        return "agent-user" if not secret else "top-secret-pw"

    monkeypatch.setattr(native, "READ_VALUE", answer)
    before = run(capsys, "auth", "status")["result"]
    assert [row["keychain"] for row in before["credentials"]] == [False, False]

    login = run(capsys, "auth", "login", "-i", "dev")
    assert login["result"]["stored"] == ["SNAGENTIC_DEV_USERNAME", "SNAGENTIC_DEV_PASSWORD"]
    assert [secret for _, secret in prompts] == [False, True]
    assert "top-secret-pw" not in json.dumps(login) and "agent-user" not in json.dumps(login)
    assert isolated_credential_store.values[
        ("snagentic", "dev.example.service-now.com/SNAGENTIC_DEV_PASSWORD")] == "top-secret-pw"

    status = run(capsys, "auth", "status", "-i", "dev")["result"]
    assert status["store"] == "keychain" and status["host"] == "dev.example.service-now.com"
    assert all(row["keychain"] for row in status["credentials"])
    assert "top-secret-pw" not in json.dumps(status)

    only = run(capsys, "auth", "logout", "--name", "SNAGENTIC_DEV_PASSWORD")["result"]
    assert only["removed"] == ["SNAGENTIC_DEV_PASSWORD"]
    rest = run(capsys, "auth", "logout")["result"]
    assert rest["removed"] == ["SNAGENTIC_DEV_USERNAME"]
    unknown = run(capsys, "auth", "status", "--name", "SNAGENTIC_OTHER_TOKEN")
    assert unknown["exit"] == 2 and "does not use" in unknown["error"]


def test_auth_login_migrates_from_environment(
    workspace: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch,
) -> None:
    missing = run(capsys, "auth", "login", "--from-env")
    assert missing["exit"] == 2 and "SNAGENTIC_DEV_USERNAME" in missing["error"]
    monkeypatch.setenv("SNAGENTIC_DEV_USERNAME", "u")
    monkeypatch.setenv("SNAGENTIC_DEV_PASSWORD", "migrated-pw")
    result = run(capsys, "auth", "login", "--from-env")["result"]
    assert result["stored"] == ["SNAGENTIC_DEV_USERNAME", "SNAGENTIC_DEV_PASSWORD"]
    assert "remove these variables" in result["next_step"]
    assert credentials.resolve_secret(
        "SNAGENTIC_DEV_PASSWORD", url="https://dev.example.service-now.com/",
        store="keychain", environ={}) == "migrated-pw"


def test_auth_refuses_env_only_profiles_and_empty_values(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    InstanceRegistry(tmp_path).add("legacy", url="https://legacy.example.com/",
                                   kind="development", store="env")
    refused = run(capsys, "auth", "login")
    assert refused["exit"] == 2 and "auth.store: env" in refused["error"]
    InstanceRegistry(tmp_path).add("dev", url="https://dev.example.com/", kind="development")
    monkeypatch.setattr(native, "READ_VALUE", lambda prompt, secret: "")
    empty = run(capsys, "auth", "login", "-i", "dev")
    assert empty["exit"] == 2 and "no value entered" in empty["error"]


def test_auth_supports_legacy_configuration(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    config = tmp_path / "config" / "snagentic.yaml"
    config.parent.mkdir()
    config.write_text(
        "default_environment: dev\nenvironments:\n  dev:\n    name: dev\n"
        "    url: https://legacy-dev.example.com/\n    kind: development\n"
        "    auth:\n      mode: bearer\n      token_env: SNAGENTIC_DEV_TOKEN\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(native, "READ_VALUE", lambda prompt, secret: "bearer-value")
    result = run(capsys, "auth", "login")["result"]
    assert result == {"target": "environment dev", "host": "legacy-dev.example.com",
                      "stored": ["SNAGENTIC_DEV_TOKEN"],
                      "backend": "snagentic.credentials.MemoryBackend"}
    profile = run(capsys, "profile")["result"]
    assert profile == {"environment": "dev", "kind": "development",
                       "credential_names": ["SNAGENTIC_DEV_TOKEN"], "store": "auto"}


def test_instance_profile_lists_names_without_values(
    workspace: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    profile = run(capsys, "instance", "profile")["result"]
    assert profile == {
        "instance": "dev", "kind": "development", "store": "keychain",
        "credential_names": ["SNAGENTIC_DEV_PASSWORD", "SNAGENTIC_DEV_USERNAME"],
    }


def test_instance_add_records_store_and_next_step(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    added = run(capsys, "instance", "add", "acme", "--url", "https://acme.example.com/",
                "--kind", "development", "--credential-store", "keychain")["result"]
    assert added["next_step"] == "snagentic auth login -i acme"
    assert "store: keychain" in (tmp_path / "instances/acme/instance.yaml").read_text()


def test_copilot_install_status_upgrade_and_uninstall(
    tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    executable = tmp_path / "bin" / "snagentic"
    executable.parent.mkdir()
    executable.write_text("#!/bin/sh\n")
    target = tmp_path / "extensions"

    installed = run(capsys, "copilot", "install", "--target", str(target),
                    "--executable", str(executable))["result"]
    destination = target / "snagentic"
    assert installed["installed"] == str(destination)
    assert set(installed["files"]) == {"extension.mjs", "instance.mjs", "lib.mjs"}
    marker = json.loads((destination / "snagentic-runtime.json").read_text())
    assert marker["executable"] == str(executable)
    assert marker["protocol"] == resources.PROTOCOL_VERSION

    status = run(capsys, "copilot", "status", "--target", str(target))["result"]
    assert status["installed"] and status["current"] and status["executable_exists"]
    assert status["protocol_compatible"]

    (destination / "lib.mjs").write_text("// modified\n")
    assert run(capsys, "copilot", "status", "--target", str(target))["result"]["current"] is False
    run(capsys, "copilot", "install", "--target", str(target), "--executable", str(executable))
    assert run(capsys, "copilot", "status", "--target", str(target))["result"]["current"] is True
    assert not list(target.glob(".snagentic-*"))

    assert run(capsys, "copilot", "uninstall", "--target", str(target))["result"]["removed"]
    assert not destination.exists()
    again = run(capsys, "copilot", "uninstall", "--target", str(target))["result"]
    assert again["removed"] is False


def test_copilot_install_protects_foreign_extensions(
    tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    executable = tmp_path / "snagentic"
    executable.write_text("")
    foreign = tmp_path / "extensions" / "snagentic"
    foreign.mkdir(parents=True)
    (foreign / "extension.mjs").write_text("// someone else's\n")
    refused = run(capsys, "copilot", "install", "--target", str(tmp_path / "extensions"),
                  "--executable", str(executable))
    assert refused["exit"] == 2 and "--force" in refused["error"]
    removed = run(capsys, "copilot", "uninstall", "--target", str(tmp_path / "extensions"))
    assert removed["exit"] == 2 and foreign.exists()
    missing = run(capsys, "copilot", "install", "--target", str(tmp_path / "x"),
                  "--executable", str(tmp_path / "missing"))
    assert missing["exit"] == 2 and "not found" in missing["error"]
    forced = run(capsys, "copilot", "install", "--target", str(tmp_path / "extensions"),
                 "--executable", str(executable), "--force")
    assert forced["ok"]


def test_copilot_status_reports_project_shadowing() -> None:
    status = native.copilot_status(Path("/nonexistent/extensions"), REPOSITORY)
    assert status["installed"] is False and status["shadowed_by_project_extension"] is True


def test_copilot_home_honours_environment(tmp_path: Path) -> None:
    assert resources.copilot_home({"COPILOT_HOME": str(tmp_path)}) == tmp_path
    assert resources.copilot_home({"HOME": str(tmp_path)}) == tmp_path / ".copilot"


def test_doctor_local_reports_runtime(
    workspace: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SNAGENTIC_NODE", "definitely-not-installed-node")
    report = run(capsys, "doctor", "--local")["result"]
    assert report["version"] == __version__ and report["native"] is False
    assert report["credential_store"]["default_store"] == "auto"
    assert report["ui"]["runner"] == "node" and report["ui"]["ready"] is False
    assert "copilot_extension" in report


class RecordingRun:
    def __init__(self, returncode: int = 0) -> None:
        self.calls: list[dict[str, Any]] = []
        self.returncode = returncode

    def __call__(self, command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        self.calls.append({"command": command, **kwargs})
        return subprocess.CompletedProcess(command, self.returncode, "", "npm ERR! offline")


def test_ui_install_uses_managed_browser_directory(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "home"))
    monkeypatch.setenv("SNAGENTIC_NODE", "/opt/node/bin/node")
    monkeypatch.delenv("PLAYWRIGHT_BROWSERS_PATH", raising=False)
    recorder = RecordingRun()
    monkeypatch.setattr(native.subprocess, "run", recorder)
    monkeypatch.setattr(native.shutil, "which", lambda name: f"/usr/bin/{name}")

    result = run(capsys, "ui", "install")["result"]
    managed = resources.playwright_browsers_dir()
    assert result["browsers_path"] == str(managed)
    commands = [call["command"] for call in recorder.calls]
    if (Path(result["app"]) / "node_modules" / "playwright").is_dir():
        assert len(commands) == 1
    else:
        assert commands[0][:2] == ["/usr/bin/npm", "ci"] and "--omit=dev" in commands[0]
    assert commands[-1][0] == "/opt/node/bin/node"
    assert commands[-1][-2:] == ["install", "chromium"]
    assert recorder.calls[-1]["env"]["PLAYWRIGHT_BROWSERS_PATH"] == str(managed)

    failing = RecordingRun(returncode=1)
    monkeypatch.setattr(native.subprocess, "run", failing)
    failed = run(capsys, "ui", "install")
    assert failed["exit"] == 2 and "npm ERR! offline" in failed["error"]


def test_ui_status_reports_missing_playwright(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", str(tmp_path / "browsers"))
    monkeypatch.setenv("SNAGENTIC_NODE", "definitely-not-installed-node")
    status = run(capsys, "ui", "status")["result"]
    assert status["browsers_source"] == "environment"
    assert status["chromium_installed"] is False and status["node_version"] is None


class FakeCopilot:
    """Simulates the `copilot plugin` commands used by the installer."""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        self.homes: set[str] = set()
        self.markets: dict[str, str] = {}
        self.plugins: dict[str, bool] = {}
        self.fail: str | None = None

    def __call__(self, argv: list[str], env: dict[str, str]) -> tuple[int, str]:
        self.calls.append(argv)
        self.homes.add(env["COPILOT_HOME"])
        command = " ".join(argv)
        if self.fail and command.startswith(self.fail):
            return 1, "boom"
        if argv[:3] == ["plugin", "list", "--json"]:
            return 0, json.dumps([{"name": n.split("@")[0], "marketplace": n.split("@")[1],
                                   "enabled": e} for n, e in self.plugins.items()])
        if argv[:4] == ["plugin", "marketplace", "list", "--json"]:
            return 0, json.dumps([{"name": n, "source": s} for n, s in self.markets.items()])
        if argv[:3] == ["plugin", "marketplace", "add"]:
            catalog = json.loads((Path(argv[3]) / ".github/plugin/marketplace.json").read_text())
            self.markets[catalog["name"]] = argv[3]
            return 0, "added"
        if argv[:3] == ["plugin", "marketplace", "remove"]:
            self.markets.pop(argv[3], None)
            return 0, "removed"
        if argv[:2] == ["plugin", "install"]:
            if argv[2].split("@")[1] not in self.markets:
                return 1, "unknown marketplace"
            self.plugins[argv[2]] = True
            return 0, "installed"
        if argv[:2] == ["plugin", "enable"]:
            self.plugins[argv[2]] = True
            return 0, "enabled"
        if argv[:2] == ["plugin", "uninstall"]:
            self.plugins = {k: v for k, v in self.plugins.items()
                            if k.split("@")[0] != argv[2]}
            return 0, "uninstalled"
        return 2, f"unexpected {command}"


def _executable(tmp_path: Path) -> Path:
    executable = tmp_path / "bin" / "snagentic"
    executable.parent.mkdir(parents=True, exist_ok=True)
    executable.write_text("#!/bin/sh\n")
    return executable


def test_plugin_staged_with_manual_steps_when_copilot_missing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    executable = _executable(tmp_path)
    target = tmp_path / "home" / "extensions"
    result = run(capsys, "copilot", "install", "--target", str(target),
                 "--executable", str(executable))["result"]["plugin"]
    marketplace = tmp_path / "home" / "snagentic-marketplace"
    assert result["marketplace"] == str(marketplace) and result["registered"] is False
    assert result["manual_steps"][0] == f"copilot plugin marketplace add {marketplace}"
    assert result["agents"] == ["servicenow-architect", "servicenow-reviewer"]
    assert "servicenow-reviewer" in result["skills"] and len(result["skills"]) == 6
    root = marketplace / "plugins" / native.PLUGIN_NAME
    hooks = json.loads((root / "hooks.json").read_text())["hooks"]["preToolUse"]
    assert hooks[0]["exec"] == str(executable)
    assert hooks[0]["matcher"] == "snagentic_instance_apply"
    assert json.loads((root / "plugin.json").read_text())["version"] == __version__
    catalog = json.loads((marketplace / ".github/plugin/marketplace.json").read_text())
    assert catalog["plugins"][0]["source"] == f"plugins/{native.PLUGIN_NAME}"

    status = run(capsys, "copilot", "status", "--target", str(target))["result"]["plugin"]
    assert status["staged"] and status["current"] and status["hook_executable_exists"]
    assert status["copilot_available"] is False
    (root / "agents" / "servicenow-reviewer.agent.md").write_text("changed\n")
    assert native.plugin_status(target)["current"] is False

    skipped = run(capsys, "copilot", "install", "--target", str(tmp_path / "other" / "ext"),
                  "--executable", str(executable), "--no-plugin")["result"]
    assert "plugin" not in skipped
    assert not (tmp_path / "other" / "snagentic-marketplace").exists()


def test_plugin_registers_upgrades_and_uninstalls_through_copilot(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = FakeCopilot()
    monkeypatch.setattr(native, "COPILOT_RUNNER", fake)
    executable = _executable(tmp_path)
    target = tmp_path / "home" / "extensions"
    plugin_id = f"{native.PLUGIN_NAME}@{native.MARKETPLACE_NAME}"

    first = run(capsys, "copilot", "install", "--target", str(target),
                "--executable", str(executable))["result"]["plugin"]
    assert first["registered"] is True and fake.plugins == {plugin_id: True}
    assert fake.homes == {str(tmp_path / "home")}
    assert ["plugin", "install", plugin_id] in fake.calls

    fake.calls.clear()
    run(capsys, "copilot", "install", "--target", str(target), "--executable", str(executable))
    assert all(call[1] in {"list"} or call[:3] == ["plugin", "list", "--json"]
               for call in fake.calls), fake.calls

    fake.plugins[plugin_id] = False
    run(capsys, "copilot", "install", "--target", str(target), "--executable", str(executable))
    assert ["plugin", "enable", plugin_id] in fake.calls and fake.plugins[plugin_id]

    status = run(capsys, "copilot", "status", "--target", str(target))["result"]["plugin"]
    assert status["registered"] and status["enabled"] and status["current"]

    removed = run(capsys, "copilot", "uninstall", "--target", str(target))["result"]
    assert removed["plugin"]["removed"] and removed["plugin"]["unregistered"]
    assert fake.plugins == {} and fake.markets == {}
    assert not (tmp_path / "home" / "snagentic-marketplace").exists()


def test_plugin_registration_failures_and_foreign_marketplace(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = FakeCopilot()
    fake.fail = "plugin install"
    monkeypatch.setattr(native, "COPILOT_RUNNER", fake)
    executable = _executable(tmp_path)
    target = tmp_path / "home" / "extensions"
    failed = run(capsys, "copilot", "install", "--target", str(target),
                 "--executable", str(executable))
    assert failed["exit"] == 2 and "copilot plugin install" in failed["error"]
    assert "boom" in failed["error"]

    monkeypatch.setattr(native, "COPILOT_RUNNER", lambda argv, env: (0, "not json"))
    assert native.plugin_status(target)["copilot_available"] is False

    monkeypatch.setattr(native, "COPILOT_RUNNER", None)
    marketplace = tmp_path / "home" / "snagentic-marketplace"
    (marketplace / native.PLUGIN_MARKER).unlink()
    refused = run(capsys, "copilot", "uninstall", "--target", str(target))
    assert refused["exit"] == 2 and "not installed by snagentic" in refused["error"]
    assert marketplace.exists()


def _frontmatter(path: Path) -> dict[str, Any]:
    import yaml

    text = path.read_text(encoding="utf-8")
    assert text.startswith("---\n"), path
    value = yaml.safe_load(text.split("---\n", 2)[1])
    assert isinstance(value, dict)
    return value


def test_plugin_assets_are_consistent() -> None:
    import re

    plugin = REPOSITORY / "copilot-plugin"
    manifest = json.loads((plugin / "plugin.json").read_text())
    assert manifest["name"] == native.PLUGIN_NAME
    assert (plugin / manifest["agents"]).is_dir() and (plugin / manifest["skills"]).is_dir()
    hooks = json.loads((plugin / "hooks.json").read_text())["hooks"]["preToolUse"]
    assert [entry["matcher"] for entry in hooks] == [native.PLUGIN_HOOK_TOOL]
    assert hooks[0]["args"] == ["copilot", "hook", "pre-tool-use"]

    extension = (REPOSITORY / ".github/extensions/snagentic/extension.mjs").read_text()
    tools = set(re.findall(r"""["'](snagentic_[a-z_]+)["']""", extension))
    assert "snagentic_instance_apply" in tools
    agents = sorted((plugin / "agents").glob("*.agent.md"))
    assert len(agents) == 2
    for agent in agents:
        meta = _frontmatter(agent)
        assert meta["name"] == agent.name.removesuffix(".agent.md") and meta["description"]
        custom = [tool for tool in meta["tools"] if tool.startswith("snagentic_")]
        assert custom and set(custom) <= tools, set(custom) - tools
        if meta["name"] == "servicenow-architect":
            assert "snagentic_instance_apply" not in custom
    reviewer = _frontmatter(plugin / "agents/servicenow-reviewer.agent.md")["tools"]
    assert "snagentic_instance_apply" not in reviewer
    assert not {"edit", "create", "write", "shell", "bash"} & set(reviewer)

    skills = sorted((plugin / "skills").glob("*/SKILL.md"))
    assert len(skills) == 6
    for skill in skills:
        meta = _frontmatter(skill)
        assert meta["name"] == skill.parent.name and meta["description"]


def test_frozen_executable_prefers_stable_path_link(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    versioned = tmp_path / "Caskroom" / "snagentic" / "0.1.0" / "snagentic" / "snagentic"
    versioned.parent.mkdir(parents=True)
    versioned.write_text("#!/bin/sh\n")
    versioned.chmod(0o755)
    link_dir = tmp_path / "bin"
    link_dir.mkdir()
    (link_dir / "snagentic").symlink_to(versioned)
    monkeypatch.setattr(native.resources, "is_frozen", lambda: True)
    monkeypatch.setattr(native.sys, "executable", str(versioned))
    monkeypatch.setenv("PATH", str(link_dir))
    assert native._self_executable() == link_dir / "snagentic"
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    assert native._self_executable() == versioned.resolve()
