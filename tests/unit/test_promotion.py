from __future__ import annotations

import hashlib
import json
from dataclasses import FrozenInstanceError
from pathlib import Path
from typing import Any

import pytest

from snagentic.changes import (
    build_change_report,
    build_promotion_manifest,
    canonical_json,
    sanitize_change_plan,
    validate_change_plan,
)
from snagentic.cli.main import main, run

CREATED_AT = "2026-09-20T19:00:00Z"
GIT_COMMIT = "0123456789abcdef0123456789abcdef01234567"


def _source_bundle(changes: list[dict[str, Any]]) -> str:
    encoded = json.dumps(changes, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()[:32]


def _plan(changes: list[dict[str, Any]]) -> dict[str, Any]:
    return {"bundle_id": _source_bundle(changes), "changes": changes}


@pytest.fixture
def realistic_plan() -> dict[str, Any]:
    changes = [
        {
            "operation": "update",
            "artifact_type": "script_include",
            "sys_id": "11111111111111111111111111111111",
            "domain": {"sys_id": "customer-a"},
            "application_scope": {"sys_id": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"},
            "expected": {
                "sys_mod_count": 3,
                "revision": "2026-09-20 10:00:00:3",
                "hash": "1" * 64,
            },
            "values": {
                "name": "hello",
                "active": True,
                "script": "function hello() { return false; }",
                "nested": {
                    "Client_Secret": "never emit",
                    "safe": [{"PRIVATE-KEY": "never emit"}],
                },
            },
        },
        {
            "operation": "create",
            "artifact_type": "business_rule",
            "domain": {"sys_id": "global"},
            "application_scope": {"sys_id": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"},
            "values": {
                "name": "Created Rule",
                "script": "current.active = true;",
                "work_notes": "never emit",
            },
        },
        {
            "operation": "delete",
            "artifact_type": "script_include",
            "sys_id": "22222222222222222222222222222222",
            "domain": {"sys_id": "customer-b"},
            "application_scope": {"sys_id": "cccccccccccccccccccccccccccccccc"},
            "expected": {
                "sys_mod_count": 8,
                "revision": "2026-09-20 11:00:00:8",
                "hash": "2" * 64,
            },
        },
    ]
    return _plan(changes)


def _manifest(plan: dict[str, Any], **overrides: str) -> Any:
    inputs = {
        "source_kind": "development",
        "target_kind": "test",
        "mechanism": "update_set",
        "created_at": CREATED_AT,
        "git_commit": GIT_COMMIT,
    }
    inputs.update(overrides)
    return build_promotion_manifest(plan, **inputs)


def _report(plan: dict[str, Any], **overrides: str) -> Any:
    inputs = {
        "source_kind": "development",
        "target_kind": "test",
        "mechanism": "update_set",
        "created_at": CREATED_AT,
        "git_commit": GIT_COMMIT,
    }
    inputs.update(overrides)
    return build_change_report(plan, **inputs)


def test_manifest_is_deterministic_canonical_and_stably_ordered(
    realistic_plan: dict[str, Any],
) -> None:
    first = _manifest(realistic_plan)
    second = _manifest(json.loads(json.dumps(realistic_plan)))
    assert first == second
    assert canonical_json(first.as_dict()) == canonical_json(second.as_dict())
    operation_encodings = [
        canonical_json(operation) for operation in first.as_dict()["operations"]
    ]
    assert operation_encodings == sorted(operation_encodings)
    material = dict(first.as_dict())
    del material["kind"]
    del material["promotion_bundle_id"]
    assert first.promotion_bundle_id == hashlib.sha256(
        canonical_json(material).encode()
    ).hexdigest()


def test_mapping_insertion_order_does_not_change_hashes(
    realistic_plan: dict[str, Any],
) -> None:
    original = realistic_plan["changes"][0]["values"]
    reordered = {key: original[key] for key in reversed(original)}
    changed = json.loads(json.dumps(realistic_plan))
    changed["changes"][0]["values"] = reordered
    changed["bundle_id"] = _source_bundle(changed["changes"])
    first = _manifest(realistic_plan)
    second = _manifest(changed)
    assert first.operations == second.operations
    assert first.source_changes_sha256 == second.source_changes_sha256
    assert first.promotion_bundle_id == second.promotion_bundle_id


def test_models_are_deeply_immutable(realistic_plan: dict[str, Any]) -> None:
    manifest = _manifest(realistic_plan)
    with pytest.raises(FrozenInstanceError):
        manifest.target_kind = "production"
    with pytest.raises(FrozenInstanceError):
        manifest.operations[0].artifact_type = "changed"
    assert isinstance(manifest.operations, tuple)


def test_created_at_is_explicit_normalized_and_part_of_identity(
    realistic_plan: dict[str, Any],
) -> None:
    normalized = _manifest(
        realistic_plan, created_at="2026-09-20T19:00:00+00:00"
    )
    assert normalized.created_at == CREATED_AT
    later = _manifest(realistic_plan, created_at="2026-09-20T19:00:01Z")
    assert normalized.promotion_bundle_id != later.promotion_bundle_id
    for invalid in (
        "2026-09-20T19:00:00",
        "2026-09-20T21:00:00+02:00",
        "not-a-timestamp",
    ):
        with pytest.raises(ValueError, match="RFC3339 UTC"):
            _manifest(realistic_plan, created_at=invalid)


def test_git_commit_is_optional_absent_from_output_and_hash_material(
    realistic_plan: dict[str, Any],
) -> None:
    first = build_promotion_manifest(
        realistic_plan,
        source_kind="development",
        target_kind="test",
        mechanism="update_set",
        created_at=CREATED_AT,
    )
    second = build_promotion_manifest(
        json.loads(json.dumps(realistic_plan)),
        source_kind="development",
        target_kind="test",
        mechanism="update_set",
        created_at=CREATED_AT,
        git_commit=None,
    )
    report = build_change_report(
        realistic_plan,
        source_kind="development",
        target_kind="test",
        mechanism="update_set",
        created_at=CREATED_AT,
    )

    assert first == second
    assert first.git_commit is None
    assert report.git_commit is None
    assert "git_commit" not in first.as_dict()
    assert "git_commit" not in report.as_dict()

    material = dict(first.as_dict())
    del material["kind"]
    del material["promotion_bundle_id"]
    assert "git_commit" not in canonical_json(material)
    assert first.promotion_bundle_id == hashlib.sha256(
        canonical_json(material).encode()
    ).hexdigest()


def test_supplied_git_commit_is_normalized_emitted_and_changes_identity(
    realistic_plan: dict[str, Any],
) -> None:
    omitted = build_promotion_manifest(
        realistic_plan,
        source_kind="development",
        target_kind="test",
        mechanism="update_set",
        created_at=CREATED_AT,
    )
    supplied = _manifest(realistic_plan, git_commit=GIT_COMMIT.upper())

    assert supplied.git_commit == GIT_COMMIT
    assert supplied.as_dict()["git_commit"] == GIT_COMMIT
    assert supplied.promotion_bundle_id != omitted.promotion_bundle_id


@pytest.mark.parametrize("mechanism", ["application", "update_set", "servicenow_cicd"])
def test_all_supported_mechanisms(
    realistic_plan: dict[str, Any], mechanism: str
) -> None:
    assert _manifest(realistic_plan, mechanism=mechanism).deployment_mechanism == mechanism


@pytest.mark.parametrize("mechanism", ["direct_write", "push", "api", "update-set"])
def test_invalid_and_direct_write_mechanisms_are_rejected(
    realistic_plan: dict[str, Any], mechanism: str
) -> None:
    with pytest.raises(ValueError, match="deployment mechanism"):
        _manifest(realistic_plan, mechanism=mechanism)


@pytest.mark.parametrize("source", ["dev", "test", "production", "Development"])
def test_source_must_be_exactly_development(
    realistic_plan: dict[str, Any], source: str
) -> None:
    with pytest.raises(ValueError, match="source kind"):
        _manifest(realistic_plan, source_kind=source)


@pytest.mark.parametrize("target", ["development", "dev", "stage", "Test"])
def test_target_must_be_test_or_production(
    realistic_plan: dict[str, Any], target: str
) -> None:
    with pytest.raises(ValueError, match="target kind"):
        _manifest(realistic_plan, target_kind=target)


def test_direct_write_flags_in_plan_are_rejected(realistic_plan: dict[str, Any]) -> None:
    for flag_value in (True, False):
        realistic_plan["metadata"] = {"Allow-Direct_Write": flag_value}
        with pytest.raises(ValueError, match="direct-write"):
            validate_change_plan(realistic_plan)


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (("write",), True),
        (("Apply",), True),
        (("APPROVED",), True),
        (("deploy",), True),
        (("metadata", "execute"), True),
        (("metadata", "apply-changes"), True),
        (("metadata", "deployment_approved"), False),
        (("metadata", "Deployment-Method"), "DIRECT_WRITE"),
        (("metadata", "deployment_method"), "direct-write"),
        (("metadata", "Deployment_Mode"), "direct_deployment"),
        (("metadata", "deployMechanism"), "REST_API"),
        (("metadata", "promotion_method"), "push"),
        (("metadata", "nested", "Allow_Direct-Write"), False),
    ],
)
def test_execution_directives_and_direct_write_aliases_are_rejected_recursively(
    realistic_plan: dict[str, Any],
    path: tuple[str, ...],
    value: Any,
) -> None:
    nested: dict[str, Any] = realistic_plan
    for key in path[:-1]:
        child: dict[str, Any] = {}
        nested[key] = child
        nested = child
    nested[path[-1]] = value

    with pytest.raises(ValueError, match="direct-write|apply|deploy"):
        validate_change_plan(realistic_plan)


def test_normal_change_operations_are_not_confused_with_deployment_actions(
    realistic_plan: dict[str, Any],
) -> None:
    validated = validate_change_plan(realistic_plan)
    assert {operation.operation for operation in validated.operations} == {
        "create",
        "update",
        "delete",
    }


def test_missing_and_malformed_provenance_is_rejected(
    realistic_plan: dict[str, Any],
) -> None:
    for bundle_id in (None, "", "abc", "g" * 32):
        candidate = dict(realistic_plan)
        if bundle_id is None:
            candidate.pop("bundle_id")
        else:
            candidate["bundle_id"] = bundle_id
        with pytest.raises(ValueError, match="bundle_id"):
            validate_change_plan(candidate)


def test_tampered_source_bundle_identity_is_rejected(
    realistic_plan: dict[str, Any],
) -> None:
    realistic_plan["changes"][0]["expected"]["hash"] = "3" * 64
    with pytest.raises(ValueError, match="does not match"):
        validate_change_plan(realistic_plan)


@pytest.mark.parametrize("operation_index", [0, 2])
def test_update_and_delete_require_valid_expected_hash(
    realistic_plan: dict[str, Any], operation_index: int
) -> None:
    change = realistic_plan["changes"][operation_index]
    change["expected"].pop("hash")
    realistic_plan["bundle_id"] = _source_bundle(realistic_plan["changes"])
    with pytest.raises(ValueError, match="hash"):
        validate_change_plan(realistic_plan)

    change["expected"]["hash"] = "not-a-hash"
    realistic_plan["bundle_id"] = _source_bundle(realistic_plan["changes"])
    with pytest.raises(ValueError, match="SHA-256"):
        validate_change_plan(realistic_plan)


@pytest.mark.parametrize("operation_index", [0, 1])
def test_create_and_update_require_values_for_derived_artifact_hash(
    realistic_plan: dict[str, Any], operation_index: int
) -> None:
    realistic_plan["changes"][operation_index].pop("values")
    realistic_plan["bundle_id"] = _source_bundle(realistic_plan["changes"])
    with pytest.raises(ValueError, match="values"):
        validate_change_plan(realistic_plan)


def test_declared_artifact_hash_must_match_values(
    realistic_plan: dict[str, Any],
) -> None:
    realistic_plan["changes"][1]["artifact_hash"] = "f" * 64
    realistic_plan["bundle_id"] = _source_bundle(realistic_plan["changes"])
    with pytest.raises(ValueError, match="does not match"):
        validate_change_plan(realistic_plan)


def test_manifest_and_report_never_contain_payloads_or_secrets(
    realistic_plan: dict[str, Any],
) -> None:
    for output in (_manifest(realistic_plan).as_dict(), _report(realistic_plan).as_dict()):
        encoded = json.dumps(output, sort_keys=True).casefold()
        for forbidden_value in (
            "function hello",
            "current.active",
            "never emit",
            "created rule",
        ):
            assert forbidden_value not in encoded
        for key in (
            '"values"',
            '"content"',
            '"script"',
            '"work_notes"',
            '"client_secret"',
            '"private-key"',
            '"password"',
            '"token"',
        ):
            assert key not in encoded


def test_recursive_sanitizer_handles_case_variants_and_lists() -> None:
    assert sanitize_change_plan(
        {
            "safe": "kept",
            "VALUES": {"name": "removed"},
            "nested": [
                {
                    "Source": "removed",
                    "source_payload": "removed",
                    "api-Key": "removed",
                    "Work_Notes": "removed",
                    "identifier": "kept",
                }
            ],
        }
    ) == {"safe": "kept", "nested": [{"identifier": "kept"}]}


def test_report_is_pr_friendly_and_tied_to_manifest(
    realistic_plan: dict[str, Any],
) -> None:
    manifest = _manifest(realistic_plan)
    report = _report(realistic_plan)
    assert report.promotion_bundle_id == manifest.promotion_bundle_id
    assert report.as_dict()["summary"] == {
        "total": 3,
        "create": 1,
        "update": 1,
        "delete": 1,
    }
    assert report.operations == manifest.operations


@pytest.mark.parametrize("command", ["promotion-manifest", "change-report"])
def test_offline_cli_commands_use_existing_json_wrapper_without_config_or_clients(
    tmp_path: Path,
    realistic_plan: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    command: str,
) -> None:
    plan_path = tmp_path / "plan.json"
    plan_path.write_text(json.dumps(realistic_plan), encoding="utf-8")

    def unexpected_call(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("offline command attempted config, client, or network access")

    monkeypatch.setattr("snagentic.cli.main._load_online_runtime", unexpected_call)
    main(
        [
            "--json",
            command,
            str(plan_path),
            "--source",
            "development",
            "--target",
            "test",
            "--mechanism",
            "application",
            "--created-at",
            CREATED_AT,
            "--git-commit",
            GIT_COMMIT,
        ]
    )
    wrapper = json.loads(capsys.readouterr().out)
    assert wrapper["ok"] is True
    assert wrapper["result"]["kind"] == f"snagentic_{command.replace('-', '_')}"


@pytest.mark.parametrize("command", ["promotion-manifest", "change-report"])
def test_offline_cli_omits_optional_git_commit(
    tmp_path: Path,
    realistic_plan: dict[str, Any],
    capsys: pytest.CaptureFixture[str],
    command: str,
) -> None:
    plan_path = tmp_path / "plan.json"
    plan_path.write_text(json.dumps(realistic_plan), encoding="utf-8")

    main(
        [
            "--json",
            command,
            str(plan_path),
            "--source",
            "development",
            "--target",
            "test",
            "--mechanism",
            "application",
            "--created-at",
            CREATED_AT,
        ]
    )

    result = json.loads(capsys.readouterr().out)["result"]
    assert "git_commit" not in result


def test_run_does_not_need_global_json_flag(
    tmp_path: Path, realistic_plan: dict[str, Any]
) -> None:
    plan_path = tmp_path / "plan.json"
    plan_path.write_text(json.dumps(realistic_plan), encoding="utf-8")
    result = run(
        __import__("argparse").Namespace(
            command="promotion-manifest",
            plan=plan_path,
            source_kind="development",
            target_kind="production",
            mechanism="servicenow_cicd",
            created_at=CREATED_AT,
            git_commit=GIT_COMMIT,
        )
    )
    assert result["target_kind"] == "production"
