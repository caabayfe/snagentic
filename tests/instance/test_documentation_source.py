from __future__ import annotations

import importlib.util
import json

import pytest
import yaml
from conftest import Harness

from snagentic.cli.main import build_parser
from snagentic.errors import ConfigurationError
from snagentic.instance.docs import DocumentationGenerator
from snagentic.instance.docsource import DocumentationSourceRepository


def _setup_valid_docs(harness: Harness) -> None:
    doc_dir = harness.paths.documentation
    (doc_dir / "capabilities").mkdir(parents=True, exist_ok=True)
    (doc_dir / "processes").mkdir(parents=True, exist_ok=True)
    (doc_dir / "guides").mkdir(parents=True, exist_ok=True)

    cap_md = """---
id: test-cap
title: Test Capability
summary: A test capability summary.
status: draft
owners:
  - Alice
audiences:
  - process_owners
processes:
  - test-proc
guides:
  - test-guide
evidence:
  - kind: table
    target: x_acme_case
    label: HR Case Table
---
## Description
This is a test capability.
"""
    (doc_dir / "capabilities" / "test-cap.md").write_text(cap_md, encoding="utf-8")

    proc_md = """---
id: test-proc
title: Test Process
summary: A test process summary.
status: draft
owners:
  - Bob
audiences:
  - process_owners
capabilities:
  - test-cap
trigger: Something happens.
actors:
  - Bob
steps:
  - id: start
    title: Start
    actor: Bob
    action: Start doing work
    next:
      - target: finish
        label: Ready
  - id: finish
    title: Finish
    actor: Bob
    action: Complete work
states:
  - id: new
    title: New
    next:
      - closed
  - id: closed
    title: Closed
interactions:
  - source: ServiceNow
    target: Notification service
    message: Send completion notice
---
## Process notes
Notes on the process.
"""
    (doc_dir / "processes" / "test-proc.md").write_text(proc_md, encoding="utf-8")

    guide_md = """---
id: test-guide
title: Test Guide
summary: A test guide summary.
status: draft
owners:
  - Charlie
audiences:
  - end_users
role: Operator
goal: Do the standard operating procedure.
processes:
  - test-proc
process_steps:
  - test-proc:start
steps:
  - title: Follow step
    instruction: Follow the steps.
---
## Additional guidance
Additional guide text.
"""
    (doc_dir / "guides" / "test-guide.md").write_text(guide_md, encoding="utf-8")

    manifest_yaml = """
capabilities:
  - test-cap
processes:
  - test-proc
guides:
  - test-guide
"""
    (doc_dir / "manifest.yaml").write_text(manifest_yaml, encoding="utf-8")


def _write_table_model(harness: Harness) -> None:
    table_model_dir = harness.paths.workspace / "model" / "tables"
    table_model_dir.mkdir(parents=True, exist_ok=True)
    table_yaml = """name: x_acme_case
label: HR Case
extends: []
fields: {}
behaviour:
  business_rules:
    - name: "My Rule"
      active: "true"
      customized: true
      when: before
      action_insert: "true"
      path: "metadata/sys_script/xyz"
"""
    (table_model_dir / "x_acme_case.yaml").write_text(table_yaml, encoding="utf-8")


def test_valid_markdown_front_matter_loading(harness: Harness) -> None:
    _setup_valid_docs(harness)
    repo = DocumentationSourceRepository(harness.paths.documentation)
    source = repo.load()

    assert not source.empty
    assert "test-cap" in source.capabilities
    assert "test-proc" in source.processes
    assert "test-guide" in source.guides

    cap = source.capabilities["test-cap"]
    assert cap.title == "Test Capability"
    assert cap.summary == "A test capability summary."
    assert cap.status == "draft"
    assert cap.owners == ["Alice"]
    assert cap.audiences == ["process_owners"]
    assert cap.processes == ["test-proc"]
    assert cap.guides == ["test-guide"]

    proc = source.processes["test-proc"]
    assert proc.title == "Test Process"
    assert proc.trigger == "Something happens."
    assert proc.actors == ["Bob"]
    assert len(proc.steps) == 2
    assert proc.steps[0].id == "start"
    assert proc.steps[1].id == "finish"
    assert proc.states[0].next == ["closed"]
    assert proc.interactions[0].target == "Notification service"

    guide = source.guides["test-guide"]
    assert guide.title == "Test Guide"
    assert guide.role == "Operator"
    assert guide.goal == "Do the standard operating procedure."
    assert guide.process_steps == ["test-proc:start"]


