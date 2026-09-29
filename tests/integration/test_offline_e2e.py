from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from snagentic.errors import ConflictError, PolicyDeniedError, ServiceNowError


def _tree_bytes(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def _scoped_source(root: Path) -> Path:
    return (
        root
        / "servicenow"
        / "domains"
        / "emea"
        / "scopes"
        / "x_fictional_kb"
        / "script_include"
        / "scoped-utility"
        / "source.js"
    )


def test_domain_context_inventory_and_deterministic_pull(
    offline_cli: Any,
) -> None:
    companion = offline_cli.companion()
    assert companion.domains() == [
        {
            "stable_id": "emea",
            "sys_id": "dddddddddddddddddddddddddddddddd",
            "name": "Fictional EMEA",
            "path": "/Fictional/EMEA/",
            "parent": "global",
        }
    ]
    assert {
        (
            context["domain"]["sys_id"],
            context["application_scope"]["scope"],
        )
        for context in companion.contexts()
    } == {
        ("global", "x_fictional_kb"),
        ("emea", "global"),
        ("emea", "x_fictional_kb"),
    }

    inventory = offline_cli.invoke("inventory")
    assert len(inventory["artifacts"]) == 3
    assert {
        (
            item["domain"]["sys_id"],
            item["application_scope"]["scope"],
            item["artifact_type"],
        )
        for item in inventory["artifacts"]
    } == {
        ("global", "x_fictional_kb", "script_include"),
        ("emea", "global", "business_rule"),
        ("emea", "x_fictional_kb", "script_include"),
    }

    first = offline_cli.invoke("pull")
    workspace = offline_cli.root / "servicenow"
    assert first == {"artifact_count": 3, "domain_count": 1, "cursor": None}
    assert (
        workspace / "global/script_include/global-utility/source.js"
    ).is_file()
    assert (
        workspace / "domains/emea/business_rule/domain-rule/source.js"
    ).is_file()
    assert _scoped_source(offline_cli.root).is_file()
    first_tree = _tree_bytes(workspace)

    second = offline_cli.invoke("pull")
    assert second == first
    assert _tree_bytes(workspace) == first_tree
    assert offline_cli.invoke("status") == {
        "added": [],
        "deleted": [],
        "modified": [],
    }


def test_local_edit_builds_bundle_and_successful_push_refreshes_baseline(
    offline_cli: Any,
) -> None:
    offline_cli.invoke("pull")
    source = _scoped_source(offline_cli.root)
    edited = (
        "var ScopedUtility = Class.create();\n"
        "ScopedUtility.prototype = {answer: 42, type: 'ScopedUtility'};\n"
    )
    source.write_text(edited, encoding="utf-8")

    plan = offline_cli.invoke("push-plan")
    assert offline_cli.invoke("push-plan") == plan
    assert len(plan["bundle_id"]) == 32
    assert plan["changes"] == [
        {
            "operation": "update",
            "artifact_type": "script_include",
            "sys_id": "33333333333333333333333333333333",
            "domain": {"sys_id": "emea"},
            "application_scope": {
                "sys_id": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
            },
            "expected": {
                "sys_mod_count": 2,
                "revision": "2026-09-18 10:17:00:2",
                "hash": (
                    "2123456789abcdef0123456789abcdef"
                    "0123456789abcdef0123456789abcdef"
                ),
            },
            "values": {
                "name": "ScopedUtility",
                "active": True,
                "script": edited,
            },
        }
    ]

    result = offline_cli.invoke("push", "--approve")
    assert result["applied"] is True
    assert result["bundle_id"] == plan["bundle_id"]
    assert offline_cli.backend.preflight_bundles == [plan]
    assert offline_cli.backend.applied_bundles == [plan]
    assert source.read_text(encoding="utf-8") == edited
    baseline_source = (
        offline_cli.root
        / ".snagentic"
        / "baselines"
        / "latest"
        / source.relative_to(offline_cli.root / "servicenow")
    )
    assert baseline_source.read_text(encoding="utf-8") == edited
    assert offline_cli.invoke("diff") == {
        "added": [],
        "deleted": [],
        "modified": [],
    }
    assert not (offline_cli.root / ".snagentic/reconciliation").exists()


def test_partial_apply_persists_marker_and_blocks_replay_until_pull(
    offline_cli: Any,
) -> None:
    offline_cli.invoke("pull")
    source = _scoped_source(offline_cli.root)
    original = source.read_text(encoding="utf-8")
    source.write_text(original.replace("type:", "changed: true, type:"), encoding="utf-8")
    plan = offline_cli.invoke("push-plan")
    offline_cli.backend.fail_apply = True

    with pytest.raises(
        ServiceNowError,
        match="Artifact changed after the bundle was prepared",
    ):
        offline_cli.invoke("push", "--approve")

    marker_path = (
        offline_cli.root
        / ".snagentic"
        / "reconciliation"
        / f"{plan['bundle_id']}.json"
    )
    marker = json.loads(marker_path.read_text(encoding="utf-8"))
    assert marker == {
        "bundle_id": plan["bundle_id"],
        "status": "reconciliation_required",
        "error_code": "concurrency_conflict",
        "status_code": 409,
        "details": [
            {
                "failed_index": 1,
                "completed_count": 1,
                "completed": [
                    {
                        "index": 0,
                        "operation": "update",
                        "artifact_type": "script_include",
                        "sys_id": "33333333333333333333333333333333",
                    }
                ],
            }
        ],
    }
    assert offline_cli.backend.apply_count == 1

    offline_cli.backend.fail_apply = False
    with pytest.raises(ConflictError, match="successful pull"):
        offline_cli.invoke("push", "--approve")
    assert offline_cli.backend.apply_count == 1
    assert marker_path.is_file()

    offline_cli.invoke("pull")
    assert not marker_path.exists()
    assert source.read_text(encoding="utf-8") == original
    assert offline_cli.invoke("status") == {
        "added": [],
        "deleted": [],
        "modified": [],
    }


@pytest.mark.parametrize("environment", ["test", "prod"])
def test_push_is_denied_outside_development(
    offline_cli: Any,
    environment: str,
) -> None:
    offline_cli.invoke("pull")
    source = _scoped_source(offline_cli.root)
    source.write_text(
        source.read_text(encoding="utf-8").replace("type:", "changed: true, type:"),
        encoding="utf-8",
    )

    with pytest.raises(PolicyDeniedError, match="writes are denied"):
        offline_cli.invoke("push", "--approve", environment=environment)

    assert offline_cli.backend.preflight_bundles == []
    assert offline_cli.backend.apply_count == 0


def test_diagnostics_are_saved_only_under_local_state(
    offline_cli: Any,
) -> None:
    result = offline_cli.invoke(
        "diagnostics",
        "--minutes",
        "15",
        "--limit",
        "25",
        "--domain",
        "emea",
    )
    path = offline_cli.root / result["path"]
    diagnostics_directory = offline_cli.root / ".snagentic" / "diagnostics"
    assert path.parent.resolve() == diagnostics_directory.resolve()
    assert path.is_file()
    assert json.loads(path.read_text(encoding="utf-8")) == (
        offline_cli.backend.fixture["diagnostics"]
    )
    assert offline_cli.backend.diagnostic_queries == [
        {"minutes": 15, "limit": 25, "domain": "emea"}
    ]
    assert list(offline_cli.root.rglob("diagnostics-*.json")) == [path]
    assert not (offline_cli.root / "servicenow").exists()


def test_pull_rejects_duplicate_artifact_paths_without_publishing_workspace(
    offline_cli: Any,
) -> None:
    offline_cli.backend.duplicate_first_artifact_path()

    with pytest.raises(
        ValueError,
        match="multiple ServiceNow artifacts resolved to the same path",
    ):
        offline_cli.invoke("pull")

    assert not (offline_cli.root / "servicenow").exists()
    assert not (offline_cli.root / ".snagentic/baselines/latest").exists()
