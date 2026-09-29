from __future__ import annotations

import shutil

import yaml
from conftest import Harness

from snagentic.instance.docs import DocumentationGenerator
from snagentic.instance.index import InstanceIndex
from snagentic.instance.mirror import MirrorRepository


def _populate(harness: Harness) -> None:
    fake = harness.fake
    scope = fake.create_scope("x_acme_hr")
    fake.insert("sys_db_object", {"name": "x_acme_hr_case", "label": "HR Case",
                                  "sys_scope": scope})
    fake.insert("sys_dictionary", {"name": "x_acme_hr_case", "element": "employee",
                                   "column_label": "Employee", "internal_type": "reference",
                                   "reference": "sys_user", "sys_scope": scope})
    fake.insert("sys_script_include", {
        "name": "CaseUtil", "api_name": "x_acme_hr.CaseUtil", "sys_scope": scope,
        "description": "Helpers for HR cases",
        "script": "var CaseUtil = Class.create();\nCaseUtil.prototype = {\n"
                  "  initialize: function() {},\n  assign: function(id) {\n"
                  "    var gr = new GlideRecord('x_acme_hr_case');\n"
                  "    new Notifier().send(id);\n    gs.eventQueue('x_acme_hr.assigned', gr);\n"
                  "  }\n};",
    })
    fake.insert("sys_script_include", {"name": "Notifier", "sys_scope": scope,
                                       "script": "var Notifier = Class.create();"})
    fake.insert("sys_script", {"name": "Assign on insert", "collection": "x_acme_hr_case",
                               "when": "after", "action_insert": "true", "sys_scope": scope,
                               "script": "new CaseUtil().assign(current.sys_id);"})
    fake.open_update_set("HR sprint 1", by="alice", scope=scope)
    fake.update(next(s for s, r in fake.records.items() if r.get("name") == "Notifier"),
                {"script": "var Notifier = Class.create(); // v2"}, by="alice")
    harness.sync().fetch()
    MirrorRepository(harness.paths, harness.config.mirror_branch).integrate(message="m")


def test_index_builds_dependency_graph(harness: Harness) -> None:
    _populate(harness)
    index = InstanceIndex(harness.paths.search_index)
    summary = index.rebuild(harness.paths.metadata, harness.paths.root)
    assert summary["records"] == 5
    callers = {row["name"] for row in index.references("CaseUtil")}
    assert callers == {"Assign on insert"}
    table_users = {(row["name"], row["kind"]) for row in index.references("x_acme_hr_case")}
    assert ("x_acme_hr.CaseUtil", "queries_table") in table_users
    assert ("Assign on insert", "table") in table_users
    assert [row["name"] for row in index.search("eventQueue")] == ["x_acme_hr.CaseUtil"]


def test_index_accepts_duplicate_sys_ids_in_distinct_record_paths(harness: Harness) -> None:
    _populate(harness)
    source = next(harness.paths.metadata.rglob("sys_script_include/*/_meta.yaml")).parent
    duplicate = harness.paths.metadata / "global" / "v_plugin" / source.name
    shutil.copytree(source, duplicate)
    meta = yaml.safe_load((duplicate / "_meta.yaml").read_text())
    meta["sys_class_name"] = "v_plugin"
    meta["read_only"] = True
    (duplicate / "_meta.yaml").write_text(yaml.safe_dump(meta, sort_keys=True))

    index = InstanceIndex(harness.paths.search_index)
    summary = index.rebuild(harness.paths.metadata, harness.paths.root)

    assert summary["records"] == 6
    duplicates = [
        row for row in index.records()
        if row["sys_id"] == meta["sys_id"]
    ]
    assert {row["class"] for row in duplicates} == {"sys_script_include", "v_plugin"}


def test_docs_generation_and_narrative_preservation(harness: Harness) -> None:
    _populate(harness)
    generator = DocumentationGenerator(harness.paths, harness.config)
    result = generator.generate()
    assert "scopes/x_acme_hr.md" in result["pages"]
    page = (harness.paths.docs / "pages" / "scopes" / "x_acme_hr.md").read_text()
    assert "### Table `x_acme_hr_case`" in page
    assert "erDiagram" in page and "sys_user" in page
    assert "**Functions:** `assign`" in page
    assert "**Uses script includes:** `Notifier`" in page
    assert "| Assign on insert | x_acme_hr_case | after |" in page
    updates = (harness.paths.docs / "pages" / "update-sets.md").read_text()
    assert "## Update sets by state" in updates
    assert "HR sprint 1" not in updates
    assert "alice" not in updates
    artifacts = (harness.paths.docs / "pages" / "artifacts.md").read_text()
    assert "| Class | Name | Scope | Updated | Path |" in artifacts

    marker = '<!-- snagentic:narrative id="scope-x_acme_hr-purpose" -->\n'
    edited = page.replace(marker + page.split(marker)[1].split("<!--")[0],
                          marker + "Handles HR case intake.\n")
    (harness.paths.docs / "pages" / "scopes" / "x_acme_hr.md").write_text(edited)
    second = generator.generate()
    assert second["narrative_blocks_preserved"] >= 1
    regenerated = (harness.paths.docs / "pages" / "scopes" / "x_acme_hr.md").read_text()
    assert "Handles HR case intake." in regenerated
    assert (harness.paths.docs / "mkdocs.yml").is_file()


def test_docs_normalize_carriage_returns_in_table_cells(harness: Harness) -> None:
    scope = harness.fake.create_scope("x_acme_hr")
    harness.fake.insert(
        "sys_properties",
        {
            "name": "x_acme_hr.multiline",
            "type": "string",
            "description": "first line\r\nsecond line",
            "sys_scope": scope,
        },
    )
    harness.sync().fetch()
    MirrorRepository(harness.paths, harness.config.mirror_branch).integrate(message="m")

    generator = DocumentationGenerator(harness.paths, harness.config)
    generator.generate()
    generator.generate(check=True)

    page = (harness.paths.docs / "pages" / "scopes" / "x_acme_hr.md").read_text()
    assert "first line  second line" in page
    assert "\r" not in page
