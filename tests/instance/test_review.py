from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
import yaml
from conftest import Harness

import snagentic.cli.instance as instance_cli
from snagentic.cli.main import main
from snagentic.instance.changes import ChangePlanner
from snagentic.instance.mirror import MirrorRepository
from snagentic.review.engine import Reviewer, ReviewTarget, Standards, script_fields
from snagentic.review.lexer import tokenize
from snagentic.review.rules import RULES, rule_catalog
from snagentic.review.scan import load_customized, review_mirror

REVIEWER = Reviewer(RULES)


def rules_for(table: str, values: dict[str, str], *, scope: str = "global",
              operation: str = "existing", previous: dict[str, str] | None = None,
              customized: bool | None = None) -> list[str]:
    target = ReviewTarget(f"instances/dev/metadata/{scope}/{table}/x--1", table, scope,
                          operation, values, previous, customized)
    return [finding.rule_id for finding in REVIEWER.review(target)]


BR = {"when": "before", "advanced": "true"}
CLIENT = {"type": "onLoad"}

# (rule, table, values, scope) triples that must and must not trigger the rule.
BAD: list[tuple[str, str, dict[str, str], str]] = [
    ("SN-SEC-001", "sys_script_include", {"script": "var x = eval(input);"}, "global"),
    ("SN-SEC-001", "sys_script_include", {"script": "var f = new Function('a', b);"}, "global"),
    ("SN-SEC-001", "sys_script_include",
     {"script": "var e = new GlideEvaluator(); GlideEvaluator.evaluateString(s);"}, "global"),
    ("SN-SEC-002", "sys_script_include", {"script": "var password = 'Sup3rS3cret!';"}, "global"),
    ("SN-SEC-002", "sys_script_include", {"script": "var c = {'api_key': 'abcdef123456'};"},
     "global"),
    ("SN-SEC-002", "sys_script_include", {"script": "r.setBasicAuth('admin', 'hunter22');"},
     "global"),
    ("SN-SEC-002", "sys_script_include",
     {"script": "r.setRequestHeader('Authorization', 'Bearer abcdefghijkl');"}, "global"),
    ("SN-SEC-003", "sys_script_include",
     {"script": "gr.addEncodedQuery('active=true^caller_id=' + userId);"}, "global"),
    ("SN-SEC-003", "sys_script_include",
     {"script": "gr.addEncodedQuery(`number=${input}`);"}, "global"),
    ("SN-SEC-004", "sys_security_acl", {"script": "answer = true;"}, "global"),
    ("SN-SEC-005", "sys_script_include",
     {"script": "var f = new Packages.java.io.File('/tmp');"}, "global"),
    ("SN-PERF-001", "sys_script", {**BR, "script": "current.state = 2;\ncurrent.update();"},
     "global"),
    ("SN-PERF-001", "sys_script", {**BR, "when": "after", "script": "current.update();"},
     "global"),
    ("SN-PERF-002", "sys_script_include",
     {"script": "while (gr.next()) {\n var u = new GlideRecord('sys_user');\n u.get(gr.x);\n}"},
     "global"),
    ("SN-PERF-002", "sys_script_include",
     {"script": "ids.forEach(function (id) { var g = new GlideAggregate('task'); });"},
     "global"),
    ("SN-PERF-003", "sys_script_include", {"script": "if (gr.getRowCount() > 0) {}"}, "global"),
    ("SN-PERF-004", "sys_script_include", {"script": "gs.sleep(1000);"}, "global"),
    ("SN-PERF-005", "sys_script_client",
     {**CLIENT, "script": "var gr = new GlideRecord('sys_user');"}, "global"),
    ("SN-PERF-006", "sys_script_client", {**CLIENT, "script": "ga.getXMLWait();"}, "global"),
    ("SN-PERF-006", "sys_script_client",
     {**CLIENT, "script": "var c = g_form.getReference('caller_id');"}, "global"),
    ("SN-PERF-007", "sys_script",
     {**BR, "script": "var r = new sn_ws.RESTMessageV2('x', 'get');"}, "global"),
    ("SN-UPG-001", "sys_script_client",
     {**CLIENT, "script": "document.getElementById('x').style.display = 'none';"}, "global"),
    ("SN-UPG-001", "sys_script_client", {**CLIENT, "script": "$j('#x').hide();"}, "global"),
    ("SN-UPG-001", "sys_ui_action",
     {"client": "true", "script": "function go() { gel('x').focus(); }"}, "global"),
    ("SN-MNT-001", "sys_script_include",
     {"script": "gr.get('0123456789abcdef0123456789abcdef');"}, "global"),
    ("SN-MNT-002", "sys_script_include",
     {"script": "var u = 'https://acme.service-now.com/api';"}, "global"),
    ("SN-MNT-003", "sys_script_include", {"script": "gs.log('x');"}, "x_acme_app"),
    ("SN-MNT-004", "sys_script_include", {"script": "gr.setWorkflow(false);"}, "global"),
    ("SN-MNT-005", "sys_script_include",
     {"name": "Greeter", "script": "var Greeting = Class.create();"}, "global"),
    ("SN-UX-001", "sys_script_client",
     {"type": "onChange", "script": "function onChange(c, o, n) { g_form.clearValue('x'); }"},
     "global"),
    ("SN-UX-002", "sys_script_client",
     {**CLIENT, "script": "function onLoad() { g_form.setMandatory('x', true); }"}, "global"),
    ("SN-UX-003", "sys_script_client", {**CLIENT, "script": "alert('saved');"}, "global"),
    ("SN-UX-003", "sp_widget", {"client_script": "if (confirm('go?')) { c.go(); }"}, "global"),
]

