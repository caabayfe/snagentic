"""``snagentic instance ...``: multi-instance mirror, update sets, docs and operations."""

from __future__ import annotations

import argparse
import datetime as dt
import json
from pathlib import Path
from typing import Any

from snagentic.errors import ConfigurationError, ConflictError
from snagentic.instance.config import InstanceConfig, InstancePaths, InstanceRegistry
from snagentic.instance.mirror import git, tune_large_repository
from snagentic.instance.records import atomic_write

# Test hook: an ``httpx`` transport used instead of the network.
TRANSPORT: Any = None

WRITE_COMMANDS = {"apply", "ops-run", "complete", "promote", "scan"}
GIT_BACKED_COMMANDS = {"fetch", "integrate", "status", "plan", "apply"}


def add_instance_parser(subparsers: Any) -> None:
    parser = subparsers.add_parser(
        "instance", help="multi-instance mirror, update sets, documentation and operations"
    )
    parser.add_argument("-i", "--instance", dest="instance_name",
                        help="instance folder name (instances/<name>/)")
    commands = parser.add_subparsers(dest="instance_command", required=True)

    add = commands.add_parser("add", help="create instances/<name>/instance.yaml")
    add.add_argument("name")
    add.add_argument("--url", required=True)
    add.add_argument("--kind", required=True, choices=("development", "test", "production"))
    add.add_argument("--auth", choices=("basic", "bearer"), default="basic")
    add.add_argument("--credential-store", choices=("auto", "keychain", "env"),
                     default="auto", help="where credential values are read from")
    commands.add_parser("list", help="list configured instances")
    commands.add_parser(
        "profile", help="non-secret profile summary used by the Copilot extension"
    )
    fetch = commands.add_parser("fetch", help="refresh the servicenow-remote/<name> mirror")
    fetch.add_argument("--full", action="store_true", help="full reconcile instead of incremental")
    commands.add_parser("integrate", help="merge the mirror branch into the current branch")
    commands.add_parser("status", help="mirror, integration and local change status")
    commands.add_parser("plan", help="plan local metadata changes as update-set writes")
    review = commands.add_parser(
        "review", help="ServiceNow best-practice review and apply gate; defaults to the "
                       "local changes that plan would write")
    review.add_argument("--all", action="store_true", dest="review_all",
                        help="review mirrored records instead of local changes")
    review.add_argument("--table", action="append", default=[], dest="review_tables")
    review.add_argument("--scope", action="append", default=[], dest="review_scopes")
    review.add_argument("--path", action="append", default=[], dest="review_paths",
                        help="record folder or subtree (repository or instance relative)")
    review.add_argument("--customized", action="store_true",
                        help="only customer-updated records (model/customer-updates.yaml)")
    review.add_argument("--rule", action="append", default=[], dest="review_rules")
    review.add_argument("--min-severity", choices=("block", "warn", "info"), default="info")
    review.add_argument("--limit", type=int, default=200)
    review.add_argument("--rules", action="store_true", dest="list_rules",
                        help="list the rule catalogue and exit")
    record = commands.add_parser(
        "review-record", help="record a review verdict bound to the current plan_id")
    record.add_argument("--plan-id", required=True)
    record.add_argument("--verdict", choices=("approve", "reject"), required=True)
    record.add_argument("--reviewer", required=True)
    record.add_argument("--notes", required=True)
    apply = commands.add_parser("apply", help="write a reviewed plan into agent update sets")
    apply.add_argument("--plan-id", required=True)
    apply.add_argument("--confirm", action="store_true")
    apply.add_argument("--label", help="update set label (default: current git branch)")
    apply.add_argument("--allow-collisions", action="store_true")

    commands.add_parser("update-sets", help="list mirrored update sets")
    commands.add_parser("collisions", help="records held in more than one open update set")
    commands.add_parser("activity", help="who is working on what")

    commands.add_parser("index", help="rebuild the local search and dependency index")
    search = commands.add_parser("search", help="search indexed metadata")
    search.add_argument("text")
    search.add_argument("--table")
    search.add_argument("--limit", type=int, default=50)
    refs = commands.add_parser("refs", help="records referencing a table, script include, "
                                             "event or property")
    refs.add_argument("target")
    docs = commands.add_parser("docs", help="build, validate or scaffold instance documentation")
    docs.add_argument(
        "docs_action",
        nargs="?",
        choices=("build", "check", "scaffold", "migrate"),
        default="build",
    )
    docs.add_argument("--type", choices=("capability", "process", "guide"), dest="doc_type")
    docs.add_argument("--id", dest="doc_id")
    docs.add_argument(
        "--strict",
        action="store_true",
        help="also run a strict MkDocs build (requires the docs dependency)",
    )
    table = commands.add_parser(
        "table", help="a table's fields, inheritance and behaviour (own and inherited)"
    )
    table.add_argument("table")
    table.add_argument("--no-inherited", action="store_true",
                       help="omit behaviour inherited from parent tables")

    commands.add_parser("ops-list", help="list supported CI/CD operations")
    ops_run = commands.add_parser("ops-run", help="run a CI/CD operation (development only)")
    ops_run.add_argument("operation")
    ops_run.add_argument("--param", action="append", default=[], metavar="KEY=VALUE")
    ops_run.add_argument("--confirm", action="store_true")
    ops_run.add_argument("--no-wait", action="store_true")
    ops_run.add_argument("--timeout", type=float, default=900)

    scan = commands.add_parser(
        "scan", help="ServiceNow Instance Scan of the agent update sets (development only)")
    scan.add_argument("--label", help="agent update set label (default: current git branch)")
    scan.add_argument("--update-set", action="append", default=[], dest="scan_update_sets",
                      help="update set sys_id to scan (repeatable)")
    scan.add_argument("--target", action="append", default=[], dest="scan_targets",
                      metavar="TABLE:SYS_ID", help="record to point scan (repeatable)")
    scan.add_argument("--suite", dest="scan_suite",
                      help="scan check suite sys_id: one suite scan of the update sets "
                           "instead of point scans of their records")
    scan.add_argument("--confirm", action="store_true")
    scan.add_argument("--timeout", type=float, default=900)
    scan_results = commands.add_parser(
        "scan-results", help="findings of Instance Scan results (read-only)")
    scan_results.add_argument("--result", action="append", default=[], dest="scan_results")
    scan_results.add_argument("--progress-id")
    scan_results.add_argument("--label", help="results of the last `scan` for this label")
    complete = commands.add_parser("complete", help="mark an agent update set complete")
    complete.add_argument("update_set_id")
    complete.add_argument("--confirm", action="store_true")
    promote = commands.add_parser(
        "promote", help="complete agent update sets and emit a content-free manifest"
    )
    promote.add_argument("--label", help="update set label (default: current git branch)")
    promote.add_argument("--confirm", action="store_true")

    ui = commands.add_parser("ui", help="Playwright recipes for UI-only operations")
    ui_commands = ui.add_subparsers(dest="ui_command", required=True)
    ui_commands.add_parser("list", help="list available UI recipes")
    ui_run = ui_commands.add_parser("run", help="run a UI recipe (development only)")
    ui_run.add_argument("recipe")
    ui_run.add_argument("--param", action="append", default=[], metavar="KEY=VALUE")
    ui_run.add_argument("--dry-run", action="store_true")
    ui_run.add_argument("--confirm", action="store_true")
    ui_run.add_argument("--prepare-only", action="store_true",
                        help="validate and write the runner request without launching a browser")


