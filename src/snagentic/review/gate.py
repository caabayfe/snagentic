"""Enforcement for ServiceNow best-practice findings (guardrails phase 3).

``plan`` evaluates the gate and ``apply`` refuses a plan whose gate did not pass:

* every ``block`` finding the change introduces must be fixed or covered by an
  active waiver in ``instances/<name>/waivers.yaml``::

      waivers:
        - rule: SN-SEC-002
          path: metadata/global/sys_script_include/legacy-*   # instance relative glob
          reason: Vendor integration hardcodes its endpoint until INT-1234 ships.
          approver: jane.architect
          expires: 2026-12-31

* with ``gate: {require_review: true}`` in ``standards.yaml`` a review record for
  the exact ``plan_id`` (``instances/<name>/reviews/<plan_id>.yaml``) must approve it.

Waivers are specific (one known rule, one path glob), justified, attributed and
time boxed; expired or malformed waivers never waive anything.
"""

from __future__ import annotations

import datetime as dt
import fnmatch
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from snagentic.review.engine import Finding, GateSettings

WAIVERS_FILE = "waivers.yaml"
REVIEWS_DIR = "reviews"
MAX_WAIVER_DAYS = 366
PLAN_ID = re.compile(r"^[0-9a-f]{16}$")
REVIEWER = re.compile(r"^[A-Za-z0-9_.@ -]{1,80}$")
VERDICTS = ("approve", "reject")
WAIVER_KEYS = {"rule", "path", "reason", "approver", "expires"}


@dataclass(frozen=True)
class Waiver:
    rule: str
    path: str
    reason: str
    approver: str
    expires: dt.date

    def covers(self, finding: Finding, workspace_prefix: str) -> bool:
        if finding.rule_id != self.rule:
            return False
        path = finding.path
        prefix = workspace_prefix.rstrip("/") + "/"
        relative = path[len(prefix):] if path.startswith(prefix) else path
        return any(fnmatch.fnmatchcase(candidate, pattern)
                   for candidate in (relative, path)
                   for pattern in (self.path, self.path.rstrip("/") + "/*"))

    def as_dict(self) -> dict[str, Any]:
        return {"rule": self.rule, "path": self.path, "reason": self.reason,
                "approver": self.approver, "expires": self.expires.isoformat()}


@dataclass(frozen=True)
class Waivers:
    active: tuple[Waiver, ...] = ()
    expired: tuple[Waiver, ...] = ()


def load_waivers(workspace: Path, known_rules: Iterable[str], *,
                 today: dt.date | None = None) -> Waivers:
    source = workspace / WAIVERS_FILE
    if not source.is_file():
        return Waivers()
    today = today or dt.date.today()
    raw = yaml.safe_load(source.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict) or set(raw) - {"waivers"}:
        raise ValueError(f"{source}: expected a mapping with a 'waivers' list")
    entries = raw.get("waivers") or []
    if not isinstance(entries, list):
        raise ValueError(f"{source}: 'waivers' must be a list")
    known = set(known_rules)
    active: list[Waiver] = []
    expired: list[Waiver] = []
    for index, entry in enumerate(entries, 1):
        where = f"{source}: waiver {index}"
        if not isinstance(entry, dict) or set(entry) != WAIVER_KEYS:
            raise ValueError(f"{where} needs exactly {sorted(WAIVER_KEYS)}")
        rule = entry["rule"]
        if rule not in known:
            raise ValueError(f"{where}: unknown rule {rule!r}")
        path = entry["path"]
        if (not isinstance(path, str) or not path.strip() or path.strip() in {"*", "**"}
                or ".." in Path(path).parts or path.startswith("/")):
            raise ValueError(f"{where}: 'path' must be a specific relative record glob")
        reason = entry["reason"]
        if not isinstance(reason, str) or len(reason.strip()) < 10:
            raise ValueError(f"{where}: 'reason' must explain the exception (10+ characters)")
        approver = entry["approver"]
        if not isinstance(approver, str) or not REVIEWER.fullmatch(approver.strip()):
            raise ValueError(f"{where}: 'approver' must name the person who accepted the risk")
        expires = entry["expires"]
        if isinstance(expires, str):
            try:
                expires = dt.date.fromisoformat(expires)
            except ValueError as exc:
                raise ValueError(f"{where}: 'expires' must be a YYYY-MM-DD date") from exc
        if not isinstance(expires, dt.date) or isinstance(expires, dt.datetime):
            raise ValueError(f"{where}: 'expires' must be a YYYY-MM-DD date")
        if (expires - today).days > MAX_WAIVER_DAYS:
            raise ValueError(f"{where}: waivers may last at most {MAX_WAIVER_DAYS} days")
        waiver = Waiver(rule, path.strip(), reason.strip(), approver.strip(), expires)
        (active if expires >= today else expired).append(waiver)
    return Waivers(tuple(active), tuple(expired))


