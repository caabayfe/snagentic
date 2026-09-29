"""Native-install commands: ``auth``, ``copilot``, ``ui``, ``profile`` and ``doctor --local``.

None of these commands ever print a credential value.
"""

from __future__ import annotations

import argparse
import getpass
import hashlib
import json
import os
import platform
import shutil
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from snagentic import __version__, credentials, resources
from snagentic.errors import ConfigurationError

EXTENSION_NAME = "snagentic"
RUNTIME_MARKER = "snagentic-runtime.json"
NODE_TIMEOUT_SECONDS = 1_800

# Test hook: ``(prompt, secret) -> value``; defaults to input()/getpass().
READ_VALUE: Callable[[str, bool], str] | None = None


def add_native_parsers(subparsers: Any) -> None:
    auth = subparsers.add_parser(
        "auth", help="store ServiceNow credentials in the OS credential store"
    )
    auth_commands = auth.add_subparsers(dest="auth_command", required=True)
    for name, help_text in (
        ("login", "prompt for credentials and store them in the OS credential store"),
        ("status", "show which credentials are stored (never their values)"),
        ("logout", "delete stored credentials from the OS credential store"),
    ):
        command = auth_commands.add_parser(name, help=help_text)
        command.add_argument("-i", "--instance", dest="auth_instance",
                             help="instance folder name (instances/<name>/)")
        command.add_argument("--name", action="append", default=[], dest="credential_names",
                             help="limit to these credential names (repeatable)")
        if name == "login":
            command.add_argument(
                "--from-env", action="store_true",
                help="migrate values from the current environment instead of prompting",
            )

    copilot = subparsers.add_parser("copilot", help="manage the GitHub Copilot CLI extension")
    copilot_commands = copilot.add_subparsers(dest="copilot_command", required=True)
    install = copilot_commands.add_parser(
        "install", help="install or upgrade the user-scope Copilot CLI extension"
    )
    install.add_argument("--target", type=Path,
                         help="extensions directory (default: $COPILOT_HOME/extensions)")
    install.add_argument("--executable", type=Path,
                         help="snagentic executable the extension should run")
    install.add_argument("--force", action="store_true",
                         help="replace an existing extension not installed by snagentic")
    install.add_argument("--no-plugin", action="store_true",
                         help="install only the extension, not the ServiceNow expert plugin")
    for name in ("status", "uninstall"):
        command = copilot_commands.add_parser(name)
        command.add_argument("--target", type=Path)
    hook = copilot_commands.add_parser(
        "hook", help="Copilot CLI hook entry point (reads the hook payload on stdin)")
    hook.add_argument("event", choices=("pre-tool-use",))

    ui = subparsers.add_parser("ui", help="manage the local Playwright runtime for UI recipes")
    ui_commands = ui.add_subparsers(dest="ui_runtime_command", required=True)
    ui_commands.add_parser("install", help="install the runner dependencies and Chromium")
    ui_commands.add_parser("status", help="report the UI runner, Node.js and browser status")

    subparsers.add_parser(
        "profile", help="non-secret profile summary used by the Copilot extension"
    )


def run_native(args: argparse.Namespace, root: Path) -> Any:
    if args.command == "auth":
        return _auth(args, root)
    if args.command == "copilot":
        if args.copilot_command == "install":
            return copilot_install(args.target, args.executable, force=args.force,
                                   plugin=not args.no_plugin)
        if args.copilot_command == "status":
            return copilot_status(args.target, root)
        return copilot_uninstall(args.target)
    if args.command == "ui":
        return ui_install(root) if args.ui_runtime_command == "install" else ui_status(root)
    if args.command == "profile":
        from snagentic.config import load_config

        environment = load_config(args.config).environment(args.environment)
        return {
            "environment": environment.name,
            "kind": environment.kind,
            "credential_names": environment.auth.credential_names(),
            "store": credentials.effective_store(environment.auth.store),
        }
    raise AssertionError(f"unhandled command: {args.command}")


# --------------------------------------------------------------------------- auth


@dataclass(frozen=True)
class CredentialTarget:
    label: str
    url: str
    store: credentials.CredentialStore
    names: list[str]


