"""Authored capability, process and user-guide documentation.

Files under ``instances/<name>/documentation`` are durable source. Generated MkDocs
pages under ``docs/pages`` may be replaced on every build.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from datetime import date
from pathlib import Path
from typing import Any, Literal, Self

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from snagentic.errors import ConfigurationError
from snagentic.instance.records import atomic_write, safe_component

DOCUMENT_ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,79}$")
NARRATIVE = re.compile(
    r'<!-- snagentic:narrative id="([A-Za-z0-9_.\-]+)" -->\n'
    r"(.*?)<!-- /snagentic:narrative -->",
    re.DOTALL,
)
PLACEHOLDER = (
    "_Not documented yet. Describe the purpose and behaviour here; this block is kept "
    "when documentation is regenerated._\n"
)
Audience = Literal["end_users", "process_owners", "admins", "developers"]
Status = Literal["draft", "review", "approved", "deprecated"]
EvidenceKind = Literal[
    "table",
    "artifact",
    "flow",
    "catalog_item",
    "role",
    "event",
    "property",
    "script_include",
]
DocumentKind = Literal["capability", "process", "guide"]

_LOADER: Any = getattr(yaml, "CSafeLoader", yaml.SafeLoader)
_DUMPER: Any = getattr(yaml, "CSafeDumper", yaml.SafeDumper)


class EvidenceReference(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    kind: EvidenceKind
    target: str = Field(min_length=1, max_length=500)
    label: str | None = Field(default=None, max_length=160)


class ProcessTransition(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    target: str = Field(pattern=DOCUMENT_ID.pattern)
    label: str | None = Field(default=None, max_length=160)


class AuthoredDocument(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    id: str = Field(pattern=DOCUMENT_ID.pattern)
    title: str = Field(min_length=1, max_length=160)
    summary: str = Field(min_length=1, max_length=500)
    status: Status = "draft"
    owners: list[str] = Field(default_factory=list)
    audiences: list[Audience] = Field(min_length=1)
    benefits: list[str] = Field(default_factory=list)
    in_scope: list[str] = Field(default_factory=list)
    out_of_scope: list[str] = Field(default_factory=list)
    evidence: list[EvidenceReference] = Field(default_factory=list)
    reviewed_on: date | None = None
    review_interval_days: int = Field(default=180, ge=1, le=3650)
    body: str = Field(default="", exclude=True)
    source_path: Path = Field(exclude=True)

    @field_validator("id")
    @classmethod
    def reject_reserved_id(cls, value: str) -> str:
        if value == "index":
            raise ValueError("document id 'index' is reserved")
        return value


class CapabilityDocument(AuthoredDocument):
    processes: list[str] = Field(default_factory=list)
    guides: list[str] = Field(default_factory=list)
    entry_points: list[str] = Field(default_factory=list)


class ProcessStep(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    id: str = Field(pattern=DOCUMENT_ID.pattern)
    title: str = Field(min_length=1, max_length=160)
    actor: str = Field(min_length=1, max_length=160)
    action: str = Field(min_length=1, max_length=1000)
    system_behavior: str | None = Field(default=None, max_length=1000)
    next: list[str | ProcessTransition] = Field(default_factory=list)
    evidence: list[EvidenceReference] = Field(default_factory=list)


class LifecycleState(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    id: str = Field(pattern=DOCUMENT_ID.pattern)
    title: str = Field(min_length=1, max_length=160)
    next: list[str] = Field(default_factory=list)


class ProcessInteraction(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    source: str = Field(min_length=1, max_length=160)
    target: str = Field(min_length=1, max_length=160)
    message: str = Field(min_length=1, max_length=500)


class ProcessDocument(AuthoredDocument):
    capabilities: list[str] = Field(default_factory=list)
    trigger: str = Field(min_length=1, max_length=500)
    actors: list[str] = Field(min_length=1)
    preconditions: list[str] = Field(default_factory=list)
    inputs: list[str] = Field(default_factory=list)
    outputs: list[str] = Field(default_factory=list)
    guides: list[str] = Field(default_factory=list)
    steps: list[ProcessStep] = Field(min_length=1)
    states: list[LifecycleState] = Field(default_factory=list)
    interactions: list[ProcessInteraction] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_graph(self) -> Self:
        step_ids = [step.id for step in self.steps]
        if len(step_ids) != len(set(step_ids)):
            raise ValueError("process step ids must be unique")
        known = set(step_ids)
        missing = sorted(
            {
                transition.target
                if isinstance(transition, ProcessTransition)
                else transition
                for step in self.steps
                for transition in step.next
            }
            - known
        )
        if missing:
            raise ValueError(f"process steps reference missing targets: {', '.join(missing)}")
        reachable: set[str] = set()
        pending = [self.steps[0].id]
        by_id = {step.id: step for step in self.steps}
        while pending:
            current = pending.pop()
            if current in reachable:
                continue
            reachable.add(current)
            pending.extend(
                transition.target
                if isinstance(transition, ProcessTransition)
                else transition
                for transition in by_id[current].next
            )
        unreachable = sorted(known - reachable)
        if unreachable:
            raise ValueError(
                "process steps are unreachable from the first step: " + ", ".join(unreachable)
            )
        state_ids = [state.id for state in self.states]
        if len(state_ids) != len(set(state_ids)):
            raise ValueError("lifecycle state ids must be unique")
        missing_states = sorted(
            {target for state in self.states for target in state.next} - set(state_ids)
        )
        if missing_states:
            raise ValueError(
                "lifecycle states reference missing targets: " + ", ".join(missing_states)
            )
        return self


class GuideStep(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    title: str = Field(min_length=1, max_length=160)
    instruction: str = Field(min_length=1, max_length=2000)
    expected_result: str | None = Field(default=None, max_length=1000)


class GuideDocument(AuthoredDocument):
    role: str = Field(min_length=1, max_length=160)
    goal: str = Field(min_length=1, max_length=500)
    processes: list[str] = Field(default_factory=list)
    process_steps: list[str] = Field(default_factory=list)
    prerequisites: list[str] = Field(default_factory=list)
    steps: list[GuideStep] = Field(min_length=1)
    troubleshooting: list[str] = Field(default_factory=list)
    escalation: str | None = Field(default=None, max_length=1000)


class DocumentationManifest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    site_name: str | None = Field(default=None, max_length=160)
    introduction: str = ""
    default_owners: list[str] = Field(default_factory=list)
    capabilities: list[str] = Field(default_factory=list)
    processes: list[str] = Field(default_factory=list)
    guides: list[str] = Field(default_factory=list)


class DocumentationSource(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    manifest: DocumentationManifest
    capabilities: dict[str, CapabilityDocument]
    processes: dict[str, ProcessDocument]
    guides: dict[str, GuideDocument]
    legacy_narratives: dict[str, str]

    @property
    def empty(self) -> bool:
        return not (self.capabilities or self.processes or self.guides)

    def documents(self) -> list[AuthoredDocument]:
        return [
            *self.capabilities.values(),
            *self.processes.values(),
            *self.guides.values(),
        ]


def narrative(block_id: str, existing: Mapping[str, str]) -> str:
    body = existing.get(block_id, PLACEHOLDER)
    return (
        f'<!-- snagentic:narrative id="{block_id}" -->\n'
        f"{body}<!-- /snagentic:narrative -->\n"
    )


def read_narratives(pages: Path) -> dict[str, str]:
    blocks: dict[str, str] = {}
    if not pages.is_dir():
        return blocks
    for page in pages.rglob("*.md"):
        for block_id, body in NARRATIVE.findall(page.read_text(encoding="utf-8")):
            blocks[block_id] = body
    return blocks


class DocumentationSourceRepository:
    def __init__(self, root: Path) -> None:
        self.root = root

    def load(self) -> DocumentationSource:
        manifest = self._load_manifest()
        capabilities = self._load_documents("capabilities", CapabilityDocument)
        processes = self._load_documents("processes", ProcessDocument)
        guides = self._load_documents("guides", GuideDocument)
        source = DocumentationSource(
            manifest=manifest,
            capabilities=self._apply_default_owners(capabilities, manifest),
            processes=self._apply_default_owners(processes, manifest),
            guides=self._apply_default_owners(guides, manifest),
            legacy_narratives=self._load_legacy_narratives(),
        )
        self._validate_links(source)
        return source

    def scaffold(self, kind: DocumentKind, document_id: str) -> Path:
        safe_component(document_id)
        if not DOCUMENT_ID.fullmatch(document_id) or document_id == "index":
            raise ConfigurationError(
                "documentation id must use lowercase letters, digits and hyphens "
                "and cannot be 'index'"
            )
        plural = {"capability": "capabilities", "process": "processes", "guide": "guides"}[kind]
        target = self.root / plural / f"{document_id}.md"
        if target.exists():
            raise ConfigurationError(f"documentation source already exists: {target}")
        atomic_write(target, self._template(kind, document_id))
        return target

    def migrate_narratives(self, pages: Path) -> dict[str, Any]:
        found = read_narratives(pages)
        existing = self._load_legacy_narratives()
        merged = {**existing, **found}
        target = self.root / "legacy-narratives.yaml"
        atomic_write(target, yaml.dump(merged, Dumper=_DUMPER, sort_keys=True))
        return {
            "path": target.as_posix(),
            "narratives": len(merged),
            "added": len(set(merged) - set(existing)),
        }

    def _load_manifest(self) -> DocumentationManifest:
        path = self.root / "manifest.yaml"
        if not path.is_file():
            return DocumentationManifest()
        data = yaml.load(  # noqa: S506 - _LOADER is CSafeLoader or SafeLoader
            path.read_text(encoding="utf-8"), Loader=_LOADER
        ) or {}
        if not isinstance(data, dict):
            raise ConfigurationError(f"{path}: manifest must be a mapping")
        try:
            return DocumentationManifest.model_validate(data)
        except ValueError as exc:
            raise ConfigurationError(f"{path}: {exc}") from exc

    def _load_documents(
        self,
        directory: str,
        model: type[CapabilityDocument] | type[ProcessDocument] | type[GuideDocument],
    ) -> dict[str, Any]:
        base = self.root / directory
        loaded: dict[str, Any] = {}
        if not base.is_dir():
            return loaded
        for path in sorted(base.glob("*.md")):
            front_matter, body = _read_markdown(path)
            try:
                document = model.model_validate(
                    {**front_matter, "body": body.strip(), "source_path": path}
                )
            except ValueError as exc:
                raise ConfigurationError(f"{path}: {exc}") from exc
            if path.stem != document.id:
                raise ConfigurationError(
                    f"{path}: file name must match document id {document.id!r}"
                )
            if document.id in loaded:
                raise ConfigurationError(f"duplicate documentation id: {document.id}")
            loaded[document.id] = document
        return loaded

    def _load_legacy_narratives(self) -> dict[str, str]:
        path = self.root / "legacy-narratives.yaml"
        if not path.is_file():
            return {}
        data = yaml.load(  # noqa: S506 - _LOADER is CSafeLoader or SafeLoader
            path.read_text(encoding="utf-8"), Loader=_LOADER
        ) or {}
        if not isinstance(data, dict) or not all(
            isinstance(key, str) and isinstance(value, str) for key, value in data.items()
        ):
            raise ConfigurationError(f"{path}: expected a string-to-string mapping")
        return data

    def _validate_links(self, source: DocumentationSource) -> None:
        errors: list[str] = []
        all_ids = [
            *source.capabilities,
            *source.processes,
            *source.guides,
        ]
        duplicate_ids = sorted(
            {identifier for identifier in all_ids if all_ids.count(identifier) > 1}
        )
        errors += [
            f"{self.root}: document id {identifier!r} is used by more than one document type"
            for identifier in duplicate_ids
        ]
        for capability in source.capabilities.values():
            errors += _missing_links(
                capability.source_path, "process", capability.processes, source.processes
            )
            errors += _missing_links(
                capability.source_path, "guide", capability.guides, source.guides
            )
        for process in source.processes.values():
            errors += _missing_links(
                process.source_path, "capability", process.capabilities, source.capabilities
            )
            errors += _missing_links(process.source_path, "guide", process.guides, source.guides)
        for guide in source.guides.values():
            errors += _missing_links(
                guide.source_path, "process", guide.processes, source.processes
            )
            for reference in guide.process_steps:
                process_id, separator, step_id = reference.partition(":")
                target_process = source.processes.get(process_id)
                if (
                    not separator
                    or target_process is None
                    or step_id not in {s.id for s in target_process.steps}
                ):
                    errors.append(
                        f"{guide.source_path}: unresolved process step {reference!r}"
                    )
                elif process_id not in guide.processes:
                    errors.append(
                        f"{guide.source_path}: process step {reference!r} requires "
                        f"{process_id!r} in processes"
                    )
        errors += _ordered_ids(
            self.root / "manifest.yaml",
            "capability",
            source.manifest.capabilities,
            source.capabilities,
        )
        errors += _ordered_ids(
            self.root / "manifest.yaml", "process", source.manifest.processes, source.processes
        )
        errors += _ordered_ids(
            self.root / "manifest.yaml", "guide", source.manifest.guides, source.guides
        )
        if errors:
            raise ConfigurationError("\n".join(errors))

    def _apply_default_owners(
        self,
        documents: dict[str, Any],
        manifest: DocumentationManifest,
    ) -> dict[str, Any]:
        if not manifest.default_owners:
            return documents
        return {
            identifier: (
                document
                if document.owners
                else document.model_copy(update={"owners": list(manifest.default_owners)})
            )
            for identifier, document in documents.items()
        }

    def _template(self, kind: DocumentKind, document_id: str) -> str:
        common: dict[str, Any] = {
            "id": document_id,
            "title": document_id.replace("-", " ").title(),
            "summary": "Describe the outcome this documentation supports.",
            "status": "draft",
            "owners": [],
            "audiences": ["process_owners"],
            "benefits": [],
            "in_scope": [],
            "out_of_scope": [],
            "evidence": [],
            "reviewed_on": None,
            "review_interval_days": 180,
        }
        if kind == "capability":
            common.update({"processes": [], "guides": [], "entry_points": []})
            body = "## Description\n\nExplain the business capability and its boundaries.\n"
        elif kind == "process":
            common.update(
                {
                    "capabilities": [],
                    "trigger": "Describe what starts the process.",
                    "actors": ["Process owner"],
                    "preconditions": [],
                    "inputs": [],
                    "outputs": [],
                    "guides": [],
                    "steps": [
                        {
                            "id": "start",
                            "title": "Start",
                            "actor": "Process owner",
                            "action": "Describe the first action.",
                            "system_behavior": None,
                            "next": [],
                            "evidence": [],
                        }
                    ],
                    "states": [],
                    "interactions": [],
                }
            )
            body = "## Process notes\n\nDocument decisions, exceptions, and controls.\n"
        else:
            common.update(
                {
                    "role": "User role",
                    "goal": "Describe the user outcome.",
                    "processes": [],
                    "process_steps": [],
                    "prerequisites": [],
                    "steps": [
                        {
                            "title": "Complete the task",
                            "instruction": "Describe the user action.",
                            "expected_result": "Describe what the user should see.",
                        }
                    ],
                    "troubleshooting": [],
                    "escalation": None,
                }
            )
            common["audiences"] = ["end_users"]
            body = "## Additional guidance\n\nAdd context that does not fit the structured steps.\n"
        front_matter = yaml.dump(common, Dumper=_DUMPER, sort_keys=False).rstrip()
        return f"---\n{front_matter}\n---\n\n{body}"


def _read_markdown(path: Path) -> tuple[dict[str, Any], str]:
    text = path.read_text(encoding="utf-8")
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        raise ConfigurationError(f"{path}: Markdown source must start with YAML front matter")
    try:
        end = next(index for index, line in enumerate(lines[1:], start=1) if line.strip() == "---")
    except StopIteration as exc:
        raise ConfigurationError(f"{path}: YAML front matter is not closed") from exc
    data = yaml.load(  # noqa: S506 - _LOADER is CSafeLoader or SafeLoader
        "\n".join(lines[1:end]), Loader=_LOADER
    ) or {}
    if not isinstance(data, dict):
        raise ConfigurationError(f"{path}: YAML front matter must be a mapping")
    return data, "\n".join(lines[end + 1 :]).strip() + "\n"


def _missing_links(
    path: Path, kind: str, identifiers: list[str], available: dict[str, Any]
) -> list[str]:
    return [
        f"{path}: unresolved {kind} id {identifier!r}"
        for identifier in identifiers
        if identifier not in available
    ]


def _ordered_ids(
    path: Path, kind: str, identifiers: list[str], available: dict[str, Any]
) -> list[str]:
    duplicates = sorted({item for item in identifiers if identifiers.count(item) > 1})
    errors = [f"{path}: duplicate {kind} id {item!r}" for item in duplicates]
    errors += _missing_links(path, kind, identifiers, available)
    return errors