def test_invalid_cross_document_reference(harness: Harness) -> None:
    _setup_valid_docs(harness)
    cap_path = harness.paths.documentation / "capabilities" / "test-cap.md"
    content = cap_path.read_text(encoding="utf-8")
    cap_path.write_text(content.replace("- test-proc", "- nonexistent-proc"), encoding="utf-8")

    repo = DocumentationSourceRepository(harness.paths.documentation)
    with pytest.raises(ConfigurationError) as exc_info:
        repo.load()
    assert "unresolved process id 'nonexistent-proc'" in str(exc_info.value)


def test_reserved_index_document_id_is_rejected(harness: Harness) -> None:
    _setup_valid_docs(harness)
    path = harness.paths.documentation / "capabilities" / "test-cap.md"
    content = path.read_text(encoding="utf-8").replace("id: test-cap", "id: index")
    (path.parent / "index.md").write_text(content, encoding="utf-8")
    path.unlink()

    with pytest.raises(ConfigurationError, match="reserved"):
        DocumentationSourceRepository(harness.paths.documentation).load()


def test_invalid_guide_process_step_reference(harness: Harness) -> None:
    _setup_valid_docs(harness)
    guide_path = harness.paths.documentation / "guides" / "test-guide.md"
    content = guide_path.read_text(encoding="utf-8")
    guide_path.write_text(
        content.replace("test-proc:start", "test-proc:nonexistent"),
        encoding="utf-8",
    )

    repo = DocumentationSourceRepository(harness.paths.documentation)
    with pytest.raises(ConfigurationError) as exc_info:
        repo.load()
    assert "unresolved process step 'test-proc:nonexistent'" in str(exc_info.value)


def test_unreachable_process_step_graph(harness: Harness) -> None:
    _setup_valid_docs(harness)
    proc_path = harness.paths.documentation / "processes" / "test-proc.md"
    content = proc_path.read_text(encoding="utf-8")
    corrupted_content = content.replace(
        "  - id: finish\n    title: Finish\n    actor: Bob\n    action: Complete work",
        "  - id: finish\n    title: Finish\n    actor: Bob\n    action: Complete work\n"
        "  - id: unreachable-step\n    title: Unreachable\n    actor: Bob\n"
        "    action: Unreachable step"
    )
    proc_path.write_text(corrupted_content, encoding="utf-8")

    repo = DocumentationSourceRepository(harness.paths.documentation)
    with pytest.raises(ConfigurationError) as exc_info:
        repo.load()
    assert "unreachable" in str(exc_info.value)


def test_missing_process_step_target(harness: Harness) -> None:
    _setup_valid_docs(harness)
    proc_path = harness.paths.documentation / "processes" / "test-proc.md"
    content = proc_path.read_text(encoding="utf-8")
    proc_path.write_text(
        content.replace("target: finish", "target: nonexistent-step"),
        encoding="utf-8",
    )

    repo = DocumentationSourceRepository(harness.paths.documentation)
    with pytest.raises(ConfigurationError) as exc_info:
        repo.load()
    assert "process steps reference missing targets" in str(exc_info.value)


def test_scaffold_creates_starter_and_no_overwrite(harness: Harness) -> None:
    repo = DocumentationSourceRepository(harness.paths.documentation)
    path = repo.scaffold("capability", "test-scaffolded")
    assert path.is_file()
    assert "id: test-scaffolded" in path.read_text(encoding="utf-8")
    assert "test-scaffolded" in repo.load().capabilities

    with pytest.raises(ConfigurationError) as exc_info:
        repo.scaffold("capability", "test-scaffolded")
    assert "already exists" in str(exc_info.value)


