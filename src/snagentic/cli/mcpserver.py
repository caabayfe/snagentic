"""``snagentic mcp ...``: run the Model Context Protocol (MCP) server, or report its
readiness.

The MCP server (``mcp-server.mjs``, bundled as part of the ``copilot-extension``
asset) exposes the same ``snagentic_instance_*`` tool catalogue, JSON Schemas and
safety gates (development-only writes, confirm=true) as the GitHub Copilot CLI
extension, over the standard Model Context Protocol instead of Copilot's
proprietary extension API. Any MCP-compatible client can use it, not only Copilot
CLI, while production-affecting actions remain centrally controlled by the same
Python CLI the extension already shells out to.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

from snagentic import resources
from snagentic.errors import ConfigurationError

MCP_SDK_PACKAGE = Path("node_modules") / "@modelcontextprotocol" / "sdk"


def add_mcp_parser(subparsers: Any) -> None:
    mcp = subparsers.add_parser(
        "mcp", help="run or inspect the Model Context Protocol (MCP) server"
    )
    mcp_commands = mcp.add_subparsers(dest="mcp_command", required=True)
    mcp_commands.add_parser(
        "serve", help="run the MCP server over stdio for any MCP-compatible client"
    )
    mcp_commands.add_parser("status", help="report the MCP server runtime readiness")


def _node_version(node: str) -> str | None:
    try:
        completed = subprocess.run(  # noqa: S603 - resolved executable, fixed argv
            [node, "--version"], capture_output=True, text=True, timeout=15, check=False
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return completed.stdout.strip() or None if completed.returncode == 0 else None


def _server_script() -> tuple[Path | None, Path | None]:
    app = resources.asset_dir("copilot-extension")
    if app is None:
        return None, None
    script = app / "mcp-server.mjs"
    return app, (script if script.is_file() else None)


def mcp_status(root: Path) -> dict[str, Any]:  # noqa: ARG001 - kept for parity with ui_status(root)
    from snagentic.instance.ui import node_executable

    app, script = _server_script()
    try:
        node: str | None = node_executable()
    except ConfigurationError:
        node = None
    node_version = _node_version(node) if node else None
    sdk_installed = bool(app and (app / MCP_SDK_PACKAGE).is_dir())
    return {
        "app": str(app) if app else None,
        "script": str(script) if script else None,
        "node": node,
        "node_version": node_version,
        "sdk_installed": sdk_installed,
        "ready": bool(node_version and script and sdk_installed),
    }


def _runtime_environment() -> dict[str, str]:
    """Builds the child Node environment so it resolves this same running CLI as its
    runtime, without depending on `snagentic` being on PATH.

    The Node MCP server (mcp-server.mjs) decides native/python/docker purely from its
    own environment (SNAGENTIC_EXECUTABLE / SNAGENTIC_PYTHON / PATH lookup / docker
    fallback). Since we already know exactly which interpreter or native executable is
    running *this* `snagentic mcp serve` invocation, we pass it through explicitly so
    every tool call it serves targets this same install, not whatever (if anything)
    happens to be on PATH. An existing user override in the environment always wins.
    """
    environment = dict(os.environ)
    if any(key in environment and environment[key] for key in
           ("SNAGENTIC_RUNTIME", "SNAGENTIC_PYTHON", "SNAGENTIC_EXECUTABLE")):
        return environment
    if resources.is_frozen():
        environment["SNAGENTIC_EXECUTABLE"] = sys.executable
    else:
        environment["SNAGENTIC_PYTHON"] = sys.executable
    return environment


def mcp_serve(root: Path) -> int:
    """Run the MCP server over stdio, inheriting this process's stdio file descriptors.

    Returns the child's exit code; callers must not print anything else to stdout,
    which is reserved for the MCP JSON-RPC protocol.
    """
    from snagentic.instance.ui import node_executable

    app, script = _server_script()
    if app is None or script is None:
        raise ConfigurationError("the MCP server assets are missing from this install")
    if not (app / MCP_SDK_PACKAGE).is_dir():
        raise ConfigurationError(
            "the MCP server dependencies are not installed; run `npm install` in "
            f"{app} (native and release installs bundle them already)"
        )
    node = node_executable()
    completed = subprocess.run(  # noqa: S603 - resolved executable, fixed argv
        [node, str(script)], cwd=root, env=_runtime_environment(), check=False,
    )
    return completed.returncode


def run_mcp(args: argparse.Namespace, root: Path) -> Any:
    if args.mcp_command == "status":
        return mcp_status(root)
    raise AssertionError(f"unhandled mcp command: {args.mcp_command}")