GOOD: list[tuple[str, str, dict[str, str], str]] = [
    ("SN-SEC-001", "sys_script_include",
     {"script": "// eval(x) is forbidden\nvar s = 'eval(y)'; obj.eval(z);"}, "global"),
    ("SN-SEC-002", "sys_script_include",
     {"script": "var password = gs.getProperty('x.password');\nvar token = '';"}, "global"),
    ("SN-SEC-002", "sys_script_include", {"script": "r.setBasicAuth(user, pass);"}, "global"),
    ("SN-SEC-003", "sys_script_include",
     {"script": "gr.addEncodedQuery('active=true^' + 'priority=1');\n"
                "gr.addQuery('caller_id', userId);"}, "global"),
    ("SN-SEC-004", "sys_security_acl",
     {"script": "answer = gs.hasRole('itil') && current.active == true;"}, "global"),
    ("SN-SEC-005", "sys_script_include", {"script": "var p = x.Packages.y;"}, "global"),
    ("SN-PERF-001", "sys_script", {**BR, "script": "current.state = 2;"}, "global"),
    ("SN-PERF-001", "sys_script", {**BR, "when": "async", "script": "current.update();"},
     "global"),
    ("SN-PERF-001", "sys_script",
     {**BR, "script": "var p = new GlideRecord('x'); p.get(a); p.update();"}, "global"),
    ("SN-PERF-001", "sys_script",
     {"when": "before", "advanced": "false", "script": "current.update();"}, "global"),
    ("SN-PERF-002", "sys_script_include",
     {"script": "var gr = new GlideRecord('x');\nwhile (gr.next()) { ids.push(gr.x); }"},
     "global"),
    ("SN-PERF-003", "sys_script_include",
     {"script": "var ga = new GlideAggregate('x'); ga.addAggregate('COUNT');"}, "global"),
    ("SN-PERF-004", "sys_script_include", {"script": "gs.eventQueue('x.later', gr);"},
     "global"),
    ("SN-PERF-005", "sys_script_include", {"script": "var gr = new GlideRecord('x');"},
     "global"),
    ("SN-PERF-005", "sys_ui_action",
     {"client": "true",
      "script": "function a(){}\nif (typeof window == 'undefined') {"
                " var gr = new GlideRecord('x'); }"}, "global"),
    ("SN-PERF-006", "sys_script_client",
     {**CLIENT, "script": "ga.getXMLAnswer(cb); g_form.getReference('caller_id', cb);"},
     "global"),
    ("SN-PERF-007", "sys_script",
     {**BR, "when": "async", "script": "var r = new sn_ws.RESTMessageV2('x', 'get');"},
     "global"),
    ("SN-UPG-001", "sp_widget", {"client_script": "$scope.x = 1; $('#a');"}, "global"),
    ("SN-UPG-001", "sys_script_client",
     {**CLIENT, "script": "var d = g_form.document; x.$(a);"}, "global"),
    ("SN-MNT-001", "sys_script_include",
     {"script": "// 0123456789abcdef0123456789abcdef\nvar id = gs.getProperty('x');"},
     "global"),
    ("SN-MNT-002", "sys_script_include",
     {"script": "var u = gs.getProperty('glide.servlet.uri');"}, "global"),
    ("SN-MNT-003", "sys_script_include", {"script": "gs.log('x');"}, "global"),
    ("SN-MNT-003", "sys_script_include", {"script": "gs.info('x');"}, "x_acme_app"),
    ("SN-MNT-004", "sys_script_include", {"script": "gr.setWorkflow(true);"}, "global"),
    ("SN-MNT-005", "sys_script_include",
     {"name": "Greeter", "script": "var Greeter = Class.create();"}, "global"),
    ("SN-UX-001", "sys_script_client",
     {"type": "onChange", "script": "function onChange(c, o, n, isLoading) {\n"
                                   "if (isLoading) return; }"}, "global"),
    ("SN-UX-002", "sys_script_client",
     {**CLIENT, "script": "function onLoad() { g_form.setMandatory('x', true);"
                         " g_form.setValue('y', 1); }"}, "global"),
    ("SN-UX-002", "sys_script_client",
     {**CLIENT, "script": "var ga = new GlideAjax('X'); g_form.setDisplay('x', false);"},
     "global"),
    ("SN-UX-003", "sys_script_client",
     {**CLIENT, "script": "g_form.addErrorMessage('x'); window.alert;"}, "global"),
]