def test_docs_cli_modes_parse() -> None:
    parser = build_parser()
    check = parser.parse_args(["instance", "-i", "dev", "docs", "check", "--strict"])
    assert check.docs_action == "check"
    assert check.strict is True

    scaffold = parser.parse_args(
        [
            "instance",
            "-i",
            "dev",
            "docs",
            "scaffold",
            "--type",
            "guide",
            "--id",
            "resolve-incident",
        ]
    )
    assert scaffold.doc_type == "guide"
    assert scaffold.doc_id == "resolve-incident"


def test_migrate_narratives_copies_blocks(harness: Harness) -> None:
    docs_pages_dir = harness.paths.docs / "pages"
    docs_pages_dir.mkdir(parents=True, exist_ok=True)
    page_path = docs_pages_dir / "some-page.md"
    page_path.write_text(
        "Some introduction.\n"
        '<!-- snagentic:narrative id="scope-x_acme-purpose" -->\n'
        "Our purpose description here.\n"
        "<!-- /snagentic:narrative -->\n",
        encoding="utf-8",
    )

    generator = DocumentationGenerator(harness.paths, harness.config)
    result = generator.migrate()

    assert result["narratives"] >= 1
    legacy_path = harness.paths.documentation / "legacy-narratives.yaml"
    assert legacy_path.is_file()

    content = yaml.safe_load(legacy_path.read_text(encoding="utf-8"))
    assert content["scope-x_acme-purpose"] == "Our purpose description here.\n"


def test_generator_renders_site_and_verification_checks(harness: Harness) -> None:
    _setup_valid_docs(harness)
    _write_table_model(harness)

    generator = DocumentationGenerator(harness.paths, harness.config)
    result = generator.generate()

    assert "capabilities/test-cap.md" in result["pages"]
    assert "processes/test-proc.md" in result["pages"]
    assert "guides/test-guide.md" in result["pages"]
    assert "capabilities/index.md" in result["pages"]
    assert "processes/index.md" in result["pages"]
    assert "guides/index.md" in result["pages"]
    assert "governance.md" in result["pages"]
    assert "tables/x_acme_case.md" in result["pages"]

    proc_page = (
        harness.paths.docs / "pages" / "processes" / "test-proc.md"
    ).read_text(encoding="utf-8")
    assert "```mermaid" in proc_page
    assert "flowchart TD" in proc_page
    assert 'n_start["Start<br/>Bob"]' in proc_page
    assert "n_start -->|Ready| n_finish" in proc_page
    assert "stateDiagram-v2" in proc_page
    assert "sequenceDiagram" in proc_page
    assert "participant participant_1 as Notification service" in proc_page
    assert "participant participant_2 as ServiceNow" in proc_page

    cap_page = (
        harness.paths.docs / "pages" / "capabilities" / "test-cap.md"
    ).read_text(encoding="utf-8")
    assert "## Technical evidence" in cap_page
    assert "[HR Case Table](../evidence/table-x-acme-case-" in cap_page

    evidence_index = (
        harness.paths.docs / "pages" / "evidence" / "index.md"
    ).read_text(encoding="utf-8")
    assert "`model/tables/x_acme_case.yaml`" in evidence_index

    gov_page = (harness.paths.docs / "pages" / "governance.md").read_text(encoding="utf-8")
    assert "# Documentation governance" in gov_page
    assert "test-cap" in gov_page
    assert "test-proc" in gov_page
    assert "test-guide" in gov_page

    assert (harness.paths.docs / "documentation-manifest.json").is_file()

    # Verify check succeeds on up-to-date documentation
    generator.generate(check=True)

    # Verify check fails after a generated page is modified
    edited_page = harness.paths.docs / "pages" / "governance.md"
    edited_page.write_text(
        edited_page.read_text(encoding="utf-8") + "\nStale text.",
        encoding="utf-8",
    )

    with pytest.raises(ConfigurationError) as exc_info:
        generator.generate(check=True)
    assert "documentation check failed" in str(exc_info.value)


@pytest.mark.skipif(importlib.util.find_spec("mkdocs") is None, reason="docs extra not installed")
def test_generator_runs_strict_mkdocs_build(harness: Harness) -> None:
    _setup_valid_docs(harness)
    _write_table_model(harness)
    generator = DocumentationGenerator(harness.paths, harness.config)
    generator.generate(strict=True)
    assert (harness.paths.state / "site" / "index.html").is_file()


