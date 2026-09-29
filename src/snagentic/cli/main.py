"""snagentic command-line interface."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from snagentic import __version__
from snagentic.changes import (
    build_change_report,
    build_promotion_manifest,
    load_change_plan,
)
from snagentic.cli.instance import add_instance_parser, run_instance
from snagentic.cli.native import add_native_parsers, local_report, run_native
from snagentic.errors import SnagenticError

ServiceNowClient: Any = None
NATIVE_COMMANDS = {"auth", "copilot", "ui", "profile"}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="snagentic")
    parser.add_argument("--version", action="version", version=f"snagentic {__version__}")
    parser.add_argument("--config", type=Path, default=Path("config/snagentic.yaml"))
    parser.add_argument("--environment")
    parser.add_argument("--json", action="store_true", dest="json_output")
    subparsers = parser.add_subparsers(dest="command", required=True)
    init = subparsers.add_parser("init")
    init.add_argument("--auth", choices=("bearer", "basic"), default="bearer")
    doctor = subparsers.add_parser("doctor")
    doctor.add_argument(
        "--local", action="store_true",
        help="check the local runtime (credential store, UI runner, Copilot extension) "
             "without contacting ServiceNow",
    )
    subparsers.add_parser("inventory")
    subparsers.add_parser("pull")
    subparsers.add_parser("status")
    subparsers.add_parser("diff")
    subparsers.add_parser("validate")
    subparsers.add_parser("push-plan")
    push = subparsers.add_parser("push")
    push.add_argument("--approve", action="store_true")
    diagnostics = subparsers.add_parser("diagnostics")
    diagnostics.add_argument("--minutes", type=int, default=60)
    diagnostics.add_argument("--limit", type=int, default=500)
    diagnostics.add_argument("--domain")
    query = subparsers.add_parser("query")
    query.add_argument("text")
    query.add_argument("--domain")
    query.add_argument("--artifact-type")
    for command, help_text in (
        ("promotion-manifest", "build an offline promotion manifest"),
        ("change-report", "build an offline PR change report"),
    ):
        promotion = subparsers.add_parser(command, help=help_text)
        promotion.add_argument("plan", type=Path)
        promotion.add_argument("--source", required=True, dest="source_kind")
        promotion.add_argument("--target", required=True, dest="target_kind")
        promotion.add_argument("--mechanism", required=True)
        promotion.add_argument("--created-at", required=True)
        promotion.add_argument("--git-commit")
    add_instance_parser(subparsers)
    add_native_parsers(subparsers)
    return parser


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    if args.command == "copilot" and args.copilot_command == "hook":
        from snagentic.cli.hook import pre_tool_use

        decision = pre_tool_use(sys.stdin.read())
        if decision is not None:
            print(json.dumps(decision))
        return
    try:
        result = run(args)
    except (SnagenticError, ValueError) as exc:
        _emit({"ok": False, "error": str(exc)}, json_output=args.json_output, error=True)
        raise SystemExit(2) from exc
    _emit({"ok": True, "result": result}, json_output=args.json_output)


def run(args: argparse.Namespace) -> Any:
    root = Path.cwd()
    if args.command == "init":
        return _initialize(root, args.config, auth_mode=args.auth)
    if args.command == "instance":
        return run_instance(args, root)
    if args.command in NATIVE_COMMANDS:
        return run_native(args, root)
    if args.command == "doctor" and args.local:
        return local_report(root)
    if args.command in {"promotion-manifest", "change-report"}:
        plan = load_change_plan(args.plan)
        builder = (
            build_promotion_manifest
            if args.command == "promotion-manifest"
            else build_change_report
        )
        return builder(
            plan,
            source_kind=args.source_kind,
            target_kind=args.target_kind,
            mechanism=args.mechanism,
            created_at=args.created_at,
            git_commit=args.git_commit,
        ).as_dict()
    load_config, service_now_client, diagnostic_service, sync_engine = (
        _load_online_runtime()
    )
    config = load_config(args.config)
    environment = config.environment(args.environment)
    if args.command == "validate":
        return {
            "configuration": "valid",
            "environment": environment.name,
            "write_allowed": environment.writable,
        }
    with service_now_client(environment) as client:
        engine = sync_engine(root, config, client)
        if args.command == "doctor":
            return {
                "environment": environment.name,
                "kind": environment.kind,
                "capabilities": client.capabilities(),
            }
        if args.command == "inventory":
            return {"artifacts": engine.inventory()}
        if args.command == "pull":
            return engine.pull().as_dict()
        if args.command in {"status", "diff"}:
            return engine.status()
        if args.command == "push-plan":
            return engine.create_change_plan()
        if args.command == "push":
            return engine.push(approved=args.approve)
        if args.command == "diagnostics":
            service = diagnostic_service(config.state_directory / "diagnostics", client)
            path = service.collect(minutes=args.minutes, limit=args.limit, domain=args.domain)
            service.expire()
            return {"path": str(path)}
        if args.command == "query":
            return _query_local(config.state_directory / "state.sqlite", args)
    raise AssertionError(f"unhandled command: {args.command}")


def _load_online_runtime() -> tuple[Any, Any, Any, Any]:
    from snagentic.client import ServiceNowClient as DefaultServiceNowClient
    from snagentic.config import load_config
    from snagentic.diagnostics import DiagnosticService
    from snagentic.sync import SyncEngine

    service_now_client = ServiceNowClient or DefaultServiceNowClient
    return load_config, service_now_client, DiagnosticService, SyncEngine


def _initialize(
    root: Path,
    config_path: Path,
    *,
    auth_mode: str = "bearer",
) -> dict[str, Any]:
    target = root / config_path
    if target.exists():
        return {"created": False, "path": str(target)}
    target.parent.mkdir(parents=True, exist_ok=True)
    if auth_mode == "basic":
        auth_blocks = {
            "dev": """mode: basic
      username_env: SNAGENTIC_DEV_USERNAME
      password_env: SNAGENTIC_DEV_PASSWORD""",
            "test": """mode: basic
      username_env: SNAGENTIC_TEST_USERNAME
      password_env: SNAGENTIC_TEST_PASSWORD""",
            "prod": """mode: basic
      username_env: SNAGENTIC_PROD_USERNAME
      password_env: SNAGENTIC_PROD_PASSWORD""",
        }
    else:
        auth_blocks = {
            "dev": """mode: bearer
      token_env: SNAGENTIC_DEV_TOKEN""",
            "test": """mode: bearer
      token_env: SNAGENTIC_TEST_TOKEN""",
            "prod": """mode: bearer
      token_env: SNAGENTIC_PROD_TOKEN""",
        }
    template = f"""default_environment: dev