@pytest.mark.parametrize(("rule", "table", "values", "scope"), BAD,
                         ids=[f"{r}-{i}" for i, (r, *_rest) in enumerate(BAD)])
def test_rule_triggers_on_anti_pattern(rule: str, table: str, values: dict[str, str],
                                       scope: str) -> None:
    assert rule in rules_for(table, values, scope=scope)


@pytest.mark.parametrize(("rule", "table", "values", "scope"), GOOD,
                         ids=[f"{r}-{i}" for i, (r, *_rest) in enumerate(GOOD)])
def test_rule_ignores_good_practice(rule: str, table: str, values: dict[str, str],
                                    scope: str) -> None:
    assert rule not in rules_for(table, values, scope=scope)


def test_every_rule_has_fixtures_and_complete_metadata() -> None:
    ids = [rule.id for rule in RULES]
    assert len(ids) == len(set(ids))
    record_rules = {"SN-UPG-002", "SN-MNT-006"}
    assert set(ids) - record_rules <= {rule for rule, *_ in BAD}
    assert set(ids) - record_rules <= {rule for rule, *_ in GOOD}
    for entry in rule_catalog():
        assert entry["title"] and entry["rationale"] and entry["fix"]
        assert entry["severity"] in {"block", "warn", "info"}


def test_record_rules() -> None:
    assert "SN-UPG-002" in rules_for("sys_script", {"script": "a();"}, operation="update",
                                     previous={"script": "b();"}, customized=False)
    assert "SN-UPG-002" not in rules_for("sys_script", {"script": "a();"}, operation="update",
                                         previous={"script": "b();"}, customized=True)
    assert "SN-UPG-002" not in rules_for("sys_script", {"script": "a();"}, operation="update",
                                         previous={"script": "b();"}, customized=None)
    assert "SN-MNT-006" in rules_for("sys_script_include", {"script": "a();"},
                                     operation="create")
    assert "SN-MNT-006" not in rules_for("sys_script_include",
                                         {"script": "a();", "description": "Does a"},
                                         operation="create")
    assert "SN-MNT-006" not in rules_for("sys_script_include", {"script": "a();"})


def test_updates_report_only_introduced_findings() -> None:
    before = "gr.get('0123456789abcdef0123456789abcdef');\ngs.sleep(10);"
    after = "// moved\n" + before + "\ngr.setWorkflow(false);"
    assert rules_for("sys_script_include", {"script": after}, operation="update",
                     previous={"script": before}) == ["SN-MNT-004"]


