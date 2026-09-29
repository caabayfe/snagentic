"""Review engine: which scripts a record contains, rule dispatch and standards overrides."""

from __future__ import annotations

import fnmatch
from collections import Counter
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from snagentic.review.lexer import Script, tokenize

CATEGORIES = ("security", "performance", "upgradability", "manageability", "user_experience")
SEVERITIES = ("block", "warn", "info")
SEVERITY_ORDER = {name: index for index, name in enumerate(SEVERITIES)}
MAX_SCRIPT_BYTES = 512_000

# Script-bearing fields by table. The kind selects which rules apply:
#   server         server-side JavaScript (business rules, script includes, jobs, ...)
#   client         classic UI client-side JavaScript (g_form, GlideAjax, ...)
#   portal_client  Service Portal / AngularJS client controller
SCRIPT_FIELDS: dict[str, dict[str, str]] = {
    "sys_script": {"script": "server"},
    "sys_script_include": {"script": "server"},
    "sys_script_fix": {"script": "server"},
    "sysauto_script": {"script": "server", "condition": "server"},
    "sys_ws_operation": {"operation_script": "server"},
    "sys_processor": {"script": "server"},
    "sys_security_acl": {"script": "server"},
    "sys_transform_map": {"script": "server"},
    "sys_transform_script": {"script": "server"},
    "sys_transform_entry": {"source_script": "server"},
    "sysevent_script_action": {"script": "server"},
    "sysevent_email_action": {"advanced_condition": "server"},
    "sys_script_email": {"script": "server"},
    "sys_ui_action": {"script": "server"},
    "sys_ui_page": {"processing_script": "server", "client_script": "client"},
    "sys_script_client": {"script": "client"},
    "catalog_script_client": {"script": "client"},
    "sys_ui_policy": {"script_true": "client", "script_false": "client"},
    "catalog_ui_policy": {"script_true": "client", "script_false": "client"},
    "sys_ui_script": {"script": "client"},
    "sp_widget": {"script": "server", "client_script": "portal_client",
                  "link": "portal_client"},
    "sp_angular_provider": {"script": "portal_client"},
}
DESCRIBED_TABLES = frozenset({
    "sys_script", "sys_script_include", "sys_script_client", "catalog_script_client",
    "sys_ui_policy", "sp_widget", "sys_ws_operation", "sys_script_fix",
})


@dataclass(frozen=True)
class ReviewTarget:
    """One record to review.

    ``previous`` holds the mirrored values for updates: only findings that the change
    introduces are reported. ``customized`` is ``None`` when unknown.
    """

    path: str
    table: str
    scope: str
    operation: str  # create | update | existing
    values: Mapping[str, str]
    previous: Mapping[str, str] | None = None
    customized: bool | None = None


@dataclass(frozen=True)
class Hit:
    line: int | None
    message: str
    evidence: str = ""


@dataclass
class ScriptContext:
    target: ReviewTarget
    field: str
    kind: str
    source: str
    script: Script

    @property
    def table(self) -> str:
        return self.target.table

    @property
    def values(self) -> Mapping[str, str]:
        return self.target.values

    @property
    def scoped(self) -> bool:
        return self.target.scope != "global"

    def line_text(self, line: int | None) -> str:
        if not line:
            return ""
        lines = self.source.splitlines()
        return lines[line - 1].strip()[:160] if 0 < line <= len(lines) else ""


ScriptCheck = Callable[[ScriptContext], Iterable[Hit]]
RecordCheck = Callable[[ReviewTarget], Iterable[Hit]]


@dataclass(frozen=True)
class Rule:
    id: str
    category: str
    severity: str
    title: str
    rationale: str
    fix: str
    kinds: frozenset[str] = frozenset()
    tables: frozenset[str] = frozenset()
    script_check: ScriptCheck | None = None
    record_check: RecordCheck | None = None
    redact_evidence: bool = False

    def describe(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "category": self.category,
            "severity": self.severity,
            "title": self.title,
            "rationale": self.rationale,
            "fix": self.fix,
            "script_kinds": sorted(self.kinds),
            "tables": sorted(self.tables),
        }


