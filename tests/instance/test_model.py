from __future__ import annotations

import yaml
from conftest import Harness


def _model(harness: Harness, table: str) -> dict:
    text = harness.git("show", f"servicenow-remote/dev:instances/dev/model/tables/{table}.yaml")
    return yaml.safe_load(text)


def _seed(harness: Harness) -> dict[str, str]:
    fake = harness.fake
    ids: dict[str, str] = {}
    fake.insert("sys_dictionary", {"name": "task", "element": "state", "internal_type": "integer",
                                   "column_label": "State", "mandatory": "true"}, track=False)
    fake.insert("sys_choice", {"name": "task", "element": "state", "value": "1", "label": "New",
                               "language": "en"}, track=False)
    fake.insert("sys_choice", {"name": "task", "element": "state", "value": "9", "label": "Old",
                               "language": "en", "inactive": "true"}, track=False)
    fake.insert("sys_dictionary", {"name": "incident", "element": "caller_id",
                                   "internal_type": "reference", "reference": "sys_user"},
                track=False)
    ids["br"] = fake.insert("sys_script", {"name": "Close children", "collection": "task",
                                           "when": "after", "order": "100", "active": "true",
                                           "action_update": "true",
                                           "script": "close();"}, track=False)
    ids["custom_br"] = fake.insert("sys_script", {"name": "Custom rule", "collection": "incident",
                                                  "when": "before", "order": "50",
                                                  "script": "custom();"})
    read = fake.insert("sys_security_operation", {"name": "read"}, track=False)
    ids["acl"] = fake.insert("sys_security_acl", {"name": "incident.caller_id",
                                                  "operation": read, "type": "record",
                                                  "active": "true"}, track=False)
    role = fake.insert("sys_user_role", {"name": "itil"}, track=False)
    fake.insert("sys_security_acl_role", {"sys_security_acl": ids["acl"], "sys_user_role": role},
                track=False)
    policy = fake.insert("sys_ui_policy", {"short_description": "Caller mandatory",
                                           "table": "incident", "active": "true"}, track=False)
    fake.insert("sys_ui_policy_action", {"ui_policy": policy, "table": "incident",
                                         "field": "caller_id", "mandatory": "true"}, track=False)
    fake.insert("sysevent_register", {"event_name": "incident.assigned", "table": "incident"},
                track=False)
    fake.insert("sysevent_script_action", {"name": "Notify", "event_name": "incident.assigned",
                                           "script": "notify();"}, track=False)
    return ids


def test_table_model_describes_fields_behaviour_and_inheritance(harness: Harness) -> None:
    ids = _seed(harness)
    result = harness.sync().fetch()
    assert result["model"]["tables"] > 0

    task = _model(harness, "task")
    assert task["extended_by"] == ["incident"]
    assert task["fields"]["state"] == {"type": "integer", "label": "State", "mandatory": True,
                                       "choices": {"1": "New"}}
    rule = task["behaviour"]["business_rules"][0]
    assert rule["name"] == "Close children" and rule["when"] == "after"
    assert rule["path"].startswith("metadata/global/sys_script/close-children--")
    assert rule["files"] == ["script.js"]
    assert "customized" not in rule

    incident = _model(harness, "incident")
    assert incident["extends"] == ["task"]
    assert incident["fields"]["caller_id"]["reference"] == "sys_user"
    assert incident["inherited"]["task"] == {"model": "task.yaml", "fields": 1,
                                             "business_rules": 1}
    custom = incident["behaviour"]["business_rules"][0]
    assert custom["sys_id"] == ids["custom_br"] and custom["customized"] is True
    acl = incident["behaviour"]["acls"][0]
    assert acl["field"] == "caller_id" and acl["operation"] == "read"
    assert acl["roles"] == ["itil"]
    policy = incident["behaviour"]["ui_policies"][0]
    assert policy["actions"][0]["field"] == "caller_id"
    event = incident["behaviour"]["events"][0]
    assert event["script_actions"][0]["name"] == "Notify"

    updates = yaml.safe_load(harness.git(
        "show", "servicenow-remote/dev:instances/dev/model/customer-updates.yaml"
    ))["customer_updates"]
    assert [item["name"] for item in updates] == [f"sys_script_{ids['custom_br']}"]
    assert updates[0]["path"].startswith("metadata/global/sys_script/custom-rule--")


def test_table_model_refreshes_incrementally(harness: Harness) -> None:
    ids = _seed(harness)
    harness.sync().fetch()
    harness.fake.update(ids["br"], {"when": "before"}, by="glide.maint")
    harness.fake.insert("sys_dictionary", {"name": "incident", "element": "u_new",
                                           "internal_type": "string"})
    result = harness.sync().fetch()
    assert result["mode"] == "incremental"
    task = _model(harness, "task")
    assert task["behaviour"]["business_rules"][0]["when"] == "before"
    assert "u_new" in _model(harness, "incident")["fields"]


def test_functional_docs_describe_table_behaviour(harness: Harness) -> None:
    from snagentic.instance.docs import DocumentationGenerator
    from snagentic.instance.functional import readable_query, script_effects
    from snagentic.instance.mirror import MirrorRepository

    _seed(harness)
    harness.fake.insert("sys_script", {
        "name": "Default category", "collection": "incident", "when": "before", "order": "90",
        "active": "true", "action_insert": "true", "advanced": "true",
        "filter_condition": "categoryISEMPTY^EQ",
        "script": "// Default the category for new incidents\ncurrent.category = 'inquiry';\n"
                  "gs.eventQueue('incident.defaulted', current);",
    })
    harness.sync().fetch()
    MirrorRepository(harness.paths, harness.config.mirror_branch).integrate(message="m")
    result = DocumentationGenerator(harness.paths, harness.config).generate()
    assert "tables/incident.md" in result["pages"]
    page = (harness.paths.docs / "pages" / "tables" / "incident.md").read_text()
    assert "## Server-side processing (business rules)" in page
    assert "### Insert" in page and "Default category" in page and "★" in page
    assert "when: category is empty" in page
    assert "sets `category`" in page and "fires events `incident.defaulted`" in page
    assert "notes: Default the category for new incidents" in page
    assert "Close children" in page and "_(from `task`)_" in page
    assert "## Access control" in page and "itil" in page
    assert "`caller_id`: mandatory=true" in page
    assert 'snagentic:narrative id="table-incident-purpose"' in page

    assert readable_query("priority=1^ORstate=2^active=true^EQ") == (
        "priority = 1 OR state = 2 AND active = true"
    )
    assert script_effects("g_form.setMandatory('caller_id', true);", client=True) == [
        "setMandatory: `caller_id`"
    ]


def test_notification_without_trigger_has_no_trailing_placeholder(tmp_path) -> None:
    from snagentic.instance.functional import FunctionalDocs

    docs = FunctionalDocs(tmp_path, tmp_path, lambda _block_id, _existing: "")
    sections = {
        "notifications": [
            (
                "incident",
                {
                    "name": "No trigger notification",
                    "path": "metadata/global/sysevent_email_action/no-trigger--abc",
                    "active": "true",
                    "condition": "^EQ",
                },
            )
        ]
    }

    line = next(
        line
        for line in docs._notifications(sections, "incident")
        if "No trigger notification" in line
    )

    assert line == line.rstrip()
    assert not line.endswith((" on", " when"))