def test_credentials_are_redacted_from_evidence() -> None:
    target = ReviewTarget("p", "sys_script_include", "global", "existing",
                          {"script": "var password = 'Sup3rS3cret!';"})
    findings = REVIEWER.review(target)
    assert findings and "Sup3rS3cret" not in json.dumps([f.as_dict() for f in findings])


def test_same_line_is_reported_once_per_rule() -> None:
    script = "var ids = ['0123456789abcdef0123456789abcdef', 'fedcba9876543210fedcba9876543210'];"
    assert rules_for("sys_script_include", {"script": script}).count("SN-MNT-001") == 1


def test_script_fields_depend_on_record_settings() -> None:
    assert script_fields("sys_ui_action", {"client": "false"}) == {"script": "server"}
    assert script_fields("sys_ui_action", {"client": "true", "script": "a()"}) == {
        "script": "client"}
    assert script_fields("sys_script", {"advanced": "false"}) == {}
    assert script_fields("unknown_table", {}) == {}


def test_lexer_separates_code_comments_strings_and_regex() -> None:
    script = tokenize(
        "var a = /eval\\(/g; // eval(x)\n/* gs.sleep(1) */ var b = 'it''s';\n"
        "var t = `x ${eval(y)} z`; x = a / b / c; f(\"q\\\"uote\");\n@#"
    )
    kinds = [(t.kind, t.value) for t in script.tokens]
    assert ("regex", "/eval\\(/g") in kinds
    assert [c.text for c in script.comments] == ["eval(x)", "gs.sleep(1)"]
    assert ("string", 'q"uote') in kinds
    assert ("ident", "eval") not in kinds
    assert sum(1 for kind, value in kinds if kind == "punct" and value == "/") == 2
    assert script.tokens[-1].line == 4
    unbalanced = tokenize("f(a, (b], {c: [1, 2]})")
    assert unbalanced.match(1) is None and unbalanced.arguments(1) == []
    assert len(unbalanced.arguments(4)) == 2
    assert tokenize("'unterminated\nx").tokens[-1].value == "x"
    assert tokenize("var x = 1.5e3 + .5; /* open").tokens[3].kind == "number"


def test_standards_overrides(tmp_path: Path) -> None:
    path = tmp_path / "standards.yaml"
    ids = [rule.id for rule in RULES]
    assert Standards.load(path, ids).disabled == set()
    path.write_text(yaml.safe_dump({
        "rules": {"SN-PERF-004": {"enabled": False}, "SN-MNT-001": {"severity": "info"}},
        "exclude_paths": ["metadata/global/sys_script_include/legacy-*"],
    }))
    standards = Standards.load(path, ids)
    reviewer = Reviewer(RULES, standards)
    target = ReviewTarget("instances/dev/metadata/global/sys_script_include/new--1",
                          "sys_script_include", "global", "existing",
                          {"script": "gs.sleep(1); gr.get('0123456789abcdef0123456789abcdef');"})
    findings = reviewer.review(target)
    assert [(f.rule_id, f.severity) for f in findings] == [("SN-MNT-001", "info")]
    excluded = ReviewTarget("instances/dev/metadata/global/sys_script_include/legacy-x--1",
                            "sys_script_include", "global", "existing", {"script": "gs.sleep(1);"})
    assert reviewer.review(excluded) == []
    for bad in ({"rules": {"SN-NOPE-001": {}}}, {"rules": {"SN-PERF-004": {"x": 1}}},
                {"rules": {"SN-PERF-004": {"severity": "fatal"}}}, {"other": 1}, ["x"],
                {"rules": []}, {"exclude_paths": "x"}):
        path.write_text(yaml.safe_dump(bad))
        with pytest.raises(ValueError):
            Standards.load(path, ids)


def _write_record(directory: Path, meta: dict[str, Any], record: dict[str, Any],
                  script: str) -> None:
    directory.mkdir(parents=True)
    (directory / "_meta.yaml").write_text(yaml.safe_dump({"files": {"script": "script.js"},
                                                          **meta}))
    (directory / "record.yaml").write_text(yaml.safe_dump(record))
    (directory / "script.js").write_text(script)