def _review(args: argparse.Namespace, paths: InstancePaths, config: InstanceConfig,
            root: Path) -> dict[str, Any]:
    from snagentic.review.rules import rule_catalog
    from snagentic.review.scan import report, review_mirror

    if args.list_rules:
        return {"rules": rule_catalog()}
    if args.limit < 1:
        raise ConfigurationError("--limit must be at least 1")
    selectors = (args.review_tables or args.review_scopes or args.review_paths
                 or args.customized)
    if args.review_all or selectors:
        result = review_mirror(
            root, paths.workspace, tables=args.review_tables, scopes=args.review_scopes,
            paths=args.review_paths, customized_only=args.customized,
            rules=args.review_rules, min_severity=args.min_severity, limit=args.limit,
        )
        return {"instance": config.name, "target": "mirror", **result}
    from snagentic.instance.changes import ChangePlanner

    tune_large_repository(root)
    plan = ChangePlanner(paths, config).plan()
    objects = plan["_review_findings"]
    result = report(objects, reviewed=plan["review"]["summary"]["records_reviewed"],
                    rules=args.review_rules, min_severity=args.min_severity,
                    limit=args.limit, mode=plan["gate"]["mode"])
    return {"instance": config.name, "target": "changes", "plan_id": plan["plan_id"],
            **result, "gate": plan["gate"]}