@dataclass(frozen=True)
class Finding:
    rule_id: str
    category: str
    severity: str
    title: str
    path: str
    table: str
    field: str | None
    line: int | None
    message: str
    fix: str
    evidence: str = ""

    def key(self) -> tuple[str, str | None, str]:
        return (self.rule_id, self.field, self.evidence or self.message)

    def as_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "rule": self.rule_id,
            "severity": self.severity,
            "category": self.category,
            "title": self.title,
            "path": self.path,
            "table": self.table,
            "field": self.field,
            "line": self.line,
            "message": self.message,
            "fix": self.fix,
        }
        if self.evidence:
            result["evidence"] = self.evidence
        return result


@dataclass(frozen=True)
class GateSettings:
    """``gate:`` block of ``standards.yaml`` (see ``snagentic.review.gate``)."""

    enforce: bool = True
    require_review: bool = False

    @classmethod
    def parse(cls, raw: Any, source: Path) -> GateSettings:
        if raw is None:
            return cls()
        if not isinstance(raw, dict) or set(raw) - {"enforce", "require_review"}:
            raise ValueError(f"{source}: 'gate' accepts only 'enforce' and 'require_review'")
        values = {key: raw[key] for key in ("enforce", "require_review") if key in raw}
        if not all(isinstance(value, bool) for value in values.values()):
            raise ValueError(f"{source}: gate settings must be true or false")
        return cls(**values)


@dataclass
class Standards:
    """Per-instance overrides from ``instances/<name>/standards.yaml``::

        rules:
          SN-MNT-003: {enabled: false}
          SN-PERF-003: {severity: info}
        exclude_paths:
          - "metadata/global/sys_script_include/legacy-*"
        gate: {enforce: true, require_review: false}
    """

    disabled: set[str] = field(default_factory=set)
    severity: dict[str, str] = field(default_factory=dict)
    exclude_paths: list[str] = field(default_factory=list)
    gate: GateSettings = field(default_factory=GateSettings)

    @classmethod
    def load(cls, path: Path, known: Iterable[str]) -> Standards:
        if not path.is_file():
            return cls()
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        if not isinstance(raw, dict):
            raise ValueError(f"{path}: expected a mapping")
        unknown_keys = set(raw) - {"rules", "exclude_paths", "gate"}
        if unknown_keys:
            raise ValueError(f"{path}: unknown keys {sorted(unknown_keys)}")
        known_ids = set(known)
        standards = cls()
        rules = raw.get("rules")
        rules = {} if rules is None else rules
        if not isinstance(rules, dict):
            raise ValueError(f"{path}: 'rules' must be a mapping")
        for rule_id, settings in rules.items():
            if rule_id not in known_ids:
                raise ValueError(f"{path}: unknown rule {rule_id}")
            if not isinstance(settings, dict) or set(settings) - {"enabled", "severity"}:
                raise ValueError(f"{path}: {rule_id} accepts only 'enabled' and 'severity'")
            if settings.get("enabled") is False:
                standards.disabled.add(rule_id)
            severity = settings.get("severity")
            if severity is not None:
                if severity not in SEVERITIES:
                    raise ValueError(f"{path}: {rule_id} severity must be one of {SEVERITIES}")
                standards.severity[rule_id] = severity
        excludes = raw.get("exclude_paths")
        excludes = [] if excludes is None else excludes
        if not isinstance(excludes, list) or not all(isinstance(p, str) for p in excludes):
            raise ValueError(f"{path}: 'exclude_paths' must be a list of glob strings")
        standards.exclude_paths = excludes
        standards.gate = GateSettings.parse(raw.get("gate"), path)
        return standards

    def excluded(self, path: str) -> bool:
        return any(fnmatch.fnmatch(path, pattern) or fnmatch.fnmatch(path, f"*/{pattern}")
                   for pattern in self.exclude_paths)


