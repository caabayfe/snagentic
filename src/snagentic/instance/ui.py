"""Playwright UI recipes for the few operations that have no supported API.

Recipes live in ``ui/recipes/<name>/`` (``recipe.json`` + ``steps.mjs``) and run in
the Node runner under ``ui/``. Python validates the request, applies policy, resolves
the UI login from the OS credential store (or environment when allowed), and hands it
to the runner over its private stdin pipe. Credentials never appear in the runner's
command line, environment, or the persisted ``request.json``. Evidence (screenshots,
post-login trace, result) is written to ``.snagentic/<instance>/ui/<run>/``.

The runner uses a bundled or local Node.js by default; ``SNAGENTIC_UI_RUNNER=docker``
keeps the containerised runner for contributors.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import re
import shutil
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any

from snagentic import resources
from snagentic.credentials import missing_message, resolve_secret
from snagentic.errors import ConfigurationError, PolicyDeniedError, ServiceNowError
from snagentic.instance.config import InstanceConfig, InstancePaths
from snagentic.policy import PolicyEnforcer

RECIPE_NAME = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
PARAMETER_NAME = re.compile(r"^[a-z][a-z0-9_]{0,62}$")
SIMPLE_EXECUTABLE = re.compile(r"^[A-Za-z0-9_.-]+$")
PASS_THROUGH_ENVIRONMENT = (
    "PATH", "HOME", "LANG", "LC_ALL", "TMPDIR", "DOCKER_HOST", "DOCKER_CONTEXT",
    "DOCKER_CERT_PATH", "DOCKER_TLS_VERIFY", "HTTPS_PROXY", "HTTP_PROXY", "NO_PROXY",
    "https_proxy", "http_proxy", "no_proxy", "SSL_CERT_FILE", "PLAYWRIGHT_BROWSERS_PATH",
    "SystemRoot", "SYSTEMROOT", "windir", "USERPROFILE", "APPDATA", "LOCALAPPDATA",
    "TEMP", "TMP", "PATHEXT", "ComSpec",
)
CONTAINER_ROOT = "/workspace"
UI_RUNNERS = ("node", "docker")


class UiRunner:
    def __init__(
        self,
        root: Path,
        paths: InstancePaths,
        config: InstanceConfig,
        *,
        policy: PolicyEnforcer | None = None,
        runner: Any = subprocess.run,
        environ: dict[str, str] | None = None,
        clock: Any = None,
        resolver: Callable[[str], str | None] | None = None,
    ) -> None:
        self.root = root.resolve()
        self.paths = paths
        self.config = config
        self.policy = policy or PolicyEnforcer()
        self.runner = runner
        self.environ = dict(os.environ if environ is None else environ)
        self.clock = clock or (lambda: dt.datetime.now(dt.UTC))
        self.resolver = resolver or (
            lambda name: resolve_secret(
                name, url=self.config.url, store=self.config.auth.store, environ=self.environ
            )
        )
        self.ui_root = ui_app_dir(self.root, self.environ)
        self.recipes_root = self.ui_root / "recipes"

    def list_recipes(self) -> list[dict[str, Any]]:
        if not self.recipes_root.is_dir():
            return []
        result = []
        for manifest in sorted(self.recipes_root.glob("*/recipe.json")):
            recipe = self.load(manifest.parent.name)
            result.append({key: recipe.get(key) for key in (
                "name", "description", "mutating", "api_alternative", "parameters",
                "verified_releases")})
        return result

    def load(self, name: str) -> dict[str, Any]:
        if not RECIPE_NAME.fullmatch(name):
            raise ValueError(f"invalid recipe name: {name!r}")
        manifest = self.recipes_root / name / "recipe.json"
        if not manifest.is_file() or not (manifest.parent / "steps.mjs").is_file():
            raise ConfigurationError(f"unknown UI recipe: {name}")
        recipe = json.loads(manifest.read_text(encoding="utf-8"))
        if not isinstance(recipe, dict) or recipe.get("name") != name:
            raise ConfigurationError(f"recipe manifest name mismatch: {manifest}")
        parameters = recipe.get("parameters", {})
        if not isinstance(parameters, dict) or not all(
            PARAMETER_NAME.fullmatch(key) for key in parameters
        ):
            raise ConfigurationError(f"invalid recipe parameters: {manifest}")
        recipe.setdefault("mutating", True)
        return recipe

    def validate(self, recipe: dict[str, Any], params: dict[str, str]) -> dict[str, str]:
        spec: dict[str, dict[str, Any]] = recipe.get("parameters", {})
        unknown = set(params) - set(spec)
        if unknown:
            raise ValueError(f"unsupported recipe parameters: {', '.join(sorted(unknown))}")
        validated: dict[str, str] = {}
        for key, rules in spec.items():
            value = params.get(key)
            if value is None or value == "":
                if rules.get("required"):
                    raise ValueError(f"missing recipe parameter: {key}")
                continue
            if rules.get("type") == "file":
                validated[key] = self._repository_file(value, rules)
                continue
            pattern = rules.get("pattern", r"^[A-Za-z0-9_.:\- ]{1,200}$")
            if not re.fullmatch(pattern, value):
                raise ValueError(f"invalid value for recipe parameter {key}")
            validated[key] = value
        return validated

    def run(
        self,
        name: str,
        params: dict[str, str],
        *,
        dry_run: bool = False,
        confirm: bool = False,
        prepare_only: bool = False,
    ) -> dict[str, Any]:
        plan, recipe = self._plan(name, params)
        if recipe["mutating"]:
            self.policy.require_write_allowed(self.config.kind)
        if dry_run:
            return {"status": "dry_run", **plan}
        if recipe["mutating"] and not confirm:
            raise PolicyDeniedError(f"UI recipe {name} changes the instance; pass confirm")
        username, password = self._credentials()
        request, evidence = self._prepare(name, plan["parameters"])
        evidence_path = evidence.relative_to(self.root).as_posix()
        if prepare_only:
            return {"status": "prepared", **plan, "request": request, "evidence": evidence_path}
        mode = self._mode()
        command = self._command(mode)
        environment = self._runner_environment(mode)
        # The private request adds the login and is only ever written to the pipe.
        private = {**request, "credentials": {"username": username, "password": password}}
        completed = self.runner(
            command, input=json.dumps(private), capture_output=True, text=True,
            cwd=self.root, env=environment, timeout=self.config.ui.timeout_seconds + 120,
            check=False,
        )
        del private
        result = _last_json_line(completed.stdout)
        if completed.returncode != 0 or not result or not result.get("ok"):
            error = (result or {}).get("error") or _tail(completed.stderr)
            raise ServiceNowError(f"UI recipe {name} failed: {error} (evidence: {evidence_path})")
        if name == "export-app-inventory":
            self._save_app_inventory(result.get("result"), evidence_path)
        return {"status": "completed", **plan, "result": result.get("result"),
                "evidence": evidence_path}

    def _save_app_inventory(self, result: Any, evidence: str) -> None:
        if not isinstance(result, dict) or result.get("complete") is not True:
            raise ServiceNowError("Application Manager inventory did not reach its final page")
        records = result.get("records")
        if not isinstance(records, list):
            raise ServiceNowError("Application Manager inventory returned an invalid record list")
        normalized = []
        for record in records:
            if not isinstance(record, dict):
                raise ServiceNowError("Application Manager inventory contains an invalid record")
            sys_id = str(record.get("sys_id") or "")
            name = str(record.get("name") or "")
            scope = str(record.get("scope") or "")
            if not re.fullmatch(r"[A-Za-z0-9]{32}", sys_id) or not name or not scope:
                raise ServiceNowError(
                    "Application Manager inventory contains an invalid application identity"
                )
            normalized.append({
                "sys_id": sys_id,
                "scope": scope,
                "name": name,
                "version": str(record.get("version") or ""),
                "vendor": str(record.get("vendor") or ""),
                "short_description": str(record.get("short_description") or ""),
            })
        destination = self.paths.state / "operational" / "sys_store_app.json"
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_suffix(".tmp")
        temporary.write_text(json.dumps({
            "source": "application_manager",
            "fetched_at": self.clock().isoformat(),
            "evidence": evidence,
            "displayed_count": int(result.get("displayed_count") or len(normalized)),
            "duplicate_count": int(result.get("duplicate_count") or 0),
            "complete": True,
            "records": normalized,
        }, indent=2) + "\n", encoding="utf-8")
        os.replace(temporary, destination)

    def _plan(self, name: str, params: dict[str, str]) -> tuple[dict[str, Any], dict[str, Any]]:
        recipe = self.load(name)
        validated = self.validate(recipe, params)
        plan = {
            "recipe": name,
            "instance": self.config.name,
            "mutating": bool(recipe["mutating"]),
            "api_alternative": recipe.get("api_alternative"),
            "parameters": validated,
            "steps": recipe.get("steps", []),
        }
        return plan, recipe

    def _credentials(self) -> tuple[str, str]:
        username_env, password_env = self.config.ui_credential_names()
        values = {key: self.resolver(key) for key in (username_env, password_env)}
        missing = [key for key, value in values.items() if not value]
        if missing:
            raise ConfigurationError(
                "missing UI credentials: " + missing_message(
                    missing, url=self.config.url, store=self.config.auth.store,
                    environ=self.environ,
                )
            )
        return values[username_env] or "", values[password_env] or ""

    def _mode(self) -> str:
        mode = self.environ.get("SNAGENTIC_UI_RUNNER") or "node"
        if mode not in UI_RUNNERS:
            raise ConfigurationError("SNAGENTIC_UI_RUNNER must be 'node' or 'docker'")
        return mode

    def _runner_environment(self, mode: str) -> dict[str, str]:
        environment = {key: self.environ[key] for key in PASS_THROUGH_ENVIRONMENT
                       if key in self.environ}
        if mode == "node" and "PLAYWRIGHT_BROWSERS_PATH" not in environment:
            managed = resources.playwright_browsers_dir(self.environ)
            if resources.is_frozen() or managed.is_dir():
                environment["PLAYWRIGHT_BROWSERS_PATH"] = str(managed)
        return environment

    def _prepare(self, name: str, parameters: dict[str, str]) -> tuple[dict[str, Any], Path]:
        username_env, password_env = self.config.ui_credential_names()
        run_id = self.clock().strftime("%Y%m%dT%H%M%SZ") + f"-{name}"
        evidence = self.paths.ui_evidence / run_id
        evidence.mkdir(parents=True, exist_ok=True)
        mode = self._mode()
        request = {
            "recipe": name,
            "base_url": str(self.config.url),
            "parameters": parameters,
            "username_env": username_env,
            "password_env": password_env,
            "repository_root": CONTAINER_ROOT if mode == "docker" else str(self.root),
            "evidence_dir": evidence.relative_to(self.root).as_posix(),
            "headless": self.config.ui.headless,
            "timeout_ms": int(self.config.ui.timeout_seconds * 1000),
        }
        (evidence / "request.json").write_text(json.dumps(request, indent=2) + "\n")
        return request, evidence

    def _command(self, mode: str) -> list[str]:
        if mode == "docker":
            docker = shutil.which("docker", path=self.environ.get("PATH")) or "docker"
            return [docker, "compose", "run", "--rm", "-T", "ui"]
        return [node_executable(self.environ), str(self.ui_root / "src" / "cli.mjs")]

    def _repository_file(self, value: str, rules: dict[str, Any]) -> str:
        candidate = (self.root / value).resolve()
        if not candidate.is_relative_to(self.root) or not candidate.is_file():
            raise ValueError("file parameters must name an existing file inside the repository")
        relative = candidate.relative_to(self.root)
        if relative.parts and relative.parts[0] in {".git", ".snagentic", "config"}:
            raise ValueError("file parameters cannot point at git, state or configuration files")
        suffixes = rules.get("extensions")
        if suffixes and candidate.suffix.lower() not in suffixes:
            raise ValueError(f"file parameter must have one of: {', '.join(suffixes)}")
        return relative.as_posix()


def installed_ui_dir(environ: dict[str, str] | None = None) -> Path:
    """Where ``snagentic ui install`` places the runner for wheel installs."""

    return resources.user_data_dir(environ) / "ui"


def ui_app_dir(root: Path, environ: dict[str, str] | None = None) -> Path:
    """The first runner with Playwright installed: the workspace's own ``ui/`` (a source
    checkout), the native bundle, then ``snagentic ui install``'s copy."""

    local = root / "ui"
    has_local = (local / "recipes").is_dir()
    bundled = resources.asset_dir("ui")
    installed = installed_ui_dir(environ)
    for candidate in (local if has_local else None, bundled, installed):
        if candidate is not None and (candidate / "node_modules" / "playwright").is_dir():
            return candidate
    if has_local:
        return local
    return bundled or local


def node_executable(environ: dict[str, str] | None = None) -> str:
    """``SNAGENTIC_NODE``, then the Node.js bundled with a native build, then ``PATH``."""

    source = os.environ if environ is None else environ
    configured = source.get("SNAGENTIC_NODE")
    if configured:
        if not (Path(configured).is_absolute() or SIMPLE_EXECUTABLE.fullmatch(configured)):
            raise ConfigurationError(
                "SNAGENTIC_NODE must be an absolute path or a simple executable name"
            )
        return configured
    bundled = resources.bundled_node()
    if bundled is not None:
        return str(bundled)
    found = shutil.which("node", path=source.get("PATH"))
    if found:
        return found
    raise ConfigurationError(
        "no Node.js runtime for UI recipes: install the native snagentic build, "
        "install Node.js 20+, or set SNAGENTIC_NODE"
    )


def _last_json_line(output: str) -> dict[str, Any] | None:
    for line in reversed(output.strip().splitlines()):
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            return value
    return None


def _tail(text: str, limit: int = 500) -> str:
    return text.strip()[-limit:] or "no output"