def _review_record(args: argparse.Namespace, paths: InstancePaths, config: InstanceConfig,
                   root: Path) -> dict[str, Any]:
    from snagentic.instance.changes import ChangePlanner
    from snagentic.review.gate import record_review

    tune_large_repository(root)
    plan = ChangePlanner(paths, config).plan()
    if plan["plan_id"] != args.plan_id:
        raise ConflictError("the plan changed since it was reviewed; review the new plan")
    if not plan["changes"]:
        raise ConfigurationError("there are no local changes to review")
    destination, record = record_review(
        paths.workspace, plan_id=args.plan_id, verdict=args.verdict, reviewer=args.reviewer,
        notes=args.notes, gate=plan["gate"],
        recorded_at=dt.datetime.now(dt.UTC).isoformat(timespec="seconds"),
    )
    return {"instance": config.name, "record": _relative(destination, root), **record}


def run_instance(args: argparse.Namespace, root: Path) -> Any:
    registry = InstanceRegistry(root)
    command = args.instance_command
    if command == "add":
        path = registry.add(args.name, url=args.url, kind=args.kind, auth_mode=args.auth,
                            store=args.credential_store)
        return {"instance": args.name, "config": _relative(path, root),
                "next_step": f"snagentic auth login -i {args.name}"}
    if command == "list":
        instances = []
        for name in registry.names():
            config = registry.load(name)
            instances.append({"name": name, "url": str(config.url), "kind": config.kind,
                              "writable": config.writable, "mirror_branch": config.mirror_branch})
        return {"instances": instances}
    config = registry.load(args.instance_name)
    paths = registry.paths(config.name)
    if command == "profile":
        from snagentic.credentials import effective_store

        names = config.auth.credential_names() + [
            name for name in (config.ui.username_env, config.ui.password_env) if name
        ]
        return {"instance": config.name, "kind": config.kind,
                "credential_names": sorted(set(names)),
                "store": effective_store(config.auth.store)}
    if command in GIT_BACKED_COMMANDS:
        tune_large_repository(root)
    if command == "integrate":
        from snagentic.instance.mirror import MirrorRepository

        return MirrorRepository(paths, config.mirror_branch).integrate(
            message=f"Integrate {config.mirror_branch}"
        )
    if command == "status":
        return _status(paths, config)
    if command == "plan":
        from snagentic.instance.changes import ChangePlanner

        plan = ChangePlanner(paths, config).plan()
        return {key: value for key, value in plan.items() if not key.startswith("_")}
    if command == "review":
        return _review(args, paths, config, root)
    if command == "review-record":
        return _review_record(args, paths, config, root)
    if command in {"update-sets", "collisions", "activity"}:
        return _update_set_views(command, paths)
    if command == "index":
        from snagentic.instance.index import InstanceIndex

        return InstanceIndex(paths.search_index).rebuild(paths.metadata, root)
    if command in {"search", "refs"}:
        from snagentic.instance.index import InstanceIndex

        index = InstanceIndex(paths.search_index)
        if not paths.search_index.is_file():
            index.rebuild(paths.metadata, root)
        if command == "search":
            return {"results": index.search(args.text, table=args.table, limit=args.limit)}
        return {"target": args.target, "referenced_by": index.references(args.target)}
    if command == "table":
        return _table_model(paths, args.table, inherited=not args.no_inherited)
    if command == "docs":
        from snagentic.instance.docs import DocumentationGenerator

        generator = DocumentationGenerator(paths, config)
        if args.docs_action == "scaffold":
            if not args.doc_type or not args.doc_id:
                raise ConfigurationError("docs scaffold requires --type and --id")
            return generator.scaffold(args.doc_type, args.doc_id)
        if args.docs_action == "migrate":
            return generator.migrate()
        return generator.generate(check=args.docs_action == "check", strict=args.strict)
    if command == "ops-list":
        from snagentic.instance.ops import PlatformOperations

        return {"operations": PlatformOperations.catalog()}
    if command == "ui":
        return _ui(args, paths, config, root)

    if command in WRITE_COMMANDS and not (
        command == "ops-run" and not _operation_mutates(args.operation)
    ):
        from snagentic.policy import PolicyEnforcer

        PolicyEnforcer().require_write_allowed(config.kind)
    with _client(config) as client:
        if command == "fetch":
            from snagentic.instance.sync import InstanceSync

            return InstanceSync(paths, config, client, progress=_progress).fetch(full=args.full)
        if command == "apply":
            from snagentic.instance.changes import UpdateSetWriter

            return UpdateSetWriter(paths, config, client).apply(
                plan_id=args.plan_id, confirm=args.confirm, label=args.label,
                allow_collisions=args.allow_collisions,
            )
        if command == "ops-run":
            from snagentic.instance.ops import PlatformOperations

            return PlatformOperations(config, client).run(
                args.operation, _params(args.param), confirm=args.confirm,
                wait=not args.no_wait, timeout_seconds=args.timeout,
            )
        if command in {"scan", "scan-results"}:
            return _instance_scan(args, paths, config, client, root)
        if command == "complete":
            from snagentic.instance.ops import complete_update_set

            return complete_update_set(config, client, args.update_set_id, confirm=args.confirm)
        if command == "promote":
            from snagentic.instance.changes import _branch_label
            from snagentic.instance.ops import promote

            manifest = promote(
                config, client, label=args.label or _branch_label(root), confirm=args.confirm,
                created_at=dt.datetime.now(dt.UTC).isoformat(timespec="seconds"),
                git_commit=_head(root),
            )
            manifest["instance_scan"] = _scan_summary(paths, manifest["label"])
            if manifest["instance_scan"] is None:
                manifest["next_steps"].insert(0, "Run `snagentic instance scan` (Instance Scan) "
                                                 "on the update sets before promoting.")
            destination = _promotion_destination(
                paths, manifest["label"], manifest["created_at"]
            )
            atomic_write(destination, json.dumps(manifest, indent=2, sort_keys=True) + "\n")
            return {**manifest, "manifest_path": _relative(destination, root)}
    raise AssertionError(f"unhandled instance command: {command}")