def credential_target(args: argparse.Namespace, root: Path) -> CredentialTarget:
    from snagentic.instance.config import InstanceRegistry

    registry = InstanceRegistry(root)
    instance = getattr(args, "auth_instance", None)
    if instance or (not args.environment and registry.names()):
        config = registry.load(instance)
        names = config.auth.credential_names() + [
            name for name in (config.ui.username_env, config.ui.password_env) if name
        ]
        target = CredentialTarget(f"instance {config.name}", str(config.url),
                                  config.auth.store, list(dict.fromkeys(names)))
    else:
        from snagentic.config import load_config

        environment = load_config(args.config).environment(args.environment)
        target = CredentialTarget(f"environment {environment.name}", str(environment.url),
                                  environment.auth.store, environment.auth.credential_names())
    selected: list[str] = getattr(args, "credential_names", []) or []
    unknown = sorted(set(selected) - set(target.names))
    if unknown:
        raise ConfigurationError(
            f"{target.label} does not use credentials: {', '.join(unknown)}"
        )
    if selected:
        target = CredentialTarget(target.label, target.url, target.store,
                                  [name for name in target.names if name in selected])
    return target


def _auth(args: argparse.Namespace, root: Path) -> dict[str, Any]:
    target = credential_target(args, root)
    summary = {"target": target.label, "host": credentials.host_key(target.url)}
    if args.auth_command == "status":
        return {**summary, **auth_status(target)}
    if args.auth_command == "logout":
        removed = [name for name in target.names if credentials.delete_secret(target.url, name)]
        return {**summary, "removed": removed}
    if target.store == "env":
        raise ConfigurationError(
            f"{target.label} sets auth.store: env; change it to keychain or auto "
            "before storing credentials in the OS credential store"
        )
    values: dict[str, str] = {}
    for name in target.names:
        if args.from_env:
            value = os.environ.get(name, "")
            if not value:
                raise ConfigurationError(f"environment variable is unset: {name}")
        else:
            value = _read(f"{name} for {summary['host']}: ", secret=not _is_username(name))
            if not value:
                raise ConfigurationError(f"no value entered for {name}")
        values[name] = value
    for name, value in values.items():
        credentials.store_secret(target.url, name, value)
    result: dict[str, Any] = {**summary, "stored": list(values),
                              "backend": credentials.backend_name()}
    if args.from_env:
        result["next_step"] = (
            "verify with `snagentic auth status`, then remove these variables from shell "
            "profiles and any ~/.config/snagentic/*.env files"
        )
    return result


def auth_status(target: CredentialTarget) -> dict[str, Any]:
    available = True
    rows = []
    for name in target.names:
        stored: bool | None
        try:
            stored = credentials.read_keychain(target.url, name) is not None
        except credentials.CredentialStoreUnavailable:
            stored, available = None, False
        rows.append({"name": name, "keychain": stored, "environment": name in os.environ})
    return {
        "store": credentials.effective_store(target.store),
        "backend": credentials.backend_name() if available else "unavailable",
        "credentials": rows,
    }


def _is_username(name: str) -> bool:
    return name.upper().endswith("USERNAME") or name.upper().endswith("_USER")


def _read(prompt: str, *, secret: bool) -> str:
    if READ_VALUE is not None:
        return READ_VALUE(prompt, secret)
    if secret:
        return getpass.getpass(prompt, stream=sys.stderr)
    print(prompt, end="", file=sys.stderr, flush=True)
    return sys.stdin.readline().strip()


# ------------------------------------------------------------------------ copilot


def extensions_directory(target: Path | None) -> Path:
    return (target or resources.copilot_home() / "extensions").expanduser()


def _self_executable() -> Path:
    found = shutil.which("snagentic")
    if resources.is_frozen():
        # Keep a package manager's stable link (e.g. /opt/homebrew/bin/snagentic): the
        # resolved target lives in a versioned folder that disappears on upgrade.
        frozen = Path(sys.executable).resolve()
        if found and Path(found).resolve() == frozen:
            return Path(found).absolute()
        return frozen
    if not found:
        raise ConfigurationError(
            "cannot locate the snagentic executable; pass --executable /path/to/snagentic"
        )
    return Path(found).resolve()


def _extension_files(source: Path) -> list[Path]:
    return sorted(path for path in source.glob("*.mjs") if path.is_file())


def _digest(files: list[Path]) -> str:
    digest = hashlib.sha256()
    for path in files:
        digest.update(path.name.encode() + b"\0" + path.read_bytes() + b"\0")
    return digest.hexdigest()