def script_fields(table: str, values: Mapping[str, str]) -> dict[str, str]:
    fields = dict(SCRIPT_FIELDS.get(table, {}))
    if table == "sys_ui_action" and values.get("client") == "true":
        # Client UI actions run "onclick" in the browser; the same script often also
        # carries a server branch guarded by ``typeof window == 'undefined'``.
        script = values.get("script", "")
        fields["script"] = "server" if "typeof window" in script else "client"
    if table == "sys_script" and values.get("advanced") == "false":
        fields.pop("script", None)
    return fields


class Reviewer:
    def __init__(self, rules: Iterable[Rule], standards: Standards | None = None) -> None:
        self.rules = list(rules)
        self.standards = standards or Standards()

    def active_rules(self) -> list[Rule]:
        return [rule for rule in self.rules if rule.id not in self.standards.disabled]

    def _severity(self, rule: Rule) -> str:
        return self.standards.severity.get(rule.id, rule.severity)

    def _findings(self, target: ReviewTarget, values: Mapping[str, str], *,
                  record_rules: bool = True) -> list[Finding]:
        view = ReviewTarget(target.path, target.table, target.scope, target.operation, values,
                            target.previous, target.customized)
        rules = self.active_rules()
        findings: list[Finding] = []
        for rule in rules:
            applies = not rule.tables or target.table in rule.tables
            if record_rules and rule.record_check and applies:
                for hit in rule.record_check(view):
                    findings.append(self._finding(rule, view, None, hit))
        for field_name, kind in sorted(script_fields(target.table, values).items()):
            source = values.get(field_name) or ""
            if not source.strip() or len(source) > MAX_SCRIPT_BYTES:
                continue
            context = ScriptContext(view, field_name, kind, source, tokenize(source))
            for rule in rules:
                if rule.script_check is None:
                    continue
                if rule.kinds and kind not in rule.kinds:
                    continue
                if rule.tables and target.table not in rule.tables:
                    continue
                seen_lines: set[int | None] = set()
                for hit in rule.script_check(context):
                    if hit.line in seen_lines:
                        continue
                    seen_lines.add(hit.line)
                    evidence = hit.evidence or ("" if rule.redact_evidence
                                                else context.line_text(hit.line))
                    findings.append(self._finding(rule, view, field_name,
                                                  Hit(hit.line, hit.message, evidence)))
        return findings

    def _finding(self, rule: Rule, target: ReviewTarget, field_name: str | None,
                 hit: Hit) -> Finding:
        return Finding(rule.id, rule.category, self._severity(rule), rule.title, target.path,
                       target.table, field_name, hit.line, hit.message, rule.fix, hit.evidence)

    def review(self, target: ReviewTarget) -> list[Finding]:
        if self.standards.excluded(target.path):
            return []
        findings = self._findings(target, target.values)
        if target.previous is None:
            return findings
        # Report only what the change introduces: subtract findings already present in
        # the mirrored version (matched by rule, field and evidence, not line number).
        existing = Counter(finding.key() for finding in self._findings(target, target.previous,
                                                    record_rules=False))
        introduced: list[Finding] = []
        for finding in findings:
            key = finding.key()
            if existing[key]:
                existing[key] -= 1
            else:
                introduced.append(finding)
        return introduced

    def review_all(self, targets: Iterable[ReviewTarget]) -> list[Finding]:
        findings: list[Finding] = []
        for target in targets:
            findings.extend(self.review(target))
        findings.sort(key=lambda f: (SEVERITY_ORDER[f.severity], f.rule_id, f.path,
                                     f.field or "", f.line or 0))
        return findings


def summarize(findings: Iterable[Finding], *, reviewed: int,
              mode: str = "report-only") -> dict[str, Any]:
    items = list(findings)
    by_severity = Counter(f.severity for f in items)
    by_rule = Counter(f.rule_id for f in items)
    return {
        "mode": mode,
        "records_reviewed": reviewed,
        "findings": len(items),
        "by_severity": {name: by_severity[name] for name in SEVERITIES if by_severity[name]},
        "by_rule": dict(sorted(by_rule.items())),
    }
