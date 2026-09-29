"""snagentic command-line interface."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from snagentic import __version__
from snagentic.cli.instance import add_instance_parser, run_instance
from snagentic.cli.native import add_native_parsers, local_report, run_native
from snagentic.errors import SnagenticError

NATIVE_COMMANDS = {"auth", "copilot", "ui"}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="snagentic")
    parser.add_argument("--version", action="version", version=f"snagentic {__version__}")
    parser.add_argument("--json", action="store_true", dest="json_output")
    subparsers = parser.add_subparsers(dest="command", required=True)
    doctor = subparsers.add_parser("doctor")
    doctor.add_argument(
        "--local", action="store_true",
        help="accepted for compatibility; doctor always checks only the local runtime",
    )
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
    if args.command == "instance":
        return run_instance(args, root)
    if args.command in NATIVE_COMMANDS:
        return run_native(args, root)
    if args.command == "doctor":
        return local_report(root)
    raise AssertionError(f"unhandled command: {args.command}")


def _emit(value: Any, *, json_output: bool, error: bool = False) -> None:
    stream = sys.stderr if error else sys.stdout
    if json_output:
        print(json.dumps(value, indent=2, sort_keys=True, default=str), file=stream)
    elif error:
        print(f"error: {value['error']}", file=stream)
    else:
        print(json.dumps(value["result"], indent=2, sort_keys=True, default=str), file=stream)
