"""Collect review targets from an instance mirror or a change plan."""

from __future__ import annotations

import os
from collections.abc import Iterable, Iterator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import yaml

from snagentic.instance.records import META_FILE, RECORD_FILE, LocalRecord, read_record
from snagentic.review.engine import (
    DESCRIBED_TABLES,
    SCRIPT_FIELDS,
    SEVERITY_ORDER,
    Finding,
    Reviewer,
    ReviewTarget,
    Standards,
    summarize,
)
from snagentic.review.rules import RULES

STANDARDS_FILE = "standards.yaml"
REVIEWED_TABLES = frozenset(SCRIPT_FIELDS) | DESCRIBED_TABLES


def reviewer_for(workspace: Path) -> Reviewer:
    return Reviewer(RULES, Standards.load(workspace / STANDARDS_FILE, (r.id for r in RULES)))


def _customer_updates(workspace: Path) -> list[dict[str, Any]] | None:
    source = workspace / "model" / "customer-updates.yaml"
    if not source.is_file():
        return None
    raw = yaml.safe_load(source.read_text(encoding="utf-8")) or {}
    entries = raw.get("customer_updates") if isinstance(raw, dict) else None
    if not isinstance(entries, list):
        return None
    return [entry for entry in entries if isinstance(entry, dict)]


def load_customized(workspace: Path) -> set[str] | None:
    """``sys_update_name`` values of customer-updated records (``model/customer-updates``).

    ``None`` when the model has not been generated, so "out of box" cannot be decided.
    """

    entries = _customer_updates(workspace)
    if entries is None:
        return None
    return {str(entry["name"]) for entry in entries if entry.get("name")}


def customized_state(meta: dict[str, Any], customized: set[str] | None) -> bool | None:
    if customized is None:
        return None
    name = meta.get("sys_update_name")
    if not isinstance(name, str) or not name:
        return None
    return name in customized


def _location(relative: Path) -> tuple[str, str] | None:
    """(scope, table) of a record folder relative to ``metadata/``."""

    parts = relative.parts
    if parts and parts[0] == "domains":
        return (parts[2], parts[3]) if len(parts) >= 5 else None
    return (parts[0], parts[1]) if len(parts) >= 3 else None


def _table_dirs(metadata: Path) -> Iterator[tuple[str, str, Path]]:
    """(scope, table, directory) for every reviewed table folder, including domains."""

    if not metadata.is_dir():
        return
    roots: list[Path] = [metadata]
    domains = metadata / "domains"
    if domains.is_dir():
        roots.extend(sorted(p for p in domains.iterdir() if p.is_dir()))
    for root in roots:
        for scope_dir in sorted(p for p in root.iterdir() if p.is_dir()):
            if root == metadata and scope_dir.name == "domains":
                continue
            for table_dir in sorted(p for p in scope_dir.iterdir() if p.is_dir()):
                if table_dir.name in REVIEWED_TABLES:
                    yield scope_dir.name, table_dir.name, table_dir


def mirror_targets(
    root: Path,
    workspace: Path,
    *,
    tables: Iterable[str] = (),
    scopes: Iterable[str] = (),
    paths: Iterable[str] = (),
    customized_only: bool = False,
    customized: set[str] | None = None,
) -> Iterator[ReviewTarget]:
    """Existing records in ``<workspace>/metadata`` (the working tree), filtered."""

    metadata = workspace / "metadata"
    wanted_tables = set(tables)
    wanted_scopes = set(scopes)
    prefixes = [p.strip("/") for p in paths]
    only: set[str] | None = None
    if customized_only:
        only = {str(entry.get("path")) for entry in _customer_updates(workspace) or []}
    candidates: list[tuple[str, str, Path]] = []
    for scope, table, table_dir in _table_dirs(metadata):
        if wanted_tables and table not in wanted_tables:
            continue
        if wanted_scopes and scope not in wanted_scopes:
            continue
        for entry in sorted(os.scandir(table_dir), key=lambda e: e.name):
            if not entry.is_dir():
                continue
            directory = Path(entry.path)
            relative = directory.relative_to(workspace).as_posix()
            if only is not None and relative not in only:
                continue
            path = directory.relative_to(root).as_posix()
            if prefixes and not any(
                    path.startswith(p) or relative.startswith(p) for p in prefixes):
                continue
            candidates.append((scope, table, directory))
    # Reading many small files is I/O bound; overlap the reads.
    with ThreadPoolExecutor(max_workers=8) as pool:
        records = pool.map(_read_if_record, (directory for _, _, directory in candidates))
        for (scope, table, directory), record in zip(candidates, records, strict=True):
            if record is None:
                continue
            state = customized_state(record.meta, customized)
            if customized_only and state is not True:
                continue
            yield ReviewTarget(directory.relative_to(root).as_posix(), table, scope,
                               "existing", record.values, None, state)


def _read_if_record(directory: Path) -> LocalRecord | None:
    if (directory / META_FILE).is_file() or (directory / RECORD_FILE).is_file():
        return read_record(directory)
    return None


def review_mirror(
    root: Path,
    workspace: Path,
    *,
    tables: Iterable[str] = (),
    scopes: Iterable[str] = (),
    paths: Iterable[str] = (),
    customized_only: bool = False,
    rules: Iterable[str] = (),
    min_severity: str = "info",
    limit: int = 200,
) -> dict[str, Any]:
    reviewer = reviewer_for(workspace)
    customized = load_customized(workspace)
    if customized_only and customized is None:
        raise ValueError("--customized needs model/customer-updates.yaml; fetch the "
                         "instance with the table model enabled first")
    reviewed = 0
    findings: list[Finding] = []
    for target in mirror_targets(root, workspace, tables=tables, scopes=scopes, paths=paths,
                                 customized_only=customized_only, customized=customized):
        reviewed += 1
        findings.extend(reviewer.review(target))
    return report(findings, reviewed=reviewed, rules=rules, min_severity=min_severity,
                  limit=limit)


def report(findings: Iterable[Finding], *, reviewed: int, rules: Iterable[str] = (),
           min_severity: str = "info", limit: int = 200,
           mode: str = "report-only") -> dict[str, Any]:
    wanted = set(rules)
    threshold = SEVERITY_ORDER[min_severity]
    selected = [
        f for f in findings
        if (not wanted or f.rule_id in wanted) and SEVERITY_ORDER[f.severity] <= threshold
    ]
    selected.sort(key=lambda f: (SEVERITY_ORDER[f.severity], f.rule_id, f.path,
                                 f.field or "", f.line or 0))
    result: dict[str, Any] = {
        "summary": summarize(selected, reviewed=reviewed, mode=mode),
        "findings": [f.as_dict() for f in selected[:limit]],
    }
    if len(selected) > limit:
        result["truncated"] = len(selected) - limit
    return result


def review_changes(workspace: Path, targets: list[ReviewTarget]) -> list[Finding]:
    return reviewer_for(workspace).review_all(targets)


def change_gate(workspace: Path, workspace_prefix: str, findings: list[Finding],
                plan_id: str) -> dict[str, Any]:
    """Gate state for a plan's introduced findings (see ``snagentic.review.gate``)."""

    from snagentic.review.gate import evaluate_gate, load_review, load_waivers

    known = [rule.id for rule in RULES]
    settings = Standards.load(workspace / STANDARDS_FILE, known).gate
    return evaluate_gate(
        findings, settings=settings, waivers=load_waivers(workspace, known),
        workspace_prefix=workspace_prefix, plan_id=plan_id,
        review=load_review(workspace, plan_id),
    )
