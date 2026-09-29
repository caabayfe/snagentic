"""Guardrails phases 3 and 4: the apply gate, waivers, review records, the Copilot
``preToolUse`` hook, and ServiceNow Instance Scan through the CI/CD API."""

from __future__ import annotations

import datetime as dt
import io
import json
from pathlib import Path
from typing import Any

import pytest
import yaml
from conftest import Harness

import snagentic.cli.instance as instance_cli
import snagentic.instance.instancescan as instancescan
from snagentic.cli.hook import pre_tool_use
from snagentic.cli.main import main
from snagentic.errors import ConfigurationError, PolicyDeniedError, ServiceNowError
from snagentic.instance.changes import ChangePlanner, UpdateSetWriter
from snagentic.instance.instancescan import InstanceScanner, clean_details, parse_target
from snagentic.instance.mirror import MirrorRepository
from snagentic.review.engine import Finding, GateSettings, Standards
from snagentic.review.gate import (
    Waiver,
    Waivers,
    denial_message,
    evaluate_gate,
    load_review,
    load_waivers,
    record_review,
)
from snagentic.review.rules import RULES

KNOWN = [rule.id for rule in RULES]
TODAY = dt.date(2026, 9, 20)


def _blocking_change(harness: Harness) -> Path:
    sys_id = harness.fake.insert(
        "sys_script", {"name": "Set state", "collection": "incident", "when": "before",
                       "advanced": "true", "script": "current.state = 2;"},
    )
    harness.sync().fetch()
    MirrorRepository(harness.paths, harness.config.mirror_branch).integrate(message="integrate")
    directory = harness.paths.metadata / "global" / "sys_script" / f"set-state--{sys_id}"
    (directory / "script.js").write_text("current.state = 3;\ncurrent.update();\n")
    return directory


def _waiver(**overrides: Any) -> dict[str, Any]:
    return {"rule": "SN-PERF-001", "path": "metadata/global/sys_script/set-state--*",
            "reason": "Legacy rule; replaced by a flow in CHG0001.", "approver": "jane.architect",
            "expires": (dt.date.today() + dt.timedelta(days=30)).isoformat(), **overrides}


def _write_waivers(harness: Harness, *waivers: dict[str, Any]) -> None:
    (harness.paths.workspace / "waivers.yaml").write_text(
        yaml.safe_dump({"waivers": list(waivers)}))


def _finding(rule: str = "SN-PERF-001", severity: str = "block",
             path: str = "instances/dev/metadata/global/sys_script/set-state--1") -> Finding:
    return Finding(rule, "performance", severity, "t", path, "sys_script", "script", 2, "m", "f")


# ----------------------------------------------------------------------------- gate


def test_gate_blocks_unwaived_block_findings_only() -> None:
    findings = [_finding(), _finding("SN-PERF-003", "warn")]
    gate = evaluate_gate(findings, settings=GateSettings(), waivers=Waivers(),
                         workspace_prefix="instances/dev", plan_id="0" * 16, review=None)
    assert gate["mode"] == "enforcing" and gate["passed"] is False
    assert [item["rule"] for item in gate["blocking"]] == ["SN-PERF-001"]
    assert gate["counts"] == {"block": 1, "warn": 1, "info": 0}
    assert "SN-PERF-001 instances/dev/metadata/global/sys_script/set-state--1:2" \
        in gate["reasons"][0]
    waiver = Waiver("SN-PERF-001", "metadata/global/sys_script/set-state--*", "reason text",
                    "jane", TODAY)
    waived = evaluate_gate(findings, settings=GateSettings(), waivers=Waivers((waiver,)),
                           workspace_prefix="instances/dev", plan_id="0" * 16, review=None)
    assert waived["passed"] is True and waived["waived"][0]["waiver"]["approver"] == "jane"
    other_rule = Waiver("SN-SEC-001", waiver.path, "reason text", "jane", TODAY)
    assert not other_rule.covers(_finding(), "instances/dev")
    folder = Waiver("SN-PERF-001", "metadata/global/sys_script", "reason text", "jane", TODAY)
    assert folder.covers(_finding(), "instances/dev")
    report_only = evaluate_gate(findings, settings=GateSettings(enforce=False),
                                waivers=Waivers(), workspace_prefix="instances/dev",
                                plan_id=None, review=None)
    assert report_only["mode"] == "report-only" and report_only["enforced"] is False