def _replace_directory(stage: Path, destination: Path) -> None:
    backup = destination.parent / f".{destination.name}-previous-{os.getpid()}"
    if destination.exists():
        shutil.rmtree(backup, ignore_errors=True)
        destination.rename(backup)
    try:
        stage.rename(destination)
    except OSError:
        if backup.exists():
            backup.rename(destination)
        raise
    shutil.rmtree(backup, ignore_errors=True)


# The ServiceNow expert plugin is published through a snagentic-owned local marketplace
# under $COPILOT_HOME/snagentic-marketplace. Copilot loads local-marketplace plugins live,
# so an upgrade only re-stages the directory.
PLUGIN_NAME = "snagentic-servicenow"
MARKETPLACE_NAME = "snagentic"
PLUGIN_MARKER = "snagentic-plugin.json"
PLUGIN_HOOK_TOOL = "snagentic_instance_apply"
COPILOT_TIMEOUT_SECONDS = 120

# Test hook: ``(argv, environ) -> (returncode, stdout)``; defaults to the copilot binary.
COPILOT_RUNNER: Callable[[list[str], dict[str, str]], tuple[int, str] | None] | None = None


def _copilot_home(target: Path | None) -> Path:
    return extensions_directory(target).parent


def plugin_marketplace(target: Path | None) -> Path:
    return _copilot_home(target) / "snagentic-marketplace"


def _plugin_files(source: Path) -> list[Path]:
    return sorted(
        path for path in source.rglob("*")
        if path.is_file() and path.name != PLUGIN_MARKER and "__pycache__" not in path.parts
    )


def _plugin_digest(root: Path) -> str:
    digest = hashlib.sha256()
    for path in _plugin_files(root):
        if path.name in {"hooks.json", "plugin.json"}:
            continue
        digest.update(path.relative_to(root).as_posix().encode() + b"\0"
                      + path.read_bytes() + b"\0")
    return digest.hexdigest()


def _run_copilot(arguments: list[str], home: Path) -> tuple[int, str] | None:
    environ = {**os.environ, "COPILOT_HOME": str(home)}
    if COPILOT_RUNNER is not None:
        return COPILOT_RUNNER(arguments, environ)
    program = shutil.which("copilot")
    if program is None:
        return None
    completed = subprocess.run(  # noqa: S603 - fixed argv, no shell
        [program, *arguments], env=environ, capture_output=True, text=True,
        timeout=COPILOT_TIMEOUT_SECONDS, check=False,
    )
    return completed.returncode, completed.stdout + completed.stderr


def _copilot_json(arguments: list[str], home: Path) -> list[dict[str, Any]] | None:
    outcome = _run_copilot(arguments, home)
    if outcome is None or outcome[0] != 0:
        return None
    try:
        value = json.loads(outcome[1])
    except ValueError:
        return None
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) \
        else None


def _plugin_registration(home: Path) -> dict[str, Any]:
    plugins = _copilot_json(["plugin", "list", "--json"], home)
    if plugins is None:
        return {"copilot_available": False}
    entry = next((item for item in plugins if item.get("name") == PLUGIN_NAME
                  and item.get("marketplace") == MARKETPLACE_NAME), None)
    return {"copilot_available": True, "registered": entry is not None,
            "enabled": bool(entry and entry.get("enabled"))}