def test_generator_reuses_single_metadata_walk_for_curated_evidence(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    _setup_valid_docs(harness)
    _write_table_model(harness)
    model = harness.paths.workspace / "model"
    (model / "customer-updates.yaml").write_text(
        "customer_updates: []\n",
        encoding="utf-8",
    )
    generator = DocumentationGenerator(harness.paths, harness.config)
    original = generator._record_directories
    calls = 0

    def counted_record_directories():
        nonlocal calls
        calls += 1
        return original()

    monkeypatch.setattr(generator, "_record_directories", counted_record_directories)

    result = generator.generate()

    assert result["authored_documents"] == 3
    assert calls == 1


def test_missing_artifact_file_is_not_resolved_to_parent_record(harness: Harness) -> None:
    _setup_valid_docs(harness)
    record = (
        harness.paths.metadata
        / "global"
        / "sys_script"
        / "example--0123456789abcdef0123456789abcdef"
    )
    record.mkdir(parents=True)
    (record / "_meta.yaml").write_text(
        "sys_id: 0123456789abcdef0123456789abcdef\n"
        "sys_class_name: sys_script\n"
        "scope: global\n",
        encoding="utf-8",
    )
    (record / "record.yaml").write_text("name: Example\n", encoding="utf-8")
    capability = harness.paths.documentation / "capabilities" / "test-cap.md"
    content = capability.read_text(encoding="utf-8").replace(
        "  - kind: table\n    target: x_acme_case\n    label: HR Case Table",
        "  - kind: artifact\n"
        "    target: metadata/global/sys_script/"
        "example--0123456789abcdef0123456789abcdef/missing.js",
    )
    capability.write_text(content, encoding="utf-8")

    result = DocumentationGenerator(harness.paths, harness.config).generate()
    assert any(
        finding["code"] == "evidence_missing" and finding["document"] == "test-cap"
        for finding in result["findings"]
    )


def test_check_rejects_overdue_approved_document(harness: Harness) -> None:
    _setup_valid_docs(harness)
    _write_table_model(harness)
    capability = harness.paths.documentation / "capabilities" / "test-cap.md"
    content = capability.read_text(encoding="utf-8").replace(
        "status: draft",
        "status: approved\nreviewed_on: 2000-01-01\nreview_interval_days: 30",
    )
    capability.write_text(content, encoding="utf-8")
    generator = DocumentationGenerator(harness.paths, harness.config)
    generator.generate()

    with pytest.raises(ConfigurationError, match="review was due on 2000-01-31"):
        generator.generate(check=True)


def test_artifact_fingerprint_includes_exploded_record_files(harness: Harness) -> None:
    _setup_valid_docs(harness)
    _write_table_model(harness)
    record = (
        harness.paths.metadata
        / "global"
        / "sys_script"
        / "example--0123456789abcdef0123456789abcdef"
    )
    record.mkdir(parents=True)
    (record / "_meta.yaml").write_text(
        "sys_id: 0123456789abcdef0123456789abcdef\n"
        "sys_class_name: sys_script\n"
        "scope: global\n"
        "files:\n"
        "  script: script.js\n",
        encoding="utf-8",
    )
    (record / "record.yaml").write_text("name: Example\n", encoding="utf-8")
    script = record / "script.js"
    script.write_text("answer = 1;\n", encoding="utf-8")
    capability = harness.paths.documentation / "capabilities" / "test-cap.md"
    content = capability.read_text(encoding="utf-8").replace(
        "  - kind: table\n    target: x_acme_case\n    label: HR Case Table",
        "  - kind: artifact\n"
        "    target: metadata/global/sys_script/"
        "example--0123456789abcdef0123456789abcdef/record.yaml",
    )
    capability.write_text(content, encoding="utf-8")
    generator = DocumentationGenerator(harness.paths, harness.config)

    generator.generate()
    manifest_path = harness.paths.docs / "documentation-manifest.json"
    before = json.loads(manifest_path.read_text())["documents"]["test-cap"]["fingerprint"]
    script.write_text("answer = 2;\n", encoding="utf-8")
    generator.generate()
    after = json.loads(manifest_path.read_text())["documents"]["test-cap"]["fingerprint"]

    assert before != after