def test_review_mirror_filters_and_customized_state(tmp_path: Path) -> None:
    workspace = tmp_path / "instances" / "dev"
    metadata = workspace / "metadata"
    _write_record(metadata / "global" / "sys_script_include" / "a--1",
                  {"sys_class_name": "sys_script_include", "sys_update_name": "si_a"},
                  {"name": "A"}, "gs.sleep(1);")
    _write_record(metadata / "x_app" / "sys_script" / "b--2",
                  {"sys_class_name": "sys_script", "sys_update_name": "br_b"},
                  {"when": "before", "advanced": "true"}, "current.update();")
    _write_record(metadata / "domains" / "acme" / "global" / "sys_script" / "c--3",
                  {"sys_class_name": "sys_script", "sys_update_name": "br_c"},
                  {"when": "after", "advanced": "true"}, "current.update();")
    (metadata / "global" / "sys_properties" / "p--4").mkdir(parents=True)
    (metadata / "global" / "sys_script_include" / "empty").mkdir()
    assert load_customized(workspace) is None
    with pytest.raises(ValueError, match="customer-updates"):
        review_mirror(tmp_path, workspace, customized_only=True)
    full = review_mirror(tmp_path, workspace)
    assert full["summary"]["records_reviewed"] == 3
    assert full["summary"]["by_rule"] == {"SN-PERF-001": 2, "SN-PERF-004": 1}
    assert full["findings"][0]["severity"] == "block"
    assert review_mirror(tmp_path, workspace, tables=["sys_script"], scopes=["global"])[
        "summary"]["records_reviewed"] == 1
    assert review_mirror(tmp_path, workspace, paths=["metadata/x_app"])["summary"][
        "records_reviewed"] == 1
    limited = review_mirror(tmp_path, workspace, min_severity="block", limit=1)
    assert len(limited["findings"]) == 1 and limited["truncated"] == 1
    only = review_mirror(tmp_path, workspace, rules=["SN-PERF-004"])
    assert only["summary"]["by_rule"] == {"SN-PERF-004": 1}

    (workspace / "model").mkdir()
    (workspace / "model" / "customer-updates.yaml").write_text(yaml.safe_dump({
        "customer_updates": [{"name": "si_a",
                              "path": "metadata/global/sys_script_include/a--1"}],
    }))
    assert load_customized(workspace) == {"si_a"}
    customized = review_mirror(tmp_path, workspace, customized_only=True)
    assert customized["summary"]["records_reviewed"] == 1
    assert customized["findings"][0]["path"] == (
        "instances/dev/metadata/global/sys_script_include/a--1")
    (workspace / "model" / "customer-updates.yaml").write_text("customer_updates: 1\n")
    assert load_customized(workspace) is None


def _integrated(harness: Harness) -> Path:
    sys_id = harness.fake.insert(
        "sys_script", {"name": "Set state", "collection": "incident", "when": "before",
                       "advanced": "true", "script": "current.state = 2;"},
    )
    harness.sync().fetch()
    MirrorRepository(harness.paths, harness.config.mirror_branch).integrate(message="integrate")
    return harness.paths.metadata / "global" / "sys_script" / f"set-state--{sys_id}"


def test_plan_reports_introduced_findings_without_changing_plan_id(harness: Harness) -> None:
    directory = _integrated(harness)
    (directory / "script.js").write_text("current.state = 3;\n")
    clean = ChangePlanner(harness.paths, harness.config).plan()
    assert clean["review"]["summary"]["findings"] == 0
    assert clean["review"]["summary"]["mode"] == "enforcing"
    assert clean["gate"]["passed"] is True
    (directory / "script.js").write_text("current.state = 3;\ncurrent.update();\n")
    plan = ChangePlanner(harness.paths, harness.config).plan()
    assert [f["rule"] for f in plan["review"]["findings"]] == ["SN-PERF-001"]
    assert plan["review"]["findings"][0]["line"] == 2
    (harness.paths.workspace / "standards.yaml").write_text(
        yaml.safe_dump({"rules": {"SN-PERF-001": {"enabled": False}}}))
    silenced = ChangePlanner(harness.paths, harness.config).plan()
    assert silenced["review"]["findings"] == []
    assert silenced["plan_id"] == plan["plan_id"]