def plugin_install(target: Path | None, program: Path, *, register: bool = True) -> dict[str, Any]:
    source = resources.asset_dir("copilot-plugin")
    if source is None:
        raise ConfigurationError("the ServiceNow plugin assets are missing from this install")
    marketplace = plugin_marketplace(target)
    marketplace.parent.mkdir(parents=True, exist_ok=True)
    stage = marketplace.parent / f".marketplace-install-{os.getpid()}"
    shutil.rmtree(stage, ignore_errors=True)
    plugin_root = stage / "plugins" / PLUGIN_NAME
    for path in _plugin_files(source):
        destination = plugin_root / path.relative_to(source)
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, destination)
    manifest = json.loads((source / "plugin.json").read_text(encoding="utf-8"))
    manifest["version"] = __version__
    (plugin_root / "plugin.json").write_text(json.dumps(manifest, indent=2) + "\n",
                                             encoding="utf-8")
    hooks = json.loads((source / "hooks.json").read_text(encoding="utf-8"))
    for entry in hooks["hooks"]["preToolUse"]:
        entry["exec"] = str(program)
    (plugin_root / "hooks.json").write_text(json.dumps(hooks, indent=2) + "\n",
                                            encoding="utf-8")
    catalog = {
        "name": MARKETPLACE_NAME,
        "owner": {"name": "snagentic"},
        "metadata": {"description": "Plugins installed by the snagentic CLI",
                     "version": __version__},
        "plugins": [{"name": PLUGIN_NAME, "source": f"plugins/{PLUGIN_NAME}",
                     "version": __version__, "description": manifest["description"]}],
    }
    (stage / ".github" / "plugin").mkdir(parents=True)
    (stage / ".github" / "plugin" / "marketplace.json").write_text(
        json.dumps(catalog, indent=2) + "\n", encoding="utf-8")
    marker = {"version": __version__, "executable": str(program),
              "digest": _plugin_digest(plugin_root)}
    (stage / PLUGIN_MARKER).write_text(json.dumps(marker, indent=2) + "\n", encoding="utf-8")
    _replace_directory(stage, marketplace)
    plugin_root = marketplace / "plugins" / PLUGIN_NAME
    result: dict[str, Any] = {
        "marketplace": str(marketplace),
        "plugin": f"{PLUGIN_NAME}@{MARKETPLACE_NAME}",
        "agents": sorted(path.name.removesuffix(".agent.md")
                         for path in (plugin_root / "agents").glob("*.agent.md")),
        "skills": sorted(path.parent.name
                         for path in (plugin_root / "skills").glob("*/SKILL.md")),
        "hooks": {"preToolUse": PLUGIN_HOOK_TOOL},
    }
    if not register:
        return {**result, "registered": False}
    home = _copilot_home(target)
    registration = _plugin_registration(home)
    if not registration["copilot_available"]:
        return {**result, "registered": False, "manual_steps": [
            f"copilot plugin marketplace add {marketplace}",
            f"copilot plugin install {PLUGIN_NAME}@{MARKETPLACE_NAME}",
        ]}
    if not registration["registered"]:
        markets = _copilot_json(["plugin", "marketplace", "list", "--json"], home) or []
        if not any(item.get("name") == MARKETPLACE_NAME for item in markets):
            _require_copilot(["plugin", "marketplace", "add", str(marketplace)], home)
        _require_copilot(["plugin", "install", f"{PLUGIN_NAME}@{MARKETPLACE_NAME}"], home)
    elif not registration["enabled"]:
        _require_copilot(["plugin", "enable", f"{PLUGIN_NAME}@{MARKETPLACE_NAME}"], home)
    return {**result, "registered": True}


def _require_copilot(arguments: list[str], home: Path) -> None:
    outcome = _run_copilot(arguments, home)
    if outcome is None or outcome[0] != 0:
        detail = "" if outcome is None else outcome[1].strip()[-500:]
        raise ConfigurationError(f"copilot {' '.join(arguments)} failed: {detail}")


def plugin_status(target: Path | None) -> dict[str, Any]:
    marketplace = plugin_marketplace(target)
    marker_path = marketplace / PLUGIN_MARKER
    status: dict[str, Any] = {"marketplace": str(marketplace), "staged": marker_path.is_file()}
    if marker_path.is_file():
        marker = json.loads(marker_path.read_text(encoding="utf-8"))
        source = resources.asset_dir("copilot-plugin")
        plugin_root = marketplace / "plugins" / PLUGIN_NAME
        status |= {
            "version": marker.get("version"),
            "hook_executable": marker.get("executable"),
            "hook_executable_exists": Path(str(marker.get("executable"))).is_file(),
            "current": source is not None and marker.get("version") == __version__
            and _plugin_digest(plugin_root) == _plugin_digest(source),
        }
    return status | _plugin_registration(_copilot_home(target))


def plugin_uninstall(target: Path | None) -> dict[str, Any]:
    marketplace = plugin_marketplace(target)
    home = _copilot_home(target)
    registration = _plugin_registration(home)
    if registration.get("registered"):
        _require_copilot(["plugin", "uninstall", PLUGIN_NAME], home)
    markets = _copilot_json(["plugin", "marketplace", "list", "--json"], home) or []
    if any(item.get("name") == MARKETPLACE_NAME for item in markets):
        _require_copilot(["plugin", "marketplace", "remove", MARKETPLACE_NAME], home)
    removed = marketplace.exists()
    if removed:
        if not (marketplace / PLUGIN_MARKER).is_file():
            raise ConfigurationError(f"{marketplace} was not installed by snagentic; not removing")
        shutil.rmtree(marketplace)
    return {"removed": removed, "unregistered": bool(registration.get("registered")),
            "marketplace": str(marketplace)}


