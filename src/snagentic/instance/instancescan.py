"""ServiceNow Instance Scan through the CI/CD API (guardrails phase 4).

After ``apply`` writes a change into agent update sets on a development instance,
``scan`` asks the platform itself for a second opinion:

* default: a **point scan** (``POST /api/sn_cicd/instance_scan/point_scan``) of every
  record captured in the agent update sets, which runs all active Instance Scan
  checks that apply to that record;
* with ``suite_sys_id``: a **suite scan** of the update sets
  (``POST /api/sn_cicd/instance_scan/suite_scan/{suite}/update_sets``).

Each run is followed through ``/api/sn_cicd/progress/{id}``; its ``scan_result`` is
found by ``progress_id`` and its ``scan_finding`` rows are joined with ``scan_check``
(name, category, priority, resolution). Only metadata is returned: finding details are
stripped of markup and record links (whose text can name people, e.g. dormant-user checks),
truncated, and at most ``FINDING_LIMIT`` findings are listed; the summary counts them all.
No record field values are read.
"""

from __future__ import annotations

import html
import re
import time
from collections import Counter
from collections.abc import Callable, Iterable, Mapping
from typing import Any

from snagentic.errors import ConfigurationError, PolicyDeniedError, ServiceNowError
from snagentic.instance.config import InstanceConfig
from snagentic.instance.ops import PlatformOperations
from snagentic.instance.tableapi import TableApiClient
from snagentic.instance.updatesets import OPEN_STATE
from snagentic.policy import PolicyEnforcer

SYS_ID = re.compile(r"^[0-9a-f]{32}$")
TABLE = re.compile(r"^[a-z0-9_]{1,80}$")
UPDATE_NAME = re.compile(r"^(?P<table>[a-z0-9_]+?)_(?P<sys_id>[0-9a-f]{32})$")
MAX_POINT_SCANS = 50
DETAIL_LIMIT = 500
FINDING_LIMIT = 200
LINK = re.compile(r"<a\b[^>]*>.*?</a>", re.IGNORECASE | re.DOTALL)
TAG = re.compile(r"<[^>]+>")
PRIORITIES = {"1": "critical", "2": "high", "3": "moderate", "4": "low", "5": "planning"}
CHECK_FIELDS = ["sys_id", "name", "category", "priority", "short_description",
                "resolution_details", "documentation_url"]
FINDING_FIELDS = ["sys_id", "check", "source", "source_table", "finding_details", "count",
                  "muted"]


def parse_target(value: str) -> tuple[str, str]:
    table, _, sys_id = value.partition(":")
    if not TABLE.fullmatch(table) or not SYS_ID.fullmatch(sys_id):
        raise ValueError(f"scan target must be <table>:<sys_id>, got {value!r}")
    return table, sys_id


