import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CONSTANTS = (
    ROOT / "servicenow" / "app" / "src" / "script_includes" / "SnSourceConstants.js"
)
VALIDATOR = (
    ROOT / "servicenow" / "app" / "src" / "script_includes" / "SnSourceValidator.js"
)
FIXTURES = ROOT / "tests" / "fixtures" / "servicenow"

EXPECTED_TYPES = {
    "acl": ("sys_security_acl", "name", "managed_bidirectional"),
    "business_rule": ("sys_script", "name", "managed_bidirectional"),
    "client_script": ("sys_script_client", "name", "managed_bidirectional"),
    "dictionary": ("sys_dictionary", "element", "managed_bidirectional"),
    "notification": ("sysevent_email_action", "name", "export_only"),
    "scheduled_job": ("sysauto_script", "name", "export_only"),
    "script_include": ("sys_script_include", "name", "managed_bidirectional"),
    "scripted_rest_api": ("sys_ws_definition", "name", "managed_bidirectional"),
    "scripted_rest_resource": (
        "sys_ws_operation",
        "name",
        "managed_bidirectional",
    ),
    "system_property": ("sys_properties", "name", "export_only"),
    "ui_action": ("sys_ui_action", "name", "managed_bidirectional"),
    "ui_policy": ("sys_ui_policy", "short_description", "managed_bidirectional"),
}

FORBIDDEN_FIELD_PATTERN = re.compile(
    r"(^|_)(password|passwd|credential|secret|token|api_key|private_key|"
    r"authorization|auth_header|cookie|session|encryption|encrypted|journal|"
    r"comments|work_notes|request_body|response_body|attachment)(_|$)",
    re.IGNORECASE,
)


def test_companion_artifact_allowlist_matches_supported_registry_subset() -> None:
    definitions = _artifact_definitions()

    assert set(definitions) == set(EXPECTED_TYPES)
    for artifact_type, (table, name_field, category) in EXPECTED_TYPES.items():
        definition = definitions[artifact_type]
        assert definition["table"] == table
        assert definition["nameField"] == name_field
        assert definition["capabilityCategory"] == category
        assert definition["readableFields"]
        assert set(definition["writableFields"]) <= set(definition["readableFields"])


def test_export_only_types_have_no_writable_fields() -> None:
    definitions = _artifact_definitions()

    for artifact_type in {"notification", "scheduled_job", "system_property"}:
        assert definitions[artifact_type]["capabilityCategory"] == "export_only"
        assert definitions[artifact_type]["writableFields"] == []


def test_change_validator_rejects_export_only_operations_before_delete_handling() -> None:
    definitions = _artifact_definitions()
    source = VALIDATOR.read_text(encoding="utf-8")
    function_start = source.index("_validateChange: function(change) {")
    body = _balanced_body(source, source.index("{", function_start))
    capability_guard = re.search(
        r"if\s*\(\s*config\.capabilityCategory\s*!==\s*"
        r"'managed_bidirectional'\s*\)\s*\{.*?"
        r"throw\s+this\.security\.error\(\s*'artifact_not_writable'",
        body,
        re.DOTALL,
    )

    assert capability_guard
    assert capability_guard.start() < body.index("change.operation === 'create'")
    assert capability_guard.start() < body.index("change.operation === 'delete'")
    assert {
        artifact_type
        for artifact_type, definition in definitions.items()
        if definition["capabilityCategory"] != "managed_bidirectional"
    } == {"notification", "scheduled_job", "system_property"}


def test_allowlisted_fields_exclude_sensitive_and_unbounded_content() -> None:
    definitions = _artifact_definitions()

    for artifact_type, definition in definitions.items():
        fields = definition["readableFields"] + definition["writableFields"]
        for field in fields:
            assert not FORBIDDEN_FIELD_PATTERN.search(field), (artifact_type, field)

    properties = definitions["system_property"]
    assert "value" not in properties["readableFields"]
    assert "value" not in properties["writableFields"]


def test_capabilities_fixture_exposes_companion_type_category_map() -> None:
    definitions = _artifact_definitions()
    payload = _fixture("responses/capabilities.json")
    actual = payload["data"]["artifact_types"]
    expected = [
        {"key": key, "category": definitions[key]["capabilityCategory"]}
        for key in sorted(definitions)
    ]

    assert actual == expected


def test_inventory_and_export_fixtures_cover_new_artifact_shapes() -> None:
    definitions = _artifact_definitions()
    inventory = _fixture("responses/artifact-inventory.json")["data"]["items"]
    export_request = _fixture("requests/artifact-export.json")["artifacts"]
    exports = _fixture("responses/artifact-export.json")["data"]["artifacts"]
    required = {"acl", "dictionary", "client_script", "ui_action", "system_property"}

    assert required <= {item["artifact_type"] for item in inventory}
    assert required <= {item["artifact_type"] for item in export_request}
    assert required <= {item["artifact_type"] for item in exports}

    for artifact in exports:
        definition = definitions[artifact["artifact_type"]]
        assert set(artifact["values"]) <= set(definition["readableFields"])

    property_export = next(
        item for item in exports if item["artifact_type"] == "system_property"
    )
    assert "value" not in property_export["values"]


def _artifact_definitions() -> dict[str, dict[str, str | list[str]]]:
    source = CONSTANTS.read_text(encoding="utf-8")
    artifact_body = _balanced_body(source, source.index("artifactTypes: {") + 15)
    definitions: dict[str, dict[str, str | list[str]]] = {}
    position = 0
    entry_pattern = re.compile(r"\s*([a-z][a-z0-9_]*):\s*\{")

    while match := entry_pattern.match(artifact_body, position):
        key = match.group(1)
        body = _balanced_body(artifact_body, match.end() - 1)
        definitions[key] = {
            "capabilityCategory": _string_property(body, "capabilityCategory"),
            "table": _string_property(body, "table"),
            "nameField": _string_property(body, "nameField"),
            "readableFields": _string_array(body, "readableFields"),
            "writableFields": _string_array(body, "writableFields"),
        }
        position = match.end() + len(body) + 1
        separator = re.match(r"\s*,?", artifact_body[position:])
        position += separator.end() if separator else 0

    return definitions


def _balanced_body(source: str, opening_brace: int) -> str:
    depth = 0
    for index in range(opening_brace, len(source)):
        if source[index] == "{":
            depth += 1
        elif source[index] == "}":
            depth -= 1
            if depth == 0:
                return source[opening_brace + 1 : index]
    raise AssertionError("unbalanced JavaScript object")


def _string_property(body: str, property_name: str) -> str:
    match = re.search(rf"\b{property_name}:\s*'([^']+)'", body)
    assert match, property_name
    return match.group(1)


def _string_array(body: str, property_name: str) -> list[str]:
    match = re.search(rf"\b{property_name}:\s*\[(.*?)\]", body, re.DOTALL)
    assert match, property_name
    return re.findall(r"'([^']+)'", match.group(1))


def _fixture(relative_path: str) -> dict:
    return json.loads((FIXTURES / relative_path).read_text(encoding="utf-8"))