def test_gate_requires_an_approving_review_when_configured() -> None:
    settings = GateSettings(require_review=True)
    missing = evaluate_gate([], settings=settings, waivers=Waivers(),
                            workspace_prefix="instances/dev", plan_id="a" * 16, review=None)
    assert missing["passed"] is False and missing["review"] == "missing"
    rejected = evaluate_gate([], settings=settings, waivers=Waivers(),
                             workspace_prefix="instances/dev", plan_id="a" * 16,
                             review={"verdict": "reject"})
    assert rejected["passed"] is False and "reject" in rejected["reasons"][0]
    approved = evaluate_gate([], settings=settings, waivers=Waivers(),
                             workspace_prefix="instances/dev", plan_id="a" * 16,
                             review={"verdict": "approve"})
    assert approved["passed"] is True and approved["review"] == "approve"
    message = denial_message(missing, "instances/dev/waivers.yaml")
    assert "snagentic_instance_review_record" in message and "waiver" not in message
    both = evaluate_gate([_finding()], settings=settings, waivers=Waivers(),
                         workspace_prefix="instances/dev", plan_id="a" * 16, review=None)
    message = denial_message(both, "instances/dev/waivers.yaml")
    assert "waivers.yaml" in message and "; then have the servicenow-reviewer" in message


def test_waivers_are_strict_specific_and_time_boxed(tmp_path: Path) -> None:
    assert load_waivers(tmp_path, KNOWN) == Waivers()
    source = tmp_path / "waivers.yaml"
    good = {**_waiver(), "expires": "2026-12-31"}
    expired = {**_waiver(), "expires": "2026-01-01"}
    source.write_text(yaml.safe_dump({"waivers": [good, expired]}))
    waivers = load_waivers(tmp_path, KNOWN, today=TODAY)
    assert [w.expires.isoformat() for w in waivers.active] == ["2026-12-31"]
    assert [w.expires.isoformat() for w in waivers.expired] == ["2026-01-01"]
    for bad, message in [
        ({**good, "rule": "SN-XXX-999"}, "unknown rule"),
        ({**good, "path": "*"}, "specific"),
        ({**good, "path": "../other/metadata"}, "specific"),
        ({**good, "path": "/abs"}, "specific"),
        ({**good, "reason": "because"}, "reason"),
        ({**good, "approver": "a;b"}, "approver"),
        ({**good, "expires": "soon"}, "YYYY-MM-DD"),
        ({**good, "expires": 20261231}, "YYYY-MM-DD"),
        ({**good, "expires": "2028-01-01"}, "at most"),
        ({key: value for key, value in good.items() if key != "approver"}, "exactly"),
        ({**good, "scope": "all"}, "exactly"),
    ]:
        source.write_text(yaml.safe_dump({"waivers": [bad]}))
        with pytest.raises(ValueError, match=message):
            load_waivers(tmp_path, KNOWN, today=TODAY)
    for bad_file in ("- a\n", "waivers: {a: 1}\n", "other: []\n"):
        source.write_text(bad_file)
        with pytest.raises(ValueError):
            load_waivers(tmp_path, KNOWN, today=TODAY)


def test_standards_gate_settings_are_validated(tmp_path: Path) -> None:
    source = tmp_path / "standards.yaml"
    source.write_text(yaml.safe_dump({"gate": {"require_review": True}}))
    assert Standards.load(source, KNOWN).gate == GateSettings(enforce=True, require_review=True)
    assert Standards.load(tmp_path / "missing.yaml", KNOWN).gate == GateSettings()
    for bad in ({"gate": {"enforce": "no"}}, {"gate": {"strict": True}}, {"gate": []}):
        source.write_text(yaml.safe_dump(bad))
        with pytest.raises(ValueError, match="gate"):
            Standards.load(source, KNOWN)