class InstanceScanner:
    def __init__(
        self,
        config: InstanceConfig,
        client: TableApiClient,
        *,
        policy: PolicyEnforcer | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.config = config
        self.client = client
        self.policy = policy or PolicyEnforcer()
        self.operations = PlatformOperations(config, client, policy=self.policy, sleep=sleep)

    # ------------------------------------------------------------------ targets
    def agent_update_sets(self, label: str, *, include_complete: bool = True
                          ) -> list[dict[str, str]]:
        from snagentic.instance.changes import validate_label

        validate_label(label)
        prefix = f"snagentic: {label} ["
        states = f"state={OPEN_STATE}" + ("^ORstate=complete" if include_complete else "")
        rows = self.client.query(
            "sys_update_set", query=f"nameSTARTSWITH{prefix}^{states}",
            fields=["sys_id", "name", "state"],
        ) or []
        return [{key: str(row.get(key, "")) for key in ("sys_id", "name", "state")}
                for row in rows if str(row.get("name", "")).startswith(prefix)]

    def update_set_targets(self, update_set_ids: Iterable[str]) -> list[tuple[str, str]]:
        targets: dict[tuple[str, str], None] = {}
        for update_set_id in update_set_ids:
            if not SYS_ID.fullmatch(update_set_id):
                raise ValueError(f"invalid update set sys_id: {update_set_id}")
            for entry in self.client.iterate(
                "sys_update_xml", query=f"update_set={update_set_id}^action!=DELETE^ORDERBYname",
                fields=["name", "action"],
            ):
                match = UPDATE_NAME.fullmatch(str(entry.get("name", "")))
                if match:
                    targets[(match["table"], match["sys_id"])] = None
        return list(targets)

    # -------------------------------------------------------------------- scans
    def scan(
        self,
        *,
        confirm: bool,
        label: str | None = None,
        update_set_ids: Iterable[str] = (),
        targets: Iterable[tuple[str, str]] = (),
        suite_sys_id: str | None = None,
        timeout_seconds: float = 900,
    ) -> dict[str, Any]:
        self.policy.require_write_allowed(self.config.kind)
        if not confirm:
            raise PolicyDeniedError("Instance Scan runs on the instance and needs confirm=true")
        update_sets = list(dict.fromkeys(update_set_ids))
        if label:
            found = self.agent_update_sets(label)
            if not found and not update_sets and not list(targets):
                raise ServiceNowError(f"no snagentic update sets found for {label!r}")
            update_sets += [row["sys_id"] for row in found if row["sys_id"] not in update_sets]
        explicit = list(dict.fromkeys(targets))
        runs: list[dict[str, Any]] = []
        if suite_sys_id is not None:
            if not SYS_ID.fullmatch(suite_sys_id):
                raise ValueError("suite_sys_id must be a 32 character sys_id")
            if not update_sets:
                raise ConfigurationError("a suite scan needs a label or update set sys_ids")
            payload = self.client.call(
                "POST", f"api/sn_cicd/instance_scan/suite_scan/{suite_sys_id}/update_sets",
                json={"update_set_sys_ids": update_sets}, retries=0,
            ) or {}
            runs.append(self._follow(payload, {"suite": suite_sys_id,
                                               "update_sets": update_sets}, timeout_seconds))
        else:
            scan_targets = list(dict.fromkeys(explicit + self.update_set_targets(update_sets)))
            if not scan_targets:
                raise ConfigurationError("nothing to scan: no records in the given update sets")
            if len(scan_targets) > MAX_POINT_SCANS:
                raise ConfigurationError(
                    f"{len(scan_targets)} records exceed the point-scan limit of "
                    f"{MAX_POINT_SCANS}; use a suite scan (suite_sys_id) instead"
                )
            for table, sys_id in scan_targets:
                payload = self.client.call(
                    "POST", "api/sn_cicd/instance_scan/point_scan",
                    params={"target_table": table, "target_sys_id": sys_id}, retries=0,
                ) or {}
                runs.append(self._follow(payload, {"target": f"{table}:{sys_id}"},
                                         timeout_seconds))
        findings = [finding for run in runs for finding in run.pop("_findings")]
        return {
            "instance": self.config.name,
            "mode": "suite" if suite_sys_id else "point",
            "label": label,
            "update_sets": update_sets,
            "runs": runs,
            **_listing(findings),
        }

    def _follow(self, payload: Mapping[str, Any], subject: dict[str, Any],
                timeout_seconds: float) -> dict[str, Any]:
        result = payload.get("result") if isinstance(payload.get("result"), dict) else payload
        links = result.get("links") if isinstance(result, Mapping) else None
        progress = links.get("progress") if isinstance(links, Mapping) else None
        progress_id = str(progress.get("id")) if isinstance(progress, Mapping) else ""
        if not progress_id:
            raise ServiceNowError("Instance Scan did not return a progress id")
        self.operations.wait(progress_id, timeout_seconds=timeout_seconds)
        scan_result = self._result_for(progress_id)
        findings = self.findings(scan_result["sys_id"]) if scan_result else []
        return {**subject, "progress_id": progress_id,
                "result": scan_result, "findings": len(findings), "_findings": findings}

    def _result_for(self, progress_id: str) -> dict[str, str] | None:
        rows = self.client.query(
            "scan_result", query=f"progress_id={progress_id}",
            fields=["sys_id", "number", "state", "finding_count"], limit=1,
        ) or []
        return {key: str(value) for key, value in rows[0].items()} if rows else None

    # ------------------------------------------------------------------ results
    def findings(self, result_sys_id: str) -> list[dict[str, Any]]:
        if not SYS_ID.fullmatch(result_sys_id):
            raise ValueError("scan result must be a 32 character sys_id")
        rows = list(self.client.iterate("scan_finding", query=f"result={result_sys_id}",
                                        fields=FINDING_FIELDS))
        check_ids = sorted({str(row.get("check")) for row in rows if row.get("check")})
        checks: dict[str, dict[str, Any]] = {}
        for start in range(0, len(check_ids), 50):
            chunk = ",".join(check_ids[start:start + 50])
            for check in self.client.query("scan_check", query=f"sys_idIN{chunk}",
                                           fields=CHECK_FIELDS, limit=50) or []:
                checks[str(check["sys_id"])] = check
        findings = []
        for row in rows:
            check = checks.get(str(row.get("check")), {})
            priority = str(check.get("priority", ""))
            findings.append({
                "check": str(check.get("name") or row.get("check") or ""),
                "category": str(check.get("category") or ""),
                "priority": PRIORITIES.get(priority, priority or "unknown"),
                "record": f"{row.get('source_table', '')}:{row.get('source', '')}",
                "details": clean_details(row.get("finding_details")),
                "count": int(str(row.get("count") or "1") or 1),
                "muted": str(row.get("muted")) == "true",
                "resolution": clean_details(check.get("resolution_details")),
                "documentation": str(check.get("documentation_url") or ""),
                "result": result_sys_id,
            })
        rank = {name: index for index, name in enumerate(PRIORITIES.values())}
        findings.sort(key=lambda f: (rank.get(str(f["priority"]), 9), str(f["check"]),
                                     str(f["record"])))
        return findings

    def results(self, *, result_sys_ids: Iterable[str] = (),
                progress_id: str | None = None) -> dict[str, Any]:
        """Read-only: findings of existing scan results (any instance kind)."""

        selected: list[dict[str, str]] = []
        for result_sys_id in dict.fromkeys(result_sys_ids):
            if not SYS_ID.fullmatch(result_sys_id):
                raise ValueError("result must be a 32 character sys_id")
            selected.append({"sys_id": result_sys_id})
        if progress_id:
            if not SYS_ID.fullmatch(progress_id):
                raise ValueError("progress_id must be a 32 character sys_id")
            found = self._result_for(progress_id)
            if found is None:
                raise ServiceNowError(f"no scan result for progress {progress_id}")
            selected.append(found)
        if not selected:
            raise ConfigurationError("give scan result sys_ids, a progress_id or a label")
        findings = [finding for row in selected for finding in self.findings(row["sys_id"])]
        return {"instance": self.config.name, "results": selected, **_listing(findings)}


def clean_details(value: Any) -> str:
    text = LINK.sub("[record]", str(value or ""))
    text = html.unescape(TAG.sub(" ", text))
    return " ".join(text.split())[:DETAIL_LIMIT]


def _listing(findings: list[dict[str, Any]]) -> dict[str, Any]:
    listing: dict[str, Any] = {"summary": summarize(findings),
                               "findings": findings[:FINDING_LIMIT]}
    if len(findings) > FINDING_LIMIT:
        listing["findings_truncated"] = {"listed": FINDING_LIMIT, "total": len(findings)}
    return listing


def summarize(findings: list[dict[str, Any]]) -> dict[str, Any]:
    active = [f for f in findings if not f["muted"]]
    return {
        "findings": len(active),
        "muted": len(findings) - len(active),
        "by_priority": dict(Counter(f["priority"] for f in active)),
        "by_category": dict(sorted(Counter(f["category"] or "uncategorised"
                                           for f in active).items())),
    }
