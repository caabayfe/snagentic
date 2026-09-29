"""Render authored documentation and resolved instance evidence as Markdown."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable, Mapping
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from snagentic.instance.docevidence import DocumentationEvidence, ResolvedEvidence
from snagentic.instance.docsource import (
    AuthoredDocument,
    CapabilityDocument,
    DocumentationSource,
    GuideDocument,
    ProcessDocument,
    ProcessTransition,
)

GENERATOR_VERSION = 1
AUDIENCE_LABELS = {
    "end_users": "End users",
    "process_owners": "Process owners",
    "admins": "Administrators",
    "developers": "Developers",
}


class AuthoredSiteRenderer:
    def __init__(
        self,
        source: DocumentationSource,
        evidence: DocumentationEvidence,
        documentation_root: Path,
        today: date | None = None,
    ) -> None:
        self.source = source
        self.evidence = evidence
        self.documentation_root = documentation_root
        self.today = today or date.today()
        self.findings: list[dict[str, str]] = []
        self.resolved: dict[str, list[ResolvedEvidence]] = {}
        self.fingerprints: dict[str, str] = {}
        self.uncovered_tables: list[str] = []

    def render(self) -> tuple[dict[str, str], dict[str, Any], list[dict[str, str]]]:
        pages: dict[str, str] = {}
        documents = {document.id: document for document in self.source.documents()}
        manifest_hash = hashlib.sha256(
            json.dumps(
                self.source.manifest.model_dump(mode="json"),
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()
        source_hashes = {
            identifier: hashlib.sha256(document.source_path.read_bytes()).hexdigest()
            for identifier, document in documents.items()
        }
        for document in documents.values():
            resolved = self.evidence.resolve_document(document)
            self.resolved[document.id] = resolved
            for item in resolved:
                if item.status != "resolved":
                    self.findings.append(
                        {
                            "severity": "error",
                            "document": document.id,
                            "code": f"evidence_{item.status}",
                            "message": item.message or f"{item.kind} {item.target} is unresolved",
                        }
                    )
            self._review_findings(document)
            linked = _linked_ids(document)
            material = {
                "generator_version": GENERATOR_VERSION,
                "manifest": manifest_hash,
                "source": source_hashes[document.id],
                "linked_sources": {
                    identifier: source_hashes[identifier]
                    for identifier in linked
                    if identifier in source_hashes
                },
                "evidence": [item.as_dict() for item in resolved],
            }
            self.fingerprints[document.id] = hashlib.sha256(
                json.dumps(material, sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest()
        covered_tables = {
            item.target
            for items in self.resolved.values()
            for item in items
            if item.kind == "table" and item.status == "resolved"
        }
        self.uncovered_tables = sorted(
            set(self.evidence.customized_tables()) - covered_tables
        )
        self.findings += [
            {
                "severity": "warning",
                "document": table,
                "code": "coverage_gap",
                "message": "customized table is not linked from curated documentation",
            }
            for table in self.uncovered_tables
        ]

        for identifier in _ordered(
            self.source.manifest.capabilities, self.source.capabilities
        ):
            pages[f"capabilities/{identifier}.md"] = self._capability(
                self.source.capabilities[identifier]
            )
        for identifier in _ordered(self.source.manifest.processes, self.source.processes):
            pages[f"processes/{identifier}.md"] = self._process(
                self.source.processes[identifier]
            )
        for identifier in _ordered(self.source.manifest.guides, self.source.guides):
            pages[f"guides/{identifier}.md"] = self._guide(self.source.guides[identifier])
        if self.source.capabilities:
            pages["capabilities/index.md"] = self._index(
                "Capabilities",
                "Business outcomes and the processes, guides, and technical areas "
                "that support them.",
                [
                    self.source.capabilities[identifier]
                    for identifier in _ordered(
                        self.source.manifest.capabilities, self.source.capabilities
                    )
                ],
            )
        if self.source.processes:
            pages["processes/index.md"] = self._index(
                "Processes",
                "Trigger-to-outcome views of work, decisions, handoffs, exceptions, and controls.",
                [
                    self.source.processes[identifier]
                    for identifier in _ordered(
                        self.source.manifest.processes, self.source.processes
                    )
                ],
            )
        if self.source.guides:
            pages["guides/index.md"] = self._index(
                "How-to guides",
                "Role-specific instructions for completing common tasks.",
                [
                    self.source.guides[identifier]
                    for identifier in _ordered(self.source.manifest.guides, self.source.guides)
                ],
            )
        if documents:
            pages["governance.md"] = self._governance(documents.values())
            pages.update(self._evidence_pages(documents))

        manifest = {
            "version": GENERATOR_VERSION,
            "documents": {
                identifier: {
                    "fingerprint": self.fingerprints[identifier],
                    "source": documents[identifier]
                    .source_path.relative_to(self.documentation_root.parent)
                    .as_posix(),
                    "status": documents[identifier].status,
                    "reviewed_on": _iso_date(documents[identifier].reviewed_on),
                    "evidence": [item.as_dict() for item in self.resolved[identifier]],
                }
                for identifier in sorted(documents)
            },
        }
        return pages, manifest, self.findings

    def _capability(self, document: CapabilityDocument) -> str:
        out = self._header(document)
        if document.benefits:
            out += ["## Benefits\n", *_bullets(document.benefits)]
        out += _scope(document)
        if document.entry_points:
            out += ["## Entry points\n", *_bullets(document.entry_points)]
        if document.processes:
            out += [
                "## Supported processes\n",
                *_linked_bullets(document.processes, self.source.processes, "../processes"),
            ]
        if document.guides:
            out += [
                "## User guides\n",
                *_linked_bullets(document.guides, self.source.guides, "../guides"),
            ]
        if document.processes:
            out += ["## Capability map\n", "```mermaid", "flowchart LR"]
            capability = _node_id("capability-" + document.id)
            out.append(f'    {capability}["{_diagram_text(document.title)}"]')
            for process_id in document.processes:
                process = self.source.processes[process_id]
                process_node = _node_id("process-" + process_id)
                out.append(f'    {capability} --> {process_node}["{_diagram_text(process.title)}"]')
                for guide_id in process.guides:
                    guide = self.source.guides[guide_id]
                    out.append(
                        f'    {process_node} --> {_node_id("guide-" + guide_id)}'
                        f'["{_diagram_text(guide.title)}"]'
                    )
            out += ["```\n"]
        out += self._body_and_evidence(document)
        return "\n".join(out).rstrip() + "\n"

    def _process(self, document: ProcessDocument) -> str:
        out = self._header(document)
        out += [
            "## Process definition\n",
            f"**Trigger:** {document.trigger}\n",
            "**Actors:** " + ", ".join(document.actors) + "\n",
        ]
        for title, values in (
            ("Preconditions", document.preconditions),
            ("Inputs", document.inputs),
            ("Outputs and outcomes", document.outputs),
        ):
            if values:
                out += [f"### {title}\n", *_bullets(values)]
        out += ["## Process flow\n", "```mermaid", "flowchart TD"]
        for step in document.steps:
            shape = f'["{_diagram_text(step.title)}<br/>{_diagram_text(step.actor)}"]'
            out.append(f"    {_node_id(step.id)}{shape}")
            for transition in step.next:
                target = _transition_target(transition)
                label = _transition_label(transition)
                arrow = f" -->|{_diagram_text(label)}| " if label else " --> "
                out.append(f"    {_node_id(step.id)}{arrow}{_node_id(target)}")
        out += ["```\n", "## Steps\n"]
        out += [
            "| Step | Actor | Action | System behaviour | Next |",
            "|---|---|---|---|---|",
        ]
        for step in document.steps:
            out.append(
                f"| `{step.id}` — {_cell(step.title)} | {_cell(step.actor)} | "
                f"{_cell(step.action)} | {_cell(step.system_behavior or '—')} | "
                f"{_cell(', '.join(_render_transition(value) for value in step.next) or 'End')} |"
            )
        out.append("")
        if document.states:
            out += ["## Lifecycle\n", "```mermaid", "stateDiagram-v2"]
            for state in document.states:
                out.append(f'    {_node_id(state.id)}: {_diagram_text(state.title)}')
                for target in state.next:
                    out.append(f"    {_node_id(state.id)} --> {_node_id(target)}")
            out += [
                "```\n",
                "| State | Next states |",
                "|---|---|",
                *[
                    f"| `{state.id}` — {_cell(state.title)} | "
                    f"{_cell(', '.join(state.next) or 'End')} |"
                    for state in document.states
                ],
                "",
            ]
        if document.interactions:
            participants = sorted(
                {
                    participant
                    for interaction in document.interactions
                    for participant in (interaction.source, interaction.target)
                }
            )
            participant_ids = {
                participant: f"participant_{index}"
                for index, participant in enumerate(participants, start=1)
            }
            out += ["## System interactions\n", "```mermaid", "sequenceDiagram"]
            for participant in participants:
                out.append(
                    f"    participant {participant_ids[participant]} as "
                    f"{_diagram_text(participant)}"
                )
            for interaction in document.interactions:
                out.append(
                    f"    {participant_ids[interaction.source]}->>"
                    f"{participant_ids[interaction.target]}: "
                    f"{_diagram_text(interaction.message)}"
                )
            out += [
                "```\n",
                "| From | To | Interaction |",
                "|---|---|---|",
                *[
                    f"| {_cell(item.source)} | {_cell(item.target)} | {_cell(item.message)} |"
                    for item in document.interactions
                ],
                "",
            ]
        if document.guides:
            out += [
                "## Related user guides\n",
                *_linked_bullets(document.guides, self.source.guides, "../guides"),
            ]
        out += self._body_and_evidence(document)
        return "\n".join(out).rstrip() + "\n"

    def _guide(self, document: GuideDocument) -> str:
        out = self._header(document)
        out += [
            "## Goal\n",
            document.goal + "\n",
            f"**Role:** {document.role}\n",
        ]
        if document.prerequisites:
            out += ["## Before you start\n", *_bullets(document.prerequisites)]
        out += ["## Instructions\n"]
        for index, step in enumerate(document.steps, start=1):
            out += [f"### {index}. {step.title}\n", step.instruction + "\n"]
            if step.expected_result:
                out.append(f"**Expected result:** {step.expected_result}\n")
        if document.troubleshooting:
            out += ["## Troubleshooting\n", *_bullets(document.troubleshooting)]
        if document.escalation:
            out += ["## Escalation\n", document.escalation + "\n"]
        if document.processes:
            out += [
                "## Process context\n",
                *_linked_bullets(document.processes, self.source.processes, "../processes"),
            ]
        out += self._body_and_evidence(document)
        return "\n".join(out).rstrip() + "\n"

    def _header(self, document: AuthoredDocument) -> list[str]:
        reviewed = document.reviewed_on.isoformat() if document.reviewed_on else "Not reviewed"
        return [
            f"# {document.title}\n",
            document.summary + "\n",
            "| Audience | Owner | Status | Last review |",
            "|---|---|---|---|",
            f"| {', '.join(AUDIENCE_LABELS[value] for value in document.audiences)} | "
            f"{', '.join(document.owners) or 'Unassigned'} | {document.status} | {reviewed} |",
            "",
        ]

    def _body_and_evidence(self, document: AuthoredDocument) -> list[str]:
        out: list[str] = []
        if document.body:
            out += [document.body.strip(), ""]
        resolved = self.resolved.get(document.id, [])
        if resolved:
            out += [
                "## Technical evidence\n",
                "| Type | Evidence | Status |",
                "|---|---|---|",
            ]
            for item in resolved:
                label = _cell(item.label)
                if item.path:
                    label = f"[{label}](../evidence/{_evidence_page_id(item)}.md)"
                out.append(f"| {item.kind.replace('_', ' ')} | {label} | {item.status} |")
            out.append("")
        out += [
            "---",
            f"Source: `{document.source_path.relative_to(self.documentation_root.parent)}` · "
            f"Fingerprint: `{self.fingerprints[document.id][:12]}`",
            "",
        ]
        return out

    def _index(
        self, title: str, description: str, documents: Iterable[AuthoredDocument]
    ) -> str:
        out = [
            f"# {title}\n",
            description + "\n",
            "| Document | Purpose | Audience | Status | Owner |",
            "|---|---|---|---|---|",
        ]
        for document in documents:
            out.append(
                f"| [{_cell(document.title)}]({document.id}.md) | {_cell(document.summary)} | "
                f"{', '.join(AUDIENCE_LABELS[value] for value in document.audiences)} | "
                f"{document.status} | {', '.join(document.owners) or 'Unassigned'} |"
            )
        return "\n".join(out) + "\n"

    def _evidence_pages(
        self, documents: dict[str, AuthoredDocument]
    ) -> dict[str, str]:
        referenced_by: dict[str, list[str]] = {}
        evidence_by_id: dict[str, ResolvedEvidence] = {}
        for document_id, items in self.resolved.items():
            for item in items:
                if item.status != "resolved" or not item.path:
                    continue
                evidence_id = _evidence_page_id(item)
                evidence_by_id[evidence_id] = item
                referenced_by.setdefault(evidence_id, []).append(document_id)
        pages: dict[str, str] = {}
        rows: list[str] = []
        for evidence_id, item in sorted(evidence_by_id.items()):
            rows.append(
                f"| [{_cell(item.label)}]({evidence_id}.md) | "
                f"{item.kind.replace('_', ' ')} | `{item.path}` |"
            )
            links = [
                f"- [{documents[document_id].title}]"
                f"(../{_document_directory(documents[document_id])}/{document_id}.md)"
                for document_id in sorted(referenced_by[evidence_id])
            ]
            pages[f"evidence/{evidence_id}.md"] = "\n".join(
                [
                    f"# {item.label}\n",
                    "This page records the mirrored source used by curated documentation. "
                    "It intentionally does not copy configuration values or business data.\n",
                    f"- **Type:** {item.kind.replace('_', ' ')}",
                    f"- **Target:** `{item.target}`",
                    f"- **Mirrored source:** `{item.path}`",
                    f"- **Fingerprint:** `{item.fingerprint}`",
                    "",
                    "## Referenced by\n",
                    *links,
                    "",
                ]
            )
        pages["evidence/index.md"] = "\n".join(
            [
                "# Technical evidence\n",
                "Validated mirror references used by capability, process, and guide pages.\n",
                *(
                    [
                        "| Evidence | Type | Mirrored source |",
                        "|---|---|---|",
                        *rows,
                        "",
                    ]
                    if rows
                    else ["_No technical evidence has been linked yet._", ""]
                ),
            ]
        )
        return pages

    def _governance(self, documents: Iterable[AuthoredDocument]) -> str:
        out = [
            "# Documentation governance\n",
            "Review ownership and technical-evidence status for curated documentation.\n",
            "| Document | Status | Owner | Last review | Review interval | Evidence |",
            "|---|---|---|---|---|---|",
        ]
        for document in sorted(documents, key=lambda item: item.id):
            evidence = self.resolved.get(document.id, [])
            unresolved = sum(item.status != "resolved" for item in evidence)
            evidence_status = (
                f"{unresolved} unresolved" if unresolved else f"{len(evidence)} resolved"
            )
            out.append(
                f"| `{document.id}` | {document.status} | "
                f"{', '.join(document.owners) or 'Unassigned'} | "
                f"{document.reviewed_on.isoformat() if document.reviewed_on else 'Never'} | "
                f"{document.review_interval_days} days | {evidence_status} |"
            )
        if self.uncovered_tables:
            out += [
                "",
                "## Coverage gaps\n",
                "Customized tables not yet linked from a curated document:\n",
                *[f"- `{table}`" for table in self.uncovered_tables],
            ]
        return "\n".join(out) + "\n"

    def _review_findings(self, document: AuthoredDocument) -> None:
        if document.reviewed_on is None:
            self.findings.append(
                {
                    "severity": "error" if document.status == "approved" else "warning",
                    "document": document.id,
                    "code": "review_missing",
                    "message": "document has not been reviewed",
                }
            )
            return
        due = document.reviewed_on + timedelta(days=document.review_interval_days)
        if due < self.today:
            self.findings.append(
                {
                    "severity": "error" if document.status == "approved" else "warning",
                    "document": document.id,
                    "code": "review_overdue",
                    "message": f"review was due on {due.isoformat()}",
                }
            )


def _ordered(preferred: list[str], available: dict[str, Any]) -> list[str]:
    return [*preferred, *sorted(set(available) - set(preferred))]


def _scope(document: AuthoredDocument) -> list[str]:
    out: list[str] = []
    if document.in_scope:
        out += ["## In scope\n", *_bullets(document.in_scope)]
    if document.out_of_scope:
        out += ["## Out of scope\n", *_bullets(document.out_of_scope)]
    return out


def _bullets(values: Iterable[str]) -> list[str]:
    return [*[f"- {value}" for value in values], ""]


def _linked_bullets(
    identifiers: Iterable[str], documents: Mapping[str, AuthoredDocument], relative: str
) -> list[str]:
    return [
        *[
            f"- [{documents[identifier].title}]({relative}/{identifier}.md)"
            for identifier in identifiers
        ],
        "",
    ]


def _linked_ids(document: AuthoredDocument) -> set[str]:
    if isinstance(document, CapabilityDocument):
        return set(document.processes) | set(document.guides)
    if isinstance(document, ProcessDocument):
        return set(document.capabilities) | set(document.guides)
    if isinstance(document, GuideDocument):
        return set(document.processes)
    return set()


def _transition_target(transition: str | ProcessTransition) -> str:
    return transition.target if isinstance(transition, ProcessTransition) else transition


def _transition_label(transition: str | ProcessTransition) -> str | None:
    return transition.label if isinstance(transition, ProcessTransition) else None


def _render_transition(transition: str | ProcessTransition) -> str:
    target = _transition_target(transition)
    label = _transition_label(transition)
    return f"{label}: {target}" if label else target


def _document_directory(document: AuthoredDocument) -> str:
    if isinstance(document, CapabilityDocument):
        return "capabilities"
    if isinstance(document, ProcessDocument):
        return "processes"
    return "guides"


def _evidence_page_id(evidence: ResolvedEvidence) -> str:
    digest = hashlib.sha256(f"{evidence.kind}:{evidence.target}".encode()).hexdigest()[:10]
    label = re.sub(r"[^a-z0-9]+", "-", evidence.target.lower()).strip("-")[:50] or "evidence"
    return f"{evidence.kind}-{label}-{digest}"


def _node_id(value: str) -> str:
    return "n_" + re.sub(r"[^A-Za-z0-9_]", "_", value)


def _diagram_text(value: str) -> str:
    return value.replace('"', "'").replace("\n", " ")


def _cell(value: str) -> str:
    text = value.replace("|", "\\|").replace("\n", " ").strip()
    return text[:240] + ("…" if len(text) > 240 else "")


def _iso_date(value: date | None) -> str | None:
    return value.isoformat() if value is not None else None