def _scan_report_path(paths: InstancePaths, label: str) -> Path:
    from snagentic.instance.changes import validate_label

    return paths.state / "scans" / f"{validate_label(label)}.json"


def _scan_summary(paths: InstancePaths, label: str) -> dict[str, Any] | None:
    source = _scan_report_path(paths, label)
    if not source.is_file():
        return None
    report = json.loads(source.read_text(encoding="utf-8"))
    return {"scanned_at": report.get("scanned_at"), "mode": report.get("mode"),
            "summary": report.get("summary"),
            "results": [run["result"]["sys_id"] for run in report.get("runs", [])
                        if run.get("result")]}


def _instance_scan(args: argparse.Namespace, paths: InstancePaths, config: InstanceConfig,
                   client: Any, root: Path) -> dict[str, Any]:
    from snagentic.instance.changes import _branch_label
    from snagentic.instance.instancescan import InstanceScanner, parse_target

    scanner = InstanceScanner(config, client)
    if args.instance_command == "scan-results":
        results = list(args.scan_results)
        if args.label:
            stored = _scan_summary(paths, args.label)
            if stored is None:
                raise ConfigurationError(f"no stored scan for label {args.label!r}")
            results += stored["results"]
        return scanner.results(result_sys_ids=results, progress_id=args.progress_id)
    targets = [parse_target(value) for value in args.scan_targets]
    label = args.label
    if label is None and not args.scan_update_sets and not targets:
        label = _branch_label(root)
    report = scanner.scan(
        confirm=args.confirm, label=label, update_set_ids=args.scan_update_sets,
        targets=targets, suite_sys_id=args.scan_suite, timeout_seconds=args.timeout,
    )
    report["scanned_at"] = dt.datetime.now(dt.UTC).isoformat(timespec="seconds")
    if label:
        destination = _scan_report_path(paths, label)
        atomic_write(destination, json.dumps(report, indent=2, sort_keys=True) + "\n")
        report["report_path"] = _relative(destination, root)
    return report