def cli(capsys: pytest.CaptureFixture[str], *args: str) -> dict[str, Any]:
    try:
        main(["--json", "instance", *args])
    except SystemExit as exc:
        out = json.loads(capsys.readouterr().err)
        out["exit"] = exc.code
        return out
    return json.loads(capsys.readouterr().out)


def test_review_cli(harness: Harness, monkeypatch: pytest.MonkeyPatch,
                    capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setattr(instance_cli, "TRANSPORT", harness.fake.transport())
    directory = _integrated(harness)
    catalogue = cli(capsys, "review", "--rules")["result"]["rules"]
    assert len(catalogue) == len(RULES)
    new = harness.paths.metadata / "global" / "sys_script_include" / "helper"
    new.mkdir(parents=True)
    (new / "record.yaml").write_text(yaml.safe_dump({"name": "Helper"}))
    (new / "script.js").write_text("var Helper = Class.create();\ngs.sleep(5);\n")
    changes = cli(capsys, "review")["result"]
    assert changes["target"] == "changes" and len(changes["plan_id"]) == 16
    assert sorted(f["rule"] for f in changes["findings"]) == ["SN-MNT-006", "SN-PERF-004"]
    warn = cli(capsys, "review", "--min-severity", "warn")["result"]
    assert [f["rule"] for f in warn["findings"]] == ["SN-PERF-004"]
    (directory / "script.js").write_text("current.update();\n")
    scan = cli(capsys, "review", "--table", "sys_script")["result"]
    assert scan["target"] == "mirror" and scan["summary"]["by_rule"] == {"SN-PERF-001": 1}
    assert cli(capsys, "review", "--all", "--limit", "0")["exit"] == 2
    harness.fake.insert("sys_script_include", {"name": "Remote", "script": "var a;"})
    harness.sync().fetch()
    stale = cli(capsys, "review")
    assert stale["exit"] == 2 and "integrate" in stale["error"]


def test_calibrated_false_positives_are_ignored() -> None:
    constants = ("var X = {PAGE_TOKEN: 'page_token', STAGE_CHANGE_PWD: 'Change password',\n"
                 "PROP_PASSWORD: 'glide.x.password'};")
    assert "SN-SEC-002" not in rules_for("sys_script_include", {"script": constants})
    namespace = "var e = new SOAPEnvelope('x', 'http://www.service-now.com/');"
    assert "SN-MNT-002" not in rules_for("sys_script_include", {"script": namespace})
    padded = {"name": "Greeter ", "script": "var Greeter = Class.create();"}
    assert "SN-MNT-005" not in rules_for("sys_script_include", padded)


def test_reviewer_skill_lists_every_rule_with_its_severity() -> None:
    skill = (Path(__file__).resolve().parents[2]
             / "copilot-plugin/skills/servicenow-reviewer/SKILL.md")
    rows = {
        cells[0]: cells[1]
        for line in skill.read_text(encoding="utf-8").splitlines()
        if line.startswith("| SN-")
        for cells in [[cell.strip() for cell in line.strip("|").split("|")]]
    }
    assert rows == {rule.id: rule.severity for rule in RULES}
    standards = skill.parents[3] / "docs" / "servicenow-standards.md"
    documented = {
        cells[0]: (cells[1], cells[2].replace(" ", "_"))
        for line in standards.read_text(encoding="utf-8").splitlines()
        if line.startswith("| SN-")
        for cells in [[cell.strip() for cell in line.strip("|").split("|")]]
    }
    assert documented == {rule.id: (rule.severity, rule.category) for rule in RULES}
    skills = skill.parent.parent
    guidance = "".join(path.read_text(encoding="utf-8") for path in skills.glob("*/SKILL.md")
                       if path.parent.name != "servicenow-reviewer")
    assert [rule.id for rule in RULES if rule.id not in guidance] == ["SN-MNT-006"]
    for path in skills.glob("servicenow-*/SKILL.md"):
        front = path.read_text(encoding="utf-8").split("---")[1]
        meta = yaml.safe_load(front)
        assert meta["name"] == path.parent.name and len(meta["description"]) > 50
