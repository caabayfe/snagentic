"""Deterministic Markdown/MkDocs documentation generated from the instance mirror.

Generated pages may contain narrative blocks that are preserved across regeneration::

    <!-- snagentic:narrative id="scope-x_acme-purpose" -->
    Hand- or agent-written text.
    <!-- /snagentic:narrative -->
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
from collections import defaultdict
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any, cast

import yaml

from snagentic.errors import ConfigurationError
from snagentic.instance.config import InstanceConfig, InstancePaths
from snagentic.instance.docevidence import EVIDENCE_TABLES, DocumentationEvidence
from snagentic.instance.docpages import AuthoredSiteRenderer
from snagentic.instance.docsource import (
    DocumentationSource,
    DocumentationSourceRepository,
    DocumentKind,
    narrative,
    read_narratives,
)
from snagentic.instance.index import extract_references, record_name
from snagentic.instance.mirror import git
from snagentic.instance.records import META_FILE, LocalRecord, atomic_write, read_record
from snagentic.instance.updatesets import activity_view, collision_report, load_update_sets

FUNCTION = re.compile(r"^\s*([A-Za-z_$][\w$]*)\s*:\s*function\b|^\s*function\s+([A-Za-z_$][\w$]*)",
                      re.MULTILINE)
SECTIONS: list[tuple[str, str, list[tuple[str, str]]]] = [
    ("sys_script_include", "Script includes",
     [("API name", "api_name"), ("Client callable", "client_callable"), ("Active", "active")]),
    ("sys_script", "Business rules",
     [("Table", "collection"), ("When", "when"), ("Order", "order"), ("Insert", "action_insert"),
      ("Update", "action_update"), ("Delete", "action_delete"), ("Query", "action_query"),
      ("Active", "active")]),
    ("sys_script_client", "Client scripts",
     [("Table", "table"), ("Type", "type"), ("Field", "field"), ("Active", "active")]),
    ("sys_ui_policy", "UI policies", [("Table", "table"), ("Active", "active")]),
    ("sys_ui_action", "UI actions",
     [("Table", "table"), ("Action name", "action_name"), ("Active", "active")]),
    ("sys_security_acl", "Access controls",
     [("Type", "type"), ("Operation", "operation"), ("Admin overrides", "admin_overrides"),
      ("Active", "active")]),
    ("sys_ws_definition", "Scripted REST APIs",
     [("Base path", "base_uri"), ("Namespace", "namespace"), ("Active", "active")]),
    ("sys_ws_operation", "Scripted REST resources",
     [("Method", "http_method"), ("Path", "relative_path"), ("Active", "active")]),
    ("sys_hub_flow", "Flows and subflows",
     [("Type", "type"), ("Status", "status"), ("Active", "active")]),
    ("sysauto_script", "Scheduled script jobs",
     [("Run", "run_type"), ("Time", "run_time"), ("Active", "active")]),
    ("sysevent_register", "Events", [("Table", "table"), ("Description", "description")]),
    ("sys_properties", "System properties",
     [("Type", "type"), ("Description", "description")]),
    ("sys_rest_message", "Outbound REST messages", [("Endpoint", "rest_endpoint")]),
    ("sys_ui_page", "UI pages", [("Category", "category")]),
    ("sp_widget", "Service Portal widgets", [("ID", "id")]),
]
KNOWN_CLASSES = {table for table, _, _ in SECTIONS} | {"sys_db_object", "sys_dictionary"}


_LOADER: Any = getattr(yaml, "CSafeLoader", yaml.SafeLoader)


def _record_class(relative: Path) -> str:
    parts = relative.parts
    if parts and parts[0] == "domains":
        return parts[3] if len(parts) > 3 else ""
    return parts[1] if len(parts) > 1 else ""


def _cell(value: Any) -> str:
    text = "" if value is None else str(value)
    text = text.replace("|", "\\|").replace("\r", " ").replace("\n", " ").strip()
    return text[:120] + ("…" if len(text) > 120 else "")


def _table(headers: list[str], rows: Iterable[list[Any]]) -> str:
    materialized = list(rows)
    if not materialized:
        return "_None._\n"
    lines = ["| " + " | ".join(headers) + " |", "|" + "---|" * len(headers)]
    lines += ["| " + " | ".join(_cell(value) for value in row) + " |" for row in materialized]
    return "\n".join(lines) + "\n"


def _anchor(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")


def _mermaid_id(value: str) -> str:
    return "n_" + re.sub(r"[^A-Za-z0-9_]", "_", value)


class DocumentationGenerator:
    def __init__(self, paths: InstancePaths, config: InstanceConfig) -> None:
        self.paths = paths
        self.config = config
        self.pages = paths.docs / "pages"

    def _record_directories(self) -> list[Path]:
        prefix = self.paths.metadata.relative_to(self.paths.root).as_posix()
        tracked = git(self.paths.root, "ls-files", "-z", "--", prefix).stdout
        deleted = set(
            git(self.paths.root, "ls-files", "-z", "--deleted", "--", prefix).stdout.split("\0")
        )
        directories = {
            self.paths.root / path.rsplit("/", 1)[0]
            for path in tracked.split("\0")
            if path.endswith(f"/{META_FILE}") and path not in deleted
        }
        return sorted(directories)

    def load(self) -> list[LocalRecord]:
        records = [read_record(directory) for directory in self._record_directories()]
        return [record for record in records if record.sys_id]

    def _customized_dirs(self) -> set[Path] | None:
        """Record folders with customer updates, from the derived model (if built)."""

        source = self.paths.workspace / "model" / "customer-updates.yaml"
        if not source.is_file():
            return None
        data = yaml.load(  # noqa: S506 - _LOADER is CSafeLoader or SafeLoader
            source.read_text(encoding="utf-8"), Loader=_LOADER
        ) or {}
        return {
            self.paths.workspace / str(entry["path"])
            for entry in data.get("customer_updates") or []
            if isinstance(entry, dict) and entry.get("path")
        }

    def _evidence_records(self, source: DocumentationSource) -> list[LocalRecord]:
        references = []
        for document in source.documents():
            references.extend(document.evidence)
            for step in getattr(document, "steps", []):
                references.extend(getattr(step, "evidence", []))
        targets = {
            (EVIDENCE_TABLES[reference.kind], reference.target)
            for reference in references
            if reference.kind in EVIDENCE_TABLES
        }
        directories: set[Path] = set()
        prefix = self.paths.metadata.relative_to(self.paths.root).as_posix()
        for table, target in sorted(targets):
            pathspecs = [
                f":(glob){prefix}/*/{table}/*/{filename}"
                for filename in (META_FILE, "record.yaml")
            ]
            pathspecs += [
                f":(glob){prefix}/domains/*/*/{table}/*/{filename}"
                for filename in (META_FILE, "record.yaml")
            ]
            matches = git(
                self.paths.root,
                "grep",
                "-l",
                "-z",
                "-F",
                "-e",
                target,
                "--",
                *pathspecs,
                check=False,
            ).stdout
            directories.update(
                self.paths.root / path.rsplit("/", 1)[0]
                for path in matches.split("\0")
                if path
            )
        records = [read_record(directory) for directory in sorted(directories)]
        return [
            record
            for record in records
            if record.sys_id
            and any(
                record.table == table
                and target in {record_name(record), record.sys_id}
                for table, target in targets
            )
        ]

    def generate(self, *, check: bool = False, strict: bool = False) -> dict[str, Any]:
        # With the full out-of-box mirror, detailed pages cover customer changes only;
        # the platform baseline is summarized by class and described by model/tables/.
        source_repository = DocumentationSourceRepository(self.paths.documentation)
        source = source_repository.load()
        customized = self._customized_dirs()
        directories = self._record_directories()
        evidence_records = self._evidence_records(source) if not source.empty else []
        mirrored: dict[str, int] = defaultdict(int)
        if customized is None:
            records = [read_record(directory) for directory in directories]
            records = [record for record in records if record.sys_id]
            for record in records:
                mirrored[record.table] += 1
        else:
            records = []
            for directory in directories:
                table = _record_class(directory.relative_to(self.paths.metadata))
                mirrored[table] += 1
                if directory in customized:
                    record = read_record(directory)
                    if record.sys_id:
                        records.append(record)
        existing = {**source.legacy_narratives, **read_narratives(self.pages)}
        by_scope: dict[str, list[LocalRecord]] = defaultdict(list)
        for record in records:
            by_scope[str(record.meta.get("scope", "global"))].append(record)
        include_names = {
            record_name(record).split(".")[-1]
            for record in records if record.table == "sys_script_include"
        }
        written: dict[str, str] = {}
        written["index.md"] = self._overview(
            records,
            by_scope,
            existing,
            mirrored,
            baseline=customized is not None,
            source=source,
        )
        for scope, scoped in sorted(by_scope.items()):
            written[f"scopes/{scope}.md"] = self._scope_page(scope, scoped, include_names,
                                                             existing)
        from snagentic.instance.functional import FunctionalDocs

        functional = FunctionalDocs(self.paths.workspace, self.paths.root, narrative)
        tables = functional.tables(self.config.docs.tables)
        for table in tables:
            written[f"tables/{table}.md"] = functional.render(table, existing)
        if tables:
            written["tables/index.md"] = "\n".join([
                "# Functional behaviour by table\n",
                "What the instance does for each table: server processing, form behaviour, "
                "actions, access control, notifications and service levels.\n",
                *[f"- [{table}]({table}.md)" for table in tables], "",
            ])
        written["update-sets.md"] = self._update_sets_page(existing)
        written["artifacts.md"] = self._artifact_catalog(records)

        findings: list[dict[str, str]] = []
        documentation_manifest: dict[str, Any] = {"version": 1, "documents": {}}
        if not source.empty:
            authored, documentation_manifest, findings = AuthoredSiteRenderer(
                source,
                DocumentationEvidence(self.paths.workspace, self.paths.root, evidence_records),
                self.paths.documentation,
            ).render()
            written.update(authored)

        mkdocs = self._mkdocs_text(sorted(by_scope), tables, source)
        manifest_text = json.dumps(documentation_manifest, indent=2, sort_keys=True) + "\n"
        differences = self._differences(written, mkdocs, manifest_text)
        if check:
            findings += [
                {
                    "severity": "error",
                    "document": path,
                    "code": "generated_output_stale",
                    "message": f"generated documentation differs: {path}",
                }
                for path in differences
            ]
            blocking = [finding for finding in findings if finding["severity"] == "error"]
            if blocking:
                summary = "\n".join(
                    f"- {finding['document']}: {finding['message']}" for finding in blocking[:25]
                )
                raise ConfigurationError(f"documentation check failed:\n{summary}")
            if strict:
                self._strict_build()
        else:
            self._write(written, mkdocs, manifest_text)
            self._sync_assets()
            if strict:
                self._strict_build()

        return {
            "instance": self.config.name,
            "pages": sorted(written),
            "records": len(records),
            "mirrored_records": sum(mirrored.values()),
            "narrative_blocks_preserved": len(existing),
            "authored_documents": len(source.documents()),
            "findings": findings,
            "check": check,
            "mkdocs": (self.paths.docs / "mkdocs.yml").relative_to(self.paths.root).as_posix(),
        }

    def scaffold(self, kind: str, document_id: str) -> dict[str, Any]:
        if kind not in {"capability", "process", "guide"}:
            raise ConfigurationError(f"unsupported documentation type: {kind}")
        path = DocumentationSourceRepository(self.paths.documentation).scaffold(
            cast(DocumentKind, kind), document_id
        )
        return {
            "instance": self.config.name,
            "created": path.relative_to(self.paths.root).as_posix(),
        }

    def migrate(self) -> dict[str, Any]:
        result = DocumentationSourceRepository(self.paths.documentation).migrate_narratives(
            self.pages
        )
        path = Path(str(result["path"]))
        result["path"] = path.relative_to(self.paths.root).as_posix()
        result["instance"] = self.config.name
        return result

    # -- pages -----------------------------------------------------------------
    def _overview(
        self,
        records: list[LocalRecord],
        by_scope: Mapping[str, list[LocalRecord]],
        existing: Mapping[str, str],
        mirrored: Mapping[str, int],
        *,
        baseline: bool,
        source: DocumentationSource,
    ) -> str:
        counts: dict[str, int] = defaultdict(int)
        for record in records:
            counts[record.table] += 1
        scope_note = (
            "The pages below document **customer changes** (records with customer updates). "
            "The full platform baseline is mirrored under `metadata/` and summarized per table in "
            f"`{self.paths.relative_workspace.as_posix()}/model/tables/`.\n"
            if baseline else ""
        )
        out = [
            f"# {self.config.name} — ServiceNow instance documentation\n",
            f"Instance kind: **{self.config.kind}**. Generated from the mirrored metadata in "
            f"`{self.paths.relative_workspace.as_posix()}/metadata`; regenerate with "
            f"`snagentic instance -i {self.config.name} docs`.\n",
            scope_note,
            "## Purpose\n",
            (
                source.manifest.introduction.strip() + "\n"
                if source.manifest.introduction.strip()
                else narrative("instance-purpose", existing)
            ),
            *(
                [
                    "## Explore the instance\n",
                    *(
                        ["- [Capabilities](capabilities/index.md)"]
                        if source.capabilities
                        else []
                    ),
                    *(
                        ["- [Processes](processes/index.md)"]
                        if source.processes
                        else []
                    ),
                    *(
                        ["- [How-to guides](guides/index.md)"]
                        if source.guides
                        else []
                    ),
                    "- [Documentation governance](governance.md)",
                    "",
                ]
                if not source.empty
                else []
            ),
            "## Applications and scopes\n",
            _table(
                ["Scope", "Artifacts", "Last change"],
                [
                    [f"[{scope}](scopes/{scope}.md)", len(items),
                     max((str(r.meta.get("sys_updated_on", "")) for r in items), default="")]
                    for scope, items in sorted(by_scope.items())
                ],
            ),
            "\n## Artifact types\n",
            _table(["Class", "Documented", "Mirrored"],
                   sorted(([k, counts.get(k, 0), v] for k, v in mirrored.items()),
                          key=lambda row: (-int(row[2]), str(row[0])))),
        ]
        return "\n".join(out)

    def _scope_page(
        self,
        scope: str,
        records: list[LocalRecord],
        include_names: set[str],
        existing: Mapping[str, str],
    ) -> str:
        by_class: dict[str, list[LocalRecord]] = defaultdict(list)
        for record in records:
            by_class[record.table].append(record)
        for items in by_class.values():
            items.sort(key=record_name)
        out = [f"# Scope `{scope}`\n", "## Purpose\n",
               narrative(f"scope-{scope}-purpose", existing)]
        out += self._data_model(by_class)
        for table, title, columns in SECTIONS:
            items = by_class.get(table, [])
            if not items:
                continue
            out.append(f"## {title}\n")
            out.append(_table(["Name", *[label for label, _ in columns]],
                              [[record_name(r), *[r.values.get(f, "") for _, f in columns]]
                               for r in items]))
            if table == "sys_script_include":
                out.append(self._script_include_details(items, include_names, existing))
        out.append(self._dependency_graph(records, include_names))
        others = sorted((t, len(v)) for t, v in by_class.items() if t not in KNOWN_CLASSES)
        if others:
            out.append("## Other artifacts\n")
            out.append(_table(["Class", "Count"], [[t, n] for t, n in others]))
        return "\n".join(out)

    def _data_model(self, by_class: Mapping[str, list[LocalRecord]]) -> list[str]:
        tables = by_class.get("sys_db_object", [])
        fields = by_class.get("sys_dictionary", [])
        if not tables and not fields:
            return []
        by_table: dict[str, list[LocalRecord]] = defaultdict(list)
        for field in fields:
            if field.values.get("element"):
                by_table[field.values.get("name", "")].append(field)
        out = ["## Data model\n"]
        names = sorted({t.values.get("name", "") for t in tables} | set(by_table))
        edges = sorted({
            (table, field.values["reference"], field.values.get("element", ""))
            for table, items in by_table.items() for field in items
            if field.values.get("reference")
        })
        if edges:
            out.append("```mermaid\nerDiagram")
            for source, target, element in edges:
                out.append(f'    {_mermaid_id(source)} }}o--|| {_mermaid_id(target)} : "{element}"')
            out.append("```\n")
        labels = {t.values.get("name", ""): t.values.get("label", "") for t in tables}
        for name in names:
            out.append(f"### Table `{name}`\n")
            if labels.get(name):
                out.append(f"{labels[name]}\n")
            out.append(_table(
                ["Field", "Label", "Type", "Reference", "Mandatory"],
                [[f.values.get("element"), f.values.get("column_label"),
                  f.values.get("internal_type"), f.values.get("reference"),
                  f.values.get("mandatory")]
                 for f in sorted(by_table.get(name, []),
                                 key=lambda f: f.values.get("element", ""))],
            ))
        return out

    def _script_include_details(
        self, items: list[LocalRecord], include_names: set[str], existing: Mapping[str, str]
    ) -> str:
        out: list[str] = []
        for record in items:
            name = record_name(record)
            script = record.values.get("script", "")
            functions = sorted({a or b for a, b in FUNCTION.findall(script)} - {"initialize"})
            calls = sorted({
                ref.target for ref in extract_references(record, include_names)
                if ref.kind == "calls_script_include"
            })
            tables = sorted({
                ref.target for ref in extract_references(record, include_names)
                if ref.kind == "queries_table"
            })
            out.append(f"### `{name}`\n")
            if record.values.get("description"):
                out.append(f"{record.values['description']}\n")
            out.append(narrative(f"si-{_anchor(name)}", existing))
            if functions:
                out.append("**Functions:** " + ", ".join(f"`{f}`" for f in functions) + "\n")
            if calls:
                out.append("**Uses script includes:** " + ", ".join(f"`{c}`" for c in calls) + "\n")
            if tables:
                out.append("**Queries tables:** " + ", ".join(f"`{t}`" for t in tables) + "\n")
            out.append(f"Source: `{record.directory.relative_to(self.paths.root).as_posix()}`\n")
        return "\n".join(out)

    def _dependency_graph(self, records: list[LocalRecord], include_names: set[str]) -> str:
        prefixes = {"calls_script_include": "si:", "queries_table": "table:",
                    "fires_event": "event:"}
        labels = {"calls_script_include": "calls", "queries_table": "queries",
                  "fires_event": "fires"}
        edges: set[tuple[str, str, str, str]] = set()
        for record in records:
            name = record_name(record)
            source = ("si:" + name.split(".")[-1]) if record.table == "sys_script_include" else (
                "rec:" + name
            )
            for ref in extract_references(record, include_names):
                if ref.kind in prefixes:
                    edges.add((source, name, prefixes[ref.kind] + ref.target, ref.kind))
        if not edges:
            return ""
        lines = ["## Dependencies\n", "```mermaid", "flowchart LR"]
        for source, name, target, kind in sorted(edges)[:400]:
            lines.append(
                f'    {_mermaid_id(source)}["{name}"] -->|{labels[kind]}| '
                f'{_mermaid_id(target)}["{target.split(":", 1)[1]}"]'
            )
        lines.append("```\n")
        return "\n".join(lines)

    def _update_sets_page(self, existing: Mapping[str, str]) -> str:
        update_sets = load_update_sets(self.paths.update_sets)
        activity = activity_view(update_sets)
        collisions = collision_report(update_sets)
        by_state: dict[str, dict[str, Any]] = defaultdict(
            lambda: {"sets": 0, "changes": 0, "last_change": ""}
        )
        for update_set in activity["update_sets"]:
            state = str(update_set["state"])
            summary = by_state[state]
            summary["sets"] += 1
            summary["changes"] += int(update_set["changes"])
            summary["last_change"] = max(
                str(summary["last_change"]), str(update_set["last_change"])
            )
        out = [
            "# Update-set summary\n",
            "Aggregate update-set and collision information without personal identifiers.\n",
            "## Open update set collisions\n",
            _table(
                ["Record", "Open update sets"],
                [[c["record"], len(c["holders"])]
                 for c in collisions["between_update_sets"]],
            ),
            "\n## Update sets by state\n",
            _table(
                ["State", "Sets", "Changes", "Last change"],
                [
                    [state, data["sets"], data["changes"], data["last_change"]]
                    for state, data in sorted(by_state.items())
                ],
            ),
        ]
        return "\n".join(out)

    def _artifact_catalog(self, records: list[LocalRecord]) -> str:
        return "\n".join([
            "# Artifact catalog\n",
            _table(["Class", "Name", "Scope", "Updated", "Path"],
                   [[r.table, record_name(r), r.meta.get("scope"), r.meta.get("sys_updated_on"),
                     f"`{r.directory.relative_to(self.paths.root).as_posix()}`"]
                    for r in sorted(records, key=lambda r: (r.table, record_name(r)))]),
        ])

    def _differences(
        self, written: Mapping[str, str], mkdocs: str, manifest: str
    ) -> list[str]:
        differences = [
            f"pages/{relative}"
            for relative, content in written.items()
            if not (self.pages / relative).is_file()
            or (self.pages / relative).read_text(encoding="utf-8") != content
        ]
        if self.pages.is_dir():
            expected = set(written)
            differences += [
                f"pages/{path.relative_to(self.pages).as_posix()}"
                for path in self.pages.rglob("*.md")
                if path.relative_to(self.pages).as_posix() not in expected
            ]
        for relative, content in (
            ("mkdocs.yml", mkdocs),
            ("documentation-manifest.json", manifest),
        ):
            path = self.paths.docs / relative
            if not path.is_file() or path.read_text(encoding="utf-8") != content:
                differences.append(relative)
        source_assets = self.paths.documentation / "assets"
        target_assets = self.pages / "assets"
        expected_assets: set[str] = set()
        if source_assets.is_dir():
            for source in source_assets.rglob("*"):
                if not source.is_file() or source.is_symlink():
                    continue
                relative = source.relative_to(source_assets).as_posix()
                expected_assets.add(relative)
                target = target_assets / relative
                if not target.is_file() or target.read_bytes() != source.read_bytes():
                    differences.append(f"pages/assets/{relative}")
        if target_assets.is_dir():
            differences += [
                f"pages/assets/{path.relative_to(target_assets).as_posix()}"
                for path in target_assets.rglob("*")
                if path.is_file()
                and path.relative_to(target_assets).as_posix() not in expected_assets
            ]
        return sorted(set(differences))

    def _write(self, written: Mapping[str, str], mkdocs: str, manifest: str) -> None:
        if self.pages.exists():
            for stale in self.pages.rglob("*.md"):
                if stale.relative_to(self.pages).as_posix() not in written:
                    stale.unlink()
        for relative, content in written.items():
            atomic_write(self.pages / relative, content)
        atomic_write(self.paths.docs / "mkdocs.yml", mkdocs)
        atomic_write(self.paths.docs / "documentation-manifest.json", manifest)

    def _sync_assets(self) -> None:
        source = self.paths.documentation / "assets"
        target = self.pages / "assets"
        expected: set[Path] = set()
        if source.is_dir():
            for asset in source.rglob("*"):
                if not asset.is_file() or asset.is_symlink():
                    continue
                relative = asset.relative_to(source)
                expected.add(relative)
                destination = target / relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(asset, destination)
        if target.is_dir():
            for stale in target.rglob("*"):
                if stale.is_file() and stale.relative_to(target) not in expected:
                    stale.unlink()

    def _strict_build(self) -> None:
        result = subprocess.run(  # noqa: S603 - fixed interpreter and argument vector
            [
                sys.executable,
                "-m",
                "mkdocs",
                "build",
                "--strict",
                "--config-file",
                str(self.paths.docs / "mkdocs.yml"),
            ],
            cwd=self.paths.root,
            text=True,
            capture_output=True,
            check=False,
        )
        if result.returncode:
            detail = (result.stderr or result.stdout).strip()
            raise ConfigurationError(f"MkDocs strict build failed: {detail}")

    def _mkdocs_text(
        self, scopes: list[str], tables: list[str], source: DocumentationSource
    ) -> str:
        curated_nav: list[dict[str, Any]] = []
        if source.capabilities:
            curated_nav.append(
                {
                    "Capabilities": [
                        {"Overview": "capabilities/index.md"},
                        *[
                            {
                                source.capabilities[identifier].title:
                                    f"capabilities/{identifier}.md"
                            }
                            for identifier in _ordered_source_ids(
                                source.manifest.capabilities, source.capabilities
                            )
                        ],
                    ]
                }
            )
        if source.processes:
            curated_nav.append(
                {
                    "Processes": [
                        {"Overview": "processes/index.md"},
                        *[
                            {source.processes[identifier].title: f"processes/{identifier}.md"}
                            for identifier in _ordered_source_ids(
                                source.manifest.processes, source.processes
                            )
                        ],
                    ]
                }
            )
        if source.guides:
            curated_nav.append(
                {
                    "How-to guides": [
                        {"Overview": "guides/index.md"},
                        *[
                            {source.guides[identifier].title: f"guides/{identifier}.md"}
                            for identifier in _ordered_source_ids(
                                source.manifest.guides, source.guides
                            )
                        ],
                    ]
                }
            )
        config = {
            "site_name": (
                source.manifest.site_name
                or f"{self.config.name} ServiceNow documentation"
            ),
            "docs_dir": "pages",
            "site_dir": "../../../.snagentic/" + self.config.name + "/site",
            "theme": {"name": "material"},
            "markdown_extensions": [
                "tables",
                {"pymdownx.superfences": {"custom_fences": [
                    {"name": "mermaid", "class": "mermaid",
                     "format": "!!python/name:pymdownx.superfences.fence_code_format"}
                ]}},
            ],
            "nav": [
                {"Overview": "index.md"},
                *curated_nav,
                *([{"Governance": "governance.md"}] if not source.empty else []),
                *([{"Technical evidence": "evidence/index.md"}] if not source.empty else []),
                {"Scopes": [{scope: f"scopes/{scope}.md"} for scope in scopes]},
                *([{"Behaviour by table": [{"Overview": "tables/index.md"},
                                           *[{t: f"tables/{t}.md"} for t in tables]]}]
                  if tables else []),
                {"Update sets": "update-sets.md"},
                {"Artifact catalog": "artifacts.md"},
            ],
        }
        return yaml.safe_dump(config, sort_keys=False).replace(
            "'!!python/name:pymdownx.superfences.fence_code_format'",
            "!!python/name:pymdownx.superfences.fence_code_format",
        )


def _ordered_source_ids(preferred: list[str], available: Mapping[str, Any]) -> list[str]:
    return [*preferred, *sorted(set(available) - set(preferred))]