def test_review_records_are_bound_to_the_plan(tmp_path: Path) -> None:
    passing = evaluate_gate([], settings=GateSettings(), waivers=Waivers(),
                            workspace_prefix="i", plan_id="b" * 16, review=None)
    path, record = record_review(tmp_path, plan_id="b" * 16, verdict="approve",
                                 reviewer="servicenow-reviewer agent",
                                 notes="Gate passed; no manual findings.", gate=passing,
                                 recorded_at="2026-09-20T10:00:00+00:00")
    assert path == tmp_path / "reviews" / f"{'b' * 16}.yaml"
    assert load_review(tmp_path, "b" * 16) == record
    assert load_review(tmp_path, "c" * 16) is None
    blocked = evaluate_gate([_finding()], settings=GateSettings(), waivers=Waivers(),
                            workspace_prefix="instances/dev", plan_id="b" * 16, review=None)
    with pytest.raises(ValueError, match="blocking"):
        record_review(tmp_path, plan_id="b" * 16, verdict="approve", reviewer="r",
                      notes="looks fine to me", gate=blocked, recorded_at="now")
    for kwargs, message in [
        ({"verdict": "maybe"}, "verdict"),
        ({"reviewer": "a;b"}, "reviewer"),
        ({"notes": "short"}, "notes"),
        ({"plan_id": "../../x"}, "plan_id"),
    ]:
        arguments: dict[str, Any] = {"plan_id": "b" * 16, "verdict": "reject", "reviewer": "r",
                                     "notes": "long enough notes", "gate": blocked,
                                     "recorded_at": "now", **kwargs}
        with pytest.raises(ValueError, match=message):
            record_review(tmp_path, **arguments)
    (tmp_path / "reviews" / f"{'d' * 16}.yaml").write_text("plan_id: other\n")
    with pytest.raises(ValueError, match="malformed"):
        load_review(tmp_path, "d" * 16)


# ------------------------------------------------------------------------ apply gate


def test_apply_refuses_blocking_findings_before_any_request(harness: Harness) -> None:
    _blocking_change(harness)
    plan = ChangePlanner(harness.paths, harness.config).plan()
    assert plan["gate"]["passed"] is False
    assert [item["rule"] for item in plan["gate"]["blocking"]] == ["SN-PERF-001"]
    writer = UpdateSetWriter(harness.paths, harness.config, harness.client)
    before = len(harness.fake.requests)
    with pytest.raises(PolicyDeniedError, match=r"SN-PERF-001.*instances/dev/waivers.yaml"):
        writer.apply(plan_id=plan["plan_id"], confirm=True)
    assert len(harness.fake.requests) == before
    _write_waivers(harness, _waiver())
    waived = ChangePlanner(harness.paths, harness.config).plan()
    assert waived["plan_id"] == plan["plan_id"] and waived["gate"]["passed"] is True
    assert waived["gate"]["waived"][0]["waiver"]["approver"] == "jane.architect"
    result = writer.apply(plan_id=plan["plan_id"], confirm=True)
    assert result["applied"] and len(harness.fake.requests) > before


def test_apply_honours_report_only_mode_and_expired_waivers(harness: Harness) -> None:
    _blocking_change(harness)
    _write_waivers(harness, _waiver(expires=(dt.date.today() - dt.timedelta(days=1))
                                    .isoformat()))
    plan = ChangePlanner(harness.paths, harness.config).plan()
    assert plan["gate"]["passed"] is False and len(plan["gate"]["expired_waivers"]) == 1
    (harness.paths.workspace / "standards.yaml").write_text(
        yaml.safe_dump({"gate": {"enforce": False}}))
    relaxed = ChangePlanner(harness.paths, harness.config).plan()
    assert relaxed["gate"]["mode"] == "report-only" and relaxed["gate"]["passed"] is False
    result = UpdateSetWriter(harness.paths, harness.config, harness.client).apply(
        plan_id=relaxed["plan_id"], confirm=True)
    assert result["applied"]


def cli(capsys: pytest.CaptureFixture[str], *args: str) -> dict[str, Any]:
    try:
        main(["--json", "instance", *args])
    except SystemExit as exc:
        out = json.loads(capsys.readouterr().err)
        out["exit"] = exc.code
        return out
    return json.loads(capsys.readouterr().out)