def copilot_install(target: Path | None, executable: Path | None, *,
                    force: bool = False, plugin: bool = True) -> dict[str, Any]:
    source = resources.asset_dir("copilot-extension")
    if source is None:
        raise ConfigurationError("the Copilot extension assets are missing from this install")
    program = (executable or _self_executable()).expanduser()
    if not program.is_absolute() or not program.is_file():
        raise ConfigurationError(f"snagentic executable not found: {program}")
    parent = extensions_directory(target)
    destination = parent / EXTENSION_NAME
    if destination.exists() and not (destination / RUNTIME_MARKER).is_file() and not force:
        raise ConfigurationError(
            f"{destination} exists and was not installed by snagentic; pass --force to replace"
        )
    parent.mkdir(parents=True, exist_ok=True)
    stage = parent / f".{EXTENSION_NAME}-install-{os.getpid()}"
    shutil.rmtree(stage, ignore_errors=True)
    stage.mkdir()
    files = _extension_files(source)
    for path in files:
        shutil.copy2(path, stage / path.name)
    marker = {
        "executable": str(program),
        "version": __version__,
        "protocol": resources.PROTOCOL_VERSION,
        "digest": _digest(files),
    }
    (stage / RUNTIME_MARKER).write_text(json.dumps(marker, indent=2) + "\n", encoding="utf-8")
    _replace_directory(stage, destination)
    result: dict[str, Any] = {
        "installed": str(destination),
        "executable": str(program),
        "version": __version__,
        "files": [path.name for path in files],
        "next_step": "restart Copilot CLI or run /clear to load the extension and plugin",
    }
    if plugin:
        result["plugin"] = plugin_install(target, program)
    return result


def copilot_status(target: Path | None, root: Path | None = None) -> dict[str, Any]:
    destination = extensions_directory(target) / EXTENSION_NAME
    marker_path = destination / RUNTIME_MARKER
    status: dict[str, Any] = {"path": str(destination), "installed": marker_path.is_file()}
    if marker_path.is_file():
        marker = json.loads(marker_path.read_text(encoding="utf-8"))
        source = resources.asset_dir("copilot-extension")
        installed_files = _extension_files(destination)
        status |= {
            "version": marker.get("version"),
            "executable": marker.get("executable"),
            "executable_exists": Path(str(marker.get("executable"))).is_file(),
            "protocol_compatible": marker.get("protocol") == resources.PROTOCOL_VERSION,
            "current": source is not None and marker.get("version") == __version__
            and _digest(installed_files) == _digest(_extension_files(source)),
        }
    project = None if root is None else root / ".github/extensions" / EXTENSION_NAME
    if project is not None and (project / "extension.mjs").is_file():
        status["shadowed_by_project_extension"] = True
    status["plugin"] = plugin_status(target)
    return status


def copilot_uninstall(target: Path | None) -> dict[str, Any]:
    destination = extensions_directory(target) / EXTENSION_NAME
    plugin = plugin_uninstall(target)
    if not destination.exists():
        return {"removed": False, "path": str(destination), "plugin": plugin}
    if not (destination / RUNTIME_MARKER).is_file():
        raise ConfigurationError(f"{destination} was not installed by snagentic; not removing")
    shutil.rmtree(destination)
    return {"removed": True, "path": str(destination), "plugin": plugin}


# ----------------------------------------------------------------------------- ui


def _playwright_version(app: Path) -> str | None:
    manifest = app / "node_modules" / "playwright" / "package.json"
    if not manifest.is_file():
        return None
    return str(json.loads(manifest.read_text(encoding="utf-8")).get("version"))


def _browsers_path() -> tuple[Path | None, str]:
    configured = os.environ.get("PLAYWRIGHT_BROWSERS_PATH")
    if configured:
        return Path(configured), "environment"
    managed = resources.playwright_browsers_dir()
    if resources.is_frozen() or managed.is_dir():
        return managed, "managed"
    return None, "playwright-default"