def _operation_mutates(name: str) -> bool:
    from snagentic.instance.ops import OPERATIONS

    operation = OPERATIONS.get(name)
    return operation is None or operation.mutating


def _promotion_destination(paths: InstancePaths, label: str, created_at: str) -> Path:
    return paths.state / "promotions" / (
        f"{created_at.replace(':', '')}-{label}.json"
    )


def _table_model(paths: Any, table: str, *, inherited: bool) -> dict[str, Any]:
    import re

    import yaml

    from snagentic.errors import ConfigurationError

    directory = paths.workspace / "model" / "tables"

    def load(name: str) -> dict[str, Any] | None:
        path = directory / f"{name}.yaml"
        if not re.fullmatch(r"[a-z0-9_]{1,80}", name) or not path.is_file():
            return None
        loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
        return loaded if isinstance(loaded, dict) else None

    model = load(table)
    if model is None:
        raise ConfigurationError(
            f"no table model for {table!r}; fetch and integrate the instance first"
        )
    if inherited:
        model["inherited_behaviour"] = {
            ancestor: (load(ancestor) or {}).get("behaviour", {})
            for ancestor in model.get("extends", [])
        }
    return model


def _client(config: InstanceConfig) -> Any:
    from snagentic.instance.tableapi import TableApiClient

    return TableApiClient(config.environment(), transport=TRANSPORT)


def _status(paths: InstancePaths, config: InstanceConfig) -> dict[str, Any]:
    from snagentic.instance.mirror import MirrorRepository

    mirror = MirrorRepository(paths, config.mirror_branch)
    state: dict[str, Any] = {}
    if paths.sync_state.is_file():
        raw = json.loads(paths.sync_state.read_text(encoding="utf-8"))
        state = raw if isinstance(raw, dict) else {}
    tip = mirror.tip()
    return {
        "instance": config.name,
        "kind": config.kind,
        "writable": config.writable,
        "mirror_branch": config.mirror_branch,
        "mirror_commit": tip,
        "integrated": mirror.is_integrated() if tip else False,
        "local_changes": mirror.workspace_dirty(),
        "sync_state": {key: state[key] for key in ("last_fetch", "last_full", "watermark")
                       if key in state},
        "pending_apply": (paths.state / "pending-apply.json").is_file(),
    }


def _update_set_views(command: str, paths: InstancePaths) -> dict[str, Any]:
    from snagentic.instance.updatesets import activity_view, collision_report, load_update_sets

    update_sets = load_update_sets(paths.mirror_workspace / "update-sets")
    if not update_sets:
        update_sets = load_update_sets(paths.update_sets)
    if command == "update-sets":
        return {"update_sets": [
            {key: value for key, value in item.items() if key != "changes"}
            | {"changes": len(item.get("changes", []))}
            for item in update_sets
        ]}
    if command == "collisions":
        return collision_report(update_sets)
    return activity_view(update_sets)


def _ui(args: argparse.Namespace, paths: InstancePaths, config: InstanceConfig,
        root: Path) -> Any:
    from snagentic.instance.ui import UiRunner

    runner = UiRunner(root, paths, config)
    if args.ui_command == "list":
        return {"recipes": runner.list_recipes()}
    return runner.run(args.recipe, _params(args.param), dry_run=args.dry_run,
                      confirm=args.confirm, prepare_only=args.prepare_only)


def _params(values: list[str]) -> dict[str, str]:
    result: dict[str, str] = {}
    for value in values:
        key, separator, item = value.partition("=")
        if not separator or not key:
            raise ConfigurationError(f"parameters must be KEY=VALUE: {value!r}")
        result[key] = item
    return result


def _head(root: Path) -> str | None:
    result = git(root, "rev-parse", "HEAD", check=False)
    return result.stdout.strip() or None if result.returncode == 0 else None


def _relative(path: Path, root: Path) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return path.as_posix()


def _progress(message: str) -> None:
    import sys

    print(f"snagentic: {message}", file=sys.stderr, flush=True)