environments:
  dev:
    name: dev
    url: https://your-development-instance.service-now.com/
    kind: development
    auth:
      {auth_blocks["dev"]}
  test:
    name: test
    url: https://your-test-instance.service-now.com/
    kind: test
    auth:
      {auth_blocks["test"]}
  prod:
    name: prod
    url: https://your-production-instance.service-now.com/
    kind: production
    auth:
      {auth_blocks["prod"]}
"""
    target.write_text(template, encoding="utf-8")
    return {"created": True, "path": str(target)}


def _query_local(database: Path, args: argparse.Namespace) -> dict[str, Any]:
    try:
        from snagentic.index import ArtifactIndex
    except ImportError as exc:
        raise SnagenticError("local index support is unavailable") from exc
    index = ArtifactIndex(database)
    matches = index.query(
        text=args.text,
        domain=args.domain,
        artifact_type=args.artifact_type,
    )
    return {
        "matches": [match.model_dump(mode="json") for match in matches]
    }


def _emit(value: Any, *, json_output: bool, error: bool = False) -> None:
    stream = sys.stderr if error else sys.stdout
    if json_output:
        print(json.dumps(value, indent=2, sort_keys=True, default=str), file=stream)
    elif error:
        print(f"error: {value['error']}", file=stream)
    else:
        print(json.dumps(value["result"], indent=2, sort_keys=True, default=str), file=stream)