def _node_version(node: str) -> str | None:
    try:
        completed = subprocess.run(  # noqa: S603 - resolved executable, fixed argv
            [node, "--version"], capture_output=True, text=True, timeout=15, check=False
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return completed.stdout.strip() or None if completed.returncode == 0 else None


def ui_status(root: Path) -> dict[str, Any]:
    from snagentic.instance.ui import node_executable, ui_app_dir

    app = ui_app_dir(root)
    try:
        node: str | None = node_executable()
    except ConfigurationError:
        node = None
    node_version = _node_version(node) if node else None
    browsers, source = _browsers_path()
    chromium = bool(browsers and browsers.is_dir() and any(browsers.glob("chromium*")))
    return {
        "runner": os.environ.get("SNAGENTIC_UI_RUNNER") or "node",
        "app": str(app),
        "node": node,
        "node_version": node_version,
        "playwright": _playwright_version(app),
        "browsers_path": str(browsers) if browsers else None,
        "browsers_source": source,
        "chromium_installed": chromium if browsers else None,
        "ready": bool(node_version and _playwright_version(app) and (chromium or not browsers)),
    }


def _npm(node: str) -> list[str]:
    candidates = [Path(node).parent / "npm-cli.js",
                  Path(node).parent.parent / "lib/node_modules/npm/bin/npm-cli.js",
                  Path(node).parent / "node_modules/npm/bin/npm-cli.js"]
    for candidate in candidates:
        if candidate.is_file():
            return [node, str(candidate)]
    found = shutil.which("npm")
    if not found:
        raise ConfigurationError("npm is required to install the UI runner dependencies")
    return [found]


def _run_setup(command: list[str], cwd: Path, env: dict[str, str]) -> None:
    completed = subprocess.run(  # noqa: S603 - fixed argv built from resolved executables
        command, cwd=cwd, env=env, capture_output=True, text=True,
        timeout=NODE_TIMEOUT_SECONDS, check=False,
    )
    if completed.returncode != 0:
        tail = (completed.stderr or completed.stdout).strip()[-800:]
        raise ConfigurationError(
            f"{Path(command[0]).name} {' '.join(Path(part).name for part in command[1:3])} "
            f"failed: {tail}"
        )


def ui_install(root: Path) -> dict[str, Any]:
    from snagentic.instance.ui import installed_ui_dir, node_executable, ui_app_dir

    node = node_executable()
    environment = dict(os.environ)
    app = ui_app_dir(root)
    if _playwright_version(app) is None:
        source = resources.asset_dir("ui")
        writable_checkout = app == root / "ui" or app == resources.CHECKOUT_ROOT / "ui"
        if not writable_checkout:
            if source is None:
                raise ConfigurationError("the UI runner assets are missing from this install")
            app = installed_ui_dir()
            shutil.rmtree(app, ignore_errors=True)
            app.mkdir(parents=True)
            for name in ("src", "recipes"):
                shutil.copytree(source / name, app / name)
            for name in ("package.json", "package-lock.json"):
                shutil.copy2(source / name, app / name)
        _run_setup([*_npm(node), "ci", "--omit=dev", "--no-audit", "--no-fund"], app,
                   environment)
    browsers, _ = _browsers_path()
    if browsers is None:
        browsers = resources.playwright_browsers_dir()
    browsers.mkdir(parents=True, exist_ok=True)
    environment["PLAYWRIGHT_BROWSERS_PATH"] = str(browsers)
    cli = app / "node_modules" / "playwright" / "cli.js"
    _run_setup([node, str(cli), "install", "chromium"], app, environment)
    return {"app": str(app), "node": node, "playwright": _playwright_version(app),
            "browsers_path": str(browsers)}


# ------------------------------------------------------------------------- doctor


def local_report(root: Path) -> dict[str, Any]:
    try:
        credentials.backend()
        store_backend = credentials.backend_name()
    except credentials.CredentialStoreUnavailable as exc:
        store_backend = f"unavailable: {exc}"
    return {
        "version": __version__,
        "protocol": resources.PROTOCOL_VERSION,
        "native": resources.is_frozen(),
        "executable": sys.executable if resources.is_frozen() else shutil.which("snagentic"),
        "platform": f"{platform.system()} {platform.machine()}",
        "python": platform.python_version(),
        "credential_store": {
            "backend": store_backend,
            "default_store": credentials.effective_store("auto"),
        },
        "git": shutil.which("git"),
        "copilot_extension": copilot_status(None, root),
        "ui": ui_status(root),
    }