def review_path(workspace: Path, plan_id: str) -> Path:
    if not PLAN_ID.fullmatch(plan_id):
        raise ValueError("plan_id must be 16 lowercase hexadecimal characters")
    return workspace / REVIEWS_DIR / f"{plan_id}.yaml"


def load_review(workspace: Path, plan_id: str) -> dict[str, Any] | None:
    source = review_path(workspace, plan_id)
    if not source.is_file():
        return None
    raw = yaml.safe_load(source.read_text(encoding="utf-8"))
    if not isinstance(raw, dict) or raw.get("plan_id") != plan_id \
            or raw.get("verdict") not in VERDICTS:
        raise ValueError(f"{source}: malformed review record")
    return raw


def record_review(workspace: Path, *, plan_id: str, verdict: str, reviewer: str, notes: str,
                  gate: dict[str, Any], recorded_at: str) -> tuple[Path, dict[str, Any]]:
    if verdict not in VERDICTS:
        raise ValueError(f"verdict must be one of {VERDICTS}")
    if not REVIEWER.fullmatch(reviewer.strip()):
        raise ValueError("reviewer must be a plain name (letters, digits, . _ @ - space)")
    notes = notes.strip()
    if len(notes) < 10 or len(notes) > 4_000:
        raise ValueError("notes must summarise the review in 10 to 4000 characters")
    if verdict == "approve" and gate["blocking"]:
        raise ValueError("cannot approve a plan with unwaived blocking findings")
    record = {
        "plan_id": plan_id,
        "verdict": verdict,
        "reviewer": reviewer.strip(),
        "recorded_at": recorded_at,
        "notes": notes,
        "findings": gate["counts"],
        "waived": [item["waiver"] for item in gate["waived"]],
    }
    destination = review_path(workspace, plan_id)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(yaml.safe_dump(record, sort_keys=False, allow_unicode=True),
                           encoding="utf-8")
    return destination, record


def evaluate_gate(findings: Sequence[Finding], *, settings: GateSettings, waivers: Waivers,
                  workspace_prefix: str, plan_id: str | None,
                  review: dict[str, Any] | None) -> dict[str, Any]:
    blocking: list[dict[str, Any]] = []
    waived: list[dict[str, Any]] = []
    for finding in findings:
        if finding.severity != "block":
            continue
        waiver = next((w for w in waivers.active if w.covers(finding, workspace_prefix)), None)
        brief = {"rule": finding.rule_id, "path": finding.path, "field": finding.field,
                 "line": finding.line, "title": finding.title}
        if waiver is None:
            blocking.append(brief)
        else:
            waived.append({**brief, "waiver": waiver.as_dict()})
    reasons: list[str] = []
    if blocking:
        reasons.append(
            f"{len(blocking)} blocking finding(s): " + "; ".join(
                f"{item['rule']} {item['path']}" + (f":{item['line']}" if item["line"] else "")
                for item in blocking[:10]
            )
        )
    review_state = None
    if settings.require_review:
        if review is None:
            review_state = "missing"
            reasons.append(f"no review record for plan {plan_id}")
        else:
            review_state = review["verdict"]
            if review_state != "approve":
                reasons.append(f"review of plan {plan_id} was {review_state}")
    elif review is not None:
        review_state = review["verdict"]
    counts = {name: sum(1 for f in findings if f.severity == name)
              for name in ("block", "warn", "info")}
    return {
        "mode": "enforcing" if settings.enforce else "report-only",
        "passed": not reasons,
        "enforced": settings.enforce,
        "require_review": settings.require_review,
        "review": review_state,
        "counts": counts,
        "blocking": blocking,
        "waived": waived,
        "expired_waivers": [w.as_dict() for w in waivers.expired],
        "reasons": reasons,
    }


def denial_message(gate: dict[str, Any], waivers_file: str) -> str:
    steps = []
    if gate["blocking"]:
        steps.append("fix the findings (snagentic_instance_review lists rule, line and fix) or "
                     f"record an approved, time-boxed waiver in {waivers_file}")
    if gate["require_review"] and gate["review"] != "approve":
        steps.append("have the servicenow-reviewer agent review the plan and record an approving "
                     "verdict with snagentic_instance_review_record")
    return ("ServiceNow standards gate refused the plan: " + " | ".join(gate["reasons"])
            + ". Next: " + "; then ".join(steps))
