from snagentic.artifacts import (
    DEFAULT_ARTIFACT_REGISTRY,
    ArtifactCapability,
    canonical_json,
    content_hash,
    normalize_line_endings,
    normalize_mapping,
)


def test_registry_contains_required_allowlist() -> None:
    expected = {
        "script_include",
        "business_rule",
        "acl",
        "dictionary",
        "system_property",
        "client_script",
        "ui_action",
        "ui_policy",
        "scripted_rest_api",
        "scripted_rest_resource",
        "scheduled_job",
        "notification",
        "flow",
        "subflow",
        "update_set",
        "app_version",
        "deployment_history",
    }
    assert {definition.artifact_type for definition in DEFAULT_ARTIFACT_REGISTRY} == expected
    properties = DEFAULT_ARTIFACT_REGISTRY.require("system_property")
    assert properties.capability == ArtifactCapability.EXPORT_ONLY
    assert "value" in properties.excluded_fields
    assert DEFAULT_ARTIFACT_REGISTRY.require("script_include").natural_key_fields == (
        "api_name",
    )


def test_normalization_is_deterministic_without_destroying_list_order() -> None:
    left = {"z": None, "a": {"b": "one\r\ntwo", "a": [2, 1]}}
    right = {"a": {"a": [2, 1], "b": "one\ntwo"}, "z": None}
    assert normalize_mapping(left) == normalize_mapping(right)
    assert canonical_json(left) == canonical_json(right)
    assert content_hash(left) == content_hash(right)


def test_normalization_can_omit_mapping_nulls_and_normalizes_text() -> None:
    assert canonical_json({"b": None, "a": [None]}, omit_nulls=True) == '{"a":[null]}'
    assert normalize_line_endings("one\r\ntwo\r") == "one\ntwo\n"