def test_review_cli_reports_gate_and_records_reviews(
    harness: Harness, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.chdir(harness.root)
    monkeypatch.setattr(instance_cli, "TRANSPORT", harness.fake.transport())
    directory = _blocking_change(harness)
    review = cli(capsys, "review")["result"]
    assert review["summary"]["mode"] == "enforcing" and review["gate"]["passed"] is False
    plan_id = review["plan_id"]
    denied = cli(capsys, "review-record", "--plan-id", plan_id, "--verdict", "approve",
                 "--reviewer", "agent", "--notes", "Looks good to me.")
    assert denied["exit"] == 2 and "blocking" in denied["error"]
    stale = cli(capsys, "review-record", "--plan-id", "f" * 16, "--verdict", "reject",
                "--reviewer", "agent", "--notes", "Stale plan id review.")
    assert stale["exit"] == 2 and "plan changed" in stale["error"]
    rejected = cli(capsys, "review-record", "--plan-id", plan_id, "--verdict", "reject",
                   "--reviewer", "servicenow-reviewer agent",
                   "--notes", "[SN-PERF-001] current.update() in a before rule.")["result"]
    assert rejected["record"] == f"instances/dev/reviews/{plan_id}.yaml"
    assert rejected["findings"] == {"block": 1, "warn": 0, "info": 0}
    applied = cli(capsys, "apply", "--plan-id", plan_id, "--confirm")
    assert applied["exit"] == 2 and "standards gate" in applied["error"]
    (directory / "script.js").write_text("current.state = 3;\n")
    (harness.paths.workspace / "standards.yaml").write_text(
        yaml.safe_dump({"gate": {"require_review": True}}))
    fixed = cli(capsys, "plan")["result"]
    assert fixed["gate"]["reasons"] == [f"no review record for plan {fixed['plan_id']}"]
    cli(capsys, "review-record", "--plan-id", fixed["plan_id"], "--verdict", "approve",
        "--reviewer", "servicenow-reviewer agent", "--notes", "Gate passes; no findings.")
    assert cli(capsys, "plan")["result"]["gate"]["passed"] is True
    assert cli(capsys, "apply", "--plan-id", fixed["plan_id"], "--confirm")["result"]["applied"]
    nothing = cli(capsys, "review-record", "--plan-id", fixed["plan_id"], "--verdict",
                  "approve", "--reviewer", "r", "--notes", "Nothing left to review.")
    assert nothing["exit"] == 2


# ----------------------------------------------------------------------------- hook


def _payload(root: Path, tool: str = "snagentic_instance_apply", **arguments: Any) -> str:
    return json.dumps({"sessionId": "s", "timestamp": 1, "cwd": str(root),
                       "toolName": tool, "toolArgs": arguments})


def test_pre_tool_use_hook_denies_blocked_or_stale_plans(harness: Harness) -> None:
    root = harness.root
    assert pre_tool_use(_payload(root, "bash", command="ls")) is None
    assert pre_tool_use("not json")["permissionDecision"] == "deny"  # type: ignore[index]
    assert pre_tool_use("[]")["permissionDecision"] == "deny"  # type: ignore[index]
    directory = _blocking_change(harness)
    plan_id = ChangePlanner(harness.paths, harness.config).plan()["plan_id"]
    denied = pre_tool_use(_payload(root / "instances", instance="dev", planId=plan_id,
                                   confirm=True))
    assert denied is not None and denied["permissionDecision"] == "deny"
    assert "SN-PERF-001" in denied["permissionDecisionReason"]
    stale = pre_tool_use(_payload(root, instance="dev", planId="0" * 16, confirm=True))
    assert stale is not None and "stale" in stale["permissionDecisionReason"]
    missing = pre_tool_use(_payload(root, instance="dev"))
    assert missing is not None and "planId" in missing["permissionDecisionReason"]
    nowhere = pre_tool_use(_payload(Path("/"), instance="dev", planId=plan_id))
    assert nowhere is not None and "instances/" in nowhere["permissionDecisionReason"]
    unknown = pre_tool_use(_payload(root, instance="nope", planId=plan_id))
    assert unknown is not None and "could not evaluate" in unknown["permissionDecisionReason"]
    (directory / "script.js").write_text("current.state = 3;\n")
    fixed = ChangePlanner(harness.paths, harness.config).plan()["plan_id"]
    as_string = json.dumps({"cwd": str(root), "tool_name": "snagentic_instance_apply",
                            "tool_input": json.dumps({"instance": "dev", "planId": fixed})})
    assert pre_tool_use(as_string) is None


def test_hook_command_prints_exactly_one_decision(
    harness: Harness, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    _blocking_change(harness)
    plan_id = ChangePlanner(harness.paths, harness.config).plan()["plan_id"]
    monkeypatch.setattr("sys.stdin", io.StringIO(_payload(harness.root, instance="dev",
                                                          planId=plan_id)))
    main(["copilot", "hook", "pre-tool-use"])
    decision = json.loads(capsys.readouterr().out)
    assert decision["permissionDecision"] == "deny"
    monkeypatch.setattr("sys.stdin", io.StringIO(_payload(harness.root, "view", path="x")))
    main(["copilot", "hook", "pre-tool-use"])
    assert capsys.readouterr().out == ""


# ---------------------------------------------------------------------- Instance Scan


def _applied(harness: Harness) -> tuple[str, str]:
    """Apply one clean change and return (label, record sys_id)."""

    sys_id = harness.fake.insert("sys_script_include", {"name": "Helper", "script": "var a;"})
    harness.sync().fetch()
    MirrorRepository(harness.paths, harness.config.mirror_branch).integrate(message="integrate")
    directory = harness.paths.metadata / "global" / "sys_script_include" / f"helper--{sys_id}"
    (directory / "script.js").write_text("var a = 1;\n")
    plan = ChangePlanner(harness.paths, harness.config).plan()
    UpdateSetWriter(harness.paths, harness.config, harness.client).apply(
        plan_id=plan["plan_id"], confirm=True, label="feature-1")
    return "feature-1", sys_id


def test_point_scan_of_agent_update_sets(harness: Harness) -> None:
    label, sys_id = _applied(harness)
    check = harness.fake.insert("scan_check", {
        "name": "Avoid hard-coded values", "category": "manageability", "priority": "2",
        "resolution_details": "Use a system property.", "documentation_url": "https://x",
    }, track=False)
    harness.fake.scan_findings[sys_id] = [check]
    scanner = InstanceScanner(harness.config, harness.client, sleep=lambda _: None)
    with pytest.raises(PolicyDeniedError, match="confirm"):
        scanner.scan(confirm=False, label=label)
    report = scanner.scan(confirm=True, label=label)
    assert report["mode"] == "point" and len(report["update_sets"]) == 1
    assert [run["target"] for run in report["runs"]] == [f"sys_script_include:{sys_id}"]
    assert report["summary"] == {"findings": 1, "muted": 0, "by_priority": {"high": 1},
                                 "by_category": {"manageability": 1}}
    finding = report["findings"][0]
    assert finding["check"] == "Avoid hard-coded values"
    assert finding["record"] == f"sys_script_include:{sys_id}"
    assert finding["resolution"] == "Use a system property."
    point = [path for path, _ in harness.fake.cicd_bodies]
    assert point == ["instance_scan/point_scan"]
    assert ("POST", "/api/sn_cicd/instance_scan/point_scan",
            {"target_table": "sys_script_include", "target_sys_id": sys_id}) \
        in harness.fake.requests
    again = scanner.results(progress_id=report["runs"][0]["progress_id"])
    assert again["summary"]["findings"] == 1
    by_result = scanner.results(result_sys_ids=[report["runs"][0]["result"]["sys_id"]])
    assert by_result["findings"] == again["findings"]
    assert "findings_truncated" not in by_result


def test_scan_findings_are_sanitised_and_capped(
    harness: Harness, monkeypatch: pytest.MonkeyPatch,
) -> None:
    dormant = ("Active user with no activity. <a target='_blank' "
               "href=sys_user.do?sys_id=005d>Jane Doe</a> has not logged in &amp; is "
               "<b>dormant</b>.")
    assert clean_details(dormant) == \
        "Active user with no activity. [record] has not logged in & is dormant ."
    assert clean_details(None) == "" and len(clean_details("x" * 900)) == 500
    label, sys_id = _applied(harness)
    check = harness.fake.insert("scan_check", {"name": "C", "category": "security",
                                               "priority": "3"}, track=False)
    harness.fake.scan_findings[sys_id] = [check, check, check]
    monkeypatch.setattr(instancescan, "FINDING_LIMIT", 2)
    report = InstanceScanner(harness.config, harness.client, sleep=lambda _: None).scan(
        confirm=True, label=label)
    assert report["summary"]["findings"] == 3 and len(report["findings"]) == 2
    assert report["findings_truncated"] == {"listed": 2, "total": 3}


def test_suite_scan_and_limits(harness: Harness) -> None:
    label, sys_id = _applied(harness)
    scanner = InstanceScanner(harness.config, harness.client, sleep=lambda _: None)
    suite = "e" * 32
    report = scanner.scan(confirm=True, label=label, suite_sys_id=suite)
    assert report["mode"] == "suite" and report["summary"]["findings"] == 0
    path, body = harness.fake.cicd_bodies[-1]
    assert path == f"instance_scan/suite_scan/{suite}/update_sets"
    assert body == {"update_set_sys_ids": report["update_sets"]}
    with pytest.raises(ValueError):
        scanner.scan(confirm=True, label=label, suite_sys_id="nope")
    with pytest.raises(ConfigurationError, match="suite scan needs"):
        scanner.scan(confirm=True, targets=[("sys_script", "a" * 32)], suite_sys_id=suite)
    with pytest.raises(ServiceNowError, match="no snagentic update sets"):
        scanner.scan(confirm=True, label="unknown")
    with pytest.raises(ConfigurationError, match="nothing to scan"):
        scanner.scan(confirm=True, update_set_ids=[harness.fake.open_update_set("empty")])
    with pytest.raises(ConfigurationError, match="point-scan limit"):
        scanner.scan(confirm=True, targets=[("sys_script", f"{i:032x}") for i in range(51)])
    with pytest.raises(ValueError, match="update set"):
        scanner.update_set_targets(["bad"])
    with pytest.raises(ConfigurationError):
        scanner.results()
    for kwargs in ({"result_sys_ids": ["x"]}, {"progress_id": "x"}):
        with pytest.raises(ValueError):
            scanner.results(**kwargs)
    with pytest.raises(ServiceNowError, match="no scan result"):
        scanner.results(progress_id="f" * 32)
    production = harness.config.model_copy(update={"kind": "production"})
    with pytest.raises(PolicyDeniedError):
        InstanceScanner(production, harness.client).scan(confirm=True, label=label)
    assert parse_target(f"sys_script:{sys_id}") == ("sys_script", sys_id)
    for bad in ("sys_script", f"Bad:{sys_id}", "sys_script:123"):
        with pytest.raises(ValueError):
            parse_target(bad)


def test_scan_cli_stores_report_and_promote_includes_it(
    harness: Harness, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.chdir(harness.root)
    monkeypatch.setattr(instance_cli, "TRANSPORT", harness.fake.transport())
    monkeypatch.setattr("snagentic.instance.ops.time.sleep", lambda _: None)
    label, sys_id = _applied(harness)
    unconfirmed = cli(capsys, "scan", "--label", label)
    assert unconfirmed["exit"] == 2
    scanned = cli(capsys, "scan", "--label", label, "--confirm")["result"]
    assert scanned["report_path"] == f".snagentic/instances/dev/scans/{label}.json" \
        or scanned["report_path"].endswith(f"scans/{label}.json")
    stored = cli(capsys, "scan-results", "--label", label)["result"]
    assert stored["results"][0]["sys_id"] == scanned["runs"][0]["result"]["sys_id"]
    assert cli(capsys, "scan-results", "--label", "other")["exit"] == 2
    adhoc = cli(capsys, "scan", "--target", f"sys_script_include:{sys_id}", "--confirm")
    assert "report_path" not in adhoc["result"]
    promoted = cli(capsys, "promote", "--label", label, "--confirm")["result"]
    assert promoted["instance_scan"]["summary"]["findings"] == 0
    assert promoted["instance_scan"]["results"] == [scanned["runs"][0]["result"]["sys_id"]]


def test_promote_without_scan_recommends_one(
    harness: Harness, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.chdir(harness.root)
    monkeypatch.setattr(instance_cli, "TRANSPORT", harness.fake.transport())
    label, _ = _applied(harness)
    promoted = cli(capsys, "promote", "--label", label, "--confirm")["result"]
    assert promoted["instance_scan"] is None
    assert "instance scan" in promoted["next_steps"][0]
